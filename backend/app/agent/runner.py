"""被测 Agent（P1：baseline 变体）。

契约：`run(task, variant, run_index) -> result`，result 字段见
`app/tools/contract.py`，形式对齐 `docs/AGENT_EVAL_PROJECT_SPEC.md` §6.5。

baseline 的定义（对照组的诚实性靠它撑着）：

- 最多**一轮**工具调用：拿到检索结果就进终轮生成，不再二次检索；
- **无自评**：不检查要点是否覆盖、引用是否支撑；
- **无修复**：检索空/报错不重试、不改写 query；
- **无澄清判定**：不识别缺槽，一律按原问题作答。

这四条砍掉的是「决策行为」，不是「提示词质量」——prompt 与 improved 用同一套
纪律写，否则 delta 是假的。

P1 只支持 baseline。improved 需要多轮、自评门、修复策略三件东西一起加，
单独放进来会让"改了到底有没有用"无法归因。
"""

import json
import logging
import re
import time
from typing import Any, Optional

from app.agent.prompts import CALC_REPAIR_HINT, COVERAGE_CHECK_TEMPLATE, ROUND_EVIDENCE_TEMPLATE
from app.services import model_provider
from app.tools.contract import ABSTAIN_KEYWORDS
from app.tools.mock import Toolbox
from app.tools.toolset import ToolOutcome

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 3
# 任务卡可以要求更多步，但一律夹到这个上限。
# 原来是 10，2026-10-28 金融摸底暴露两个问题：①夹断是静默的，任务卡写 16 实际只有 10；
# ②步数撞上限会被记成 `max_steps_exceeded`，让检查把"预算不够"判成"答案错"。
# 24 条里 9 条卡在 10 步（平均 9.5），说明 10 是真正的瓶颈，不是模型懒得查。
MAX_STEPS_CEILING = 20
# 1024 在实测里不够：模型偶尔把预算全花在生成前的推理上，content 回来是空的。
# 提到 2048 并把 finish_reason 记进 trace，真撞上限时能一眼看出来。
FINAL_MAX_TOKENS = 2048
# 显式传温度，不靠客户端默认值：跑批结果必须能被别人按同一组参数复现。
# `run_agent_eval` 会把这个值写进 runs 的 `run_config`，报告才能自证口径。
TOOL_TEMPERATURE = 0.1
SUPPORTED_VARIANTS = ("baseline", "improved")

# improved 的三个机制参数。它们必须能被复现，所以运行时一起写进 run_config：
# - 多轮：最多 3 个"模型回合"（每回合可发多条工具调用），受 max_steps 进一步约束；
# - 空/错自动重试：整条任务最多 1 次，避免在坏工具上反复烧 token；
# - 覆盖自检：整条任务最多 1 次，只判断"子问题是否有资料"，不读答案要点。
IMPROVED_MAX_TOOL_ROUNDS = 3
IMPROVED_AUTO_RETRY_LIMIT = 1
IMPROVED_SELF_CHECK_LIMIT = 1

VARIANT_CONFIG = {
    "baseline": {
        "tool_rounds": 1,
        "auto_retry_limit": 0,
        "self_check_limit": 0,
    },
    "improved": {
        "tool_rounds": IMPROVED_MAX_TOOL_ROUNDS,
        "auto_retry_limit": IMPROVED_AUTO_RETRY_LIMIT,
        "self_check_limit": IMPROVED_SELF_CHECK_LIMIT,
    },
}

_MARKER_PATTERN = re.compile(r"\[(\d{1,2})\]")
_SENTENCE_BOUNDARY = re.compile(r"[。！？\n]")

# 分情形作答的信号。只用于回答"首句里的'无法确定'是拒答还是分情形开场"，
# 不用于判分——判分仍靠 judge（见 §8.2）。
_CONDITIONAL_SIGNALS = (
    "取决于",
    "如果是",
    "若是",
    "若按",
    "两种情形",
    "两种情况",
    "分别",
    "哪一种",
    "哪一套",
    "哪条",
)

# 澄清信号的保守词表。**只做粗判，不能当最终判据**：实测里 C2 那种
# "如果你要办的是借调而非正式转岗……" 的追问，关键词一个都命中不了。
# 所以 `__final__.clarify.asked` 只当辅助信号；§8.2 的"追问相关性"仍要靠 P3 的
# judge 判，触发率也要等 P3 用 judge 复核一遍再定口径。
CLARIFY_SIGNALS = (
    "请告诉我",
    "请确认",
    "请说明",
    "是哪一种",
    "哪一种",
    "哪一类",
    "哪一年",
    "哪个年度",
    "你指的是",
    "是否需要",
    "取决于",
)


_HEADING_LABELS = {"结论", "答案", "回答", "判断", "总结", "概要"}


def _leading_sentence(answer: str, limit: int = 80) -> str:
    """取答案的第一句实质内容，用来判"这是拒答还是带说明的正常回答"。

    实测踩过两类：
    1. 一条完全正确的答案结尾写了"这两点根据现有资料无法确定"，
       被整句关键词匹配判成 abstained——正样本误拒比漏拒更伤。
    2. Markdown 答案先写一行 `## 结论`，换行就结束了"首句"，
       后面的"根据现有资料无法确定"永远看不到。2026-10-27 实测
       N2#3 因此被记成 answered（实际是正确拒答）。
    所以：先剥掉 Markdown 标题标记，跳过纯标签行，再看第一句。
    """
    lines: list[str] = []
    for raw in (answer or "").strip().splitlines():
        line = re.sub(r"^#{1,6}\s*", "", raw.strip()).strip()
        if line:
            lines.append(line)
    while lines and lines[0].rstrip("：:").strip("* ") in _HEADING_LABELS:
        lines.pop(0)
    text = "\n".join(lines) if lines else (answer or "").strip()
    boundary = _SENTENCE_BOUNDARY.search(text)
    head = text[: boundary.start()] if boundary else text
    return head[:limit].strip()


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _normalize_max_steps(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_STEPS
    return max(1, min(value, MAX_STEPS_CEILING))


# 比率 / 占比 / 百分点 / 倍数：答案里一旦出现，就必须要么来自原文、要么来自 calculate
_RATIO_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|个百分点|倍)")


def needs_calc_repair(answer: str, evidence: list[dict], calcs: list[dict]) -> bool:
    """答案里有"证据原文里没有的比率"，而本轮一次 calculate 都没调 → 需要补做。

    为什么不能只看有没有 `%`：年报本来就写着"同比 12.11%""占比 3.90%"，
    照抄不是手算。只有"答案里的比率在证据原文里找不到"才说明这个数是模型自己算的。
    `calcs` 非空就不再追问——闸门只管"一次都没算过"，不管算得对不对（那是判分的事）。
    """
    if calcs or not answer:
        return False
    haystack = " ".join(str(item.get("content") or "") for item in evidence)
    haystack = haystack.replace(" ", "").replace(",", "")
    for match in _RATIO_PATTERN.finditer(answer):
        literal = match.group(0).replace(" ", "").replace(",", "")
        if literal in haystack:
            continue
        return True
    return False


def _format_calcs(calcs: list[dict]) -> str:
    """把 calculate 记录渲染成给模型看的几行。

    必须显式喂回去：终稿是用 `chat()` 生成的，**请求里没有工具定义**，
    模型既看不到 calculate 的结果、也会误以为没有这个工具
    （实测 FIN-09 重写后仍写着"我这一轮没有可调用的 calculate 工具"）。
    """
    lines = []
    for item in calcs or []:
        lines.append(
            f"- formula: {item.get('formula')}｜inputs: "
            f"{json.dumps(item.get('inputs') or {}, ensure_ascii=False)}｜"
            f"result: {item.get('result')}"
        )
    return "\n".join(lines) or "（没有计算记录）"

def _accumulate_tokens(usage: dict, call_usage: Optional[dict]) -> tuple[int, int]:
    tokens_in = int((call_usage or {}).get("prompt_tokens") or 0)
    tokens_out = int((call_usage or {}).get("completion_tokens") or 0)
    usage["tokens_in"] += tokens_in
    usage["tokens_out"] += tokens_out
    # 记下实际出答的 provider：`no_tool_call` 既可能是"模型选择不查"，也可能是
    # "这家 provider 不支持 tool calling"。没有这一项，两种原因在报告里分不开。
    provider = (call_usage or {}).get("provider")
    if provider:
        providers = usage.setdefault("providers", [])
        if provider not in providers:
            providers.append(provider)
    return tokens_in, tokens_out


def _format_evidence(evidence: list[dict]) -> str:
    blocks = []
    for index, item in enumerate(evidence, 1):
        similarity = item.get("similarity")
        similarity_text = f"{similarity:.3f}" if isinstance(similarity, (int, float)) else "未知"
        blocks.append(
            f"[{index}] {item.get('title') or '未知文档'}"
            f"（chunk_id={item.get('chunk_id', '')}，similarity={similarity_text}）\n"
            f"{item.get('content', '')}"
        )
    return "\n\n".join(blocks)


def _extract_citations(answer: str, evidence: list[dict]) -> list[dict]:
    """把答案里的 [n] 映射回 chunk_id。

    只做映射，不判正确性——"引用存在"和"引用正确"是两件事（文档 §8.9 规则二），
    正确性由 P3 的判分器逐点核验。
    """
    if not answer or not evidence:
        return []
    ordered: list[int] = []
    for raw in _MARKER_PATTERN.findall(answer):
        marker = int(raw)
        if 1 <= marker <= len(evidence) and marker not in ordered:
            ordered.append(marker)
    return [
        {"marker": marker, "chunk_id": str(evidence[marker - 1].get("chunk_id", ""))}
        for marker in ordered
    ]


def _toolset_name(toolbox: Any) -> str:
    return str(getattr(toolbox, "name", "retrieval"))


def _public_evidence(evidence: list[dict], limit: int = 400) -> list[dict]:
    """落盘的证据副本：只留可核对字段，正文截短。

    全文留在内存里供 prompt 用；runs 文件是给人和报告看的，
    把 6000 字的章节整段写进去会让一次 30 题 × 3 次的跑批文件大到不可读。
    """
    rows = []
    for item in evidence:
        row = {
            "chunk_id": item.get("chunk_id"),
            "title": item.get("title"),
            "kind": item.get("kind", "chunk"),
            "page": item.get("page"),
            "truncated": bool(item.get("truncated")),
            "content": str(item.get("content") or "")[:limit],
        }
        if isinstance(item.get("similarity"), (int, float)):
            row["similarity"] = item["similarity"]
        for key in ("path", "table_id", "row_name", "column", "cell_text", "unit", "doc_id"):
            if item.get(key) is not None:
                row[key] = item[key]
        rows.append(row)
    return rows


def _decide_status(
    *,
    answer: str,
    executed: int,
    clarify_mode: str,
    failed: bool,
) -> str:
    """状态由行为判，不由 prompt 求。

    四态与文档 §6.5 一致：`answered / abstained / clarified / error`。
    """
    if failed or not answer:
        return "error"
    head = _leading_sentence(answer)
    if any(keyword in head for keyword in ABSTAIN_KEYWORDS):
        # ask 任务里，"结论：取决于你走哪套制度，现有资料无法确定" 是分情形作答的开场，
        # 不是拒答。整段后面给出了两种情形各自的结论，就该记 answered；
        # 否则 false_refusal 门禁会把这类答案误判成误拒。
        if clarify_mode == "ask" and any(signal in answer for signal in _CONDITIONAL_SIGNALS):
            pass
        else:
            return "abstained"
    if executed == 0 and clarify_mode == "ask" and ("？" in answer or "?" in answer):
        return "clarified"
    return "answered"


async def _execute_tool_call(
    toolbox: Any,
    call: dict,
    step: int,
    evidence: list[dict],
    seen_ids: set[str],
    locators: Optional[list[dict]] = None,
    seen_locators: Optional[set[str]] = None,
    policy: str = "",
) -> dict:
    name = str(call.get("name") or "")
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    started_at = time.perf_counter()

    if call.get("parse_error"):
        outcome = ToolOutcome(
            tool=name or "unknown",
            ok=False,
            error_kind="bad_args",
            error_detail=str(call["parse_error"]),
        )
    else:
        outcome = await toolbox.call(name, arguments)

    latency_ms = _elapsed_ms(started_at)

    new_result_count = 0
    similarities: list[float] = []
    truncated = False
    if outcome.ok:
        # 「工具返回什么」由 toolset 决定；runner 只认 to_evidence 的契约。
        # 制度库把它变成 chunk，金融把它变成 section/table/cell，
        # 换域时这一层不用改。
        for item in toolbox.to_evidence(name, outcome.data):
            if not isinstance(item, dict):
                continue
            if item.get("truncated"):
                truncated = True
            chunk_id = str(item.get("chunk_id") or "")
            citable = item.get("citable", True)
            # 去重分两类：定位结果和原文共用同一个 id（`doc#s0003`），
            # 如果共用一个 seen 集合，先定位后 read_section 会把原文判成重复丢掉，
            # 于是证据永远为空、模型只能拒答（2026-10-28 实测踩中）。
            bucket = seen_ids if citable else seen_locators
            if chunk_id and bucket is not None and chunk_id in bucket:
                continue
            if chunk_id and bucket is not None:
                bucket.add(chunk_id)
            new_result_count += 1
            similarity = item.get("similarity")
            if isinstance(similarity, (int, float)):
                similarities.append(round(float(similarity), 4))
            # 只定位不算证据（如 search_sections）：不编号、不可引用，
            # **但必须让模型看见**——否则它拿不到 section_id，下一轮根本没法 read_section，
            # 2026-10-28 实测就是这样：查到 10 个章节，模型只看到"新增 10 条"，
            # 拿不到任何 id，最后直接拒答。
            if citable:
                evidence.append(item)
                if locators is not None and chunk_id:
                    # 原文已读到，把同名定位项撤掉，避免上下文里同一节出现两次
                    locators[:] = [one for one in locators if one.get("chunk_id") != chunk_id]
            elif locators is not None:
                locators.append(item)

    record = {
        "step": step,
        "tool": name or "unknown",
        "args": arguments,
        "ok": outcome.ok,
        "result_ids": list(outcome.result_ids),
        "result_count": outcome.result_count,
        "injected": outcome.injected,
        "latency_ms": latency_ms,
        "tokens_in": 0,
        "tokens_out": 0,
    }
    if name == toolbox.search_tool or new_result_count:
        record["new_result_count"] = new_result_count
    if similarities:
        record["similarities"] = similarities
        record["max_similarity"] = max(similarities)
    # 截断注入不是"错误"，但它是"证据不可用"的信号：不标出来，
    # 自动重试和覆盖自检都会把 12 个字的残段当成完整依据。
    if truncated:
        record["truncated"] = True
    if outcome.error_kind:
        record["error_kind"] = outcome.error_kind
    if outcome.error_detail:
        record["error_detail"] = outcome.error_detail[:300]
    if policy:
        record["policy"] = policy
    return record


def _rewrite_query(question: str, failed_query: str = "") -> str:
    """空结果/报错后的 query 改写。

    只做保守的"去客套 + 回到完整问题"：不引入同义词库，也不猜答案，
    保证这个机制可复现、可审计。若改写后与失败 query 相同，就退回原问题。
    """
    cleaned = re.sub(r"[？?。！!，,、；;：:\s]+", " ", question or "")
    for token in ("请问", "麻烦", "帮我", "我想问", "想问", "一下", "咱们", "我们"):
        cleaned = cleaned.replace(token, " ")
    cleaned = " ".join(cleaned.split()).strip()
    if cleaned and cleaned != (failed_query or "").strip():
        return cleaned
    return (question or "").strip()


def _format_locators(locators: list[dict], limit: int = 12) -> str:
    """把"能看但不能引用"的资料分段渲染。

    分两种，混在一起模型会懵：
    - `catalog`（文档清单）要把内容直接摊开，否则模型只看到"返回 7 条"，
      会反复调 list_documents 想看清单（2026-10-28 实测连调三次然后拒答）；
    - `section_locator`（章节定位）只给 id + 标题 + 页码，让模型下一步去读。
    """
    if not locators:
        return ""
    blocks: list[str] = []

    catalogs = [item for item in locators if item.get("kind") == "catalog"]
    if catalogs:
        blocks.append(
            "本地文档清单（只是索引，不是证据）：\n" + str(catalogs[-1].get("content") or "")
        )

    sections = [item for item in locators if item.get("kind") != "catalog"]
    if sections:
        rows = ["可读取的章节（只是定位结果，**不是证据**；要引用必须先 read_section / get_table / get_cell）："]
        for item in sections[:limit]:
            title = str(item.get("title") or "")
            page = item.get("page")
            page_text = f"｜PDF 第 {page} 页" if page else ""
            rows.append(f"- {item.get('chunk_id')}｜{title}{page_text}")
        blocks.append("\n".join(rows))

    return "\n\n".join(blocks)


def _round_message(
    evidence: list[dict],
    round_steps: list[dict],
    locators: list[dict],
    search_tool: str = "",
) -> str:
    """把本轮工具结果回填给模型。

    与 baseline 的 FINAL_EVIDENCE_TEMPLATE 不同：这里**不终止生成**，
    而是明确告诉模型"还缺就继续查、够了就回答"，多轮循环才有意义。
    """
    status_lines: list[str] = []
    for step in round_steps:
        if step.get("ok"):
            extra = ""
            if step.get("new_result_count") is not None:
                extra = f"，新增 {step.get('new_result_count', 0)} 条"
                if step.get("truncated"):
                    extra += "，内容被截断"
            status_lines.append(f"- {step.get('tool')} 成功，返回 {step.get('result_count', 0)} 条{extra}")
        else:
            status_lines.append(
                f"- {step.get('tool')} 失败：{step.get('error_kind')} {step.get('error_detail') or ''}".strip()
            )
    # 检索没有新结果时给一句明确引导：重复同义检索是实测最常见的空转形态。
    if any(
        step.get("tool") == search_tool
        and int(step.get("new_result_count") or 0) == 0
        for step in round_steps
    ):
        status_lines.append("- 本次检索没有新增内容：不要再用同义关键词重复检索，改用 read_section / get_table 读已定位的内容")
    evidence_text = _format_evidence(evidence) if evidence else "（还没有读到原文，下面是可定位/可读取的内容）"
    locator_text = _format_locators(locators)
    if locator_text:
        evidence_text = f"{evidence_text}\n\n{locator_text}"
    # 只有定位、没有原文时必须给一句明确的下一步，否则模型会继续换关键词搜
    # （实测连搜 6 次不读，最后拒答）。
    if not evidence and locators:
        evidence_text = (
            f"{evidence_text}\n\n"
            "下一步：从上面挑 1–2 个最相关的章节 id，直接调用 read_section 读原文；"
            "读到的章节若带 table_ids，再用 get_table / get_cell 取数字。不要再用同义关键词重复检索。"
        )
    return ROUND_EVIDENCE_TEMPLATE.format(
        evidence=evidence_text,
        status="\n".join(status_lines) or "- 本轮没有工具调用",
    )


def _parse_self_check(text: str) -> Optional[dict]:
    """解析覆盖自检的 JSON。解析失败返回 None，绝不阻断主流程。"""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    missing = data.get("missing") or []
    return {
        "covered": bool(data.get("covered")),
        "missing": [str(item) for item in missing][:5] if isinstance(missing, list) else [],
        "next_query": str(data.get("next_query") or "").strip()[:200],
    }


async def _coverage_check(question: str, evidence: list[dict]) -> tuple[Optional[dict], dict]:
    """终轮前的覆盖自检：只问"资料够不够回答每个子问题"。

    硬约束：**不读 `answer_points`、不读 gold chunk**，只看用户问题与已检索段落。
    读了 gold 就是拿答案作弊，improved 的 delta 全部作废。
    """
    blocks = []
    # 最多 15 段（3 轮 × 5 条）都送进自检：只送前 8 段会把后补的检索结果漏掉，
    # 自检就会误判"没覆盖"，白烧一次补检索。
    for index, item in enumerate(evidence[:15], 1):
        blocks.append(
            f"[{index}] {item.get('title') or '未知文档'}："
            f"{str(item.get('content') or '')[:220]}"
        )
    prompt = COVERAGE_CHECK_TEMPLATE.format(
        question=question,
        evidence="\n\n".join(blocks) or "（无）",
    )
    text, _, usage = await model_provider.chat(
        [{"role": "user", "content": prompt}],
        reasoning_mode="flash",
        temperature=TOOL_TEMPERATURE,
        max_tokens=512,
    )
    return _parse_self_check(text), usage or {}


async def _run_improved(
    *,
    task_id: str,
    question: str,
    max_steps: int,
    clarify_mode: str,
    toolbox: Any,
    citation_required: bool = False,
    run_index: int = 1,
) -> dict:
    """improved：多轮工具循环 + 空/错自动重试 + 终轮覆盖自检。

    与 baseline 的差别只有机制，没有提示词特权：
    - 每一轮都允许模型继续调用工具，直到它给出回答或回合/步数用尽；
    - 一轮里出现空结果、工具错误或截断时，runner 自动用改写后的 query 补一次；
    - 生成回答前做一次覆盖自检，缺子问题就补检索再重写答案。
    """
    messages = [
        {"role": "system", "content": toolbox.system_prompt("improved")},
        {"role": "user", "content": question},
    ]
    trace: list[dict] = []
    evidence: list[dict] = []
    locators: list[dict] = []
    seen_chunk_ids: set[str] = set()
    seen_locator_ids: set[str] = set()
    usage = {"steps": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0}
    notes: list[str] = []

    started_at = time.perf_counter()
    executed = 0
    rounds = 0
    auto_retries = 0
    self_checks = 0
    budget_exceeded = False
    calc_repaired = False
    failed = False
    answer = ""
    final_meta: dict = {}
    final_latency_ms = 0
    final_tokens_in = 0
    final_tokens_out = 0
    self_eval: Optional[dict] = None

    max_rounds = max(
        1,
        min(getattr(toolbox, "max_tool_rounds", IMPROVED_MAX_TOOL_ROUNDS), max_steps),
    )

    try:
        while rounds < max_rounds:
            # 预算用完就收口，不再发起一轮"注定要被拒"的工具调用：
            # 收口本身不是失败，只记 `budget_truncated` 这个信号
            # （2026-10-28 起 step_budget 只在 steps > max_steps 时判失败）。
            if executed >= max_steps:
                break
            rounds += 1
            llm_started_at = time.perf_counter()
            text, tool_calls, _, round_usage = await model_provider.chat_with_tools(
                messages,
                toolbox.specs(),
                reasoning_mode="flash",
                temperature=TOOL_TEMPERATURE,
            )
            final_latency_ms = _elapsed_ms(llm_started_at)
            final_tokens_in, final_tokens_out = _accumulate_tokens(usage, round_usage)
            final_meta = round_usage or {}

            if not tool_calls:
                text = (text or "").strip()
                # 派生数值必须留下 calculate 记录。提示词已经写了两遍"比率一律 calculate"，
                # 2026-10-28 实测仍有 2/24 条在正文里手算（数值还对），所以这里加一道确定性闸门：
                # 答案里出现"证据原文里没有的比率"且本轮没有任何 calc → 退回让它补做一次。
                # 只对声明了 `calcs` 的工具集生效：制度库那套没有 calculate，硬追问会问出空转。
                calcs = getattr(toolbox, "calcs", None)
                if (
                    not calc_repaired
                    and calcs is not None
                    and needs_calc_repair(text, evidence, calcs)
                ):
                    calc_repaired = True
                    notes.append("calc_repair")
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": CALC_REPAIR_HINT})
                    continue
                answer = text
                break

            if executed >= max_steps:
                budget_exceeded = True
                break

            round_steps: list[dict] = []
            for call in tool_calls:
                if executed >= max_steps:
                    budget_exceeded = True
                    break
                executed += 1
                step = await _execute_tool_call(
                    toolbox, call, executed, evidence, seen_chunk_ids, locators, seen_locator_ids
                )
                trace.append(step)
                round_steps.append(step)

            # 自动重试：只处理"这一轮有失败/空结果/截断"，不替模型做无限重试。
            round_problem = any(
                (not step.get("ok"))
                or (
                    step.get("tool") == toolbox.search_tool
                    and (int(step.get("new_result_count") or 0) == 0 or step.get("truncated"))
                )
                for step in round_steps
            )
            if (
                round_problem
                and auto_retries < IMPROVED_AUTO_RETRY_LIMIT
                and executed < max_steps
            ):
                failed_query = ""
                for step in reversed(round_steps):
                    if step.get("tool") == toolbox.search_tool:
                        failed_query = str((step.get("args") or {}).get("query") or "")
                        break
                retry_query = _rewrite_query(question, failed_query)
                auto_retries += 1
                executed += 1
                step = await _execute_tool_call(
                    toolbox,
                    {"name": toolbox.search_tool, "arguments": {"query": retry_query}},
                    executed,
                    evidence,
                    seen_chunk_ids,
                    locators,
                    seen_locator_ids,
                    policy="auto_retry",
                )
                trace.append(step)
                round_steps.append(step)
                notes.append("auto_retry")

            messages.append({
                "role": "user",
                "content": _round_message(
                    evidence, round_steps, locators, toolbox.search_tool
                ),
            })

        if not answer:
            # 回合用完 / 预算撞顶：仍然要产出一个可判分的答案，不能丢记录。
            if evidence:
                content = toolbox.final_template.format(evidence=_format_evidence(evidence))
                if locators:
                    content = f"{content}\n\n{_format_locators(locators)}"
            elif locators:
                # 只有定位没有原文：不能说"没检索到"，那会把"读得不够"说成"资料没有"，
                # 归因整条错掉（实测模型照着这句话直接拒答）。
                content = (
                    f"{_format_locators(locators)}\n\n"
                    "以上只有章节定位，没有读到原文。请据此回答：说明还需要读哪一节/哪张表，不要给数字。"
                )
            else:
                content = "本次没有检索到可用段落。请据此回答：资料不足时直接说明无法确定，不要猜测。"
            messages.append({"role": "user", "content": content})
            llm_started_at = time.perf_counter()
            text, _, final_usage = await model_provider.chat(
                messages,
                reasoning_mode="flash",
                temperature=TOOL_TEMPERATURE,
                max_tokens=FINAL_MAX_TOKENS,
            )
            final_latency_ms = _elapsed_ms(llm_started_at)
            final_tokens_in, final_tokens_out = _accumulate_tokens(usage, final_usage)
            final_meta = final_usage or {}
            answer = (text or "").strip()

        # 派生数值复核：答案已经产生了，但里面的比率是手算的（证据原文里没有、calcs 为空）。
        # 放在覆盖自检之前——先保证"数是怎么来的"对，再谈"子问题全不全"。
        # 只补算一次；只接受 calculate 这一步，别的工具调用忽略（避免借机把预算花在检索上）。
        calcs = getattr(toolbox, "calcs", None)
        if (
            answer
            and calcs is not None
            and not calc_repaired
            and executed < max_steps
            and needs_calc_repair(answer, evidence, calcs)
        ):
            calc_repaired = True
            notes.append("calc_repair")
            messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content": CALC_REPAIR_HINT})
            llm_started_at = time.perf_counter()
            text, repair_calls, _, repair_usage = await model_provider.chat_with_tools(
                messages,
                toolbox.specs(),
                reasoning_mode="flash",
                temperature=TOOL_TEMPERATURE,
            )
            _accumulate_tokens(usage, repair_usage)
            for call in repair_calls or []:
                if executed >= max_steps:
                    break
                if str(call.get("name") or "") != "calculate":
                    continue
                executed += 1
                step = await _execute_tool_call(
                    toolbox, call, executed, evidence, seen_chunk_ids,
                    locators, seen_locator_ids, policy="calc_repair",
                )
                trace.append(step)
            messages.append({
                "role": "user",
                "content": (
                    toolbox.final_template.format(evidence=_format_evidence(evidence))
                    + "\n\n补算说明：上一版草稿里的比率是手算的，现已用 calculate 复核，结果如下：\n"
                    + _format_calcs(calcs)
                    + "\n\n请按上面的公式与结果重写最终回答，不要再用草稿里的数字。"
                ),
            })
            llm_started_at = time.perf_counter()
            text, _, final_usage = await model_provider.chat(
                messages,
                reasoning_mode="flash",
                temperature=TOOL_TEMPERATURE,
                max_tokens=FINAL_MAX_TOKENS,
            )
            final_latency_ms = _elapsed_ms(llm_started_at)
            final_tokens_in, final_tokens_out = _accumulate_tokens(usage, final_usage)
            final_meta = final_usage or {}
            answer = (text or "").strip()

        # 覆盖自检：只在"有证据、有草稿、还有步数预算"时做一次。
        # 澄清任务（clarify_mode=ask）直接跳过——该问的时候不该被自检逼成硬答。
        if answer and evidence and clarify_mode != "ask":
            if executed < max_steps and self_checks < IMPROVED_SELF_CHECK_LIMIT:
                self_checks += 1
                try:
                    parsed, check_usage = await _coverage_check(question, evidence)
                    _accumulate_tokens(usage, check_usage)
                except Exception as exc:  # noqa: BLE001 - 自检失败不能把已生成的答案带走
                    parsed = None
                    notes.append(f"self_check_error:{type(exc).__name__}")
                self_eval = {
                    "checked": parsed is not None,
                    "covered": parsed.get("covered") if parsed else None,
                    "missing": parsed.get("missing") if parsed else [],
                    "supplemented": False,
                }
                if parsed and not parsed["covered"] and parsed["next_query"] and executed < max_steps:
                    executed += 1
                    step = await _execute_tool_call(
                        toolbox,
                        {"name": toolbox.search_tool, "arguments": {"query": parsed["next_query"]}},
                        executed,
                        evidence,
                        seen_chunk_ids,
                        locators,
                        seen_locator_ids,
                        policy="self_check_supplement",
                    )
                    trace.append(step)
                    self_eval["supplemented"] = True
                    messages.append({"role": "assistant", "content": answer})
                    messages.append({
                        "role": "user",
                        "content": (
                            toolbox.final_template.format(evidence=_format_evidence(evidence))
                            + "\n\n补充说明：上一版草稿可能漏了子问题，请基于以上全部资料重新给出完整回答。"
                        ),
                    })
                    llm_started_at = time.perf_counter()
                    text, _, final_usage = await model_provider.chat(
                        messages,
                        reasoning_mode="flash",
                        temperature=TOOL_TEMPERATURE,
                        max_tokens=FINAL_MAX_TOKENS,
                    )
                    final_latency_ms = _elapsed_ms(llm_started_at)
                    final_tokens_in, final_tokens_out = _accumulate_tokens(usage, final_usage)
                    final_meta = final_usage or {}
                    answer = (text or "").strip()
            else:
                self_eval = {
                    "checked": False,
                    "reason": "步数预算已用尽或自检次数已达上限",
                }
    except Exception as exc:  # noqa: BLE001 - 运行失败要落成 failed 而不是丢记录
        failed = True
        notes.append(f"run_error: {type(exc).__name__}: {exc}")
        logger.exception("Improved agent run failed: task=%s", task_id)

    if budget_exceeded:
        notes.append("budget_truncated")
    if not failed and not trace:
        notes.append("no_tool_call")
    if not failed and not answer:
        if final_meta.get("finish_reason") == "length":
            notes.append("empty_final_answer:max_tokens")
        elif final_meta.get("has_reasoning"):
            notes.append("empty_final_answer:reasoning_only")
        else:
            notes.append("empty_final_answer")
    planned = getattr(toolbox, "planned_injections", 0)
    fired = getattr(toolbox, "fired_injections", 0)
    if planned and fired < planned:
        notes.append(
            f"injection_not_triggered:{fired}/{planned}"
        )

    citations = _extract_citations(answer, evidence)
    clarify_signal = bool(answer) and any(signal in answer for signal in CLARIFY_SIGNALS)
    status = _decide_status(
        answer=answer,
        executed=executed,
        clarify_mode=clarify_mode,
        failed=failed,
    )
    if status == "answered" and not citations and citation_required:
        notes.append("citation_missing")

    usage["steps"] = executed
    usage["rounds"] = rounds
    usage["auto_retries"] = auto_retries
    usage["self_checks"] = self_checks
    usage["latency_ms"] = _elapsed_ms(started_at)

    final_step = {
        "step": executed + 1,
        "tool": "__final__",
        "args": {},
        "ok": not failed,
        "result_ids": [],
        "result_count": 0,
        "injected": None,
        "latency_ms": final_latency_ms,
        "tokens_in": final_tokens_in,
        "tokens_out": final_tokens_out,
        "self_eval": self_eval,
        "clarify": {"mode": clarify_mode, "signal": clarify_signal},
    }
    if final_meta.get("finish_reason"):
        final_step["finish_reason"] = final_meta["finish_reason"]
    if notes:
        final_step["note"] = "；".join(notes)
    trace.append(final_step)

    return {
        "task_id": task_id,
        "variant": "improved",
        "run_index": run_index,
        "status": status,
        "answer": answer,
        "citations": citations,
        "trace": trace,
        "usage": usage,
        "toolset": _toolset_name(toolbox),
        "evidence": _public_evidence(evidence),
        "calc": list(getattr(toolbox, "calcs", []) or []),
    }


async def run(
    task: dict,
    variant: str = "baseline",
    run_index: int = 1,
    user_context=None,
    clear_cache: bool = True,
    toolset: Any = None,
) -> dict:
    """跑一条任务，返回可直接入库的 result。

    `toolset` 为空时使用制度库工具集（`Toolbox`）；传 `FinanceToolset()`
    就切到金融文档。runner 本身不认识任何具体工具名。
    `user_context` 只影响工具层的 ACL：None = 评测/内部调用，不过滤；
    线上入口必须传，否则未授权文档会进入模型上下文。
    `clear_cache` 默认 True 供评测使用（避免跨次运行命中缓存）；线上传 False。
    """
    if variant not in SUPPORTED_VARIANTS:
        raise ValueError(f"variant={variant!r} 不在支持范围（仅 {SUPPORTED_VARIANTS}）")

    task_id = str(task.get("id") or task.get("task_id") or "unknown")
    question = str(task.get("question") or "").strip()
    if not question:
        raise ValueError("task.question 不能为空")

    max_steps = _normalize_max_steps(task.get("max_steps"))
    clarify_mode = str(task.get("clarify_mode") or "none")
    injections = task.get("inject") if task.get("mock") else None

    if toolset is None:
        toolbox = Toolbox(injections, user_context=user_context)
    else:
        toolbox = toolset
        # 复用同一个工具集实例连跑多条任务时，由 prepare 清掉上一条的
        # calcs / 调用计数并换成本条的注入计划。没有这个方法（Toolbox）就跳过。
        prepare = getattr(toolbox, "prepare", None)
        if prepare is not None:
            prepare(injections)

    if variant == "improved":
        return await _run_improved(
            task_id=task_id,
            question=question,
            max_steps=max_steps,
            clarify_mode=clarify_mode,
            toolbox=toolbox,
            citation_required=bool(task.get("citation_required")),
            run_index=run_index,
        )

    # 每次运行前清检索缓存：否则第 2/3 次运行会命中 300s TTL 的缓存，
    # latency_ms 被系统性低估，"多调一次工具"和"缓存命中"就分不开了。
    # 金融工具集不走向量检索，没有这条缓存。
    if clear_cache and _toolset_name(toolbox) == "retrieval":
        from app.services.hybrid_search import clear_retrieval_cache

        clear_retrieval_cache()

    messages = [
        {"role": "system", "content": toolbox.system_prompt("baseline")},
        {"role": "user", "content": question},
    ]
    trace: list[dict] = []
    evidence: list[dict] = []
    locators: list[dict] = []
    seen_chunk_ids: set[str] = set()
    seen_locator_ids: set[str] = set()
    usage = {"steps": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0}
    notes: list[str] = []

    started_at = time.perf_counter()
    executed = 0
    budget_exceeded = False
    failed = False
    answer = ""
    final_latency_ms = 0
    final_tokens_in = 0
    final_tokens_out = 0
    final_meta: dict = {}

    try:
        llm_started_at = time.perf_counter()
        text, tool_calls, _, round_usage = await model_provider.chat_with_tools(
            messages, toolbox.specs(), reasoning_mode="flash", temperature=TOOL_TEMPERATURE
        )
        final_latency_ms = _elapsed_ms(llm_started_at)
        final_tokens_in, final_tokens_out = _accumulate_tokens(usage, round_usage)
        final_meta = round_usage or {}

        # baseline：最多一轮工具。拿到结果就进终轮生成，不再二次检索。
        if tool_calls:
            for call in tool_calls:
                if executed >= max_steps:
                    budget_exceeded = True
                    break
                executed += 1
                trace.append(
                    await _execute_tool_call(
                        toolbox, call, executed, evidence, seen_chunk_ids, locators, seen_locator_ids
                    )
                )

            if evidence:
                content = toolbox.final_template.format(evidence=_format_evidence(evidence))
                # 定位结果也要给 baseline 看：它的短板是"不能再读一轮"，不是"看不见结果"。
                # 只给摘要不给原文，正好对应"chunk RAG 只能看到检索片段"这个对照组设定。
                if locators:
                    content = f"{content}\n\n{_format_locators(locators)}"
                messages.append({"role": "user", "content": content})
            elif locators:
                messages.append({
                    "role": "user",
                    "content": (
                        f"{_format_locators(locators)}\n\n"
                        "本轮只拿到章节定位，没有读到原文。请据此回答："
                        "说明还需要读哪一节才能确定，不要猜测数字。"
                    ),
                })
            else:
                messages.append({
                    "role": "user",
                    "content": "本轮检索没有得到可用段落。请据此回答：资料不足时直接说明无法确定，不要猜测。",
                })

            llm_started_at = time.perf_counter()
            final_text, _, final_usage = await model_provider.chat(
                messages,
                reasoning_mode="flash",
                temperature=TOOL_TEMPERATURE,
                max_tokens=FINAL_MAX_TOKENS,
            )
            final_latency_ms = _elapsed_ms(llm_started_at)
            final_tokens_in, final_tokens_out = _accumulate_tokens(usage, final_usage)
            final_meta = final_usage or {}
            answer = (final_text or "").strip()
        else:
            answer = (text or "").strip()
    except Exception as exc:  # noqa: BLE001 - 运行失败要落成 failed 而不是丢记录
        failed = True
        notes.append(f"run_error: {type(exc).__name__}: {exc}")
        logger.exception("Agent run failed: task=%s", task_id)

    if budget_exceeded:
        notes.append("budget_truncated")
    if not failed and not trace:
        notes.append("no_tool_call")
    if not failed and not answer:
        # 空答案有两种成因：撞到 max_tokens，或 token 全花在 reasoning 上。
        # 不区分就只能看到"这条失败了"，没法判断该调预算还是该换模型。
        if final_meta.get("finish_reason") == "length":
            notes.append("empty_final_answer:max_tokens")
        elif final_meta.get("has_reasoning"):
            notes.append("empty_final_answer:reasoning_only")
        else:
            notes.append("empty_final_answer")
    # 注入没打中（比如计划第 2 次调用故障，但这次只查了 1 次）：这一轮不能算恢复题的
    # 有效样本，必须显式标出来，否则恢复率会被"没触发"的运行稀释。
    planned = getattr(toolbox, "planned_injections", 0)
    fired = getattr(toolbox, "fired_injections", 0)
    if planned and fired < planned:
        notes.append(
            f"injection_not_triggered:{fired}/{planned}"
        )

    citations = _extract_citations(answer, evidence)
    clarify_signal = bool(answer) and any(signal in answer for signal in CLARIFY_SIGNALS)
    status = _decide_status(
        answer=answer,
        executed=executed,
        clarify_mode=clarify_mode,
        failed=failed,
    )
    if status == "answered" and not citations and task.get("citation_required"):
        notes.append("citation_missing")

    usage["steps"] = executed
    usage["latency_ms"] = _elapsed_ms(started_at)

    final_step = {
        "step": executed + 1,
        "tool": "__final__",
        "args": {},
        "ok": not failed,
        "result_ids": [],
        "result_count": 0,
        "injected": None,
        "latency_ms": final_latency_ms,
        "tokens_in": final_tokens_in,
        "tokens_out": final_tokens_out,
        "self_eval": None,
        # 澄清信号：`mode` 是任务期望，`signal` 是这次文本里是否出现追问/请求确认的词。
        # **`signal` 只是关键词命中，不是判定**：实测 6 条澄清任务里漏判 1 条
        # （C2 的"如果你要办的是借调而非正式转岗…"）、误判 1 条（C5 该直接答却命中词表）。
        # 澄清触发率与误澄清率必须由 P3 的 judge 判，这一项只作辅助线索。
        "clarify": {"mode": clarify_mode, "signal": clarify_signal},
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
        "toolset": _toolset_name(toolbox),
        "evidence": _public_evidence(evidence),
        "calc": list(getattr(toolbox, "calcs", []) or []),
    }
