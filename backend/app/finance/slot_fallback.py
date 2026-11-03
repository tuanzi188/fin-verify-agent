"""规则判不了的查询，用一次闭集槽位抽取兜底（探针用，默认不接产品路径）。

背景：`routing_policy` 的规则层对 52 条任务题 0 误杀，但对两类场景仍靠关键词
兜底——相对期间（"最新一期/去年"）和口语化指代。本模块把"要查什么"和
"是否可查证"合并成一次小调用：输出必须是 JSON，槽位做闭集校验（公司必须
命中语料别名表、年份必须是 20XX）；校验失败即返回 None，维持规则结论。

三条纪律：
1. **不产出布尔 in/out**——`out_of_scope_reason` 是枚举，上层按原因选话术；
2. **路由仍由规则决定**——LLM 只补槽位，补完的查询交回 `initial_decision`，
   路径、边界、澄清全部可复现；
3. 只允许在规则给出 clarify/abstain 且 rule_id 在 `SLOT_FALLBACK_RULE_IDS`
   时触发，产品接线点由 `api/finance.py` 决定（当前未接线）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.finance.query_scope import scope_companies, scope_company_aliases, scope_years
from app.finance.routing_policy import InitialDecision, initial_decision

SLOT_RULE_VERSION = "slot-fallback-v1"
#: 允许被槽位兜底改写的规则结论。观点/行情（out_of_scope_intent）与主体越界
#: （scope_boundary）不在内：前者已由确定性规则覆盖，后者是语料事实，不应由模型推翻。
SLOT_FALLBACK_RULE_IDS = frozenset({"out_of_scope", "missing_scope"})

OUT_OF_SCOPE_REASONS = frozenset(
    {
        "none",
        "opinion",
        "forecast",
        "market_data",
        "uncovered_entity",
        "off_topic",
        "chit_chat",
    }
)
INTENTS = frozenset({"lookup", "compare", "calculate", "reconcile", "clarify"})

_REASON_MESSAGES = {
    "opinion": "本产品只查证定期报告中的披露数字与原文，不提供观点或投资建议。",
    "forecast": "本产品不做预测，只能查证已披露的报告期数字；请点名报告期和指标。",
    "market_data": "本产品只有定期报告语料，没有行情数据；请改问年报或募集说明书中的披露项。",
    "uncovered_entity": "本地语料未覆盖该主体，请提供对应材料后再查证。",
    "off_topic": "这里只查证本地财报材料；请提供公司、报告期或具体指标。",
    "chit_chat": "这里只查证本地财报材料；请提供公司、报告期或具体指标。",
}

#: 指令、schema 与闭集规则全部放在单条 user 消息里。实测（2026-11-03）：
#: 该网关模型（cn:deepseek-v4.1-flash）对 system 角色的 JSON 约束跟随不可靠
#: ——会把抽取任务当成问答直接开答；user 消息里给全 schema 才能稳定输出。
SLOT_INSTRUCTION_TEMPLATE = """只做槽位抽取，不要回答下面的问题，不要输出 JSON 以外的任何内容，不要输出约定之外的键。

输出 schema：
{{"companies":[],"years":[],"metric":"","intent":"lookup","out_of_scope_reason":"none"}}

规则：
- companies 只能取：{companies}；没点名就给空列表
- years 是四位年份字符串；"最新一期"用 {latest}；去年/前年按当前年份 {current} 推算
- intent 只能是 lookup/compare/calculate/reconcile/clarify
- out_of_scope_reason 只能是 none/opinion/forecast/market_data/uncovered_entity/off_topic/chit_chat

上一轮问题：{previous}
本轮问题：{query}"""


@dataclass(frozen=True)
class SlotExtraction:
    companies: tuple[str, ...] = ()
    years: tuple[str, ...] = ()
    metric: str = ""
    intent: str = "lookup"
    out_of_scope_reason: str = "none"
    raw: str = ""
    usage: dict = field(default_factory=dict)


def _corpus_coverage(documents: list[dict]) -> tuple[list[str], list[str]]:
    companies = sorted(
        {str(document.get("company") or "").strip() for document in documents}
        - {""}
    )
    years = sorted(
        {
            year
            for document in documents
            for year in scope_years(str(document.get("period") or ""))
        }
    )
    return companies, years


def _json_object(text: str) -> dict | None:
    """从模型输出里取出第一个 JSON 对象；容忍 ```json 围栏与前后废话。"""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = text[start : end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_slots(raw: str, documents: list[dict]) -> SlotExtraction | None:
    """闭集校验：公司必须映射到语料别名、年份必须是四位、枚举必须合法。

    任何一项不合法都返回 None（维持规则结论）——宁可退回澄清，也不让模型
    编出来的主体或期间进入检索。
    """
    payload = _json_object(raw)
    if payload is None:
        return None
    aliases = scope_company_aliases(documents)
    companies: list[str] = []
    for item in payload.get("companies") or []:
        name = str(item or "").strip()
        if not name:
            continue
        canonical = aliases.get(name)
        if canonical is None:
            # 允许"全称里包含别名"的写法，但不接受凭空主体。
            hits = [company for alias, company in aliases.items() if alias in name]
            if not hits:
                return None
            canonical = hits[0]
        if canonical not in companies:
            companies.append(canonical)
    years: list[str] = []
    for item in payload.get("years") or []:
        year = str(item or "").strip()
        if not re.fullmatch(r"20\d{2}", year):
            return None
        if year not in years:
            years.append(year)
    if len(years) > 3:
        return None
    reason = str(payload.get("out_of_scope_reason") or "none").strip()
    if reason not in OUT_OF_SCOPE_REASONS:
        return None
    intent = str(payload.get("intent") or "lookup").strip()
    if intent not in INTENTS:
        return None
    metric = str(payload.get("metric") or "").strip()
    return SlotExtraction(
        companies=tuple(companies),
        years=tuple(sorted(years)),
        metric=metric,
        intent=intent,
        out_of_scope_reason=reason,
        raw=raw,
    )


async def extract_slots(
    query: str,
    documents: list[dict],
    previous_query: str = "",
    *,
    max_tokens: int = 800,
) -> SlotExtraction | None:
    """一次小调用做闭集槽位抽取；通道异常或校验失败返回 None。

    预算要覆盖"思维链 + JSON 输出"：网关的 flash 模型带推理，给 300 会
    全花在思考上、content 为空（实测 finish_reason=length）。
    """
    from app.services import model_provider

    companies, years = _corpus_coverage(documents)
    instruction = SLOT_INSTRUCTION_TEMPLATE.format(
        companies="、".join(companies) or "（空）",
        latest=years[-1] if years else "",
        current=datetime.now().year,
        previous=previous_query or "无",
        query=query,
    )
    messages = [{"role": "user", "content": instruction}]
    try:
        content, _reasoning, usage = await model_provider.chat(
            messages, temperature=0.0, max_tokens=max_tokens
        )
    except Exception:  # noqa: BLE001 - 兜底通道失败必须退回规则结论
        return None
    slots = parse_slots(content, documents)
    if slots is None:
        return None
    return SlotExtraction(
        companies=slots.companies,
        years=slots.years,
        metric=slots.metric,
        intent=slots.intent,
        out_of_scope_reason=slots.out_of_scope_reason,
        raw=content,
        usage=usage or {},
    )


def enrich_query(query: str, slots: SlotExtraction, documents: list[dict]) -> str:
    """把槽位补进原句：只补原句缺的主体/期间/指标，不重写原话。"""
    enriched = query.strip()
    if slots.companies and not scope_companies(enriched, documents):
        enriched = "、".join(slots.companies) + " " + enriched
    if slots.years and not scope_years(enriched):
        enriched = f"{slots.years[-1]} 年 " + enriched
    if slots.metric and slots.metric not in enriched:
        enriched += "；查询指标：" + slots.metric
    return enriched


def fallback_decision(
    query: str,
    documents: list[dict],
    slots: SlotExtraction,
    previous_query: str = "",
) -> InitialDecision | None:
    """槽位兜底的最终结论：域外按枚举给边界说明；可查证则交回规则重路由。

    返回 None 表示兜底没有带来改善，调用方维持原规则结论。
    """
    if slots.out_of_scope_reason != "none":
        return InitialDecision(
            "clarify",
            _REASON_MESSAGES.get(
                slots.out_of_scope_reason, _REASON_MESSAGES["off_topic"]
            ),
            "slot_boundary",
            query,
        )
    enriched = enrich_query(query, slots, documents)
    if enriched == query.strip():
        return None
    decision = initial_decision(enriched, documents, previous_query)
    return InitialDecision(
        decision.initial_path,
        decision.reason,
        decision.rule_id,
        enriched,
    )
