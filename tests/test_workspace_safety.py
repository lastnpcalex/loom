"""Recovery snapshot and workspace-change audit tests."""

from pathlib import Path

from workspace_safety import (
    capture_workspace_snapshot,
    default_recovery_root,
    finalize_workspace_snapshot,
)


def test_default_recovery_root_honors_external_override(monkeypatch, tmp_path):
    recovery = tmp_path / "outside" / "recovery"
    monkeypatch.setenv("LOOM_WORKSPACE_RECOVERY_DIR", str(recovery))
    assert default_recovery_root() == recovery.resolve()


def test_workspace_snapshot_recovers_preexisting_files_and_reports_churn(tmp_path):
    workspace = tmp_path / "workspace"
    recovery = tmp_path / "recovery"
    workspace.mkdir()
    feature = workspace / "feature.py"
    feature.write_text("one\ntwo\nthree\n", encoding="utf-8")
    keep = workspace / "keep.txt"
    keep.write_text("keep\n", encoding="utf-8")

    snapshot = capture_workspace_snapshot(
        workspace,
        recovery,
        conversation_id=7,
        generation_id=11,
    )
    assert snapshot is not None

    feature.unlink()
    keep.write_text("keep\nchanged\n", encoding="utf-8")
    (workspace / "new.txt").write_text("new\n", encoding="utf-8")
    report = finalize_workspace_snapshot(snapshot)

    assert report["changed_count"] == 3
    assert report["deleted_count"] == 1
    assert any("deleted" in warning for warning in report["warnings"])
    deleted = next(item for item in report["changed_files"] if item["path"] == "feature.py")
    assert deleted["status"] == "deleted"
    assert Path(deleted["recovery_blob"]).read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    modified = next(item for item in report["changed_files"] if item["path"] == "keep.txt")
    assert Path(modified["after_recovery_blob"]).read_text(encoding="utf-8") == "keep\nchanged\n"
    assert Path(snapshot.manifest_path).with_name("change-report.json").is_file()


def test_completed_parallel_result_survives_a_later_overwrite(tmp_path):
    workspace = tmp_path / "workspace"
    recovery = tmp_path / "recovery"
    workspace.mkdir()
    target = workspace / "feature.py"
    target.write_text("baseline\n", encoding="utf-8")

    first = capture_workspace_snapshot(workspace, recovery, conversation_id=1, generation_id=1)
    second = capture_workspace_snapshot(workspace, recovery, conversation_id=1, generation_id=2)

    target.write_text("first agent feature\n", encoding="utf-8")
    first_report = finalize_workspace_snapshot(first)
    first_change = first_report["changed_files"][0]

    target.write_text("second agent stale version\n", encoding="utf-8")
    finalize_workspace_snapshot(second)

    assert target.read_text(encoding="utf-8") == "second agent stale version\n"
    assert Path(first_change["after_recovery_blob"]).read_text(encoding="utf-8") == "first agent feature\n"
