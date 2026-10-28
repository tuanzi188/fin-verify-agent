"""判分层测试：Wilson 区间、确定性检查、维度判定、门禁、judge 解析。

离线：不调模型、不碰语料文件。所有输入都是手搓的 task / result / judgment。
"""

import json

import pytest

from evaluation.agent_checks import (
    check_citations,
    check_injection,
    check_retrieval_hit,
    check_status,
    check_tool_constraints,
    collect_checks,
    evidence_chunk_ids,
)
from evaluation.agent_judge import normalize_judgment, parse_judge_output
from evaluation.compare_agent_reports import compare_reports
from evaluation.merge_agent_runs import merge_payloads
from evaluation.agent_metrics import (
    fisher_exact_two_sided,
    fisher_min_split,
    format_interval,
    mcnemar_exact,
    mcnemar_min_split,
    percentile,
    wilson_interval,
    wilson_lower,
)
from evaluation.agent_report import (
    aggregate,
    build_badcases,
    build_construct_notes,
    build_power_k,
    build_resolution,
    classify_causes,
    dimension_verdict,
    hard_gates,
)

# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------


def make_task(**overrides) -> dict:
    task = {
        "id": "T1",
        "dimension": "检索与引用",
        "difficulty": "easy",
        "source": "synthetic",
        "question": "餐费上限多少？",
        "tool_constraints": {
            "must_include": ["search_kb"],
            "call_count": {"search_kb": [1, 3]},
            "order": [],
            "must_not_include": [],
        },
        "required_args": ["餐费"],
        "answer_points": [{"text": "每人每天一百元", "chunk_ids": ["d6_ch3"]}],
        "citation_required": True,
        "should_abstain": False,
        "clarify_mode": "none",
        "forbidden": ["编造其他金额"],
        "max_steps": 3,
        "relevant_chunk_ids": ["d6_ch3"],
        "query_type": "精确查找",
        "mock": False,
    }
    task.update(overrides)
    return task


def make_step(tool="search_kb", result_ids=None, injected=None, ok=True, **overrides) -> dict:
    step = {
        "step": 1,
        "tool": tool,
        "args": {"query": "x"},
        "ok": ok,
        "result_ids": result_ids if result_ids is not None else ["d6_ch3"],
        "result_count": len(result_ids) if result_ids is not None else 1,
        "injected": injected,
        "latency_ms": 120,
        "tokens_in": 0,
        "tokens_out": 0,
    }
    step.update(overrides)
    return step


def make_final(ok=True, note=None, **overrides) -> dict:
    step = {
        "step": 2,
        "tool": "__final__",
        "args": {},
        "ok": ok,
        "result_ids": [],
        "result_count": 0,
        "injected": None,
        "latency_ms": 800,
        "tokens_in": 100,
        "tokens_out": 30,
        "self_eval": None,
        "clarify": {"mode": "none", "signal": False},
    }
    if note:
        step["note"] = note
    step.update(overrides)
    return step


def make_result(**overrides) -> dict:
    result = {
        "task_id": "T1",
        "variant": "baseline",
        "run_index": 1,
        "status": "answered",
        "answer": "餐费上限为每人每天一百元 [1]。",
        "citations": [{"marker": 1, "chunk_id": "d6_ch3"}],
        "trace": [make_step(), make_final()],
        "usage": {"steps": 1, "tokens_in": 100, "tokens_out": 30, "latency_ms": 920},
    }
    result.update(overrides)
    return result


def make_judgment(**overrides) -> dict:
    judgment = {
        "points": [{
            "index": 0, "pass": True, "cited": True, "citation_supports": True, "reason": "ok",
        }],
        "forbidden_hit": False,
        "forbidden_reason": "",
        "grounded": True,
        "grounded_reason": "",
        "clarify_verdict": "ok",
        "clarify_reason": "",
    }
    judgment.update(overrides)
    return judgment


def make_judge_result(judgment=None, ok=True, error=None, agreement=1.0) -> dict:
    return {
        "ok": ok,
        "judgment": judgment,
        "error": error,
        "repeats": 1,
        "agreement": agreement,
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


# ---------------------------------------------------------------------------
# Wilson：拿文档里的表当验收
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "successes,total,expected_lower",
    [(5, 6, 0.436), (6, 6, 0.610), (7, 8, 0.529), (8, 8, 0.676), (11, 12, 0.646), (12, 12, 0.758)],
)
def test_wilson_lower_matches_design_doc_table(successes, total, expected_lower):
    assert wilson_lower(successes, total) == pytest.approx(expected_lower, abs=0.001)


def test_wilson_interval_is_bounded_and_wider_for_small_n():
    assert 0.0 <= wilson_interval(0, 6)[0] <= wilson_interval(0, 6)[1] <= 1.0
    small = wilson_interval(3, 6)
    large = wilson_interval(30, 60)
    assert (small[1] - small[0]) > (large[1] - large[0])


def test_wilson_handles_zero_total():
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_format_interval_shape():
    assert format_interval(5, 6) == "5/6 (83.3%) [43.6%, 97.0%]"
    assert format_interval(0, 0) == "N/A"


def test_percentile_linear_interpolation():
    assert percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)


def test_fisher_exact_matches_known_values():
    """n=6 的现实：差 3 条任务也只有 p≈0.24。"""
    assert fisher_exact_two_sided(6, 6, 6, 6) == pytest.approx(1.0)
    assert fisher_exact_two_sided(6, 6, 2, 6) == pytest.approx(0.0606, abs=1e-4)
    assert fisher_exact_two_sided(5, 6, 2, 6) == pytest.approx(0.2424, abs=1e-4)
    assert fisher_exact_two_sided(6, 6, 1, 6) < 0.05


def test_min_significant_splits_document_the_resolution():
    # 两批独立比较：每维 6 条要 6:1 才算差异，5:2 是噪声
    assert fisher_min_split(6) == (6, 1)
    assert fisher_exact_two_sided(6, 6, 2, 6) > 0.05
    # 配对设计：6 个不一致对要 6:0；整卷 30 条可容忍到 21:9
    assert mcnemar_min_split(6) == (6, 0)
    assert mcnemar_min_split(30) == (21, 9)
    assert mcnemar_exact(21, 9) < 0.05


def test_build_resolution_reports_one_task_granularity():
    dimensions = ("检索与引用", "多步拆解", "拒答与边界", "澄清与追问", "恢复与纠错")
    verdicts = [
        _verdict(dimension=dimension, task_id=f"{dimension}-{index}")
        for dimension in dimensions
        for index in range(6)
    ]

    resolution = build_resolution(verdicts, runs_per_task=1)

    assert resolution["tasks_per_dimension"]["检索与引用"] == 6
    assert resolution["one_task_percentage_points"] == 16.7
    assert "1/6" in resolution["independent_two_batch_min"]
    assert "21:9" in resolution["paired_suite_min"]
    assert resolution["runs_per_task"] == 1
    assert "只描述" in resolution["note"]
    assert percentile([5], 0.95) == 5.0
    assert percentile([], 0.95) is None


# ---------------------------------------------------------------------------
# 工具约束
# ---------------------------------------------------------------------------


def test_must_include_missing_is_failure():
    checks = check_tool_constraints(make_task(), make_result(trace=[make_final()]))
    assert checks["tool_must_include"]["ok"] is False
    assert "search_kb" in checks["tool_must_include"]["detail"]


def test_must_not_include_violation():
    task = make_task(tool_constraints={
        "must_include": ["search_kb"], "call_count": {}, "order": [], "must_not_include": ["list_documents"],
    })
    result = make_result(trace=[make_step(tool="list_documents"), make_final()])
    checks = check_tool_constraints(task, result)
    assert checks["tool_must_not_include"]["ok"] is False


def test_call_count_upper_bound_enforced():
    task = make_task(tool_constraints={
        "must_include": ["search_kb"], "call_count": {"search_kb": [1, 1]}, "order": [], "must_not_include": [],
    })
    result = make_result(trace=[make_step(), make_step(), make_final()])
    checks = check_tool_constraints(task, result)
    assert checks["tool_call_count"]["ok"] is False
    assert checks["tool_call_count"]["detail_by_tool"]["search_kb"]["actual"] == 2


def test_order_violation():
    task = make_task(tool_constraints={
        "must_include": [], "call_count": {}, "order": [["search_kb", "get_citation"]], "must_not_include": [],
    })
    result = make_result(trace=[make_step(tool="get_citation"), make_step(tool="search_kb"), make_final()])
    checks = check_tool_constraints(task, result)
    assert checks["tool_order"]["ok"] is False
    assert "search_kb 应在 get_citation 之前" in checks["tool_order"]["detail"]


# ---------------------------------------------------------------------------
# 召回命中：mock 必须排除
# ---------------------------------------------------------------------------


def test_retrieval_hit_counts_missing_chunks():
    check = check_retrieval_hit(make_task(relevant_chunk_ids=["d6_ch3", "d6_ch1"]), make_result())
    assert check["ok"] is False
    assert check["misses"] == ["d6_ch1"]


def test_retrieval_hit_is_na_for_mock_tasks():
    check = check_retrieval_hit(make_task(mock=True), make_result())
    assert check["ok"] is None
    assert "mock" in check["detail"]


def test_retrieval_hit_is_na_for_negative_tasks():
    check = check_retrieval_hit(make_task(relevant_chunk_ids=[]), make_result())
    assert check["ok"] is None


def test_evidence_chunk_ids_are_deduped_in_order():
    result = make_result(trace=[
        make_step(result_ids=["a", "b"]),
        make_step(result_ids=["b", "c"]),
        make_final(),
    ])
    assert evidence_chunk_ids(result) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 引用
# ---------------------------------------------------------------------------


def test_dangling_marker_detected():
    result = make_result(answer="答案 [1][7]。", citations=[{"marker": 1, "chunk_id": "d6_ch3"}])
    checks = check_citations(make_task(), result)
    assert checks["citation_markers_resolve"]["ok"] is False
    assert checks["citation_markers_resolve"]["dangling"] == [7]


def test_answer_point_not_cited_is_failure():
    task = make_task(answer_points=[
        {"text": "a", "chunk_ids": ["d6_ch3"]},
        {"text": "b", "chunk_ids": ["d6_ch1"]},
    ], relevant_chunk_ids=["d6_ch3", "d6_ch1"])
    result = make_result(
        answer="两句话 [1]。",
        citations=[{"marker": 1, "chunk_id": "d6_ch3"}],
        trace=[make_step(result_ids=["d6_ch3", "d6_ch1"]), make_final()],
    )
    checks = check_citations(task, result)
    assert checks["answer_points_cited"]["ok"] is False
    assert checks["answer_points_cited"]["points"][1]["ok"] is False


def test_citation_outside_retrieval_is_failure():
    result = make_result(
        citations=[{"marker": 1, "chunk_id": "d9_ch1"}],
        trace=[make_step(result_ids=["d6_ch3"]), make_final()],
    )
    checks = check_citations(make_task(), result)
    assert checks["citation_scope"]["ok"] is False
    assert checks["citation_scope"]["unknown"] == ["d9_ch1"]


def test_citation_present_only_enforced_when_required():
    assert check_citations(make_task(citation_required=True), make_result(citations=[]))["citation_present"]["ok"] is False
    assert check_citations(make_task(citation_required=False), make_result(citations=[]))["citation_present"]["ok"] is None


# ---------------------------------------------------------------------------
# 状态与预算
# ---------------------------------------------------------------------------


def test_false_refusal_on_positive_task():
    checks = check_status(make_task(should_abstain=False), make_result(status="abstained"))
    assert checks["false_refusal"]["ok"] is False
    assert checks["abstain_status"]["ok"] is False


def test_abstain_expected_and_delivered():
    task = make_task(should_abstain=True, answer_points=[], relevant_chunk_ids=[], citation_required=False)
    checks = check_status(task, make_result(status="abstained", citations=[]))
    assert checks["abstain_status"]["ok"] is True
    assert checks["false_refusal"]["ok"] is None


def test_negative_task_that_answers_is_failure():
    task = make_task(should_abstain=True, answer_points=[], relevant_chunk_ids=[], citation_required=False)
    checks = check_status(task, make_result(status="answered"))
    assert checks["abstain_status"]["ok"] is False


def test_step_budget_exceeded_via_trace_note():
    checks = check_status(make_task(max_steps=3), make_result(trace=[make_step(), make_final(note="max_steps_exceeded")]))
    assert checks["step_budget"]["ok"] is False


# ---------------------------------------------------------------------------
# 注入与恢复
# ---------------------------------------------------------------------------


def test_injection_triggered_false_when_planned_two_fired_one():
    task = make_task(mock=True, inject=[{"call_index": 1, "mode": "empty"}, {"call_index": 2, "mode": "timeout"}])
    result = make_result(trace=[make_step(injected="empty"), make_final()])
    checks = check_injection(task, result)
    assert checks["injection_triggered"]["ok"] is False
    assert checks["injection_triggered"]["fired"] == 1


def test_recovery_attempted_when_tool_call_after_failure():
    task = make_task(mock=True, inject=[{"call_index": 1, "mode": "timeout"}])
    result = make_result(trace=[
        make_step(ok=False, injected="timeout", result_ids=[]),
        make_step(ok=True, result_ids=["d6_ch3"]),
        make_final(),
    ])
    checks = check_injection(task, result)
    assert checks["recovery_attempted"]["ok"] is True
    assert checks["recovery_attempted"]["attempts_after"] == 1


def test_recovery_not_attempted_when_only_failed_call():
    task = make_task(mock=True, inject=[{"call_index": 1, "mode": "timeout"}])
    result = make_result(trace=[make_step(ok=False, injected="timeout", result_ids=[]), make_final()])
    checks = check_injection(task, result)
    assert checks["recovery_attempted"]["ok"] is False


def test_injection_checks_are_na_without_mock():
    checks = check_injection(make_task(), make_result())
    assert checks["injection_triggered"]["ok"] is None
    assert checks["recovery_attempted"]["ok"] is None


# ---------------------------------------------------------------------------
# 维度判定
# ---------------------------------------------------------------------------


def test_retrieval_dimension_passes_when_everything_holds():
    verdict = dimension_verdict(make_task(), make_result(), collect_checks(make_task(), make_result()), make_judge_result(make_judgment()))
    assert verdict["outcome"] == "pass"
    assert verdict["causes"] == []


def test_dimension_is_na_without_judge():
    verdict = dimension_verdict(
        make_task(), make_result(), collect_checks(make_task(), make_result()), {"ok": False, "error": "judge down"}
    )
    assert verdict["outcome"] == "na"
    assert "judge down" in verdict["reasons"][0]


def test_dimension_is_invalid_when_injection_never_fired():
    task = make_task(dimension="恢复与纠错", mock=True, inject=[{"call_index": 2, "mode": "timeout"}])
    result = make_result(trace=[make_step(), make_final()])
    verdict = dimension_verdict(task, result, collect_checks(task, result), make_judge_result(make_judgment()))
    assert verdict["outcome"] == "invalid"


def test_clarify_dimension_uses_only_clarify_verdict():
    task = make_task(dimension="澄清与追问", clarify_mode="ask", citation_required=False)
    result = make_result(answer="是哪一种？", citations=[])
    judgment = make_judgment(clarify_verdict="ok")
    verdict = dimension_verdict(task, result, collect_checks(task, result), make_judge_result(judgment))
    assert verdict["outcome"] == "pass"

    bad = make_judgment(clarify_verdict="bad", clarify_reason="直接给了单一结论")
    verdict_bad = dimension_verdict(task, result, collect_checks(task, result), make_judge_result(bad))
    assert verdict_bad["outcome"] == "fail"
    assert "澄清判定 bad" in verdict_bad["reasons"][0]


def test_abstain_dimension_fails_when_negative_task_answers():
    task = make_task(dimension="拒答与边界", should_abstain=True, answer_points=[], relevant_chunk_ids=[], citation_required=False)
    result = make_result(status="answered", answer="年假是 5 天。", citations=[])
    judged = make_judgment(points=[], forbidden_hit=True, forbidden_reason="编造 5 天")
    verdict = dimension_verdict(task, result, collect_checks(task, result), make_judge_result(judged))
    assert verdict["outcome"] == "fail"
    assert "编造" in verdict["causes"]
    assert "过度拒答" not in verdict["causes"]


def test_point_not_supported_fails_retrieval_dimension():
    task = make_task()
    result = make_result()
    judgment = make_judgment(points=[{
        "index": 0, "pass": True, "cited": True, "citation_supports": False, "reason": "引错段",
    }])
    verdict = dimension_verdict(task, result, collect_checks(task, result), make_judge_result(judgment))
    assert verdict["outcome"] == "fail"
    assert "引用错误" in verdict["causes"]


def test_classify_causes_priority_prefers_structural():
    task = make_task()
    result = make_result(status="error")
    judgment = make_judgment(forbidden_hit=True)
    causes = classify_causes(task, result, collect_checks(task, result), judgment)
    assert causes[0] == "崩溃"
    assert "编造" in causes


def test_uncited_point_counts_as_missing_citation_not_citation_error():
    """没引用 ≠ 引错。把"没引"记成"引错"会让引用错误率虚高。"""
    task = make_task()
    result = make_result(answer="答案没有标号。", citations=[])
    judgment = make_judgment(points=[{
        "index": 0, "pass": True, "cited": False, "citation_supports": False, "reason": "没标引用",
    }])
    causes = classify_causes(task, result, collect_checks(task, result), judgment)
    assert "引用缺失" in causes
    assert "引用错误" not in causes


def test_clarify_cause_only_counted_in_clarify_dimension():
    judgment = make_judgment(clarify_verdict="bad")
    task_other = make_task(dimension="恢复与纠错")
    causes_other = classify_causes(task_other, make_result(), collect_checks(task_other, make_result()), judgment)
    assert "澄清错误" not in causes_other

    task_clarify = make_task(dimension="澄清与追问", clarify_mode="ask")
    causes_clarify = classify_causes(
        task_clarify, make_result(), collect_checks(task_clarify, make_result()), judgment
    )
    assert "澄清错误" in causes_clarify


def test_false_refusal_cause_skipped_for_mock_tasks():
    task = make_task(mock=True, inject=[{"call_index": 1, "mode": "timeout"}])
    result = make_result(status="abstained", answer="无法确定。", citations=[], trace=[
        make_step(ok=False, injected="timeout", result_ids=[]), make_final(),
    ])
    causes = classify_causes(task, result, collect_checks(task, result), make_judgment())
    assert "过度拒答" not in causes
    assert "工具故障未恢复" in causes


def test_single_judgment_has_no_agreement_number():
    """只判一次就没有一致率可言，不能报 1.0。"""
    from evaluation.agent_judge import _merge_judgments

    merged, consistency = _merge_judgments([make_judgment()])
    assert merged["forbidden_hit"] is False
    assert consistency is None


# ---------------------------------------------------------------------------
# 汇总与门禁
# ---------------------------------------------------------------------------


def _verdict(dimension="检索与引用", outcome="pass", causes=None, **overrides) -> dict:
    verdict = {
        "task_id": "T",
        "run_index": 1,
        "dimension": dimension,
        "outcome": outcome,
        "reasons": [],
        "causes": causes or [],
        "checks": {},
        "judgment": None,
        "mock": False,
        "usage": {"steps": 1, "tokens_in": 10, "tokens_out": 5, "latency_ms": 1000},
        "answer": "a",
        "trace": [],
    }
    verdict.update(overrides)
    return verdict


def test_aggregate_excludes_na_and_invalid_from_denominator():
    verdicts = [
        _verdict(outcome="pass"),
        _verdict(outcome="fail"),
        _verdict(outcome="na"),
        _verdict(outcome="invalid"),
    ]
    aggregated = aggregate(verdicts)
    entry = aggregated["dimensions"]["检索与引用"]
    assert entry["pass"] == 1
    assert entry["fail"] == 1
    assert entry["denominator"] == 2
    assert entry["na"] == 1
    assert entry["invalid"] == 1
    assert entry["point_estimate"] == 0.5


def test_aggregate_empty_dimension_is_na():
    aggregated = aggregate([_verdict(outcome="pass")])
    assert aggregated["dimensions"]["多步拆解"]["label"] == "N/A"


def test_power_k_requires_all_runs_and_excludes_incomplete():
    verdicts = [
        _verdict(task_id="A", outcome="pass", run_index=1),
        _verdict(task_id="A", outcome="pass", run_index=2),
        _verdict(task_id="A", outcome="pass", run_index=3),
        # B 只有 2 次有效运行（第 3 次 NA），不能冒充"3 次全过"
        _verdict(task_id="B", outcome="pass", run_index=1),
        _verdict(task_id="B", outcome="fail", run_index=2),
        _verdict(task_id="B", outcome="na", run_index=3),
    ]
    power = build_power_k(verdicts, runs_per_task=3)

    assert power["overall"]["pass_at_1"] == pytest.approx(0.75)  # A=1.0, B=0.5
    assert power["overall"]["pass_pow_k"] == 1.0
    assert power["overall"]["complete_tasks"] == 1
    assert [row["task_id"] for row in power["excluded"]] == ["B"]


def test_power_k_marks_flaky_task_as_unstable():
    verdicts = [
        _verdict(task_id="A", outcome="pass", run_index=1),
        _verdict(task_id="A", outcome="pass", run_index=2),
        _verdict(task_id="A", outcome="fail", run_index=3),
    ]
    power = build_power_k(verdicts, runs_per_task=3)

    assert power["overall"]["pass_at_1"] == pytest.approx(2 / 3)
    assert power["overall"]["pass_pow_k"] == 0.0
    assert power["overall"]["stable_tasks"] == 0

    aggregated = aggregate(verdicts, runs_per_task=3)
    assert aggregated["cross"]["pass_at_1"] == pytest.approx(2 / 3)
    assert aggregated["cross"]["pass_pow_3"] == 0.0


def _fake_report(rows, tokens=100, latency=1000):
    return {
        "power_k": {
            "k": 3,
            "overall": {
                "pass_at_1": sum(row["pass_at_1"] for row in rows) / len(rows),
                "pass_pow_k": sum(1 for row in rows if row["pass_pow_k"] == 1.0) / len(rows),
                "complete_tasks": len(rows),
                "tasks": len(rows),
            },
            "by_dimension": {
                "检索与引用": {
                    "tasks": len(rows),
                    "pass_at_1": sum(row["pass_at_1"] for row in rows) / len(rows),
                }
            },
            "tasks": rows,
        },
        "cross": {
            "tokens_in": tokens * len(rows) * 3,
            "tokens_out": 0,
            "median_latency_ms": latency,
            "max_latency_ms": latency,
            "avg_steps": 2,
        },
    }


def test_merge_replaces_by_task_and_run_not_by_position():
    base = {
        "generated_at": "old",
        "results": [
            make_result(task_id="A", run_index=1, answer="old-a1"),
            make_result(task_id="A", run_index=2, answer="old-a2"),
        ],
    }
    patch = {
        "generated_at": "new",
        "results": [
            make_result(task_id="A", run_index=2, answer="new-a2"),
            make_result(task_id="B", run_index=1, answer="new-b1"),
        ],
    }

    merged = merge_payloads(base, patch)
    answers = {(row["task_id"], row["run_index"]): row["answer"] for row in merged["results"]}

    assert answers[("A", 1)] == "old-a1"
    assert answers[("A", 2)] == "new-a2"
    assert answers[("B", 1)] == "new-b1"
    assert merged["summary"]["total"] == 3
    assert merged["schema_problems"] == {}


def test_compare_reports_counts_fixed_and_broke():
    ids = [f"T{i}" for i in range(6)]

    def row(task_id, rate):
        return {
            "task_id": task_id,
            "dimension": "检索与引用",
            "runs": 3,
            "pass_at_1": rate,
            "pass_pow_k": 1.0 if rate == 1.0 else 0.0,
            "complete": True,
        }

    baseline = _fake_report([row(task_id, 0.0) for task_id in ids])
    improved = _fake_report([row(task_id, 1.0) for task_id in ids])

    result = compare_reports(baseline, improved, metric="majority")

    assert result["mcnemar"]["fixed"] == 6
    assert result["mcnemar"]["broke"] == 0
    assert result["mcnemar"]["p_value"] == pytest.approx(0.03125)
    assert result["mcnemar"]["significant"] is True
    assert "显著优于" in result["verdict"]


def test_cross_pass_pow_3_is_none_below_three_runs():
    verdicts = [
        _verdict(task_id="A", outcome="pass", run_index=1),
        _verdict(task_id="A", outcome="pass", run_index=2),
    ]
    aggregated = aggregate(verdicts, runs_per_task=2)
    assert aggregated["cross"]["pass_at_1"] == 1.0
    assert aggregated["cross"]["pass_pow_3"] is None
    assert "pass^2" in aggregated["cross"]["pass_note"]


def test_aggregate_judge_counts_requests_not_tasks():
    """自检抓过的口径错误：报告写"调用 30 次"，实际发了 30×repeats 次请求。"""
    verdicts = [_verdict(judgment=make_judgment()) for _ in range(3)]

    aggregated = aggregate(verdicts, judge_repeats=2)

    judge = aggregated["judge"]
    assert judge["judged_tasks"] == 3
    assert judge["repeats"] == 2
    assert judge["llm_calls"] == 6
    assert "点级" in judge["agreement_scope"]


def test_judge_error_counts_distinguish_unusable_from_partial():
    """repeats=2 时一次解析失败、另一次成功：维度仍有判定，不能记成"这条没分"。"""
    verdicts = [
        _verdict(judgment=None, judge_error="judge 输出解析失败"),
        _verdict(judgment=make_judgment(), judge_error="judge 输出解析失败"),
        _verdict(judgment=make_judgment(), judge_error=None),
    ]

    judge = aggregate(verdicts, judge_repeats=2)["judge"]

    assert judge["errors"] == 2
    assert judge["failed_tasks"] == 1
    assert judge["partial_failures"] == 1
    assert judge["judged_tasks"] == 2


def test_failure_causes_separate_primary_from_cooccurrence():
    """一次失败会挂多个原因；直方图若只报共现，会加起来超过失败条数。"""
    verdicts = [
        _verdict(outcome="fail", causes=["工具故障未恢复", "要点缺失"]),
        _verdict(outcome="fail", causes=["工具故障未恢复", "状态错误"]),
        _verdict(outcome="fail", causes=["要点缺失"]),
        _verdict(outcome="pass"),
    ]

    aggregated = aggregate(verdicts)
    summary = aggregated["failure_cause_summary"]
    causes = aggregated["failure_causes"]

    assert summary == {"failed_runs": 3, "primary_total": 3, "cooccurrence_total": 5}
    assert causes["工具故障未恢复"]["primary_count"] == 2
    assert causes["工具故障未恢复"]["cooccurrence_count"] == 2
    assert causes["要点缺失"]["primary_count"] == 1
    assert causes["要点缺失"]["cooccurrence_count"] == 2
    assert list(causes)[0] == "工具故障未恢复"  # 主因多的排前面


def test_construct_notes_flag_baseline_single_round_ceiling():
    notes = build_construct_notes("baseline", [_verdict()])
    joined = " ".join(notes)
    assert "一轮" in joined
    assert "恢复与纠错" in joined
    assert "不能当能力结论" in joined


def test_construct_notes_only_mention_clarify_when_dimension_present():
    without = build_construct_notes("baseline", [_verdict(dimension="检索与引用")])
    assert not any("澄清与追问" in note for note in without)

    with_clarify = build_construct_notes("baseline", [_verdict(dimension="澄清与追问")])
    assert any("澄清与追问" in note for note in with_clarify)
    assert any("静默选边" in note for note in with_clarify)


def test_construct_notes_warn_about_run_to_run_variance_only_for_single_run():
    single = build_construct_notes("baseline", [_verdict(run_index=1)], runs_per_task=1)
    assert any("run-to-run" in note for note in single)

    multi = build_construct_notes(
        "baseline",
        [_verdict(run_index=1), _verdict(run_index=2), _verdict(run_index=3)],
        runs_per_task=3,
    )
    assert not any("run-to-run" in note for note in multi)
    assert any("pass^k" in note for note in multi)


def test_construct_notes_explain_mock_exclusion():
    notes = build_construct_notes("baseline", [_verdict(mock=True)])
    assert any("mock" in note and "false_refusal" in note for note in notes)


def test_hard_gates_flag_fabrication():
    verdicts = [_verdict(outcome="fail", judgment=make_judgment(forbidden_hit=True), causes=["编造"])]
    gates = hard_gates(verdicts, {"median_latency_ms": 1000, "max_latency_ms": 2000})
    assert gates["fabrication"]["value"] == 1
    assert gates["fabrication"]["pass"] is False
    assert gates["overall_pass"] is False


def test_hard_gates_mark_latency_uncalibrated():
    gates = hard_gates([_verdict()], {"median_latency_ms": 7000, "max_latency_ms": 11000})
    assert gates["latency_median_ms"]["pass"] is False
    assert gates["latency_median_ms"]["calibrated"] is False
    assert gates["latency_median_ms"]["blocking"] is False
    assert "校准" in gates["latency_median_ms"]["note"]
    # 未校准的红不该拦住整体
    assert gates["overall_pass"] is True
    assert "latency_median_ms" in gates["advisory_failures"]


def test_false_refusal_gate_ignores_mock_tasks():
    """注入把工具打坏后说"资料不足"是诚实行为，不能算过度拒答。"""
    failed_check = {"false_refusal": {"ok": False, "detail": "正样本被拒答"}}
    verdicts = [
        _verdict(outcome="fail", mock=True, checks=failed_check),
    ]
    gates = hard_gates(verdicts, {"median_latency_ms": 1000, "max_latency_ms": 2000})
    assert gates["false_refusal"]["value"] == 0

    verdicts_real = [_verdict(outcome="fail", mock=False, checks=failed_check)]
    gates_real = hard_gates(verdicts_real, {"median_latency_ms": 1000, "max_latency_ms": 2000})
    assert gates_real["false_refusal"]["value"] == 1
    assert gates_real["overall_pass"] is False


def test_hard_gates_smoke_is_not_implemented():
    gates = hard_gates([_verdict()], {"median_latency_ms": 1000, "max_latency_ms": 2000})
    assert gates["smoke"]["pass"] is None
    assert gates["smoke"]["blocking"] is False
    assert "未实现" in gates["smoke"]["note"]


def test_badcases_carry_primary_cause_and_hint():
    verdicts = [_verdict(outcome="fail", causes=["召回失败", "要点缺失"], reasons=["命中 0/1"])]
    badcases = build_badcases(verdicts)
    assert len(badcases) == 1
    assert badcases[0]["primary_cause"] == "召回失败"
    assert badcases[0]["suggested_fix"]


# ---------------------------------------------------------------------------
# judge 解析
# ---------------------------------------------------------------------------


def test_parse_judge_output_plain_json():
    payload = parse_judge_output('{"forbidden_hit": false}')
    assert payload == {"forbidden_hit": False}


def test_parse_judge_output_with_code_fence():
    payload = parse_judge_output('```json\n{"grounded": true}\n```')
    assert payload == {"grounded": True}


def test_parse_judge_output_with_surrounding_prose():
    payload = parse_judge_output('好的，判定如下：{"clarify_verdict": "ok"} 以上。')
    assert payload == {"clarify_verdict": "ok"}


def test_parse_judge_output_raises_without_json():
    with pytest.raises(ValueError):
        parse_judge_output("这次我不给 JSON")


def test_normalize_fills_missing_points_as_fail():
    judgment = normalize_judgment({"points": [{"index": 0, "pass": True}]}, points_count=2)
    assert judgment["points"][0]["pass"] is True
    assert judgment["points"][1]["pass"] is False
    assert "未返回" in judgment["points"][1]["reason"]


def test_normalize_rejects_unknown_clarify_verdict():
    judgment = normalize_judgment({"clarify_verdict": "也许"}, points_count=0)
    assert judgment["clarify_verdict"] == "na"


def test_normalize_defaults_grounded_to_true_when_missing():
    judgment = normalize_judgment({}, points_count=0)
    assert judgment["grounded"] is True
    assert judgment["forbidden_hit"] is False


# ---------------------------------------------------------------------------
# judge 可用性：截断与解析失败的重试
# ---------------------------------------------------------------------------


def _judgment_json() -> str:
    return json.dumps({
        "points": [{"index": 0, "pass": True, "cited": True,
                    "citation_supports": True, "reason": "ok"}],
        "forbidden_hit": False,
        "forbidden_reason": "",
        "grounded": True,
        "grounded_reason": "",
        "clarify_verdict": "ok",
        "clarify_reason": "",
    }, ensure_ascii=False)


def test_judge_retries_after_length_truncation(monkeypatch):
    """空 content + finish_reason=length 是最常见的假解析失败，必须重试而不是记 NA。"""
    import asyncio
    from evaluation import agent_judge

    calls: list = []

    async def fake_simple_query(query, system_prompt=None, max_tokens=None):
        calls.append(max_tokens)
        if len(calls) == 1:
            return "", None, {"finish_reason": "length",
                              "prompt_tokens": 10, "completion_tokens": 4096}
        return _judgment_json(), None, {"finish_reason": "stop",
                                        "prompt_tokens": 10, "completion_tokens": 20}

    monkeypatch.setattr("app.services.model_provider.simple_query", fake_simple_query)
    monkeypatch.setattr("evaluation.agent_judge.build_prompt", lambda *a, **k: "prompt")

    task = {"id": "T1", "answer_points": [{"text": "x"}]}
    result = asyncio.run(agent_judge.judge_task(task, {}, {}))

    assert result["ok"] is True
    assert result["judgment"]["points"][0]["pass"] is True
    assert "截断" in (result["error"] or "")
    assert calls == [4096, 4096]


def test_judge_returns_na_after_all_attempts_fail(monkeypatch):
    import asyncio
    from evaluation import agent_judge

    calls: list = []

    async def fake_simple_query(query, system_prompt=None, max_tokens=None):
        calls.append(1)
        return "这不是 JSON", None, {"finish_reason": "stop"}

    monkeypatch.setattr("app.services.model_provider.simple_query", fake_simple_query)
    monkeypatch.setattr("evaluation.agent_judge.build_prompt", lambda *a, **k: "prompt")

    task = {"id": "T2", "answer_points": [{"text": "x"}]}
    result = asyncio.run(agent_judge.judge_task(task, {}, {}, repeats=2))

    assert result["ok"] is False
    assert result["judgment"] is None
    # 每个 repeat 都有 2 次尝试
    assert len(calls) == agent_judge.JUDGE_PARSE_ATTEMPTS * 2


# ---------------------------------------------------------------------------
# 位置偏差：只换展示顺序，不换 index 标签
# ---------------------------------------------------------------------------

def test_judge_prompt_reverses_display_order_but_keeps_indices():
    from evaluation.agent_judge import build_prompt

    task = {
        "id": "T-order",
        "question": "q",
        "clarify_mode": "none",
        "forbidden": [],
        "answer_points": [
            {"text": "要点甲", "chunk_ids": ["c1"]},
            {"text": "要点乙", "chunk_ids": ["c2"]},
        ],
    }
    result = {"answer": "答", "citations": [{"marker": 1, "chunk_id": "c1"}], "run_index": 1}
    chunks = {"c1": {"content": "原文甲"}, "c2": {"content": "原文乙"}}

    forward = build_prompt(task, result, chunks)
    backward = build_prompt(task, result, chunks, reverse=True)

    assert forward.index("0. 要点：要点甲") < forward.index("1. 要点：要点乙")
    assert backward.index("1. 要点：要点乙") < backward.index("0. 要点：要点甲")
    # 标签没被换掉：倒序时 0 号还是 0 号，判分结果才能按 index 归位
    assert "0. 要点：要点甲" in backward
    assert forward.index("[chunk_id=c1]") < forward.index("[chunk_id=c2]")
    assert backward.index("[chunk_id=c2]") < backward.index("[chunk_id=c1]")


def test_judge_alternates_order_across_repeats_and_is_deterministic(monkeypatch):
    import asyncio
    from evaluation import agent_judge

    async def fake_simple_query(query, system_prompt=None, max_tokens=None):
        return _judgment_json(), None, {"finish_reason": "stop"}

    monkeypatch.setattr("app.services.model_provider.simple_query", fake_simple_query)

    task = {"id": "T3", "answer_points": [{"text": "x"}]}
    result = {"answer": "答", "run_index": 1}
    first = asyncio.run(agent_judge.judge_task(task, result, {}, repeats=2))
    second = asyncio.run(agent_judge.judge_task(task, result, {}, repeats=2))

    # 两轮重复必须一次正序一次倒序，否则位置偏差会整批落在同一侧
    assert sorted(first["orders"]) == ["forward", "reversed"]
    # 确定性：同一份输入重判得到同一套顺序，评测才可复现
    assert first["orders"] == second["orders"]
