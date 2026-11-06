"""评测运行清单：把一次运行绑定到代码、模型、语料、索引与切片参数。

为什么单独一层：`run_config` 只记了入口参数，报告里的数字仍然回答不了
"这是哪个 commit / 哪份索引 / 哪个模型端点跑出来的"。工作区一旦有未提交改动，
只写 commit 会误导；索引参数改了而没进清单，两次运行也不可比。

这里只做快照，不做判分。字段刻意保持可 JSON 序列化，直接写进 runs 文件。
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    """字符串内容指纹：用于把 runs 绑定到「实际发给模型的 system prompt」。

    提示词是多次实验里唯一变化的量，但 `git.commit` 在脏工作区里区分不出
    哪一版提示词跑的。取拼接后的最终文本（含 variant / search_mode 带来的
    `.replace` 与后缀）做 hash，比取 prompts.py 里的常量更贴近运行时。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def items_fingerprint(
    items: Iterable[dict],
    *,
    id_key: str = "chunk_id",
    content_key: str = "content",
) -> str:
    """有序内容指纹：顺序变了也要变，不能只对集合做 hash。"""
    digest = hashlib.sha256()
    for item in items:
        digest.update(
            f"{item.get(id_key)}|{item.get(content_key)}".encode()
        )
    return digest.hexdigest()


def git_provenance(cwd: Path | None = None) -> dict:
    """代码版本。脏工作区必须显式标 dirty，否则 commit 不能代表实际代码。"""
    cwd = cwd or _BACKEND_DIR
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False, cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=False, cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=False, cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        return {
            "commit": (commit.stdout or "").strip() or None,
            "branch": (branch.stdout or "").strip() or None,
            "dirty": bool((status.stdout or "").strip()),
        }
    except Exception:  # noqa: BLE001 - 取不到版本不该让评测失败
        return {"commit": None, "branch": None, "dirty": None}


def model_snapshot() -> dict:
    """模型与端点快照。只记 base_url，不记 api_key。"""
    from app.config import DEEPSEEK_CONFIG, MODEL_CONFIG, MODEL_PROVIDER

    flash = MODEL_CONFIG.get("flash") or {}
    endpoints = [
        str(config.get("base_url") or "")
        for config in DEEPSEEK_CONFIG.values()
        if config.get("base_url")
    ]
    return {
        "provider": MODEL_PROVIDER,
        "model": flash.get("model"),
        "max_tokens": flash.get("max_tokens"),
        "configured_temperature": flash.get("temperature"),
        "endpoints": endpoints,
    }


def build_run_manifest(
    *,
    tasks_path: Path,
    corpus_hash: str,
    corpus_stats: dict,
    index_snapshot: dict | None = None,
    chunking: dict | None = None,
    generation: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """一次运行的完整快照。原始轨迹就在 runs 文件的 `results[*].trace`。"""
    manifest: dict[str, Any] = {
        "schema": "finance_run_manifest/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "code": git_provenance(),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "model": model_snapshot(),
        "tasks": {
            "path": str(tasks_path),
            "sha256": sha256_file(tasks_path),
        },
        "corpus": {
            "sha256": corpus_hash,
            "stats": corpus_stats,
        },
        "index": index_snapshot or {},
        "chunking": chunking or {},
        "generation": generation or {},
        "artifacts": {
            "raw_traces": "results[*].trace",
            "evidence": "results[*].evidence",
            "answers": "results[*].answer",
        },
    }
    if extra:
        manifest.update(extra)
    return manifest
