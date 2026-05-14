"""
nomad-kb-builder: AI-assisted Cisco PDF → Nomad KB markdown pipeline.

Usage:
    python kb_builder.py extract <pdf-path>
    python kb_builder.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pipeline.pdf_extractor import extract_pdf, save_extraction

console = Console()


@click.group()
def cli():
    """nomad-kb-builder: convert Cisco PDFs into Nomad-compatible KB markdown."""
    pass


@cli.command()
@click.argument('pdf_path', type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option('--output-dir', '-o', type=click.Path(path_type=Path), default=None,
              help='Output directory (default: output/<pdf-basename>)')
def extract(pdf_path: Path, output_dir: Path | None):
    """Step 1: Extract text from a PDF, filtering boilerplate."""

    # Default output: output/<basename>/
    if output_dir is None:
        basename = pdf_path.stem
        output_dir = Path("output") / basename

    console.print(f"[bold]Extracting:[/bold] {pdf_path}")
    console.print(f"[bold]Output to:[/bold] {output_dir}")
    console.print()

    try:
        extraction = extract_pdf(pdf_path)
    except Exception as e:
        console.print(f"[bold red]Extraction failed:[/bold red] {e}")
        sys.exit(1)

    # Save to disk
    summary_path = save_extraction(extraction, output_dir)

    # Print summary
    table = Table(title="Extraction Summary", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value")

    table.add_row("Document", extraction.document_title)
    table.add_row("Total pages", str(extraction.total_pages))
    table.add_row("Content pages", str(len(extraction.content_pages)))
    table.add_row("Boilerplate pages", str(len(extraction.boilerplate_pages)))

    content_pages_with_headers = sum(
        1 for p in extraction.content_pages if p.detected_headers
    )
    table.add_row("Pages with detected headers", str(content_pages_with_headers))

    total_headers = sum(len(p.detected_headers) for p in extraction.content_pages)
    table.add_row("Total detected headers", str(total_headers))

    console.print(table)
    console.print()
    console.print(f"[green]✓[/green] Output saved to {output_dir}/")
    console.print(f"  - extraction.txt    (summary + boilerplate decisions)")
    console.print(f"  - content.txt       (full content of non-boilerplate pages)")
    console.print(f"  - boilerplate.txt   (filtered pages for debugging)")


@cli.command('detect-sections')
@click.argument('extraction_dir', type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('--output-dir', '-o', type=click.Path(path_type=Path), default=None,
              help='Output directory (default: same as extraction_dir)')
@click.option('--skip-filter', is_flag=True, default=False,
              help='Skip H3 noise filtering (keeps all candidates)')
def detect_sections_cmd(extraction_dir: Path, output_dir: Path | None, skip_filter: bool):
    """Step 2: Detect sections in extracted PDF content."""

    # Find the PDF that was extracted (look in input/ matching the dir name)
    pdf_basename = extraction_dir.name
    pdf_candidates = list(Path("input").glob(f"{pdf_basename}.pdf"))
    if not pdf_candidates:
        console.print(f"[red]No PDF found matching {pdf_basename} in input/[/red]")
        sys.exit(1)

    from pipeline.pdf_extractor import extract_pdf
    from pipeline.section_detector import (
        detect_sections,
        save_sections_json,
        save_sections_markdown,
    )

    console.print(f"[bold]Re-extracting PDF for section detection...[/bold]")
    extraction = extract_pdf(pdf_candidates[0])

    console.print(f"[bold]Detecting sections...[/bold]")
    result = detect_sections(extraction)

    if output_dir is None:
        output_dir = extraction_dir

    h3_before = sum(1 for s in result.all_sections if s.section_level == 3)
    h3_after = h3_before
    h3_rejected = 0

    if not skip_filter:
        from pipeline.h3_filter import filter_h3_sections, save_filter_report

        console.print(f"[bold]Filtering H3 noise...[/bold]")
        filter_result = filter_h3_sections(result)

        report_path = output_dir / "rejected-h3s.txt"
        save_filter_report(filter_result, report_path)

        h3_before = filter_result.total_h3_input
        h3_after = filter_result.accepted_count
        h3_rejected = filter_result.rejected_count

    json_path = output_dir / "sections.json"
    md_dir = output_dir / "sections"

    # Clean previous markdown output so stale section files don't linger when
    # indices/filenames shift between runs (especially after H3 filtering).
    if md_dir.exists():
        import shutil
        shutil.rmtree(md_dir)

    save_sections_json(result, json_path)
    save_sections_markdown(result, md_dir)

    table = Table(title="Section Detection Summary", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value")

    table.add_row("Document", result.document_title)
    table.add_row("Chapters detected", str(len(result.chapters)))
    table.add_row(
        "Total H2 sections",
        str(sum(1 for s in result.all_sections if s.section_level == 2)),
    )

    if not skip_filter:
        table.add_row("Total H3 candidates (before filter)", str(h3_before))
        table.add_row("H3 sections (after filter)", str(h3_after))
        table.add_row("H3 candidates rejected", str(h3_rejected))
    else:
        table.add_row("Total H3 sections (unfiltered)", str(h3_before))

    table.add_row(
        "Total notes extracted",
        str(sum(len(s.notes) for s in result.all_sections)),
    )
    table.add_row("Warnings", str(len(result.warnings)))

    console.print(table)

    if result.warnings:
        console.print("\n[yellow bold]Warnings:[/yellow bold]")
        for w in result.warnings:
            console.print(f"  [yellow]⚠[/yellow]  {w}")

    console.print(f"\n[green]✓[/green] Saved to {output_dir}/")
    console.print(f"  - sections.json    (machine-readable master)")
    console.print(f"  - sections/        (markdown per section for review)")
    if not skip_filter:
        console.print(f"  - rejected-h3s.txt (H3 filter decisions)")


if __name__ == '__main__':
    cli()
