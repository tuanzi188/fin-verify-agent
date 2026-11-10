"""每次查证独立的任务清单：记录已读证据、期间、口径、冲突和执行进展。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import product

from app.finance.calculation_records import record_number_tokens
from app.finance.query_scope import scope_companies, scope_quarter, scope_years
from app.finance.routing_policy import requested_metrics
from app.finance.store import FinanceStore, FinanceStoreError, _normalize_label

TASK_PLAN_VERSION = "finance-task-plan-v1"
_PLAN_MONEY_SCALE = {
    "元": 1,
    "千元": 1000,
    "万元": 10000,
    "百万元": 1000000,
    "亿元": 100000000,
}
_PLAN_UNIT_PATTERN = re.compile(r"亿元|百万元|万元|千元|元/股|元|%|％|倍")


@dataclass
class EvidenceWorkItem:
    """待查项只描述证据覆盖，不宣称模型结论已经验证。"""

    item_id: str
    company: str
    period: str
    metric: str
    aliases: tuple[str, ...]
    statement_scope: str
    quarter: str = ""
    status: str = "pending"
    evidence: list[dict] = field(default_factory=list)
    reason: str = "尚未取得目标行列证据"


class FinanceTaskPlan:
    """状态只属于当前请求，所有更新都来自真实工具返回的证据。"""

    def __init__(self, question: str, documents: list[dict]):
        self.question = question
        self.items: list[EvidenceWorkItem] = []
        self.no_progress_steps = 0
        self.stop_reason = ""
        self.calculation_count = 0
        self._calculation_keys: set[str] = set()
        self._seen_evidence_keys: set[str] = set()
        self.requires_calculation = bool(
            re.search(
                r"自行|计算|验算|换算|折合|平均每年|合计是多少|增加多少|相差多少",
                question,
            )
        )
        companies = scope_companies(question, documents)
        periods = sorted(scope_years(question))
        metric_specs = requested_metrics(question)
        # 派生率不假设原文存在同名行；基础科目与计算记录分别跟踪。
        self.untracked_metrics = [name for name, _ in metric_specs if "率" in name]
        metric_specs = [
            (name, aliases) for name, aliases in metric_specs if "率" not in name
        ]
        if (
            self.requires_calculation
            and len(periods) == 1
            and re.search(r"上年|去年|同比", question)
        ):
            periods = sorted({periods[0], str(int(periods[0]) - 1)})
        scope = (
            "parent"
            if "母公司" in question
            else ("consolidated" if "合并" in question else "")
        )
        for company, period, (metric, aliases) in product(
            companies, periods, metric_specs
        ):
            self.items.append(
                EvidenceWorkItem(
                    item_id=f"item-{len(self.items) + 1}",
                    company=company,
                    period=period,
                    metric=metric,
                    aliases=aliases,
                    statement_scope=scope,
                    quarter=scope_quarter(question),
                )
            )

    def refresh(
        self, evidence: list[dict], calculations: list[dict], store: FinanceStore
    ) -> None:
        """只在明确读过的目标行列里找候选；定位项、错位和截断结果不认证。"""
        observed_cells = self._observed_cells(evidence, store)
        for item in self.items:
            item.evidence = [
                cell
                for cell in observed_cells
                if (
                    cell["company"] == item.company
                    and cell["period"] == item.period
                    and cell["label"]
                    in {_normalize_label(alias) for alias in item.aliases}
                    and (not item.quarter or cell["quarter"] == item.quarter)
                    and (
                        not item.statement_scope
                        or cell["statement_scope"] == item.statement_scope
                    )
                )
            ]
            signatures = {
                (
                    cell["normalized_value"],
                    cell["normalized_unit"],
                    cell["statement_scope"],
                )
                for cell in item.evidence
            }
            if len(signatures) > 1:
                item.status, item.reason = (
                    "conflict",
                    "同一待查项存在不同数值或口径，请核对冲突",
                )
            elif item.evidence and all(cell["unit"] for cell in item.evidence):
                item.status, item.reason = (
                    "evidence_found",
                    "已读取对应指标与期间，结论仍须独立校验",
                )
            elif item.evidence:
                item.status, item.reason = "pending", "已找到数值，但单位尚不明确"
            else:
                item.status, item.reason = (
                    "pending",
                    "尚未取得目标指标、期间和口径的行列证据",
                )
        self.calculation_count = len(calculations)

    def observe_progress(self, record: dict, calculations: list[dict]) -> None:
        """连续无新增证据、定位或计算时停止空转，允许不同待查项正常搜索。"""
        calculation_keys = {
            json.dumps(
                {"formula": value.get("formula"), "inputs": value.get("inputs")},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            for value in calculations
        }
        evidence_keys = {
            cell["evidence_id"] for item in self.items for cell in item.evidence
        }
        progressed = (
            bool(evidence_keys - self._seen_evidence_keys)
            or bool(calculation_keys - self._calculation_keys)
            or bool(record.get("ok") and int(record.get("new_result_count") or 0) > 0)
        )
        self.no_progress_steps = 0 if progressed else self.no_progress_steps + 1
        self._seen_evidence_keys.update(evidence_keys)
        self._calculation_keys.update(calculation_keys)
        if self.no_progress_steps >= 3:
            self.stop_reason = "no_progress"

    def snapshot(self) -> dict:
        pending = [item for item in self.items if item.status != "evidence_found"]
        calculation_missing = self.requires_calculation and not self.calculation_count
        return {
            "version": TASK_PLAN_VERSION,
            "status": "incomplete"
            if pending or calculation_missing
            else ("covered" if self.items else "untracked"),
            "coverage_scope": "仅跟踪可识别科目的证据覆盖，不认证答案语义或计算口径",
            "untracked_metrics": list(self.untracked_metrics),
            "calculation_required": self.requires_calculation,
            "calculation_recorded": bool(self.calculation_count),
            "no_progress_steps": self.no_progress_steps,
            "stop_reason": self.stop_reason,
            "items": [
                {
                    "id": item.item_id,
                    "company": item.company,
                    "period": item.period,
                    "metric": item.metric,
                    "statement_scope": item.statement_scope,
                    "quarter": item.quarter,
                    "status": item.status,
                    "reason": item.reason,
                    "evidence": list(item.evidence),
                }
                for item in self.items
            ],
        }

    def guidance(self) -> str:
        """给模型简短的下一步依据，避免每轮重复粘贴完整证据。"""
        if not self.items and not self.requires_calculation:
            return ""
        pending = [item for item in self.items if item.status != "evidence_found"]
        lines = [
            f"任务清单：已取得 {len(self.items) - len(pending)}/{len(self.items)} 项目标证据。"
        ]
        for item in pending[:8]:
            scope_label = {"parent": "母公司", "consolidated": "合并"}.get(
                item.statement_scope, "核对口径"
            )
            lines.append(
                f"- {item.item_id}：{item.company} {item.period}{item.quarter} {item.metric}（{scope_label}）：{item.reason}"
            )
        if self.requires_calculation and not self.calculation_count:
            lines.append(
                "- 尚无计算记录：先读齐输入，再调用 calculate；不得用心算补齐。"
            )
        if self.stop_reason:
            lines.append(
                "连续调用没有新增信息，停止重复工具调用；在回答中明确仍缺的项目。"
            )
        elif pending:
            lines.append("下一步只针对上述缺口定位、读取或核对；不要重复读取已覆盖项。")
        else:
            lines.append(
                "已知待查项的证据已覆盖，请检查计算与引用后收尾；清单不代表答案正确。"
            )
        return "\n".join(lines)

    @staticmethod
    def _observed_cells(evidence: list[dict], store: FinanceStore) -> list[dict]:
        observed: list[dict] = []
        for entry in evidence:
            if (
                entry.get("citable") is False
                or entry.get("truncated")
                or entry.get("misaligned")
            ):
                continue
            anchor = str(entry.get("chunk_id") or "")
            try:
                location = store.locate(anchor)
                table_id = location.get("table_id")
                if not table_id:
                    continue
                table = store.get_table(table_id)
                document = store.get_document(location["doc_id"])
            except (FinanceStoreError, KeyError):
                continue
            rows, columns = table.get("rows") or [], table.get("columns") or []
            row_ids = location.get("row_indices") or list(range(len(rows)))
            column_ids = location.get("column_indices") or list(range(1, len(columns)))
            for row_id in row_ids:
                if not 0 <= row_id < len(rows):
                    continue
                row = rows[row_id]
                label = FinanceStore._row_label(row)
                for column_id in column_ids:
                    if not 0 < column_id < min(len(columns), len(row)):
                        continue
                    column_label = str(columns[column_id])
                    if re.search(r"同比|增减|变动|占比|比重", column_label):
                        continue
                    values = record_number_tokens(str(row[column_id] or ""))
                    if len(values) != 1:
                        continue
                    value, unit = values[0]
                    unit_match = _PLAN_UNIT_PATTERN.search(label + column_label)
                    unit = unit or (
                        unit_match[0] if unit_match else str(table.get("unit") or "")
                    )
                    years = scope_years(column_label)
                    if not years and re.search(
                        r"本期|本年|期末|本报告期", column_label
                    ):
                        years = {str(document.get("period") or "")}
                    if not years and re.search(r"上期|上年|期初", column_label):
                        doc_year = str(document.get("period") or "")
                        years = (
                            {str(int(doc_year) - 1)} if doc_year.isdigit() else set()
                        )
                    normalized_value = value * Decimal(_PLAN_MONEY_SCALE.get(unit, 1))
                    for period in years:
                        observed.append(
                            {
                                "evidence_id": anchor,
                                "table_id": table_id,
                                "row": row_id,
                                "column": column_id,
                                "company": str(document.get("company") or ""),
                                "period": period,
                                "label": _normalize_label(label),
                                "statement_scope": str(
                                    table.get("statement_scope") or ""
                                ),
                                "quarter": scope_quarter(column_label),
                                "value": str(value),
                                "unit": unit,
                                "normalized_value": str(normalized_value.normalize()),
                                "normalized_unit": "元"
                                if unit in _PLAN_MONEY_SCALE
                                else unit,
                            }
                        )
        return observed
