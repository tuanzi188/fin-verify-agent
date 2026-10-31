"""结构感知的一次检索 RAG 语料：正文按段落、表格按行组切片。

这是第三条对照臂，不替换预注册的固定窗基线：

- 普通 RAG：整篇拍平后 512 字硬切，用来回答"只有普通文本上下文时能做到什么"；
- 结构感知 RAG：保留章节边界、表头、行列名、单位与页码，用来回答
  "同样的证据结构交给一次检索 RAG，能否缩小与 Agent 的差距"。

**检索实现不在这里重复一份**：块生成和 `search()` 都直接调用
`app.finance.block_index`，与 Agent 的 `search_blocks` 共用同一个
`BlockIndex`、同一份 bge + BM25 + RRF、同一套公司/期间过滤和来源多样化。
本模块只负责"一次检索、一次生成"的评测臂适配。

结构感知臂仍然只检索一次、生成一次：没有多轮、没有修复、没有计算器。
"""

from __future__ import annotations

from functools import lru_cache

from app.finance.block_index import (
    BODY_MAX_CHARS,
    BODY_MIN_CHARS,
    INDEX_VERSION,
    TABLE_GROUP_OVERLAP_ROWS,
    TABLE_GROUP_ROWS,
    TABLE_MAX_COLUMNS,
    BlockIndex,
)
from app.finance.block_index import (
    parameters as block_parameters,
)
from app.finance.store import FinanceStore
from evaluation import rag_corpus
from evaluation.finance_corpus import DOCUMENT_DIR

CORPUS_VERSION = "finance-structure-v1"


@lru_cache(maxsize=1)
def _block_index() -> BlockIndex:
    """评测侧的唯一结构索引；与 Agent 共用同一个类，不复制检索逻辑。"""
    return BlockIndex(FinanceStore(DOCUMENT_DIR))


@lru_cache(maxsize=1)
def structured_chunks() -> list[dict]:
    return [dict(block) for block in _block_index().blocks]


def search(
    query: str,
    top_k: int = 6,
    doc_codes: list[str] | None = None,
) -> list[dict]:
    """结构块上的混合检索；直接委托共享实现，顺序/过滤/内容都保持一致。"""
    return _block_index().search(query, top_k=top_k, doc_codes=doc_codes)


def docs_for_question(question: str) -> list[str] | None:
    return rag_corpus.docs_for_question(question)


def stats() -> dict:
    index_stats = _block_index().stats()
    return {
        "corpus_version": CORPUS_VERSION,
        "index_version": INDEX_VERSION,
        "chunks": index_stats["blocks"],
        "prose_chunks": index_stats["prose_blocks"],
        "table_chunks": index_stats["table_blocks"],
        "docs": index_stats["documents"],
        "fingerprint": index_stats["fingerprint"],
        "retriever": f"bge-small-zh + BM25 (RRF k={block_parameters()['rrf_k']})",
        "embed_model": index_stats["embed_model"],
        "body_chars": [BODY_MIN_CHARS, BODY_MAX_CHARS],
        "table_group_rows": TABLE_GROUP_ROWS,
        "table_group_overlap_rows": TABLE_GROUP_OVERLAP_ROWS,
        "table_max_columns": TABLE_MAX_COLUMNS,
    }


def index_snapshot() -> dict:
    """运行清单用的索引描述，带有序内容指纹。"""
    return {
        "kind": "structured_blocks",
        **stats(),
        "parameters": block_parameters(),
    }


def chunking_params() -> dict:
    return block_parameters()
