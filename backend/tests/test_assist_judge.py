"""辅助问答 judge：prompt 组装与归一化。全部离线。"""

from __future__ import annotations

import pytest

from evaluation import assist_judge


def _task():
    return {
        "question": "什么是商誉？",
        "key_facts": [
            {"text": "商誉是差额", "must_include_any": ["差额", "超过"]},
            {"text": "非同一控制下产生", "must_include_any": ["非同一控制"]},
        ],
        "forbidden": ["把商誉说成无形资产"],
    }


def _result(answer="商誉是差额，非同一控制下产生。"):
    return {"answer": answer, "web_sources": []}


def test_prompt_contains_question_facts_and_forbidden():
    messages = assist_judge.build_prompt(_task(), _result())
    text = "\n".join(item["content"] for item in messages)
    assert "什么是商誉" in text
    assert "商誉是差额" in text
    assert "把商誉说成无形资产" in text
    assert "只输出 JSON" in text


def test_parse_json_accepts_fenced_block():
    raw = '```json\n{"facts": [], "forbidden_hit": false}\n```'
    assert assist_judge.parse_json(raw) == {"facts": [], "forbidden_hit": False}


def test_parse_json_rejects_non_json():
    assert assist_judge.parse_json("不是 JSON") is None


def test_normalize_fills_missing_facts_as_fail():
    judgment = assist_judge.normalize_judgment(
        {"facts": [{"index": 0, "pass": True, "reason": "ok"}]},
        fact_count=2,
    )
    assert judgment["facts"][0]["pass"] is True
    assert judgment["facts"][1]["pass"] is False
    assert "judge 未返回" in judgment["facts"][1]["reason"]


@pytest.mark.asyncio
async def test_judge_task_uses_injected_call():
    async def fake_call(messages, **kwargs):
        return '{"facts":[{"index":0,"pass":true},{"index":1,"pass":true}],"forbidden_hit":false,"attribution_ok":true}', None, {}

    outcome = await assist_judge.judge_task(_task(), _result(), call=fake_call)
    assert outcome["ok"] is True
    assert all(row["pass"] for row in outcome["judgment"]["facts"])


@pytest.mark.asyncio
async def test_judge_task_reports_error_on_bad_json():
    async def fake_call(messages, **kwargs):
        return "不是 JSON", None, {}

    outcome = await assist_judge.judge_task(_task(), _result(), call=fake_call)
    assert outcome["ok"] is False
    assert "JSON" in outcome["error"]
