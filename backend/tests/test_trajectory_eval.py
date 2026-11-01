"""轨迹评测测试：按序匹配、关键工具、无效循环、聚合。"""

from evaluation.trajectory_eval import (
    aggregate,
    evaluate,
    invalid_loops,
    key_tools,
    ordered_match,
    required_sequence,
    tool_sequence,
)


def make_task(**overrides):
    task = {
        "id": "T",
        "dimension": "多步拆解",
        "question": "美的集团 2025 年营业收入同比增长多少？",
        "citation_required": True,
        "max_steps": 16,
        "answer_points": [{"text": "同比", "derived": True, "chunk_ids": ["doc#t1"]}],
        "calc": {"formula": "a/b", "inputs": {"a": 1, "b": 2}, "expected": "0.5"},
    }
    task.update(overrides)
    return task


def make_result(tools, **overrides):
    trace = [
        {"step": index, "tool": tool, "args": {"n": index}, "ok": True, "result_ids": []}
        for index, tool in enumerate(tools, 1)
    ]
    result = {"task_id": "T", "trace": trace, "usage": {"steps": len(trace)}}
    result.update(overrides)
    return result


def test_ordered_match_is_subsequence_not_contiguous():
    assert ordered_match(["search_blocks", "get_table", "read_block"], ["search", "read"])["ok"] is True
    assert ordered_match(["read_block", "search_blocks"], ["search", "read"])["ok"] is False


def test_required_sequence_defaults():
    assert required_sequence({"should_abstain": True}) == ["search"]
    assert required_sequence({"citation_required": True}) == ["search", "read"]
    assert required_sequence({"citation_required": True, "required_sequence": ["search_blocks"]}) == [
        "search_blocks"
    ]


def test_key_tools_defaults_to_calculate_for_derived_points():
    assert key_tools({"answer_points": [{"derived": True}]}) == ["calculate"]
    assert key_tools({"key_tools": ["get_rows"]}) == ["get_rows"]


def test_tool_sequence_ignores_final_step():
    result = {
        "trace": [
            {"step": 1, "tool": "search_blocks"},
            {"step": 2, "tool": "__final__"},
        ]
    }
    assert tool_sequence(result) == ["search_blocks"]


def test_evaluate_passes_with_required_sequence_and_key_tool():
    task = make_task()
    result = make_result(["search_blocks", "read_block", "calculate"])
    row = evaluate(task, result)
    assert row["ordered_ok"] is True
    assert row["key_tools_ok"] is True
    assert row["invalid_loops"] == 0
    assert row["passed"] is True


def test_evaluate_fails_when_calculate_missing():
    task = make_task()
    result = make_result(["search_blocks", "read_block"])
    row = evaluate(task, result)
    assert row["key_tools_ok"] is False
    assert row["passed"] is False


def test_invalid_loops_counts_repeats_and_duplicates():
    trace = [
        {"step": index, "tool": "get_table", "args": {"table_id": "doc#t1"}, "ok": True}
        for index in range(1, 4)
    ]
    result = {"trace": trace, "usage": {"steps": 3, "duplicate_skipped": 4}}
    loops = invalid_loops(result, {"max_steps": 16})
    assert loops["count"] == 2
    assert len(loops["repeated"]) == 1


def test_aggregate_reports_rates_and_failures():
    rows = [
        evaluate(make_task(), make_result(["search_blocks", "read_block", "calculate"])),
        evaluate(make_task(), make_result(["search_blocks", "read_block"])),
    ]
    summary = aggregate(rows)
    assert summary["tasks"] == 2
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["key_tools_ok"] == 1
    assert len(summary["failures"]) == 1
