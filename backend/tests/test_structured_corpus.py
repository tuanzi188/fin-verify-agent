"""结构切片与引用来源归属测试。全部离线，不加载向量模型。"""

import json

from app.finance import block_index
from app.finance.store import FinanceStore
from evaluation import rag_baseline, rag_corpus, structured_corpus
from evaluation.citation_anchor import anchor_report


def _document() -> dict:
    return {
        "doc_id": "demo",
        "title": "XX公司2025年年度报告",
        "company": "XX股份",
        "doc_type": "annual_report",
        "period": "2025",
        "unit": "万元",
        "sections": [
            {
                "section_id": "demo#s1",
                "path": ["第二节", "经营讨论"],
                "page_start": 10,
                "page_end": 11,
                "text": "甲" * 500 + "。" + "乙" * 300 + "。",
            }
        ],
        "tables": [
            {
                "table_id": "demo#t1",
                "section_id": "demo#s1",
                "page": 12,
                "caption": "主要会计数据",
                "unit": "万元",
                "columns": ["项目", "2025 年", "2024 年"],
                "rows": [
                    ["营业收入", "1,234", "1,000"],
                    ["营业成本", "800", "700"],
                    ["毛利率", "35.17%", "30.00%"],
                ],
            }
        ],
    }


def test_body_chunks_stay_inside_section_and_respect_size():
    body = ("甲" * 350 + "。") * 5
    chunks = block_index.pack_sentences(body)
    assert len(chunks) >= 2
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= block_index.BODY_MAX_CHARS + 120 for chunk in chunks)
    # 只在句子边界重叠，不把整章内容重复进每个块
    assert sum(len(chunk) for chunk in chunks) < len(body) * 1.5


def test_question_period_filter_keeps_cross_year_questions():
    assert block_index.periods_for_question("美的集团 2025 年营业收入") == {"2025"}
    assert block_index.periods_for_question(
        "比亚迪 2025 年比 2024 年增速差多少"
    ) == {"2024", "2025"}
    assert block_index.periods_for_question("美的集团营业收入") is None

    chunks = [
        {"doc_id": "cninfo-000333-2025-annual", "period": "2025"},
        {"doc_id": "cninfo-000333-2024-annual", "period": "2024"},
    ]
    index = block_index.BlockIndex.__new__(block_index.BlockIndex)
    index.blocks = chunks
    allowed = index._allowed_indices([], {"2025"}, ["000333"])
    assert allowed == {0}


def test_table_groups_cover_all_rows_and_columns():
    row_groups = block_index.row_groups(13)
    assert row_groups[0] == list(range(6))
    assert row_groups[-1][-1] == 12
    assert set().union(*map(set, row_groups)) == set(range(13))

    columns = block_index.column_groups(20)
    assert columns[0][0] == 0
    assert set().union(*map(set, columns)) == set(range(20))
    assert all(index in group for group in columns for index in [0])


def test_document_chunks_keep_source_metadata_and_coordinates():
    chunks = block_index.build_document_blocks(_document())
    prose = [chunk for chunk in chunks if chunk["kind"] == "paragraph"]
    tables = [chunk for chunk in chunks if chunk["kind"] == "table_rows"]

    assert len(prose) >= 2
    assert prose[0]["source_id"] == "demo#s1"
    assert prose[0]["source_spans"][0]["page"] == 10
    assert "第二节 > 经营讨论" in prose[0]["content"]

    assert tables
    assert tables[0]["table_id"] == "demo#t1"
    assert tables[0]["row_indices"] == [0, 1, 2]
    assert tables[0]["column_indices"] == [0, 1, 2]
    assert "PDF 第 12 页" in tables[0]["content"]
    assert "单位：万元" in tables[0]["content"]
    assert "[行0]" in tables[0]["content"]


def test_fixed_window_records_every_overlapping_source():
    spans = [
        (0, 100, "section", "doc#s1", 3),
        (100, 200, "table", "doc#t1", 4),
        (200, 300, "section", "doc#s2", 5),
    ]
    sources = rag_corpus._source_spans(spans, 50, 250)
    # 表格与窗口重叠 100 字，两个章节各 50 字；全部保留，按覆盖字符数排序。
    assert [item["source_id"] for item in sources] == ["doc#t1", "doc#s1", "doc#s2"]
    assert [item["chars"] for item in sources] == [100, 50, 50]


async def test_answer_once_accepts_structured_corpus_without_changing_generation_path():
    class FakeCorpus:
        @staticmethod
        def docs_for_question(question):
            return None

        @staticmethod
        def search(question, top_k=8, doc_codes=None):
            return [{
                "chunk_id": "demo#t1#rows#0#cols#1",
                "doc_id": "demo",
                "source_id": "demo#t1",
                "source_spans": [
                    {"source_id": "demo#t1", "kind": "table", "page": 12,
                     "start": 0, "end": 20, "chars": 20}
                ],
                "kind": "table_rows",
                "table_id": "demo#t1",
                "row_indices": [0],
                "column_indices": [1],
                "page": 12,
                "title": "XX公司2025年年度报告",
                "content": "营业收入 | 1,234",
                "score": 0.5,
            }]

    class FakeModel:
        async def chat(self, messages, **kwargs):
            return "营业收入为 1,234 万元 [1]。", None, {
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }

    result = await rag_baseline.answer_once(
        {"id": "CX-X", "question": "营业收入是多少？"},
        model=FakeModel(),
        corpus=FakeCorpus(),
        variant="rag_structured",
    )
    assert result["variant"] == "rag_structured"
    assert result["evidence"][0]["source_spans"][0]["source_id"] == "demo#t1"
    assert result["evidence"][0]["kind"] == "table_rows"
    assert result["citations"][0]["chunk_id"] == "demo#t1#rows#0#cols#1"


def test_structured_corpus_search_delegates_to_shared_block_index(monkeypatch):
    """结构 RAG 不能维护第二套检索：调用参数与返回顺序必须原样走 BlockIndex。"""
    calls: dict = {}

    class FakeIndex:
        def search(self, query, top_k=8, doc_codes=None):
            calls.update({"query": query, "top_k": top_k, "doc_codes": doc_codes})
            return [
                {"chunk_id": "doc#a", "content": "第一块", "score": 0.9},
                {"chunk_id": "doc#b", "content": "第二块", "score": 0.5},
            ]

    monkeypatch.setattr(structured_corpus, "_block_index", lambda: FakeIndex())
    hits = structured_corpus.search("营业收入", top_k=2, doc_codes=["000333"])

    assert [hit["chunk_id"] for hit in hits] == ["doc#a", "doc#b"]
    assert calls == {"query": "营业收入", "top_k": 2, "doc_codes": ["000333"]}


def test_block_index_search_filters_period_and_preserves_rank_order(tmp_path, monkeypatch):
    """检索验收要看有序结果：集合相同但顺序不同也会改变生成。"""
    for doc_id, period in (("cninfo-000333-2024-annual", "2024"),
                           ("cninfo-000333-2025-annual", "2025")):
        document = {
            "doc_id": doc_id,
            "company": "美的集团",
            "code": "000333",
            "period": period,
            "doc_type": "annual_report",
            "sections": [{
                "section_id": f"{doc_id}#s1",
                "path": ["第三节", "经营情况"],
                "page_start": 10,
                "text": f"{period} 年营业收入为 1,234 万元。营业收入同比增长 10%。",
            }],
            "tables": [],
        }
        path = tmp_path / doc_id / "document.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    index = block_index.BlockIndex(FinanceStore(tmp_path))
    monkeypatch.setattr(index, "_dense_rank", lambda *args, **kwargs: [])
    hits = index.search("美的集团 2025 年营业收入", top_k=5)

    assert hits
    assert all(hit["period"] == "2025" for hit in hits)
    assert all(hit["company"] == "美的集团" for hit in hits)
    scores = [hit["score"] for hit in hits]
    assert scores == sorted(scores, reverse=True)

    filtered = index.search("营业收入", top_k=5, doc_codes=["000333"])
    assert filtered
    assert all(hit["doc_code"] == "000333" for hit in filtered)

    # 过滤条件一条都不匹配时必须是空结果，不能退化成全库检索。
    assert index.search("比亚迪 2026 年第一季度营业收入", top_k=5) == []


def test_citation_anchor_uses_all_window_sources(tmp_path):
    document = _document()
    path = tmp_path / "demo" / "document.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    store = FinanceStore(tmp_path)

    task = {
        "answer_points": [
            {
                "text": "营业收入 1,234 万元",
                "evidence": {
                    "table_id": "demo#t1",
                    "row": "营业收入",
                    "column": "2025 年",
                    "value": "1,234",
                },
            }
        ]
    }
    # 窗口主锚点在章节，但窗口实际跨到了表格；只按主锚点会误判未命中。
    result = {
        "citations": [{"marker": 1, "chunk_id": "demo#s1#c0001"}],
        "evidence": [
            {
                "chunk_id": "demo#s1#c0001",
                "source_spans": [
                    {"source_id": "demo#s1", "kind": "section", "page": 10},
                    {"source_id": "demo#t1", "kind": "table", "page": 12},
                ],
            }
        ],
    }
    report = anchor_report(task, result, store)
    assert report["points"] == 1
    assert report["page_hits"] == 1
    assert report["table_hits"] == 1
    assert report["cell_hits"] == 0

    # 全部来源仍保留，但只擦到表格 2 个字符不能算命中该表。
    result["evidence"][0]["source_spans"][1]["chars"] = 2
    report = anchor_report(task, result, store)
    assert report["table_hits"] == 0

    # get_rows 的行组若覆盖目标行列，也应算精确定位命中。
    row_result = {
        "citations": [
            {"marker": 1, "chunk_id": "demo#t1#rows#0,1#cols#1,2"}
        ],
        "evidence": [
            {
                "chunk_id": "demo#t1#rows#0,1#cols#1,2",
                "kind": "rows",
                "table_id": "demo#t1",
                "row_indices": [0, 1],
                "column_indices": [1, 2],
            }
        ],
    }
    report = anchor_report(task, row_result, store)
    assert report["page_hits"] == 1
    assert report["table_hits"] == 1
    assert report["cell_hits"] == 1
