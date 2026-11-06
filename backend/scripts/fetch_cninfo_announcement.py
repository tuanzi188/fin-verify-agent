"""按「股票代码 + 年份」把巨潮的年报公告接进固定语料链路。

固定路径（每一步都可单独复现）：

1. 解析 orgId：巨潮的公告检索必须带 `代码,orgId`，只给代码会静默返回 0 条；
2. 查公告列表，只认标题恰为「{year}年年度报告」的那一条（摘要/英文/更正已排除）；
3. `--dry-run` 到此为止，打印候选与被排除项，人工确认后才动 manifest；
4. 正式执行：写入 `docs/finance/corpus_manifest.json` → 复用
   `fetch_finance_corpus` 下载并回填 sha256/size → 复用 `ingest_finance_corpus`
   的解析函数写 document.json → 回填 page_count/parser_version。

用法：

    python backend/scripts/fetch_cninfo_announcement.py --code 000333 --year 2025 --dry-run
    python backend/scripts/fetch_cninfo_announcement.py --code 000333 --year 2025
    python backend/scripts/fetch_cninfo_announcement.py --code 000333 --year 2025 --unit 千元 --force

不做的事：翻页遍历、绕过验证码、替换已有文档之外的任何维护动作。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import requests

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.cninfo import (
    announcement_query_payload,
    build_manifest_document,
    parse_announcement_body,
    pick_annual_report,
    upsert_manifest_document,
)
from app.finance.ingest import ingest_pdf, summarize, write_document

MANIFEST_PATH = _REPO_ROOT / "docs" / "finance" / "corpus_manifest.json"
STOCK_CACHE = _REPO_ROOT / "data" / "finance" / "meta" / "cninfo_stocks.json"

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STOCK_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
DISCLOSURE_PAGE = "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": DISCLOSURE_PAGE,
    "Origin": "http://www.cninfo.com.cn",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


def load_script_module(name: str):
    """同目录的脚本不是包，按路径加载即可复用它的下载/校验函数。"""
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


STOCK_CACHE_TTL_DAYS = 7


def resolve_org_id(session: requests.Session, code: str) -> str:
    """orgId 从巨潮的全市场股票表里查；缓存 7 天，避免每次重新拉 580KB。

    设 TTL 而不是永久缓存：新股上市后表会变，永久缓存会让新代码永远查不到。
    """
    stocks: list[dict] = []
    if STOCK_CACHE.exists():
        age_seconds = time.time() - STOCK_CACHE.stat().st_mtime
        if age_seconds < STOCK_CACHE_TTL_DAYS * 86400:
            try:
                cached = json.loads(STOCK_CACHE.read_text(encoding="utf-8"))
                stocks = cached.get("stockList") or []
            except (json.JSONDecodeError, OSError):
                stocks = []
    if not stocks:
        response = session.get(STOCK_URL, timeout=30)
        response.raise_for_status()
        stocks = response.json().get("stockList") or []
        STOCK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        STOCK_CACHE.write_text(
            json.dumps({"stockList": stocks}, ensure_ascii=False), encoding="utf-8"
        )
    for item in stocks:
        if str(item.get("code")) == str(code):
            return str(item.get("orgId") or "")
    raise SystemExit(f"股票代码 {code} 不在巨潮股票表里：确认代码是否属于沪深北 A 股")


def query_announcements(
    session: requests.Session, code: str, org_id: str, year: int
) -> list[dict]:
    # 先访问一次公告页拿 cookie，否则接口会返回 0 条（实测）。
    session.get(DISCLOSURE_PAGE, timeout=20)
    payload = announcement_query_payload(code, org_id, year=year)
    response = session.post(QUERY_URL, data=payload, timeout=30)
    response.raise_for_status()
    return parse_announcement_body(response.json())


def print_choice(choice, document: dict | None) -> None:
    if choice.chosen:
        chosen = choice.chosen
        print(f"\n选定：{chosen['title']}  ({chosen['date']})")
        print(f"      公告号 {chosen['announcement_id']}  {chosen['pdf_url']}")
        if chosen.get("declared_size_kb"):
            print(f"      公告声明大小 {chosen['declared_size_kb']}KB")
    else:
        print(f"\n未选定：{choice.reason}")
    if choice.rejected:
        print("\n被排除的候选（最多显示 8 条）：")
        for item in choice.rejected[:8]:
            print(f"  - {item.get('date') or '日期未知'}  {item.get('title')}  [{item.get('why')}]")
    if document:
        print("\n将写入 manifest 的条目：")
        print(json.dumps(document, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按代码+年份入库巨潮年报")
    parser.add_argument("--code", required=True, help="六位股票代码，如 000333")
    parser.add_argument("--year", required=True, type=int, help="报告期年份，如 2025")
    parser.add_argument("--unit", default="千元", help="文档级单位默认值（人工确认项）")
    parser.add_argument("--dry-run", action="store_true", help="只打印候选，不写 manifest、不下载")
    parser.add_argument("--force", action="store_true", help="已有 document.json 也重下重解")
    args = parser.parse_args(argv)

    session = requests.Session()
    session.headers.update(HEADERS)

    org_id = resolve_org_id(session, args.code)
    print(f"orgId: {args.code} -> {org_id}")
    items = query_announcements(session, args.code, org_id, args.year)
    print(f"公告候选：{len(items)} 条（{args.year} 年窗口）")

    choice = pick_annual_report(items, args.year)
    document = None
    if choice.chosen:
        document = build_manifest_document(
            choice.chosen, year=args.year, unit=args.unit
        )
    print_choice(choice, document)

    if not choice.ok or document is None:
        return 1
    if args.dry_run:
        print("\n--dry-run：未写入 manifest、未下载。")
        return 0

    manifest = load_manifest()
    action = upsert_manifest_document(manifest, document)
    print(f"\nmanifest: {action} {document['doc_id']}")

    fetch_module = load_script_module("fetch_finance_corpus")
    record = fetch_module.fetch_one(manifest, document, check_only=False)
    print(f"[{record['status']:>10}] 下载  {record['detail']}")
    if record["status"] == "failed":
        save_manifest(manifest)
        return 1
    save_manifest(manifest)

    storage = manifest.get("storage") or {}
    pdf_dir = _REPO_ROOT / storage.get("pdf_dir", "data/finance/pdfs")
    document_dir = _REPO_ROOT / storage.get("document_dir", "data/finance/documents")
    pdf_path = pdf_dir / f"{document['doc_id']}.pdf"
    target = document_dir / document["doc_id"] / "document.json"

    if target.exists() and not args.force:
        cached = json.loads(target.read_text(encoding="utf-8"))
        print(f"[      skip] 解析  {document['doc_id']} 已解析（--force 可重解）")
        print(f"QC: {json.dumps(summarize(cached), ensure_ascii=False)}")
        return 0

    started = time.time()
    parsed = ingest_pdf(pdf_path, document)
    write_document(parsed, document_dir)
    qc = summarize(parsed)
    document["page_count"] = qc["pages"]
    document["parser_version"] = qc["parser_version"]
    document["tree_version"] = parsed["tree_version"]
    save_manifest(manifest)

    print(
        f"[        ok] 解析  {document['doc_id']}  {qc['pages']}p {qc['sections']}sec "
        f"{qc['tables']}tab  {time.time() - started:.1f}s body={qc['body_font_size']}"
    )
    print(f"\nQC：{json.dumps(qc, ensure_ascii=False)}")
    print("提醒：单位/口径是自动填充的默认值，请核对文档首页「金额单位」后再用于对外报告。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
