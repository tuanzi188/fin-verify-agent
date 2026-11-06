"""金融文档工具集：把 `FinanceToolbox` 接到 Agent loop 上。

三件事决定了它和制度库工具集不一样：

1. **`search_sections` 只定位，不算证据**。它返回的是章节摘要，不是原文；
   如果把它当证据编号喂给模型，模型会引用一个"看起来像出处"的章节摘要，
   而真正被引用的数值根本没被读过。所以它的证据项标 `citable=False`：
   参与去重与空结果判断，但不进编号列表。
2. **`calculate` 不是证据，是审计记录**。每次调用都进 `calcs`，
   评测侧据 "每个 calc 有 formula/inputs/result" 判硬门禁。
3. **截断要显式标注**。一个 26k 字的章节整段塞进上下文既贵又没用；
   截断时标 `truncated=True`，报告里能看出"这条结论是在被截断的资料上得出的"。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.prompts import (
    FINANCE_BASELINE_SYSTEM,
    FINANCE_FINAL_TEMPLATE,
    FINANCE_IMPROVED_SYSTEM,
)
from app.finance.block_index import BlockIndex
from app.finance.query_scope import scope_metrics
from app.finance.store import FinanceStore, _normalize_label
from app.tools.finance import FINANCE_TOOL_SPECS, FinanceToolbox
from app.tools.toolset import Injector, ToolOutcome, evidence_item

MAX_SECTION_CHARS = 4000
MAX_TABLE_CHARS = 1500
MAX_CELL_CHARS = 400
# 检索预算：超过这个次数后不再执行 search_*，逼模型去读已定位的内容。
# 2026-10-28 实测 CX-11 搜了 9 次、CX-12 搜了 7 次，token 全花在换关键词上。
# 2026-11-04 复测：跨 3 家公司的题（FIN-11）每家都要先定位表，5 次不够，
# 预算耗尽后模型仍想搜、又打满 20 步。提到 8 给多文档题余量；同时预算耗尽时
# 回显「已定位的 chunk 清单」（见 call()），让模型直接读已定位内容而不是空转。
MAX_SEARCH_CALLS = 8
#: 读取类工具的重复调用缓存；同一参数不重复回填正文。
READ_CACHE_TOOLS = ("get_table", "get_rows", "get_cell", "read_section", "read_block")

# 覆盖检查用的词表。只做"问题点名了哪些公司/年度/文档"的粗判，
# 不做语义理解；判断结果只用于"要不要补读"，不用于判分。
_COMPANY_ALIASES = (
    ("美的", "美的集团"),
    ("比亚迪", "比亚迪"),
    ("茅台", "贵州茅台"),
    ("天顺", "天顺风能"),
)
_YEAR_PATTERN = re.compile(r"(20\d{2})\s*年")
_DOC_WORD_PATTERN = re.compile(r"年报|年度报告|募集说明书|募集书|招股说明书")
_DOC_TYPE_LABELS = {"annual_report": "年报", "prospectus": "募集说明书"}

# 槽位覆盖用的指标→可接受行名。只收「同一科目的常见写法」，
# 判定规则是「行名包含别名」，不做反向包含，避免「研发人员」被当成「研发投入」。
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "研发投入": ("研发投入", "研发费用", "研发支出"),
    "营业收入": ("营业收入", "营业总收入"),
    "营业成本": ("营业成本", "营业总成本"),
    "归母净利润": (
        "归属于上市公司股东的净利润",
        "归属于母公司所有者的净利润",
        "归母净利润",
    ),
    "净利润": ("净利润",),
    "经营活动产生的现金流量净额": (
        "经营活动产生的现金流量净额",
        "经营活动现金流量净额",
        "经营现金流",
    ),
    "总资产": ("总资产", "资产总计"),
    "净资产": ("净资产",),
    "每股收益": ("每股收益",),
}


_RATIO_LABEL_WORDS = (
    "比例", "占比", "比重", "率", "增减", "增长", "下降", "变动", "同比", "环比",
)


def _metric_matches(label: object, metric: str) -> bool:
    text = _normalize_label(label)
    if not text:
        return False
    # 「研发投入占营业收入比例」不是营业收入本身；派生/比率行一律不当作基础科目。
    if any(word in text for word in _RATIO_LABEL_WORDS):
        return False
    aliases = _METRIC_ALIASES.get(metric) or (metric,)
    for alias in aliases:
        target = _normalize_label(alias)
        if not target:
            continue
        # 只接受「等于 / 以科目开头 / 以科目结尾」：
        # 营业收入（元）→ 开头匹配；归属于上市公司股东的净利润 → 结尾匹配；
        # 「研发投入占营业收入比例」→ 已在上面被比率词拦掉。
        if text == target or text.startswith(target) or text.endswith(target):
            return True
    return False


def _filled_ratio(table: dict) -> float:
    rows = table.get("rows") or []
    total = sum(len(row) for row in rows)
    if not total:
        return 0.0
    filled = sum(
        1
        for row in rows
        for cell in row
        if str(cell or "").strip()
    )
    return filled / total


def _matching_rows(table: dict, metric: str) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    for index, row in enumerate(table.get("rows") or []):
        label = FinanceStore._row_label(row)
        if label and _metric_matches(label, metric):
            matches.append((index, label))
    return matches

# 截断注入的长度。制度库用 12 字，因为那里一个 chunk 25–70 字；
# 金融这里要短到"数字必坏"：`456,451,731` 是 11 个字符，12 字截断等于不截断。
_TRUNCATE_CHARS = 8


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    text = str(text or "")
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…[已截断]", True


def _cut(text: Any) -> str:
    value = str(text or "")
    if len(value) <= _TRUNCATE_CHARS:
        return value
    return value[:_TRUNCATE_CHARS] + "…[截断]"


def _cut_payload(name: str, data: Any) -> Any:
    """截断注入：只切"模型看得到的那部分正文"，结构字段一律保留。

    保留 `table_ids` / `table_id` / `row` / `column` 是故意的：注入要逼出"重取"，
    而不是让模型连重取什么都找不到——找不到就成了考运气，不是考纠错。
    """
    if name in ("search_sections", "search_blocks") and isinstance(data, list):
        return [{**item, "snippet": _cut(item.get("snippet") or item.get("content"))} for item in data]
    if name == "read_section" and isinstance(data, dict):
        return {**data, "text": _cut(data.get("text"))}
    if name == "read_block" and isinstance(data, dict):
        return {**data, "content": _cut(data.get("content"))}
    if name in ("get_table", "get_rows") and isinstance(data, dict):
        rows = data.get("rows") or []
        return {**data, "rows": rows[:1], "truncated": True}
    if name == "get_cell" and isinstance(data, dict):
        return {**data, "cell_text": _cut(data.get("cell_text"))}
    return data


_STATEMENT_SCOPE_LABELS = {"consolidated": "合并", "parent": "母公司"}
_STATEMENT_TYPE_LABELS = {
    "balance_sheet": "资产负债表",
    "income_statement": "利润表",
    "cash_flow": "现金流量表",
    "equity_change": "所有者权益变动表",
    "main_indicators": "主要会计数据",
}


def _statement_tags(payload: dict) -> str:
    """表头里的口径/报表标签，让模型在 read 阶段就能看到合并 vs 母公司。"""
    scope = _STATEMENT_SCOPE_LABELS.get(str(payload.get("statement_scope") or ""), "")
    statement = _STATEMENT_TYPE_LABELS.get(str(payload.get("statement_type") or ""), "")
    parts = []
    if scope:
        parts.append(f"口径：{scope}")
    if statement:
        parts.append(f"报表：{statement}")
    return ("｜" + "｜".join(parts)) if parts else ""


def _render_table(table: dict) -> str:
    head = (
        f"表 {table.get('table_id')}｜{table.get('caption') or '无标题'}"
        f"｜PDF 第 {table.get('page')} 页"
    )
    if table.get("page_end") and table.get("page_end") != table.get("page"):
        head += f"–{table.get('page_end')}"
    head += f"｜单位：{table.get('unit') or '未标注'}"
    head += _statement_tags(table)
    lines = [head, " | ".join(str(name) for name in table.get("columns") or [])]
    for row in table.get("rows") or []:
        lines.append(" | ".join(str(cell) for cell in row))
        if sum(len(line) for line in lines) > MAX_TABLE_CHARS:
            lines.append("…[表格过长，已截断，按需用 get_cell 取具体单元格]")
            break
    return "\n".join(lines)


def _render_rows(rows: dict) -> str:
    head = (
        f"表 {rows.get('table_id')}｜{rows.get('caption') or '无标题'}"
        f"｜PDF 第 {rows.get('page')} 页"
    )
    if rows.get("page_end") and rows.get("page_end") != rows.get("page"):
        head += f"–{rows.get('page_end')}"
    head += f"｜单位：{rows.get('unit') or '未标注'}（来源：{rows.get('unit_source') or '未标注'}）"
    head += _statement_tags(rows)
    columns = [str(name) for name in rows.get("columns") or []]
    lines = [head, "行号 | " + " | ".join(columns)]
    for row in rows.get("rows") or []:
        values = " | ".join(
            "None" if value is None else str(value) for value in row.get("values") or []
        )
        lines.append(f"{row.get('row')} | {values}")
    if rows.get("skipped_rows"):
        notes = "；".join(
            f"{item.get('input')}（{item.get('reason')}）"
            for item in rows.get("skipped_rows")[:5]
        )
        lines.append(f"未读取行：{notes}")
    if rows.get("suspected_misalignment"):
        notes = "；".join(
            f"{item.get('row_name')} 的「{item.get('column')}」：{item.get('reason')}"
            for item in rows.get("suspected_misalignment")[:5]
        )
        lines.append(f"警告：以下单元格疑似表头错位，勿直接引用：{notes}")
    if rows.get("footnote"):
        lines.append(f"脚注：{rows.get('footnote')}")
    return "\n".join(lines)


class FinanceToolset:
    """金融文档工具集。接口与 `app.tools.mock.Toolbox` 一致。"""

    name = "finance"
    final_template = FINANCE_FINAL_TEMPLATE
    # 金融任务的动作链更长：定位 → 读章节 → 读表 → 取单元格 → 计算，
    # 3 轮装不下（实测 3 轮时模型只来得及做 4 次 search_sections 就被收口）。
    max_tool_rounds = 5

    def __init__(
        self,
        store: FinanceStore,
        injections: Any = None,
        search_mode: str = "sections",
        block_index: BlockIndex | None = None,
    ):
        if search_mode not in ("sections", "blocks"):
            raise ValueError(f"未知 search_mode: {search_mode}")
        self.search_mode = search_mode
        self.search_tool = "search_blocks" if search_mode == "blocks" else "search_sections"
        self.toolbox = FinanceToolbox(store, block_index=block_index)
        #: 每次 calculate 的 {formula, inputs, result}，供硬门禁与报告使用
        self.calcs: list[dict] = []
        self._injector = Injector(injections)
        #: 每个工具本条的调用次数，用于检索预算
        self._tool_calls: dict[str, int] = {}
        #: 已读取内容的缓存：同一表/单元格/块不重复回填正文
        self._read_cache: dict[tuple[str, str], ToolOutcome] = {}
        #: 检索工具剩余预算；覆盖修复时由 runner 临时加
        self._search_budget = MAX_SEARCH_CALLS
        #: 读失败换表的额外检索额度（每条任务最多 2 次）
        self._budget_grants = 0
        #: 本次任务已定位到的 chunk（检索结果），预算耗尽时回显给模型
        self._located_ids: list[str] = []

    # ------------------------------------------------------------------
    # 注入与复用
    # ------------------------------------------------------------------

    def prepare(self, injections: Any = None) -> None:
        """一条任务开跑前调用：清掉上一条的 calcs / 调用计数，换成本条的注入计划。

        评测脚本会复用同一个实例连跑几十条；不清就会把上一条的计算记到下一条头上，
        硬门禁"每个数字都有公式"直接失效。
        """
        self.calcs = []
        self._injector = Injector(injections)
        self._tool_calls = {}
        self._read_cache = {}
        self._search_budget = MAX_SEARCH_CALLS
        self._budget_grants = 0
        self._located_ids = []

    @property
    def planned_injections(self) -> int:
        return self._injector.planned

    @property
    def fired_injections(self) -> int:
        return self._injector.fired

    @property
    def calls(self) -> int:
        return self._injector.calls

    def grant_search_budget(self, extra: int = 2) -> None:
        """覆盖修复时临时加检索预算：缺文档必须能搜到，不能被总预算卡死。"""
        try:
            self._search_budget += max(0, int(extra))
        except (TypeError, ValueError):
            self._search_budget += 2

    # ------------------------------------------------------------------
    # 覆盖检查（跨文档）
    # ------------------------------------------------------------------

    def coverage_gaps(self, question: str, evidence: list[dict]) -> list[str]:
        """问题点名的文档里，证据中一份都没读到的，返回缺失清单。

        只在两种情况下要求“多份文档”：
        - 问题点名 ≥2 家公司（每家至少一份对应年度的文档）；
        - 问题明确写到两份文档/两个年度（“2024 年年报和 2025 年年报”）。

        单公司单年度的题不要求跨文档：否则“2025 年营收同比”会被误判成
        “必须再读 2024 年报”，而同比数据本来就在 2025 年报的上期列里。
        """
        docs = self.toolbox.store.list_documents()
        text = str(question or "")
        if not text or not docs:
            return []

        companies: list[str] = []
        for alias, full in _COMPANY_ALIASES:
            if alias in text and full not in companies:
                companies.append(full)
        years = sorted(set(_YEAR_PATTERN.findall(text)))
        doc_words = len(_DOC_WORD_PATTERN.findall(text))
        explicit_multi_doc = doc_words >= 2 or ("两份" in text and bool(years))
        multi_company = len(companies) >= 2

        required: list[dict] = []
        if multi_company:
            for company in companies:
                company_docs = [d for d in docs if d.get("company") == company]
                target_years = years or sorted(
                    {str(d.get("period")) for d in company_docs if d.get("period")},
                    reverse=True,
                )[:1]
                for year in target_years:
                    required.extend(
                        d for d in company_docs if str(d.get("period")) == year
                    )
        elif explicit_multi_doc and years:
            for year in years:
                candidates = [
                    d for d in docs
                    if str(d.get("period")) == year
                    and (not companies or d.get("company") in companies)
                ]
                if candidates:
                    required.append(candidates[0])
        elif explicit_multi_doc:
            pool = [d for d in docs if not companies or d.get("company") in companies]
            required = pool[:2]

        evidence_doc_ids = {
            str(item.get("doc_id"))
            for item in evidence or []
            if item.get("doc_id")
        }
        gaps: list[str] = []
        missing_doc_ids: set[str] = set()
        seen: set[str] = set()
        for doc in required:
            doc_id = str(doc.get("doc_id") or "")
            if not doc_id or doc_id in seen or doc_id in evidence_doc_ids:
                continue
            seen.add(doc_id)
            missing_doc_ids.add(doc_id)
            label = _DOC_TYPE_LABELS.get(
                str(doc.get("doc_type")), str(doc.get("doc_type") or "文档")
            )
            gaps.append(
                f"{doc.get('company')} {doc.get('period')} 年{label}（{doc_id}）"
            )

        # 指标/表级覆盖：文档读到了，不等于问题要的那张表读到了。
        # FIN-12 实测：2024 年报读了 t0023（研发人员），但没读 t0024（研发投入金额），
        # 文档级检查因此放行，模型最后答「无法判定」。
        metrics = scope_metrics(text)
        if not metrics or not companies:
            return gaps

        if years and explicit_multi_doc:
            target_years = years
        elif years:
            # 同比/上期数在同一张表的对比列里，不强制再读上一年年报。
            target_years = [max(years)]
        else:
            target_years = []
        quarter_hint = bool(re.search(r"季度|Q[1-4]", text, re.IGNORECASE))

        for company in companies:
            company_docs = [d for d in docs if d.get("company") == company]
            if not company_docs:
                continue
            if target_years:
                year_docs = [
                    d for d in company_docs
                    if str(d.get("period")) in target_years
                ]
            else:
                latest = max(
                    {str(d.get("period")) for d in company_docs if d.get("period")},
                    default="",
                )
                year_docs = [
                    d for d in company_docs if str(d.get("period")) == latest
                ]
            for doc in year_docs:
                doc_id = str(doc.get("doc_id") or "")
                # 整份文档没读：文档级 gap 已经说过，不重复。
                if not doc_id or doc_id in missing_doc_ids:
                    continue
                if doc_id not in evidence_doc_ids:
                    continue
                for metric in metrics:
                    if self._doc_covers_metric(doc_id, metric, evidence):
                        continue
                    candidate = self._metric_candidate(
                        doc_id,
                        metric,
                        years=target_years,
                        quarter_hint=quarter_hint,
                        evidence=evidence,
                    )
                    if not candidate:
                        continue
                    suffix = (
                        "已在章节中定位但未读取"
                        if candidate["located"]
                        else "尚未读取"
                    )
                    gaps.append(
                        f"{company} {doc.get('period')} 年{metric}"
                        f"（候选表 {candidate['table_id']} 第 {candidate['page']} 页，"
                        f"行名：{candidate['row']}；{suffix}）"
                    )

        return list(dict.fromkeys(gaps))

    def _doc_covers_metric(
        self, doc_id: str, metric: str, evidence: list[dict]
    ) -> bool:
        """该文档里是否已经用「行/单元格」读过这个指标。"""
        for item in evidence or []:
            if str(item.get("doc_id") or "") != doc_id:
                continue
            kind = str(item.get("kind") or "")
            row_name = str(item.get("row_name") or "")
            if kind == "cell":
                if row_name and _metric_matches(row_name, metric):
                    return True
                continue
            table_id = str(item.get("table_id") or "")
            if not table_id:
                continue
            try:
                table = self.toolbox.store.get_table(table_id)
            except Exception:  # noqa: BLE001 - 覆盖检查不能阻断主流程
                table = None
            if table is None:
                continue
            rows = table.get("rows") or []
            indices = item.get("row_indices")
            if isinstance(indices, list) and indices:
                subset = [
                    rows[index]
                    for index in indices
                    if isinstance(index, int) and 0 <= index < len(rows)
                ]
            else:
                subset = rows
            if any(
                _metric_matches(FinanceStore._row_label(row), metric)
                for row in subset
            ):
                return True
        return False

    def _metric_candidate(
        self,
        doc_id: str,
        metric: str,
        *,
        years: list[str],
        quarter_hint: bool,
        evidence: list[dict],
    ) -> dict | None:
        """在文档的表里找最可能承载该指标的一张，并说明是否已定位未读取。"""
        try:
            document = self.toolbox.store.get_document(doc_id)
        except Exception:  # noqa: BLE001
            return None
        located_tables: set[str] = set()
        for item in evidence or []:
            if str(item.get("doc_id") or "") == doc_id:
                located_tables.update(str(value) for value in item.get("table_ids") or [])

        section_paths = {
            str(section.get("section_id") or ""): " > ".join(
                str(part) for part in (section.get("path") or [])
            )
            for section in document.get("sections") or []
        }
        best: tuple[float, dict, str] | None = None
        for table in document.get("tables") or []:
            matches = _matching_rows(table, metric)
            if not matches:
                continue
            columns = [str(name) for name in table.get("columns") or []]
            section_path = section_paths.get(str(table.get("section_id") or ""), "")
            heading = f"{table.get('caption') or ''} {section_path}"
            score = _filled_ratio(table) + 0.2 * len(matches)
            # 优先「主要会计数据和财务指标」表：它是最标准的读数出处，
            # 也是同比/上期列的集中地。合并利润表也能查到，但不是首选。
            if "主要会计" in heading or "主要财务指标" in heading:
                score += 0.5
            if metric == "研发投入" and "研发投入" in heading:
                # 优先「公司研发投入情况」专表，而不是管理层讨论里的「费用」表：
                # 后者可能把同一笔钱写成「研发费用」，答错科目名（FIN-12 实测）。
                score += 0.8
            if any("增减" in name for name in columns):
                score += 0.2
            if quarter_hint and any("季度" in name for name in columns):
                score += 2.0
            score += 0.3 * sum(
                1 for year in years if any(str(year) in name for name in columns)
            )
            if best is None or score > best[0]:
                best = (score, table, matches[0][1])
        if best is None:
            return None
        _, table, row_label = best
        return {
            "table_id": str(table.get("table_id") or ""),
            "page": table.get("page"),
            "row": row_label,
            "located": str(table.get("table_id") or "") in located_tables,
        }

    def unsupported_periods(self, question: str) -> list[str]:
        """问题要求晚于本地语料最新报告期的年份（如 2026 Q1）。

        这类题的正确行为是拒答，不是拿 2025 年的数字顶上。
        只判“未来年份”；更早的年份不判——2024 年报里可能带 2023 比较期，
        拿年份差去判会把“比 2023 年增加多少”误伤成不可回答。
        """
        docs = self.toolbox.store.list_documents()
        years = sorted({int(y) for y in _YEAR_PATTERN.findall(str(question or ""))})
        available = [
            int(str(d.get("period")))
            for d in docs
            if str(d.get("period") or "").isdigit()
        ]
        if not years or not available:
            return []
        latest = max(available)
        return [
            f"{year} 年（本地语料最新到 {latest} 年）"
            for year in years
            if year > latest
        ]

    # ------------------------------------------------------------------
    # Toolset 契约
    # ------------------------------------------------------------------

    def specs(self) -> list[dict]:
        if self.search_mode == "blocks":
            # 块模式只走结构索引；旧章节检索留着会出现两套定位结果互相干扰。
            return [
                spec
                for spec in FINANCE_TOOL_SPECS
                if spec["function"]["name"] != "search_sections"
            ]
        return FINANCE_TOOL_SPECS

    def system_prompt(self, variant: str) -> str:
        prompt = FINANCE_IMPROVED_SYSTEM if variant == "improved" else FINANCE_BASELINE_SYSTEM
        prompt += (
            "\n每个计算的输入必须先读取对应单元格。"
            "百分数相减后用百分点表示时，原始小数差必须乘以 100，例如 30%-20%=10 个百分点。"
        )
        if self.search_mode == "blocks":
            # 规则正文里提到了 search_sections；不删掉，模型会去调一个不存在的工具
            # （2026-11-03 实测：blocks 模式下仍在条数预算内反复尝试）。
            prompt = prompt.replace(
                "search_sections / search_blocks / search_tables 只给章节/块/表 id 和摘要",
                "search_blocks / search_tables 只给块/表 id 和摘要",
            ).replace(
                "；结构块检索不到再退回 search_sections（本模式提供时才用）；\n    仍然",
                "；仍然",
            )
            prompt += (
                "\n\n本模式只提供 search_blocks，不提供 search_sections；"
                "检索必须从 search_blocks 开始，拿到 block_id 后 read_block。"
                "普通查数优先 get_rows，不要用 get_table 浏览整张表。"
            )
        return prompt

    async def call(self, name: str, arguments: dict) -> ToolOutcome:
        arguments = arguments or {}
        mode = self._injector.take()
        self._tool_calls[name] = self._tool_calls.get(name, 0) + 1
        # 检索预算：只拦检索工具，读原文/取单元格/计算都不限次。
        # 返回 ok=False 而不是空结果，是为了让 trace 能区分"预算用完"和"没搜到"。
        if name in ("search_sections", "search_blocks", "search_tables"):
            if self._search_budget <= 0:
                # 回显已定位清单：模型此时往往已经搜到目标表，只是想再换关键词确认。
                # 给出具体 id 让它直接 read_*，比干巴巴一句「预算用完」更能止住空转
                # （FIN-11 实测：6 次 search_tables 后被打回，随后 20 步打满）。
                located = "、".join(self._located_ids[-12:]) or "（本条任务尚无成功检索结果）"
                return ToolOutcome(
                    tool=name,
                    ok=False,
                    error_kind="unavailable",
                    error_detail=(
                        "检索预算已用完；请改用 read_block / read_section / get_rows / "
                        "get_table / get_cell 读取下面已定位的内容，不要再用同义关键词检索。\n"
                        f"已定位：{located}"
                    ),
                )
            self._search_budget -= 1
        if mode is not None:
            return await self._injected(name, arguments, mode)

        cache_key = None
        if name in READ_CACHE_TOOLS:
            cache_key = (
                name,
                json.dumps(arguments, sort_keys=True, ensure_ascii=False),
            )
            cached = self._read_cache.get(cache_key)
            if cached is not None:
                # 回填的是同一份证据；runner 的 seen 集合会阻止重复编号。
                return cached

        outcome = await self.toolbox.call(name, arguments)
        if not outcome.ok:
            # 读失败通常意味着选错了表/行名。给一次额外检索额度换表，
            # 否则模型只能在错误表上反复猜，最后预算耗尽拒答（FIN-S01 实测）。
            if (
                name in ("get_rows", "get_cell")
                and outcome.error_kind == "not_found"
                and self._budget_grants < 2
            ):
                self._search_budget += 1
                self._budget_grants += 1
            return ToolOutcome(
                tool=name,
                ok=False,
                error_kind=outcome.error_kind,
                error_detail=outcome.error_detail,
            )
        data = outcome.data
        if name == "calculate":
            data = dict(data, calculation_id=f"calc-{len(self.calcs) + 1}")
            self.calcs.append(
                {
                    "formula": data.get("formula"),
                    "inputs": data.get("inputs"),
                    "result": data.get("result"),
                    "calculation_id": data["calculation_id"],
                }
            )
        ids, count = self._summarize(name, data)
        if name in ("search_sections", "search_blocks", "search_tables"):
            for chunk_id in ids or []:
                text = str(chunk_id)
                if text and text not in self._located_ids:
                    self._located_ids.append(text)
        result = ToolOutcome(tool=name, ok=True, data=data, result_ids=ids, result_count=count)
        if cache_key is not None:
            self._read_cache[cache_key] = result
        return result

    # ------------------------------------------------------------------
    # 故障注入
    # ------------------------------------------------------------------

    async def _injected(self, name: str, arguments: dict, mode: str) -> ToolOutcome:
        """注入故障。Agent 侧看到的形状与真实失败一致，注入标记只在 trace 里。"""
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
            data: Any = [] if name in ("search_sections", "search_blocks", "list_documents") else {}
            return ToolOutcome(tool=name, ok=True, data=data, injected="empty")

        # truncated：先真实执行，再把正文/单元格切短
        outcome = await self.toolbox.call(name, arguments)
        if not outcome.ok:
            return ToolOutcome(
                tool=name, ok=False, injected="truncated",
                error_kind=outcome.error_kind, error_detail=outcome.error_detail,
            )
        data = _cut_payload(name, outcome.data)
        ids, count = self._summarize(name, data)
        return ToolOutcome(
            tool=name, ok=True, data=data, result_ids=ids,
            result_count=count, injected="truncated",
        )

    # ------------------------------------------------------------------
    # 结果 → 证据
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize(name: str, data: Any) -> tuple[list[str], int]:
        if name == "list_documents" and isinstance(data, list):
            return [str(item.get("doc_id") or "") for item in data], len(data)
        if name == "search_sections" and isinstance(data, list):
            return [str(item.get("section_id") or "") for item in data], len(data)
        if name == "search_blocks" and isinstance(data, list):
            return [str(item.get("block_id") or "") for item in data], len(data)
        if name == "search_tables" and isinstance(data, list):
            return [str(item.get("table_id") or "") for item in data], len(data)
        if name == "read_block" and isinstance(data, dict):
            return [str(data.get("block_id") or "")], 1
        if name == "read_section" and isinstance(data, dict):
            return [str(data.get("section_id") or "")], 1
        if name == "get_table" and isinstance(data, dict):
            return [str(data.get("table_id") or "")], 1
        if name == "get_rows" and isinstance(data, dict):
            anchor = (
                f"{data.get('table_id')}#rows#"
                + ",".join(str(index) for index in data.get("row_indices") or [])
                + "#cols#"
                + ",".join(str(index) for index in data.get("column_indices") or [])
            )
            return [anchor], 1
        if name == "get_cell" and isinstance(data, dict):
            anchor = f"{data.get('table_id')}#{data.get('row')}#{data.get('column')}"
            return [anchor], 1
        # 计算不是证据，但也不能报 0 条：2026-11-03 探针里 FIN-22 两次 calculate
        # 都成功，模型却在答案里写"均返回 0 条、无法产出计算记录"——因为这里落到
        # 默认值，runner 的步骤摘要就成了"calculate 成功，返回 0 条"。计算编号
        # 只进 trace，不会混进证据池（证据只走 to_evidence）。
        if name == "calculate" and isinstance(data, dict):
            return [str(data.get("calculation_id") or "")], 1
        return [], 0

    def to_evidence(self, name: str, data: Any) -> list[dict]:
        if name == "search_blocks" and isinstance(data, list):
            rows = []
            for item in data:
                if not isinstance(item, dict) or not item.get("block_id"):
                    continue
                block_kind = str(item.get("kind") or "block")
                meta = (
                    f"{item.get('title') or ''}｜{block_kind}｜PDF 第 {item.get('page')} 页"
                )
                if item.get("table_id"):
                    meta += (
                        f"｜表 {item.get('table_id')}｜行 {item.get('row_indices')}"
                        f"｜列 {item.get('column_indices')}"
                    )
                rows.append(
                    evidence_item(
                        chunk_id=str(item.get("block_id")),
                        title=meta,
                        content=str(item.get("content") or "")[:400],
                        kind="block_locator",
                        page=item.get("page"),
                        citable=False,
                        block_kind=block_kind,
                        doc_id=item.get("doc_id"),
                        table_id=item.get("table_id"),
                        section_id=item.get("section_id"),
                        row_indices=item.get("row_indices"),
                        column_indices=item.get("column_indices"),
                    )
                )
            return rows

        if name == "search_tables" and isinstance(data, list):
            rows = []
            for item in data:
                if not isinstance(item, dict) or not item.get("table_id"):
                    continue
                matched = "、".join(
                    str(value) for value in item.get("matched_rows") or []
                )
                content = (
                    f"表 {item.get('table_id')}｜{item.get('caption') or '无标题'}"
                    f"｜PDF 第 {item.get('page')} 页"
                    f"｜列：{'、'.join(str(value) for value in item.get('columns') or [])}"
                    f"｜行：{'、'.join(str(value) for value in item.get('row_labels') or [])}"
                )
                if matched:
                    content += f"｜匹配行：{matched}"
                content = content[:600]
                caption = str(item.get("caption") or "")
                # 选表引导（2026-11-04，2026-11-06 改为结构化字段）：CX-08 实测模型
                # 放着金标「主要会计数据」汇总表不读，转头读「营业收入构成」叙述表，
                # 导致 4 个要点只有 1 个被引用。
                #
                # 原来把「｜【推荐】…」拼进 content，但定位项由 runner._format_locators
                # 渲染，那里只取 chunk_id/title/page，content 到不了模型——标记是死的。
                # 现在改成字段：标记由渲染层生成，提示词不再依赖工具的自由文本，
                # caption 特征改了也不用同步改提示词。
                summary_table = "主要会计" in caption or "主要财务指标" in caption
                rows.append(
                    evidence_item(
                        chunk_id=f"{item.get('table_id')}#table_locator",
                        title=(
                            f"{item.get('company') or ''} {item.get('period') or ''}"
                            f"｜表目录｜{item.get('caption') or item.get('table_id')}"
                        ),
                        content=content,
                        kind="table_locator",
                        page=item.get("page"),
                        citable=False,
                        table_id=item.get("table_id"),
                        table_caption=item.get("caption") or "",
                        doc_id=item.get("doc_id"),
                        row_labels=item.get("row_labels"),
                        matched_rows=item.get("matched_rows"),
                        summary_table=summary_table,
                    )
                )
            return rows

        if name == "read_block" and isinstance(data, dict):
            content = str(data.get("content") or "")
            return [
                evidence_item(
                    chunk_id=str(data.get("block_id") or ""),
                    title=f"{data.get('title') or ''}｜{data.get('kind') or 'block'}",
                    content=content,
                    kind=str(data.get("kind") or "block"),
                    page=data.get("page"),
                    table_id=data.get("table_id"),
                    table_caption=data.get("table_caption"),
                    unit=data.get("unit") or "",
                    row_indices=data.get("row_indices"),
                    column_indices=data.get("column_indices"),
                    row_name="、".join(
                        str(value) for value in data.get("row_indices") or []
                    ),
                    doc_id=data.get("doc_id"),
                )
            ]

        if name == "search_sections" and isinstance(data, list):
            return [
                evidence_item(
                    chunk_id=str(item.get("section_id") or ""),
                    title=f"{item.get('title') or ''}｜{' > '.join(item.get('path') or [])}",
                    content=f"PDF 第 {item.get('page_start')} 页\n{item.get('snippet') or ''}",
                    kind="section_locator",
                    page=item.get("page_start"),
                    path=item.get("path") or [],
                    # 只定位不算证据：不进编号列表，但参与去重与空结果判断
                    citable=False,
                    doc_id=item.get("doc_id"),
                )
                for item in data
                if isinstance(item, dict) and item.get("section_id")
            ]

        if name == "read_section" and isinstance(data, dict):
            body, truncated = _truncate(data.get("text") or "", MAX_SECTION_CHARS)
            path = data.get("path") or []
            table_ids = list(data.get("table_ids") or [])
            # 年报里大量小节只有一句标题 + 一张表，正文是空的。不把表 id 交出去，
            # 模型读完只会得到"本节没有内容"，然后判定未披露（实测踩中）。
            footer = ""
            if table_ids:
                footer = (
                    "本节包含表格：" + "、".join(table_ids)
                    + "（用 get_table 读整表，get_cell 取具体单元格）\n"
                )
            content, _ = _truncate(
                f"{' > '.join(path)}\n{footer}{body or '（本节正文为空，内容在表格里）'}",
                MAX_SECTION_CHARS,
            )
            return [
                evidence_item(
                    chunk_id=str(data.get("section_id") or ""),
                    title=f"{data.get('title') or ''}｜{' > '.join(path)}",
                    content=content,
                    kind="section",
                    page=data.get("page_start"),
                    path=path,
                    truncated=truncated,
                    table_ids=table_ids,
                    doc_id=data.get("doc_id"),
                )
            ]

        if name == "get_table" and isinstance(data, dict):
            rendered = _render_table(data)
            content, truncated = _truncate(rendered, MAX_TABLE_CHARS + 200)
            return [
                evidence_item(
                    chunk_id=str(data.get("table_id") or ""),
                    title=f"{data.get('title') or ''}｜表 {data.get('table_id')}",
                    content=content,
                    kind="table",
                    page=data.get("page"),
                    truncated=truncated,
                    table_caption=data.get("caption") or "",
                    unit=data.get("unit") or "",
                    statement_scope=data.get("statement_scope") or "",
                    statement_type=data.get("statement_type") or "",
                    doc_id=data.get("doc_id"),
                )
            ]

        if name == "get_rows" and isinstance(data, dict):
            rendered = _render_rows(data)
            content, truncated = _truncate(rendered, MAX_TABLE_CHARS + 200)
            anchor = (
                f"{data.get('table_id')}#rows#"
                + ",".join(str(index) for index in data.get("row_indices") or [])
                + "#cols#"
                + ",".join(str(index) for index in data.get("column_indices") or [])
            )
            return [
                evidence_item(
                    chunk_id=anchor,
                    title=f"{data.get('title') or ''}｜{data.get('table_caption') or '表格'}",
                    content=content,
                    kind="rows",
                    page=data.get("page"),
                    truncated=truncated,
                    table_id=data.get("table_id"),
                    table_caption=data.get("table_caption") or "",
                    unit=data.get("unit") or "",
                    statement_scope=data.get("statement_scope") or "",
                    statement_type=data.get("statement_type") or "",
                    row_indices=list(data.get("row_indices") or []),
                    column_indices=list(data.get("column_indices") or []),
                    row_name="、".join(
                        str(row.get("row_name") or "")
                        for row in data.get("rows") or []
                    ),
                    doc_id=data.get("doc_id"),
                )
            ]

        if name == "get_cell" and isinstance(data, dict):
            quote = (
                f"{data.get('table_caption') or '表格'}｜第 {data.get('page')} 页"
                f"｜{data.get('row_name')} 的「{data.get('column')}」= {data.get('cell_text')}"
                f"（单位：{data.get('unit') or '未标注'}，来源：{data.get('unit_source') or 'table'}）"
            )
            misalignment = data.get("misalignment") or {}
            if misalignment.get("suspected"):
                quote += (
                    f"\n警告：该单元格疑似表头错位（{misalignment.get('reason')}），"
                    "请改用 get_rows 或换行列名核对，勿直接引用。"
                )
            content, truncated = _truncate(quote, MAX_CELL_CHARS)
            anchor = f"{data.get('table_id')}#{data.get('row')}#{data.get('column')}"
            extra = {}
            if misalignment.get("suspected"):
                extra["misaligned"] = True
                extra["misalignment_reason"] = misalignment.get("reason")
            return [
                evidence_item(
                    chunk_id=anchor,
                    title=f"{data.get('title') or ''}｜{data.get('table_caption') or '表格'}",
                    content=content,
                    kind="cell",
                    page=data.get("page"),
                    truncated=truncated,
                    table_id=data.get("table_id"),
                    row_name=data.get("row_name"),
                    column=data.get("column"),
                    cell_text=data.get("cell_text"),
                    unit=data.get("unit") or "",
                    statement_scope=data.get("statement_scope") or "",
                    statement_type=data.get("statement_type") or "",
                    doc_id=data.get("doc_id"),
                    **extra,
                )
            ]

        if name == "list_documents" and isinstance(data, list):
            summary = "; ".join(
                f"{item.get('doc_id')}（{item.get('company')} {item.get('period')} {item.get('doc_type')}）"
                for item in data[:20]
            )
            return [
                evidence_item(
                    chunk_id="__documents__",
                    title="本地文档清单",
                    content=summary,
                    kind="catalog",
                    citable=False,
                )
            ]
        return []
