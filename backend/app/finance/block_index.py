"""金融结构块索引：段落块/表格行组块 + 混合检索。

这是 Agent 与结构感知 RAG 共用的底层证据单元。把块生成放在 `app/finance`
而不是 `evaluation`，原因是线上 Agent 不能反向依赖评测脚本；评测侧只消费
同一份块结构。

参数是实验起点，不是已验证结论：
- 正文 400–800 字，按句子边界切，短句只做必要的邻接重叠；
- 表格连续 6 行一组、组间重叠 1 行；
- 宽表按首列 + 8 个数据列切列组。

检索仍是一次混合检索（bge-small-zh + BM25 + RRF），Agent 只是可以在这份
索引上多轮使用工具；结构 RAG 则一次检索后直接生成。
"""

from __future__ import annotations

import hashlib
import math
import re

from app.config import DATA_DIR, EMBEDDING_CONFIG
from app.finance.store import FinanceStore, _tokenize
from app.finance.query_scope import scope_companies, scope_quarter, scope_years, unknown_scope_company

BODY_MIN_CHARS = 400
BODY_MAX_CHARS = 800
BODY_OVERLAP_MAX_CHARS = 120
TABLE_GROUP_ROWS = 6
TABLE_GROUP_OVERLAP_ROWS = 1
TABLE_MAX_COLUMNS = 8
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
CANDIDATE_DEPTH = 60
STOPWORD_DF_RATIO = 0.5
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
INDEX_VERSION = "finance-blocks-v1"
EMBED_CACHE_DIR = DATA_DIR / "finance" / "block_index"

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_YEAR_PATTERN = re.compile(r"(20\d{2})\s*年")
_COMPANY_ALIASES = (
    ("美的", "美的集团"),
    ("比亚迪", "比亚迪"),
    ("贵州茅台", "贵州茅台"),
    ("茅台", "贵州茅台"),
    ("天顺风能", "天顺风能"),
    ("天顺", "天顺风能"),
)


def pack_sentences(text: str) -> list[str]:
    """把章节正文按句子边界装成可检索块，不跨章节。"""
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
        if len(sentence) > BODY_MAX_CHARS:
            pending = flush()
            if pending:
                chunks.append(pending)
            for start in range(0, len(sentence), BODY_MAX_CHARS):
                chunks.append(sentence[start:start + BODY_MAX_CHARS])
            continue

        if current and current_len + len(sentence) > BODY_MAX_CHARS:
            previous = list(current)
            pending = flush()
            if pending:
                chunks.append(pending)
            carry = previous[-1] if len(previous[-1]) <= BODY_OVERLAP_MAX_CHARS else ""
            current = [carry] if carry else []
            current_len = len(carry)

        current.append(sentence)
        current_len += len(sentence)

    pending = flush()
    if pending:
        chunks.append(pending)
    return chunks


def column_groups(column_count: int) -> list[list[int]]:
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


def row_groups(row_count: int) -> list[list[int]]:
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


def _table_content(document: dict, table: dict, row_indices: list[int], column_indices: list[int]) -> str:
    columns = [str(table.get("columns", [])[index]) for index in column_indices]
    unit = str(table.get("unit") or document.get("unit") or "未标注")
    lines = [
        (
            f"文档：{document.get('company') or ''} {document.get('period') or ''} "
            f"{document.get('doc_type') or ''}"
        ),
        (
            f"表：{table.get('caption') or '未命名表格'}｜表 {table.get('table_id')}｜"
            f"PDF 第 {table.get('page')} 页｜单位：{unit}"
        ),
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


def build_document_blocks(document: dict) -> list[dict]:
    """把一份 document.json 变成段落块和表格行组块。"""
    doc_id = str(document.get("doc_id") or "")
    company = str(document.get("company") or "")
    period = str(document.get("period") or "")
    doc_type = str(document.get("doc_type") or "")
    code = str(document.get("code") or "")
    title = f"{company} {period} {doc_type}".strip()
    blocks: list[dict] = []

    for section in document.get("sections") or []:
        section_id = str(section.get("section_id") or "")
        body = str(section.get("text") or "")
        if not section_id or not body.strip():
            continue
        path_text = " > ".join(str(part) for part in section.get("path") or [])
        for index, body_text in enumerate(pack_sentences(body)):
            content = f"{path_text}\n{body_text}" if path_text else body_text
            blocks.append(
                {
                    "block_id": f"{section_id}#b{index:03d}",
                    "chunk_id": f"{section_id}#b{index:03d}",
                    "doc_id": doc_id,
                    "doc_code": code,
                    "company": company,
                    "period": period,
                    "source_id": section_id,
                    "source_ids": [section_id],
                    "source_spans": [
                        {
                            "source_id": section_id,
                            "kind": "section",
                            "page": section.get("page_start"),
                            "start": 0,
                            "end": len(body_text),
                            "chars": len(body_text),
                        }
                    ],
                    "kind": "paragraph",
                    "page": section.get("page_start"),
                    "section_id": section_id,
                    "path": list(section.get("path") or []),
                    "title": title,
                    "content": content,
                }
            )

    for table in document.get("tables") or []:
        table_id = str(table.get("table_id") or "")
        rows = table.get("rows") or []
        columns = table.get("columns") or []
        if not table_id or not rows or not columns:
            continue
        for row_indices in row_groups(len(rows)):
            for column_indices in column_groups(len(columns)):
                block_id = (
                    f"{table_id}#rows#"
                    + ",".join(str(index) for index in row_indices)
                    + "#cols#"
                    + ",".join(str(index) for index in column_indices)
                )
                blocks.append(
                    {
                        "block_id": block_id,
                        "chunk_id": block_id,
                        "doc_id": doc_id,
                        "doc_code": code,
                        "company": company,
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
                        "unit": table.get("unit") or document.get("unit") or "",
                        "footnote": table.get("footnote") or "",
                        "row_indices": row_indices,
                        "column_indices": column_indices,
                        "page": table.get("page"),
                        "section_id": table.get("section_id"),
                        "title": title,
                        "content": _table_content(document, table, row_indices, column_indices),
                    }
                )
    return blocks


def build_blocks(documents: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for document in documents:
        blocks.extend(build_document_blocks(document))
    return blocks


def parameters() -> dict:
    """切片与检索参数快照。运行清单必须记录这些值，否则复现不了同一份索引。"""
    return {
        "index_version": INDEX_VERSION,
        "body_min_chars": BODY_MIN_CHARS,
        "body_max_chars": BODY_MAX_CHARS,
        "body_overlap_max_chars": BODY_OVERLAP_MAX_CHARS,
        "table_group_rows": TABLE_GROUP_ROWS,
        "table_group_overlap_rows": TABLE_GROUP_OVERLAP_ROWS,
        "table_max_columns": TABLE_MAX_COLUMNS,
        "bm25_k1": BM25_K1,
        "bm25_b": BM25_B,
        "rrf_k": RRF_K,
        "candidate_depth": CANDIDATE_DEPTH,
        "bge_query_prefix": BGE_QUERY_PREFIX,
    }


def fingerprint(blocks: list[dict]) -> str:
    """块级指纹：block_id + content。索引内容变了，指纹必须变。"""
    digest = hashlib.sha256()
    for block in blocks:
        digest.update(f"{block.get('block_id')}|{block.get('content')}".encode())
    return digest.hexdigest()


def periods_for_question(question: str) -> set[str] | None:
    years = scope_years(question)
    return years or None


def companies_for_question(question: str) -> list[str]:
    text = str(question or "")
    companies: list[str] = []
    for alias, full in _COMPANY_ALIASES:
        if alias in text and full not in companies:
            companies.append(full)
    return companies


def _query_tokens(tokens: set[str], postings: dict[str, list[int]], total: int) -> set[str]:
    if not tokens:
        return set()
    limit = max(int(total * STOPWORD_DF_RATIO), 1)
    kept = {token for token in tokens if token in postings and len(postings[token]) <= limit}
    if kept:
        return kept
    ranked = sorted((token for token in tokens if token in postings), key=lambda t: len(postings[t]))
    return set(ranked[:5])


class BlockIndex:
    """一份文档库的结构块索引，Agent 和评测共用同一份块。"""

    def __init__(self, store: FinanceStore):
        self.store = store
        self.blocks = build_blocks(store.documents())
        self._block_by_id = {block["block_id"]: block for block in self.blocks}
        self._postings: dict[str, list[int]] = {}
        self._lengths: list[int] = []
        for index, block in enumerate(self.blocks):
            tokens = _tokenize(f"{block['title']}\n{block['content']}")
            self._lengths.append(len(tokens))
            for token in tokens:
                self._postings.setdefault(token, []).append(index)
        total = len(self.blocks)
        self._idf = {
            token: math.log((total - len(items) + 0.5) / (len(items) + 0.5) + 1.0)
            for token, items in self._postings.items()
        }
        self._average_length = sum(self._lengths) / max(len(self._lengths), 1)
        self._vectors = None
        self._embedder = None
        self._model_name = str(EMBEDDING_CONFIG.get("model_name") or "BAAI/bge-small-zh-v1.5")

    def get_block(self, block_id: str) -> dict:
        block = self._block_by_id.get(str(block_id))
        if block is None:
            raise KeyError(f"未知 block_id: {block_id}")
        return dict(block)

    def _allowed_indices(
        self,
        companies: list[str],
        periods: set[str] | None,
        doc_codes: list[str] | None = None,
    ) -> set[int] | None:
        has_filter = bool(companies or periods or doc_codes)
        if not has_filter:
            return None
        allowed = {
            index
            for index, block in enumerate(self.blocks)
            if (not companies or block.get("company") in companies)
            and (not periods or bool(scope_years(str(block.get("period"))) & periods))
            and (
                not doc_codes
                or any(
                    str(block.get("doc_id") or "").startswith(f"cninfo-{code}")
                    or str(block.get("doc_code") or "") == str(code)
                    for code in doc_codes
                )
            )
        }
        # 有过滤条件但一条都不匹配时必须返回空集，不能退化成“全库检索”。
        # 2026 Q1 这类超纲问题若回退到 2024/2025 年报，等于用相邻期间冒充答案。
        return allowed

    def _bm25_rank(self, query: str, allowed: set[int] | None, depth: int) -> list[int]:
        tokens = _query_tokens(_tokenize(query), self._postings, len(self.blocks))
        if not tokens:
            return []
        scores: dict[int, float] = {}
        matched: dict[int, int] = {}
        for token in tokens:
            weight = self._idf.get(token)
            if weight is None:
                continue
            for index in self._postings[token]:
                if allowed is not None and index not in allowed:
                    continue
                length = self._lengths[index]
                norm = BM25_K1 * (1 - BM25_B + BM25_B * length / self._average_length) + 1
                scores[index] = scores.get(index, 0.0) + weight * (BM25_K1 + 1) / norm
                matched[index] = matched.get(index, 0) + 1
        for index in scores:
            scores[index] *= (matched[index] / len(tokens)) ** 0.5
        return [
            index
            for index, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:depth]
        ]

    def _load_vectors(self):
        if self._vectors is not None:
            return self._vectors
        import numpy as np

        from app.services.embedding import get_embedding

        self._embedder = get_embedding()
        digest = hashlib.sha256(
            f"{INDEX_VERSION}:{self._embedder.version_tag}".encode()
        )
        for block in self.blocks:
            digest.update(f"{block['block_id']}|{block['content']}".encode())
        path = EMBED_CACHE_DIR / f"emb-{digest.hexdigest()[:16]}.npy"
        if path.exists():
            self._vectors = np.load(path)
            return self._vectors

        vectors = self._embedder.embed(
            [f"{block['title']}\n{block['content']}" for block in self.blocks]
        )
        vectors = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.maximum(norms, 1e-10)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, vectors)
        self._vectors = vectors
        return vectors

    def _dense_rank(self, query: str, allowed: set[int] | None, depth: int) -> list[int]:
        import numpy as np

        vectors = self._load_vectors()
        # 使用 embedding 服务的同一模型；query 侧的 bge 指令与评测保持一致。
        query_vector = np.asarray(
            self._embedder.embed([f"{BGE_QUERY_PREFIX}{query}"])[0], dtype=np.float32
        )
        norm = np.linalg.norm(query_vector)
        if norm:
            query_vector = query_vector / norm
        similarities = np.asarray(vectors @ query_vector)
        if allowed is not None:
            mask = np.full(similarities.shape, -np.inf)
            mask[list(allowed)] = similarities[list(allowed)]
            similarities = mask
        order = np.argsort(-similarities)[:depth]
        return [int(index) for index in order if np.isfinite(similarities[index])]

    def search(
        self,
        query: str,
        top_k: int = 8,
        doc_codes: list[str] | None = None,
    ) -> list[dict]:
        inventory = self.store.list_documents()
        if unknown_scope_company(query, inventory):
            return []
        companies = scope_companies(query, inventory) or companies_for_question(query)
        periods = periods_for_question(query)
        allowed = self._allowed_indices(companies, periods, doc_codes)
        requested_quarter = scope_quarter(query)
        if requested_quarter:
            quarter_doc_ids = {
                str(document.get("doc_id")) for document in inventory
                if scope_quarter(" ".join(str(document.get(field) or "") for field in ("period", "doc_type", "title"))) == requested_quarter
            }
            # 只在语料里真有对应季报时收窄。年报的「主要会计数据」表本身就带
            # 分季度行（FIN-09 实测：问 Q4 营业收入、金标在年报 t0007 rank 1–2），
            # 语料只有年报时收窄会把 allowed 清空、整题打空。「没有季报」的
            # 边界说明是 API 层 prepare_finance_scope 的职责，检索层不越权。
            if quarter_doc_ids:
                allowed = {
                    index for index, block in enumerate(self.blocks)
                    if (allowed is None or index in allowed) and str(block.get("doc_id")) in quarter_doc_ids
                }
        if allowed is not None and not allowed:
            return []
        fused: dict[int, float] = {}
        for ranked in (
            self._dense_rank(query, allowed, CANDIDATE_DEPTH),
            self._bm25_rank(query, allowed, CANDIDATE_DEPTH),
        ):
            for rank, index in enumerate(ranked, 1):
                fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank)
        ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)
        # 先每个文档取一个最优块，再按来源去重补足。
        # 跨文档题如果被同一份年报的多个窗口占满 top-k，另一份文档永远进不来；
        # 这里的来源多样性只影响“返回哪几个块”，不改变检索分数。
        selected: list[tuple[int, float]] = []
        used_docs: set[str] = set()
        used_sources: set[str] = set()
        for index, score in ordered:
            block = self.blocks[index]
            doc_id = str(block.get("doc_id") or "")
            if doc_id and doc_id not in used_docs:
                used_docs.add(doc_id)
                used_sources.add(str(block.get("source_id") or ""))
                selected.append((index, score))
            if len(selected) >= max(1, top_k):
                break
        if len(selected) < max(1, top_k):
            for index, score in ordered:
                block = self.blocks[index]
                source_id = str(block.get("source_id") or "")
                if source_id in used_sources:
                    continue
                used_sources.add(source_id)
                selected.append((index, score))
                if len(selected) >= max(1, top_k):
                    break
        results = []
        for index, score in selected[: max(1, top_k)]:
            block = dict(self.blocks[index])
            block["score"] = round(score, 6)
            results.append(block)
        return results

    def stats(self) -> dict:
        return {
            "index_version": INDEX_VERSION,
            "blocks": len(self.blocks),
            "prose_blocks": sum(1 for block in self.blocks if block["kind"] == "paragraph"),
            "table_blocks": sum(1 for block in self.blocks if block["kind"] == "table_rows"),
            "documents": len({block["doc_id"] for block in self.blocks}),
            "fingerprint": fingerprint(self.blocks),
            "embed_model": self._model_name,
        }
