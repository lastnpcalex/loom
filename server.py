"""FastAPI server with REST endpoints and WebSocket streaming."""

import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketState

import database as db
from config import config
from character_loader import (
    load_all_characters, load_character, save_character, delete_character,
    load_all_personas, load_persona, save_persona, delete_persona,
    load_all_lore, load_lore_entry, save_lore, delete_lore,
)
from ollama_client import health_check, stream_chat, describe_image
from prompt_engine import (
    build_system_prompt, assemble_prompt,
    get_style_nudge, STYLE_NUDGES
)
from context_manager import get_context_for_generation, update_rolling_summary
import local_summary
import claude_client

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    await db.init_db()
    # Preload Gemma 3 1B for CPU summarization (downloads ~806MB on first run)
    asyncio.create_task(_preload_summarizer())
    yield


async def _preload_summarizer():
    """Background preload — doesn't block server startup."""
    try:
        await local_summary.preload()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Gemma preload failed (will retry on first use): {e}")

app = FastAPI(title="Ex Astris Umbra — A Loom Interface", lifespan=lifespan)

# Ensure upload directory exists
os.makedirs(config.upload_dir, exist_ok=True)

# Active WebSocket generation tasks (for cancellation)
_active_generations: dict[int, asyncio.Task] = {}
# Active Claude Code subprocesses (for cancellation)
_active_claude_procs: dict[int, asyncio.subprocess.Process] = {}
# Active WebSocket connections per conversation (for permission prompts)
_active_websockets: dict[int, WebSocket] = {}
# Pending hook-based permission requests: request_id -> {event, response, conv_id}
_pending_hook_permissions: dict[str, dict] = {}
# Sessions where user clicked "Allow All" — auto-approve for rest of generation
_auto_approve_sessions: set[int] = set()


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


async def _background_summarize_message(msg_id: int, content: str, role: str,
                                         conv_id: int = None, image_path: str = None):
    """Generate a short Gemma summary for a message (tree pill) and update
    the rolling context summary if needed."""
    try:
        summary_content = content
        paths = _parse_image_paths(image_path)

        if paths:
            alts = []
            for p in paths:
                alt = await describe_image(p)
                alts.append(alt)
            combined_alt = "; ".join(alts)
            await db.update_message_image_alt(msg_id, combined_alt)
            summary_content = f"{content}\n[Attached images: {combined_alt}]"

        # Tree pill summary
        summary = await local_summary.summarize_message(summary_content, role)
        await db.update_message_summary(msg_id, summary)

        # Rolling context summary — incrementally summarize messages that aged
        # out of the verbatim window
        if conv_id:
            await update_rolling_summary(conv_id)

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Background summary failed for msg {msg_id}: {e}")


# ── Static files ──

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ── Health ──

@app.get("/api/health")
async def api_health():
    ollama_status = await health_check()
    ollama_status["local_summarizer"] = {
        "model": "gemma-3-1b-it-abliterated-Q4_K_M",
        "loaded": local_summary.is_loaded(),
        "loading": local_summary.is_loading(),
    }
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

    conv = await db.create_conversation(title, character_id, mode=mode, project_dir=project_dir)

    # Store additional fields
    import json as _json
    fields = dict(
        persona_id=persona_id,
        lore_ids=_json.dumps(lore_ids),
        style_nudge=style_nudge,
        custom_scene=custom_scene,
        cc_model=cc_model,
        cc_effort=cc_effort,
    )
    if local_model:
        fields["local_model"] = local_model
    await db.update_conversation_fields(conv["id"], **fields)
    # Refresh conv data
    conv = await db.get_conversation(conv["id"])

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
            asyncio.create_task(_background_summarize_message(
                scene_msg["id"], custom_scene, "user", conv_id=conv["id"]
            ))
        elif char and char.get("greeting"):
            greeting_msg = await db.add_message(conv["id"], "assistant", char["greeting"])
            await db.set_active_branch(conv["id"], greeting_msg["id"])
            asyncio.create_task(_background_summarize_message(
                greeting_msg["id"], char["greeting"], "assistant",
                conv_id=conv["id"]
            ))

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
    if fields:
        await db.update_conversation_fields(conv_id, **fields)
    return await db.get_conversation(conv_id)


@app.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(conv_id: int):
    await db.delete_conversation(conv_id)
    return {"ok": True}


# ── Tree ──

@app.get("/api/conversations/{conv_id}/tree")
async def api_get_tree(conv_id: int):
    tree = await db.get_conversation_tree(conv_id)
    return tree


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
    await db.set_active_branch(conv_id, leaf_id)
    branch = await db.get_active_branch(conv_id)
    return branch


# ── Messages ──

@app.post("/api/conversations/{conv_id}/messages")
async def api_add_message(conv_id: int, data: dict):
    role = data.get("role", "user")
    content = data.get("content", "")
    raw_image_path = data.get("image_path")
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

    # If no parent_id, use the current active leaf
    if parent_id is None:
        leaf = await db.get_active_leaf(conv_id)
        parent_id = leaf["id"] if leaf else None

    msg = await db.add_message(conv_id, role, content, parent_id=parent_id,
                               image_path=image_path)
    await db.set_active_branch(conv_id, msg["id"])
    # Background: generate Gemma summary for tree display
    asyncio.create_task(_background_summarize_message(msg["id"], content, role,
                                                      conv_id=conv_id, image_path=image_path))
    return msg


@app.get("/api/conversations/{conv_id}/messages/{msg_id}/siblings")
async def api_get_siblings(conv_id: int, msg_id: int):
    siblings = await db.get_siblings(msg_id)
    return siblings


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
        (conv_id,)
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
    safe_title = safe_title.encode("ascii", "ignore").decode("ascii").strip() or "conversation"
    return JSONResponse(export, headers={
        "Content-Disposition": f'attachment; filename="{safe_title}.json"'
    })


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
            new_conv["id"], msg["role"], msg.get("content", ""),
            parent_id=new_parent, image_path=msg.get("image_path"),
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
    return FileResponse(filepath, filename=f"{persona_id}.md", media_type="text/markdown")


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


# ── Image Upload ──

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        raise HTTPException(400, "Unsupported image format")

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(config.upload_dir, filename)

    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)

    return {"path": filepath, "url": f"/uploads/{filename}"}


# ── Config ──

@app.get("/api/config")
async def api_get_config():
    return config.to_dict()


@app.put("/api/config")
async def api_update_config(data: dict):
    config.update_from_dict(data)
    return config.to_dict()


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
        return {"parent": parent, "dirs": [], "current": path, "error": "Permission denied"}


# ── Serve Project Files (images, etc.) ──

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
_MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
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

    return FileResponse(target, media_type=media_type)


# ── Claude Code Permission Hook Endpoint ──

@app.post("/api/cc-permission")
async def handle_cc_permission(data: dict):
    """Receive permission request from CC hook script, forward to UI, wait for response.

    The hook script (cc_permission_hook.py) POSTs here when CC needs tool approval.
    This endpoint long-polls until the user responds in the browser UI (up to 5 min).
    """
    conv_id = int(data.get("loom_conv_id", 0))
    request_id = str(uuid.uuid4())

    tool_name = data.get("tool_name", "Unknown")
    tool_input = data.get("tool_input", {})

    print(f"[PERM] Hook request: conv={conv_id} tool={tool_name} request_id={request_id}")

    # Auto-approve if user previously clicked "Allow All" for this session
    if conv_id in _auto_approve_sessions:
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

    # Send to UI via WebSocket
    ws = _active_websockets.get(conv_id)
    if ws and ws.client_state == WebSocketState.CONNECTED:
        await ws.send_json({
            "type": "permission_request",
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "input_summary": input_summary,
        })
    else:
        print(f"[PERM] No active WebSocket for conv={conv_id} — denying")
        return {"allow": False, "message": "No active Loom UI session"}

    # Wait for user response
    event = asyncio.Event()
    _pending_hook_permissions[request_id] = {
        "event": event,
        "response": None,
        "conv_id": conv_id,
    }

    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        user_response = _pending_hook_permissions.pop(request_id, {}).get("response", {})

        allowed = user_response.get("allow", False)
        print(f"[PERM] User decision: {'allow' if allowed else 'deny'}")

        if allowed:
            return {"allow": True}
        else:
            return {"allow": False, "message": "Denied by user in Loom UI"}

    except asyncio.TimeoutError:
        _pending_hook_permissions.pop(request_id, None)
        print(f"[PERM] Timeout for request_id={request_id}")
        return {"allow": False, "message": "Permission timeout (5min)"}


# ── WebSocket Chat ──

async def _ws_send(conv_id: int, data: dict):
    """Best-effort send to the active WebSocket for a conversation.
    Silently skips if no client is connected — generation continues regardless."""
    ws = _active_websockets.get(conv_id)
    if ws is None:
        return
    try:
        await ws.send_json(data)
    except Exception:
        # Client gone — that's fine, generation keeps going
        pass


@app.websocket("/ws/chat/{conv_id}")
async def ws_chat(websocket: WebSocket, conv_id: int):
    await websocket.accept()
    print(f"[WS] Connection opened for conv={conv_id}")
    _active_websockets[conv_id] = websocket

    # Tell the client whether a generation is running so it can sync state
    if conv_id in _active_generations and not _active_generations[conv_id].done():
        await websocket.send_json({"type": "generation_active"})
    else:
        await websocket.send_json({"type": "generation_idle"})

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            if action == "cancel":
                task = _active_generations.pop(conv_id, None)
                if task:
                    task.cancel()
                proc = _active_claude_procs.pop(conv_id, None)
                if proc:
                    await claude_client.cancel_claude(proc)
                for rid in list(_pending_hook_permissions):
                    if _pending_hook_permissions[rid].get("conv_id") == conv_id:
                        _pending_hook_permissions.pop(rid, None)
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
                continue

            print(f"[WS] Received action={action} for conv={conv_id}")
            if action in ("generate", "regenerate"):
                # Cancel any existing generation
                old_task = _active_generations.pop(conv_id, None)
                if old_task:
                    old_task.cancel()

                task = asyncio.create_task(
                    _handle_generation(websocket, conv_id, data)
                )
                _active_generations[conv_id] = task

    except WebSocketDisconnect:
        print(f"[WS] Client disconnected conv={conv_id}")
        # Remove websocket ref but do NOT cancel active generation — let it finish and save
        _active_websockets.pop(conv_id, None)
    except Exception as e:
        print(f"[WS] Error conv={conv_id}: {e}")
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({"type": "error", "error": str(e)})


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

    await _handle_weave_generation(websocket, conv_id, conv, data)


async def _handle_claude_generation(websocket: WebSocket, conv_id: int, conv: dict, data: dict):
    """Handle Claude Code CLI generation with session resume support."""
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")

        if action == "generate" and parent_id is None:
            leaf = await db.get_active_leaf(conv_id)
            parent_id = leaf["id"] if leaf else None

        project_dir = conv.get("project_dir") or "."
        cc_model = conv.get("cc_model") or "sonnet"
        cc_effort = conv.get("cc_effort") or "high"

        # --- Session resume logic ---
        # Walk ancestors to find the nearest assistant node with a cc_session_id.
        # Only resume if the parent message itself has a session (linear continue)
        # or we're regenerating.  When the user edits a message and branches,
        # parent_id is the NEW user message whose ancestor session is stale —
        # resuming it would leak the old branch's context.
        resume_session_id = None
        fork_session = False
        use_resume = False

        if parent_id:
            branch = await db.get_branch_to_root(parent_id)
            parent_msg = branch[-1] if branch else None

            if action == "regenerate":
                # Regenerate: find nearest ancestor session and fork it
                for msg in reversed(branch):
                    if msg["role"] == "assistant" and msg.get("cc_session_id"):
                        resume_session_id = msg["cc_session_id"]
                        fork_session = True
                        break
            elif parent_msg and parent_msg["role"] == "assistant" and parent_msg.get("cc_session_id"):
                # Linear continue: parent is an assistant message with a session
                resume_session_id = parent_msg["cc_session_id"]

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

            # Attach images if present on the latest user message
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
                        dest = Path(project_dir) / src.name
                        try:
                            shutil.copy2(str(src), str(dest))
                            file_notes.append(f"{src.name} (placed in working directory)")
                        except Exception:
                            abs_path = str(src).replace("\\", "/")
                            file_notes.append(f"{abs_path}")
                    if file_notes:
                        files_str = ", ".join(file_notes)
                        prompt += f"\n\n[User attached {len(file_notes)} file(s): {files_str}. Use the Read tool to view them.]"
        else:
            # No session to resume — fall back to full history rebuild
            branch = await db.get_branch_to_root(parent_id) if parent_id else []
            prompt = _build_claude_history_prompt(branch)
            if not prompt:
                await _ws_send(conv_id, {"type": "error", "error": "No message to send to Claude"})
                return

            # Attach images
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
                        dest = Path(project_dir) / src.name
                        try:
                            shutil.copy2(str(src), str(dest))
                            file_notes.append(f"{src.name} (placed in working directory)")
                        except Exception:
                            abs_path = str(src).replace("\\", "/")
                            file_notes.append(f"{abs_path}")
                    if file_notes:
                        files_str = ", ".join(file_notes)
                        prompt += f"\n\n[User attached {len(file_notes)} file(s): {files_str}. Use the Read tool to view them.]"

        await _ws_send(conv_id, {"type": "stream_start"})

        # Launch CC — with resume if available, with fallback on failure
        try:
            proc, event_stream = await claude_client.run_claude(
                prompt, project_dir, conv_id=conv_id, server_port=config.port,
                model=cc_model, effort=cc_effort,
                resume_session_id=resume_session_id if use_resume else None,
                fork_session=fork_session,
            )
        except Exception as e:
            if use_resume:
                # Fallback: retry without --resume (session may be stale/deleted)
                print(f"[CC] Resume failed ({e}), falling back to full history")
                branch = await db.get_branch_to_root(parent_id) if parent_id else []
                prompt = _build_claude_history_prompt(branch) or "(continue)"
                proc, event_stream = await claude_client.run_claude(
                    prompt, project_dir, conv_id=conv_id, server_port=config.port,
                    model=cc_model, effort=cc_effort,
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
                await _ws_send(conv_id, {"type": "stream_chunk", "content": evt["text"]})

            elif etype == "tool_start":
                current_block = {
                    "type": "tool_use",
                    "name": evt["name"],
                    "tool_id": evt.get("tool_id", ""),
                    "input": "",
                    "result": "",
                }
                content_blocks.append(current_block)
                await _ws_send(conv_id, {
                    "type": "tool_start",
                    "name": evt["name"],
                    "tool_id": evt.get("tool_id", ""),
                })

            elif etype == "tool_input_delta":
                if current_block and current_block["type"] == "tool_use":
                    current_block["input"] += evt["json"]
                await _ws_send(conv_id, {
                    "type": "tool_input_chunk",
                    "content": evt["json"],
                    "tool_id": evt.get("tool_id", ""),
                })

            elif etype == "tool_result":
                result_content = evt.get("content", "")
                tool_id = evt.get("tool_id", "")
                for block in reversed(content_blocks):
                    if block["type"] == "tool_use" and block.get("tool_id") == tool_id:
                        block["result"] = result_content
                        break
                current_block = None
                await _ws_send(conv_id, {
                    "type": "tool_result",
                    "content": result_content,
                    "tool_id": tool_id,
                })

            elif etype == "thinking_delta":
                if current_block and current_block["type"] == "thinking":
                    current_block["text"] += evt["text"]
                else:
                    current_block = {"type": "thinking", "text": evt["text"]}
                    content_blocks.append(current_block)
                await _ws_send(conv_id, {"type": "thinking_chunk", "content": evt["text"]})

            elif etype == "usage":
                total_input_tokens += evt.get("input_tokens", 0)
                total_output_tokens += evt.get("output_tokens", 0)

            elif etype == "cc_raw_event":
                # Forward unknown events to UI for debugging
                raw_data = evt.get("data", {})
                raw_type = evt.get("event_type", "")
                print(f"[CC] Unknown event type={raw_type}: {json.dumps(raw_data, default=str)[:300]}")
                await _ws_send(conv_id, {
                    "type": "cc_debug_event",
                    "event_type": raw_type,
                    "data": raw_data,
                })

            elif etype == "result":
                result_info = evt
                got_error = evt.get("is_error", False)
                # Use result text as fallback if no text came from assistant events
                if not full_text and evt.get("result_text"):
                    full_text = evt["result_text"]
                    content_blocks.append({"type": "text", "text": full_text})

        _active_claude_procs.pop(conv_id, None)

        # If --resume failed (is_error), retry with full history fallback
        if got_error and use_resume and not full_text:
            print(f"[CC] Resume returned error, retrying with full history")
            branch = await db.get_branch_to_root(parent_id) if parent_id else []
            fallback_prompt = _build_claude_history_prompt(branch) or "(continue)"
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
                        dest = Path(project_dir) / src.name
                        try:
                            shutil.copy2(str(src), str(dest))
                            file_notes.append(f"{src.name} (placed in working directory)")
                        except Exception:
                            abs_path = str(src).replace("\\", "/")
                            file_notes.append(f"{abs_path}")
                    if file_notes:
                        files_str = ", ".join(file_notes)
                        fallback_prompt += f"\n\n[User attached {len(file_notes)} file(s): {files_str}. Use the Read tool to view them.]"

            proc, event_stream = await claude_client.run_claude(
                fallback_prompt, project_dir, conv_id=conv_id, server_port=config.port,
                model=cc_model, effort=cc_effort,
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
                elif etype == "text_delta":
                    full_text += evt["text"]
                    if current_block and current_block["type"] == "text":
                        current_block["text"] += evt["text"]
                    else:
                        current_block = {"type": "text", "text": evt["text"]}
                        content_blocks.append(current_block)
                    await _ws_send(conv_id, {"type": "stream_chunk", "content": evt["text"]})
                elif etype == "tool_start":
                    current_block = {"type": "tool_use", "name": evt["name"], "tool_id": evt.get("tool_id", ""), "input": "", "result": ""}
                    content_blocks.append(current_block)
                    await _ws_send(conv_id, {"type": "tool_start", "name": evt["name"], "tool_id": evt.get("tool_id", "")})
                elif etype == "tool_input_delta":
                    if current_block and current_block["type"] == "tool_use":
                        current_block["input"] += evt["json"]
                    await _ws_send(conv_id, {"type": "tool_input_chunk", "content": evt["json"], "tool_id": evt.get("tool_id", "")})
                elif etype == "tool_result":
                    result_content = evt.get("content", "")
                    tool_id = evt.get("tool_id", "")
                    for block in reversed(content_blocks):
                        if block["type"] == "tool_use" and block.get("tool_id") == tool_id:
                            block["result"] = result_content
                            break
                    current_block = None
                    await _ws_send(conv_id, {"type": "tool_result", "content": result_content, "tool_id": tool_id})
                elif etype == "thinking_delta":
                    if current_block and current_block["type"] == "thinking":
                        current_block["text"] += evt["text"]
                    else:
                        current_block = {"type": "thinking", "text": evt["text"]}
                        content_blocks.append(current_block)
                    await _ws_send(conv_id, {"type": "thinking_chunk", "content": evt["text"]})
                elif etype == "usage":
                    total_input_tokens += evt.get("input_tokens", 0)
                    total_output_tokens += evt.get("output_tokens", 0)
                elif etype == "result":
                    result_info = evt
                    if not full_text and evt.get("result_text"):
                        full_text = evt["result_text"]
                        content_blocks.append({"type": "text", "text": full_text})

            _active_claude_procs.pop(conv_id, None)

        # Extract cost info
        cost_usd = result_info.get("cost_usd", 0)
        input_tokens = total_input_tokens
        output_tokens = total_output_tokens
        new_session_id = result_info.get("session_id", "") or new_session_id
        duration_ms = result_info.get("duration_ms", 0)

        # Save assistant message with cc_session_id stored on the node
        msg = await db.add_message(
            conv_id, "assistant", full_text, parent_id=parent_id,
            content_blocks=json.dumps(content_blocks),
            turn_cost_usd=cost_usd,
            turn_input_tokens=input_tokens,
            turn_output_tokens=output_tokens,
            cc_session_id=new_session_id or None,
        )
        await db.set_active_branch(conv_id, msg["id"])

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

        await _ws_send(conv_id, {
            "type": "stream_end",
            "message": dict(msg),
            "cost": cost_info,
        })

    except asyncio.CancelledError:
        _active_claude_procs.pop(conv_id, None)
        await _ws_send(conv_id, {"type": "cancelled"})
    except Exception as e:
        _active_claude_procs.pop(conv_id, None)
        print(f"[GEN] Claude generation error conv={conv_id}: {e}")
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _active_generations.pop(conv_id, None)
        _auto_approve_sessions.discard(conv_id)
        # Clean up any pending hook permissions for this conversation
        for rid in list(_pending_hook_permissions):
            if _pending_hook_permissions[rid].get("conv_id") == conv_id:
                _pending_hook_permissions.pop(rid, None)


def _build_claude_history_prompt(branch: list[dict]) -> str:
    """Build a text prompt from conversation history (fallback when --resume unavailable)."""
    history_parts = []
    for msg in branch:
        if msg["role"] == "system":
            continue
        if msg["role"] == "user":
            history_parts.append(f"Human: {msg['content']}")
        elif msg["role"] == "assistant":
            blocks = None
            if msg.get("content_blocks"):
                try:
                    blocks = json.loads(msg["content_blocks"]) if isinstance(msg["content_blocks"], str) else msg["content_blocks"]
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
                            inp = block["input"][:500] if len(block.get("input", "")) > 500 else block.get("input", "")
                            tool_summary += f"\nInput: {inp}"
                        if block.get("result"):
                            res = block["result"][:500] if len(block.get("result", "")) > 500 else block.get("result", "")
                            tool_summary += f"\nResult: {res}"
                        parts.append(tool_summary)
                history_parts.append(f"Assistant: {chr(10).join(parts)}")
            else:
                history_parts.append(f"Assistant: {msg['content']}")

    if not history_parts:
        return ""

    if len(history_parts) > 1:
        history = "\n\n".join(history_parts[:-1])
        latest = history_parts[-1].removeprefix("Human: ")
        return f"<conversation_history>\n{history}\n</conversation_history>\n\n{latest}"
    else:
        return history_parts[0].removeprefix("Human: ")


async def _handle_local_generation(websocket: WebSocket, conv_id: int, conv: dict, data: dict):
    """Handle Local mode generation with optional tool calling for file access."""
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")

        if action == "generate" and parent_id is None:
            leaf = await db.get_active_leaf(conv_id)
            parent_id = leaf["id"] if leaf else None

        model = conv.get("local_model") or config.ollama_model
        project_dir = conv.get("project_dir")

        # Build plain message list from active branch
        branch = await db.get_branch_to_root(parent_id) if parent_id else []
        if action == "regenerate" and parent_id is not None:
            branch = [m for m in branch if m["id"] <= parent_id]

        messages = []
        for m in branch:
            if m["role"] == "system":
                continue
            messages.append({"role": m["role"], "content": m["content"]})

        # If a project directory is set, use tool-calling agent loop
        if project_dir and os.path.isdir(project_dir):
            await _local_agent_loop(websocket, conv_id, messages, model,
                                    project_dir, parent_id)
        else:
            # Plain chat (no tools)
            await _local_plain_chat(conv_id, messages, model, parent_id)

    except asyncio.CancelledError:
        await _ws_send(conv_id, {"type": "cancelled"})
    except Exception as e:
        print(f"[GEN] Local generation error conv={conv_id}: {e}")
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _active_generations.pop(conv_id, None)


async def _local_request_permission(conv_id: int, tool_name: str, tool_input: dict) -> bool:
    """Request permission from the user — same UI flow as CC permission hooks."""
    # Respect "Allow All" if user already clicked it for this session
    if conv_id in _auto_approve_sessions:
        return True

    request_id = str(uuid.uuid4())

    # Build human-readable summary (same logic as CC hook handler)
    if isinstance(tool_input, dict):
        if "path" in tool_input:
            input_summary = tool_input["path"]
        elif "pattern" in tool_input:
            input_summary = tool_input["pattern"]
        else:
            input_summary = json.dumps(tool_input)[:500]
    else:
        input_summary = str(tool_input)[:500]

    ws = _active_websockets.get(conv_id)
    if not ws or ws.client_state != WebSocketState.CONNECTED:
        print(f"[PERM-LOCAL] No active WebSocket for conv={conv_id} — denying")
        return False

    await ws.send_json({
        "type": "permission_request",
        "request_id": request_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "input_summary": input_summary,
    })

    event = asyncio.Event()
    _pending_hook_permissions[request_id] = {
        "event": event,
        "response": None,
        "conv_id": conv_id,
    }

    try:
        await asyncio.wait_for(event.wait(), timeout=300)
        user_response = _pending_hook_permissions.pop(request_id, {}).get("response", {})
        return user_response.get("allow", False)
    except asyncio.TimeoutError:
        _pending_hook_permissions.pop(request_id, None)
        return False


async def _local_agent_loop(websocket: WebSocket, conv_id: int,
                            messages: list[dict], model: str,
                            project_dir: str, parent_id: int | None):
    """Run a tool-calling agent loop with the local Ollama model."""
    import local_tools

    system_prompt = local_tools.build_system_prompt(project_dir)
    agent_messages = [{"role": "system", "content": system_prompt}] + messages

    await _ws_send(conv_id, {"type": "stream_start"})

    max_tool_rounds = 15
    full_response = ""

    for round_num in range(max_tool_rounds):
        # Make a non-streaming request to check for tool calls
        tool_calls = await _ollama_tool_request(agent_messages, model, local_tools.TOOL_DEFINITIONS)

        if tool_calls:
            # Model wants to use tools — process each call
            # Add the assistant message with tool calls to context
            agent_messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    func_args = json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"]
                except json.JSONDecodeError:
                    func_args = {}

                tool_id = tc.get("id", str(uuid.uuid4())[:8])

                # Send tool_start to UI
                await _ws_send(conv_id, {
                    "type": "tool_start",
                    "name": func_name,
                    "tool_id": tool_id,
                })

                # Send tool input to UI
                input_display = json.dumps(func_args, indent=2)
                await _ws_send(conv_id, {
                    "type": "tool_input_chunk",
                    "content": input_display,
                    "tool_id": tool_id,
                })

                # Request permission for write operations
                allowed = await _local_request_permission(conv_id, func_name, func_args)

                if allowed:
                    result = local_tools.execute_tool(project_dir, func_name, func_args)
                    # Notify UI permission was resolved
                    await _ws_send(conv_id, {
                        "type": "permission_resolved",
                        "request_id": "",
                        "allowed": True,
                    })
                else:
                    result = f"Permission denied by user for {func_name}"
                    await _ws_send(conv_id, {
                        "type": "permission_resolved",
                        "request_id": "",
                        "allowed": False,
                    })

                # Truncate very large results for display
                display_result = result[:2000] + "..." if len(result) > 2000 else result

                # Check if this tool produced an image
                image_url = None
                if func_name == "write_file" and allowed:
                    written_path = func_args.get("path", "")
                    suffix = Path(written_path).suffix.lower()
                    if suffix in _IMAGE_EXTENSIONS:
                        image_url = f"/api/conversations/{conv_id}/file?path={written_path}"

                # Send tool result to UI
                tool_result_msg = {
                    "type": "tool_result",
                    "content": display_result,
                    "tool_id": tool_id,
                }
                if image_url:
                    tool_result_msg["image_url"] = image_url
                await _ws_send(conv_id, tool_result_msg)

                # Add tool result to context for next round
                agent_messages.append({
                    "role": "tool",
                    "content": result,
                })

            print(f"[GEN] Local agent round {round_num + 1}: {len(tool_calls)} tool call(s)")
            continue  # Next round — let model process tool results

        # No tool calls — model is giving a final text response, stream it
        full_response = ""
        async for token in stream_chat(agent_messages, model=model):
            if isinstance(token, dict):
                await _ws_send(conv_id, token)
                continue
            full_response += token
            await _ws_send(conv_id, {"type": "stream_chunk", "content": token})
        break  # Done

    # Strip <think>...</think> blocks
    import re as _re
    cleaned = _re.sub(r'<think>[\s\S]*?</think>\s*', '', full_response).strip()
    if cleaned:
        full_response = cleaned

    if not full_response.strip():
        await _ws_send(conv_id, {
            "type": "error",
            "error": "Model returned an empty response — try again",
        })
        return

    msg = await db.add_message(conv_id, "assistant", full_response, parent_id=parent_id)
    await db.set_active_branch(conv_id, msg["id"])
    asyncio.create_task(_background_summarize_message(
        msg["id"], full_response, "assistant", conv_id=conv_id
    ))

    await _ws_send(conv_id, {"type": "stream_end", "message": dict(msg)})


async def _ollama_tool_request(messages: list[dict], model: str, tools: list[dict]) -> list[dict] | None:
    """Make a non-streaming Ollama request with tools. Returns tool_calls or None."""
    import httpx
    from ollama_client import _build_ollama_messages

    # Build messages, filtering out tool_calls metadata for the API
    ollama_msgs = []
    for msg in messages:
        entry = {"role": msg["role"], "content": msg.get("content", "")}
        if msg.get("images"):
            entry["images"] = msg["images"]
        ollama_msgs.append(entry)

    payload = {
        "model": model,
        "messages": ollama_msgs,
        "stream": False,
        "tools": tools,
        "options": {
            "temperature": config.temperature,
            "num_predict": config.max_tokens,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            resp = await client.post(f"{config.ollama_host}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls")

            if tool_calls and len(tool_calls) > 0:
                return tool_calls
            return None
    except Exception as e:
        print(f"[OLLAMA-TOOLS] Error: {e}")
        return None


async def _local_plain_chat(conv_id: int, messages: list[dict],
                            model: str, parent_id: int | None):
    """Plain streaming chat with no tool calling (original local mode behavior)."""
    await _ws_send(conv_id, {"type": "stream_start"})

    full_response = ""
    async for token in stream_chat(messages, model=model):
        if isinstance(token, dict):
            await _ws_send(conv_id, token)
            continue
        full_response += token
        await _ws_send(conv_id, {"type": "stream_chunk", "content": token})

    # Strip <think>...</think> blocks
    import re as _re
    cleaned = _re.sub(r'<think>[\s\S]*?</think>\s*', '', full_response).strip()
    if cleaned:
        full_response = cleaned

    if not full_response.strip():
        await _ws_send(conv_id, {
            "type": "error",
            "error": "Model returned an empty response — try again",
        })
        return

    msg = await db.add_message(conv_id, "assistant", full_response, parent_id=parent_id)
    await db.set_active_branch(conv_id, msg["id"])
    asyncio.create_task(_background_summarize_message(
        msg["id"], full_response, "assistant", conv_id=conv_id
    ))

    await _ws_send(conv_id, {"type": "stream_end", "message": dict(msg)})


async def _handle_weave_generation(websocket: WebSocket, conv_id: int, conv: dict, data: dict):
    """Handle Weave (Ollama) generation — original logic."""
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        print(f"[GEN] _handle_weave_generation called: action={action} parent_id={parent_id} conv_id={conv_id}")
        print(f"[GEN] conv={conv}")

        # For regenerate, parent_id should be provided
        # For generate, use the current active leaf
        if action == "generate" and parent_id is None:
            leaf = await db.get_active_leaf(conv_id)
            parent_id = leaf["id"] if leaf else None
        character = None
        if conv and conv.get("character_id"):
            char_path = os.path.join(config.characters_dir, f"{conv['character_id']}.md")
            print(f"[GEN] Loading character from: {char_path} (exists={os.path.exists(char_path)})")
            character = load_character(char_path)
            print(f"[GEN] Character loaded: name={character.get('name') if character else 'NONE'}, personality_len={len(character.get('personality','')) if character else 0}, scenario_len={len(character.get('scenario','')) if character else 0}")
        else:
            print(f"[GEN] No character_id on conv! character_id={conv.get('character_id') if conv else 'NO CONV'}")

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
            persona = load_persona(os.path.join("personas", f"{conv['persona_id']}.md"))

        # Load lore entries if set
        lore_entries = []
        if conv and conv.get("lore_ids"):
            import json as _json
            try:
                lore_ids = _json.loads(conv["lore_ids"]) if isinstance(conv["lore_ids"], str) else conv["lore_ids"]
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
                m for m in context["verbatim_messages"]
                if m["id"] <= parent_id
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
        print(f"[GEN] Context verbatim_messages count: {len(context['verbatim_messages'])}")
        for i, m in enumerate(messages):
            print(f"[GEN]   msg[{i}] role={m['role']} len={len(m['content'])}")

        # Send context info
        active_nudge = get_style_nudge(nudge_index)
        await _ws_send(conv_id, {
            "type": "context_info",
            "total_tokens": context["total_tokens"],
            "was_compactified": context["was_compactified"],
            "style_nudge": active_nudge["name"],
        })

        # Stream the response
        print(f"[GEN] Starting generation for conv={conv_id} parent={parent_id} model={config.ollama_model}")
        await _ws_send(conv_id, {"type": "stream_start"})

        full_response = ""
        async for token in stream_chat(messages):
            if isinstance(token, dict):
                # Thinking status events
                await _ws_send(conv_id, token)
                continue
            full_response += token
            await _ws_send(conv_id, {
                "type": "stream_chunk",
                "content": token,
            })

        # Strip <think>...</think> blocks (thinking models like qwen3)
        import re as _re
        cleaned = _re.sub(r'<think>[\s\S]*?</think>\s*', '', full_response).strip()
        if cleaned:
            full_response = cleaned

        # If response is empty, send error instead of saving empty message
        if not full_response.strip():
            print(f"[WARN] Empty response. Raw length={len(full_response)} Cleaned length={len(cleaned)}")
            await _ws_send(conv_id, {
                "type": "error",
                "error": "Model returned an empty response — try again",
            })
            return

        # Save the assistant message (always, even if client disconnected)
        msg = await db.add_message(
            conv_id, "assistant", full_response, parent_id=parent_id
        )
        await db.set_active_branch(conv_id, msg["id"])
        # Background: generate Gemma summary for tree display
        asyncio.create_task(_background_summarize_message(msg["id"], full_response, "assistant",
                                                              conv_id=conv_id))

        await _ws_send(conv_id, {
            "type": "stream_end",
            "message": dict(msg),
        })

    except asyncio.CancelledError:
        await _ws_send(conv_id, {"type": "cancelled"})
    except Exception as e:
        print(f"[GEN] Weave generation error conv={conv_id}: {e}")
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _active_generations.pop(conv_id, None)


if __name__ == "__main__":
    import uvicorn
    ssl_kwargs = {}
    if os.path.exists(config.ssl_certfile) and os.path.exists(config.ssl_keyfile):
        ssl_kwargs["ssl_certfile"] = config.ssl_certfile
        ssl_kwargs["ssl_keyfile"] = config.ssl_keyfile
        print(f"[SSL] HTTPS enabled — cert={config.ssl_certfile}")
    else:
        print("[SSL] No certs found — running plain HTTP")
    uvicorn.run(app, host=config.host, port=config.port, **ssl_kwargs)
