"""Tests for TradeEngine bot cash tracking."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.bots.engine import TradeEngine


def _engine_with_trades(budget, trades):
    eng = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=budget)
    db = MagicMock()
    db.get_trades.return_value = trades
    with patch("src.db.get_db", return_value=db):
        eng.refresh_cash()
    return eng


def test_engine_cash_initialized_to_budget_when_no_trades():
    eng = _engine_with_trades(100000, [])
    assert eng.cash == 100000


def test_engine_cash_reflects_buy_trades_only_for_this_bot():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True, "source": "test-bot"},
    ]
    eng = _engine_with_trades(100000, trades)
    assert eng.cash == 50000


def test_engine_can_afford_uses_cash_not_budget():
    """cash가 부족하면 거부. budget이 남아있어도 안 됨."""
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 4, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True, "source": "test-bot"},
    ]
    eng = _engine_with_trades(100000, trades)
    assert eng.cash == 0
    assert eng.can_afford(25000) is False
    assert eng.can_afford(0) is True


def test_engine_get_buyable_qty_uses_cash():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True, "source": "test-bot"},
    ]
    eng = _engine_with_trades(100000, trades)
    market = MagicMock()
    market.get_buyable_qty.return_value = {"max_qty": 100}
    eng._market = market
    # cash=50000, price=25000 → 2주
    assert eng.get_buyable_qty("000000", 25000) == 2


def test_reconcile_does_not_consult_pipe_holdings():
    """다른 봇이 같은 종목을 사도 본인 보유는 영향받지 않는다."""
    from types import SimpleNamespace
    from src.bots.base import BotBase, BotConfig

    class _DummyBot(BotBase):
        BOT_ID = "test-bot"
        def scan(self): return []
        def decide(self, candidates): return {}
        def execute(self, decision): return {}

    bot = object.__new__(_DummyBot)
    bot._config = BotConfig(instance_id="strat-001:test-bot", budget=100000)
    bot._holding_code = ""
    bot._holding_qty = 0
    bot._holding_price = 0
    bot._realized_pnl = 0
    bot._scan_count = 0
    bot._save_state = MagicMock()
    bot.engine = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=100000)

    db = MagicMock()
    db.get_trades.return_value = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True, "source": "test-bot"},
    ]
    # 파이프에 다른 봇이 같은 종목 5주 보유 — 봇이 StrategyManager를 들여다보면
    # 5주에 영향받음. 격리 검증: StrategyManager가 호출조차 안 되어야 한다.
    with patch("src.db.get_db", return_value=db), \
         patch("src.strategy.StrategyManager") as mgr_cls:
        bot._reconcile_with_trades()

    assert bot._holding_code == "102780"
    assert bot._holding_qty == 1  # 본인 1주
    mgr_cls.assert_not_called()  # 파이프 holdings를 조회조차 하지 않음


def test_sell_clamps_qty_to_actual_kis_balance():
    """봇이 2주 매도 시도하지만 KIS 실잔고가 1주뿐이면 1주만 매도."""
    eng = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=100000)
    market = MagicMock()
    market.get_balance.return_value = {
        "holdings": [{"stock_code": "102780", "quantity": 1}],
        "summary": {},
    }
    eng._market = market
    eng._own_ledger_qty = MagicMock(return_value=-1)  # bypass own-ledger clamp

    order = MagicMock()
    order.sell.return_value = SimpleNamespace(
        success=True, order_no="123", message="ok", price=25000, quantity=1)
    eng._order = order

    with patch.object(eng, "_log_trade"), \
         patch.object(eng, "_sync_pipe_sell"), \
         patch.object(eng, "_post_to_board"), \
         patch.object(eng, "_track_daily_loss"):
        result = eng.sell("102780", qty=2, entry_price=24000)

    # KIS에 1주만 매도 요청
    args, kwargs = order.sell.call_args
    assert kwargs["quantity"] == 1
    assert result["success"] is True
    assert result.get("clamped_qty") == 1


def test_sell_returns_failure_when_kis_balance_zero():
    """실잔고 0주면 매도 시도 안 하고 실패 반환."""
    eng = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=100000)
    market = MagicMock()
    market.get_balance.return_value = {"holdings": [], "summary": {}}
    eng._market = market

    result = eng.sell("102780", qty=1, entry_price=24000)
    assert result["success"] is False
    assert "실잔고 부족" in result["message"]


def test_sell_proceeds_unclamped_when_balance_query_fails():
    """KIS 잔고 조회 실패 시 클램프 우회 — 봇 요청 qty로 진행."""
    eng = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=100000)
    market = MagicMock()
    market.get_balance.side_effect = RuntimeError("network down")
    eng._market = market
    eng._own_ledger_qty = MagicMock(return_value=-1)

    order = MagicMock()
    order.sell.return_value = SimpleNamespace(
        success=True, order_no="x", message="ok", price=25000, quantity=2)
    eng._order = order

    with patch.object(eng, "_log_trade"), \
         patch.object(eng, "_sync_pipe_sell"), \
         patch.object(eng, "_post_to_board"), \
         patch.object(eng, "_track_daily_loss"):
        result = eng.sell("102780", qty=2, entry_price=24000)

    args, kwargs = order.sell.call_args
    assert kwargs["quantity"] == 2  # not clamped
    assert result["success"] is True


def test_botbase_clears_phantom_position_on_realbalance_zero():
    """KIS 실잔고 0이면 BotBase가 _holding_*를 clear하고 phantom_cleared로 표시."""
    from src.bots.base import BotBase, BotConfig

    class _DummyBot(BotBase):
        BOT_ID = "test-bot"
        def scan(self): return []
        def decide(self, candidates): return {}
        def execute(self, decision): return {}

    bot = object.__new__(_DummyBot)
    bot._config = BotConfig(instance_id="strat-001:test-bot", budget=100000)
    bot._holding_code = "102780"
    bot._holding_qty = 1
    bot._holding_price = 25000
    bot._holding_entry_time = None
    bot._realized_pnl = 0
    bot._scan_count = 0
    bot._save_state = MagicMock()

    eng = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=100000)
    market = MagicMock()
    market.get_balance.return_value = {"holdings": [], "summary": {}}
    eng._market = market
    bot.engine = eng

    result = bot.sell("102780", qty=1, entry_price=25000)

    assert result["success"] is False
    assert result["action"] == "phantom_cleared"
    assert bot._holding_code == ""
    assert bot._holding_qty == 0
    bot._save_state.assert_called()
