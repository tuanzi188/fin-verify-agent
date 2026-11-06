"""拒答分级：把「为什么没给答案」收敛成六类，每类带一个可执行的下一步。

为什么需要分级：一句「材料不足」把三种完全不同的情况说成同一件事——
用户问的是天气（产品不干这个）、语料里没有这家公司（补材料就行）、
检索到了但同一指标出现两个候选值（要人工核对）。这三种的下一步动作不同，
混在一起写就成了万金油提示，用户读完不知道该干什么。

分级不是严重程度，而是**谁能把这一轮救回来**：

- L1 可继续：用户补一句来源/期间/口径就能重问（谁：用户）
- L2 产品边界：产品不提供这类判断，改问披露项才有答案（谁：无）
- L3 证据缺口：缺的是资料或需人工核对（谁：补语料 / 人工）

这里只定义分类与文案，不判断"该不该拒答"——路由与检索层已经做完了，
本模块负责在已有结论上贴一个稳定、可统计的标签。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RefusalClass:
    code: str
    label: str
    level: int
    action: str  # clarify = 追问后可能可答；refuse = 这一轮不给答案
    recover_by: str  # user / corpus / manual / none
    hint: str


#: 证据冲突类 issue：有候选但不敢给——和"没检索到"是两件事，不能合并。
CONFLICT_ISSUES = frozenset(
    {
        "candidate_conflict",
        "numeric_mismatch",
        "unit_missing",
        "citation_missing",
        "scope_ambiguous",
    }
)

LEVEL_LABELS = {1: "可继续", 2: "产品边界", 3: "证据缺口"}

REFUSALS: dict[str, RefusalClass] = {
    "ambiguous_scope": RefusalClass(
        code="ambiguous_scope",
        label="条件不完整",
        level=1,
        action="clarify",
        recover_by="user",
        hint="补一句公司、报告期或指标口径即可继续查证。",
    ),
    "out_of_domain": RefusalClass(
        code="out_of_domain",
        label="不在查证范围",
        level=2,
        action="refuse",
        recover_by="none",
        hint="这里只查定期报告里的披露事实；换成「公司 + 年份 + 指标」的问法。",
    ),
    "unsupported_intent": RefusalClass(
        code="unsupported_intent",
        label="不可查证的判断",
        level=2,
        action="refuse",
        recover_by="none",
        hint="行情、估值、涨跌预测与买卖建议没有可回查的原件，改问具体披露项。",
    ),
    "out_of_scope": RefusalClass(
        code="out_of_scope",
        label="语料未覆盖",
        level=3,
        action="refuse",
        recover_by="corpus",
        hint="本地没有这家公司/这个期间的材料；可先入库对应年报，或开启联网综述看公开说法。",
    ),
    "insufficient_evidence": RefusalClass(
        code="insufficient_evidence",
        label="证据不足",
        level=3,
        action="refuse",
        recover_by="corpus",
        hint="材料里没有检索到这一项；确认指标名称与披露口径，或补充对应期间的报告。",
    ),
    "source_conflict": RefusalClass(
        code="source_conflict",
        label="候选冲突",
        level=3,
        action="refuse",
        recover_by="manual",
        hint="同一指标出现不同候选值或单位，需人工核对表与口径后再引用。",
    ),
}

_LEVEL_ACTION_LABELS = {
    (1, "clarify"): "需要补充条件",
    (2, "refuse"): "不在产品范围",
    (3, "refuse"): "证据不够",
}


def get(code: str) -> RefusalClass:
    return REFUSALS[code]


def is_known(code: str) -> bool:
    return code in REFUSALS


def payload(code: str, *, detail: str = "") -> dict[str, Any]:
    """返回给前端/历史的稳定结构；未知 code 退回「证据不足」而不是丢字段。"""
    item = REFUSALS.get(code) or REFUSALS["insufficient_evidence"]
    return {
        "code": item.code,
        "label": item.label,
        "level": item.level,
        "level_label": LEVEL_LABELS[item.level],
        "action": item.action,
        "action_label": _LEVEL_ACTION_LABELS.get(
            (item.level, item.action), "拒答"
        ),
        "recover_by": item.recover_by,
        "hint": item.hint,
        "detail": detail,
    }


def from_rule_id(rule_id: str) -> str:
    """路由规则的 rule_id → 拒答类别。不在表里的（放行规则）返回空串。"""
    return {
        "out_of_scope": "out_of_domain",
        "missing_scope": "ambiguous_scope",
        "ambiguous_metric": "ambiguous_scope",
        "out_of_scope_intent": "unsupported_intent",
    }.get(rule_id, "")


def classify_issues(issues: list[dict[str, Any]]) -> str:
    """已有证据链的 issue → 拒答类别。

    冲突优先于缺失：只要存在"有候选但对不上"，就不该说成"没查到"。
    """
    codes = {str(issue.get("code") or "") for issue in issues}
    if codes & CONFLICT_ISSUES:
        return "source_conflict"
    return "insufficient_evidence"


def from_issues(issues: list[dict[str, Any]], *, detail: str = "") -> dict[str, Any]:
    return payload(classify_issues(issues), detail=detail)


def detail_from_issues(issues: list[dict[str, Any]]) -> str:
    return "；".join(
        dict.fromkeys(
            str(issue.get("reason") or "").strip()
            for issue in issues
            if str(issue.get("reason") or "").strip()
        )
    )


def from_verification(verification: dict[str, Any] | None) -> dict[str, Any] | None:
    """回答生成后仍为 abstained 时补一个类别；没有 issue 时返回 None，不硬造。"""
    verification = verification or {}
    issues = list(verification.get("issues") or [])
    evidence_check = verification.get("evidence_check") or {}
    issues.extend(evidence_check.get("issues") or [])
    if not issues:
        return None
    return from_issues(issues, detail=detail_from_issues(issues))
