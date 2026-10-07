"""시세 데이터 캐시 — 중복 API 요청 방지.

여러 봇이 같은 종목의 시세를 요청하면 캐시에서 반환.
TTL(기본 5초) 이내 동일 요청은 API를 호출하지 않음.
"""

import logging
import time
import threading

logger = logging.getLogger(__name__)


class MarketCache:
    """시세 데이터 TTL 캐시."""

    def __init__(self, ttl: float = 5.0):
        self._cache: dict[str, tuple[float, any]] = {}  # key → (timestamp, data)
        self._ttl = ttl
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> any:
        """캐시 조회. TTL 이내면 데이터 반환, 아니면 None."""
        with self._lock:
            if key in self._cache:
                ts, data = self._cache[key]
                if time.time() - ts < self._ttl:
                    self._hits += 1
                    return data
                del self._cache[key]
            self._misses += 1
            return None

    def set(self, key: str, data: any):
        """캐시 저장."""
        with self._lock:
            self._cache[key] = (time.time(), data)

    def get_or_fetch(self, key: str, fetch_fn) -> any:
        """캐시에 있으면 반환, 없으면 fetch_fn() 호출 후 캐시."""
        cached = self.get(key)
        if cached is not None:
            return cached
        data = fetch_fn()
        if data is not None:
            self.set(key, data)
        return data

    def clear(self):
        with self._lock:
            self._cache.clear()

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / max(1, self._hits + self._misses),
                "ttl": self._ttl,
            }


# 싱글톤
_cache: MarketCache | None = None


def get_cache(ttl: float = 5.0) -> MarketCache:
    global _cache
    if _cache is None:
        _cache = MarketCache(ttl=ttl)
    return _cache
