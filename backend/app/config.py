import os
import logging
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from app.core.secrets import secrets_manager

load_dotenv()

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 模型提供商选择: deepseek / qwen
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "deepseek").strip().lower()

API_KEY = secrets_manager.get("API_KEY", "")

MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

DEEPSEEK_CONFIG: Dict[str, Dict[str, Any]] = {
    "primary": {
        "base_url": os.getenv("DS_BASE_URL", "https://api.deepseek.com/v1"),
        "api_key": secrets_manager.get("DS_API_KEY"),
    },
    "fallback_1": {
        "base_url": os.getenv("ALI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        "api_key": secrets_manager.get("ALI_API_KEY"),
    },
}

QWEN_CONFIG: Dict[str, Dict[str, Any]] = {
    "primary": {
        "base_url": os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        "api_key": secrets_manager.get("QWEN_API_KEY"),
    },
    "fallback_1": {
        "base_url": os.getenv("DS_BASE_URL", "https://api.deepseek.com/v1"),
        "api_key": secrets_manager.get("DS_API_KEY"),
    },
}

MODEL_CONFIG: Dict[str, Any] = {
    "flash": {
        "model": os.getenv("FLASH_MODEL", "deepseek-chat"),
        "max_tokens": 2048,
        "temperature": 0.1,
    },
    "pro": {
        "model": os.getenv("PRO_MODEL", "deepseek-reasoner"),
        "max_tokens": 4096,
        "temperature": 0.1,
    },
}

QWEN_MODEL_CONFIG: Dict[str, Any] = {
    "flash": {
        "model": os.getenv("QWEN_FLASH_MODEL", "qwen-turbo"),
        "max_tokens": 2048,
        "temperature": 0.1,
    },
    "pro": {
        "model": os.getenv("QWEN_PRO_MODEL", "qwen-plus"),
        "max_tokens": 4096,
        "temperature": 0.1,
    },
}

EMBEDDING_CONFIG: Dict[str, Any] = {
    "model_name": os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-small-zh-v1.5"),
    "device": os.getenv("EMBEDDING_DEVICE", "auto"),
    "batch_size": int(os.getenv("EMBEDDING_BATCH_SIZE", 32)),
    "normalize": os.getenv("EMBEDDING_NORMALIZE", "true").lower() == "true",
    "cache_dir": os.getenv("EMBEDDING_CACHE_DIR", ""),
    "fallback_dimension": int(os.getenv("EMBEDDING_FALLBACK_DIMENSION", 384)),
}

SERVER_CONFIG: Dict[str, Any] = {
    "cors_origins": os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(","),
}

CACHE_CONFIG: Dict[str, Any] = {
    "redis_url": os.getenv("REDIS_URL", "redis://localhost:6379/0"),
}

_required_secrets = []
if MODEL_PROVIDER == "qwen":
    _required_secrets.append("QWEN_API_KEY")
else:
    _required_secrets.append("DS_API_KEY")
missing = secrets_manager.validate_required_secrets(_required_secrets)
if missing:
    logger.warning(
        "Missing required API keys: %s. The application may not function correctly.",
        ", ".join(missing),
    )
