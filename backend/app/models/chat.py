from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional, Dict, Any
from enum import Enum
import uuid


class ReasoningMode(str, Enum):
    non_think = "non_think"
    think_high = "think_high"
    think_max = "think_max"


class ChatMode(str, Enum):
    """问答模式。默认 rag，保持现有行为不变。"""

    rag = "rag"
    agent = "agent"


class ChunkingStrategy(str, Enum):
    fixed = "fixed"
    semantic = "semantic"
    structural = "structural"


class DocumentChunk(BaseModel):
    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_id: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict, description="文档信息+内容信息+权限信息")
    embedding: Optional[List[float]] = None
    sparse_embedding: Optional[Dict[str, float]] = None


class ChatRequest(BaseModel):
    # 禁止请求体夹带 mock/inject：故障注入只属于评测，不能从公网打开。
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=10000, description="用户问题")
    conversation_id: Optional[str] = None
    reasoning_mode: Optional[ReasoningMode] = None
    top_k: Optional[int] = Field(default=5, ge=1, le=20)
    temperature: Optional[float] = Field(default=0.1, ge=0.0, le=2.0)
    use_fallback: Optional[bool] = None
    # rag = 现有固定流水线；agent = 工具调用循环 + trace。默认 rag，老客户端无感。
    mode: ChatMode = ChatMode.rag
    max_steps: Optional[int] = Field(default=3, ge=1, le=5, description="agent 模式的最大工具步数")


class Citation(BaseModel):
    document_id: str
    document_title: str
    page: Optional[int] = None
    paragraph: Optional[str] = None
    content: str
    # agent 模式的引用没有重排分：允许 None，不要编一个 0 让前端显示"匹配 0%"。
    score: Optional[float] = None


class AgentTraceStep(BaseModel):
    step: int
    tool: str
    ok: bool
    result_count: int = 0
    latency_ms: int = 0
    injected: Optional[str] = None
    error_kind: Optional[str] = None
    args: Dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = None
    # 结构化证据字段：让前端能直接显示“读了哪张表、哪些行列、新增多少证据”，
    # 而不是从 note/args 里猜。旧客户端忽略这些字段即可。
    result_ids: List[str] = Field(default_factory=list)
    new_result_count: Optional[int] = None
    truncated: bool = False
    table_id: Optional[str] = None
    row_indices: List[int] = Field(default_factory=list)
    column_indices: List[int] = Field(default_factory=list)
    result_chars: int = 0
    payload_chars: int = 0
    evidence_chars_added: int = 0
    duplicate_skipped: int = 0
    policy: Optional[str] = None
    error_detail: Optional[str] = None


class ChatSegment(BaseModel):
    text: str
    citations: List[Citation] = []


class ChatResponse(BaseModel):
    answer: str
    segments: List[ChatSegment] = []
    citations: List[Citation] = []
    reasoning_content: Optional[str] = None
    confidence_score: float = 0.0
    conversation_id: str
    token_usage: Optional[dict] = None
    model_used: str = ""
    reasoning_mode_used: str = ""
    is_fallback: bool = False
    # 以下字段只对 agent 模式有值；rag 模式保持 null，老前端不受影响。
    status: Optional[str] = None
    trace: Optional[List[AgentTraceStep]] = None
    variant: Optional[str] = None
    duration_ms: Optional[int] = None


class DocumentUploadResponse(BaseModel):
    document_id: str
    filename: str
    status: str
    chunks_count: int = 0
    message: str = ""
    task_id: Optional[str] = None
    owner: Optional[str] = None
    is_public: Optional[bool] = False


class DocumentInfo(BaseModel):
    document_id: str
    filename: str
    title: str
    author: Optional[str] = None
    department: Optional[str] = None
    classification: Optional[str] = None
    file_type: str
    file_size: int
    upload_time: str
    chunks_count: int
    status: str
    owner: Optional[str] = None
    is_public: Optional[bool] = False
    allowed_users: Optional[List[str]] = []
    allowed_departments: Optional[List[str]] = []
    allowed_roles: Optional[List[str]] = []


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)
    filters: Optional[dict] = None
    query_type: Optional[str] = Field(default="综合分析", description="检索权重类型，如 概念解释、精确查找、综合分析")


class SearchResult(BaseModel):
    document_id: str
    content: str
    score: float
    metadata: dict


class SearchResponse(BaseModel):
    results: List[SearchResult]
    total: int
    query: str


class ConversationTurn(BaseModel):
    query: str
    answer: str
    timestamp: float
    # 金融查证的结构化结果（要点/引用坐标/计算/轨迹）。旧会话没有这个字段，
    # 前端据此退回 Markdown 渲染；通用 RAG 轮次始终为 None。
    payload: Optional[Dict[str, Any]] = None


class ConversationSummary(BaseModel):
    conversation_id: str
    created_at: float
    last_updated: float
    turn_count: int
    preview: str


class ConversationDetail(BaseModel):
    conversation_id: str
    created_at: float
    turns: List[ConversationTurn]


class ConversationListResponse(BaseModel):
    conversations: List[ConversationSummary]


class ConversationDeleteResponse(BaseModel):
    success: bool
    conversation_id: str
