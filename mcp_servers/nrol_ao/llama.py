"""Small llama-server client for NROL-AO MCP jobs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx


# DiffusionGemma's nuspy adapter can leak the reasoning channel into
# message.content as <|channel>thought ... <channel|> markup instead of
# separating it into message.reasoning_content. This regex extracts and
# strips that markup so nrol_ao's structured-output parsers (DECISION/VERDICT/
# PROPOSAL blocks) see clean text. Canonical copy: dream_client.py:44-72
# (repo root). Duplicated here to avoid coupling the sync nrol_ao client to
# the async dream_client module.
_DREAM_THOUGHT_BLOCK_RE = re.compile(
    r"<\|channel>thought\s*(.*?)<channel\|>",
    re.IGNORECASE | re.DOTALL,
)


def _split_channel_scaffold(text: str) -> tuple[str, str]:
    """Return (reasoning, content), extracting Dream thought-channel markup.

    For content with no channel tags: fast-path returns ("", text) verbatim.
    Mirrors dream_client._split_channel_scaffold (dream_client.py:44-72)."""
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


def _load_loom_config() -> dict:
    cfg_path = Path.cwd() / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _normalize_host(host: str) -> str:
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    # Windows resolves "localhost" IPv6-first while the local model servers
    # bind IPv4-only — ~2s per fresh TCP connect. Pin to 127.0.0.1.
    return host.replace("//localhost", "//127.0.0.1").rstrip("/")


def llama_host() -> str:
    cfg = _load_loom_config()
    host = (
        os.environ.get("NROL_AO_LLAMA_HOST")
        or os.environ.get("LLAMA_HOST")
        or cfg.get("llama_host")
        or "http://127.0.0.1:8000"
    )
    return _normalize_host(host)


def llama_model(explicit: str = "") -> str:
    cfg = _load_loom_config()
    return (
        explicit
        or os.environ.get("NROL_AO_LLAMA_MODEL")
        or os.environ.get("LLAMA_MODEL")
        or cfg.get("llama_model")
        or ""
    )


def dream_host() -> str:
    cfg = _load_loom_config()
    host = (
        os.environ.get("NROL_AO_DREAM_HOST")
        or os.environ.get("DREAM_HOST")
        or cfg.get("dream_host")
        or "http://127.0.0.1:8787"
    )
    return _normalize_host(host)


def dream_model(explicit: str = "") -> str:
    cfg = _load_loom_config()
    return (
        explicit
        or os.environ.get("NROL_AO_DREAM_MODEL")
        or os.environ.get("DREAM_MODEL")
        or cfg.get("dream_model")
        or ""
    )


def resolve_backend(model: str = "") -> tuple[str, str, str]:
    """Map a requested model to (backend, host, resolved_model).

    model="dream" or "dream:<id>" targets the Dream Engine (DiffusionGemma
    OpenAI-compatible sidecar); anything else targets llama-server. Setting
    NROL_AO_LLM_BACKEND=dream flips the default backend for every LLM job
    without touching per-tool arguments.
    """
    m = (model or "").strip()
    low = m.lower()
    if low == "dream" or low.startswith("dream:"):
        explicit = m.split(":", 1)[1].strip() if ":" in m else ""
        return "dream", dream_host(), dream_model(explicit)
    default_backend = os.environ.get("NROL_AO_LLM_BACKEND", "llama").strip().lower()
    if not m and default_backend == "dream":
        return "dream", dream_host(), dream_model()
    return "llama", llama_host(), llama_model(m)


def llama_chat_template_file() -> str:
    cfg = _load_loom_config()
    return (
        os.environ.get("NROL_AO_LLAMA_CHAT_TEMPLATE_FILE")
        or os.environ.get("LLAMA_CHAT_TEMPLATE_FILE")
        or cfg.get("llama_chat_template_file")
        or ""
    )


def status() -> dict:
    host = llama_host()
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(f"{host}/v1/models")
            response.raise_for_status()
            data = response.json()
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return {
            "ok": True,
            "host": host,
            "models": models,
            "target_model": llama_model(),
            "chat_template_file": llama_chat_template_file(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "host": host,
            "error": str(exc),
            "target_model": llama_model(),
            "chat_template_file": llama_chat_template_file(),
        }


def dream_status() -> dict:
    host = dream_host()
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(f"{host}/v1/models")
            response.raise_for_status()
            data = response.json()
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return {
            "ok": True,
            "host": host,
            "models": models,
            "target_model": dream_model(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "host": host,
            "error": str(exc),
            "target_model": dream_model(),
        }


def chat(
    prompt: str,
    *,
    system_prompt: str = "",
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_sec: int = 600,
    disable_thinking: bool = False,
) -> dict:
    """disable_thinking=True asks the chat template to skip the reasoning
    channel (Qwen-style enable_thinking). Reasoning models otherwise spend
    the whole max_tokens budget inside reasoning_content and return an
    empty message.content — observed with Qwen3.6-27B on matcher prompts
    (finish_reason=length, 0 content tokens). Templates without the
    variable ignore the kwarg.

    On the dream backend (DiffusionGemma), the Loom sidecar accepts the same
    enable_thinking kwarg and maps it onto Gemma4's thought-channel template.

    model="dream" or "dream:<id>" routes to the Dream Engine (DiffusionGemma
    sidecar) instead of llama-server; see resolve_backend()."""
    backend, host, resolved_model = resolve_backend(model)
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if resolved_model:
        payload["model"] = resolved_model
    if backend == "dream":
        payload["chat_template_kwargs"] = {"enable_thinking": not disable_thinking}
    elif disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    with httpx.Client(timeout=timeout_sec) as client:
        response = client.post(f"{host}/v1/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

    text = ""
    reasoning = ""
    finish_reason = ""
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        finish_reason = choice.get("finish_reason") or ""
    except Exception:
        text = json.dumps(data, ensure_ascii=True)

    # DiffusionGemma's nuspy adapter can leak the thought channel into
    # message.content as <|channel>thought...<channel|> markup. Strip it
    # and merge into reasoning so structured-output parsers see clean text.
    # Qwen path (backend != "dream") is unchanged — fast-path returns the
    # text verbatim when no channel tags are present.
    if backend == "dream":
        extracted_reasoning, stripped_content = _split_channel_scaffold(text)
        text = stripped_content
        if extracted_reasoning:
            reasoning = (
                (reasoning.rstrip() + "\n\n" + extracted_reasoning).strip()
                if reasoning else extracted_reasoning
            )

    return {
        "backend": backend,
        "host": host,
        "model": resolved_model,
        "text": text,
        "reasoning_chars": len(reasoning),
        "finish_reason": finish_reason,
        "raw": data,
    }
