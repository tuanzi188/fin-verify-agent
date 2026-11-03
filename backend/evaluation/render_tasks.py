"""把任务集 JSONL 渲染成可读 Markdown（评审/对账用，不做校验）。

背景：任务文件是一行一个 JSON，git diff 里改一个字段整行变红，评审时读不动。
本工具把题目、要点、证据坐标、calc、forbidden 渲染成 markdown——对账看这个，
机器校验仍走 `lint_finance_tasks`。

用法：
    python -m evaluation.render_tasks --tasks evaluation/finance_tasks.jsonl
    python -m evaluation.render_tasks --tasks evaluation/finance_tasks.jsonl \\
        --tasks evaluation/finance_tasks_complex.jsonl --out docs/tasks_review.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def render_task(task: dict, tier: str = "") -> str:
    lines: list[str] = []
    flags: list[str] = []
    for key in ("holdout", "mock", "should_abstain"):
        if task.get(key):
            flags.append(key)
    mode = task.get("clarify_mode")
    if mode and mode != "none":
        flags.append(f"clarify={mode}")
    heading = f"## {task.get('id')}"
    meta = "｜".join(
        str(item) for item in (tier, task.get("dimension"), task.get("difficulty")) if item
    )
    if meta:
        heading += f"（{meta}）"
    lines.append(heading)
    if flags:
        lines.append(f"- 标记：{', '.join(flags)}")
    if task.get("route_target"):
        lines.append(f"- 路由金标：{task['route_target']}")
    if task.get("token_budget"):
        lines.append(f"- token 预算：{task['token_budget']}")
    lines.append("")
    lines.append(f"> {task.get('question')}")
    lines.append("")

    points = task.get("answer_points") or []
    if points:
        lines.append("**答案要点**")
        for index, point in enumerate(points, 1):
            notes: list[str] = []
            evidence = point.get("evidence") or {}
            if evidence.get("table_id"):
                notes.append(
                    f"证据 {evidence['table_id']} "
                    f"{evidence.get('row')}/{evidence.get('column')} = {evidence.get('value')}"
                )
            if point.get("derived"):
                notes.append("派生")
            if point.get("kind"):
                notes.append(str(point["kind"]))
            suffix = f"（{'；'.join(notes)}）" if notes else ""
            lines.append(f"{index}. {point.get('text') or ''}{suffix}")
        lines.append("")

    calc = task.get("calc")
    if calc:
        specs = calc if isinstance(calc, list) else [calc]
        lines.append("**计算**")
        for spec in specs:
            if isinstance(spec, dict):
                lines.append(
                    f"- `{spec.get('formula')}` → {spec.get('expected')}"
                    f"（{spec.get('note') or '无 note'}）"
                )
        lines.append("")

    forbidden = task.get("forbidden") or []
    if forbidden:
        lines.append("**禁止**：" + "；".join(str(item) for item in forbidden))
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="任务集 Markdown 渲染")
    parser.add_argument("--tasks", action="append", default=[], help="任务 JSONL，可多次给")
    parser.add_argument("--ids", default="", help="逗号分隔的任务 id，过滤用")
    parser.add_argument("--out", default="", help="输出文件；默认打印到 stdout")
    args = parser.parse_args()
    if not args.tasks:
        parser.error("至少给一个 --tasks")
    wanted = {part.strip() for part in args.ids.split(",") if part.strip()}

    chunks: list[str] = []
    for raw in args.tasks:
        path = Path(raw)
        if not path.is_absolute():
            path = _BACKEND_DIR / path
        if not path.exists():
            raise SystemExit(f"任务文件不存在: {path}")
        tier = path.stem.replace("finance_tasks", "").strip("_") or "main"
        chunks.append(f"# {tier} · {path.name}\n")
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                task = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if wanted and str(task.get("id")) not in wanted:
                continue
            chunks.append(render_task(task, tier))

    text = "\n".join(chunks)
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"已写出：{out_path}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
