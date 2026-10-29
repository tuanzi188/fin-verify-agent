"""普通 RAG 基线臂：一次检索（top-k）→ 一次生成，没有多轮、没有表格结构、没有计算器。

这是「Agent 多花的钱到底买了什么」的对照组。三条设计纪律：

1. **只给一次机会**：检索一次、生成一次。它不能换关键词、不能重取、不能发现
   自己检索错了——这正是 Agent 花额外 token 想买的东西。
2. **不给结构化工具**：没有 get_cell、没有 calculate、没有页码；证据单位是
   512 字固定窗，表格在切片时已经被拍平。
3. **同一个模型、同一份语料、同一批问题**：模型与温度从 `app.config` 走，
   与 Agent 臂同源；语料来自同一批 document.json。

指标口径与 Agent 臂一致（同一套 `_extract_citations`、同一套 status 判定），
所以两边可以直接按 task_id 配对比较。
"""

from __future__ import annotations

import time
from typing import Any

from app.agent.runner import _decide_status, _extract_citations
from app.services.deepseek import deepseek_client
from evaluation import rag_corpus

#: 基线每次问只喂这么多片段。8 × 512 字 ≈ 4k 字符 ≈ 2.5k token，
#: 是"便宜但不算糊弄"的常见档位；调大就是加钱，比较时要连成本一起看。
TOP_K = 8
#: 与 Agent 臂的 FINAL_MAX_TOKENS 对齐（2048）。给低了会把"被 max_tokens 截空"
#: 记成基线答不出——那是量具问题，不是能力差。实测 1200 时 CX-05/CX-09 各烧满
#: 1200 token 的 reasoning 后交白卷。
MAX_ANSWER_TOKENS = 2048

RAG_SYSTEM = """你是金融文档问答助手。下面是从本地文档里检索出的片段，你没有别的工具。

规则：
1. 只依据片段回答。片段里没有的数字、单位、口径一律不要写。
2. 每条结论后用 [n] 标注来源片段编号。
3. 金额保持片段里的原始写法与单位；不要自行换算，也不要心算比率。
4. 片段不足以回答时，直接说"根据现有资料无法确定"，不要猜测。
5. 问题缺少关键条件（哪家公司 / 哪一年 / 哪个口径）时，先把要确认的点问出来。
6. 不要在回答里复述检索过程。"""


def _render_evidence(hits: list[dict]) -> str:
    blocks = []
    for index, hit in enumerate(hits, 1):
        blocks.append(f"[{index}] {hit.get('title') or ''}\n{hit.get('content') or ''}")
    return "\n\n".join(blocks)


def _to_evidence(hits: list[dict]) -> list[dict]:
    return [
        {
            "chunk_id": str(hit.get("chunk_id") or ""),
            "title": str(hit.get("title") or ""),
            "content": str(hit.get("content") or ""),
            "kind": "chunk",
            "page": hit.get("page"),
        }
        for hit in hits
    ]


async def answer_once(
    task: dict,
    *,
    run_index: int = 1,
    top_k: int = TOP_K,
    model: Any = None,
    variant: str = "rag",
) -> dict:
    """跑一条任务：检索 top-k → 一次生成 → 解析引用。"""
    model = model or deepseek_client
    started_at = time.perf_counter()
    task_id = str(task.get("id"))
    question = str(task.get("question") or "")
    notes: list[str] = []

    hits = rag_corpus.search(
        question,
        top_k=top_k,
        doc_codes=rag_corpus.docs_for_question(question),
    )
    retrieve_ms = int((time.perf_counter() - started_at) * 1000)
    evidence = _to_evidence(hits)
    if not hits:
        notes.append("retrieval_empty")

    trace = [
        {
            "step": 1,
            "tool": "search_chunks",
            "args": {"query": question, "top_k": top_k},
            "ok": bool(hits),
            "result_ids": [hit["chunk_id"] for hit in hits],
            "result_count": len(hits),
            "injected": None,
            "latency_ms": retrieve_ms,
            "tokens_in": 0,
            "tokens_out": 0,
        }
    ]

    usage = {"steps": 1, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "model_calls": 1}
    answer = ""
    failed = False
    final_meta: dict = {}

    messages = [
        {"role": "system", "content": RAG_SYSTEM},
        {
            "role": "user",
            "content": f"问题：{question}\n\n检索到的片段：\n\n{_render_evidence(hits)}\n\n"
            "请只依据以上片段回答，并用 [n] 标注来源。",
        },
    ]
    try:
        answer, _, call_usage = await model.chat(
            messages,
            temperature=0.1,
            max_tokens=MAX_ANSWER_TOKENS,
        )
        final_meta = call_usage or {}
        usage["tokens_in"] = int(final_meta.get("prompt_tokens") or 0)
        usage["tokens_out"] = int(final_meta.get("completion_tokens") or 0)
    except Exception as exc:  # noqa: BLE001 - 崩溃要落成 error 而不是丢记录
        failed = True
        notes.append(f"run_error: {type(exc).__name__}: {exc}")

    answer = str(answer or "").strip()
    citations = _extract_citations(answer, evidence)
    if answer and not citations and task.get("citation_required"):
        notes.append("citation_missing")
    # 基线没有模型侧工具调用：executed=0，澄清判定才认"这一轮没动手只提问"
    status = _decide_status(
        answer=answer,
        executed=0,
        clarify_mode=str(task.get("clarify_mode") or "none"),
        failed=failed,
    )

    usage["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
    final_latency = max(usage["latency_ms"] - retrieve_ms, 0)
    final_step = {
        "step": 2,
        "tool": "__final__",
        "args": {},
        "ok": not failed,
        "result_ids": [],
        "result_count": 0,
        "injected": None,
        "latency_ms": final_latency,
        "tokens_in": usage["tokens_in"],
        "tokens_out": usage["tokens_out"],
    }
    if final_meta.get("finish_reason"):
        final_step["finish_reason"] = final_meta["finish_reason"]
    if notes:
        final_step["note"] = "；".join(notes)
    trace.append(final_step)

    return {
        "task_id": task_id,
        "variant": variant,
        "run_index": run_index,
        "status": status,
        "answer": answer,
        "citations": citations,
        "trace": trace,
        "usage": usage,
        "evidence": evidence,
        "retrieved": [
            {"chunk_id": hit["chunk_id"], "source_id": hit["source_id"], "score": hit["score"]}
            for hit in hits
        ],
    }
