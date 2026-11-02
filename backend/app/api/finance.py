"""金融查证工作台的 HTTP 接口。

四个端点各自只做一层适配：

- `/finance/chat`：会话命名空间 + 审计 + 金融 Agent；
- `/finance/stats`：覆盖范围（文档/章节/表/结构块分开统计）；
- `/finance/documents`：语料清单；
- `/finance/evidence`：按锚点或表格坐标回查证据。

语料是本地固定文档库，没有通用上传文档的 per-document ACL；仍然复用
`extract_user_context` 做身份与审计，避免出现一条绕过认证的旁路。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.conversation_scope import ConversationNamespace
from app.core.user_context import extract_user_context
from app.models.finance import (
    FinanceChatRequest,
    FinanceChatResponse,
    FinanceEvidenceResponse,
)
from app.services.audit_logger import AuditAction, AuditEvent, AuditStatus, audit_logger
from app.services.finance_corpus import (
    FinanceCorpusError,
    document_inventory,
    get_finance_store,
    index_fingerprint,
    stats as finance_stats,
)
from app.services.finance_entry import build_evidence, run_finance_query
from app.services.finance_rag import run_structured_rag
from app.services.finance_router import decide_strategy
from app.finance.claim_consistency import check_response
from app.finance.query_scope import prepare_finance_scope
from app.services.conversation_memory import conversation_memory
from app.services.finance_history import build_history_payload

logger = logging.getLogger(__name__)
router = APIRouter()

#: 结构检索失败后是否升级到多步查证。产品默认开；关掉退回纯二选一（对照用）。
ESCALATE_ON_STRUCTURED_FAILURE = True


def _escalation_reason(response: FinanceChatResponse) -> Optional[str]:
    """结构检索的结果够不够格直接交付；够就返回 None。

    两条判据，按代价从小到大：

    1. 没拿到可用证据（检索为空 / 执行失败）—— 一定升级，否则用户看到的是
       "资料不足"，而资料其实在库里（实测：资产总计块在块检索里排第 18 位）。
    2. 答了，但结论里的数字对不上它引用的单元格 —— 复用绑定级一致性校验器；
       不升级等于把"有页码的错误数字"直接交付，这是查证类产品最坏的失败形态。
    """
    if response.status == "error":
        return "结构检索执行失败"
    if response.status == "abstained" and not response.citations:
        return "结构检索没有取得可用证据"
    report = check_response(response)
    if report.get("mismatched"):
        detail = next(
            (row for row in report.get("details") or [] if row.get("result") == "mismatch"),
            {},
        )
        return f"结论与引用不一致（{detail.get('raw') or '数值'}）"
    return None


def _parse_values(raw: Optional[str], *, numeric: bool = True) -> list[int | str]:
    if not raw:
        return []
    values: list[int | str] = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        # 列名经常就是 "2025"/"2024"。如果按数字转成 int，会被当成列号
        # 直接越界；所以列参数只按名称解析，行参数才允许行号。
        values.append(int(text) if numeric and text.isdigit() else text)
    return values


@router.post("/finance/chat", response_model=FinanceChatResponse)
async def finance_chat(request: Request, body: FinanceChatRequest):
    user_context = extract_user_context(request)
    conversation_id = body.conversation_id or str(uuid.uuid4())
    scoped_conversation_id = ConversationNamespace(user_context).storage_id(conversation_id)
    started_at = asyncio.get_event_loop().time()
    status = AuditStatus.SUCCESS
    error_message = ""
    response: Optional[FinanceChatResponse] = None
    strategy = body.strategy
    route_reason = ""

    try:
        previous_query = ""
        if body.conversation_id:
            previous = await conversation_memory.get_conversation(scoped_conversation_id)
            if previous and previous.get("turns"):
                last_turn = previous["turns"][-1]
                previous_query = str((last_turn.get("payload") or {}).get("resolved_query") or last_turn.get("query") or "")
        scope = prepare_finance_scope(body.query, get_finance_store().list_documents(), previous_query)
        if scope.blocked_reason:
            response = FinanceChatResponse(
                status="clarified" if scope.clarify else "abstained",
                answer=scope.blocked_reason,
                conversation_id=scoped_conversation_id,
                strategy="structured_rag" if strategy == "auto" else strategy,
                route_reason="检索前检查主体、期间与追问条件",
                resolved_query=scope.resolved_query,
                notes=["请核对本地语料覆盖范围及本轮查询条件。"],
            )
            await conversation_memory.add_turn(
                scoped_conversation_id, body.query, response.answer,
                payload=build_history_payload(response, corpus_fingerprint=index_fingerprint()),
            )
        elif strategy == "auto":
            strategy, route_reason = decide_strategy(scope.resolved_query, get_finance_store())
        elif strategy == "structured_rag":
            route_reason = "显式指定结构检索"
        else:
            route_reason = "显式指定多步查证"

        if response is not None:
            pass
        elif strategy == "structured_rag":
            response = await run_structured_rag(
                query=scope.resolved_query,
                original_query=body.query,
                conversation_id=scoped_conversation_id,
                user_context=user_context,
                route_reason=route_reason,
                corpus_fingerprint=index_fingerprint(),
                # 升级判断要用到 citations 里的单元格，所以先不落库；
                # 决定交付这一次结果后再补写，保证一轮会话只有一条记录。
                persist=False,
            )
            escalation = (
                _escalation_reason(response)
                if ESCALATE_ON_STRUCTURED_FAILURE and body.strategy == "auto"
                else None
            )
            if escalation:
                escalated = await run_finance_query(
                    query=scope.resolved_query,
                    original_query=body.query,
                    conversation_id=scoped_conversation_id,
                    user_context=user_context,
                    max_steps=body.max_steps,
                    search_mode=body.search_mode,
                    route_reason=f"{route_reason}；{escalation}，升级多步查证",
                    corpus_fingerprint=index_fingerprint(),
                )
                escalated.notes = list(escalated.notes or []) + [
                    f"escalated_from_structured_rag:{escalation}"
                ]
                response = escalated
            elif response.status != "error" and response.answer:
                await conversation_memory.add_turn(
                    scoped_conversation_id,
                    body.query,
                    response.answer,
                    payload=build_history_payload(
                        response, corpus_fingerprint=index_fingerprint()
                    ),
                )
        else:
            response = await run_finance_query(
                query=scope.resolved_query,
                original_query=body.query,
                conversation_id=scoped_conversation_id,
                user_context=user_context,
                max_steps=body.max_steps,
                search_mode=body.search_mode,
                route_reason=route_reason,
                corpus_fingerprint=index_fingerprint(),
            )
    except FinanceCorpusError as exc:
        status = AuditStatus.FAILURE
        error_message = str(exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException as exc:
        status = AuditStatus.DENIED if exc.status_code == 403 else AuditStatus.FAILURE
        error_message = str(exc.detail)
        raise
    except Exception as exc:  # noqa: BLE001 - 入口层统一记录并返回 500
        status = AuditStatus.FAILURE
        error_message = str(exc)
        logger.exception("Finance chat failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc
    finally:
        duration_ms = (asyncio.get_event_loop().time() - started_at) * 1000
        await audit_logger.log(
            AuditEvent(
                user_context=user_context,
                action=AuditAction.CHAT_QUERY,
                resource_type="conversation",
                resource_id=conversation_id,
                status=status,
                duration_ms=duration_ms,
                details={
                    "query": body.query,
                    "mode": "finance",
                    "strategy": strategy,
                    "search_mode": body.search_mode,
                    "agent_status": getattr(response, "status", None),
                },
                error_message=error_message,
            )
        )

    assert response is not None
    response.conversation_id = conversation_id
    return response


@router.get("/finance/stats")
async def get_finance_stats():
    try:
        return finance_stats()
    except FinanceCorpusError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Finance stats failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.get("/finance/documents")
async def list_finance_documents():
    try:
        return {"documents": document_inventory()}
    except FinanceCorpusError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Finance documents failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.get("/finance/evidence", response_model=FinanceEvidenceResponse)
async def get_finance_evidence(
    evidence_id: Optional[str] = Query(default=None, description="章节/表格/行组/单元格锚点"),
    table_id: Optional[str] = Query(default=None, description="按表格取证据"),
    rows: Optional[str] = Query(default=None, description="行号或行名，逗号分隔"),
    columns: Optional[str] = Query(default=None, description="列号或列名，逗号分隔"),
):
    if not evidence_id and not table_id:
        raise HTTPException(status_code=400, detail="evidence_id 或 table_id 至少提供一个")

    try:
        store = get_finance_store()
        anchor = evidence_id
        raw: dict = {}
        if not anchor and table_id:
            row_values = _parse_values(rows)
            column_values = _parse_values(columns, numeric=False)
            if row_values:
                selected = store.get_rows(table_id, row_values, column_values or None)
                anchor = (
                    f"{table_id}#rows#"
                    + ",".join(str(index) for index in selected.get("row_indices") or [])
                    + "#cols#"
                    + ",".join(str(index) for index in selected.get("column_indices") or [])
                )
            else:
                anchor = table_id
            raw = {"table_id": table_id}
        assert anchor is not None
        return FinanceEvidenceResponse(evidence=build_evidence(store, anchor, raw))
    except FinanceCorpusError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 未知锚点/坐标越界都是 404
        logger.warning("Finance evidence failed: %s", exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
