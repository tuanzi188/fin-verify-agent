"""把 manifest 里的 PDF 解析成结构化文档库（sections/tables + 页码 + bbox）。

产出：
- `data/finance/documents/<doc_id>/document.json`（`app/finance/store.py` 直接读）
- `data/finance/qc/<doc_id>.json`（解析质量摘要，人工核对用）
- manifest 回填 `page_count / parser_version / tree_version`

用法：
    python backend/scripts/ingest_finance_corpus.py
    python backend/scripts/ingest_finance_corpus.py --only cninfo-600519-2025-annual
    python backend/scripts/ingest_finance_corpus.py --limit 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.ingest import ingest_pdf, summarize, write_document  # noqa: E402

MANIFEST_PATH = _REPO_ROOT / "docs" / "finance" / "corpus_manifest.json"


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_report(rows: list[dict]) -> Path:
    """把解析 QC 写成仓库内的固定产物——语料冻结时，解析质量也要一起冻住。"""
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "parser_version": rows[0]["parser_version"] if rows else None,
        "tree_version": "v1",
        "note": "由 backend/scripts/ingest_finance_corpus.py 生成；PDF 与 document.json 在 data/ 下不入库。",
        "totals": {
            "documents": len(rows),
            "pages": sum(row["pages"] for row in rows),
            "sections": sum(row["sections"] for row in rows),
            "tables": sum(row["tables"] for row in rows),
            "avg_coverage": round(
                sum(row.get("char_coverage") or 0 for row in rows) / len(rows), 3
            )
            if rows
            else 0,
        },
        "documents": rows,
    }
    target = _REPO_ROOT / "docs" / "finance" / "corpus_qc.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析金融评测语料")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="已有 document.json 也重解")
    args = parser.parse_args(argv)

    manifest = load_manifest()
    storage = manifest.get("storage") or {}
    pdf_dir = _REPO_ROOT / storage.get("pdf_dir", "data/finance/pdfs")
    document_dir = _REPO_ROOT / storage.get("document_dir", "data/finance/documents")

    documents = manifest.get("documents") or []
    if args.only:
        wanted = set(args.only)
        documents = [doc for doc in documents if doc.get("doc_id") in wanted]
    if args.limit:
        documents = documents[: args.limit]

    failures = 0
    qc_rows: list[dict] = []
    for document in documents:
        doc_id = document["doc_id"]
        pdf_path = pdf_dir / f"{doc_id}.pdf"
        if not pdf_path.exists():
            print(f"[      missing] {doc_id}  {pdf_path}")
            failures += 1
            continue
        target = document_dir / doc_id / "document.json"
        if target.exists() and not args.force and not args.only:
            # QC 从 document.json 现算，不再单独维护 data/finance/qc 缓存：
            # 缓存会和 document.json 漂（改了 summarize 却忘了重跑），
            # 而 summarize 只读列表，重算是毫秒级。
            cached = json.loads(target.read_text(encoding="utf-8"))
            qc = summarize(cached)
            qc_rows.append(qc)
            print(f"[         skip] {doc_id}  已解析")
            continue

        started = time.time()
        try:
            parsed = ingest_pdf(pdf_path, document)
        except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮整批
            print(f"[       failed] {doc_id}  {type(exc).__name__}: {exc}")
            failures += 1
            continue
        elapsed = time.time() - started
        write_document(parsed, document_dir)

        qc = summarize(parsed)
        qc_rows.append(qc)

        document["page_count"] = qc["pages"]
        document["parser_version"] = qc["parser_version"]
        document["tree_version"] = parsed["tree_version"]

        print(
            f"[           ok] {doc_id}  {qc['pages']}p {qc['sections']}sec "
            f"{qc['tables']}tab(no-caption {qc['tables_without_caption']}) "
            f"{elapsed:.1f}s body={qc['body_font_size']}"
        )

    save_manifest(manifest)
    if qc_rows:
        report = write_report(qc_rows)
        print(f"QC 报告：{report}")
    print(f"\n完成：{len(documents) - failures}/{len(documents)}；文档库 {document_dir}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
