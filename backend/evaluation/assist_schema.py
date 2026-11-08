"""辅助问答任务卡的结构校验。

与金融查证的 `lint_finance_tasks` 分开：概念/联网题没有 `chunk_ids`、没有表格
单元格、没有 `tool_constraints`，硬套那套规则只会报假错。这里只校验
"判分器能不能稳定判"这件事：

- id 唯一、必填字段齐、枚举合法；
- 概念题作答必须有 `key_facts` 且每条至少一个可命中词；
- 费率题必须联网且要求来源（否则"凭记忆报税率"会被判成正确答案）；
- 拒答题必须有六类编码之一；证据类必须带 `synthetic_issues`；
- 禁止内容不能为空（否则编造/超范围没有红线）。

用法：
    python -m evaluation.lint_assist_tasks
    python -m evaluation.lint_assist_tasks --concept
    python -m evaluation.lint_assist_tasks --refusal
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.refusal import CONFLICT_ISSUES, REFUSALS

from evaluation.lint_common import load_tasks

DEFAULT_CONCEPT = _BACKEND_DIR / "evaluation" / "assist_tasks_concept.jsonl"
DEFAULT_REFUSAL = _BACKEND_DIR / "evaluation" / "assist_tasks_refusal.jsonl"
DEFAULT_ADVICE = _BACKEND_DIR / "evaluation" / "assist_tasks_advice.jsonl"

SUITES = ("concept", "refusal", "advice")
STATUSES = ("answered", "abstained", "clarified")
DIFFICULTIES = ("easy", "medium", "hard")
EVIDENCE_CATEGORIES = ("insufficient_evidence", "source_conflict")

REQUIRED_FIELDS = (
    "id", "suite", "category", "question", "expected_status",
    "expected_refusal_code", "key_facts", "forbidden",
    "allow_web", "must_web", "sources_required",
    "holdout", "difficulty", "tags",
)


RESULT_STATUSES = ("answered", "abstained", "clarified", "needs_review", "error")
USAGE_FIELDS = ("latency_ms", "llm_calls", "tokens_in", "tokens_out", "web_queries")


def validate_result(result: dict) -> list[str]:
    """一条辅助问答运行的最小结构校验；与金融 contract 分开。"""
    problems: list[str] = []
    for field in ("task_id", "suite", "run_index", "status", "answer", "usage"):
        if field not in result:
            problems.append(f"缺少字段: {field}")
    if result.get("suite") not in SUITES:
        problems.append(f"suite 非法: {result.get('suite')!r}")
    if result.get("status") not in RESULT_STATUSES:
        problems.append(f"status 非法: {result.get('status')!r}")
    usage = result.get("usage")
    if not isinstance(usage, dict):
        problems.append("usage 必须是对象")
    else:
        for field in USAGE_FIELDS:
            if field not in usage:
                problems.append(f"usage 缺少字段: {field}")
    refusal = result.get("refusal")
    if refusal is not None and not isinstance(refusal, dict):
        problems.append("refusal 必须是对象或 null")
    if result.get("invalid_reason") is not None and not isinstance(
        result.get("invalid_reason"), str
    ):
        problems.append("invalid_reason 必须是字符串或 null")
    return problems


def _tag(task: dict) -> str:
    return str(task.get("id") or f"line{task.get('_line', '?')}")


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _lint_fact(task: dict, index: int, fact: object, errors: list[str], warnings: list[str]) -> None:
    tag = _tag(task)
    if not isinstance(fact, dict):
        errors.append(f"[{tag}] key_facts[{index}] 不是对象")
        return
    if not _is_nonempty_str(fact.get("text")):
        errors.append(f"[{tag}] key_facts[{index}] 缺少 text")
    includes = fact.get("must_include_any")
    if not isinstance(includes, list) or not includes:
        errors.append(f"[{tag}] key_facts[{index}] must_include_any 必须是非空列表")
    else:
        for value in includes:
            if not _is_nonempty_str(value):
                errors.append(f"[{tag}] key_facts[{index}] must_include_any 含空值")
    numbers = fact.get("numbers")
    if not isinstance(numbers, list):
        errors.append(f"[{tag}] key_facts[{index}] numbers 必须是列表")
    else:
        for value in numbers:
            if not _is_nonempty_str(value):
                errors.append(f"[{tag}] key_facts[{index}] numbers 含空值")
    if isinstance(includes, list) and len(includes) == 1 and len(str(fact.get("text") or "")) > 12:
        warnings.append(
            f"[{tag}] key_facts[{index}] 只有一个命中词，定义题容易判死；建议补同义词"
        )


def lint_assist(tasks: list[dict]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    ids = [str(task.get("id") or "") for task in tasks]
    for task_id in sorted({item for item in ids if item}):
        count = ids.count(task_id)
        if count > 1:
            errors.append(f"id 重复: {task_id} 出现 {count} 次")
    if not all(ids):
        errors.append("存在 id 为空的 task")

    for task in tasks:
        tag = _tag(task)
        for field in REQUIRED_FIELDS:
            if field not in task:
                errors.append(f"[{tag}] 缺少字段: {field}")

        suite = task.get("suite")
        if suite not in SUITES:
            errors.append(f"[{tag}] suite 非法: {suite!r}")
        if not _is_nonempty_str(task.get("category")):
            errors.append(f"[{tag}] category 为空")
        if not _is_nonempty_str(task.get("question")):
            errors.append(f"[{tag}] question 为空")
        if task.get("expected_status") not in STATUSES:
            errors.append(f"[{tag}] expected_status 非法: {task.get('expected_status')!r}")
        if task.get("difficulty") not in DIFFICULTIES:
            errors.append(f"[{tag}] difficulty 非法: {task.get('difficulty')!r}")
        if not task.get("forbidden"):
            errors.append(f"[{tag}] forbidden 为空：禁止内容不写，判分器就没有红线")
        if not isinstance(task.get("tags"), list):
            errors.append(f"[{tag}] tags 必须是列表")
        if not isinstance(task.get("holdout"), bool):
            errors.append(f"[{tag}] holdout 必须是布尔值")
        if not isinstance(task.get("allow_web"), bool):
            errors.append(f"[{tag}] allow_web 必须是布尔值")
        if not isinstance(task.get("must_web"), bool):
            errors.append(f"[{tag}] must_web 必须是布尔值")
        if not isinstance(task.get("sources_required"), bool):
            errors.append(f"[{tag}] sources_required 必须是布尔值")

        if task.get("must_web") and not task.get("allow_web"):
            errors.append(f"[{tag}] must_web=true 但 allow_web=false：费率题必须允许联网")
        if task.get("must_web") and not task.get("sources_required"):
            errors.append(f"[{tag}] must_web=true 必须 sources_required=true")

        code = task.get("expected_refusal_code")
        if code is not None and code not in REFUSALS:
            errors.append(f"[{tag}] expected_refusal_code 不在六类里: {code!r}")

        facts = task.get("key_facts")
        if not isinstance(facts, list):
            errors.append(f"[{tag}] key_facts 必须是列表")
            facts = []
        for index, fact in enumerate(facts):
            _lint_fact(task, index, fact, errors, warnings)

        if suite == "concept":
            if task.get("expected_status") == "answered":
                if not facts:
                    errors.append(f"[{tag}] 概念作答任务至少需要 1 条 key_facts")
                if code is not None:
                    errors.append(f"[{tag}] 作答任务不应带 expected_refusal_code")
            else:
                if code is None:
                    errors.append(f"[{tag}] 拒答/澄清任务必须写 expected_refusal_code")
                if facts:
                    errors.append(f"[{tag}] 拒答/澄清任务不应有 key_facts")
            if task.get("sources_required") and not task.get("allow_web"):
                errors.append(f"[{tag}] sources_required=true 但 allow_web=false")
        elif suite == "refusal":
            if not facts:
                pass
            else:
                errors.append(f"[{tag}] 拒答任务不应有 key_facts")
            if task.get("expected_status") not in ("abstained", "clarified"):
                errors.append(f"[{tag}] 拒答任务 expected_status 只能是 abstained/clarified")
            issues = task.get("synthetic_issues") or []
            if not isinstance(issues, list):
                errors.append(f"[{tag}] synthetic_issues 必须是列表")
                issues = []
            category = str(task.get("category") or "")
            if category in EVIDENCE_CATEGORIES:
                if not issues:
                    errors.append(f"[{tag}] 证据类拒答必须带 synthetic_issues")
                codes = {str(issue.get("code") or "") for issue in issues if isinstance(issue, dict)}
                if category == "source_conflict" and not (codes & CONFLICT_ISSUES):
                    errors.append(
                        f"[{tag}] source_conflict 至少需要一个冲突类 issue: "
                        f"{sorted(CONFLICT_ISSUES)}"
                    )
                if category == "insufficient_evidence" and (codes & CONFLICT_ISSUES):
                    errors.append(f"[{tag}] insufficient_evidence 不应带冲突类 issue")
            elif issues:
                errors.append(f"[{tag}] 路由类拒答不应带 synthetic_issues")
            if category not in (
                "ambiguous_scope", "out_of_domain", "unsupported_intent",
                "out_of_scope", *EVIDENCE_CATEGORIES,
            ):
                errors.append(f"[{tag}] refusal category 非法: {category!r}")
        elif suite == "advice":
            if task.get("expected_status") != "answered":
                errors.append(f"[{tag}] 观点任务 expected_status 必须是 answered")
            if code is not None:
                errors.append(f"[{tag}] 观点任务不应带 expected_refusal_code")
            if not facts:
                warnings.append(f"[{tag}] 观点任务没有 key_facts：只能判免责/禁止内容，判不了观点质量")
            if not isinstance(task.get("synthetic_issues") or [], list):
                errors.append(f"[{tag}] synthetic_issues 必须是列表")
            if task.get("synthetic_issues"):
                errors.append(f"[{tag}] 观点任务不应带 synthetic_issues")

    holdout = sum(1 for task in tasks if task.get("holdout"))
    if tasks:
        ratio = holdout / len(tasks)
        if not 0.15 <= ratio <= 0.30:
            warnings.append(
                f"留出比例 {holdout}/{len(tasks)} = {ratio:.0%}，设计目标约 20%"
            )
    return errors, warnings


def _report(name: str, tasks: list[dict]) -> tuple[int, int]:
    errors, warnings = lint_assist(tasks)
    print(f"\n{name}: {len(tasks)} 条")
    for warning in warnings:
        print(f"  WARN {warning}")
    for error in errors:
        print(f"  ERROR {error}")
    if not errors:
        print("  0 error")
    return len(errors), len(warnings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="辅助问答任务卡 lint")
    parser.add_argument("--concept", action="store_true", help="只查概念集")
    parser.add_argument("--refusal", action="store_true", help="只查拒答集")
    parser.add_argument("--advice", action="store_true", help="只查观点集")
    parser.add_argument("--concept-tasks", default=str(DEFAULT_CONCEPT))
    parser.add_argument("--refusal-tasks", default=str(DEFAULT_REFUSAL))
    parser.add_argument("--advice-tasks", default=str(DEFAULT_ADVICE))
    args = parser.parse_args(argv)

    selected = []
    if args.concept:
        selected.append("concept")
    if args.refusal:
        selected.append("refusal")
    if args.advice:
        selected.append("advice")
    if not selected:
        selected = ["concept", "refusal", "advice"]

    paths = {
        "concept": (args.concept_tasks, "概念集"),
        "refusal": (args.refusal_tasks, "拒答集"),
        "advice": (args.advice_tasks, "观点集"),
    }
    total_errors = 0
    for name in selected:
        path, label = paths[name]
        total_errors += _report(label, load_tasks(Path(path)))[0]

    print(f"\n合计 error: {total_errors}")
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
