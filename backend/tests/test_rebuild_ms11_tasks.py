"""rebuild_ms11_tasks：11 题合并输入卡的离线契约测试。"""

import json

from evaluation import rebuild_ms11_tasks as r


def test_build_returns_exactly_11_in_fixed_order():
    by_id = {tid: {"id": tid, "question": "x"} for tid in r.MS11_IDS}
    tasks = r.build(by_id)
    assert len(tasks) == 11
    assert [t["id"] for t in tasks] == list(r.MS11_IDS)


def test_build_fails_loudly_on_missing_id():
    by_id = {tid: {"id": tid} for tid in r.MS11_IDS[:-1]}
    try:
        r.build(by_id)
    except SystemExit as exc:
        assert "FIN-S10" in str(exc)
    else:
        raise AssertionError("缺 id 时必须报错，不能静默少跑一题")


def test_load_all_covers_all_11_from_real_task_files():
    by_id = r.load_all()
    missing = [tid for tid in r.MS11_IDS if tid not in by_id]
    assert missing == []
    # 三套任务卡都确实被读到了（各自至少贡献一题）
    for tid in ("FIN-07", "CX-07", "FIN-S01"):
        assert tid in by_id


def test_output_is_valid_jsonl():
    by_id = r.load_all()
    tasks = r.build(by_id)
    text = "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n"
    parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert len(parsed) == 11
    assert all(isinstance(t, dict) and "id" in t for t in parsed)
