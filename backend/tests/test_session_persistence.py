import asyncio
from uuid import uuid4

import pytest

from app.core.conversation_scope import ConversationNamespace
from app.core.user_context import UserContext
from app.services.conversation_memory import ConversationMemory, JsonSessionStore


@pytest.mark.asyncio
async def test_scoped_sessions_survive_reload(tmp_path):
    session_store = JsonSessionStore(str(tmp_path))
    first_memory = ConversationMemory(store=session_store)
    first_namespace = ConversationNamespace(UserContext(user_id="alice"))
    public_identifier = str(uuid4())
    private_identifier = first_namespace.storage_id(public_identifier)
    await first_memory.add_turn(private_identifier, "问题", "回答")
    await first_memory.flush()
    first_memory._flush_task.cancel()
    second_memory = ConversationMemory(store=JsonSessionStore(str(tmp_path)))
    await second_memory._init_store()
    reloaded_session = await second_memory.get_conversation(private_identifier)
    assert reloaded_session["turns"][0]["answer"] == "回答"
    assert first_namespace.public_id(reloaded_session["conversation_id"]) == public_identifier
    assert ConversationNamespace(UserContext(user_id="bob")).public_id(private_identifier) is None


@pytest.mark.asyncio
async def test_turn_payload_survives_reload(tmp_path):
    """结构化查证载荷必须随会话落盘，重新打开还能还原结论与引用坐标。"""
    payload = {
        "kind": "finance",
        "status": "answered",
        "strategy": "agent",
        "claims": [{"text": "毛利率 65.71%", "citation_markers": [1], "calc_index": 0}],
        "citations": [
            {
                "marker": 1,
                "evidence": {
                    "evidence_id": "doc#t0013#rows#5#cols#2",
                    "doc_id": "doc",
                    "kind": "table_rows",
                    "unit": "千元",
                    "row_indices": [5],
                    "column_indices": [2],
                },
            }
        ],
        "calc": [{"formula": "a/b*100", "inputs": {"a": 1}, "result": 65.71, "unit": "%"}],
        "trace": [{"step": 1, "tool": "search_blocks", "ok": True, "result_count": 3}],
        "notes": [],
    }
    first_memory = ConversationMemory(store=JsonSessionStore(str(tmp_path)))
    await first_memory.add_turn("conv-1", "问题", "回答", payload=payload)
    await first_memory.flush()
    first_memory._flush_task.cancel()

    second_memory = ConversationMemory(store=JsonSessionStore(str(tmp_path)))
    await second_memory._init_store()
    reloaded = await second_memory.get_conversation("conv-1")
    assert reloaded is not None
    assert reloaded["turns"][0]["payload"] == payload


@pytest.mark.asyncio
async def test_turn_without_payload_stays_backward_compatible(tmp_path):
    """旧会话文件没有 payload 键，读取后不能出现空 dict 冒充结构化结果。"""
    first_memory = ConversationMemory(store=JsonSessionStore(str(tmp_path)))
    await first_memory.add_turn("conv-legacy", "旧问题", "旧回答")
    await first_memory.flush()
    first_memory._flush_task.cancel()

    second_memory = ConversationMemory(store=JsonSessionStore(str(tmp_path)))
    await second_memory._init_store()
    reloaded = await second_memory.get_conversation("conv-legacy")
    assert reloaded is not None
    assert "payload" not in reloaded["turns"][0]


@pytest.mark.asyncio
async def test_session_limit_does_not_deadlock(tmp_path):
    bounded_memory = ConversationMemory(max_sessions=1, store=JsonSessionStore(str(tmp_path)))
    await bounded_memory.get_or_create_session(str(uuid4()))
    next_session_id = str(uuid4())
    new_session = await asyncio.wait_for(bounded_memory.get_or_create_session(next_session_id), timeout=1)
    assert new_session.conversation_id == next_session_id
    assert len(bounded_memory._sessions) == 1
