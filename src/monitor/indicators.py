"""기술지표 계산 엔진.

pandas + ta 라이브러리를 사용하여 주요 기술지표를 계산한다.
실시간 Tick 데이터가 쌓이면 지표를 업데이트하는 방식.
"""

from dataclasses import dataclass, field

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator, EMAIndicator
from ta.volatility import BollingerBands


@dataclass
class IndicatorResult:
    """계산된 기술지표 결과."""
    stock_code: str
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    sma_5: float | None = None
    sma_20: float | None = None
    sma_60: float | None = None
    ema_12: float | None = None
    ema_26: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    price: float | None = None
    volume: int | None = None


class IndicatorEngine:
    """종목별 기술지표 계산기.

    실시간 체결 데이터를 받아서 DataFrame에 누적하고,
    지표를 계산한다.
    """

    def __init__(self, max_candles: int = 200):
        self._max = max_candles
        self._data: dict[str, pd.DataFrame] = {}  # stock_code -> DataFrame

    def update(self, stock_code: str, price: int, volume: int) -> IndicatorResult:
        """새 체결 데이터 추가 후 지표 계산."""
        if stock_code not in self._data:
            self._data[stock_code] = pd.DataFrame(columns=["close", "volume"])

        df = self._data[stock_code]
        new_row = pd.DataFrame([{"close": float(price), "volume": volume}])
        self._data[stock_code] = pd.concat([df, new_row], ignore_index=True).tail(self._max)
        df = self._data[stock_code]

        return self._calculate(stock_code, df)

    def load_history(self, stock_code: str, closes: list[float], volumes: list[int] | None = None):
        """과거 데이터 일괄 로드 (초기화용)."""
        data = {"close": closes}
        if volumes:
            data["volume"] = volumes
        self._data[stock_code] = pd.DataFrame(data).tail(self._max)

    def get(self, stock_code: str) -> IndicatorResult | None:
        """현재 지표 조회 (계산 없이)."""
        df = self._data.get(stock_code)
        if df is None or df.empty:
            return None
        return self._calculate(stock_code, df)

    def _calculate(self, stock_code: str, df: pd.DataFrame) -> IndicatorResult:
        """DataFrame에서 기술지표 계산."""
        result = IndicatorResult(stock_code=stock_code)
        close = df["close"]
        n = len(close)

        result.price = close.iloc[-1] if n > 0 else None
        result.volume = int(df["volume"].iloc[-1]) if "volume" in df and n > 0 else None

        # RSI (14)
        if n >= 14:
            rsi = RSIIndicator(close, window=14)
            val = rsi.rsi().iloc[-1]
            result.rsi = round(val, 2) if pd.notna(val) else None

        # MACD (12, 26, 9)
        if n >= 26:
            macd = MACD(close)
            m = macd.macd().iloc[-1]
            s = macd.macd_signal().iloc[-1]
            h = macd.macd_diff().iloc[-1]
            result.macd = round(m, 2) if pd.notna(m) else None
            result.macd_signal = round(s, 2) if pd.notna(s) else None
            result.macd_histogram = round(h, 2) if pd.notna(h) else None

        # SMA
        if n >= 5:
            result.sma_5 = round(SMAIndicator(close, window=5).sma_indicator().iloc[-1], 2)
        if n >= 20:
            result.sma_20 = round(SMAIndicator(close, window=20).sma_indicator().iloc[-1], 2)
        if n >= 60:
            result.sma_60 = round(SMAIndicator(close, window=60).sma_indicator().iloc[-1], 2)

        # EMA
        if n >= 12:
            result.ema_12 = round(EMAIndicator(close, window=12).ema_indicator().iloc[-1], 2)
        if n >= 26:
            result.ema_26 = round(EMAIndicator(close, window=26).ema_indicator().iloc[-1], 2)

        # Bollinger Bands (20, 2)
        if n >= 20:
            bb = BollingerBands(close, window=20, window_dev=2)
            result.bb_upper = round(bb.bollinger_hband().iloc[-1], 2)
            result.bb_middle = round(bb.bollinger_mavg().iloc[-1], 2)
            result.bb_lower = round(bb.bollinger_lband().iloc[-1], 2)

        return result
