"""Tests for news sentiment and ETF news-score calibration."""

import json
import sqlite3
from datetime import datetime

from src.news.classifier import analyze_sentiment
from src.services.etf_signal import _calc_news_score


def test_overvaluation_warning_is_not_treated_as_bullish():
    result = analyze_sentiment(
        "`오늘도 신고가 찍었는데`…삼성전자·SK하이닉스 `고평가` 경고 확산, 왜?"
    )

    assert result["score"] <= 0
    assert result["sentiment"] in ("neutral", "negative", "strong_negative")


def test_etf_news_score_dedupes_keyword_and_category_matches():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE news (id INTEGER PRIMARY KEY, timestamp TEXT, category TEXT, title TEXT, tags TEXT, sentiment TEXT)"
    )
    conn.execute(
        "INSERT INTO news VALUES (?, ?, ?, ?, ?, ?)",
        (
            1,
            datetime.now().isoformat(),
            "반도체/IT",
            "삼성전자 SK하이닉스 반도체 랠리",
            json.dumps(["삼성전자", "SK하이닉스"], ensure_ascii=False),
            json.dumps({"score": 2.0}, ensure_ascii=False),
        ),
    )

    score = _calc_news_score(
        conn,
        {"category": ["반도체/IT"], "keywords": ["삼성전자", "SK하이닉스"]},
    )

    assert 0 < score < 4
