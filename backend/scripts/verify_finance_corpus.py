"""语料验收：用 FinanceStore 回答几条"跳两年"的固定问题，并与报告自报的同比核对。

这不是评测集，是**解析验收**：如果连"营业收入同比"都取不出来或算不对，
后面 30 条题不用出——先修解析。

判据（任一不满足即退出码 1）：
1. 能定位到含两个以上年份列、且行标签为「营业收入」的表；
2. 本期/上期两个数都能取到；
3. `calculate` 算出的同比与报告自报同比的差 ≤ 0.02 个百分点；
4. 表格带页码。

用法：
    python backend/scripts/verify_finance_corpus.py
    python backend/scripts/verify_finance_corpus.py --json docs/finance/corpus_verify.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.calc import calculate  # noqa: E402
from app.finance.store import FinanceStore  # noqa: E402

TOLERANCE = 0.02
_YEAR_RE = re.compile(r"20\d{2}")
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def to_number(text: str) -> float | None:
    if not text:
        return None
    cleaned = str(text).replace(",", "").replace(" ", "")
    match = _NUMBER_RE.search(cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def year_columns(columns: list[str]) -> list[str]:
    return [name for name in columns if _YEAR_RE.search(str(name))]


def ratio_column(columns: list[str]) -> str | None:
    for name in columns:
        if any(token in str(name) for token in ("增减", "同比", "变动")):
            return name
    return None


def cell(table: dict, row_name: str, column: str) -> str | None:
    for row in table["rows"]:
        if row and str(row[0]).strip().startswith(row_name):
            for index, name in enumerate(table["columns"]):
                if str(name).strip() == column and index < len(row):
                    return row[index]
    return None


def check_document(store: FinanceStore, document: dict) -> dict:
    doc_id = document["doc_id"]
    record = {
        "doc_id": doc_id,
        "title": document.get("title"),
        "doc_type": document.get("doc_type"),
        "result": "fail",
        "checks": {},
    }
    # 只有年报有《主要会计数据》这种"同表跳两年"的结构；募集说明书的无框线表格
    # PyMuPDF 抽不出来（实测 105 页只识别 39 张表，且不含营业收入表），
    # 它的数值仍在正文里，v1 只按文本+页码使用，不做表格验收。
    if document.get("doc_type") != "annual_report":
        record["result"] = "n/a"
        record["checks"]["reason"] = "非年报，跳过同表跳两年验收"
        record["checks"]["sections"] = len(document.get("sections") or [])
        record["checks"]["tables"] = len(document.get("tables") or [])
        return record

    candidates = []
    for table in document.get("tables") or []:
        columns = table.get("columns") or []
        if len(year_columns(columns)) < 2:
            continue
        if not any(row and str(row[0]).strip().startswith("营业收入") for row in table.get("rows") or []):
            continue
        candidates.append(table)
    if not candidates:
        record["checks"]["table_found"] = False
        return record

    table = candidates[0]
    years = year_columns(table["columns"])
    current_col, prior_col = years[0], years[1]
    current = to_number(cell(table, "营业收入", current_col) or "")
    prior = to_number(cell(table, "营业收入", prior_col) or "")
    record["checks"]["table_found"] = True
    record["table"] = {
        "table_id": table["table_id"],
        "page": table.get("page"),
        "page_end": table.get("page_end", table.get("page")),
        "caption": table.get("caption"),
        "unit": table.get("unit"),
        "columns": table["columns"],
        "current_column": current_col,
        "prior_column": prior_col,
        "current": current,
        "prior": prior,
    }
    if current is None or prior is None or not prior:
        record["checks"]["values_readable"] = False
        return record
    record["checks"]["values_readable"] = True

    computed = calculate(
        "(cur - prior) / prior * 100",
        {"cur": current, "prior": prior},
    )
    record["calc"] = computed
    computed_pct = float(computed["result"])

    reported_col = ratio_column(table["columns"])
    reported = to_number(cell(table, "营业收入", reported_col) or "") if reported_col else None
    record["checks"]["reported_ratio"] = {"column": reported_col, "value": reported}
    if reported is None:
        record["checks"]["ratio_matches"] = None
        record["checks"]["page_present"] = isinstance(table.get("page"), int)
        record["result"] = "partial"
        return record

    delta = abs(computed_pct - reported)
    record["delta"] = round(delta, 4)
    record["checks"]["ratio_matches"] = delta <= TOLERANCE
    record["checks"]["page_present"] = isinstance(table.get("page"), int)
    if record["checks"]["ratio_matches"] and record["checks"]["page_present"]:
        record["result"] = "pass"
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="金融语料解析验收")
    parser.add_argument("--json", dest="json_path", default="")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)

    manifest_path = _REPO_ROOT / "docs" / "finance" / "corpus_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(args.root) if args.root else _REPO_ROOT / (manifest.get("storage") or {}).get(
        "document_dir", "data/finance/documents"
    )
    store = FinanceStore(root)

    records = []
    for meta in manifest.get("documents") or []:
        document = store.get_document(meta["doc_id"])
        record = check_document(store, document)
        records.append(record)
        table_info = record.get("table") or {}
        print(
            f"[{record['result']:>7}] {record['doc_id']:<32} "
            f"p{table_info.get('page', '-')} "
            f"{record.get('calc', {}).get('result', '-')}% "
            f"vs 自报 {record.get('checks', {}).get('reported_ratio', {}).get('value', '-')}"
            f"{'' if record['result'] == 'pass' else '  ← ' + str(record['checks'])}"
        )

    passed = sum(1 for record in records if record["result"] == "pass")
    required = [record for record in records if record["result"] != "n/a"]
    payload = {
        "tolerance_pct": TOLERANCE,
        "passed": passed,
        "required": len(required),
        "total": len(records),
        "documents": records,
    }
    if args.json_path:
        target = Path(args.json_path)
        if not target.is_absolute():
            target = _REPO_ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n报告：{target}")
    print(f"通过 {passed}/{len(required)}（另有 {len(records) - len(required)} 份不适用）")
    return 0 if passed == len(required) else 1


if __name__ == "__main__":
    sys.exit(main())
