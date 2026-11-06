"""判分入口的默认口径锁：语料与任务集只能是金融域。

背景：通用 RAG 下线时，`--corpus` 的默认值一直指向已删除的制度库语料，
`--tasks` 也指向已删除的 `agent_tasks.jsonl`。照 README 命令跑会拿错原文判分，
而且不报错——这类漂移必须由测试挡住。
"""

import pytest

from evaluation.score_agent_runs import build_parser


def test_scorer_defaults_to_finance_tasks_and_finance_corpus():
    args = build_parser().parse_args(["--runs", "runs.json"])

    assert args.tasks == "evaluation/finance_tasks.jsonl"
    assert args.corpus == "finance"
    # 历史复现命令里仍带 --corpus finance，必须继续可用
    assert build_parser().parse_args(["--runs", "runs.json", "--corpus", "finance"]).corpus == "finance"


def test_scorer_rejects_policy_corpus():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--runs", "runs.json", "--corpus", "policy"])
