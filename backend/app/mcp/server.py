"""KnowledgeMind MCP server

把检索能力以 MCP 协议对外提供，任何 MCP 兼容客户端（Cursor / Claude Desktop /
LangGraph 等）都能接入。

运行：
    cd backend
    python -m app.mcp.server                          # stdio（本机客户端）
    python -m app.mcp.server --transport http --port 8931   # streamable-http（远程/多客户端）

数据源：
    默认读取真实向量库（DATA_DIR/chroma_db），复用主链路的混合检索 + CE 重排；
    设置环境变量 MCP_DEMO=1 时改用 evaluation/sample_data 的内存索引，便于零依赖演示。

工具：
    search_kb(query, top_k)   混合召回 + Cross-Encoder 重排后返回相关段落
    get_citation(chunk_id)    按 chunk 取原文与出处（答案回查）

已知限制：
    1. 无鉴权：MCP 无内建鉴权。stdio 模式靠进程隔离；http 模式默认只绑 127.0.0.1，
       如需跨机访问必须在外层网关加 token。
    2. 无用户身份：工具调用不携带 ACL 上下文，只应部署在可信环境。
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from mcp.server.mcpserver import MCPServer                            # noqa: E402

DEMO_MODE = os.getenv("MCP_DEMO", "0").strip().lower() in {"1", "true", "yes"}

server = MCPServer(
    name="knowledgemind",
    version="0.3.0",
    instructions=(
        "企业知识检索服务。用于查询公司制度、流程、规定类问题。"
        "回答前先调用 search_kb 获取原文，再用 get_citation 回查出处；"
        "如果 search_kb 返回空或相关度低，应明确说明资料不足，不要编造。"
    ),
)

# 候选放大倍数：先多取再重排，与线上 _ACL_OVERFETCH_MULTIPLIER 口径一致
_CANDIDATE_MULTIPLIER = 5

_state: dict[str, Any] = {}


def _load_demo_corpus() -> list[dict]:
    from evaluation.sample_data import CORPUS

    return CORPUS


async def _ensure_demo_index() -> None:
    if _state.get("demo_ready"):
        return
    from app.services.embedding import get_embedding

    embedding = get_embedding()
    corpus = _load_demo_corpus()
    vectors = embedding.embed([item["content"] for item in corpus])
    _state["embedding"] = embedding
    _state["vectors"] = np.asarray(vectors, dtype=np.float32)
    _state["by_id"] = {item["chunk_id"]: item for item in corpus}
    _state["demo_ready"] = True


async def _ensure_real_index() -> None:
    if _state.get("real_ready"):
        return
    from app.services.vector_store import vector_store

    await vector_store.initialize()
    _state["real_ready"] = True


@server.tool()
async def search_kb(query: str, top_k: int = 5) -> list[dict]:
    """在企业知识库中检索相关段落。

    当用户询问公司制度、流程、规定、政策类问题时使用。
    返回段落 id、所属文档、正文，以及三个分数：
      - `score`：Cross-Encoder 重排分（无固定量纲，越高越相关，仅用于相对排序）
      - `similarity`：向量余弦相似度（通常 0-1）；该段落只被关键词路召回时为 null
      - `rrf_score`：RRF 融合分（rrf_k=60，量级约 0.016），仅供调试，
        不要用它判断资料是否充足
    若返回空列表，或所有条目的 `similarity` 都低于 0.35，说明资料不足，应如实说明。

    Args:
        query: 用户问题原文，不要改写
        top_k: 返回条数，1-10，默认 5（越界夹断）
    """
    from app.services.reranker import reranker
    from app.tools.retrieval import vector_similarity

    top_k = max(1, min(int(top_k or 5), 10))
    candidate_k = max(top_k * _CANDIDATE_MULTIPLIER, top_k)

    if DEMO_MODE:
        await _ensure_demo_index()
        q = np.asarray(_state["embedding"].embed([query])[0], dtype=np.float32)
        matrix = _state["vectors"]
        sims = (matrix @ q) / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(q) + 1e-9)
        order = np.argsort(-sims)[:candidate_k]
        corpus = _load_demo_corpus()
        candidates = [
            {
                "chunk_id": corpus[i]["chunk_id"],
                "title": corpus[i].get("title", ""),
                "document_id": corpus[i].get("doc_id", ""),
                "content": corpus[i]["content"],
                "rrf_score": float(sims[i]),
                # demo 模式下相似度就是余弦，直接复用
                "vector_similarity": float(sims[i]),
            }
            for i in order
        ]
    else:
        # 复用线上检索链路（向量 + BM25 + RRF），避免维护第二套召回逻辑
        await _ensure_real_index()
        from app.services.hybrid_search import hybrid_retriever

        raw_results = await hybrid_retriever.retrieve(query=query, top_k=candidate_k)
        candidates = [
            {
                "chunk_id": item.get("chunk_id", ""),
                "title": (item.get("metadata") or {}).get("title", ""),
                "document_id": (item.get("metadata") or {}).get("document_id", ""),
                "content": item.get("content", ""),
                "rrf_score": float(item.get("rrf_score", item.get("score", 0.0))),
                "vector_similarity": vector_similarity(item),
            }
            for item in raw_results
        ]

    try:
        ranked = await reranker.rerank(query, candidates, top_k=top_k, strategy="l2")
    except Exception:
        ranked = candidates[:top_k]

    return [
        {
            "chunk_id": item["chunk_id"],
            "title": item.get("title", ""),
            "document_id": item.get("document_id", ""),
            "score": round(float(item.get("rerank_score", 0.0)), 4),
            "similarity": item.get("vector_similarity"),
            "rrf_score": round(float(item.get("rrf_score", 0.0)), 6),
            "content": item["content"],
        }
        for item in ranked[:top_k]
    ]


@server.tool()
async def get_citation(chunk_id: str) -> dict:
    """按 chunk_id 取出原文与出处，用于回查答案依据。

    Args:
        chunk_id: search_kb 返回的段落 id
    """
    if DEMO_MODE:
        await _ensure_demo_index()
        item = _state["by_id"].get(chunk_id)
        if not item:
            return {"found": False, "chunk_id": chunk_id, "error": "未找到该段落"}
        return {
            "found": True,
            "chunk_id": chunk_id,
            "title": item.get("title", ""),
            "document_id": item.get("doc_id", ""),
            "content": item["content"],
        }

    await _ensure_real_index()
    from app.services.vector_store import vector_store

    item = await vector_store.get_chunk(chunk_id)
    if not item:
        return {"found": False, "chunk_id": chunk_id, "error": "未找到该段落"}
    metadata = item.get("metadata") or {}
    return {
        "found": True,
        "chunk_id": chunk_id,
        "title": metadata.get("title", metadata.get("filename", "")),
        "document_id": metadata.get("document_id", ""),
        "content": item.get("content", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="KnowledgeMind MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="stdio=本机客户端；http=streamable-http，可跨机/多客户端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8931)
    args = parser.parse_args()

    if args.transport == "stdio":
        asyncio.run(server.run_stdio_async())
    else:
        print(f"streamable-http: http://{args.host}:{args.port}/mcp", flush=True)
        asyncio.run(server.run_streamable_http_async(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
