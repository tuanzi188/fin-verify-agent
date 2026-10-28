"""下载固定金融评测语料到本地，并把 hash/size 回填进 manifest。

为什么单独一个脚本：语料必须可复现但 PDF 不入库。这个脚本做三件事——

1. 按 `docs/finance/corpus_manifest.json` 里的 URL 下载到 `data/finance/pdfs/`；
2. 算 sha256、比对公告页声明的大小，把 `fetched_at/size/sha256/path` 写回 manifest；
3. 已存在且 hash 一致的文件直接跳过（幂等），不重复下载。

边界：
- 只抓 manifest 里列出的公告，不做站点遍历、不绕验证码；
- 下载有间隔，失败最多重试 2 次，不无限重压；
- 内容与声明大小差太多（>5%）直接判失败——截断的 PDF 会在解析阶段
  变成"表格缺失"，最难查。

用法：

    python backend/scripts/fetch_finance_corpus.py            # 下载缺失的
    python backend/scripts/fetch_finance_corpus.py --check    # 只校验已下载文件
    python backend/scripts/fetch_finance_corpus.py --only cninfo-600519-2025-annual
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = _REPO_ROOT / "docs" / "finance" / "corpus_manifest.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
SIZE_TOLERANCE = 0.05
DOWNLOAD_DELAY = 1.5
MAX_RETRIES = 2
CHUNK_SIZE = 1 << 16


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise SystemExit(f"manifest 不存在: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def target_path(manifest: dict, document: dict) -> Path:
    pdf_dir = _REPO_ROOT / (manifest.get("storage") or {}).get(
        "pdf_dir", "data/finance/pdfs"
    )
    return pdf_dir / f"{document['doc_id']}.pdf"


def download(url: str, destination: Path) -> int:
    """流式下载到临时文件，成功后再改名——避免半截文件被当成完整 PDF。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".pdf.part")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": "http://www.cninfo.com.cn/"})

    written = 0
    with session.get(url, timeout=(10, 120), stream=True) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        with partial.open("wb") as handle:
            for chunk in response.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)
    if declared and written != int(declared):
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"下载体不完整：{written} != {declared} 字节")
    partial.replace(destination)
    return written


def verify_size(document: dict, size: int) -> str:
    declared_kb = document.get("declared_size_kb")
    if not isinstance(declared_kb, (int, float)) or declared_kb <= 0:
        return ""
    expected = float(declared_kb) * 1024
    drift = abs(size - expected) / expected
    if drift > SIZE_TOLERANCE:
        return f"大小偏差 {drift:.1%}（声明 {declared_kb}KB，实际 {size / 1024:.0f}KB）"
    return ""


def fetch_one(manifest: dict, document: dict, *, check_only: bool) -> dict:
    doc_id = document["doc_id"]
    path = target_path(manifest, document)
    record = {"doc_id": doc_id, "status": "", "detail": ""}

    if path.exists() and document.get("sha256"):
        actual = sha256_of(path)
        if actual == document["sha256"]:
            record.update(status="skip", detail=f"已存在 {path.stat().st_size} 字节")
            return record
        record["detail"] = "本地文件 hash 与 manifest 不符，重下"

    if check_only:
        record.update(
            status="missing" if not path.exists() else "hash_mismatch",
            detail=record["detail"] or "尚未下载",
        )
        return record

    url = document["url"]
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            size = download(url, path)
            break
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 * (attempt + 1))
    else:
        record.update(status="failed", detail=f"{type(last_error).__name__}: {last_error}")
        return record

    digest = sha256_of(path)
    warning = verify_size(document, size)
    document.update(
        {
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "size": size,
            "sha256": digest,
            "path": str(path.relative_to(_REPO_ROOT)).replace("\\", "/"),
            "content_type": "application/pdf",
        }
    )
    record.update(
        status="warn" if warning else "ok",
        detail=warning or f"{size / 1024:.0f}KB sha256={digest[:12]}…",
    )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载固定金融评测语料")
    parser.add_argument("--check", action="store_true", help="只校验，不下载")
    parser.add_argument("--only", action="append", default=[], help="只处理指定 doc_id")
    args = parser.parse_args(argv)

    manifest = load_manifest()
    documents = manifest.get("documents") or []
    if args.only:
        wanted = set(args.only)
        documents = [doc for doc in documents if doc.get("doc_id") in wanted]
        if len(documents) != len(wanted):
            known = {doc.get("doc_id") for doc in manifest.get("documents") or []}
            raise SystemExit(f"未知 doc_id: {sorted(wanted - known)}")

    results = []
    for index, document in enumerate(documents):
        record = fetch_one(manifest, document, check_only=args.check)
        results.append(record)
        print(f"[{record['status']:>12}] {record['doc_id']}  {record['detail']}")
        if not args.check and record["status"] in {"ok", "warn"} and index < len(documents) - 1:
            time.sleep(DOWNLOAD_DELAY)

    if not args.check:
        save_manifest(manifest)

    ok = sum(1 for item in results if item["status"] in {"ok", "skip"})
    print(f"\n完成 {ok}/{len(results)}；manifest: {MANIFEST_PATH}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
