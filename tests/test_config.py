"""config 모듈 테스트."""

import os

import pytest


def test_kis_config_base_url():
    os.environ.update({
        "KIS_APP_KEY": "test_key",
        "KIS_APP_SECRET": "test_secret",
        "KIS_ACCOUNT_NO": "50012345-01",
        "KIS_IS_PAPER": "true",
    })
    from src.config import KISConfig

    cfg = KISConfig()
    assert "openapivts" in cfg.base_url  # 모의투자 URL
    assert cfg.account_prefix == "50012345"
    assert cfg.account_suffix == "01"


def test_kis_config_real_url():
    os.environ.update({
        "KIS_APP_KEY": "test_key",
        "KIS_APP_SECRET": "test_secret",
        "KIS_ACCOUNT_NO": "50012345-01",
        "KIS_IS_PAPER": "false",
    })
    from src.config import KISConfig

    cfg = KISConfig()
    assert "openapi.koreainvestment" in cfg.base_url  # 실전투자 URL
