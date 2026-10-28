"""金融查证工具层：读章节、读表格、读单元格、受限计算、本地检索。

与 `retrieval.py` 的分工：
- `retrieval.py` 面向制度/文本 chunk；
- 这里面向**带页码与表格结构**的金融文档，返回值必须能直接进证据链。

工具描述就是模型的说明书，所以每条都写清"不许猜、必须用 calculate"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.finance.calc import CalculationError, calculate as run_calculation
from app.finance.store import FinanceStore, FinanceStoreError


@dataclass
class FinanceOutcome:
    tool: str
    ok: bool
    data: Any = None
    error_kind: Optional[str] = None
    error_detail: str = ""


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, CalculationError):
        return "bad_args"
    if isinstance(exc, FinanceStoreError):
        text = str(exc)
        if "未知" in text or "找不到" in text or "越界" in text:
            return "not_found"
        return "bad_args"
    return "upstream_error"


class FinanceToolbox:
    """金融工具集合。没有网络、没有 LLM，纯本地确定性操作。"""

    def __init__(self, store: FinanceStore):
        self.store = store

    def list_documents(self) -> list[dict]:
        return self.store.list_documents()

    def read_section(self, section_id: str) -> dict:
        return self.store.read_section(section_id)

    def get_table(self, table_id: str) -> dict:
        return self.store.get_table(table_id)

    def get_cell(self, table_id: str, row: int | str, column: int | str) -> dict:
        return self.store.get_cell(table_id, row, column)

    def search_sections(
        self,
        query: str,
        *,
        doc_id: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        return self.store.search_sections(query, doc_id=doc_id, top_k=top_k)

    def calculate(self, expression: str, inputs: dict[str, Any] | None = None) -> dict:
        return run_calculation(expression, inputs)

    async def call(self, name: str, arguments: dict | None = None) -> FinanceOutcome:
        """统一入口：异常不抛出，转成可归因的 error_kind。"""
        arguments = arguments or {}
        try:
            if name == "list_documents":
                data = self.list_documents()
            elif name == "search_sections":
                data = self.search_sections(
                    str(arguments.get("query") or ""),
                    doc_id=arguments.get("doc_id") or None,
                    top_k=int(arguments.get("top_k") or 5),
                )
            elif name == "read_section":
                data = self.read_section(str(arguments.get("section_id") or ""))
            elif name == "get_table":
                data = self.get_table(str(arguments.get("table_id") or ""))
            elif name == "get_cell":
                data = self.get_cell(
                    str(arguments.get("table_id") or ""),
                    arguments.get("row"),
                    arguments.get("column"),
                )
            elif name == "calculate":
                data = self.calculate(
                    str(arguments.get("expression") or ""),
                    arguments.get("inputs") or {},
                )
            else:
                raise CalculationError(f"未知工具: {name}")
        except Exception as exc:  # noqa: BLE001 - 工具层必须可归因
            return FinanceOutcome(
                tool=name,
                ok=False,
                error_kind=_error_kind(exc),
                error_detail=str(exc)[:300],
            )
        return FinanceOutcome(tool=name, ok=True, data=data)


# ---------------------------------------------------------------------------
# 工具描述（模型看到的原文）
# ---------------------------------------------------------------------------

LIST_DOCUMENTS_SPEC = {
    "type": "function",
    "function": {
        "name": "list_documents",
        "description": "列出本地金融文档库里的文档（doc_id、标题、公司、报告期、文档类型）。先看有哪些文档，再决定读哪一份。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

SEARCH_SECTIONS_SPEC = {
    "type": "function",
    "function": {
        "name": "search_sections",
        "description": (
            "在本地金融文档里按关键词找章节，返回章节 id、标题路径、页码和摘要。"
            "这只是定位，不是证据；回答前必须用 read_section 或 get_table 读原文。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，尽量带公司/年份/指标名"},
                "doc_id": {"type": "string", "description": "限定某份文档；不传则全库搜"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
        },
    },
}

READ_SECTION_SPEC = {
    "type": "function",
    "function": {
        "name": "read_section",
        "description": "按 section_id 读章节原文，返回 doc_id、标题路径、起止页码和正文。引用时必须带页码。",
        "parameters": {
            "type": "object",
            "properties": {"section_id": {"type": "string"}},
            "required": ["section_id"],
        },
    },
}

GET_TABLE_SPEC = {
    "type": "function",
    "function": {
        "name": "get_table",
        "description": "按 table_id 读整张表，返回列名、行、页码和标题。读数字优先用表格，不要从正文猜。",
        "parameters": {
            "type": "object",
            "properties": {"table_id": {"type": "string"}},
            "required": ["table_id"],
        },
    },
}

GET_CELL_SPEC = {
    "type": "function",
    "function": {
        "name": "get_cell",
        "description": (
            "按 table_id + 行 + 列读单元格，返回数值、行名、列名、表格标题、页码和 bbox。"
            "行可以传行号，也可以传首列名称（如“营业收入”）；列可以传列号或列名（如“2025”）。"
            "回答里的证据必须带 table_caption/row_name/column/cell_text/page，不能只写 cell 编号。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
                "row": {"type": ["integer", "string"], "description": "行号或首列名称"},
                "column": {"type": ["integer", "string"], "description": "列号或列名"},
            },
            "required": ["table_id", "row", "column"],
        },
    },
}

CALCULATE_SPEC = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "执行四则运算，返回公式、输入和结果。所有比率、同比、环比、勾稽都必须调用它，"
            "不要心算。inputs 是变量表，例如 {\"current\": \"12.34\", \"previous\": \"10.00\"}；"
            "百分数可以写成 \"23.4%\"。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "只含数字、变量名、+ - * / % 和括号"},
                "inputs": {
                    "type": "object",
                    "description": "变量名 → 数字或数字字符串",
                    "additionalProperties": {"type": ["number", "string"]},
                },
            },
            "required": ["expression"],
        },
    },
}

FINANCE_TOOL_SPECS = [
    LIST_DOCUMENTS_SPEC,
    SEARCH_SECTIONS_SPEC,
    READ_SECTION_SPEC,
    GET_TABLE_SPEC,
    GET_CELL_SPEC,
    CALCULATE_SPEC,
]
