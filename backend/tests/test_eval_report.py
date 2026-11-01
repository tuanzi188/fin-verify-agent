"""三层报告测试：层状态、失败分岔、误拒诊断、Markdown 渲染。"""

from evaluation.eval_report import build_report, render_markdown


def make_retrieval():
    return {
        "kind": "retrieval_metrics",
        "top_k": 5,
        "arms": {
            "agent": {
                "summary": {
                    "tasks": 1,
                    "primary_k": 5,
                    "first_search": {
                        "5": {
                            "point_hits": 0,
                            "point_total": 2,
                            "point_hit_rate": 0.0,
                            "task_hit_all_rate": 0.0,
                            "mrr": 0.0,
                            "context_precision": 0.0,
                        }
                    },
                    "any_search": {"5": {"point_hit_rate": 0.0}},
                    "read": {"read_rate": 0.0},
                    "searches": {"mean": 1.0},
                },
                "tasks": [
                    {
                        "task_id": "T1",
                        "first_search": {"5": {"point_hits": 0}},
                        "any_search": {"5": {"point_hits": 0}},
                        "gold_points": 2,
                        "read_points": 0,
                        "read_rate": 0.0,
                        "searches": 1,
                    }
                ],
            }
        },
    }


def make_answer():
    return {
        "variant": "improved",
        "runs_per_task": 1,
        "cross": {
            "pass_at_1": 0.0,
            "pass_at_k": None,
            "pass_pow_k": None,
            "flaky_tasks": 0,
            "citation_precision": None,
            "grounded_rate": None,
            "token_stats": {"n": 1, "mean": 40000, "std": None, "median": 40000, "p95": 40000},
        },
        "dimensions": {
            "检索与引用": {"label": "0/1 (0.0%)", "point_estimate": 0.0, "interval": [0.0, 0.8], "denominator": 1, "sample_note": "样本不足"}
        },
        "hard_gates": {},
        "verdicts": [
            {
                "task_id": "T1",
                "run_index": 1,
                "outcome": "fail",
                "checks": {"false_refusal": {"ok": False, "status": "abstained"}},
            }
        ],
        "failure_taxonomy": {"primary": {"检索无命中": 1}, "cooccurrence": {"检索无命中": 1}},
    }


def test_build_report_marks_layers_and_failure_split():
    report = build_report(
        retrieval=make_retrieval(),
        routing={"basis": "task_metadata", "tasks": 1, "accuracy": 0.0, "wilson": [0.0, 0.8], "macro_f1": 0.0, "errors": []},
        answer=make_answer(),
        numeric={"gold_coverage": 0.0, "hallucination_rate": 0.0, "unsourced_rate": 0.0},
        trajectory={"pass_rate": 1.0, "ordered_rate": 1.0, "key_tools_rate": 1.0, "invalid_loop_rate": 0.0},
        faithfulness={"faithfulness_mean": 0.5, "faithfulness_micro": 0.5},
    )
    assert report["layers"]["retrieval"]["status"] == "ok"
    assert report["layers"]["routing"]["status"] == "ok"
    assert report["layers"]["answer"]["status"] == "ok"
    assert report["layers"]["gate"]["status"] == "no_data"

    split = report["diagnostics"]["failure_split"]
    assert split["retrieval_miss"]["count"] == 1
    assert split["retrieval_hit_but_wrong"]["count"] == 0

    refusal = report["diagnostics"]["false_refusal"]
    assert refusal["false_refusals"] == 1
    assert refusal["hit_but_refused"] == 0


def test_missing_layers_are_no_data_not_zero():
    report = build_report()
    for layer in ("retrieval", "routing", "answer", "numeric", "trajectory", "faithfulness", "gate"):
        assert report["layers"][layer]["status"] == "no_data"


def test_markdown_contains_all_sections():
    report = build_report(
        retrieval=make_retrieval(),
        routing={"basis": "task_metadata", "tasks": 1, "accuracy": 0.0, "wilson": [0.0, 0.8], "macro_f1": 0.0, "errors": []},
        answer=make_answer(),
    )
    markdown = render_markdown(report)
    assert "# 金融查证 Agent · 三层评测报告" in markdown
    assert "## 1. 检索层" in markdown
    assert "## 2. 路由层" in markdown
    assert "## 3. 答案层" in markdown
    assert "检索 miss：1 条" in markdown
