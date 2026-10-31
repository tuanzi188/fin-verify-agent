"""运行清单测试：不联网、不调模型，只验证可复现字段与顺序敏感性。"""

import json

from evaluation.provenance import (
    build_run_manifest,
    git_provenance,
    items_fingerprint,
    model_snapshot,
)


def test_items_fingerprint_is_order_sensitive():
    left = [{"chunk_id": "a", "content": "1"}, {"chunk_id": "b", "content": "2"}]
    right = list(reversed(left))
    assert items_fingerprint(left) != items_fingerprint(right)
    assert items_fingerprint(left) == items_fingerprint(list(left))


def test_model_snapshot_never_contains_api_key():
    snapshot = model_snapshot()
    assert "api_key" not in json.dumps(snapshot, ensure_ascii=False).lower()
    assert snapshot["model"]
    assert snapshot["provider"]


def test_build_run_manifest_has_reproducibility_fields(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text('{"id": "T1", "question": "x"}\n', encoding="utf-8")

    manifest = build_run_manifest(
        tasks_path=tasks,
        corpus_hash="abc123",
        corpus_stats={"documents": 7},
        index_snapshot={"kind": "structured_blocks", "fingerprint": "idx"},
        chunking={"body_chars": [400, 800]},
        generation={"temperature": 0.1},
        extra={"arm": "agent"},
    )

    assert manifest["code"] == git_provenance()
    assert manifest["model"]["model"]
    assert manifest["tasks"]["sha256"]
    assert manifest["corpus"]["sha256"] == "abc123"
    assert manifest["index"]["fingerprint"] == "idx"
    assert manifest["chunking"]["body_chars"] == [400, 800]
    assert manifest["generation"]["temperature"] == 0.1
    assert manifest["arm"] == "agent"
    assert manifest["artifacts"]["raw_traces"] == "results[*].trace"
