"""实验 1：候选池饱和诊断

回答的问题：为什么「三路融合 / 查询改写」在这套语料上没有增益？

做法：固定候选规模 CAND，统计
  - recall@CAND      正确答案是否已进入候选池
  - 候选池 / 语料     候选池占语料的比例（饱和度）
  - 候选重合度        不同召回方式选出的候选有多大比例相同

若 recall@CAND 已接近 1.0，说明召回阶段没有优化空间，瓶颈在排序。
输出：experiments/out/coverage.json（可用 --out 指定）
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
from evaluation.sample_data import CORPUS, QA_PAIRS                     # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent / "out" / "coverage.json"
MODES = ("dense", "bm25", "hybrid", "hybrid_rw")
PROFILE = "complex_analysis"   # 统一意图档位，避免意图分类非确定性影响实验


async def collect(query: str, top_k: int) -> dict:
    profile = hybrid_retriever.query_type_to_profile(PROFILE)
    dense = await hybrid_retriever._vector_search_safe(query, top_k, None) or []
    bm25 = hybrid_retriever._keyword_search_safe(query, top_k, None) or []
    hybrid = await hybrid_retriever.retrieve(query=query, top_k=top_k, query_type=profile) or []
    rewritten_queries = await rag_engine._build_retrieval_queries(
        query, {"query_type": PROFILE, "complexity": 3})
    rewritten = await rag_engine._retrieve_documents_with_rewrites(
        retrieval_queries=rewritten_queries, top_k=top_k, query_type=profile) or []
    return {
        "dense": {c["chunk_id"] for c in dense},
        "bm25": {c["chunk_id"] for c in bm25},
        "hybrid": {c["chunk_id"] for c in hybrid},
        "hybrid_rw": {c["chunk_id"] for c in rewritten},
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cand", type=int, default=25, help="候选规模，需与线上 ACL 过采样口径一致")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="km_cov_"))
    await RE._build_real_indexes(tmp)

    stats = {m: {"hit": 0, "sizes": [], "union": set()} for m in MODES}
    overlaps = {"dense_vs_hybrid": [], "dense_vs_hybrid_rw": [], "dense_vs_bm25": []}

    try:
        for qa in QA_PAIRS:
            rel = set(qa["relevant_chunk_ids"])
            sets = await collect(qa["query"], args.cand)
            for mode, ids in sets.items():
                stats[mode]["sizes"].append(len(ids))
                stats[mode]["union"] |= ids
                if rel & ids:
                    stats[mode]["hit"] += 1
            for name, (a, b) in {
                "dense_vs_hybrid": ("dense", "hybrid"),
                "dense_vs_hybrid_rw": ("dense", "hybrid_rw"),
                "dense_vs_bm25": ("dense", "bm25"),
            }.items():
                base = sets[a] or {""}
                overlaps[name].append(len(sets[b] & base) / len(base))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n = len(QA_PAIRS)
    result = {
        "corpus_chunks": len(CORPUS),
        "candidate_k": args.cand,
        "queries": n,
        "candidate_pool_ratio": round(args.cand / len(CORPUS), 4),
        "per_mode": {
            m: {
                f"recall@{args.cand}": round(stats[m]["hit"] / n, 4),
                "avg_candidate_size": round(sum(stats[m]["sizes"]) / n, 2),
                "distinct_in_union": len(stats[m]["union"]),
            } for m in MODES
        },
        "candidate_overlap": {k: round(sum(v) / len(v), 4) for k, v in overlaps.items()},
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
