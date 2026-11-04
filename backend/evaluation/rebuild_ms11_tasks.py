"""从三套任务卡按固定 id 列表合并 11 道多步题，供 11 题 blocks 子集复测使用。

顺序按 2026-11-03 runs 的 task_id 顺序，便于逐题对比。产物落盘到 docs/evidence/，
让 run_finance_eval 的 run_config.tasks_file 指向仓库内路径（可复现）。

用法（在 backend/ 下）：
    python -m evaluation.rebuild_ms11_tasks
"""

from __future__ import annotations

import json
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_DIR = _BACKEND_DIR.parent
_OUT = _REPO_DIR / "docs" / "evidence" / "ms11_tasks_20261104.jsonl"

#: 11 道多步拆解题，顺序与 docs/evidence/finance_answer_runs_20261103.json 一致
MS11_IDS = (
    "FIN-07", "FIN-08", "FIN-09", "FIN-11", "FIN-12",
    "CX-07", "CX-08",
    "FIN-S01", "FIN-S03", "FIN-S06", "FIN-S10",
)

_TASK_FILES = (
    "evaluation/finance_tasks.jsonl",
    "evaluation/finance_tasks_complex.jsonl",
    "evaluation/finance_tasks_stress.jsonl",
)


def load_all() -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for rel in _TASK_FILES:
        path = _BACKEND_DIR / rel
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            task = json.loads(line)
            by_id[str(task["id"])] = task
    return by_id


def build(by_id: dict[str, dict], ids=MS11_IDS) -> list[dict]:
    missing = [tid for tid in ids if tid not in by_id]
    if missing:
        raise SystemExit(f"任务卡里找不到这些 id: {missing}")
    return [by_id[tid] for tid in ids]


def main() -> int:
    by_id = load_all()
    tasks = build(by_id)
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n",
        encoding="utf-8",
    )
    print(f"写入 {len(tasks)} 题 -> {_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
