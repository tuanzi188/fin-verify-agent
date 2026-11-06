"""普通 RAG 基线臂的语料视图：512 字固定窗 + 20% 重叠，**不看表格结构**。

为什么基线要单独切一遍，而不是复用 `finance_corpus.chunk_map()`：

- `chunk_map()` 的锚点是章节 / 整张表格。整张表进一个 chunk，等于把"表格结构"
  送给了基线——那已经不是普通 RAG 了，是"结构感知 RAG"。
- 普通 RAG 的输入只有一段连续文本：解析器把标题、正文、表格拍平成文字，
  再按固定长度切窗。表格的行列关系在这一步就丢了，这正是基线该有的劣势。

每个 chunk 仍然记 `source_id`（它主要覆盖的章节或表格）。这个字段**只用于评测
归因**（判断"检索有没有命中真值表"），不会出现在喂给模型的正文里，也不参与检索打分。

切分参数固定为 512 字 / 20% 重叠，保证"普通 RAG"这四个字不是随口说的：
换参数要在报告里写明，否则基线读数不可比。切窗逻辑在本文件内联实现，
不依赖已下线的通用 RAG 分块器。
"""

from __future__ import annotations

import json
import math
from functools import lru_cache

from evaluation.finance_corpus import DOCUMENT_DIR

CHUNK_SIZE = 512
CHUNK_OVERLAP = 0.2
#: 与 FinanceStore._tokenize 同源的二元组切词（避免两套检索口径无法比较）
from app.finance.store import _tokenize  # noqa: E402


def _render_table(table: dict) -> str:
    """表格拍平成文字——注意：这一步之后行列关系就没了。"""
    head = f"表：{table.get('caption') or '未命名表格'}（单位：{table.get('unit') or '未标注'}）"
    lines = [head, " | ".join(str(name) for name in table.get("columns") or [])]
    for row in table.get("rows") or []:
        lines.append(" | ".join(str(cell) for cell in row))
    return "\n".join(lines)


def _document_stream(document: dict) -> tuple[str, list[tuple[int, int, str, str, int | None]]]:
    """把一份文档拍平成连续文本，并记下每个元素在文本里的区间。

    顺序：按 section 的存储顺序（解析器已按 (page, y0, x0) 排过）；每个 section
    先放标题路径与正文，再放挂在它下面的表格；没挂到任何 section 的表格按页码补在末尾。
    """
    tables = {str(t.get("table_id")): t for t in document.get("tables") or []}
    used: set[str] = set()
    parts: list[str] = []
    spans: list[tuple[int, int, str, str, int | None]] = []
    cursor = 0

    def _append(text: str, kind: str, source_id: str, page: int | None) -> None:
        nonlocal cursor
        if not text.strip():
            return
        block = text.strip() + "\n\n"
        parts.append(block)
        spans.append((cursor, cursor + len(block), kind, source_id, page))
        cursor += len(block)

    for section in document.get("sections") or []:
        section_id = str(section.get("section_id") or "")
        if not section_id:
            continue
        path_text = " > ".join(str(part) for part in section.get("path") or [])
        body = str(section.get("text") or "")
        _append(f"{path_text}\n{body}" if path_text else body, "section", section_id, section.get("page_start"))
        for table_id in section.get("table_ids") or []:
            table = tables.get(str(table_id))
            if not table or str(table_id) in used:
                continue
            used.add(str(table_id))
            caption = f"{path_text} > {table.get('caption') or '未命名表格'}" if path_text else str(table.get("caption") or "")
            _append(f"{caption}\n{_render_table(table)}", "table", str(table_id), table.get("page"))

    leftovers = [t for tid, t in tables.items() if tid not in used]
    leftovers.sort(key=lambda item: (int(item.get("page") or 0), str(item.get("table_id"))))
    for table in leftovers:
        _append(_render_table(table), "table", str(table.get("table_id")), table.get("page"))

    return "".join(parts), spans


def _source_spans(
    spans: list[tuple[int, int, str, str, int | None]],
    start: int,
    end: int,
) -> list[dict]:
    """窗口覆盖的全部原文来源，按重叠字符数降序。

    固定窗不保证只落在一个章节或一张表里。只记"重叠最多的那个 source_id"
    会把窗口里真实包含的其它来源抹掉，引用定位因此系统性偏低——这是量具错误。
    来源只用于归因与引用核验，不改检索正文，也不改 chunk_id 的兼容口径。
    """
    found = []
    for item in spans:
        left = max(start, item[0])
        right = min(end, item[1])
        if right <= left:
            continue
        found.append(
            {
                "source_id": item[3],
                "kind": item[2],
                "page": item[4],
                "start": left,
                "end": right,
                "chars": right - left,
            }
        )
    found.sort(key=lambda item: (-item["chars"], str(item["source_id"])))
    return found


def _overlap(spans: list[tuple[int, int, str, str, int | None]], start: int, end: int) -> tuple[str, str, int | None]:
    """取与窗口重叠最多的元素；同长度时**表格优先**。

    为什么表格优先：512 字窗口经常一半落在小节正文、一半落在挂在它下面的表上。
    如果按"哪个字多"归属，小表会被小节吞掉，评测时"检索有没有命中真值表"
    就会永远记成没命中——那是量具错误，不是基线能力差。这个偏好只影响归因，
    不影响检索本身（检索只看 content）。
    """
    sources = _source_spans(spans, start, end)
    if not sources:
        return "section", "", None
    # 表格优先只在最高权重相同时生效；source_spans 已按权重排序。
    top_weight = sources[0]["chars"]
    for item in sources:
        if item["chars"] != top_weight:
            break
        if item["kind"] == "table":
            return item["kind"], item["source_id"], item["page"]
    first = sources[0]
    return first["kind"], first["source_id"], first["page"]


@lru_cache(maxsize=1)
def rag_chunks() -> list[dict]:
    """全部文档切成的固定窗 chunk 列表（按 doc_id、位置排序）。"""
    step = max(CHUNK_SIZE - int(CHUNK_SIZE * CHUNK_OVERLAP), 1)
    chunks: list[dict] = []
    for path in sorted(DOCUMENT_DIR.glob("*/document.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        doc_id = str(document.get("doc_id") or path.parent.name)
        company = str(document.get("company") or "")
        period = str(document.get("period") or "")
        doc_type = str(document.get("doc_type") or "")
        stream, spans = _document_stream(document)
        index = 0
        for start in range(0, len(stream), step):
            text = stream[start:start + CHUNK_SIZE].strip()
            if not text:
                continue
            kind, source_id, page = _overlap(spans, start, start + CHUNK_SIZE)
            source_spans = _source_spans(spans, start, start + CHUNK_SIZE)
            chunks.append(
                {
                    "chunk_id": f"{source_id}#c{index:04d}",
                    "doc_id": doc_id,
                    "source_id": source_id,
                    "source_ids": [
                        item["source_id"] for item in source_spans if item["source_id"]
                    ],
                    "source_spans": source_spans,
                    "kind": kind,
                    "page": page,
                    "title": f"{company} {period} {doc_type}".strip(),
                    "content": text,
                }
            )
            index += 1
    return chunks


@lru_cache(maxsize=1)
def _index() -> tuple[list[dict], dict[str, list[int]], dict[str, float], list[int], float]:
    """倒排索引 + BM25 参数。"""
    chunks = rag_chunks()
    postings: dict[str, list[int]] = {}
    lengths: list[int] = []
    for index, chunk in enumerate(chunks):
        # 标题进索引：年报正文里几乎不写公司全称（写"公司"/"本集团"），
        # 不把 doc 标题带上，检索分不出"比亚迪的营收"和"茅台的营收"。
        # 这也是普通 RAG 的常规做法（chunk 前面拼 metadata 头），不是结构优势。
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


#: 向量路模型。用项目 `EMBEDDING_CONFIG` 的默认模型，本机已缓存，
#: 因此评测仍然是离线的（不下载、不联网）。
EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EMBED_CACHE_DIR = DOCUMENT_DIR.parent / "rag_index"
#: bge 系列的非对称检索指令前缀（query 侧）
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

BM25_K1 = 1.2
BM25_B = 0.75
STOPWORD_DF_RATIO = 0.5
RRF_K = 60
CANDIDATE_DEPTH = 60

#: 公司名 → 文档前缀。普通 RAG 在真实产品里也会按 metadata 过滤（公司/年份），
#: 不给这层过滤等于让基线去猜"茅台的营收"还是"比亚迪的营收"，是稻草人。
COMPANY_DOCS = {
    "美的": ("000333",),
    "比亚迪": ("002594",),
    "贵州茅台": ("600519",),
    "茅台": ("600519",),
    "天顺风能": ("002531",),
}


def docs_for_question(question: str) -> list[str] | None:
    """问题里点了哪些公司，就只在这些公司的文档里检索；没点名返回 None（全库）。"""
    text = str(question or "")
    codes: set[str] = set()
    for name, prefixes in COMPANY_DOCS.items():
        if name in text:
            codes.update(prefixes)
    if not codes:
        return None
    return sorted(codes)


def _query_tokens(tokens: set[str], postings: dict[str, list[int]], total: int) -> set[str]:
    if not tokens:
        return set()
    limit = max(int(total * STOPWORD_DF_RATIO), 1)
    kept = {token for token in tokens if token in postings and len(postings[token]) <= limit}
    if kept:
        return kept
    ranked = sorted((token for token in tokens if token in postings), key=lambda t: len(postings[t]))
    return set(ranked[:5])


def _allowed_indices(chunks: list[dict], doc_codes: list[str] | None) -> set[int] | None:
    if not doc_codes:
        return None
    allowed = {
        index
        for index, chunk in enumerate(chunks)
        if any(str(chunk.get("doc_id") or "").startswith(f"cninfo-{code}") for code in doc_codes)
    }
    return allowed or None


def _bm25_rank(query: str, allowed: set[int] | None, depth: int) -> list[int]:
    chunks, postings, idf, lengths, average = _index()
    tokens = _query_tokens(_tokenize(query), postings, len(chunks))
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
            norm = BM25_K1 * (1 - BM25_B + BM25_B * lengths[index] / average) + 1
            scores[index] = scores.get(index, 0.0) + weight * (BM25_K1 + 1) / norm
            matched[index] = matched.get(index, 0) + 1
    for index in scores:
        scores[index] *= (matched[index] / len(tokens)) ** 0.5
    return [index for index, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:depth]]


@lru_cache(maxsize=1)
def _model() -> "object":
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBED_MODEL_NAME, device="cpu")


@lru_cache(maxsize=1)
def _embeddings() -> "object":
    """全量 chunk 的 bge 向量。首次跑会编码 4000+ 段（CPU 数分钟），之后读缓存。"""
    import hashlib

    import numpy as np

    chunks = rag_chunks()
    digest = hashlib.sha256(EMBED_MODEL_NAME.encode("utf-8"))
    for chunk in chunks:
        digest.update(f"{chunk['chunk_id']}|{chunk['content']}".encode("utf-8"))
    fingerprint = digest.hexdigest()[:16]
    path = EMBED_CACHE_DIR / f"emb-{fingerprint}.npy"
    if path.exists():
        return np.load(path)

    model = _model()
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
    model = _model()
    # bge 系列是**非对称**检索模型：query 侧要加指令前缀，document 侧不加。
    # 不加这一句，同一份语料的相关度排序会明显变差（实测 CX-09 从第 1 掉出前 8）。
    query_vector = model.encode(
        [f"{BGE_QUERY_PREFIX}{query}"],
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
    """混合检索（BM25 + bge 向量，RRF 融合）+ 公司元数据过滤。

    这是「普通 RAG」的定义：512 字固定窗、无表格结构、一次检索；
    但检索器本身是正常配置（向量 + 关键词），不是故意做弱的稻草人。
    """
    chunks = rag_chunks()
    allowed = _allowed_indices(chunks, doc_codes)
    fused: dict[int, float] = {}
    for ranked in (
        _dense_rank(query, allowed, CANDIDATE_DEPTH),
        _bm25_rank(query, allowed, CANDIDATE_DEPTH),
    ):
        for rank, index in enumerate(ranked, 1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank)
    top = sorted(fused.items(), key=lambda item: item[1], reverse=True)[: max(1, top_k)]
    results = []
    for index, score in top:
        chunk = dict(chunks[index])
        chunk["score"] = round(score, 6)
        results.append(chunk)
    return results


def stats() -> dict:
    chunks = rag_chunks()
    return {
        "chunks": len(chunks),
        "chars": sum(len(chunk["content"]) for chunk in chunks),
        "chunk_size": CHUNK_SIZE,
        "overlap": CHUNK_OVERLAP,
        "docs": len({chunk["doc_id"] for chunk in chunks}),
        "retriever": f"bge-small-zh + BM25 (RRF k={RRF_K})",
        "embed_model": EMBED_MODEL_NAME,
    }
