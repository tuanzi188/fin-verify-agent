"""评测入口共用的那一段：读任务、跑批、汇总、打表。

为什么单独抽出来：`run_finance_eval` 与 `run_rag_eval` 的跑批骨架是同一份代码
（load_tasks / run_all / summarize / print_table），差别只有"怎么跑一条"。
再来一个入口就是再抄一份，改一处忘两处。

**只抽不改**：这里的逻辑与原来逐行一致，包括打印文案与重试节奏。
`run_batch` 通过 `call` 回调拿"怎么跑一条"，域差异留在各自入口里。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
from pathlib import Path
from typing import Awaitable, Callable

from app.tools.contract import validate_result


def load_tasks(path: Path) -> list[dict]:
    """读 JSONL。空行与 `#` 开头的注释行跳过。"""
    tasks: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                task = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no} JSON 解析失败: {exc}") from exc
            if not isinstance(task, dict):
                raise SystemExit(f"{path}:{line_no} 必须是 JSON 对象")
            tasks.append(task)
    return tasks


async def run_batch(
    tasks: list[dict],
    *,
    variant: str,
    runs: int,
    call: Callable[[dict, str, int], Awaitable[dict]],
    delay_seconds: float = 0.0,
    retry_errors: int = 0,
) -> tuple[list[dict], dict]:
    """按任务 × 次数跑批，返回 (results, schema_problems)。

    `call(task, variant, run_index)` 由各域入口提供：制度库直接调 runner，
    金融文档把同一个 toolset 实例传进去（runner 每条任务前会 prepare 清账）。
    """
    results: list[dict] = []
    schema_problems: dict[str, list[str]] = {}
    total = len(tasks) * runs
    index = 0
    for task in tasks:
        for run_index in range(1, runs + 1):
            index += 1
            # 长跑必须可见：90 次运行中途卡住时，"没有输出"和"正在跑"要能分开。
            print(f"[{index}/{total}] {task.get('id')} run {run_index}/{runs}", flush=True)
            result = None
            for attempt in range(retry_errors + 1):
                result = await call(task, variant, run_index)
                if result["status"] != "error" or attempt >= retry_errors:
                    break
                note = str((result.get("trace") or [{}])[-1].get("note") or "")[:120]
                wait = 15 * (attempt + 1)
                print(f"  error，{wait}s 后重试（{attempt + 1}/{retry_errors}）: {note}", flush=True)
                await asyncio.sleep(wait)
            problems = validate_result(result)
            if problems:
                schema_problems[f"{result['task_id']}#{run_index}"] = problems
            results.append(result)
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
    return results, schema_problems


def summarize(results: list[dict], schema_problems: dict) -> dict:
    status_counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        status_counts[status] = status_counts.get(status, 0) + 1

    latencies = [int(result["usage"]["latency_ms"]) for result in results]
    steps = [int(result["usage"]["steps"]) for result in results]
    untriggered = sum(
        1 for result in results
        if any("injection_not_triggered" in str(step.get("note") or "") for step in result["trace"])
    )
    return {
        "total": len(results),
        "status_counts": status_counts,
        "median_latency_ms": int(statistics.median(latencies)) if latencies else 0,
        "max_latency_ms": max(latencies) if latencies else 0,
        "avg_steps": round(sum(steps) / len(steps), 2) if steps else 0,
        "injection_untriggered_runs": untriggered,
        "schema_problem_runs": len(schema_problems),
    }


def print_table(results: list[dict]) -> None:
    header = f"{'task':<10}{'run':<5}{'status':<11}{'steps':<7}{'latency':<9}{'cites':<7}answer"
    print(header)
    print("-" * len(header))
    for result in results:
        preview = str(result.get("answer") or "").replace("\n", " ")[:60]
        print(
            f"{str(result.get('task_id')):<10}{str(result.get('run_index')):<5}"
            f"{str(result.get('status')):<11}{str(result['usage']['steps']):<7}"
            f"{str(result['usage']['latency_ms']):<9}"
            f"{len(result.get('citations') or []):<7}{preview}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()
