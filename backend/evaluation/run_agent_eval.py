"""Agent 评测入口（P1：只跑不判）。

用法（在 backend 目录下）：

    python -m evaluation.run_agent_eval --variant baseline --limit 2
    python -m evaluation.run_agent_eval --runs 2 --out %TEMP%\\agent_smoke.json

P1 的职责只有三件：把任务跑出 result、按 `app/tools/contract.py` 校验 schema、
把原始结果落盘。判分、聚合、剖面在 P3——现在出"分数"只会是假分数。

读任务 / 跑批 / 汇总 / 打表 与 `run_finance_eval` 共用 `_runner_common`；
本文件只剩"制度库这一域怎么跑一条"和"落盘要写哪些出处"。

退出码：0 = 全部 schema 通过；1 = 有 result 不符合 schema，或任务文件读不了。
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.agent.runner import (
    DEFAULT_MAX_STEPS,
    FINAL_MAX_TOKENS,
    SUPPORTED_VARIANTS,
    TOOL_TEMPERATURE,
    VARIANT_CONFIG,
    run as run_agent,
)
from app.config import MODEL_CONFIG, MODEL_PROVIDER
from evaluation import run_eval as RE
from evaluation._runner_common import load_tasks, print_table, run_batch, summarize
from evaluation.agent_corpus import (
    AGENT_CORPUS,
    AGENT_CORPUS_VERSION,
    INJECTIONS,
    corpus_hash,
    doc_inventory,
)

DEFAULT_TASKS = "evaluation/agent_tasks.jsonl"


async def _call(task: dict, variant: str, run_index: int) -> dict:
    return await run_agent(task, variant=variant, run_index=run_index)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 评测入口（P1：只跑不判）")
    parser.add_argument("--tasks", default=DEFAULT_TASKS, help="任务 JSONL 路径")
    parser.add_argument("--variant", default="baseline", choices=list(SUPPORTED_VARIANTS))
    parser.add_argument("--runs", type=int, default=1, help="每条任务跑几次")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条任务，0 = 全部")
    parser.add_argument("--ids", default="", help="只跑指定 id，逗号分隔，如 R1,V6")
    parser.add_argument("--out", default="", help="结果写入的 JSON 路径；空 = 只打印")
    parser.add_argument("--skip-index", action="store_true", help="跳过建索引（复用已有 data/ 索引）")
    parser.add_argument("--delay", type=float, default=0.0, help="每次运行之间等待秒数（限流时用）")
    parser.add_argument("--retry-errors", type=int, default=0, help="运行报错时重试次数（每次等 15s×n）")
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    if not tasks_path.exists():
        raise SystemExit(f"任务文件不存在: {tasks_path}")

    tasks = load_tasks(tasks_path)
    if args.ids.strip():
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        tasks = [task for task in tasks if str(task.get("id")) in wanted]
        missing = wanted - {str(task.get("id")) for task in tasks}
        if missing:
            raise SystemExit(f"--ids 里有任务不存在: {sorted(missing)}")
    if args.limit > 0:
        tasks = tasks[: args.limit]
    if not tasks:
        raise SystemExit("没有可跑的任务")

    runs = max(1, args.runs)

    if args.skip_index:
        results, schema_problems = asyncio.run(
            run_batch(
                tasks,
                variant=args.variant,
                runs=runs,
                call=_call,
                delay_seconds=max(0.0, args.delay),
                retry_errors=max(0, args.retry_errors),
            )
        )
    else:
        # 不用 TemporaryDirectory：Windows 上 Chroma 仍持有 data_level0.bin，
        # 退出 with 时会抛 WinError 32 把整次运行带走。与 run_eval.py 同口径，
        # 用 mkdtemp + ignore_errors 清理，残留目录交给系统临时目录管理。
        index_root = Path(tempfile.mkdtemp(prefix="agent_eval_"))

        async def _run_with_index() -> tuple[list[dict], dict]:
            await RE._build_real_indexes(index_root, corpus=AGENT_CORPUS)
            return await run_batch(
                tasks,
                variant=args.variant,
                runs=runs,
                call=_call,
                delay_seconds=max(0.0, args.delay),
                retry_errors=max(0, args.retry_errors),
            )

        try:
            results, schema_problems = asyncio.run(_run_with_index())
        finally:
            shutil.rmtree(index_root, ignore_errors=True)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "variant": args.variant,
        "tasks_file": str(tasks_path),
        "corpus_version": AGENT_CORPUS_VERSION,
        "corpus_hash": corpus_hash(),
        "corpus_chunks": len(AGENT_CORPUS),
        "corpus_docs": len(doc_inventory()),
        "injections": [item["doc_id"] for item in INJECTIONS],
        "provider": MODEL_PROVIDER,
        "model": MODEL_CONFIG["flash"]["model"],
        "runs_per_task": runs,
        # 生成参数显式落盘：别人要复现这批数字，必须知道温度与 token 上限，
        # 不能靠"客户端默认值应该是 0.1 吧"这种猜测。
        "run_config": {
            "reasoning_mode": "flash",
            "temperature": TOOL_TEMPERATURE,
            "final_max_tokens": FINAL_MAX_TOKENS,
            "default_max_steps": DEFAULT_MAX_STEPS,
            "max_steps_source": "task.max_steps（缺省时用 default_max_steps）",
            "variant_policy": VARIANT_CONFIG.get(args.variant, {}),
        },
        "schema_problems": schema_problems,
        "summary": summarize(results, schema_problems),
        "results": results,
    }

    print_table(results)
    print()
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    if schema_problems:
        print()
        print("schema 问题：")
        for key, problems in schema_problems.items():
            print(f"  {key}: {'; '.join(problems)}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入: {out_path}")

    return 1 if schema_problems else 0


if __name__ == "__main__":
    sys.exit(main())
