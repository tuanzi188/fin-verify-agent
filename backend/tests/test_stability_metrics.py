"""稳定性口径测试：连续型分布、pass@k / pass^k、flaky 任务、样本分组与元数据分栏。"""

from evaluation.agent_metrics import continuous_summary
from evaluation.agent_report import (
    aggregate,
    build_populations,
    build_power_k,
    population_of,
    render_console,
)


def verdict(task_id, run_index, outcome, dimension="检索与引用", tokens=100, steps=2):
    return {
        "task_id": task_id,
        "run_index": run_index,
        "dimension": dimension,
        "outcome": outcome,
        "reasons": [],
        "causes": [],
        "checks": {},
        "judgment": None,
        "usage": {"tokens_in": tokens, "tokens_out": 0, "steps": steps, "latency_ms": 1000},
    }


def test_continuous_summary_reports_spread_and_tail():
    summary = continuous_summary([1, 2, 3, 4])
    assert summary["n"] == 4
    assert summary["mean"] == 2.5
    assert summary["std"] == 1.29
    assert summary["median"] == 2.5
    assert summary["p95"] == 3.85
    assert summary["min"] == 1
    assert summary["max"] == 4


def test_continuous_summary_single_sample_has_no_std():
    summary = continuous_summary([5])
    assert summary["n"] == 1
    assert summary["std"] is None
    assert continuous_summary([])["n"] == 0


def test_pass_at_k_and_pass_pow_k_separate_flaky_tasks():
    verdicts = [
        verdict("T1", 1, "pass"),
        verdict("T1", 2, "fail"),
        verdict("T1", 3, "pass"),
        verdict("T2", 1, "pass"),
        verdict("T2", 2, "pass"),
        verdict("T2", 3, "pass"),
        verdict("T3", 1, "fail"),
        verdict("T3", 2, "fail"),
        verdict("T3", 3, "fail"),
    ]
    power = build_power_k(verdicts, runs_per_task=3)
    overall = power["overall"]
    assert round(overall["pass_at_1"], 6) == round((2 / 3 + 1 + 0) / 3, 6)
    assert round(overall["pass_at_k"], 6) == round(2 / 3, 6)
    assert round(overall["pass_pow_k"], 6) == round(1 / 3, 6)
    assert overall["flaky_tasks"] == 1
    assert overall["always_fail_tasks"] == 1
    rows = {row["task_id"]: row for row in power["tasks"]}
    assert rows["T1"]["outcomes"] == "PFP"
    assert rows["T1"]["flaky"] is True


def test_aggregate_exposes_pass_at_k_and_continuous_stats():
    verdicts = [
        verdict("T1", 1, "pass", tokens=100),
        verdict("T1", 2, "fail", tokens=300),
        verdict("T1", 3, "pass", tokens=200),
    ]
    summary = aggregate(verdicts, runs_per_task=3)
    cross = summary["cross"]
    assert cross["pass_at_k"] == 1.0
    assert cross["pass_pow_k"] == 0.0
    assert cross["flaky_tasks"] == 1
    assert cross["token_stats"]["n"] == 3
    assert cross["token_stats"]["mean"] == 200
    assert cross["token_stats"]["p95"] == 290
    console = render_console(
        {
            "variant": "improved",
            "runs_per_task": 3,
            "model": "m",
            "corpus_version": "c",
            "corpus_chunks": 1,
            "corpus_hash": "h",
            "dimensions": summary["dimensions"],
            "cross": cross,
            "resolution": {},
            "power_k": summary["power_k"],
            "hard_gates": {},
            "failure_causes": {},
            "failure_cause_summary": {},
            "judge": {},
            "badcase_count": 0,
            "construct_notes": [],
        }
    )
    assert "pass@3" in console
    assert "token/次" in console


def test_population_of_routes_by_mock_and_dimension():
    assert population_of({"mock": True, "dimension": "恢复与纠错"}) == "mock_recovery"
    assert population_of({"mock": False, "dimension": "拒答与边界"}) == "refusal_clarification"
    assert population_of({"mock": False, "dimension": "澄清与追问"}) == "refusal_clarification"
    assert population_of({"mock": False, "dimension": "检索与引用"}) == "core_financial_qa"
    # 兜底：mock=False 的恢复维度也归 mock_recovery（主集恢复维度全部是 mock）
    assert population_of({"mock": False, "dimension": "恢复与纠错"}) == "mock_recovery"


def test_build_populations_splits_three_groups_without_mock_pollution():
    verdicts = [
        verdict("T1", 1, "pass", dimension="检索与引用"),
        verdict("T2", 1, "fail", dimension="多步拆解"),
        verdict("T3", 1, "pass", dimension="拒答与边界"),
        verdict("T4", 1, "pass", dimension="澄清与追问"),
        verdict("T5", 1, "pass", dimension="恢复与纠错", tokens=500),
    ]
    for item in verdicts:
        if item["dimension"] == "恢复与纠错":
            item["mock"] = True
    populations = build_populations(verdicts)
    assert populations["core_financial_qa"]["tasks"] == 2
    assert populations["core_financial_qa"]["cross"]["pass_at_1"] == 0.5
    assert populations["refusal_clarification"]["tasks"] == 2
    assert populations["refusal_clarification"]["cross"]["pass_at_1"] == 1.0
    assert populations["mock_recovery"]["tasks"] == 1
    # 核心组的 token 统计不含 mock 题：mock 的 500 token 不能拉高核心均值
    core_tokens = populations["core_financial_qa"]["cross"]["token_stats"]
    assert core_tokens["max"] == 100
    # 恢复组单独成表
    assert populations["mock_recovery"]["cross"]["pass_at_1"] == 1.0


def test_build_populations_empty_group_is_not_zero():
    populations = build_populations([verdict("T1", 1, "pass")])
    assert populations["core_financial_qa"]["tasks"] == 1
    assert populations["mock_recovery"]["tasks"] == 0
    assert populations["mock_recovery"]["note"] == "该组没有样本"


def test_aggregate_breaks_down_by_prompt_style_and_hops():
    verdicts = [
        dict(verdict("T1", 1, "pass"), prompt_style="open_question", retrieval_hops=1),
        dict(verdict("T2", 1, "fail"), prompt_style="open_question", retrieval_hops=1),
        dict(verdict("T3", 1, "pass"), prompt_style="direct_constraint", retrieval_hops=2),
        dict(verdict("T4", 1, "pass", dimension="拒答与边界"), prompt_style=None),
    ]
    summary = aggregate(verdicts)
    style = summary["prompt_style_breakdown"]
    assert style["open_question"]["passed"] == 1
    assert style["open_question"]["denominator"] == 2
    assert style["direct_constraint"]["passed"] == 1
    # T4 没标 style：落进"未标注"桶，而不是被扔掉
    assert style["未标注"]["runs"] == 1
    assert style["未标注"]["passed"] == 1
    hops = summary["retrieval_hops_breakdown"]
    assert hops["1"]["passed"] == 1
    assert hops["2"]["passed"] == 1
    console = render_console(
        {
            "variant": "improved",
            "runs_per_task": 1,
            "model": "m",
            "corpus_version": "c",
            "corpus_chunks": 1,
            "corpus_hash": "h",
            "dimensions": summary["dimensions"],
            "cross": summary["cross"],
            "resolution": {},
            "power_k": summary["power_k"],
            "hard_gates": {},
            "failure_causes": {},
            "failure_cause_summary": {},
            "judge": {},
            "badcase_count": 0,
            "construct_notes": [],
            "populations": build_populations(verdicts),
            "prompt_style_breakdown": style,
            "retrieval_hops_breakdown": hops,
        }
    )
    assert "样本分组" in console
    assert "mock 恢复不进核心总分" in console
    assert "prompt_style 分栏" in console
    assert "retrieval_hops 分栏" in console
