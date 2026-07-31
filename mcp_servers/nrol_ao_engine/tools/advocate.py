"""propose_advocate — the advocate's typed verdict tool (Track A phase 2).

Replaces the legacy ``ADVOCATE / ARTICLE: A<n> / VERDICT / REASON / END`` line
block + its single-line ``[^\n]*`` regex parser. The advocate calls this tool
once per article; the tool-use protocol delivers a typed object, so there is
nothing to regex-parse and the ``analysis`` field can be multi-paragraph and
unconstrained (§2.1, §6).

SAFETY (phase 2, same invariants as phase 1):
  - **No commits, no topic mutation, no posterior movement.** This tool
    RECORDS a proposal in an in-process, module-level list. It does not touch
    topic JSON, the evidence log, or the proposal store. No import of
    ``pipeline.apply_decisions`` / ``process_evidence`` / ``save_topic`` lives
    in the execution path. Commit is a later-phase concern and will route
    through the *existing* commit gates (Loom approval, governance) — never a
    new path introduced here.
  - **In-memory only.** The proposal list is process-local and cleared on
    restart. Phase 2 is about validating deliberation quality, not persisting
    proposals; persistence to topic JSON is out of scope here.

The ``analysis`` parameter is the substantive field. Its description (not the
system prompt — see the Phase-1 terse-prompt finding, §6) demands a detailed
multi-paragraph analysis citing specific article evidence and indicator ids,
exceeding 400 characters. This is the §4.1 phase-3 verification gate made
concrete in the tool schema.
"""

from __future__ import annotations

import uuid
from typing import Any

# ──────────────────────────────────────────────────────────────────────────
# In-memory proposal store (phase 2 only — no persistence to topic JSON)
# ──────────────────────────────────────────────────────────────────────────
#
# Module-level so tests and the runner can inspect what the advocate recorded.
# This is deliberately NOT the engine repo's proposal store; it is a phase-2
# scratch record. When phase 4 wires the commit gates, a committed proposal
# will flow through ``propose_match`` / ``commit_match`` (the existing MCP
# tools) — never through this list.

_proposals: list[dict[str, Any]] = []


def reset_proposals() -> None:
    """Clear the in-memory proposal list. Tests call this between cases."""
    _proposals.clear()


def list_proposals() -> list[dict[str, Any]]:
    """Return a shallow copy of recorded proposals (test/runner inspection)."""
    return [dict(p) for p in _proposals]


# ──────────────────────────────────────────────────────────────────────────
# Enums enforced by the tool schema (the protocol, not a regex)
# ──────────────────────────────────────────────────────────────────────────

VERDICTS = ("COMMIT", "PARK", "WITHDRAW", "DUPLICATE_OF", "SCHEMA_GAP")
ACTION_KINDS = ("FIRE", "OBSERVE", "PARK", "IGNORE", "SCHEMA_GAP")


def _new_proposal_id() -> str:
    return f"adv_{uuid.uuid4().hex[:8]}"


# ──────────────────────────────────────────────────────────────────────────
# Tool schema (OpenAI function spec)
# ──────────────────────────────────────────────────────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_advocate",
        "description": (
            "Record the advocate's verdict for one article. Call this ONCE per "
            "article after reading the indicator schema (and recent evidence "
            "where relevant). The verdict and proposed_action.kind are enums "
            "enforced by this schema — a bad value is a tool-call error, not a "
            "silent PARK. RECORDS a proposal only; it does not commit, does not "
            "move posteriors, and does not mutate topic state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "The article identifier supplied in the task (e.g. A1 or the article url).",
                },
                "verdict": {
                    "type": "string",
                    "enum": list(VERDICTS),
                    "description": (
                        "COMMIT: confirm a FIRE/OBSERVE action is correct. "
                        "PARK: keep parked or demote an OBSERVE/FIRE to PARK. "
                        "WITHDRAW: ignore as irrelevant or pure rhetoric. "
                        "DUPLICATE_OF: this article duplicates coverage of the event in parent_idx. "
                        "SCHEMA_GAP: the article reports evidence the schema has no observable for."
                    ),
                },
                "proposed_action": {
                    "type": "object",
                    "description": (
                        "The concrete action proposed. kind=FIRE fires an indicator; "
                        "kind=OBSERVE records an observed value for an indicator; "
                        "kind=PARK parks the article; kind=IGNORE drops it; "
                        "kind=SCHEMA_GAP flags a gap. parent_idx is REQUIRED when "
                        "verdict=DUPLICATE_OF (the parent article id)."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": list(ACTION_KINDS),
                            "description": "The action kind.",
                        },
                        "indicator_id": {
                            "type": "string",
                            "description": "The indicator id to fire/observe (required for FIRE and OBSERVE).",
                        },
                        "value": {
                            "type": "number",
                            "description": "The observed numeric value (required for OBSERVE).",
                        },
                        "parent_idx": {
                            "type": "string",
                            "description": "The parent article id (required when verdict=DUPLICATE_OF).",
                        },
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                "citation": {
                    "type": "string",
                    "description": (
                        "An exact quote or numeric value from the article that grounds "
                        "the verdict. Required for COMMIT/OBSERVE/FIRE; pass an empty "
                        "string for WITHDRAW/SCHEMA_GAP when no single phrase applies."
                    ),
                },
                "analysis": {
                    "type": "string",
                    "description": (
                        "Detailed multi-paragraph strategic and logical analysis. Cite "
                        "specific evidence from the article and specific indicator ids "
                        "from the schema. Must exceed 400 characters. This is the "
                        "substantive field — a one-liner here is a failed verdict."
                    ),
                },
            },
            "required": ["article_id", "verdict", "proposed_action", "citation", "analysis"],
            "additionalProperties": False,
        },
    },
}


# ──────────────────────────────────────────────────────────────────────────
# Tool function
# ──────────────────────────────────────────────────────────────────────────


def propose_advocate(
    article_id: str,
    verdict: str,
    proposed_action: dict[str, Any] | None,
    citation: str,
    analysis: str,
) -> dict[str, Any]:
    """Record one advocate proposal. Returns ``{proposal_id, recorded: true}``.

    The tool schema enforces the enum/shape before this function runs, so a
    bad verdict/kind is a tool-call error at the protocol layer. This body
    still validates defensively (in case the schema layer is bypassed by a
    test or a future caller) and surfaces a structured error rather than
    raising into the agent loop.

    Does NOT commit. Does NOT mutate topic state. See module docstring.
    """
    # Defensive validation (the schema is the primary enforcer).
    if not article_id or not str(article_id).strip():
        return {"error": "article_id is required"}
    if verdict not in VERDICTS:
        return {"error": f"verdict must be one of {VERDICTS}, got {verdict!r}"}
    if not isinstance(proposed_action, dict):
        return {"error": "proposed_action must be an object"}
    kind = proposed_action.get("kind")
    if kind not in ACTION_KINDS:
        return {"error": f"proposed_action.kind must be one of {ACTION_KINDS}, got {kind!r}"}
    if verdict == "DUPLICATE_OF" and not proposed_action.get("parent_idx"):
        return {"error": "parent_idx is required when verdict=DUPLICATE_OF"}
    if kind in ("FIRE", "OBSERVE") and not proposed_action.get("indicator_id"):
        return {"error": f"indicator_id is required when kind={kind}"}
    if kind == "OBSERVE" and proposed_action.get("value") is None:
        return {"error": "value is required when kind=OBSERVE"}

    analysis_str = str(analysis or "")
    if len(analysis_str) < 400:
        # We do NOT reject — we record the proposal AND flag the shortfall so
        # the runner/test can detect it. The schema description demands >400;
        # a model that ignores that still gets recorded, but the gate (§4.1)
        # catches it. Rejecting here would lose the run's audit trail.
        pass

    proposal_id = _new_proposal_id()
    record = {
        "proposal_id": proposal_id,
        "article_id": str(article_id),
        "verdict": verdict,
        "proposed_action": dict(proposed_action),
        "citation": str(citation or ""),
        "analysis": analysis_str,
        "analysis_len": len(analysis_str),
        "analysis_meets_min_len": len(analysis_str) >= 400,
    }
    _proposals.append(record)
    return {"proposal_id": proposal_id, "recorded": True}
