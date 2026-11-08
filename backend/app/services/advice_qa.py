"""观点/建议类问答。

与查证、概念的分工：

- 查证：有原文、有坐标、可回查；
- 概念：解释术语，不引用本地材料；
- 观点：可以给分析意见，但**没有可回查坐标**，因此：

1. 免责声明由服务端强制追加，不依赖模型自觉；
2. 观点与披露事实必须分开，目标价/评级必须标来源或标推测；
3. 不得承诺收益、不得保证涨跌。

`disclaimer` 与 `advisory` 同时写进响应：前端可以单独渲染免责条，
不解析正文也不会丢合规信息。
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

from app.models.finance import FinanceChatResponse

DISCLAIMER_VERSION = "2026-11-07"
DISCLAIMER_TEXT = (
    "本回答为基于公开信息的分析意见，不构成投资建议，也不承诺任何收益。"
    "市场有风险，决策需自主。"
)

_SYSTEM_PROMPT = """你是金融信息分析助手。回答观点/建议类问题，遵守以下规则：

1. 把“披露事实”和“分析意见”分开写；事实要能指向公开披露，意见要明确标为观点。
2. 可以给分析框架和倾向性判断，但不得承诺收益、不得保证涨跌、不得使用“稳赚”“必涨”等表述。
3. 涉及目标价、评级、机构观点时，必须注明来源与日期；没有来源的数字只能标为“推测”，不能写成事实。
4. 至少列出两条主要风险或不确定性。
5. 不要伪造公司披露数据；不确定就说不确定。
"""


def _usage(started: float, usage: dict | None) -> dict:
    usage = usage or {}
    return {
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "llm_calls": 1,
        "tokens_in": int(usage.get("prompt_tokens") or usage.get("tokens_in") or 0),
        "tokens_out": int(usage.get("completion_tokens") or usage.get("tokens_out") or 0),
        "web_queries": 0,
    }


async def answer_advice(
    *,
    query: str,
    conversation_id: str,
    route_reason: str = "",
    chat: Optional[Callable[..., Awaitable[Any]]] = None,
) -> FinanceChatResponse:
    started = time.perf_counter()
    if chat is None:
        from app.services.model_provider import chat as model_chat

        chat = model_chat
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": str(query or "")},
    ]
    try:
        reply, error, usage = await chat(
            messages,
            reasoning_mode="flash",
            temperature=0.3,
            max_tokens=2048,
        )
    except Exception as exc:  # noqa: BLE001 - 单条失败不能把整批拖垮
        return FinanceChatResponse(
            status="error",
            answer=f"观点生成失败：{type(exc).__name__}" + "\n\n" + DISCLAIMER_TEXT,
            conversation_id=conversation_id,
            variant="advice",
            strategy="advice",
            route_reason=route_reason,
            resolved_query=str(query or ""),
            token_usage=_usage(started, {}),
            notes=["advice:model_error"],
            disclaimer=DISCLAIMER_TEXT,
            advisory={"kind": "opinion", "disclaimer_version": DISCLAIMER_VERSION},
        )

    text = str(reply or "").strip()
    if error or not text:
        status = "error"
        note = "advice:empty_answer" if not error else "advice:model_error"
        body = text or f"观点生成失败：{error or '空回答'}"
    else:
        status = "answered"
        note = "advice:opinion"
        body = text
    answer = body + "\n\n" + DISCLAIMER_TEXT
    return FinanceChatResponse(
        status=status,
        answer=answer,
        conversation_id=conversation_id,
        model_used="",
        variant="advice",
        strategy="advice",
        route_reason=route_reason,
        resolved_query=str(query or ""),
        token_usage=_usage(started, usage if not error else {}),
        notes=[note],
        answer_lead="",
        disclaimer=DISCLAIMER_TEXT,
        advisory={
            "kind": "opinion",
            "disclaimer_version": DISCLAIMER_VERSION,
            "requires_disclaimer": True,
        },
    )
