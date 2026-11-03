"""把 `run_agent_eval` 的原始运行记录判成分数、剖面与门禁。

用法（在 backend 目录下）：

    # 先跑原始运行
    python -m evaluation.run_agent_eval --out %TEMP%\\agent_runs.json
    # 再判分（含 LLM judge）
    python -m evaluation.score_agent_runs --runs %TEMP%\\agent_runs.json --out %TEMP%\\agent_report.json
    # 只跑确定性检查（CI 第 1 层，零 LLM）
    python -m evaluation.score_agent_runs --runs ... --no-judge

退出码：0 = 硬门禁全过；1 = 有门禁红；2 = 输入/判分本身出错（不是模型的问题）。

职责只有"编排"：检查逻辑在 `agent_checks`，判分在 `agent_judge`，
折分与门禁在 `agent_report`。这一层不写业务规则，否则规则会散成两处。
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation import (
    failure_taxonomy,
    numeric_audit,
    retrieval_metrics,
    trajectory_eval,
)
from evaluation._runner_common import load_tasks
from evaluation.agent_checks import apply_equivalent_sources, collect_checks
from evaluation.agent_corpus import AGENT_CORPUS_VERSION, chunk_map
from evaluation.agent_judge import judge_task
from evaluation.agent_report import (
    aggregate,
    build_badcases,
    build_construct_notes,
    build_populations,
    build_resolution,
    dimension_verdict,
    hard_gates,
    render_console,
)
from evaluation.calc_schema import calc_specs
from evaluation.provenance import git_provenance, sha256_file

DEFAULT_TASKS = "evaluation/agent_tasks.jsonl"

PASS_CRITERIA = {
    "unit": "task",
    "pass": "verdict.outcome == 'pass'（确定性检查与 judge 均通过）",
    "not_pass": "status=answered 只表示流程产出了答案，不等于任务正确",
    "undefined": "na / invalid 不计入通过分母",
    "score_key": "cross.pass_at_1（每任务 1 次）；跨多次运行才看 pass^k",
}


def build_verdict(
    task: dict,
    result: dict,
    judge_result: dict | None,
    *,
    chunks: dict[str, dict] | None = None,
    tables: dict[str, dict] | None = None,
    routing: dict | None = None,
    section_map: dict[str, list[str]] | None = None,
) -> dict:
    checks = collect_checks(task, result, tables=tables)
    judged = dimension_verdict(task, result, checks, judge_result)
    judgment = (judge_result or {}).get("judgment")
    apply_equivalent_sources(checks, judgment)

    # 检索层：零模型调用，失败题先在这里分岔（miss vs hit-but-wrong）。
    try:
        retrieval = retrieval_metrics.score_task(
            task, result, section_map=section_map
        )
    except Exception as exc:  # noqa: BLE001 - 量具失败不能把整条判分拖垮
        retrieval = {"task_id": str(task.get("id")), "error": f"{type(exc).__name__}: {exc}"}

    # 轨迹层：按序匹配 / 关键工具 / 无效循环。
    try:
        trajectory = trajectory_eval.evaluate(task, result)
    except Exception as exc:  # noqa: BLE001
        trajectory = {"task_id": str(task.get("id")), "error": f"{type(exc).__name__}: {exc}"}

    # 数值审计：只在有金标数值的题上跑；没有 evidence 的制度库任务跳过。
    numeric = None
    has_numeric_gold = bool(calc_specs(task)) or any(
        isinstance(point, dict) and isinstance(point.get("evidence"), dict) and point["evidence"].get("value") is not None
        for point in task.get("answer_points") or []
    )
    if chunks is not None and has_numeric_gold:
        try:
            numeric = numeric_audit.audit_result(task, result, chunks, judgment=judgment)
        except Exception as exc:  # noqa: BLE001
            numeric = {"task_id": str(task.get("id")), "error": f"{type(exc).__name__}: {exc}"}

    taxonomy = failure_taxonomy.classify(
        task,
        result,
        checks,
        judgment,
        retrieval=retrieval,
        routing=routing,
        chunks=chunks,
    )

    return {
        "task_id": str(task.get("id")),
        "run_index": int(result.get("run_index") or 1),
        "dimension": judged["dimension"],
        "difficulty": task.get("difficulty"),
        "source": task.get("source"),
        "mock": bool(task.get("mock")),
        "outcome": judged["outcome"],
        "reasons": judged["reasons"],
        "causes": judged["causes"],
        "checks": checks,
        "judgment": judgment,
        "judge_error": (judge_result or {}).get("error"),
        "judge_agreement": (judge_result or {}).get("agreement"),
        # 这一条的正序/倒序判分记录：以后要比"forward vs reversed 的 pass 率差"
        # 直接从这里取，不用重跑
        "judge_orders": (judge_result or {}).get("orders"),
        # 三层评测新增：检索段 / 轨迹 / 数值审计 / 失败自动归类
        "retrieval": retrieval,
        "trajectory": trajectory,
        "numeric": numeric,
        "taxonomy": taxonomy,
        "routing": routing,
        # 用途标注元数据（2026-11-01）：报告按 prompt_style / retrieval_hops 分栏、
        # 按 mock 分组时从这里取；没标注的任务集这些字段是 None，分栏自动不渲染。
        "route_target": task.get("route_target"),
        "prompt_style": task.get("prompt_style"),
        "retrieval_hops": task.get("retrieval_hops"),
        "answer": result.get("answer"),
        "citations": result.get("citations"),
        "trace": result.get("trace"),
        "usage": result.get("usage"),
    }


async def score_all(
    tasks_by_id: dict[str, dict],
    results: list[dict],
    chunks: dict[str, dict],
    *,
    use_judge: bool,
    judge_repeats: int,
    concurrency: int,
    reuse_judgments: dict[tuple[str, int], dict] | None = None,
    routing_by_task: dict[str, dict] | None = None,
    section_map: dict[str, list[str]] | None = None,
    tables: dict[str, dict] | None = None,
) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _score_one(result: dict) -> dict:
        task_id = str(result.get("task_id"))
        task = tasks_by_id.get(task_id)
        if task is None:
            return {
                "task_id": task_id,
                "run_index": result.get("run_index"),
                "dimension": "",
                "outcome": "na",
                "reasons": [f"任务集里没有 {task_id}，可能是旧报告或任务集改过"],
                "causes": [],
                "checks": {},
                "judgment": None,
                "judge_error": None,
                "judge_agreement": None,
                "answer": result.get("answer"),
                "citations": result.get("citations"),
                "trace": result.get("trace"),
                "usage": result.get("usage"),
            }

        judge_result = None
        key = (task_id, int(result.get("run_index") or 1))
        reused = (reuse_judgments or {}).get(key)
        if reused is not None:
            # 重算确定性检查时不重复烧 judge 调用：judgment 与答案无关，
            # 只有 checks 会随 runner 的 trace 修正而变化。
            judge_result = {
                "ok": reused.get("judgment") is not None,
                "judgment": reused.get("judgment"),
                "error": reused.get("judge_error"),
                "repeats": 1,
                "agreement": reused.get("judge_agreement"),
                "usage": {},
            }
        elif use_judge:
            async with semaphore:
                judge_result = await judge_task(
                    task, result, chunks, repeats=judge_repeats
                )
        return build_verdict(
            task,
            result,
            judge_result,
            chunks=chunks,
            tables=tables,
            routing=(routing_by_task or {}).get(task_id),
            section_map=section_map,
        )

    return await asyncio.gather(*(_score_one(result) for result in results))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 运行结果判分")
    parser.add_argument("--runs", required=True, help="run_agent_eval 产出的 JSON")
    parser.add_argument("--tasks", default=DEFAULT_TASKS, help="任务 JSONL")
    parser.add_argument("--out", default="", help="报告 JSON 输出路径；空 = 只打印")
    parser.add_argument("--badcases", default="", help="badcase JSONL 输出路径；空 = 不写")
    parser.add_argument("--no-judge", action="store_true", help="只跑确定性检查（零 LLM）")
    parser.add_argument("--judge-repeats", type=int, default=1, help="每条运行判几次，>1 用来测 judge 自身一致率")
    parser.add_argument("--concurrency", type=int, default=3, help="judge 并发数")
    parser.add_argument("--limit", type=int, default=0, help="只判前 N 条运行结果")
    parser.add_argument(
        "--corpus",
        choices=("policy", "finance"),
        default="policy",
        help="judge 用哪套 chunk_id → 原文（金融这边现读 document.json）",
    )
    parser.add_argument(
        "--holdout",
        choices=("include", "only", "exclude"),
        default="include",
        help="留出集纪律：调参时 exclude，最后验收时 only，日常看总体用 include",
    )
    parser.add_argument(
        "--reuse-judgments",
        default="",
        help="复用旧报告的 judge 结论（只重算确定性检查时用），按 task_id+run_index 对齐",
    )
    parser.add_argument(
        "--no-routing",
        action="store_true",
        help="不跑路由审计（省掉一次 FinanceStore 加载；默认金融语料会跑）",
    )
    args = parser.parse_args()

    runs_path = Path(args.runs)
    if not runs_path.is_absolute():
        runs_path = _BACKEND_DIR / runs_path
    if not runs_path.exists():
        print(f"运行结果文件不存在: {runs_path}", file=sys.stderr)
        return 2

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    if not tasks_path.exists():
        print(f"任务文件不存在: {tasks_path}", file=sys.stderr)
        return 2

    payload = json.loads(runs_path.read_text(encoding="utf-8"))
    results = list(payload.get("results") or [])
    if args.limit > 0:
        results = results[: args.limit]
    if not results:
        print("运行结果里没有 results", file=sys.stderr)
        return 2

    tasks = load_tasks(tasks_path)
    if args.holdout != "include":
        keep = args.holdout == "only"
        tasks = [task for task in tasks if bool(task.get("holdout")) is keep]
    tasks_by_id = {str(task.get("id")): task for task in tasks}
    tables = None
    if args.corpus == "finance":
        from evaluation.finance_corpus import chunk_map as finance_chunk_map
        from evaluation.finance_corpus import table_map as finance_table_map

        chunks = finance_chunk_map()
        tables = finance_table_map()
    else:
        chunks = chunk_map()

    routing_by_task = None
    # 制度库没有「章节包含哪些表」的结构，传空表避免误读金融语料的 section 映射。
    section_map: dict[str, list[str]] = {}
    if args.corpus == "finance" and not args.no_routing:
        from app.finance.store import FinanceStore
        from evaluation import routing_metrics
        from evaluation.finance_corpus import DOCUMENT_DIR
        from evaluation.retrieval_metrics import section_tables

        store = FinanceStore(DOCUMENT_DIR)
        routing_by_task = {}
        for task in tasks:
            task_id = str(task.get("id"))
            expected, basis = routing_metrics.expected_arm(task)
            decision = routing_metrics.route(task, store)
            routing_by_task[task_id] = {
                "expected_arm": expected,
                "expected_basis": basis,
                "actual_arm": decision["strategy"],
                "route_reason": decision["reason"],
                "misrouted": expected is not None and decision["strategy"] != expected,
            }
        section_map = section_tables()

    reuse_judgments = None
    if args.reuse_judgments:
        reuse_path = Path(args.reuse_judgments)
        if not reuse_path.is_absolute():
            reuse_path = _BACKEND_DIR / reuse_path
        if not reuse_path.exists():
            print(f"旧报告不存在: {reuse_path}", file=sys.stderr)
            return 2
        previous = json.loads(reuse_path.read_text(encoding="utf-8"))
        reuse_judgments = {
            (str(row.get("task_id")), int(row.get("run_index") or 1)): row
            for row in previous.get("verdicts") or []
        }

    verdicts = asyncio.run(
        score_all(
            tasks_by_id,
            results,
            chunks,
            use_judge=not args.no_judge,
            judge_repeats=max(1, args.judge_repeats),
            concurrency=max(1, args.concurrency),
            reuse_judgments=reuse_judgments,
            routing_by_task=routing_by_task,
            section_map=section_map,
            tables=tables,
        )
    )

    # 老 payload 可能没有 runs_per_task；从 run_index 推断，避免把 3 次运行
    # 错算成"每任务 1 次"从而让 pass^k 整块消失。
    inferred_runs = max((int(result.get("run_index") or 1) for result in results), default=1)
    runs_per_task = max(1, int(payload.get("runs_per_task") or inferred_runs))
    aggregated = aggregate(
        verdicts,
        judge_repeats=max(1, args.judge_repeats),
        runs_per_task=runs_per_task,
    )
    gates = hard_gates(verdicts, aggregated["cross"])
    badcases = build_badcases(verdicts)
    # 金融那边的 runs 文件把出处放在 run_config 里（`finance_agent_runs/v1`），
    # 制度库放在顶层。两处都认，报告的抬头才不会变成一屏 None。
    run_config = payload.get("run_config") or {}
    variant = payload.get("variant") or run_config.get("variant")
    corpus_version = payload.get("corpus_version") or run_config.get("corpus_version")
    corpus_hash = payload.get("corpus_hash") or run_config.get("corpus_hash")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "variant": variant,
        "runs_file": str(runs_path),
        "runs_sha256": sha256_file(runs_path)[:16],
        "runs_per_task": payload.get("runs_per_task") or run_config.get("runs_per_task"),
        "tasks_file": str(tasks_path),
        "tasks_sha256": sha256_file(tasks_path)[:16],
        "corpus_version": corpus_version
        or ("finance" if args.corpus == "finance" else AGENT_CORPUS_VERSION),
        "corpus_hash": corpus_hash,
        "corpus_chunks": payload.get("corpus_chunks") or len(chunks),
        "corpus_docs": payload.get("corpus_docs") or len(payload.get("corpus") or []),
        "model": payload.get("model") or run_config.get("model"),
        "provider": payload.get("provider") or run_config.get("model_provider"),
        "agent_run_config": run_config or None,
        "run_manifest": payload.get("manifest"),
        "index_fingerprint": ((payload.get("manifest") or {}).get("index") or {}).get("fingerprint"),
        "git": git_provenance(),
        "pass_criteria": PASS_CRITERIA,
        "judge_enabled": not args.no_judge,
        "judge_reused_from": args.reuse_judgments or None,
        "judge_repeats": max(1, args.judge_repeats),
        "dimensions": aggregated["dimensions"],
        "cross": aggregated["cross"],
        "power_k": aggregated["power_k"],
        "resolution": build_resolution(verdicts, runs_per_task=runs_per_task),
        "hard_gates": gates,
        "failure_causes": aggregated["failure_causes"],
        "failure_cause_summary": aggregated["failure_cause_summary"],
        "failure_taxonomy": aggregated.get("failure_taxonomy"),
        "retrieval_layer": aggregated.get("retrieval_layer"),
        "trajectory_layer": aggregated.get("trajectory_layer"),
        "numeric_layer": aggregated.get("numeric_layer"),
        # 样本分组与元数据分栏（2026-11-01）：mock 恢复不进核心总分，
        # prompt_style / retrieval_hops 各自分开读数。
        "populations": build_populations(
            verdicts,
            judge_repeats=max(1, args.judge_repeats),
            runs_per_task=runs_per_task,
        ),
        "prompt_style_breakdown": aggregated.get("prompt_style_breakdown"),
        "retrieval_hops_breakdown": aggregated.get("retrieval_hops_breakdown"),
        "judge": aggregated["judge"],
        "construct_notes": build_construct_notes(variant, verdicts, runs_per_task=runs_per_task),
        "badcase_count": len(badcases),
        "badcases": badcases,
        "verdicts": verdicts,
    }

    print(render_console(report))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {out_path}")

    if args.badcases:
        badcase_path = Path(args.badcases)
        badcase_path.parent.mkdir(parents=True, exist_ok=True)
        with open(badcase_path, "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(badcase, ensure_ascii=False) + "\n" for badcase in badcases)
        print(f"badcase 已写入: {badcase_path}")

    return 0 if gates.get("overall_pass") else 1


if __name__ == "__main__":
    sys.exit(main())
