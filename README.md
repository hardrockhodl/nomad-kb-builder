# nomad-kb-builder

AI-assisted pipeline that converts Cisco PDF documentation into Nomad-compatible
Knowledge Base markdown files.

## Status

Work in progress. Building incrementally:

- [x] Step 1: PDF text extraction
- [ ] Step 2: Section detection
- [ ] Step 3: LLM-based KB generation
- [ ] Step 4: LLM-based verification
- [ ] Step 5: Output organization

## Architecture

```
PDF
 ↓ Step 1: pymupdf extraction + boilerplate removal
Structured text per page
 ↓ Step 2: Section detection
List of sections: {chapter, section, page, content}
 ↓ Step 3: LLM generation (qwen3.6:35b-a3b-coding-nvfp4)
KB drafts or SKIP
 ↓ Step 4: LLM verification
PASS or FLAG
 ↓ Step 5: Write to output/
```

## Setup

```bash
git clone git@github.com:hardrockhodl/nomad-kb-builder.git
cd nomad-kb-builder
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

Place PDF in `input/`, then:

```bash
python kb_builder.py extract input/cisco-nexus-9000-series-nx-os-high-availability-and-redundancy-guide-105x.pdf
```

Output appears in `output/<document-name>/`.

## Requirements

- Python 3.11+
- Ollama running locally with `qwen3.6:35b-a3b-coding-nvfp4` (added in Step 3)
- macOS on Apple Silicon recommended for Ollama performance

## License

Private project. Not for distribution.
