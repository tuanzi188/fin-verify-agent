"""语义判分：rubric + LLM judge。

**只判确定性检查判不了的四件事**：

1. 每个答案要点是否真的被回答覆盖（不是含糊其辞、不是答了一半）；
2. 每个要点是否被引用，且被引的那段原文是否**真的支撑**该要点（这就是文档 §8.9
   规则二的"引用正确率"，不是字符串匹配，也不是"有 [1] 就算引用"）；
3. 是否出现任务卡 `forbidden` 描述的内容（语义判，不要求字面相同）；
4. 澄清行为对不对（该问的问了 / 不该问的没问 / 说明假设了没有）。

**原文一律从语料回查**，不信 Agent 上下文里的内容，也不信答案里的转述：
judge 拿到的 `chunk_id -> 原文` 由调用方从 `evaluation.finance_corpus.chunk_map()` 传入。
这样即使 Agent 篡改了引用文本，judge 也能发现。

judge 不可用时（网络、额度、解析失败）返回 `ok=False`，调用方把这一维记成 NA，
**绝不当成 0 分**——把"没判"记成"判错"是评测里最常见的假数据来源。

**位置偏差**（2026-10-28 补）：pairwise judge 的经典坑是把 chosen 永远放 A 位，
实测能把 50% 的随机猜测洗成 96% 的"准确率"。pointwise judge 也有同一个病：
**先出现的要点、先给出的原文更容易被判 pass**。这里的做法是只换**展示顺序**、
不换 index 标签——`_reverse_for()` 按 (task_id, run_index, 重复序号) 决定这一轮正序
还是倒序，保证同一批任务里两种顺序都出现，且不额外多花一次调用。
注意：换顺序不等于消除偏差，它只保证偏差在批次内被平均掉、且能从
"正序 pass 率 vs 倒序 pass 率"的差值里看出来。真正确认要看 `judge_audit`。
"""

import asyncio
import json
import logging
import re
from typing import Any, Optional

from app.services import model_provider
from evaluation.agent_metrics import agreement
from evaluation.equivalence import rubric_text
from evaluation.judge_context import (
    JUDGE_CONTEXT_VERSION,
    JudgeContextError,
    judge_coordinate_content,
    validate_judge_context_size,
)

logger = logging.getLogger(__name__)

JUDGE_SYSTEM = (
    "你是严格的企业知识库问答评测员。只依据给定资料原文与回答判断，"
    "不使用你自己的常识补充事实。只输出 JSON，不输出任何其他文字。"
)

_MAX_CHUNK_CHARS = 260

CLARIFY_INSTRUCTIONS = {
    "ask": (
        "本题存在**会改变结论**的歧义，正确行为有两种，任一即可："
        "①明确请求用户确认关键条件；"
        "②把两种情形都说到，并**分别给出该情形下的结论**（用户能据此对号入座）。"
        "判 bad 的只有一种：只讲一种情形、直接把它当成用户的答案——用户若指另一种，答案就是错的。"
        "不要因为“没有出现问号”“没有写取决于”就判 bad：两种情形各自的结论都在，就算合格。"
    ),
    "none": (
        "本题表面含糊，但资料能唯一确定答案，正确行为是**直接作答**。"
        "若回答反问用户或要求用户补充条件，判 bad。"
    ),
    "assume": (
        "本题缺少一个非关键条件，正确行为是**说明所做假设后给出答案**（合理追问也可接受）。"
        "若既不说明假设也不追问就直接下结论，判 bad。"
    ),
}

_JUDGE_TEMPLATE = """【用户问题】
{question}

【资料原文】（来自知识库，chunk_id 是唯一标识）
{evidence}

【待评回答】
{answer}

【引用对照】（回答里的 [n] 指向哪一段）
{citation_map}

【判定任务】
{points_block}
{rubric_block}
{forbidden_block}
【澄清判定】
{clarify_instruction}
另外判断回答是否「有依据」：
- 回答里所有事实性陈述都能在原文中找到，或明确注明是推算 → grounded=true；
- 数值/规则在原文中找不到且未注明来源 → grounded=false；
- 回答整体只是「资料不足 / 无法确定」而没有任何事实性陈述 → grounded=true
  （没有断言就没有无据断言，别把诚实拒答记成编造）。
澄清判定只按上面【澄清判定】给出的本题专属口径判断，不要套用通用印象或
「有没有问号」这类形式判据；不同 clarify_mode 的正确行为不同，以本题说明为准。

只输出下面这个 JSON（字段名照抄，点数与要求条数一致）：
{{
  "points": [{{"index": 0, "pass": true, "cited": true, "citation_supports": true, "reason": "不超过 30 字"}}],
  "forbidden_hit": false,
  "forbidden_reason": "",
  "grounded": true,
  "grounded_reason": "",
  "clarify_verdict": "ok",
  "clarify_reason": "不超过 30 字"
}}
"""


def _truncate(text: str, limit: int = _MAX_CHUNK_CHARS) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _resolve_chunk(chunks: dict[str, dict], chunk_id: str) -> dict:
    """取原文；取不到时把定位锚点折回它所属的块。

    金融的 `get_cell` 引用是 `<doc>#<table>#<row>#<column>`，语料索引里只有
    `<doc>#<table>`。不折回，judge 拿到的就是空原文，等于让它凭感觉判引用。
    规则与 `agent_checks.base_chunk_id` 一致：段数 ≥3 就取前两段。
    """
    hit = chunks.get(chunk_id)
    if hit:
        return hit
    parts = str(chunk_id or "").split("#")
    if len(parts) >= 3:
        return chunks.get("#".join(parts[:2])) or {}
    return {}


def _collect_chunk_ids(task: dict, result: dict) -> list[str]:
    """judge 需要看哪些原文：答案引用到的 + 要点要求的。"""
    ids: list[str] = []
    for citation in result.get("citations") or []:
        chunk_id = str(citation.get("chunk_id") or "")
        if chunk_id and chunk_id not in ids:
            ids.append(chunk_id)
    for point in task.get("answer_points") or []:
        for chunk_id in (point or {}).get("chunk_ids") or []:
            if chunk_id and chunk_id not in ids:
                ids.append(chunk_id)
    return ids


def _reverse_for(task: dict, result: dict, repeat_index: int) -> bool:
    """这一轮是否倒序展示要点与原文。

    只换展示顺序，**不换 index 标签**：`0.` 永远是 0 号要点，判分结果照样按 index 归位。
    用 (task_id, run_index, 重复序号) 做一个确定性散列，而不是随机：
    评测要可复现，同一份 runs 重判必须得到同一套顺序。
    """
    key = f"{task.get('id')}|{result.get('run_index') or 1}|{repeat_index}"
    return sum(ord(char) for char in key) % 2 == 1


def build_prompt(task: dict, result: dict, chunks: dict[str, dict], *, reverse: bool = False) -> str:
    chunk_ids = _collect_chunk_ids(task, result)
    if chunk_ids:
        evidence_lines = []
        for chunk_id in chunk_ids:
            chunk = _resolve_chunk(chunks, chunk_id)
            source = chunk.get("chunk_id") or ""
            suffix = f"（原文取自所属块 {source}）" if source and source != chunk_id else ""
            evidence_lines.append(
                f"[chunk_id={chunk_id}] {judge_coordinate_content(chunk_id, chunks)}{suffix}"
            )
        if reverse:
            evidence_lines.reverse()
        evidence_block = "\n".join(evidence_lines)
    else:
        evidence_block = "（本题没有指定原文；请只判断回答里是否出现资料外的事实）"

    citation_map = "\n".join(
        f"[{citation.get('marker')}] -> {citation.get('chunk_id')}"
        for citation in (result.get("citations") or [])
    ) or "（回答没有引用）"

    points = task.get("answer_points") or []
    if points:
        point_lines = []
        for index, point in enumerate(points):
            required = "、".join((point or {}).get("chunk_ids") or [])
            point_lines.append(
                f"{index}. 要点：{(point or {}).get('text', '')}（对应原文 {required or '未指定'}）"
            )
        if reverse:
            point_lines.reverse()
        points_block = "逐条判断下列答案要点（pass=回答明确给出了该事实，不含糊、不残缺）：\n" + "\n".join(point_lines)
    else:
        points_block = "本题没有答案要点（负样本），points 返回空数组 []。"

    forbidden = task.get("forbidden") or []
    if forbidden:
        forbidden_block = "禁止内容（出现即 forbidden_hit=true，语义判断即可）：\n" + "\n".join(
            f"- {item}" for item in forbidden
        )
    else:
        forbidden_block = "本题没有禁止内容清单。"

    if task.get("should_abstain"):
        forbidden_block += (
            "\n注意：本题资料中**不存在答案**，正确行为是说明资料不足。"
            "若回答给出了具体数值、天数、比例或规则，forbidden_hit 必须为 true。"
        )

    clarify_mode = str(task.get("clarify_mode") or "none")
    clarify_instruction = CLARIFY_INSTRUCTIONS.get(clarify_mode, CLARIFY_INSTRUCTIONS["none"])

    return validate_judge_context_size(_JUDGE_TEMPLATE.format(
        question=task.get("question", ""),
        evidence=evidence_block,
        answer=str(result.get("answer") or "（空回答）"),
        citation_map=citation_map,
        points_block=points_block,
        rubric_block=rubric_text(),
        forbidden_block=forbidden_block,
        clarify_instruction=clarify_instruction,
    ))


def parse_judge_output(text: str) -> dict:
    """从 judge 输出里抽 JSON。允许前后有废话或代码围栏。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("judge 输出里没有 JSON 对象")
    return json.loads(cleaned[start : end + 1])


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "是"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def normalize_judgment(payload: dict, points_count: int) -> dict:
    """把 judge 的原始输出整理成固定结构，缺字段一律按"未通过"处理。"""
    raw_points = payload.get("points")
    normalized_points: list[dict] = []
    if isinstance(raw_points, list):
        by_index: dict[int, dict] = {}
        for position, item in enumerate(raw_points):
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index", position))
            except (TypeError, ValueError):
                index = position
            by_index[index] = item
        for index in range(points_count):
            item = by_index.get(index)
            if item is None:
                # 要点整条缺失和"要点判 false"是两件事：前者说明 judge 输出不完整，
                # 报告里要能看出来是判分器的问题，而不是答案的问题。
                normalized_points.append({
                    "index": index, "pass": False, "cited": False,
                    "citation_supports": False, "reason": "judge 未返回该要点",
                })
                continue
            normalized_points.append({
                "index": index,
                "pass": _as_bool(item.get("pass")),
                "cited": _as_bool(item.get("cited")),
                "citation_supports": _as_bool(item.get("citation_supports")),
                "reason": str(item.get("reason") or "")[:120],
            })
    elif points_count == 0:
        normalized_points = []
    else:
        # 要点一条都没返回：按全不通过记，并在 reason 里说明是 judge 的问题
        normalized_points = [
            {"index": index, "pass": False, "cited": False, "citation_supports": False,
             "reason": "judge 未返回该要点"}
            for index in range(points_count)
        ]

    clarify_verdict = str(payload.get("clarify_verdict") or "na").strip().lower()
    if clarify_verdict not in {"ok", "bad", "na"}:
        clarify_verdict = "na"

    return {
        "points": normalized_points,
        "forbidden_hit": _as_bool(payload.get("forbidden_hit")),
        "forbidden_reason": str(payload.get("forbidden_reason") or "")[:200],
        "grounded": _as_bool(payload.get("grounded"), default=True),
        "grounded_reason": str(payload.get("grounded_reason") or "")[:200],
        "clarify_verdict": clarify_verdict,
        "clarify_reason": str(payload.get("clarify_reason") or "")[:200],
    }


def _merge_judgments(judgments: list[dict]) -> tuple[dict, Optional[float]]:
    """多次判分取多数，并算一致率（按字段逐个比）。

    只判一次时一致率返回 None——**"只判一次所以完全一致"不是信号，是自欺**。
    """
    if len(judgments) == 1:
        return judgments[0], None

    field_values: dict[str, list] = {
        "forbidden_hit": [item["forbidden_hit"] for item in judgments],
        "grounded": [item["grounded"] for item in judgments],
        "clarify_verdict": [item["clarify_verdict"] for item in judgments],
    }
    point_count = len(judgments[0]["points"])
    for index in range(point_count):
        field_values[f"point_{index}_pass"] = [item["points"][index]["pass"] for item in judgments]
        field_values[f"point_{index}_supports"] = [
            item["points"][index]["citation_supports"] for item in judgments
        ]

    agreements = [agreement(values) for values in field_values.values()]
    overall = sum(value for value in agreements if value is not None) / max(len(agreements), 1)

    def majority(values: list):
        counts: dict = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        return max(counts.items(), key=lambda pair: pair[1])[0]

    merged = {
        "points": [],
        "forbidden_hit": majority(field_values["forbidden_hit"]),
        "forbidden_reason": judgments[0]["forbidden_reason"],
        "grounded": majority(field_values["grounded"]),
        "grounded_reason": judgments[0]["grounded_reason"],
        "clarify_verdict": majority(field_values["clarify_verdict"]),
        "clarify_reason": judgments[0]["clarify_reason"],
    }
    for index in range(point_count):
        merged["points"].append({
            "index": index,
            "pass": majority(field_values[f"point_{index}_pass"]),
            "cited": judgments[0]["points"][index]["cited"],
            "citation_supports": majority(field_values[f"point_{index}_supports"]),
            "reason": judgments[0]["points"][index]["reason"],
        })
    return merged, overall


# judge 的输出预算。2048 实测不够：模型偶尔把整份预算花在生成前的推理上，
# content 回来是空串、finish_reason=length（2026-10-27 一批 60 次里 6 次）。
JUDGE_MAX_TOKENS = 4096
# 解析失败再试一次：多一次调用远比把这条记成 NA 便宜，NA 会直接减少分母。
JUDGE_PARSE_ATTEMPTS = 2
# 网关抖动要不要等：2026-10-28 一批 24 条判分，前 2 条成功后网关返回
# `no_healthy_account` 503，熔断器随即打开 30s，后面 22 条全部秒失败记成 NA。
# 熔断窗口是固定的 30s，等一等就能过——**不重试等于把"网关忙"记成"判不了"**。
# 两次等待合计 60s，超过就老实记 NA（不能无限等，会把一次判分拖成小时级）。
JUDGE_RETRY_WAITS = (20.0, 40.0)


async def _call_with_backoff(prompt: str) -> tuple[str, Optional[str], dict]:
    """调一次 judge，网关抖动时按 JUDGE_RETRY_WAITS 等一等再试。

    只在"调用本身失败"时等；返回了内容就走原来的解析/重试逻辑，两件事不混。
    最后一次仍失败就抛出去，由调用方记成 NA——不把"网关忙"伪装成"判错"。
    """
    waits = (0.0,) + tuple(JUDGE_RETRY_WAITS)
    last_error: Optional[Exception] = None
    for index, wait in enumerate(waits):
        if wait:
            logger.warning("judge call failed, retry in %.0fs (%d/%d)", wait, index, len(waits) - 1)
            await asyncio.sleep(wait)
        try:
            return await model_provider.simple_query(
                prompt, JUDGE_SYSTEM, max_tokens=JUDGE_MAX_TOKENS
            )
        except Exception as exc:  # noqa: BLE001 - 分类交给上层，这里只负责重试
            last_error = exc
    raise last_error if last_error else RuntimeError("judge 调用未产生结果")


async def judge_task(
    task: dict,
    result: dict,
    chunks: dict[str, dict],
    *,
    repeats: int = 1,
) -> dict:
    """判一条运行。返回 `{"ok": bool, "judgment": {...}|None, "error": str|None, ...}`。"""
    points_count = len(task.get("answer_points") or [])
    judgments: list[dict] = []
    orders: list[str] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    errors: list[str] = []

    for repeat_index in range(max(1, repeats)):
        # 每轮重新决定展示顺序：重复判分时正反各来一次，位置偏差才会落在一致率里
        reverse = _reverse_for(task, result, repeat_index)
        try:
            prompt = build_prompt(task, result, chunks, reverse=reverse)
        except JudgeContextError as exc:
            errors.append(str(exc))
            break
        for attempt in range(JUDGE_PARSE_ATTEMPTS):
            attempt_prompt = prompt
            if attempt:
                attempt_prompt = (
                    prompt
                    + "\n\n【重试】上一次输出无法解析。这次只输出 JSON 对象，"
                    "不要任何解释、不要代码块围栏。"
                )
            try:
                content, _, usage = await _call_with_backoff(attempt_prompt)
            except Exception as exc:  # noqa: BLE001 - judge 失败要落成 NA，不能污染分数
                errors.append(f"judge 调用失败: {type(exc).__name__}: {exc}")
                break
            usage_total["prompt_tokens"] += int((usage or {}).get("prompt_tokens") or 0)
            usage_total["completion_tokens"] += int((usage or {}).get("completion_tokens") or 0)

            # 先看是不是被预算截断：空 content + finish_reason=length 是最常见的假解析失败
            if str((usage or {}).get("finish_reason") or "") == "length":
                errors.append("judge 输出被 token 上限截断")
                logger.warning("judge output hit length limit: task=%s", task.get("id"))
                continue
            try:
                payload = parse_judge_output(content)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"judge 输出解析失败: {type(exc).__name__}: {exc}")
                logger.warning("judge raw output (task=%s): %r", task.get("id"), (content or "")[:400])
                continue
            judgments.append(normalize_judgment(payload, points_count))
            orders.append("reversed" if reverse else "forward")
            break

    if not judgments:
        return {
            "ok": False,
            "context_version": JUDGE_CONTEXT_VERSION,
            "judgment": None,
            "error": "; ".join(errors) or "judge 未返回可用结果",
            "repeats": repeats,
            "agreement": None,
            "orders": orders,
            "usage": usage_total,
        }

    merged, consistency = _merge_judgments(judgments)
    return {
        "ok": True,
        "context_version": JUDGE_CONTEXT_VERSION,
        "judgment": merged,
        "error": "; ".join(errors) if errors else None,
        "repeats": len(judgments),
        "agreement": consistency,
        # 正序/倒序各判了几次：判分侧要能看出"偏差有没有被摊开"，
        # 也方便以后专门比 forward 与 reversed 的 pass 率差
        "orders": orders,
        "usage": usage_total,
    }
