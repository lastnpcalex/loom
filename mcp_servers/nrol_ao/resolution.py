"""Resolution: two-lane Brier (shadow vs committed) + red-team after-action.

At topic resolution, compute Brier scores comparing the SHADOW posterior
trajectory against the ACTUAL/committed posterior trajectory, both vs the
resolved truth; then run a red-team after-action review over the evidence
stream and scan digests.

The shadow path is reconstructed, not persisted. ``shadow_posteriors`` is
stateless and deterministic for a fixed (spec, asof, seed), so for each
committed ``posteriorHistory`` date ``d`` we call
``dynamics_shadow.run(repo_root, slug, asof=d)`` to recover the shadow
posterior as of that date. No new persistence is introduced for the shadow
lane — the comparison is reproducible on demand.

Reuse, do not reinvent:
- ``framework.scoring.record_outcome`` (scoring.py:96) — committed-lane
  scoring; called UNMODIFIED. It appends the outcome and scores snapshots
  on the live topic dict; the caller saves.
- ``framework.scoring.compute_brier_score`` (scoring.py:244) — per-snapshot
  Brier; pure function.
- ``tests/synthetic/score.py:brier`` (L144) — the two-lane (oracle vs
  pipeline) shape; generalized here to shadow vs committed, using the
  topic's actual posteriorHistory dates as checkpoints.
- ``engine.extract_posteriors`` (engine.py:3837) — reads both flat and
  nested posteriorHistory entry formats.

No engine.py modifications. Only composes existing read-only accessors plus
one call to ``scoring.record_outcome`` (which mutates the topic's
predictionScoring block — that is the committed-lane scoring the engine
already does at resolution; the caller persists it via save_topic).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _posteriors(topic: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for hk, h in (topic.get("model", {}).get("hypotheses") or {}).items():
        if isinstance(h, dict):
            out[hk] = float(h.get("posterior", 0.0))
    return out


def _committed_trajectory(topic: dict) -> list[dict]:
    """Extract the committed posterior trajectory from posteriorHistory.

    Returns [{date, posteriors}] sorted by date, using extract_posteriors to
    handle both flat and nested entry formats.
    """
    import importlib
    engine = importlib.import_module("engine")
    h_keys = list((topic.get("model", {}).get("hypotheses") or {}).keys())
    history = (topic.get("model", {}).get("posteriorHistory") or [])
    rows: list[dict] = []
    for entry in history:
        date = entry.get("date") or (entry.get("timestamp", "")[:10] if isinstance(entry.get("timestamp"), str) else "")
        if not date:
            continue
        post = engine.extract_posteriors(entry, h_keys)
        if post:
            rows.append({"date": date, "posteriors": {k: float(post.get(k, 0.0)) for k in h_keys}})
    rows.sort(key=lambda r: r["date"])
    return rows


def reconstruct_shadow_trajectory(
    repo_root: Path, slug: str, committed_dates: list[str], hypothesis_keys: list[str],
) -> list[dict]:
    """Reconstruct the shadow posterior path at each committed posteriorHistory date.

    Calls dynamics_shadow.run(repo_root, slug, asof=d) for each date d.
    Deterministic: fixed seed=20260610 in shadow_posteriors. The shadow
    posterior's hypothesis keys may include a residual (e.g. H_never) that
    has no committed counterpart; align by intersecting on hypothesis_keys
    and renormalizing the shadow lane to the committed key set.
    """
    import importlib
    dyn = importlib.import_module("framework.dynamics_shadow")
    out: list[dict] = []
    for d in committed_dates:
        try:
            result = dyn.run(repo_root, slug, asof=d)
            if "error" in result:
                out.append({"date": d, "error": result["error"]})
                continue
            shadow_post = result.get("shadow_posteriors", {})
            # Align to the committed key set. Shadow may carry a residual
            # hypothesis (H_never) absent from the committed set; fold it out
            # and renormalize so the lanes are comparable.
            aligned = {k: float(shadow_post.get(k, 0.0)) for k in hypothesis_keys}
            total = sum(aligned.values())
            if total > 0:
                aligned = {k: v / total for k, v in aligned.items()}
            out.append({"date": d, "shadow_posteriors": aligned,
                        "raw_shadow": shadow_post,
                        "elapsed_in_entrenched_days": result.get("elapsed_in_entrenched_days")})
        except Exception as exc:
            out.append({"date": d, "error": str(exc)})
    return out


def compute_two_lane_brier(
    committed_trajectory: list[dict],
    shadow_trajectory: list[dict],
    resolved_hypothesis: str,
    hypothesis_keys: list[str],
    priors: dict[str, float] | None = None,
) -> dict:
    """Two-lane Brier: shadow vs committed, both vs the resolved truth.

    Generalizes tests/synthetic/score.py:brier (L144): step-function
    interpolation (posteriors hold until the next update), per-checkpoint
    per-hypothesis squared error, plus full-vector Brier at the last
    checkpoint. Reuses framework.scoring.compute_brier_score for the
    per-snapshot math.
    """
    import importlib
    scoring = importlib.import_module("framework.scoring")
    _priors = dict(priors or {})

    def at(rows: list[dict], date: str, key: str) -> dict[str, float]:
        cur = dict(_priors) if _priors else {k: 0.0 for k in hypothesis_keys}
        for row in rows:
            if row.get("date", "") <= date and "posteriors" in row:
                cur = dict(row["posteriors"])
        return cur

    # Checkpoints = committed update dates (the dates the operator actually
    # moved the committed posterior). Both lanes are evaluated at each.
    checkpoints = sorted({r["date"] for r in committed_trajectory if r.get("date")})
    if not checkpoints:
        return {"checkpoints": [], "vector_end": {"shadow": None, "committed": None},
                "avg_brier": {"shadow": None, "committed": None},
                "note": "No committed posteriorHistory checkpoints to score."}

    rows: list[dict] = []
    shadow_briers: list[float] = []
    committed_briers: list[float] = []
    for d in checkpoints:
        committed_post = at(committed_trajectory, d, "committed")
        shadow_post = at(shadow_trajectory, d, "shadow") if shadow_trajectory else committed_post
        # Shadow rows may carry errors instead of posteriors; fall back to
        # committed so the checkpoint still scores the committed lane.
        shadow_row = next((r for r in shadow_trajectory if r.get("date") == d), None)
        if shadow_row and "shadow_posteriors" in shadow_row:
            shadow_post = shadow_row["shadow_posteriors"]

        c_score = scoring.compute_brier_score(committed_post, resolved_hypothesis)
        s_score = scoring.compute_brier_score(shadow_post, resolved_hypothesis)
        committed_briers.append(c_score["brier"])
        shadow_briers.append(s_score["brier"])
        rows.append({
            "date": d,
            "shadow_brier": s_score["brier"],
            "committed_brier": c_score["brier"],
            "shadow_posteriors": shadow_post,
            "committed_posteriors": committed_post,
        })

    end = checkpoints[-1]
    end_committed = at(committed_trajectory, end, "committed")
    end_shadow_row = next((r for r in shadow_trajectory if r.get("date") == end), None)
    end_shadow = end_shadow_row["shadow_posteriors"] if end_shadow_row and "shadow_posteriors" in end_shadow_row else end_committed
    vector_end = {
        "shadow": round(sum((end_shadow.get(h, 0.0) - (1.0 if h == resolved_hypothesis else 0.0)) ** 2
                             for h in hypothesis_keys), 6),
        "committed": round(sum((end_committed.get(h, 0.0) - (1.0 if h == resolved_hypothesis else 0.0)) ** 2
                               for h in hypothesis_keys), 6),
    }

    def _avg(xs):
        return round(sum(xs) / len(xs), 6) if xs else None

    return {
        "checkpoints": rows,
        "vector_end": vector_end,
        "avg_brier": {"shadow": _avg(shadow_briers), "committed": _avg(committed_briers)},
    }


def _red_team_after_action(
    slug: str, resolved_hypothesis: str, resolved_label: str,
    brier_report: dict, evidence_summary: list[dict], scan_summary: list[dict],
    *, llama_client, model: str = "", timeout_sec: int = 600,
) -> dict:
    """Red-team the resolution: did the system get it right, and where did
    perception vs authority diverge? Mirrors _red_team_design (server.py:3940):
    single-pass llama_client.chat, structured prompt, verdict parse."""
    review: dict[str, Any] = {
        "verdict": "UNREVIEWED",
        "at": _now_iso(),
        "model": "",
        "critique": "",
        "missed_signals": [],
        "false_signals": [],
        "shadow_vs_committed_divergence": "",
    }
    try:
        prompt_lines = [
            "You are the RED TEAM conducting an after-action review of a "
            "resolved NROL-AO topic. The topic has resolved; review the full "
            "evidence and scan history. Identify: missed signals (events the "
            "system should have caught earlier), false signals (parked or "
            "noisy evidence that wasted review), and whether the shadow "
            "(dynamics-derived) or committed (indicator-driven) posterior "
            "tracked the truth better. End with VERDICT: SOUND or "
            "VERDICT: REVISE, then MISSED_SIGNALS: (comma list), "
            "FALSE_SIGNALS: (comma list), SHADOW_VS_COMMITTED: (one sentence).",
            f"TOPIC: {slug}",
            f"RESOLVED: {resolved_hypothesis} — {resolved_label}",
            "TWO_LANE_BRIER: " + json.dumps(brier_report, default=str)[:2000],
            "EVIDENCE_SUMMARY: " + json.dumps(evidence_summary, default=str)[:2500],
            "SCAN_SUMMARY: " + json.dumps(scan_summary, default=str)[:1500],
        ]
        response = llama_client.chat(
            "\n".join(prompt_lines),
            system_prompt="You are an adversarial after-action reviewer.",
            model=model, temperature=0.3, max_tokens=2048,
            timeout_sec=timeout_sec, disable_thinking=True,
        )
        text = response.get("text", "")
        review["model"] = response.get("model") or ""
        review["critique"] = text
        verdicts = re.findall(r"VERDICT:\s*(SOUND|REVISE)", text.upper())
        review["verdict"] = verdicts[-1] if verdicts else ("REVISE" if text.strip() else "UNREVIEWED")
        for field, key in (("MISSED_SIGNALS", "missed_signals"),
                           ("FALSE_SIGNALS", "false_signals")):
            m = re.search(rf"{field}:\s*(.+?)(?=\n[A-Z_]+:|\Z)", text, re.S)
            if m:
                review[key] = [s.strip() for s in re.split(r"[,;\n]", m.group(1)) if s.strip()][:10]
        m = re.search(r"SHADOW_VS_COMMITTED:\s*(.+?)(?=\n[A-Z_]+:|\Z)", text, re.S)
        if m:
            review["shadow_vs_committed_divergence"] = m.group(1).strip()[:500]
    except Exception as exc:
        review["error"] = str(exc)
    return review


def _evidence_summary(topic: dict, limit: int = 40) -> list[dict]:
    """A compact projection of the evidence log for the AAR prompt."""
    rows: list[dict] = []
    for ev in (topic.get("evidenceLog") or [])[:limit]:
        rows.append({
            "id": ev.get("id"),
            "time": ev.get("time"),
            "tag": ev.get("tag") or (ev.get("tags") or [None])[0],
            "source": ev.get("source"),
            "claimState": ev.get("claimState"),
            "posteriorImpact": ev.get("posteriorImpact"),
            "text": (ev.get("text") or "")[:160],
        })
    return rows


def _scan_summary(scan_digests: list[dict], limit: int = 10) -> list[dict]:
    """A compact projection of recent scan digests for the AAR prompt."""
    out: list[dict] = []
    for dg in (scan_digests or [])[:limit]:
        out.append({
            "job_id": dg.get("job_id"),
            "timestamp": dg.get("timestamp"),
            "article_count": dg.get("article_count"),
            "decision_count": dg.get("decision_count"),
            "commit_policy": dg.get("commit_policy"),
        })
    return out


def run_resolution(
    repo_root: Path, slug: str, resolved_hypothesis: str, note: str,
    skip_aar: bool, llama_client, model: str, timeout_sec: int,
    scan_digests: list[dict] | None = None,
) -> dict:
    """Resolve a topic: set RESOLVED, record outcome, compute two-lane Brier,
    optionally generate an AAR. Returns a packet describing what was done.

    The caller is responsible for the fail-closed Loom permission gate and
    for persisting via engine.save_topic — this pure function does NOT save.
    It mutates the topic dict in place (status, predictionScoring) so the
    caller can save the whole result.
    """
    import importlib
    engine = importlib.import_module("engine")
    scoring = importlib.import_module("framework.scoring")

    topic = engine.load_topic(slug)
    meta = topic.get("meta", {})
    if meta.get("status") == "RESOLVED":
        raise ValueError(f"Topic {slug!r} is already RESOLVED.")
    if meta.get("status") != "ACTIVE":
        raise ValueError(f"Topic {slug!r} is {meta.get('status')!r}; only ACTIVE topics resolve.")
    hypotheses = topic.get("model", {}).get("hypotheses") or {}
    if resolved_hypothesis not in hypotheses:
        raise ValueError(
            f"Unknown hypothesis {resolved_hypothesis!r}. "
            f"Valid keys: {list(hypotheses.keys())}"
        )
    h_keys = list(hypotheses.keys())

    # Committed-lane scoring (the engine's existing resolution math).
    outcome = scoring.record_outcome(topic, resolved_hypothesis, note=note)

    # Reconstruct the shadow trajectory and compute two-lane Brier.
    committed_traj = _committed_trajectory(topic)
    committed_dates = [r["date"] for r in committed_traj]
    priors = committed_traj[0]["posteriors"] if committed_traj else None
    shadow_traj: list[dict] = []
    brier_report: dict | None = None
    shadow_error: str | None = None
    try:
        shadow_traj = reconstruct_shadow_trajectory(repo_root, slug, committed_dates, h_keys)
        # If every shadow row is an error (e.g. no dynamics spec), the shadow
        # lane is unavailable — surface it honestly rather than silently
        # degrading to shadow==committed in compute_two_lane_brier.
        if shadow_traj and all("error" in r for r in shadow_traj):
            shadow_error = shadow_traj[0]["error"]
            shadow_traj = []
        if shadow_traj:
            brier_report = compute_two_lane_brier(
                committed_traj, shadow_traj, resolved_hypothesis, h_keys, priors=priors)
        else:
            brier_report = None
    except Exception as exc:
        shadow_error = str(exc)

    # Flip status to RESOLVED on the topic dict (caller saves).
    topic["meta"]["status"] = "RESOLVED"
    topic["meta"]["resolvedAt"] = _now_iso()
    topic["meta"]["resolvedHypothesis"] = resolved_hypothesis

    packet: dict[str, Any] = {
        "slug": slug,
        "resolved_hypothesis": resolved_hypothesis,
        "resolved_label": hypotheses[resolved_hypothesis].get("label", ""),
        "status": "RESOLVED",
        "resolution_timestamp": _now_iso(),
        "outcome": outcome,
        "two_lane_brier": brier_report,
        "shadow_error": shadow_error,
    }

    # Optional AAR: red-team over evidence + scans + Brier divergence.
    if not skip_aar:
        packet["red_team_aar"] = _red_team_after_action(
            slug, resolved_hypothesis, hypotheses[resolved_hypothesis].get("label", ""),
            brier_report or {}, _evidence_summary(topic), _scan_summary(scan_digests or []),
            llama_client=llama_client, model=model, timeout_sec=timeout_sec,
        )

    return {"packet": packet, "topic": topic}


def run_resolution_brier(
    repo_root: Path, slug: str, asof: str = "",
) -> dict:
    """Recompute two-lane Brier for an already-RESOLVED topic. Read-only —
    never mutates topic state. Useful for post-hoc calibration review."""
    import importlib
    engine = importlib.import_module("engine")
    topic = engine.load_topic(slug)
    if (topic.get("meta", {}) or {}).get("status") != "RESOLVED":
        raise ValueError(f"Topic {slug!r} is not RESOLVED; resolve it first.")
    outcomes = (topic.get("predictionScoring") or {}).get("outcomes") or []
    # Pick the last outcome that is an actual resolution (carries "resolved"),
    # not a PARTIAL_EXPIRY entry (which "expired" a hypothesis but didn't
    # resolve the topic). save_topic's check_expired_hypotheses path can
    # append PARTIAL_EXPIRY entries after the resolution outcome.
    resolution_outcomes = [o for o in outcomes if o.get("resolved")]
    if not resolution_outcomes:
        raise ValueError(f"Topic {slug!r} has no recorded resolution outcome to score against.")
    resolved_hypothesis = resolution_outcomes[-1].get("resolved")
    h_keys = list((topic.get("model", {}).get("hypotheses") or {}).keys())
    committed_traj = _committed_trajectory(topic)
    priors = committed_traj[0]["posteriors"] if committed_traj else None
    # If asof is given, reconstruct the shadow trajectory only up to that date.
    if asof:
        committed_traj = [r for r in committed_traj if r["date"] <= asof]
    shadow_traj = reconstruct_shadow_trajectory(
        repo_root, slug, [r["date"] for r in committed_traj], h_keys)
    return compute_two_lane_brier(
        committed_traj, shadow_traj, resolved_hypothesis, h_keys, priors=priors)
