import uuid
import json
import logging
import asyncio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.models.chat import (
    ChatRequest, ChatResponse, ChatMode, SearchRequest, SearchResponse,
    ConversationListResponse, ConversationDetail, ConversationDeleteResponse
)
from app.core.user_context import extract_user_context
from app.core.conversation_scope import ConversationNamespace
from app.core.exceptions import UpstreamAPIError
from app.services.rag_engine import rag_query, rag_query_stream
from app.services.agent_entry import run_agent_query
from app.services.hybrid_search import hybrid_retriever
from app.services.model_provider import classify_intent as model_classify_intent, select_reasoning_mode as model_select_reasoning_mode
from app.services.conversation_memory import conversation_memory
from app.services.access_control import filter_accessible_records
from app.services.audit_logger import audit_logger, AuditAction, AuditStatus, AuditEvent

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/auth/me")
async def authenticated_profile(request: Request):
    verified_profile = extract_user_context(request)
    return {
        "userId": verified_profile.user_id,
        "roles": verified_profile.roles,
        "department": verified_profile.department,
    }


def _handle_endpoint_error(operation: str, exc: Exception) -> None:
    """记录异常并返回合适的 HTTP 错误。

    - UpstreamAPIError（如 API Key 无效、余额不足）会透传上游状态码和原因。
    - 其它未预期异常返回 500，并在日志中保留完整堆栈。
    """
    if isinstance(exc, UpstreamAPIError):
        logger.warning(
            "%s upstream error [%s]: %s",
            operation,
            exc.status_code,
            exc.detail,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    logger.error("%s error: %s", operation, exc, exc_info=True)
    raise HTTPException(status_code=500, detail="Internal server error") from exc


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest):
    user_context = extract_user_context(request)
    conversation_id = body.conversation_id or str(uuid.uuid4())
    scoped_conversation_id = ConversationNamespace(user_context).storage_id(conversation_id)
    start_time = asyncio.get_event_loop().time()
    status = AuditStatus.SUCCESS
    error_message = ""
    response = None

    try:
        if body.mode == ChatMode.agent:
            response = await run_agent_query(
                query=body.query,
                conversation_id=scoped_conversation_id,
                user_context=user_context,
                max_steps=body.max_steps or 3,
            )
        else:
            response = await rag_query(
                query=body.query,
                conversation_id=scoped_conversation_id,
                reasoning_mode=body.reasoning_mode.value if body.reasoning_mode else None,
                top_k=body.top_k or 5,
                temperature=body.temperature or 0.1,
                use_fallback=body.use_fallback,
                user_context=user_context,
            )
    except HTTPException as e:
        status = AuditStatus.DENIED if e.status_code == 403 else AuditStatus.FAILURE
        error_message = str(e.detail)
        raise
    except Exception as e:
        status = AuditStatus.FAILURE
        error_message = str(e)
        _handle_endpoint_error("Chat", e)
    finally:
        duration_ms = (asyncio.get_event_loop().time() - start_time) * 1000
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
                    "mode": body.mode.value,
                    "agent_status": getattr(response, "status", None) if body.mode == ChatMode.agent else None,
                },
                error_message=error_message,
            )
        )

    response.conversation_id = conversation_id
    return response


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest):
    user_context = extract_user_context(request)
    if body.mode == ChatMode.agent:
        # v1 的 Agent 是非流式：宁可明确拒绝，也不要让客户端以为自己在用 agent。
        raise HTTPException(
            status_code=400,
            detail="agent 模式暂不支持流式，请使用 POST /api/v1/chat（mode=agent）",
        )
    conversation_id = body.conversation_id or str(uuid.uuid4())
    stream_storage_id = ConversationNamespace(user_context).storage_id(conversation_id)
    start_time = asyncio.get_event_loop().time()

    async def event_stream():
        result_holder = {}
        stream_error = None
        try:
            async for token in rag_query_stream(
                query=body.query,
                conversation_id=stream_storage_id,
                reasoning_mode=body.reasoning_mode.value if body.reasoning_mode else None,
                top_k=body.top_k or 5,
                temperature=body.temperature or 0.1,
                use_fallback=body.use_fallback,
                result_holder=result_holder,
                user_context=user_context,
            ):
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            citations_data = [c.model_dump() for c in result_holder.get("citations", [])]
            stream_done_payload = {
                'type': 'done',
                'conversation_id': conversation_id,
                'citations': citations_data,
                'confidence_score': result_holder.get('confidence', 0),
                'model_used': result_holder.get('model_used', ''),
                'reasoning_content': result_holder.get('reasoning_content', ''),
                'is_fallback': result_holder.get('is_fallback', False),
            }
            yield f"data: {json.dumps(stream_done_payload)}\n\n"
        except UpstreamAPIError as e:
            stream_error = e
            logger.warning("Stream upstream error [%s]: %s", e.status_code, e.detail)
            yield f"data: {json.dumps({'type': 'error', 'code': e.status_code, 'content': e.detail})}\n\n"
        except Exception as e:
            stream_error = e
            logger.error("Stream error: %s", e, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'code': 500, 'content': 'Internal server error'})}\n\n"
        finally:
            duration_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            status = AuditStatus.FAILURE if stream_error else AuditStatus.SUCCESS
            await audit_logger.log(
                AuditEvent(
                    user_context=user_context,
                    action=AuditAction.CHAT_STREAM,
                    resource_type="conversation",
                    resource_id=conversation_id,
                    status=status,
                    duration_ms=duration_ms,
                    details={"query": body.query},
                    error_message=str(stream_error) if stream_error else "",
                )
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/search", response_model=SearchResponse)
async def search(request: Request, body: SearchRequest):
    user_context = extract_user_context(request)
    start_time = asyncio.get_event_loop().time()

    try:
        results = await hybrid_retriever.retrieve(
            query=body.query,
            top_k=body.top_k or 10,
            filters=body.filters,
            query_type=body.query_type or "综合分析",
        )
    except HTTPException:
        raise
    except Exception as e:
        _handle_endpoint_error("Search", e)

    # 文档级权限过滤
    if not user_context.is_admin:
        results = filter_accessible_records(user_context, results)

    search_results = [
        {
            "document_id": r.get("chunk_id", ""),
            "content": r.get("content", ""),
            "score": r.get("rrf_score", r.get("score", 0)),
            "metadata": r.get("metadata", {}),
        }
        for r in results
    ]

    duration_ms = (asyncio.get_event_loop().time() - start_time) * 1000
    await audit_logger.log(
        AuditEvent(
            user_context=user_context,
            action=AuditAction.DOCUMENT_SEARCH,
            resource_type="document",
            status=AuditStatus.SUCCESS,
            duration_ms=duration_ms,
            details={"query": body.query, "result_count": len(search_results)},
        )
    )

    return SearchResponse(results=search_results, total=len(search_results), query=body.query)


@router.post("/classify")
async def classify_query(query: str):
    intent = await model_classify_intent(query)
    mode = model_select_reasoning_mode(intent)
    return {"intent": intent, "recommended_mode": mode}


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(request: Request):
    user_context = extract_user_context(request)
    start_time = asyncio.get_event_loop().time()
    try:
        stored_conversations = await conversation_memory.list_conversations()
        conversation_namespace = ConversationNamespace(user_context)
        conversations = []
        for stored_conversation in stored_conversations:
            visible_conversation_id = conversation_namespace.public_id(stored_conversation["conversation_id"])
            if visible_conversation_id is not None:
                conversations.append({**stored_conversation, "conversation_id": visible_conversation_id})
    except HTTPException:
        raise
    except Exception as e:
        _handle_endpoint_error("List conversations", e)
    finally:
        duration_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        await audit_logger.log(
            AuditEvent(
                user_context=user_context,
                action=AuditAction.CONVERSATION_LIST,
                resource_type="conversation",
                status=AuditStatus.SUCCESS,
                duration_ms=duration_ms,
                details={"count": len(conversations) if 'conversations' in locals() else 0},
            )
        )
    return ConversationListResponse(conversations=conversations)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str, request: Request):
    user_context = extract_user_context(request)
    start_time = asyncio.get_event_loop().time()
    detail_storage_id = ConversationNamespace(user_context).storage_id(conversation_id)
    conversation = await conversation_memory.get_conversation(detail_storage_id)
    status = AuditStatus.SUCCESS if conversation else AuditStatus.FAILURE
    duration_ms = (asyncio.get_event_loop().time() - start_time) * 1000
    await audit_logger.log(
        AuditEvent(
            user_context=user_context,
            action=AuditAction.CONVERSATION_VIEW,
            resource_type="conversation",
            resource_id=conversation_id,
            status=status,
            duration_ms=duration_ms,
            error_message="" if conversation else "Conversation not found",
        )
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetail(**{**conversation, "conversation_id": conversation_id})


@router.delete("/conversations/{conversation_id}", response_model=ConversationDeleteResponse)
async def delete_conversation(conversation_id: str, request: Request):
    user_context = extract_user_context(request)
    start_time = asyncio.get_event_loop().time()
    status = AuditStatus.SUCCESS
    error_message = ""
    try:
        delete_storage_id = ConversationNamespace(user_context).storage_id(conversation_id)
        if await conversation_memory.get_conversation(delete_storage_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        await conversation_memory.clear(delete_storage_id)
    except HTTPException as e:
        status = AuditStatus.DENIED if e.status_code == 403 else AuditStatus.FAILURE
        error_message = str(e.detail)
        raise
    except Exception as e:
        status = AuditStatus.FAILURE
        error_message = str(e)
        _handle_endpoint_error("Delete conversation", e)
    finally:
        duration_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        await audit_logger.log(
            AuditEvent(
                user_context=user_context,
                action=AuditAction.CONVERSATION_DELETE,
                resource_type="conversation",
                resource_id=conversation_id,
                status=status,
                duration_ms=duration_ms,
                error_message=error_message,
            )
        )
    return ConversationDeleteResponse(success=True, conversation_id=conversation_id)
