"""故障注入包装。

恢复与纠错维度不能等自然故障：真实空结果不会刚好 6 条，也不可复现。
这里按 `(task_id, call_index)` 注入，任务卡里写 `mock: true` + `inject`。

纪律三条：

1. **Agent 看不到 mock**：接口、返回结构、错误类型与真实工具完全一致；
   注入计划只存在于任务卡和 trace 里，不进 Agent 上下文。
2. **同一接口，不同响应**：不是另造一套假工具，而是在真实工具前面挂一层。
3. **`mock: true` 的任务排除出召回诊断**：它的检索结果是被人为改过的，
   拿去算 recall@k 会把"人造故障"算成"召回失败"。

注入模式：

| 模式 | 行为 |
|---|---|
| `empty` | 返回空（search_kb → `[]`；get_citation → `found=false`） |
| `timeout` | 抛 `ToolError(timeout)` |
| `500` | 抛 `ToolError(upstream_error)` |
| `truncated` | 真实检索后把正文截断到 40 字，标 `truncated=true` |
"""

from typing import Any

from app.agent.prompts import BASELINE_SYSTEM, FINAL_EVIDENCE_TEMPLATE, IMPROVED_SYSTEM
from app.tools.contract import TOOL_SPECS, ToolError
from app.tools.retrieval import dispatch, summarize_result
from app.tools.toolset import ToolOutcome, evidence_item

INJECTION_MODES = ("empty", "timeout", "500", "truncated")

# 这个值必须明显小于语料 chunk 的长度（现有 chunk 约 25–70 字），
# 否则截断注入等于没注入——实测 40 字时，25 字的 chunk 原样返回，
# V6 任务"被截断"却毫发无损地答对了。
_TRUNCATE_CHARS = 12


def normalize_injections(injections: Any) -> dict[int, str]:
    """把任务卡的注入计划规整成 {call_index: mode}，非法项静默丢弃。"""
    plan: dict[int, str] = {}
    if not isinstance(injections, list):
        return plan
    for entry in injections:
        if not isinstance(entry, dict):
            continue
        try:
            call_index = int(entry.get("call_index"))
        except (TypeError, ValueError):
            continue
        mode = str(entry.get("mode") or "")
        if call_index >= 1 and mode in INJECTION_MODES:
            plan[call_index] = mode
    return plan


def _truncate_payload(name: str, data: Any) -> Any:
    def cut(text: str) -> str:
        if not isinstance(text, str) or len(text) <= _TRUNCATE_CHARS:
            return text
        return text[:_TRUNCATE_CHARS] + "…[截断]"

    if name == "search_kb" and isinstance(data, list):
        return [{**item, "content": cut(item.get("content", "")), "truncated": True} for item in data]
    if name == "get_citation" and isinstance(data, dict) and data.get("found"):
        return {**data, "content": cut(data.get("content", "")), "truncated": True}
    return data


async def _injected_outcome(name: str, arguments: dict, mode: str, user_context=None) -> ToolOutcome:
    if mode == "timeout":
        return ToolOutcome(
            tool=name, ok=False, injected="timeout",
            error_kind="timeout", error_detail="注入：工具超时",
        )
    if mode == "500":
        return ToolOutcome(
            tool=name, ok=False, injected="500",
            error_kind="upstream_error", error_detail="注入：上游 500",
        )
    if mode == "empty":
        data = [] if name == "search_kb" else {"found": False, "chunk_id": (arguments or {}).get("chunk_id", ""), "error": "未找到该段落"}
        ids, count = summarize_result(name, data)
        return ToolOutcome(tool=name, ok=True, data=data, result_ids=ids, result_count=count, injected="empty")

    # truncated：先真实检索，再截断正文
    try:
        data = await dispatch(name, arguments or {}, user_context=user_context)
    except ToolError as exc:
        return ToolOutcome(
            tool=name, ok=False, injected="truncated",
            error_kind=exc.kind, error_detail=exc.detail,
        )
    data = _truncate_payload(name, data)
    ids, count = summarize_result(name, data)
    return ToolOutcome(tool=name, ok=True, data=data, result_ids=ids, result_count=count, injected="truncated")


class Toolbox:
    """工具调用入口。没有注入计划时就是真实工具的薄壳。

    `user_context` 只用于 ACL：None = 评测/内部调用；线上入口必须传。
    """

    name = "retrieval"
    search_tool = "search_kb"
    final_template = FINAL_EVIDENCE_TEMPLATE
    max_tool_rounds = 3

    def __init__(self, injections: Any = None, user_context=None):
        self._modes = normalize_injections(injections)
        self._calls = 0
        self._fired = 0
        self._user_context = user_context

    def specs(self) -> list[dict]:
        return TOOL_SPECS

    def system_prompt(self, variant: str) -> str:
        return IMPROVED_SYSTEM if variant == "improved" else BASELINE_SYSTEM

    def to_evidence(self, name: str, data: Any) -> list[dict]:
        if name == "search_kb" and isinstance(data, list):
            return [
                evidence_item(
                    chunk_id=item.get("chunk_id", ""),
                    title=item.get("title", ""),
                    content=item.get("content", ""),
                    kind="chunk",
                    similarity=(
                        float(item["similarity"])
                        if isinstance(item.get("similarity"), (int, float))
                        else None
                    ),
                )
                for item in data
                if isinstance(item, dict)
            ]
        if name == "get_citation" and isinstance(data, dict) and data.get("found"):
            return [
                evidence_item(
                    chunk_id=data.get("chunk_id", ""),
                    title=data.get("title", ""),
                    content=data.get("content", ""),
                    kind="chunk",
                )
            ]
        return []

    @property
    def planned_injections(self) -> int:
        return len(self._modes)

    @property
    def fired_injections(self) -> int:
        return self._fired

    @property
    def calls(self) -> int:
        return self._calls

    async def call(self, name: str, arguments: dict) -> ToolOutcome:
        self._calls += 1
        mode = self._modes.get(self._calls)
        if mode is not None:
            self._fired += 1
            return await _injected_outcome(name, arguments or {}, mode, user_context=self._user_context)
        try:
            data = await dispatch(name, arguments or {}, user_context=self._user_context)
        except ToolError as exc:
            return ToolOutcome(
                tool=name, ok=False,
                error_kind=exc.kind, error_detail=exc.detail,
            )
        ids, count = summarize_result(name, data)
        return ToolOutcome(tool=name, ok=True, data=data, result_ids=ids, result_count=count)
