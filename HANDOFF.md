# LOOM Handoff — Bug Hunt Context for the Next LLM

**Last update:** 2026-06-24, late-night (two sessions, 22:00 + 23:00)
**User state:** Exhausted, hitting recurring bug shapes, low patience for further churn. Read this *first* before patching anything.

---

## CRITICAL FINDING FROM SECOND SESSION (2026-06-24 23:00)

**The REAL root cause of "NROL operator agy doesn't use MCP tools" was NOT what the first session's fixes addressed.**

`gemini_client.py:564` (now patched) was launching agy with `--conversation <id>` on every turn, including operator turns. This makes agy *resume* its internal conversation, which has two devastating side effects:

1. **The tool registry is baked in at conv-creation time.** Resumed agy conversations DO NOT re-read `.agents/mcp_config.json`. So even with the correct NROL MCP config sitting on disk after this session's fixes, agy operator turns saw only the default tool set the conversation was created with (typically no MCP tools at all).
2. **agy's internal context accumulates across Loom turns.** After ~120 prior steps, agy auto-compacts BEFORE processing the new prompt. The user sees "Resuming from a compaction" on what they think is turn 1 of a fresh NROL conv — it's actually turn N=many on agy's side because the conv-id was reused.

**Evidence in `~/.gemini/antigravity-cli/cli.log` (23:04 capture):**
```
Print mode: conversation has 123 initial steps
Print mode: resuming conversation ba79b56f-5baa-4aae-8bcd-13dbb9fc5c2b
```

**Fix applied this session**: `gemini_client.py:run_gemini` no longer passes `--conversation` when `nrol_operator=True`. Operator turns are short, self-contained scan/triage requests where fresh agy state per turn is correct — and required so MCP gets re-registered each turn.

**Non-operator agy turns still use `--conversation` for continuity** — that's the right behavior there.

**Generalize this**: any time we add a new MCP server, new tool, or new system instruction for agy, *resumed* sessions won't see it. If you need an agent to pick up a config change, you must either start a fresh agy conv OR fork it. There is no "hot-reload MCP into a live agy session."

---

---

## Read This Before You Patch Anything

Loom is a coordination layer over four external CLIs (Claude Code, Codex, agy/Antigravity, Hermes). Each has its own workspace-discovery rules, config-file conventions, and tool-name vocabularies — **and none of those conventions are documented in our code**. Most regressions originate at the boundary between Loom's launch logic and these CLIs' undocumented expectations. Don't trust comments in our code that describe CLI behavior unless you've verified it from the CLI itself (see `--help`, source, or live probe).

Before doing anything else:
1. Read `AGENTS.md` (codebase map).
2. Read `Bug List for the Loom.md` (user's running UX complaint list).
3. Read `~/.claude/projects/.../memory/MEMORY.md` (running auto-memory).
4. Read this file.
5. **Do not trust `bug_analysis_report.md`** — it's an artifact from a single LLM session and was partially wrong (specifically: its claim about `.agents/AGENTS.md` vs `AGENTS.md` was incorrect — codex auto-loads `AGENTS.md` from cwd, agy auto-loads `GEMINI.md`).

---

## What Was Fixed Tonight (commit: see git log around 2026-06-24)

1. **agy operator MCP not loading** — `gemini_client._configure_operator` now writes `GEMINI.md` + `.agents/mcp_config.json` to BOTH cwd AND the agy-discovered workspace root (walks up to `.git` ancestor). Previously, agy ignored the operator workspace's files because its `.agents/` discovery anchors at the workspace root it walks to from cwd, not at cwd itself.
2. **Stale `.agents/mcp_config.json` contamination between modes** — `gemini_client._configure_permission_hook` now deletes any leftover `mcp_config.json` when launching a non-backstage, non-operator agy session. Previously a May-30 backstage test left a stale registration (port 8080, parent 123) that silently poisoned every later agy run in the same workspace.
3. **Stale loom-root `.agents/mcp_config.json` deleted** (was poisoning all neutral agy runs in the repo).
4. **UTF-8 BOM in `~/.gemini/config/mcp_config.json` deleted** — was breaking agy's user-scope MCP discovery on every launch with `invalid character '﻿'`.
5. **Streaming text flattened-newlines fix** — `static/style.css` `.streaming-text { white-space: pre-wrap; }`. The append-text-node optimization in commit `d41d58d` bypassed markdown rendering per chunk but inherited `.message-content`'s `white-space: normal`, collapsing all newlines into spaces until finalize.
6. **WS reconnect → permanent UI freeze loop** — `static/chat.js`:
   - Held `State._reconstructing = true` through both `loadMessages()` AND `_reconstructFromSnapshot()`. Previously it cleared between them, leaving a window where chunks-while-null re-requested snapshots, looping forever.
   - Gated `appendStreamChunk` and `_flushStreamBuffer`'s `_requestSnapshotIfStreaming()` calls on `!State._reconstructing`.

---

## What's Still Open (Deliberately Deferred)

### Provider classification alias mismatch (REAL, but low blast radius)
`server.is_gemini_model()` (server.py:4513–4521) aliases three agy-served labels — `"Claude Sonnet 4.6 (Thinking)"`, `"Claude Opus 4.6 (Thinking)"`, `"GPT-OSS 120B (Medium)"` — to gemini for **provider routing**. These ARE agy models; the routing is correct. But `model_context.is_gemini()` (model_context.py:75–78) only matches `startswith("gemini")`, so these labels fall through to `is_local_llama()` and get `THRESHOLD_LOCAL_LLAMA` instead of `THRESHOLD_GEMINI` (175k). (THRESHOLD_LOCAL_LLAMA was bumped 28k→220k this session because the actual llama-server Qwen runs 262k context — but the alias mismatch is still wrong on principle.)

**Fix shape**: move the alias set into `model_context.py` as a constant, have both `server.is_gemini_model` and `model_context.is_gemini` consume it. Add a test that asserts `is_gemini_model(m) == is_gemini(m)` for every model that appears in `models_config.json` and any UI-surfaced labels.

### "Prior messages disappear when session limit hits" — NOT YET FIXED
**Reported this session, not investigated to root cause.** Symptom: when a Claude turn dies on a 5-hour session limit, messages that were rendered before the failed turn disappear from the UI. The current-turn rate-limit message appears (server.py:5829-5864 writes it to the draft); but prior rendered messages are reported missing.

**Possible causes (untested):**
- Frontend `loadMessages()` after the error event refetches from DB and gets a shorter list than `State.messages` had — implies a server-side data loss
- Tree navigation accidentally switching branches on error
- `_cleanup_stale_drafts` (server.py:337) deletes empty assistant messages older than 30 min on STARTUP using `db.delete_branch(msg_id)` — if `delete_branch` is recursive (likely), and the cleanup runs at the wrong time or on the wrong row, it could cascade-delete user-visible message subtrees
- The screenshot showed the rate-limit message itself rendered with text content — so the *current* draft isn't an empty stale-draft target; the loss must be elsewhere

**Reproduction needed**: hit a real 5-hour limit (or simulate via mock), capture browser console + network traffic + DB state of the conv before and after. Without this trace, "fix" attempts will be guesswork.

### PowerShell/Bash failures in non-operator conversations
User reported "a lot more failed bash/powershell commands" but did not provide a specific reproducer. In operator mode, Bash is intentionally denied (`cc_permission_hook.py:352–359`) — that's by design (defense in depth against the operator using shell to bypass NROL). If failures are happening in non-operator convs, root cause is unknown; needs a concrete (conv_id, command, error message) triple to investigate.

### Operator-mode Bash deny is intentional, NOT a bug
NROL operators only get NROL MCP tools (typed transitions). If the user asks for shell access in operator mode, that's a policy change, not a fix. Confirm with the user before relaxing.

### Hook timeout bumped to 24h
`gemini_client._configure_permission_hook` PreToolUse timeout was bumped from 15 min → 24h. Intent: user can disconnect, come back, approve. Side effect: if the user never approves (or the WS-freeze bug we just fixed prevents them from seeing the prompt), the agy process sits blocked for hours. Watch for "agent appears hung" reports — confirm whether a permission prompt is silently pending in Loom before assuming hang.

---

## The Recurring Bug Shape

Across the bugs fixed tonight and historically, the same root-cause classes keep producing different symptoms:

| Class | Examples tonight | Why it recurs |
|---|---|---|
| Hidden CLI invariants | agy reads `.agents/` from `.git` ancestor, not cwd | Not documented in our code; each agent re-derives wrong |
| Gitignored runtime files with no lifecycle | `.agents/mcp_config.json` survived between modes | Nothing in code owns the "delete when not needed" path |
| Cross-file invariants without enforcement | `is_gemini_model` vs `is_gemini` | One file gets refactored, coupled file misses the change |
| Optimizations without visual verification | `d41d58d` streaming append-text bypass | Unit-test-clean, eyeball-broken; no UX regression test |
| Multi-agent churn on same surface | Many agents have touched provider clients | Each one preserves what's there, doesn't see the whole shape |
| Windows-specific tool fragility | UTF-8 BOM written by some Windows editor | No test reads these files; corruption persists silently |

---

## Infrastructure That Would Prevent Recurrence (Not Yet Built)

In priority order — each one would have prevented a specific bug class above:

1. **`tests/test_invariants.py`** — structure-not-behavior assertions:
   - `is_gemini_model(m) == is_gemini(m)` for every label in `models_config.json` + the UI alias map
   - every `streamingDiv = null` branch in `chat.js` is gated on `!State._reconstructing`
   - `.streaming-text` has an explicit non-default `white-space` rule in `style.css`
   - `gemini_client._configure_operator` writes to both cwd and the `.git` ancestor
2. **`.git/hooks/pre-commit`** that runs `tests/test_invariants.py` plus `test_operator_parity.py` + `test_nrol_ao_mcp.py` when touching their watch-list of files (provider clients, MCP code, `chat.js`, `style.css`, `cc_permission_hook.py`).
3. **Chrome-devtools verification harness** — see `dashboard/chrome_verify_recipe.md`. Drives the running Loom through `mcp__chrome-devtools__*` to assert on actual rendered DOM after a synthetic streaming payload. The 29 chrome-devtools MCP tools are already connected; what's missing is the recipe and a pytest wrapper.
4. **Doc the CLI conventions we depend on** — a single `CLI_QUIRKS.md` at repo root that lists, per CLI, what files it auto-loads, from where, what tool names it uses, and how its workspace root is determined. So the next agent doesn't have to re-derive it from logs and `--help`.

---

## Specific Gotchas (Pin These)

- **agy's workspace root** = nearest `.git` ancestor of cwd, NOT cwd. Its `.agents/` discovery anchors at that root. See `_agy_workspace_root()` in `gemini_client.py`.
- **agy auto-loads `GEMINI.md`**; codex auto-loads `AGENTS.md`; Claude takes everything via `--mcp-config` / `--append-system-prompt` inline (the most reliable provider).
- **NROL operator mode forces `mode = 'claude'` at conv-create time, then re-routes based on `cc_model`** (server.py:1828, 1870–1873). So an "NROL operator" conv with `cc_model='Gemini 3.5 Flash (High)'` is stored as `mode='gemini'`.
- **`cc_permission_hook.py` is the tool-blocking layer for agy** — agy has no `excludeTools` flag (verified 2026-06-11), so the hook's NROL deny-list IS the lockdown.
- **Per-conversation runtime files in `.agents/` have no automatic cleanup unless the launch flow explicitly handles it.** This is the invariant we added tonight.
- **OneDrive sync can corrupt SQLite locks and config files.** See memory: `project_onedrive_db_sync`.
- **Use `C:\Python314\python.exe` explicitly**, not bare `python`. See memory: `project_python_interpreter`.

---

## When in Doubt

1. The user prefers conceptual assessment BEFORE editing — say what you think the cause is, then propose a fix shape, then ask before applying. See memory: `feedback_evaluate_before_editing`.
2. Ground every behavioral claim in code, a probe, or a test. Design intent ≠ implementation. See memory: `feedback_ground_claims_in_code`.
3. If you're about to do prevention work (tests, harnesses, scaffolding), this is what the user has explicitly asked for. It is NOT out of scope, even if it doesn't address the immediate bug. See memory: `feedback_prevention_must_be_explicit_ask`.
