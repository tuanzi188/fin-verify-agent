"""金融查证工具层：读章节、读表格、读单元格、受限计算、本地检索。

与 `retrieval.py` 的分工：
- `retrieval.py` 面向制度/文本 chunk；
- 这里面向**带页码与表格结构**的金融文档，返回值必须能直接进证据链。

工具描述就是模型的说明书，所以每条都写清"不许猜、必须用 calculate"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from app.finance.block_index import BlockIndex
from app.finance.calc import CalculationError
from app.finance.calc import calculate as run_calculation
from app.finance.store import FinanceStore, FinanceStoreError, _tokenize


@dataclass
class FinanceOutcome:
    tool: str
    ok: bool
    data: Any = None
    error_kind: Optional[str] = None
    error_detail: str = ""


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return "not_found"
    if isinstance(exc, CalculationError):
        return "bad_args"
    if isinstance(exc, FinanceStoreError):
        text = str(exc)
        if "未知" in text or "找不到" in text or "越界" in text:
            return "not_found"
        return "bad_args"
    return "upstream_error"


def _table_fill_ratio(table: dict) -> float:
    rows = table.get("rows") or []
    total = sum(len(row) for row in rows)
    if not total:
        return 0.0
    filled = sum(1 for row in rows for cell in row if str(cell or "").strip())
    return filled / total


class FinanceToolbox:
    """金融工具集合。没有网络、没有 LLM，纯本地确定性操作。"""

    def __init__(self, store: FinanceStore, block_index: BlockIndex | None = None):
        self.store = store
        # 线上每次请求都会新建 toolset（calcs / 读取缓存必须隔离），
        # 但块索引是只读的、构建一次要几秒，所以允许从外面注入共享实例。
        self._block_index: BlockIndex | None = block_index

    @property
    def block_index(self) -> BlockIndex:
        if self._block_index is None:
            self._block_index = BlockIndex(self.store)
        return self._block_index

    def list_documents(self) -> list[dict]:
        return self.store.list_documents()

    def read_section(self, section_id: str) -> dict:
        return self.store.read_section(section_id)

    def get_table(self, table_id: str) -> dict:
        return self.store.get_table(table_id)

    def get_cell(self, table_id: str, row: int | str, column: int | str) -> dict:
        return self.store.get_cell(table_id, row, column)

    def get_rows(
        self,
        table_id: str,
        rows: list[int | str],
        columns: list[int | str] | None = None,
    ) -> dict:
        return self.store.get_rows(table_id, rows, columns)

    def search_sections(
        self,
        query: str,
        *,
        doc_id: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        return self.store.search_sections(query, doc_id=doc_id, top_k=top_k)

    def search_blocks(self, query: str, *, top_k: int = 5) -> list[dict]:
        # 4–5 个块足够让模型定位；再多会把整段上下文成本推高。
        try:
            bounded = max(4, min(int(top_k), 5))
        except (TypeError, ValueError):
            bounded = 5
        return self.block_index.search(query, top_k=bounded)

    def read_block(self, block_id: str) -> dict:
        return self.block_index.get_block(block_id)

    def search_tables(
        self,
        query: str,
        *,
        doc_id: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """在表格目录里按表名/章节/行名/列名定位，返回候选表而不返回数值。

        为什么单独做一个工具：年报里很多表没有 caption（如「分季度主要财务指标」
        解析后 caption 为空），search_sections 只看章节正文，模型根本不知道表存在。
        FIN-S01 实测：季度表 t0008 就在语料里，四季度相加正好等于全年，但模型
        在章节检索里找不到它，最后只能拒答。
        """
        text = str(query or "").strip()
        if not text:
            return []
        try:
            bounded = max(1, min(int(top_k), 5))
        except (TypeError, ValueError):
            bounded = 5
        terms = _tokenize(text)
        if not terms:
            return []

        if doc_id:
            documents = [self.store.get_document(doc_id)]
        else:
            documents = [
                self.store.get_document(str(item.get("doc_id")))
                for item in self.store.list_documents()
            ]

        hits: list[tuple[float, dict]] = []
        for document in documents:
            section_paths = {
                str(section.get("section_id") or ""): " > ".join(
                    str(part) for part in (section.get("path") or [])
                )
                for section in document.get("sections") or []
            }
            for table in document.get("tables") or []:
                rows = table.get("rows") or []
                row_labels = [
                    self.store._row_label(row)
                    for row in rows[:40]
                ]
                row_labels = [label for label in row_labels if label]
                columns = [str(name) for name in table.get("columns") or []]
                haystack = " ".join(
                    [
                        # 公司/期间也是定位信息：模型常把「美的集团 2025 年…」整句
                        # 传给 search_tables，不带上元数据会排到别家公司的同形表。
                        str(document.get("company") or ""),
                        str(document.get("period") or ""),
                        str(table.get("caption") or ""),
                        section_paths.get(str(table.get("section_id") or ""), ""),
                        " ".join(row_labels),
                        " ".join(columns),
                    ]
                )
                haystack_terms = _tokenize(haystack)
                if not haystack_terms:
                    continue
                overlap = terms & haystack_terms
                if not overlap:
                    continue
                score = len(overlap) / max(1, len(terms))
                matched_rows = [
                    label
                    for label in row_labels
                    if any(term in label for term in terms if len(term) >= 2)
                ]
                if matched_rows:
                    score += 0.15
                if score < 0.2:
                    continue
                hits.append(
                    (
                        score + 0.2 * _table_fill_ratio(table),
                        {
                            "table_id": table.get("table_id"),
                            "doc_id": document.get("doc_id"),
                            "company": document.get("company"),
                            "period": document.get("period"),
                            "page": table.get("page"),
                            "caption": table.get("caption") or "",
                            "columns": columns[:8],
                            "row_labels": row_labels[:8],
                            "matched_rows": matched_rows[:5],
                            "score": round(score, 3),
                            "filled_ratio": round(_table_fill_ratio(table), 2),
                        },
                    )
                )
        hits.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in hits[:bounded]]

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
            elif name == "search_blocks":
                data = self.search_blocks(
                    str(arguments.get("query") or ""),
                    top_k=int(arguments.get("top_k") or 5),
                )
            elif name == "search_tables":
                data = self.search_tables(
                    str(arguments.get("query") or ""),
                    doc_id=arguments.get("doc_id") or None,
                    top_k=int(arguments.get("top_k") or 5),
                )
            elif name == "read_block":
                data = self.read_block(str(arguments.get("block_id") or ""))
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
            elif name == "get_rows":
                rows = arguments.get("rows")
                if not isinstance(rows, list):
                    raise CalculationError("rows 必须是数组")
                data = self.get_rows(
                    str(arguments.get("table_id") or ""),
                    rows,
                    arguments.get("columns"),
                )
            elif name == "calculate":
                # 不同模型对参数名的叫法不一致：expression / formula / expr 都见过
                # （2026-11-03 FIN-11 连发 4 次 formula，全部因 expression 为空失败）。
                # 这里做一次确定性别名归一化，不改变计算语义。
                expression = arguments.get("expression")
                if expression in (None, ""):
                    expression = arguments.get("formula") or arguments.get("expr") or ""
                inputs = arguments.get("inputs") or {}
                if isinstance(inputs, str):
                    try:
                        inputs = json.loads(inputs)
                    except json.JSONDecodeError as exc:
                        raise CalculationError(
                            "inputs 必须是对象，例如 {\"current\": \"12.34\"}"
                        ) from exc
                if not isinstance(inputs, dict):
                    raise CalculationError("inputs 必须是对象")
                data = self.calculate(str(expression or ""), inputs)
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

SEARCH_BLOCKS_SPEC = {
    "type": "function",
    "function": {
        "name": "search_blocks",
        "description": (
            "在结构块索引里一次混合检索（bge + BM25），返回段落块或表格行组块的 block_id、"
            "来源、页码和摘要。它只是定位，不是证据；读取用 read_block，表格行可用 get_rows。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "问题关键词，尽量带公司、年份、指标名"},
                "top_k": {"type": "integer", "minimum": 4, "maximum": 5, "default": 5},
            },
            "required": ["query"],
        },
    },
}

SEARCH_TABLES_SPEC = {
    "type": "function",
    "function": {
        "name": "search_tables",
        "description": (
            "在表格目录里按表名、所属章节、行名或列名找表，返回 table_id、页码、"
            "列名、行名和匹配行。它只负责定位，不是证据；查数必须再用 get_rows / "
            "get_cell 读取。适用于不知道数字在哪张表、或章节正文里没有表的情况。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词，尽量带公司/年份/指标名，如「分季度 营业收入」",
                },
                "doc_id": {"type": "string", "description": "限定某份文档；不传则全库找表"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
            },
            "required": ["query"],
        },
    },
}

READ_BLOCK_SPEC = {    "type": "function",
    "function": {
        "name": "read_block",
        "description": (
            "按 block_id 读取结构块原文。段落块返回正文；表格行组块返回表名、单位、页码、"
            "原始行列号和数值，可直接作为引用证据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"block_id": {"type": "string"}},
            "required": ["block_id"],
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

GET_ROWS_SPEC = {
    "type": "function",
    "function": {
        "name": "get_rows",
        "description": (
            "按 table_id 读取指定的相关行，返回表名、列名、每行数值、行号、列号、单位、脚注和页码。"
            "普通查数优先用它：比 get_cell 多给同组上下文，又比 get_table 少读无关行。"
            "rows 必须显式列出首列行名或行号（1–20 行）；行名可省略单位括号（写「营业收入」即可匹配"
            "「营业收入（千元）」），也接受常用别名；columns 可只选相关列，省略则读全部列。"
            "个别行号越界不会让整次调用失败，会在 skipped_rows 里说明。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
                "rows": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {"type": ["integer", "string"]},
                    "description": "相关行的行号或首列名称，如 [\"营业收入\", \"营业成本\", \"毛利率\"]",
                },
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {"type": ["integer", "string"]},
                    "description": "只读取哪些列；省略读全部列。宽表优先只选相关年份/指标列。",
                },
            },
            "required": ["table_id", "rows"],
        },
    },
}

GET_CELL_SPEC = {
    "type": "function",
    "function": {
        "name": "get_cell",
        "description": (
            "按 table_id + 行 + 列读单元格，返回数值、行名、列名、表格标题、页码和 bbox。"
            "行可以传行号，也可以传首列名称（如「营业收入」，可省略单位括号）；"
            "列可以传列号或列名（如「2025」或「2025 年」）。"
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
            "不要心算。expression 必填（兼容 formula/expr 别名）；inputs 是变量表，"
            "例如 {\"current\": \"12.34\", \"previous\": \"10.00\"}；百分数可以写成 \"23.4%\"。"
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
    SEARCH_BLOCKS_SPEC,
    SEARCH_TABLES_SPEC,
    READ_SECTION_SPEC,
    READ_BLOCK_SPEC,
    GET_TABLE_SPEC,
    GET_ROWS_SPEC,
    GET_CELL_SPEC,
    CALCULATE_SPEC,
]
