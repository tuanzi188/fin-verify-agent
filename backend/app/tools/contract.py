"""Agent 工具契约与结果 schema。

三件事集中在这里，避免"工具描述"和"工具行为"各说一套：

1. **工具签名**：`TOOL_SPECS` 是喂给 LLM 的 function-calling schema。
   模型只能看到这里的描述，所以它就是 Agent 的说明书。
2. **分数语义**：`score` / `similarity` / `rrf_score` 三个字段各不相同。
   2026-10-27 修正：此前 `similarity` 实际返回的是 RRF 融合分（rrf_k=60 时最大约
   0.016），而描述写着"0-1、低于 0.35 表示资料不足"——照描述实现会让 Agent
   对每个问题都判定资料不足，拒答维度直接失效。
3. **trace / result schema**：评测按这个判分，字段对齐
   `docs/AGENT_EVAL_PROJECT_SPEC.md` §6.5。

本模块不导入 app 内其他模块，测试可以单独 import。
"""

from typing import Any

# ---------------------------------------------------------------------------
# 工具错误
# ---------------------------------------------------------------------------

TOOL_ERROR_KINDS = ("timeout", "upstream_error", "unavailable", "bad_args")


class ToolError(Exception):
    """工具层可归因的失败。

    Agent 必须把它记进 trace，而不是当成"空结果"——两者在恢复维度里是完全
    不同的信号：空结果该改写 query，超时该重试或降级。
    """

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind if kind in TOOL_ERROR_KINDS else "upstream_error"
        self.detail = detail
        super().__init__(f"{self.kind}: {detail}" if detail else self.kind)


# ---------------------------------------------------------------------------
# 工具描述（模型看到的原文）
# ---------------------------------------------------------------------------

SEARCH_KB_DESCRIPTION = (
    "在企业知识库中检索相关段落，用于查询公司制度、流程、规定、政策类问题。"
    "返回段落 id、所属文档、正文，以及三个分数："
    "score 为重排分（无量纲，只用于相对排序）；"
    "similarity 为向量余弦相似度（通常 0-1），仅当该段落由向量检索召回时有值，否则为 null；"
    "rrf_score 为融合分（量级约 0.016），仅供调试，不要用它判断资料是否充足。"
    "若返回空列表，或所有条目的 similarity 都低于 0.35，说明资料不足，"
    "应如实说明而不是编造。"
)

GET_CITATION_DESCRIPTION = (
    "按 chunk_id 取回原文与出处，用于核对答案依据。"
    "chunk_id 必须来自 search_kb 的返回；未找到时返回 found=false。"
)

SEARCH_KB_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_kb",
        "description": SEARCH_KB_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索用的查询词。用用户问题里的关键实体与限定词，"
                        "去掉「请」「帮我」之类的客套话；不要一次塞进多个不相干的问题。"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数，1-10，默认 5。越界会返回 bad_args 错误。",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

GET_CITATION_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_citation",
        "description": GET_CITATION_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {
                    "type": "string",
                    "description": "search_kb 返回的 chunk_id，例如 d2_ch1。",
                },
            },
            "required": ["chunk_id"],
        },
    },
}

TOOL_SPECS: list[dict[str, Any]] = [SEARCH_KB_SPEC, GET_CITATION_SPEC]

# ---------------------------------------------------------------------------
# 结果 schema
# ---------------------------------------------------------------------------

# 四态与 docs/AGENT_EVAL_PROJECT_SPEC.md §6.5 一致（第四态是 error，不是 failed）
VALID_STATUSES = ("answered", "abstained", "clarified", "error")

RESULT_REQUIRED_FIELDS = (
    "task_id",
    "variant",
    "run_index",
    "status",
    "answer",
    "citations",
    "trace",
    "usage",
)

TRACE_REQUIRED_FIELDS = (
    "step",
    "tool",
    "args",
    "ok",
    "result_ids",
    "result_count",
    "injected",
    "latency_ms",
    "tokens_in",
    "tokens_out",
)

USAGE_REQUIRED_FIELDS = ("steps", "tokens_in", "tokens_out", "latency_ms")

# 与 app.services.rag_engine._CANNOT_ANSWER_KEYWORDS 保持一致。
# tests/test_agent_runner.py 有断言守着，防止两处漂移。
ABSTAIN_KEYWORDS = (
    "无法确定",
    "无法回答",
    "无法提供",
    "资料不足",
    "没有相关信息",
    "不清楚",
    "不知道",
    "抱歉",
)


def validate_result(result: dict) -> list[str]:
    """校验一条 run 结果，返回问题列表（空列表 = 通过）。

    评测前先跑它：schema 漂移会让后面所有聚合静默算错，比报错更难查。
    """
    problems: list[str] = []
    if not isinstance(result, dict):
        return ["result 不是 dict"]

    for field in RESULT_REQUIRED_FIELDS:
        if field not in result:
            problems.append(f"缺少字段: {field}")
    if problems:
        return problems

    if result["status"] not in VALID_STATUSES:
        problems.append(f"status 非法: {result['status']!r}")

    citations = result["citations"]
    if not isinstance(citations, list):
        problems.append("citations 必须是 list")
    else:
        for index, citation in enumerate(citations):
            if not isinstance(citation, dict):
                problems.append(f"citations[{index}] 不是 dict")
                continue
            if not isinstance(citation.get("marker"), int):
                problems.append(f"citations[{index}].marker 必须是 int")
            if not isinstance(citation.get("chunk_id"), str) or not citation.get("chunk_id"):
                problems.append(f"citations[{index}].chunk_id 缺失")

    usage = result["usage"]
    if not isinstance(usage, dict):
        problems.append("usage 必须是 dict")
    else:
        for field in USAGE_REQUIRED_FIELDS:
            if field not in usage:
                problems.append(f"usage 缺少字段: {field}")

    trace = result["trace"]
    if not isinstance(trace, list) or not trace:
        problems.append("trace 必须是非空 list")
    else:
        for index, step in enumerate(trace):
            if not isinstance(step, dict):
                problems.append(f"trace[{index}] 不是 dict")
                continue
            for field in TRACE_REQUIRED_FIELDS:
                if field not in step:
                    problems.append(f"trace[{index}] 缺少字段: {field}")
            if not isinstance(step.get("result_ids"), list):
                problems.append(f"trace[{index}].result_ids 必须是 list")
            if not isinstance(step.get("injected"), (str, type(None))):
                problems.append(f"trace[{index}].injected 必须是 str 或 null")
        if trace[-1].get("tool") != "__final__":
            problems.append("trace 末步必须是 __final__")

    return problems
