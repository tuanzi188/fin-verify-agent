"""数据污染检查：闭卷答对了，说明模型记得，不是查到的。

为什么需要
----------
年报是公开数据，模型很可能在预训练里见过。如果它闭卷就能答对，
那么所有"正确率"读数都可能来自记忆，而不是检索与推理。

做法是同一批题跑两次：

| 组 | 提示里给不给资料 | 期望 |
|---|---|---|
| **闭卷** | 不给 | 答对率应接近 0 |
| **开卷** | 给 | 答对率应显著更高 |

闭卷命中超过阈值（默认 0）说明**答案来源不在模型知识里**——这批是虚构公司与虚构数值，
模型没有渠道知道。所以命中只可能来自三条泄漏路径：

1. 金标被拼进了提示（任务卡误入 system / few-shot）；
2. 语料或索引混入了评测文件，检索直接拿到答案；
3. 缓存回放了历史答案（会话缓存 / 答案缓存 / prompt 缓存）。

它**不检测**模型对公开年报的预训练记忆——那需要反事实语料（见文末）。

当前夹具的性质（必须说清）
--------------------------
`contamination_tasks.jsonl` 是**合成公司与虚构数值**（星原精工、澄海微电…），
本地语料里没有。它的用途是守住两条底线：

1. **金标不能泄漏进提示**——模型没有任何渠道知道 812,345,678 这个数；
2. **闭卷不能靠猜**——这种量级的数猜不中。

它**不能**替代"真实年报的预训练污染"检测。那一项需要**反事实语料**：
把原文里的数字改掉再问，记忆型模型会答原值、检索型模型会答改后的值。
反事实语料是后续工作，本轮不做。

用法
----
    cd backend
    python -m evaluation.contamination_check \
        --closed %TEMP%\\contamination_closed.json \
        --retrieval %TEMP%\\contamination_open.json \
        --out %TEMP%\\contamination_report.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from evaluation.equivalence import value_in_text


def load_tasks(paths: Iterable[str]) -> list[dict]:
    tasks: list[dict] = []
    for raw in paths:
        for line in Path(raw).read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            tasks.append(json.loads(text))
    return tasks


def _gold_values(task: dict) -> list[str]:
    values: list[str] = []
    for point in task.get("answer_points") or []:
        if not isinstance(point, dict):
            continue
        evidence = point.get("evidence")
        if isinstance(evidence, dict) and evidence.get("value") is not None:
            unit = str(point.get("unit") or evidence.get("unit") or "")
            values.append(f"{evidence['value']} {unit}".strip())
    return values


def _first_results(runs: dict) -> dict[str, dict]:
    picked: dict[str, dict] = {}
    for result in sorted(
        runs.get("results") or [], key=lambda item: int(item.get("run_index") or 0)
    ):
        task_id = str(result.get("task_id") or "")
        if task_id and task_id not in picked:
            picked[task_id] = result
    return picked


def _tool_calls(result: dict) -> int:
    steps = result.get("trace") or []
    return sum(1 for step in steps if isinstance(step, dict) and step.get("tool"))


def hit_task(task: dict, result: dict | None) -> dict[str, Any]:
    """答案里是否出现金标数值。没有运行结果时返回 hit=None。"""
    if not result:
        return {"hit": None, "tool_calls": 0, "answer": ""}
    answer = str(result.get("answer") or "")
    values = _gold_values(task)
    matched = next(
        (value for value in values if value_in_text(value, answer).get("match")), ""
    )
    return {
        "hit": bool(matched) if values else None,
        "matched_value": matched,
        "tool_calls": _tool_calls(result),
        "answer": answer,
    }


def build_report(
    tasks: list[dict],
    closed_runs: dict | None,
    retrieval_runs: dict | None,
    *,
    threshold: int = 0,
) -> dict[str, Any]:
    closed = _first_results(closed_runs or {})
    retrieval = _first_results(retrieval_runs or {})
    rows: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("id"))
        closed_hit = hit_task(task, closed.get(task_id))
        open_hit = hit_task(task, retrieval.get(task_id))
        rows.append(
            {
                "task_id": task_id,
                "question": task.get("question"),
                "closed_hit": closed_hit["hit"],
                "closed_tool_calls": closed_hit["tool_calls"],
                "retrieval_hit": open_hit["hit"],
                "matched_value": closed_hit.get("matched_value") or open_hit.get("matched_value"),
            }
        )

    closed_judged = [row for row in rows if row["closed_hit"] is not None]
    open_judged = [row for row in rows if row["retrieval_hit"] is not None]
    closed_hits = sum(1 for row in closed_judged if row["closed_hit"])
    open_hits = sum(1 for row in open_judged if row["retrieval_hit"])
    zero_tool_hits = sum(
        1 for row in closed_judged if row["closed_hit"] and row["closed_tool_calls"] == 0
    )

    suspicious = closed_hits > threshold
    return {
        "tasks": len(rows),
        "closed_judged": len(closed_judged),
        "retrieval_judged": len(open_judged),
        "closed_hits": closed_hits,
        "retrieval_hits": open_hits,
        "closed_zero_tool_hits": zero_tool_hits,
        "threshold": threshold,
        "suspicious": suspicious,
        "rows": rows,
        "note": (
            "闭卷命中>阈值即为可疑：金标泄漏进提示、语料混入评测文件，或缓存回放。"
            "虚构数据测不到预训练记忆；真实年报的污染需要反事实语料，本轮未做。"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据污染检查（闭卷 vs 开卷）")
    parser.add_argument(
        "--tasks", default="evaluation/contamination_tasks.jsonl", help="污染对照题 JSONL"
    )
    parser.add_argument("--closed", default=None, help="闭卷运行结果 JSON")
    parser.add_argument("--retrieval", default=None, help="开卷运行结果 JSON")
    parser.add_argument("--threshold", type=int, default=0, help="闭卷命中容忍数，默认 0")
    parser.add_argument("--out", default=None, help="报告 JSON 输出路径")
    args = parser.parse_args(argv)

    tasks = load_tasks([args.tasks])
    closed = (
        json.loads(Path(args.closed).read_text(encoding="utf-8")) if args.closed else None
    )
    retrieval = (
        json.loads(Path(args.retrieval).read_text(encoding="utf-8"))
        if args.retrieval
        else None
    )
    report = build_report(tasks, closed, retrieval, threshold=args.threshold)

    print(
        f"题：{report['tasks']}　闭卷命中：{report['closed_hits']}/{report['closed_judged']}"
        f"　开卷命中：{report['retrieval_hits']}/{report['retrieval_judged']}"
    )
    if report["suspicious"]:
        print("可疑：闭卷命中超过阈值，先排查金标泄漏或记忆，再报数。")
        for row in report["rows"]:
            if row["closed_hit"]:
                print(f"  {row['task_id']} tool_calls={row['closed_tool_calls']}")
    else:
        print("闭卷未命中（或未跑），符合预期。")

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告：{args.out}")
    return 1 if report["suspicious"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
