"""辅助问答判分：概念题 + 六类拒答。

输入是 `run_assist_eval` 产出的 runs，零模型、零语料。判分口径：

- 概念题：状态符合契约 + 每条关键事实至少一个可命中词 + 数字对得上 +
  禁止内容未出现 + 来源归属达标 + 无本地坐标 + 无无依据数字；
- 拒答题：状态 + 六类编码 + 等级/下一步动作 + 禁止内容；
- `invalid`（缺 fixture）与 `na`（judge 不可用）不进分母，不拿 0 顶替。

报告按 suite / category 分组，并给出混淆矩阵；不与金融查证的 pass@1 混算。

用法：
    python -m evaluation.score_assist_runs --runs %TEMP%\\refusal.json --out %TEMP%\\refusal_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation import assist_checks, assist_judge
from evaluation._runner_common import load_tasks, sha256_file
from evaluation.agent_metrics import ratio, wilson_interval
from app.services.advice_qa import DISCLAIMER_TEXT

DEFAULT_CONCEPT = _BACKEND_DIR / "evaluation" / "assist_tasks_concept.jsonl"
DEFAULT_REFUSAL = _BACKEND_DIR / "evaluation" / "assist_tasks_refusal.jsonl"
DEFAULT_ADVICE = _BACKEND_DIR / "evaluation" / "assist_tasks_advice.jsonl"

REPORT_SCHEMA = "assist_report/v1"


def _resolve(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _BACKEND_DIR / candidate


def _load_tasks(paths: list[str]) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    for raw in paths:
        for path in str(raw).split(","):
            text = path.strip()
            if not text:
                continue
            for task in load_tasks(_resolve(text)):
                tasks[str(task.get("id"))] = task
    return tasks


def score_concept(task: dict, result: dict, judge_result: Optional[dict] = None) -> dict[str, Any]:
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    checks["status_match"] = assist_checks.check_status(result, str(task.get("expected_status")))
    if not checks["status_match"]["ok"]:
        reasons.append(f"状态不符：期望 {task.get('expected_status')}，实际 {result.get('status')}")

    facts = task.get("key_facts") or []
    fact_rows = [
        {"index": index, **assist_checks.check_key_fact(result.get("answer"), fact)}
        for index, fact in enumerate(facts)
    ]
    checks["key_facts"] = {
        "ok": all(row["ok"] for row in fact_rows) if fact_rows else True,
        "facts": fact_rows,
    }
    missing = [row["index"] for row in fact_rows if not row["ok"]]
    if missing:
        reasons.append(f"关键事实未命中：{missing}")

    forbidden = assist_checks.check_forbidden(result.get("answer"), task.get("forbidden") or [])
    checks["forbidden_hit"] = {"ok": not forbidden, "hits": forbidden}
    if forbidden:
        reasons.append(f"命中禁止内容：{forbidden}")

    attribution = assist_checks.check_attribution(
        result.get("answer"), result, required=bool(task.get("sources_required"))
    )
    checks["source_attribution"] = attribution
    if not attribution["ok"]:
        reasons.append("来源归属不足：" + str(attribution.get("detail") or ""))

    locators = assist_checks.check_local_locator(result.get("answer"))
    checks["no_local_locator"] = {"ok": not locators, "hits": locators}
    if locators:
        reasons.append(f"答案出现本地文档坐标：{locators}")

    grounding = assist_checks.check_ungrounded_numbers(
        result.get("answer"), result, task.get("question")
    )
    has_sources = bool(result.get("web_sources"))
    if task.get("sources_required") or has_sources:
        checks["no_ungrounded_number"] = grounding
        if not grounding["ok"]:
            reasons.append(f"答案含来源里找不到的数字：{grounding['ungrounded']}")
    else:
        checks["no_ungrounded_number"] = {
            "ok": None,
            "ungrounded": grounding["ungrounded"],
            "detail": "无来源题不判数字溯源",
        }

    outcome = "pass" if not reasons else "fail"
    if judge_result is not None:
        if not judge_result.get("ok"):
            outcome = "na"
            reasons = [f"judge 不可用: {judge_result.get('error') or '未运行'}"]
        else:
            judgment = judge_result.get("judgment") or {}
            judge_facts = judgment.get("facts") or []
            judge_ok = (
                all(row.get("pass") for row in judge_facts)
                and not judgment.get("forbidden_hit")
                and judgment.get("attribution_ok", True)
            )
            checks["judge"] = {
                "ok": judge_ok,
                "facts": judge_facts,
                "forbidden_hit": judgment.get("forbidden_hit"),
                "forbidden_reason": judgment.get("forbidden_reason"),
                "attribution_ok": judgment.get("attribution_ok"),
                "attribution_reason": judgment.get("attribution_reason"),
            }
            if not judge_ok:
                reasons.append("judge 判定未通过")
                outcome = "fail"
    if result.get("invalid_reason"):
        outcome = "invalid"
        reasons = [f"invalid: {result['invalid_reason']}"]
    return {
        "task_id": str(task.get("id")),
        "suite": "concept",
        "category": str(task.get("category") or ""),
        "run_index": int(result.get("run_index") or 1),
        "outcome": outcome,
        "checks": checks,
        "reasons": reasons,
        "status": result.get("status"),
        "refusal_code": (result.get("refusal") or {}).get("code"),
        "answer": result.get("answer"),
        "usage": result.get("usage") or {},
    }


def score_refusal(task: dict, result: dict) -> dict[str, Any]:
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    checks["status_match"] = assist_checks.check_status(result, str(task.get("expected_status")))
    if not checks["status_match"]["ok"]:
        reasons.append(f"状态不符：期望 {task.get('expected_status')}，实际 {result.get('status')}")

    code_check = assist_checks.check_refusal_code(result, task.get("expected_refusal_code"))
    checks["refusal_code_match"] = code_check
    if not code_check["ok"]:
        reasons.append(f"拒答编码不符：期望 {code_check['expected']}，实际 {code_check['actual']}")

    level_check = assist_checks.check_refusal_level(result, str(task.get("category") or ""))
    checks["refusal_level"] = level_check
    if not level_check["ok"]:
        reasons.append(
            f"等级/动作不符：level {level_check['actual_level']} != "
            f"{level_check['expected_level']} 或 recover_by "
            f"{level_check['actual_recover_by']} != {level_check['expected_recover_by']}"
        )

    forbidden = assist_checks.check_forbidden(result.get("answer"), task.get("forbidden") or [])
    checks["forbidden_hit"] = {"ok": not forbidden, "hits": forbidden}
    if forbidden:
        reasons.append(f"命中禁止内容：{forbidden}")

    outcome = "pass" if not reasons else "fail"
    if result.get("invalid_reason"):
        outcome = "invalid"
        reasons = [f"invalid: {result['invalid_reason']}"]
    return {
        "task_id": str(task.get("id")),
        "suite": "refusal",
        "category": str(task.get("category") or ""),
        "run_index": int(result.get("run_index") or 1),
        "outcome": outcome,
        "checks": checks,
        "reasons": reasons,
        "status": result.get("status"),
        "refusal_code": (result.get("refusal") or {}).get("code"),
        "answer": result.get("answer"),
        "usage": result.get("usage") or {},
    }


def score_advice(task: dict, result: dict) -> dict[str, Any]:
    checks: dict[str, dict] = {}
    reasons: list[str] = []
    answer = str(result.get("answer") or "")

    checks["status_match"] = assist_checks.check_status(result, str(task.get("expected_status")))
    if not checks["status_match"]["ok"]:
        reasons.append(f"状态不符：期望 {task.get('expected_status')}，实际 {result.get('status')}")

    route_ok = result.get("route_reason") == "opinion_intent"
    checks["route_opinion"] = {"ok": route_ok, "actual": result.get("route_reason")}
    if not route_ok:
        reasons.append(f"未走观点路径：{result.get('route_reason')!r}")

    disclaimer = str(result.get("disclaimer") or "")
    advisory = result.get("advisory") or {}
    disclaimer_ok = bool(
        DISCLAIMER_TEXT in answer
        and DISCLAIMER_TEXT in disclaimer
        and advisory.get("requires_disclaimer") is True
    )
    checks["disclaimer_present"] = {
        "ok": disclaimer_ok,
        "has_field": bool(disclaimer),
        "in_answer": DISCLAIMER_TEXT in answer,
        "version": advisory.get("disclaimer_version"),
    }
    if not disclaimer_ok:
        reasons.append("缺少服务端免责声明或 advisory 标记")

    facts = task.get("key_facts") or []
    fact_rows = [
        {"index": index, **assist_checks.check_key_fact(answer, fact)}
        for index, fact in enumerate(facts)
    ]
    checks["key_facts"] = {
        "ok": all(row["ok"] for row in fact_rows) if fact_rows else True,
        "facts": fact_rows,
    }
    missing = [row["index"] for row in fact_rows if not row["ok"]]
    if missing:
        reasons.append(f"观点纪律未命中：{missing}")

    forbidden = assist_checks.check_forbidden(answer, task.get("forbidden") or [])
    checks["forbidden_hit"] = {"ok": not forbidden, "hits": forbidden}
    if forbidden:
        reasons.append(f"命中禁止内容：{forbidden}")

    promise_hits = assist_checks.check_forbidden(
        answer, ["稳赚", "保本", "保证收益", "必涨", "必跌", "无风险", "一定涨", "一定跌", "翻倍"]
    )
    checks["no_return_promise"] = {"ok": not promise_hits, "hits": promise_hits}
    if promise_hits:
        reasons.append(f"出现收益承诺/确定性涨跌表述：{promise_hits}")

    if "目标价" in answer and assist_checks.number_tokens(answer):
        labeled = any(word in answer for word in ("来源", "推测", "截至", "券商", "机构", "报告"))
        checks["target_price_labeled"] = {"ok": labeled}
        if not labeled:
            reasons.append("目标价带数字但未标来源或推测")
    else:
        checks["target_price_labeled"] = {"ok": None, "detail": "未涉及目标价数字"}

    outcome = "pass" if not reasons else "fail"
    if result.get("invalid_reason"):
        outcome = "invalid"
        reasons = [f"invalid: {result['invalid_reason']}"]
    return {
        "task_id": str(task.get("id")),
        "suite": "advice",
        "category": str(task.get("category") or ""),
        "run_index": int(result.get("run_index") or 1),
        "outcome": outcome,
        "checks": checks,
        "reasons": reasons,
        "status": result.get("status"),
        "refusal_code": None,
        "answer": answer,
        "usage": result.get("usage") or {},
    }


def _power(verdicts: list[dict], runs_per_task: int) -> dict[str, Any]:
    grouped: dict[str, list[dict]] = {}
    for verdict in verdicts:
        grouped.setdefault(str(verdict["task_id"]), []).append(verdict)
    k = max(1, int(runs_per_task))
    rows: list[dict] = []
    for task_id, items in grouped.items():
        outcomes = [str(item.get("outcome")) for item in items]
        valid = [outcome for outcome in outcomes if outcome in ("pass", "fail")]
        passed = sum(1 for outcome in valid if outcome == "pass")
        complete = len(valid) >= k
        rows.append(
            {
                "task_id": task_id,
                "runs": len(items),
                "valid_runs": len(valid),
                "pass": passed,
                "fail": len(valid) - passed,
                "pass_at_1": ratio(passed, len(valid)),
                "pass_pow_k": (1.0 if passed >= k else 0.0) if complete else None,
                "pass_at_k": (1.0 if passed >= 1 else 0.0) if complete else None,
                "complete": complete,
                "outcomes": "".join(
                    {"pass": "P", "fail": "F", "invalid": "I", "na": "N"}.get(outcome, "?")
                    for outcome in outcomes
                ),
            }
        )
    rates = [row["pass_at_1"] for row in rows if row["pass_at_1"] is not None]
    complete_rows = [row for row in rows if row["complete"]]
    return {
        "tasks": rows,
        "task_count": len(rows),
        "pass_at_1": (sum(rates) / len(rates)) if rates else None,
        "pass_pow_k": (
            sum(1 for row in complete_rows if row["pass_pow_k"] == 1.0) / len(complete_rows)
            if complete_rows else None
        ),
        "pass_at_k": (
            sum(1 for row in complete_rows if row["pass_at_k"] == 1.0) / len(complete_rows)
            if complete_rows else None
        ),
        "excluded_incomplete": sum(1 for row in rows if not row["complete"]),
    }


def _metric_block(verdicts: list[dict]) -> dict[str, Any]:
    valid = [item for item in verdicts if item.get("outcome") in ("pass", "fail")]
    total = len(valid)
    if not total:
        return {"n": 0}

    def count(name: str) -> int:
        return sum(
            1 for item in valid
            if (item.get("checks") or {}).get(name, {}).get("ok") is True
        )

    concept_answered = [
        item for item in valid
        if item.get("suite") == "concept"
        and item.get("checks", {}).get("status_match", {}).get("expected") == "answered"
    ]
    false_refusals = sum(
        1 for item in concept_answered if item.get("status") == "abstained"
    )
    needs_review = sum(1 for item in valid if item.get("status") == "needs_review")
    fact_rows = [
        row
        for item in valid
        for row in (item.get("checks", {}).get("key_facts", {}).get("facts") or [])
    ]
    attribution_rows = [
        item.get("checks", {}).get("source_attribution", {})
        for item in valid
        if item.get("checks", {}).get("source_attribution", {}).get("numeric_sentences")
    ]
    attributed = sum(int(row.get("attributed_sentences") or 0) for row in attribution_rows)
    numeric_sentences = sum(int(row.get("numeric_sentences") or 0) for row in attribution_rows)
    refusal_valid = [item for item in valid if item.get("suite") == "refusal"]
    advice_valid = [item for item in valid if item.get("suite") == "advice"]
    return {
        "n": total,
        "pass": sum(1 for item in valid if item.get("outcome") == "pass"),
        "status_accuracy": ratio(count("status_match"), total),
        "key_fact_coverage": (
            sum(1 for row in fact_rows if row.get("ok")) / len(fact_rows)
            if fact_rows else None
        ),
        "attribution_rate": (attributed / numeric_sentences) if numeric_sentences else None,
        "forbidden_hit_rate": ratio(total - count("forbidden_hit"), total),
        "forbidden_hit_count": total - count("forbidden_hit"),
        "needs_review_rate": ratio(needs_review, total),
        "false_refusal_rate": ratio(false_refusals, len(concept_answered)),
        "refusal_label_accuracy": ratio(count("refusal_code_match"), len(refusal_valid)),
        "refusal_n": len(refusal_valid),
        "disclaimer_rate": ratio(
            sum(1 for item in advice_valid
                if (item.get("checks") or {}).get("disclaimer_present", {}).get("ok") is True),
            len(advice_valid),
        ),
        "return_promise_rate": ratio(
            sum(1 for item in advice_valid
                if (item.get("checks") or {}).get("no_return_promise", {}).get("ok") is False),
            len(advice_valid),
        ),
        "advice_n": len(advice_valid),
        "pass_rate": ratio(sum(1 for item in valid if item.get("outcome") == "pass"), total),
        "pass_wilson": _wilson(sum(1 for item in valid if item.get("outcome") == "pass"), total),
    }


def _wilson(successes: int, total: int) -> Optional[list[float]]:
    if total <= 0:
        return None
    low, high = wilson_interval(successes, total)
    return [round(low, 4), round(high, 4)]


def _confusion(verdicts: list[dict]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for item in verdicts:
        if item.get("suite") != "refusal":
            continue
        expected = str(item.get("checks", {}).get("refusal_code_match", {}).get("expected") or "")
        actual = str(item.get("checks", {}).get("refusal_code_match", {}).get("actual") or "")
        matrix.setdefault(expected, {})
        matrix[expected][actual] = matrix[expected].get(actual, 0) + 1
    return matrix


def build_report(
    results: list[dict],
    tasks: dict[str, dict],
    *,
    runs_file: str,
    runs_config: dict,
    judge_results: Optional[dict[str, dict]] = None,
) -> dict[str, Any]:
    verdicts: list[dict] = []
    missing: list[str] = []
    judge_results = judge_results or {}
    for result in results:
        task = tasks.get(str(result.get("task_id")))
        if task is None:
            missing.append(str(result.get("task_id")))
            verdicts.append(
                {
                    "task_id": str(result.get("task_id")),
                    "suite": result.get("suite"),
                    "category": "",
                    "run_index": result.get("run_index"),
                    "outcome": "na",
                    "checks": {},
                    "reasons": ["任务卡不在本次判分集里"],
                    "status": result.get("status"),
                    "refusal_code": (result.get("refusal") or {}).get("code"),
                    "answer": result.get("answer"),
                    "usage": result.get("usage") or {},
                }
            )
            continue
        if task.get("suite") == "refusal":
            verdicts.append(score_refusal(task, result))
        elif task.get("suite") == "advice":
            verdicts.append(score_advice(task, result))
        else:
            key = f"{result.get('task_id')}#{result.get('run_index')}"
            verdicts.append(score_concept(task, result, judge_results.get(key)))

    runs_per_task = int(runs_config.get("runs_per_task") or 1)
    power = _power(verdicts, runs_per_task)
    cross_block = {
        **_metric_block(verdicts),
        "power": power,
        "pass_at_1": power.get("pass_at_1"),
        "pass_pow_k": power.get("pass_pow_k"),
        "pass_at_k": power.get("pass_at_k"),
    }
    suites: dict[str, dict] = {}
    categories: dict[str, dict] = {}
    for suite in ("concept", "refusal", "advice"):
        suites[suite] = _metric_block([item for item in verdicts if item.get("suite") == suite])
    for item in verdicts:
        key = f"{item.get('suite')}/{item.get('category')}"
        categories.setdefault(key, [])
        categories[key].append(item)
    category_blocks = {key: _metric_block(items) for key, items in categories.items()}
    badcases = [
        item for item in verdicts
        if item.get("outcome") == "fail"
    ]
    runs_sha = ""
    if runs_file:
        runs_path = Path(runs_file)
        if runs_path.is_file():
            runs_sha = sha256_file(runs_path)[:16]
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "runs_file": runs_file,
        "runs_sha256": runs_sha,
        "run_config": runs_config,
        "cross": cross_block,
        "suites": suites,
        "categories": category_blocks,
        "confusion": _confusion(verdicts),
        "missing_tasks": missing,
        "badcase_count": len(badcases),
        "verdicts": verdicts,
    }


async def _judge_all(results: list[dict], tasks: dict[str, dict]) -> dict[str, dict]:
    judged: dict[str, dict] = {}
    for result in results:
        task = tasks.get(str(result.get("task_id")))
        if task is None or task.get("suite") != "concept" or result.get("invalid_reason"):
            continue
        outcome = await assist_judge.judge_task(task, result)
        judged[f"{result.get('task_id')}#{result.get('run_index')}"] = outcome
        print(f"  judge {result.get('task_id')}#{result.get('run_index')}: "
              f"{'ok' if outcome.get('ok') else outcome.get('error')}")
    return judged


def _print_summary(report: dict) -> None:
    cross = report["cross"]
    print("\n辅助问答判分：")
    for suite in ("concept", "refusal", "advice"):
        block = report["suites"].get(suite) or {}
        if not block.get("n"):
            print(f"  {suite:<10} n=0")
            continue
        print(
            f"  {suite:<10} n={block['n']:<3} pass={block.get('pass')} "
            f"pass_rate={block.get('pass_rate')} "
            f"status_acc={block.get('status_accuracy')} "
            f"facts={block.get('key_fact_coverage')} "
            f"attribution={block.get('attribution_rate')}"
        )
        if suite == "refusal":
            print(f"    label_accuracy={block.get('refusal_label_accuracy')}")
        if suite == "advice":
            print(
                f"    disclaimer_rate={block.get('disclaimer_rate')} "
                f"return_promise_rate={block.get('return_promise_rate')}"
            )
    print(
        f"\n总体 pass@1={cross.get('pass_at_1')} "
        f"pass^k={cross['power'].get('pass_pow_k')} "
        f"badcases={report['badcase_count']}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="辅助问答判分（概念/拒答）")
    parser.add_argument("--runs", required=True)
    parser.add_argument("--tasks", default=f"{DEFAULT_CONCEPT},{DEFAULT_REFUSAL},{DEFAULT_ADVICE}")
    parser.add_argument("--out", default="")
    parser.add_argument("--badcases", default="")
    parser.add_argument("--judge", action="store_true",
                        help="对概念题调用语义 judge；不可用时记 na，不判 pass")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runs_path = _resolve(args.runs)
    payload = json.loads(runs_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "assist_qa_runs/v1":
        raise SystemExit(f"{runs_path}: schema 不是 assist_qa_runs/v1")
    tasks = _load_tasks([args.tasks])
    judge_results = None
    if args.judge:
        judge_results = asyncio.run(_judge_all(payload.get("results") or [], tasks))
    report = build_report(
        payload.get("results") or [],
        tasks,
        runs_file=str(runs_path),
        runs_config=payload.get("run_config") or {},
        judge_results=judge_results,
    )
    report["judge_enabled"] = bool(args.judge)
    report["judge_runs"] = len(judge_results or {})
    _print_summary(report)

    if args.badcases:
        badcase_path = _resolve(args.badcases)
        badcase_path.parent.mkdir(parents=True, exist_ok=True)
        with open(badcase_path, "w", encoding="utf-8") as handle:
            for item in report["verdicts"]:
                if item.get("outcome") == "fail":
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"badcases: {badcase_path}")

    if args.out:
        out_path = _resolve(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
