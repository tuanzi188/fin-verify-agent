import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.config import MODEL_PROVIDER, VECTOR_STORE_CONFIG
from app.models.chat import DocumentChunk
from app.services.embedding import get_embedding
from app.services.keyword_search import BM25Searcher, get_bm25_searcher
from app.services.graph_search import graph_searcher
from app.services.vector_store import vector_store
from app.services.reranker import reranker
from app.services.hybrid_search import HybridRetriever, hybrid_retriever, clear_retrieval_cache
from app.services.model_provider import rag_query as model_rag_query
from app.services.model_provider import simple_query as model_simple_query
from app.services import rerank_api

from evaluation.metrics import mrr_at_k, ndcg_at_k, recall_at_k
from evaluation.sample_data import CORPUS, QA_PAIRS

_JUDGE_SYSTEM = "你是知识库回答质量评估员，只输出 JSON，不要输出其他内容。"

_JUDGE_TEMPLATE = (
    "请评估下面问答的质量，输出 JSON 对象，包含两个 0 到 1 之间的小数：\n"
    "1. faithfulness：回答是否完全基于『参考资料』、没有编造资料外的事实（1=完全基于资料，0=完全编造）。\n"
    "2. relevancy：回答是否直接切题、回答了用户问题（1=完全切题，0=无关）。\n\n"
    "用户问题：{query}\n\n"
    "参考资料：\n{context}\n\n"
    "回答：\n{answer}\n\n"
    '只输出：{{"faithfulness": 0.0, "relevancy": 0.0}}'
)

# 检索候选放大倍数：与线上 rag_engine 的 ACL 过采样口径一致
_CANDIDATE_MULTIPLIER = 5


# --------------------------------------------------------------------------
# 轻量模式（自建内存索引，不依赖 ChromaDB / 图谱）
# --------------------------------------------------------------------------
def _dense_search(query_vec, corpus_vecs, corpus, top_k):
    q = np.asarray(query_vec, dtype=np.float32)
    matrix = np.asarray(corpus_vecs, dtype=np.float32)
    query_norm = np.linalg.norm(q)
    doc_norms = np.linalg.norm(matrix, axis=1)
    similarities = (matrix @ q) / (doc_norms * query_norm + 1e-9)

    results = []
    for index in np.argsort(-similarities)[:top_k]:
        item = dict(corpus[index])
        item["score"] = float(similarities[index])
        item["source"] = "vector"
        results.append(item)
    return results


def _bm25_search(bm25, query, top_k):
    return bm25.search(query, top_k=top_k, fuzzy_match=False)


def _fuse(vector_results, keyword_results, top_k, query_type="综合分析"):
    hybrid = HybridRetriever()
    weights = hybrid.weight_profiles.get(query_type, hybrid.weight_profiles["综合分析"])
    return hybrid.rrf_fuse(
        [vector_results, keyword_results],
        weights=[weights.get("vector", 0.4), weights.get("keyword", 0.3)],
        top_k=top_k,
    )


# --------------------------------------------------------------------------
# 真实 pipeline 模式（三路检索 + RRF + 重排，全部走线上同一套组件）
# --------------------------------------------------------------------------
async def _build_real_indexes(tmp_root: Path, corpus: list[dict] | None = None):
    """把评测语料灌入真实的向量库 / BM25 / 图谱索引。

    存储全部指向临时目录，跑完即删，不污染 data/ 下的线上索引。

    `corpus` 默认为检索回归夹具的 `CORPUS`（33 chunk）；能力评测传
    `evaluation.agent_corpus.AGENT_CORPUS`（60 chunk）——两者不能混用，
    否则 60 条 query 的基线指标会被新语料改变。
    """
    corpus = corpus if corpus is not None else CORPUS
    VECTOR_STORE_CONFIG["persist_dir"] = str(tmp_root / "chroma_db")
    vector_store._initialized = False
    vector_store._collection = None
    await vector_store.initialize()

    chunks = [
        DocumentChunk(
            chunk_id=item["chunk_id"],
            doc_id=item["doc_id"],
            content=item["content"],
            metadata={
                "document_id": item["doc_id"],
                "title": item["title"],
                "filename": item["title"],
                "chunk_index": index,
            },
        )
        for index, item in enumerate(corpus)
    ]
    await vector_store.add_chunks(chunks)

    bm25 = get_bm25_searcher()
    bm25.reset_index()
    bm25._tokenizer.user_dict_path = tmp_root / "user_dict.txt"
    bm25.build_index([
        {
            "chunk_id": item["chunk_id"],
            "doc_id": item["doc_id"],
            "content": item["content"],
            "metadata": {"document_id": item["doc_id"], "title": item["title"]},
        }
        for item in corpus
    ])

    graph_searcher.reset_memory_index()
    for item in corpus:
        entities = graph_searcher.extract_entities_from_text(item["content"])
        for entity in entities:
            graph_searcher.add_entity(entity["name"], [item["chunk_id"]], document_id=item["doc_id"])

    clear_retrieval_cache()
    return bm25


async def _real_pipeline_search(query, query_type, top_k, rerank_strategy):
    """返回 (三路融合后的候选 chunk_id, 重排后的 chunk_id)。"""
    candidate_k = max(top_k * _CANDIDATE_MULTIPLIER, top_k)
    fused = await hybrid_retriever.retrieve(query=query, top_k=candidate_k, query_type=query_type)
    fused_ids = [item["chunk_id"] for item in fused]

    if not fused or rerank_strategy == "none":
        return fused_ids, fused_ids[:top_k]

    reranked = await reranker.rerank(query, fused, top_k=top_k, strategy=rerank_strategy)
    return fused_ids, [item["chunk_id"] for item in reranked]


# --------------------------------------------------------------------------
# 生成评测（LLM-as-Judge）
# --------------------------------------------------------------------------
def _has_llm():
    try:
        if MODEL_PROVIDER == "qwen":
            from app.services.qwen_client import qwen_client
            return bool(getattr(qwen_client, "clients", {}))
        from app.services.deepseek import deepseek_client
        return bool(getattr(deepseek_client, "clients", {}))
    except Exception:
        return False


def _item_title(item):
    meta = item.get("metadata") or {}
    return item.get("title") or meta.get("title") or item.get("doc_id") or "未知文档"


def _format_context(items):
    parts = []
    for index, item in enumerate(items, 1):
        title = _item_title(item)
        parts.append(f"[{index}] {title}:\n{item.get('content', '')}")
    return "\n\n".join(parts)


async def _generate_answer(query, context):
    answer, _reasoning, _usage = await model_rag_query(query, context, reasoning_mode="flash")
    return answer


def _parse_judge_json(content):
    cleaned = content.strip()
    cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


async def _judge(query, answer, context):
    prompt = _JUDGE_TEMPLATE.format(query=query, context=context, answer=answer)
    content, _reasoning, _usage = await model_simple_query(prompt, _JUDGE_SYSTEM)
    data = _parse_judge_json(content)
    faith = min(max(float(data.get("faithfulness", 0.0)), 0.0), 1.0)
    relev = min(max(float(data.get("relevancy", 0.0)), 0.0), 1.0)
    return faith, relev


def _avg(rows, key):
    values = [row[key] for row in rows if key in row]
    return sum(values) / len(values) if values else float("nan")


def _metric_block(retrieved_ids, relevant_ids, k_max=5):
    return {
        f"recall@{k}": recall_at_k(retrieved_ids, relevant_ids, k) for k in (1, 3, 5)
    } | {
        f"mrr@{k_max}": mrr_at_k(retrieved_ids, relevant_ids, k_max),
        f"ndcg@{k_max}": ndcg_at_k(retrieved_ids, relevant_ids, k_max),
    }


def _print_report(rows, top_k, use_generation, embedding, pipeline, rerank_strategy, rerank_backend=None):
    print(f"\n=== 效果评测报告 (top_k={top_k}, pipeline={pipeline}, rerank={rerank_strategy}) ===")
    print(f"语料 {len(CORPUS)} 个 chunk，问答 {len(rows)} 条，embedding={embedding.version_tag}")
    if rerank_backend:
        print(f"重排后端：{rerank_backend.get('mode')} / {rerank_backend.get('model')}")

    header = ["query_type", "query", "retrieved@3召回", "最终@1", "最终@3", "最终@5", "最终mrr@5", "最终ndcg@5"]
    if use_generation:
        header += ["faithfulness", "relevancy"]
    print(" | ".join(header))

    for row in rows:
        cells = [
            row.get("query_type", "-"),
            row["query"],
            f"{row['retrieval_recall@3']:.2f}",
            f"{row['final_recall@1']:.2f}",
            f"{row['final_recall@3']:.2f}",
            f"{row['final_recall@5']:.2f}",
            f"{row['final_mrr@5']:.2f}",
            f"{row['final_ndcg@5']:.2f}",
        ]
        if use_generation:
            cells += [f"{row['faithfulness']:.2f}", f"{row['relevancy']:.2f}"]
        print(" | ".join(cells))

    print("\n--- 总体平均：三路融合(未重排) vs 重排后 ---")
    print("                    融合召回           重排后（最终指标）")
    print(f"recall@1   {_avg(rows,'retrieval_recall@1'):>10.3f}   {_avg(rows,'final_recall@1'):>14.3f}")
    print(f"recall@3   {_avg(rows,'retrieval_recall@3'):>10.3f}   {_avg(rows,'final_recall@3'):>14.3f}")
    print(f"recall@5   {_avg(rows,'retrieval_recall@5'):>10.3f}   {_avg(rows,'final_recall@5'):>14.3f}")
    print(f"mrr@5      {_avg(rows,'retrieval_mrr@5'):>10.3f}   {_avg(rows,'final_mrr@5'):>14.3f}")
    print(f"ndcg@5     {_avg(rows,'retrieval_ndcg@5'):>10.3f}   {_avg(rows,'final_ndcg@5'):>14.3f}")

    def hit_at(retrieved_key, k):
        hits = 0
        for row in rows:
            relevant = set(row["relevant"])
            if any(cid in relevant for cid in row[retrieved_key][:k]):
                hits += 1
        return hits / len(rows) if rows else float("nan")

    print(f"hit@3      {hit_at('retrieval_ids',3):>10.3f}   {hit_at('final_ids',3):>14.3f}")

    print("\n--- 分场景（重排后）---")
    by_type = {}
    for row in rows:
        by_type.setdefault(row.get("query_type", "-"), []).append(row)
    print(f"{'场景':<8} {'条数':>4} {'recall@5':>10} {'mrr@5':>8} {'ndcg@5':>8}")
    for qtype, group in by_type.items():
        print(f"{qtype:<8} {len(group):>4} {_avg(group,'final_recall@5'):>10.3f} "
              f"{_avg(group,'final_mrr@5'):>8.3f} {_avg(group,'final_ndcg@5'):>8.3f}")

    if use_generation:
        print(f"\nfaithfulness = {_avg(rows, 'faithfulness'):.3f}")
        print(f"relevancy    = {_avg(rows, 'relevancy'):.3f}")
    else:
        print("\n生成指标未跑（未配置 LLM API key 或已跳过）")

    report_path = _BACKEND_DIR / "eval_report.json"
    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump({
            "embedding": embedding.version_tag,
            "pipeline": pipeline,
            "rerank_strategy": rerank_strategy,
            "rerank_backend": rerank_backend,
            "corpus_chunks": len(CORPUS),
            "queries": len(rows),
            "summary": {
                "retrieval": {
                    "recall@1": _avg(rows, "retrieval_recall@1"),
                    "recall@3": _avg(rows, "retrieval_recall@3"),
                    "recall@5": _avg(rows, "retrieval_recall@5"),
                    "mrr@5": _avg(rows, "retrieval_mrr@5"),
                    "ndcg@5": _avg(rows, "retrieval_ndcg@5"),
                    "hit@3": hit_at("retrieval_ids", 3),
                },
                "final": {
                    "recall@1": _avg(rows, "final_recall@1"),
                    "recall@3": _avg(rows, "final_recall@3"),
                    "recall@5": _avg(rows, "final_recall@5"),
                    "mrr@5": _avg(rows, "final_mrr@5"),
                    "ndcg@5": _avg(rows, "final_ndcg@5"),
                    "hit@3": hit_at("final_ids", 3),
                },
            },
            "rows": rows,
        }, report_file, ensure_ascii=False, indent=2)
    print(f"\n报告已写入：{report_path}")


async def run(top_k, skip_generation, pipeline, rerank_strategy):
    embedding = get_embedding()
    use_generation = (not skip_generation) and _has_llm()
    if skip_generation:
        print("已跳过生成评测（--skip-generation）")

    tmp_root = None
    bm25 = None
    corpus_vecs = None
    query_vecs = None

    if pipeline == "real":
        tmp_root = Path(tempfile.mkdtemp(prefix="km_eval_"))
        bm25 = await _build_real_indexes(tmp_root)
        print(f"真实 pipeline 已就绪（临时索引目录 {tmp_root}）："
              f"向量库 {await vector_store.count()} chunk，BM25 {len(bm25._documents)} chunk")
    else:
        corpus_vecs = embedding.embed([item["content"] for item in CORPUS])
        query_vecs = embedding.embed([qa["query"] for qa in QA_PAIRS])
        bm25 = BM25Searcher()
        bm25._tokenizer.user_dict_path = Path(tempfile.mkdtemp(prefix="km_eval_")) / "user_dict.txt"
        bm25.build_index([
            {
                "chunk_id": item["chunk_id"],
                "doc_id": item["doc_id"],
                "content": item["content"],
                "metadata": {"document_id": item["doc_id"], "title": item["title"]},
            }
            for item in CORPUS
        ])

    try:
        rows = []
        for index, qa in enumerate(QA_PAIRS):
            query_type = qa.get("query_type", "综合分析")
            relevant_ids = qa["relevant_chunk_ids"]

            if pipeline == "real":
                retrieval_ids, final_ids = await _real_pipeline_search(
                    qa["query"], query_type, top_k, rerank_strategy
                )
                final_items = [
                    {"chunk_id": cid, "title": cid, "content": ""} for cid in final_ids
                ]
            else:
                vector_results = _dense_search(query_vecs[index], corpus_vecs, CORPUS, top_k)
                keyword_results = _bm25_search(bm25, qa["query"], top_k)
                fused = _fuse(vector_results, keyword_results, top_k, query_type=query_type)
                retrieval_ids = [item["chunk_id"] for item in fused]
                final_ids = retrieval_ids
                final_items = list(fused)

            retrieval_metrics = _metric_block(retrieval_ids, relevant_ids)
            final_metrics = _metric_block(final_ids, relevant_ids)

            row = {
                "query_type": query_type,
                "query": qa["query"],
                "relevant": relevant_ids,
                "retrieval_ids": retrieval_ids,
                "final_ids": final_ids,
                **{f"retrieval_{key}": value for key, value in retrieval_metrics.items()},
                **{f"final_{key}": value for key, value in final_metrics.items()},
            }

            if use_generation:
                context = _format_context(final_items)
                answer = await _generate_answer(qa["query"], context)
                faith, relev = await _judge(qa["query"], answer, context)
                row["faithfulness"] = faith
                row["relevancy"] = relev
                row["answer"] = answer[:120]
            rows.append(row)

        _print_report(
            rows,
            top_k,
            use_generation,
            embedding,
            pipeline,
            rerank_strategy,
            rerank_api.describe() if rerank_strategy == "l2" else None,
        )
    finally:
        if tmp_root is not None:
            shutil.rmtree(tmp_root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="KnowledgeMind 效果评测")
    parser.add_argument("--top-k", type=int, default=5, help="最终返回的 top-k 数量")
    parser.add_argument("--skip-generation", action="store_true", help="跳过生成评测，只跑检索指标")
    parser.add_argument(
        "--pipeline",
        choices=["real", "lightweight"],
        default="real",
        help="real=走线上同一套三路检索+RRF+重排；lightweight=自建内存索引（旧行为）",
    )
    parser.add_argument(
        "--rerank-strategy",
        choices=["none", "l1", "l2", "l3", "auto"],
        default="l1",
        help="重排档位：l1=规则启发式（默认，无需外部依赖），l2=Cross-Encoder，l3=LLM，none=只融合不重排",
    )
    args = parser.parse_args()
    asyncio.run(run(args.top_k, args.skip_generation, args.pipeline, args.rerank_strategy))


if __name__ == "__main__":
    main()
