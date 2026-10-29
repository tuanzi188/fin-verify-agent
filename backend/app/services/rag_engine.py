import re
import asyncio
import logging
from typing import List, Optional, AsyncIterator

from app.config import RETRIEVAL_CONFIG, SERVER_CONFIG
from app.core.cache import TimedLRUCache
from app.core.user_context import UserContext
from app.services.access_control import filter_accessible_records
from app.services.model_provider import (
    fallback_query as model_fallback_query,
    rag_query as model_rag_query,
    chat_stream as model_chat_stream,
    rag_query_stream as model_rag_query_stream,
    classify_intent as model_classify_intent,
    rewrite_query as model_rewrite_query,
    select_reasoning_mode as model_select_reasoning_mode,
)
from app.services.hybrid_search import hybrid_retriever
from app.services.reranker import reranker
from app.services.vector_store import vector_store
from app.services.conversation_memory import conversation_memory
from app.services.prompts import FALLBACK_SYSTEM_PROMPT
from app.models.chat import Citation, ChatSegment, ChatResponse

logger = logging.getLogger(__name__)

# 查询级缓存：降低重复查询的 LLM 调用与检索开销
_intent_cache = TimedLRUCache(maxsize=256, ttl_seconds=300)
_rewrite_cache = TimedLRUCache(maxsize=256, ttl_seconds=300)


def clear_query_caches():
    """文档上传/删除后调用：意图分类与查询改写不依赖语料，但保持三类缓存同步失效便于推理一致性。"""
    _intent_cache.clear()
    _rewrite_cache.clear()

_ACL_OVERFETCH_MULTIPLIER = 5

_CITATION_PATTERN = re.compile(r'\[引用:\s*(.+?),\s*(.+?)\]')
_CITE_INDEX_PATTERN = re.compile(r'\[引用:\s*(\d+(?:\s*[,，、]\s*\d+)*)\]')
_CANNOT_ANSWER_KEYWORDS = ["无法确定", "无法回答", "无法提供",
                          "无法据此", "没有任何一份", "均未提供", "资料不足", "没有相关信息", "不清楚", "不知道", "抱歉",
                          # 金融文档里的"拒答"常写成"未披露 / 未提及"：这不是作答，是边界。
                          # 不加会把"诚实说没披露"判成"answered"，abstain 维度整段失真。
                          "未披露", "未提及", "未列示",
                          # 期间边界："本地资料不包含该期间"同样是拒答。
                          "不包含", "不含"]
_GENERIC_ANSWER_PATTERNS = ["我是KnowledgeMind企业知识库AI助手", "请问有什么可以帮助你的", "请提供具体问题", "请提供更多信息"]


async def _execute_retrieval_pipeline(
    query: str,
    conversation_id: str,
    top_k: int = 5,
    reasoning_mode: Optional[str] = None,
    user_context: Optional[UserContext] = None,
) -> dict:
    cached_intent = _intent_cache.get(query)
    if cached_intent is not None:
        intent = cached_intent
    else:
        intent = await model_classify_intent(query)
        _intent_cache.set(intent, query)
    if reasoning_mode is None:
        reasoning_mode = model_select_reasoning_mode(intent)

    query_type = intent.get("query_type", "complex_analysis")
    profile = hybrid_retriever.query_type_to_profile(query_type)
    retrieval_queries = await _build_retrieval_queries(query, intent)

    retrieved_docs = await _retrieve_documents_with_rewrites(
        retrieval_queries=retrieval_queries,
        top_k=top_k * _ACL_OVERFETCH_MULTIPLIER,
        query_type=profile,
    )

    # 文档级权限过滤：非管理员只能访问授权文档
    if user_context is not None and not user_context.is_admin:
        retrieved_docs = filter_accessible_records(user_context, retrieved_docs)

    # 选档依据改为「查询类型枚举」，不再依赖模型返回的 complexity：
    # 后者与人工标注的一致率仅约 48% 且两次运行结果不同，会让同一查询有时付 5.8s、有时不付。
    # 默认档位用 l2 而非 l1：实测 l1 规则重排在三个场景均为负收益（整体 recall@3 0.7781 → 0.7711），
    # 而 l2（Cross-Encoder）是 0 token 的本地模型——没有理由为了 0.6ms 牺牲 6 个点的 recall@3。
    # l1 仅保留为 Cross-Encoder 不可用时的兜底档（由 reranker 内部回退）。
    l3_query_types = set(RETRIEVAL_CONFIG.get("l3_query_types") or [])
    rerank_strategy = "l3" if query_type in l3_query_types else "l2"

    reranked = await reranker.rerank(query, retrieved_docs, top_k=top_k * 2, strategy=rerank_strategy)

    context, used_docs = await reranker.assemble_context(reranked, max_tokens=5000)

    history_text = await conversation_memory.format_history_for_prompt(conversation_id, max_turns=5)
    # system prompt 由各模型客户端统一拼装（带真实日期），这里只组织参考资料与历史对话，
    # 避免同一段 system prompt 被拼两遍、且第二遍日期为空。
    full_context = f"{context}{history_text}"

    return {
        "intent": intent,
        "reasoning_mode": reasoning_mode,
        "query_type": query_type,
        "retrieval_queries": retrieval_queries,
        "retrieved_docs": retrieved_docs,
        "reranked": reranked,
        "context": context,
        "used_docs": used_docs,
        "full_context": full_context,
        "rerank_strategy": rerank_strategy,
    }


async def _handle_fallback_response(
    query: str,
    conversation_id: str,
    enable_fallback: bool,
    no_fallback_message: str,
    tip_suffix: str,
    reasoning_mode: str,
    log_message: str = "",
    fallback_error_answer: str = "",
) -> ChatResponse:
    if enable_fallback:
        if log_message:
            logger.info(log_message)
        try:
            answer, reasoning, usage = await model_fallback_query(query, FALLBACK_SYSTEM_PROMPT)
        except Exception as e:
            logger.error("Fallback API call failed: %s", e)
            answer = fallback_error_answer or "抱歉，服务暂时不可用，请稍后再试。"
            usage = {}
        response = ChatResponse(
            answer=answer + tip_suffix,
            segments=[ChatSegment(text=answer, citations=[])],
            citations=[],
            conversation_id=conversation_id,
            confidence_score=0.5,
            model_used=usage.get("model", ""),
            reasoning_mode_used=reasoning_mode,
            token_usage=usage,
            is_fallback=True,
        )
    else:
        response = ChatResponse(
            answer=no_fallback_message,
            segments=[],
            citations=[],
            conversation_id=conversation_id,
            confidence_score=0.0,
            model_used="none",
            reasoning_mode_used=reasoning_mode,
        )
    await conversation_memory.add_turn(conversation_id, query, response.answer)
    return response


async def _handle_fallback_stream(
    query: str,
    conversation_id: str,
    enable_fallback: bool,
    no_fallback_message: str,
    reasoning_mode: str,
    temperature: float,
    result_holder: dict = None,
) -> AsyncIterator[str]:
    if enable_fallback:
        full = ""
        llm_holder = result_holder if result_holder is not None else {}
        try:
            async for token in model_chat_stream(
                [{"role": "system", "content": FALLBACK_SYSTEM_PROMPT}, {"role": "user", "content": query}],
                reasoning_mode=reasoning_mode,
                temperature=temperature,
                result_holder=llm_holder,
            ):
                full += token
                yield token
        except Exception as e:
            logger.error("Fallback stream error: %s", e)
            yield "\n\n[错误: 服务暂时不可用，请稍后再试]"
            if result_holder is not None:
                result_holder["answer"] = full
                result_holder["citations"] = []
                result_holder["confidence"] = 0.0
                result_holder["model_used"] = llm_holder.get("provider", "")
                result_holder["reasoning_content"] = llm_holder.get("reasoning_content", "")
                result_holder["is_fallback"] = True
            return
        if result_holder is not None:
            result_holder["answer"] = full
            result_holder["citations"] = []
            result_holder["confidence"] = 0.5
            result_holder["model_used"] = llm_holder.get("provider", "")
            result_holder["reasoning_content"] = llm_holder.get("reasoning_content", "")
            result_holder["is_fallback"] = True
        await conversation_memory.add_turn(conversation_id, query, full)
    else:
        yield no_fallback_message
        if result_holder is not None:
            result_holder["answer"] = no_fallback_message
            result_holder["citations"] = []
            result_holder["confidence"] = 0.0
            result_holder["model_used"] = "none"
            result_holder["is_fallback"] = False


async def rag_query(
    query: str,
    conversation_id: str,
    reasoning_mode: Optional[str] = None,
    top_k: int = 5,
    temperature: float = 0.1,
    use_fallback: Optional[bool] = None,
    user_context: Optional[UserContext] = None,
) -> ChatResponse:
    enable_fallback = use_fallback if use_fallback is not None else SERVER_CONFIG.get("enable_fallback_chat", False)

    chunk_count = await vector_store.count()
    if chunk_count == 0:
        logger.info("Vector store is empty, no documents uploaded yet")
        return await _handle_fallback_response(
            query=query,
            conversation_id=conversation_id,
            enable_fallback=enable_fallback,
            no_fallback_message="知识库中暂无资料。请先上传文档，或联系我们获取帮助。",
            tip_suffix="\n\n💡 **提示**：此回答来自通用模型，如需基于企业内部资料，请上传文档。",
            reasoning_mode=reasoning_mode or "flash",
            log_message=f"Fallback enabled, using general chat for: {query}",
            fallback_error_answer=(
                f"我是 KnowledgeMind AI 助手。关于「{query}」的问题，"
                "目前知识库中还没有上传相关资料。\n\n"
                "请先上传文档（支持 PDF、Word、Excel、PPT、TXT、图片等格式），"
                "然后我可以基于企业内部资料为你解答。"
            ),
        )

    pipeline = await _execute_retrieval_pipeline(
        query=query,
        conversation_id=conversation_id,
        top_k=top_k,
        reasoning_mode=reasoning_mode,
        user_context=user_context,
    )
    reasoning_mode = pipeline["reasoning_mode"]
    retrieved_docs = pipeline["retrieved_docs"]
    reranked = pipeline["reranked"]
    context = pipeline["context"]
    used_docs = pipeline["used_docs"]
    full_context = pipeline["full_context"]

    if not retrieved_docs:
        return await _handle_fallback_response(
            query=query,
            conversation_id=conversation_id,
            enable_fallback=enable_fallback,
            no_fallback_message=(
                "暂未找到相关资料，建议尝试：换关键词或联系知识管理员补充。\n\n"
                "💡 **提示**：如需启用通用问答，请在配置中设置 ENABLE_FALLBACK_CHAT=true"
            ),
            tip_suffix="\n\n💡 **提示**：此回答来自通用模型，如需基于企业内部资料，请上传文档。",
            reasoning_mode=reasoning_mode,
            log_message=f"No docs found, using fallback chat for: {query}",
        )

    if not context:
        return await _handle_fallback_response(
            query=query,
            conversation_id=conversation_id,
            enable_fallback=enable_fallback,
            no_fallback_message="抱歉，经过重排序后未找到足够相关的资料。",
            tip_suffix="\n\n💡 **提示**：此回答来自通用模型，未能匹配到知识库内容。",
            reasoning_mode=reasoning_mode,
        )

    answer, reasoning, usage = await model_rag_query(
        query=query,
        context=full_context,
        reasoning_mode=reasoning_mode,
    )

    if enable_fallback:
        if any(keyword in answer for keyword in _CANNOT_ANSWER_KEYWORDS) or any(
            pattern in answer for pattern in _GENERIC_ANSWER_PATTERNS
        ):
            logger.info("LLM gave generic/template answer, triggering fallback for: %s", query)
            try:
                fallback_answer, fallback_reasoning, fallback_usage = await model_fallback_query(query, FALLBACK_SYSTEM_PROMPT)
                await conversation_memory.add_turn(conversation_id, query, fallback_answer)
                return ChatResponse(
                    answer=fallback_answer + "\n\n💡 **提示**：此回答来自通用模型，知识库中未找到相关资料。",
                    segments=[ChatSegment(text=fallback_answer, citations=[])],
                    citations=[],
                    conversation_id=conversation_id,
                    confidence_score=0.5,
                    model_used=fallback_usage.get("model", ""),
                    reasoning_mode_used=reasoning_mode,
                    token_usage=fallback_usage,
                    is_fallback=True,
                )
            except Exception as e:
                logger.error("Fallback after LLM refused failed: %s", e)

    await conversation_memory.add_turn(conversation_id, query, answer)

    citations = _extract_citations_from_answer(answer, used_docs)
    segments = _parse_answer_segments(answer, citations, used_docs)
    confidence = _compute_confidence(citations, reranked)

    return ChatResponse(
        answer=answer,
        segments=segments,
        citations=citations,
        reasoning_content=reasoning,
        confidence_score=confidence,
        conversation_id=conversation_id,
        token_usage=usage,
        model_used=usage.get("model", ""),
        reasoning_mode_used=reasoning_mode,
    )


async def rag_query_stream(
    query: str,
    conversation_id: str = "default",
    reasoning_mode: Optional[str] = None,
    top_k: int = 5,
    temperature: float = 0.1,
    use_fallback: Optional[bool] = None,
    result_holder: dict = None,
    user_context: Optional[UserContext] = None,
) -> AsyncIterator[str]:
    chunk_count = await vector_store.count()
    enable_fallback = use_fallback if use_fallback is not None else SERVER_CONFIG.get("enable_fallback_chat", False)

    if chunk_count == 0:
        async for token in _handle_fallback_stream(
            query=query,
            conversation_id=conversation_id,
            enable_fallback=enable_fallback,
            no_fallback_message="知识库中暂无资料。请先上传文档。",
            reasoning_mode=reasoning_mode or "flash",
            temperature=temperature,
            result_holder=result_holder,
        ):
            yield token
        return

    pipeline = await _execute_retrieval_pipeline(
        query=query,
        conversation_id=conversation_id,
        top_k=top_k,
        reasoning_mode=reasoning_mode,
        user_context=user_context,
    )
    reasoning_mode = pipeline["reasoning_mode"]
    retrieved_docs = pipeline["retrieved_docs"]
    reranked = pipeline["reranked"]
    context = pipeline["context"]
    used_docs = pipeline["used_docs"]
    full_context = pipeline["full_context"]

    if not retrieved_docs:
        async for token in _handle_fallback_stream(
            query=query,
            conversation_id=conversation_id,
            enable_fallback=enable_fallback,
            no_fallback_message="知识库中暂无资料。",
            reasoning_mode=reasoning_mode,
            temperature=temperature,
            result_holder=result_holder,
        ):
            yield token
        return

    if not reranked:
        async for token in _handle_fallback_stream(
            query=query,
            conversation_id=conversation_id,
            enable_fallback=enable_fallback,
            no_fallback_message="未找到相关的资料。",
            reasoning_mode=reasoning_mode,
            temperature=temperature,
            result_holder=result_holder,
        ):
            yield token
        return

    if not context:
        async for token in _handle_fallback_stream(
            query=query,
            conversation_id=conversation_id,
            enable_fallback=enable_fallback,
            no_fallback_message="未找到相关的资料。",
            reasoning_mode=reasoning_mode,
            temperature=temperature,
            result_holder=result_holder,
        ):
            yield token
        return

    full_text = ""
    llm_holder: dict = {}
    async for token in model_rag_query_stream(
        query=query,
        context=full_context,
        reasoning_mode=reasoning_mode,
        result_holder=llm_holder,
    ):
        full_text += token
        yield token

    citations = _extract_citations_from_answer(full_text, used_docs)
    confidence = _compute_confidence(citations, reranked)

    await conversation_memory.add_turn(conversation_id, query, full_text)

    if result_holder is not None:
        result_holder["answer"] = full_text
        result_holder["citations"] = citations
        result_holder["confidence"] = confidence
        result_holder["model_used"] = llm_holder.get("provider", "")
        result_holder["reasoning_content"] = llm_holder.get("reasoning_content", "")
        result_holder["is_fallback"] = False


def _citation_from_doc(doc: dict) -> Citation:
    metadata = doc.get("metadata", {})
    return Citation(
        document_id=doc.get("chunk_id", ""),
        document_title=_clean_citation_title(metadata.get("title", metadata.get("filename", ""))),
        page=metadata.get("page", metadata.get("page_number")),
        paragraph=str(metadata.get("chunk_index", "")),
        content=doc.get("content", "")[:250],
        score=float(doc.get("rerank_score", doc.get("rrf_score", 0))),
    )


def _extract_index_citations(answer: str, docs: List[dict], seen: set) -> List[Citation]:
    citations = []
    for match in _CITE_INDEX_PATTERN.finditer(answer):
        for part in re.split(r'[,，、]', match.group(1)):
            part = part.strip()
            if not part.isdigit():
                continue
            index = int(part)
            if 1 <= index <= len(docs):
                doc = docs[index - 1]
                chunk_id = doc.get("chunk_id", "")
                if chunk_id not in seen:
                    seen.add(chunk_id)
                    citations.append(_citation_from_doc(doc))
    return citations


def _extract_title_citations(answer: str, docs: List[dict], seen: set) -> List[Citation]:
    doc_index: dict[str, dict] = {}
    for doc in docs:
        metadata = doc.get("metadata", {})
        doc_title = _clean_citation_title(metadata.get("title", metadata.get("filename", "")))
        doc_index[doc_title] = doc
        for keyword in re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}', doc_title):
            if len(keyword) >= 2:
                doc_index[keyword] = doc

    citations = []
    for match in _CITATION_PATTERN.finditer(answer):
        title_hint = match.group(1).strip()
        page = match.group(2).strip()
        matched_doc = None
        for key, doc in doc_index.items():
            if title_hint in key or key in title_hint:
                matched_doc = doc
                break
        if matched_doc is None:
            matched_doc = _select_best_citation_doc(title_hint, docs)
        if not matched_doc:
            continue

        chunk_id = matched_doc.get("chunk_id", "")
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        citation = _citation_from_doc(matched_doc)
        if page and page.isdigit():
            citation.page = int(page)
        citations.append(citation)
    return citations


def _extract_citations_from_answer(answer: str, docs: List[dict]) -> List[Citation]:
    seen: set = set()
    citations = _extract_index_citations(answer, docs, seen)
    if not citations:
        citations = _extract_title_citations(answer, docs, seen)
    if not citations:
        citations = _fallback_citation_by_content_overlap(answer, docs)
    return citations


def _clean_citation_title(raw_title: str) -> str:
    if not raw_title or not raw_title.strip():
        return "知识库文档"
    cleaned = raw_title.strip()
    uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    if re.match(uuid_pattern, cleaned.lower()):
        return "知识库文档"
    if len(cleaned) > 50:
        cleaned = cleaned[:47] + "..."
    return cleaned


def _fallback_citation_by_content_overlap(answer: str, docs: List[dict]) -> List[Citation]:
    sentences = re.split(r'[。！？.!?\n]', answer)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]

    if not sentences:
        return _top_docs_as_citations(docs)

    doc_scores: dict[str, float] = {}
    doc_best_match: dict[str, str] = {}

    for doc in docs:
        chunk_id = doc.get("chunk_id", "")
        content = doc.get("content", "")
        if not content:
            continue

        total_overlap = 0
        for sentence in sentences:
            words = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', sentence)
            if not words:
                continue
            matched = sum(1 for w in words if w in content)
            if matched > 0:
                total_overlap += matched / len(words)
                if chunk_id not in doc_best_match or len(sentence) < len(doc_best_match.get(chunk_id, "")):
                    doc_best_match[chunk_id] = sentence

        if total_overlap > 0:
            doc_scores[chunk_id] = total_overlap

    ranked = sorted(doc_scores.items(), key=lambda x: -x[1])
    citations = []
    seen = set()

    for chunk_id, score in ranked[:5]:
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        doc = next((d for d in docs if d.get("chunk_id") == chunk_id), None)
        if doc:
            metadata = doc.get("metadata", {})
            citations.append(Citation(
                document_id=chunk_id,
                document_title=_clean_citation_title(metadata.get("title", metadata.get("filename", ""))),
                page=metadata.get("page", metadata.get("page_number")),
                paragraph=str(metadata.get("chunk_index", "")),
                content=doc_best_match.get(chunk_id, doc.get("content", ""))[:250],
                score=float(min(score / max(len(sentences), 1), 1.0)),
            ))

    if not citations:
        return _top_docs_as_citations(docs)
    return citations


def _top_docs_as_citations(docs: List[dict]) -> List[Citation]:
    citations = []
    seen = set()
    for doc in docs[:3]:
        metadata = doc.get("metadata", {})
        chunk_id = doc.get("chunk_id", "")
        if chunk_id not in seen:
            seen.add(chunk_id)
            citations.append(Citation(
                document_id=chunk_id,
                document_title=_clean_citation_title(metadata.get("title", metadata.get("filename", ""))),
                page=metadata.get("page", metadata.get("page_number")),
                paragraph=str(metadata.get("chunk_index", "")),
                content=doc.get("content", "")[:250],
                score=float(doc.get("rerank_score", doc.get("rrf_score", 0))),
            ))
    return citations


def _parse_answer_segments(answer: str, citations: List[Citation], docs: List[dict]) -> List[ChatSegment]:
    segments = []
    paragraphs = re.split(r'\n\s*\n', answer)
    for para in paragraphs:
        if not para.strip():
            continue
        para_citations = []
        for match in _CITE_INDEX_PATTERN.finditer(para):
            for part in re.split(r'[,，、]', match.group(1)):
                part = part.strip()
                if not part.isdigit():
                    continue
                index = int(part)
                if 1 <= index <= len(docs):
                    target_chunk = docs[index - 1].get("chunk_id", "")
                    for c in citations:
                        if c.document_id == target_chunk and c not in para_citations:
                            para_citations.append(c)
        for match in _CITATION_PATTERN.finditer(para):
            title_hint = match.group(1).strip()
            for c in citations:
                if title_hint in c.document_title or c.document_title in title_hint:
                    if c not in para_citations:
                        para_citations.append(c)
                    break
        segments.append(ChatSegment(text=para.strip(), citations=para_citations))

    return segments


def _select_best_citation_doc(title_hint: str, docs: List[dict]) -> Optional[dict]:
    if not docs:
        return None

    normalized_hint_tokens = set(re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}', title_hint.lower()))
    best_doc = None
    best_score = -1.0

    for doc in docs:
        metadata = doc.get("metadata", {})
        title_value = _clean_citation_title(metadata.get("title", metadata.get("filename", "")))
        title_tokens = set(re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}', title_value.lower()))
        title_overlap_score = len(normalized_hint_tokens & title_tokens)
        rerank_score_value = float(doc.get("rerank_score", doc.get("rrf_score", doc.get("score", 0.0))))
        combined_score = title_overlap_score * 10 + rerank_score_value
        if combined_score > best_score:
            best_score = combined_score
            best_doc = doc

    return best_doc


def _compute_confidence(citations: List[Citation], reranked: List[dict]) -> float:
    score = 0.0

    if citations:
        score += 0.3
        avg_relevance = sum(c.score for c in citations) / len(citations)
        if avg_relevance > 0.6:
            score += 0.2

    if reranked:
        rerank_scores = [float(r.get("rerank_score", r.get("rrf_score", 0.0))) for r in reranked[:5]]
        if rerank_scores:
            score += min(sum(rerank_scores) / len(rerank_scores), 0.3)

    return round(max(0.0, min(1.0, score)), 2)


async def _build_retrieval_queries(query: str, intent: dict) -> List[str]:
    retrieval_query_list = [query.strip()]
    if not RETRIEVAL_CONFIG.get("query_rewrite_enabled", True):
        return retrieval_query_list

    query_type = intent.get("query_type", "complex_analysis")
    # 实测：全局改写会让 recall@3 下降，仅在白名单场景（见 config.rewrite_query_types）开启
    rewrite_query_types = set(RETRIEVAL_CONFIG.get("rewrite_query_types") or [])
    if rewrite_query_types and query_type not in rewrite_query_types:
        return retrieval_query_list

    cached_rewrite = _rewrite_cache.get(query)
    if cached_rewrite is not None:
        return cached_rewrite

    try:
        rewritten_queries = await model_rewrite_query(query)
    except Exception as rewrite_error:
        logger.warning("Query rewrite failed: %s", rewrite_error)
        rewritten_queries = []

    max_rewrite_queries = max(int(RETRIEVAL_CONFIG.get("max_rewrite_queries", 3)), 1)
    for rewritten_query in rewritten_queries[:max_rewrite_queries]:
        normalized_query_text = rewritten_query.strip()
        if normalized_query_text and normalized_query_text not in retrieval_query_list:
            retrieval_query_list.append(normalized_query_text)

    _rewrite_cache.set(retrieval_query_list, query)
    return retrieval_query_list


async def _retrieve_documents_with_rewrites(
    retrieval_queries: List[str],
    top_k: int,
    query_type: str,
) -> List[dict]:
    merged_result_map: dict[str, dict] = {}

    # 多个改写查询之间无依赖，并行执行检索
    retrieval_coros = [
        hybrid_retriever.retrieve(query=q, top_k=top_k, query_type=query_type)
        for q in retrieval_queries
    ]
    retrieval_results_list = await asyncio.gather(*retrieval_coros)

    for query_index, retrieval_results in enumerate(retrieval_results_list):
        retrieval_query_text = retrieval_queries[query_index]
        rewrite_bonus_value = max(0.0, 0.03 - query_index * 0.01)

        for retrieval_result in retrieval_results:
            chunk_identifier = retrieval_result.get("chunk_id", "")
            if not chunk_identifier:
                continue

            merged_score_value = float(
                retrieval_result.get("rrf_score", retrieval_result.get("score", 0.0))
            ) + rewrite_bonus_value

            if chunk_identifier not in merged_result_map:
                merged_result_map[chunk_identifier] = {
                    **retrieval_result,
                    "rrf_score": merged_score_value,
                    "retrieval_queries": [retrieval_query_text],
                }
                continue

            existing_result = merged_result_map[chunk_identifier]
            existing_result["rrf_score"] = max(
                float(existing_result.get("rrf_score", 0.0)),
                merged_score_value,
            )
            existing_query_list = existing_result.setdefault("retrieval_queries", [])
            if retrieval_query_text not in existing_query_list:
                existing_query_list.append(retrieval_query_text)

    merged_results = sorted(
        merged_result_map.values(),
        key=lambda merged_item: float(merged_item.get("rrf_score", merged_item.get("score", 0.0))),
        reverse=True,
    )
    return merged_results[:top_k]
