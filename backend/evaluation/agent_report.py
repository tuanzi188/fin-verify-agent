"""把"检查结果 + judge 结论"折成维度判定、硬门禁、失败分类与 badcase。

**三条不许破的规矩**（来自文档 §8.9 / §8.6）：

1. **维度归因 ≠ 失败原因**：一条任务的分只算在它自己那一维；失败原因另开一张表全局统计。
   同一任务可能"多步失败"但真实原因是"第二跳空结果后没重试"。
2. **NA 不是 0**：judge 不可用、注入没触发 → 这一轮不进分母，不能当失败算。
3. **门槛不拍脑袋**：延迟门禁的阈值来自文档示例，尚未用 baseline 校准，
   报告里必须标 `calibrated: false`，不许让它看起来像统计结论。

每维 6 条，所以维度分数只报点估计 + Wilson 95% CI，**不设百分比门禁**。
"""

from typing import Optional

from evaluation.agent_metrics import (
    count_by,
    fisher_exact_two_sided,
    fisher_min_split,
    format_interval,
    mcnemar_exact,
    mcnemar_min_split,
    median,
    percentile,
    ratio,
    wilson_interval,
)

DIMENSIONS = ("检索与引用", "多步拆解", "拒答与边界", "澄清与追问", "恢复与纠错")

# 哪些维度的判定必须有 judge：没有 judge 只能记 NA，不能记 0
NEEDS_JUDGE = {
    "检索与引用": True,
    "多步拆解": True,
    "拒答与边界": True,
    "澄清与追问": True,
    "恢复与纠错": True,
}

# 失败原因优先级：结构性 > 语义性。排在前面的当"主因"。
CAUSE_PRIORITY = (
    "崩溃",
    "预算超限",
    "编造",
    "过度拒答",
    "漏调用",
    "参数错误",
    "工具故障未恢复",
    "召回失败",
    "引用错误",
    "引用缺失",
    "要点缺失",
    "澄清错误",
    "状态错误",
)

CAUSE_HINTS = {
    "崩溃": "查 trace 的 run_error，先修可用性再看分数",
    "预算超限": "检查 max_steps 与停止判据，是否在空结果上反复重试",
    "编造": "补资料不足时的拒答判据，不要只靠 prompt 求",
    "过度拒答": "正样本被拒：查 similarity 阈值与弃答关键词是否过严",
    "漏调用": "该检索却直接作答：把'先检索'从提示词搬成硬约束",
    "参数错误": "工具参数越界：在工具描述里写清边界，或让工具返回可读错误",
    "工具故障未恢复": "注入后没有后续动作：补重试/改写/降级路径",
    "召回失败": "相关段落没进结果：先看 query 构造，再看召回配置",
    "引用错误": "引用的段落不支撑结论：检查编号映射与引用生成规则",
    "引用缺失": "该引用却没有引用：强制逐点标注出处",
    "要点缺失": "答案不完整：查拆解是否漏子问题",
    "澄清错误": "该问没问 / 不该问却问：查槽位判定与反向对照样本",
    "状态错误": "状态与期望不符：查状态判定规则",
}

# 硬门禁的阈值。`calibrated=False` 表示还没按 §8.9 用 baseline 重设，
# 报告里必须原样标出来，不能悄悄当成验收线。
HARD_GATE_CONFIG = {
    "fabrication": {"limit": 0, "calibrated": True, "unit": "条"},
    "false_refusal": {"limit": 0, "calibrated": True, "unit": "条"},
    "citation_errors": {"limit": 1, "calibrated": True, "unit": "条"},
    "crashes": {"limit": 0, "calibrated": True, "unit": "条"},
    "latency_median_ms": {"limit": 3000, "calibrated": False, "unit": "ms"},
    "latency_max_ms": {"limit": 6000, "calibrated": False, "unit": "ms"},
    "smoke": {"limit": 0, "calibrated": True, "unit": "条", "implemented": False},
}


def _check_ok(checks: dict, name: str) -> Optional[bool]:
    record = checks.get(name) or {}
    return record.get("ok")


def _any_false(checks: dict, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if _check_ok(checks, name) is False]


def _points_all_pass(judgment: dict) -> bool:
    points = judgment.get("points") or []
    return all(bool(point.get("pass")) for point in points)


def _points_all_supported(judgment: dict) -> bool:
    points = judgment.get("points") or []
    return all(bool(point.get("citation_supports")) for point in points)


def _step_error_kinds(result: dict) -> list[str]:
    kinds: list[str] = []
    for step in result.get("trace") or []:
        if step.get("error_kind"):
            kinds.append(str(step["error_kind"]))
    return kinds


def classify_causes(task: dict, result: dict, checks: dict, judgment: Optional[dict]) -> list[str]:
    """按实际原因给失败分类（与维度无关，全局统计用）。"""
    causes: list[str] = []
    tool_steps = [step for step in result.get("trace") or [] if step.get("tool") != "__final__"]

    if _check_ok(checks, "no_crash") is False:
        causes.append("崩溃")
    if _check_ok(checks, "step_budget") is False:
        causes.append("预算超限")
    if judgment and judgment.get("forbidden_hit"):
        causes.append("编造")
    if _check_ok(checks, "false_refusal") is False and not task.get("mock"):
        # mock 任务的"拒答"是工具被打坏后的诚实反应，不算过度拒答
        causes.append("过度拒答")
    if _check_ok(checks, "tool_must_include") is False and not tool_steps:
        causes.append("漏调用")
    if "bad_args" in _step_error_kinds(result):
        causes.append("参数错误")
    if _check_ok(checks, "recovery_attempted") is False:
        causes.append("工具故障未恢复")
    if _check_ok(checks, "retrieval_hit") is False:
        causes.append("召回失败")
    if _check_ok(checks, "citation_markers_resolve") is False or _check_ok(checks, "citation_scope") is False:
        causes.append("引用错误")
    if judgment and any(
        point.get("cited") and not point.get("citation_supports")
        for point in judgment.get("points") or []
    ):
        if "引用错误" not in causes:
            causes.append("引用错误")
    if _check_ok(checks, "citation_present") is False:
        causes.append("引用缺失")
    if judgment and any(not point.get("pass") for point in judgment.get("points") or []):
        causes.append("要点缺失")
    # 澄清判定只在澄清维度算失败原因：别的维度里 mode=none 的"别反问"指令会误伤，
    # 把"工具坏了只好说资料不足"记成澄清错误，纯属噪声。
    if (
        judgment
        and judgment.get("clarify_verdict") == "bad"
        and task.get("dimension") == "澄清与追问"
    ):
        causes.append("澄清错误")
    if _check_ok(checks, "abstain_status") is False and "过度拒答" not in causes:
        causes.append("状态错误")

    ordered = [cause for cause in CAUSE_PRIORITY if cause in causes]
    return ordered or (["未知"] if causes else [])


def dimension_verdict(
    task: dict,
    result: dict,
    checks: dict,
    judge_result: Optional[dict],
) -> dict:
    """给出这一轮在这一维上的判定：pass / fail / na / invalid。"""
    dimension = str(task.get("dimension") or "")
    judgment = (judge_result or {}).get("judgment") if judge_result else None
    judge_ok = bool((judge_result or {}).get("ok"))
    reasons: list[str] = []

    # 注入没触发：这一轮不是有效的恢复样本，整轮不进分母
    if _check_ok(checks, "injection_triggered") is False:
        return {
            "dimension": dimension,
            "outcome": "invalid",
            "reasons": ["注入未触发，本轮不是有效样本"],
            "causes": [],
        }

    if NEEDS_JUDGE.get(dimension) and not judge_ok:
        return {
            "dimension": dimension,
            "outcome": "na",
            "reasons": [f"judge 不可用: {(judge_result or {}).get('error') or '未运行'}"],
            "causes": [],
        }

    causes = classify_causes(task, result, checks, judgment)

    if dimension == "澄清与追问":
        # 澄清维度只看行为对不对：该问的问了、不该问的没问。
        # 答案要点在别的维度考，不在这里重复计分。
        verdict_ok = (
            _check_ok(checks, "no_crash") is not False
            and (judgment or {}).get("clarify_verdict") == "ok"
        )
        if not verdict_ok:
            reasons.append(
                f"澄清判定 {((judgment or {}).get('clarify_verdict'))}: "
                f"{((judgment or {}).get('clarify_reason') or '')}"
            )
        return {
            "dimension": dimension,
            "outcome": "pass" if verdict_ok else "fail",
            "reasons": reasons,
            "causes": causes if not verdict_ok else [],
        }

    common_checks = (
        "no_crash",
        "tool_must_include",
        "tool_must_not_include",
        "tool_call_count",
        "tool_order",
        "citation_markers_resolve",
        "citation_scope",
        "step_budget",
    )
    failed = _any_false(checks, common_checks)
    if failed:
        reasons.append("确定性检查未过: " + ", ".join(failed))

    if dimension in ("检索与引用", "多步拆解"):
        if _check_ok(checks, "retrieval_hit") is False:
            reasons.append(_detail(checks, "retrieval_hit"))
        if _check_ok(checks, "citation_present") is False:
            reasons.append("要求引用但没有引用")
        if _check_ok(checks, "answer_points_cited") is False:
            reasons.append(_detail(checks, "answer_points_cited"))
        if judgment:
            if not _points_all_pass(judgment):
                reasons.append("答案要点未全部覆盖")
            if not _points_all_supported(judgment):
                reasons.append("存在引用不支撑要点的情况")
            if judgment.get("forbidden_hit"):
                reasons.append("命中禁止内容: " + str(judgment.get("forbidden_reason") or ""))

    if dimension == "拒答与边界":
        if _check_ok(checks, "abstain_status") is False:
            reasons.append(_detail(checks, "abstain_status"))
        if judgment and judgment.get("forbidden_hit"):
            reasons.append("命中禁止内容: " + str(judgment.get("forbidden_reason") or ""))

    if dimension == "恢复与纠错":
        if _check_ok(checks, "retrieval_hit") is False and not task.get("mock"):
            reasons.append(_detail(checks, "retrieval_hit"))
        if judgment:
            if not _points_all_pass(judgment):
                reasons.append("故障后未能答对")
            if judgment.get("forbidden_hit"):
                reasons.append("命中禁止内容")

    outcome = "fail" if reasons else "pass"
    return {
        "dimension": dimension,
        "outcome": outcome,
        "reasons": reasons,
        "causes": causes if outcome == "fail" else [],
    }


def _detail(checks: dict, name: str) -> str:
    record = checks.get(name) or {}
    return str(record.get("detail") or name)


PASS_K_MIN_RUNS = 3


def build_power_k(verdicts: list[dict], runs_per_task: int = 1) -> dict:
    """按任务聚合多次运行：pass@1（单次成功率均值）与 pass^k（k 次全过）。

    三条口径：

    1. 只有 `pass` / `fail` 是有效运行；`na`（judge 不可用）与 `invalid`
       （注入未触发）既不算过也不算挂，**不拿 0 顶替**。
    2. 一个任务要进 pass^k 的分母，必须有 k 次有效运行；缺次的任务
       进 `excluded`，不能拿"只跑了 1 次且过了"冒充"三次全过"。
    3. pass@1 先按任务算通过率、再对任务取平均。等次数下与"逐次池化"同值，
       但某条任务多跑几次时不会被它拉偏口径。
    """
    k = max(1, int(runs_per_task))
    grouped: dict[str, list[dict]] = {}
    for verdict in verdicts:
        grouped.setdefault(str(verdict.get("task_id")), []).append(verdict)

    task_rows: list[dict] = []
    excluded: list[dict] = []
    for task_id, rows in grouped.items():
        rows = sorted(rows, key=lambda row: int(row.get("run_index") or 1))
        outcomes = [str(row.get("outcome") or "") for row in rows]
        valid = [outcome for outcome in outcomes if outcome in ("pass", "fail")]
        passed = sum(1 for outcome in valid if outcome == "pass")
        complete = len(valid) >= k and len(rows) >= k
        row = {
            "task_id": task_id,
            "dimension": str(rows[0].get("dimension") or ""),
            "runs": len(rows),
            "valid_runs": len(valid),
            "pass": passed,
            "fail": len(valid) - passed,
            "na": outcomes.count("na"),
            "invalid": outcomes.count("invalid"),
            "pass_at_1": ratio(passed, len(valid)),
            "pass_pow_k": (1.0 if passed >= k else 0.0) if complete else None,
            "complete": complete,
        }
        task_rows.append(row)
        if not complete:
            excluded.append({
                "task_id": task_id,
                "valid_runs": len(valid),
                "required_runs": k,
                "reason": f"有效运行 {len(valid)}/{k}（NA {row['na']}、无效 {row['invalid']}）",
            })

    def _summarize(rows: list[dict]) -> dict:
        rates = [row["pass_at_1"] for row in rows if row["pass_at_1"] is not None]
        complete_rows = [row for row in rows if row["complete"]]
        stable = sum(1 for row in complete_rows if row["pass_pow_k"] == 1.0)
        return {
            "tasks": len(rows),
            "complete_tasks": len(complete_rows),
            "pass_at_1": (sum(rates) / len(rates)) if rates else None,
            "pass_pow_k": ratio(stable, len(complete_rows)),
            "stable_tasks": stable,
            "task_rows": rows,
        }

    by_dimension = {
        dimension: _summarize([row for row in task_rows if row["dimension"] == dimension])
        for dimension in DIMENSIONS
    }
    overall = _summarize(task_rows)
    return {
        "k": k,
        "runs_per_task": k,
        "meets_minimum_runs": k >= PASS_K_MIN_RUNS,
        "definition": (
            "pass@1 = 各任务有效运行通过率的均值；"
            f"pass^{k} = {k} 次有效运行全部通过的任务占比"
        ),
        "overall": overall,
        "by_dimension": by_dimension,
        "tasks": task_rows,
        "excluded": excluded,
        "note": (
            f"本报告每任务 {k} 次运行，pass^{k} 只在完整任务上计算；"
            f"{len(excluded)} 条任务因有效运行不足被排除。"
            if k > 1
            else "每任务只跑 1 次，pass^k 不成立；只有 pass@1 这一个数。"
        ),
    }


def aggregate(verdicts: list[dict], judge_repeats: int = 1, runs_per_task: int = 1) -> dict:
    """按维度汇总点估计 + Wilson 区间，并算横切指标与任务级 pass^k。"""
    dimensions: dict[str, dict] = {}
    for dimension in DIMENSIONS:
        rows = [verdict for verdict in verdicts if verdict["dimension"] == dimension]
        passed = sum(1 for row in rows if row["outcome"] == "pass")
        failed = sum(1 for row in rows if row["outcome"] == "fail")
        na = sum(1 for row in rows if row["outcome"] == "na")
        invalid = sum(1 for row in rows if row["outcome"] == "invalid")
        denominator = passed + failed
        task_ids = {row.get("task_id") for row in rows}
        interval = wilson_interval(passed, denominator) if denominator else (0.0, 0.0)
        dimensions[dimension] = {
            "pass": passed,
            "fail": failed,
            "denominator": denominator,
            "tasks": len(task_ids),
            "runs": len(rows),
            "rate_basis": "per_run" if int(runs_per_task) > 1 else "per_task",
            "na": na,
            "invalid": invalid,
            "point_estimate": ratio(passed, denominator),
            "interval": list(interval) if denominator else None,
            "label": format_interval(passed, denominator) if denominator else "N/A",
            # 有效样本不足 6 时必须自己标出来：2/2 = 100% 这种数字最容易被当成结论。
            # 多次运行时按**任务数**判，不按运行次数——3 次 × 2 任务不等于 6 条样本。
            "sample_note": (
                None
                if len(task_ids) >= 6
                else f"有效任务仅 {len(task_ids)}/6（NA {na}、无效 {invalid}），点估计不可当结论"
            ),
            "gate": "无（不设百分比门禁）",
        }

    latencies = [
        int((verdict.get("usage") or {}).get("latency_ms") or 0)
        for verdict in verdicts
        if (verdict.get("usage") or {}).get("latency_ms")
    ]
    steps = [
        int((verdict.get("usage") or {}).get("steps") or 0)
        for verdict in verdicts
    ]
    tokens_in = sum(int((verdict.get("usage") or {}).get("tokens_in") or 0) for verdict in verdicts)
    tokens_out = sum(int((verdict.get("usage") or {}).get("tokens_out") or 0) for verdict in verdicts)

    judged_points = [
        point
        for verdict in verdicts
        for point in ((verdict.get("judgment") or {}).get("points") or [])
    ]
    cited_points = [point for point in judged_points if point.get("cited")]
    supported_points = [point for point in cited_points if point.get("citation_supports")]

    recovery_rows = [
        verdict for verdict in verdicts if verdict["dimension"] == "恢复与纠错" and verdict["outcome"] != "invalid"
    ]
    # 「尝试恢复」是确定性检查，na（judge 缺席）不影响它；
    # 「恢复成功」必须有判定，所以只算 pass+fail，NA 不能记成 0。
    attempt_rows = [
        verdict
        for verdict in recovery_rows
        if (verdict.get("checks") or {}).get("recovery_attempted", {}).get("ok") is not None
    ]
    success_rows = [verdict for verdict in recovery_rows if verdict["outcome"] in ("pass", "fail")]
    attempted = sum(
        1 for verdict in attempt_rows if (verdict.get("checks") or {}).get("recovery_attempted", {}).get("ok")
    )
    recovered = sum(1 for verdict in success_rows if verdict["outcome"] == "pass")

    grounded_rows = [
        verdict for verdict in verdicts
        if (verdict.get("judgment") or {}).get("grounded") is not None
    ]
    grounded_true = sum(1 for verdict in grounded_rows if (verdict.get("judgment") or {}).get("grounded"))

    power_k = build_power_k(verdicts, runs_per_task=runs_per_task)
    overall_power = power_k["overall"]
    pass_note = (
        f"每任务 {power_k['k']} 次：pass@1 取任务均值，pass^{power_k['k']} 只计完整任务"
        if power_k["k"] > 1
        else "每任务 1 次：只有 pass@1，pass^k 不成立"
    )

    cross = {
        "avg_steps": round(sum(steps) / len(steps), 2) if steps else None,
        "median_latency_ms": int(median(latencies)) if latencies else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "p95_latency_ms": int(percentile(latencies, 0.95)) if latencies else None,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "citation_precision": ratio(len(supported_points), len(cited_points)),
        "citation_precision_label": format_interval(len(supported_points), len(cited_points)),
        "grounded_rate": ratio(grounded_true, len(grounded_rows)),
        "grounded_label": format_interval(grounded_true, len(grounded_rows)),
        "recovery_attempt_rate": ratio(attempted, len(attempt_rows)),
        "recovery_attempt_label": format_interval(attempted, len(attempt_rows)),
        "recovery_success_rate": ratio(recovered, len(success_rows)),
        "recovery_success_label": format_interval(recovered, len(success_rows)),
        "pass_at_1": overall_power.get("pass_at_1"),
        # 字段名固定为 pass_pow_3：只有 k=3 及以上才有意义，k=2 时留空并写清原因。
        "pass_pow_3": (
            overall_power.get("pass_pow_k")
            if power_k["k"] >= PASS_K_MIN_RUNS
            else None
        ),
        "pass_note": pass_note,
    }

    cause_counts = count_by(
        (cause for verdict in verdicts for cause in verdict.get("causes") or []),
        lambda cause: cause,
    )
    # 一次失败常常同时挂 3–4 个原因（"工具故障未恢复"必然连带"要点缺失""状态错误"）。
    # 只报共现次数会让直方图加起来超过失败条数，读者会重复计数；
    # 所以两栏都给：primary 是 badcase 里用的那个主因，cooccurrence 是全景。
    primary_counts = count_by(
        ((verdict.get("causes") or ["未知"])[0] for verdict in verdicts if verdict.get("outcome") == "fail"),
        lambda cause: cause,
    )
    failure_causes = {
        cause: {
            "primary_count": primary_counts.get(cause, 0),
            "cooccurrence_count": count,
            "hint": CAUSE_HINTS.get(cause, ""),
        }
        for cause, count in sorted(
            cause_counts.items(), key=lambda pair: (-primary_counts.get(pair[0], 0), -pair[1], pair[0])
        )
    }
    failure_cause_summary = {
        "failed_runs": sum(1 for verdict in verdicts if verdict.get("outcome") == "fail"),
        "primary_total": sum(primary_counts.values()),
        "cooccurrence_total": sum(cause_counts.values()),
    }

    judge_judged = sum(1 for verdict in verdicts if verdict.get("judgment"))
    judge_errors = sum(1 for verdict in verdicts if verdict.get("judge_error"))
    # 有 error 不等于整条判不了：repeats=2 时一次解析失败、另一次成功，维度仍有判定。
    # 把两者分开报，否则"失败 1 次"会被读成"有 1 条没分"。
    judge_failed_tasks = sum(
        1 for verdict in verdicts if verdict.get("judge_error") and not verdict.get("judgment")
    )
    judge_partial = sum(
        1 for verdict in verdicts if verdict.get("judge_error") and verdict.get("judgment")
    )
    agreements = [
        verdict["judge_agreement"]
        for verdict in verdicts
        if verdict.get("judge_agreement") is not None
    ]

    return {
        "dimensions": dimensions,
        "cross": cross,
        "power_k": power_k,
        "failure_causes": failure_causes,
        "failure_cause_summary": failure_cause_summary,
        "judge": {
            # `calls` 是历史字段名，语义其实是"判过几条"；真实请求数 = 条数 × repeats。
            # 自检发现过一次口径错误：报告写"调用 30 次"，实际发了 60 次请求。
            "judged_tasks": judge_judged,
            "repeats": max(1, int(judge_repeats)),
            "llm_calls": judge_judged * max(1, int(judge_repeats)),
            "errors": judge_errors,
            "failed_tasks": judge_failed_tasks,
            "partial_failures": judge_partial,
            "mean_self_agreement": round(sum(agreements) / len(agreements), 3) if agreements else None,
            "agreement_samples": len(agreements),
            "agreement_scope": "点级（answer point / forbidden / clarify 判定）跨重复一致率，不是整条通过率",
        },
    }


def hard_gates(verdicts: list[dict], cross: dict) -> dict:
    """硬门禁：只设在零容忍/确定性项上，用计数不用百分比。"""
    fabrication = sum(
        1 for verdict in verdicts if (verdict.get("judgment") or {}).get("forbidden_hit")
    )
    # 正样本误拒只算非 mock 任务：注入把工具打坏之后，Agent 说"资料不足"是诚实行为，
    # 不是过度拒答。把这类算进误拒，门禁会在"我们故意弄坏工具"的地方变红。
    false_refusal = sum(
        1
        for verdict in verdicts
        if not verdict.get("mock")
        and (verdict.get("checks") or {}).get("false_refusal", {}).get("ok") is False
    )
    citation_errors = sum(
        1
        for verdict in verdicts
        if (verdict.get("checks") or {}).get("citation_markers_resolve", {}).get("ok") is False
        or (verdict.get("checks") or {}).get("citation_scope", {}).get("ok") is False
        or any(
            not point.get("citation_supports")
            for point in ((verdict.get("judgment") or {}).get("points") or [])
            if point.get("cited")
        )
    )
    crashes = sum(
        1 for verdict in verdicts if (verdict.get("checks") or {}).get("no_crash", {}).get("ok") is False
    )

    measured = {
        "fabrication": fabrication,
        "false_refusal": false_refusal,
        "citation_errors": citation_errors,
        "crashes": crashes,
        "latency_median_ms": cross.get("median_latency_ms"),
        "latency_max_ms": cross.get("max_latency_ms"),
        "smoke": None,
    }

    results: dict[str, dict] = {}
    for name, config in HARD_GATE_CONFIG.items():
        value = measured.get(name)
        entry = {
            "value": value,
            "limit": config["limit"],
            "unit": config.get("unit"),
            "calibrated": config.get("calibrated", True),
        }
        if config.get("implemented") is False:
            entry["pass"] = None
            entry["note"] = "本版未实现（12 条冒烟子集在 P4 落地）"
        elif value is None:
            entry["pass"] = None
            entry["note"] = "无数据"
        else:
            entry["pass"] = value <= config["limit"]
        if config.get("calibrated") is False:
            entry["note"] = (
                "阈值来自文档示例，尚未按 §8.9 用 baseline 校准；红了不代表产品不合格，"
                "代表门槛该重设"
            )
        # 未校准的门禁只做提示、不参与 overall_pass：
        # 一个没校准的阈值不该拦住任何人，否则 CI 会长期变红，然后大家开始忽略它。
        entry["blocking"] = bool(config.get("calibrated", True)) and config.get("implemented") is not False
        results[name] = entry

    results["overall_pass"] = all(
        entry.get("pass") is not False
        for entry in results.values()
        if isinstance(entry, dict) and entry.get("blocking")
    )
    results["advisory_failures"] = [
        name
        for name, entry in results.items()
        if isinstance(entry, dict) and not entry.get("blocking") and entry.get("pass") is False
    ]
    return results


def build_badcases(verdicts: list[dict], limit: int = 100) -> list[dict]:
    """失败样本清单：每条要给得出"哪个任务、哪一轮、主因、证据、怎么改"。"""
    badcases: list[dict] = []
    for verdict in verdicts:
        if verdict.get("outcome") != "fail":
            continue
        causes = verdict.get("causes") or []
        primary = causes[0] if causes else "未知"
        badcases.append({
            "task_id": verdict.get("task_id"),
            "run_index": verdict.get("run_index"),
            "dimension": verdict.get("dimension"),
            "primary_cause": primary,
            "causes": causes,
            "reasons": verdict.get("reasons") or [],
            "suggested_fix": CAUSE_HINTS.get(primary, ""),
            "answer_preview": str(verdict.get("answer") or "")[:200],
            "trace_tools": [
                {
                    "tool": step.get("tool"),
                    "ok": step.get("ok"),
                    "injected": step.get("injected"),
                    "result_count": step.get("result_count"),
                    "error_kind": step.get("error_kind"),
                }
                for step in verdict.get("trace") or []
            ],
        })
        if len(badcases) >= limit:
            break
    return badcases


def build_construct_notes(
    variant: Optional[str],
    verdicts: list[dict],
    runs_per_task: int = 1,
) -> list[str]:
    """写出"这些数字该怎么读"的口径说明。

    自检发现的两个构造效度问题：低分不一定代表能力差，可能是不变量（invariant）
    决定的。这类话不能只写在对话里，必须跟报告一起落盘，否则下一个人会把
    16.7% 当成"Agent 不会恢复"。
    """
    notes: list[str] = []
    if variant == "baseline":
        notes.append(
            "baseline 只跑一轮工具调用：首个检索调用失败后没有第二轮修正机会。"
            "「恢复与纠错」在 baseline 上近似测量“模型是否恰好并行发了两次调用”，"
            "不是恢复策略的效果——该维度低分是设计上限，不能当能力结论。"
        )
    if variant == "improved":
        notes.append(
            "improved 的多轮、空/错自动重试与覆盖自检是 runner 里的机制，不是换了一套更会写的提示词；"
            "与 baseline 共用同一套证据纪律。成本差（token/延迟）看 cross，不看单维分数。"
        )
    if any(verdict.get("dimension") == "澄清与追问" for verdict in verdicts):
        notes.append(
            "「澄清与追问」判的是“有没有静默选边”：明确追问、或把两种情形各自的结论都给出，都算对；"
            "只讲一种情形并把它当成用户答案才算错。"
            "不要拿“有没有问号”“有没有写取决于”当判据。"
        )
    if any(verdict.get("mock") for verdict in verdicts):
        notes.append(
            "注入任务的“拒答”不计过度拒答：工具被故意打坏后说资料不足是诚实行为。"
            "false_refusal 门禁只统计非 mock 任务，恢复维度另按注入是否触发算有效样本。"
        )
    if int(runs_per_task) <= 1:
        # Wilson 只回答"再抽 6 条会怎样"，不回答"同一套题再跑一遍会怎样"。
        # 2026-10-27 实测两批独立运行有 6/30 条维度结论翻转，所以必须提示这一层方差。
        notes.append(
            "本报告每任务只跑 1 次。Wilson 区间只反映样本量，不含 run-to-run 方差——"
            "同一代码两批独立运行的维度翻转达 6/30（§8.12）。引用剖面数字前请先看多次运行的一致性，"
            "单次剖面只能当“这一批长这样”，不能当能力结论。"
        )
    else:
        notes.append(
            f"本报告每任务跑 {int(runs_per_task)} 次。维度表的分母是运行次数（任务数 × 次数），"
            "同一任务的多次运行不独立，Wilson 区间会偏窄；任务级结论看 power_k 的 pass@1 / pass^k。"
            "pass@1 是各任务有效运行通过率的均值，不是“第一次运行”的成绩。"
        )
    return notes


def build_resolution(verdicts: list[dict], runs_per_task: int = 1) -> dict:
    """这套题集能看见多大的差异——把"别拿 n=6 做显著性宣称"变成报告里的数。

    单批运行只能描述；跨批比较必须知道最小可检出差异。两个检验分别对应
    "两批独立比较"（Fisher）和"同一批任务跑两变体"（McNemar 配对）。
    """
    tasks_by_dimension: dict[str, set[str]] = {}
    for verdict in verdicts:
        tasks_by_dimension.setdefault(verdict["dimension"], set()).add(verdict["task_id"])
    sizes = {dimension: len(ids) for dimension, ids in tasks_by_dimension.items()}
    smallest = min(sizes.values()) if sizes else None
    suite_tasks = len({verdict["task_id"] for verdict in verdicts})

    fisher_split = fisher_min_split(smallest) if smallest else None
    mcnemar_split = mcnemar_min_split(suite_tasks) if suite_tasks else None

    def _fmt_fisher(split):
        if not split:
            return None
        a, b = split
        return (
            f"{a}/{smallest} vs {b}/{smallest} "
            f"(p={fisher_exact_two_sided(a, smallest, b, smallest):.3f})"
        )

    def _fmt_mcnemar(split):
        if not split:
            return None
        fixed, broke = split
        return f"{fixed}:{broke} (p={mcnemar_exact(fixed, broke):.4f})"

    return {
        "tasks_per_dimension": sizes,
        "one_task_percentage_points": round(100 / smallest, 1) if smallest else None,
        "independent_two_batch_min": _fmt_fisher(fisher_split),
        "paired_suite_min": _fmt_mcnemar(mcnemar_split),
        "runs_per_task": max(1, int(runs_per_task)),
        "note": (
            "分维度 n 太小：两批独立比较要到 "
            f"{_fmt_fisher(fisher_split) or '—'} 才算差异，其余一律是噪声。"
            "显著性检验只能放整卷配对（同一批任务跑两变体）。"
            "单批运行只描述，不做推断。"
        ),
    }


def _format_rate(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def render_console(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"变体: {report.get('variant')}    运行: {report.get('runs_per_task')} 次/任务")
    lines.append(
        f"模型: {report.get('model')}    语料: {report.get('corpus_version')} "
        f"({report.get('corpus_chunks')} chunk)    hash: {str(report.get('corpus_hash'))[:12]}"
    )
    lines.append("")
    lines.append(f"{'维度':<10}{'判定':<34}{'NA':<5}{'无效':<5}")
    lines.append("-" * 60)
    for dimension, entry in report["dimensions"].items():
        flag = "  ⚠ 样本不足" if entry.get("sample_note") else ""
        lines.append(
            f"{dimension:<10}{entry['label']:<34}{entry['na']:<5}{entry['invalid']:<5}{flag}"
        )
    lines.append("")

    cross = report["cross"]
    lines.append("横切：")
    lines.append(f"  平均步数 {cross.get('avg_steps')}    "
                 f"中位延迟 {cross.get('median_latency_ms')} ms    "
                 f"最大 {cross.get('max_latency_ms')} ms    "
                 f"P95 {cross.get('p95_latency_ms')} ms")
    lines.append(f"  引用正确率 {cross.get('citation_precision_label')}    "
                 f"有据可依 {cross.get('grounded_label')}")
    lines.append(f"  尝试恢复率 {cross.get('recovery_attempt_label')}    "
                 f"恢复成功率 {cross.get('recovery_success_label')}")
    lines.append(f"  token: 入 {cross.get('tokens_in')} / 出 {cross.get('tokens_out')}")
    lines.append("")

    resolution = report.get("resolution") or {}
    if resolution:
        sizes = resolution.get("tasks_per_dimension") or {}
        smallest = min(sizes.values()) if sizes else None
        lines.append("分辨率（这套题集能看见多大的差异）：")
        lines.append(
            f"  每维 {smallest if smallest is not None else '—'} 条"
            f" → 1 条任务 = {resolution.get('one_task_percentage_points')} 个百分点"
        )
        lines.append(
            f"  两批独立比较需 {resolution.get('independent_two_batch_min')} 才算差异；"
            f"整卷配对需 {resolution.get('paired_suite_min')}"
        )
        lines.append("  分维度只描述，不做显著性宣称。")
        lines.append("")

    power_k = report.get("power_k") or {}
    if power_k:
        overall = power_k.get("overall") or {}
        k = power_k.get("k")
        lines.append(f"pass^k（任务级稳定性，k={k}）：")
        lines.append(
            f"  整体 pass@1 {_format_rate(overall.get('pass_at_1'))}    "
            f"pass^{k} {_format_rate(overall.get('pass_pow_k'))}    "
            f"完整任务 {overall.get('complete_tasks')}/{overall.get('tasks')}"
        )
        for dimension, entry in (power_k.get("by_dimension") or {}).items():
            lines.append(
                f"  {dimension:<10} pass@1 {_format_rate(entry.get('pass_at_1'))}    "
                f"pass^{k} {_format_rate(entry.get('pass_pow_k'))}    "
                f"完整 {entry.get('complete_tasks')}/{entry.get('tasks')}"
            )
        excluded = power_k.get("excluded") or []
        if excluded:
            lines.append(
                f"  排除 {len(excluded)} 条有效运行不足的任务（不拿少跑的次数冒充 k 次）"
            )
        lines.append("")

    lines.append("硬门禁：")
    for name, entry in report["hard_gates"].items():
        if not isinstance(entry, dict) or "value" not in entry:
            continue
        flag = "✗" if entry.get("pass") is False else ("✓" if entry.get("pass") else "—")
        note = f"  ({entry.get('note')})" if entry.get("note") else ""
        calibrated = "" if entry.get("calibrated", True) else " [未校准·不拦截]"
        lines.append(
            f"  {flag} {name:<20} 实测 {entry.get('value')} / 上限 {entry.get('limit')}"
            f"{entry.get('unit') or ''}{calibrated}{note}"
        )
    advisory = report["hard_gates"].get("advisory_failures") or []
    if advisory:
        lines.append(f"  提示项（不参与 overall_pass）: {', '.join(advisory)}")
    lines.append("")

    if report.get("failure_causes"):
        summary = report.get("failure_cause_summary") or {}
        lines.append(
            f"失败原因分布（主因 {summary.get('primary_total')} / 共现 {summary.get('cooccurrence_total')}，"
            f"失败 {summary.get('failed_runs')} 条）："
        )
        for cause, entry in report["failure_causes"].items():
            lines.append(
                f"  {cause:<10} 主因 {entry.get('primary_count', 0)}"
                f"    共现 {entry.get('cooccurrence_count', 0)}"
            )
        lines.append("")

    judge = report.get("judge") or {}
    lines.append(
        f"judge: 判 {judge.get('judged_tasks')} 条 × {judge.get('repeats')} 次"
        f" = {judge.get('llm_calls')} 次请求，失败 {judge.get('errors')} 次"
        f"（整条不可判 {judge.get('failed_tasks')} / 靠剩余重复判完 {judge.get('partial_failures')}），"
        f"点级自身一致率 {judge.get('mean_self_agreement')}"
        f"（基于 {judge.get('agreement_samples')} 条重复判分）"
    )
    lines.append(f"badcase: {report.get('badcase_count')} 条")

    notes = report.get("construct_notes") or []
    if notes:
        lines.append("")
        lines.append("读数口径（不是能力结论的地方）：")
        for note in notes:
            lines.append(f"  · {note}")
    return "\n".join(lines)
