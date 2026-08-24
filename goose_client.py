"""Goose ACP subprocess wrapper for Loom.

Runs `goose acp` over stdio JSON-RPC and maps ACP updates into the same
event shape consumed by server.py for Claude/Codex/Hermes-style providers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import urlsplit, urlunsplit

import httpx

from loom_agent_prompt import prepend_loom_agent_context

log = logging.getLogger(__name__)

MODEL_PREFIX = "goose:"
_MODE_MARKERS = {"auto", "subagents", "approve", "smart-approve", "chat"}
_PERMISSION_HTTP_TIMEOUT = 900.0
_STREAM_LIMIT = 16 * 1024 * 1024
_OPENROUTER_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max", "none"}


class _RpcConn:
    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._backlog: dict[int, dict] = {}

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _write(self, msg: dict) -> None:
        if self._proc.stdin is None:
            raise RuntimeError("goose acp stdin is closed")
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def respond(self, req_id: Any, result: Any = None, error: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        await self._write(msg)

    def resolve_response(self, msg: dict) -> bool:
        rid = msg.get("id")
        if rid is None or "method" in msg:
            return False
        fut = self._pending.get(rid)
        if fut is None:
            self._backlog[rid] = msg
            return True
        if fut.done():
            return True
        if "error" in msg:
            fut.set_exception(RuntimeError(_text_from_value(msg["error"]) or "goose ACP request failed"))
        else:
            fut.set_result(msg.get("result") or {})
        return True


def default_goose_exe() -> str:
    """Find the Goose CLI in PATH or common Windows install locations."""
    env = os.environ.get("GOOSE_EXE", "").strip()
    if env:
        return _normalize_goose_exe(env)
    names = ["goose.exe", "goose.cmd", "goose"] if sys.platform == "win32" else ["goose"]
    for name in names:
        found = shutil.which(name)
        if found:
            return _normalize_goose_exe(found)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        appdata = os.environ.get("APPDATA", "")
        candidates = [
            Path(local) / "Programs" / "Goose" / "resources" / "bin" / "goose.exe",
            Path(local) / "Programs" / "Goose" / "Goose.exe",
            Path(local) / "Programs" / "Goose" / "goose.exe",
            Path(local) / "Goose" / "resources" / "bin" / "goose.exe",
            Path(local) / "Goose" / "goose.exe",
            Path(local) / "goose" / "bin" / "goose.exe",
            Path(appdata) / "Goose" / "goose.exe",
            Path(appdata) / "npm" / "goose.cmd",
        ]
        for path in candidates:
            if path.exists():
                return _normalize_goose_exe(str(path))
    return "goose"


def _normalize_goose_exe(exe: str) -> str:
    """Prefer Goose's CLI binary when a Windows desktop app path is discovered."""
    if sys.platform != "win32":
        return exe
    try:
        path = Path(exe)
    except (OSError, ValueError):
        return exe
    if path.name.lower() != "goose.exe":
        return exe
    if path.parent.name.lower() == "bin" and path.parent.parent.name.lower() == "resources":
        return str(path)
    cli = path.parent / "resources" / "bin" / "goose.exe"
    if cli.exists():
        return str(cli)
    return exe


def _goose_command(goose_exe: str | None = None) -> list[str]:
    exe = _normalize_goose_exe(goose_exe or default_goose_exe())
    if Path(exe).exists():
        return [exe]
    parts = shlex.split(exe, posix=sys.platform != "win32")
    return parts or ["goose"]


def is_goose_model(model: str | None) -> bool:
    return (model or "").strip().lower().startswith(MODEL_PREFIX)


def _strip_selector_mode(raw: str) -> tuple[str | None, str]:
    """Remove Loom-only Goose mode marker from a selector body."""
    marker, sep, rest = raw.partition(":")
    normalized = marker.strip().lower()
    if sep and normalized in _MODE_MARKERS:
        return ("auto" if normalized == "subagents" else normalized), rest
    return None, raw


def permission_mode_for_model(model: str | None, default: str | None = None) -> str:
    """Return the safe Goose permission mode implied by a Loom selector.

    Historical ``goose:auto:`` selectors are retained as stable model IDs, but
    Loom must still receive write/destructive permission requests. Goose's true
    auto mode bypasses ACP approval entirely, so route those selectors through
    smart approval instead.
    """
    raw = (model or "").strip()
    if raw.lower().startswith(MODEL_PREFIX):
        raw = raw.split(":", 1)[1]
    marker, _ = _strip_selector_mode(raw)
    selected = marker or default or "approve"
    if selected in {"auto", "subagents"}:
        return "smart_approve"
    return selected.replace("-", "_")


def split_goose_model(model: str | None) -> tuple[str, str]:
    """Return (provider, model_id) for a Loom Goose model selector value."""
    raw = (model or "").strip()
    if raw.lower().startswith(MODEL_PREFIX):
        raw = raw.split(":", 1)[1]
    _, raw = _strip_selector_mode(raw)
    provider, sep, model_id = raw.partition(":")
    if not sep:
        return "openrouter", provider
    provider = provider.strip().lower()
    if provider in {"dream", "diffusiongemma", "diffusion-gemma"}:
        return "dream", model_id.strip()
    if provider in {"or", "openrouter"}:
        return "openrouter", model_id.strip()
    if provider in {"openai", "custom"}:
        return "openai", model_id.strip()
    if provider in {"llama", "local", "llama-server", "llamaserver"}:
        return "llama", model_id.strip()
    return provider, model_id.strip()


def model_label(model: str | None) -> str:
    provider, model_id = split_goose_model(model)
    return f"goose:{provider}:{model_id}" if model_id else "goose"


def _goose_model_attestation(
    requested_model: str | None,
    effective_model: str | None = None,
    *,
    session_id: str = "",
) -> dict:
    """Describe Goose's model evidence without treating env config as proof."""
    provider, model_id = split_goose_model(requested_model)
    launch_model = f"{provider}:{model_id}" if model_id else provider
    effective = str(effective_model or "").strip()
    effective_label = effective
    if effective_label.lower().startswith("goose:"):
        _, effective_label = effective_label.split(":", 1)
    if effective:
        status = (
            "verified"
            if effective_label.casefold() == launch_model.casefold()
            else "mismatch"
        )
        source = "goose_acp_session_model_state"
        level = "harness"
    else:
        # ACP does not require Goose to expose an effective model. The process
        # environment is exact launch configuration, but not downstream proof.
        status = "configured" if model_id else "unverified"
        source = "goose_process_environment"
        level = "launch_configuration"
    return {
        "status": status,
        "harness": "Goose ACP",
        "requested_model": str(requested_model or "").strip(),
        "launch_model": launch_model or None,
        "effective_model": effective or None,
        "model_provider": provider or None,
        "source": source,
        "verification_level": level,
        "session_id": session_id or None,
        "fallback_allowed": False,
    }


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _dream_openai_base_url() -> str:
    try:
        from config import config as _config

        host = (_config.dream_host or "http://127.0.0.1:8787").strip()
    except Exception:
        host = os.environ.get("DREAM_HOST", "http://127.0.0.1:8787")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host.replace("//localhost", "//127.0.0.1").rstrip("/")


def _llama_openai_base_url() -> str:
    try:
        from config import config as _config

        host = _config.llama_host_url()
    except Exception:
        host = os.environ.get("LLAMA_HOST", "http://127.0.0.1:8000")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host.replace("//localhost", "//127.0.0.1").rstrip("/")


def _llama_context_limit(model_id: str | None) -> int:
    try:
        from config import config as _config

        configured = model_id or getattr(_config, "llama_model", "") or ""
        cfg_path = Path(__file__).resolve().parent / "models_config.json"
        if configured and cfg_path.is_file():
            with cfg_path.open("r", encoding="utf-8") as f:
                model_cfg = json.load(f)
            ctx = model_cfg.get(configured, {}).get("ctx_size")
            if ctx:
                return int(ctx)
        return int(getattr(_config, "max_context_tokens", 32768) or 32768)
    except Exception:
        return int(os.environ.get("LLAMA_CONTEXT_SIZE", "32768"))


def _normalize_reasoning_effort(effort: str | None) -> str | None:
    value = (effort or "").strip().lower().replace("-", "")
    aliases = {
        "extra": "xhigh",
        "extrahigh": "xhigh",
        "x-high": "xhigh",
    }
    value = aliases.get(value, value)
    return value if value in _OPENROUTER_REASONING_EFFORTS else None


def _merge_openrouter_parameters(raw: str | None, effort: str | None) -> str | None:
    normalized = _normalize_reasoning_effort(effort)
    if not normalized:
        return raw
    params: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                params = parsed
        except json.JSONDecodeError:
            params = {}
    reasoning = params.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}
    reasoning["effort"] = normalized
    reasoning.setdefault("enabled", True)
    params["reasoning"] = reasoning
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False)


def _goose_env(model: str | None, permission_mode: str | None = None, effort: str | None = None) -> dict[str, str]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    provider, model_id = split_goose_model(model)
    mode = (permission_mode or os.environ.get("LOOM_GOOSE_MODE") or "approve").strip().lower()
    mode = mode.replace("-", "_")
    if mode in {"default", "on-request", "request"}:
        mode = "approve"
    if mode not in {"smart_approve", "approve", "chat"}:
        mode = "approve"
    env["GOOSE_MODE"] = mode
    if _truthy(os.environ.get("LOOM_GOOSE_DISABLE_KEYRING")):
        env["GOOSE_DISABLE_KEYRING"] = "true"

    if provider == "dream":
        dream_host = _dream_openai_base_url().rstrip("/")
        dream_api_key = os.environ.get("DREAM_GOOSE_API_KEY", "loom-local")
        env["GOOSE_PROVIDER"] = "openai"
        env["GOOSE_MODEL"] = model_id or os.environ.get("DREAM_MODEL", "diffusiongemma")
        env["GOOSE_PROVIDER__TYPE"] = "openai"
        env["GOOSE_PROVIDER__HOST"] = dream_host
        env["GOOSE_PROVIDER__API_KEY"] = dream_api_key
        env["OPENAI_HOST"] = dream_host
        env["OPENAI_BASE_URL"] = dream_host
        env["OPENAI_BASE_PATH"] = os.environ.get("DREAM_GOOSE_OPENAI_BASE_PATH", "v1/chat/completions")
        env["OPENAI_API_KEY"] = dream_api_key
        try:
            from config import config as _config

            env.setdefault("GOOSE_CONTEXT_LIMIT", str(int(getattr(_config, "dream_context_size", 131072) or 131072)))
        except Exception:
            env.setdefault("GOOSE_CONTEXT_LIMIT", os.environ.get("DREAM_CONTEXT_SIZE", "131072"))
    elif provider == "llama":
        llama_host = _llama_openai_base_url().rstrip("/")
        llama_api_key = os.environ.get("LLAMA_GOOSE_API_KEY", "loom-local")
        if not model_id:
            try:
                from config import config as _config

                model_id = getattr(_config, "llama_model", "") or os.environ.get("LLAMA_MODEL", "")
            except Exception:
                model_id = os.environ.get("LLAMA_MODEL", "")
        env["GOOSE_PROVIDER"] = "openai"
        env["GOOSE_MODEL"] = model_id
        env["GOOSE_PROVIDER__TYPE"] = "openai"
        env["GOOSE_PROVIDER__HOST"] = llama_host
        env["GOOSE_PROVIDER__API_KEY"] = llama_api_key
        env["OPENAI_HOST"] = llama_host
        env["OPENAI_BASE_URL"] = llama_host
        env["OPENAI_BASE_PATH"] = os.environ.get("LLAMA_GOOSE_OPENAI_BASE_PATH", "v1/chat/completions")
        env["OPENAI_API_KEY"] = llama_api_key
        env.setdefault("GOOSE_CONTEXT_LIMIT", str(_llama_context_limit(model_id)))
    elif provider == "openai":
        env["GOOSE_PROVIDER"] = "openai"
        env["GOOSE_MODEL"] = model_id
    elif provider == "openrouter":
        env["GOOSE_PROVIDER"] = "openrouter"
        env["GOOSE_MODEL"] = model_id
        merged_params = _merge_openrouter_parameters(env.get("OPENROUTER_PARAMETERS"), effort)
        if merged_params:
            env["OPENROUTER_PARAMETERS"] = merged_params
        try:
            import openrouter_client

            key = openrouter_client.api_key()
            if key:
                env["OPENROUTER_API_KEY"] = key
                env["GOOSE_PROVIDER__API_KEY"] = key
            env.setdefault("OPENROUTER_HOST", _openrouter_host_for_goose(openrouter_client.base_url()))
            env.setdefault("GOOSE_PROVIDER__TYPE", "openrouter")
            env.setdefault("GOOSE_PROVIDER__HOST", env["OPENROUTER_HOST"])
        except Exception:
            pass
    else:
        env["GOOSE_PROVIDER"] = provider
        env["GOOSE_MODEL"] = model_id
    return env


def _openrouter_host_for_goose(base_url: str) -> str:
    raw = (base_url or "https://openrouter.ai").strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    if path.lower().endswith("/api/v1"):
        path = path[: -len("/api/v1")].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _loom_mcp_servers() -> list[dict]:
    web_tools = Path(__file__).resolve().parent / "mcp_web_tools.py"
    if not web_tools.is_file():
        return []
    return [{
        "name": "web-tools",
        "command": sys.executable,
        "args": [str(web_tools)],
        "env": [],
    }]


def _goose_work_dir(cwd: str | None) -> str:
    """ACP requires an absolute cwd in session/new and session/load."""
    candidate = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    return str(Path(candidate).resolve())


def _content_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_content_to_text(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "data", "output", "result"):
            text = _content_to_text(value.get(key))
            if text:
                return text
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except TypeError:
            return str(value)
    return str(value)


def _text_from_value(value) -> str:
    return _content_to_text(value)


def _exception_text(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) and not str(exc).strip():
        return "Goose ACP operation timed out"
    return str(exc).strip() or exc.__class__.__name__


def _with_stderr_tail(message: str, stderr_tail: list[str]) -> str:
    text = (message or "").strip()
    tail = "\n".join(line for line in stderr_tail[-12:] if line.strip()).strip()
    if not text:
        text = "Goose ACP failed"
    if tail:
        return f"{text}\n{tail}"
    return text


def _normalize_update(update: dict) -> tuple[str, dict]:
    kind = (
        update.get("sessionUpdate")
        or update.get("session_update")
        or update.get("type")
        or update.get("kind")
        or update.get("updateType")
        or update.get("update_type")
        or ""
    )
    if not kind and "agentMessageChunk" in update:
        kind = "agentMessageChunk"
        update = update.get("agentMessageChunk") or {}
    elif not kind and "toolCall" in update:
        kind = "toolCall"
        update = update.get("toolCall") or {}
    elif not kind and "toolCallUpdate" in update:
        kind = "toolCallUpdate"
        update = update.get("toolCallUpdate") or {}
    return str(kind), update


def dispatch_session_update(update: dict, state: dict | None = None) -> list[dict]:
    """Map Goose ACP update payloads into Loom event dicts."""
    state = state if state is not None else {}
    kind, update = _normalize_update(update or {})
    events: list[dict] = []
    if kind in {"agent_message_chunk", "agentMessageChunk", "message_delta", "messageDelta"}:
        text = _content_to_text(update.get("content") if isinstance(update, dict) else update)
        if text:
            events.append({"type": "text_delta", "text": text})
    elif kind in {"agent_thought_chunk", "agentThoughtChunk", "thought_delta", "thoughtDelta"}:
        text = _content_to_text(update.get("content") if isinstance(update, dict) else update)
        if text:
            events.append({"type": "thinking_delta", "text": text})
    elif kind in {"tool_call", "toolCall"}:
        tc_id = str(update.get("toolCallId") or update.get("tool_call_id") or update.get("id") or "")
        seen = state.setdefault("tool_calls", set())
        seen.add(tc_id)
        events.append({
            "type": "tool_start",
            "name": update.get("title") or update.get("name") or update.get("kind") or "goose_tool",
            "tool_id": tc_id,
        })
        body = _content_to_text(update.get("content") or update.get("input") or update.get("arguments"))
        if body:
            events.append({"type": "tool_input_delta", "json": body, "tool_id": tc_id})
    elif kind in {"tool_call_update", "toolCallUpdate"}:
        tc_id = str(update.get("toolCallId") or update.get("tool_call_id") or update.get("id") or "")
        seen = state.setdefault("tool_calls", set())
        if tc_id not in seen:
            seen.add(tc_id)
            events.append({
                "type": "tool_start",
                "name": update.get("title") or update.get("name") or update.get("kind") or "goose_tool",
                "tool_id": tc_id,
            })
        status = update.get("status") or update.get("state")
        body = _content_to_text(update.get("content") or update.get("output") or update.get("result"))
        if body or status in (None, "completed", "failed", "error"):
            events.append({
                "type": "tool_result",
                "content": body,
                "tool_id": tc_id,
                "is_error": status in {"failed", "error"},
            })
    elif kind in {"usage_update", "usageUpdate"}:
        events.append({
            "type": "usage",
            "input_tokens": update.get("inputTokens") or update.get("input_tokens") or update.get("promptTokens") or 0,
            "output_tokens": update.get("outputTokens") or update.get("output_tokens") or update.get("completionTokens") or 0,
        })
    elif kind:
        events.append({"type": "goose_raw_update", "kind": kind, "data": update})
    return events


def _extract_notification_update(msg: dict) -> dict | None:
    params = msg.get("params") or {}
    method = msg.get("method")
    if method == "session/update":
        return params.get("update") or params
    if method in {"session/notification", "sessionNotification"}:
        return params.get("update") or params.get("notification") or params
    return None


def _pick_option(options: list, needle: str) -> str | None:
    for opt in options or []:
        oid = str(opt.get("id") or opt.get("optionId") or opt.get("option_id") or "")
        label = str(opt.get("label") or opt.get("name") or "").lower()
        if needle in oid.lower() or needle.replace("_", " ") in label:
            return oid
    return None


async def _bridge_permission(
    rpc: _RpcConn,
    req_id: Any,
    params: dict,
    conv_id: int,
    loom_port: int,
    permission_scope: str = "",
) -> None:
    tool_call = params.get("toolCall") or params.get("tool_call") or params.get("tool") or {}
    tool_name = (
        tool_call.get("title")
        or tool_call.get("name")
        or params.get("title")
        or params.get("name")
        or "GooseTool"
    )
    body = {
        "loom_conv_id": conv_id,
        "tool_name": tool_name,
        "tool_input": {"toolCall": tool_call, "params": params},
        "hook_event_name": "PreToolUse",
    }
    if permission_scope:
        body["permission_scope"] = permission_scope
    certs_dir = Path(__file__).parent / "certs"
    protocol = "https" if (certs_dir / "cert.pem").exists() and (certs_dir / "key.pem").exists() else "http"
    allow = False
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(
                f"{protocol}://127.0.0.1:{loom_port}/api/cc-permission",
                json=body,
                timeout=_PERMISSION_HTTP_TIMEOUT,
            )
            payload = resp.json()
        allow = bool(payload.get("allow"))
    except Exception as exc:  # noqa: BLE001
        log.warning("[Goose] permission bridge failed for conv %s: %s", conv_id, exc)

    options = params.get("options") or []
    if allow:
        opt_id = _pick_option(options, "allow_once") or _pick_option(options, "allow")
    else:
        opt_id = _pick_option(options, "reject_once") or _pick_option(options, "deny") or _pick_option(options, "reject")
    result = {"outcome": {"outcome": "selected", "optionId": opt_id}} if opt_id else {"outcome": {"outcome": "cancelled"}}
    try:
        await rpc.respond(req_id, result=result)
    except Exception as exc:  # noqa: BLE001
        log.warning("[Goose] permission reply failed for conv %s: %s", conv_id, exc)


def _prepare_goose_prompt(prompt: str, *, nrol_operator: bool = False) -> str:
    if nrol_operator:
        return prompt
    return prepend_loom_agent_context(prompt, "goose")


def _image_prompt_block(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        return None
    try:
        data = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        return None
    block: dict[str, Any] = {
        "type": "image",
        "data": data,
        "mimeType": mime,
    }
    try:
        block["uri"] = p.resolve().as_uri()
    except ValueError:
        pass
    return block


def _prompt_blocks(prompt: str, image_paths: list[str | Path] | None = None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_path in image_paths or []:
        block = _image_prompt_block(image_path)
        if block:
            blocks.append(block)
    return blocks


async def _rpc_request_via_reader(
    rpc: _RpcConn,
    method: str,
    params: dict,
    proc: asyncio.subprocess.Process,
    state: dict,
    timeout: float = 60.0,
) -> Any:
    rid = rpc._alloc_id()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    rpc._pending[rid] = fut
    await rpc._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    deadline = asyncio.get_event_loop().time() + timeout
    assert proc.stdout is not None
    while not fut.done():
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            rpc._pending.pop(rid, None)
            raise asyncio.TimeoutError(f"goose {method} timed out")
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            rpc._pending.pop(rid, None)
            raise asyncio.TimeoutError(f"goose {method} timed out after {timeout:g}s") from exc
        if not raw:
            rpc._pending.pop(rid, None)
            raise RuntimeError(f"goose acp closed stdout during {method}")
        line = raw.rstrip(b"\r\n")
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.warning("[Goose] non-JSON stdout during %s: %r", method, line[:200])
            continue
        if rpc.resolve_response(msg):
            continue
        meth = msg.get("method")
        mid = msg.get("id")
        if meth and mid is not None:
            await rpc.respond(mid, result={})
            continue
        update = _extract_notification_update(msg)
        if update:
            dispatch_session_update(update, state)
    rpc._pending.pop(rid, None)
    return fut.result()


async def run_goose(
    prompt: str,
    *,
    conv_id: int = 0,
    model: str | None = None,
    cwd: str = ".",
    loom_port: int = 3000,
    goose_exe: str | None = None,
    permission_mode: str = "default",
    branch: list[dict] | None = None,
    resume_session_id: str | None = None,
    fork_session: bool = False,
    nrol_operator: bool = False,
    builtins: str | list[str] | None = None,
    effort: str | None = None,
    image_paths: list[str | Path] | None = None,
) -> tuple[asyncio.subprocess.Process, AsyncGenerator[dict, None]]:
    del branch  # reserved for parity; server owns history rendering.
    exe = goose_exe or default_goose_exe()
    builtin_names = builtins if builtins is not None else os.environ.get("LOOM_GOOSE_BUILTINS", "developer")
    if isinstance(builtin_names, str):
        builtin_list = [b.strip() for b in builtin_names.split(",") if b.strip()]
    else:
        builtin_list = [str(b).strip() for b in builtin_names if str(b).strip()]
    cmd = _goose_command(exe) + ["acp"]
    if builtin_list:
        cmd.extend(["--with-builtin", ",".join(builtin_list)])

    work_dir = _goose_work_dir(cwd)
    acp_cwd = work_dir.replace("\\", "/")
    env = _goose_env(model, permission_mode, effort)
    prompt = _prepare_goose_prompt(prompt, nrol_operator=nrol_operator)

    kwargs = {}
    if sys.platform == "win32":
        import subprocess

        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=work_dir,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_STREAM_LIMIT,
        **kwargs,
    )

    stderr_tail: list[str] = []

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                stderr_tail.append(text)
                del stderr_tail[:-40]
                print(f"[Goose-stderr] {text}")

    asyncio.create_task(_drain_stderr())
    rpc = _RpcConn(proc)
    state: dict = {}
    pending_tasks: set[asyncio.Task] = set()
    gen_key = getattr(asyncio.current_task(), "_gen_key", None)
    permission_scope = f"gen:{gen_key[2]}" if gen_key else ""

    def _spawn_bridge(req_id: Any, params: dict) -> None:
        task = asyncio.create_task(
            _bridge_permission(rpc, req_id, params, conv_id, loom_port, permission_scope)
        )
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)

    async def _event_stream() -> AsyncGenerator[dict, None]:
        import time as _time

        t0 = _time.time()
        session_id = ""
        models_info: dict = {}
        prompt_fut: asyncio.Future | None = None
        prompt_req_id: int | None = None
        finished = False
        try:
            init_result = await _rpc_request_via_reader(
                rpc,
                "initialize",
                {
                    "protocolVersion": "v1",
                    "clientCapabilities": {},
                    "clientInfo": {"name": "loom", "version": "1"},
                },
                proc,
                state,
            )
            yield {"type": "status", "text": f"Goose ACP connected: {_content_to_text((init_result or {}).get('agentInfo') or {}).strip() or 'ready'}"}

            if resume_session_id:
                try:
                    method = "session/fork" if fork_session else "session/load"
                    params = {"sessionId": resume_session_id, "cwd": acp_cwd}
                    res = await _rpc_request_via_reader(rpc, method, params, proc, state)
                    session_id = (res or {}).get("sessionId") or (res or {}).get("session_id") or resume_session_id
                    models_info = (res or {}).get("models") or {}
                except Exception as exc:
                    try:
                        await cancel_goose(proc)
                    except Exception:
                        pass
                    detail = _with_stderr_tail(_exception_text(exc), stderr_tail)
                    raise RuntimeError(f"Goose session resume/fork failed: {detail}") from exc

            if not session_id:
                res = await _rpc_request_via_reader(
                    rpc,
                    "session/new",
                    {"cwd": acp_cwd, "mcpServers": _loom_mcp_servers()},
                    proc,
                    state,
                )
                session_id = (res or {}).get("sessionId") or (res or {}).get("session_id") or ""
                models_info = (res or {}).get("models") or {}

            effective_model = (
                models_info.get("currentModelId")
                or models_info.get("current_model_id")
                or ""
            )
            attestation = _goose_model_attestation(
                model,
                effective_model,
                session_id=session_id,
            )
            yield {
                "type": "session_info",
                "session_id": session_id,
                "requested_model": model,
                "model": effective_model or model_label(model),
                "model_confirmed": bool(effective_model),
                "model_attestation": attestation,
            }

            prompt_req_id = rpc._alloc_id()
            prompt_fut = asyncio.get_event_loop().create_future()
            rpc._pending[prompt_req_id] = prompt_fut
            await rpc._write({
                "jsonrpc": "2.0",
                "id": prompt_req_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": _prompt_blocks(prompt, image_paths),
                },
            })

            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.rstrip(b"\r\n")
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("[Goose] non-JSON stdout: %r", line[:200])
                    continue

                if rpc.resolve_response(msg):
                    if prompt_fut.done():
                        prompt_failed = False
                        try:
                            result = prompt_fut.result()
                        except Exception as exc:  # noqa: BLE001
                            detail = _with_stderr_tail(_exception_text(exc), stderr_tail)
                            yield {"type": "error", "error": f"Goose prompt failed: {detail}"}
                            result = {}
                            prompt_failed = True
                        usage = (result or {}).get("usage") or {}
                        if usage:
                            yield {
                                "type": "usage",
                                "input_tokens": usage.get("inputTokens", usage.get("input_tokens", 0)),
                                "output_tokens": usage.get("outputTokens", usage.get("output_tokens", 0)),
                            }
                        yield {
                            "type": "result",
                            "session_id": session_id,
                            "stop_reason": (result or {}).get("stopReason") or (result or {}).get("stop_reason") or ("error" if prompt_failed else "end_turn"),
                            "duration_ms": int((_time.time() - t0) * 1000),
                            "num_turns": 1,
                        }
                        finished = True
                        break
                    continue

                method = msg.get("method")
                mid = msg.get("id")
                if method and mid is not None:
                    if method in {"session/request_permission", "session/requestPermission", "requestPermission"}:
                        params = msg.get("params") or {}
                        tool_call = params.get("toolCall") or params.get("tool_call") or params
                        yield {
                            "type": "permission_request",
                            "request_id": mid,
                            "tool_name": tool_call.get("title") or tool_call.get("name") or "GooseTool",
                            "tool_input": tool_call,
                        }
                        _spawn_bridge(mid, params)
                    elif method.startswith("fs/") or method.startswith("terminal/"):
                        await rpc.respond(mid, error={"code": -32601, "message": "not supported by Loom client"})
                    else:
                        await rpc.respond(mid, result={})
                    continue

                update = _extract_notification_update(msg)
                if update:
                    for evt in dispatch_session_update(update, state):
                        yield evt

            if not finished:
                yield {
                    "type": "error",
                    "error": _with_stderr_tail("Goose ACP exited before completing the turn", stderr_tail),
                }
                yield {
                    "type": "result",
                    "session_id": session_id,
                    "stop_reason": "incomplete",
                    "duration_ms": int((_time.time() - t0) * 1000),
                    "num_turns": 1,
                }
        except Exception as exc:  # noqa: BLE001
            yield {
                "type": "error",
                "error": _with_stderr_tail(_exception_text(exc), stderr_tail),
            }
            yield {
                "type": "result",
                "session_id": session_id,
                "stop_reason": "error",
                "duration_ms": int((_time.time() - t0) * 1000),
                "num_turns": 1,
            }
        finally:
            if prompt_req_id is not None:
                rpc._pending.pop(prompt_req_id, None)
            for task in list(pending_tasks):
                task.cancel()
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass

    return proc, _event_stream()


async def cancel_goose(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        import subprocess

        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5)
        except Exception:
            proc.kill()
    else:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()


def diagnostics(goose_exe: str | None = None) -> dict[str, Any]:
    exe = goose_exe or default_goose_exe()
    cmd = _goose_command(exe)
    first = cmd[0]
    path = shutil.which(first) or (first if Path(first).exists() else None)
    return {
        "exe": exe,
        "command": cmd,
        "resolved": path,
        "on_path": bool(shutil.which("goose") or shutil.which("goose.exe") or shutil.which("goose.cmd")),
    }
