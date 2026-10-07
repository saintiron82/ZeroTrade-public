"""미리내 공통 봇 보호 장치 테스트 — 연속 손절 차단 + 종목 블록리스트.

2026-06-10 mirinae-3 실거래 사고(130680 손절 → 당일 재매수 → 재손절) 재발 방지.
연속 손절 차단(`consecutive_loss_block`)과 종목 블록리스트(`symbol_blocklist`)는
부모 MirinaeBot에서 일반화되어 mirinae-2/mirinae-3이 공유한다.
"""

from unittest.mock import MagicMock, patch

from src.bots.base import BotConfig
from src.bots.mirinae import DEFAULT_PARAMS, MirinaeBot
from src.bots.mirinae2 import SCALP_PARAMS, Mirinae2Bot
from src.bots.mirinae3 import SWING_PARAMS
from src.bots.safeguards import kst_now
from src.db import DB


def _bare_bot(params: dict | None = None) -> MirinaeBot:
    bot = object.__new__(MirinaeBot)
    bot.BOT_ID = "mirinae"
    bot._config = BotConfig(
        instance_id="strat-001:mirinae",
        etf_codes=["069500", "130680"],
        interval_seconds=1,
        budget=100000,
    )
    bot._params = {**DEFAULT_PARAMS, **(params or {})}
    bot._scan_count = 10
    bot._last_ai_error = ""
    bot._last_preflight = {}
    bot._last_analysis = {}
    bot._last_raw_output_preview = ""
    bot._last_ai_call_meta = {}
    bot._last_collected_data = {}
    bot._last_position_reviews = []
    bot._last_preflight_board_key = ""
    bot._debate_log = []
    bot._trade_log = []
    bot._realized_pnl = 0
    bot._positions = []
    bot._daily_limit = MagicMock()
    return bot


def _position(code: str, qty: int = 2, price: int = 10000) -> dict:
    hold_timer = MagicMock()
    hold_timer.can_sell.return_value = True
    return {
        "code": code,
        "name": code,
        "qty": qty,
        "price": price,
        "target_price": 0,
        "stop_loss": 0,
        "hold_timer": hold_timer,
        "trailing_stop": MagicMock(),
    }


def _run_exit(bot: MirinaeBot, code: str, pnl: int) -> dict:
    """포지션을 1개 세팅하고 자동 청산 경로(_execute_position_exit)를 실행."""
    bot._positions = [_position(code)]
    bot.sell = MagicMock(return_value={
        "success": True,
        "trade_id": 1,
        "sell_price": 9000,
        "realized_pnl": pnl,
        "sell_basis": {"entry_price": 10000, "quantity": 2},
    })
    return bot._execute_position_exit({
        "action": "sell", "code": code, "qty": 2, "reason": "손절",
    })


def _today() -> str:
    return kst_now().strftime("%Y-%m-%d")


# ===========================================
# B-1. 연속 손절 차단
# ===========================================

def test_two_consecutive_loss_exits_block_same_symbol_buy():
    bot = _bare_bot()
    _run_exit(bot, "069500", pnl=-5130)
    assert bot._daily_losses.get("069500") == 1
    _run_exit(bot, "069500", pnl=-1425)
    assert bot._daily_losses.get("069500") == 2

    result = bot._execute_decision({
        "decision": "BUY", "code": "069500", "name": "KODEX 200",
        "trader_reason": "재진입",
    })
    assert result["success"] is False
    assert result["reason"] == "연속 손절 차단: 069500 당일 2연속 손실"


def test_single_loss_does_not_block_buy():
    bot = _bare_bot()
    _run_exit(bot, "069500", pnl=-5130)
    assert bot._buy_block_reason("069500") == ""


def test_profit_sell_resets_loss_counter():
    bot = _bare_bot()
    _run_exit(bot, "069500", pnl=-5130)
    _run_exit(bot, "069500", pnl=+3000)
    assert bot._daily_losses.get("069500", 0) == 0
    assert bot._buy_block_reason("069500") == ""


def test_date_change_resets_loss_counts():
    bot = _bare_bot()
    bot._daily_losses = {"069500": 2}
    bot._daily_losses_date = "2026-01-01"  # 과거 날짜
    assert bot._buy_block_reason("069500") == ""
    assert bot._daily_losses == {}
    assert bot._daily_losses_date == _today()


def test_consecutive_loss_block_zero_disables_check():
    bot = _bare_bot({"consecutive_loss_block": 0})
    bot._daily_losses = {"069500": 5}
    bot._daily_losses_date = _today()
    assert bot._buy_block_reason("069500") == ""


def test_ai_sell_loss_also_increments_counter():
    bot = _bare_bot()
    bot._positions = [_position("069500")]
    bot.sell = MagicMock(return_value={
        "success": True,
        "trade_id": 2,
        "sell_price": 9800,
        "realized_pnl": -400,
        "sell_basis": {"entry_price": 10000, "quantity": 2},
    })
    result = bot._execute_decision({
        "decision": "SELL", "code": "069500", "trader_reason": "손절",
    })
    assert result["success"] is True
    assert bot._daily_losses.get("069500") == 1


def test_blocked_buy_takes_existing_blocked_decision_path():
    """차단 결과(success=False + reason)가 _loop의 blocked_buy 기록 경로로 흘러간다."""
    bot = _bare_bot()
    bot._daily_losses = {"069500": 2}
    bot._daily_losses_date = _today()
    result = bot._execute_decision({
        "decision": "BUY", "code": "069500", "trader_reason": "재진입",
    })
    # _loop는 success=False인 buy를 action="blocked_buy"로 변환해 decisions에 기록한다.
    assert result["success"] is False
    assert "연속 손절 차단" in result["reason"]


def test_consecutive_loss_state_survives_restart(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    bot._daily_losses = {"130680": 2}
    bot._daily_losses_date = _today()
    with patch("src.db.get_db", return_value=db):
        bot._save_state()

    bot2 = _bare_bot()
    with patch("src.db.get_db", return_value=db):
        bot2._restore_positions()
    assert bot2._daily_losses == {"130680": 2}
    assert bot2._daily_losses_date == _today()
    assert bot2._buy_block_reason("130680").startswith("연속 손절 차단")
    db.close()


def test_child_bots_inherit_default_block_threshold():
    assert SCALP_PARAMS["consecutive_loss_block"] == 2
    assert SWING_PARAMS["consecutive_loss_block"] == 2


def test_mirinae2_blocks_after_two_losses_same_as_before():
    """mirinae-2 기존 동작(2연속 손절 시 당일 재진입 금지) 회귀 확인."""
    bot = object.__new__(Mirinae2Bot)
    bot.BOT_ID = "mirinae-2"
    bot._config = BotConfig(instance_id="strat-001:mirinae-2", interval_seconds=1)
    bot._params = {**SCALP_PARAMS}
    bot._positions = []
    bot._daily_limit = MagicMock()
    bot._no_entry = False
    bot._last_loss_time = {}
    bot._daily_losses = {"069500": 2}
    bot._daily_losses_date = _today()

    result = bot._execute_decision({
        "decision": "BUY", "code": "069500", "trader_reason": "재진입",
    })
    assert result["success"] is False
    assert "연속 손절 차단" in result["reason"]


# ===========================================
# B-2. 종목 블록리스트
# ===========================================

def test_blocklist_blocks_buy_decision():
    bot = _bare_bot({"symbol_blocklist": ["130680"]})
    result = bot._execute_decision({
        "decision": "BUY", "code": "130680", "name": "TIGER 원유선물",
        "trader_reason": "유가 반등",
    })
    assert result["success"] is False
    assert result["reason"] == "블록리스트 종목: 130680"


def test_blocklist_does_not_block_sell():
    bot = _bare_bot({"symbol_blocklist": ["130680"]})
    bot._positions = [_position("130680")]
    bot.sell = MagicMock(return_value={
        "success": True,
        "trade_id": 3,
        "sell_price": 10500,
        "realized_pnl": 1000,
        "sell_basis": {"entry_price": 10000, "quantity": 2},
    })
    result = bot._execute_decision({
        "decision": "SELL", "code": "130680", "trader_reason": "청산",
    })
    assert result["success"] is True


def _preflight_ready_bot(params: dict | None = None) -> MirinaeBot:
    bot = _bare_bot(params)
    bot._filter_derivative_etfs = lambda codes: codes
    market = MagicMock()
    market.get_price.side_effect = lambda code: {"code": code, "name": code, "price": 10000}
    market.get_buyable_qty.return_value = {"max_qty": 5, "max_amount": 50000, "cash": 100000}
    bot._get_market = MagicMock(return_value=market)
    bot.engine = MagicMock()
    bot.engine._pre_buy_checks.return_value = ""
    return bot


def test_blocklist_excluded_from_preflight_candidates():
    bot = _preflight_ready_bot({"symbol_blocklist": ["130680"]})
    with (
        patch("src.trading_mode.current_mode", return_value="paper"),
        patch("src.trading_mode.max_order_amount", return_value=100000),
    ):
        preflight = bot._domestic_buy_preflight()
    codes = [c["code"] for c in preflight["buyable_candidates"]]
    assert "069500" in codes
    assert "130680" not in codes


def test_consecutive_loss_symbol_marked_blocked_in_preflight():
    bot = _preflight_ready_bot()
    bot._daily_losses = {"069500": 2}
    bot._daily_losses_date = _today()
    with (
        patch("src.trading_mode.current_mode", return_value="paper"),
        patch("src.trading_mode.max_order_amount", return_value=100000),
    ):
        preflight = bot._domestic_buy_preflight()
    buyable = [c["code"] for c in preflight["buyable_candidates"]]
    assert "069500" not in buyable
    blocked = {c["code"]: c["reason"] for c in preflight["blocked_candidates"]}
    assert blocked.get("069500", "").startswith("연속 손절 차단")
