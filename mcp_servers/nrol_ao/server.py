"""MCP facade for the NROL-AO epistemic engine.

This server intentionally exposes proposal-shaped operations instead of raw
engine internals. Runtime posterior movement is restricted to the source
repo's own process_evidence/apply_observation paths, which enforce indicator
binding, governance gates, source weighting, and topic JSON persistence.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import ssl
import subprocess
import sys
import uuid
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from mcp.server.fastmcp import FastMCP

from .activity import ActivityStore, default_activity_dir, new_job_id
from .proposals import ProposalStore
from . import llama as llama_client

mcp = FastMCP("nrol-ao")

_DEFAULT_REPO = Path(r"C:\Claude-Code\NROL-AO\temp-repo")
# Black-hole public-surface repo: export_blackhole_snapshot writes
# surfaces/nrol-ao/data.json here; publish_black_hole_snapshot(commit/push=True)
# stages only that file and pushes to master.
_DEFAULT_BLACK_HOLE_REPO = Path(
    r"C:\Users\exast\OneDrive\Documents\Loom-Projects\black-hole"
)
_ALLOWED_TRANSITIONS = {"PARK", "FIRE", "OBSERVE", "SCHEMA_GAP", "IGNORE"}
_MUTATING_TRANSITIONS = {"PARK", "FIRE", "OBSERVE", "SCHEMA_GAP"}
_MAX_SEARCH_RESULTS_PER_CHANNEL = 6
_MAX_AGGREGATED_SEARCH_RESULTS_PER_CHANNEL = 24
_SOURCE_QUALIFIED_SEARCH_DOMAINS = (
    "bbc.com",
    "aljazeera.com",
    "reuters.com",
    "apnews.com",
    "theguardian.com",
)
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "vero_id",
    "yclid",
    "wickedid",
}
_RELATIVE_DATE_RE = re.compile(
    r"(?i)(\d+)\s*(minute|hour|day|week|month|year)s?\s*ago"
)
_DATE_FORMATS = (
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
)


def _to_int_idx(idx) -> int:
    if isinstance(idx, (int, float)):
        return int(idx)
    try:
        if isinstance(idx, str) and "." in idx:
            return int(float(idx))
        return int(idx)
    except (ValueError, TypeError):
        return 0


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=True, default=str)


def _repo_path() -> Path:
    configured = os.environ.get("NROL_AO_REPO", "").strip()
    root = Path(configured) if configured else _DEFAULT_REPO
    return root.resolve()


def _black_hole_path() -> Path:
    configured = os.environ.get("NROL_AO_BLACK_HOLE_REPO", "").strip()
    root = Path(configured) if configured else _DEFAULT_BLACK_HOLE_REPO
    return root.resolve()


def _activity_store() -> ActivityStore:
    configured = os.environ.get("NROL_AO_ACTIVITY_DIR", "").strip()
    root = Path(configured).resolve() if configured else default_activity_dir(_repo_path())
    return ActivityStore(root)


def _proposal_store() -> ProposalStore:
    """Proposals live beside the activity ledger (same configurable root)."""
    configured = os.environ.get("NROL_AO_ACTIVITY_DIR", "").strip()
    root = Path(configured).resolve() if configured else default_activity_dir(_repo_path())
    return ProposalStore(root)


def _ensure_repo() -> Path:
    root = _repo_path()
    if not root.exists():
        raise FileNotFoundError(
            f"NROL-AO repo not found at {root}. Set NROL_AO_REPO to the repo path."
        )
    if not (root / "engine.py").is_file() or not (root / "governor.py").is_file():
        raise FileNotFoundError(f"{root} does not look like an NROL-AO repo")
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
        importlib.invalidate_caches()
    return root


def _import_from_repo(module_name: str):
    root = _ensure_repo()
    module = importlib.import_module(module_name)
    module_file = Path(getattr(module, "__file__", "")).resolve()
    if module_file != root / f"{module_name}.py" and root not in module_file.parents:
        raise RuntimeError(
            f"Imported {module_name} from {module_file}, not configured repo {root}"
        )
    return module


def _post_loom_json(port: str, endpoint: str, payload: dict, timeout: float | None) -> dict:
    last_error = ""
    for proto in ("http", "https"):
        url = f"{proto}://127.0.0.1:{port}{endpoint}"
        try:
            with httpx.Client(verify=False, timeout=timeout) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                return response.json() if response.content else {}
        except Exception as exc:
            last_error = f"{proto}: {exc}"
    raise RuntimeError(last_error)


def _ask_loom_permission(action: str, payload: dict) -> str | None:
    conv_id = os.environ.get("LOOM_CONV_ID", "")
    port = os.environ.get("LOOM_PORT", "3000")
    if not conv_id:
        # Fail closed: commits without a Loom conversation context are denied
        # unless explicitly opted out. Loom always provides LOOM_CONV_ID
        # (claude_client injects it per conversation via --mcp-config), so
        # this only triggers for headless/external sessions.
        if os.environ.get("NROL_AO_ALLOW_UNGATED_COMMITS") == "1":
            return None
        return (
            "Commit denied: no Loom conversation context (LOOM_CONV_ID is not "
            "set), so no human can approve this mutation. Run through Loom, or "
            "set NROL_AO_ALLOW_UNGATED_COMMITS=1 to explicitly opt out for "
            "headless/dev use."
        )

    tool_id = f"nrol-ao-{uuid.uuid4().hex[:12]}"
    request = {
        "loom_conv_id": conv_id,
        "tool_name": action,
        "tool_id": tool_id,
        "tool_input": payload,
    }
    try:
        _post_loom_json(port, "/api/cc-tool-start", request, timeout=3)
    except Exception:
        pass
    try:
        response = _post_loom_json(port, "/api/cc-permission", request, timeout=None)
    except Exception as exc:
        return f"Loom permission request failed: {exc}"
    if not response.get("allow"):
        return response.get("message") or "Denied by user in Loom UI"
    return None


def _posteriors(topic: dict) -> dict:
    return {
        key: value.get("posterior")
        for key, value in topic.get("model", {}).get("hypotheses", {}).items()
    }


def _topic_summary(topic: dict) -> dict:
    meta = topic.get("meta", {})
    gov = topic.get("governance", {})
    return {
        "slug": meta.get("slug"),
        "title": meta.get("title"),
        "status": meta.get("status"),
        "classification": meta.get("classification"),
        "lastUpdated": meta.get("lastUpdated"),
        "posteriors": _posteriors(topic),
        "governance": {
            "health": gov.get("health"),
            "issues": gov.get("issues", [])[:10],
            "flagged_for_indicator_review": len(gov.get("flagged_for_indicator_review", []) or []),
            "flagged_schema_gaps": len(gov.get("flagged_schema_gaps", []) or []),
            "proposed_schema_extensions": len(gov.get("proposed_schema_extensions", []) or []),
        },
    }


def _topic_scan_status(topic: dict) -> dict:
    meta = topic.get("meta", {}) or {}
    gov = topic.get("governance", {}) or {}
    evidence = topic.get("evidenceLog", []) or []
    last_scanned = meta.get("lastScanned") or ""
    age_hours = None
    stale = True
    if last_scanned:
        try:
            ts = datetime.fromisoformat(str(last_scanned).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = round((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1)
            classification = (meta.get("classification") or "ROUTINE").upper()
            threshold = 12 if classification == "ALERT" else (7 * 24 if classification == "CALIBRATION" else 72)
            stale = age_hours >= threshold
        except Exception:
            pass
    return {
        "slug": meta.get("slug"),
        "title": meta.get("title"),
        "status": meta.get("status"),
        "classification": meta.get("classification"),
        "governanceHealth": gov.get("health"),
        "lastUpdated": meta.get("lastUpdated"),
        "lastScanned": last_scanned,
        "scanAgeHours": age_hours,
        "scanStale": stale,
        "evidenceCount": len(evidence),
        "flaggedForIndicatorReview": len(gov.get("flagged_for_indicator_review", []) or []),
        "flaggedSchemaGaps": len(gov.get("flagged_schema_gaps", []) or []),
        "proposedSchemaExtensions": len(gov.get("proposed_schema_extensions", []) or []),
        "parkedReviewDebt": _parked_review_debt(topic),
    }


def _parked_review_debt(topic: dict) -> dict | None:
    """Reverse staleness summary for the parked queue (None if engine lacks it)."""
    try:
        engine = _import_from_repo("engine")
        if not hasattr(engine, "parked_review_status"):
            return None
        status = engine.parked_review_status(topic)
        return {
            "parkedTotal": status["parked_total"],
            "dueCount": status["due_count"],
            "reviewDebtRatio": status["review_debt_ratio"],
            "oldestDueDays": status["oldest_due_days"],
            "schemaFingerprint": status["schema_fingerprint"],
        }
    except Exception:
        return None


def _find_indicator(topic: dict, indicator_id: str) -> tuple[dict | None, str | None]:
    indicators = topic.get("indicators", {}) or {}
    for tier, items in (indicators.get("tiers", {}) or {}).items():
        for item in items or []:
            if isinstance(item, dict) and item.get("id") == indicator_id:
                return item, tier
    for item in indicators.get("anti_indicators", []) or []:
        if isinstance(item, dict) and item.get("id") == indicator_id:
            return item, "anti_indicators"
    return None, None


def _indicator_brief(indicator: dict, tier: str) -> dict:
    return {
        "id": indicator.get("id"),
        "tier": tier,
        "desc": indicator.get("desc"),
        "status": indicator.get("status", "NOT_FIRED"),
        "posteriorEffect": indicator.get("posteriorEffect"),
        "likelihoods": indicator.get("likelihoods"),
        "lr_range": indicator.get("lr_range"),
        "observable": indicator.get("observable"),
        "n_firings": indicator.get("n_firings", 0),
        "causal_event_id": indicator.get("causal_event_id"),
    }


def _normalize_transition(transition: str) -> str:
    normalized = (transition or "").strip().upper()
    if normalized not in _ALLOWED_TRANSITIONS:
        raise ValueError(
            f"transition must be one of {sorted(_ALLOWED_TRANSITIONS)}, got {transition!r}"
        )
    return normalized


def _evidence_entry(evidence: dict, default_tag: str = "EVENT") -> dict:
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be a JSON object")
    text = (
        evidence.get("text")
        or evidence.get("claim")
        or evidence.get("headline")
        or evidence.get("title")
        or ""
    ).strip()
    if not text:
        raise ValueError("evidence requires text, claim, headline, or title")
    tag = (evidence.get("tag") or default_tag or "EVENT").strip().upper()
    entry = {
        "tag": tag,
        "tags": evidence.get("tags") or [tag],
        "text": text,
        "source": evidence.get("source") or "operator",
        "provenance": evidence.get("provenance") or "OBSERVED",
    }
    for key in (
        "url",
        "note",
        "claim",
        "posteriorImpact",
        "informationChain",
        "causal_event_id",
        "evidence_refs",
        "surfaced_via",
        "scanRound",
        "queryProvenance",
        "time",
        "published",
    ):
        if key in evidence:
            entry[key] = evidence[key]
    # Date evidence by the article's publication, not by when the operator
    # got around to committing it. The engine's add_evidence honors a "time"
    # field and only falls back to now() when it is absent.
    if not entry.get("time") and entry.get("published"):
        entry["time"] = entry["published"]
    return entry


def _schema_gap_record(evidence: dict, reason: str, missing_direction: str) -> dict:
    return {
        "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "headline": evidence.get("headline") or evidence.get("title") or evidence.get("text", "")[:160],
        "url": evidence.get("url", ""),
        "source": evidence.get("source", ""),
        "claim": evidence.get("claim") or evidence.get("text", ""),
        "missing_direction": missing_direction or reason,
        "matcher_reason": reason,
        "submitted_via": "nrol_ao_mcp",
    }


def _commit_schema_gap(slug: str, evidence: dict, reason: str, missing_direction: str) -> dict:
    engine = _import_from_repo("engine")
    pipeline = _import_from_repo("framework.pipeline")
    before = _posteriors(engine.load_topic(slug))
    record = _schema_gap_record(evidence, reason, missing_direction)
    result = pipeline.log_schema_gap(slug, record)
    topic = result["topic"]
    return {
        "slug": slug,
        "transition": "SCHEMA_GAP",
        "committed": True,
        "posteriors_before": before,
        "posteriors_after": _posteriors(topic),
        "schema_gap": result["schema_gap"],
        "topic": _topic_summary(topic),
    }


def _summarize_pipeline_result(result: dict, include_topic: bool = False) -> dict:
    out = {k: v for k, v in result.items() if k != "topic"}
    topic = result.get("topic")
    if isinstance(topic, dict):
        out["topic_summary"] = _topic_summary(topic)
        if include_topic:
            out["topic"] = topic
    return out


def _compact_query(value: str, max_len: int = 420) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_len].strip()


def _topic_query(topic: dict, channel: str, window_label: str) -> str:
    meta = topic.get("meta", {}) or {}
    title = meta.get("title") or meta.get("slug") or ""
    question = meta.get("question") or meta.get("statement") or meta.get("description") or ""
    if channel == "wildcard":
        actors = []
        for actor in (topic.get("actorModel", {}).get("actors", {}) or {}).values():
            if isinstance(actor, dict):
                actors.append(actor.get("name") or "")
        actor_text = " ".join(a for a in actors if a)
        return _compact_query(f"{title} {actor_text} latest developments news")
    hyp = (topic.get("model", {}).get("hypotheses") or {}).get(channel, {}) or {}
    if isinstance(hyp, dict):
        label = hyp.get("label") or hyp.get("description") or hyp.get("desc") or ""
    else:
        label = str(hyp)
    return _compact_query(f"{title} {question} {channel} {label} latest news {window_label}")


def _search_query_specs(topic: dict, window_label: str) -> list[dict]:
    """Return explicit web-search channels for an MCP-owned scan."""
    specs = []
    for channel in (topic.get("model", {}).get("hypotheses") or {}).keys():
        specs.append({"channel": channel, "query": _topic_query(topic, channel, window_label)})
    specs.append({"channel": "wildcard", "query": _topic_query(topic, "wildcard", window_label)})

    for idx, raw in enumerate(topic.get("searchQueries") or [], start=1):
        if isinstance(raw, dict):
            query = raw.get("query") or raw.get("q") or raw.get("text") or ""
            label = raw.get("channel") or raw.get("label") or f"searchQueries:{idx:02d}"
        else:
            query = str(raw or "")
            label = f"searchQueries:{idx:02d}"
        query = (
            str(query)
            .replace("{window}", window_label)
            .replace("{window_label}", window_label)
            .replace("{date}", window_label)
        )
        query = _compact_query(query)
        if query:
            specs.append({"channel": str(label), "query": query})
    return specs


def _search_query_text(raw: Any) -> str:
    if isinstance(raw, dict):
        raw = raw.get("query") or raw.get("q") or raw.get("text") or ""
    return re.sub(r"\s+", " ", str(raw or "")).strip()


def _normalize_search_queries(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out = []
    for value in values or []:
        text = _search_query_text(value)
        if text:
            out.append(text)
    return out


def _search_query_key(value: Any) -> str:
    return _search_query_text(value).casefold()


def _apply_search_query_delta(
    current: list[Any], add_queries: list[str], remove_queries: list[str]
) -> list[Any]:
    remove_keys = {_search_query_key(q) for q in remove_queries}
    proposed = [q for q in (current or []) if _search_query_key(q) not in remove_keys]
    existing = {_search_query_key(q) for q in proposed}
    for query in add_queries:
        key = _search_query_key(query)
        if key and key not in existing:
            proposed.append(query)
            existing.add(key)
    return proposed


def _query_terms(query: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", query)


def _topic_schema_terms(topic: dict | None) -> set[str]:
    if not topic:
        return set()
    terms: set[str] = set()
    indicators = topic.get("indicators", {}) or {}
    for tier, items in (indicators.get("tiers", {}) or {}).items():
        for ind in items or []:
            if not isinstance(ind, dict):
                continue
            parts = [
                ind.get("id", ""),
                ind.get("desc", ""),
                ind.get("posteriorEffect", ""),
                ind.get("causal_event_id", ""),
                ind.get("shape", ""),
            ]
            observable = ind.get("observable") or {}
            if isinstance(observable, dict):
                parts.extend(str(v) for v in observable.values())
            for term in _query_terms(" ".join(str(p) for p in parts if p)):
                if len(term) >= 4:
                    terms.add(term.casefold())
    for ind in indicators.get("anti_indicators", []) or []:
        if isinstance(ind, dict):
            for term in _query_terms(" ".join(str(v) for v in ind.values())):
                if len(term) >= 4:
                    terms.add(term.casefold())
    return terms


def _coverage_axes(queries: list[str], topic: dict | None = None) -> dict[str, bool]:
    text = " ".join(queries).casefold()

    def any_term(*terms: str) -> bool:
        return any(term.casefold() in text for term in terms)

    schema_terms = _topic_schema_terms(topic)
    schema_hits = sorted(term for term in schema_terms if term in text)
    return {
        "core_event": bool(queries) and any(len(_query_terms(q)) >= 3 for q in queries),
        "escalation": any_term(
            "closure", "closed", "attack", "seizure", "blockade", "failure",
            "breakdown", "decline", "risk", "sanction", "enforcement",
        ),
        "deescalation": any_term(
            "reopen", "reopening", "agreement", "deal", "talks", "diplomacy",
            "recovery", "resume", "normalization", "confirmed",
        ),
        "measurement": any_term(
            "metric", "data", "baseline", "percent", "volume", "index",
            "rate", "count", "survey", "report", "lloyd", "eia",
        ),
        "institutional": any_term(
            "reuters", "ap", "official", "agency", "ministry", "court",
            "regulator", "military", "centcom", "eia", "lloyd",
        ),
        "schema": bool(schema_hits) or any_term(
            "indicator", "threshold", "observable", "signal", "metric",
            "event", "source", "evidence",
        ),
    }


def _validate_search_query_set(
    current: list[Any],
    add_queries: list[str],
    remove_queries: list[str],
    topic: dict | None = None,
) -> dict:
    current_texts = [_search_query_text(q) for q in current or [] if _search_query_text(q)]
    proposed_raw = _apply_search_query_delta(current or [], add_queries, remove_queries)
    proposed = [_search_query_text(q) for q in proposed_raw if _search_query_text(q)]
    errors: list[str] = []
    warnings_out: list[str] = []

    if not add_queries and not remove_queries:
        errors.append("proposal must add or remove at least one query")
    if len(proposed) < 3:
        warnings_out.append("resulting query set has fewer than 3 queries")
    if len(proposed) > 25:
        errors.append("resulting query set exceeds 25 queries")

    existing_keys = {_search_query_key(q) for q in current_texts}
    for query in remove_queries:
        if _search_query_key(query) not in existing_keys:
            warnings_out.append(f"remove query not present: {query}")

    seen = set()
    for query in proposed:
        key = _search_query_key(query)
        if key in seen:
            errors.append(f"duplicate query after update: {query}")
        seen.add(key)
        terms = _query_terms(query)
        if len(query) > 220:
            errors.append(f"query exceeds 220 chars: {query[:80]}")
        elif len(query) > 180:
            warnings_out.append(f"query is long (>180 chars): {query[:80]}")
        if len(terms) < 3:
            errors.append(f"query has fewer than 3 terms: {query}")
        elif len(terms) > 14:
            warnings_out.append(f"query has more than 14 terms: {query[:80]}")
        if '"' in query:
            warnings_out.append(f"quoted query may overfit a headline: {query[:80]}")

    site_count = sum(1 for query in proposed if "site:" in query.casefold())
    if proposed and site_count == len(proposed):
        errors.append("all queries are site-filtered; at least one broad query is required")
    elif proposed and site_count > max(1, len(proposed) // 2):
        warnings_out.append("site-filtered queries dominate the set")

    axes = _coverage_axes(proposed, topic)
    missing_axes = [axis for axis, covered in axes.items() if not covered]
    for axis in missing_axes:
        warnings_out.append(f"coverage axis missing or weak: {axis}")

    return {
        "current_queries": current_texts,
        "proposed_queries": proposed,
        "add_queries": add_queries,
        "remove_queries": remove_queries,
        "errors": errors,
        "warnings": warnings_out,
        "coverage_axes": axes,
        "missing_axes": missing_axes,
        "schema_terms_matched": sorted(
            term for term in _topic_schema_terms(topic)
            if term in " ".join(proposed).casefold()
        )[:40],
    }


def _parse_search_query_red_team_review(text: str) -> dict:
    text = text or ""
    verdict_match = re.search(r"(?im)^VERDICT:\s*(APPROVE|REVISE|REJECT)\b", text)
    verdict = verdict_match.group(1).upper() if verdict_match else "REVISE"

    def field(name: str) -> str:
        match = re.search(rf"(?ims)^{name}:\s*(.*?)(?=^[A-Z_]+:|\Z)", text)
        return match.group(1).strip() if match else ""

    return {
        "verdict": verdict,
        "coverage": field("COVERAGE"),
        "neutrality": field("NEUTRALITY"),
        "overfitting": field("OVERFITTING"),
        "noise": field("NOISE"),
        "schema_axis": field("SCHEMA_AXIS"),
        "recommendation": field("RECOMMENDATION"),
        "raw": text,
    }


def _build_search_query_red_team_prompt(proposal: dict, topic: dict, validation: dict) -> str:
    indicators = [
        _indicator_brief(ind, tier)
        for tier, items in (topic.get("indicators", {}).get("tiers", {}) or {}).items()
        for ind in items or []
    ] + [
        _indicator_brief(ind, "anti_indicators")
        for ind in topic.get("indicators", {}).get("anti_indicators", []) or []
    ]
    meta = topic.get("meta", {}) or {}
    return "\n".join([
        "Red-team this proposed durable searchQueries update for an NROL-AO topic.",
        "",
        "Search queries are retrieval hooks, not evidence claims. They should improve recall",
        "without overfitting to one headline, one source, or one favored hypothesis.",
        "",
        f"TOPIC: {meta.get('slug', '')} — {meta.get('title', '')}",
        f"QUESTION: {meta.get('question', '')}",
        "",
        "HYPOTHESES:",
        json.dumps(topic.get("model", {}).get("hypotheses", {}), indent=2, ensure_ascii=True),
        "",
        "INDICATORS / SCHEMA TERMS:",
        json.dumps(indicators, indent=2, ensure_ascii=True),
        "",
        "CURRENT QUERIES:",
        json.dumps(validation.get("current_queries", []), indent=2, ensure_ascii=True),
        "",
        "PROPOSED ADD:",
        json.dumps(proposal.get("add_queries", []), indent=2, ensure_ascii=True),
        "",
        "PROPOSED REMOVE:",
        json.dumps(proposal.get("remove_queries", []), indent=2, ensure_ascii=True),
        "",
        "OPERATOR RATIONALE:",
        str(proposal.get("rationale", "")),
        "",
        "OPERATOR COVERAGE GAPS:",
        json.dumps(proposal.get("coverage_gaps", []), indent=2, ensure_ascii=True),
        "",
        "DETERMINISTIC LINT CONTEXT (advisory unless errors are non-empty):",
        json.dumps(validation, indent=2, ensure_ascii=True),
        "",
        "Review adversarially. Approve only if the resulting durable query set is",
        "broad, neutral, robust, and likely to catch evidence relevant to the topic's",
        "hypotheses and indicators. Do not reject merely because deterministic coverage",
        "labels are imperfect; use topic understanding.",
        "",
        "Return exactly:",
        "VERDICT: APPROVE | REVISE | REJECT",
        "COVERAGE: <are the main causal/source/measurement/schema axes covered?>",
        "NEUTRALITY: <does the query set avoid hunting only one favored outcome?>",
        "OVERFITTING: <headline/date/site/source brittleness, if any>",
        "NOISE: <expected junk volume or ambiguity risk>",
        "SCHEMA_AXIS: <does the set cover high-value unfired indicators/observables?>",
        "RECOMMENDATION: <specific edits or approval rationale>",
    ])


def _deterministic_search_query_review(proposal: dict, topic: dict) -> dict:
    add_queries = _normalize_search_queries(proposal.get("add_queries"))
    remove_queries = _normalize_search_queries(proposal.get("remove_queries"))
    validation = _validate_search_query_set(
        topic.get("searchQueries") or [], add_queries, remove_queries, topic=topic
    )
    coverage_gaps = proposal.get("coverage_gaps") or []
    if validation["errors"]:
        verdict = "REJECT"
    else:
        verdict = "CHECK"
    risks = []
    if validation["errors"]:
        risks.extend(validation["errors"])
    if validation["warnings"]:
        risks.extend(validation["warnings"])
    if coverage_gaps and not add_queries:
        verdict = "REVISE" if verdict == "CHECK" else verdict
        risks.append("coverage gaps were stated but no new queries were added")
    return {
        "verdict": verdict,
        "deterministic_only": True,
        "risk": "; ".join(risks[:6]) or "no blocking risks found",
        "coverage": validation["coverage_axes"],
        "missing_axes": validation["missing_axes"],
        "neutrality": (
            "Queries are treated as retrieval hooks; red team found no deterministic "
            "one-sidedness signal."
        ),
        "overfitting": [
            w for w in validation["warnings"]
            if "headline" in w or "quoted" in w or "site-filtered" in w
        ],
        "noise": [
            w for w in validation["warnings"]
            if "long" in w or "more than 14" in w
        ],
        "recommendation": (
            "Rejected by deterministic structural validation."
            if verdict == "REJECT"
            else "Send to local-model red team for substantive retrieval review."
        ),
        "validation": validation,
    }


def _ddgs_hits(
    ddgs,
    method: str,
    query: str,
    limit: int,
    *,
    timelimit: str | None = None,
) -> list[dict]:
    search = getattr(ddgs, method, None)
    if search is None:
        return []
    kwargs: dict[str, Any] = {"max_results": limit}
    if timelimit:
        kwargs["timelimit"] = timelimit
    try:
        return list(search(query, **kwargs))
    except TypeError:
        # Older DDGS variants may not accept timelimit; retry without it.
        try:
            return list(search(query, max_results=limit))
        except TypeError:
            return list(search(query))[:limit]


def _ddg_timelimit_for_window(hours: float) -> str:
    """Map window hours to DDGS timelimit code (d/w/m/y).

    Round UP to the next coarser bucket so retrieval is never narrower
    than the freshness gate that runs after fetch.
    """
    try:
        h = float(hours)
    except (TypeError, ValueError):
        h = 24.0
    if h <= 24:
        return "d"
    if h <= 24 * 7:
        return "w"
    if h <= 24 * 31:
        return "m"
    return "y"


def _scan_search_window(topic: dict, *, tempo_floor_hours: int) -> dict:
    """News-scan window with 1-day overlap and a 30-day first-scan default.

    Wraps framework.news_mutation.compute_time_window so:
      - First scan (no lastScanned) opens a 30-day month for wide initial
        coverage, instead of the tempo floor.
      - Subsequent scans use a 24h buffer so consecutive scans overlap by
        ~1 day; dedupe handles the overlap.
    """
    mutation = _import_from_repo("framework.news_mutation")
    last_scanned = ((topic.get("meta") or {}).get("lastScanned") or "").strip()
    if not last_scanned:
        hours = float(30 * 24)
        return {
            "hours": hours,
            "label": "last 30 days",
            "reason": "no prior lastScanned — defaulting scan window to last 30 days",
            "capped": False,
        }
    return mutation.compute_time_window(
        topic,
        tempo_floor_hours=tempo_floor_hours,
        buffer_hours=24.0,
    )


def _dedupe_search_hits(hits: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = (hit.get("href") or hit.get("url") or "").strip()
        title = (hit.get("title") or "").strip().lower()
        compact_title = re.sub(r"\s+", " ", title)[:160]
        key = _canonical_article_url(url) or f"title::{compact_title}"
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped


def _search_web_articles(
    query: str,
    channel: str,
    max_results: int,
    *,
    timelimit: str | None = None,
) -> list[dict]:
    """Server-side search backend for MCP-owned news scans.

    DDGS text search alone is shallow and can miss relevant mainstream news
    articles behind aggregator/SEO results. Pull from the news vertical when
    available, then add small source-qualified searches for high-signal
    international outlets before the normal scan dedupe/freshness gates run.

    timelimit is a DDGS code (d/w/m/y) computed from the topic's scan window
    so search engines do retrieval-time freshness filtering; the post-fetch
    freshness gate still does the precise cutoff.
    """
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError("ddgs package not installed; install ddgs for MCP-side news scans") from exc

    limit = max(1, min(int(max_results), _MAX_SEARCH_RESULTS_PER_CHANNEL))
    aggregate_limit = max(limit, min(limit * 4, _MAX_AGGREGATED_SEARCH_RESULTS_PER_CHANNEL))
    raw_hits = []
    source_domains = (
        ()
        if "site:" in query.lower()
        else _SOURCE_QUALIFIED_SEARCH_DOMAINS
    )
    with DDGS() as ddgs:
        for method, search_query, search_limit in [
            ("text", query, limit),
            ("news", query, limit),
            *[
                ("text", f"{query} site:{domain}", 2)
                for domain in source_domains
            ],
        ]:
            for hit in _ddgs_hits(ddgs, method, search_query, search_limit, timelimit=timelimit):
                if isinstance(hit, dict):
                    hit = dict(hit)
                    hit.setdefault("_search_backend", method)
                    hit.setdefault("_search_query", search_query)
                    hit.setdefault("_search_timelimit", timelimit or "")
                    raw_hits.append(hit)
    hits = _dedupe_search_hits(raw_hits)[:aggregate_limit]
    articles = []
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for rank, hit in enumerate(hits, start=1):
        headline = (hit.get("title") or "").strip()
        url = (hit.get("href") or hit.get("url") or "").strip()
        body = (hit.get("body") or hit.get("snippet") or "").strip()
        published = (
            hit.get("date")
            or hit.get("published")
            or hit.get("published_at")
            or hit.get("time")
            or ""
        )
        if not headline and not url:
            continue
        source = ""
        try:
            from urllib.parse import urlparse
            source = urlparse(url).netloc.replace("www.", "")
        except Exception:
            pass
        article = {
            "headline": headline or url,
            "url": url,
            "canonical_url": _canonical_article_url(url),
            "source": source or "web_search",
            "relevance": body[:500] or f"Surfaced by server-side search channel {channel}.",
            "query": query,
            "channel": channel,
            "retrieved_at": retrieved_at,
            "search_backend": hit.get("_search_backend") or "text",
            "search_rank": rank,
        }
        if published:
            article["date"] = str(published)
            article["published"] = str(published)
        articles.append(article)
    return articles


def _canonical_article_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "https").lower()
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        query_pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            k_lower = key.lower()
            if k_lower.startswith("utm_") or k_lower in _TRACKING_QUERY_KEYS:
                continue
            query_pairs.append((key, value))
        query = urlencode(sorted(query_pairs), doseq=True)
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/")
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return raw


def _article_scan_key(article: dict) -> str:
    import re

    art = article.get("article", article) if isinstance(article, dict) else {}
    url = _canonical_article_url(art.get("canonical_url") or art.get("url") or "")
    if url:
        return f"url::{url}"
    headline = (art.get("headline") or art.get("title") or "").strip().lower()
    headline = re.sub(r"\s+", " ", headline)[:160]
    return f"hl::{headline}" if headline else ""


def _prior_article_keys(topic: dict) -> set[str]:
    keys = set()
    for entry in topic.get("evidenceLog", []) or []:
        if not isinstance(entry, dict):
            continue
        key = _article_scan_key({
            "url": entry.get("url") or "",
            "headline": entry.get("headline") or entry.get("text") or "",
        })
        if key:
            keys.add(key)
    return keys


def _normalize_scan_datetime(value: str) -> datetime | None:
    dt = _parse_iso_date(str(value or ""))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _filter_scan_articles(
    topic: dict,
    articles: list[dict],
    window: dict,
    *,
    drop_old_dated: bool = True,
) -> tuple[list[dict], dict]:
    """Keep only fresh or undated-but-not-seen scan results before matching."""
    now = datetime.now(timezone.utc)
    try:
        hours = max(1.0, float((window or {}).get("hours") or 24.0))
    except (TypeError, ValueError):
        hours = 24.0
    cutoff = now - timedelta(hours=hours)
    prior_keys = _prior_article_keys(topic)
    stats = {
        "input": len(articles or []),
        "kept": 0,
        "dated_in_window": 0,
        "undated_kept": 0,
        "old_dated_dropped": 0,
        "old_dated_kept_for_fetch": 0,
        "prior_seen_dropped": 0,
        "cutoff": cutoff.isoformat(timespec="seconds"),
    }
    kept = []
    for article in articles or []:
        key = _article_scan_key(article)
        if key and key in prior_keys:
            stats["prior_seen_dropped"] += 1
            continue
        art = article.get("article", article) if isinstance(article, dict) else {}
        published = art.get("published") or art.get("published_at") or art.get("date") or art.get("time") or ""
        published_dt = _normalize_scan_datetime(published)
        if published_dt is not None:
            if published_dt < cutoff:
                if drop_old_dated:
                    stats["old_dated_dropped"] += 1
                    continue
                art["freshness"] = "dated_outside_window_pending_fetch"
                art["freshness_cutoff"] = stats["cutoff"]
                stats["old_dated_kept_for_fetch"] += 1
                kept.append(article)
                continue
            art["freshness"] = "dated_in_window"
            art["freshness_cutoff"] = stats["cutoff"]
            stats["dated_in_window"] += 1
        else:
            art["freshness"] = "undated_not_previously_seen"
            art["freshness_cutoff"] = stats["cutoff"]
            stats["undated_kept"] += 1
        kept.append(article)
    stats["kept"] = len(kept)
    return kept, stats


def _run_debate(
    topic: dict,
    articles: list,
    decisions: list,
    news,
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout_sec: int,
    store=None,
    job_id: str = "",
    slug: str = "",
) -> tuple[dict, dict]:
    """3-stage deliberation (advocate / rebut / jury) over candidates (FIRE/OBSERVE/PARK).

    Returns (jury_overrides, debate_packet) where jury_overrides maps
    idx -> {"action": {...}, "rationale": str}.
    Never raises — a failed stage returns no overrides and reports why.
    """
    packet: dict[str, Any] = {
        "candidates": 0,
        "parks": sum(1 for c in decisions if c.get("action", {}).get("kind") == "PARK"),
        "schema_gaps": sum(
            1 for c in decisions if c.get("action", {}).get("kind") == "SCHEMA_GAP"
        ),
        "advocate_proposals": 0,
        "jury_verdicts": {},
    }
    try:
        candidates = _debate_candidates_with_reasons(news, decisions)
        packet["candidates"] = len(candidates)
        if not candidates:
            packet["note"] = "no candidates to deliberate"
            return {}, packet

        def _stage(name: str, prompt: str, system: str) -> str:
            response = llama_client.chat(
                prompt,
                system_prompt=system,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
                disable_thinking=False,  # Deliberation requires reasoning mode enabled!
            )
            text = response.get("text", "")
            if store is not None:
                store.record(
                    job_id,
                    "running",
                    task="debate",
                    slug=slug,
                    model=response.get("model"),
                    summary={"stage": name, "output_chars": len(text),
                             "finish_reason": response.get("finish_reason")},
                    response=text,
                )
            return text

        adv_text = _stage(
            "advocate",
            news.build_advocate_prompt(topic, articles, candidates),
            "You are the ADVOCATE in the NROL-AO debate. Return only ADVOCATE "
            "blocks in the requested format.",
        )
        advocate_moves = news.parse_advocate_output(adv_text)
        packet["advocate_proposals"] = len(advocate_moves)
        packet["argue_moves"] = len([m for m in advocate_moves if m.get("verdict") in ("COMMIT", "ARGUE_MOVE")])
        if not advocate_moves:
            packet["note"] = "advocate found no defensible proposals"
            return {}, packet

        strict_reasons = news.get_strict_reasons_map(decisions)
        reb_text = _stage(
            "rebut",
            news.build_rebut_prompt(topic, articles, advocate_moves, strict_reasons),
            "You are the REBUTTAL in the NROL-AO debate. Return only REBUT "
            "blocks in the requested format.",
        )
        rebuts = news.parse_rebut_output(reb_text)
        packet["rebuttals"] = len(rebuts)

        jury_text = _stage(
            "jury",
            news.build_jury_prompt(topic, articles, advocate_moves, rebuts),
            "You are the JURY in the NROL-AO debate. Return only JURY blocks "
            "in the requested format.",
        )
        jury = news.parse_jury_output(jury_text)
        packet["jury_verdicts"] = {
            str(i): v.get("verdict_raw", "") for i, v in jury.items()
        }
        overrides = {
            idx: {"action": v["action"], "rationale": v.get("rationale", "")}
            for idx, v in jury.items()
        }
        decisions_by_idx = {str(d.get("idx")): d for d in decisions}
        packet["rescued"] = sum(
            1 for idx, v in overrides.items()
            if (decisions_by_idx.get(str(idx), {}).get("action", {}).get("kind") == "PARK"
                and v["action"]["kind"] in ("FIRE", "OBSERVE"))
        )
        packet["schema_gap_rescued"] = sum(
            1 for idx, v in overrides.items()
            if (
                decisions_by_idx.get(str(idx), {}).get("action", {}).get("kind") == "SCHEMA_GAP"
                and v["action"]["kind"] in ("FIRE", "OBSERVE", "PARK", "IGNORE")
            )
        )
        return overrides, packet
    except Exception as exc:
        packet["error"] = str(exc)
        return {}, packet


def _debate_candidates_with_reasons(news, matcher_decisions: list) -> list:
    """Return debate candidates, including SCHEMA_GAP rows."""
    out = list(news.get_candidates_with_reasons(matcher_decisions))
    existing = {str(c.get("idx")) for c in out}
    for d in matcher_decisions:
        if (d.get("action") or {}).get("kind") != "SCHEMA_GAP":
            continue
        if str(d.get("idx")) in existing:
            continue
        desc = (d.get("action") or {}).get("description") or ""
        action_raw = f"SCHEMA_GAP {desc}".strip()
        out.append({
            "idx": d.get("idx"),
            "claim": d.get("claim") or "",
            "action_raw": action_raw,
            "reason": d.get("reason") or desc,
        })
    return out


def _apply_jury_overrides(decisions: list, jury_overrides: dict) -> list:
    """Fold jury verdicts into the decision list."""
    if not jury_overrides:
        return decisions
    effective = []
    for d in decisions:
        idx = d.get("idx")
        override = jury_overrides.get(idx) or jury_overrides.get(str(idx))
        if override:
            override_action = override["action"]
            nd = dict(d)
            if override_action["kind"] == "COMMIT":
                pass  # keep original action
            else:
                nd["action"] = override_action
            nd["jury_override"] = True
            nd["reason"] = (
                "jury: " + (override.get("rationale") or "override verdict")
            )[:500]
            effective.append(nd)
        else:
            effective.append(d)
    return effective


def _require_deliberation(
    kind: str, deliberation: dict | None, waiver: str
) -> tuple[str | None, dict]:
    """The deliberation gate. Posterior-moving actions (FIRE/OBSERVE) are
    refused unless they carry a debate record or an explicit waiver — and
    whichever they carry is stamped onto the evidence and shown at the Loom
    approval. Deliberation is a capability constraint of this server, not a
    convention of its callers: skipping it silently is not expressible.

    Returns (refusal_message_or_None, stamp_dict_for_evidence).
    """
    if kind not in {"FIRE", "OBSERVE"}:
        return None, {}
    if deliberation:
        return None, {"deliberation": deliberation}
    waiver = (waiver or "").strip()
    if waiver:
        return None, {"deliberationWaiver": waiver}
    return (
        f"{kind} commit refused: no deliberation record. Posterior-moving "
        "actions require the advocate/rebut/jury debate — run "
        "deliberate_candidates (or run_news_scan / review_parked, which "
        "debate by default) and attach its record as deliberation=..., or "
        "pass an explicit no_deliberation_reason. A waiver is recorded on "
        "the evidence entry and shown in the Loom approval prompt.",
        {},
    )


def _deliberation_stamp_from_debate(decision: dict, debate_packet: dict | None) -> dict:
    """Per-candidate deliberation record for a proposal, from a debate run.

    A debate that errored or saw zero candidates yields NO record — an empty
    debate must not mint gate-passing stamps.
    """
    if not debate_packet or debate_packet.get("error") or not debate_packet.get("candidates"):
        return {}
    idx = decision.get("idx")
    verdict = (debate_packet.get("jury_verdicts") or {}).get(str(idx), "")
    record = {
        "jury_verdict": verdict or "NO_BLOCK",
        "rationale": (decision.get("reason") or "")[:300],
        "candidates_debated": debate_packet.get("candidates", 0),
        "rebuttals": debate_packet.get("rebuttals", 0),
    }
    return record


_MATCHER_RELEVANCE_OVERLAY = """

## MCP relevance calibration overlay

Be strict about posterior movement, but liberal about relevance preservation.
FIRE and OBSERVE still require literal threshold/metric support and directional
alignment. IGNORE is only for clearly off-topic articles, duplicate-only noise,
or pure forecast/odds/opinion with no reported factual development.

If an article is plausibly causally related to the topic question, hypotheses,
or resolution pathway but does not meet an indicator, choose PARK rather than
IGNORE. If the article is directionally meaningful but no indicator — including
anti-indicators — covers the direction with a fitting observable or binary
threshold, choose SCHEMA_GAP. When uncertain between IGNORE and PARK/SCHEMA_GAP,
preserve the article with PARK or SCHEMA_GAP so the advocate/rebut/jury pass can
deliberate it.

Anti-indicators are pre-committed falsification targets: directional indicators
whose likelihoods are authored so firing suppresses their target hypothesis
(the target H carries the lowest LR, enforced at design time by the inversion
lint). They are a first-class FIRE target, not a SCHEMA_GAP fallback. When an
article reports evidence whose direction matches an anti-indicator's threshold
and the indicator's LR direction is consistent with the article — e.g. transit
recovery firing anti_h4_transit_recovery_toward_baseline, which suppresses H4
and lifts H1/H2 — FIRE the anti-indicator, or OBSERVE it if it carries an
observable block. Anti-indicators move posteriors through the same directional
LR machinery as tier indicators; treat them as FIRE/OBSERVE targets whenever
their threshold is met and direction aligns, not as evidence the schema lacks
coverage for that direction.

Indirect causal pathways count as topic-relevant. For example, ceasefire
compliance, attacks, sanctions implementation, diplomatic breakdown/resumption,
shipping insurance, escorts, maritime notices, or traffic-flow reports can be
relevant even when the headline does not literally repeat the resolution text.
"""


def _build_matcher_prompt(news, topic: dict, articles: list) -> str:
    """Build matcher prompt with MCP-side relevance-preservation guidance."""
    return news.build_matcher_prompt(topic, articles) + _MATCHER_RELEVANCE_OVERLAY


def _split_safe_policy_decisions(news, articles: list, decisions: list) -> tuple[list, list, dict]:
    """Return (safe_to_apply, posterior_moving_to_propose, audit).

    Safe policy means apply_decisions must never see FIRE/OBSERVE. A previous
    safe-scan path filtered only canonical decisions, then appended duplicate
    decisions unfiltered; posterior-moving duplicates could therefore bypass
    the proposal gate. This splitter classifies every grouped decision and
    keeps movement on the proposal side.
    """
    canonical, duplicate_map = news.group_decisions_by_duplicates(articles, decisions)
    safe_to_apply: list[dict] = []
    to_propose: list[dict] = []
    audit = {"freshness_downgrades": []}

    def freshness_gated(decision: dict) -> dict:
        kind = (decision.get("action") or {}).get("kind")
        if kind not in {"FIRE", "OBSERVE"}:
            return decision
        idx_int = _to_int_idx(decision.get("idx"))
        art = articles[idx_int - 1] if 0 < idx_int <= len(articles) else {}
        inner = art.get("article", art) if isinstance(art, dict) else {}
        if inner.get("freshness") != "undated_not_previously_seen":
            return decision
        downgraded = dict(decision)
        downgraded["action"] = {"kind": "PARK"}
        downgraded["freshness_gate"] = "undated_posterior_mover_downgraded"
        reason = downgraded.get("reason") or downgraded.get("claim") or ""
        downgraded["reason"] = (
            "freshness gate: undated search result cannot file FIRE/OBSERVE; "
            f"original action was {kind}. {reason}"
        )[:500]
        audit["freshness_downgrades"].append({
            "idx": decision.get("idx"),
            "original_action": decision.get("action"),
            "replacement_action": downgraded["action"],
            "claim": decision.get("claim") or "",
            "reason": downgraded["reason"],
        })
        return downgraded

    for d in canonical:
        d = freshness_gated(d)
        kind = (d.get("action") or {}).get("kind")
        dups = [freshness_gated(dup) for dup in duplicate_map.get(d.get("idx"), [])]
        if kind in {"FIRE", "OBSERVE"}:
            d = dict(d)
            d["_duplicate_decisions"] = dups
            to_propose.append(d)
        elif kind in {"PARK", "SCHEMA_GAP", "IGNORE"}:
            safe_to_apply.append(d)
            for dup in dups:
                dup_kind = (dup.get("action") or {}).get("kind")
                if dup_kind in {"FIRE", "OBSERVE"}:
                    to_propose.append(dup)
                elif dup_kind in {"PARK", "SCHEMA_GAP", "IGNORE"}:
                    safe_to_apply.append(dup)

    audit["safe_to_apply_count"] = len(safe_to_apply)
    audit["to_propose_count"] = len(to_propose)
    return safe_to_apply, to_propose, audit


def _fetch_article_payload(url: str, max_chars: int, timeout_sec: float = 15.0) -> dict:
    """Fetch readable article text and best-effort publication metadata."""
    if not url:
        return {}
    try:
        import trafilatura

        html = None
        try:
            html = trafilatura.fetch_url(url)
        except Exception:
            html = None
        if not html:
            with httpx.Client(
                timeout=timeout_sec,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; NROL-AO scan)"},
            ) as client:
                r = client.get(url)
                r.raise_for_status()
                html = r.text

        payload: dict[str, Any] = {}
        try:
            metadata = trafilatura.extract_metadata(html)
            if metadata is not None:
                for src_key, out_key in (
                    ("date", "published"),
                    ("title", "metadata_title"),
                    ("sitename", "metadata_source"),
                    ("hostname", "metadata_host"),
                    ("url", "metadata_url"),
                ):
                    value = getattr(metadata, src_key, None)
                    if value:
                        payload[out_key] = str(value)
        except Exception:
            pass

        text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
        text = " ".join(text.split())
        if text:
            payload["excerpt"] = text[:max_chars]
        return payload
    except Exception as exc:
        return {"fetch_error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def _fetch_article_excerpt(url: str, max_chars: int, timeout_sec: float = 15.0) -> str:
    """Fetch a URL and extract readable article text (trafilatura).

    Search snippets are ~500 chars of SEO text; the numeric values OBSERVE
    decisions need usually live in the article body. Returns "" on any
    failure — a missing excerpt degrades one article, never the scan.
    """
    if not url:
        return ""
    try:
        import trafilatura

        # trafilatura's own fetcher gets past bot walls that refuse plain
        # httpx requests (observed: 403 for httpx with a browser UA, 200 for
        # fetch_url on the same article).
        html = None
        try:
            html = trafilatura.fetch_url(url)
        except Exception:
            html = None
        if not html:
            with httpx.Client(
                timeout=timeout_sec,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; NROL-AO scan)"},
            ) as client:
                r = client.get(url)
                r.raise_for_status()
                html = r.text
        text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
        text = " ".join(text.split())
        return text[:max_chars]
    except Exception:
        return ""


def _parse_iso_date(value: str) -> datetime | None:
    if not value:
        return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        pass

    try:
        return datetime.fromisoformat(text[:10] + "T00:00:00+00:00")
    except Exception:
        pass

    normalized = re.sub(r"\s+", " ", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    lower = text.lower()
    if "yesterday" in lower:
        return datetime.now(timezone.utc) - timedelta(days=1)
    match = _RELATIVE_DATE_RE.search(lower)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit == "minute":
            delta = timedelta(minutes=amount)
        elif unit == "hour":
            delta = timedelta(hours=amount)
        elif unit == "day":
            delta = timedelta(days=amount)
        elif unit == "week":
            delta = timedelta(weeks=amount)
        elif unit == "month":
            delta = timedelta(days=30 * amount)
        elif unit == "year":
            delta = timedelta(days=365 * amount)
        else:
            return None
        return datetime.now(timezone.utc) - delta
    return None


def _token_overlap(a: str, b: str) -> float:
    import re

    sa = {t for t in re.findall(r"[a-z0-9]{4,}", (a or "").lower())}
    sb = {t for t in re.findall(r"[a-z0-9]{4,}", (b or "").lower())}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))


def _candidate_duplicate_evidence(
    topic: dict,
    article: dict,
    decision: dict,
    window_days: int,
    limit: int,
) -> list[dict]:
    art = article.get("article", article) if isinstance(article, dict) else {}
    art_text = " ".join(
        str(art.get(k) or "") for k in ("headline", "title", "relevance", "excerpt", "body")
    )
    decision_text = " ".join(str(decision.get(k) or "") for k in ("claim", "reason"))
    probe_text = f"{art_text} {decision_text}".strip()
    art_url = (art.get("url") or "").strip()
    art_time = _normalize_scan_datetime(
        str(art.get("published") or art.get("date") or art.get("time") or "")
    )
    rows = []
    for entry in topic.get("evidenceLog", []) or []:
        score = 0.0
        reasons = []
        if art_url and art_url == (entry.get("url") or "").strip():
            score += 1.0
            reasons.append("same_url")
        entry_time = _normalize_scan_datetime(str(entry.get("time") or ""))
        if art_time and entry_time:
            delta = abs((art_time - entry_time).days)
            if delta <= window_days:
                score += max(0.0, 0.4 * (1 - (delta / max(1, window_days))))
                reasons.append(f"within_{window_days}d")
        overlap = _token_overlap(probe_text, str(entry.get("text") or ""))
        if overlap:
            score += overlap
            if overlap >= 0.2:
                reasons.append(f"text_overlap_{overlap:.2f}")
        if score <= 0:
            continue
        rows.append({
            "evidence_id": entry.get("id", ""),
            "time": entry.get("time", ""),
            "source": entry.get("source", ""),
            "url": entry.get("url", ""),
            "text": entry.get("text", ""),
            "posteriorImpact": entry.get("posteriorImpact", ""),
            "score": round(score, 4),
            "reasons": reasons,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[: max(1, min(int(limit), 30))]


def _parse_duplicate_judgment(text: str) -> dict:
    import re

    text = text or ""
    m = re.search(
        r"VERDICT:\s*(DUPLICATE_OF|UNIQUE_EVENT|UNCERTAIN_DUPLICATE)\s*([A-Za-z0-9_\-]*)",
        text,
        re.IGNORECASE,
    )
    verdict = (m.group(1).upper() if m else "UNCERTAIN_DUPLICATE")
    evidence_id = (m.group(2) if m and m.group(2) else "")
    reason = ""
    rm = re.search(r"REASON:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if rm:
        reason = rm.group(1).strip()
    return {"verdict": verdict, "evidence_id": evidence_id, "reason": reason}


def _proposal_suppression_reason(
    topic: dict,
    article: dict,
    decision: dict,
    evidence_id: str,
) -> str:
    """Return a conservative reason to avoid filing an obvious duplicate/no-op proposal."""
    action = decision.get("action", {}) or {}
    kind = action.get("kind", "")
    indicator_id = action.get("indicator_id", "")
    observed_value = action.get("value")
    indicator, _tier = _find_indicator(topic, indicator_id)

    if kind == "OBSERVE" and indicator is not None and observed_value is not None:
        try:
            new_value = float(observed_value)
            old_value = float(indicator.get("lastObservedValue"))
        except (TypeError, ValueError):
            old_value = None
        if old_value is not None and abs(new_value - old_value) <= 1e-9:
            return (
                f"duplicate_observation: {indicator_id} already has "
                f"lastObservedValue={old_value:g}"
            )

    if kind in {"FIRE", "OBSERVE"} and indicator_id:
        # Match the cross-day judge's window (window_days=45, max_candidates=12
        # in _judge_cross_day_duplicate) so the mechanical and semantic screens
        # see the same evidence population — otherwise a candidate just outside
        # 30d is mechanically let through, then suppressed by the semantic
        # check that runs right after it. The mechanical screen only suppresses
        # on strong signals (same_url+score>=1.0+FIRED, or indicator_id in
        # posteriorImpact), so widening the pool raises recall without lowering
        # the suppression bar.
        candidates = [
            row for row in _candidate_duplicate_evidence(
                topic, article, decision, window_days=45, limit=12,
            )
            if row.get("evidence_id") != evidence_id
        ]
        for row in candidates:
            impact = str(row.get("posteriorImpact") or "")
            reasons = set(row.get("reasons") or [])
            already_applied = indicator_id in impact and (
                "FIRED" in impact or "OBSERVE" in impact or "rebind" in impact
            )
            strong_same_article = "same_url" in reasons and row.get("score", 0) >= 1.0
            if already_applied or (
                strong_same_article
                and indicator is not None
                and indicator.get("status") == "FIRED"
            ):
                return (
                    f"duplicate_prior_evidence: {row.get('evidence_id')} "
                    f"score={row.get('score')} reasons={','.join(row.get('reasons') or [])}"
                )

    return ""


def _load_topics(engine, slugs: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Load topics tolerantly: one malformed file must not fail the whole call.

    Returns (topics, skipped) where skipped records files engine.list_topics
    surfaced but load_topic rejected (e.g. manifest.json has no meta section).
    """
    topics = []
    skipped = []
    selected = set(_normalize_slugs(slugs))
    for row in engine.list_topics():
        slug = row.get("slug")
        if selected and slug not in selected:
            continue
        try:
            topics.append(engine.load_topic(slug))
        except Exception as exc:
            skipped.append({"slug": slug, "error": str(exc)})
    return topics, skipped


def _normalize_slugs(slugs: list[str] | str | None) -> list[str]:
    """Normalize MCP slug inputs.

    Some clients occasionally pass a single slug as a bare string even though
    the schema says list[str]. Treat that as one slug, not an iterable of
    characters that silently selects zero topics.
    """
    if slugs is None:
        return []
    if isinstance(slugs, str):
        return [s.strip() for s in slugs.split(",") if s.strip()]
    try:
        return [str(s).strip() for s in slugs if str(s).strip()]
    except TypeError:
        return [str(slugs).strip()] if str(slugs).strip() else []


def _select_scan_topics(engine, slugs: list[str] | None, max_topics: int) -> list[dict]:
    selected = set(_normalize_slugs(slugs))
    loaded, _ = _load_topics(engine, slugs)
    topics = [t for t in loaded if t.get("meta", {}).get("status") == "ACTIVE"]
    if not selected:
        topics.sort(
            key=lambda t: (
                _topic_scan_status(t).get("scanStale") is False,
                -(_topic_scan_status(t).get("scanAgeHours") or 999999),
            )
        )
        topics = topics[: max(1, min(int(max_topics), 20))]
    return topics


@mcp.tool()
def nrol_status() -> str:
    """Show configured NROL-AO repo status and available MCP transition tools."""
    try:
        engine = _import_from_repo("engine")
        topics = engine.list_topics()
        topics_dir = Path(getattr(engine, "TOPICS_DIR", _repo_path() / "topics"))
        state_root = topics_dir.parent
        return _json(
            {
                "repo": str(_repo_path()),
                "state_root": str(state_root),
                "topics_dir": str(topics_dir),
                "activity_snapshot": str(_activity_store().snapshot_path),
                "topics": len(topics),
                "transitions": sorted(_ALLOWED_TRANSITIONS),
                "mutating_transitions": sorted(_MUTATING_TRANSITIONS),
                "loom_permission": bool(os.environ.get("LOOM_CONV_ID")),
                "llama": llama_client.status(),
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "repo": str(_repo_path())})


def _git_run(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    """Run a git command in cwd; return (returncode, stdout, stderr).

    The only subprocess user in the server is publish_black_hole_snapshot; this
    helper keeps the git invocation in one place. Never uses a shell.
    """
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True,
        text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


@mcp.tool()
def publish_black_hole_snapshot(
    commit: bool = False,
    push: bool = False,
    message: str = "",
    note: str = "",
) -> str:
    """Regenerate the public NROL-AO dashboard snapshot and optionally publish it.

    Always regenerates `<black-hole>/surfaces/nrol-ao/data.json` from current
    topic state via export_blackhole_snapshot.build_snapshot() (sanitized: no
    evidence text, no source names, no URLs).

    commit=True stages ONLY surfaces/nrol-ao/data.json and commits it (never
    index.html, config.json, or git add -A). push=True (requires commit=True)
    runs `git push origin master` and goes through the Loom approval gate — a
    push to the public live site is outward-facing and hard to reverse, so it
    is fail-closed without LOOM_CONV_ID unless NROL_AO_ALLOW_UNGATED_COMMITS=1.
    On a non-fast-forward rejection the tool reports the conflict and does NOT
    auto-rebase/stash; surface it for the operator to resolve manually.

    commit=False, push=False is the purely programmatic refresh path: writes a
    fresh local data.json with no git mutation and no human gate. A scheduled
    job can use it to keep the local snapshot current.
    """
    store = _activity_store()
    job_id = new_job_id("publish-snapshot")
    t0 = time.time()
    try:
        black_hole = _black_hole_path()
        if not black_hole.is_dir():
            raise FileNotFoundError(
                f"black-hole repo not found at {black_hole}. Set "
                f"NROL_AO_BLACK_HOLE_REPO to the repo path."
            )
        snapshot_dir = black_hole / "surfaces" / "nrol-ao"
        if not snapshot_dir.is_dir():
            raise FileNotFoundError(
                f"black-hole surface dir not found: {snapshot_dir}"
            )

        # Push is an outward-facing mutation: require the Loom gate.
        denied = None
        if push:
            denied = _ask_loom_permission(
                "nrol_ao_publish_black_hole_snapshot",
                {"commit": commit, "push": push, "message": message or "(default)"},
            )
            if denied:
                store.record(
                    job_id, "denied", task="publish_black_hole_snapshot",
                    summary={"denied": denied, "commit": commit, "push": push},
                )
                return _json({"job_id": job_id, "denied": denied, "pushed": False})

        store.record(
            job_id, "running", task="publish_black_hole_snapshot",
            summary={"commit": commit, "push": push},
        )

        exporter = _import_from_repo("export_blackhole_snapshot")
        snapshot = exporter.build_snapshot()
        out_path = snapshot_dir / "data.json"
        out_path.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        result = {
            "regenerated": True,
            "data_json": str(out_path),
            "topic_count": snapshot.get("topic_count"),
            "generated_at": snapshot.get("generated_at"),
        }

        if commit:
            rc, out, err = _git_run(
                ["add", "--", "surfaces/nrol-ao/data.json"], black_hole
            )
            if rc != 0:
                raise RuntimeError(f"git add failed: {err or out}")
            rc, out, err = _git_run(
                ["rev-parse", "HEAD"], black_hole
            )
            head_before = out
            commit_msg = message or (
                f"chore(nrol-ao): refresh public snapshot "
                f"({snapshot.get('topic_count')} topics, "
                f"{(snapshot.get('generated_at') or '')[:10]})"
            )
            rc, out, err = _git_run(
                ["commit", "-m", commit_msg, "--", "surfaces/nrol-ao/data.json"],
                black_hole,
            )
            if rc != 0:
                # Nothing staged / nothing to commit is not fatal for a refresh.
                result["commit"] = {"committed": False, "note": err or out}
            else:
                rc2, out2, _ = _git_run(["rev-parse", "HEAD"], black_hole)
                result["commit"] = {
                    "committed": True,
                    "head_before": head_before,
                    "head_after": out2,
                }
                if push:
                    rc3, out3, err3 = _git_run(
                        ["push", "origin", "master"], black_hole, timeout=120
                    )
                    if rc3 != 0:
                        result["push"] = {
                            "pushed": False,
                            "error": err3 or out3,
                            "note": (
                                "non-fast-forward or rejected — fetch, rebase onto "
                                "origin/master, and retry; do not force-push"
                            ),
                        }
                    else:
                        result["push"] = {"pushed": True, "output": out3}

        result["duration_ms"] = int((time.time() - t0) * 1000)
        store.record(
            job_id, "completed", task="publish_black_hole_snapshot",
            duration_ms=result["duration_ms"],
            summary={
                "commit": commit, "push": push,
                "topic_count": snapshot.get("topic_count"),
                "pushed": bool(result.get("push", {}).get("pushed")),
            },
            note=note,
        )
        return _json({"job_id": job_id, **result})
    except Exception as exc:
        try:
            store.record(
                job_id, "failed", task="publish_black_hole_snapshot",
                error=str(exc), summary={"commit": commit, "push": push},
            )
        except Exception:
            pass
        return _json({"job_id": job_id, "error": str(exc)})


@mcp.tool()
def help() -> str:
    """Explain the intended NROL-AO MCP workflow and available tools."""
    return _json(
        {
            "boundary": (
                "The human/operator LLM is on the client side. Topic inspection, "
                "news scanning, search, governance checks, matcher deliberation, "
                "and proposed FIRE/OBSERVE/PARK/SCHEMA_GAP decisions are exposed "
                "through this MCP server."
            ),
            "normal_workflow": [
                "Call nrol_status to verify the MCP bridge and configured repo.",
                "Call topic_status or list_topics to choose scope.",
                "Call read_topic/read_search_queries or list_hypotheses to inspect a topic.",
                "Use propose_search_query_update -> red_team_search_query_update -> apply_search_query_update for durable retrieval coverage changes.",
                "Call run_news_scan with commit_policy='safe' for review-first MCP-side search and deliberation.",
                "Review the returned operator packet and brief pending FIRE/OBSERVE proposals before committing.",
                "Use commit=true only when explicit mutation is intended and Loom approval is expected.",
            ],
            "proposal_lifecycle": [
                "submit_article(article) — store a candidate observation; no mutation.",
                "propose_match(article_id, slug, action, ...) — record a typed proposal; no mutation.",
                "list_proposals(slug, status) — review the pending queue.",
                "commit_match(proposal_id) — validate + apply through engine gates and Loom approval.",
                "withdraw_proposal(proposal_id) — the IGNORE decision for proposals.",
                "propose_search_query_update(...) — record durable query changes; no mutation.",
                "red_team_search_query_update(proposal_id) — mandatory query governance review.",
                "apply_search_query_update(proposal_id) — apply approved retrieval metadata with Loom approval.",
            ],
            "scan_semantics": {
                "commit_false": "No evidence/posterior mutation.",
                "dry_run_false": "Records successful scan coverage by stamping topic.meta.lastScanned.",
                "dry_run_true": "Preview only; does not stamp lastScanned.",
                "commit_policy_safe": (
                    "PARK/SCHEMA_GAP may auto-apply; FIRE/OBSERVE are forced "
                    "to pending proposals with deliberation attached. Safe "
                    "policy still wins if commit=true is supplied."
                ),
                "freshness": (
                    "Tracker query params are stripped for duplicate keys; "
                    "old dated articles are dropped; full-article metadata "
                    "can supply missing publication dates; undated FIRE/OBSERVE "
                    "candidates are downgraded to PARK."
                ),
            },
            "do_not": [
                "Do not perform operator-side web search as a fallback for run_news_scan.",
                "Do not edit NROL topic JSON directly.",
                "Do not invent likelihoods, posteriors, or target probabilities.",
                "If this MCP server is unavailable, stop and report a setup failure.",
            ],
            "server_structure": {
                "bridge_check": (
                    "Call nrol_status() first to verify the MCP bridge, the "
                    "configured repo (NROL_AO_REPO), and the black-hole repo. It "
                    "returns the repo root, topic count, and available transition "
                    "tools — if it errors or the repo root is wrong, stop and report "
                    "a setup failure rather than proceeding."
                ),
                "boundary": (
                    "This MCP server is the authority boundary: it is the only path "
                    "to topic state, evidence, proposals, and posteriors. The "
                    "operator never reads topic JSON from disk or runs shell "
                    "commands — file and shell tools are stripped from the session. "
                    "Everything goes through the tools below."
                ),
                "llm_backend": (
                    "LLM-backed tools (red-teams, run_matcher_with_llama, "
                    "review_duplicate_candidate, review_parked, deliberation, "
                    "future_cast, the resolution AAR) call a local llama-server "
                    "endpoint. Check llama_server_status() if those return empty or "
                    "timeout — the token-budget reference doc covers the empty-"
                    "rationale failure mode."
                ),
            },
            "tool_groups": {
                "Inspect state": [
                    "nrol_status", "topic_status", "list_topics", "list_hypotheses",
                    "read_topic", "list_activity", "read_evidence",
                ],
                "Triage": ["triage_headline (first, always, before anything else)"],
                "Scan": ["run_news_scan (pass brief=true in operator mode)"],
                "Deliberate": ["deliberate_candidates", "review_duplicate_candidate"],
                "Proposals": [
                    "submit_article", "propose_match", "commit_match",
                    "withdraw_proposal", "list_proposals",
                ],
                "Transitions": [
                    "submit_transition (PARK / FIRE / OBSERVE / SCHEMA_GAP / IGNORE)",
                ],
                "Search queries": [
                    "read_search_queries", "propose_search_query_update",
                    "red_team_search_query_update", "apply_search_query_update",
                    "withdraw_search_query_update",
                ],
                "Schema gaps": [
                    "list_schema_gaps", "run_schema_gap_resolver",
                    "propose_schema_extension", "list_schema_extension_proposals",
                    "red_team_schema_extension_proposal",
                    "mark_schema_extension_proposal",
                    "apply_schema_extension_proposal",
                ],
                "Parked queue": ["review_parked", "acknowledge_parked_reviews"],
                "Replay/undo": [
                    "list_scan_runs", "read_scan_run", "replay_scan_run",
                    "undo_scan_run",
                ],
                "Shadow/calibration": [
                    "shadow_posteriors", "future_cast", "resolve_topic",
                    "resolution_brier", "source_calibration_status",
                    "source_profile", "validate_source_db",
                    "source_domain_patterns", "log_social_forecast",
                    "social_user_brier", "list_social_handles",
                    "list_triage_log", "read_triage_log",
                ],
                "Publish": ["publish_black_hole_snapshot"],
                "Topic design": ["design_topic", "activate_topic"],
                "Reference": ["help", "read_reference"],
            },
            "tools": [
                "nrol_status",
                "help",
                "list_topics",
                "topic_status",
                "read_topic",
                "read_search_queries",
                "read_evidence",
                "acknowledge_parked_reviews",
                "list_hypotheses",
                "list_schema_gaps",
                "run_schema_gap_resolver",
                "list_schema_extension_proposals",
                "red_team_schema_extension_proposal",
                "mark_schema_extension_proposal",
                "apply_schema_extension_proposal",
                "run_news_scan",
                "propose_search_query_update",
                "list_search_query_updates",
                "red_team_search_query_update",
                "apply_search_query_update",
                "withdraw_search_query_update",
                "list_activity",
                "submit_transition",
                "submit_article",
                "propose_match",
                "commit_match",
                "list_proposals",
                "withdraw_proposal",
                "list_scan_runs",
                "read_scan_run",
                "replay_scan_run",
                "undo_scan_run",
                "read_reference",
            ],
        }
    )


# --- Reference docs -------------------------------------------------------
# Deep reference material externalized from the always-on OPERATOR.md prompt
# (which is injected onto the `claude` command line via --append-system-prompt
# and must stay small to avoid breaching Windows' 32,767-char CreateProcess
# limit). These docs are fetched on demand through read_reference(), whose
# response travels the tool-result channel — not the command line — so it
# carries no length penalty.
_DOCS_DIR = Path(__file__).parent / "docs"

# Single source of truth: section name -> filename. The lean OPERATOR.md and
# the index returned by read_reference(section="") both reference these keys,
# so adding/renaming a doc is a one-line change here.
_REFERENCE_DOCS = {
    "tool-reference": "tool-reference.md",
    "shadow-tools": "shadow-tools.md",
    "search-queries": "search-queries.md",
    "operator-loop": "operator-loop.md",
    "token-budget": "token-budget.md",
}

_DOC_SUMMARIES = {
    "tool-reference": (
        "Per-tool inventory with footguns (deliberate_candidates output_text "
        "shape, anti-indicator authoring, brief=true rationale)."
    ),
    "shadow-tools": (
        "shadow_posteriors, future_cast, source trust, triage, social Brier — "
        "calibration, not action."
    ),
    "search-queries": (
        "Query-authoring guide, coverage axes, retrieval limits, proposal template."
    ),
    "operator-loop": (
        "6-step evidence loop with deep sub-notes: repeat-firing, framing "
        "traps, draining the parked queue, freshness downgrades."
    ),
    "token-budget": (
        "max_tokens guidance, empty-REVISE failure mode, when to raise the budget."
    ),
}


@mcp.tool()
def read_reference(section: str = "") -> str:
    """Read a deep-reference doc for the NROL-AO operator.

    The lean OPERATOR.md prompt keeps only the always-on guardrails and
    decision rules; the bulky per-tool footguns, shadow-tool semantics,
    query-authoring guide, operator-loop sub-notes, and token-budget
    guidance live in external markdown files beside this server. Call this
    with the section name to fetch the relevant doc. With no argument (or
    an unknown name) it returns the index of available sections with a
    one-line summary of each, so you can discover what is available before
    fetching.

    These are reference docs, not state. They never move posteriors and are
    not evidence.
    """
    if not section or section not in _REFERENCE_DOCS:
        return _json(
            {
                "available_sections": _DOC_SUMMARIES,
                "usage": "read_reference(section='<name>') to fetch the full doc.",
            }
        )
    # `section` is a dict key, so "../" cannot reach this lookup; the
    # resolve()+relative_to() guard is defense-in-depth in case the lookup
    # is ever changed to be filesystem-based.
    path = (_DOCS_DIR / _REFERENCE_DOCS[section]).resolve()
    try:
        path.relative_to(_DOCS_DIR.resolve())
    except ValueError:
        return _json({"error": "path traversal refused", "section": section})
    if not path.is_file():
        return _json(
            {
                "error": "doc file missing on disk",
                "section": section,
                "expected_path": str(path),
                "available_sections": list(_REFERENCE_DOCS),
            }
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _json({"error": str(exc), "section": section})
    # Return raw markdown, NOT wrapped in _json(). The reference docs are
    # prose with embedded code blocks; returning raw text lets the model
    # read it directly without a JSON encode/decode round-trip for several
    # KB. A leading header line grounds the response.
    return f"# reference:{section}\n\n{text}"


@mcp.tool()
def llama_server_status() -> str:
    """Check the llama-server endpoint used by NROL-AO MCP LLM jobs."""
    return _json(llama_client.status())


@mcp.tool()
def model_endpoint_status() -> str:
    """Check the local model endpoint used by MCP-side deliberation jobs."""
    return _json(
        {
            "default_provider": "model-agnostic",
            "local_endpoint": llama_client.status(),
            "notes": [
                "run_news_scan uses this endpoint for MCP-side matcher deliberation.",
                "build_* and apply_* tools are debug/manual override surfaces, not the primary scan path.",
            ],
        }
    )


@mcp.tool()
def list_activity(limit: int = 20) -> str:
    """Read recent NROL-AO MCP job activity for dashboard monitoring."""
    try:
        snapshot = _activity_store().list_jobs(limit=limit)
        snapshot["snapshot_path"] = str(_activity_store().snapshot_path)
        return _json(snapshot)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def list_topics(status: str = "", include_governance: bool = False) -> str:
    """List NROL-AO topics from the configured repo."""
    try:
        engine = _import_from_repo("engine")
        loaded, _skipped = _load_topics(engine)
        rows = []
        for topic in loaded:
            if include_governance:
                row = _topic_summary(topic)
            else:
                meta = topic.get("meta", {}) or {}
                gov = topic.get("governance", {}) or {}
                row = {
                    "slug": meta.get("slug"),
                    "title": meta.get("title"),
                    "status": meta.get("status"),
                    "classification": meta.get("classification"),
                    "question": meta.get("question", ""),
                    "lastUpdated": meta.get("lastUpdated", ""),
                    "governanceHealth": gov.get("health") if gov else None,
                }
            rows.append(row)
        if status:
            rows = [row for row in rows if str(row.get("status", "")).upper() == status.upper()]
        return _json(rows)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def read_topic(slug: str, include_indicators: bool = True, evidence_limit: int = 10) -> str:
    """Read a topic summary, indicators, governance queues, and recent evidence."""
    try:
        engine = _import_from_repo("engine")
        governor = _import_from_repo("governor")
        topic = engine.load_topic(slug)
        indicators = []
        if include_indicators:
            for tier, items in (topic.get("indicators", {}).get("tiers", {}) or {}).items():
                indicators.extend(_indicator_brief(item, tier) for item in items or [])
            indicators.extend(
                _indicator_brief(item, "anti_indicators")
                for item in topic.get("indicators", {}).get("anti_indicators", []) or []
            )
        evidence = topic.get("evidenceLog", []) or []
        limit = max(0, min(int(evidence_limit), 100))
        gov_report = governor.governance_report(topic)
        return _json(
            {
                "topic": _topic_summary(topic),
                "searchQueries": [
                    _search_query_text(q)
                    for q in topic.get("searchQueries", []) or []
                    if _search_query_text(q)
                ],
                "hypotheses": topic.get("model", {}).get("hypotheses", {}),
                "indicators": indicators,
                "recent_evidence": evidence[-limit:] if limit else [],
                "review_queues": {
                    "flagged_for_indicator_review": topic.get("governance", {}).get(
                        "flagged_for_indicator_review", []
                    ),
                    "flagged_schema_gaps": topic.get("governance", {}).get(
                        "flagged_schema_gaps", []
                    ),
                    "proposed_schema_extensions": topic.get("governance", {}).get(
                        "proposed_schema_extensions", []
                    ),
                },
                "governance_report": {
                    "health": gov_report.get("health"),
                    "issues": gov_report.get("issues", []),
                    "rt": gov_report.get("rt"),
                    "entropy": gov_report.get("entropy"),
                    "uncertainty_ratio": gov_report.get("uncertainty_ratio"),
                },
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def read_search_queries(slug: str, include_generated_preview: bool = True) -> str:
    """Read durable topic searchQueries and optional generated scan preview."""
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        queries = [
            _search_query_text(q)
            for q in topic.get("searchQueries", []) or []
            if _search_query_text(q)
        ]
        out = {
            "slug": slug,
            "searchQueries": queries,
            "count": len(queries),
            "coverage_axes": _coverage_axes(queries, topic),
        }
        if include_generated_preview:
            out["generated_preview"] = _search_query_specs(topic, "preview")
        return _json(out)
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def propose_search_query_update(
    slug: str,
    add: list[str] | str | None = None,
    remove: list[str] | str | None = None,
    rationale: str = "",
    coverage_gaps: list[str] | str | None = None,
) -> str:
    """File a durable searchQueries update proposal. No topic mutation."""
    try:
        if not rationale.strip():
            raise ValueError("rationale is required")
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        if topic.get("meta", {}).get("status") != "ACTIVE":
            raise ValueError(f"topic {slug!r} is not ACTIVE")
        add_queries = _normalize_search_queries(add)
        remove_queries = _normalize_search_queries(remove)
        gaps = _normalize_search_queries(coverage_gaps)
        validation = _validate_search_query_set(
            topic.get("searchQueries") or [], add_queries, remove_queries, topic=topic
        )
        if validation["errors"]:
            return _json({
                "slug": slug,
                "committed": False,
                "error": "search query proposal failed validation",
                "validation": validation,
            })
        record = _proposal_store().add_search_query_proposal(
            slug=slug,
            add_queries=add_queries,
            remove_queries=remove_queries,
            rationale=rationale.strip(),
            coverage_gaps=gaps,
            result={"validation": validation},
        )
        record["next_step"] = f"red_team_search_query_update({record['id']!r})"
        return _json(record)
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def list_search_query_updates(
    slug: str = "", status: str = "pending", limit: int = 50
) -> str:
    """List search-query update proposals."""
    try:
        rows = _proposal_store().list_search_query_proposals(
            slug=slug, status=status, limit=limit
        )
        return _json({"count": len(rows), "proposals": rows})
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def red_team_search_query_update(
    proposal_id: str,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 600,
) -> str:
    """MCP red-team gate for a proposed searchQueries update.

    Hard structural failures are rejected deterministically. Otherwise this
    mirrors the scan jury pattern by asking the local llama endpoint for the
    substantive retrieval-quality verdict.
    """
    activity = _activity_store()
    job_id = new_job_id("red-team-query")
    try:
        store = _proposal_store()
        proposal = store.get_search_query_proposal(proposal_id)
        if proposal is None:
            raise ValueError(f"search query proposal {proposal_id!r} not found")
        engine = _import_from_repo("engine")
        topic = engine.load_topic(proposal["slug"])
        deterministic = _deterministic_search_query_review(proposal, topic)
        if deterministic["verdict"] == "REJECT":
            review = deterministic
            review["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            review["model"] = "deterministic"
            updated = store.set_search_query_red_team(proposal_id, review)
            return _json({"proposal": updated, "red_team": review})
        prompt = _build_search_query_red_team_prompt(
            proposal, topic, deterministic["validation"]
        )
        activity.record(
            job_id,
            "running",
            task="red_team_search_query_update",
            slug=proposal["slug"],
            model=model or llama_client.llama_model(),
            summary={"proposal_id": proposal_id},
            prompt=prompt,
        )
        response = llama_client.chat(
            prompt,
            system_prompt=(
                "You are the RED TEAM for NROL-AO search-query coverage. "
                "Return only the requested review fields."
            ),
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            disable_thinking=False,
        )
        review = _parse_search_query_red_team_review(response.get("text", ""))
        review["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        review["model"] = response.get("model")
        review["deterministic"] = deterministic
        review["finish_reason"] = response.get("finish_reason")
        # Defensive: if the model returned empty content (reasoning-budget
        # exhaustion on a thinking-enabled model, or a transport hiccup), do NOT
        # let the parser's default masquerade as a substantive review.
        if not (response.get("text") or "").strip():
            review["verdict"] = "REVISE"
            review["risk"] = "NO_ANSWER_EMITTED"
            review["recommendation"] = (
                "Red-team model returned empty content (finish_reason="
                f"{response.get('finish_reason')!r}, reasoning_chars="
                f"{response.get('reasoning_chars', 0)}). This is a non-answer, "
                "not a substantive REVISE — rerun the red-team review."
            )
        updated = store.set_search_query_red_team(proposal_id, review)
        activity.record(
            job_id,
            "completed",
            task="red_team_search_query_update",
            slug=proposal["slug"],
            model=response.get("model"),
            summary={"proposal_id": proposal_id, "verdict": review["verdict"]},
            response=response.get("text", ""),
        )
        return _json({"proposal": updated, "red_team": review})
    except Exception as exc:
        try:
            activity.record(
                job_id,
                "failed",
                task="red_team_search_query_update",
                error=str(exc),
                summary={"proposal_id": proposal_id},
            )
        except Exception:
            pass
        return _json({"job_id": job_id, "error": str(exc), "proposal_id": proposal_id})


@mcp.tool()
def apply_search_query_update(proposal_id: str, dry_run: bool = True) -> str:
    """Apply an approved searchQueries update. Mutates only query metadata."""
    store = _proposal_store()
    try:
        proposal = store.get_search_query_proposal(proposal_id)
        if proposal is None:
            raise ValueError(f"search query proposal {proposal_id!r} not found")
        if proposal.get("status") != "pending":
            raise ValueError(
                f"search query proposal {proposal_id!r} already decided: {proposal.get('status')}"
            )
        engine = _import_from_repo("engine")
        topic = engine.load_topic(proposal["slug"])
        review = proposal.get("red_team") if isinstance(proposal.get("red_team"), dict) else {}
        if review.get("verdict") != "APPROVE":
            raise ValueError("search query update requires red_team verdict APPROVE before apply")
        add_queries = _normalize_search_queries(proposal.get("add_queries"))
        remove_queries = _normalize_search_queries(proposal.get("remove_queries"))
        validation = _validate_search_query_set(
            topic.get("searchQueries") or [], add_queries, remove_queries, topic=topic
        )
        if validation["errors"]:
            raise ValueError(f"search query update failed validation: {validation['errors']}")
        proposed_queries = validation["proposed_queries"]
        result = {
            "proposal_id": proposal_id,
            "slug": proposal["slug"],
            "dry_run": dry_run,
            "committed": False,
            "before": validation["current_queries"],
            "after": proposed_queries,
            "red_team": review,
            "validation": validation,
        }
        if dry_run:
            return _json(result)
        denied = _ask_loom_permission(
            "nrol_ao_apply_search_query_update",
            {
                "proposal_id": proposal_id,
                "slug": proposal["slug"],
                "before": validation["current_queries"],
                "after": proposed_queries,
                "rationale": proposal.get("rationale", ""),
                "red_team": review,
            },
        )
        if denied:
            return _json({"proposal_id": proposal_id, "committed": False, "denied": denied})
        topic["searchQueries"] = proposed_queries
        gov = topic.setdefault("governance", {})
        gov.setdefault("search_query_history", []).append({
            "proposal_id": proposal_id,
            "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "before": validation["current_queries"],
            "after": proposed_queries,
            "add": add_queries,
            "remove": remove_queries,
            "rationale": proposal.get("rationale", ""),
            "coverage_gaps": proposal.get("coverage_gaps", []),
            "red_team": review,
        })
        engine.save_topic(topic)
        result["committed"] = True
        result["dry_run"] = False
        store.mark_search_query_proposal(
            proposal_id, "applied", note="applied searchQueries update", result=result
        )
        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc), "proposal_id": proposal_id})


@mcp.tool()
def withdraw_search_query_update(proposal_id: str, reason: str = "") -> str:
    """Withdraw a pending search-query update proposal."""
    try:
        store = _proposal_store()
        proposal = store.get_search_query_proposal(proposal_id)
        if proposal is None:
            raise ValueError(f"search query proposal {proposal_id!r} not found")
        if proposal.get("status") != "pending":
            raise ValueError(
                f"search query proposal {proposal_id!r} already decided: {proposal.get('status')}"
            )
        updated = store.mark_search_query_proposal(
            proposal_id, "withdrawn", note=reason or "withdrawn by operator"
        )
        return _json(updated)
    except Exception as exc:
        return _json({"error": str(exc), "proposal_id": proposal_id})


@mcp.tool()
def read_evidence(
    slug: str,
    evidence_ids: str = "",
    flagged_only: bool = False,
    limit: int = 50,
    text_chars: int = 1200,
) -> str:
    """Read stored evidence rows by id, or inspect the flagged parked queue."""
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        evidence = [
            e for e in topic.get("evidenceLog", []) or []
            if isinstance(e, dict) and e.get("id")
        ]
        requested = [
            part.strip()
            for part in re.split(r"[, \n\t]+", evidence_ids or "")
            if part.strip()
        ]
        requested_set = set(requested)
        flagged = set(
            topic.get("governance", {}).get("flagged_for_indicator_review", []) or []
        )
        if requested_set:
            selected = [e for e in evidence if e.get("id") in requested_set]
        elif flagged_only:
            selected = [e for e in evidence if e.get("id") in flagged]
        else:
            selected = evidence

        max_items = max(0, min(int(limit), 200))
        max_text = max(0, min(int(text_chars), 10000))
        rows = []
        for e in selected[:max_items]:
            text = str(e.get("text") or "")
            rows.append({
                "id": e.get("id"),
                "time": e.get("time"),
                "source": e.get("source", ""),
                "url": e.get("url", ""),
                "has_url": bool(e.get("url")),
                "tags": e.get("tags", []),
                "transition": e.get("transition") or e.get("decision") or "",
                "claimState": e.get("claimState", ""),
                "posteriorImpact": e.get("posteriorImpact", ""),
                "parked_reason": e.get("parked_reason") or e.get("parkedReason") or "",
                "text": text[:max_text],
                "text_truncated": len(text) > max_text,
                "flagged_for_indicator_review": e.get("id") in flagged,
            })

        missing = [ev_id for ev_id in requested if ev_id not in {r["id"] for r in rows}]
        return _json({
            "slug": slug,
            "count": len(rows),
            "limit": max_items,
            "requested_ids": requested,
            "missing_ids": missing,
            "flagged_only": flagged_only,
            "evidence": rows,
        })
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def acknowledge_parked_reviews(
    slug: str,
    evidence_ids: str = "",
    reason: str = "",
    limit: int = 50,
    dry_run: bool = True,
) -> str:
    """Stamp parked evidence as operator-reviewed without matcher/proposals."""
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        if not hasattr(engine, "parked_review_status"):
            return _json({"error": "engine lacks parked_review_status", "slug": slug})

        debt_before = engine.parked_review_status(topic)
        requested = [
            part.strip()
            for part in re.split(r"[, \n\t]+", evidence_ids or "")
            if part.strip()
        ]
        if requested:
            selected = requested
        else:
            max_items = max(1, min(int(limit), 500))
            selected = [d["evidence_id"] for d in debt_before.get("due", [])[:max_items]]

        flagged = set(
            topic.get("governance", {}).get("flagged_for_indicator_review", []) or []
        )
        evidence_by_id = {
            e.get("id"): e for e in topic.get("evidenceLog", []) or []
            if isinstance(e, dict) and e.get("id")
        }
        reviews = []
        missing = []
        skipped_not_flagged = []
        note = (reason or "operator acknowledged parked evidence as reviewed; no proposal filed").strip()
        for ev_id in selected:
            if ev_id not in evidence_by_id:
                missing.append(ev_id)
                continue
            if ev_id not in flagged:
                skipped_not_flagged.append(ev_id)
                continue
            reviews.append({
                "evidence_id": ev_id,
                "decision": "ACK",
                "note": note[:300],
            })

        recorded = None
        debt_after = None
        if not dry_run and reviews:
            recorded = engine.record_parked_reviews(slug, reviews, reviewer="operator_ack")
            debt_after = engine.parked_review_status(engine.load_topic(slug))
            debt_after.pop("due", None)

        return _json({
            "slug": slug,
            "dry_run": dry_run,
            "selected": selected,
            "acknowledgeable": [r["evidence_id"] for r in reviews],
            "missing_ids": missing,
            "skipped_not_flagged": skipped_not_flagged,
            "recorded": recorded,
            "debt_before": {k: v for k, v in debt_before.items() if k != "due"},
            "debt_after": debt_after,
            "note": "No posterior movement; this only records review attendance.",
        })
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def list_schema_gaps(slug: str, clustered: bool = True) -> str:
    """List flagged schema gaps for a topic, optionally clustered by pattern."""
    try:
        engine = _import_from_repo("engine")
        resolver = _import_from_repo("framework.schema_gap_resolver")
        topic = engine.load_topic(slug)
        gaps = topic.get("governance", {}).get("flagged_schema_gaps", []) or []
        payload = {"slug": slug, "count": len(gaps), "gaps": gaps}
        if clustered:
            payload["clusters"] = resolver.cluster_gaps(topic)
        return _json(payload)
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def run_schema_gap_resolver(
    slug: str,
    persist: bool = False,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 900,
) -> str:
    """Ask the local model to propose schema extensions for flagged gaps.

    persist=false (default) is preview only. persist=true writes proposals to
    governance.proposed_schema_extensions for operator review; it does not
    apply schema edits. Application remains a cleanup/design task.
    """
    store = _activity_store()
    job_id = new_job_id("schema-gap-resolver")
    try:
        engine = _import_from_repo("engine")
        resolver = _import_from_repo("framework.schema_gap_resolver")
        topic = engine.load_topic(slug)
        clusters = resolver.cluster_gaps(topic)
        if not clusters:
            return _json({
                "job_id": job_id, "slug": slug, "clusters": [],
                "proposals": [], "note": "no flagged schema gaps",
                "persisted": False,
            })
        prompt = resolver.build_resolver_prompt(topic, clusters)
        store.record(
            job_id, "running", task="run_schema_gap_resolver", slug=slug,
            model=model or llama_client.llama_model(),
            summary={"clusters": len(clusters), "persist": persist},
            prompt=prompt,
        )
        response = llama_client.chat(
            prompt,
            system_prompt=(
                "You are the NROL-AO schema-gap resolver. Return only "
                "PROPOSAL blocks in the requested format. Do not edit topic JSON."
            ),
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            disable_thinking=False,
        )
        text = response.get("text", "")
        proposals = resolver.parse_resolver_proposals(text)
        proposals = resolver.validate_proposals_balance(topic, proposals)
        # Defensive: if the model returned empty content (reasoning-budget
        # exhaustion on a thinking-enabled model, or a transport hiccup), do NOT
        # silently persist nothing and report success. Surface the non-answer so
        # an operator reruns instead of assuming "no gaps found."
        no_answer = bool(proposals) is False and not text.strip()
        persisted_count = 0
        if persist and proposals and not no_answer:
            updated = resolver.persist_proposals(slug, proposals, validated=False)
            persisted_count = len(
                updated.get("governance", {}).get("proposed_schema_extensions", []) or []
            )
        payload = {
            "job_id": job_id, "slug": slug, "clusters": clusters,
            "model": response.get("model"), "response": text,
            "proposals": proposals, "persisted": bool(persist and proposals),
            "proposed_schema_extensions_count": persisted_count,
            "no_answer_emitted": no_answer,
            "note": (
                "Resolver model returned empty content (finish_reason="
                f"{response.get('finish_reason')!r}, reasoning_chars="
                f"{response.get('reasoning_chars', 0)}). No proposals parsed — "
                "rerun; do not treat as 'no gaps found'."
            ) if no_answer else None,
        }
        store.record(
            job_id, "completed", task="run_schema_gap_resolver", slug=slug,
            model=response.get("model"), summary={
                "clusters": len(clusters),
                "proposals": len(proposals),
                "persisted": bool(persist and proposals),
            },
            response=_json(payload),
        )
        return _json(payload)
    except Exception as exc:
        try:
            store.record(job_id, "failed", task="run_schema_gap_resolver",
                         slug=slug, error=str(exc))
        except Exception:
            pass
        return _json({"job_id": job_id, "error": str(exc), "slug": slug})


@mcp.tool()
def list_schema_extension_proposals(slug: str, status: str = "pending") -> str:
    """List governance.proposed_schema_extensions for operator review."""
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        queue = topic.get("governance", {}).get("proposed_schema_extensions", []) or []
        rows = []
        for i, proposal in enumerate(queue):
            p_status = proposal.get("status", "")
            if status and status != "all":
                if status == "pending":
                    if not p_status.startswith("pending_operator_review"):
                        continue
                elif p_status != status:
                    continue
            rows.append({"index": i, **proposal})
        return _json({"slug": slug, "count": len(rows), "proposals": rows})
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


def _format_schema_body(
    desc: str,
    likelihoods: dict | None,
    observable: dict | None,
    shape: str,
    causal_event_id: str,
    ladder_group: str,
    ladder_step: str,
) -> str:
    """Build the YAML-ish SCHEMA block that apply_schema_extension_proposal re-parses.

    Mirrors the shape _parse_schema_body expects (server.py): a `SCHEMA:` header
    with `desc`, `likelihoods: {H1: .., H2: ..}`, an `observable:` block
    (metric/family/direction strings, threshold_value/baseline floats), and
    optional shape/causal_event_id/ladder_group/ladder_step. Structured dicts at
    the proposal top level are IGNORED by apply — only the parsed body matters.
    """
    lines = ["SCHEMA:"]
    if desc:
        lines.append(f"  desc: {desc}")
    if likelihoods:
        lr_parts = ", ".join(f"{k}: {float(v)}" for k, v in likelihoods.items())
        lines.append(f"  likelihoods: {{{lr_parts}}}")
    if observable:
        lines.append("  observable:")
        for key in ("metric", "family", "direction"):
            if observable.get(key):
                lines.append(f"    {key}: {observable[key]}")
        for key in ("threshold_value", "baseline"):
            if observable.get(key) is not None:
                lines.append(f"    {key}: {float(observable[key])}")
    if shape:
        lines.append(f"  shape: {shape}")
    if causal_event_id:
        lines.append(f"  causal_event_id: {causal_event_id}")
    if ladder_group:
        lines.append(f"  ladder_group: {ladder_group}")
    if ladder_step:
        lines.append(f"  ladder_step: {ladder_step}")
    return "\n".join(lines)


@mcp.tool()
def propose_schema_extension(
    slug: str,
    kind: str,
    target: str,
    tier: str = "tier3_suggestive",
    desc: str = "",
    rationale: str = "",
    likelihoods: dict | None = None,
    observable: dict | None = None,
    shape: str = "",
    causal_event_id: str = "",
    target_hypothesis: str = "",
    ladder_group: str = "",
    ladder_step: str = "",
) -> str:
    """File a hand-authored indicator proposal into the schema-extension queue.

    Appends to governance.proposed_schema_extensions so an operator can red-team
    it, mark it approved, and apply it via apply_schema_extension_proposal — the
    same review-first lifecycle the resolver's auto-generated proposals use, but
    for window-specific indicators that require human causal reasoning (the
    resolver cannot produce them).

    No mutation to indicators, no posterior movement, no Loom gate — this is a
    zero-authority queue append, the same tier as run_schema_gap_resolver's
    persist step. The proposal synthesizes the YAML-ish `body` string that apply
    re-parses (structured dicts at the top level are ignored by apply).

    kind: add_new_indicator | extend_observable | no_fix.
    target: indicator id (existing for extend_observable, new for add_new_indicator).
    tier: one of the topic's tier keys (tier1_critical/tier2_strong/tier3_suggestive)
          or anti_indicators. Anti-indicators should also pass target_hypothesis
          (single H or list) so the inversion lint can verify LR direction at apply.
    likelihoods/observable: structured fields, rendered into body. Required for
    add_new_indicator.
    """
    try:
        kind_norm = (kind or "").strip().lower()
        if kind_norm not in {"add_new_indicator", "extend_observable", "no_fix"}:
            raise ValueError(
                "kind must be add_new_indicator, extend_observable, or no_fix"
            )
        target = (target or "").strip()
        if not target and kind_norm != "no_fix":
            raise ValueError("target id is required")

        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        if (topic.get("meta", {}).get("status") or "").upper() != "ACTIVE":
            raise ValueError(
                f"topic {slug!r} is not ACTIVE (status="
                f"{topic.get('meta', {}).get('status')}); "
                "schema extensions target live topics"
            )

        h_keys = list((topic.get("model", {}).get("hypotheses") or {}).keys())
        tiers = topic.get("indicators", {}).get("tiers", {}) or {}
        allowed_tiers = set(tiers.keys()) | {"anti_indicators"}
        tier = (tier or "").strip()
        if kind_norm == "add_new_indicator":
            if tier not in allowed_tiers:
                raise ValueError(
                    f"tier must be 'anti_indicators' or one of {sorted(tiers.keys())}"
                )

        # Validate target existence/duplication, mirroring apply's checks.
        existing, _ = _find_indicator(topic, target)
        if kind_norm == "add_new_indicator" and existing is not None:
            raise ValueError(f"indicator {target!r} already exists on topic")
        if kind_norm == "extend_observable" and existing is None:
            raise ValueError(
                f"extend_observable target {target!r} does not exist on topic"
            )

        # Validate target_hypothesis against the topic's hypotheses (anti-indicator
        # inversion lint resolves the suppress-target from this field).
        th_out = None
        if target_hypothesis:
            if isinstance(target_hypothesis, str):
                th_list = [h.strip().upper() for h in target_hypothesis.split(",") if h.strip()]
            else:
                th_list = [str(h).strip().upper() for h in target_hypothesis if str(h).strip()]
            bad = [h for h in th_list if h not in h_keys]
            if bad:
                raise ValueError(
                    f"target_hypothesis {bad} not in topic hypotheses {h_keys}"
                )
            th_out = th_list[0] if len(th_list) == 1 else th_list

        # For add_new_indicator, validate likelihoods + observable presence now
        # so the operator doesn't reach apply and fail there (mirrors apply gates).
        if kind_norm == "add_new_indicator":
            if not likelihoods:
                raise ValueError("add_new_indicator requires likelihoods")
            bad_lr_h = [k for k in likelihoods if k not in h_keys]
            if bad_lr_h:
                raise ValueError(
                    f"likelihoods keys {bad_lr_h} not in topic hypotheses {h_keys}"
                )
            if not observable:
                raise ValueError("add_new_indicator requires an observable block")

        body = _format_schema_body(
            desc, likelihoods, observable, shape, causal_event_id,
            ladder_group, ladder_step,
        )
        proposal = {
            "kind": kind_norm,
            "target": target,
            "tier": tier if kind_norm == "add_new_indicator" else "",
            "cluster_addressed": "operator_injected",
            "rationale": rationale or desc or target,
            "body": body,
            "proposed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "pending_operator_review",
        }
        if th_out is not None:
            proposal["target_hypothesis"] = th_out

        topic.setdefault("governance", {}).setdefault(
            "proposed_schema_extensions", []
        ).append(proposal)
        engine.save_topic(topic)

        queue = topic["governance"]["proposed_schema_extensions"]
        index = len(queue) - 1
        return _json({
            "slug": slug,
            "proposal_index": index,
            "proposal": proposal,
            "next": (
                "red_team_schema_extension_proposal (mandatory) → "
                "mark_schema_extension_proposal approved → "
                "apply_schema_extension_proposal"
            ),
        })
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def mark_schema_extension_proposal(
    slug: str,
    proposal_index: int,
    status: str,
    note: str = "",
) -> str:
    """Mark a schema-extension proposal approved/rejected/deferred.

    This is a review queue update only; it does not edit indicators. Approved
    proposals are inputs to a cleanup/design pass.
    """
    try:
        status_norm = (status or "").strip().lower()
        allowed = {"approved", "rejected", "deferred", "pending_operator_review"}
        if status_norm not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        queue = topic.setdefault("governance", {}).setdefault(
            "proposed_schema_extensions", []
        )
        if proposal_index < 0 or proposal_index >= len(queue):
            raise IndexError(f"proposal_index {proposal_index} out of range")
        if status_norm == "approved":
            review = queue[proposal_index].get("red_team_review") or {}
            if (review.get("verdict") or "").upper() != "APPROVE":
                raise ValueError(
                    "schema extension requires red_team_schema_extension_proposal "
                    "with verdict APPROVE before it can be marked approved"
                )
        queue[proposal_index]["status"] = status_norm
        queue[proposal_index]["reviewed_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        if note:
            queue[proposal_index]["review_note"] = note
        engine.save_topic(topic)
        return _json({
            "slug": slug,
            "proposal_index": proposal_index,
            "proposal": queue[proposal_index],
        })
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug, "proposal_index": proposal_index})


def _parse_schema_red_team_review(text: str) -> dict:
    import re

    text = text or ""
    verdict_match = re.search(r"(?im)^VERDICT:\s*(APPROVE|REVISE|REJECT)\b", text)
    verdict = verdict_match.group(1).upper() if verdict_match else "REVISE"

    def field(name: str) -> str:
        match = re.search(rf"(?ims)^{name}:\s*(.*?)(?=^[A-Z_]+:|\Z)", text)
        return match.group(1).strip() if match else ""

    return {
        "verdict": verdict,
        "risk": field("RISK"),
        "directionality": field("DIRECTIONALITY"),
        "duplicate_or_overlap": field("DUPLICATE_OR_OVERLAP"),
        "recommendation": field("RECOMMENDATION"),
        "raw": text,
    }


@mcp.tool()
def red_team_schema_extension_proposal(
    slug: str,
    proposal_index: int,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 600,
) -> str:
    """Mandatory red-team review for one schema-extension proposal."""
    store = _activity_store()
    job_id = new_job_id("red-team-schema")
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        queue = topic.setdefault("governance", {}).setdefault(
            "proposed_schema_extensions", []
        )
        if proposal_index < 0 or proposal_index >= len(queue):
            raise IndexError(f"proposal_index {proposal_index} out of range")
        proposal = queue[proposal_index]
        prompt = "\n".join([
            "Review this proposed NROL-AO schema extension adversarially.",
            "",
            "A schema extension changes future evidence extraction. It must not:",
            "- create a same-step path from one article to both schema creation and posterior movement",
            "- duplicate or overlap an existing indicator",
            "- add a one-sided indicator that amplifies an already over-covered hypothesis",
            "- use vague threshold language or non-observable criteria",
            "- route evidence through an LR vector that points in the wrong direction",
            "",
            "ANTI-INDICATOR DIRECTIONALITY (applies when the proposal targets the "
            "'anti_indicators' tier or its id matches anti_h<digit>_...):",
            "An anti-indicator's LRs are authored so FIRING SUPPRESSES its targeted "
            "hypothesis. Verify the inversion is correct:",
            "- single target (target_hypothesis is one H, e.g. 'H3'): that target H "
            "must carry the LOWEST LR in the likelihoods vector. If the target H does "
            "NOT have the lowest LR, firing would move it UP — this is the dangerous "
            "direction and must be REJECT.",
            "- multi target (target_hypothesis is a list, e.g. ['H1','H2','H3']): "
            "every target H LR must be below every non-target H LR, so firing "
            "suppresses all targets.",
            "- if target_hypothesis is absent and the id does not encode a target, "
            "inversion cannot be verified — REVISE and request the field.",
            "Note: the engine enforces this mechanically at apply (the apply will be "
            "blocked if inversion is wrong), but catch it here so the operator does "
            "not waste an apply attempt.",
            "",
            f"TOPIC: {slug}",
            f"QUESTION: {topic.get('meta', {}).get('question', '')}",
            "",
            "HYPOTHESES:",
            json.dumps(topic.get("model", {}).get("hypotheses", {}), indent=2, ensure_ascii=True),
            "",
            "EXISTING INDICATORS:",
            json.dumps([
                _indicator_brief(ind, tier)
                for tier, items in (topic.get("indicators", {}).get("tiers", {}) or {}).items()
                for ind in items or []
            ] + [
                _indicator_brief(ind, "anti_indicators")
                for ind in topic.get("indicators", {}).get("anti_indicators", []) or []
            ], indent=2, ensure_ascii=True),
            "",
            "PROPOSAL:",
            json.dumps(proposal, indent=2, ensure_ascii=True),
            "",
            "Return exactly:",
            "VERDICT: APPROVE | REVISE | REJECT",
            "RISK: <main failure mode>",
            "DIRECTIONALITY: <does LR/observable direction match the evidence direction?>",
            "DUPLICATE_OR_OVERLAP: <existing indicator overlap, if any>",
            "RECOMMENDATION: <specific required change or approval rationale>",
        ])
        store.record(
            job_id, "running", task="red_team_schema_extension_proposal",
            slug=slug, model=model or llama_client.llama_model(),
            summary={"proposal_index": proposal_index},
            prompt=prompt,
        )
        response = llama_client.chat(
            prompt,
            system_prompt=(
                "You are the RED TEAM for NROL-AO schema extensions. "
                "Return only the requested review fields."
            ),
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            # Deliberation enabled: Qwen3.6 reasons in reasoning_content, then
            # emits VERDICT:/RISK:/... in message.content. Budget MUST be large
            # enough for thinking to finish AND leave room for the answer —
            # measured on the Hormuz schema red-team prompt (~19k chars, 20
            # indicators): thinking uses ~2860 tokens, answer ~260. At 2048
            # thinking can't finish (finish_reason=length, 0 content → parser
            # defaults to empty REVISE). 4096 gives full answers with margin.
            # The NO_ANSWER_EMITTED guard below catches any future prompt that
            # pushes thinking past the budget rather than failing silently.
            disable_thinking=False,
        )
        review = _parse_schema_red_team_review(response.get("text", ""))
        review["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        review["model"] = response.get("model")
        # Defensive: if the model returned empty content (e.g. reasoning-budget
        # exhaustion on a thinking-enabled model, or a transport hiccup), do NOT
        # let the parser's REVISE default masquerade as a substantive review.
        # Surface the non-answer so an operator reruns instead of acting on it.
        if not (response.get("text") or "").strip():
            review["verdict"] = "REVISE"
            review["risk"] = "NO_ANSWER_EMITTED"
            review["recommendation"] = (
                "Red-team model returned empty content (finish_reason="
                f"{response.get('finish_reason')!r}, reasoning_chars="
                f"{response.get('reasoning_chars', 0)}). This is a non-answer, "
                "not a substantive REVISE — rerun the red-team review."
            )
        proposal["red_team_review"] = review
        proposal["red_team_required"] = True
        if review["verdict"] != "APPROVE" and proposal.get("status") == "approved":
            proposal["status"] = "pending_operator_review"
        engine.save_topic(topic)
        store.record(
            job_id, "completed", task="red_team_schema_extension_proposal",
            slug=slug, model=response.get("model"),
            summary={"proposal_index": proposal_index, "verdict": review["verdict"]},
            response=response.get("text", ""),
        )
        return _json({
            "job_id": job_id,
            "slug": slug,
            "proposal_index": proposal_index,
            "review": review,
            "proposal": proposal,
            "note": "Schema proposal red-team review is mandatory before approval/apply.",
        })
    except Exception as exc:
        try:
            store.record(
                job_id, "failed", task="red_team_schema_extension_proposal",
                slug=slug, error=str(exc),
            )
        except Exception:
            pass
        return _json({"job_id": job_id, "error": str(exc), "slug": slug, "proposal_index": proposal_index})


def _parse_schema_body(body: str) -> dict:
    """Parse the resolver's intentionally-small YAML-ish SCHEMA block."""
    import re

    body = body or ""
    out: dict[str, Any] = {}
    desc = re.search(r"(?m)^\s*desc:\s*(.+?)\s*$", body)
    if desc:
        value = desc.group(1).strip()
        if value and value != "<unchanged>":
            out["desc"] = value
    lrs = re.search(r"(?m)^\s*likelihoods:\s*\{([^}]+)\}", body)
    if lrs:
        parsed = {}
        for part in lrs.group(1).split(","):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            try:
                parsed[key.strip()] = float(value.strip())
            except ValueError:
                pass
        if parsed:
            out["likelihoods"] = parsed

    observable = {}
    for key in ("metric", "family", "direction"):
        m = re.search(rf"(?m)^\s*{key}:\s*(.+?)\s*$", body)
        if m:
            observable[key] = m.group(1).strip()
    for key in ("shape", "causal_event_id", "ladder_group", "ladder_step"):
        m = re.search(rf"(?m)^\s*{key}:\s*(.+?)\s*$", body)
        if m:
            out[key] = m.group(1).strip()
    for key in ("threshold_value", "baseline"):
        m = re.search(rf"(?m)^\s*{key}:\s*(-?\d+(?:\.\d+)?)\s*$", body)
        if m:
            observable[key] = float(m.group(1))
    if observable:
        out["observable"] = observable
    return out


@mcp.tool()
def apply_schema_extension_proposal(
    slug: str,
    proposal_index: int,
    tier: str = "",
    note: str = "",
) -> str:
    """Apply an approved schema-extension proposal to topic indicators.

    This changes schema only; it never replays evidence or moves posteriors.
    The proposal must already be marked approved by an operator. Supported
    proposal kinds: extend_observable, add_new_indicator. `no_fix` proposals
    are marked applied-noop.
    """
    cleanup_started = False
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        gov = topic.setdefault("governance", {})
        queue = gov.setdefault("proposed_schema_extensions", [])
        if proposal_index < 0 or proposal_index >= len(queue):
            raise IndexError(f"proposal_index {proposal_index} out of range")
        proposal = queue[proposal_index]
        if proposal.get("status") != "approved":
            raise ValueError("schema extension must be marked approved before apply")
        review = proposal.get("red_team_review") or {}
        if (review.get("verdict") or "").upper() != "APPROVE":
            raise ValueError(
                "schema extension requires mandatory red-team review with "
                "verdict APPROVE before apply"
            )

        kind = (proposal.get("kind") or "").strip()
        schema = _parse_schema_body(proposal.get("body") or "")
        applied: dict[str, Any] = {"kind": kind}

        if kind == "no_fix":
            applied["note"] = "no schema mutation requested"
        elif kind == "extend_observable":
            target = (proposal.get("target") or "").strip()
            indicator, indicator_tier = _find_indicator(topic, target)
            if indicator is None:
                raise ValueError(f"indicator {target!r} not found")
            if "observable" not in schema:
                raise ValueError("extend_observable proposal has no observable block")
            engine.start_indicator_cleanup_session(
                slug,
                reason=f"apply approved schema extension proposal {proposal_index}",
            )
            cleanup_started = True
            topic = engine.load_topic(slug)
            indicator, indicator_tier = _find_indicator(topic, target)
            indicator["observable"] = schema["observable"]
            engine.save_topic(topic)
            engine.commit_indicator_cleanup_session(
                slug,
                summary=f"Applied schema extension proposal {proposal_index}: {target}",
            )
            cleanup_started = False
            applied.update({"target": target, "tier": indicator_tier})
        elif kind == "add_new_indicator":
            target = (proposal.get("target") or "").strip()
            if not target:
                raise ValueError("add_new_indicator proposal missing target id")
            existing, _ = _find_indicator(topic, target)
            if existing is not None:
                raise ValueError(f"indicator {target!r} already exists")
            if "likelihoods" not in schema:
                raise ValueError("add_new_indicator proposal has no likelihoods")
            if "observable" not in schema:
                raise ValueError("add_new_indicator proposal has no observable block")
            tiers = topic.setdefault("indicators", {}).setdefault("tiers", {})
            # Resolve the target tier: an explicit `tier` argument wins; else
            # fall back to the tier the proposal was filed with (propose_schema_extension
            # stamps this); else tier3_suggestive. Without this, an anti-indicator
            # filed with tier="anti_indicators" silently lands as tier3_suggestive
            # when the operator omits the tier arg at apply — bypassing the inversion
            # lint and the anti-indicator governance coverage check.
            if not tier:
                tier = (proposal.get("tier") or "").strip() or "tier3_suggestive"
            # anti_indicators is a sibling list of tiers (indicator_schema.py),
            # not a key inside it; engine.add_indicator accepts it as a valid
            # tier. Allow it here so an approved anti-indicator proposal can be
            # applied — the engine's inversion lint (_check_anti_indicator_inversion)
            # validates the LR direction at apply time.
            allowed_tiers = set(tiers.keys()) | {"anti_indicators"}
            if tier not in allowed_tiers:
                raise ValueError(
                    f"tier must be 'anti_indicators' or one of {sorted(tiers.keys())}"
                )
            indicator = {
                "id": target,
                "desc": schema.get("desc") or proposal.get("rationale") or target,
                "posteriorEffect": (
                    proposal.get("rationale")
                    or f"Approved schema extension {target}; likelihoods define direction."
                ),
                "likelihoods": schema["likelihoods"],
                "observable": schema["observable"],
                # Decay disabled 2026-06-29 (see engine.apply_indicator_effect):
                # saturation + [0.005,0.98] clamp bound stacked firings; any
                # lr_decay < 1.0 deafens at high n_firings. Field retained as
                # dead metadata; dynamics replaces it structurally.
                "lr_decay": 1.0,
                "n_firings": 0,
                "resolution_class": False,
                "shape": schema.get("shape") or "per_event_member",
                "causal_event_id": schema.get("causal_event_id") or target,
            }
            # Forward target_hypothesis so the engine inversion lint can resolve
            # the suppress-target for anti-indicators (single H or list). Falls
            # back to an id heuristic only when absent; multi-target / non-id-
            # encoded anti-indicators need this field to be verifiable.
            th = proposal.get("target_hypothesis")
            if th:
                indicator["target_hypothesis"] = th
            if schema.get("ladder_group"):
                indicator["ladder_group"] = schema["ladder_group"]
            if schema.get("ladder_step"):
                try:
                    indicator["ladder_step"] = int(schema["ladder_step"])
                except ValueError:
                    indicator["ladder_step"] = schema["ladder_step"]
            engine.start_indicator_cleanup_session(
                slug,
                reason=f"apply approved schema extension proposal {proposal_index}",
            )
            cleanup_started = True
            added = engine.add_indicator(slug, tier, indicator, rationale=proposal.get("rationale", ""))
            topic = engine.load_topic(slug)
            added_indicator, _ = _find_indicator(topic, target)
            if added_indicator is None:
                raise ValueError(f"indicator {target!r} was not added")
            # engine.add_indicator predates observable blocks; attach the
            # reviewed observable while the cleanup session remains active.
            added_indicator["observable"] = schema["observable"]
            engine.save_topic(topic)
            engine.commit_indicator_cleanup_session(
                slug,
                summary=f"Applied schema extension proposal {proposal_index}: {target}",
            )
            cleanup_started = False
            applied.update({"target": target, "tier": tier})
        else:
            raise ValueError(f"unsupported schema proposal kind {kind!r}")

        topic = engine.load_topic(slug)
        queue = topic.setdefault("governance", {}).setdefault(
            "proposed_schema_extensions", []
        )
        proposal = queue[proposal_index]
        proposal["status"] = "applied"
        proposal["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if note:
            proposal["apply_note"] = note
        proposal["applied_result"] = applied
        gov.setdefault("schema_extension_history", []).append({
            "proposal_index": proposal_index,
            "proposal": proposal,
            "applied": applied,
        })
        engine.save_topic(topic)
        return _json({
            "slug": slug,
            "proposal_index": proposal_index,
            "applied": applied,
            "proposal": proposal,
            "note": "schema changed only; evidence was not replayed and posteriors did not move",
        })
    except Exception as exc:
        if cleanup_started:
            try:
                engine.abort_indicator_cleanup_session(
                    slug,
                    reason=f"apply schema extension proposal {proposal_index} failed: {exc}",
                )
            except Exception:
                pass
        return _json({"error": str(exc), "slug": slug, "proposal_index": proposal_index})


@mcp.tool()
def list_hypotheses(slug: str = "", active_only: bool = True) -> str:
    """List topic hypotheses without dumping full topic JSON."""
    try:
        engine = _import_from_repo("engine")
        topics = []
        selected = slug.strip()
        loaded, skipped = _load_topics(engine, [selected] if selected else None)
        for topic in loaded:
            meta = topic.get("meta", {}) or {}
            if active_only and meta.get("status") != "ACTIVE":
                continue
            hypotheses = topic.get("model", {}).get("hypotheses", {}) or {}
            topics.append(
                {
                    "slug": meta.get("slug"),
                    "title": meta.get("title"),
                    "status": meta.get("status"),
                    "classification": meta.get("classification"),
                    "hypotheses": [
                        {
                            "id": hyp_id,
                            "label": hyp.get("label") if isinstance(hyp, dict) else str(hyp),
                            "description": hyp.get("desc") if isinstance(hyp, dict) else "",
                            "posterior": hyp.get("posterior") if isinstance(hyp, dict) else None,
                        }
                        for hyp_id, hyp in hypotheses.items()
                    ],
                }
            )
        out = {"topics": topics, "count": len(topics)}
        if skipped:
            out["skipped"] = skipped
        return _json(out)
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def topic_status(slugs: list[str] | None = None, active_only: bool = True) -> str:
    """Return per-topic freshness, governance queues, and scan status."""
    try:
        engine = _import_from_repo("engine")
        loaded, skipped = _load_topics(engine, slugs)
        rows = [
            _topic_scan_status(topic)
            for topic in loaded
            if not active_only or topic.get("meta", {}).get("status") == "ACTIVE"
        ]
        rows.sort(key=lambda r: (not r.get("scanStale", True), -(r.get("scanAgeHours") or 999999)))
        out = {"topics": rows, "count": len(rows)}
        if skipped:
            out["skipped"] = skipped
        return _json(out)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def build_news_scan_plan(slugs: list[str] | None = None, max_topics: int = 5) -> str:
    """Build debug/manual news scan prompts for selected/stale topics.

    This does not search the web or mutate state. The normal workflow is
    `run_news_scan`, which performs MCP-side search and deliberation.
    """
    try:
        engine = _import_from_repo("engine")
        mutation = _import_from_repo("framework.news_mutation")
        rows = []
        selected = set(slugs or [])
        loaded, _ = _load_topics(engine, slugs)
        topics = [t for t in loaded if t.get("meta", {}).get("status") == "ACTIVE"]
        if not selected:
            topics.sort(key=lambda t: (_topic_scan_status(t).get("scanStale") is False, -(_topic_scan_status(t).get("scanAgeHours") or 999999)))
            topics = topics[: max(1, min(int(max_topics), 20))]

        max_rounds = mutation.budget_for_scan(len(topics) or 1)
        for topic in topics:
            meta = topic.get("meta", {})
            classification = (meta.get("classification") or "ROUTINE").upper()
            floor = 12 if classification == "ALERT" else (7 * 24 if classification == "CALIBRATION" else 72)
            window = _scan_search_window(topic, tempo_floor_hours=floor)
            window["ddg_timelimit"] = _ddg_timelimit_for_window(window.get("hours", 24.0))
            prompts = {}
            for hyp in topic.get("model", {}).get("hypotheses", {}):
                prompts[hyp] = mutation.build_hypothesis_search_prompt(
                    topic,
                    hyp,
                    round_num=1,
                    time_window=window["label"],
                    prior_articles=[],
                )
            prompts["wildcard"] = mutation.build_wildcard_search_prompt(
                topic,
                round_num=1,
                time_window=window["label"],
                prior_articles=[],
            )
            rows.append({
                "slug": meta.get("slug"),
                "title": meta.get("title"),
                "scan_status": _topic_scan_status(topic),
                "max_rounds": max_rounds,
                "time_window": window,
                "channels": list(prompts.keys()),
                "search_prompts": prompts,
            })
        return _json({
            "provider": "debug/manual",
            "instructions": (
                "Preview only. For the real workflow call run_news_scan so MCP performs "
                "search, matcher deliberation, and returns an operator packet."
            ),
            "topics": rows,
        })
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def apply_news_scan_results(
    slug: str,
    search_results_by_channel: dict,
    matcher_output: str = "",
    commit: bool = False,
) -> str:
    """Dedupe model-produced ARTICLE blocks, build/apply matcher decisions.

    search_results_by_channel maps channel names such as H1/H2/wildcard to the
    raw ARTICLE-block text returned by a search model. If matcher_output is
    empty, this returns the matcher prompt for any model/provider to run. If
    matcher_output is supplied, it parses decisions and optionally commits them.
    """
    store = _activity_store()
    job_id = new_job_id("news-scan")
    start = time.time()
    try:
        engine = _import_from_repo("engine")
        mutation = _import_from_repo("framework.news_mutation")
        news = _import_from_repo("framework.news_observation_pipeline")
        topic = engine.load_topic(slug)
        parsed = {
            channel: mutation.parse_search_response(text or "")
            for channel, text in (search_results_by_channel or {}).items()
        }
        deduped, surfaced = mutation.dedupe_articles(parsed)
        matcher_prompt = _build_matcher_prompt(news, topic, deduped) if deduped else ""
        store.record(
            job_id,
            "running",
            task="apply_news_scan_results",
            slug=slug,
            summary={
                "channels": list(parsed.keys()),
                "article_count": len(deduped),
                "commit": commit,
                "has_matcher_output": bool(matcher_output),
            },
        )
        if not matcher_output:
            store.record(
                job_id,
                "completed",
                task="apply_news_scan_results",
                slug=slug,
                duration_ms=int((time.time() - start) * 1000),
                summary={"article_count": len(deduped), "needs_matcher": True},
            )
            return _json({
                "job_id": job_id,
                "slug": slug,
                "articles": deduped,
                "surfaced": surfaced,
                "matcher_prompt": matcher_prompt,
                "committed": False,
                "next_step": "Run matcher_prompt with any model, then call apply_news_scan_results again with matcher_output.",
            })
        decisions = news.parse_matcher_output(matcher_output)
        if not commit:
            store.record(
                job_id,
                "completed",
                task="apply_news_scan_results",
                slug=slug,
                duration_ms=int((time.time() - start) * 1000),
                summary={"article_count": len(deduped), "decision_count": len(decisions), "committed": False},
            )
            return _json({
                "job_id": job_id,
                "slug": slug,
                "articles": deduped,
                "decisions": decisions,
                "committed": False,
            })
        denied = _ask_loom_permission(
            "nrol_ao_apply_news_scan_results",
            {"slug": slug, "article_count": len(deduped), "decision_count": len(decisions)},
        )
        if denied:
            store.record(job_id, "denied", task="apply_news_scan_results", slug=slug, summary={"denied": denied})
            return _json({"job_id": job_id, "denied": denied, "committed": False})
        summary = news.apply_decisions(slug, deduped, decisions)
        try:
            mutation.stamp_last_scanned(slug)
        except Exception:
            pass
        store.record(
            job_id,
            "completed",
            task="apply_news_scan_results",
            slug=slug,
            duration_ms=int((time.time() - start) * 1000),
            summary=summary,
        )
        return _json({"job_id": job_id, "slug": slug, "committed": True, "summary": summary})
    except Exception as exc:
        store.record(
            job_id,
            "failed",
            task="apply_news_scan_results",
            slug=slug,
            duration_ms=int((time.time() - start) * 1000),
            error=str(exc),
        )
        return _json({"job_id": job_id, "error": str(exc), "slug": slug})


@mcp.tool()
def run_news_scan(
    slugs: list[str] | None = None,
    max_topics: int = 3,
    max_results_per_channel: int = 4,
    commit: bool = False,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 900,
    dry_run: bool = False,
    commit_policy: str = "",
    fetch_full_articles: bool = True,
    excerpt_chars: int = 2800,
    deliberate: bool = True,
    brief: bool = False,
) -> str:
    """Run the full NROL-AO news scan on the MCP/server side.

    fetch_full_articles=true (default) downloads each deduped article and
    feeds the matcher a bounded readable-text excerpt alongside the search
    snippet — snippets alone rarely contain the numeric values OBSERVE
    decisions require.

    deliberate=true (default) runs the 3-stage advocate/rebut/jury debate
    over the strict matcher's PARKs. Jury MOVE_TO verdicts supersede the
    PARK and flow into the same proposal/commit gates as direct matcher
    decisions — the debate widens recall, never authority.

    brief=true returns a COMPACT summary (counts, decisions by kind,
    proposals filed, freshness downgrades, scan coverage) — enough for the
    operator to brief the human and act on the proposal queue without a huge
    in-context blob. The full packet (articles, excerpts, deliberation) is
    still written to the digest on disk; read it with read_scan_run /
    latest_digest. brief=false (default) returns the full operator packet.
    In operator mode (file tools stripped) pass brief=true — the full packet
    plus a digest_path is a common trigger for attempted sandbox break-outs.

    This is the one-call operational path: select stale topics, perform
    server-side web search, dedupe articles, deliberate with the local
    llama-server matcher, parse FIRE/OBSERVE/PARK/SCHEMA_GAP decisions, and
    optionally apply through NROL engine gates after Loom approval.

    dry_run=true never mutates state and does not stamp lastScanned.
    dry_run=false records successful scan coverage by stamping lastScanned.
    commit=true applies evidence decisions after Loom approval only when
    commit_policy is not "safe".

    commit_policy="safe" is the scheduled-scan policy (spec Flow B): PARK
    and SCHEMA_GAP decisions auto-apply (they cannot move posteriors —
    engine-enforced), while FIRE/OBSERVE decisions are filed as pending
    proposals for operator review via list_proposals/commit_match, even if
    commit=true is supplied. No posterior ever moves without a human approving
    a proposal or direct non-safe commit. A digest is written beside the
    activity ledger.
    """
    store = _activity_store()
    job_id = new_job_id("news-scan-worker")
    start = time.time()
    try:
        engine = _import_from_repo("engine")
        mutation = _import_from_repo("framework.news_mutation")
        news = _import_from_repo("framework.news_observation_pipeline")
        pipeline = _import_from_repo("framework.pipeline")
        topics = _select_scan_topics(engine, slugs, max_topics)
        store.record(
            job_id,
            "running",
            task="run_news_scan",
            model=model or llama_client.llama_model(),
            summary={
                "phase": "planning",
                "slugs": slugs or "(stale-active)",
                "topics": [t.get("meta", {}).get("slug") for t in topics],
                "commit": commit,
                "dry_run": dry_run,
            },
        )

        topic_packets = []
        total_articles = 0
        total_decisions = 0
        for topic in topics:
            meta = topic.get("meta", {}) or {}
            slug = meta.get("slug")
            classification = (meta.get("classification") or "ROUTINE").upper()
            floor = 12 if classification == "ALERT" else (7 * 24 if classification == "CALIBRATION" else 72)
            window = _scan_search_window(topic, tempo_floor_hours=floor)
            timelimit_code = _ddg_timelimit_for_window(window.get("hours", 24.0))
            window["ddg_timelimit"] = timelimit_code
            query_specs = _search_query_specs(topic, window.get("label", "recent period"))
            channels = [spec["channel"] for spec in query_specs]

            store.record(
                job_id,
                "running",
                task="run_news_scan",
                slug=slug,
                model=model or llama_client.llama_model(),
                summary={
                    "phase": "searching",
                    "channels": channels,
                    "window": window.get("label"),
                    "ddg_timelimit": timelimit_code,
                },
            )
            parsed_by_channel = {}
            queries = {}
            search_errors = {}
            for spec in query_specs:
                channel = spec["channel"]
                query = spec["query"]
                queries[channel] = query
                try:
                    parsed_by_channel[channel] = _search_web_articles(
                        query,
                        channel,
                        max_results_per_channel,
                        timelimit=timelimit_code,
                    )
                except Exception as exc:
                    search_errors[channel] = str(exc)
                    parsed_by_channel[channel] = []

            deduped_raw, surfaced = mutation.dedupe_articles(parsed_by_channel)
            deduped, freshness_stats = _filter_scan_articles(
                topic,
                deduped_raw,
                window,
                drop_old_dated=not fetch_full_articles,
            )
            matcher_output = ""
            decisions = []
            applied = None
            packet_policy = None
            excerpt_stats = None
            debate_packet = None
            jury_overrides = {}
            if deduped and fetch_full_articles:
                store.record(
                    job_id,
                    "running",
                    task="run_news_scan",
                    slug=slug,
                    model=model or llama_client.llama_model(),
                    summary={"phase": "fetching", "article_count": len(deduped)},
                )
                fetched = 0
                fetch_errors = 0
                metadata_only = 0
                for item in deduped:
                    art = item.get("article", item) if isinstance(item, dict) else item
                    payload = _fetch_article_payload(art.get("url", ""), excerpt_chars)
                    if isinstance(payload, str):
                        payload = {"excerpt": payload}
                    if payload.get("fetch_error"):
                        art["fetch_error"] = payload["fetch_error"]
                        fetch_errors += 1
                    payload_published = payload.get("published")
                    if payload_published:
                        existing_published = (
                            art.get("published") or art.get("date") or art.get("time") or ""
                        )
                        existing_dt = _normalize_scan_datetime(existing_published)
                        payload_dt = _normalize_scan_datetime(payload_published)
                        if not existing_dt or (payload_dt and payload_dt > existing_dt):
                            if existing_published:
                                art.setdefault("search_published", existing_published)
                            art["published"] = payload_published
                            art["date"] = payload_published
                    for meta_key in ("metadata_title", "metadata_source", "metadata_host", "metadata_url"):
                        if payload.get(meta_key) and not art.get(meta_key):
                            art[meta_key] = payload[meta_key]
                    excerpt = payload.get("excerpt") or ""
                    if excerpt:
                        art["excerpt"] = excerpt
                        fetched += 1
                    elif payload and not payload.get("fetch_error"):
                        metadata_only += 1
                deduped, postfetch_freshness_stats = _filter_scan_articles(topic, deduped, window)
                freshness_stats = {
                    **postfetch_freshness_stats,
                    "prefetch": freshness_stats,
                    "postfetch": postfetch_freshness_stats,
                }
                retained_fetched = sum(
                    1
                    for item in deduped
                    if (item.get("article", item) if isinstance(item, dict) else item).get("excerpt")
                )
                excerpt_stats = {
                    "fetched": retained_fetched,
                    "attempted_fetched": fetched,
                    "fetch_errors": fetch_errors,
                    "metadata_only": metadata_only,
                    "of": len(deduped),
                    "prefetch_of": freshness_stats["prefetch"].get("kept"),
                    "chars_cap": excerpt_chars,
                }
            total_articles += len(deduped)
            if deduped:
                matcher_prompt = _build_matcher_prompt(news, topic, deduped)
                store.record(
                    job_id,
                    "running",
                    task="run_news_scan",
                    slug=slug,
                    model=model or llama_client.llama_model(),
                    prompt=matcher_prompt,
                    summary={"phase": "matching", "article_count": len(deduped)},
                )
                response = llama_client.chat(
                    matcher_prompt,
                    system_prompt=(
                        "You are the NROL-AO evidence matcher. Return only DECISION blocks "
                        "in the requested format. Do not invent indicators, likelihoods, or posteriors."
                    ),
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout_sec=timeout_sec,
                    disable_thinking=True,
                )
                matcher_output = response.get("text", "")
                if not matcher_output.strip():
                    # An empty matcher is a failure, not a quiet news window —
                    # reasoning models can spend the whole token budget in the
                    # think channel and return no content at all.
                    search_errors["matcher"] = (
                        "matcher returned no content "
                        f"(finish_reason={response.get('finish_reason') or '?'}, "
                        f"reasoning_chars={response.get('reasoning_chars', 0)}, "
                        f"model={response.get('model') or 'default'}) — "
                        "articles NOT marked scanned; raise max_tokens or check the chat template"
                    )
                decisions = news.parse_matcher_output(matcher_output)
                total_decisions += len(decisions)

                if deliberate and decisions and "matcher" not in search_errors:
                    jury_overrides, debate_packet = _run_debate(
                        topic, deduped, decisions, news,
                        model=model, temperature=temperature,
                        max_tokens=max_tokens, timeout_sec=timeout_sec,
                        store=store, job_id=job_id, slug=slug,
                    )
                    if debate_packet and "error" in debate_packet:
                        search_errors["debate"] = debate_packet["error"]
                    decisions = _apply_jury_overrides(decisions, jury_overrides)

                if "debate" not in search_errors:
                    if commit_policy == "safe" and not dry_run and decisions:
                        safe_decisions, review_decisions, safe_policy_audit = _split_safe_policy_decisions(
                            news, deduped, decisions
                        )

                        applied = {}
                        if safe_decisions:
                            applied = news.apply_decisions(slug, deduped, safe_decisions)
                        proposals_filed = []
                        pstore = _proposal_store()
                        for d in review_decisions:
                            idx = d.get("idx") or 0
                            idx_int = _to_int_idx(idx)
                            art = deduped[idx_int - 1] if 0 < idx_int <= len(deduped) else None
                            if art is None:
                                continue
                            try:
                                # 1. Park duplicates first to get their evidence IDs in engine
                                dups = d.get("_duplicate_decisions") or []
                                secondary_evidence_ids = []
                                for dup_d in dups:
                                    dup_idx = dup_d["idx"]
                                    dup_idx_int = _to_int_idx(dup_idx)
                                    dup_art = deduped[dup_idx_int - 1] if 0 < dup_idx_int <= len(deduped) else None
                                    if dup_art is None:
                                        continue
                                    dup_inner = dup_art.get("article", dup_art)
                                    dup_entry = news.article_to_evidence_entry(
                                        dup_inner, round_num=1,
                                        default_tag=dup_d.get("tag", "EVENT") or "EVENT",
                                    )
                                    dup_entry["claim"] = dup_d.get("claim") or dup_entry.get("text", "")
                                    dup_result = pipeline.process_evidence(
                                        slug, dup_entry,
                                        fired_indicator_id=None,
                                        reason=f"Duplicate coverage of canonical article A{idx}",
                                    )
                                    if dup_result.get("evidence_id"):
                                        secondary_evidence_ids.append(dup_result["evidence_id"])
                                
                                # 2. File proposal for canonical article
                                art_rec = pstore.submit_article(art, submitted_by="scheduled-scan")
                                action = d.get("action", {})
                                _validate_proposal_shape(
                                    topic, action.get("kind", ""),
                                    action.get("indicator_id", ""), action.get("value"),
                                )
                                scan_delib = _deliberation_stamp_from_debate(d, debate_packet)
                                prop = pstore.add_proposal(
                                    article_id=art_rec["id"],
                                    slug=slug,
                                    action=action.get("kind", ""),
                                    indicator_id=action.get("indicator_id", ""),
                                    observed_value=action.get("value"),
                                    rationale=(d.get("reason") or d.get("claim") or "matcher decision")[:500],
                                    evidence_refs=json.dumps(secondary_evidence_ids) if secondary_evidence_ids else "",
                                    deliberation=json.dumps(
                                        {"deliberation": scan_delib} if scan_delib
                                        else {"deliberationWaiver": "news scan ran with deliberate=false"}
                                    ),
                                )
                                proposals_filed.append(prop["id"])
                            except Exception as exc:
                                search_errors[f"proposal_idx_{idx}"] = str(exc)
                        packet_policy = {
                            "policy": "safe",
                            "commit_requested": bool(commit),
                            "posterior_movers_forced_to_review": True,
                            "auto_committed": (applied or {}),
                            "proposals_filed": proposals_filed,
                            "safe_policy_audit": safe_policy_audit,
                        }

                    if commit and commit_policy != "safe":
                        denied = _ask_loom_permission(
                            "nrol_ao_run_news_scan",
                            {
                                "slug": slug,
                                "article_count": len(deduped),
                                "decision_count": len(decisions),
                                "model": response.get("model"),
                            },
                        )
                        if denied:
                            store.record(
                                job_id,
                                "denied",
                                task="run_news_scan",
                                slug=slug,
                                model=response.get("model"),
                                summary={"denied": denied},
                            )
                            applied = {"denied": denied, "committed": False}
                        else:
                            applied = news.apply_decisions(slug, deduped, decisions)

            search_failed_all = bool(channels) and all(c in search_errors for c in channels)
            matcher_failed = "matcher" in search_errors
            debate_failed = "debate" in search_errors
            scan_record = {
                "recorded": False,
                "dry_run": dry_run,
                "skipped_reason": "",
            }
            if dry_run:
                scan_record["skipped_reason"] = "dry_run=true"
            elif search_failed_all:
                scan_record["skipped_reason"] = "all search channels failed"
            elif matcher_failed:
                # Stamping lastScanned would shrink the next adaptive window
                # and silently drop these articles from ever being matched.
                scan_record["skipped_reason"] = "matcher returned no content — window left open"
            elif debate_failed:
                scan_record["skipped_reason"] = f"deliberation failed: {search_errors['debate']} — window left open"
            else:
                try:
                    stamped = mutation.stamp_last_scanned(slug)
                    meta["lastScanned"] = stamped
                    scan_record = {"recorded": True, "dry_run": False, "timestamp": stamped}
                except Exception as exc:
                    scan_record["skipped_reason"] = f"stamp failed: {exc}"

            packet = {
                "slug": slug,
                "title": meta.get("title"),
                "scan_status": _topic_scan_status(topic),
                "time_window": window,
                "queries": queries,
                "search_errors": search_errors,
                "raw_article_count": len(deduped_raw),
                "freshness_filter": freshness_stats,
                "articles": deduped,
                "surfaced": surfaced,
                "excerpts": excerpt_stats,
                "decisions": decisions,
                "deliberation": debate_packet,
                "matcher_output": matcher_output,
                "committed": bool(commit and applied and not applied.get("denied")),
                "dry_run": dry_run,
                "scan_record": scan_record,
                "applied": applied,
                "commit_policy": packet_policy,
            }
            topic_packets.append(packet)

        operator_packet = {
            "job_id": job_id,
            "committed": bool(commit),
            "dry_run": dry_run,
            "commit_policy": commit_policy or None,
            "topics_scanned": len(topic_packets),
            "article_count": total_articles,
            "decision_count": total_decisions,
            "topics": topic_packets,
        }
        if not dry_run:
            try:
                operator_packet["digest_path"] = _write_digest(operator_packet)
            except Exception as exc:
                operator_packet["digest_error"] = str(exc)
        store.record(
            job_id,
            "completed",
            task="run_news_scan",
            model=model or llama_client.llama_model(),
            duration_ms=int((time.time() - start) * 1000),
            response=_json(operator_packet),
            summary={
                "topics_scanned": len(topic_packets),
                "article_count": total_articles,
                "decision_count": total_decisions,
                "commit": commit,
                "dry_run": dry_run,
                "scan_records": [
                    p.get("scan_record") for p in topic_packets if p.get("scan_record", {}).get("recorded")
                ],
            },
        )
        if brief:
            return _json(_brief_scan_packet(operator_packet, job_id))
        return _json(operator_packet)
    except Exception as exc:
        store.record(
            job_id,
            "failed",
            task="run_news_scan",
            model=model or llama_client.llama_model(),
            duration_ms=int((time.time() - start) * 1000),
            error=str(exc),
        )
        return _json({"job_id": job_id, "error": str(exc), "committed": False})


def _brief_scan_packet(packet: dict, job_id: str) -> dict:
    """Compact scan summary for operator mode.

    The full operator packet (articles, excerpts, deliberation, matcher_output)
    is large and — combined with a digest_path the operator can't reach without
    file tools — a common trigger for attempted sandbox break-outs. This brief
    carries everything the operator needs to brief the human and act on the
    proposal queue: per-topic decision counts by kind, proposals filed,
    freshness downgrades, scan coverage, and read-back pointers. The full
    packet stays on disk in the digest; read it with read_scan_run /
    latest_digest (the dashboard surfaces it too).
    """
    topics_brief = []
    for tp in packet.get("topics", []):
        decisions = tp.get("decisions") or []
        # Resolve this topic's anti-indicator ids so we can surface anti-indicator
        # matches distinctly (ANTI_FIRE / ANTI_OBSERVE) instead of collapsing them
        # into FIRE/OBSERVE. Anti-indicators are NOT a distinct posterior
        # semantic: they flow through the same bayesian_update path as tier
        # indicators, with their likelihoods applied verbatim. "Anti" is a
        # design-time authoring convention — the LRs are authored so the target
        # H carries the lowest value (validated by the inversion lint at
        # lint_indicators.py:195), so firing suppresses the target H. The relabel
        # exists for falsification-evidence visibility: an ANTI_FIRE is evidence
        # against its target hypothesis, and the operator should read it as
        # falsification, not as hypothesis-strengthening. Lazy + best-effort: if
        # the topic can't be loaded, fall back to plain FIRE/OBSERVE.
        anti_ids: set[str] = set()
        slug = tp.get("slug") or ""
        if slug:
            try:
                _eng = _import_from_repo("engine")
                _sch = _import_from_repo("framework.indicator_schema")
                anti_ids = {a.get("id") for a in _sch.anti_indicators_for_topic(_eng.load_topic(slug)) if a.get("id")}
            except Exception:
                pass
        # Tally decision actions by kind. A FIRE/OBSERVE whose indicator_id is an
        # anti-indicator is tallied as ANTI_FIRE / ANTI_OBSERVE for visibility.
        by_kind: dict[str, int] = {}
        proposal_kinds: list[str] = []
        for d in decisions:
            act = (d.get("action") or {})
            kind = act.get("kind", "?")
            ind_id = act.get("indicator_id") or ""
            if kind in {"FIRE", "OBSERVE"} and ind_id in anti_ids:
                kind = f"ANTI_{kind}"
            by_kind[kind] = by_kind.get(kind, 0) + 1
            # Proposals filed under safe policy are posterior-movers awaiting commit.
        commit_policy = tp.get("commit_policy") or {}
        auto = (commit_policy.get("auto_committed") if isinstance(commit_policy, dict) else {}) or {}
        proposals_filed = commit_policy.get("proposals_filed") if isinstance(commit_policy, dict) else None
        safe_audit = commit_policy.get("safe_policy_audit") if isinstance(commit_policy, dict) else {}
        downgrades = (safe_audit.get("freshness_downgrades") if isinstance(safe_audit, dict) else []) or []
        # Map the applied-decision evidence_id onto each downgrade so the
        # brief gives the operator a row handle, not just a marker. The
        # downgrade audit carries idx; apply_decisions labels each result
        # row article="A{idx}" from that same idx, so we join on the label.
        # Best-effort: a downgrade whose row can't be matched (e.g. bundled
        # edge case) emits evidence_id=None and the operator falls back to
        # review_parked on the slug rather than re-reading the full digest.
        applied_results = (tp.get("applied") or {}).get("results") if isinstance(tp.get("applied"), dict) else None
        ev_id_by_article = {}
        if isinstance(applied_results, list):
            for res in applied_results:
                if isinstance(res, dict) and res.get("article") and res.get("evidence_id"):
                    ev_id_by_article[res["article"]] = res["evidence_id"]
        topics_brief.append({
            "slug": tp.get("slug"),
            "title": tp.get("title"),
            "scan_status": tp.get("scan_status"),
            "time_window": tp.get("time_window"),
            "raw_article_count": tp.get("raw_article_count"),
            "decision_count": len(decisions),
            "decisions_by_kind": by_kind,
            "deliberation": (tp.get("deliberation") or {}).get("candidates")
                and {k: (tp["deliberation"].get(k)) for k in ("candidates", "parks", "argue_moves", "rescued")}
                or None,
            "freshness_downgrades": len(downgrades),
            "freshness_downgrade_samples": [
                {"idx": r.get("idx"),
                 "kind": (r.get("original_action") or {}).get("kind"),
                 "claim": (r.get("claim") or "")[:120],
                 "evidence_id": ev_id_by_article.get(f"A{_to_int_idx(r.get('idx'))}")}
                for r in downgrades[:3]
            ],
            "committed": tp.get("committed"),
            "applied": tp.get("applied"),
            "scan_record": tp.get("scan_record"),
            "search_errors": tp.get("search_errors"),
            "proposals_filed_count": len(proposals_filed) if isinstance(proposals_filed, list) else 0,
        })
    return {
        "job_id": job_id,
        "brief": True,
        "committed": packet.get("committed"),
        "dry_run": packet.get("dry_run"),
        "commit_policy": packet.get("commit_policy"),
        "topics_scanned": packet.get("topics_scanned"),
        "article_count": packet.get("article_count"),
        "decision_count": packet.get("decision_count"),
        "topics": topics_brief,
        "digest_available": ("digest_path" in packet) or ("digest_error" not in packet and not packet.get("dry_run")),
        "digest_error": packet.get("digest_error"),
        "read_back": "Full packet (articles, excerpts, deliberation) is in the on-disk digest. "
                     "Read it with read_scan_run (list_scan_runs for ids) or latest_digest; "
                     "the dashboard surfaces it at /mcp-activity. Do NOT attempt to write/read "
                     "the digest file directly in operator mode.",
    }


def _write_digest(packet: dict) -> str:
    """Write a scan digest (json + markdown) beside the activity ledger.

    The digest makes non-mutations visible: a scan that found nothing, or
    only parked evidence, is reported with the same weight as one that
    moved posteriors — otherwise operators pressure the system to "do
    something", which is how freeform updates return.
    """
    root = _activity_store().snapshot_path.parent / "digests"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (root / f"digest-{stamp}.json").write_text(_json(packet), encoding="utf-8")

    lines = [
        f"# NROL-AO scan digest — {stamp}",
        "",
        f"- topics scanned: {packet.get('topics_scanned')}",
        f"- articles (deduped): {packet.get('article_count')}",
        f"- matcher decisions: {packet.get('decision_count')}",
        f"- commit policy: {packet.get('commit_policy') or 'review-only'}",
        "",
    ]
    for tp in packet.get("topics", []):
        lines.append(f"## {tp.get('slug')}")
        kinds: dict[str, int] = {}
        for d in tp.get("decisions", []) or []:
            kind = (d.get("action") or {}).get("kind", "?")
            kinds[kind] = kinds.get(kind, 0) + 1
        lines.append(
            f"- articles: {len(tp.get('articles') or [])}; decisions: "
            + (", ".join(f"{k}×{v}" for k, v in sorted(kinds.items())) or "none")
        )
        if tp.get("excerpts"):
            ex = tp["excerpts"]
            lines.append(
                f"- article excerpts: {ex.get('fetched', 0)}/{ex.get('of', 0)} fetched "
                f"(cap {ex.get('chars_cap')} chars)"
            )
        if tp.get("deliberation"):
            db = tp["deliberation"]
            if db.get("error"):
                lines.append(f"- ⚠ DEBATE FAILED: {db['error']}")
            else:
                lines.append(
                    f"- deliberation: {db.get('candidates', 0)} candidates debated ({db.get('parks', 0)} parks), "
                    f"{db.get('argue_moves', 0)} argued, "
                    f"{db.get('rescued', 0)} rescued by jury"
                )
        policy = tp.get("commit_policy") or {}
        if policy:
            auto = policy.get("auto_committed") or {}
            lines.append(
                f"- recorded as non-moving evidence (safe): park={auto.get('park', 0)} "
                f"schema_gap={auto.get('schema_gap', 0)} "
                f"rejections={auto.get('engine_rejections', 0)}"
            )
            filed = policy.get("proposals_filed") or []
            lines.append(
                f"- proposals filed for review: {len(filed)}"
                + (f" ({', '.join(filed)})" if filed else "")
            )
            audit = policy.get("safe_policy_audit") or {}
            downgrades = audit.get("freshness_downgrades") or []
            if downgrades:
                lines.append(
                    f"- ACTION REQUIRED: freshness downgrades: {len(downgrades)} "
                    "posterior-moving decision(s) converted to PARK because "
                    "publication date was missing"
                )
                for row in downgrades[:5]:
                    original = (row.get("original_action") or {}).get("kind", "?")
                    replacement = (row.get("replacement_action") or {}).get("kind", "?")
                    lines.append(
                        f"  - A{row.get('idx')}: {original} -> {replacement}; "
                        f"{(row.get('claim') or '')[:120]}"
                    )
        if tp.get("search_errors"):
            lines.append(f"- search errors: {tp['search_errors']}")
        rec = tp.get("scan_record") or {}
        lines.append(
            f"- scan coverage: {'recorded ' + str(rec.get('timestamp')) if rec.get('recorded') else rec.get('skipped_reason', 'not recorded')}"
        )
        if not (tp.get("decisions") or []):
            if "matcher" in (tp.get("search_errors") or {}):
                lines.append("- ⚠ MATCHER FAILED — zero decisions is an error here, not a quiet window")
            elif tp.get("articles"):
                lines.append("- no actionable evidence this window (a valid result)")
            else:
                lines.append("- no articles surfaced this window (a valid result)")
        lines.append("")
    (root / f"digest-{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    return str(root / f"digest-{stamp}.md")


@mcp.tool()
def latest_digest() -> str:
    """Return the most recent scan digest (markdown) and its path."""
    try:
        root = _activity_store().snapshot_path.parent / "digests"
        files = sorted(root.glob("digest-*.md"))
        if not files:
            return _json({"digest": None, "note": "no digests yet — run run_news_scan"})
        latest = files[-1]
        return _json({"path": str(latest), "digest": latest.read_text(encoding="utf-8")})
    except Exception as exc:
        return _json({"error": str(exc)})


def _digest_root() -> Path:
    return _activity_store().snapshot_path.parent / "digests"


def _read_digest_json(path: Path) -> dict:
    if path.suffix.lower() == ".md":
        path = path.with_suffix(".json")
    root = _digest_root().resolve()
    resolved = path.resolve()
    if root not in resolved.parents and resolved != root:
        raise ValueError(f"digest path must stay under {root}")
    return json.loads(resolved.read_text(encoding="utf-8"))


def _scan_packet_article_count(packet: dict) -> int:
    counts = []
    try:
        counts.append(int(packet.get("article_count") or 0))
    except Exception:
        pass
    for topic_packet in packet.get("topics", []) or []:
        if not isinstance(topic_packet, dict):
            continue
        for key in ("raw_article_count", "article_count"):
            try:
                counts.append(int(topic_packet.get(key) or 0))
            except Exception:
                pass
        if "articles" in topic_packet:
            try:
                counts.append(len(topic_packet.get("articles") or []))
            except Exception:
                pass
    return max(counts or [0])


def _scan_packet_slugs(packet: dict) -> set[str]:
    return {
        str(topic_packet.get("slug") or "")
        for topic_packet in packet.get("topics", []) or []
        if isinstance(topic_packet, dict) and topic_packet.get("slug")
    }


def _scan_packet_matches(packet: dict, job_id: str, slug: str, min_article_count: int) -> bool:
    if job_id and packet.get("job_id") != job_id:
        return False
    if slug and slug not in _scan_packet_slugs(packet):
        return False
    if job_id:
        return True
    return _scan_packet_article_count(packet) >= min_article_count


def _compact_activity_event(event: dict) -> dict:
    fields = event
    summary = fields.get("summary")
    if isinstance(summary, dict):
        # Mirror ActivityStore.record: the full topic is preserved in the
        # audit log; the compact snapshot must drop summary.topic, which can
        # run to megabytes and blow the list_activity transport frame.
        summary = {k: v for k, v in summary.items() if k != "topic"}
    compact = {
        "job_id": fields.get("job_id"),
        "status": fields.get("status"),
        "updated_at": fields.get("time"),
        "task": fields.get("task"),
        "slug": fields.get("slug"),
        "model": fields.get("model"),
        "transition": fields.get("transition"),
        "duration_ms": fields.get("duration_ms"),
        "error": fields.get("error"),
        "summary": summary,
    }
    return {k: v for k, v in compact.items() if v is not None}


def _rewrite_activity_without_jobs(job_ids: set[str]) -> dict:
    store = _activity_store()
    log_path = store.log_path
    if not log_path.exists():
        return {"activity_events_removed": 0, "activity_log": str(log_path)}

    kept_events = []
    removed = 0
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            kept_events.append(line)
            continue
        if event.get("job_id") in job_ids:
            removed += 1
            continue
        kept_events.append(event)

    tmp = log_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for event in kept_events:
            if isinstance(event, str):
                f.write(event + "\n")
            else:
                f.write(json.dumps(event, ensure_ascii=True, default=str) + "\n")
    tmp.replace(log_path)

    latest_by_job: dict[str, dict] = {}
    for event in kept_events:
        if not isinstance(event, dict):
            continue
        jid = event.get("job_id")
        if jid:
            latest_by_job[jid] = _compact_activity_event(event)
    jobs = sorted(
        latest_by_job.values(),
        key=lambda j: j.get("updated_at", ""),
        reverse=True,
    )[:100]
    snapshot = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "active": sum(1 for j in jobs if j.get("status") in {"queued", "running"}),
        "jobs": jobs,
    }
    store._write_snapshot(snapshot)
    return {
        "activity_events_removed": removed,
        "activity_log": str(log_path),
        "snapshot": str(store.snapshot_path),
    }


def _article_for_proposal(item: dict) -> dict:
    return item.get("article", item) if isinstance(item, dict) else item


@mcp.tool()
def list_scan_runs(limit: int = 20) -> str:
    """List stored scan digest JSON files available for replay/inspection."""
    try:
        root = _digest_root()
        files = sorted(root.glob("digest-*.json"), reverse=True)
        rows = []
        for path in files[: max(1, min(int(limit), 200))]:
            try:
                packet = json.loads(path.read_text(encoding="utf-8"))
                rows.append({
                    "path": str(path),
                    "markdown_path": str(path.with_suffix(".md")),
                    "job_id": packet.get("job_id"),
                    "topics_scanned": packet.get("topics_scanned"),
                    "article_count": packet.get("article_count"),
                    "decision_count": packet.get("decision_count"),
                    "commit_policy": packet.get("commit_policy"),
                    "dry_run": packet.get("dry_run"),
                })
            except Exception as exc:
                rows.append({"path": str(path), "error": str(exc)})
        return _json({"count": len(rows), "runs": rows})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def read_scan_run(path: str = "") -> str:
    """Read a stored scan digest JSON. Empty path reads the latest run."""
    try:
        root = _digest_root()
        if path:
            packet = _read_digest_json(Path(path))
            source = str(Path(path))
        else:
            files = sorted(root.glob("digest-*.json"))
            if not files:
                return _json({"error": "no scan digests found"})
            source = str(files[-1])
            packet = _read_digest_json(files[-1])
        return _json({"path": source, "packet": packet})
    except Exception as exc:
        return _json({"error": str(exc), "path": path})


@mcp.tool()
def undo_scan_run(
    job_id: str = "",
    slug: str = "",
    min_article_count: int = 50,
    dry_run: bool = True,
    remove_digests: bool = True,
) -> str:
    """Remove dirty scan run records from the MCP activity ledger.

    This only edits MCP activity/digest records. It does not roll back topic
    evidence, pending proposals, posteriors, or lastScanned stamps.
    """
    try:
        threshold = max(1, int(min_article_count or 50))
        root = _digest_root()
        candidates: dict[str, dict] = {}
        digest_files = sorted(root.glob("digest-*.json")) if root.exists() else []
        for path in digest_files:
            try:
                packet = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not _scan_packet_matches(packet, job_id, slug, threshold):
                continue
            jid = packet.get("job_id") or path.stem
            candidates[jid] = {
                "job_id": jid,
                "path": str(path),
                "markdown_path": str(path.with_suffix(".md")),
                "article_count": _scan_packet_article_count(packet),
                "slugs": sorted(_scan_packet_slugs(packet)),
            }

        store = _activity_store()
        if store.log_path.exists():
            for line in store.log_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("task") != "run_news_scan" or event.get("status") != "completed":
                    continue
                response = event.get("response")
                if not response:
                    continue
                try:
                    packet = json.loads(response)
                except Exception:
                    continue
                if not _scan_packet_matches(packet, job_id, slug, threshold):
                    continue
                jid = event.get("job_id") or packet.get("job_id")
                if not jid:
                    continue
                candidates.setdefault(jid, {
                    "job_id": jid,
                    "path": packet.get("digest_path", ""),
                    "markdown_path": packet.get("digest_path", ""),
                    "article_count": _scan_packet_article_count(packet),
                    "slugs": sorted(_scan_packet_slugs(packet)),
                })

        if not candidates:
            return _json({
                "dry_run": dry_run,
                "matched": 0,
                "criteria": {
                    "job_id": job_id,
                    "slug": slug,
                    "min_article_count": threshold,
                },
                "note": "No matching scan activity/digest records found.",
            })

        if dry_run:
            return _json({
                "dry_run": True,
                "matched": len(candidates),
                "candidates": list(candidates.values()),
                "note": (
                    "Dry run only. Re-run with dry_run=false to remove MCP "
                    "activity/digest records. Topic evidence/proposals are not changed."
                ),
            })

        job_ids = set(candidates.keys())
        rewrite = _rewrite_activity_without_jobs(job_ids)
        removed_files = []
        if remove_digests:
            for candidate in candidates.values():
                for key in ("path", "markdown_path"):
                    value = candidate.get(key) or ""
                    if not value:
                        continue
                    path = Path(value)
                    try:
                        root_resolved = root.resolve()
                        resolved = path.resolve()
                        if root_resolved not in resolved.parents:
                            continue
                        if resolved.exists():
                            resolved.unlink()
                            removed_files.append(str(resolved))
                    except Exception:
                        continue

        return _json({
            "dry_run": False,
            "matched": len(candidates),
            "removed_job_ids": sorted(job_ids),
            "removed_digest_files": removed_files,
            **rewrite,
            "note": (
                "Removed MCP scan ledger records only. Topic evidence, pending "
                "proposals, posteriors, and lastScanned were not changed."
            ),
        })
    except Exception as exc:
        return _json({"error": str(exc), "dry_run": dry_run})


@mcp.tool()
def replay_scan_run(
    path: str = "",
    slug: str = "",
    mode: str = "dry_run",
) -> str:
    """Replay a stored scan digest under current code.

    mode:
      - dry_run: no mutation; report what would be safe-applied vs proposed.
      - proposal_only: file FIRE/OBSERVE proposals only; no evidence-log writes.
      - safe_apply: apply only PARK/SCHEMA_GAP/IGNORE, file FIRE/OBSERVE.

    This is intentionally not a posterior-moving replay mode.
    """
    mode = (mode or "dry_run").strip().lower()
    if mode not in {"dry_run", "proposal_only", "safe_apply"}:
        return _json({"error": "mode must be dry_run, proposal_only, or safe_apply"})
    try:
        root = _digest_root()
        if path:
            packet = _read_digest_json(Path(path))
            source = str(Path(path))
        else:
            files = sorted(root.glob("digest-*.json"))
            if not files:
                return _json({"error": "no scan digests found"})
            source = str(files[-1])
            packet = _read_digest_json(files[-1])

        engine = _import_from_repo("engine")
        news = _import_from_repo("framework.news_observation_pipeline")
        pstore = _proposal_store()
        topics_out = []
        for tp in packet.get("topics", []) or []:
            tp_slug = tp.get("slug", "")
            if slug and tp_slug != slug:
                continue
            topic = engine.load_topic(tp_slug)
            articles = tp.get("articles") or []
            decisions = tp.get("decisions") or []
            debate_packet = tp.get("deliberation") or {}
            safe_decisions, review_decisions, safe_policy_audit = _split_safe_policy_decisions(
                news, articles, decisions
            )
            applied = None
            if mode == "safe_apply" and safe_decisions:
                applied = news.apply_decisions(tp_slug, articles, safe_decisions)

            proposals_filed = []
            proposal_errors = []
            if mode in {"proposal_only", "safe_apply"}:
                for d in review_decisions:
                    idx = d.get("idx") or 0
                    idx_int = _to_int_idx(idx)
                    if not (0 < idx_int <= len(articles)):
                        proposal_errors.append({"idx": idx, "error": "article index out of range"})
                        continue
                    action = d.get("action") or {}
                    try:
                        _validate_proposal_shape(
                            topic, action.get("kind", ""),
                            action.get("indicator_id", ""), action.get("value"),
                        )
                        art_rec = pstore.submit_article(
                            _article_for_proposal(articles[idx_int - 1]),
                            submitted_by="scan-replay",
                        )
                        stamp = _deliberation_stamp_from_debate(d, debate_packet)
                        delib_payload = (
                            {"deliberation": stamp} if stamp else {
                                "deliberationWaiver": (
                                    f"scan replay from {source}; original digest carried "
                                    "no gate-passing deliberation record"
                                )
                            }
                        )
                        prop = pstore.add_proposal(
                            article_id=art_rec["id"],
                            slug=tp_slug,
                            action=action.get("kind", ""),
                            indicator_id=action.get("indicator_id", ""),
                            observed_value=action.get("value"),
                            rationale=(
                                d.get("reason") or d.get("claim") or "scan replay decision"
                            )[:500],
                            deliberation=json.dumps(delib_payload),
                        )
                        proposals_filed.append(prop["id"])
                    except Exception as exc:
                        proposal_errors.append({"idx": idx, "error": str(exc)})

            topics_out.append({
                "slug": tp_slug,
                "article_count": len(articles),
                "decision_count": len(decisions),
                "safe_to_apply_count": len(safe_decisions),
                "posterior_moving_count": len(review_decisions),
                "safe_policy_audit": safe_policy_audit,
                "mode": mode,
                "applied": applied,
                "proposals_filed": proposals_filed,
                "proposal_errors": proposal_errors,
            })

        return _json({
            "path": source,
            "mode": mode,
            "topics": topics_out,
            "note": (
                "dry_run mutates nothing; proposal_only files proposals only; "
                "safe_apply applies only PARK/SCHEMA_GAP/IGNORE and files movers"
            ),
        })
    except Exception as exc:
        return _json({"error": str(exc), "path": path, "mode": mode})


def _red_team_design(
    slug: str,
    question: str,
    resolution: str,
    priors_rationale: str,
    hypotheses: dict,
    indicators: dict,
    *,
    model: str = "",
    timeout_sec: int = 600,
) -> dict:
    """Mandatory adversarial pass over a topic design.

    Returns a design_review record: verdict SOUND / REVISE / UNREVIEWED.
    A critique that renders no parseable verdict counts as REVISE (fail
    toward review); an unreachable model leaves UNREVIEWED, which
    activate_topic refuses outright.
    """
    review: dict[str, Any] = {
        "verdict": "UNREVIEWED",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": "",
        "critique": "",
    }
    try:
        prompt_lines = [
            "You are the RED TEAM reviewing a freshly designed NROL-AO topic. "
            "Attack the design: anchored priors, unfalsifiable hypotheses, "
            "compound or correlated indicators, missing anti-indicators, "
            "resolution ambiguity. Be specific; cite the field you attack. "
            "End with VERDICT: SOUND or VERDICT: REVISE.",
            f"TOPIC: {slug}",
            f"QUESTION: {question}",
            f"RESOLUTION: {resolution}",
            f"PRIORS RATIONALE: {priors_rationale}",
            "HYPOTHESES: " + json.dumps(hypotheses, default=str)[:2000],
            "INDICATORS: " + json.dumps(indicators, default=str)[:4000],
        ]
        response = llama_client.chat(
            "\n".join(prompt_lines),
            system_prompt="You are an adversarial topic-design reviewer.",
            model=model, temperature=0.3, max_tokens=2048,
            timeout_sec=timeout_sec, disable_thinking=True,
        )
        text = response.get("text", "")
        review["model"] = response.get("model") or ""
        review["critique"] = text
        verdicts = re.findall(r"VERDICT:\s*(SOUND|REVISE)", text.upper())
        if verdicts:
            review["verdict"] = verdicts[-1]
        elif text.strip():
            review["verdict"] = "REVISE"
    except Exception as exc:
        review["error"] = str(exc)
    return review


def _stamp_design_review(engine, slug: str, review: dict) -> None:
    """Write the verdict summary onto the draft's meta (full critique lives
    in the activity ledger and the returned packet)."""
    topic = engine.load_topic(slug)
    summary = {k: v for k, v in review.items() if k != "critique"}
    summary["critique_chars"] = len(review.get("critique") or "")
    topic["meta"]["design_review"] = summary
    engine.save_topic(topic)


@mcp.tool()
def red_team_topic(slug: str, model: str = "", timeout_sec: int = 600) -> str:
    """(Re)run the mandatory red-team design review on a DRAFT topic.

    Use after revising a draft, or when design_topic left the verdict
    UNREVIEWED because the model was unreachable. Re-reviewing ACTIVE
    topics is the indicator-cleanup session's job, not this tool's.
    """
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        if (topic.get("meta", {}).get("status") or "").upper() != "DRAFT":
            return _json({"error": f"topic {slug!r} is not a DRAFT"})
        history = topic.get("model", {}).get("posteriorHistory", []) or []
        rationale = (history[0].get("note") if history else "") or "(none recorded)"
        review = _red_team_design(
            slug,
            topic.get("meta", {}).get("question", ""),
            topic.get("meta", {}).get("resolution", ""),
            rationale,
            topic.get("model", {}).get("hypotheses", {}),
            topic.get("indicators", {}),
            model=model, timeout_sec=timeout_sec,
        )
        _stamp_design_review(engine, slug, review)
        _activity_store().record(
            new_job_id("red-team-topic"), "completed", task="red_team_topic",
            slug=slug, model=review.get("model"),
            summary={"verdict": review["verdict"]},
            response=review.get("critique") or "",
        )
        return _json({"slug": slug, "design_review": review})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def design_topic(
    slug: str,
    title: str,
    question: str,
    resolution: str,
    hypotheses: dict,
    indicators: dict,
    priors_rationale: str,
    classification: str = "CALIBRATION",
    resolution_date: str = "",
    dynamics: dict | None = None,
    model: str = "",
    timeout_sec: int = 600,
) -> str:
    """Create a new topic as a DRAFT through the engine's design gates.

    The full operator lifecycle starts here: design_topic -> review the
    lint + red-team output -> activate_topic (human-gated). Drafts move no
    beliefs and are excluded from active-topic status until activated.

    hypotheses: {"H1": {"label": ..., "prior": 0.5, "midpoint": 100}, ...}
      — labels must be concrete and falsifiable (governor admissibility is
      a HARD gate: inadmissible hypotheses block creation with details).
      priors_rationale is required: non-uniform priors without a written
      justification are the documented design failure mode.
    indicators: {"tier1_critical": [...], "tier2_strong": [...],
      "tier3_suggestive": [...], "anti_indicators": [...]} with
      pre-committed likelihoods per indicator.
    dynamics: optional shadow-model spec (priors with rationales); REQUIRED
      before activate_topic will accept the topic, so time-as-evidence
      works from day one.

    The red-team critique is NOT optional. Design is the highest-leverage
    judgment point in the system — runtime evidence gets a full debate, so
    design gets at least one adversarial pass. The verdict (SOUND/REVISE)
    is stamped on the draft; activate_topic refuses UNREVIEWED drafts and
    requires an explicit override to activate over a REVISE. If llama is
    down at design time, the draft saves as UNREVIEWED — run
    red_team_topic(slug) when it's back.
    """
    store = _activity_store()
    job_id = new_job_id("design-topic")
    try:
        engine = _import_from_repo("engine")
        try:
            engine.load_topic(slug)
            return _json({"error": f"topic {slug!r} already exists — design_topic never overwrites"})
        except FileNotFoundError:
            pass

        meta = {
            "slug": slug,
            "title": title,
            "question": question,
            "resolution": resolution,
            "classification": classification,
            "status": "DRAFT",
            "lens": "OPERATOR_JUDGMENT",
            "calibrationStatus": "SKIPPED_OPERATOR_JUDGMENT",
            "calibrationSkipReason": "Operator-designed topic; calibration via design gates.",
        }
        if resolution_date:
            meta["resolutionDate"] = resolution_date
        config = {
            "slug": slug, "title": title, "question": question,
            "resolution": resolution, "classification": classification,
            "status": "DRAFT",
            "meta": meta,
            "hypotheses": hypotheses,
            "indicators": indicators,
        }
        store.record(job_id, "running", task="design_topic", slug=slug,
                     summary={"phase": "create", "hypotheses": list(hypotheses)})
        topic = engine.create_topic(config)

        # Record the prior justification as the first posteriorHistory entry —
        # the design-gate requirement, satisfied structurally.
        topic = engine.load_topic(slug)
        topic.setdefault("model", {}).setdefault("posteriorHistory", []).insert(0, {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "posteriors": {k: v.get("posterior") for k, v in topic["model"]["hypotheses"].items()},
            "note": f"Initial priors: {priors_rationale}"[:500],
        })
        engine.save_topic(topic)

        lint_report = None
        try:
            lint_mod = _import_from_repo("framework.lint_indicators")
            flat = []
            for tier_list in (topic.get("indicators", {}).get("tiers", {}) or {}).values():
                flat.extend(i for i in (tier_list or []) if isinstance(i, dict))
            for i in (topic.get("indicators", {}).get("anti_indicators", []) or []):
                if isinstance(i, dict):
                    ai = dict(i)
                    ai["_tier"] = "anti_indicators"  # so the inversion lint can identify it
                    flat.append(ai)
            lint_report = lint_mod.propose_indicators_lint(topic, flat)
        except Exception as exc:
            lint_report = {"error": f"lint unavailable: {exc}"}

        dynamics_path = None
        if dynamics:
            dyn_mod = _import_from_repo("framework.dynamics_shadow")
            dynamics_path = dyn_mod.write_spec(_ensure_repo(), slug, dynamics)

        review = _red_team_design(
            slug, question, resolution, priors_rationale, hypotheses, indicators,
            model=model, timeout_sec=timeout_sec,
        )
        _stamp_design_review(engine, slug, review)

        next_step = {
            "SOUND": f"Review the above, then activate_topic(slug={slug!r}) to go live.",
            "REVISE": "Red team says REVISE — address the critique (then "
                      f"red_team_topic(slug={slug!r})), or activate with "
                      "accept_red_team_revise=true as a logged override.",
            "UNREVIEWED": f"Model unreachable for red team — run red_team_topic(slug={slug!r}) "
                          "before activation (UNREVIEWED drafts cannot activate).",
        }[review["verdict"]]
        packet = {
            "slug": slug,
            "status": "DRAFT",
            "hypothesis_admissibility": "passed (creation would have raised otherwise)",
            "indicator_lint": lint_report,
            "dynamics_spec": dynamics_path or "MISSING — required before activate_topic",
            "red_team": review,
            "next": next_step,
        }
        store.record(job_id, "completed", task="design_topic", slug=slug,
                     summary={"status": "DRAFT", "dynamics": bool(dynamics_path)},
                     response=_json(packet))
        return _json(packet)
    except Exception as exc:
        store.record(job_id, "error", task="design_topic", slug=slug,
                     summary={"error": str(exc)})
        return _json({"error": str(exc)})


@mcp.tool()
def activate_topic(slug: str, accept_red_team_revise: bool = False) -> str:
    """Activate a DRAFT topic — the human-gated commit of the design loop.

    Hard requirements: topic exists in DRAFT status, hypotheses re-pass
    admissibility, a lint-clean dynamics spec exists (no topic goes live
    without pricing time-as-evidence), and the mandatory red-team review
    has run. UNREVIEWED drafts never activate; a REVISE verdict requires
    accept_red_team_revise=true — an explicit, logged human override.
    Raises a Loom browser approval (fail-closed) before flipping ACTIVE.
    """
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        status = (topic.get("meta", {}).get("status") or "").upper()
        if status != "DRAFT":
            return _json({"error": f"topic {slug!r} is {status or 'unknown'}, not DRAFT"})

        dyn_mod = _import_from_repo("framework.dynamics_shadow")
        try:
            dyn_mod.load_spec(Path(_ensure_repo()), slug)
        except Exception as exc:
            return _json({"error": f"dynamics spec required before activation: {exc}"})

        admissibility = engine.validate_hypotheses(topic)
        bad = {k: v for k, v in admissibility.items() if v.get("grade") == "INADMISSIBLE"}
        if bad:
            return _json({"error": f"inadmissible hypotheses block activation: {sorted(bad)}"})

        verdict = str(
            (topic.get("meta", {}).get("design_review") or {}).get("verdict") or "UNREVIEWED"
        ).upper()
        if verdict == "UNREVIEWED":
            return _json({
                "error": "design is UNREVIEWED — the red-team pass is mandatory; "
                         f"run red_team_topic(slug={slug!r}) first",
            })
        if verdict == "REVISE" and not accept_red_team_revise:
            return _json({
                "error": "red team verdict is REVISE — revise the draft (then "
                         "red_team_topic again), or activate with "
                         "accept_red_team_revise=true as a logged override",
            })

        denied = _ask_loom_permission(
            "nrol_ao_activate_topic",
            {
                "slug": slug,
                "title": topic.get("meta", {}).get("title"),
                "red_team_verdict": verdict,
                "revise_overridden": bool(verdict == "REVISE" and accept_red_team_revise),
            },
        )
        if denied:
            return _json({"slug": slug, "activated": False, "denied": denied})

        topic["meta"]["status"] = "ACTIVE"
        engine.save_topic(topic)
        _activity_store().record(
            new_job_id("activate-topic"), "completed", task="activate_topic",
            slug=slug, summary={
                "activated": True,
                "red_team_verdict": verdict,
                "revise_overridden": bool(verdict == "REVISE" and accept_red_team_revise),
            },
        )
        return _json({
            "slug": slug,
            "activated": True,
            "scan_status": _topic_scan_status(engine.load_topic(slug)),
        })
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def shadow_posteriors(slug: str, asof: str = "") -> str:
    """Derive SHADOW posteriors from the topic's pre-committed dynamics spec.

    Zero authority — never writes topic state. Posteriors here are
    first-passage probabilities of a regime-switching process whose
    transition-rate priors are pre-committed (with rationales, lint-gated)
    in loom/topics/dynamics/<slug>.dynamics.json. Elapsed time in the
    current regime updates the exit-rate posterior exactly (Gamma
    conjugacy), so "still closed, N days later" is priced instead of
    ignored. Compare against topic_status posteriors; divergence is the
    calibration conversation, not an error. asof=YYYY-MM-DD for
    counterfactual runs.
    """
    try:
        root = _ensure_repo()
        dyn = _import_from_repo("framework.dynamics_shadow")
        return _json(dyn.run(root, slug, asof=asof))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def future_cast(
    slug: str,
    scenario: str,
    target: str = "",
    proposed_transition: str = "",
    observed_value: str = "",
    asof: str = "",
    assumptions: list[str] | None = None,
    save: bool = False,
    model: str = "",
    timeout_sec: int = 600,
) -> str:
    """Dry-run exploration of a hypothetical event or action (shadow analysis).

    Asks what would happen if a proposed event, indicator firing, or
    hypothesis-resolution scenario occurred — WITHOUT mutating topic JSON.
    Deep-clones the topic in memory and applies the hypothetical through the
    engine's own bayesian_update code path (no save), so the shadow posterior
    delta is exact, not a reproduction. Output posteriors are named
    shadow_posteriors, never posteriors. Synthetic evidence is labeled
    HYPOTHETICAL. A red-team critique flags missing evidence and confidence
    risks. Pass asof=YYYY-MM-DD to also report the dynamics shadow posterior
    at that counterfactual date. save=true writes the cast to
    future_casts/future_casts.jsonl (outside topic state); a saved cast is
    never evidence and never satisfies evidence requirements.

    Zero authority: no writes to topic JSON, posteriorHistory, evidenceLog,
    sourceCalibration, or sources/source_db.json.
    """
    try:
        from . import future_cast as fc
        root = _ensure_repo()
        _import_from_repo("engine")  # ensure engine importable from configured repo
        ov: float | None = None
        if observed_value:
            try:
                ov = float(observed_value)
            except (TypeError, ValueError):
                return _json({"error": f"observed_value must be numeric, got {observed_value!r}"})
        packet = fc.run_future_cast(
            repo_root=root,
            slug=slug,
            scenario=scenario,
            target=target,
            transition=proposed_transition,
            observed_value=ov,
            asof=asof,
            assumptions=assumptions or [],
            save=save,
            llama_client=llama_client,
            model=model,
            timeout_sec=timeout_sec,
        )
        return _json(packet)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def list_future_casts(slug: str = "", tag: str = "", limit: int = 25) -> str:
    """List saved future casts (brief view), newest first. Optional slug/tag
    filters. Saved casts live in future_casts/future_casts.jsonl, outside topic
    state. Read-only."""
    try:
        from . import future_cast as fc
        root = _ensure_repo()
        return _json(fc.list_future_casts(root, slug=slug, tag=tag, limit=limit))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def get_future_cast(cast_id: str) -> str:
    """Read one saved future cast by id (full packet). Read-only."""
    try:
        from . import future_cast as fc
        root = _ensure_repo()
        return _json(fc.get_future_cast(root, cast_id=cast_id))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def save_future_cast(cast_id: str, tags: list[str] | None = None, note: str = "") -> str:
    """Re-tag an already-saved future cast, or attach a note. To save a cast's
    full packet, call future_cast(..., save=true) at cast time — a transient
    cast's packet is not persisted. Saved casts are never evidence."""
    try:
        from . import future_cast as fc
        root = _ensure_repo()
        return _json(fc.save_future_cast(root, cast_id=cast_id, tags=tags or [], note=note))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def withdraw_future_cast(cast_id: str, reason: str = "") -> str:
    """Remove a saved future cast from the store by id. Edits only the
    future_casts.jsonl store — never rolls back topic evidence/proposals/
    posteriors (a cast never moved any). A cast promoted_to_real_action is
    refused until the real proposal is withdrawn first."""
    try:
        from . import future_cast as fc
        root = _ensure_repo()
        return _json(fc.withdraw_future_cast(root, cast_id=cast_id, reason=reason))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def resolve_topic(
    slug: str,
    resolved_hypothesis: str,
    note: str = "",
    skip_aar: bool = False,
    model: str = "",
    timeout_sec: int = 600,
) -> str:
    """Resolve a topic: set RESOLVED, record the outcome, compute two-lane
    Brier (shadow vs committed, both vs the resolved truth), and optionally
    generate a red-team after-action review over the evidence stream and scan
    digests.

    This is the single sanctioned resolution entry point — no other path sets
    meta.status=RESOLVED. It calls scoring.record_outcome (the engine's
    existing committed-lane scoring, unmodified) and reconstructs the shadow
    trajectory by calling shadow_posteriors(slug, asof=d) for each
    posteriorHistory date d (deterministic). The AAR re-examines the evidence
    log and recent scan digests and asks the local model where perception vs
    authority diverged.

    commit=true semantics: resolution raises a browser approval request
    (fail-closed); on denial the topic is NOT resolved. The shadow/Brier/AAR
    analytics are read-only and never move the committed posterior.
    """
    try:
        from . import resolution as res
        root = _ensure_repo()
        _import_from_repo("engine")
        # Gather recent scan digests to feed the AAR (read-only).
        digests: list[dict] = []
        try:
            droot = _digest_root()
            for path in sorted(droot.glob("digest-*.json"), reverse=True)[:10]:
                try:
                    digests.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    pass
        except Exception:
            pass

        result = res.run_resolution(
            repo_root=root, slug=slug, resolved_hypothesis=resolved_hypothesis,
            note=note, skip_aar=skip_aar, llama_client=llama_client,
            model=model, timeout_sec=timeout_sec, scan_digests=digests,
        )
        packet = result["packet"]
        topic = result["topic"]

        # Fail-closed: resolution is a real commit (status flip + scoring).
        denied = _ask_loom_permission(
            "nrol_ao_resolve_topic",
            {"slug": slug, "resolved_hypothesis": resolved_hypothesis,
             "note": note,
             "two_lane_brier": packet.get("two_lane_brier"),
             "red_team_verdict": (packet.get("red_team_aar") or {}).get("verdict")},
        )
        if denied:
            return _json({"slug": slug, "resolved": False, "denied": denied})

        # Persist the RESOLVED status + committed-lane scoring block.
        engine = _import_from_repo("engine")
        engine.save_topic(topic)
        return _json(packet)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def resolution_brier(slug: str, asof: str = "") -> str:
    """Recompute two-lane Brier (shadow vs committed) for a RESOLVED topic.

    Read-only — never mutates topic state. Useful for post-hoc calibration
    review after resolution. Pass asof=YYYY-MM-DD to score only the trajectory
    up to that date.
    """
    try:
        from . import resolution as res
        root = _ensure_repo()
        _import_from_repo("engine")
        return _json(res.run_resolution_brier(root, slug, asof=asof))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def source_calibration_status(slug: str = "") -> str:
    """Source-trust status: topic-local sourceCalibration summary (slug given)
    or the cross-topic source database summary (slug empty). Read-only.

    Source trust is a Bayesian trust ledger (confirmed/refuted claims), NOT a
    Brier score — the two are kept separate by design. This exposes the LIVE
    framework/source_db.py + source_ledger.py machinery; it does not move
    posteriors or write to source_db.json.
    """
    try:
        from . import source_trust as st
        _ensure_repo()
        _import_from_repo("framework.source_db")
        return _json(st.source_calibration_status(slug=slug))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def source_profile(source: str, domain: str = "") -> str:
    """Full trust profile for one source from the cross-topic DB. Pass a domain
    tag to resolve domain-specific trust via the 5-tier fallback chain (domain
    -> effective -> base -> static prior -> 0.50). Read-only."""
    try:
        from . import source_trust as st
        _ensure_repo()
        _import_from_repo("framework.source_db")
        return _json(st.source_profile(source=source, domain=domain))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def validate_source_db() -> str:
    """Schema sanity check of sources/source_db.json. Reports structural
    problems (non-numeric trust, negative counts) without raising. Read-only."""
    try:
        from . import source_trust as st
        _ensure_repo()
        _import_from_repo("framework.source_db")
        return _json(st.validate_source_db())
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def source_domain_patterns(min_claims: int = 3) -> str:
    """Cross-source domain reliability patterns (most/least reliable domains,
    per-source variance). Wraps framework.source_db.find_domain_patterns. Read-only."""
    try:
        from . import source_trust as st
        _ensure_repo()
        _import_from_repo("framework.source_db")
        return _json(st.domain_patterns(min_claims=min_claims))
    except Exception as exc:
        return _json({"error": str(exc)})


def _file_review_parked_proposal(
    pstore, slug: str, article: dict, decision: dict, ev_id: str,
    debate_packet: dict, review: dict, proposals_filed: list,
) -> None:
    """File a FIRE/OBSERVE proposal from review_parked (shared by the
    mechanical-only and cross-day-duplicate-checked paths). Mutates review
    (stamps escalated_proposal_id) and appends to proposals_filed."""
    action = decision.get("action", {}) or {}
    art_rec = pstore.submit_article(article, submitted_by="review-parked")
    review_delib = _deliberation_stamp_from_debate(decision, debate_packet)
    prop = pstore.add_proposal(
        article_id=art_rec["id"],
        slug=slug,
        action=action.get("kind", ""),
        indicator_id=action.get("indicator_id", ""),
        observed_value=action.get("value"),
        rationale=(
            f"re-adjudication of parked {ev_id}: "
            + (decision.get("reason") or decision.get("claim") or "matcher re-decision")
        )[:500],
        evidence_id=ev_id,
        deliberation=json.dumps(
            {"deliberation": review_delib} if review_delib
            else {"deliberationWaiver": "review_parked ran with deliberate=false"}
        ),
    )
    review["escalated_proposal_id"] = prop["id"]
    proposals_filed.append(prop["id"])


@mcp.tool()
def review_parked(
    slug: str,
    limit: int = 12,
    refetch: bool = True,
    excerpt_chars: int = 2800,
    dry_run: bool = True,
    review_interval_days: float = 14.0,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 900,
    deliberate: bool = True,
    check_cross_day_duplicates: bool = False,
) -> str:
    """Re-adjudicate parked evidence against the CURRENT indicator schema.

    deliberate=true (default) runs the advocate/rebut/jury debate over
    re-decisions that remain PARK — these items already died once on a
    one-shot judgment; the debate is what makes re-review more than a
    second coin flip from the same model.

    check_cross_day_duplicates=true (opt-in) runs the semantic cross-day
    duplicate judge on each FIRE/OBSERVE candidate that survives the
    mechanical suppression check, before filing. Catches the case the
    mechanical check misses: a new article (different URL) reporting an
    event already committed via different evidence refs. DUPLICATE_OF and
    UNCERTAIN_DUPLICATE suppress the proposal (parked as a duplicate note
    instead). Adds one llama call per surviving candidate — bounded but
    not free. Default false so existing behavior/tests are unaffected.

    Kept-but-timestamped: items never leave the flagged queue here — every
    reviewed item gets a review record (engine.record_parked_reviews) so the
    review-debt metric in topic_status stays honest. Clearing the queue
    remains the indicator-cleanup session's job.

    Selection order (the reverse staleness detector): schema-changed first
    (their PARK was conditioned on a schema that no longer exists), then
    never-reviewed, then oldest past the review interval. Recently reviewed
    items are NOT due — the interval doubles as a refractory period that
    caps multiple-comparisons noise from re-rolling the matcher.

    OBSERVE/FIRE re-decisions are filed as pending proposals for human
    commit via commit_match; posteriors never move here. refetch=true pulls
    full article text for each item's URL so the matcher re-judges on the
    body, not the snippet it originally parked on.
    """
    store = _activity_store()
    job_id = new_job_id("review-parked")
    start = time.time()
    try:
        engine = _import_from_repo("engine")
        news = _import_from_repo("framework.news_observation_pipeline")
        if not hasattr(engine, "parked_review_status"):
            return _json({"error": "engine lacks parked_review_status — update the NROL-AO repo"})
        topic = engine.load_topic(slug)
        debt_before = engine.parked_review_status(topic, review_interval_days)

        priority = {"schema_changed": 0, "never_reviewed": 1, "interval_elapsed": 2}
        due = sorted(
            debt_before["due"],
            key=lambda d: (priority.get(d["reason"], 9), -(d.get("age_days") or 0)),
        )[: max(1, min(int(limit), 30))]
        if not due:
            return _json({
                "slug": slug,
                "considered": 0,
                "reviews": [],
                "debt": debt_before,
                "note": "no parked items due for review",
            })

        evidence_by_id = {
            e.get("id"): e for e in topic.get("evidenceLog", []) or []
            if isinstance(e, dict) and e.get("id")
        }
        selected = []
        articles = []
        missing_evidence_ids = []
        for d in due:
            ev = evidence_by_id.get(d["evidence_id"])
            if not ev:
                missing_evidence_ids.append(d["evidence_id"])
                continue
            art = {
                "headline": (ev.get("text") or "")[:140] or d["evidence_id"],
                "url": ev.get("url") or "",
                "source": ev.get("source") or "evidence_log",
                "date": str(ev.get("time") or "")[:10],
                "relevance": (ev.get("text") or "")[:500],
            }
            if refetch and art["url"]:
                excerpt = _fetch_article_excerpt(art["url"], excerpt_chars)
                if excerpt:
                    art["excerpt"] = excerpt
            selected.append(d["evidence_id"])
            articles.append(art)

        if not selected:
            return _json({
                "slug": slug,
                "considered": 0,
                "reviews": [],
                "debt": {k: v for k, v in debt_before.items() if k != "due"},
                "missing_evidence_ids": missing_evidence_ids,
                "note": "due parked IDs were missing from evidenceLog; no matcher run",
            })

        store.record(
            job_id,
            "running",
            task="review_parked",
            slug=slug,
            model=model or llama_client.llama_model(),
            summary={
                "phase": "matching",
                "due_total": debt_before["due_count"],
                "reviewing": len(selected),
                "missing_evidence_ids": missing_evidence_ids,
                "refetched": sum(1 for a in articles if a.get("excerpt")),
            },
        )
        prompt = _build_matcher_prompt(news, topic, articles)
        response = llama_client.chat(
            prompt,
            system_prompt=(
                "You are the NROL-AO evidence matcher re-reviewing previously parked "
                "evidence against the CURRENT indicator schema. Return only DECISION "
                "blocks in the requested format. Do not invent indicators or values."
            ),
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            disable_thinking=True,
        )
        matcher_output = response.get("text", "")
        if not matcher_output.strip():
            store.record(
                job_id, "error", task="review_parked", slug=slug,
                summary={"error": "matcher returned no content — nothing recorded"},
            )
            return _json({
                "slug": slug,
                "error": "matcher returned no content "
                         f"(finish_reason={response.get('finish_reason') or '?'}) — "
                         "no reviews recorded, queue untouched",
            })
        decisions = news.parse_matcher_output(matcher_output)

        debate_packet = None
        if deliberate and decisions:
            jury_overrides, debate_packet = _run_debate(
                topic, articles, decisions, news,
                model=model, temperature=temperature,
                max_tokens=max_tokens, timeout_sec=timeout_sec,
                store=store, job_id=job_id, slug=slug,
            )
            if debate_packet and "error" in debate_packet:
                store.record(
                    job_id, "error", task="review_parked", slug=slug,
                    summary={"error": f"debate failed: {debate_packet['error']}"},
                )
                return _json({
                    "slug": slug,
                    "error": f"deliberation/debate failed: {debate_packet['error']} — no reviews recorded, queue untouched",
                })
            decisions = _apply_jury_overrides(decisions, jury_overrides)

        pstore = _proposal_store()
        reviews = []
        proposals_filed = []
        errors = {}
        for d in decisions:
            idx = d.get("idx") or 0
            idx_int = _to_int_idx(idx)
            if not (0 < idx_int <= len(selected)):
                continue
            ev_id = selected[idx_int - 1]
            action = d.get("action", {}) or {}
            kind = action.get("kind", "")
            review = {
                "evidence_id": ev_id,
                "decision": kind or "NO_DECISION",
                "note": (d.get("reason") or d.get("claim") or "")[:300],
            }
            if kind in {"FIRE", "OBSERVE"}:
                try:
                    _validate_proposal_shape(
                        topic, kind, action.get("indicator_id", ""), action.get("value"),
                    )
                    suppression = _proposal_suppression_reason(
                        topic, articles[idx_int - 1], d, ev_id,
                    )
                    if suppression:
                        review["suppressed_proposal"] = suppression
                    elif check_cross_day_duplicates:
                        # Semantic cross-day duplicate check: catches the case
                        # the mechanical check misses (different URL, same
                        # already-counted event). Suppresses on DUPLICATE_OF
                        # or UNCERTAIN_DUPLICATE (bias toward duplicate — the
                        # dangerous direction is duplicate movement).
                        try:
                            dup = _judge_cross_day_duplicate(
                                slug, topic, articles[idx_int - 1], d,
                                model=model, temperature=0.0,
                                max_tokens=2048, timeout_sec=timeout_sec,
                            )
                            verdict = (dup.get("judgment") or {}).get("verdict", "")
                            if verdict in {"DUPLICATE_OF", "UNCERTAIN_DUPLICATE"}:
                                review["suppressed_proposal"] = (
                                    f"cross_day_duplicate: {verdict} of "
                                    f"{(dup.get('judgment') or {}).get('evidence_id', '?')} "
                                    f"— {(dup.get('judgment') or {}).get('reason', '')[:200]}"
                                )
                            else:
                                review["cross_day_duplicate_check"] = {"verdict": verdict}
                                if not dry_run:
                                    _file_review_parked_proposal(
                                        pstore, slug, articles[idx_int - 1], d,
                                        ev_id, debate_packet, review, proposals_filed,
                                    )
                        except Exception as exc:
                            # The duplicate check must never block filing on
                            # its own failure — fall through to file normally.
                            review["cross_day_duplicate_error"] = str(exc)
                            if not dry_run:
                                _file_review_parked_proposal(
                                    pstore, slug, articles[idx_int - 1], d,
                                    ev_id, debate_packet, review, proposals_filed,
                                )
                    elif not dry_run:
                        _file_review_parked_proposal(
                            pstore, slug, articles[idx_int - 1], d,
                            ev_id, debate_packet, review, proposals_filed,
                        )
                except Exception as exc:
                    errors[ev_id] = str(exc)
                    review["proposal_error"] = str(exc)
            reviews.append(review)

        recorded = None
        debt_after = None
        if not dry_run and reviews:
            recorded = engine.record_parked_reviews(slug, reviews, reviewer="review_parked")
            debt_after = engine.parked_review_status(engine.load_topic(slug), review_interval_days)
            # Trim the verbose due list out of the after-summary
            debt_after.pop("due", None)

        packet = {
            "slug": slug,
            "dry_run": dry_run,
            "considered": len(selected),
            "reviews": reviews,
            "deliberation": debate_packet,
            "proposals_filed": proposals_filed,
            "proposal_errors": errors,
            "missing_evidence_ids": missing_evidence_ids,
            "recorded": recorded,
            "debt_before": {k: v for k, v in debt_before.items() if k != "due"},
            "debt_after": debt_after,
            "matcher_model": response.get("model"),
        }
        store.record(
            job_id,
            "completed",
            task="review_parked",
            slug=slug,
            model=response.get("model"),
            duration_ms=int((time.time() - start) * 1000),
            summary={
                "reviewed": len(reviews),
                "proposals_filed": len(proposals_filed),
                "dry_run": dry_run,
            },
            response=_json(packet),
        )
        return _json(packet)
    except Exception as exc:
        store.record(
            job_id, "error", task="review_parked", slug=slug,
            summary={"error": str(exc)},
        )
        return _json({"error": str(exc)})


@mcp.tool()
def triage_headline(headline: str, source: str = "", save: bool = False, note: str = "") -> str:
    """Triage a headline against active topics without mutating state.

    save=true appends the triage result to loom/triage_log/triage_log.jsonl
    (an audit ledger outside topic state). A logged triage is NOT evidence —
    it never moves posteriors; promotion to real action still goes through
    submit_transition / propose_match -> commit_match. Use list_triage_log /
    read_triage_log to review prior triages."""
    try:
        engine = _import_from_repo("engine")
        result = engine.triage_headline(headline, source=source or None)
        if save:
            from . import triage_log as tl
            root = _ensure_repo()
            record = tl.save_triage(root, result, note=note)
            result["saved_triage_id"] = record["triage_id"]
            result["saved_to"] = "loom/triage_log/triage_log.jsonl"
        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc), "headline": headline})


@mcp.tool()
def list_triage_log(slug: str = "", limit: int = 25) -> str:
    """List recent triage ledger entries (audit only). Optional slug filters
    by topic. Read-only — never mutates topic state."""
    try:
        from . import triage_log as tl
        root = _ensure_repo()
        return _json(tl.list_triage(root, slug=slug, limit=limit))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def read_triage_log(triage_id: str) -> str:
    """Read one triage ledger entry by id (full matches). Read-only."""
    try:
        from . import triage_log as tl
        root = _ensure_repo()
        return _json(tl.read_triage(root, triage_id=triage_id))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def log_social_forecast(
    handle: str, slug: str, posteriors: dict, note: str = "", forecast_date: str = "",
) -> str:
    """Log a social-media handle's probability forecast for a topic.

    GREENFIELD pathway: a handle (Bluesky/Twitter/etc.) is a forecaster. The
    forecast is logged to loom/social_forecasts/social_forecasts.jsonl (outside
    topic state) and scored with Brier at resolution via social_user_brier.
    This is forecast calibration, NOT source trust — kept separate by design.
    The forecast is NOT evidence; it never moves posteriors. Posteriors are
    renormalized to sum to 1.0 before storing."""
    try:
        from . import social_brier as sb
        root = _ensure_repo()
        _import_from_repo("framework.scoring")
        record = sb.log_social_forecast(
            root, handle, slug, posteriors, note=note, forecast_date=forecast_date)
        return _json(record)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def social_user_brier(handle: str, slug: str = "") -> str:
    """Score a handle's forecasts with Brier against resolved truth.

    If slug is given, score only that topic's forecasts (topic must be
    RESOLVED). If slug is empty, score all of the handle's forecasts whose
    topic has resolved. Unresolved forecasts are reported as pending. Reuses
    framework.scoring.compute_brier_score. Read-only on topic state."""
    try:
        from . import social_brier as sb
        root = _ensure_repo()
        _import_from_repo("engine")
        _import_from_repo("framework.scoring")
        return _json(sb.score_social_user(root, handle, slug=slug))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def list_social_handles() -> str:
    """List all handles with logged forecasts + counts. Read-only."""
    try:
        from . import social_brier as sb
        root = _ensure_repo()
        return _json(sb.list_handles(root))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def build_matcher_prompt(slug: str, articles: list[dict]) -> str:
    """Build the source repo's OBSERVE/FIRE/PARK/SCHEMA_GAP matcher prompt."""
    try:
        engine = _import_from_repo("engine")
        news = _import_from_repo("framework.news_observation_pipeline")
        topic = engine.load_topic(slug)
        return _build_matcher_prompt(news, topic, articles)
    except Exception as exc:
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def parse_matcher_output(output_text: str) -> str:
    """Parse matcher text into typed decisions without mutating state."""
    try:
        news = _import_from_repo("framework.news_observation_pipeline")
        return _json(news.parse_matcher_output(output_text))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def apply_matcher_output(slug: str, articles: list[dict], output_text: str, commit: bool = False,
                         deliberate: bool = True, no_deliberation_reason: str = "") -> str:
    """Parse matcher output and optionally apply it through the NROL-AO pipeline.

    commit=true runs the advocate/rebut/jury debate over the decisions first
    (deliberate=true, default). Skipping the debate on a batch containing
    FIRE/OBSERVE requires an explicit no_deliberation_reason waiver; without
    one the apply is refused.
    """
    try:
        store = _activity_store()
        job_id = new_job_id("matcher-apply")
        news = _import_from_repo("framework.news_observation_pipeline")
        decisions = news.parse_matcher_output(output_text)
        store.record(
            job_id,
            "queued",
            task="apply_matcher_output",
            slug=slug,
            summary={"commit": commit, "article_count": len(articles), "decision_count": len(decisions)},
        )
        if not commit:
            store.record(
                job_id,
                "completed",
                task="apply_matcher_output",
                slug=slug,
                summary={"committed": False, "decisions": decisions},
            )
            return _json(
                {
                    "job_id": job_id,
                    "slug": slug,
                    "committed": False,
                    "decisions": decisions,
                    "note": "Dry run only. Re-run with commit=true to mutate topic JSON.",
                }
            )
        debate_packet = None
        has_movers = any(
            (d.get("action") or {}).get("kind") in {"FIRE", "OBSERVE"}
            for d in decisions
        )
        if deliberate and decisions:
            engine = _import_from_repo("engine")
            topic = engine.load_topic(slug)
            jury_overrides, debate_packet = _run_debate(
                topic, articles, decisions, news,
                model="", temperature=0.2, max_tokens=4096, timeout_sec=600,
                store=store, job_id=job_id, slug=slug,
            )
            if debate_packet.get("error"):
                store.record(
                    job_id, "failed", task="apply_matcher_output", slug=slug,
                    error=f"deliberation failed: {debate_packet['error']}",
                )
                return _json({
                    "job_id": job_id, "committed": False,
                    "error": f"deliberation failed: {debate_packet['error']} — "
                             "nothing applied (fail closed)",
                })
            decisions = _apply_jury_overrides(decisions, jury_overrides)
        elif has_movers and not (no_deliberation_reason or "").strip():
            return _json({
                "job_id": job_id, "committed": False,
                "error": (
                    "apply refused: batch contains FIRE/OBSERVE and "
                    "deliberate=false with no waiver. Pass "
                    "no_deliberation_reason to skip the debate explicitly — "
                    "the waiver is recorded in the activity ledger."
                ),
            })
        denied = _ask_loom_permission(
            "nrol_ao_apply_matcher_output",
            {"slug": slug, "article_count": len(articles), "decision_count": len(decisions),
             **({"deliberation": {"jury_verdicts": debate_packet.get("jury_verdicts", {})}}
                if debate_packet else
                {"deliberationWaiver": (no_deliberation_reason or "").strip()}
                if has_movers else {})},
        )
        if denied:
            store.record(
                job_id,
                "denied",
                task="apply_matcher_output",
                slug=slug,
                summary={"denied": denied},
            )
            return _json({"job_id": job_id, "denied": denied, "committed": False})
        start = time.time()
        store.record(
            job_id,
            "running",
            task="apply_matcher_output",
            slug=slug,
            summary={"commit": True,
                     **({"deliberation_waiver": (no_deliberation_reason or "").strip()}
                        if (has_movers and not debate_packet) else {})},
        )
        result = news.apply_decisions(slug, articles, decisions)
        if debate_packet:
            result["deliberation"] = debate_packet
        store.record(
            job_id,
            "completed",
            task="apply_matcher_output",
            slug=slug,
            duration_ms=int((time.time() - start) * 1000),
            summary=result,
        )
        return _json({"job_id": job_id, "slug": slug, "committed": True, "summary": result})
    except Exception as exc:
        try:
            _activity_store().record(
                locals().get("job_id", new_job_id("matcher-apply")),
                "failed",
                task="apply_matcher_output",
                slug=slug,
                error=str(exc),
            )
        except Exception:
            pass
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def deliberate_candidates(
    slug: str,
    articles: list[dict],
    output_text: str,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 600,
) -> str:
    """Run the advocate/rebut/jury debate over matcher decisions. No mutation.

    output_text is matcher DECISION blocks (from build_matcher_prompt or
    hand-written in the same format). Returns the original decisions, the
    jury-folded effective decisions, and the debate packet. Attach a
    per-candidate record from `deliberation_records` to
    propose_match(deliberation=...) or submit_transition(deliberation=...)
    — the gate refuses posterior-moving actions without one (or an explicit
    no_deliberation_reason waiver).
    """
    store = _activity_store()
    job_id = new_job_id("deliberate")
    try:
        engine = _import_from_repo("engine")
        news = _import_from_repo("framework.news_observation_pipeline")
        topic = engine.load_topic(slug)
        decisions = news.parse_matcher_output(output_text)
        store.record(
            job_id, "running", task="deliberate_candidates", slug=slug,
            summary={"article_count": len(articles), "decision_count": len(decisions)},
        )
        jury_overrides, debate_packet = _run_debate(
            topic, articles, decisions, news,
            model=model, temperature=temperature,
            max_tokens=max_tokens, timeout_sec=timeout_sec,
            store=store, job_id=job_id, slug=slug,
        )
        effective = _apply_jury_overrides(decisions, jury_overrides)
        records = {
            str(d.get("idx")): _deliberation_stamp_from_debate(d, debate_packet)
            for d in effective
        }
        store.record(
            job_id, "completed", task="deliberate_candidates", slug=slug,
            summary={"candidates": debate_packet.get("candidates", 0),
                     "jury_verdicts": debate_packet.get("jury_verdicts", {}),
                     "error": debate_packet.get("error")},
        )
        return _json({
            "job_id": job_id,
            "slug": slug,
            "decisions": decisions,
            "effective_decisions": effective,
            "deliberation_records": records,
            "debate": debate_packet,
            "note": "No mutation. Use a deliberation_records entry as the "
                    "deliberation= argument when proposing or committing.",
        })
    except Exception as exc:
        store.record(job_id, "failed", task="deliberate_candidates", slug=slug, error=str(exc))
        return _json({"error": str(exc), "slug": slug})


@mcp.tool()
def review_duplicate_candidate(
    slug: str,
    article: dict,
    decision: dict,
    window_days: int = 45,
    max_candidates: int = 12,
    model: str = "",
    temperature: float = 0.0,
    max_tokens: int = 2048,
    timeout_sec: int = 600,
) -> str:
    """Ask whether a FIRE/OBSERVE candidate is a duplicate of prior evidence.

    This is a perception-only tool: it does not mutate the topic, withdraw a
    proposal, or move posteriors. Verdicts are typed:
    DUPLICATE_OF <evidence_id>, UNIQUE_EVENT, or UNCERTAIN_DUPLICATE.
    Bias uncertain cases toward duplicate/park in operator briefings because
    duplicate movement is the dangerous direction.
    """
    store = _activity_store()
    job_id = new_job_id("duplicate-review")
    try:
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        out = _judge_cross_day_duplicate(
            slug, topic, article, decision, window_days, max_candidates,
            model, temperature, max_tokens, timeout_sec,
        )
        # Record the job lifecycle (the helper is pure; logging stays in the wrapper).
        if "error" in out:
            store.record(job_id, "failed", task="review_duplicate_candidate",
                         slug=slug, error=out["error"])
            return _json({"job_id": job_id, **out})
        store.record(job_id, "running", task="review_duplicate_candidate", slug=slug,
                     summary={"candidate_count": out.get("candidate_count", 0),
                              "window_days": window_days})
        store.record(job_id, "completed", task="review_duplicate_candidate", slug=slug,
                     model=out.get("model"),
                     summary={"verdict": out.get("judgment", {}).get("verdict"),
                              "evidence_id": out.get("judgment", {}).get("evidence_id", "")},
                     response=out.get("response", ""))
        return _json({"job_id": job_id, **out})
    except Exception as exc:
        try:
            store.record(job_id, "failed", task="review_duplicate_candidate",
                         slug=slug, error=str(exc))
        except Exception:
            pass
        return _json({"job_id": job_id, "error": str(exc), "slug": slug})


def _judge_cross_day_duplicate(
    slug: str, topic: dict, article: dict, decision: dict,
    window_days: int = 45, max_candidates: int = 12,
    model: str = "", temperature: float = 0.0,
    max_tokens: int = 4096, timeout_sec: int = 600,
) -> dict:
    """Core duplicate-event judgment (no job logging). Returns the judgment
    dict + candidates, or {error}. Pure-ish: calls the local llama endpoint.

    Used by both the review_duplicate_candidate MCP tool AND review_parked's
    optional cross-day duplicate pre-check. Verdicts:
    DUPLICATE_OF <evidence_id> | UNIQUE_EVENT | UNCERTAIN_DUPLICATE.
    Bias uncertain toward duplicate (duplicate movement is the dangerous
    direction)."""
    candidates = _candidate_duplicate_evidence(
        topic, article, decision, window_days, max_candidates
    )
    if not candidates:
        return {
            "slug": slug, "candidate_count": 0,
            "judgment": {"verdict": "UNIQUE_EVENT", "evidence_id": "",
                         "reason": "no plausible prior evidence candidates found"},
            "candidates": [],
        }
    prompt = {
        "task": (
            "Decide whether the candidate article/decision describes the "
            "same underlying causal event or same measurement as one prior "
            "evidence entry. Be conservative: if uncertain, return "
            "UNCERTAIN_DUPLICATE, not UNIQUE_EVENT."
        ),
        "candidate_article": article,
        "candidate_decision": decision,
        "prior_evidence_candidates": candidates,
        "output_format": (
            "VERDICT: DUPLICATE_OF <evidence_id> | UNIQUE_EVENT | "
            "UNCERTAIN_DUPLICATE\nREASON: <one concise reason>"
        ),
    }
    response = llama_client.chat(
        json.dumps(prompt, ensure_ascii=False, indent=2),
        system_prompt=(
            "You are an NROL-AO duplicate-event judge. Return only the "
            "VERDICT and REASON lines in the requested format."
        ),
        model=model, temperature=temperature, max_tokens=max_tokens,
        timeout_sec=timeout_sec, disable_thinking=False,
    )
    judgment = _parse_duplicate_judgment(response.get("text", ""))
    # Defensive: if the model returned empty content (reasoning-budget
    # exhaustion on a thinking-enabled model, or a transport hiccup), do NOT
    # let the parser's default verdict masquerade as a substantive judgment —
    # a wrong duplicate call is the dangerous direction (suppresses real evidence).
    if not (response.get("text") or "").strip():
        judgment = {
            "verdict": "UNCERTAIN_DUPLICATE",
            "reason": (
                "NO_ANSWER_EMITTED: duplicate judge returned empty content "
                f"(finish_reason={response.get('finish_reason')!r}, "
                f"reasoning_chars={response.get('reasoning_chars', 0)}). "
                "This is a non-answer — rerun; do not treat as UNIQUE_EVENT."
            ),
        }
    return {
        "slug": slug, "candidate_count": len(candidates),
        "judgment": judgment, "candidates": candidates,
        "model": response.get("model"), "response": response.get("text", ""),
    }


@mcp.tool()
def run_matcher_with_llama(
    slug: str,
    articles: list[dict],
    commit: bool = False,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 600,
    deliberate: bool = True,
    no_deliberation_reason: str = "",
) -> str:
    """Run the NROL-AO matcher prompt through llama-server and parse decisions.

    commit=false only records and returns llama output plus parsed decisions.
    commit=true runs the advocate/rebut/jury debate over the decisions
    (deliberate=true, default), asks Loom permission, then routes decisions
    through the existing NROL-AO pipeline. Skipping the debate on a batch
    containing FIRE/OBSERVE requires an explicit no_deliberation_reason
    waiver. All job states are written to the activity ledger.
    """
    store = _activity_store()
    job_id = new_job_id("matcher-llama")
    start = time.time()
    try:
        engine = _import_from_repo("engine")
        news = _import_from_repo("framework.news_observation_pipeline")
        topic = engine.load_topic(slug)
        prompt = _build_matcher_prompt(news, topic, articles)
        system_prompt = (
            "You are the NROL-AO evidence matcher. Return only DECISION blocks "
            "in the requested format. Do not invent indicators, likelihoods, or posteriors."
        )
        store.record(
            job_id,
            "running",
            task="run_matcher_with_llama",
            slug=slug,
            model=model or llama_client.llama_model(),
            summary={"article_count": len(articles), "commit": commit},
            prompt=prompt,
        )
        response = llama_client.chat(
            prompt,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            disable_thinking=True,
        )
        output_text = response["text"]
        decisions = news.parse_matcher_output(output_text)
        parsed_summary = {
            "article_count": len(articles),
            "decision_count": len(decisions),
            "commit": commit,
            "llama_host": response.get("host"),
            "model": response.get("model"),
        }
        if articles and not output_text.strip():
            parsed_summary["matcher_error"] = (
                "matcher returned no content "
                f"(finish_reason={response.get('finish_reason') or '?'}, "
                f"reasoning_chars={response.get('reasoning_chars', 0)})"
            )
        store.record(
            job_id,
            "running",
            task="run_matcher_with_llama",
            slug=slug,
            model=response.get("model"),
            summary=parsed_summary,
            response=output_text,
        )

        applied = None
        debate_packet = None
        if commit:
            has_movers = any(
                (d.get("action") or {}).get("kind") in {"FIRE", "OBSERVE"}
                for d in decisions
            )
            if deliberate and decisions:
                jury_overrides, debate_packet = _run_debate(
                    topic, articles, decisions, news,
                    model=model, temperature=temperature,
                    max_tokens=max_tokens, timeout_sec=timeout_sec,
                    store=store, job_id=job_id, slug=slug,
                )
                if debate_packet.get("error"):
                    store.record(
                        job_id, "failed", task="run_matcher_with_llama",
                        slug=slug,
                        error=f"deliberation failed: {debate_packet['error']}",
                    )
                    return _json({
                        "job_id": job_id, "committed": False,
                        "decisions": decisions,
                        "error": f"deliberation failed: {debate_packet['error']} — "
                                 "nothing applied (fail closed)",
                    })
                decisions = _apply_jury_overrides(decisions, jury_overrides)
                parsed_summary["deliberation"] = {
                    "candidates": debate_packet.get("candidates", 0),
                    "jury_verdicts": debate_packet.get("jury_verdicts", {}),
                }
            elif has_movers and not (no_deliberation_reason or "").strip():
                return _json({
                    "job_id": job_id, "committed": False,
                    "decisions": decisions,
                    "error": (
                        "apply refused: batch contains FIRE/OBSERVE and "
                        "deliberate=false with no waiver. Pass "
                        "no_deliberation_reason to skip the debate explicitly."
                    ),
                })
            elif has_movers:
                parsed_summary["deliberation_waiver"] = no_deliberation_reason.strip()
            denied = _ask_loom_permission(
                "nrol_ao_run_matcher_with_llama",
                {
                    "slug": slug,
                    "article_count": len(articles),
                    "decision_count": len(decisions),
                    "model": response.get("model"),
                    **({"deliberation": parsed_summary.get("deliberation")}
                       if debate_packet else
                       {"deliberationWaiver": parsed_summary.get("deliberation_waiver", "")}
                       if has_movers else {}),
                },
            )
            if denied:
                store.record(
                    job_id,
                    "denied",
                    task="run_matcher_with_llama",
                    slug=slug,
                    model=response.get("model"),
                    summary={**parsed_summary, "denied": denied},
                )
                return _json(
                    {
                        "job_id": job_id,
                        "committed": False,
                        "denied": denied,
                        "llama_output": output_text,
                        "decisions": decisions,
                    }
                )
            applied = news.apply_decisions(slug, articles, decisions)
            if debate_packet:
                applied["deliberation"] = debate_packet

        final_summary = {
            **parsed_summary,
            "committed": bool(commit),
            "applied": applied,
        }
        store.record(
            job_id,
            "completed",
            task="run_matcher_with_llama",
            slug=slug,
            model=response.get("model"),
            duration_ms=int((time.time() - start) * 1000),
            summary=final_summary,
        )
        return _json(
            {
                "job_id": job_id,
                "committed": bool(commit),
                "llama": {
                    "host": response.get("host"),
                    "model": response.get("model"),
                },
                "decisions": decisions,
                "llama_output": output_text,
                "applied": applied,
            }
        )
    except Exception as exc:
        store.record(
            job_id,
            "failed",
            task="run_matcher_with_llama",
            slug=slug,
            duration_ms=int((time.time() - start) * 1000),
            error=str(exc),
        )
        return _json({"job_id": job_id, "error": str(exc), "slug": slug})


@mcp.tool()
def run_matcher_with_model(
    slug: str,
    articles: list[dict],
    provider: str = "model-agnostic",
    commit: bool = False,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 600,
) -> str:
    """Run or prepare an NROL-AO matcher job for any model provider.

    provider=llama or provider=local executes through the monitored local
    llama-server endpoint. Other providers return the matcher prompt and the
    follow-up tool call shape so Claude, Codex, agy, or another model can run
    the reasoning externally and hand the output back to apply_matcher_output.
    """
    normalized = (provider or "model-agnostic").strip().lower()
    if normalized in {"llama", "local", "llama-server"}:
        return run_matcher_with_llama(
            slug=slug,
            articles=articles,
            commit=commit,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
        )

    store = _activity_store()
    job_id = new_job_id("matcher-model")
    try:
        engine = _import_from_repo("engine")
        news = _import_from_repo("framework.news_observation_pipeline")
        topic = engine.load_topic(slug)
        prompt = _build_matcher_prompt(news, topic, articles)
        store.record(
            job_id,
            "completed",
            task="run_matcher_with_model",
            slug=slug,
            model=model or normalized,
            summary={
                "provider": normalized,
                "article_count": len(articles),
                "direct_execution": False,
                "commit_requested": commit,
            },
            prompt=prompt,
        )
        return _json(
            {
                "job_id": job_id,
                "provider": normalized,
                "direct_execution": False,
                "committed": False,
                "matcher_prompt": prompt,
                "next_step": (
                    "Run matcher_prompt with the selected provider. Then call "
                    "apply_matcher_output(slug, articles, output_text, commit)."
                ),
                "commit_note": "commit is only applied after matcher output is returned and Loom approval passes.",
            }
        )
    except Exception as exc:
        store.record(
            job_id,
            "failed",
            task="run_matcher_with_model",
            slug=slug,
            model=model or normalized,
            error=str(exc),
        )
        return _json({"job_id": job_id, "error": str(exc), "slug": slug})


@mcp.tool()
def submit_transition(
    slug: str,
    transition: str,
    evidence: dict,
    indicator_id: str = "",
    observed_value: float | None = None,
    reason: str = "",
    missing_direction: str = "",
    commit: bool = False,
    include_topic: bool = False,
    existing_evidence_id: str = "",
    deliberation: dict | None = None,
    no_deliberation_reason: str = "",
) -> str:
    """Submit a typed runtime transition.

    transition must be PARK, FIRE, OBSERVE, SCHEMA_GAP, or IGNORE.
    existing_evidence_id rebinds the transition to an entry ALREADY in the
    evidence log (re-adjudicated parked evidence) instead of inserting a
    new one — the resolution path for the parked queue.
    commit=false performs validation and preview only. commit=true mutates the
    NROL-AO topic JSON through the source repo's own engine/pipeline gates.
    Operators cannot provide target posteriors or freeform likelihoods here.

    FIRE/OBSERVE commits additionally require deliberation (a debate record
    from deliberate_candidates / run_news_scan / review_parked) or an
    explicit no_deliberation_reason waiver, which is stamped on the evidence
    entry and shown in the Loom approval prompt.
    """
    try:
        transition_kind = _normalize_transition(transition)
        entry = _evidence_entry(evidence)
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        before = _posteriors(topic)

        indicator = None
        indicator_tier = None
        if transition_kind in {"FIRE", "OBSERVE"}:
            if not indicator_id:
                raise ValueError(f"{transition_kind} requires indicator_id")
            indicator, indicator_tier = _find_indicator(topic, indicator_id)
            if indicator is None:
                raise ValueError(f"indicator {indicator_id!r} not found on topic {slug!r}")
            if transition_kind == "OBSERVE" and observed_value is None:
                raise ValueError("OBSERVE requires observed_value")
            if transition_kind == "OBSERVE" and not indicator.get("observable"):
                raise ValueError(f"indicator {indicator_id!r} has no observable block")

        if transition_kind == "IGNORE":
            return _json(
                {
                    "slug": slug,
                    "transition": transition_kind,
                    "committed": False,
                    "posteriors_before": before,
                    "posteriors_after": before,
                    "ignored": True,
                    "note": "IGNORE does not write operational state.",
                }
            )

        if not commit:
            preview: dict[str, Any] = {
                "slug": slug,
                "transition": transition_kind,
                "committed": False,
                "posteriors_before": before,
                "posteriors_after": before,
                "evidence_entry": entry,
                "note": "Dry run only. Re-run with commit=true to mutate topic JSON.",
            }
            if indicator is not None:
                preview["indicator"] = _indicator_brief(indicator, indicator_tier or "")
                if indicator.get("likelihoods"):
                    preview["pre_committed_likelihoods"] = indicator["likelihoods"]
            if transition_kind == "SCHEMA_GAP":
                preview["schema_gap"] = _schema_gap_record(
                    evidence, reason, missing_direction
                )
            return _json(preview)

        refusal, delib_stamp = _require_deliberation(
            transition_kind, deliberation, no_deliberation_reason
        )
        if refusal:
            return _json({
                "slug": slug,
                "transition": transition_kind,
                "committed": False,
                "posteriors_before": before,
                "posteriors_after": before,
                "error": refusal,
            })
        entry.update(delib_stamp)

        job_id = new_job_id(f"transition-{transition_kind.lower()}")
        start = time.time()
        _activity_store().record(
            job_id,
            "queued",
            task="submit_transition",
            slug=slug,
            transition=transition_kind,
            summary={"indicator_id": indicator_id, "observed_value": observed_value,
                     **({"deliberation_waiver": delib_stamp["deliberationWaiver"]}
                        if "deliberationWaiver" in delib_stamp else {}),
                     **({"jury_verdict": delib_stamp["deliberation"].get("jury_verdict", "")}
                        if "deliberation" in delib_stamp else {})},
        )
        denied = _ask_loom_permission(
            "nrol_ao_submit_transition",
            {
                "slug": slug,
                "transition": transition_kind,
                **delib_stamp,
                "indicator_id": indicator_id,
                "observed_value": observed_value,
                "reason": reason,
                "evidence": entry,
            },
        )
        if denied:
            _activity_store().record(
                job_id,
                "denied",
                task="submit_transition",
                slug=slug,
                transition=transition_kind,
                summary={"denied": denied},
            )
            return _json({"job_id": job_id, "denied": denied, "committed": False})

        if transition_kind == "SCHEMA_GAP":
            result = _commit_schema_gap(slug, evidence, reason, missing_direction)
            _activity_store().record(
                job_id,
                "completed",
                task="submit_transition",
                slug=slug,
                transition=transition_kind,
                duration_ms=int((time.time() - start) * 1000),
                summary=result,
            )
            result["job_id"] = job_id
            return _json(result)

        pipeline = _import_from_repo("framework.pipeline")
        _activity_store().record(
            job_id,
            "running",
            task="submit_transition",
            slug=slug,
            transition=transition_kind,
            summary={"indicator_id": indicator_id, "observed_value": observed_value},
        )
        if transition_kind == "PARK":
            result = pipeline.process_evidence(
                slug=slug,
                entry=entry,
                fired_indicator_id=None,
                reason=reason or "MCP PARK transition",
                existing_evidence_id=existing_evidence_id or None,
            )
        elif transition_kind == "FIRE":
            result = pipeline.process_evidence(
                slug=slug,
                entry=entry,
                fired_indicator_id=indicator_id,
                reason=reason or "MCP FIRE transition",
                existing_evidence_id=existing_evidence_id or None,
            )
        elif transition_kind == "OBSERVE":
            result = pipeline.apply_observation(
                slug=slug,
                entry=entry,
                indicator_id=indicator_id,
                observed_value=observed_value,
                existing_evidence_id=existing_evidence_id or None,
            )
        else:
            raise ValueError(f"Unsupported mutating transition {transition_kind}")

        summarized = _summarize_pipeline_result(result, include_topic=include_topic)
        summarized["transition"] = transition_kind
        summarized["committed"] = True
        summarized["job_id"] = job_id
        _activity_store().record(
            job_id,
            "completed",
            task="submit_transition",
            slug=slug,
            transition=transition_kind,
            duration_ms=int((time.time() - start) * 1000),
            summary=summarized,
        )
        return _json(summarized)
    except Exception as exc:
        try:
            _activity_store().record(
                locals().get("job_id", new_job_id("transition")),
                "failed",
                task="submit_transition",
                slug=slug,
                transition=transition,
                error=str(exc),
            )
        except Exception:
            pass
        return _json({"error": str(exc), "slug": slug, "transition": transition})


_PROPOSAL_ACTIONS = {"PARK", "FIRE", "OBSERVE", "SCHEMA_GAP"}


def _validate_proposal_shape(
    topic: dict, action: str, indicator_id: str, observed_value: float | None
) -> None:
    """Static validation shared by propose_match and commit_match re-checks."""
    if action not in _PROPOSAL_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(_PROPOSAL_ACTIONS)}, got {action!r}. "
            "IGNORE is not proposable — simply do not propose, or withdraw."
        )
    if action in {"FIRE", "OBSERVE"}:
        if not indicator_id:
            raise ValueError(f"{action} proposals require indicator_id")
        indicator, _tier = _find_indicator(topic, indicator_id)
        if indicator is None:
            slug = topic.get("meta", {}).get("slug")
            raise ValueError(f"indicator {indicator_id!r} not found on topic {slug!r}")
        if action == "OBSERVE":
            if observed_value is None:
                raise ValueError("OBSERVE proposals require observed_value")
            if not indicator.get("observable"):
                raise ValueError(f"indicator {indicator_id!r} has no observable block")


@mcp.tool()
def submit_article(article: dict) -> str:
    """Store a fetched article/headline as a candidate observation.

    No posterior movement, no topic mutation. Returns a stable article_id
    (same URL resubmitted dedupes to the same id) for use with
    propose_match. This is the entry point of the proposal lifecycle:
    submit_article -> propose_match -> commit_match.
    """
    try:
        if not isinstance(article, dict):
            raise ValueError("article must be a JSON object")
        if not (article.get("url") or article.get("headline") or article.get("title")):
            raise ValueError("article requires url, headline, or title")
        record = _proposal_store().submit_article(
            article, submitted_by=os.environ.get("LOOM_CONV_ID", "headless")
        )
        record.pop("raw", None)
        return _json(record)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def propose_match(
    article_id: str,
    slug: str,
    action: str,
    indicator_id: str = "",
    observed_value: float | None = None,
    rationale: str = "",
    missing_direction: str = "",
    deliberation: dict | None = None,
    no_deliberation_reason: str = "",
) -> str:
    """Record a typed match proposal for a submitted article. No mutation.

    action is PARK, FIRE, OBSERVE, or SCHEMA_GAP. The proposal is validated
    statically (article exists, topic is ACTIVE, indicator/observable shape)
    and stored pending. Posteriors move only when commit_match(proposal_id)
    passes the server's gates and Loom approval.

    FIRE/OBSERVE proposals must carry deliberation (a debate record from
    deliberate_candidates) or an explicit no_deliberation_reason waiver;
    undeliberated posterior-moving proposals are refused at filing.
    """
    try:
        action = (action or "").strip().upper()
        if not rationale.strip():
            raise ValueError("rationale is required — state the directional case")
        refusal, delib_stamp = _require_deliberation(
            action, deliberation, no_deliberation_reason
        )
        if refusal:
            raise ValueError(refusal.replace("commit refused", "proposal refused", 1))
        store = _proposal_store()
        if store.get_article(article_id) is None:
            raise ValueError(f"article {article_id!r} not found; call submit_article first")
        engine = _import_from_repo("engine")
        topic = engine.load_topic(slug)
        if topic.get("meta", {}).get("status") != "ACTIVE":
            raise ValueError(f"topic {slug!r} is not ACTIVE")
        _validate_proposal_shape(topic, action, indicator_id, observed_value)
        record = store.add_proposal(
            article_id=article_id, slug=slug, action=action,
            indicator_id=indicator_id, observed_value=observed_value,
            rationale=rationale, missing_direction=missing_direction,
            deliberation=json.dumps(delib_stamp) if delib_stamp else "",
        )
        record["next_step"] = f"commit_match({record['id']!r}) after operator review"
        return _json(record)
    except Exception as exc:
        return _json({"error": str(exc), "article_id": article_id, "slug": slug})


@mcp.tool()
def commit_match(proposal_id: str, include_topic: bool = False) -> str:
    """Validate and apply a pending proposal through the engine's gates.

    Routes through the same machinery as submit_transition: typed
    transition, pre-committed likelihoods only, Loom approval (fail-closed),
    governance gates, activity ledger. Outcomes: committed (applied),
    rejected (validation/governance refused — recorded with reason), or
    pending (Loom approval denied; the proposal stays in the queue).
    """
    store = _proposal_store()
    try:
        prop = store.get_proposal(proposal_id)
        if prop is None:
            raise ValueError(f"proposal {proposal_id!r} not found")
        if prop["status"] != "pending":
            raise ValueError(
                f"proposal {proposal_id!r} already decided: {prop['status']}"
            )
        article = store.get_article(prop["article_id"])
        if article is None:
            raise ValueError(f"article {prop['article_id']!r} missing from store")

        engine = _import_from_repo("engine")
        topic = engine.load_topic(prop["slug"])
        _validate_proposal_shape(
            topic, prop["action"], prop.get("indicator_id") or "",
            prop.get("observed_value"),
        )

        # Deliberation gate: proposals filed without a debate record or
        # waiver (e.g. legacy rows predating the gate) cannot commit — the
        # queue is not a path around deliberation.
        prop_delib_stamp: dict = {}
        if (prop.get("deliberation") or "").strip():
            try:
                prop_delib_stamp = json.loads(prop["deliberation"])
            except Exception:
                prop_delib_stamp = {}
        if prop["action"] in {"FIRE", "OBSERVE"} and not prop_delib_stamp:
            return _json({
                "proposal_id": proposal_id, "committed": False,
                "status": "pending",
                "error": (
                    f"proposal {proposal_id!r} carries no deliberation record. "
                    "Run deliberate_candidates over the article and re-file via "
                    "propose_match(deliberation=...), or withdraw_proposal and "
                    "re-propose with an explicit no_deliberation_reason."
                ),
            })

        # Rebind proposals (from review_parked) point at evidence ALREADY in
        # the ledger — that's their whole point, so the URL duplicate guard
        # below does not apply to them. The engine binds the indicator to the
        # existing entry instead of inserting a new one.
        rebind_evidence_id = (prop.get("evidence_id") or "").strip()

        # Duplicate guard: the same URL must not be committed twice on a
        # topic (spec: same URL/canonical claim is not already committed).
        url = (article.get("url") or "").strip()
        if url and not rebind_evidence_id:
            for entry in topic.get("evidenceLog", []) or []:
                if (entry.get("url") or "").strip() == url:
                    store.mark_proposal(
                        proposal_id, "rejected",
                        note=f"duplicate: {url} already in evidenceLog as {entry.get('id')}",
                    )
                    return _json({
                        "proposal_id": proposal_id, "committed": False,
                        "status": "rejected",
                        "error": f"article URL already committed on {prop['slug']} "
                                 f"(evidence {entry.get('id')}). If this is "
                                 "re-adjudicated parked evidence, the proposal "
                                 "needs evidence_id set (review_parked does this).",
                    })

        evidence = {
            "headline": article.get("headline") or "",
            "url": url,
            "source": article.get("source") or "operator",
            "text": article.get("headline") or article.get("body") or url,
            "claim": prop.get("rationale") or "",
            "tag": "EVENT",
        }
        refs_raw = prop.get("evidence_refs")
        if refs_raw:
            try:
                evidence["evidence_refs"] = json.loads(refs_raw)
            except Exception:
                pass
        published = (
            article.get("published") or article.get("published_at")
            or article.get("date") or ""
        ).strip()
        if published:
            evidence["published"] = published
        raw = submit_transition(
            slug=prop["slug"],
            transition=prop["action"],
            evidence=evidence,
            indicator_id=prop.get("indicator_id") or "",
            observed_value=prop.get("observed_value"),
            reason=f"Proposal {proposal_id}: {prop.get('rationale') or ''}"[:300],
            missing_direction=prop.get("missing_direction") or "",
            commit=True,
            include_topic=include_topic,
            existing_evidence_id=rebind_evidence_id,
            deliberation=prop_delib_stamp.get("deliberation"),
            no_deliberation_reason=prop_delib_stamp.get("deliberationWaiver", ""),
        )
        result = json.loads(raw)
        if result.get("denied"):
            # Human said no this time — the proposal remains reviewable.
            return _json({
                "proposal_id": proposal_id, "committed": False,
                "status": "pending", "denied": result["denied"],
            })
        if result.get("error"):
            store.mark_proposal(proposal_id, "rejected", note=result["error"], result=result)
            return _json({
                "proposal_id": proposal_id, "committed": False,
                "status": "rejected", "error": result["error"],
            })
        store.mark_proposal(proposal_id, "committed", note="applied", result=result)
        result["proposal_id"] = proposal_id
        result["status"] = "committed"
        return _json(result)
    except Exception as exc:
        return _json({"proposal_id": proposal_id, "committed": False, "error": str(exc)})


@mcp.tool()
def list_proposals(slug: str = "", status: str = "pending", limit: int = 50) -> str:
    """List the proposal review queue (default: pending). Empty status = all."""
    try:
        rows = _proposal_store().list_proposals(slug=slug, status=status, limit=limit)
        return _json({"proposals": rows, "count": len(rows)})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def withdraw_proposal(proposal_id: str, reason: str = "") -> str:
    """Withdraw a pending proposal (the IGNORE decision for proposals)."""
    try:
        store = _proposal_store()
        prop = store.get_proposal(proposal_id)
        if prop is None:
            raise ValueError(f"proposal {proposal_id!r} not found")
        if prop["status"] != "pending":
            raise ValueError(f"proposal {proposal_id!r} already decided: {prop['status']}")
        record = store.mark_proposal(proposal_id, "withdrawn", note=reason or "withdrawn")
        return _json(record)
    except Exception as exc:
        return _json({"error": str(exc), "proposal_id": proposal_id})


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    # Keep an SSL context import live for parity with other Loom MCP servers on
    # self-signed localhost deployments.
    ssl.create_default_context()
    mcp.run(transport="stdio")
