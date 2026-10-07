"""투자 모드 안전 제어 테스트."""

from unittest.mock import patch

import pytest


def test_default_mode_is_paper(tmp_path):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        assert tm.current_mode() == "paper"


def test_disabled_blocks_orders(tmp_path):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        tm.set_disabled()
        ok, msg = tm.validate_order("005930", 100000)
        assert ok is False
        assert "비활성화" in msg


def test_max_order_amount(tmp_path):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        tm.set_paper()
        tm.set_max_order_amount(50000)
        ok, msg = tm.validate_order("005930", 100000)
        assert ok is False
        assert "초과" in msg

        ok2, _ = tm.validate_order("005930", 30000)
        assert ok2 is True


def test_live_requires_3step_confirm(tmp_path):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        # 1단계: 코드 요청
        code = tm.request_live()
        assert code.startswith("LIVE-")
        assert tm.current_mode() != "live"  # 아직 전환 안 됨

        # 잘못된 코드
        ok, msg = tm.confirm_live("WRONG")
        assert ok is False

        # 정확한 코드
        ok, msg = tm.confirm_live(code)
        assert ok is True
        assert tm.current_mode() == "live"


def test_paper_switch_is_immediate(tmp_path):
    with patch("src.trading_mode.MODE_FILE", tmp_path / "mode.json"), \
         patch("src.trading_mode.DecisionLog"):
        import src.trading_mode as tm
        tm.set_disabled()
        assert tm.current_mode() == "disabled"
        tm.set_paper()
        assert tm.current_mode() == "paper"  # 즉시 전환
