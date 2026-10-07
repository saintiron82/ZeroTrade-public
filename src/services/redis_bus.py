"""Redis Bus — 서비스 간 통신 (상태, 명령, 이벤트)."""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Callable

import redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# 키 접두사
PREFIX = "ff:"

# Pub/Sub 채널
CH_BOT_COMMAND = f"{PREFIX}bot:command"
CH_PIPE_COMMAND = f"{PREFIX}pipe:command"
CH_EVENTS = f"{PREFIX}events"


class RedisBus:
    """Redis 기반 서비스 간 통신."""

    def __init__(self, url: str = REDIS_URL):
        self._r = redis.from_url(url, decode_responses=True)
        self._pubsub = None
        self._listener_thread = None

    @property
    def client(self) -> redis.Redis:
        return self._r

    def ping(self) -> bool:
        try:
            return self._r.ping()
        except redis.ConnectionError:
            return False

    # ── 상태 저장/조회 ──

    def set_status(self, service_type: str, service_id: str, status: dict, ttl: int = 60):
        """서비스 상태 저장 (TTL 자동 만료)."""
        key = f"{PREFIX}{service_type}:{service_id}:status"
        self._r.set(key, json.dumps(status, ensure_ascii=False, default=str), ex=ttl)

    def get_status(self, service_type: str, service_id: str) -> dict | None:
        """서비스 상태 조회."""
        key = f"{PREFIX}{service_type}:{service_id}:status"
        data = self._r.get(key)
        return json.loads(data) if data else None

    def list_services(self, service_type: str) -> list[str]:
        """특정 타입의 활성 서비스 목록."""
        pattern = f"{PREFIX}{service_type}:*:status"
        keys = self._r.keys(pattern)
        return [k.split(":")[2] for k in keys]

    # ── 명령 발행 ──

    def send_command(self, service_type: str, service_id: str, action: str, config: dict = None):
        """서비스에 명령 발행."""
        channel = CH_BOT_COMMAND if service_type == "bot" else CH_PIPE_COMMAND
        msg = {
            "service_id": service_id,
            "action": action,
            "config": config or {},
            "timestamp": datetime.now().isoformat(),
        }
        self._r.publish(channel, json.dumps(msg, ensure_ascii=False))
        logger.info("명령 발행: %s %s → %s", service_type, service_id, action)

    # ── 이벤트 발행 ──

    def publish_event(self, event_type: str, data: dict):
        """이벤트 발행 (웹 SSE로 중계됨)."""
        msg = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now().isoformat(),
        }
        self._r.publish(CH_EVENTS, json.dumps(msg, ensure_ascii=False))

    # ── 명령 수신 (봇/파이프용) ──

    def subscribe_commands(self, service_type: str, callback: Callable[[dict], None]):
        """명령 채널 구독 (별도 스레드)."""
        channel = CH_BOT_COMMAND if service_type == "bot" else CH_PIPE_COMMAND
        self._pubsub = self._r.pubsub()
        self._pubsub.subscribe(**{channel: lambda msg: self._handle_msg(msg, callback)})
        self._listener_thread = self._pubsub.run_in_thread(sleep_time=0.1, daemon=True)
        logger.info("명령 채널 구독: %s", channel)

    def subscribe_events(self, callback: Callable[[dict], None]):
        """이벤트 채널 구독 (웹 SSE용)."""
        ps = self._r.pubsub()
        ps.subscribe(**{CH_EVENTS: lambda msg: self._handle_msg(msg, callback)})
        thread = ps.run_in_thread(sleep_time=0.1, daemon=True)
        return thread

    def _handle_msg(self, msg, callback):
        if msg["type"] != "message":
            return
        try:
            data = json.loads(msg["data"])
            callback(data)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("메시지 처리 오류: %s", e)

    # ── 프로세스 등록 ──

    def register_process(self, service_type: str, service_id: str, pid: int):
        """프로세스 등록 (orchestrator용)."""
        key = f"{PREFIX}process:{service_type}:{service_id}"
        self._r.hset(key, mapping={
            "pid": pid,
            "started_at": datetime.now().isoformat(),
            "service_type": service_type,
            "service_id": service_id,
        })
        self._r.expire(key, 300)  # 5분 TTL (heartbeat로 갱신)

    def heartbeat(self, service_type: str, service_id: str):
        """프로세스 heartbeat (TTL 갱신)."""
        key = f"{PREFIX}process:{service_type}:{service_id}"
        self._r.expire(key, 300)

    def list_processes(self) -> list[dict]:
        """실행 중인 프로세스 목록."""
        keys = self._r.keys(f"{PREFIX}process:*")
        processes = []
        for key in keys:
            data = self._r.hgetall(key)
            if data:
                processes.append(data)
        return processes

    def close(self):
        if self._pubsub:
            self._pubsub.close()
        self._r.close()


# 싱글톤
_bus: RedisBus | None = None


def get_bus() -> RedisBus:
    global _bus
    if _bus is None:
        _bus = RedisBus()
    return _bus
