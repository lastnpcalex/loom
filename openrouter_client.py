"""OpenRouter helpers for remote Loom/Weave models.

Keys are read from process env or the repo-local .env file. They are never
persisted through config.json because config.json is user-editable UI state, not
secret storage.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from config import config


MODEL_PREFIX = "openrouter:"
DEFAULT_MODELS = [
    {
        "name": f"{MODEL_PREFIX}z-ai/glm-5.2",
        "label": "OpenRouter GLM 5.2",
        "backend": "openrouter",
        "context_length": 1_000_000,
    },
    {
        "name": f"{MODEL_PREFIX}moonshotai/kimi-k2.7-code",
        "label": "OpenRouter Kimi K2.7 Code",
        "backend": "openrouter",
        "context_length": 262_144,
    },
    {
        "name": f"{MODEL_PREFIX}openai/gpt-5.6-luna",
        "label": "OpenRouter GPT 5.6 Luna",
        "backend": "openrouter",
        "context_length": 1_050_000,
    },
    {
        "name": f"{MODEL_PREFIX}deepseek/deepseek-v4-flash-0731",
        "label": "OpenRouter DeepSeek V4 Flash 0731",
        "backend": "openrouter",
        "context_length": 1_048_576,
    },
]
_KNOWN_MODEL_SLUGS = {
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "openai/gpt-5.6-luna",
    "deepseek/deepseek-v4-flash-0731",
}
_SECRET_NAMES = {
    "OPENROUTER_API_KEY",
    "OPENROUTER_MANAGEMENT_KEY",
}
_KEY_STATUS_CACHE_TTL_SEC = 60.0
_key_status_cache: dict[str, Any] = {
    "token": "",
    "fetched_at": 0.0,
    "status": None,
}


class OpenRouterBudgetError(RuntimeError):
    """Raised when a request would violate the configured spend guardrail."""


def is_openrouter_model(model: str | None) -> bool:
    value = (model or "").strip().lower()
    if not value:
        return False
    if value.startswith(MODEL_PREFIX):
        return True
    return value in _KNOWN_MODEL_SLUGS


def model_slug(model: str | None) -> str:
    value = (model or "").strip()
    if value.lower().startswith(MODEL_PREFIX):
        return value.split(":", 1)[1].strip()
    return value


def base_url() -> str:
    raw = (
        getattr(config, "openrouter_base_url", "")
        or os.getenv("OPENROUTER_BASE_URL", "")
        or "https://openrouter.ai/api/v1"
    ).strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw.rstrip("/")


def _dotenv_path() -> Path:
    return Path(__file__).parent / ".env"


def _dotenv_values() -> dict[str, str]:
    path = _dotenv_path()
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            out[key.strip()] = value
    except OSError:
        return {}
    return out


def _read_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return _dotenv_values().get(name, "").strip()


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return "configured"
    return f"{value[:7]}...{value[-4:]}"


def _secret_source(name: str) -> str | None:
    if os.environ.get(name, "").strip():
        return "environment"
    if _dotenv_values().get(name, "").strip():
        return ".env"
    return None


def secret_status() -> dict[str, Any]:
    dotenv = _dotenv_values()
    status: dict[str, Any] = {"dotenv_path": str(_dotenv_path())}
    for public_name, env_name in (
        ("api_key", "OPENROUTER_API_KEY"),
        ("management_key", "OPENROUTER_MANAGEMENT_KEY"),
    ):
        value = _read_secret(env_name)
        status[public_name] = {
            "configured": bool(value),
            "source": _secret_source(env_name),
            "preview": _mask_secret(value),
            "env_overrides_dotenv": bool(os.environ.get(env_name, "").strip())
            and bool(dotenv.get(env_name, "").strip()),
        }
    return status


def _validate_secret_value(name: str, value: str) -> str:
    if name not in _SECRET_NAMES:
        raise ValueError(f"Unsupported secret name: {name}")
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{name} is empty")
    if any(ch in value for ch in ("\r", "\n", "\0")):
        raise ValueError(f"{name} contains an invalid character")
    return value


def write_dotenv_secrets(
    updates: dict[str, str] | None = None,
    clear_names: set[str] | None = None,
) -> dict[str, Any]:
    """Write OpenRouter secrets to repo .env without exposing existing values."""
    updates = updates or {}
    clear_names = clear_names or set()
    sanitized = {
        name: _validate_secret_value(name, value)
        for name, value in updates.items()
    }
    for name in clear_names:
        if name not in _SECRET_NAMES:
            raise ValueError(f"Unsupported secret name: {name}")
        if name in sanitized:
            raise ValueError(f"Cannot set and clear {name} in the same request")

    path = _dotenv_path()
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    pending = dict(sanitized)
    output: list[str] = []
    seen: set[str] = set()
    written: set[str] = set()
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in _SECRET_NAMES and not stripped.startswith("#"):
            seen.add(key)
            if key in clear_names:
                continue
            if key in sanitized:
                if key not in written:
                    output.append(f"{key}={sanitized[key]}")
                    written.add(key)
                    pending.pop(key, None)
                continue
        output.append(line)

    if pending and output and output[-1].strip():
        output.append("")
    for key, value in pending.items():
        if key not in seen:
            output.append(f"{key}={value}")

    path.write_text("\n".join(output).rstrip() + ("\n" if output else ""), encoding="utf-8")
    return {
        "dotenv_path": str(path),
        "updated": sorted(sanitized),
        "cleared": sorted(clear_names),
        "status": secret_status(),
    }


def api_key() -> str:
    return _read_secret("OPENROUTER_API_KEY")


def management_key() -> str:
    return _read_secret("OPENROUTER_MANAGEMENT_KEY")


def request_headers(key: str | None = None) -> dict[str, str]:
    token = (key or api_key()).strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost:3000",
        "X-Title": "A Shadow Loom",
    }
    return headers


def public_models() -> list[dict[str, Any]]:
    return [dict(m) for m in DEFAULT_MODELS]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _limit(name: str, default: float) -> float:
    return _float(getattr(config, name, default), default)


def budget_limits() -> dict[str, float]:
    return {
        "weekly_limit_usd": _limit("openrouter_weekly_limit_usd", 12.5),
        "monthly_limit_usd": _limit("openrouter_monthly_limit_usd", 50.0),
        "max_prompt_price_per_mtok": _limit(
            "openrouter_max_prompt_price_per_mtok", 1.0
        ),
        "max_completion_price_per_mtok": _limit(
            "openrouter_max_completion_price_per_mtok", 4.0
        ),
    }


def estimate_request_cost(
    messages: list[dict[str, Any]],
    max_tokens: int | None,
) -> float:
    """Conservative preflight estimate based on configured route price caps."""
    prompt_chars = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            prompt_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    prompt_chars += len(block.get("text") or "")
    prompt_tokens = max(0, prompt_chars // 3)
    completion_tokens = max(0, int(max_tokens or getattr(config, "max_tokens", 0) or 0))
    limits = budget_limits()
    return (
        prompt_tokens * limits["max_prompt_price_per_mtok"] / 1_000_000
        + completion_tokens * limits["max_completion_price_per_mtok"] / 1_000_000
    )


def usage_from_openai_payload(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    out = {
        "input_tokens": int(_float(usage.get("prompt_tokens"), 0)),
        "output_tokens": int(_float(usage.get("completion_tokens"), 0)),
        "total_tokens": int(_float(usage.get("total_tokens"), 0)),
        "cost_usd": _float(usage.get("cost_usd", usage.get("cost")), 0.0),
    }
    details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = int(_float(details.get("reasoning_tokens"), 0))
    if reasoning_tokens:
        out["reasoning_tokens"] = reasoning_tokens
    prompt_details = usage.get("prompt_tokens_details") or {}
    cached_tokens = int(_float(prompt_details.get("cached_tokens"), 0))
    if cached_tokens:
        out["cached_tokens"] = cached_tokens
    return out


async def current_key_status(
    key: str | None = None,
    *,
    use_cache: bool = False,
) -> dict[str, Any]:
    token = (key or api_key()).strip()
    if not token:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    now = time.time()
    if (
        use_cache
        and _key_status_cache.get("token") == token
        and isinstance(_key_status_cache.get("status"), dict)
        and now - float(_key_status_cache.get("fetched_at") or 0) <= _KEY_STATUS_CACHE_TTL_SEC
    ):
        return dict(_key_status_cache["status"])
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        resp = await client.get(f"{base_url()}/key", headers=request_headers(token))
        if resp.status_code >= 400:
            raise RuntimeError(_safe_error(resp, "OpenRouter key status failed"))
        status = resp.json().get("data") or {}
        _key_status_cache.update({
            "token": token,
            "fetched_at": now,
            "status": dict(status),
        })
        return status


async def account_credits(key: str | None = None) -> dict[str, Any]:
    token = (key or management_key()).strip()
    if not token:
        raise RuntimeError("OPENROUTER_MANAGEMENT_KEY is not set")
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        resp = await client.get(
            f"{base_url()}/credits",
            headers=request_headers(token),
        )
        if resp.status_code >= 400:
            raise RuntimeError(_safe_error(resp, "OpenRouter credits lookup failed"))
        return resp.json().get("data") or {}


async def usage_snapshot() -> dict[str, Any]:
    key_configured = bool(api_key())
    mgmt_configured = bool(management_key())
    payload: dict[str, Any] = {
        "api_key_configured": key_configured,
        "management_key_configured": mgmt_configured,
        "secrets": secret_status(),
        "base_url": base_url(),
        "limits": budget_limits(),
        "models": public_models(),
    }
    tasks = []
    if key_configured:
        tasks.append(("current_key", current_key_status()))
    if mgmt_configured:
        tasks.append(("account", account_credits()))
    if tasks:
        results = await asyncio.gather(
            *(task for _, task in tasks),
            return_exceptions=True,
        )
        for (name, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                payload[f"{name}_error"] = str(result)
            else:
                payload[name] = result
    return payload


async def ensure_budget_available(
    *,
    projected_cost_usd: float = 0.0,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status is None:
        status = await current_key_status(use_cache=True)
    limits = budget_limits()
    weekly = _float(status.get("usage_weekly"), 0.0)
    monthly = _float(status.get("usage_monthly"), 0.0)
    projected = max(0.0, projected_cost_usd)
    weekly_limit = limits["weekly_limit_usd"]
    monthly_limit = limits["monthly_limit_usd"]
    if weekly_limit > 0 and weekly + projected >= weekly_limit:
        raise OpenRouterBudgetError(
            f"OpenRouter weekly budget reached: ${weekly:.2f} used, "
            f"${weekly_limit:.2f} limit"
        )
    if monthly_limit > 0 and monthly + projected >= monthly_limit:
        raise OpenRouterBudgetError(
            f"OpenRouter monthly budget reached: ${monthly:.2f} used, "
            f"${monthly_limit:.2f} limit"
        )
    return status


def provider_price_guard() -> dict[str, Any]:
    limits = budget_limits()
    prompt = limits["max_prompt_price_per_mtok"]
    completion = limits["max_completion_price_per_mtok"]
    if prompt <= 0 and completion <= 0:
        return {}
    max_price: dict[str, float] = {}
    if prompt > 0:
        max_price["prompt"] = prompt
    if completion > 0:
        max_price["completion"] = completion
    return {"provider": {"max_price": max_price}}


async def create_limited_key(
    *,
    name: str = "A Shadow Loom",
    limit: float = 12.5,
    limit_reset: str = "weekly",
    include_byok_in_limit: bool = True,
) -> dict[str, Any]:
    token = management_key()
    if not token:
        raise RuntimeError("OPENROUTER_MANAGEMENT_KEY is not set")
    body = {
        "name": name,
        "limit": float(limit),
        "limit_reset": limit_reset,
        "include_byok_in_limit": bool(include_byok_in_limit),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        resp = await client.post(
            f"{base_url()}/keys",
            headers=request_headers(token),
            json=body,
        )
        if resp.status_code >= 400:
            raise RuntimeError(_safe_error(resp, "OpenRouter key creation failed"))
        return resp.json()


def _safe_error(resp: httpx.Response, fallback: str) -> str:
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return f"{fallback}: HTTP {resp.status_code}"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code")
    else:
        msg = err
    return f"{fallback}: {msg or 'HTTP ' + str(resp.status_code)}"
