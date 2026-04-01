"""SQLite database with tree-structured message storage."""

import aiosqlite
import json
import time
from typing import Optional

import os as _os
from config import config as _config
DB_PATH = _config.db_path

_db: aiosqlite.Connection | None = None

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

CREATE TABLE IF NOT EXISTS state_schemas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    fields TEXT NOT NULL,
    is_builtin INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    schema_id TEXT NOT NULL,
    label TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    is_readonly INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    UNIQUE(conversation_id, schema_id, label)
);
CREATE INDEX IF NOT EXISTS idx_state_cards_conv ON state_cards(conversation_id);

CREATE TABLE IF NOT EXISTS character_state_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    label TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    is_readonly INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    UNIQUE(character_id, schema_id, label)
);
CREATE INDEX IF NOT EXISTS idx_char_state_cards ON character_state_cards(character_id);
"""


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        print("[DB] Opening new shared connection")
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _db.execute("PRAGMA busy_timeout=30000")
    else:
        try:
            await _db.execute("SELECT 1")
        except (ValueError, Exception) as e:
            print(f"[DB] Shared connection dead ({e}), reconnecting...")
            try:
                await _db.close()
            except Exception:
                pass
            _db = await aiosqlite.connect(DB_PATH)
            _db.row_factory = aiosqlite.Row
            await _db.execute("PRAGMA journal_mode=WAL")
            await _db.execute("PRAGMA foreign_keys=ON")
            await _db.execute("PRAGMA busy_timeout=30000")
            print("[DB] Reconnected successfully")
    return _db


async def close_db():
    global _db
    if _db:
        print("[DB] Checkpointing WAL before close...")
        try:
            await _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            print(f"[DB] WAL checkpoint failed: {e}")
        print("[DB] Closing shared connection")
        await _db.close()
        _db = None


async def init_db():
    db = await get_db()
    await db.executescript(SCHEMA)
    await _run_migrations(db)
    await db.commit()



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
        # OODA harness toggle
        "ALTER TABLE conversations ADD COLUMN ooda_enabled INTEGER DEFAULT 0",
        # Tier 3: branch-level state deltas stored per assistant message
        "ALTER TABLE messages ADD COLUMN state_deltas TEXT",
        # Track which model generated each message (for provider switch detection)
        "ALTER TABLE messages ADD COLUMN cc_model_used TEXT",
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
        """CREATE TABLE IF NOT EXISTS state_schemas (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            fields TEXT NOT NULL,
            is_builtin INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS state_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            schema_id TEXT NOT NULL,
            label TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            is_readonly INTEGER DEFAULT 0,
            updated_at REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            UNIQUE(conversation_id, schema_id, label)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_state_cards_conv ON state_cards(conversation_id)",
        """CREATE TABLE IF NOT EXISTS character_state_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT NOT NULL,
            schema_id TEXT NOT NULL,
            label TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            is_readonly INTEGER DEFAULT 0,
            updated_at REAL NOT NULL,
            UNIQUE(character_id, schema_id, label)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_char_state_cards ON character_state_cards(character_id)",
        # Modules: pluggable skills, commands, and agent definitions
        """CREATE TABLE IF NOT EXISTS modules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('skill','command','agent')),
            description TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'builtin',
            config TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            discovered_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
    ]
    for sql in table_migrations:
        await db.execute(sql)

    # Seed builtin state schemas
    await _seed_builtin_schemas(db)


BUILTIN_SCHEMAS = [
    {
        "id": "character_state",
        "name": "Character State",
        "fields": json.dumps([
            {"name": "personality", "type": "text", "description": "Core personality traits"},
            {"name": "appearance", "type": "text", "description": "Physical appearance"},
            {"name": "current_mood", "type": "text", "description": "Current emotional state"},
            {"name": "goals", "type": "text", "description": "Active goals and motivations"},
            {"name": "relationships", "type": "text", "description": "Key relationships"},
            {"name": "physical_situation", "type": "text", "description": "Current physical state and location"},
        ]),
    },
    {
        "id": "scene_state",
        "name": "Scene State",
        "fields": json.dumps([
            {"name": "location", "type": "text", "description": "Current location"},
            {"name": "time_of_day", "type": "text", "description": "Time of day"},
            {"name": "atmosphere", "type": "text", "description": "Mood and atmosphere"},
            {"name": "present_characters", "type": "text", "description": "Characters in the scene"},
            {"name": "recent_events", "type": "text", "description": "What just happened"},
        ]),
    },
    {
        "id": "lore",
        "name": "Lore",
        "fields": json.dumps([
            {"name": "content", "type": "text", "description": "Background information"},
        ]),
    },
    {
        "id": "persona_state",
        "name": "Persona State",
        "fields": json.dumps([
            {"name": "description", "type": "text", "description": "Who you are in this RP"},
            {"name": "appearance", "type": "text", "description": "Your physical appearance"},
            {"name": "goals", "type": "text", "description": "Your active goals"},
        ]),
    },
]


async def _seed_builtin_schemas(db):
    """Insert builtin state schemas if they don't exist."""
    now = time.time()
    for schema in BUILTIN_SCHEMAS:
        await db.execute(
            "INSERT OR IGNORE INTO state_schemas (id, name, fields, is_builtin, created_at) VALUES (?, ?, ?, 1, ?)",
            (schema["id"], schema["name"], schema["fields"], now)
        )


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

    return dict(row[0])


async def list_conversations() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations ORDER BY starred DESC, updated_at DESC"
    )

    return [dict(r) for r in rows]


async def search_conversations(query: str, limit: int = 20) -> list[dict]:
    """Search across all conversations by title and message content."""
    db = await get_db()
    pattern = f"%{query}%"
    rows = await db.execute_fetchall(
        """SELECT c.id as conversation_id, c.title, c.mode,
                  m.id as message_id, m.role,
                  substr(m.content,
                         max(1, instr(lower(m.content), lower(?)) - 80),
                         200) as snippet
           FROM messages m
           JOIN conversations c ON c.id = m.conversation_id
           WHERE m.content LIKE ?
           UNION
           SELECT c.id as conversation_id, c.title, c.mode,
                  NULL as message_id, NULL as role,
                  c.title as snippet
           FROM conversations c
           WHERE c.title LIKE ? AND c.id NOT IN (
               SELECT DISTINCT m2.conversation_id FROM messages m2
               WHERE m2.content LIKE ?
           )
           ORDER BY conversation_id DESC
           LIMIT ?""",
        (query, pattern, pattern, pattern, limit)
    )
    return [dict(r) for r in rows]


async def search_conversation_messages(conv_id: int, query: str) -> list[dict]:
    """Search messages within a single conversation."""
    db = await get_db()
    pattern = f"%{query}%"
    rows = await db.execute_fetchall(
        """SELECT id, role,
                  substr(content,
                         max(1, instr(lower(content), lower(?)) - 80),
                         200) as snippet
           FROM messages
           WHERE conversation_id = ? AND content LIKE ?
           ORDER BY created_at""",
        (query, conv_id, pattern)
    )
    return [dict(r) for r in rows]


async def get_conversation(conv_id: int) -> Optional[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    )

    return dict(rows[0]) if rows else None


async def delete_conversation(conv_id: int):
    db = await get_db()
    await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    await db.commit()



async def touch_conversation(conv_id: int):
    db = await get_db()
    await db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (time.time(), conv_id)
    )
    await db.commit()



async def save_custom_scene(conv_id: int, scene: str):
    db = await get_db()
    await db.execute(
        "UPDATE conversations SET custom_scene = ? WHERE id = ?",
        (scene, conv_id)
    )
    await db.commit()



async def update_conversation_fields(conv_id: int, **fields):
    """Update arbitrary fields on a conversation."""
    db = await get_db()
    allowed = {"persona_id", "lore_ids", "style_nudge", "custom_scene", "title",
                "claude_session_id", "total_cost_usd", "cc_model", "cc_effort",
                "starred", "folder", "local_model", "cc_permission_mode",
                "ooda_enabled"}
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

    return dict(row[0])


async def update_message_content(msg_id: int, content: str = None,
                                  content_blocks: str = None,
                                  turn_cost_usd: float = None,
                                  turn_input_tokens: int = None,
                                  turn_output_tokens: int = None,
                                  cc_session_id: str = None,
                                  cc_model_used: str = None):
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
    if cc_model_used is not None:
        updates.append("cc_model_used = ?")
        params.append(cc_model_used)
    if updates:
        params.append(msg_id)
        await db.execute(
            f"UPDATE messages SET {', '.join(updates)} WHERE id = ?",
            params
        )
        await db.commit()



async def get_message(msg_id: int) -> Optional[dict]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (msg_id,))

    return dict(rows[0]) if rows else None


async def update_message_summary(msg_id: int, summary: str):
    db = await get_db()
    await db.execute(
        "UPDATE messages SET summary = ? WHERE id = ?",
        (summary, msg_id)
    )
    await db.commit()



async def update_message_image_alt(msg_id: int, image_alt: str):
    db = await get_db()
    await db.execute(
        "UPDATE messages SET image_alt = ? WHERE id = ?",
        (image_alt, msg_id)
    )
    await db.commit()



async def get_children(msg_id: int) -> list[dict]:
    """Get all direct children of a message."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE parent_id = ? ORDER BY created_at",
        (msg_id,)
    )

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



async def get_active_branch(conv_id: int) -> list[dict]:
    """Get the currently active branch messages in order."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT * FROM messages
           WHERE conversation_id = ? AND is_active = 1
           ORDER BY created_at""",
        (conv_id,)
    )

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
    
        return {"deleted": 0}

    conv_id = rows[0]["conversation_id"]
    parent_id = rows[0]["parent_id"]

    # Delete all collected IDs
    placeholders = ",".join("?" * len(to_delete))
    await db.execute(
        f"DELETE FROM messages WHERE id IN ({placeholders})", to_delete
    )
    await db.commit()


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



# ── State Cards ──

async def get_state_schemas() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM state_schemas ORDER BY is_builtin DESC, name")

    return [dict(r) for r in rows]


async def get_state_cards(conv_id: int, schema_id: str = None) -> list[dict]:
    db = await get_db()
    if schema_id:
        rows = await db.execute_fetchall(
            "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? ORDER BY label",
            (conv_id, schema_id)
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM state_cards WHERE conversation_id = ? ORDER BY schema_id, label",
            (conv_id,)
        )

    return [dict(r) for r in rows]


async def get_state_card_by_label(conv_id: int, schema_id: str, label: str):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? AND label = ?",
        (conv_id, schema_id, label)
    )

    return dict(rows[0]) if rows else None


async def create_state_card(conv_id: int, schema_id: str, label: str,
                            data: dict = None, is_readonly: bool = False) -> dict:
    db = await get_db()
    now = time.time()
    data_json = json.dumps(data or {})
    await db.execute(
        "INSERT OR IGNORE INTO state_cards (conversation_id, schema_id, label, data, is_readonly, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, schema_id, label, data_json, int(is_readonly), now)
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? AND label = ?",
        (conv_id, schema_id, label)
    )

    return dict(rows[0]) if rows else {}


async def update_state_card(card_id: int, data: dict) -> dict:
    db = await get_db()
    now = time.time()
    # Merge: load existing data, update fields
    rows = await db.execute_fetchall("SELECT * FROM state_cards WHERE id = ?", (card_id,))
    if not rows:
    
        return {}
    existing = json.loads(rows[0]["data"] or "{}")
    existing.update(data)
    await db.execute(
        "UPDATE state_cards SET data = ?, updated_at = ? WHERE id = ?",
        (json.dumps(existing), now, card_id)
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM state_cards WHERE id = ?", (card_id,))

    return dict(rows[0]) if rows else {}


async def update_state_card_field(conv_id: int, schema_id: str, label: str,
                                  field: str, value: str) -> dict:
    """Update a single field on a state card, creating the card if it doesn't exist."""
    db = await get_db()
    now = time.time()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? AND label = ?",
        (conv_id, schema_id, label)
    )
    if rows:
        existing = json.loads(rows[0]["data"] or "{}")
        existing[field] = value
        await db.execute(
            "UPDATE state_cards SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(existing), now, rows[0]["id"])
        )
    else:
        data = {field: value}
        await db.execute(
            "INSERT INTO state_cards (conversation_id, schema_id, label, data, is_readonly, updated_at) VALUES (?, ?, ?, ?, 0, ?)",
            (conv_id, schema_id, label, json.dumps(data), now)
        )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? AND label = ?",
        (conv_id, schema_id, label)
    )

    return dict(rows[0]) if rows else {}


async def delete_state_card(card_id: int):
    db = await get_db()
    await db.execute("DELETE FROM state_cards WHERE id = ?", (card_id,))
    await db.commit()



# ── Branch State (Tier 3 — Deltas per message) ──

async def save_state_deltas(msg_id: int, deltas: list[dict]):
    """Save state deltas on an assistant message.
    deltas: [{"schema_id": ..., "label": ..., "field": ..., "value": ...}, ...]
    """
    db = await get_db()
    await db.execute(
        "UPDATE messages SET state_deltas = ? WHERE id = ?",
        (json.dumps(deltas), msg_id)
    )
    await db.commit()



async def get_branch_state(conv_id: int, leaf_msg_id: int) -> list[dict]:
    """Reconstruct effective state for a branch by applying deltas along the path.

    Returns state cards with deltas applied: base conversation cards + all
    deltas from root to leaf_msg_id, in order.
    """
    # Get base conversation state cards
    base_cards = await get_state_cards(conv_id)

    # Get the branch path (root to leaf)
    branch = await get_branch_to_root(leaf_msg_id)

    # Build effective state: start from base, apply deltas in order
    # Index base cards by (schema_id, label)
    effective = {}
    for card in base_cards:
        data = json.loads(card["data"]) if isinstance(card["data"], str) else card["data"]
        effective[(card["schema_id"], card["label"])] = {**card, "data": data}

    # Walk branch in chronological order (branch_to_root returns root-first)
    for msg in branch:
        deltas_raw = msg.get("state_deltas")
        if not deltas_raw:
            continue
        try:
            deltas = json.loads(deltas_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for delta in deltas:
            key = (delta["schema_id"], delta["label"])
            if key not in effective:
                effective[key] = {
                    "schema_id": delta["schema_id"],
                    "label": delta["label"],
                    "data": {},
                }
            effective[key]["data"][delta["field"]] = delta["value"]

    return list(effective.values())


# ── Character State Cards (Tier 1 — Global) ──

async def get_character_state_cards(character_id: str, schema_id: str = None) -> list[dict]:
    db = await get_db()
    if schema_id:
        rows = await db.execute_fetchall(
            "SELECT * FROM character_state_cards WHERE character_id = ? AND schema_id = ? ORDER BY label",
            (character_id, schema_id)
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM character_state_cards WHERE character_id = ? ORDER BY schema_id, label",
            (character_id,)
        )

    return [dict(r) for r in rows]


async def create_character_state_card(character_id: str, schema_id: str, label: str,
                                      data: dict = None, is_readonly: bool = False) -> dict:
    db = await get_db()
    now = time.time()
    data_json = json.dumps(data or {})
    await db.execute(
        "INSERT OR IGNORE INTO character_state_cards (character_id, schema_id, label, data, is_readonly, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (character_id, schema_id, label, data_json, int(is_readonly), now)
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM character_state_cards WHERE character_id = ? AND schema_id = ? AND label = ?",
        (character_id, schema_id, label)
    )

    return dict(rows[0]) if rows else {}


async def update_character_state_card(card_id: int, data: dict) -> dict:
    db = await get_db()
    now = time.time()
    rows = await db.execute_fetchall("SELECT * FROM character_state_cards WHERE id = ?", (card_id,))
    if not rows:
    
        return {}
    existing = json.loads(rows[0]["data"] or "{}")
    existing.update(data)
    await db.execute(
        "UPDATE character_state_cards SET data = ?, updated_at = ? WHERE id = ?",
        (json.dumps(existing), now, card_id)
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM character_state_cards WHERE id = ?", (card_id,))

    return dict(rows[0]) if rows else {}


async def delete_character_state_card(card_id: int):
    db = await get_db()
    await db.execute("DELETE FROM character_state_cards WHERE id = ?", (card_id,))
    await db.commit()



async def copy_character_state_to_conversation(character_id: str, conv_id: int) -> int:
    """Copy all character-level state cards into conversation-level state cards. Returns count."""
    db = await get_db()
    now = time.time()
    rows = await db.execute_fetchall(
        "SELECT * FROM character_state_cards WHERE character_id = ?", (character_id,)
    )
    count = 0
    for row in rows:
        await db.execute(
            "INSERT OR IGNORE INTO state_cards (conversation_id, schema_id, label, data, is_readonly, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, row["schema_id"], row["label"], row["data"], row["is_readonly"], now)
        )
        count += 1
    await db.commit()

    return count


# ── Bookmarks ──

async def get_bookmarks(conv_id: int) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM bookmarks WHERE conversation_id = ? ORDER BY created_at DESC",
        (conv_id,)
    )

    return [dict(r) for r in rows]


async def get_all_bookmarks() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT b.*, c.title as conversation_title, c.mode as conversation_mode
           FROM bookmarks b
           JOIN conversations c ON b.conversation_id = c.id
           ORDER BY b.created_at DESC"""
    )

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

    return dict(rows[0]) if rows else {}


async def delete_bookmark(bookmark_id: int):
    db = await get_db()
    await db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
    await db.commit()



# ── Modules (Skills / Commands / Agents) ──

async def upsert_module(module_id: str, name: str, module_type: str,
                        description: str = "", source: str = "builtin",
                        config: dict = None) -> dict:
    """Insert or update a module. Returns the module dict."""
    import time
    db = await get_db()
    now = time.time()
    config_json = json.dumps(config or {})
    await db.execute(
        """INSERT INTO modules (id, name, type, description, source, config, enabled, discovered_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, description=excluded.description,
               config=excluded.config, updated_at=excluded.updated_at""",
        (module_id, name, module_type, description, source, config_json, now, now)
    )
    await db.commit()
    row = await db.execute_fetchall("SELECT * FROM modules WHERE id = ?", (module_id,))
    return dict(row[0]) if row else {}


async def get_modules(module_type: str = None, enabled_only: bool = True) -> list[dict]:
    db = await get_db()
    sql = "SELECT * FROM modules"
    params = []
    conditions = []
    if module_type:
        conditions.append("type = ?")
        params.append(module_type)
    if enabled_only:
        conditions.append("enabled = 1")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY name"
    rows = await db.execute_fetchall(sql, params)
    return [dict(r) for r in rows]


async def set_module_enabled(module_id: str, enabled: bool):
    import time
    db = await get_db()
    await db.execute(
        "UPDATE modules SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, time.time(), module_id)
    )
    await db.commit()


async def delete_module(module_id: str):
    db = await get_db()
    await db.execute("DELETE FROM modules WHERE id = ?", (module_id,))
    await db.commit()


# ── Helpers ──

async def count_conversation_tokens(conv_id: int) -> int:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT COALESCE(SUM(token_estimate), 0) as total FROM messages WHERE conversation_id = ? AND is_active = 1",
        (conv_id,)
    )

    return rows[0]["total"] if rows else 0
