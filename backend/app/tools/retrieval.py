"""Agent 可调用的只读工具（进程内实现）。

与 `app/mcp/server.py` 同一套检索逻辑，但有意做了三处不同：

1. **不走协议层**：评测只在进程内调用。MCP 的 stdio/http transport 会把
   序列化与进程通信耗时混进 `latency_ms`，让"Agent 多调了一次工具"和
   "协议慢"没法区分。
2. **分数语义分开**：`score`（重排分）/ `similarity`（余弦 0-1）/
   `rrf_score`（融合分）三个字段各归各的，描述见 `app/tools/contract.py`。
3. **失败抛 `ToolError`**：空结果和工具失败是两种信号——空结果该改写
   query，超时该重试。MCP 现在把两者都压成空列表，恢复维度测不出来。

已知限制（P1 有意保留）：

- 不暴露 `query_type`：`hybrid_retriever` 有六套向量/关键词权重，但让
  Agent 自己选 profile 会把"检索配置问题"和"Agent 决策问题"混在一起。
  等参数错误维度真正要测时再开，且要写进任务卡约束。
- 不做查询改写：改写属于 Agent 的决策（多步/恢复维度），不能藏在工具里。
"""

import logging
from typing import Any, Optional

from app.core.user_context import UserContext
from app.services.access_control import can_access, filter_accessible_records
from app.tools.contract import ToolError

logger = logging.getLogger(__name__)

TOOL_TOP_K_MIN = 1
TOOL_TOP_K_MAX = 10
# 候选放大：先多取再精排，与 MCP 和线上 ACL 口径一致
CANDIDATE_MULTIPLIER = 5


def _normalize_top_k(top_k: Any) -> int:
    """越界直接报错，不静默夹断。

    静默夹断会让"参数错误"这一维度永远测不到——Agent 传了 50 被悄悄改成 10，
    从外部看是"Agent 会用工具"。
    """
    try:
        value = int(top_k)
    except (TypeError, ValueError) as exc:
        raise ToolError("bad_args", f"top_k 必须是整数，收到 {top_k!r}") from exc
    if value < TOOL_TOP_K_MIN or value > TOOL_TOP_K_MAX:
        raise ToolError(
            "bad_args",
            f"top_k 超出 {TOOL_TOP_K_MIN}-{TOOL_TOP_K_MAX}，收到 {value}",
        )
    return value


def vector_similarity(item: dict) -> Optional[float]:
    """取向量路原始余弦分。

    `hybrid_search.rrf_fuse` 把每路的原始分写进 `source_scores`
    （`source_scores["vector"]` 即 `1 - 余弦距离`）。只被 BM25 召回的段落
    没有这个分，返回 None——不要用 0 代替，0 会被误读成"完全不相关"。
    """
    source_scores = item.get("source_scores")
    if isinstance(source_scores, dict):
        raw = source_scores.get("vector")
        if isinstance(raw, (int, float)):
            return round(float(raw), 4)
    # 记忆库兜底模式下没有 source_scores，但 item 自带 score
    if item.get("source") == "vector" and isinstance(item.get("score"), (int, float)):
        return round(float(item["score"]), 4)
    return None


def _to_candidate(item: dict) -> dict:
    metadata = item.get("metadata") or {}
    rrf_score = item.get("rrf_score", item.get("score", 0.0))
    return {
        "chunk_id": item.get("chunk_id", ""),
        "title": metadata.get("title", ""),
        "document_id": metadata.get("document_id", ""),
        "content": item.get("content", ""),
        "rrf_score": float(rrf_score or 0.0),
        "vector_similarity": vector_similarity(item),
    }


def _to_tool_result(item: dict) -> dict:
    similarity = item.get("vector_similarity")
    rerank_score = item.get("rerank_score")
    # 精排不可用时（reranker 抛异常，直接截断候选）没有 rerank_score，
    # 回落成融合分：两者都是"只用于相对排序"，但别记成 0.0 丢掉排序信息。
    score = (
        float(rerank_score)
        if isinstance(rerank_score, (int, float))
        else float(item.get("rrf_score", 0.0) or 0.0)
    )
    return {
        "chunk_id": item.get("chunk_id", ""),
        "title": item.get("title", ""),
        "document_id": item.get("document_id", ""),
        "content": item.get("content", ""),
        "score": round(score, 4),
        "similarity": similarity,
        "rrf_score": round(float(item.get("rrf_score", 0.0) or 0.0), 6),
    }


async def search_kb(
    query: str,
    top_k: int = 5,
    user_context: Optional[UserContext] = None,
) -> list[dict]:
    """在企业知识库中检索相关段落（工具实现，描述见 contract.py）。

    `user_context=None` 表示评测/内部调用，不做 ACL 过滤；
    传给线上接口时必须带用户上下文，否则未授权文档会进入模型上下文。
    """
    if not isinstance(query, str) or not query.strip():
        raise ToolError("bad_args", "query 不能为空")
    top_k = _normalize_top_k(top_k)

    from app.services.hybrid_search import hybrid_retriever
    from app.services.reranker import reranker
    from app.services.vector_store import vector_store

    await vector_store.initialize()

    try:
        total_chunks = await vector_store.count()
    except Exception as exc:  # 向量库不可用要能归因，不能伪装成"没搜到"
        raise ToolError("unavailable", f"向量库不可用: {exc}") from exc
    if total_chunks == 0:
        return []

    candidate_k = max(top_k * CANDIDATE_MULTIPLIER, top_k)
    try:
        raw_results = await hybrid_retriever.retrieve(query=query, top_k=candidate_k)
    except Exception as exc:
        raise ToolError("upstream_error", f"混合检索失败: {exc}") from exc

    # ACL 必须在重排之前：未授权段落一旦进了 candidates，就会通过分数/顺序泄漏存在性。
    if user_context is not None and not user_context.is_admin:
        raw_results = filter_accessible_records(user_context, raw_results)

    candidates = [_to_candidate(item) for item in raw_results if item.get("chunk_id")]
    if not candidates:
        return []

    try:
        ranked = await reranker.rerank(query, candidates, top_k=top_k, strategy="l2")
    except Exception as exc:
        # 精排失败是降级不是失败：融合顺序仍然可用，记日志后继续
        logger.warning("精排失败，降级到融合顺序: %s", exc)
        ranked = candidates[:top_k]

    return [_to_tool_result(item) for item in ranked[:top_k]]


async def get_citation(
    chunk_id: str,
    user_context: Optional[UserContext] = None,
) -> dict:
    """按 chunk_id 取回原文与出处（工具实现，描述见 contract.py）。"""
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise ToolError("bad_args", "chunk_id 不能为空")

    from app.services.vector_store import vector_store

    await vector_store.initialize()
    try:
        item = await vector_store.get_chunk(chunk_id)
    except Exception as exc:
        raise ToolError("upstream_error", f"取原文失败: {exc}") from exc

    if not item:
        return {"found": False, "chunk_id": chunk_id, "error": "未找到该段落"}

    metadata = item.get("metadata") or {}
    # 越权时不区分"不存在"与"无权访问"，避免用 found 探测文档是否存在。
    if user_context is not None and not can_access(user_context, metadata):
        return {"found": False, "chunk_id": chunk_id, "error": "未找到该段落"}

    return {
        "found": True,
        "chunk_id": item.get("chunk_id", chunk_id),
        "title": metadata.get("title", metadata.get("filename", "")),
        "document_id": metadata.get("document_id", ""),
        "content": item.get("content", ""),
    }


TOOL_FUNCTIONS = {
    "search_kb": search_kb,
    "get_citation": get_citation,
}


async def dispatch(
    name: str,
    arguments: dict,
    user_context: Optional[UserContext] = None,
) -> Any:
    """按名字调用工具。参数不合法统一转成 ToolError，便于 trace 归因。"""
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        raise ToolError("bad_args", f"未知工具: {name}")
    try:
        return await function(**(arguments or {}), user_context=user_context)
    except TypeError as exc:
        raise ToolError("bad_args", f"{name} 参数不合法: {exc}") from exc


def summarize_result(name: str, data: Any) -> tuple[list[str], int]:
    """给 trace 用：从工具返回里抽 result_ids 与 result_count。"""
    if name == "search_kb":
        if not isinstance(data, list):
            return [], 0
        ids = [str(item.get("chunk_id", "")) for item in data if item.get("chunk_id")]
        return ids, len(data)
    if name == "get_citation":
        if not isinstance(data, dict) or not data.get("found"):
            return [], 0
        chunk_id = str(data.get("chunk_id", ""))
        return [chunk_id], 1
    return [], 0
