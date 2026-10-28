"""把一次补跑（patch）合并回原始运行文件（base）。

用途：全量跑到一半被上游限流打断时，只重跑失败的任务/运行，再把结果按
`(task_id, run_index)` 覆盖回原文件——不用为了一次 503 重烧 90 次调用。

用法（在 backend 目录下）：

    python -m evaluation.merge_agent_runs \
        --base docs/evidence/agent_improved_runs3.json \
        --patch %TEMP%/agent_improved_rerun.json \
        --out docs/evidence/agent_improved_runs3_merged.json

纪律：
- 只按 `(task_id, run_index)` 覆盖，不按位置覆盖；
- base 里没有的 task 追加到末尾；
- 合并后重新跑 schema 校验与 summary，不沿用旧 summary。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.tools.contract import validate_result
from evaluation.run_agent_eval import summarize


def _key(result: dict) -> tuple[str, int]:
    return (
        str(result.get("task_id") or ""),
        int(result.get("run_index") or 1),
    )


def merge_payloads(base: dict, patch: dict) -> dict:
    merged: dict[tuple[str, int], dict] = {}
    order: list[tuple[str, int]] = []
    for result in base.get("results") or []:
        key = _key(result)
        if key not in merged:
            order.append(key)
        merged[key] = result
    for result in patch.get("results") or []:
        key = _key(result)
        if key not in merged:
            order.append(key)
        merged[key] = result

    results = [merged[key] for key in order]
    schema_problems = {}
    for result in results:
        problems = validate_result(result)
        if problems:
            schema_problems[f"{result.get('task_id')}#{result.get('run_index')}"] = problems

    payload = dict(base)
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["merged_from"] = {
        "base_generated_at": base.get("generated_at"),
        "patch_generated_at": patch.get("generated_at"),
        "base_results": len(base.get("results") or []),
        "patch_results": len(patch.get("results") or []),
    }
    payload["schema_problems"] = schema_problems
    payload["summary"] = summarize(results, schema_problems)
    payload["results"] = results
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="合并 Agent 运行结果")
    parser.add_argument("--base", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    base_path = Path(args.base)
    patch_path = Path(args.patch)
    for path in (base_path, patch_path):
        if not path.is_absolute():
            path = _BACKEND_DIR / path
        if not path.exists():
            print(f"文件不存在: {path}", file=sys.stderr)
            return 2

    base = json.loads(base_path.read_text(encoding="utf-8"))
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    merged = merge_payloads(base, patch)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = _BACKEND_DIR / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(merged["summary"], ensure_ascii=False, indent=2))
    print(f"已写入: {out_path}")
    return 0 if not merged["schema_problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
