"""judge 审计表测试：要点行 + 任务级行 + CSV 回读。

为什么要有任务级行：负样本和澄清题没有答案要点，judge 的判定落在
forbidden_hit / clarify_verdict 上；2026-10-28 实测这两项最容易翻车
（FIN-15 因为答案提到"2025 年 Q1 是 X"被判命中禁止内容）。
只标要点的话，这类分歧永远进不了审计表。
"""

import csv
import io
import json

from evaluation.judge_audit import build_rows, compare


def _report(verdicts):
    return {"verdicts": verdicts}


def _judgment(points=None, forbidden=False, clarify="na"):
    return {
        "points": points or [],
        "forbidden_hit": forbidden,
        "forbidden_reason": "给了具体数值",
        "grounded": True,
        "grounded_reason": "",
        "clarify_verdict": clarify,
        "clarify_reason": "",
    }


def test_build_rows_includes_task_level_for_negative_and_clarify():
    tasks = {
        "N1": {"id": "N1", "should_abstain": True, "answer_points": [], "clarify_mode": "none"},
        "C1": {"id": "C1", "should_abstain": False, "clarify_mode": "ask",
               "answer_points": []},
        "P1": {"id": "P1", "should_abstain": False, "clarify_mode": "none",
               "answer_points": [{"text": "要点甲"}]},
    }
    report = _report([
        {"task_id": "N1", "run_index": 1, "dimension": "拒答与边界", "answer": "答案",
         "judgment": _judgment(forbidden=True)},
        {"task_id": "C1", "run_index": 1, "dimension": "澄清与追问", "answer": "答案",
         "judgment": _judgment(clarify="ok")},
        {"task_id": "P1", "run_index": 1, "dimension": "检索与引用", "answer": "答案",
         "judgment": _judgment(points=[{"index": 0, "pass": True, "cited": True,
                                        "citation_supports": True, "reason": "ok"}])},
    ])

    rows = build_rows(report, tasks)
    by_task = {row["task_id"]: row for row in rows}
    assert set(by_task) == {"N1", "C1", "P1"}
    # 负样本命中禁止内容 → judge 认为不合格
    assert by_task["N1"]["point_index"] == -1 and by_task["N1"]["judge_pass"] is False
    # 澄清判 ok → judge 认为合格
    assert by_task["C1"]["point_index"] == -1 and by_task["C1"]["judge_pass"] is True
    # 正样本没有任务级行，只有要点行
    assert by_task["P1"]["point_index"] == 0 and by_task["P1"]["judge_pass"] is True


def test_compare_reads_csv_and_separates_loose_from_strict(tmp_path):
    columns = ("task_id", "point_index", "judge_pass", "human_pass", "note", "dimension")
    rows = [
        {"task_id": "T1", "point_index": 0, "judge_pass": "True", "human_pass": "true",
         "note": "", "dimension": "检索与引用"},
        {"task_id": "T2", "point_index": 0, "judge_pass": "True", "human_pass": "false",
         "note": "答案没给这个数", "dimension": "检索与引用"},
        {"task_id": "T3", "point_index": 0, "judge_pass": "False", "human_pass": "true",
         "note": "", "dimension": "多步拆解"},
        {"task_id": "T4", "point_index": 0, "judge_pass": "False", "human_pass": "skip",
         "note": "", "dimension": "多步拆解"},
    ]
    path = tmp_path / "labels.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    assert compare(path) == 0


def test_compare_refuses_when_nothing_labelled(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("task_id,judge_pass,human_pass\nT1,True,\n", encoding="utf-8")
    assert compare(path) == 2
