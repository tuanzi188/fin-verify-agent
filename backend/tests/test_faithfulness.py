"""RAGAS 忠实度测试：拆陈述、判推断、失败降级、空回答。"""

import pytest

from evaluation.faithfulness import (
    aggregate,
    build_context,
    judge_claims,
    score_result,
    split_claims,
    split_claims_fallback,
)


def test_split_claims_fallback_splits_sentences():
    claims = split_claims_fallback("营业收入 456,451,731 千元。同比增长 12.11%。")
    assert claims == ["营业收入 456,451,731 千元。", "同比增长 12.11%。"]


def test_build_context_uses_evidence_content():
    result = {"evidence": [{"chunk_id": "doc#t1", "content": "营业收入 100 元"}]}
    context = build_context(result)
    assert "doc#t1" in context
    assert "营业收入 100 元" in context


@pytest.mark.asyncio
async def test_split_claims_parses_json():
    async def fake_call(prompt, system, max_tokens):
        assert "原子陈述" in prompt
        return '{"claims": ["营业收入 100 元", "同比增长 10%"]}', None, {"prompt_tokens": 1}

    row = await split_claims("营业收入 100 元，同比增长 10%", call=fake_call)
    assert row["claims"] == ["营业收入 100 元", "同比增长 10%"]
    assert row["fallback"] is False


@pytest.mark.asyncio
async def test_split_claims_falls_back_on_bad_json():
    async def fake_call(prompt, system, max_tokens):
        return "not json", None, {}

    row = await split_claims("营业收入 100 元。同比增长 10%。", call=fake_call)
    assert row["fallback"] is True
    assert len(row["claims"]) == 2


@pytest.mark.asyncio
async def test_judge_claims_counts_supported():
    async def fake_call(prompt, system, max_tokens):
        return (
            '{"claims": [{"index": 0, "supported": true, "reason": "原文有"},'
            ' {"index": 1, "supported": false, "reason": "原文没有"}]}',
            None,
            {"prompt_tokens": 1},
        )

    row = await judge_claims(["A", "B"], "context", call=fake_call)
    assert row["supported"] == 1
    assert row["total"] == 2


@pytest.mark.asyncio
async def test_score_result_returns_faithfulness():
    async def fake_call(prompt, system, max_tokens):
        if "【上下文】" in prompt:
            return (
                '{"claims": [{"index": 0, "supported": true}, {"index": 1, "supported": false}]}',
                None,
                {},
            )
        return '{"claims": ["A", "B"]}', None, {}

    result = {
        "task_id": "T",
        "run_index": 1,
        "answer": "A。B。",
        "evidence": [{"chunk_id": "doc#t1", "content": "A"}],
    }
    row = await score_result({"id": "T"}, result, call=fake_call)
    assert row["faithfulness"] == 0.5
    assert row["supported"] == 1
    assert row["total"] == 2


@pytest.mark.asyncio
async def test_empty_answer_is_not_applicable():
    row = await score_result({"id": "T"}, {"task_id": "T", "answer": "", "evidence": []})
    assert row["faithfulness"] is None
    assert row["note"] == "空回答，忠实度不适用"


def test_aggregate_micro_and_macro():
    rows = [
        {"faithfulness": 1.0, "supported": 2, "total": 2, "errors": []},
        {"faithfulness": 0.5, "supported": 1, "total": 2, "errors": []},
    ]
    summary = aggregate(rows)
    assert summary["faithfulness_mean"] == 0.75
    assert summary["faithfulness_micro"] == 0.75
    assert summary["model_calls"] == 4
