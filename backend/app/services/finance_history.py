"""会话历史里的金融查证载荷。

只保存重建界面必需的字段：结论要点、引用坐标、计算记录、轨迹摘要。
证据正文 `content` 与表格预览 `table_preview` 不落库——一张 200 行的表预览
就有几十 KB，而这两项都能按 `evidence_id` 从 `/finance/evidence` 回查。

契约与 `/finance/chat` 的响应保持一致，前端可以直接复用 `FinanceMessagePayload`；
`kind="finance"` 用于区分旧 RAG 轮次（payload 为 None）。
"""

from __future__ import annotations

from typing import Any

from app.models.finance import FinanceChatResponse, FinanceCitation

PAYLOAD_KIND = "finance"
TRACE_RESULT_ID_LIMIT = 12


def _compact_citation(citation: FinanceCitation) -> dict[str, Any]:
    evidence = citation.evidence
    return {
        "marker": citation.marker,
        "evidence": {
            "evidence_id": evidence.evidence_id,
            "doc_id": evidence.doc_id,
            "doc_title": evidence.doc_title,
            "company": evidence.company,
            "period": evidence.period,
            "doc_type": evidence.doc_type,
            "kind": evidence.kind.value,
            "page": evidence.page,
            "section_id": evidence.section_id,
            "path": list(evidence.path),
            # 页码、表名、行列号是“重新打开还能核对”的最小坐标，必须保留。
            "table_id": evidence.table_id,
            "table_caption": evidence.table_caption,
            "row_indices": list(evidence.row_indices),
            "column_indices": list(evidence.column_indices),
            "row_name": evidence.row_name,
            "column": evidence.column,
            "cell_text": evidence.cell_text,
            "unit": evidence.unit,
            "footnote": evidence.footnote,
            "verified": evidence.verified,
            "note": evidence.note,
            "source_url": evidence.source_url,
        },
    }


def _compact_trace(response: FinanceChatResponse) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in response.trace:
        data = step.model_dump(mode="json")
        result_ids = data.get("result_ids")
        if isinstance(result_ids, list) and len(result_ids) > TRACE_RESULT_ID_LIMIT:
            data["result_ids"] = result_ids[:TRACE_RESULT_ID_LIMIT]
            data["result_ids_truncated_in_history"] = True
        steps.append(data)
    return steps


def build_history_payload(
    response: FinanceChatResponse,
    *,
    corpus_fingerprint: str = "",
) -> dict[str, Any]:
    """把一次金融查证响应压成可持久化的会话载荷。

    `corpus_fingerprint` 由入口层传入（它掌握当前语料），重开会话时和
    `/finance/stats.index_fingerprint` 比对，就能判断证据是否还对应同一版语料。
    """
    return {
        "kind": PAYLOAD_KIND,
        "status": response.status,
        "strategy": response.strategy,
        "route_reason": response.route_reason,
        "search_mode": response.search_mode,
        "variant": response.variant,
        "model_used": response.model_used,
        "duration_ms": response.duration_ms,
        "corpus_fingerprint": corpus_fingerprint,
        "claims": [claim.model_dump(mode="json") for claim in response.claims],
        "citations": [_compact_citation(item) for item in response.citations],
        "calc": [record.model_dump(mode="json") for record in response.calc],
        "trace": _compact_trace(response),
        "notes": list(response.notes),
        "verification": response.verification,
        "resolved_query": response.resolved_query,
    }
