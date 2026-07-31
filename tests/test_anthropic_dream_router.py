import json

import pytest

import anthropic_dream_router as router


def test_anthropic_request_converts_tools_and_tool_results():
    body = {
        "model": "claude",
        "system": "Be brief.",
        "max_tokens": 128,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Read it"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "file contents",
                    },
                    {"type": "text", "text": "Summarize it."},
                ],
            },
        ],
        "tools": [{
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }],
        "tool_choice": {"type": "any"},
    }

    out = router.anthropic_to_openai(body, model="dream")

    assert out["model"] == "dream"
    assert out["stream"] is False
    assert out["max_tokens"] >= 128
    assert out["chat_template_kwargs"] == {"enable_thinking": True}
    assert out["tool_choice"] == "required"
    assert out["tools"][0]["function"]["name"] == "read_file"
    assert out["messages"][0] == {"role": "system", "content": "Be brief."}
    assert out["messages"][2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(out["messages"][2]["tool_calls"][0]["function"]["arguments"]) == {
        "path": "README.md"
    }
    assert out["messages"][3] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "file contents",
    }
    assert out["messages"][4] == {"role": "user", "content": "Summarize it."}


def test_anthropic_request_uses_fallback_max_tokens_when_omitted():
    out = router.anthropic_to_openai(
        {"model": "claude", "messages": [{"role": "user", "content": "hello"}]},
        model="dream",
    )

    assert out["max_tokens"] == router.DEFAULT_MAX_TOKENS


def test_openai_response_converts_text_and_strips_dream_channel_scaffold():
    data = {
        "id": "chatcmpl-1",
        "model": "dream",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "<|channel>thought\nhidden\n<channel|>\nVisible answer",
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
    }

    out = router.openai_to_anthropic(data, model="dream")

    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["stop_reason"] == "end_turn"
    assert out["content"] == [
        {"type": "thinking", "thinking": "hidden"},
        {"type": "text", "text": "\nVisible answer"},
    ]
    assert out["usage"]["input_tokens"] == 12
    assert out["usage"]["output_tokens"] == 34


def test_openai_response_preserves_reasoning_content_as_thinking_block():
    data = {
        "id": "chatcmpl-1",
        "model": "dream",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Visible answer",
                "reasoning_content": "hidden reasoning",
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
    }

    out = router.openai_to_anthropic(data, model="dream")

    assert out["content"] == [
        {"type": "thinking", "thinking": "hidden reasoning"},
        {"type": "text", "text": "Visible answer"},
    ]


def test_openai_tool_calls_convert_to_anthropic_tool_use():
    data = {
        "id": "chatcmpl-2",
        "model": "dream",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-0",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": "{\"path\":\"x.txt\",\"content\":\"hi\"}",
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 256},
    }

    out = router.openai_to_anthropic(data, model="dream")

    assert out["stop_reason"] == "tool_use"
    assert out["content"] == [{
        "type": "tool_use",
        "id": "call-0",
        "name": "write_file",
        "input": {"path": "x.txt", "content": "hi"},
    }]


@pytest.mark.asyncio
async def test_anthropic_sse_sequence_for_text_and_tool_use():
    message = {
        "id": "msg_1",
        "model": "dream",
        "content": [
            {"type": "thinking", "thinking": "checking"},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {"path": "a"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 9},
    }

    raw = b"".join([chunk async for chunk in router._anthropic_sse(message)]).decode("utf-8")
    events = []
    for part in raw.strip().split("\n\n"):
        lines = part.splitlines()
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event, data))

    assert [e for e, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1][1]["content_block"]["type"] == "thinking"
    assert events[2][1]["delta"]["type"] == "thinking_delta"
    assert events[4][1]["content_block"]["type"] == "text"
    assert events[7][1]["content_block"]["type"] == "tool_use"
    assert events[8][1]["delta"]["type"] == "input_json_delta"
    assert events[10][1]["delta"]["stop_reason"] == "tool_use"
