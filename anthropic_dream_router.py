"""Anthropic Messages API shim for the Dream DiffusionGemma sidecar.

This file is deliberately standalone. It is not imported by Loom's normal
generation paths, so failures here cannot affect Claude, local llama, Hermes,
Dream Space, or Weave unless a caller explicitly points at this router.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import config


DEFAULT_HOST = "http://127.0.0.1:8787"
REQUEST_TIMEOUT = 600.0
DEFAULT_MAX_TOKENS = int(os.getenv("DREAM_SHIM_MAX_TOKENS_FALLBACK", "4096"))

app = FastAPI(title="Dream Anthropic Router", version="0.1.0")

_DREAM_THOUGHT_BLOCK_RE = re.compile(
    r"<\|channel>thought\s*(.*?)<channel\|>",
    re.IGNORECASE | re.DOTALL,
)


def _host() -> str:
    host = os.getenv("DREAM_HOST") or getattr(config, "dream_host", "") or DEFAULT_HOST
    if host and not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return (host or DEFAULT_HOST).replace("//localhost", "//127.0.0.1").rstrip("/")


def _model() -> str:
    return os.getenv("DREAM_MODEL") or getattr(config, "dream_model", "") or "diffusiongemma"


def _envbool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _enable_thinking_default() -> bool:
    configured = bool(getattr(config, "dream_enable_thinking", True))
    return _envbool("DREAM_SHIM_ENABLE_THINKING", configured)


def _thinking_min_tokens() -> int:
    raw = os.getenv("DREAM_SHIM_THINKING_MIN_TOKENS")
    if raw is not None:
        try:
            parsed = int(raw)
            return parsed if parsed > 0 else 0
        except ValueError:
            pass
    return int(getattr(config, "dream_thinking_min_tokens", 4096) or 0)


def _max_tokens_from_body(body: dict[str, Any]) -> int:
    value = body.get("max_tokens")
    if value is None:
        return DEFAULT_MAX_TOKENS
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS
    return parsed if parsed > 0 else DEFAULT_MAX_TOKENS


def _split_channel_scaffold(text: str) -> tuple[str, str]:
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
    return _split_channel_scaffold(text or "")[1]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text") or ""))
                elif btype == "image":
                    source = block.get("source") if isinstance(block.get("source"), dict) else {}
                    media = source.get("media_type") or "image"
                    parts.append(f"[image omitted: {media}]")
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(value)


def _anthropic_message_to_oai(msg: dict[str, Any]) -> list[dict[str, Any]]:
    role = str(msg.get("role") or "")
    if "content" not in msg:
        return [] if role == "assistant" else [msg]

    content = msg.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        return [{"role": role, "content": _as_text(content)}]

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text") or ""))
        elif btype == "thinking":
            reasoning_parts.append(str(block.get("thinking") or ""))
        elif btype == "image":
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            media = source.get("media_type") or "image"
            text_parts.append(f"[image omitted: {media}]")
        elif btype == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}),
                },
            })
        elif btype == "tool_result":
            result_text = _as_text(block.get("content"))
            tool_results.append({
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id") or ""),
                "content": result_text,
            })

    out: list[dict[str, Any]] = []
    if role == "user" and tool_results:
        out.extend(tool_results)
    if text_parts or reasoning_parts or tool_calls:
        new_msg: dict[str, Any] = {"role": role, "content": "".join(text_parts)}
        if tool_calls:
            new_msg["tool_calls"] = tool_calls
        if reasoning_parts:
            new_msg["reasoning_content"] = "".join(reasoning_parts)
        out.append(new_msg)
    if role != "user" and tool_results:
        out.extend(tool_results)
    return out


def anthropic_to_openai(body: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []

    system = body.get("system")
    if system is not None:
        system_text = _as_text(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("'messages' must be an array")
    for msg in raw_messages:
        if isinstance(msg, dict):
            messages.extend(_anthropic_message_to_oai(msg))

    oai: dict[str, Any] = {
        "model": model or _model(),
        "messages": messages,
        "stream": False,
        "max_tokens": _max_tokens_from_body(body),
    }
    enable_thinking = _enable_thinking_default()
    oai["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    if enable_thinking:
        min_tokens = _thinking_min_tokens()
        if min_tokens:
            oai["max_tokens"] = max(int(oai["max_tokens"]), min_tokens)

    if isinstance(body.get("tools"), list):
        oai["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {},
                },
            }
            for tool in body["tools"]
            if isinstance(tool, dict)
        ]

    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == "auto":
            oai["tool_choice"] = "auto"
        elif tc_type in {"any", "tool"}:
            oai["tool_choice"] = "required"
    elif isinstance(tool_choice, str):
        oai["tool_choice"] = "required" if tool_choice in {"any", "tool"} else tool_choice

    passthrough = ("temperature", "top_p", "top_k", "seed", "ignore_eos")
    for key in passthrough:
        if key in body:
            oai[key] = body[key]
    if "stop_sequences" in body:
        oai["stop"] = body["stop_sequences"]
    return oai


def _stop_reason(finish_reason: str | None) -> str:
    return {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }.get(finish_reason or "stop", "end_turn")


def _usage(oai_usage: dict[str, Any] | None) -> dict[str, int]:
    oai_usage = oai_usage or {}
    return {
        "input_tokens": int(oai_usage.get("prompt_tokens") or 0),
        "cache_read_input_tokens": int(
            (oai_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        ),
        "output_tokens": int(oai_usage.get("completion_tokens") or 0),
    }


def openai_to_anthropic(data: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content_blocks: list[dict[str, Any]] = []

    extracted_reasoning, content = _split_channel_scaffold(str(msg.get("content") or ""))
    reasoning = str(msg.get("reasoning_content") or "")
    if extracted_reasoning:
        reasoning = (reasoning.rstrip() + "\n\n" + extracted_reasoning).strip() if reasoning else extracted_reasoning
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning})
    if content:
        content_blocks.append({"type": "text", "text": content})

    for call in msg.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else {}
        if not isinstance(fn, dict):
            fn = {}
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            parsed_args = {}
        content_blocks.append({
            "type": "tool_use",
            "id": str(call.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": str(fn.get("name") or ""),
            "input": parsed_args if isinstance(parsed_args, dict) else {},
        })

    return {
        "id": str(data.get("id") or f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": model or str(data.get("model") or _model()),
        "content": content_blocks or [{"type": "text", "text": ""}],
        "stop_reason": _stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": _usage(data.get("usage")),
    }


def _estimate_tokens(value: Any) -> int:
    text = ""
    if isinstance(value, dict):
        text = " ".join(str(k) + " " + str(_estimate_tokens(v)) for k, v in value.items())
    elif isinstance(value, list):
        text = " ".join(str(_estimate_tokens(v)) for v in value)
    else:
        text = str(value or "")
    return max(1, len(text) // 3)


async def _dream_chat(oai_body: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(f"{_host()}/v1/chat/completions", json=oai_body)
            if resp.status_code >= 400:
                raise HTTPException(resp.status_code, resp.text[:1000])
            return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Dream sidecar unreachable at {_host()}: {exc}") from exc


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def _anthropic_sse(message: dict[str, Any]) -> AsyncIterator[bytes]:
    start = {
        "type": "message_start",
        "message": {
            "id": message["id"],
            "type": "message",
            "role": "assistant",
            "model": message["model"],
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": message["usage"].get("input_tokens", 0)},
        },
    }
    yield _sse("message_start", start).encode("utf-8")

    for idx, block in enumerate(message["content"]):
        block_start = {k: v for k, v in block.items() if k not in {"text", "thinking", "signature"}}
        if block.get("type") == "tool_use":
            block_start = {
                "type": "tool_use",
                "id": block.get("id"),
                "name": block.get("name"),
                "input": {},
            }
        yield _sse("content_block_start", {
            "type": "content_block_start",
            "index": idx,
            "content_block": block_start,
        }).encode("utf-8")
        if block.get("type") == "text":
            text = block.get("text") or ""
            if text:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                }).encode("utf-8")
        elif block.get("type") == "thinking":
            thinking = block.get("thinking") or ""
            if thinking:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "thinking_delta", "thinking": thinking},
                }).encode("utf-8")
        elif block.get("type") == "tool_use":
            args = json.dumps(block.get("input") or {}, separators=(",", ":"))
            yield _sse("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": args},
            }).encode("utf-8")
        yield _sse("content_block_stop", {
            "type": "content_block_stop",
            "index": idx,
        }).encode("utf-8")

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": message["usage"].get("output_tokens", 0)},
    }).encode("utf-8")
    yield _sse("message_stop", {"type": "message_stop"}).encode("utf-8")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "dream_host": _host(), "model": _model()}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{
            "id": _model(),
            "object": "model",
            "created": int(time.time()),
            "owned_by": "local",
        }],
    }


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> dict[str, int]:
    body = await request.json()
    return {"input_tokens": _estimate_tokens(body)}


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    try:
        oai_body = anthropic_to_openai(body, model=_model())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    data = await _dream_chat(oai_body)
    message = openai_to_anthropic(data, model=_model())
    if body.get("stream"):
        return StreamingResponse(_anthropic_sse(message), media_type="text/event-stream")
    return JSONResponse(message)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("DREAM_SHIM_PORT", "8788"))
    uvicorn.run(app, host="127.0.0.1", port=port)
