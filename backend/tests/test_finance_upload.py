"""上传链路测试：PDF 校验、解析落盘、QC 闸门、多根目录合并。

全部离线：PDF 在 tmp_path 现场生成，不依赖真实年报。
"""

from __future__ import annotations

import json

import pytest

fitz = pytest.importorskip("fitz")

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import finance as finance_api
from app.finance.store import FinanceStore
from app.services import finance_upload
from app.services.finance_upload import UploadError, save_and_parse

PAGE = fitz.paper_rect("a4")


def _text(page, x, y, value, size=10.5):
    page.insert_text((x, y), value, fontname="china-s", fontsize=size)


def _grid(page, x0, y0, cols, rows, widths=None, height=22):
    widths = widths or [140] + [(520 - 140) // max(len(cols) - 1, 1)] * (len(cols) - 1)
    x = x0
    for width in widths:
        page.draw_line((x, y0), (x, y0 + height * rows), color=0, width=0.6)
        x += width
    page.draw_line((x, y0), (x, y0 + height * rows), color=0, width=0.6)
    for index in range(rows + 1):
        page.draw_line(
            (x0, y0 + height * index),
            (x0 + sum(widths), y0 + height * index),
            color=0,
            width=0.6,
        )


def build_demo_pdf(path, *, with_table: bool = True) -> None:
    doc = fitz.open()
    page = doc.new_page(width=PAGE.width, height=PAGE.height)
    _text(page, 60, 80, "第二节 公司简介和主要财务指标", size=14)
    _text(page, 60, 110, "（一）主要会计数据", size=10.5)
    _text(page, 60, 132, "单位：万元", size=9)
    if with_table:
        _grid(page, 60, 145, ["项目", "2025 年", "2024 年"], 3, widths=[146, 146, 146])
        for row, values in enumerate(
            (
                ["项目", "2025 年", "2024 年"],
                ["营业收入", "1,234", "1,000"],
                ["净利润", "120", "100"],
            )
        ):
            for col, value in enumerate(values):
                _text(page, 64 + col * 146, 145 + 15 + row * 22, value, size=9)
    doc.save(path)
    doc.close()


@pytest.fixture
def upload_root(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    monkeypatch.setattr(finance_upload, "UPLOAD_ROOT", root)
    return root


def test_upload_parses_and_writes_document(tmp_path, upload_root, monkeypatch):
    monkeypatch.setattr(finance_upload, "MIN_TEXT_CHARS", 10)
    pdf = tmp_path / "demo.pdf"
    build_demo_pdf(pdf)
    result = save_and_parse(
        "demo.pdf", pdf.read_bytes(), company="XX股份", period="2025"
    )

    assert result["status"] == "解析完成"
    assert result["doc_id"].startswith("upload-")
    assert result["tables"] == 1
    assert result["sections"] >= 1
    assert result["pipeline_version"] == finance_upload.PIPELINE_VERSION

    document_path = upload_root / result["doc_id"] / "document.json"
    assert document_path.exists()
    document = json.loads(document_path.read_text(encoding="utf-8"))
    assert document["origin"] == "upload"
    assert document["company"] == "XX股份"
    assert document["tables"][0]["table_id"].startswith(result["doc_id"] + "#")


def test_upload_is_idempotent_by_content(tmp_path, upload_root, monkeypatch):
    monkeypatch.setattr(finance_upload, "MIN_TEXT_CHARS", 10)
    pdf = tmp_path / "demo.pdf"
    build_demo_pdf(pdf)
    first = save_and_parse("demo.pdf", pdf.read_bytes())
    second = save_and_parse("demo.pdf", pdf.read_bytes())
    assert second["doc_id"] == first["doc_id"]
    assert second["status"].startswith("已存在")


def test_upload_reparses_when_pipeline_version_changes(tmp_path, upload_root, monkeypatch):
    """解析规则升级后，同一个文件不能继续返回旧产物。"""
    monkeypatch.setattr(finance_upload, "MIN_TEXT_CHARS", 10)
    pdf = tmp_path / "demo.pdf"
    build_demo_pdf(pdf)
    first = save_and_parse("demo.pdf", pdf.read_bytes())
    path = upload_root / first["doc_id"] / "document.json"
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["pipeline_version"] = "legacy-0"
    stale["tables"][0]["columns"] = ["BROKEN"]
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    second = save_and_parse("demo.pdf", pdf.read_bytes())
    assert "已重新解析" in second["status"]
    assert "规则升级" in second["status"]
    refreshed = json.loads(path.read_text(encoding="utf-8"))
    assert refreshed["pipeline_version"] == finance_upload.PIPELINE_VERSION
    assert refreshed["tables"][0]["columns"] != ["BROKEN"]


def test_upload_force_reparses_even_when_version_matches(tmp_path, upload_root, monkeypatch):
    monkeypatch.setattr(finance_upload, "MIN_TEXT_CHARS", 10)
    pdf = tmp_path / "demo.pdf"
    build_demo_pdf(pdf)
    first = save_and_parse("demo.pdf", pdf.read_bytes())
    path = upload_root / first["doc_id"] / "document.json"
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["tables"][0]["columns"] = ["BROKEN"]
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    second = save_and_parse("demo.pdf", pdf.read_bytes(), force=True)
    assert "手动刷新" in second["status"]
    refreshed = json.loads(path.read_text(encoding="utf-8"))
    assert refreshed["tables"][0]["columns"] != ["BROKEN"]


def test_upload_rejects_non_pdf(upload_root):
    with pytest.raises(UploadError) as exc:
        save_and_parse("x.pdf", b"not a pdf")
    assert exc.value.status_code == 400


def test_upload_rejects_scanned_pdf(tmp_path, upload_root):
    pdf = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page(width=PAGE.width, height=PAGE.height)
    doc.save(pdf)
    doc.close()
    with pytest.raises(UploadError) as exc:
        save_and_parse("blank.pdf", pdf.read_bytes())
    assert "文字层" in exc.value.detail


def test_upload_rejects_tableless_annual_report(tmp_path, upload_root, monkeypatch):
    monkeypatch.setattr(finance_upload, "MIN_TEXT_CHARS", 10)
    pdf = tmp_path / "text_only.pdf"
    build_demo_pdf(pdf, with_table=False)
    with pytest.raises(UploadError) as exc:
        save_and_parse("text_only.pdf", pdf.read_bytes(), doc_type="annual_report")
    assert "未识别到表格" in exc.value.detail


def test_store_merges_official_and_upload_roots(tmp_path):
    def write_doc(root, doc_id, origin):
        target = root / doc_id
        target.mkdir(parents=True)
        (target / "document.json").write_text(
            json.dumps(
                {
                    "doc_id": doc_id,
                    "title": doc_id,
                    "origin": origin,
                    "sections": [],
                    "tables": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    official = tmp_path / "official"
    uploads = tmp_path / "uploads"
    write_doc(official, "official-1", "corpus")
    write_doc(uploads, "upload-1", "upload")

    store = FinanceStore(official, extra_roots=[uploads])
    rows = {row["doc_id"]: row for row in store.list_documents()}
    assert set(rows) == {"official-1", "upload-1"}
    assert rows["official-1"]["origin"] == "corpus"
    assert rows["upload-1"]["origin"] == "upload"


async def test_upload_endpoint_returns_201(tmp_path, upload_root, monkeypatch):
    monkeypatch.setattr(finance_upload, "MIN_TEXT_CHARS", 10)
    monkeypatch.setattr(finance_api, "reset_cache", lambda: None)

    app = FastAPI()
    app.include_router(finance_api.router)
    pdf = tmp_path / "demo.pdf"
    build_demo_pdf(pdf)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/finance/documents",
            files={"file": ("demo.pdf", pdf.read_bytes(), "application/pdf")},
            data={"company": "XX股份", "period": "2025"},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["tables"] == 1
    assert body["doc_id"].startswith("upload-")
