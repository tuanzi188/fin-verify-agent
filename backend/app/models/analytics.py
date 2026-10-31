from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List


class AuditLogQueryRequest(BaseModel):
    user_id: Optional[str] = None
    action: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    status: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class AuditLogQueryResponse(BaseModel):
    total: int
    logs: List[Dict[str, Any]]
