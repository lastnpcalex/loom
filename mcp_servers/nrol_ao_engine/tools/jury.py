"""submit_jury — the jury stage's typed verdict tool (Track A phase 3).

Replaces the legacy ``JURY / ARTICLE: A<n> / VERDICT / RATIONALE / END`` line
block + its single-line ``[^\n]*`` regex parser. The jury subagent calls this
tool once per case (article); the tool-use protocol delivers a typed object,
so there is nothing to regex-parse and the ``jury_rationale`` field can be
multi-paragraph and unconstrained (§2.1, §6).

The jury is the fresh, calibrated voter: it did not participate in the
advocate or rebut rounds. It receives both prior rounds' structured records
(the advocate's full ``analysis`` and the rebut's full
``rebuttal_analysis``/``objection_details``) and renders one final action per
article — accepting, modifying, or rejecting the advocate's proposal.

SAFETY (phase 3, same invariants as phases 1-2):
  - **No commits, no topic mutation, no posterior movement.** This tool
    RECORDS a verdict in an in-process, module-level list. It does not touch
    topic JSON, the evidence log, or the proposal store. No import of
    ``pipeline.apply_decisions`` / ``process_evidence`` / ``save_topic`` lives
    in the execution path. Commit is a later-phase concern and will route
    through the *existing* commit gates (Loom approval, governance) — never a
    new path introduced here.
  - **In-memory only.** The verdict list is process-local and cleared on
    restart. Phase 3 is about validating deliberation quality, not persisting
    verdicts; persistence to topic JSON is out of scope here.

The ``jury_rationale`` parameter is the substantive field. Its description
(not the system prompt — see the Phase-1 terse-prompt finding, §6) demands a
detailed multi-paragraph rationale that explicitly references BOTH the
advocate and rebuttal records, and explains why the final action accepts,
modifies, or rejects the advocate proposal. This is the §4.1 phase-3
verification gate made concrete in the tool schema.

The ``final_action.kind`` enum includes ``DUPLICATE_OF`` (§2.1 — the
discriminator-on-parent_idx form that the prior JSON-mode spec contradicted
itself over). When ``kind=DUPLICATE_OF``, ``parent_idx`` is required.
"""

from __future__ import annotations

import uuid
from typing import Any

# ──────────────────────────────────────────────────────────────────────────
# In-memory verdict store (phase 3 only — no persistence to topic JSON)
# ──────────────────────────────────────────────────────────────────────────
#
# Module-level so tests and the runner can inspect what the jury stage
# recorded. This is deliberately NOT the engine repo's proposal store; it is a
# phase-3 scratch record. When phase 4 wires the commit gates, a committed
# verdict will flow through ``propose_match`` / ``commit_match`` (the existing
# MCP tools) — never through this list.

_verdicts: list[dict[str, Any]] = []


def reset_verdicts() -> None:
    """Clear the in-memory verdict list. Tests call this between cases."""
    _verdicts.clear()


def list_verdicts() -> list[dict[str, Any]]:
    """Return a shallow copy of recorded verdicts (test/runner inspection)."""
    return [dict(v) for v in _verdicts]


# ──────────────────────────────────────────────────────────────────────────
# Enums enforced by the tool schema (the protocol, not a regex)
# ──────────────────────────────────────────────────────────────────────────

# The jury's final_action.kind is the action-kind enum PLUS DUPLICATE_OF.
# Unlike the advocate's proposed_action (where DUPLICATE_OF was a verdict, not
# a kind), the jury's final action can itself be a duplicate-folding
# directive — §2.1 resolves the prior JSON-mode contradiction by making
# parent_idx the discriminator on final_action when kind=DUPLICATE_OF.
JURY_FINAL_ACTION_KINDS = (
    "FIRE",
    "OBSERVE",
    "PARK",
    "IGNORE",
    "SCHEMA_GAP",
    "DUPLICATE_OF",
)


def _new_verdict_id() -> str:
    return f"jur_{uuid.uuid4().hex[:8]}"


# ──────────────────────────────────────────────────────────────────────────
# Tool schema (OpenAI function spec)
# ──────────────────────────────────────────────────────────────────────────

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_jury",
        "description": (
            "Record the jury's final verdict for one case (article). Call this "
            "ONCE per case, after reading the indicator schema. You are the "
            "jury: a fresh, calibrated voter who did NOT participate in the "
            "advocate or rebut rounds. Weigh the advocate proposal against the "
            "rebuttal and render one final action — accepting, modifying, or "
            "rejecting the advocate's proposal. You are NOT limited to the "
            "advocate's proposed indicator: if another listed observable or "
            "anti-indicator clearly matches, you may verdict OBSERVE/FIRE on "
            "that indicator instead. If no listed indicator captures the "
            "evidence direction, return SCHEMA_GAP. The default in genuine "
            "doubt is PARK. The final_action.kind is an enum enforced by this "
            "schema — a bad value is a tool-call error, not a silent PARK. "
            "RECORDS a verdict only; it does not commit, does not move "
            "posteriors, and does not mutate topic state."
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
                        "propose_advocate call for this article. Required so the "
                        "verdict is paired to its advocate proposal."
                    ),
                },
                "rebuttal_id": {
                    "type": "string",
                    "description": (
                        "The rebuttal_id returned by the rebut's propose_rebut call "
                        "for this article. Required so the verdict is paired to its "
                        "rebuttal record."
                    ),
                },
                "final_action": {
                    "type": "object",
                    "description": (
                        "The final action the jury renders for this article. "
                        "kind=FIRE fires an indicator; kind=OBSERVE records an "
                        "observed value; kind=PARK parks; kind=IGNORE drops; "
                        "kind=SCHEMA_GAP flags a gap (use description for the gap); "
                        "kind=DUPLICATE_OF folds this article into parent_idx. "
                        "indicator_id+value required for FIRE/OBSERVE; parent_idx "
                        "required for DUPLICATE_OF; description required for SCHEMA_GAP."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": list(JURY_FINAL_ACTION_KINDS),
                            "description": "The final action kind.",
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
                            "description": "The parent article id (required when kind=DUPLICATE_OF).",
                        },
                        "description": {
                            "type": "string",
                            "description": "Free-text description (required for SCHEMA_GAP; optional otherwise).",
                        },
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                "jury_rationale": {
                    "type": "string",
                    "description": (
                        "Detailed multi-paragraph rationale. Must explicitly reference "
                        "BOTH the advocate record (its proposal_id, verdict, proposed "
                        "action, or a quote from its analysis) AND the rebuttal record "
                        "(its rebuttal_id, verdict, objection, or corrected action). "
                        "Must explain WHY the final action accepts, modifies, or rejects "
                        "the advocate proposal — including directional alignment, "
                        "factual citation, and duplicate/over-interpretation checks. "
                        "Must cite real indicator ids (t[0-9]_…) or evidence ids "
                        "(ev_NNN) where relevant. Must exceed 300 characters. "
                        "A one-liner here is a failed verdict."
                    ),
                },
            },
            "required": [
                "article_id",
                "advocate_proposal_id",
                "rebuttal_id",
                "final_action",
                "jury_rationale",
            ],
            "additionalProperties": False,
        },
    },
}


# ──────────────────────────────────────────────────────────────────────────
# Tool function
# ──────────────────────────────────────────────────────────────────────────


def submit_jury(
    article_id: str,
    advocate_proposal_id: str,
    rebuttal_id: str,
    final_action: dict[str, Any] | None,
    jury_rationale: str,
) -> dict[str, Any]:
    """Record one jury verdict. Returns ``{verdict_id, recorded: true}``.

    The tool schema enforces the enum/shape before this function runs, so a
    bad kind is a tool-call error at the protocol layer. This body still
    validates defensively (in case the schema layer is bypassed by a test or a
    future caller) and surfaces a structured error rather than raising into
    the agent loop.

    Does NOT commit. Does NOT mutate topic state. See module docstring.
    """
    # Defensive validation (the schema is the primary enforcer).
    if not article_id or not str(article_id).strip():
        return {"error": "article_id is required"}
    if not advocate_proposal_id or not str(advocate_proposal_id).strip():
        return {"error": "advocate_proposal_id is required"}
    if not rebuttal_id or not str(rebuttal_id).strip():
        return {"error": "rebuttal_id is required"}
    if not isinstance(final_action, dict):
        return {"error": "final_action must be an object"}
    kind = final_action.get("kind")
    if kind not in JURY_FINAL_ACTION_KINDS:
        return {"error": f"final_action.kind must be one of {JURY_FINAL_ACTION_KINDS}, got {kind!r}"}
    if kind in ("FIRE", "OBSERVE") and not final_action.get("indicator_id"):
        return {"error": f"indicator_id is required when kind={kind}"}
    if kind == "OBSERVE" and final_action.get("value") is None:
        return {"error": "value is required when kind=OBSERVE"}
    if kind == "DUPLICATE_OF" and not final_action.get("parent_idx"):
        return {"error": "parent_idx is required when kind=DUPLICATE_OF"}
    if kind == "SCHEMA_GAP" and not str(final_action.get("description") or "").strip():
        return {"error": "description is required when kind=SCHEMA_GAP"}

    rationale_str = str(jury_rationale or "")
    if len(rationale_str) < 300:
        # We do NOT reject — we record the verdict AND flag the shortfall so
        # the runner/test can detect it. The schema description demands >300;
        # a model that ignores that still gets recorded, but the gate (§4.1)
        # catches it. Rejecting here would lose the run's audit trail.
        pass

    verdict_id = _new_verdict_id()
    record = {
        "verdict_id": verdict_id,
        "article_id": str(article_id),
        "advocate_proposal_id": str(advocate_proposal_id),
        "rebuttal_id": str(rebuttal_id),
        "final_action": dict(final_action),
        "jury_rationale": rationale_str,
        "jury_rationale_len": len(rationale_str),
        "jury_rationale_meets_min_len": len(rationale_str) >= 300,
    }
    _verdicts.append(record)
    return {"verdict_id": verdict_id, "recorded": True}
