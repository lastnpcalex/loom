"""propose_rebut — the rebuttal stage's typed verdict tool (Track A phase 3).

Replaces the legacy ``REBUT / ARTICLE: A<n> / VERDICT / OBJECTION /
CORRECTED_ACTION / REASON / END`` line block + its single-line ``[^\n]*`` regex
parser. The rebut subagent calls this tool once per advocate proposal; the
tool-use protocol delivers a typed object, so there is nothing to regex-parse
and the ``rebuttal_analysis`` field can be multi-paragraph and unconstrained
(§2.1, §6).

SAFETY (phase 3, same invariants as phases 1-2):
  - **No commits, no topic mutation, no posterior movement.** This tool
    RECORDS a rebuttal in an in-process, module-level list. It does not touch
    topic JSON, the evidence log, or the proposal store. No import of
    ``pipeline.apply_decisions`` / ``process_evidence`` / ``save_topic`` lives
    in the execution path. Commit is a later-phase concern and will route
    through the *existing* commit gates (Loom approval, governance) — never a
    new path introduced here.
  - **In-memory only.** The rebuttal list is process-local and cleared on
    restart. Phase 3 is about validating deliberation quality, not persisting
    verdicts; persistence to topic JSON is out of scope here.

The ``rebuttal_analysis`` parameter is the substantive field. Its description
(not the system prompt — see the Phase-1 terse-prompt finding, §6) demands a
detailed multi-paragraph analysis that explicitly references the advocate's
proposal/claim/action and cites real indicator or evidence ids. This is the
§4.1 phase-3 verification gate made concrete in the tool schema.
"""

from __future__ import annotations

import uuid
from typing import Any

# ──────────────────────────────────────────────────────────────────────────
# In-memory rebuttal store (phase 3 only — no persistence to topic JSON)
# ──────────────────────────────────────────────────────────────────────────
#
# Module-level so tests and the runner can inspect what the rebut stage
# recorded. This is deliberately NOT the engine repo's proposal store; it is a
# phase-3 scratch record. When phase 4 wires the commit gates, a committed
# verdict will flow through ``propose_match`` / ``commit_match`` (the existing
# MCP tools) — never through this list.

_rebuttals: list[dict[str, Any]] = []


def reset_rebuttals() -> None:
    """Clear the in-memory rebuttal list. Tests call this between cases."""
    _rebuttals.clear()


def list_rebuttals() -> list[dict[str, Any]]:
    """Return a shallow copy of recorded rebuttals (test/runner inspection)."""
    return [dict(r) for r in _rebuttals]


# ──────────────────────────────────────────────────────────────────────────
# Enums enforced by the tool schema (the protocol, not a regex)
# ──────────────────────────────────────────────────────────────────────────

# Rebut verdict reuses the advocate verdict set: the rebut either endorses the
# advocate's verdict (COMMIT), demotes it (PARK/WITHDRAW), flags a duplicate
# (DUPLICATE_OF), or identifies a schema gap (SCHEMA_GAP).
REBUT_VERDICTS = ("COMMIT", "PARK", "WITHDRAW", "DUPLICATE_OF", "SCHEMA_GAP")
# corrected_action.kind is the action-kind enum (same surface as the advocate's
# proposed_action.kind). FIRE/OBSERVE/PARK/IGNORE/SCHEMA_GAP.
CORRECTED_ACTION_KINDS = ("FIRE", "OBSERVE", "PARK", "IGNORE", "SCHEMA_GAP")


def _new_rebuttal_id() -> str:
    return f"reb_{uuid.uuid4().hex[:8]}"


# ──────────────────────────────────────────────────────────────────────────
# Tool schema (OpenAI function spec)
# ──────────────────────────────────────────────────────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "propose_rebut",
        "description": (
            "Record the rebuttal verdict for one advocate proposal. Call this "
            "ONCE per advocate proposal, after reading the indicator schema "
            "where the advocate's action touches an indicator. You are the "
            "skeptic: scrutinize the advocate's proposed action for "
            "directional alignment, factual citation, correct metrics/units, "
            "over-interpretation, and duplicate-event grouping. The verdict "
            "and corrected_action.kind are enums enforced by this schema — a "
            "bad value is a tool-call error, not a silent PARK. RECORDS a "
            "rebuttal only; it does not commit, does not move posteriors, "
            "and does not mutate topic state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "The article identifier the advocate proposed on (e.g. A1 or the article url).",
                },
                "advocate_proposal_id": {
                    "type": "string",
                    "description": (
                        "The proposal_id returned by the advocate's "
                        "propose_advocate call for this article (e.g. adv_1a2b3c4d). "
                        "Required so the jury can pair this rebuttal to its advocate proposal."
                    ),
                },
                "verdict": {
                    "type": "string",
                    "enum": list(REBUT_VERDICTS),
                    "description": (
                        "COMMIT: endorse the advocate's action as correct. "
                        "PARK: demote the advocate's FIRE/OBSERVE to PARK, or keep parked. "
                        "WITHDRAW: the article is irrelevant or pure rhetoric. "
                        "DUPLICATE_OF: the article duplicates coverage in parent_idx. "
                        "SCHEMA_GAP: the article reports evidence the schema has no observable for."
                    ),
                },
                "objection_raised": {
                    "type": "boolean",
                    "description": (
                        "True if you raise a specific objection to the advocate's "
                        "proposal (wrong direction, wrong metric, over-interpretation, "
                        "duplicate). False if you endorse it (COMMIT) without objection."
                    ),
                },
                "objection_details": {
                    "type": "string",
                    "description": (
                        "If objection_raised is true, state the specific flaw: which "
                        "advocate claim/action is wrong and why. Reference the advocate's "
                        "proposed_action.kind, its indicator_id, or a quote from its "
                        "analysis. If objection_raised is false, pass an empty string."
                    ),
                },
                "corrected_action": {
                    "type": "object",
                    "description": (
                        "The action the rebuttal proposes INSTEAD of (or endorsing) the "
                        "advocate's. kind=FIRE/OBSERVE require indicator_id (and value for "
                        "OBSERVE); kind=PARK/IGNORE/SCHEMA_GAP are self-contained. "
                        "parent_idx is REQUIRED when verdict=DUPLICATE_OF (the parent article id). "
                        "When endorsing the advocate unchanged, echo the advocate's proposed_action."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": list(CORRECTED_ACTION_KINDS),
                            "description": "The corrected action kind.",
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
                "rebuttal_analysis": {
                    "type": "string",
                    "description": (
                        "Detailed multi-paragraph skeptical analysis. Must explicitly "
                        "reference at least one advocate claim, proposal id, or proposed "
                        "action (e.g. 'the advocate proposed OBSERVE on t2_transit_recovery_70pct "
                        "at value 60; I find...'). Must cite real indicator ids (t[0-9]_…) or "
                        "evidence ids (ev_NNN) where relevant. Must exceed 300 characters. "
                        "A one-liner here is a failed rebuttal."
                    ),
                },
            },
            "required": [
                "article_id",
                "advocate_proposal_id",
                "verdict",
                "objection_raised",
                "objection_details",
                "corrected_action",
                "rebuttal_analysis",
            ],
            "additionalProperties": False,
        },
    },
}


# ──────────────────────────────────────────────────────────────────────────
# Tool function
# ──────────────────────────────────────────────────────────────────────────


def propose_rebut(
    article_id: str,
    advocate_proposal_id: str,
    verdict: str,
    objection_raised: bool,
    objection_details: str,
    corrected_action: dict[str, Any] | None,
    rebuttal_analysis: str,
) -> dict[str, Any]:
    """Record one rebuttal. Returns ``{rebuttal_id, recorded: true}``.

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
    if not advocate_proposal_id or not str(advocate_proposal_id).strip():
        return {"error": "advocate_proposal_id is required"}
    if verdict not in REBUT_VERDICTS:
        return {"error": f"verdict must be one of {REBUT_VERDICTS}, got {verdict!r}"}
    if not isinstance(objection_raised, bool):
        return {"error": "objection_raised must be a boolean"}
    if not isinstance(corrected_action, dict):
        return {"error": "corrected_action must be an object"}
    kind = corrected_action.get("kind")
    if kind not in CORRECTED_ACTION_KINDS:
        return {"error": f"corrected_action.kind must be one of {CORRECTED_ACTION_KINDS}, got {kind!r}"}
    if verdict == "DUPLICATE_OF" and not corrected_action.get("parent_idx"):
        return {"error": "parent_idx is required when verdict=DUPLICATE_OF"}
    if kind in ("FIRE", "OBSERVE") and not corrected_action.get("indicator_id"):
        return {"error": f"indicator_id is required when kind={kind}"}
    if kind == "OBSERVE" and corrected_action.get("value") is None:
        return {"error": "value is required when kind=OBSERVE"}

    analysis_str = str(rebuttal_analysis or "")
    if len(analysis_str) < 300:
        # We do NOT reject — we record the rebuttal AND flag the shortfall so
        # the runner/test can detect it. The schema description demands >300;
        # a model that ignores that still gets recorded, but the gate (§4.1)
        # catches it. Rejecting here would lose the run's audit trail.
        pass

    rebuttal_id = _new_rebuttal_id()
    record = {
        "rebuttal_id": rebuttal_id,
        "article_id": str(article_id),
        "advocate_proposal_id": str(advocate_proposal_id),
        "verdict": verdict,
        "objection_raised": objection_raised,
        "objection_details": str(objection_details or ""),
        "corrected_action": dict(corrected_action),
        "rebuttal_analysis": analysis_str,
        "rebuttal_analysis_len": len(analysis_str),
        "rebuttal_analysis_meets_min_len": len(analysis_str) >= 300,
    }
    _rebuttals.append(record)
    return {"rebuttal_id": rebuttal_id, "recorded": True}
