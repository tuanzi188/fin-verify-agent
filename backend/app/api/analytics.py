"""审计日志查询接口。

原来的 `/analytics/{overview,documents,conversations,tasks,graph}` 五个端点
随 `AnalyticsDashboard` 一起下线：前端已切到 `/lab`，这些端点没有调用方。
这里只保留 `/analytics/audit`，它是 AuditLogs 页面的唯一数据源。
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import require_administrator
from app.core.user_context import extract_user_context
from app.models.analytics import AuditLogQueryRequest, AuditLogQueryResponse
from app.services.audit_logger import AuditAction, AuditEvent, AuditStatus, audit_logger

router = APIRouter(dependencies=[Depends(require_administrator)])


async def _log_audit_query(request: Request, details: dict = None):
    user_context = extract_user_context(request)
    await audit_logger.log(
        AuditEvent(
            user_context=user_context,
            action=AuditAction.ANALYTICS_VIEW,
            resource_type="analytics",
            status=AuditStatus.SUCCESS,
            details=details or {},
        )
    )


@router.post("/analytics/audit", response_model=AuditLogQueryResponse)
async def query_audit_logs(request: Request, body: AuditLogQueryRequest):
    """审计日志查询（建议仅管理员使用）。"""
    user_context = extract_user_context(request)
    if not user_context.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    logs = await audit_logger.query(
        user_id=body.user_id,
        action=body.action,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        status=body.status,
        start_time=body.start_time,
        end_time=body.end_time,
        limit=body.limit,
        offset=body.offset,
    )
    await _log_audit_query(
        request, {"metric": "audit", "result_count": len(logs)}
    )
    return AuditLogQueryResponse(total=len(logs), logs=logs)
