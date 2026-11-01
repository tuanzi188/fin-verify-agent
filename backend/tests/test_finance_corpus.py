"""金融语料视图测试：标题必须带公司/期间，否则 judge 与失败归类失去上下文。

2026-10-31 发现：解析产物把 company/period/doc_type 放在顶层，`chunk_map()`
只读 `metadata`，导致所有标题变成「   ｜无标题表格」。这个测试用临时语料锁住修复。
"""

import json

from evaluation import finance_corpus


def write_document(root):
    document = {
        "doc_id": "demo-2025-annual",
        "company": "美的集团",
        "period": "2025",
        "doc_type": "annual_report",
        "sections": [
            {
                "section_id": "demo-2025-annual#s0001",
                "path": ["第二节", "主要会计数据"],
                "page_start": 1,
                "text": "营业收入 100 千元",
                "table_ids": ["demo-2025-annual#t0001"],
            }
        ],
        "tables": [
            {
                "table_id": "demo-2025-annual#t0001",
                "caption": "主要会计数据",
                "page": 1,
                "unit": "千元",
                "columns": ["项目", "2025 年"],
                "rows": [["营业收入", "100"]],
            }
        ],
    }
    path = root / "demo-2025-annual" / "document.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_chunk_map_reads_top_level_company_and_period(tmp_path, monkeypatch):
    write_document(tmp_path)
    monkeypatch.setattr(finance_corpus, "DOCUMENT_DIR", tmp_path)
    finance_corpus.chunk_map.cache_clear()

    chunks = finance_corpus.chunk_map()
    title = chunks["demo-2025-annual#t0001"]["title"]
    assert "美的集团" in title
    assert "2025" in title
    assert "annual_report" in title

    finance_corpus.chunk_map.cache_clear()


def test_doc_inventory_reads_top_level_fields(tmp_path, monkeypatch):
    write_document(tmp_path)
    monkeypatch.setattr(finance_corpus, "DOCUMENT_DIR", tmp_path)

    inventory = finance_corpus.doc_inventory()
    assert inventory == [
        {
            "doc_id": "demo-2025-annual",
            "company": "美的集团",
            "period": "2025",
            "doc_type": "annual_report",
            "sections": 1,
            "tables": 1,
        }
    ]
