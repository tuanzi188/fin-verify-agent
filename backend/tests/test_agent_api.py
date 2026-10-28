"""Agent 模式的 HTTP 入口测试：复用 /chat，不新开端点。

覆盖：鉴权、响应契约、user_context 透传、会话落盘、extra=forbid、stream 拒绝。
全部离线：LLM 与检索都用替身。
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import chat as chat_api
from app.core import auth
from app.core.conversation_scope import ConversationNamespace
from app.models.chat import ChatResponse
from app.services import agent_entry
from app.services.conversation_memory import ConversationMemory, JsonSessionStore

_ALICE = {"Authorization": "Bearer " + "a" * 32}


def _agent_result(status: str = "answered") -> dict:
    return {
        "task_id": "ui-test",
        "variant": "baseline",
        "run_index": 1,
        "status": status,
        "answer": "住宿标准为每晚不超过五百元 [1]。",
        "citations": [{"marker": 1, "chunk_id": "d2_ch1"}],
        "trace": [
            {
                "step": 1, "tool": "search_kb", "args": {"query": "住宿"},
                "ok": True, "result_ids": ["d2_ch1"], "result_count": 1,
                "injected": None, "latency_ms": 10, "tokens_in": 1, "tokens_out": 2,
            },
            {
                "step": 2, "tool": "__final__", "args": {}, "ok": True,
                "result_ids": [], "result_count": 0, "injected": None,
                "latency_ms": 5, "tokens_in": 1, "tokens_out": 2,
                "self_eval": None, "note": "ok",
            },
        ],
        "usage": {"steps": 1, "tokens_in": 2, "tokens_out": 4, "latency_ms": 15},
    }


@pytest.fixture
def agent_app(monkeypatch, tmp_path):
    test_identities = [
        {"token": "a" * 32, "user_id": "alice", "roles": ["user"], "department": "研发"},
    ]
    monkeypatch.setattr(auth, "API_IDENTITIES", test_identities)
    monkeypatch.setattr(auth, "API_KEY", "legacy-shared-key")
    monkeypatch.setattr(auth.audit_logger, "log", AsyncMock())

    isolated_memory = ConversationMemory(store=JsonSessionStore(str(tmp_path)))
    monkeypatch.setattr(agent_entry, "conversation_memory", isolated_memory)

    async def fake_get_citation(chunk_id, user_context=None):
        return {
            "found": True,
            "chunk_id": chunk_id,
            "title": "差旅与报销制度",
            "document_id": "d2",
            "content": "住宿标准为每晚不超过五百元。",
        }

    monkeypatch.setattr(agent_entry, "get_citation", fake_get_citation)

    application = FastAPI()
    application.include_router(chat_api.router, dependencies=[Depends(auth.verify_api_key)])
    return application, isolated_memory


@pytest.mark.asyncio
async def test_agent_mode_requires_auth(agent_app):
    application, _ = agent_app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post("/chat", json={"query": "住宿标准", "mode": "agent"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_agent_mode_returns_contract_and_persists(agent_app, monkeypatch):
    application, memory = agent_app
    captured = {}

    async def fake_run(task, variant="baseline", run_index=1, user_context=None, clear_cache=True):
        captured["task"] = task
        captured["user_context"] = user_context
        captured["clear_cache"] = clear_cache
        return _agent_result()

    monkeypatch.setattr(agent_entry, "run_agent", fake_run)
    conversation_id = str(uuid4())

    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post("/chat", headers=_ALICE, json={
            "query": "出差住宿标准是多少？",
            "conversation_id": conversation_id,
            "mode": "agent",
        })

    assert response.status_code == 200
    body = response.json()
    assert body["conversation_id"] == conversation_id
    assert body["status"] == "answered"
    assert body["variant"] == "baseline"
    assert body["duration_ms"] == 15
    assert body["citations"] == [{
        "document_id": "d2",
        "document_title": "差旅与报销制度",
        "page": None,
        "paragraph": None,
        "content": "住宿标准为每晚不超过五百元。",
        "score": None,
    }]
    assert [step["tool"] for step in body["trace"]] == ["search_kb", "__final__"]
    assert body["trace"][0]["args"] == {"query": "住宿"}

    # 透传：用户上下文、任务参数、线上不清缓存
    assert captured["user_context"].user_id == "alice"
    assert captured["task"]["question"] == "出差住宿标准是多少？"
    assert captured["task"]["clarify_mode"] == "ask"
    assert captured["clear_cache"] is False

    # 会话落盘
    storage_id = ConversationNamespace(captured["user_context"]).storage_id(conversation_id)
    stored = await memory.get_or_create_session(storage_id)
    assert stored.turns[0].query == "出差住宿标准是多少？"


@pytest.mark.asyncio
async def test_agent_error_status_still_returns_200(agent_app, monkeypatch):
    application, _ = agent_app

    async def fake_run(task, variant="baseline", run_index=1, user_context=None, clear_cache=True):
        return _agent_result(status="error")

    monkeypatch.setattr(agent_entry, "run_agent", fake_run)

    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post("/chat", headers=_ALICE, json={
            "query": "住宿标准", "mode": "agent",
        })

    assert response.status_code == 200
    assert response.json()["status"] == "error"


@pytest.mark.asyncio
async def test_agent_mode_rejects_unknown_fields(agent_app):
    """mock/inject 只属于评测，不能从公网打开。"""
    application, _ = agent_app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post("/chat", headers=_ALICE, json={
            "query": "住宿标准", "mode": "agent", "mock": True,
        })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_agent_mode_validates_max_steps(agent_app):
    application, _ = agent_app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post("/chat", headers=_ALICE, json={
            "query": "住宿标准", "mode": "agent", "max_steps": 99,
        })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_stream_rejects_agent_mode(agent_app):
    application, _ = agent_app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post("/chat/stream", headers=_ALICE, json={
            "query": "住宿标准", "mode": "agent",
        })
    assert response.status_code == 400
    assert "流式" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rag_mode_still_uses_rag_pipeline(agent_app, monkeypatch):
    application, _ = agent_app
    called = {}

    async def fake_rag(**options):
        called.update(options)
        return ChatResponse(answer="RAG 回答", conversation_id=options["conversation_id"])

    monkeypatch.setattr(chat_api, "rag_query", fake_rag)

    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.post("/chat", headers=_ALICE, json={"query": "住宿标准"})

    assert response.status_code == 200
    assert response.json()["answer"] == "RAG 回答"
    assert response.json()["status"] is None
    assert called["query"] == "住宿标准"
