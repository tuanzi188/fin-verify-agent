"""金融文档工具集：把 `FinanceToolbox` 接到 Agent loop 上。

三件事决定了它和制度库工具集不一样：

1. **`search_sections` 只定位，不算证据**。它返回的是章节摘要，不是原文；
   如果把它当证据编号喂给模型，模型会引用一个"看起来像出处"的章节摘要，
   而真正被引用的数值根本没被读过。所以它的证据项标 `citable=False`：
   参与去重与空结果判断，但不进编号列表。
2. **`calculate` 不是证据，是审计记录**。每次调用都进 `calcs`，
   评测侧据 "每个 calc 有 formula/inputs/result" 判硬门禁。
3. **截断要显式标注**。一个 26k 字的章节整段塞进上下文既贵又没用；
   截断时标 `truncated=True`，报告里能看出"这条结论是在被截断的资料上得出的"。
"""

from __future__ import annotations

from typing import Any

from app.agent.prompts import (
    FINANCE_BASELINE_SYSTEM,
    FINANCE_FINAL_TEMPLATE,
    FINANCE_IMPROVED_SYSTEM,
)
from app.finance.store import FinanceStore
from app.tools.finance import FINANCE_TOOL_SPECS, FinanceToolbox
from app.tools.toolset import Injector, ToolOutcome, evidence_item

MAX_SECTION_CHARS = 6000
MAX_TABLE_CHARS = 3000
MAX_CELL_CHARS = 400

# 截断注入的长度。制度库用 12 字，因为那里一个 chunk 25–70 字；
# 金融这里要短到"数字必坏"：`456,451,731` 是 11 个字符，12 字截断等于不截断。
_TRUNCATE_CHARS = 8


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    text = str(text or "")
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…[已截断]", True


def _cut(text: Any) -> str:
    value = str(text or "")
    if len(value) <= _TRUNCATE_CHARS:
        return value
    return value[:_TRUNCATE_CHARS] + "…[截断]"


def _cut_payload(name: str, data: Any) -> Any:
    """截断注入：只切"模型看得到的那部分正文"，结构字段一律保留。

    保留 `table_ids` / `table_id` / `row` / `column` 是故意的：注入要逼出"重取"，
    而不是让模型连重取什么都找不到——找不到就成了考运气，不是考纠错。
    """
    if name == "search_sections" and isinstance(data, list):
        return [{**item, "snippet": _cut(item.get("snippet"))} for item in data]
    if name == "read_section" and isinstance(data, dict):
        return {**data, "text": _cut(data.get("text"))}
    if name == "get_table" and isinstance(data, dict):
        rows = data.get("rows") or []
        return {**data, "rows": rows[:1], "truncated": True}
    if name == "get_cell" and isinstance(data, dict):
        return {**data, "cell_text": _cut(data.get("cell_text"))}
    return data


def _render_table(table: dict) -> str:
    head = (
        f"表 {table.get('table_id')}｜{table.get('caption') or '无标题'}"
        f"｜PDF 第 {table.get('page')} 页"
    )
    if table.get("page_end") and table.get("page_end") != table.get("page"):
        head += f"–{table.get('page_end')}"
    head += f"｜单位：{table.get('unit') or '未标注'}"
    lines = [head, " | ".join(str(name) for name in table.get("columns") or [])]
    for row in table.get("rows") or []:
        lines.append(" | ".join(str(cell) for cell in row))
        if sum(len(line) for line in lines) > MAX_TABLE_CHARS:
            lines.append("…[表格过长，已截断，按需用 get_cell 取具体单元格]")
            break
    return "\n".join(lines)


class FinanceToolset:
    """金融文档工具集。接口与 `app.tools.mock.Toolbox` 一致。"""

    name = "finance"
    search_tool = "search_sections"
    final_template = FINANCE_FINAL_TEMPLATE
    # 金融任务的动作链更长：定位 → 读章节 → 读表 → 取单元格 → 计算，
    # 3 轮装不下（实测 3 轮时模型只来得及做 4 次 search_sections 就被收口）。
    max_tool_rounds = 5

    def __init__(self, store: FinanceStore, injections: Any = None):
        self.toolbox = FinanceToolbox(store)
        #: 每次 calculate 的 {formula, inputs, result}，供硬门禁与报告使用
        self.calcs: list[dict] = []
        self._injector = Injector(injections)

    # ------------------------------------------------------------------
    # 注入与复用
    # ------------------------------------------------------------------

    def prepare(self, injections: Any = None) -> None:
        """一条任务开跑前调用：清掉上一条的 calcs / 调用计数，换成本条的注入计划。

        评测脚本会复用同一个实例连跑几十条；不清就会把上一条的计算记到下一条头上，
        硬门禁"每个数字都有公式"直接失效。
        """
        self.calcs = []
        self._injector = Injector(injections)

    @property
    def planned_injections(self) -> int:
        return self._injector.planned

    @property
    def fired_injections(self) -> int:
        return self._injector.fired

    @property
    def calls(self) -> int:
        return self._injector.calls

    # ------------------------------------------------------------------
    # Toolset 契约
    # ------------------------------------------------------------------

    def specs(self) -> list[dict]:
        return FINANCE_TOOL_SPECS

    def system_prompt(self, variant: str) -> str:
        return FINANCE_IMPROVED_SYSTEM if variant == "improved" else FINANCE_BASELINE_SYSTEM

    async def call(self, name: str, arguments: dict) -> ToolOutcome:
        arguments = arguments or {}
        mode = self._injector.take()
        if mode is not None:
            return await self._injected(name, arguments, mode)

        outcome = await self.toolbox.call(name, arguments)
        if not outcome.ok:
            return ToolOutcome(
                tool=name,
                ok=False,
                error_kind=outcome.error_kind,
                error_detail=outcome.error_detail,
            )
        data = outcome.data
        if name == "calculate":
            self.calcs.append(
                {
                    "formula": data.get("formula"),
                    "inputs": data.get("inputs"),
                    "result": data.get("result"),
                }
            )
        ids, count = self._summarize(name, data)
        return ToolOutcome(tool=name, ok=True, data=data, result_ids=ids, result_count=count)

    # ------------------------------------------------------------------
    # 故障注入
    # ------------------------------------------------------------------

    async def _injected(self, name: str, arguments: dict, mode: str) -> ToolOutcome:
        """注入故障。Agent 侧看到的形状与真实失败一致，注入标记只在 trace 里。"""
        if mode == "timeout":
            return ToolOutcome(
                tool=name, ok=False, injected="timeout",
                error_kind="timeout", error_detail="注入：工具超时",
            )
        if mode == "500":
            return ToolOutcome(
                tool=name, ok=False, injected="500",
                error_kind="upstream_error", error_detail="注入：上游 500",
            )
        if mode == "empty":
            data: Any = [] if name in ("search_sections", "list_documents") else {}
            return ToolOutcome(tool=name, ok=True, data=data, injected="empty")

        # truncated：先真实执行，再把正文/单元格切短
        outcome = await self.toolbox.call(name, arguments)
        if not outcome.ok:
            return ToolOutcome(
                tool=name, ok=False, injected="truncated",
                error_kind=outcome.error_kind, error_detail=outcome.error_detail,
            )
        data = _cut_payload(name, outcome.data)
        ids, count = self._summarize(name, data)
        return ToolOutcome(
            tool=name, ok=True, data=data, result_ids=ids,
            result_count=count, injected="truncated",
        )

    # ------------------------------------------------------------------
    # 结果 → 证据
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize(name: str, data: Any) -> tuple[list[str], int]:
        if name == "list_documents" and isinstance(data, list):
            return [str(item.get("doc_id") or "") for item in data], len(data)
        if name == "search_sections" and isinstance(data, list):
            return [str(item.get("section_id") or "") for item in data], len(data)
        if name == "read_section" and isinstance(data, dict):
            return [str(data.get("section_id") or "")], 1
        if name == "get_table" and isinstance(data, dict):
            return [str(data.get("table_id") or "")], 1
        if name == "get_cell" and isinstance(data, dict):
            anchor = f"{data.get('table_id')}#{data.get('row')}#{data.get('column')}"
            return [anchor], 1
        return [], 0

    def to_evidence(self, name: str, data: Any) -> list[dict]:
        if name == "search_sections" and isinstance(data, list):
            return [
                evidence_item(
                    chunk_id=str(item.get("section_id") or ""),
                    title=f"{item.get('title') or ''}｜{' > '.join(item.get('path') or [])}",
                    content=f"PDF 第 {item.get('page_start')} 页\n{item.get('snippet') or ''}",
                    kind="section_locator",
                    page=item.get("page_start"),
                    path=item.get("path") or [],
                    # 只定位不算证据：不进编号列表，但参与去重与空结果判断
                    citable=False,
                    doc_id=item.get("doc_id"),
                )
                for item in data
                if isinstance(item, dict) and item.get("section_id")
            ]

        if name == "read_section" and isinstance(data, dict):
            body, truncated = _truncate(data.get("text") or "", MAX_SECTION_CHARS)
            path = data.get("path") or []
            table_ids = list(data.get("table_ids") or [])
            # 年报里大量小节只有一句标题 + 一张表，正文是空的。不把表 id 交出去，
            # 模型读完只会得到"本节没有内容"，然后判定未披露（实测踩中）。
            footer = ""
            if table_ids:
                footer = (
                    "本节包含表格：" + "、".join(table_ids)
                    + "（用 get_table 读整表，get_cell 取具体单元格）\n"
                )
            content, _ = _truncate(
                f"{' > '.join(path)}\n{footer}{body or '（本节正文为空，内容在表格里）'}",
                MAX_SECTION_CHARS,
            )
            return [
                evidence_item(
                    chunk_id=str(data.get("section_id") or ""),
                    title=f"{data.get('title') or ''}｜{' > '.join(path)}",
                    content=content,
                    kind="section",
                    page=data.get("page_start"),
                    path=path,
                    truncated=truncated,
                    table_ids=table_ids,
                    doc_id=data.get("doc_id"),
                )
            ]

        if name == "get_table" and isinstance(data, dict):
            rendered = _render_table(data)
            content, truncated = _truncate(rendered, MAX_TABLE_CHARS + 200)
            return [
                evidence_item(
                    chunk_id=str(data.get("table_id") or ""),
                    title=f"{data.get('title') or ''}｜表 {data.get('table_id')}",
                    content=content,
                    kind="table",
                    page=data.get("page"),
                    truncated=truncated,
                    table_caption=data.get("caption") or "",
                    unit=data.get("unit") or "",
                    doc_id=data.get("doc_id"),
                )
            ]

        if name == "get_cell" and isinstance(data, dict):
            quote = (
                f"{data.get('table_caption') or '表格'}｜第 {data.get('page')} 页"
                f"｜{data.get('row_name')} 的「{data.get('column')}」= {data.get('cell_text')}"
                f"（单位：{data.get('unit') or '未标注'}，来源：{data.get('unit_source') or 'table'}）"
            )
            content, truncated = _truncate(quote, MAX_CELL_CHARS)
            anchor = f"{data.get('table_id')}#{data.get('row')}#{data.get('column')}"
            return [
                evidence_item(
                    chunk_id=anchor,
                    title=f"{data.get('title') or ''}｜{data.get('table_caption') or '表格'}",
                    content=content,
                    kind="cell",
                    page=data.get("page"),
                    truncated=truncated,
                    table_id=data.get("table_id"),
                    row_name=data.get("row_name"),
                    column=data.get("column"),
                    cell_text=data.get("cell_text"),
                    unit=data.get("unit") or "",
                    doc_id=data.get("doc_id"),
                )
            ]

        if name == "list_documents" and isinstance(data, list):
            summary = "; ".join(
                f"{item.get('doc_id')}（{item.get('company')} {item.get('period')} {item.get('doc_type')}）"
                for item in data[:20]
            )
            return [
                evidence_item(
                    chunk_id="__documents__",
                    title="本地文档清单",
                    content=summary,
                    kind="catalog",
                    citable=False,
                )
            ]
        return []
