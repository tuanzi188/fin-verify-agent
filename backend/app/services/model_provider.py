import logging
from typing import Optional, List, AsyncIterator

from app.config import MODEL_PROVIDER
from app.services.deepseek import deepseek_client
from app.services.qwen_client import qwen_client

logger = logging.getLogger(__name__)


def _resolve_provider():
    if MODEL_PROVIDER == "qwen":
        return qwen_client
    return deepseek_client


async def chat(
    messages: List[dict],
    reasoning_mode: str = "flash",
    temperature: float = 0.1,
    max_tokens: Optional[int] = None,
) -> tuple[str, Optional[str], dict]:
    provider_instance = _resolve_provider()
    return await provider_instance.chat(messages, reasoning_mode, temperature, max_tokens)


async def chat_with_tools(
    messages: List[dict],
    tools: List[dict],
    tool_choice: Optional[str] = None,
    reasoning_mode: str = "flash",
    temperature: float = 0.1,
    max_tokens: Optional[int] = None,
) -> tuple[str, list[dict], Optional[str], dict]:
    """工具调用入口。推理档不支持 tool calling，调用方应固定传 flash。"""
    provider_instance = _resolve_provider()
    return await provider_instance.chat_with_tools(
        messages,
        tools,
        tool_choice=tool_choice,
        reasoning_mode=reasoning_mode,
        temperature=temperature,
        max_tokens=max_tokens,
    )


async def chat_stream(
    messages: List[dict],
    reasoning_mode: str = "flash",
    temperature: float = 0.1,
    max_tokens: Optional[int] = None,
    result_holder: Optional[dict] = None,
) -> AsyncIterator[str]:
    provider_instance = _resolve_provider()
    async for token in provider_instance.chat_stream(
        messages,
        reasoning_mode,
        temperature,
        max_tokens,
        result_holder=result_holder,
    ):
        yield token


async def simple_query(
    query: str, system_prompt: Optional[str] = None, max_tokens: Optional[int] = None
) -> tuple[str, Optional[str], dict]:
    provider_instance = _resolve_provider()
    if max_tokens is None:
        return await provider_instance.simple_query(query, system_prompt)
    return await provider_instance.simple_query(query, system_prompt, max_tokens=max_tokens)


async def fallback_query(
    query: str, system_prompt: Optional[str] = None
) -> tuple[str, Optional[str], dict]:
    provider_instance = _resolve_provider()
    fallback_handler = getattr(provider_instance, "fallback_query", None)
    if callable(fallback_handler):
        return await fallback_handler(query, system_prompt)
    return await provider_instance.simple_query(query, system_prompt)


async def rag_query(
    query: str, context: str, reasoning_mode: str = "pro"
) -> tuple[str, Optional[str], dict]:
    provider_instance = _resolve_provider()
    return await provider_instance.rag_query(query, context, reasoning_mode)


async def rag_query_stream(
    query: str,
    context: str,
    reasoning_mode: str = "pro",
    result_holder: Optional[dict] = None,
) -> AsyncIterator[str]:
    provider_instance = _resolve_provider()
    async for token in provider_instance.rag_query_stream(
        query,
        context,
        reasoning_mode,
        result_holder=result_holder,
    ):
        yield token


async def classify_intent(query: str) -> dict:
    provider_instance = _resolve_provider()
    return await provider_instance.classify_intent(query)


async def rewrite_query(query: str) -> List[str]:
    provider_instance = _resolve_provider()
    rewrite_handler = getattr(provider_instance, "rewrite_query", None)
    if callable(rewrite_handler):
        return await rewrite_handler(query)
    return []


def select_reasoning_mode(intent: dict) -> str:
    provider_instance = _resolve_provider()
    return provider_instance.select_reasoning_mode(intent)
