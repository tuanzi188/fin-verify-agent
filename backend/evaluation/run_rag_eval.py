"""普通 RAG 基线臂的评测入口（只跑不判，判分复用 score_agent_runs）。

用法（在 backend 目录下）：

    python -m evaluation.run_rag_eval --tasks evaluation/finance_tasks_complex.jsonl
    python -m evaluation.run_rag_eval --limit 2 --out %TEMP%\\rag_smoke.json

产出与 `run_finance_eval` 同 schema（`finance_agent_runs/v1`），所以两臂可以用
同一条 `score_agent_runs --corpus finance` 判分、按 task_id 逐题配对。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.config import MODEL_CONFIG, MODEL_PROVIDER
from evaluation import rag_corpus
from evaluation._runner_common import load_tasks, print_table, run_batch, sha256_file, summarize
from evaluation.finance_corpus import corpus_hash, doc_inventory
from evaluation.rag_baseline import TOP_K, answer_once

DEFAULT_TASKS = "evaluation/finance_tasks_complex.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description="普通 RAG 基线臂评测（只跑不判）")
    parser.add_argument("--tasks", default=DEFAULT_TASKS)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--retry-errors", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--out", default="", help="结果 JSON 路径；空 = 不落盘")
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    if not tasks_path.exists():
        raise SystemExit(f"任务文件不存在: {tasks_path}")

    tasks = load_tasks(tasks_path)
    if args.limit > 0:
        tasks = tasks[: args.limit]
    if not tasks:
        raise SystemExit("筛完没有任务可跑")

    async def _call(task: dict, variant: str, run_index: int) -> dict:
        return await answer_once(task, run_index=run_index, top_k=args.top_k, variant=variant)

    started = time.perf_counter()
    results, schema_problems = asyncio.run(
        run_batch(
            tasks,
            variant="rag",
            runs=max(1, args.runs),
            call=_call,
            delay_seconds=max(0.0, args.delay),
            retry_errors=max(0, args.retry_errors),
        )
    )
    elapsed = time.perf_counter() - started

    print()
    print_table(results)
    summary = summarize(results, schema_problems)
    print()
    print(
        f"共 {summary['total']} 次运行，用时 {elapsed:.0f}s；"
        f"状态 {summary['status_counts']}；"
        f"中位延迟 {summary['median_latency_ms']}ms；平均步数 {summary['avg_steps']}"
    )
    tokens = sum(int(r["usage"]["tokens_in"]) + int(r["usage"]["tokens_out"]) for r in results)
    print(f"token 合计 {tokens:,}（{tokens // max(len(results), 1):,}/次）")
    print(f"切片：{rag_corpus.stats()}")

    if args.out:
        payload = {
            "schema": "finance_agent_runs/v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "run_config": {
                "variant": "rag",
                "arm": "rag",
                "runs_per_task": max(1, args.runs),
                "toolset": "chunks",
                "top_k": args.top_k,
                "chunk_size": rag_corpus.CHUNK_SIZE,
                "chunk_overlap": rag_corpus.CHUNK_OVERLAP,
                "model": (MODEL_CONFIG.get("flash") or {}).get("model"),
                "model_provider": MODEL_PROVIDER,
                "corpus_version": "finance-7docs-v1",
                "tasks_file": str(tasks_path),
                "tasks_sha256": sha256_file(tasks_path),
                "corpus_hash": corpus_hash(),
                "holdout": "exclude",
                "elapsed_seconds": round(elapsed, 1),
            },
            "corpus": doc_inventory(),
            "summary": summary,
            "schema_problems": schema_problems,
            "results": results,
        }
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"结果：{out_path}")

    return 0 if not schema_problems else 1


if __name__ == "__main__":
    sys.exit(main())
