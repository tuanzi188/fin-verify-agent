"""工具层契约测试：分数语义、参数校验、故障注入。

全部离线：不打网络、不调 LLM、不碰真实向量库。
"""

import pytest

from app.tools import mock as mock_module
from app.tools import retrieval
from app.tools.contract import (
    ABSTAIN_KEYWORDS,
    TOOL_SPECS,
    ToolError,
    validate_result,
)
from app.tools.mock import Toolbox, normalize_injections


# ---------------------------------------------------------------------------
# 分数语义（P0 的核心修复）
# ---------------------------------------------------------------------------

def test_three_scores_have_distinct_meaning():
    """RRF 融合分绝不能被当成相似度返回。"""
    item = {
        "chunk_id": "d2_ch1",
        "content": "住宿标准为每晚不超过五百元。",
        "metadata": {"title": "差旅与报销制度", "document_id": "d2"},
        "rrf_score": 0.0164,
        "source_scores": {"vector": 0.87, "keyword": 12.5},
        "source": "vector",
    }
    candidate = retrieval._to_candidate(item)
    # 精排器会把分数写回候选（真实路径由 reranker.rerank 完成）
    candidate["rerank_score"] = 3.21
    result = retrieval._to_tool_result(candidate)

    assert result["similarity"] == 0.87          # 余弦，0-1
    assert result["rrf_score"] == 0.0164         # 融合分，量级极小
    assert result["score"] == 3.21               # 重排分，无量纲
    assert result["similarity"] != result["rrf_score"]


def test_score_falls_back_to_fusion_score_when_rerank_missing():
    """精排不可用时不写 0.0——0.0 会让"降级"看起来像"完全不相关"。"""
    item = {
        "chunk_id": "d2_ch1",
        "content": "x",
        "metadata": {},
        "rrf_score": 0.0164,
        "source_scores": {"vector": 0.7},
        "source": "vector",
    }
    result = retrieval._to_tool_result(retrieval._to_candidate(item))
    assert result["score"] == 0.0164


def test_similarity_is_far_below_the_old_fake_threshold():
    """回归测试：修复前 similarity 最大约 0.033，永远低于 0.35。"""
    worst_case = {"source_scores": {"vector": 1.0, "keyword": 1.0}, "rrf_score": 2 / 61}
    assert retrieval.vector_similarity(worst_case) == 1.0
    assert worst_case["rrf_score"] < 0.35


def test_keyword_only_chunk_has_null_similarity():
    """只被 BM25 召回时没有余弦分，必须是 None，不能用 0 冒充。"""
    item = {
        "chunk_id": "d1_ch1",
        "content": "试用期为三个月。",
        "metadata": {},
        "rrf_score": 0.008,
        "source_scores": {"keyword": 4.2},
        "source": "keyword",
    }
    assert retrieval.vector_similarity(item) is None


def test_memory_fallback_uses_item_score():
    item = {"source": "vector", "score": 0.63}
    assert retrieval.vector_similarity(item) == 0.63


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("top_k", [0, 11, -1, "abc", None])
def test_top_k_out_of_range_raises_bad_args(top_k):
    with pytest.raises(ToolError) as exc_info:
        retrieval._normalize_top_k(top_k)
    assert exc_info.value.kind == "bad_args"


@pytest.mark.parametrize("top_k,expected", [(1, 1), (5, 5), (10, 10), ("7", 7)])
def test_top_k_in_range_is_accepted(top_k, expected):
    assert retrieval._normalize_top_k(top_k) == expected


async def test_search_kb_rejects_blank_query():
    with pytest.raises(ToolError) as exc_info:
        await retrieval.search_kb("   ")
    assert exc_info.value.kind == "bad_args"


# ---------------------------------------------------------------------------
# search_kb / get_citation（monkeypatch 掉下游）
# ---------------------------------------------------------------------------

class _FakeVectorStore:
    def __init__(self, total: int = 2, chunk: dict | None = None, broken: bool = False):
        self._total = total
        self._chunk = chunk
        self._broken = broken

    async def initialize(self):
        return None

    async def count(self):
        if self._broken:
            raise RuntimeError("chroma down")
        return self._total

    async def get_chunk(self, chunk_id):
        return self._chunk


class _FakeHybrid:
    def __init__(self, items):
        self._items = items

    async def retrieve(self, query, top_k):
        return self._items


class _FakeReranker:
    async def rerank(self, query, candidates, top_k, strategy):
        for candidate in candidates:
            candidate["rerank_score"] = 1.5
        return candidates[:top_k]


def _candidate_item():
    return {
        "chunk_id": "d2_ch1",
        "content": "住宿标准为每晚不超过五百元。",
        "metadata": {"title": "差旅与报销制度", "document_id": "d2"},
        "rrf_score": 0.0164,
        "source_scores": {"vector": 0.82},
        "source": "vector",
    }


async def test_search_kb_returns_three_score_fields(monkeypatch):
    monkeypatch.setattr("app.services.vector_store.vector_store", _FakeVectorStore())
    monkeypatch.setattr("app.services.hybrid_search.hybrid_retriever", _FakeHybrid([_candidate_item()]))
    monkeypatch.setattr("app.services.reranker.reranker", _FakeReranker())

    results = await retrieval.search_kb("出差住宿标准", top_k=5)

    assert len(results) == 1
    assert results[0]["chunk_id"] == "d2_ch1"
    assert 0.0 <= results[0]["similarity"] <= 1.0
    assert results[0]["rrf_score"] < 0.1
    assert "score" in results[0]


async def test_search_kb_empty_corpus_returns_empty_list(monkeypatch):
    monkeypatch.setattr("app.services.vector_store.vector_store", _FakeVectorStore(total=0))
    assert await retrieval.search_kb("出差住宿标准") == []


async def test_search_kb_unavailable_store_raises(monkeypatch):
    monkeypatch.setattr("app.services.vector_store.vector_store", _FakeVectorStore(broken=True))
    with pytest.raises(ToolError) as exc_info:
        await retrieval.search_kb("出差住宿标准")
    assert exc_info.value.kind == "unavailable"


async def test_get_citation_found(monkeypatch):
    chunk = {
        "chunk_id": "d2_ch1",
        "content": "住宿标准为每晚不超过五百元。",
        "metadata": {"title": "差旅与报销制度", "document_id": "d2"},
    }
    monkeypatch.setattr("app.services.vector_store.vector_store", _FakeVectorStore(chunk=chunk))

    result = await retrieval.get_citation("d2_ch1")

    assert result["found"] is True
    assert result["document_id"] == "d2"
    assert result["title"] == "差旅与报销制度"


async def test_get_citation_missing(monkeypatch):
    monkeypatch.setattr("app.services.vector_store.vector_store", _FakeVectorStore(chunk=None))
    result = await retrieval.get_citation("nope")
    assert result["found"] is False


async def test_dispatch_unknown_tool_raises_bad_args():
    with pytest.raises(ToolError) as exc_info:
        await retrieval.dispatch("list_documents", {})
    assert exc_info.value.kind == "bad_args"


# ---------------------------------------------------------------------------
# 故障注入
# ---------------------------------------------------------------------------

def test_normalize_injections_drops_illegal_entries():
    plan = normalize_injections([
        {"call_index": 1, "mode": "empty"},
        {"call_index": "2", "mode": "timeout"},
        {"call_index": 0, "mode": "empty"},
        {"call_index": 3, "mode": "nonsense"},
        "not-a-dict",
        None,
    ])
    assert plan == {1: "empty", 2: "timeout"}


async def test_injected_timeout_and_500_are_attributable():
    toolbox = Toolbox([
        {"call_index": 1, "mode": "timeout"},
        {"call_index": 2, "mode": "500"},
    ])

    first = await toolbox.call("search_kb", {"query": "年假"})
    assert first.ok is False
    assert first.error_kind == "timeout"
    assert first.injected == "timeout"
    assert first.result_ids == []

    second = await toolbox.call("search_kb", {"query": "年假"})
    assert second.ok is False
    assert second.error_kind == "upstream_error"
    assert second.injected == "500"


async def test_injected_empty_returns_clean_empty_result():
    toolbox = Toolbox([
        {"call_index": 1, "mode": "empty"},
        {"call_index": 2, "mode": "empty"},
    ])

    search = await toolbox.call("search_kb", {"query": "年假"})
    assert search.ok is True
    assert search.data == []
    assert search.result_count == 0

    citation = await toolbox.call("get_citation", {"chunk_id": "d1_ch1"})
    assert citation.ok is True
    assert citation.data["found"] is False


def test_truncate_payload_keeps_ids_and_marks_items():
    data = [{"chunk_id": "d2_ch1", "content": "长" * 100}]
    truncated = mock_module._truncate_payload("search_kb", data)
    assert truncated[0]["truncated"] is True
    assert truncated[0]["chunk_id"] == "d2_ch1"
    assert truncated[0]["content"].endswith("…[截断]")


async def test_truncated_mode_calls_real_tool_then_cuts(monkeypatch):
    async def fake_dispatch(name, arguments, user_context=None):
        return [{"chunk_id": "d2_ch1", "content": "住宿标准为每晚不超过五百元，一线城市可上浮百分之二十。"}]

    monkeypatch.setattr("app.tools.mock.dispatch", fake_dispatch)
    toolbox = Toolbox([{"call_index": 1, "mode": "truncated"}])

    outcome = await toolbox.call("search_kb", {"query": "住宿"})

    assert outcome.ok is True
    assert outcome.injected == "truncated"
    assert outcome.result_ids == ["d2_ch1"]
    assert outcome.data[0]["truncated"] is True


# ---------------------------------------------------------------------------
# 契约自检
# ---------------------------------------------------------------------------

def test_abstain_keywords_match_rag_engine():
    """两处常量必须一致：Agent 判弃答，线上 API 也判弃答。"""
    from app.services.rag_engine import _CANNOT_ANSWER_KEYWORDS

    assert set(ABSTAIN_KEYWORDS) == set(_CANNOT_ANSWER_KEYWORDS)


def test_tool_specs_expose_two_readonly_tools():
    names = [spec["function"]["name"] for spec in TOOL_SPECS]
    assert names == ["search_kb", "get_citation"]
    for spec in TOOL_SPECS:
        parameters = spec["function"]["parameters"]
        assert parameters["required"]
        assert parameters["properties"]


def test_validate_result_flags_missing_final_step():
    result = {
        "task_id": "T01",
        "variant": "baseline",
        "run_index": 1,
        "status": "answered",
        "answer": "x",
        "citations": [],
        "usage": {"steps": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 5},
        "trace": [{
            "step": 1, "tool": "search_kb", "args": {}, "ok": True,
            "result_ids": [], "result_count": 0, "injected": None,
            "latency_ms": 1, "tokens_in": 0, "tokens_out": 0,
        }],
    }
    problems = validate_result(result)
    assert any("__final__" in problem for problem in problems)


def test_validate_result_flags_bad_status():
    result = {
        "task_id": "T01",
        "variant": "baseline",
        "run_index": 1,
        "status": "success",
        "answer": "x",
        "citations": [],
        "usage": {"steps": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 5},
        "trace": [{
            "step": 1, "tool": "__final__", "args": {}, "ok": True,
            "result_ids": [], "result_count": 0, "injected": None,
            "latency_ms": 1, "tokens_in": 0, "tokens_out": 0,
        }],
    }
    problems = validate_result(result)
    assert any("status" in problem for problem in problems)


# ---------------------------------------------------------------------------
# ACL：线上入口必须过滤，评测路径不过滤
# ---------------------------------------------------------------------------

def _raw_chunk(chunk_id: str, **metadata) -> dict:
    return {
        "chunk_id": chunk_id,
        "content": f"{chunk_id} 的正文",
        "metadata": {"title": chunk_id, "document_id": chunk_id.split("_")[0], **metadata},
        "rrf_score": 0.016,
        "source_scores": {"vector": 0.9},
    }


def _patch_search_pipeline(monkeypatch, raw_items):
    async def fake_initialize():
        return None

    async def fake_count():
        return len(raw_items)

    async def fake_retrieve(query, top_k):
        return raw_items

    async def fake_rerank(query, candidates, top_k=5, strategy="l2"):
        return candidates[:top_k]

    from app.services.hybrid_search import hybrid_retriever
    from app.services.reranker import reranker
    from app.services.vector_store import vector_store

    monkeypatch.setattr(vector_store, "initialize", fake_initialize)
    monkeypatch.setattr(vector_store, "count", fake_count)
    monkeypatch.setattr(hybrid_retriever, "retrieve", fake_retrieve)
    monkeypatch.setattr(reranker, "rerank", fake_rerank)


async def test_search_kb_filters_unauthorized_chunks(monkeypatch):
    from app.core.user_context import UserContext

    _patch_search_pipeline(monkeypatch, [
        _raw_chunk("d1_ch1", is_public=True),
        _raw_chunk("d2_ch1", owner="someone-else"),
    ])

    results = await retrieval.search_kb(
        "住宿", top_k=5, user_context=UserContext(user_id="u1")
    )

    assert [item["chunk_id"] for item in results] == ["d1_ch1"]


async def test_search_kb_without_user_context_keeps_eval_behavior(monkeypatch):
    _patch_search_pipeline(monkeypatch, [
        _raw_chunk("d1_ch1", is_public=True),
        _raw_chunk("d2_ch1", owner="someone-else"),
    ])

    results = await retrieval.search_kb("住宿", top_k=5, user_context=None)

    assert len(results) == 2


async def test_search_kb_admin_sees_all(monkeypatch):
    from app.core.user_context import UserContext

    _patch_search_pipeline(monkeypatch, [
        _raw_chunk("d1_ch1", is_public=False),
        _raw_chunk("d2_ch1", owner="someone-else"),
    ])

    results = await retrieval.search_kb(
        "住宿", top_k=5, user_context=UserContext(user_id="root", roles=["admin"])
    )

    assert len(results) == 2


async def test_get_citation_hides_unauthorized_chunk(monkeypatch):
    from app.core.user_context import UserContext
    from app.services.vector_store import vector_store

    async def fake_initialize():
        return None

    async def fake_get_chunk(chunk_id):
        return {
            "chunk_id": chunk_id,
            "content": "机密正文",
            "metadata": {"title": "机密", "document_id": "d9", "owner": "someone-else"},
        }

    monkeypatch.setattr(vector_store, "initialize", fake_initialize)
    monkeypatch.setattr(vector_store, "get_chunk", fake_get_chunk)

    denied = await retrieval.get_citation("d9_ch1", user_context=UserContext(user_id="u1"))
    assert denied["found"] is False

    allowed = await retrieval.get_citation(
        "d9_ch1", user_context=UserContext(user_id="root", roles=["admin"])
    )
    assert allowed["found"] is True
