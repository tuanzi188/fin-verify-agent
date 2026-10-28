"""确定性检查：只判机器能判的部分。

**边界必须硬**：

| 判 | 不判 |
|---|---|
| 该调的工具调了吗、调了几次、先后对不对、有没有禁用工具 | 答案要点是否真的答到（rubric / judge） |
| 相关 chunk 有没有进过检索结果 | 所引 chunk 是否真的支撑该结论（judge） |
| 答案里的 `[n]` 能不能解析到真实段落 | 是否编造了资料外的事实（judge） |
| 每个答案要点是否被引用 | 是否该追问、追问得对不对（judge） |
| 状态是否符合 `should_abstain` | —— |
| 步数预算、注入是否触发、注入后是否继续尝试 | —— |

把"要点是否答到"塞进确定性检查，会得到一堆看起来客观、实际靠字符串匹配的假分数。
这类判断一律留给 `agent_judge`，并在报告里标明"需 judge"。

副作用为零：只读 task 与 result，不调模型、不碰数据库，方便单测与 CI 第 1 层。
"""

import re
from typing import Any, Optional

_MARKER_PATTERN = re.compile(r"\[(\d{1,2})\]")


def _tool_steps(result: dict) -> list[dict]:
    return [step for step in result.get("trace") or [] if step.get("tool") != "__final__"]


def _final_step(result: dict) -> dict:
    trace = result.get("trace") or []
    return trace[-1] if trace else {}


def _final_note(result: dict) -> str:
    return str(_final_step(result).get("note") or "")


def _check(ok: Optional[bool], detail: str = "", **extra: Any) -> dict:
    record = {"ok": ok, "detail": detail}
    record.update(extra)
    return record


def evidence_chunk_ids(result: dict) -> list[str]:
    """Agent 实际见过的 chunk，按首次出现顺序去重。

    这就是答案里 `[n]` 能取到的编号范围：runner 会把检索结果去重后编号 1..N。
    """
    seen: list[str] = []
    for step in _tool_steps(result):
        for chunk_id in step.get("result_ids") or []:
            if chunk_id and chunk_id not in seen:
                seen.append(chunk_id)
    return seen


# ---------------------------------------------------------------------------
# 工具约束
# ---------------------------------------------------------------------------

def check_tool_constraints(task: dict, result: dict) -> dict[str, dict]:
    constraints = task.get("tool_constraints") or {}
    tool_steps = _tool_steps(result)
    tool_sequence = [str(step.get("tool") or "") for step in tool_steps]
    checks: dict[str, dict] = {}

    must_include = constraints.get("must_include") or []
    missing = [name for name in must_include if name not in tool_sequence]
    checks["tool_must_include"] = _check(
        not missing if must_include else None,
        "缺失: " + ", ".join(missing) if missing else "该调的都在",
        expected=must_include,
        actual=tool_sequence,
    )

    must_not_include = constraints.get("must_not_include") or []
    violated = [name for name in must_not_include if name in tool_sequence]
    checks["tool_must_not_include"] = _check(
        not violated if must_not_include else None,
        "出现了禁用工具: " + ", ".join(violated) if violated else "未出现禁用工具",
        expected=must_not_include,
        actual=tool_sequence,
    )

    call_count = constraints.get("call_count") or {}
    count_details: dict[str, dict] = {}
    count_ok: Optional[bool] = None
    for name, bounds in call_count.items():
        actual_count = tool_sequence.count(name)
        lower, upper = (bounds or [None, None])[:2]
        ok = True
        if isinstance(lower, int) and actual_count < lower:
            ok = False
        if isinstance(upper, int) and actual_count > upper:
            ok = False
        count_details[name] = {"actual": actual_count, "expected": [lower, upper], "ok": ok}
        count_ok = ok if count_ok is None else (count_ok and ok)
    checks["tool_call_count"] = _check(
        count_ok,
        "; ".join(f"{name}={item['actual']}" for name, item in count_details.items()) or "未约束",
        detail_by_tool=count_details,
    )

    order_pairs = constraints.get("order") or []
    order_failures: list[str] = []
    for pair in order_pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        first, second = pair[0], pair[1]
        if first not in tool_sequence or second not in tool_sequence:
            order_failures.append(f"{first}->{second} 有一步没发生")
            continue
        if tool_sequence.index(first) >= tool_sequence.index(second):
            order_failures.append(f"{first} 应在 {second} 之前")
    checks["tool_order"] = _check(
        not order_failures if order_pairs else None,
        "; ".join(order_failures) if order_failures else "顺序符合" if order_pairs else "未约束顺序",
        expected=order_pairs,
    )

    return checks


# ---------------------------------------------------------------------------
# 检索命中
# ---------------------------------------------------------------------------

def check_retrieval_hit(task: dict, result: dict) -> dict:
    """相关 chunk 是否出现在 Agent 的检索结果里。

    这是"召回失败 vs Agent 决策失败"的分离点。**mock 任务返回 None**：
    注入改过检索结果，拿它算召回会把人为故障算成检索质量问题（文档 §8.7）。
    """
    relevant = list(task.get("relevant_chunk_ids") or [])
    if task.get("mock"):
        return _check(None, "mock 任务不计入召回诊断", expected=relevant)
    if not relevant:
        return _check(None, "任务没有 relevant_chunk_ids（负样本）", expected=[])

    retrieved = set(evidence_chunk_ids(result))
    hits = [chunk_id for chunk_id in relevant if chunk_id in retrieved]
    misses = [chunk_id for chunk_id in relevant if chunk_id not in retrieved]
    return _check(
        not misses,
        f"命中 {len(hits)}/{len(relevant)}" + (f"，缺 {', '.join(misses)}" if misses else ""),
        expected=relevant,
        hits=hits,
        misses=misses,
    )


# ---------------------------------------------------------------------------
# 引用
# ---------------------------------------------------------------------------

def _answer_markers(answer: str) -> list[int]:
    markers: list[int] = []
    for raw in _MARKER_PATTERN.findall(answer or ""):
        value = int(raw)
        if value not in markers:
            markers.append(value)
    return markers


def check_citations(task: dict, result: dict) -> dict[str, dict]:
    checks: dict[str, dict] = {}
    answer = str(result.get("answer") or "")
    citations = result.get("citations") or []
    evidence = evidence_chunk_ids(result)

    citation_required = bool(task.get("citation_required"))
    checks["citation_present"] = _check(
        (len(citations) > 0) if citation_required else None,
        f"引用 {len(citations)} 条" + ("（要求引用）" if citation_required else "（未要求引用）"),
    )

    markers = _answer_markers(answer)
    dangling = [marker for marker in markers if marker < 1 or marker > len(evidence)]
    checks["citation_markers_resolve"] = _check(
        not dangling,
        (
            f"悬空标记 {dangling}（答案引用了没检索到的段落编号，共见过 {len(evidence)} 段）"
            if dangling
            else f"{len(markers)} 个标记全部可解析"
        ),
        markers=markers,
        evidence_count=len(evidence),
        dangling=dangling,
    )

    cited_chunk_ids = {
        str(citation.get("chunk_id") or "") for citation in citations if citation.get("chunk_id")
    }
    points = task.get("answer_points") or []
    point_details: list[dict] = []
    for index, point in enumerate(points):
        expected = list((point or {}).get("chunk_ids") or [])
        cited = [chunk_id for chunk_id in expected if chunk_id in cited_chunk_ids]
        point_details.append(
            {"index": index, "expected": expected, "cited": cited, "ok": bool(cited)}
        )

    if points:
        all_cited = all(detail["ok"] for detail in point_details)
        uncited = [detail["index"] for detail in point_details if not detail["ok"]]
        checks["answer_points_cited"] = _check(
            all_cited,
            f"{sum(1 for d in point_details if d['ok'])}/{len(points)} 个要点被引用"
            + (f"；未引用要点 {uncited}" if uncited else ""),
            points=point_details,
        )
    else:
        checks["answer_points_cited"] = _check(None, "任务没有答案要点")

    # 引用的 chunk 必须是 Agent 真见过的：否则就是凭空造出处。
    unknown = sorted(cited_chunk_ids - set(evidence))
    checks["citation_scope"] = _check(
        not unknown if cited_chunk_ids else None,
        f"引用了未检索到的 chunk: {unknown}" if unknown else "引用全部来自检索结果",
        unknown=unknown,
    )

    return checks


# ---------------------------------------------------------------------------
# 状态与预算
# ---------------------------------------------------------------------------

def check_status(task: dict, result: dict) -> dict[str, dict]:
    checks: dict[str, dict] = {}
    status = str(result.get("status") or "")
    should_abstain = bool(task.get("should_abstain"))
    note = _final_note(result)

    checks["no_crash"] = _check(
        status != "error",
        "运行崩溃" if status == "error" else "未崩溃",
        status=status,
        note=note,
    )

    expected_status_ok = (status == "abstained") == should_abstain
    checks["abstain_status"] = _check(
        expected_status_ok,
        f"期望{'拒答' if should_abstain else '作答'}，实际 {status}",
        expected_abstain=should_abstain,
        actual_status=status,
    )

    # 正样本误拒：最伤的失败——它把"答对了"记成"没答"。
    if should_abstain:
        checks["false_refusal"] = _check(None, "负样本，不适用")
    else:
        refused = status == "abstained"
        checks["false_refusal"] = _check(
            not refused,
            "正样本被拒答" if refused else "正样本正常作答",
            status=status,
        )

    max_steps = task.get("max_steps")
    steps = int((result.get("usage") or {}).get("steps") or 0)
    budget_ok = True
    reasons: list[str] = []
    if isinstance(max_steps, int) and steps > max_steps:
        budget_ok = False
        reasons.append(f"步数 {steps} > max_steps {max_steps}")
    if "max_steps_exceeded" in note:
        budget_ok = False
        reasons.append("trace 标记 max_steps_exceeded")
    checks["step_budget"] = _check(
        budget_ok,
        "; ".join(reasons) if reasons else f"步数 {steps}",
        steps=steps,
        max_steps=max_steps,
    )

    return checks


# ---------------------------------------------------------------------------
# 注入与恢复
# ---------------------------------------------------------------------------

def check_injection(task: dict, result: dict) -> dict[str, dict]:
    checks: dict[str, dict] = {}
    planned = list(task.get("inject") or []) if task.get("mock") else []
    tool_steps = _tool_steps(result)
    fired_steps = [step for step in tool_steps if step.get("injected")]

    if not task.get("mock"):
        checks["injection_triggered"] = _check(None, "非 mock 任务")
        checks["recovery_attempted"] = _check(None, "非 mock 任务")
        return checks

    modes = [str(step.get("injected")) for step in fired_steps]
    checks["injection_triggered"] = _check(
        len(fired_steps) == len(planned),
        f"计划 {len(planned)} 次，实际触发 {len(fired_steps)} 次: {modes}",
        planned=len(planned),
        fired=len(fired_steps),
        modes=modes,
    )

    if not fired_steps:
        checks["recovery_attempted"] = _check(None, "没有触发注入，无法判定是否尝试恢复")
        return checks

    last_injected_position = max(
        position for position, step in enumerate(tool_steps) if step.get("injected")
    )
    attempts_after = len(tool_steps) - last_injected_position - 1
    checks["recovery_attempted"] = _check(
        attempts_after > 0,
        f"注入之后还有 {attempts_after} 次工具调用",
        attempts_after=attempts_after,
    )
    return checks


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def collect_checks(task: dict, result: dict) -> dict[str, dict]:
    """跑全部确定性检查，返回 name -> {ok, detail, ...} 的扁平字典。"""
    checks: dict[str, dict] = {}
    checks.update(check_tool_constraints(task, result))
    checks["retrieval_hit"] = check_retrieval_hit(task, result)
    checks.update(check_citations(task, result))
    checks.update(check_status(task, result))
    checks.update(check_injection(task, result))
    return checks


def failed_checks(checks: dict[str, dict]) -> list[str]:
    """只挑明确失败（ok is False）的，None 表示不适用。"""
    return [name for name, record in checks.items() if record.get("ok") is False]


def has_check(checks: dict[str, dict], name: str) -> bool:
    return name in checks and checks[name].get("ok") is not None
