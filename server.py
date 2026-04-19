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
import uuid
from pathlib import Path

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    File,
    HTTPException,
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketState

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
from ollama_client import health_check, stream_chat, sync_chat, describe_image
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
import model_context
from skill_scanner import get_all_skills, BUILTIN_COMMANDS

from contextlib import asynccontextmanager

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
    await db.init_db()
    # Clean up stale draft messages (empty assistant msgs older than 30 min)
    await _cleanup_stale_drafts()
    # Re-broadcast pending permission requests from DB (server restart)
    await _reload_pending_permissions()
    # Ensure Ollama is running (launches it if not)
    asyncio.create_task(_ensure_ollama())
    yield
    await db.close_db()


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
        subprocess.Popen(
            [ollama_path, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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


# Ensure upload directory exists
os.makedirs(config.upload_dir, exist_ok=True)

# Active WebSocket generation tasks — keyed by (conv_id, parent_id, seq) for parallel support
# CC mode only allows one per conv; Weave/OODA allow multiple (even on same parent)
_active_generations: dict[tuple[int, int | None, int], asyncio.Task] = {}
_gen_seq = 0  # monotonic counter for unique gen keys
# Active Claude Code subprocesses (for cancellation)
_active_claude_procs: dict[int, asyncio.subprocess.Process] = {}
# Active WebSocket connections per conversation — multiple clients can watch the same conv
_active_websockets: dict[int, set[WebSocket]] = {}
# Pending hook-based permission requests: request_id -> {event, response, conv_id}
_pending_hook_permissions: dict[str, dict] = {}
# Sessions where user clicked "Allow All" — auto-approve for rest of generation
_auto_approve_sessions: set[int] = set()
# Live generation state — survives WS disconnects so reconnecting clients get a snapshot.
# Keyed by gen_key (conv_id, parent_id, seq). Updated on every stream event.
_generation_snapshots: dict[tuple[int, int | None, int], dict] = {}


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

    await db.update_conversation_fields(conv_id, canvas_enabled=1 if enabled else 0)
    return {"ok": True, "enabled": enabled, "project_dir": project_dir}


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


# ── Health ──


@app.get("/api/health")
async def api_health():
    ollama_status = await health_check()
    return ollama_status


@app.get("/api/ollama/models")
async def api_ollama_models():
    status = await health_check()
    return {"models": status.get("models", [])}


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

    first_turn = data.get("first_turn", "character")  # "character" or "user"
    custom_scene = data.get("custom_scene")

    cc_model = data.get("cc_model", "sonnet")
    cc_effort = data.get("cc_effort", "high")
    local_model = data.get("local_model")

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
    if mode == "local" and local_model:
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


@app.get("/api/conversations/{conv_id}/branch-state/{msg_id}")
async def api_get_branch_state(conv_id: int, msg_id: int):
    """Get effective state for a specific branch point (base + deltas)."""
    return await db.get_branch_state(conv_id, msg_id)


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
    {"group": "Gemini", "models": [
        {"value": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview"},
        {"value": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
        {"value": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash Lite Preview"},
        {"value": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"value": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
        {"value": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite"},
    ]},
]


@app.get("/api/cc-models")
async def api_cc_models():
    return CC_MODELS


@app.get("/api/ollama/vision-models")
async def api_ollama_vision_models():
    """Return Ollama models that support vision (have a projector/clip family or vision capability)."""
    _VISION_FAMILIES = {"clip", "mllama", "mmproj"}
    host = config.ollama_host
    if host and not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{host}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            vision = []
            for m in data.get("models", []):
                details = m.get("details", {})
                families = set(details.get("families") or [])
                # Check families for known vision projector types
                if families & _VISION_FAMILIES:
                    vision.append(m["name"])
                    continue
                # Newer Ollama versions may include capabilities
                caps = m.get("capabilities") or []
                if "vision" in caps:
                    vision.append(m["name"])
            return {"models": vision}
    except Exception as e:
        return {"models": [], "error": str(e)}


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

    # These tools always need user interaction, even under "Allow All"
    _interactive_tools = {"ExitPlanMode", "exit_plan_mode"}

    # Auto-approve if user previously clicked "Allow All" for this session
    if conv_id in _auto_approve_sessions and tool_name not in _interactive_tools:
        print(f"[PERM] Auto-approved (Allow All active)")
        return {"allow": True}

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
    }

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
    }

    # Wait for user response (no timeout - CC waits indefinitely)
    await _pending_hook_permissions[request_id]["event"].wait()
    perm_data = _pending_hook_permissions.pop(request_id, {})
    user_response = perm_data.get("response", {})

    allowed = user_response.get("allow", False)
    print(f"[PERM] User decision: {'allow' if allowed else 'deny'}")

    if allowed:
        return {"allow": True}
    else:
        return {"allow": False, "message": "Denied by user in Loom UI"}


# ── Skills & Modules ──


@app.get("/api/skills")
async def list_skills(conv_id: int = None):
    """List available skills for a conversation (or globally).
    Scans both built-in skills and .claude/skills/ in the project directory.
    """
    project_dir = None
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv:
            project_dir = conv.get("project_dir")
    skills = get_all_skills(project_dir)
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
    if not clients:
        return
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(data)
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


@app.websocket("/ws/chat/{conv_id}")
async def ws_chat(websocket: WebSocket, conv_id: int):
    await websocket.accept()
    print(f"[WS] Connection opened for conv={conv_id}")
    if conv_id not in _active_websockets:
        _active_websockets[conv_id] = set()
    _active_websockets[conv_id].add(websocket)

    # Tell the client whether a generation is running — include live snapshot if available
    active_gen_keys = [
        k for k, t in _active_generations.items() if k[0] == conv_id and not t.done()
    ]
    if active_gen_keys:
        # Find the snapshot for the active generation(s)
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
                        "mode": snap.get("mode", "claude"),
                    }
                )
        await websocket.send_json(
            {
                "type": "generation_active",
                "snapshots": snapshots,
            }
        )
        # Resend ALL pending permission requests (broadcast globally now)
        for rid, pending in list(_pending_hook_permissions.items()):
            if not pending.get("response"):
                print(f"[WS] Resending pending permission request {rid} on reconnect")
                await websocket.send_json(
                    {
                        "type": "permission_request",
                        "request_id": rid,
                        "conv_id": pending.get("conv_id"),
                        "tool_name": pending.get("tool_name", ""),
                        "tool_input": pending.get("tool_input", ""),
                        "input_summary": pending.get("input_summary", ""),
                    }
                )
    else:
        await websocket.send_json({"type": "generation_idle"})
        # Even when idle, resend any pending permissions from other conversations
        for rid, pending in list(_pending_hook_permissions.items()):
            if not pending.get("response"):
                await websocket.send_json(
                    {
                        "type": "permission_request",
                        "request_id": rid,
                        "conv_id": pending.get("conv_id"),
                        "tool_name": pending.get("tool_name", ""),
                        "tool_input": pending.get("tool_input", ""),
                        "input_summary": pending.get("input_summary", ""),
                    }
                )

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            if action == "cancel":
                # Cancel all active generations for this conversation
                cancelled_keys = [k for k in _active_generations if k[0] == conv_id]
                for key in cancelled_keys:
                    task = _active_generations.pop(key, None)
                    if task:
                        task.cancel()
                proc = _active_claude_procs.pop(conv_id, None)
                if proc:
                    await claude_client.cancel_claude(proc)
                # Clean up pending permissions from memory and DB
                for rid in list(_pending_hook_permissions):
                    if _pending_hook_permissions[rid].get("conv_id") == conv_id:
                        _pending_hook_permissions.pop(rid, None)
                        await db.delete_pending_permission(rid)
                # Delete empty draft messages left by cancelled generations
                for key in cancelled_keys:
                    snap = _generation_snapshots.pop(key, None)
                    if snap and snap.get("draft_msg_id"):
                        msg = await db.get_message(snap["draft_msg_id"])
                        if msg and not (msg.get("content") or "").strip():
                            await db.delete_branch(snap["draft_msg_id"])
                # Send cancelled event immediately so UI responds
                await _ws_send(conv_id, {"type": "cancelled"})
                continue

            if action == "permission_response":
                request_id = data.get("request_id", "")
                if data.get("always"):
                    _auto_approve_sessions.add(conv_id)
                # Resolve the hook-based pending permission
                pending = _pending_hook_permissions.get(request_id)
                if pending:
                    pending["response"] = {
                        "allow": data.get("allow", False),
                        "always": data.get("always", False),
                    }
                    pending["event"].set()
                    # Also clean up from DB
                    await db.delete_pending_permission(request_id)
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
                is_cc = mode == "claude"

                if is_cc:
                    # CC mode: only one generation per conversation
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
                                    "error": "Claude Code generation already running on another branch. Wait for it to finish or cancel it first.",
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

        # Determine provider (same logic as _handle_claude_generation)
        _ANTHROPIC_MODELS = {"sonnet", "opus", "haiku"}
        _base_model = cc_model.split("[")[0] if "[" in cc_model else cc_model
        is_anthropic = _base_model in _ANTHROPIC_MODELS
        use_ollama = conv.get("_use_ollama", False) or not (is_anthropic or cc_model.startswith("gemini"))
        if is_anthropic or cc_model.startswith("gemini"):
            use_ollama = False

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
            use_ollama=use_ollama,
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


async def _handle_generation(websocket: WebSocket, conv_id: int, data: dict):
    """Handle a generation request over WebSocket — routes by conversation mode."""
    conv = await db.get_conversation(conv_id)
    mode = conv.get("mode", "weave") if conv else "weave"

    if mode == "claude":
        await _handle_claude_generation(websocket, conv_id, conv, data)
        return

    if mode == "local":
        await _handle_local_generation(websocket, conv_id, conv, data)
        return

    if conv.get("ooda_enabled"):
        await _handle_ooda_generation(websocket, conv_id, conv, data)
    else:
        await _handle_weave_generation(websocket, conv_id, conv, data)


async def _handle_claude_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Claude Code CLI generation with session resume support."""
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")

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
        # _use_ollama on the conv dict — don't let the frontend override it.
        # For claude mode, prefer the client's current UI state over stale DB.
        if conv.get("_use_ollama"):
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

        # Identify provider based on model name
        # Strip [1m] suffix for provider detection (e.g. "sonnet[1m]" → "sonnet")
        _ANTHROPIC_MODELS = {"sonnet", "opus", "haiku"}
        _base_model = cc_model.split("[")[0] if "[" in cc_model else cc_model
        is_anthropic = _base_model in _ANTHROPIC_MODELS
        is_gemini = cc_model.startswith("gemini")
        # Only use Ollama when explicitly flagged (local mode) or when the model
        # is not a known Anthropic/Gemini model.  The _use_ollama flag is set by
        # _handle_local_generation on a shallow copy — it is never in the DB.
        use_ollama = conv.get("_use_ollama", False) or not (is_anthropic or is_gemini)
        # Belt-and-suspenders: NEVER route Anthropic/Gemini through Ollama
        if is_anthropic or is_gemini:
            use_ollama = False
        print(f"[CC] Model routing: cc_model={cc_model!r} is_anthropic={is_anthropic} is_gemini={is_gemini} use_ollama={use_ollama} conv._use_ollama={conv.get('_use_ollama')} conv.mode={conv.get('mode')}")

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
                if not content.strip() or content.startswith("[Error:"):
                    print(f"[CC] Skipping empty/error session on msg {msg['id']}")
                    continue
                # Check if the session was created by a different provider
                prev_model = msg.get("cc_model_used") or ""
                _prev_base = prev_model.split("[")[0] if "[" in prev_model else prev_model
                prev_is_anthropic = _prev_base in _ANTHROPIC_MODELS
                prev_is_gemini = prev_model.startswith("gemini")
                prev_is_ollama = bool(prev_model) and not (
                    prev_is_anthropic or prev_is_gemini
                )
                if (
                    (prev_is_anthropic != is_anthropic)
                    or (prev_is_gemini != is_gemini)
                    or (prev_is_ollama != use_ollama)
                ):
                    print(
                        f"[CC] Cross-provider turn at msg {msg['id']} ({prev_model}), will rebuild full history"
                    )
                    crossed_provider = True
                    break
                # Gemini CLI doesn't support --fork-session, so resuming
                # mutates the session in place. Skip resume for Gemini.
                if is_gemini:
                    print(f"[CC] Gemini has no --fork-session, skipping resume for branch safety")
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
                    if is_gemini:
                        print(f"[CC] WARNING: Empty prompt from branch for Gemini!")
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
                for ip in img_paths:
                    src = Path(ip).resolve()
                    file_ext = src.suffix.lower()
                    # Use attached_files subfolder for all attached files
                    attached_files_dir = Path(project_dir) / "attached_files"
                    attached_files_dir.mkdir(exist_ok=True)
                    dest = attached_files_dir / src.name
                    copied = False
                    try:
                        shutil.copy2(str(src), str(dest))
                        copied = True
                        # For local mode, describe from project_dir where CC can access it
                        if use_ollama and copied:
                            src = dest
                    except Exception as e:
                        print(
                            f"[UPLOAD] Failed to copy file {src.name} to attached_files/: {e}"
                        )
                    if use_ollama:
                        # Local mode: describe image via Ollama's native multimodal API
                        # since CC's view_image tool returns file paths that local models can't parse
                        if file_ext in _IMAGE_EXTS:
                            try:
                                await _ws_send(conv_id, {"type": "status", "text": f"Describing image {src.name}..."})
                                desc = await asyncio.wait_for(describe_image(str(src), model=config.vision_model or None, context=_describe_context), timeout=120)
                                file_notes.append(f"[Image: {desc}]")
                            except asyncio.TimeoutError:
                                print(f"[DESCRIBE] Timed out describing {src.name} (30s)")
                                file_notes.append("[Image shared but description timed out]")
                            except Exception as e:
                                print(f"[DESCRIBE] Failed to describe image {src.name}: {e}")
                                file_notes.append("[Image shared but description unavailable]")
                        else:
                            file_notes.append(f"{src.name} (in attached_files/)")
                    else:
                        if copied:
                            file_notes.append(
                                f"{src.name} (in attached_files/)"
                            )
                        else:
                            file_notes.append(str(src).replace("\\", "/"))
                if file_notes:
                    files_str = "\n".join(f"  • {note}" for note in file_notes)
                    _has_img = any(note.startswith("[Image") for note in file_notes)
                    _hdr = "[Attached files — image descriptions already provided, do NOT read image files.]" if _has_img else f"[User attached {len(file_notes)} file(s). See attached_files/ for the files.]"
                    prompt += f"\n\n{_hdr}\n{files_str}"
        # Create draft message in DB immediately so it survives navigation/restarts.
        # If parent already has an empty assistant child (stale draft), reuse it.
        draft_msg = None
        if parent_id:
            existing_children = await db.get_children(parent_id)
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
                conv_id, "assistant", "", parent_id=parent_id
            )
        draft_msg_id = draft_msg["id"]
        await db.set_active_branch(conv_id, draft_msg_id)

        # Initialize live snapshot for this generation (survives WS disconnects)
        import time as _time

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
                mode="gemini" if is_gemini else ("local" if use_ollama else "claude"),
            )

        # Let the client know we're launching
        if is_gemini:
            launch_label = f"Launching Gemini CLI ({cc_model})..."
        elif use_ollama:
            launch_label = f"Launching {cc_model} via Ollama..."
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
            },
        )

        # Initialize accumulation vars before try so error handlers can reference them
        full_text = ""
        content_blocks = []

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
                    use_ollama=use_ollama,
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
                        use_ollama=use_ollama,
                    )
                use_resume = False
            else:
                raise

        _active_claude_procs[conv_id] = proc

        full_text = ""
        content_blocks = []
        current_block = None
        result_info = {}
        new_session_id = ""
        total_input_tokens = 0
        total_output_tokens = 0
        got_error = False

        async for evt in event_stream:
            etype = evt["type"]

            if etype == "session_info":
                new_session_id = evt.get("session_id", "") or new_session_id

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
                total_input_tokens += evt.get("input_tokens", 0)
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
                if draft_msg_id:
                    _db = await db.get_db()
                    await _db.execute(
                        "UPDATE messages SET parent_id = ? WHERE id = ?",
                        (marker["id"], draft_msg_id),
                    )
                    await _db.commit()
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
                    for ip in img_paths:
                        src = Path(ip).resolve()
                        file_ext = src.suffix.lower()
                        # Use attached_files subfolder for all attached files
                        attached_files_dir = Path(project_dir) / "attached_files"
                        attached_files_dir.mkdir(exist_ok=True)
                        dest = attached_files_dir / src.name
                        copied = False
                        try:
                            shutil.copy2(str(src), str(dest))
                            copied = True
                            # For local mode, describe from project_dir where CC can access it
                            if use_ollama and copied:
                                src = dest
                        except Exception as e:
                            print(
                                f"[UPLOAD] Failed to copy file {src.name} to attached_files/: {e}"
                            )
                        if use_ollama:
                            # Local mode: describe image via Ollama's native multimodal API
                            # since CC's view_image tool returns file paths that local models can't parse
                            if file_ext in _IMAGE_EXTS:
                                try:
                                    await _ws_send(conv_id, {"type": "status", "text": f"Describing image {src.name}..."})
                                    desc = await asyncio.wait_for(describe_image(str(src), model=config.vision_model or None, context=_describe_context), timeout=120)
                                    file_notes.append(f"[Image: {desc}]")
                                except asyncio.TimeoutError:
                                    print(f"[DESCRIBE] Timed out describing {src.name} (30s)")
                                    file_notes.append("[Image shared but description timed out]")
                                except Exception as e:
                                    print(f"[DESCRIBE] Failed to describe image {src.name}: {e}")
                                    file_notes.append("[Image shared but description unavailable]")
                            else:
                                file_notes.append(f"{src.name} (in attached_files/)")
                        else:
                            if copied:
                                file_notes.append(
                                    f"{src.name} (in attached_files/)"
                                )
                            else:
                                file_notes.append(str(src).replace("\\", "/"))
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
                use_ollama=use_ollama,
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
                    total_input_tokens += evt.get("input_tokens", 0)
                    total_output_tokens += evt.get("output_tokens", 0)
                    await _ws_send(
                        conv_id,
                        {
                            "type": "usage",
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                        },
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
                "Gemini CLI"
                if is_gemini
                else ("Ollama" if use_ollama else "Claude Code")
            )
            error_msg = (
                result_info.get("error") or f"{provider_name} exited with no response"
            )
            if use_ollama:
                error_msg += f" — check that '{cc_model}' is available in Ollama"
            await db.update_message_content(
                draft_msg_id,
                content=f"[Error: {error_msg}]",
            )
            await _ws_send(conv_id, {"type": "error", "error": error_msg})
            await _ws_send(
                conv_id,
                {"type": "stream_end", "message": await db.get_message(draft_msg_id)},
            )
            return

        # Extract cost info
        cost_usd = result_info.get("cost_usd", 0)
        input_tokens = total_input_tokens
        output_tokens = total_output_tokens
        new_session_id = result_info.get("session_id", "") or new_session_id
        duration_ms = result_info.get("duration_ms", 0)

        # Finalize the draft message with full content
        await db.update_message_content(
            draft_msg_id,
            content=full_text,
            content_blocks=json.dumps(content_blocks),
            turn_cost_usd=cost_usd,
            turn_input_tokens=input_tokens,
            turn_output_tokens=output_tokens,
            cc_session_id=new_session_id or None,
            cc_model_used=cc_model if not use_ollama else f"{cc_model}@ollama",
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
        _active_claude_procs.pop(conv_id, None)
        if draft_msg_id and (full_text or content_blocks):
            # Save accumulated work to draft before cancelling
            await db.update_message_content(
                draft_msg_id,
                content=full_text,
                content_blocks=json.dumps(content_blocks) if content_blocks else None,
            )
            print(f"[GEN] Saved partial draft {draft_msg_id} on cancel")
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
        _auto_approve_sessions.discard(conv_id)
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

    # Add file references if files were found
    if attached_files:
        files_str = "\n".join(f"  • {Path(ip).name} (in attached_files/)" for ip in attached_files)
        return f"\n\n[Attached files:]\n{files_str}\n\n" + history_parts[-1]

    if not history_parts:
        return ""

    if len(history_parts) > 1:
        history = "\n\n".join(history_parts[:-1])
        latest = history_parts[-1].removeprefix("Human: ")
        return f"<conversation_history>\n{history}\n</conversation_history>\n\n{latest}"
    else:
        return history_parts[0].removeprefix("Human: ")


async def _handle_local_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Local mode: Claude Code launched via 'ollama launch claude'."""
    # Local mode = Claude Code powered by a local Ollama model.
    # Reuse the full CC handler but with use_ollama=True.
    conv = dict(conv)  # mutable copy
    # Map local_model into cc_model so _handle_claude_generation uses it
    conv["cc_model"] = conv.get("local_model") or config.ollama_model
    conv["_use_ollama"] = True
    await _handle_claude_generation(websocket, conv_id, conv, data)


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

        context = await get_context_for_generation(conv_id, character)
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
        cleaned_pass1 = _re.sub(r"<think>[\s\S]*?</think>\s*", "", raw_pass1).strip()
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

            # Execute state reads
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
        await db.update_message_content(draft_msg_id, content=final_prose)
        await db.set_active_branch(conv_id, draft_msg_id)
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
        context = await get_context_for_generation(conv_id, character)
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
        weave_model = conv.get("local_model") or None
        async for token in stream_chat(messages, model=weave_model):
            if isinstance(token, dict):
                # Thinking status events
                await _ws_send(conv_id, token)
                continue
            full_response += token
            await _ws_send(
                conv_id,
                {
                    "type": "stream_chunk",
                    "content": token,
                },
            )
            # Keep live snapshot in sync
            if _gen_key_local:
                _update_gen_snapshot(_gen_key_local, full_text=full_response)

        # Strip <think>...</think> blocks (thinking models like qwen3)
        import re as _re

        cleaned = _re.sub(r"<think>[\s\S]*?</think>\s*", "", full_response).strip()
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
        await db.update_message_content(draft_msg_id, content=full_response)
        await db.set_active_branch(conv_id, draft_msg_id)
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
            await db.update_message_content(draft_msg_id, content=full_response)
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
