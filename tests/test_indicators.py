"""기술지표 계산 엔진 테스트."""

import pytest

from src.monitor.indicators import IndicatorEngine


@pytest.fixture
def engine():
    return IndicatorEngine()


def test_rsi_calculation(engine):
    """RSI가 14개 이상 데이터에서 계산되는지 확인."""
    # 상승 추세 데이터 (RSI > 50 예상)
    prices = [100 + i for i in range(20)]
    engine.load_history("005930", [float(p) for p in prices])
    result = engine.get("005930")
    assert result is not None
    assert result.rsi is not None
    assert 50 < result.rsi <= 100  # 지속 상승이므로 RSI > 50


def test_sma_calculation(engine):
    """SMA가 올바르게 계산되는지 확인."""
    prices = [float(i) for i in range(1, 21)]  # 1~20
    engine.load_history("005930", prices)
    result = engine.get("005930")
    assert result.sma_5 == pytest.approx(18.0)  # (16+17+18+19+20)/5
    assert result.sma_20 == pytest.approx(10.5)  # (1+...+20)/20


def test_update_accumulates_data(engine):
    """update()가 데이터를 누적하는지 확인."""
    for i in range(15):
        result = engine.update("005930", 50000 + i * 100, 1000)
    assert result.price == 51400
    assert result.rsi is not None  # 14개 이상이므로 RSI 계산 가능


def test_insufficient_data_returns_none(engine):
    """데이터 부족 시 지표가 None인지 확인."""
    engine.load_history("005930", [100.0, 200.0])
    result = engine.get("005930")
    assert result.rsi is None  # 14개 미만
    assert result.macd is None  # 26개 미만
    assert result.sma_20 is None  # 20개 미만


def test_max_candles_limit(engine):
    """max_candles 제한이 동작하는지 확인."""
    engine._max = 50
    prices = [float(i) for i in range(100)]
    engine.load_history("005930", prices)
    assert len(engine._data["005930"]) == 50


def test_bollinger_bands(engine):
    """볼린저밴드가 올바른 범위인지 확인."""
    prices = [float(100 + (i % 10)) for i in range(30)]
    engine.load_history("005930", prices)
    result = engine.get("005930")
    assert result.bb_upper is not None
    assert result.bb_lower is not None
    assert result.bb_upper > result.bb_middle > result.bb_lower
