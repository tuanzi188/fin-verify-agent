"""产品级结构感知 RAG 的离线测试：不调模型、不建真实向量索引。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.finance.store import FinanceStore
from app.services import finance_rag
from tests.test_finance_api import write_demo_document


class FakeIndex:
    def __init__(self, hits: list[dict]):
        self.hits = hits

    def search(self, query: str, top_k: int = 6):
        return self.hits[:top_k]


def _hit() -> dict:
    return {
        "block_id": "demo#t1#rows#0,1#cols#0,1",
        "doc_id": "demo",
        "kind": "table_rows",
        "page": 42,
        "table_id": "demo#t1",
        "row_indices": [0, 1],
        "column_indices": [0, 1],
        "content": "表 主要会计数据",
    }


@pytest.mark.asyncio
async def test_structured_rag_returns_table_evidence_and_claims(tmp_path, monkeypatch):
    store = FinanceStore(write_demo_document(tmp_path))

    async def fake_chat(*args, **kwargs):
        return (
            "2025 年营业收入为 12.34 千元 [1]。",
            None,
            {"prompt_tokens": 100, "completion_tokens": 20},
        )

    memory = AsyncMock()
    monkeypatch.setattr(finance_rag, "get_finance_store", lambda: store)
    monkeypatch.setattr(finance_rag, "get_block_index", lambda: FakeIndex([_hit()]))
    monkeypatch.setattr(finance_rag.model_provider, "chat", fake_chat)
    monkeypatch.setattr(finance_rag, "conversation_memory", memory)

    response = await finance_rag.run_structured_rag(
        query="2025 年营业收入是多少？",
        conversation_id="conv-rag",
        route_reason="单公司/单期间的查数",
    )

    assert response.strategy == "structured_rag"
    assert response.status == "answered"
    assert response.citations[0].evidence.table_id == "demo#t1"
    assert response.citations[0].evidence.row_indices == [0, 1]
    assert response.claims[0].citation_markers == [1]
    assert response.calc == []
    assert response.trace[0].tool == "search_blocks"
    assert response.token_usage["total_tokens"] == 120
    memory.add_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_structured_rag_abstains_on_empty_retrieval(tmp_path, monkeypatch):
    store = FinanceStore(write_demo_document(tmp_path))
    monkeypatch.setattr(finance_rag, "get_finance_store", lambda: store)
    monkeypatch.setattr(finance_rag, "get_block_index", lambda: FakeIndex([]))
    monkeypatch.setattr(finance_rag, "conversation_memory", AsyncMock())

    response = await finance_rag.run_structured_rag(
        query="不存在的指标",
        conversation_id="conv-empty",
    )

    assert response.status == "abstained"
    assert response.citations == []
    assert "无法确定" in response.answer
