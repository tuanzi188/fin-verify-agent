"""P1 回归：指标槽位覆盖、表级检索、读失败预算、正文数字补单元格。

全部离线，不依赖 data/finance 语料，也不调模型。
"""

import json
from pathlib import Path

import pytest

from app.agent import runner
from app.finance.query_scope import scope_metrics
from app.finance.store import FinanceStore
from app.tools.finance import FINANCE_TOOL_SPECS, FinanceToolbox
from app.tools.finance_set import FinanceToolset


def write_doc(
    root: Path,
    doc_id: str,
    company: str,
    period: str,
    tables: list[dict],
) -> None:
    section_id = f"{doc_id}#s1"
    document = {
        "doc_id": doc_id,
        "title": f"{company}{period}年年度报告",
        "company": company,
        "code": "000001",
        "doc_type": "annual_report",
        "period": period,
        "published_at": f"{int(period) + 1}-04-20",
        "source_url": "https://example.com/demo.pdf",
        "sections": [
            {
                "section_id": section_id,
                "path": ["第三节", "研发投入"],
                "page_start": 53,
                "page_end": 53,
                "text": "研发投入情况。",
            }
        ],
        "tables": [
            dict(table, section_id=section_id)
            for table in tables
        ],
    }
    doc_dir = root / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "document.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture()
def store(tmp_path):
    write_doc(
        tmp_path,
        "midea-2024",
        "美的集团",
        "2024",
        [
            {
                "table_id": "midea-2024#t0024",
                "page": 53,
                "caption": "公司研发投入情况",
                "columns": ["项目", "2024 年"],
                "rows": [["研发投入金额（千元）", "16,232,771"]],
            }
        ],
    )
    write_doc(
        tmp_path,
        "midea-2025",
        "美的集团",
        "2025",
        [
            {
                "table_id": "midea-2025#t0022",
                "page": 53,
                "caption": "公司研发投入情况",
                "columns": ["项目", "2025 年", "2024 年"],
                "rows": [["研发费用金额（千元）", "17,787,624", "16,232,771"]],
            },
            {
                "table_id": "midea-2025#t0006",
                "page": 9,
                "caption": "",
                "columns": ["项目", "2025 年", "2024 年"],
                "rows": [["营业收入（千元）", "456,451,731", "407,149,600"]],
            },
            {
                "table_id": "midea-2025#t0008",
                "page": 11,
                "caption": "",
                "columns": ["项目", "第一季度", "第二季度", "第三季度", "第四季度"],
                "rows": [
                    ["营业收入", "127,838,538", "123,285,176", "111,933,037", "93,394,980"]
                ],
            },
        ],
    )
    return FinanceStore(tmp_path)


def test_scope_metrics_dedupes_and_keeps_specific_metric():
    assert scope_metrics("比亚迪 2025 年归母净利率（归母净利润 ÷ 营业收入）是多少？") == [
        "归母净利润",
        "营业收入",
    ]
    assert scope_metrics("美的集团 2024 年研发相关支出") == ["研发投入"]
    assert scope_metrics("美的集团 2025 年营业收入") == ["营业收入"]
    assert scope_metrics("2025 年净利润") == ["净利润"]


def test_scope_metrics_skips_components_of_ratio_questions():
    # 净资产收益率问的是比率行，不该生成「净资产」槽位。
    assert scope_metrics("美的集团 2024 年加权平均净资产收益率是多少？") == []
    # 研发投入占营业收入比例问的是比率行，不该分别生成两个基础科目槽位。
    assert scope_metrics("比亚迪 2025 年研发投入占营业收入比例是多少？") == []


def test_search_tables_finds_uncaptioned_quarterly_table(store):
    toolbox = FinanceToolbox(store)
    hits = toolbox.search_tables("分季度 营业收入")
    assert hits
    assert hits[0]["table_id"] == "midea-2025#t0008"
    assert "营业收入" in hits[0]["matched_rows"]
    assert any("季度" in column for column in hits[0]["columns"])


def test_search_tables_returns_nothing_for_unrelated_query(store):
    toolbox = FinanceToolbox(store)
    assert toolbox.search_tables("完全不存在的指标和人名") == []


def test_search_tables_is_a_locator_not_evidence(store):
    toolset = FinanceToolset(store, search_mode="sections")
    hits = toolset.toolbox.search_tables("研发 投入")
    evidence = toolset.to_evidence("search_tables", hits)
    assert evidence
    assert evidence[0]["kind"] == "table_locator"
    assert evidence[0]["citable"] is False
    assert evidence[0]["chunk_id"].endswith("#table_locator")


def test_tool_specs_include_search_tables():
    names = {spec["function"]["name"] for spec in FINANCE_TOOL_SPECS}
    assert "search_tables" in names


def test_coverage_gaps_flags_located_but_unread_metric_table(store):
    toolset = FinanceToolset(store, search_mode="sections")
    question = "美的集团 2024 年研发相关支出，在 2024 年报和 2025 年报里分别叫什么科目名？两边数字是否一致？"
    evidence = [
        {
            "doc_id": "midea-2024",
            "kind": "section",
            "chunk_id": "midea-2024#s1",
            "table_ids": ["midea-2024#t0024"],
        },
        {
            "doc_id": "midea-2025",
            "kind": "rows",
            "chunk_id": "midea-2025#t0022#rows#0#cols#0,1",
            "table_id": "midea-2025#t0022",
            "row_indices": [0],
            "row_name": "研发费用金额（千元）",
        },
    ]
    gaps = toolset.coverage_gaps(question, evidence)
    assert len(gaps) == 1
    assert "midea-2024#t0024" in gaps[0]
    assert "研发投入金额（千元）" in gaps[0]
    assert "已在章节中定位但未读取" in gaps[0]


def test_coverage_gaps_does_not_require_prior_year_for_comparatives(store):
    toolset = FinanceToolset(store, search_mode="sections")
    question = "美的集团 2025 年营业收入比上年增加多少千元？"
    evidence = [
        {
            "doc_id": "midea-2025",
            "kind": "rows",
            "chunk_id": "midea-2025#t0006#rows#0#cols#0,1,2",
            "table_id": "midea-2025#t0006",
            "row_indices": [0],
            "row_name": "营业收入（千元）",
        }
    ]
    gaps = toolset.coverage_gaps(question, evidence)
    assert gaps == []


def test_search_tables_uses_company_and_period_metadata(store):
    toolbox = FinanceToolbox(store)
    hits = toolbox.search_tables("美的集团 2025 年 营业收入")
    assert hits
    assert hits[0]["doc_id"] == "midea-2025"


def test_coverage_gaps_does_not_flag_ratio_questions_as_components(store):
    toolset = FinanceToolset(store, search_mode="sections")
    question = "美的集团 2025 年研发费用占营业收入的比例是多少？"
    evidence = [
        {
            "doc_id": "midea-2025",
            "kind": "rows",
            "chunk_id": "midea-2025#t0022#rows#0#cols#0,1,2",
            "table_id": "midea-2025#t0022",
            "row_indices": [0],
            "row_name": "研发费用金额（千元）",
        }
    ]
    assert toolset.coverage_gaps(question, evidence) == []


async def test_unknown_finance_tool_lists_available_tools(store):
    toolbox = FinanceToolbox(store)
    outcome = await toolbox.call("search_kb", {"query": "营业收入"})
    assert outcome.ok is False
    assert outcome.error_kind == "bad_args"
    assert "search_tables" in outcome.error_detail
    assert "get_rows" in outcome.error_detail


async def test_read_failure_grants_extra_search_budget(store):
    toolset = FinanceToolset(store, search_mode="sections")
    before = toolset._search_budget
    outcome = await toolset.call(
        "get_rows",
        {"table_id": "midea-2025#t0006", "rows": ["不存在的行"]},
    )
    assert outcome.ok is False
    assert outcome.error_kind == "not_found"
    assert toolset._search_budget == before + 1

    for _ in range(3):
        await toolset.call(
            "get_cell",
            {"table_id": "midea-2025#t0006", "row": "不存在的行", "column": "2025 年"},
        )
    assert toolset._search_budget == before + 2
    assert toolset._budget_grants == 2


def test_section_only_numeric_answer_triggers_cell_repair():
    evidence = [{"kind": "section", "content": "2025 年营业收入 456,451,731 千元。"}]
    assert runner.needs_cell_repair("营业收入 456,451,731 千元 [1]", evidence) is True
    assert runner.needs_cell_repair("本节说明了收入确认政策 [1]", evidence) is False
