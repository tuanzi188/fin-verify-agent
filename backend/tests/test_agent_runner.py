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


def test_extract_citations_accepts_full_width_marker_with_metadata():
    evidence = [
        {"chunk_id": "d2_ch1"},
        {"chunk_id": "d1#t1"},
    ]
    answer = "营业收入 299,927,239 千元【2，PDF 第 49 页，表 d1#t1】；住宿标准五百元 [1]。"
    assert runner._extract_citations(answer, evidence) == [
        {"marker": 2, "chunk_id": "d1#t1"},
        {"marker": 1, "chunk_id": "d2_ch1"},
    ]


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
    assert result["usage"]["llm_calls"] == 2
    assert [call["phase"] for call in result["llm_calls"]] == ["baseline_tools", "baseline_final"]
    assert result["usage"]["final_context_tokens"] == 100
    assert result["usage"]["max_messages_chars"] > 0
    assert result["trace"][0]["result_chars"] > 0
    assert result["trace"][0]["payload_chars"] > 0
    assert result["usage"]["tool_result_chars"] > 0
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


async def test_abstained_when_answer_says_period_not_included(monkeypatch):
    """期间边界常写成"不包含/不含"，不能因为没命中"无法确定"就判成 answered。"""
    _patch_llm(
        monkeypatch,
        tool_calls=[_search_call()],
        text="",
        final_answer="本地文档资料中不包含该期间的财务数据。",
    )
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run({"id": "T13", "question": "2026 年营收是多少？", "max_steps": 3})

    assert result["status"] == "abstained"


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
    # 预算收口要记进 trace（能归因），但名字不能叫 exceeded——它不是失败
    assert "budget_truncated" in result["trace"][-1]["note"]
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
    assert result["usage"]["llm_calls"] == len(result["llm_calls"])
    assert result["usage"]["llm_calls"] >= 3
    assert result["usage"]["final_context_tokens"] == 100
    assert "self_check" in [call["phase"] for call in result["llm_calls"]]
    assert result["usage"]["tool_result_chars"] > 0
    assert any(step.get("result_chars", 0) > 0 for step in result["trace"])
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


# ---------------------------------------------------------------------------
# 派生数值闸门：答案里出现"证据里没有的比率"且没算过 → 退回补一次 calculate
# ---------------------------------------------------------------------------

def test_needs_calc_repair_only_for_numbers_missing_from_evidence():
    evidence = [{"content": "营业收入（千元） 1,234 占比 3.90%"}]
    # 原文里写着的比率 = 照抄，不是手算
    assert runner.needs_calc_repair("占比 3.90% [1]", evidence, []) is False
    # 原文里没有 + 一次没算过 → 要补
    assert runner.needs_calc_repair("第四季度占全年约 35.37%", evidence, []) is True
    # 算过了就不再追问
    assert runner.needs_calc_repair("占比 35.37%", evidence, [{"formula": "a/b"}]) is False
    # 没有比率就不用算
    assert runner.needs_calc_repair("营业收入 1,234 千元", evidence, []) is False


class _CalcToolset:
    """最小金融形状工具集：只提供 calculate，并像 FinanceToolset 一样记 calcs。"""

    name = "fake-finance"
    search_tool = "search_kb"
    max_tool_rounds = 3
    final_template = "证据：\n{evidence}\n请回答。"

    def __init__(self):
        self.calcs: list[dict] = []
        self.calls = 0

    def specs(self):
        return [{"type": "function", "function": {"name": "calculate", "parameters": {}}}]

    def system_prompt(self, variant):
        return "system"

    async def call(self, name, arguments):
        from app.tools.toolset import ToolOutcome

        self.calls += 1
        if name == "calculate":
            data = {"formula": "a / b * 100", "inputs": {"a": 1, "b": 3}, "result": "35.37"}
            self.calcs.append(dict(data))
            return ToolOutcome(tool=name, ok=True, data=data)
        return ToolOutcome(tool=name, ok=True, data=[])

    def to_evidence(self, name, data):
        return []


async def test_calc_repaired_once_when_answer_computes_without_calculate(monkeypatch):
    toolset = _CalcToolset()
    _patch_improved_llm(
        monkeypatch,
        rounds=[
            # 第一轮：直接给答案，比率是它自己算的，没有 calculate
            ("第四季度占全年约 35.37%。", []),
            # 被闸门退回后：调 calculate
            ("", [{"id": "c1", "name": "calculate",
                   "arguments": {"expression": "a / b * 100", "inputs": {"a": 1, "b": 3}}}]),
            # 补算完给最终答案
            ("占比 35.37%（a / b * 100）[1]。", []),
        ],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="占比 35.37% [1]。",
    )

    result = await run(_TASK, variant="improved", toolset=toolset)

    assert "calc_repair" in result["trace"][-1]["note"]
    assert len(toolset.calcs) == 1
    assert any(step["tool"] == "calculate" for step in result["trace"])
    # 只补一次：补完不再追问
    assert result["trace"][-1]["note"].count("calc_repair") == 1


async def test_no_calc_repair_when_answer_has_no_derived_numbers(monkeypatch):
    toolset = _CalcToolset()
    _patch_improved_llm(
        monkeypatch,
        rounds=[("营业收入为 1,234 千元 [1]。", [])],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="营业收入为 1,234 千元 [1]。",
    )

    result = await run(_TASK, variant="improved", toolset=toolset)

    assert toolset.calcs == []
    assert "calc_repair" not in result["trace"][-1]["note"]


async def test_post_loop_calc_repair_when_rounds_are_exhausted(monkeypatch):
    """回合用完才出答案也要补算：FIN-08/09 就是走这条路，不是走回合内的答案分支。"""
    toolset = _CalcToolset()
    _patch_dispatch(monkeypatch, [_evidence_item()])
    _patch_improved_llm(
        monkeypatch,
        rounds=[
            ("", [_search_call("c1", "净利率")]),
            ("", [_search_call("c2", "营业收入")]),
            ("", [_search_call("c3", "净利润")]),
            ("", [_search_call("c4", "归母净利润")]),
            # 第 5 次是回合结束后的补算（max_tool_rounds=3 + 1 收口轮 = 4 轮搜索）
            ("", [{"id": "c9", "name": "calculate",
                   "arguments": {"expression": "a / b * 100", "inputs": {"a": 1, "b": 3}}}]),
        ],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="归母净利率约 4.06%，环比下降 1.12 个百分点。",
    )

    # _TASK 的 max_steps 是 3，三轮搜索就用完了；补算需要预算，所以这里放宽到 6
    result = await run({**_TASK, "max_steps": 6}, variant="improved", toolset=toolset)

    assert "calc_repair" in result["trace"][-1]["note"]
    assert len(toolset.calcs) == 1
    assert any(step["tool"] == "calculate" for step in result["trace"])


def test_round_message_only_repeats_new_evidence():
    """多轮回填只贴新增证据：旧正文已在上文，重贴等于按轮次重复计费。"""
    old = {"chunk_id": "d1#t1", "title": "旧证据", "content": "旧正文-不应再出现"}
    new = {"chunk_id": "d1#t2", "title": "新证据", "content": "新正文-本轮新增"}
    message = runner._round_message(
        [old, new],
        [{"tool": "get_table", "ok": True, "result_count": 1, "new_result_count": 1}],
        [],
        "search_kb",
        new_from=1,
    )
    assert "新正文-本轮新增" in message
    assert "旧正文-不应再出现" not in message
    assert "[1] d1#t1" in message  # 旧编号仍要给索引，否则模型无法引用
    assert "[2]" in message


def test_round_message_without_new_evidence_says_so():
    old = {"chunk_id": "d1#t1", "title": "旧证据", "content": "旧正文"}
    message = runner._round_message(
        [old],
        [{"tool": "get_table", "ok": True, "result_count": 1, "new_result_count": 0}],
        [],
        "search_kb",
        new_from=1,
    )
    assert "本轮没有新增原文" in message
    assert "旧正文" not in message


# ---------------------------------------------------------------------------
# 回答闸门：拒答数值泄漏 / 单元格引用 / 跨文档覆盖
# ---------------------------------------------------------------------------

def test_numeric_leaks_ignore_years_pages_and_ids():
    text = (
        "2026 年第一季度没有数据，PDF 第 11 页只有 2025 年四个季度，"
        "表 t0006 和 cninfo-000333-2025-annual 都没列。"
    )
    assert runner._numeric_leaks(text) == []

    text = "无法确定。资料中只有 2025 年第一季度的 127,838,538 千元（约 1,278.39 亿元）。"
    leaks = runner._numeric_leaks(text)
    assert "127,838,538" in leaks
    assert any("1,278.39" in leak for leak in leaks)


def test_needs_cell_repair_only_when_table_cited_with_number():
    table = {"kind": "table", "chunk_id": "d#t1"}
    cell = {"kind": "cell", "chunk_id": "d#t1#0#2025 年"}
    assert runner.needs_cell_repair("营业收入 1,234 万元 [1]", [table]) is True
    assert runner.needs_cell_repair("营业收入 1,234 万元 [1]", [cell]) is False
    assert runner.needs_cell_repair("表里有相关数据 [1]", [table]) is False
    assert runner.needs_cell_repair("营业收入 1,234 万元", [table]) is False


class _GateToolset:
    """最小金融形状工具集：可控 coverage_gaps、表/单元格证据。"""

    name = "fake-finance"
    search_tool = "search_sections"
    max_tool_rounds = 3
    final_template = "证据：\n{evidence}\n请回答。"

    def __init__(self, *, missing_doc: str = "", unsupported: list[str] | None = None):
        self.calcs: list[dict] = []
        self.missing_doc = missing_doc
        self.unsupported = unsupported or []
        self.calls: list[str] = []

    def specs(self):
        return []

    def system_prompt(self, variant):
        return "system"

    async def call(self, name, arguments):
        from app.tools.toolset import ToolOutcome

        self.calls.append(name)
        if name == "read_section":
            return ToolOutcome(
                tool=name,
                ok=True,
                data={"section_id": "d2#s1", "text": "2024 年数据", "page_start": 1},
                result_ids=["d2#s1"],
                result_count=1,
            )
        if name == "get_table":
            return ToolOutcome(
                tool=name,
                ok=True,
                data={
                    "table_id": "d1#t1",
                    "rows": [["营业收入", "1,234"]],
                    "columns": ["项目", "2025 年"],
                    "page": 12,
                    "unit": "万元",
                },
                result_ids=["d1#t1"],
                result_count=1,
            )
        if name == "get_cell":
            return ToolOutcome(
                tool=name,
                ok=True,
                data={
                    "table_id": "d1#t1",
                    "row": 0,
                    "row_name": "营业收入",
                    "column": "2025 年",
                    "cell_text": "1,234",
                    "page": 12,
                    "unit": "万元",
                    "table_caption": "主要会计数据",
                },
                result_ids=["d1#t1#0#2025 年"],
                result_count=1,
            )
        return ToolOutcome(tool=name, ok=True, data=[])

    def to_evidence(self, name, data):
        from app.tools.toolset import evidence_item

        if name == "read_section":
            return [
                evidence_item(
                    chunk_id="d2#s1",
                    title="2024 年报",
                    content="2024 年数据",
                    kind="section",
                    page=1,
                    doc_id="d2",
                )
            ]
        if name == "get_table":
            return [
                evidence_item(
                    chunk_id="d1#t1",
                    title="主要会计数据",
                    content="表 d1#t1｜单位：万元\n项目 | 2025 年\n营业收入 | 1,234",
                    kind="table",
                    page=12,
                    table_caption="主要会计数据",
                    unit="万元",
                    doc_id="d1",
                )
            ]
        if name == "get_cell":
            return [
                evidence_item(
                    chunk_id="d1#t1#0#2025 年",
                    title="主要会计数据",
                    content="营业收入 的「2025 年」= 1,234（单位：万元）",
                    kind="cell",
                    page=12,
                    table_id="d1#t1",
                    row_name="营业收入",
                    column="2025 年",
                    cell_text="1,234",
                    unit="万元",
                    doc_id="d1",
                )
            ]
        return []

    def coverage_gaps(self, question, evidence):
        if not self.missing_doc:
            return []
        doc_ids = {str(item.get("doc_id") or "") for item in evidence}
        return [] if self.missing_doc in doc_ids else [f"缺失文档 {self.missing_doc}"]

    def unsupported_periods(self, question):
        return list(self.unsupported)


async def test_coverage_gate_reads_missing_document_before_answering(monkeypatch):
    toolset = _GateToolset(missing_doc="d2")
    _patch_improved_llm(
        monkeypatch,
        rounds=[
            ("无法确定 2024 年数据 [1]。", []),
            ("", [{"id": "c1", "name": "read_section", "arguments": {"section_id": "d2#s1"}}]),
            ("2024 年数据为 1,234 万元 [2]。", []),
        ],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="",
    )

    result = await run(_TASK, variant="improved", toolset=toolset)

    assert "coverage_repair" in result["trace"][-1]["note"]
    assert any(step["tool"] == "read_section" for step in result["trace"])
    assert any(item.get("doc_id") == "d2" for item in result["evidence"])
    assert "coverage_unresolved" not in result["trace"][-1]["note"]


async def test_cell_gate_adds_get_cell_for_table_only_citation(monkeypatch):
    toolset = _GateToolset()
    _patch_improved_llm(
        monkeypatch,
        rounds=[
            ("", [{"id": "c1", "name": "get_table", "arguments": {"table_id": "d1#t1"}}]),
            ("营业收入 1,234 万元 [1]。", []),
            ("", [{"id": "c2", "name": "get_cell",
                   "arguments": {"table_id": "d1#t1", "row": "营业收入", "column": "2025 年"}}]),
            ("营业收入 1,234 万元 [2]。", []),
        ],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="",
    )

    result = await run(_TASK, variant="improved", toolset=toolset)

    assert "cell_repair" in result["trace"][-1]["note"]
    assert any(step["tool"] == "get_cell" for step in result["trace"])
    assert result["citations"] == [{"marker": 2, "chunk_id": "d1#t1#0#2025 年"}]
    assert "cell_citation_missing" not in result["trace"][-1]["note"]


async def test_unsupported_period_gate_forces_clean_refusal(monkeypatch):
    """2026 Q1 这类语料里没有的期间：直接拒答，不许拿 2025 的数字顶上。"""
    toolset = _GateToolset(unsupported=["2026 年（本地语料最新到 2025 年）"])
    _patch_improved_llm(
        monkeypatch,
        rounds=[
            ("美的集团 2026 年 Q1 营业收入约为 1,278.39 亿元 [1]。", []),
            (
                (
                    "根据现有资料无法确定美的集团 2026 年第一季度的营业收入。"
                    "本地资料最新到 2025 年，不含 2026 年第一季度。"
                ),
                [],
            ),
        ],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="",
    )

    result = await run(_TASK, variant="improved", toolset=toolset)

    assert "unsupported_period" in result["trace"][-1]["note"]
    assert "1,278.39" not in result["answer"]
    assert "abstain_numeric_leak" not in result["trace"][-1]["note"]


async def test_abstain_gate_rewrites_adjacent_period_numbers(monkeypatch):
    toolset = _GateToolset()
    _patch_improved_llm(
        monkeypatch,
        rounds=[
            (
                (
                    "根据现有资料无法确定 2026 年第一季度的营业收入。"
                    "资料中只有 2025 年第一季度的 127,838,538 千元。"
                ),
                [],
            ),
            (
                (
                    "根据现有资料无法确定 2026 年第一季度的营业收入。"
                    "资料只覆盖 2025 年，不含 2026 年第一季度。"
                ),
                [],
            ),
        ],
        coverage_reply='{"covered": true, "missing": [], "next_query": ""}',
        final_reply="",
    )

    result = await run(_TASK, variant="improved", toolset=toolset)

    assert "abstain_rewrite" in result["trace"][-1]["note"]
    assert "127,838,538" not in result["answer"]
    assert "abstain_numeric_leak" not in result["trace"][-1]["note"]


async def test_forced_final_round_then_repair_round_allows_tools(monkeypatch):
    """最后一轮强制收口；闸门批下的补救轮必须重新允许工具。

    2026-10-29 冒烟踩过：补救轮仍然带 tool_choice=none，模型只能写
    "我无法完成第 1 步：本轮没有可用的 get_cell 返回结果"。
    """
    toolset = _GateToolset()
    choices: list[str | None] = []
    state = {"calls": 0}
    rounds = [
        ("", [{"id": "c1", "name": "get_table", "arguments": {"table_id": "d1#t1"}}]),
        ("", [{"id": "c2", "name": "get_table", "arguments": {"table_id": "d1#t1"}}]),
        ("", [{"id": "c3", "name": "get_table", "arguments": {"table_id": "d1#t1"}}]),
        ("营业收入 1,234 万元 [1]。", []),
        ("", [{"id": "c4", "name": "get_cell",
               "arguments": {"table_id": "d1#t1", "row": "营业收入", "column": "2025 年"}}]),
        ("营业收入 1,234 万元 [2]。", []),
    ]

    async def fake_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                         temperature=0.1, max_tokens=None):
        choices.append(tool_choice)
        index = min(state["calls"], len(rounds) - 1)
        state["calls"] += 1
        text, calls = rounds[index]
        return text, calls, None, _usage()

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        return '{"covered": true, "missing": [], "next_query": ""}', None, _usage()

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)

    result = await run({**_TASK, "max_steps": 10}, variant="improved", toolset=toolset)

    # 第 4 轮是 base 的最后一轮（3 轮工具 + 1 轮收口）→ 强制收口
    assert choices[3] == "none"
    # 闸门批下的补救轮 → 必须允许工具
    assert choices[4] is None
    # 补救完再收口 → 再次强制
    assert choices[5] == "none"
    assert "cell_repair" in result["trace"][-1]["note"]
    assert result["citations"] == [{"marker": 2, "chunk_id": "d1#t1#0#2025 年"}]


async def test_answer_truncated_note_when_final_hits_max_tokens(monkeypatch):
    """答案非空但终轮 finish_reason=length：必须留痕（2026-11-03 FIN-09 半句截断）。"""

    async def fake_chat_with_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                                   temperature=0.1, max_tokens=None):
        return "", [_search_call()], None, _usage()

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        return (
            "住宿标准为每晚不超过五百元 [1]，一线",
            None,
            {
                "prompt_tokens": 100,
                "completion_tokens": 4096,
                "finish_reason": "length",
                "has_reasoning": True,
            },
        )

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_chat_with_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)
    _patch_dispatch(monkeypatch, [_evidence_item()])

    result = await run(_TASK)

    assert "answer_truncated" in result["trace"][-1]["note"]
    # 正常收尾（finish_reason 缺失或 stop）不得误标
    assert result["status"] == "answered"
