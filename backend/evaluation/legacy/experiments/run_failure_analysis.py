"""实验 4：失败案例分析

找出「正确答案没进 top-3」的查询，并区分两类失败：
  - 召回失败：正确答案根本没进候选池（检索阶段丢了）
  - 排序失败：正确答案在候选池里，但没排进 top-3（重排阶段没救回来）

输出：experiments/out/failure_cases.json + failure_cases.txt（便于人工写案例）
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
from app.services.reranker import reranker                              # noqa: E402
from evaluation import run_eval as RE                                   # noqa: E402
from evaluation.sample_data import CORPUS, QA_PAIRS                     # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent / "out" / "failure_cases.json"
PROFILE = "complex_analysis"
CORPUS_TEXT = {c["chunk_id"]: c["content"] for c in CORPUS}
CORPUS_TITLE = {c["chunk_id"]: c.get("title", "") for c in CORPUS}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--cand", type=int, default=25)
    ap.add_argument("--strategy", default="l2", choices=["none", "l1", "l2", "l3"])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="km_fail_"))
    await RE._build_real_indexes(tmp)
    profile = hybrid_retriever.query_type_to_profile(PROFILE)
    cases = []
    total = missed_at_3 = 0
    try:
        for qa in QA_PAIRS:
            query = qa["query"]
            rel = set(qa["relevant_chunk_ids"])
            retrieval_queries = await rag_engine._build_retrieval_queries(
                query, {"query_type": PROFILE, "complexity": 3})
            cands = await rag_engine._retrieve_documents_with_rewrites(
                retrieval_queries=retrieval_queries, top_k=args.cand, query_type=profile) or []
            cands = sorted(cands, key=lambda d: -float(d.get("rrf_score", 0)))
            cand_ids = [c["chunk_id"] for c in cands]

            if args.strategy == "none":
                final = cands[: args.top_k]
            else:
                final = await reranker.rerank(query, list(cands), top_k=args.top_k, strategy=args.strategy)
            final_ids = [c["chunk_id"] for c in final]

            total += 1
            if any(cid in rel for cid in final_ids[:3]):
                continue
            missed_at_3 += 1

            in_candidates = rel & set(cand_ids)
            cases.append({
                "query": query,
                "query_type": qa.get("query_type", "?"),
                "relevant_ids": sorted(rel),
                "relevant_texts": [CORPUS_TEXT.get(cid, "") for cid in sorted(rel)],
                "retrieved_ids": final_ids,
                "retrieved_texts": [CORPUS_TEXT.get(cid, "")[:160] for cid in final_ids],
                "relevant_in_candidates": sorted(in_candidates),
                "relevant_rank_in_candidates": {cid: (cand_ids.index(cid) + 1) for cid in sorted(in_candidates)},
                "failure_type": "排序失败" if in_candidates else "召回失败",
            })
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    payload = {"corpus_chunks": len(CORPUS), "queries": total, "strategy": args.strategy,
               "candidate_k": args.cand, "missed_at_3": missed_at_3,
               "hit_at_3_rate": round(1 - missed_at_3 / total, 4), "cases": cases}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    txt = [f"重排档位={args.strategy}  候选={args.cand}  查询={total}  top-3 未命中={missed_at_3}",
           f"hit@3 = {payload['hit_at_3_rate']}", ""]
    for i, c in enumerate(cases, 1):
        txt += [f"### 案例 {i}｜{c['failure_type']}｜{c['query_type']}",
                f"问题：{c['query']}",
                f"标准答案 chunk：{', '.join(c['relevant_ids'])}",
                f"  内容：{c['relevant_texts'][0][:120]}",
                f"候选池里的名次：{c['relevant_rank_in_candidates']}",
                f"实际返回 top-{args.top_k}：{', '.join(c['retrieved_ids'])}", ""]
    txt_path = args.out.with_suffix(".txt")
    txt_path.write_text("\n".join(txt), encoding="utf-8")

    print("\n".join(txt[:6]))
    print(f"\n共 {len(cases)} 个失败案例")
    print(f"写入 {args.out}\n写入 {txt_path}")


if __name__ == "__main__":
    asyncio.run(main())
