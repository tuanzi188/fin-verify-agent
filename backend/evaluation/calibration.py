"""判分器人工校准：抽一批判定，人工标一遍，算一致率和 Cohen's κ。

为什么需要
----------
判分器本身也是被测对象。它把一道题判成 pass，不代表这题真的过了——
如果它只会做字符串匹配，单位错、方向错、引用错都会漏。变异测试查的是
"错答案会不会被判过"，校准查的是"判过的答案里有多少是错的"。

这一步不能自动化：它需要一个人独立看一眼。工具只负责三件事：

1. 从真实 runs + report 里抽 N 条，导出评审表（CSV，人填 `human_pass`）；
2. 读回填好的表，算原始一致率与 Cohen's κ；
3. 列出分歧条目，供逐条回看。

口径
----
- 机器标签：`report.power_k.tasks[*].pass_at_1 >= 0.5`（与 compare_agent_reports 的默认口径一致）；
- 人工标签：1/0（也接受 true/false、是/否、pass/fail）；
- 未填写 `human_pass` 的行不参与统计，不当作"通过"；
- κ < 0.6 说明判分器与人不一致，先修判分器，别急着报数。

用法
----
    cd backend
    python -m evaluation.calibration \
        --runs docs/evidence/finance_answer_runs_20261103.json \
        --report docs/evidence/finance_answer_report_20261103.json \
        --tasks evaluation/finance_tasks.jsonl \
        --limit 20 --export review.csv

    # 人工填完 human_pass 后
    python -m evaluation.calibration --score review.csv --out calibration.json
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable

_TRUE_WORDS = {"1", "true", "yes", "y", "pass", "是", "通过", "对"}
_FALSE_WORDS = {"0", "false", "no", "n", "fail", "否", "不通过", "错"}


def _load_tasks(paths: Iterable[str]) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    for raw in paths:
        for line in Path(raw).read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            row = json.loads(text)
            tasks[str(row.get("id"))] = row
    return tasks


def _first_answer(results: list[dict]) -> dict[str, str]:
    """每个 task 取 run_index 最小的一次运行答案，保证导出可复现。"""
    answers: dict[str, str] = {}
    for result in sorted(results, key=lambda item: int(item.get("run_index") or 0)):
        task_id = str(result.get("task_id") or "")
        if task_id and task_id not in answers:
            answers[task_id] = str(result.get("answer") or "")
    return answers


def _machine_pass(report: dict) -> dict[str, bool]:
    rows = ((report.get("power_k") or {}).get("tasks")) or []
    labels: dict[str, bool] = {}
    for row in rows:
        if not row.get("complete", True):
            continue
        task_id = str(row.get("task_id") or "")
        if task_id:
            labels[task_id] = float(row.get("pass_at_1") or 0.0) >= 0.5
    return labels


def build_review_rows(
    runs: dict,
    report: dict,
    tasks: dict[str, dict],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """把 runs / report / 任务卡拼成评审行。"""
    answers = _first_answer(list(runs.get("results") or []))
    labels = _machine_pass(report)
    rows: list[dict[str, Any]] = []
    for task_id, answer in answers.items():
        if task_id not in labels:
            continue
        task = tasks.get(task_id) or {}
        rows.append(
            {
                "task_id": task_id,
                "dimension": str(task.get("dimension") or ""),
                "question": str(task.get("question") or ""),
                "machine_pass": 1 if labels[task_id] else 0,
                "human_pass": "",
                "answer": answer,
                "note": "",
            }
        )
    rows.sort(key=lambda row: row["task_id"])
    if limit is not None:
        rows = rows[: max(0, limit)]
    return rows


def write_review(rows: list[dict], path: str | Path) -> None:
    """导出 CSV。用 utf-8-sig，Excel 直接打开不乱码。"""
    fields = ["task_id", "dimension", "question", "machine_pass", "human_pass", "answer", "note"]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def _parse_label(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    if text in _TRUE_WORDS:
        return 1
    if text in _FALSE_WORDS:
        return 0
    return None


def read_review(path: str | Path) -> list[dict]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def cohen_kappa(pairs: list[tuple[int, int]]) -> float | None:
    """二分类 Cohen's κ；pe=1（全同类）时返回 None。"""
    total = len(pairs)
    if not total:
        return None
    agree = sum(1 for machine, human in pairs if machine == human)
    po = agree / total
    machine_pos = sum(1 for machine, _ in pairs if machine == 1) / total
    human_pos = sum(1 for _, human in pairs if human == 1) / total
    pe = machine_pos * human_pos + (1 - machine_pos) * (1 - human_pos)
    if pe >= 1:
        return None
    return (po - pe) / (1 - pe)


def score_review(rows: list[dict]) -> dict[str, Any]:
    """一致率 + κ + 混淆矩阵 + 分歧清单。"""
    pairs: list[tuple[int, int]] = []
    disagreements: list[dict] = []
    for row in rows:
        human = _parse_label(row.get("human_pass"))
        machine = _parse_label(row.get("machine_pass"))
        if human is None or machine is None:
            continue
        pairs.append((machine, human))
        if machine != human:
            disagreements.append(
                {
                    "task_id": row.get("task_id"),
                    "machine_pass": machine,
                    "human_pass": human,
                    "note": str(row.get("note") or ""),
                }
            )

    total = len(pairs)
    if not total:
        return {
            "labeled": 0,
            "agreement": None,
            "kappa": None,
            "confusion": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
            "disagreements": [],
            "note": "没有已标注的行；human_pass 留空的行不参与统计",
        }

    tp = sum(1 for machine, human in pairs if machine == 1 and human == 1)
    fp = sum(1 for machine, human in pairs if machine == 1 and human == 0)
    tn = sum(1 for machine, human in pairs if machine == 0 and human == 0)
    fn = sum(1 for machine, human in pairs if machine == 0 and human == 1)
    return {
        "labeled": total,
        "agreement": round((tp + tn) / total, 4),
        "kappa": None if cohen_kappa(pairs) is None else round(cohen_kappa(pairs), 4),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "disagreements": disagreements,
        "note": "kappa<0.6 时先修判分器，再报数",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判分器人工校准（导出/评分）")
    parser.add_argument("--runs", default=None, help="运行结果 JSON")
    parser.add_argument("--report", default=None, help="判分报告 JSON")
    parser.add_argument(
        "--tasks",
        action="append",
        default=None,
        help="任务卡路径，可重复；默认主集",
    )
    parser.add_argument("--limit", type=int, default=None, help="最多导出多少条")
    parser.add_argument("--export", default=None, help="评审表 CSV 输出路径")
    parser.add_argument("--score", default=None, help="读回已填写的评审表")
    parser.add_argument("--out", default=None, help="评分结果 JSON 输出路径")
    args = parser.parse_args(argv)

    if args.score:
        result = score_review(read_review(args.score))
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"结果：{args.out}")
        else:
            print(text)
        return 0 if result.get("labeled") else 1

    if not (args.runs and args.report):
        parser.error("导出需要 --runs 与 --report；评分用 --score")

    runs = json.loads(Path(args.runs).read_text(encoding="utf-8"))
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    tasks = _load_tasks(args.tasks or ["evaluation/finance_tasks.jsonl"])
    rows = build_review_rows(runs, report, tasks, limit=args.limit)

    if args.export:
        write_review(rows, args.export)
        print(f"评审表：{args.export}（{len(rows)} 条，请填写 human_pass）")
    else:
        for row in rows:
            print(f"{row['task_id']}\t机器={row['machine_pass']}\t{row['question']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
