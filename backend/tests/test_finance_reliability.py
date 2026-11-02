"""金融产品查证闭环回归：引用、计算、追问、主体边界与脚注。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.finance.calculation_records import build_calculation_records
from app.finance.query_scope import prepare_finance_scope, scope_companies, scope_years
from app.finance.references import reference_markers, remap_references
from app.finance.store import FinanceStore
from app.finance.table_annotations import annotate_finance_tables
from app.finance.tree import Line
from app.models.finance import (
    CalcRecord,
    Claim,
    EvidenceRef,
    FinanceChatResponse,
    FinanceCitation,
)
from app.services import finance_entry, finance_rag
from app.services.finance_validation import validate_finance_response
from tests.test_finance_api import write_demo_document


def reliability_inventory() -> list[dict]:
    return [
        {
            "company": "美的集团",
            "period": "2025",
            "doc_type": "annual_report",
            "code": "000333",
        },
        {
            "company": "美的集团",
            "period": "2024",
            "doc_type": "annual_report",
            "code": "000333",
        },
        {
            "company": "比亚迪",
            "period": "2025",
            "doc_type": "annual_report",
            "code": "002594",
        },
    ]


def reliability_citation(
    marker: int = 1, value: str = "12.34", unit: str = "千元"
) -> FinanceCitation:
    return FinanceCitation(
        marker=marker,
        evidence=EvidenceRef(
            evidence_id=f"表甲#r0c{marker}",
            doc_id="文档甲",
            kind="table_cell",
            table_id="表甲",
            cell_text=value,
            row_indices=[0],
            column_indices=[marker],
            unit=unit,
        ),
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("结论[1,2]", [1, 2]),
        ("结论【1、2，PDF 第 49 页】", [1, 2]),
        ("结论[1][2][1]", [1, 2]),
        ("计算[计算1]并引用[12]", [12]),
    ],
)
def test_reliability_reference_groups(text, expected):
    assert reference_markers(text) == expected


def test_reliability_remapping_preserves_claim_bindings():
    rewritten = remap_references("收入100千元[2]，利润10千元[1,2]。", {1: 1, 2: 1})
    claims = finance_entry.build_claims(rewritten, [reliability_citation()], [])
    assert reference_markers(rewritten) == [1]
    assert claims[0].citation_markers == [1]


@pytest.mark.parametrize(
    "formula, inputs, result, unit, scale",
    [
        ("a-b", {"a": "30%", "b": "20%"}, "10", "pp", "100"),
        ("(a-b)*100", {"a": "30%", "b": "20%"}, "10", "pp", "1"),
        ("a-b", {"a": "20%", "b": "30%"}, "-10", "pp", "100"),
        ("(a-b)/b*100", {"a": "12.34", "b": "10"}, "23.4", "%", "1"),
        ("a*100", {"a": "12"}, "1200", "", "1"),
    ],
)
def test_reliability_calculation_scale(formula, inputs, result, unit, scale):
    record = build_calculation_records([{"formula": formula, "inputs": inputs}], [])[0]
    assert float(record.result) == float(result)
    assert record.unit == unit
    assert record.result_scale == scale
    assert record.recomputed


def test_reliability_no_substring_or_ambiguous_calculation_binding():
    record = CalcRecord(
        formula="a+b", inputs={"a": 5, "b": 7}, result="12", unit="亿元"
    )
    assert (
        finance_entry.build_claims(
            "计算得到112亿元[1]", [reliability_citation()], [record]
        )[0].calc_index
        is None
    )
    assert (
        finance_entry.build_claims(
            "计算得到12亿元[1]", [reliability_citation()], [record, record]
        )[0].calc_index
        is None
    )
    assert (
        finance_entry.build_claims(
            "计算得到12亿元[1][计算2]", [reliability_citation()], [record, record]
        )[0].calc_index
        == 1
    )


def test_reliability_inputs_require_unique_coordinates():
    record = build_calculation_records(
        [{"formula": "a-b", "inputs": {"a": "12.34", "b": "10"}}],
        [reliability_citation(), reliability_citation(2, "10")],
    )[0]
    assert record.grounding_status == "complete"
    assert record.input_sources["b"]["column"] == 2
    assert record.unit == "千元"
    ambiguous = build_calculation_records(
        [{"formula": "a-b", "inputs": {"a": "12.34", "b": "10"}}],
        [
            reliability_citation(),
            reliability_citation(2, "10"),
            reliability_citation(3, "10"),
        ],
    )[0]
    assert ambiguous.grounding_status == "partial"
    assert "b" not in ambiguous.input_sources


def test_reliability_no_automatic_citations_for_unmarked_answer():
    assert (
        finance_rag._used_citations("营业收入999亿元。", [reliability_citation()]) == []
    )
    assert finance_rag._decide_status("营业收入999亿元。", []) == "needs_review"
    assert finance_rag._decide_status("根据现有资料无法确定。", []) == "abstained"


@pytest.mark.parametrize(
    "text, value, unit, status",
    [
        ("营业收入12.34千元", "12.34", "千元", "answered"),
        ("营业收入12340元", "12.34", "千元", "answered"),
        ("营业收入12.34亿元", "12.34", "千元", "needs_review"),
        ("营业收入999千元", "12.34", "千元", "needs_review"),
        ("同比下降18.97%", "-18.97%", "元", "answered"),
    ],
)
def test_reliability_claim_numeric_validation(text, value, unit, status):
    response = FinanceChatResponse(
        status="answered",
        answer=text + "[1]",
        claims=[Claim(text=text, citation_markers=[1])],
        citations=[reliability_citation(value=value, unit=unit)],
    )
    validate_finance_response(response)
    assert response.status == status
    assert response.verification["checked_numbers"] == 1


def test_reliability_cell_preview_does_not_validate_neighbor_value(tmp_path):
    store = FinanceStore(write_demo_document(tmp_path))
    evidence = finance_entry.build_evidence(store, "demo#t1#0#1")
    response = FinanceChatResponse(
        status="answered",
        answer="营业收入1.20千元[1]",
        claims=[Claim(text="营业收入1.20千元", citation_markers=[1])],
        citations=[FinanceCitation(marker=1, evidence=evidence)],
    )
    validate_finance_response(response)
    assert response.status == "needs_review"


def test_reliability_evidence_keeps_original_source(tmp_path):
    root_source = write_demo_document(tmp_path)
    document_source_path = root_source / "demo" / "document.json"
    import json

    source_document = json.loads(document_source_path.read_text(encoding="utf-8"))
    source_document["source_url"] = "https://example.com/annual.pdf"
    document_source_path.write_text(
        json.dumps(source_document, ensure_ascii=False), encoding="utf-8"
    )
    source_evidence = finance_entry.build_evidence(
        FinanceStore(root_source), "demo#t1#0#1"
    )
    assert source_evidence.source_url == "https://example.com/annual.pdf"


@pytest.mark.parametrize(
    "question, blocked",
    [
        ("宁德时代2025年收入是多少", True),
        ("宁德时代营业收入是多少", True),
        ("美的与宁德时代2025年收入对比", True),
        ("300750收入是多少", True),
        ("美的2025Q1收入是多少", True),
        ("美的2026年收入是多少", True),
        ("美的2025年收入是多少", False),
        ("000333 2025年收入是多少", False),
    ],
)
def test_reliability_subject_and_period_boundary(question, blocked):
    assert (
        bool(prepare_finance_scope(question, reliability_inventory()).blocked_reason)
        is blocked
    )


def test_reliability_dynamic_company_and_quarter_year():
    documents = [
        {
            "company": "新示例股份有限公司",
            "code": "688888",
            "period": "2025Q1",
            "title": "2025年第一季度报告",
        }
    ]
    assert scope_companies("688888收入", documents) == ["新示例股份有限公司"]
    assert scope_companies("新示例2025Q1收入", documents) == ["新示例股份有限公司"]
    assert scope_years("2025Q1") == {"2025"}
    assert not prepare_finance_scope("新示例2025Q1收入", documents).blocked_reason


def test_reliability_followup_inherits_company_metric_and_relative_year():
    scope = prepare_finance_scope(
        "那上一年呢？", reliability_inventory(), "美的集团2025年营业收入是多少？"
    )
    assert scope.companies == ("美的集团",)
    assert scope.years == ("2024",)
    assert "营业收入" in scope.resolved_query
    next_scope = prepare_finance_scope(
        "那上一年呢？", reliability_inventory(), scope.resolved_query
    )
    assert next_scope.years == ("2023",)
    clarified = prepare_finance_scope(
        "2024年", reliability_inventory(), "美的集团营业收入是多少？"
    )
    assert clarified.companies == ("美的集团",)
    assert "营业收入" in clarified.resolved_query


def test_reliability_ambiguous_followup_requests_clarification():
    scope = prepare_finance_scope(
        "那上一年呢？", reliability_inventory(), "美的与比亚迪2025年收入对比"
    )
    assert scope.clarify
    assert scope.blocked_reason


def test_reliability_footnote_geometry_and_section_boundary():
    table = {"page": 1, "bbox": [50, 50, 300, 100], "footnote": ""}
    lines = [
        Line(1, 105, 115, 50, 290, "注：按合并口径披露。", 9, False),
        Line(1, 119, 129, 50, 290, "金额单位为千元。", 9, False),
        Line(1, 135, 145, 50, 290, "二、其他事项", 10, True),
        Line(2, 105, 115, 50, 290, "注：另一页的说明。", 9, False),
    ]
    result = annotate_finance_tables([table], lines)[0]
    assert result["footnote"] == "注：按合并口径披露。\n金额单位为千元。"
    assert result["footnote_page"] == 1
    assert table["footnote"] == ""


@pytest.mark.asyncio
async def test_reliability_agent_deduplication_and_review_end_to_end(
    tmp_path, monkeypatch
):
    store = FinanceStore(write_demo_document(tmp_path))

    async def reliability_agent_stub(*args, **kwargs):
        return {
            "status": "answered",
            "answer": "营业收入12.34千元[2]。",
            "citations": [
                {"marker": 1, "chunk_id": "demo#t1#0#1"},
                {"marker": 2, "chunk_id": "demo#t1#0#1"},
            ],
            "evidence": [],
            "usage": {},
        }

    monkeypatch.setattr(finance_entry, "get_finance_store", lambda: store)
    monkeypatch.setattr(finance_entry, "run_agent", reliability_agent_stub)
    monkeypatch.setattr(finance_entry, "conversation_memory", AsyncMock())
    response = await finance_entry.run_finance_query(
        query="收入", conversation_id="test", user_context=None, search_mode="sections"
    )
    assert len(response.citations) == 1
    assert response.claims[0].citation_markers == [1]
    assert "[2]" not in response.answer
    assert response.status == "answered"


@pytest.mark.asyncio
async def test_reliability_rag_numeric_error_is_persisted_for_review(
    tmp_path, monkeypatch
):
    store = FinanceStore(write_demo_document(tmp_path))

    class ReliabilityIndexStub:
        def search(self, *args, **kwargs):
            return [{"block_id": "demo#t1#0#1"}]

    monkeypatch.setattr(finance_rag, "get_finance_store", lambda: store)
    monkeypatch.setattr(finance_rag, "get_block_index", lambda: ReliabilityIndexStub())
    monkeypatch.setattr(
        finance_rag.model_provider,
        "chat",
        AsyncMock(return_value=("营业收入999千元[1]。", None, {})),
    )
    memory = AsyncMock()
    monkeypatch.setattr(finance_rag, "conversation_memory", memory)
    response = await finance_rag.run_structured_rag(
        query="营业收入", conversation_id="test"
    )
    assert response.status == "needs_review"
    assert memory.add_turn.await_args.kwargs["payload"]["verification"]["issues"]
