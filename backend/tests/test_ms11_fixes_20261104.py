"""2026-11-04 11 题复测 badcase 的逐项修复回归（全部离线，零模型调用）。

对应 docs/evidence/finance_ms11_report_20261104.json 的 4 条 badcase：
- P1-1 get_rows 不同行名解析到同一行 → 部分成功（FIN-S06 / FIN-11）
- P1-2 检索预算 5→8，耗尽时回显已定位清单（FIN-11）
- P1-3 search_tables 纳入检索别名（FIN-11 / FIN-S06 / FIN-S10 的 tool_call_count 假失败）
- P2-1 needs_calc_repair 覆盖合计/换算（FIN-S10 心算对了但没调 calculate）
- P2-2 search_tables 结果里给汇总表加【推荐】标记（CX-08 选表偏好）
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.agent import runner
from app.agent.prompts import CALC_REPAIR_HINT, FINANCE_RULES
from app.finance.store import FinanceStore, FinanceStoreError
from app.tools.finance_set import MAX_SEARCH_CALLS, FinanceToolset
from evaluation.agent_checks import check_tool_constraints


# ---------------------------------------------------------------------------
# P1-1 / fixture：与 test_table_lookup_aliases.py 同款最小语料
# ---------------------------------------------------------------------------

def _write_document(root: Path) -> Path:
    document = {
        "doc_id": "demo",
        "title": "XX公司2025年年度报告",
        "company": "XX股份",
        "code": "000001",
        "doc_type": "annual_report",
        "period": "2025",
        "published_at": "2026-04-20",
        "source_url": "https://example.com/demo.pdf",
        "sections": [{
            "section_id": "demo-s1", "path": ["第二节", "主要会计数据"],
            "page_start": 9, "page_end": 9,
            "text": "主要会计数据。营业收入 456,451,731 千元。",
        }],
        "tables": [{
            "table_id": "demo#t1",
            "section_id": "demo-s1",
            "page": 9,
            "caption": "主要会计数据和财务指标",
            "columns": ["项目", "2025 年", "2024 年"],
            "rows": [
                ["营业收入（千元）", "456,451,731", "407,149,600"],
                ["归属于上市公司股东的净利润（元）", "32,619,022,000.00", "40,254,346,000.00"],
            ],
        }],
    }
    doc_dir = root / "demo"
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "document.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


@pytest.fixture
def store(tmp_path):
    return FinanceStore(_write_document(tmp_path))


def test_get_rows_distinct_names_resolving_to_same_row_is_partial_success(store):
    """「营业收入」与「营业总收入」经别名解析到同一行：跳过重复，不抛错。

    FIN-S06 实测连报 4 次「重复行: 营业总收入」、FIN-11 因此打满 20 步。
    """
    result = store.get_rows("demo#t1", ["营业收入", "营业总收入"])
    assert result["row_indices"] == [0]
    assert len(result["rows"]) == 1
    assert result["skipped_rows"]
    assert result["skipped_rows"][0]["input"] == "营业总收入"
    assert "重复行" in result["skipped_rows"][0]["reason"]


def test_get_rows_identical_row_name_twice_still_raises(store):
    """同一个行名原样写两遍仍是调用错误，保留既有契约，模型要收到明确信号。"""
    with pytest.raises(FinanceStoreError, match="重复行"):
        store.get_rows("demo#t1", ["营业收入（千元）", "营业收入（千元）"])


# ---------------------------------------------------------------------------
# P1-2 检索预算与已定位清单回显
# ---------------------------------------------------------------------------

def test_search_budget_is_raised_for_multi_document_questions():
    assert MAX_SEARCH_CALLS == 8


def test_budget_exhaustion_returns_located_ids(store):
    toolset = FinanceToolset(store, search_mode="sections")
    for _ in range(MAX_SEARCH_CALLS):
        asyncio.run(toolset.call("search_sections", {"query": "营业收入"}))
    blocked = asyncio.run(toolset.call("search_sections", {"query": "再搜一次"}))
    assert blocked.ok is False
    assert blocked.error_kind == "unavailable"
    assert "预算" in blocked.error_detail
    # 关键：不再只给一句空错，而是把已定位的 chunk 交回模型
    assert "已定位：" in blocked.error_detail
    assert "demo-s1" in blocked.error_detail


def test_prepare_clears_located_ids(store):
    toolset = FinanceToolset(store, search_mode="sections")
    asyncio.run(toolset.call("search_sections", {"query": "营业收入"}))
    assert toolset._located_ids
    toolset.prepare(None)
    assert toolset._located_ids == []
    assert toolset._search_budget == MAX_SEARCH_CALLS


# ---------------------------------------------------------------------------
# P1-3 search_tables 纳入检索别名
# ---------------------------------------------------------------------------

def _result_with_tools(tools):
    return {
        "trace": [
            {"tool": name, "result_ids": [], "result_count": 0}
            for name in tools
        ]
    }


def test_search_tables_counts_as_search_sections():
    """FIN-11 / FIN-S06 / FIN-S10 的 tool_call_count 假失败：4~6 次 search_tables 被记成 0~1。"""
    task = {"tool_constraints": {
        "must_include": ["search_sections"],
        "call_count": {"search_sections": [2, 8]},
    }}
    result = _result_with_tools([
        "list_documents", "search_tables", "search_tables", "search_tables",
        "get_rows", "calculate",
    ])
    checks = check_tool_constraints(task, result)
    assert checks["tool_must_include"]["ok"] is True
    assert checks["tool_call_count"]["ok"] is True
    assert checks["tool_call_count"]["detail_by_tool"]["search_sections"]["actual"] == 3


def test_read_tools_do_not_count_as_search():
    """别名只覆盖检索工具，get_table / get_cell 不能被算进检索次数。"""
    task = {"tool_constraints": {"call_count": {"search_sections": [2, 8]}}}
    result = _result_with_tools([
        "get_table", "get_table", "get_cell", "get_cell", "get_rows",
    ])
    checks = check_tool_constraints(task, result)
    assert checks["tool_call_count"]["ok"] is False
    assert checks["tool_call_count"]["detail_by_tool"]["search_sections"]["actual"] == 0


# ---------------------------------------------------------------------------
# P2-1 合计 / 换算类派生数值也要走 calculate
# ---------------------------------------------------------------------------

def test_needs_calc_repair_for_sum_and_unit_conversion():
    evidence = [{"content": "美的 456,451,731 千元；比亚迪 803,964,958,000.00 元"}]
    # FIN-S10：两数合计 + 千元换算元，答案算对了但一轮没调 calculate
    q = "美的集团与比亚迪 2025 年营业收入合计是多少元？两者单位不同，请注意换算。"
    assert runner.needs_calc_repair(
        "合计 1,260,416,689,000 元", evidence, [], q
    ) is True
    # 已有 calculate 记录就不再追问
    assert runner.needs_calc_repair(
        "合计 1,260,416,689,000 元", evidence, [{"formula": "a+b*1000"}], q
    ) is False


def test_needs_calc_repair_without_derived_signal_stays_quiet():
    evidence = [{"content": "营业收入（千元） 1,234"}]
    # 问题没点名合计/换算，照抄一个原文数字不该触发补算
    assert runner.needs_calc_repair(
        "营业收入 1,234 千元", evidence, [], "2025 年营业收入是多少？"
    ) is False


def test_calc_repair_hint_and_rules_cover_sum_and_conversion():
    assert "合计" in CALC_REPAIR_HINT
    assert "换算" in CALC_REPAIR_HINT
    assert "换算" in FINANCE_RULES
    assert "单位换算" in FINANCE_RULES


# ---------------------------------------------------------------------------
# P2-2 汇总表选表引导（不泄题）
# ---------------------------------------------------------------------------

def test_search_tables_locator_marks_summary_table_as_recommended(store):
    toolset = FinanceToolset(store, search_mode="sections")
    data = [{
        "table_id": "demo#t1",
        "caption": "主要会计数据和财务指标",
        "page": 9,
        "columns": ["项目", "2025 年"],
        "row_labels": ["营业收入（千元）"],
        "doc_id": "demo",
    }]
    items = toolset.to_evidence("search_tables", data)
    assert len(items) == 1
    assert "【推荐】" in items[0]["content"]
    assert "demo#t1" not in items[0]["content"].split("【推荐】")[-1]  # 不写死具体表号


def test_search_tables_locator_does_not_mark_narrative_table(store):
    toolset = FinanceToolset(store, search_mode="sections")
    data = [{
        "table_id": "demo#t2", "caption": "（1）营业收入构成", "page": 49,
        "columns": ["项目", "2025 年"], "row_labels": ["营业收入合计"], "doc_id": "demo",
    }]
    items = toolset.to_evidence("search_tables", data)
    assert "【推荐】" not in items[0]["content"]


def test_finance_rules_guides_table_selection():
    assert "主要会计数据" in FINANCE_RULES
    assert "汇总表" in FINANCE_RULES
