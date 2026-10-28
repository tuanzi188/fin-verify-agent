"""对比 baseline 与 improved 两份 Agent 报告。

用法（在 backend 目录下）：

    python -m evaluation.compare_agent_reports \
        --baseline docs/evidence/agent_baseline_report.json \
        --improved docs/evidence/agent_improved_report.json \
        --out docs/evidence/agent_compare.json

职责边界（与 §8.13 一致）：

- **只做整卷配对检验**：30 条任务的二元结果做 McNemar，分维度只报 delta 与样本量，
  **不出分维度 p 值**——每维 6 条，任何分维显著性都是噪声。
- 任务级二元结果默认按「多数运行通过」（pass@1 ≥ 0.5）。`--metric all`
  改成 pass^k 全过（更严），`--metric any` 改成至少一次通过（更松），口径必须写进报告。
- 成本差按每次运行的平均 token 与中位延迟算，不拿总量掩盖运行次数差异。
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation.agent_metrics import mcnemar_exact, wilson_interval


def _task_rows(report: dict) -> dict[str, dict]:
    power = report.get("power_k") or {}
    rows = power.get("tasks") or []
    return {str(row.get("task_id")): row for row in rows if row.get("complete")}


def _task_pass(row: dict, metric: str) -> bool:
    if metric == "all":
        return row.get("pass_pow_k") == 1.0
    if metric == "any":
        return (row.get("pass_at_1") or 0.0) > 0.0
    return (row.get("pass_at_1") or 0.0) >= 0.5


def _cost(report: dict) -> dict:
    cross = report.get("cross") or {}
    power = report.get("power_k") or {}
    runs = sum(int(row.get("runs") or 0) for row in power.get("tasks") or [])
    tokens = int(cross.get("tokens_in") or 0) + int(cross.get("tokens_out") or 0)
    return {
        "runs": runs,
        "tokens_per_run": round(tokens / runs, 1) if runs else None,
        "median_latency_ms": cross.get("median_latency_ms"),
        "max_latency_ms": cross.get("max_latency_ms"),
        "avg_steps": cross.get("avg_steps"),
    }


def compare_reports(
    baseline: dict,
    improved: dict,
    metric: str = "majority",
) -> dict:
    if metric not in ("majority", "all", "any"):
        raise ValueError(f"metric 非法: {metric!r}")

    base_rows = _task_rows(baseline)
    impr_rows = _task_rows(improved)
    common = sorted(set(base_rows) & set(impr_rows))
    only_base = sorted(set(base_rows) - set(impr_rows))
    only_impr = sorted(set(impr_rows) - set(base_rows))

    fixed = broke = 0
    table: list[dict] = []
    for task_id in common:
        base = _task_pass(base_rows[task_id], metric)
        impr = _task_pass(impr_rows[task_id], metric)
        if not base and impr:
            transition = "fixed"
            fixed += 1
        elif base and not impr:
            transition = "broke"
            broke += 1
        else:
            transition = "same"
        table.append({
            "task_id": task_id,
            "dimension": base_rows[task_id].get("dimension") or impr_rows[task_id].get("dimension"),
            "baseline_pass_at_1": base_rows[task_id].get("pass_at_1"),
            "improved_pass_at_1": impr_rows[task_id].get("pass_at_1"),
            "baseline_pass_pow_k": base_rows[task_id].get("pass_pow_k"),
            "improved_pass_pow_k": impr_rows[task_id].get("pass_pow_k"),
            "transition": transition,
        })

    p_value = mcnemar_exact(fixed, broke) if (fixed + broke) else 1.0

    def _overall(report: dict) -> dict:
        power = report.get("power_k") or {}
        overall = power.get("overall") or {}
        return {
            "pass_at_1": overall.get("pass_at_1"),
            "pass_pow_k": overall.get("pass_pow_k"),
            "complete_tasks": overall.get("complete_tasks"),
            "tasks": overall.get("tasks"),
            "k": power.get("k"),
        }

    baseline_power = baseline.get("power_k") or {}
    improved_power = improved.get("power_k") or {}
    by_dimension: dict[str, dict] = {}

    def _task_interval(entry: dict) -> tuple[Optional[dict], Optional[float]]:
        """任务级区间：每维 6 条任务，按"多数运行通过"折成二元后算 Wilson。

        不用报告里的运行级区间（18 次运行）——同一任务的运行不独立，会偏窄，
        用它来判"delta 是否超过 CI 宽度"会把噪声当成果。
        """
        rows = entry.get("task_rows") or []
        if not rows:
            return None, None
        passed = sum(1 for row in rows if (row.get("pass_at_1") or 0.0) >= 0.5)
        lower, upper = wilson_interval(passed, len(rows))
        return {"pass": passed, "total": len(rows), "interval": [lower, upper]}, upper - lower

    for dimension, entry in (baseline_power.get("by_dimension") or {}).items():
        other = (improved_power.get("by_dimension") or {}).get(dimension) or {}
        before = entry.get("pass_at_1")
        after = other.get("pass_at_1")
        delta = (after - before) if before is not None and after is not None else None
        base_interval, base_width = _task_interval(entry)
        impr_interval, _ = _task_interval(other)
        by_dimension[dimension] = {
            "tasks": entry.get("tasks"),
            "baseline_pass_at_1": before,
            "improved_pass_at_1": after,
            "delta": delta,
            "baseline_task_interval": base_interval,
            "improved_task_interval": impr_interval,
            # 阶段 3 完成判据：delta ≥ 该维 CI 宽度（任务级、多数运行口径）。
            "delta_exceeds_ci_width": (
                delta is not None and base_width is not None and delta >= base_width
            ),
            # 不设 p 值：n=6 时它只会制造假精确。
        }

    cost_base = _cost(baseline)
    cost_impr = _cost(improved)
    token_delta = None
    if cost_base["tokens_per_run"] and cost_impr["tokens_per_run"]:
        token_delta = round(
            (cost_impr["tokens_per_run"] - cost_base["tokens_per_run"])
            / cost_base["tokens_per_run"],
            3,
        )
    latency_delta = None
    if cost_base["median_latency_ms"] and cost_impr["median_latency_ms"]:
        latency_delta = round(
            (cost_impr["median_latency_ms"] - cost_base["median_latency_ms"])
            / cost_base["median_latency_ms"],
            3,
        )

    if p_value < 0.05 and fixed > broke:
        verdict = "improved 显著优于 baseline"
    elif p_value < 0.05 and broke > fixed:
        verdict = "improved 显著差于 baseline"
    else:
        verdict = "不可分辨（未达到显著差异）"

    return {
        "metric": metric,
        "metric_definition": {
            "majority": "任务级 pass@1 ≥ 0.5（多数运行通过）",
            "all": "任务级 pass^k = 1（k 次全过）",
            "any": "任务级 pass@1 > 0（至少一次通过）",
        }[metric],
        "tasks_compared": len(common),
        "only_in_baseline": only_base,
        "only_in_improved": only_impr,
        "baseline": _overall(baseline),
        "improved": _overall(improved),
        "by_dimension": by_dimension,
        "mcnemar": {
            "fixed": fixed,
            "broke": broke,
            "discordant_pairs": fixed + broke,
            "p_value": p_value,
            "significant": p_value < 0.05,
        },
        "cost": {
            "baseline": cost_base,
            "improved": cost_impr,
            "tokens_per_run_delta": token_delta,
            "latency_median_delta": latency_delta,
        },
        "verdict": verdict,
        "table": table,
        "note": "分维度只报 delta，不报 p 值：每维 6 条，分维显著性不可判定。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="对比 baseline / improved Agent 报告")
    parser.add_argument("--baseline", required=True, help="baseline 报告 JSON")
    parser.add_argument("--improved", required=True, help="improved 报告 JSON")
    parser.add_argument("--metric", default="majority", choices=("majority", "all", "any"))
    parser.add_argument("--out", default="", help="对比结果 JSON 输出路径")
    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    improved_path = Path(args.improved)
    for path in (baseline_path, improved_path):
        if not path.is_absolute():
            path = _BACKEND_DIR / path
        if not path.exists():
            print(f"报告不存在: {path}", file=sys.stderr)
            return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    improved = json.loads(improved_path.read_text(encoding="utf-8"))
    result = compare_reports(baseline, improved, metric=args.metric)

    print(f"对比口径: {result['metric_definition']}")
    print(f"任务数: {result['tasks_compared']}")
    print(
        f"pass@1: baseline {result['baseline']['pass_at_1']} → "
        f"improved {result['improved']['pass_at_1']}"
    )
    print(
        f"pass^{result['baseline'].get('k')}: baseline {result['baseline']['pass_pow_k']} → "
        f"improved {result['improved']['pass_pow_k']}"
    )
    print("分维度 delta:")
    for dimension, entry in result["by_dimension"].items():
        print(
            f"  {dimension:<10} {entry.get('baseline_pass_at_1')} → "
            f"{entry.get('improved_pass_at_1')} (delta {entry.get('delta')})"
        )
    mcnemar = result["mcnemar"]
    print(
        f"整卷配对 McNemar: 修好 {mcnemar['fixed']} / 弄坏 {mcnemar['broke']} "
        f"/ p={mcnemar['p_value']:.4f}"
    )
    cost = result["cost"]
    print(
        f"成本: tokens/run {cost['baseline']['tokens_per_run']} → "
        f"{cost['improved']['tokens_per_run']} (delta {cost['tokens_per_run_delta']}); "
        f"中位延迟 {cost['baseline']['median_latency_ms']} → "
        f"{cost['improved']['median_latency_ms']} ms (delta {cost['latency_median_delta']})"
    )
    print(f"结论: {result['verdict']}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
