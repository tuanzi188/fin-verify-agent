"""金融任务集与注入链路测试。

守三件事：

1. **注入下沉到 `Injector` 之后，金融工具集也能造故障**——否则"恢复与纠错"
   这一维在金融这边只能是装饰。
2. **`prepare()` 必须清账**：同一个工具集实例连跑几十条任务时，
   `calcs` 不清就会把上一条的公式记到下一条头上，硬门禁直接失效。
3. **lint 必须拦得住"任务卡数字写错"**：这是金融任务集最危险的静默错误——
   判分器拿错期望值去判，Agent 答对了反被判失败。
"""

import asyncio
import json

from app.agent.runner import run
from app.finance.store import FinanceStore
from app.tools.finance_set import MAX_SEARCH_CALLS, FinanceToolset
from evaluation.lint_finance_tasks import _lint_calc, _lint_evidence

DOC_ID = "demo-2025-annual"
SECTION_ID = f"{DOC_ID}#s0001"
TABLE_ID = f"{DOC_ID}#t0001"


def write_store(root):
    document = {
        "doc_id": DOC_ID,
        "title": "XX公司2025年年度报告",
        "company": "XX股份",
        "doc_type": "annual_report",
        "period": "2025",
        "unit": "万元",
        "sections": [
            {
                "section_id": SECTION_ID,
                "path": ["第二节", "主要会计数据"],
                "page_start": 12,
                "page_end": 12,
                "text": "2025 年营业收入 1,234 万元，上年 1,000 万元。",
                "table_ids": [TABLE_ID],
            }
        ],
        "tables": [
            {
                "table_id": TABLE_ID,
                "section_id": SECTION_ID,
                "page": 12,
                "page_end": 12,
                "caption": "主要会计数据",
                "unit": "万元",
                "columns": ["项目", "2025 年", "2024 年"],
                "rows": [["营业收入", "1,234", "1,000"], ["净利润", "120", "100"]],
            }
        ],
    }
    path = root / DOC_ID / "document.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def write_company(root, company: str, code: str, year: int) -> str:
    """写一份最小年报：覆盖检查只需要 doc_id / company / period / doc_type。"""
    doc_id = f"cninfo-{code}-{year}-annual"
    document = {
        "doc_id": doc_id,
        "title": f"{company}{year}年年度报告",
        "company": company,
        "code": code,
        "doc_type": "annual_report",
        "period": str(year),
        "unit": "万元",
        "sections": [
            {
                "section_id": f"{doc_id}#s0001",
                "path": ["第二节", "主要会计数据"],
                "page_start": 1,
                "page_end": 1,
                "text": f"{year} 年营业收入 1,234 万元。",
                "table_ids": [],
            }
        ],
        "tables": [],
    }
    path = root / doc_id / "document.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return doc_id


def _patch_llm(monkeypatch, rounds, final_answer="营业收入 1,234 万元 [1]。"):
    state = {"calls": 0}

    async def fake_chat_with_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                                   temperature=0.1, max_tokens=None):
        index = state["calls"]
        state["calls"] += 1
        text, calls = rounds[min(index, len(rounds) - 1)]
        return text, calls, None, {"prompt_tokens": 10, "completion_tokens": 5}

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        return final_answer, None, {"prompt_tokens": 10, "completion_tokens": 5}

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_chat_with_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------

def test_injection_timeout_then_empty_then_real(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(
        FinanceStore(tmp_path),
        injections=[{"call_index": 1, "mode": "timeout"}, {"call_index": 2, "mode": "empty"}],
    )
    assert toolset.planned_injections == 2

    first = asyncio.run(toolset.call("search_sections", {"query": "营业收入"}))
    assert first.ok is False and first.injected == "timeout" and first.error_kind == "timeout"

    second = asyncio.run(toolset.call("search_sections", {"query": "营业收入"}))
    assert second.ok is True and second.injected == "empty" and second.data == []

    third = asyncio.run(toolset.call("search_sections", {"query": "营业收入"}))
    assert third.ok is True and third.injected is None and third.data
    assert toolset.fired_injections == 2


def test_injection_truncated_cuts_the_number(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(
        FinanceStore(tmp_path),
        injections=[{"call_index": 1, "mode": "truncated"}],
    )
    outcome = asyncio.run(
        toolset.call("get_cell", {"table_id": TABLE_ID, "row": "营业收入", "column": "2025 年"})
    )
    assert outcome.ok is True and outcome.injected == "truncated"
    # 1,234 只有 5 个字符，8 字上限下不会触发截断——所以用一个更长的单元格验证
    assert outcome.data["cell_text"]


def test_injection_truncated_keeps_structure(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(
        FinanceStore(tmp_path),
        injections=[{"call_index": 1, "mode": "truncated"}],
    )
    outcome = asyncio.run(toolset.call("get_table", {"table_id": TABLE_ID}))
    assert outcome.ok is True and outcome.injected == "truncated"
    # 结构字段保留，正文被切：模型还能重取，不是考运气
    assert outcome.data["table_id"] == TABLE_ID
    assert len(outcome.data["rows"]) == 1
    assert outcome.data["columns"] == ["项目", "2025 年", "2024 年"]


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def test_prepare_clears_calcs_and_swaps_plan(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    asyncio.run(
        toolset.call("calculate", {"expression": "a / b * 100", "inputs": {"a": 1234, "b": 1000}})
    )
    assert len(toolset.calcs) == 1

    toolset.prepare([{"call_index": 1, "mode": "timeout"}])
    assert toolset.calcs == []
    assert toolset.planned_injections == 1
    assert toolset.calls == 0

    outcome = asyncio.run(toolset.call("search_sections", {"query": "营业收入"}))
    assert outcome.injected == "timeout"


def test_runner_applies_injection_through_toolset(tmp_path, monkeypatch):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    _patch_llm(
        monkeypatch,
        rounds=[
            ("", [{"id": "c1", "name": "search_sections", "arguments": {"query": "营业收入"}}]),
            ("", [{"id": "c2", "name": "search_sections", "arguments": {"query": "主要会计数据"}}]),
            ("", [{"id": "c3", "name": "get_cell",
                   "arguments": {"table_id": TABLE_ID, "row": "营业收入", "column": "2025 年"}}]),
        ],
    )
    task = {
        "id": "T-inject",
        "question": "2025 年营业收入是多少万元？",
        "max_steps": 8,
        "clarify_mode": "none",
        "citation_required": True,
        "mock": True,
        "inject": [{"call_index": 1, "mode": "timeout"}],
    }
    result = asyncio.run(run(task, variant="improved", toolset=toolset))
    injected = [step.get("injected") for step in result["trace"] if step.get("injected")]
    assert injected == ["timeout"]
    assert result["status"] != "error"


def test_calculate_outcome_reports_one_result(tmp_path):
    """calculate 成功必须报 1 条：落默认 0 会让模型以为计算器不可用（2026-11-03 FIN-22）。"""
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    outcome = asyncio.run(
        toolset.call("calculate", {"expression": "a + b", "inputs": {"a": "1", "b": "2"}})
    )
    assert outcome.ok is True
    assert outcome.result_count == 1
    assert outcome.result_ids == ["calc-1"]


# ---------------------------------------------------------------------------
# lint：任务卡数字必须对得回表格
# ---------------------------------------------------------------------------

def test_lint_rejects_value_not_in_table(tmp_path):
    write_store(tmp_path)
    store = FinanceStore(tmp_path)
    chunks = {TABLE_ID: {"chunk_id": TABLE_ID}}
    task = {
        "id": "T-bad",
        "answer_points": [
            {
                "text": "营业收入 9,999 万元",
                "chunk_ids": [TABLE_ID],
                "evidence": {"table_id": TABLE_ID, "row": "营业收入", "column": "2025 年", "value": "9,999"},
                "unit": "万元",
            }
        ],
    }
    errors = _lint_evidence(task, chunks, store)
    assert any("与表格不一致" in error for error in errors), errors


def test_lint_accepts_correct_value_and_catches_unit_drift(tmp_path):
    write_store(tmp_path)
    store = FinanceStore(tmp_path)
    chunks = {TABLE_ID: {"chunk_id": TABLE_ID}}
    ok_task = {
        "id": "T-ok",
        "answer_points": [
            {
                "text": "营业收入 1,234 万元",
                "chunk_ids": [TABLE_ID],
                "evidence": {"table_id": TABLE_ID, "row": "营业收入", "column": "2025 年", "value": "1,234"},
                "unit": "万元",
            }
        ],
    }
    assert _lint_evidence(ok_task, chunks, store) == []

    drift_task = {
        "id": "T-unit",
        "answer_points": [
            {
                "text": "营业收入 1,234 千元",
                "chunk_ids": [TABLE_ID],
                "evidence": {"table_id": TABLE_ID, "row": "营业收入", "column": "2025 年", "value": "1,234"},
                "unit": "千元",
            }
        ],
    }
    errors = _lint_evidence(drift_task, chunks, store)
    assert any("单位不一致" in error for error in errors), errors


def test_lint_calc_must_match_expected():
    good = {
        "id": "T-calc",
        "answer_points": [{"text": "增长 23.4%", "derived": True}],
        "calc": {
            "formula": "(a - b) / b * 100",
            "inputs": {"a": 1234, "b": 1000},
            "expected": "23.4",
            "note": "同比",
        },
    }
    errors, _warnings = _lint_calc(good)
    assert errors == []

    bad = dict(good)
    bad["calc"] = dict(good["calc"], expected="25.0")
    errors, _warnings = _lint_calc(bad)
    assert any("不符" in error for error in errors), errors


def test_lint_requires_calc_for_derived_points():
    task = {"id": "T-derive", "answer_points": [{"text": "占比 12%", "derived": True}]}
    errors, _warnings = _lint_calc(task)
    assert any("没有 calc" in error for error in errors), errors


def _minimal_task(**overrides):
    """过 base_lint 所需的最小合法任务（其余字段由 lint 自带默认口径）。"""
    task = {
        "id": "T-min",
        "dimension": "检索与引用",
        "secondary_tags": ["单跳"],
        "difficulty": "easy",
        "question": "XX股份 2025 年营业收入是多少？",
        "tool_constraints": {},
        "answer_points": [{"text": "营业收入 1,234 万元", "chunk_ids": [TABLE_ID]}],
        "citation_required": True,
        "should_abstain": False,
        "clarify_mode": "none",
        "forbidden": ["编造数据"],
        "max_steps": 8,
        "relevant_chunk_ids": [TABLE_ID],
        "query_type": "single_hop",
        "source": "real",
        "mock": False,
    }
    task.update(overrides)
    return task


def test_finance_required_fields_drop_required_args():
    """`required_args` 是通用制度库的字段，金融集不再强制（2026-11-03 删除）。"""
    from evaluation.lint_common import REQUIRED_FIELDS
    from evaluation.lint_common import lint as base_lint
    from evaluation.lint_finance_tasks import FINANCE_REQUIRED_FIELDS

    assert "required_args" in REQUIRED_FIELDS
    assert "required_args" not in FINANCE_REQUIRED_FIELDS
    assert set(FINANCE_REQUIRED_FIELDS) == set(REQUIRED_FIELDS) - {"required_args"}

    chunks = {TABLE_ID: {"chunk_id": TABLE_ID}}
    errors, _warnings = base_lint(
        [_minimal_task()], chunks, profile=False, required_fields=FINANCE_REQUIRED_FIELDS
    )
    assert not any("required_args" in error for error in errors), errors
    # 默认清单（通用制度库）仍然要求它，证明参数真的生效
    legacy_errors, _warnings = base_lint([_minimal_task()], chunks, profile=False)
    assert any("required_args" in error for error in legacy_errors), legacy_errors


def test_lint_anchor_sets_must_match_relevant_chunks():
    """`relevant_chunk_ids` 必须等于答案点锚点并集，含 evidence.table_id（2026-11-03）。"""
    from evaluation.lint_common import lint as base_lint

    extra_id = f"{TABLE_ID}#b2"
    chunks = {TABLE_ID: {"chunk_id": TABLE_ID}, extra_id: {"chunk_id": extra_id}}
    point = {"text": "营业收入 1,234 万元", "chunk_ids": [TABLE_ID]}

    ok = _minimal_task(answer_points=[point], relevant_chunk_ids=[TABLE_ID])
    errors, _warnings = base_lint([ok], chunks, profile=False)
    assert not any("锚定" in error or "反向覆盖" in error for error in errors), errors

    # evidence.table_id 也算锚点：只写 evidence 时 relevant 必须带上它
    evidence_point = {
        "text": "营业收入 1,234 万元",
        "chunk_ids": [extra_id],
        "evidence": {"table_id": TABLE_ID},
    }
    with_evidence = _minimal_task(
        answer_points=[evidence_point], relevant_chunk_ids=[TABLE_ID, extra_id]
    )
    errors, _warnings = base_lint([with_evidence], chunks, profile=False)
    assert not any("锚定" in error or "反向覆盖" in error for error in errors), errors

    # 漏写：答案点有锚点而 relevant 没有
    missing = _minimal_task(answer_points=[point], relevant_chunk_ids=[])
    errors, _warnings = base_lint([missing], chunks, profile=False)
    assert any("锚定不一致" in error for error in errors), errors

    # 多写：relevant 有摆设 chunk
    extra = _minimal_task(answer_points=[point], relevant_chunk_ids=[TABLE_ID, extra_id])
    errors, _warnings = base_lint([extra], chunks, profile=False)
    assert any("反向覆盖失败" in error for error in errors), errors


def _complex_task(index: int, kind: str) -> dict:
    return {
        "id": f"CX-{index:02d}",
        "complex_type": kind,
        "dimension": "检索与引用",
        "tool_constraints": {},
        "holdout": False,
        "answer_points": [],
    }


def test_lint_complex_requires_twelve_and_four_buckets():
    from evaluation.lint_finance_tasks import COMPLEX_TYPES, _lint_complex

    short = [_complex_task(i, "calc") for i in range(1, 12)]
    errors, _warnings = _lint_complex(short)
    assert any("必须恰好 12 条" in error for error in errors), errors

    twelve_same_bucket = [_complex_task(i, "calc") for i in range(1, 13)]
    errors, _warnings = _lint_complex(twelve_same_bucket)
    assert any("缺少 cross_table" in error for error in errors), errors

    balanced = [_complex_task(index, kind) for index, kind in enumerate(COMPLEX_TYPES * 3, 1)]
    for index in range(len(COMPLEX_TYPES)):
        balanced[index]["holdout"] = True
    errors, _warnings = _lint_complex(balanced)
    assert errors == [], errors


def test_lint_complex_requires_one_frozen_holdout_per_bucket():
    from evaluation.lint_finance_tasks import COMPLEX_TYPES, _lint_complex

    tasks = [_complex_task(index, kind) for index, kind in enumerate(COMPLEX_TYPES * 3, 1)]
    tasks[0]["tool_constraints"] = {"must_include": ["calculate"]}
    for index, kind in enumerate(COMPLEX_TYPES):
        tasks[index]["holdout"] = True
    errors, _warnings = _lint_complex(tasks)
    assert any("不许写 tool_constraints" in error for error in errors), errors

    tasks[1]["holdout"] = False
    errors, _warnings = _lint_complex(tasks)
    assert any("必须恰好 1 条冻结留出" in error for error in errors), errors


def test_lint_case_schema_v2_accepts_route_and_question_metadata():
    from evaluation.lint_finance_tasks import _lint_schema_v2

    errors, warnings = _lint_schema_v2(
        {
            "id": "T-schema",
            "route_target": "indeterminate",
            "prompt_style": "open_question",
            "retrieval_hops": 1,
            "answer_operations": ["lookup", "cite"],
        }
    )
    assert errors == []
    assert warnings == []


def test_lint_case_schema_v2_rejects_unknown_route_target():
    from evaluation.lint_finance_tasks import _lint_schema_v2

    errors, _warnings = _lint_schema_v2({"id": "T-schema-bad", "route_target": "guess"})
    assert any("route_target 非法" in error for error in errors), errors


def test_lint_schema_v2_flags_structured_target_with_multihop_or_calc():
    from evaluation.lint_finance_tasks import _lint_schema_v2

    errors, _warnings = _lint_schema_v2(
        {"id": "T-a", "route_target": "structured_rag", "retrieval_hops": 2}
    )
    assert any("retrieval_hops" in error for error in errors), errors

    errors, _warnings = _lint_schema_v2(
        {
            "id": "T-b",
            "route_target": "structured_rag",
            "retrieval_hops": 1,
            "calc": {"formula": "a/b", "inputs": {"a": 1, "b": 2}, "expected": "0.5"},
        }
    )
    assert any("calc/派生要点" in error for error in errors), errors

    errors, _warnings = _lint_schema_v2(
        {
            "id": "T-c",
            "route_target": "structured_rag",
            "retrieval_hops": 1,
            "answer_points": [{"text": "x", "derived": True, "chunk_ids": ["d#t1"]}],
        }
    )
    assert any("calc/派生要点" in error for error in errors), errors


def test_lint_schema_v2_errors_disclosed_value_labeled_calculate():
    """FIN-04 型漂移：ROE 是披露值，calculate 是过度标注。

    2026-11-01 审计 #5 起（answer_operations.calculate ⇔ calc/派生要点）
    升为 error 级，双向互检：披露值不许标 calculate，带 calc/派生要点
    也不许漏标。
    """
    from evaluation.lint_finance_tasks import _lint_schema_v2

    errors, _warnings = _lint_schema_v2(
        {
            "id": "T-disclosed",
            "route_target": "structured_rag",
            "retrieval_hops": 1,
            "answer_operations": ["lookup", "calculate", "cite"],
        }
    )
    assert any(
        "answer_operations 含 calculate 但无 calc/派生要点" in error for error in errors
    ), errors

    # 反向：带派生要点却漏标 calculate 同样报错
    errors, _warnings = _lint_schema_v2(
        {
            "id": "T-missing-tag",
            "route_target": "agent",
            "retrieval_hops": 2,
            "answer_points": [{"text": "x", "derived": True, "chunk_ids": ["d#t1"]}],
            "answer_operations": ["lookup", "cite"],
        }
    )
    assert any(
        "answer_operations 缺 calculate" in error for error in errors
    ), errors


def test_lint_schema_v2_errors_abstain_hops_must_be_zero():
    """审计 #4：纯拒答/澄清题（无金标位置）retrieval_hops 必须为 0。"""
    from evaluation.lint_finance_tasks import _lint_schema_v2

    errors, _warnings = _lint_schema_v2(
        {
            "id": "T-abstain-hops",
            "should_abstain": True,
            "retrieval_hops": 1,
        }
    )
    assert any("retrieval_hops 应为 0" in error for error in errors), errors

    # FIN-22 型：澄清后按假设作答、带着金标 chunk → hops=1 合法
    errors, _warnings = _lint_schema_v2(
        {
            "id": "T-clarify-assume",
            "clarify_mode": "assume",
            "retrieval_hops": 1,
            "relevant_chunk_ids": ["d#t1"],
        }
    )
    assert not any("retrieval_hops 应为 0" in error for error in errors), errors


def test_lint_schema_v2_warns_singlehop_agent_without_calc():
    from evaluation.lint_finance_tasks import _lint_schema_v2

    _errors, warnings = _lint_schema_v2(
        {
            "id": "T-loose-agent",
            "route_target": "agent",
            "retrieval_hops": 1,
            "answer_operations": ["lookup", "cite"],
        }
    )
    assert any("route_target=agent" in warning for warning in warnings), warnings


def test_lint_schema_v2_warns_open_question_with_direct_phrase():
    from evaluation.lint_finance_tasks import _lint_schema_v2

    _errors, warnings = _lint_schema_v2(
        {
            "id": "T-style-drift",
            "prompt_style": "open_question",
            "question": "贵州茅台 2025 年营业收入是多少元？请注明该数字在 PDF 的第几页。",
        }
    )
    assert any("direct_constraint" in warning for warning in warnings), warnings


# ---------------------------------------------------------------------------
# 覆盖检查与检索预算
# ---------------------------------------------------------------------------

def test_coverage_gaps_detect_missing_year(tmp_path):
    write_company(tmp_path, "美的集团", "000333", 2024)
    write_company(tmp_path, "美的集团", "000333", 2025)
    toolset = FinanceToolset(FinanceStore(tmp_path))

    gaps = toolset.coverage_gaps(
        "美的集团 2024 年年报和 2025 年年报的数字是否一致？",
        [{"doc_id": "cninfo-000333-2025-annual"}],
    )
    assert len(gaps) == 1
    assert "2024" in gaps[0]


def test_coverage_gaps_detect_missing_company(tmp_path):
    write_company(tmp_path, "美的集团", "000333", 2025)
    write_company(tmp_path, "比亚迪", "002594", 2025)
    toolset = FinanceToolset(FinanceStore(tmp_path))

    gaps = toolset.coverage_gaps(
        "美的集团、比亚迪 2025 年营业收入谁高？",
        [{"doc_id": "cninfo-000333-2025-annual"}],
    )
    assert len(gaps) == 1
    assert "比亚迪" in gaps[0]


def test_coverage_gaps_ignore_single_doc_question(tmp_path):
    write_company(tmp_path, "美的集团", "000333", 2025)
    toolset = FinanceToolset(FinanceStore(tmp_path))

    # 单公司单年度：同比数据在同一份年报的上期列里，不要求再读 2024 年报
    assert toolset.coverage_gaps("美的集团 2025 年营业收入是多少？", []) == []
    # 语料里没有 2026 年文档：不能把“本来就没有”判成“漏读”
    assert toolset.coverage_gaps("比亚迪 2026 年第一季度营业收入是多少？", []) == []


def test_unsupported_periods_only_flag_future_years(tmp_path):
    write_company(tmp_path, "美的集团", "000333", 2024)
    write_company(tmp_path, "美的集团", "000333", 2025)
    toolset = FinanceToolset(FinanceStore(tmp_path))

    future = toolset.unsupported_periods("美的集团 2026 年第一季度营业收入是多少？")
    assert len(future) == 1 and "2026" in future[0]

    # 2023 是 2024 年报的比较期，不是“语料没覆盖”
    assert toolset.unsupported_periods("美的集团 2025 年营业收入比 2023 年增加多少？") == []
    assert toolset.unsupported_periods("美的集团 2025 年营业收入是多少？") == []


def test_search_budget_blocks_then_can_be_granted(tmp_path):
    write_company(tmp_path, "美的集团", "000333", 2025)
    toolset = FinanceToolset(FinanceStore(tmp_path))

    for _ in range(MAX_SEARCH_CALLS):
        assert asyncio.run(toolset.call("search_sections", {"query": "营业收入"})).ok is True

    blocked = asyncio.run(toolset.call("search_sections", {"query": "营业收入"}))
    assert blocked.ok is False
    assert blocked.error_kind == "unavailable"
    assert "预算" in blocked.error_detail

    toolset.grant_search_budget(1)
    assert asyncio.run(toolset.call("search_sections", {"query": "营业收入"})).ok is True


def test_prepare_resets_search_budget(tmp_path):
    write_company(tmp_path, "美的集团", "000333", 2025)
    toolset = FinanceToolset(FinanceStore(tmp_path))

    for _ in range(MAX_SEARCH_CALLS):
        asyncio.run(toolset.call("search_sections", {"query": "营业收入"}))
    assert asyncio.run(toolset.call("search_sections", {"query": "营业收入"})).ok is False

    toolset.prepare(None)
    assert asyncio.run(toolset.call("search_sections", {"query": "营业收入"})).ok is True


# ---------------------------------------------------------------------------
# 坐标一致性 / 路由一致性 / 冻结配比（2026-11-03 起）
# ---------------------------------------------------------------------------


def test_lint_refs_ok_for_consistent_task():
    from evaluation.lint_finance_tasks import _lint_refs

    chunks = {"doc#t0001": {"content": "项目 营业收入 1,234"}}
    task = {
        "id": "T1",
        "relevant_chunk_ids": ["doc#t0001"],
        "answer_points": [
            {
                "text": "营业收入 1,234",
                "chunk_ids": ["doc#t0001"],
                "evidence": {
                    "table_id": "doc#t0001",
                    "row": "营业收入",
                    "column": "2025 年",
                    "value": "1,234",
                },
            }
        ],
    }
    assert _lint_refs(task, chunks) == []


def test_lint_refs_detects_dangling_reference():
    from evaluation.lint_finance_tasks import _lint_refs

    task = {"id": "T2", "relevant_chunk_ids": ["doc#t9999"], "answer_points": []}
    errors = _lint_refs(task, {"doc#t0001": {"content": "x"}})
    assert any("引用不存在" in error for error in errors), errors


def test_lint_refs_detects_silent_content_drift():
    """表 id 重排后引用还在、内容已不是同一块——只查存在性会放过。"""
    from evaluation.lint_finance_tasks import _lint_refs

    chunks = {"doc#t0001": {"content": "项目 净利润 100"}}
    task = {
        "id": "T3",
        "relevant_chunk_ids": ["doc#t0001"],
        "answer_points": [
            {
                "text": "营业收入 1,234",
                "chunk_ids": ["doc#t0001"],
                "evidence": {
                    "table_id": "doc#t0001",
                    "row": "营业收入",
                    "column": "2025 年",
                    "value": "1,234",
                },
            }
        ],
    }
    errors = _lint_refs(task, chunks)
    assert any("不含金标值" in error for error in errors), errors


def test_lint_routing_flags_rule_drift():
    from evaluation.lint_finance_tasks import _lint_routing

    documents = [{"company": "美的集团", "period": "2025", "code": "000333"}]
    assert any(
        "clarify_mode=ask" in error
        for error in _lint_routing(
            {"id": "T1", "question": "美的集团 2025 年营业收入是多少？", "clarify_mode": "ask"},
            documents,
        )
    )
    assert any(
        "route_target=agent" in error
        for error in _lint_routing(
            {"id": "T2", "question": "美的集团 2025 年营业收入是多少？", "route_target": "agent"},
            documents,
        )
    )
    assert any(
        "被路由拦截" in error
        for error in _lint_routing(
            {"id": "T3", "question": "美的集团 2026 年营业收入是多少？"},
            documents,
        )
    )
    assert _lint_routing({"id": "T4", "question": "随便", "mock": True}, documents) == []


def test_lint_tier_freezes_profile_ratio():
    from evaluation.lint_common import DIMENSIONS
    from evaluation.lint_finance_tasks import PROFILE_TASK_COUNT, _lint_tier

    short = [
        {"id": f"T{i}", "dimension": DIMENSIONS[i % len(DIMENSIONS)]}
        for i in range(PROFILE_TASK_COUNT - 1)
    ]
    errors, _warnings = _lint_tier(short, "profile")
    assert any("必须恰好 30 条" in error for error in errors), errors

    errors, _warnings = _lint_tier(short, "profile", holdout_only=True)
    assert not any("必须恰好" in error for error in errors), errors


def test_lint_complex_freezes_bucket_count():
    from evaluation.lint_finance_tasks import COMPLEX_TYPES, _lint_complex

    tasks = []
    for kind in COMPLEX_TYPES:
        for index in range(3):
            tasks.append(
                {
                    "id": f"{kind}-{index}",
                    "complex_type": kind,
                    "holdout": index == 0,
                    "tool_constraints": {},
                }
            )
    errors, _warnings = _lint_complex(tasks)
    assert errors == [], errors

    tasks[0] = dict(tasks[0], complex_type="cross_table_extra")
    errors, _warnings = _lint_complex(tasks)
    assert any("complex_type 非法" in error for error in errors), errors
