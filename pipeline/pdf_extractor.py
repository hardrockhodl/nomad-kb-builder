"""
PDF text extraction for Cisco configuration guides.

Extracts text per page with metadata, filters out boilerplate (covers, ToC,
preface, index), and produces structured output that section detection can
build on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pymupdf


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class PageContent:
    """Extracted content from a single PDF page."""
    page_number: int  # 1-indexed (matches PDF page numbering)
    text: str
    is_boilerplate: bool
    boilerplate_reason: Optional[str] = None

    # Future-proofing: structural hints for section detection
    has_chapter_header: bool = False
    has_section_header: bool = False
    detected_headers: list[str] = field(default_factory=list)


@dataclass
class ExtractedDocument:
    """Complete extraction result for a PDF."""
    source_path: Path
    document_title: str
    total_pages: int
    pages: list[PageContent]

    @property
    def content_pages(self) -> list[PageContent]:
        """Pages that contain actual content (not boilerplate)."""
        return [p for p in self.pages if not p.is_boilerplate]

    @property
    def boilerplate_pages(self) -> list[PageContent]:
        """Pages filtered out as boilerplate."""
        return [p for p in self.pages if p.is_boilerplate]


# ============================================================================
# Boilerplate detection
# ============================================================================

# Patterns that indicate a page is boilerplate (not actual content)
BOILERPLATE_PATTERNS = [
    # Legal pages
    (r"THE SPECIFICATIONS AND INFORMATION REGARDING THE PRODUCTS", "Legal notice"),
    (r"Cisco and the Cisco logo are trademarks", "Trademark notice"),

    # Table of contents (typically has "CONTENTS" header and many page references)
    (r"^CONTENTS\s*$", "Table of contents header"),

    # Preface boilerplate
    (r"^PREFACE\s+Preface\s+", "Preface header"),
    (r"This preface includes the following sections", "Preface intro"),
    (r"^Reference Preface Map here", "Preface placeholder"),

    # Audience/conventions/documentation feedback
    (r"^Audience\s*$", "Audience section"),
    (r"This publication is for network administrators", "Audience description"),
    (r"^Document Conventions\s*$", "Document conventions"),
    (r"^Documentation Feedback\s*$", "Documentation feedback"),
    (r"^Related Documentation for Cisco Nexus", "Related documentation"),
    (r"^Communications, services, and additional information", "Communications section"),
    (r"^Cisco Bug Search Tool\s*$", "Bug search section"),

    # Index pages (typically have alphabetical entries with page numbers)
    (r"^INDEX\s*$", "Index header"),
    (r"^I\s*N\s*D\s*E\s*X\s*$", "Index header (spaced)"),
]


def is_page_boilerplate(text: str, page_number: int) -> tuple[bool, Optional[str]]:
    """
    Determine if a page is boilerplate.

    Returns (is_boilerplate, reason). Reason is None if not boilerplate.
    """
    # First two pages are always cover + legal in Cisco PDFs
    if page_number <= 2:
        return True, "Cover or legal page (first two pages)"

    # Check against patterns
    for pattern, reason in BOILERPLATE_PATTERNS:
        if re.search(pattern, text, re.MULTILINE | re.IGNORECASE):
            return True, reason

    # Pages with very little text (typically separator pages)
    cleaned = re.sub(r'\s+', ' ', text).strip()
    if len(cleaned) < 200:
        return True, f"Sparse content ({len(cleaned)} chars)"

    # Pages that are mostly a single line repeated (page break artifacts)
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if len(lines) <= 3:
        return True, f"Too few lines ({len(lines)})"

    return False, None


# ============================================================================
# Page text cleaning
# ============================================================================

def clean_page_text(text: str, document_title: str) -> str:
    """
    Remove headers, footers, and other repeated artifacts from page text.

    Cisco PDFs typically have:
    - Document title in footer
    - Chapter name in header
    - Page numbers
    """
    lines = text.split('\n')
    cleaned_lines = []

    # Build a normalized title for matching (handle minor formatting differences)
    title_normalized = re.sub(r'\s+', ' ', document_title).strip().lower()

    for line in lines:
        line_stripped = line.strip()
        line_normalized = re.sub(r'\s+', ' ', line_stripped).lower()

        # Skip lines matching document title (footer)
        if line_normalized == title_normalized:
            continue

        # Skip lines that are just page numbers (roman or arabic)
        if re.match(r'^[ivxlcdm]+$', line_stripped, re.IGNORECASE):
            continue
        if re.match(r'^\d+$', line_stripped):
            continue
        if re.match(r'^IN-\d+$', line_stripped):  # index pagination
            continue

        # Skip empty lines (will be re-added consistently)
        if not line_stripped:
            continue

        cleaned_lines.append(line)

    return '\n'.join(cleaned_lines)


# ============================================================================
# Header detection (structural hints for next pipeline step)
# ============================================================================

CHAPTER_HEADER_PATTERN = re.compile(r'^CHAPTER\s+\d+', re.MULTILINE)


def detect_headers(text: str) -> tuple[bool, bool, list[str]]:
    """
    Detect structural elements in page text.

    Returns (has_chapter, has_section, detected_headers).

    This is preliminary detection. Full section detection happens in Step 2.
    """
    has_chapter = bool(CHAPTER_HEADER_PATTERN.search(text))

    # Section headers are heuristically: lines that are short, capitalized,
    # and followed by content. This is fragile and will be improved in Step 2.
    headers = []
    lines = text.split('\n')

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Heuristic: short line that starts with capital, no punctuation at end
        # except common section markers
        if 5 <= len(stripped) <= 80:
            if stripped[0].isupper() and not stripped.endswith('.'):
                # Avoid CLI commands and similar
                if not re.match(r'^[a-z\s]+#', stripped):
                    # Check if next non-empty line looks like content
                    for j in range(i + 1, min(i + 3, len(lines))):
                        if lines[j].strip():
                            if lines[j][0].islower() or lines[j].strip().startswith(('The ', 'A ', 'This ')):
                                headers.append(stripped)
                                break
                            break

    has_section = len(headers) > 0
    return has_chapter, has_section, headers


# ============================================================================
# Main extraction function
# ============================================================================

def extract_pdf(pdf_path: Path) -> ExtractedDocument:
    """
    Extract structured content from a PDF.

    Args:
        pdf_path: Path to the PDF file

    Returns:
        ExtractedDocument with per-page content and metadata
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = pymupdf.open(str(pdf_path))

    # Get document title from metadata or filename
    metadata = doc.metadata
    document_title = metadata.get('title', '').strip() or pdf_path.stem

    # If metadata title is generic, try to extract from first content page
    if not document_title or document_title.lower() in ('', 'untitled'):
        document_title = pdf_path.stem

    pages: list[PageContent] = []

    for page_index in range(doc.page_count):
        page = doc[page_index]
        page_number = page_index + 1  # 1-indexed

        # Extract text
        raw_text = page.get_text()

        # Check if boilerplate
        is_boilerplate, reason = is_page_boilerplate(raw_text, page_number)

        if is_boilerplate:
            pages.append(PageContent(
                page_number=page_number,
                text=raw_text,  # Keep raw for debug
                is_boilerplate=True,
                boilerplate_reason=reason
            ))
            continue

        # Clean text
        cleaned_text = clean_page_text(raw_text, document_title)

        # Detect structural hints
        has_chapter, has_section, headers = detect_headers(cleaned_text)

        pages.append(PageContent(
            page_number=page_number,
            text=cleaned_text,
            is_boilerplate=False,
            has_chapter_header=has_chapter,
            has_section_header=has_section,
            detected_headers=headers
        ))

    doc.close()

    return ExtractedDocument(
        source_path=pdf_path,
        document_title=document_title,
        total_pages=len(pages),
        pages=pages
    )


# ============================================================================
# Serialization (for inspection between pipeline steps)
# ============================================================================

def save_extraction(extraction: ExtractedDocument, output_dir: Path) -> Path:
    """
    Save extraction result to a structured format for inspection and
    consumption by later pipeline steps.

    Creates:
        output_dir/extraction.txt   - Human-readable summary
        output_dir/content.txt      - Full content of non-boilerplate pages
        output_dir/boilerplate.txt  - Filtered pages (for debugging filtering logic)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Summary file
    summary_path = output_dir / "extraction.txt"
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write(f"Document: {extraction.document_title}\n")
        f.write(f"Source: {extraction.source_path}\n")
        f.write(f"Total pages: {extraction.total_pages}\n")
        f.write(f"Content pages: {len(extraction.content_pages)}\n")
        f.write(f"Boilerplate pages: {len(extraction.boilerplate_pages)}\n")
        f.write("\n")
        f.write("=" * 70 + "\n")
        f.write("BOILERPLATE FILTER SUMMARY\n")
        f.write("=" * 70 + "\n\n")

        for page in extraction.boilerplate_pages:
            f.write(f"Page {page.page_number}: {page.boilerplate_reason}\n")

        f.write("\n")
        f.write("=" * 70 + "\n")
        f.write("DETECTED HEADERS (preliminary)\n")
        f.write("=" * 70 + "\n\n")

        for page in extraction.content_pages:
            if page.detected_headers:
                f.write(f"Page {page.page_number}:\n")
                for header in page.detected_headers:
                    f.write(f"  - {header}\n")

    # Full content file (for next pipeline step)
    content_path = output_dir / "content.txt"
    with open(content_path, 'w', encoding='utf-8') as f:
        for page in extraction.content_pages:
            f.write(f"\n{'=' * 70}\n")
            f.write(f"PAGE {page.page_number}\n")
            if page.has_chapter_header:
                f.write("[CHAPTER START]\n")
            f.write(f"{'=' * 70}\n\n")
            f.write(page.text)
            f.write("\n")

    # Boilerplate file (for debugging)
    boilerplate_path = output_dir / "boilerplate.txt"
    with open(boilerplate_path, 'w', encoding='utf-8') as f:
        for page in extraction.boilerplate_pages:
            f.write(f"\n{'=' * 70}\n")
            f.write(f"PAGE {page.page_number} - FILTERED: {page.boilerplate_reason}\n")
            f.write(f"{'=' * 70}\n\n")
            f.write(page.text)
            f.write("\n")

    return summary_path
