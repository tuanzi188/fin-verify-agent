"""产品级结构感知 RAG：简单查数的一次检索、一次生成。

与评测侧 `evaluation.rag_baseline` 的区别：

- 检索直接调用线上共享的 `BlockIndex`，与 Agent 的 `search_blocks` 同源；
- 证据转成 `EvidenceRef`，前端仍能定位表格行列、页码、单位；
- 只有一次 LLM 调用，不提供工具、不计算、不多轮；
- 固定窗 RAG 不在这里，产品也不会路由到它。

它回答不了的问题应该升级到 Agent；上游由 `finance_router.decide_strategy` 决定。
"""

from __future__ import annotations

import logging
import re
import time

from app.config import MODEL_CONFIG
from app.models.finance import FinanceChatResponse, FinanceCitation
from app.services import model_provider
from app.services.agent_trace import to_trace
from app.services.conversation_memory import conversation_memory
from app.services.finance_corpus import get_block_index, get_finance_store
from app.services.finance_entry import build_claims, build_evidence
from app.services.finance_history import build_history_payload
from app.tools.contract import ABSTAIN_KEYWORDS

logger = logging.getLogger(__name__)

TOP_K = 6
MAX_EVIDENCE_CHARS = 1800
MAX_ANSWER_TOKENS = 2048
TEMPERATURE = 0.1

STRUCTURED_RAG_SYSTEM = """你是金融文档查证助手。下面是一次结构检索返回的证据块，
你不能再检索、不能调用计算器，也没有别的工具。

规则：
1. 只依据证据回答；证据里没有的数字、单位、口径一律不要写。
2. 每条结论后用 [n] 标注来源证据编号。
3. 表格证据要写清表名/页码/单位，能定位到行列时写出列名或行名。
4. 不要心算、换算或推导派生比率；需要计算时直接说明"需要多步查证"。
5. 证据不足以回答时，直接说"根据现有资料无法确定"，不要猜测。
6. 问题缺少关键条件时，先把要确认的点问出来。

证据：
{evidence}

问题：{question}

请给出简洁结论；如果只是单点读数，不要写检索过程。"""

_MARKER_PATTERN = re.compile(r"[\[【]\s*(\d{1,2})(?=[，,、\s\]】])")


def _render_evidence(citations: list[FinanceCitation]) -> str:
    blocks: list[str] = []
    for citation in citations:
        evidence = citation.evidence
        header = f"[{citation.marker}] {evidence.doc_title or evidence.doc_id}"
        if evidence.page:
            header += f"｜PDF 第 {evidence.page} 页"
        if evidence.table_id:
            header += f"｜表 {evidence.table_id}"
        if evidence.row_indices:
            header += f"｜行 {evidence.row_indices}"
        if evidence.column_indices:
            header += f"｜列 {evidence.column_indices}"
        if evidence.unit:
            header += f"｜单位 {evidence.unit}"
        content = evidence.content[:MAX_EVIDENCE_CHARS]
        blocks.append(f"{header}\n{content}")
    return "\n\n".join(blocks)


def _leading_line(answer: str) -> str:
    for raw in (answer or "").splitlines():
        line = re.sub(r"^#{1,6}\s*", "", raw.strip()).strip()
        if line:
            return line
    return ""


def _decide_status(answer: str, citations: list[FinanceCitation]) -> str:
    if not answer.strip():
        return "error"
    if not citations:
        return "abstained"
    if any(keyword in _leading_line(answer) for keyword in ABSTAIN_KEYWORDS):
        return "abstained"
    return "answered"


def _used_citations(answer: str, citations: list[FinanceCitation]) -> list[FinanceCitation]:
    used = {int(value) for value in _MARKER_PATTERN.findall(answer or "")}
    if not used:
        return citations
    return [citation for citation in citations if citation.marker in used]


async def run_structured_rag(
    *,
    query: str,
    conversation_id: str,
    user_context=None,
    route_reason: str = "",
    corpus_fingerprint: str = "",
) -> FinanceChatResponse:
    """简单查数：一次结构检索 + 一次生成。"""
    store = get_finance_store()
    started_at = time.perf_counter()

    retrieve_started_at = time.perf_counter()
    hits = get_block_index().search(query, top_k=TOP_K)
    retrieve_ms = int((time.perf_counter() - retrieve_started_at) * 1000)

    citations: list[FinanceCitation] = []
    for index, hit in enumerate(hits, 1):
        block_id = str(hit.get("block_id") or "")
        if not block_id:
            continue
        try:
            evidence = build_evidence(store, block_id, hit)
        except Exception as exc:  # noqa: BLE001 - 单个证据坏掉不能拖垮整次查数
            logger.warning("structured rag evidence failed: block=%s err=%s", block_id, exc)
            continue
        citations.append(FinanceCitation(marker=index, evidence=evidence))

    trace: list[dict] = [
        {
            "step": 1,
            "tool": "search_blocks",
            "args": {"query": query, "top_k": TOP_K},
            "ok": bool(hits),
            "result_ids": [str(hit.get("block_id") or "") for hit in hits],
            "result_count": len(hits),
            "new_result_count": len(hits),
            "latency_ms": retrieve_ms,
            "result_chars": sum(len(str(hit.get("content") or "")) for hit in hits),
        }
    ]

    if not citations:
        trace.append(
            {
                "step": 2,
                "tool": "__final__",
                "ok": False,
                "result_count": 0,
                "latency_ms": 0,
                "note": "retrieval_empty",
            }
        )
        response = FinanceChatResponse(
            status="abstained",
            answer="根据现有资料无法确定：本次结构检索没有找到可用证据。",
            citations=[],
            strategy="structured_rag",
            route_reason=route_reason,
            search_mode="blocks",
            conversation_id=conversation_id,
            model_used=MODEL_CONFIG["flash"]["model"],
            variant="structured_rag",
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            trace=to_trace(trace),
            notes=["retrieval_empty"],
        )
        # 拒答也要进历史：重新打开时能看到“查过但没证据”，而不是一条空回答。
        await conversation_memory.add_turn(
            conversation_id,
            query,
            response.answer,
            payload=build_history_payload(response, corpus_fingerprint=corpus_fingerprint),
        )
        return response

    prompt = STRUCTURED_RAG_SYSTEM.format(
        evidence=_render_evidence(citations),
        question=query,
    )
    messages = [
        {"role": "system", "content": "你是严谨的金融文档查证助手，只依据证据回答。"},
        {"role": "user", "content": prompt},
    ]

    answer = ""
    usage: dict = {}
    failed = False
    llm_started_at = time.perf_counter()
    try:
        answer, _, usage = await model_provider.chat(
            messages,
            reasoning_mode="flash",
            temperature=TEMPERATURE,
            max_tokens=MAX_ANSWER_TOKENS,
        )
        answer = (answer or "").strip()
    except Exception as exc:  # noqa: BLE001 - 返回 error 状态，不丢 HTTP 契约
        failed = True
        logger.exception("structured rag generation failed")
        answer = f"结构检索已找到证据，但生成失败：{type(exc).__name__}"
    llm_ms = int((time.perf_counter() - llm_started_at) * 1000)

    if failed:
        status = "error"
        used_citations: list[FinanceCitation] = []
    else:
        used_citations = _used_citations(answer, citations)
        status = _decide_status(answer, used_citations)

    claims = build_claims(answer, used_citations, []) if status != "error" and answer else []

    tokens_in = int((usage or {}).get("prompt_tokens") or 0)
    tokens_out = int((usage or {}).get("completion_tokens") or 0)
    trace.append(
        {
            "step": 2,
            "tool": "__final__",
            "ok": not failed,
            "result_count": 0,
            "latency_ms": llm_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "note": "" if status == "answered" else f"status:{status}",
        }
    )

    response = FinanceChatResponse(
        status=status,
        answer=answer,
        claims=claims,
        citations=used_citations,
        calc=[],
        trace=to_trace(trace),
        conversation_id=conversation_id,
        model_used=MODEL_CONFIG["flash"]["model"],
        variant="structured_rag",
        strategy="structured_rag",
        route_reason=route_reason,
        search_mode="blocks",
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        token_usage={
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
            "llm_calls": 1,
            "final_context_tokens": tokens_in,
        },
        notes=["structured_rag:top_k=6"],
    )
    if status != "error" and answer:
        await conversation_memory.add_turn(
            conversation_id,
            query,
            answer,
            payload=build_history_payload(response, corpus_fingerprint=corpus_fingerprint),
        )
    return response
