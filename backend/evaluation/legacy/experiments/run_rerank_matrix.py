"""实验 3：重排档位矩阵（效果 × 成本 × 延迟）

固定召回（融合，候选 25），比较四档重排：
  none  不重排（只按融合分排序）
  l1    规则启发式（关键词重合）
  l2    Cross-Encoder（bge-reranker-base，本地 CPU）
  l3    LLM 重排（推理型模型，逐条打分）

同时记录每档的 token 成本与延迟——这是「按场景选档」的依据。

输出：experiments/out/rerank_matrix.json
"""
import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services import rag_engine                                    # noqa: E402
from app.services.deepseek import deepseek_client                      # noqa: E402
from app.services.hybrid_search import hybrid_retriever                 # noqa: E402
from app.services.reranker import reranker                              # noqa: E402
from evaluation import run_eval as RE                                   # noqa: E402
from evaluation.metrics import mrr_at_k, ndcg_at_k, recall_at_k         # noqa: E402
from evaluation.sample_data import CORPUS, QA_PAIRS                     # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent / "out" / "rerank_matrix.json"
PROFILE = "complex_analysis"
STRATEGIES = ("none", "l1", "l2", "l3")


def metrics_block(ids, relevant, k_max=5):
    return {f"recall@{k}": round(recall_at_k(ids, relevant, k), 4) for k in (1, 3, 5)} | {
        f"mrr@{k_max}": round(mrr_at_k(ids, relevant, k_max), 4),
        f"ndcg@{k_max}": round(ndcg_at_k(ids, relevant, k_max), 4),
    }


def hit_at(rows, k):
    return round(sum(1 for r in rows if any(c in set(r["relevant"]) for c in r["ids"][:k])) / len(rows), 4)


def summarize(rows):
    def avg(key):
        return round(sum(r[key] for r in rows) / len(rows), 4)
    return {"n": len(rows), "recall@1": avg("recall@1"), "recall@3": avg("recall@3"),
            "recall@5": avg("recall@5"), "mrr@5": avg("mrr@5"), "ndcg@5": avg("ndcg@5"),
            "hit@1": hit_at(rows, 1), "hit@3": hit_at(rows, 3)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--cand", type=int, default=25)
    ap.add_argument("--strategies", default=",".join(STRATEGIES))
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    # 记录 LLM 调用成本（仅 l3 会触发）
    usages = []
    _orig_simple = deepseek_client.simple_query

    async def _wrapped(prompt, *a, **kw):
        t0 = time.time()
        content, reasoning, usage = await _orig_simple(prompt, *a, **kw)
        usages.append({"in": (usage or {}).get("prompt_tokens", 0),
                       "out": (usage or {}).get("completion_tokens", 0),
                       "ms": round((time.time() - t0) * 1000, 1)})
        return content, reasoning, usage

    deepseek_client.simple_query = _wrapped

    tmp = Path(tempfile.mkdtemp(prefix="km_rr_"))
    await RE._build_real_indexes(tmp)
    profile = hybrid_retriever.query_type_to_profile(PROFILE)
    result = {}
    try:
        for strategy in strategies:
            rows, latencies, before = [], [], len(usages)
            for qa in QA_PAIRS:
                query = qa["query"]
                cands = await rag_engine._retrieve_documents_with_rewrites(
                    retrieval_queries=[query], top_k=args.cand, query_type=profile) or []
                cands = sorted(cands, key=lambda d: -float(d.get("rrf_score", 0)))
                t0 = time.time()
                if strategy == "none":
                    final = cands[: args.top_k]
                else:
                    final = await reranker.rerank(query, list(cands), top_k=args.top_k, strategy=strategy)
                latencies.append(round((time.time() - t0) * 1000, 1))
                ids = [c["chunk_id"] for c in final]
                rel = qa["relevant_chunk_ids"]
                rows.append({"query_type": qa.get("query_type", "?"), "relevant": rel, "ids": ids,
                             **metrics_block(ids, rel)})

            by_type = {}
            for qt in sorted({r["query_type"] for r in rows}):
                group = [r for r in rows if r["query_type"] == qt]
                by_type[qt] = {"n": len(group),
                               "recall@3": round(sum(r["recall@3"] for r in group) / len(group), 4),
                               "mrr@5": round(sum(r["mrr@5"] for r in group) / len(group), 4)}

            calls = usages[before:]
            cost = {
                "llm_calls": len(calls),
                "avg_in_tokens": round(sum(c["in"] for c in calls) / len(calls), 1) if calls else 0,
                "avg_out_tokens": round(sum(c["out"] for c in calls) / len(calls), 1) if calls else 0,
                "avg_llm_ms": round(sum(c["ms"] for c in calls) / len(calls), 1) if calls else 0,
                "avg_rerank_ms": round(sum(latencies) / len(latencies), 1),
            }
            result[strategy] = {"overall": summarize(rows), "by_query_type": by_type, "cost": cost}
            o, c = result[strategy]["overall"], cost
            print(f"{strategy:5} recall@3={o['recall@3']:.4f} mrr@5={o['mrr@5']:.4f} ndcg@5={o['ndcg@5']:.4f} "
                  f"hit@3={o['hit@3']:.4f} | llm_calls={c['llm_calls']} "
                  f"tokens={c['avg_in_tokens']}/{c['avg_out_tokens']} rerank={c['avg_rerank_ms']}ms", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        deepseek_client.simple_query = _orig_simple

    payload = {"corpus_chunks": len(CORPUS), "queries": len(QA_PAIRS), "top_k": args.top_k,
               "candidate_k": args.cand, "retrieval": f"hybrid (candidates={args.cand})", "results": result}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
