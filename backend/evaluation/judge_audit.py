"""judge 准确率审计：把"一致性"补成"正确性"。

报告里现成的 `mean_self_agreement` 只回答"同一个 judge 多跑几遍结论稳不稳"。
实测教训（业界公开实践也一致）：稳定性与正确性可以背离——一个稳定判错的 judge
一致率能到 80%，准确率只有 55%，而且**看起来比不稳的 judge 更可信**。
所以必须另做一件事：抽一批要点，人工标一次，算 judge 与人工的一致。

两步用法：

    # 1. 生成标注表（全是空格子，人工填 human_pass）
    python -m evaluation.judge_audit --report %TEMP%\\fin_smoke_judged.json \\
        --tasks evaluation/finance_tasks.jsonl --out %TEMP%\\judge_audit.jsonl

    # 2. 人工标完再比对
    python -m evaluation.judge_audit --compare %TEMP%\\judge_audit.jsonl

标注口径（写在 `human_pass` 里）：

| 值 | 含义 |
|---|---|
| `true` | 这个要点在答案里确实答到了，且引用支撑得住 |
| `false` | 没答到 / 答错 / 引用不支撑 |
| `skip` | 判断不了（例如答案被截断），不计入准确率 |

关键指标是 **判松（judge 放过、人工判错）**：它直接决定报告会不会高估 Agent 能力。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation.lint_agent_tasks import load_tasks


def build_rows(report: dict, tasks_by_id: dict[str, dict], limit: int = 0) -> list[dict]:
    rows: list[dict] = []
    for verdict in report.get("verdicts") or []:
        judgment = verdict.get("judgment")
        if not isinstance(judgment, dict):
            continue
        task = tasks_by_id.get(str(verdict.get("task_id"))) or {}
        points = task.get("answer_points") or []
        for point in judgment.get("points") or []:
            index = int(point.get("index") or 0)
            text = ""
            if 0 <= index < len(points):
                text = str((points[index] or {}).get("text") or "")
            rows.append(
                {
                    "task_id": verdict.get("task_id"),
                    "run_index": verdict.get("run_index"),
                    "dimension": verdict.get("dimension") or task.get("dimension"),
                    "point_index": index,
                    "point_text": text,
                    "judge_pass": bool(point.get("pass")),
                    "judge_cited": bool(point.get("cited")),
                    "judge_supports": bool(point.get("citation_supports")),
                    "judge_reason": point.get("reason") or "",
                    "forbidden_hit": judgment.get("forbidden_hit"),
                    "grounded": judgment.get("grounded"),
                    "human_pass": None,
                    "note": "",
                }
            )
    rows.sort(key=lambda row: (str(row["task_id"]), int(row["run_index"] or 1), row["point_index"]))
    return rows[:limit] if limit > 0 else rows


def _parse_label(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("true", "1", "yes", "y", "pass", "t"):
        return True
    if text in ("false", "0", "no", "n", "fail", "f"):
        return False
    return None


def compare(path: Path) -> int:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                print(f"{path}:{line_no} JSON 解析失败: {exc}", file=sys.stderr)
                return 2
            label = _parse_label(row.get("human_pass"))
            if label is None:
                continue
            row["_label"] = label
            rows.append(row)

    if not rows:
        print("没有可用的标注行（human_pass 填 true/false 才算）", file=sys.stderr)
        return 2

    judged = [row for row in rows if row.get("judge_pass") is not None]
    correct = sum(1 for row in judged if bool(row["judge_pass"]) == row["_label"])
    loose = [row for row in judged if row["judge_pass"] and not row["_label"]]
    strict = [row for row in judged if not row["judge_pass"] and row["_label"]]
    total = len(judged)

    print(f"标注点: {total}    一致: {correct}    准确率: {correct / total:.1%}")
    print(f"判松（judge 放过、人工判错）: {len(loose)}  ← 这一项决定报告会不会高估")
    print(f"判严（judge 判错、人工放过）: {len(strict)}")
    print()

    by_dimension: dict[str, list[dict]] = {}
    for row in judged:
        by_dimension.setdefault(str(row.get("dimension") or "未知"), []).append(row)
    print(f"{'维度':<10}{'点数':<7}{'准确率':<10}{'判松':<7}{'判严':<7}")
    print("-" * 42)
    for dimension, items in sorted(by_dimension.items()):
        hits = sum(1 for row in items if bool(row["judge_pass"]) == row["_label"])
        loose_n = sum(1 for row in items if row["judge_pass"] and not row["_label"])
        strict_n = sum(1 for row in items if not row["judge_pass"] and row["_label"])
        print(
            f"{dimension:<10}{len(items):<7}{hits / len(items):<10.1%}{loose_n:<7}{strict_n:<7}"
        )

    if loose or strict:
        print()
        print("需要人看的分歧点：")
        for row in (loose + strict)[:20]:
            direction = "判松" if row in loose else "判严"
            print(
                f"  [{direction}] {row.get('task_id')}#{row.get('run_index')} "
                f"point{row.get('point_index')} judge={row.get('judge_pass')} "
                f"human={row.get('_label')} | {str(row.get('point_text'))[:40]} | "
                f"{str(row.get('judge_reason'))[:40]}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="judge 准确率审计")
    parser.add_argument("--report", default="", help="score_agent_runs 产出的报告 JSON")
    parser.add_argument("--tasks", default="", help="任务 JSONL（用来回填要点原文）")
    parser.add_argument("--out", default="", help="标注表输出路径（JSONL）")
    parser.add_argument("--limit", type=int, default=0, help="只抽前 N 个要点")
    parser.add_argument("--compare", default="", help="对已标注的 JSONL 做比对")
    args = parser.parse_args()

    if args.compare:
        path = Path(args.compare)
        if not path.is_absolute():
            path = _BACKEND_DIR / path
        return compare(path)

    if not args.report:
        parser.error("要么给 --report 生成标注表，要么给 --compare 比对")

    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = _BACKEND_DIR / report_path
    if not report_path.exists():
        print(f"报告不存在: {report_path}", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))

    tasks_by_id: dict[str, dict] = {}
    if args.tasks:
        tasks_path = Path(args.tasks)
        if not tasks_path.is_absolute():
            tasks_path = _BACKEND_DIR / tasks_path
        tasks_by_id = {str(task.get("id")): task for task in load_tasks(tasks_path)}

    rows = build_rows(report, tasks_by_id, limit=args.limit)
    if not rows:
        print("报告里没有可审计的要点（没跑 judge？）", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else (report_path.with_suffix(".audit.jsonl"))
    if not out_path.is_absolute():
        out_path = _BACKEND_DIR / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("# human_pass 填 true / false / skip；note 写判断依据\n")
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"标注表 {len(rows)} 行：{out_path}")
    print("填完 human_pass 后跑：python -m evaluation.judge_audit --compare " + str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
