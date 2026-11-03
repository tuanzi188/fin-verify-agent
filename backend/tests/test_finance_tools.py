"""金融工具层测试：受限计算、章节/表格读取、工具错误归因。

全部离线：不碰 PDF、不调模型、不联网。
"""

import json
from pathlib import Path

import pytest

from app.finance.calc import CalculationError, calculate
from app.finance.store import FinanceStore, FinanceStoreError
from app.tools.finance import FINANCE_TOOL_SPECS, FinanceToolbox
from app.tools.finance_set import _render_rows


def write_demo_document(root: Path) -> Path:
    document = {
        "doc_id": "demo",
        "title": "XX公司2025年年度报告",
        "company": "XX股份",
        "code": "000001",
        "doc_type": "annual_report",
        "period": "2025",
        "published_at": "2026-04-20",
        "source_url": "https://example.com/demo.pdf",
        "sections": [
            {
                "section_id": "demo-s1",
                "path": ["第二节", "主要会计数据"],
                "page_start": 42,
                "page_end": 43,
                "text": "2025 年营业收入 12.34 亿元，2024 年为 10.00 亿元。",
            },
            {
                "section_id": "demo-s2",
                "path": ["第三节", "管理层讨论"],
                "page_start": 44,
                "page_end": 46,
                "text": "营业收入增长主要来自华东区域。",
            },
        ],
        "tables": [
            {
                "table_id": "demo-t1",
                "section_id": "demo-s1",
                "page": 42,
                "caption": "主要会计数据",
                "columns": ["项目", "2025", "2024"],
                "rows": [
                    ["营业收入", "12.34", "10.00"],
                    ["归母净利润", "1.20", "1.00"],
                ],
            }
        ],
    }
    doc_dir = root / "demo"
    doc_dir.mkdir(parents=True)
    (doc_dir / "document.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------------
# 受限计算
# ---------------------------------------------------------------------------


def test_calculate_revenue_growth():
    result = calculate(
        "(current - previous) / previous * 100",
        {"current": "12.34", "previous": "10.00"},
    )
    assert result["result"] == "23.4"
    assert result["formula"].startswith("(current")


def test_calculate_supports_percent_and_thousands_separator():
    result = calculate("rate * amount", {"rate": "23.4%", "amount": "1,000"})
    assert result["result"] == "234"


def test_calculate_supports_parentheses_negative():
    result = calculate("a + b", {"a": "(1,234.56)", "b": "0"})
    assert result["result"] == "-1234.56"

    full_width = calculate("a * 2", {"a": "（100）"})
    assert full_width["result"] == "-200"


def test_calculate_rejects_division_by_zero():
    with pytest.raises(CalculationError):
        calculate("a / b", {"a": "1", "b": "0"})


def test_calculate_rejects_code_execution():
    with pytest.raises(CalculationError):
        calculate("__import__('os').system('echo hi')")


def test_calculate_rejects_unknown_variable():
    with pytest.raises(CalculationError):
        calculate("a + b", {"a": "1"})


# ---------------------------------------------------------------------------
# 文档库
# ---------------------------------------------------------------------------


def test_store_reads_section_and_table(tmp_path):
    store = FinanceStore(write_demo_document(tmp_path))

    section = store.read_section("demo-s1")
    assert section["page_start"] == 42
    assert "12.34" in section["text"]

    table = store.get_table("demo-t1")
    assert table["columns"] == ["项目", "2025", "2024"]
    assert table["page"] == 42


def test_store_get_cell_by_name_and_index(tmp_path):
    store = FinanceStore(write_demo_document(tmp_path))

    by_name = store.get_cell("demo-t1", "营业收入", "2025")
    assert by_name["value"] == "12.34"
    assert by_name["cell_text"] == "12.34"
    assert by_name["table_caption"] == "主要会计数据"
    assert by_name["row_name"] == "营业收入"
    assert by_name["page"] == 42

    by_index = store.get_cell("demo-t1", 1, 2)
    assert by_index["value"] == "1.00"


def test_store_resolves_row_label_in_second_column(tmp_path):
    """首列为空、行名落在第 2 列的表也要能按行名取数（比亚迪 t0013 形状）。"""
    document = {
        "doc_id": "shifted",
        "title": "XX公司2025年年度报告",
        "company": "XX股份",
        "code": "000002",
        "doc_type": "annual_report",
        "period": "2025",
        "published_at": "2026-04-20",
        "source_url": "https://example.com/shifted.pdf",
        "sections": [
            {
                "section_id": "shifted-s1",
                "path": ["第三节", "营业收入构成"],
                "page_start": 50,
                "page_end": 50,
                "text": "营业收入构成。",
            }
        ],
        "tables": [
            {
                "table_id": "shifted-t1",
                "section_id": "shifted-s1",
                "page": 50,
                "caption": "（1） 营业收入构成",
                "columns": ["项目", "col2", "金额", "col4"],
                "rows": [
                    ["", "营业收入合计", "803,964,958,000.00", "100%"],
                    ["分行业", "", "", ""],
                    ["汽车", "", "648,645,636,000.00", "80.68%"],
                ],
            }
        ],
    }
    doc_dir = tmp_path / "shifted"
    doc_dir.mkdir(parents=True)
    (doc_dir / "document.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    store = FinanceStore(tmp_path)

    cell = store.get_cell("shifted-t1", "营业收入合计", "金额")
    assert cell["value"] == "803,964,958,000.00"
    assert cell["row_name"] == "营业收入合计"

    result = store.get_rows("shifted-t1", ["营业收入合计"], ["金额"])
    assert result["rows"][0]["row_name"] == "营业收入合计"
    assert result["rows"][0]["values"] == ["803,964,958,000.00"]


def test_render_rows_exposes_statement_scope():
    """工具输出要带口径/报表类型，模型才能在合并 vs 母公司之间做选择。"""
    text = _render_rows(
        {
            "table_id": "demo#t1",
            "caption": "现金流量表",
            "page": 66,
            "unit": "元",
            "unit_source": "table",
            "columns": ["项目", "2025 年"],
            "rows": [
                {
                    "row": 0,
                    "row_name": "经营活动产生的现金流量净额",
                    "values": ["经营活动产生的现金流量净额", "1"],
                }
            ],
            "statement_scope": "parent",
            "statement_type": "cash_flow",
        }
    )
    assert "口径：母公司" in text
    assert "报表：现金流量表" in text


def test_store_get_rows_keeps_coordinates_and_selected_columns(tmp_path):
    store = FinanceStore(write_demo_document(tmp_path))

    result = store.get_rows("demo-t1", ["营业收入", "归母净利润"], ["项目", "2025"])

    assert result["row_indices"] == [0, 1]
    assert result["column_indices"] == [0, 1]
    assert result["columns"] == ["项目", "2025"]
    assert [row["row_name"] for row in result["rows"]] == ["营业收入", "归母净利润"]
    assert result["rows"][0]["values"] == ["营业收入", "12.34"]
    assert result["rows"][0]["cells"][1] == {
        "column": "2025",
        "column_index": 1,
        "value": "12.34",
    }
    assert result["caption"] == "主要会计数据"
    assert result["page"] == 42


def test_store_get_rows_rejects_invalid_requests(tmp_path):
    store = FinanceStore(write_demo_document(tmp_path))
    with pytest.raises(FinanceStoreError):
        store.get_rows("demo-t1", [])
    with pytest.raises(FinanceStoreError):
        store.get_rows("demo-t1", ["营业收入", "营业收入"])
    with pytest.raises(FinanceStoreError):
        store.get_rows("demo-t1", ["不存在的行"])
    with pytest.raises(FinanceStoreError):
        store.get_rows("demo-t1", ["营业收入"], [])


def test_store_rejects_unknown_ids(tmp_path):
    store = FinanceStore(write_demo_document(tmp_path))
    with pytest.raises(FinanceStoreError):
        store.read_section("../../etc/passwd")
    with pytest.raises(FinanceStoreError):
        store.get_table("missing-table")


def test_store_search_sections(tmp_path):
    store = FinanceStore(write_demo_document(tmp_path))
    hits = store.search_sections("营业收入", top_k=5)
    assert hits
    assert hits[0]["section_id"] in ("demo-s1", "demo-s2")
    assert hits[0]["page_start"] >= 42


# ---------------------------------------------------------------------------
# 工具面
# ---------------------------------------------------------------------------


async def test_toolbox_call_success_and_error(tmp_path):
    toolbox = FinanceToolbox(FinanceStore(write_demo_document(tmp_path)))

    cell = await toolbox.call(
        "get_cell", {"table_id": "demo-t1", "row": "归母净利润", "column": "2024"}
    )
    assert cell.ok is True
    assert cell.data["value"] == "1.00"

    rows = await toolbox.call(
        "get_rows", {"table_id": "demo-t1", "rows": ["营业收入"], "columns": ["2025"]}
    )
    assert rows.ok is True
    assert rows.data["rows"][0]["cells"][0]["value"] == "12.34"

    calc = await toolbox.call(
        "calculate", {"expression": "a + b", "inputs": {"a": "1", "b": "2"}}
    )
    assert calc.ok is True
    assert calc.data["result"] == "3"

    bad = await toolbox.call("get_cell", {"table_id": "missing", "row": 0, "column": 0})
    assert bad.ok is False
    assert bad.error_kind == "not_found"

    unknown = await toolbox.call("drop_table", {})
    assert unknown.ok is False
    assert unknown.error_kind == "bad_args"


def test_finance_tool_specs_cover_required_tools():
    names = {spec["function"]["name"] for spec in FINANCE_TOOL_SPECS}
    assert {"list_documents", "search_sections", "read_section",
            "get_rows", "get_table", "get_cell", "calculate"} <= names
