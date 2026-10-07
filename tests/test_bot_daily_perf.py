"""Tests for /api/bot-performance/daily — per-bot daily realized PnL.

The user wants to see which bot is actually doing well day by day, not just a
single aggregate number. The endpoint groups trades by (source, date) and
returns one row per bot per day, plus a per-bot total.

Schema:
{
  "period_days": N,
  "bots": [
    {
      "bot_id": "...",
      "budget": int,
      "totals": {"pnl": int, "trades": int, "wins": int, "losses": int,
                 "win_rate": float, "roi_pct": float},
      "days": [
        {"date": "YYYY-MM-DD", "pnl": int, "trades": int,
         "wins": int, "losses": int, "roi_pct": float}, ...
      ]
    }, ...
  ]
}
"""

from datetime import datetime, timedelta
from fastapi.testclient import TestClient
import pytest


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
    from src.web import routes as routes_mod
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_quote", lambda code: None)
    yield db
    db._conn.close()


def _trade(db, *, source, side, qty, price, realized=0, days_ago=0, pipe="p1", code="111111"):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    db._conn.execute(
        """INSERT INTO trades (timestamp, pipe_id, stock_code, stock_name,
           side, quantity, price, amount, order_no, success, reason, source,
           realized_pnl, decision_id)
           VALUES (?, ?, ?, '', ?, ?, ?, ?, '', 1, '', ?, ?, 0)""",
        (ts, pipe, code, side, qty, price, qty * price, source, realized),
    )
    db._conn.commit()


def _client():
    from src.web.app import app
    return TestClient(app)


def test_endpoint_returns_per_bot_daily_rows(db):
    """3 days × 2 bots = 6 daily rows (sparse: only days with trades)."""
    _trade(db, source="bot-a", side="sell", qty=1, price=1000, realized=100, days_ago=0)
    _trade(db, source="bot-a", side="sell", qty=1, price=1000, realized=-50, days_ago=1)
    _trade(db, source="bot-b", side="sell", qty=1, price=2000, realized=300, days_ago=0)

    r = _client().get("/api/bot-performance/daily?days=7")
    assert r.status_code == 200
    data = r.json()
    assert data["period_days"] == 7

    by_bot = {b["bot_id"]: b for b in data["bots"]}
    assert "bot-a" in by_bot and "bot-b" in by_bot

    a = by_bot["bot-a"]
    assert a["totals"]["pnl"] == 50  # 100 + (-50)
    assert a["totals"]["wins"] == 1
    assert a["totals"]["losses"] == 1
    # Two distinct days for bot-a
    assert len(a["days"]) == 2

    b = by_bot["bot-b"]
    assert b["totals"]["pnl"] == 300
    assert b["totals"]["wins"] == 1
    assert b["totals"]["losses"] == 0


def test_only_sell_trades_count_for_pnl(db):
    """buy trades have realized_pnl=0 and must not be counted in win/loss."""
    _trade(db, source="bot-c", side="buy", qty=2, price=1000, realized=0, days_ago=0)
    _trade(db, source="bot-c", side="sell", qty=2, price=1100, realized=200, days_ago=0)

    r = _client().get("/api/bot-performance/daily?days=7").json()
    c = next(b for b in r["bots"] if b["bot_id"] == "bot-c")
    # Only the sell counts toward wins/losses
    assert c["totals"]["wins"] == 1
    assert c["totals"]["losses"] == 0
    assert c["totals"]["pnl"] == 200


def test_excludes_phantom_cleanup_rows(db):
    """phantom-cleanup synthetic sells must not skew daily PnL stats."""
    _trade(db, source="bot-d", side="sell", qty=1, price=1000, realized=100, days_ago=0)
    # phantom-cleanup row — has reason set
    db._conn.execute(
        """INSERT INTO trades (timestamp, pipe_id, stock_code, stock_name,
           side, quantity, price, amount, order_no, success, reason, source,
           realized_pnl, decision_id)
           VALUES (?, 'p1', '111111', '', 'sell', 1, 1000, 1000, '', 1,
           'phantom-cleanup', 'bot-d', 0, 0)""",
        (datetime.now().isoformat(),),
    )
    db._conn.commit()

    r = _client().get("/api/bot-performance/daily?days=7").json()
    d = next(b for b in r["bots"] if b["bot_id"] == "bot-d")
    assert d["totals"]["trades"] == 1  # phantom-cleanup excluded
    assert d["totals"]["pnl"] == 100


def test_filter_by_days_window(db):
    """days=2 must exclude trades from 5 days ago."""
    _trade(db, source="bot-e", side="sell", qty=1, price=1000, realized=200, days_ago=0)
    _trade(db, source="bot-e", side="sell", qty=1, price=1000, realized=500, days_ago=5)

    r = _client().get("/api/bot-performance/daily?days=2").json()
    e = next((b for b in r["bots"] if b["bot_id"] == "bot-e"), None)
    assert e is not None
    assert e["totals"]["pnl"] == 200  # 5-day-old trade excluded


def test_benchmark_uses_period_anchored_compound_not_daily_sum(db, monkeypatch):
    """벤치마크는 '기간 시작→끝 복리 수익률'이 진짜 기회비용.

    이전 구현: bot 활성 일자만 일간 % 합산 → bot이 적게 거래하면 벤치도 같이 작아짐.
    올바른 측정: bot.budget을 기간 시작일에 KODEX 200으로 매수, 끝까지 보유 시 수익률.
    """
    from src.web import routes as routes_mod
    # 14일 chart: 시작가 1000, 끝가 1200 → period return = +20%
    fake_chart = [
        {"date": "20260513", "close": 1200},
        {"date": "20260512", "close": 1150},
        {"date": "20260511", "close": 1100},
        {"date": "20260508", "close": 1050},
        {"date": "20260507", "close": 1000},  # 기간 시작 anchor
    ]
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_daily",
                        lambda code, count: list(fake_chart))

    r = _client().get("/api/bot-performance/daily?days=7").json()
    bm = r["benchmark"]
    # 복리(period_return_pct)는 (1200-1000)/1000 = +20.0%, 일간 합 ≠ 복리
    assert "period_return_pct" in bm
    assert abs(bm["period_return_pct"] - 20.0) < 0.01
    # start/end 가격 노출
    assert bm.get("start_close") == 1000
    assert bm.get("end_close") == 1200


def test_bot_alpha_uses_period_anchored_benchmark(db, monkeypatch):
    """각 봇의 alpha = bot.roi_pct − period_return_pct.

    벤치마크는 '봇 거래 구간(첫 거래일 ~ 마지막 거래일)'으로 anchor 정렬해서
    재계산한다. 봇이 풀 윈도우 일부 기간만 거래했을 때 풀 윈도우 KODEX와
    비교하던 왜곡을 제거한다.
    """
    from src.web import routes as routes_mod
    today_yyyymmdd = datetime.now().strftime("%Y%m%d")
    three_days_ago_yyyymmdd = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
    fake_chart = [
        {"date": today_yyyymmdd, "close": 1200},
        {"date": three_days_ago_yyyymmdd, "close": 1000},
    ]
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_daily",
                        lambda code, count: list(fake_chart))

    # 봇 거래일이 구간 양끝(3일 전, 오늘) → 봇 구간 ≈ 차트 풀 윈도우 → +20%
    _trade(db, source="bot-z", side="sell", qty=1, price=1000, realized=500, days_ago=3)
    _trade(db, source="bot-z", side="sell", qty=1, price=1000, realized=0, days_ago=0)

    r = _client().get("/api/bot-performance/daily?days=7").json()
    z = next((b for b in r["bots"] if b["bot_id"] == "bot-z"), None)
    assert z is not None
    assert "benchmark_period_pct" in z["totals"]
    # 봇 구간(3일 전~오늘) 안의 차트 두 점: 1000 → 1200 = +20%
    assert abs(z["totals"]["benchmark_period_pct"] - 20.0) < 0.01
    # 봇 거래 구간 메타 정보도 노출되어야 한다
    assert z["totals"].get("bot_first_date") is not None
    assert z["totals"].get("bot_last_date") is not None


def test_bot_alpha_uses_only_bot_trading_window(db, monkeypatch):
    """봇이 풀 윈도우의 후반부만 거래했으면, 그 후반부 KODEX만 비교 대상.

    예: days=7 윈도우에서 봇이 최근 2일만 거래 → KODEX도 최근 2일치만 사용.
    풀 7일 KODEX(+50%)와 비교하면 부당, 봇 구간(+5%)으로 비교해야 공정.
    """
    from src.web import routes as routes_mod
    today_yyyymmdd = datetime.now().strftime("%Y%m%d")
    one_day_ago_yyyymmdd = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    six_days_ago_yyyymmdd = (datetime.now() - timedelta(days=6)).strftime("%Y%m%d")
    # 풀 윈도우: 1000 → 1500 (+50%). 최근 2일: 1428 → 1500 (+5.04%).
    fake_chart = [
        {"date": today_yyyymmdd, "close": 1500},
        {"date": one_day_ago_yyyymmdd, "close": 1428},
        {"date": six_days_ago_yyyymmdd, "close": 1000},
    ]
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_daily",
                        lambda code, count: list(fake_chart))

    # 봇은 최근 2일만 거래
    _trade(db, source="bot-late", side="sell", qty=1, price=1000, realized=100, days_ago=1)
    _trade(db, source="bot-late", side="sell", qty=1, price=1000, realized=50, days_ago=0)

    r = _client().get("/api/bot-performance/daily?days=7").json()
    bot = next((b for b in r["bots"] if b["bot_id"] == "bot-late"), None)
    assert bot is not None
    # 풀 윈도우 50%가 아니라 봇 구간 ~5.04%로 비교
    assert abs(bot["totals"]["benchmark_period_pct"] - 5.042) < 0.05


def test_alpha_krw_equals_realized_minus_buyhold_profit(db, monkeypatch):
    """알파(KRW) = 봇 실현손익 − (예산을 KODEX200에 풀투입한 가상 수익).

    봇 예산을 알 수 없는 fixture 환경에선 0으로 계산되지만 키는 존재.
    """
    from src.web import routes as routes_mod
    today = datetime.now().strftime("%Y%m%d")
    fake_chart = [
        {"date": today, "close": 1100},
        {"date": (datetime.now() - timedelta(days=7)).strftime("%Y%m%d"), "close": 1000},
    ]
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_daily",
                        lambda code, count: list(fake_chart))
    _trade(db, source="bot-y", side="sell", qty=1, price=2000, realized=200, days_ago=0)

    r = _client().get("/api/bot-performance/daily?days=7").json()
    y = next((b for b in r["bots"] if b["bot_id"] == "bot-y"), None)
    assert y is not None
    # benchmark_period_value는 budget × period_pct / 100. budget=0이면 0.
    assert "benchmark_period_value" in y["totals"]
    assert "alpha_krw" in y["totals"]


def test_benchmark_block_present_in_response(db, monkeypatch):
    """Response carries a `benchmark` block with daily close-to-close % returns.

    Without a benchmark to compare against, daily PnL is meaningless — bots that
    barely beat the index aren't worth the time. The endpoint must expose KODEX
    200 daily returns alongside per-bot stats.
    """
    from src.web import routes as routes_mod

    fake_chart = [
        {"date": "20260512", "close": 26500},
        {"date": "20260511", "close": 26000},
        {"date": "20260508", "close": 26260},  # +1.0% from prev
    ]
    # Patch the helper used to fetch benchmark data so we don't hit KIS in tests.
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_daily",
                        lambda code, count: list(fake_chart))

    r = _client().get("/api/bot-performance/daily?days=7").json()
    assert "benchmark" in r
    bm = r["benchmark"]
    assert bm["code"] == "069500"
    # Sparse close-to-close returns: 26000→26500 = +1.923%, 26260→26000 = -0.99%
    by_date = {d["date"]: d for d in bm["days"]}
    assert "2026-05-12" in by_date
    assert abs(by_date["2026-05-12"]["return_pct"] - 1.923) < 0.01
    assert "2026-05-11" in by_date
    assert abs(by_date["2026-05-11"]["return_pct"] - (-0.99)) < 0.01


def test_alpha_pct_attached_to_each_day_row(db, monkeypatch):
    """Each bot's day row gains alpha_pct = bot.roi_pct − benchmark.return_pct.

    equity 의미론(2026-06-15): roi_pct는 equity 변화/시작 equity로 계산되므로
    alpha가 non-None이려면 그날 equity base가 양수여야 한다. 어제 매수한 보유분
    (직전 종가로 평가)을 두면 오늘 base가 양수가 되어 roi·alpha가 산출된다.
    """
    from src.web import routes as routes_mod
    today = datetime.now().strftime("%Y-%m-%d")
    today_yyyymmdd = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_yyyymmdd = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # Build a chart anchored on today so DATE(trade.timestamp) lines up with a
    # benchmark return entry.
    fake_chart = [
        {"date": today_yyyymmdd, "close": 26500},
        {"date": yesterday_yyyymmdd, "close": 26000},  # today return = +1.923%
    ]
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_daily",
                        lambda code, count: list(fake_chart))
    # 보유분(111111)을 어제·오늘 종가로 평가 → equity base 양수
    monkeypatch.setattr(routes_mod, "_build_close_table",
                        lambda codes, days: {("111111", yesterday): 1000,
                                             ("111111", today): 1200})
    # bot-bm에 예산 배정 → equity 현금 anchor 양수
    import src.strategy as strategy_mod

    class _FakeStrat:
        id = "p1"
        budget = 1000000
        bots = [{"bot_id": "bot-bm", "ratio": 1.0}]

    class _FakeMgr:
        def list_strategies(self):
            return [_FakeStrat()]
    monkeypatch.setattr(strategy_mod, "StrategyManager", _FakeMgr)

    # 어제 10주 매수(보유), 오늘 일부 매도 → 오늘 equity base 양수
    _trade(db, source="bot-bm", side="buy", qty=10, price=1000, days_ago=1)
    _trade(db, source="bot-bm", side="sell", qty=1, price=1300, realized=300, days_ago=0)
    r = _client().get(f"/api/bot-performance/daily?days=2").json()
    b = next(x for x in r["bots"] if x["bot_id"] == "bot-bm")
    today_row = next(d for d in b["days"] if d["date"] == today)
    assert "alpha_pct" in today_row
    # alpha_pct must reflect bot ROI − benchmark return for that day
    assert today_row["alpha_pct"] is not None


def test_benchmark_includes_realtime_today_when_daily_chart_missing(db, monkeypatch):
    """KODEX 200 daily benchmark should expose today's live return too."""
    from src.web import routes as routes_mod

    today = datetime.now().strftime("%Y-%m-%d")
    yesterday_yyyymmdd = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    two_days_ago_yyyymmdd = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
    fake_chart = [
        {"date": yesterday_yyyymmdd, "close": 1000},
        {"date": two_days_ago_yyyymmdd, "close": 980},
    ]
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_daily",
                        lambda code, count: list(fake_chart))
    monkeypatch.setattr(routes_mod, "_fetch_benchmark_quote",
                        lambda code: {"price": 1030, "change_rate": 3.0})

    r = _client().get("/api/bot-performance/daily?days=7").json()
    by_date = {d["date"]: d for d in r["benchmark"]["days"]}

    assert today in by_date
    assert by_date[today]["return_pct"] == 3.0
    assert by_date[today]["source"] == "realtime"
