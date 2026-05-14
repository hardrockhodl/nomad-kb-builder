"""
LLM-based verification of KB drafts against source text.

For each KB file in unverified/, finds the matching source section, sends both
to Ollama with a strict verification prompt, parses PASS/FLAG response, and
organizes the file into auto-approved/ or needs-review/.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from pipeline.ollama_client import DEFAULT_MODEL, OllamaResponse, call_ollama
from pipeline.section_detector import (
    DetectedSection,
    SectionDetectionResult,
    load_sections_from_json,
)


console = Console()


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class VerificationProblem:
    category: str
    claim: str
    issue: str
    severity: str


@dataclass
class VerificationResult:
    kb_filename: str
    kb_path: Path
    decision: str  # PASS | FLAG | ERROR
    problems: list[VerificationProblem] = field(default_factory=list)
    error_message: Optional[str] = None
    duration_seconds: float = 0.0
    retry_count: int = 0
    raw_response: str = ""
    source_section_title: Optional[str] = None


# ============================================================================
# Frontmatter parsing
# ============================================================================

def parse_kb_frontmatter(kb_path: Path) -> tuple[dict, str]:
    """
    Parse YAML frontmatter from a KB markdown file.

    Returns (frontmatter_dict, body_text). Raises ValueError on malformed input.
    """
    text = kb_path.read_text(encoding="utf-8")

    if not text.startswith("---"):
        raise ValueError(f"No frontmatter in {kb_path.name}")

    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"Malformed frontmatter in {kb_path.name}")

    yaml_text = match.group(1)
    body = match.group(2)

    try:
        frontmatter = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error in {kb_path.name}: {e}")

    if not isinstance(frontmatter, dict):
        raise ValueError(f"Frontmatter is not a dict in {kb_path.name}")

    return frontmatter, body


# ============================================================================
# Source text lookup
# ============================================================================

def _normalize_title(title: str) -> str:
    """Normalize title for comparison: lowercase, drop punctuation, collapse spaces."""
    if not title:
        return ""
    normalized = title.lower()
    # Drop chapter prefix like "Chapter 5: " or "5. "
    normalized = re.sub(r"^chapter\s+\d+:?\s+", "", normalized)
    normalized = re.sub(r"^\d+\.\s+", "", normalized)
    # Remove punctuation
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _extract_title_from_id(kb_id: str) -> str:
    """
    Extract a likely section title from a KB id field.

    Examples:
        "nx-os-troubleshooting-service-restarts" → "troubleshooting service restarts"
        "nx-os-hsrp-theory"                      → "hsrp"
        "nx-os-network-level-high-availability"  → "network level high availability"
        "ios-xe-configuring-vlan"                → "configuring vlan"
    """
    if not kb_id:
        return ""
    s = kb_id

    platform_prefixes = ("nx-os-", "nxos-", "ios-xe-", "iosxe-")
    for prefix in platform_prefixes:
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break

    type_suffixes = ("-theory", "-config", "-troubleshooting")
    for suffix in type_suffixes:
        if s.lower().endswith(suffix):
            s = s[: -len(suffix)]
            break

    return s.replace("-", " ").strip()


def find_source_section(
    frontmatter: dict,
    sections_result: SectionDetectionResult,
) -> Optional[DetectedSection]:
    """
    Find the source section that this KB was generated from.

    Match strategy (in order of preference):
      1. Exact title match against section_title in the correct chapter,
         using a candidate title extracted from frontmatter `id`.
      2. Substring title match (either direction) with the same candidate.
      3. Page-overlap fallback: section in chapter with most overlap, preferring
         H2 over H3 when overlaps are equal.
    """
    source = frontmatter.get("source", {})
    chapter_str = str(source.get("chapter", ""))
    pages = source.get("pages", [])
    kb_id = str(frontmatter.get("id", ""))

    # Find chapter — try number first ("Chapter 5: ..."), fall back to title.
    chapter = None
    match = re.search(r"(\d+)", chapter_str)
    if match:
        chapter_num = int(match.group(1))
        chapter = next(
            (ch for ch in sections_result.chapters if ch.number == chapter_num),
            None,
        )

    if chapter is None and chapter_str:
        chapter_title_norm = _normalize_title(chapter_str)
        for ch in sections_result.chapters:
            ch_norm = _normalize_title(ch.title)
            if ch_norm and (ch_norm == chapter_title_norm
                            or ch_norm in chapter_title_norm
                            or chapter_title_norm in ch_norm):
                chapter = ch
                break

    if chapter is None or not chapter.sections:
        return None

    # STRATEGY 1+2: Title match via id-derived candidate.
    if kb_id:
        candidate = _extract_title_from_id(kb_id)
        if candidate:
            candidate_norm = _normalize_title(candidate)

            # Exact normalized match
            for section in chapter.sections:
                if _normalize_title(section.section_title) == candidate_norm:
                    return section

            # Substring match — require both strings to be substantial (>=5 chars)
            # so short tokens like "ha" don't trigger spurious matches.
            if len(candidate_norm) >= 5:
                for section in chapter.sections:
                    sect_norm = _normalize_title(section.section_title)
                    if len(sect_norm) < 5:
                        continue
                    if candidate_norm in sect_norm or sect_norm in candidate_norm:
                        return section

    # STRATEGY 3: Page-overlap fallback.
    if not pages:
        return chapter.sections[0]

    kb_page_start = pages[0]
    kb_page_end = pages[-1] if len(pages) > 1 else pages[0]

    best_match: Optional[DetectedSection] = None
    best_overlap = -1

    for section in chapter.sections:
        overlap_start = max(kb_page_start, section.page_start)
        overlap_end = min(kb_page_end, section.page_end)
        overlap = max(0, overlap_end - overlap_start + 1)

        if overlap > best_overlap:
            best_overlap = overlap
            best_match = section
        elif overlap == best_overlap and best_match is not None:
            # Tie-break: prefer H2 over H3 (the more general section).
            if section.section_level == 2 and best_match.section_level == 3:
                best_match = section

    return best_match


def build_source_text(
    section: DetectedSection,
    chapter_summary: str,
    h3_children: Optional[list[DetectedSection]] = None,
) -> str:
    """
    Build source-text payload for verification.

    When the matched source is an H2 with H3 children, include the H3 content
    too — the KB may have been generated in h2_unit mode where the LLM saw
    the H2 plus all its H3 subsections as one combined input.
    """
    parts = [f"Chapter context: {chapter_summary}\n"]
    parts.append(f"Section: {section.section_title}\n")
    parts.append(f"Content:\n{section.content}\n")

    if h3_children:
        for h3 in h3_children:
            parts.append(f"\n### Subsection: {h3.section_title}\n")
            parts.append(h3.content)
            if h3.notes:
                parts.append("Subsection notes:")
                for note in h3.notes:
                    parts.append(f"- {note}")

    if section.notes:
        parts.append("Notes:")
        for note in section.notes:
            parts.append(f"- {note}")

    return "\n".join(parts)


# ============================================================================
# Verification call
# ============================================================================

def build_verifier_prompt(kb_content: str, source_text: str) -> str:
    """Build user prompt for verifier."""
    return (
        "# KB file to verify\n\n"
        "```markdown\n"
        f"{kb_content}\n"
        "```\n\n"
        "# Source text\n\n"
        f"{source_text}\n\n"
        "# Task\n\n"
        "Verify that every factual claim in the KB file is supported by the "
        "source text. Pay extra attention to numeric values, CLI commands, "
        "product/model references, and strong assertions (\"must\", \"always\", "
        "\"never\").\n\n"
        "Return JSON only.\n"
    )


def verify_kb(
    kb_path: Path,
    sections_result: SectionDetectionResult,
    prompts_dir: Path,
    model: str,
) -> VerificationResult:
    """Verify a single KB file against its source."""
    try:
        frontmatter, _body = parse_kb_frontmatter(kb_path)
    except ValueError as e:
        return VerificationResult(
            kb_filename=kb_path.name,
            kb_path=kb_path,
            decision="ERROR",
            error_message=f"Frontmatter parse error: {e}",
        )

    source_section = find_source_section(frontmatter, sections_result)
    if source_section is None:
        source_info = frontmatter.get("source", {})
        return VerificationResult(
            kb_filename=kb_path.name,
            kb_path=kb_path,
            decision="ERROR",
            error_message=(
                f"Could not find source section for "
                f"chapter={source_info.get('chapter')}, "
                f"pages={source_info.get('pages')}"
            ),
        )

    chapter = next(
        (ch for ch in sections_result.chapters
         if ch.number == source_section.chapter_number),
        None,
    )
    chapter_summary = chapter.summary if chapter else ""

    # If the matched source is an H2, include its H3 children so the verifier
    # sees the full content the KB was generated from (h2_unit mode).
    h3_children: list[DetectedSection] = []
    if chapter and source_section.section_level == 2:
        h3_children = [
            s for s in chapter.sections
            if s.section_level == 3 and s.parent_section == source_section.section_title
        ]

    source_text = build_source_text(source_section, chapter_summary, h3_children)

    system_prompt = (prompts_dir / "system_verifier.md").read_text(encoding="utf-8")
    kb_full_text = kb_path.read_text(encoding="utf-8")
    user_prompt = build_verifier_prompt(kb_full_text, source_text)

    response: OllamaResponse = call_ollama(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
    )

    if not response.success:
        return VerificationResult(
            kb_filename=kb_path.name,
            kb_path=kb_path,
            decision="ERROR",
            error_message=response.error,
            duration_seconds=response.duration_seconds,
            retry_count=response.retry_count,
            source_section_title=source_section.section_title,
        )

    parsed_data, parse_error = _parse_verifier_response(response.raw_text)
    if parse_error:
        return VerificationResult(
            kb_filename=kb_path.name,
            kb_path=kb_path,
            decision="ERROR",
            error_message=f"Parse error: {parse_error}",
            duration_seconds=response.duration_seconds,
            retry_count=response.retry_count,
            raw_response=response.raw_text,
            source_section_title=source_section.section_title,
        )

    problems = [
        VerificationProblem(
            category=p.get("category", "unknown"),
            claim=p.get("claim", ""),
            issue=p.get("issue", ""),
            severity=p.get("severity", "minor"),
        )
        for p in parsed_data.get("problems", [])
    ]

    return VerificationResult(
        kb_filename=kb_path.name,
        kb_path=kb_path,
        decision=parsed_data.get("decision", "FLAG"),
        problems=problems,
        duration_seconds=response.duration_seconds,
        retry_count=response.retry_count,
        raw_response=response.raw_text,
        source_section_title=source_section.section_title,
    )


def _parse_verifier_response(raw_text: str) -> tuple[dict, Optional[str]]:
    """Parse verifier JSON response. Returns (data, error_message)."""
    text = raw_text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}, "No JSON object found"
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            return {}, f"Invalid JSON: {e}"

    if not isinstance(data, dict):
        return {}, "Response is not a JSON object"
    if "decision" not in data:
        return {}, "Missing 'decision' field"
    if data["decision"] not in ("PASS", "FLAG"):
        return {}, f"Invalid decision: {data['decision']}"

    if "problems" not in data:
        data["problems"] = []

    return data, None


# ============================================================================
# Output writing
# ============================================================================

def write_verification_report(result: VerificationResult, report_path: Path) -> None:
    """Write human-readable verification report alongside KB file."""
    lines = [
        f"# Verification report: {result.kb_filename}",
        "",
        f"**Source section:** {result.source_section_title or 'Unknown'}",
        f"**Decision:** {result.decision}",
        f"**Problems found:** {len(result.problems)}",
        f"**Duration:** {result.duration_seconds:.1f}s",
        "",
    ]

    if result.decision == "PASS":
        lines.append("All factual claims verified against source. No issues found.")
    elif result.decision == "FLAG":
        lines.append("## Problems")
        lines.append("")

        for severity in ("critical", "major", "minor"):
            sev_problems = [p for p in result.problems if p.severity == severity]
            if not sev_problems:
                continue

            lines.append(f"### {severity.upper()} ({len(sev_problems)})")
            lines.append("")
            for p in sev_problems:
                lines.append(f"**Category:** {p.category}  ")
                lines.append(f"**Claim:** {p.claim}  ")
                lines.append(f"**Issue:** {p.issue}")
                lines.append("")
    elif result.decision == "ERROR":
        lines.append("## Error")
        lines.append("")
        lines.append(result.error_message or "Unknown error")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def organize_verified_files(
    results: list[VerificationResult],
    output_dir: Path,
) -> dict[str, int]:
    """
    Copy verified KB files into auto-approved/, needs-review/, or
    verification-errors/ based on the verifier decision. Each destination
    also gets a `.verification.md` sidecar report.

    Returns {"pass": N, "flag": M, "error": K}.
    """
    auto_approved_dir = output_dir / "kb-drafts" / "auto-approved"
    needs_review_dir = output_dir / "kb-drafts" / "needs-review"
    errors_dir = output_dir / "kb-drafts" / "verification-errors"

    auto_approved_dir.mkdir(parents=True, exist_ok=True)
    needs_review_dir.mkdir(parents=True, exist_ok=True)

    counts = {"pass": 0, "flag": 0, "error": 0}

    for result in results:
        if result.decision == "PASS":
            target_dir = auto_approved_dir
            counts["pass"] += 1
        elif result.decision == "FLAG":
            target_dir = needs_review_dir
            counts["flag"] += 1
        else:
            errors_dir.mkdir(parents=True, exist_ok=True)
            target_dir = errors_dir
            counts["error"] += 1

        shutil.copy2(result.kb_path, target_dir / result.kb_filename)

        report_name = result.kb_filename.replace(".md", ".verification.md")
        write_verification_report(result, target_dir / report_name)

    return counts


# ============================================================================
# Main pipeline
# ============================================================================

def verify_kbs(
    output_dir: Path,
    sections_path: Path,
    prompts_dir: Path,
    model: str = DEFAULT_MODEL,
    limit: Optional[int] = None,
) -> list[VerificationResult]:
    """Verify all KB files in unverified/."""
    unverified_dir = output_dir / "kb-drafts" / "unverified"
    if not unverified_dir.exists():
        raise FileNotFoundError(f"No unverified/ directory in {output_dir}")

    sections_result = load_sections_from_json(sections_path)

    kb_files = sorted(unverified_dir.glob("*.md"))

    if limit:
        original = len(kb_files)
        kb_files = kb_files[:limit]
        console.print(
            f"[yellow]Limit set: verifying first {limit} of {original} files[/yellow]"
        )

    console.print(f"[bold]Verifying {len(kb_files)} KB files...[/bold]")
    console.print(f"[dim]Model: {model}[/dim]")
    console.print()

    results: list[VerificationResult] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TextColumn("[cyan]{task.fields[current]}"),
        console=console,
    ) as progress:
        task = progress.add_task("Verifying", total=len(kb_files), current="")

        for kb_path in kb_files:
            progress.update(task, current=kb_path.stem[:50])
            result = verify_kb(
                kb_path=kb_path,
                sections_result=sections_result,
                prompts_dir=prompts_dir,
                model=model,
            )
            results.append(result)
            progress.advance(task)

    return results
