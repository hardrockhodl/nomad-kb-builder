"""
vLLM HTTP client mirroring the ollama_client public surface.

Talks to an OpenAI-compatible vLLM server (default http://localhost:8000) over
``/v1/chat/completions``. The dataclass and function names match
``ollama_client`` deliberately so call sites can swap backends with zero edits
beyond ``pipeline.get_llm_client()``.

In addition to the sync ``call_ollama`` wrapper, this module exposes
``call_vllm_batch`` — an async fan-out used by the generator/verifier pipelines
to keep the GH200 saturated.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx


# ============================================================================
# Configuration
# ============================================================================

DEFAULT_MODEL = "Qwen3.6-27B"
DEFAULT_TIMEOUT = 300  # seconds per request
DEFAULT_CONCURRENCY = 32
MAX_RETRIES = 3
RETRY_DELAYS = [2, 5, 15]  # seconds between attempts


def _vllm_host() -> str:
    """Read VLLM_HOST at call time so test/CI overrides take effect."""
    return os.environ.get("VLLM_HOST", "http://localhost:8000").rstrip("/")


def _resolved_concurrency(default: int) -> int:
    """VLLM_CONCURRENCY env var overrides the call-site default."""
    raw = os.environ.get("VLLM_CONCURRENCY")
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _enable_thinking() -> bool:
    """VLLM_ENABLE_THINKING toggles Qwen3 thinking-mode emission.

    Default ``False``: Qwen3 emits a 200+ token ``<think>`` trace otherwise,
    which is wasted work for our JSON-only KB generation workload.
    """
    return os.environ.get("VLLM_ENABLE_THINKING", "false").lower() == "true"


DEFAULT_MAX_TOKENS = 4096


def _build_payload(
    system_prompt: str,
    user_prompt: str,
    model: str,
    temperature: float,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """Single source of truth for the /v1/chat/completions request body."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "top_p": 0.9,
        "max_tokens": max_tokens,
        # vLLM honors this via xgrammar/outlines and constrains output to valid
        # JSON. Critical: every prompt in this project asks for JSON.
        "response_format": {"type": "json_object"},
        # Qwen3 chat template flag. Coexists with response_format.
        "chat_template_kwargs": {"enable_thinking": _enable_thinking()},
    }


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class OllamaResponse:
    """Same shape as ollama_client.OllamaResponse to keep call sites identical."""
    success: bool
    raw_text: str
    error: Optional[str]
    duration_seconds: float
    retry_count: int


# ============================================================================
# Per-request async call
# ============================================================================

async def _call_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    system_prompt: str,
    user_prompt: str,
    model: str,
    temperature: float,
    timeout: int,
    max_tokens: int,
) -> OllamaResponse:
    payload = _build_payload(
        system_prompt, user_prompt, model, temperature, max_tokens,
    )
    url = f"{_vllm_host()}/v1/chat/completions"
    start = time.time()
    last_error: Optional[str] = None

    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                raw_text = (data["choices"][0]["message"].get("content") or "")

                if not raw_text.strip():
                    last_error = "Empty response from vLLM"
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAYS[attempt])
                        continue
                    break

                return OllamaResponse(
                    success=True,
                    raw_text=raw_text,
                    error=None,
                    duration_seconds=time.time() - start,
                    retry_count=attempt,
                )

            except httpx.TimeoutException:
                last_error = f"Timeout after {timeout}s"
            except httpx.ConnectError as e:
                last_error = f"Connection error: {e}"
            except httpx.HTTPStatusError as e:
                last_error = f"HTTP error: {e}"
            except (KeyError, IndexError, ValueError) as e:
                last_error = f"Malformed vLLM response: {type(e).__name__}: {e}"
            except Exception as e:
                last_error = f"Unexpected error: {type(e).__name__}: {e}"

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])

    return OllamaResponse(
        success=False,
        raw_text="",
        error=last_error,
        duration_seconds=time.time() - start,
        retry_count=MAX_RETRIES,
    )


# ============================================================================
# Batch entry point
# ============================================================================

async def call_vllm_batch(
    requests: list[tuple[str, str]],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,
    timeout: int = DEFAULT_TIMEOUT,
    concurrency: int = DEFAULT_CONCURRENCY,
    progress_callback: Optional[Callable[[int, OllamaResponse], None]] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[OllamaResponse]:
    """Send many ``(system_prompt, user_prompt)`` pairs in parallel.

    Returns a list of ``OllamaResponse`` in the same order as ``requests``.
    Optionally invokes ``progress_callback(index, response)`` as each request
    completes (out of order) so callers can drive a progress bar.

    ``max_tokens`` is the per-request output budget. Stages like verify that
    produce short JSON responses should pass a smaller value so the input
    side of the (input + output ≤ model_max_len) window has more headroom.
    """
    if not requests:
        return []

    concurrency = _resolved_concurrency(concurrency)
    sem = asyncio.Semaphore(concurrency)

    limits = httpx.Limits(
        max_keepalive_connections=concurrency,
        max_connections=concurrency * 2,
    )

    results: list[Optional[OllamaResponse]] = [None] * len(requests)

    async with httpx.AsyncClient(limits=limits) as client:

        async def run(idx: int, sys_p: str, usr_p: str) -> None:
            resp = await _call_one(
                client, sem, sys_p, usr_p, model, temperature, timeout, max_tokens,
            )
            results[idx] = resp
            if progress_callback is not None:
                progress_callback(idx, resp)

        await asyncio.gather(*[
            run(i, s, u) for i, (s, u) in enumerate(requests)
        ])

    # By construction every slot is filled.
    return [r for r in results if r is not None]


# ============================================================================
# Sync convenience wrapper — same signature as ollama_client.call_ollama
# ============================================================================

def call_ollama(
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,
    timeout: int = DEFAULT_TIMEOUT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> OllamaResponse:
    """Single-request convenience wrapper. Runs the async batch with one item."""
    results = asyncio.run(call_vllm_batch(
        [(system_prompt, user_prompt)],
        model=model,
        temperature=temperature,
        timeout=timeout,
        concurrency=1,
        max_tokens=max_tokens,
    ))
    return results[0]


# ============================================================================
# Availability check
# ============================================================================

def check_ollama_available(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    """Probe ``/v1/models`` and verify the requested model is served."""
    url = f"{_vllm_host()}/v1/models"
    try:
        r = httpx.get(url, timeout=5)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, f"vLLM not reachable at {_vllm_host()}: {e}"

    models = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    if model not in models:
        return False, (
            f"Model '{model}' not served by vLLM. "
            f"Available: {', '.join(models) or '(none)'}"
        )
    return True, ""
