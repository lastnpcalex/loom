"""Regression coverage for same-checkout parallel agent coordination."""

import asyncio

import database as db


def test_hook_based_harnesses_export_generation_permission_scope():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for filename in ("claude_client.py", "gemini_client.py"):
        source = (root / filename).read_text(encoding="utf-8")
        assert 'env["LOOM_PERMISSION_SCOPE"] = f"gen:{gen_key[2]}"' in source


async def test_parallel_agents_setting_persists_and_forks():
    conv = await db.create_conversation("Parallel", mode="codex", project_dir=".")
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
    conv = await db.get_conversation(conv["id"])
    assert conv["parallel_agents_enabled"] == 1

    msg = await db.add_message(conv["id"], "user", "independent task")
    fork = await db.fork_conversation(conv["id"], msg["id"])
    assert fork["parallel_agents_enabled"] == 1


async def test_create_and_update_parallel_agents_api(client, mock_llama, tmp_path):
    response = await client.post(
        "/api/conversations",
        json={
            "title": "Parallel API",
            "mode": "claude",
            "project_dir": str(tmp_path),
            "cc_model": "sonnet",
            "parallel_agents_enabled": True,
        },
    )
    assert response.status_code == 200
    conv = response.json()
    assert conv["parallel_agents_enabled"] == 1

    response = await client.put(
        f"/api/conversations/{conv['id']}",
        json={"parallel_agents_enabled": False},
    )
    assert response.status_code == 200
    assert response.json()["parallel_agents_enabled"] == 0


async def test_parallel_agents_require_agent_workspace(client, mock_llama):
    response = await client.post(
        "/api/conversations",
        json={
            "title": "Invalid Parallel",
            "mode": "weave",
            "parallel_agents_enabled": True,
        },
    )
    assert response.status_code == 400
    assert "agent conversations" in response.json()["detail"]


async def test_cancel_generation_keys_can_target_one_parallel_generation():
    import server

    gate = asyncio.Event()
    first = asyncio.create_task(gate.wait())
    second = asyncio.create_task(gate.wait())
    first_key = (81, 101, 1001)
    second_key = (81, 202, 1002)
    server._active_generations[first_key] = first
    server._active_generations[second_key] = second
    server._generation_snapshots[first_key] = {"draft_msg_id": 301}
    server._generation_snapshots[second_key] = {"draft_msg_id": 302}
    try:
        assert server._active_generation_keys_for_cancel(81, gen_id=1002) == [second_key]
        assert server._active_generation_keys_for_cancel(81, draft_msg_id=301) == [first_key]
        assert set(server._active_generation_keys_for_cancel(81)) == {first_key, second_key}
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        server._active_generations.pop(first_key, None)
        server._active_generations.pop(second_key, None)
        server._generation_snapshots.pop(first_key, None)
        server._generation_snapshots.pop(second_key, None)


async def test_ws_send_tags_generation_events_with_branch_snapshot():
    import server

    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, value):
            self.messages.append(value)

    current = asyncio.current_task()
    old_key = getattr(current, "_gen_key", None)
    key = (91, 401, 7001)
    fake = FakeWebSocket()
    server._active_websockets[91] = {fake}
    server._generation_snapshots[key] = {"parent_id": 401, "draft_msg_id": 402}
    current._gen_key = key
    try:
        await server._ws_send(91, {"type": "stream_chunk", "content": "ok"})
        assert fake.messages[-1]["gen_id"] == 7001
        assert fake.messages[-1]["parent_id"] == 401
        assert fake.messages[-1]["draft_msg_id"] == 402
    finally:
        if old_key is None:
            delattr(current, "_gen_key")
        else:
            current._gen_key = old_key
        server._active_websockets.pop(91, None)
        server._generation_snapshots.pop(key, None)


async def test_every_agent_generation_uses_workspace_safety_wrapper(
    monkeypatch, tmp_database, tmp_path
):
    import server

    conv = await db.create_conversation(
        "Safe wrapper",
        mode="codex",
        project_dir=str(tmp_path),
    )
    calls = []
    sent = []
    token = object()

    def fake_capture(workspace, recovery_root, **kwargs):
        calls.append(("capture", workspace, kwargs["generation_id"]))
        return token

    def fake_finalize(snapshot):
        assert snapshot is token
        calls.append(("finalize",))
        return {"type": "workspace_change_report", "changed_count": 1, "warnings": []}

    async def fake_generation(websocket, conv_id, data):
        calls.append(("generate", conv_id))

    async def fake_send(conv_id, message):
        sent.append((conv_id, message))

    monkeypatch.setattr(server, "capture_workspace_snapshot", fake_capture)
    monkeypatch.setattr(server, "finalize_workspace_snapshot", fake_finalize)
    monkeypatch.setattr(server, "default_recovery_root", lambda: tmp_path / "recovery")
    monkeypatch.setattr(server, "_handle_generation", fake_generation)
    monkeypatch.setattr(server, "_ws_send", fake_send)

    current = asyncio.current_task()
    old_key = getattr(current, "_gen_key", None)
    current._gen_key = (conv["id"], 10, 99)
    try:
        await server._handle_generation_with_workspace_safety(None, conv["id"], {})
    finally:
        if old_key is None:
            delattr(current, "_gen_key")
        else:
            current._gen_key = old_key

    assert calls[0] == ("capture", str(tmp_path), 99)
    assert calls[1:] == [("generate", conv["id"]), ("finalize",)]
    assert sent[0][1]["type"] == "workspace_change_report"
