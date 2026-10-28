"""拒答评测：该说"不知道"的时候，系统说了没有？

指标（比"幻觉率"更客观，逐条可人工复核）：
  - 干净拒答率  = 负样本中「明确说没有 + 未给出具体数值/规则」的比例
  - 编造率      = 负样本中「说没有但随后给了具体值」+「直接给具体值」的比例
  - 误拒率      = 正样本中「明明能答却拒答」的比例

用法：
    python -m evaluation.legacy.run_abstention_eval            # 全量 20 负 + 20 正
    python -m evaluation.legacy.run_abstention_eval --limit 5  # 快速冒烟

输出：
    backend/eval_abstention.json    机器可读结果
    backend/eval_abstention.csv     逐条明细（人工复核用）
"""
import argparse
import asyncio
import csv
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.services import rag_engine                                   # noqa: E402
from app.services import conversation_memory                          # noqa: E402
from app.services.deepseek import deepseek_client                     # noqa: E402
from evaluation import run_eval as RE                                 # noqa: E402
from evaluation.legacy.negative_samples import NEGATIVE_SAMPLES              # noqa: E402
from evaluation.sample_data import QA_PAIRS                           # noqa: E402

# 拒答关键词：与线上 rag_engine._CANNOT_ANSWER_KEYWORDS 同源，另补若干否定表述
REFUSAL_KEYWORDS = list(rag_engine._CANNOT_ANSWER_KEYWORDS) + [
    "未找到", "未提及", "未涉及", "没有相关", "无相关", "暂无", "未提供", "没有明确", "未作规定",
]

# 正样本：从 60 条里按固定步长抽 20 条（可复现）
POSITIVE_INDICES = list(range(0, len(QA_PAIRS), 3))[:20]

_JUDGE_SYSTEM = "你是严格的答案审核员，只输出 JSON。"

_JUDGE_TEMPLATE = """判断下面这条 AI 回答属于哪一类。问题在给定的企业制度资料里【没有对应答案】。

问题：{query}

回答：{answer}

分类定义：
- REFUSE_CLEAN：明确说明资料中没有/未规定，且没有给出任何具体数值、天数、比例、期限、金额、条款内容。
- REFUSE_WITH_REFERENCE：明确说明资料中没有/未规定，并且只引用资料里确实存在的其他规定作为参考（未针对本问题编造新数值）。
- REFUSE_THEN_FABRICATE：先说明没有规定，但随后又针对本问题给出了具体的数值/天数/比例/期限/金额/条款。
- ANSWER_FABRICATE：没有说资料不足，直接针对本问题给出了具体的数值/天数/比例/期限/金额/条款内容。
- ANSWER_IRRELEVANT：答非所问，既没说没有，也没有给具体值。

注意：「具体数值」指明确的阿拉伯数字、天数、比例、金额、期限，或一条可直接执行的新规则。
仅在复述资料里已有的管理规定、并未针对本问题补充新数值的，算 REFUSE_WITH_REFERENCE。

只输出：{{"label": "分类", "evidence": "回答中支撑该判断的原句片段"}}"""

_POS_JUDGE_TEMPLATE = """判断下面这条 AI 回答有没有回答问题。问题在给定的企业制度资料里【是有答案的】。

问题：{query}

回答：{answer}

分类定义：
- ANSWER_GROUNDED：给出了实质内容，回答了问题。
- REFUSE：以「无法确定 / 资料不足 / 未找到」等理由拒绝作答或表示资料不包含。
- ANSWER_VAGUE：既没拒答，也没有给出实质内容（如只复述问题、只给泛泛建议）。

只输出：{{"label": "分类", "evidence": "原句片段"}}"""


async def _judge_positive(query: str, answer: str) -> dict:
    prompt = _POS_JUDGE_TEMPLATE.format(query=query, answer=answer[:1200])
    try:
        data = await _llm_json(prompt)
        label = str(data.get("label", "")).strip().upper()
        if label not in {"ANSWER_GROUNDED", "REFUSE", "ANSWER_VAGUE"}:
            label = "UNKNOWN"
        return {"label": label, "evidence": str(data.get("evidence", ""))[:200]}
    except Exception as exc:
        return {"label": "UNKNOWN", "evidence": f"judge parse failed: {exc}"}


async def _llm_json(prompt: str, max_tokens: int = 3072, retries: int = 2) -> dict:
    """调用模型并解析 JSON；空返回（推理型模型可能把 token 全花在思考上）自动重试。"""
    last_err = ""
    for attempt in range(retries + 1):
        content, _reasoning, _usage = await deepseek_client.chat(
            [
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            reasoning_mode="flash",
            max_tokens=max_tokens + attempt * 1024,
        )
        text = (content or "").strip()
        if not text:
            last_err = "empty content"
            continue
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        try:
            return json.loads(text)
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    raise ValueError(f"judge 多次尝试仍未拿到合法 JSON：{last_err}")


_NEG_LABELS = {"REFUSE_CLEAN", "REFUSE_WITH_REFERENCE", "REFUSE_THEN_FABRICATE",
               "ANSWER_FABRICATE", "ANSWER_IRRELEVANT"}


async def _judge_label(query: str, answer: str) -> dict:
    prompt = _JUDGE_TEMPLATE.format(query=query, answer=answer[:1200])
    try:
        data = await _llm_json(prompt)
        label = str(data.get("label", "")).strip().upper()
        if label not in _NEG_LABELS:
            label = "UNKNOWN"
        return {"label": label, "evidence": str(data.get("evidence", ""))[:200]}
    except Exception as exc:
        return {"label": "UNKNOWN", "evidence": f"judge parse failed: {exc}"}


def _keyword_refusal(answer: str) -> bool:
    return any(k in answer for k in REFUSAL_KEYWORDS)


async def _ask(query: str) -> dict:
    """走线上 rag_query 主链路，但关掉 fallback（否则拒答会被通用模型答案替换掉）。"""
    t0 = time.time()
    resp = await rag_engine.rag_query(
        query=query,
        conversation_id=str(uuid4()),
        top_k=5,
        use_fallback=False,
    )
    return {
        "answer": resp.answer or "",
        "citations": len(resp.citations or []),
        "confidence": float(resp.confidence_score or 0),
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


def _summarize(rows: list) -> dict:
    neg = [r for r in rows if r["kind"] == "negative"]
    pos = [r for r in rows if r["kind"] == "positive"]
    clean_ref = [r for r in neg if r["label"] in {"REFUSE_CLEAN", "REFUSE_WITH_REFERENCE"}]
    fab = [r for r in neg if r["label"] in {"REFUSE_THEN_FABRICATE", "ANSWER_FABRICATE"}]
    pos_refused = [r for r in pos if r["label"] in {"REFUSE", "REFUSE_CLEAN"}]
    n_neg, n_pos = len(neg), len(pos)
    return {
        "negative_n": n_neg,
        "positive_n": n_pos,
        "clean_refusal_rate": round(len(clean_ref) / n_neg, 4) if n_neg else 0,
        "fabrication_rate": round(len(fab) / n_neg, 4) if n_neg else 0,
        "false_refusal_rate": round(len(pos_refused) / n_pos, 4) if n_pos else 0,
        "negative_label_dist": {k: sum(1 for r in neg if r["label"] == k) for k in
                                ("REFUSE_CLEAN", "REFUSE_WITH_REFERENCE", "REFUSE_THEN_FABRICATE",
                                 "ANSWER_FABRICATE", "ANSWER_IRRELEVANT", "UNKNOWN")},
        "positive_label_dist": {k: sum(1 for r in pos if r["label"] == k) for k in
                                ("ANSWER_GROUNDED", "REFUSE", "ANSWER_VAGUE", "UNKNOWN")},
    }


def _write_outputs(rows: list) -> dict:
    summary = _summarize(rows)
    out_json = _BACKEND_DIR / "eval_abstention.json"
    out_csv = _BACKEND_DIR / "eval_abstention.csv"
    out_json.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=["kind", "query", "label", "citations", "confidence",
                                               "keyword_refusal", "evidence", "answer", "why", "fabrication_hint"])
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
    print("\n=== 拒答评测结果 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n写入 {out_json}\n写入 {out_csv}")
    return summary


async def _rejudge() -> None:
    """只重新判定已有答案，不重跑检索与生成（改 judge 后校准用）。"""
    path = _BACKEND_DIR / "eval_abstention.json"
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    for i, r in enumerate(rows, 1):
        judge = (await _judge_label(r["query"], r["answer"])) if r["kind"] == "negative" \
            else (await _judge_positive(r["query"], r["answer"]))
        r.update(judge)
        print(f"[{i}/{len(rows)}] {r['kind'][:3]} {r['label']:24} {r['query'][:28]}", flush=True)
    _write_outputs(rows)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（冒烟用）")
    ap.add_argument("--rejudge", action="store_true",
                    help="不重跑检索与生成，只用现有答案重新判定（改 judge 后校准用）")
    args = ap.parse_args()

    if args.rejudge:
        await _rejudge()
        return

    # 评测不需要会话记忆，且避免依赖 Redis
    async def _noop(*a, **kw):
        return None

    conversation_memory.add_turn = _noop

    negatives = NEGATIVE_SAMPLES[: args.limit] if args.limit else NEGATIVE_SAMPLES
    pos_idx = POSITIVE_INDICES[: args.limit] if args.limit else POSITIVE_INDICES

    tmp_root = Path(tempfile.mkdtemp(prefix="km_abstain_"))
    await RE._build_real_indexes(tmp_root)
    print(f"索引就绪；负样本 {len(negatives)} 条，正样本 {len(pos_idx)} 条", flush=True)

    rows = []
    try:
        for i, item in enumerate(negatives, 1):
            r = await _ask(item["query"])
            judge = await _judge_label(item["query"], r["answer"])
            rows.append({
                "kind": "negative", "query": item["query"], "why": item["why"],
                "fabrication_hint": item["fabrication_hint"], **r,
                "keyword_refusal": _keyword_refusal(r["answer"]), **judge,
            })
            print(f"[neg {i}/{len(negatives)}] {judge['label']:22} cites={r['citations']} {r['elapsed_ms']}ms", flush=True)

        for i, idx in enumerate(pos_idx, 1):
            qa = QA_PAIRS[idx]
            r = await _ask(qa["query"])
            judge = await _judge_positive(qa["query"], r["answer"])
            rows.append({
                "kind": "positive", "query": qa["query"], "why": "", "fabrication_hint": "",
                **r, "keyword_refusal": _keyword_refusal(r["answer"]), **judge,
            })
            print(f"[pos {i}/{len(pos_idx)}] {judge['label']:22} cites={r['citations']} {r['elapsed_ms']}ms", flush=True)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    _write_outputs(rows)


if __name__ == "__main__":
    asyncio.run(main())
