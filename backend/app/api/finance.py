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

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.conversation_scope import ConversationNamespace
from app.core.user_context import extract_user_context
from app.finance.query_scope import prepare_finance_scope
from app.finance.refusal import from_verification
from app.finance.refusal import payload as refusal_payload
from app.finance.routing_policy import initial_decision, response_upgrade
from app.models.finance import (
    FinanceChatRequest,
    FinanceChatResponse,
    FinanceDocumentUploadResponse,
    FinanceEvidenceResponse,
)
from app.services.audit_logger import AuditAction, AuditEvent, AuditStatus, audit_logger
from app.services.concept_qa import answer_concept
from app.services.conversation_memory import conversation_memory
from app.services.finance_corpus import (
    FinanceCorpusError,
    document_inventory,
    get_finance_store,
    index_fingerprint,
    reset_cache,
)
from app.services.finance_corpus import (
    stats as finance_stats,
)
from app.services.finance_entry import build_evidence, run_finance_query
from app.services.finance_history import build_history_payload
from app.services.finance_rag import run_structured_rag
from app.services.finance_upload import UploadError, save_and_parse
from app.services.market_brief import build_market_brief, should_brief_on_local_miss

logger = logging.getLogger(__name__)
router = APIRouter()

#: 结构检索失败后是否升级到多步查证。产品默认开；关掉退回纯二选一（对照用）。
ESCALATE_ON_STRUCTURED_FAILURE = True


def _escalation_reason(response: FinanceChatResponse) -> str | None:
    """只接收可枚举的修复理由；执行故障、澄清和语料越界不自动升级。"""
    decision = response_upgrade(response)
    return decision["reason"] if decision["upgrade"] else None


def _parse_values(raw: str | None, *, numeric: bool = True) -> list[int | str]:
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
    response: FinanceChatResponse | None = None
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
        initial = initial_decision(body.query, get_finance_store().list_documents(), previous_query)
        if body.strategy == "auto" and initial.initial_path in ("clarify", "abstain"):
            from dataclasses import replace

            scope = replace(scope, blocked_reason=initial.reason, clarify=initial.initial_path == "clarify")
        if body.strategy == "auto" and initial.initial_path == "concept":
            # 概念题不检索、不引用。必须在 scope.blocked_reason 之前处理：
            # prepare_finance_scope 会把「什么是」当成主体，先判范围就永远到不了这里。
            response = await answer_concept(
                query=body.query,
                conversation_id=scoped_conversation_id,
                route_reason=initial.reason,
                allow_web=body.allow_web,
                must_web=initial.must_web,
            )
            await conversation_memory.add_turn(
                scoped_conversation_id,
                body.query,
                response.answer,
                payload=build_history_payload(
                    # 概念答案不依赖语料：不写指纹，语料变更时不会被升级成「需人工复核」。
                    response,
                    corpus_fingerprint="",
                ),
            )
        elif (
            scope.blocked_reason
            and body.allow_web
            and not scope.clarify
            and should_brief_on_local_miss(body.query)
        ):
            # 本地语料覆盖不到、且用户显式允许联网：转公开信息综述。
            # 综述永远不给 `[n]` 引用，也不冒充披露数据；免责由服务端追加。
            brief = await build_market_brief(
                body.query,
                company=scope.companies[0] if scope.companies else None,
                period=str(max(scope.years)) if scope.years else None,
                allow_web=True,
                local_citable_count=0,
            )
            notes = list(brief.warnings)
            if brief.error:
                notes.append(f"综述不可用：{brief.error}")
            try:
                local_docs = len(get_finance_store().list_documents())
            except Exception:  # noqa: BLE001 - 回显覆盖面失败不能影响回答
                local_docs = 0
            notes.insert(
                0,
                f"已查来源：本地 {local_docs} 份文档；"
                f"联网检索 {brief.queries_used} 次（{brief.provider or '未执行'}）",
            )
            response = FinanceChatResponse(
                status="answered" if brief.ok else "abstained",
                answer=brief.summary or scope.blocked_reason,
                conversation_id=scoped_conversation_id,
                strategy="market_brief",
                route_reason="本地语料未覆盖，转入公开信息综述（未核实）",
                resolved_query=scope.resolved_query,
                market_brief=brief.to_dict(),
                refusal=None
                if brief.ok
                else refusal_payload(
                    "insufficient_evidence",
                    detail=brief.skipped_reason or brief.error or "",
                ),
                notes=notes,
            )
            await conversation_memory.add_turn(
                scoped_conversation_id,
                body.query,
                response.answer,
                payload=build_history_payload(
                    response, corpus_fingerprint=index_fingerprint()
                ),
            )
        elif scope.blocked_reason:
            notes = ["请核对本地语料覆盖范围及本轮查询条件。"]
            if body.allow_web:
                # 纯数值题即使开了联网也不转综述：媒体数字没有坐标，不能当答案。
                notes.append(
                    "联网综述只汇总市场说法，不提供可核对数值；"
                    "如需实数，请先入库对应的原始报告。"
                )
            response = FinanceChatResponse(
                status="clarified" if scope.clarify else "abstained",
                answer=scope.blocked_reason,
                conversation_id=scoped_conversation_id,
                strategy="structured_rag" if strategy == "auto" else strategy,
                route_reason="检索前检查主体、期间与追问条件",
                resolved_query=scope.resolved_query,
                refusal=refusal_payload(
                    initial.reason_code or scope.blocked_code or "out_of_scope",
                    detail=scope.blocked_reason,
                ),
                notes=notes,
            )
            await conversation_memory.add_turn(
                scoped_conversation_id, body.query, response.answer,
                payload=build_history_payload(response, corpus_fingerprint=index_fingerprint()),
            )
        elif strategy == "auto":
            strategy, route_reason = initial.initial_path, initial.reason
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
                preliminary_usage = response.token_usage or {}
                preliminary_duration = response.duration_ms
                escalated = await run_finance_query(
                    query=scope.resolved_query,
                    original_query=body.query,
                    conversation_id=scoped_conversation_id,
                    user_context=user_context,
                    max_steps=body.max_steps,
                    search_mode=body.search_mode,
                    route_reason=f"{route_reason}；{escalation}，升级多步查证",
                    corpus_fingerprint=index_fingerprint(),
                    persist=False,
                )
                escalated.notes = list(escalated.notes or []) + [
                    f"escalated_from_structured_rag:{escalation}"
                ]
                escalated.duration_ms += preliminary_duration
                escalated.token_usage = {
                    **(escalated.token_usage or {}),
                    **{key: int((escalated.token_usage or {}).get(key) or 0) + int(preliminary_usage.get(key) or 0)
                       for key in ("prompt_tokens", "completion_tokens", "total_tokens", "llm_calls")},
                }
                escalated.verification["routing"] = {
                    "initial_path": "structured_rag", "upgraded": True,
                    "reason": escalation, "preliminary_duration_ms": preliminary_duration,
                    "preliminary_token_usage": preliminary_usage,
                }
                response = escalated
                if response.status != "error" and response.answer:
                    await conversation_memory.add_turn(
                        scoped_conversation_id, body.query, response.answer,
                        payload=build_history_payload(response, corpus_fingerprint=index_fingerprint()),
                    )
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
    except Exception as exc:
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
    # 检索/生成之后才失败的轮次在这里补类别：能拿到 issue 就按"冲突优先"判，
    # 拿不到就留空，不硬造一个看起来像结论的标签。
    if response.status == "abstained" and not response.refusal:
        response.refusal = from_verification(response.verification)
    response.conversation_id = conversation_id
    return response


@router.get("/finance/stats")
async def get_finance_stats():
    try:
        return finance_stats()
    except FinanceCorpusError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Finance stats failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.get("/finance/documents")
async def list_finance_documents():
    try:
        return {"documents": document_inventory()}
    except FinanceCorpusError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Finance documents failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.post(
    "/finance/documents",
    response_model=FinanceDocumentUploadResponse,
    status_code=201,
)
async def upload_finance_document(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI 依赖注入的标准写法
    company: str = Form(""),
    period: str = Form(""),
    doc_type: str = Form("annual_report"),
    force: bool = Form(False),
):
    """上传一份 PDF：解析 → QC → 并入运行时语料。单用户演示级，不做 per-user ACL。"""
    data = await file.read()
    try:
        result = await run_in_threadpool(
            save_and_parse,
            file.filename or "upload.pdf",
            data,
            company=company,
            period=period,
            doc_type=doc_type,
            force=force,
        )
    except UploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception as exc:
        logger.exception("Finance document upload failed")
        raise HTTPException(status_code=500, detail="Internal server error") from exc
    # 语料/块索引都是进程内缓存：上传成功后必须清一次，下一次请求才会看到新文档。
    reset_cache()
    return result


@router.get("/finance/evidence", response_model=FinanceEvidenceResponse)
async def get_finance_evidence(
    evidence_id: str | None = Query(default=None, description="章节/表格/行组/单元格锚点"),
    table_id: str | None = Query(default=None, description="按表格取证据"),
    rows: str | None = Query(default=None, description="行号或行名，逗号分隔"),
    columns: str | None = Query(default=None, description="列号或列名，逗号分隔"),
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
    except Exception as exc:
        logger.warning("Finance evidence failed: %s", exc)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
