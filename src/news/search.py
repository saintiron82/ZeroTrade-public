"""시맨틱 뉴스 검색 엔진 — SQLite + numpy 코사인 유사도."""

import logging
import numpy as np
from src.db import get_db
from src.news.embeddings import embed_text, blob_to_array

logger = logging.getLogger(__name__)


class NewsSearchEngine:
    """하이브리드 검색: 시맨틱 + 키워드."""

    def __init__(self):
        self._cache_dirty = True
        self._ids: list[int] = []
        self._matrix: np.ndarray | None = None  # (N, 384) 행렬

    def _load_vectors(self):
        """DB에서 전체 벡터를 numpy 행렬로 캐시."""
        pairs = get_db().get_all_embeddings()
        if not pairs:
            self._ids = []
            self._matrix = None
            self._cache_dirty = False
            return
        self._ids = [p[0] for p in pairs]
        self._matrix = np.array([blob_to_array(p[1]) for p in pairs], dtype=np.float32)
        self._cache_dirty = False
        logger.info("벡터 캐시 로드: %d건", len(self._ids))

    def invalidate(self):
        """새 뉴스 추가 시 캐시 무효화."""
        self._cache_dirty = True

    def semantic_search(self, query: str, top_k: int = 10) -> list[dict]:
        """시맨틱 검색 — 쿼리 임베딩 vs 전체 뉴스 코사인 유사도."""
        if self._cache_dirty or self._matrix is None:
            self._load_vectors()
        if self._matrix is None or len(self._ids) == 0:
            return []

        query_vec = blob_to_array(embed_text(query))  # (384,)
        scores = self._matrix @ query_vec              # dot product (정규화 벡터)
        top_indices = np.argsort(scores)[::-1][:top_k]

        result_ids = [self._ids[i] for i in top_indices]
        result_scores = [float(scores[i]) for i in top_indices]

        news_list = get_db().get_news_by_ids(result_ids)
        id_to_score = dict(zip(result_ids, result_scores))
        for n in news_list:
            n["relevance"] = round(id_to_score.get(n["id"], 0), 4)
        news_list.sort(key=lambda x: x["relevance"], reverse=True)
        return news_list

    def hybrid_search(self, query: str, top_k: int = 10) -> list[dict]:
        """하이브리드: 시맨틱 + 키워드 결합."""
        semantic = self.semantic_search(query, top_k=top_k)
        keyword = get_db().search_news(keyword=query, count=top_k)

        # ID 기준 병합, 시맨틱 점수 우선
        seen = {n["id"]: n for n in semantic}
        for n in keyword:
            if n["id"] not in seen:
                n["relevance"] = 0.3  # 키워드 매칭 기본 점수
                seen[n["id"]] = n

        merged = sorted(seen.values(), key=lambda x: x.get("relevance", 0), reverse=True)
        return merged[:top_k]


# 싱글톤
_engine: NewsSearchEngine | None = None


def get_search_engine() -> NewsSearchEngine:
    global _engine
    if _engine is None:
        _engine = NewsSearchEngine()
    return _engine
