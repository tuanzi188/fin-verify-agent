"""结构感知的一次检索 RAG 语料：正文按段落、表格按行组切片。

这是第三条对照臂，不替换预注册的固定窗基线：

- 普通 RAG：整篇拍平后 512 字硬切，用来回答"只有普通文本上下文时能做到什么"；
- 结构感知 RAG：保留章节边界、表头、行列名、单位与页码，用来回答
  "同样的证据结构交给一次检索 RAG，能否缩小与 Agent 的差距"。

两臂共用同一检索器（bge + BM25 + RRF）与同一生成预算；结构臂额外使用
问题里已有的公司/报告期 metadata 过滤，避免拿 2024 年报去回答限定 2025 年的题。
结构感知臂仍然只检索一次、生成一次：没有多轮、没有修复、没有计算器。

参数是实验起点，不是已验收结论：
- 正文块 400–800 字符，尽量在段落/句子边界切开，单句重叠避免断义；
- 表格默认连续 6 行一组、组间重叠 1 行；宽表按首列 + 8 个数据列切列组；
- 每块都带 doc_id、公司、报告期、表名/章节路径、页码和原始行列坐标。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from functools import lru_cache

from app.finance.store import _tokenize
from evaluation import rag_corpus
from evaluation.finance_corpus import DOCUMENT_DIR

CORPUS_VERSION = "finance-structure-v1"
BODY_MIN_CHARS = 400
BODY_MAX_CHARS = 800
BODY_OVERLAP_MAX_CHARS = 120
TABLE_GROUP_ROWS = 6
TABLE_GROUP_OVERLAP_ROWS = 1
TABLE_MAX_COLUMNS = 8

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_YEAR_PATTERN = re.compile(r"(20\d{2})\s*年")


def periods_for_question(question: str) -> set[str] | None:
    """问题点名的报告期；没写年份则不按期间过滤。

    这是普通 metadata 过滤，不算表格结构：问题写“2025 年”时仍把 2024 年报
    混进 top-k，会让结构臂的比较变成“谁更会猜年份”，而不是“证据单元谁更好”。
    跨年题（2025 vs 2024）会同时保留两个年度。
    """
    years = set(_YEAR_PATTERN.findall(str(question or "")))
    return years or None


def _pack_sentences(text: str) -> list[str]:
    """把章节正文按句子边界装进大小合适的块，不跨章节。

    优先保留整段；只有超长时才按句拆。块之间只重叠上一块最后一句，
    且该句不超过 120 字，避免用大段重复换边界连续性。
    """
    text = str(text or "").strip()
    if not text:
        return []
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    if not sentences:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> str | None:
        nonlocal current, current_len
        if not current:
            return None
        value = "".join(current).strip()
        current = []
        current_len = 0
        return value or None

    for sentence in sentences:
        # 极长单句（PDF 偶尔整页无标点）：先硬切成上限，避免一个块吞掉整章。
        if len(sentence) > BODY_MAX_CHARS:
            pending = flush()
            if pending:
                chunks.append(pending)
            for start in range(0, len(sentence), BODY_MAX_CHARS):
                chunks.append(sentence[start:start + BODY_MAX_CHARS])
            continue

        projected = current_len + len(sentence)
        if current and projected > BODY_MAX_CHARS:
            previous = list(current)
            pending = flush()
            if pending:
                chunks.append(pending)
            # 只重叠上一块的最后一句，且该句足够短；不要拿尚未入块的新句充当重叠。
            carry = previous[-1] if len(previous[-1]) <= BODY_OVERLAP_MAX_CHARS else ""
            current = [carry] if carry else []
            current_len = len(carry)

        current.append(sentence)
        current_len += len(sentence)

    pending = flush()
    if pending:
        chunks.append(pending)
    return chunks


def _column_groups(column_count: int) -> list[list[int]]:
    """宽表按首列 + 数据列切组；窄表保留全部列。"""
    if column_count <= 0:
        return []
    if column_count <= TABLE_MAX_COLUMNS + 1:
        return [list(range(column_count))]
    first = [0]
    data_columns = list(range(1, column_count))
    return [
        first + data_columns[start:start + TABLE_MAX_COLUMNS]
        for start in range(0, len(data_columns), TABLE_MAX_COLUMNS)
    ]


def _row_groups(row_count: int) -> list[list[int]]:
    if row_count <= 0:
        return []
    if row_count <= TABLE_GROUP_ROWS:
        return [list(range(row_count))]
    step = max(TABLE_GROUP_ROWS - TABLE_GROUP_OVERLAP_ROWS, 1)
    groups = []
    for start in range(0, row_count, step):
        group = list(range(start, min(start + TABLE_GROUP_ROWS, row_count)))
        if group:
            groups.append(group)
        if start + TABLE_GROUP_ROWS >= row_count:
            break
    return groups


def _render_table_group(
    document: dict,
    table: dict,
    row_indices: list[int],
    column_indices: list[int],
) -> str:
    columns = [str(table.get("columns", [])[index]) for index in column_indices]
    unit = str(table.get("unit") or document.get("unit") or "未标注")
    lines = [
        f"文档：{document.get('company') or ''} {document.get('period') or ''} "
        f"{document.get('doc_type') or ''}",
        f"表：{table.get('caption') or '未命名表格'}｜表 {table.get('table_id')}｜"
        f"PDF 第 {table.get('page')} 页｜单位：{unit}",
        " | ".join(columns),
    ]
    for index in row_indices:
        row = table.get("rows", [])[index]
        values = [
            str(row[column_index]) if column_index < len(row) else ""
            for column_index in column_indices
        ]
        lines.append(f"[行{index}] " + " | ".join(values))
    if table.get("footnote"):
        lines.append(f"脚注：{table.get('footnote')}")
    return "\n".join(lines)


def _document_chunks(document: dict) -> list[dict]:
    doc_id = str(document.get("doc_id") or "")
    company = str(document.get("company") or "")
    period = str(document.get("period") or "")
    doc_type = str(document.get("doc_type") or "")
    title = f"{company} {period} {doc_type}".strip()
    chunks: list[dict] = []

    for section in document.get("sections") or []:
        section_id = str(section.get("section_id") or "")
        body = str(section.get("text") or "")
        if not section_id or not body.strip():
            continue
        path_text = " > ".join(str(part) for part in section.get("path") or [])
        for index, body_chunk in enumerate(_pack_sentences(body)):
            content = f"{path_text}\n{body_chunk}" if path_text else body_chunk
            chunks.append(
                {
                    "chunk_id": f"{section_id}#b{index:03d}",
                    "doc_id": doc_id,
                    "period": period,
                    "source_id": section_id,
                    "source_ids": [section_id],
                    "source_spans": [
                        {
                            "source_id": section_id,
                            "kind": "section",
                            "page": section.get("page_start"),
                            "start": 0,
                            "end": len(body_chunk),
                            "chars": len(body_chunk),
                        }
                    ],
                    "kind": "paragraph",
                    "page": section.get("page_start"),
                    "title": title,
                    "path": list(section.get("path") or []),
                    "content": content,
                }
            )

    for table in document.get("tables") or []:
        table_id = str(table.get("table_id") or "")
        rows = table.get("rows") or []
        columns = table.get("columns") or []
        if not table_id or not rows or not columns:
            continue
        for row_indices in _row_groups(len(rows)):
            for column_indices in _column_groups(len(columns)):
                chunks.append(
                    {
                        "chunk_id": (
                            f"{table_id}#rows#"
                            + ",".join(str(index) for index in row_indices)
                            + "#cols#"
                            + ",".join(str(index) for index in column_indices)
                        ),
                        "doc_id": doc_id,
                        "period": period,
                        "source_id": table_id,
                        "source_ids": [table_id],
                        "source_spans": [
                            {
                                "source_id": table_id,
                                "kind": "table",
                                "page": table.get("page"),
                                "start": 0,
                                "end": len(rows),
                                "chars": len(row_indices),
                            }
                        ],
                        "kind": "table_rows",
                        "table_id": table_id,
                        "table_caption": table.get("caption") or "",
                        "row_indices": row_indices,
                        "column_indices": column_indices,
                        "page": table.get("page"),
                        "title": title,
                        "content": _render_table_group(
                            document, table, row_indices, column_indices
                        ),
                    }
                )
    return chunks


@lru_cache(maxsize=1)
def structured_chunks() -> list[dict]:
    chunks: list[dict] = []
    for path in sorted(DOCUMENT_DIR.glob("*/document.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        chunks.extend(_document_chunks(document))
    return chunks


@lru_cache(maxsize=1)
def _index() -> tuple[list[dict], dict[str, list[int]], dict[str, float], list[int], float]:
    chunks = structured_chunks()
    postings: dict[str, list[int]] = {}
    lengths: list[int] = []
    for index, chunk in enumerate(chunks):
        tokens = _tokenize(f"{chunk['title']}\n{chunk['content']}")
        lengths.append(len(tokens))
        for token in tokens:
            postings.setdefault(token, []).append(index)
    total = len(chunks)
    idf = {
        token: math.log((total - len(items) + 0.5) / (len(items) + 0.5) + 1.0)
        for token, items in postings.items()
    }
    average = sum(lengths) / max(len(lengths), 1)
    return chunks, postings, idf, lengths, average


def _allowed_indices(
    chunks: list[dict],
    doc_codes: list[str] | None,
    periods: set[str] | None = None,
) -> set[int] | None:
    if not doc_codes and not periods:
        return None
    allowed = set()
    for index, chunk in enumerate(chunks):
        doc_id = str(chunk.get("doc_id") or "")
        if doc_codes and not any(doc_id.startswith(f"cninfo-{code}") for code in doc_codes):
            continue
        if periods and str(chunk.get("period") or "") not in periods:
            continue
        allowed.add(index)
    return allowed or None


def _bm25_rank(query: str, allowed: set[int] | None, depth: int) -> list[int]:
    chunks, postings, idf, lengths, average = _index()
    tokens = rag_corpus._query_tokens(_tokenize(query), postings, len(chunks))
    if not tokens:
        return []
    scores: dict[int, float] = {}
    matched: dict[int, int] = {}
    for token in tokens:
        weight = idf.get(token)
        if weight is None:
            continue
        for index in postings[token]:
            if allowed is not None and index not in allowed:
                continue
            norm = (
                rag_corpus.BM25_K1
                * (1 - rag_corpus.BM25_B + rag_corpus.BM25_B * lengths[index] / average)
                + 1
            )
            scores[index] = (
                scores.get(index, 0.0)
                + weight * (rag_corpus.BM25_K1 + 1) / norm
            )
            matched[index] = matched.get(index, 0) + 1
    for index in scores:
        scores[index] *= (matched[index] / len(tokens)) ** 0.5
    return [
        index
        for index, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:depth]
    ]


@lru_cache(maxsize=1)
def _embeddings() -> object:
    import numpy as np

    chunks = structured_chunks()
    digest = hashlib.sha256(CORPUS_VERSION.encode("utf-8"))
    for chunk in chunks:
        digest.update(f"{chunk['chunk_id']}|{chunk['content']}".encode("utf-8"))
    fingerprint = digest.hexdigest()[:16]
    path = rag_corpus.EMBED_CACHE_DIR / f"structure-emb-{fingerprint}.npy"
    if path.exists():
        return np.load(path)

    model = rag_corpus._model()
    texts = [f"{chunk['title']}\n{chunk['content']}" for chunk in chunks]
    vectors = model.encode(
        texts,
        batch_size=32,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, vectors)
    return vectors


def _dense_rank(query: str, allowed: set[int] | None, depth: int) -> list[int]:
    import numpy as np

    vectors = _embeddings()
    model = rag_corpus._model()
    query_vector = model.encode(
        [f"{rag_corpus.BGE_QUERY_PREFIX}{query}"],
        normalize_embeddings=True,
        show_progress_bar=False,
    )[0]
    similarities = np.asarray(vectors @ query_vector)
    if allowed is not None:
        mask = np.full(similarities.shape, -np.inf)
        mask[list(allowed)] = similarities[list(allowed)]
        similarities = mask
    order = np.argsort(-similarities)[:depth]
    return [int(index) for index in order if np.isfinite(similarities[index])]


def search(query: str, top_k: int = 6, doc_codes: list[str] | None = None) -> list[dict]:
    """结构块上的同款混合检索；只改变证据单元，不改变检索器与生成预算。"""
    chunks = structured_chunks()
    allowed = _allowed_indices(chunks, doc_codes, periods_for_question(query))
    fused: dict[int, float] = {}
    for ranked in (
        _dense_rank(query, allowed, rag_corpus.CANDIDATE_DEPTH),
        _bm25_rank(query, allowed, rag_corpus.CANDIDATE_DEPTH),
    ):
        for rank, index in enumerate(ranked, 1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (rag_corpus.RRF_K + rank)
    top = sorted(fused.items(), key=lambda item: item[1], reverse=True)[: max(1, top_k)]
    results = []
    for index, score in top:
        chunk = dict(chunks[index])
        chunk["score"] = round(score, 6)
        results.append(chunk)
    return results


def docs_for_question(question: str) -> list[str] | None:
    return rag_corpus.docs_for_question(question)


def stats() -> dict:
    chunks = structured_chunks()
    return {
        "corpus_version": CORPUS_VERSION,
        "chunks": len(chunks),
        "prose_chunks": sum(1 for chunk in chunks if chunk["kind"] == "paragraph"),
        "table_chunks": sum(1 for chunk in chunks if chunk["kind"] == "table_rows"),
        "docs": len({chunk["doc_id"] for chunk in chunks}),
        "retriever": f"bge-small-zh + BM25 (RRF k={rag_corpus.RRF_K})",
        "embed_model": rag_corpus.EMBED_MODEL_NAME,
        "body_chars": [BODY_MIN_CHARS, BODY_MAX_CHARS],
        "table_group_rows": TABLE_GROUP_ROWS,
        "table_group_overlap_rows": TABLE_GROUP_OVERLAP_ROWS,
        "table_max_columns": TABLE_MAX_COLUMNS,
    }
