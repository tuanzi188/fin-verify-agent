"""认证与会话历史接口。

通用 `/chat`、`/chat/stream`、`/search`、`/classify` 已下线：产品统一走
`/finance/chat`。这里只保留 `/auth/me` 与金融查证仍需要的会话列表、详情、删除。
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.conversation_scope import ConversationNamespace
from app.core.exceptions import UpstreamAPIError
from app.core.user_context import extract_user_context
from app.models.chat import (
    ConversationDeleteResponse,
    ConversationDetail,
    ConversationListResponse,
)
from app.services.audit_logger import AuditAction, AuditEvent, AuditStatus, audit_logger
from app.services.conversation_memory import conversation_memory

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
    """记录异常并返回合适的 HTTP 错误。"""
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


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(request: Request):
    user_context = extract_user_context(request)
    start_time = asyncio.get_event_loop().time()
    try:
        stored_conversations = await conversation_memory.list_conversations()
        conversation_namespace = ConversationNamespace(user_context)
        conversations = []
        for stored_conversation in stored_conversations:
            visible_conversation_id = conversation_namespace.public_id(
                stored_conversation["conversation_id"]
            )
            if visible_conversation_id is not None:
                conversations.append(
                    {**stored_conversation, "conversation_id": visible_conversation_id}
                )
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
                details={"count": len(conversations) if "conversations" in locals() else 0},
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
