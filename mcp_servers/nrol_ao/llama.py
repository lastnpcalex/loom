"""Small llama-server client for NROL-AO MCP jobs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx


def _load_loom_config() -> dict:
    cfg_path = Path.cwd() / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def llama_host() -> str:
    cfg = _load_loom_config()
    host = (
        os.environ.get("NROL_AO_LLAMA_HOST")
        or os.environ.get("LLAMA_HOST")
        or cfg.get("llama_host")
        or "http://localhost:8000"
    )
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host.rstrip("/")


def llama_model(explicit: str = "") -> str:
    cfg = _load_loom_config()
    return (
        explicit
        or os.environ.get("NROL_AO_LLAMA_MODEL")
        or os.environ.get("LLAMA_MODEL")
        or cfg.get("llama_model")
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
        return {"ok": True, "host": host, "models": models, "target_model": llama_model()}
    except Exception as exc:
        return {"ok": False, "host": host, "error": str(exc), "target_model": llama_model()}


def chat(
    prompt: str,
    *,
    system_prompt: str = "",
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_sec: int = 600,
) -> dict:
    host = llama_host()
    resolved_model = llama_model(model)
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

    with httpx.Client(timeout=timeout_sec) as client:
        response = client.post(f"{host}/v1/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

    text = ""
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except Exception:
        text = json.dumps(data, ensure_ascii=True)
    return {
        "host": host,
        "model": resolved_model,
        "text": text,
        "raw": data,
    }

