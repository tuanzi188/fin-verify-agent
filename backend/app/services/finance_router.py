"""产品查证选路：简单查数走结构感知 RAG，复杂查证走 Agent。

这不是评测口径，而是产品默认路径。规则刻意保持可解释、可测试：

- 单公司 / 单期间 / 单指标的“读数”走一次结构检索；
- 比较、计算、跨文档核对、多子问题、未来期间（拒答边界）走多步 Agent。

固定窗 RAG 不在产品路由里出现。它只保留在离线评测，作为可选辅助基线。
"""

from __future__ import annotations

import re

from app.finance.block_index import companies_for_question
from app.finance.store import FinanceStore

#: 需要多步/计算的信号。命中任一就升级到 Agent。
COMPLEX_SIGNALS = (
    "对比",
    "比较",
    "差异",
    "是否一致",
    "是否相同",
    "勾稽",
    "占比",
    "比重",
    "占营业",
    "毛利率",
    "净利率",
    "同比",
    "环比",
    "增长",
    "下降",
    "百分点",
    "倍",
    "合计",
    "汇总",
    "分别",
    "两份",
    "跨文档",
    "计算",
    "推算",
    "排名",
    "最高",
    "最低",
    "哪个高",
    "哪个低",
    "相差",
)

_YEAR_PATTERN = re.compile(r"(20\d{2})\s*年")


def decide_strategy(query: str, store: FinanceStore) -> tuple[str, str]:
    """返回 (strategy, reason)。strategy ∈ {structured_rag, agent}。"""
    text = str(query or "").strip()
    if not text:
        return "structured_rag", "空问题按一次结构检索处理"

    years = sorted({int(value) for value in _YEAR_PATTERN.findall(text)})
    available = sorted(
        {
            int(str(item.get("period")))
            for item in store.list_documents()
            if str(item.get("period") or "").isdigit()
        }
    )
    if years and available and max(years) > max(available):
        return (
            "agent",
            f"问题点名了本地未覆盖的期间（本地最新 {max(available)} 年），需要拒答边界",
        )

    companies = companies_for_question(text)
    if len(companies) >= 2:
        return "agent", "问题涉及多家公司，需要跨文档核对"

    # 只统计问题里明确写出的年份；“2025 年报”只有一个期间，不应升级。
    if len(years) >= 2:
        return "agent", "问题涉及多个报告期，需要跨期间比较"

    matched = [signal for signal in COMPLEX_SIGNALS if signal in text]
    if matched:
        return "agent", f"问题包含比较/计算信号：{'、'.join(matched[:3])}"

    if text.count("？") + text.count("?") > 1:
        return "agent", "问题包含多个子问题，需要多步拆解"

    return "structured_rag", "单公司/单期间/单指标的查数，一次结构检索即可"
