"""조건 엔진 — 구조화된 조건을 평가하여 진입/청산 판단.

TemplateBot이 사용하는 핵심 엔진.
분봉 데이터에서 지표를 계산하고 조건 충족 여부를 판정.
"""

import logging
import math
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════
# 지표 계산기
# ══════════════════════════════════════════

def calc_rsi(minutes: list[dict], period: int = 14) -> float | None:
    """RSI 계산 (0~100). minutes: [{"close": int, ...}, ...]"""
    closes = [m.get("close", m.get("stck_prpr", 0)) for m in minutes]
    if len(closes) < period + 1:
        return None
    series = pd.Series(closes)
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, float('inf'))
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 2) if not math.isnan(val) else None


def calc_vol_spike(minutes: list[dict], recent: int = 5, baseline: int = 25) -> float | None:
    """거래량 폭발 배율. 최근 recent분 합 / 기준 baseline분 평균."""
    vols = [m.get("volume", m.get("cntg_vol", 0)) for m in minutes]
    if len(vols) < recent + baseline:
        return None
    recent_vol = sum(vols[-recent:])
    base_vols = vols[-(recent + baseline):-recent]
    base_avg = sum(base_vols) / len(base_vols) if base_vols else 1
    if base_avg <= 0:
        return None
    return round(recent_vol / base_avg, 2)


def calc_bb_position(minutes: list[dict], period: int = 20, std_mult: float = 2.0) -> float | None:
    """볼린저밴드 %B. 0=하단, 0.5=중간, 1=상단. 음수=하단 이탈."""
    closes = [m.get("close", m.get("stck_prpr", 0)) for m in minutes]
    if len(closes) < period:
        return None
    series = pd.Series(closes[-period:])
    sma = series.mean()
    std = series.std()
    if std == 0:
        return 0.5
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    band_width = upper - lower
    if band_width == 0:
        return 0.5
    current = closes[-1]
    pct_b = (current - lower) / band_width
    return round(float(pct_b), 4)


def calc_bb_mid(minutes: list[dict], period: int = 20) -> float | None:
    """볼린저밴드 중간선 (SMA)."""
    closes = [m.get("close", m.get("stck_prpr", 0)) for m in minutes]
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def calc_vwap(minutes: list[dict]) -> float | None:
    """VWAP 계산."""
    cum_tp_vol = 0.0
    cum_vol = 0
    for m in minutes:
        high = m.get("high", m.get("stck_hgpr", 0))
        low = m.get("low", m.get("stck_lwpr", 0))
        close = m.get("close", m.get("stck_prpr", 0))
        volume = m.get("volume", m.get("cntg_vol", 0))
        if volume <= 0:
            continue
        tp = (high + low + close) / 3
        cum_tp_vol += tp * volume
        cum_vol += volume
    if cum_vol == 0:
        return None
    return round(cum_tp_vol / cum_vol, 2)


def calc_price_above_vwap(minutes: list[dict]) -> bool | None:
    """현재가가 VWAP 위인지."""
    vwap = calc_vwap(minutes)
    if vwap is None:
        return None
    current = minutes[-1].get("close", minutes[-1].get("stck_prpr", 0))
    return current > vwap


def calc_price_change_pct(minutes: list[dict]) -> float | None:
    """최근 분봉 가격 변동률 (%)."""
    if len(minutes) < 2:
        return None
    prev = minutes[-2].get("close", minutes[-2].get("stck_prpr", 0))
    curr = minutes[-1].get("close", minutes[-1].get("stck_prpr", 0))
    if prev <= 0:
        return None
    return round((curr - prev) / prev * 100, 4)


def calc_macd_cross(minutes: list[dict]) -> str | None:
    """MACD 크로스. 'golden'=골든크로스, 'dead'=데드크로스, None=미발생."""
    closes = [m.get("close", m.get("stck_prpr", 0)) for m in minutes]
    if len(closes) < 26:
        return None
    series = pd.Series(closes)
    ema12 = series.ewm(span=12).mean()
    ema26 = series.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    if len(macd) < 2:
        return None
    prev_diff = macd.iloc[-2] - signal.iloc[-2]
    curr_diff = macd.iloc[-1] - signal.iloc[-1]
    if prev_diff <= 0 and curr_diff > 0:
        return "golden"
    if prev_diff >= 0 and curr_diff < 0:
        return "dead"
    return None


def calc_sma_cross(minutes: list[dict], short: int = 5, long: int = 20) -> str | None:
    """SMA 크로스. short기간 SMA가 long기간 SMA를 상향/하향 돌파."""
    closes = [m.get("close", m.get("stck_prpr", 0)) for m in minutes]
    if len(closes) < long + 1:
        return None
    series = pd.Series(closes)
    sma_s = series.rolling(short).mean()
    sma_l = series.rolling(long).mean()
    prev = sma_s.iloc[-2] - sma_l.iloc[-2]
    curr = sma_s.iloc[-1] - sma_l.iloc[-1]
    if prev <= 0 and curr > 0:
        return "golden"
    if prev >= 0 and curr < 0:
        return "dead"
    return None


def calc_price_vs_sma(minutes: list[dict], period: int = 20) -> float | None:
    """SMA 대비 이격도 (%). 양수=SMA 위, 음수=SMA 아래."""
    closes = [m.get("close", m.get("stck_prpr", 0)) for m in minutes]
    if len(closes) < period:
        return None
    sma = sum(closes[-period:]) / period
    if sma == 0:
        return None
    current = closes[-1]
    return round((current - sma) / sma * 100, 4)


# 지표 이름 → 계산 함수 매핑
INDICATOR_CALCULATORS = {
    "rsi": calc_rsi,
    "vol_spike": calc_vol_spike,
    "bb_position": calc_bb_position,
    "price_above_vwap": calc_price_above_vwap,
    "price_change_pct": calc_price_change_pct,
    "macd_cross": calc_macd_cross,
    "sma_cross": calc_sma_cross,
    "price_vs_sma": calc_price_vs_sma,
}

# 사용 가능한 지표 메타데이터 (UI/MCP용)
INDICATOR_META = {
    "rsi": {"label": "RSI", "type": "float", "range": "0~100", "default_period": 14},
    "vol_spike": {"label": "거래량 배율", "type": "float", "range": "0~∞"},
    "bb_position": {"label": "볼린저밴드 %B", "type": "float", "range": "-∞~+∞", "default_period": 20},
    "price_above_vwap": {"label": "VWAP 위 여부", "type": "bool"},
    "price_change_pct": {"label": "가격 변동률 (%)", "type": "float"},
    "macd_cross": {"label": "MACD 크로스", "type": "str", "values": ["golden", "dead"]},
    "sma_cross": {"label": "SMA 크로스", "type": "str", "values": ["golden", "dead"]},
    "price_vs_sma": {"label": "SMA 이격도 (%)", "type": "float", "default_period": 20},
}


# ══════════════════════════════════════════
# 조건 평가
# ══════════════════════════════════════════

OPERATORS = {
    ">=": lambda a, b: a is not None and float(a) >= float(b),
    "<=": lambda a, b: a is not None and float(a) <= float(b),
    ">": lambda a, b: a is not None and float(a) > float(b),
    "<": lambda a, b: a is not None and float(a) < float(b),
    "==": lambda a, b: a is not None and str(a).lower() == str(b).lower(),
    "!=": lambda a, b: a is not None and str(a).lower() != str(b).lower(),
}


def calc_indicator(indicator: str, minutes: list[dict], period: int = 0) -> Any:
    """지표 계산. period가 0이면 기본값 사용."""
    calc_fn = INDICATOR_CALCULATORS.get(indicator)
    if not calc_fn:
        logger.warning("알 수 없는 지표: %s", indicator)
        return None

    import inspect
    sig = inspect.signature(calc_fn)
    kwargs = {}
    if "period" in sig.parameters and period > 0:
        kwargs["period"] = period

    return calc_fn(minutes, **kwargs)


def evaluate_condition(cond: dict, minutes: list[dict]) -> tuple[bool, str]:
    """단일 조건 평가. (충족 여부, 근거 문자열)."""
    indicator = cond.get("indicator", "")
    operator = cond.get("operator", ">=")
    value = cond.get("value")
    period = cond.get("period", 0)

    actual = calc_indicator(indicator, minutes, period)
    if actual is None:
        return False, f"{indicator}: 데이터 부족"

    op_fn = OPERATORS.get(operator)
    if not op_fn:
        return False, f"알 수 없는 연산자: {operator}"

    met = op_fn(actual, value)
    meta = INDICATOR_META.get(indicator, {})
    label = meta.get("label", indicator)
    reason = f"{label} {actual} {operator} {value}" if met else ""
    return met, reason


def evaluate_conditions(conditions: list[dict], logic: str, minutes: list[dict]) -> tuple[bool, list[str]]:
    """복수 조건 평가. logic='AND' 또는 'OR'.
    Returns: (전체 충족 여부, 충족된 조건 근거 리스트)
    """
    if not conditions:
        return False, []

    results = []
    reasons = []
    for cond in conditions:
        met, reason = evaluate_condition(cond, minutes)
        results.append(met)
        if met and reason:
            reasons.append(reason)

    if logic.upper() == "AND":
        return all(results), reasons
    else:  # OR
        return any(results), reasons


def evaluate_exit(exit_spec: dict, position: dict, current_price: int, minutes: list[dict]) -> tuple[bool, str]:
    """청산 조건 평가.
    position: {"price": int, "peak_price": int}
    Returns: (청산 여부, 사유)
    """
    entry_price = position.get("price", 0)
    peak_price = position.get("peak_price", entry_price)
    if entry_price <= 0:
        return False, ""

    pnl_pct = (current_price - entry_price) / entry_price * 100
    from_peak_pct = (current_price - peak_price) / peak_price * 100 if peak_price > 0 else 0

    # 트레일링 스탑
    trailing = exit_spec.get("trailing_stop_pct", -999)
    if trailing > -999 and from_peak_pct <= trailing:
        return True, f"트레일링 스탑 ({from_peak_pct:+.2f}% from peak)"

    # 익절
    tp = exit_spec.get("take_profit_pct", 999)
    if tp < 999 and pnl_pct >= tp:
        return True, f"익절 ({pnl_pct:+.2f}%)"

    # 하드 손절
    sl = exit_spec.get("hard_stop_pct", -999)
    if sl > -999 and pnl_pct <= sl:
        return True, f"손절 ({pnl_pct:+.2f}%)"

    # 조건 기반 청산
    exit_conditions = exit_spec.get("conditions", [])
    for cond in exit_conditions:
        met, reason = evaluate_condition(cond, minutes)
        if met:
            return True, f"조건 청산: {reason}"

    return False, ""
