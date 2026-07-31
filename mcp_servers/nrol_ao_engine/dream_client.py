"""Thin OpenAI-format tool-call client for the Dream (DiffusionGemma) sidecar.

This is the engine agent's only LLM transport (Track A phase 1, custom Python
loop path — A.7). It speaks OpenAI Chat Completions directly to the sidecar at
``:8787`` with ``tools`` and returns the parsed choice. It is NOT the Anthropic
shim (Track B, ``anthropic_dream_router.py``) and is NOT a general chat client.

Why sync: the engine-agent loop is a sequential turn-by-turn dispatch — one
Dream call, dispatch tool calls, append results, repeat. Sync keeps the loop
trivial and matches ``mcp_servers/nrol_ao/llama.py:chat()``'s style. The
sidecar itself queues concurrent calls; a sync caller does not reduce
throughput vs the scan's existing single-worker serialization.

Tool-call path is verified clean (§2.2 + Phase 0.5 probe): when the model emits
a tool call, ``content`` is empty and no ``<|channel>thought`` markup leaks
into the structured ``tool_calls`` object. We therefore do NOT run the channel
strip on tool-call responses — only on the final stop-turn text, and only as a
defensive best-effort.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

DEFAULT_HOST = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT = 600.0
DEFAULT_MAX_TOKENS = 4096

_DREAM_THOUGHT_BLOCK_RE = re.compile(
    r"<\|channel>thought\s*(.*?)<channel\|>",
    re.IGNORECASE | re.DOTALL,
)


def _split_channel_scaffold(text: str) -> tuple[str, str]:
    """Return (reasoning, content), stripping Dream thought-channel markup.

    Canonical copy of the strip in ``mcp_servers/nrol_ao/llama.py`` and
    ``dream_client.py``. Used here ONLY on the final stop-turn text response
    (defensive); tool-call responses have empty content by construction.
    """
    if "<|channel>" not in text and "<channel|>" not in text:
        return "", text
    normalized = text.replace("\r\n", "\n")
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


def _strip_channel_scaffold(text: str) -> str:
    return _split_channel_scaffold(text)[1]


def resolve_host(host: str | None = None) -> str:
    """Pick the Dream host: explicit arg > env > config > default.

    Rewrites localhost→127.0.0.1 (Windows resolves localhost IPv6-first while
    the sidecar binds IPv4-only — ~2s per fresh TCP connect). Mirrors
    ``llama._normalize_host``.
    """
    if host:
        h = host.strip()
    else:
        h = (
            os.environ.get("NROL_AO_DREAM_HOST")
            or os.environ.get("DREAM_HOST")
            or ""
        ).strip()
    if not h:
        # Best-effort config.json read, matching llama._load_loom_config shape.
        try:
            from pathlib import Path
            cfg_path = Path.cwd() / "config.json"
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                h = str(cfg.get("dream_host") or "")
        except Exception:
            h = ""
    if not h:
        h = DEFAULT_HOST
    if h and not h.startswith(("http://", "https://")):
        h = f"http://{h}"
    return h.replace("//localhost", "//127.0.0.1").rstrip("/")


def resolve_model(model: str | None = None) -> str:
    """Pick the Dream model id: explicit arg > env > config > empty."""
    if model:
        return model
    m = (
        os.environ.get("NROL_AO_DREAM_MODEL")
        or os.environ.get("DREAM_MODEL")
        or ""
    ).strip()
    if not m:
        try:
            from pathlib import Path
            cfg_path = Path.cwd() / "config.json"
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                m = str(cfg.get("dream_model") or "")
        except Exception:
            m = ""
    return m


def chat_with_tools(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    host: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT,
    tool_choice: str = "auto",
    enable_thinking: bool = True,
) -> dict[str, Any]:
    """Send one OpenAI-format chat request (with tools) to the Dream sidecar.

    Returns a normalized dict:
        {
          "finish_reason": str,            # "tool_calls" | "stop" | "length" | ...
          "content": str,                 # stripped of channel markup (may be "")
          "tool_calls": list[dict],       # OpenAI tool_calls objects (possibly [])
          "usage": dict,                  # raw usage from the sidecar
          "raw": dict,                    # the full response JSON
        }

    Does NOT parse tool-call arguments — the caller (engine_agent) does, so it
    can distinguish malformed-JSON from transport errors. Each tool_calls entry
    is the raw sidecar object: ``{id, type, function: {name, arguments(str)}}``.
    """
    h = resolve_host(host)
    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)},
    }
    resolved_model = resolve_model(model)
    if resolved_model:
        payload["model"] = resolved_model
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{h}/v1/chat/completions", json=payload)
        if resp.status_code >= 400:
            message_summary = [
                {
                    "role": str(msg.get("role") or ""),
                    "content_len": len(str(msg.get("content") or "")),
                    "tool_calls": len(msg.get("tool_calls") or []),
                    "tool_call_id": str(msg.get("tool_call_id") or ""),
                }
                for msg in messages[-8:]
            ]
            raise RuntimeError(
                f"Dream sidecar HTTP {resp.status_code} at {h}: "
                f"{resp.text[:1000]}; "
                f"request_summary={{'messages': {len(messages)}, "
                f"'recent': {message_summary}, "
                f"'tools': {[t.get('function', {}).get('name') for t in (tools or [])]}, "
                f"'tool_choice': {tool_choice!r}, "
                f"'temperature': {temperature}, "
                f"'max_tokens': {max_tokens}, "
                f"'enable_thinking': {bool(enable_thinking)}}}"
            )
        data = resp.json()

    choice = (data.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content = _strip_channel_scaffold(str(msg.get("content") or ""))
    tool_calls = msg.get("tool_calls") or []
    return {
        "finish_reason": choice.get("finish_reason") or "",
        "content": content,
        "tool_calls": tool_calls,
        "usage": data.get("usage") or {},
        "raw": data,
    }


def parse_tool_args(raw_arguments: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a tool_call.function.arguments value into a dict.

    The sidecar delivers arguments as a JSON string. Returns (parsed_dict, None)
    on success or (None, error_message) on malformed JSON. Used by the engine
    agent loop to decide retry-vs-fail-closed.
    """
    if isinstance(raw_arguments, dict):
        return raw_arguments, None
    if not isinstance(raw_arguments, str):
        return None, f"arguments is {type(raw_arguments).__name__}, not str/dict"
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc}"
    if not isinstance(parsed, dict):
        return None, f"arguments parsed to {type(parsed).__name__}, not object"
    return parsed, None
