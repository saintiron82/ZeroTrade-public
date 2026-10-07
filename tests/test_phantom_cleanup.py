"""Tests for phantom_cleanup service.

A bot ledger entry is "phantom" when its computed position (from trades with
source=bot_id) exceeds what the pipe actually holds — i.e., the bot claims a
share that does not exist on the broker side.

The cleanup service reconciles per-bot ledgers against pipe holdings (KIS truth):
- Pipe holdings are the ground truth (sum across all bots must match).
- Allocate actual shares to most-recent buyers; older orphan buys become phantoms.
- For each phantom (bot, code) pair, write a synthetic SELL row with
  reason='phantom-cleanup' so future ledger reads stay consistent.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Spin up a fresh sqlite DB and patch get_db() to use it."""
    db_path = tmp_path / "ff.db"
    # Force config to use isolated DB before importing src.db
    monkeypatch.setenv("FF_DB_PATH", str(db_path))
    # Reload db module so the new env is honored
    for mod in ("src.db",):
        if mod in sys.modules:
            del sys.modules[mod]
    import src.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path, raising=False)
    db = db_mod.DB(db_path)
    monkeypatch.setattr(db_mod, "_db_instance", db, raising=False)
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    yield db
    db._conn.close()


def _log(db, *, source, side, code, qty, price, ts="2026-05-01T10:00:00", pipe="p1"):
    """Helper: insert a trade row with a controlled timestamp."""
    db._conn.execute(
        """INSERT INTO trades (timestamp, pipe_id, stock_code, stock_name,
           side, quantity, price, amount, order_no, success, reason, source,
           realized_pnl, decision_id)
           VALUES (?, ?, ?, '', ?, ?, ?, ?, '', 1, '', ?, 0, 0)""",
        (ts, pipe, code, side, qty, price, qty * price, source),
    )
    db._conn.commit()


def test_no_phantom_when_bot_ledger_matches_pipe(db):
    from src.services.phantom_cleanup import cleanup_pipe

    _log(db, source="bot-a", side="buy", code="111111", qty=2, price=1000,
         ts="2026-05-01T10:00:00")
    pipe_holdings = {"111111": 2}

    report = cleanup_pipe(pipe_id="p1", pipe_holdings=pipe_holdings)
    assert report["cleanups"] == []
    # No phantom-cleanup rows written
    rows = db._conn.execute(
        "SELECT COUNT(*) FROM trades WHERE reason='phantom-cleanup'"
    ).fetchone()[0]
    assert rows == 0


def test_phantom_when_two_bots_claim_more_than_pipe(db):
    """Bot A bought 1, Bot B bought 2; pipe truth = 2. Excess = 1 share."""
    from src.services.phantom_cleanup import cleanup_pipe

    # Older buyer first
    _log(db, source="bot-a", side="buy", code="111111", qty=1, price=1000,
         ts="2026-05-01T09:00:00")
    _log(db, source="bot-b", side="buy", code="111111", qty=2, price=1100,
         ts="2026-05-01T11:00:00")
    pipe_holdings = {"111111": 2}

    report = cleanup_pipe(pipe_id="p1", pipe_holdings=pipe_holdings)

    # bot-a is the older buyer → its 1 share becomes phantom
    assert {"source": "bot-a", "code": "111111", "qty": 1, "price": 1000} in report["cleanups"]
    # A phantom-cleanup SELL row was written for bot-a
    rows = db._conn.execute(
        """SELECT source, stock_code, quantity, price FROM trades
           WHERE reason='phantom-cleanup' AND source='bot-a'"""
    ).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("bot-a", "111111", 1, 1000)


def test_phantom_cleanup_preserves_recent_buyer(db):
    """Allocate ground truth to most-recent buyers; clean up older phantoms."""
    from src.services.phantom_cleanup import cleanup_pipe
    from src.bots.ledger import BotLedger

    _log(db, source="bot-a", side="buy", code="222222", qty=3, price=1000,
         ts="2026-05-01T09:00:00")
    _log(db, source="bot-b", side="buy", code="222222", qty=2, price=1100,
         ts="2026-05-01T11:00:00")
    # Pipe truth: 2 shares — bot-b's most-recent 2 lots are the real ones
    cleanup_pipe(pipe_id="p1", pipe_holdings={"222222": 2})

    # After cleanup: bot-a has 0 in its ledger (3 phantom-cleaned), bot-b has 2
    a_trades = [dict(r) for r in db._conn.execute(
        "SELECT * FROM trades WHERE source='bot-a' ORDER BY timestamp").fetchall()]
    b_trades = [dict(r) for r in db._conn.execute(
        "SELECT * FROM trades WHERE source='bot-b' ORDER BY timestamp").fetchall()]
    a_ledger = BotLedger.from_trades(budget=100000, trades=a_trades)
    b_ledger = BotLedger.from_trades(budget=100000, trades=b_trades)
    assert a_ledger.positions == []
    assert b_ledger.positions[0]["qty"] == 2


def test_cleanup_idempotent(db):
    """Running cleanup twice produces no additional cleanups."""
    from src.services.phantom_cleanup import cleanup_pipe

    _log(db, source="bot-a", side="buy", code="333333", qty=2, price=1000,
         ts="2026-05-01T09:00:00")
    pipe_holdings = {"333333": 1}

    r1 = cleanup_pipe(pipe_id="p1", pipe_holdings=pipe_holdings)
    r2 = cleanup_pipe(pipe_id="p1", pipe_holdings=pipe_holdings)

    assert len(r1["cleanups"]) == 1
    assert r2["cleanups"] == []  # ledger already balanced


def test_negative_phantom_left_alone(db):
    """Bot oversold (more sells than buys); BotLedger already filters this to 0.

    No action needed — these aren't false ownership claims, just attribution
    noise from past trades. Leave the rows alone to preserve history.
    """
    from src.services.phantom_cleanup import cleanup_pipe

    _log(db, source="bot-a", side="buy", code="444444", qty=1, price=1000,
         ts="2026-05-01T09:00:00")
    _log(db, source="bot-a", side="sell", code="444444", qty=3, price=1100,
         ts="2026-05-01T11:00:00")
    pipe_holdings = {}  # nothing held

    report = cleanup_pipe(pipe_id="p1", pipe_holdings=pipe_holdings)
    assert report["cleanups"] == []
    # No phantom-cleanup rows were written
    rows = db._conn.execute(
        "SELECT COUNT(*) FROM trades WHERE reason='phantom-cleanup'"
    ).fetchone()[0]
    assert rows == 0


def test_cleanup_when_pipe_holds_zero_but_bot_claims(db):
    """Pipe has 0 shares but bot ledger claims some → full cleanup."""
    from src.services.phantom_cleanup import cleanup_pipe

    _log(db, source="bot-a", side="buy", code="555555", qty=2, price=1000,
         ts="2026-05-01T09:00:00")

    report = cleanup_pipe(pipe_id="p1", pipe_holdings={})
    assert {"source": "bot-a", "code": "555555", "qty": 2, "price": 1000} in report["cleanups"]


def test_cleanup_uses_kis_balance_when_pipe_holdings_stale(db):
    """When pipe.holdings is stale and KIS is the broker truth, KIS wins.

    Scenario: pipe.holdings says 2 shares (stale), bot ledger says 2, but KIS
    actually has 0 (external sell, T+2 settlement, etc). Without KIS-truth
    cleanup, the bot keeps trying to sell phantoms forever.
    """
    from src.services.phantom_cleanup import cleanup_pipe

    _log(db, source="bot-a", side="buy", code="999999", qty=2, price=1000,
         ts="2026-05-12T09:00:00")
    # pipe.holdings claims 2 — same as bot ledger, would otherwise be a no-op.
    # But the explicit kis_balance override says 0 — that wins.
    report = cleanup_pipe(
        pipe_id="p1",
        pipe_holdings={"999999": 2},
        kis_balance={"999999": 0},
    )
    assert {"source": "bot-a", "code": "999999", "qty": 2, "price": 1000} in report["cleanups"]


def test_kis_balance_truth_writes_to_pipe_holdings_too(db):
    """KIS-anchored cleanup must also correct strategies.json holdings.

    Otherwise pipe.holdings stays stale, and the next display/decision uses
    fabricated holdings.
    """
    from src.services.phantom_cleanup import cleanup_pipe
    from src.strategy import StrategyManager

    _log(db, source="bot-a", side="buy", code="888888", qty=2, price=1000,
         ts="2026-05-12T09:00:00")

    # Stub StrategyManager to verify pipe.holdings is updated
    from unittest.mock import patch, MagicMock
    mock_mgr = MagicMock()
    with patch("src.services.phantom_cleanup.StrategyManager",
               return_value=mock_mgr):
        cleanup_pipe(
            pipe_id="p1",
            pipe_holdings={"888888": 2},
            kis_balance={"888888": 0},
        )

    # pipe.holdings를 KIS truth로 동기화하는 메서드가 호출되어야 함
    mock_mgr.sync_holdings.assert_called_once()
    args, kwargs = mock_mgr.sync_holdings.call_args
    # Called with pipe_id and the KIS truth dict
    assert args[0] == "p1"
    assert args[1] == {"888888": 0}


def test_cleanup_clears_cached_bot_state(db):
    """bot_state cache for cleaned (source, code) should be invalidated.

    Otherwise the bot's _load_state would restore the phantom holding from cache,
    bypassing the now-clean ledger.
    """
    from src.services.phantom_cleanup import cleanup_pipe

    _log(db, source="bot-a", side="buy", code="666666", qty=2, price=1000,
         ts="2026-05-01T09:00:00")
    # Bot saved its phantom holding to bot_state
    db.save_bot_state("p1:bot-a", {
        "holding_code": "666666",
        "holding_qty": 2,
        "holding_price": 1000,
        "realized_pnl": 999,
        "scan_count": 42,
        "positions": [{"code": "666666", "qty": 2, "price": 1000}],
    })

    cleanup_pipe(pipe_id="p1", pipe_holdings={})  # pipe has nothing

    state = db.load_bot_state("p1:bot-a")
    # holding fields cleared
    assert state["holding_code"] == ""
    assert state["holding_qty"] == 0
    # mirinae-style positions list filtered
    extra = state.get("extra", {})
    if isinstance(extra, dict):
        assert all(p.get("code") != "666666" for p in extra.get("positions", []))
    # realized_pnl preserved (not nuked)
    assert state["realized_pnl"] == 999


def test_cleanup_keeps_unrelated_cached_positions(db):
    """When a bot has multiple cached positions and only some are phantom,
    keep the non-phantom ones in bot_state.extra.positions."""
    from src.services.phantom_cleanup import cleanup_pipe

    # bot-a has 1 real + 1 phantom (pipe truth = 1)
    _log(db, source="bot-a", side="buy", code="777777", qty=1, price=1000,
         ts="2026-05-01T09:00:00")
    _log(db, source="bot-a", side="buy", code="888888", qty=1, price=2000,
         ts="2026-05-01T10:00:00")
    db.save_bot_state("p1:bot-a", {
        "holding_code": "",
        "holding_qty": 0,
        "holding_price": 0,
        "positions": [
            {"code": "777777", "qty": 1, "price": 1000},
            {"code": "888888", "qty": 1, "price": 2000},
        ],
    })

    # Pipe only really holds 777777
    cleanup_pipe(pipe_id="p1", pipe_holdings={"777777": 1})

    state = db.load_bot_state("p1:bot-a")
    positions = state.get("extra", {}).get("positions", []) if isinstance(state.get("extra"), dict) else []
    codes_in_cache = {p.get("code") for p in positions}
    assert "888888" not in codes_in_cache
    assert "777777" in codes_in_cache
