"""用户上传 PDF 的落盘、解析与质量闸门。

与官方语料严格分离：`data/finance/uploads` 不参与评测
（`evaluation.finance_corpus` 只读 `data/finance/documents`），
避免用户文档污染任务金标和读数。

单用户演示级实现：同一份文件按内容哈希去重，重复上传直接返回已有产物。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import fitz

from app.config import DATA_DIR, MAX_UPLOAD_SIZE_BYTES
from app.finance.ingest import PIPELINE_VERSION, ingest_pdf, summarize, write_document

UPLOAD_ROOT = DATA_DIR / "finance" / "uploads"

#: 文字层下限：低于这个值基本是扫描件，解析出来只有空表。
MIN_TEXT_CHARS = 500
#: 单份 PDF 页数上限，防止误传整本手册把进程拖死。
MAX_PAGES = 800
#: 这些类型的文档没有表格就没有查证价值，直接拒绝；其他类型只告警。
TABLE_REQUIRED_TYPES = {"annual_report", "prospectus"}


class UploadError(ValueError):
    """可读的上传失败。status_code 直接映射到 HTTP。"""

    def __init__(self, detail: str, *, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def upload_root() -> Path:
    return UPLOAD_ROOT


def _validate_payload(data: bytes) -> None:
    if not data:
        raise UploadError("空文件", status_code=400)
    if len(data) > MAX_UPLOAD_SIZE_BYTES:
        limit = MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)
        raise UploadError(f"文件超过 {limit}MB 上限", status_code=413)
    if not data.startswith(b"%PDF-"):
        raise UploadError("不是 PDF 文件（缺少 %PDF- 头）", status_code=400)


def _inspect_pdf(path: Path) -> int:
    try:
        doc = fitz.open(path)
    except Exception as exc:  # 损坏的 PDF 统一转成可读错误
        raise UploadError(f"PDF 无法打开: {exc}", status_code=422) from exc
    try:
        if doc.needs_pass:
            raise UploadError("PDF 已加密，暂不支持", status_code=422)
        if doc.page_count > MAX_PAGES:
            raise UploadError(f"页数 {doc.page_count} 超过 {MAX_PAGES} 页上限", status_code=413)
        return doc.page_count
    finally:
        doc.close()


def _response(document: dict, filename: str, status: str, warnings: list[str] | None = None) -> dict:
    return {
        "doc_id": str(document.get("doc_id") or ""),
        "filename": filename,
        "status": status,
        "sections": len(document.get("sections") or []),
        "tables": len(document.get("tables") or []),
        "raw_chars": int(document.get("raw_chars") or 0),
        "pipeline_version": str(document.get("pipeline_version") or ""),
        "warnings": warnings or [],
        "message": "",
    }


def save_and_parse(
    filename: str,
    data: bytes,
    *,
    company: str = "",
    period: str = "",
    doc_type: str = "annual_report",
    force: bool = False,
) -> dict:
    """校验 → 落盘 → 解析 → QC。返回上传结果摘要。

    去重按内容哈希；同一规则版本直接复用。`PIPELINE_VERSION` 变化后，
    同一个文件会自动重解析，避免用户一直拿到旧规则产物；`force=True`
    用于用户手动刷新。
    """
    _validate_payload(data)
    digest = hashlib.sha256(data).hexdigest()
    doc_id = f"upload-{digest[:12]}"
    root = upload_root()
    target = root / doc_id
    document_path = target / "document.json"
    had_existing = document_path.exists()

    status = "解析完成"
    if had_existing:
        try:
            existing = json.loads(document_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if not force and existing is not None:
            if str(existing.get("pipeline_version") or "") == PIPELINE_VERSION:
                return _response(existing, filename, "已存在（同一文件）")
            status = "已重新解析（规则升级）"
        elif force:
            status = "已重新解析（手动刷新）"

    root.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    pdf_path = target / "source.pdf"
    pdf_path.write_bytes(data)
    _inspect_pdf(pdf_path)

    metadata = {
        "doc_id": doc_id,
        "title": Path(filename).stem or doc_id,
        "company": company.strip(),
        "code": "",
        "doc_type": (doc_type or "annual_report").strip(),
        "period": period.strip(),
        "unit": "元",
        "sha256": digest,
    }
    try:
        document = ingest_pdf(pdf_path, metadata)
    except Exception as exc:  # 解析器异常转成用户可读错误
        # 重解析失败时保留旧产物，别把上一次能用的结果删掉。
        if not had_existing:
            shutil.rmtree(target, ignore_errors=True)
        raise UploadError(f"解析失败: {exc}") from exc

    qc = summarize(document)
    if qc["raw_chars"] < MIN_TEXT_CHARS:
        if not had_existing:
            shutil.rmtree(target, ignore_errors=True)
        raise UploadError("未检测到足够的文字层（可能是扫描件），当前不支持 OCR")
    if qc["tables"] == 0 and metadata["doc_type"] in TABLE_REQUIRED_TYPES:
        if not had_existing:
            shutil.rmtree(target, ignore_errors=True)
        raise UploadError("未识别到表格；年报/募集说明书的查数会退化为读正文")

    document["origin"] = "upload"
    document["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    write_document(document, root)

    warnings: list[str] = []
    if qc["tables"] == 0:
        warnings.append("未识别到表格，查数会退化为读正文")
    if qc["tables_with_placeholder_columns"]:
        warnings.append(f"{qc['tables_with_placeholder_columns']} 张表存在占位列名")
    if qc["tables_without_caption"]:
        warnings.append(f"{qc['tables_without_caption']} 张表没有标题")
    return _response(document, filename, status, warnings)
