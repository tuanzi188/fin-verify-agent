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
from pathlib import Path
from typing import Optional

_RUN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


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
        return {
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
            "row_name": self._row_label(rows[row_index]),
            "column": columns[column_index],
            "column_index": column_index,
            "value": value,
            "cell_text": value,
            "bbox": table.get("bbox"),
            "row_values": rows[row_index],
        }

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
        for row in rows:
            index = self._resolve_row(all_rows, row)
            if index >= len(all_rows):
                raise FinanceStoreError(f"行越界: {row}")
            if index in row_indices:
                raise FinanceStoreError(f"重复行: {row}")
            row_indices.append(index)

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
        for index in row_indices:
            values = all_rows[index]
            cells = [
                {
                    "column": all_columns[column_index],
                    "column_index": column_index,
                    "value": values[column_index] if column_index < len(values) else None,
                }
                for column_index in column_indices
            ]
            output_rows.append(
                {
                    "row": index,
                    "row_name": self._row_label(values),
                    "values": [
                        values[column_index] if column_index < len(values) else None
                        for column_index in column_indices
                    ],
                    "cells": cells,
                }
            )

        return {
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

    @staticmethod
    def _row_label(values: list[str]) -> str:
        """行名：首列优先；首列为空时取第二列（合并单元格表常见）。"""
        for cell in values[:2]:
            text = str(cell).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _resolve_row(rows: list[list[str]], row: int | str) -> int:
        if isinstance(row, bool):
            raise FinanceStoreError("row 不能是布尔值")
        if isinstance(row, int):
            if row < 0:
                raise FinanceStoreError("row 必须 >= 0")
            return row
        text = str(row).strip()
        # 先按首列名称匹配，再退回行号：金融表格里“2024”既可能是列名，也可能是行号。
        for index, values in enumerate(rows):
            if values and str(values[0]).strip() == text:
                return index
        # 有些表的行标签落在第 2 列：首列是合并单元格/空列，行名在下一列。
        # 2026-11-03 实测比亚迪「营业收入构成」t0013：`营业收入合计` 在 col2，
        # 按首列查会全部 not_found。只在首列为空的行上回退，避免误命中。
        for index, values in enumerate(rows):
            if (
                len(values) > 1
                and not str(values[0]).strip()
                and str(values[1]).strip() == text
            ):
                return index
        if text.isdigit():
            return int(text)
        raise FinanceStoreError(f"找不到行: {row}")

    @staticmethod
    def _resolve_column(columns: list[str], column: int | str) -> int:
        if isinstance(column, bool):
            raise FinanceStoreError("column 不能是布尔值")
        if isinstance(column, int):
            if column < 0:
                raise FinanceStoreError("column 必须 >= 0")
            return column
        text = str(column).strip()
        # 列名优先：金融表的列名经常就是“2025”“2024”，不能先当列号解析。
        for index, name in enumerate(columns):
            if str(name).strip() == text:
                return index
        if text.isdigit():
            return int(text)
        raise FinanceStoreError(f"找不到列: {column}")

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
