"""PDF → sections/tables：把公告原文变成可回查的结构化文档。

产出对齐 `app/finance/store.py` 的读取契约（每份文档一个 `document.json`）：

- `sections`：标题树切出来的叶子章节，带 `path / page_start / page_end / text`；
- `tables`：带 `caption / page / bbox / columns / rows`，单元格定位要有物理位置，
  只给"第几行第几列"在跨页表格上对不回原文。

两条纪律：

1. **表格区域从章节正文里剔除**。同一段数字既出现在 text 又出现在 table 里，
   BM25 会把"在表格里"和"在正文里"算成两次命中，评测时就分不清 Agent 是从
   表格取的数还是从正文猜的。
2. **解析结果是落盘的中间产物**，不是运行时算的。PDF 解析器升级会改表格切分，
   同一份文档两次运行给出不同的 `columns`，评测就不可复现——所以带 `parser_version`。

已知限制：PyMuPDF 的 `find_tables` 靠线框识别，无框线表格会漏。漏掉的表格不会
产生错误数字（正文里仍有文字），但会让"读表"类任务退化成"读正文"。
QC 会统计每份文档的表格数与无标题表格数，判断是否可接受。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import fitz

from app.finance.tree import (
    Line,
    body_size,
    build_sections,
    count_repeated_lines,
    extract_headings,
    extract_lines,
    outline_tree,
)

PARSER_VERSION = "pymupdf-" + (fitz.__doc__ or "").split()[1].rstrip(":")
TREE_VERSION = "v1"

_WHITESPACE_RE = re.compile(r"\s+")
_UNIT_LINE_RE = re.compile(r"^[（(]?单位[：:）)]")
# "公司是否需追溯调整…" 这类勾选题紧贴表格上方，但它是问句不是表标题
_QUESTION_LINE_RE = re.compile(r"是否|适用|不适用")
_PLACEHOLDER_COLUMN_RE = re.compile(r"^col\d+$")
# 单位是逐表而不是逐文档的：同一份美的年报里，主要会计数据用千元、其他表用万元。
# 只记文档级 unit 会让"168,838,102,514.79"和"1,688.38 亿元"变成两个答案。
_UNIT_STATEMENT_RE = re.compile(r"单位\s*[:：]\s*(?:人民币)?\s*(元|千元|万元|百万元)")
_ROW_UNIT_RE = re.compile(r"[（(]\s*(元|千元|万元|百万元)\s*[）)]")
_UNIT_SCAN_GAP = 90.0
_MIN_ROWS = 2
_MIN_COLS = 2
_MIN_FILLED_CELLS = 4
_CAPTION_MAX_CHARS = 60
_CAPTION_MAX_GAP = 45.0
# 行标签列的名字。财报里这一列通常没有表头，但 `get_cell(column=...)` 需要一个能寻址的名字。
_ROW_LABEL_COLUMN = "项目"
# 跨页表格合并：上一页表格底边距页脚 <150pt、下一页表格顶边距页眉 <150pt 且栏宽一致
_CONTINUE_EDGE_MARGIN = 150.0
_CONTINUE_X_TOLERANCE = 6.0
# 只有"表头 + 少量行"的碎片才接回下一页，整表分页不接
_FRAGMENT_MAX_ROWS = 4


def _clean_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _normalize_grid(raw_rows: list[list[Any]]) -> list[list[str]]:
    rows = [[_clean_cell(cell) for cell in row] for row in raw_rows]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    while rows and not any(cell for cell in rows[-1]):
        rows.pop()
    return rows


def _split_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """把首行当表头，并对齐到数据列。

    中文财报的表头常有纵向合并：表头文字落在第 2/4/6 列，数字却落在第 1/3/5 列。
    直接按索引读会让 `get_cell(column='2024 年')` 拿到空字符串——
    美的、比亚迪的「主要会计数据」表都是这个形状，实测表头整体右移一列。
    所以先按"表头名该落在哪一列"做一次 ±1 的整体位移，再丢掉全空的数据列。
    """
    if len(rows) < _MIN_ROWS:
        return [], []
    header_index = 0
    if sum(1 for cell in rows[0] if cell) < 2 and len(rows) > 2:
        header_index = 1
    header = rows[header_index]
    body = rows[header_index + 1:]
    if not any(cell for cell in header):
        header = [""] * len(rows[0])
        body = rows[header_index:]
    return _align_columns(header, body)


def _align_columns(header: list[str], body: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    width = len(header)
    if not body or width < _MIN_COLS:
        return [], []

    fill = [
        sum(1 for row in body if index < len(row) and row[index]) / len(body)
        for index in range(width)
    ]
    if not any(fill):
        return [], []

    def score(shift: int) -> float:
        total = 0.0
        for index, name in enumerate(header):
            target = index + shift
            if name and 0 <= target < width:
                total += fill[target]
        return total

    shift = max((-1, 0, 1), key=lambda value: (score(value), -abs(value)))

    keep = [index for index in range(width) if fill[index] > 0]
    if len(keep) < _MIN_COLS:
        return [], []

    columns: list[str] = []
    for index in keep:
        source = index - shift
        columns.append(header[source].strip() if 0 <= source < width else "")
    # 第一列是行标签列：财报里它常常没有表头名，给一个可寻址的固定名
    if not columns[0]:
        columns[0] = _ROW_LABEL_COLUMN

    seen: dict[str, int] = {}
    unique: list[str] = []
    for index, name in enumerate(columns):
        label = name or f"col{index + 1}"
        if label in seen:
            seen[label] += 1
            label = f"{label}#{seen[label]}"
        else:
            seen[label] = 0
        unique.append(label)

    trimmed = [[row[index] if index < len(row) else "" for index in keep] for row in body]
    return unique, trimmed


def _caption_for(table_bbox: tuple, page_lines: list[Line], repeated: set[str]) -> str:
    """表标题：紧贴表格上方的那一行。取不到就留空，不猜。"""
    x0, y0, _, _ = table_bbox
    candidates: list[Line] = []
    for line in page_lines:
        text = line.text.strip()
        if text in repeated:
            continue
        if line.y1 > y0 + 2:
            continue
        if y0 - line.y1 > _CAPTION_MAX_GAP:
            continue
        if len(text) > _CAPTION_MAX_CHARS:
            continue
        if text[-1:] in "。；;":
            continue
        if line.x1 < x0 - 60:
            continue
        candidates.append(line)
    # 按从下往上找：先排除"单位：元"这类表头说明和"是否…"勾选题，它们不是标题
    for line in sorted(candidates, key=lambda item: item.y1, reverse=True):
        text = line.text.strip()
        if _UNIT_LINE_RE.match(text) or _QUESTION_LINE_RE.search(text):
            continue
        return text
    return ""


def _unit_for(table_bbox: tuple, page_lines: list[Line], rows: list[list[str]], repeated: set[str]) -> str:
    """表单位：先看表上方最近的一行"单位：万元"，再看行标签里的"（千元）"。"""
    _, y0, _, _ = table_bbox
    for line in sorted(page_lines, key=lambda item: item.y1, reverse=True):
        if line.text.strip() in repeated or line.y1 > y0 + 2:
            continue
        if y0 - line.y1 > _UNIT_SCAN_GAP:
            break
        match = _UNIT_STATEMENT_RE.search(line.text)
        if match:
            return match.group(1)

    counter: dict[str, int] = {}
    for row in rows:
        if not row:
            continue
        match = _ROW_UNIT_RE.search(str(row[0]))
        if match:
            counter[match.group(1)] = counter.get(match.group(1), 0) + 1
    if counter:
        return max(counter.items(), key=lambda item: item[1])[0]

    # 最后一档：整页只出现一种"单位：X"就采用它。年报常把单位写在节首，
    # 一张表离它上百点；同页出现两种单位（元 + 万元）时不猜。
    page_units = {
        match.group(1)
        for line in page_lines
        if (match := _UNIT_STATEMENT_RE.search(line.text)) and line.text.strip() not in repeated
    }
    return page_units.pop() if len(page_units) == 1 else ""


def extract_tables(doc: "fitz.Document", lines: list[Line]) -> tuple[list[dict], dict[int, list[tuple]]]:
    """抽取全部表格。返回 (表格列表, 每页的表格 bbox——用于从正文里剔除)。"""
    repeated = count_repeated_lines(lines)
    lines_by_page: dict[int, list[Line]] = {}
    for line in lines:
        lines_by_page.setdefault(line.page, []).append(line)

    tables: list[dict] = []
    boxes: dict[int, list[tuple]] = {}
    page_heights: dict[int, float] = {}
    for page_index in range(doc.page_count):
        page = doc[page_index]
        page_number = page_index + 1
        page_heights[page_number] = float(page.rect.height)
        try:
            found = page.find_tables()
        except Exception:  # noqa: BLE001 - 个别畸形页解析失败不应中断整份文档
            continue
        for table in found.tables or []:
            bbox = tuple(float(value) for value in table.bbox)
            grid = _normalize_grid(table.extract())
            if len(grid) < _MIN_ROWS:
                continue
            if sum(1 for row in grid for cell in row if cell) < _MIN_FILLED_CELLS:
                continue
            columns, rows = _split_header(grid)
            if not columns or not rows:
                continue
            boxes.setdefault(page_number, []).append(bbox)
            tables.append(
                {
                    "table_id": "",
                    "section_id": "",
                    "page": page_number,
                    "page_end": page_number,
                    "caption": _caption_for(bbox, lines_by_page.get(page_number, []), repeated),
                    "unit": _unit_for(
                        bbox, lines_by_page.get(page_number, []), rows, repeated
                    ),
                    "footnote": "",
                    "bbox": [round(value, 2) for value in bbox],
                    "columns": columns,
                    "rows": rows,
                }
            )

    tables = _merge_continuations(tables, page_heights)
    for number, table in enumerate(tables, 1):
        table["table_id"] = f"t{number:04d}"
    return tables, boxes


def _merge_continuations(tables: list[dict], page_heights: dict[int, float]) -> list[dict]:
    """把"页底只剩表头 + 一两行"的碎片接回下一页。

    只接碎片，不接整表。理由：接错比不接更危险——两段不同表的行拼在一起，
    `get_cell(row_name)` 可能命中另一张表的同名行，给出一个**有页码的错误数字**。
    这是查证类产品最坏的失败形态，所以宁可让长表保持分页可见，也不猜。
    """
    merged: list[dict] = []
    for table in tables:
        if merged:
            previous = merged[-1]
            if _is_fragment_continuation(previous, table, page_heights):
                rows = table["rows"]
                if rows and rows[0] == previous["columns"]:
                    rows = rows[1:]
                previous["rows"].extend(rows)
                previous["page_end"] = table["page"]
                continue
        merged.append(table)
    return merged


def _is_fragment_continuation(
    previous: dict, table: dict, page_heights: dict[int, float]
) -> bool:
    if len(previous["rows"]) > _FRAGMENT_MAX_ROWS:
        return False
    if table["page"] != previous.get("page_end", previous["page"]) + 1:
        return False
    if len(table["columns"]) != len(previous["columns"]):
        return False
    if abs(table["bbox"][0] - previous["bbox"][0]) > _CONTINUE_X_TOLERANCE:
        return False
    if abs(table["bbox"][2] - previous["bbox"][2]) > _CONTINUE_X_TOLERANCE:
        return False
    bottom_gap = page_heights.get(previous["page"], 0.0) - previous["bbox"][3]
    if bottom_gap > _CONTINUE_EDGE_MARGIN:
        return False
    return table["bbox"][1] <= _CONTINUE_EDGE_MARGIN


def _locate_section(sections: list[dict], page: int, y: float) -> str:
    """表格归属：起点在表格之前的最后一个 section。

    sections 按文档顺序排列，所以"最后一个起点不晚于表格位置"就是它所在的章节；
    跨页表格会归到起始那页所属的章节，不会因为落页而跳到别的节。
    """
    if not sections:
        return ""
    best = sections[0]
    for section in sections:
        start = section.get("_start")
        if start is None:
            continue
        if start <= (page, y):
            best = section
        else:
            break
    return best["section_id"]


def _attach_table_keywords(sections: list[dict], tables: list[dict]) -> None:
    """把表格的表名与行标签挂到所属章节，作为检索词。

    很多小节正文为空、内容全在表里（茅台的「近三年主要会计数据和财务指标」就是）。
    只索引正文的话，搜"营业收入"永远找不到这张表所在的章节——
    2026-10-28 实测：模型搜了 6 次都没定位到主要会计数据表，最后拒答。
    关键词来自原文（表名 + 首列），不是模型生成，仍可追溯。
    """
    by_id = {section["section_id"]: section for section in sections}
    buckets: dict[str, list[str]] = {}
    for table in tables:
        owner = table.get("section_id")
        if owner not in by_id:
            continue
        words = buckets.setdefault(owner, [])
        caption = str(table.get("caption") or "").strip()
        if caption:
            words.append(caption)
        for row in table.get("rows") or []:
            if row and row[0]:
                label = str(row[0]).strip()
                if 1 < len(label) <= 24:
                    words.append(label)
    for section_id, words in buckets.items():
        seen: list[str] = []
        for word in words:
            if word not in seen:
                seen.append(word)
        by_id[section_id]["table_keywords"] = seen[:40]


def ingest_pdf(pdf_path: str | Path, metadata: dict) -> dict:
    """解析一份 PDF，返回可以直接写盘的 document 结构。"""
    path = Path(pdf_path)
    doc = fitz.open(path)
    try:
        lines = extract_lines(doc)
        size = body_size(lines)
        # 先抽表格：表格区域要同时用于「剔除正文」和「不参与标题判定」。
        # 放在标题之前是有意的——表格单元格里的 `一、` `1、` 会被误认成标题。
        tables, boxes = extract_tables(doc, lines)
        headings = extract_headings(lines, size, table_boxes=boxes)
        sections = build_sections(lines, headings, table_boxes=boxes)

        by_section: dict[str, list[str]] = {}
        for table in tables:
            x0, y0, _, _ = table["bbox"]
            table["section_id"] = _locate_section(sections, table["page"], y0)
            by_section.setdefault(table["section_id"], []).append(table["table_id"])
        for section in sections:
            section["table_ids"] = by_section.get(section["section_id"], [])

        # id 必须跨文档唯一：FinanceStore 把所有文档装进一个命名空间，
        # 两份年报都从 s0001 开始会直接撞车（实测 7 份语料装不进去）。
        # 用 `<doc_id>#<局部 id>`，证据里一眼能看出出自哪份文档。
        prefix = metadata["doc_id"]
        remap = {section["section_id"]: f"{prefix}#{section['section_id']}" for section in sections}
        for section in sections:
            section["section_id"] = remap[section["section_id"]]
            section["table_ids"] = [f"{prefix}#{tid}" for tid in section["table_ids"]]
            section.pop("_start", None)
            section.pop("_title", None)
        for table in tables:
            table["table_id"] = f"{prefix}#{table['table_id']}"
            table["section_id"] = remap.get(table["section_id"], table["section_id"])

        _attach_table_keywords(sections, tables)

        document = {
            "doc_id": metadata["doc_id"],
            "title": metadata.get("title", ""),
            "company": metadata.get("company", ""),
            "code": metadata.get("code", ""),
            "doc_type": metadata.get("doc_type", ""),
            "period": metadata.get("period", ""),
            "published_at": metadata.get("published_at"),
            "announcement_date": metadata.get("announcement_date"),
            "source_url": metadata.get("url") or metadata.get("source_url"),
            "currency": metadata.get("currency", "CNY"),
            "unit": metadata.get("unit", "元"),
            "scope": metadata.get("scope", "consolidated"),
            "restated": bool(metadata.get("restated", False)),
            "negative_format": metadata.get("negative_format", "parentheses"),
            "footnotes": metadata.get("footnotes") or [],
            "page_count": doc.page_count,
            "body_font_size": size,
            "raw_chars": sum(len(line.text) for line in lines),
            "parser_version": PARSER_VERSION,
            "tree_version": TREE_VERSION,
            "sha256": metadata.get("sha256"),
            "sections": sections,
            "tables": tables,
            "outline": outline_tree(sections),
        }
    finally:
        doc.close()
    return document


def summarize(document: dict) -> dict:
    """QC 摘要：判断这份解析结果能不能用来出题。"""
    sections = document.get("sections") or []
    tables = document.get("tables") or []
    top_level = [section for section in sections if section.get("level") == 1]
    without_caption = [table["table_id"] for table in tables if not table.get("caption")]
    thin = [
        table["table_id"]
        for table in tables
        if len(table.get("rows") or []) < 2 or len(table.get("columns") or []) < 2
    ]
    orphan = [table["table_id"] for table in tables if not table.get("section_id")]
    empty = [section["section_id"] for section in sections if not section.get("text")]
    section_chars = sum(len(section.get("text") or "") for section in sections)
    table_chars = sum(
        len("".join("".join(row) for row in table.get("rows") or [])) for table in tables
    )
    raw_chars = int(document.get("raw_chars") or 0)
    return {
        "doc_id": document.get("doc_id"),
        "pages": document.get("page_count"),
        "chars": section_chars,
        "raw_chars": raw_chars,
        "char_coverage": round((section_chars + table_chars) / raw_chars, 3) if raw_chars else None,
        "sections": len(sections),
        "empty_sections": len(empty),
        "empty_without_tables": sum(
            1 for section in sections if not section.get("text") and not section.get("table_ids")
        ),
        "top_level_sections": len(top_level),
        "top_level_titles": [section["path"][-1] for section in top_level],
        "tables": len(tables),
        "tables_without_caption": len(without_caption),
        "tables_with_placeholder_columns": sum(
            1
            for table in tables
            if (table.get("columns") or [])
            and sum(
                1 for name in table["columns"] if _PLACEHOLDER_COLUMN_RE.match(str(name))
            )
            / len(table["columns"])
            > 0.3
        ),
        "thin_tables": len(thin),
        "orphan_tables": len(orphan),
        "body_font_size": document.get("body_font_size"),
        "parser_version": document.get("parser_version"),
    }


def write_document(document: dict, out_dir: str | Path) -> Path:
    doc_id = document["doc_id"]
    target = Path(out_dir) / doc_id / "document.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return target
