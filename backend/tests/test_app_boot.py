"""应用启动冒烟测试（Harness 韧性工程）。

背景：本项目曾出现过「已提交代码在启动阶段直接崩溃」的问题——`ConversationMemory`
读取了不存在的 `_session_ttl` 属性，导致 `uvicorn app.main:app` 一启动就退出。
单元测试全绿也拦不住这种问题，因为没有任何测试真的把应用起起来。

本测试真正拉起 uvicorn 子进程并轮询健康检查，确保：
1. 应用能启动完成（进程存活、/api/v1/health 返回 ok）；
2. 核心子系统（向量库 / BM25 / 图谱 / 会话存储）都初始化成功。

为了在 CI 与冷启动机器上都快速且确定：
- `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`：禁止启动阶段联网下载模型，
  让 embedding 与 L2 重排走既有的降级分支（哈希向量 / L1 规则重排）；
- `DATA_DIR` 指向临时目录，不在仓库里留下索引/日志；
- 不依赖 LLM API Key——深度检查里 LLM 那一项允许不健康（CI 无 Key）。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
BOOT_TIMEOUT_SECONDS = float(os.getenv("BOOT_SMOKE_TIMEOUT", "120"))
POLL_INTERVAL_SECONDS = 0.5
# 与 LLM API Key 无关、必须全部健康的核心子系统
REQUIRED_HEALTHY_CHECKS = (
    "vector_store",
    "bm25_index",
    "graph_index",
    "conversation_memory",
)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _wait_for_health(base_url: str, process: subprocess.Popen) -> dict:
    """轮询健康检查直到就绪；失败时抛出带子进程输出的异常。"""
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    last_error: str = "no attempt made"

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                "启动过程中进程已退出（exit code %s）。\n--- 子进程输出 ---\n%s"
                % (process.returncode, _read_process_output(process))
            )
        try:
            status, payload = _http_get_json(base_url + "/api/v1/health")
            if status == 200:
                return payload
            last_error = "health returned HTTP %s" % status
        except (urllib.error.URLError, ConnectionError, OSError, ValueError) as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
        except json.JSONDecodeError as exc:  # pragma: no cover - 防御性
            last_error = "响应不是合法 JSON: %s" % exc
        time.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(
        "应用在 %.0fs 内未通过健康检查（最后错误：%s）。\n--- 子进程输出 ---\n%s"
        % (BOOT_TIMEOUT_SECONDS, last_error, _read_process_output(process))
    )


def _read_process_output(process: subprocess.Popen) -> str:
    """读取重定向到临时文件的子进程输出（用于失败诊断）。"""
    path = getattr(process, "_smoke_log_path", None)
    if not path or not os.path.exists(path):
        return "(无输出)"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read().strip()
    except OSError:
        return "(输出读取失败)"
    return content[-4000:] if content else "(空)"


@pytest.mark.slow
def test_application_boots_and_core_subsystems_are_healthy():
    """应用能真正启动，且核心子系统全部就绪。"""
    port = _pick_free_port()
    base_url = "http://127.0.0.1:%d" % port

    env = dict(os.environ)
    env.update({
        "DATA_DIR": tempfile.mkdtemp(prefix="km_boot_smoke_"),
        # 禁止启动阶段联网下载模型：既保证 CI 快，也避免 to_thread 下载不可取消导致卡死
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONUNBUFFERED": "1",
        # 冒烟测试固定匿名开发模式，避免开发者本机 .env 的鉴权配置影响断言
        "APP_ENV": "development",
        "API_KEY": "",
        "AUTH_USERS_JSON": "",
    })

    log_fd, log_path = tempfile.mkstemp(prefix="km_boot_smoke_", suffix=".log")
    process = None
    try:
        with os.fdopen(log_fd, "w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(BACKEND_DIR),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        process._smoke_log_path = log_path  # 供失败诊断读取

        payload = _wait_for_health(base_url, process)
        assert payload.get("status") == "ok", "健康检查返回异常内容: %s" % payload

        # 深度检查：核心子系统必须健康（LLM API Key 允许缺失，故不纳入断言）
        status, deep = _http_get_json(base_url + "/api/v1/health/deep")
        assert status == 200, "深度健康检查 HTTP %s" % status

        checks = deep.get("checks", {})
        unhealthy = {
            name: checks.get(name, {}).get("error", checks.get(name, {}).get("status"))
            for name in REQUIRED_HEALTHY_CHECKS
            if checks.get(name, {}).get("status") != "healthy"
        }
        assert not unhealthy, "核心子系统未就绪: %s" % unhealthy
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
        # 清理临时数据目录与日志
        data_dir = env.get("DATA_DIR")
        if data_dir:
            import shutil
            shutil.rmtree(data_dir, ignore_errors=True)
        if os.path.exists(log_path):
            os.remove(log_path)
