"""任务集维护工具：坐标重绑 / 留出登记 / Markdown 渲染。"""

from __future__ import annotations

from datetime import datetime

from evaluation.holdout_registry import load_usage, record_usage
from evaluation.rebind_coords import rebind_task
from evaluation.render_tasks import render_task

DOCUMENTS = [
    {
        "doc_id": "docA",
        "tables": [
            {"table_id": "docA#t0001"},
            {"table_id": "docA#t0002"},
            {"table_id": "docA#t0003"},
        ],
        "sections": [],
    }
]


class FakeStore:
    """只实现重绑用到的 get_cell：{(table_id, row, column): 值}。"""

    def __init__(self, cells: dict[tuple, str]):
        self.cells = cells

    def get_cell(self, table_id: str, row: str, column: str) -> dict:
        key = (table_id, row, column)
        if key not in self.cells:
            raise KeyError(f"{table_id} {row}/{column}")
        return {"cell_text": self.cells[key]}


def make_task() -> dict:
    return {
        "id": "T1",
        "relevant_chunk_ids": ["docA#t0001"],
        "answer_points": [
            {
                "text": "营业收入 1,234",
                "chunk_ids": ["docA#t0001"],
                "evidence": {
                    "table_id": "docA#t0001",
                    "row": "营业收入",
                    "column": "2025 年",
                    "value": "1,234",
                },
            }
        ],
    }


def test_rebind_unique_signature_updates_all_refs():
    store = FakeStore({("docA#t0002", "营业收入", "2025 年"): "1,234"})
    task = make_task()
    changed, changes, manual = rebind_task(task, store, DOCUMENTS)
    assert changed and not manual, (changes, manual)
    assert task["answer_points"][0]["evidence"]["table_id"] == "docA#t0002"
    assert task["answer_points"][0]["chunk_ids"] == ["docA#t0002"]
    assert task["relevant_chunk_ids"] == ["docA#t0002"]


def test_rebind_keeps_original_table_when_still_valid():
    store = FakeStore({("docA#t0001", "营业收入", "2025 年"): "1,234"})
    task = make_task()
    changed, _changes, manual = rebind_task(task, store, DOCUMENTS)
    assert not changed and not manual
    assert task["answer_points"][0]["evidence"]["table_id"] == "docA#t0001"


def test_rebind_reports_manual_when_signature_missing():
    store = FakeStore({})
    task = make_task()
    changed, _changes, manual = rebind_task(task, store, DOCUMENTS)
    assert not changed
    assert any("找不到内容签名" in item for item in manual)
    assert task["answer_points"][0]["evidence"]["table_id"] == "docA#t0001"


def test_rebind_reports_manual_on_ambiguous_signature():
    store = FakeStore(
        {
            ("docA#t0002", "营业收入", "2025 年"): "1,234",
            ("docA#t0003", "营业收入", "2025 年"): "1,234",
        }
    )
    task = make_task()
    changed, _changes, manual = rebind_task(task, store, DOCUMENTS)
    assert not changed
    assert any("命中多张表" in item for item in manual)


def test_holdout_registry_records_and_loads(tmp_path):
    registry = tmp_path / "holdout_usage.json"
    record_usage(
        ["FIN-05"], "run1.json",
        when=datetime(2026, 11, 3, 12, 0, 0), path=registry,
    )
    record_usage(
        ["FIN-05"], "run2.json",
        when=datetime(2026, 11, 3, 13, 0, 0), path=registry,
    )
    usage = load_usage(path=registry)
    assert usage["FIN-05"]["count"] == 2
    assert usage["FIN-05"]["run"] == "run2.json"


def test_render_task_contains_key_sections():
    text = render_task(
        {
            "id": "T1",
            "dimension": "检索与引用",
            "difficulty": "easy",
            "question": "美的集团 2025 年营业收入是多少？",
            "answer_points": [
                {
                    "text": "456,451,731 千元",
                    "evidence": {
                        "table_id": "docA#t0001",
                        "row": "营业收入",
                        "column": "2025 年",
                        "value": "456,451,731",
                    },
                }
            ],
            "forbidden": ["编造数字"],
        },
        "profile",
    )
    assert "T1" in text
    assert "美的集团 2025 年营业收入是多少？" in text
    assert "456,451,731" in text
    assert "编造数字" in text
