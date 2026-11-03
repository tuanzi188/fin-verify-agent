"""任务集可判性检查（文档 §9.4 的九项 + 结构校验）。

用法（在 backend 目录下）：

    python -m evaluation.lint_agent_tasks
    python -m evaluation.lint_agent_tasks --tasks evaluation/agent_tasks.jsonl

退出码：0 = 无 error（warning 不影响退出码）；1 = 有 error。

为什么 lint 是验收项而不是可选项：任务卡写错不会有任何报错，只会让后面的分数
静默算错——比如 chunk_id 打错一个字母，recall@k 诊断就永久偏低；负样本里留了
支撑 chunk，"编造率"就永远测不出来。这类错误只能靠机器查。
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation.agent_corpus import AGENT_CORPUS, INJECTIONS, chunk_map

DEFAULT_TASKS = "evaluation/agent_tasks.jsonl"

DIMENSIONS = ("检索与引用", "多步拆解", "拒答与边界", "澄清与追问", "恢复与纠错")
DIFFICULTIES = ("easy", "medium", "hard")
SOURCES = ("real", "failure", "synthetic")
CLARIFY_MODES = ("ask", "assume", "none")
INJECTION_MODES = ("empty", "timeout", "500", "truncated")

REQUIRED_FIELDS = (
    "id", "dimension", "secondary_tags", "difficulty", "question",
    "tool_constraints", "required_args", "answer_points", "citation_required",
    "should_abstain", "clarify_mode", "forbidden", "max_steps",
    "relevant_chunk_ids", "query_type", "source", "mock",
)

# 单 chunk 被超过这么多条任务引用就告警：任务设计偏食，剖面会集中在同一段文本
CITATION_CONCENTRATION_LIMIT = 5


def load_tasks(path: Path) -> list[dict]:
    tasks: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                task = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no} JSON 解析失败: {exc}") from exc
            if not isinstance(task, dict):
                raise SystemExit(f"{path}:{line_no} 必须是 JSON 对象")
            task["_line"] = line_no
            tasks.append(task)
    return tasks


def lint(
    tasks: list[dict],
    chunks: dict[str, dict],
    profile: bool = True,
    required_fields: tuple[str, ...] | None = None,
) -> tuple[list[str], list[str]]:
    """`profile=False` 用于压力/回归子集：那里的条数、维度配比、澄清配比
    都是按另一套设计走的，拿主集的比例规则去卡只会报假错。

    `required_fields` 允许别的任务集换一份必填清单（金融集没有 `required_args`，
    那是通用制度库的工具参数约束，金融工具层的参数由 `tool_constraints` 表达）。"""
    errors: list[str] = []
    warnings: list[str] = []

    # ---- 结构完整性 ----
    ids = [str(task.get("id") or "") for task in tasks]
    for task_id, count in Counter(ids).items():
        if count > 1:
            errors.append(f"id 重复: {task_id} 出现 {count} 次")
    if not all(ids):
        errors.append("存在 id 为空的 task")

    for task in tasks:
        tag = task.get("id") or f"line{task['_line']}"
        for field in required_fields or REQUIRED_FIELDS:
            if field not in task:
                errors.append(f"[{tag}] 缺少字段: {field}")
        if task.get("dimension") not in DIMENSIONS:
            errors.append(f"[{tag}] dimension 非法: {task.get('dimension')!r}")
        if task.get("difficulty") not in DIFFICULTIES:
            errors.append(f"[{tag}] difficulty 非法: {task.get('difficulty')!r}")
        if task.get("source") not in SOURCES:
            errors.append(f"[{tag}] source 非法: {task.get('source')!r}")
        if task.get("clarify_mode") not in CLARIFY_MODES:
            errors.append(f"[{tag}] clarify_mode 非法: {task.get('clarify_mode')!r}")
        if not str(task.get("question") or "").strip():
            errors.append(f"[{tag}] question 为空")
        if not task.get("forbidden"):
            errors.append(f"[{tag}] forbidden 为空：禁止内容不写，判分器就没有红线")
        if not isinstance(task.get("max_steps"), int) or task["max_steps"] < 1:
            errors.append(f"[{tag}] max_steps 必须是正整数")

    # ---- 1. chunk 存在性 ----
    for task in tasks:
        tag = task.get("id") or f"line{task['_line']}"
        for chunk_id in task.get("relevant_chunk_ids") or []:
            if chunk_id not in chunks:
                errors.append(f"[{tag}] relevant_chunk_ids 里的 chunk 不存在: {chunk_id}")

    # ---- 2. 正向锚定 ----
    for task in tasks:
        tag = task.get("id") or f"line{task['_line']}"
        for index, point in enumerate(task.get("answer_points") or []):
            if not isinstance(point, dict):
                errors.append(f"[{tag}] answer_points[{index}] 不是对象")
                continue
            if not str(point.get("text") or "").strip():
                errors.append(f"[{tag}] answer_points[{index}] 缺少 text")
            chunk_ids = point.get("chunk_ids") or []
            if not chunk_ids:
                errors.append(f"[{tag}] answer_points[{index}] 没有 chunk_ids：judge 只能凭感觉判")
            for chunk_id in chunk_ids:
                if chunk_id not in chunks:
                    errors.append(f"[{tag}] answer_points[{index}] 的 chunk 不存在: {chunk_id}")

    # ---- 3. 反向覆盖 + 4. 锚定一致 ----
    for task in tasks:
        tag = task.get("id") or f"line{task['_line']}"
        points = [point for point in task.get("answer_points") or [] if isinstance(point, dict)]
        anchored = {
            chunk_id
            for point in points
            for chunk_id in (point.get("chunk_ids") or [])
        }
        # 金融任务卡还会把坐标写进 evidence.table_id（2026-11-03）：只写 evidence
        # 不写 chunk_ids 时，它也必须是 relevant_chunk_ids 的一部分——否则检索层
        # 标错位置而判分器照旧判对，两份读数互相打架。
        anchored |= {
            str(point["evidence"]["table_id"])
            for point in points
            if isinstance(point.get("evidence"), dict) and point["evidence"].get("table_id")
        }
        relevant = set(task.get("relevant_chunk_ids") or [])
        for chunk_id in sorted(relevant - anchored):
            errors.append(f"[{tag}] 反向覆盖失败：{chunk_id} 没有被任何 answer_point 引用（摆设 chunk）")
        if relevant != anchored:
            errors.append(
                f"[{tag}] 锚定不一致：relevant={sorted(relevant)} 与 "
                f"answer_points 并集={sorted(anchored)} 不等"
            )

    # ---- 5. 引用集中度 ----
    citation_counter: Counter = Counter()
    for task in tasks:
        if task.get("should_abstain"):
            continue
        for chunk_id in task.get("relevant_chunk_ids") or []:
            citation_counter[chunk_id] += 1
    for chunk_id, count in citation_counter.most_common():
        if count > CITATION_CONCENTRATION_LIMIT:
            warnings.append(
                f"引用集中：{chunk_id} 被 {count} 条任务引用（上限 {CITATION_CONCENTRATION_LIMIT}）"
            )

    # ---- 6. 负样本洁净 ----
    for task in tasks:
        if not task.get("should_abstain"):
            continue
        tag = task.get("id") or f"line{task['_line']}"
        if task.get("answer_points"):
            errors.append(f"[{tag}] 负样本不应有 answer_points：有支撑 chunk 就测不出编造")
        if task.get("relevant_chunk_ids"):
            errors.append(f"[{tag}] 负样本不应有 relevant_chunk_ids")
        if task.get("citation_required"):
            errors.append(f"[{tag}] 负样本不应要求引用")

    # ---- 7. 澄清对照配比 ----
    clarify_counter = Counter(task.get("clarify_mode") for task in tasks)
    if profile:
        if clarify_counter.get("ask", 0) < 2:
            errors.append(f"澄清对照不足：clarify_mode=ask 只有 {clarify_counter.get('ask', 0)} 条（需 ≥2）")
        if clarify_counter.get("none", 0) < 2:
            errors.append(f"澄清反向对照不足：clarify_mode=none 只有 {clarify_counter.get('none', 0)} 条（需 ≥2）")
        if clarify_counter.get("assume", 0) < 1:
            errors.append("缺少 clarify_mode=assume 的样本（需 ≥1）")

    # ---- 8. mock 隔离 ----
    for task in tasks:
        tag = task.get("id") or f"line{task['_line']}"
        if not task.get("mock"):
            if task.get("inject"):
                errors.append(f"[{tag}] 有注入计划但 mock 不为 true：注入不会生效")
            continue
        injections = task.get("inject") or []
        if not injections:
            errors.append(f"[{tag}] mock=true 但没有 inject 计划")
            continue
        for index, plan in enumerate(injections):
            if not isinstance(plan, dict):
                errors.append(f"[{tag}] inject[{index}] 不是对象")
                continue
            if plan.get("mode") not in INJECTION_MODES:
                errors.append(f"[{tag}] inject[{index}] mode 非法: {plan.get('mode')!r}")
            try:
                if int(plan.get("call_index")) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"[{tag}] inject[{index}] call_index 必须是 ≥1 的整数")
        if task.get("dimension") != "恢复与纠错":
            errors.append(f"[{tag}] mock=true 只用于恢复与纠错维度，当前是 {task.get('dimension')!r}")

    # ---- 9. 元数据完整 + 维度均衡 ----
    dimension_counter = Counter(task.get("dimension") for task in tasks)
    if profile:
        for dimension in DIMENSIONS:
            count = dimension_counter.get(dimension, 0)
            if count != 6:
                warnings.append(f"维度条数不是 6：{dimension} = {count}")
        if len(tasks) != 30:
            warnings.append(f"任务总数 {len(tasks)}，设计目标是 30")

    # ---- 附加：注入清单与语料对得上 ----
    injected_doc_ids = {item["doc_id"] for item in INJECTIONS}
    corpus_doc_ids = {chunk["doc_id"] for chunk in AGENT_CORPUS}
    missing_docs = injected_doc_ids - corpus_doc_ids
    if missing_docs:
        errors.append(f"注入清单里的文档不在语料中: {sorted(missing_docs)}")

    # ---- 10. 要点原子性（2026-10-27 加）----
    #
    # 实测教训：一条要点里捆两个事实时，judge 会在两批判分之间翻脸。
    #   V3 要点原文「离开工位超过十分钟须锁屏，办公电脑需设置自动锁屏」，
    #   答案只答了前半句：批 1 判 pass，批 2 判 fail，同一个答案两次结论相反。
    #   M2 要点「调整仅适用于 2026 年度及以后，历史年度按原制度执行」同样翻过。
    # 规则：一个 answer_point 只放一个可判定的事实，不出现句读分隔的第二个断言。
    # 2026-11-01 金融集审计：CX-07/08/09 有 4 个要点用「、」捆绑两个数字事实
    # （如「2025 年归母净利润 …元、营业收入 …元」），同样会让 judge 翻脸 ⇒ 纳入拦截。
    multi_fact_markers = ("，", "；", "。", "、")
    for task in tasks:
        tag = task.get("id") or f"line{task['_line']}"
        for index, point in enumerate(task.get("answer_points") or []):
            if not isinstance(point, dict):
                continue
            text = str(point.get("text") or "")
            hits = [marker for marker in multi_fact_markers if marker in text]
            if hits:
                errors.append(
                    f"[{tag}] answer_points[{index}] 含句读分隔（{'/'.join(hits)}）："
                    f"一个要点只能是一个事实，否则 judge 会漏判或翻脸 → {text}"
                )

    return errors, warnings


def print_report(tasks: list[dict], errors: list[str], warnings: list[str]) -> None:
    dimension_counter = Counter(task.get("dimension") for task in tasks)
    print(f"任务数: {len(tasks)}    语料 chunk: {len(AGENT_CORPUS)}    注入文档: {len(INJECTIONS)}")
    print()
    print("维度分布：")
    for dimension in DIMENSIONS:
        print(f"  {dimension:<8} {dimension_counter.get(dimension, 0)}")
    print()
    if warnings:
        print(f"警告 {len(warnings)} 条：")
        for warning in warnings:
            print(f"  ! {warning}")
        print()
    if errors:
        print(f"错误 {len(errors)} 条：")
        for error in errors:
            print(f"  x {error}")
        return
    print("lint 通过：0 error")


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 任务集 lint")
    parser.add_argument("--tasks", default=DEFAULT_TASKS)
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    if not tasks_path.exists():
        raise SystemExit(f"任务文件不存在: {tasks_path}")

    tasks = load_tasks(tasks_path)
    errors, warnings = lint(tasks, chunk_map())
    print_report(tasks, errors, warnings)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
