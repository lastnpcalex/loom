"""Invariant test: CC-family mode whitelists in the chat UI must include 'umans'.

The server rewrites ``mode`` from ``'claude'`` to ``'umans'`` at conversation-create
time when an ``umans-*`` model is picked (``server.py`` ``api_create_conversation``).
The frontend has a scattered set of hand-maintained mode-string whitelists that
decide whether to reveal the inline control bar (model dropdown / Plan-Act /
Canvas), persist CC settings, filter the conversation list, and inline project
image paths. Every one of these whitelists that lists ``claude`` together with
``codex``/``gemini`` is a CC-family gate and MUST also list ``umans`` — otherwise
an Umans conversation silently loses UI affordances.

This test is the cross-file-invariant guard for the bug class described in
``memory/project_loom_recurring_bug_shapes.md``: the server and the frontend
each maintain their own notion of the mode set, and they drift.

The heuristic: any single line in ``static/app.js`` or ``static/chat.js`` that
compares against BOTH ``'claude'`` and ``'gemini'`` (the CC-family fingerprint)
must also contain ``'umans'``. This catches the drift without enumerating sites
by hand.
"""

from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# app-Kayleighbot.js is an OneDrive-sync artifact, not the live file — excluded.
UI_JS_FILES = [STATIC_DIR / "app.js", STATIC_DIR / "chat.js"]

# A line is a "CC-family whitelist" if it mentions both claude and gemini mode
# literals. The umans-eligible set is the same CC family the server rewrites to.
_CC_FAMILY_FINGERPRINT = ("'claude'", "'gemini'")


def _cc_family_lines(text: str):
    """Yield (lineno, line) for lines that look like CC-family mode whitelists."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if all(tok in line for tok in _CC_FAMILY_FINGERPRINT):
            yield lineno, line


@pytest.mark.parametrize("js_file", UI_JS_FILES, ids=lambda p: p.name)
def test_cc_family_mode_whitelists_include_umans(js_file):
    """Every CC-family mode whitelist must include 'umans'.

    Fails with the offending file:line so the next drift is locatable. If a line
    legitimately should NOT include umans (none currently do), add an entry to
    the SKIP_LINES set below with a one-line reason — drift should be a
    conscious choice, not a silent omission.
    """
    SKIP_LINES = {
        # No exclusions yet. Add "file:lineno" (e.g. "app.js:586") here only
        # with a comment explaining why umans is intentionally excluded.
    }
    skip_key = f"{js_file.name}:"
    text = js_file.read_text(encoding="utf-8")
    offenders = []
    for lineno, line in _cc_family_lines(text):
        if any(str(lineno) == sk[len(skip_key):] for sk in SKIP_LINES if sk.startswith(skip_key)):
            continue
        if "'umans'" not in line:
            offenders.append(f"{js_file.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "CC-family mode whitelist(s) missing 'umans' — the server rewrites "
        "mode to 'umans' for umans-* models, so these gates drop Umans "
        "conversations:\n  " + "\n  ".join(offenders)
    )
