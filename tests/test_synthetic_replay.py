"""Oracle-lane replay of the synthetic Meridia timeline.

Runs tests/synthetic/replay.py as a subprocess (own interpreter, so the
engine module binds to the replay's isolated repo instead of the capability
suite's fixture repo) and asserts the mechanics the harness exists to prove:
typed transitions move posteriors correctly through simulated time, and
non-updates are visible as non-updates.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LOOM_ROOT = Path(__file__).resolve().parent.parent
SOURCE_REPO = Path(
    os.environ.get("NROL_AO_SOURCE_REPO", r"C:\Claude-Code\NROL-AO\temp-repo")
)
FIXTURES = LOOM_ROOT / "tests" / "fixtures" / "synthetic_topic"

pytestmark = pytest.mark.skipif(
    not (SOURCE_REPO / "engine.py").is_file(),
    reason="NROL-AO engine repo not available at NROL_AO_SOURCE_REPO",
)


def _run_replay(out_path: Path) -> list[dict]:
    env = {**os.environ}
    env.pop("NROL_AO_AS_OF", None)
    env.pop("LOOM_CONV_ID", None)
    proc = subprocess.run(
        [sys.executable, str(LOOM_ROOT / "tests" / "synthetic" / "replay.py"),
         "--out", str(out_path)],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert proc.returncode == 0, f"replay failed:\n{proc.stdout}\n{proc.stderr}"
    return [
        json.loads(line)
        for line in out_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(scope="module")
def trajectory(tmp_path_factory):
    out = tmp_path_factory.mktemp("oracle") / "trajectory.jsonl"
    return _run_replay(out)


@pytest.fixture(scope="module")
def timeline():
    return json.loads((FIXTURES / "timeline.json").read_text(encoding="utf-8"))


def _row(trajectory, event_id):
    return next(t for t in trajectory if t["event_id"] == event_id)


def test_all_gold_transitions_commit(trajectory):
    for t in trajectory:
        if t["action"] in ("IGNORE", "REDTEAM"):
            assert t["committed"] is False
        else:
            assert t["committed"] is True, f"{t['event_id']}: {t.get('error')}"
            assert not t.get("error")


def test_posteriors_sum_to_one_throughout(trajectory):
    for t in trajectory:
        assert abs(sum(t["posteriors"].values()) - 1.0) < 0.01, t["event_id"]


def test_deadline_eliminations_fire_in_simulated_time(trajectory):
    # H1's tranche deadline is 2026-08-01, H2's is 2026-09-01. The engine
    # floors an expired hypothesis on the next update after the crossing.
    for t in trajectory:
        if t["committed"] and t["date"] >= "2026-08-02":
            assert t["posteriors"]["H1"] < 0.02, f"H1 not floored by {t['date']}"
        if t["committed"] and t["date"] >= "2026-09-02":
            assert t["posteriors"]["H2"] < 0.02, f"H2 not floored by {t['date']}"
    # And not before: H1 lives until its deadline.
    early = [t for t in trajectory if t["date"] < "2026-08-01"]
    assert any(t["posteriors"]["H1"] > 0.02 for t in early)


def test_park_and_schema_gap_move_nothing(trajectory):
    assert _row(trajectory, "E07")["action"] == "PARK"
    assert _row(trajectory, "E07")["moved"] is False
    assert _row(trajectory, "E06")["action"] == "SCHEMA_GAP"
    assert _row(trajectory, "E06")["moved"] is False


def test_refire_attenuates(trajectory):
    # E01 and E04 fire the same indicator (lr_decay 0.6): the second
    # firing must move posteriors less than the first.
    first = _row(trajectory, "E01")["tv_distance"]
    refire = _row(trajectory, "E04")["tv_distance"]
    assert 0 < refire < first


def test_observation_at_baseline_is_a_visible_non_update(trajectory):
    # E03 observes transit at 4% with baseline 5: no information, no
    # movement — and the commit is still recorded (non-updates visible).
    row = _row(trajectory, "E03")
    assert row["committed"] is True
    assert row["moved"] is False


def test_oracle_converges_on_authored_truth(trajectory, timeline):
    final = trajectory[-1]["posteriors"]
    truth = timeline["truth"]["hypothesis"]
    assert max(final, key=final.get) == truth
    assert final[truth] > 0.9


def test_replay_is_deterministic(trajectory, tmp_path):
    second = _run_replay(tmp_path / "trajectory2.jsonl")
    assert [t["posteriors"] for t in second] == [
        t["posteriors"] for t in trajectory
    ]
