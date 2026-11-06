"""金融结构检索回归：CI 里替代已下线的通用 RAG 门禁。

为什么是合成语料：官方语料（`data/finance`）不入库，CI 里没有真年报。
这里用一份固定的小语料覆盖检索链路里最容易回归的几段——
表格行组切分、核心表加权、公司/期间过滤、RRF 融合排序。
它不衡量语义质量（embedding 用 hash 兜底，不发网络请求），
衡量的是"结构没被改坏"。

回归时要改的是 `BASELINE_RECALL_AT_3` 与 `BASELINE_MRR`：先跑一遍看新值，
确认变化符合预期（而不是掩盖退化），再连同原因写进提交说明。
"""

import json

from app.finance.block_index import BlockIndex
from app.finance.store import FinanceStore
from app.services.embedding import HashFallbackEmbedding

BASELINE_RECALL_AT_3 = 1.0
BASELINE_MRR = 1.0

_DOCS = {
    "a-2025-annual": {
        "doc_id": "a-2025-annual",
        "title": "美的集团2025年年度报告",
        "company": "美的集团",
        "code": "000333",
        "doc_type": "annual_report",
        "period": "2025",
        "unit": "千元",
        "sections": [
            {
                "section_id": "a-2025-annual#s0001",
                "path": ["第二节", "公司简介和主要财务指标", "主要会计数据"],
                "page_start": 10,
                "page_end": 12,
                "text": "报告期内公司经营稳健，主要会计数据如下表所示。" * 30,
                "table_ids": ["a-2025-annual#t0001"],
            }
        ],
        "tables": [
            {
                "table_id": "a-2025-annual#t0001",
                "section_id": "a-2025-annual#s0001",
                "page": 11,
                "page_end": 11,
                "caption": "主要会计数据",
                "unit": "千元",
                "columns": ["项目", "2025 年", "2024 年"],
                "rows": [
                    ["营业收入", "456451731", "409084000"],
                    ["归属于上市公司股东的净利润", "38500000", "33720000"],
                    ["经营活动产生的现金流量净额", "52000000", "48000000"],
                ],
            }
        ],
    },
    "b-2025-annual": {
        "doc_id": "b-2025-annual",
        "title": "格力电器2025年年度报告",
        "company": "格力电器",
        "code": "000651",
        "doc_type": "annual_report",
        "period": "2025",
        "unit": "千元",
        "sections": [
            {
                "section_id": "b-2025-annual#s0001",
                "path": ["第二节", "公司简介和主要财务指标", "主要会计数据"],
                "page_start": 8,
                "page_end": 10,
                "text": "报告期内公司聚焦空调主业，主要会计数据如下表所示。" * 30,
                "table_ids": ["b-2025-annual#t0001"],
            }
        ],
        "tables": [
            {
                "table_id": "b-2025-annual#t0001",
                "section_id": "b-2025-annual#s0001",
                "page": 9,
                "page_end": 9,
                "caption": "主要会计数据",
                "unit": "千元",
                "columns": ["项目", "2025 年", "2024 年"],
                "rows": [
                    ["营业收入", "189000000", "205000000"],
                    ["归属于上市公司股东的净利润", "29000000", "27000000"],
                    ["经营活动产生的现金流量净额", "31000000", "30000000"],
                ],
            }
        ],
    },
}

# (query, expected doc_id, 是否要求命中表格行组块)
_QUERIES = [
    ("美的集团 2025 年营业收入是多少", "a-2025-annual", True),
    ("格力电器 2025 年营业收入是多少", "b-2025-annual", True),
    ("美的集团 2025 年归母净利润是多少", "a-2025-annual", True),
    ("格力电器 2025 年经营活动产生的现金流量净额", "b-2025-annual", True),
]

_QUERIES_WITHOUT_COMPANY = [
    ("2025 年营业收入是多少", {"a-2025-annual", "b-2025-annual"}),
    ("2025 年归母净利润", {"a-2025-annual", "b-2025-annual"}),
]


def _write_corpus(root) -> None:
    for doc_id, document in _DOCS.items():
        path = root / doc_id / "document.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _build_index(tmp_path, monkeypatch) -> BlockIndex:
    _write_corpus(tmp_path / "documents")
    # hash 兜底 embedding：不发网络请求，结果与机器无关
    monkeypatch.setattr(
        "app.services.embedding.get_embedding",
        lambda: HashFallbackEmbedding(dim=384),
    )
    monkeypatch.setattr("app.finance.block_index.EMBED_CACHE_DIR", tmp_path / "emb")
    return BlockIndex(FinanceStore(tmp_path / "documents"))


def _first_hit_rank(hits: list[dict], doc_id: str, table_only: bool) -> int | None:
    for rank, hit in enumerate(hits, 1):
        if hit.get("doc_id") != doc_id:
            continue
        if table_only and hit.get("kind") != "table_rows":
            continue
        return rank
    return None


def test_block_retrieval_regression(tmp_path, monkeypatch):
    index = _build_index(tmp_path, monkeypatch)

    ranks = []
    for query, doc_id, table_only in _QUERIES:
        rank = _first_hit_rank(index.search(query, top_k=3), doc_id, table_only)
        assert rank is not None, f"{query!r} 前 3 条没有命中 {doc_id} 的表格行组块"
        ranks.append(rank)

    recall_at_3 = sum(1 for rank in ranks if rank <= 3) / len(ranks)
    mrr = sum(1.0 / rank for rank in ranks) / len(ranks)
    assert recall_at_3 >= BASELINE_RECALL_AT_3, f"recall@3 跌破基线：{recall_at_3}"
    assert mrr >= BASELINE_MRR, f"MRR 跌破基线：{mrr}"


def test_company_filter_excludes_other_company(tmp_path, monkeypatch):
    index = _build_index(tmp_path, monkeypatch)

    hits = index.search("美的集团 2025 年营业收入是多少", top_k=8)
    assert hits
    assert {hit["doc_id"] for hit in hits} == {"a-2025-annual"}


def test_no_company_question_spans_both_documents(tmp_path, monkeypatch):
    index = _build_index(tmp_path, monkeypatch)

    for query, expected_docs in _QUERIES_WITHOUT_COMPANY:
        hits = index.search(query, top_k=8)
        assert {hit["doc_id"] for hit in hits} >= expected_docs, f"{query!r} 漏掉了文档"


def test_table_row_group_carries_coordinates(tmp_path, monkeypatch):
    index = _build_index(tmp_path, monkeypatch)

    hits = index.search("美的集团 2025 年营业收入是多少", top_k=3)
    table_hits = [hit for hit in hits if hit.get("kind") == "table_rows"]
    assert table_hits, "应命中表格行组块"
    hit = table_hits[0]
    assert hit["table_id"] == "a-2025-annual#t0001"
    assert hit["row_indices"], "行组块必须带行索引"
    assert hit["column_indices"], "行组块必须带列索引"
    assert hit["page"] == 11
    assert hit["unit"] == "千元"


def test_unknown_company_returns_no_hits(tmp_path, monkeypatch):
    index = _build_index(tmp_path, monkeypatch)

    assert index.search("贵州茅台 2025 年营业收入是多少", top_k=3) == []
