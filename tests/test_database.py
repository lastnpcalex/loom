"""Tests for the database layer."""

import pytest
import aiosqlite
import database as db


async def test_init_db():
    """Schema + migrations run without error on a fresh database."""
    # init_db already ran via autouse fixture; verify tables exist
    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    await conn.close()
    table_names = {r["name"] for r in rows}
    assert "conversations" in table_names
    assert "messages" in table_names
    assert "summaries" in table_names
    assert "style_state" in table_names


async def test_local_model_migration():
    """The local_model column exists on conversations after migration."""
    conn = await db.get_db()
    cursor = await conn.execute("PRAGMA table_info(conversations)")
    columns = await cursor.fetchall()
    await conn.close()
    col_names = [c[1] for c in columns]
    assert "local_model" in col_names


async def test_create_conversation_default_mode():
    """Creating a conversation without explicit mode defaults to 'weave'."""
    conv = await db.create_conversation("Test Chat")
    assert conv["mode"] == "weave"
    assert conv["title"] == "Test Chat"
    assert conv["id"] is not None


async def test_update_conversation_local_model():
    """update_conversation_fields accepts and persists local_model."""
    conv = await db.create_conversation("Local Test", mode="local")
    await db.update_conversation_fields(conv["id"], local_model="qwen3:4b")
    updated = await db.get_conversation(conv["id"])
    assert updated["local_model"] == "qwen3:4b"


async def test_add_message_and_branch():
    """Add messages in a chain and verify branch walk returns root->leaf order."""
    conv = await db.create_conversation("Branch Test")
    m1 = await db.add_message(conv["id"], "user", "Hello")
    m2 = await db.add_message(conv["id"], "assistant", "Hi there", parent_id=m1["id"])
    m3 = await db.add_message(conv["id"], "user", "How are you?", parent_id=m2["id"])

    branch = await db.get_branch_to_root(m3["id"])
    assert len(branch) == 3
    assert branch[0]["id"] == m1["id"]
    assert branch[1]["id"] == m2["id"]
    assert branch[2]["id"] == m3["id"]


async def test_set_active_branch():
    """Activating a branch marks only path messages as active."""
    conv = await db.create_conversation("Active Test")
    m1 = await db.add_message(conv["id"], "user", "Root")
    m2a = await db.add_message(conv["id"], "assistant", "Branch A", parent_id=m1["id"])
    m2b = await db.add_message(conv["id"], "assistant", "Branch B", parent_id=m1["id"])

    await db.set_active_branch(conv["id"], m2b["id"])

    active = await db.get_active_branch(conv["id"])
    active_ids = {m["id"] for m in active}
    assert m1["id"] in active_ids
    assert m2b["id"] in active_ids
    assert m2a["id"] not in active_ids


async def test_get_active_leaf():
    """get_active_leaf returns the deepest active message."""
    conv = await db.create_conversation("Leaf Test")
    m1 = await db.add_message(conv["id"], "user", "Start")
    m2 = await db.add_message(conv["id"], "assistant", "Reply", parent_id=m1["id"])
    await db.set_active_branch(conv["id"], m2["id"])

    leaf = await db.get_active_leaf(conv["id"])
    assert leaf is not None
    assert leaf["id"] == m2["id"]


async def test_list_conversations():
    """list_conversations returns all conversations."""
    await db.create_conversation("First")
    await db.create_conversation("Second")
    await db.create_conversation("Third")

    convs = await db.list_conversations()
    titles = [c["title"] for c in convs]
    assert "First" in titles
    assert "Second" in titles
    assert "Third" in titles
    assert len(convs) >= 3
