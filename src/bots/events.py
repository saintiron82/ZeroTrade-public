"""봇 이벤트 발행 — SSE/Redis 자동 분기.

웹서버 내부: SSE broker로 발행
독립 프로세스: Redis로 발행
둘 다 없으면: 무시 (에러 없음)
"""

import logging

logger = logging.getLogger(__name__)


def publish(event_type: str, data: dict):
    """이벤트 발행. SSE → Redis → 무시 순서로 시도."""
    # 1. SSE broker (웹서버 내부)
    try:
        import asyncio
        from src.web.sse import broker
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(broker.publish(event_type, data))
            return
    except Exception:
        pass

    # 2. Redis (독립 프로세스)
    try:
        from src.services.redis_bus import get_bus
        bus = get_bus()
        if bus.ping():
            bus.publish_event(event_type, data)
            return
    except Exception:
        pass

    # 3. 둘 다 안 되면 무시
    logger.debug("이벤트 발행 불가 (SSE/Redis 없음): %s", event_type)
