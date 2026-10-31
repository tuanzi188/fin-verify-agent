import json
import os
import time
import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Dict
from collections import OrderedDict

from app.config import CACHE_CONFIG, DATA_DIR

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = str(DATA_DIR / "conversations")

SESSION_TTL_SECONDS = 365 * 24 * 3600
REDIS_KEY_PREFIX = "km:session:"
REDIS_INDEX_KEY = "km:session_index"


class BaseSessionStore(ABC):
    """会话持久化存储抽象基类。"""

    @abstractmethod
    async def load_all(self) -> Dict[str, "ConversationSession"]:
        """加载所有未过期的会话。"""

    @abstractmethod
    async def save(self, session: "ConversationSession") -> None:
        """持久化单个会话。"""

    @abstractmethod
    async def delete(self, conversation_id: str) -> None:
        """删除单个会话。"""

    @abstractmethod
    async def flush(self) -> None:
        """批量刷写脏数据（如有缓冲）。"""


class JsonSessionStore(BaseSessionStore):
    """基于 JSON 文件的会话持久化存储。"""

    def __init__(self, persist_dir: str, session_ttl: int = SESSION_TTL_SECONDS):
        self._persist_dir = persist_dir
        self._session_ttl = session_ttl

    def _session_path(self, conversation_id: str) -> str:
        return os.path.join(self._persist_dir, f"{conversation_id}.json")

    async def load_all(self) -> Dict[str, "ConversationSession"]:
        sessions: Dict[str, ConversationSession] = OrderedDict()
        if not os.path.isdir(self._persist_dir):
            return sessions
        now = time.time()
        try:
            for filename in os.listdir(self._persist_dir):
                if not filename.endswith(".json"):
                    continue
                filepath = os.path.join(self._persist_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    session = ConversationSession.from_dict(data)
                    if now - session.created_at > self._session_ttl:
                        os.remove(filepath)
                        continue
                    sessions[session.conversation_id] = session
                except json.JSONDecodeError as e:
                    logger.warning("Corrupted conversation file %s: %s", filename, e)
                    continue
                except KeyError as e:
                    logger.warning("Missing required field in %s: %s", filename, e)
                    continue
                except Exception as e:
                    logger.warning("Unexpected error loading %s: %s", filename, e)
                    continue
        except Exception as e:
            logger.warning("Failed to load conversations: %s", e)

        if sessions:
            logger.info("Loaded %d persisted conversation sessions", len(sessions))
        return sessions

    async def save(self, session: "ConversationSession") -> None:
        try:
            Path(self._persist_dir).mkdir(parents=True, exist_ok=True)
            filepath = self._session_path(session.conversation_id)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save conversation %s: %s", session.conversation_id, e)

    async def delete(self, conversation_id: str) -> None:
        try:
            filepath = self._session_path(conversation_id)
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            logger.warning("Failed to delete conversation file %s: %s", conversation_id, e)

    async def flush(self) -> None:
        pass  # JSON 存储每次 save 即写盘，无需批量刷写


class RedisSessionStore(BaseSessionStore):
    """基于 Redis 的会话持久化存储。"""

    def __init__(self, redis_url: str, session_ttl: int = SESSION_TTL_SECONDS):
        self._redis_url = redis_url
        self._session_ttl = session_ttl
        self._redis = None
        self._available = False
        self._pending: Dict[str, "ConversationSession"] = {}
        self._max_pending = 20

    async def _ensure_redis(self) -> bool:
        if self._available:
            return True
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2,
            )
            await self._redis.ping()
            self._available = True
            logger.info("Redis session store connected: %s", self._redis_url)
            return True
        except Exception as e:
            logger.warning("Redis session store unavailable: %s, will use JSON fallback", e)
            self._redis = None
            return False

    async def load_all(self) -> Optional[Dict[str, "ConversationSession"]]:
        """返回 None 表示 Redis 不可用/读取失败；返回 {} 表示 Redis 可用但没有会话。"""
        if not await self._ensure_redis():
            return None
        sessions: Dict[str, ConversationSession] = OrderedDict()
        try:
            session_ids = await self._redis.smembers(REDIS_INDEX_KEY)
            for cid in session_ids:
                key = f"{REDIS_KEY_PREFIX}{cid}"
                data_str = await self._redis.get(key)
                if not data_str:
                    await self._redis.srem(REDIS_INDEX_KEY, cid)
                    continue
                try:
                    data = json.loads(data_str)
                    session = ConversationSession.from_dict(data)
                    if time.time() - session.created_at > self._session_ttl:
                        await self._redis.delete(key)
                        await self._redis.srem(REDIS_INDEX_KEY, cid)
                        continue
                    sessions[session.conversation_id] = session
                except json.JSONDecodeError as e:
                    logger.warning("Corrupted session data for key %s: %s", cid, e)
                    continue
                except KeyError as e:
                    logger.warning("Missing required field in session %s: %s", cid, e)
                    continue
                except Exception as e:
                    logger.warning("Unexpected error loading session %s: %s", cid, e)
                    continue
        except Exception as e:
            logger.warning("Failed to load sessions from Redis: %s", e)
            self._available = False
            return None

        if sessions:
            logger.info("Loaded %d conversation sessions from Redis", len(sessions))
        return sessions

    async def save(self, session: "ConversationSession") -> None:
        self._pending[session.conversation_id] = session
        if len(self._pending) >= self._max_pending:
            await self.flush()

    async def delete(self, conversation_id: str) -> None:
        self._pending.pop(conversation_id, None)
        if not await self._ensure_redis():
            return
        try:
            key = f"{REDIS_KEY_PREFIX}{conversation_id}"
            await self._redis.delete(key)
            await self._redis.srem(REDIS_INDEX_KEY, conversation_id)
        except Exception as e:
            logger.warning("Failed to delete session from Redis: %s", e)

    async def flush(self) -> None:
        if not self._pending:
            return
        if not await self._ensure_redis():
            self._pending.clear()
            return
        try:
            pipe = self._redis.pipeline()
            for cid, session in self._pending.items():
                key = f"{REDIS_KEY_PREFIX}{cid}"
                data_str = json.dumps(session.to_dict(), ensure_ascii=False)
                pipe.setex(key, self._session_ttl, data_str)
                pipe.sadd(REDIS_INDEX_KEY, cid)
            await pipe.execute()
            logger.debug("Flushed %d sessions to Redis", len(self._pending))
        except Exception as e:
            logger.warning("Failed to flush sessions to Redis: %s", e)
            self._available = False
        finally:
            self._pending.clear()


def _create_session_store(persist_dir: str, session_ttl: int) -> BaseSessionStore:
    """尝试创建 Redis 存储，失败则回退到 JSON 存储。"""
    redis_url = CACHE_CONFIG.get("redis_url", "")
    if redis_url:
        redis_store = RedisSessionStore(redis_url, session_ttl)
        # 不在这里连接 — load_all 时延迟连接
        logger.info("Session store: Redis backend configured (url=%s)", redis_url)
        return redis_store
    logger.info("Session store: JSON file backend (dir=%s)", persist_dir)
    return JsonSessionStore(persist_dir, session_ttl)


class ConversationTurn:
    def __init__(
        self,
        query: str,
        answer: str,
        timestamp: float = None,
        payload: Optional[dict] = None,
    ):
        self.query = query
        self.answer = answer
        self.timestamp = timestamp or time.time()
        # 结构化结果原样保存；旧文件没有该键时为 None，保持向后兼容。
        self.payload = payload

    def to_dict(self) -> dict:
        data = {"query": self.query, "answer": self.answer, "timestamp": self.timestamp}
        if self.payload is not None:
            data["payload"] = self.payload
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationTurn":
        return cls(
            query=data["query"],
            answer=data["answer"],
            timestamp=data.get("timestamp", time.time()),
            payload=data.get("payload"),
        )


class ConversationSession:
    def __init__(self, conversation_id: str, max_turns: int = 30):
        self.conversation_id = conversation_id
        self.turns: List[ConversationTurn] = []
        self.max_turns = max_turns
        self.created_at = time.time()

    def add_turn(self, query: str, answer: str, payload: Optional[dict] = None):
        self.turns.append(ConversationTurn(query, answer, payload=payload))
        if len(self.turns) > self.max_turns:
            self.turns.pop(0)

    def get_history(self, max_turns: int = 5) -> List[Dict[str, str]]:
        recent_turns = self.turns[-max_turns:] if len(self.turns) >= max_turns else self.turns
        history = []
        for turn in recent_turns:
            history.append({"role": "user", "content": turn.query})
            history.append({"role": "assistant", "content": turn.answer})
        return history

    def is_expired(self, ttl_seconds: int = 1800) -> bool:
        return time.time() - self.created_at > ttl_seconds

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "turns": [t.to_dict() for t in self.turns],
            "max_turns": self.max_turns,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationSession":
        session = cls(
            conversation_id=data["conversation_id"],
            max_turns=data.get("max_turns", 30),
        )
        session.created_at = data.get("created_at", time.time())
        session.turns = [ConversationTurn.from_dict(t) for t in data.get("turns", [])]
        return session


class ConversationMemory:
    def __init__(
        self,
        max_sessions: int = 100,
        session_ttl: int = 1800,
        max_turns_per_session: int = 30,
        persist_dir: str = None,
        store: BaseSessionStore = None,
        flush_delay: float = 2.0,
    ):
        self._sessions: Dict[str, ConversationSession] = OrderedDict()
        self.max_sessions = max_sessions
        self.session_ttl = session_ttl
        self.max_turns_per_session = max_turns_per_session
        self._last_cleanup = time.time()
        self._persist_dir = persist_dir or DEFAULT_DATA_DIR
        self._store = store
        self._dirty_sessions: set[str] = set()
        self._lock = asyncio.Lock()
        self._flush_delay = flush_delay
        self._flush_task: Optional[asyncio.Task] = None

    async def _init_store(self):
        """延迟初始化存储后端并加载已有会话。"""
        async with self._lock:
            if self._store is not None and self._sessions:
                return
            if self._store is None:
                self._store = _create_session_store(self._persist_dir, self.session_ttl)
            sessions = await self._store.load_all()
            # None = Redis 不可用（真回退）；{} = Redis 可用但为空（保持 Redis，避免误切 JSON）
            if sessions is None:
                logger.info("Redis session store unavailable, falling back to JSON store")
                self._store = JsonSessionStore(self._persist_dir, self.session_ttl)
                sessions = await self._store.load_all() or {}
            self._sessions.update(sessions)
            if sessions:
                logger.info("Loaded %d persisted conversation sessions", len(sessions))

    async def flush(self):
        async with self._lock:
            if not self._dirty_sessions:
                return
            session_ids = list(self._dirty_sessions)
            successful_sessions = []
            for session_id in session_ids:
                session = self._sessions.get(session_id)
                if session:
                    try:
                        await self._store.save(session)
                        successful_sessions.append(session_id)
                    except Exception as e:
                        logger.warning("Failed to flush conversation %s: %s", session_id, e)
            self._dirty_sessions -= set(successful_sessions)
            if successful_sessions:
                logger.debug("Flushed %d dirty conversation sessions", len(successful_sessions))
            # Redis store 的 save() 只是进 _pending 缓冲，必须显式 flush 才不会在重启后丢数据
            try:
                await self._store.flush()
            except Exception as flush_error:
                logger.warning("Failed to flush session store buffer: %s", flush_error)

    async def _cleanup_expired(self):
        async with self._lock:
            now = time.time()
            if now - self._last_cleanup < 300:
                return
            self._last_cleanup = now
        # flush 会自行加锁
        await self.flush()
        async with self._lock:
            expired_keys = [k for k, s in self._sessions.items() if s.is_expired(self.session_ttl)]
            for k in expired_keys:
                await self._store.delete(k)
                del self._sessions[k]
            if expired_keys:
                logger.info("Cleaned up %d expired conversation sessions", len(expired_keys))

    async def get_or_create_session(self, conversation_id: str) -> ConversationSession:
        await self._cleanup_expired()
        # flush 会获取同一把锁，必须在进入临界区前调用。
        if conversation_id not in self._sessions and len(self._sessions) >= self.max_sessions:
            await self.flush()
        async with self._lock:
            if conversation_id not in self._sessions:
                if len(self._sessions) >= self.max_sessions:
                    oldest_key = next(iter(self._sessions))
                    self._dirty_sessions.discard(oldest_key)
                    await self._store.delete(oldest_key)
                    del self._sessions[oldest_key]
                self._sessions[conversation_id] = ConversationSession(
                    conversation_id, max_turns=self.max_turns_per_session
                )
            return self._sessions[conversation_id]

    async def add_turn(
        self,
        conversation_id: str,
        query: str,
        answer: str,
        payload: Optional[dict] = None,
    ):
        session = await self.get_or_create_session(conversation_id)
        async with self._lock:
            # 重新确认会话未被清理
            session = self._sessions.get(conversation_id, session)
            session.add_turn(query, answer, payload=payload)
            self._dirty_sessions.add(conversation_id)
        self._schedule_flush()

    def _schedule_flush(self):
        """延迟批量刷盘：短时间内多次 add_turn 只触发一次后台 flush。"""
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self):
        await asyncio.sleep(self._flush_delay)
        await self.flush()
        # 若延迟期间又有新脏数据，继续调度下一次
        if self._dirty_sessions:
            self._schedule_flush()

    async def get_history(self, conversation_id: str, max_turns: int = 5) -> List[Dict[str, str]]:
        async with self._lock:
            session = self._sessions.get(conversation_id)
            if not session:
                return []
            return session.get_history(max_turns)

    async def format_history_for_prompt(self, conversation_id: str, max_turns: int = 5) -> str:
        history = await self.get_history(conversation_id, max_turns)
        if not history:
            return ""
        parts = ["\n历史对话："]
        for msg in history:
            role_label = "用户" if msg["role"] == "user" else "助手"
            parts.append(f"{role_label}: {msg['content']}")
        return "\n".join(parts)

    async def clear(self, conversation_id: Optional[str] = None):
        await self.flush()
        async with self._lock:
            if conversation_id:
                self._sessions.pop(conversation_id, None)
                await self._store.delete(conversation_id)
            else:
                for cid in list(self._sessions.keys()):
                    await self._store.delete(cid)
                self._sessions.clear()

    async def list_conversations(self) -> List[Dict]:
        await self._cleanup_expired()
        async with self._lock:
            result = []
            for session in reversed(list(self._sessions.values())):
                preview = ""
                if session.turns:
                    preview = session.turns[0].query[:30]
                    if len(session.turns[0].query) > 30:
                        preview += "..."
                result.append({
                    "conversation_id": session.conversation_id,
                    "created_at": session.created_at,
                    "last_updated": session.turns[-1].timestamp if session.turns else session.created_at,
                    "turn_count": len(session.turns),
                    "preview": preview,
                })
            return result

    async def get_conversation(self, conversation_id: str) -> Optional[Dict]:
        await self._cleanup_expired()
        async with self._lock:
            session = self._sessions.get(conversation_id)
            if not session:
                return None
            return {
                "conversation_id": session.conversation_id,
                "created_at": session.created_at,
                "turns": [t.to_dict() for t in session.turns],
            }


conversation_memory = ConversationMemory(session_ttl=365 * 24 * 3600)
