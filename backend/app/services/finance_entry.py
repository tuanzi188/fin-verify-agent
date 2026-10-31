"""金融查证 Agent 的线上入口。

与 `/chat` 的分工：

- `/chat` 保留通用 RAG/Agent 行为，兼容旧客户端；
- `/finance/chat` 固定走金融工具集，并把引用从“一段 chunk 文本”升级为
  `EvidenceRef`：文档、页码、章节路径、表格行列、单位、脚注、计算记录。

引用坐标只从 `FinanceStore` 的解析产物取，前端不需要正则解析正文。
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

from app.agent.runner import run as run_agent
from app.config import MODEL_CONFIG
from app.core.user_context import UserContext
from app.finance.store import FinanceStore, FinanceStoreError
from app.models.finance import (
    CalcRecord,
    Claim,
    EvidenceKind,
    EvidenceRef,
    FinanceChatResponse,
    FinanceCitation,
    SourceSpan,
    TableCell as EvidenceTableCell,
    TablePreview,
    TableRow as EvidenceTableRow,
)
from app.services.agent_trace import to_trace
from app.services.conversation_memory import conversation_memory
from app.services.finance_corpus import get_block_index, get_finance_store
from app.services.finance_history import build_history_payload
from app.tools.finance_set import FinanceToolset

logger = logging.getLogger(__name__)

_MARKER_PATTERN = re.compile(r"[\[【]\s*(\d{1,2})(?=[，,、\s\]】])")
_MARKER_GROUP_PATTERN = re.compile(r"[\[【]\s*\d{1,2}(?:[，,、\s][^\]】]*)?[\]】]")
_HEADING_PATTERN = re.compile(r"^#{1,6}\s*")
_LIST_PREFIX_PATTERN = re.compile(r"^(?:[-*+]\s+|\d+[.、)]\s*)")
_MAX_CLAIMS = 12
_MAX_PARAGRAPH_CHARS = 4000


def _safe_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _document_meta(store: FinanceStore, doc_id: str) -> dict:
    if not doc_id:
        return {}
    try:
        document = store.get_document(doc_id)
    except FinanceStoreError:
        return {"doc_id": doc_id}
    company = _as_text(document.get("company"))
    period = _as_text(document.get("period"))
    doc_type = _as_text(document.get("doc_type"))
    title = _as_text(document.get("title")) or " ".join(
        part for part in (company, period, doc_type) if part
    )
    return {
        "doc_id": doc_id,
        "doc_title": title,
        "company": company,
        "period": period,
        "doc_type": doc_type,
        "unit": _as_text(document.get("unit")),
    }


def _span_from_raw(raw: dict) -> list[SourceSpan]:
    spans: list[SourceSpan] = []
    for item in raw.get("source_spans") or []:
        if not isinstance(item, dict):
            continue
        spans.append(
            SourceSpan(
                source_id=_as_text(item.get("source_id")),
                kind=_as_text(item.get("kind") or "section"),
                page=_safe_int(item.get("page")),
                start=_safe_int(item.get("start")),
                end=_safe_int(item.get("end")),
                chars=_safe_int(item.get("chars")),
            )
        )
    return spans


_COL_PLACEHOLDER = re.compile(r"^col\d+$", re.I)
_HEADER_NUMBER = re.compile(r"^[\d,.\-%（）()\s]+$")


def _display_columns(table: dict) -> list[str]:
    """用前几行表头补全 `col2`/空列名，原始列名保持不变。

    解析器把合并表头摊平后，常见 `['2025 年','col2','2024 年','col4']`。
    直接展示 `col2` 会让证据检查器失去可读性；这里只做展示层补全，
    不修改 `table.columns`，避免影响行/列坐标解析。
    """
    columns = [_as_text(name) for name in table.get("columns") or []]
    rows = table.get("rows") or []
    header_rows = rows[:3]
    display: list[str] = []
    last_group = ""

    for index, name in enumerate(columns):
        cell_text = ""
        for row in header_rows:
            if index >= len(row):
                continue
            raw = _as_text(row[index]).strip()
            if not raw or _HEADER_NUMBER.match(raw):
                continue
            cell_text = raw
            break

        if index == 0 and (
            not name
            or _COL_PLACEHOLDER.match(name)
            or re.fullmatch(r"20\d{2}\s*年?", name)
            or "单位" in name
        ):
            display.append("项目")
            # 第一列展示成“项目”，但年份分组标签要留给后面的数据列做前缀。
            last_group = name if re.fullmatch(r"20\d{2}\s*年?", name) else "项目"
            continue

        if name and not _COL_PLACEHOLDER.match(name):
            display.append(name)
            last_group = name
            continue

        if not cell_text:
            display.append(name or f"列{index + 1}")
            continue

        # “同比增减/变动”不区分年份，不要硬加 2025/2024 前缀。
        prefix = ""
        if last_group and last_group != "项目" and not any(
            token in cell_text for token in ("同比", "增减", "变动")
        ):
            prefix = f"{last_group} "
        display.append(f"{prefix}{cell_text}".strip())

    return display


def _table_preview(
    table: dict,
    *,
    row_indices: list[int],
    column_indices: list[int],
) -> TablePreview:
    all_columns = [_as_text(name) for name in table.get("columns") or []]
    display_columns = _display_columns(table)
    all_rows = table.get("rows") or []
    total_rows = len(all_rows)

    # 首列始终保留：没有行名的表格，读者无法判断 65.71% 属于哪一行。
    selected_columns = list(dict.fromkeys([0, *column_indices]))
    selected_columns = [index for index in selected_columns if 0 <= index < len(all_columns)][:12]
    if not selected_columns:
        selected_columns = list(range(min(len(all_columns), 12)))

    rows: list[EvidenceTableRow] = []
    for index in row_indices[:20]:
        if index < 0 or index >= total_rows:
            continue
        values = all_rows[index]
        cells = [
            EvidenceTableCell(
                column=all_columns[column_index],
                column_index=column_index,
                value=(
                    _as_text(values[column_index])
                    if column_index < len(values) and values[column_index] is not None
                    else None
                ),
            )
            for column_index in selected_columns
        ]
        rows.append(
            EvidenceTableRow(
                row=index,
                row_name=_as_text(values[0]) if values else "",
                cells=cells,
            )
        )

    return TablePreview(
        table_id=_as_text(table.get("table_id")),
        caption=_as_text(table.get("caption")),
        page=_safe_int(table.get("page")),
        page_end=_safe_int(table.get("page_end")),
        unit=_as_text(table.get("unit")),
        unit_source=_as_text(table.get("unit_source")),
        footnote=_as_text(table.get("footnote")),
        columns=all_columns,
        display_columns=display_columns,
        column_indices=selected_columns,
        rows=rows,
        row_indices=[row.row for row in rows],
        total_rows=total_rows,
    )


def _render_table_content(preview: TablePreview, row_indices: list[int]) -> str:
    header = (
        f"表 {preview.table_id}｜{preview.caption or '无标题'}"
        f"｜PDF 第 {preview.page} 页"
    )
    if preview.page_end and preview.page_end != preview.page:
        header += f"–{preview.page_end}"
    header += f"｜单位：{preview.unit or '未标注'}"
    lines = [header, " | ".join(preview.display_columns or preview.columns)]
    for row in preview.rows:
        if row_indices and row.row not in row_indices:
            continue
        cells = []
        for cell in row.cells:
            name = (
                preview.display_columns[cell.column_index]
                if cell.column_index < len(preview.display_columns)
                else cell.column
            )
            cells.append(f"{name}={_as_text(cell.value)}")
        lines.append(f"[行{row.row}] " + " | ".join(cells))
    if preview.footnote:
        lines.append(f"脚注：{preview.footnote}")
    return "\n".join(lines)


def _table_evidence(
    store: FinanceStore,
    *,
    anchor: str,
    raw: dict,
    located: dict,
) -> EvidenceRef:
    table_id = _as_text(located.get("table_id") or raw.get("table_id"))
    table = store.get_table(table_id)
    kind = located.get("kind")
    target_rows = list(located.get("row_indices") or [])
    target_columns = list(located.get("column_indices") or [])
    preview_rows = list(target_rows)
    preview_columns = list(target_columns)

    if kind == "table_cell":
        preview_rows = target_rows[:1]
        preview_columns = target_columns[:1]
        if target_rows:
            # 单元格证据给一个上下文窗口，检查器才能看到同组指标；
            # 但高亮坐标仍只保留目标单元格。
            row_index = target_rows[0]
            preview_rows = list(
                range(
                    max(0, row_index - 2),
                    min(len(table.get("rows") or []), row_index + 3),
                )
            )
    elif not preview_rows:
        preview_rows = list(range(min(len(table.get("rows") or []), 6)))

    if not preview_columns:
        preview_columns = list(range(min(len(table.get("columns") or []), 12)))

    preview = _table_preview(
        table,
        row_indices=preview_rows,
        column_indices=preview_columns,
    )
    meta = _document_meta(store, _as_text(located.get("doc_id") or raw.get("doc_id")))

    row_name = ""
    column = None
    cell_text = None
    if kind == "table_cell" and target_rows and target_columns:
        target_row = target_rows[0]
        target_column = target_columns[0]
        row_values = table.get("rows") or []
        columns = table.get("columns") or []
        if target_row < len(row_values):
            row_name = _as_text(row_values[target_row][0]) if row_values[target_row] else ""
            cell_text = (
                _as_text(row_values[target_row][target_column])
                if target_column < len(row_values[target_row])
                else None
            )
        if target_column < len(preview.display_columns):
            column = preview.display_columns[target_column]
        elif target_column < len(columns):
            column = _as_text(columns[target_column])
    elif preview.rows:
        row_name = "、".join(row.row_name for row in preview.rows[:4])

    evidence_kind = (
        EvidenceKind.table_cell if kind == "table_cell" else EvidenceKind.table_rows
    )
    content = _render_table_content(preview, preview_rows)
    if kind == "table_cell" and row_name:
        content = (
            f"{preview.caption or '表格'}｜第 {preview.page} 页"
            f"｜{row_name} 的「{column}」= {cell_text}"
            f"（单位：{preview.unit or '未标注'}）\n{content}"
        )

    return EvidenceRef(
        evidence_id=anchor or table_id,
        doc_id=meta.get("doc_id", ""),
        doc_title=meta.get("doc_title", ""),
        company=meta.get("company", ""),
        period=meta.get("period", ""),
        doc_type=meta.get("doc_type", ""),
        kind=evidence_kind,
        page=_safe_int(preview.page),
        section_id=_as_text(located.get("section_id") or table.get("section_id")) or None,
        table_id=table_id,
        table_caption=preview.caption,
        # 高亮只标“调用方明确指定的坐标”：
        # - get_table / 无列限定的 get_rows 不把整张表刷成蓝色；
        # - get_cell 只标高亮目标行/列。
        row_indices=(
            target_rows
            if kind in ("table_cell", "table_rows") and target_rows
            else []
        ),
        column_indices=(
            target_columns
            if target_columns and len(target_columns) < len(table.get("columns") or [])
            else []
        ),
        row_name=row_name or None,
        column=column,
        cell_text=cell_text,
        unit=preview.unit,
        footnote=preview.footnote,
        content=content,
        source_spans=[
            SourceSpan(
                source_id=table_id,
                kind="table",
                page=_safe_int(preview.page),
                start=min([row.row for row in preview.rows], default=0),
                end=max([row.row for row in preview.rows], default=0) + 1,
                chars=len(preview.rows),
            )
        ],
        table_preview=preview,
        verified=True,
        note="表格坐标来自解析产物；页码为 PDF 解析页码，不是原版截图。",
    )


def _section_evidence(
    store: FinanceStore,
    *,
    anchor: str,
    raw: dict,
    located: dict,
) -> EvidenceRef:
    section_id = _as_text(located.get("section_id") or raw.get("section_id"))
    section = store.read_section(section_id)
    meta = _document_meta(store, _as_text(located.get("doc_id") or raw.get("doc_id")))

    content = ""
    if anchor and "#b" in anchor:
        try:
            block = get_block_index().get_block(anchor)
            content = _as_text(block.get("content"))
        except (KeyError, RuntimeError):
            content = ""
    full_text = _as_text(section.get("text"))
    raw_content = _as_text(raw.get("content"))
    if not content:
        # `_public_evidence` 会把正文截到 400 字；章节证据优先给完整正文，
        # 否则检查器会把“截断后的摘要”当成原文。
        content = full_text if len(full_text) > len(raw_content) else raw_content
    content = content[:_MAX_PARAGRAPH_CHARS]
    if len(full_text) > _MAX_PARAGRAPH_CHARS and not content.endswith("…[已截断，完整原文见文档]"):
        content += "\n…[已截断，完整原文见文档]"

    spans = _span_from_raw(raw)
    if not spans:
        spans = [
            SourceSpan(
                source_id=section_id,
                kind="section",
                page=_safe_int(section.get("page_start")),
                start=0,
                end=len(_as_text(section.get("text"))),
                chars=len(_as_text(section.get("text"))),
            )
        ]

    return EvidenceRef(
        evidence_id=anchor or section_id,
        doc_id=meta.get("doc_id", ""),
        doc_title=meta.get("doc_title", ""),
        company=meta.get("company", ""),
        period=meta.get("period", ""),
        doc_type=meta.get("doc_type", ""),
        kind=EvidenceKind.paragraph,
        page=_safe_int(section.get("page_start")),
        section_id=section_id,
        path=[_as_text(part) for part in section.get("path") or []],
        content=content,
        source_spans=spans,
        verified=False,
        note="章节段落证据；页码为 PDF 解析页码。",
    )


def build_evidence(
    store: FinanceStore,
    anchor: str,
    raw: Optional[dict] = None,
) -> EvidenceRef:
    """把 runner 的证据锚点转成前端可渲染的 `EvidenceRef`。"""
    raw = raw or {}
    try:
        located = store.locate(anchor)
    except FinanceStoreError as exc:
        raise FinanceStoreError(f"证据坐标无法解析: {exc}") from exc

    if located.get("kind") in ("table", "table_rows", "table_cell"):
        return _table_evidence(store, anchor=anchor, raw=raw, located=located)
    if located.get("kind") in ("section", "paragraph_block"):
        return _section_evidence(store, anchor=anchor, raw=raw, located=located)

    meta = _document_meta(store, _as_text(located.get("doc_id") or raw.get("doc_id")))
    return EvidenceRef(
        evidence_id=anchor,
        doc_id=meta.get("doc_id", ""),
        doc_title=meta.get("doc_title", ""),
        company=meta.get("company", ""),
        period=meta.get("period", ""),
        doc_type=meta.get("doc_type", ""),
        kind=EvidenceKind.document,
        content=_as_text(raw.get("content")),
        verified=False,
    )


def _calc_unit(formula: str, inputs: dict) -> str:
    compact = str(formula or "").replace(" ", "")
    values = [_as_text(value).strip() for value in (inputs or {}).values()]
    has_percent_input = any(value.endswith("%") for value in values)
    if has_percent_input and "*" not in compact and "/" not in compact:
        return "pp"
    if "*100" in compact:
        return "%"
    return ""


def _build_calcs(raw_calcs: list[dict]) -> list[CalcRecord]:
    records: list[CalcRecord] = []
    for item in raw_calcs or []:
        if not isinstance(item, dict):
            continue
        inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        formula = _as_text(item.get("formula"))
        records.append(
            CalcRecord(
                formula=formula,
                inputs=inputs,
                result=item.get("result"),
                unit=_calc_unit(formula, inputs),
                note="由受限计算器 calculate 生成；可按 formula + inputs 复算。",
            )
        )
    return records


def _clean_claim_line(line: str) -> str:
    text = _HEADING_PATTERN.sub("", line.strip())
    text = _LIST_PREFIX_PATTERN.sub("", text).strip()
    # 要点卡不是 Markdown 渲染器：把强调标记和行内反引号去掉，
    # 否则界面上会直接出现 `**`。
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip("*_ ")


def build_claims(answer: str, citations: list[FinanceCitation], calcs: list[CalcRecord]) -> list[Claim]:
    markers = [citation.marker for citation in citations]
    claims: list[Claim] = []
    for line in (answer or "").splitlines():
        text = _clean_claim_line(line)
        if len(text) < 4:
            continue
        # Markdown 表格行不是“要点”：它们会被拆成 `| 指标 | 公式 | 结果 |`，
        # 既读不懂也会把要点数量撑到十几条。完整表格保留在“完整回答”里。
        if text.startswith("|") or text.endswith("|"):
            continue
        if re.fullmatch(r"[\s|:\-]+", text):
            continue
        found = [int(value) for value in _MARKER_PATTERN.findall(text)]
        found = [value for value in dict.fromkeys(found) if value in markers]
        # 没有引用也没有数字的行（标题、过渡句）不单独成为要点。
        if not found and not re.search(r"\d", text):
            continue
        normalized = text.replace(",", "").replace("，", "")
        calc_index = None
        for index, calc in enumerate(calcs):
            result = _as_text(calc.result).replace(",", "").strip()
            if not result:
                continue
            variants = {result}
            try:
                numeric = float(result.rstrip("%"))
                variants.add(f"{numeric:.2f}")
                variants.add(f"{numeric:.4f}")
            except ValueError:
                pass
            if any(variant and variant in normalized for variant in variants):
                calc_index = index
                break
        # 引用标记已经渲染成胶囊，正文里不再保留 [1] / 【2，PDF...】 这种原始角标。
        text = _MARKER_GROUP_PATTERN.sub("", text).strip()
        if len(text) < 2:
            continue
        claims.append(
            Claim(
                text=text,
                citation_markers=found,
                calc_index=calc_index,
                uncertainty=(
                    "原文使用“无法确定/资料不足”等表述，需结合证据检查器确认边界。"
                    if any(word in text for word in ("无法确定", "资料不足", "未披露", "没有披露"))
                    else None
                ),
            )
        )
        if len(claims) >= _MAX_CLAIMS:
            break

    if not claims and answer.strip():
        claims.append(
            Claim(
                text=answer.strip()[:500],
                citation_markers=markers,
            )
        )
    return claims


def _notes_from_trace(raw_trace: list[dict]) -> list[str]:
    if not raw_trace:
        return []
    final = raw_trace[-1] if isinstance(raw_trace[-1], dict) else {}
    note = _as_text(final.get("note"))
    if not note:
        return []
    return [item for item in note.split("；") if item]


async def run_finance_query(
    *,
    query: str,
    conversation_id: str,
    user_context: UserContext,
    max_steps: int = 10,
    search_mode: str = "blocks",
    variant: str = "improved",
    route_reason: str = "",
    corpus_fingerprint: str = "",
) -> FinanceChatResponse:
    store = get_finance_store()
    block_index = get_block_index() if search_mode == "blocks" else None
    toolset = FinanceToolset(
        store,
        search_mode=search_mode,
        block_index=block_index,
    )
    task = {
        "id": f"web-finance-{uuid.uuid4()}",
        "question": query,
        "clarify_mode": "ask",
        "citation_required": True,
        "max_steps": max_steps,
    }
    result = await run_agent(
        task,
        variant=variant,
        user_context=user_context,
        clear_cache=False,
        toolset=toolset,
    )

    evidence_rows = result.get("evidence") or []
    citations: list[FinanceCitation] = []
    notes = _notes_from_trace(result.get("trace") or [])
    for item in result.get("citations") or []:
        try:
            marker = int(item.get("marker") or 0)
        except (TypeError, ValueError):
            continue
        anchor = _as_text(item.get("chunk_id"))
        if not marker or not anchor:
            continue
        raw = evidence_rows[marker - 1] if 1 <= marker <= len(evidence_rows) else {}
        try:
            evidence = build_evidence(store, anchor, raw if isinstance(raw, dict) else {})
        except FinanceStoreError as exc:
            logger.warning("finance evidence build failed: anchor=%s err=%s", anchor, exc)
            notes.append(f"证据坐标无法解析: {anchor}")
            continue
        citations.append(FinanceCitation(marker=marker, evidence=evidence))

    # 模型常对同一段证据重复标 [1][3][4]；界面按“每个锚点一条胶囊”展示，
    # 否则同一张表会在一条要点后面排四遍。保留第一次出现的 marker。
    deduped: list[FinanceCitation] = []
    seen_evidence: set[str] = set()
    for citation in citations:
        key = citation.evidence.evidence_id
        if key in seen_evidence:
            continue
        seen_evidence.add(key)
        deduped.append(citation)
    citations = deduped

    calcs = _build_calcs(result.get("calc") or [])
    answer = _as_text(result.get("answer"))
    status = _as_text(result.get("status") or "error")

    usage = result.get("usage") or {}
    response = FinanceChatResponse(
        status=status,
        answer=answer,
        claims=build_claims(answer, citations, calcs),
        citations=citations,
        calc=calcs,
        trace=to_trace(result.get("trace") or []),
        conversation_id=conversation_id,
        model_used=MODEL_CONFIG["flash"]["model"],
        variant=_as_text(result.get("variant") or variant),
        strategy="agent",
        route_reason=route_reason,
        search_mode=search_mode,
        duration_ms=int(usage.get("latency_ms") or 0),
        token_usage={
            "prompt_tokens": int(usage.get("tokens_in") or 0),
            "completion_tokens": int(usage.get("tokens_out") or 0),
            "total_tokens": int(usage.get("tokens_in") or 0) + int(usage.get("tokens_out") or 0),
            "llm_calls": int(usage.get("llm_calls") or 0),
            "final_context_tokens": int(usage.get("final_context_tokens") or 0),
        },
        notes=notes,
    )
    if status != "error" and answer:
        await conversation_memory.add_turn(
            conversation_id,
            query,
            answer,
            payload=build_history_payload(
                response, corpus_fingerprint=corpus_fingerprint
            ),
        )
    return response