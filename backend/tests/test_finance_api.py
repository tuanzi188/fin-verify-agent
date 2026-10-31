"""金融查证 HTTP 契约测试。

全部离线：不调模型，用临时 document.json 验证坐标、计算和引用链
不会退化成“只给一段文本”。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import finance as finance_api
from app.api import lab as lab_api
from app.core import auth
from app.core.conversation_scope import ConversationNamespace
from app.finance.store import FinanceStore
from app.models.finance import FinanceChatResponse
from app.services import finance_entry, finance_router

_ALICE = {"Authorization": "Bearer " + "a" * 32}


def write_demo_document(root: Path) -> Path:
    document = {
        "doc_id": "demo",
        "title": "XX公司2025年年度报告",
        "company": "XX股份",
        "code": "000001",
        "doc_type": "annual_report",
        "period": "2025",
        "unit": "千元",
        "currency": "CNY",
        "sections": [
            {
                "section_id": "demo#s1",
                "path": ["第二节", "主要会计数据"],
                "page_start": 42,
                "page_end": 43,
                "text": "2025 年营业收入 12.34 千元，2024 年为 10.00 千元。",
                "table_ids": ["demo#t1"],
            }
        ],
        "tables": [
            {
                "table_id": "demo#t1",
                "section_id": "demo#s1",
                "page": 42,
                "caption": "主要会计数据",
                "unit": "千元",
                "footnote": "注：数据经审计。",
                "columns": ["项目", "2025", "2024"],
                "rows": [
                    ["营业收入", "12.34", "10.00"],
                    ["归母净利润", "1.20", "1.00"],
                ],
            }
        ],
    }
    doc_dir = root / "demo"
    doc_dir.mkdir(parents=True)
    (doc_dir / "document.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return root


@pytest.fixture
def demo_store(tmp_path) -> FinanceStore:
    return FinanceStore(write_demo_document(tmp_path))


@pytest.fixture
def api_app(monkeypatch):
    monkeypatch.setattr(
        auth,
        "API_IDENTITIES",
        [{"token": "a" * 32, "user_id": "alice", "roles": ["user"], "department": "研发"}],
    )
    monkeypatch.setattr(auth, "API_KEY", "legacy-shared-key")
    monkeypatch.setattr(auth.audit_logger, "log", AsyncMock())

    application = FastAPI()
    application.include_router(finance_api.router, dependencies=[Depends(auth.verify_api_key)])
    application.include_router(lab_api.router, dependencies=[Depends(auth.verify_api_key)])
    return application


def test_build_evidence_keeps_table_coordinates(demo_store):
    anchor = "demo#t1#rows#0,1#cols#0,1"
    evidence = finance_entry.build_evidence(demo_store, anchor)

    assert evidence.kind.value == "table_rows"
    assert evidence.doc_id == "demo"
    assert evidence.company == "XX股份"
    assert evidence.period == "2025"
    assert evidence.table_id == "demo#t1"
    assert evidence.page == 42
    assert evidence.unit == "千元"
    assert evidence.row_indices == [0, 1]
    assert evidence.column_indices == [0, 1]
    assert evidence.verified is True
    assert evidence.table_preview is not None
    assert evidence.table_preview.total_rows == 2
    assert evidence.table_preview.rows[0].row_name == "营业收入"


def test_build_evidence_keeps_cell_coordinates_and_context(demo_store):
    evidence = finance_entry.build_evidence(demo_store, "demo#t1#0#1")

    assert evidence.kind.value == "table_cell"
    assert evidence.row_indices == [0]
    assert evidence.column_indices == [1]
    assert evidence.row_name == "营业收入"
    assert evidence.column == "2025"
    assert evidence.cell_text == "12.34"
    # 上下文窗口可以更宽，但目标坐标不能被窗口替换
    assert len(evidence.table_preview.rows) >= 1


def test_table_level_evidence_does_not_highlight_all_rows(demo_store):
    evidence = finance_entry.build_evidence(demo_store, "demo#t1")

    assert evidence.kind.value == "table_rows"
    assert evidence.row_indices == []
    assert evidence.column_indices == []
    assert evidence.table_preview is not None
    assert len(evidence.table_preview.rows) == 2


def test_display_columns_repairs_parser_placeholders():
    table = {
        "columns": ["项目", "col2", "col3"],
        "rows": [["", "营业收入", "毛利率"], ["收入", "12.34", "29.9%"]],
    }
    assert finance_entry._display_columns(table) == ["项目", "营业收入", "毛利率"]

    unit_table = {
        "columns": ["单位：千元", "智能家居业务", "楼宇科技"],
        "rows": [["收入", "299,927,239", "35,790,825"], ["同比变化", "11.28%", "25.72%"]],
    }
    assert finance_entry._display_columns(unit_table)[0] == "项目"


def test_build_evidence_paragraph_has_page_and_path(demo_store):
    evidence = finance_entry.build_evidence(demo_store, "demo#s1")

    assert evidence.kind.value == "paragraph"
    assert evidence.page == 42
    assert evidence.path == ["第二节", "主要会计数据"]
    assert "营业收入" in evidence.content
    assert evidence.verified is False


@pytest.mark.asyncio
async def test_run_finance_query_maps_claims_citations_and_calc(demo_store, monkeypatch):
    async def fake_run_agent(task, **kwargs):
        assert kwargs["toolset"].search_mode == "sections"
        return {
            "status": "answered",
            "answer": "营业收入为 12.34 千元，同比增长 23.4% [1]。",
            "citations": [{"marker": 1, "chunk_id": "demo#t1#rows#0,1#cols#0,1"}],
            "evidence": [
                {
                    "chunk_id": "demo#t1#rows#0,1#cols#0,1",
                    "kind": "rows",
                    "table_id": "demo#t1",
                    "row_indices": [0, 1],
                    "column_indices": [0, 1],
                    "content": "表 主要会计数据",
                }
            ],
            "trace": [
                {
                    "step": 1,
                    "tool": "get_rows",
                    "ok": True,
                    "result_ids": ["demo#t1#rows#0,1#cols#0,1"],
                    "result_count": 1,
                    "new_result_count": 1,
                    "latency_ms": 5,
                    "result_chars": 100,
                    "payload_chars": 180,
                    "evidence_chars_added": 80,
                    "duplicate_skipped": 0,
                },
                {"step": 2, "tool": "__final__", "ok": True, "result_count": 0},
            ],
            "calc": [
                {
                    "formula": "(a - b) / b * 100",
                    "inputs": {"a": "12.34", "b": "10.00"},
                    "result": "23.4",
                }
            ],
            "usage": {
                "tokens_in": 100,
                "tokens_out": 20,
                "latency_ms": 50,
                "llm_calls": 2,
                "final_context_tokens": 90,
            },
            "variant": "improved",
        }

    memory = AsyncMock()
    monkeypatch.setattr(finance_entry, "get_finance_store", lambda: demo_store)
    monkeypatch.setattr(finance_entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(finance_entry, "conversation_memory", memory)

    response = await finance_entry.run_finance_query(
        query="2025 年营业收入是多少？",
        conversation_id="conv-1",
        user_context=object(),
        search_mode="sections",
    )

    assert response.status == "answered"
    assert response.citations[0].marker == 1
    assert response.citations[0].evidence.table_id == "demo#t1"
    assert response.citations[0].evidence.row_indices == [0, 1]
    assert response.calc[0].unit == "%"
    assert response.claims[0].citation_markers == [1]
    assert response.claims[0].calc_index == 0
    assert response.trace[0].new_result_count == 1
    assert response.trace[0].table_id == "demo#t1"
    assert response.trace[0].row_indices == [0, 1]
    assert response.trace[0].column_indices == [0, 1]
    assert response.token_usage["total_tokens"] == 120
    memory.add_turn.assert_awaited_once()
    # 结构化载荷必须随会话落库：重开旧会话时结论卡/引用坐标/计算才能还原。
    persisted = memory.add_turn.await_args.kwargs["payload"]
    assert persisted["kind"] == "finance"
    assert persisted["corpus_fingerprint"] == ""
    assert persisted["claims"][0]["citation_markers"] == [1]
    assert persisted["citations"][0]["evidence"]["table_id"] == "demo#t1"
    # 正文与表格预览不落库，靠 evidence_id 回查，避免会话文件被整表撑大。
    assert "content" not in persisted["citations"][0]["evidence"]
    assert "table_preview" not in persisted["citations"][0]["evidence"]


@pytest.mark.asyncio
async def test_finance_chat_uses_scoped_conversation_and_returns_contract(api_app, monkeypatch):
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return FinanceChatResponse(
            status="answered",
            answer="12.34 千元",
            conversation_id=kwargs["conversation_id"],
            trace=[],
        )

    monkeypatch.setattr(finance_api, "run_finance_query", fake_run)
    monkeypatch.setattr(finance_api, "index_fingerprint", lambda: "fp-test")
    conversation_id = str(uuid.uuid4())

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            "/finance/chat",
            headers=_ALICE,
            json={
                "query": "营业收入",
                "conversation_id": conversation_id,
                "search_mode": "blocks",
                "strategy": "agent",
            },
        )

    assert response.status_code == 200
    assert response.json()["conversation_id"] == conversation_id
    assert captured["query"] == "营业收入"
    assert captured["search_mode"] == "blocks"
    assert captured["conversation_id"] == ConversationNamespace(captured["user_context"]).storage_id(conversation_id)
    assert captured["corpus_fingerprint"] == "fp-test"


def test_decide_strategy_simple_lookup_uses_structured_rag(demo_store):
    strategy, reason = finance_router.decide_strategy("美的 2025 年营业收入是多少？", demo_store)
    assert strategy == "structured_rag"
    assert "结构检索" in reason


def test_decide_strategy_calculation_uses_agent(demo_store):
    strategy, reason = finance_router.decide_strategy(
        "美的 2025 年智能家居收入占公司营业收入多少？毛利率高多少个百分点？",
        demo_store,
    )
    assert strategy == "agent"
    assert "比较/计算" in reason


def test_decide_strategy_future_period_uses_agent(demo_store):
    strategy, reason = finance_router.decide_strategy("2026 年第一季度营业收入是多少？", demo_store)
    assert strategy == "agent"
    assert "未覆盖" in reason


@pytest.mark.asyncio
async def test_finance_chat_auto_routes_simple_query_to_structured(api_app, demo_store, monkeypatch):
    captured = {}

    async def fake_structured(**kwargs):
        captured.update(kwargs)
        return FinanceChatResponse(
            status="answered",
            answer="12.34 千元",
            strategy="structured_rag",
            route_reason=kwargs["route_reason"],
            conversation_id=kwargs["conversation_id"],
        )

    monkeypatch.setattr(finance_api, "get_finance_store", lambda: demo_store)
    monkeypatch.setattr(finance_api, "run_structured_rag", fake_structured)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            "/finance/chat",
            headers=_ALICE,
            json={"query": "美的 2025 年营业收入是多少？", "strategy": "auto"},
        )

    assert response.status_code == 200
    assert response.json()["strategy"] == "structured_rag"
    assert captured["query"] == "美的 2025 年营业收入是多少？"
    assert "结构检索" in captured["route_reason"]


@pytest.mark.asyncio
async def test_finance_chat_requires_auth(api_app):
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post("/finance/chat", json={"query": "营业收入"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_finance_evidence_endpoint_resolves_table_rows(api_app, demo_store, monkeypatch):
    monkeypatch.setattr(finance_api, "get_finance_store", lambda: demo_store)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get(
            "/finance/evidence",
            headers=_ALICE,
            params={"table_id": "demo#t1", "rows": "营业收入", "columns": "2025"},
        )

    assert response.status_code == 200
    evidence = response.json()["evidence"]
    assert evidence["kind"] == "table_rows"
    assert evidence["table_id"] == "demo#t1"
    assert evidence["row_indices"] == [0]
    assert evidence["column_indices"] == [1]
    assert evidence["verified"] is True


@pytest.mark.asyncio
async def test_finance_evidence_returns_404_for_unknown_anchor(api_app, demo_store, monkeypatch):
    monkeypatch.setattr(finance_api, "get_finance_store", lambda: demo_store)
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get(
            "/finance/evidence",
            headers=_ALICE,
            params={"evidence_id": "demo#t999"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_lab_runs_reads_compare_and_manifests(api_app, tmp_path, monkeypatch):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    compare = {
        "metric": "majority",
        "tasks_compared": 30,
        "verdict": "不可分辨（未达到显著差异）",
        "baseline": {"pass_at_1": 0.8, "pass_pow_k": 0.7, "tasks": 30},
        "improved": {"pass_at_1": 0.95, "pass_pow_k": 0.93, "tasks": 30},
        "by_dimension": {
            "恢复与纠错": {
                "tasks": 6,
                "baseline_pass_at_1": 0.22,
                "improved_pass_at_1": 0.83,
                "delta": 0.61,
                "delta_exceeds_ci_width": True,
            }
        },
        "mcnemar": {"fixed": 5, "broke": 0, "p_value": 0.0625, "significant": False},
        "cost": {
            "baseline": {"runs": 90, "tokens_per_run": 2378.2, "median_latency_ms": 6053, "avg_steps": 1.67},
            "improved": {"runs": 90, "tokens_per_run": 5650.1, "median_latency_ms": 10809, "avg_steps": 2.71},
        },
    }
    baseline = {
        "generated_at": "2026-10-27T22:00:00",
        "model": "deepseek-chat",
        "corpus_hash": "abc123",
        "tasks_sha256": "def456",
        "runs_sha256": "ghi789",
        "runs_per_task": 3,
        "judge_enabled": True,
        "git": {"commit": "1234567890", "dirty": True},
        "hard_gates": {"fabrication": {"value": 0, "limit": 0, "pass": True, "blocking": True}},
    }
    improved = {**baseline, "generated_at": "2026-10-27T22:22:34", "model": "deepseek-v4.1-flash"}
    (evidence_dir / "agent_compare3.json").write_text(json.dumps(compare), encoding="utf-8")
    (evidence_dir / "agent_baseline_report3.json").write_text(json.dumps(baseline), encoding="utf-8")
    (evidence_dir / "agent_improved_report3.json").write_text(json.dumps(improved), encoding="utf-8")
    monkeypatch.setenv("LAB_EVIDENCE_DIR", str(evidence_dir))

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get("/lab/runs", headers=_ALICE)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["tasks_compared"] == 30
    assert [arm["name"] for arm in body["arms"]] == ["baseline", "improved"]
    assert body["arms"][1]["tokens_per_run"] == 5650.1
    assert body["arms"][0]["manifest"]["git"]["dirty"] is True
    assert body["dimensions"][0]["delta_exceeds_ci_width"] is True
    assert body["comparison_kind"] == "agent_vs_agent"
    assert body["left_label"] == "Agent baseline"


@pytest.mark.asyncio
async def test_lab_runs_prefers_structured_rag_compare(api_app, tmp_path, monkeypatch):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    compare = {
        "tasks_file": "evaluation/finance_tasks_complex.jsonl",
        "paired": 12,
        "agent_variant": "improved",
        "rag_variant": "rag_structured",
        "rag_retrieval_mode": "structured",
        "agent": {
            "total": 12, "passed": 10, "pass_rate": 0.8333,
            "tokens_mean": 5000.0, "latency_mean": 9000.0, "calls_mean": 2.5,
            "hard_fail_tasks": 0, "hard_fail_reasons": [], "wilson": [0.55, 0.95],
            "citation_precision": 0.9, "point_accuracy": 0.88,
        },
        "rag": {
            "total": 12, "passed": 7, "pass_rate": 0.5833,
            "tokens_mean": 1800.0, "latency_mean": 3000.0, "calls_mean": 1.0,
            "hard_fail_tasks": 0, "hard_fail_reasons": [], "wilson": [0.32, 0.80],
            "citation_precision": 0.7, "point_accuracy": 0.70,
        },
        "delta_pass_rate": 0.25,
        "delta_tokens_mean": 3200.0,
        "flips": {"rag_wrong_agent_right": 3, "rag_right_agent_wrong": 0},
        "thresholds": {
            "delta_pass_rate": 0.05,
            "delta_tokens_ceiling": 5000,
            "pass_ok": True,
            "cost_ok": True,
        },
        "pairs": [
            {"complex_type": "跨表计算", "rag": {"pass": False}, "agent": {"pass": True}},
            {"complex_type": "跨表计算", "rag": {"pass": True}, "agent": {"pass": True}},
        ],
    }
    (evidence_dir / "compare_arms_structured.json").write_text(json.dumps(compare), encoding="utf-8")
    monkeypatch.setenv("LAB_EVIDENCE_DIR", str(evidence_dir))

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get("/lab/runs", headers=_ALICE)

    body = response.json()
    assert body["available"] is True
    assert body["comparison_kind"] == "agent_vs_structured_rag"
    assert [arm["name"] for arm in body["arms"]] == ["rag", "agent"]
    assert body["arms"][0]["label"] == "结构感知 RAG"
    assert body["arms"][1]["pass_at_1"] == 0.8333
    assert body["dimensions"][0]["delta"] == 0.5


@pytest.mark.asyncio
async def test_lab_runs_keeps_fixed_rag_as_optional_baseline(api_app, tmp_path, monkeypatch):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    compare = {
        "paired": 6,
        "rag_retrieval_mode": "fixed",
        "agent": {"total": 6, "passed": 4, "pass_rate": 0.6667, "tokens_mean": 4000.0},
        "rag": {"total": 6, "passed": 2, "pass_rate": 0.3333, "tokens_mean": 1500.0},
        "pairs": [],
        "thresholds": {},
    }
    (evidence_dir / "compare_arms_fixed.json").write_text(json.dumps(compare), encoding="utf-8")
    monkeypatch.setenv("LAB_EVIDENCE_DIR", str(evidence_dir))

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get("/lab/runs", headers=_ALICE)

    body = response.json()
    assert body["available"] is False
    assert body["comparison_kind"] == "agent_vs_fixed_rag"
    assert body["arms"] == []
    assert body["optional_baseline"]["label"] == "固定窗 RAG"
    assert "可选辅助基线" in body["message"]
