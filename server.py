"""FastAPI server with REST endpoints and WebSocket streaming."""

import sys

# Windows defaults stdout/stderr to the system codepage (CP1252), which cannot
# encode characters outside Latin-1 (e.g. Greek, Cyrillic).  Reconfigure to
# UTF-8 so print() / logging never raises UnicodeEncodeError.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import json
import os
import sqlite3
import subprocess
import time
import time as _time  # alias used by generation_ms timing in CC/OODA/Weave handlers
import uuid
from pathlib import Path

from fastapi import (
    Body,
    FastAPI,
    Header,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    File,
    HTTPException,
    Request,
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.websockets import WebSocketState

from canvas_slug import generate_canvas_slug, is_valid_slug

import httpx
import database as db
from config import config
from character_loader import (
    load_all_characters,
    load_character,
    save_character,
    delete_character,
    load_all_personas,
    load_persona,
    save_persona,
    delete_persona,
    load_all_lore,
    load_lore_entry,
    save_lore,
    delete_lore,
)
from local_llm import health_check, stream_chat, sync_chat, describe_image
from ooda_harness import (
    build_ooda_system_prompt,
    parse_ooda_block,
    extract_post_ooda_prose,
    execute_ooda_reads,
    execute_ooda_updates,
    build_pass2_context,
)
from prompt_engine import (
    build_system_prompt,
    assemble_prompt,
    get_style_nudge,
    STYLE_NUDGES,
)
from context_manager import get_context_for_generation
import claude_client
import gemini_client
import hermes_client
import codex_client
import model_context
import local_summary
from skill_scanner import get_all_skills, BUILTIN_COMMANDS

from contextlib import asynccontextmanager

CRON_DB_PATH = Path("loom_cron.db")
CRON_ALLOWED_SUFFIXES = {".py", ".ps1", ".js"}
CRON_MIN_INTERVAL_SECONDS = 60
CRON_DENY_PATTERNS = (
    "rm -rf",
    "remove-item -recurse",
    "remove-item -force -recurse",
    "del /s",
    "erase /s",
    "rmdir /s",
    "rd /s",
    "format ",
    "shutdown ",
    "reboot",
    "mkfs",
    "diskpart",
)
_cron_scheduler_task: asyncio.Task | None = None
_cron_running: set[int] = set()

# ── Canvas CLAUDE.md template ──
CANVAS_CLAUDE_MD = """\
# Interactive Canvas

This directory is a live canvas rendered in the user's browser as an iframe.
Everything you write here is immediately visible — the iframe auto-refreshes
when you save files.

## Structure

- `index.html` — entry point, always loaded by the iframe
- `triggers/*.md` — prompt templates for SDK-driven interactions (see below)
- All other files (CSS, JS, images) use relative paths from index.html

## Canvas SDK

Include the SDK to let the canvas page trigger Loom actions:

```html
<script src="/static/canvas-sdk.js"></script>
```

### API

| Method | Description |
|--------|-------------|
| `Loom.send(prompt, opts?)` | Send a chat message and trigger AI generation. `opts`: `{imagePaths: [...], parentId: int}` |
| `Loom.upload(file)` | Upload a `File` object. Returns `{path, url, is_image}` |
| `Loom.uploadAndSend(file, prompt)` | Upload a file then send a message referencing it |
| `Loom.loadTrigger(name, vars?)` | Load `triggers/{name}.md`, interpolate `{{key}}` → value |
| `Loom.dropZone(el, opts?)` | Make an element a file drop target. `opts`: `{trigger: 'name'}` or `{prompt: '...'}` |
| `Loom.getConvId()` | Returns `{convId: int}` — the current conversation ID |
| `Loom.on(event, handler)` | Listen for Loom events |

### Trigger templates

Put prompt templates in `canvas/triggers/*.md`. They're plain text with `{{variable}}`
placeholders that get interpolated by `Loom.loadTrigger()`.

Example `triggers/analyze.md`:
```
Analyze the uploaded file "{{filename}}" and update the canvas with a visualization.
Write your results to canvas/index.html.
```

### Drop zone pattern

```javascript
const dropArea = document.getElementById('drop-area');
Loom.dropZone(dropArea, { trigger: 'analyze' });
// When a file is dropped:
// 1. File is uploaded to Loom
// 2. triggers/analyze.md is loaded and {{filename}} is filled in
// 3. Message is sent, AI generates a response
// 4. AI writes to canvas/, iframe auto-refreshes
```

### Direct send pattern

```javascript
document.getElementById('run-btn').addEventListener('click', () => {
    const userInput = document.getElementById('custom-input').value;
    Loom.send(`Update the canvas dashboard: ${userInput}`);
});
```

## Guidelines

- The canvas runs inside a sandboxed iframe with `allow-scripts allow-same-origin`
- You have full access to the Loom REST API (same-origin) from canvas JS
- Keep index.html self-contained or use relative imports
- The user sees the canvas in tree view as a thumbnail and can click to fullview
- Build progressively — start simple, layer in interactivity
- When using the SDK, the AI response creates a new branch in the conversation tree
"""


@asynccontextmanager
async def lifespan(app):
    global _cron_scheduler_task
    await db.init_db()
    await _cron_init_db()
    # Clean up stale draft messages (empty assistant msgs older than 30 min)
    await _cleanup_stale_drafts()
    # Reap orphan CC/ollama subprocesses from prior server instances.
    await _reap_orphan_generations()
    # Re-broadcast pending permission requests from DB (server restart)
    await _reload_pending_permissions()
    # Ensure Ollama is running (launches it if not)
    asyncio.create_task(_ensure_ollama())
    # Warm the local-model caches once Ollama is up so the settings panel
    # opens instantly. Runs as a background task so we don't block startup
    # while waiting on a slow Ollama probe.
    async def _warm_model_caches():
        await asyncio.sleep(2)  # let _ensure_ollama finish first
        try:
            await _refresh_local_models_cache()
            await _refresh_vision_models_cache()
            await _refresh_all_engines_cache()
            print(f"[CACHE] Warmed model caches: {len((_LOCAL_MODELS_CACHE or {}).get('models', []))} models, {len((_VISION_MODELS_CACHE or {}).get('models', []))} vision-capable, {len((_ALL_ENGINES_CACHE or {}).get('models', []))} cross-engine")
        except Exception as e:
            print(f"[CACHE] Initial warm failed (will fetch on demand): {e}")
    asyncio.create_task(_warm_model_caches())
    # Sync model IDs from the running Llama Server into config
    asyncio.create_task(_update_model_ids_on_startup())

    async def _refresh_remote_models_on_startup():
        await asyncio.sleep(3)
        try:
            print("[STARTUP] Refreshing Anthropic models...")
            await api_cc_models_refresh()
            print("[STARTUP] Anthropic models refreshed successfully")
        except Exception as e:
            print(f"[STARTUP] Anthropic model refresh skipped/failed: {e}")

        try:
            print("[STARTUP] Refreshing Codex models from cache...")
            local_models = load_local_codex_models()
            if local_models:
                for group in CC_MODELS:
                    if group["group"].startswith("ChatGPT Codex"):
                        group["models"] = local_models
                        print(f"[STARTUP] Codex models updated from cache: {len(local_models)} models loaded")
                        break
        except Exception as e:
            print(f"[STARTUP] Codex model refresh failed: {e}")

    asyncio.create_task(_refresh_remote_models_on_startup())
    _cron_scheduler_task = asyncio.create_task(_cron_scheduler_loop())
    yield
    if _cron_scheduler_task:
        _cron_scheduler_task.cancel()
        try:
            await _cron_scheduler_task
        except asyncio.CancelledError:
            pass
    await db.close_db()


def _pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check. Returns False on unknown error."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _kill_pid(pid: int) -> bool:
    """Force-kill a process. Returns True if the kill was dispatched."""
    if not pid or pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import subprocess
            # /T kills the child tree too — CC spawns sub-procs (e.g. bash for tools)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        else:
            import signal as _signal
            os.kill(pid, _signal.SIGKILL)
        return True
    except Exception as e:
        print(f"[REAP] Failed to kill pid={pid}: {e}")
        return False


async def _reap_orphan_generations():
    """On server startup, any row in active_generations came from a prior
    server instance that didn't finalize before shutdown. Kill the subprocess
    if it's still alive, mark the draft errored, and delete the tracking row."""
    rows = await db.list_active_generations()
    if not rows:
        print("[REAP] No orphan generations to check")
        return
    print(f"[REAP] Found {len(rows)} tracked generation(s) from prior run")
    for row in rows:
        pid = row.get("pid")
        draft_msg_id = row.get("draft_msg_id")
        conv_id = row.get("conv_id")
        alive = _pid_alive(pid) if pid else False
        if alive:
            killed = _kill_pid(pid)
            print(f"[REAP] conv={conv_id} draft={draft_msg_id} pid={pid} alive — killed={killed}")
        else:
            print(f"[REAP] conv={conv_id} draft={draft_msg_id} pid={pid} already dead")
        # Mark draft as errored if it's still an empty draft (don't clobber
        # finalized content).
        try:
            m = await db.get_message(draft_msg_id)
            if m and (m.get("role") == "assistant") and not (m.get("content") or "").strip():
                await db.update_message_content(
                    draft_msg_id,
                    content="[Error: generation orphaned by server restart]",
                )
        except Exception as e:
            print(f"[REAP] Could not update draft {draft_msg_id}: {e}")
        await db.unregister_active_generation(draft_msg_id)
    print(f"[REAP] Reaped {len(rows)} orphan generation(s)")


async def _cleanup_stale_drafts():
    """Remove empty assistant draft messages older than 30 minutes on startup."""
    import time

    cutoff = time.time() - 1800  # 30 minutes
    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT id FROM messages WHERE role='assistant' AND (content IS NULL OR content='') AND content_blocks IS NULL AND created_at < ?",
        (cutoff,),
    )
    if rows:
        ids = [r["id"] for r in rows]
        print(f"[STARTUP] Cleaning up {len(ids)} stale draft(s): {ids}")
        for msg_id in ids:
            await db.delete_branch(msg_id)
    await conn.close()


async def _reload_pending_permissions():
    """Load pending permissions from DB and re-broadcast to WebSockets."""
    import time

    # Clean up very old permissions (1 hour+) before reloading
    await db.delete_old_pending_permissions(time.time() - 3600)
    print(f"[STARTUP] Cleaned up permissions older than 1 hour")

    # Load pending permissions for all conversations
    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT request_id, conv_id, tool_name, tool_input, input_summary, started_at FROM pending_permissions ORDER BY started_at"
    )

    # Build permission messages
    perm_messages = {}
    for row in rows:
        perm_msg = {
            "type": "permission_request",
            "request_id": row["request_id"],
            "conv_id": row["conv_id"],
            "tool_name": row["tool_name"],
            "tool_input": row["tool_input"],
            "input_summary": row["input_summary"],
        }
        perm_messages[row["conv_id"]] = perm_msg
        print(
            f"[STARTUP] Reloading pending permission: {row['request_id']} for conv={row['conv_id']} tool={row['tool_name']}"
        )

    # Broadcast to active WebSockets
    dead_pairs = []
    sent_count = 0
    for cid, clients in list(_active_websockets.items()):
        for ws in list(clients):
            try:
                if cid in perm_messages:
                    await ws.send_json(perm_messages[cid])
                    sent_count += 1
            except Exception as e:
                print(f"[STARTUP] Failed to reload perm to conv={cid}: {e}")
                dead_pairs.append((cid, ws))
    print(f"[STARTUP] Reloaded {sent_count} permission(s) to active WebSockets")

    # Clean up from DB
    for row in rows:
        await db.delete_pending_permission(row["request_id"])
    print(f"[STARTUP] Cleared {len(rows)} pending permissions from DB")


async def _ensure_ollama():
    """Check if Ollama is running; launch it in the background if not."""
    import shutil
    import subprocess

    try:
        status = await health_check()
        if status.get("status") == "ok":
            print(
                f"[STARTUP] Ollama already running — {len(status.get('models', []))} model(s) available"
            )
            return
    except Exception:
        pass

    ollama_path = shutil.which("ollama")
    if not ollama_path:
        print("[STARTUP] Ollama not found on PATH — skipping auto-launch")
        return

    print(f"[STARTUP] Ollama not responding — launching: {ollama_path} serve")
    try:
        env = os.environ.copy()
        env["OLLAMA_KV_CACHE_TYPE"] = env.get("OLLAMA_KV_CACHE_TYPE", "q8_0")
        subprocess.Popen(
            [ollama_path, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Wait for it to come up (up to 15s)
        for i in range(15):
            await asyncio.sleep(1)
            try:
                status = await health_check()
                if status.get("status") == "connected":
                    print(
                        f"[STARTUP] Ollama ready after {i+1}s — {len(status.get('models', []))} model(s)"
                    )
                    return
            except Exception:
                pass
        print(
            "[STARTUP] Ollama launched but not yet responding after 15s — will retry on first use"
        )
    except Exception as e:
        print(f"[STARTUP] Failed to launch Ollama: {e}")



def _cron_now() -> float:
    return time.time()


def _cron_iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _cron_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CRON_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _cron_init_db_sync() -> None:
    with _cron_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id INTEGER NOT NULL,
                workspace TEXT NOT NULL,
                script TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                every_seconds INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                archived_at REAL,
                last_run_at REAL,
                last_finished_at REAL,
                next_run_at REAL NOT NULL,
                last_status TEXT,
                last_exit_code INTEGER,
                last_output TEXT,
                last_error TEXT,
                run_count INTEGER NOT NULL DEFAULT 0,
                skip_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cron_active_due ON cron_jobs(archived, enabled, next_run_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cron_conv ON cron_jobs(conv_id, archived)")
        conn.commit()


async def _cron_init_db() -> None:
    await asyncio.to_thread(_cron_init_db_sync)


def _cron_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("created_at", "updated_at", "archived_at", "last_run_at", "last_finished_at", "next_run_at"):
        d[f"{key}_display"] = _cron_iso(d.get(key))
    return d


def _cron_list_jobs_sync(include_archived: bool = False, conv_id: int | None = None) -> list[dict]:
    where = []
    args: list = []
    if not include_archived:
        where.append("archived = 0")
    if conv_id is not None:
        where.append("conv_id = ?")
        args.append(conv_id)
    sql = "SELECT * FROM cron_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY archived ASC, enabled DESC, next_run_at ASC, id DESC"
    with _cron_connect() as conn:
        return [_cron_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


async def _cron_list_jobs(include_archived: bool = False, conv_id: int | None = None) -> list[dict]:
    return await asyncio.to_thread(_cron_list_jobs_sync, include_archived, conv_id)


def _cron_validate_script(script: str, workspace: str) -> tuple[Path, str]:
    if not isinstance(script, str) or not script.strip():
        raise HTTPException(status_code=400, detail="script is required")
    if "\x00" in script or script.startswith(("~", "/", "\\")) or ":" in Path(script).parts[0]:
        raise HTTPException(status_code=400, detail="script must be a relative path inside the conversation workspace")
    rel = Path(script)
    if any(part in ("", ".", "..") for part in rel.parts):
        raise HTTPException(status_code=400, detail="script path may not contain traversal segments")
    workspace_path = Path(workspace).resolve()
    target = (workspace_path / rel).resolve()
    try:
        target.relative_to(workspace_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="script must stay inside the conversation workspace")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="script file does not exist")
    if target.suffix.lower() not in CRON_ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"script suffix must be one of {sorted(CRON_ALLOWED_SUFFIXES)}")
    try:
        text = target.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not read script for safety scan: {e}")
    for pattern in CRON_DENY_PATTERNS:
        if pattern in text:
            raise HTTPException(status_code=400, detail=f"script contains rejected command pattern: {pattern}")
    return target, str(rel).replace("\\", "/")


def _cron_interval_from_payload(data: dict) -> int:
    seconds = data.get("every_seconds")
    if seconds is None and data.get("every_minutes") is not None:
        seconds = int(data["every_minutes"]) * 60
    if seconds is None and data.get("every_hours") is not None:
        seconds = int(data["every_hours"]) * 3600
    try:
        seconds = int(seconds)
    except Exception:
        raise HTTPException(status_code=400, detail="provide every_seconds, every_minutes, or every_hours")
    if seconds < CRON_MIN_INTERVAL_SECONDS:
        raise HTTPException(status_code=400, detail=f"interval must be at least {CRON_MIN_INTERVAL_SECONDS} seconds")
    return seconds


def _cron_create_job_sync(conv_id: int, workspace: str, script: str, description: str, every_seconds: int, enabled: bool) -> dict:
    now = _cron_now()
    next_run = now + every_seconds
    with _cron_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO cron_jobs
                (conv_id, workspace, script, description, every_seconds, enabled, created_at, updated_at, next_run_at, last_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (conv_id, workspace, script, description, every_seconds, 1 if enabled else 0, now, now, next_run, "created"),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM cron_jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _cron_row_to_dict(row)


async def _cron_create_job(conv_id: int, workspace: str, script: str, description: str, every_seconds: int, enabled: bool) -> dict:
    return await asyncio.to_thread(_cron_create_job_sync, conv_id, workspace, script, description, every_seconds, enabled)


def _cron_archive_job_sync(job_id: int) -> bool:
    now = _cron_now()
    with _cron_connect() as conn:
        cur = conn.execute(
            "UPDATE cron_jobs SET enabled = 0, archived = 1, archived_at = ?, updated_at = ?, last_status = ? WHERE id = ? AND archived = 0",
            (now, now, "archived", job_id),
        )
        conn.commit()
        return cur.rowcount > 0


async def _cron_archive_job(job_id: int) -> bool:
    return await asyncio.to_thread(_cron_archive_job_sync, job_id)


def _cron_set_enabled_sync(job_id: int, enabled: bool) -> bool:
    now = _cron_now()
    with _cron_connect() as conn:
        cur = conn.execute(
            "UPDATE cron_jobs SET enabled = ?, updated_at = ?, last_status = ? WHERE id = ? AND archived = 0",
            (1 if enabled else 0, now, "enabled" if enabled else "disabled", job_id),
        )
        conn.commit()
        return cur.rowcount > 0


async def _cron_set_enabled(job_id: int, enabled: bool) -> bool:
    return await asyncio.to_thread(_cron_set_enabled_sync, job_id, enabled)


def _cron_due_jobs_sync(now: float) -> list[dict]:
    with _cron_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cron_jobs WHERE archived = 0 AND enabled = 1 AND next_run_at <= ? ORDER BY next_run_at ASC",
            (now,),
        ).fetchall()
        return [_cron_row_to_dict(r) for r in rows]


def _cron_update_after_run_sync(job_id: int, started: float, exit_code: int | None, output: str, error: str, every_seconds: int) -> None:
    now = _cron_now()
    status = "ok" if exit_code == 0 else "error"
    with _cron_connect() as conn:
        conn.execute(
            """
            UPDATE cron_jobs
            SET last_run_at = ?, last_finished_at = ?, next_run_at = ?, last_status = ?,
                last_exit_code = ?, last_output = ?, last_error = ?, run_count = run_count + 1, updated_at = ?
            WHERE id = ?
            """,
            (started, now, now + every_seconds, status, exit_code, output[-4000:], error[-4000:], now, job_id),
        )
        conn.commit()


def _cron_mark_skipped_sync(job_id: int, every_seconds: int) -> None:
    now = _cron_now()
    with _cron_connect() as conn:
        conn.execute(
            "UPDATE cron_jobs SET skip_count = skip_count + 1, next_run_at = ?, last_status = ?, updated_at = ? WHERE id = ?",
            (now + every_seconds, "skipped_overlap", now, job_id),
        )
        conn.commit()


def _cron_command(script_path: Path) -> list[str]:
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(script_path)]
    if suffix == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    if suffix == ".js":
        return ["node", str(script_path)]
    raise RuntimeError(f"unsupported script suffix: {suffix}")


async def _cron_run_job(job: dict) -> None:
    job_id = job["id"]
    if job_id in _cron_running:
        await asyncio.to_thread(_cron_mark_skipped_sync, job_id, int(job["every_seconds"]))
        return
    _cron_running.add(job_id)
    started = _cron_now()
    try:
        script_path, _ = _cron_validate_script(job["script"], job["workspace"])
        proc = await asyncio.create_subprocess_exec(
            *_cron_command(script_path),
            cwd=job["workspace"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=min(max(int(job["every_seconds"]) - 5, 30), 3600))
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            output = ""
            error = "cron job timed out"
            exit_code = -1
    except Exception as e:
        output = ""
        error = str(e)
        exit_code = -1
    finally:
        _cron_running.discard(job_id)
    await asyncio.to_thread(_cron_update_after_run_sync, job_id, started, exit_code, output, error, int(job["every_seconds"]))


async def _cron_scheduler_loop() -> None:
    while True:
        try:
            due = await asyncio.to_thread(_cron_due_jobs_sync, _cron_now())
            for job in due:
                asyncio.create_task(_cron_run_job(job))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[CRON] Scheduler error: {e}")
        await asyncio.sleep(5)


CRON_HELP = {
    "purpose": "Create simple persisted interval jobs for scripts inside a Loom conversation workspace.",
    "create_endpoint": "POST /api/conversations/{conv_id}/cronjobs",
    "list_endpoint": "GET /api/cron/jobs?include_archived=false",
    "archive_endpoint": "DELETE /api/cron/jobs/{job_id}",
    "example": {
        "script": "scripts/pull_bluesky.py",
        "description": "Pull Bluesky profile data and update the conversation workspace cache.",
        "every_seconds": 3600,
        "enabled": True,
    },
    "rules": [
        "script must be a relative path inside the conversation workspace",
        f"script suffix must be one of {sorted(CRON_ALLOWED_SUFFIXES)}",
        f"minimum interval is {CRON_MIN_INTERVAL_SECONDS} seconds",
        "jobs skip overlapping runs by default",
        "delete archives and disables the job; archived jobs remain visible to admin",
        "scripts inherit the Loom server environment, so API keys should come from .env or process env",
    ],
    "rejected_patterns": list(CRON_DENY_PATTERNS),
}


app = FastAPI(title="Ex Astris Umbra — A Loom Interface", lifespan=lifespan)

# --- Graceful shutdown endpoint ---
_server_ref: list = []  # holds uvicorn.Server for shutdown


@app.post("/shutdown")
async def shutdown():
    """Gracefully stop this server instance — checkpoints WAL first."""
    await db.close_db()  # checkpoints WAL + closes connection
    if _server_ref:
        _server_ref[0].should_exit = True
        return JSONResponse({"status": "shutting down"})
    # Fallback: signal the process
    import signal

    os.kill(os.getpid(), signal.SIGINT)
    return JSONResponse({"status": "shutting down (signal)"})


@app.get("/api/generations")
async def api_list_generations():
    """List currently tracked generations (for admin dashboard)."""
    rows = await db.list_active_generations()
    # Annotate each with liveness + in-memory status
    for r in rows:
        r["pid_alive"] = _pid_alive(r.get("pid")) if r.get("pid") else False
        r["in_memory"] = any(
            k[0] == r.get("conv_id") and not t.done()
            for k, t in _active_generations.items()
        )
    return rows


@app.post("/api/generations/{draft_msg_id}/kill")
async def api_kill_generation(draft_msg_id: int):
    """Terminate a tracked generation and mark its draft errored."""
    rows = await db.list_active_generations()
    row = next((r for r in rows if r.get("draft_msg_id") == draft_msg_id), None)
    if not row:
        return JSONResponse({"error": "not tracked"}, status_code=404)
    pid = row.get("pid")
    killed = _kill_pid(pid) if pid else False
    # Cancel the in-memory task too
    for k, t in list(_active_generations.items()):
        if k[0] == row.get("conv_id") and not t.done():
            t.cancel()
    # Mark draft as errored if still empty
    try:
        m = await db.get_message(draft_msg_id)
        if m and m.get("role") == "assistant" and not (m.get("content") or "").strip():
            await db.update_message_content(
                draft_msg_id,
                content="[Error: generation killed by admin]",
            )
    except Exception:
        pass
    await db.unregister_active_generation(draft_msg_id)
    return {"status": "killed", "pid": pid, "pid_killed": killed}


@app.get("/api/cron/help")
async def api_cron_help():
    """LLM-facing reference for creating Loom cron jobs."""
    return CRON_HELP


@app.get("/api/cron/jobs")
async def api_cron_jobs(include_archived: bool = False):
    return await _cron_list_jobs(include_archived=include_archived)


@app.get("/api/conversations/{conv_id}/cronjobs")
async def api_conversation_cron_jobs(conv_id: int, include_archived: bool = False):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return await _cron_list_jobs(include_archived=include_archived, conv_id=conv_id)


@app.post("/api/conversations/{conv_id}/cronjobs")
async def api_create_conversation_cron_job(conv_id: int, data: dict = Body(...)):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    workspace = conv.get("project_dir") or "."
    if workspace == ".":
        raise HTTPException(status_code=400, detail="conversation has no dedicated workspace directory")
    every_seconds = _cron_interval_from_payload(data)
    _, script = _cron_validate_script(data.get("script", ""), workspace)
    description = str(data.get("description") or "").strip()
    if len(description) > 2000:
        raise HTTPException(status_code=400, detail="description must be 2000 characters or fewer")
    enabled = bool(data.get("enabled", True))
    job = await _cron_create_job(conv_id, str(Path(workspace).resolve()), script, description, every_seconds, enabled)
    return JSONResponse(job, status_code=201)


@app.put("/api/cron/jobs/{job_id}")
async def api_update_cron_job(job_id: int, data: dict = Body(...)):
    if "enabled" not in data:
        raise HTTPException(status_code=400, detail="only enabled can be updated for now")
    ok = await _cron_set_enabled(job_id, bool(data.get("enabled")))
    if not ok:
        raise HTTPException(status_code=404, detail="cron job not found")
    jobs = await _cron_list_jobs(include_archived=True)
    return next((j for j in jobs if j["id"] == job_id), {"status": "ok"})


@app.delete("/api/cron/jobs/{job_id}")
async def api_archive_cron_job(job_id: int):
    ok = await _cron_archive_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="cron job not found")
    return {"status": "archived", "id": job_id}


# Ensure upload directory exists
os.makedirs(config.upload_dir, exist_ok=True)

# Active WebSocket generation tasks — keyed by (conv_id, parent_id, seq) for parallel support
# CC mode only allows one per conv; Weave/OODA allow multiple (even on same parent)
_active_generations: dict[tuple[int, int | None, int], asyncio.Task] = {}
_gen_seq = 0  # monotonic counter for unique gen keys
# Active Claude Code subprocesses (for cancellation)
_active_claude_procs: dict[int, asyncio.subprocess.Process] = {}
# Active Hermes (ACP) subprocesses (for cancellation) — parallel to _active_claude_procs
_active_hermes_procs: dict[int, asyncio.subprocess.Process] = {}
# Active WebSocket connections per conversation — multiple clients can watch the same conv
_active_websockets: dict[int, set[WebSocket]] = {}
# Pending hook-based permission requests: request_id -> {event, response, conv_id}
_pending_hook_permissions: dict[str, dict] = {}
# Permission requests the user approved for the current branch generation.
# Keyed by (conversation id, permission scope), then an exact request fingerprint.
_auto_approve_permissions: dict[tuple[int, str], set[str]] = {}
# Live generation state — survives WS disconnects so reconnecting clients get a snapshot.
# Keyed by gen_key (conv_id, parent_id, seq). Updated on every stream event.
_generation_snapshots: dict[tuple[int, int | None, int], dict] = {}


def _stable_json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return str(value)


def _permission_fingerprint(tool_name: str, tool_input, approval_method: str = "") -> str:
    """Stable key for the exact permission request a Loom approval grants."""
    method = str(approval_method or "")
    tool = str(tool_name or "Unknown")
    if method == "item/permissions/requestApproval":
        requested = {}
        if isinstance(tool_input, dict):
            for key in ("permissions", "requestedPermissions", "requested_permissions"):
                if isinstance(tool_input.get(key), dict):
                    requested = tool_input[key]
                    break
        return f"{method}:{_stable_json(requested)}"
    if method == "item/fileChange/requestApproval":
        return f"{method}:{_stable_json(tool_input)}"
    if method == "item/commandExecution/requestApproval":
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command") or tool_input.get("cmd") or tool_input.get("execCommand") or "")
            if not command and isinstance(tool_input.get("argv"), list):
                command = " ".join(str(part) for part in tool_input["argv"])
        return f"{method}:{command or _stable_json(tool_input)}"
    if method:
        return f"{method}:{tool}"
    return tool


def _permission_scope_for_active_generation(conv_id: int, explicit_scope: str = "") -> str:
    """Return the branch-generation scope for remembered permissions."""
    explicit = str(explicit_scope or "").strip()
    if explicit:
        return explicit
    active = [
        gk for gk, task in _active_generations.items()
        if gk[0] == conv_id and not task.done()
    ]
    if len(active) == 1:
        return f"gen:{active[0][2]}"
    return "manual"


def _permission_scope_gen_id(permission_scope: str) -> int | None:
    scope = str(permission_scope or "")
    if not scope.startswith("gen:"):
        return None
    try:
        return int(scope.split(":", 1)[1])
    except ValueError:
        return None


def _update_gen_snapshot(gen_key: tuple, **fields):
    """Update the live snapshot for an active generation (called on every stream event)."""
    snap = _generation_snapshots.get(gen_key)
    if snap is None:
        snap = {
            "full_text": "",
            "content_blocks": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "started_at": 0,
            "draft_msg_id": None,
            "parent_id": None,
            "cc_model": "",
            "mode": "claude",
        }
        _generation_snapshots[gen_key] = snap
    snap.update(fields)
    return snap


def _parse_image_paths(image_path) -> list[str]:
    """Parse image_path field: handles single string, JSON array string, or list."""
    if not image_path:
        return []
    if isinstance(image_path, list):
        return image_path
    try:
        parsed = json.loads(image_path)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return [image_path]



# ── Static files ──

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ── Canvas ──


@app.post("/api/conversations/{conv_id}/canvas")
async def toggle_canvas(conv_id: int, data: dict = None):
    """Enable or disable canvas for a conversation."""
    data = data or {}
    enabled = bool(data.get("enabled", True))
    conv = await db.get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "Not found"}, status_code=404)

    project_dir = conv.get("project_dir") or ""
    if enabled and not project_dir:
        # Auto-create a workspace for conversations without a project_dir
        workspace = Path("canvas_workspaces") / str(conv_id)
        workspace.mkdir(parents=True, exist_ok=True)
        project_dir = str(workspace)
        await db.update_conversation_fields(conv_id, project_dir=project_dir)

    canvas_slug = conv.get("canvas_slug") or ""
    if enabled:
        canvas_dir = Path(project_dir) / "canvas"
        canvas_dir.mkdir(parents=True, exist_ok=True)
        # Auto-create .gitignore so canvas output doesn't leak into git
        gitignore = canvas_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# Canvas output is generated per-conversation — don't commit it\n"
                "*\n"
                "!.gitignore\n",
                encoding="utf-8",
            )
        index_file = canvas_dir / "index.html"
        if not index_file.exists():
            index_file.write_text(
                '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
                "<title>Canvas</title></head>\n"
                "<body><h1>Canvas ready</h1></body></html>\n",
                encoding="utf-8",
            )

        # Mint a stable slug the first time canvas is enabled. Retry on the
        # off chance the unique index rejects the generated slug.
        if not canvas_slug:
            for _ in range(5):
                candidate = generate_canvas_slug()
                if not await db.get_conversation_by_canvas_slug(candidate):
                    canvas_slug = candidate
                    break
            if canvas_slug:
                await db.update_conversation_fields(conv_id, canvas_slug=canvas_slug)

    await db.update_conversation_fields(conv_id, canvas_enabled=1 if enabled else 0)
    return {
        "ok": True,
        "enabled": enabled,
        "project_dir": project_dir,
        "canvas_slug": canvas_slug or None,
    }


@app.get("/api/canvas/{conv_id}")
async def list_canvas_files(conv_id: int):
    """List files in a conversation's canvas directory."""
    conv = await db.get_conversation(conv_id)
    if not conv or not conv.get("project_dir"):
        return JSONResponse({"error": "No canvas"}, status_code=404)
    canvas_dir = Path(conv["project_dir"]) / "canvas"
    if not canvas_dir.is_dir():
        return {"files": []}
    files = [f.name for f in canvas_dir.iterdir() if f.is_file()]
    return {"files": sorted(files)}


@app.get("/api/canvas/{conv_id}/{file_path:path}")
async def serve_canvas_file(conv_id: int, file_path: str = "index.html"):
    """Serve a file from a conversation's canvas directory."""
    conv = await db.get_conversation(conv_id)
    if not conv or not conv.get("project_dir"):
        return JSONResponse({"error": "No canvas"}, status_code=404)
    canvas_dir = Path(conv["project_dir"]).resolve() / "canvas"
    if not file_path:
        file_path = "index.html"
    target = (canvas_dir / file_path).resolve()
    # Path traversal guard
    if not str(target).startswith(str(canvas_dir)):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(str(target))


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/{slug}")
async def canvas_by_slug(slug: str):
    """Direct-access wrapper: serve a full-viewport iframe of a conversation's canvas
    when the URL path matches a canvas slug. Strict regex prevents collisions with
    any other top-level path."""
    if not is_valid_slug(slug):
        return JSONResponse({"error": "Not found"}, status_code=404)
    conv = await db.get_conversation_by_canvas_slug(slug)
    if not conv or not conv.get("canvas_enabled"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    # Title is user-controlled — escape for HTML safety.
    from html import escape as _esc
    title = _esc(conv.get("title") or slug)
    html = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>{title}</title>"
        "<style>html,body{margin:0;height:100%;background:#000}"
        "iframe{border:0;width:100vw;height:100vh;display:block;background:#fff}</style>"
        '<iframe sandbox="allow-scripts allow-same-origin" '
        f'src="/api/canvas/{conv["id"]}/index.html"></iframe>'
    )
    return HTMLResponse(html)


# ── Health ──


@app.get("/api/health")
async def api_health():
    ollama_status = await health_check()
    return ollama_status


@app.post("/api/nrol/mcp-activity")
async def api_nrol_mcp_activity():
    """Return recent NROL MCP activity from the admin sidecar without launching work."""
    admin_port = int(os.environ.get("ADMIN_PORT", "3002"))
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{admin_port}/tools/nrol-mcp-activity",
                json={},
            )
        payload = resp.json() if resp.content else {}
        return JSONResponse(payload, status_code=resp.status_code)
    except Exception as e:
        return JSONResponse(
            {"status": "error", "output": f"Could not read NROL MCP activity via admin server: {e}"},
            status_code=502,
        )


# Map of probe targets to (port, health_path). All run on localhost alongside
# main Loom; main Loom proxies them so the settings panel can probe over HTTPS
# without browser mixed-content blocking the direct HTTP request.
_STATUS_TARGETS = {
    "admin":   (3002, "/api/status"),
    "ollama":  (11434, "/api/tags"),
    "vllm":    (8000, "/v1/models"),
    "nrol":    (int(os.environ.get("ALPHA_OMEGA_PORT", "8098")), "/topics"),
    "comfyui": (8188, "/system_stats"),
}


@app.get("/api/server-status/{target}")
async def api_server_status(target: str):
    """Server-side proxy of a co-located service's health endpoint.
    Browser probes from HTTPS are blocked as mixed content, so we probe from
    the server trying HTTPS first, then HTTP. Returns {up: bool}."""
    if target == "llama":
        from urllib.parse import urlparse
        try:
            url = config.llama_host_url()
            parsed = urlparse(url)
            port = parsed.port or 8000
        except Exception:
            port = 8000
        path = "/v1/models"
    else:
        spec = _STATUS_TARGETS.get(target)
        if not spec:
            return {"up": False}
        port, path = spec
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            for scheme in ("https", "http"):
                try:
                    r = await client.get(f"{scheme}://127.0.0.1:{port}{path}")
                    if r.status_code == 200:
                        return {"up": True}
                except Exception:
                    pass
    except Exception:
        pass
    return {"up": False}


BSKY_API = "https://bsky.social/xrpc"
BSKY_PUBLIC_API = "https://public.api.bsky.app/xrpc"

# Endpoints available on the read-only public API (no auth needed)
# Verified: 200 with real data on public.api.bsky.app
# getTimeline requires auth on both APIs — intentionally excluded
_BSKY_PUBLIC_ENDPOINTS = {
    "app.bsky.feed.getAuthorFeed",
    "app.bsky.actor.getProfile",
    "app.bsky.graph.getFollows",
}


async def _resolve_bsky_handle(handle: str) -> str | None:
    """Resolve a Bluesky handle to its DID via the public identity API.
    Returns the original string if it's already a DID, or the resolved DID.
    Returns None on failure."""
    import urllib.request, urllib.parse
    import asyncio, ssl

    # Already a DID — nothing to resolve
    if handle.startswith("did:"):
        return handle

    def _resolve():
        ctx = ssl.create_default_context()
        url = f"https://bsky.social/xrpc/com.atproto.identity.resolveHandle?handle={urllib.parse.quote(handle)}"
        req = urllib.request.Request(url, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=5, context=ctx)
            data = json.loads(resp.read())
            return data.get("did")
        except Exception:
            return None

    return await asyncio.to_thread(_resolve)


async def _do_bsky_proxy(method: str, path: str, body: dict = None, query_params: dict = None, auth_header: str = None):
    """Core handler for the Bluesky XRPC proxy.
    Uses urllib (not httpx) because httpx gets blocked by bsky's AWS WAF while urllib works fine.
    Public read endpoints route to public.api.bsky.app, everything else goes to bsky.social.
    Auto-resolves handle values in actor/author query params to DIDs."""
    import urllib.request, urllib.parse
    import asyncio, ssl

    target = BSKY_PUBLIC_API if method == "GET" and path in _BSKY_PUBLIC_ENDPOINTS else BSKY_API

    # Resolve any handle values in actor/author params to DIDs
    if query_params:
        for key in ("actor", "author"):
            val = query_params.get(key)
            if val and not val.startswith("did:"):
                resolved = await _resolve_bsky_handle(val)
                if resolved:
                    query_params[key] = resolved
                    print(f"[BSKY] Resolved handle {val} -> {resolved}")

    url = f"{target}/{path}"
    if query_params:
        qs = urllib.parse.urlencode(query_params, doseq=True)
        url = f"{url}?{qs}"

    data = json.dumps(body).encode("utf-8") if body else None
    fwd_headers = {"Content-Type": "application/json"}
    if auth_header:
        fwd_headers["Authorization"] = auth_header

    print(f"[BSKY] {method} {path} target={target.split('//')[1].split('/')[0]} params={query_params} auth={bool(auth_header)}")

    def _fetch():
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, data=data, headers=fwd_headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=10, context=ctx)
            return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    status, raw = await asyncio.to_thread(_fetch)
    try:
        content = json.loads(raw)
        return JSONResponse(status_code=status, content=content)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=status, content={"_raw": raw.decode("utf-8", errors="replace")})


@app.post("/api/proxy/bsky/{path:path}")
async def proxy_bsky_post(path: str, body: dict = Body(default={}), authorization: str = Header(None)):
    """Proxy Bluesky ATProto XRPC POST calls so the browser isn't blocked by CORS."""
    return await _do_bsky_proxy("POST", path, body, auth_header=authorization)


@app.get("/api/proxy/bsky/{path:path}")
async def proxy_bsky_get(request: Request, path: str, authorization: str = Header(None)):
    """Proxy Bluesky ATProto XRPC GET calls."""
    qp = dict(request.query_params) if len(request.query_params) > 0 else None
    return await _do_bsky_proxy("GET", path, query_params=qp, auth_header=authorization)


# ── Bluesky token storage (canvas uses this to persist session tokens) ──

@app.get("/api/bluesky-token/{conv_id}")
async def get_bsky_token(conv_id: int):
    """Return the stored Bluesky access token for a conversation."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "not found"}, status_code=404)
    token = conv.get("bsky_token")
    if not token:
        return JSONResponse({"error": "no token stored"}, status_code=404)
    return {"token": token}


@app.put("/api/bluesky-token/{conv_id}")
async def set_bsky_token(conv_id: int, data: dict = Body(default={})):
    """Store a Bluesky access token for a conversation."""
    token = data.get("token")
    if not token:
        return JSONResponse({"error": "no token provided"}, status_code=400)
    await db.update_conversation_fields(conv_id, bsky_token=token)
    return {"ok": True}


@app.delete("/api/bluesky-token/{conv_id}")
async def delete_bsky_token(conv_id: int):
    """Clear the stored Bluesky access token for a conversation."""
    await db.update_conversation_fields(conv_id, bsky_token=None)
    return {"ok": True}


@app.get("/api/admin-status")
async def api_admin_status():
    """Back-compat alias for the unified server-status endpoint."""
    return await api_server_status("admin")



# ── Local model list cache ────────────────────────────────────────────────
# Serves the .gguf file list from disk so the UI model dropdown is instant.

_LOCAL_MODELS_CACHE: dict | None = None
_VISION_MODELS_CACHE: dict | None = None


async def _refresh_local_models_cache() -> dict:
    global _LOCAL_MODELS_CACHE
    status = await health_check()
    import llama_client as _lc
    disk_models = _lc.list_local_models()
    _LOCAL_MODELS_CACHE = {
        "backend": "llama",
        "host": config.llama_host,
        "models": disk_models,
        "disk_models": disk_models,
        "target_model": config.llama_model,
        "available": status.get("model_available", False),
        "fetched_at": time.time(),
    }
    return _LOCAL_MODELS_CACHE


async def _refresh_vision_models_cache() -> dict:
    """Vision model list — same as main models (llama-server uses same model for all tasks)."""
    global _VISION_MODELS_CACHE
    import llama_client as _lc
    disk_models = _lc.list_local_models()
    _VISION_MODELS_CACHE = {
        "backend": "llama",
        "host": config.llama_host,
        "models": disk_models,
        "fetched_at": time.time(),
    }
    return _VISION_MODELS_CACHE


def _cache_is_stale(cache: dict | None) -> bool:
    """Returns True if the cache was built against a different host than current config."""
    if not cache:
        return True
    return cache.get("backend") != "llama" or cache.get("host") != config.llama_host


@app.get("/api/local/models")
async def api_local_models():
    """Active-backend-aware model list, served from cache. Use
    POST /api/local/refresh-models to force a re-fetch."""
    if _cache_is_stale(_LOCAL_MODELS_CACHE):
        await _refresh_local_models_cache()
    return _LOCAL_MODELS_CACHE


@app.get("/api/local/vision-models")
async def api_local_vision_models():
    """Vision-capable models from the active backend, served from cache."""
    if _cache_is_stale(_VISION_MODELS_CACHE):
        await _refresh_vision_models_cache()
    return _VISION_MODELS_CACHE


@app.post("/api/local/refresh-models")
async def api_local_refresh_models():
    """Force-refresh both model caches. Wired to the 'Refresh' button in
    Settings; also called after llama-server starts/stops."""
    await _refresh_local_models_cache()
    await _refresh_vision_models_cache()
    return {
        "models": _LOCAL_MODELS_CACHE.get("models", []),
        "vision_models": _VISION_MODELS_CACHE.get("models", []),
        "backend": "llama",
    }


# ── All models list ───────────────────────────────────────────────────────
# Returns .gguf models from disk + any live server models.
# Used by the Braid/Weave inline dropdown.

_ALL_ENGINES_CACHE: dict | None = None


async def _refresh_all_engines_cache() -> dict:
    """Scan disk for .gguf files and probe llama-server for loaded models."""
    global _ALL_ENGINES_CACHE
    import llama_client as _lc
    disk_models = _lc.list_local_models()
    status = await _lc.health_check()
    live_models = status.get("models", [])
    model_name_map = status.get("model_name_map", {})
    out: list[dict] = []
    for m in disk_models:
        is_loaded = False
        if m in model_name_map and model_name_map[m] in live_models:
            is_loaded = True
        elif live_models and m == status.get("target_model"):
            is_loaded = True
        out.append({"name": m, "backend": "llama", "loaded": is_loaded})
    _ALL_ENGINES_CACHE = {
        "models": out,
        "active_backend": "llama",
        "fetched_at": time.time(),
    }
    return _ALL_ENGINES_CACHE


@app.get("/api/local/all-models")
async def api_local_all_models():
    """Returns all local .gguf models plus live llama-server models.
    Used by the inline dropdown for model selection."""
    if _ALL_ENGINES_CACHE is None:
        await _refresh_all_engines_cache()
    return _ALL_ENGINES_CACHE


@app.post("/api/local/refresh-all-models")
async def api_local_refresh_all_models():
    return await _refresh_all_engines_cache()


# ── Per-model configuration ────────────────────────────────────────────────
# models_config.json lives in the project root next to config.json.
# Format: { "ModelName.gguf": { ctx_size, ngl, flash_attn, kv_quant, threads,
#             batch, ubatch, mlock, extra_args } }

MODEL_CONFIG_PATH = Path(__file__).parent / "models_config.json"
_KV_QUANT_OPTIONS = ["none", "K4", "K4_0", "K5", "K5_0", "K8", "K8_0"]


def _load_model_configs() -> dict:
    if MODEL_CONFIG_PATH.exists():
        try:
            with open(MODEL_CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_model_configs(cfg: dict):
    try:
        with open(MODEL_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[SERVER] Failed to save models_config.json: {e}")


def _default_ctx(name: str) -> int:
    """Guess context size from model name. Check larger sizes first."""
    import re
    # Match patterns like "27b", "32b" etc. - largest match wins
    m = re.search(r'(\d+)b', name, re.IGNORECASE)
    if m:
        size = int(m.group(1))
        if size <= 3:
            return 200000
        if size <= 8:
            return 150000
        if size <= 14:
            return 100000
        if size <= 27:
            return 80000
        if size <= 70:
            return 40000
    return 150000


def _new_model_default(name: str) -> dict:
    return {
        "ctx_size": _default_ctx(name),
        "ngl": 999,
        "flash_attn": True,
        "kv_quant": "none",
        "threads": None,
        "batch": None,
        "ubatch": None,
        "mlock": False,
        "mmproj": None,
        "extra_args": "",
        "server_model_id": None,
    }


def _sync_disk_models():
    """Add new .gguf files to config with defaults. Never deletes."""
    import llama_client as _lc
    cfg = _load_model_configs()
    disk = _lc.list_local_models()
    for m in disk:
        if m not in cfg:
            cfg[m] = _new_model_default(m)
    _save_model_configs(cfg)
    return cfg


@app.get("/api/models-config")
async def api_get_models_config():
    """Return models_config.json (synced with disk)."""
    cfg = _sync_disk_models()
    return {"models": cfg, "kv_quant_options": _KV_QUANT_OPTIONS}


@app.put("/api/models-config")
async def api_update_models_config(data: dict):
    """Update all model configs. Only saves entries for models that exist on disk."""
    import llama_client as _lc
    disk_set = set(_lc.list_local_models())
    cfg = {}
    for name, settings in data.get("models", {}).items():
        if name in disk_set:
            cfg[name] = settings
    _save_model_configs(cfg)
    return {"status": "ok", "saved": len(cfg)}


@app.get("/api/disk-models")
async def api_get_disk_models():
    """Return list of .gguf files in the models directory."""
    import llama_client as _lc
    models = _lc.list_local_models()
    return {"models": models}


@app.post("/api/update-model-ids")
async def api_update_model_ids():
    """Update server_model_id in config by querying the running Llama Server.

    Matches the server-registered model ID to the active config model name
    and persists it in models_config.json.
    """
    import httpx
    import llama_client as _lc
    cfg = _load_model_configs()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config.llama_host_url()}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            server_models = [m["id"] for m in data.get("data", [])]
    except Exception as e:
        return {"status": "error", "message": str(e)}

    # Match server IDs to local model configs
    active = config.llama_model or ""
    active_norm = active.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
    for model_file in cfg:
        norm = model_file.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
        for srv_id in server_models:
            srv_norm = srv_id.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
            if norm and (norm in srv_norm or srv_norm in norm):
                cfg[model_file]["server_model_id"] = srv_id

    _save_model_configs(cfg)
    return {"status": "ok", "updated": len(server_models), "config": cfg}


async def _update_model_ids_on_startup():
    """On server startup, sync server_model_id from the running Llama Server."""
    await asyncio.sleep(3)  # let Llama Server finish starting
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config.llama_host_url()}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            server_models = [m["id"] for m in data.get("data", [])]
    except Exception as e:
        print(f"[MODEL-IDS] Could not fetch server models: {e}")
        return

    cfg = _load_model_configs()
    for model_file in cfg:
        norm = model_file.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
        for srv_id in server_models:
            srv_norm = srv_id.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
            if norm and (norm in srv_norm or srv_norm in norm):
                cfg[model_file]["server_model_id"] = srv_id

    _save_model_configs(cfg)
    for k, v in cfg.items():
        sid = v.get("server_model_id")
        if sid:
            print(f"[MODEL-IDS] {k} -> {sid}")


# ── Characters ──




# ── Characters ──


@app.get("/api/characters")
async def api_characters():
    chars = load_all_characters(config.characters_dir)
    return chars


@app.post("/api/characters")
async def api_create_character(data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Character name is required")
    char = save_character(config.characters_dir, data)
    if not char:
        raise HTTPException(500, "Failed to save character")
    return char


@app.put("/api/characters/{char_id}")
async def api_update_character(char_id: str, data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Character name is required")
    data["id"] = char_id
    char = save_character(config.characters_dir, data)
    if not char:
        raise HTTPException(500, "Failed to save character")
    return char


@app.delete("/api/characters/{char_id}")
async def api_delete_character(char_id: str):
    deleted = delete_character(config.characters_dir, char_id)
    if not deleted:
        raise HTTPException(404, "Character not found")
    return {"ok": True}


# ── Character State Cards (Tier 1) ──


@app.get("/api/characters/{char_id}/state")
async def api_get_character_state(char_id: str):
    return await db.get_character_state_cards(char_id)


@app.post("/api/characters/{char_id}/state")
async def api_create_character_state(char_id: str, data: dict):
    return await db.create_character_state_card(
        char_id,
        data["schema_id"],
        data["label"],
        data.get("data", {}),
        data.get("is_readonly", False),
    )


@app.put("/api/character-state/{card_id}")
async def api_update_character_state(card_id: int, data: dict):
    return await db.update_character_state_card(card_id, data.get("data", {}))


@app.delete("/api/character-state/{card_id}")
async def api_delete_character_state(card_id: int):
    await db.delete_character_state_card(card_id)
    return {"ok": True}


@app.post("/api/characters/{char_id}/duplicate")
async def api_duplicate_character(char_id: str):
    """Duplicate a character and its global state cards."""
    char = load_character(os.path.join(config.characters_dir, f"{char_id}.md"))
    if not char:
        raise HTTPException(404, "Character not found")
    # Generate unique ID
    base_name = char["name"] + " Copy"
    new_data = {**char, "name": base_name, "id": None}  # id=None → auto-slug
    new_char = save_character(config.characters_dir, new_data)
    if not new_char:
        raise HTTPException(500, "Failed to duplicate character")
    # Copy global state cards
    old_cards = await db.get_character_state_cards(char_id)
    for card in old_cards:
        card_data = (
            json.loads(card["data"]) if isinstance(card["data"], str) else card["data"]
        )
        await db.create_character_state_card(
            new_char["id"],
            card["schema_id"],
            card["label"],
            card_data,
            card.get("is_readonly", False),
        )
    return new_char


# ── Personas ──


@app.get("/api/personas")
async def api_personas():
    return load_all_personas("personas")


@app.post("/api/personas")
async def api_create_persona(data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Persona name is required")
    persona = save_persona("personas", data)
    if not persona:
        raise HTTPException(500, "Failed to save persona")
    return persona


@app.put("/api/personas/{persona_id}")
async def api_update_persona(persona_id: str, data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Persona name is required")
    data["id"] = persona_id
    persona = save_persona("personas", data)
    if not persona:
        raise HTTPException(500, "Failed to save persona")
    return persona


@app.delete("/api/personas/{persona_id}")
async def api_delete_persona(persona_id: str):
    deleted = delete_persona("personas", persona_id)
    if not deleted:
        raise HTTPException(404, "Persona not found")
    return {"ok": True}


# ── Lore ──


@app.get("/api/lore")
async def api_lore():
    return load_all_lore("lore")


@app.post("/api/lore")
async def api_create_lore(data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Lore entry name is required")
    entry = save_lore("lore", data)
    if not entry:
        raise HTTPException(500, "Failed to save lore entry")
    return entry


@app.put("/api/lore/{lore_id}")
async def api_update_lore(lore_id: str, data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Lore entry name is required")
    data["id"] = lore_id
    entry = save_lore("lore", data)
    if not entry:
        raise HTTPException(500, "Failed to save lore entry")
    return entry


@app.delete("/api/lore/{lore_id}")
async def api_delete_lore(lore_id: str):
    deleted = delete_lore("lore", lore_id)
    if not deleted:
        raise HTTPException(404, "Lore entry not found")
    return {"ok": True}


# ── Conversations ──


@app.get("/api/conversations")
async def api_list_conversations():
    return await db.list_conversations()


@app.get("/api/search")
async def api_search(q: str = ""):
    if len(q) < 2:
        return []
    return await db.search_conversations(q)


@app.get("/api/conversations/{conv_id}/search")
async def api_search_conversation(conv_id: int, q: str = ""):
    if len(q) < 2:
        return []
    return await db.search_conversation_messages(conv_id, q)


@app.post("/api/conversations")
async def api_create_conversation(data: dict = None):
    data = data or {}
    title = data.get("title", "New Conversation")
    character_id = data.get("character_id")
    persona_id = data.get("persona_id")
    lore_ids = data.get("lore_ids", [])
    style_nudge = data.get("style_nudge", "Natural")
    mode = data.get("mode", "weave")
    project_dir = data.get("project_dir")
    nrol_operator = 1 if data.get("nrol_operator") else 0
    if nrol_operator:
        # Operator conversations are CC sessions launched from a neutral
        # workspace — never the Loom repo or the NROL engine repo — so any
        # file path the session touches is outside its project boundary.
        mode = "claude"
        if not project_dir:
            operator_dir = Path(__file__).parent / "workspaces" / "nrol_operator"
            operator_dir.mkdir(parents=True, exist_ok=True)
            # Provision the scheduled-scan shim so a Loom cron job can be
            # registered on this conversation (cron scripts must live in the
            # conversation workspace). POST /api/conversations/{id}/cronjobs
            # with script="nrol_scan_tick.py" to enable scheduled scans.
            tick = operator_dir / "nrol_scan_tick.py"
            if not tick.exists():
                tick.write_text(
                    '"""NROL-AO scheduled scan tick (Loom cron shim).\n'
                    "\n"
                    "Safe-policy scan: PARK/SCHEMA_GAP auto-apply (cannot move\n"
                    "posteriors), FIRE/OBSERVE are filed as pending proposals for\n"
                    "operator review, digest written beside the activity ledger.\n"
                    '"""\n'
                    "import sys\n"
                    "from pathlib import Path\n"
                    "\n"
                    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))\n"
                    "\n"
                    "from bin.nrol_scan_tick import main\n"
                    "\n"
                    'if __name__ == "__main__":\n'
                    "    sys.exit(main())\n",
                    encoding="utf-8",
                )
            project_dir = str(operator_dir)

    first_turn = data.get("first_turn", "character")  # "character" or "user"
    custom_scene = data.get("custom_scene")

    cc_model = data.get("cc_model", "sonnet")
    cc_effort = data.get("cc_effort", "high")
    local_model = data.get("local_model")

    if nrol_operator:
        blocked = _nrol_operator_block_reason(cc_model)
        if blocked:
            raise HTTPException(status_code=400, detail=blocked)

    conv = await db.create_conversation(
        title, character_id, mode=mode, project_dir=project_dir
    )

    # Store additional fields
    import json as _json

    fields = dict(
        persona_id=persona_id,
        lore_ids=_json.dumps(lore_ids),
        style_nudge=style_nudge,
        custom_scene=custom_scene,
        cc_model=cc_model,
        cc_effort=cc_effort,
        ooda_enabled=1 if mode == "weave" else 0,
    )
    if nrol_operator:
        fields["nrol_operator"] = 1
    if mode in ("local", "hermes") and local_model:
        fields["local_model"] = local_model
    await db.update_conversation_fields(conv["id"], **fields)
    # Refresh conv data
    conv = await db.get_conversation(conv["id"])

    # Auto-seed state cards for OODA-enabled Weave conversations
    if mode == "weave" and character_id:
        global_cards = await db.get_character_state_cards(character_id)
        if global_cards:
            await db.copy_character_state_to_conversation(character_id, conv["id"])
        else:
            char = load_character(
                os.path.join(config.characters_dir, f"{character_id}.md")
            )
            if char:
                await db.create_state_card(
                    conv["id"],
                    "character_state",
                    char.get("name", "Character"),
                    {
                        "personality": char.get("personality", ""),
                        "appearance": "",
                        "current_mood": "",
                        "current_goal": "",
                        "physical_state": "",
                        "speech_pattern": "",
                        "relationship_to_player": "",
                        "secrets": "",
                    },
                )
                if char.get("scenario"):
                    await db.create_state_card(
                        conv["id"],
                        "scene_state",
                        "current",
                        {
                            "location": "",
                            "time_of_day": "",
                            "atmosphere": "",
                            "characters_present": "",
                            "recent_events": char["scenario"],
                            "tension_level": "",
                        },
                    )
        if persona_id:
            persona = load_persona(os.path.join("personas", f"{persona_id}.md"))
            if persona:
                await db.create_state_card(
                    conv["id"],
                    "persona_state",
                    persona["name"],
                    {
                        "description": persona.get("content", ""),
                        "appearance": "",
                        "goals": "",
                    },
                )
        if lore_ids:
            for lid in lore_ids:
                entry = load_lore_entry(os.path.join("lore", f"{lid}.md"))
                if entry:
                    await db.create_state_card(
                        conv["id"],
                        "lore",
                        entry["name"],
                        {
                            "content": entry["content"],
                        },
                        is_readonly=True,
                    )

    # If character goes first:
    #   - Custom scene → add it as a user message so the model responds to it
    #   - No custom scene + greeting exists → use the static greeting
    #   - No custom scene + no greeting → leave empty, client triggers generation
    if first_turn == "character" and character_id:
        char = load_character(os.path.join(config.characters_dir, f"{character_id}.md"))
        if custom_scene:
            # Add custom scene as a user message for the model to respond to
            scene_msg = await db.add_message(conv["id"], "user", custom_scene)
            await db.set_active_branch(conv["id"], scene_msg["id"])
        elif char and char.get("greeting"):
            greeting_msg = await db.add_message(
                conv["id"], "assistant", char["greeting"]
            )
            await db.set_active_branch(conv["id"], greeting_msg["id"])

    return conv


@app.get("/api/conversations/{conv_id}")
async def api_get_conversation(conv_id: int):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    # Lazy-migrate legacy local model names to loaded model filenames if they match
    if conv.get("local_model") and config.llama_model:
        local_model = conv["local_model"]
        if models_match(local_model, config.llama_model):
            if local_model != config.llama_model:
                conv["local_model"] = config.llama_model
                await db.update_conversation_fields(conv_id, local_model=config.llama_model)
    # Lazy-migrate legacy local model names in cc_model to loaded model filenames if they match
    if conv.get("cc_model") and config.llama_model:
        cc_model = conv["cc_model"]
        is_api = any(
            cc_model.startswith(prefix)
            for prefix in ("claude-", "fable", "sonnet", "haiku", "opus", "gemini", "Gemini", "gpt", "GPT")
        )
        if not is_api and models_match(cc_model, config.llama_model):
            if cc_model != config.llama_model:
                conv["cc_model"] = config.llama_model
                await db.update_conversation_fields(conv_id, cc_model=config.llama_model)
    # Lazy-mint a slug for conversations that had canvas enabled before
    # the canvas_slug column existed.
    if conv.get("canvas_enabled") and not conv.get("canvas_slug"):
        for _ in range(5):
            candidate = generate_canvas_slug()
            if not await db.get_conversation_by_canvas_slug(candidate):
                conv["canvas_slug"] = candidate
                await db.update_conversation_fields(conv_id, canvas_slug=candidate)
                break
    return conv


@app.put("/api/conversations/{conv_id}")
async def api_update_conversation(conv_id: int, data: dict):
    """Update conversation settings (style_nudge, persona_id, lore_ids, etc.)."""
    import json as _json

    fields = {}
    if "style_nudge" in data:
        fields["style_nudge"] = data["style_nudge"]
    if "persona_id" in data:
        fields["persona_id"] = data["persona_id"]
    if "lore_ids" in data:
        fields["lore_ids"] = _json.dumps(data["lore_ids"])
    if "title" in data:
        fields["title"] = data["title"]
    if "custom_scene" in data:
        fields["custom_scene"] = data["custom_scene"]
    if "starred" in data:
        fields["starred"] = int(data["starred"])
    if "nsfw" in data:
        fields["nsfw"] = int(data["nsfw"])
    if "cc_model" in data:
        fields["cc_model"] = data["cc_model"]
        cc_model = data["cc_model"]
        is_gemini = is_gemini_model(cc_model)
        is_codex = is_codex_model(cc_model)
        fields["mode"] = "gemini" if is_gemini else ("codex" if is_codex else "claude")
    if "cc_effort" in data:
        fields["cc_effort"] = data["cc_effort"]
    if "cc_permission_mode" in data:
        fields["cc_permission_mode"] = data["cc_permission_mode"]
    if "local_model" in data:
        fields["local_model"] = data["local_model"]
    if "ooda_enabled" in data:
        fields["ooda_enabled"] = int(data["ooda_enabled"])
    if "folder" in data:
        fields["folder"] = data["folder"]
    if fields:
        await db.update_conversation_fields(conv_id, **fields)
    return await db.get_conversation(conv_id)


@app.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(conv_id: int):
    await db.delete_conversation(conv_id)
    return {"ok": True}


# ── Bookmarks ──


@app.get("/api/bookmarks")
async def api_get_all_bookmarks():
    return await db.get_all_bookmarks()


@app.get("/api/conversations/{conv_id}/bookmarks")
async def api_get_bookmarks(conv_id: int):
    return await db.get_bookmarks(conv_id)


@app.post("/api/conversations/{conv_id}/bookmarks")
async def api_add_bookmark(conv_id: int, data: dict):
    return await db.add_bookmark(
        conv_id,
        data["message_id"],
        data.get("branch_name", ""),
        data.get("description", ""),
    )


@app.put("/api/bookmarks/{bookmark_id}")
async def api_update_bookmark(bookmark_id: int, data: dict):
    return await db.update_bookmark(bookmark_id, data.get("description", ""))


@app.delete("/api/bookmarks/{bookmark_id}")
async def api_delete_bookmark(bookmark_id: int):
    await db.delete_bookmark(bookmark_id)
    return {"ok": True}


# ── State Cards ──


@app.get("/api/state-schemas")
async def api_get_state_schemas():
    return await db.get_state_schemas()


@app.get("/api/conversations/{conv_id}/state")
async def api_get_state_cards(conv_id: int, schema_id: str = None):
    return await db.get_state_cards(conv_id, schema_id)


@app.post("/api/conversations/{conv_id}/state")
async def api_create_state_card(conv_id: int, data: dict):
    return await db.create_state_card(
        conv_id,
        data["schema_id"],
        data["label"],
        data.get("data", {}),
        data.get("is_readonly", False),
    )


@app.put("/api/state/{card_id}")
async def api_update_state_card(card_id: int, data: dict):
    return await db.update_state_card(card_id, data.get("data", {}))


@app.delete("/api/state/{card_id}")
async def api_delete_state_card(card_id: int):
    await db.delete_state_card(card_id)
    return {"ok": True}


# Conversation-scoped variants — used by the backstage MCP server so a
# compromised agent can't mutate cards in other conversations by guessing ids.
@app.put("/api/conversations/{conv_id}/state/{card_id}")
async def api_update_scoped_state_card(conv_id: int, card_id: int, data: dict):
    existing = await db.get_state_card(card_id) if hasattr(db, "get_state_card") else None
    if existing is None:
        # Fallback: fetch via the conversation list
        all_cards = await db.get_state_cards(conv_id)
        existing = next((c for c in all_cards if c.get("id") == card_id), None)
    if not existing or existing.get("conversation_id") != conv_id:
        raise HTTPException(404, "Card not found in this conversation")
    return await db.update_state_card(card_id, data.get("data", {}))


@app.delete("/api/conversations/{conv_id}/state/{card_id}")
async def api_delete_scoped_state_card(conv_id: int, card_id: int):
    all_cards = await db.get_state_cards(conv_id)
    if not any(c.get("id") == card_id for c in all_cards):
        raise HTTPException(404, "Card not found in this conversation")
    await db.delete_state_card(card_id)
    return {"ok": True}


@app.get("/api/conversations/{conv_id}/branch-state/{msg_id}")
async def api_get_branch_state(conv_id: int, msg_id: int):
    """Get effective state for a specific branch point (base + deltas)."""
    return await db.get_branch_state(conv_id, msg_id)


@app.post("/api/conversations/{conv_id}/backstage")
async def api_get_or_create_backstage(conv_id: int):
    """Return the backstage conversation for this parent, creating on first call."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if conv.get("backstage_parent_id"):
        raise HTTPException(400, "Cannot create backstage of a backstage conversation")
    return await db.get_or_create_backstage(conv_id)


@app.post("/api/conversations/{conv_id}/state/seed")
async def api_seed_state_cards(conv_id: int):
    """Auto-seed state cards from the conversation's character, persona, and lore."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    cards_created = []

    # Seed from character — prefer Tier 1 global state cards, fallback to text extraction
    if conv.get("character_id"):
        char_id = conv["character_id"]
        global_cards = await db.get_character_state_cards(char_id)
        if global_cards:
            # Copy Tier 1 → Tier 2
            count = await db.copy_character_state_to_conversation(char_id, conv_id)
            if count:
                cards_created.extend(await db.get_state_cards(conv_id))
        else:
            # Fallback: extract from character text
            char = load_character(os.path.join(config.characters_dir, f"{char_id}.md"))
            if char:
                card = await db.create_state_card(
                    conv_id,
                    "character_state",
                    char.get("name", "Character"),
                    {
                        "personality": char.get("personality", ""),
                        "appearance": "",
                        "current_mood": "",
                        "current_goal": "",
                        "physical_state": "",
                        "speech_pattern": "",
                        "relationship_to_player": "",
                        "secrets": "",
                    },
                )
                if card:
                    cards_created.append(card)
                if char.get("scenario"):
                    scene = await db.create_state_card(
                        conv_id,
                        "scene_state",
                        "current",
                        {
                            "location": "",
                            "time_of_day": "",
                            "atmosphere": "",
                            "characters_present": "",
                            "recent_events": char["scenario"],
                            "tension_level": "",
                        },
                    )
                    if scene:
                        cards_created.append(scene)

    # Seed persona as persona_state card
    if conv.get("persona_id"):
        persona = load_persona(os.path.join("personas", f"{conv['persona_id']}.md"))
        if persona:
            card = await db.create_state_card(
                conv_id,
                "persona_state",
                persona["name"],
                {
                    "description": persona.get("content", ""),
                    "appearance": "",
                    "goals": "",
                },
            )
            if card:
                cards_created.append(card)

    # Seed from lore
    if conv.get("lore_ids"):
        try:
            lore_ids = (
                json.loads(conv["lore_ids"])
                if isinstance(conv["lore_ids"], str)
                else conv["lore_ids"]
            )
        except (ValueError, TypeError):
            lore_ids = []
        for lid in lore_ids:
            entry = load_lore_entry(os.path.join("lore", f"{lid}.md"))
            if entry:
                card = await db.create_state_card(
                    conv_id,
                    "lore",
                    entry["name"],
                    {
                        "content": entry["content"],
                    },
                    is_readonly=True,
                )
                if card:
                    cards_created.append(card)

    return {"seeded": len(cards_created), "cards": cards_created}


# ── Tree ──

GREEK = [
    "α",
    "β",
    "γ",
    "δ",
    "ε",
    "ζ",
    "η",
    "θ",
    "ι",
    "κ",
    "λ",
    "μ",
    "ν",
    "ξ",
    "ο",
    "π",
    "ρ",
    "σ",
    "τ",
    "υ",
    "φ",
    "χ",
    "ψ",
    "ω",
]


def _compute_branch_names(tree: list[dict]) -> dict[int, str]:
    """Compute branch position labels (matching tree.js UI) for all nodes."""
    node_map = {n["id"]: n for n in tree}
    children_map: dict[int | None, list[int]] = {}
    roots = []
    for n in tree:
        pid = n.get("parent_id")
        if pid is None:
            roots.append(n["id"])
        else:
            children_map.setdefault(pid, []).append(n["id"])
    # Sort children by id (creation order) to match JS
    for k in children_map:
        children_map[k].sort()
    roots.sort()

    names = {}

    def _get_label(depth):
        return GREEK[depth] if depth < len(GREEK) else f"branch{depth}"

    def walk(node_id, prefix, pos, fork_depth):
        label = f"{prefix}{pos}" if prefix else f"{pos}"
        names[node_id] = label
        kids = children_map.get(node_id, [])
        if len(kids) == 1:
            walk(kids[0], prefix, pos + 1, fork_depth)
        elif len(kids) > 1:
            for i, kid in enumerate(kids):
                fl = _get_label(i)
                new_prefix = f"{prefix}{pos}.{fl}" if prefix else f"{pos}.{fl}"
                walk(kid, new_prefix, 1, fork_depth + 1)

    for i, root in enumerate(roots):
        root_label = _get_label(i) if len(roots) > 1 else ""
        walk(root, root_label, 1, 0)
    return names


@app.get("/api/conversations/{conv_id}/tree")
async def api_get_tree(conv_id: int):
    tree = await db.get_conversation_tree(conv_id)
    return tree


@app.get("/api/conversations/{conv_id}/tree/map")
async def api_get_tree_map(conv_id: int):
    """Return tree with branch position labels and session info for debugging."""
    d = await db.get_db()
    rows = await d.execute_fetchall(
        """SELECT id, parent_id, role, cc_session_id,
                  length(content) as content_len,
                  length(content_blocks) as blocks_len,
                  summary
           FROM messages WHERE conversation_id = ?
           ORDER BY created_at""",
        (conv_id,),
    )
    nodes = [dict(r) for r in rows]
    names = _compute_branch_names(nodes)
    result = []
    for n in nodes:
        sess = n.get("cc_session_id")
        result.append(
            {
                "pos": names.get(n["id"], "?"),
                "id": n["id"],
                "parent": n.get("parent_id"),
                "role": n["role"][0],  # u/a
                "session": sess[:8] if sess else None,
                "content": n.get("content_len") or 0,
                "blocks": n.get("blocks_len") or 0,
                "summary": (n.get("summary") or "")[:50],
            }
        )
    return result


@app.put("/api/conversations/{conv_id}/messages/{msg_id}")
async def api_update_message(conv_id: int, msg_id: int, data: dict):
    """Update a user message's content in-place (no new branch)."""
    content = data.get("content", "").strip()
    if not content:
        return {"ok": False, "error": "Content cannot be empty"}
    image_path = data.get("image_path")
    await db.update_message_content(msg_id, content=content)
    if image_path is not None:
        d = await get_db()
        ip = json.dumps(image_path) if isinstance(image_path, list) else image_path
        await d.execute("UPDATE messages SET image_path = ? WHERE id = ?", (ip, msg_id))
        await d.commit()
    msg = await db.get_message(msg_id)
    return msg


@app.delete("/api/conversations/{conv_id}/messages/{msg_id}")
async def api_delete_branch(conv_id: int, msg_id: int):
    """Delete a message and its entire subtree."""
    result = await db.delete_branch(msg_id)
    if result["deleted"] == 0:
        return {"ok": False, "error": "Message not found"}

    # If we deleted part of the active branch, re-activate from parent or first remaining root
    if result.get("parent_id"):
        await db.set_active_branch(conv_id, result["parent_id"])
    else:
        # Deleted a root — try to activate another root if any exist
        tree = await db.get_conversation_tree(conv_id)
        if tree:
            # Find a leaf to activate
            ids = {n["id"] for n in tree}
            parent_ids = {n["parent_id"] for n in tree if n["parent_id"]}
            leaves = ids - parent_ids
            if leaves:
                await db.set_active_branch(conv_id, next(iter(leaves)))

    return {"ok": True, "deleted": result["deleted"]}


# ── Branch ──


@app.get("/api/conversations/{conv_id}/branch/{leaf_id}")
async def api_get_branch(conv_id: int, leaf_id: int):
    branch = await db.get_branch_to_root(leaf_id)
    return branch


@app.post("/api/conversations/{conv_id}/switch-branch/{leaf_id}")
async def api_switch_branch(conv_id: int, leaf_id: int):
    # Walk down from clicked node to deepest descendant (follow first/latest child)
    current = leaf_id
    while True:
        children = await db.get_children(current)
        if not children:
            break
        current = max(children, key=lambda c: c.get("created_at", 0))["id"]
    await db.set_active_branch(conv_id, current)
    branch = await db.get_active_branch(conv_id)
    return branch


# ── Messages ──


@app.post("/api/conversations/{conv_id}/messages")
async def api_add_message(conv_id: int, data: dict):
    role = data.get("role", "user")
    content = data.get("content", "")
    raw_image_path = data.get("image_path")
    # Distinguish between "parent_id not provided" (auto-detect) and "parent_id: null" (root)
    parent_id_provided = "parent_id" in data
    parent_id = data.get("parent_id")

    # Normalize image_path: accept string, list, or null → store as JSON array or null
    if isinstance(raw_image_path, list):
        image_path = json.dumps(raw_image_path) if raw_image_path else None
    elif raw_image_path:
        image_path = raw_image_path  # legacy single string
    else:
        image_path = None

    if not content.strip() and not image_path:
        raise HTTPException(400, "Message content required")

    # If parent_id was not provided at all, use the current active leaf.
    # If parent_id was explicitly null, create a root message.
    if not parent_id_provided:
        leaf = await db.get_active_leaf(conv_id)
        parent_id = leaf["id"] if leaf else None

    describe_context = data.get("describe_context", "").strip() or None
    msg = await db.add_message(
        conv_id, role, content, parent_id=parent_id, image_path=image_path,
        describe_context=describe_context
    )
    await db.set_active_branch(conv_id, msg["id"])
    from context_manager import update_rolling_summary
    asyncio.create_task(update_rolling_summary(conv_id))
    return msg


@app.get("/api/conversations/{conv_id}/messages/{msg_id}/siblings")
async def api_get_siblings(conv_id: int, msg_id: int):
    siblings = await db.get_siblings(msg_id)
    return siblings


@app.get("/api/conversations/{conv_id}/messages/{msg_id}/children")
async def api_get_children(conv_id: int, msg_id: int):
    children = await db.get_children(msg_id)
    return children


# ── Regenerate (branch) ──


@app.post("/api/conversations/{conv_id}/regenerate/{msg_id}")
async def api_regenerate(conv_id: int, msg_id: int):
    """Create a branch point: new sibling of msg_id from the same parent."""
    msg = await db.get_message(msg_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    # Return parent info so client knows where to generate from
    return {"parent_id": msg["parent_id"], "original_id": msg_id}


@app.post("/api/conversations/{conv_id}/redescribe/{msg_id}")
async def api_redescribe(conv_id: int, msg_id: int):
    """Re-describe all images attached to a user message.

    Emits describe_start/describe_done WebSocket events so the UI can show
    a spinner and updated description.
    """
    msg = await db.get_message(msg_id)
    if not msg or not msg.get("image_path"):
        raise HTTPException(404, "Message not found or has no images")

    img_paths = _parse_image_paths(msg["image_path"])
    desc_map: dict[str, str] = {}
    _describe_model = config.vision_model or config.llama_model

    # Get describe context from the message
    _ctx = msg.get("describe_context") or None

    await _ws_send(conv_id, {
        "type": "describe_start",
        "parent_msg_id": msg_id,
        "image_count": len(img_paths),
        "model": _describe_model,
        "started_at": _time.time()
    })

    _desc_start = _time.time()
    for ip in img_paths:
        src = Path(ip).resolve()
        try:
            desc = await asyncio.wait_for(describe_image(str(src), model=config.vision_model or None, context=_ctx), timeout=120)
            desc_map[src.name] = desc
        except asyncio.TimeoutError:
            print(f"[DESCRIBE] Timed out describing {src.name}")
        except Exception as e:
            print(f"[DESCRIBE] Failed to describe image {src.name}: {e}")

    _elapsed = (_time.time() - _desc_start) * 1000

    if desc_map:
        try:
            await db.update_message_image_alt(msg_id, json.dumps(desc_map))

            _describe_content = f"[Image description — {_describe_model}]\n\n" + "\n\n".join(desc_map.values())
            _db = await db.get_db()
            existing_rows = await _db.execute_fetchall(
                "SELECT id FROM messages WHERE conversation_id=? AND role='system' AND parent_id=? AND content LIKE ?",
                (conv_id, msg_id, "[Image description —%")
            )
            if existing_rows:
                await _db.execute("UPDATE messages SET content=? WHERE id=?", (_describe_content, existing_rows[0]["id"]))
            else:
                await db.add_message(conv_id, "system", _describe_content, parent_id=msg_id)
        except Exception as e:
            print(f"[DESCRIBE] Failed to persist descriptions: {e}")

    await _ws_send(conv_id, {
        "type": "describe_done",
        "parent_msg_id": msg_id,
        "descriptions": desc_map,
        "model": _describe_model,
        "elapsed_ms": _elapsed
    })

    return {"descriptions": desc_map, "elapsed_ms": _elapsed, "model": _describe_model}


@app.post("/api/conversations/{conv_id}/fork/{msg_id}")
async def api_fork_conversation(conv_id: int, msg_id: int):
    """Fork a conversation from a specific message, creating a new conversation."""
    new_conv = await db.fork_conversation(conv_id, msg_id)
    if not new_conv:
        raise HTTPException(404, "Conversation not found")
    return new_conv


# ── Export / Import ──


@app.get("/api/conversations/{conv_id}/export")
async def api_export_conversation(conv_id: int):
    """Export a conversation with all messages as JSON."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    tree = await db.get_conversation_tree(conv_id)
    # Get full message content (tree only has preview)
    full_db = await db.get_db()
    rows = await full_db.execute_fetchall(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
        (conv_id,),
    )
    await full_db.close()
    messages = [dict(r) for r in rows]
    export = {
        "type": "loom_conversation",
        "version": 1,
        "conversation": dict(conv),
        "messages": messages,
    }
    # Sanitize filename for HTTP header (ASCII only)
    import unicodedata

    safe_title = unicodedata.normalize("NFKD", conv["title"] or "conversation")
    safe_title = (
        safe_title.encode("ascii", "ignore").decode("ascii").strip() or "conversation"
    )
    return JSONResponse(
        export,
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.json"'},
    )


@app.post("/api/conversations/import")
async def api_import_conversation(file: UploadFile = File(...)):
    """Import a conversation from JSON."""
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    if data.get("type") != "loom_conversation":
        raise HTTPException(400, "Not a Loom conversation export")

    conv_data = data["conversation"]
    new_conv = await db.create_conversation(
        conv_data.get("title", "Imported"),
        conv_data.get("character_id"),
    )
    # Update optional fields
    fields = {}
    for key in ("persona_id", "lore_ids", "style_nudge", "custom_scene"):
        if conv_data.get(key):
            fields[key] = conv_data[key]
    if fields:
        await db.update_conversation_fields(new_conv["id"], **fields)

    # Import messages, mapping old IDs to new
    id_map = {}
    for msg in data.get("messages", []):
        new_parent = id_map.get(msg.get("parent_id")) if msg.get("parent_id") else None
        new_msg = await db.add_message(
            new_conv["id"],
            msg["role"],
            msg.get("content", ""),
            parent_id=new_parent,
            image_path=msg.get("image_path"),
        )
        id_map[msg["id"]] = new_msg["id"]
        if msg.get("summary"):
            await db.update_message_summary(new_msg["id"], msg["summary"])
        if msg.get("is_active"):
            await db.set_active_branch(new_conv["id"], new_msg["id"])

    return await db.get_conversation(new_conv["id"])


@app.get("/api/characters/{char_id}/export")
async def api_export_character(char_id: str):
    """Download a character .md file."""
    filepath = os.path.join(config.characters_dir, f"{char_id}.md")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Character not found")
    return FileResponse(filepath, filename=f"{char_id}.md", media_type="text/markdown")


@app.post("/api/characters/import")
async def api_import_character(file: UploadFile = File(...)):
    """Import a character from a .md file."""
    content = (await file.read()).decode("utf-8")
    filename = Path(file.filename).stem
    filepath = os.path.join(config.characters_dir, f"{filename}.md")
    os.makedirs(config.characters_dir, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    char = load_character(filepath)
    return char or {"id": filename, "name": filename}


@app.get("/api/personas/{persona_id}/export")
async def api_export_persona(persona_id: str):
    filepath = os.path.join("personas", f"{persona_id}.md")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Persona not found")
    return FileResponse(
        filepath, filename=f"{persona_id}.md", media_type="text/markdown"
    )


@app.post("/api/personas/import")
async def api_import_persona(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8")
    filename = Path(file.filename).stem
    filepath = os.path.join("personas", f"{filename}.md")
    os.makedirs("personas", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    persona = load_persona(filepath)
    return persona or {"id": filename, "name": filename}


@app.get("/api/lore/{lore_id}/export")
async def api_export_lore(lore_id: str):
    filepath = os.path.join("lore", f"{lore_id}.md")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Lore not found")
    return FileResponse(filepath, filename=f"{lore_id}.md", media_type="text/markdown")


@app.post("/api/lore/import")
async def api_import_lore(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8")
    filename = Path(file.filename).stem
    filepath = os.path.join("lore", f"{filename}.md")
    os.makedirs("lore", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    entry = load_lore_entry(filepath)
    return entry or {"id": filename, "name": filename}


# ── File Upload ──

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_TEXT_EXTS = {
    ".md",
    ".txt",
    ".pdf",
    ".json",
    ".csv",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".sh",
    ".bat",
    ".ps1",
    ".log",
    ".rst",
    ".tex",
    ".r",
    ".sql",
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
}
_ALLOWED_EXTS = _IMAGE_EXTS | _TEXT_EXTS


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file format: {ext}")

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(config.upload_dir, filename)

    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)

    is_image = ext in _IMAGE_EXTS

    # For local/braid mode: also copy to project_dir so CC can read it
    # We'll determine project_dir later from the message context
    # For now, store the relative path so we can resolve it when needed
    return {
        "path": filepath,
        "url": f"/uploads/{filename}",
        "is_image": is_image,
        "original_name": file.filename,
    }


# ── Config ──


@app.get("/api/config")
async def api_get_config():
    return config.to_dict()


@app.put("/api/config")
async def api_update_config(data: dict):
    config.update_from_dict(data)
    return config.to_dict()


def load_local_codex_models() -> list[dict]:
    """Load Codex models from the local ~/.codex/models_cache.json file if it exists."""
    import os
    from pathlib import Path
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    cache_path = home / ".codex" / "models_cache.json"
    if not cache_path.exists():
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        models = []
        for m in data.get("models", []):
            slug = m.get("slug")
            display_name = m.get("display_name")
            if slug and display_name:
                models.append({
                    "value": f"codex-{slug}",
                    "label": f"Codex ({display_name})"
                })
        return models
    except Exception as e:
        print(f"[CODEX] Failed to load local models cache: {e}")
        return []


def get_initial_codex_models() -> list[dict]:
    local_models = load_local_codex_models()
    if local_models:
        return local_models
    return [
        {"value": "codex-gpt-5.5", "label": "Codex (GPT-5.5)"},
        {"value": "codex-gpt-5.4", "label": "Codex (GPT-5.4)"},
        {"value": "codex-gpt-5.4-mini", "label": "Codex (GPT-5.4-mini)"},
        {"value": "codex-gpt-5.3-codex", "label": "Codex (GPT-5.3-codex)"},
        {"value": "codex-gpt-4o", "label": "Codex (GPT-4o)"},
    ]


# ── CC Model List ──
# Single source of truth for Anthropic + Gemini models available in Loom/Braid.
# Update this list when new models ship — all dropdowns pull from here.

CC_MODELS = [
    {"group": "Anthropic", "models": [
        {"value": "sonnet", "label": "Sonnet"},
        {"value": "sonnet[1m]", "label": "Sonnet (1M)"},
        {"value": "opus", "label": "Opus"},
        {"value": "opus[1m]", "label": "Opus (1M)"},
        {"value": "haiku", "label": "Haiku"},
    ]},
    {"group": "Antigravity (agy)", "models": [
        {"value": "Gemini 3.5 Flash (Low)", "label": "Gemini 3.5 Flash (Low)"},
        {"value": "Gemini 3.5 Flash (High)", "label": "Gemini 3.5 Flash (High)"},
        {"value": "Gemini 3.5 Flash (Medium)", "label": "Gemini 3.5 Flash (Medium)"},
        {"value": "Gemini 3.1 Pro (High)", "label": "Gemini 3.1 Pro (High)"},
        {"value": "Gemini 3.1 Pro (Low)", "label": "Gemini 3.1 Pro (Low)"},
        {"value": "Claude Sonnet 4.6 (Thinking)", "label": "Claude Sonnet 4.6 (Thinking)"},
        {"value": "Claude Opus 4.6 (Thinking)", "label": "Claude Opus 4.6 (Thinking)"},
        {"value": "GPT-OSS 120B (Medium)", "label": "GPT-OSS 120B (Medium)"},
    ]},
    {"group": "ChatGPT Codex (codex)", "models": get_initial_codex_models()},
]


@app.get("/api/cc-models")
async def api_cc_models():
    return CC_MODELS


# 1M-context support is per family+version, not per family. Subscription users
# can only enable 1M via the CLI's `[1m]` model suffix (`--betas` is
# API-key-only), and the CLI's suffix parser is independent of alias-vs-full-ID
# resolution, so `sonnet[1m]` and `claude-sonnet-4-7[1m]` both work. Sonnet
# supports 1M across our surfaced range; Opus only from 4.7 onward (Opus 4.6 has
# no 1M tier and a pinned `claude-opus-4-6[1m]` just rate-limits/fails); Haiku
# never. Aliases (`opus`, `sonnet`) resolve to the latest version, which always
# supports 1M, so the Auto group emits `[1m]` for both.
_CC_ONEM_MIN_VERSION = {"fable": (5, 0), "opus": (4, 7), "sonnet": (4, 5)}


def _supports_1m(family: str, version: tuple[int, int]) -> bool:
    floor = _CC_ONEM_MIN_VERSION.get(family)
    return floor is not None and version >= floor


# Family display order, applied to both the Auto group and within Pinned.
_CC_FAMILY_ORDER = ["fable", "opus", "sonnet", "haiku"]
_CC_ALIAS_FAMILIES = {"opus", "sonnet", "haiku"}

import re as _re
# claude-<family>-<major>[-<minor>][-<yyyymmdd>] — minor is 1-2 digits and
# optional (e.g. `claude-opus-4-20250514` is version 4.0 with a date, NOT
# minor=20250514). The optional 8-digit date tail is anchored so it can't be
# mistaken for the minor.
_CC_MODEL_ID_RE = _re.compile(
    r"^claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$",
    _re.IGNORECASE,
)


def _read_oauth_token() -> str | None:
    """Read the Claude Code subscription OAuth access token from the standard
    credentials file. Returns None if missing/unreadable. The expiresAt timestamp
    is intentionally not checked — the API returns 401 if it's actually expired,
    and reading the file each call means the user can refresh their session via
    the Claude Code CLI without restarting Loom."""
    cred_path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return (data.get("claudeAiOauth") or {}).get("accessToken")


def _build_anthropic_model_dropdowns(api_models: list[dict]) -> tuple[list[dict], list[dict], dict[str, list]]:
    # Group every returned claude-* model by family. display_name comes
    # pre-formatted ("Claude Opus 4.8"); strip the "Claude " prefix so labels
    # read "Opus 4.8" to match the compact dropdown style.
    by_family: dict[str, list[tuple[tuple[int, int], str, str]]] = {
        f: [] for f in _CC_FAMILY_ORDER
    }
    for m in api_models:
        model_id = m.get("id", "")
        match = _CC_MODEL_ID_RE.match(model_id)
        if not match:
            continue
        family = match.group(1).lower()
        if family not in by_family:
            continue
        version = (int(match.group(2)), int(match.group(3) or 0))
        if version < (4, 5):
            continue
        label = (m.get("display_name") or "").removeprefix("Claude ").strip()
        if not label:
            label = f"{family.capitalize()} {version[0]}.{version[1]}"
        by_family[family].append((version, model_id, label))

    if not any(by_family.values()):
        raise ValueError("Anthropic /v1/models returned no recognizable claude-* models.")

    for family in by_family:
        by_family[family].sort(key=lambda t: t[0], reverse=True)

    # Auto group — one alias entry per family with a known Claude Code alias.
    # The CLI's bundled alias map (frozen at the CLI's build time) decides which
    # concrete ID runs, so the label can't honestly name a version.
    auto_models: list[dict] = []
    for family in _CC_FAMILY_ORDER:
        if not by_family[family]:
            continue
        if family not in _CC_ALIAS_FAMILIES:
            continue
        cap = family.capitalize()
        auto_models.append({"value": family, "label": f"{cap} (latest)"})
        # Alias resolves to the family's latest version, which always supports 1M
        # if any version in the family does.
        if family in _CC_ONEM_MIN_VERSION:
            auto_models.append({"value": f"{family}[1m]", "label": f"{cap} (latest, 1M)"})

    # Pinned group — every concrete claude-* ID, with [1m] siblings interleaved
    # per version so each release block stays together. Fable's documented API
    # ID is already 1M-context, so we do not add a synthetic suffix variant.
    pinned_models: list[dict] = []
    for family in _CC_FAMILY_ORDER:
        for _ver, model_id, label in by_family[family]:
            pinned_models.append({"value": model_id, "label": label})
            if family != "fable" and _supports_1m(family, _ver):
                pinned_models.append({"value": f"{model_id}[1m]", "label": f"{label} (1M)"})

    return auto_models, pinned_models, by_family


@app.post("/api/cc-models/refresh")
async def api_cc_models_refresh():
    """Rebuild the Anthropic dropdown from the live `/v1/models` API, using the
    local Claude Code subscription OAuth token (no API key, no generation —
    one free metadata request). Produces two groups in place of the old single
    "Anthropic" group:

      • "Anthropic — Auto"   alias values (`opus`, `sonnet[1m]`, …) labeled
                              "(latest)" without a specific version, since the
                              CLI's own bundled alias→ID map decides which
                              concrete model actually runs.
      • "Anthropic — Pinned" every full claude-* ID the API returns, so the
                              user can pin an exact version. Each version is
                              followed by its `[1m]` sibling where the version
                              supports an explicit 1M suffix (Sonnet ≥4.5,
                              Opus ≥4.7). Fable is surfaced as a pinned ID
                              because the documented API ID is `claude-fable-5`.

    Mutates CC_MODELS in place so existing references stay valid."""
    token = _read_oauth_token()
    if not token:
        raise HTTPException(
            401,
            "No Claude Code login found (~/.claude/.credentials.json). "
            "Run `claude` once to sign in, then retry.",
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                params={"limit": 100},
                headers=headers,
            )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Failed to reach Anthropic /v1/models: {e}")

    if resp.status_code == 401:
        raise HTTPException(
            401,
            "Anthropic rejected the login token (expired?). "
            "Run `claude` and complete sign-in again, then retry.",
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"Anthropic /v1/models returned {resp.status_code}: {resp.text[:200]}")

    try:
        auto_models, pinned_models, by_family = _build_anthropic_model_dropdowns(resp.json().get("data", []))
    except ValueError as e:
        raise HTTPException(502, str(e))

    # Splice the two new groups in where the old Anthropic group(s) were, keep
    # everything else in original order. Handles both the initial hardcoded
    # "Anthropic" group and any prior refresh's "Anthropic — Auto/Pinned" pair.
    new_cc_models: list[dict] = []
    inserted = False
    for group in CC_MODELS:
        if group["group"].startswith("Anthropic"):
            if not inserted:
                new_cc_models.append({"group": "Anthropic — Auto", "models": auto_models})
                new_cc_models.append({"group": "Anthropic — Pinned", "models": pinned_models})
                inserted = True
        else:
            new_cc_models.append(group)
    if not inserted:
        new_cc_models.insert(0, {"group": "Anthropic — Pinned", "models": pinned_models})
        new_cc_models.insert(0, {"group": "Anthropic — Auto", "models": auto_models})

    # Also reload and update Codex models cache in CC_MODELS
    try:
        local_models = load_local_codex_models()
        if local_models:
            for group in new_cc_models:
                if group["group"].startswith("ChatGPT Codex"):
                    group["models"] = local_models
                    break
    except Exception as e:
        print(f"[REFRESH] Failed to update Codex models: {e}")

    CC_MODELS[:] = new_cc_models
    return {"models": CC_MODELS, "families": sorted(f for f, v in by_family.items() if v)}


_VISION_MODEL_CACHE: dict[tuple[str, str], bool] = {}


@app.get("/api/llama/models")
async def api_llama_models():
    """Return .gguf models from disk plus any models reported by live llama-server."""
    import llama_client as _lc
    disk_models = _lc.list_local_models()
    status = await _lc.health_check()
    live = status.get("models", [])
    all_m = list(disk_models)
    for m in live:
        if m not in all_m:
            all_m.append(m)
    return {"models": all_m, "live": live, "disk": disk_models}


# ── Directory Browser (for Claude mode project picker) ──


@app.get("/api/browse-dirs")
async def api_browse_dirs(path: str = ""):
    """List subdirectories of a given path for the folder picker UI."""
    import string

    if not path:
        # Return drive roots on Windows, or / on Unix
        if os.name == "nt":
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.isdir(drive):
                    drives.append({"name": f"{letter}:", "path": drive})
            return {"parent": None, "dirs": drives, "current": ""}
        else:
            path = "/"

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise HTTPException(400, "Not a directory")

    parent = os.path.dirname(path) if path != os.path.dirname(path) else None

    try:
        entries = []
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                entries.append({"name": entry.name, "path": entry.path})
        return {"parent": parent, "dirs": entries, "current": path}
    except PermissionError:
        return {
            "parent": parent,
            "dirs": [],
            "current": path,
            "error": "Permission denied",
        }


# ── Serve Project Files (images, etc.) ──

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}


@app.get("/api/conversations/{conv_id}/file")
async def serve_project_file(conv_id: int, path: str = ""):
    """Serve a file from a conversation's project directory (scoped, images only by default)."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    project_dir = conv.get("project_dir")
    if not project_dir:
        raise HTTPException(400, "No project directory set")

    base = Path(project_dir).resolve()
    target = (base / path).resolve()

    # Prevent path traversal
    if not str(target).startswith(str(base)):
        raise HTTPException(403, "Path traversal blocked")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")

    suffix = target.suffix.lower()
    media_type = _MIME_MAP.get(suffix)
    if not media_type:
        # Allow text files too for previews
        media_type = "text/plain"

    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
        content_disposition_type="inline",
    )


@app.get("/api/conversations/{conv_id}/artifacts")
async def serve_project_artifacts(conv_id: int):
    """Scan conversation's project directory for planning and artifact files."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    project_dir = conv.get("project_dir")
    if not project_dir:
        raise HTTPException(400, "No project directory set")

    base = Path(project_dir).resolve()
    if not base.exists() or not base.is_dir():
        return {"artifacts": []}

    # Files we want to check for in the workspace
    standard_files = ["CLAUDE.md", "task.md", "tasks.md", "walkthrough.md", "implementation_plan.md"]
    artifacts = []

    # 1. Check standard root files
    for filename in standard_files:
        target = base / filename
        if target.exists() and target.is_file():
            artifacts.append({
                "name": filename,
                "path": filename,
                "size": target.stat().st_size,
                "modified": target.stat().st_mtime
            })

    # 2. Check files inside .claude/plans/
    plans_dir = base / ".claude" / "plans"
    if plans_dir.is_dir():
        try:
            for p in plans_dir.glob("*.md"):
                if p.is_file():
                    rel_path = f".claude/plans/{p.name}"
                    artifacts.append({
                        "name": p.name,
                        "path": rel_path,
                        "size": p.stat().st_size,
                        "modified": p.stat().st_mtime
                    })
        except Exception:
            pass

    # Sort by name
    artifacts.sort(key=lambda x: x["name"].lower())
    return {"artifacts": artifacts}


# ── Claude Code Permission Hook Endpoint ──


@app.post("/api/cc-permission")
async def handle_cc_permission(data: dict):
    """Receive permission request from CC hook script, forward to UI, wait for response.

    The hook script (cc_permission_hook.py) POSTs here when CC needs tool approval.
    This endpoint long-polls until the user responds in the browser UI (no timeout).
    """
    conv_id = int(data.get("loom_conv_id", 0))
    request_id = str(uuid.uuid4())

    tool_name = data.get("tool_name", "Unknown")
    tool_input = data.get("tool_input", {})

    print(
        f"[PERM] Hook request: conv={conv_id} tool={tool_name} request_id={request_id}"
    )

    approval_method = data.get("approval_method", "")
    permission_scope = _permission_scope_for_active_generation(
        conv_id,
        data.get("permission_scope", ""),
    )
    permission_key = _permission_fingerprint(tool_name, tool_input, approval_method)
    scoped_key = (conv_id, permission_scope)

    # Auto-approve only the same request within the same branch generation.
    if permission_key in _auto_approve_permissions.get(scoped_key, set()):
        print(f"[PERM] Auto-approved scoped permission: scope={permission_scope} key={permission_key}")
        return {"allow": True, "always": True}

    # Build a human-readable summary
    input_summary = ""
    if isinstance(tool_input, dict):
        if "command" in tool_input:
            input_summary = tool_input["command"]
        elif "file_path" in tool_input:
            input_summary = tool_input["file_path"]
        elif "description" in tool_input:
            input_summary = tool_input["description"]
        else:
            input_summary = json.dumps(tool_input)[:500]
    elif isinstance(tool_input, str):
        input_summary = tool_input[:500]

    # For ExitPlanMode, read the actual plan file content
    if tool_name in ("ExitPlanMode", "exit_plan_mode"):
        plan_content = ""
        # Check project-local .claude/plans/ first, then user-level
        conv = await db.get_conversation(conv_id) if conv_id else None
        plan_dirs = []
        if conv and conv.get("project_dir"):
            plan_dirs.append(Path(conv["project_dir"]) / ".claude" / "plans")
        plan_dirs.append(Path.home() / ".claude" / "plans")
        for plan_dir in plan_dirs:
            if plan_dir.is_dir():
                plan_files = sorted(plan_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
                if plan_files:
                    try:
                        plan_content = plan_files[0].read_text(encoding="utf-8")
                        print(f"[PERM] Read plan file: {plan_files[0]} ({len(plan_content)} chars)")
                    except Exception as e:
                        print(f"[PERM] Failed to read plan file: {e}")
                    break
        if plan_content:
            input_summary = plan_content
            tool_input = {"plan": plan_content, "planFilePath": str(plan_files[0])}

    # Sanitize strings — tool_input can contain surrogate characters that crash WS send
    def _sanitize(obj):
        if isinstance(obj, str):
            return obj.encode("utf-8", errors="replace").decode("utf-8")
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    # Build permission message once — broadcast to all active WebSockets globally
    perm_msg = {
        "type": "permission_request",
        "request_id": request_id,
        "conv_id": conv_id,
        "tool_name": tool_name,
        "tool_input": _sanitize(tool_input),
        "input_summary": _sanitize(input_summary),
        "approval_method": approval_method,
        "permission_key": permission_key,
        "permission_scope": permission_scope,
    }
    gen_id = _permission_scope_gen_id(permission_scope)
    if gen_id is not None:
        perm_msg["gen_id"] = gen_id
    # Persist to DB first (survives server restart)
    await db.save_pending_permission(
        request_id, conv_id, tool_name, tool_input, input_summary
    )
    print(f"[PERM] Persisted permission request to DB: {request_id}")

    # Broadcast permission request to any connected WebSockets (for real-time notifications)
    # Don't block or deny if none are connected — let the user respond when they return
    dead_pairs = []
    sent_count = 0
    for cid, clients in list(_active_websockets.items()):
        for ws in list(clients):
            try:
                await ws.send_json(perm_msg)
                sent_count += 1
            except Exception as e:
                print(f"[PERM] Failed to send to conv={cid}: {e}")
                dead_pairs.append((cid, ws))
    print(f"[PERM] Broadcast permission_request to {sent_count} websocket(s)")
    for cid, ws in dead_pairs:
        _active_websockets.get(cid, set()).discard(ws)

    # Mark as broadcasted
    await db.mark_permission_broadcasted(request_id)

    # Store in memory dict for fast retrieval (will be loaded from DB on restart)
    _pending_hook_permissions[request_id] = {
        "event": asyncio.Event(),
        "response": None,
        "conv_id": conv_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "input_summary": input_summary,
        "approval_method": approval_method,
        "permission_key": permission_key,
        "permission_scope": permission_scope,
    }

    # Wait for user response with a timeout.
    # Keep the tool blocked while it waits for a real user decision. Pending
    # requests are persisted and rebroadcast on reconnect/startup.
    _PERM_TOTAL_DEADLINE = 24 * 60 * 60
    _PERM_POLL_INTERVAL = 30

    elapsed = 0
    user_response = {}
    while elapsed < _PERM_TOTAL_DEADLINE:
        perm_data = _pending_hook_permissions.get(request_id)
        if not perm_data:
            break  # cleaned up by cancel/finally or already responded
        if perm_data.get("response"):
            user_response = perm_data["response"]
            _pending_hook_permissions.pop(request_id, None)
            break

        # Wait on the event with a short timeout, then re-check
        try:
            await asyncio.wait_for(perm_data["event"].wait(), timeout=_PERM_POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
        elapsed += _PERM_POLL_INTERVAL

    allowed = user_response.get("allow", False)
    print(f"[PERM] User decision: {'allow' if allowed else 'deny'}")

    if allowed:
        return {"allow": True, "always": bool(user_response.get("always", False))}
    else:
        return {"allow": False, "message": "Denied by user in Loom UI"}


@app.post("/api/cc-tool-start")
async def handle_cc_tool_start(data: dict):
    """Receive tool start event from CC hook script, forward to event stream."""
    conv_id = int(data.get("loom_conv_id", 0))
    tool_name = data.get("tool_name", "")
    tool_id = data.get("tool_id", "")
    tool_input = data.get("tool_input", {})
    
    print(f"[PERM] Hook tool-start: conv={conv_id} tool={tool_name} id={tool_id}")
    
    if conv_id in gemini_client._active_queues:
        gemini_client._active_queues[conv_id].put_nowait({
            "type": "tool_start",
            "name": tool_name,
            "tool_id": tool_id,
        })
        if tool_input:
            gemini_client._active_queues[conv_id].put_nowait({
                "type": "tool_input_delta",
                "json": json.dumps(tool_input, indent=2),
                "tool_id": tool_id,
            })
    return {"status": "ok"}


@app.post("/api/cc-tool-result")
async def handle_cc_tool_result(data: dict):
    """Receive tool result event from CC hook script, forward to event stream."""
    conv_id = int(data.get("loom_conv_id", 0))
    tool_name = data.get("tool_name", "")
    tool_id = data.get("tool_id", "")
    content = data.get("content", "")
    is_error = data.get("is_error", False)
    
    print(f"[PERM] Hook tool-result: conv={conv_id} tool={tool_name} id={tool_id} error={is_error}")
    
    if conv_id in gemini_client._active_queues:
        gemini_client._active_queues[conv_id].put_nowait({
            "type": "tool_result",
            "content": content,
            "tool_id": tool_id,
            "is_error": is_error,
        })
    return {"status": "ok"}


# ── Skills & Modules ──


@app.get("/api/skills")
async def list_skills(conv_id: int = None):
    """List available skills for a conversation (or globally).
    Scans both built-in skills and .claude/skills/ in the project directory.
    """
    project_dir = None
    agent = "claude"
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv:
            project_dir = conv.get("project_dir")
            cc_model = (conv.get("cc_model") or "").lower()
            mode = (conv.get("mode") or "").lower()
            if mode == "codex" or is_codex_model(conv.get("cc_model") or ""):
                agent = "codex"
            elif mode == "gemini" or is_gemini_model(conv.get("cc_model") or "") or "antigravity" in cc_model:
                agent = "agy"
    skills = get_all_skills(project_dir, agent=agent)
    return skills


@app.get("/api/cc-hooks")
async def get_cc_hooks():
    """Read CC hooks from settings files and return them."""
    import json as _json
    hooks = {}
    # Check all settings locations CC uses
    paths = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
        Path(".claude") / "settings.json",
        Path(".claude") / "settings.local.json",
    ]
    for p in paths:
        try:
            if p.is_file():
                data = _json.loads(p.read_text(encoding="utf-8"))
                file_hooks = data.get("hooks", {})
                if file_hooks:
                    hooks[str(p)] = file_hooks
        except Exception:
            pass
    return {"hooks": hooks, "paths": [str(p) for p in paths]}


@app.get("/api/codex-diagnostics")
async def codex_diagnostics(conv_id: int = None, project_dir: str = None):
    """Report Codex state that can surprise Loom users across arbitrary folders."""
    target_dir = project_dir
    nrol_operator = False
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv:
            target_dir = conv.get("project_dir") or target_dir
            nrol_operator = bool(conv.get("nrol_operator"))
    target_path = Path(target_dir).resolve() if target_dir else Path.cwd().resolve()

    config_path = Path.home() / ".codex" / "config.toml"
    config_text = ""
    stale_global_entries: list[str] = []
    if config_path.is_file():
        try:
            config_text = config_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            config_text = f"<read failed: {e}>"
    for marker in ("loom-file-edits", "loom-actions", "mcp_loom_file_edits", "mcp_loom_actions"):
        if marker in config_text:
            stale_global_entries.append(marker)

    project_hook_path = target_path / ".codex" / "hooks.json"
    project_hooks = None
    if project_hook_path.is_file():
        try:
            project_hooks = json.loads(project_hook_path.read_text(encoding="utf-8"))
        except Exception as e:
            project_hooks = {"error": str(e)}

    expected_approval, expected_sandbox = codex_client._codex_launch_policies(
        "default", nrol_operator
    )
    return {
        "target_dir": str(target_path),
        "expected_launch": {
            "surface": "app-server",
            "approval_policy": expected_approval,
            "sandbox": expected_sandbox,
            "writable_roots": [] if nrol_operator else [str(target_path)],
            "hook_scope": "disabled",
            "mcp_servers": ["nrol-ao", "web-tools"] if nrol_operator else [],
        },
        "project_hook": {
            "path": str(project_hook_path),
            "exists": project_hook_path.is_file(),
            "scope": "project-local" if project_hook_path.is_file() else None,
            "ignored_by_loom_codex": True,
            "reason": "Codex app-server launches use --disable hooks; approval requests come from app-server JSON-RPC.",
            "hooks": project_hooks,
        },
        "global_config": {
            "path": str(config_path),
            "exists": config_path.is_file(),
            "stale_loom_entries": stale_global_entries,
        },
        "warnings": [
            *(
                ["Project-local .codex/hooks.json exists; Loom disables Codex hooks for app-server launches so stale repo hooks are ignored."]
                if project_hook_path.is_file()
                else []
            ),
            *(
                [f"Global Codex config contains stale Loom markers: {', '.join(stale_global_entries)}"]
                if stale_global_entries
                else []
            ),
        ],
    }


@app.get("/api/modules")
async def list_modules(module_type: str = None):
    """List registered modules from the database."""
    return await db.get_modules(module_type=module_type)


@app.post("/api/modules/sync")
async def sync_modules(conv_id: int = None):
    """Sync skills from filesystem into the modules database.
    Call this after changing project_dir or adding new skills.
    """
    project_dir = None
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv:
            project_dir = conv.get("project_dir")
    skills = get_all_skills(project_dir)
    synced = []
    for skill in skills:
        module = await db.upsert_module(
            module_id=skill["id"],
            name=skill["name"],
            module_type="skill",
            description=skill.get("description", ""),
            source=skill.get("source_path", "builtin"),
            config={
                "command": skill.get("command", ""),
                "prompt_template": skill.get("prompt_template", ""),
            },
        )
        synced.append(module)
    return {"synced": len(synced), "modules": synced}


@app.put("/api/modules/{module_id}/enabled")
async def toggle_module(module_id: str, data: dict):
    """Enable or disable a module."""
    enabled = data.get("enabled", True)
    await db.set_module_enabled(module_id, enabled)
    return {"ok": True}


@app.post("/api/skills/create")
async def create_user_skill(data: dict):
    """Create a custom user skill by writing a SKILL.md file.

    Expects: { conv_id, name, description, prompt_template }
    Writes to: <project_dir>/.claude/skills/<name>/SKILL.md
    """
    conv_id = data.get("conv_id")
    name = data.get("name", "").strip().lower().replace(" ", "-")
    description = data.get("description", "")
    prompt_template = data.get("prompt_template", "")

    if not name:
        raise HTTPException(400, "Skill name is required")
    if not prompt_template:
        raise HTTPException(400, "Prompt template is required")

    # Determine project directory
    project_dir = "."
    if conv_id:
        conv = await db.get_conversation(int(conv_id))
        if conv and conv.get("project_dir"):
            project_dir = conv["project_dir"]

    skills_dir = Path(project_dir) / ".claude" / "skills" / name
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skills_dir / "SKILL.md"

    content = f"---\ndescription: {description}\n---\n\n{prompt_template}\n"
    skill_md.write_text(content, encoding="utf-8")

    return {
        "ok": True,
        "path": str(skill_md),
        "skill": {
            "id": f"skill:custom:{name}",
            "name": name,
            "command": f"/{name}",
            "description": description,
            "prompt_template": prompt_template,
        },
    }


@app.get("/api/skills/user")
async def list_user_skills(conv_id: int = None):
    """List only user-created custom skills (from .claude/skills/)."""
    from skill_scanner import scan_skills_dir

    project_dir = "."
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv and conv.get("project_dir"):
            project_dir = conv["project_dir"]
    return scan_skills_dir(project_dir)


@app.delete("/api/skills/user/{skill_name}")
async def delete_user_skill(skill_name: str, conv_id: int = None):
    """Delete a user-created skill by removing its directory."""
    import shutil

    project_dir = "."
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv and conv.get("project_dir"):
            project_dir = conv["project_dir"]
    skills_dir = Path(project_dir) / ".claude" / "skills" / skill_name
    if not skills_dir.exists():
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    shutil.rmtree(skills_dir)
    return {"ok": True, "deleted": skill_name}


# ── WebSocket Chat ──


def _scrub_surrogates(obj):
    """Strip unpaired UTF-16 surrogates from strings in a JSON-serializable obj.

    CC tool inputs (especially Bash command strings chunked mid-codepoint) can
    contain lone surrogates like '\\udc90'. json.dumps + websocket.send_json
    encodes to UTF-8 which rejects them, raising UnicodeEncodeError and
    tearing down the WS handler. This recurses through dicts/lists and
    replaces bad chars with U+FFFD.
    """
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-8", "replace").decode("utf-8")
    if isinstance(obj, dict):
        return {k: _scrub_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_surrogates(v) for v in obj]
    return obj


async def _ws_send(conv_id: int, data: dict):
    """Best-effort broadcast to ALL active WebSockets for a conversation.
    Silently skips dead clients — generation continues regardless.
    Auto-injects gen_id from the current task if present."""
    # Auto-tag with gen_id so client can distinguish parallel streams
    task = asyncio.current_task()
    gen_key = getattr(task, "_gen_key", None)
    if gen_key and "gen_id" not in data:
        data = {**data, "gen_id": gen_key[2]}
    clients = _active_websockets.get(conv_id)
    # TEMP DEBUG: log every WS send target + count + payload type so we can
    # verify whether content events are reaching the WebSocket layer at all.
    _t = data.get("type", "?")
    _content = data.get("content", "") or ""
    _snippet = (_content[:30] + "…") if len(_content) > 30 else _content
    print(f"[WS-TRACE] conv={conv_id} type={_t} clients={len(clients) if clients else 0} {_snippet!r}")
    if not clients:
        return
    dead = []
    scrubbed = None
    for ws in list(clients):
        try:
            await ws.send_json(data)
        except UnicodeEncodeError:
            # Unpaired surrogate in data — socket is fine, payload is bad.
            # Scrub once and retry; don't mark ws dead.
            if scrubbed is None:
                scrubbed = _scrub_surrogates(data)
            try:
                await ws.send_json(scrubbed)
            except Exception:
                dead.append(ws)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def _ws_broadcast_all(data: dict):
    """Broadcast to ALL active WebSockets across ALL conversations."""
    dead_pairs = []
    for cid, clients in list(_active_websockets.items()):
        for ws in list(clients):
            try:
                await ws.send_json(data)
            except Exception:
                dead_pairs.append((cid, ws))
    for cid, ws in dead_pairs:
        remaining = _active_websockets.get(cid)
        if remaining:
            remaining.discard(ws)
            if not remaining:
                _active_websockets.pop(cid, None)


async def _send_active_gen_state(websocket: WebSocket, conv_id: int) -> bool:
    """Send generation_active (with snapshots) or generation_idle to one ws.
    Returns True if a generation is active, False otherwise."""
    active_gen_keys = [
        k for k, t in _active_generations.items() if k[0] == conv_id and not t.done()
    ]
    if not active_gen_keys:
        await websocket.send_json({"type": "generation_idle"})
        return False
    snapshots = []
    for gk in active_gen_keys:
        snap = _generation_snapshots.get(gk)
        if snap:
            snapshots.append(
                {
                    "gen_id": gk[2],
                    "parent_id": snap.get("parent_id"),
                    "draft_msg_id": snap.get("draft_msg_id"),
                    "full_text": snap.get("full_text", ""),
                    "content_blocks": snap.get("content_blocks", []),
                    "input_tokens": snap.get("input_tokens", 0),
                    "output_tokens": snap.get("output_tokens", 0),
                    "started_at": snap.get("started_at", 0),
                    "cc_model": snap.get("cc_model", ""),
                    "mode": snap.get("mode", "claude"),
                }
            )
    await websocket.send_json(
        {
            "type": "generation_active",
            "snapshots": snapshots,
        }
    )
    return True


@app.websocket("/ws/chat/{conv_id}")
async def ws_chat(websocket: WebSocket, conv_id: int):
    await websocket.accept()
    print(f"[WS] Connection opened for conv={conv_id}")
    if conv_id not in _active_websockets:
        _active_websockets[conv_id] = set()
    _active_websockets[conv_id].add(websocket)

    # Tell the client whether a generation is running — include live snapshot if available
    has_active = await _send_active_gen_state(websocket, conv_id)
    if has_active:
        # Resend ALL pending permission requests (broadcast globally now)
        for rid, pending in list(_pending_hook_permissions.items()):
            if pending.get("response"):
                continue
            print(f"[WS] Resending pending permission request {rid} on reconnect")
            try:
                perm_msg = {
                    "type": "permission_request",
                    "request_id": rid,
                    "conv_id": pending.get("conv_id"),
                    "tool_name": pending.get("tool_name", ""),
                    "tool_input": pending.get("tool_input", ""),
                    "input_summary": pending.get("input_summary", ""),
                    "approval_method": pending.get("approval_method", ""),
                    "permission_key": pending.get("permission_key", ""),
                    "permission_scope": pending.get("permission_scope", ""),
                }
                gen_id = _permission_scope_gen_id(pending.get("permission_scope", ""))
                if gen_id is not None:
                    perm_msg["gen_id"] = gen_id
                await websocket.send_json(_scrub_surrogates(perm_msg))
            except Exception as e:
                # One malformed pending perm must not kill the whole reconnect —
                # UnicodeEncodeError on an unpaired surrogate in tool_input was
                # tearing down ws_chat before the receive loop started.
                print(f"[WS] Failed to resend pending permission {rid}: {e!r} — skipping")
    else:
        # Even when idle, resend any pending permissions from other conversations
        for rid, pending in list(_pending_hook_permissions.items()):
            if pending.get("response"):
                continue
            try:
                perm_msg = {
                    "type": "permission_request",
                    "request_id": rid,
                    "conv_id": pending.get("conv_id"),
                    "tool_name": pending.get("tool_name", ""),
                    "tool_input": pending.get("tool_input", ""),
                    "input_summary": pending.get("input_summary", ""),
                    "approval_method": pending.get("approval_method", ""),
                    "permission_key": pending.get("permission_key", ""),
                    "permission_scope": pending.get("permission_scope", ""),
                }
                gen_id = _permission_scope_gen_id(pending.get("permission_scope", ""))
                if gen_id is not None:
                    perm_msg["gen_id"] = gen_id
                await websocket.send_json(_scrub_surrogates(perm_msg))
            except Exception as e:
                print(f"[WS] Failed to resend pending permission {rid}: {e!r} — skipping")

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            if action == "ping":
                # Reply so the client's heartbeat worker sees inbound activity
                # and doesn't force-reconnect (which would trigger loadMessages
                # and wipe any in-progress textarea edit).
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    pass
                continue

            if action == "request_snapshot":
                # Client noticed its streaming UI is detached/missing while a
                # generation is still running — re-emit current state so it can
                # rebuild without a full page refresh.
                try:
                    await _send_active_gen_state(websocket, conv_id)
                except Exception as e:
                    print(f"[WS] request_snapshot failed for conv={conv_id}: {e!r}")
                continue

            if action == "cancel":
                # Cancel all active generations for this conversation
                cancelled_keys = [k for k in _active_generations if k[0] == conv_id]
                cancelled_tasks = []
                for key in cancelled_keys:
                    task = _active_generations.pop(key, None)
                    if task:
                        task.cancel()
                        cancelled_tasks.append(task)
                proc = _active_claude_procs.pop(conv_id, None)
                if proc:
                    await claude_client.cancel_claude(proc)
                hproc = _active_hermes_procs.pop(conv_id, None)
                if hproc:
                    await hermes_client.cancel_hermes(hproc)
                # Clean up pending permissions from memory and DB
                for rid in list(_pending_hook_permissions):
                    if _pending_hook_permissions[rid].get("conv_id") == conv_id:
                        _pending_hook_permissions.pop(rid, None)
                        await db.delete_pending_permission(rid)
                # Wait for tasks to finish their CancelledError handler (saves partial content).
                # Pop snapshots — the task handlers save/delete drafts themselves.
                for key in cancelled_keys:
                    _generation_snapshots.pop(key, None)
                if cancelled_tasks:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*cancelled_tasks, return_exceptions=True),
                            timeout=8.0,
                        )
                    except asyncio.TimeoutError:
                        pass  # tasks hung — already cleaned up, continue
                # Send cancelled event immediately so UI responds
                await _ws_send(conv_id, {"type": "cancelled"})
                continue

            if action == "permission_response":
                request_id = data.get("request_id", "")
                # Resolve the hook-based pending permission
                pending = _pending_hook_permissions.get(request_id)
                if pending:
                    allow = bool(data.get("allow", False))
                    pending_conv_id = int(pending.get("conv_id") or conv_id)
                    permission_key = pending.get("permission_key") or _permission_fingerprint(
                        pending.get("tool_name", ""),
                        pending.get("tool_input", {}),
                        pending.get("approval_method", ""),
                    )
                    permission_scope = pending.get("permission_scope") or _permission_scope_for_active_generation(pending_conv_id)
                    always = bool(data.get("always"))
                    if allow and always:
                        _auto_approve_permissions.setdefault((pending_conv_id, permission_scope), set()).add(permission_key)
                    pending["response"] = {
                        "allow": allow,
                        "always": always,
                    }
                    pending["event"].set()
                    # Also clean up from DB
                    await db.delete_pending_permission(request_id)
                    await _ws_broadcast_all(
                        {
                            "type": "permission_resolved",
                            "request_id": request_id,
                            "allowed": allow,
                            "conv_id": pending_conv_id,
                        }
                    )
                continue

            if action == "compact":
                # Manual compaction: launch CC with /compact as the prompt
                # using --resume to compact the existing session
                asyncio.create_task(
                    _handle_compact(websocket, conv_id, data)
                )
                continue

            print(f"[WS] Received action={action} for conv={conv_id}")
            if action in ("generate", "regenerate"):
                global _gen_seq
                parent_id = data.get("parent_id")

                # Check conversation mode to decide parallel policy
                conv = await db.get_conversation(conv_id)
                mode = conv.get("mode", "weave") if conv else "weave"
                # CLI-agent subprocess modes — Claude Code (claude), Braid (local,
                # = CC via Ollama), and Hermes (hermes acp). All three drive a
                # single subprocess per conversation, so parallel generations on
                # different branches would race the same child. (Including "local"
                # here fixes a latent Braid bug — it was previously treated like
                # Weave/OODA and allowed to spawn parallel CC subprocesses.)
                is_subprocess_agent = mode in ("claude", "local", "hermes")

                if is_subprocess_agent:
                    # Only one agent generation per conversation
                    cc_busy = any(
                        k[0] == conv_id and not t.done()
                        for k, t in _active_generations.items()
                    )
                    if cc_busy:
                        # Same client retrying → cancel old; different client → reject
                        old_key = next(
                            k
                            for k, t in _active_generations.items()
                            if k[0] == conv_id and not t.done()
                        )
                        old_task = _active_generations[old_key]
                        if websocket is getattr(old_task, "_origin_ws", None):
                            old_task.cancel()
                            _active_generations.pop(old_key, None)
                        else:
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "error": "An agent generation is already running on another branch. Wait for it to finish or cancel it first.",
                                }
                            )
                            continue
                elif action == "regenerate":
                    # Regenerate: cancel any existing gen on same parent
                    for k in [
                        k
                        for k in _active_generations
                        if k[0] == conv_id
                        and k[1] == parent_id
                        and not _active_generations[k].done()
                    ]:
                        _active_generations.pop(k).cancel()

                # Weave/OODA generate: allow parallel, even on same parent
                _gen_seq += 1
                gen_key = (conv_id, parent_id, _gen_seq)
                data["_gen_id"] = (
                    _gen_seq  # unique ID so client can filter parallel streams
                )
                task = asyncio.create_task(_handle_generation(websocket, conv_id, data))
                task._origin_ws = websocket
                task._gen_key = gen_key
                _active_generations[gen_key] = task

    except WebSocketDisconnect:
        print(f"[WS] Client disconnected conv={conv_id}")
        # Remove this websocket but do NOT cancel active generation — let it finish and save
        clients = _active_websockets.get(conv_id)
        if clients:
            clients.discard(websocket)
            if not clients:
                _active_websockets.pop(conv_id, None)
    except Exception as e:
        print(f"[WS] Error conv={conv_id}: {e}")
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({"type": "error", "error": str(e)})


def _model_family(model_id: str) -> str | None:
    """Return a Claude family name for a model id or alias."""
    if not model_id:
        return None
    m = model_id.lower()
    for fam in ("fable", "opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return None


def _detect_model_fallback(requested: str, actual: str) -> str | None:
    """If CC silently downgraded to a different family, return a warning line."""
    rf = _model_family(requested)
    af = _model_family(actual)
    if rf and af and rf != af:
        return f"Claude Code fell back from {rf} to {af} (usage cap on {rf})"
    return None


def _format_rate_limit_note(data: dict) -> str:
    """Build a user-facing note from CC's rate_limit_event payload.

    CC emits rate_limit_event for proactive warnings (e.g. approaching a cap).
    Tries common field shapes; falls back to a raw JSON dump so we learn the
    schema if it changes.
    """
    if not data:
        return ""
    
    # Flatten rate_limit_info keys if present
    info = data.get("rate_limit_info") or {}
    if not isinstance(info, dict):
        info = {}
        
    parts = []
    
    # Check limit type/window
    limit_type = info.get("rateLimitType") or data.get("limit_type") or data.get("window") or data.get("scope")
    if limit_type:
        parts.append(f"limit_type={limit_type}")
        
    # Check status
    status = info.get("status") or data.get("status")
    if status:
        parts.append(f"status={status}")

    # Check reset time/delay
    resets_at = info.get("resetsAt") or data.get("resets_at") or data.get("reset_at") or data.get("reset")
    if resets_at:
        try:
            import time
            resets_str = time.ctime(float(resets_at))
            parts.append(f"resets_at={resets_str}")
        except Exception:
            parts.append(f"resets_at={resets_at}")
            
    retry_after = data.get("retry_after") or data.get("retry_after_seconds") or data.get("retry_delay_ms")
    if retry_after:
        parts.append(f"retry_after={retry_after}")

    # Overage information
    overage_status = info.get("overageStatus")
    if overage_status:
        parts.append(f"overage_status={overage_status}")
    overage_reason = info.get("overageDisabledReason")
    if overage_reason:
        parts.append(f"overage_reason={overage_reason}")

    if not parts:
        parts.append("raw=" + json.dumps(data, default=str)[:300])
        
    return "\n\n[Loom note: rate-limit details from CC — " + "; ".join(parts) + "]"


def _format_synthetic_error_note(syn: dict) -> str:
    """Note for CC-fabricated error messages (model: '<synthetic>').

    CC writes these client-side after a 4xx from the API. The wording often
    mislabels the rate-limit window — e.g. saying "monthly" for an hourly
    trip — so flag that the text above is generated by CC, not the API.
    """
    err = syn.get("error", "") or "unknown"
    status = syn.get("status")
    suffix = (
        " — wording is fabricated by CC and may mislabel the rate-limit window "
        "(e.g. 'monthly' shown for an hourly cap). Check the Anthropic Console "
        "for actual reset times."
        if err == "rate_limit"
        else ""
    )
    return (
        f"\n\n[Loom note: CC-synthesized error — type={err}"
        f"{f', status={status}' if status else ''}{suffix}]"
    )


async def _patch_marker_with_summary(conv_id: int, marker_id: int, new_session: str):
    """Background task: once CC has flushed the post-compact transcript,
    read the summary out of the JSONL and UPDATE the marker row content.
    Emits `compact_summary_ready` so the UI can swap in the narrative text."""
    try:
        summary = await claude_client.read_compact_summary(new_session)
        if not summary:
            return
        _dbh = await db.get_db()
        # Prepend the existing header so we keep the token count line.
        row = await _dbh.execute_fetchall(
            "SELECT content FROM messages WHERE id = ?", (marker_id,)
        )
        header = ""
        if row:
            header = (dict(row[0]).get("content") or "").split("\n", 1)[0]
        new_content = (header + "\n\n---\nPreviously:\n" + summary).strip()
        await _dbh.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (new_content, marker_id),
        )
        await _dbh.commit()
        await _ws_send(
            conv_id,
            {
                "type": "compact_summary_ready",
                "marker_id": marker_id,
                "summary": summary,
            },
        )
    except Exception as e:
        print(f"[Handoff] Failed to patch marker {marker_id} with summary: {e}")


async def _run_compact_handoff(
    conv_id: int,
    conv: dict,
    parent_leaf_id: int,
    target_model: str,
    cc_effort: str,
    project_dir: str,
) -> tuple[int | None, str | None, int | None]:
    """Execute a compact-and-handoff before switching to a non-1M target.

    Runs CC `/compact` on the most recent 1M-capable session, forks a new
    branch rooted at a compact marker under `parent_leaf_id`, and returns
    `(marker_id, new_session_id, new_parent_id)`. `new_parent_id` is the
    msg id the caller should use as `parent_id` for the pending generation —
    either the marker itself, or (when `parent_leaf_id` was a user message we
    re-parented under the marker) the unchanged user message id.
    Returns (None, None, None) on failure.
    """
    # Find the most recent assistant session we can actually /compact.
    # Needs to be Anthropic (gemini has no --fork-session, local doesn't have
    # /compact) and ideally 1M-context since we're over 175k.
    session_id = None
    branch = await db.get_branch_to_root(parent_leaf_id)
    for msg in reversed(branch):
        if msg["role"] != "assistant":
            continue
        if not msg.get("cc_session_id"):
            continue
        prev_model = msg.get("cc_model_used") or ""
        # Only Anthropic sessions can run /compact.
        _base = prev_model.split("[")[0] if "[" in prev_model else prev_model
        if _base not in {"sonnet", "opus", "haiku"}:
            continue
        session_id = msg["cc_session_id"]
        compact_model = prev_model or "sonnet[1m]"
        break

    if not session_id:
        await _ws_send(
            conv_id,
            {"type": "status", "text": "Handoff skipped — no compactable session in this branch"},
        )
        return None, None, None

    await _ws_send(
        conv_id,
        {"type": "status", "text": f"Compacting for handoff to {target_model}..."},
    )

    server_port = int(os.environ.get("LOOM_PORT", 3000))
    try:
        proc, event_stream = await claude_client.run_claude(
            prompt="/compact",
            cwd=project_dir,
            conv_id=conv_id,
            server_port=server_port,
            model=compact_model,
            effort=cc_effort,
            resume_session_id=session_id,
            fork_session=False,
            use_ollama=False,
        )
    except Exception as e:
        await _ws_send(conv_id, {"type": "status", "text": f"Handoff launch failed: {e}"})
        return None, None, None

    # If the caller's parent is a user message (the turn we're about to
    # generate for), we re-parent it UNDER the marker so the replay-history
    # path keeps that user message visible after truncating at the marker.
    parent_row = None
    _dbh = await db.get_db()
    _prows = await _dbh.execute_fetchall(
        "SELECT id, role, parent_id FROM messages WHERE id = ?", (parent_leaf_id,)
    )
    if _prows:
        parent_row = dict(_prows[0])
    reparent_user = bool(parent_row and parent_row.get("role") == "user")
    marker_parent = parent_row.get("parent_id") if reparent_user else parent_leaf_id

    marker_id = None
    new_session = None
    pre_tokens = None
    async for evt in event_stream:
        etype = evt.get("type", "")
        if etype == "compact_boundary":
            pre_tokens = evt.get("pre_tokens")
            token_info = f" — {pre_tokens:,} tokens before" if pre_tokens else ""
            content = f"[CC context compactified (handoff → {target_model}){token_info}]"
            new_session = evt.get("session_id", "") or ""
            if new_session:
                await db.update_conversation_fields(
                    conv_id, claude_session_id=new_session
                )
            marker = await db.add_message(
                conv_id, "system",
                content,
                parent_id=marker_parent,
                is_active=True,
                cc_session_id=new_session or None,
            )
            marker_id = marker["id"]
            if reparent_user:
                # Slot the marker between the user message and its old parent.
                await _dbh.execute(
                    "UPDATE messages SET parent_id = ? WHERE id = ?",
                    (marker_id, parent_leaf_id),
                )
                await _dbh.commit()
                await db.set_active_branch(conv_id, parent_leaf_id)
            else:
                await db.set_active_branch(conv_id, marker_id)
            await _ws_send(
                conv_id,
                {
                    "type": "compact_boundary",
                    "trigger": "handoff",
                    "pre_tokens": pre_tokens,
                    "marker_id": marker_id,
                    "target_model": target_model,
                },
            )
        elif etype == "result":
            rs = evt.get("session_id", "")
            if rs and not new_session:
                new_session = rs
                await db.update_conversation_fields(
                    conv_id, claude_session_id=rs
                )

    if marker_id and new_session:
        asyncio.create_task(_patch_marker_with_summary(conv_id, marker_id, new_session))
        await _ws_send(
            conv_id,
            {"type": "status", "text": f"Handoff complete — continuing on {target_model}"},
        )
    elif not marker_id:
        await _ws_send(
            conv_id,
            {"type": "status", "text": "Handoff did not produce a compact marker — continuing anyway"},
        )
    new_parent_id = parent_leaf_id if reparent_user else marker_id
    return marker_id, new_session, new_parent_id


async def _handle_compact(websocket: WebSocket, conv_id: int, data: dict):
    """Trigger manual CC compaction via /compact on an existing session.

    Finds the most recent CC session ID for the conversation, then launches
    a CC subprocess with `-p "/compact" --resume <session_id>` and streams
    compact_boundary events back to the UI.
    """
    try:
        conv = await db.get_conversation(conv_id)
        if not conv:
            await _ws_send(conv_id, {"type": "error", "error": "Conversation not found"})
            return

        mode = conv.get("mode", "weave") if conv else "weave"
        if mode == "hermes":
            await _ws_send(conv_id, {"type": "error", "error": "/compact is not supported in Hermes mode yet"})
            return
        if mode != "claude":
            await _ws_send(conv_id, {"type": "error", "error": "/compact is only available in Claude mode"})
            return

        # Find the most recent CC session ID
        session_id = conv.get("claude_session_id")
        if not session_id:
            # Fall back to scanning the branch for a session ID
            leaf = await db.get_active_leaf(conv_id)
            if leaf:
                branch = await db.get_branch_to_root(leaf["id"])
                for msg in reversed(branch):
                    if msg["role"] == "assistant" and msg.get("cc_session_id"):
                        session_id = msg["cc_session_id"]
                        break

        if not session_id:
            await _ws_send(conv_id, {"type": "error", "error": "No CC session to compact — send at least one message first"})
            return

        project_dir = conv.get("project_dir") or "."
        cc_model = data.get("cc_model") or conv.get("cc_model") or "sonnet"
        cc_effort = conv.get("cc_effort") or "high"

        # Determine provider (same logic as _handle_claude_generation).
        # Pinned full IDs (e.g. `claude-opus-4-6`) count as Anthropic too —
        # routed through model_context.is_anthropic to keep the shape in sync.
        is_anthropic = model_context.is_anthropic(cc_model)
        is_llama = cc_model.endswith(".gguf")
        use_llama = conv.get("_use_llama", False) or is_llama
        if is_anthropic or is_gemini_model(cc_model) or is_codex_model(cc_model):
            use_llama = False

        await _ws_send(conv_id, {"type": "status", "text": "Compacting context..."})

        # Launch CC with /compact as the prompt, resuming the existing session
        # No --fork-session: we want to compact the session in place
        compact_prompt = data.get("focus") or ""
        prompt = f"/compact {compact_prompt}".strip() if compact_prompt else "/compact"

        server_port = int(os.environ.get("LOOM_PORT", 3000))
        proc, event_stream = await claude_client.run_claude(
            prompt=prompt,
            cwd=project_dir,
            conv_id=conv_id,
            server_port=server_port,
            model=cc_model,
            effort=cc_effort,
            resume_session_id=session_id,
            fork_session=False,  # compact in place, don't fork
            use_llama=use_llama,
        )

        # Get the current leaf — compact marker becomes its child,
        # forking the conversation at this point
        leaf = await db.get_active_leaf(conv_id)
        compact_parent_id = leaf["id"] if leaf else None

        got_compact = False
        async for evt in event_stream:
            etype = evt.get("type", "")

            if etype == "compact_boundary":
                got_compact = True
                pre_tokens = evt.get("pre_tokens")
                token_info = f" — {pre_tokens:,} tokens before" if pre_tokens else ""
                content = f"[CC context compactified (manual){token_info}]"
                compact_session = evt.get("session_id", "") or ""
                if compact_session:
                    await db.update_conversation_fields(
                        conv_id, claude_session_id=compact_session
                    )
                marker = await db.add_message(
                    conv_id, "system",
                    content,
                    parent_id=compact_parent_id,
                    is_active=True,
                    cc_session_id=compact_session or None,
                )
                # Switch active branch to the compact marker
                await db.set_active_branch(conv_id, marker["id"])
                await _ws_send(conv_id, {
                    "type": "compact_boundary",
                    "trigger": "manual",
                    "pre_tokens": pre_tokens,
                    "marker_id": marker["id"],
                })
                if compact_session:
                    asyncio.create_task(
                        _patch_marker_with_summary(conv_id, marker["id"], compact_session)
                    )

            elif etype == "result":
                new_session_id = evt.get("session_id", "")
                if new_session_id:
                    await db.update_conversation_fields(
                        conv_id, claude_session_id=new_session_id
                    )
                cost = evt.get("cost_usd", 0)
                if cost:
                    old_cost = conv.get("total_cost_usd") or 0
                    await db.update_conversation_fields(
                        conv_id, total_cost_usd=old_cost + cost
                    )

            elif etype == "api_retry":
                attempt = evt.get("attempt", 1)
                max_retries = evt.get("max_retries", 5)
                error = evt.get("error", "unknown")
                await _ws_send(conv_id, {
                    "type": "status",
                    "text": f"API retry {attempt}/{max_retries} ({error})...",
                })

        if got_compact:
            await _ws_send(conv_id, {"type": "status", "text": "Context compacted successfully"})
        else:
            await _ws_send(conv_id, {"type": "status", "text": "Compaction completed (no boundary event received)"})
        # Clear the status bar and trigger a message reload to show the new branch
        await asyncio.sleep(1.5)
        await _ws_send(conv_id, {"type": "compact_done"})

    except Exception as e:
        print(f"[Compact] Error: {e}")
        import traceback; traceback.print_exc()
        await _ws_send(conv_id, {"type": "status", "text": f"Compaction failed: {e}"})
        await asyncio.sleep(2)
        await _ws_send(conv_id, {"type": "compact_done"})
        await _ws_send(conv_id, {"type": "error", "error": f"Compaction failed: {e}"})


def is_codex_model(model: str) -> bool:
    if not model:
        return False
    ml = model.lower()
    return (
        ml.startswith("codex")
        or ml.startswith("gpt-5")
        or ml == "gpt-4o"
        or ml.startswith("o3")
        or ml.startswith("o4")
    )


def is_gemini_model(model: str) -> bool:
    if not model:
        return False
    ml = model.lower()
    return ml.startswith("gemini") or model in (
        "Claude Sonnet 4.6 (Thinking)",
        "Claude Opus 4.6 (Thinking)",
        "GPT-OSS 120B (Medium)"
    )


# Providers with a verified NROL operator lockdown port. Extended as each
# port lands — see mcp_servers/nrol_ao/ROADMAP.md "Multi-provider operator
# parity". "claude" covers the whole claude_client launch family (incl.
# local llama, which reuses the same CLI flags and tool stripping).
NROL_OPERATOR_PROVIDERS = {"claude", "codex", "gemini"}


def _nrol_operator_block_reason(cc_model: str) -> str | None:
    """Refuse operator traffic on providers without a lockdown port.

    The nrol-ao MCP commit gate is provider-agnostic, but an unported
    provider runs with its full default toolset (shell, file writes) in the
    operator workspace — the side-channel the lockdown exists to close.
    """
    provider = (
        "gemini" if is_gemini_model(cc_model)
        else "codex" if is_codex_model(cc_model)
        else "claude"
    )
    if provider in NROL_OPERATOR_PROVIDERS:
        return None
    return (
        f"NROL operator mode is not ported to provider '{provider}' yet "
        f"(model {cc_model!r}); pick a supported model instead. See "
        'mcp_servers/nrol_ao/ROADMAP.md "Multi-provider operator parity".'
    )


def models_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    def norm(s: str) -> str:
        s = s.lower()
        if s.endswith(".gguf"):
            s = s[:-5]
        return "".join(c for c in s if c.isalnum())
    na = norm(a)
    nb = norm(b)
    return na == nb or na in nb or nb in na


async def _handle_generation(websocket: WebSocket, conv_id: int, data: dict):
    """Handle a generation request over WebSocket — routes by conversation mode."""
    conv = await db.get_conversation(conv_id)
    if conv and conv.get("local_model") and config.llama_model:
        local_model = conv["local_model"]
        if models_match(local_model, config.llama_model):
            if local_model != config.llama_model:
                await db.update_conversation_fields(conv_id, local_model=config.llama_model)
                conv = dict(conv)
                conv["local_model"] = config.llama_model
    if conv and conv.get("cc_model") and config.llama_model:
        cc_model = conv["cc_model"]
        is_api = any(
            cc_model.startswith(prefix)
            for prefix in ("claude-", "fable", "sonnet", "haiku", "opus", "gemini", "Gemini", "gpt", "GPT")
        )
        if not is_api and models_match(cc_model, config.llama_model):
            if cc_model != config.llama_model:
                await db.update_conversation_fields(conv_id, cc_model=config.llama_model)
                conv = dict(conv)
                conv["cc_model"] = config.llama_model

    mode = conv.get("mode", "weave") if conv else "weave"

    if mode == "claude":
        await _handle_claude_generation(websocket, conv_id, conv, data)
        return

    if mode == "local":
        await _handle_local_generation(websocket, conv_id, conv, data)
        return

    if mode == "hermes":
        await _handle_hermes_generation(websocket, conv_id, conv, data)
        return

    # Backstage convs always go through OODA so state card tools are available.
    # The model is picked from cc-inline-controls in the UI and sent as cc_model;
    # inject it as local_model so the OODA handler uses it for the Ollama call.
    if conv.get("backstage_parent_id") and data.get("cc_model"):
        conv = dict(conv)
        conv["local_model"] = data["cc_model"]
        # Also persist so the dropdown re-selects on next load
        await db.update_conversation_fields(conv_id, local_model=data["cc_model"])

    if conv.get("ooda_enabled"):
        await _handle_ooda_generation(websocket, conv_id, conv, data)
    else:
        await _handle_weave_generation(websocket, conv_id, conv, data)


async def _handle_claude_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Claude Code CLI generation with session resume support."""
    print(f"[CC-GEN] Entered: conv_id={conv_id}, action={data.get('action')}, parent_id={data.get('parent_id')}, cc_model={data.get('cc_model')}, _use_llama={conv.get('_use_llama')}")
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        _describe_msg_id = None
        draft_msg_id = None

        if action == "generate" and parent_id is None:
            leaf = await db.get_active_leaf(conv_id)
            parent_id = leaf["id"] if leaf else None

        project_dir = conv.get("project_dir") or "."
        canvas_enabled = bool(conv.get("canvas_enabled"))
        if project_dir != "." and not os.path.isdir(project_dir):
            await _ws_send(
                conv_id,
                {
                    "type": "error",
                    "error": f"Working directory not found: {project_dir}",
                },
            )
            return
        # For local mode, _handle_local_generation already set cc_model and
        # _use_llama on the conv dict — don't let the frontend override it.
        # For claude mode, prefer the client's current UI state over stale DB.
        if conv.get("_use_llama"):
            cc_model = conv.get("cc_model") or "sonnet"
        else:
            cc_model = data.get("cc_model") or conv.get("cc_model") or "sonnet"
        cc_effort = data.get("cc_effort") or conv.get("cc_effort") or "high"
        cc_permission_mode = (
            data.get("cc_permission_mode")
            or conv.get("cc_permission_mode")
            or "default"
        )
        # Persist client-provided settings back to DB for reload continuity
        if data.get("cc_model") and data["cc_model"] != conv.get("cc_model"):
            await db.update_conversation_fields(conv_id, cc_model=cc_model)

        # Identify provider based on model name. Accepts aliases (sonnet/opus/
        # haiku), pinned full IDs (claude-opus-4-6), and `[1m]` suffix variants.
        is_anthropic = model_context.is_anthropic(cc_model)
        is_gemini = is_gemini_model(cc_model)
        is_codex = is_codex_model(cc_model)
        # .gguf models go through Claude Code with ANTHROPIC_BASE_URL pointed
        # at the local llama-server (which speaks /v1/messages natively on :11434).
        is_llama = cc_model.endswith(".gguf")
        # Only use llama when explicitly flagged (local mode) or when model is a .gguf.
        # The _use_llama flag is set by _handle_local_generation on a shallow copy.
        use_llama = conv.get("_use_llama", False) or is_llama
        # Belt-and-suspenders: NEVER route Anthropic/Gemini/Codex through llama
        if is_anthropic or is_gemini or is_codex:
            use_llama = False
        print(f"[CC] Model routing: cc_model={cc_model!r} is_anthropic={is_anthropic} is_gemini={is_gemini} is_codex={is_codex} is_llama={is_llama} use_llama={use_llama} conv._use_llama={conv.get('_use_llama')} conv.mode={conv.get('mode')}")

        target_mode = "gemini" if is_gemini else ("codex" if is_codex else ("local" if use_llama else "claude"))

        # Operator conversations only launch on providers with a ported
        # lockdown — the model picker can otherwise route a locked-down conv
        # to a client that knows nothing about nrol_operator.
        if conv.get("nrol_operator"):
            blocked = _nrol_operator_block_reason(cc_model)
            if blocked:
                await _ws_send(conv_id, {"type": "error", "error": blocked})
                return

        if conv.get("mode") != target_mode:
            print(f"[CC] Updating conversation {conv_id} mode from {conv.get('mode')} to {target_mode}")
            await db.update_conversation_fields(conv_id, mode=target_mode)
            conv["mode"] = target_mode

        # --- Compact-handoff gate ---
        # Fast check: if the target model is a 1M Anthropic, skip. Otherwise
        # sum the branch's token estimate and compare against a per-provider
        # threshold. If we trip, run /compact on the latest 1M session and fork
        # a new branch under a compact marker — leaving the old branch intact.
        handoff_forced_session = None
        if (
            action == "generate"
            and parent_id
            and not model_context.is_1m_anthropic(cc_model)
        ):
            branch_tokens = await db.sum_branch_tokens(parent_id)
            if model_context.needs_handoff(cc_model, branch_tokens):
                threshold = model_context.handoff_threshold(cc_model)
                await _ws_send(
                    conv_id,
                    {
                        "type": "status",
                        "text": f"Branch is {branch_tokens:,} tokens (> {threshold:,} for {cc_model}) — preparing handoff...",
                    },
                )
                marker_id, new_session, new_parent = await _run_compact_handoff(
                    conv_id, conv, parent_id, cc_model, cc_effort, project_dir
                )
                if marker_id:
                    parent_id = new_parent or marker_id
                    handoff_forced_session = new_session

        # --- Session resume logic ---
        # Every turn uses --resume + --fork-session. Each assistant message
        # gets its own immutable session snapshot. This means branches, edits,
        # regenerates, and linear continuations all use the same operation.
        # If no ancestor session exists (first message), fall through to
        # full history rebuild.
        resume_session_id = None
        fork_session = True  # always fork — every turn creates a new snapshot
        use_resume = False
        branch = []
        crossed_provider = False

        if parent_id:
            branch = await db.get_branch_to_root(parent_id)

        if handoff_forced_session and is_anthropic:
            # Same-provider (Anthropic) handoff — resume the fresh compacted
            # session rather than walking back to a pre-compact session that
            # the target window can't hold.
            resume_session_id = handoff_forced_session
            use_resume = True
            print(f"[CC] Handoff resume: id={resume_session_id} model={cc_model}")
        elif handoff_forced_session:
            # Cross-provider handoff (Gemini / local) — fall through to history
            # replay, which will truncate at the compact marker.
            print(f"[CC] Handoff replay for {cc_model}")
        elif parent_id and branch:
            # Find nearest ancestor assistant with a session ID AND real content
            # Skip empty drafts, error messages, and broken sessions
            for msg in reversed(branch):
                if msg["role"] != "assistant":
                    continue
                if not msg.get("cc_session_id"):
                    continue
                content = msg.get("content") or ""
                blocks_raw = msg.get("content_blocks") or ""
                has_blocks = False
                if blocks_raw:
                    try:
                        _b = json.loads(blocks_raw) if isinstance(blocks_raw, str) else blocks_raw
                        has_blocks = bool(_b)
                    except (json.JSONDecodeError, TypeError):
                        has_blocks = False
                if content.startswith("[Error:"):
                    print(f"[CC] Skipping error session on msg {msg['id']}")
                    continue
                if not content.strip() and not has_blocks:
                    print(f"[CC] Skipping empty session on msg {msg['id']}")
                    continue
                # Check if the session was created by a different provider.
                # cc_model_used can hold either a shortcode alias ("sonnet[1m]")
                # or a full Anthropic model id ("claude-sonnet-4-7[1m]") — the
                # helper accepts both.
                prev_model = msg.get("cc_model_used") or ""
                prev_is_anthropic = model_context.is_anthropic(prev_model)
                prev_is_gemini = is_gemini_model(prev_model)
                prev_is_codex = is_codex_model(prev_model)
                prev_is_llama = bool(prev_model) and not (
                    prev_is_anthropic or prev_is_gemini or prev_is_codex
                )
                if (
                    (prev_is_anthropic != is_anthropic)
                    or (prev_is_gemini != is_gemini)
                    or (prev_is_codex != is_codex)
                    or (prev_is_llama != use_llama)
                ):
                    print(
                        f"[CC] Cross-provider turn at msg {msg['id']} ({prev_model}), will rebuild full history"
                    )
                    crossed_provider = True
                    break
                resume_session_id = msg["cc_session_id"]
                break

        if resume_session_id:
            use_resume = True
            print(f"[CC] Session resume: id={resume_session_id} fork={fork_session}")

            # When resuming, we only need the latest user message — CC has the rest
            # Find the last user message in the branch
            latest_user_content = ""
            if branch:
                for msg in reversed(branch):
                    if msg["role"] == "user":
                        latest_user_content = msg["content"]
                        break

            prompt = latest_user_content or "(continue)"
        else:
            # No session resume — build full history from branch
            print(f"[CC] No session resume, building full history from branch, parent_id={parent_id}, is_gemini={is_gemini}")
            if parent_id:
                branch = await db.get_branch_to_root(parent_id)
                print(f"[CC] Retrieved branch with {len(branch)} messages")
                prompt = _build_claude_history_prompt(branch, project_dir)
                if not prompt:
                    if is_gemini or is_codex:
                        print(f"[CC] WARNING: Empty prompt from branch for Gemini/Codex!")
                        prompt = "(continue)"
                    else:
                        await _ws_send(
                            conv_id, {"type": "error", "error": "No message to send to Claude"}
                        )
                        return
            else:
                print(f"[CC] No parent_id, no branch to retrieve")
                prompt = "(continue)"

        # Ensure canvas CLAUDE.md, CANVAS_GUIDE.md, and .gitignore exist if canvas is enabled
        if canvas_enabled and project_dir != ".":
            canvas_dir = Path(project_dir) / "canvas"
            canvas_dir.mkdir(parents=True, exist_ok=True)
            gitignore = canvas_dir / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(
                    "# Canvas output is generated per-conversation — don't commit it\n"
                    "*\n"
                    "!.gitignore\n",
                    encoding="utf-8",
                )
            canvas_claude_md = canvas_dir / "CLAUDE.md"
            if not canvas_claude_md.exists():
                canvas_claude_md.write_text(
                    CANVAS_CLAUDE_MD,
                    encoding="utf-8",
                )
            canvas_guide = canvas_dir / "CANVAS_GUIDE.md"
            guide_source = Path(__file__).parent / "CANVAS_GUIDE.md"
            if not canvas_guide.exists() and guide_source.is_file():
                canvas_guide.write_text(
                    guide_source.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )

        # Retrieve describe context from DB (vision model only, never sent to chat model)
        _describe_context = None
        if branch:
            for msg in reversed(branch):
                if msg["role"] == "user":
                    _describe_context = msg.get("describe_context")
                    break

        # Attach images if present on the latest user message
        # (runs for both session-resume and fresh-session paths)
        if branch:
            last_user_msg = None
            for msg in reversed(branch):
                if msg["role"] == "user":
                    last_user_msg = msg
                    break
            if last_user_msg and last_user_msg.get("image_path"):
                img_paths = _parse_image_paths(last_user_msg["image_path"])
                import shutil

                file_notes = []
                # Track which files are images for describe
                image_files: list[tuple[str, Path]] = []  # (original_name, attached_path)

                # Step 1: Copy all files to attached_files/
                for ip in img_paths:
                    src = Path(ip).resolve()
                    file_ext = src.suffix.lower()
                    attached_files_dir = Path(project_dir) / "attached_files"
                    attached_files_dir.mkdir(exist_ok=True)
                    dest = attached_files_dir / src.name
                    copied = False
                    try:
                        shutil.copy2(str(src), str(dest))
                        copied = True
                    except Exception as e:
                        print(
                            f"[UPLOAD] Failed to copy file {src.name} to attached_files/: {e}"
                        )
                    if file_ext in _IMAGE_EXTS and copied:
                        image_files.append((src.name, dest))
                    if use_llama:
                        if file_ext not in _IMAGE_EXTS:
                            file_notes.append(f"{src.name} (in attached_files/)")
                    else:
                        if copied:
                            file_notes.append(f"{src.name} (in attached_files/)")
                        else:
                            file_notes.append(str(src).replace("\\", "/"))

                   # Step 2: Describe images (local mode only) — runs ONCE for all images
                desc_map: dict[str, str] = {}
                print(f"[DESCRIBE] check — use_llama={use_llama}, image_files={len(image_files)}, cc_model={cc_model}")
                if not use_llama or not image_files:
                    print(f"[DESCRIBE] Skipping: use_llama={use_llama}, image_files={len(image_files)}")
                if use_llama and image_files:
                    print(f"[DESCRIBE] Running describe for {len(image_files)} image(s), model={config.llama_model}")
                    _describe_model = config.vision_model or config.llama_model
                    await _ws_send(conv_id, {
                        "type": "describe_start",
                        "parent_msg_id": last_user_msg["id"],
                        "image_count": len(image_files),
                        "model": _describe_model,
                        "started_at": _time.time()
                    })
                    _describe_start = _time.time()
                    for orig_name, attached_path in image_files:
                        try:
                            await _ws_send(conv_id, {"type": "status", "text": f"Describing image {orig_name}..."})
                            desc = await asyncio.wait_for(describe_image(str(attached_path), model=config.vision_model or None, context=_describe_context), timeout=120)
                            file_notes.append(f"[Image: {desc}]")
                            desc_map[orig_name] = desc
                        except asyncio.TimeoutError:
                            print(f"[DESCRIBE] Timed out describing {orig_name} (120s)")
                        except Exception as e:
                            print(f"[DESCRIBE] Failed to describe image {orig_name}: {e}")
                    _describe_elapsed = (_time.time() - _describe_start) * 1000
                    await _ws_send(conv_id, {
                        "type": "describe_done",
                        "parent_msg_id": last_user_msg["id"],
                        "descriptions": desc_map,
                        "model": _describe_model,
                        "elapsed_ms": _describe_elapsed
                    })

                # Step 3: Persist descriptions and create describe message
                if desc_map:
                    try:
                        await db.update_message_image_alt(
                            last_user_msg["id"], json.dumps(desc_map)
                        )
                        await _ws_send(conv_id, {
                            "type": "image_describe",
                            "message_id": last_user_msg["id"],
                            "descriptions": desc_map,
                            "model": _describe_model,
                        })
                    except Exception as e:
                        print(f"[DESCRIBE] Failed to persist/emit descriptions: {e}")

                    _describe_content = f"[Image description — {_describe_model}]\n\n" + "\n\n".join(desc_map.values())
                    _describe_msg = await db.add_message(
                        conv_id, "system", _describe_content,
                        parent_id=last_user_msg["id"]
                    )
                    _describe_msg_id = _describe_msg["id"]

                if file_notes:
                    files_str = "\n".join(f"  • {note}" for note in file_notes)
                    _has_img = any(note.startswith("[Image") for note in file_notes)
                    _hdr = "[Attached files — image descriptions already provided, do NOT read image files.]" if _has_img else f"[User attached {len(file_notes)} file(s). See attached_files/ for the files.]"
                    prompt += f"\n\n{_hdr}\n{files_str}"
        # Create draft message in DB immediately so it survives navigation/restarts.
        # If parent already has an empty assistant child (stale draft), reuse it.
        _draft_parent = _describe_msg_id or parent_id
        draft_msg = None
        if _draft_parent:
            existing_children = await db.get_children(_draft_parent)
            for child in existing_children:
                if (
                    child["role"] == "assistant"
                    and not child.get("content", "").strip()
                ):
                    draft_msg = child
                    print(f"[CC] Reusing stale draft msg {child['id']}")
                    break
        if not draft_msg:
            draft_msg = await db.add_message(
                conv_id, "assistant", "", parent_id=_draft_parent
            )
        draft_msg_id = draft_msg["id"]
        await db.update_message_content(draft_msg_id, cc_model_used=cc_model)
        await db.set_active_branch(conv_id, draft_msg_id)

        # Initialize live snapshot for this generation (survives WS disconnects)

        _gen_key_local = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key_local:
            _update_gen_snapshot(
                _gen_key_local,
                full_text="",
                content_blocks=[],
                input_tokens=0,
                output_tokens=0,
                started_at=_time.time(),
                draft_msg_id=draft_msg_id,
                parent_id=parent_id,
                cc_model=cc_model,
                mode="gemini" if is_gemini else ("codex" if is_codex else ("local" if use_llama else "claude")),
            )

        # Let the client know we're launching
        if is_gemini:
            launch_label = f"Launching agy ({cc_model})..."
        elif is_codex:
            launch_label = f"Launching Codex ({cc_model})..."
        elif use_llama:
            launch_label = f"Launching {cc_model} via Llama Server..."
        else:
            launch_label = f"Launching Claude Code ({cc_model})..."
        if use_resume:
            launch_label += " (resuming session)"
        else:
            launch_label += " (building history)"
        await _ws_send(
            conv_id, {"type": "status", "text": launch_label, "parent_id": parent_id}
        )
        await _ws_send(
            conv_id,
            {
                "type": "stream_start",
                "parent_id": parent_id,
                "draft_msg_id": draft_msg_id,
                "cc_model": cc_model,
            },
        )
        # Mirror the FE's _streamStartTime so the persisted generation_ms
        # matches what the user saw as the live timer.
        _gen_start_t = _time.time()

        # Initialize accumulation vars before try so error handlers can reference them
        full_text = ""
        content_blocks = []
        new_session_id = ""
        actual_model = ""

        # Launch CC — with resume if available, with fallback on failure
        try:
            if is_gemini:
                proc, event_stream = await gemini_client.run_gemini(
                    prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=cc_model,
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    resume_session_id=resume_session_id if use_resume else None,
                    fork_session=fork_session,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    nrol_operator=bool(conv.get("nrol_operator")),
                )
            elif is_codex:
                proc, event_stream = await codex_client.run_codex(
                    prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=cc_model,
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    resume_session_id=resume_session_id if use_resume else None,
                    fork_session=fork_session,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    nrol_operator=bool(conv.get("nrol_operator")),
                    permission_request_handler=handle_cc_permission,
                )
            else:
                proc, event_stream = await claude_client.run_claude(
                    prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=cc_model,
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    resume_session_id=resume_session_id if use_resume else None,
                    fork_session=fork_session,
                    use_llama=use_llama,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    nrol_operator=bool(conv.get("nrol_operator")),
                )
        except Exception as e:
            if use_resume:
                # Fallback: retry without --resume (session may be stale/deleted)
                print(f"[CC] Resume failed ({e}), falling back to full history")
                await _ws_send(conv_id, {"type": "status", "text": "Session resume failed — rebuilding from history..."})
                branch = await db.get_branch_to_root(parent_id) if parent_id else []
                prompt = _build_claude_history_prompt(branch, project_dir) or "(continue)"
                if is_gemini:
                    proc, event_stream = await gemini_client.run_gemini(
                        prompt,
                        project_dir,
                        conv_id=conv_id,
                        server_port=config.port,
                        model=cc_model,
                        effort=cc_effort,
                        permission_mode=cc_permission_mode,
                        nrol_operator=bool(conv.get("nrol_operator")),
                    )
                elif is_codex:
                    proc, event_stream = await codex_client.run_codex(
                        prompt,
                        project_dir,
                        conv_id=conv_id,
                        server_port=config.port,
                        model=cc_model,
                        effort=cc_effort,
                        permission_mode=cc_permission_mode,
                        nrol_operator=bool(conv.get("nrol_operator")),
                        permission_request_handler=handle_cc_permission,
                    )
                else:
                    proc, event_stream = await claude_client.run_claude(
                        prompt,
                        project_dir,
                        conv_id=conv_id,
                        server_port=config.port,
                        model=cc_model,
                        effort=cc_effort,
                        permission_mode=cc_permission_mode,
                        use_llama=use_llama,
                        backstage_parent_id=conv.get("backstage_parent_id"),
                        nrol_operator=bool(conv.get("nrol_operator")),
                    )
                use_resume = False
            else:
                raise

        _active_claude_procs[conv_id] = proc

        # Persist the generation so server-restart can reap or (phase 2) rescue.
        # session_id is filled in later when CC emits session_info.
        try:
            await db.register_active_generation(
                draft_msg_id=draft_msg_id,
                conv_id=conv_id,
                pid=proc.pid,
                project_dir=project_dir,
                mode="local" if use_llama else ("gemini" if is_gemini else ("codex" if is_codex else "claude")),
            )
        except Exception as e:
            print(f"[GEN] Failed to register active generation: {e}")

        full_text = ""
        content_blocks = []
        current_block = None
        result_info = {}
        new_session_id = ""
        actual_model = ""  # what CC actually ran (may differ from cc_model if CC fell back)
        total_input_tokens = 0
        total_output_tokens = 0
        got_error = False
        rate_limit_data: dict | None = None
        synthetic_error: dict | None = None

        async for evt in event_stream:
            etype = evt["type"]

            if etype == "session_info":
                new_session_id = evt.get("session_id", "") or new_session_id
                actual_model = evt.get("model", "") or actual_model
                # Patch session_id onto the tracking row for future rescue support
                try:
                    await db.update_active_generation_session(draft_msg_id, new_session_id)
                except Exception:
                    pass

            elif etype == "codex_launch_info":
                if _gen_key_local:
                    _update_gen_snapshot(
                        _gen_key_local,
                        codex_launch_info=evt,
                        cc_model=evt.get("model", cc_model),
                    )
                await _ws_send(
                    conv_id,
                    {
                        "type": "status",
                        "text": (
                            f"Codex launch: approval={evt.get('approval_policy')}, "
                            f"sandbox={evt.get('sandbox')}, cwd={evt.get('cwd')}"
                        ),
                    },
                )

            elif etype == "status":
                await _ws_send(
                    conv_id,
                    {
                        "type": "status",
                        "text": evt.get("text", ""),
                        "parent_id": parent_id,
                    },
                )

            elif etype == "text_delta":
                full_text += evt["text"]
                if current_block and current_block["type"] == "text":
                    current_block["text"] += evt["text"]
                else:
                    current_block = {"type": "text", "text": evt["text"]}
                    content_blocks.append(current_block)
                await _ws_send(
                    conv_id, {"type": "stream_chunk", "content": evt["text"]}
                )

            elif etype == "tool_start":
                current_block = {
                    "type": "tool_use",
                    "name": evt["name"],
                    "tool_id": evt.get("tool_id", ""),
                    "input": "",
                    "result": "",
                }
                content_blocks.append(current_block)
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_start",
                        "name": evt["name"],
                        "tool_id": evt.get("tool_id", ""),
                    },
                )

            elif etype == "tool_input_delta":
                if current_block and current_block["type"] == "tool_use":
                    current_block["input"] += evt["json"]
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_input_chunk",
                        "content": evt["json"],
                        "tool_id": evt.get("tool_id", ""),
                    },
                )

            elif etype == "ask_user_question":
                await _ws_send(
                    conv_id,
                    {
                        "type": "ask_user_question",
                        "questions": evt.get("questions", []),
                        "tool_id": evt.get("tool_id", ""),
                    },
                )

            elif etype == "plan_ready":
                await _ws_send(
                    conv_id,
                    {
                        "type": "plan_ready",
                        "plan": evt.get("plan", ""),
                        "plan_file": evt.get("plan_file", ""),
                        "tool_id": evt.get("tool_id", ""),
                    },
                )
                # Global notification for plan completion
                conv_title = conv.get("title", "Conversation")
                await _ws_broadcast_all(
                    {
                        "type": "plan_landed",
                        "conv_id": conv_id,
                        "conv_title": conv_title,
                        "plan_file": evt.get("plan_file", ""),
                    }
                )

            elif etype == "tool_result":
                result_content = evt.get("content", "")
                tool_id = evt.get("tool_id", "")
                for block in reversed(content_blocks):
                    if block["type"] == "tool_use" and block.get("tool_id") == tool_id:
                        block["result"] = result_content
                        break
                current_block = None
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_result",
                        "content": result_content,
                        "tool_id": tool_id,
                    },
                )
                # Progressive save: update draft with accumulated content_blocks
                await db.update_message_content(
                    draft_msg_id,
                    content=full_text,
                    content_blocks=json.dumps(content_blocks),
                )
                # Notify frontend if a canvas file was written
                if canvas_enabled:
                    matched_block = None
                    for block in reversed(content_blocks):
                        if block["type"] == "tool_use" and block.get("tool_id") == tool_id:
                            matched_block = block
                            break
                    if matched_block and matched_block.get("name") in ("Write", "Edit", "MultiEdit"):
                        tool_input_str = matched_block.get("input", "")
                        if "canvas/" in tool_input_str or "canvas\\" in tool_input_str:
                            await _ws_send(conv_id, {"type": "canvas_updated"})

            elif etype == "thinking_delta":
                if current_block and current_block["type"] == "thinking":
                    current_block["text"] += evt["text"]
                else:
                    current_block = {"type": "thinking", "text": evt["text"]}
                    content_blocks.append(current_block)
                await _ws_send(
                    conv_id, {"type": "thinking_chunk", "content": evt["text"]}
                )

            elif etype == "usage":
                # Each API call within a turn carries the full cached prefix in
                # input_tokens, so summing double-counts across tool-call cycles.
                # Track the LAST value (= final context size at end of turn);
                # output_tokens IS per-call delta so summing is correct there.
                total_input_tokens = evt.get("input_tokens", 0)
                total_output_tokens += evt.get("output_tokens", 0)
                await _ws_send(
                    conv_id,
                    {
                        "type": "usage",
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                )

            elif etype == "compact_boundary":
                # CC compactified its context — fork into a new branch.
                # The compact summary becomes the first message of the new branch,
                # and the draft (assistant response) is reparented under it.
                trigger = evt.get("trigger", "auto")
                pre_tokens = evt.get("pre_tokens")
                token_info = f" — {pre_tokens:,} tokens before" if pre_tokens else ""
                content = f"[CC context compactified ({trigger}){token_info}]"
                # CC forks to a new session at the compact boundary. Capture it
                # now so the post-compact assistant reply doesn't end up orphaned
                # if the result event's session_id is missing or a retry resets state.
                compact_session = evt.get("session_id", "") or ""
                if compact_session:
                    new_session_id = compact_session
                    await db.update_conversation_fields(
                        conv_id, claude_session_id=compact_session
                    )
                marker = await db.add_message(
                    conv_id, "system",
                    content,
                    parent_id=parent_id,
                    is_active=True,
                    cc_session_id=compact_session or None,
                )
                # Reparent the draft under the compact marker so the branch is:
                # ... → parent → [compact marker] → draft(assistant) → ...
                # The DRAFT must remain the active leaf — otherwise the
                # streaming assistant response ends up on a sibling branch and
                # the user has to click "Switch to this branch" to see their
                # own reply.
                if draft_msg_id:
                    _db = await db.get_db()
                    await _db.execute(
                        "UPDATE messages SET parent_id = ? WHERE id = ?",
                        (marker["id"], draft_msg_id),
                    )
                    await _db.commit()
                    await db.set_active_branch(conv_id, draft_msg_id)
                else:
                    await db.set_active_branch(conv_id, marker["id"])
                await _ws_send(
                    conv_id,
                    {
                        "type": "compact_boundary",
                        "trigger": trigger,
                        "pre_tokens": pre_tokens,
                        "marker_id": marker["id"],
                    },
                )
                if compact_session:
                    asyncio.create_task(
                        _patch_marker_with_summary(conv_id, marker["id"], compact_session)
                    )

            elif etype == "api_retry":
                attempt = evt.get("attempt", 1)
                max_retries = evt.get("max_retries", 5)
                delay_ms = evt.get("retry_delay_ms", 0)
                error = evt.get("error", "unknown")
                await _ws_send(
                    conv_id,
                    {
                        "type": "status",
                        "text": f"API retry {attempt}/{max_retries} ({error}) — retrying in {delay_ms // 1000}s...",
                    },
                )

            elif etype == "auto_continue":
                rnd = evt.get("round", 1)
                suffix = f" (round {rnd})" if rnd > 1 else ""
                await _ws_send(
                    conv_id,
                    {"type": "status", "text": f"Continuing long response{suffix}…"},
                )

            elif etype == "cc_raw_event":
                # Forward unknown events to UI for debugging
                raw_data = evt.get("data", {})
                raw_type = evt.get("event_type", "")
                print(
                    f"[CC] Unknown event type={raw_type}: {json.dumps(raw_data, default=str)[:300]}"
                )
                await _ws_send(
                    conv_id,
                    {
                        "type": "cc_debug_event",
                        "event_type": raw_type,
                        "data": raw_data,
                    },
                )

            elif etype == "rate_limit":
                rate_limit_data = evt.get("data") or {}
                print(
                    f"[CC] rate_limit_event: {json.dumps(rate_limit_data, default=str)[:500]}"
                )
                await _ws_send(
                    conv_id,
                    {"type": "rate_limit_info", "data": rate_limit_data},
                )

            elif etype == "cc_synthetic_error":
                synthetic_error = {
                    "error": evt.get("error", ""),
                    "status": evt.get("status"),
                }
                print(f"[CC] synthetic error: {synthetic_error}")
                await _ws_send(
                    conv_id,
                    {"type": "cc_synthetic_error", **synthetic_error},
                )

            elif etype == "result":
                result_info = evt
                got_error = evt.get("is_error", False)
                # Use result text as fallback if no text came from assistant events
                # Don't adopt error text as full_text — it would block the retry fallback
                if not full_text and evt.get("result_text") and not got_error:
                    full_text = evt["result_text"]
                    content_blocks.append({"type": "text", "text": full_text})

            # Keep live snapshot in sync (reconnecting clients read this)
            if _gen_key_local:
                _update_gen_snapshot(
                    _gen_key_local,
                    full_text=full_text,
                    content_blocks=content_blocks,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

        _active_claude_procs.pop(conv_id, None)

        # If --resume failed (is_error), retry with full history fallback.
        # CC may emit the error as assistant text before the result event,
        # so don't gate on `not full_text` — always retry on resume errors.
        if got_error and use_resume:
            error_detail = result_info.get("result_text", "") or result_info.get("error", "")
            print(f"[CC] Resume returned error, retrying with full history: {error_detail[:200]}")
            await _ws_send(conv_id, {"type": "status", "text": f"Session error — retrying with full history..."})
            branch = await db.get_branch_to_root(parent_id) if parent_id else []
            fallback_prompt = _build_claude_history_prompt(branch, project_dir) or "(continue)"
            # Re-attach images
            if branch:
                last_user_msg = None
                for msg in reversed(branch):
                    if msg["role"] == "user":
                        last_user_msg = msg
                        break
                if last_user_msg and last_user_msg.get("image_path"):
                    img_paths = _parse_image_paths(last_user_msg["image_path"])
                    import shutil

                    file_notes = []
                    image_files: list[tuple[str, Path]] = []
                    _describe_msg_id = None

                    # Step 1: Copy files
                    for ip in img_paths:
                        src = Path(ip).resolve()
                        file_ext = src.suffix.lower()
                        attached_files_dir = Path(project_dir) / "attached_files"
                        attached_files_dir.mkdir(exist_ok=True)
                        dest = attached_files_dir / src.name
                        copied = False
                        try:
                            shutil.copy2(str(src), str(dest))
                            copied = True
                        except Exception as e:
                            print(
                                f"[UPLOAD] Failed to copy file {src.name} to attached_files/: {e}"
                            )
                        if file_ext in _IMAGE_EXTS and copied:
                            image_files.append((src.name, dest))
                        if use_llama:
                            if file_ext not in _IMAGE_EXTS:
                                file_notes.append(f"{src.name} (in attached_files/)")
                        else:
                            if copied:
                                file_notes.append(f"{src.name} (in attached_files/)")
                            else:
                                file_notes.append(str(src).replace("\\", "/"))

                    # Step 2: Describe images (runs ONCE)
                    desc_map: dict[str, str] = {}
                    if use_llama and image_files:
                        _describe_model = config.vision_model or config.llama_model
                        await _ws_send(conv_id, {
                            "type": "describe_start",
                            "parent_msg_id": last_user_msg["id"],
                            "image_count": len(image_files),
                            "model": _describe_model,
                            "started_at": _time.time()
                        })
                        _describe_start = _time.time()
                        for orig_name, attached_path in image_files:
                            try:
                                await _ws_send(conv_id, {"type": "status", "text": f"Describing image {orig_name}..."})
                                desc = await asyncio.wait_for(describe_image(str(attached_path), model=config.vision_model or None, context=_describe_context), timeout=120)
                                file_notes.append(f"[Image: {desc}]")
                                desc_map[orig_name] = desc
                            except asyncio.TimeoutError:
                                print(f"[DESCRIBE] Timed out describing {orig_name} (120s)")
                            except Exception as e:
                                print(f"[DESCRIBE] Failed to describe image {orig_name}: {e}")
                        _describe_elapsed = (_time.time() - _describe_start) * 1000
                        await _ws_send(conv_id, {
                            "type": "describe_done",
                            "parent_msg_id": last_user_msg["id"],
                            "descriptions": desc_map,
                            "model": _describe_model,
                            "elapsed_ms": _describe_elapsed
                        })

                    # Step 3: Persist
                    if desc_map:
                        try:
                            await db.update_message_image_alt(
                                last_user_msg["id"], json.dumps(desc_map)
                            )
                            await _ws_send(conv_id, {
                                "type": "image_describe",
                                "message_id": last_user_msg["id"],
                                "descriptions": desc_map,
                                "model": _describe_model,
                            })
                        except Exception as e:
                            print(f"[DESCRIBE] Failed to persist/emit descriptions: {e}")

                        if not _describe_msg_id:
                            _describe_content = f"[Image description — {_describe_model}]\n\n" + "\n\n".join(desc_map.values())
                            _describe_msg = await db.add_message(
                                conv_id, "system", _describe_content,
                                parent_id=last_user_msg["id"]
                            )
                            _describe_msg_id = _describe_msg["id"]
                            if draft_msg_id:
                                _db = await db.get_db()
                                await _db.execute(
                                    "UPDATE messages SET parent_id = ? WHERE id = ?",
                                    (_describe_msg_id, draft_msg_id),
                                )
                                await _db.commit()

                    if file_notes:
                        files_str = "\n".join(f"  • {note}" for note in file_notes)
                        _has_img = any(note.startswith("[Image") for note in file_notes)
                        _hdr = "[Attached files — image descriptions already provided, do NOT read image files.]" if _has_img else f"[User attached {len(file_notes)} file(s). See attached_files/ for the files.]"
                        fallback_prompt += f"\n\n{_hdr}\n{files_str}"

            proc, event_stream = await claude_client.run_claude(
                fallback_prompt,
                project_dir,
                conv_id=conv_id,
                server_port=config.port,
                model=cc_model,
                effort=cc_effort,
                permission_mode=cc_permission_mode,
                use_llama=use_llama,
                backstage_parent_id=conv.get("backstage_parent_id"),
                nrol_operator=bool(conv.get("nrol_operator")),
            )
            _active_claude_procs[conv_id] = proc

            full_text = ""
            content_blocks = []
            current_block = None
            result_info = {}
            new_session_id = ""
            total_input_tokens = 0
            total_output_tokens = 0

            async for evt in event_stream:
                etype = evt["type"]
                if etype == "session_info":
                    new_session_id = evt.get("session_id", "") or new_session_id
                    model_name = evt.get("model", cc_model)
                    await _ws_send(
                        conv_id,
                        {
                            "type": "status",
                            "text": f"Connected — {model_name} is thinking...",
                            "parent_id": parent_id,
                        },
                    )
                elif etype == "codex_launch_info":
                    await _ws_send(
                        conv_id,
                        {
                            "type": "status",
                            "text": (
                                f"Codex launch: approval={evt.get('approval_policy')}, "
                                f"sandbox={evt.get('sandbox')}, cwd={evt.get('cwd')}"
                            ),
                        },
                    )
                elif etype == "status":
                    await _ws_send(
                        conv_id,
                        {
                            "type": "status",
                            "text": evt.get("text", ""),
                            "parent_id": parent_id,
                        },
                    )
                elif etype == "text_delta":
                    full_text += evt["text"]
                    if current_block and current_block["type"] == "text":
                        current_block["text"] += evt["text"]
                    else:
                        current_block = {"type": "text", "text": evt["text"]}
                        content_blocks.append(current_block)
                    await _ws_send(
                        conv_id, {"type": "stream_chunk", "content": evt["text"]}
                    )
                elif etype == "tool_start":
                    current_block = {
                        "type": "tool_use",
                        "name": evt["name"],
                        "tool_id": evt.get("tool_id", ""),
                        "input": "",
                        "result": "",
                    }
                    content_blocks.append(current_block)
                    await _ws_send(
                        conv_id,
                        {
                            "type": "tool_start",
                            "name": evt["name"],
                            "tool_id": evt.get("tool_id", ""),
                        },
                    )
                elif etype == "tool_input_delta":
                    if current_block and current_block["type"] == "tool_use":
                        current_block["input"] += evt["json"]
                    await _ws_send(
                        conv_id,
                        {
                            "type": "tool_input_chunk",
                            "content": evt["json"],
                            "tool_id": evt.get("tool_id", ""),
                        },
                    )
                elif etype == "tool_result":
                    result_content = evt.get("content", "")
                    tool_id = evt.get("tool_id", "")
                    # Find the matching tool_use block to get tool name + input
                    matched_block = None
                    for block in reversed(content_blocks):
                        if (
                            block["type"] == "tool_use"
                            and block.get("tool_id") == tool_id
                        ):
                            block["result"] = result_content
                            matched_block = block
                            break
                    current_block = None

                    # Check if this tool created/referenced an image file
                    image_url = None
                    if matched_block:
                        tool_input_str = matched_block.get("input", "")
                        tool_name = matched_block.get("name", "")
                        # Scan tool input for image paths
                        import re as _re

                        for candidate in _re.findall(
                            r"[\w/\\._-]+\.(?:png|jpg|jpeg|gif|webp)",
                            tool_input_str,
                            _re.IGNORECASE,
                        ):
                            candidate_path = (Path(project_dir) / candidate).resolve()
                            if candidate_path.exists() and candidate_path.is_file():
                                image_url = f"/api/conversations/{conv_id}/file?path={candidate}"
                                break
                        # Also check the result text for image paths
                        if not image_url:
                            for candidate in _re.findall(
                                r"[\w/\\._-]+\.(?:png|jpg|jpeg|gif|webp)",
                                result_content,
                                _re.IGNORECASE,
                            ):
                                candidate_path = (
                                    Path(project_dir) / candidate
                                ).resolve()
                                if candidate_path.exists() and candidate_path.is_file():
                                    image_url = f"/api/conversations/{conv_id}/file?path={candidate}"
                                    break

                    tool_result_msg = {
                        "type": "tool_result",
                        "content": result_content,
                        "tool_id": tool_id,
                    }
                    if evt.get("is_error"):
                        tool_result_msg["is_error"] = True
                    if image_url:
                        tool_result_msg["image_url"] = image_url
                    await _ws_send(conv_id, tool_result_msg)
                    # Progressive save
                    await db.update_message_content(
                        draft_msg_id,
                        content=full_text,
                        content_blocks=json.dumps(content_blocks),
                    )
                elif etype == "thinking_delta":
                    if current_block and current_block["type"] == "thinking":
                        current_block["text"] += evt["text"]
                    else:
                        current_block = {"type": "thinking", "text": evt["text"]}
                        content_blocks.append(current_block)
                    await _ws_send(
                        conv_id, {"type": "thinking_chunk", "content": evt["text"]}
                    )
                elif etype == "usage":
                    # See the primary CC handler above — track LAST input,
                    # SUM output. Each API call within a turn carries the full
                    # cached prefix so summing input double-counts.
                    total_input_tokens = evt.get("input_tokens", 0)
                    total_output_tokens += evt.get("output_tokens", 0)
                    await _ws_send(
                        conv_id,
                        {
                            "type": "usage",
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                        },
                    )
                elif etype == "rate_limit":
                    rate_limit_data = evt.get("data") or {}
                    print(
                        f"[CC] rate_limit_event (retry): {json.dumps(rate_limit_data, default=str)[:500]}"
                    )
                    await _ws_send(
                        conv_id,
                        {"type": "rate_limit_info", "data": rate_limit_data},
                    )
                elif etype == "cc_synthetic_error":
                    synthetic_error = {
                        "error": evt.get("error", ""),
                        "status": evt.get("status"),
                    }
                    print(f"[CC] synthetic error (retry): {synthetic_error}")
                    await _ws_send(
                        conv_id,
                        {"type": "cc_synthetic_error", **synthetic_error},
                    )
                elif etype == "result":
                    result_info = evt
                    if not full_text and evt.get("result_text"):
                        full_text = evt["result_text"]
                        content_blocks.append({"type": "text", "text": full_text})

                # Keep live snapshot in sync (fallback retry loop)
                if _gen_key_local:
                    _update_gen_snapshot(
                        _gen_key_local,
                        full_text=full_text,
                        content_blocks=content_blocks,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                    )

            _active_claude_procs.pop(conv_id, None)

        # If CC produced no output at all, mark draft as error (don't delete)
        if not full_text and not any(b["type"] == "tool_use" for b in content_blocks):
            provider_name = (
                "Antigravity (agy)"
                if is_gemini
                else ("ChatGPT Codex" if is_codex else ("Llama Server" if use_llama else "Claude Code"))
            )
            error_msg = result_info.get("error")
            if not error_msg:
                if rate_limit_data:
                    info = rate_limit_data.get("rate_limit_info", {})
                    limit_type = info.get("rateLimitType") or rate_limit_data.get("rateLimitType") or "rate cap"
                    resets_at = info.get("resetsAt") or rate_limit_data.get("resetsAt")
                    if resets_at:
                        try:
                            import time
                            reset_time_str = time.ctime(float(resets_at))
                            error_msg = f"{provider_name} rate limit reached ({limit_type} limit) — resets at {reset_time_str}"
                        except Exception:
                            error_msg = f"{provider_name} rate limit reached ({limit_type} limit)"
                    else:
                        error_msg = f"{provider_name} rate limit reached ({limit_type} limit)"
                else:
                    error_msg = f"{provider_name} exited with no response"

            if use_llama and not rate_limit_data:
                error_msg += f" — check that llama-server is running with '{cc_model}'"
            content = f"[Error: {error_msg}]"
            if rate_limit_data:
                content += _format_rate_limit_note(rate_limit_data)
            await db.update_message_content(draft_msg_id, content=content)
            await _ws_send(conv_id, {"type": "error", "error": error_msg})
            await _ws_send(
                conv_id,
                {"type": "stream_end", "message": await db.get_message(draft_msg_id)},
            )
            return

        # CC streamed an error response (e.g. "You've hit your org's monthly
        # usage limit") as assistant text. Augment the saved draft so the user
        # knows the wording came from CC, not the API, and may be wrong about
        # the window. Prefer synthetic_error metadata (the 429 path); fall back
        # to rate_limit_event data (the proactive-warning path).
        if result_info.get("is_error") and full_text and "[Loom note:" not in full_text:
            note = ""
            if synthetic_error:
                note = _format_synthetic_error_note(synthetic_error)
            elif rate_limit_data:
                note = _format_rate_limit_note(rate_limit_data)
            if note:
                full_text = full_text.rstrip() + note
                if content_blocks and content_blocks[-1].get("type") == "text":
                    content_blocks[-1]["text"] = (
                        content_blocks[-1]["text"].rstrip() + note
                    )
                await db.update_message_content(
                    draft_msg_id,
                    content=full_text,
                    content_blocks=json.dumps(content_blocks),
                )

        # Extract cost info
        cost_usd = result_info.get("cost_usd", 0)
        output_tokens = total_output_tokens
        # Final context size at end of turn — see the streaming usage handler
        # above for why we don't sum input_tokens across API calls.
        input_tokens = total_input_tokens
        new_session_id = result_info.get("session_id", "") or new_session_id
        duration_ms = result_info.get("duration_ms", 0)

        # Record what CC actually ran (from session_info), not just what we asked
        # for. If CC silently downgraded (usage cap, availability), surface it.
        resolved_model = actual_model or cc_model
        if use_llama:
            model_used_field = f"{cc_model}@llama-server"
        else:
            model_used_field = resolved_model

        fallback_note = _detect_model_fallback(cc_model, actual_model)
        if fallback_note:
            await _ws_send(
                conv_id,
                {
                    "type": "warning",
                    "text": f"{fallback_note} — this branch is now on {actual_model}. Further turns may auto-compact sooner.",
                    "requested_model": cc_model,
                    "actual_model": actual_model,
                },
            )

        # Finalize the draft message with full content
        _gen_ms = int((_time.time() - _gen_start_t) * 1000)
        await db.update_message_content(
            draft_msg_id,
            content=full_text,
            content_blocks=json.dumps(content_blocks),
            turn_cost_usd=cost_usd,
            turn_input_tokens=input_tokens,
            turn_output_tokens=output_tokens,
            cc_session_id=new_session_id or None,
            cc_model_used=model_used_field,
            generation_ms=_gen_ms,
        )
        msg = await db.get_message(draft_msg_id)

        # Update conversation with session_id and cumulative cost
        old_cost = conv.get("total_cost_usd") or 0
        await db.update_conversation_fields(
            conv_id,
            claude_session_id=new_session_id,
            total_cost_usd=old_cost + cost_usd,
        )

        cost_info = {
            "cost_usd": cost_usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
        }

        # Detect image paths in the response text and tool blocks
        import re as _re

        detected_images = []
        seen_paths = set()
        all_text = (
            full_text
            + " "
            + " ".join(
                b.get("input", "") + " " + b.get("result", "")
                for b in content_blocks
                if b.get("type") == "tool_use"
            )
        )
        base_path = Path(project_dir).resolve()
        for candidate in _re.findall(
            r"[\w/\\._-]+\.(?:png|jpg|jpeg|gif|webp)", all_text, _re.IGNORECASE
        ):
            candidate_path = (base_path / candidate).resolve()
            if (
                str(candidate_path).startswith(str(base_path))
                and candidate_path.exists()
                and candidate_path.is_file()
            ):
                if candidate_path not in seen_paths:
                    seen_paths.add(candidate_path)
                    detected_images.append(
                        f"/api/conversations/{conv_id}/file?path={candidate}"
                    )

        end_msg = {
            "type": "stream_end",
            "message": dict(msg),
            "cost": cost_info,
        }
        if detected_images:
            end_msg["images"] = detected_images
        await _ws_send(conv_id, end_msg)
        # Notify all connected clients (cross-conversation bell)
        conv_title = conv.get("title", "Conversation")
        preview = (full_text or "").replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all(
            {
                "type": "branch_landed",
                "conv_id": conv_id,
                "conv_title": conv_title,
                "message_id": draft_msg_id,
                "preview": preview,
            }
        )

    except asyncio.CancelledError:
        proc = _active_claude_procs.pop(conv_id, None)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        if draft_msg_id and (full_text or content_blocks):
            # Save accumulated work to draft before cancelling.
            # Persist cc_session_id so the next turn's --resume walk finds this
            # draft instead of skipping back past the cancelled prompt (which
            # would cause amnesia + force a full-history rebuild).
            _gen_ms_cancel = int((_time.time() - _gen_start_t) * 1000)
            await db.update_message_content(
                draft_msg_id,
                content=full_text,
                content_blocks=json.dumps(content_blocks) if content_blocks else None,
                cc_session_id=new_session_id or None,
                cc_model_used=(actual_model or cc_model) if (actual_model or cc_model) else None,
                generation_ms=_gen_ms_cancel,
            )
            print(f"[GEN] Saved partial draft {draft_msg_id} on cancel (session={new_session_id or 'none'})")
        elif draft_msg_id:
            # No content produced — delete empty draft to avoid phantoms
            await db.delete_branch(draft_msg_id)
        await _ws_send(conv_id, {"type": "cancelled"})
    except Exception as e:
        _active_claude_procs.pop(conv_id, None)
        print(f"[GEN] Claude generation error conv={conv_id}: {e}")
        import traceback

        traceback.print_exc()
        # Save accumulated work to draft so it's not lost
        if draft_msg_id and (full_text or content_blocks):
            await db.update_message_content(
                draft_msg_id,
                content=full_text or "[Generation interrupted]",
                content_blocks=json.dumps(content_blocks) if content_blocks else None,
            )
            print(f"[GEN] Saved partial draft {draft_msg_id} on error")
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _gen_key = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key:
            _active_generations.pop(_gen_key, None)
            _generation_snapshots.pop(_gen_key, None)
            _auto_approve_permissions.pop((conv_id, f"gen:{_gen_key[2]}"), None)
        # Drop the orphan-tracking row — the generation finished (success,
        # cancel, or error), so there's nothing to reap on next startup.
        try:
            if 'draft_msg_id' in locals() and draft_msg_id:
                await db.unregister_active_generation(draft_msg_id)
        except Exception:
            pass
        # Clean up any pending hook permissions for this conversation (memory + DB)
        for rid in list(_pending_hook_permissions):
            if _pending_hook_permissions[rid].get("conv_id") == conv_id:
                _pending_hook_permissions.pop(rid, None)
                await db.delete_pending_permission(rid)


def _build_claude_history_prompt(branch: list[dict], project_dir: Path = None) -> str:
    """Build a text prompt from conversation history (fallback when --resume unavailable).

    Also scans for attached files and includes references to them.

    If the branch contains one or more `[CC context compactified ...]` system
    markers, the history is truncated at the LAST such marker and the marker's
    body (which should contain the narrative summary) is injected as a
    preamble. This keeps replay prompts compact after a handoff.
    """
    history_parts = []
    # Collect all attached files from the conversation
    attached_files = []

    # Respect compact markers: treat the latest one as a "start here" point.
    compact_preamble = ""
    last_compact_idx = None
    for i, msg in enumerate(branch):
        if msg.get("role") != "system":
            continue
        content = (msg.get("content") or "").lstrip()
        if content.startswith("[CC context compactified"):
            last_compact_idx = i
    if last_compact_idx is not None:
        compact_preamble = (branch[last_compact_idx].get("content") or "").strip()
        branch = branch[last_compact_idx + 1 :]
    if compact_preamble:
        history_parts.append(compact_preamble)

    for msg in branch:
        if msg["role"] == "system":
            continue
        if msg["role"] == "user":
            # Check for attached files in image_path
            if msg.get("image_path"):
                img_paths = _parse_image_paths(msg["image_path"])
                for ip in img_paths:
                    if project_dir and Path(ip).is_file():
                        attached_files.append(ip)
            history_parts.append(f"Human: {msg['content']}")
        elif msg["role"] == "assistant":
            blocks = None
            if msg.get("content_blocks"):
                try:
                    blocks = (
                        json.loads(msg["content_blocks"])
                        if isinstance(msg["content_blocks"], str)
                        else msg["content_blocks"]
                    )
                except (json.JSONDecodeError, TypeError):
                    blocks = None
            if blocks:
                parts = []
                for block in blocks:
                    if block.get("type") == "text" and block.get("text"):
                        parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_summary = f"[Used tool: {block.get('name', 'unknown')}]"
                        if block.get("input"):
                            inp = (
                                block["input"][:2000]
                                if len(block.get("input", "")) > 2000
                                else block.get("input", "")
                            )
                            tool_summary += f"\nInput: {inp}"
                        if block.get("result"):
                            res = (
                                block["result"][:2000]
                                if len(block.get("result", "")) > 2000
                                else block.get("result", "")
                            )
                            tool_summary += f"\nResult: {res}"
                        parts.append(tool_summary)
                history_parts.append(f"Assistant: {chr(10).join(parts)}")
            else:
                history_parts.append(f"Assistant: {msg['content']}")

    if not history_parts:
        base = ""
    elif len(history_parts) > 1:
        history = "\n\n".join(history_parts[:-1])
        latest = history_parts[-1].removeprefix("Human: ")
        base = f"<conversation_history>\n{history}\n</conversation_history>\n\n{latest}"
    else:
        base = history_parts[0].removeprefix("Human: ")

    if attached_files:
        files_str = "\n".join(f"  • {Path(ip).name} (in attached_files/)" for ip in attached_files)
        return f"[Attached files:]\n{files_str}\n\n{base}" if base else f"[Attached files:]\n{files_str}"
    return base


async def _handle_local_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Local mode: Claude Code launched against llama-server."""
    print(f"[LOCAL-GEN] Starting: conv_id={conv_id}, local_model={conv.get('local_model')}, action={data.get('action')}, parent_id={data.get('parent_id')}")
    # Local mode = Claude Code powered by a local llama-server model.
    # Reuse the full CC handler but with use_llama=True.
    conv = dict(conv)  # mutable copy
    # Map local_model into cc_model so _handle_claude_generation uses it
    conv["cc_model"] = conv.get("local_model") or config.llama_model
    conv["_use_llama"] = True
    await _handle_claude_generation(websocket, conv_id, conv, data)


async def _handle_hermes_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Hermes mode — drive a per-turn `hermes acp` subprocess.

    Sibling of _handle_claude_generation (NOT templated off it — none of CC's
    compact-handoff / cross-provider-resume / cc_model plumbing applies). v1 is
    history-replay: every turn opens a fresh ACP session and the full branch is
    rendered into the prompt, so Loom's fork-every-turn invariant holds trivially.

    Not wired into _handle_generation's dispatch yet — that, plus DB-mode
    acceptance and the admin status probe, is Phase 3.
    """
    import time as _time
    draft_msg_id = None
    full_text = ""
    proc = None
    start_t = _time.time()
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        if action == "generate" and parent_id is None:
            leaf = await db.get_active_leaf(conv_id)
            parent_id = leaf["id"] if leaf else None

        project_dir = conv.get("project_dir") or "."
        if project_dir != "." and not os.path.isdir(project_dir):
            await _ws_send(conv_id, {"type": "error",
                                     "error": f"Working directory not found: {project_dir}"})
            return

        # Build the prompt from the branch (history replay — no ACP session resume in v1).
        branch = await db.get_branch_to_root(parent_id) if parent_id else []
        prompt = _build_claude_history_prompt(branch, project_dir) or "(continue)"

        model = conv.get("local_model") or None  # None -> Hermes uses its config.yaml default

        # Draft message so the tree shows a ghost node while streaming.
        draft_msg = await db.add_message(conv_id, "assistant", "", parent_id=parent_id)
        draft_msg_id = draft_msg["id"]
        await _ws_send(conv_id, {"type": "stream_start", "parent_id": parent_id,
                                 "draft_msg_id": draft_msg_id})

        try:
            proc, event_stream = await hermes_client.run_hermes(
                prompt,
                conv_id=conv_id,
                model=model,
                cwd=project_dir,
                loom_port=config.port,
                hermes_exe=config.hermes_executable(),
                hermes_home=config.hermes_home,
            )
        except Exception as e:
            if draft_msg_id:
                await db.delete_branch(draft_msg_id)
            await _ws_send(conv_id, {"type": "error", "error": f"Failed to start Hermes: {e}"})
            return

        _active_hermes_procs[conv_id] = proc
        try:
            await db.register_active_generation(
                draft_msg_id=draft_msg_id, conv_id=conv_id, pid=proc.pid,
                project_dir=project_dir, mode="hermes",
            )
        except Exception as e:
            print(f"[Hermes] Failed to register active generation: {e}")

        content_blocks: list[dict] = []
        current_block = None
        new_session_id = ""
        total_input_tokens = 0
        total_output_tokens = 0
        result_info: dict = {}

        async for evt in event_stream:
            etype = evt.get("type")
            if etype == "session_info":
                new_session_id = evt.get("session_id", "") or new_session_id
                try:
                    await db.update_active_generation_session(draft_msg_id, new_session_id)
                except Exception:
                    pass
                # If the conv has no pinned model, record the one Hermes actually
                # resolved (config.yaml default) so the chat header / model picker
                # show it instead of a bare "(local model)".
                if not conv.get("local_model"):
                    resolved = (evt.get("model") or "").strip()
                    for prefix in ("custom:", "ollama:"):
                        if resolved.startswith(prefix):
                            resolved = resolved[len(prefix):].strip()
                            break
                    
                    # Heuristic: if Hermes returned a mangled suffix (like '27b' for 'qwen3.6:27b'),
                    # re-expand it so we don't save a broken model name that fails next turn.
                    if resolved and ":" in config.ollama_model:
                        base, tag = config.ollama_model.rsplit(":", 1)
                        if resolved == tag or resolved == config.ollama_model.split(":")[-1]:
                            resolved = config.ollama_model

                    if resolved:
                        conv["local_model"] = resolved
                        try:
                            await db.update_conversation_fields(conv_id, local_model=resolved)
                        except Exception:
                            pass
                        await _ws_send(conv_id, {"type": "conv_field_update", "local_model": resolved})
            elif etype == "hermes_commands":
                # Forward Hermes' own slash-command list so the chat input's
                # "/" autocomplete shows them (in place of Loom's meta commands).
                cmds = evt.get("commands") or []
                if cmds:
                    await _ws_send(conv_id, {"type": "hermes_commands", "commands": cmds})
            elif etype == "text_delta":
                full_text += evt["text"]
                if current_block and current_block["type"] == "text":
                    current_block["text"] += evt["text"]
                else:
                    current_block = {"type": "text", "text": evt["text"]}
                    content_blocks.append(current_block)
                await _ws_send(conv_id, {"type": "stream_chunk", "content": evt["text"]})
            elif etype == "thinking_delta":
                if current_block and current_block["type"] == "thinking":
                    current_block["text"] += evt["text"]
                else:
                    current_block = {"type": "thinking", "text": evt["text"]}
                    content_blocks.append(current_block)
                await _ws_send(conv_id, {"type": "thinking_chunk", "content": evt["text"]})
            elif etype == "tool_start":
                current_block = {"type": "tool_use", "name": evt["name"],
                                 "tool_id": evt.get("tool_id", ""), "input": "", "result": ""}
                content_blocks.append(current_block)
                await _ws_send(conv_id, {"type": "tool_start", "name": evt["name"],
                                         "tool_id": evt.get("tool_id", "")})
            elif etype == "tool_input_delta":
                if current_block and current_block["type"] == "tool_use":
                    current_block["input"] += evt["json"]
                await _ws_send(conv_id, {"type": "tool_input_chunk", "content": evt["json"],
                                         "tool_id": evt.get("tool_id", "")})
            elif etype == "tool_result":
                tool_id = evt.get("tool_id", "")
                for block in reversed(content_blocks):
                    if block["type"] == "tool_use" and block.get("tool_id") == tool_id:
                        block["result"] = evt.get("content", "")
                        break
                current_block = None
                await _ws_send(conv_id, {"type": "tool_result", "content": evt.get("content", ""),
                                         "tool_id": tool_id})
                await db.update_message_content(draft_msg_id, content=full_text,
                                                content_blocks=json.dumps(content_blocks))
            elif etype == "permission_request":
                # The browser prompt/notification is driven by /api/cc-permission.
                pass
            elif etype == "usage":
                total_input_tokens = evt.get("input_tokens", 0) or total_input_tokens
                total_output_tokens += evt.get("output_tokens", 0)
                await _ws_send(conv_id, {"type": "usage", "input_tokens": total_input_tokens,
                                         "output_tokens": total_output_tokens})
            elif etype == "hermes_usage_update":
                used = evt.get("used")
                if used is not None:
                    await _ws_send(conv_id, {"type": "context_info", "total_tokens": used})
            elif etype == "plan_update":
                await _ws_send(conv_id, {"type": "status", "text": "Hermes updated its plan."})
            elif etype == "error":
                await _ws_send(conv_id, {"type": "status", "text": f"Hermes: {evt.get('error', '')}"})
            elif etype == "result":
                result_info = evt
                new_session_id = evt.get("session_id", "") or new_session_id
            # hermes_commands / hermes_raw_update: ignored in v1.

        _active_hermes_procs.pop(conv_id, None)

        if not full_text.strip() and not any(b["type"] == "tool_use" for b in content_blocks):
            if draft_msg_id:
                await db.delete_branch(draft_msg_id)
            await _ws_send(conv_id, {"type": "error",
                                     "error": "Hermes returned an empty response — try again"})
            return

        gen_ms = int((_time.time() - start_t) * 1000)
        await db.update_message_content(
            draft_msg_id, content=full_text, content_blocks=json.dumps(content_blocks),
            cc_session_id=new_session_id or None,
            cc_model_used=f"hermes:{model}" if model else "hermes:default",
            generation_ms=gen_ms,
        )
        await db.set_active_branch(conv_id, draft_msg_id)
        msg = await db.get_message(draft_msg_id)
        await _ws_send(conv_id, {"type": "stream_end", "message": dict(msg)})
        preview = (full_text or "").replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all({"type": "branch_landed", "conv_id": conv_id,
                                 "conv_title": conv.get("title", "Conversation"),
                                 "message_id": draft_msg_id, "preview": preview})

    except asyncio.CancelledError:
        if proc is not None:
            try:
                await hermes_client.cancel_hermes(proc)
            except Exception:
                pass
        if draft_msg_id:
            if full_text.strip():
                try:
                    await db.update_message_content(draft_msg_id, content=full_text)
                except Exception:
                    pass
            else:
                await db.delete_branch(draft_msg_id)
        await _ws_send(conv_id, {"type": "cancelled"})
    except Exception as e:
        if draft_msg_id and full_text.strip():
            try:
                await db.update_message_content(draft_msg_id, content=full_text)
            except Exception:
                pass
        elif draft_msg_id:
            await db.delete_branch(draft_msg_id)
        print(f"[Hermes] generation error conv={conv_id}: {e}")
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _active_hermes_procs.pop(conv_id, None)
        if proc is not None and proc.returncode is None:
            try:
                await hermes_client.cancel_hermes(proc)
            except Exception:
                pass


async def _handle_ooda_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle OODA-enhanced Weave generation — two-pass with state card scaffolding."""
    import re as _re

    draft_msg_id = None
    final_prose = ""
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        print(
            f"[OODA] Starting ooda_generation conv={conv_id} action={action} parent={parent_id}"
        )

        if action == "generate" and parent_id is None:
            leaf = await db.get_active_leaf(conv_id)
            parent_id = leaf["id"] if leaf else None

        # ── Setup (same as weave) ──
        character = None
        if conv and conv.get("character_id"):
            char_path = os.path.join(
                config.characters_dir, f"{conv['character_id']}.md"
            )
            character = load_character(char_path)

        style_nudge_name = conv.get("style_nudge", "Natural") if conv else "Natural"
        nudge_index = 0
        for i, nudge in enumerate(STYLE_NUDGES):
            if nudge["name"] == style_nudge_name:
                nudge_index = i
                break

        persona = None
        if conv and conv.get("persona_id"):
            persona = load_persona(os.path.join("personas", f"{conv['persona_id']}.md"))

        lore_entries = []
        if conv and conv.get("lore_ids"):
            try:
                lore_ids = (
                    json.loads(conv["lore_ids"])
                    if isinstance(conv["lore_ids"], str)
                    else conv["lore_ids"]
                )
            except (ValueError, TypeError):
                lore_ids = []
            for lid in lore_ids:
                entry = load_lore_entry(os.path.join("lore", f"{lid}.md"))
                if entry:
                    lore_entries.append(entry)

        context = await get_context_for_generation(conv_id, character, leaf_id=parent_id)
        if action == "regenerate" and parent_id is not None:
            context["verbatim_messages"] = [
                m for m in context["verbatim_messages"] if m["id"] <= parent_id
            ]

        custom_scene = conv.get("custom_scene") if conv else None
        base_system = build_system_prompt(
            character=character,
            style_nudge_index=nudge_index,
            scenario_override=custom_scene,
        )

        # ── OODA enhancement: build system prompt with branch-aware state ──
        if parent_id:
            state_cards = await db.get_branch_state(conv_id, parent_id)
        else:
            state_cards = await db.get_state_cards(conv_id)
        global_cards = (
            await db.get_character_state_cards(conv.get("character_id", ""))
            if conv.get("character_id")
            else []
        )
        ooda_system = build_ooda_system_prompt(
            base_system, state_cards, global_cards=global_cards
        )

        example_msgs = character.get("example_messages", []) if character else []
        messages = assemble_prompt(
            system_prompt=ooda_system,
            example_messages=example_msgs,
            summary=context.get("summary"),
            conversation_messages=context["verbatim_messages"],
            persona=persona,
            lore_entries=lore_entries,
        )

        actual_tokens = sum(len(m["content"]) // 3 for m in messages)
        active_nudge = get_style_nudge(nudge_index)
        context_payload = {
            "type": "context_info",
            "total_tokens": actual_tokens,
            "was_compactified": context["was_compactified"],
            "style_nudge": active_nudge["name"],
            "parent_id": parent_id,
        }
        if context["was_compactified"]:
            context_payload["total_messages"] = context.get("total_messages")
            context_payload["verbatim_count"] = context.get("verbatim_count")
            context_payload["summarized_count"] = context.get("summarized_count")
            context_payload["summary_text"] = context.get("summary")
            # Auto-branch: insert a system marker at the compaction point
            summ_count = context.get("summarized_count", "?")
            verb_count = context.get("verbatim_count", "?")
            marker = await db.add_message(
                conv_id, "system",
                f"[Context compactified — {summ_count} messages summarized, {verb_count} sent verbatim]",
                parent_id=parent_id,
            )
            parent_id = marker["id"]
            context_payload["compaction_marker_id"] = marker["id"]
        await _ws_send(conv_id, context_payload)

        # Create draft message in DB so it appears as ghost node on tree
        draft_msg = await db.add_message(conv_id, "assistant", "", parent_id=parent_id)
        draft_msg_id = draft_msg["id"]

        print(
            f"[OODA] System prompt: {len(ooda_system)} chars, {len(messages)} messages, {len(state_cards)} state cards"
        )
        await _ws_send(
            conv_id,
            {
                "type": "stream_start",
                "parent_id": parent_id,
                "draft_msg_id": draft_msg_id,
            },
        )
        _ooda_start_t = _time.time()

        # ── Pass 1: Orient ──
        print(f"[OODA] Pass 1: Orient...")
        await _ws_send(
            conv_id,
            {
                "type": "status",
                "text": "OODA: Observing and orienting...",
                "parent_id": parent_id,
            },
        )
        weave_model = conv.get("local_model") or None
        raw_pass1 = await sync_chat(
            messages, max_tokens=2048, think=False, model=weave_model
        )
        # Check if cancelled during the sync call
        if asyncio.current_task().cancelled():
            raise asyncio.CancelledError()
        cleaned_pass1 = _re.sub(r"<think>[\s\S]*?</think>", "", raw_pass1)
        cleaned_pass1 = _re.sub(r"<think>[\s\S]*?</think>\s*", "", cleaned_pass1).strip()
        print(f"[OODA] Pass 1 done: {len(cleaned_pass1)} chars")
        print(f"[OODA] Raw OODA output:\n{cleaned_pass1[:1500]}")

        # Parse OODA block
        ooda = parse_ooda_block(cleaned_pass1)

        if ooda:
            # Emit OODA steps as tool blocks for visibility
            if ooda["observe"]:
                tool_id = f"ooda-observe-{conv_id}"
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_start",
                        "name": "OODA: Observe",
                        "tool_id": tool_id,
                        "ooda": True,
                    },
                )
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_result",
                        "content": ooda["observe"],
                        "tool_id": tool_id,
                    },
                )
            if ooda["orient"]:
                tool_id = f"ooda-orient-{conv_id}"
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_start",
                        "name": "OODA: Orient",
                        "tool_id": tool_id,
                        "ooda": True,
                    },
                )
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_result",
                        "content": ooda["orient"],
                        "tool_id": tool_id,
                    },
                )
            if ooda["decide"]:
                tool_id = f"ooda-decide-{conv_id}"
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_start",
                        "name": "OODA: Decide",
                        "tool_id": tool_id,
                        "ooda": True,
                    },
                )
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_result",
                        "content": ooda["decide"],
                        "tool_id": tool_id,
                    },
                )

            resolved = await execute_ooda_reads(conv_id, ooda["reads"])
            print(
                f"[OODA] Resolved {len(resolved)} reads, applying {len(ooda['updates'])} updates, {len(ooda['creates'])} creates"
            )

            # State updates saved as branch deltas only (Tier 3) — base cards stay pristine
            # Notify client of the effective state change for this branch
            if ooda["updates"] or ooda["creates"]:
                await _ws_send(
                    conv_id, {"type": "state_update", "updates": ooda["updates"]}
                )

        # ── Extract prose (single-pass: prose comes after </ooda> tag) ──
        final_prose = ""
        if ooda:
            final_prose = extract_post_ooda_prose(cleaned_pass1)
        if not final_prose:
            # No OODA block or no prose after it — use the whole output
            final_prose = cleaned_pass1
            # Strip closed ooda blocks
            final_prose = _re.sub(r"<ooda>[\s\S]*?</ooda>\s*", "", final_prose).strip()
            # Strip truncated/unclosed ooda blocks (model ran out of tokens)
            final_prose = _re.sub(r"<ooda>[\s\S]*$", "", final_prose).strip()

        # Stream prose to client
        for i in range(0, len(final_prose), 8):
            await _ws_send(
                conv_id, {"type": "stream_chunk", "content": final_prose[i : i + 8]}
            )

        if not final_prose.strip():
            if ooda:
                # OODA analysis succeeded but no prose — save analysis summary as content
                summary_parts = []
                if ooda.get("observe"):
                    summary_parts.append(f"*{ooda['observe'][:200]}*")
                if ooda.get("orient"):
                    summary_parts.append(f"*{ooda['orient'][:200]}*")
                final_prose = (
                    "\n\n".join(summary_parts)
                    if summary_parts
                    else "[OODA analysis completed but no prose generated — try regenerating]"
                )
                for i in range(0, len(final_prose), 8):
                    await _ws_send(
                        conv_id,
                        {"type": "stream_chunk", "content": final_prose[i : i + 8]},
                    )
            else:
                if draft_msg_id:
                    await db.delete_branch(draft_msg_id)
                await _ws_send(
                    conv_id,
                    {
                        "type": "error",
                        "error": "Model returned an empty response — try regenerating",
                    },
                )
                return

        # Update draft with final content
        _ooda_gen_ms = int((_time.time() - _ooda_start_t) * 1000)
        await db.update_message_content(draft_msg_id, content=final_prose, generation_ms=_ooda_gen_ms)
        await db.set_active_branch(conv_id, draft_msg_id)
        from context_manager import update_rolling_summary
        asyncio.create_task(update_rolling_summary(conv_id))
        msg = await db.get_message(draft_msg_id)
        # Save branch-level state deltas (Tier 3)
        if ooda and ooda.get("updates"):
            await db.save_state_deltas(draft_msg_id, ooda["updates"])
        await _ws_send(conv_id, {"type": "stream_end", "message": dict(msg)})
        conv_title = conv.get("title", "Conversation")
        preview = (final_prose or "").replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all(
            {
                "type": "branch_landed",
                "conv_id": conv_id,
                "conv_title": conv_title,
                "message_id": draft_msg_id,
                "preview": preview,
            }
        )

    except asyncio.CancelledError:
        if draft_msg_id:
            await db.delete_branch(draft_msg_id)
        await _ws_send(conv_id, {"type": "cancelled"})
    except Exception as e:
        if draft_msg_id and final_prose and final_prose.strip():
            await db.update_message_content(draft_msg_id, content=final_prose)
        elif draft_msg_id:
            await db.delete_branch(draft_msg_id)
        print(f"[OODA] Generation error conv={conv_id}: {e}")
        import traceback

        traceback.print_exc()
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _gen_key = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key:
            _active_generations.pop(_gen_key, None)
            _generation_snapshots.pop(_gen_key, None)


async def _handle_weave_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Weave (Ollama) generation — original logic."""
    draft_msg_id = None
    full_response = ""
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        print(
            f"[GEN] _handle_weave_generation called: action={action} parent_id={parent_id} conv_id={conv_id}"
        )
        print(f"[GEN] conv={conv}")

        # For regenerate, parent_id should be provided
        # For generate, use the current active leaf
        if action == "generate" and parent_id is None:
            leaf = await db.get_active_leaf(conv_id)
            parent_id = leaf["id"] if leaf else None
        character = None
        if conv and conv.get("character_id"):
            char_path = os.path.join(
                config.characters_dir, f"{conv['character_id']}.md"
            )
            print(
                f"[GEN] Loading character from: {char_path} (exists={os.path.exists(char_path)})"
            )
            character = load_character(char_path)
            print(
                f"[GEN] Character loaded: name={character.get('name') if character else 'NONE'}, personality_len={len(character.get('personality','')) if character else 0}, scenario_len={len(character.get('scenario','')) if character else 0}"
            )
        else:
            print(
                f"[GEN] No character_id on conv! character_id={conv.get('character_id') if conv else 'NO CONV'}"
            )

        # Get style nudge from conversation settings (user-selected, not rotating)
        style_nudge_name = conv.get("style_nudge", "Natural") if conv else "Natural"
        nudge_index = 0
        for i, nudge in enumerate(STYLE_NUDGES):
            if nudge["name"] == style_nudge_name:
                nudge_index = i
                break

        # Load persona if set
        persona = None
        if conv and conv.get("persona_id"):
            persona_path = os.path.join("personas", f"{conv['persona_id']}.md")
            persona = load_persona(persona_path)
            print(
                f"[GEN] Persona: id={conv['persona_id']} path={persona_path} loaded={'yes' if persona else 'NO'}"
            )
        else:
            print(f"[GEN] No persona_id set on conversation")

        # Load lore entries if set
        lore_entries = []
        if conv and conv.get("lore_ids"):
            import json as _json

            try:
                lore_ids = (
                    _json.loads(conv["lore_ids"])
                    if isinstance(conv["lore_ids"], str)
                    else conv["lore_ids"]
                )
            except (ValueError, TypeError):
                lore_ids = []
            for lid in lore_ids:
                entry = load_lore_entry(os.path.join("lore", f"{lid}.md"))
                if entry:
                    lore_entries.append(entry)

        # Get context (with potential compactification)
        # For regenerate, truncate context to parent_id so we don't include
        # the old response we're regenerating
        context = await get_context_for_generation(conv_id, character, leaf_id=parent_id)
        if action == "regenerate" and parent_id is not None:
            context["verbatim_messages"] = [
                m for m in context["verbatim_messages"] if m["id"] <= parent_id
            ]

        # Run repetition analysis on recent assistant messages
        # Build system prompt (use custom scene if set)
        custom_scene = conv.get("custom_scene") if conv else None
        system_prompt = build_system_prompt(
            character=character,
            style_nudge_index=nudge_index,
            scenario_override=custom_scene,
        )

        # Assemble full prompt (persona + lore injected as user turn)
        example_msgs = character.get("example_messages", []) if character else []
        messages = assemble_prompt(
            system_prompt=system_prompt,
            example_messages=example_msgs,
            summary=context.get("summary"),
            conversation_messages=context["verbatim_messages"],
            persona=persona,
            lore_entries=lore_entries,
        )

        # Debug: log assembled prompt
        print(f"[GEN] System prompt length: {len(system_prompt)}")
        print(f"[GEN] System prompt preview: {system_prompt[:300]}...")
        print(f"[GEN] Total messages in prompt: {len(messages)}")
        print(
            f"[GEN] Context verbatim_messages count: {len(context['verbatim_messages'])}"
        )
        for i, m in enumerate(messages):
            print(f"[GEN]   msg[{i}] role={m['role']} len={len(m['content'])}")

        # Send context info — use actual assembled prompt token count
        actual_tokens = sum(len(m["content"]) // 3 for m in messages)
        active_nudge = get_style_nudge(nudge_index)
        context_payload = {
            "type": "context_info",
            "total_tokens": actual_tokens,
            "was_compactified": context["was_compactified"],
            "style_nudge": active_nudge["name"],
            "parent_id": parent_id,
        }
        if context["was_compactified"]:
            context_payload["total_messages"] = context.get("total_messages")
            context_payload["verbatim_count"] = context.get("verbatim_count")
            context_payload["summarized_count"] = context.get("summarized_count")
            context_payload["summary_text"] = context.get("summary")
            # Auto-branch: insert a system marker at the compaction point
            summ_count = context.get("summarized_count", "?")
            verb_count = context.get("verbatim_count", "?")
            marker = await db.add_message(
                conv_id, "system",
                f"[Context compactified — {summ_count} messages summarized, {verb_count} sent verbatim]",
                parent_id=parent_id,
            )
            parent_id = marker["id"]
            context_payload["compaction_marker_id"] = marker["id"]
        await _ws_send(conv_id, context_payload)

        # Create draft message in DB so it appears as ghost node on tree
        draft_msg = await db.add_message(conv_id, "assistant", "", parent_id=parent_id)
        draft_msg_id = draft_msg["id"]

        # Stream the response
        print(
            f"[GEN] Starting generation for conv={conv_id} parent={parent_id} model={config.ollama_model}"
        )
        await _ws_send(
            conv_id,
            {
                "type": "stream_start",
                "parent_id": parent_id,
                "draft_msg_id": draft_msg_id,
            },
        )

        # Initialize live snapshot for Weave generation
        import time as _time
        _weave_start_t = _time.time()

        _gen_key_local = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key_local:
            _update_gen_snapshot(
                _gen_key_local,
                full_text="",
                content_blocks=[],
                started_at=_time.time(),
                draft_msg_id=draft_msg_id,
                parent_id=parent_id,
                mode="weave",
            )

        full_response = ""
        _thinking_buffer = ""
        weave_model = conv.get("local_model") or None
        async for item in stream_chat(messages, model=weave_model):
            if isinstance(item, dict):
                if item.get("type") == "thinking_delta":
                    _thinking_buffer += item["text"]
                    await _ws_send(
                        conv_id, {"type": "thinking_chunk", "content": item["text"]}
                    )
                else:
                    # thinking_start, thinking_end, usage — forward as-is
                    await _ws_send(conv_id, item)
                continue
            full_response += item
            await _ws_send(
                conv_id,
                {
                    "type": "stream_chunk",
                    "content": item,
                },
            )
            # Keep live snapshot in sync
            if _gen_key_local:
                _update_gen_snapshot(_gen_key_local, full_text=full_response)

        # Strip thinking blocks from models that include tags in their content field
        # (qwen3 uses <think>.../», DeepSeek/llama.cpp uses <think>...</think>)
        import re as _re

        cleaned = _re.sub(r"<think>[\s\S]*?</think>\s*", "", full_response)
        cleaned = _re.sub(r"<think>[\s\S]*?</think>\s*", "", cleaned).strip()
        if cleaned:
            full_response = cleaned

        # If response is empty, send error instead of saving empty message
        if not full_response.strip():
            print(
                f"[WARN] Empty response. Raw length={len(full_response)} Cleaned length={len(cleaned)}"
            )
            if draft_msg_id:
                await db.delete_branch(draft_msg_id)
            await _ws_send(
                conv_id,
                {
                    "type": "error",
                    "error": "Model returned an empty response — try again",
                },
            )
            return

        # Update draft with final content
        _weave_gen_ms = int((_time.time() - _weave_start_t) * 1000)
        await db.update_message_content(draft_msg_id, content=full_response, generation_ms=_weave_gen_ms)
        await db.set_active_branch(conv_id, draft_msg_id)
        from context_manager import update_rolling_summary
        asyncio.create_task(update_rolling_summary(conv_id))
        msg = await db.get_message(draft_msg_id)
        await _ws_send(
            conv_id,
            {
                "type": "stream_end",
                "message": dict(msg),
            },
        )
        conv_title = conv.get("title", "Conversation")
        preview = (full_response or "").replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all(
            {
                "type": "branch_landed",
                "conv_id": conv_id,
                "conv_title": conv_title,
                "message_id": draft_msg_id,
                "preview": preview,
            }
        )

    except asyncio.CancelledError:
        # Clean up draft on cancel
        if draft_msg_id:
            await db.delete_branch(draft_msg_id)
        await _ws_send(conv_id, {"type": "cancelled"})
    except Exception as e:
        # Save partial content or clean up empty draft
        if draft_msg_id and full_response.strip():
            _weave_partial_ms = int((_time.time() - _weave_start_t) * 1000)
            await db.update_message_content(draft_msg_id, content=full_response, generation_ms=_weave_partial_ms)
        elif draft_msg_id:
            await db.delete_branch(draft_msg_id)
        print(f"[GEN] Weave generation error conv={conv_id}: {e}")
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _gen_key = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key:
            _active_generations.pop(_gen_key, None)
            _generation_snapshots.pop(_gen_key, None)


if __name__ == "__main__":
    import uvicorn

    # Suppress Windows ProactorEventLoop pipe errors from CC subprocess cleanup.
    # Patch at startup so it applies to whatever event loop uvicorn creates.
    import asyncio.proactor_events as _pe

    _orig_call_connection_lost = _pe._ProactorBasePipeTransport._call_connection_lost

    def _safe_call_connection_lost(self, exc=None):
        try:
            _orig_call_connection_lost(self, exc)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    _pe._ProactorBasePipeTransport._call_connection_lost = _safe_call_connection_lost

    ssl_kwargs = {}
    if os.path.exists(config.ssl_certfile) and os.path.exists(config.ssl_keyfile):
        ssl_kwargs["ssl_certfile"] = config.ssl_certfile
        ssl_kwargs["ssl_keyfile"] = config.ssl_keyfile
        print(f"[SSL] HTTPS enabled — cert={config.ssl_certfile}")
    else:
        print("[SSL] No certs found — running plain HTTP")

    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        **ssl_kwargs,
    )
    server = uvicorn.Server(uv_config)
    _server_ref.append(server)
    server.run()
