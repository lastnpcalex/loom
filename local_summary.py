"""Local Gemma 3 1B summarizer — runs on CPU via llama-cpp-python.

Downloads the quantized GGUF model on first use (~500MB).
Auto-unloads after idle timeout to free memory.
Completely independent from Ollama — won't compete for GPU resources.
"""

import asyncio
import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Singleton model instance
_llm = None
_loading = False
_load_lock = None
_inference_lock = None  # prevents concurrent llama.cpp calls (not thread-safe)
_last_used = 0.0        # timestamp of last inference call
_idle_task = None        # background task that checks for idle timeout

# Model config — abliterated (no refusals) needed because this summarizes RP content
# Set LOCAL_SUMMARIZER_PATH to a .gguf file to skip HuggingFace entirely
LOCAL_MODEL_PATH = os.getenv("LOCAL_SUMMARIZER_PATH", "")
REPO_ID = "bartowski/huihui-ai_gemma-3-1b-it-abliterated-GGUF"
FILENAME = "gemma-3-1b-it-abliterated-Q4_K_M.gguf"
N_CTX = 2048       # 2K is plenty for short summaries
N_THREADS = None    # None = auto-detect
IDLE_TIMEOUT = 300  # unload after 5 minutes idle


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
    global _llm, _loading, _last_used, _idle_task

    _last_used = time.monotonic()

    if _llm is not None:
        return _llm

    async with _get_lock():
        # Double-check after acquiring lock
        if _llm is not None:
            return _llm

        _loading = True
        logger.info("Loading local Gemma 3 1B for summarization...")

        try:
            # Run the blocking download + load in a thread
            _llm = await asyncio.get_event_loop().run_in_executor(None, _load_model)
            logger.info("Gemma 3 1B loaded — CPU summarization ready")
        except Exception as e:
            logger.error(f"Failed to load Gemma model: {e}")
            _loading = False
            raise
        finally:
            _loading = False

        # Start idle monitor
        if _idle_task is None or _idle_task.done():
            _idle_task = asyncio.create_task(_idle_monitor())

        return _llm


async def _idle_monitor():
    """Periodically check if the model has been idle and unload it."""
    global _llm
    while True:
        await asyncio.sleep(60)  # check every minute
        if _llm is None:
            return  # already unloaded, stop monitoring
        elapsed = time.monotonic() - _last_used
        if elapsed >= IDLE_TIMEOUT:
            logger.info(f"Gemma idle for {int(elapsed)}s — unloading to free memory")
            unload()
            return


def _load_model():
    """Synchronous model download + initialization.

    Priority: LOCAL_SUMMARIZER_PATH env var → HF local cache → HF download (once).
    """
    from llama_cpp import Llama

    # 1) Explicit local path — no HF dependency at all
    if LOCAL_MODEL_PATH and os.path.isfile(LOCAL_MODEL_PATH):
        model_path = LOCAL_MODEL_PATH
        logger.info(f"Using local summarizer model: {model_path}")
    else:
        from huggingface_hub import hf_hub_download
        if LOCAL_MODEL_PATH:
            logger.warning(f"LOCAL_SUMMARIZER_PATH set but file not found: {LOCAL_MODEL_PATH}")
        # 2) Try HF local cache — no network request
        try:
            model_path = hf_hub_download(
                repo_id=REPO_ID, filename=FILENAME, local_files_only=True
            )
            logger.info("Summarizer model found in local HF cache")
        except Exception:
            # 3) First run — download once, then it's cached forever
            logger.info("Summarizer model not cached, downloading (one-time ~800MB)...")
            model_path = hf_hub_download(repo_id=REPO_ID, filename=FILENAME)
            logger.info(f"Summarizer model downloaded to {model_path}")
            logger.info("Tip: set LOCAL_SUMMARIZER_PATH=%s to bypass HF in the future", model_path)

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
        global _last_used
        # Serialize inference calls — llama.cpp is not thread-safe
        async with _get_inference_lock():
            _last_used = time.monotonic()
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
            _last_used = time.monotonic()
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
        global _last_used
        # Serialize inference calls — llama.cpp is not thread-safe
        async with _get_inference_lock():
            _last_used = time.monotonic()
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
            _last_used = time.monotonic()
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
        # Force garbage collection to actually free the memory
        import gc
        gc.collect()
        logger.info("Gemma model unloaded from memory")
