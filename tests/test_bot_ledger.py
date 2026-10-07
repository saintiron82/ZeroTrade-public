"""Tests for BotLedger — bot's own trade history → cash/positions."""

from src.bots.ledger import BotLedger, Lot


def test_empty_trades_returns_initial_cash():
    ledger = BotLedger.from_trades(budget=100000, trades=[])
    assert ledger.cash == 100000
    assert ledger.positions == []
    assert ledger.realized_pnl == 0


def test_single_buy_deducts_cash_and_creates_position():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.cash == 100000 - 2 * 25000
    assert len(ledger.positions) == 1
    pos = ledger.positions[0]
    assert pos["code"] == "102780"
    assert pos["qty"] == 2
    assert pos["avg_price"] == 25000


def test_buy_then_partial_sell_uses_fifo_realized_pnl():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "sell", "stock_code": "102780", "quantity": 1, "price": 26000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.cash == 76000  # 100000 - 50000 + 26000
    assert ledger.positions[0]["qty"] == 1
    assert ledger.realized_pnl == 1000  # (26000 - 25000) * 1


def test_full_sell_removes_position():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "sell", "stock_code": "102780", "quantity": 2, "price": 26000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions == []
    assert ledger.realized_pnl == 2000
    assert ledger.cash == 100000 + 2000  # initial budget + realized


def test_failed_trade_is_ignored():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": False},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.cash == 100000
    assert ledger.positions == []


def test_multi_lot_average_price():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 20000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 30000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions[0]["qty"] == 2
    assert ledger.positions[0]["avg_price"] == 25000


def test_overnight_sell_more_than_held_clamps_to_held():
    """over-sell beyond known qty: only held qty is matched, rest ignored."""
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "sell", "stock_code": "102780", "quantity": 5, "price": 26000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions == []
    assert ledger.realized_pnl == 1000  # only 1주 matched


def test_lots_in_chronological_order_regardless_of_input():
    trades = [
        {"side": "sell", "stock_code": "102780", "quantity": 1, "price": 26000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions == []
    assert ledger.realized_pnl == 1000


def test_phantom_cleanup_sell_removes_lot_without_cash_or_pnl_change():
    """phantom-cleanup sell pops lots but does NOT credit cash or accrue realized_pnl.

    Use case: bot ledger claims a share that doesn't exist in reality. A sweep
    writes a synthetic sell row with reason='phantom-cleanup' to clear the
    phantom lot. The bot's cash should not be inflated by the fake sale, and
    realized_pnl should not be falsely improved.
    """
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "sell", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-07T10:00:00", "success": True,
         "reason": "phantom-cleanup"},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    # Cash: only the original buy debit, no credit from cleanup sell
    assert ledger.cash == 100000 - 2 * 25000
    # Realized PnL: cleanup sell does not contribute
    assert ledger.realized_pnl == 0
    # Position: 1 share remains after cleanup pops 1 lot
    assert ledger.positions[0]["qty"] == 1


def test_orphan_assign_buy_adds_lot_without_cash_or_pnl_change():
    """orphan-assign buy adds a lot WITHOUT debiting cash.

    Use case: a share exists in the broker account but no bot ledger claims it
    (orphan). The user assigns it to a specific bot. The bot's ledger should
    now own the lot, but the bot's cash should NOT be debited — the share was
    already paid for previously (somehow); this is just attribution.
    """
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-13T10:00:00", "success": True,
         "reason": "orphan-assign"},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    # Cash unchanged — assignment is attribution, not a real purchase
    assert ledger.cash == 100000
    # Position exists with the lot
    assert len(ledger.positions) == 1
    assert ledger.positions[0]["qty"] == 1
    assert ledger.positions[0]["avg_price"] == 25000


def test_orphan_assign_then_real_sell_realizes_pnl_normally():
    """After orphan-assign, a subsequent real sell behaves like FIFO sell of any lot."""
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-13T10:00:00", "success": True,
         "reason": "orphan-assign"},
        {"side": "sell", "stock_code": "102780", "quantity": 1, "price": 26000,
         "timestamp": "2026-05-13T11:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions == []
    # Realized PnL = sell - assigned cost = 1000
    assert ledger.realized_pnl == 1000
    # Cash: only the sell proceeds (no buy debit since assign skipped it)
    assert ledger.cash == 100000 + 26000


def test_phantom_cleanup_after_full_holding_clears_position():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "sell", "stock_code": "102780", "quantity": 1, "price": 0,
         "timestamp": "2026-05-07T10:00:00", "success": True,
         "reason": "phantom-cleanup"},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions == []
    assert ledger.cash == 75000  # only the buy debit, no fake credit
    assert ledger.realized_pnl == 0
