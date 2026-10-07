"""산업 지수 엔진 — 뉴스 감성/볼륨/속도/집중도를 종합하여 섹터·서브섹터별 복합 지수 산출.

점수 범위: -10 ~ +10
  급등: score ≥ 5 or velocity ≥ 3
  상승: score ≥ 2
  보합: -2 < score < 2
  하락: score ≤ -2
  급락: score ≤ -5 or velocity ≤ -3
"""

import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.config import PROJECT_ROOT
from src.services.etf_signal import _impact_weight

logger = logging.getLogger(__name__)

DB_PATH = PROJECT_ROOT / "data" / "ff.db"
CACHE_PATH = PROJECT_ROOT / "data" / "industry_index.json"
CACHE_TTL = 600  # 10분

# 시간 가중치 반감기: 2시간 (최신 뉴스 가치가 압도적)
HALF_LIFE_HOURS = 2.0


def _time_weight(published_iso: str, now: datetime) -> float:
    """최신 뉴스일수록 높은 가중치. 반감기 2시간 지수 감쇠.

    1시간 전: 0.71, 2시간 전: 0.50, 4시간 전: 0.25, 8시간 전: 0.06
    """
    if not published_iso:
        return 0.05
    try:
        pub = datetime.fromisoformat(published_iso)
        if pub.tzinfo:
            pub = pub.replace(tzinfo=None)
        hours_ago = max(0, (now - pub).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.05
    return math.exp(-hours_ago * math.log(2) / HALF_LIFE_HOURS)

# ══════════════════════════════════════════
# 섹터 → 서브섹터 분류 체계
# ══════════════════════════════════════════

SECTOR_TAXONOMY: dict[str, dict] = {
    "반도체/IT": {
        "sub_sectors": {
            "AI반도체": ["AI", "AI반도체", "엔비디아", "GPU"],
            "HBM/메모리": ["HBM", "D램", "DDR", "메모리", "낸드"],
            "파운드리": ["파운드리", "TSMC", "시스템반도체"],
            "디스플레이": ["디스플레이", "OLED", "LG디스플레이"],
            "IT일반": ["삼성전자", "SK하이닉스", "반도체"],
        },
        "related_etfs": ["091160", "139260", "102780"],
    },
    "에너지": {
        "sub_sectors": {
            "2차전지": ["2차전지", "배터리", "리튬", "양극재", "LG에너지", "삼성SDI"],
            "원유/정유": ["원유", "유가", "WTI", "OPEC", "정유"],
            "신재생": ["태양광", "풍력", "수소"],
            "원자력": ["원자력", "원전", "SMR"],
        },
        "related_etfs": ["117460", "305720", "130680"],
    },
    "금리/환율": {
        "sub_sectors": {
            "금리정책": ["금리", "기준금리", "한국은행", "금통위", "통화정책"],
            "환율": ["환율", "원달러", "원/달러"],
            "인플레이션": ["CPI", "인플레이션", "디플레이션"],
        },
        "related_etfs": ["091170"],
    },
    "미국증시": {
        "sub_sectors": {
            "빅테크": ["나스닥", "빅테크", "엔비디아", "애플", "마이크로소프트"],
            "연준정책": ["FOMC", "연준", "파월"],
            "미국경제": ["미국경제", "미국증시", "뉴욕증시", "트럼프"],
        },
        "related_etfs": ["133690"],
    },
    "자동차": {
        "sub_sectors": {
            "전기차": ["전기차", "EV", "테슬라", "하이브리드"],
            "완성차": ["현대차", "기아", "완성차", "자동차"],
            "자율주행": ["자율주행"],
        },
        "related_etfs": ["091180"],
    },
    "바이오": {
        "sub_sectors": {
            "신약/임상": ["신약", "임상", "FDA", "허가"],
            "바이오시밀러": ["셀트리온", "삼성바이오", "바이오시밀러"],
            "제약일반": ["제약", "의약품", "바이오"],
        },
        "related_etfs": [],
    },
    "코스피/코스닥": {
        "sub_sectors": {
            "시장전체": ["코스피", "코스닥", "주가지수"],
            "수급": ["외국인", "기관", "개인투자자", "수급"],
        },
        "related_etfs": ["069500", "122630", "114800"],
    },
    "정책/규제": {
        "sub_sectors": {
            "금융규제": ["금감원", "금융위", "공매도", "증권거래세"],
            "세제/정책": ["양도세", "대주주", "정책", "규제"],
            "건설/부동산": ["건설", "부동산", "아파트"],
        },
        "related_etfs": ["117700"],
    },
    "ETF": {
        "sub_sectors": {
            "ETF일반": ["ETF", "KODEX", "TIGER", "상장지수"],
        },
        "related_etfs": [],
    },
}

# 교차 영향: (소스 섹터, 소스 방향, 타겟 섹터, 효과 배율)
CROSS_SECTOR_EFFECTS = [
    ("금리/환율", "UP", "바이오", -0.3),
    ("금리/환율", "UP", "코스피/코스닥", -0.2),
    ("미국증시", "UP", "반도체/IT", +0.3),
    ("미국증시", "UP", "코스피/코스닥", +0.2),
    ("에너지", "UP", "자동차", -0.15),
    ("정책/규제", "UP", "코스피/코스닥", +0.15),
]

# 가중치
W_SENTIMENT = 0.40
W_VOLUME = 0.25
W_VELOCITY = 0.25
W_CONCENTRATION = 0.10

# 기본 뉴스 볼륨 기준선 (서브섹터당 12시간 기준)
DEFAULT_VOLUME_BASELINE = 3.0


# ══════════════════════════════════════════
# 핵심 계산
# ══════════════════════════════════════════

def calculate_industry_index() -> dict[str, dict]:
    """전체 섹터 + 서브섹터 지수 계산.

    Returns:
        {
            "반도체/IT": {
                "score": 4.2, "direction": "상승", "news_count": 15,
                "sentiment_avg": 3.1, "volume_signal": 2.0, "velocity": 1.5,
                "momentum": {"1h": +0.8, "3h": +2.1, "6h": ..., "12h": ...},
                "breakdown": {"positive": 8, "negative": 2, ...},
                "sub_sectors": {
                    "AI반도체": {"score": 6.1, "direction": "급등", "news_count": 8, ...},
                    ...
                },
                "cross_effects": ["+0.9 from 미국증시"],
            },
            ...
        }
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    now = datetime.now()

    indices: dict[str, dict] = {}

    for sector, info in SECTOR_TAXONOMY.items():
        sub_results: dict[str, dict] = {}
        sector_news_count = 0
        sector_weighted_score = 0.0

        for sub_name, keywords in info["sub_sectors"].items():
            sub = _calc_subsector(conn, sector, sub_name, keywords, now)
            sub_results[sub_name] = sub
            sector_news_count += sub["news_count"]
            sector_weighted_score += sub["score"] * max(sub["news_count"], 1)

        # 섹터 레벨: 서브섹터 뉴스 건수 가중 평균
        if sector_news_count > 0:
            sector_score = sector_weighted_score / sector_news_count
        else:
            sector_score = 0.0
        sector_score = max(-10, min(10, sector_score))

        # 섹터 breakdown 합산
        breakdown: dict[str, int] = {}
        for sub in sub_results.values():
            for k, v in sub.get("breakdown", {}).items():
                breakdown[k] = breakdown.get(k, 0) + v

        # 섹터 velocity (서브섹터 velocity 가중 평균)
        total_vel_weight = sum(max(s["news_count"], 1) for s in sub_results.values())
        sector_velocity = sum(
            s["velocity"] * max(s["news_count"], 1) for s in sub_results.values()
        ) / max(total_vel_weight, 1)

        direction = _calc_direction(sector_score, sector_velocity)

        # 모멘텀 조회
        momentum = _calc_momentum(conn, sector, "")

        indices[sector] = {
            "score": round(sector_score, 1),
            "direction": direction,
            "news_count": sector_news_count,
            "sentiment_avg": round(sector_score, 1),  # 섹터 레벨에서는 score와 동일
            "volume_signal": 0.0,
            "velocity": round(sector_velocity, 1),
            "momentum": momentum,
            "breakdown": breakdown,
            "sub_sectors": sub_results,
            "cross_effects": [],
        }

    # 2차 패스: 교차 영향
    _apply_cross_sector(indices)

    conn.close()
    return indices


def _calc_subsector(conn: sqlite3.Connection, sector: str, sub_name: str,
                    keywords: list[str], now: datetime,
                    lookback_hours: int = 12) -> dict:
    """서브섹터 1개의 복합 지수 계산."""
    cutoff = (now - timedelta(hours=lookback_hours)).isoformat()
    midpoint = (now - timedelta(hours=lookback_hours / 2)).isoformat()

    # 해당 섹터 + 키워드 매칭 뉴스 조회
    placeholders = " OR ".join(["(title LIKE ? OR tags LIKE ?)"] * len(keywords))
    params: list = []
    for kw in keywords:
        params.extend([f"%{kw}%", f"%{kw}%"])
    params.extend([sector, cutoff])

    query = (
        f"SELECT sentiment, published_iso FROM news "
        f"WHERE ({placeholders}) AND category = ? AND published_iso >= ? "
        f"ORDER BY published_iso DESC"
    )

    rows = conn.execute(query, params).fetchall()

    if not rows:
        return {
            "score": 0.0, "direction": "보합", "news_count": 0,
            "sentiment_avg": 0.0, "volume_signal": 0.0, "velocity": 0.0,
            "breakdown": {},
        }

    # ── 1. 감성 강도 (시간가중 평균) ──
    weighted_sum = 0.0
    total_weight = 0.0
    breakdown: dict[str, int] = {}
    recent_scores: list[float] = []  # 최근 절반
    older_scores: list[float] = []   # 이전 절반

    for row in rows:
        sent = _parse_sentiment(row["sentiment"])
        score = sent.get("score", 0)
        impact = sent.get("impact", "low")
        label = sent.get("sentiment", "neutral")

        tw = _time_weight(row["published_iso"], now)
        iw = _impact_weight(impact)

        weighted_sum += score * tw * iw
        total_weight += tw * iw

        breakdown[label] = breakdown.get(label, 0) + 1

        # velocity 계산용 분류
        pub_iso = row["published_iso"] or ""
        if pub_iso >= midpoint:
            recent_scores.append(score)
        else:
            older_scores.append(score)

    news_count = len(rows)
    sentiment_avg = weighted_sum / total_weight if total_weight > 0 else 0.0
    # -10~+10 범위로 정규화
    sentiment_intensity = max(-10, min(10, sentiment_avg))

    # ── 2. 볼륨 시그널 ──
    baseline = DEFAULT_VOLUME_BASELINE
    volume_signal = (news_count / baseline - 1) * 5
    volume_signal = max(-5, min(5, volume_signal))

    # ── 3. 속도 (velocity) ──
    # 양쪽 모두 뉴스가 있어야 유의미한 변화율 계산 가능
    if recent_scores and older_scores:
        avg_recent = sum(recent_scores) / len(recent_scores)
        avg_older = sum(older_scores) / len(older_scores)
        velocity = (avg_recent - avg_older) * 2
    else:
        velocity = 0.0
    velocity = max(-5, min(5, velocity))

    # ── 4. 집중도 (HHI 기반) ──
    # 동일 섹터 내 다른 서브섹터 대비 이 서브섹터의 비중
    # 단일 서브섹터 계산에서는 self-concentration = 1이므로,
    # breakdown 다양성으로 대체: 한쪽 감성에 치우칠수록 확신↑
    total_bd = sum(breakdown.values()) or 1
    hhi = sum((v / total_bd) ** 2 for v in breakdown.values())
    sign = 1 if sentiment_avg >= 0 else -1
    concentration = hhi * sign * 3
    concentration = max(-3, min(3, concentration))

    # ── 복합 점수 ──
    composite = (
        W_SENTIMENT * sentiment_intensity
        + W_VOLUME * volume_signal
        + W_VELOCITY * velocity
        + W_CONCENTRATION * concentration
    )
    composite = max(-10, min(10, round(composite, 1)))

    direction = _calc_direction(composite, velocity)

    return {
        "score": composite,
        "direction": direction,
        "news_count": news_count,
        "sentiment_avg": round(sentiment_intensity, 1),
        "volume_signal": round(volume_signal, 1),
        "velocity": round(velocity, 1),
        "breakdown": breakdown,
    }


def _apply_cross_sector(indices: dict):
    """교차 영향 적용. indices를 in-place 수정."""
    adjustments: dict[str, list] = {s: [] for s in indices}

    for src_sector, src_dir, tgt_sector, multiplier in CROSS_SECTOR_EFFECTS:
        if src_sector not in indices or tgt_sector not in indices:
            continue
        src_score = indices[src_sector]["score"]
        # 방향 매칭: UP이면 score > 0일 때만, DOWN이면 score < 0일 때만
        if src_dir == "UP" and src_score <= 0:
            continue
        if src_dir == "DOWN" and src_score >= 0:
            continue

        effect = src_score * multiplier
        adjustments[tgt_sector].append((effect, src_sector))

    for sector, effects in adjustments.items():
        if not effects:
            continue
        total_adj = sum(e for e, _ in effects)
        # 교차 조정 합계 ±2점 상한
        total_adj = max(-2, min(2, total_adj))
        indices[sector]["score"] = max(-10, min(10, round(indices[sector]["score"] + total_adj, 1)))
        # 방향 재계산
        indices[sector]["direction"] = _calc_direction(
            indices[sector]["score"], indices[sector]["velocity"]
        )
        indices[sector]["cross_effects"] = [
            f"{e:+.1f} from {src}" for e, src in effects
        ]


def _calc_direction(score: float, velocity: float) -> str:
    if score >= 5 or velocity >= 3:
        return "급등"
    elif score >= 2:
        return "상승"
    elif score <= -5 or velocity <= -3:
        return "급락"
    elif score <= -2:
        return "하락"
    return "보합"


def _calc_momentum(conn: sqlite3.Connection, sector: str, sub_sector: str) -> dict:
    """과거 스냅샷 대비 모멘텀 계산."""
    now = datetime.now()
    momentum = {}
    for label, hours in [("1h", 1), ("3h", 3), ("6h", 6), ("12h", 12)]:
        target_time = (now - timedelta(hours=hours)).isoformat()
        row = conn.execute(
            "SELECT score FROM industry_index "
            "WHERE sector = ? AND sub_sector = ? AND timestamp <= ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (sector, sub_sector, target_time)
        ).fetchone()
        if row:
            momentum[label] = round(0 - row["score"], 1)  # 현재 score는 아직 없으므로 placeholder
        else:
            momentum[label] = 0.0
    return momentum


def _parse_sentiment(sentiment_str: str) -> dict:
    if not sentiment_str:
        return {}
    try:
        return json.loads(sentiment_str)
    except (json.JSONDecodeError, TypeError):
        return {}


# ══════════════════════════════════════════
# 스냅샷 저장/조회
# ══════════════════════════════════════════

def _save_snapshots(indices: dict):
    """현재 지수를 DB industry_index 테이블에 저장."""
    from src.db import get_db
    db = get_db()
    now_iso = datetime.now().isoformat()

    for sector, data in indices.items():
        # 섹터 레벨 스냅샷
        db.save_industry_snapshot(
            timestamp=now_iso, sector=sector, sub_sector="",
            score=data["score"], sentiment_avg=data.get("sentiment_avg", 0),
            news_count=data["news_count"], volume_signal=data.get("volume_signal", 0),
            velocity=data.get("velocity", 0), direction=data["direction"],
            momentum_json=json.dumps(data.get("momentum", {}), ensure_ascii=False),
            extra_json=json.dumps({
                "breakdown": data.get("breakdown", {}),
                "cross_effects": data.get("cross_effects", []),
            }, ensure_ascii=False),
        )
        # 서브섹터 스냅샷
        for sub_name, sub_data in data.get("sub_sectors", {}).items():
            if sub_data["news_count"] == 0:
                continue  # 뉴스 없는 서브섹터는 저장 안 함
            db.save_industry_snapshot(
                timestamp=now_iso, sector=sector, sub_sector=sub_name,
                score=sub_data["score"], sentiment_avg=sub_data.get("sentiment_avg", 0),
                news_count=sub_data["news_count"], volume_signal=sub_data.get("volume_signal", 0),
                velocity=sub_data.get("velocity", 0), direction=sub_data["direction"],
                momentum_json="{}",
                extra_json=json.dumps({"breakdown": sub_data.get("breakdown", {})}, ensure_ascii=False),
            )


def _update_momentum(conn: sqlite3.Connection, indices: dict):
    """저장 후 모멘텀을 실제 값으로 업데이트."""
    now = datetime.now()
    for sector, data in indices.items():
        momentum = {}
        for label, hours in [("1h", 1), ("3h", 3), ("6h", 6), ("12h", 12)]:
            target_time = (now - timedelta(hours=hours)).isoformat()
            row = conn.execute(
                "SELECT score FROM industry_index "
                "WHERE sector = ? AND sub_sector = '' AND timestamp <= ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (sector, target_time)
            ).fetchone()
            if row:
                momentum[label] = round(data["score"] - row["score"], 1)
            else:
                momentum[label] = 0.0
        data["momentum"] = momentum


# ══════════════════════════════════════════
# 공개 API
# ══════════════════════════════════════════

def refresh_industry_index() -> dict[str, dict]:
    """산업 지수 계산 + DB 스냅샷 저장 + 캐시 갱신."""
    indices = calculate_industry_index()

    # DB 스냅샷 저장
    try:
        _save_snapshots(indices)
    except Exception as e:
        logger.warning("산업 지수 스냅샷 저장 실패: %s", e)

    # 모멘텀 업데이트 (스냅샷 저장 후)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        _update_momentum(conn, indices)
        conn.close()
    except Exception as e:
        logger.warning("모멘텀 업데이트 실패: %s", e)

    # 캐시 파일 저장
    try:
        CACHE_PATH.write_text(json.dumps({
            "_updated_at": datetime.now().isoformat(),
            "indices": indices,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.warning("산업 지수 캐시 저장 실패: %s", e)

    # 오래된 스냅샷 정리
    try:
        from src.db import get_db
        get_db().purge_old_industry_index(days=7)
    except Exception:
        pass

    logger.info("산업 지수 갱신 완료: %d개 섹터", len(indices))
    return indices


def get_industry_index(use_cache: bool = True) -> dict[str, dict]:
    """산업 지수 조회 (캐시 우선)."""
    if use_cache:
        try:
            if CACHE_PATH.exists():
                data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                updated = data.get("_updated_at", "")
                if updated:
                    age = (datetime.now() - datetime.fromisoformat(updated)).total_seconds()
                    if age < CACHE_TTL:
                        return data.get("indices", {})
        except Exception:
            pass

    return refresh_industry_index()


def get_index_history(sector: str, sub_sector: str = "",
                      hours: int = 24) -> list[dict]:
    """산업 지수 시계열 조회."""
    from src.db import get_db
    return get_db().get_industry_snapshots(sector, sub_sector, hours)


# ══════════════════════════════════════════
# 포맷팅
# ══════════════════════════════════════════

def format_index_for_prompt(indices: dict) -> str:
    """봇 프롬프트용 간결 텍스트.

    예:
      [급등] 반도체/IT: +6.2 (15건) ↑1h:+1.3 ↑3h:+3.1
             AI반도체: +7.8 (8건) | HBM: +4.1 (5건)
    """
    if not indices:
        return "  (지수 데이터 없음)"

    lines = []
    # 뉴스 있는 섹터만, 점수 절대값 기준 정렬
    sorted_sectors = sorted(
        [(s, d) for s, d in indices.items() if d.get("news_count", 0) > 0],
        key=lambda x: abs(x[1].get("score", 0)), reverse=True
    )

    if not sorted_sectors:
        return "  (뉴스 수집 대기 중)"

    for sector, data in sorted_sectors:
        score = data.get("score", 0)
        count = data.get("news_count", 0)
        direction = data.get("direction", "보합")
        momentum = data.get("momentum", {})

        # 모멘텀 화살표
        mom_parts = []
        for label in ["1h", "3h"]:
            val = momentum.get(label, 0)
            if val != 0:
                arrow = "↑" if val > 0 else "↓" if val < 0 else "→"
                mom_parts.append(f"{arrow}{label}:{val:+.1f}")
        mom_str = " ".join(mom_parts)

        lines.append(f"  [{direction}] {sector}: {score:+.1f} ({count}건) {mom_str}".rstrip())

        # 서브섹터 (뉴스 있는 것만, 상위 3개)
        subs = data.get("sub_sectors", {})
        active_subs = [(n, s) for n, s in subs.items() if s.get("news_count", 0) > 0]
        active_subs.sort(key=lambda x: abs(x[1]["score"]), reverse=True)
        if active_subs:
            sub_parts = [f"{n}: {s['score']:+.1f} ({s['news_count']}건)" for n, s in active_subs[:3]]
            lines.append(f"         {' | '.join(sub_parts)}")

    # 교차 영향 요약 (뉴스 있는 섹터 전체에서)
    cross_parts = []
    for _, data in sorted(indices.items(), key=lambda x: abs(x[1].get("score", 0)), reverse=True):
        for effect in data.get("cross_effects", []):
            cross_parts.append(effect)
    if cross_parts:
        lines.append(f"  [상관] {' | '.join(cross_parts[:4])}")

    return "\n".join(lines)


def format_index_for_human(indices: dict, sub_sector_filter: str = "") -> str:
    """MCP/채팅용 상세 텍스트."""
    if not indices:
        return "산업 지수 데이터가 없습니다."

    lines = ["[산업 종합 지수 (-10~+10)]", ""]

    sorted_sectors = sorted(indices.items(), key=lambda x: abs(x[1].get("score", 0)), reverse=True)

    for sector, data in sorted_sectors:
        score = data["score"]
        direction = data["direction"]
        count = data["news_count"]
        velocity = data.get("velocity", 0)
        momentum = data.get("momentum", {})

        lines.append(f"## {sector}  [{direction}]  {score:+.1f}점  (뉴스 {count}건)")

        # 모멘텀
        mom_parts = []
        for label in ["1h", "3h", "6h", "12h"]:
            val = momentum.get(label, 0)
            if val != 0:
                arrow = "↑" if val > 0 else "↓"
                mom_parts.append(f"{label}: {val:+.1f}")
        if mom_parts:
            lines.append(f"  모멘텀: {', '.join(mom_parts)}")

        if velocity != 0:
            lines.append(f"  변화속도: {velocity:+.1f}")

        # breakdown
        bd = data.get("breakdown", {})
        if bd:
            pos = bd.get("positive", 0) + bd.get("strong_positive", 0)
            neg = bd.get("negative", 0) + bd.get("strong_negative", 0)
            neu = bd.get("neutral", 0)
            lines.append(f"  감성분포: 호재 {pos} / 악재 {neg} / 중립 {neu}")

        # 교차 영향
        for effect in data.get("cross_effects", []):
            lines.append(f"  교차영향: {effect}")

        # 서브섹터
        subs = data.get("sub_sectors", {})
        active_subs = [(n, s) for n, s in subs.items() if s.get("news_count", 0) > 0]
        if sub_sector_filter:
            active_subs = [(n, s) for n, s in active_subs if n == sub_sector_filter]
        active_subs.sort(key=lambda x: abs(x[1]["score"]), reverse=True)
        for sub_name, sub_data in active_subs:
            s = sub_data["score"]
            d = sub_data["direction"]
            c = sub_data["news_count"]
            lines.append(f"  - {sub_name}: {s:+.1f} [{d}] ({c}건)")

        lines.append("")

    return "\n".join(lines)
