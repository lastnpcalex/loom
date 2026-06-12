"""Score the pipeline lane against the oracle lane and the authored truth.

Three views, per the harness design:

1. Trajectory divergence — per simulated day, total-variation distance between
   the pipeline's posteriors and the oracle's. Divergent days name the
   articles processed that day: perception error, isolated from engine math.
2. Matcher confusion matrix — gold action (from the authored timeline) vs the
   matcher's decision per article, plus indicator routing, observed-value
   deltas, distractor rejection, and duplicate discipline per causal event.
3. Brier at resolution — both lanes vs the authored truth: binary Brier per
   tranche at its simulated resolution deadline, full-vector Brier (sum of
   squared errors, range 0-2) at the end date.

Usage:
    python tests/synthetic/score.py --oracle oracle.jsonl \
        --pipeline pipe.jsonl --decisions decisions.jsonl [--json report.json]
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures" / "synthetic_topic"


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def posterior_series(trajectory: list[dict]) -> dict[str, dict]:
    """date -> posteriors after that date's last row."""
    series: dict[str, dict] = {}
    for row in trajectory:
        series[row["date"]] = row["posteriors"]
    return series


def tv(a: dict, b: dict) -> float:
    return 0.5 * sum(abs(a[k] - b[k]) for k in a)


def divergence(oracle: list[dict], pipeline: list[dict],
               priors: dict) -> list[dict]:
    o_series = posterior_series(oracle)
    p_series = posterior_series(pipeline)
    articles_by_day = {row["date"]: row.get("articles", []) for row in pipeline}
    rows = []
    o_cur, p_cur = dict(priors), dict(priors)
    for day in sorted(set(o_series) | set(p_series)):
        o_cur = o_series.get(day, o_cur)
        p_cur = p_series.get(day, p_cur)
        rows.append({
            "date": day,
            "tv_distance": tv(o_cur, p_cur),
            "oracle": o_cur,
            "pipeline": p_cur,
            "articles": articles_by_day.get(day, []),
        })
    return rows


def confusion(decisions: list[dict], timeline: dict) -> dict:
    matrix: dict[str, dict[str, int]] = {}
    routing = {"correct": 0, "wrong": 0}
    value_deltas = []
    distractors = {"total": 0, "ignored": 0, "parked": 0, "acted_on": 0}
    decided_ids = set()

    for row in decisions:
        gold = row.get("gold") or {}
        gold_action = gold.get("action", "?")
        d_action = (row["decision"].get("action") or {})
        kind = d_action.get("kind", "MISSING")
        matrix.setdefault(gold_action, {})
        matrix[gold_action][kind] = matrix[gold_action].get(kind, 0) + 1
        if row.get("article_id"):
            decided_ids.add(row["article_id"])

        if gold_action in ("FIRE", "OBSERVE") and kind in ("FIRE", "OBSERVE"):
            if d_action.get("indicator_id") == gold.get("indicator_id"):
                routing["correct"] += 1
            else:
                routing["wrong"] += 1
        if gold_action == "OBSERVE" and kind == "OBSERVE" \
                and d_action.get("value") is not None:
            value_deltas.append({
                "article_id": row.get("article_id"),
                "gold": gold.get("observed_value"),
                "matched": d_action["value"],
                "delta": d_action["value"] - gold.get("observed_value", 0),
            })
        if row.get("label") == "DISTRACTOR":
            distractors["total"] += 1
            if kind == "IGNORE":
                distractors["ignored"] += 1
            elif kind == "PARK":
                distractors["parked"] += 1
            else:
                distractors["acted_on"] += 1

    # Duplicate discipline: how many separate move decisions (FIRE/OBSERVE)
    # the matcher emitted for each multi-article causal event. The apply path
    # bundles same-day OBSERVEs, so >1 here is only a hazard for FIREs — but
    # either way the count is the matcher's raw dedup behavior.
    move_counts: dict[str, int] = {}
    for row in decisions:
        if not row.get("event_id"):
            continue
        kind = (row["decision"].get("action") or {}).get("kind")
        if kind in ("FIRE", "OBSERVE"):
            move_counts[row["event_id"]] = move_counts.get(row["event_id"], 0) + 1
    multi = {
        e["id"]: {"articles": e["articles"],
                  "move_decisions": move_counts.get(e["id"], 0)}
        for e in timeline["events"] if e.get("articles", 0) >= 2
    }

    return {
        "matrix": matrix,
        "indicator_routing": routing,
        "observe_value_deltas": value_deltas,
        "distractors": distractors,
        "duplicate_discipline": multi,
        "decided_articles": len(decided_ids),
    }


def brier(oracle: list[dict], pipeline: list[dict], timeline: dict,
          priors: dict) -> dict:
    truth = timeline["truth"]["hypothesis"]
    end = timeline["end"]

    def at(series_rows: list[dict], date: str) -> dict:
        cur = dict(priors)
        for row in series_rows:
            if row["date"] <= date:
                cur = row["posteriors"]
        return cur

    out = {}
    # Binary per tranche at its authored deadline: did this tranche resolve
    # true by then? Only the truth tranche resolves 1, at the end date.
    deadlines = {d["hypothesis"]: d["date"]
                 for d in timeline.get("deadline_crossings", [])}
    deadlines.setdefault(truth, end)
    for hyp, date in sorted(deadlines.items()):
        outcome = 1.0 if hyp == truth else 0.0
        out[f"{hyp}@{date}"] = {
            lane: round((at(rows, date)[hyp] - outcome) ** 2, 4)
            for lane, rows in (("oracle", oracle), ("pipeline", pipeline))
        }
    # Full-vector Brier (sum of squared errors) at the end date.
    out[f"vector@{end}"] = {
        lane: round(sum(
            (at(rows, end)[h] - (1.0 if h == truth else 0.0)) ** 2
            for h in priors
        ), 4)
        for lane, rows in (("oracle", oracle), ("pipeline", pipeline))
    }
    return out


def report(div: list[dict], conf: dict, briers: dict, timeline: dict) -> str:
    lines = ["# Synthetic Meridia replay — pipeline vs oracle", ""]
    truth = timeline["truth"]["hypothesis"]
    final = div[-1]
    lines += [
        f"Authored truth: **{truth}** "
        f"(reopen {timeline['truth']['reopen_date']})",
        "",
        "## Trajectory divergence (TV distance, pipeline vs oracle)",
        "",
        "| date | TV | articles |",
        "|------|----|----------|",
    ]
    for row in div:
        flag = " (!)" if row["tv_distance"] > 0.05 else ""
        lines.append(f"| {row['date']} | {row['tv_distance']:.3f}{flag} | "
                     f"{', '.join(row['articles']) or '—'} |")
    lines += [
        "",
        f"Final-day posteriors — oracle: `{json.dumps(final['oracle'])}` "
        f"pipeline: `{json.dumps(final['pipeline'])}`",
        "",
        "## Matcher confusion (gold action → matcher decision)",
        "",
    ]
    kinds = sorted({k for row in conf["matrix"].values() for k in row})
    lines.append("| gold \\ matcher | " + " | ".join(kinds) + " |")
    lines.append("|---" * (len(kinds) + 1) + "|")
    for gold_action in sorted(conf["matrix"]):
        row = conf["matrix"][gold_action]
        lines.append(f"| {gold_action} | " + " | ".join(
            str(row.get(k, 0)) for k in kinds) + " |")
    routing = conf["indicator_routing"]
    lines += [
        "",
        f"- Indicator routing on moves: {routing['correct']} correct, "
        f"{routing['wrong']} wrong",
        f"- Distractors: {conf['distractors']}",
        f"- OBSERVE value deltas: "
        f"{json.dumps(conf['observe_value_deltas'])}",
        "- Duplicate discipline (move decisions per multi-article event): "
        f"{json.dumps(conf['duplicate_discipline'])}",
        "",
        "## Brier vs authored truth",
        "",
        "| checkpoint | oracle | pipeline |",
        "|------------|--------|----------|",
    ]
    for key, lanes in briers.items():
        lines.append(f"| {key} | {lanes['oracle']} | {lanes['pipeline']} |")
    return "\n".join(lines)


def main() -> int:
    # Windows consoles default to cp1252; the report is markdown with
    # arrows/dashes in it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", required=True)
    ap.add_argument("--pipeline", required=True)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--json", default="", help="machine-readable report path")
    args = ap.parse_args()

    timeline = json.loads(
        (FIXTURES / "timeline.json").read_text(encoding="utf-8")
    )
    topic = json.loads((FIXTURES / "topic.json").read_text(encoding="utf-8"))
    priors = {k: v["posterior"]
              for k, v in topic["model"]["hypotheses"].items()}

    oracle = load_jsonl(Path(args.oracle))
    pipeline = load_jsonl(Path(args.pipeline))
    decisions = load_jsonl(Path(args.decisions))

    div = divergence(oracle, pipeline, priors)
    conf = confusion(decisions, timeline)
    briers = brier(oracle, pipeline, timeline, priors)

    print(report(div, conf, briers, timeline))
    if args.json:
        Path(args.json).write_text(json.dumps({
            "divergence": div, "confusion": conf, "brier": briers,
        }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
