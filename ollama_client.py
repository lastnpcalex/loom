"""Async Ollama client for streaming and sync chat completion.
Falls back to mock mode when Ollama is unreachable."""

import asyncio
import httpx
import json
import random
import base64
from typing import AsyncGenerator, Optional
from config import config

# Track whether we're in mock mode
_mock_mode = False

# Serializes Ollama API calls so describe_image doesn't collide with
# CC's own Ollama calls (Braid mode shares one Ollama instance).
_ollama_lock = asyncio.Lock()

# Keep models resident in VRAM between turns so llama.cpp's prefix cache
# hits on the shared prompt prefix. Ollama accepts durations ("30m") or
# -1 for indefinite; 0 unloads immediately.
# Per-request keep_alive intentionally omitted from chat/vision payloads —
# Ollama uses the OLLAMA_KEEP_ALIVE env var. Model eviction on swap is
# handled explicitly by _prepare_model -> _unload_model below.

# Tracks the model Ollama is currently holding resident so we can unload
# it before loading a different one (avoids VRAM thrash when /describe
# fires a vision model mid-chat).
_last_loaded_model: Optional[str] = None


async def _unload_model(model: str) -> None:
    """Tell Ollama to drop the given model from memory (keep_alive=0)."""
    if not model:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{_ollama_host()}/api/generate",
                json={"model": model, "keep_alive": 0},
            )
    except Exception as e:
        print(f"[OLLAMA] unload {model!r} failed: {e}")


async def _prepare_model(model: str) -> None:
    """If a different model is currently resident, unload it first."""
    global _last_loaded_model
    if _last_loaded_model and _last_loaded_model != model:
        print(f"[OLLAMA] switching {_last_loaded_model!r} -> {model!r}, unloading previous")
        await _unload_model(_last_loaded_model)
    _last_loaded_model = model


def _ollama_host():
    """Return the Ollama host URL with protocol prefix."""
    host = config.ollama_host
    if host and not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host


async def health_check() -> dict:
    """Check if Ollama is reachable and the model is available."""
    global _mock_mode
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_ollama_host()}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            model_available = any(
                config.ollama_model in m or m.startswith(config.ollama_model.split(":")[0])
                for m in models
            )
            _mock_mode = False
            return {
                "status": "ok",
                "models": models,
                "target_model": config.ollama_model,
                "model_available": model_available,
                "mock_mode": False,
            }
    except Exception as e:
        _mock_mode = True
        return {
            "status": "mock",
            "error": str(e),
            "mock_mode": True,
            "message": "Ollama not reachable — running in mock mode with sample responses",
        }


def _parse_image_paths(image_path) -> list[str]:
    """Parse image_path: handles single string, JSON array string, or list."""
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


def _build_ollama_messages(messages: list[dict]) -> list[dict]:
    """Convert internal message format to Ollama API format."""
    ollama_msgs = []
    for msg in messages:
        entry = {"role": msg["role"], "content": msg["content"]}
        if msg.get("image_path"):
            images = []
            for p in _parse_image_paths(msg["image_path"]):
                try:
                    with open(p, "rb") as f:
                        images.append(base64.b64encode(f.read()).decode("utf-8"))
                except (IOError, OSError):
                    pass
            if images:
                entry["images"] = images
        elif msg.get("images"):
            entry["images"] = msg["images"]
        ollama_msgs.append(entry)
    return ollama_msgs


# ── Mock responses ──

MOCK_RESPONSES = [
    '''She leaned back against the doorframe, arms crossed, studying the newcomer with an expression that couldn't decide between amusement and suspicion. The lantern behind her threw her shadow long across the floorboards.

"Interesting," she said, and the word carried about six different meanings, none of them straightforward. Her fingers drummed once against her elbow — a habit she'd never bothered to break. "Most people knock first. Or at least hesitate. You just walked in like you owned the place."

A pause. Somewhere outside, rain found a tin gutter and made music of it.

"I don't hate confidence. But I've learned to watch it carefully."''',

    '''The room smelled like old paper and cold coffee — the particular combination that meant someone had been working too long on something that mattered too much. He set down the pen he'd been holding like a weapon and looked up.

His eyes did that thing they did: catalogued, assessed, filed away. Shoes, posture, the way the newcomer's weight shifted slightly left. Everyone carried their story in their body if you knew how to read it.

"You've got about thirty seconds of my attention before the kettle boils," he said. "I'd use them wisely."''',

    '''The wind picked up outside — not dramatically, not the kind of wind that announced storms, but the quiet persistent kind that got under doors and reminded you that the world outside was still happening regardless of whatever was going on in here.

She caught the change in the air before she turned. A shift in pressure, or maybe just instinct.

"Ah." The single syllable contained a novel's worth of recognition. "I was wondering when you'd show up." She pushed a second glass across the bar without being asked. "Sit. You look like someone who needs to sit before they say whatever they're about to say."''',

    '''*The mechanism clicked — three tumblers, each finding their groove with the kind of precision that only came from craftsmanship or desperation. In this case, both.*

He withdrew his hand slowly, listening. The corridor stretched ahead in alternating pools of light and shadow, each lamp casting its circle like a small territorial claim against the dark.

"Well," he murmured to nobody, because talking to nobody was preferable to the alternative of thinking too loudly. "That's either very good or very bad."

He stepped forward. The floor had opinions about it.''',

    '''There was a quality to the silence that followed — not empty, but full. The kind of silence that happened when two people were both choosing their next words with unusual care.

She traced the rim of her cup with one finger. A nervous gesture from anyone else; from her, it was a way of measuring time.

"You know what the problem with the truth is?" She didn't wait for an answer. "It never sounds as convincing as a good lie. People expect the truth to feel *true* — solid, obvious, satisfying. But it usually just sounds... ordinary."

She met the other's eyes. "So when I tell you what actually happened, you're going to be disappointed. Fair warning."''',
]


async def _mock_stream(messages: list[dict]) -> AsyncGenerator[str, None]:
    """Generate a mock streaming response for testing."""
    response = random.choice(MOCK_RESPONSES)

    # Stream word by word with small delays to simulate real streaming
    words = response.split(' ')
    for i, word in enumerate(words):
        prefix = '' if i == 0 else ' '
        yield prefix + word
        await asyncio.sleep(0.02 + random.random() * 0.03)


async def stream_chat(messages: list[dict],
                      temperature: float = None,
                      top_p: float = None,
                      max_tokens: int = None,
                      repeat_penalty: float = None,
                      model: str = None) -> AsyncGenerator[str, None]:
    """Stream chat completion tokens from Ollama (or mock)."""
    global _mock_mode

    # Try real Ollama first, fall back to mock
    if _mock_mode:
        print(f"[OLLAMA] WARNING: Running in MOCK MODE — not sending to Ollama!")
        async for token in _mock_stream(messages):
            yield token
        return

    target_model = model or config.ollama_model
    print(f"[OLLAMA] Sending {len(messages)} messages to {target_model}")

    await _prepare_model(target_model)

    try:
        effective_max = max_tokens or config.max_tokens
        # Set num_predict high — we enforce the response token limit ourselves
        # so thinking tokens don't count against the response budget.
        num_predict = effective_max + 8192

        payload = {
            "model": target_model,
            "messages": _build_ollama_messages(messages),
            "stream": True,
            "options": {
                "kv_cache_type": "q8_0",
                "temperature": temperature or config.temperature,
                "top_p": top_p or config.top_p,
                "num_predict": num_predict,
                "repeat_penalty": repeat_penalty or config.repeat_penalty,
            },
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            async with client.stream("POST", f"{_ollama_host()}/api/chat",
                                     json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    try:
                        err = json.loads(body).get("error", body.decode())
                    except Exception:
                        err = f"HTTP {response.status_code}"
                    raise RuntimeError(f"Ollama error: {err}")
                _was_thinking = False
                _content_tokens = 0
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise RuntimeError(f"Ollama error: {chunk['error']}")
                        if chunk.get("done"):
                            yield {
                                "type": "usage",
                                "input_tokens": chunk.get("prompt_eval_count", 0),
                                "output_tokens": chunk.get("eval_count", _content_tokens),
                            }
                            return
                        msg = chunk.get("message", {})
                        thinking = msg.get("thinking", "")
                        token = msg.get("content", "")
                        if thinking and not _was_thinking:
                            _was_thinking = True
                            yield {"type": "thinking_start"}
                        if token and _was_thinking:
                            _was_thinking = False
                            yield {"type": "thinking_end"}
                        if token:
                            _content_tokens += 1
                            yield token
                            # Enforce response token limit (thinking doesn't count)
                            if _content_tokens >= effective_max:
                                return
                    except json.JSONDecodeError:
                        continue
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as e:
        raise RuntimeError(f"Cannot reach Ollama at {_ollama_host()}: {e}")


async def sync_chat(messages: list[dict],
                    temperature: float = None,
                    max_tokens: int = None,
                    model: str = None,
                    think: bool = None) -> str:
    """Non-streaming chat completion (for summarization, OODA passes, etc.)."""
    global _mock_mode

    if _mock_mode:
        return "Summary: The conversation continues with escalating tension and mutual wariness."

    target_model = model or config.ollama_model
    await _prepare_model(target_model)

    try:
        payload = {
            "model": target_model,
            "messages": _build_ollama_messages(messages),
            "stream": False,
            "options": {
                "kv_cache_type": "q8_0",
                "temperature": temperature or config.temperature,
                "num_predict": max_tokens or config.max_tokens,
            },
        }
        if think is not None:
            payload["think"] = think

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            resp = await client.post(f"{_ollama_host()}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            msg = data.get("message", {})
            # Workaround: some models route output to thinking field
            return msg.get("content") or msg.get("thinking") or ""
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError):
        _mock_mode = True
        return "Summary: The conversation continues with escalating tension and mutual wariness."


async def describe_image(image_path: str, model: str = None, context: str = None) -> str:
    """Use a multimodal Ollama model to describe an image in detail."""
    global _mock_mode

    if _mock_mode:
        return "An image was shared."

    try:
        with open(image_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
    except (IOError, OSError):
        return "An image was shared but could not be read."

    try:
        _describe_prompt = "Describe this image in thorough detail. Include: subjects and their appearance (clothing, expression, physical features), their physical pose and body language (how they are positioned, what their limbs are doing, spatial arrangement relative to each other and the environment), setting and environment, lighting and mood, composition and framing, any text or symbols visible, and notable artistic or photographic qualities. Describe what you observe objectively and completely without editorializing or omitting details. No preamble."
        if context:
            _describe_prompt += f"\n\nAdditional focus: {context}"
        payload = {
            "model": model or config.ollama_model,
            "messages": [
                {
                    "role": "user",
                    "content": _describe_prompt,
                    "images": [img_data],
                }
            ],
            "stream": False,
            "think": False,  # Disable thinking to avoid vision output routing bug
            "options": {
                "kv_cache_type": "q8_0",
                "temperature": 0.3,
                "num_predict": 800,
            },
        }

        # Try the configured model first, then fall back to defaults if it crashes.
        # Some models 500 on certain content with long prompts — fallback gives us
        # a fighting chance instead of returning useless "An image was shared".
        _candidates = []
        _primary = model or config.vision_model or config.ollama_model
        _candidates.append(_primary)
        for _fb in (config.ollama_model, "llama3.2-vision:latest"):
            if _fb and _fb not in _candidates:
                _candidates.append(_fb)

        async with _ollama_lock:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                last_err = None
                for _used_model in _candidates:
                    payload["model"] = _used_model
                    await _prepare_model(_used_model)
                    print(f"[DESCRIBE] Trying model={_used_model!r}")
                    try:
                        resp = await client.post(f"{_ollama_host()}/api/chat", json=payload)
                        resp.raise_for_status()
                        data = resp.json()
                        msg = data.get("message", {})
                        result = msg.get("content") or msg.get("thinking") or ""
                        if result:
                            print(f"[DESCRIBE] Got {len(result)} chars from {_used_model!r}")
                            return result
                        print(f"[DESCRIBE] Empty response from {_used_model!r}, trying fallback")
                    except Exception as e:
                        last_err = e
                        print(f"[DESCRIBE] {_used_model!r} failed: {type(e).__name__}: {str(e)[:200]}")
                        continue
                if last_err:
                    print(f"[DESCRIBE] All models failed, last error: {last_err}")
                return "An image was shared."
    except Exception as e:
        print(f"[DESCRIBE] Outer exception: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        return "An image was shared."


async def describe_image_with_data(image_path: str, model: str = None, context: str = None) -> tuple[str, dict]:
    """Use a multimodal Ollama model to describe an image AND return the image data for direct passing.

    Returns:
        Tuple of (description_text, image_payload) where image_payload is a dict with base64 image data
        that can be used in Ollama's chat API.
    """
    global _mock_mode

    if _mock_mode:
        return ("An image was shared.", {})

    try:
        with open(image_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
    except (IOError, OSError):
        return ("An image was shared but could not be read.", {})

    # Describe the image
    try:
        _describe_prompt = "Describe this image in thorough detail. Include: subjects and their appearance (clothing, expression, physical features), their physical pose and body language (how they are positioned, what their limbs are doing, spatial arrangement relative to each other and the environment), setting and environment, lighting and mood, composition and framing, any text or symbols visible, and notable artistic or photographic qualities. Describe what you observe objectively and completely without editorializing or omitting details. No preamble."
        if context:
            _describe_prompt += f"\n\nAdditional focus: {context}"
        target_model = model or config.ollama_model
        await _prepare_model(target_model)
        describe_payload = {
            "model": target_model,
            "messages": [
                {
                    "role": "user",
                    "content": _describe_prompt,
                    "images": [img_data],
                }
            ],
            "stream": False,
            "think": False,
            "options": {
                "kv_cache_type": "q8_0",
                "temperature": 0.3,
                "num_predict": 800,
            },
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            resp = await client.post(f"{_ollama_host()}/api/chat", json=describe_payload)
            resp.raise_for_status()
            data = resp.json()
            msg = data.get("message", {})
            description = msg.get("content") or msg.get("thinking") or "An image was shared."
    except Exception:
        description = "An image was shared."

    # Return both description and the image payload for direct passing
    return (description, {"images": [img_data]})
