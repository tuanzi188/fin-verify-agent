import json
import logging
from datetime import datetime
from typing import Optional, List, AsyncIterator

from openai import AsyncOpenAI, APIStatusError, RateLimitError, APITimeoutError, APIConnectionError

from app.services.resilience import (
    circuit_breaker_registry,
    retry_with_backoff,
    CircuitBreakerOpenError,
)
from app.core.exceptions import UpstreamAPIError

logger = logging.getLogger(__name__)

# 可重试的异常：速率限制、超时、连接错误（4xx 客户端错误不重试）
_RETRYABLE_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError)


def parse_tool_calls(message) -> list[dict]:
    """把 SDK 返回的 tool_calls 规整成可记录进 trace 的 dict。

    `arguments` 解析失败不抛异常：模型偶尔会吐出半截 JSON，这是「参数错误」
    维度要观测的现象，不该让整次运行崩掉。解析失败时 `arguments` 为 `{}`、
    `parse_error` 带原因，Agent 侧按 bad_args 处理。
    """
    raw_calls = getattr(message, "tool_calls", None) or []
    parsed: list[dict] = []
    for raw_call in raw_calls:
        function = getattr(raw_call, "function", None)
        name = getattr(function, "name", "") or ""
        raw_arguments = getattr(function, "arguments", "") or ""
        arguments: dict = {}
        parse_error: Optional[str] = None

        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif isinstance(raw_arguments, str):
            text = raw_arguments.strip()
            if text:
                try:
                    loaded = json.loads(text)
                except Exception as exc:  # noqa: BLE001 - 解析失败要记录，不中断
                    parse_error = f"{type(exc).__name__}: {exc}"
                else:
                    if isinstance(loaded, dict):
                        arguments = loaded
                    else:
                        parse_error = f"arguments 不是 JSON 对象: {type(loaded).__name__}"

        parsed.append({
            "id": getattr(raw_call, "id", "") or "",
            "name": name,
            "arguments": arguments,
            "parse_error": parse_error,
        })
    return parsed


class OpenAICompatibleClient:
    """OpenAI 兼容 API 客户端基类，封装熔断、重试、多 provider 降级等公共逻辑。"""

    def __init__(
        self,
        *,
        config: dict,
        model_config: dict,
        provider_name: str,
        breaker_name: str,
    ):
        self._config = config
        self._model_config = model_config
        self._provider_name = provider_name
        self.clients: dict[str, AsyncOpenAI] = {}
        self._init_clients()
        self.provider_order = ["primary", "fallback_1"]
        self._breaker = circuit_breaker_registry.get_or_create(
            name=breaker_name,
            failure_threshold=5,
            recovery_timeout=30.0,
        )

    def _init_clients(self):
        for name, cfg in self._config.items():
            api_key = cfg.get("api_key")
            base_url = cfg.get("base_url")
            if api_key:
                try:
                    self.clients[name] = AsyncOpenAI(
                        api_key=api_key,
                        base_url=base_url,
                        # 不设超时会被 SDK 默认的 600s 拖着走：2026-10-27 实测一次任务
                        # 挂了 4,528,320 ms（75 分钟），整批跑了 78 分钟。
                        # 我们自己的 retry_with_backoff 负责重试，这里不再叠 SDK 重试。
                        timeout=120.0,
                        max_retries=0,
                    )
                    logger.info(
                        "%s client initialized: %s -> %s",
                        self._provider_name,
                        name,
                        base_url,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to init %s client %s: %s",
                        self._provider_name,
                        name,
                        e,
                    )

    def _get_model_config(self, reasoning_mode: str) -> dict:
        normalized = reasoning_mode.lower().strip()
        if normalized in ("pro", "think_high", "think_max"):
            return self._model_config["pro"]
        return self._model_config["flash"]

    async def _chat_impl(
        self,
        messages: List[dict],
        reasoning_mode: str = "flash",
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[str], dict]:
        model_cfg = self._get_model_config(reasoning_mode)
        max_tokens = max_tokens or model_cfg["max_tokens"]
        last_error = None
        errors: list[tuple[str, str]] = []

        for provider in self.provider_order:
            client = self.clients.get(provider)
            if not client:
                continue
            try:
                response = await client.chat.completions.create(
                    model=model_cfg["model"],
                    messages=messages,
                    # 不能用 `temperature or ...`：显式传 0.0 会被当成"没传"而换成
                    # 模型档位默认值，评测里就会悄悄丢掉确定性设置。
                    temperature=temperature if temperature is not None else model_cfg["temperature"],
                    max_tokens=max_tokens,
                    stream=False,
                )
                choice = response.choices[0]
                content = choice.message.content or ""
                reasoning = getattr(choice.message, "reasoning_content", None)
                usage_info = {
                    "provider": provider,
                    "model": model_cfg["model"],
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                    "total_tokens": response.usage.total_tokens if response.usage else 0,
                    "finish_reason": getattr(choice, "finish_reason", None),
                    "has_reasoning": bool(reasoning),
                }
                return content, reasoning, usage_info
            except APIStatusError as e:
                status_code = getattr(e, "status_code", 0)
                detail = str(getattr(e, "message", e))[:500]
                # 4xx 客户端错误直接转换为业务异常，让 api 层透传具体原因
                if 400 <= status_code <= 499:
                    raise UpstreamAPIError(
                        status_code=status_code,
                        detail=detail,
                        provider=f"{self._provider_name}:{provider}",
                    ) from e
                err_msg = f"{type(e).__name__}: {e}"
                errors.append((provider, err_msg))
                logger.error(
                    "%s provider %s failed: %s",
                    self._provider_name,
                    provider,
                    e,
                )
                last_error = e
                continue
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                errors.append((provider, err_msg))
                logger.error(
                    "%s provider %s failed: %s",
                    self._provider_name,
                    provider,
                    e,
                )
                last_error = e
                continue

        error_detail = "; ".join(f"{p}: {m}" for p, m in errors)
        raise UpstreamAPIError(
            status_code=502,
            detail=f"All {len(self.provider_order)} {self._provider_name} providers failed: [{error_detail}]",
            provider=self._provider_name,
        ) from last_error

    async def chat(
        self,
        messages: List[dict],
        reasoning_mode: str = "flash",
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[str], dict]:
        """带熔断器 + 退避重试的 chat 调用。"""
        self._breaker.before_call()
        try:
            result = await retry_with_backoff(
                lambda: self._chat_impl(messages, reasoning_mode, temperature, max_tokens),
                max_retries=3,
                base_delay=0.5,
                retryable_exceptions=_RETRYABLE_ERRORS,
            )
            self._breaker.on_success()
            return result
        except UpstreamAPIError as e:
            # 4xx 客户端错误直接抛出，不计入熔断失败
            if 400 <= e.status_code <= 499:
                raise
            self._breaker.on_failure()
            raise
        except CircuitBreakerOpenError:
            raise
        except Exception:
            self._breaker.on_failure()
            raise

    async def _chat_with_tools_impl(
        self,
        messages: List[dict],
        tools: List[dict],
        tool_choice: Optional[str],
        reasoning_mode: str,
        temperature: float,
        max_tokens: Optional[int],
    ) -> tuple[str, list[dict], Optional[str], dict]:
        """工具调用版 chat 实现：provider 逐次尝试，每次都透传 tools。

        注意不能换成推理档：`deepseek-reasoner` 一类模型不支持 tool calling，
        传了 tools 也拿不到 tool_calls。
        """
        model_cfg = self._get_model_config(reasoning_mode)
        max_tokens = max_tokens or model_cfg["max_tokens"]
        last_error = None
        errors: list[tuple[str, str]] = []

        for provider in self.provider_order:
            client = self.clients.get(provider)
            if not client:
                continue
            try:
                request: dict = {
                    "model": model_cfg["model"],
                    "messages": messages,
                    # 同 `_chat_impl`：0.0 是合法温度，不能被 `or` 吃掉。
                    "temperature": temperature if temperature is not None else model_cfg["temperature"],
                    "max_tokens": max_tokens,
                    "stream": False,
                    "tools": tools,
                }
                if tool_choice:
                    request["tool_choice"] = tool_choice
                response = await client.chat.completions.create(**request)
                choice = response.choices[0]
                content = choice.message.content or ""
                reasoning = getattr(choice.message, "reasoning_content", None)
                tool_calls = parse_tool_calls(choice.message)
                usage_info = {
                    "provider": provider,
                    "model": model_cfg["model"],
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                    "total_tokens": response.usage.total_tokens if response.usage else 0,
                    "finish_reason": getattr(choice, "finish_reason", None),
                    "has_reasoning": bool(reasoning),
                }
                return content, tool_calls, reasoning, usage_info
            except APIStatusError as e:
                status_code = getattr(e, "status_code", 0)
                detail = str(getattr(e, "message", e))[:500]
                if 400 <= status_code <= 499:
                    raise UpstreamAPIError(
                        status_code=status_code,
                        detail=detail,
                        provider=f"{self._provider_name}:{provider}",
                    ) from e
                errors.append((provider, f"{type(e).__name__}: {e}"))
                logger.error(
                    "%s provider %s tool-call failed: %s",
                    self._provider_name, provider, e,
                )
                last_error = e
                continue
            except Exception as e:
                errors.append((provider, f"{type(e).__name__}: {e}"))
                logger.error(
                    "%s provider %s tool-call failed: %s",
                    self._provider_name, provider, e,
                )
                last_error = e
                continue

        error_detail = "; ".join(f"{p}: {m}" for p, m in errors)
        raise UpstreamAPIError(
            status_code=502,
            detail=f"All {len(self.provider_order)} {self._provider_name} providers failed: [{error_detail}]",
            provider=self._provider_name,
        ) from last_error

    async def chat_with_tools(
        self,
        messages: List[dict],
        tools: List[dict],
        tool_choice: Optional[str] = None,
        reasoning_mode: str = "flash",
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, list[dict], Optional[str], dict]:
        """带熔断器 + 退避重试的工具调用 chat。

        返回值比 `chat()` 多一个 `tool_calls` 位置，所以单开方法而不改
        `chat()` 的签名——`rag_query` / `simple_query` / `classify_intent`
        等调用方都还在用三元组解包。

        返回 `(text, tool_calls, reasoning, usage)`；`tool_calls` 元素形如
        `{"id", "name", "arguments", "parse_error"}`，无工具调用时为空列表。
        """
        self._breaker.before_call()
        try:
            result = await retry_with_backoff(
                lambda: self._chat_with_tools_impl(
                    messages, tools, tool_choice, reasoning_mode, temperature, max_tokens
                ),
                max_retries=3,
                base_delay=0.5,
                retryable_exceptions=_RETRYABLE_ERRORS,
            )
            self._breaker.on_success()
            return result
        except UpstreamAPIError as e:
            if 400 <= e.status_code <= 499:
                raise
            self._breaker.on_failure()
            raise
        except CircuitBreakerOpenError:
            raise
        except Exception:
            self._breaker.on_failure()
            raise

    async def chat_stream(
        self,
        messages: List[dict],
        reasoning_mode: str = "flash",
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        result_holder: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        """带熔断器 + 退避重试 + reasoning_content 采集的流式调用。"""
        self._breaker.before_call()
        model_cfg = self._get_model_config(reasoning_mode)
        max_tokens = max_tokens or model_cfg["max_tokens"]
        reasoning_parts: list[str] = []

        for provider in self.provider_order:
            client = self.clients.get(provider)
            if not client:
                continue

            async def _create_stream(provider_client=client):
                return await provider_client.chat.completions.create(
                    model=model_cfg["model"],
                    messages=messages,
                    temperature=temperature or model_cfg["temperature"],
                    max_tokens=max_tokens,
                    stream=True,
                )

            try:
                # 对流连接建立阶段使用退避重试，缓解瞬时速率/超时/连接错误
                stream = await retry_with_backoff(
                    _create_stream,
                    max_retries=3,
                    base_delay=0.5,
                    retryable_exceptions=_RETRYABLE_ERRORS,
                )
                provider_used = provider
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta:
                        reasoning_content = getattr(delta, "reasoning_content", None)
                        if reasoning_content:
                            reasoning_parts.append(reasoning_content)
                        if delta.content:
                            yield delta.content
                self._breaker.on_success()
                if result_holder is not None:
                    result_holder["reasoning_content"] = "".join(reasoning_parts)
                    result_holder["provider"] = provider_used
                return
            except APIStatusError as e:
                status_code = getattr(e, "status_code", 0)
                detail = str(getattr(e, "message", e))[:500]
                # 4xx 客户端错误直接转换为业务异常，让 api 层透传具体原因
                if 400 <= status_code <= 499:
                    raise UpstreamAPIError(
                        status_code=status_code,
                        detail=detail,
                        provider=f"{self._provider_name}:{provider}",
                    ) from e
                logger.error(
                    "%s stream provider %s failed: %s",
                    self._provider_name,
                    provider,
                    e,
                )
                continue
            except Exception as e:
                logger.error(
                    "%s stream provider %s failed: %s",
                    self._provider_name,
                    provider,
                    e,
                )
                continue

        self._breaker.on_failure()
        raise UpstreamAPIError(
            status_code=502,
            detail=f"All {len(self.provider_order)} {self._provider_name} stream providers failed",
            provider=self._provider_name,
        )

    async def rag_query_stream(
        self,
        query: str,
        context: str,
        reasoning_mode: str = "pro",
        result_holder: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        from app.services.prompts import SYSTEM_PROMPT
        system_prompt = SYSTEM_PROMPT.format(current_date=datetime.now().strftime("%Y-%m-%d"))
        user_prompt = f"参考资料：\n{context}\n\n问题：{query}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        async for token in self.chat_stream(
            messages,
            reasoning_mode=reasoning_mode,
            max_tokens=4096,
            result_holder=result_holder,
        ):
            yield token

    async def simple_query(
        self, query: str, system_prompt: Optional[str] = None, max_tokens: int = 2048
    ) -> tuple[str, Optional[str], dict]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": query})
        # 默认 2048 对外部调用方保持不变；judge 需要更大的预算——
        # 实测 judge 偶尔把 2048 token 全花在生成前的推理上，content 回来是空串。
        return await self.chat(messages, reasoning_mode="flash", max_tokens=max_tokens)

    async def fallback_query(
        self, query: str, system_prompt: Optional[str] = None
    ) -> tuple[str, Optional[str], dict]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": query})
        return await self.chat(messages, reasoning_mode="flash", temperature=0.5, max_tokens=4096)

    async def rag_query(
        self, query: str, context: str, reasoning_mode: str = "pro"
    ) -> tuple[str, Optional[str], dict]:
        from app.services.prompts import SYSTEM_PROMPT
        system_prompt = SYSTEM_PROMPT.format(current_date=datetime.now().strftime("%Y-%m-%d"))
        user_prompt = f"参考资料：\n{context}\n\n问题：{query}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self.chat(messages, reasoning_mode=reasoning_mode, max_tokens=4096)

    async def classify_intent(self, query: str) -> dict:
        prompt = (
            f"分析以下查询的意图，返回JSON格式：\n"
            f"查询: {query}\n"
            f"请分析：\n"
            f"1. query_type: 枚举值 [simple_fact, complex_analysis, document_summary, relationship_query, comparison]\n"
            f"2. reasoning_needed: 布尔值, 是否需要深度推理\n"
            f"3. complexity: 1-5 的复杂度评分\n"
            f"4. estimated_docs_needed: 预估需要的文档数量\n\n"
            f"只返回JSON，不要其他内容。"
        )
        content, _, _ = await self.simple_query(prompt)
        try:
            import json
            cleaned = content.strip().removeprefix("```json").removesuffix("```").strip()
            return json.loads(cleaned)
        except Exception as e:
            logger.debug("Intent classification JSON parse failed: %s", e)
            return {"query_type": "complex_analysis", "reasoning_needed": True, "complexity": 3, "estimated_docs_needed": 5}

    async def rewrite_query(self, query: str) -> list[str]:
        prompt = (
            "请将用户问题改写为适合企业知识库检索的查询表达。\n"
            "要求：\n"
            "1. 保留原意，不要引入原问题没有的新事实\n"
            "2. 输出 2 到 3 条更适合检索的短查询\n"
            "3. 覆盖简称、正式名称、关键词表达\n"
            "4. 只返回 JSON 数组，例如 [\"改写1\", \"改写2\"]\n\n"
            f"用户问题：{query}"
        )
        content, _, _ = await self.simple_query(prompt)
        try:
            import json
            cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed_queries = json.loads(cleaned)
            if isinstance(parsed_queries, list):
                normalized_queries = []
                for parsed_query in parsed_queries:
                    if isinstance(parsed_query, str):
                        cleaned_query = parsed_query.strip()
                        if cleaned_query and cleaned_query not in normalized_queries:
                            normalized_queries.append(cleaned_query)
                return normalized_queries[:3]
        except Exception as e:
            logger.debug("Query rewrite JSON parse failed: %s", e)
        return []

    def select_reasoning_mode(self, intent: dict) -> str:
        complexity = intent.get("complexity", 1)
        reasoning_needed = intent.get("reasoning_needed", False)
        query_type = intent.get("query_type", "simple_fact")

        if query_type == "simple_fact" and complexity <= 2 and not reasoning_needed:
            return "flash"
        return "pro"
