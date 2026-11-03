"""金融语料视图：给 judge 与 lint 一份 `chunk_id -> 原文`。

和制度库的区别：制度库的 `AGENT_CORPUS` 是手写常量（60 chunk），金融这边
是 `data/finance/documents/*/document.json` 现读——2842 节 + 1114 张表，
不可能手抄。所以这里只做**索引**，真值仍以解析产物为准。

引用锚点两种：

| 锚点 | 例子 |
|---|---|
| 章节 | `cninfo-000333-2025-annual#s0009` |
| 表格 | `cninfo-000333-2025-annual#t0006` |

**不把单元格当锚点**（`t0006#4#2025 年`）：行号列名会随解析版本漂移，
任务卡写死它，重解析一次就全断。要限定"哪个单元格"，写在
`answer_point.evidence` 里，由 `lint_finance_tasks` 对着表格现查。

给 judge 的正文有长度上限：一份年报的「管理层讨论」动辄两万字，
整段塞进判分 prompt 既贵又会让要点淹没在噪声里。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
FINANCE_ROOT = _BACKEND_DIR.parent / "data" / "finance"
DOCUMENT_DIR = FINANCE_ROOT / "documents"
MANIFEST = _BACKEND_DIR.parent / "docs" / "finance" / "corpus_manifest.json"

#: 单条证据喂给 judge 的字符上限
JUDGE_SECTION_CHARS = 1500
JUDGE_TABLE_CHARS = 2500


def _render_table(table: dict) -> str:
    head = (
        f"表 {table.get('table_id')}｜{table.get('caption') or '无标题'}"
        f"｜PDF 第 {table.get('page')} 页｜单位：{table.get('unit') or '未标注'}"
    )
    lines = [head, " | ".join(str(name) for name in table.get("columns") or [])]
    for row in table.get("rows") or []:
        lines.append(" | ".join(str(cell) for cell in row))
        if sum(len(line) for line in lines) > JUDGE_TABLE_CHARS:
            lines.append("…[表格过长已截断，完整内容见 PDF]")
            break
    return "\n".join(lines)


@lru_cache(maxsize=1)
def chunk_map() -> dict[str, dict]:
    """`chunk_id -> {chunk_id, doc_id, title, content, kind, page}`。

    首次调用会读全部 document.json（约 1–2 秒），之后走缓存。
    """
    if not DOCUMENT_DIR.exists():
        raise FileNotFoundError(
            f"金融文档库不存在: {DOCUMENT_DIR}\n先跑 python -m scripts.ingest_finance_corpus"
        )
    chunks: dict[str, dict] = {}
    for path in sorted(DOCUMENT_DIR.glob("*/document.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        # 解析产物把 company/period/doc_type 放在顶层；旧版本曾放在 metadata 下。
        # 只读 metadata 会让所有标题变成「   ｜无标题表格」，judge 与失败归类都失去
        # 公司/期间上下文——2026-10-31 由 failure_taxonomy 的误报暴露。
        metadata = document.get("metadata") or {}
        doc_id = (
            metadata.get("doc_id")
            or document.get("doc_id")
            or path.parent.name
        )
        company = metadata.get("company") or document.get("company") or ""
        period = str(metadata.get("period") or document.get("period") or "")
        doc_type = metadata.get("doc_type") or document.get("doc_type") or ""
        for section in document.get("sections") or []:
            section_id = str(section.get("section_id") or "")
            if not section_id:
                continue
            path_text = " > ".join(section.get("path") or [])
            body = str(section.get("text") or "")[:JUDGE_SECTION_CHARS]
            chunks[section_id] = {
                "chunk_id": section_id,
                "doc_id": doc_id,
                "title": f"{company} {period} {doc_type}｜{path_text}".strip(),
                "content": f"{path_text}\n{body}".strip(),
                "kind": "section",
                "page": section.get("page_start"),
            }
        for table in document.get("tables") or []:
            table_id = str(table.get("table_id") or "")
            if not table_id:
                continue
            chunks[table_id] = {
                "chunk_id": table_id,
                "doc_id": doc_id,
                "title": f"{company} {period} {doc_type}｜{table.get('caption') or '无标题表格'}",
                "content": _render_table(table),
                "kind": "table",
                "page": table.get("page"),
            }
    return chunks


@lru_cache(maxsize=1)
def table_map() -> dict[str, dict]:
    """`table_id -> table dict`，用于判定“等价表”里的行/列/值。

    与 `chunk_map` 分开：judge 只需要渲染文本，等价判定需要原始 rows/columns。
    """
    if not DOCUMENT_DIR.exists():
        raise FileNotFoundError(
            f"金融文档库不存在: {DOCUMENT_DIR}\n先跑 python -m scripts.ingest_finance_corpus"
        )
    tables: dict[str, dict] = {}
    for path in sorted(DOCUMENT_DIR.glob("*/document.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        doc_id = str(document.get("doc_id") or path.parent.name)
        for table in document.get("tables") or []:
            table_id = str(table.get("table_id") or "")
            if table_id:
                tables[table_id] = {**table, "doc_id": doc_id}
    return tables


def corpus_hash() -> str:
    """语料指纹：直接用 manifest 里的 PDF sha256，重解析不会让指纹变化。

    这一点和制度库相反（那边 hash 的是 chunk 文本），因为金融的"语料"
    是 PDF 本身——解析器升级会改 chunk 文本，但证据同一份。
    """
    digest = hashlib.sha256()
    if not MANIFEST.exists():
        return ""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for document in sorted(manifest.get("documents") or [], key=lambda item: item.get("doc_id") or ""):
        digest.update(str(document.get("doc_id") or "").encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(str(document.get("sha256") or "").encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def doc_inventory() -> list[dict]:
    """按文档聚合：doc_id / 公司 / 期数 / 类型 / 章节数 / 表数。"""
    if not DOCUMENT_DIR.exists():
        return []
    inventory: list[dict] = []
    for path in sorted(DOCUMENT_DIR.glob("*/document.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        metadata = document.get("metadata") or {}
        inventory.append(
            {
                "doc_id": metadata.get("doc_id") or document.get("doc_id") or path.parent.name,
                "company": metadata.get("company") or document.get("company"),
                "period": str(metadata.get("period") or document.get("period") or ""),
                "doc_type": metadata.get("doc_type") or document.get("doc_type"),
                "sections": len(document.get("sections") or []),
                "tables": len(document.get("tables") or []),
            }
        )
    return inventory
