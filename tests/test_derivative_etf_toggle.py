"""파생ETF(인버스/레버리지) 거래 토글 테스트.

전역 설정(trading_mode) · 분류 헬퍼(master) · 엔진 가드 · 봇 universe 필터.
실제 data/trading_mode.json은 절대 건드리지 않는다 (MODE_FILE 격리).
"""

from unittest.mock import patch

import pytest

# 코드→이름 mock (실제 마스터/시세 조회 없이 테스트)
NAMES = {
    "114800": "KODEX 인버스",
    "122630": "KODEX 레버리지",
    "069500": "KODEX 200",
    "130680": "TIGER 원유선물",
    "252670": "KODEX 200선물인버스2X",
}


def _fake_name(code):
    return NAMES.get(code, "")


# ── 1. 전역 설정 (trading_mode) ──

def test_derivative_etf_default_off(tmp_path):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        assert tm.derivative_etf_enabled() is False


def test_set_derivative_etf_on_off(tmp_path):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        data = tm.set_derivative_etf(True)
        assert data["allow_derivative_etf"] is True
        assert tm.derivative_etf_enabled() is True
        # mode_info()에 자동 노출 (GET /api/mode에서 사용)
        assert tm.mode_info()["allow_derivative_etf"] is True

        tm.set_derivative_etf(False)
        assert tm.derivative_etf_enabled() is False


# ── 2. 분류 헬퍼 (is_derivative_etf) ──

@pytest.mark.parametrize("name,expected", [
    ("KODEX 인버스", True),
    ("KODEX 레버리지", True),
    ("KODEX 200선물인버스2X", True),
    ("TIGER 원유선물", False),     # "선물"은 계좌에서 거래 가능 — 차단 안 함
    ("KODEX 골드선물(H)", False),
    ("KODEX 200", False),
    ("", False),
    (None, False),
])
def test_is_derivative_etf(name, expected):
    from src.kis.master import is_derivative_etf
    assert is_derivative_etf(name) is expected


def test_stock_name_by_code_from_master_file(tmp_path, monkeypatch):
    """ETF_MAPPING에 없는 코드(예: TIGER 인버스 123310)도 마스터 파일로 이름 조회."""
    import json
    import src.kis.master as master_mod
    mf = tmp_path / "stock_master.json"
    mf.write_text(json.dumps({"all": [
        {"code": "123310", "name": "TIGER 인버스", "market": "KOSPI", "type": "ETF"},
        {"code": "005930", "name": "삼성전자", "market": "KOSPI", "type": "STOCK"},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(master_mod, "MASTER_FILE", mf)
    monkeypatch.setattr(master_mod, "_code_name_cache", None)
    assert master_mod.stock_name_by_code("123310") == "TIGER 인버스"
    assert master_mod.stock_name_by_code("005930") == "삼성전자"
    assert master_mod.stock_name_by_code("999999") == ""


def test_stock_name_by_code_missing_master_file(tmp_path, monkeypatch):
    import src.kis.master as master_mod
    monkeypatch.setattr(master_mod, "MASTER_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(master_mod, "_code_name_cache", None)
    assert master_mod.stock_name_by_code("123310") == ""  # 다운로드 시도 없이 빈 문자열


# ── 3. 엔진 가드 (_check_derivative_etf) ──

def _make_engine():
    from src.bots.engine import TradeEngine
    return TradeEngine(bot_id="test-bot")


def test_engine_blocks_derivative_when_off(tmp_path, monkeypatch):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.bots.engine as engine_mod
        monkeypatch.setattr(engine_mod, "stock_name_for", _fake_name)
        eng = _make_engine()
        assert "파생ETF" in eng._check_derivative_etf("114800")   # 인버스
        assert "파생ETF" in eng._check_derivative_etf("122630")   # 레버리지
        # 비파생 종목은 통과
        assert eng._check_derivative_etf("069500") == ""
        assert eng._check_derivative_etf("130680") == ""          # 원유선물
        # 이름 조회 실패 시 차단하지 않음
        assert eng._check_derivative_etf("999999") == ""


def test_engine_allows_derivative_when_on(tmp_path, monkeypatch):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        tm.set_derivative_etf(True)
        import src.bots.engine as engine_mod
        monkeypatch.setattr(engine_mod, "stock_name_for", _fake_name)
        eng = _make_engine()
        assert eng._check_derivative_etf("114800") == ""
        assert eng._check_derivative_etf("122630") == ""


def test_pre_buy_chain_includes_derivative_guard(tmp_path, monkeypatch):
    """가드 전체 비활성(기본) 상태에서도 파생ETF 차단은 동작 — 전역 설정 기반."""
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.bots.engine as engine_mod
        monkeypatch.setattr(engine_mod, "stock_name_for", _fake_name)
        eng = _make_engine()
        assert "파생ETF" in eng._pre_buy_checks("114800", 10000)
        assert eng._pre_buy_checks("069500", 10000) == ""


# ── 4. 봇 universe 필터 (_filter_derivative_etfs) ──

def _make_bot_self():
    return type("DummyBot", (), {"BOT_ID": "test-bot"})()


def test_filter_removes_derivatives_when_off(tmp_path, monkeypatch):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.bots.engine as engine_mod
        monkeypatch.setattr(engine_mod, "stock_name_for", _fake_name)
        from src.bots.base import BotBase
        codes = ["114800", "122630", "069500", "130680", "999999"]
        out = BotBase._filter_derivative_etfs(_make_bot_self(), codes)
        # 인버스/레버리지 제외, 이름 조회 실패(999999)는 보수적으로 유지
        assert out == ["069500", "130680", "999999"]


def test_filter_keeps_all_when_on(tmp_path, monkeypatch):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        tm.set_derivative_etf(True)
        import src.bots.engine as engine_mod
        monkeypatch.setattr(engine_mod, "stock_name_for", _fake_name)
        from src.bots.base import BotBase
        codes = ["114800", "122630", "069500"]
        assert BotBase._filter_derivative_etfs(_make_bot_self(), codes) == codes
