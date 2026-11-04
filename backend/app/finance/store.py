"""本地金融文档库：按 doc/section/table 读取，带页码与原文元数据。

存储格式（每份文档一个目录）：

```
<root>/<doc_id>/document.json
{
  "doc_id": "...",
  "title": "...",
  "company": "...",
  "code": "000001",
  "doc_type": "annual_report",
  "period": "2025",
  "published_at": "2026-04-20",
  "source_url": "https://...",
  "sections": [
    {"section_id": "...", "path": ["第二节", "主要财务指标"],
     "page_start": 42, "page_end": 43, "text": "..."}
  ],
  "tables": [
    {"table_id": "...", "section_id": "...", "page": 42,
     "caption": "主要会计数据",
     "columns": ["项目", "2025", "2024"],
     "rows": [["营业收入", "12.34", "10.00"]]}
  ]
}
```

为什么不用数据库：v1 的数据量是 12–40 份文档，JSON + 内存索引足够；
真正需要的是**页码/表格/原文可追溯**，不是查询性能。
"""

from __future__ import annotations

import json
import math
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

_RUN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")

# 行名/列名的归一化：
# - 财报表格的行标签常把单位写在括号里（营业收入（千元）），模型通常只写「营业收入」；
# - 列名常写「2025 年」，模型常只写「2025」；
# - 部分表行标签带「一、」「其中：」「减：」前缀，或落在第 2/3 列。
# 这里只做确定性的字符串归一化，不做语义猜测；匹配不到时把候选回显给模型。
_UNIT_TAIL_RE = re.compile(
    r"[（(][^（）()]{0,16}(?:元/股|百万元|千元|万元|亿元|元|股|%|％|倍)"
    r"[^（）()]{0,8}[)）]\s*$"
)
_LABEL_PREFIX_RE = re.compile(
    r"^(?:[一二三四五六七八九十百]+[、.．]|其中[:：]|加[:：]|减[:：]|注\s*\d*\s*[:：]?)\s*"
)
_FOUR_DIGIT_YEAR_RE = re.compile(r"^(\d{4})\s*年?$")
_NUMERIC_CELL_RE = re.compile(r"^[-+−]?[\d,，.]+\s*(?:%|％)?$")

# 常用财务科目别名。键和值都按 _normalize_label 归一化后的形式书写。
# 只收「同一科目在不同表/不同公司里的常见写法」，不把不同口径（营业总收入 vs 营业收入）
# 混为一谈；命中不了就走模糊匹配并回显候选。
_ROW_ALIASES: dict[str, tuple[str, ...]] = {
    "营业收入": ("营业收入", "营业总收入", "营业收入合计", "营业总收入合计"),
    "营业总收入": ("营业总收入", "营业收入", "营业总收入合计"),
    "营业收入合计": ("营业收入合计", "营业总收入合计", "营业收入", "营业总收入"),
    "营业成本": ("营业成本", "营业总成本"),
    "营业总成本": ("营业总成本", "营业成本"),
    "归母净利润": (
        "归属于上市公司股东的净利润",
        "归属于母公司所有者的净利润",
        "归属于母公司股东的净利润",
    ),
    "归属于上市公司股东的净利润": (
        "归属于上市公司股东的净利润",
        "归属于母公司所有者的净利润",
        "归属于母公司股东的净利润",
    ),
    "归属于母公司所有者的净利润": (
        "归属于母公司所有者的净利润",
        "归属于上市公司股东的净利润",
        "归属于母公司股东的净利润",
    ),
    "研发投入": (
        "研发投入金额",
        "研发投入",
        "研发投入合计",
        "研发费用金额",
        "研发费用",
    ),
    "研发费用": (
        "研发费用",
        "研发费用金额",
        "研发投入金额",
        "研发投入",
    ),
    "总资产": ("总资产", "资产总计"),
    "资产总计": ("资产总计", "总资产"),
    "净资产": (
        "归属于上市公司股东的净资产",
        "净资产",
        "归属于母公司所有者权益合计",
    ),
    "每股收益": ("基本每股收益", "稀释每股收益"),
}


def _normalize_label(value: object) -> str:
    """把行名/列名归一化成可比较的键。只做确定性字符串处理。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", "", text)
    text = _LABEL_PREFIX_RE.sub("", text)
    # 单位后缀可能连续出现（如「营业收入（千元）（经重述）」），最多剥两层。
    for _ in range(2):
        stripped = _UNIT_TAIL_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    year = _FOUR_DIGIT_YEAR_RE.match(text)
    if year:
        return year.group(1)
    return text.casefold()


def _looks_numeric(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text) and bool(_NUMERIC_CELL_RE.match(text))


def _row_labels(values: list) -> list[str]:
    """一行里可能作为行名的前三个单元格（跳过表头/数字/编号空串）。"""
    labels: list[str] = []
    for cell in list(values or [])[:3]:
        text = str(cell or "").strip()
        if not text or _looks_numeric(text):
            continue
        labels.append(text)
    return labels


def _row_fill_score(values: list) -> int:
    """数值填充度：用于在模糊候选里优先选真正有数据的行，而不是小节标题行。"""
    return sum(1 for cell in values or [] if _looks_numeric(cell))


def _is_label_column(name: object) -> bool:
    """行标签列本身就会返回行名，不能据此判定「单元格错位」。"""
    normalized = _normalize_label(name)
    if not normalized:
        return False
    if normalized in {"项目", "科目", "指标", "行名", "报表项目"}:
        return True
    return bool(re.fullmatch(r"col\d+", normalized))


def _tokenize(text: str) -> set[str]:
    """中文按二元组切、英文数字按整词切。

    为什么不用分词器：金融文档里"营业收入""归属于上市公司股东的净利润"这类
    术语，二元组能稳定覆盖；引入 jieba 会把"本年比上年增减"切成不可控的词，
    还要额外维护词典。二元组唯一的问题是长章节靠字数堆分，用 IDF + 长度归一压住。
    """
    tokens: set[str] = set()
    for run in _RUN_RE.findall(str(text or "").lower()):
        if run[0].isascii():
            if len(run) >= 2:
                tokens.add(run)
        elif len(run) <= 2:
            tokens.add(run)
        else:
            tokens.update(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


def _section_haystack(section: dict) -> str:
    parts = [
        " ".join(str(part) for part in (section.get("path") or [])),
        str(section.get("text") or ""),
        # 表格的表名与行标签：正文为空、内容在表里的小节只能靠这些词被检索到
        " ".join(str(word) for word in (section.get("table_keywords") or [])),
    ]
    return "\n".join(part for part in parts if part)


class FinanceStoreError(ValueError):
    """文档库读取错误（不是模型/网络错误）。"""


class FinanceStore:
    def __init__(self, root: str | Path, extra_roots: list[str | Path] | None = None):
        self.root = Path(root)
        # 额外根目录：用户上传的文档与官方语料分开放（评测只读官方语料），
        # 运行时把两边合成一个命名空间。额外根不存在时跳过，不影响主库。
        self.extra_roots = [Path(item) for item in (extra_roots or [])]
        self._documents: dict[str, dict] = {}
        self._sections: dict[str, tuple[dict, dict]] = {}
        self._tables: dict[str, tuple[dict, dict]] = {}
        self._df_cache: dict[str, tuple[dict[str, int], int]] = {}
        self._load()

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    @staticmethod
    def _document_paths(root: Path) -> list[Path]:
        return [root] if root.is_file() else sorted(root.glob("*/document.json"))

    def _load(self) -> None:
        if not self.root.exists():
            raise FinanceStoreError(f"文档库不存在: {self.root}")

        paths = self._document_paths(self.root)
        if not paths:
            raise FinanceStoreError(f"文档库里没有 document.json: {self.root}")
        for extra in self.extra_roots:
            if extra.exists():
                paths.extend(self._document_paths(extra))

        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FinanceStoreError(f"读取失败 {path}: {exc}") from exc
            self._add_document(document, source=path)

    def _add_document(self, document: dict, *, source: Path) -> None:
        if not isinstance(document, dict):
            raise FinanceStoreError(f"{source} 必须是 JSON 对象")
        doc_id = str(document.get("doc_id") or "").strip()
        if not doc_id:
            raise FinanceStoreError(f"{source} 缺少 doc_id")
        if doc_id in self._documents:
            raise FinanceStoreError(f"doc_id 重复: {doc_id}")

        sections = document.get("sections") or []
        tables = document.get("tables") or []
        if not isinstance(sections, list) or not isinstance(tables, list):
            raise FinanceStoreError(f"{doc_id} 的 sections/tables 必须是数组")

        self._documents[doc_id] = document
        for section in sections:
            section_id = str(section.get("section_id") or "").strip()
            if not section_id:
                raise FinanceStoreError(f"{doc_id} 有 section 缺少 section_id")
            if section_id in self._sections:
                raise FinanceStoreError(f"section_id 重复: {section_id}")
            self._sections[section_id] = (document, section)
        for table in tables:
            table_id = str(table.get("table_id") or "").strip()
            if not table_id:
                raise FinanceStoreError(f"{doc_id} 有 table 缺少 table_id")
            if table_id in self._tables:
                raise FinanceStoreError(f"table_id 重复: {table_id}")
            self._tables[table_id] = (document, table)

    # ------------------------------------------------------------------
    # 文档 / 章节 / 表格
    # ------------------------------------------------------------------

    def documents(self) -> list[dict]:
        """只读文档快照，供块索引/离线切片构建使用。"""
        return list(self._documents.values())

    def list_documents(self) -> list[dict]:
        rows = []
        for document in self._documents.values():
            rows.append({
                "doc_id": document.get("doc_id"),
                "title": document.get("title"),
                "company": document.get("company"),
                "code": document.get("code"),
                "doc_type": document.get("doc_type"),
                "period": document.get("period"),
                "announcement_date": document.get("announcement_date"),
                "published_at": document.get("published_at"),
                "currency": document.get("currency"),
                "unit": document.get("unit"),
                "scope": document.get("scope"),
                "restated": document.get("restated"),
                "source_url": document.get("source_url"),
                "origin": document.get("origin") or "corpus",
                "pipeline_version": document.get("pipeline_version") or "",
                "sections": len(document.get("sections") or []),
                "tables": len(document.get("tables") or []),
            })
        return sorted(rows, key=lambda row: str(row.get("doc_id")))

    def get_document(self, doc_id: str) -> dict:
        document = self._documents.get(str(doc_id))
        if document is None:
            raise FinanceStoreError(f"未知 doc_id: {doc_id}")
        return document

    def page_span(self, doc_id: str) -> dict:
        """文档的解析页码范围。

        不承诺 PDF 总页数：解析产物只记录了章节/表格出现的页码，
        这里返回的是“可定位到的最早/最晚页”，不是 PDF 物理页数。
        """
        document = self.get_document(doc_id)
        pages: list[int] = []
        for section in document.get("sections") or []:
            for key in ("page_start", "page_end"):
                value = section.get(key)
                if isinstance(value, int) and value > 0:
                    pages.append(value)
        for table in document.get("tables") or []:
            for key in ("page", "page_end"):
                value = table.get(key)
                if isinstance(value, int) and value > 0:
                    pages.append(value)
        return {
            "page_start": min(pages) if pages else None,
            "page_end": max(pages) if pages else None,
        }

    def locate(self, anchor: str) -> dict:
        """把证据锚点解析成可回查的坐标。

        锚点有五种形态，前端不解析，统一由这里给：
        - `section_id`
        - `section_id#b000`（段落块）
        - `table_id`
        - `table_id#rows#1,2#cols#0,1`（行组块）
        - `table_id#row#column`（单元格）

        `table_id` / `section_id` 本身含 `#`（`doc#t0006`），所以不能直接
        `split("#")`；用“已知 id 的最长前缀”匹配，避免把 doc 前缀当成 id。
        """
        text = str(anchor or "").strip()
        if not text:
            raise FinanceStoreError("锚点不能为空")

        for table_id in self._tables:
            if text == table_id:
                document, table = self._tables[table_id]
                return {
                    "anchor": text,
                    "kind": "table",
                    "doc_id": document.get("doc_id"),
                    "table_id": table_id,
                    "section_id": table.get("section_id"),
                    "row_indices": [],
                    "column_indices": [],
                }
            if text.startswith(f"{table_id}#rows#"):
                document, table = self._tables[table_id]
                rest = text[len(table_id) + len("#rows#"):]
                row_part, _, column_part = rest.partition("#cols#")
                row_indices = self._indices(row_part)
                column_indices = self._indices(column_part)
                return {
                    "anchor": text,
                    "kind": "table_rows",
                    "doc_id": document.get("doc_id"),
                    "table_id": table_id,
                    "section_id": table.get("section_id"),
                    "row_indices": row_indices,
                    "column_indices": column_indices,
                }
            if text.startswith(f"{table_id}#"):
                document, table = self._tables[table_id]
                rest = text[len(table_id) + 1:]
                row_token, _, column_token = rest.partition("#")
                if row_token and column_token:
                    row_index = self._resolve_row(table.get("rows") or [], row_token)
                    column_index = self._resolve_column(table.get("columns") or [], column_token)
                    return {
                        "anchor": text,
                        "kind": "table_cell",
                        "doc_id": document.get("doc_id"),
                        "table_id": table_id,
                        "section_id": table.get("section_id"),
                        "row_indices": [row_index],
                        "column_indices": [column_index],
                    }

        for section_id in self._sections:
            if text == section_id:
                document, section = self._sections[section_id]
                return {
                    "anchor": text,
                    "kind": "section",
                    "doc_id": document.get("doc_id"),
                    "section_id": section_id,
                    "row_indices": [],
                    "column_indices": [],
                }
            if text.startswith(f"{section_id}#b"):
                document, section = self._sections[section_id]
                return {
                    "anchor": text,
                    "kind": "paragraph_block",
                    "doc_id": document.get("doc_id"),
                    "section_id": section_id,
                    "row_indices": [],
                    "column_indices": [],
                }

        raise FinanceStoreError(f"无法识别的证据锚点: {anchor}")

    @staticmethod
    def _indices(text: str) -> list[int]:
        values: list[int] = []
        for part in str(text or "").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                values.append(int(part))
        return values

    def read_section(self, section_id: str) -> dict:
        entry = self._sections.get(str(section_id))
        if entry is None:
            raise FinanceStoreError(f"未知 section_id: {section_id}")
        document, section = entry
        return {
            "doc_id": document.get("doc_id"),
            "title": document.get("title"),
            "company": document.get("company"),
            "code": document.get("code"),
            "doc_type": document.get("doc_type"),
            "period": document.get("period"),
            "announcement_date": document.get("announcement_date"),
            "section_id": section.get("section_id"),
            "path": section.get("path") or [],
            "page_start": section.get("page_start"),
            "page_end": section.get("page_end"),
            "text": section.get("text") or "",
            # 年报的很多小节只有一句标题 + 一张表，正文为空。不给表格清单，
            # Agent 会读到一个空 section 然后判定"未披露"。
            "table_ids": list(section.get("table_ids") or []),
        }

    def get_table(self, table_id: str) -> dict:
        entry = self._tables.get(str(table_id))
        if entry is None:
            raise FinanceStoreError(f"未知 table_id: {table_id}")
        document, table = entry
        # 表格自身没标单位时回落到文档级单位，但把来源写出来——
        # 静默补一个单位等于替原文下结论，读者必须知道这是推断还是原文。
        unit, unit_source = table.get("unit") or "", "table"
        if not unit:
            unit, unit_source = document.get("unit") or "", "document"
        if not unit:
            unit_source = ""
        return {
            "doc_id": document.get("doc_id"),
            "title": document.get("title"),
            "table_id": table.get("table_id"),
            "section_id": table.get("section_id"),
            "page": table.get("page"),
            "page_end": table.get("page_end", table.get("page")),
            "caption": table.get("caption") or "",
            "unit": unit,
            "unit_source": unit_source,
            "statement_scope": table.get("statement_scope") or "",
            "statement_type": table.get("statement_type") or "",
            "footnote": table.get("footnote") or "",
            "bbox": table.get("bbox"),
            "columns": list(table.get("columns") or []),
            "rows": [list(row) for row in (table.get("rows") or [])],
        }

    @staticmethod
    def _cell_suspicion(
        value: object,
        row_name: str,
        column_name: str,
        columns: list[str],
    ) -> str:
        """识别「解析错位」的典型症状，避免把表头/行标签当数值证据。

        只做确定性判断，宁可少报也不猜：返回值等于行名/列名/其他列名，
        或金额行返回百分比，都说明行列可能错位。
        """
        text = str(value).strip() if value is not None else ""
        if _is_label_column(column_name):
            return ""
        # 空单元格不叫「错位」：稀疏表里大量存在，标出来只会淹没真正的错位。
        if not text:
            return ""
        normalized = _normalize_label(text)
        if normalized and normalized == _normalize_label(row_name):
            return "返回值等于行名"
        if normalized and normalized == _normalize_label(column_name):
            return "返回值等于列名"
        for name in columns:
            if str(name).strip() and normalized == _normalize_label(name):
                return "返回值是表头列名"
        ratio_words = (
            "率", "比例", "占比", "比重", "增减", "增长", "下降", "变动",
            "同比", "环比", "幅度", "%", "％",
        )
        if text.endswith(("%", "％")) and not any(
            word in str(row_name) or word in str(column_name) for word in ratio_words
        ):
            return "金额行返回百分比"
        return ""

    def get_cell(self, table_id: str, row: int | str, column: int | str) -> dict:
        table = self.get_table(table_id)
        columns = table["columns"]
        rows = table["rows"]

        row_index = self._resolve_row(rows, row)
        column_index = self._resolve_column(columns, column)
        if row_index >= len(rows):
            raise FinanceStoreError(f"行越界: {row}")
        if column_index >= len(columns):
            raise FinanceStoreError(f"列越界: {column}")

        value = rows[row_index][column_index] if column_index < len(rows[row_index]) else None
        row_name = self._row_label(rows[row_index])
        suspicion = self._cell_suspicion(value, row_name, columns[column_index], columns)
        result = {
            "doc_id": table["doc_id"],
            "title": table["title"],
            "table_id": table["table_id"],
            "page": table["page"],
            "caption": table["caption"],
            "table_caption": table["caption"],
            "unit": table.get("unit", ""),
            "unit_source": table.get("unit_source", ""),
            "statement_scope": table.get("statement_scope") or "",
            "statement_type": table.get("statement_type") or "",
            "row": row_index,
            "row_name": row_name,
            "column": columns[column_index],
            "column_index": column_index,
            "value": value,
            "cell_text": value,
            "bbox": table.get("bbox"),
            "row_values": rows[row_index],
        }
        if suspicion:
            result["misalignment"] = {"suspected": True, "reason": suspicion}
        return result

    def get_rows(
        self,
        table_id: str,
        rows: list[int | str],
        columns: list[int | str] | None = None,
    ) -> dict:
        """读表格中的相关行，保留表名、单位、页码与原始行列坐标。

        这是 `get_cell` 和 `get_table` 之间的粒度：普通查数只需要目标行及
        同组行，不必把几百行的整张表塞进上下文。`rows` 必须显式给出，
        工具层不替调用方猜“哪些行相关”。
        """
        table = self.get_table(table_id)
        all_rows = table["rows"]
        all_columns = table["columns"]
        if not isinstance(rows, list) or not rows:
            raise FinanceStoreError("rows 必须是非空数组")
        if len(rows) > 20:
            raise FinanceStoreError("rows 一次最多 20 行")

        row_indices: list[int] = []
        skipped_rows: list[dict] = []
        first_error: FinanceStoreError | None = None
        for row in rows:
            try:
                index = self._resolve_row(all_rows, row)
            except FinanceStoreError as exc:
                # 部分成功：一行拼错不应该让整次读取失败。把失败原因回显给模型，
                # 有效行照常返回（FIN-S01 实测：rows=[10..14] 里只有 13 越界，
                # 旧实现直接抛错，模型只能重新检索，最后预算耗尽拒答）。
                if first_error is None:
                    first_error = exc
                skipped_rows.append({"input": row, "reason": str(exc)[:200]})
                continue
            if index >= len(all_rows):
                exc = FinanceStoreError(
                    f"行越界: {row}；该表共 {len(all_rows)} 行（0–{len(all_rows) - 1}）"
                )
                if first_error is None:
                    first_error = exc
                skipped_rows.append({"input": row, "reason": str(exc)[:200]})
                continue
            if index in row_indices:
                raise FinanceStoreError(f"重复行: {row}")
            row_indices.append(index)

        if not row_indices:
            raise first_error or FinanceStoreError("没有可读取的行")

        if columns is None:
            column_indices = list(range(len(all_columns)))
        else:
            if not isinstance(columns, list) or not columns:
                raise FinanceStoreError("columns 必须是非空数组或省略")
            if len(columns) > 12:
                raise FinanceStoreError("columns 一次最多 12 列")
            column_indices = []
            for column in columns:
                index = self._resolve_column(all_columns, column)
                if index >= len(all_columns):
                    raise FinanceStoreError(f"列越界: {column}")
                if index in column_indices:
                    raise FinanceStoreError(f"重复列: {column}")
                column_indices.append(index)

        output_rows = []
        suspected_cells: list[dict] = []
        for index in row_indices:
            values = all_rows[index]
            row_name = self._row_label(values)
            cells = []
            for column_index in column_indices:
                value = values[column_index] if column_index < len(values) else None
                cells.append(
                    {
                        "column": all_columns[column_index],
                        "column_index": column_index,
                        "value": value,
                    }
                )
                suspicion = self._cell_suspicion(
                    value, row_name, all_columns[column_index], all_columns
                )
                if suspicion:
                    suspected_cells.append(
                        {
                            "row": index,
                            "row_name": row_name,
                            "column": all_columns[column_index],
                            "reason": suspicion,
                        }
                    )
            output_rows.append(
                {
                    "row": index,
                    "row_name": row_name,
                    "values": [
                        values[column_index] if column_index < len(values) else None
                        for column_index in column_indices
                    ],
                    "cells": cells,
                }
            )

        result = {
            "doc_id": table["doc_id"],
            "title": table["title"],
            "table_id": table["table_id"],
            "page": table["page"],
            "page_end": table.get("page_end", table["page"]),
            "caption": table["caption"],
            "table_caption": table["caption"],
            "unit": table.get("unit", ""),
            "unit_source": table.get("unit_source", ""),
            "statement_scope": table.get("statement_scope") or "",
            "statement_type": table.get("statement_type") or "",
            "footnote": table.get("footnote") or "",
            "columns": [all_columns[index] for index in column_indices],
            "column_indices": column_indices,
            "rows": output_rows,
            "row_indices": row_indices,
            "total_rows": len(all_rows),
        }
        if skipped_rows:
            result["skipped_rows"] = skipped_rows
        if suspected_cells:
            result["suspected_misalignment"] = suspected_cells[:10]
        return result

    @staticmethod
    def _row_label(values: list[str]) -> str:
        """行名：优先取前三个单元格里第一个「有意义的」标签。

        合并单元格表常把编号前缀（「一、」「其中：」）放在首列、行名放在第 2 列；
        这些前缀归一化后为空，不能当行名。纯数字单元格（页码/序号）也跳过。
        """
        for cell in list(values or [])[:3]:
            text = str(cell).strip()
            if not text or _looks_numeric(text):
                continue
            if _normalize_label(text):
                return text
        return ""

    @classmethod
    def _resolve_row(cls, rows: list[list[str]], row: int | str) -> int:
        if isinstance(row, bool):
            raise FinanceStoreError("row 不能是布尔值")
        if isinstance(row, int):
            if row < 0:
                raise FinanceStoreError("row 必须 >= 0")
            if row < len(rows):
                return row
            # 模型常把年份当行号传（如 2025）；先按行名再判越界。
            matched = cls._match_row_text(rows, str(row))
            if matched is not None:
                return matched
            raise FinanceStoreError(
                f"行越界: {row}；该表共 {len(rows)} 行（0–{len(rows) - 1}）"
            )
        text = str(row).strip()
        if not text:
            raise FinanceStoreError("行名不能为空")
        matched = cls._match_row_text(rows, text)
        if matched is not None:
            return matched
        raise FinanceStoreError(f"找不到行: {row}；{cls._row_hint(rows)}")

    @classmethod
    def _match_row_text(cls, rows: list[list[str]], text: str) -> int | None:
        """按「原样 → 归一化 → 别名 → 模糊」四层匹配行名。"""
        # 1) 原样：首列优先（保留旧行为，包括数字行号）；行名落到第 2/3 列时，
        # 只匹配非数字标签，避免把「45」这类页码/序号当成行名。
        for index, values in enumerate(rows):
            if values and str(values[0]).strip() == text:
                return index
        if not _looks_numeric(text):
            for index, values in enumerate(rows):
                for cell in list(values or [])[1:3]:
                    cell_text = str(cell).strip()
                    if cell_text == text and not _looks_numeric(cell_text):
                        return index

        normalized = _normalize_label(text)
        if not normalized:
            return None

        # 2) 归一化：去掉单位括号、空格、编号前缀，并把「2025 年」视为「2025」。
        for index, values in enumerate(rows):
            for label in _row_labels(values):
                if _normalize_label(label) == normalized:
                    return index

        # 3) 别名：营业收入 ↔ 营业总收入、归母净利润 ↔ 归属于上市公司股东的净利润。
        for alias in _ROW_ALIASES.get(normalized, ()):
            target = _normalize_label(alias)
            if not target:
                continue
            for index, values in enumerate(rows):
                for label in _row_labels(values):
                    if _normalize_label(label) == target:
                        return index

        # 4) 纯数字仍按行号解析（证据锚点 `doc#t1#0#1` 的 row_token 是字符串）。
        if text.isdigit():
            index = int(text)
            return index if 0 <= index < len(rows) else None

        # 5) 模糊：只在相似度足够高时命中；用数值填充度打破平局。
        candidates: list[tuple[float, int, int]] = []
        for index, values in enumerate(rows):
            labels = _row_labels(values)
            if not labels:
                continue
            best = 0.0
            for label in labels:
                target = _normalize_label(label)
                score = SequenceMatcher(None, normalized, target).ratio()
                if len(normalized) >= 3 and (
                    normalized in target or target in normalized
                ):
                    score = max(score, 0.82)
                best = max(best, score)
            if best >= 0.72:
                candidates.append((best, _row_fill_score(values), index))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1], -item[2]), reverse=True)
        return candidates[0][2]

    @staticmethod
    def _row_hint(rows: list[list[str]]) -> str:
        labels: list[str] = []
        for values in rows:
            for label in _row_labels(values):
                if label not in labels:
                    labels.append(label)
            if len(labels) >= 8:
                break
        head = f"该表共 {len(rows)} 行"
        if not labels:
            return f"{head}，没有可用行名"
        return f"{head}；可用行名: {'、'.join(labels[:8])}"

    @classmethod
    def _resolve_column(cls, columns: list[str], column: int | str) -> int:
        if isinstance(column, bool):
            raise FinanceStoreError("column 不能是布尔值")
        if isinstance(column, int):
            if column < 0:
                raise FinanceStoreError("column 必须 >= 0")
            if column < len(columns):
                return column
            # 模型常把列名写成整数（column=2025），先按列名匹配再判越界。
            matched = cls._match_column_text(columns, str(column))
            if matched is not None:
                return matched
            raise FinanceStoreError(
                f"列越界: {column}；该表共 {len(columns)} 列"
                f"（0–{len(columns) - 1}）；{cls._column_hint(columns)}"
            )
        text = str(column).strip()
        if not text:
            raise FinanceStoreError("列名不能为空")
        matched = cls._match_column_text(columns, text)
        if matched is not None:
            return matched
        raise FinanceStoreError(f"找不到列: {column}；{cls._column_hint(columns)}")

    @classmethod
    def _match_column_text(cls, columns: list[str], text: str) -> int | None:
        for index, name in enumerate(columns):
            if str(name).strip() == text:
                return index
        normalized = _normalize_label(text)
        if not normalized:
            return None
        for index, name in enumerate(columns):
            if _normalize_label(name) == normalized:
                return index
        # 纯数字仍按列号解析（证据锚点里的 column_token 是字符串）。
        if text.isdigit():
            index = int(text)
            return index if 0 <= index < len(columns) else None
        candidates: list[tuple[float, int]] = []
        for index, name in enumerate(columns):
            target = _normalize_label(name)
            score = SequenceMatcher(None, normalized, target).ratio()
            if len(normalized) >= 2 and (
                normalized in target or target in normalized
            ):
                score = max(score, 0.82)
            if score >= 0.8:
                candidates.append((score, index))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        return candidates[0][1]

    @staticmethod
    def _column_hint(columns: list[str]) -> str:
        names = [str(name).strip() for name in columns if str(name).strip()]
        if not names:
            return "该表没有可用列名"
        return f"可用列名: {'、'.join(names[:8])}"

    # ------------------------------------------------------------------
    # 检索（v1：IDF + 路径加权 + 长度归一；结构路由在 Phase 4）
    # ------------------------------------------------------------------

    def _df_for(self, doc_id: Optional[str]) -> tuple[dict[str, int], int]:
        """文档频率缓存。按 doc 作用域算一次，避免每次检索重扫全部章节。"""
        key = doc_id or "__all__"
        cached = self._df_cache.get(key)
        if cached is not None:
            return cached
        counter: dict[str, int] = {}
        total = 0
        for document, section in self._sections.values():
            if doc_id and document.get("doc_id") != doc_id:
                continue
            total += 1
            hay = _section_haystack(section)
            for token in _tokenize(hay):
                counter[token] = counter.get(token, 0) + 1
        self._df_cache[key] = (counter, total)
        return counter, total

    def search_sections(
        self,
        query: str,
        *,
        doc_id: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        question = str(query or "").strip()
        if not question:
            return []
        tokens = _tokenize(question)
        if not tokens:
            return []
        df, total = self._df_for(doc_id)
        if not total:
            return []

        scored: list[tuple[float, dict, dict]] = []
        for document, section in self._sections.values():
            if doc_id and document.get("doc_id") != doc_id:
                continue
            path_text = " ".join(str(part) for part in (section.get("path") or [])).lower()
            body_text = (
                str(section.get("text") or "")
                + " "
                + " ".join(str(word) for word in (section.get("table_keywords") or []))
            ).lower()
            score = 0.0
            for token in tokens:
                # 停用词（"2025"这类到处都有的词）IDF 接近 0，自动失去话语权。
                # 分母用 df+0.5 而不是 df+1：语料只有一两节时，词出现在所有节里
                # 会算出 0 权重，检索直接返回空——小语料的自测就是这么挂的。
                weight = math.log((total + 1) / (df.get(token, 0) + 0.5))
                if weight <= 0:
                    continue
                path_hits = path_text.count(token)
                body_hits = body_text.count(token)
                if not path_hits and not body_hits:
                    continue
                # 路径命中权重 3 倍：标题对上了就是对的章节
                # 正文计数封顶 8，避免长章节靠字数堆分
                score += weight * (3 * path_hits + min(body_hits, 8))
            if score <= 0:
                continue
            # 长度归一：不除长度，附录里又长又杂的小节会一直排第一
            score = score / (1 + math.log1p(len(body_text) / 400))
            scored.append((score, document, section))

        scored.sort(key=lambda item: (-item[0], str(item[2].get("section_id"))))
        results = []
        for score, document, section in scored[: max(1, min(int(top_k), 20))]:
            results.append({
                "doc_id": document.get("doc_id"),
                "title": document.get("title"),
                "section_id": section.get("section_id"),
                "path": section.get("path") or [],
                "page_start": section.get("page_start"),
                "page_end": section.get("page_end"),
                "table_ids": list(section.get("table_ids") or []),
                "score": round(score, 3),
                "snippet": str(section.get("text") or "")[:160],
            })
        return results
