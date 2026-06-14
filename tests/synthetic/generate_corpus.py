"""Synthetic-topic corpus generator — Haiku writes the news, the timeline owns the truth.

For each event in tests/fixtures/synthetic_topic/timeline.json this script has
Haiku (via the claude CLI) write the event's articles: 1 = single coverage,
2-3 = duplicate coverage of one causal event (the dedup discipline the
pipeline lane must demonstrate). Gold labels are copied from the timeline by
this script — Haiku never sees the indicator schema and never emits labels,
so the corpus can't leak the answer key into its own prose beyond what a real
news article about the event would carry.

Deliberate perception noise, because the matcher is what the corpus tests:
trade-press duplicates express observable values in awkward units (ships/day
against a stated baseline instead of percent), distractors are plausible
regional news matching no indicator, E06/E07 are authored near-misses
(SCHEMA_GAP / PARK bait).

The corpus is committed to the repo: generation is one-time and replays are
deterministic and API-free afterward. Regenerate single events after the
human spot-check with --events.

Usage:
    python tests/synthetic/generate_corpus.py [--events E01,D02] [--dry-run]
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures" / "synthetic_topic"
CORPUS_DIR = FIXTURES / "corpus"
MODEL = "claude-haiku-4-5-20251001"

# The world bible: every article draws names from here so the corpus is
# internally consistent and contains no real-world entities. The scenario is
# structured like the live hormuz topic but shares nothing nameable with it.
WORLD = """\
THE WORLD (fictional; never hint that it is fictional, never use real-world
country, city, organization, or company names — only the names below):

- The Strait of Meridia: the chokepoint between the Varessan Sea and the open
  ocean. Pre-crisis traffic: roughly 140 commercial transits per day. Closed
  by blockade since May 2026.
- The Republic of Varessa (capital Ostrev): the blockading power. Its navy is
  the Varessan Coastal Forces.
- The Meridian Compact: the riparian coalition opposing the blockade. Member
  states: Khoreth, Sundaria, and Elbar.
- Qoros: a neutral city-state; hosts mediation talks and the marine insurance
  market (the Qoros Marine Underwriters' Association, which publishes the
  Meridia Route Normalization Index, 0 = full war-risk crisis pricing,
  100 = pre-crisis pricing).
- HarborTrack: the maritime tracking data provider whose transit counts the
  shipping world quotes.
"""

# Entities that come into existence mid-timeline: only events on/after the
# entity's birth date may know about it. A July article casually referencing
# the August escort framework is an anachronism that would bait the matcher
# into firing early.
WORLD_LATER = [
    ("2026-08-20",
     "- Operation Open Water: the five-nation escort framework (Khoreth, "
     "Sundaria, Elbar, plus distant partners Aldenport and Virelles), "
     "announced 2026-08-20.",
     re.compile(r"open\s+water", re.IGNORECASE)),
]


def world_for(date: str) -> str:
    extra = [text for born, text, _ in WORLD_LATER if date >= born]
    return WORLD + ("\n".join(extra) + "\n" if extra else "")

OUTLETS = [
    ("Qoros Wire", "neutral newswire; terse, sourced, dateline style"),
    ("Harbor & Hull", "shipping trade press; operational and market detail, "
     "quotes brokers and underwriters"),
    ("Continental News Service", "international wire; broader political frame"),
    ("The Meridian Courier", "coalition-side regional daily"),
    ("The Ostrev Gazette", "Varessan state-leaning daily; official framing"),
    ("Sundaria Times", "coalition member-state daily"),
]

# Per-article-index angle: duplicates must be genuinely different tellings of
# the same causal event, not copy-paste — that is what the dedup gate sees.
ANGLES = [
    "Straight same-day wire report of the event.",
    "Trade-press angle: operational/market detail, practitioner quotes. If the "
    "event involves a measured value, express it in ABSOLUTE terms with the "
    "baseline stated (e.g. ships per day against the ~140/day pre-crisis "
    "norm, or index points out of 100) rather than repeating the headline "
    "percentage.",
    "Next-morning recap/analysis piece that references earlier coverage of "
    "the same event ('first reported by...') and adds reaction quotes.",
]

# Published-time stagger for duplicate coverage of one event (same simulated
# day; the third piece reads as next-morning but stays on the event date so
# day-grouping in the replay matches the timeline).
HOURS = ["08:10:00", "12:40:00", "17:25:00"]

# Real-world leakage scan: the corpus must stay fictional. Word-boundary
# matches, case-insensitive.
FORBIDDEN = [
    "hormuz", "iran", "tehran", "oman", "uae", "saudi", "israel", "gulf",
    "united states", "u\\.s\\.", "us navy", "washington", "pentagon", "nato",
    "britain", "british", "china", "chinese", "russia", "russian", "europe",
    "lloyd", "suez", "panama", "red sea",
]
FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(FORBIDDEN) + r")\b", re.IGNORECASE
)

EVENT_DIRECTIVES = {
    "E03": "Each article must carry the transit level: headline piece quotes "
           "HarborTrack at about 4 percent of the pre-crisis baseline; the "
           "trade piece gives roughly six transits a day against the ~140/day "
           "norm instead of a percentage.",
    "E05": "The article must state the Meridia Route Normalization Index "
           "level of 12 out of 100 and what the index measures.",
    "E06": "The point is coverage WITHDRAWAL, not price: two syndicates stop "
           "offering Meridia hull cover entirely. State explicitly that the "
           "normalization index was NOT updated — availability, not price, "
           "changed. Do not give any index number.",
    "E07": "Deliberately vague: both sides confirm a de-escalation "
           "understanding but officials decline to give any timetable, any "
           "transit arrangements, or published terms. No numbers, no dates, "
           "no named mechanism.",
    "E09": "Transit recovery to about 15 percent of baseline; trade piece "
           "may use about 20 ships a day against the ~140/day norm.",
    "E11": "The decree names the reopening date: 12 September 2026, under "
           "escort and insurance protocols. Every article carries the date.",
    "E11b": "Discovery of a newly laid floating naval mine near the central shipping lanes. "
            "Postponement of scheduled Sept 12 reopening to Sept 20. Make sure to specify "
            "the new date Sept 20 and the reason (mine hazard).",
    "E11c": "Following intensive sweep operations, joint naval authorities issue a new "
            "decree rescheduling the formal reopening to 20 September. Every article must "
            "state the new reopening date Sept 20.",
    "E12": "First convoys through; HarborTrack shows daily transits near 38 "
           "percent of baseline (trade angle: low fifties of ships per day).",
    "E13": "Index rises to 64 of 100 as war-risk premiums fall for escorted "
           "transits.",
    "E14": "Fifth consecutive day at 78 percent of the pre-crisis baseline "
           "(roughly 109 ships a day); brokers call the recovery sustained.",
    "D01": "Distractor: regional fishing-quota dispute 200nm east of the "
           "strait. Must NOT mention the blockade's status, transit volumes, "
           "insurance, talks, escorts, clearance, or reopening prospects.",
    "D02": "Distractor: a port east of the strait announces a five-year "
           "container terminal expansion. Explicitly framed as long-term "
           "planning unrelated to the blockade. No transit or insurance data.",
    "D03": "Distractor: Varessan energy ministry reports record domestic "
           "refinery output; analysts debate sanctions leakage. No strait "
           "operations content.",
    "D04": "Distractor: a cruise operator will resume regional itineraries "
           "NEXT SEASON citing improving outlook. Forecast-flavored optimism "
           "only — no transit data, no operational changes now.",
}


def _label(event: dict, idx: int) -> str:
    if event["gold"]["action"] == "IGNORE":
        return "DISTRACTOR"
    return event["id"] if idx == 0 else f"DUPLICATE-of-{event['id']}"


def build_brief(event: dict) -> str:
    n = event["articles"]
    lines = [
        world_for(event["date"]),
        f"THE EVENT (this actually happened in this world on {event['date']}):",
        event["summary"],
        "",
    ]
    extra = EVENT_DIRECTIVES.get(event["id"])
    if extra:
        lines += [f"COVERAGE DIRECTIVE: {extra}", ""]
    lines.append(
        f"TASK: Write {n} news article(s) covering this one event, as JSON. "
        "Newswire register, 150-280 words of body each, concrete and "
        "specific, datelined to the event date. Do not editorialize about "
        "what the event means for any forecast."
    )
    for i in range(n):
        id_num = int(re.sub(r"\D", "", event["id"]))
        outlet, voice = OUTLETS[(id_num + i) % len(OUTLETS)]
        lines.append(f"Article {i + 1} — outlet: {outlet} ({voice}). "
                     f"Angle: {ANGLES[i]}")
    lines.append(
        '\nOUTPUT: ONLY a JSON array of exactly '
        f'{n} object(s), each {{"headline": str, "body": str}}. '
        "No markdown fences, no commentary."
    )
    return "\n".join(lines)


def call_haiku(prompt: str) -> str:
    # Use local llama-server wrapper instead of claude CLI
    import sys
    sys.path.insert(0, str(HERE.parent.parent))
    from mcp_servers.nrol_ao import llama
    response = llama.chat(
        prompt,
        system_prompt="You are a creative writer generating fictional news articles based on a brief. Return only the requested JSON array.",
        temperature=0.7,
        max_tokens=4096,
        disable_thinking=True,
    )
    return response.get("text", "")


def parse_articles(raw: str, expected: int, event_date: str = "") -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.MULTILINE)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON array in output: {text[:200]!r}")
    arts = json.loads(text[start:end + 1])
    if not isinstance(arts, list) or len(arts) != expected:
        raise ValueError(f"expected {expected} articles, got "
                         f"{len(arts) if isinstance(arts, list) else type(arts)}")
    for a in arts:
        if not a.get("headline") or not a.get("body"):
            raise ValueError("article missing headline or body")
        wc = len(a["body"].split())
        if not 80 <= wc <= 420:
            raise ValueError(f"body length {wc} words outside 80-420")
        text = a["headline"] + " " + a["body"]
        leak = FORBIDDEN_RE.search(text)
        if leak:
            raise ValueError(f"real-world leakage: {leak.group(0)!r}")
        for born, _, pattern in WORLD_LATER:
            if event_date and event_date < born and pattern.search(text):
                raise ValueError(
                    f"anachronism: {pattern.pattern!r} before {born}"
                )
    return arts


def generate_event(event: dict, attempts: int = 3) -> list[dict]:
    brief = build_brief(event)
    last_err = None
    for _ in range(attempts):
        try:
            return parse_articles(call_haiku(brief), event["articles"],
                                  event["date"])
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            print(f"  retry {event['id']}: {exc}")
    raise RuntimeError(f"{event['id']}: generation failed after "
                       f"{attempts} attempts: {last_err}")


def write_corpus_files(event: dict, articles: list[dict]) -> list[Path]:
    paths = []
    for i, art in enumerate(articles):
        id_num = int(re.sub(r"\D", "", event["id"]))
        outlet, _ = OUTLETS[(id_num + i) % len(OUTLETS)]
        slug_outlet = outlet.lower().replace(" & ", "-").replace(" ", "-")
        art_id = f"{event['id']}-a{i + 1}"
        record = {
            "id": art_id,
            "event_id": event["id"],
            "label": _label(event, i),
            "gold": event["gold"],
            "published": f"{event['date']}T{HOURS[i % len(HOURS)]}+00:00",
            "outlet": outlet,
            "url": f"synthetic://{slug_outlet}/{art_id}",
            "headline": art["headline"].strip(),
            "body": art["body"].strip(),
            "generator": {
                "model": MODEL,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        path = CORPUS_DIR / f"{art_id}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        paths.append(path)
    return paths


def scan_corpus() -> int:
    """Validate committed corpus files: leakage, anachronisms, coverage."""
    timeline = json.loads(
        (FIXTURES / "timeline.json").read_text(encoding="utf-8")
    )
    expected = {
        f"{e['id']}-a{i + 1}": e
        for e in timeline["events"] if e.get("articles", 0) >= 1
        for i in range(e["articles"])
    }
    problems = []
    seen = set()
    for path in sorted(CORPUS_DIR.glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        seen.add(rec["id"])
        text = rec["headline"] + " " + rec["body"]
        leak = FORBIDDEN_RE.search(text)
        if leak:
            problems.append(f"{rec['id']}: real-world leakage {leak.group(0)!r}")
        for born, _, pattern in WORLD_LATER:
            if rec["published"][:10] < born and pattern.search(text):
                problems.append(
                    f"{rec['id']}: anachronism {pattern.pattern!r} before {born}"
                )
        event = expected.get(rec["id"])
        if event is None:
            problems.append(f"{rec['id']}: not in timeline")
        elif rec["gold"] != event["gold"]:
            problems.append(f"{rec['id']}: gold label drifted from timeline")
    for missing in sorted(set(expected) - seen):
        problems.append(f"{missing}: missing from corpus")
    for p in problems:
        print(f"PROBLEM {p}")
    print(f"scanned {len(seen)} articles: "
          f"{'OK' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="",
                    help="comma-separated event ids to (re)generate")
    ap.add_argument("--dry-run", action="store_true",
                    help="print briefs, call nothing")
    ap.add_argument("--scan", action="store_true",
                    help="validate existing corpus files, call nothing")
    args = ap.parse_args()

    if args.scan:
        return scan_corpus()

    timeline = json.loads(
        (FIXTURES / "timeline.json").read_text(encoding="utf-8")
    )
    only = {e.strip() for e in args.events.split(",") if e.strip()}
    events = [
        e for e in timeline["events"]
        if e.get("articles", 0) >= 1 and (not only or e["id"] in only)
    ]

    if args.dry_run:
        for event in events:
            print(f"===== {event['id']} ({event['articles']} articles) =====")
            print(build_brief(event))
            print()
        return 0

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for event in events:
        print(f"{event['id']} ({event['date']}, {event['articles']} articles)...")
        articles = generate_event(event)
        for path in write_corpus_files(event, articles):
            print(f"  wrote {path.name}")
            total += 1
    print(f"done: {total} articles across {len(events)} events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
