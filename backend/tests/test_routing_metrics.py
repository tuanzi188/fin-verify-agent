"""路由层指标测试：金标推导、混淆矩阵、Macro-F1、经验金标。"""

from evaluation.routing_metrics import (
    evaluate,
    evaluate_from_compare,
    expected_arm,
)


class FakeStore:
    def list_documents(self):
        return [{"company": "美的集团", "period": "2025", "code": "000333"}]


def make_task(task_id, query_type, question, **overrides):
    task = {
        "id": task_id,
        "query_type": query_type,
        "question": question,
        "dimension": "检索与引用",
    }
    task.update(overrides)
    return task


def test_expected_arm_from_task_structure():
    assert expected_arm(make_task("A", "single_hop", "美的集团 2025 年营业收入是多少？"))[0] == "structured_rag"
    assert expected_arm(make_task("B", "multi_hop", "美的集团 2025 年营业收入是多少？"))[0] == "agent"
    assert expected_arm(make_task("C", "complex", "跨表计算"))[0] == "agent"
    assert expected_arm(make_task("D", "abstain", "五粮液 2025 年营业收入是多少？"))[0] is None
    assert expected_arm(make_task("E", "single_hop", "美的集团 2025 年营业收入是多少？", mock=True))[0] is None
    assert expected_arm(make_task("F", "unit_trap", "美的集团 2025 年营业收入折合多少亿元？"))[0] is None


def test_expected_arm_upgrades_multi_company_and_multi_period():
    assert expected_arm(
        make_task("A", "single_hop", "美的集团和比亚迪 2025 年营业收入分别是多少？")
    )[0] == "agent"
    assert expected_arm(
        make_task("B", "single_hop", "美的集团 2024 年与 2025 年营业收入分别是多少？")
    )[0] == "agent"


def test_explicit_expected_arm_wins():
    task = make_task("A", "single_hop", "美的集团 2025 年营业收入是多少？", expected_arm="agent")
    assert expected_arm(task) == ("agent", "explicit:legacy_expected_arm")


def test_route_target_can_mark_ambiguous_cases_without_making_up_a_winner():
    task = make_task(
        "A",
        "single_hop",
        "美的集团 2025 年营业收入是多少？",
        route_target="indeterminate",
    )
    assert expected_arm(task) == (None, "explicit:indeterminate")


def test_evaluate_confusion_and_cost_types():
    tasks = [
        make_task("T1", "single_hop", "美的集团 2025 年营业收入是多少？"),
        make_task("T2", "multi_hop", "美的集团 2025 年营业收入是多少？比上年增减多少？"),
        make_task("T3", "single_hop", "美的集团 2025 年营业收入是多少？同比增减多少？"),
        make_task("T4", "multi_hop", "美的集团 2025 年营业收入是多少？"),
        make_task("T5", "abstain", "五粮液 2025 年营业收入是多少？"),
        make_task("T6", "multi_hop", "美的集团 2025 年营业收入是多少？", mock=True),
    ]
    summary = evaluate(tasks, store=FakeStore())

    assert summary["tasks"] == 4
    assert summary["correct"] == 2
    assert summary["accuracy"] == 0.5
    assert summary["macro_f1"] == 0.3333
    assert summary["confusion"]["structured_rag"]["agent"] == 0
    assert summary["confusion"]["agent"]["structured_rag"] == 2
    assert summary["cost_breakdown"] == {"simple_to_agent": 0, "complex_to_rag": 2}
    assert summary["excluded_count"] == 2
    assert {row["task_id"] for row in summary["errors"]} == {"T2", "T4"}


def test_evaluate_from_compare_uses_empirical_winners():
    tasks = [
        make_task("T1", "single_hop", "美的集团 2025 年营业收入是多少？"),
        make_task("T2", "multi_hop", "美的集团 2025 年营业收入是多少？"),
    ]
    payload = {
        "pairs": [
            {
                "task_id": "T1",
                "run_index": 1,
                "agent": {"pass": True},
                "rag": {"pass": False},
            },
            {
                "task_id": "T2",
                "run_index": 1,
                "agent": {"pass": False},
                "rag": {"pass": True},
            },
            {
                "task_id": "T1",
                "run_index": 2,
                "agent": {"pass": True},
                "rag": {"pass": True},
            },
        ]
    }
    summary = evaluate_from_compare(payload, tasks, store=FakeStore())
    assert summary["basis"] == "empirical_compare"
    assert summary["tasks"] == 2
    assert summary["ties_excluded"] == 1
    # T1 经验金标 agent，router 给 structured_rag → 错；T2 经验金标 structured_rag，router 给 structured_rag → 对
    assert summary["correct"] == 1
    assert summary["accuracy"] == 0.5
