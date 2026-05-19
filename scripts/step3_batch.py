"""
step3_batch.py

Generate KB drafts from extracted sections using vLLM batch inference.

Input:  output/<doc>/sections.json  (list of {chapter, section, page, content})
Output: output/<doc>/kb_drafts.jsonl (one KB draft per line, or SKIP)

Designed for vLLM server running locally on http://localhost:8000.
Sends many requests concurrently to utilize GH200 + Qwen3.6-27B fully.

Usage:
    python step3_batch.py output/cisco-nexus-9000-.../sections.json
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

console = Console()

VLLM_URL = "http://localhost:8000/v1/chat/completions"
MODEL = "Qwen3.6-27B"
CONCURRENCY = 32        # number of requests in flight at once
TIMEOUT = 120           # seconds per request

SYSTEM_PROMPT = """You are a senior network engineer who writes concise, accurate knowledge base entries about Cisco NX-OS, IOS, and IOS-XE features.

Your task: convert one section of Cisco documentation into a Nomad-compatible KB entry.

Output format (strict JSON, no markdown fences):
{
  "title": "<short title, max 80 chars>",
  "summary": "<2-3 sentence overview of what this section covers>",
  "key_commands": ["<command 1>", "<command 2>", ...],
  "use_cases": ["<bullet 1>", "<bullet 2>", ...],
  "caveats": ["<important warning or caveat>", ...],
  "body": "<full KB content in markdown, 200-800 words>"
}

If the section is purely table-of-contents, index, or copyright boilerplate, respond with exactly:
{"skip": true, "reason": "<one short reason>"}

Important:
- Preserve exact command syntax from the source
- Do not hallucinate features or commands not present in the source
- Write for an engineer who will use this KB during a customer engagement
- Use American English"""


def build_user_prompt(section: dict) -> str:
    return f"""Source document section:

Chapter: {section.get('chapter', 'N/A')}
Section: {section.get('section', 'N/A')}
Page: {section.get('page', 'N/A')}

Content:
---
{section.get('content', '')}
---

Generate the KB entry as strict JSON now."""


async def process_section(
    client: httpx.AsyncClient,
    section: dict,
    idx: int,
    sem: asyncio.Semaphore,
) -> dict:
    """Send one section to vLLM and return result with metadata."""
    async with sem:
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(section)},
            ],
            "max_tokens": 2048,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        try:
            start = time.time()
            r = await client.post(VLLM_URL, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return {
                "idx": idx,
                "section_meta": {
                    "chapter": section.get("chapter"),
                    "section": section.get("section"),
                    "page": section.get("page"),
                },
                "kb_draft": json.loads(content),
                "tokens": usage,
                "latency_s": round(time.time() - start, 2),
                "status": "ok",
            }
        except Exception as e:
            return {
                "idx": idx,
                "section_meta": {
                    "chapter": section.get("chapter"),
                    "section": section.get("section"),
                    "page": section.get("page"),
                },
                "error": str(e),
                "status": "error",
            }


async def run(sections_path: Path):
    output_path = sections_path.parent / "kb_drafts.jsonl"
    error_path = sections_path.parent / "kb_errors.jsonl"

    sections = json.loads(sections_path.read_text())
    console.print(f"[cyan]Loaded {len(sections)} sections from {sections_path}[/cyan]")
    console.print(f"[cyan]Output: {output_path}[/cyan]")
    console.print(f"[cyan]Concurrency: {CONCURRENCY}[/cyan]")
    console.print()

    sem = asyncio.Semaphore(CONCURRENCY)
    results = []
    total_tokens = 0
    skipped = 0
    errors = 0
    start_time = time.time()

    async with httpx.AsyncClient() as client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Generating KB drafts", total=len(sections))

            coros = [
                process_section(client, s, i, sem)
                for i, s in enumerate(sections)
            ]

            with output_path.open("w") as out_f, error_path.open("w") as err_f:
                for fut in asyncio.as_completed(coros):
                    result = await fut

                    if result["status"] == "error":
                        errors += 1
                        err_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                    else:
                        kb = result["kb_draft"]
                        if kb.get("skip"):
                            skipped += 1
                        out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                        if "tokens" in result:
                            total_tokens += result["tokens"].get("total_tokens", 0)

                    results.append(result)
                    progress.update(task, advance=1)

    elapsed = time.time() - start_time
    console.print()
    console.print(f"[green]Done.[/green]")
    console.print(f"  Total sections:  {len(sections)}")
    console.print(f"  Successful:      {len(sections) - errors}")
    console.print(f"  Skipped:         {skipped}")
    console.print(f"  Errors:          {errors}")
    console.print(f"  Total tokens:    {total_tokens:,}")
    console.print(f"  Elapsed:         {elapsed:.1f}s")
    console.print(f"  Throughput:      {len(sections)/elapsed:.1f} sections/s")
    console.print(f"  Token rate:      {total_tokens/elapsed:.0f} tok/s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sections_json", type=Path, help="Path to sections.json from Step 2")
    args = ap.parse_args()

    if not args.sections_json.exists():
        sys.exit(f"Error: {args.sections_json} not found")

    asyncio.run(run(args.sections_json))


if __name__ == "__main__":
    main()
