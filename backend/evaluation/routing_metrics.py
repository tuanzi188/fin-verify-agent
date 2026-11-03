"""路由层指标：`finance_router` 自动选路选对了没有。

产品默认路径是「简单查数走结构检索 / 复杂查证走多步 Agent」。路由错选有两种
代价完全不同的错误：

- **简单题 → Agent**：答得出来，但白烧 10 倍 token（成本错误）；
- **复杂题 → 结构检索**：省了钱，但拿到一次性检索的差答案（质量错误）。

这个模块用**任务卡里的金标元数据**判断"该走哪一臂"，不看 router 自己的关键词表——
否则就是拿 router 的实现去验证 router，永远 100%。金标来源按优先级：

1. 任务卡显式 `route_target`（`agent` / `structured_rag` / `indeterminate`）；
   旧 `expected_arm` 只为历史任务兼容；
2. 由任务结构推导：有派生计算 / 多公司 / 多期间 / 跨文档/跨表/对比标签 /
   multi_hop 类查询 → agent；干净的单跳单公司单期间 → structured_rag；
3. **不参与打分**：拒答、澄清、假设类任务——两臂都可能做对，硬判"更优臂"
   等于编金标；这类只报 router 的实际分流分布。

**第二种口径（可选）**：如果已经跑过 `compare_arms`，可以拿逐题实测胜负
（Agent 过/RAG 不过 → agent 更优；反之 → RAG 更优；同过同挂 → 平局）作为
经验金标。这时报告标 `basis=empirical_compare`，和设计金标分开列，不能混算。

零模型调用、零 judge：只读任务卡、router 与（可选的）既有 compare 结果。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.block_index import companies_for_question
from app.finance.store import FinanceStore
from app.services.finance_router import decide_strategy
from evaluation._runner_common import load_tasks
from evaluation.agent_metrics import ratio, wilson_interval

ARMS = ("structured_rag", "agent")

#: 查询类型本身就意味着多步/校验的，归 Agent。
AGENT_QUERY_TYPES = {
    "multi_hop",
    "complex",
}

#: 两臂都可能做对、硬判更优臂会编金标的，排除出准确率分母。
#:
#: - `abstain` / `clarify` / `assume`：两臂都能拒答/追问；
#: - `unit_trap` / `false_premise`：考的是生成时的单位与前提校验，不是检索步数；
#: - `recovery` / mock：故障是注入的，router 不可能从问题里预知；
#: - 其余未知类型：不猜。
EXCLUDED_QUERY_TYPES = {
    "abstain",
    "clarify",
    "assume",
    "unit_trap",
    "false_premise",
    "recovery",
}

_YEAR_PATTERN = re.compile(r"(20\d{2})\s*年")


def _years(question: str) -> list[int]:
    return sorted({int(value) for value in _YEAR_PATTERN.findall(str(question or ""))})


def expected_arm(task: dict) -> tuple[Optional[str], str]:
    """返回 `(expected_arm, basis)`；None 表示不参与准确率打分。

    金标只看**任务结构**，不看 router 的关键词表：

    - `single_hop` / `no_clarify_control` → 结构检索（除非问题点名多家公司/多个期间）；
    - `multi_hop` / `complex` → Agent；
    - `abstain` / `clarify` / `assume` / `unit_trap` / `false_premise` /
      `recovery` / mock → 不判更优臂。

    为什么不用标签（如"跨文档"）判：FIN-04 带"跨文档"标签，但答案是单张表里的
    一个数，属于 metadata 过滤能解决的检索，不是多步决策；拿标签判会把
    structured RAG 做对的题记成 router 错。
    """
    explicit_target = task.get("route_target")
    if explicit_target == "indeterminate":
        return None, "explicit:indeterminate"
    if explicit_target in ARMS:
        return explicit_target, "explicit:route_target"

    # 历史任务仍可能只有 expected_arm；新任务不再把它当作唯一金标字段。
    explicit_legacy = task.get("expected_arm")
    if explicit_legacy in ARMS:
        return explicit_legacy, "explicit:legacy_expected_arm"

    if task.get("mock"):
        return None, "excluded:mock_injection"

    query_type = str(task.get("query_type") or "")
    if task.get("should_abstain") or query_type in EXCLUDED_QUERY_TYPES:
        return None, f"excluded:{query_type or 'abstain'}"

    question = str(task.get("question") or "")
    multi_company = len(companies_for_question(question)) >= 2
    multi_period = len(_years(question)) >= 2

    if query_type in AGENT_QUERY_TYPES:
        return "agent", f"metadata:query_type:{query_type}"

    if query_type in ("single_hop", "no_clarify_control", ""):
        if multi_company:
            return "agent", "metadata:multi_company"
        if multi_period:
            return "agent", "metadata:multi_period"
        return "structured_rag", f"metadata:simple:{query_type or 'unspecified'}"

    # 未知 query_type：不猜
    return None, f"excluded:unknown_query_type:{query_type}"


def route(task: dict, store: FinanceStore) -> dict:
    strategy, reason = decide_strategy(str(task.get("question") or ""), store)
    return {"strategy": strategy, "reason": reason}


def _prf(confusion: dict[str, dict[str, int]], label: str) -> dict:
    true_positive = confusion.get(label, {}).get(label, 0)
    false_positive = sum(
        confusion.get(other, {}).get(label, 0) for other in ARMS if other != label
    )
    false_negative = sum(
        confusion.get(label, {}).get(other, 0) for other in ARMS if other != label
    )
    precision = ratio(true_positive, true_positive + false_positive)
    recall = ratio(true_positive, true_positive + false_negative)
    if true_positive == 0 and false_negative > 0:
        f1 = 0.0
    elif precision is None or recall is None or precision + recall == 0:
        f1 = 0.0 if (precision is not None and recall is not None) else None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "support": true_positive + false_negative,
    }


def evaluate(
    tasks: list[dict],
    *,
    store: Optional[FinanceStore] = None,
    include_holdout: bool = True,
) -> dict:
    """用任务卡金标跑一遍 router，输出准确率 / 混淆矩阵 / Macro-F1 / 错分代价。"""
    store = store or _default_store()
    rows: list[dict] = []
    excluded: list[dict] = []
    indeterminate: list[dict] = []
    for task in tasks:
        if not include_holdout and task.get("holdout"):
            continue
        task_id = str(task.get("id"))
        expected, basis = expected_arm(task)
        decision = route(task, store)
        row = {
            "task_id": task_id,
            "dimension": task.get("dimension"),
            "query_type": task.get("query_type"),
            "complex_type": task.get("complex_type"),
            "question": task.get("question"),
            "expected_arm": expected,
            "route_target": task.get("route_target"),
            "expected_basis": basis,
            "actual_arm": decision["strategy"],
            "route_reason": decision["reason"],
            "correct": None if expected is None else decision["strategy"] == expected,
        }
        if expected is None:
            if basis == "explicit:indeterminate":
                indeterminate.append(row)
            else:
                excluded.append(row)
        else:
            rows.append(row)

    confusion = {expected: {actual: 0 for actual in ARMS} for expected in ARMS}
    for row in rows:
        confusion[row["expected_arm"]][row["actual_arm"]] += 1

    correct = sum(1 for row in rows if row["correct"])
    low, high = wilson_interval(correct, len(rows)) if rows else (0.0, 0.0)
    per_class = {label: _prf(confusion, label) for label in ARMS}
    f1_values = [entry["f1"] for entry in per_class.values() if entry["f1"] is not None]
    macro_f1 = sum(f1_values) / len(f1_values) if f1_values else None

    errors = [row for row in rows if row["correct"] is False]
    for row in errors:
        row["cost_type"] = (
            "cost_waste:simple_to_agent"
            if row["expected_arm"] == "structured_rag"
            else "quality_risk:complex_to_rag"
        )

    by_query_type: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("query_type") or "unknown")
        entry = by_query_type.setdefault(key, {"tasks": 0, "correct": 0})
        entry["tasks"] += 1
        entry["correct"] += int(bool(row["correct"]))

    abstain_rows = [
        row for row in excluded if str(row.get("query_type")) == "abstain"
    ]
    return {
        "kind": "routing_metrics",
        "basis": "task_metadata",
        "definition": {
            "accuracy": "router 选中的臂 == 任务卡金标更优臂",
            "macro_f1": "两类（structured_rag / agent）F1 的算术平均",
            "excluded": "拒答/澄清/假设类不判更优臂；只报 router 实际分流分布",
            "indeterminate": "可回答但没有明确更优臂的边界题，不进入准确率分母；单独报实际分流",
            "cost_type": "simple→agent 记成本浪费；complex→rag 记质量风险",
        },
        "tasks": len(rows),
        "correct": correct,
        "accuracy": ratio(correct, len(rows)),
        "wilson": [round(low, 4), round(high, 4)],
        "macro_f1": round(macro_f1, 4) if macro_f1 is not None else None,
        "confusion": confusion,
        "per_class": per_class,
        "errors": errors,
        "error_count": len(errors),
        "cost_breakdown": {
            "simple_to_agent": sum(
                1 for row in errors if row["cost_type"].startswith("cost_waste")
            ),
            "complex_to_rag": sum(
                1 for row in errors if row["cost_type"].startswith("quality_risk")
            ),
        },
        "by_query_type": by_query_type,
        "excluded": excluded,
        "excluded_count": len(excluded),
        "indeterminate": indeterminate,
        "indeterminate_count": len(indeterminate),
        "indeterminate_router_distribution": {
            "tasks": len(indeterminate),
            "structured_rag": sum(
                1 for row in indeterminate if row["actual_arm"] == "structured_rag"
            ),
            "agent": sum(1 for row in indeterminate if row["actual_arm"] == "agent"),
        },
        "abstain_router_distribution": {
            "tasks": len(abstain_rows),
            "structured_rag": sum(
                1 for row in abstain_rows if row["actual_arm"] == "structured_rag"
            ),
            "agent": sum(1 for row in abstain_rows if row["actual_arm"] == "agent"),
        },
        "rows": rows,
    }


def evaluate_from_compare(
    compare_payload: dict,
    tasks: list[dict],
    *,
    store: Optional[FinanceStore] = None,
) -> dict:
    """用 compare_arms 的逐题实测胜负作为经验金标，量 router 的一致率。

    只统计两臂分出胜负的题；同过/同挂是平局，router 选哪边都不该扣分，
    单独报平局数。这是"事后组合"，不新增任何运行。
    """
    store = store or _default_store()
    tasks_by_id = {str(task.get("id")): task for task in tasks}
    pairs = compare_payload.get("pairs") or []
    rows: list[dict] = []
    ties = 0
    for pair in pairs:
        task = tasks_by_id.get(str(pair.get("task_id")))
        if task is None:
            continue
        agent_pass = bool((pair.get("agent") or {}).get("pass"))
        rag_pass = bool((pair.get("rag") or {}).get("pass"))
        if agent_pass == rag_pass:
            ties += 1
            continue
        expected = "agent" if agent_pass and not rag_pass else "structured_rag"
        decision = route(task, store)
        rows.append(
            {
                "task_id": pair.get("task_id"),
                "run_index": pair.get("run_index"),
                "expected_arm": expected,
                "actual_arm": decision["strategy"],
                "route_reason": decision["reason"],
                "correct": decision["strategy"] == expected,
            }
        )
    correct = sum(1 for row in rows if row["correct"])
    low, high = wilson_interval(correct, len(rows)) if rows else (0.0, 0.0)
    return {
        "kind": "routing_metrics",
        "basis": "empirical_compare",
        "tasks": len(rows),
        "ties_excluded": ties,
        "correct": correct,
        "accuracy": ratio(correct, len(rows)),
        "wilson": [round(low, 4), round(high, 4)],
        "errors": [row for row in rows if not row["correct"]],
        "rows": rows,
        "note": "只统计 compare_arms 中两臂分出胜负的题；平局不计入分母。",
    }


def _default_store() -> FinanceStore:
    from evaluation.finance_corpus import DOCUMENT_DIR

    return FinanceStore(DOCUMENT_DIR)


def _print_summary(summary: dict) -> None:
    print(f"路由准确率（basis={summary.get('basis')}）：")
    print(
        f"  {summary['correct']}/{summary['tasks']} = "
        f"{(summary.get('accuracy') or 0):.1%} "
        f"[{summary['wilson'][0]:.1%}, {summary['wilson'][1]:.1%}]   "
        f"Macro-F1 {summary.get('macro_f1')}"
    )
    confusion = summary.get("confusion") or {}
    if confusion:
        print("  混淆矩阵（行=金标，列=router）：")
        for expected in ARMS:
            counts = confusion.get(expected) or {}
            print(
                f"    {expected:<14}"
                + "  ".join(f"{actual}={counts.get(actual, 0)}" for actual in ARMS)
            )
    errors = summary.get("errors") or []
    if errors:
        print(f"  错分 {len(errors)} 条：")
        for row in errors:
            print(
                f"    {row['task_id']:<10} 金标 {row['expected_arm']:<14} "
                f"实际 {row['actual_arm']:<14} {row.get('cost_type') or ''}"
            )
            print(f"      理由：{row['route_reason']}")
    excluded = summary.get("excluded") or []
    if excluded:
        print(f"  不参与打分 {len(excluded)} 条（拒答/澄清/假设，两臂都可能对）：")
        distribution: dict[str, int] = {}
        for row in excluded:
            distribution[row["actual_arm"]] = distribution.get(row["actual_arm"], 0) + 1
        print(f"    实际分流：{distribution}")
    indeterminate = summary.get("indeterminate") or []
    if indeterminate:
        distribution: dict[str, int] = {}
        for row in indeterminate:
            distribution[row["actual_arm"]] = distribution.get(row["actual_arm"], 0) + 1
        print(f"  路由边界题 {len(indeterminate)} 条（不进入准确率分母）：")
        print(f"    实际分流：{distribution}")


def _load_tasks_spec(spec: str) -> tuple[list[dict], list[str]]:
    """支持逗号分隔的多个任务文件（主集/复杂集/压力集合起来看路由）。"""
    tasks: list[dict] = []
    paths: list[str] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        path = Path(part)
        if not path.is_absolute():
            path = _BACKEND_DIR / path
        if not path.exists():
            raise SystemExit(f"任务文件不存在: {path}")
        tasks.extend(load_tasks(path))
        paths.append(str(path))
    return tasks, paths


def main() -> int:
    parser = argparse.ArgumentParser(description="路由层指标（router 选臂一致率 + Macro-F1）")
    parser.add_argument(
        "--tasks",
        default="evaluation/finance_tasks.jsonl",
        help="任务 JSONL，可用逗号分隔多个文件",
    )
    parser.add_argument("--compare", default="", help="compare_arms 产出的 JSON（可选，用实测胜负做金标）")
    parser.add_argument("--include-holdout", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    tasks, task_paths = _load_tasks_spec(args.tasks)

    if args.compare:
        compare_path = Path(args.compare)
        if not compare_path.is_absolute():
            compare_path = _BACKEND_DIR / compare_path
        payload = json.loads(compare_path.read_text(encoding="utf-8"))
        summary = evaluate_from_compare(payload, tasks)
    else:
        summary = evaluate(tasks, include_holdout=args.include_holdout)

    summary["tasks_file"] = ",".join(task_paths)
    _print_summary(summary)

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n结果：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
