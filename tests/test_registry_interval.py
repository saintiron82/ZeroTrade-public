"""interval_spec — 봇별 분석 주기 범위/기본값 조회."""

from src.bots import registry


def test_interval_spec_for_registered_bot():
    spec = registry.interval_spec("mirinae-2")
    assert spec == {"min": 300, "max": 1800, "default": 600}


def test_interval_spec_for_stealth_volume():
    spec = registry.interval_spec("stealth-volume")
    assert spec == {"min": 10, "max": 300, "default": 30}


def test_interval_spec_none_for_unregistered_interval_bot():
    # analyst: interval_seconds BotParam 미등록 → None
    assert registry.interval_spec("analyst") is None


def test_interval_spec_none_for_unknown_bot():
    assert registry.interval_spec("does-not-exist") is None
