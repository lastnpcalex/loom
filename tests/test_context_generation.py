"""Regression tests for branch-scoped local generation context."""

import database as db
import context_manager
from context_manager import get_context_for_generation
from prompt_engine import assemble_prompt


async def test_explicit_leaf_context_ignores_stale_active_branch():
    conv = await db.create_conversation("Existing Loom")

    old_root = await db.add_message(conv["id"], "user", "old root")
    old_reply = await db.add_message(
        conv["id"], "assistant", "old reply", parent_id=old_root["id"]
    )
    old_marker = await db.add_message(
        conv["id"],
        "system",
        "[Context compactified - stale marker]",
        parent_id=old_reply["id"],
    )
    await db.set_active_branch(conv["id"], old_marker["id"])

    fresh_user = await db.add_message(conv["id"], "user", "fresh opening")

    context = await get_context_for_generation(
        conv["id"], character=None, leaf_id=fresh_user["id"]
    )

    assert context["was_compactified"] is False
    assert [m["id"] for m in context["verbatim_messages"]] == [fresh_user["id"]]
    assert [m["role"] for m in context["verbatim_messages"]] == ["user"]


async def test_assembled_prompt_keeps_system_role_at_start_only():
    messages = assemble_prompt(
        system_prompt="Base system",
        summary="Earlier branch summary",
        conversation_messages=[
            {"role": "user", "content": "fresh opening"},
            {"role": "system", "content": "[Context compactified - marker]"},
            {"role": "assistant", "content": "reply"},
        ],
    )

    assert messages[0]["role"] == "system"
    assert "Earlier branch summary" in messages[0]["content"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]


async def test_explicit_leaf_context_still_uses_compacted_summary():
    conv = await db.create_conversation("Compacted branch")
    root = await db.add_message(conv["id"], "user", "root " * 300)
    current = root
    for i in range(8):
        role = "assistant" if i % 2 == 0 else "user"
        current = await db.add_message(
            conv["id"], role, f"message {i} " * 300, parent_id=current["id"]
        )

    branch = await db.get_branch_to_root(current["id"])
    await db.save_summary(
        conv["id"],
        [m["id"] for m in branch[:5]],
        "Saved compact summary",
        covers_up_to=branch[4]["id"],
    )

    original_budget = context_manager.config.max_context_tokens
    original_window = context_manager.config.verbatim_window
    try:
        context_manager.config.max_context_tokens = 10
        context_manager.config.verbatim_window = 3
        context = await get_context_for_generation(
            conv["id"], character=None, leaf_id=current["id"]
        )
    finally:
        context_manager.config.max_context_tokens = original_budget
        context_manager.config.verbatim_window = original_window

    assert context["was_compactified"] is True
    assert context["summary"] == "Saved compact summary"
    assert [m["id"] for m in context["verbatim_messages"]] == [
        m["id"] for m in branch[-3:]
    ]
