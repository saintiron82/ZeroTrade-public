"""KIS API 레이트 리미터 — 초당 요청 제한 관리."""

import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

# KIS API 제한: 초당 20건
MAX_REQUESTS_PER_SECOND = 18  # 여유 2건
MIN_INTERVAL = 1.0 / MAX_REQUESTS_PER_SECOND  # ~0.055초


class RateLimiter:
    """Thread-safe 레이트 리미터. 모든 KIS API 호출 전 wait() 호출."""

    def __init__(self, max_per_second: int = MAX_REQUESTS_PER_SECOND):
        self._max = max_per_second
        self._min_interval = 1.0 / max_per_second
        self._timestamps: deque = deque()
        self._lock = threading.Lock()
        self._total_calls = 0
        self._throttled_count = 0

    def wait(self):
        """다음 요청 가능할 때까지 대기."""
        with self._lock:
            now = time.time()

            # 1초 이전 타임스탬프 제거
            while self._timestamps and self._timestamps[0] < now - 1.0:
                self._timestamps.popleft()

            # 초당 한도 초과 시 대기
            if len(self._timestamps) >= self._max:
                wait_until = self._timestamps[0] + 1.0
                sleep_time = wait_until - now
                if sleep_time > 0:
                    self._throttled_count += 1
                    logger.debug("Rate limit: %.3f초 대기 (총 %d회 throttle)", sleep_time, self._throttled_count)
                    time.sleep(sleep_time)

            self._timestamps.append(time.time())
            self._total_calls += 1

    @property
    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            recent = sum(1 for t in self._timestamps if t > now - 1.0)
            return {
                "total_calls": self._total_calls,
                "throttled": self._throttled_count,
                "current_rps": recent,
                "max_rps": self._max,
            }


# 싱글톤
_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
