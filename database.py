"""SQLite database with tree-structured message storage."""

import aiosqlite
import json
import time
from typing import Optional

DB_PATH = "loom.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'New Conversation',
    character_id TEXT,
    persona_id TEXT,
    lore_ids TEXT NOT NULL DEFAULT '[]',
    style_nudge TEXT NOT NULL DEFAULT 'Natural',
    custom_scene TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    parent_id INTEGER,
    role TEXT NOT NULL CHECK(role IN ('system','user','assistant')),
    content TEXT NOT NULL,
    image_path TEXT,
    image_alt TEXT,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    summary TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES messages(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    branch_path TEXT NOT NULL DEFAULT '[]',
    content TEXT NOT NULL,
    covers_up_to INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS style_state (
    conversation_id INTEGER PRIMARY KEY,
    current_nudge_index INTEGER NOT NULL DEFAULT 0,
    repetition_alert_level INTEGER NOT NULL DEFAULT 0,
    last_ngram_snapshot TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_summaries_conv ON summaries(conversation_id);
"""


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    await db.executescript(SCHEMA)
    await db.commit()
    await db.close()


# ── Conversations ──

async def create_conversation(title: str, character_id: str = None) -> dict:
    db = await get_db()
    now = time.time()
    cursor = await db.execute(
        "INSERT INTO conversations (title, character_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (title, character_id, now, now)
    )
    conv_id = cursor.lastrowid
    # Init style state
    await db.execute(
        "INSERT INTO style_state (conversation_id) VALUES (?)", (conv_id,)
    )
    await db.commit()
    row = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    )
    await db.close()
    return dict(row[0])


async def list_conversations() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations ORDER BY updated_at DESC"
    )
    await db.close()
    return [dict(r) for r in rows]


async def get_conversation(conv_id: int) -> Optional[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    )
    await db.close()
    return dict(rows[0]) if rows else None


async def delete_conversation(conv_id: int):
    db = await get_db()
    await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    await db.commit()
    await db.close()


async def touch_conversation(conv_id: int):
    db = await get_db()
    await db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (time.time(), conv_id)
    )
    await db.commit()
    await db.close()


async def save_custom_scene(conv_id: int, scene: str):
    db = await get_db()
    await db.execute(
        "UPDATE conversations SET custom_scene = ? WHERE id = ?",
        (scene, conv_id)
    )
    await db.commit()
    await db.close()


async def update_conversation_fields(conv_id: int, **fields):
    """Update arbitrary fields on a conversation."""
    db = await get_db()
    allowed = {"persona_id", "lore_ids", "style_nudge", "custom_scene", "title"}
    updates = []
    params = []
    for key, val in fields.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            params.append(val)
    if updates:
        params.append(conv_id)
        await db.execute(
            f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?",
            params
        )
        await db.commit()
    await db.close()


# ── Messages ──

async def add_message(conversation_id: int, role: str, content: str,
                      parent_id: int = None, image_path: str = None,
                      is_active: bool = True) -> dict:
    db = await get_db()
    token_est = len(content) // 3
    now = time.time()
    cursor = await db.execute(
        """INSERT INTO messages
           (conversation_id, parent_id, role, content, image_path, token_estimate, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (conversation_id, parent_id, role, content, image_path, token_est, int(is_active), now)
    )
    msg_id = cursor.lastrowid
    await db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (now, conversation_id)
    )
    await db.commit()
    row = await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (msg_id,))
    await db.close()
    return dict(row[0])


async def get_message(msg_id: int) -> Optional[dict]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (msg_id,))
    await db.close()
    return dict(rows[0]) if rows else None


async def update_message_content(msg_id: int, content: str):
    db = await get_db()
    token_est = len(content) // 3
    await db.execute(
        "UPDATE messages SET content = ?, token_estimate = ? WHERE id = ?",
        (content, token_est, msg_id)
    )
    await db.commit()
    await db.close()


async def update_message_summary(msg_id: int, summary: str):
    db = await get_db()
    await db.execute(
        "UPDATE messages SET summary = ? WHERE id = ?",
        (summary, msg_id)
    )
    await db.commit()
    await db.close()


async def update_message_image_alt(msg_id: int, image_alt: str):
    db = await get_db()
    await db.execute(
        "UPDATE messages SET image_alt = ? WHERE id = ?",
        (image_alt, msg_id)
    )
    await db.commit()
    await db.close()


async def get_children(msg_id: int) -> list[dict]:
    """Get all direct children of a message."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE parent_id = ? ORDER BY created_at",
        (msg_id,)
    )
    await db.close()
    return [dict(r) for r in rows]


async def get_siblings(msg_id: int) -> list[dict]:
    """Get all siblings of a message (same parent), ordered by creation time."""
    msg = await get_message(msg_id)
    if not msg:
        return []
    db = await get_db()
    if msg["parent_id"] is None:
        rows = await db.execute_fetchall(
            """SELECT * FROM messages
               WHERE conversation_id = ? AND parent_id IS NULL AND role = ?
               ORDER BY created_at""",
            (msg["conversation_id"], msg["role"])
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE parent_id = ? ORDER BY created_at",
            (msg["parent_id"],)
        )
    await db.close()
    return [dict(r) for r in rows]


async def get_branch_to_root(msg_id: int) -> list[dict]:
    """Walk from a message up to the root, return list root->leaf order."""
    chain = []
    db = await get_db()
    current_id = msg_id
    while current_id is not None:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE id = ?", (current_id,)
        )
        if not rows:
            break
        msg = dict(rows[0])
        chain.append(msg)
        current_id = msg["parent_id"]
    await db.close()
    chain.reverse()
    return chain


async def set_active_branch(conv_id: int, leaf_id: int):
    """Mark a branch as active: deactivate all, then activate root->leaf path."""
    db = await get_db()
    await db.execute(
        "UPDATE messages SET is_active = 0 WHERE conversation_id = ?", (conv_id,)
    )
    # Walk up to root
    current_id = leaf_id
    ids_to_activate = []
    while current_id is not None:
        rows = await db.execute_fetchall(
            "SELECT id, parent_id FROM messages WHERE id = ?", (current_id,)
        )
        if not rows:
            break
        ids_to_activate.append(rows[0]["id"])
        current_id = rows[0]["parent_id"]
    if ids_to_activate:
        placeholders = ",".join("?" * len(ids_to_activate))
        await db.execute(
            f"UPDATE messages SET is_active = 1 WHERE id IN ({placeholders})",
            ids_to_activate
        )
    await db.commit()
    await db.close()


async def get_active_branch(conv_id: int) -> list[dict]:
    """Get the currently active branch messages in order."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT * FROM messages
           WHERE conversation_id = ? AND is_active = 1
           ORDER BY created_at""",
        (conv_id,)
    )
    await db.close()
    return [dict(r) for r in rows]


async def get_active_leaf(conv_id: int) -> Optional[dict]:
    """Get the leaf message of the active branch."""
    db = await get_db()
    # The leaf is an active message with no active children
    rows = await db.execute_fetchall(
        """SELECT m.* FROM messages m
           WHERE m.conversation_id = ? AND m.is_active = 1
           AND NOT EXISTS (
               SELECT 1 FROM messages c WHERE c.parent_id = m.id AND c.is_active = 1
           )
           ORDER BY m.created_at DESC LIMIT 1""",
        (conv_id,)
    )
    await db.close()
    return dict(rows[0]) if rows else None


async def delete_branch(msg_id: int) -> dict:
    """Delete a message and its entire subtree. Returns info about what was deleted."""
    db = await get_db()
    # Collect all descendant IDs via BFS
    to_delete = []
    queue = [msg_id]
    while queue:
        current = queue.pop(0)
        to_delete.append(current)
        children = await db.execute_fetchall(
            "SELECT id FROM messages WHERE parent_id = ?", (current,)
        )
        for child in children:
            queue.append(child["id"])

    # Get the message's parent and conversation before deleting
    rows = await db.execute_fetchall(
        "SELECT conversation_id, parent_id FROM messages WHERE id = ?", (msg_id,)
    )
    if not rows:
        await db.close()
        return {"deleted": 0}

    conv_id = rows[0]["conversation_id"]
    parent_id = rows[0]["parent_id"]

    # Delete all collected IDs
    placeholders = ",".join("?" * len(to_delete))
    await db.execute(
        f"DELETE FROM messages WHERE id IN ({placeholders})", to_delete
    )
    await db.commit()
    await db.close()

    return {
        "deleted": len(to_delete),
        "conversation_id": conv_id,
        "parent_id": parent_id,
    }


async def get_conversation_tree(conv_id: int) -> list[dict]:
    """Get all messages for a conversation (for tree visualization)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, parent_id, role, substr(content, 1, 200) as preview,
                  is_active, created_at, token_estimate, summary, image_path, image_alt
           FROM messages WHERE conversation_id = ?
           ORDER BY created_at""",
        (conv_id,)
    )
    await db.close()
    return [dict(r) for r in rows]


# ── Summaries ──

async def save_summary(conv_id: int, branch_path: list[int], content: str,
                       covers_up_to: int) -> dict:
    db = await get_db()
    now = time.time()
    token_est = len(content) // 3
    # Upsert: replace existing summary for this branch path
    path_json = json.dumps(branch_path)
    await db.execute(
        """INSERT OR REPLACE INTO summaries
           (conversation_id, branch_path, content, covers_up_to, token_estimate, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (conv_id, path_json, content, covers_up_to, token_est, now)
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM summaries WHERE conversation_id = ? AND branch_path = ?",
        (conv_id, path_json)
    )
    await db.close()
    return dict(rows[0]) if rows else {}


async def get_summary(conv_id: int, branch_path: list[int] = None) -> Optional[dict]:
    db = await get_db()
    path_json = json.dumps(branch_path or [])
    rows = await db.execute_fetchall(
        """SELECT * FROM summaries
           WHERE conversation_id = ? AND branch_path = ?
           ORDER BY created_at DESC LIMIT 1""",
        (conv_id, path_json)
    )
    await db.close()
    return dict(rows[0]) if rows else None


# ── Style State ──

async def get_style_state(conv_id: int) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM style_state WHERE conversation_id = ?", (conv_id,)
    )
    if not rows:
        await db.execute(
            "INSERT INTO style_state (conversation_id) VALUES (?)", (conv_id,)
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM style_state WHERE conversation_id = ?", (conv_id,)
        )
    await db.close()
    return dict(rows[0])


async def update_style_state(conv_id: int, nudge_index: int = None,
                             alert_level: int = None, ngram_snapshot: dict = None):
    db = await get_db()
    updates = []
    params = []
    if nudge_index is not None:
        updates.append("current_nudge_index = ?")
        params.append(nudge_index)
    if alert_level is not None:
        updates.append("repetition_alert_level = ?")
        params.append(alert_level)
    if ngram_snapshot is not None:
        updates.append("last_ngram_snapshot = ?")
        params.append(json.dumps(ngram_snapshot))
    if updates:
        params.append(conv_id)
        await db.execute(
            f"UPDATE style_state SET {', '.join(updates)} WHERE conversation_id = ?",
            params
        )
        await db.commit()
    await db.close()


# ── Helpers ──

async def count_conversation_tokens(conv_id: int) -> int:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT COALESCE(SUM(token_estimate), 0) as total FROM messages WHERE conversation_id = ? AND is_active = 1",
        (conv_id,)
    )
    await db.close()
    return rows[0]["total"] if rows else 0
