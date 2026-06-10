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
import sys
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .activity import ActivityStore, default_activity_dir, new_job_id
from .proposals import ProposalStore
from . import llama as llama_client

mcp = FastMCP("nrol-ao")

_DEFAULT_REPO = Path(r"C:\Claude-Code\NROL-AO\temp-repo")
_ALLOWED_TRANSITIONS = {"PARK", "FIRE", "OBSERVE", "SCHEMA_GAP", "IGNORE"}
_MUTATING_TRANSITIONS = {"PARK", "FIRE", "OBSERVE", "SCHEMA_GAP"}
_MAX_SEARCH_RESULTS_PER_CHANNEL = 6


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=True, default=str)


def _repo_path() -> Path:
    configured = os.environ.get("NROL_AO_REPO", "").strip()
    root = Path(configured) if configured else _DEFAULT_REPO
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
    ):
        if key in evidence:
            entry[key] = evidence[key]
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
        return _compact_query(f"{title} {question} latest news {window_label}")
    hyp = (topic.get("model", {}).get("hypotheses") or {}).get(channel, {}) or {}
    if isinstance(hyp, dict):
        label = hyp.get("label") or hyp.get("description") or hyp.get("desc") or ""
    else:
        label = str(hyp)
    return _compact_query(f"{title} {question} {channel} {label} latest news {window_label}")


def _search_web_articles(query: str, channel: str, max_results: int) -> list[dict]:
    """Server-side search backend for MCP-owned news scans."""
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError("ddgs package not installed; install ddgs for MCP-side news scans") from exc

    limit = max(1, min(int(max_results), _MAX_SEARCH_RESULTS_PER_CHANNEL))
    with DDGS() as ddgs:
        hits = list(ddgs.text(query, max_results=limit))

    articles = []
    today = datetime.now(timezone.utc).date().isoformat()
    for hit in hits:
        headline = (hit.get("title") or "").strip()
        url = (hit.get("href") or hit.get("url") or "").strip()
        body = (hit.get("body") or hit.get("snippet") or "").strip()
        if not headline and not url:
            continue
        source = ""
        try:
            from urllib.parse import urlparse
            source = urlparse(url).netloc.replace("www.", "")
        except Exception:
            pass
        articles.append(
            {
                "headline": headline or url,
                "url": url,
                "source": source or "web_search",
                "date": hit.get("date") or today,
                "relevance": body[:500] or f"Surfaced by server-side search channel {channel}.",
                "query": query,
                "channel": channel,
            }
        )
    return articles


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
    """3-stage deliberation (advocate / rebut / jury) over PARKed decisions.

    The strict matcher is deliberately conservative; without this structure
    every borderline observation dies in PARK on a single model's one-shot
    judgment. The advocate argues to rescue parks (OBSERVE on observable
    indicators only), the rebuttal attacks, a fresh jury renders verdicts
    with KEEP_PARK as the burden-of-proof default.

    Returns (jury_overrides, debate_packet) where jury_overrides maps
    idx -> {"action": {...}, "rationale": str} for MOVE_TO verdicts only.
    Never raises — a failed stage returns no overrides and reports why.
    """
    packet: dict[str, Any] = {"parks": 0, "argue_moves": 0, "jury_verdicts": {}}
    try:
        parks = news.get_parks_with_reasons(decisions)
        packet["parks"] = len(parks)
        if not parks:
            packet["note"] = "no parks to deliberate"
            return {}, packet

        def _stage(name: str, prompt: str, system: str) -> str:
            response = llama_client.chat(
                prompt,
                system_prompt=system,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
                disable_thinking=True,
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
            news.build_advocate_prompt(topic, articles, parks),
            "You are the ADVOCATE in the NROL-AO debate. Return only ADVOCATE "
            "blocks in the requested format.",
        )
        advocate_moves = [
            a for a in news.parse_advocate_output(adv_text)
            if str(a.get("verdict", "")).upper().startswith("ARGUE")
        ]
        packet["argue_moves"] = len(advocate_moves)
        if not advocate_moves:
            packet["note"] = "advocate found no defensible moves"
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
            "in the requested format. In doubt, KEEP_PARK.",
        )
        jury = news.parse_jury_output(jury_text)
        packet["jury_verdicts"] = {
            str(i): v.get("verdict_raw", "") for i, v in jury.items()
        }
        overrides = {
            idx: {"action": v["action"], "rationale": v.get("rationale", "")}
            for idx, v in jury.items()
            if (v.get("action") or {}).get("kind") not in (None, "", "PARK")
        }
        packet["rescued"] = len(overrides)
        return overrides, packet
    except Exception as exc:
        packet["error"] = str(exc)
        return {}, packet


def _apply_jury_overrides(decisions: list, jury_overrides: dict) -> list:
    """Fold MOVE_TO verdicts into the decision list (PARKs only)."""
    if not jury_overrides:
        return decisions
    effective = []
    for d in decisions:
        idx = d.get("idx")
        if idx in jury_overrides and (d.get("action") or {}).get("kind") == "PARK":
            nd = dict(d)
            nd["action"] = jury_overrides[idx]["action"]
            nd["jury_override"] = True
            nd["reason"] = (
                "jury: " + (jury_overrides[idx].get("rationale") or "MOVE_TO verdict")
            )[:500]
            effective.append(nd)
        else:
            effective.append(d)
    return effective


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


def _load_topics(engine, slugs: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Load topics tolerantly: one malformed file must not fail the whole call.

    Returns (topics, skipped) where skipped records files engine.list_topics
    surfaced but load_topic rejected (e.g. manifest.json has no meta section).
    """
    topics = []
    skipped = []
    selected = set(slugs or [])
    for row in engine.list_topics():
        slug = row.get("slug")
        if selected and slug not in selected:
            continue
        try:
            topics.append(engine.load_topic(slug))
        except Exception as exc:
            skipped.append({"slug": slug, "error": str(exc)})
    return topics, skipped


def _select_scan_topics(engine, slugs: list[str] | None, max_topics: int) -> list[dict]:
    selected = set(slugs or [])
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
        return _json(
            {
                "repo": str(_repo_path()),
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
                "Call read_topic or list_hypotheses to inspect a topic.",
                "Call run_news_scan for MCP-side search and deliberation.",
                "Review the returned operator packet.",
                "Use commit=true only when explicit mutation is intended and Loom approval is expected.",
            ],
            "proposal_lifecycle": [
                "submit_article(article) — store a candidate observation; no mutation.",
                "propose_match(article_id, slug, action, ...) — record a typed proposal; no mutation.",
                "list_proposals(slug, status) — review the pending queue.",
                "commit_match(proposal_id) — validate + apply through engine gates and Loom approval.",
                "withdraw_proposal(proposal_id) — the IGNORE decision for proposals.",
            ],
            "scan_semantics": {
                "commit_false": "No evidence/posterior mutation.",
                "dry_run_false": "Records successful scan coverage by stamping topic.meta.lastScanned.",
                "dry_run_true": "Preview only; does not stamp lastScanned.",
            },
            "do_not": [
                "Do not perform operator-side web search as a fallback for run_news_scan.",
                "Do not edit NROL topic JSON directly.",
                "Do not invent likelihoods, posteriors, or target probabilities.",
                "If this MCP server is unavailable, stop and report a setup failure.",
            ],
            "tools": [
                "nrol_status",
                "help",
                "list_topics",
                "topic_status",
                "read_topic",
                "list_hypotheses",
                "run_news_scan",
                "list_activity",
                "submit_transition",
                "submit_article",
                "propose_match",
                "commit_match",
                "list_proposals",
                "withdraw_proposal",
            ],
        }
    )


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
        rows = engine.list_topics()
        if status:
            rows = [row for row in rows if str(row.get("status", "")).upper() == status.upper()]
        if include_governance:
            enriched = []
            for row in rows:
                try:
                    topic = engine.load_topic(row["slug"])
                except Exception as exc:
                    enriched.append({**row, "loadError": str(exc)})
                    continue
                enriched.append(_topic_summary(topic))
            rows = enriched
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
            window = mutation.compute_time_window(topic, tempo_floor_hours=floor)
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
        matcher_prompt = news.build_matcher_prompt(topic, deduped) if deduped else ""
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

    This is the one-call operational path: select stale topics, perform
    server-side web search, dedupe articles, deliberate with the local
    llama-server matcher, parse FIRE/OBSERVE/PARK/SCHEMA_GAP decisions, and
    optionally apply through NROL engine gates after Loom approval.

    dry_run=true never mutates state and does not stamp lastScanned.
    dry_run=false records successful scan coverage by stamping lastScanned.
    commit=true additionally applies evidence decisions after Loom approval.

    commit_policy="safe" is the scheduled-scan policy (spec Flow B): PARK
    and SCHEMA_GAP decisions auto-apply (they cannot move posteriors —
    engine-enforced), while FIRE/OBSERVE decisions are filed as pending
    proposals for operator review via list_proposals/commit_match. No
    posterior ever moves without a human approving it. A digest is written
    beside the activity ledger.
    """
    store = _activity_store()
    job_id = new_job_id("news-scan-worker")
    start = time.time()
    try:
        engine = _import_from_repo("engine")
        mutation = _import_from_repo("framework.news_mutation")
        news = _import_from_repo("framework.news_observation_pipeline")
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
            window = mutation.compute_time_window(topic, tempo_floor_hours=floor)
            channels = list((topic.get("model", {}).get("hypotheses") or {}).keys()) + ["wildcard"]

            store.record(
                job_id,
                "running",
                task="run_news_scan",
                slug=slug,
                model=model or llama_client.llama_model(),
                summary={"phase": "searching", "channels": channels, "window": window.get("label")},
            )
            parsed_by_channel = {}
            queries = {}
            search_errors = {}
            for channel in channels:
                query = _topic_query(topic, channel, window.get("label", "recent period"))
                queries[channel] = query
                try:
                    parsed_by_channel[channel] = _search_web_articles(
                        query,
                        channel,
                        max_results_per_channel,
                    )
                except Exception as exc:
                    search_errors[channel] = str(exc)
                    parsed_by_channel[channel] = []

            deduped, surfaced = mutation.dedupe_articles(parsed_by_channel)
            total_articles += len(deduped)
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
                for item in deduped:
                    art = item.get("article", item) if isinstance(item, dict) else item
                    excerpt = _fetch_article_excerpt(art.get("url", ""), excerpt_chars)
                    if excerpt:
                        art["excerpt"] = excerpt
                        fetched += 1
                excerpt_stats = {
                    "fetched": fetched,
                    "of": len(deduped),
                    "chars_cap": excerpt_chars,
                }
            if deduped:
                matcher_prompt = news.build_matcher_prompt(topic, deduped)
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
                    decisions = _apply_jury_overrides(decisions, jury_overrides)

                if commit_policy == "safe" and not commit and not dry_run and decisions:
                    safe_decisions = [
                        d for d in decisions
                        if d.get("action", {}).get("kind") in {"PARK", "SCHEMA_GAP", "IGNORE"}
                    ]
                    review_decisions = [
                        d for d in decisions
                        if d.get("action", {}).get("kind") in {"FIRE", "OBSERVE"}
                    ]
                    if safe_decisions:
                        # PARK/SCHEMA_GAP cannot move posteriors (engine-
                        # enforced, capability-tested) — safe to auto-apply.
                        applied = news.apply_decisions(slug, deduped, safe_decisions)
                    proposals_filed = []
                    pstore = _proposal_store()
                    for d in review_decisions:
                        idx = d.get("idx") or 0
                        art = deduped[idx - 1] if 0 < idx <= len(deduped) else None
                        if art is None:
                            continue
                        try:
                            art_rec = pstore.submit_article(art, submitted_by="scheduled-scan")
                            action = d.get("action", {})
                            _validate_proposal_shape(
                                topic, action.get("kind", ""),
                                action.get("indicator_id", ""), action.get("value"),
                            )
                            prop = pstore.add_proposal(
                                article_id=art_rec["id"],
                                slug=slug,
                                action=action.get("kind", ""),
                                indicator_id=action.get("indicator_id", ""),
                                observed_value=action.get("value"),
                                rationale=(d.get("reason") or d.get("claim") or "matcher decision")[:500],
                            )
                            proposals_filed.append(prop["id"])
                        except Exception as exc:
                            search_errors[f"proposal_idx_{idx}"] = str(exc)
                    packet_policy = {
                        "policy": "safe",
                        "auto_committed": (applied or {}),
                        "proposals_filed": proposals_filed,
                    }

                if commit:
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
                    f"- deliberation: {db.get('parks', 0)} parks debated, "
                    f"{db.get('argue_moves', 0)} argued, "
                    f"{db.get('rescued', 0)} rescued by jury"
                )
        policy = tp.get("commit_policy") or {}
        if policy:
            auto = policy.get("auto_committed") or {}
            lines.append(
                f"- auto-committed (safe): park={auto.get('park', 0)} "
                f"schema_gap={auto.get('schema_gap', 0)} "
                f"rejections={auto.get('engine_rejections', 0)}"
            )
            filed = policy.get("proposals_filed") or []
            lines.append(
                f"- proposals filed for review: {len(filed)}"
                + (f" ({', '.join(filed)})" if filed else "")
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
) -> str:
    """Re-adjudicate parked evidence against the CURRENT indicator schema.

    deliberate=true (default) runs the advocate/rebut/jury debate over
    re-decisions that remain PARK — these items already died once on a
    one-shot judgment; the debate is what makes re-review more than a
    second coin flip from the same model.

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
        for d in due:
            ev = evidence_by_id.get(d["evidence_id"])
            if not ev:
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
                "refetched": sum(1 for a in articles if a.get("excerpt")),
            },
        )
        prompt = news.build_matcher_prompt(topic, articles)
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
            decisions = _apply_jury_overrides(decisions, jury_overrides)

        pstore = _proposal_store()
        reviews = []
        proposals_filed = []
        errors = {}
        for d in decisions:
            idx = d.get("idx") or 0
            if not (0 < idx <= len(selected)):
                continue
            ev_id = selected[idx - 1]
            action = d.get("action", {}) or {}
            kind = action.get("kind", "")
            review = {
                "evidence_id": ev_id,
                "decision": kind or "NO_DECISION",
                "note": (d.get("reason") or d.get("claim") or "")[:300],
            }
            if kind in {"FIRE", "OBSERVE"} and not dry_run:
                try:
                    art_rec = pstore.submit_article(articles[idx - 1], submitted_by="review-parked")
                    _validate_proposal_shape(
                        topic, kind, action.get("indicator_id", ""), action.get("value"),
                    )
                    prop = pstore.add_proposal(
                        article_id=art_rec["id"],
                        slug=slug,
                        action=kind,
                        indicator_id=action.get("indicator_id", ""),
                        observed_value=action.get("value"),
                        rationale=(
                            f"re-adjudication of parked {ev_id}: "
                            + (d.get("reason") or d.get("claim") or "matcher re-decision")
                        )[:500],
                        evidence_id=ev_id,
                    )
                    review["escalated_proposal_id"] = prop["id"]
                    proposals_filed.append(prop["id"])
                except Exception as exc:
                    errors[ev_id] = str(exc)
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
def triage_headline(headline: str, source: str = "") -> str:
    """Triage a headline against active topics without mutating state."""
    try:
        engine = _import_from_repo("engine")
        return _json(engine.triage_headline(headline, source=source or None))
    except Exception as exc:
        return _json({"error": str(exc), "headline": headline})


@mcp.tool()
def build_matcher_prompt(slug: str, articles: list[dict]) -> str:
    """Build the source repo's OBSERVE/FIRE/PARK/SCHEMA_GAP matcher prompt."""
    try:
        engine = _import_from_repo("engine")
        news = _import_from_repo("framework.news_observation_pipeline")
        topic = engine.load_topic(slug)
        return news.build_matcher_prompt(topic, articles)
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
def apply_matcher_output(slug: str, articles: list[dict], output_text: str, commit: bool = False) -> str:
    """Parse matcher output and optionally apply it through the NROL-AO pipeline."""
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
        denied = _ask_loom_permission(
            "nrol_ao_apply_matcher_output",
            {"slug": slug, "article_count": len(articles), "decision_count": len(decisions)},
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
            summary={"commit": True},
        )
        result = news.apply_decisions(slug, articles, decisions)
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
def run_matcher_with_llama(
    slug: str,
    articles: list[dict],
    commit: bool = False,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout_sec: int = 600,
) -> str:
    """Run the NROL-AO matcher prompt through llama-server and parse decisions.

    commit=false only records and returns llama output plus parsed decisions.
    commit=true asks Loom permission, then routes decisions through the existing
    NROL-AO pipeline. All job states are written to the activity ledger.
    """
    store = _activity_store()
    job_id = new_job_id("matcher-llama")
    start = time.time()
    try:
        engine = _import_from_repo("engine")
        news = _import_from_repo("framework.news_observation_pipeline")
        topic = engine.load_topic(slug)
        prompt = news.build_matcher_prompt(topic, articles)
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
        if commit:
            denied = _ask_loom_permission(
                "nrol_ao_run_matcher_with_llama",
                {
                    "slug": slug,
                    "article_count": len(articles),
                    "decision_count": len(decisions),
                    "model": response.get("model"),
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
        prompt = news.build_matcher_prompt(topic, articles)
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
) -> str:
    """Submit a typed runtime transition.

    transition must be PARK, FIRE, OBSERVE, SCHEMA_GAP, or IGNORE.
    existing_evidence_id rebinds the transition to an entry ALREADY in the
    evidence log (re-adjudicated parked evidence) instead of inserting a
    new one — the resolution path for the parked queue.
    commit=false performs validation and preview only. commit=true mutates the
    NROL-AO topic JSON through the source repo's own engine/pipeline gates.
    Operators cannot provide target posteriors or freeform likelihoods here.
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

        job_id = new_job_id(f"transition-{transition_kind.lower()}")
        start = time.time()
        _activity_store().record(
            job_id,
            "queued",
            task="submit_transition",
            slug=slug,
            transition=transition_kind,
            summary={"indicator_id": indicator_id, "observed_value": observed_value},
        )
        denied = _ask_loom_permission(
            "nrol_ao_submit_transition",
            {
                "slug": slug,
                "transition": transition_kind,
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
) -> str:
    """Record a typed match proposal for a submitted article. No mutation.

    action is PARK, FIRE, OBSERVE, or SCHEMA_GAP. The proposal is validated
    statically (article exists, topic is ACTIVE, indicator/observable shape)
    and stored pending. Posteriors move only when commit_match(proposal_id)
    passes the server's gates and Loom approval.
    """
    try:
        action = (action or "").strip().upper()
        if not rationale.strip():
            raise ValueError("rationale is required — state the directional case")
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
