"""金融查证接口的数据契约。

与通用 `Citation` 分开，是为了不把表格坐标、单位、脚注和计算记录
硬塞进 RAG 的通用响应里；旧客户端继续走 `/chat`，不会受这些字段影响。

契约原则（见 `docs/WEB_UI_REDESIGN_PLAN.md` §3）：

1. 坐标由后端给，前端不解析正文猜行列；
2. 每个数字要点要么有单元格证据，要么有计算记录，要么明确标“未提供”；
3. 页码、表名、单位、脚注随证据一起返回；
4. 拒答也要说明查过什么、缺什么。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.chat import AgentTraceStep


class EvidenceKind(str, Enum):
    paragraph = "paragraph"
    table_rows = "table_rows"
    table_cell = "table_cell"
    document = "document"


class SourceSpan(BaseModel):
    source_id: str
    kind: str = "section"  # section | table
    page: Optional[int] = None
    start: Optional[int] = None
    end: Optional[int] = None
    chars: Optional[int] = None


class TableCell(BaseModel):
    column: str
    column_index: int
    value: Optional[str] = None


class TableRow(BaseModel):
    row: int
    row_name: str = ""
    cells: List[TableCell] = Field(default_factory=list)


class TablePreview(BaseModel):
    table_id: str
    caption: str = ""
    page: Optional[int] = None
    page_end: Optional[int] = None
    unit: str = ""
    unit_source: str = ""
    footnote: str = ""
    columns: List[str] = Field(default_factory=list)
    # 解析器常把表头写成 col2/col4；display_columns 用前几行表头做可读补全，
    # 原始列名仍保留在 columns，审计时能对照。
    display_columns: List[str] = Field(default_factory=list)
    column_indices: List[int] = Field(default_factory=list)
    rows: List[TableRow] = Field(default_factory=list)
    row_indices: List[int] = Field(default_factory=list)
    total_rows: int = 0


class EvidenceRef(BaseModel):
    evidence_id: str
    doc_id: str
    doc_title: str = ""
    company: str = ""
    period: str = ""
    doc_type: str = ""
    kind: EvidenceKind = EvidenceKind.document
    page: Optional[int] = None
    section_id: Optional[str] = None
    path: List[str] = Field(default_factory=list)
    table_id: Optional[str] = None
    table_caption: Optional[str] = None
    row_indices: List[int] = Field(default_factory=list)
    column_indices: List[int] = Field(default_factory=list)
    row_name: Optional[str] = None
    column: Optional[str] = None
    cell_text: Optional[str] = None
    unit: str = ""
    footnote: str = ""
    #: 表级口径/类型：consolidated|parent / balance_sheet|income_statement|...
    statement_scope: str = ""
    statement_type: str = ""
    content: str = ""
    #: 逐字原文摘录。校验规则：必须是 content / 表格行 / 单元格的精确子串。
    #: 页码可以缺，但这句原文不能缺——它才是“有原文依据”的硬证据。
    quote: str = ""
    #: quote 能否在解析产物里逐字找到。False 时只能当线索，不能当证据。
    quote_verified: bool = False
    #: quote 在“同一份文件”里出现的次数（不跨文件去重：多份文件一致是佐证）。
    quote_occurrences: int = 0
    #: 同文件内出现多次、且 locator 不足以区分落点时为 True。
    quote_ambiguous: bool = False
    #: 可选定位。PDF: {"page": 11}；XLSX: {"sheet": "...", "cell": "B12"}；
    #: DOCX: {"paragraph": 37}。locator 只用于展示/跳转，不参与正确性判定。
    locator: Dict[str, Any] = Field(default_factory=dict)
    source_spans: List[SourceSpan] = Field(default_factory=list)
    table_preview: Optional[TablePreview] = None
    # verified=True 只表示“坐标来自解析表格/单元格”，不表示模型结论正确。
    verified: bool = False
    note: Optional[str] = None
    source_url: Optional[str] = None


class FinanceCitation(BaseModel):
    marker: int
    evidence: EvidenceRef


class CalcRecord(BaseModel):
    formula: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    unit: str = ""
    tolerance: Optional[float] = None
    note: str = ""
    calculation_id: str = ""
    result_scale: str = "1"
    input_sources: Dict[str, Any] = Field(default_factory=dict)
    grounding_status: str = "unverified"
    recomputed: bool = False


class Claim(BaseModel):
    text: str
    citation_markers: List[int] = Field(default_factory=list)
    calc_index: Optional[int] = None
    uncertainty: Optional[str] = None


class FinanceChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=10000)
    conversation_id: Optional[str] = None
    max_steps: int = Field(default=10, ge=1, le=20)
    # 线上默认块索引；sections 留给对照与排障，不在普通 UI 暴露。
    search_mode: str = Field(default="blocks", pattern="^(sections|blocks)$")
    # auto 由产品路由决定；agent / structured_rag 仅供实验与排障显式调用。
    strategy: str = Field(default="auto", pattern="^(auto|agent|structured_rag)$")


class FinanceChatResponse(BaseModel):
    status: str = "error"
    answer: str = ""
    claims: List[Claim] = Field(default_factory=list)
    citations: List[FinanceCitation] = Field(default_factory=list)
    calc: List[CalcRecord] = Field(default_factory=list)
    trace: List[AgentTraceStep] = Field(default_factory=list)
    conversation_id: str = ""
    model_used: str = ""
    variant: str = "improved"
    # 产品自动选路结果：structured_rag 或 agent。UI 不暴露固定窗 RAG 选项。
    strategy: str = "agent"
    route_reason: str = ""
    search_mode: str = "blocks"
    duration_ms: int = 0
    token_usage: Optional[dict] = None
    notes: List[str] = Field(default_factory=list)
    verification: Dict[str, Any] = Field(default_factory=dict)
    resolved_query: str = ""


class FinanceDocumentInfo(BaseModel):
    doc_id: str
    title: str = ""
    company: str = ""
    period: str = ""
    doc_type: str = ""
    code: str = ""
    unit: str = ""
    currency: str = ""
    announcement_date: Optional[str] = None
    published_at: Optional[str] = None
    source_url: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    sections: int = 0
    tables: int = 0
    status: str = "parsed"


class FinanceStats(BaseModel):
    documents: int = 0
    companies: int = 0
    periods: List[str] = Field(default_factory=list)
    sections: int = 0
    tables: int = 0
    blocks: int = 0
    prose_blocks: int = 0
    table_blocks: int = 0
    # 固定窗 chunk 属于评测侧口径，线上不重复切一遍；缺失时前端不展示。
    chunks: Optional[int] = None
    index_fingerprint: Optional[str] = None
    index_version: Optional[str] = None
    corpus_ready: bool = False
    company_names: List[str] = Field(default_factory=list)


class FinanceEvidenceResponse(BaseModel):
    evidence: EvidenceRef


class LabArmSummary(BaseModel):
    name: str
    label: str = ""
    pass_at_1: Optional[float] = None
    pass_pow_k: Optional[float] = None
    tasks: int = 0
    runs: int = 0
    tokens_per_run: Optional[float] = None
    median_latency_ms: Optional[float] = None
    latency_mean_ms: Optional[float] = None
    avg_steps: Optional[float] = None
    hard_gates: Dict[str, Any] = Field(default_factory=dict)
    manifest: Dict[str, Any] = Field(default_factory=dict)


class LabDimension(BaseModel):
    name: str
    tasks: int = 0
    baseline_pass_at_1: Optional[float] = None
    improved_pass_at_1: Optional[float] = None
    delta: Optional[float] = None
    delta_exceeds_ci_width: Optional[bool] = None


class LabRunSummary(BaseModel):
    available: bool = False
    comparison_kind: str = "unavailable"
    left_label: str = "对照臂"
    right_label: str = "Agent"
    generated_at: Optional[str] = None
    verdict: Optional[str] = None
    metric: Optional[str] = None
    tasks_compared: int = 0
    arms: List[LabArmSummary] = Field(default_factory=list)
    dimensions: List[LabDimension] = Field(default_factory=list)
    mcnemar: Dict[str, Any] = Field(default_factory=dict)
    # 固定窗 RAG 只作为可选辅助基线出现，不进入主比较 arms。
    optional_baseline: Optional[LabArmSummary] = None
    optional_baseline_label: Optional[str] = None
    artifact: Optional[str] = None
    compare_artifact: Optional[str] = None
    message: Optional[str] = None


class FinanceDocumentUploadResponse(BaseModel):
    """上传解析结果：只回传用户能判断“能不能用”的字段。"""

    doc_id: str
    filename: str
    status: str
    sections: int = 0
    tables: int = 0
    raw_chars: int = 0
    pipeline_version: str = ""
    warnings: List[str] = Field(default_factory=list)
    message: str = ""
