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


async def test_update_conversation_mode():
    """update_conversation_fields accepts and persists mode changes."""
    conv = await db.create_conversation("Mode Test", mode="claude")
    await db.update_conversation_fields(conv["id"], mode="openrouter")
    updated = await db.get_conversation(conv["id"])
    assert updated["mode"] == "openrouter"


async def test_update_conversation_system_only_weave_fields():
    """Minimal Weave fields persist on conversations."""
    conv = await db.create_conversation("Minimal Weave", mode="weave")
    await db.update_conversation_fields(
        conv["id"],
        system_only=1,
        minimal_ooda_enabled=0,
        system_prompt="Stay in close third person.",
        ooda_enabled=0,
    )
    updated = await db.get_conversation(conv["id"])
    assert updated["system_only"] == 1
    assert updated["minimal_ooda_enabled"] == 0
    assert updated["system_prompt"] == "Stay in close third person."
    assert updated["ooda_enabled"] == 0


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


async def test_cc_session_mode_column_exists():
    """The cc_session_mode migration ran — column is present on messages."""
    conn = await db.get_db()
    cursor = await conn.execute("PRAGMA table_info(messages)")
    columns = await cursor.fetchall()
    await conn.close()
    col_names = [c[1] for c in columns]
    assert "cc_session_mode" in col_names


async def test_model_attestation_round_trip():
    """Provider-returned model evidence persists separately from the model label."""
    conv = await db.create_conversation("Model Proof", mode="codex")
    msg = await db.add_message(conv["id"], "assistant", "proved")
    proof = '{"status":"verified","effective_model":"gpt-5.6-sol"}'

    await db.update_message_content(
        msg["id"],
        cc_model_used="gpt-5.6-sol",
        model_attestation=proof,
    )

    fetched = await db.get_message(msg["id"])
    assert fetched["cc_model_used"] == "gpt-5.6-sol"
    assert fetched["model_attestation"] == proof


async def test_codex_goal_columns_round_trip():
    conv = await db.create_conversation("Codex Goal", mode="codex")
    await db.update_conversation_fields(
        conv["id"],
        codex_goal_objective="Finish the migration",
        codex_goal_status="active",
        codex_goal_token_budget=40000,
        codex_goal_tokens_used=123,
        codex_goal_time_used_seconds=7,
        codex_goal_updated_at=42.0,
    )

    updated = await db.get_conversation(conv["id"])
    assert updated["codex_goal_objective"] == "Finish the migration"
    assert updated["codex_goal_status"] == "active"
    assert updated["codex_goal_token_budget"] == 40000
    assert updated["codex_goal_tokens_used"] == 123
    assert updated["codex_goal_time_used_seconds"] == 7
    assert updated["codex_goal_updated_at"] == 42.0


async def test_cc_session_mode_round_trip_and_null_is_unscoped():
    """update_message_content persists cc_session_mode; NULL is the legacy default.

    A NULL mode cannot prove which harness owns the native session. The shared
    provider contract therefore rebuilds that turn from Loom history.
    """
    conv = await db.create_conversation("Mode Tag Test")
    m1 = await db.add_message(conv["id"], "user", "hello")
    m2 = await db.add_message(conv["id"], "assistant", "hi", parent_id=m1["id"])

    # Tag m2 as a dream-session message.
    await db.update_message_content(m2["id"], cc_session_id="dream-sess-1",
                                   cc_session_mode="dream")
    fetched = await db.get_message(m2["id"])
    assert fetched["cc_session_id"] == "dream-sess-1"
    assert fetched["cc_session_mode"] == "dream"

    # m1 was never tagged — NULL mode, therefore unscoped.
    m1_fetched = await db.get_message(m1["id"])
    assert m1_fetched.get("cc_session_mode") is None

    # Only an explicit matching mode is compatible.
    def _compatible(msg_mode, current_mode):
        return bool(msg_mode) and msg_mode == current_mode
    assert not _compatible(m1_fetched.get("cc_session_mode"), "dream")
    assert _compatible(fetched["cc_session_mode"], "dream")          # same mode → compatible
    assert not _compatible(fetched["cc_session_mode"], "hermes")     # cross-mode → skip
