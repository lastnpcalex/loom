"""One-shot cleanup: drop summary.topic from the activity snapshot.

The persisted activity snapshot grew to ~33 MB because submit_transition /
commit_match with include_topic=True embedded the full topic object into
summary.topic. The compact topic_summary is already stored alongside it, so
the full topic is redundant in the dashboard view and is preserved verbatim
in the audit log (activity.jsonl).

This script trims the on-disk snapshot in place (with a .bak-pretrim backup)
and applies the per-job budget backstop so no job can re-bloat it. Idempotent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from mcp_servers.nrol_ao.activity import (
    _MAX_SNAPSHOT_JOBS,
    _clip,
    _enforce_snapshot_budget_inplace,
    _snapshot_summary,
)

SNAPSHOT = Path(r"C:\Claude-Code\NROL-AO\temp-repo\loom\mcp_activity\snapshot.json")


def main(snap: Path = SNAPSHOT) -> None:
    backup = snap.with_suffix(".json.bak-pretrim")
    if not snap.exists():
        print(f"snapshot not found: {snap}")
        return
    if not backup.exists():
        shutil.copy2(snap, backup)
        print(f"backup -> {backup}  ({backup.stat().st_size // 1024} KB)")
    else:
        print(f"backup already exists -> {backup}")

    before = snap.stat().st_size
    data = json.loads(snap.read_text(encoding="utf-8"))
    jobs = data.get("jobs", [])
    print(f"jobs before: {len(jobs)}")

    dropped_topic = 0
    budget_stubbed = 0
    for j in jobs:
        if isinstance(j.get("summary"), dict) and "topic" in j["summary"]:
            dropped_topic += 1
            j["summary"] = _snapshot_summary(j["summary"])
        if isinstance(j.get("summary"), dict):
            j["summary"] = _clip(j["summary"])
        _enforce_snapshot_budget_inplace(j)
        summary = j.get("summary")
        if isinstance(summary, dict) and summary.get("_truncated"):
            budget_stubbed += 1

    jobs = sorted(jobs, key=lambda j: j.get("updated_at", ""), reverse=True)[:_MAX_SNAPSHOT_JOBS]
    data["jobs"] = jobs

    tmp = snap.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
    tmp.replace(snap)
    after = snap.stat().st_size
    print(f"dropped summary.topic from {dropped_topic} job(s)")
    print(f"budget-stubbed: {budget_stubbed}")
    print(f"snapshot: {before // 1024} KB -> {after // 1024} KB")


if __name__ == "__main__":
    main()
