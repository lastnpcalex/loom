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
    load_all_lore, load_lore_entry,
)
from ollama_client import health_check, stream_chat, describe_image
from prompt_engine import (
    RepetitionDetector, build_system_prompt, assemble_prompt,
    get_style_nudge, STYLE_NUDGES
)
from context_manager import get_context_for_generation, update_rolling_summary
import local_summary

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

app = FastAPI(title="Loom — RP Harness", lifespan=lifespan)

# Ensure upload directory exists
os.makedirs(config.upload_dir, exist_ok=True)

# Active WebSocket generation tasks (for cancellation)
_active_generations: dict[int, asyncio.Task] = {}


async def _background_summarize_message(msg_id: int, content: str, role: str,
                                         conv_id: int = None, image_path: str = None):
    """Generate a short Gemma summary for a message (tree pill) and update
    the rolling context summary if needed."""
    try:
        summary_content = content

        if image_path:
            image_alt = await describe_image(image_path)
            await db.update_message_image_alt(msg_id, image_alt)
            summary_content = f"{content}\n[Attached image: {image_alt}]"

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

    first_turn = data.get("first_turn", "character")  # "character" or "user"
    custom_scene = data.get("custom_scene")

    conv = await db.create_conversation(title, character_id)

    # Store additional fields
    import json as _json
    await db.update_conversation_fields(
        conv["id"],
        persona_id=persona_id,
        lore_ids=_json.dumps(lore_ids),
        style_nudge=style_nudge,
        custom_scene=custom_scene,
    )
    # Refresh conv data
    conv = await db.get_conversation(conv["id"])

    # If character goes first, add the greeting (unless custom scene is set — that needs generation)
    if first_turn == "character" and character_id:
        char = load_character(os.path.join(config.characters_dir, f"{character_id}.md"))
        if char and char.get("greeting") and not custom_scene:
            greeting_msg = await db.add_message(conv["id"], "assistant", char["greeting"])
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
    image_path = data.get("image_path")
    parent_id = data.get("parent_id")

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


# ── WebSocket Chat ──

@app.websocket("/ws/chat/{conv_id}")
async def ws_chat(websocket: WebSocket, conv_id: int):
    await websocket.accept()

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            if action == "cancel":
                task = _active_generations.pop(conv_id, None)
                if task:
                    task.cancel()
                continue

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
        _active_generations.pop(conv_id, None)
    except Exception as e:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({"type": "error", "error": str(e)})


async def _handle_generation(websocket: WebSocket, conv_id: int, data: dict):
    """Handle a generation request over WebSocket."""
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")

        # For regenerate, parent_id should be provided
        # For generate, use the current active leaf
        if action == "generate" and parent_id is None:
            leaf = await db.get_active_leaf(conv_id)
            parent_id = leaf["id"] if leaf else None

        # Load character
        conv = await db.get_conversation(conv_id)
        character = None
        if conv and conv.get("character_id"):
            character = load_character(
                os.path.join(config.characters_dir, f"{conv['character_id']}.md")
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
        context = await get_context_for_generation(conv_id, character)

        # Run repetition analysis on recent assistant messages
        assistant_msgs = [
            m["content"] for m in context["verbatim_messages"]
            if m["role"] == "assistant"
        ]
        detector = RepetitionDetector()
        rep_analysis = detector.analyze(assistant_msgs)

        rep_directives = [
            issue["directive"] for issue in rep_analysis.get("issues", [])
        ]

        # Build system prompt (use custom scene if set)
        custom_scene = conv.get("custom_scene") if conv else None
        system_prompt = build_system_prompt(
            character=character,
            style_nudge_index=nudge_index,
            repetition_directives=rep_directives,
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

        # Send context info
        active_nudge = get_style_nudge(nudge_index)
        await websocket.send_json({
            "type": "context_info",
            "total_tokens": context["total_tokens"],
            "was_compactified": context["was_compactified"],
            "style_nudge": active_nudge["name"],
            "repetition_alerts": len(rep_analysis.get("issues", [])),
        })

        # Stream the response
        await websocket.send_json({"type": "stream_start"})

        full_response = ""
        async for token in stream_chat(messages):
            full_response += token
            await websocket.send_json({
                "type": "stream_chunk",
                "content": token,
            })

        # Save the assistant message
        msg = await db.add_message(
            conv_id, "assistant", full_response, parent_id=parent_id
        )
        await db.set_active_branch(conv_id, msg["id"])
        # Background: generate Gemma summary for tree display
        asyncio.create_task(_background_summarize_message(msg["id"], full_response, "assistant",
                                                              conv_id=conv_id))

        # Update style state (repetition tracking only, no auto-rotation)
        await db.update_style_state(
            conv_id,
            alert_level=rep_analysis.get("alert_level", 0),
        )

        await websocket.send_json({
            "type": "stream_end",
            "message": dict(msg),
        })

    except asyncio.CancelledError:
        await websocket.send_json({"type": "cancelled"})
    except Exception as e:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({"type": "error", "error": str(e)})
    finally:
        _active_generations.pop(conv_id, None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.host, port=config.port)
