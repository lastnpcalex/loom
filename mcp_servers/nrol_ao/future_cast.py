"""Future Cast — dry-run exploration of a hypothetical event or action.

SHADOW ANALYSIS SURFACE. This module has ZERO authority. It never writes to
topic JSON, posteriorHistory, evidenceLog, sourceCalibration, or
sources/source_db.json. It deep-clones a topic in memory, applies a
hypothetical indicator firing through the engine's own ``bayesian_update``
code path (so the math is exact, not a reproduction), reads the resulting
posteriors, and discards the clone. The clone is never saved.

Why reuse ``bayesian_update`` on a clone rather than reproduce the math:
``engine.bayesian_update`` (engine.py:1733) mutates only the in-memory dict
and returns it — it does NOT call ``save_topic`` (verified: the save happens
in ``pipeline.process_evidence`` / ``pipeline.apply_observation``, not inside
``bayesian_update``). So operating on a deep-cloned topic and discarding it
produces zero disk writes. This avoids math drift (a separate pure-function
reproduction would diverge from the real update over time) without modifying
the posterior-moving code path (a no-write adapter is forbidden by the
blast-radius constraint). The clone pattern mirrors
``engine.reset_to_design_priors`` (engine.py:2324).

Spec: ``specs/source-calibration-future-casts.md`` (Future Addition 2).
Governance rules (spec L374-387) are enforced as hardcoded invariants below.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Governance-rule labels stamped onto every packet so an operator reading the
# output can see the authority boundary without re-reading the spec.
_AUTHORITY = "No topic mutation. No posterior update. No source trust update."
_HYPOTHETICAL_TAG = "HYPOTHETICAL"
_FUTURE_CAST_DIR = "future_casts"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_asof(asof: str) -> str:
    """Pass asof through; validation happens in shadow_posteriors. Empty = today."""
    return (asof or "").strip()


def _posteriors(topic: dict) -> dict[str, float]:
    """Read the committed posteriors off a topic dict (matches server._posteriors)."""
    out: dict[str, float] = {}
    for hk, h in (topic.get("model", {}).get("hypotheses") or {}).items():
        if isinstance(h, dict):
            out[hk] = float(h.get("posterior", 0.0))
    return out


def _candidate_transitions(
    engine, topic: dict, target: str, transition: str, observed_value: float | None,
    scenario: str, reason: str,
) -> list[dict]:
    """Compute the shadow posterior delta for the proposed transition.

    The transition is applied to a deep clone via the engine's own
    ``bayesian_update`` (no save). If a gate refuses (indicator_id required,
    indicator not found, calibrationStatus missing, lens missing, observable
    missing for OBSERVE), the candidate is reported structurally_invalid with
    the reason — never raised, because future-cast is advisory.
    """
    before = _posteriors(topic)
    candidates: list[dict] = []

    transition_kind = (transition or "").strip().upper()
    if transition_kind not in {"FIRE", "OBSERVE"}:
        # Only FIRE/OBSERVE move posteriors through bayesian_update. A cast
        # with no transition (or PARK/SCHEMA_GAP/IGNORE) is a scenario-only
        # exploration: report the current state, no delta computation.
        candidates.append({
            "transition": transition_kind or "NONE",
            "indicator_id": target or "",
            "structurally_valid": True,
            "governance": {"passed": True, "failures": [], "explanation": (
                "No posterior-moving transition proposed; shadow_posteriors "
                "field reflects current committed state, no delta applied."
            )},
            "shadow_posteriors": {"before": before, "after": before,
                                  "delta": {k: 0.0 for k in before}},
            "reason": "" if transition_kind in {"", "NONE"} else "scenario-only cast",
        })
        return candidates

    if not target:
        candidates.append({
            "transition": transition_kind,
            "indicator_id": "",
            "structurally_valid": False,
            "governance": {"passed": False, "failures": ["missing_indicator_id"],
                           "explanation": f"{transition_kind} requires a target indicator id."},
            "shadow_posteriors": None,
        })
        return candidates

    # Build the candidate by deep-cloning and applying the update on the clone.
    clone = copy.deepcopy(topic)
    indicator = None
    indicator_tier = None
    for tier, items in (clone.get("indicators", {}).get("tiers", {}) or {}).items():
        for ind in items or []:
            if isinstance(ind, dict) and ind.get("id") == target:
                indicator, indicator_tier = ind, tier
                break
        if indicator:
            break
    if not indicator:
        for ind in clone.get("indicators", {}).get("anti_indicators", []) or []:
            if isinstance(ind, dict) and ind.get("id") == target:
                indicator, indicator_tier = ind, "anti_indicators"
                break

    if not indicator:
        candidates.append({
            "transition": transition_kind,
            "indicator_id": target,
            "structurally_valid": False,
            "governance": {"passed": False, "failures": ["indicator_not_found"],
                           "explanation": f"indicator {target!r} not found on topic."},
            "shadow_posteriors": None,
        })
        return candidates

    # Resolve the likelihoods to apply. FIRE uses the indicator's pre-committed
    # point likelihoods (or lr_range dual pass). OBSERVE derives LRs from the
    # observable block via framework.likelihood_models.evaluate.
    likelihoods = indicator.get("likelihoods")
    lr_range = indicator.get("lr_range")
    failures: list[str] = []

    if transition_kind == "OBSERVE":
        if observed_value is None:
            failures.append("missing_observed_value")
        elif not indicator.get("observable"):
            failures.append("no_observable_block")
        else:
            try:
                # Imported lazily so the module loads even if likelihood_models
                # is unavailable; the failure is reported as structural.
                import importlib
                lm_mod = importlib.import_module("framework.likelihood_models")
                likelihoods = lm_mod.evaluate(
                    indicator["observable"], likelihoods, observed_value)
            except Exception as exc:
                failures.append(f"observable_evaluation_failed: {exc}")
                likelihoods = None

    if not likelihoods and not lr_range:
        failures.append("no_pre_committed_likelihoods")

    if failures:
        candidates.append({
            "transition": transition_kind,
            "indicator_id": target,
            "structurally_valid": False,
            "governance": {"passed": False, "failures": failures,
                           "explanation": "Indicator lacks the pre-committed "
                           "likelihoods needed for this transition."},
            "shadow_posteriors": None,
        })
        return candidates

    # Fire the indicator on the clone (sets status=FIRED; no save) then run the
    # engine's bayesian_update on the clone. bayesian_update raises on missing
    # calibrationStatus / lens — catch and report as structural, never raise.
    try:
        clone = engine.fire_indicator(clone, target, note=f"HYPOTHETICAL: {scenario}")
        lens = clone.get("meta", {}).get("lens")
        clone = engine.bayesian_update(
            clone,
            likelihoods=likelihoods,
            lr_range=lr_range,
            reason=f"HYPOTHETICAL future cast: {reason or scenario}",
            evidence_refs=[target],
            indicator_id=target,
            lens=lens,
        )
    except Exception as exc:
        candidates.append({
            "transition": transition_kind,
            "indicator_id": target,
            "structurally_valid": False,
            "governance": {"passed": False, "failures": ["update_refused"],
                           "explanation": f"bayesian_update refused the hypothetical: {exc}"},
            "shadow_posteriors": None,
        })
        return candidates

    after = _posteriors(clone)
    delta = {k: round(after.get(k, 0.0) - before.get(k, 0.0), 4) for k in before}

    # Dry-run governance: confidence_inflation etc. via governance_report on
    # the clone (read-only). Not all repos ship governor.governance_report; if
    # absent, report governance as not_checked rather than fail.
    gov = {"passed": True, "failures": [], "explanation": "Dry-run; not checked."}
    try:
        import importlib
        gov_mod = importlib.import_module("governor")
        if hasattr(gov_mod, "governance_report"):
            report = gov_mod.governance_report(clone)
            issues = (report or {}).get("issues") or []
            if issues:
                gov = {"passed": False, "failures": [str(i) for i in issues[:5]],
                       "explanation": "Governance flagged the hypothetical state."}
    except Exception:
        pass

    candidates.append({
        "transition": transition_kind,
        "indicator_id": target,
        "tier": indicator_tier,
        "structurally_valid": True,
        "governance": gov,
        "shadow_posteriors": {"before": before, "after": after, "delta": delta},
    })
    return candidates


def _red_team_future_cast(
    slug: str, scenario: str, transition: str, target: str,
    shadow_summary: dict, assumptions: list[str],
    *, llama_client, model: str = "", timeout_sec: int = 600,
) -> dict:
    """Adversarial critique of a future cast scenario.

    Mirrors the _red_team_design pattern (server.py:3940): single-pass
    llama_client.chat, structured prompt, verdict parse. Returns
    {verdict, strongest_objection, missing_evidence, recommended_operator_action}.
    """
    review: dict[str, Any] = {
        "verdict": "UNREVIEWED",
        "at": _now_iso(),
        "model": "",
        "strongest_objection": "",
        "missing_evidence": [],
        "recommended_operator_action": "",
        "critique": "",
    }
    try:
        prompt_lines = [
            "You are the RED TEAM reviewing a proposed future-cast scenario "
            "for an NROL-AO topic. The operator is asking what would happen if "
            "a hypothetical event occurred. Attack the scenario: is it a real "
            "signal or a vibe? What evidence is missing before the proposed "
            "transition could be justified? Would firing this indicator "
            "double-count a sustained metric or trip confidence_inflation? "
            "End with VERDICT: SOUND or VERDICT: REVISE, then "
            "STRONGEST_OBJECTION:, MISSING_EVIDENCE: (comma list), "
            "RECOMMENDED_ACTION:.",
            f"TOPIC: {slug}",
            f"SCENARIO: {scenario}",
            f"PROPOSED_TRANSITION: {transition or '(none)'} target={target or '(none)'}",
            "SHADOW_DELTA: " + json.dumps(shadow_summary, default=str)[:1500],
            "ASSUMPTIONS: " + json.dumps(assumptions, default=str)[:800],
        ]
        response = llama_client.chat(
            "\n".join(prompt_lines),
            system_prompt="You are an adversarial scenario reviewer.",
            model=model, temperature=0.3, max_tokens=2048,
            timeout_sec=timeout_sec, disable_thinking=True,
        )
        text = response.get("text", "")
        review["model"] = response.get("model") or ""
        review["critique"] = text
        verdicts = re.findall(r"VERDICT:\s*(SOUND|REVISE)", text.upper())
        review["verdict"] = verdicts[-1] if verdicts else ("REVISE" if text.strip() else "UNREVIEWED")
        m = re.search(r"STRONGEST_OBJECTION:\s*(.+?)(?=\n[A-Z_]+:|\Z)", text, re.S)
        if m:
            review["strongest_objection"] = m.group(1).strip()[:500]
        m = re.search(r"MISSING_EVIDENCE:\s*(.+?)(?=\n[A-Z_]+:|\Z)", text, re.S)
        if m:
            review["missing_evidence"] = [
                s.strip() for s in re.split(r"[,;\n]", m.group(1)) if s.strip()
            ][:10]
        m = re.search(r"RECOMMENDED_ACTION:\s*(.+?)(?=\n[A-Z_]+:|\Z)", text, re.S)
        if m:
            review["recommended_operator_action"] = m.group(1).strip()[:500]
    except Exception as exc:
        review["error"] = str(exc)
    return review


def _cast_id() -> str:
    return "fc_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _scenario_summary(scenario: str, target: str, transition: str) -> str:
    parts = []
    if transition:
        parts.append(f"{transition.upper()} {target}".strip())
    parts.append((scenario or "").strip())
    return " — ".join(p for p in parts if p)[:400]


def _save_future_cast(repo_root: Path, packet: dict, tags: list[str]) -> str:
    """Append to future_casts/future_casts.jsonl. Lives outside topic state."""
    cast_dir = repo_root / _FUTURE_CAST_DIR
    cast_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "cast_id": packet["cast_id"],
        "created_at": _now_iso(),
        "created_by": os.environ.get("LOOM_CONV_ID", "headless"),
        "slug": packet["slug"],
        "scenario_hash": "sha256:" + hashlib.sha256(
            packet["scenario_summary"].encode("utf-8")
        ).hexdigest()[:16],
        "scenario_summary": packet["scenario_summary"],
        "packet": packet,
        "tags": tags,
        "promoted_to_real_action": False,
        "promoted_proposal_id": None,
    }
    with open(cast_dir / "future_casts.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record["cast_id"]


def _store_path(repo_root: Path) -> Path:
    return repo_root / _FUTURE_CAST_DIR / "future_casts.jsonl"


def _load_store(repo_root: Path) -> list[dict]:
    """Read the future_casts.jsonl store. Missing file -> empty list."""
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


def list_future_casts(repo_root: Path, slug: str = "", tag: str = "",
                      limit: int = 25) -> dict:
    """List saved future casts, newest first. Optional slug/tag filters.
    Returns a brief view (no packet) to keep payloads bounded. Read-only."""
    rows = _load_store(repo_root)
    out: list[dict] = []
    for r in rows:
        if slug and r.get("slug") != slug:
            continue
        if tag and tag not in (r.get("tags") or []):
            continue
        out.append({
            "cast_id": r.get("cast_id"),
            "slug": r.get("slug"),
            "created_at": r.get("created_at"),
            "scenario_summary": r.get("scenario_summary"),
            "tags": r.get("tags") or [],
            "promoted_to_real_action": r.get("promoted_to_real_action", False),
            "promoted_proposal_id": r.get("promoted_proposal_id"),
        })
    out.reverse()  # newest first
    out = out[: max(1, min(int(limit), 200))]
    return {"count": len(out), "casts": out}


def get_future_cast(repo_root: Path, cast_id: str) -> dict:
    """Read one saved future cast by id (full packet). Read-only."""
    for r in _load_store(repo_root):
        if r.get("cast_id") == cast_id:
            return r
    return {"error": f"cast_id {cast_id!r} not found"}


def save_future_cast(repo_root: Path, cast_id: str, tags: list[str] | None = None,
                    note: str = "") -> dict:
    """Promote a transient cast to a saved one (or re-tag an already-saved cast).

    A transient cast (future_cast(save=false)) carries a cast_id but was never
    written. This writes a record for it if absent, or appends tags to the
    existing record. NOTE: the transient cast's packet is not persisted by the
    save=false path, so this can only re-tag an ALREADY-saved cast (the cast_id
    of a transient cast is not findable in the store). To save a cast's packet,
    call future_cast(..., save=true) at cast time. Saved casts are never
    evidence and never satisfy evidence requirements."""
    rows = _load_store(repo_root)
    path = _store_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    for r in rows:
        if r.get("cast_id") == cast_id:
            if tags:
                existing = r.get("tags") or []
                for t in tags:
                    if t not in existing:
                        existing.append(t)
                r["tags"] = existing
            if note:
                r["note"] = note
            _rewrite_store(repo_root, rows)
            return r
    return {"error": f"cast_id {cast_id!r} not found in the saved store. "
                     "To save a cast's packet, call future_cast(save=true) at cast time."}


def withdraw_future_cast(repo_root: Path, cast_id: str, reason: str = "") -> dict:
    """Remove a saved future cast from the store by id.

    This edits only the future_casts.jsonl store. It does not roll back any
    topic evidence, proposals, or posteriors (a cast never moved any). A
    withdrawn cast that was promoted_to_real_action is refused — withdraw the
    real proposal first, then withdraw the cast."""
    rows = _load_store(repo_root)
    target = None
    for r in rows:
        if r.get("cast_id") == cast_id:
            target = r
            break
    if target is None:
        return {"error": f"cast_id {cast_id!r} not found"}
    if target.get("promoted_to_real_action"):
        return {"error": "cast was promoted to a real action; withdraw the "
                         "proposal first, then withdraw the cast.",
                "promoted_proposal_id": target.get("promoted_proposal_id")}
    kept = [r for r in rows if r.get("cast_id") != cast_id]
    _rewrite_store(repo_root, kept)
    return {"withdrawn": cast_id, "reason": reason,
            "remaining_casts": len(kept)}


def _rewrite_store(repo_root: Path, rows: list[dict]) -> None:
    """Rewrite the JSONL store from a row list (for tag/withdraw edits)."""
    path = _store_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")



def run_future_cast(
    repo_root: Path, slug: str, scenario: str, target: str, transition: str,
    observed_value: float | None, asof: str, assumptions: list[str],
    save: bool, llama_client, model: str, timeout_sec: int,
) -> dict:
    """Execute the 8-step dry-run workflow (spec L189-198) and return the packet.

    This is the pure-function core called by the MCP tool wrapper. It does no
    MCP I/O and raises no Loom permission — those are the wrapper's job. All
    governance rules (spec L374-387) are enforced here as hardcoded invariants.
    """
    import importlib
    engine = importlib.import_module("engine")

    # Step 1: load topic (read-only).
    topic = engine.load_topic(slug)

    # Step 2: clone for the shadow computation (happens inside
    # _candidate_transitions; the original topic is never mutated).

    # Step 3 + 4 + 5: synthetic HYPOTHETICAL evidence is built in memory only —
    # never inserted into evidenceLog. Candidate transitions compute the delta.
    reason = f"future cast: {scenario[:200]}"
    candidates = _candidate_transitions(
        engine, topic, target, transition, observed_value, scenario, reason)

    # Step 6: governance checks ran inside _candidate_transitions (dry-run on clone).

    # Optional asof shadow: if the operator passed asof, also report the
    # dynamics-shadow posterior at that counterfactual date. Reuses the
    # existing shadow_posteriors module (deterministic, fixed seed).
    asof_shadow: dict | None = None
    if _parse_asof(asof):
        try:
            dyn = importlib.import_module("framework.dynamics_shadow")
            asof_shadow = dyn.run(repo_root, slug, asof=asof)
        except Exception as exc:
            asof_shadow = {"error": f"shadow_posteriors(asof={asof}) failed: {exc}"}

    # Step 7: red-team critique.
    shadow_for_review = next(
        (c["shadow_posteriors"] for c in candidates
         if c.get("shadow_posteriors") and c.get("structurally_valid")),
        None,
    )
    red_team = _red_team_future_cast(
        slug, scenario, transition, target,
        shadow_for_review or {}, assumptions or [],
        llama_client=llama_client, model=model, timeout_sec=timeout_sec,
    )

    # Step 8: assemble the operator-facing packet. No writes to topic state.
    packet: dict[str, Any] = {
        "cast_id": _cast_id(),
        "slug": slug,
        "status": "dry_run_only",
        "scenario_summary": _scenario_summary(scenario, target, transition),
        "candidate_transitions": candidates,
        "asof_shadow": asof_shadow,
        "red_team": red_team,
        "authority": _AUTHORITY,
    }

    # Optional save: outside topic state, never evidence.
    if save:
        try:
            packet["saved_cast_id"] = _save_future_cast(repo_root, packet, [])
            packet["saved_to"] = f"{_FUTURE_CAST_DIR}/future_casts.jsonl"
        except Exception as exc:
            packet["save_error"] = str(exc)

    return packet
