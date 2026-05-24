"""Dispatcher for the local-model backend (Weave / OODA paths).

All calls route directly to llama_client, which talks to llama-server.exe
via the OpenAI-compatible /v1/chat/completions endpoint on port 11434.
"""

from typing import AsyncGenerator

import llama_client


async def health_check() -> dict:
    return await llama_client.health_check()


async def stream_chat(messages, **kwargs) -> AsyncGenerator:
    async for item in llama_client.stream_chat(messages, **kwargs):
        yield item


async def sync_chat(messages, **kwargs) -> str:
    return await llama_client.sync_chat(messages, **kwargs)


async def describe_image(image_path: str, **kwargs) -> str:
    return await llama_client.describe_image(image_path, **kwargs)


async def describe_image_with_data(image_path: str, **kwargs):
    return await llama_client.describe_image_with_data(image_path, **kwargs)


def list_local_models() -> list[str]:
    """Return sorted list of .gguf filenames from the models directory."""
    return llama_client.list_local_models()
