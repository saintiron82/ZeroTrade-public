"""Tests for GET /api/pnl/daily — 봇(source)별 당일 실현손익.

LIVE 대시보드 "오늘 손익" 위젯의 데이터 소스.
해당일 실현손익 = BotLedger(해당일 종료 시점) − BotLedger(해당일 시작 시점).
FIFO 매칭이 과거 매수 lot과 당일 매도를 정확히 짝짓는다.

실DB·실API 금지 — tmp DB에 trades를 직접 삽입해 검증.

Schema:
{"date": "YYYY-MM-DD",
 "bots": [{"source": "mirinae-2", "realized": 11440, "buys": 1, "sells": 2}],
 "total_realized": 12750}
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

KST = ZoneInfo("Asia/Seoul")

TODAY = datetime.now(KST).strftime("%Y-%m-%d")
YESTERDAY = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
TWO_DAYS_AGO = (datetime.now(KST) - timedelta(days=2)).strftime("%Y-%m-%d")


@pytest.fixture
def db(monkeypatch, tmp_path):
    import sys
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
    # equity 종가 조회가 실 KIS에 닿지 않도록 차단 (cost-basis 폴백)
    from src.web import routes as routes_mod
    monkeypatch.setattr(routes_mod, "_build_close_table",
                        lambda codes, days: {})
    yield db
    db._conn.close()


def _trade(db, *, source, side, qty, price, date=TODAY, time="10:00:00",
           reason="", success=1, code="111111", pipe="p1"):
    ts = f"{date}T{time}+09:00"
    db._conn.execute(
        """INSERT INTO trades (timestamp, pipe_id, stock_code, stock_name,
           side, quantity, price, amount, order_no, success, reason, source,
           realized_pnl, decision_id)
           VALUES (?, ?, ?, '', ?, ?, ?, ?, '', ?, ?, ?, 0, 0)""",
        (ts, pipe, code, side, qty, price, qty * price, success, reason, source),
    )
    db._conn.commit()


def _client():
    from src.web.app import app
    return TestClient(app)


def _get(date=""):
    url = "/api/pnl/daily" + (f"?date={date}" if date else "")
    r = _client().get(url)
    assert r.status_code == 200
    return r.json()


def _bot(data, source):
    return next((b for b in data["bots"] if b["source"] == source), None)


# ── FIFO 귀속 ──

def test_yesterday_buy_today_sell_attributes_pnl_to_today(db):
    """어제 매수 lot + 오늘 매도 → FIFO 차익이 전부 오늘 실현손익."""
    _trade(db, source="bot-a", side="buy", qty=2, price=1000, date=YESTERDAY)
    _trade(db, source="bot-a", side="sell", qty=2, price=1100, date=TODAY)

    data = _get(TODAY)
    a = _bot(data, "bot-a")
    assert a is not None
    assert a["realized"] == 200  # (1100-1000)×2
    assert a["buys"] == 0        # 매수는 어제
    assert a["sells"] == 1
    assert data["total_realized"] == 200
    assert data["date"] == TODAY


def test_same_day_round_trip(db):
    """오늘 매수+매도 왕복 → 당일 손익, 매수/매도 건수 집계."""
    _trade(db, source="bot-b", side="buy", qty=3, price=500,
           date=TODAY, time="09:10:00")
    _trade(db, source="bot-b", side="sell", qty=3, price=450,
           date=TODAY, time="14:00:00")

    data = _get(TODAY)
    b = _bot(data, "bot-b")
    assert b["realized"] == -150  # (450-500)×3
    assert b["buys"] == 1
    assert b["sells"] == 1
    assert data["total_realized"] == -150


def test_partial_fifo_only_sold_portion_realized(db):
    """어제 2회 매수(다른 단가) + 오늘 일부 매도 → FIFO로 오래된 lot부터 차감."""
    _trade(db, source="bot-f", side="buy", qty=2, price=1000,
           date=YESTERDAY, time="09:00:00")
    _trade(db, source="bot-f", side="buy", qty=2, price=1200,
           date=YESTERDAY, time="10:00:00")
    _trade(db, source="bot-f", side="sell", qty=3, price=1300, date=TODAY)

    data = _get(TODAY)
    f = _bot(data, "bot-f")
    # FIFO: 1000원 lot 2주 (+600) + 1200원 lot 1주 (+100)
    assert f["realized"] == 700


# ── source 필터 ──

def test_bot_without_today_trades_excluded(db):
    """당일 거래 없는 source는 응답에서 제외."""
    _trade(db, source="bot-old", side="buy", qty=1, price=1000, date=YESTERDAY)
    _trade(db, source="bot-now", side="buy", qty=1, price=1000, date=TODAY)

    data = _get(TODAY)
    assert _bot(data, "bot-old") is None
    now = _bot(data, "bot-now")
    assert now is not None
    assert now["realized"] == 0
    assert now["buys"] == 1


def test_manual_source_included(db):
    """수동 주문(source='manual')도 당일 거래가 있으면 포함."""
    _trade(db, source="manual", side="buy", qty=1, price=70000, date=TODAY)

    data = _get(TODAY)
    m = _bot(data, "manual")
    assert m is not None
    assert m["buys"] == 1


def test_failed_trades_ignored(db):
    """success=0 거래는 손익·건수에서 제외."""
    _trade(db, source="bot-g", side="buy", qty=1, price=1000, date=YESTERDAY)
    _trade(db, source="bot-g", side="sell", qty=1, price=2000,
           date=TODAY, time="10:00:00", success=0)
    _trade(db, source="bot-g", side="sell", qty=1, price=1100,
           date=TODAY, time="11:00:00")

    data = _get(TODAY)
    g = _bot(data, "bot-g")
    assert g["realized"] == 100  # 실패 매도(2000원) 미반영
    assert g["sells"] == 1


def test_no_trades_at_all(db):
    """거래가 전혀 없으면 빈 bots + total 0."""
    data = _get(TODAY)
    assert data["bots"] == []
    assert data["total_realized"] == 0


# ── phantom-cleanup ──

def test_phantom_cleanup_sell_not_realized_not_counted(db):
    """phantom-cleanup 합성 SELL은 손익 미반영 + 매도 건수 미집계."""
    _trade(db, source="bot-d", side="buy", qty=2, price=1000, date=YESTERDAY)
    _trade(db, source="bot-d", side="sell", qty=1, price=1100,
           date=TODAY, time="10:00:00")
    _trade(db, source="bot-d", side="sell", qty=1, price=1100,
           date=TODAY, time="11:00:00", reason="phantom-cleanup")

    data = _get(TODAY)
    d = _bot(data, "bot-d")
    assert d["realized"] == 100  # 실매도 1주만, cleanup 1주는 미반영
    assert d["sells"] == 1
    assert data["total_realized"] == 100


def test_phantom_cleanup_only_bot_excluded(db):
    """당일 phantom-cleanup만 있는 봇은 '거래 없음' 취급 → 제외."""
    _trade(db, source="bot-e", side="buy", qty=1, price=1000, date=YESTERDAY)
    _trade(db, source="bot-e", side="sell", qty=1, price=1000,
           date=TODAY, reason="phantom-cleanup")

    data = _get(TODAY)
    assert _bot(data, "bot-e") is None


# ── orphan-assign (합성 귀속 BUY) ──

def test_orphan_assign_buy_not_counted_in_buys(db):
    """orphan-assign 합성 BUY는 매수 건수에서 제외 — 실거래만 센다.

    실사고: mirinae-3 실제 3매수가 귀속 행 2건 때문에 5매수로 표시됨.
    """
    _trade(db, source="mirinae-3", side="buy", qty=1, price=1000,
           date=TODAY, time="09:10:00")
    _trade(db, source="mirinae-3", side="buy", qty=2, price=1000,
           date=TODAY, time="09:20:00", reason="orphan-assign")
    _trade(db, source="mirinae-3", side="buy", qty=1, price=1000,
           date=TODAY, time="09:30:00", code="222222")

    data = _get(TODAY)
    m = _bot(data, "mirinae-3")
    assert m is not None
    assert m["buys"] == 2  # 합성 행 제외, 실거래만


def test_orphan_assign_only_bot_excluded(db):
    """당일 orphan-assign만 있는 봇은 '거래 없음' 취급 → 응답에서 제외."""
    _trade(db, source="bot-z", side="buy", qty=1, price=1000,
           date=TODAY, reason="orphan-assign")

    data = _get(TODAY)
    assert _bot(data, "bot-z") is None


# ── date 파라미터 ──

def test_past_date_query_isolates_that_day(db):
    """과거 일자 조회 — 그날 이후 거래는 손익에 포함되지 않는다."""
    _trade(db, source="bot-h", side="buy", qty=2, price=1000, date=TWO_DAYS_AGO)
    _trade(db, source="bot-h", side="sell", qty=1, price=1100, date=YESTERDAY)
    _trade(db, source="bot-h", side="sell", qty=1, price=1500, date=TODAY)

    data = _get(YESTERDAY)
    h = _bot(data, "bot-h")
    assert h["realized"] == 100  # 어제 매도 1주만 — 오늘 +500 미포함
    assert h["sells"] == 1
    assert data["date"] == YESTERDAY


def test_default_date_is_today_kst(db):
    """date 미지정 시 오늘(KST) 기준."""
    _trade(db, source="bot-i", side="buy", qty=1, price=1000,
           date=TODAY, time="09:30:00")
    _trade(db, source="bot-i", side="sell", qty=1, price=1050,
           date=TODAY, time="10:30:00")

    data = _get()
    assert data["date"] == TODAY
    assert _bot(data, "bot-i")["realized"] == 50


def test_invalid_date_format_rejected(db):
    data = _get("2026-6-1")
    assert "error" in data
    assert data["bots"] == []


# ── 합산 ──

def test_total_realized_sums_all_bots(db, monkeypatch):
    """total_realized 합산 + equity 수익률(%) 내림차순 정렬.

    의미론 변경(2026-06-15): 정렬 기준이 realized 금액 → equity 당일 수익률(%).
    같은 budget이면 +수익 봇이 +%, 손실 봇이 -% → 수익 봇이 먼저.
    """
    import src.strategy as strategy_mod

    class _FakeStrat:
        id = "p1"
        budget = 1000000
        bots = [{"bot_id": "bot-x", "ratio": 0.5},
                {"bot_id": "bot-y", "ratio": 0.5}]

    class _FakeMgr:
        def list_strategies(self):
            return [_FakeStrat()]
    monkeypatch.setattr(strategy_mod, "StrategyManager", _FakeMgr)

    # bot-x: 당일 왕복 +300, bot-y: 당일 왕복 -100 (보유 이월 없음 → base=budget)
    _trade(db, source="bot-x", side="buy", qty=1, price=1000,
           date=TODAY, time="09:30:00")
    _trade(db, source="bot-x", side="sell", qty=1, price=1300,
           date=TODAY, time="10:30:00")
    _trade(db, source="bot-y", side="buy", qty=1, price=2000,
           date=TODAY, time="10:00:00")
    _trade(db, source="bot-y", side="sell", qty=1, price=1900,
           date=TODAY, time="11:00:00")

    data = _get(TODAY)
    assert data["total_realized"] == 300 - 100
    assert len(data["bots"]) == 2
    # 정렬: equity 당일 수익률(%) 내림차순 — bot-x(+) > bot-y(−)
    assert data["bots"][0]["source"] == "bot-x"
    assert data["bots"][0]["return_pct"] > data["bots"][1]["return_pct"]
