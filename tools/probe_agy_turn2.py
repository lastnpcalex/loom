#!/usr/bin/env python3
"""End-to-end probe for the agy NROL-operator turn-2 regression.

The invariant test (tests/test_operator_parity.py::test_gemini_operator_turn2_
forces_fresh_conv) stubs the subprocess, so it proves the launch/poller-mode
invariant but not that the *real* agy CLI behaves as assumed. This probe
closes that gap: it drives two real `agy` turns through
`gemini_client.run_gemini` and asserts turn 2 produces real output.

Repro of the pre-fix symptom: every operator session worked on turn 1 and
died on turn 2 with `[Error: Antigravity (agy) exited with no response]`
because the poller pinned to the turn-1 transcript while agy wrote its real
output to a fresh folder the poller never inspected. See
[[agy-operator-turn2-no-response]].

This is a *probe*, not production code. Run it manually against a real agy
install when verifying the fix, e.g. after a provider upgrade or a
gemini_client.py refactor that touches the launch/resume path:

    C:\\Python314\\python.exe tools\\probe_agy_turn2.py

Exits 0 on success (turn 2 yielded non-empty result_text), non-zero on the
regression or an agy launch failure. Sets a watchdog so a hung agy can't
hold the shell forever — bare headless agy is known to hang outside the
Loom harness (see [[agy-headless-smoke-hangs]]), so a timeout is treated
as a regression, not a pass.

NOTE: this probe launches real agy, which talks to real Gemini and may
incur API cost. It also relies on the operator workspace config already
being in place (GEMINI.md + .agents/mcp_config.json); run it from the repo
root or pass --cwd. The probe does NOT commit any operator state — agy's
PreToolUse hook denies mutations under LOOM_NROL_OPERATOR, and the prompt
is a harmless status request.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Probe lives in tools/, repo root is the parent. Make `import gemini_client`
# work without requiring the user to set PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import gemini_client  # noqa: E402

WATCHDOG_S = 180  # per-turn cap; bare agy hangs are a known failure mode


async def _drain_until_result(event_stream) -> tuple[str, bool, str | None]:
    """Consume the event stream until the terminal result event.

    Returns (result_text, is_error, session_id). is_error=True with empty
    result_text is the pre-fix turn-2 signature.
    """
    result_text = ""
    is_error = False
    session_id: str | None = None
    async for evt in event_stream:
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "session_info":
            session_id = evt.get("session_id") or session_id
        elif evt.get("type") == "result":
            result_text = evt.get("result_text") or ""
            is_error = bool(evt.get("is_error"))
            session_id = evt.get("session_id") or session_id
            return result_text, is_error, session_id
    return result_text, is_error, session_id


async def _run_one_turn(prompt: str, cwd: str, conv_id: int,
                        nrol_operator: bool,
                        resume_session_id: str | None = None,
                        fork_session: bool = False) -> tuple[str, bool, str | None]:
    """Launch one real agy turn and drain to the result event."""
    proc, event_stream = await gemini_client.run_gemini(
        prompt=prompt,
        cwd=cwd,
        conv_id=conv_id,
        server_port=0,  # no live Loom server; hook falls back to deny
        model="Gemini 3.5 Flash (High)",
        effort="high",
        permission_mode="default",
        resume_session_id=resume_session_id,
        fork_session=fork_session,
        nrol_operator=nrol_operator,
    )
    try:
        return await asyncio.wait_for(
            _drain_until_result(event_stream), timeout=WATCHDOG_S
        )
    except asyncio.TimeoutError:
        await gemini_client.cancel_gemini(proc)
        print(f"[probe] TIMEOUT after {WATCHDOG_S}s — bare agy hang? "
              f"(see [[agy-headless-smoke-hangs]])", file=sys.stderr)
        return "", True, resume_session_id
    finally:
        await gemini_client.cancel_gemini(proc)


async def main_async(args: argparse.Namespace) -> int:
    cwd = args.cwd or str(_REPO_ROOT)
    if not Path(cwd).is_dir():
        print(f"[probe] cwd not found: {cwd}", file=sys.stderr)
        return 2

    # --- Turn 1: fresh operator launch, no prior session ---
    print("[probe] === Turn 1 (fresh operator launch) ===", flush=True)
    t1_prompt = "Reply with the single word READY and nothing else."
    t1_text, t1_err, t1_session = await _run_one_turn(
        t1_prompt, cwd, conv_id=9001, nrol_operator=True,
    )
    print(f"[probe] turn 1: is_error={t1_err} session_id={t1_session!r} "
          f"text_len={len(t1_text)}", flush=True)
    print(f"[probe] turn 1 text (first 200): {t1_text[:200]!r}", flush=True)

    if t1_err or not t1_text:
        print("[probe] turn 1 failed — cannot exercise the turn-2 path "
              "(check agy install / API access)", file=sys.stderr)
        return 3
    if not t1_session or t1_session == "9001":
        print("[probe] turn 1 returned no real session id — the poller did "
              "not locate agy's fresh transcript folder. This is itself a "
              "regression signal.", file=sys.stderr)
        return 4

    # --- Turn 2: the regression path. server.py would pass the turn-1
    # cc_session_id and fork_session=True; the operator override must
    # neutralize both so the poller finds agy's fresh folder again. ---
    print("[probe] === Turn 2 (resume_id + fork, operator override) ===",
          flush=True)
    t2_prompt = "Reply with the single word GO and nothing else."
    t2_text, t2_err, t2_session = await _run_one_turn(
        t2_prompt, cwd, conv_id=9001,
        nrol_operator=True,
        resume_session_id=t1_session,
        fork_session=True,
    )
    print(f"[probe] turn 2: is_error={t2_err} session_id={t2_session!r} "
          f"text_len={len(t2_text)}", flush=True)
    print(f"[probe] turn 2 text (first 200): {t2_text[:200]!r}", flush=True)

    # --- The assertion the unit test can't make: real agy output on turn 2. ---
    if t2_err and not t2_text:
        print("[probe] FAIL: turn 2 produced no response — this is the "
              "'Antigravity (agy) exited with no response' regression. The "
              "operator override did not take the poller off the stale "
              "turn-1 transcript.", file=sys.stderr)
        return 1
    if not t2_text:
        print("[probe] FAIL: turn 2 result_text is empty (no error surfaced "
              "either). Pre-fix signature.", file=sys.stderr)
        return 1

    print("[probe] PASS: turn 2 yielded real output. The launch/poller-mode "
          "invariant holds against the real agy CLI.", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=None,
                    help="Workspace agy runs in (default: repo root). Must "
                         "already have GEMINI.md + .agents/mcp_config.json "
                         "for operator mode.")
    args = ap.parse_args()
    t0 = time.time()
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("[probe] interrupted", file=sys.stderr)
        return 130
    print(f"[probe] done in {time.time() - t0:.1f}s, exit={rc}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
