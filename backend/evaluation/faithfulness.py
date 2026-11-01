"""RAGAS 忠实度（faithfulness）：答案里的每条原子陈述，能否仅从本次召回内容推断。

算法两步，不需要参考答案：

1. **拆陈述**：让模型把答案拆成若干条原子陈述（一条只含一个事实）；
2. **判推断**：逐条判断"仅凭给定上下文能否推断出该陈述"，可推断数 / 总数 = 忠实度。

**为什么本项目要它**：现有 judge 判"要点是否答到"，但不直接回答"答案有没有
超出资料"。忠实度给双臂一把统一的生成质量尺子，也能和 judge 并跑校准——
两者不一致的样本正好进人工复核队列（评测集审计的"无答案等价规则"缺口）。

**口径适配**：RAGAS 原生面向稠密块检索；本项目检索单元是结构切片，
判定文本改成"该题实际注入上下文的切片内容"即可，算法不变。
上下文取 `result["evidence"]`（RAG 臂）或 trace 里读过的 chunk 原文（Agent 臂）。

**成本纪律**：只在探针级并跑（4 题 × 2 臂 ≈ 16 次轻调用），不进全量。
任何真实模型复跑前仍按项目纪律报预算上限。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services import model_provider
from evaluation.agent_checks import base_chunk_id, evidence_chunk_ids
from evaluation.agent_metrics import ratio
from evaluation._runner_common import load_tasks

FAITHFULNESS_SYSTEM = (
    "你是严格的检索增强生成评测员。只依据给定上下文判断，不使用你自己的常识。"
    "只输出 JSON，不输出任何其他文字。"
)

SPLIT_TEMPLATE = """【回答】
{answer}

把回答拆成若干条**原子陈述**：每条只含一个事实，能独立判断真假；
不要合并、不要改写数字、不要补充回答里没有的内容。
没有事实性陈述（例如纯拒答）就返回空数组。

只输出 JSON：
{{"claims": ["陈述 1", "陈述 2"]}}
"""

JUDGE_TEMPLATE = """【上下文】（本次召回并注入给模型的切片）
{context}

【原子陈述】
{claims}

逐条判断：**仅凭上面上下文**，能否推断出该陈述？
- 上下文里没有这个数字/事实，且陈述没有注明是推算 ⇒ supported=false；
- 陈述是"资料不足/无法确定"这类元陈述 ⇒ supported=true（没有断言就没有无据断言）；
- 上下文里有等价数值但单位/口径不同 ⇒ 只有能换算且口径一致才算 supported=true。

只输出 JSON：
{{"claims": [{{"index": 0, "supported": true, "reason": "不超过 30 字"}}]}}
"""

_MAX_CONTEXT_CHARS = 12000
_MAX_CHUNK_CHARS = 1500
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


def _truncate(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def split_claims_fallback(answer: str) -> list[str]:
    """模型拆分失败时的确定性兜底：按句切。粗糙，但比"整段当一条"可解释。"""
    return [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT.split(str(answer or ""))
        if sentence.strip()
    ]


def build_context(result: dict, chunks: Optional[dict[str, dict]] = None) -> str:
    """本次注入的上下文：优先 evidence 正文，其次 trace 读过的 chunk 原文。"""
    blocks: list[str] = []
    seen: set[str] = set()

    for item in result.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id") or "")
        content = str(item.get("content") or "")
        if chunk_id and chunk_id in seen:
            continue
        seen.add(chunk_id)
        blocks.append(f"[{chunk_id}] {_truncate(content, _MAX_CHUNK_CHARS)}")

    if chunks:
        for chunk_id in evidence_chunk_ids(result):
            base = base_chunk_id(chunk_id)
            if base in seen:
                continue
            chunk = chunks.get(base) or chunks.get(chunk_id) or {}
            content = str(chunk.get("content") or "")
            if not content:
                continue
            seen.add(base)
            blocks.append(f"[{base}] {_truncate(content, _MAX_CHUNK_CHARS)}")

    text = "\n\n".join(blocks)
    return _truncate(text, _MAX_CONTEXT_CHARS)


def _parse_json(text: str) -> dict:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出里没有 JSON 对象")
    return json.loads(cleaned[start : end + 1])


async def _default_call(prompt: str, system: str, max_tokens: int):
    return await model_provider.simple_query(prompt, system, max_tokens=max_tokens)


async def split_claims(
    answer: str,
    *,
    call: Optional[Callable[..., Awaitable]] = None,
) -> dict:
    call = call or _default_call
    prompt = SPLIT_TEMPLATE.format(answer=_truncate(answer, 2400))
    errors: list[str] = []
    try:
        content, _, usage = await call(prompt, FAITHFULNESS_SYSTEM, 1024)
        payload = _parse_json(content)
        claims = [str(item).strip() for item in payload.get("claims") or [] if str(item).strip()]
        return {
            "claims": claims,
            "fallback": False,
            "errors": errors,
            "usage": usage or {},
        }
    except Exception as exc:  # noqa: BLE001 - 拆分失败要降级，不能让整条评测失败
        errors.append(f"拆分失败: {type(exc).__name__}: {exc}")
        return {
            "claims": split_claims_fallback(answer),
            "fallback": True,
            "errors": errors,
            "usage": {},
        }


async def judge_claims(
    claims: list[str],
    context: str,
    *,
    call: Optional[Callable[..., Awaitable]] = None,
) -> dict:
    call = call or _default_call
    if not claims:
        return {"supported": 0, "total": 0, "rows": [], "errors": [], "usage": {}}
    lines = "\n".join(f"{index}. {claim}" for index, claim in enumerate(claims))
    prompt = JUDGE_TEMPLATE.format(context=context or "（没有上下文）", claims=lines)
    try:
        content, _, usage = await call(prompt, FAITHFULNESS_SYSTEM, 2048)
        payload = _parse_json(content)
        by_index = {}
        for position, item in enumerate(payload.get("claims") or []):
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index", position))
            except (TypeError, ValueError):
                index = position
            by_index[index] = item
        rows = []
        for index, claim in enumerate(claims):
            item = by_index.get(index) or {}
            rows.append(
                {
                    "index": index,
                    "claim": claim,
                    "supported": bool(item.get("supported")),
                    "reason": str(item.get("reason") or "")[:120],
                    "missing": index not in by_index,
                }
            )
        supported = sum(1 for row in rows if row["supported"])
        return {
            "supported": supported,
            "total": len(rows),
            "rows": rows,
            "errors": [],
            "usage": usage or {},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "supported": 0,
            "total": len(claims),
            "rows": [
                {"index": index, "claim": claim, "supported": False, "reason": "判定失败", "missing": True}
                for index, claim in enumerate(claims)
            ],
            "errors": [f"判定失败: {type(exc).__name__}: {exc}"],
            "usage": {},
        }


async def score_result(
    task: dict,
    result: dict,
    *,
    chunks: Optional[dict[str, dict]] = None,
    call: Optional[Callable[..., Awaitable]] = None,
) -> dict:
    """给一条运行算忠实度。"""
    answer = str(result.get("answer") or "")
    if chunks is None:
        # Agent 的 trace 只带 id，不带正文；不主动回查语料就会拿空上下文判"全不忠实"。
        try:
            from evaluation.finance_corpus import chunk_map

            chunks = chunk_map()
        except Exception:  # noqa: BLE001 - 没有语料时退化成只用 evidence 正文
            chunks = {}
    context = build_context(result, chunks)
    if not answer.strip():
        return {
            "task_id": str(task.get("id")),
            "run_index": int(result.get("run_index") or 1),
            "faithfulness": None,
            "claims": [],
            "supported": 0,
            "total": 0,
            "note": "空回答，忠实度不适用",
            "errors": [],
            "usage": {},
        }
    split = await split_claims(answer, call=call)
    judged = await judge_claims(split["claims"], context, call=call)
    return {
        "task_id": str(task.get("id")),
        "run_index": int(result.get("run_index") or 1),
        "faithfulness": ratio(judged["supported"], judged["total"]),
        "claims": judged["rows"],
        "supported": judged["supported"],
        "total": judged["total"],
        "split_fallback": split["fallback"],
        "note": "没有可拆的原子陈述" if judged["total"] == 0 else None,
        "errors": split["errors"] + judged["errors"],
        "usage": {
            "split": split["usage"],
            "judge": judged["usage"],
        },
    }


def aggregate(rows: list[dict]) -> dict:
    values = [row["faithfulness"] for row in rows if row.get("faithfulness") is not None]
    supported = sum(row.get("supported") or 0 for row in rows)
    total = sum(row.get("total") or 0 for row in rows)
    calls = 2 * len(values)
    return {
        "runs": len(rows),
        "scored_runs": len(values),
        "faithfulness_mean": round(sum(values) / len(values), 4) if values else None,
        "faithfulness_micro": ratio(supported, total),
        "claims_supported": supported,
        "claims_total": total,
        "split_fallbacks": sum(1 for row in rows if row.get("split_fallback")),
        "errors": sum(len(row.get("errors") or []) for row in rows),
        "model_calls": calls,
        "definition": "可仅从注入上下文推断的原子陈述数 / 原子陈述总数；无参考答案",
        "note": "只在探针级并跑，不进全量；与 judge 不一致的样本进人工复核队列。",
    }


def _runs_from_payload(payload: dict) -> list[dict]:
    return list(payload.get("results") or [])


async def score_runs(
    tasks: list[dict],
    payload: dict,
    *,
    chunks: Optional[dict[str, dict]] = None,
    call: Optional[Callable[..., Awaitable]] = None,
    limit: int = 0,
) -> list[dict]:
    tasks_by_id = {str(task.get("id")): task for task in tasks}
    results = [
        result
        for result in _runs_from_payload(payload)
        if str(result.get("task_id")) in tasks_by_id
    ]
    if limit > 0:
        results = results[:limit]
    return [
        await score_result(tasks_by_id[str(result["task_id"])], result, chunks=chunks, call=call)
        for result in results
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGAS 忠实度（探针级并跑）")
    parser.add_argument("--runs", required=True)
    parser.add_argument("--tasks", default="evaluation/finance_tasks_complex.jsonl")
    parser.add_argument("--limit", type=int, default=4, help="默认只跑 4 条探针")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    runs_path = Path(args.runs)
    if not runs_path.is_absolute():
        runs_path = _BACKEND_DIR / runs_path
    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    payload = json.loads(runs_path.read_text(encoding="utf-8"))
    tasks = load_tasks(tasks_path)

    from evaluation.finance_corpus import chunk_map

    rows = asyncio.run(
        score_runs(tasks, payload, chunks=chunk_map(), limit=args.limit)
    )
    summary = aggregate(rows)
    summary["tasks_file"] = str(tasks_path)
    summary["runs_file"] = str(runs_path)

    print("RAGAS 忠实度：")
    print(
        f"  均值 {summary['faithfulness_mean']}   "
        f"微平均 {summary['claims_supported']}/{summary['claims_total']} "
        f"({(summary.get('faithfulness_micro') or 0):.1%})   "
        f"模型调用 {summary['model_calls']} 次"
    )
    for row in rows:
        value = row.get("faithfulness")
        print(
            f"  {row['task_id']}#{row['run_index']}: "
            f"{value if value is not None else 'n/a'} "
            f"({row.get('supported')}/{row.get('total')})"
        )

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({**summary, "rows": rows}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n结果：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
