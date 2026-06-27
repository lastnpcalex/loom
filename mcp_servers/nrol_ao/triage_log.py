"""Triage audit ledger — durable record of triage decisions, outside topic state.

Triage is LIVE (framework/triage.py + the triage_headline MCP tool + the
dashboard /triage endpoint). This module adds an OPTIONAL audit ledger so
triage decisions are reviewable: a triage result can be appended to
loom/triage_log.jsonl, and prior entries listed/read back.

The ledger is NOT evidence. It never moves posteriors, never writes to topic
JSON, evidenceLog, or sourceCalibration. A logged triage is an audit aid,
not a typed transition — promotion to real action still goes through
submit_transition / propose_match -> commit_match.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_TRIAGE_LOG_DIRNAME = "triage_log"
_TRIAGE_LOG_FILE = "triage_log.jsonl"


def _log_path(repo_root: Path) -> Path:
    # Mirror the engine's loom/ state layout; sits beside mcp_activity/.
    return repo_root / "loom" / _TRIAGE_LOG_DIRNAME / _TRIAGE_LOG_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_triage(repo_root: Path, triage_result: dict, note: str = "") -> dict:
    """Append a triage result to the audit ledger. Returns the record (with
    its triage_id). Never evidence; never moves posteriors."""
    path = _log_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    import hashlib
    h = hashlib.sha256(
        (str(triage_result.get("headline", "")) + "|" +
         str(triage_result.get("timestamp", _now_iso()))).encode("utf-8")
    ).hexdigest()[:12]
    record = {
        "triage_id": "triage_" + h,
        "logged_at": _now_iso(),
        "logged_by": os.environ.get("LOOM_CONV_ID", "headless"),
        "headline": triage_result.get("headline", ""),
        "source": triage_result.get("source"),
        "triage_timestamp": triage_result.get("timestamp"),
        "top_action": triage_result.get("top_action"),
        "summary": triage_result.get("summary", ""),
        "match_count": len(triage_result.get("matches") or []),
        "matches": triage_result.get("matches") or [],
        "note": note,
        "promoted_to_real_action": False,
        "promoted_proposal_id": None,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def list_triage(repo_root: Path, slug: str = "", limit: int = 25) -> dict:
    """List recent triage ledger entries. Optional slug filters matches by topic.
    Read-only."""
    path = _log_path(repo_root)
    if not path.exists():
        return {"count": 0, "entries": [], "path": str(path)}
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if slug:
                # Keep only entries whose matches touch this slug.
                if not any(m.get("slug") == slug for m in rec.get("matches") or []):
                    continue
            rows.append(rec)
    rows.reverse()  # newest first
    rows = rows[: max(1, min(int(limit), 200))]
    # Compact the match list in the summary view to keep payloads bounded.
    for r in rows:
        r["matches_brief"] = [
            {"slug": m.get("slug"), "relevance": m.get("relevance"),
             "action": m.get("action")} for m in (r.pop("matches", []) or [])
        ]
    return {"count": len(rows), "entries": rows, "path": str(path)}


def read_triage(repo_root: Path, triage_id: str) -> dict:
    """Read one triage ledger entry by id (full matches). Read-only."""
    path = _log_path(repo_root)
    if not path.exists():
        return {"error": "no triage ledger yet"}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("triage_id") == triage_id:
                return rec
    return {"error": f"triage_id {triage_id!r} not found"}
