"""轨迹评测：只看过程，不看答案。

方案 §3.6 说轨迹五档里本项目先用两档，这里落成三件可确定性判的事：

1. **按序匹配**：任务卡可以声明关键动作序列（如"拒答前必须已检索过"），
   trace 里必须按顺序出现。伪工具 `search`/`read` 分别匹配任一检索/读取工具。
2. **关键工具**：某些题必须调用某个工具（如派生数值必须 `calculate`），
   单独出指标，不混进答案分。
3. **无效循环**：同工具同参数 ≥3 次、重复跳过 ≥3 次、或预算用尽仍在重试。

没有声明 `required_sequence` / `key_tools` 的题，用两条默认纪律：

- 拒答题：回答前至少检索过一次（不能不看资料就拒答）；
- 要求引用的题：至少检索一次 + 至少读开一处原文（不能只定位不读）。

零模型调用，只读 trace。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation._runner_common import load_tasks
from evaluation.agent_metrics import ratio, wilson_interval
from evaluation.calc_schema import calc_specs
from evaluation.retrieval_metrics import READ_TOOLS, SEARCH_TOOLS

PSEUDO_TOOLS = {
    "search": SEARCH_TOOLS,
    "read": READ_TOOLS,
}


def _steps(result: dict) -> list[dict]:
    return [step for step in result.get("trace") or [] if step.get("tool") != "__final__"]


def tool_sequence(result: dict) -> list[str]:
    return [str(step.get("tool") or "") for step in _steps(result)]


def _matches(tool: str, requirement: str) -> bool:
    if requirement in PSEUDO_TOOLS:
        return tool in PSEUDO_TOOLS[requirement]
    return tool == requirement


def required_sequence(task: dict) -> list[str]:
    """任务卡声明优先；否则用两条默认纪律。"""
    explicit = task.get("required_sequence")
    if isinstance(explicit, list) and explicit:
        return [str(item.get("tool") if isinstance(item, dict) else item) for item in explicit]
    if task.get("should_abstain"):
        return ["search"]
    if task.get("citation_required"):
        return ["search", "read"]
    return []


def ordered_match(tools: Sequence[str], required: Sequence[str]) -> dict:
    """required 是否是 tools 的子序列（按顺序，不要求连续）。"""
    if not required:
        return {"ok": True, "missing": [], "matched_at": []}
    position = 0
    matched_at: list[int] = []
    missing: list[str] = []
    for requirement in required:
        found = None
        for index in range(position, len(tools)):
            if _matches(tools[index], requirement):
                found = index
                break
        if found is None:
            missing.append(requirement)
        else:
            matched_at.append(found)
            position = found + 1
    return {"ok": not missing, "missing": missing, "matched_at": matched_at}


def key_tools(task: dict) -> list[str]:
    explicit = task.get("key_tools")
    if isinstance(explicit, list):
        return [str(item) for item in explicit]
    points = task.get("answer_points") or []
    if calc_specs(task) or any(
        isinstance(point, dict) and point.get("derived") for point in points
    ):
        return ["calculate"]
    return []


def invalid_loops(result: dict, task: dict) -> dict:
    counts: dict[tuple[str, str], int] = {}
    for step in _steps(result):
        key = (
            str(step.get("tool") or ""),
            json.dumps(step.get("args") or {}, sort_keys=True, ensure_ascii=False),
        )
        counts[key] = counts.get(key, 0) + 1
    repeated = [
        {"tool": tool, "args": args, "count": count}
        for (tool, args), count in counts.items()
        if count >= 3
    ]
    usage = result.get("usage") or {}
    steps = int(usage.get("steps") or 0)
    max_steps = int(task.get("max_steps") or 0)
    note = str((result.get("trace") or [{}])[-1].get("note") or "")
    exhausted = bool(max_steps and steps >= max_steps and "budget_truncated" in note)
    duplicate_skipped = int(usage.get("duplicate_skipped") or 0)
    return {
        "repeated": repeated,
        "duplicate_skipped": duplicate_skipped,
        "steps": steps,
        "max_steps": max_steps or None,
        "budget_truncated": "budget_truncated" in note,
        "count": len(repeated) + (1 if duplicate_skipped >= 3 else 0) + (1 if exhausted else 0),
    }


def evaluate(task: dict, result: dict) -> dict:
    tools = tool_sequence(result)
    required = required_sequence(task)
    ordered = ordered_match(tools, required)
    loops = invalid_loops(result, task)
    required_key_tools = key_tools(task)
    missing_key_tools = [name for name in required_key_tools if name not in tools]
    steps = int((result.get("usage") or {}).get("steps") or 0)
    max_steps = task.get("max_steps")
    budget_ok = not (isinstance(max_steps, int) and steps > max_steps)

    return {
        "task_id": str(task.get("id")),
        "dimension": task.get("dimension"),
        "tools": tools,
        "required": required,
        "ordered_ok": ordered["ok"],
        "missing": ordered["missing"],
        "key_tools": required_key_tools,
        "key_tools_ok": not missing_key_tools,
        "key_tools_missing": missing_key_tools,
        "invalid_loops": loops["count"],
        "loop_detail": loops,
        "steps": steps,
        "max_steps": max_steps,
        "budget_ok": budget_ok,
        "passed": bool(
            ordered["ok"]
            and not missing_key_tools
            and loops["count"] == 0
            and budget_ok
        ),
    }


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"tasks": 0, "note": "没有可打分的运行"}
    ordered = sum(1 for row in rows if row["ordered_ok"])
    key_ok = sum(1 for row in rows if row["key_tools_ok"])
    loops = sum(1 for row in rows if row["invalid_loops"])
    budget = sum(1 for row in rows if row["budget_ok"])
    passed = sum(1 for row in rows if row["passed"])
    low, high = wilson_interval(passed, len(rows))
    tool_counts: dict[str, int] = {}
    for row in rows:
        for tool in row["tools"]:
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
    return {
        "tasks": len(rows),
        "passed": passed,
        "pass_rate": ratio(passed, len(rows)),
        "wilson": [round(low, 4), round(high, 4)],
        "ordered_ok": ordered,
        "ordered_rate": ratio(ordered, len(rows)),
        "key_tools_ok": key_ok,
        "key_tools_rate": ratio(key_ok, len(rows)),
        "invalid_loop_tasks": loops,
        "invalid_loop_rate": ratio(loops, len(rows)),
        "budget_ok": budget,
        "budget_ok_rate": ratio(budget, len(rows)),
        "tool_counts": dict(sorted(tool_counts.items(), key=lambda pair: -pair[1])),
        "failures": [
            {
                "task_id": row["task_id"],
                "missing": row["missing"],
                "key_tools_missing": row["key_tools_missing"],
                "invalid_loops": row["invalid_loops"],
                "budget_ok": row["budget_ok"],
            }
            for row in rows
            if not row["passed"]
        ],
        "definition": {
            "ordered": "required_sequence 是 trace 工具序列的子序列（按序不要求连续）",
            "key_tools": "任务声明的关键工具（或派生数值的 calculate）必须出现",
            "invalid_loop": "同工具同参数 ≥3 次 / 重复跳过 ≥3 次 / 预算用尽仍在重试",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="轨迹评测（按序匹配 / 关键工具 / 无效循环）")
    parser.add_argument("--runs", required=True)
    parser.add_argument("--tasks", default="evaluation/finance_tasks_complex.jsonl")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    runs_path = Path(args.runs)
    if not runs_path.is_absolute():
        runs_path = _BACKEND_DIR / runs_path
    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    payload = json.loads(runs_path.read_text(encoding="utf-8"))
    tasks = {str(task.get("id")): task for task in load_tasks(tasks_path)}
    rows = []
    for result in payload.get("results") or []:
        task = tasks.get(str(result.get("task_id")))
        if task is None:
            continue
        rows.append(evaluate(task, result))
    summary = aggregate(rows)
    summary["tasks_file"] = str(tasks_path)
    summary["runs_file"] = str(runs_path)

    print("轨迹评测：")
    print(
        f"  通过 {summary['passed']}/{summary['tasks']} "
        f"({(summary.get('pass_rate') or 0):.1%})   "
        f"按序 {summary['ordered_ok']}/{summary['tasks']}   "
        f"关键工具 {summary['key_tools_ok']}/{summary['tasks']}   "
        f"无效循环 {summary['invalid_loop_tasks']} 条"
    )
    for failure in summary["failures"]:
        print(f"  ! {failure['task_id']}: {failure}")

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({**summary, "rows": rows}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n结果：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
