"""Tests for split_holdings_by_bot — per-bot attribution for /pipes/{id}/holdings.

The web UI shows which bot owns each share of each holding. This requires
walking the pipe's full trade history with a cross-bot FIFO, since a bot may
have sold shares it didn't originally buy (filling from another bot's lots).

Phantom-cleanup rows (reason='phantom-cleanup') are synthetic ledger
corrections, not real broker transactions. They must be skipped here, or the
function double-removes shares: once via cross-bot sells, once via cleanup.
"""

from src.web.routes import split_holdings_by_bot, split_holdings_by_bot_positions


def _trade(side, code, qty, price, source, reason=""):
    return {"side": side, "stock_code": code, "quantity": qty, "price": price,
            "source": source, "reason": reason}


def test_single_bot_owns_all_shares():
    trades = [_trade("buy", "111111", 2, 1000, "bot-a")]
    out = split_holdings_by_bot(trades, code="111111", total_qty=2, aggregate_avg=1000)
    assert out == [{"bot_id": "bot-a", "qty": 2, "avg_price": 1000}]


def test_phantom_cleanup_rows_are_ignored():
    """Cleanup rows must not eat into real holders' shares."""
    trades = [
        _trade("buy", "222222", 3, 1000, "bot-real"),
        _trade("sell", "222222", 1, 1000, "bot-phantom", reason="phantom-cleanup"),
        _trade("sell", "222222", 1, 1000, "bot-phantom2", reason="phantom-cleanup"),
    ]
    out = split_holdings_by_bot(trades, code="222222", total_qty=3, aggregate_avg=1000)
    # bot-real keeps all 3; cleanup rows are skipped
    assert out == [{"bot_id": "bot-real", "qty": 3, "avg_price": 1000}]


def test_attributed_returns_unknown_when_truth_exceeds_history():
    """If history attributes 0 but pipe truth says 3, fall back to 'unknown'."""
    trades = []
    out = split_holdings_by_bot(trades, code="333333", total_qty=3, aggregate_avg=5000)
    # No buys → falls through to unknown fallback
    assert out == [{"bot_id": "", "qty": 3, "avg_price": 5000}] or \
           out == [{"bot_id": "unknown", "qty": 3, "avg_price": 5000}]


def test_cross_bot_fifo_when_bot_oversells():
    """When bot-b sells more than it bought, the excess pulls from bot-a's lots."""
    trades = [
        _trade("buy", "444444", 2, 1000, "bot-a"),
        _trade("buy", "444444", 1, 1100, "bot-b"),
        _trade("sell", "444444", 2, 1200, "bot-b"),  # only bought 1, oversell 1
    ]
    out = split_holdings_by_bot(trades, code="444444", total_qty=1, aggregate_avg=1000)
    # bot-b's oversell takes 1 from bot-a → bot-a has 1 left
    by_bot = {r["bot_id"]: r for r in out}
    assert "bot-a" in by_bot
    assert by_bot["bot-a"]["qty"] == 1


def test_position_fallback_attributes_when_trade_history_missing():
    """If trade replay cannot identify owner, live bot positions should."""
    statuses = [
        {
            "bot_id": "mirinae-2",
            "positions": [],
        },
        {
            "bot_id": "mirinae-3",
            "positions": [{"code": "091180", "qty": 1, "entry_price": 35660}],
        },
    ]

    out = split_holdings_by_bot_positions(
        statuses, code="091180", total_qty=1, aggregate_avg=35660
    )

    assert out == [{"bot_id": "mirinae-3", "qty": 1, "avg_price": 35660}]


def test_position_fallback_scales_to_pipe_truth():
    """Stale bot cache must not exceed actual pipe holding quantity."""
    statuses = [
        {
            "bot_id": "mirinae-3",
            "positions": [{"code": "091180", "qty": 2, "entry_price": 35660}],
        },
    ]

    out = split_holdings_by_bot_positions(
        statuses, code="091180", total_qty=1, aggregate_avg=35660
    )

    assert out == [{"bot_id": "mirinae-3", "qty": 1, "avg_price": 35660}]
