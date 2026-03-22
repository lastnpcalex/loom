"""Tests for local mode specific behavior."""

import pytest
import database as db


async def test_local_conv_stores_model():
    """Creating a local conversation and setting local_model persists it in DB."""
    conv = await db.create_conversation("Local Chat", mode="local")
    await db.update_conversation_fields(conv["id"], local_model="llama3:8b")

    fetched = await db.get_conversation(conv["id"])
    assert fetched["mode"] == "local"
    assert fetched["local_model"] == "llama3:8b"


async def test_local_conv_in_list():
    """Local conversations appear with correct mode and model in the list."""
    conv = await db.create_conversation("Local Listed", mode="local")
    await db.update_conversation_fields(conv["id"], local_model="qwen3:4b")

    convs = await db.list_conversations()
    local_convs = [c for c in convs if c["id"] == conv["id"]]
    assert len(local_convs) == 1
    assert local_convs[0]["mode"] == "local"
    assert local_convs[0]["local_model"] == "qwen3:4b"


async def test_local_conv_filter():
    """Can filter list to only local conversations."""
    await db.create_conversation("Weave One", mode="weave")
    c1 = await db.create_conversation("Local One", mode="local")
    c2 = await db.create_conversation("Local Two", mode="local")
    await db.create_conversation("Claude One", mode="claude")

    all_convs = await db.list_conversations()
    local_only = [c for c in all_convs if c["mode"] == "local"]
    local_ids = {c["id"] for c in local_only}
    assert c1["id"] in local_ids
    assert c2["id"] in local_ids
    assert len(local_only) == 2


async def test_add_messages_to_local_conv():
    """Can add user messages to a local conversation."""
    conv = await db.create_conversation("Local Msgs", mode="local")
    await db.update_conversation_fields(conv["id"], local_model="llama3:8b")

    m1 = await db.add_message(conv["id"], "user", "Hello local model")
    m2 = await db.add_message(conv["id"], "assistant", "Hi!", parent_id=m1["id"])

    branch = await db.get_branch_to_root(m2["id"])
    assert len(branch) == 2
    assert branch[0]["role"] == "user"
    assert branch[1]["role"] == "assistant"


async def test_local_conv_no_character():
    """Local conversation has no character_id, persona_id, or lore."""
    conv = await db.create_conversation("Bare Local", mode="local")
    fetched = await db.get_conversation(conv["id"])
    assert fetched["character_id"] is None
    assert fetched["persona_id"] is None
    assert fetched["lore_ids"] == "[]"
