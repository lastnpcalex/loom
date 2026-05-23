"""Async vLLM client for streaming and sync chat completion via OpenAI-compat API.

Mirrors ollama_client.py's public surface so callers don't change. The dispatcher
in local_llm.py picks between ollama_client and vllm_client based on config.

Two key shape differences vs Ollama:
  * KV cache dtype is a vLLM launch flag (--kv-cache-dtype), not per-request.
  * Vision uses OpenAI content blocks ({type: image_url, image_url:{url:data:...}})
    instead of Ollama's {images: [b64]} sidecar field.

Dual-path image handling: messages within the verbatim window send pixels natively
as content blocks; older messages substitute the stored text description from
image_alt, so KV cache doesn't balloon as image history grows.
"""

import asyncio
import base64
import json
import mimetypes
import random
from typing import AsyncGenerator, Optional

import httpx

from config import config

_mock_mode = False

MOCK_RESPONSES = [
    "She studied the newcomer for a long moment. \"Interesting,\" she said, the word doing six different jobs at once.",
    "He set down the pen. \"You've got thirty seconds before the kettle boils. Use them wisely.\"",
    "The wind got under the door. She didn't turn. \"Ah. I was wondering when you'd show up.\"",
]


def _vllm_host() -> str:
    host = config.vllm_host
    if host and not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host


def _vllm_model() -> str:
    """Resolve the model id to send to vLLM in API requests.

    When `vllm_served_name` is configured, that's the ONLY id vLLM accepts
    (--served-model-name replaces the original; the HF id no longer routes).
    Stale `conv.local_model` values from before we set the alias would otherwise
    blow up with "Model not found"; this normalizer absorbs that.
    """
    if config.vllm_served_name:
        return config.vllm_served_name
    return config.vllm_model or config.ollama_model


def _resolve_vllm_model(requested: str | None) -> str:
    """Pick the right model id for an outgoing vLLM request. Pass through what
    the caller asked for if it's a known served name; otherwise fall back to
    the alias (handles legacy convs whose saved model is no longer served).

    With admin spawning vLLM under both the HF id AND the vllm-* alias, the
    same loaded model answers to either, so Weave/OODA can pick the
    descriptive name and Loom CC can stick with the alias."""
    return (requested or "").strip() or _vllm_model()


def _parse_image_paths(image_path) -> list[str]:
    if not image_path:
        return []
    if isinstance(image_path, list):
        return image_path
    try:
        parsed = json.loads(image_path)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return [image_path]


def _image_to_data_url(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (IOError, OSError):
        return None
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('utf-8')}"


def _image_alt_for(msg: dict, filename: str) -> Optional[str]:
    """Look up the stored text description for an image filename, if any."""
    alt = msg.get("image_alt")
    if not alt:
        return None
    if isinstance(alt, str):
        try:
            alt = json.loads(alt)
        except (json.JSONDecodeError, TypeError):
            return alt  # legacy: single string for single image
    if isinstance(alt, dict):
        return alt.get(filename)
    return None


def _build_vllm_messages(messages: list[dict], verbatim_window: Optional[int] = None) -> list[dict]:
    """Convert internal message format to OpenAI-compat content-block format.

    If verbatim_window is set, only the last N messages get native image blocks;
    older messages substitute the stored text description (image_alt) so the KV
    cache doesn't carry historical pixels.
    """
    cutoff_idx = -1
    if verbatim_window is not None and verbatim_window > 0:
        cutoff_idx = max(0, len(messages) - verbatim_window)

    out: list[dict] = []
    for i, msg in enumerate(messages):
        text = msg.get("content") or ""
        img_paths = _parse_image_paths(msg.get("image_path"))
        inline_images = msg.get("images") or []
        is_recent = (i >= cutoff_idx)

        if not img_paths and not inline_images:
            out.append({"role": msg["role"], "content": text})
            continue

        if is_recent:
            blocks: list[dict] = []
            if text:
                blocks.append({"type": "text", "text": text})
            for p in img_paths:
                url = _image_to_data_url(p)
                if url:
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
            for b64 in inline_images:
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })
            if not blocks:
                blocks = [{"type": "text", "text": text or ""}]
            out.append({"role": msg["role"], "content": blocks})
        else:
            # Older message: substitute text description for pixels.
            notes = []
            for p in img_paths:
                fname = p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                desc = _image_alt_for(msg, fname)
                notes.append(f"[Image: {desc}]" if desc else f"[Image: {fname}]")
            if inline_images and not notes:
                notes.append(f"[{len(inline_images)} image(s) attached]")
            joined = "\n".join(notes)
            combined = f"{text}\n{joined}".strip() if text else joined
            out.append({"role": msg["role"], "content": combined})
    return out


async def health_check() -> dict:
    global _mock_mode
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_vllm_host()}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            target = _vllm_model()
            available = any(target == m or target in m for m in models) if target else bool(models)
            _mock_mode = False
            return {
                "status": "ok",
                "models": models,
                "target_model": target,
                "model_available": available,
                "mock_mode": False,
                "backend": "vllm",
            }
    except Exception as e:
        _mock_mode = True
        return {
            "status": "mock",
            "error": str(e),
            "mock_mode": True,
            "backend": "vllm",
            "message": f"vLLM not reachable at {_vllm_host()} — running in mock mode",
        }


async def _mock_stream(messages: list[dict]) -> AsyncGenerator[str, None]:
    response = random.choice(MOCK_RESPONSES)
    for i, word in enumerate(response.split(" ")):
        yield ("" if i == 0 else " ") + word
        await asyncio.sleep(0.02 + random.random() * 0.03)


async def stream_chat(messages: list[dict],
                      temperature: float = None,
                      top_p: float = None,
                      max_tokens: int = None,
                      repeat_penalty: float = None,
                      model: str = None,
                      verbatim_window: int = None) -> AsyncGenerator:
    """Stream chat completion tokens from vLLM (or mock).

    Yields:
      str token chunks for content,
      {"type": "thinking_start"} / {"type": "thinking_delta", "text": ...} / {"type": "thinking_end"} for reasoning_content,
      {"type": "usage", "input_tokens": N, "output_tokens": N} as the final event.
    """
    global _mock_mode
    if _mock_mode:
        print("[VLLM] WARNING: running in MOCK MODE")
        async for tok in _mock_stream(messages):
            yield tok
        return

    target_model = _resolve_vllm_model(model)
    print(f"[VLLM] Sending {len(messages)} messages to {target_model}")

    effective_max = max_tokens or config.max_tokens
    win = verbatim_window if verbatim_window is not None else config.verbatim_window

    # Pad for thinking budget so the model has room to reason AND produce content,
    # but clamp to ~90% of max_model_len — vLLM rejects max_tokens > max_model_len,
    # and we still need headroom for the prompt itself.
    max_len = getattr(config, "vllm_max_model_len", 0) or 32768
    padded_max = min(effective_max + 8192, max(effective_max, int(max_len * 0.9)))

    payload = {
        "model": target_model,
        "messages": _build_vllm_messages(messages, verbatim_window=win),
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": temperature if temperature is not None else config.temperature,
        "top_p": top_p if top_p is not None else config.top_p,
        # vLLM enforces this absolutely; we keep our own thinking-aware cap below.
        "max_tokens": padded_max,
        # vLLM-specific extension passed through on the OpenAI request body.
        "repetition_penalty": repeat_penalty if repeat_penalty is not None else config.repeat_penalty,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            async with client.stream(
                "POST",
                f"{_vllm_host()}/v1/chat/completions",
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    try:
                        err = json.loads(body).get("error", body.decode())
                    except Exception:
                        err = f"HTTP {response.status_code}"
                    raise RuntimeError(f"vLLM error: {err}")

                _was_thinking = False
                _content_tokens = 0
                _input_tokens = 0
                _output_tokens = 0

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue  # SSE comment
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(f"vLLM error: {chunk['error']}")

                    usage = chunk.get("usage")
                    if usage:
                        _input_tokens = usage.get("prompt_tokens", _input_tokens)
                        _output_tokens = usage.get("completion_tokens", _output_tokens)

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # vLLM streaming emits the reasoning delta under "reasoning"
                    # (0.20+) — the non-streaming response uses "reasoning_content".
                    # Accept either so we don't silently drop thinking tokens.
                    reasoning = (delta.get("reasoning")
                                 or delta.get("reasoning_content")
                                 or "")
                    token = delta.get("content") or ""

                    if reasoning:
                        if not _was_thinking:
                            _was_thinking = True
                            yield {"type": "thinking_start"}
                        yield {"type": "thinking_delta", "text": reasoning}
                    
                    if token:
                        if _was_thinking:
                            _was_thinking = False
                            yield {"type": "thinking_end"}
                        _content_tokens += 1
                        yield token
                        if _content_tokens >= effective_max:
                            break

                yield {
                    "type": "usage",
                    "input_tokens": _input_tokens,
                    "output_tokens": _output_tokens or _content_tokens,
                }
                return
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as e:
        raise RuntimeError(f"Cannot reach vLLM at {_vllm_host()}: {e}")


async def sync_chat(messages: list[dict],
                    temperature: float = None,
                    max_tokens: int = None,
                    model: str = None,
                    think: bool = None) -> str:
    """Non-streaming chat completion (summarization, OODA passes, etc.)."""
    global _mock_mode
    if _mock_mode:
        return "Summary: The conversation continues with escalating tension and mutual wariness."

    target_model = _resolve_vllm_model(model)
    payload = {
        "model": target_model,
        "messages": _build_vllm_messages(messages, verbatim_window=config.verbatim_window),
        "stream": False,
        "temperature": temperature if temperature is not None else config.temperature,
        "max_tokens": max_tokens or config.max_tokens,
    }
    # vLLM exposes reasoning toggling via chat_template_kwargs for models that
    # support it (Qwen3 thinking, DeepSeek-R1, etc.). Honored when model supports it.
    if think is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(think)}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            resp = await client.post(f"{_vllm_host()}/v1/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            return msg.get("content") or msg.get("reasoning_content") or ""
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError):
        _mock_mode = True
        return "Summary: The conversation continues with escalating tension and mutual wariness."


async def describe_image(image_path: str, model: str = None, context: str = None) -> str:
    """Describe an image via vLLM's native vision (single model, no fallback chain)."""
    global _mock_mode
    if _mock_mode:
        return "An image was shared."

    url = _image_to_data_url(image_path)
    if not url:
        return "An image was shared but could not be read."

    prompt = (
        "Describe this image in thorough detail. Include: subjects and their appearance "
        "(clothing, expression, physical features), their physical pose and body language "
        "(how they are positioned, what their limbs are doing, spatial arrangement relative "
        "to each other and the environment), setting and environment, lighting and mood, "
        "composition and framing, any text or symbols visible, and notable artistic or "
        "photographic qualities. Describe what you observe objectively and completely without "
        "editorializing or omitting details. No preamble."
    )
    if context:
        prompt += f"\n\nAdditional focus: {context}"

    payload = {
        "model": _resolve_vllm_model(model or config.vision_model),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 800,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            resp = await client.post(f"{_vllm_host()}/v1/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return "An image was shared."
            msg = choices[0].get("message") or {}
            return msg.get("content") or msg.get("reasoning_content") or "An image was shared."
    except Exception as e:
        print(f"[VLLM-DESCRIBE] {type(e).__name__}: {str(e)[:200]}")
        return "An image was shared."


async def describe_image_with_data(image_path: str, model: str = None, context: str = None) -> tuple[str, dict]:
    """Returns (description, image_payload). image_payload uses the legacy
    {'images': [b64]} shape so the existing Ollama-shaped consumers still work."""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except (IOError, OSError):
        return ("An image was shared but could not be read.", {})
    desc = await describe_image(image_path, model=model, context=context)
    return (desc, {"images": [b64]})
