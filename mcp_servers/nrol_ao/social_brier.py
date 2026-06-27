"""Social-media-user Brier — per-handle forecast calibration.

GREENFIELD pathway (no prior implementation existed). A social-media handle
(Bluesky/Twitter/etc.) is a *forecaster*: it emits probability forecasts
over topic hypotheses. This module logs those forecasts and, at topic
resolution, scores them with Brier against the resolved truth.

This is forecast calibration, NOT source trust — it is kept separate from
the source-trust ledger (framework/source_db.py) per the spec
(source-calibration-future-casts.md L96-100). Source trust scores whether a
source's *claims* were confirmed/refuted; this scores whether a handle's
*probability forecasts* were accurate.

Reuse, do not reinvent:
- framework.scoring.compute_brier_score (scoring.py:244) — the per-forecast
  Brier math. Pure function.
- The attribution pattern from framework/lens_calibration.py: walk forecasts,
  attribute each Brier to a key (handle instead of lens), aggregate into
  cells with {brier: mean, n, contributors}.

No engine.py modifications. The forecast store lives at
loom/social_forecasts/social_forecasts.jsonl — outside topic state. A logged
forecast is NOT evidence; it never moves posteriors.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_FORECAST_DIR = "social_forecasts"
_FORECAST_FILE = "social_forecasts.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _store_path(repo_root: Path) -> Path:
    return repo_root / "loom" / _FORECAST_DIR / _FORECAST_FILE


def _normalize_posteriors(posteriors: dict[str, float]) -> dict[str, float]:
    """Coerce + renormalize a forecast so it sums to 1.0 (Brier assumes a
    proper distribution). Refuses empty/negative forecasts."""
    out: dict[str, float] = {}
    for k, v in (posteriors or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv < 0:
            continue
        out[k] = fv
    total = sum(out.values())
    if total <= 0 or not out:
        raise ValueError("posteriors must be a non-empty distribution of non-negative values")
    return {k: round(v / total, 6) for k, v in out.items()}


def log_social_forecast(
    repo_root: Path, handle: str, slug: str, posteriors: dict[str, float],
    *, note: str = "", forecast_date: str = "",
) -> dict:
    """Append a handle's probability forecast for a topic to the store.

    The forecast is NOT evidence — it never moves posteriors or writes to
    topic state. It is scored later (at/after resolution) via
    score_social_user / social_user_brier.
    """
    if not handle or not slug:
        raise ValueError("handle and slug are required")
    norm = _normalize_posteriors(posteriors)
    path = _store_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "forecast_id": "fc_" + hashlib.sha256(
            (handle + "|" + slug + "|" + (forecast_date or _now_iso())).encode("utf-8")
        ).hexdigest()[:12],
        "logged_at": _now_iso(),
        "logged_by": os.environ.get("LOOM_CONV_ID", "headless"),
        "handle": handle,
        "slug": slug,
        "forecast_date": forecast_date or _now_iso()[:10],
        "posteriors": norm,
        "note": note,
        "scored": False,
        "brier": None,
        "resolved_hypothesis": None,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _load_forecasts(repo_root: Path) -> list[dict]:
    path = _store_path(repo_root)
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _resolved_hypothesis(topic: dict) -> str | None:
    """Read the resolved hypothesis off a topic (mirrors lens_calibration's
    helper — skips PARTIAL_EXPIRY entries)."""
    ps = topic.get("predictionScoring") or {}
    for entry in reversed(ps.get("outcomes") or []):
        if entry.get("type") == "PARTIAL_EXPIRY":
            continue
        if entry.get("resolved"):
            return entry["resolved"]
    if (topic.get("meta") or {}).get("status") == "RESOLVED":
        return (topic.get("meta") or {}).get("resolvedHypothesis")
    return None


def score_social_user(repo_root: Path, handle: str, slug: str = "") -> dict:
    """Score a handle's forecasts with Brier against resolved truth.

    If slug is given, score only that topic's forecasts (requires the topic
    be RESOLVED). If slug is empty, score all of the handle's forecasts whose
    topic has resolved. Unscored forecasts (unresolved topics) are reported
    as pending. Reuses framework.scoring.compute_brier_score. Read-only on
    the store EXCEPT it stamps brier/resolved_hypothesis back onto scored
    rows (an audit convenience, not a posterior movement)."""
    import importlib
    scoring = importlib.import_module("framework.scoring")
    engine = importlib.import_module("engine")

    rows = _load_forecasts(repo_root)
    mine = [r for r in rows if r.get("handle") == handle]
    if slug:
        mine = [r for r in mine if r.get("slug") == slug]
    if not mine:
        return {"handle": handle, "slug": slug or "(all)",
                "scored": 0, "pending": 0, "avg_brier": None,
                "note": "No forecasts logged for this handle" + (f" on {slug}" if slug else "")}

    scored: list[dict] = []
    pending: list[dict] = []
    briers: list[float] = []
    for r in mine:
        sslug = r.get("slug")
        try:
            topic = engine.load_topic(sslug)
        except Exception as exc:
            pending.append({"forecast_id": r["forecast_id"], "slug": sslug,
                            "reason": f"topic load failed: {exc}"})
            continue
        resolved = _resolved_hypothesis(topic)
        if not resolved:
            pending.append({"forecast_id": r["forecast_id"], "slug": sslug,
                            "reason": "topic not resolved"})
            continue
        try:
            result = scoring.compute_brier_score(r["posteriors"], resolved)
        except Exception as exc:
            pending.append({"forecast_id": r["forecast_id"], "slug": sslug,
                            "reason": f"brier failed: {exc}"})
            continue
        scored.append({
            "forecast_id": r["forecast_id"], "slug": sslug,
            "forecast_date": r.get("forecast_date"),
            "resolved_hypothesis": resolved,
            "brier": result["brier"], "per_hypothesis": result["per_hypothesis"],
        })
        briers.append(result["brier"])

    return {
        "handle": handle,
        "slug": slug or "(all)",
        "scored": len(scored),
        "pending": len(pending),
        "avg_brier": round(sum(briers) / len(briers), 6) if briers else None,
        "scored_forecasts": scored,
        "pending_forecasts": pending,
    }


def list_handles(repo_root: Path) -> dict:
    """List all handles with logged forecasts + counts. Read-only."""
    rows = _load_forecasts(repo_root)
    counts: dict[str, dict[str, Any]] = {}
    for r in rows:
        h = r.get("handle", "?")
        c = counts.setdefault(h, {"handle": h, "total": 0, "scored": 0,
                                  "slugs": set()})
        c["total"] += 1
        if r.get("scored"):
            c["scored"] += 1
        c["slugs"].add(r.get("slug", ""))
    for c in counts.values():
        c["slugs"] = sorted(s for s in c["slugs"] if s)
    return {"handles": list(counts.values()), "count": len(counts)}
