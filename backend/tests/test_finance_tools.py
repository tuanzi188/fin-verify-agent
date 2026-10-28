"""金融工具层测试：受限计算、章节/表格读取、工具错误归因。

全部离线：不碰 PDF、不调模型、不联网。
"""

import json
from pathlib import Path

import pytest

from app.finance.calc import CalculationError, calculate
from app.finance.store import FinanceStore, FinanceStoreError
from app.tools.finance import FINANCE_TOOL_SPECS, FinanceToolbox


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
            "get_table", "get_cell", "calculate"} <= names
