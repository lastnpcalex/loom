"""Async HTTP client for the Dream Hermes sidecar (nuspy DiffusionGemma OpenAI adapter).

Talks to the nuspy server's OpenAI-compatible endpoint (/v1/chat/completions,
/v1/models, /health). The sidecar JIT-loads the NVFP4 DiffusionGemma GGUF into
VRAM on first request and reports per-message tok/s telemetry in usage.timings.

This client is deliberately self-contained — no Loom config import — so it can
run from the host OR from inside the Dream Hermes container (pointing at
host.docker.internal:8787). Loom code passes an explicit host; the container
sets DREAM_HOST=http://host.docker.internal:8787.

The diffusion model generates text in 256-token "canvas" blocks via iterative
denoising, NOT token-by-token. Streaming semantics therefore differ from a
standard autoregressive LLM: the nuspy adapter emits canvas-commit deltas
(reasoning_content + content) rather than per-token deltas. dream_chat() yields
strings in either case; callers that want the structured timings should use
dream_chat_sync() which returns the full usage dict.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import AsyncGenerator, Optional

import httpx

# Default host = the sidecar on localhost. Override via DREAM_HOST env or per-call.
DEFAULT_HOST = "http://127.0.0.1:8787"
# The nuspy adapter uses 256-token canvases; default to 8 blocks (~2048 tokens)
# when the caller doesn't specify max_tokens, matching the adapter's own default.
DEFAULT_MAX_TOKENS = 2048
CANVAS = 256


def _envbool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_enable_thinking() -> bool:
    return _envbool("DREAM_ENABLE_THINKING", True)
REQUEST_TIMEOUT = 600.0  # 10 min — cold loads take a while

_DREAM_THOUGHT_BLOCK_RE = re.compile(
    r"<\|channel>thought\s*(.*?)<channel\|>",
    re.IGNORECASE | re.DOTALL,
)


def _split_channel_scaffold(text: str) -> tuple[str, str]:
    """Return (reasoning, content), extracting Dream thought-channel markup."""
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


def _resolve_host(host: Optional[str]) -> str:
    """Pick the host: explicit arg > DREAM_HOST env > default."""
    if host:
        h = host.strip()
    else:
        import os
        h = os.getenv("DREAM_HOST", DEFAULT_HOST).strip()
    if h and not h.startswith(("http://", "https://")):
        h = f"http://{h}"
    return h or DEFAULT_HOST


def _build_messages(messages: list[dict]) -> list[dict]:
    """Flatten internal message format to the {role, content} the adapter expects.
    Strips image blocks (the diffusion text path is text-only; vision tower is
    dropped by the converter). Mirrors llama_client._build_messages minus images."""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            # Multi-part content (text + image blocks) — keep text only.
            parts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            content = "\n".join(parts)
        if content is None:
            content = ""
        out.append({"role": msg.get("role", "user"), "content": str(content)})
    return out


def _blocks_for(max_tokens: Optional[int]) -> int:
    """Map max_tokens to diffusion canvas blocks (256 tokens each), mirroring the
    nuspy adapter's _blocks_for. None/<=0 -> 8 blocks (~2048 tokens)."""
    if not max_tokens or max_tokens <= 0:
        return 8
    return max(1, min(64, -(-max_tokens // CANVAS)))  # ceil division, capped at 64


async def health(host: Optional[str] = None, timeout: float = 3.0) -> Optional[dict]:
    """Probe the dream endpoint. Returns normalized status or None if down.

    The dedicated llama.cpp DiffusionGemma server returns only {"status":"ok"}
    from /health, so fill in model details from /v1/models when needed.
    """
    h = _resolve_host(host)
    models: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{h}/health")
            if r.status_code == 200:
                try:
                    d = r.json()
                    if isinstance(d, dict) and (d.get("available") or d.get("loaded_model")):
                        return d
                except Exception:
                    d = {"status": "ok"}
                mr = await client.get(f"{h}/v1/models")
                if mr.status_code == 200:
                    models = mr.json().get("data", [])
    except Exception:
        models = await list_models(h, timeout=timeout)
    if not models:
        models = await list_models(h, timeout=timeout)
    if models:
        ids = [str(m.get("id") or m.get("name") or "") for m in models if isinstance(m, dict)]
        ids = [m for m in ids if m]
        ctx_lengths = [
            int(m.get("context_length") or m.get("max_model_len") or 0)
            for m in models
            if isinstance(m, dict)
        ]
        slot_ctx = 0
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                sr = await client.get(f"{h}/slots")
                if sr.status_code == 200:
                    slots = sr.json()
                    slot_ctx = max([int(s.get("n_ctx") or 0) for s in slots if isinstance(s, dict)] or [0])
        except Exception:
            pass
        return {
            "status": "ok",
            "loaded_model": ids[0] if len(ids) == 1 else None,
            "available": ids,
            "maxtok": slot_ctx or max(ctx_lengths or [0]),
        }
    return None


async def list_models(host: Optional[str] = None, timeout: float = 5.0) -> list[dict]:
    """GET /v1/models — returns the list of available GGUF model ids with context_length.

    The nuspy adapter lists every .gguf in models/ (loaded or not), so this works
    even before any model is JIT-loaded into VRAM.
    """
    h = _resolve_host(host)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{h}/v1/models")
            if r.status_code == 200:
                return r.json().get("data", [])
    except Exception:
        pass
    return []


async def dream_chat(
    messages: list[dict],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    enable_thinking: Optional[bool] = None,
    host: Optional[str] = None,
    timeout: float = REQUEST_TIMEOUT,
) -> AsyncGenerator[str, None]:
    """Stream a chat completion from the dream sidecar. Yields content strings
    (the final answer text, NOT the reasoning channel).

    Diffusion generates in 256-token canvas blocks, so deltas arrive in bursts
    (one per denoising commit) rather than per-token. If the adapter doesn't
    support streaming for a given request, this falls back to non-streaming
    and yields the whole content at once.
    """
    h = _resolve_host(host)
    payload = {
        "messages": _build_messages(messages),
        "stream": True,
    }
    if model:
        payload["model"] = model
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    payload["chat_template_kwargs"] = {
        "enable_thinking": _default_enable_thinking() if enable_thinking is None else bool(enable_thinking)
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{h}/v1/chat/completions",
                                     json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"dream chat HTTP {resp.status_code}: {body.decode('utf-8', 'replace')[:300]}"
                    )
                sent_final = ""
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk:
                        raise RuntimeError(f"dream server error: {chunk['error']}")
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    # The adapter streams reasoning_content + content separately;
                    # yield only the final answer content (callers wanting
                    # reasoning should use dream_chat_sync).
                    piece = delta.get("content") or ""
                    piece = _strip_channel_scaffold(piece)
                    if piece:
                        sent_final += piece
                        yield piece
        return
    except httpx.ConnectError:
        # Sidecar down — fall through to non-streaming mock? No: surface the
        # error. Callers (dream_hermes orchestrator) handle downtime.
        raise RuntimeError(f"dream sidecar not reachable at {h} (is it running?)")


async def dream_chat_sync(
    messages: list[dict],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    enable_thinking: Optional[bool] = None,
    host: Optional[str] = None,
    timeout: float = REQUEST_TIMEOUT,
) -> dict:
    """Non-streaming chat completion. Returns the full response dict:

        {content, reasoning_content, finish_reason, usage: {prompt_tokens,
         completion_tokens, timings: {tokens_per_second, decode_ms,
         denoising_steps, blocks, n_ctx}}}

    The usage.timings.tokens_per_second is the steady-state tok/s the model
    reports for that request — this is the number dream_bench() reports.
    """
    h = _resolve_host(host)
    payload = {
        "messages": _build_messages(messages),
        "stream": False,
    }
    if model:
        payload["model"] = model
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    payload["chat_template_kwargs"] = {
        "enable_thinking": _default_enable_thinking() if enable_thinking is None else bool(enable_thinking)
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{h}/v1/chat/completions", json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"dream chat HTTP {r.status_code}: {r.text[:400]}")
        d = r.json()
    choice = (d.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    # The nuspy adapter may split reasoning into reasoning_content, or leak it
    # as <|channel>thought...<channel|> inside content. Normalize both shapes.
    extracted_reasoning, content = _split_channel_scaffold(msg.get("content") or "")
    reasoning = msg.get("reasoning_content") or ""
    if extracted_reasoning:
        reasoning = (reasoning.rstrip() + "\n\n" + extracted_reasoning).strip() if reasoning else extracted_reasoning
    return {
        "content": content,
        "reasoning_content": reasoning,
        "finish_reason": choice.get("finish_reason"),
        "usage": d.get("usage") or {},
        "timings": d.get("timings") or {},
    }


async def dream_bench(host: Optional[str] = None, *, warm: bool = False, model: Optional[str] = None) -> dict:
    """Benchmark the dream sidecar: measure TTFT + tok/s for a short generation.

    If warm=False (default) and the model is NOT yet loaded, this triggers the
    cold JIT load (~17GB into VRAM, ~seconds) — so the first call measures
    cold TTFT. Pass warm=True to skip the cold-load measurement (assumes the
    model is already resident from a prior request).

    Returns: {cold_load, wall_s, ttft_s, tok_s, completion_tokens,
              denoising_steps, decode_ms, prompt_tokens, loaded_before,
              loaded_after}
    """
    h = _resolve_host(host)
    before = await health(h)
    loaded_before = bool(before and before.get("loaded_model"))
    cold_load = (not loaded_before) and (not warm)

    prompt = "Write exactly one sentence about the ocean."
    t0 = time.perf_counter()
    res = await dream_chat_sync(
        [{"role": "user", "content": prompt}],
        model=model,
        max_tokens=512,
        host=h,
    )
    wall_s = time.perf_counter() - t0

    after = await health(h)
    timings = (res.get("usage") or {}).get("timings") or res.get("timings") or {}
    completion_tokens = (res.get("usage") or {}).get("completion_tokens", 0)
    tok_s = timings.get("tokens_per_second") or timings.get("predicted_per_second") or 0.0
    if not tok_s and completion_tokens and wall_s > 0:
        tok_s = completion_tokens / wall_s
    return {
        "cold_load": cold_load,
        "wall_s": round(wall_s, 2),
        "ttft_s": round(wall_s, 2),  # non-streaming: TTFT ≈ wall (one canvas burst)
        "tok_s": tok_s,
        "completion_tokens": completion_tokens,
        "denoising_steps": timings.get("denoising_steps", 0),
        "decode_ms": timings.get("decode_ms", 0),
        "prompt_tokens": (res.get("usage") or {}).get("prompt_tokens", 0),
        "loaded_before": loaded_before,
        "loaded_after": bool(after and after.get("loaded_model")),
        "content_preview": res["content"][:120],
    }


# ── CLI (for the GO/NO-GO gate + manual smoke) ──────────────────────────────

async def _cli():
    import argparse, os
    ap = argparse.ArgumentParser(description="Dream Hermes sidecar client + bench")
    ap.add_argument("cmd", choices=["bench", "chat", "health", "models"])
    ap.add_argument("--host", default=os.getenv("DREAM_HOST", DEFAULT_HOST))
    ap.add_argument("--prompt", default="Say hello in one short sentence.")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--model", default=os.getenv("DREAM_MODEL", ""))
    ap.add_argument("--warm", action="store_true", help="bench: assume model already loaded")
    args = ap.parse_args()

    if args.cmd == "health":
        h = await health(args.host)
        print(json.dumps(h, indent=2) if h else "sidecar down")
    elif args.cmd == "models":
        ms = await list_models(args.host)
        print(json.dumps(ms, indent=2))
    elif args.cmd == "chat":
        res = await dream_chat_sync(
            [{"role": "user", "content": args.prompt}],
            model=args.model or None,
            max_tokens=args.max_tokens,
            host=args.host,
        )
        print("CONTENT:", res["content"])
        if res["reasoning_content"]:
            print("REASONING:", res["reasoning_content"][:200])
        print("USAGE:", json.dumps(res["usage"], indent=2))
    elif args.cmd == "bench":
        print(f"Running bench against {args.host} (warm={args.warm})...")
        r = await dream_bench(args.host, warm=args.warm, model=args.model or None)
        print(json.dumps(r, indent=2))
        # GO/NO-GO gate
        if r["tok_s"] >= 5 and r["ttft_s"] < 30:
            print("\nGO: tok/s >= 5 and TTFT < 30s - CPU/GPU orchestrator viable.")
        elif r["tok_s"] > 0:
            print(f"\nMARGINAL: tok/s={r['tok_s']} ttft={r['ttft_s']}s - review thresholds.")
        else:
            print("\nNO-GO: no tok/s reported - sidecar error?")


if __name__ == "__main__":
    asyncio.run(_cli())
