"""
Minimal Ollama HTTP client with retry logic.

Talks directly to Ollama's /api/generate endpoint. No abstractions over the
HTTP layer — we want full control over prompts and timing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests


# ============================================================================
# Configuration
# ============================================================================

OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.6:35b-a3b-nvfp4"
DEFAULT_TIMEOUT = 300  # 5 minutes per call (sections can be large)
MAX_RETRIES = 3
RETRY_DELAYS = [2, 5, 15]  # seconds between retries


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class OllamaResponse:
    success: bool
    raw_text: str
    error: Optional[str]
    duration_seconds: float
    retry_count: int


# ============================================================================
# Core client
# ============================================================================

def call_ollama(
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,
    timeout: int = DEFAULT_TIMEOUT,
) -> OllamaResponse:
    """
    Call Ollama with system + user prompt. Returns response or error.

    Uses /api/generate (not /api/chat) for simpler streaming-free interaction.
    Constructs a combined prompt with ChatML-style markers that qwen models
    parse natively.
    """
    combined_prompt = (
        f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
        f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )

    payload = {
        "model": model,
        "prompt": combined_prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "num_ctx": 8192,
        },
    }

    start = time.time()
    last_error: Optional[str] = None

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                f"{OLLAMA_HOST}/api/generate",
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()

            data = response.json()
            raw_text = data.get("response", "")

            if not raw_text.strip():
                last_error = "Empty response from Ollama"
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                break

            return OllamaResponse(
                success=True,
                raw_text=raw_text,
                error=None,
                duration_seconds=time.time() - start,
                retry_count=attempt,
            )

        except requests.exceptions.Timeout:
            last_error = f"Timeout after {timeout}s"
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {e}"
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP error: {e}"
        except Exception as e:
            last_error = f"Unexpected error: {type(e).__name__}: {e}"

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAYS[attempt])

    return OllamaResponse(
        success=False,
        raw_text="",
        error=last_error,
        duration_seconds=time.time() - start,
        retry_count=MAX_RETRIES,
    )


def check_ollama_available(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    """
    Verify Ollama is running and the requested model is available.

    Returns (available, error_message).
    """
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        if model not in models:
            return False, f"Model '{model}' not found. Available: {', '.join(models)}"
        return True, ""
    except Exception as e:
        return False, f"Ollama not reachable: {e}"
