"""任务坐标重绑：语料重解析后，按内容签名把表级坐标找回来。

每轮 `--force` 重解析都会重排 `table_id`（按页序枚举）。任务卡里的三处硬编码
坐标——`evidence.table_id`、`answer_points[].chunk_ids`、`relevant_chunk_ids`
——重排后要么悬空、要么指向别的表。2026-11-03 手工重绑过 3 条（FIN-05/28/29），
本工具把这一步变成命令：

- 签名 = 同一文档内「`(row, column)` 的 `get_cell` 值 == 任务卡 `value`」命中；
- 命中恰好一张表 ⇒ 重绑三处坐标；0 张或多张 ⇒ 列出来人工处理，不猜；
- 原表仍然命中（重解析没动它）⇒ 不动，避免无意义改写；
- 默认 dry-run，`--write` 才落盘；任务文件按行重写，非任务行原样保留。

用法：
    python -m evaluation.rebind_coords --tasks evaluation/finance_tasks.jsonl
    python -m evaluation.rebind_coords --tasks evaluation/finance_tasks.jsonl --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.store import FinanceStore
from evaluation.finance_corpus import DOCUMENT_DIR

_DIGITS = str.maketrans("", "", " ,\u00a0")


def _norm(value: object) -> str:
    return str(value or "").translate(_DIGITS).strip()


def _doc_of(table_id: str) -> str:
    return str(table_id).split("#")[0]


def _tables_of(documents: list[dict], doc_id: str) -> list[str]:
    for document in documents:
        if str(document.get("doc_id")) == doc_id:
            return [str(table.get("table_id")) for table in document.get("tables") or []]
    return []


def _known_refs(documents: list[dict]) -> set[str]:
    refs: set[str] = set()
    for document in documents:
        refs.update(str(table.get("table_id")) for table in document.get("tables") or [])
        refs.update(str(section.get("section_id")) for section in document.get("sections") or [])
    return refs


def find_candidate_tables(
    store: FinanceStore,
    documents: list[dict],
    old_table_id: str,
    row: object,
    column: object,
    value: object,
) -> list[str]:
    """同一文档内，找 `(row, column)` 的单元格值等于任务卡 value 的表。"""
    if row is None or column is None or value is None:
        return []
    candidates: list[str] = []
    for table_id in _tables_of(documents, _doc_of(old_table_id)):
        try:
            cell = store.get_cell(table_id, row, column)
        except Exception:  # noqa: BLE001, S112 - 取不到就是这张表没有该行/列
            continue
        if _norm(cell.get("cell_text")) == _norm(value):
            candidates.append(table_id)
    return candidates


def rebind_task(
    task: dict, store: FinanceStore, documents: list[dict]
) -> tuple[bool, list[str], list[str]]:
    """重绑一条任务的三处坐标。返回 (changed, changes, manual)。"""
    tag = task.get("id") or "?"
    changes: list[str] = []
    manual: list[str] = []
    mapping: dict[str, str] = {}
    known = _known_refs(documents)
    for index, point in enumerate(task.get("answer_points") or []):
        evidence = point.get("evidence") or {}
        old = str(evidence.get("table_id") or "")
        if not old or old in mapping:
            continue
        candidates = find_candidate_tables(
            store, documents, old, evidence.get("row"), evidence.get("column"), evidence.get("value")
        )
        if old in candidates:
            continue  # 原表仍然命中：重解析没动它
        if not candidates:
            manual.append(
                f"[{tag}] answer_points[{index}] 找不到内容签名：{old} "
                f"{evidence.get('row')!r}/{evidence.get('column')!r}={evidence.get('value')!r}"
            )
            continue
        if len(candidates) > 1:
            manual.append(f"[{tag}] answer_points[{index}] 内容签名命中多张表 {candidates}，需人工确认")
            continue
        mapping[old] = candidates[0]
        changes.append(f"{old} → {candidates[0]}")

    def rewrite_refs(refs: list, label: str) -> list:
        rewritten: list = []
        for ref in refs or []:
            text = str(ref)
            anchor = text.split("#rows#")[0].split("#b")[0]
            if text in mapping:
                text = mapping[text]
            elif anchor in mapping:
                text = mapping[anchor] + text[len(anchor):]
            elif text not in known and anchor not in known:
                manual.append(f"[{tag}] {label} 引用无法解析：{text}")
            rewritten.append(text)
        return rewritten

    for point in task.get("answer_points") or []:
        if "chunk_ids" in point:
            point["chunk_ids"] = rewrite_refs(point.get("chunk_ids"), "chunk_ids")
        evidence = point.get("evidence")
        if isinstance(evidence, dict) and evidence.get("table_id") in mapping:
            evidence["table_id"] = mapping[evidence["table_id"]]
    if "relevant_chunk_ids" in task:
        task["relevant_chunk_ids"] = rewrite_refs(task.get("relevant_chunk_ids"), "relevant_chunk_ids")

    return bool(changes), changes, manual


def rebind_file(
    path: Path, store: FinanceStore, documents: list[dict], *, write: bool = False
) -> tuple[int, int, list[str]]:
    """重绑一个任务文件；返回 (changed_tasks, manual_count, report_lines)。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    changed_tasks = 0
    report: list[str] = []
    manual_total: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(line)
            continue
        try:
            task = json.loads(stripped)
        except json.JSONDecodeError:
            output.append(line)
            continue
        changed, changes, manual = rebind_task(task, store, documents)
        if changed:
            changed_tasks += 1
            report.append(f"[{task.get('id')}] " + "；".join(changes))
            output.append(json.dumps(task, ensure_ascii=False))
        else:
            output.append(line)
        manual_total.extend(manual)
    if write and changed_tasks:
        path.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    return changed_tasks, len(manual_total), report + manual_total


def main() -> int:
    parser = argparse.ArgumentParser(description="按内容签名重绑任务坐标（表 id 重排后）")
    parser.add_argument("--tasks", action="append", default=[], help="任务 JSONL，可多次给")
    parser.add_argument("--write", action="store_true", help="落盘；默认只 dry-run")
    args = parser.parse_args()
    if not args.tasks:
        parser.error("至少给一个 --tasks")
    store = FinanceStore(DOCUMENT_DIR)
    documents = store.documents()
    manual = 0
    for raw in args.tasks:
        path = Path(raw)
        if not path.is_absolute():
            path = _BACKEND_DIR / path
        if not path.exists():
            raise SystemExit(f"任务文件不存在: {path}")
        changed, manual_count, report = rebind_file(path, store, documents, write=args.write)
        manual += manual_count
        print(f"{path.name}: 重绑 {changed} 条，人工 {manual_count} 条")
        for line in report:
            print("  " + line)
    if args.write:
        print("已写回任务文件")
    else:
        print("dry-run：加 --write 落盘")
    return 1 if manual else 0


if __name__ == "__main__":
    sys.exit(main())
