"""Agent runner 测试：状态判定、trace 结构、预算、错误落库。

全部离线：LLM 与工具都用 monkeypatch 替身，不打网络。
"""

import pytest

from app.agent import runner
from app.agent.runner import run
from app.services.openai_client import parse_tool_calls
from app.tools.contract import validate_result

_TASK = {"id": "T01", "question": "出差住宿标准每晚多少钱？", "max_steps": 3, "clarify_mode": "none"}


def _usage(prompt_tokens: int = 100, completion_tokens: int = 20) -> dict:
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}


def _search_call(call_id: str = "c1", query: str = "出差住宿标准") -> dict:
    return {"id": call_id, "name": "search_kb", "arguments": {"query": query}, "parse_error": None}


def _evidence_item() -> dict:
    return {
        "chunk_id": "d2_ch1",
        "title": "差旅与报销制度",
        "document_id": "d2",
        "content": "住宿标准为每晚不超过五百元，一线城市可上浮百分之二十。",
        "score": 1.2,
        "similarity": 0.81,
        "rrf_score": 0.0164,
    }


def _patch_llm(monkeypatch, *, tool_calls, text, final_answer=None):
    async def fake_chat_with_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                                   temperature=0.1, max_tokens=None):
        return text, tool_calls, None, _usage()

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        return final_answer if final_answer is not None else text, None, _usage()

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_chat_with_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)


def _patch_dispatch(monkeypatch, payload):
    async def fake_dispatch(name, arguments, user_context=None):
        return payload

    monkeypatch.setattr("app.tools.mock.dispatch", fake_dispatch)


async def test_answered_with_citation_and_valid_trace(monkeypatch):
    _patch_llm(
        monkeypatch,
        tool_calls=[_search_call()],
        text="",
        final_answer="住宿标准为每晚不超过五百元 [1]，一线城市可上浮百分之二十 [1]。",
    )
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run(_TASK)

    assert result["status"] == "answered"
    assert result["citations"] == [{"marker": 1, "chunk_id": "d2_ch1"}]
    assert result["usage"]["steps"] == 1
    assert result["trace"][0]["tool"] == "search_kb"
    assert result["trace"][0]["result_ids"] == ["d2_ch1"]
    assert result["trace"][-1]["tool"] == "__final__"
    assert result["trace"][-1]["self_eval"] is None
    assert validate_result(result) == []


async def test_abstained_when_evidence_empty(monkeypatch):
    _patch_llm(
        monkeypatch,
        tool_calls=[_search_call(query="年假天数")],
        text="",
        final_answer="根据现有资料无法确定。",
    )
    _patch_dispatch(monkeypatch, [])

    result = await run({"id": "T02", "question": "员工年假有多少天？", "max_steps": 3})

    assert result["status"] == "abstained"
    assert result["citations"] == []
    assert validate_result(result) == []


async def test_no_tool_call_is_recorded_as_note(monkeypatch):
    _patch_llm(
        monkeypatch,
        tool_calls=[],
        text="你好，我可以帮你查公司制度。",
    )

    result = await run({"id": "T03", "question": "你好", "max_steps": 3})

    assert result["status"] == "answered"
    assert result["usage"]["steps"] == 0
    assert "no_tool_call" in result["trace"][-1]["note"]


async def test_clarify_mode_ask_marks_clarified(monkeypatch):
    _patch_llm(
        monkeypatch,
        tool_calls=[],
        text="你说的报销是哪一类？差旅还是招待？",
    )

    result = await run({"id": "T04", "question": "报销怎么走？", "clarify_mode": "ask", "max_steps": 3})

    assert result["status"] == "clarified"


async def test_same_answer_without_clarify_mode_is_answered(monkeypatch):
    """反向对照：不该问的任务里反问，不能算澄清成功。"""
    _patch_llm(
        monkeypatch,
        tool_calls=[],
        text="你说的报销是哪一类？差旅还是招待？",
    )

    result = await run({"id": "T05", "question": "报销怎么走？", "clarify_mode": "none", "max_steps": 3})

    assert result["status"] == "answered"


async def test_answered_with_trailing_caveat_is_not_misread_as_abstain(monkeypatch):
    """真实翻车案例：答案正确、结尾补了"资料中没有……"，不能整条判成拒答。

    2026-10-27 冒烟实测：S01 第 2 次运行答案完整覆盖两个要点，只因末句写了
    "因此这两点根据现有资料无法确定" 被整句关键词匹配判成 abstained。
    正样本误拒会把"答对了"记成"没答"，比漏拒更伤拒答维度。
    """
    _patch_llm(
        monkeypatch,
        tool_calls=[_search_call()],
        text="",
        final_answer=(
            "住宿标准为每晚不超过五百元 [1]。一线城市可上浮百分之二十 [1]。"
            "资料中没有给出一线城市上浮后的具体金额，因此这点根据现有资料无法确定。"
        ),
    )
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run(_TASK)

    assert result["status"] == "answered"


async def test_ask_task_conditional_opening_is_not_abstain(monkeypatch):
    """ask 任务里“取决于…无法确定”是分情形作答的开场，不是拒答。

    2026-10-27 实测：C3 这样开头的答案被判 abstained，false_refusal 门禁误红。
    """
    _patch_llm(
        monkeypatch,
        tool_calls=[_search_call()],
        text="",
        final_answer=(
            "结论：取决于你走的哪套制度，现有资料无法确定该走哪条。"
            "若是居家办公，当日向直属主管报备即可 [1]。"
            "若是例行远程办公，需提前一天在 OA 申请 [1]。"
        ),
    )
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run({
        "id": "T07", "question": "明天要在家办公，现在报备来得及吗？",
        "clarify_mode": "ask", "max_steps": 3,
    })

    assert result["status"] == "answered"


async def test_none_task_same_conditional_opening_stays_abstain(monkeypatch):
    """反向对照：不该分情形作答的任务里，这样开头仍按拒答处理。"""
    _patch_llm(
        monkeypatch,
        tool_calls=[_search_call()],
        text="",
        final_answer=(
            "结论：取决于你走的哪套制度，现有资料无法确定该走哪条。"
            "若是居家办公，当日向直属主管报备即可 [1]。"
        ),
    )
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run({"id": "T08", "question": "住宿标准是多少？", "clarify_mode": "none"})

    assert result["status"] == "abstained"


async def test_markdown_heading_before_refusal_is_abstained(monkeypatch):
    """`## 结论` 单独一行时，后面的拒答不能被换行截断掉。"""
    _patch_llm(
        monkeypatch,
        tool_calls=[_search_call()],
        text="",
        final_answer="## 结论\n\n根据现有资料无法确定病假期间的工资发放比例。",
    )
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run({"id": "T11", "question": "病假工资怎么发？", "max_steps": 3})

    assert result["status"] == "abstained"


async def test_markdown_heading_with_content_keeps_refusal(monkeypatch):
    _patch_llm(
        monkeypatch,
        tool_calls=[_search_call()],
        text="",
        final_answer="## 结论：根据现有资料无法确定病假工资比例。",
    )
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run({"id": "T12", "question": "病假工资怎么发？", "max_steps": 3})

    assert result["status"] == "abstained"


async def test_clarify_field_records_mode_and_keyword_signal(monkeypatch):
    """clarify.signal 只是关键词线索，字段名要保证没人把它当判定用。"""
    _patch_llm(monkeypatch, tool_calls=[], text="请告诉我你想问的是哪一年度？")

    result = await run({"id": "T08", "question": "体检额度多少？", "clarify_mode": "ask"})

    assert result["trace"][-1]["clarify"] == {"mode": "ask", "signal": True}


async def test_clarify_signal_false_when_no_keywords(monkeypatch):
    _patch_llm(monkeypatch, tool_calls=[], text="报销需要增值税发票，抬头写公司全称。")

    result = await run({"id": "T09", "question": "报销要什么材料？", "clarify_mode": "none"})

    assert result["trace"][-1]["clarify"] == {"mode": "none", "signal": False}


async def test_budget_exceeded_is_attributable(monkeypatch):
    calls = [
        _search_call("c1", "住宿标准"),
        _search_call("c2", "报销流程"),
        _search_call("c3", "审批权限"),
    ]
    _patch_llm(monkeypatch, tool_calls=calls, text="", final_answer="无法确定。")
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run({"id": "T06", "question": "住宿和报销分别是什么？", "max_steps": 1})

    assert result["usage"]["steps"] == 1
    assert "max_steps_exceeded" in result["trace"][-1]["note"]
    assert validate_result(result) == []


async def test_llm_error_lands_as_failed_not_lost(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", boom)

    result = await run(_TASK)

    assert result["status"] == "error"
    assert result["answer"] == ""
    assert "run_error" in result["trace"][-1]["note"]
    assert result["trace"][-1]["ok"] is False
    assert validate_result(result) == []


async def test_tool_parse_error_becomes_bad_args_step(monkeypatch):
    _patch_llm(
        monkeypatch,
        tool_calls=[{
            "id": "c1",
            "name": "search_kb",
            "arguments": {},
            "parse_error": "JSONDecodeError: Expecting value",
        }],
        text="",
        final_answer="根据现有资料无法确定。",
    )
    # 工具不该被调用
    async def should_not_be_called(name, arguments):
        raise AssertionError("parse_error 时不应调用真实工具")

    monkeypatch.setattr("app.tools.mock.dispatch", should_not_be_called)

    result = await run(_TASK)

    assert result["trace"][0]["error_kind"] == "bad_args"
    assert result["status"] == "abstained"


async def test_retrieval_cache_cleared_once_per_run(monkeypatch):
    cleared: list[int] = []
    monkeypatch.setattr(
        "app.services.hybrid_search.clear_retrieval_cache",
        lambda: cleared.append(1),
    )
    _patch_llm(monkeypatch, tool_calls=[_search_call()], text="", final_answer="无法确定。")
    _patch_dispatch(monkeypatch, [_evidence_item()])

    await run(_TASK)
    await run(_TASK)

    assert len(cleared) == 2


def _patch_improved_llm(monkeypatch, rounds, coverage_reply, final_reply):
    """improved 的离线替身：按顺序返回工具回合，chat 区分自检与终稿。"""
    state = {"tool": 0, "chat": 0}

    async def fake_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                         temperature=0.1, max_tokens=None):
        index = min(state["tool"], len(rounds) - 1)
        state["tool"] += 1
        text, tool_calls = rounds[index]
        return text, tool_calls, None, _usage()

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        state["chat"] += 1
        prompt = str((messages[-1] or {}).get("content") or "")
        if "覆盖" in prompt:
            return coverage_reply, None, _usage()
        return final_reply, None, _usage()

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)
    return state


async def test_improved_is_a_supported_variant(monkeypatch):
    _patch_improved_llm(
        monkeypatch,
        rounds=[("住宿标准为每晚不超过五百元 [1]。", [])],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="",
    )
    result = await run(_TASK, variant="improved")
    assert result["variant"] == "improved"


async def test_improved_auto_retries_after_empty_search(monkeypatch):
    """空结果必须自动改写 query 补一次，而不是直接拒答。"""
    payloads = [[], [_evidence_item()]]
    calls = {"dispatch": 0}

    async def fake_dispatch(name, arguments, user_context=None):
        index = min(calls["dispatch"], len(payloads) - 1)
        calls["dispatch"] += 1
        return payloads[index]

    monkeypatch.setattr("app.tools.mock.dispatch", fake_dispatch)
    _patch_improved_llm(
        monkeypatch,
        rounds=[("", [_search_call(query="住宿标准")]), ("", [])],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="住宿标准为每晚不超过五百元 [1]。",
    )

    result = await run(_TASK, variant="improved")

    assert result["usage"]["steps"] == 2
    assert result["usage"]["auto_retries"] == 1
    assert result["trace"][1]["policy"] == "auto_retry"
    assert result["trace"][1]["result_ids"] == ["d2_ch1"]
    assert result["status"] == "answered"
    assert validate_result(result) == []


async def test_improved_self_check_supplements_and_rewrites(monkeypatch):
    """覆盖自检发现缺子问题后，要补检索再重写答案，而不是把初稿直接交出去。"""
    second = {
        "chunk_id": "d3_ch2",
        "title": "居家办公制度",
        "document_id": "d3",
        "content": "居家办公需提前一天在 OA 申请。",
        "score": 1.0,
        "similarity": 0.77,
        "rrf_score": 0.014,
    }
    payloads = [[_evidence_item()], [second]]
    calls = {"dispatch": 0}

    async def fake_dispatch(name, arguments, user_context=None):
        index = min(calls["dispatch"], len(payloads) - 1)
        calls["dispatch"] += 1
        return payloads[index]

    monkeypatch.setattr("app.tools.mock.dispatch", fake_dispatch)
    _patch_improved_llm(
        monkeypatch,
        rounds=[("", [_search_call()]), ("初稿只答了一半 [1]。", [])],
        coverage_reply='{"covered": false, "missing": ["第二跳"], "next_query": "居家办公 报备"}',
        final_reply="完整答案：住宿标准五百元 [1]；居家办公需提前一天申请 [2]。",
    )

    result = await run(_TASK, variant="improved")

    assert any(step.get("policy") == "self_check_supplement" for step in result["trace"])
    assert result["trace"][-1]["self_eval"]["supplemented"] is True
    assert "完整答案" in result["answer"]
    assert result["usage"]["self_checks"] == 1


async def test_improved_budget_exhaustion_is_not_marked_as_exceeded(monkeypatch):
    """步数用尽 ≠ 超限：用满 max_steps 不能记 max_steps_exceeded。

    2026-10-27 实测：旧实现把 43 次"刚好用满预算"记成超限，
    确定性检查据此把 32 条运行判成失败（含 16 条其实答对了的拒答题）。
    """
    _patch_improved_llm(
        monkeypatch,
        rounds=[("", [_search_call()])],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="住宿标准为每晚不超过五百元 [1]。",
    )
    _patch_dispatch(monkeypatch, [_evidence_item()])

    task = {"id": "T10", "question": "住宿标准？", "max_steps": 1, "citation_required": True}
    result = await run(task, variant="improved")

    note = str(result["trace"][-1].get("note") or "")
    assert "max_steps_exceeded" not in note
    assert result["usage"]["steps"] == 1


async def test_improved_clarify_task_skips_self_check(monkeypatch):
    """澄清任务该问就问，不能被覆盖自检逼成硬答。"""
    state = _patch_improved_llm(
        monkeypatch,
        rounds=[("请确认你要问的是哪一年度的制度？", [])],
        coverage_reply="{}",
        final_reply="",
    )

    result = await run(
        {"id": "T07", "question": "报销怎么走？", "clarify_mode": "ask", "max_steps": 3},
        variant="improved",
    )

    assert result["status"] == "clarified"
    assert result["trace"][-1]["self_eval"] is None
    assert state["chat"] == 0


def test_parse_tool_calls_bad_json_does_not_raise():
    class _Function:
        name = "search_kb"
        arguments = "{not valid json"

    class _Call:
        id = "c1"
        function = _Function()

    class _Message:
        tool_calls = [_Call()]

    parsed = parse_tool_calls(_Message())

    assert parsed[0]["name"] == "search_kb"
    assert parsed[0]["arguments"] == {}
    assert parsed[0]["parse_error"]


def test_parse_tool_calls_good_json():
    class _Function:
        name = "get_citation"
        arguments = '{"chunk_id": "d2_ch1"}'

    class _Call:
        id = "c2"
        function = _Function()

    class _Message:
        tool_calls = [_Call()]

    parsed = parse_tool_calls(_Message())

    assert parsed[0]["arguments"] == {"chunk_id": "d2_ch1"}
    assert parsed[0]["parse_error"] is None


async def test_runner_pins_explicit_temperature(monkeypatch):
    """温度必须显式传给客户端：靠客户端默认值会让报告无法被别人复现。"""
    seen: dict[str, float] = {}

    async def fake_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                         temperature=0.1, max_tokens=None):
        seen["tool_round"] = temperature
        return "", [_search_call()], None, _usage()

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        seen["final"] = temperature
        return "住宿标准为每晚不超过五百元 [1]。", None, _usage()

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)
    _patch_dispatch(monkeypatch, [_evidence_item()])

    await run(_TASK)

    assert seen["tool_round"] == runner.TOOL_TEMPERATURE
    assert seen["final"] == runner.TOOL_TEMPERATURE


def test_runner_does_not_import_evaluation():
    """边界：被测对象不能 import 评测层，否则"评测改变行为"就成了自我实现。"""
    import inspect

    source = inspect.getsource(runner)
    assert "from evaluation" not in source
    assert "import evaluation" not in source
