"""金融查证 Agent 评测入口（只跑不判，判分走 score_agent_runs）。

用法（在 backend 目录下）：

    python -m evaluation.run_finance_eval --limit 3 --out %TEMP%\\fin_runs.json
    python -m evaluation.run_finance_eval --variant improved --runs 3
    python -m evaluation.run_finance_eval --tasks evaluation/finance_tasks_stress.jsonl

和 `run_agent_eval` 的差别只有一处：这里**自己构造 `FinanceToolset` 并复用同一个实例**。
`--search-mode blocks` 会把 Agent 的检索入口切到结构块索引；默认 `sections` 保持旧口径。
复用是为了省掉每次重读 7 份 document.json（约 1–2 秒 × 90 次）；代价是 `calcs`
与调用计数会跨任务累积，所以 runner 每条任务开跑前会调 `toolset.prepare()` 清一次
（`app/agent/runner.py` 里那三行注释说的就是这件事）。

`--holdout` 与判分脚本同名同义：调参阶段 exclude，最后验收 only。
读任务 / 跑批 / 汇总 / 打表 共用 `_runner_common`。
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

from app.agent.runner import (
    DEFAULT_MAX_STEPS,
    FINAL_MAX_TOKENS,
    IMPROVED_AUTO_RETRY_LIMIT,
    IMPROVED_MAX_TOOL_ROUNDS,
    IMPROVED_SELF_CHECK_LIMIT,
    MAX_STEPS_CEILING,
    REPAIR_ROUND_BUDGET,
    SUPPORTED_VARIANTS,
    TOOL_TEMPERATURE,
)
from app.agent.runner import (
    run as run_agent,
)
from app.config import MODEL_CONFIG, MODEL_PROVIDER
from app.finance.block_index import parameters as block_parameters
from app.finance.store import FinanceStore
from app.tools.finance_set import (
    MAX_CELL_CHARS,
    MAX_SEARCH_CALLS,
    MAX_SECTION_CHARS,
    MAX_TABLE_CHARS,
    FinanceToolset,
)
from evaluation._runner_common import (
    load_tasks,
    print_table,
    run_batch,
    sha256_file,
    summarize,
)
from evaluation.finance_corpus import DOCUMENT_DIR, corpus_hash, doc_inventory
from evaluation.provenance import build_run_manifest, git_provenance, sha256_text

DEFAULT_TASKS = "evaluation/finance_tasks.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description="金融查证 Agent 评测（只跑不判）")
    parser.add_argument("--tasks", default=DEFAULT_TASKS)
    parser.add_argument("--variant", default="improved", choices=SUPPORTED_VARIANTS)
    parser.add_argument("--runs", type=int, default=1, help="每条任务跑几次（pass^k 需要 ≥3）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--ids",
        default="",
        help="逗号分隔的 task id，只跑这些（4 题探针用，如 CX-01,CX-04,CX-07,CX-10）",
    )
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--retry-errors", type=int, default=1)
    parser.add_argument(
        "--holdout",
        choices=("include", "only", "exclude"),
        default="exclude",
        help="默认 exclude：日常跑不算留出集，最后验收显式用 only",
    )
    parser.add_argument("--search-mode", choices=("sections", "blocks"), default="sections",
                        help="Agent 检索入口：sections=原章节定位，blocks=结构块索引")
    parser.add_argument("--out", default="", help="结果 JSON 路径；空 = 不落盘")
    parser.add_argument(
        "--no-task-snapshot",
        action="store_true",
        help="不落盘实际任务集快照（默认落盘；仅用于临时探针，正式产物请勿关闭）",
    )
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    if not tasks_path.exists():
        raise SystemExit(f"任务文件不存在: {tasks_path}")

    tasks = load_tasks(tasks_path)
    if args.ids:
        wanted = {part.strip() for part in args.ids.split(",") if part.strip()}
        tasks = [task for task in tasks if str(task.get("id")) in wanted]
    if args.holdout != "include":
        keep = args.holdout == "only"
        tasks = [task for task in tasks if bool(task.get("holdout")) is keep]
    if args.limit > 0:
        tasks = tasks[: args.limit]
    if not tasks:
        raise SystemExit("筛完没有任务可跑")

    store = FinanceStore(DOCUMENT_DIR)
    toolset = FinanceToolset(store, search_mode=args.search_mode)
    inventory = doc_inventory()
    block_index_stats = toolset.toolbox.block_index.stats()
    # 取拼接后的最终 system prompt 做绑定（含 variant 与 blocks 模式的 .replace / 后缀）。
    # 只旁路记录，不改 runner 行为：runner 内部仍会自己调 toolset.system_prompt()。
    system_prompt_text = toolset.system_prompt(args.variant)

    async def _call(task: dict, variant: str, run_index: int) -> dict:
        return await run_agent(task, variant=variant, run_index=run_index, toolset=toolset)

    started = time.perf_counter()
    results, schema_problems = asyncio.run(
        run_batch(
            tasks,
            variant=args.variant,
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
    if summary["injection_untriggered_runs"]:
        print(f"! 有 {summary['injection_untriggered_runs']} 次注入没触发")
    if schema_problems:
        print(f"! schema 不合规 {len(schema_problems)} 次")
        for key, problems in list(schema_problems.items())[:5]:
            print(f"  {key}: {problems}")

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 把「筛选后实际跑的任务集」落盘到 runs 同目录，run_config 指向它。
        # 旧实现把 run_config.tasks_file 写成筛选前的 --tasks 路径（可能是
        # %TEMP% 下的合并临时文件），筛选（--ids/--holdout/--limit）之后
        # 路径与哈希都不再对应实际任务集，报告因此不可复现。
        # 快照与 runs 同生共死：正式产物落 docs/evidence/ 时自动进 git，
        # 临时探针写 %TEMP% 时不污染仓库。
        tasks_path_for_run = tasks_path
        if not args.no_task_snapshot:
            snapshot_path = out_path.with_suffix(".tasks.jsonl")
            snapshot_path.write_text(
                "\n".join(json.dumps(task, ensure_ascii=False) for task in tasks) + "\n",
                encoding="utf-8",
            )
            tasks_path_for_run = snapshot_path

        manifest = build_run_manifest(
            tasks_path=tasks_path_for_run,
            corpus_hash=corpus_hash(),
            corpus_stats={
                "version": "finance-7docs-v1",
                "documents": len(inventory),
            },
            index_snapshot={
                "kind": "structured_blocks",
                **block_index_stats,
                "parameters": block_parameters(),
            },
            chunking=block_parameters(),
            generation={
                "temperature": TOOL_TEMPERATURE,
                "final_max_tokens": FINAL_MAX_TOKENS,
                "default_max_steps": DEFAULT_MAX_STEPS,
                "max_steps_ceiling": MAX_STEPS_CEILING,
                "improved_max_tool_rounds": IMPROVED_MAX_TOOL_ROUNDS,
                "improved_auto_retry_limit": IMPROVED_AUTO_RETRY_LIMIT,
                "improved_self_check_limit": IMPROVED_SELF_CHECK_LIMIT,
                "repair_round_budget": REPAIR_ROUND_BUDGET,
                "max_search_calls": MAX_SEARCH_CALLS,
                "max_section_chars": MAX_SECTION_CHARS,
                "max_table_chars": MAX_TABLE_CHARS,
                "max_cell_chars": MAX_CELL_CHARS,
            },
            extra={
                "arm": "agent",
                "agent": {
                    "variant": args.variant,
                    "search_mode": toolset.search_mode,
                    "runs_per_task": max(1, args.runs),
                    "max_tool_rounds": toolset.max_tool_rounds,
                },
            },
        )
        payload = {
            "schema": "finance_agent_runs/v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "run_config": {
                "variant": args.variant,
                "runs_per_task": max(1, args.runs),
                "toolset": toolset.name,
                "search_mode": toolset.search_mode,
                "model": (MODEL_CONFIG.get("flash") or {}).get("model"),
                "model_provider": MODEL_PROVIDER,
                "corpus_version": "finance-7docs-v1",
                "tasks_file": str(tasks_path_for_run),
                "tasks_sha256": sha256_file(tasks_path_for_run),
                "tasks_source_file": str(tasks_path),
                "tasks_selection": {
                    "ids": args.ids or None,
                    "limit": args.limit or None,
                    "task_count": len(tasks),
                },
                "task_snapshot": not args.no_task_snapshot,
                "corpus_hash": corpus_hash(),
                "holdout": args.holdout,
                "elapsed_seconds": round(elapsed, 1),
                # 提示词绑定：三次格式探针的唯一变量就是 system prompt，
                # 但 git commit 在脏工作区里区分不出跑了哪一版。缺这个字段时
                # 按 AGENTS.md §9.2 标 binding: incomplete，不作可复现基线。
                "prompt_sha256": sha256_text(system_prompt_text),
                "prompt_variant": args.variant,
                "prompt_search_mode": toolset.search_mode,
                "prompt_chars": len(system_prompt_text),
                # 产物自带代码版本：不打开 manifest 也能判断这份 runs 属于哪个
                # commit、当时工作区是否 dirty。旧产物缺这个字段，只能靠文档转述，
                # 容易把「唯一的 runs」误读成「当前有效读数」（2026-11-04 复盘）。
                "git": git_provenance(),
            },
            "manifest": manifest,
            "corpus": inventory,
            "summary": summary,
            "schema_problems": schema_problems,
            "results": results,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"结果：{out_path}")
        if not args.no_task_snapshot:
            print(f"任务集快照：{tasks_path_for_run}")
        # 留出题使用登记：--holdout include/only 跑过的留出题记一笔，
        # 验收前用 `python -m evaluation.holdout_registry --check` 看有没有被消耗。
        holdout_ids = [str(task.get("id")) for task in tasks if task.get("holdout")]
        if holdout_ids:
            from evaluation.holdout_registry import record_usage

            record_usage(holdout_ids, out_path)

    return 0 if not schema_problems else 1


if __name__ == "__main__":
    sys.exit(main())
