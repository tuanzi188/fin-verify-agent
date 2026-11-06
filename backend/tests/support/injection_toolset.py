"""制度库工具集的测试替身：只保留故障注入，不做真实检索。

原实现（`app/tools/mock.py` + `app/tools/retrieval.py`）随通用 RAG 一起下线。
runner 的恢复与纠错维度仍需要可复现的空结果、超时、500 与截断，
所以把这一层留在测试侧：接口、返回结构、错误类型与真实工具一致，
但 `dispatch` 只返回用例给定的数据。
"""

from typing import Any

from app.agent.prompts import BASELINE_SYSTEM, FINAL_EVIDENCE_TEMPLATE, IMPROVED_SYSTEM
from app.tools.contract import TOOL_SPECS, ToolError
from app.tools.toolset import ToolOutcome, evidence_item

INJECTION_MODES = ("empty", "timeout", "500", "truncated")

# 必须明显小于语料 chunk 的长度，否则截断注入等于没注入。
_TRUNCATE_CHARS = 12

#: 由用例 monkeypatch；默认返回空，保证不会误打真实检索。
async def dispatch(name: str, arguments: dict, user_context=None) -> Any:
    return [] if name == "search_kb" else {"found": False}


def summarize_result(name: str, data: Any) -> tuple[list[str], int]:
    """给 trace 用：从工具返回里抽 result_ids 与 result_count。"""
    if name == "search_kb":
        if not isinstance(data, list):
            return [], 0
        ids = [str(item.get("chunk_id", "")) for item in data if item.get("chunk_id")]
        return ids, len(data)
    if name == "get_citation":
        if not isinstance(data, dict) or not data.get("found"):
            return [], 0
        chunk_id = str(data.get("chunk_id", ""))
        return [chunk_id], 1
    return [], 0


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
        data = [] if name == "search_kb" else {
            "found": False,
            "chunk_id": (arguments or {}).get("chunk_id", ""),
            "error": "未找到该段落",
        }
        ids, count = summarize_result(name, data)
        return ToolOutcome(
            tool=name, ok=True, data=data, result_ids=ids, result_count=count, injected="empty"
        )

    # truncated：先取用例数据，再截断正文
    try:
        data = await dispatch(name, arguments or {}, user_context=user_context)
    except ToolError as exc:
        return ToolOutcome(
            tool=name, ok=False, injected="truncated",
            error_kind=exc.kind, error_detail=exc.detail,
        )
    data = _truncate_payload(name, data)
    ids, count = summarize_result(name, data)
    return ToolOutcome(
        tool=name, ok=True, data=data, result_ids=ids, result_count=count, injected="truncated"
    )


class InjectionToolset:
    """与真实工具集同接口，按 (call_index) 注入故障。"""

    name = "retrieval"
    search_tool = "search_kb"
    final_template = FINAL_EVIDENCE_TEMPLATE
    max_tool_rounds = 3

    def __init__(self, injections: Any = None, user_context=None):
        self._modes = normalize_injections(injections)
        self._calls = 0
        self._fired = 0
        self._user_context = user_context

    def prepare(self, injections: Any = None) -> None:
        self._modes = normalize_injections(injections)
        self._calls = 0
        self._fired = 0

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
