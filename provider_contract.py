"""Provider-independent session continuity rules for Loom agent harnesses.

The Loom message tree is the source of truth. Provider-native sessions are an
optimization that may be resumed only when the nearest prior assistant turn is
provably compatible with the target harness. Searching past an intervening
assistant turn can silently omit that turn when an older session is resumed.
"""

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True)
class ResumeDecision:
    """Why Loom will resume a native provider session or rebuild from history."""

    session_id: str | None
    reason: str
    message_id: int | None = None

    @property
    def can_resume(self) -> bool:
        return bool(self.session_id)


def _has_content_blocks(message: Mapping) -> bool:
    value = message.get("content_blocks")
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip() not in {"[]", "{}", "null"}
    return bool(value)


def select_resume_session(
    branch: Iterable[Mapping],
    target_mode: str,
    *,
    target_model: str | None = None,
    models_match: Callable[[str, str], bool] | None = None,
) -> ResumeDecision:
    """Select a resumable session at the nearest assistant boundary.

    A provider switch must rebuild from Loom history. Consequently this stops
    at the first prior assistant turn even when it is foreign, unscoped,
    model-incompatible, errored, empty, or missing a native session id. It
    never searches farther back for an older compatible session.

    Legacy rows with no ``cc_session_mode`` deliberately rebuild once. Their
    native session namespace cannot be proven safe across harnesses; after the
    fresh turn, the newly saved message carries an explicit mode and normal
    same-provider resume behavior returns.
    """
    messages = list(branch)
    for message in reversed(messages):
        role = message.get("role")
        content = str(message.get("content") or "")

        if role == "system":
            if content.lstrip().startswith("[CC context compactified"):
                return ResumeDecision(None, "compact_boundary", message.get("id"))
            continue
        if role != "assistant":
            continue

        message_id = message.get("id")
        if content.startswith("[Error:"):
            return ResumeDecision(None, "assistant_error", message_id)
        if not content.strip() and not _has_content_blocks(message):
            return ResumeDecision(None, "assistant_empty", message_id)

        session_id = str(message.get("cc_session_id") or "").strip()
        if not session_id:
            return ResumeDecision(None, "assistant_without_session", message_id)

        session_mode = str(message.get("cc_session_mode") or "").strip()
        if not session_mode:
            return ResumeDecision(None, "legacy_unscoped_session", message_id)
        if session_mode != target_mode:
            return ResumeDecision(None, "provider_boundary", message_id)

        if models_match is not None and target_model:
            recorded_model = str(message.get("cc_model_used") or "").strip()
            if not recorded_model:
                return ResumeDecision(None, "model_unscoped_session", message_id)
            if not models_match(recorded_model, target_model):
                return ResumeDecision(None, "model_boundary", message_id)

        return ResumeDecision(session_id, "resume", message_id)

    return ResumeDecision(None, "no_assistant_session")
