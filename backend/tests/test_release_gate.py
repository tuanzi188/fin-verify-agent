"""Release Gate 测试：五类闸门、拦截/提示分流、缺数据不通过。"""

from evaluation.release_gate import (
    evaluate,
    metrics_from_faithfulness,
    metrics_from_numeric,
    metrics_from_report,
    metrics_from_retrieval,
    metrics_from_routing,
    metrics_from_trajectory,
)


def good_metrics():
    return {
        "pass_at_1": 0.9,
        "pass_pow_k": 0.8,
        "faithfulness": 0.95,
        "gold_coverage": 0.95,
        "ordered_rate": 0.95,
        "key_tools_rate": 0.95,
        "invalid_loop_rate": 0.0,
        "budget_ok_rate": 1.0,
        "token_mean": 30000,
        "token_p95": 50000,
        "calls_mean": 6,
        "numeric_support_gap_rate": 0.0,
        "fabrication": 0,
        "citation_errors": 0,
        "crashes": 0,
        "false_refusal_rate": 0.05,
        "abstain_accuracy": 1.0,
        "routing_accuracy": 0.9,
    }


def test_all_good_passes():
    result = evaluate(good_metrics())
    assert result["overall_pass"] is True
    assert result["blocking_failures"] == []
    assert result["gates"]["safety"]["status"] == "pass"


def test_numeric_support_gap_is_a_blocking_red_line():
    metrics = good_metrics()
    metrics["numeric_support_gap_rate"] = 0.1
    result = evaluate(metrics)
    assert result["overall_pass"] is False
    assert "safety.numeric_support_gap_rate" in result["blocking_failures"]


def test_false_refusal_is_advisory_unless_strict():
    metrics = good_metrics()
    metrics["false_refusal_rate"] = 0.2
    soft = evaluate(metrics)
    assert soft["overall_pass"] is True
    assert "business.false_refusal_rate" in soft["advisory_failures"]

    strict = evaluate(metrics, strict=True)
    assert strict["overall_pass"] is False
    assert "business.false_refusal_rate" in strict["blocking_failures"]


def test_missing_data_is_not_a_pass():
    result = evaluate({})
    assert result["overall_pass"] is None
    assert result["status"] == "insufficient_data"
    assert all(gate["status"] == "no_data" for gate in result["gates"].values())


def test_partial_success_cannot_hide_missing_required_metrics():
    for strict_mode in (False, True):
        partial = evaluate({"crashes": 0}, strict=strict_mode)
        assert partial["overall_pass"] is None
        assert partial["status"] == "insufficient_data"
        assert "safety.fabrication" in partial["missing_required"]
        assert partial["gates"]["safety"]["pass"] is None


def test_known_failure_still_blocks_with_missing_data():
    partial_failure = evaluate({"crashes": 1})
    assert partial_failure["overall_pass"] is False
    assert partial_failure["status"] == "fail"
    assert partial_failure["missing_required"]


def test_strict_mode_requires_false_refusal_measurement():
    incomplete = good_metrics()
    incomplete.pop("false_refusal_rate")
    assert evaluate(incomplete)["overall_pass"] is True
    strict_result = evaluate(incomplete, strict=True)
    assert strict_result["overall_pass"] is None
    assert "business.false_refusal_rate" in strict_result["missing_required"]


def test_metrics_from_report_extracts_false_refusal_rate():
    report = {
        "cross": {
            "pass_at_1": 0.5,
            "pass_pow_k": 0.4,
            "token_stats": {"mean": 100, "p95": 200},
            "calls_stats": {"mean": 3},
        },
        "hard_gates": {
            "fabrication": {"value": 0},
            "citation_errors": {"value": 1},
            "crashes": {"value": 0},
        },
        "dimensions": {"拒答与边界": {"point_estimate": 0.8}},
        "verdicts": [
            {"checks": {"false_refusal": {"ok": True}}},
            {"checks": {"false_refusal": {"ok": False}}},
            {"checks": {"false_refusal": {"ok": None}}},
        ],
    }
    metrics = metrics_from_report(report)
    assert metrics["false_refusal_count"] == 1
    assert metrics["answerable_runs"] == 2
    assert metrics["false_refusal_rate"] == 0.5
    assert metrics["token_p95"] == 200
    assert metrics["abstain_accuracy"] == 0.8


def test_metrics_from_report_skips_mock_false_refusal():
    report = {
        "verdicts": [
            {"mock": True, "checks": {"false_refusal": {"ok": False}}},
            {"mock": False, "checks": {"false_refusal": {"ok": True}}},
        ]
    }
    metrics = metrics_from_report(report)
    assert metrics["answerable_runs"] == 1
    assert metrics["false_refusal_count"] == 0


def test_metrics_from_other_layers():
    retrieval = {
        "top_k": 5,
        "arms": {
            "agent": {
                "summary": {
                    "primary_k": 5,
                    "first_search": {"5": {"point_hit_rate": 0.7, "task_hit_all_rate": 0.3, "mrr": 0.5, "context_precision": 0.2}},
                    "read": {"read_rate": 0.6},
                }
            }
        },
    }
    assert metrics_from_retrieval(retrieval)["retrieval_point_hit"] == 0.7
    assert metrics_from_routing({"accuracy": 0.8, "macro_f1": 0.7})["routing_accuracy"] == 0.8
    assert metrics_from_numeric({"hallucination_rate": 0.0, "gold_coverage": 0.9})[
        "numeric_support_gap_rate"
    ] is None
    assert metrics_from_numeric({"numeric_audit_version": "numeric-support-v1",
                                 "support_audit_complete": True,
                                 "unsupported_number_rate": 0.0})["numeric_support_gap_rate"] == 0.0
    assert metrics_from_trajectory({"ordered_rate": 1.0})["ordered_rate"] == 1.0
    assert metrics_from_faithfulness({"faithfulness_mean": 0.9})["faithfulness"] == 0.9
