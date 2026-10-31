import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.core.logging_context import (
    TraceIdFilter,
    TraceIdFormatter,
    trace_id_var,
    generate_trace_id,
)

from app.config import SERVER_CONFIG, MODEL_PROVIDER, API_KEY
from app.api.chat import router as chat_router
from app.api.analytics import router as analytics_router
from app.api.finance import router as finance_router
from app.api.lab import router as lab_router
from app.core.auth import verify_api_key, API_IDENTITIES
from app.services.vector_store import vector_store
from app.services.reranker import reranker
from app.services.keyword_search import get_bm25_searcher
from app.services.graph_search import graph_searcher
from app.services.conversation_memory import conversation_memory
from app.services.audit_logger import audit_logger
from app.models.chat import DocumentChunk

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | trace_id=%(trace_id)s | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    style="%",
)
# 为已创建的 handler 应用兼容性格式化与 trace_id 注入
_root_logger = logging.getLogger()
_trace_filter = TraceIdFilter()
_root_logger.addFilter(_trace_filter)
for _handler in _root_logger.handlers:
    _handler.setFormatter(TraceIdFormatter(_LOG_FORMAT))
    _handler.addFilter(_trace_filter)
logger = logging.getLogger(__name__)


async def _restore_vector_store_from_bm25_index(bm25_searcher_instance):
    if await vector_store.count() > 0:
        return

    persisted_documents = list(getattr(bm25_searcher_instance, "_documents", []))
    if not persisted_documents:
        return

    restored_chunks = []
    for persisted_document in persisted_documents:
        restored_chunk_id = str(persisted_document.get("chunk_id", "")).strip()
        restored_content = str(persisted_document.get("content", "")).strip()
        restored_metadata = persisted_document.get("metadata", {}) or {}
        restored_doc_id = str(
            persisted_document.get("doc_id")
            or restored_metadata.get("document_id")
            or "restored_document"
        ).strip()

        if not restored_chunk_id or not restored_content:
            continue

        restored_chunks.append(
            DocumentChunk(
                chunk_id=restored_chunk_id,
                doc_id=restored_doc_id,
                content=restored_content,
                metadata=restored_metadata,
            )
        )

    if not restored_chunks:
        return

    try:
        await vector_store.add_chunks(restored_chunks)
    except Exception as restore_error:
        logger.error("Failed to restore vector store from BM25 documents: %s", restore_error)
        return
    logger.info(
        "Vector store restored from BM25 persisted documents: chunks=%s",
        len(restored_chunks),
    )


async def _rebuild_index_bm25(bm25_searcher_instance) -> bool:
    """从向量库流式读取 chunks 并重建 BM25 索引。返回是否有数据。"""
    bm25_docs: list[dict] = []
    async for chunk in vector_store.list_chunks_stream():
        bm25_docs.append({
            "chunk_id": chunk.get("chunk_id", ""),
            "content": chunk.get("content", ""),
            "metadata": chunk.get("metadata", {}),
        })

    if not bm25_docs:
        return False

    bm25_searcher_instance.reset_index()
    bm25_searcher_instance.build_index(bm25_docs)
    bm25_searcher_instance.save_to_disk()
    logger.info("BM25 index rebuilt from persisted chunks: %s documents", len(bm25_docs))
    return True


async def _rebuild_index_graph() -> bool:
    """从向量库流式读取 chunks 并重建图谱索引。返回是否有数据。"""
    has_data = False
    graph_searcher.reset_memory_index()
    async for chunk in vector_store.list_chunks_stream():
        metadata = chunk.get("metadata", {})
        doc_identifier = str(metadata.get("document_id", "")).strip()
        chunk_identifier = chunk.get("chunk_id", "")
        content_text = chunk.get("content", "")
        if not doc_identifier or not chunk_identifier or not content_text:
            continue
        has_data = True
        extracted_entities = graph_searcher.extract_entities_from_text(content_text)
        for extracted_entity in extracted_entities:
            entity_name = extracted_entity.get("name", "").strip()
            if entity_name:
                graph_searcher.add_entity(
                    entity_name, [chunk_identifier], document_id=doc_identifier
                )
    if has_data:
        graph_searcher.save_to_disk()
        logger.info("Graph index rebuilt from persisted chunks")
    return has_data


async def _rebuild_retrieval_indexes():
    bm25_searcher_instance = get_bm25_searcher()
    bm25_loaded_from_disk = False
    graph_loaded_from_disk = False

    # ---- 各索引独立从磁盘加载 ----
    try:
        bm25_loaded_from_disk = bm25_searcher_instance.load_from_disk()
        if bm25_loaded_from_disk:
            logger.info("BM25 index loaded from disk")
    except Exception as load_bm25_error:
        logger.warning("Failed to load BM25 index from disk: %s", load_bm25_error)
        bm25_searcher_instance.reset_index()

    try:
        graph_loaded_from_disk = graph_searcher.load_from_disk()
        if graph_loaded_from_disk:
            logger.info("Graph index loaded from disk")
    except Exception as load_graph_error:
        logger.warning("Failed to load graph index from disk: %s", load_graph_error)
        graph_searcher.reset_memory_index()

    # 从 BM25 恢复向量库（如向量库为空但 BM25 有数据）
    if bm25_loaded_from_disk:
        await _restore_vector_store_from_bm25_index(bm25_searcher_instance)

    # 各索引独立判断是否需要重建
    need_bm25_rebuild = not bm25_loaded_from_disk
    need_graph_rebuild = not graph_loaded_from_disk

    if not need_bm25_rebuild and not need_graph_rebuild:
        logger.info("All retrieval indexes loaded from disk, no rebuild needed")
        return

    if not SERVER_CONFIG.get("bm25_rebuild_on_startup", True):
        logger.info("Startup retrieval index rebuild disabled")
        return

    # ---- 按需重建，避免不必要的 list_chunks 调用 ----
    if need_bm25_rebuild and need_graph_rebuild:
        # 两者都需要重建：双 pass 流式读取，避免全量内存加载
        has_bm25_data = await _rebuild_index_bm25(bm25_searcher_instance)
        if has_bm25_data:
            await _rebuild_index_graph()
    elif need_bm25_rebuild:
        has_data = await _rebuild_index_bm25(bm25_searcher_instance)
        if not has_data:
            logger.info("No persisted chunks found, skip BM25 rebuild")
    elif need_graph_rebuild:
        has_data = await _rebuild_index_graph()
        if not has_data:
            logger.info("No persisted chunks found, skip graph rebuild")

    logger.info(
        "Retrieval indexes rebuild complete: bm25=%s, graph=%s",
        "loaded" if bm25_loaded_from_disk else "rebuilt",
        "loaded" if graph_loaded_from_disk else "rebuilt",
    )


APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
_PRODUCTION_ENVS = {"production", "prod", "staging"}


def _assert_production_security() -> None:
    """生产/预发环境不允许以匿名模式启动。"""
    if APP_ENV in _PRODUCTION_ENVS and not API_KEY and not API_IDENTITIES:
        raise RuntimeError(
            f"APP_ENV={APP_ENV} 时必须配置 API_KEY 或 AUTH_USERS_JSON；"
            "匿名模式仅允许在 development/test 环境使用。"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_production_security()
    logger.info("Starting 金融查证 Agent API... (env=%s)", APP_ENV)
    await vector_store.initialize()
    logger.info("Vector store initialized")
    await conversation_memory._init_store()
    logger.info("Conversation memory initialized")
    await audit_logger.start()
    logger.info("Audit logger started")
    await _rebuild_retrieval_indexes()
    logger.info("Retrieval indexes initialized")

    try:
        await reranker.warmup()
        logger.info("Reranker warmed up")
    except Exception as e:
        logger.warning("Reranker warmup skipped: %s", e)

    yield
    await conversation_memory.flush()
    await audit_logger.stop()
    logger.info("Shutting down 金融查证 Agent API...")


app = FastAPI(
    title="金融查证 Agent API",
    description="金融文档查证 Agent：结构检索 / 多步 Agent / 证据回查 / 会话历史",
    version="1.1.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """为每个请求生成或复用 trace_id，并注入响应头与日志上下文。"""
    trace_id = request.headers.get("X-Request-ID") or generate_trace_id()
    request.state.trace_id = trace_id
    token = trace_id_var.set(trace_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = trace_id
        return response
    finally:
        trace_id_var.reset(token)

app.add_middleware(
    CORSMiddleware,
    allow_origins=SERVER_CONFIG["cors_origins"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router, prefix="/api/v1", tags=["Chat"], dependencies=[Depends(verify_api_key)])
app.include_router(analytics_router, prefix="/api/v1", tags=["Analytics"], dependencies=[Depends(verify_api_key)])
app.include_router(finance_router, prefix="/api/v1", tags=["Finance"], dependencies=[Depends(verify_api_key)])
app.include_router(lab_router, prefix="/api/v1", tags=["Lab"], dependencies=[Depends(verify_api_key)])

FRONTEND_PATH = Path(__file__).parent.parent.parent / "frontend" / "dist"
FRONTEND_ASSETS = FRONTEND_PATH / "assets"

if FRONTEND_PATH.exists():
    logger.info("Serving frontend from %s", FRONTEND_PATH)
    # 只有 dist 没有 assets 时（构建中断/半产物）不要 mount 空目录，
    # 否则整个应用在 import 阶段就崩掉，健康检查也起不来。
    if FRONTEND_ASSETS.exists():
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_ASSETS)), name="frontend_assets")
    else:
        logger.warning("Frontend assets directory missing: %s", FRONTEND_ASSETS)

    @app.get("/")
    @app.get("/lab")
    @app.get("/documents")
    @app.get("/analytics")
    @app.get("/audit")
    async def serve_index():
        return FileResponse(str(FRONTEND_PATH / "index.html"))
else:
    @app.get("/")
    async def root():
        return {
            "service": "金融查证 Agent",
            "version": "1.1.0",
            "status": "running",
            "docs": "/docs",
        }


@app.get("/api/v1/health")
async def health_check():
    """基础健康检查"""
    return {"status": "ok", "service": "金融查证 Agent API"}


@app.get("/api/v1/health/deep", dependencies=[Depends(verify_api_key)])
async def deep_health_check():
    """
    深度健康检查（Harness 韧性工程）
    检查所有依赖组件的可用性，包括熔断器状态
    """
    checks = {}
    overall_healthy = True

    # 1. 向量库状态
    try:
        chunk_count = await vector_store.count()
        checks["vector_store"] = {
            "status": "healthy",
            "chunk_count": chunk_count,
        }
    except Exception as e:
        checks["vector_store"] = {"status": "unhealthy", "error": str(e)[:200]}
        overall_healthy = False

    # 2. BM25 索引状态
    try:
        bm25 = get_bm25_searcher()
        doc_count = len(getattr(bm25, "_documents", []))
        checks["bm25_index"] = {
            "status": "healthy",
            "document_count": doc_count,
        }
    except Exception as e:
        checks["bm25_index"] = {"status": "unhealthy", "error": str(e)[:200]}
        overall_healthy = False

    # 3. 图谱索引状态
    try:
        graph_entity_count = len(getattr(graph_searcher, "entity_to_chunks", {}))
        checks["graph_index"] = {
            "status": "healthy",
            "entity_count": graph_entity_count,
        }
    except Exception as e:
        checks["graph_index"] = {"status": "unhealthy", "error": str(e)[:200]}
        overall_healthy = False

    # 4. 熔断器状态
    from app.services.resilience import circuit_breaker_registry
    checks["circuit_breakers"] = circuit_breaker_registry.get_all_states()
    for name, state in checks["circuit_breakers"].items():
        if state.get("state") == "open":
            overall_healthy = False

    # 5. 模型 API 配置检查（不调用 models.list，避免依赖远程接口）
    provider_label = "qwen" if MODEL_PROVIDER == "qwen" else "deepseek"
    try:
        if MODEL_PROVIDER == "qwen":
            from app.services.qwen_client import qwen_client as api_client
        else:
            from app.services.deepseek import deepseek_client as api_client
        if api_client.clients:
            checks[f"{provider_label}_api"] = {
                "status": "healthy",
                "provider": "primary",
                "client_count": len(api_client.clients),
            }
        else:
            checks[f"{provider_label}_api"] = {"status": "unhealthy", "error": "No API client configured"}
            overall_healthy = False
    except Exception as e:
        checks[f"{provider_label}_api"] = {"status": "unhealthy", "error": str(e)[:200]}
        overall_healthy = False

    # 6. 重排档位（托管 qwen3.7-text-rerank / 本地 Cross-Encoder）
    from app.services import rerank_api
    checks["rerank"] = rerank_api.describe()

    # 7. 会话存储状态
    try:
        conversations = await conversation_memory.list_conversations()
        checks["conversation_memory"] = {
            "status": "healthy",
            "conversation_count": len(conversations),
        }
    except Exception as e:
        checks["conversation_memory"] = {"status": "unhealthy", "error": str(e)[:200]}

    return {
        "status": "healthy" if overall_healthy else "degraded",
        "service": "金融查证 Agent API",
        "version": "1.1.0",
        "timestamp": datetime.now().isoformat(),
        "checks": checks,
    }
