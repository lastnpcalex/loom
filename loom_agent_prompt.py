"""Shared Loom agent contract injection helpers."""

from pathlib import Path

_LOOM_AGENT_PATH = Path(__file__).parent / "loom_agent.md"


def load_loom_agent_prompt() -> str:
    """Return the shared role-neutral Loom contract, or an empty string."""
    try:
        return _LOOM_AGENT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def merge_system_prompts(*parts: str | None) -> str:
    """Join optional system-prompt fragments with stable spacing."""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def prepend_loom_agent_context(prompt: str, provider: str) -> str:
    """Inject the Loom contract into prompt-only harnesses without repo files."""
    contract = load_loom_agent_prompt()
    if not contract:
        return prompt
    return (
        f"<loom_agent_contract provider=\"{provider}\">\n"
        f"{contract}\n"
        f"</loom_agent_contract>\n\n"
        f"<user_task>\n"
        f"{prompt}\n"
        f"</user_task>"
    )
