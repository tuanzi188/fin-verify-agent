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

from evaluation.equivalence import cell_equivalence

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


def base_chunk_id(chunk_id: str) -> str:
    """把"定位锚点"折回它所属的块。

    金融工具里 `get_cell` 的锚点是 `<doc>#<table>#<row>#<column>`：
    它比表更精确，但**任务卡声明的是表**。不折回就会把"读了表里的正确单元格"
    判成"没命中/没引用"——2026-10-28 摸底 24 条里，这一条造成了 7 条假失败。

    折叠规则只看段数：`a#b#c#d` → `a#b`；段数 <3 的原样返回（制度库的 `d2_ch1` 不受影响）。
    """
    text = str(chunk_id or "")
    parts = text.split("#")
    if len(parts) >= 3:
        return "#".join(parts[:2])
    return text


# ---------------------------------------------------------------------------
# 工具约束
# ---------------------------------------------------------------------------

# 产品默认检索入口已从 sections 切到 blocks，后又加入 search_tables（表目录检索）。
# 三个工具在 FinanceToolset 里共用同一份检索预算（finance_set.py 的 _search_budget），
# 约束检查只关心「有没有检索」，视为同一个动作，避免把正确运行判成没检索。
# 2026-11-04 实测：漏折 search_tables 让 FIN-11 / FIN-S06 / FIN-S10 的
# tool_call_count 全部误判（实际搜了 4–6 次，记成 0–1 次）。
_SEARCH_TOOL_ALIASES = {
    "search_blocks": "search_sections",
    "search_tables": "search_sections",
}


def _canonical_tool(name: object) -> str:
    text = str(name or "")
    return _SEARCH_TOOL_ALIASES.get(text, text)


def check_tool_constraints(task: dict, result: dict) -> dict[str, dict]:
    constraints = task.get("tool_constraints") or {}
    tool_steps = _tool_steps(result)
    raw_sequence = [str(step.get("tool") or "") for step in tool_steps]
    tool_sequence = [_canonical_tool(name) for name in raw_sequence]
    checks: dict[str, dict] = {}

    must_include = [_canonical_tool(name) for name in (constraints.get("must_include") or [])]
    missing = [name for name in must_include if name not in tool_sequence]
    checks["tool_must_include"] = _check(
        not missing if must_include else None,
        "缺失: " + ", ".join(missing) if missing else "该调的都在",
        expected=must_include,
        actual=raw_sequence,
    )

    must_not_include = [
        _canonical_tool(name) for name in (constraints.get("must_not_include") or [])
    ]
    violated = [name for name in must_not_include if name in tool_sequence]
    checks["tool_must_not_include"] = _check(
        not violated if must_not_include else None,
        "出现了禁用工具: " + ", ".join(violated) if violated else "未出现禁用工具",
        expected=must_not_include,
        actual=raw_sequence,
    )

    call_count = {
        _canonical_tool(name): bounds
        for name, bounds in (constraints.get("call_count") or {}).items()
    }
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
        first, second = _canonical_tool(pair[0]), _canonical_tool(pair[1])
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

    retrieved = {base_chunk_id(chunk_id) for chunk_id in evidence_chunk_ids(result)}

    # 按**要点**判，不按"列表里的每个 id"判：一个事实在年报里往往有 2–3 张表
    # 都能证明（主要会计数据表 / 营业收入构成表 / 附注），任务卡把可接受的都列进
    # `chunk_ids`。要求"全中"等于要求把每张表都读一遍——2026-10-28 实测会把
    # 读了其中一张的正确运行判成漏召回。正确口径：每个要点至少要有一处证据被读到。
    points = task.get("answer_points") or []
    point_details: list[dict] = []
    for index, point in enumerate(points):
        expected = list((point or {}).get("chunk_ids") or [])
        hit = [chunk_id for chunk_id in expected if base_chunk_id(chunk_id) in retrieved]
        point_details.append({"index": index, "expected": expected, "hit": hit, "ok": bool(hit)})

    if point_details:
        missed = [detail["index"] for detail in point_details if not detail["ok"]]
        hits = [chunk_id for detail in point_details for chunk_id in detail["hit"]]
        return _check(
            not missed,
            f"要点证据命中 {len(point_details) - len(missed)}/{len(point_details)}"
            + (f"，缺要点 {missed}" if missed else ""),
            points=point_details,
            hits=hits,
        )

    hits = [chunk_id for chunk_id in relevant if base_chunk_id(chunk_id) in retrieved]
    misses = [chunk_id for chunk_id in relevant if base_chunk_id(chunk_id) not in retrieved]
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


def _equivalent_citation(
    point: dict,
    cited_base_ids: set[str],
    tables: dict[str, dict],
) -> Optional[dict]:
    """引用表不是金标表时，按“同一行/同列/同值”判等价。

    只放宽表，不放宽口径：合并 vs 母公司直接拒绝（statement_scope 不同）。
    这样研发费用同时出现在利润表和研发投入表可以接受，但拿母公司现金流
    冒充合并现金流不会被放过。
    """
    evidence = point.get("evidence") if isinstance(point.get("evidence"), dict) else {}
    row = evidence.get("row")
    column = evidence.get("column")
    value = evidence.get("value")
    if not row or not column or value is None:
        return None
    expected_table = tables.get(str(evidence.get("table_id") or ""))
    expected_scope = str((expected_table or {}).get("statement_scope") or "")
    for base_id in sorted(cited_base_ids):
        table = tables.get(base_id)
        if not table:
            continue
        cited_scope = str(table.get("statement_scope") or "")
        if expected_scope and cited_scope and expected_scope != cited_scope:
            continue
        result = cell_equivalence(row, column, value, table)
        if result.get("match"):
            return {
                "table_id": base_id,
                "row": result.get("row_label"),
                "column": result.get("column_label"),
                "value": result.get("cell_text"),
                "statement_scope": cited_scope,
            }
    return None


def check_citations(
    task: dict,
    result: dict,
    *,
    tables: Optional[dict[str, dict]] = None,
) -> dict[str, dict]:
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
    cited_base_ids = {base_chunk_id(chunk_id) for chunk_id in cited_chunk_ids}
    points = task.get("answer_points") or []
    point_details: list[dict] = []
    for index, point in enumerate(points):
        expected = list((point or {}).get("chunk_ids") or [])
        cited = [chunk_id for chunk_id in expected if base_chunk_id(chunk_id) in cited_base_ids]
        equivalent = None
        if not cited and tables:
            equivalent = _equivalent_citation(point or {}, cited_base_ids, tables)
        point_details.append(
            {
                "index": index,
                "expected": expected,
                "cited": cited,
                "ok": bool(cited or equivalent),
                "equivalent": equivalent,
            }
        )

    if points:
        all_cited = all(detail["ok"] for detail in point_details)
        uncited = [detail["index"] for detail in point_details if not detail["ok"]]
        equivalents = [
            detail["equivalent"] for detail in point_details if detail.get("equivalent")
        ]
        checks["answer_points_cited"] = _check(
            all_cited,
            f"{sum(1 for d in point_details if d['ok'])}/{len(points)} 个要点被引用"
            + (f"（其中 {len(equivalents)} 条为等价表）" if equivalents else "")
            + (f"；未引用要点 {uncited}" if uncited else ""),
            points=point_details,
        )
    else:
        checks["answer_points_cited"] = _check(None, "任务没有答案要点")

    # 引用的 chunk 必须是 Agent 真见过的：否则就是凭空造出处。
    seen_base_ids = {base_chunk_id(chunk_id) for chunk_id in evidence}
    unknown = sorted(cited_base_ids - seen_base_ids)
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
    checks["step_budget"] = _check(
        budget_ok,
        "; ".join(reasons) if reasons else f"步数 {steps}",
        steps=steps,
        max_steps=max_steps,
    )

    # 预算被用尽（模型还想继续、但轮次/步数没了）是"该调预算"的信号，**不是答案错**。
    # 2026-10-28 摸底踩过：24 条里 3 条步数 10 < max_steps 16，却被这条 note 判成失败——
    # 真实原因是 runner 把 max_steps 静默夹到 10。收口与超限必须分开记。
    truncated = any(
        flag in note for flag in ("budget_truncated", "max_steps_exceeded")
    )
    checks["budget_truncated"] = _check(
        None,
        "工具预算用尽（模型还想继续），需要时调大预算" if truncated else "预算内收口",
        truncated=truncated,
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

def collect_checks(
    task: dict,
    result: dict,
    *,
    tables: Optional[dict[str, dict]] = None,
) -> dict[str, dict]:
    """跑全部确定性检查，返回 name -> {ok, detail, ...} 的扁平字典。"""
    checks: dict[str, dict] = {}
    checks.update(check_tool_constraints(task, result))
    checks["retrieval_hit"] = check_retrieval_hit(task, result)
    checks.update(check_citations(task, result, tables=tables))
    checks.update(check_status(task, result))
    checks.update(check_injection(task, result))
    return checks


def apply_equivalent_sources(checks: dict[str, dict], judgment: Optional[dict]) -> None:
    """judge 说“引用的是另一张表”时，用确定性等价结果覆盖来源判定。

    只覆盖 citation_supports，不改 pass/数值判断；等价条件由
    `cell_equivalence` 保证（同行/同列/同值），不是放宽字符串匹配。
    """
    if not judgment:
        return
    equivalents = {
        detail["index"]: detail.get("equivalent")
        for detail in (checks.get("answer_points_cited") or {}).get("points") or []
        if detail.get("equivalent")
    }
    for index, point in enumerate(judgment.get("points") or []):
        if index not in equivalents or not point.get("pass"):
            continue
        if point.get("citation_supports"):
            continue
        point["citation_supports"] = True
        point["equivalent_source"] = equivalents[index]
        point["reason"] = (point.get("reason") or "") + "；来源按等价表接受"


def failed_checks(checks: dict[str, dict]) -> list[str]:
    """只挑明确失败（ok is False）的，None 表示不适用。"""
    return [name for name, record in checks.items() if record.get("ok") is False]


def has_check(checks: dict[str, dict], name: str) -> bool:
    return name in checks and checks[name].get("ok") is not None
