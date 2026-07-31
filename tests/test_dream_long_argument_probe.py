"""Track A Phase 0.5 gating probe: long-argument Dream tool-call stress test.

The §2.2 probes verified Dream returns clean tool_calls for short arguments
({"location":"Paris"}) and multi-turn use. But the real deliberation payload
shape is a *multi-paragraph* `analysis` string inside JSON tool arguments —
exactly the case that could trip a length/JSON-boundary bug in DiffusionGemma's
nuspy adapter. This probe closes that gap before the full A.2 tool surface is
built.

Lives here (tests/) rather than mcp_servers/nrol_ao_engine/ because it is a
gating probe, not engine code. It hits the live Dream sidecar at :8787 and is
skipped when that sidecar is down, so the normal suite never depends on the
GPU being warm.

Run the live probe explicitly:
    C:\\Python314\\python.exe -m pytest tests/test_dream_long_argument_probe.py -v
or force-run even when the sidecar is up but the module-level skip fires:
    LOOM_RUN_LIVE_DREAM_PROBE=1 pytest tests/test_dream_long_argument_probe.py -v
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

DREAM_HOST = os.environ.get("NROL_AO_DREAM_HOST") or os.environ.get("DREAM_HOST") or "http://127.0.0.1:8787"
DREAM_MODEL = os.environ.get("NROL_AO_DREAM_MODEL") or os.environ.get("DREAM_MODEL") or ""
N_CALLS = int(os.environ.get("DREAM_LONG_PROBE_N", "20"))
REQUEST_TIMEOUT = 300.0
MIN_ANALYSIS_LEN = 400  # matches §4.1 phase-3 metric floor
LEGAL_VERDICTS = {"COMMIT", "PARK", "SCHEMA_GAP", "DUPLICATE_OF"}

# A deliberation-shaped article the model can reason about. The matcher sees
# article text like this; the verdict tool must carry the analysis.
ARTICLE_TEXT = (
    "Article A1: Lloyd's List Intelligence reports tanker traffic through the "
    "Strait of Hormuz fell 12% in July 2026 month-over-month, the steepest "
    "single-month decline since the 2019 tanker wars. Marine insurance premiums "
    "for voyages transiting the strait rose 18% over the same window, with "
    "underwriters citing 'elevated seizure and mine risk.' Two Very Large "
    "Crude Carriers diverted to the Cape of Good Hope rather than transit the "
    "strait. Iranian Revolutionary Guard Corps Navy fast-boat activity was "
    "reported at elevated levels by the UK Maritime Trade Operations desk."
)

SYSTEM_PROMPT = (
    "You are a geopolitical risk analyst for a Bayesian forecasting system. "
    "For each article you are given, call the submit_verdict tool exactly once. "
    "The analysis field MUST be a detailed, multi-paragraph strategic and "
    "logical analysis of more than 400 characters, citing specific evidence "
    "from the article. Do not summarize in one sentence."
)

VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": (
            "Submit a deliberation verdict for a news article. The analysis "
            "field is the substantive record — write a detailed multi-paragraph "
            "strategic and logical analysis citing specific evidence from the "
            "article and specific indicators from the schema."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "Article identifier, e.g. A1, A2.",
                },
                "verdict": {
                    "type": "string",
                    "enum": ["COMMIT", "PARK", "SCHEMA_GAP", "DUPLICATE_OF"],
                    "description": "Deliberation verdict.",
                },
                "analysis": {
                    "type": "string",
                    "description": (
                        "Detailed multi-paragraph strategic and logical "
                        "analysis; cite specific evidence from the article and "
                        "specific indicators from the schema. Must be more "
                        "than 400 characters and span multiple paragraphs."
                    ),
                },
                "indicator_id": {
                    "type": "string",
                    "description": "Optional indicator id if verdict is COMMIT.",
                },
                "value": {
                    "type": "number",
                    "description": "Optional observed numeric value.",
                },
            },
            "required": ["article_id", "verdict", "analysis"],
            "additionalProperties": False,
        },
    },
}


def _dream_up() -> bool:
    """Best-effort liveness check on the Dream sidecar's /v1/models."""
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{DREAM_HOST}/v1/models")
            return r.status_code == 200 and bool(r.json().get("data"))
    except Exception:
        return False


# Skip the live probe entirely when the sidecar is down AND not forced on.
# This keeps the normal suite green without the GPU.
_run_live = os.environ.get("LOOM_RUN_LIVE_DREAM_PROBE") == "1"
pytestmark = pytest.mark.skipif(
    not (_run_live or _dream_up()),
    reason=f"Dream sidecar not reachable at {DREAM_HOST} (set LOOM_RUN_LIVE_DREAM_PROBE=1 to force)",
)


def _one_call(client: httpx.Client) -> dict:
    """Send a single long-argument tool-call request; return a result record."""
    payload = {
        "model": DREAM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": ARTICLE_TEXT},
        ],
        "tools": [VERDICT_TOOL],
        "tool_choice": "auto",
        "stream": False,
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    t0 = time.perf_counter()
    r = client.post(f"{DREAM_HOST}/v1/chat/completions", json=payload, timeout=REQUEST_TIMEOUT)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []

    record: dict = {
        "elapsed_s": round(elapsed, 1),
        "finish_reason": choice.get("finish_reason"),
        "content_empty": not content.strip(),
        "content_has_channel": ("<|channel>" in content) or ("<channel|>" in content),
        "n_tool_calls": len(tool_calls),
        "args_parse_ok": False,
        "args_has_channel": False,
        "verdict_legal": False,
        "analysis_present": False,
        "analysis_long_enough": False,
        "analysis_len": 0,
        "verdict": None,
        "malformed_json": False,
    }

    if not tool_calls:
        return record

    fn = (tool_calls[0].get("function") or {})
    raw_args = fn.get("arguments") or ""
    record["args_has_channel"] = ("<|channel>" in raw_args) or ("<channel|>" in raw_args)
    try:
        parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        if not isinstance(parsed, dict):
            raise ValueError("arguments not a JSON object")
    except (json.JSONDecodeError, ValueError):
        record["malformed_json"] = True
        return record

    record["args_parse_ok"] = True
    analysis = str(parsed.get("analysis") or "")
    verdict = str(parsed.get("verdict") or "")
    record["analysis_present"] = bool(analysis.strip())
    record["analysis_len"] = len(analysis)
    record["analysis_long_enough"] = len(analysis) >= MIN_ANALYSIS_LEN
    record["verdict"] = verdict
    record["verdict_legal"] = verdict in LEGAL_VERDICTS
    return record


@pytest.mark.live_dream
def test_dream_long_argument_tool_call_probe():
    """N=20 Dream tool-call requests with a required multi-paragraph analysis.

    Gates the full A.2 deliberation tool surface. Fails loudly if:
      - any call's arguments are not valid JSON (malformed_json rate > 0),
      - finish_reason is not 'tool_calls',
      - content or arguments carry <|channel>/<channel|> contamination,
      - the verdict is not a legal enum value,
      - the analysis is missing or shorter than MIN_ANALYSIS_LEN (400).

    Per-call failures are reported individually; the aggregate fails the test.
    A small allowance for a transient sidecar hiccup is tolerated (1/20) but
    anything structural fails.
    """
    results: list[dict] = []
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        for i in range(N_CALLS):
            results.append(_one_call(client))

    n = len(results)
    assert n == N_CALLS, f"expected {N_CALLS} calls, ran {n}"

    malformed = [r for r in results if r["malformed_json"]]
    assert not malformed, (
        f"{len(malformed)}/{n} calls returned malformed (non-JSON) tool "
        f"arguments — malformed_json rate is non-trivial. This is the exact "
        f"gating risk the probe exists to catch. First malformed elapsed "
        f"{malformed[0]['elapsed_s']}s, finish_reason={malformed[0]['finish_reason']}."
    )

    not_tool_calls = [r for r in results if r["finish_reason"] != "tool_calls"]
    assert not not_tool_calls, (
        f"{len(not_tool_calls)}/{n} calls did not return finish_reason='tool_calls'. "
        f"Reasons: {sorted({r['finish_reason'] for r in not_tool_calls})}"
    )

    no_tool_calls = [r for r in results if r["n_tool_calls"] == 0]
    assert not no_tool_calls, (
        f"{len(no_tool_calls)}/{n} calls returned no tool_calls object."
    )

    channel_contamination = [
        r for r in results if r["content_has_channel"] or r["args_has_channel"]
    ]
    assert not channel_contamination, (
        f"{len(channel_contamination)}/{n} calls leaked <|channel>/<channel|> "
        f"markup into content or tool-call arguments — the tool-call path was "
        f"supposed to be immune to the thought-channel leak (§2.2)."
    )

    illegal_verdicts = [r for r in results if not r["verdict_legal"] and r["args_parse_ok"]]
    assert not illegal_verdicts, (
        f"{len(illegal_verdicts)}/{n} calls returned a verdict not in "
        f"{sorted(LEGAL_VERDICTS)}. Values: "
        f"{sorted({r['verdict'] for r in illegal_verdicts})}"
    )

    short_analysis = [
        r for r in results if r["args_parse_ok"] and not r["analysis_long_enough"]
    ]
    assert not short_analysis, (
        f"{len(short_analysis)}/{n} calls returned an analysis shorter than "
        f"{MIN_ANALYSIS_LEN} chars. Lengths: "
        f"{sorted(r['analysis_len'] for r in short_analysis)}"
    )

    # Aggregate report for the test log.
    mean_len = sum(r["analysis_len"] for r in results) / n
    mean_elapsed = sum(r["elapsed_s"] for r in results) / n
    print(
        f"\n[Phase 0.5 probe] N={n} all-pass. "
        f"mean analysis_len={mean_len:.0f} chars, mean elapsed={mean_elapsed:.1f}s. "
        f"verdicts: {sorted({r['verdict'] for r in results})}"
    )
