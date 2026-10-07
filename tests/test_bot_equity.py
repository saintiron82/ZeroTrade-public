"""봇 자기자본 일별 재구성 — 순수 함수, 네트워크 없음."""

from src.services.bot_equity import reconstruct_daily_equity, period_return


def _close_fn(table):
    """table: {(code, date): price}. 없으면 None."""
    return lambda code, date: table.get((code, date))


def _buy(ts, code, qty, price, reason=""):
    return {"timestamp": ts, "side": "buy", "stock_code": code,
            "quantity": qty, "price": price, "reason": reason, "success": True}


def _sell(ts, code, qty, price, reason=""):
    return {"timestamp": ts, "side": "sell", "stock_code": code,
            "quantity": qty, "price": price, "reason": reason, "success": True}


def test_single_day_buy_sell_uses_budget_as_base():
    # 예산 100,000. 당일 10주 @1,000 매수 → 10주 @1,100 매도. 실현 +1,000.
    trades = [_buy("2026-06-12T09:00:00", "AAA", 10, 1000),
              _sell("2026-06-12T10:00:00", "AAA", 10, 1100)]
    rows = reconstruct_daily_equity(
        trades, budget=100000, days=["2026-06-12"],
        close_price=_close_fn({("AAA", "2026-06-12"): 1100}))
    assert len(rows) == 1
    r = rows[0]
    # 시작: 보유 없음, 현금 100,000. 끝: 보유 없음, 현금 101,000.
    assert r["equity_start"] == 100000
    assert r["equity_end"] == 101000
    assert r["daily_pnl"] == 1000
    assert round(r["return_pct"], 4) == 1.0
    assert r["price_estimated"] is False


def test_overnight_position_valued_at_close():
    # d0: 10주 @1,000 매수(보유). d1: 거래 없음은 days에 없음 → d0만.
    # d0 종가 1,050 → 미실현 +500.
    trades = [_buy("2026-06-12T09:00:00", "AAA", 10, 1000)]
    rows = reconstruct_daily_equity(
        trades, budget=100000, days=["2026-06-12"],
        close_price=_close_fn({("AAA", "2026-06-12"): 1050}))
    r = rows[0]
    # base: 매수 전 현금 100,000, 보유 0 → equity_base = 100,000
    # end: 현금 90,000 + 10×1,050 = 100,500
    assert r["equity_start"] == 100000
    assert r["equity_end"] == 100500
    assert r["daily_pnl"] == 500


def test_carried_position_next_day_unrealized_change():
    # d0 매수 보유, d1 추가 거래 1주 매도. 이틀 모두 window.
    trades = [_buy("2026-06-12T09:00:00", "AAA", 10, 1000),
              _sell("2026-06-13T10:00:00", "AAA", 1, 1200)]
    table = {("AAA", "2026-06-12"): 1050, ("AAA", "2026-06-13"): 1100}
    rows = reconstruct_daily_equity(
        trades, budget=100000, days=["2026-06-12", "2026-06-13"],
        close_price=_close_fn(table))
    d0, d1 = rows
    # d0 end: 90,000 + 10×1,050 = 100,500
    assert d0["equity_end"] == 100500
    # d1: 1주 @1,200 매도 → 현금 90,000+1,200=91,200, 보유 9주×1,100=9,900 → 101,100
    assert d1["equity_end"] == 101100
    # d1 daily_pnl = 101,100 − 100,500 = 600
    assert d1["daily_pnl"] == 600
    assert round(d1["return_pct"], 4) == round(600 / 100500 * 100, 4)


def test_zero_budget_bot_with_holding_has_valid_return():
    # budget 0인데 이전부터 보유분(orphan-assign으로 들어온 lot) 존재.
    # 버그② 회귀: 분모가 보유가치라 ROI가 0%가 아니어야 한다.
    # 윈도우 첫날 base는 전일(D-1=6/11) 종가 5,400으로 평가한다.
    trades = [_buy("2026-06-10T09:00:00", "BBB", 10, 5000, reason="orphan-assign"),
              _sell("2026-06-12T10:00:00", "BBB", 10, 5500)]
    table = {("BBB", "2026-06-11"): 5400, ("BBB", "2026-06-12"): 5500}
    rows = reconstruct_daily_equity(
        trades, budget=0, days=["2026-06-12"],
        close_price=_close_fn(table))
    r = rows[0]
    # base(전일까지): orphan-assign으로 현금 0 + 10주, 전일종가 5,400 평가 = 54,000
    assert r["equity_start"] == 54000
    # end: 10주 매도 → 현금 55,000, 보유 0 = 55,000.
    assert r["equity_end"] == 55000
    # daily_pnl = 55,000 − 54,000 = 1,000 (전일 대비 미실현→실현 1주당 +100)
    assert r["daily_pnl"] == 1000
    # 핵심: equity_start>0이라 return_pct가 None이 아님 (분모 0 버그 없음)
    assert r["equity_start"] > 0
    assert r["return_pct"] is not None


def test_price_unavailable_falls_back_to_cost_basis():
    trades = [_buy("2026-06-12T09:00:00", "AAA", 10, 1000)]
    rows = reconstruct_daily_equity(
        trades, budget=100000, days=["2026-06-12"],
        close_price=_close_fn({}))  # 종가 없음
    r = rows[0]
    # cost basis 1,000 폴백 → 보유 10×1,000=10,000, 현금 90,000 = 100,000
    assert r["equity_end"] == 100000
    assert r["price_estimated"] is True


def test_equity_start_zero_yields_null_return():
    # 보유 없고 현금 0(budget 0)인 날 — 분모 0 → return_pct None
    trades = [_buy("2026-06-12T09:00:00", "AAA", 0, 0)]  # 사실상 무거래
    rows = reconstruct_daily_equity(
        [], budget=0, days=["2026-06-12"], close_price=_close_fn({}))
    assert rows[0]["return_pct"] is None


def test_period_return_compounds():
    daily = [{"return_pct": 1.0, "daily_pnl": 1000},
             {"return_pct": 1.0, "daily_pnl": 1010}]
    p = period_return(daily)
    assert p["cum_pnl"] == 2010
    assert round(p["twr_pct"], 4) == round((1.01 * 1.01 - 1) * 100, 4)


def test_period_return_skips_null_days():
    daily = [{"return_pct": None, "daily_pnl": 0},
             {"return_pct": 2.0, "daily_pnl": 500}]
    p = period_return(daily)
    assert p["cum_pnl"] == 500
    assert round(p["twr_pct"], 4) == 2.0


def test_window_first_day_carried_position_uses_prior_close():
    # 이월 보유 10주(평단 900). 윈도우 첫날=6/15만 평가.
    # 전일(6/12) 종가 1,000으로 base 평가, 당일(6/15) 종가 1,100.
    # base = 10×1,000 = 10,000(현금 0 가정 위해 orphan-assign 사용해도 됨)
    trades = [_buy("2026-06-10T09:00:00", "AAA", 10, 900, reason="orphan-assign")]
    table = {("AAA", "2026-06-12"): 1000, ("AAA", "2026-06-15"): 1100}
    rows = reconstruct_daily_equity(
        trades, budget=0, days=["2026-06-15"],
        close_price=_close_fn(table))
    r = rows[0]
    # base: 전일종가 1,000 → 10×1,000 = 10,000 (D-1 평가)
    assert r["equity_start"] == 10000
    # end: 당일종가 1,100 → 10×1,100 = 11,000
    assert r["equity_end"] == 11000
    assert r["daily_pnl"] == 1000
    assert round(r["return_pct"], 4) == 10.0


def test_window_first_day_no_prior_close_falls_back_to_cost_basis():
    # 전일종가 없음 → cost basis(평단 900) 폴백 + price_estimated=True.
    trades = [_buy("2026-06-10T09:00:00", "AAA", 10, 900, reason="orphan-assign")]
    table = {("AAA", "2026-06-15"): 1100}  # 전일종가 없음
    rows = reconstruct_daily_equity(
        trades, budget=0, days=["2026-06-15"],
        close_price=_close_fn(table))
    r = rows[0]
    # base: 폴백 cost basis 900 → 10×900 = 9,000
    assert r["equity_start"] == 9000
    assert r["equity_end"] == 11000
    assert r["price_estimated"] is True


def test_period_return_excludes_negative_avg_equity():
    # equity_start 음수가 avg_equity 분모에 들어가면 안 됨.
    daily = [{"return_pct": 1.0, "daily_pnl": 100, "equity_start": -500},
             {"return_pct": 2.0, "daily_pnl": 200, "equity_start": 1000}]
    p = period_return(daily)
    # 음수 제외 → 1,000 / 2 = 500
    assert p["avg_equity"] == 500


def test_orphan_assign_day_inflow_excluded_from_pnl():
    # mirinae-3 6/11 재현: D-1까지 보유 10주, 당일 orphan-assign 30주 주입
    # + 실제 매도 2주. 주입분(자본 흐름)은 daily_pnl에서 제외되어야 한다.
    trades = [
        # 이월 보유 (윈도우 전): 10주, 평단 5,000
        _buy("2026-06-10T09:00:00", "MMM", 10, 5000, reason="orphan-assign"),
        # 당일 orphan-assign 주입: 30주 (현금 차감 없이 보유분 +)
        _buy("2026-06-11T09:30:00", "MMM", 30, 5600, reason="orphan-assign"),
        # 당일 실제 매도: 2주 @5,800 (실현 손익 발생)
        _sell("2026-06-11T13:00:00", "MMM", 2, 5800),
    ]
    table = {("MMM", "2026-06-10"): 5500, ("MMM", "2026-06-11"): 5600}
    rows = reconstruct_daily_equity(
        trades, budget=0, days=["2026-06-11"],
        close_price=_close_fn(table))
    r = rows[0]
    # base: 전일종가 5,500 × 10주 = 55,000
    assert r["equity_start"] == 55000
    # end: 매도 2주 현금 +11,600, 보유 38주 × 5,600 = 212,800 → cash 11,600 + 212,800 = 224,400
    assert r["equity_end"] == 224400
    # raw 변동 = 224,400 - 55,000 = 169,400 (그중 30주×5,600=168,000은 주입 흐름)
    # net_flow = +168,000 → daily_pnl = 169,400 - 168,000 = 1,400
    assert r["daily_pnl"] == 1400
    # return_pct = 1,400 / 55,000 ≈ 2.5% — 91.9% 폭주가 사라져야 한다
    assert round(r["return_pct"], 4) == round(1400 / 55000 * 100, 4)
    assert r["return_pct"] < 10


def test_phantom_cleanup_day_outflow_excluded_from_pnl():
    # phantom-cleanup으로 보유분 제거 — 손실로 잡히면 안 된다.
    trades = [
        # 이월 보유 10주, 평단 1,000
        _buy("2026-06-10T09:00:00", "PPP", 10, 1000, reason="orphan-assign"),
        # 당일 phantom-cleanup: 4주 제거 (현금/실현 무영향, 보유분만 -)
        _sell("2026-06-11T10:00:00", "PPP", 4, 1100, reason="phantom-cleanup"),
    ]
    table = {("PPP", "2026-06-10"): 1000, ("PPP", "2026-06-11"): 1100}
    rows = reconstruct_daily_equity(
        trades, budget=0, days=["2026-06-11"],
        close_price=_close_fn(table))
    r = rows[0]
    # base: 전일종가 1,000 × 10 = 10,000
    assert r["equity_start"] == 10000
    # end: phantom 제거 후 6주 × 1,100 = 6,600 (cash 0)
    assert r["equity_end"] == 6600
    # raw 변동 = 6,600 - 10,000 = -3,400. 그중 -4,400(4주×1,100)은 제거 흐름.
    # net_flow = -4,400 → daily_pnl = -3,400 - (-4,400) = +1,000 (남은 6주 평가이익)
    assert r["daily_pnl"] == 1000
    assert r["return_pct"] is not None


def test_normal_day_unaffected_by_flow_correction():
    # orphan/phantom 없는 평범한 날 — net_flow 0, 기존 동작 그대로.
    trades = [_buy("2026-06-12T09:00:00", "AAA", 10, 1000),
              _sell("2026-06-12T10:00:00", "AAA", 10, 1100)]
    rows = reconstruct_daily_equity(
        trades, budget=100000, days=["2026-06-12"],
        close_price=_close_fn({("AAA", "2026-06-12"): 1100}))
    r = rows[0]
    assert r["daily_pnl"] == 1000
    assert round(r["return_pct"], 4) == 1.0
