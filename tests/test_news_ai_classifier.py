"""Tests for AI-based news sentiment classifier (agentcli-backed).

Wraps agentcli.LLMClient with explicit provider/model selection and a
structured JSON output contract. Output shape matches the existing regex-based
`analyze_sentiment()` so the collector can swap implementations transparently.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _fake_response(text: str, *, tokens: int = 100):
    return SimpleNamespace(
        content=text,
        provider="claude",
        model="claude-haiku-4-5",
        tokens=SimpleNamespace(total_tokens=tokens),
        error=None,
        error_type=None,
    )


@pytest.fixture
def classifier_mod():
    import importlib
    import src.news.ai_classifier as m
    importlib.reload(m)
    return m


def test_classify_parses_strong_positive_response(classifier_mod):
    """AI returns structured JSON — classifier parses to analyze_sentiment() shape."""
    fake_client = MagicMock()
    fake_client.chat.return_value = _fake_response(json.dumps({
        "sentiment": "strong_positive",
        "score": 7.5,
        "impact": "high",
        "reasons": ["사상 최고가", "외국인 매수 유입"],
        "affected_stocks": ["005930"],
    }))

    c = classifier_mod.AISentimentClassifier(
        provider="claude", model="claude-haiku-4-5", client=fake_client)
    result = c.classify("삼성전자, 사상 최고가 돌파 외국인 매수 유입", "")

    assert result["sentiment"] == "strong_positive"
    assert result["score"] == 7.5
    assert result["impact"] == "high"
    assert "사상 최고가" in result["reasons"]
    assert result["label"]  # 한국어 라벨이 부여됨


def test_classify_passes_provider_and_model_explicitly(classifier_mod):
    """agentcli의 다중 모델 선택 — chat() 호출에 provider/model이 정확히 전달."""
    fake_client = MagicMock()
    fake_client.chat.return_value = _fake_response(json.dumps({
        "sentiment": "neutral", "score": 0.0, "impact": "low", "reasons": []}))

    c = classifier_mod.AISentimentClassifier(
        provider="copilot", model="claude-sonnet-4.6", client=fake_client,
        fallback=True)
    c.classify("test title")

    kwargs = fake_client.chat.call_args.kwargs
    assert kwargs["provider"] == "copilot"
    assert kwargs["model"] == "claude-sonnet-4.6"
    assert kwargs["fallback"] is True
    assert kwargs["owner"] == "news-classifier"


def test_classify_robust_to_extra_text_around_json(classifier_mod):
    """LLM이 JSON 앞뒤로 설명 문장을 덧붙여도 파싱 성공."""
    fake_client = MagicMock()
    fake_client.chat.return_value = _fake_response(
        '이 뉴스는 강한 호재입니다.\n```json\n'
        '{"sentiment": "strong_positive", "score": 5.0, "impact": "high", "reasons": ["호재"]}\n'
        '```\n끝.'
    )
    c = classifier_mod.AISentimentClassifier(
        provider="claude", model="haiku", client=fake_client)
    result = c.classify("삼성 신고가")
    assert result["sentiment"] == "strong_positive"


def test_classify_falls_back_to_regex_on_ai_failure(classifier_mod):
    """AI 응답이 비거나 파싱 실패하면 정규식 분류로 폴백."""
    fake_client = MagicMock()
    fake_client.chat.return_value = _fake_response("")  # empty content
    c = classifier_mod.AISentimentClassifier(
        provider="claude", model="haiku", client=fake_client,
        fallback_to_regex=True)
    # 정규식이 잡을 키워드 포함
    result = c.classify("코스피 사상 최고가 돌파")
    # 정규식 폴백이 정상 작동했는지 — strong_positive (사상최고 +3, 돌파 +2)
    assert result["sentiment"] in ("strong_positive", "positive")
    assert result.get("source") == "regex-fallback"


def test_news_ai_config_endpoint_round_trip(tmp_path, monkeypatch):
    """GET/POST /api/news/ai-config persist and read back."""
    from fastapi.testclient import TestClient
    import src.news.ai_classifier as m
    monkeypatch.setattr(m, "_NEWS_CONFIG_PATH", tmp_path / "news_config.json")
    from src.web.app import app
    c = TestClient(app)
    # 기본값 (file 없음)
    r = c.get("/api/news/ai-config").json()
    assert r["ai_enabled"] is False
    # 변경
    c.post("/api/news/ai-config", json={
        "ai_enabled": True, "provider": "copilot",
        "model": "claude-haiku-4.5", "timeout_seconds": 20,
    })
    r2 = c.get("/api/news/ai-config").json()
    assert r2["ai_enabled"] is True
    assert r2["provider"] == "copilot"
    assert r2["model"] == "claude-haiku-4.5"
    assert r2["timeout_seconds"] == 20


def test_classify_handles_stock_specific_impact(classifier_mod):
    """AI 응답의 affected_stocks 필드가 결과에 포함됨 — 종목별 영향 분리."""
    fake_client = MagicMock()
    fake_client.chat.return_value = _fake_response(json.dumps({
        "sentiment": "neutral",
        "score": 0.0,
        "impact": "medium",
        "reasons": ["삼성전자에는 호재, SK하이닉스에는 악재"],
        "affected_stocks": [
            {"code": "005930", "name": "삼성전자", "sentiment": "positive", "score": 3},
            {"code": "000660", "name": "SK하이닉스", "sentiment": "negative", "score": -3},
        ],
    }))
    c = classifier_mod.AISentimentClassifier(
        provider="claude", model="haiku", client=fake_client)
    result = c.classify("삼성전자 호재, SK하이닉스 악재")
    assert "affected_stocks" in result
    by_code = {s["code"]: s for s in result["affected_stocks"]}
    assert by_code["005930"]["sentiment"] == "positive"
    assert by_code["000660"]["sentiment"] == "negative"
