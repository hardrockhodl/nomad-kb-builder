"""
LLM-based KB generation from detected sections.

For each section, decides granularity (H2 as unit or split by H3), determines
the KB type (config/theory/troubleshooting), builds prompts, calls Ollama,
parses the response, and saves output.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from pipeline import get_llm_client
from pipeline.section_detector import (
    DetectedChapter,
    DetectedSection,
    SectionDetectionResult,
)

_llm = get_llm_client()
DEFAULT_MODEL = _llm.DEFAULT_MODEL
OllamaResponse = _llm.OllamaResponse
call_ollama = _llm.call_ollama


console = Console()


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class GenerationUnit:
    """A single unit to send to LLM (either H2 as unit or single H3)."""
    section: DetectedSection
    h3_children: list[DetectedSection]
    suggested_type: str  # config | theory | troubleshooting
    chapter_number: int
    chapter_title: str
    chapter_summary: str
    parent_section: Optional[str]


@dataclass
class GenerationResult:
    unit: GenerationUnit
    decision: str  # generate | skip | error
    kb_filename: Optional[str]
    kb_content: Optional[str]
    skip_reason: Optional[str]
    error_message: Optional[str]
    duration_seconds: float
    retry_count: int
    raw_response: str
    # config | theory | troubleshooting. With the unified prompt the LLM picks
    # this and returns it in the JSON response; we fall back to the
    # ``unit.suggested_type`` heuristic when the LLM omits or garbles it.
    kb_type: Optional[str] = None


# ============================================================================
# Granularity decision
# ============================================================================

def split_into_units(chapter: DetectedChapter) -> list[GenerationUnit]:
    """
    Split chapter sections into generation units based on granularity rules.

    Rules:
      - If H2.word_count < 400 AND <=2 H3 children: emit as h2_unit
        (combined H2 + H3 children content goes in one LLM call)
      - Otherwise: split. H2's own content becomes its own unit if >=100 words.
        Each H3 becomes its own unit.
    """
    units: list[GenerationUnit] = []

    h2_sections = [s for s in chapter.sections if s.section_level == 2]
    h3_by_parent: dict[str, list[DetectedSection]] = {}
    for s in chapter.sections:
        if s.section_level == 3 and s.parent_section:
            h3_by_parent.setdefault(s.parent_section, []).append(s)

    for h2 in h2_sections:
        h3_children = h3_by_parent.get(h2.section_title, [])

        if h2.word_count < 400 and len(h3_children) <= 2:
            units.append(GenerationUnit(
                section=h2,
                h3_children=h3_children,
                suggested_type=_suggest_type(h2, h3_children),
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                chapter_summary=chapter.summary,
                parent_section=None,
            ))
            continue

        # H3 split mode.
        # h2.word_count covers only pre-H3 text in our detection model.
        if h2.word_count >= 100:
            units.append(GenerationUnit(
                section=h2,
                h3_children=[],
                suggested_type=_suggest_type(h2, []),
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                chapter_summary=chapter.summary,
                parent_section=None,
            ))

        for h3 in h3_children:
            units.append(GenerationUnit(
                section=h3,
                h3_children=[],
                suggested_type=_suggest_type(h3, []),
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                chapter_summary=chapter.summary,
                parent_section=h2.section_title,
            ))

    return units


def _suggest_type(section: DetectedSection, children: list[DetectedSection]) -> str:
    """Heuristic suggestion of KB type. LLM may override in its decision."""
    title_lower = section.section_title.lower()
    content_lower = section.content.lower()[:500]

    if any(kw in title_lower for kw in (
        "troubleshooting", "verifying", "replacing", "displaying",
        "switchover possibilities",
    )):
        return "troubleshooting"
    if any(kw in title_lower for kw in ("restart", "failure", "recovery")):
        return "troubleshooting"

    if any(kw in title_lower for kw in (
        "configuring", "configuration", "installing", "enabling",
    )):
        return "config"
    if "config" in title_lower:
        return "config"
    if "configure terminal" in content_lower or "switch(config)" in content_lower:
        return "config"

    return "theory"


# ============================================================================
# Prompt building
# ============================================================================

def build_user_prompt(
    unit: GenerationUnit,
    document_title: str,
    template_path: Path,
) -> str:
    """Build user prompt from template + unit data."""
    template_text = template_path.read_text(encoding="utf-8")
    template = Template(template_text)

    content_parts = [unit.section.content]
    all_notes = list(unit.section.notes)

    for h3 in unit.h3_children:
        content_parts.append(f"\n\n### {h3.section_title}\n\n{h3.content}")
        all_notes.extend(h3.notes)

    full_content = "\n".join(content_parts).strip()

    if all_notes:
        notes_block = (
            "## Notes extracted from section\n\n"
            + "\n".join(f"- {n}" for n in all_notes)
        )
    else:
        notes_block = ""

    if unit.h3_children:
        page_start = min(
            unit.section.page_start, *[h.page_start for h in unit.h3_children]
        )
        page_end = max(
            unit.section.page_end, *[h.page_end for h in unit.h3_children]
        )
    else:
        page_start = unit.section.page_start
        page_end = unit.section.page_end

    page_range = (
        f"{page_start}-{page_end}" if page_start != page_end else f"{page_start}"
    )
    word_count = unit.section.word_count + sum(
        h.word_count for h in unit.h3_children
    )

    return template.safe_substitute(
        document_title=document_title,
        chapter_number=unit.chapter_number,
        chapter_title=unit.chapter_title,
        chapter_summary=unit.chapter_summary,
        section_title=unit.section.section_title,
        section_level=unit.section.section_level,
        parent_section=unit.parent_section or "None (this is a top-level H2)",
        page_range=page_range,
        word_count=word_count,
        section_content=full_content,
        notes_block=notes_block,
        kb_type=unit.suggested_type,
    )


def load_system_prompt(kb_type: str, prompts_dir: Path) -> str:
    """Load the unified generator prompt.

    The ``kb_type`` arg is accepted for backwards compatibility but ignored —
    the LLM now classifies the section type itself and emits ``kb_type`` in
    its response. The argument stays in the signature so call sites and the
    heuristic suggestion path don't need to know about this change.
    """
    del kb_type  # intentionally unused
    path = prompts_dir / "system_generator.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


# ============================================================================
# Response parsing
# ============================================================================

def parse_llm_response(raw_text: str) -> tuple[bool, dict, str]:
    """
    Parse LLM response (expected JSON).

    Returns (success, parsed_dict, error_message).
    """
    text = raw_text.strip()

    # Strip markdown code fences if the LLM wrapped its output.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: find a JSON object inside the text.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return False, {}, "No JSON object found in response"
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e2:
            return False, {}, f"Invalid JSON in response: {e2}"

    if not isinstance(data, dict):
        return False, {}, "Response is not a JSON object"

    if "decision" not in data:
        return False, {}, "Missing 'decision' field"

    if data["decision"] not in ("generate", "skip"):
        return False, {}, f"Invalid decision: {data['decision']}"

    if data["decision"] == "generate":
        if not data.get("kb_filename"):
            return False, {}, "Missing kb_filename for generate decision"
        if not data.get("kb_content"):
            return False, {}, "Missing kb_content for generate decision"

    return True, data, ""


# ============================================================================
# Generation
# ============================================================================

def _build_unit_prompts(
    unit: GenerationUnit,
    document_title: str,
    prompts_dir: Path,
) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for one generation unit."""
    system_prompt = load_system_prompt(unit.suggested_type, prompts_dir)
    user_prompt = build_user_prompt(
        unit, document_title, prompts_dir / "user_template.md"
    )
    return system_prompt, user_prompt


_VALID_KB_TYPES = ("config", "theory", "troubleshooting")


def _resolve_kb_type(parsed: dict, fallback: str) -> str:
    """LLM-returned ``kb_type`` if valid, else the heuristic ``suggested_type``."""
    candidate = parsed.get("kb_type")
    if isinstance(candidate, str) and candidate in _VALID_KB_TYPES:
        return candidate
    return fallback


def _result_from_response(
    unit: GenerationUnit, response: OllamaResponse,
) -> GenerationResult:
    """Turn an LLM response into a GenerationResult, handling parse failures."""
    if not response.success:
        return GenerationResult(
            unit=unit,
            decision="error",
            kb_filename=None,
            kb_content=None,
            skip_reason=None,
            error_message=response.error,
            duration_seconds=response.duration_seconds,
            retry_count=response.retry_count,
            raw_response="",
            kb_type=unit.suggested_type,
        )

    success, parsed, error = parse_llm_response(response.raw_text)
    if not success:
        return GenerationResult(
            unit=unit,
            decision="error",
            kb_filename=None,
            kb_content=None,
            skip_reason=None,
            error_message=f"Parse error: {error}",
            duration_seconds=response.duration_seconds,
            retry_count=response.retry_count,
            raw_response=response.raw_text,
            kb_type=unit.suggested_type,
        )

    return GenerationResult(
        unit=unit,
        decision=parsed["decision"],
        kb_filename=parsed.get("kb_filename"),
        kb_content=parsed.get("kb_content"),
        skip_reason=parsed.get("skip_reason"),
        error_message=None,
        duration_seconds=response.duration_seconds,
        retry_count=response.retry_count,
        raw_response=response.raw_text,
        kb_type=_resolve_kb_type(parsed, unit.suggested_type),
    )


def generate_kb_for_unit(
    unit: GenerationUnit,
    document_title: str,
    prompts_dir: Path,
    model: str,
) -> GenerationResult:
    """Generate KB file (or skip decision) for one unit."""
    system_prompt, user_prompt = _build_unit_prompts(
        unit, document_title, prompts_dir,
    )

    response: OllamaResponse = call_ollama(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
    )

    return _result_from_response(unit, response)


# ============================================================================
# Output writing
# ============================================================================

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "untitled"


def save_generation_result(
    result: GenerationResult,
    output_dir: Path,
    save_debug: bool = True,
) -> None:
    """Save generation result to the appropriate output location."""
    unverified_dir = output_dir / "kb-drafts" / "unverified"
    skipped_dir = output_dir / "kb-drafts" / "skipped"
    debug_dir = output_dir / "debug" / "llm-calls"

    unverified_dir.mkdir(parents=True, exist_ok=True)
    skipped_dir.mkdir(parents=True, exist_ok=True)
    if save_debug:
        debug_dir.mkdir(parents=True, exist_ok=True)

    section_slug = _slug(result.unit.section.section_title)

    if result.decision == "generate":
        llm_filename = result.kb_filename or section_slug
        # Strip any extension the LLM included, then re-normalize.
        if llm_filename.endswith(".md"):
            llm_filename = llm_filename[:-3]
        filename = _slug(llm_filename) + ".md"

        output_path = unverified_dir / filename
        if output_path.exists():
            base = output_path.stem
            counter = 2
            while output_path.exists():
                output_path = unverified_dir / f"{base}-{counter}.md"
                counter += 1

        output_path.write_text(result.kb_content or "", encoding="utf-8")

    elif result.decision == "skip":
        skip_path = skipped_dir / f"{section_slug}.skip.txt"
        skip_path.write_text(
            f"Section: {result.unit.section.section_title}\n"
            f"Chapter: {result.unit.chapter_number} - {result.unit.chapter_title}\n"
            f"Parent: {result.unit.parent_section or 'None'}\n"
            f"Suggested type: {result.unit.suggested_type}\n\n"
            f"Skip reason: {result.skip_reason or 'Not specified'}\n",
            encoding="utf-8",
        )

    elif result.decision == "error":
        error_dir = output_dir / "kb-drafts" / "errors"
        error_dir.mkdir(parents=True, exist_ok=True)
        (error_dir / f"{section_slug}.error.txt").write_text(
            f"Section: {result.unit.section.section_title}\n"
            f"Chapter: {result.unit.chapter_number}\n"
            f"Error: {result.error_message}\n"
            f"Retries: {result.retry_count}\n\n"
            f"Raw response (first 2000 chars):\n{result.raw_response[:2000]}\n",
            encoding="utf-8",
        )

    if save_debug:
        debug_data = {
            "section_title": result.unit.section.section_title,
            "chapter": result.unit.chapter_number,
            "decision": result.decision,
            "duration_seconds": result.duration_seconds,
            "retry_count": result.retry_count,
            "error": result.error_message,
            "raw_response_length": len(result.raw_response),
            "raw_response_preview": result.raw_response[:500],
        }
        (debug_dir / f"{section_slug}.debug.json").write_text(
            json.dumps(debug_data, indent=2), encoding="utf-8"
        )


# ============================================================================
# Main pipeline
# ============================================================================

def generate_kbs(
    sections_result: SectionDetectionResult,
    output_dir: Path,
    prompts_dir: Path,
    model: str = DEFAULT_MODEL,
    limit: Optional[int] = None,
) -> list[GenerationResult]:
    """
    Generate KB files for all sections.

    Args:
        sections_result: From section detection
        output_dir: Where to write kb-drafts/
        prompts_dir: Where prompt files live
        model: Ollama model name
        limit: If set, only process the first N units (for testing)
    """
    all_units: list[GenerationUnit] = []
    for chapter in sections_result.chapters:
        all_units.extend(split_into_units(chapter))

    if limit:
        original = len(all_units)
        all_units = all_units[:limit]
        console.print(
            f"[yellow]Limit set: processing first {limit} of {original} units[/yellow]"
        )

    console.print(f"[bold]Generating KBs for {len(all_units)} units...[/bold]")
    console.print(f"[dim]Model: {model}[/dim]")

    batch_fn = getattr(_llm, "call_vllm_batch", None)
    if batch_fn is not None:
        console.print("[dim]Backend: vLLM (parallel batch mode)[/dim]")
    console.print()

    if batch_fn is not None:
        return _generate_kbs_batch(
            all_units=all_units,
            sections_result=sections_result,
            output_dir=output_dir,
            prompts_dir=prompts_dir,
            model=model,
            batch_fn=batch_fn,
        )

    return _generate_kbs_sequential(
        all_units=all_units,
        sections_result=sections_result,
        output_dir=output_dir,
        prompts_dir=prompts_dir,
        model=model,
    )


def _generate_kbs_sequential(
    all_units: list[GenerationUnit],
    sections_result: SectionDetectionResult,
    output_dir: Path,
    prompts_dir: Path,
    model: str,
) -> list[GenerationResult]:
    results: list[GenerationResult] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TextColumn("[cyan]{task.fields[current]}"),
        console=console,
    ) as progress:
        task = progress.add_task("Generating", total=len(all_units), current="")

        for unit in all_units:
            progress.update(task, current=unit.section.section_title[:50])

            result = generate_kb_for_unit(
                unit=unit,
                document_title=sections_result.document_title,
                prompts_dir=prompts_dir,
                model=model,
            )

            save_generation_result(result, output_dir)
            results.append(result)

            progress.advance(task)

    return results


def _generate_kbs_batch(
    all_units: list[GenerationUnit],
    sections_result: SectionDetectionResult,
    output_dir: Path,
    prompts_dir: Path,
    model: str,
    batch_fn,
) -> list[GenerationResult]:
    """Build all prompts up front, fan out via call_vllm_batch, then save."""
    requests: list[tuple[str, str]] = [
        _build_unit_prompts(u, sections_result.document_title, prompts_dir)
        for u in all_units
    ]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TextColumn("[cyan]{task.fields[current]}"),
        console=console,
    ) as progress:
        task = progress.add_task("Generating", total=len(all_units), current="")

        def on_done(idx: int, _resp: OllamaResponse) -> None:
            progress.update(
                task,
                advance=1,
                current=all_units[idx].section.section_title[:50],
            )

        responses = asyncio.run(batch_fn(
            requests,
            model=model,
            progress_callback=on_done,
        ))

    results = [_result_from_response(u, r) for u, r in zip(all_units, responses)]
    for result in results:
        save_generation_result(result, output_dir)

    return results
