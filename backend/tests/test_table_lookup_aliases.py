"""行/列名解析回归：单位后缀、别名、年份、部分成功、错位自校验。

用例形状直接取自 2026-11-03 答案层 run 里真实失败的 tool 参数：
- FIN-08: get_rows(t0104, ["营业收入"]) / get_cell(..., column=2025)
- FIN-11: calculate(formula=..., inputs=<json string>)
- FIN-S01: get_rows(t0011, [10..14])
- FIN-S06: get_cell 取到行标签/百分比
- FIN-S10: get_rows(..., ["营业收入合计"])

全部离线，不依赖 data/finance 语料。
"""

import json
from pathlib import Path

import pytest

from app.finance.store import FinanceStore, FinanceStoreError
from app.tools.finance import FinanceToolbox


def write_document(root: Path, tables: list[dict], doc_id: str = "demo") -> Path:
    document = {
        "doc_id": doc_id,
        "title": "XX公司2025年年度报告",
        "company": "XX股份",
        "code": "000001",
        "doc_type": "annual_report",
        "period": "2025",
        "published_at": "2026-04-20",
        "source_url": "https://example.com/demo.pdf",
        "sections": [
            {
                "section_id": f"{doc_id}-s1",
                "path": ["第二节", "主要会计数据"],
                "page_start": 9,
                "page_end": 9,
                "text": "主要会计数据。",
            }
        ],
        "tables": tables,
    }
    doc_dir = root / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "document.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def main_table() -> dict:
    """形状同比亚迪/美的「主要会计数据和财务指标」表（t0006/t0007）。"""
    return {
        "table_id": "demo#t1",
        "section_id": "demo-s1",
        "page": 9,
        "caption": "主要会计数据和财务指标",
        "columns": ["项目", "2025 年", "2024 年", "本年比上年增减", "2023 年"],
        "rows": [
            ["营业收入（千元）", "456,451,731", "407,149,600", "12.11%", "372,037,280"],
            ["归属于上市公司股东的净利润（元）", "32,619,022,000.00", "40,254,346,000.00", "-18.97%", "30,040,811,000.00"],
            ["研发费用金额（千元）", "17,787,624", "16,232,771", "9.58%", "14,583,311"],
            ["毛利率", "23.40%", "22.10%", "1.30%", "21.90%"],
        ],
    }


def shifted_table() -> dict:
    """形状同 BYD t0104：编号前缀在第 1 列，行名落到第 2 列。"""
    return {
        "table_id": "demo#t2",
        "section_id": "demo-s1",
        "page": 11,
        "caption": "合并利润表",
        "columns": ["项目", "col2", "col3", "2025 年", "2024 年"],
        "rows": [
            ["一、", "营业收入", "45", "803,964,958", "777,102,455"],
            ["减：", "营业成本", "46", "600,000,000", "580,000,000"],
        ],
    }


def pathological_table() -> dict:
    """形状同 FIN-S06 的错位：金额行的目标列返回百分比。"""
    return {
        "table_id": "demo#t3",
        "section_id": "demo-s1",
        "page": 12,
        "caption": "错位表",
        "columns": ["项目", "2025 年", "2024 年"],
        "rows": [
            ["营业收入（千元）", "456,451,731", "100.00%"],
        ],
    }


@pytest.fixture()
def store(tmp_path):
    root = write_document(
        tmp_path,
        [main_table(), shifted_table(), pathological_table()],
    )
    return FinanceStore(root)


def test_row_and_column_normalization_matches_model_guesses(store):
    # 模型常写「营业收入」+ 2025（整数）；实际是「营业收入（千元）」+「2025 年」。
    cell = store.get_cell("demo#t1", "营业收入", 2025)
    assert cell["value"] == "456,451,731"
    assert cell["row_name"] == "营业收入（千元）"
    assert cell["column"] == "2025 年"

    # 2025 也可以按列名传成字符串。
    by_name = store.get_cell("demo#t1", "营业收入（千元）", "2025")
    assert by_name["value"] == "456,451,731"


def test_row_label_with_number_prefix_in_second_column(store):
    # t0104 形状：首列是「一、」，行名在第二列；旧实现只看首列会 not_found。
    result = store.get_rows("demo#t2", ["营业收入"], ["2025 年"])
    assert result["rows"][0]["row"] == 0
    assert result["rows"][0]["row_name"] == "营业收入"
    assert result["rows"][0]["values"] == ["803,964,958"]


def test_financial_aliases(store):
    assert store.get_cell("demo#t1", "归母净利润", "2025 年")["value"] == "32,619,022,000.00"
    assert store.get_rows("demo#t1", ["营业收入合计"], ["2025 年"])["rows"][0]["values"] == ["456,451,731"]
    # 「研发投入」在不同年份可能写成「研发费用金额」。
    assert store.get_cell("demo#t1", "研发投入", "2024 年")["value"] == "16,232,771"


def test_get_rows_partial_success_on_out_of_range(store):
    result = store.get_rows("demo#t1", [0, 1, 99])
    assert result["row_indices"] == [0, 1]
    assert [row["row_name"] for row in result["rows"]] == [
        "营业收入（千元）",
        "归属于上市公司股东的净利润（元）",
    ]
    assert result["skipped_rows"][0]["input"] == 99
    assert "行越界" in result["skipped_rows"][0]["reason"]


def test_get_rows_still_fails_when_all_rows_invalid(store):
    with pytest.raises(FinanceStoreError):
        store.get_rows("demo#t1", ["不存在的行"])
    with pytest.raises(FinanceStoreError):
        store.get_rows("demo#t1", ["营业收入（千元）", "营业收入（千元）"])


def test_numeric_text_is_not_matched_as_page_number_label(store):
    # t0104 的第 3 列是页码（45/46），不能把 row="45" 误判成这一行。
    with pytest.raises(FinanceStoreError):
        store.get_cell("demo#t2", "45", "2025 年")


def test_lookup_errors_include_candidates(store):
    with pytest.raises(FinanceStoreError) as row_error:
        store.get_cell("demo#t1", "不存在的行", "2025 年")
    assert "可用行名" in str(row_error.value)
    assert "营业收入（千元）" in str(row_error.value)

    with pytest.raises(FinanceStoreError) as column_error:
        store.get_cell("demo#t1", "营业收入", 99)
    assert "可用列名" in str(column_error.value) or "列越界" in str(column_error.value)


def test_misalignment_is_flagged_for_percentage_in_amount_row(store):
    cell = store.get_cell("demo#t3", "营业收入", "2024 年")
    assert cell["value"] == "100.00%"
    assert cell["misalignment"]["suspected"] is True
    assert "百分比" in cell["misalignment"]["reason"]


def test_ratio_columns_are_not_flagged(store):
    cell = store.get_cell("demo#t1", "毛利率", "2025 年")
    assert cell["value"] == "23.40%"
    assert "misalignment" not in cell


def test_label_column_is_not_flagged(store):
    rows = store.get_rows("demo#t2", ["营业收入"], ["项目", "col2"])
    assert "suspected_misalignment" not in rows


async def test_calculate_accepts_formula_alias_and_json_inputs(store):
    toolbox = FinanceToolbox(store)
    outcome = await toolbox.call(
        "calculate",
        {"formula": "a / b", "inputs": '{"a": "1", "b": "2"}'},
    )
    assert outcome.ok is True
    assert outcome.data["result"] == "0.5"

    bad = await toolbox.call(
        "calculate",
        {"formula": "a / b", "inputs": "not-json"},
    )
    assert bad.ok is False
    assert bad.error_kind == "bad_args"
    assert "inputs" in bad.error_detail
