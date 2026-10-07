"""AI 기반 뉴스 감성 분류기 — agentcli 정식 호출.

기존 정규식(`classifier.analyze_sentiment`)이 못 잡는 케이스:
- 맥락("우려 속 반등"의 양면성)
- 반전 구조("호재로 알려졌으나 미미")
- 종목별 영향 분리 (한 헤드라인에 여러 종목 다른 방향)
- 신규 이슈 (사전 등록 패턴 밖)

이 분류기는 agentcli.LLMClient.chat()을 provider/model 명시 호출하고
구조화된 JSON으로 결과를 받는다. 출력 shape은 기존 analyze_sentiment()와
호환 — 콜렉터가 투명하게 교체 가능. AI 실패 시 정규식으로 폴백.

설정 파일: data/news_config.json (ai_enabled, provider, model, fallback ...).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

_NEWS_CONFIG_PATH = PROJECT_ROOT / "data" / "news_config.json"

# Shared LLMClient singleton — 다른 진입점(채팅·봇)과 동일 클라이언트 재사용.
_shared_client = None


def _get_shared_client():
    """채팅·봇과 동일한 LLMClient 인스턴스를 재사용 (싱글톤)."""
    global _shared_client
    if _shared_client is None:
        from agentcli import LLMClient, MemoryStore
        from agentcli.providers.registry import create_default_registry
        _shared_client = LLMClient(
            store=MemoryStore(max_conversations=500, ttl_hours=24),
            registry=create_default_registry(),
        )
    return _shared_client


def load_news_config() -> dict:
    """뉴스 분류기 설정 로드. 파일 없으면 안전한 기본값 반환."""
    defaults = {
        "ai_enabled": False,  # 안전 기본값: 비활성
        "provider": "claude",
        "model": "claude-haiku-4-5",
        "fallback_to_regex": True,
        "ai_fallback_enabled": True,  # agentcli provider fallback 체인
        "timeout_seconds": 30,
        "include_affected_stocks": True,
    }
    try:
        if _NEWS_CONFIG_PATH.exists():
            cfg = json.loads(_NEWS_CONFIG_PATH.read_text(encoding="utf-8"))
            return {**defaults, **cfg}
    except Exception as e:
        logger.warning("news_config 로드 실패, 기본값 사용: %s", e)
    return defaults


def save_news_config(cfg: dict):
    """뉴스 분류기 설정 저장."""
    _NEWS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _NEWS_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# AI에게 보낼 프롬프트 — JSON only 출력, 한국어 금융 뉴스 컨텍스트.
_PROMPT_TEMPLATE = """\
당신은 한국 금융 뉴스 감성 분석 전문가입니다. 다음 뉴스 헤드라인(및 요약)을
주식 거래 관점에서 분석하고 JSON으로만 응답하세요.

[헤드라인]
{title}

[요약]
{summary}

[분류 기준]
- sentiment: strong_positive | positive | neutral | negative | strong_negative
- score: -10 ~ +10 정수/실수
- impact: high | medium | low (거래에 미칠 영향 강도)
- reasons: 판단 근거 키 포인트 1~5개 (간결한 한국어)
- affected_stocks: 영향받는 구체적 종목 리스트. 각 항목 {{code: 6자리, name, sentiment, score}}.
  종목 특정 안 되면 빈 배열.

[주의사항]
- "우려 속 반등" 같은 양면 헤드라인은 실시간 시장 영향을 우선
- "~로 알려졌으나 실제론 미미" 같은 반전 구조는 반대 방향으로
- 한 헤드라인에 여러 종목 영향이 갈리면 affected_stocks로 분리, 전체 sentiment는 neutral

JSON만 출력. 다른 텍스트 금지:
{{"sentiment": "...", "score": 0, "impact": "...", "reasons": [...], "affected_stocks": [...]}}
"""


_SENTIMENT_LABELS = {
    "strong_positive": "강한 호재",
    "positive": "호재",
    "neutral": "중립",
    "negative": "악재",
    "strong_negative": "강한 악재",
}


class AISentimentClassifier:
    """agentcli 기반 뉴스 감성 분류기.

    출력 shape은 src/news/classifier.py의 analyze_sentiment()와 호환:
        {sentiment, score, impact, reasons, label,
         [affected_stocks], [source]}
    """

    def __init__(self, provider: str = "claude",
                 model: str = "claude-haiku-4-5",
                 client=None,
                 fallback: bool = True,
                 fallback_to_regex: bool = True,
                 timeout: int = 30):
        """
        Args:
            provider: agentcli provider id (claude/codex/copilot)
            model: 해당 provider의 모델 id (catalog는 /api/ai/models)
            client: 외부 주입(테스트용). None이면 공유 LLMClient
            fallback: agentcli 내부 provider fallback 체인 사용 여부
            fallback_to_regex: AI 호출 실패 시 정규식으로 폴백
            timeout: AI 호출 타임아웃 (초)
        """
        self._provider = provider
        self._model = model
        self._client = client  # lazy via property
        self._fallback = fallback
        self._fallback_to_regex = fallback_to_regex
        self._timeout = timeout

    @property
    def client(self):
        if self._client is None:
            self._client = _get_shared_client()
        return self._client

    def classify(self, title: str, summary: str = "") -> dict:
        """단일 뉴스 항목 감성 분류."""
        prompt = _PROMPT_TEMPLATE.format(
            title=title or "", summary=summary or "")
        try:
            response = self.client.chat(
                prompt,
                provider=self._provider,
                model=self._model,
                owner="news-classifier",
                # conversation_id 미지정 — 각 뉴스 항목은 독립 평가
                timeout=self._timeout,
                fallback=self._fallback,
            )
            result = self._parse_response(response.content)
            if result is not None:
                result["label"] = _SENTIMENT_LABELS.get(result["sentiment"], "중립")
                result["source"] = "ai"
                result["model"] = getattr(response, "model", "") or self._model
                result["provider"] = getattr(response, "provider", "") or self._provider
                return result
            logger.warning("[news-ai] 응답 파싱 실패, 정규식 폴백: %s", title[:40])
        except Exception as e:
            logger.warning("[news-ai] 호출 예외, 정규식 폴백: %s — %s", title[:40], e)

        # AI 실패 → 정규식 폴백
        if self._fallback_to_regex:
            from src.news.classifier import analyze_sentiment
            r = analyze_sentiment(title, summary)
            r["source"] = "regex-fallback"
            return r
        return {
            "sentiment": "neutral", "score": 0.0, "impact": "low",
            "reasons": ["분류 실패"],
            "label": _SENTIMENT_LABELS["neutral"], "source": "error",
        }

    @staticmethod
    def _parse_response(content: str) -> dict | None:
        if not content:
            return None
        # JSON 블록 추출: ```json ... ``` 또는 첫 { ... } 매칭
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        sentiment = data.get("sentiment", "neutral")
        if sentiment not in _SENTIMENT_LABELS:
            return None
        score = float(data.get("score", 0) or 0)
        impact = data.get("impact", "low")
        if impact not in ("high", "medium", "low"):
            impact = "low"
        reasons = data.get("reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        out = {
            "sentiment": sentiment,
            "score": round(score, 2),
            "impact": impact,
            "reasons": [str(r)[:80] for r in reasons[:5]],
        }
        affected = data.get("affected_stocks")
        if isinstance(affected, list) and affected:
            out["affected_stocks"] = affected
        return out


def get_default_classifier() -> AISentimentClassifier:
    """현재 news_config 기반 분류기 (싱글톤 LLMClient 공유)."""
    cfg = load_news_config()
    return AISentimentClassifier(
        provider=cfg.get("provider", "claude"),
        model=cfg.get("model", "claude-haiku-4-5"),
        fallback=cfg.get("ai_fallback_enabled", True),
        fallback_to_regex=cfg.get("fallback_to_regex", True),
        timeout=int(cfg.get("timeout_seconds", 30)),
    )
