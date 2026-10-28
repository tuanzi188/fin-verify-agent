"""工具集契约：runner 只认三件事——工具描述、怎么调、结果怎么变成证据。

为什么要这层：评测引擎要能换域（制度库 / 金融文档 / 以后的合同库），
换域时 runner 一行都不该改。所以把"工具长什么样"从 runner 里挪出来：

| 谁提供 | 内容 |
|---|---|
| `specs()` | 喂给模型的 function-calling schema |
| `system_prompt(variant)` | 该域的工具说明与纪律 |
| `call(name, args)` | 执行并返回 `ToolOutcome`（失败也要可归因） |
| `to_evidence(name, data)` | 把工具返回变成带编号的证据项 |

`to_evidence` 返回的每一项可以有 `citable=False`：像 `search_sections`
这种"只定位不算证据"的返回，要参与去重和空结果判断，但不能进编号列表——
否则模型会把一个章节摘要当依据引出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class ToolOutcome:
    """一次工具调用的结果，字段直接映射到 trace 的一步。"""

    tool: str
    ok: bool
    data: Any = None
    result_ids: list[str] = field(default_factory=list)
    result_count: int = 0
    injected: Optional[str] = None
    error_kind: Optional[str] = None
    error_detail: str = ""


#: 注入模式：空结果 / 超时 / 上游错误 / 截断正文
INJECTION_MODES = ("empty", "timeout", "500", "truncated")


class Injector:
    """按"第几次工具调用"注入故障，与具体工具集无关。

    分两层的原因：注入只是"第 N 次调用该坏"，坏成什么样由工具集决定
    （制度库坏在 chunk 上，金融坏在表 / 单元格上）。次数与统计只有这一处实现，
    `planned_injections` / `fired_injections` 才不会各算各的。

    用法：工具集在 `call` 开头取 `mode = injector.take()`；None 就正常执行。
    """

    def __init__(self, injections: Any = None):
        self._plan = self._normalize(injections)
        self._calls = 0
        self._fired = 0

    @staticmethod
    def _normalize(injections: Any) -> dict[int, str]:
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

    def take(self) -> Optional[str]:
        self._calls += 1
        mode = self._plan.get(self._calls)
        if mode is not None:
            self._fired += 1
        return mode

    @property
    def planned(self) -> int:
        return len(self._plan)

    @property
    def fired(self) -> int:
        return self._fired

    @property
    def calls(self) -> int:
        return self._calls


class Toolset(Protocol):
    """工具集最小接口。runner 只依赖这些成员。

    `prepare(injections)` 是可选的：评测侧复用同一个工具集实例连跑多条任务时，
    由它清掉上一条的 `calcs` / 调用计数并换上本条的注入计划。
    没有这个方法（例如制度库 `Toolbox` 在构造时就固定了注入）也不会出错。
    """

    #: 工具集名字，写进 run_config，便于报告区分跑的是哪套工具
    name: str
    #: 检索类工具的名字。空结果自动重试、query 改写都作用在它上面
    search_tool: str

    def specs(self) -> list[dict]: ...

    def system_prompt(self, variant: str) -> str: ...

    async def call(self, name: str, arguments: dict) -> ToolOutcome: ...

    def to_evidence(self, name: str, data: Any) -> list[dict]: ...


def evidence_item(
    *,
    chunk_id: str,
    title: str,
    content: str,
    kind: str = "chunk",
    similarity: Optional[float] = None,
    page: Optional[int] = None,
    path: Optional[list[str]] = None,
    citable: bool = True,
    **extra: Any,
) -> dict:
    """统一的证据项形状。

    `chunk_id` 是引用锚点：制度库是 chunk_id，金融文档是 `doc_id#section_id`
    或 `doc_id#table_id`。agent_checks 用 `[n]` → 这个字段做引用校验。
    """
    item = {
        "chunk_id": str(chunk_id or ""),
        "title": str(title or ""),
        "content": str(content or ""),
        "kind": kind,
        "citable": citable,
    }
    if similarity is not None:
        item["similarity"] = round(float(similarity), 4)
    if page is not None:
        item["page"] = int(page)
    if path:
        item["path"] = list(path)
    item.update(extra)
    return item
