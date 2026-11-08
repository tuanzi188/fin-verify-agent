"""辅助问答的可选语义判分。

确定性检查只能判"命中词在不在"和"数字对不对"，判不了：
- 定义是否真的成立；
- 同义改写是否等价；
- 禁止内容是否换了说法。

所以这里给一个轻量 judge，只判三件事：关键事实、禁止内容、来源归属。
默认关闭；开启后若 judge 不可用，该条记 `na`，不判 pass。

与金融 `agent_judge` 分开：金融 rubric 是数值等价 + 表格坐标，这里不需要，
混用会把"商誉定义"塞进"金额是否等价"的口径里。

用法（测试）：`await judge_task(task, result, call=fake, repeats=1)`
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Optional

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

JUDGE_MAX_TOKENS = 1024

_TEMPLATE = """你是金融问答评测的判分员。只判下面三件事，不要评价文风。

问题：
{question}

待判答案：
{answer}

关键事实（逐条判 pass）：
{facts}

禁止内容（出现即 forbidden_hit=true）：
{forbidden}

检索到的来源（label / 标题 / 摘要）：
{sources}

输出只能是 JSON，不要 Markdown 代码块：
{{
  "facts": [{{"index": 0, "pass": true, "reason": "≤30字"}}],
  "forbidden_hit": false,
  "forbidden_reason": "",
  "attribution_ok": true,
  "attribution_reason": ""
}}

规则：
- facts 必须覆盖每一条关键事实，index 从 0 开始；
- 答案只要语义上表达了该事实即算 pass，不要求逐字相同；
- 禁止内容按语义判，不要只做字符串匹配；
- attribution_ok：答案里每个具体数字/费率是否能在来源摘要里找到依据；没有数字则填 true。
"""


def _facts_block(task: dict) -> str:
    facts = task.get("key_facts") or []
    if not facts:
        return "（无；返回空数组）"
    return "\n".join(
        f"{index}. {fact.get('text') or ''}（可接受表述：{', '.join(fact.get('must_include_any') or [])}）"
        for index, fact in enumerate(facts)
    )


def _forbidden_block(task: dict) -> str:
    return "\n".join(f"- {item}" for item in (task.get("forbidden") or [])) or "（无）"


def _sources_block(result: dict) -> str:
    rows: list[str] = []
    for source in result.get("web_sources") or []:
        if not isinstance(source, dict):
            continue
        rows.append(
            f"- {source.get('label') or source.get('domain') or '来源'} / "
            f"{source.get('title') or ''} / {source.get('snippet') or ''}"
        )
    return "\n".join(rows) or "（本题没有联网来源）"


def build_prompt(task: dict, result: dict) -> list[dict[str, str]]:
    content = _TEMPLATE.format(
        question=task.get("question") or "",
        answer=str(result.get("answer") or "")[:2000],
        facts=_facts_block(task),
        forbidden=_forbidden_block(task),
        sources=_sources_block(result),
    )
    return [
        {"role": "system", "content": "你是严格的金融评测判分员，只输出 JSON。"},
        {"role": "user", "content": content},
    ]


def parse_json(text: str) -> Optional[dict]:
    match = _JSON_RE.search(text or "")
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def normalize_judgment(raw: Optional[dict], fact_count: int) -> dict:
    raw = raw or {}
    rows: list[dict] = []
    incoming = {
        int(item.get("index")): item
        for item in (raw.get("facts") or [])
        if isinstance(item, dict) and str(item.get("index", "")).isdigit()
    }
    for index in range(fact_count):
        item = incoming.get(index) or {}
        rows.append(
            {
                "index": index,
                "pass": bool(item.get("pass")),
                "reason": str(item.get("reason") or "judge 未返回该要点")[:80],
            }
        )
    return {
        "facts": rows,
        "forbidden_hit": bool(raw.get("forbidden_hit")),
        "forbidden_reason": str(raw.get("forbidden_reason") or "")[:120],
        "attribution_ok": bool(raw.get("attribution_ok", True)),
        "attribution_reason": str(raw.get("attribution_reason") or "")[:120],
    }


async def judge_task(
    task: dict,
    result: dict,
    *,
    call: Optional[Callable[..., Awaitable[Any]]] = None,
) -> dict:
    if call is None:
        from app.services.model_provider import chat

        call = chat
    try:
        reply, error, usage = await call(
            build_prompt(task, result),
            temperature=0.0,
            max_tokens=JUDGE_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "judgment": None, "error": f"{type(exc).__name__}: {exc}", "usage": {}}
    if error:
        return {"ok": False, "judgment": None, "error": str(error), "usage": usage or {}}
    parsed = parse_json(str(reply or ""))
    if parsed is None:
        return {"ok": False, "judgment": None, "error": "judge 返回不是合法 JSON", "usage": usage or {}}
    judgment = normalize_judgment(parsed, len(task.get("key_facts") or []))
    return {"ok": True, "judgment": judgment, "error": "", "usage": usage or {}}
