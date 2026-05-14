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


if __name__ == '__main__':
    cli()
