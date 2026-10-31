import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import analytics as analytics_api
from app.api import chat as chat_api
from app.core import auth
from app.core.conversation_scope import ConversationNamespace
from app.core.identity_keys import parse_identity_keys
from app.core.user_context import UserContext
from app.services.conversation_memory import ConversationMemory, JsonSessionStore


@pytest.fixture
def identity_app(monkeypatch, tmp_path):
    test_identities = [
        {"token": "a" * 32, "user_id": "alice", "roles": ["user"], "department": "研发"},
        {"token": "b" * 32, "user_id": "bob", "roles": ["user"], "department": "销售"},
    ]
    monkeypatch.setattr(auth, "API_IDENTITIES", test_identities)
    monkeypatch.setattr(auth, "API_KEY", "legacy-shared-key")
    monkeypatch.setattr(auth.audit_logger, "log", AsyncMock())
    isolated_memory = ConversationMemory(store=JsonSessionStore(str(tmp_path)))
    monkeypatch.setattr(chat_api, "conversation_memory", isolated_memory)

    test_application = FastAPI()
    test_application.include_router(chat_api.router, dependencies=[Depends(auth.verify_api_key)])
    test_application.include_router(analytics_api.router, dependencies=[Depends(auth.verify_api_key)])
    return test_application, isolated_memory


@pytest.mark.asyncio
async def test_forged_roles_are_ignored(identity_app):
    application, _ = identity_app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        forged_response = await client.get("/auth/me", headers={
            "Authorization": "Bearer " + "a" * 32,
            "X-User-ID": "bob",
            "X-User-Roles": "admin",
            "X-User-Department": "finance",
        })
        assert forged_response.status_code == 200
        assert forged_response.json() == {"userId": "alice", "roles": ["user"], "department": "研发"}
        denied_response = await client.post("/analytics/audit", json={}, headers={
            "Authorization": "Bearer " + "a" * 32, "X-User-Roles": "admin",
        })
        assert denied_response.status_code == 403


@pytest.mark.asyncio
async def test_auth_rejects_missing_invalid_and_shared_keys(identity_app):
    application, _ = identity_app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        assert (await client.get("/auth/me")).status_code == 401
        for rejected_key in ["wrong", "legacy-shared-key"]:
            response = await client.get("/auth/me", headers={"X-API-Key": rejected_key})
            assert response.status_code == 403


@pytest.mark.asyncio
async def test_conversation_endpoints_are_isolated(identity_app):
    application, memory = identity_app
    alice_headers = {"Authorization": "Bearer " + "a" * 32}
    bob_headers = {"Authorization": "Bearer " + "b" * 32}
    public_conversation_id = str(uuid4())
    alice_storage_id = ConversationNamespace(UserContext(user_id="alice")).storage_id(public_conversation_id)
    bob_storage_id = ConversationNamespace(UserContext(user_id="bob")).storage_id(public_conversation_id)
    await memory.add_turn(alice_storage_id, "甲的私有问题", "测试回答")
    await memory.add_turn(bob_storage_id, "乙的私有问题", "测试回答")

    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        alice_list = (await client.get("/conversations", headers=alice_headers)).json()["conversations"]
        bob_list = (await client.get("/conversations", headers=bob_headers)).json()["conversations"]
        assert [item["conversation_id"] for item in alice_list] == [public_conversation_id]
        assert [item["conversation_id"] for item in bob_list] == [public_conversation_id]

        for request_method in ["GET", "DELETE"]:
            forbidden_response = await client.request(
                request_method, "/conversations/" + str(uuid4()), headers=bob_headers,
            )
            assert forbidden_response.status_code == 404

        for headers, expected_query in [(alice_headers, "甲的私有问题"), (bob_headers, "乙的私有问题")]:
            detail_response = await client.get("/conversations/" + public_conversation_id, headers=headers)
            assert detail_response.json()["turns"][0]["query"] == expected_query
        assert len(memory._sessions) == 2
        assert (await client.delete("/conversations/" + public_conversation_id, headers=bob_headers)).status_code == 200
        assert (await client.get("/conversations/" + public_conversation_id, headers=alice_headers)).status_code == 200


@pytest.mark.asyncio
async def test_conversation_detail_returns_finance_payload(identity_app):
    """重开旧会话时，结构化查证结果要随 /conversations/{id} 一起返回。"""
    application, memory = identity_app
    alice_headers = {"Authorization": "Bearer " + "a" * 32}
    public_conversation_id = str(uuid4())
    stored_id = ConversationNamespace(UserContext(user_id="alice")).storage_id(public_conversation_id)
    payload = {
        "kind": "finance",
        "status": "answered",
        "strategy": "agent",
        "claims": [{"text": "毛利率 65.71%", "citation_markers": [1]}],
        "citations": [
            {
                "marker": 1,
                "evidence": {
                    "evidence_id": "doc#t0013#rows#5#cols#2",
                    "doc_id": "doc",
                    "kind": "table_rows",
                    "row_indices": [5],
                    "column_indices": [2],
                    "unit": "千元",
                },
            }
        ],
        "calc": [],
        "trace": [],
        "notes": [],
    }
    await memory.add_turn(stored_id, "毛利率是多少？", "65.71% [1]", payload=payload)

    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get(
            "/conversations/" + public_conversation_id, headers=alice_headers
        )

    assert response.status_code == 200
    turn = response.json()["turns"][0]
    assert turn["payload"] == payload
    # 正文与表格预览不落库：会话文件只保留坐标。
    assert "table_preview" not in turn["payload"]["citations"][0]["evidence"]


@pytest.mark.asyncio
async def test_unknown_conversation_is_404_and_does_not_create_session(identity_app):
    application, memory = identity_app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get(
            "/conversations/" + str(uuid4()),
            headers={"Authorization": "Bearer " + "a" * 32},
        )
        assert response.status_code == 404
        assert not memory._sessions


def test_identity_configuration_fails_closed():
    for invalid_configuration in [
        "{", "{}", "[]", '[{"token":"short","user_id":"alice"}]',
        json.dumps([{"token": "a" * 32, "user_id": " anonymous "}]),
    ]:
        with pytest.raises(ValueError):
            parse_identity_keys(invalid_configuration)
    configured_identity = {"token": "a" * 32, "user_id": "alice", "roles": ["ADMIN"]}
    assert parse_identity_keys(json.dumps([configured_identity]))[0]["roles"] == ["admin"]
    with pytest.raises(ValueError):
        parse_identity_keys(json.dumps([configured_identity, configured_identity]))


def test_legacy_sessions_are_only_visible_in_single_user_mode():
    legacy_public_id = str(uuid4())
    legacy_namespace = ConversationNamespace(UserContext())
    alice_namespace = ConversationNamespace(UserContext(user_id="alice"))
    assert legacy_namespace.public_id(legacy_public_id) == legacy_public_id
    assert alice_namespace.public_id(legacy_public_id) is None
    assert legacy_namespace.public_id(alice_namespace.storage_id(legacy_public_id)) is None
