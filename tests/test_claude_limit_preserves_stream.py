import server


def test_cc_error_result_text_is_not_adopted_as_assistant_output():
    evt = {
        "type": "result",
        "is_error": True,
        "result_text": "Claude usage limit reached",
    }

    assert not server._cc_should_adopt_result_text(evt)


def test_cc_thinking_only_stream_counts_as_partial_output():
    blocks = [{"type": "thinking", "text": "checking files"}]

    assert server._cc_has_streamed_output("", blocks)


def test_cc_interrupt_note_preserves_streamed_blocks_and_uses_result_text():
    blocks = [
        {"type": "thinking", "text": "checking files"},
        {
            "type": "tool_use",
            "name": "Read",
            "tool_id": "toolu_1",
            "input": '{"file_path":"server.py"}',
            "result": "content",
        },
    ]

    full_text = server._append_cc_interrupt_note(
        "",
        blocks,
        {"is_error": True, "result_text": "Claude usage limit reached"},
    )

    assert blocks[0]["type"] == "thinking"
    assert blocks[1]["type"] == "tool_use"
    assert blocks[-1]["type"] == "text"
    assert "Claude usage limit reached" in blocks[-1]["text"]
    assert "Claude usage limit reached" in full_text


def test_cc_interrupt_note_keeps_text_when_structured_blocks_are_missing():
    blocks = []

    full_text = server._append_cc_interrupt_note(
        "partial answer",
        blocks,
        {"is_error": True, "result_text": "Claude usage limit reached"},
    )

    assert blocks == [{"type": "text", "text": full_text}]
    assert "partial answer" in full_text
    assert "Claude usage limit reached" in full_text
