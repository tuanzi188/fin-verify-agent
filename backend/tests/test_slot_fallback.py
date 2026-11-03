"""槽位兜底：闭集校验、查询补全与兜底结论（不调真实模型）。"""

import json

from app.finance import slot_fallback
from app.services import model_provider

DOCUMENTS = [
    {"company": "美的集团", "period": "2025", "code": "000333"},
    {"company": "贵州茅台", "period": "2025", "code": "600519"},
]


def slots_json(**overrides) -> str:
    payload = {
        "companies": [],
        "years": [],
        "metric": "",
        "intent": "lookup",
        "out_of_scope_reason": "none",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_parse_slots_accepts_corpus_entities():
    slots = slot_fallback.parse_slots(
        slots_json(companies=["贵州茅台"], years=["2025"], metric="营业收入"),
        DOCUMENTS,
    )
    assert slots is not None
    assert slots.companies == ("贵州茅台",)
    assert slots.years == ("2025",)


def test_parse_slots_rejects_unknown_company():
    assert slot_fallback.parse_slots(slots_json(companies=["五粮液"]), DOCUMENTS) is None


def test_parse_slots_rejects_bad_year_and_reason():
    assert slot_fallback.parse_slots(slots_json(years=["25年"]), DOCUMENTS) is None
    assert (
        slot_fallback.parse_slots(slots_json(out_of_scope_reason="maybe"), DOCUMENTS)
        is None
    )


def test_parse_slots_strips_code_fence():
    raw = "```json\n" + slots_json(companies=["美的集团"]) + "\n```"
    slots = slot_fallback.parse_slots(raw, DOCUMENTS)
    assert slots is not None
    assert slots.companies == ("美的集团",)


def test_enrich_query_fills_missing_period_only():
    slots = slot_fallback.SlotExtraction(
        companies=("贵州茅台",), years=("2025",), metric="营业收入"
    )
    enriched = slot_fallback.enrich_query(
        "茅台最新一期的营业收入是多少？", slots, DOCUMENTS
    )
    # 公司已在句中、指标已在句中，只补期间
    assert enriched == "2025 年 茅台最新一期的营业收入是多少？"


def test_fallback_decision_boundary_for_opinion():
    slots = slot_fallback.SlotExtraction(out_of_scope_reason="opinion")
    decision = slot_fallback.fallback_decision("茅台股票能买吗", DOCUMENTS, slots)
    assert decision is not None
    assert decision.initial_path == "clarify"
    assert decision.rule_id == "slot_boundary"


def test_fallback_decision_reroutes_enriched_query():
    slots = slot_fallback.SlotExtraction(
        companies=("贵州茅台",), years=("2025",), metric="营业收入"
    )
    decision = slot_fallback.fallback_decision(
        "茅台最新一期的营业收入是多少？", DOCUMENTS, slots
    )
    assert decision is not None
    assert decision.initial_path == "structured_rag"
    assert "2025 年" in decision.resolved_query


async def test_extract_slots_uses_single_call(monkeypatch):
    calls: list[dict] = []

    async def fake_chat(messages, temperature=0.1, max_tokens=None):
        calls.append({"messages": messages, "max_tokens": max_tokens})
        return slots_json(companies=["贵州茅台"], years=["2025"]), None, {
            "total_tokens": 321
        }

    monkeypatch.setattr(model_provider, "chat", fake_chat)
    slots = await slot_fallback.extract_slots("茅台 2025 年营业收入", DOCUMENTS)
    assert slots is not None
    assert slots.usage["total_tokens"] == 321
    assert len(calls) == 1


async def test_extract_slots_channel_failure_returns_none(monkeypatch):
    async def boom(messages, temperature=0.1, max_tokens=None):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(model_provider, "chat", boom)
    assert await slot_fallback.extract_slots("茅台 2025 年营业收入", DOCUMENTS) is None
