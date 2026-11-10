"""runner 的收尾公共层：两个 variant 共用的 notes / 定稿 / usage / 返回结构。

只放纯函数：不发起模型调用，也不读取 toolbox 的可变状态——`planned_injections`
等由调用方取值后传入。多轮、闸门、自检等 variant 分叉留在 runner.py，不在这里统一。

`runner.py` 会把这些名字重新导出，现有 `from app.agent.runner import ...` 不受影响。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from app.tools.contract import ABSTAIN_KEYWORDS

# 模型有时输出 [1]，有时输出【2，PDF 第 49 页，表 ...】。
# 只取方括号后的第一个编号；不要匹配 t0011 这类标识符。
_MARKER_PATTERN = re.compile(r"[\[【]\s*(\d{1,2})(?=[，,、\s\]】])")
_SENTENCE_BOUNDARY = re.compile(r"[。！？\n]")

_HEADING_LABELS = {"结论", "答案", "回答", "判断", "总结", "概要"}

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

#: 模型偶尔把工具调用标记当正文输出（2026-11-03 全量：FIN-04 的 DSML、
#: CX-05 的 <tool_call>）。这些标记不是答案，必须剥掉并留痕；
#: 剥离后为空或只剩过程话术时，由状态判定把它们降级，不能当有效回答。
_TOOL_MARKUP_PATTERN = re.compile(
    r"<tool_call>.*?</tool_call>"
    r"|<｜｜DSML｜｜[^>]*>"
    r"|</｜｜DSML｜｜[^>]*>"
    r"|<\|[^|]*\|>",
    re.DOTALL,
)

_FENCED_BLOCK_PATTERN = re.compile(
    r"```[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)\r?\n?```",
    re.DOTALL,
)


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


def _usage_telemetry(
    calls: list[dict],
    *,
    tool_result_chars: int = 0,
    tool_payload_chars: int = 0,
    duplicate_skipped: int = 0,
    final_context_tokens: int | None = None,
) -> dict:
    """把逐轮调用折成 usage 里的可比较字段。

    `final_context_tokens` 必须由调用方显式传“最终答案那次调用”的输入 token；
    不能直接取 `calls[-1]`，因为覆盖自检发生在答案之后，会把它顶掉。
    """
    if final_context_tokens is None:
        final_context_tokens = calls[-1]["tokens_in"] if calls else 0
    return {
        "llm_calls": len(calls),
        "max_context_tokens": max((call["tokens_in"] for call in calls), default=0),
        "final_context_tokens": final_context_tokens,
        "max_messages_chars": max((call["messages_chars"] for call in calls), default=0),
        "tool_result_chars": tool_result_chars,
        "tool_payload_chars": tool_payload_chars,
        "duplicate_skipped": duplicate_skipped,
    }


def _iter_json_objects(text: str):
    """按括号配对找出文本里的 JSON 对象，跳过字符串内的括号。"""
    index = 0
    length = len(text)
    while index < length:
        start = text.find("{", index)
        if start < 0:
            break
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for pos in range(start, length):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break
        if end < 0:
            break
        yield start, end, text[start:end]
        index = end


def _extract_text_tool_call(text: str) -> dict | None:
    """从正文里找出「模型把工具调用写成 JSON」的形态。

    只认两类形状，避免误伤普通 JSON：
    - `{"tool": "search_kb", "query": "..."}`
    - `{"name": "get_rows", "arguments": {...}}`
    """
    for start, end, raw in _iter_json_objects(str(text or "")):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        has_tool = isinstance(data.get("tool"), str)
        has_name = (
            isinstance(data.get("name"), str)
            and isinstance(data.get("arguments"), dict)
        )
        if not (has_tool or has_name):
            continue
        name = data.get("tool") if has_tool else data.get("name")
        if not str(name or "").strip():
            continue
        if isinstance(data.get("arguments"), dict):
            arguments = data["arguments"]
        else:
            arguments = {
                key: value
                for key, value in data.items()
                if key not in ("tool", "name", "arguments")
            }
        return {
            "name": str(name).strip(),
            "arguments": arguments,
            "span": (start, end),
        }
    return None


def _strip_fenced_tool_calls(text: str) -> str:
    """整块删掉只包含工具调用 JSON 的代码围栏，别留下空的 ``` 壳。"""

    def replace(match: re.Match) -> str:
        return "" if _extract_text_tool_call(match.group(1)) else match.group(0)

    return _FENCED_BLOCK_PATTERN.sub(replace, text)


def _strip_tool_markup(answer: str) -> tuple[str, bool]:
    original = answer or ""
    cleaned = _TOOL_MARKUP_PATTERN.sub("", original)
    cleaned = _strip_fenced_tool_calls(cleaned)
    # 裸 JSON 形式的工具调用也要剥掉，否则会当成答案输出给用户。
    while True:
        embedded = _extract_text_tool_call(cleaned)
        if embedded is None:
            break
        start, end = embedded["span"]
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = cleaned.strip()
    return cleaned, cleaned != original


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


def _available_tool_names(toolbox: Any) -> str:
    try:
        specs = toolbox.specs()
    except Exception:  # noqa: BLE001 - 提示词生成失败不能阻断主流程
        return "（未知）"
    names = [
        str((spec.get("function") or {}).get("name") or "")
        for spec in specs
        if isinstance(spec, dict)
    ]
    return "、".join(name for name in names if name) or "（未知）"


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
            "snapshot_truncated": len(str(item.get("content") or "")) > limit,
            "content": str(item.get("content") or "")[:limit],
        }
        if isinstance(item.get("similarity"), (int, float)):
            row["similarity"] = item["similarity"]
        for key in (
            "path",
            "table_id",
            "table_caption",
            "row_name",
            "row_indices",
            "column",
            "column_indices",
            "cell_text",
            "unit",
            "doc_id",
            "source_spans",
        ):
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


def collect_common_notes(
    *,
    budget_exceeded: bool,
    failed: bool,
    trace: list[dict],
    answer: str,
    final_meta: dict,
) -> list[str]:
    """两个 variant 共用的收尾 notes；顺序与原先逐行一致。"""
    notes: list[str] = []
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
    elif not failed and final_meta.get("finish_reason") == "length":
        # 正文非空但撞上限：答案可能在半句处断掉，完整性不可信（2026-11-03 FIN-09）。
        notes.append("answer_truncated")
    return notes


def injection_note(planned: int, fired: int) -> str | None:
    """注入没打中（计划第 2 次调用故障，但这次只查了 1 次）要显式留痕。"""
    if planned and fired < planned:
        return f"injection_not_triggered:{fired}/{planned}"
    return None


def finalize_answer(
    *,
    answer: str,
    evidence: list[dict],
    executed: int,
    clarify_mode: str,
    failed: bool,
    citation_required: bool,
    notes: list[str],
) -> tuple[str, list[dict], bool, str]:
    """剥工具标记、映射引用、判状态；顺序与原先逐行一致。"""
    answer, markup_stripped = _strip_tool_markup(answer)
    if markup_stripped:
        notes.append("tool_markup_stripped")
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
    return answer, citations, clarify_signal, status


def build_usage(
    *,
    usage: dict,
    executed: int,
    latency_ms: int,
    telemetry: dict,
    extra: dict | None = None,
) -> dict:
    usage["steps"] = executed
    if extra:
        usage.update(extra)
    usage["latency_ms"] = latency_ms
    usage.update(telemetry)
    return usage


def build_final_step(
    *,
    executed: int,
    failed: bool,
    latency_ms: int,
    tokens_in: int,
    tokens_out: int,
    self_eval: dict | None,
    clarify_mode: str,
    clarify_signal: bool,
    finish_reason: str | None,
    notes: list[str],
) -> dict:
    final_step = {
        "step": executed + 1,
        "tool": "__final__",
        "args": {},
        "ok": not failed,
        "result_ids": [],
        "result_count": 0,
        "injected": None,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "self_eval": self_eval,
        "clarify": {"mode": clarify_mode, "signal": clarify_signal},
    }
    if finish_reason:
        final_step["finish_reason"] = finish_reason
    if notes:
        final_step["note"] = "；".join(notes)
    return final_step


def build_result(
    *,
    task_id: str,
    variant: str,
    run_index: int,
    status: str,
    answer: str,
    citations: list[dict],
    trace: list[dict],
    usage: dict,
    llm_calls: list[dict],
    toolbox: Any,
    evidence: list[dict],
) -> dict:
    return {
        "task_id": task_id,
        "variant": variant,
        "run_index": run_index,
        "status": status,
        "answer": answer,
        "citations": citations,
        "trace": trace,
        "usage": usage,
        "llm_calls": llm_calls,
        "toolset": _toolset_name(toolbox),
        "evidence": _public_evidence(evidence),
        "calc": list(getattr(toolbox, "calcs", []) or []),
        "task_plan": (
            toolbox.update_task_plan(evidence)
            if hasattr(toolbox, "update_task_plan") else {}
        ),
    }
