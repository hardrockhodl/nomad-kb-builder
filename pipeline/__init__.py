"""LLM backend selection.

Exposes a single function `get_llm_client()` that returns the configured
backend module. Both backends expose the same public surface
(`call_ollama`, `check_ollama_available`, `DEFAULT_MODEL`, `OllamaResponse`),
so call sites don't need to know which one is active. The vLLM backend
additionally exposes `call_vllm_batch` for true parallelism.
"""

from __future__ import annotations

import os


def get_llm_client():
    """Return the configured LLM client module.

    Selection via env var ``LLM_BACKEND`` (values: ``"ollama"`` | ``"vllm"``).
    Default: ``"ollama"`` to preserve existing behavior.
    """
    backend = os.environ.get("LLM_BACKEND", "ollama").lower()
    if backend == "vllm":
        from . import vllm_client
        return vllm_client
    if backend == "ollama":
        from . import ollama_client
        return ollama_client
    raise ValueError(f"Unknown LLM_BACKEND: {backend!r} (expected 'ollama' or 'vllm')")
