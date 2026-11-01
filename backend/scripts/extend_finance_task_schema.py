"""给金融任务集补 Case Schema v2 字段：路由目标、预算与题型元数据。

为什么用脚本而不是手改 52 行：手改会漏、会改错题面；脚本只追加字段，
不碰 question / answer_points / evidence / calc。跑完仍要过
`python -m evaluation.lint_finance_tasks --tier ...`。

字段口径：

- `route_target`：路由层设计目标，取 `agent` / `structured_rag` / `indeterminate`；
  不确定题不进入路由准确率分母。旧 `expected_arm` 仍为历史报告保留。
- `expected_arm`：旧版路由金标。只看任务结构，不看 router 关键词：
  - `multi_hop` / `complex` → agent；
  - `single_hop` / `no_clarify_control` → structured_rag（除非问题点名 ≥2 家公司
    或 ≥2 个期间）；
  - `abstain` / `clarify` / `assume` / `unit_trap` / `false_premise` / `recovery`
    与 mock 任务 → 不写（两臂都可能对，硬判会编金标）。
- `token_budget`：成本闸门用的逐 case 上限。初始值来自 2026-10-28 探针实测
  （33k–45k token/题，CX-10 61.8k），按题型留余量；**未校准**，报告里必须
  标 `calibrated=false`，等正式 baseline 再收紧。

用法（在 backend 目录下）：

    python scripts/extend_finance_task_schema.py --write
    python scripts/extend_finance_task_schema.py          # 只预览统计
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.block_index import companies_for_question  # noqa: E402
from app.finance.block_index import periods_for_question  # noqa: E402

DEFAULT_FILES = (
    "evaluation/finance_tasks.jsonl",
    "evaluation/finance_tasks_complex.jsonl",
    "evaluation/finance_tasks_stress.jsonl",
)
MARKER = "# 2026-10-31 Case Schema v2：补 route_target / token_budget / 题型元数据（不改题面与答案）"

EXCLUDED_QUERY_TYPES = {
    "abstain",
    "clarify",
    "assume",
    "unit_trap",
    "false_premise",
    "recovery",
}


def expected_arm(task: dict) -> str | None:
    if task.get("mock"):
        return None
    query_type = str(task.get("query_type") or "")
    if query_type in EXCLUDED_QUERY_TYPES:
        return None
    if query_type in ("multi_hop", "complex"):
        return "agent"
    if query_type in ("single_hop", "no_clarify_control", ""):
        question = str(task.get("question") or "")
        if len(companies_for_question(question)) >= 2:
            return "agent"
        if len(periods_for_question(question) or []) >= 2:
            return "agent"
        return "structured_rag"
    return None


def route_target(task: dict) -> str | None:
    """返回可自动推导的路由目标；边界题留给人工标为 indeterminate。"""
    return expected_arm(task)


def prompt_style(task: dict) -> str:
    """区分自然提问与带直接约束的题面，避免混合解释能力。"""
    markers = ("请", "注意", "自行", "验算", "确认", "说明", "注明", "是否", "一致", "换算", "分别", "给出")
    question = str(task.get("question") or "")
    return "direct_constraint" if any(marker in question for marker in markers) else "open_question"


def retrieval_hops(task: dict) -> int:
    query_type = str(task.get("query_type") or "")
    return 2 if query_type in ("multi_hop", "complex") else 1


def answer_operations(task: dict) -> list[str]:
    query_type = str(task.get("query_type") or "")
    if task.get("mock") or query_type == "recovery":
        return ["recover"]
    if task.get("should_abstain") or query_type == "abstain":
        return ["abstain"]
    if query_type in ("clarify", "assume"):
        return ["clarify"]
    question = str(task.get("question") or "")
    result = ["lookup"]
    if any(marker in question for marker in ("比", "同比", "增减", "相差", "占", "排序", "一致", "超过", "变动")):
        result.append("compare")
    if isinstance(task.get("calc"), dict):
        result.append("calculate")
    if query_type in ("false_premise", "unit_trap"):
        result.append("validate")
    if task.get("citation_required"):
        result.append("cite")
    return list(dict.fromkeys(result))


def token_budget(task: dict) -> int:
    complex_type = str(task.get("complex_type") or "")
    query_type = str(task.get("query_type") or "")
    if complex_type == "insufficient":
        return 55000
    if complex_type in ("cross_table", "cross_doc", "calc"):
        return 60000
    if query_type == "multi_hop":
        return 55000
    if query_type in ("clarify", "assume"):
        return 25000
    if query_type == "recovery":
        return 50000
    if query_type == "abstain":
        return 45000
    return 45000


def _split_header(lines: list[str]) -> tuple[list[str], list[str]]:
    header: list[str] = []
    body: list[str] = []
    in_header = True
    for line in lines:
        stripped = line.strip()
        if in_header and (not stripped or stripped.startswith("#")):
            header.append(line.rstrip("\n"))
            continue
        in_header = False
        if stripped:
            body.append(stripped)
    return header, body


def process(path: Path, *, write: bool) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    header, body = _split_header(lines)
    tasks = [json.loads(line) for line in body]
    added_arm = 0
    added_budget = 0
    added_metadata = 0
    for task in tasks:
        arm = expected_arm(task)
        if arm and not task.get("expected_arm") and not task.get("route_target"):
            task["expected_arm"] = arm
            added_arm += 1
        target = route_target(task)
        if not task.get("route_target") and target:
            task["route_target"] = target
        if not task.get("prompt_style"):
            task["prompt_style"] = prompt_style(task)
            task["retrieval_hops"] = retrieval_hops(task)
            task["answer_operations"] = answer_operations(task)
            added_metadata += 1
        if not task.get("token_budget"):
            task["token_budget"] = token_budget(task)
            added_budget += 1

    if write:
        if MARKER not in header:
            header.append(MARKER)
        payload = "\n".join(header + [json.dumps(task, ensure_ascii=False) for task in tasks]) + "\n"
        path.write_text(payload, encoding="utf-8")

    return {
        "file": str(path),
        "tasks": len(tasks),
        "added_expected_arm": added_arm,
        "added_token_budget": added_budget,
        "added_metadata": added_metadata,
        "route_target_counts": {
            "agent": sum(1 for task in tasks if task.get("route_target") == "agent"),
            "structured_rag": sum(
                1 for task in tasks if task.get("route_target") == "structured_rag"
            ),
            "indeterminate": sum(
                1 for task in tasks if task.get("route_target") == "indeterminate"
            ),
        },
        "expected_arm_counts": {
            "agent": sum(1 for task in tasks if task.get("expected_arm") == "agent"),
            "structured_rag": sum(
                1 for task in tasks if task.get("expected_arm") == "structured_rag"
            ),
            "unset": sum(1 for task in tasks if not task.get("expected_arm")),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="补金融任务集 Case Schema v2 字段")
    parser.add_argument("--write", action="store_true", help="不传只预览，不落盘")
    args = parser.parse_args()

    for relative in DEFAULT_FILES:
        path = _BACKEND_DIR / relative
        summary = process(path, write=args.write)
        print(
            f"{summary['file']}: {summary['tasks']} 条；"
            f"新增 expected_arm {summary['added_expected_arm']}、"
            f"token_budget {summary['added_token_budget']}、"
            f"题型元数据 {summary['added_metadata']}；"
            f"路由目标 {summary['route_target_counts']}"
        )
    if not args.write:
        print("\n预览模式：未落盘。加 --write 才写回。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
