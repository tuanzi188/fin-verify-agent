"""金融文档解析测试：标题树、表头对齐、跨页碎片合并、PDF → document.json。

全部离线：PDF 在 tmp_path 里现场生成，不下载、不联网。
"""

from __future__ import annotations

import json

import pytest

fitz = pytest.importorskip("fitz")

from app.finance.ingest import (  # noqa: E402
    _align_columns,
    _merge_continuations,
    ingest_pdf,
    summarize,
    write_document,
)
from app.finance.store import FinanceStore  # noqa: E402
from app.finance.tree import _level_of, body_size, extract_lines  # noqa: E402

PAGE = fitz.paper_rect("a4")


def _text(page, x, y, value, size=10.5):
    page.insert_text((x, y), value, fontname="china-s", fontsize=size)


def _grid(page, x0, y0, cols, rows, widths=None, height=22):
    widths = widths or [140] + [(520 - 140) // max(len(cols) - 1, 1)] * (len(cols) - 1)
    x = x0
    for width in widths:
        page.draw_line((x, y0), (x, y0 + height * rows), color=0, width=0.6)
        x += width
    page.draw_line((x, y0), (x, y0 + height * rows), color=0, width=0.6)
    for index in range(rows + 1):
        page.draw_line((x0, y0 + height * index), (x0 + sum(widths), y0 + height * index),
                       color=0, width=0.6)
    x = x0
    for width in widths:
        page.draw_line((x, y0), (x, y0 + height * rows), color=0, width=0.6)
        x += width


def build_pdf(path):
    doc = fitz.open()
    cover = doc.new_page(width=PAGE.width, height=PAGE.height)
    _text(cover, 60, 80, "公司代码：000001", size=12)
    _text(cover, 60, 110, "XX股份有限公司 2025 年年度报告", size=18)

    page1 = doc.new_page(width=PAGE.width, height=PAGE.height)
    _text(page1, 60, 80, "第一节 释义", size=14)
    _text(page1, 60, 110, "一、公司信息", size=10.5)
    _text(page1, 60, 130, "公司中文名称：XX股份有限公司", size=10.5)
    _text(page1, 60, 150, "二、联系人和联系方式", size=10.5)
    _text(page1, 60, 170, "董事会秘书：张三", size=10.5)

    page2 = doc.new_page(width=PAGE.width, height=PAGE.height)
    _text(page2, 60, 80, "第二节 公司简介和主要财务指标", size=14)
    _text(page2, 60, 110, "（一）主要会计数据", size=10.5)
    _text(page2, 60, 132, "单位：万元", size=9)
    _grid(page2, 60, 145, ["项目", "2025 年", "2024 年"], 3, widths=[146, 146, 146])
    for row, values in enumerate(
        (["项目", "2025 年", "2024 年"], ["营业收入", "1,234", "1,000"], ["净利润", "120", "100"])
    ):
        for col, value in enumerate(values):
            _text(page2, 64 + col * 146, 145 + 15 + row * 22, value, size=9)
    _text(page2, 60, 260, "（二）主要财务指标", size=10.5)
    _text(page2, 60, 280, "基本每股收益为 1.20 元。", size=10.5)
    doc.save(path)
    doc.close()


def test_level_rules():
    """编号 + 长度 + 中文，三条规则挡掉正文里的伪标题。"""
    body = 10.4
    assert _level_of("第三节 管理层讨论与分析", 14.0, body)[0] == 1
    assert _level_of("一、公司信息", body, body)[0] == 2
    assert _level_of("（一）营业收入", body, body)[0] == 3
    assert _level_of("1、主营业务分析", body, body)[0] == 4
    # 中文年报里正文也用 `一、`，靠长度和句末标点挡
    assert _level_of("一、本公司董事会及董事、高级管理人员保证年度报告内容的真实性、准确性、完整性，不存在", body, body) is None
    assert _level_of("二、公司全体董事出席董事会会议。", body, body) is None
    # 数字行不能被当成「十二、11%」
    assert _level_of("12.11%", body, body) is None
    assert _level_of("4.95", body, body) is None
    # 脚注字号
    assert _level_of("一、公司信息", 6.0, body) is None


def test_align_columns_shifts_merged_header():
    """表头右移一列（纵向合并）时，列名要对齐到真正有数据的列。"""
    header = ["", "", "2025 年", "", "2024 年"]
    body = [
        ["营业收入", "1,234", "", "1,000", ""],
        ["净利润", "120", "", "100", ""],
    ]
    columns, rows = _align_columns(header, body)
    assert columns == ["项目", "2025 年", "2024 年"]
    assert rows[0] == ["营业收入", "1,234", "1,000"]
    assert rows[1] == ["净利润", "120", "100"]


def test_merge_continuations_only_joins_fragments():
    """只有"表头 + 少量行"的碎片接回下一页，整表分页不接。"""
    fragment = {
        "page": 1, "page_end": 1, "bbox": [60, 700, 520, 760],
        "columns": ["项目", "2025"], "rows": [["营业收入", "1"]] * 2,
    }
    tail = {
        "page": 2, "page_end": 2, "bbox": [60, 60, 520, 300],
        "columns": ["项目", "2025"], "rows": [["净利润", "2"]] * 3,
    }
    merged = _merge_continuations([fragment, tail], {1: 842.0, 2: 842.0})
    assert len(merged) == 1
    assert merged[0]["page_end"] == 2
    assert len(merged[0]["rows"]) == 5

    long_table = {
        "page": 1, "page_end": 1, "bbox": [60, 200, 520, 760],
        "columns": ["项目", "2025"], "rows": [["行", "1"]] * 20,
    }
    tail2 = dict(tail, page=2, page_end=2)
    assert len(_merge_continuations([long_table, tail2], {1: 842.0, 2: 842.0})) == 2

    # 栏宽不同 ⇒ 两张表，不合并
    other = dict(tail, bbox=[80, 60, 500, 300])
    assert len(_merge_continuations([fragment, other], {1: 842.0, 2: 842.0})) == 2


def test_ingest_pdf_end_to_end(tmp_path):
    pdf = tmp_path / "demo.pdf"
    build_pdf(pdf)

    parsed = ingest_pdf(pdf, {"doc_id": "demo-2025-annual", "title": "XX公司2025年年度报告",
                             "company": "XX股份", "code": "000001",
                             "doc_type": "annual_report", "period": "2025",
                             "unit": "万元"})
    titles = [section["path"][-1] for section in parsed["sections"] if section["level"] == 1]
    assert "第一节 释义" in titles
    assert "第二节 公司简介和主要财务指标" in titles

    assert parsed["tables"], "应识别出主要会计数据表"
    table = next(t for t in parsed["tables"] if "营业收入" in " ".join(r[0] for r in t["rows"]))
    assert table["columns"] == ["项目", "2025 年", "2024 年"]
    assert table["unit"] == "万元"
    assert table["page"] == 3
    assert table["rows"][0] == ["营业收入", "1,234", "1,000"]

    sections = {section["section_id"]: section for section in parsed["sections"]}
    owner = sections[table["section_id"]]
    assert table["table_id"] in owner["table_ids"]
    assert owner["path"][-1] == "（一）主要会计数据"

    qc = summarize(parsed)
    assert qc["pages"] == 3
    assert qc["tables"] == 1
    assert qc["char_coverage"] > 0.5

    # 落盘后能被 FinanceStore 读回来，且按列名取数正确
    document_dir = tmp_path / "documents"
    write_document(parsed, document_dir)
    store = FinanceStore(document_dir)
    cell = store.get_cell(table["table_id"], "营业收入", "2024 年")
    assert cell["cell_text"] == "1,000"
    assert cell["unit"] == "万元"
    assert cell["page"] == 3
    assert json.loads((document_dir / "demo-2025-annual" / "document.json")
                      .read_text(encoding="utf-8"))["doc_id"] == "demo-2025-annual"


def test_body_size_ignores_footnotes(tmp_path):
    pdf = tmp_path / "demo.pdf"
    build_pdf(pdf)
    doc = fitz.open(pdf)
    try:
        lines = extract_lines(doc)
    finally:
        doc.close()
    assert body_size(lines) == pytest.approx(10.5, abs=0.2)
