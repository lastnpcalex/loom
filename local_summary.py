"""Local Gemma 3 1B summarizer — runs on CPU via llama-cpp-python.

Downloads the quantized GGUF model on first use (~806MB).
Keeps the model loaded in memory as a singleton for fast repeated calls.
Completely independent from Ollama — won't compete for GPU resources.
"""

import asyncio
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Singleton model instance
_llm = None
_loading = False
_load_lock = None
_inference_lock = None  # prevents concurrent llama.cpp calls (not thread-safe)

# Model config — abliterated (no refusals) for RP summarization
REPO_ID = "mlabonne/gemma-3-1b-it-abliterated-GGUF"
FILENAME = "gemma-3-1b-it-abliterated.q8_0.gguf"
N_CTX = 4096
N_THREADS = None  # None = auto-detect


def _get_lock():
    """Lazy-create the asyncio lock (must be called inside a running loop)."""
    global _load_lock
    if _load_lock is None:
        _load_lock = asyncio.Lock()
    return _load_lock


def _get_inference_lock():
    """Lazy-create the inference lock to serialize llama.cpp calls."""
    global _inference_lock
    if _inference_lock is None:
        _inference_lock = asyncio.Lock()
    return _inference_lock


async def _ensure_model():
    """Download (if needed) and load the Gemma model. Thread-safe singleton."""
    global _llm, _loading

    if _llm is not None:
        return _llm

    async with _get_lock():
        # Double-check after acquiring lock
        if _llm is not None:
            return _llm

        _loading = True
        logger.info("Loading local Gemma 3 1B for summarization (first run downloads ~806MB)...")

        try:
            # Run the blocking download + load in a thread
            _llm = await asyncio.get_event_loop().run_in_executor(None, _load_model)
            logger.info("Gemma 3 1B loaded successfully — CPU summarization ready")
        except Exception as e:
            logger.error(f"Failed to load Gemma model: {e}")
            _loading = False
            raise
        finally:
            _loading = False

        return _llm


def _load_model():
    """Synchronous model download + initialization."""
    from huggingface_hub import hf_hub_download
    from llama_cpp import Llama

    # Download model (cached after first download)
    model_path = hf_hub_download(repo_id=REPO_ID, filename=FILENAME)

    # Determine thread count
    n_threads = N_THREADS
    if n_threads is None:
        n_threads = max(1, (os.cpu_count() or 4) // 2)  # use half of cores

    llm = Llama(
        model_path=model_path,
        n_ctx=N_CTX,
        n_threads=n_threads,
        n_gpu_layers=0,   # pure CPU — no GPU
        verbose=False,
    )
    return llm


async def summarize(text: str, max_tokens: int = 400, temperature: float = 0.3) -> str:
    """Summarize text using the local Gemma model.

    Args:
        text: The text to summarize.
        max_tokens: Maximum tokens in the summary response.
        temperature: Sampling temperature (low = more focused).

    Returns:
        Summary string.
    """
    try:
        llm = await _ensure_model()
    except Exception as e:
        logger.warning(f"Gemma unavailable, using fallback summary: {e}")
        return _fallback_summary(text)

    # Truncate input to fit Gemma's context window (4096 tokens ≈ 12K chars)
    # Reserve ~500 tokens for the instruction prefix + response
    max_input_chars = (N_CTX - max_tokens - 500) * 3
    if len(text) > max_input_chars:
        text = text[:max_input_chars] + "\n[...truncated...]"

    prompt = (
        "<start_of_turn>user\n"
        "Summarize these RP events. Be brief and factual. Present tense, third person. "
        "Cover: plot points, character actions, setting changes, unresolved threads.\n\n"
        f"{text}\n"
        "<end_of_turn>\n"
        "<start_of_turn>model\n"
    )

    try:
        # Serialize inference calls — llama.cpp is not thread-safe
        async with _get_inference_lock():
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: llm(
                    prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=0.9,
                    stop=["<end_of_turn>"],
                )
            )
        return result["choices"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Gemma inference failed: {e}")
        return _fallback_summary(text)


async def summarize_message(content: str, role: str = "user") -> str:
    """Generate a short 10-15 word summary of a single message for tree display.

    Args:
        content: The message text.
        role: 'user' or 'assistant'.

    Returns:
        Short summary string for the tree node.
    """
    # For very short messages, just return them directly
    if len(content.split()) <= 10:
        return content.strip().replace('\n', ' ')

    try:
        llm = await _ensure_model()
    except Exception as e:
        logger.warning(f"Gemma unavailable for message summary: {e}")
        return _short_fallback(content)

    # Truncate long messages to fit context
    max_chars = 2000
    text = content[:max_chars] if len(content) > max_chars else content

    prompt = (
        "<start_of_turn>user\n"
        "Summarize this RP message in under 8 words. Present tense. No preamble.\n\n"
        f"{text}\n"
        "<end_of_turn>\n"
        "<start_of_turn>model\n"
    )

    try:
        # Serialize inference calls — llama.cpp is not thread-safe
        async with _get_inference_lock():
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: llm(
                    prompt,
                    max_tokens=20,
                    temperature=0.2,
                    top_p=0.9,
                    stop=["<end_of_turn>", "\n\n"],
                )
            )
        summary = result["choices"][0]["text"].strip()
        # Clean up: remove quotes, trailing periods from fragments
        summary = summary.strip('"\'')
        return summary if summary else _short_fallback(content)
    except Exception as e:
        logger.error(f"Gemma message summary failed: {e}")
        return _short_fallback(content)


def _short_fallback(text: str) -> str:
    """Truncate to ~12 words as fallback."""
    words = text.replace('\n', ' ').replace('  ', ' ').strip().split()
    if len(words) <= 12:
        return ' '.join(words)
    return ' '.join(words[:12]) + '...'


def _fallback_summary(text: str) -> str:
    """Simple extractive fallback if Gemma fails."""
    lines = text.strip().split('\n')
    key_lines = [l.strip() for l in lines if l.strip() and len(l.strip()) > 20]
    if not key_lines:
        return "The conversation continues."
    # Take first and last few meaningful lines
    selected = key_lines[:3]
    if len(key_lines) > 6:
        selected += key_lines[-2:]
    return ' '.join(selected)[:800]


def is_loaded() -> bool:
    """Check if the model is currently loaded in memory."""
    return _llm is not None


def is_loading() -> bool:
    """Check if the model is currently being downloaded/loaded."""
    return _loading


async def preload():
    """Optionally call at startup to pre-download and load the model."""
    await _ensure_model()


def unload():
    """Free the model from memory."""
    global _llm
    if _llm is not None:
        del _llm
        _llm = None
        logger.info("Gemma model unloaded from memory")
