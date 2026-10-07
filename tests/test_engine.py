"""Tests for TradeEngine — centralized trading engine."""

from datetime import datetime
from unittest.mock import ANY, MagicMock, patch
import pytest

from src.bots.engine import TradeEngine
from src.kis.order import OrderResult, OrderType
from src.bots.base import BotBase, BotConfig


@pytest.fixture
def engine():
    """Create a TradeEngine with mocked dependencies."""
    e = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=500_000)
    # Prevent lazy init from hitting real KIS APIs
    e._order = MagicMock()
    e._market = MagicMock()
    # Default: KIS 잔고 + own ledger 조회를 우회하여 봇이 요청한 qty 그대로 진행 (-1 sentinel)
    e._kis_balance_qty = MagicMock(return_value=-1)
    e._own_ledger_qty = MagicMock(return_value=-1)
    return e


@pytest.fixture
def mock_db():
    """Patch get_db() used in deferred imports inside engine."""
    with patch("src.db.get_db") as m:
        db = MagicMock()
        db.log_trade.return_value = 42
        db.post_board.return_value = 1
        m.return_value = db
        yield db


@pytest.fixture
def mock_strategy(engine):
    """Inject a mock StrategyManager into engine's cached field."""
    mgr = MagicMock()
    engine._strategy_mgr = mgr
    return mgr


@pytest.fixture
def mock_trade_logger():
    """Patch TradeLogger used in deferred import inside engine."""
    with patch("src.trade.logger.TradeLogger") as m:
        tl = MagicMock()
        m.return_value = tl
        yield tl


# ═══════════════════════════════════════════
# pipe_id property
# ═══════════════════════════════════════════

class TestPipeId:
    def test_pipe_id_from_instance_id(self):
        e = TradeEngine("bot", instance_id="strat-001:bot")
        assert e.pipe_id == "strat-001"

    def test_pipe_id_empty_when_no_colon(self):
        e = TradeEngine("bot", instance_id="no-colon")
        assert e.pipe_id == ""

    def test_pipe_id_empty_when_no_instance_id(self):
        e = TradeEngine("bot")
        assert e.pipe_id == ""


# ═══════════════════════════════════════════
# sell() tests
# ═══════════════════════════════════════════

class TestSell:
    def test_sell_clamped_to_own_ledger_not_kis_pool(
        self, engine, mock_db, mock_strategy, mock_trade_logger
    ):
        """봇 독립 원칙: 봇은 자기 ledger 보유분까지만 매도. KIS에 다른 봇 주식이
        있어도 그 분량까지 손대지 않는다.

        시나리오: KIS 102780 보유 5주(자기 2주 + 다른 봇 3주). 봇이 5주 매도
        요청해도 자기 ledger 2주로 클램프되어 2주만 매도해야 한다.
        """
        engine._kis_balance_qty = MagicMock(return_value=5)
        engine._own_ledger_qty = MagicMock(return_value=2)
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD-OWN", message="ok",
            stock_code="102780", side="sell", quantity=2, price=26000,
        )

        engine.sell("102780", qty=5, entry_price=26000)

        # 주문은 2주(자기 ledger)로만 나가야 함
        engine._order.sell.assert_called_once()
        kwargs = engine._order.sell.call_args.kwargs
        assert kwargs["quantity"] == 2

    def test_sell_blocked_when_own_ledger_zero(
        self, engine, mock_db, mock_strategy, mock_trade_logger
    ):
        """자기 ledger 보유 0주면 매도 차단. KIS에 다른 봇 주식이 있어도 무시."""
        engine._kis_balance_qty = MagicMock(return_value=5)  # 다른 봇 보유분
        engine._own_ledger_qty = MagicMock(return_value=0)

        result = engine.sell("102780", qty=1, entry_price=26000)

        assert result["success"] is False
        assert "자기 ledger" in result["message"] or "ledger" in result["message"]
        engine._order.sell.assert_not_called()

    def test_sell_market_order(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """sell with MARKET — PnL, DB, board, pipe sync all called."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD001", message="ok",
            stock_code="005930", side="sell", quantity=10, price=75000,
        )

        result = engine.sell("005930", 10, entry_price=70000, order_type=OrderType.MARKET)

        assert result["success"] is True
        assert result["order_no"] == "ORD001"
        assert result["sell_price"] == 75000
        assert result["realized_pnl"] == 50000  # (75000-70000)*10
        assert engine.realized_pnl == 50000

        # Verify order called with correct order_type
        engine._order.sell.assert_called_once_with(
            stock_code="005930", quantity=10, order_type=OrderType.MARKET,
        )
        # DB log
        mock_db.log_trade.assert_called_once()
        # Board
        mock_db.post_board.assert_called_once()
        # post_board(pipe_id, bot_id, type, content, code)
        call_args = mock_db.post_board.call_args[0]
        assert call_args[2] == "trade"  # type is 3rd positional arg
        # Pipe sync
        mock_strategy.record_sell.assert_called_once_with("strat-001", "005930", 10, 75000)
        # TradeLogger
        mock_trade_logger.log_trade.assert_called_once()

    def test_sell_after_hours_close(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """sell with AFTER_HOURS_CLOSE — order_type passed through."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD002", message="ok",
            stock_code="069500", side="sell", quantity=5, price=35000,
        )

        result = engine.sell("069500", 5, entry_price=34000, order_type=OrderType.AFTER_HOURS_CLOSE)

        assert result["success"] is True
        assert result["sell_price"] == 35000
        assert result["realized_pnl"] == 5000  # (35000-34000)*5

        engine._order.sell.assert_called_once_with(
            stock_code="069500", quantity=5, order_type=OrderType.AFTER_HOURS_CLOSE,
        )

    def test_sell_persists_reason_and_basis(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """sell records the decision-time prices used by the bot."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD-BASIS", message="ok",
            stock_code="005930", side="sell", quantity=2, price=69000,
        )

        result = engine.sell(
            "005930",
            2,
            entry_price=70000,
            reason="손절 조건 도달",
            basis={"current_price": 69100, "target_price": 73000, "stop_loss": 69500},
        )

        assert result["sell_basis"]["entry_price"] == 70000
        assert result["sell_basis"]["current_price"] == 69100
        assert result["sell_basis"]["sell_price"] == 69000
        reason = mock_db.log_trade.call_args.kwargs["reason"]
        assert "사유:손절 조건 도달" in reason
        assert "진입:70,000" in reason
        assert "판단현재:69,100" in reason
        assert "체결:69,000" in reason
        assert "목표:73,000" in reason
        assert "손절:69,500" in reason

    def test_sell_returns_trade_id_from_db(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """successful sell returns the DB trade id."""
        mock_db.log_trade.return_value = 77
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD-TRADE-ID", message="ok",
            stock_code="005930", side="sell", quantity=1, price=75000,
        )

        result = engine.sell("005930", 1, entry_price=70000, order_type=OrderType.MARKET)

        assert result["success"] is True
        assert result["trade_id"] == 77

    def test_sell_posts_full_reason_to_board(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """Board trade posts should keep the full sell rationale."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD-FULL", message="ok",
            stock_code="005930", side="sell", quantity=2, price=69000,
        )
        long_reason = "목표 도달 후 절반 익절. " * 12

        engine.sell("005930", 2, entry_price=70000, reason=long_reason)

        board_content = mock_db.post_board.call_args[0][3]
        assert long_reason in board_content
        assert len(board_content) > 100

    def test_sell_entry_price_zero_no_pnl(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """sell with entry_price=0 → realized_pnl=0."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD003", message="ok",
            stock_code="005930", side="sell", quantity=10, price=75000,
        )

        result = engine.sell("005930", 10, entry_price=0)

        assert result["success"] is True
        assert result["realized_pnl"] == 0
        assert engine.realized_pnl == 0

    def test_sell_failure_no_side_effects(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """sell failure → no DB, no board, no sync."""
        engine._order.sell.return_value = OrderResult(
            success=False, order_no="", message="주문 실패",
            stock_code="005930", side="sell",
        )

        result = engine.sell("005930", 10, entry_price=70000)

        assert result["success"] is False
        assert result["sell_price"] == 0
        assert result["realized_pnl"] == 0
        assert engine.realized_pnl == 0

        mock_db.log_trade.assert_not_called()
        mock_db.post_board.assert_not_called()
        mock_strategy.record_sell.assert_not_called()
        mock_trade_logger.log_trade.assert_not_called()

    def test_sell_price_fallback_to_market(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """sell price=0 in result → fallback to market price."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD004", message="ok",
            stock_code="005930", side="sell", quantity=10, price=0,
        )
        engine._market.get_price.return_value = {"price": 72000}

        result = engine.sell("005930", 10, entry_price=70000)

        assert result["sell_price"] == 72000
        assert result["realized_pnl"] == 20000  # (72000-70000)*10

    def test_sell_price_fallback_to_entry_price(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """sell price=0, market also fails → fallback to entry_price."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD005", message="ok",
            stock_code="005930", side="sell", quantity=10, price=0,
        )
        engine._market.get_price.side_effect = Exception("API error")

        result = engine.sell("005930", 10, entry_price=70000)

        assert result["sell_price"] == 70000
        assert result["realized_pnl"] == 0  # (70000-70000)*10 = 0

    def test_sell_negative_pnl(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """sell at a loss → negative PnL."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD006", message="ok",
            stock_code="005930", side="sell", quantity=10, price=65000,
        )

        result = engine.sell("005930", 10, entry_price=70000)

        assert result["realized_pnl"] == -50000  # (65000-70000)*10
        assert engine.realized_pnl == -50000

    def test_sell_pnl_accumulation(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """Multiple sells accumulate realized_pnl."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD007", message="ok",
            stock_code="005930", side="sell", quantity=10, price=75000,
        )

        engine.sell("005930", 10, entry_price=70000)  # +50000
        assert engine.realized_pnl == 50000

        engine._order.sell.return_value = OrderResult(
            success=True, order_no="ORD008", message="ok",
            stock_code="005930", side="sell", quantity=5, price=68000,
        )
        engine.sell("005930", 5, entry_price=70000)  # -10000
        assert engine.realized_pnl == 40000  # 50000 + (-10000)


# ═══════════════════════════════════════════
# buy() tests
# ═══════════════════════════════════════════

class TestBuy:
    def test_buy_limit_order(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """buy with price > 0 → LIMIT order."""
        engine._order.buy.return_value = OrderResult(
            success=True, order_no="BUY001", message="ok",
            stock_code="005930", side="buy", quantity=5, price=72000,
        )

        result = engine.buy("005930", 5, price=72000)

        assert result["success"] is True
        assert result["order_no"] == "BUY001"
        engine._order.buy.assert_called_once_with(
            stock_code="005930", quantity=5, price=72000, order_type=OrderType.LIMIT,
        )
        mock_db.log_trade.assert_called_once()
        mock_db.post_board.assert_called_once()
        # 종목명은 ETF_MAPPING→마스터 파일 순으로 조회 — 환경에 따라 ""일 수 있어 ANY
        mock_strategy.record_buy.assert_called_once_with("strat-001", "005930", ANY, 5, 72000)

    def test_buy_returns_trade_id_from_db(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """successful buy returns the DB trade id."""
        engine._order.buy.return_value = OrderResult(
            success=True, order_no="BUY-TRADE-ID", message="ok",
            stock_code="005930", side="buy", quantity=1, price=72000,
        )

        result = engine.buy("005930", 1, price=72000)

        assert result["success"] is True
        assert result["trade_id"] == 42

    def test_buy_market_order(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """buy with price=0 → MARKET order."""
        engine._order.buy.return_value = OrderResult(
            success=True, order_no="BUY002", message="ok",
            stock_code="005930", side="buy", quantity=5, price=0,
        )

        result = engine.buy("005930", 5, price=0)

        assert result["success"] is True
        engine._order.buy.assert_called_once_with(
            stock_code="005930", quantity=5, price=0, order_type=OrderType.MARKET,
        )

    def test_buy_budget_exceeded(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """buy exceeding cash → rejected without placing order."""
        # cash=500,000 (initialized from budget), trying to buy 10 @ 60,000 = 600,000
        result = engine.buy("005930", 10, price=60000)

        assert result["success"] is False
        assert "현금 부족" in result["message"]
        engine._order.buy.assert_not_called()
        mock_db.log_trade.assert_not_called()
        mock_db.post_board.assert_not_called()

    def test_buy_budget_zero_means_unlimited(self, mock_db, mock_strategy, mock_trade_logger):
        """budget=0 means no budget limit."""
        e = TradeEngine("test-bot", instance_id="strat-001:test-bot", budget=0)
        e._order = MagicMock()
        e._market = MagicMock()
        e._order.buy.return_value = OrderResult(
            success=True, order_no="BUY003", message="ok",
            stock_code="005930", side="buy", quantity=100, price=80000,
        )

        with patch("src.trading_mode.validate_order", return_value=(True, "")):
            result = e.buy("005930", 100, price=80000)
        assert result["success"] is True

    def test_buy_budget_check_uses_max_price_1(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """buy with price=0 uses max(price, 1) for budget check."""
        # budget=500,000, qty=600,000, price=0 → 600000 * max(0,1) = 600000 > 500000
        result = engine.buy("005930", 600000, price=0)
        assert result["success"] is False

    def test_buy_failure_returns_correct_dict(self, engine, mock_db, mock_strategy, mock_trade_logger):
        """buy failure → correct dict shape, no side effects."""
        engine._order.buy.return_value = OrderResult(
            success=False, order_no="", message="주문 실패",
            stock_code="005930", side="buy",
        )

        result = engine.buy("005930", 5, price=72000)

        assert result["success"] is False
        assert "order_no" in result
        assert "message" in result
        mock_db.log_trade.assert_not_called()
        mock_db.post_board.assert_not_called()
        mock_strategy.record_buy.assert_not_called()


# ═══════════════════════════════════════════
# get_buyable_qty() tests
# ═══════════════════════════════════════════

class TestGetBuyableQty:
    def test_constrained_by_budget(self, engine):
        """Budget constrains qty below market max."""
        engine._market.get_buyable_qty.return_value = {"max_qty": 100}
        # budget=500,000, price=60,000 → budget_qty = 8
        qty = engine.get_buyable_qty("005930", 60000)
        assert qty == 8

    def test_constrained_by_market(self, engine):
        """Market max constrains qty below budget."""
        engine._market.get_buyable_qty.return_value = {"max_qty": 3}
        # budget=500,000, price=60,000 → budget_qty = 8, market = 3
        qty = engine.get_buyable_qty("005930", 60000)
        assert qty == 3

    def test_no_budget_uses_market_max(self):
        """budget=0 → use market max only."""
        e = TradeEngine("test-bot", budget=0)
        e._market = MagicMock()
        e._market.get_buyable_qty.return_value = {"max_qty": 50}
        qty = e.get_buyable_qty("005930", 60000)
        assert qty == 50


# ═══════════════════════════════════════════
# close() tests
# ═══════════════════════════════════════════

class TestClose:
    def test_close_cleans_up(self, engine):
        """close() calls close on market and order, then nulls them."""
        market_mock = engine._market
        order_mock = engine._order
        engine.close()
        market_mock.close.assert_called_once()
        order_mock.close.assert_called_once()
        assert engine._market is None
        assert engine._order is None

    def test_close_when_not_initialized(self):
        """close() is safe when market/order are None."""
        e = TradeEngine("bot")
        e.close()  # Should not raise


# ═══════════════════════════════════════════
# Return dict shape tests
# ═══════════════════════════════════════════

class TestReturnShape:
    def test_buy_success_shape(self, engine, mock_db, mock_strategy, mock_trade_logger):
        engine._order.buy.return_value = OrderResult(
            success=True, order_no="X", message="m",
            stock_code="005930", side="buy", quantity=1, price=1,
        )
        result = engine.buy("005930", 1, 1)
        assert set(result.keys()) == {"success", "order_no", "message", "trade_id"}

    def test_sell_success_shape(self, engine, mock_db, mock_strategy, mock_trade_logger):
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="X", message="m",
            stock_code="005930", side="sell", quantity=1, price=100,
        )
        result = engine.sell("005930", 1, entry_price=100)
        assert set(result.keys()) == {"success", "order_no", "message", "trade_id", "sell_price", "realized_pnl", "sell_basis", "clamped_qty"}

    def test_sell_failure_shape(self, engine, mock_db, mock_strategy, mock_trade_logger):
        engine._order.sell.return_value = OrderResult(
            success=False, order_no="", message="fail",
            stock_code="005930", side="sell",
        )
        result = engine.sell("005930", 1, entry_price=100)
        assert set(result.keys()) == {"success", "order_no", "message", "sell_price", "realized_pnl"}
        assert result["sell_price"] == 0
        assert result["realized_pnl"] == 0


# ═══════════════════════════════════════════
# BotBase → TradeEngine integration tests
# ═══════════════════════════════════════════

class _TestBot(BotBase):
    """Concrete BotBase subclass for testing."""
    BOT_ID = "test-bot"

    def scan(self):
        return []

    def decide(self, candidates):
        return {"action": "hold"}

    def execute(self, decision):
        return {"success": False}


@pytest.fixture
def bot():
    """Create a _TestBot with mocked DB and engine dependencies."""
    with patch("src.db.get_db") as mock_get_db:
        db = MagicMock()
        db.load_bot_state.return_value = None
        db.save_bot_state.return_value = None
        db.log_trade.return_value = 42
        db.post_board.return_value = 1
        mock_get_db.return_value = db

        config = BotConfig(budget=500_000, instance_id="strat-001:test-bot")
        b = _TestBot(config)
        # Inject mocks into engine to prevent real API calls
        b.engine._order = MagicMock()
        b.engine._market = MagicMock()
        b.engine._strategy_mgr = MagicMock()
        # Default: KIS 잔고 + own ledger 조회를 우회 (-1 sentinel)
        b.engine._kis_balance_qty = MagicMock(return_value=-1)
        b.engine._own_ledger_qty = MagicMock(return_value=-1)
        yield b


class TestBotBaseIntegration:
    """BotBase delegates to TradeEngine."""

    def test_base_buy_delegates_to_engine(self, bot, mock_db, mock_trade_logger):
        """base.buy() uses engine.buy()."""
        bot.engine._order.buy.return_value = OrderResult(
            success=True, order_no="BUY100", message="ok",
            stock_code="005930", side="buy", quantity=5, price=72000,
        )

        result = bot.buy("005930", 5, price=72000)

        assert result["success"] is True
        assert result["order_no"] == "BUY100"
        # Holdings updated
        assert bot._holding_code == "005930"
        assert bot._holding_qty == 5
        assert bot._holding_price == 72000
        # Engine order was called
        bot.engine._order.buy.assert_called_once_with(
            stock_code="005930", quantity=5, price=72000, order_type=OrderType.LIMIT,
        )

    def test_base_sell_delegates_to_engine(self, bot, mock_db, mock_trade_logger):
        """base.sell() uses engine.sell() and returns realized_pnl."""
        bot._holding_code = "005930"
        bot._holding_qty = 10
        bot._holding_price = 70000

        bot.engine._order.sell.return_value = OrderResult(
            success=True, order_no="SELL100", message="ok",
            stock_code="005930", side="sell", quantity=10, price=75000,
        )

        result = bot.sell()

        assert result["success"] is True
        assert result["realized_pnl"] == 50000  # (75000-70000)*10
        assert bot._realized_pnl == 50000
        # Holdings cleared (entry_price == _holding_price)
        assert bot._holding_code == ""
        assert bot._holding_qty == 0
        assert bot._holding_price == 0

    def test_base_sell_with_order_type(self, bot, mock_db, mock_trade_logger):
        """base.sell(order_type=AFTER_HOURS_CLOSE) passes through to engine."""
        bot._holding_code = "069500"
        bot._holding_qty = 5
        bot._holding_price = 34000

        bot.engine._order.sell.return_value = OrderResult(
            success=True, order_no="SELL101", message="ok",
            stock_code="069500", side="sell", quantity=5, price=35000,
        )

        result = bot.sell(order_type=OrderType.AFTER_HOURS_CLOSE)

        assert result["success"] is True
        assert result["sell_price"] == 35000
        # Verify engine received the order_type
        bot.engine._order.sell.assert_called_once_with(
            stock_code="069500", quantity=5, order_type=OrderType.AFTER_HOURS_CLOSE,
        )

    def test_base_sell_multi_position_keeps_other_holdings(self, bot, mock_db, mock_trade_logger):
        """When entry_price differs from _holding_price, don't clear holdings."""
        bot._holding_code = "005930"
        bot._holding_qty = 10
        bot._holding_price = 15000

        bot.engine._order.sell.return_value = OrderResult(
            success=True, order_no="SELL102", message="ok",
            stock_code="005930", side="sell", quantity=3, price=16000,
        )

        # entry_price=14000 differs from _holding_price=15000
        result = bot.sell(code="005930", qty=3, entry_price=14000)

        assert result["success"] is True
        assert result["realized_pnl"] == 6000  # (16000-14000)*3
        # Holdings NOT cleared because entry_price != _holding_price
        assert bot._holding_code == "005930"
        assert bot._holding_qty == 10
        assert bot._holding_price == 15000

    def test_base_buy_failure_no_state_change(self, bot, mock_db, mock_trade_logger):
        """buy failure does not update holdings or save state."""
        bot.engine._order.buy.return_value = OrderResult(
            success=False, order_no="", message="주문 실패",
            stock_code="005930", side="buy",
        )

        result = bot.buy("005930", 5, price=72000)

        assert result["success"] is False
        assert bot._holding_code == ""
        assert bot._holding_qty == 0


# ═══════════════════════════════════════════
# Pre-buy guards tests
# ═══════════════════════════════════════════

class TestPreBuyGuards:
    """engine.buy() pre-flight guard tests."""

    def test_daily_loss_blocks_buy(self, engine, mock_db, mock_trade_logger):
        """일일 손실 한도 도달 시 매수 차단."""
        engine._guard["daily_loss_limit_pct"] = -2
        engine.budget = 500_000
        engine._daily_loss = -10001  # exceeds -10000 (2% of 500k)
        engine._daily_loss_date = datetime.now().date().isoformat()

        engine._order.buy.return_value = OrderResult(
            success=True, order_no="X", stock_code="A", side="buy", quantity=1, price=100,
        )
        result = engine.buy("A", 1, 100)
        assert result["success"] is False
        assert "손실 한도" in result["message"]
        engine._order.buy.assert_not_called()

    def test_daily_loss_resets_new_day(self, engine):
        """날짜 변경 시 리셋."""
        engine._guard["daily_loss_limit_pct"] = -2
        engine.budget = 500_000
        engine._daily_loss = -99999
        engine._daily_loss_date = "2020-01-01"  # old date
        assert engine._check_daily_loss_limit() == ""

    def test_daily_loss_disabled_default(self, engine):
        """기본값(0)이면 비활성."""
        engine._daily_loss = -999999
        assert engine._check_daily_loss_limit() == ""

    def test_market_index_blocks_crash(self, engine):
        """KOSPI -1.5% 시 차단."""
        engine._guard["market_drop_block_pct"] = -1.0
        engine._market.get_price.return_value = {"change_rate": -1.5}
        result = engine._check_market_index("069500")
        assert "KOSPI" in result

    def test_market_index_cache_hit(self, engine):
        """5분 내 재조회 안 함."""
        engine._guard["market_drop_block_pct"] = -1.0
        engine._market.get_price.return_value = {"change_rate": 0.5}
        engine._check_market_index("069500")
        engine._check_market_index("069500")
        assert engine._market.get_price.call_count == 1

    def test_market_index_fail_open(self, engine):
        """API 오류 시 차단 안 함."""
        engine._guard["market_drop_block_pct"] = -1.0
        engine._market.get_price.side_effect = Exception("API error")
        assert engine._check_market_index("069500") == ""

    def test_trend_filter_blocks_downtrend(self, engine):
        """SMA20 아래 매수 차단."""
        engine._guard["trend_filter_enabled"] = True
        engine._market.get_daily_price.return_value = [
            {"close": 10000 + i * 10} for i in range(20)
        ]
        # SMA20 = avg of these ~ 10095
        engine._market.get_price.return_value = {"price": 9500}  # below SMA
        result = engine._check_trend_filter("069500")
        assert "하락추세" in result

    def test_trend_filter_disabled_default(self, engine):
        """기본값 비활성."""
        assert engine._check_trend_filter("069500") == ""

    def test_duplicate_blocks_other_bot(self, engine, mock_db):
        """다른봇 보유종목 차단."""
        engine._guard["duplicate_check_enabled"] = True
        mock_db.get_board.return_value = [
            {"code": "005930", "type": "trade", "content": "매수 005930 5주", "bot_id": "other-bot"}
        ]
        result = engine._check_duplicate("005930")
        assert "다른 봇" in result

    def test_duplicate_allows_same_bot(self, engine, mock_db):
        """자기 글은 무시."""
        engine._guard["duplicate_check_enabled"] = True
        mock_db.get_board.return_value = [
            {"code": "005930", "type": "trade", "content": "매수 005930 5주", "bot_id": "test-bot"}
        ]
        result = engine._check_duplicate("005930")
        assert result == ""

    def test_budget_cap_is_full_allocation(self, engine):
        """가용 현금 캡의 유일한 정의 = self.cash (= 파이프예산 × ratio − 누적 매수액). 봇은 전액 자율 집행."""
        engine.budget = 1_000_000
        engine.cash = 1_000_000  # cash mirrors fresh budget
        engine._market.get_buyable_qty.return_value = {"max_qty": 100}
        qty = engine.get_buyable_qty("005930", 100_000)
        assert qty == 10  # 1M / 100K = 10 — 30% 같은 이중 캡 없음

    def test_sell_tracks_daily_loss(self, engine, mock_db, mock_trade_logger):
        """매도 손실 누적."""
        engine._order.sell.return_value = OrderResult(
            success=True, order_no="S1", stock_code="A", side="sell", quantity=1, price=9000,
        )
        engine.sell("A", 1, entry_price=10000)
        assert engine._daily_loss == -1000

    def test_guards_disabled_no_regression(self, engine, mock_db, mock_trade_logger):
        """기본값 → 기존 동작 동일."""
        engine._order.buy.return_value = OrderResult(
            success=True, order_no="B1", stock_code="A", side="buy", quantity=1, price=100,
        )
        result = engine.buy("A", 1, 100)
        assert result["success"] is True


class TestBudgetEnforcement:
    """2026-06-12 실사고 회귀: 마이너스 cash·ratio 0% 봇의 무제한 매수 차단."""

    def test_buy_blocked_when_cash_negative(self, engine):
        # budget이 있는 봇은 cash가 음수(과거 과집행)여도 반드시 차단
        engine.cash = -206_300
        result = engine.buy("305720", 7, 17_770)
        assert result["success"] is False
        assert "가용 현금 부족" in result["message"]

    def test_buy_blocked_when_cash_zero(self, engine):
        engine.cash = 0
        result = engine.buy("305720", 1, 17_770)
        assert result["success"] is False
        assert "가용 현금 부족" in result["message"]

    def test_buy_block_reason_blocks_all_buys(self, engine):
        # pipe_runner가 ratio 0% 봇에 설정하는 커스토디언 모드
        engine.buy_block_reason = "ratio 0% — 매수 비활성 (보유분 관리 전용)"
        engine.cash = 1_000_000
        result = engine.buy("005930", 1, 70_000)
        assert result["success"] is False
        assert "ratio 0%" in result["message"]

    def test_standalone_unlimited_budget_zero_keeps_old_semantics(self, engine):
        # budget=0(독립 실행 무제한)은 cash 체크를 건너뛰는 기존 의미론 유지
        engine.budget = 0
        engine.cash = 0
        engine.buy_block_reason = ""
        engine._order.buy.return_value = OrderResult(
            success=False, order_no="", message="모의 거부")
        result = engine.buy("005930", 1, 70_000)
        # cash 체크에서 차단되지 않고 주문 단계까지 도달했는지 확인
        assert "가용 현금 부족" not in result.get("message", "")
