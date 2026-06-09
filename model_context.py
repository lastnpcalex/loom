"""Model context-window table + handoff gate.

Used by the generation pipeline to decide, for a given target model and branch
size, whether we need to run a pre-generation compact-and-handoff before
letting the target model see the branch.

1M-context Anthropic models always skip the gate. Everything else (standard
Anthropic, Gemini, local Ollama, vLLM) trips the gate if the active branch's
cumulative token estimate exceeds a per-target threshold. For vLLM the
threshold tracks the configured `vllm_max_model_len` so reconfiguring the
served context size automatically shifts where the boundary fires.
"""

# ~12% safety margin under each provider's real window, because token_estimate
# is len(content)//3 and can drift on code-heavy or tool-heavy turns.
THRESHOLD_ANTHROPIC_STD = 175_000   # 200k window → 175k gate
THRESHOLD_GEMINI = 175_000          # Gemini 1M+ but treated as non-1M for CC handoff
THRESHOLD_LOCAL_OLLAMA = 28_000     # conservative for 32k-window local models
VLLM_SAFETY_MARGIN = 0.85           # 85% of configured max-model-len


import re as _re

# claude-<family>-<major>[-<minor>][-date], minor optional (matches server.py).
_1M_ID_RE = _re.compile(r"^claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?")

# Minimum version per family that actually has a 1M tier. Opus only from 4.7;
# Opus 4.6[1m] rate-limits/fails. Sonnet across our range. Haiku never.
_1M_MIN_VERSION = {"fable": (5, 0), "opus": (4, 7), "sonnet": (4, 5)}


def is_1m_anthropic(model_id: str) -> bool:
    """True for 1M-context Anthropic models.

    Loom surfaces supported 1M variants as `<base>[1m]` — either an alias
    (`sonnet[1m]`, `opus[1m]`, which resolve to the family's latest version) or a
    pinned full id (`claude-opus-4-7[1m]`). Family-level aliases always count
    since the latest version supports 1M; pinned ids are gated by version so an
    old `claude-opus-4-6[1m]` does not bypass the handoff as if it were real 1M.
    """
    if not model_id:
        return False
    m = model_id.lower()
    base = m.split("[")[0] if "[" in m else m
    if base.startswith("claude-fable-"):
        match = _1M_ID_RE.match(base)
        if not match:
            return False
        version = (int(match.group(2)), int(match.group(3) or 0))
        return version >= _1M_MIN_VERSION["fable"]
    if "[1m]" not in m:
        return False
    if base in ("sonnet", "opus"):
        return True
    match = _1M_ID_RE.match(base)
    if not match:
        return False
    family = match.group(1)
    version = (int(match.group(2)), int(match.group(3) or 0))
    floor = _1M_MIN_VERSION.get(family)
    return floor is not None and version >= floor


def is_anthropic(model_id: str) -> bool:
    """True for any Anthropic value Loom surfaces in the dropdown — either the
    sonnet/opus/haiku alias or a full `claude-<family>-...` ID, with optional
    `[1m]` suffix. Used by routing to decide between cloud Anthropic, Gemini,
    and local-llama paths."""
    if not model_id:
        return False
    base = model_id.split("[")[0] if "[" in model_id else model_id
    bl = base.lower()
    if bl in ("sonnet", "opus", "haiku"):
        return True
    return bl.startswith(("claude-fable-", "claude-opus-", "claude-sonnet-", "claude-haiku-"))


def is_gemini(model_id: str) -> bool:
    if not model_id:
        return False
    return model_id.lower().startswith("gemini")


def is_vllm(model_id: str) -> bool:
    """vLLM-direct CC models are surfaced via the `vllm-*` alias prefix
    (set up by --served-model-name) or by matching the configured HF id."""
    if not model_id:
        return False
    m = model_id.lower()
    if m.startswith("vllm-"):
        return True
    # Fall back to the live config — handles users who picked the descriptive
    # HF id from the Weave/Braid dropdown instead of the alias.
    try:
        from config import config as _cfg
        if _cfg.vllm_model and model_id == _cfg.vllm_model:
            return True
    except Exception:
        pass
    return False


def is_local_ollama(model_id: str) -> bool:
    """Local Ollama models — anything not Anthropic, Gemini, or vLLM. They
    come through CC in Braid mode with `use_ollama=True`."""
    if not model_id:
        return False
    if is_gemini(model_id) or is_vllm(model_id) or is_anthropic(model_id):
        return False
    return True


def _vllm_threshold() -> int:
    """vLLM threshold = 85% of the configured max-model-len. Read live so
    bumping vllm_max_model_len in Settings shifts the compact gate without
    needing a server restart."""
    try:
        from config import config as _cfg
        return max(8_000, int(_cfg.vllm_max_model_len * VLLM_SAFETY_MARGIN))
    except Exception:
        return THRESHOLD_LOCAL_OLLAMA


def handoff_threshold(target_model: str) -> int:
    """Token threshold above which a non-1M target needs a compact-handoff."""
    if is_vllm(target_model):
        return _vllm_threshold()
    if is_local_ollama(target_model):
        return THRESHOLD_LOCAL_OLLAMA
    if is_gemini(target_model):
        return THRESHOLD_GEMINI
    return THRESHOLD_ANTHROPIC_STD


def needs_handoff(target_model: str, branch_tokens: int) -> bool:
    """Fast gate check. Runs on every generation; most turns exit at the
    first line (1M target → skip)."""
    if is_1m_anthropic(target_model):
        return False
    return branch_tokens > handoff_threshold(target_model)


def provider_label(target_model: str) -> str:
    """Human-readable target family — used in toasts."""
    if is_1m_anthropic(target_model):
        return "Anthropic 1M"
    if is_gemini(target_model):
        return "Gemini"
    if is_vllm(target_model):
        return "vLLM"
    if is_local_ollama(target_model):
        return "Local"
    return "Anthropic"
