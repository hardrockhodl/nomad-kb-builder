"""
Section detection for extracted Cisco PDF content.

Takes an ExtractedDocument from pdf_extractor and produces a structured list
of chapters with H2/H3 sections. Uses the per-chapter ToC as ground truth for
H2 boundaries; H3 detection is heuristic.

The detector is intentionally deterministic — H2 vs H3 granularity decisions
for KB generation happen downstream (Step 3).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from pipeline.pdf_extractor import ExtractedDocument, PageContent


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class DetectedSection:
    chapter_number: int
    chapter_title: str
    section_title: str
    section_level: int  # 2 (H2) or 3 (H3)

    parent_section: Optional[str]  # H3 only; name of parent H2

    page_start: int
    page_end: int

    content: str
    notes: list[str] = field(default_factory=list)

    word_count: int = 0
    h3_count: int = 0

    chapter_summary: str = ""


@dataclass
class DetectedChapter:
    number: int
    title: str
    summary: str
    expected_sections: list[str]
    sections: list[DetectedSection]
    page_start: int
    page_end: int


@dataclass
class SectionDetectionResult:
    document_title: str
    source_pdf: Path
    chapters: list[DetectedChapter]
    warnings: list[str] = field(default_factory=list)

    @property
    def all_sections(self) -> list[DetectedSection]:
        return [s for ch in self.chapters for s in ch.sections]


# ============================================================================
# Line-based representation: each line tagged with its source page number
# ============================================================================

@dataclass
class TaggedLine:
    page_number: int
    text: str


def _build_tagged_lines(extraction: ExtractedDocument) -> list[TaggedLine]:
    """Flatten content pages into a single sequence of (page, line) tuples."""
    tagged: list[TaggedLine] = []
    for page in extraction.content_pages:
        for line in page.text.split("\n"):
            tagged.append(TaggedLine(page_number=page.page_number, text=line))
    return tagged


# ============================================================================
# Chapter detection
# ============================================================================

# Cisco PDFs render "CHAPTER N" with spaces between every letter due to
# typographic styling. The chapter title is on the immediately following line.
CHAPTER_MARKER_RE = re.compile(r"^C\s+H\s+A\s+P\s+T\s+E\s+R\s+(\d+)\s*$")


def _find_chapter_starts(lines: list[TaggedLine]) -> list[tuple[int, int, str]]:
    """
    Locate every chapter start.

    Returns a list of (line_index, chapter_number, chapter_title).
    The line_index points at the "C H A P T E R N" marker.
    The chapter title is taken from the next non-empty line.
    """
    results: list[tuple[int, int, str]] = []
    for i, tl in enumerate(lines):
        m = CHAPTER_MARKER_RE.match(tl.text.strip())
        if not m:
            continue
        chapter_number = int(m.group(1))

        title = ""
        for j in range(i + 1, min(i + 4, len(lines))):
            cand = lines[j].text.strip()
            if cand:
                title = cand
                break
        results.append((i, chapter_number, title))
    return results


# ============================================================================
# ToC extraction per chapter
# ============================================================================

# A ToC bullet looks like:
#     • Section Title, on page N
# The bullet character is "•" (U+2022). The ", on page <N>" suffix is the
# anchor we strip to get the section title.
TOC_BULLET_RE = re.compile(r"^\s*•\s*(.+?),\s*on\s+page\s+\d+\s*$")


def _extract_chapter_toc(
    lines: list[TaggedLine], chapter_start: int, chapter_end: int
) -> tuple[str, list[str], int]:
    """
    Extract the chapter intro paragraph (summary), the list of expected
    section titles from the bullet ToC, and the line index where the ToC
    ends (= where the first H2 section starts).

    Returns (summary, expected_sections, first_section_line_index).
    """
    # Skip the "C H A P T E R N" marker line and the chapter title line.
    cursor = chapter_start + 1
    while cursor < chapter_end and not lines[cursor].text.strip():
        cursor += 1
    cursor += 1  # advance past chapter title

    # Collect intro lines until we hit the first ToC bullet.
    intro_parts: list[str] = []
    while cursor < chapter_end:
        text = lines[cursor].text.strip()
        if TOC_BULLET_RE.match(text):
            break
        if text:
            intro_parts.append(text)
        cursor += 1

    summary = " ".join(intro_parts).strip()

    # Read consecutive bullet lines as expected sections.
    expected_sections: list[str] = []
    while cursor < chapter_end:
        text = lines[cursor].text.strip()
        m = TOC_BULLET_RE.match(text)
        if m:
            expected_sections.append(m.group(1).strip())
            cursor += 1
            continue
        # Allow a single blank line between bullets but bail on real content.
        if not text:
            cursor += 1
            continue
        break

    return summary, expected_sections, cursor


# ============================================================================
# Section title matching
# ============================================================================

def _normalize_for_match(s: str) -> str:
    """Collapse whitespace and lowercase for tolerant comparison."""
    return re.sub(r"\s+", " ", s).strip().lower()


def _find_section_line(
    lines: list[TaggedLine],
    section_title: str,
    search_from: int,
    search_to: int,
    forbidden_lines: set[int],
) -> tuple[Optional[int], bool]:
    """
    Find the line index where this section title appears as a standalone
    header (i.e. the line equals the title, modulo whitespace differences).

    Skip lines in `forbidden_lines` (already claimed by other sections or
    inside the ToC bullet range).

    Returns (line_index, used_fuzzy_match). line_index is None if not found.
    """
    target = _normalize_for_match(section_title)

    # Exact match (whitespace-tolerant).
    for i in range(search_from, search_to):
        if i in forbidden_lines:
            continue
        if _normalize_for_match(lines[i].text) == target:
            return i, False

    # Fuzzy fallback — flagged via warning by caller.
    best_ratio = 0.0
    best_idx: Optional[int] = None
    for i in range(search_from, search_to):
        if i in forbidden_lines:
            continue
        cand = _normalize_for_match(lines[i].text)
        if not cand or len(cand) > 120:
            continue
        # Quick filter: candidate length should be in same ballpark.
        if abs(len(cand) - len(target)) > 15:
            continue
        ratio = SequenceMatcher(None, target, cand).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i

    if best_ratio >= 0.88:
        return best_idx, True
    return None, False


# ============================================================================
# H3 detection within a section
# ============================================================================

# CLI prompts / commands we never want to treat as headers.
CLI_PROMPT_RE = re.compile(r"(switch|router)\s*[#(>]")
NUMERIC_ONLY_RE = re.compile(r"^[\d.\s]+$")
ENDS_WITH_TERMINAL_PUNCT_RE = re.compile(r"[.,:;!?]$")


def _looks_like_h3(line: str) -> bool:
    """
    Heuristic: is this line a plausible H3 subsection header?

    Standalone, short, capitalized, not a sentence, not CLI, not numeric.
    """
    s = line.strip()
    if not (5 <= len(s) <= 60):
        return False
    if not s[0].isupper():
        return False
    if ENDS_WITH_TERMINAL_PUNCT_RE.search(s):
        return False
    if CLI_PROMPT_RE.search(s):
        return False
    if NUMERIC_ONLY_RE.match(s):
        return False
    # Obvious non-headers
    lowered = s.lower()
    NON_HEADER_PREFIXES = (
        "step ", "example", "procedure", "purpose", "command or action",
        "configure terminal", "table ", "figure ", "before you begin",
        "summary steps", "detailed steps", "what to do next",
        "on page ", "this chapter", "this section",
    )
    for prefix in NON_HEADER_PREFIXES:
        if lowered.startswith(prefix):
            return False
    # Avoid lines that are themselves bullet content (start with "•").
    if s.startswith("•") or s.startswith("-"):
        return False
    # Skip standalone code-ish tokens (no spaces, all-lower) — likely CLI fragments.
    if " " not in s and s.lower() == s:
        return False
    return True


def _detect_h3_starts(
    lines: list[TaggedLine],
    section_start: int,
    section_end: int,
    chapter_title: str,
    h2_title: str,
    sibling_titles: set[str],
) -> list[tuple[int, str]]:
    """
    Find H3 candidate lines inside an H2 section.

    Filters out lines that are actually page footers (chapter title or
    H2 title repeated) and lines that match a sibling H2 (false positive).
    """
    chapter_norm = _normalize_for_match(chapter_title)
    h2_norm = _normalize_for_match(h2_title)
    sibling_norms = {_normalize_for_match(t) for t in sibling_titles}

    h3s: list[tuple[int, str]] = []
    # Skip the first line because it is the H2 title itself.
    for i in range(section_start + 1, section_end):
        text = lines[i].text
        if not _looks_like_h3(text):
            continue

        norm = _normalize_for_match(text)
        if norm in (chapter_norm, h2_norm):
            continue  # page footer artifact
        if norm in sibling_norms:
            continue  # an H2 title that drifted into this range

        # Require the next non-empty line to look like content (not another
        # candidate header). This filters CLI-output tables where every line
        # is short and capitalized.
        followed_by_content = False
        for j in range(i + 1, min(i + 4, section_end)):
            cand = lines[j].text.strip()
            if not cand:
                continue
            # Content-y: lowercase start, "The"/"A"/"This"/"Cisco", or a
            # long sentence with terminal punctuation.
            if (cand[0].islower()
                or cand.startswith(("The ", "A ", "This ", "Cisco ",
                                    "For ", "To ", "When ", "If ", "You "))
                or (len(cand) > 60 and ENDS_WITH_TERMINAL_PUNCT_RE.search(cand))):
                followed_by_content = True
            break
        if not followed_by_content:
            continue

        h3s.append((i, text.strip()))
    return h3s


# ============================================================================
# Note extraction
# ============================================================================

NOTE_LINE_RE = re.compile(r"^\s*Note\s*$")


def _extract_notes(lines: list[TaggedLine], start: int, end: int) -> list[str]:
    """
    Extract Cisco "Note" blocks within [start, end).

    Cisco renders notes with the literal word "Note" on its own line. The
    note body is the 1-3 lines immediately before that anchor. If those
    are missing/empty, we fall back to the 1-3 lines after.
    """
    notes: list[str] = []
    for i in range(start, end):
        if not NOTE_LINE_RE.match(lines[i].text):
            continue

        # Collect up to 3 preceding non-empty lines.
        preceding: list[str] = []
        j = i - 1
        while j >= start and len(preceding) < 3:
            t = lines[j].text.strip()
            if not t:
                j -= 1
                continue
            # Stop if we run into a probable header (very short title-like line).
            if len(t) < 30 and not ENDS_WITH_TERMINAL_PUNCT_RE.search(t):
                break
            preceding.insert(0, t)
            j -= 1

        if preceding:
            notes.append(" ".join(preceding))
            continue

        # Fallback: look at following lines.
        following: list[str] = []
        k = i + 1
        while k < end and len(following) < 3:
            t = lines[k].text.strip()
            if not t:
                k += 1
                continue
            if len(t) < 30 and not ENDS_WITH_TERMINAL_PUNCT_RE.search(t):
                break
            following.append(t)
            k += 1
        if following:
            notes.append(" ".join(following))

    return notes


# ============================================================================
# Content extraction
# ============================================================================

def _content_text(
    lines: list[TaggedLine],
    start: int,
    end: int,
    chapter_title: str,
    section_title: str,
) -> str:
    """
    Build the raw content text for [start, end), filtering out page footers
    that repeat the chapter title or current section title verbatim.
    """
    chapter_norm = _normalize_for_match(chapter_title)
    section_norm = _normalize_for_match(section_title)

    out: list[str] = []
    for i in range(start, end):
        t = lines[i].text
        norm = _normalize_for_match(t)
        if norm in (chapter_norm, section_norm):
            continue
        out.append(t)
    return "\n".join(out).strip()


def _count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


# ============================================================================
# Page range lookup
# ============================================================================

def _page_range(lines: list[TaggedLine], start: int, end: int) -> tuple[int, int]:
    if start >= end:
        # Defensive: empty range. Anchor to the start line's page.
        p = lines[start].page_number if start < len(lines) else 0
        return p, p
    pages = [lines[i].page_number for i in range(start, end)]
    return min(pages), max(pages)


# ============================================================================
# Main entry point
# ============================================================================

def detect_sections(extraction: ExtractedDocument) -> SectionDetectionResult:
    """
    Detect chapters and sections in an ExtractedDocument.

    Algorithm:
      1. Locate chapter boundaries.
      2. For each chapter, extract the summary intro and ToC bullets.
      3. Locate each ToC-named section as a standalone header line.
      4. Slice content between H2 boundaries; recurse for H3 candidates.
      5. Extract Note blocks per section.
    """
    lines = _build_tagged_lines(extraction)
    warnings: list[str] = []

    chapter_marks = _find_chapter_starts(lines)
    if not chapter_marks:
        warnings.append("No chapter markers found — detection aborted.")
        return SectionDetectionResult(
            document_title=extraction.document_title,
            source_pdf=extraction.source_path,
            chapters=[],
            warnings=warnings,
        )

    # Sentinel "next chapter" boundary at end of document.
    chapter_marks_with_end = [
        (idx, num, title, (chapter_marks[i + 1][0] if i + 1 < len(chapter_marks) else len(lines)))
        for i, (idx, num, title) in enumerate(chapter_marks)
    ]

    chapters: list[DetectedChapter] = []

    for chapter_start, chapter_number, chapter_title, chapter_end in chapter_marks_with_end:
        summary, expected_sections, toc_end = _extract_chapter_toc(
            lines, chapter_start, chapter_end
        )

        if not expected_sections:
            warnings.append(
                f"Chapter {chapter_number} ({chapter_title}): no ToC bullets found"
            )

        # Lines inside the ToC bullet range must not be matched as section
        # headers later — they're the bullets themselves.
        forbidden = set(range(chapter_start, toc_end))

        # Locate each expected section. We search forward starting at
        # toc_end and only allow later matches than the previous one so
        # we don't accept a footer repeat as the "next" section.
        section_anchors: list[tuple[str, int, bool]] = []  # (title, line_idx, fuzzy)
        cursor = toc_end
        for expected_title in expected_sections:
            line_idx, used_fuzzy = _find_section_line(
                lines, expected_title, cursor, chapter_end, forbidden
            )
            if line_idx is None:
                warnings.append(
                    f"Chapter {chapter_number}: section "
                    f"'{expected_title}' not found in chapter text"
                )
                continue
            if used_fuzzy:
                warnings.append(
                    f"Chapter {chapter_number}: section '{expected_title}' "
                    f"matched via fuzzy fallback at line {line_idx}"
                )
            section_anchors.append((expected_title, line_idx, used_fuzzy))
            cursor = line_idx + 1

        # Build section objects with their content ranges.
        sibling_titles = {t for t, _, _ in section_anchors}
        sections: list[DetectedSection] = []

        for i, (title, start_idx, _) in enumerate(section_anchors):
            end_idx = (
                section_anchors[i + 1][1]
                if i + 1 < len(section_anchors)
                else chapter_end
            )

            # H3 candidates inside this H2.
            h3_anchors = _detect_h3_starts(
                lines,
                section_start=start_idx,
                section_end=end_idx,
                chapter_title=chapter_title,
                h2_title=title,
                sibling_titles=sibling_titles,
            )

            # H2's own content runs from start_idx+1 up to either the first
            # H3 or the end of the section.
            h2_body_end = h3_anchors[0][0] if h3_anchors else end_idx
            h2_content = _content_text(
                lines, start_idx + 1, h2_body_end, chapter_title, title
            )
            h2_notes = _extract_notes(lines, start_idx + 1, h2_body_end)

            h2_page_start, h2_page_end = _page_range(lines, start_idx, end_idx)

            # Compute word count including H3 children.
            full_h2_text = _content_text(
                lines, start_idx + 1, end_idx, chapter_title, title
            )

            sections.append(DetectedSection(
                chapter_number=chapter_number,
                chapter_title=chapter_title,
                section_title=title,
                section_level=2,
                parent_section=None,
                page_start=h2_page_start,
                page_end=h2_page_end,
                content=h2_content,
                notes=h2_notes,
                word_count=_count_words(full_h2_text),
                h3_count=len(h3_anchors),
                chapter_summary=summary,
            ))

            # Append H3 sections.
            for j, (h3_start, h3_title) in enumerate(h3_anchors):
                h3_end = (
                    h3_anchors[j + 1][0]
                    if j + 1 < len(h3_anchors)
                    else end_idx
                )
                h3_content = _content_text(
                    lines, h3_start + 1, h3_end, chapter_title, h3_title
                )
                h3_notes = _extract_notes(lines, h3_start + 1, h3_end)
                h3_page_start, h3_page_end = _page_range(lines, h3_start, h3_end)

                sections.append(DetectedSection(
                    chapter_number=chapter_number,
                    chapter_title=chapter_title,
                    section_title=h3_title,
                    section_level=3,
                    parent_section=title,
                    page_start=h3_page_start,
                    page_end=h3_page_end,
                    content=h3_content,
                    notes=h3_notes,
                    word_count=_count_words(h3_content),
                    h3_count=0,
                    chapter_summary=summary,
                ))

        ch_page_start, ch_page_end = _page_range(lines, chapter_start, chapter_end)
        chapters.append(DetectedChapter(
            number=chapter_number,
            title=chapter_title,
            summary=summary,
            expected_sections=expected_sections,
            sections=sections,
            page_start=ch_page_start,
            page_end=ch_page_end,
        ))

    return SectionDetectionResult(
        document_title=extraction.document_title,
        source_pdf=extraction.source_path,
        chapters=chapters,
        warnings=warnings,
    )


# ============================================================================
# Serialization
# ============================================================================

def _slugify(s: str) -> str:
    """Lowercase, replace non-alphanumeric with dashes, collapse repeats."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "untitled"


def save_sections_json(result: SectionDetectionResult, output_path: Path) -> None:
    """Serialize the full detection result to JSON."""
    payload = {
        "document_title": result.document_title,
        "source_pdf": str(result.source_pdf),
        "warnings": result.warnings,
        "chapters": [
            {
                "number": ch.number,
                "title": ch.title,
                "summary": ch.summary,
                "expected_sections": ch.expected_sections,
                "page_start": ch.page_start,
                "page_end": ch.page_end,
                "sections": [asdict(s) for s in ch.sections],
            }
            for ch in result.chapters
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)


def save_sections_markdown(result: SectionDetectionResult, output_dir: Path) -> None:
    """
    Write each section as a Markdown file under output_dir/chapter-N-<slug>/.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for ch in result.chapters:
        ch_dir = output_dir / f"chapter-{ch.number}-{_slugify(ch.title)}"
        ch_dir.mkdir(parents=True, exist_ok=True)

        for idx, section in enumerate(ch.sections, start=1):
            slug = _slugify(section.section_title)
            level_prefix = "" if section.section_level == 2 else "h3-"
            filename = f"{idx:02d}-{level_prefix}{slug}.md"
            filepath = ch_dir / filename

            parent_line = (
                f"**Parent section:** {section.parent_section}  \n"
                if section.parent_section else ""
            )

            notes_block = ""
            if section.notes:
                notes_block = "\n## Notes (extracted)\n\n" + "\n".join(
                    f"- {n}" for n in section.notes
                ) + "\n"

            md = (
                f"# {section.section_title}\n\n"
                f"**Chapter:** {section.chapter_number}. {section.chapter_title}  \n"
                f"**Section level:** H{section.section_level}  \n"
                f"{parent_line}"
                f"**Page range:** {section.page_start}-{section.page_end}  \n"
                f"**Word count:** {section.word_count}  \n"
                f"**H3 subsections:** {section.h3_count}\n\n"
                f"## Chapter Summary (context only, not part of section)\n\n"
                f"{section.chapter_summary or '_(no chapter intro detected)_'}\n\n"
                f"## Section Content\n\n"
                f"{section.content or '_(empty)_'}\n"
                f"{notes_block}\n"
                f"---\n\n"
                f"*Raw structured data: this section will be processed by KB generator in Step 3*\n"
            )

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md)
