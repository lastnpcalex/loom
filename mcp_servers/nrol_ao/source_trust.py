"""Source-trust surfacing — read-only views over the LIVE source-trust stores.

The source-trust machinery is LIVE in the engine repo (framework/source_db.py,
framework/source_ledger.py, framework/calibrate.py). It was never a Brier
score — it is a Bayesian trust ledger updated by confirmed/refuted claims.
This module EXPOSES that existing machinery through the MCP boundary; it does
not reinvent trust, move posteriors, or write to source_db.json.

Three read surfaces (per specs/source-calibration-future-casts.md Future
Addition 1, L102-149):
  - source_calibration_status: topic-local sourceCalibration summary, or the
    cross-topic database summary when slug is empty.
  - source_profile: one source's full profile (baseTrust, domains,
    effectiveTrust, claim counts) from the cross-topic DB.
  - validate_source_db: schema sanity check of sources/source_db.json.

The forecast Brier (resolution scoring) and source trust (claim calibration)
are kept separate, as the spec requires (L96-100). This module is the
source-trust side; the Brier side lives in resolution.py.
"""

from __future__ import annotations

from typing import Any


def _load_db():
    """Load the cross-topic source database (sources/source_db.json)."""
    import importlib
    sd = importlib.import_module("framework.source_db")
    return sd, sd.load_db()


def source_calibration_status(slug: str = "") -> dict:
    """Topic-local sourceCalibration summary (slug given) or cross-topic DB
    summary (slug empty). Read-only."""
    import importlib
    sd, db = _load_db()
    if slug:
        engine = importlib.import_module("engine")
        topic = engine.load_topic(slug)
        cal = topic.get("sourceCalibration") or {}
        ledger = cal.get("ledger") or []
        # Per-source effective trust from the topic-local ledger.
        eff = cal.get("effectiveTrust") or {}
        sources = []
        for rec in ledger:
            s = rec.get("source") or rec.get("confirming_source") or ""
            if s and s not in [x["source"] for x in sources]:
                sources.append({
                    "source": s,
                    "topic_local_trust": eff.get(s),
                    "cross_topic_trust": sd.get_source_profile(db, s) or None,
                })
        return {
            "slug": slug,
            "ledger_entries": len(ledger),
            "sources": sources,
            "has_source_calibration": bool(cal),
        }
    # Cross-topic summary
    srcs = db.get("sources", {})
    return {
        "sources_tracked": len(srcs),
        "last_full_scan": (db.get("meta") or {}).get("lastFullScan"),
        "topics_scanned": (db.get("meta") or {}).get("topicsScanned") or [],
        "db_path": str(getattr(sd, "_DB_FILE", "")),
        "sources": [
            {"name": n, "effectiveTrust": s.get("effectiveTrust"),
             "totalClaims": s.get("totalClaims"), "totalConfirmed": s.get("totalConfirmed"),
             "totalRefuted": s.get("totalRefuted"), "category": s.get("category")}
            for n, s in srcs.items()
        ],
    }


def source_profile(source: str, domain: str = "") -> dict:
    """Full profile for one source from the cross-topic DB. Optionally a
    domain tag resolves the domain-specific trust via the 5-tier fallback
    chain (domain -> effective -> base -> static prior -> 0.5). Read-only."""
    sd, db = _load_db()
    profile = sd.get_source_profile(db, source)
    if not profile:
        return {"source": source, "tracked": False,
                "fallback_trust": 0.50,
                "note": "Source not in cross-topic DB; governor falls back to static prior / 0.50."}
    out: dict[str, Any] = {"source": source, "tracked": True, "profile": profile}
    if domain:
        out["domain"] = domain
        out["domain_trust"] = sd.get_domain_trust(db, source, domain)
    return out


def validate_source_db() -> dict:
    """Schema sanity check of sources/source_db.json. Reports structural
    problems (missing required fields, non-numeric trust, negative counts)
    without raising. Read-only."""
    sd, db = _load_db()
    problems: list[str] = []
    srcs = db.get("sources")
    if not isinstance(srcs, dict):
        problems.append("sources/ missing or not an object")
        return {"valid": False, "sources_checked": 0, "problems": problems,
                "db_path": str(getattr(sd, "_DB_FILE", ""))}
    for name, s in srcs.items():
        if not isinstance(s, dict):
            problems.append(f"{name}: record is not an object")
            continue
        for f in ("baseTrust", "effectiveTrust"):
            v = s.get(f)
            if v is not None and not isinstance(v, (int, float)):
                problems.append(f"{name}: {f} is not numeric ({v!r})")
        for f in ("totalClaims", "totalConfirmed", "totalRefuted"):
            v = s.get(f, 0)
            if not isinstance(v, int) or v < 0:
                problems.append(f"{name}: {f} invalid ({v!r})")
        for tag, dom in (s.get("domains") or {}).items():
            if not isinstance(dom, dict):
                problems.append(f"{name}/{tag}: domain not an object")
                continue
            for f in ("claims", "confirmed", "refuted"):
                v = dom.get(f, 0)
                if not isinstance(v, int) or v < 0:
                    problems.append(f"{name}/{tag}: {f} invalid ({v!r})")
    return {
        "valid": not problems,
        "sources_checked": len(srcs),
        "problems": problems,
        "db_path": str(getattr(sd, "_DB_FILE", "")),
    }


def domain_patterns(min_claims: int = 3) -> dict:
    """Cross-source domain reliability patterns (which domains are most/least
    reliable, per-source variance). Thin wrapper over source_db's existing
    find_domain_patterns. Read-only."""
    sd, db = _load_db()
    return sd.find_domain_patterns(db, min_claims=min_claims)
