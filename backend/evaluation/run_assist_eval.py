"""辅助问答跑批入口：概念题 + 六类拒答。只跑不判。

两条路径刻意分开：

- `refusal` 全零模型：调 `initial_decision` / `prepare_finance_scope` 得到
  `reason_code`，或用 `synthetic_issues` 走 `classify_issues`，再取
  `refusal.payload`。所以六类拒答可以在 CI/无网环境完整跑通。
- `concept` 调产品同一个 `answer_concept` 入口。带 `must_web` 的费率题必须有
  `--replay` 快照；没有快照就记 `invalid`，**不拿模型记忆冒充可核对答案**。

产出边界（与金融域一致）：
- 任务集筛选后落 `<out>.tasks.jsonl`；
- `run_config` 记录 `tasks_sha256` / `fixture_sha256` / `git` / `model`；
- 辅助问答不使用本地语料，`corpus_hash` 明确写 `n/a`，不伪造。

用法：
    python -m evaluation.run_assist_eval --suite refusal --out %TEMP%\\refusal.json
    python -m evaluation.run_assist_eval --suite concept --replay evaluation/fixtures/assist_web_20261107.json --out %TEMP%\\concept.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.refusal import classify_issues, from_rule_id, payload
from app.finance.routing_policy import initial_decision
from app.services.advice_qa import answer_advice
from app.services.concept_qa import answer_concept
from app.services.web_search import WebSearchBudget

from evaluation import assist_fixtures, assist_schema
from evaluation._runner_common import load_tasks, sha256_file
from evaluation.provenance import git_provenance, model_snapshot

DEFAULT_CONCEPT = _BACKEND_DIR / "evaluation" / "assist_tasks_concept.jsonl"
DEFAULT_REFUSAL = _BACKEND_DIR / "evaluation" / "assist_tasks_refusal.jsonl"
DEFAULT_ADVICE = _BACKEND_DIR / "evaluation" / "assist_tasks_advice.jsonl"

RUNS_SCHEMA = "assist_qa_runs/v1"


def _resolve(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _BACKEND_DIR / candidate


def _status_for_path(initial_path: str) -> str:
    if initial_path == "clarify":
        return "clarified"
    if initial_path == "abstain":
        return "abstained"
    return "answered"


def _usage(*, latency_ms: int = 0, llm_calls: int = 0, tokens_in: int = 0,
           tokens_out: int = 0, web_queries: int = 0) -> dict[str, int]:
    return {
        "latency_ms": int(latency_ms),
        "llm_calls": int(llm_calls),
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "web_queries": int(web_queries),
    }


def _empty_result(task: dict, run_index: int) -> dict[str, Any]:
    return {
        "task_id": str(task.get("id")),
        "suite": str(task.get("suite")),
        "run_index": int(run_index),
        "status": "error",
        "answer": "",
        "notes": [],
        "web_sources": [],
        "refusal": None,
        "route_reason": "",
        "resolved_query": str(task.get("question") or ""),
        "disclaimer": "",
        "advisory": None,
        "usage": _usage(),
        "invalid_reason": None,
    }


def run_refusal_task(task: dict, run_index: int) -> dict[str, Any]:
    """零模型：路由类走 initial_decision，证据类走 classify_issues。"""
    started = time.perf_counter()
    result = _empty_result(task, run_index)
    question = str(task.get("question") or "")
    issues = task.get("synthetic_issues") or []

    if issues:
        code = classify_issues(list(issues))
        result.update(
            {
                "status": "abstained",
                "answer": payload(code).get("hint") or "",
                "refusal": payload(code),
                "route_reason": "synthetic_issues",
                "usage": _usage(latency_ms=int((time.perf_counter() - started) * 1000)),
            }
        )
        return result

    decision = initial_decision(
        question,
        assist_fixtures.DEMO_DOCUMENTS,
        str(task.get("previous_query") or ""),
    )
    code = decision.reason_code or from_rule_id(decision.rule_id)
    result.update(
        {
            "status": _status_for_path(decision.initial_path),
            "answer": decision.reason,
            "refusal": payload(code) if code else None,
            "route_reason": decision.rule_id,
            "resolved_query": decision.resolved_query,
            "usage": _usage(latency_ms=int((time.perf_counter() - started) * 1000)),
        }
    )
    return result


async def run_concept_task(
    task: dict,
    run_index: int,
    *,
    provider=None,
    chat=None,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = _empty_result(task, run_index)
    question = str(task.get("question") or "")

    decision = initial_decision(question, assist_fixtures.DEMO_DOCUMENTS)
    if decision.initial_path != "concept":
        code = decision.reason_code or from_rule_id(decision.rule_id)
        result.update(
            {
                "status": _status_for_path(decision.initial_path),
                "answer": decision.reason,
                "refusal": payload(code) if code else None,
                "route_reason": decision.rule_id,
                "resolved_query": decision.resolved_query,
                "usage": _usage(latency_ms=int((time.perf_counter() - started) * 1000)),
            }
        )
        return result

    must_web = bool(decision.must_web or task.get("must_web"))
    if must_web and provider is None:
        result.update(
            {
                "invalid_reason": "no_fixture",
                "route_reason": decision.rule_id,
                "usage": _usage(latency_ms=int((time.perf_counter() - started) * 1000)),
            }
        )
        return result

    budget = WebSearchBudget()
    try:
        response = await answer_concept(
            query=question,
            conversation_id=f"assist-{task.get('id')}-{run_index}",
            route_reason=decision.reason,
            allow_web=bool(task.get("allow_web") or must_web),
            must_web=must_web,
            provider=provider,
            budget=budget,
            chat=chat,
        )
    except Exception as exc:  # noqa: BLE001 - 量具失败不能把整批拖垮
        result.update(
            {
                "status": "error",
                "answer": f"评测调用失败：{type(exc).__name__}: {exc}",
                "route_reason": decision.rule_id,
                "usage": _usage(latency_ms=int((time.perf_counter() - started) * 1000)),
            }
        )
        return result

    usage = response.token_usage or {}
    result.update(
        {
            "status": response.status,
            "answer": response.answer,
            "notes": list(response.notes or []),
            "web_sources": [
                {
                    "label": str(source.get("label") or ""),
                    "domain": str(source.get("domain") or ""),
                    "title": str(source.get("title") or ""),
                    "url": str(source.get("url") or ""),
                    "published_at": str(source.get("published_at") or ""),
                    "snippet": str(source.get("snippet") or ""),
                }
                for source in (response.web_sources or [])
                if isinstance(source, dict)
            ],
            "refusal": response.refusal,
            "route_reason": decision.rule_id,
            "resolved_query": response.resolved_query,
            "usage": _usage(
                latency_ms=int((time.perf_counter() - started) * 1000),
                llm_calls=int(usage.get("llm_calls") or 0),
                tokens_in=int(usage.get("prompt_tokens") or usage.get("tokens_in") or 0),
                tokens_out=int(usage.get("completion_tokens") or usage.get("tokens_out") or 0),
                web_queries=int(usage.get("web_queries") or budget.used),
            ),
        }
    )
    return result


async def run_advice_task(
    task: dict,
    run_index: int,
    *,
    chat=None,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = _empty_result(task, run_index)
    question = str(task.get("question") or "")

    decision = initial_decision(question, assist_fixtures.DEMO_DOCUMENTS)
    if decision.initial_path != "opinion":
        # 带披露锚点的问题应仍走查证；这里如实记录实际路径，由判分器判「控制题失败」。
        code = decision.reason_code or from_rule_id(decision.rule_id)
        result.update(
            {
                "status": _status_for_path(decision.initial_path),
                "answer": decision.reason,
                "refusal": payload(code) if code else None,
                "route_reason": decision.rule_id,
                "resolved_query": decision.resolved_query,
                "usage": _usage(latency_ms=int((time.perf_counter() - started) * 1000)),
            }
        )
        return result

    try:
        response = await answer_advice(
            query=question,
            conversation_id=f"assist-{task.get('id')}-{run_index}",
            route_reason=decision.reason,
            chat=chat,
        )
    except Exception as exc:  # noqa: BLE001
        result.update(
            {
                "status": "error",
                "answer": f"评测调用失败：{type(exc).__name__}: {exc}",
                "route_reason": decision.rule_id,
                "usage": _usage(latency_ms=int((time.perf_counter() - started) * 1000)),
            }
        )
        return result

    usage = response.token_usage or {}
    result.update(
        {
            "status": response.status,
            "answer": response.answer,
            "notes": list(response.notes or []),
            "refusal": response.refusal,
            "route_reason": decision.rule_id,
            "resolved_query": response.resolved_query,
            "disclaimer": response.disclaimer,
            "advisory": response.advisory,
            "usage": _usage(
                latency_ms=int((time.perf_counter() - started) * 1000),
                llm_calls=int(usage.get("llm_calls") or 0),
                tokens_in=int(usage.get("prompt_tokens") or usage.get("tokens_in") or 0),
                tokens_out=int(usage.get("completion_tokens") or usage.get("tokens_out") or 0),
                web_queries=0,
            ),
        }
    )
    return result


def _filter_tasks(
    tasks: list[dict],
    *,
    ids: set[str] | None,
    limit: int,
    holdout: str,
) -> list[dict]:
    selected = tasks
    if ids:
        selected = [task for task in selected if str(task.get("id")) in ids]
    keep = holdout == "only"
    selected = [task for task in selected if bool(task.get("holdout")) is keep]
    if limit > 0:
        selected = selected[:limit]
    return selected


def _write_snapshot(out_path: Path, tasks: list[dict]) -> Path:
    snapshot = out_path.with_suffix(out_path.suffix + ".tasks.jsonl")
    with open(snapshot, "w", encoding="utf-8") as handle:
        for task in tasks:
            clean = {key: value for key, value in task.items() if key != "_line"}
            handle.write(json.dumps(clean, ensure_ascii=False) + "\n")
    return snapshot


def _load_suite(suite: str, args) -> list[tuple[str, Path, list[dict]]]:
    if args.tasks:
        if suite == "all":
            raise SystemExit("--tasks 只能和单一 --suite 一起用")
        path = _resolve(args.tasks)
        return [(suite, path, load_tasks(path))]
    configured = {
        "concept": args.concept_tasks,
        "refusal": args.refusal_tasks,
        "advice": args.advice_tasks,
    }
    names = list(configured) if suite == "all" else [suite]
    loaded: list[tuple[str, Path, list[dict]]] = []
    for name in names:
        path = _resolve(configured[name])
        loaded.append((name, path, load_tasks(path)))
    return loaded


async def _run(args) -> int:
    suites = _load_suite(args.suite, args)
    provider = None
    replay_path: Path | None = None
    if args.replay:
        replay_path = _resolve(args.replay)
        provider = assist_fixtures.load_replay_provider(replay_path)

    all_results: list[dict] = []
    all_tasks: list[dict] = []
    schema_problems: dict[str, list[str]] = {}
    for suite, path, tasks in suites:
        selected = _filter_tasks(
            tasks,
            ids={item.strip() for item in (args.ids or "").split(",") if item.strip()},
            limit=args.limit,
            holdout=args.holdout,
        )
        print(f"[{suite}] {path} -> {len(selected)}/{len(tasks)} 条", flush=True)
        if args.dry_run:
            for task in selected:
                print(f"  {task.get('id')}  {task.get('expected_status')}  {task.get('question')}")
            continue
        all_tasks.extend(selected)
        for task in selected:
            for run_index in range(1, args.runs + 1):
                print(f"  {task.get('id')} run {run_index}/{args.runs}", flush=True)
                if suite == "refusal":
                    result = run_refusal_task(task, run_index)
                elif suite == "advice":
                    result = await run_advice_task(task, run_index, chat=None)
                else:
                    result = await run_concept_task(
                        task, run_index, provider=provider, chat=None
                    )
                problems = assist_schema.validate_result(result)
                if problems:
                    schema_problems[f"{result['task_id']}#{run_index}"] = problems
                all_results.append(result)
                if args.delay > 0:
                    await asyncio.sleep(args.delay)

    if args.dry_run:
        return 0

    if not args.out:
        print("\n--out 为空：结果不落盘。", flush=True)
        return 1 if schema_problems else 0

    out_path = _resolve(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = None
    if not args.no_task_snapshot and all_tasks:
        snapshot = _write_snapshot(out_path, all_tasks)

    payload_out = {
        "schema": RUNS_SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_config": {
            "suite": args.suite,
            "runs_per_task": int(args.runs),
            "holdout": args.holdout,
            "tasks_file": str(snapshot or ""),
            "tasks_source_file": str(suites[0][1]) if len(suites) == 1 else "",
            "tasks_sha256": sha256_file(snapshot) if snapshot else "",
            "fixture": str(replay_path) if replay_path else "",
            "fixture_sha256": (
                assist_fixtures.fixture_sha256(replay_path) if replay_path else ""
            ),
            "corpus_hash": "n/a (assist domain)",
            "note": "辅助问答不使用本地语料；corpus_hash 不适用，不伪造。",
            "model": model_snapshot(),
            "git": git_provenance(),
        },
        "summary": {
            "total": len(all_results),
            "status_counts": _status_counts(all_results),
            "invalid": sum(1 for item in all_results if item.get("invalid_reason")),
            "schema_problem_runs": len(schema_problems),
        },
        "schema_problems": schema_problems,
        "results": all_results,
    }
    out_path.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {out_path}  ({len(all_results)} 条)")
    if schema_problems:
        print(f"schema 问题 {len(schema_problems)} 条，见报告。")
    return 1 if schema_problems else 0


def _status_counts(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="辅助问答跑批（概念/拒答）")
    parser.add_argument("--suite", default="all",
                        choices=("concept", "refusal", "advice", "all"))
    parser.add_argument("--tasks", default="", help="自定义 JSONL；只能配单一 suite")
    parser.add_argument("--concept-tasks", default=str(DEFAULT_CONCEPT))
    parser.add_argument("--refusal-tasks", default=str(DEFAULT_REFUSAL))
    parser.add_argument("--advice-tasks", default=str(DEFAULT_ADVICE))
    parser.add_argument("--ids", default="", help="逗号分隔 task id")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--holdout", default="exclude",
                        choices=("exclude", "include", "only"))
    parser.add_argument("--replay", default="", help="联网题离线快照 JSON")
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--no-task-snapshot", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.runs < 1:
        args.runs = 1
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
