"""기존 뉴스 데이터 벡터화 + 분류 백필.

Usage:
    python -m src.news.backfill
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.db import get_db
from src.news.classifier import classify, extract_tags, analyze_sentiment
from src.news.embeddings import embed_texts

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def backfill_news():
    """embedding이 NULL인 뉴스에 대해 분류/태그/임베딩 생성."""
    db = get_db()
    rows = db.get_news_for_embedding(limit=5000)
    if not rows:
        logger.info("백필 대상 없음")
        return 0

    logger.info("백필 시작: %d건", len(rows))

    # 배치 임베딩
    texts = [f"{r['title']} {r.get('summary', '')}" for r in rows]
    embeddings = embed_texts(texts)

    for row, emb in zip(rows, embeddings):
        category = classify(row["title"], row.get("summary", ""), row.get("source", ""))
        tags = json.dumps(extract_tags(row["title"], row.get("summary", "")), ensure_ascii=False)
        sent_result = analyze_sentiment(row["title"], row.get("summary", ""))
        sentiment = json.dumps({
            "sentiment": sent_result["sentiment"],
            "score": sent_result["score"],
            "impact": sent_result["impact"],
            "reasons": sent_result["reasons"],
            "label": sent_result["label"],
        }, ensure_ascii=False)
        db.update_news_enrichment(row["id"], category, tags, sentiment, emb)

    logger.info("백필 완료: %d건 처리", len(rows))
    return len(rows)


if __name__ == "__main__":
    backfill_news()
