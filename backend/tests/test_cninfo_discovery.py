"""巨潮公告发现：标题判据、时区、manifest 合并。全部离线。"""

import pytest

from app.finance.cninfo import (
    announcement_query_payload,
    build_manifest_document,
    column_for_code,
    doc_id_for,
    normalize_announcement,
    parse_announcement_body,
    pick_annual_report,
    upsert_manifest_document,
)

_ITEMS = [
    {
        "announcement_id": "1",
        "code": "000333",
        "company": "美的集团",
        "title": "2025年年度报告摘要",
        "date": "2026-03-31",
        "pdf_url": "http://static.cninfo.com.cn/finalpage/a.PDF",
        "declared_size_kb": 200,
    },
    {
        "announcement_id": "2",
        "code": "000333",
        "company": "美的集团",
        "title": "2025年年度报告",
        "date": "2026-03-31",
        "pdf_url": "http://static.cninfo.com.cn/finalpage/b.PDF",
        "declared_size_kb": 5804,
    },
    {
        "announcement_id": "3",
        "code": "000333",
        "company": "美的集团",
        "title": "2024年年度报告",
        "date": "2025-03-29",
        "pdf_url": "http://static.cninfo.com.cn/finalpage/c.PDF",
        "declared_size_kb": 6581,
    },
]


def test_column_follows_exchange():
    assert column_for_code("600519") == "sse"
    assert column_for_code("000333") == "szse"
    assert column_for_code("430047") == "bse"
    assert column_for_code("830799") == "bse"


def test_payload_pairs_code_with_org_id():
    payload = announcement_query_payload("000333", "9900005965", year=2025)
    # 只给代码会静默返回 0 条，所以 stock 必须是「代码,orgId」
    assert payload["stock"] == "000333,9900005965"
    assert payload["category"] == "category_ndbg_szsh"
    assert payload["seDate"].startswith("2024-06-01")


def test_epoch_ms_is_parsed_in_beijing_time():
    item = normalize_announcement(
        {
            "secCode": "000333",
            "secName": "美的集团",
            "announcementTitle": "2025年年度报告",
            "announcementTime": 1774886400000,
            "adjunctUrl": "finalpage/2026-03-31/1225065145.PDF",
        }
    )
    # 时间戳是北京时间零点（UTC 前一天 16:00），按 UTC 会错一天
    assert item["date"] == "2026-03-31"
    assert item["pdf_url"] == (
        "http://static.cninfo.com.cn/finalpage/2026-03-31/1225065145.PDF"
    )


def test_parse_body_tolerates_null_response():
    assert parse_announcement_body(None) == []
    assert parse_announcement_body({"announcements": None}) == []


def test_pick_excludes_summary_and_other_years():
    choice = pick_annual_report(_ITEMS, 2025)
    assert choice.ok is True
    assert choice.chosen["announcement_id"] == "2"
    reasons = {item["announcement_id"] for item in choice.rejected}
    assert reasons == {"1", "3"}


def test_pick_accepts_company_prefixed_sse_title():
    items = [
        {
            "announcement_id": "9",
            "code": "600519",
            "company": "贵州茅台",
            "title": "贵州茅台2025年年度报告",
            "date": "2026-04-17",
            "pdf_url": "http://static.cninfo.com.cn/finalpage/d.PDF",
        },
        {
            "announcement_id": "10",
            "code": "600519",
            "company": "贵州茅台",
            "title": "贵州茅台2025年年度报告（英文版）",
            "date": "2026-04-17",
            "pdf_url": "http://static.cninfo.com.cn/finalpage/e.PDF",
        },
    ]
    choice = pick_annual_report(items, 2025)
    assert choice.chosen["announcement_id"] == "9"


def test_pick_prefers_latest_disclosure_when_duplicated():
    items = [
        {**_ITEMS[1], "announcement_id": "old", "date": "2026-03-31"},
        {**_ITEMS[1], "announcement_id": "new", "date": "2026-04-20"},
    ]
    assert pick_annual_report(items, 2025).chosen["announcement_id"] == "new"


def test_missing_year_reports_reason_instead_of_guessing():
    choice = pick_annual_report(_ITEMS, 2023)
    assert choice.ok is False
    assert "2023年年度报告" in choice.reason


def test_build_document_matches_manifest_shape():
    document = build_manifest_document(_ITEMS[1], year=2025, unit="千元")
    assert document["doc_id"] == "cninfo-000333-2025-annual"
    assert document["title"] == "美的集团2025年年度报告"
    assert document["declared_size_kb"] == 5804
    assert document["unit"] == "千元"
    assert "需人工确认" in document["unit_note"]
    assert doc_id_for("600519", "2025") == "cninfo-600519-2025-annual"


def test_build_document_rejects_incomplete_candidate():
    with pytest.raises(ValueError):
        build_manifest_document({"code": "000333"}, year=2025)


def test_upsert_replaces_and_keeps_download_fields():
    manifest = {
        "documents": [
            {
                "doc_id": "cninfo-000333-2025-annual",
                "sha256": "abc",
                "page_count": 276,
            }
        ]
    }
    action = upsert_manifest_document(
        manifest, build_manifest_document(_ITEMS[1], year=2025)
    )
    assert action == "replaced"
    assert manifest["documents"][0]["sha256"] == "abc"
    assert manifest["documents"][0]["page_count"] == 276
    assert manifest["documents"][0]["url"].endswith("b.PDF")


def test_upsert_appends_new_document():
    manifest = {"documents": []}
    action = upsert_manifest_document(
        manifest, build_manifest_document(_ITEMS[1], year=2025)
    )
    assert action == "added"
    assert len(manifest["documents"]) == 1
