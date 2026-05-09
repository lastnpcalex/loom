"""Dispatcher for the local-model backend (Weave / OODA paths).

Selects between ollama_client and vllm_client based on config.local_backend.
Calls into the underlying module by attribute lookup so test fixtures that
patch `ollama_client.<fn>` continue to take effect under the default backend.

When config.vllm_text_only is True, describe_image always routes to Ollama
even with backend=vllm — used when vLLM serves a text-only model (e.g., the
MTP-tuned Qwen3.6-27B-Text-NVFP4-MTP) and Ollama hosts a small VL model on
the side for image-describe.
"""

from typing import AsyncGenerator

from config import config
import ollama_client
import vllm_client


def _backend():
    return vllm_client if config.local_backend == "vllm" else ollama_client


def _vision_backend():
    """Vision/describe goes to Ollama when vLLM is text-only, otherwise tracks
    the active backend like everything else."""
    if config.local_backend == "vllm" and config.vllm_text_only:
        return ollama_client
    return _backend()


async def health_check() -> dict:
    return await _backend().health_check()


async def stream_chat(messages, **kwargs) -> AsyncGenerator:
    async for item in _backend().stream_chat(messages, **kwargs):
        yield item


async def sync_chat(messages, **kwargs) -> str:
    return await _backend().sync_chat(messages, **kwargs)


async def describe_image(image_path: str, **kwargs) -> str:
    return await _vision_backend().describe_image(image_path, **kwargs)


async def describe_image_with_data(image_path: str, **kwargs):
    return await _vision_backend().describe_image_with_data(image_path, **kwargs)
