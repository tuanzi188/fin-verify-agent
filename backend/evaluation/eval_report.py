"""三层评测报告：检索层 × 路由层 × 答案层 × 闸门层，合成一份可读结论。

这个脚本不产生新数据，只做三件事：

1. **对齐**：把 retrieval / routing / answer / numeric / trajectory / gate
   六份离线 JSON 按任务对齐；
2. **分岔**：失败题先过检索段——`检索 miss` 还是 `检索 hit 但答错/拒答`，
   直接回答"该修检索还是该修生成/决策"；
3. **写口径**：缺哪层就写 `no_data`，不拿 0 顶替；每个数字都带样本量。

输出 JSON（给机器）与 Markdown（给人看，可直接进 docs/ 或面试材料）。
零模型调用。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _load(path: str) -> Optional[dict]:
    if not path:
        return None
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = _BACKEND_DIR / file_path
    if not file_path.exists():
        raise SystemExit(f"输入文件不存在: {file_path}")
    return json.loads(file_path.read_text(encoding="utf-8"))


def _retrieval_layer(payload: Optional[dict]) -> dict:
    if not payload:
        return {"status": "no_data", "note": "没有检索段结果；先跑 evaluation.retrieval_metrics"}
    arms = payload.get("arms") or {}
    primary = str(payload.get("top_k") or 5)
    summary: dict = {}
    for arm, entry in arms.items():
        arm_summary = entry.get("summary") or {}
        first = (arm_summary.get("first_search") or {}).get(primary) or {}
        any_search = (arm_summary.get("any_search") or {}).get(primary) or {}
        summary[arm] = {
            "tasks": arm_summary.get("tasks"),
            "point_hit_rate": first.get("point_hit_rate"),
            "point_hits": first.get("point_hits"),
            "point_total": first.get("point_total"),
            "task_hit_all_rate": first.get("task_hit_all_rate"),
            "mrr": first.get("mrr"),
            "context_precision": first.get("context_precision"),
            "any_search_point_hit_rate": any_search.get("point_hit_rate"),
            "read_rate": (arm_summary.get("read") or {}).get("read_rate"),
            "mean_searches": (arm_summary.get("searches") or {}).get("mean"),
        }
    return {
        "status": "ok",
        "primary_k": primary,
        "arms": summary,
        "note": "first_search 为双臂同尺口径；any_search 只作 Agent 诊断。",
    }


def _routing_layer(payload: Optional[dict]) -> dict:
    if not payload:
        return {"status": "no_data", "note": "没有路由结果；先跑 evaluation.routing_metrics"}
    return {
        "status": "ok",
        "basis": payload.get("basis"),
        "tasks": payload.get("tasks"),
        "accuracy": payload.get("accuracy"),
        "wilson": payload.get("wilson"),
        "macro_f1": payload.get("macro_f1"),
        "confusion": payload.get("confusion"),
        "cost_breakdown": payload.get("cost_breakdown"),
        "errors": [
            {
                "task_id": row.get("task_id"),
                "expected": row.get("expected_arm"),
                "actual": row.get("actual_arm"),
                "cost_type": row.get("cost_type"),
                "reason": row.get("route_reason"),
            }
            for row in payload.get("errors") or []
        ],
        "excluded_count": payload.get("excluded_count"),
        "indeterminate_count": payload.get("indeterminate_count"),
        "indeterminate_router_distribution": payload.get("indeterminate_router_distribution"),
    }


def _answer_layer(report: Optional[dict]) -> dict:
    if not report:
        return {"status": "no_data", "note": "没有答案层报告；先跑 evaluation.score_agent_runs"}
    cross = report.get("cross") or {}
    gates = report.get("hard_gates") or {}
    taxonomy = report.get("failure_taxonomy") or {}
    return {
        "status": "ok",
        "variant": report.get("variant"),
        "runs_per_task": report.get("runs_per_task"),
        "dimensions": {
            name: {
                "label": entry.get("label"),
                "point_estimate": entry.get("point_estimate"),
                "interval": entry.get("interval"),
                "denominator": entry.get("denominator"),
                "sample_note": entry.get("sample_note"),
            }
            for name, entry in (report.get("dimensions") or {}).items()
        },
        "pass_at_1": cross.get("pass_at_1"),
        "pass_at_k": cross.get("pass_at_k"),
        "pass_pow_k": cross.get("pass_pow_k"),
        "flaky_tasks": cross.get("flaky_tasks"),
        "citation_precision": cross.get("citation_precision"),
        "grounded_rate": cross.get("grounded_rate"),
        "token_stats": cross.get("token_stats"),
        "step_stats": cross.get("step_stats"),
        "latency_stats": cross.get("latency_stats"),
        "calls_stats": cross.get("calls_stats"),
        "hard_gates": {
            name: entry
            for name, entry in gates.items()
            if isinstance(entry, dict) and "value" in entry
        },
        "failure_taxonomy": taxonomy,
        # 样本分组与元数据分栏（2026-11-01）：mock 恢复不进核心总分。
        "populations": {
            name: {
                "tasks": population.get("tasks"),
                "runs": population.get("runs"),
                "pass_at_1": (population.get("cross") or {}).get("pass_at_1"),
                "pass_at_k": ((population.get("power_k") or {}).get("overall") or {}).get(
                    "pass_at_k"
                ),
                "pass_pow_k": ((population.get("power_k") or {}).get("overall") or {}).get(
                    "pass_pow_k"
                ),
            }
            for name, population in (report.get("populations") or {}).items()
        },
        "prompt_style_breakdown": report.get("prompt_style_breakdown"),
        "retrieval_hops_breakdown": report.get("retrieval_hops_breakdown"),
    }


def _numeric_layer(payload: Optional[dict]) -> dict:
    if not payload:
        return {"status": "no_data", "note": "没有数值审计；先跑 evaluation.numeric_audit"}
    return {
        "status": "ok",
        "gold_coverage": payload.get("gold_coverage"),
        "gold_points": [payload.get("gold_points_covered"), payload.get("gold_points_checked")],
        "hallucination_rate": payload.get("hallucination_rate"),
        "hallucination_events": payload.get("hallucination_events"),
        "judge_numeric_conflict_rate": payload.get("judge_numeric_conflict_rate", payload.get("hallucination_rate")),
        "unsupported_number_rate": payload.get("unsupported_number_rate"),
        "support_audit_complete": payload.get("support_audit_complete"),
        "unsourced_rate": payload.get("unsourced_rate"),
        "pii_hits": payload.get("pii_hits"),
        "tasks_with_events": payload.get("tasks_with_events"),
    }


def _trajectory_layer(payload: Optional[dict]) -> dict:
    if not payload:
        return {"status": "no_data", "note": "没有轨迹评测；先跑 evaluation.trajectory_eval"}
    return {
        "status": "ok",
        "pass_rate": payload.get("pass_rate"),
        "ordered_rate": payload.get("ordered_rate"),
        "key_tools_rate": payload.get("key_tools_rate"),
        "invalid_loop_rate": payload.get("invalid_loop_rate"),
        "budget_ok_rate": payload.get("budget_ok_rate"),
        "failures": payload.get("failures"),
    }


def _faithfulness_layer(payload: Optional[dict]) -> dict:
    if not payload:
        return {"status": "no_data", "note": "没有忠实度；探针级可选并跑 evaluation.faithfulness"}
    return {
        "status": "ok",
        "faithfulness_mean": payload.get("faithfulness_mean"),
        "faithfulness_micro": payload.get("faithfulness_micro"),
        "claims": [payload.get("claims_supported"), payload.get("claims_total")],
        "scored_runs": payload.get("scored_runs"),
    }


def _failure_split(
    report: Optional[dict],
    retrieval: Optional[dict],
    *,
    arm: str = "agent",
    primary_k: str = "5",
) -> dict:
    """失败题先过检索段：miss 还是 hit 但答错/拒答。"""
    if not report or not retrieval:
        return {"status": "no_data"}
    rows = ((retrieval.get("arms") or {}).get(arm) or {}).get("tasks") or []
    by_task = {str(row.get("task_id")): row for row in rows}
    split = {"retrieval_miss": [], "retrieval_hit_but_wrong": [], "no_retrieval_data": []}
    for verdict in report.get("verdicts") or []:
        if verdict.get("outcome") != "fail":
            continue
        task_id = str(verdict.get("task_id"))
        row = by_task.get(task_id)
        if row is None:
            split["no_retrieval_data"].append(task_id)
            continue
        first = (row.get("first_search") or {}).get(primary_k) or {}
        if not first.get("point_hits"):
            split["retrieval_miss"].append(task_id)
        else:
            split["retrieval_hit_but_wrong"].append(task_id)
    total = sum(len(value) for value in split.values())
    return {
        "status": "ok",
        "failed_runs": total,
        **{key: {"count": len(value), "tasks": value} for key, value in split.items()},
        "note": (
            "检索 miss → 修切片/过滤/召回；检索 hit 但答错/拒答 → 修生成/决策。"
            "同一题多次运行时按运行计。"
        ),
    }


def _false_refusal_diagnostic(report: Optional[dict], retrieval: Optional[dict]) -> dict:
    """误拒的检索段分岔：检索 hit 却拒答 = 误拒（业务红线候选）。"""
    if not report:
        return {"status": "no_data"}
    rows = (((retrieval or {}).get("arms") or {}).get("agent") or {}).get("tasks") or []
    by_task = {str(row.get("task_id")): row for row in rows}
    events = []
    for verdict in report.get("verdicts") or []:
        record = (verdict.get("checks") or {}).get("false_refusal") or {}
        if record.get("ok") is not False:
            continue
        task_id = str(verdict.get("task_id"))
        row = by_task.get(task_id)
        first = ((row or {}).get("first_search") or {}).get("5") or {}
        events.append(
            {
                "task_id": task_id,
                "retrieval_hit": bool(first.get("point_hits")),
                "status": record.get("status"),
                "retrieval_available": row is not None,
            }
        )
    hit_refusals = sum(1 for event in events if event["retrieval_hit"])
    return {
        "status": "ok",
        "events": events,
        "false_refusals": len(events),
        "hit_but_refused": hit_refusals,
        "note": "检索 hit 却拒答 = 误拒；检索 miss 的拒答可能是诚实行为，需人工确认。",
    }


def build_report(
    *,
    retrieval: Optional[dict] = None,
    routing: Optional[dict] = None,
    answer: Optional[dict] = None,
    numeric: Optional[dict] = None,
    trajectory: Optional[dict] = None,
    faithfulness: Optional[dict] = None,
    gate: Optional[dict] = None,
) -> dict:
    layers = {
        "retrieval": _retrieval_layer(retrieval),
        "routing": _routing_layer(routing),
        "answer": _answer_layer(answer),
        "numeric": _numeric_layer(numeric),
        "trajectory": _trajectory_layer(trajectory),
        "faithfulness": _faithfulness_layer(faithfulness),
    }
    gate_layer = (
        {
            "status": "ok",
            "overall_pass": gate.get("overall_pass"),
            "gate_status": gate.get("status"),
            "blocking_failures": gate.get("blocking_failures"),
            "advisory_failures": gate.get("advisory_failures"),
            "red_lines": gate.get("red_lines"),
            "gates": {
                name: {
                    "label": entry.get("label"),
                    "status": entry.get("status"),
                    "blocking_failures": entry.get("blocking_failures"),
                    "advisory_failures": entry.get("advisory_failures"),
                }
                for name, entry in (gate.get("gates") or {}).items()
            },
        }
        if gate
        else {"status": "no_data", "note": "没有 Release Gate；先跑 evaluation.release_gate"}
    )
    layers["gate"] = gate_layer

    return {
        "kind": "eval_report_v2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "layers": layers,
        "diagnostics": {
            "failure_split": _failure_split(answer, retrieval),
            "false_refusal": _false_refusal_diagnostic(answer, retrieval),
        },
        "reading_notes": [
            "检索层 first_search 是双臂同尺口径；any_search 是 Agent 诊断口径，不能与一次检索的 RAG 直接比。",
            "答案层的 pass@1 / pass@k / pass^k 只在完整任务上计算；Wilson 区间只反映样本量，不含 run-to-run 方差。",
            "路由层的金标来自任务卡元数据；拒答/澄清/假设类不判更优臂，只报分流分布。",
            "裁判数值冲突率只审计判分器；数值证据缺口单独按实际读取原文检查，不认证指标语义归属。",
            "缺层的指标写 no_data，不拿 0 顶替；no_data 不等于通过。",
        ],
    }


def render_markdown(report: dict) -> str:
    lines = ["# 金融查证 Agent · 三层评测报告", ""]
    lines.append(f"生成时间：{report.get('generated_at')}")
    lines.append("")

    retrieval = report["layers"]["retrieval"]
    lines.append("## 1. 检索层")
    if retrieval.get("status") != "ok":
        lines.append(f"- no_data：{retrieval.get('note')}")
    else:
        k = retrieval.get("primary_k")
        lines.append(f"主口径 K={k}（first_search，双臂同尺）")
        lines.append("")
        lines.append("| 臂 | 要点命中 | 整题全中 | MRR | context precision | read rate | 平均检索次数 |")
        lines.append("|---|---|---|---|---|---|---|")
        for arm, entry in retrieval.get("arms", {}).items():
            point = f"{entry.get('point_hits')}/{entry.get('point_total')} ({_pct(entry.get('point_hit_rate'))})"
            task = f"{_pct(entry.get('task_hit_all_rate'))}"
            mrr = entry.get("mrr")
            precision = entry.get("context_precision")
            lines.append(
                f"| {arm} | {point} | {task} | {mrr if mrr is not None else 'n/a'} | "
                f"{_pct(precision)} | {_pct(entry.get('read_rate'))} | {entry.get('mean_searches')} |"
            )
        lines.append("")
        lines.append("> first_search 是主口径；any_search（Agent 所有检索并集）只在 JSON 里作诊断。")

    routing = report["layers"]["routing"]
    lines.append("")
    lines.append("## 2. 路由层")
    if routing.get("status") != "ok":
        lines.append(f"- no_data：{routing.get('note')}")
    else:
        lines.append(
            f"- 准确率 {_pct(routing.get('accuracy'))} "
            f"（{routing.get('tasks')} 题，Wilson {routing.get('wilson')}）"
        )
        lines.append(f"- Macro-F1：{routing.get('macro_f1')}")
        cost = routing.get("cost_breakdown") or {}
        lines.append(
            f"- 错分代价：简单题误走 Agent {cost.get('simple_to_agent', 0)} 题；"
            f"复杂题误走结构检索 {cost.get('complex_to_rag', 0)} 题"
        )
        if routing.get("indeterminate_count"):
            lines.append(
                f"- 路由边界题：{routing.get('indeterminate_count')} 题，"
                f"实际分流 {routing.get('indeterminate_router_distribution')}（不进准确率分母）"
            )
        if routing.get("errors"):
            lines.append("")
            lines.append("| 任务 | 金标 | 实际 | 代价 | 理由 |")
            lines.append("|---|---|---|---|---|")
            for row in routing["errors"]:
                lines.append(
                    f"| {row.get('task_id')} | {row.get('expected')} | {row.get('actual')} | "
                    f"{row.get('cost_type')} | {row.get('reason')} |"
                )

    answer = report["layers"]["answer"]
    lines.append("")
    lines.append("## 3. 答案层")
    if answer.get("status") != "ok":
        lines.append(f"- no_data：{answer.get('note')}")
    else:
        lines.append(f"- 变体：{answer.get('variant')}，每任务 {answer.get('runs_per_task')} 次")
        lines.append(
            f"- pass@1 {_pct(answer.get('pass_at_1'))}；"
            f"pass@k {_pct(answer.get('pass_at_k'))}；"
            f"pass^k {_pct(answer.get('pass_pow_k'))}；"
            f"不稳定任务 {answer.get('flaky_tasks')}"
        )
        lines.append(
            f"- 引用正确率 {_pct(answer.get('citation_precision'))}；"
            f"有据可依 {_pct(answer.get('grounded_rate'))}"
        )
        token = answer.get("token_stats") or {}
        if token.get("n"):
            lines.append(
                f"- token/次：均值 {token.get('mean')} ± {token.get('std')}，"
                f"中位 {token.get('median')}，P95 {token.get('p95')}"
            )
        lines.append("")
        lines.append("| 维度 | 判定 | 分母 | 样本提示 |")
        lines.append("|---|---|---|---|")
        for name, entry in (answer.get("dimensions") or {}).items():
            lines.append(
                f"| {name} | {entry.get('label')} | {entry.get('denominator')} | "
                f"{entry.get('sample_note') or ''} |"
            )
        taxonomy = answer.get("failure_taxonomy") or {}
        if taxonomy.get("primary"):
            lines.append("")
            lines.append("失败自动归类（主因）：")
            for category, count in taxonomy["primary"].items():
                lines.append(f"- {category}：{count}")
        populations = answer.get("populations") or {}
        if any(population.get("runs") for population in populations.values()):
            lines.append("")
            lines.append("样本分组（不混合计分；mock 恢复不进核心总分）：")
            lines.append("")
            lines.append("| 组 | 任务数 | pass@1 | pass@k | pass^k |")
            lines.append("|---|---|---|---|---|")
            labels = {
                "core_financial_qa": "核心金融问答",
                "refusal_clarification": "拒答与澄清",
                "mock_recovery": "故障恢复 mock",
            }
            for name, population in populations.items():
                if not population.get("runs"):
                    continue
                lines.append(
                    f"| {labels.get(name, name)} | {population.get('tasks')} | "
                    f"{_pct(population.get('pass_at_1'))} | "
                    f"{_pct(population.get('pass_at_k'))} | "
                    f"{_pct(population.get('pass_pow_k'))} |"
                )
        style_breakdown = answer.get("prompt_style_breakdown") or {}
        if style_breakdown:
            lines.append("")
            lines.append(
                "prompt_style 分栏（direct_constraint 考指令执行，不混入开放场景能力）："
            )
            for style, entry in style_breakdown.items():
                lines.append(f"- {style}：{entry.get('label')} (n={entry.get('denominator')})")
        hops_breakdown = answer.get("retrieval_hops_breakdown") or {}
        if hops_breakdown:
            lines.append("")
            lines.append("retrieval_hops 分栏（1=单证据单元，2=跨证据单元）：")
            for hops, entry in hops_breakdown.items():
                lines.append(f"- hop {hops}：{entry.get('label')} (n={entry.get('denominator')})")

    numeric = report["layers"]["numeric"]
    lines.append("")
    lines.append("## 4. 数值审计")
    if numeric.get("status") != "ok":
        lines.append(f"- no_data：{numeric.get('note')}")
    else:
        lines.append(
            f"- 金标数值覆盖率 {_pct(numeric.get('gold_coverage'))} "
            f"（{numeric.get('gold_points')}）"
        )
        lines.append(
            f"- 裁判数值冲突率 {_pct(numeric.get('judge_numeric_conflict_rate'))} "
            f"（{numeric.get('hallucination_events')} 个事件）"
        )
        lines.append(f"- 未溯源数值率 {_pct(numeric.get('unsourced_rate'))}（提示项）")
        lines.append(f"- 数值证据缺口率 {_pct(numeric.get('unsupported_number_rate'))}"
                     f"（审计完整：{numeric.get('support_audit_complete') is True}；不认证语义归属）")
        if numeric.get("pii_hits"):
            lines.append(f"- PII 命中 {numeric.get('pii_hits')} 处（提示项，公开公司联系方式不算个人隐私）")

    trajectory = report["layers"]["trajectory"]
    lines.append("")
    lines.append("## 5. 轨迹层")
    if trajectory.get("status") != "ok":
        lines.append(f"- no_data：{trajectory.get('note')}")
    else:
        lines.append(
            f"- 轨迹通过 {_pct(trajectory.get('pass_rate'))}；"
            f"按序匹配 {_pct(trajectory.get('ordered_rate'))}；"
            f"关键工具 {_pct(trajectory.get('key_tools_rate'))}；"
            f"无效循环 {_pct(trajectory.get('invalid_loop_rate'))}"
        )

    faithfulness = report["layers"]["faithfulness"]
    lines.append("")
    lines.append("## 6. 忠实度（RAGAS）")
    if faithfulness.get("status") != "ok":
        lines.append(f"- no_data：{faithfulness.get('note')}")
    else:
        lines.append(
            f"- 均值 {faithfulness.get('faithfulness_mean')}；"
            f"微平均 {_pct(faithfulness.get('faithfulness_micro'))} "
            f"（{faithfulness.get('claims')}）"
        )

    gate = report["layers"]["gate"]
    lines.append("")
    lines.append("## 7. Release Gate")
    if gate.get("status") != "ok":
        lines.append(f"- no_data：{gate.get('note')}")
    else:
        overall = gate.get("overall_pass")
        overall_text = "未评估（缺可拦截项）" if overall is None else ("通过" if overall else "不通过")
        lines.append(f"- 总体：{overall_text}")
        lines.append(f"- 拦截失败：{gate.get('blocking_failures') or '无'}")
        lines.append(f"- 提示失败：{gate.get('advisory_failures') or '无'}")
        lines.append(f"- 金融红线：{', '.join(gate.get('red_lines') or []) or '未配置'}")

    diagnostics = report.get("diagnostics") or {}
    split = diagnostics.get("failure_split") or {}
    lines.append("")
    lines.append("## 8. 分岔诊断（失败题先过检索段）")
    if split.get("status") != "ok":
        lines.append("- no_data：需要同时有答案层报告与检索层结果")
    else:
        lines.append(f"- 检索 miss：{split['retrieval_miss']['count']} 条 → 修检索")
        lines.append(
            f"- 检索 hit 但答错/拒答：{split['retrieval_hit_but_wrong']['count']} 条 → 修生成/决策"
        )
        if split["retrieval_miss"]["tasks"]:
            lines.append(f"  - miss 任务：{', '.join(split['retrieval_miss']['tasks'])}")
        if split["retrieval_hit_but_wrong"]["tasks"]:
            lines.append(
                f"  - hit 但错的任务：{', '.join(split['retrieval_hit_but_wrong']['tasks'])}"
            )
    refusal = diagnostics.get("false_refusal") or {}
    if refusal.get("status") == "ok":
        lines.append(
            f"- 误拒：{refusal.get('false_refusals')} 条，其中检索 hit 却拒答 "
            f"{refusal.get('hit_but_refused')} 条"
        )

    lines.append("")
    lines.append("## 9. 读数口径")
    for note in report.get("reading_notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="三层评测报告（检索 × 路由 × 答案 × 闸门）")
    parser.add_argument("--retrieval", default="")
    parser.add_argument("--routing", default="")
    parser.add_argument("--answer-report", default="")
    parser.add_argument("--numeric", default="")
    parser.add_argument("--trajectory", default="")
    parser.add_argument("--faithfulness", default="")
    parser.add_argument("--gate", default="")
    parser.add_argument("--out-json", default="")
    parser.add_argument("--out-md", default="")
    args = parser.parse_args()

    retrieval = _load(args.retrieval)
    routing = _load(args.routing)
    answer = _load(args.answer_report)
    numeric = _load(args.numeric)
    trajectory = _load(args.trajectory)
    faithfulness = _load(args.faithfulness)
    gate = _load(args.gate)

    # answer report 里已经带了数值/轨迹汇总时，不必再单独传文件；
    # 只有 tasks>0 才采用，避免把空层的 {"tasks":0} 当成有数据。
    if numeric is None and ((answer or {}).get("numeric_layer") or {}).get("tasks"):
        numeric = answer["numeric_layer"]
    if trajectory is None and ((answer or {}).get("trajectory_layer") or {}).get("tasks"):
        trajectory = answer["trajectory_layer"]

    if gate is None and any([retrieval, routing, answer, numeric, trajectory, faithfulness]):
        from evaluation import release_gate

        metrics: dict = {}
        metrics.update(release_gate.metrics_from_report(answer))
        metrics.update(release_gate.metrics_from_retrieval(retrieval))
        metrics.update(release_gate.metrics_from_routing(routing))
        metrics.update(release_gate.metrics_from_numeric(numeric))
        metrics.update(release_gate.metrics_from_trajectory(trajectory))
        metrics.update(release_gate.metrics_from_faithfulness(faithfulness))
        gate = release_gate.evaluate(metrics)

    report = build_report(
        retrieval=retrieval,
        routing=routing,
        answer=answer,
        numeric=numeric,
        trajectory=trajectory,
        faithfulness=faithfulness,
        gate=gate,
    )
    markdown = render_markdown(report)

    print(markdown)

    if args.out_json:
        out_path = Path(args.out_json)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nJSON：{out_path}")
    if args.out_md:
        out_path = Path(args.out_md)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        print(f"Markdown：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
