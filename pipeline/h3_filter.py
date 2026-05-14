"""
Filter H3 candidates to remove table noise.

Section detection produces many H3 candidates because Cisco PDFs flatten tables
into one cell per row. This module filters out candidates that are clearly not
real headers (model numbers, status values, system strings).

Strategy: medium-strict.
- Hard rejects: pattern matches against known noise (Cisco part numbers, etc.)
- Soft rejects: content < 20 words AND no header-keyword match
- Always accept: content >= 20 words and no hard reject
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pipeline.section_detector import DetectedSection, SectionDetectionResult


# ============================================================================
# Hard reject patterns
# ============================================================================

# Cisco product part numbers and serial numbers
CISCO_PRODUCT_RE = re.compile(
    r"^(N\d+K[-A-Z0-9]+|SAL\d+[A-Z0-9]+|C\d+[A-Z0-9]+|SUP-?[AB]\+?)$",
    re.IGNORECASE,
)

# Cisco log/CLI output lines that get extracted as standalone "headers" because
# PDF flow makes them look like short, capitalized lines.
CISCO_LOG_RE = re.compile(
    r"^(Module\s+\d+\s+(powered\s+down|powered\s+up|detected)|"
    r"Manual\s+power-on\s+of\s+Module|"
    r'Service\s+".*?"\s+\(PID\s+\d+\)|'
    r"\d{4}\s+\w{3}\s+\d+\s+\d+:\d+:\d+\s+switch|"
    r"switch\(config\)#|"
    r"switch#)",
    re.IGNORECASE,
)

# Pure status values (when used as standalone "H3")
STATUS_VALUES = {
    "active", "standby", "offline", "initializing", "unknown", "failed",
    "standby (failed)", "at bios", "not present", "ha standby",
    "ha standby (failed)", "active with ha standby", "active with no standby",
    "shutting down", "ha switchover in progress",
    "ha synchronization in progress", "active with failed standby", "other",
    "yes", "no", "ok", "na", "n/a", "true", "false",
}

# System/terminal output strings that appear in code blocks
SYSTEM_STRINGS = {
    "gnu grub", "loader version", "reset reason registers",
    "performing memory detection", "filesystem type", "boot loader",
    "bios found", "numcpus", "pmcon_1", "pmcon_2", "pmcon_3", "pm1_sts",
    "status 61", "status 62", "status 9f", "status 9e", "status 9a",
    "status 98", "status 90", "cisco ide", "serial port parameters",
    "testing 1 dram patterns", "total mem found", "memory test complete",
    "raw time read from hardware clock", "writing reset reason",
    "nx9 sup ver", "current standby sup", "pci devices enumeration",
    "iofpga found", "booting from primary rom",
}

# Table header repetitions (when appearing as standalone "H3")
TABLE_HEADERS = {
    "related topic", "document title", "mibs link", "convention",
    "description", "step", "command or action", "purpose", "feature",
    "maximum number", "modules", "mibs", "mib link", "changed in release",
    "where documented",
}


# ============================================================================
# Sentence-starter patterns (hard reject)
# ============================================================================
# H3 candidates whose title begins with one of these words are almost certainly
# extracted sentences, not real headers. Real Cisco headers start with nouns
# or technical terms.

SENTENCE_STARTERS = {
    "this", "if", "to", "when", "wait", "copies", "saves", "auto-copy",
    "for", "you", "the", "there", "a", "an", "use", "begins", "enters",
    "issues", "initiates", "performs", "configures", "returns", "specifies",
    "forces", "enables", "disables", "includes", "refers", "allows",
    "provides", "supports", "requires", "permits", "displays", "verifies",
}


# ============================================================================
# Soft accept patterns (keyword-based)
# ============================================================================

KEYWORD_PATTERNS = re.compile(
    r"\b(Redundancy|Restart|Restartability|Switchover|Failure|Module|Protocol|"
    r"Verifying|Replacing|Configuring|Configuration|Troubleshooting|"
    r"Compatibility|Mechanism|Characteristic)\b",
    re.IGNORECASE,
)

ENDS_WITH_KEYWORD = re.compile(
    r"\b(Redundancy|Restarts|Switchovers|Failures|Mechanisms|"
    r"Characteristics|Modules|Cards|Services)\s*$",
    re.IGNORECASE,
)


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class H3FilterDecision:
    section_title: str
    parent_section: str
    chapter_number: int
    accepted: bool
    reason: str
    word_count: int
    content_preview: str  # First 80 chars of content


@dataclass
class H3FilterResult:
    total_h3_input: int
    accepted_count: int
    rejected_count: int
    decisions: list[H3FilterDecision]

    @property
    def acceptance_rate(self) -> float:
        if self.total_h3_input == 0:
            return 0.0
        return self.accepted_count / self.total_h3_input


# ============================================================================
# Decision logic
# ============================================================================

DIGITS_SYMBOLS_RE = re.compile(r"^[\d\.\-\s]+$")


def evaluate_h3(section: DetectedSection) -> tuple[bool, str]:
    """
    Decide if an H3 candidate should be accepted or rejected.

    Returns (accepted, reason).
    """
    title = section.section_title.strip()
    title_lower = title.lower()

    # ---- HARD REJECTS ----

    if len(title) < 4:
        return False, "Title too short (<4 chars)"

    # Sentence-starter pattern
    title_words = title.split()
    first_word = title_words[0].lower() if title_words else ""
    if "-" in first_word:
        first_word_root = first_word.split("-")[0]
        if first_word_root in SENTENCE_STARTERS:
            return False, (
                f"Title starts with sentence-starter '{first_word}' "
                "(looks like prose, not header)"
            )
    if first_word in SENTENCE_STARTERS:
        return False, (
            f"Title starts with sentence-starter '{first_word}' "
            "(looks like prose, not header)"
        )

    # Empty content
    if section.word_count == 0:
        return False, "Zero word content (empty section)"

    if CISCO_PRODUCT_RE.match(title):
        return False, "Cisco product/serial number pattern"

    if CISCO_LOG_RE.match(title):
        return False, "Cisco syslog/CLI output line"

    if title_lower in STATUS_VALUES:
        return False, "Pure status value"

    if title_lower in SYSTEM_STRINGS:
        return False, "System/terminal output string"

    for sys_str in SYSTEM_STRINGS:
        if title_lower.startswith(sys_str):
            return False, f"Starts with system string: '{sys_str}'"

    if title_lower in TABLE_HEADERS:
        return False, "Table header repetition"

    if DIGITS_SYMBOLS_RE.match(title):
        return False, "Mostly digits/symbols"

    # ---- CONTENT-BASED ----

    word_count = section.word_count

    if word_count >= 20:
        return True, f"Sufficient content ({word_count} words)"

    if KEYWORD_PATTERNS.search(title) or ENDS_WITH_KEYWORD.search(title):
        return True, f"Keyword pattern (only {word_count} words content)"

    return False, f"Insufficient content ({word_count} words) and no keyword pattern"


# ============================================================================
# Duplicate H3 merging
# ============================================================================

def merge_duplicate_h3s(result: SectionDetectionResult) -> int:
    """
    Merge H3 sections that have the same title within the same parent H2.

    Cisco PDFs sometimes produce duplicate H3 detections because section text
    spans multiple pages and the same header appears as a continuation marker.
    These are not separate sections — they are the same section split by PDF
    page breaks.

    Within each chapter, group H3s by (parent_section, section_title). If
    multiple exist, keep the first and append other content to it.

    Returns the number of duplicate sections merged.
    """
    merge_count = 0

    for chapter in result.chapters:
        seen: dict[tuple[str, str], DetectedSection] = {}
        new_sections: list[DetectedSection] = []

        for s in chapter.sections:
            if s.section_level == 2:
                new_sections.append(s)
                continue

            key = (s.parent_section or "", s.section_title)

            if key not in seen:
                seen[key] = s
                new_sections.append(s)
                continue

            # Duplicate — merge into first occurrence.
            first = seen[key]
            if s.content.strip():
                first.content = first.content.rstrip() + "\n\n" + s.content.lstrip()
            first.page_end = max(first.page_end, s.page_end)
            first.notes.extend(s.notes)
            first.word_count = len(first.content.split())
            merge_count += 1

        chapter.sections = new_sections

    return merge_count


def filter_h3_sections(result: SectionDetectionResult) -> H3FilterResult:
    """
    Filter H3 candidates across all chapters.

    Mutates each chapter's `sections` list in place: rejected H3 sections are
    removed. Returns an H3FilterResult with the full decision log.
    """
    decisions: list[H3FilterDecision] = []

    for chapter in result.chapters:
        accepted_titles: set[str] = set()

        for s in chapter.sections:
            if s.section_level != 3:
                continue

            accepted, reason = evaluate_h3(s)
            content_preview = s.content.strip()[:80].replace("\n", " ")

            decisions.append(H3FilterDecision(
                section_title=s.section_title,
                parent_section=s.parent_section or "?",
                chapter_number=chapter.number,
                accepted=accepted,
                reason=reason,
                word_count=s.word_count,
                content_preview=content_preview,
            ))

            if accepted:
                accepted_titles.add(s.section_title)

        # Rebuild section list: keep all H2s, plus accepted H3s in original order.
        # Note: if a chapter has duplicate H3 titles (same string appearing
        # multiple times after detection), all instances of that title are
        # accepted together. This mirrors the original decision per title.
        chapter.sections = [
            s for s in chapter.sections
            if s.section_level == 2
            or (s.section_level == 3 and s.section_title in accepted_titles)
        ]

        # Recompute h3_count on H2 parents.
        h3_per_parent: Counter[str] = Counter(
            s.parent_section for s in chapter.sections if s.section_level == 3
        )
        for s in chapter.sections:
            if s.section_level == 2:
                s.h3_count = h3_per_parent.get(s.section_title, 0)

    total = len(decisions)
    accepted_n = sum(1 for d in decisions if d.accepted)

    return H3FilterResult(
        total_h3_input=total,
        accepted_count=accepted_n,
        rejected_count=total - accepted_n,
        decisions=decisions,
    )


# ============================================================================
# Output for inspection
# ============================================================================

def save_filter_report(filter_result: H3FilterResult, output_path: Path) -> None:
    """Save human-readable filter decisions for inspection."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rejected = [d for d in filter_result.decisions if not d.accepted]
    accepted = [d for d in filter_result.decisions if d.accepted]

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("H3 Filter Report\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Total H3 candidates: {filter_result.total_h3_input}\n")
        f.write(f"Accepted: {filter_result.accepted_count} "
                f"({filter_result.acceptance_rate * 100:.1f}%)\n")
        f.write(f"Rejected: {filter_result.rejected_count} "
                f"({(1 - filter_result.acceptance_rate) * 100:.1f}%)\n\n")

        f.write("=" * 70 + "\n")
        f.write("REJECTION REASONS (summary)\n")
        f.write("=" * 70 + "\n\n")

        reason_counts = Counter(d.reason for d in rejected)
        for reason, count in reason_counts.most_common():
            f.write(f"  {count:3d}  {reason}\n")

        f.write("\n")
        f.write("=" * 70 + "\n")
        f.write("REJECTED H3 CANDIDATES (full list)\n")
        f.write("=" * 70 + "\n\n")

        rejected_sorted = sorted(
            rejected,
            key=lambda d: (d.chapter_number, d.parent_section, d.section_title),
        )

        current_chapter: int | None = None
        current_parent: str | None = None
        for d in rejected_sorted:
            if d.chapter_number != current_chapter:
                f.write(f"\n--- Chapter {d.chapter_number} ---\n\n")
                current_chapter = d.chapter_number
                current_parent = None
            if d.parent_section != current_parent:
                f.write(f"  Parent: {d.parent_section}\n")
                current_parent = d.parent_section
            f.write(f"    REJECT: \"{d.section_title}\" ({d.word_count}w)\n")
            f.write(f"        Reason: {d.reason}\n")
            if d.content_preview:
                f.write(f"        Preview: {d.content_preview}\n")
            f.write("\n")

        f.write("\n")
        f.write("=" * 70 + "\n")
        f.write("ACCEPTED H3 SECTIONS\n")
        f.write("=" * 70 + "\n\n")

        accepted_sorted = sorted(
            accepted,
            key=lambda d: (d.chapter_number, d.parent_section, d.section_title),
        )
        current_chapter = None
        current_parent = None
        for d in accepted_sorted:
            if d.chapter_number != current_chapter:
                f.write(f"\n--- Chapter {d.chapter_number} ---\n\n")
                current_chapter = d.chapter_number
                current_parent = None
            if d.parent_section != current_parent:
                f.write(f"  Parent: {d.parent_section}\n")
                current_parent = d.parent_section
            f.write(f"    ACCEPT: \"{d.section_title}\" ({d.word_count}w) - {d.reason}\n")
