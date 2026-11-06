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
from app.services.conversation_memory import conversation_memory
from app.services.audit_logger import audit_logger

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
    # 通用知识库 RAG 已下线，启动只初始化本产品实际依赖的存储。
    await conversation_memory._init_store()
    logger.info("Conversation memory initialized")
    await audit_logger.start()
    logger.info("Audit logger started")

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

    # 1. 熔断器状态
    from app.services.resilience import circuit_breaker_registry
    checks["circuit_breakers"] = circuit_breaker_registry.get_all_states()
    for name, state in checks["circuit_breakers"].items():
        if state.get("state") == "open":
            overall_healthy = False

    # 2. 模型 API 配置检查（不调用 models.list，避免依赖远程接口）
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

    # 3. 会话存储状态
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
