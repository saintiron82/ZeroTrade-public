"""Tests for orphan-holdings detection + assignment.

Orphan = stock that exists in the KIS broker balance but no bot ledger claims it.
Common causes: external buy, T+2 settlement quirks, pre-bot-era holdings.
The user can assign an orphan to a specific bot, which writes a synthetic BUY
row with reason='orphan-assign' (lot added, cash unchanged).
"""

from __future__ import annotations

import sys
from datetime import datetime
from fastapi.testclient import TestClient
from unittest.mock import patch

import pytest


@pytest.fixture
def db(monkeypatch, tmp_path):
    db_path = tmp_path / "ff.db"
    monkeypatch.setenv("FF_DB_PATH", str(db_path))
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


def _client():
    from src.web.app import app
    return TestClient(app)


def _log(db, *, source, side, code, qty, price, days_ago=0, reason="", pipe="strat-001"):
    from datetime import timedelta
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    db._conn.execute(
        """INSERT INTO trades (timestamp, pipe_id, stock_code, stock_name,
           side, quantity, price, amount, order_no, success, reason, source,
           realized_pnl, decision_id)
           VALUES (?, ?, ?, '', ?, ?, ?, ?, '', 1, ?, ?, 0, 0)""",
        (ts, pipe, code, side, qty, price, qty * price, reason, source),
    )
    db._conn.commit()


def _patch_kis_balance(monkeypatch, holdings):
    """Patch KIS get_balance to return the given holdings dict."""
    from src.web import routes
    def fake_fetcher(pipe_id):
        return {"holdings": [{"stock_code": k, "name": "", "quantity": int(v)}
                             for k, v in holdings.items()]}
    monkeypatch.setattr(routes, "_fetch_kis_balance_for_pipe", fake_fetcher)


def test_orphan_detected_when_kis_qty_exceeds_bot_ledger_sum(db, monkeypatch):
    """1 share in KIS, no bot owns it → orphan."""
    _patch_kis_balance(monkeypatch, {"102780": 1, "091180": 1})
    # bot-a has 1 share of 091180 in ledger
    _log(db, source="bot-a", side="buy", code="091180", qty=1, price=36000)

    r = _client().get("/api/orphan-holdings?pipe_id=strat-001").json()
    orphans = {o["code"]: o for o in r["orphans"]}
    # 102780: KIS=1, bots=0 → orphan qty=1
    assert "102780" in orphans
    assert orphans["102780"]["orphan_qty"] == 1
    # 091180: KIS=1, bot-a=1 → no orphan
    assert "091180" not in orphans


def test_no_orphans_when_all_kis_covered_by_bots(db, monkeypatch):
    _patch_kis_balance(monkeypatch, {"102780": 2})
    _log(db, source="bot-a", side="buy", code="102780", qty=1, price=25000)
    _log(db, source="bot-b", side="buy", code="102780", qty=1, price=26000)
    r = _client().get("/api/orphan-holdings?pipe_id=strat-001").json()
    assert r["orphans"] == []


def test_assign_writes_orphan_attribution_trade(db, monkeypatch):
    _patch_kis_balance(monkeypatch, {"102780": 1})
    # No bot owns 102780 → 1 orphan

    r = _client().post(
        "/api/orphan-holdings/assign",
        json={"pipe_id": "strat-001", "code": "102780", "bot_id": "bot-x",
              "qty": 1, "price": 25000},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True

    # Trade row was written with reason='orphan-assign'
    rows = db._conn.execute(
        "SELECT source, stock_code, side, quantity, price, reason FROM trades "
        "WHERE reason='orphan-assign'"
    ).fetchall()
    assert len(rows) == 1
    assert dict(rows[0]) == {
        "source": "bot-x", "stock_code": "102780", "side": "buy",
        "quantity": 1, "price": 25000, "reason": "orphan-assign",
    }


def test_assign_then_orphan_disappears(db, monkeypatch):
    _patch_kis_balance(monkeypatch, {"102780": 1})
    client = _client()
    # Before assign: orphan present
    assert client.get("/api/orphan-holdings?pipe_id=strat-001").json()["orphans"]
    # Assign
    client.post(
        "/api/orphan-holdings/assign",
        json={"pipe_id": "strat-001", "code": "102780", "bot_id": "bot-x",
              "qty": 1, "price": 25000},
    )
    # After assign: orphan resolved (bot-x ledger now claims the share)
    r2 = client.get("/api/orphan-holdings?pipe_id=strat-001").json()
    assert r2["orphans"] == []
