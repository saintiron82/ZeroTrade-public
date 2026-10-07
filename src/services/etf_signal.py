"""ETF 시그널 엔진 — 뉴스 감성 + 가격 모멘텀을 종합하여 ETF별 방향 점수를 산출.

봇들이 이 점수를 보고 "지금 이 ETF가 오를 분위기인가, 내릴 분위기인가"를 판단.

점수 범위: -10 ~ +10
  +5 이상: 강한 매수 신호
  +2 ~ +5: 약한 매수 신호
  -2 ~ +2: 중립
  -5 ~ -2: 약한 매도 신호
  -5 이하: 강한 매도 신호
"""

import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)


def _time_weight(published_iso: str, now: datetime) -> float:
    """최신 뉴스일수록 높은 가중치. 반감기 6시간 지수 감쇠."""
    if not published_iso:
        return 0.1
    try:
        pub = datetime.fromisoformat(published_iso)
        if pub.tzinfo:
            pub = pub.replace(tzinfo=None)
        hours_ago = max(0, (now - pub).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.1
    return math.exp(-hours_ago * math.log(2) / 6)


def _impact_weight(impact: str) -> float:
    """영향도에 따른 가중치."""
    return {"high": 2.0, "medium": 1.0, "low": 0.5}.get(impact, 0.5)

DB_PATH = PROJECT_ROOT / "data" / "ff.db"

# ETF 코드 ↔ 카테고리/키워드 매핑
ETF_MAPPING = {
    "069500": {"name": "KODEX 200", "keywords": ["코스피", "KOSPI", "시장", "증시"], "category": ["코스피/코스닥"]},
    "091160": {"name": "KODEX 반도체", "keywords": ["반도체", "삼성전자", "SK하이닉스", "HBM", "D램", "낸드"], "category": ["반도체/IT"]},
    "091170": {"name": "KODEX 은행", "keywords": ["은행", "금리", "기준금리", "한은", "금융"], "category": ["금리/환율"]},
    "091180": {"name": "KODEX 자동차", "keywords": ["자동차", "현대차", "기아", "EV", "전기차"], "category": ["자동차"]},
    "102780": {"name": "KODEX 삼성그룹", "keywords": ["삼성", "삼성전자", "삼성SDI"], "category": ["반도체/IT"]},
    "102970": {"name": "KODEX 증권", "keywords": ["증권", "증시", "IPO", "주식시장"], "category": ["코스피/코스닥"]},
    "114800": {"name": "KODEX 인버스", "keywords": ["하락", "급락", "폭락", "위기", "공포"], "category": ["코스피/코스닥"], "inverse": True},
    "117460": {"name": "KODEX 에너지화학", "keywords": ["에너지", "화학", "석유", "정유", "배터리"], "category": ["에너지"]},
    "117680": {"name": "KODEX 철강", "keywords": ["철강", "철광석", "포스코", "현대제철"], "category": []},
    "117700": {"name": "KODEX 건설", "keywords": ["건설", "부동산", "아파트", "인프라"], "category": ["정책/규제"]},
    "122630": {"name": "KODEX 레버리지", "keywords": ["코스피", "상승", "강세", "반등"], "category": ["코스피/코스닥"]},
    "130680": {"name": "TIGER 원유선물", "keywords": ["원유", "유가", "WTI", "OPEC", "석유"], "category": ["에너지"]},
    "132030": {"name": "KODEX 골드선물", "keywords": ["금", "골드", "금값", "안전자산", "인플레이션"], "category": []},
    "133690": {"name": "TIGER 미국나스닥100", "keywords": ["나스닥", "미국", "빅테크", "엔비디아", "애플"], "category": ["미국증시"]},
    "139260": {"name": "TIGER 200 IT", "keywords": ["IT", "반도체", "삼성전자", "테크"], "category": ["반도체/IT"]},
    "305720": {"name": "KODEX 2차전지", "keywords": ["2차전지", "배터리", "리튬", "양극재", "LG에너지"], "category": ["에너지"]},
}


def calculate_signals() -> dict[str, dict]:
    """모든 ETF에 대해 종합 시그널 점수를 계산.

    뉴스 감성 + 가격 모멘텀을 결합.
    - news_score: -10~+10 (뉴스 기반)
    - momentum_score: -10~+10 (가격 변동 기반)
    - score: 종합 점수 (가중 평균)
    - alignment: 시그널과 추세 일치 여부

    Returns:
        {etf_code: {score, news_score, momentum_score, direction, alignment, reasons}}
    """
    signals = {}

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 가격 모멘텀 일괄 수집
    momentum_data = _calc_momentum_batch()

    for code, info in ETF_MAPPING.items():
        news_score = _calc_news_score(conn, info)
        momentum = momentum_data.get(code, {})
        momentum_score = momentum.get("momentum_score", 0)
        change_pct = momentum.get("change_pct", 0)

        # 인버스 ETF는 뉴스 점수 반전 (하락 뉴스가 인버스에는 호재)
        if info.get("inverse"):
            news_score = -news_score

        # 종합 점수: 뉴스 40% + 모멘텀 60% (가격이 더 중요)
        combined = news_score * 0.4 + momentum_score * 0.6
        combined = max(-10, min(10, combined))

        # 시그널-추세 일치도 판단
        news_dir = "UP" if news_score > 1 else "DOWN" if news_score < -1 else "FLAT"
        price_dir = "UP" if momentum_score > 1 else "DOWN" if momentum_score < -1 else "FLAT"

        if news_dir == price_dir and news_dir != "FLAT":
            alignment = "ALIGNED"  # 시그널과 추세 일치 → 강한 신호
        elif news_dir != "FLAT" and price_dir != "FLAT" and news_dir != price_dir:
            alignment = "DIVERGENT"  # 시그널과 추세 불일치 → 주의
        else:
            alignment = "NEUTRAL"

        direction = "UP" if combined > 1 else "DOWN" if combined < -1 else "FLAT"

        reasons = []
        if alignment == "ALIGNED" and direction == "UP":
            reasons.append("시그널+추세 일치 상승")
        elif alignment == "ALIGNED" and direction == "DOWN":
            reasons.append("시그널+추세 일치 하락")
        elif alignment == "DIVERGENT":
            reasons.append(f"시그널({news_dir})≠추세({price_dir}) 불일치")

        signals[code] = {
            "code": code,
            "name": info["name"],
            "score": round(combined, 1),
            "news_score": round(news_score, 1),
            "momentum_score": round(momentum_score, 1),
            "change_pct": round(change_pct, 2),
            "direction": direction,
            "alignment": alignment,
            "reasons": reasons,
            "updated_at": datetime.now().isoformat(),
        }

    conn.close()
    return signals


def _calc_momentum_batch() -> dict[str, dict]:
    """모든 ETF의 가격 모멘텀을 일괄 계산.

    Returns:
        {code: {change_pct, momentum_score}}
    """
    results = {}
    try:
        from src.kis.market import KISMarket
        market = KISMarket()
        import time as _time

        for code in ETF_MAPPING:
            try:
                info = market.get_price(code)
                if info and info.get("price", 0) > 0 and info.get("open", 0) > 0:
                    price = info["price"]
                    open_price = info["open"]
                    change_pct = (price - open_price) / open_price * 100

                    # 모멘텀 점수: 등락률을 -10~+10 스케일로 변환
                    # ±3% = ±10점 (포화)
                    momentum_score = max(-10, min(10, change_pct / 3 * 10))

                    results[code] = {
                        "change_pct": change_pct,
                        "momentum_score": momentum_score,
                        "price": price,
                        "open": open_price,
                    }
            except Exception:
                pass
            _time.sleep(0.05)  # 레이트 리밋

        market.close()
    except Exception as e:
        logger.warning("모멘텀 계산 실패: %s", e)

    return results


def _calc_news_score(conn: sqlite3.Connection, info: dict) -> float:
    """뉴스 감성 점수 계산 — 카테고리/키워드 매칭.

    같은 뉴스가 카테고리와 여러 키워드에 동시에 걸려도 한 번만 반영한다.
    뉴스 건수는 확신도만 완만하게 높이고, 중립~약호재 기사가 많다는 이유로
    +10 신호까지 포화되지 않게 제한한다.
    """
    matched: dict[int, tuple[float, float]] = {}
    cutoff = (datetime.now() - timedelta(hours=12)).isoformat()

    def add_match(row, weight: float = 1.0):
        news_id = int(row["id"])
        score = _parse_sentiment_score(row["sentiment"])
        current = matched.get(news_id)
        if current is None or weight > current[1]:
            matched[news_id] = (score, weight)

    # 1. 카테고리 매칭
    for cat in info.get("category", []):
        rows = conn.execute(
            "SELECT id, sentiment FROM news WHERE category = ? AND timestamp > ?",
            (cat, cutoff)
        ).fetchall()
        for r in rows:
            add_match(r, 1.0)

    # 2. 키워드 매칭 (제목 + 태그)
    for kw in info.get("keywords", []):
        rows = conn.execute(
            "SELECT id, sentiment FROM news WHERE (title LIKE ? OR tags LIKE ?) AND timestamp > ?",
            (f"%{kw}%", f"%{kw}%", cutoff)
        ).fetchall()
        for r in rows:
            add_match(r, 1.5)

    if not matched:
        return 0.0

    weighted_sum = sum(score * weight for score, weight in matched.values())
    total_weight = sum(weight for _, weight in matched.values())
    avg_score = weighted_sum / max(total_weight, 1.0)
    confidence = min(1.35, 0.85 + 0.05 * len(matched))
    return max(-10, min(10, avg_score * confidence))


def _parse_sentiment_score(sentiment_str: str) -> float:
    """DB의 sentiment 필드에서 점수 추출."""
    if not sentiment_str:
        return 0.0
    try:
        data = json.loads(sentiment_str)
        return float(data.get("score", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        # 문자열인 경우
        if "positive" in str(sentiment_str):
            return 2.0
        elif "negative" in str(sentiment_str):
            return -2.0
        return 0.0


def calc_category_trends(hours: int = 48) -> dict[str, dict]:
    """카테고리별 시간 가중 뉴스 트렌드 점수.

    산업 지수 엔진이 있으면 위임, 실패 시 레거시 로직 fallback.

    Returns:
        {"반도체/IT": {"score": +3.2, "count": 12, "direction": "UP",
                      "breakdown": {"positive": 8, "negative": 2, "neutral": 2}}, ...}
    """
    try:
        from src.services.industry_index import get_industry_index
        indices = get_industry_index()
        if indices:
            return _convert_index_to_legacy(indices)
    except Exception:
        pass
    return _calc_category_trends_legacy(hours)


def _convert_index_to_legacy(indices: dict) -> dict[str, dict]:
    """산업 지수 → 기존 category_trends 형식 변환."""
    direction_map = {"급등": "UP", "상승": "UP", "보합": "FLAT", "하락": "DOWN", "급락": "DOWN"}
    result = {}
    for sector, data in indices.items():
        result[sector] = {
            "score": data["score"],
            "count": data["news_count"],
            "direction": direction_map.get(data["direction"], "FLAT"),
            "breakdown": data.get("breakdown", {}),
        }
    return result


def _calc_category_trends_legacy(hours: int = 48) -> dict[str, dict]:
    """레거시 카테고리 트렌드 계산 (fallback)."""
    db_path = PROJECT_ROOT / "data" / "ff.db"
    if not db_path.exists():
        return {}

    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).isoformat()

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT category, sentiment, published_iso FROM news "
            "WHERE published_iso >= ? AND category != '' "
            "ORDER BY published_iso DESC",
            (cutoff,)
        ).fetchall()
        conn.close()
    except Exception:
        return {}

    categories: dict[str, dict] = {}

    for row in rows:
        cat = row["category"]
        if cat not in categories:
            categories[cat] = {
                "weighted_sum": 0.0, "total_weight": 0.0,
                "count": 0, "breakdown": {}
            }

        try:
            sent = json.loads(row["sentiment"]) if row["sentiment"] else {}
        except (json.JSONDecodeError, TypeError):
            sent = {}

        score = sent.get("score", 0)
        if not isinstance(score, (int, float)):
            score = 0
        impact = sent.get("impact", "low")

        tw = _time_weight(row["published_iso"], now)
        iw = _impact_weight(impact)

        categories[cat]["weighted_sum"] += score * tw * iw
        categories[cat]["total_weight"] += tw * iw
        categories[cat]["count"] += 1

        label = sent.get("sentiment", "neutral")
        categories[cat]["breakdown"][label] = categories[cat]["breakdown"].get(label, 0) + 1

    result = {}
    for cat, data in categories.items():
        if data["total_weight"] > 0:
            avg_score = data["weighted_sum"] / data["total_weight"]
            final_score = max(-10, min(10, avg_score))
        else:
            final_score = 0.0

        result[cat] = {
            "score": round(final_score, 1),
            "count": data["count"],
            "direction": "UP" if final_score > 2 else "DOWN" if final_score < -2 else "FLAT",
            "breakdown": data["breakdown"],
        }

    return result


def get_category_trends() -> dict[str, dict]:
    """카테고리 트렌드 반환 (캐시 활용)."""
    cache_path = PROJECT_ROOT / "data" / "etf_signals.json"
    try:
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            updated = data.get("_updated_at", "")
            if updated:
                age = (datetime.now() - datetime.fromisoformat(updated)).total_seconds()
                if age < 600 and "category_trends" in data:
                    return data["category_trends"]
    except Exception:
        pass
    return calc_category_trends()


def get_etf_signals() -> dict[str, dict]:
    """ETF 시그널 조회 (캐시 파일 우선, 없으면 계산)."""
    cache_path = PROJECT_ROOT / "data" / "etf_signals.json"
    try:
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            # 10분 이내면 캐시 사용
            updated = data.get("_updated_at", "")
            if updated:
                age = (datetime.now() - datetime.fromisoformat(updated)).total_seconds()
                if age < 600:
                    return data.get("signals", {})
    except Exception:
        pass

    signals = calculate_signals()
    try:
        trends = calc_category_trends()
        cache_path.write_text(json.dumps({
            "_updated_at": datetime.now().isoformat(),
            "signals": signals,
            "category_trends": trends,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    return signals


def refresh_signals():
    """시그널 강제 갱신."""
    signals = calculate_signals()
    cache_path = PROJECT_ROOT / "data" / "etf_signals.json"
    try:
        trends = calc_category_trends()
        cache_path.write_text(json.dumps({
            "_updated_at": datetime.now().isoformat(),
            "signals": signals,
            "category_trends": trends,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return signals
