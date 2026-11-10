"""判分完整性与数值来源回归：尾部错误、深层表格、金标泄漏和单位差异。"""

from unittest.mock import AsyncMock

import pytest

from evaluation import agent_judge
from evaluation.judge_context import JudgeContextError, judge_coordinate_content
from evaluation.numeric_audit import aggregate, audit_result
from evaluation.numeric_support import evaluate_numeric_support
from evaluation.release_gate import metrics_from_numeric


def integrity_table_chunks():
    return {
        "doc#t1": {
            "chunk_id": "doc#t1",
            "content": "旧截断摘要",
            "table": {
                "caption": "利润表",
                "unit": "万元",
                "columns": ["项目", "2025年", "2024年"],
                "rows": [["营业收入", "100", "80"], ["净利润", "20", "10"]],
            },
        }
    }


def integrity_run(answer, anchor="doc#t1#rows#0#cols#1", calculations=None):
    return {
        "answer": answer,
        "status": "answered",
        "run_index": 1,
        "citations": [{"marker": 1, "chunk_id": anchor}],
        "evidence": [{"chunk_id": anchor}],
        "trace": [{"tool": "get_rows", "ok": True, "result_ids": [anchor]}],
        "calc": calculations or [],
    }


def test_judge_sees_answer_tail_beyond_previous_limit():
    long_answer = "这是一段说明。" * 250 + "最后还声称收入999万元。"
    prompt = agent_judge.build_prompt(
        {"id": "tail", "question": "问"}, {"answer": long_answer}, {}
    )
    assert long_answer in prompt


def test_coordinate_evidence_reaches_deep_rows_without_unrelated_values():
    chunks = integrity_table_chunks()
    chunks["doc#t1"]["table"]["rows"] = [
        ["普通项目", str(index), "0"] for index in range(120)
    ]
    chunks["doc#t1"]["table"]["rows"][99] = ["目标项目", "987654", "543210"]
    content = judge_coordinate_content("doc#t1#rows#99#cols#1", chunks)
    assert "987654" in content
    assert "543210" not in content
    assert "[行99]" in content
    assert "普通项目" not in content


def test_judge_preserves_complete_paragraph_snapshot():
    full = "正文。" * 200 + "末尾依据100万元"
    prompt = agent_judge.build_prompt(
        {"id": "paragraph", "question": "问"},
        {"answer": "答", "citations": [{"marker": 1, "chunk_id": "doc#s1"}]},
        {"doc#s1": {"content": full[:200], "full_content": full}},
    )
    assert "末尾依据100万元" in prompt


def test_invalid_coordinate_is_not_replaced_by_whole_table():
    with pytest.raises(JudgeContextError):
        judge_coordinate_content("doc#t1#rows#999#cols#1", integrity_table_chunks())


@pytest.mark.asyncio
async def test_oversized_judgment_is_na_without_calling_model(monkeypatch):
    model_call = AsyncMock()
    monkeypatch.setattr(agent_judge, "_call_with_backoff", model_call)
    outcome = await agent_judge.judge_task(
        {"id": "too-long"}, {"answer": "长" * 65000}, {}
    )
    assert outcome["ok"] is False
    assert "超过" in outcome["error"]
    model_call.assert_not_awaited()


def test_numeric_support_does_not_borrow_gold_answer():
    task = {
        "id": "gold",
        "answer_points": [
            {"text": "收入999万元", "evidence": {"value": "999"}, "unit": "万元"}
        ],
    }
    row = audit_result(
        task, integrity_run("营业收入999万元[1]。"), integrity_table_chunks()
    )
    assert row["unsupported_number_count"] == 1
    assert row["unsupported_number_rate"] == 1.0


def test_numeric_support_rejects_wrong_unit_for_same_digits():
    audit = evaluate_numeric_support(
        integrity_run("营业收入100元[1]。"), integrity_table_chunks()
    )
    assert audit["unsupported_number_count"] == 1
    valid = evaluate_numeric_support(
        integrity_run("营业收入1000000元[1]。"), integrity_table_chunks()
    )
    assert valid["unsupported_number_count"] == 0


def test_numeric_support_only_uses_read_coordinates():
    audit = evaluate_numeric_support(
        integrity_run("净利润20万元[1]。"), integrity_table_chunks()
    )
    assert audit["unsupported_number_count"] == 1


def test_numeric_support_search_result_is_not_read_evidence():
    run = integrity_run("营业收入100万元[1]。")
    run["evidence"] = []
    run["trace"][0]["tool"] = "search_tables"
    assert (
        evaluate_numeric_support(run, integrity_table_chunks())[
            "unsupported_number_count"
        ]
        == 1
    )


def test_numeric_support_recomputes_only_grounded_calculations():
    run = integrity_run(
        "增加25%[1]。",
        "doc#t1#rows#0#cols#1,2",
        [
            {
                "formula": "(a-b)/b*100",
                "inputs": {"a": "100", "b": "80"},
                "result": "999",
            },
        ],
    )
    audit = evaluate_numeric_support(run, integrity_table_chunks())
    assert audit["unsupported_number_count"] == 0
    run["calc"][0]["inputs"]["b"] = "40"
    assert (
        evaluate_numeric_support(run, integrity_table_chunks())[
            "unsupported_number_count"
        ]
        == 1
    )


def test_unresolved_truncated_evidence_prevents_complete_audit():
    run = integrity_run("营业收入100万元[1]。")
    run["evidence"][0].update(content="片段100万元", snapshot_truncated=True)
    audit = evaluate_numeric_support(run, {})
    assert audit["support_audit_complete"] is False
    assert metrics_from_numeric(audit)["numeric_support_gap_rate"] is None


def test_no_numeric_claims_remain_insufficient_for_numeric_gate():
    row = audit_result({"id": "empty"}, {"answer": "资料不足"}, {})
    summary = aggregate([row])
    assert summary["unsupported_number_rate"] is None
    assert metrics_from_numeric(summary)["numeric_support_gap_rate"] is None


def test_judge_conflicts_are_separate_from_number_support():
    task = {
        "id": "conflict",
        "answer_points": [{"evidence": {"value": "100"}, "unit": "万元"}],
    }
    row = audit_result(
        task,
        integrity_run("营业收入999万元[1]。"),
        integrity_table_chunks(),
        judgment={"points": [{"index": 0, "pass": False}]},
    )
    assert row["judge_numeric_conflict_rate"] is None
    assert row["unsupported_number_rate"] == 1.0
