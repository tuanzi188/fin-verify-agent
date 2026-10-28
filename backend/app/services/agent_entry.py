"""Agent 模式的入口适配层。

放在 service 层而不是 `api/chat.py` 里，是为了让路由层只负责鉴权/审计/会话，
Agent 的具体执行与响应组装集中在一处。

与 RAG 路径的边界：
- 复用同一套 HTTP 入口、用户上下文、会话命名空间与审计；
- **不复用** `rag_engine` 的固定流水线——Agent 需要工具循环与 trace；
- 多轮：线上走 improved（多轮工具循环 + 空/错重试 + 覆盖自检）；
- 仍不读历史对话（v1 的已知限制，见 docs）。
"""

import logging
import uuid
from typing import Optional

from app.agent.runner import run as run_agent
from app.config import MODEL_CONFIG
from app.core.user_context import UserContext
from app.models.chat import AgentTraceStep, ChatResponse, Citation
from app.services.conversation_memory import conversation_memory
from app.tools.retrieval import get_citation

logger = logging.getLogger(__name__)

_MAX_ARG_CHARS = 200


def _trim_args(arguments: object) -> dict:
    """trace 里的工具参数只保留可读的短值，避免把长文本塞进响应。"""
    if not isinstance(arguments, dict):
        return {}
    trimmed = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > _MAX_ARG_CHARS:
            trimmed[key] = value[:_MAX_ARG_CHARS] + "…"
        else:
            trimmed[key] = value
    return trimmed


def _to_trace(trace: list[dict]) -> list[AgentTraceStep]:
    steps: list[AgentTraceStep] = []
    for raw in trace or []:
        steps.append(AgentTraceStep(
            step=int(raw.get("step") or 0),
            tool=str(raw.get("tool") or ""),
            ok=bool(raw.get("ok")),
            result_count=int(raw.get("result_count") or 0),
            latency_ms=int(raw.get("latency_ms") or 0),
            injected=raw.get("injected"),
            error_kind=raw.get("error_kind"),
            args=_trim_args(raw.get("args")),
            note=raw.get("note"),
        ))
    return steps


async def run_agent_query(
    *,
    query: str,
    conversation_id: str,
    user_context: UserContext,
    max_steps: int = 3,
    variant: str = "improved",
) -> ChatResponse:
    """执行一次 Agent 问答，返回与 RAG 路径同构的 ChatResponse。"""
    task = {
        "id": f"ui-{uuid.uuid4()}",
        "question": query,
        # UI 侧一律按 ask 处理：分情形作答的开场（"取决于…无法确定"）不该被误判成拒答。
        "clarify_mode": "ask",
        "citation_required": True,
        "max_steps": max_steps,
    }

    # clear_cache=False：线上不能每次请求都清全局检索缓存（评测才需要）。
    result = await run_agent(
        task,
        variant=variant,
        user_context=user_context,
        clear_cache=False,
    )

    citations: list[Citation] = []
    for item in result.get("citations") or []:
        detail = await get_citation(str(item.get("chunk_id") or ""), user_context=user_context)
        if not detail.get("found"):
            # 检索后被删/越权：跳过并记日志。正常情况下不会发生。
            logger.warning(
                "agent citation lookup failed: chunk=%s marker=%s",
                item.get("chunk_id"),
                item.get("marker"),
            )
            continue
        citations.append(Citation(
            document_id=str(detail.get("document_id") or ""),
            document_title=str(detail.get("title") or ""),
            content=str(detail.get("content") or ""),
            score=None,
        ))

    usage = result.get("usage") or {}
    answer = str(result.get("answer") or "")
    status = str(result.get("status") or "error")

    if status != "error" and answer:
        await conversation_memory.add_turn(conversation_id, query, answer)

    return ChatResponse(
        answer=answer,
        citations=citations,
        conversation_id=conversation_id,
        token_usage={
            "prompt_tokens": int(usage.get("tokens_in") or 0),
            "completion_tokens": int(usage.get("tokens_out") or 0),
            "total_tokens": int(usage.get("tokens_in") or 0) + int(usage.get("tokens_out") or 0),
        },
        model_used=MODEL_CONFIG["flash"]["model"],
        reasoning_mode_used="agent-flash",
        is_fallback=False,
        status=status,
        trace=_to_trace(result.get("trace") or []),
        variant=str(result.get("variant") or variant),
        duration_ms=int(usage.get("latency_ms") or 0),
    )
