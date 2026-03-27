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

CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    branch_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    UNIQUE(conversation_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_conv ON bookmarks(conversation_id);
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
    await _run_migrations(db)
    await db.commit()
    await db.close()


async def _run_migrations(db):
    """Add new columns idempotently via ALTER TABLE."""
    migrations = [
        # Conversations: Claude mode fields
        "ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'weave'",
        "ALTER TABLE conversations ADD COLUMN project_dir TEXT",
        "ALTER TABLE conversations ADD COLUMN claude_session_id TEXT",
        "ALTER TABLE conversations ADD COLUMN total_cost_usd REAL DEFAULT 0",
        # Messages: Claude content blocks + cost tracking
        "ALTER TABLE messages ADD COLUMN content_blocks TEXT",
        "ALTER TABLE messages ADD COLUMN turn_cost_usd REAL",
        "ALTER TABLE messages ADD COLUMN turn_input_tokens INTEGER",
        "ALTER TABLE messages ADD COLUMN turn_output_tokens INTEGER",
        # Phase 2: model & effort selection
        "ALTER TABLE conversations ADD COLUMN cc_model TEXT DEFAULT 'sonnet'",
        "ALTER TABLE conversations ADD COLUMN cc_effort TEXT DEFAULT 'high'",
        # Phase 3: starred & folders
        "ALTER TABLE conversations ADD COLUMN starred INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN folder TEXT DEFAULT ''",
        # Phase 4: session resume — store CC session_id per message node
        "ALTER TABLE messages ADD COLUMN cc_session_id TEXT",
        # Local mode: store selected Ollama model per conversation
        "ALTER TABLE conversations ADD COLUMN local_model TEXT",
        # Permission mode (default, plan, auto, etc.)
        "ALTER TABLE conversations ADD COLUMN cc_permission_mode TEXT DEFAULT 'default'",
        # Bookmarked message — auto-navigate to this on conversation load (deprecated)
        "ALTER TABLE conversations ADD COLUMN bookmark_msg_id INTEGER",
    ]
    for sql in migrations:
        try:
            await db.execute(sql)
        except Exception:
            pass  # Column already exists

    # Table-level migrations (CREATE IF NOT EXISTS is idempotent)
    table_migrations = [
        """CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            branch_name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
            UNIQUE(conversation_id, message_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bookmarks_conv ON bookmarks(conversation_id)",
    ]
    for sql in table_migrations:
        await db.execute(sql)


# ── Conversations ──

async def create_conversation(title: str, character_id: str = None,
                              mode: str = "weave", project_dir: str = None) -> dict:
    db = await get_db()
    now = time.time()
    cursor = await db.execute(
        "INSERT INTO conversations (title, character_id, mode, project_dir, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (title, character_id, mode, project_dir, now, now)
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
        "SELECT * FROM conversations ORDER BY starred DESC, updated_at DESC"
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
    allowed = {"persona_id", "lore_ids", "style_nudge", "custom_scene", "title",
                "claude_session_id", "total_cost_usd", "cc_model", "cc_effort",
                "starred", "folder", "local_model", "cc_permission_mode"}
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
                      is_active: bool = True, content_blocks: str = None,
                      turn_cost_usd: float = None, turn_input_tokens: int = None,
                      turn_output_tokens: int = None,
                      cc_session_id: str = None) -> dict:
    db = await get_db()
    token_est = len(content) // 3
    now = time.time()
    cursor = await db.execute(
        """INSERT INTO messages
           (conversation_id, parent_id, role, content, image_path, token_estimate,
            is_active, content_blocks, turn_cost_usd, turn_input_tokens, turn_output_tokens,
            cc_session_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (conversation_id, parent_id, role, content, image_path, token_est,
         int(is_active), content_blocks, turn_cost_usd, turn_input_tokens, turn_output_tokens,
         cc_session_id, now)
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


async def update_message_content(msg_id: int, content: str = None,
                                  content_blocks: str = None,
                                  turn_cost_usd: float = None,
                                  turn_input_tokens: int = None,
                                  turn_output_tokens: int = None,
                                  cc_session_id: str = None):
    """Update a message's content and metadata (used for draft → final)."""
    db = await get_db()
    updates = []
    params = []
    if content is not None:
        updates.append("content = ?")
        params.append(content)
        updates.append("token_estimate = ?")
        params.append(len(content) // 3)
    if content_blocks is not None:
        updates.append("content_blocks = ?")
        params.append(content_blocks)
    if turn_cost_usd is not None:
        updates.append("turn_cost_usd = ?")
        params.append(turn_cost_usd)
    if turn_input_tokens is not None:
        updates.append("turn_input_tokens = ?")
        params.append(turn_input_tokens)
    if turn_output_tokens is not None:
        updates.append("turn_output_tokens = ?")
        params.append(turn_output_tokens)
    if cc_session_id is not None:
        updates.append("cc_session_id = ?")
        params.append(cc_session_id)
    if updates:
        params.append(msg_id)
        await db.execute(
            f"UPDATE messages SET {', '.join(updates)} WHERE id = ?",
            params
        )
        await db.commit()
    await db.close()


async def get_message(msg_id: int) -> Optional[dict]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (msg_id,))
    await db.close()
    return dict(rows[0]) if rows else None


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


async def fork_conversation(conv_id: int, from_msg_id: int, new_title: str = None) -> dict:
    """Fork a conversation: create a new conversation with messages up to from_msg_id."""
    db = await get_db()

    # Get original conversation
    rows = await db.execute_fetchall("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not rows:
        await db.close()
        return None
    orig = dict(rows[0])

    # Walk from from_msg_id up to root to get the branch to copy
    branch = []
    current_id = from_msg_id
    while current_id is not None:
        msg_rows = await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (current_id,))
        if not msg_rows:
            break
        msg = dict(msg_rows[0])
        branch.insert(0, msg)
        current_id = msg["parent_id"]

    # Create new conversation
    title = new_title or f"{orig['title']} (fork)"
    now = time.time()
    cursor = await db.execute(
        """INSERT INTO conversations (title, character_id, persona_id, lore_ids,
           style_nudge, custom_scene, mode, project_dir, cc_model, cc_effort,
           local_model, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, orig.get("character_id"), orig.get("persona_id"), orig.get("lore_ids"),
         orig.get("style_nudge"), orig.get("custom_scene"),
         orig.get("mode", "weave"), orig.get("project_dir"),
         orig.get("cc_model"), orig.get("cc_effort"), orig.get("local_model"),
         now, now)
    )
    new_conv_id = cursor.lastrowid
    await db.execute("INSERT INTO style_state (conversation_id) VALUES (?)", (new_conv_id,))

    # Copy messages, mapping old IDs to new IDs
    id_map = {}
    for msg in branch:
        new_parent = id_map.get(msg["parent_id"]) if msg["parent_id"] else None
        cursor = await db.execute(
            """INSERT INTO messages (conversation_id, parent_id, role, content,
               token_estimate, is_active, summary, image_path, image_alt,
               cc_session_id, content_blocks, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
            (new_conv_id, new_parent, msg["role"], msg["content"],
             msg.get("token_estimate", 0), msg.get("summary"),
             msg.get("image_path"), msg.get("image_alt"),
             msg.get("cc_session_id"), msg.get("content_blocks"),
             msg["created_at"])
        )
        id_map[msg["id"]] = cursor.lastrowid

    await db.commit()
    row = await db.execute_fetchall("SELECT * FROM conversations WHERE id = ?", (new_conv_id,))
    await db.close()
    return dict(row[0])


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


# ── Bookmarks ──

async def get_bookmarks(conv_id: int) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM bookmarks WHERE conversation_id = ? ORDER BY created_at DESC",
        (conv_id,)
    )
    await db.close()
    return [dict(r) for r in rows]


async def add_bookmark(conv_id: int, message_id: int,
                       branch_name: str = '', description: str = '') -> dict:
    db = await get_db()
    now = time.time()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO bookmarks (conversation_id, message_id, branch_name, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (conv_id, message_id, branch_name, description, now)
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM bookmarks WHERE conversation_id = ? AND message_id = ?",
        (conv_id, message_id)
    )
    await db.close()
    return dict(rows[0]) if rows else {}


async def update_bookmark(bookmark_id: int, description: str) -> dict:
    db = await get_db()
    await db.execute(
        "UPDATE bookmarks SET description = ? WHERE id = ?",
        (description, bookmark_id)
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,)
    )
    await db.close()
    return dict(rows[0]) if rows else {}


async def delete_bookmark(bookmark_id: int):
    db = await get_db()
    await db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
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
