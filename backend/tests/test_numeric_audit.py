"""数值审计测试：金标覆盖率、跨单位换算、幻觉事件、未溯源数值。"""

from evaluation.numeric_audit import (
    aggregate,
    audit_result,
    data_numbers,
    pii_hits,
    strip_citation_markers,
)


def make_chunks():
    return {
        "doc#t0001": {
            "chunk_id": "doc#t0001",
            "doc_id": "cninfo-000333-2025-annual",
            "title": "美的集团 2025 年报｜主要会计数据",
            "content": "表 doc#t0001｜营业收入\n单位：千元\n[行0] 营业收入 | 456,451,731",
        }
    }


def make_task():
    return {
        "id": "T",
        "question": "美的集团 2025 年营业收入是多少千元？",
        "answer_points": [
            {
                "text": "营业收入为 456,451,731 千元",
                "evidence": {
                    "table_id": "doc#t0001",
                    "row": "营业收入",
                    "column": 1,
                    "value": "456,451,731",
                },
                "unit": "千元",
            }
        ],
    }


def make_result(answer, status="answered"):
    return {
        "task_id": "T",
        "run_index": 1,
        "status": status,
        "answer": answer,
        "citations": [{"marker": 1, "chunk_id": "doc#t0001"}],
        "trace": [
            {
                "step": 1,
                "tool": "search_blocks",
                "args": {"query": "q"},
                "ok": True,
                "result_ids": ["doc#t0001#rows#0#cols#0"],
                "result_count": 1,
            }
        ],
        "evidence": [],
    }


def test_strip_citation_markers_removes_full_width_metadata():
    text = "营业收入 456,451,731 千元【2，PDF 第 49 页，表 cninfo】"
    assert "PDF" not in strip_citation_markers(text)
    assert "456,451,731" in strip_citation_markers(text)


def test_data_numbers_excludes_years_and_page_numbers():
    values = data_numbers("见 PDF 第 9 页，2025 年营业收入 456,451,731 千元")
    assert [item["value"] for item in values] == [456451731]


def test_audit_accepts_same_unit_and_converted_unit():
    task = make_task()
    same = audit_result(task, make_result("营业收入为 456,451,731 千元"), make_chunks())
    assert same["gold_coverage"] == 1.0
    assert same["unsourced_numbers"] == []

    converted = audit_result(task, make_result("营业收入约 4,564.52 亿元"), make_chunks())
    assert converted["gold_coverage"] == 1.0
    assert converted["unsourced_numbers"] == []


def test_audit_flags_hallucination_only_when_judge_says_pass():
    task = make_task()
    judgment = {
        "points": [{"index": 0, "pass": True, "cited": True, "citation_supports": True}]
    }
    row = audit_result(
        task,
        make_result("营业收入为 500,000,000 千元"),
        make_chunks(),
        judgment=judgment,
    )
    assert row["gold_points_covered"] == 0
    assert len(row["hallucination_events"]) == 1
    assert row["hallucination_rate"] == 1.0

    # judge 判 pass=false 时是"没答到"，不是幻觉
    missed = audit_result(
        task,
        make_result("营业收入为 500,000,000 千元"),
        make_chunks(),
        judgment={"points": [{"index": 0, "pass": False}]},
    )
    assert missed["hallucination_events"] == []


def test_audit_lists_unsourced_numbers_as_advisory():
    task = make_task()
    row = audit_result(
        task,
        make_result("营业收入为 456,451,731 千元，另据传 123,456 千元"),
        make_chunks(),
    )
    assert [item["value"] for item in row["unsourced_numbers"]] == ["123456"]
    assert row["unsourced_rate"] == 0.5


def test_aggregate_sums_events_and_points():
    task = make_task()
    rows = [
        audit_result(task, make_result("营业收入为 456,451,731 千元"), make_chunks()),
        audit_result(task, make_result("营业收入为 500,000,000 千元"), make_chunks()),
    ]
    summary = aggregate(rows)
    assert summary["tasks"] == 2
    assert summary["gold_points_checked"] == 2
    assert summary["gold_points_covered"] == 1
    assert summary["gold_coverage"] == 0.5


def test_pii_detection_is_context_aware():
    hits = pii_hits("联系人：张三 电话 13812345678，邮箱 ir@example.com")
    types = {hit["type"] for hit in hits}
    assert "phone" in types
    assert "email" in types

    # 财务金额没有银行卡上下文，不能被判成 PII
    assert pii_hits("营业收入 456451731000 元") == []


def test_audit_result_includes_pii_hits():
    task = make_task()
    row = audit_result(
        task,
        make_result("营业收入为 456,451,731 千元，联系方式 ir@example.com"),
        make_chunks(),
    )
    assert len(row["pii_hits"]) == 1
    assert row["pii_hits"][0]["type"] == "email"
