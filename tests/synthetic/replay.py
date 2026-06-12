"""Synthetic-topic replay — oracle and pipeline lanes.

Replays the authored timeline (tests/fixtures/synthetic_topic/) through the
real MCP boundary + engine in an isolated repo, stepping the NROL_AO_AS_OF
simulation clock through the simulated days.

Oracle lane: the gold transitions ARE the reference — the engine is
deterministic given typed observations, so this trajectory is what a perfect
perception layer would produce.

Pipeline lane: the generated corpus goes through the real perception path —
submit_article -> run_matcher_with_llama (local llama-server) -> committed
decisions — and its divergence from the oracle is perception error, cleanly
separated from engine math. v1 auto-commits everything the matcher decides;
an operator-review-policy lane is a later extension.

Runs as a subprocess (own interpreter) so the engine module binds to the
isolated repo without fighting other test fixtures' module caches.

Usage:
    python tests/synthetic/replay.py --out trajectory.jsonl [--keep-repo DIR]
    python tests/synthetic/replay.py --lane pipeline --out pipe.jsonl \
        --decisions decisions.jsonl
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOOM_ROOT = HERE.parent.parent
FIXTURES = LOOM_ROOT / "tests" / "fixtures" / "synthetic_topic"
SOURCE_REPO = Path(
    os.environ.get("NROL_AO_SOURCE_REPO", r"C:\Claude-Code\NROL-AO\temp-repo")
)


def build_repo(dest: Path, topic: dict) -> None:
    """Isolated engine repo: same recipe as the capability-suite fixture."""
    shutil.copy2(SOURCE_REPO / "engine.py", dest / "engine.py")
    shutil.copy2(SOURCE_REPO / "governor.py", dest / "governor.py")
    shutil.copytree(
        SOURCE_REPO / "framework",
        dest / "framework",
        ignore=shutil.ignore_patterns("__pycache__", "backtest_data"),
    )
    if (SOURCE_REPO / "sources").is_dir():
        shutil.copytree(SOURCE_REPO / "sources", dest / "sources")
    (dest / "topics").mkdir()
    (dest / "loom" / "topics").mkdir(parents=True)
    (dest / "briefs").mkdir()
    slug = topic["meta"]["slug"]
    (dest / "topics" / f"{slug}.json").write_text(
        json.dumps(topic, indent=2), encoding="utf-8"
    )


def setup_env(repo: Path) -> None:
    os.environ["NROL_AO_REPO"] = str(repo)
    os.environ["NROL_AO_ACTIVITY_DIR"] = str(repo / "mcp_activity")
    os.environ["NROL_AO_ALLOW_UNGATED_COMMITS"] = "1"
    os.environ.pop("LOOM_CONV_ID", None)
    sys.path.insert(0, str(LOOM_ROOT))


def disk_posteriors(repo: Path, slug: str) -> dict:
    topic = json.loads(
        (repo / "topics" / f"{slug}.json").read_text(encoding="utf-8")
    )
    return {k: v["posterior"] for k, v in topic["model"]["hypotheses"].items()}


def run_oracle(server, repo: Path, timeline: dict) -> list[dict]:
    slug = timeline["slug"]
    trajectory = []
    for event in sorted(timeline["events"], key=lambda e: e["date"]):
        gold = event["gold"]
        os.environ["NROL_AO_AS_OF"] = f"{event['date']}T12:00:00+00:00"
        before = disk_posteriors(repo, slug)

        if gold["action"] == "IGNORE":
            trajectory.append({
                "date": event["date"], "event_id": event["id"],
                "action": "IGNORE", "committed": False,
                "posteriors": before, "moved": False,
            })
            continue

        if gold["action"] == "REDTEAM":
            # Operator maintenance, not a transition: the indicator-cleanup
            # session records its red-team result on the latest
            # posteriorHistory entry (what the saturation gate looks for).
            # The harness owns this repo, so a direct stamp stands in for
            # the cleanup-session skill.
            topic_file = repo / "topics" / f"{slug}.json"
            topic = json.loads(topic_file.read_text(encoding="utf-8"))
            history = topic["model"]["posteriorHistory"]
            history[-1]["redTeam"] = {
                "devil_advocate_score": gold.get("devil_advocate_score", 0.0),
                "challenge": gold.get("challenge", ""),
                "timestamp": f"{event['date']}T12:00:00+00:00",
            }
            topic_file.write_text(json.dumps(topic, indent=2), encoding="utf-8")
            trajectory.append({
                "date": event["date"], "event_id": event["id"],
                "action": "REDTEAM", "committed": False,
                "posteriors": before, "moved": False,
            })
            continue

        evidence = {
            "headline": event["summary"],
            "text": event["summary"],
            "source": "oracle",
            "url": f"oracle://{event['id']}",
            "published": f"{event['date']}T08:00:00+00:00",
            "tag": "EVENT",
        }
        if event.get("articles", 1) >= 2:
            # Duplicate coverage of one causal event doubles as corroboration:
            # the confidence_inflation gate requires >= 2 refs for >15pp moves.
            evidence["evidence_refs"] = [
                f"{event['id']}-art{i + 1}" for i in range(event["articles"])
            ]
        kwargs = dict(
            slug=slug,
            transition=gold["action"],
            evidence=evidence,
            reason=f"oracle gold {event['id']}: {gold['action']}",
            commit=True,
        )
        if gold.get("indicator_id"):
            kwargs["indicator_id"] = gold["indicator_id"]
        if gold.get("observed_value") is not None:
            kwargs["observed_value"] = gold["observed_value"]
        if gold.get("missing_direction"):
            kwargs["missing_direction"] = gold["missing_direction"]

        result = json.loads(server.submit_transition(**kwargs))
        after = disk_posteriors(repo, slug)
        trajectory.append({
            "date": event["date"], "event_id": event["id"],
            "action": gold["action"],
            "indicator_id": gold.get("indicator_id"),
            "committed": bool(result.get("committed")),
            "error": result.get("error"),
            "denied": result.get("denied"),
            "posteriors": after,
            "moved": after != before,
            "tv_distance": 0.5 * sum(
                abs(after[k] - before[k]) for k in after
            ),
        })
    return trajectory


def load_corpus() -> list[dict]:
    corpus_dir = FIXTURES / "corpus"
    articles = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(corpus_dir.glob("*.json"))
    ]
    if not articles:
        raise SystemExit(f"no corpus articles in {corpus_dir}; "
                         "run tests/synthetic/generate_corpus.py first")
    return sorted(articles, key=lambda a: a["published"])


def run_pipeline(server, repo: Path, timeline: dict,
                 corpus: list[dict]) -> tuple[list[dict], list[dict]]:
    """Articles through the real perception path, one simulated day at a time.

    All of a day's articles go to the matcher in one batch — duplicate
    coverage of one causal event lands in the same prompt, which is exactly
    the dedup discipline the corpus was authored to test.
    """
    slug = timeline["slug"]
    trajectory: list[dict] = []
    decision_rows: list[dict] = []

    by_day: dict[str, list[dict]] = {}
    for art in corpus:
        by_day.setdefault(art["published"][:10], []).append(art)

    for day in sorted(by_day):
        day_articles = by_day[day]
        os.environ["NROL_AO_AS_OF"] = f"{day}T12:00:00+00:00"
        before = disk_posteriors(repo, slug)

        matcher_articles = []
        for art in day_articles:
            # The store's entry point of record; the matcher batch below is
            # what actually decides.
            json.loads(server.submit_article({
                "url": art["url"], "headline": art["headline"],
                "source": art["outlet"], "published": art["published"],
                "text": art["body"],
            }))
            matcher_articles.append({
                "headline": art["headline"], "url": art["url"],
                "source": art["outlet"], "date": art["published"],
                "excerpt": art["body"],
            })

        result = json.loads(server.run_matcher_with_llama(
            slug, matcher_articles, commit=True, temperature=0.0,
        ))
        after = disk_posteriors(repo, slug)
        decisions = result.get("decisions") or []
        for d in decisions:
            idx = d.get("idx")
            art = day_articles[idx - 1] if idx and idx <= len(day_articles) else None
            decision_rows.append({
                "date": day,
                "article_id": art["id"] if art else None,
                "event_id": art["event_id"] if art else None,
                "label": art["label"] if art else None,
                "gold": art["gold"] if art else None,
                "decision": d,
            })
        trajectory.append({
            "date": day,
            "articles": [a["id"] for a in day_articles],
            "decision_count": len(decisions),
            "applied": result.get("applied"),
            "error": result.get("error"),
            "denied": result.get("denied"),
            "posteriors": after,
            "moved": after != before,
            "tv_distance": 0.5 * sum(abs(after[k] - before[k]) for k in after),
        })
        status = "moved" if after != before else "no move"
        print(f"  {day}: {len(day_articles)} articles, "
              f"{len(decisions)} decisions, {status}")
        if result.get("error"):
            print(f"    ERROR: {result['error']}")
    return trajectory, decision_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="trajectory JSONL path")
    ap.add_argument("--keep-repo", default="", help="build repo here and keep it")
    ap.add_argument("--lane", choices=("oracle", "pipeline"), default="oracle")
    ap.add_argument("--decisions", default="",
                    help="pipeline lane: per-article decision JSONL path")
    args = ap.parse_args()

    topic = json.loads((FIXTURES / "topic.json").read_text(encoding="utf-8"))
    timeline = json.loads((FIXTURES / "timeline.json").read_text(encoding="utf-8"))

    if args.keep_repo:
        repo = Path(args.keep_repo).resolve()
        repo.mkdir(parents=True, exist_ok=True)
        tmp = None
    else:
        tmp = tempfile.mkdtemp(prefix="nrol-synthetic-")
        repo = Path(tmp)

    try:
        build_repo(repo, topic)
        setup_env(repo)
        from mcp_servers.nrol_ao import server

        if args.lane == "pipeline":
            corpus = load_corpus()
            trajectory, decision_rows = run_pipeline(
                server, repo, timeline, corpus
            )
            if args.decisions:
                dec_out = Path(args.decisions)
                dec_out.parent.mkdir(parents=True, exist_ok=True)
                with dec_out.open("w", encoding="utf-8") as f:
                    for row in decision_rows:
                        f.write(json.dumps(row) + "\n")
        else:
            trajectory = run_oracle(server, repo, timeline)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for row in trajectory:
                f.write(json.dumps(row) + "\n")

        final = trajectory[-1]["posteriors"]
        print(f"{args.lane} lane: {len(trajectory)} rows replayed")
        print(f"final posteriors: {json.dumps(final)}")
        print(f"argmax: {max(final, key=final.get)} "
              f"(authored truth: {timeline['truth']['hypothesis']})")
        errors = [t for t in trajectory if t.get("error")]
        if errors:
            print(f"ERRORS in {len(errors)} rows:")
            for t in errors:
                where = t.get("event_id") or ",".join(t.get("articles", []))
                print(f"  {where} {t['date']}: {t['error']}")
            return 1
        return 0
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
