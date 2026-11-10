"""run_finance_eval 任务集快照测试：不调模型，只验证落盘产物的可复现字段。

旧实现的 run_config.tasks_file 指向筛选前的 --tasks 路径（可能是 %TEMP% 下的
合并临时文件），--ids/--holdout/--limit 筛选后路径与哈希都不对应实际任务集。
本测试锁住：快照内容 == 实际跑的任务集、run_config 指向它、哈希匹配。
"""

import json
import sys

import pytest

from evaluation import run_finance_eval as rfe


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """替换掉所有重依赖，只保留 main() 的任务筛选与落盘逻辑。"""
    tasks_file = tmp_path / "source_tasks.jsonl"
    tasks = [
        {"id": "A", "question": "a", "holdout": False},
        {"id": "B", "question": "b", "holdout": True},
        {"id": "C", "question": "c", "holdout": False},
    ]
    tasks_file.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n",
        encoding="utf-8",
    )

    class FakeBlockIndex:
        def stats(self):
            return {"blocks": 1}

    class FakeToolbox:
        block_index = FakeBlockIndex()

    class FakeToolset:
        name = "finance"
        search_mode = "blocks"
        max_tool_rounds = 8
        toolbox = FakeToolbox()

        def __init__(self, store, search_mode="sections"):
            self.search_mode = search_mode

        def system_prompt(self, variant):
            return "prompt-v1"

    def fake_run_batch(tasks_arg, **kwargs):
        payload = ([{"task_id": t["id"], "status": "answered", "usage": {},
                     "trace": [], "answer": "", "citations": []} for t in tasks_arg], {})

        async def _coro():
            return payload

        # main() 里是 asyncio.run(run_batch(...))，所以必须返回协程/可等待对象
        return _coro()

    monkeypatch.setattr(rfe, "FinanceStore", lambda *a, **k: object())
    monkeypatch.setattr(rfe, "FinanceToolset", FakeToolset)
    monkeypatch.setattr(rfe, "DOCUMENT_DIR", "fake")
    monkeypatch.setattr(rfe, "doc_inventory", lambda: [{"doc": 1}] * 7)
    monkeypatch.setattr(rfe, "corpus_hash", lambda: "hash123")
    monkeypatch.setattr(rfe, "run_batch", fake_run_batch)
    monkeypatch.setattr(rfe, "run_agent", None)
    monkeypatch.setattr(rfe, "print_table", lambda results: None)
    monkeypatch.setattr(rfe, "summarize", lambda results, problems: {
        "total": len(results), "status_counts": {}, "median_latency_ms": 0,
        "avg_steps": 0, "injection_untriggered_runs": 0,
    })
    monkeypatch.setattr(rfe, "build_run_manifest", lambda **k: {"tasks_path": str(k["tasks_path"])})
    monkeypatch.setattr(rfe, "block_parameters", lambda: {})
    monkeypatch.setattr(rfe, "MODEL_CONFIG", {"flash": {"model": "m"}})
    monkeypatch.setattr(rfe, "MODEL_PROVIDER", "test")
    # 不依赖真实 git 状态；断言 run_config 里确实带上了版本字段
    monkeypatch.setattr(rfe, "git_provenance",
                        lambda: {"commit": "abc123", "branch": "test", "dirty": False})
    # 留出题使用登记会写 docs/evidence/holdout_usage.json，测试不应污染仓库
    import evaluation.holdout_registry as hr
    monkeypatch.setattr(hr, "record_usage", lambda ids, out: None)

    return tmp_path, tasks_file


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["run_finance_eval", *argv])
    return rfe.main()


def test_snapshot_matches_filtered_tasks_and_run_config_points_to_it(fake_env, monkeypatch):
    tmp_path, tasks_file = fake_env
    out = tmp_path / "runs.json"

    assert _run(monkeypatch, "--tasks", str(tasks_file), "--out", str(out),
                "--holdout", "exclude") == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    rc = payload["run_config"]
    snapshot = tmp_path / "runs.tasks.jsonl"

    assert snapshot.exists()
    assert rc["tasks_file"] == str(snapshot)
    assert rc["task_snapshot"] is True
    assert rc["tasks_sha256"] == rfe.sha256_file(snapshot)
    assert rc["tasks_source_file"] == str(tasks_file)
    assert rc["tasks_selection"]["task_count"] == 2  # A + C，B 是 holdout
    # 产物自带代码版本，不需要打开 manifest 或查文档
    assert rc["git"] == {"commit": "abc123", "branch": "test", "dirty": False}
    # 提示词绑定：三次格式探针的唯一变量就是 system prompt，缺此字段时
    # dirty=true 的 commit 区分不出跑了哪一版（AGENTS.md §9.2）。
    assert rc["prompt_sha256"] == rfe.sha256_text("prompt-v1")
    assert rc["prompt_variant"] == "improved"
    # 默认与线上一致的结构块检索，同时绑定任务清单开关。
    assert rc["prompt_search_mode"] == "blocks"
    assert rc["task_plan_enabled"] is True
    assert rc["task_plan_version"] == "finance-task-plan-v1"
    assert rc["prompt_chars"] == len("prompt-v1")

    snapshot_tasks = [json.loads(line)
                      for line in snapshot.read_text(encoding="utf-8").splitlines() if line.strip()]
    # --holdout exclude 后只剩 A、C（B 是留出题）
    assert snapshot_tasks == [
        {"id": "A", "question": "a", "holdout": False},
        {"id": "C", "question": "c", "holdout": False},
    ]
    assert [t["id"] for t in snapshot_tasks] == ["A", "C"]


def test_no_task_snapshot_keeps_source_path(fake_env, monkeypatch):
    tmp_path, tasks_file = fake_env
    out = tmp_path / "runs2.json"

    assert _run(monkeypatch, "--tasks", str(tasks_file), "--out", str(out),
                "--holdout", "exclude", "--no-task-snapshot") == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    rc = payload["run_config"]
    assert rc["task_snapshot"] is False
    assert rc["tasks_file"] == str(tasks_file)
    assert not (tmp_path / "runs2.tasks.jsonl").exists()


def test_task_plan_ablation_is_recorded(fake_env, monkeypatch):
    directory, tasks_file = fake_env
    output = directory / "without-plan.json"
    assert _run(monkeypatch, "--tasks", str(tasks_file), "--out", str(output),
                "--disable-task-plan", "--search-mode", "sections") == 0
    configuration = json.loads(output.read_text(encoding="utf-8"))["run_config"]
    assert configuration["task_plan_enabled"] is False
    assert configuration["search_mode"] == "sections"


def test_ids_selection_is_recorded(fake_env, monkeypatch):
    tmp_path, tasks_file = fake_env
    out = tmp_path / "runs3.json"

    assert _run(monkeypatch, "--tasks", str(tasks_file), "--out", str(out),
                "--ids", "B", "--holdout", "include") == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    rc = payload["run_config"]
    assert rc["tasks_selection"]["ids"] == "B"
    assert rc["tasks_selection"]["task_count"] == 1
    snapshot_tasks = [json.loads(line)
                      for line in (tmp_path / "runs3.tasks.jsonl").read_text(encoding="utf-8").splitlines()
                      if line.strip()]
    assert [t["id"] for t in snapshot_tasks] == ["B"]
