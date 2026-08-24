"""Provider-independent recovery snapshots for agent workspaces.

Snapshots are intentionally outside the project checkout. They preserve the
live files an agent inherited (including uncommitted and untracked files), then
produce a post-turn report without mutating the workspace or Git index.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


MAX_RECOVERY_FILE_BYTES = 8 * 1024 * 1024
MAX_RECOVERY_TOTAL_BYTES = 256 * 1024 * 1024
MAX_REPORTED_FILES = 500
_FALLBACK_EXCLUDES = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "target",
}


@dataclass(frozen=True)
class WorkspaceSnapshot:
    snapshot_id: str
    root: str
    manifest_path: str


def default_recovery_root() -> Path:
    """Return persistent recovery storage that is never inside the checkout by default."""
    override = os.environ.get("LOOM_WORKSPACE_RECOVERY_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).expanduser().resolve() / "A Shadow Loom" / "workspace-recovery"
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if state_home:
        return Path(state_home).expanduser().resolve() / "a-shadow-loom" / "workspace-recovery"
    return Path.home().resolve() / ".local" / "state" / "a-shadow-loom" / "workspace-recovery"


def _git_bytes(root: Path, args: list[str]) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _decode_nul_paths(payload: bytes | None) -> set[str]:
    if payload is None:
        return set()
    return {
        part.decode("utf-8", errors="replace").replace("\\", "/")
        for part in payload.split(b"\0")
        if part
    }


def _fallback_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _FALLBACK_EXCLUDES]
        base = Path(current)
        for name in files:
            path = base / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                paths.add(path.relative_to(root).as_posix())
            except (OSError, ValueError):
                continue
    return paths


def _workspace_paths(root: Path) -> tuple[set[str], set[str], bool, str | None]:
    top_level_raw = _git_bytes(root, ["rev-parse", "--show-toplevel"])
    if top_level_raw is None:
        return _fallback_paths(root), set(), False, None
    try:
        top_level = Path(
            top_level_raw.decode("utf-8", errors="replace").strip()
        ).resolve()
    except (OSError, ValueError):
        return _fallback_paths(root), set(), False, None
    if top_level != root:
        # A directory nested beneath some unrelated parent checkout is its own
        # workspace for snapshot purposes. Inheriting the parent's ignore rules
        # can otherwise make every live file disappear from the snapshot.
        return _fallback_paths(root), set(), False, None

    tracked_and_untracked = _git_bytes(
        root, ["ls-files", "-co", "--exclude-standard", "-z"]
    )
    if tracked_and_untracked is None:
        return _fallback_paths(root), set(), False, None

    paths = _decode_nul_paths(tracked_and_untracked)
    dirty = set()
    dirty.update(_decode_nul_paths(_git_bytes(root, ["diff", "--name-only", "--no-renames", "-z"])))
    dirty.update(_decode_nul_paths(_git_bytes(root, ["diff", "--cached", "--name-only", "--no-renames", "-z"])))
    dirty.update(_decode_nul_paths(_git_bytes(root, ["ls-files", "--others", "--exclude-standard", "-z"])))
    head_raw = _git_bytes(root, ["rev-parse", "HEAD"])
    head = head_raw.decode("ascii", errors="replace").strip() if head_raw else None
    return paths, dirty, True, head


def _safe_file(root: Path, relative: str) -> Path | None:
    try:
        raw_candidate = root / relative
        if raw_candidate.is_symlink():
            return None
        candidate = raw_candidate.resolve()
        candidate.relative_to(root)
        if not candidate.is_file():
            return None
        return candidate
    except (OSError, ValueError):
        return None


def _read_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _is_text(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _store_blob(blob_root: Path, digest: str, data: bytes) -> str:
    blob = blob_root / digest[:2] / digest
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        temp = blob.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
        temp.write_bytes(data)
        try:
            os.replace(temp, blob)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return str(blob)


def capture_workspace_snapshot(
    workspace: str | Path,
    recovery_root: str | Path,
    *,
    conversation_id: int,
    generation_id: int | None,
) -> WorkspaceSnapshot | None:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return None

    paths, dirty, is_git, head = _workspace_paths(root)
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_id = f"{stamp}-c{conversation_id}-g{generation_id or 0}-{uuid.uuid4().hex[:8]}"
    recovery_root = Path(recovery_root).expanduser().resolve()
    snapshot_dir = recovery_root / "snapshots" / f"conversation-{conversation_id}" / snapshot_id
    blob_root = recovery_root / "blobs"

    stored_bytes = 0
    skipped: list[dict] = []
    files: dict[str, dict] = {}
    for relative in sorted(paths):
        path = _safe_file(root, relative)
        if path is None:
            continue
        data = _read_file(path)
        if data is None:
            skipped.append({"path": relative, "reason": "unreadable"})
            continue
        digest = hashlib.sha256(data).hexdigest()
        entry = {
            "sha256": digest,
            "size": len(data),
            "dirty_before": relative in dirty,
            "text": _is_text(data),
            "recovery_blob": None,
        }
        if len(data) > MAX_RECOVERY_FILE_BYTES:
            skipped.append({"path": relative, "reason": "file_too_large", "size": len(data)})
        elif stored_bytes + len(data) > MAX_RECOVERY_TOTAL_BYTES:
            skipped.append({"path": relative, "reason": "snapshot_cap", "size": len(data)})
        else:
            entry["recovery_blob"] = _store_blob(blob_root, digest, data)
            stored_bytes += len(data)
        files[relative] = entry

    manifest = {
        "version": 1,
        "snapshot_id": snapshot_id,
        "workspace": str(root),
        "conversation_id": conversation_id,
        "generation_id": generation_id,
        "created_at": now,
        "git": is_git,
        "git_head": head,
        "files": files,
        "skipped": skipped,
        "stored_bytes": stored_bytes,
        "blob_root": str(blob_root),
        "restore_note": (
            "Copy a recovery_blob back to workspace/path only after reviewing the live file. "
            "Snapshots never restore files automatically."
        ),
    }
    manifest_path = snapshot_dir / "manifest.json"
    _write_json_atomic(manifest_path, manifest)
    return WorkspaceSnapshot(snapshot_id, str(root), str(manifest_path))


def _line_delta(before: bytes, after: bytes) -> tuple[int, int] | tuple[None, None]:
    if not _is_text(before) or not _is_text(after):
        return None, None
    before_lines = before.decode("utf-8").splitlines()
    after_lines = after.decode("utf-8").splitlines()
    added = 0
    removed = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return added, removed


def finalize_workspace_snapshot(snapshot: WorkspaceSnapshot) -> dict:
    manifest_path = Path(snapshot.manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(manifest["workspace"]).resolve()
    current_paths, _, _, current_head = _workspace_paths(root)
    before_files: dict[str, dict] = manifest.get("files", {})
    all_paths = sorted(set(before_files) | current_paths)
    changed: list[dict] = []
    total_added = 0
    total_removed = 0
    deleted = 0
    preexisting_changed = 0
    stored_after_bytes = 0
    blob_root = Path(manifest.get("blob_root") or manifest_path.parents[3] / "blobs")

    for relative in all_paths:
        before = before_files.get(relative)
        current_path = _safe_file(root, relative)
        after_data = _read_file(current_path) if current_path else None
        after_hash = hashlib.sha256(after_data).hexdigest() if after_data is not None else None
        if before and before.get("sha256") == after_hash:
            continue
        if before is None:
            status = "created"
        elif after_data is None:
            status = "deleted"
            deleted += 1
        else:
            status = "modified"

        before_data = b""
        blob = before.get("recovery_blob") if before else None
        if blob:
            before_data = _read_file(Path(blob)) or b""
        added, removed = _line_delta(before_data, after_data or b"") if (blob or before is None) else (None, None)
        if added is not None:
            total_added += added
        if removed is not None:
            total_removed += removed
        dirty_before = bool(before and before.get("dirty_before"))
        if dirty_before:
            preexisting_changed += 1
        after_recovery_blob = None
        if (
            after_data is not None
            and after_hash
            and len(after_data) <= MAX_RECOVERY_FILE_BYTES
            and stored_after_bytes + len(after_data) <= MAX_RECOVERY_TOTAL_BYTES
        ):
            after_recovery_blob = _store_blob(blob_root, after_hash, after_data)
            stored_after_bytes += len(after_data)
        changed.append(
            {
                "path": relative,
                "status": status,
                "dirty_before": dirty_before,
                "before_sha256": before.get("sha256") if before else None,
                "after_sha256": after_hash,
                "added_lines": added,
                "removed_lines": removed,
                "recovery_blob": blob,
                "after_recovery_blob": after_recovery_blob,
            }
        )

    warnings: list[str] = []
    if deleted:
        warnings.append(f"{deleted} file(s) were deleted")
    if preexisting_changed:
        warnings.append(f"{preexisting_changed} file(s) that already had live changes were modified again")
    if total_removed >= 25:
        warnings.append(f"{total_removed} lines were removed across text files")
    if manifest.get("git_head") and current_head and manifest["git_head"] != current_head:
        warnings.append("Git HEAD changed during the agent turn")

    report = {
        "type": "workspace_change_report",
        "snapshot_id": snapshot.snapshot_id,
        "workspace": str(root),
        "manifest_path": str(manifest_path),
        "changed_count": len(changed),
        "created_count": sum(1 for item in changed if item["status"] == "created"),
        "modified_count": sum(1 for item in changed if item["status"] == "modified"),
        "deleted_count": deleted,
        "added_lines": total_added,
        "removed_lines": total_removed,
        "stored_after_bytes": stored_after_bytes,
        "warnings": warnings,
        "changed_files": changed[:MAX_REPORTED_FILES],
        "truncated": len(changed) > MAX_REPORTED_FILES,
    }
    _write_json_atomic(manifest_path.parent / "change-report.json", report)
    return report
