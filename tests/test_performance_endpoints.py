"""성과 엔드포인트 — equity 기반 정렬·수익률. KISMarket mock."""

from unittest.mock import patch


def test_build_close_table_normalizes_dates():
    from src.web.routes import _build_close_table
    # get_daily_price는 date를 'YYYYMMDD'로 준다 → 'YYYY-MM-DD'로 정규화
    fake = {"AAA": [{"date": "20260612", "close": 1100},
                    {"date": "20260613", "close": 1200}]}
    with patch("src.web.routes._fetch_daily_closes",
               side_effect=lambda c, n: fake.get(c, [])):
        table = _build_close_table(["AAA"], days=5)
    assert table[("AAA", "2026-06-12")] == 1100
    assert table[("AAA", "2026-06-13")] == 1200
    assert table.get(("AAA", "2026-06-14")) is None


def _make_trades_db(perf_rows, equity_trades):
    """get_db() mock — _conn.execute(...).fetchall() + get_trades 지원."""
    from unittest.mock import MagicMock
    db = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = perf_rows
    db._conn.execute.return_value = cur
    db.get_trades.return_value = equity_trades
    return db


def test_bot_performance_sorts_by_return_not_amount():
    """자본 작은 봇이 금액은 적어도 수익률이 높으면 위로."""
    import asyncio
    import src.web.routes as routes

    perf_rows = [
        {"bot_id": "big", "day": "2026-06-12", "trades": 1, "wins": 1,
         "losses": 0, "pnl": 2000},
        {"bot_id": "small", "day": "2026-06-12", "trades": 1, "wins": 1,
         "losses": 0, "pnl": 1000},
    ]
    equity_trades = [
        {"timestamp": "2026-06-12T09:00:00", "side": "buy", "stock_code": "X",
         "quantity": 10, "price": 1000, "source": "big", "success": True},
        {"timestamp": "2026-06-12T10:00:00", "side": "sell", "stock_code": "X",
         "quantity": 10, "price": 1200, "source": "big", "success": True},
        {"timestamp": "2026-06-12T09:00:00", "side": "buy", "stock_code": "Y",
         "quantity": 10, "price": 100, "source": "small", "success": True},
        {"timestamp": "2026-06-12T10:00:00", "side": "sell", "stock_code": "Y",
         "quantity": 10, "price": 200, "source": "small", "success": True},
    ]
    db = _make_trades_db(perf_rows, equity_trades)

    class FakeStrat:
        id = "strat-001"
        budget = 300000
        bots = [{"bot_id": "big", "ratio": 0.9},
                {"bot_id": "small", "ratio": 0.1}]

    # 라우트가 함수 내부에서 import하므로 원본 모듈을 패치한다.
    with (
        patch("src.db.get_db", return_value=db),
        patch("src.strategy.StrategyManager") as SM,
        patch("src.web.routes._build_close_table", return_value={}),
        patch("src.web.routes._benchmark_daily_returns", return_value={}),
        patch("src.web.routes._benchmark_period_anchor", return_value={}),
        patch("src.web.routes._fetch_benchmark_daily", return_value=[]),
        patch("src.web.routes._fetch_benchmark_quote", return_value={}),
    ):
        SM.return_value.list_strategies.return_value = [FakeStrat()]
        result = asyncio.run(
            routes.bots_daily_performance(days=14, pipe_id="strat-001"))

    # big: +2000 on 270k base ≈ +0.74% ; small: +1000 on 30k base ≈ +3.33%
    # 금액은 big이 크지만 수익률은 small이 높다 → small이 먼저여야 한다
    ids = [b["bot_id"] for b in result["bots"]]
    assert ids[0] == "small", f"수익률 정렬 실패: {ids}"
    assert result["bots"][0]["totals"]["twr_pct"] > result["bots"][1]["totals"]["twr_pct"]


def test_daily_pnl_includes_return_pct_and_sorts_by_it():
    import asyncio
    import src.web.routes as routes
    from unittest.mock import MagicMock

    trades = [
        # big: +2000, 큰 자본
        {"timestamp": "2026-06-12T09:00:00", "side": "buy", "stock_code": "X",
         "quantity": 10, "price": 1000, "source": "big", "success": True},
        {"timestamp": "2026-06-12T10:00:00", "side": "sell", "stock_code": "X",
         "quantity": 10, "price": 1200, "source": "big", "success": True},
        # small: +1000, 작은 자본 → 수익률 높음
        {"timestamp": "2026-06-12T09:00:00", "side": "buy", "stock_code": "Y",
         "quantity": 10, "price": 100, "source": "small", "success": True},
        {"timestamp": "2026-06-12T10:00:00", "side": "sell", "stock_code": "Y",
         "quantity": 10, "price": 200, "source": "small", "success": True},
    ]
    db = MagicMock()
    db.get_trades.return_value = trades

    class FakeStrat:
        id = "strat-001"
        budget = 300000
        bots = [{"bot_id": "big", "ratio": 0.9}, {"bot_id": "small", "ratio": 0.1}]

    with (
        patch("src.db.get_db", return_value=db),
        patch("src.strategy.StrategyManager") as SM,
        patch("src.web.routes._build_close_table", return_value={}),
    ):
        SM.return_value.list_strategies.return_value = [FakeStrat()]
        result = asyncio.run(routes.get_daily_pnl(date="2026-06-12"))

    bots = result["bots"]
    assert all("return_pct" in b for b in bots)
    # small 수익률(+3.33%) > big(+0.74%) → small 먼저
    assert bots[0]["source"] == "small"
