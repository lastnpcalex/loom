"""Model context-window table + handoff gate.

Used by the generation pipeline to decide, for a given target model and branch
size, whether we need to run a pre-generation compact-and-handoff before
letting the target model see the branch.

1M-context Anthropic models always skip the gate. Everything else (standard
Anthropic, Gemini, local Ollama) trips the gate if the active branch's
cumulative token estimate exceeds a conservative per-provider threshold.
"""

# ~12% safety margin under each provider's real window, because token_estimate
# is len(content)//3 and can drift on code-heavy or tool-heavy turns.
THRESHOLD_ANTHROPIC_STD = 175_000   # 200k window → 175k gate
THRESHOLD_GEMINI = 175_000          # Gemini 1M+ but treated as non-1M for CC handoff
THRESHOLD_LOCAL_OLLAMA = 28_000     # conservative for 32k-window local models


def is_1m_anthropic(model_id: str) -> bool:
    """True for 1M-context Anthropic models.

    Loom surfaces 1M variants as `<base>[1m]` (e.g. `sonnet[1m]`, `opus[1m]`).
    """
    if not model_id:
        return False
    m = model_id.lower()
    return "[1m]" in m


def is_gemini(model_id: str) -> bool:
    if not model_id:
        return False
    return model_id.lower().startswith("gemini")


def is_local_ollama(model_id: str) -> bool:
    """Local models are anything that isn't Anthropic or Gemini — they come
    through CC in Braid mode with `use_ollama=True`."""
    if not model_id:
        return False
    m = model_id.lower()
    if is_gemini(m):
        return False
    # Anthropic shortcodes Loom uses: sonnet, haiku, opus (with optional [1m])
    if m.startswith(("sonnet", "haiku", "opus")):
        return False
    return True


def handoff_threshold(target_model: str) -> int:
    """Token threshold above which a non-1M target needs a compact-handoff."""
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
    if is_local_ollama(target_model):
        return "Local"
    return "Anthropic"
