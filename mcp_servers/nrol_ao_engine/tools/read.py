"""Reading tools for the NROL-AO engine agent (Track A phase 2).

Three read-only tools that give the advocate/rebut/jury subagents the *context*
the legacy ``build_advocate_prompt`` used to inline as text — indicator schema,
hypotheses + posteriors, and recent evidence — but as structured dicts instead
of prompt strings. These mirror ``framework/news_observation_pipeline.walk_indicators``
and ``build_advocate_prompt``'s CONTEXT block (hypotheses, indicators, articles),
replacing only the *output* mechanism (tool-call verdicts vs line-regex), per
the Phase 2 mandate.

All three are strictly read-only. They never write topic JSON, the evidence
log, or source_db. Errors are surfaced as ``{"error": "..."}`` dicts — they
never raise into the agent loop (the dispatcher wraps them in a tool message
either way, but returning a structured error keeps the failure legible to the
model instead of looking like a transport fault).

Repo access goes through ``imports.import_from_repo`` so the engine repo stays
the single source of truth for topic state (A.6 — additive, never breaks the
working scan).
"""

from __future__ import annotations

from typing import Any

from ..imports import import_from_repo

# Caps the evidence text snippet returned to the agent. The full evidence text
# can be long; the advocate only needs the excerpt that carries the claim and
# the indicator it fired/didn't fire. ~300 chars matches the spec.
_EVIDENCE_TEXT_SNIPPET_CHARS = 300
_DEFAULT_EVIDENCE_LIMIT = 10


def _load_topic(slug: str) -> dict[str, Any] | None:
    """Load a topic dict from the engine repo, or None on any failure.

    Uses ``engine.load_topic`` (which validates + expires hypotheses) via the
    import shim — the same entry point the operator MCP uses. The topic dict is
    the raw on-disk state; callers below extract only what they need.
    """
    try:
        engine = import_from_repo("engine")
    except Exception as exc:
        return {"_import_error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    try:
        return engine.load_topic(slug)
    except Exception as exc:
        # FileNotFoundError, validation error, etc. — surface as a sentinel.
        return {"_load_error": f"{type(exc).__name__}: {str(exc)[:200]}"}


# ──────────────────────────────────────────────────────────────────────────
# read_topic
# ──────────────────────────────────────────────────────────────────────────

READ_TOPIC_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_topic",
        "description": (
            "Read a topic's metadata, question, and current hypothesis "
            "posteriors. Use this first to understand what the topic is "
            "asking and where beliefs currently sit. Returns ONLY meta + "
            "hypotheses — the evidence log and full indicator schema are "
            "deliberately excluded (use read_recent_evidence and "
            "read_indicator_schema for those). Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The topic slug, e.g. calibration-hormuz-reopen-2027.",
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    },
}


def read_topic(slug: str) -> dict[str, Any]:
    """Return a slim topic view: meta + hypotheses (label + posterior).

    Strips ``evidenceLog`` and ``indicators`` — they're large and the agent
    gets them via the dedicated tools. Always returns a dict; errors land in
    an ``error`` key.
    """
    topic = _load_topic(slug)
    if topic is None:
        return {"error": f"load_topic returned None for {slug!r}"}
    if "_import_error" in topic:
        return {"error": f"could not import engine: {topic['_import_error']}"}
    if "_load_error" in topic:
        return {"error": f"could not load topic {slug!r}: {topic['_load_error']}"}

    meta = topic.get("meta") or {}
    hyps_raw = (topic.get("model") or {}).get("hypotheses") or {}
    hypotheses: dict[str, Any] = {}
    for hk, hv in hyps_raw.items():
        if not isinstance(hv, dict):
            continue
        hypotheses[hk] = {
            "label": hv.get("label") or hv.get("desc") or "",
            "posterior": hv.get("posterior"),
        }

    return {
        "slug": meta.get("slug") or slug,
        "title": meta.get("title", ""),
        "question": meta.get("question", ""),
        "resolution": meta.get("resolution", ""),
        "status": meta.get("status", ""),
        "classification": meta.get("classification", ""),
        "hypotheses": hypotheses,
    }


# ──────────────────────────────────────────────────────────────────────────
# read_indicator_schema
# ──────────────────────────────────────────────────────────────────────────

READ_INDICATOR_SCHEMA_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_indicator_schema",
        "description": (
            "Read the topic's full indicator schema as a flat list. Each entry "
            "has an id (e.g. t1_transit_band_40_to_55), its tier, a human "
            "description, the per-hypothesis likelihood vector, the observable "
            "spec (metric/threshold/direction) when present, and the "
            "posteriorEffect summary. Call this before proposing any verdict "
            "so citations reference real indicator ids. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The topic slug.",
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    },
}


def _slim_indicator(ind: dict[str, Any], tier: str) -> dict[str, Any]:
    """Project a raw indicator dict down to the fields the advocate needs.

    Keeps the id, tier, desc, likelihoods, posteriorEffect, observable, and
    shape — everything the legacy prompt inlined as INDICATORS reference text
    (news_observation_pipeline.py:326-334). Drops n_firings/status/note and
    other engine-internal bookkeeping that would just bloat the tool result.
    """
    out: dict[str, Any] = {
        "id": ind.get("id", ""),
        "tier": tier,
        "desc": ind.get("desc") or ind.get("description") or "",
        "likelihoods": ind.get("likelihoods") or {},
    }
    if "observable" in ind and isinstance(ind["observable"], dict):
        out["observable"] = ind["observable"]
    if "posteriorEffect" in ind:
        out["posteriorEffect"] = ind["posteriorEffect"]
    if "shape" in ind:
        out["shape"] = ind["shape"]
    if "target_hypothesis" in ind:
        out["target_hypothesis"] = ind["target_hypothesis"]
    return out


def read_indicator_schema(slug: str) -> dict[str, Any]:
    """Return the flat indicator list for a topic via ``walk_indicators``.

    Mirrors ``framework.news_observation_pipeline.walk_indicators`` (which itself
    wraps ``iter_indicators_for_topic``) — the same canonical schema walk the
    legacy prompt builders use, so the advocate sees the identical set of
    indicators it would have seen inlined as text. Returns a list (possibly
    empty) under an ``indicators`` key, or ``{"error": ...}``.
    """
    topic = _load_topic(slug)
    if topic is None:
        return {"error": f"load_topic returned None for {slug!r}"}
    if "_import_error" in topic:
        return {"error": f"could not import engine: {topic['_import_error']}"}
    if "_load_error" in topic:
        return {"error": f"could not load topic {slug!r}: {topic['_load_error']}"}

    try:
        nop = import_from_repo("framework.news_observation_pipeline")
    except Exception as exc:
        return {"error": f"could not import news_observation_pipeline: {type(exc).__name__}: {str(exc)[:200]}"}

    try:
        flat = nop.walk_indicators(topic)
    except Exception as exc:
        return {"error": f"walk_indicators failed: {type(exc).__name__}: {str(exc)[:200]}"}

    # walk_indicators yields (tier, ind) — re-walk to pair tiers. The helper
    # returns a flat list of indicators only, so we re-derive tier pairing via
    # iter_indicators_for_topic to keep the tier label faithful.
    try:
        schema_mod = import_from_repo("framework.indicator_schema")
        paired: list[dict[str, Any]] = []
        for tier, ind in schema_mod.iter_indicators_for_topic(topic):
            if isinstance(ind, dict) and "id" in ind and "likelihoods" in ind:
                paired.append(_slim_indicator(ind, tier))
        indicators = paired
    except Exception:
        # Fallback: walk_indicators already gave us the flat list without tiers.
        indicators = [_slim_indicator(ind, "") for ind in flat if isinstance(ind, dict)]

    return {"slug": slug, "count": len(indicators), "indicators": indicators}


# ──────────────────────────────────────────────────────────────────────────
# read_recent_evidence
# ──────────────────────────────────────────────────────────────────────────

READ_RECENT_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_recent_evidence",
        "description": (
            "Read the most recent evidence-log entries for a topic. Each entry "
            "has an evidence_id (ev_NNN), timestamp, url, a short text "
            "snippet, the action taken (OBSERVED/PARKED/FIRED...), and the "
            "indicator_id it fired if any. Use this to check for duplicate "
            "coverage and to see what the schema has already captured. "
            "Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The topic slug.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of recent entries to return (default 10).",
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    },
}


def _slim_evidence(ev: dict[str, Any]) -> dict[str, Any]:
    """Project one evidence-log entry down to the advocate-facing fields.

    Caps text_snippet to ~300 chars so a large evidence log doesn't blow the
    tool result through the context budget. Keeps the evidence_id (so the
    advocate can cite ``ev_NNN`` per the §4.1 metric), the url, the action, and
    the indicator_id it fired (if any) — the fields needed to detect
    duplicates and to ground citations in prior observations.
    """
    text = str(ev.get("text") or "")
    snippet = text[:_EVIDENCE_TEXT_SNIPPET_CHARS]
    if len(text) > _EVIDENCE_TEXT_SNIPPET_CHARS:
        snippet += "…"

    out: dict[str, Any] = {
        "evidence_id": ev.get("id") or ev.get("evidence_id") or "",
        "timestamp": ev.get("time") or ev.get("timestamp") or "",
        "url": ev.get("url") or "",
        "text_snippet": snippet,
        "action": ev.get("provenance") or ev.get("action") or "",
    }
    if "indicator_id" in ev:
        out["indicator_id"] = ev["indicator_id"]
    if "fired_indicator_id" in ev:
        out["indicator_id"] = ev["fired_indicator_id"]
    return out


def read_recent_evidence(slug: str, *, limit: int = _DEFAULT_EVIDENCE_LIMIT) -> dict[str, Any]:
    """Return the last ``limit`` evidence entries, oldest-of-the-window last.

    Reads ``topic["evidenceLog"][-limit:]``. Caps text snippets and handles an
    empty log gracefully (returns an empty list, not an error — an empty log
    is a legitimate state for a fresh topic).
    """
    topic = _load_topic(slug)
    if topic is None:
        return {"error": f"load_topic returned None for {slug!r}"}
    if "_import_error" in topic:
        return {"error": f"could not import engine: {topic['_import_error']}"}
    if "_load_error" in topic:
        return {"error": f"could not load topic {slug!r}: {topic['_load_error']}"}

    log = topic.get("evidenceLog") or []
    if not isinstance(log, list):
        return {"slug": slug, "count": 0, "evidence": []}

    # Defensive: clamp limit to a sane range.
    n = max(0, min(int(limit or _DEFAULT_EVIDENCE_LIMIT), 100))
    window = log[-n:] if n > 0 else []
    evidence = [_slim_evidence(ev) for ev in window if isinstance(ev, dict)]
    return {"slug": slug, "count": len(evidence), "evidence": evidence}
