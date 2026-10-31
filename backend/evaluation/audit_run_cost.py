"""零模型成本归因：读 runs JSON，拆 token、上下文、工具结果与重复读取。

不调用模型，只读已有轨迹。旧 runs 没有逐轮 `llm_calls`，仍可算总 token、
步数、工具调用、精确重复读取和同表重复读取；新 runs 额外给出逐轮 token、
消息历史字符和每个工具结果的字符数。

用法（在 backend 目录下）：

    python -m evaluation.audit_run_cost --runs %TEMP%\\agent_blocks.json
    python -m evaluation.audit_run_cost --runs %TEMP%\\agent_blocks.json --out %TEMP%\\cost.json
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

READ_TOOLS = ("get_table", "get_rows", "get_cell", "read_section", "read_block")


def _load_results(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    return list(payload.get("results") or [])


def _tokens(usage: dict) -> int:
    return int(usage.get("tokens_in") or 0) + int(usage.get("tokens_out") or 0)


def _table_id(step: dict) -> str:
    args = step.get("args") or {}
    if args.get("table_id"):
        return str(args["table_id"])
    block_id = str(args.get("block_id") or "")
    parts = block_id.split("#")
    if len(parts) >= 2 and "#t" in "#".join(parts[:2]):
        return "#".join(parts[:2])
    return ""


def _tool_rollup(result: dict) -> dict:
    tools: dict[str, dict] = {}
    exact: dict[tuple[str, str], int] = collections.Counter()
    tables: dict[str, int] = collections.Counter()
    for step in result.get("trace") or []:
        name = str(step.get("tool") or "")
        if name == "__final__":
            continue
        bucket = tools.setdefault(
            name,
            {"calls": 0, "result_chars": 0, "payload_chars": 0,
             "evidence_chars_added": 0, "duplicate_skipped": 0},
        )
        bucket["calls"] += 1
        bucket["result_chars"] += int(step.get("result_chars") or 0)
        bucket["payload_chars"] += int(step.get("payload_chars") or 0)
        bucket["evidence_chars_added"] += int(step.get("evidence_chars_added") or 0)
        bucket["duplicate_skipped"] += int(step.get("duplicate_skipped") or 0)
        if name in READ_TOOLS:
            exact[(name, json.dumps(step.get("args") or {}, sort_keys=True, ensure_ascii=False))] += 1
            table_id = _table_id(step)
            if table_id:
                tables[table_id] += 1
    duplicate_groups = [
        {"tool": tool, "args": args, "calls": count}
        for (tool, args), count in exact.items()
        if count > 1
    ]
    repeated_tables = [
        {"table_id": table_id, "reads": count}
        for table_id, count in tables.items()
        if count > 1
    ]
    return {
        "tools": tools,
        "duplicate_read_groups": duplicate_groups,
        "repeated_tables": sorted(repeated_tables, key=lambda item: item["reads"], reverse=True),
    }


def _llm_rollup(result: dict) -> dict:
    calls = result.get("llm_calls")
    if not calls:
        return {"available": False}
    phases: dict[str, dict] = {}
    for call in calls:
        phase = str(call.get("phase") or "unknown")
        bucket = phases.setdefault(phase, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
        bucket["calls"] += 1
        bucket["tokens_in"] += int(call.get("tokens_in") or 0)
        bucket["tokens_out"] += int(call.get("tokens_out") or 0)
    total_tokens = sum(
        item["tokens_in"] + item["tokens_out"] for item in phases.values()
    )
    for item in phases.values():
        item["share"] = round(
            (item["tokens_in"] + item["tokens_out"]) / total_tokens, 4
        ) if total_tokens else 0.0
    return {"available": True, "phases": phases}


def _summarize(result: dict) -> dict:
    usage = result.get("usage") or {}
    rollup = _tool_rollup(result)
    llm = _llm_rollup(result)
    calls = result.get("llm_calls") or []
    return {
        "task_id": str(result.get("task_id") or ""),
        "variant": result.get("variant"),
        "status": result.get("status"),
        "total_tokens": _tokens(usage),
        "tokens_in": int(usage.get("tokens_in") or 0),
        "tokens_out": int(usage.get("tokens_out") or 0),
        "llm_calls": int(usage.get("llm_calls") or len(calls)),
        "steps": int(usage.get("steps") or 0),
        "rounds": usage.get("rounds"),
        "repair_rounds": usage.get("repair_rounds"),
        "auto_retries": usage.get("auto_retries"),
        "self_checks": usage.get("self_checks"),
        "final_context_tokens": usage.get("final_context_tokens")
        or (int(calls[-1].get("tokens_in") or 0) if calls else None),
        "max_context_tokens": usage.get("max_context_tokens")
        or (max((int(c.get("tokens_in") or 0) for c in calls), default=None) if calls else None),
        "max_messages_chars": usage.get("max_messages_chars")
        or (max((int(c.get("messages_chars") or 0) for c in calls), default=None) if calls else None),
        "tool_result_chars": int(usage.get("tool_result_chars") or 0),
        "tool_payload_chars": int(usage.get("tool_payload_chars") or 0),
        "duplicate_skipped": int(usage.get("duplicate_skipped") or 0),
        "llm": llm,
        **rollup,
    }


def _aggregate(rows: list[dict]) -> dict:
    tokens = [row["total_tokens"] for row in rows]
    tools: dict[str, dict] = {}
    phases: dict[str, dict] = {}
    for row in rows:
        for name, bucket in row["tools"].items():
            target = tools.setdefault(
                name, {"calls": 0, "result_chars": 0, "payload_chars": 0}
            )
            for key in ("calls", "result_chars", "payload_chars"):
                target[key] += bucket[key]
        if row["llm"].get("available"):
            for phase, bucket in row["llm"]["phases"].items():
                target = phases.setdefault(
                    phase, {"calls": 0, "tokens_in": 0, "tokens_out": 0}
                )
                for key in ("calls", "tokens_in", "tokens_out"):
                    target[key] += bucket[key]
    for bucket in phases.values():
        bucket["tokens"] = bucket["tokens_in"] + bucket["tokens_out"]
    total_phase_tokens = sum(bucket["tokens"] for bucket in phases.values())
    for bucket in phases.values():
        bucket["share"] = round(bucket["tokens"] / total_phase_tokens, 4) if total_phase_tokens else 0.0
    return {
        "runs": len(rows),
        "total_tokens": sum(tokens),
        "median_tokens": int(statistics.median(tokens)) if tokens else 0,
        "mean_tokens": int(sum(tokens) / len(tokens)) if tokens else 0,
        "legacy_runs": sum(1 for row in rows if not row["llm"].get("available")),
        "tool_totals": tools,
        "phase_totals": phases,
        "duplicate_read_groups": sum(len(row["duplicate_read_groups"]) for row in rows),
        "runs_with_repeated_tables": sum(1 for row in rows if row["repeated_tables"]),
    }


def _display(value: object) -> str:
    return "-" if value is None or value == "" else str(value)


def _print_report(rows: list[dict], aggregate: dict, top: int) -> None:
    header = (
        f"{'task':<10}{'status':<11}{'tokens':>9}{'llm':>5}{'steps':>6}"
        f"{'rounds':>7}{'repair':>7}{'retry':>6}{'self':>5}"
        f"{'final_ctx':>10}{'msg_chars':>10}{'tool_chars':>11}{'dup':>5}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['task_id']:<10}{_display(row['status']):<11}{row['total_tokens']:>9}"
            f"{row['llm_calls']:>5}{row['steps']:>6}"
            f"{_display(row['rounds']):>7}{_display(row['repair_rounds']):>7}"
            f"{_display(row['auto_retries']):>6}{_display(row['self_checks']):>5}"
            f"{_display(row['final_context_tokens']):>10}"
            f"{_display(row['max_messages_chars']):>10}"
            f"{row['tool_result_chars']:>11}{row['duplicate_skipped']:>5}"
        )
    print()
    print(
        f"合计 {aggregate['runs']} 次：{aggregate['total_tokens']:,} token；"
        f"中位 {aggregate['median_tokens']:,}/次；旧轨迹 {aggregate['legacy_runs']} 次"
    )
    if aggregate["phase_totals"]:
        print("按阶段：")
        for phase, bucket in sorted(
            aggregate["phase_totals"].items(), key=lambda item: item[1]["tokens"], reverse=True
        ):
            print(
                f"  {phase:<24}{bucket['calls']:>4} 次  {bucket['tokens']:>9,} token"
                f"  {bucket['share'] * 100:5.1f}%"
            )
    print("按工具：")
    for name, bucket in sorted(
        aggregate["tool_totals"].items(), key=lambda item: item[1]["result_chars"], reverse=True
    ):
        print(
            f"  {name:<20}{bucket['calls']:>4} 次  结果 {bucket['result_chars']:>8,} 字"
            f"  原始 {bucket['payload_chars']:>8,} 字"
        )
    if aggregate["duplicate_read_groups"] or aggregate["runs_with_repeated_tables"]:
        print(
            f"重复：精确重复组 {aggregate['duplicate_read_groups']}；"
            f"同表多次读取的运行 {aggregate['runs_with_repeated_tables']} 次"
        )
    print(f"\nTop {top} token：")
    for row in sorted(rows, key=lambda item: item["total_tokens"], reverse=True)[:top]:
        share = row["total_tokens"] / aggregate["total_tokens"] * 100 if aggregate["total_tokens"] else 0
        print(f"  {row['task_id']:<10}{row['total_tokens']:>8,}  {share:5.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线成本归因（不调用模型）")
    parser.add_argument("--runs", required=True, help="run_finance_eval / run_rag_eval 的 runs JSON")
    parser.add_argument("--out", default="", help="归因 JSON 输出路径")
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()

    path = Path(args.runs)
    if not path.is_absolute():
        path = _BACKEND_DIR / path
    if not path.exists():
        raise SystemExit(f"runs 文件不存在: {path}")

    rows = [_summarize(result) for result in _load_results(path)]
    if not rows:
        raise SystemExit("runs 文件里没有 results")
    aggregate = _aggregate(rows)
    _print_report(rows, aggregate, max(1, args.top))

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {"kind": "run_cost_audit", "runs_file": str(path),
                 "aggregate": aggregate, "rows": rows},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\n结果：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
