"""辅助问答评测的离线夹具。

两件事：

1. **拒答用的最小语料**：`prepare_finance_scope` / `initial_decision` 只读
   `documents` 里的 company / period / doc_type，不需要真实 PDF。这里给一份
   甲集团 2025 年报，够把六类拒答跑成零模型。
2. **联网题的按 query 分桶回放**：产品自带的 `FixtureProvider` 忽略 query，
   而费率题会发两条改写查询（「… 最新规定」「… 官方公告」）。这里用
   `match_any` 关键字匹配，而不是精确 query，避免一改写就回放不到。

夹具文件格式（JSON）：

    {
      "schema": "assist_web_replay/v1",
      "entries": [
        {"match_any": ["印花税"], "sources": [
          {"url": "https://www.chinatax.gov.cn/...", "title": "...",
           "publishedDate": "2023-08-27", "text": "..."}
        ]}
      ]
    }

`match_any` 任一命中即采用该条目的 sources；多条命中按顺序合并去重。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


DEMO_DOCUMENTS: list[dict[str, Any]] = [
    {
        "doc_id": "demo-000001-2025-annual",
        "company": "甲集团",
        "code": "000001",
        "period": "2025",
        "doc_type": "annual_report",
        "title": "甲集团2025年年度报告",
    }
]

REPLAY_SCHEMA = "assist_web_replay/v1"


def fixture_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def load_replay(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path}: 夹具必须是 JSON 对象")
    if payload.get("schema") != REPLAY_SCHEMA:
        raise SystemExit(f"{path}: schema 必须是 {REPLAY_SCHEMA}")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SystemExit(f"{path}: entries 必须是列表")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SystemExit(f"{path}: entries[{index}] 不是对象")
        if not entry.get("match_any"):
            raise SystemExit(f"{path}: entries[{index}] 缺少 match_any")
        if not isinstance(entry.get("sources"), list):
            raise SystemExit(f"{path}: entries[{index}] sources 必须是列表")
    return payload


class ReplayProvider:
    """按 `match_any` 关键字回放的 WebSearchProvider。

    每个实例一个独立 `cache_tag`：同一进程里跑两套夹具时，不能互相命中
    `web_search` 的 10 分钟缓存。
    """

    name = "replay"
    _instance_counter = 0

    def __init__(self, replay: dict[str, Any], *, tag: str = "") -> None:
        self.entries = list(replay.get("entries") or [])
        ReplayProvider._instance_counter += 1
        self.cache_tag = f"replay:{tag or ReplayProvider._instance_counter}:{ReplayProvider._instance_counter}"

    def _matched(self, query: str) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in self.entries:
            if not any(word in query for word in entry.get("match_any") or []):
                continue
            for source in entry.get("sources") or []:
                url = str(source.get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                matched.append(dict(source))
        return matched

    def search(
        self,
        query: str,
        *,
        domains: list[str],
        num_results: int = 10,
        timeout: float = 10.0,
    ) -> list[dict[str, Any]]:
        return self._matched(query)[: max(1, int(num_results))]


def make_replay_provider(replay: dict[str, Any], *, tag: str = "") -> ReplayProvider:
    return ReplayProvider(replay, tag=tag)


def load_replay_provider(path: Path) -> ReplayProvider:
    replay = load_replay(path)
    return make_replay_provider(replay, tag=fixture_sha256(path)[:12])


def iter_sources(replay: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for entry in replay.get("entries") or []:
        yield from entry.get("sources") or []
