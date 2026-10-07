"""증권 뉴스 RSS 수집기 — 주기적으로 뉴스를 수집하여 DB에 저장."""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser

from src.db import get_db, normalize_published

logger = logging.getLogger(__name__)

# 구글 뉴스 RSS (한국어, 증권/경제 관련 검색)
RSS_FEEDS = {
    "증권_전체": "https://news.google.com/rss/search?q=증권+주식+ETF&hl=ko&gl=KR&ceid=KR:ko",
    "코스피_코스닥": "https://news.google.com/rss/search?q=코스피+코스닥+장마감&hl=ko&gl=KR&ceid=KR:ko",
    "반도체_IT": "https://news.google.com/rss/search?q=반도체+삼성전자+SK하이닉스&hl=ko&gl=KR&ceid=KR:ko",
    "금리_환율": "https://news.google.com/rss/search?q=금리+환율+한국은행&hl=ko&gl=KR&ceid=KR:ko",
    "미국_증시": "https://news.google.com/rss/search?q=미국+증시+나스닥+S%26P500&hl=ko&gl=KR&ceid=KR:ko",
}


class NewsCollector:
    """RSS 뉴스 자동 수집기."""

    def __init__(self, interval_seconds: int = 600):
        self._interval = interval_seconds
        self._running = False
        self._task = None
        self._collected = 0
        self._last_index_refresh: datetime | None = None
        self._index_refresh_min_interval = 3600  # 산업지수 갱신 최소 간격 (1시간)

    async def start(self):
        if self._running:
            return
        self._running = True
        # 기존 뉴스의 published_iso 정규화 (1회)
        get_db().backfill_published_iso()
        self._task = asyncio.create_task(self._loop())
        logger.info("뉴스 수집기 시작 (%d초 간격, %d개 피드)", self._interval, len(RSS_FEEDS))

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("뉴스 수집기 정지 (총 %d건 수집)", self._collected)

    async def _loop(self):
        while self._running:
            try:
                count = self.collect()
                if count > 0:
                    logger.info("뉴스 %d건 수집", count)
            except Exception as e:
                logger.error("뉴스 수집 오류: %s", e)
            await asyncio.sleep(self._interval)

    def collect(self) -> int:
        """모든 RSS 피드에서 뉴스 수집 → 분류/태깅/임베딩 → DB 저장."""
        import json as _json
        from src.news.classifier import classify, extract_tags, analyze_sentiment
        from src.news.ai_classifier import load_news_config, get_default_classifier

        news_cfg = load_news_config()
        ai_enabled = bool(news_cfg.get("ai_enabled"))
        ai_classifier = get_default_classifier() if ai_enabled else None

        db = get_db()
        total = 0
        new_articles = []

        now = datetime.now(timezone.utc)
        max_age = timedelta(hours=2)

        for source, url in RSS_FEEDS.items():
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:20]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "")
                    summary = entry.get("summary", entry.get("description", "")).strip()
                    published = entry.get("published", entry.get("updated", ""))

                    if not title:
                        continue

                    # 발행 시간 파싱 → 2시간 이내 뉴스만 수집
                    if published:
                        try:
                            pub_dt = parsedate_to_datetime(published)
                            if pub_dt.tzinfo is None:
                                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                            if now - pub_dt > max_age:
                                continue
                        except Exception:
                            pass  # 파싱 실패 시 수집 허용

                    news_hash = hashlib.md5(f"{title}{link}".encode()).hexdigest()
                    if db.news_exists(news_hash):
                        continue

                    # 분류 + 태깅 + 감성 분석 (AI 또는 정규식)
                    category = classify(title, summary, source)
                    tags = _json.dumps(extract_tags(title, summary), ensure_ascii=False)
                    if ai_classifier is not None:
                        sent_result = ai_classifier.classify(title, summary)
                    else:
                        sent_result = analyze_sentiment(title, summary)
                    sentiment_payload = {
                        "sentiment": sent_result["sentiment"],
                        "score": sent_result["score"],
                        "impact": sent_result["impact"],
                        "reasons": sent_result["reasons"],
                        "label": sent_result["label"],
                        "source": sent_result.get("source", "regex"),
                    }
                    if sent_result.get("affected_stocks"):
                        sentiment_payload["affected_stocks"] = sent_result["affected_stocks"]
                    if sent_result.get("model"):
                        sentiment_payload["model"] = sent_result["model"]
                    sentiment = _json.dumps(sentiment_payload, ensure_ascii=False)

                    new_articles.append({
                        "title": title, "summary": summary[:500], "url": link,
                        "source": source, "published": published, "news_hash": news_hash,
                        "category": category, "tags": tags, "sentiment": sentiment,
                        "published_iso": normalize_published(published),
                    })
            except Exception as e:
                logger.warning("RSS 수집 실패 (%s): %s", source, e)

        # 배치 임베딩 (한 번에 처리)
        if new_articles:
            try:
                from src.news.embeddings import embed_texts
                texts = [f"{a['title']} {a['summary']}" for a in new_articles]
                embeddings = embed_texts(texts)
            except Exception as e:
                logger.warning("임베딩 실패 (텍스트만 저장): %s", e)
                embeddings = [None] * len(new_articles)

            for article, emb in zip(new_articles, embeddings):
                db.save_news_enriched(embedding=emb, **article)
                total += 1

            # 시맨틱 검색 캐시 무효화
            if total > 0:
                try:
                    from src.news.search import get_search_engine
                    get_search_engine().invalidate()
                except Exception:
                    pass

                # 산업 지수 갱신 (시간당 1회로 제한 — DB 부풀림 방지)
                now = datetime.now(timezone.utc)
                should_refresh = (
                    self._last_index_refresh is None
                    or (now - self._last_index_refresh).total_seconds() >= self._index_refresh_min_interval
                )
                if should_refresh:
                    try:
                        from src.services.industry_index import refresh_industry_index
                        refresh_industry_index()
                        self._last_index_refresh = now
                    except Exception as e:
                        logger.warning("산업 지수 갱신 실패: %s", e)

        self._collected += total
        self._purge_expired()
        return total

    def _purge_expired(self):
        """48시간 초과 뉴스 파기. 파기 전 AI 보존 마킹."""
        from src.news.classifier import should_keep

        db = get_db()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        cutoff_iso = cutoff.isoformat()

        # 1단계: 파기 대상 중 보존할 뉴스 마킹
        candidates = db.get_purge_candidates(cutoff_iso)
        kept_count = 0
        for row in candidates:
            if should_keep(row["sentiment"]):
                db.mark_kept(row["id"])
                kept_count += 1

        # 2단계: kept=0인 오래된 뉴스 삭제
        deleted = db.purge_old_news(cutoff_iso)

        if kept_count > 0 or deleted > 0:
            logger.info("뉴스 파기: %d건 삭제, %d건 보존 마킹", deleted, kept_count)

        # 3단계: 삭제 시 검색 캐시 무효화
        if deleted > 0:
            try:
                from src.news.search import get_search_engine
                get_search_engine().invalidate()
            except Exception:
                pass

    def recent(self, count: int = 20) -> list[dict]:
        """최근 뉴스 조회."""
        return get_db().get_news(count=count)

    def search(self, keyword: str, count: int = 20) -> list[dict]:
        """키워드로 뉴스 검색."""
        return get_db().search_news(keyword=keyword, count=count)

    def status(self) -> dict:
        return {
            "running": self._running,
            "interval": self._interval,
            "feeds": len(RSS_FEEDS),
            "total_collected": self._collected,
        }
