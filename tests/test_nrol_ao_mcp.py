"""Capability tests for the NROL-AO MCP boundary.

These are the executable form of the system's core promise: beliefs only
move when the world moves — through typed transitions bound to
pre-committed likelihoods. No operator, human or LLM, can submit a
posterior through this surface.

The suite builds an isolated fixture repo (engine + framework copied from
the source repo, synthetic topic state) so no live topic data is touched.
"""

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

SOURCE_REPO = Path(os.environ.get("NROL_AO_SOURCE_REPO", r"C:\Claude-Code\NROL-AO\temp-repo"))

pytestmark = pytest.mark.skipif(
    not (SOURCE_REPO / "engine.py").is_file(),
    reason="NROL-AO engine repo not available at NROL_AO_SOURCE_REPO",
)

SLUG = "test-capability"


def _fixture_topic() -> dict:
    return {
        "meta": {
            "slug": SLUG,
            "title": "Capability test topic",
            "question": "Which synthetic outcome resolves by 2030-12-31?",
            "resolution": "Resolves to the hypothesis whose synthetic event is published by test-rig before 2030-12-31.",
            "resolutionDate": "2030-12-31",
            "classification": "CALIBRATION",
            "status": "ACTIVE",
            "lens": "OPERATOR_JUDGMENT",
            "calibrationStatus": "SKIPPED_OPERATOR_JUDGMENT",
            "calibrationSkipReason": "Synthetic capability-test fixture; no real-world claim.",
            "created": "2026-06-01T00:00:00+00:00",
            "startDate": "2026-06-01",
            "dayCount": 1,
            "lastUpdated": "2026-06-01T00:00:00+00:00",
            "lastScanned": "2026-06-01T00:00:00+00:00",
        },
        "model": {
            "hypotheses": {
                # midpoint is required: bayesian_update recomputes
                # model.expectedValue as sum(midpoint * posterior).
                "H1": {"label": "Synthetic outcome A by 2030", "midpoint": 100, "posterior": 0.5},
                "H2": {"label": "Synthetic outcome B by 2030", "midpoint": 50, "posterior": 0.3},
                "H3": {"label": "Neither outcome by 2030", "midpoint": 0, "posterior": 0.2},
            },
            # Initial history entry documents the non-uniform priors so the
            # design gate ("NON-UNIFORM PRIORS without justification") passes.
            "posteriorHistory": [
                {
                    "date": "2026-06-01",
                    "timestamp": "2026-06-01T00:00:00+00:00",
                    "posteriors": {"H1": 0.5, "H2": 0.3, "H3": 0.2},
                    "note": (
                        "Initial priors: synthetic fixture; H1 favored by design "
                        "so directional assertions have headroom."
                    ),
                }
            ],
        },
        "governance": {
            "health": "HEALTHY",
            "issues": [],
            "flagged_for_indicator_review": [],
            "flagged_schema_gaps": [],
            "proposed_schema_extensions": [],
        },
        "indicators": {
            "tiers": {
                "tier1_critical": [
                    {
                        "id": "ind_binary_mild",
                        "desc": "Synthetic binary indicator: test-rig publishes event A confirmation.",
                        "status": "NOT_FIRED",
                        "firedDate": None,
                        "note": None,
                        "posteriorEffect": "H1 +6pp; H2 -3pp; H3 -3pp.",
                        "likelihoods": {"H1": 0.6, "H2": 0.45, "H3": 0.5},
                        "lr_decay": 1.0,
                        "n_firings": 0,
                        "resolution_class": False,
                        "shape": "per_event_member",
                        "causal_event_id": "test_event_binary",
                    },
                    {
                        "id": "ind_observable_metric",
                        "desc": "Synthetic numeric indicator: monthly test metric percent from test-rig.",
                        "status": "NOT_FIRED",
                        "firedDate": None,
                        "note": None,
                        "posteriorEffect": "H1 +5pp; H2 -2pp; H3 -3pp.",
                        "likelihoods": {"H1": 0.65, "H2": 0.45, "H3": 0.35},
                        "lr_decay": 1.0,
                        "n_firings": 0,
                        "resolution_class": False,
                        "shape": "per_event_member",
                        "causal_event_id": "test_event_metric",
                        "observable": {
                            "metric": "test:metric_pct",
                            "family": "logistic",
                            "threshold_value": 50,
                            "baseline": 40,
                            "direction": "higher_strengthens",
                        },
                    },
                ],
                "tier2_strong": [],
                "tier3_suggestive": [],
            },
            "anti_indicators": [],
        },
        "actorModel": {},
        "dataFeeds": {},
        "predictionScoring": {},
        "contradictionTracker": {},
        "evidenceLog": [],
        "sourceCalibration": {},
    }


@pytest.fixture(scope="session")
def nrol_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("nrol_fixture_repo")
    shutil.copy2(SOURCE_REPO / "engine.py", root / "engine.py")
    shutil.copy2(SOURCE_REPO / "governor.py", root / "governor.py")
    shutil.copytree(
        SOURCE_REPO / "framework",
        root / "framework",
        ignore=shutil.ignore_patterns("__pycache__", "backtest_data"),
    )
    if (SOURCE_REPO / "sources").is_dir():
        shutil.copytree(SOURCE_REPO / "sources", root / "sources")
    (root / "topics").mkdir()
    (root / "loom" / "topics").mkdir(parents=True)
    (root / "briefs").mkdir()
    return root


@pytest.fixture(scope="session")
def nrol(nrol_repo):
    """Configure env and return the MCP server module bound to the fixture repo."""
    os.environ["NROL_AO_REPO"] = str(nrol_repo)
    os.environ["NROL_AO_ACTIVITY_DIR"] = str(nrol_repo / "mcp_activity")
    os.environ.pop("LOOM_CONV_ID", None)
    # Commits are fail-closed without Loom context; tests opt out explicitly.
    os.environ["NROL_AO_ALLOW_UNGATED_COMMITS"] = "1"
    from mcp_servers.nrol_ao import server

    return server


@pytest.fixture
def topic_path(nrol, nrol_repo):
    """Write a pristine synthetic topic before each test."""
    path = nrol_repo / "topics" / f"{SLUG}.json"
    path.write_text(json.dumps(_fixture_topic(), indent=2), encoding="utf-8")
    return path


def _disk_topic(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _disk_posteriors(path: Path) -> dict:
    return {k: v["posterior"] for k, v in _disk_topic(path)["model"]["hypotheses"].items()}


def _evidence(text: str = "", **extra) -> dict:
    return {
        "text": text or f"Synthetic observation {uuid.uuid4().hex[:8]} from test-rig.",
        "source": "test-rig",
        "tag": "EVENT",
        **extra,
    }


def _submit(nrol, **kwargs) -> dict:
    return json.loads(nrol.submit_transition(**kwargs))


# ---------------------------------------------------------------------------
# Read-path robustness
# ---------------------------------------------------------------------------


def test_topic_status_lists_fixture(nrol, topic_path):
    out = json.loads(nrol.topic_status())
    assert "error" not in out
    assert SLUG in [r["slug"] for r in out["topics"]]


def test_malformed_file_does_not_break_listing(nrol, nrol_repo, topic_path):
    bad = nrol_repo / "topics" / "manifest.json"
    bad.write_text(json.dumps({"topics": [SLUG]}), encoding="utf-8")
    try:
        out = json.loads(nrol.topic_status())
        assert "error" not in out
        assert SLUG in [r["slug"] for r in out["topics"]]
        assert "manifest" in [s["slug"] for s in out.get("skipped", [])]
    finally:
        bad.unlink()


# ---------------------------------------------------------------------------
# Rejection gates: malformed transitions must not move anything
# ---------------------------------------------------------------------------


def test_fire_requires_indicator_id(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(nrol, slug=SLUG, transition="FIRE", evidence=_evidence(), commit=True)
    assert "error" in out
    assert "indicator_id" in out["error"]
    assert _disk_posteriors(topic_path) == before


def test_fire_unknown_indicator_rejected(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_does_not_exist", commit=True,
    )
    assert "error" in out
    assert "not found" in out["error"]
    assert _disk_posteriors(topic_path) == before


def test_observe_requires_value(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="OBSERVE", evidence=_evidence(),
        indicator_id="ind_observable_metric", commit=True,
    )
    assert "error" in out
    assert "observed_value" in out["error"]
    assert _disk_posteriors(topic_path) == before


def test_observe_requires_observable_block(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="OBSERVE", evidence=_evidence(),
        indicator_id="ind_binary_mild", observed_value=55.0, commit=True,
    )
    assert "error" in out
    assert "observable" in out["error"]
    assert _disk_posteriors(topic_path) == before


def test_unknown_transition_rejected(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(nrol, slug=SLUG, transition="SET_POSTERIOR", evidence=_evidence(), commit=True)
    assert "error" in out
    assert _disk_posteriors(topic_path) == before


def test_evidence_cannot_smuggle_likelihoods(nrol, topic_path):
    """The evidence whitelist must drop likelihood/posterior payloads."""
    evidence = _evidence(
        likelihoods={"H1": 0.99, "H2": 0.01, "H3": 0.01},
        posteriors={"H1": 0.9},
        target_posterior=0.9,
    )
    out = _submit(nrol, slug=SLUG, transition="PARK", evidence=evidence, commit=False)
    entry = out["evidence_entry"]
    assert "likelihoods" not in entry
    assert "posteriors" not in entry
    assert "target_posterior" not in entry


# ---------------------------------------------------------------------------
# Non-mutating transitions
# ---------------------------------------------------------------------------


def test_dry_run_never_mutates(nrol, topic_path):
    before_bytes = topic_path.read_bytes()
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=False,
    )
    assert out["committed"] is False
    assert out["posteriors_before"] == out["posteriors_after"]
    assert out["pre_committed_likelihoods"] == {"H1": 0.6, "H2": 0.45, "H3": 0.5}
    assert topic_path.read_bytes() == before_bytes


def test_ignore_writes_nothing(nrol, topic_path):
    before_bytes = topic_path.read_bytes()
    out = _submit(nrol, slug=SLUG, transition="IGNORE", evidence=_evidence(), commit=True)
    assert out["ignored"] is True
    assert out["committed"] is False
    assert topic_path.read_bytes() == before_bytes


def test_park_commits_evidence_without_posterior_movement(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(nrol, slug=SLUG, transition="PARK", evidence=_evidence(), commit=True)
    assert out.get("committed") is True, out
    after_topic = _disk_topic(topic_path)
    after = {k: v["posterior"] for k, v in after_topic["model"]["hypotheses"].items()}
    assert after == before
    assert len(after_topic["evidenceLog"]) == 1
    assert len(after_topic["governance"]["flagged_for_indicator_review"]) == 1


def test_schema_gap_commits_without_posterior_movement(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="SCHEMA_GAP", evidence=_evidence(),
        reason="no indicator covers synthetic direction", missing_direction="H2-negative",
        commit=True,
    )
    assert out.get("committed") is True, out
    after_topic = _disk_topic(topic_path)
    after = {k: v["posterior"] for k, v in after_topic["model"]["hypotheses"].items()}
    assert after == before
    assert len(after_topic["governance"]["flagged_schema_gaps"]) == 1


# ---------------------------------------------------------------------------
# Mutating transitions: Bayes through pre-committed likelihoods only
# ---------------------------------------------------------------------------


def test_fire_moves_posteriors_via_precommitted_lrs(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out.get("committed") is True, out
    after = _disk_posteriors(topic_path)
    assert after != before
    # engine rounds posteriors to 4 decimals; sum holds to ~5e-4
    assert abs(sum(after.values()) - 1.0) < 5e-4
    # H1 has the highest pre-committed likelihood; it must not lose mass.
    assert after["H1"] > before["H1"]


def test_repeat_fire_attenuates(nrol, topic_path):
    """Re-firing the same indicator must move posteriors less than the first
    firing (lr_decay repeat-firing attenuation + causal de-correlation)."""
    p0 = _disk_posteriors(topic_path)
    out1 = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out1.get("committed") is True, out1
    p1 = _disk_posteriors(topic_path)
    out2 = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out2.get("committed") is True, out2
    p2 = _disk_posteriors(topic_path)
    first_shift = abs(p1["H1"] - p0["H1"])
    second_shift = abs(p2["H1"] - p1["H1"])
    assert first_shift > 0
    assert second_shift < first_shift


def test_observe_applies_mechanical_partial_lr(nrol, topic_path):
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="OBSERVE", evidence=_evidence(),
        indicator_id="ind_observable_metric", observed_value=55.0, commit=True,
    )
    assert out.get("committed") is True, out
    after = _disk_posteriors(topic_path)
    assert after != before
    # engine rounds posteriors to 4 decimals; sum holds to ~5e-4
    assert abs(sum(after.values()) - 1.0) < 5e-4
    # 55 is beyond the higher_strengthens threshold (50): H1 (highest
    # likelihood) must not lose mass.
    assert after["H1"] > before["H1"]


def test_legacy_posterior_path_rejected_on_active_topic(nrol, topic_path, monkeypatch):
    """The legacy run_update(posteriors=...) freeform path must refuse ACTIVE
    topics — the migration spec's 'biggest authority leak'. Admin override
    requires a signed NROL_AO_ADMIN_POSTERIORS_REASON."""
    import importlib

    monkeypatch.delenv("NROL_AO_ADMIN_POSTERIORS_REASON", raising=False)
    update_mod = importlib.import_module("framework.update")
    before = _disk_posteriors(topic_path)
    with pytest.raises(Exception) as exc_info:
        update_mod.run_update(
            SLUG,
            posteriors={"H1": 0.9, "H2": 0.05, "H3": 0.05},
            posterior_reason="operator says so",
        )
    assert "ACTIVE" in str(exc_info.value)
    assert _disk_posteriors(topic_path) == before


# ---------------------------------------------------------------------------
# Proposal lifecycle: submit_article -> propose_match -> commit_match
# ---------------------------------------------------------------------------


def _article(**extra) -> dict:
    suffix = uuid.uuid4().hex[:8]
    return {
        "headline": f"Synthetic event report {suffix}",
        "url": f"https://example.test/articles/{suffix}",
        "source": "test-wire",
        "date": "2026-06-09",
        "body": "Official synthetic print released by test-rig.",
        **extra,
    }


def test_submit_article_dedupes_by_url(nrol, topic_path):
    art = _article()
    first = json.loads(nrol.submit_article(art))
    again = json.loads(nrol.submit_article(art))
    assert "error" not in first
    assert first["id"].startswith("art-")
    assert again["id"] == first["id"]
    assert again["deduped"] is True


def test_propose_match_validates_without_mutation(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    before_bytes = topic_path.read_bytes()

    bad_action = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="IGNORE", rationale="meh"))
    assert "error" in bad_action

    bad_indicator = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_nope", rationale="directional case"))
    assert "error" in bad_indicator

    no_rationale = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="PARK", rationale=""))
    assert "error" in no_rationale

    ok = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild",
        rationale="threshold met per official synthetic print"))
    assert ok.get("status") == "pending"
    assert topic_path.read_bytes() == before_bytes  # proposals never mutate


def test_commit_match_applies_fire_through_gates(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    prop = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild",
        rationale="threshold met per official synthetic print"))
    before = _disk_posteriors(topic_path)

    out = json.loads(nrol.commit_match(prop["id"]))
    assert out.get("status") == "committed", out
    after = _disk_posteriors(topic_path)
    assert after["H1"] > before["H1"]
    # engine rounds posteriors to 4 decimals; sum holds to ~5e-4
    assert abs(sum(after.values()) - 1.0) < 5e-4

    # A decided proposal cannot be committed again.
    again = json.loads(nrol.commit_match(prop["id"]))
    assert again.get("committed") is False
    assert "already decided" in again.get("error", "")


def test_commit_match_rejects_duplicate_url(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    first = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="PARK",
        rationale="relevant, no indicator matches"))
    committed = json.loads(nrol.commit_match(first["id"]))
    assert committed.get("status") == "committed", committed

    second = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="FIRE",
        indicator_id="ind_binary_mild",
        rationale="same article again, dressed as new evidence"))
    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.commit_match(second["id"]))
    assert out.get("status") == "rejected"
    assert "already committed" in out.get("error", "")
    assert _disk_posteriors(topic_path) == before


def test_commit_match_denied_without_loom_stays_pending(nrol, topic_path, monkeypatch):
    art = json.loads(nrol.submit_article(_article()))
    prop = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="PARK",
        rationale="relevant, no indicator matches"))
    monkeypatch.delenv("NROL_AO_ALLOW_UNGATED_COMMITS", raising=False)
    monkeypatch.delenv("LOOM_CONV_ID", raising=False)
    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.commit_match(prop["id"]))
    assert out.get("committed") is False
    assert out.get("status") == "pending"  # denial is not rejection
    assert _disk_posteriors(topic_path) == before
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    assert prop["id"] in [p["id"] for p in queue["proposals"]]


def test_withdraw_proposal(nrol, topic_path):
    art = json.loads(nrol.submit_article(_article()))
    prop = json.loads(nrol.propose_match(
        article_id=art["id"], slug=SLUG, action="PARK",
        rationale="probably noise on reflection"))
    out = json.loads(nrol.withdraw_proposal(prop["id"], reason="noise"))
    assert out["status"] == "withdrawn"
    again = json.loads(nrol.commit_match(prop["id"]))
    assert again.get("committed") is False


# ---------------------------------------------------------------------------
# Scheduled scan with safe commit policy (spec Flow B)
# ---------------------------------------------------------------------------


def test_safe_policy_scan_parks_and_files_proposals(nrol, topic_path, monkeypatch):
    """commit_policy="safe": PARK auto-applies (no posterior movement),
    FIRE is filed as a pending proposal, a digest is written. No posterior
    moves without a human."""
    import importlib

    suffix = uuid.uuid4().hex[:6]
    articles = [
        {
            "headline": f"Background development {suffix}",
            "url": f"https://example.test/scan/{suffix}-a",
            "source": "test-wire", "date": "2026-06-09",
            "relevance": "relevant but matches no indicator",
        },
        {
            "headline": f"Official threshold print {suffix}",
            "url": f"https://example.test/scan/{suffix}-b",
            "source": "test-wire", "date": "2026-06-09",
            "relevance": "synthetic event A confirmed",
        },
    ]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local"},
    )
    fw = importlib.import_module("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "relevant, unmatched", "reason": "no indicator threshold met"},
        {"idx": 2, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
    ])

    before = _disk_posteriors(topic_path)
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, commit_policy="safe",
        fetch_full_articles=False,
    ))
    assert "error" not in out, out
    assert _disk_posteriors(topic_path) == before  # nothing moved beliefs

    topic = _disk_topic(topic_path)
    assert len(topic["governance"]["flagged_for_indicator_review"]) >= 1  # PARK landed

    packet = out["topics"][0]
    filed = packet["commit_policy"]["proposals_filed"]
    assert len(filed) == 1  # FIRE awaits human review
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    queued = {p["id"]: p for p in queue["proposals"]}
    assert filed[0] in queued
    assert queued[filed[0]]["action"] == "FIRE"
    assert queued[filed[0]]["indicator_id"] == "ind_binary_mild"

    assert out.get("digest_path")
    digest = Path(out["digest_path"]).read_text(encoding="utf-8")
    assert SLUG in digest
    assert "proposals filed for review: 1" in digest


def test_empty_matcher_output_is_an_error_not_a_quiet_window(nrol, topic_path, monkeypatch):
    """A reasoning model can burn the whole token budget in the think channel
    and return empty content (observed live: Qwen3.6-27B, finish_reason=length,
    content=""). That must surface as a matcher error, must NOT stamp
    lastScanned (stamping shrinks the next adaptive window and drops the
    articles forever), and must not read as a quiet news window in the digest."""
    suffix = uuid.uuid4().hex[:6]
    articles = [{
        "headline": f"Relevant development {suffix}",
        "url": f"https://example.test/empty-matcher/{suffix}",
        "source": "test-wire", "date": "2026-06-10",
        "relevance": "clearly relevant",
    }]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "", "reasoning_chars": 4096, "finish_reason": "length",
                         "model": "test-thinker", "host": "local"},
    )

    before_scanned = _disk_topic(topic_path)["meta"].get("lastScanned")
    out = json.loads(nrol.run_news_scan(
        slugs=[SLUG], commit=False, dry_run=False, fetch_full_articles=False,
    ))
    assert "error" not in out, out

    packet = out["topics"][0]
    assert "matcher" in packet["search_errors"]
    assert "finish_reason=length" in packet["search_errors"]["matcher"]
    assert packet["decisions"] == []
    assert packet["scan_record"]["recorded"] is False
    assert "matcher" in packet["scan_record"]["skipped_reason"]
    assert _disk_topic(topic_path)["meta"].get("lastScanned") == before_scanned

    digest = Path(out["digest_path"]).read_text(encoding="utf-8")
    assert "MATCHER FAILED" in digest
    assert "a valid result" not in digest


def test_scan_feeds_matcher_full_article_excerpts(nrol, topic_path, monkeypatch):
    """Search snippets are ~500 chars of SEO text; OBSERVE needs the numbers
    from the article body. The scan fetches each deduped article and the
    matcher prompt carries an EXCERPT block with the extracted text."""
    suffix = uuid.uuid4().hex[:6]
    articles = [{
        "headline": f"Transit data print {suffix}",
        "url": f"https://example.test/full/{suffix}",
        "source": "test-wire", "date": "2026-06-10",
        "relevance": "short snippet only",
    }]
    monkeypatch.setattr(
        nrol, "_search_web_articles",
        lambda query, channel, max_results: list(articles) if channel == "wildcard" else [],
    )
    monkeypatch.setattr(
        nrol, "_fetch_article_excerpt",
        lambda url, max_chars, **kw: f"FULL BODY {suffix}: AIS shows transit at 10% of baseline.",
    )
    seen = {}

    def fake_chat(prompt, **kw):
        seen["prompt"] = prompt
        return {"text": "canned", "model": "t", "host": "l",
                "finish_reason": "stop", "reasoning_chars": 0}

    monkeypatch.setattr(nrol.llama_client, "chat", fake_chat)

    out = json.loads(nrol.run_news_scan(slugs=[SLUG], commit=False, dry_run=True))
    assert "error" not in out, out
    assert "EXCERPT:" in seen["prompt"]
    assert f"FULL BODY {suffix}" in seen["prompt"]
    packet = out["topics"][0]
    assert packet["excerpts"] == {"fetched": 1, "of": 1, "chars_cap": 2800}


# ---------------------------------------------------------------------------
# Parked-queue re-adjudication: kept-but-timestamped + reverse staleness
# ---------------------------------------------------------------------------


def _seed_parked(topic_path, n=2):
    """Append n parked evidence entries to the fixture topic on disk."""
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    ids = []
    for i in range(n):
        ev_id = f"ev_test_{uuid.uuid4().hex[:6]}"
        topic.setdefault("evidenceLog", []).append({
            "id": ev_id,
            "time": "2026-05-07T12:00:00+00:00",
            "tag": "EVENT",
            "text": f"Parked development number {i}: threshold-adjacent report.",
            "url": f"https://example.test/parked/{ev_id}",
            "source": "test-wire",
        })
        topic.setdefault("governance", {}).setdefault(
            "flagged_for_indicator_review", []
        ).append(ev_id)
        ids.append(ev_id)
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")
    return ids


def test_review_parked_timestamps_without_clearing(nrol, topic_path, monkeypatch):
    """Kept-but-timestamped: re-reviewed items stay in the flagged queue but
    gain review records; FIRE re-decisions escalate to pending proposals
    (human commit); an immediate second run finds nothing due (refractory)."""
    ev_ids = _seed_parked(topic_path, n=2)
    monkeypatch.setattr(
        nrol, "_fetch_article_excerpt",
        lambda url, max_chars, **kw: "FULL TEXT: event A confirmed at threshold.",
    )
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "test-llm", "host": "local",
                         "finish_reason": "stop", "reasoning_chars": 0},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "FIRE", "indicator_id": "ind_binary_mild"},
         "tag": "EVENT", "claim": "event A confirmed", "reason": "threshold met"},
        {"idx": 2, "action": {"kind": "PARK"},
         "tag": "EVENT", "claim": "still nothing extractable", "reason": "no threshold"},
    ])

    out = json.loads(nrol.review_parked(slug=SLUG, dry_run=False))
    assert "error" not in out, out
    assert out["considered"] == 2

    topic = _disk_topic(topic_path)
    flagged = topic["governance"]["flagged_for_indicator_review"]
    for ev_id in ev_ids:
        assert ev_id in flagged  # kept, never cleared here
    book = topic["governance"]["parked_reviews"]
    assert book[ev_ids[0]]["last_decision"] == "FIRE"
    assert book[ev_ids[1]]["last_decision"] == "PARK"
    assert book[ev_ids[0]]["review_count"] == 1
    assert book[ev_ids[0]]["history"][-1]["escalated_proposal_id"]

    # FIRE escalated to a pending proposal, posteriors untouched by review
    assert len(out["proposals_filed"]) == 1
    queue = json.loads(nrol.list_proposals(slug=SLUG, status="pending"))
    assert any(p["id"] == out["proposals_filed"][0] for p in queue["proposals"])

    # Refractory: immediately re-running finds nothing due
    again = json.loads(nrol.review_parked(slug=SLUG, dry_run=False))
    assert again["considered"] == 0
    assert again["debt"]["due_count"] == 0

    # Debt accounting moved from due to fresh
    assert out["debt_after"]["fresh_count"] >= 2


def test_schema_change_makes_reviewed_items_due_again(nrol, topic_path, monkeypatch):
    """A PARK is conditioned on the schema: changing an indicator makes
    previously reviewed items due again via the fingerprint."""
    _seed_parked(topic_path, n=1)
    monkeypatch.setattr(nrol, "_fetch_article_excerpt", lambda url, max_chars, **kw: "")
    monkeypatch.setattr(
        nrol.llama_client, "chat",
        lambda *a, **k: {"text": "canned", "model": "t", "host": "l",
                         "finish_reason": "stop", "reasoning_chars": 0},
    )
    fw = nrol._import_from_repo("framework.news_observation_pipeline")
    monkeypatch.setattr(fw, "parse_matcher_output", lambda text: [
        {"idx": 1, "action": {"kind": "PARK"}, "tag": "EVENT",
         "claim": "nothing", "reason": "no threshold"},
    ])
    out = json.loads(nrol.review_parked(slug=SLUG, dry_run=False))
    assert "error" not in out, out
    assert out["debt_after"]["due_count"] == 0

    # Mutate an indicator description on disk — the PARK's conditional changed
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    tiers = topic["indicators"]["tiers"]
    first_tier = next(iter(tiers.values()))
    first_tier[0]["desc"] = first_tier[0]["desc"] + " (threshold revised)"
    topic_path.write_text(json.dumps(topic, indent=2), encoding="utf-8")

    again = json.loads(nrol.review_parked(slug=SLUG, dry_run=True))
    assert again["considered"] >= 1
    status = json.loads(nrol.topic_status(slugs=[SLUG]))
    debt = status["topics"][0]["parkedReviewDebt"]
    assert debt["dueCount"] >= 1


# ---------------------------------------------------------------------------
# State/code split: NROL_AO_STATE_DIR
# ---------------------------------------------------------------------------


def test_state_dir_relocates_topics(nrol_repo, tmp_path):
    """With NROL_AO_STATE_DIR set, the engine reads and writes topics in the
    state dir, not the repo. Run in a subprocess: the engine computes its
    paths at import time, and this session already imported it repo-rooted."""
    import subprocess
    import sys as _sys

    state = tmp_path / "state_root"
    (state / "topics").mkdir(parents=True)
    (state / "topics" / f"{SLUG}.json").write_text(
        json.dumps(_fixture_topic(), indent=2), encoding="utf-8"
    )
    script = (
        "import engine, json\n"
        f"t = engine.load_topic({SLUG!r})\n"
        "engine.save_topic(t)\n"
        "print(json.dumps({'topics_dir': str(engine.TOPICS_DIR),"
        " 'slug': t['meta']['slug']}))\n"
    )
    env = {
        **os.environ,
        "NROL_AO_STATE_DIR": str(state),
        "PYTHONPATH": str(nrol_repo),
    }
    proc = subprocess.run(
        [_sys.executable, "-c", script],
        capture_output=True, text=True, env=env, cwd=str(nrol_repo), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["slug"] == SLUG
    assert Path(out["topics_dir"]) == state / "topics"
    # save_topic stamped lastUpdated in the STATE dir copy, not the repo's
    saved = json.loads((state / "topics" / f"{SLUG}.json").read_text(encoding="utf-8"))
    assert saved["meta"]["lastUpdated"] > "2026-06-01"


# ---------------------------------------------------------------------------
# Loom approval gate
# ---------------------------------------------------------------------------


def test_commit_without_loom_denied_by_default(nrol, topic_path, monkeypatch):
    """Fail-closed: no Loom context and no explicit opt-out means no commit."""
    monkeypatch.delenv("NROL_AO_ALLOW_UNGATED_COMMITS", raising=False)
    monkeypatch.delenv("LOOM_CONV_ID", raising=False)
    before = _disk_posteriors(topic_path)
    out = _submit(
        nrol, slug=SLUG, transition="FIRE", evidence=_evidence(),
        indicator_id="ind_binary_mild", commit=True,
    )
    assert out.get("committed") is not True
    assert "Loom" in (out.get("denied") or "")
    assert _disk_posteriors(topic_path) == before
