"""实验 2：召回方式矩阵（固定无重排，只比较召回）

四种召回：单路向量 / 纯 BM25 / 三路融合（RRF）/ 三路融合 + 查询改写。
用途：回答「融合与改写到底有没有带来增益」。

输出：experiments/out/retrieval_matrix.json
"""
import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services import rag_engine                                    # noqa: E402
from app.services.hybrid_search import hybrid_retriever                 # noqa: E402
from evaluation import run_eval as RE                                   # noqa: E402
from evaluation.metrics import mrr_at_k, ndcg_at_k, recall_at_k         # noqa: E402
from evaluation.sample_data import CORPUS, QA_PAIRS                     # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent / "out" / "retrieval_matrix.json"
PROFILE = "complex_analysis"


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
    return {
        "n": len(rows),
        "recall@1": avg("recall@1"), "recall@3": avg("recall@3"), "recall@5": avg("recall@5"),
        "mrr@5": avg("mrr@5"), "ndcg@5": avg("ndcg@5"),
        "hit@1": hit_at(rows, 1), "hit@3": hit_at(rows, 3),
    }


async def retrieve(mode: str, query: str, top_k: int) -> list:
    profile = hybrid_retriever.query_type_to_profile(PROFILE)
    if mode == "dense":
        hits = await hybrid_retriever._vector_search_safe(query, top_k, None) or []
    elif mode == "bm25":
        hits = hybrid_retriever._keyword_search_safe(query, top_k, None) or []
    elif mode == "hybrid":
        hits = await hybrid_retriever.retrieve(query=query, top_k=top_k, query_type=profile) or []
    elif mode == "hybrid_rw":
        retrieval_queries = await rag_engine._build_retrieval_queries(
            query, {"query_type": PROFILE, "complexity": 3})
        hits = await rag_engine._retrieve_documents_with_rewrites(
            retrieval_queries=retrieval_queries, top_k=top_k, query_type=profile) or []
        hits = sorted(hits, key=lambda d: -float(d.get("rrf_score", 0)))
    else:
        raise ValueError(mode)
    return hits


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--cand", type=int, default=25)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="km_rm_"))
    await RE._build_real_indexes(tmp)
    result = {}
    try:
        for mode in ("dense", "bm25", "hybrid", "hybrid_rw"):
            # 所有召回方式使用同一候选池：此前 hybrid_rw 单独用 25 候选，
            # 把「改写」与「候选池大小」两个变量混在了一起（见 RESULTS.md 复核说明）。
            full_k = args.cand
            rows = []
            for qa in QA_PAIRS:
                hits = await retrieve(mode, qa["query"], full_k)
                ids = [h["chunk_id"] for h in hits][: args.top_k]
                rel = qa["relevant_chunk_ids"]
                rows.append({"query_type": qa.get("query_type", "?"), "relevant": rel, "ids": ids, **metrics_block(ids, rel)})
            by_type = {}
            for qt in sorted({r["query_type"] for r in rows}):
                group = [r for r in rows if r["query_type"] == qt]
                by_type[qt] = {"n": len(group), "recall@3": round(sum(r["recall@3"] for r in group) / len(group), 4),
                               "mrr@5": round(sum(r["mrr@5"] for r in group) / len(group), 4)}
            result[mode] = {"overall": summarize(rows), "by_query_type": by_type}
            o = result[mode]["overall"]
            print(f"{mode:10} recall@3={o['recall@3']:.4f} mrr@5={o['mrr@5']:.4f} ndcg@5={o['ndcg@5']:.4f} hit@3={o['hit@3']:.4f}", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    payload = {"corpus_chunks": len(CORPUS), "queries": len(QA_PAIRS),
               "top_k": args.top_k, "intent_profile": PROFILE, "results": result}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
