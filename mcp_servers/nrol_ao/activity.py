"""Activity ledger for NROL-AO MCP jobs.

The dashboard can monitor the compact JSON snapshot. Full prompts/responses are
kept in the JSONL log for audit without forcing the panel to load large blobs.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX_SNAPSHOT_JOBS = 100
_MAX_INLINE_CHARS = 2000
# Per-job ceiling for the compact snapshot. The full event (including any
# large summary) is preserved in the audit log (activity.jsonl); the snapshot
# is the dashboard view and must stay small enough to load and return whole.
_SNAPSHOT_JOB_BUDGET_BYTES = 64 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_activity_dir(repo_root: Path) -> Path:
    return repo_root / "loom" / "mcp_activity"


def _clip(value: Any, limit: int = _MAX_INLINE_CHARS) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\n[truncated {len(value) - limit} chars]"
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value]
    return value


def _snapshot_summary(summary: Any) -> Any:
    """Prepare a summary for snapshot persistence.

    The full topic object (summary.topic) is kept verbatim in the audit log
    and in the tool's return value, but it can run to megabytes. The compact
    topic_summary is already present alongside it, so drop the full topic here.
    """
    if isinstance(summary, dict):
        return {k: v for k, v in summary.items() if k != "topic"}
    return summary


def _enforce_snapshot_budget(compact: dict) -> dict:
    """Return a budget-compliant copy of a compact job (used by callers that
    build fresh dicts). The full event is preserved in the audit log."""
    try:
        size = len(json.dumps(compact, ensure_ascii=True, default=str))
    except Exception:
        return compact
    if size <= _SNAPSHOT_JOB_BUDGET_BYTES:
        return compact
    out = dict(compact)
    if "summary" in out:
        out["summary"] = {
            "_truncated": "summary dropped to keep snapshot under budget",
            "_original_bytes": size,
        }
    return out


def _enforce_snapshot_budget_inplace(job: dict) -> None:
    """Mutate a stored job dict in place if it exceeds the byte budget.

    Used on the existing-job update path, where update() may have carried a
    prior bloated summary forward. The full event is in the audit log.
    """
    try:
        size = len(json.dumps(job, ensure_ascii=True, default=str))
    except Exception:
        return
    if size <= _SNAPSHOT_JOB_BUDGET_BYTES:
        return
    if "summary" in job:
        job["summary"] = {
            "_truncated": "summary dropped to keep snapshot under budget",
            "_original_bytes": size,
        }


class ActivityStore:
    def __init__(self, root: Path):
        self.root = root
        self.log_path = root / "activity.jsonl"
        self.snapshot_path = root / "snapshot.json"

    def _read_snapshot(self) -> dict:
        if not self.snapshot_path.exists():
            return {"updated_at": None, "jobs": []}
        try:
            return json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            return {"updated_at": None, "jobs": []}

    def _write_snapshot(self, snapshot: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=True), encoding="utf-8")
        tmp.replace(self.snapshot_path)

    def _append_log(self, event: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=True, default=str) + "\n")

    def record(self, job_id: str, status: str, **fields: Any) -> dict:
        now = utc_now()
        event = {
            "job_id": job_id,
            "status": status,
            "time": now,
            **fields,
        }
        self._append_log(event)

        snapshot = self._read_snapshot()
        jobs = snapshot.get("jobs", [])
        existing = next((j for j in jobs if j.get("job_id") == job_id), None)
        compact = {
            "job_id": job_id,
            "status": status,
            "updated_at": now,
            "task": fields.get("task"),
            "slug": fields.get("slug"),
            "model": fields.get("model"),
            "transition": fields.get("transition"),
            "duration_ms": fields.get("duration_ms"),
            "error": fields.get("error"),
            "summary": _clip(_snapshot_summary(fields.get("summary"))),
        }
        compact = {k: v for k, v in compact.items() if v is not None}
        if existing:
            existing.update(compact)
            if status == "running" and "started_at" not in existing:
                existing["started_at"] = now
            if status in {"completed", "failed", "denied"}:
                existing["finished_at"] = now
            job = existing
        else:
            if status in {"queued", "running"}:
                compact["started_at"] = now if status == "running" else None
            jobs.insert(0, compact)
            job = compact
        # Backstop: keep the snapshot loadable even if a summary still bloats
        # a job past the per-job budget (full event is in the audit log).
        _enforce_snapshot_budget_inplace(job)
        jobs = sorted(jobs, key=lambda j: j.get("updated_at", ""), reverse=True)[:_MAX_SNAPSHOT_JOBS]
        snapshot = {
            "updated_at": now,
            "active": sum(1 for j in jobs if j.get("status") in {"queued", "running"}),
            "jobs": jobs,
        }
        self._write_snapshot(snapshot)
        return event

    def list_jobs(self, limit: int = 20) -> dict:
        snapshot = self._read_snapshot()
        jobs = snapshot.get("jobs", [])
        try:
            limit = max(1, min(int(limit), _MAX_SNAPSHOT_JOBS))
        except Exception:
            limit = 20
        snapshot["jobs"] = jobs[:limit]
        return snapshot


def new_job_id(prefix: str = "nrol") -> str:
    return f"{prefix}-{int(time.time() * 1000)}"

