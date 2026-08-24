"""Async Llama Server client for streaming and sync chat completion.

Talks to llama-server.exe via OpenAI-compatible /v1/chat/completions endpoint.
Falls back to mock mode when the server is unreachable.

The server is launched externally (via admin_server.py or manually) using:
  llama-server.exe -m <model.gguf> --port 11434 --ctx-size 150000 --parallel 1 -ngl 999 --flash-attn on

list_local_models() scans config.llama_models_dir for *.gguf files so the
UI can populate a model picker without querying the running server.
"""

import asyncio
import base64
import json
import mimetypes
import os
import random
import re
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx

from config import config
import openrouter_client

# ── State ──────────────────────────────────────────────────────────────────

_mock_mode = False

# Serializes vision calls so parallel describe_image tasks don't collide.
_llama_lock = asyncio.Lock()

# Persistent httpx client, reused across sync_chat / stream_chat calls.
# Creating a fresh AsyncClient per request costs ~265ms of TCP+TLS setup on
# every call (measured 2026-07-07: fresh=510ms vs reused=245ms per dream
# request). Weave does an OODA pass + a repair pass per turn, so the per-call
# overhead compounded to ~530ms/turn — roughly the "1s slower" the user felt
# after the dream server swap. A shared client pools connections and skips that
# setup. Lazily created on first use (no event loop yet at import time); the
# per-request `timeout=` passed to .post()/.stream() still overrides per-call.
_shared_client: httpx.AsyncClient | None = None

_DREAM_THOUGHT_BLOCK_RE = re.compile(
    r"<\|channel>thought\s*(.*?)<channel\|>",
    re.IGNORECASE | re.DOTALL,
)
_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"


def _split_channel_scaffold(text: str) -> tuple[str, str]:
    if "<|channel>" not in (text or "") and "<channel|>" not in (text or ""):
        return "", text or ""
    normalized = (text or "").replace("\r\n", "\n")
    thoughts: list[str] = []
    content_parts: list[str] = []
    cursor = 0
    matched = False
    for match in _DREAM_THOUGHT_BLOCK_RE.finditer(normalized):
        matched = True
        before = normalized[cursor:match.start()]
        if before.strip():
            content_parts.append(before)
        thought = match.group(1).strip()
        if thought:
            thoughts.append(thought)
        cursor = match.end()
    if matched:
        after = normalized[cursor:]
        if after.strip():
            content_parts.append(after)
        return "\n\n".join(thoughts), "".join(content_parts)
    if normalized.strip() in {"<|channel>", "<channel|>", "thought"}:
        return "", ""
    return "", normalized


def _trailing_tag_prefix_len(text: str, tag: str) -> int:
    """Length of a trailing fragment that may become `tag` next chunk."""
    max_len = min(len(text), len(tag) - 1)
    for size in range(max_len, 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


def _consume_think_markup(
    buffer: str, in_think: bool, *, final: bool = False
) -> tuple[list[tuple[str, str]], str, bool]:
    """Split streamed `<think>...</think>` markup into visible/thinking events.

    llama.cpp-compatible templates sometimes place reasoning in the normal
    `content` delta wrapped in tags instead of using `reasoning_content`.
    Streaming can split tags across chunks, so keep incomplete tag fragments in
    `buffer` until the next delta.
    """
    events: list[tuple[str, str]] = []
    pending = buffer or ""
    while pending:
        if in_think:
            close_idx = pending.find(_THINK_CLOSE_TAG)
            if close_idx >= 0:
                thought = pending[:close_idx]
                if thought:
                    events.append(("thinking", thought))
                pending = pending[close_idx + len(_THINK_CLOSE_TAG):]
                in_think = False
                continue
            if final:
                if pending:
                    events.append(("thinking", pending))
                pending = ""
                break
            keep = _trailing_tag_prefix_len(pending, _THINK_CLOSE_TAG)
            emit = pending[:-keep] if keep else pending
            if emit:
                events.append(("thinking", emit))
            pending = pending[-keep:] if keep else ""
            break

        open_idx = pending.find(_THINK_OPEN_TAG)
        if open_idx >= 0:
            visible = pending[:open_idx]
            if visible:
                events.append(("visible", visible))
            pending = pending[open_idx + len(_THINK_OPEN_TAG):]
            in_think = True
            continue
        if final:
            if pending:
                events.append(("visible", pending))
            pending = ""
            break
        keep = _trailing_tag_prefix_len(pending, _THINK_OPEN_TAG)
        emit = pending[:-keep] if keep else pending
        if emit:
            events.append(("visible", emit))
        pending = pending[-keep:] if keep else ""
        break
    return events, pending, in_think


def _client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first use.

    Kept open for the process lifetime — httpx pools connections internally and
    handles server-side keep-alive close gracefully. The connect timeout is set
    on the client (10s); read/write timeouts are set per-request at the call
    site, since they vary (dream cold-loads need 600s, normal turns <30s)."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120.0),
            timeout=httpx.Timeout(None, connect=10.0),
        )
    return _shared_client

# Mapping from disk .gguf filename → server-registered model ID.
# Populated by health_check() when the server is reachable.
_model_name_map: dict[str, str] = {}


def _model_key(name: str) -> str:
    """Canonical key for matching llama-server IDs to local GGUF filenames."""
    base = Path(str(name or "")).name.lower()
    if base.endswith(".gguf"):
        base = base[:-5]
    return "".join(c for c in base if c.isalnum())


def _resolve_model(name: str) -> str:
    """Resolve a config model name (filename or server ID) to a server-registered ID.

    Checks (in order):
    1. Runtime mapping (_model_name_map) built by health_check()
    2. Persisted server_model_id in models_config.json
    3. For .gguf files: fall back to the filename itself (llama-server registers
       loaded models by their GGUF internal name, which is the filename).
    4. For non-.gguf names: return as-is (already a server ID or model alias).
    """
    if not name:
        return name
    # Runtime mapping from health check
    if name in _model_name_map:
        return _model_name_map[name]
    # Already a server ID (not a filename)
    if not name.endswith(".gguf"):
        return name
    # Persisted config mapping
    try:
        import json
        models_cfg_path = Path(__file__).parent / "models_config.json"
        if models_cfg_path.exists():
            with open(models_cfg_path) as f:
                mc = json.load(f)
            sid = mc.get(name, {}).get("server_model_id")
            if sid:
                _model_name_map[name] = sid
                return sid
    except Exception:
        pass
    # Fallback: the .gguf filename itself is what llama-server registers.
    # Populate the runtime map so subsequent calls skip the file read.
    _model_name_map[name] = name
    return name


def _completion_model_attestation(
    requested_model: str,
    launch_model: str,
    effective_model: str | None,
    *,
    provider: str,
    source: str,
) -> dict:
    """Build model evidence for direct OpenAI-compatible completion paths."""
    effective = str(effective_model or "").strip()
    launch = str(launch_model or "").strip()
    if effective:
        left = _model_key(launch)
        right = _model_key(effective)
        equivalent = left == right
        if provider == "llama-server" and left and right:
            equivalent = equivalent or left in right or right in left
        status = "verified" if equivalent else "mismatch"
    elif launch:
        status = "configured"
    else:
        status = "unverified"
    harness = {
        "openrouter": "OpenRouter API",
        "dream": "Dream Engine",
        "llama-server": "Llama Server",
    }.get(provider, "Direct completion")
    return {
        "status": status,
        "harness": harness,
        "requested_model": str(requested_model or "").strip(),
        "launch_model": launch or None,
        "effective_model": effective or None,
        "model_provider": provider,
        "source": source,
        "verification_level": "provider_response" if effective else "launch_configuration",
        "fallback_allowed": False,
    }


def configured_completion_attestation(model: str | None) -> dict:
    """Return the exact direct-completion launch configuration for a model."""
    raw_model = model or config.llama_model
    if openrouter_client.is_openrouter_model(raw_model):
        provider = "openrouter"
        target_model = openrouter_client.model_slug(raw_model)
        source = "openrouter_request_configuration"
    elif _is_dream_model(raw_model):
        provider = "dream"
        target_model = config.dream_model
        source = "dream_request_configuration"
    else:
        provider = "llama-server"
        target_model = _resolve_model(raw_model)
        source = "llama_server_request_configuration"
    return _completion_model_attestation(
        raw_model,
        target_model,
        None,
        provider=provider,
        source=source,
    )

# ── Helpers ────────────────────────────────────────────────────────────────

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
]


def _llama_host() -> str:
    """Return the Llama Server host URL with protocol prefix."""
    return config.llama_host_url()


def _model_matches(a: str, b: str) -> bool:
    if not a or not b:
        return False
    def norm(s: str) -> str:
        s = Path(str(s)).name.lower()
        if s.endswith(".gguf"):
            s = s[:-5]
        return "".join(c for c in s if c.isalnum())
    na = norm(a)
    nb = norm(b)
    return na == nb or na in nb or nb in na


def _is_dream_model(model: str | None) -> bool:
    return bool(model and getattr(config, "dream_model", "") and _model_matches(model, config.dream_model))


def _effective_max_tokens(max_tokens: int | None, raw_model: str | None) -> int:
    effective = int(max_tokens or config.max_tokens)
    if _is_dream_model(raw_model):
        floor = int(getattr(config, "dream_min_output_tokens", 0) or 0)
        if floor > 0:
            effective = max(effective, floor)
    return effective


def _chat_host_for_model(model: str | None) -> str:
    if openrouter_client.is_openrouter_model(model):
        return openrouter_client.base_url()
    if _is_dream_model(model):
        # Default to the real sidecar port (8787). The legacy 18081 port survives
        # only in config.py's env default; if DREAM_HOST is unset, falling back to
        # 18081 sends Weave (sync_chat / stream_chat OODA passes) to a dead port.
        host = getattr(config, "dream_host", "") or "http://127.0.0.1:8787"
        if host and not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        # localhost -> 127.0.0.1: the DiffusionGemma sidecar listens on IPv4 only,
        # but Windows resolves "localhost" IPv6-first, so every HTTP request eats a
        # ~2s ::1 connect fallback. Weave makes several requests per turn (OODA
        # pass + repair pass), so this compounds. Mirrors _dream_openai_base_url
        # in server.py — keep both rewrites in lockstep.
        host = host.replace("//localhost", "//127.0.0.1")
        return host.rstrip("/")
    return _llama_host()


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


def _image_to_data_url(path: str) -> Optional[str]:
    """Convert an image file to a data: URL for OpenAI vision blocks.
    Converts formats like WebP (unsupported by llama.cpp/stb_image) to JPEG in memory."""
    try:
        mime, _ = mimetypes.guess_type(path)
        if mime == "image/webp" or path.lower().endswith(".webp"):
            from PIL import Image
            import io
            with Image.open(path) as img:
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    img = img.convert("RGB")
                buffered = io.BytesIO()
                img.save(buffered, format="JPEG", quality=90)
                data = buffered.getvalue()
                mime = "image/jpeg"
        else:
            with open(path, "rb") as f:
                data = f.read()
    except Exception as e:
        print(f"[LLAMA-CLIENT] Image read/conversion failed for {path}: {e}")
        return None
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


def _build_messages(messages: list[dict], verbatim_window: Optional[int] = None) -> list[dict]:
    """Convert internal message format to OpenAI-compatible content-block format.

    If verbatim_window is set, only the last N messages get native image blocks;
    older messages substitute the stored text description so the KV cache
    doesn't carry historical pixels.
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


async def _mock_stream(messages: list[dict]) -> AsyncGenerator[str, None]:
    """Generate a mock streaming response for testing."""
    response = random.choice(MOCK_RESPONSES)
    words = response.split(" ")
    for i, word in enumerate(words):
        yield ("" if i == 0 else " ") + word
        await asyncio.sleep(0.02 + random.random() * 0.03)


async def _stream_openrouter_chat(
    messages: list[dict],
    temperature: float = None,
    top_p: float = None,
    max_tokens: int = None,
    model: str = None,
    verbatim_window: int = None,
) -> AsyncGenerator:
    raw_model = model or "openrouter:z-ai/glm-5.2"
    target_model = openrouter_client.model_slug(raw_model)
    effective_max = _effective_max_tokens(max_tokens, raw_model)
    win = verbatim_window if verbatim_window is not None else config.verbatim_window
    built_messages = _build_messages(messages, verbatim_window=win)
    projected = openrouter_client.estimate_request_cost(built_messages, effective_max)
    await openrouter_client.ensure_budget_available(projected_cost_usd=projected)

    payload = {
        "model": target_model,
        "messages": built_messages,
        "stream": True,
        "temperature": temperature if temperature is not None else config.temperature,
        "top_p": top_p if top_p is not None else config.top_p,
        "max_tokens": effective_max,
    }
    payload.update(openrouter_client.provider_price_guard())

    print(f"[OPENROUTER] Sending {len(messages)} messages to {target_model}")
    try:
        client = _client()
        async with client.stream(
            "POST",
            f"{openrouter_client.base_url()}/chat/completions",
            json=payload,
            headers={**openrouter_client.request_headers(), "Accept": "text/event-stream"},
            timeout=httpx.Timeout(300.0, connect=10.0),
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                try:
                    err = json.loads(body).get("error", body.decode())
                    if isinstance(err, dict):
                        err = err.get("message") or err.get("code") or err
                except Exception:
                    err = f"HTTP {response.status_code}"
                raise RuntimeError(f"OpenRouter error: {err}")

            _was_thinking = False
            _content_tokens = 0
            _usage_info = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            }
            _attested_model = ""
            async for line in response.aiter_lines():
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    if isinstance(err, dict):
                        err = err.get("message") or err.get("code") or err
                    raise RuntimeError(f"OpenRouter error: {err}")

                reported_model = str(chunk.get("model") or "").strip()
                if reported_model and reported_model != _attested_model:
                    _attested_model = reported_model
                    yield {
                        "type": "model_attestation",
                        "model_attestation": _completion_model_attestation(
                            raw_model,
                            target_model,
                            reported_model,
                            provider="openrouter",
                            source="openrouter_stream_chunk",
                        ),
                    }

                usage = chunk.get("usage")
                if usage:
                    _usage_info.update(openrouter_client.usage_from_openai_payload(usage))

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                reasoning = (delta.get("reasoning_content") or delta.get("reasoning") or "")
                token = delta.get("content") or ""
                if reasoning:
                    if not _was_thinking:
                        _was_thinking = True
                        yield {"type": "thinking_start"}
                    # Reasoning must go out on the thinking channel. A bare string
                    # here would be concatenated into the visible message body by
                    # the Weave/OODA handlers, leaking chain-of-thought into chat
                    # for reasoning models (e.g. deepseek-v4-flash, kimi-k2.7).
                    yield {"type": "thinking_delta", "text": reasoning}
                if token:
                    if _was_thinking:
                        _was_thinking = False
                        yield {"type": "thinking_end"}
                    _content_tokens += 1
                    yield token

            yield {
                "type": "usage",
                "input_tokens": _usage_info.get("input_tokens", 0),
                "output_tokens": _usage_info.get("output_tokens", 0) or _content_tokens,
                "cost_usd": _usage_info.get("cost_usd", 0.0),
                **({
                    "reasoning_tokens": _usage_info["reasoning_tokens"]
                } if _usage_info.get("reasoning_tokens") else {}),
                **({
                    "cached_tokens": _usage_info["cached_tokens"]
                } if _usage_info.get("cached_tokens") else {}),
            }
            return
    except openrouter_client.OpenRouterBudgetError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as e:
        raise RuntimeError(f"Cannot reach OpenRouter at {openrouter_client.base_url()}: {e}")


async def _sync_openrouter_chat(
    messages: list[dict],
    temperature: float = None,
    max_tokens: int = None,
    model: str = None,
    think: bool = None,
    return_usage: bool = False,
):
    raw_model = model or "openrouter:z-ai/glm-5.2"
    target_model = openrouter_client.model_slug(raw_model)
    effective_max = _effective_max_tokens(max_tokens, raw_model)
    built_messages = _build_messages(messages, verbatim_window=config.verbatim_window)
    projected = openrouter_client.estimate_request_cost(built_messages, effective_max)
    await openrouter_client.ensure_budget_available(projected_cost_usd=projected)

    payload = {
        "model": target_model,
        "messages": built_messages,
        "stream": False,
        "temperature": temperature if temperature is not None else config.temperature,
        "max_tokens": effective_max,
    }
    payload.update(openrouter_client.provider_price_guard())
    if think is not None:
        payload["reasoning"] = {"enabled": bool(think)}

    try:
        client = _client()
        resp = await client.post(
            f"{openrouter_client.base_url()}/chat/completions",
            json=payload,
            headers=openrouter_client.request_headers(),
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error")
                if isinstance(err, dict):
                    err = err.get("message") or err.get("code") or err
            except Exception:
                err = f"HTTP {resp.status_code}"
            raise RuntimeError(f"OpenRouter error: {err}")
        data = resp.json()
        usage_info = dict(openrouter_client.usage_from_openai_payload(data.get("usage")))
        usage_info["model_attestation"] = _completion_model_attestation(
            raw_model,
            target_model,
            data.get("model"),
            provider="openrouter",
            source="openrouter_completion_response",
        )
        choices = data.get("choices") or []
        if not choices:
            return ("", usage_info) if return_usage else ""
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        result = content or reasoning or ""
        return (result, usage_info) if return_usage else result
    except openrouter_client.OpenRouterBudgetError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as e:
        raise RuntimeError(f"Cannot reach OpenRouter at {openrouter_client.base_url()}: {e}")


# ── Public API ─────────────────────────────────────────────────────────────

def list_local_models() -> list[str]:
    """Scan the models directory and return a sorted list of .gguf filenames."""
    models_dir = Path(config.llama_models_dir)
    if not models_dir.exists():
        return []
    return sorted(p.name for p in models_dir.glob("*.gguf"))


async def health_check() -> dict:
    """Check if Llama Server is reachable and return available models."""
    global _mock_mode
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.5)) as client:
            resp = await client.get(f"{_llama_host()}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            server_items = [m for m in data.get("data", []) if isinstance(m, dict)]
            models = [str(m.get("id") or "") for m in server_items if m.get("id")]
            model_meta = {}
            for item in server_items:
                mid = str(item.get("id") or "")
                if not mid:
                    continue
                meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                model_meta[mid] = {
                    "n_ctx": meta.get("n_ctx"),
                    "n_ctx_train": meta.get("n_ctx_train"),
                    "n_params": meta.get("n_params"),
                    "capabilities": item.get("capabilities") or [],
                }
            local = list_local_models()
            # Build filename → server-ID mapping.
            # If server and local lists match in size, pair them directly.
            # Otherwise try to match by normalised name substring.
            _model_name_map.clear()
            # Map the active config model to any server model it matches.
            # Llama-server loads one model at a time; the config stores the .gguf
            # filename but the server registers it by its internal GGUF name.
            active = config.llama_model or ""
            active_norm = _model_key(active)
            for srv in models:
                srv_norm = _model_key(srv)
                # Check if the server ID contains key parts of the config filename
                if active_norm and (active_norm in srv_norm or srv_norm in active_norm):
                    _model_name_map[active] = srv
            # Also map any local .gguf that shares the same normalization
            for loc in local:
                if loc in _model_name_map:
                    continue
                loc_norm = _model_key(loc)
                for srv in models:
                    srv_norm = _model_key(srv)
                    if loc_norm in srv_norm or srv_norm in loc_norm:
                        _model_name_map[loc] = srv
            target = config.llama_model
            available = bool(models)
            _mock_mode = False
            return {
                "status": "ok",
                "models": models,
                "target_model": target,
                "model_available": available,
                "mock_mode": False,
                "local_models": local,
                "model_name_map": dict(_model_name_map),
                "model_meta": model_meta,
            }
    except Exception as e:
        _mock_mode = True
        return {
            "status": "mock",
            "error": str(e),
            "mock_mode": True,
            "message": f"Llama Server not reachable at {_llama_host()} — running in mock mode",
            "local_models": list_local_models(),
        }


async def stream_chat(
    messages: list[dict],
    temperature: float = None,
    top_p: float = None,
    max_tokens: int = None,
    repeat_penalty: float = None,
    model: str = None,
    verbatim_window: int = None,
    think: bool = None,
) -> AsyncGenerator:
    """Stream chat completion tokens from Llama Server (or mock).

    Yields:
      str token chunks for visible content,
      {"type": "thinking_start"} / {"type": "thinking_delta"} /
      {"type": "thinking_end"} for reasoning content,
      {"type": "usage", "input_tokens": N, "output_tokens": N} as the final event.
    """
    global _mock_mode
    raw_model = model or config.llama_model
    if openrouter_client.is_openrouter_model(raw_model):
        async for item in _stream_openrouter_chat(
            messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            model=raw_model,
            verbatim_window=verbatim_window,
        ):
            yield item
        return
    if _mock_mode and not _is_dream_model(raw_model):
        print("[LLAMA] WARNING: running in MOCK MODE")
        mock_attestation = _completion_model_attestation(
            raw_model,
            _resolve_model(raw_model),
            None,
            provider="llama-server",
            source="local_mock_response",
        )
        mock_attestation["status"] = "unverified"
        mock_attestation["verification_level"] = "none"
        yield {"type": "model_attestation", "model_attestation": mock_attestation}
        async for tok in _mock_stream(messages):
            yield tok
        return
    target_model = config.dream_model if _is_dream_model(raw_model) else _resolve_model(raw_model)
    chat_host = _chat_host_for_model(raw_model)
    print(f"[LLAMA] Sending {len(messages)} messages to {target_model} via {chat_host}")

    effective_max = _effective_max_tokens(max_tokens, raw_model)
    win = verbatim_window if verbatim_window is not None else config.verbatim_window

    payload = {
        "model": target_model,
        "messages": _build_messages(messages, verbatim_window=win),
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": temperature if temperature is not None else config.temperature,
        "top_p": top_p if top_p is not None else config.top_p,
        "max_tokens": effective_max,
        "repeat_penalty": repeat_penalty if repeat_penalty is not None else config.repeat_penalty,
    }
    if _is_dream_model(raw_model):
        print(
            f"[DREAM] stream request max_tokens={effective_max} "
            f"floor={getattr(config, 'dream_min_output_tokens', None)} requested={max_tokens}"
        )
    if think is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    elif _is_dream_model(raw_model):
        payload["chat_template_kwargs"] = {
            "enable_thinking": bool(getattr(config, "dream_enable_thinking", True))
        }

    # Dream sidecar cold-loads (JIT load of the 17GB NVFP4 GGUF) can exceed the
    # default 300s timeout. Mirror dream_client.REQUEST_TIMEOUT (600s) for dream.
    _stream_timeout = 600.0 if _is_dream_model(raw_model) else 300.0
    try:
        client = _client()
        async with client.stream(
            "POST",
            f"{chat_host}/v1/chat/completions",
            json=payload,
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(_stream_timeout, connect=10.0),
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                try:
                    err = json.loads(body).get("error", body.decode())
                except Exception:
                    err = f"HTTP {response.status_code}"
                raise RuntimeError(f"Llama Server error: {err}")

            _was_thinking = False
            _content_tokens = 0
            _input_tokens = 0
            _output_tokens = 0
            _think_markup_buffer = ""
            _in_think_markup = False
            _attested_model = ""
            backend_provider = "dream" if _is_dream_model(raw_model) else "llama-server"

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
                    raise RuntimeError(f"Llama Server error: {chunk['error']}")

                reported_model = str(chunk.get("model") or "").strip()
                if reported_model and reported_model != _attested_model:
                    _attested_model = reported_model
                    yield {
                        "type": "model_attestation",
                        "model_attestation": _completion_model_attestation(
                            raw_model,
                            target_model,
                            reported_model,
                            provider=backend_provider,
                            source="openai_compatible_stream_chunk",
                        ),
                    }

                usage = chunk.get("usage")
                if usage:
                    _input_tokens = usage.get("prompt_tokens", _input_tokens)
                    _output_tokens = usage.get("completion_tokens", _output_tokens)

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                # llama-server emits reasoning under "reasoning_content" delta key
                reasoning = (delta.get("reasoning_content") or delta.get("reasoning") or "")
                token = delta.get("content") or ""
                if _is_dream_model(raw_model) and token:
                    extracted_reasoning, token = _split_channel_scaffold(token)
                    if extracted_reasoning:
                        if not _was_thinking:
                            _was_thinking = True
                            yield {"type": "thinking_start"}
                        yield extracted_reasoning

                if reasoning:
                    if not _was_thinking:
                        _was_thinking = True
                        yield {"type": "thinking_start"}
                    if _is_dream_model(raw_model):
                        yield reasoning
                    else:
                        yield {"type": "thinking_delta", "text": reasoning}

                stop_stream = False
                if token:
                    if _is_dream_model(raw_model):
                        if _was_thinking:
                            _was_thinking = False
                            yield {"type": "thinking_end"}
                        _content_tokens += 1
                        yield token
                    else:
                        events, _think_markup_buffer, _in_think_markup = _consume_think_markup(
                            _think_markup_buffer + token,
                            _in_think_markup,
                        )
                        for kind, text in events:
                            if not text:
                                continue
                            if kind == "thinking":
                                if not _was_thinking:
                                    _was_thinking = True
                                    yield {"type": "thinking_start"}
                                yield {"type": "thinking_delta", "text": text}
                                continue
                            if _was_thinking:
                                _was_thinking = False
                                yield {"type": "thinking_end"}
                            _content_tokens += 1
                            yield text
                            if _content_tokens >= effective_max:
                                stop_stream = True
                                break
                if stop_stream:
                    break

            if not _is_dream_model(raw_model) and _think_markup_buffer:
                events, _think_markup_buffer, _in_think_markup = _consume_think_markup(
                    _think_markup_buffer,
                    _in_think_markup,
                    final=True,
                )
                for kind, text in events:
                    if not text:
                        continue
                    if kind == "thinking":
                        if not _was_thinking:
                            _was_thinking = True
                            yield {"type": "thinking_start"}
                        yield {"type": "thinking_delta", "text": text}
                    else:
                        if _was_thinking:
                            _was_thinking = False
                            yield {"type": "thinking_end"}
                        _content_tokens += 1
                        yield text
            if _was_thinking:
                yield {"type": "thinking_end"}

            if _is_dream_model(raw_model):
                yield {
                    "type": "usage",
                    "input_tokens": _input_tokens,
                    "output_tokens": _output_tokens or _content_tokens,
                    "content_chunks": _content_tokens,
                }
            else:
                yield {
                    "type": "usage",
                    "input_tokens": _input_tokens,
                    "output_tokens": _output_tokens or _content_tokens,
                }
            return
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as e:
        raise RuntimeError(f"Cannot reach local chat backend at {chat_host}: {e}")


async def sync_chat(
    messages: list[dict],
    temperature: float = None,
    max_tokens: int = None,
    model: str = None,
    think: bool = None,
    return_usage: bool = False,
):
    """Non-streaming chat completion (summarization, OODA passes, etc.)."""
    global _mock_mode
    raw_model = model or config.llama_model
    if openrouter_client.is_openrouter_model(raw_model):
        return await _sync_openrouter_chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=raw_model,
            think=think,
            return_usage=return_usage,
        )
    # Dream backend is independent — don't skip it because the local llama server
    # fell into mock mode. Dream failures re-raise rather than setting _mock_mode.
    if _mock_mode and not _is_dream_model(raw_model):
        fallback = "Summary: The conversation continues with escalating tension and mutual wariness."
        usage = {
            "input_tokens": sum(len(m.get("content") or "") // 3 for m in messages),
            "output_tokens": len(fallback) // 3,
            "cost_usd": 0.0,
        }
        usage["model_attestation"] = _completion_model_attestation(
            raw_model,
            _resolve_model(raw_model),
            None,
            provider="llama-server",
            source="local_mock_response",
        )
        usage["model_attestation"]["status"] = "unverified"
        usage["model_attestation"]["verification_level"] = "none"
        return (fallback, usage) if return_usage else fallback
    target_model = config.dream_model if _is_dream_model(raw_model) else _resolve_model(raw_model)
    chat_host = _chat_host_for_model(raw_model)
    payload = {
        "model": target_model,
        "messages": _build_messages(messages, verbatim_window=config.verbatim_window),
        "stream": False,
        "temperature": temperature if temperature is not None else config.temperature,
        "max_tokens": _effective_max_tokens(max_tokens, raw_model),
    }
    if _is_dream_model(raw_model):
        print(
            f"[DREAM] sync request max_tokens={payload['max_tokens']} "
            f"floor={getattr(config, 'dream_min_output_tokens', None)} requested={max_tokens}"
        )
    if think is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    elif _is_dream_model(raw_model):
        payload["chat_template_kwargs"] = {
            "enable_thinking": bool(getattr(config, "dream_enable_thinking", True))
        }

    # Dream sidecar cold-loads can exceed the default 300s timeout; mirror
    # dream_client.REQUEST_TIMEOUT (600s) for dream models (OODA sync passes).
    _sync_timeout = 600.0 if _is_dream_model(raw_model) else 300.0
    try:
        client = _client()
        resp = await client.post(
            f"{chat_host}/v1/chat/completions",
            json=payload,
            timeout=httpx.Timeout(_sync_timeout, connect=10.0),
        )
        resp.raise_for_status()
        data = resp.json()
        usage_info = dict(openrouter_client.usage_from_openai_payload(data.get("usage")))
        backend_provider = "dream" if _is_dream_model(raw_model) else "llama-server"
        usage_info["model_attestation"] = _completion_model_attestation(
            raw_model,
            target_model,
            data.get("model"),
            provider=backend_provider,
            source="openai_compatible_completion_response",
        )
        choices = data.get("choices") or []
        if not choices:
            return ("", usage_info) if return_usage else ""
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        if _is_dream_model(raw_model):
            extracted_reasoning, content = _split_channel_scaffold(content)
            if extracted_reasoning:
                reasoning = (
                    (reasoning.rstrip() + "\n\n" + extracted_reasoning).strip()
                    if reasoning else extracted_reasoning
                )
        result = content or reasoning or ""
        return (result, usage_info) if return_usage else result
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as _e:
        if _is_dream_model(raw_model):
            raise RuntimeError(
                f"Dream sidecar unreachable at {chat_host} — is DiffusionGemma running?"
            ) from _e
        _mock_mode = True
        fallback = "Summary: The conversation continues with escalating tension and mutual wariness."
        usage = {
            "input_tokens": sum(len(m.get("content") or "") // 3 for m in messages),
            "output_tokens": len(fallback) // 3,
            "cost_usd": 0.0,
        }
        usage["model_attestation"] = _completion_model_attestation(
            raw_model,
            _resolve_model(raw_model),
            None,
            provider="llama-server",
            source="local_mock_response",
        )
        usage["model_attestation"]["status"] = "unverified"
        usage["model_attestation"]["verification_level"] = "none"
        return (fallback, usage) if return_usage else fallback


async def describe_image(image_path: str, model: str = None, context: str = None) -> str:
    """Use llama-server (vision-capable model) to describe an image in detail."""
    global _mock_mode
    if _mock_mode:
        # Vision is independent — check if the server recovered.
        # Don't let stale _mock_mode from a prior chat failure permanently blind vision.
        import httpx as _httpx
        try:
            async with _httpx.AsyncClient(timeout=_httpx.Timeout(2.0, connect=0.5)) as _hc:
                await _hc.get(f"{_llama_host()}/v1/models")
            _mock_mode = False  # Server is back — clear the ratchet
        except Exception:
            # Server still down — return informative fallback, not the silent mock
            return "An image was shared, but the local vision model is unavailable right now."

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

    target_model = _resolve_model(model or config.vision_model or config.llama_model)
    payload = {
        "model": target_model,
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
    }
    try:
        async with _llama_lock:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                resp = await client.post(f"{_llama_host()}/v1/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    return "An image was shared, but the description response was empty."
                msg = choices[0].get("message") or {}
                return msg.get("content") or msg.get("reasoning_content") or "An image was shared, but the description response content was empty."
    except Exception as e:
        print(f"[LLAMA-DESCRIBE] {type(e).__name__}: {str(e)[:200]}")
        return f"An image was shared, but the local vision model failed to describe it ({type(e).__name__})."


async def describe_image_with_data(
    image_path: str, model: str = None, context: str = None
) -> tuple[str, dict]:
    """Returns (description, image_payload) compatible with the rest of the codebase."""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except (IOError, OSError):
        return ("An image was shared but could not be read.", {})
    desc = await describe_image(image_path, model=model, context=context)
    return (desc, {"images": [b64]})
