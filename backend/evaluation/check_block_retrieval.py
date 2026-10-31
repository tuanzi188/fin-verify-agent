"""检索一致性验收：Agent 的 `search_blocks` 与结构 RAG 的 `search` 必须同序同内容。

为什么单独跑：两边现在都调用同一个 `BlockIndex`，但入口不同（工具层 vs 评测臂）。
只看“返回了相同的块集合”不够——排序、过滤条件和证据正文任一不同，生成结果就会分叉。
这个脚本逐题比对：

- 有序 `block_id` 列表；
- 每个块的 `doc_id` / 期间 / 来源 / 行组坐标；
- 每个块正文的 sha256；
- 分数序列；
- 问题点名的公司/期间过滤是否生效。

用法（在 backend 目录下）：

    python -m evaluation.check_block_retrieval \
        --tasks evaluation/finance_tasks_complex.jsonl --top-k 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.block_index import companies_for_question, periods_for_question
from app.finance.store import FinanceStore
from app.tools.finance import FinanceToolbox
from evaluation import structured_corpus
from evaluation._runner_common import load_tasks
from evaluation.finance_corpus import DOCUMENT_DIR


def _sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _view(hit: dict) -> dict:
    return {
        "chunk_id": str(hit.get("chunk_id") or hit.get("block_id") or ""),
        "doc_id": hit.get("doc_id"),
        "doc_code": hit.get("doc_code"),
        "company": hit.get("company"),
        "period": str(hit.get("period") or ""),
        "source_id": hit.get("source_id"),
        "table_id": hit.get("table_id"),
        "row_indices": hit.get("row_indices"),
        "column_indices": hit.get("column_indices"),
        "content_sha256": _sha256(hit.get("content") or ""),
        "score": hit.get("score"),
    }


def _ordered_equal(left: list[dict], right: list[dict], key: str) -> bool:
    return [item.get(key) for item in left] == [item.get(key) for item in right]


def compare_question(toolbox: FinanceToolbox, question: str, top_k: int) -> dict:
    doc_codes = structured_corpus.docs_for_question(question)
    companies = companies_for_question(question)
    periods = periods_for_question(question)
    agent_hits = toolbox.search_blocks(question, top_k=top_k)
    rag_hits = structured_corpus.search(question, top_k=top_k, doc_codes=doc_codes)
    agent = [_view(hit) for hit in agent_hits]
    rag = [_view(hit) for hit in rag_hits]

    mismatches: list[str] = []
    if not _ordered_equal(agent, rag, "chunk_id"):
        mismatches.append("ordered_chunk_ids")
    if not _ordered_equal(agent, rag, "content_sha256"):
        mismatches.append("ordered_content")
    if [item.get("score") for item in agent] != [item.get("score") for item in rag]:
        mismatches.append("scores")

    filtered = []
    for item in agent + rag:
        if doc_codes and item.get("doc_code") not in doc_codes:
            filtered.append(f"doc_code:{item.get('chunk_id')}")
        if companies and item.get("company") not in companies:
            filtered.append(f"company:{item.get('chunk_id')}")
        if periods and item.get("period") not in periods:
            filtered.append(f"period:{item.get('chunk_id')}")
    if filtered:
        mismatches.append("filter_leak")

    return {
        "question": question,
        "doc_codes": doc_codes,
        "companies": companies,
        "periods": sorted(periods) if periods else None,
        "agent_order": [item["chunk_id"] for item in agent],
        "rag_order": [item["chunk_id"] for item in rag],
        "agent_content": [item["content_sha256"] for item in agent],
        "rag_content": [item["content_sha256"] for item in rag],
        "mismatches": mismatches,
        "ok": not mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="结构索引检索一致性验收")
    parser.add_argument("--tasks", default="evaluation/finance_tasks_complex.jsonl")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--out", default="", help="结果 JSON 路径；空 = 不落盘")
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    if not tasks_path.exists():
        raise SystemExit(f"任务文件不存在: {tasks_path}")

    tasks = [task for task in load_tasks(tasks_path) if not task.get("holdout")]
    if args.limit > 0:
        tasks = tasks[: args.limit]
    if not tasks:
        raise SystemExit("筛完没有任务可跑")

    store = FinanceStore(DOCUMENT_DIR)
    toolbox = FinanceToolbox(store)
    # Agent 工具层把 search_blocks 夹在 4–5；验收必须用同一个有效 top_k。
    top_k = max(4, min(int(args.top_k), 5))
    if top_k != args.top_k:
        print(f"top_k={args.top_k} 超出 Agent 工具上限，按 {top_k} 比较")
    index_stats = toolbox.block_index.stats()
    checks = [
        compare_question(toolbox, str(task.get("question") or ""), top_k=top_k)
        for task in tasks
    ]
    failed = [check for check in checks if not check["ok"]]

    print(f"索引：{index_stats}")
    print(f"问题 {len(checks)} 条，通过 {len(checks) - len(failed)} 条，失败 {len(failed)} 条")
    for check in failed[:5]:
        print(f"! {check['question'][:40]} -> {check['mismatches']}")
        print(f"  agent: {check['agent_order']}")
        print(f"  rag:   {check['rag_order']}")

    payload = {
        "kind": "block_retrieval_parity",
        "tasks_file": str(tasks_path),
        "top_k": top_k,
        "index": index_stats,
        "passed": len(checks) - len(failed),
        "total": len(checks),
        "checks": checks,
    }
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"结果：{out_path}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
