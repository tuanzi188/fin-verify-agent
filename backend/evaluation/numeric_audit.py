"""数值审计：把"数值幻觉"从 judge 的感觉变成可复算的确定性读数。

金融场景的硬红线是「数值幻觉率 = 0」。但"幻觉"必须先定义清楚，否则会变成
一句口号。这里拆成两个口径：

1. **数值幻觉（硬红线）**：judge 判定某个要点"已覆盖"，但答案里找不到与金标
   一致的数值（含单位换算与合理舍入）。这表示模型给出了一个**与金标冲突**的数，
   而不是"没答到"——没答到是覆盖问题，答错数是幻觉问题，两者不能混。
2. **未溯源数值（提示项）**：答案里的数值既不在证据原文、也不在问题/金标/计算
   结果里。它可能是模型自己推的中间值（runner 的补算闸门要求比率走 calculate），
   也可能是编的；单列出来供人工复核，不直接当硬红线——否则派生值会造成假阳性。

另外报 **金标数值覆盖率**：答案里出现了多少金标数值。它和 judge 的要点通过率
互相校验：judge 说 pass 但金标数值不在答案里，就是 §1 的幻觉事件。

全部零模型调用：只读 task / result / 语料原文。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Optional

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation._runner_common import load_tasks
from evaluation.agent_checks import base_chunk_id, evidence_chunk_ids
from evaluation.agent_metrics import ratio
from evaluation.calc_schema import calc_specs
from evaluation.equivalence import (
    DEFAULT_REL_TOLERANCE,
    MONEY_ABS_TOLERANCE,
    numbers_in,
    same_number,
    to_base,
    value_in_text,
)

#: 引用标记（半角/全角、可带页码与表名）不是数值声明，先剔掉再抽数。
_CITATION_MARKER = re.compile(r"[\[【]\s*\d{1,2}(?:[，,、\s][^\]】]*)?[\]】]")
_PAGE_NUMBER = re.compile(r"第\s*\d+\s*页")
_DEFAULT_UNIT = re.compile(r"单位\s*[:：]\s*(亿元|万元|千元|元)")
_MONEY_UNITS = {"元", "千元", "万元", "亿元"}

#: PII 检测：公开年报里的公司电话/邮箱不算个人隐私，所以除了邮箱，
#: 其余类型都要求上下文词，避免把财务金额误判成身份证/银行卡。
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PII_CONTEXT = {
    "id_card": ("身份证", "证件号"),
    "phone": ("电话", "手机", "联系方式", "联系人"),
    "bank_card": ("银行卡", "卡号", "账号"),
}


def pii_hits(text: str) -> list[dict]:
    """保守的 PII 检测：邮箱直接认；证件/电话/卡号必须有上下文词。"""
    value = str(text or "")
    hits: list[dict] = []
    for match in _EMAIL.finditer(value):
        hits.append({"type": "email", "raw": match.group(0)})
    for kind, pattern in (("id_card", _ID_CARD), ("phone", _PHONE), ("bank_card", _BANK_CARD)):
        for match in pattern.finditer(value):
            window = value[max(0, match.start() - 12): match.end() + 4]
            if any(word in window for word in _PII_CONTEXT[kind]):
                hits.append({"type": kind, "raw": match.group(0)})
    return hits


def strip_citation_markers(text: str) -> str:
    return _CITATION_MARKER.sub("", str(text or ""))


def _default_unit(content: str) -> str:
    match = _DEFAULT_UNIT.search(str(content or ""))
    return match.group(1) if match else ""


def _is_year(number: dict) -> bool:
    value = number["value"]
    return (
        value == value.to_integral_value()
        and Decimal(1900) <= value <= Decimal(2100)
        and "年" in number.get("raw", "")
    )


def _is_page_number(text: str, number: dict) -> bool:
    start = max(0, int(number.get("position") or 0) - 2)
    end = min(len(text), int(number.get("position") or 0) + len(number.get("raw") or "") + 2)
    return bool(_PAGE_NUMBER.search(text[start:end]))


def data_numbers(text: str) -> list[dict]:
    """答案里的"数据数值"：剔引用标记、年份、页码。"""
    cleaned = strip_citation_markers(text)
    return [
        number
        for number in numbers_in(cleaned)
        if not _is_year(number) and not _is_page_number(cleaned, number)
    ]


def _chunk_contents(result: dict, chunks: dict[str, dict]) -> list[dict]:
    """这条运行实际见过的证据原文（去重、保序）。

    两个来源：result["evidence"]（RAG 臂直接带正文）与 trace 里的 result_ids
    （Agent 只带 id，正文从语料回查）。块/单元格锚点先折回表/章节。
    """
    seen: list[dict] = []
    seen_ids: set[str] = set()

    for item in result.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id") or "")
        content = str(item.get("content") or "")
        if chunk_id and chunk_id not in seen_ids:
            seen_ids.add(chunk_id)
            seen.append(
                {
                    "chunk_id": chunk_id,
                    "content": content,
                    "default_unit": _default_unit(content),
                }
            )

    for chunk_id in evidence_chunk_ids(result):
        base = base_chunk_id(chunk_id)
        if base in seen_ids:
            continue
        chunk = chunks.get(base) or chunks.get(chunk_id) or {}
        content = str(chunk.get("content") or "")
        if not content:
            continue
        seen_ids.add(base)
        seen.append(
            {
                "chunk_id": base,
                "content": content,
                "default_unit": _default_unit(content),
            }
        )
    return seen


def _source_numbers(
    result: dict,
    task: dict,
    chunks: dict[str, dict],
) -> list[dict]:
    """答案里的数值允许溯源到哪里：证据原文、问题、金标要点、calc 期望值。"""
    sources: list[dict] = []
    for chunk in _chunk_contents(result, chunks):
        for number in numbers_in(chunk["content"]):
            sources.append(
                {
                    **number,
                    "source": chunk["chunk_id"],
                    "default_unit": chunk.get("default_unit") or "",
                }
            )
    for label, text in (
        ("question", str(task.get("question") or "")),
        ("gold", " ".join(str(point.get("text") or "") for point in task.get("answer_points") or [])),
        ("calc", " ".join(str(spec.get("expected") or "") for spec in calc_specs(task))),
    ):
        for number in numbers_in(text):
            sources.append({**number, "source": label, "default_unit": ""})
    return sources


def _matches_source(answer_number: dict, source_number: dict) -> bool:
    """答案数值是否可溯源到某个来源数值。

    三种比法都要试：
    1. 原样（同单位/无单位）；
    2. 来源按所在表格的默认单位解释（表头写"单位：千元"，单元格本身不带单位）；
    3. 金额跨单位换算。
    百分比不做 ×100 换算——0.29 与 29% 不是同一个数。
    """
    answer_unit = answer_number.get("unit") or ""
    source_unit = source_number.get("unit") or ""
    default_unit = source_number.get("default_unit") or ""

    if same_number(answer_number["value"], source_number["value"]):
        return True

    for candidate_unit in {source_unit, default_unit} - {""}:
        if answer_unit in _MONEY_UNITS and candidate_unit in _MONEY_UNITS:
            left = to_base(answer_number["value"], answer_unit)
            right = to_base(source_number["value"], candidate_unit)
            if same_number(
                left,
                right,
                rel_tolerance=DEFAULT_REL_TOLERANCE,
                abs_tolerance=MONEY_ABS_TOLERANCE,
            ):
                return True
    return False


def _judge_points(judgment: Optional[dict]) -> dict[int, dict]:
    if not judgment:
        return {}
    return {
        int(point.get("index") or 0): point
        for point in judgment.get("points") or []
        if isinstance(point, dict)
    }


def audit_result(
    task: dict,
    result: dict,
    chunks: dict[str, dict],
    *,
    judgment: Optional[dict] = None,
) -> dict:
    """审一条运行的数值。返回幻觉事件、未溯源数值、金标覆盖率。"""
    answer = str(result.get("answer") or "")
    answer_numbers = data_numbers(answer)
    sources = _source_numbers(result, task, chunks)

    unsourced = []
    for number in answer_numbers:
        if not any(_matches_source(number, source) for source in sources):
            unsourced.append(
                {
                    "raw": number["raw"],
                    "value": str(number["value"]),
                    "unit": number.get("unit") or "",
                }
            )

    judged = _judge_points(judgment)
    point_rows: list[dict] = []
    hallucination_events: list[dict] = []
    covered = 0
    checked = 0
    for index, point in enumerate(task.get("answer_points") or []):
        if not isinstance(point, dict):
            continue
        evidence = point.get("evidence") if isinstance(point.get("evidence"), dict) else {}
        value = evidence.get("value")
        if value is None:
            continue
        checked += 1
        # 金标单位可能在 point.unit 或 evidence.unit：拼回去才能做跨单位等价。
        unit = str(point.get("unit") or evidence.get("unit") or "")
        expected_text = f"{value} {unit}".strip()
        present = value_in_text(expected_text, answer)
        judge_point = judged.get(index) or {}
        judge_pass = judge_point.get("pass")
        if present["match"]:
            covered += 1
        # 幻觉事件：judge 说这个要点答到了，但答案里没有金标数值。
        # 只判"judge 说 pass"的要点，避免把"没答到"也算成幻觉。
        if judge_pass is True and not present["match"]:
            hallucination_events.append(
                {
                    "point_index": index,
                    "expected": str(value),
                    "point_text": str(point.get("text") or ""),
                    "judge_reason": str(judge_point.get("reason") or ""),
                }
            )
        point_rows.append(
            {
                "index": index,
                "expected": str(value),
                "gold_present": bool(present["match"]),
                "matched_raw": present.get("matched_raw"),
                "judge_pass": judge_pass,
                "derived": bool(point.get("derived")),
            }
        )

    judge_passed_points = sum(1 for row in point_rows if row["judge_pass"] is True)
    return {
        "task_id": str(task.get("id")),
        "run_index": int(result.get("run_index") or 1),
        "answer_numbers": len(answer_numbers),
        "unsourced_numbers": unsourced,
        "unsourced_rate": ratio(len(unsourced), len(answer_numbers)),
        "gold_points_checked": checked,
        "gold_points_covered": covered,
        "gold_coverage": ratio(covered, checked),
        "judge_passed_points": judge_passed_points,
        "hallucination_events": hallucination_events,
        "hallucination_rate": ratio(len(hallucination_events), judge_passed_points),
        "pii_hits": pii_hits(answer),
        "points": point_rows,
    }


def aggregate(rows: list[dict]) -> dict:
    answer_numbers = sum(row.get("answer_numbers") or 0 for row in rows)
    unsourced = sum(len(row.get("unsourced_numbers") or []) for row in rows)
    checked = sum(row.get("gold_points_checked") or 0 for row in rows)
    covered = sum(row.get("gold_points_covered") or 0 for row in rows)
    judged = sum(row.get("judge_passed_points") or 0 for row in rows)
    events = sum(len(row.get("hallucination_events") or []) for row in rows)
    pii = sum(len(row.get("pii_hits") or []) for row in rows)
    return {
        "tasks": len(rows),
        "answer_numbers": answer_numbers,
        "unsourced_numbers": unsourced,
        "unsourced_rate": ratio(unsourced, answer_numbers),
        "gold_points_checked": checked,
        "gold_points_covered": covered,
        "gold_coverage": ratio(covered, checked),
        "judge_passed_points": judged,
        "hallucination_events": events,
        "hallucination_rate": ratio(events, judged),
        "pii_hits": pii,
        "tasks_with_pii": [row["task_id"] for row in rows if row.get("pii_hits")],
        "definition": {
            "hallucination": "judge 判 pass 的要点里，答案找不到与金标等价的数值 ⇒ 与金标冲突",
            "unsourced": "答案数值不在证据/问题/金标/calc 中；派生值可能合法，只作提示项",
            "gold_coverage": "有金标数值的要点中，答案出现等价数值的比例",
            "pii": "邮箱直接认；证件/电话/卡号必须有上下文词，避免把财务金额误判成 PII",
        },
        "tasks_with_events": [
            row["task_id"] for row in rows if row.get("hallucination_events")
        ],
    }


def audit_runs(
    tasks: list[dict],
    payload: dict,
    chunks: dict[str, dict],
    *,
    report: Optional[dict] = None,
) -> list[dict]:
    tasks_by_id = {str(task.get("id")): task for task in tasks}
    judgments: dict[tuple[str, int], dict] = {}
    if report:
        for verdict in report.get("verdicts") or []:
            key = (str(verdict.get("task_id")), int(verdict.get("run_index") or 1))
            if verdict.get("judgment"):
                judgments[key] = verdict["judgment"]
    rows = []
    for result in payload.get("results") or []:
        task_id = str(result.get("task_id") or "")
        task = tasks_by_id.get(task_id)
        if task is None:
            continue
        key = (task_id, int(result.get("run_index") or 1))
        rows.append(audit_result(task, result, chunks, judgment=judgments.get(key)))
    return rows


def _print_summary(summary: dict) -> None:
    print("数值审计：")
    print(
        f"  金标覆盖率 {summary['gold_points_covered']}/{summary['gold_points_checked']} "
        f"({(summary.get('gold_coverage') or 0):.1%})"
    )
    rate = summary.get("hallucination_rate")
    print(
        f"  数值幻觉 {summary['hallucination_events']}/{summary['judge_passed_points']} "
        f"({(rate or 0):.1%})" if rate is not None else "  数值幻觉：无 judge 数据"
    )
    print(
        f"  未溯源数值 {summary['unsourced_numbers']}/{summary['answer_numbers']} "
        f"({(summary.get('unsourced_rate') or 0):.1%})"
    )
    if summary.get("pii_hits"):
        print(f"  PII 命中 {summary['pii_hits']} 处：{summary.get('tasks_with_pii')}")
    if summary.get("tasks_with_events"):
        print(f"  有幻觉事件的题：{', '.join(summary['tasks_with_events'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description="数值审计（金标覆盖率 / 数值幻觉 / 未溯源数值）")
    parser.add_argument("--runs", required=True)
    parser.add_argument("--tasks", default="evaluation/finance_tasks_complex.jsonl")
    parser.add_argument("--report", default="", help="score_agent_runs 报告（有 judge 才能算幻觉率）")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    runs_path = Path(args.runs)
    if not runs_path.is_absolute():
        runs_path = _BACKEND_DIR / runs_path
    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    payload = json.loads(runs_path.read_text(encoding="utf-8"))
    tasks = load_tasks(tasks_path)

    from evaluation.finance_corpus import chunk_map

    report = None
    if args.report:
        report_path = Path(args.report)
        if not report_path.is_absolute():
            report_path = _BACKEND_DIR / report_path
        report = json.loads(report_path.read_text(encoding="utf-8"))

    rows = audit_runs(tasks, payload, chunk_map(), report=report)
    summary = aggregate(rows)
    summary["tasks_file"] = str(tasks_path)
    summary["runs_file"] = str(runs_path)
    _print_summary(summary)

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({**summary, "rows": rows}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n结果：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
