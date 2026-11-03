"""线上金融语料的唯一入口。

与评测侧的 `evaluation.finance_corpus` 分工：

- 评测侧负责给 judge/lint 拼 `chunk_id -> 原文`；
- 线上侧只提供文档库、结构块索引和覆盖统计，不依赖 `evaluation`。

语料根目录默认是 `DATA_DIR/finance/documents`，可用 `FINANCE_DOCUMENT_DIR`
覆盖。语料缺失时抛 `FinanceCorpusError`，由 API 返回 503 和可执行的修复提示，
而不是假装“0 篇文档”。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from app.config import DATA_DIR
from app.finance.block_index import BlockIndex
from app.finance.store import FinanceStore
from app.services.finance_upload import upload_root


class FinanceCorpusError(RuntimeError):
    """金融语料不存在或无法加载。"""


def finance_document_dir() -> Path:
    configured = os.getenv("FINANCE_DOCUMENT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (DATA_DIR / "finance" / "documents").resolve()


@lru_cache(maxsize=1)
def get_finance_store() -> FinanceStore:
    root = finance_document_dir()
    if not root.exists():
        raise FinanceCorpusError(
            f"金融语料不存在: {root}；先运行 "
            "python -m scripts.ingest_finance_corpus 或设置 FINANCE_DOCUMENT_DIR"
        )
    try:
        # 官方语料 + 用户上传合成一个运行时命名空间；上传目录不存在时跳过。
        # 评测侧不走这里，仍然只读 official documents。
        extra = [upload_root()] if upload_root().exists() else []
        return FinanceStore(root, extra_roots=extra)
    except Exception as exc:  # 统一转成可读的语料错误
        raise FinanceCorpusError(f"金融语料加载失败: {exc}") from exc


@lru_cache(maxsize=1)
def get_block_index() -> BlockIndex:
    """结构块索引。只读；首次构建后进程内复用。"""
    return BlockIndex(get_finance_store())


def reset_cache() -> None:
    """测试或语料热更新时使用。"""
    index_fingerprint.cache_clear()
    get_block_index.cache_clear()
    get_finance_store.cache_clear()


@lru_cache(maxsize=1)
def index_fingerprint() -> str:
    """当前块索引指纹。写会话载荷时记下来，重开时可判断语料是否已变化。

    `BlockIndex.stats()` 会对全部块做一次哈希，这里缓存，避免每次提问重算。
    """
    return str(get_block_index().stats().get("fingerprint") or "")


def stats() -> dict:
    store = get_finance_store()
    documents = store.list_documents()
    index_stats = get_block_index().stats()
    periods = sorted(
        {str(item.get("period")) for item in documents if item.get("period")},
        reverse=True,
    )
    companies = sorted({str(item.get("company")) for item in documents if item.get("company")})
    return {
        "documents": len(documents),
        "companies": len(companies),
        "company_names": companies,
        "periods": periods,
        "sections": sum(int(item.get("sections") or 0) for item in documents),
        "tables": sum(int(item.get("tables") or 0) for item in documents),
        "blocks": index_stats["blocks"],
        "prose_blocks": index_stats["prose_blocks"],
        "table_blocks": index_stats["table_blocks"],
        "chunks": None,
        "index_fingerprint": index_stats["fingerprint"],
        "index_version": index_stats["index_version"],
        "corpus_ready": bool(documents),
    }


def document_inventory() -> list[dict]:
    store = get_finance_store()
    rows: list[dict] = []
    for item in store.list_documents():
        doc_id = str(item.get("doc_id") or "")
        span = store.page_span(doc_id) if doc_id else {}
        rows.append(
            {
                **item,
                "page_start": span.get("page_start"),
                "page_end": span.get("page_end"),
                "status": "parsed",
            }
        )
    return rows
