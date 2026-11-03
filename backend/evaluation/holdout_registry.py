"""留出题使用登记：`--holdout only` 的验收读数只在"留出题没被调参跑过"时成立。

`holdout: true` 标记本身不防腐——跑过一次就失效，但没人记得。这里在
`run_finance_eval` 落盘后记一笔（task_id → 最近使用时间与 run 文件名），
验收前用 `--check` 看清单。

用法：
    python -m evaluation.holdout_registry --check
    （run_finance_eval 落盘时自动登记；--holdout exclude 不会记）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: 登记文件放在证据目录里（与 run 产物同处，跟仓库一起走）。
REGISTRY_PATH = _BACKEND_DIR.parent / "docs" / "evidence" / "holdout_usage.json"


def load_usage(path: Path | None = None) -> dict:
    target = Path(path) if path else REGISTRY_PATH
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def record_usage(
    task_ids: list[str], run_path: object, *, when: datetime | None = None, path: Path | None = None
) -> dict:
    """把本次跑过的留出题记进登记表；返回更新后的内容。"""
    target = Path(path) if path else REGISTRY_PATH
    usage = load_usage(path=target)
    stamp = (when or datetime.now().astimezone()).isoformat(timespec="seconds")
    for task_id in task_ids:
        entry = usage.setdefault(str(task_id), {"count": 0})
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["last_run"] = stamp
        entry["run"] = str(run_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(usage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return usage


def check_holdouts(task_paths: list[Path] | None = None, path: Path | None = None) -> list[str]:
    """返回人读的检查结果：已使用的留出题 + 尚未使用的留出题。"""
    usage = load_usage(path=path)
    lines: list[str] = []
    if usage:
        lines.append(f"已登记使用 {len(usage)} 条留出题：")
        for task_id, entry in sorted(usage.items()):
            lines.append(
                f"  - {task_id}: {entry.get('count', 0)} 次，最近 {entry.get('last_run')}（{entry.get('run')}）"
            )
    else:
        lines.append("登记表为空：没有留出题被记录使用过。")
    if task_paths:
        holdout_ids: list[str] = []
        for path_item in task_paths:
            for line in Path(path_item).read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                try:
                    task = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if task.get("holdout"):
                    holdout_ids.append(str(task.get("id")))
        unused = [task_id for task_id in holdout_ids if task_id not in usage]
        lines.append(f"未使用的留出题 {len(unused)} 条：{unused}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="留出题使用登记检查")
    parser.add_argument("--check", action="store_true", help="打印登记表与未使用留出题")
    parser.add_argument("--tasks", action="append", default=[], help="任务 JSONL，可多次给")
    args = parser.parse_args()
    if not args.check:
        parser.error("用 --check 查看登记")
    paths = [
        (Path(raw) if Path(raw).is_absolute() else _BACKEND_DIR / raw) for raw in args.tasks
    ]
    for line in check_holdouts(paths or None):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
