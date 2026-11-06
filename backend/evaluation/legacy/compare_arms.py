"""两臂逐题配对比较：Agent 多花的钱，买到了多少查证质量。

回答的是一个具体问题，不是"平均分谁高"：

    在**跑实验前固定**的复杂题集上，
    Agent 的通过率比普通 RAG 高多少个百分点？
    每道题多花多少 token？
    多出来的 token 换来了几个"从错到对"？

口径（2026-10-28 与用户共同拍板）：

- **复杂题集在跑之前固定**（`evaluation/finance_tasks_complex.jsonl`，12 题，四桶各 3）；
- **逐题配对**：同一题、同一模型、同一语料，两边各跑一次，按 task_id 对齐；
- **成本看增量**：`tokens_agent − tokens_rag`，不是 Agent 的总量；
- **硬门禁单列**：编造（forbidden_hit / grounded=false）、引用硬错误、运行崩溃，
  任何一条出现，该题直接记"不通过"，且单独计数，不被平均分掩盖；
- 判据：复杂题通过率提升 ≥15 个百分点，且每题增量 ≤20000 token，才算划算。

用法：

    python -m evaluation.legacy.compare_arms \
        --agent-report %TEMP%\\agent_report.json --agent-runs %TEMP%\\agent_runs.json \
        --rag-report %TEMP%\\rag_report.json --rag-runs %TEMP%\\rag_runs.json \
        --tasks evaluation/finance_tasks_complex.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation._runner_common import load_tasks
from evaluation.citation_anchor import anchor_report
from evaluation.finance_corpus import DOCUMENT_DIR
from app.finance.store import FinanceStore

PASS = "pass"
DELTA_PASS_THRESHOLD = 0.15
DELTA_TOKEN_CEILING = 20000


def _wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 1.0)
    p = hits / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _tokens(result: dict) -> int:
    usage = result.get("usage") or {}
    return int(usage.get("tokens_in") or 0) + int(usage.get("tokens_out") or 0)


def _model_calls(result: dict) -> int:
    usage = result.get("usage") or {}
    explicit = usage.get("model_calls")
    if isinstance(explicit, int):
        return explicit
    rounds = usage.get("rounds")
    self_checks = int(usage.get("self_checks") or 0)
    if isinstance(rounds, int):
        return rounds + 1 + self_checks
    return int(usage.get("steps") or 0) + 1


def _citations(result: dict) -> int:
    return len(result.get("citations") or [])


def _is_derived_point(task: dict, index: int) -> bool:
    points = task.get("answer_points") or []
    if 0 <= index < len(points):
        point = points[index] or {}
        return bool(point.get("derived")) or point.get("kind") == "note"
    return False


def _hard_failures(verdict: dict, result: dict, task: dict) -> list[str]:
    """硬门禁：按 编造 / 引用错误 / 崩溃 / 其他违规 四类归因。

    任一命中，该题直接不通过。归因分开是为了报告能回答"到底栽在哪一类"，
    不是把四种毛病都压成一个"fail"。

    注意：`citation_supports=false` 只对**原始事实**要点算引用错误。
    派生要点（比率/差额）引用的是输入格，算错了是数值错误，不是引用错误——
    2026-10-28 审计 CX-01 时踩过：9.08 vs 9.09 被记成"引用不支撑"，
    实际是拿两个未四舍五入的毛利率相减，引用本身没错。
    """
    failures: list[str] = []
    judgment = verdict.get("judgment") or {}
    checks = verdict.get("checks") or {}
    if result.get("status") == "error":
        failures.append("崩溃：运行失败")
    if (checks.get("no_crash") or {}).get("ok") is False:
        failures.append("崩溃：运行失败")
    if judgment.get("forbidden_hit"):
        failures.append("编造：给出禁用内容")
    if judgment.get("grounded") is False:
        failures.append("其他违规：无据断言")
    if (checks.get("citation_markers_resolve") or {}).get("ok") is False:
        failures.append("引用错误：标记无法解析")
    if (checks.get("citation_scope") or {}).get("ok") is False:
        failures.append("引用错误：引到非证据")
    for point in judgment.get("points") or []:
        index = int(point.get("index") or 0)
        if (
            point.get("cited")
            and point.get("citation_supports") is False
            and not _is_derived_point(task, index)
        ):
            failures.append("引用错误：引用不支持结论")
            break
    return failures


def _failure_categories(pairs: list[dict], key: str) -> dict[str, int]:
    """按四类统计命中题数（一题可命中多类）。"""
    counter: dict[str, int] = {}
    for pair in pairs:
        for reason in pair[key]["hard_fail"]:
            category = reason.split("：")[0]
            counter[category] = counter.get(category, 0) + 1
    return counter


def _citation_precision(verdict: dict) -> float | None:
    """被引用的要点里，引用真的支撑结论的比例。"""
    cited = [p for p in (verdict.get("judgment") or {}).get("points") or [] if p.get("cited")]
    if not cited:
        return None
    return sum(1 for p in cited if p.get("citation_supports")) / len(cited)


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _index(results: list[dict]) -> dict[tuple[str, int], dict]:
    return {
        (str(item.get("task_id")), int(item.get("run_index") or 1)): item
        for item in results
    }


def _arm_stats(pairs: list[dict], key: str) -> dict:
    tasks = [pair[key] for pair in pairs]
    total = len(tasks)
    passed = sum(1 for item in tasks if item["pass"])
    low, high = _wilson(passed, total)
    tokens = [item["tokens"] for item in tasks]
    stats = {
        "total": total,
        "passed": passed,
        "pass_rate": passed / total if total else 0.0,
        "wilson": [round(low, 4), round(high, 4)],
        "tokens_mean": round(statistics.mean(tokens), 1) if tokens else 0.0,
        "tokens_median": statistics.median(tokens) if tokens else 0,
        "tokens_total": sum(tokens),
        "calls_mean": round(statistics.mean([item["calls"] for item in tasks]), 2) if tasks else 0.0,
        "latency_mean": round(statistics.mean([item["latency"] for item in tasks]), 1) if tasks else 0.0,
        "citations_mean": round(statistics.mean([item["citations"] for item in tasks]), 2) if tasks else 0.0,
        "citation_precision": (
            round(statistics.mean([i["citation_precision"] for i in tasks if i["citation_precision"] is not None]), 4)
            if any(i["citation_precision"] is not None for i in tasks)
            else None
        ),
        "hard_fail_tasks": sum(1 for item in tasks if item["hard_fail"]),
        "hard_fail_reasons": sorted({reason for item in tasks for reason in item["hard_fail"]}),
        "point_pass": sum(item["point_pass"] for item in tasks),
        "point_total": sum(item["point_total"] for item in tasks),
        "anchor": {
            "points": sum(item["anchor"]["points"] for item in tasks),
            "page_hits": sum(item["anchor"]["page_hits"] for item in tasks),
            "table_hits": sum(item["anchor"]["table_hits"] for item in tasks),
            "cell_hits": sum(item["anchor"]["cell_hits"] for item in tasks),
        },
    }
    stats["point_accuracy"] = (
        round(stats["point_pass"] / stats["point_total"], 4) if stats["point_total"] else None
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent vs RAG 逐题配对比较")
    parser.add_argument("--agent-report", required=True)
    parser.add_argument("--agent-runs", required=True)
    parser.add_argument("--rag-report", required=True)
    parser.add_argument("--rag-runs", required=True)
    parser.add_argument("--tasks", default="evaluation/finance_tasks_complex.jsonl")
    parser.add_argument("--out", default="", help="对比结果 JSON 输出路径")
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    tasks = {str(task["id"]): task for task in load_tasks(tasks_path)}
    store = FinanceStore(DOCUMENT_DIR)

    agent_report = _load(args.agent_report)
    rag_report = _load(args.rag_report)
    agent_runs_payload = _load(args.agent_runs)
    rag_runs_payload = _load(args.rag_runs)
    agent_runs = _index(agent_runs_payload.get("results") or [])
    rag_runs = _index(rag_runs_payload.get("results") or [])

    def collect(report: dict, runs: dict, arm: str) -> dict[tuple[str, int], dict]:
        items: dict[tuple[str, int], dict] = {}
        for verdict in report.get("verdicts") or []:
            key = (str(verdict.get("task_id")), int(verdict.get("run_index") or 1))
            task = tasks.get(key[0])
            result = runs.get(key)
            if not task or not result:
                continue
            hard = _hard_failures(verdict, result, task)
            points = (verdict.get("judgment") or {}).get("points") or []
            anchor = anchor_report(task, result, store)
            items[key] = {
                "pass": verdict.get("outcome") == PASS and not hard,
                "outcome": verdict.get("outcome"),
                "hard_fail": hard,
                "tokens": _tokens(result),
                "calls": _model_calls(result),
                "latency": int((result.get("usage") or {}).get("latency_ms") or 0),
                "citations": _citations(result),
                "citation_precision": _citation_precision(verdict),
                # 要点级：任务级"全对才算过"在 6 要点题上分辨率很低，
                # 要点正确率只作辅助读数，不改预注册的通过率判据。
                "point_pass": sum(1 for point in points if point.get("pass")),
                "point_total": len(points),
                "anchor": anchor,
            }
        return items

    agent_items = collect(agent_report, agent_runs, "agent")
    rag_items = collect(rag_report, rag_runs, "rag")

    keys = sorted(set(agent_items) & set(rag_items))
    if not keys:
        print("两臂没有能对齐的 (task_id, run_index)", file=sys.stderr)
        return 2

    pairs = []
    for key in keys:
        task = tasks.get(key[0]) or {}
        pairs.append(
            {
                "task_id": key[0],
                "run_index": key[1],
                "complex_type": task.get("complex_type") or "",
                "question": task.get("question") or "",
                "agent": agent_items[key],
                "rag": rag_items[key],
            }
        )

    agent_stats = _arm_stats(pairs, "agent")
    rag_stats = _arm_stats(pairs, "rag")
    delta_pass = agent_stats["pass_rate"] - rag_stats["pass_rate"]
    deltas = [pair["agent"]["tokens"] - pair["rag"]["tokens"] for pair in pairs]
    delta_tokens = statistics.mean(deltas)
    gained = sum(1 for p in pairs if p["agent"]["pass"] and not p["rag"]["pass"])
    lost = sum(1 for p in pairs if p["rag"]["pass"] and not p["agent"]["pass"])
    extra_correct = agent_stats["passed"] - rag_stats["passed"]

    print(f"配对题数: {len(pairs)}（task_id + run_index 对齐）")
    print()
    header = f"{'':<14}{'通过':<8}{'通过率':<10}{'Wilson 95%':<20}{'token/题':<12}{'调用/题':<9}{'延迟ms':<10}{'引用精度':<10}{'硬门禁':<8}"
    print(header)
    print("-" * len(header))
    for name, stats in (("Agent", agent_stats), ("RAG 基线", rag_stats)):
        wilson = f"[{stats['wilson'][0]:.1%}, {stats['wilson'][1]:.1%}]"
        precision = f"{stats['citation_precision']:.1%}" if stats["citation_precision"] is not None else "n/a"
        print(
            f"{name:<14}{stats['passed']}/{stats['total']:<6}{stats['pass_rate']:<10.1%}{wilson:<20}"
            f"{stats['tokens_mean']:<12,.0f}{stats['calls_mean']:<9}{stats['latency_mean']:<10,.0f}{precision:<10}{stats['hard_fail_tasks']:<8}"
        )
    print()
    print(f"通过率差（Agent − RAG）: {delta_pass:+.1%}    判据 ≥ {DELTA_PASS_THRESHOLD:.0%} → {'达标' if delta_pass >= DELTA_PASS_THRESHOLD else '未达标'}")
    print(f"token 增量/题（Agent − RAG）: {delta_tokens:+,.0f}    判据 ≤ {DELTA_TOKEN_CEILING:,} → {'达标' if delta_tokens <= DELTA_TOKEN_CEILING else '超支'}")
    print(f"每题最大增量: {max(deltas):+,.0f}    最小: {min(deltas):+,.0f}")
    print(f"翻转：RAG 错→Agent 对 {gained} 题；RAG 对→Agent 错 {lost} 题")
    if extra_correct > 0:
        total_delta = sum(deltas)
        print(f"每多答对 1 题多花的 token: {total_delta / extra_correct:,.0f}")
    else:
        print("Agent 没有多答对任何一题：增量 token 买到的正确性为 0")
    print()
    for name, stats in (("Agent", agent_stats), ("RAG", rag_stats)):
        total = stats["tokens_total"]
        per_10k = stats["passed"] / total * 10000 if total else 0
        accuracy = f"{stats['point_accuracy']:.1%}" if stats["point_accuracy"] is not None else "n/a"
        print(
            f"{name}：每 1 万 token 答对 {per_10k:.2f} 题（总 token {total:,}）；"
            f"要点级正确率 {stats['point_pass']}/{stats['point_total']} = {accuracy}"
        )
    print()

    print("引用定位（按要点，精确定位不与语义支持合并）：")
    print(f"{'':<14}{'页码命中':<14}{'表格命中':<14}{'单元格命中':<14}")
    for name, stats in (("Agent", agent_stats), ("RAG", rag_stats)):
        anchor = stats["anchor"]
        total = anchor["points"] or 1
        print(
            f"{name:<14}"
            f"{anchor['page_hits']}/{anchor['points']} ({anchor['page_hits']/total:.0%})   "
            f"{anchor['table_hits']}/{anchor['points']} ({anchor['table_hits']/total:.0%})   "
            f"{anchor['cell_hits']}/{anchor['points']} ({anchor['cell_hits']/total:.0%})"
        )
    print()

    buckets: dict[str, list[dict]] = {}
    for pair in pairs:
        buckets.setdefault(pair["complex_type"] or "未分桶", []).append(pair)
    print(f"{'桶':<16}{'题数':<6}{'Agent 通过':<12}{'RAG 通过':<11}{'通过率差':<10}{'token 增量/题':<14}")
    print("-" * 69)
    for bucket, items in sorted(buckets.items()):
        a = sum(1 for i in items if i["agent"]["pass"])
        r = sum(1 for i in items if i["rag"]["pass"])
        d = statistics.mean([i["agent"]["tokens"] - i["rag"]["tokens"] for i in items])
        print(f"{bucket:<16}{len(items):<6}{a}/{len(items):<10}{r}/{len(items):<9}{(a - r) / len(items):<10.1%}{d:<14,.0f}")
    print()

    for name, key in (("Agent", "agent"), ("RAG", "rag")):
        stats = agent_stats if key == "agent" else rag_stats
        if not stats["hard_fail_tasks"]:
            continue
        categories = _failure_categories(pairs, key)
        detail = "；".join(f"{category} {count} 题" for category, count in sorted(categories.items()))
        print(f"! {name} 有 {stats['hard_fail_tasks']} 题命中硬门禁：{detail}")

    print()
    print(f"{'task':<8}{'桶':<14}{'判':<6}{'token A/R':<20}{'调用 A/R':<10}{'延迟 A/R':<18}{'引用精度 A/R':<14}")
    print("-" * 92)
    for pair in pairs:
        a, r = pair["agent"], pair["rag"]
        flag = ""
        if a["pass"] and not r["pass"]:
            flag = "  ← Agent 赢"
        elif r["pass"] and not a["pass"]:
            flag = "  ← RAG 赢"
        if a["hard_fail"] or r["hard_fail"]:
            flag += f"  [硬门禁 A:{','.join(a['hard_fail']) or '-'} R:{','.join(r['hard_fail']) or '-'}]"
        ap = f"{a['citation_precision']:.0%}" if a["citation_precision"] is not None else "-"
        rp = f"{r['citation_precision']:.0%}" if r["citation_precision"] is not None else "-"
        verdict_text = f"A{'过' if a['pass'] else '败'}/R{'过' if r['pass'] else '败'}"
        print(
            f"{pair['task_id']:<8}{pair['complex_type']:<14}{verdict_text:<7}"
            f"{a['tokens']:>9,}/{r['tokens']:>8,}  "
            f"{a['calls']:>3}/{r['calls']:<4}"
            f"{a['latency']:>7,}/{r['latency']:>7,}  "
            f"{ap:>5}/{rp:<5}{flag}"
        )

    if args.out:
        payload = {
            "tasks_file": str(tasks_path),
            "paired": len(pairs),
            "agent_variant": agent_report.get("variant"),
            "rag_variant": rag_report.get("variant"),
            # 关键：Lab 必须能区分“结构感知 RAG”和“固定窗 RAG”，
            # 否则同一份 compare 文件无法判断它能不能作为产品主比较。
            "rag_retrieval_mode": (rag_runs_payload.get("run_config") or {}).get("retrieval_mode"),
            "agent": agent_stats,
            "rag": rag_stats,
            "delta_pass_rate": round(delta_pass, 4),
            "delta_tokens_mean": round(delta_tokens, 1),
            "flips": {"rag_wrong_agent_right": gained, "rag_right_agent_wrong": lost},
            "thresholds": {
                "delta_pass_rate": DELTA_PASS_THRESHOLD,
                "delta_tokens_ceiling": DELTA_TOKEN_CEILING,
                "pass_ok": delta_pass >= DELTA_PASS_THRESHOLD,
                "cost_ok": delta_tokens <= DELTA_TOKEN_CEILING,
            },
            "pairs": pairs,
        }
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n对比结果：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
