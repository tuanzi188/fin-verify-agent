"""旧两臂调用方的路由适配；正式接口使用四路决策。"""

from __future__ import annotations

from app.finance.routing_policy import initial_decision
from app.finance.store import FinanceStore


def decide_strategy(query: str, store: FinanceStore) -> tuple[str, str]:
    """保留旧评测的两臂契约；澄清与拒答由正式入口直接处理。"""
    decision = initial_decision(query, store.list_documents())
    strategy = (
        decision.initial_path
        if decision.initial_path in ("agent", "structured_rag")
        else "structured_rag"
    )
    return strategy, decision.reason
