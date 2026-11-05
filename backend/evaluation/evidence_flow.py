"""证据流分析：定位 → 读取 → 引用 的转化率（零 token，只读 runs 产物）。

和 `trajectory_eval` 的分工：

- `trajectory_eval` 判**动作是否合规**（按序、关键工具、无效循环、步数预算），
  需要任务卡；
- 本模块判**证据流是否高效**（定位到的表读了多少、读过的表引了多少、
  第一次定位是否就命中），只依赖 `trace` / `evidence` / `citations`，
  不需要任务卡，也不读语料、不调模型。

指标口径（全部可在产物上复核）：

- `locator_to_reader_rate`：定位工具返回的表/节实体中，后续被读取工具碰过的比例；
- `read_evidence_rate`：读取步中真正新增证据的比例（`evidence_chars_added > 0`）；
- `uncited_read_rate`：读过的实体中未出现在最终引用里的比例（浪费的上界，
  因为探索性阅读是合理的）；
- `first_locator_hit` / `first_read_hit`：第一次定位 / 第一次读取的实体
  是否最终被引用；
- `redundant_read_steps`：实体已读过且本轮没有新增证据的重复读取。

用法（在 backend 目录下，相对路径按 backend 解析）：

    python -m evaluation.evidence_flow --runs ../docs/evidence/finance_ms11_20261104.json
    python -m evaluation.evidence_flow --runs <runs.json> --out <out.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation.agent_metrics import ratio

# 只负责"定位"的工具：返回表 id / 块 id / 章节 id，不是证据。
LOCATOR_TOOLS = ("search_tables", "search_blocks", "search_sections")
# 只负责"清点文档"的工具，返回文档级 id，不参与表/节粒度的转化率。
INVENTORY_TOOLS = ("list_documents",)
# 真正读取原文的工具：返回表 / 行组 / 单元格 / 章节块，可能进证据。
READER_TOOLS = ("get_rows", "get_table", "get_cell", "read_block", "read_section")
CALC_TOOLS = ("calculate",)
FINAL_TOOL = "__final__"


def entity_key(identifier: object) -> str:
    """把各种粒度的 id 归一到"表 / 节"一级。

    `doc#t0006#rows#0#cols#0,1`、`doc#t0006#0#2025 年`、`doc#t0006` 都归到 `doc#t0006`；
    `doc#s0016#b000` 归到 `doc#s0016`；`calc-1` 原样返回。
    """
    text = str(identifier or "")
    if not text:
        return ""
    parts = text.split("#")
    if len(parts) >= 2:
        return "#".join(parts[:2])
    return text


def _tool_steps(result: dict) -> list[dict]:
    return [step for step in result.get("trace") or [] if step.get("tool") != FINAL_TOOL]


def _entity_set(steps: list[dict]) -> set[str]:
    keys = {entity_key(item) for step in steps for item in (step.get("result_ids") or [])}
    keys.discard("")
    return keys


def analyze_run(result: dict) -> dict:
    """单次运行的证据流指标。缺字段时返回 None 而不是 0。"""
    steps = _tool_steps(result)
    tool_names = [str(step.get("tool") or "") for step in steps]

    locator_steps = [step for step in steps if step.get("tool") in LOCATOR_TOOLS]
    reader_steps = [step for step in steps if step.get("tool") in READER_TOOLS]
    calc_steps = [step for step in steps if step.get("tool") in CALC_TOOLS]

    located = _entity_set(locator_steps)
    read = _entity_set(reader_steps)
    cited = {
        entity_key(item.get("chunk_id"))
        for item in (result.get("citations") or [])
        if entity_key(item.get("chunk_id"))
    }
    # 旧产物可能没有 citations 字段；缺字段记 None（不可判），
    # 有字段但为空（拒答）才是真的"读了没引"。
    has_citations = "citations" in result

    # 第一次定位 / 第一次读取分别命中没有。
    first_locator_ids = (
        {entity_key(i) for i in (locator_steps[0].get("result_ids") or [])} if locator_steps else set()
    )
    first_reader_ids = (
        {entity_key(i) for i in (reader_steps[0].get("result_ids") or [])} if reader_steps else set()
    )
    first_locator_ids.discard("")
    first_reader_ids.discard("")

    # 定位调用发生在第一次读取之前的有几次（乱试的代理指标）。
    first_read_index = next(
        (index for index, name in enumerate(tool_names) if name in READER_TOOLS), None
    )
    locator_calls_before_first_read = (
        sum(1 for name in tool_names[:first_read_index] if name in LOCATOR_TOOLS)
        if first_read_index is not None
        else None
    )

    # 重复读取：实体此前已读过，且本轮没有新增证据。
    seen: set[str] = set()
    redundant = 0
    reads_with_evidence = 0
    for step in reader_steps:
        if int(step.get("evidence_chars_added") or 0) > 0:
            reads_with_evidence += 1
        keys = {entity_key(item) for item in (step.get("result_ids") or [])}
        keys.discard("")
        if keys and keys <= seen and int(step.get("evidence_chars_added") or 0) == 0:
            redundant += 1
        seen |= keys

    # 工具失败与恢复：第一次读取失败时 `first_read_hit` 记 False，不是 None。
    failed = [step for step in steps if step.get("ok") is False]
    failure_recovered: bool | None = None
    if failed:
        first_fail = next(index for index, step in enumerate(steps) if step.get("ok") is False)
        failure_recovered = any(
            step.get("ok") is not False for step in steps[first_fail + 1 :]
        )

    return {
        "task_id": str(result.get("task_id") or ""),
        "variant": result.get("variant"),
        "status": result.get("status"),
        "steps": len(steps),
        "tool_sequence": tool_names,
        "search_calls": sum(1 for name in tool_names if name in LOCATOR_TOOLS),
        "inventory_calls": sum(1 for name in tool_names if name in INVENTORY_TOOLS),
        "reader_calls": len(reader_steps),
        "calc_calls": len(calc_steps),
        "located_entities": len(located),
        "read_entities": len(read),
        "located_and_read": len(located & read),
        "cited_entities": len(cited),
        "uncited_read_entities": len(read - cited) if has_citations else None,
        "locator_to_reader_rate": ratio(len(located & read), len(located)),
        "read_evidence_rate": ratio(reads_with_evidence, len(reader_steps)),
        "uncited_read_rate": ratio(len(read - cited), len(read)) if has_citations else None,
        "first_locator_hit": (
            bool(first_locator_ids & cited) if (locator_steps and has_citations) else None
        ),
        "first_read_hit": (
            bool(first_reader_ids & cited) if (reader_steps and has_citations) else None
        ),
        "locator_calls_before_first_read": locator_calls_before_first_read,
        "redundant_read_steps": redundant,
        "reads_with_evidence": reads_with_evidence,
        "failed_calls": len(failed),
        "failure_recovered": failure_recovered,
    }


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"runs": 0, "note": "没有可分析的运行"}

    located = sum(row["located_entities"] for row in rows)
    located_and_read = sum(row["located_and_read"] for row in rows)
    read = sum(row["read_entities"] for row in rows)
    cited = sum(row["cited_entities"] for row in rows)
    reader_calls = sum(row["reader_calls"] for row in rows)
    reads_with_evidence = sum(row["reads_with_evidence"] for row in rows)
    redundant = sum(row["redundant_read_steps"] for row in rows)
    # 只有带 citations 字段的运行才参与"读了没引"的分母。
    uncited_rows = [row for row in rows if row["uncited_read_entities"] is not None]

    # 聚合口径按"实体/步数求和再相除"，不是对逐题比率取平均，
    # 避免小题权重被放大。分子只数"定位到且读过"的实体：
    # 直接读没定位过的表不算转化，否则会把绕路读法算成高转化。
    return {
        "runs": len(rows),
        "steps_total": sum(row["steps"] for row in rows),
        "search_calls_total": sum(row["search_calls"] for row in rows),
        "reader_calls_total": reader_calls,
        "calc_calls_total": sum(row["calc_calls"] for row in rows),
        "located_entities_total": located,
        "located_and_read_total": located_and_read,
        "read_entities_total": read,
        "cited_entities_total": cited,
        "uncited_read_entities_total": sum(
            row["uncited_read_entities"] for row in uncited_rows
        ),
        "locator_to_reader_rate": ratio(located_and_read, located),
        "read_evidence_rate": ratio(reads_with_evidence, reader_calls),
        "uncited_read_rate": ratio(
            sum(row["uncited_read_entities"] for row in uncited_rows),
            sum(row["read_entities"] for row in uncited_rows),
        ),
        "redundant_read_steps_total": redundant,
        "redundant_read_rate": ratio(redundant, reader_calls),
        "first_locator_hit_runs": sum(1 for row in rows if row["first_locator_hit"] is True),
        "first_locator_hit_rate": ratio(
            sum(1 for row in rows if row["first_locator_hit"] is True),
            sum(1 for row in rows if row["first_locator_hit"] is not None),
        ),
        "first_read_hit_runs": sum(1 for row in rows if row["first_read_hit"] is True),
        "first_read_hit_rate": ratio(
            sum(1 for row in rows if row["first_read_hit"] is True),
            sum(1 for row in rows if row["first_read_hit"] is not None),
        ),
        "failed_calls_total": sum(row["failed_calls"] for row in rows),
        "runs_with_failures": sum(1 for row in rows if row["failed_calls"]),
        "failure_recovery_rate": ratio(
            sum(1 for row in rows if row["failure_recovered"] is True),
            sum(1 for row in rows if row["failure_recovered"] is not None),
        ),
        "definition": {
            "locator_to_reader_rate": "定位工具返回的表/节实体中，被读取工具碰过的比例",
            "read_evidence_rate": "读取步中 evidence_chars_added > 0 的比例",
            "uncited_read_rate": "读过的实体未出现在最终引用里的比例（浪费上界）",
            "first_locator_hit": "第一次定位返回的实体是否最终被引用",
            "redundant_read_steps": "实体已读过且本轮未新增证据的重复读取",
            "failure_recovery_rate": "有工具失败的运行里，失败之后还能继续成功的比例",
        },
    }


def analyze_payload(payload: object) -> dict:
    """接受 `{"results": [...]}` 或裸列表；返回 `{"cross": ..., "rows": [...]}`。"""
    if isinstance(payload, list):
        results = payload
    elif isinstance(payload, dict):
        results = list(payload.get("results") or [])
    else:
        results = []
    rows = [analyze_run(result) for result in results if isinstance(result, dict)]
    return {"cross": aggregate(rows), "rows": rows}


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="证据流分析（定位 → 读取 → 引用，零模型调用）")
    parser.add_argument("--runs", required=True, help="runs JSON 路径")
    parser.add_argument("--out", default="", help="结果 JSON 路径")
    args = parser.parse_args()

    runs_path = Path(args.runs)
    if not runs_path.is_absolute():
        runs_path = _BACKEND_DIR / runs_path
    report = analyze_payload(_load(runs_path))
    report["runs_file"] = str(runs_path)

    cross = report["cross"]
    print("证据流分析：")
    print(
        f"  运行 {cross['runs']} 条   步数 {cross['steps_total']}   "
        f"检索 {cross['search_calls_total']}   读取 {cross['reader_calls_total']}   "
        f"计算 {cross['calc_calls_total']}"
    )
    print(
        f"  定位→读取 {_pct(cross['locator_to_reader_rate'])}   "
        f"读取有效 {_pct(cross['read_evidence_rate'])}   "
        f"读了没引 {_pct(cross['uncited_read_rate'])}   "
        f"重复读取 {_pct(cross['redundant_read_rate'])}   "
        f"首轮定位命中 {_pct(cross['first_locator_hit_rate'])}   "
        f"首轮读取命中 {_pct(cross['first_read_hit_rate'])}   "
        f"失败恢复 {_pct(cross['failure_recovery_rate'])}"
    )
    for row in report["rows"]:
        print(
            f"  {row['task_id']:<8} 定位→读取 {_pct(row['locator_to_reader_rate']):>6}"
            f"  读取有效 {_pct(row['read_evidence_rate']):>6}"
            f"  重复读取 {row['redundant_read_steps']}"
            f"  首轮定位 {'-' if row['first_locator_hit'] is None else int(row['first_locator_hit'])}"
        )

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n结果：{out_path}")
    return 0


def _pct(value: object) -> str:
    return "-" if value is None else f"{float(value):.1%}"


if __name__ == "__main__":
    sys.exit(main())
