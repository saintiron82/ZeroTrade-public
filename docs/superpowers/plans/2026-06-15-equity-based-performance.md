# 자기자본 기반 성과 비교 엔진 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 봇 성과 비교를 절대 금액이 아닌 일별 자기자본(equity) 기반 수익률로 바꿔, 자본 규모가 다른 봇(winner-1 9만 / mirinae-3 21만 / mirinae-2 0%)을 공정하게 비교한다.

**Architecture:** 순수 함수 모듈 `src/services/bot_equity.py`가 봇 trades + 종가 콜백으로 일별 equity·수익률을 재구성(네트워크 0, 테스트 용이). 두 엔드포인트(`/bot-performance/daily`, `/pnl/daily`)가 이 모듈을 쓰고 정렬을 %로 교체, UI는 % 우선 표시. 스펙: `docs/superpowers/specs/2026-06-15-equity-based-performance.md`.

**Tech Stack:** Python 3.14, 기존 `BotLedger`(FIFO 재구성)·`KISMarket.get_daily_price`(일봉), FastAPI, pytest.

**작업 브랜치:** main에서 `feat/equity-performance` 분기.
**테스트 실행:** `.venv/bin/python -m pytest ...` (실 네트워크·실거래 금지 — 종가는 콜백/ mock).
**현재 베이스라인:** 369 passed.

---

## 핵심 알고리즘 (체인 방식 — 검증됨)

window 날짜 오름차순 `d0..dn` (봇이 거래한 날들). 종가 콜백 `close(code, date)`:
```
equity_close(di) = cash(trades ≤ di) + Σ qty(≤di)[code] × close(code, di)
equity_base      = cash(trades < d0) + Σ qty(<d0)[code] × close(code, d0)   # 이전 보유분을 d0 종가로 평가
prev = equity_base
for di:
    daily_pnl(di)   = equity_close(di) − prev
    return_pct(di)  = daily_pnl/prev×100   (prev > 0일 때만, 아니면 None)
    prev = equity_close(di)
period: cum_pnl = Σ daily_pnl ; twr_pct = (Π(1+rp/100) − 1)×100  (rp None인 날 제외)
```
하루 내 자본 유입/유출이 없으므로 `daily_pnl = realized(di) + Δunrealized(di)`와 정확히 일치. budget 변경(ratio)은 numerator에 안 들어가므로 오염 없음.

---

## File Structure

- Create: `src/services/bot_equity.py` — 순수 재구성 함수 2개
- Modify: `src/web/routes.py` — 종가 캐시 헬퍼 + 두 엔드포인트 연결 + 정렬 교체
- Modify: `src/web/static/index.html` — 오늘 손익 위젯에 % 표시
- Test: `tests/test_bot_equity.py` (순수, 네트워크 0)
- Test: `tests/test_performance_endpoints.py` (라우트, KISMarket mock)

---

### Task 1: bot_equity 순수 모듈

**Files:**
- Create: `src/services/bot_equity.py`
- Test: `tests/test_bot_equity.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_bot_equity.py` 생성:

```python
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
    trades = [_buy("2026-06-10T09:00:00", "BBB", 10, 5000, reason="orphan-assign"),
              _sell("2026-06-12T10:00:00", "BBB", 10, 5500)]
    table = {("BBB", "2026-06-12"): 5500}
    rows = reconstruct_daily_equity(
        trades, budget=0, days=["2026-06-12"],
        close_price=_close_fn(table))
    r = rows[0]
    # base(전일까지): orphan-assign으로 현금 0 + 10주, d0종가 5,500 평가 = 55,000
    assert r["equity_start"] == 55000
    # end: 10주 매도 → 현금 55,000, 보유 0 = 55,000. 실현 +5,000(5,500-5,000)
    assert r["equity_end"] == 55000
    # daily_pnl = 55,000 − 55,000 = 0?  ← 매도 차익은 base가 이미 5,500로 평가해서 0
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
```

- [ ] **Step 2: FAIL 확인**

Run: `.venv/bin/python -m pytest tests/test_bot_equity.py -v`
Expected: `ModuleNotFoundError: src.services.bot_equity`

- [ ] **Step 3: 구현**

`src/services/bot_equity.py` 생성:

```python
"""봇 자기자본(equity) 일별 재구성 — 성과 비교용.

순수 함수: trades + 종가 콜백만 받는다 (네트워크/DB 없음).
equity = 현금 + 보유가치(종가×수량). 일별 수익률 = equity 변화 / 시작 equity.
자본 규모가 다른 봇을 공정 비교하기 위한 분모 정규화.
스펙: docs/superpowers/specs/2026-06-15-equity-based-performance.md
"""

from typing import Callable

from src.bots.ledger import BotLedger


def _value_positions(positions: list[dict], date: str,
                     close_price: Callable[[str, str], int | None]) -> tuple[int, bool]:
    """positions(list of {code, qty, avg_price}) → (시장가치, 추정여부).

    종가 없으면 평단(cost basis)으로 폴백하고 estimated=True.
    """
    total = 0
    estimated = False
    for p in positions:
        px = close_price(p["code"], date)
        if not px or px <= 0:
            px = int(p.get("avg_price") or 0)
            estimated = True
        total += int(p.get("qty") or 0) * int(px)
    return total, estimated


def reconstruct_daily_equity(
    trades: list[dict],
    budget: int,
    days: list[str],
    close_price: Callable[[str, str], int | None],
) -> list[dict]:
    """봇의 일별 equity·수익률 재구성 (체인 방식).

    Args:
        trades: 한 봇의 success 체결 이력 (source 필터는 호출자 책임)
        budget: allocated budget (현금 절대값 anchor)
        days: 평가 대상 날짜 YYYY-MM-DD (정렬 무관, 내부 정렬)
        close_price: (code, date) → 종가 or None

    Returns:
        [{date, equity_start, equity_end, realized, unrealized_change,
          daily_pnl, return_pct(None 가능), price_estimated}]
    """
    rows = sorted(trades, key=lambda t: t.get("timestamp") or "")
    ordered = sorted(days)
    out: list[dict] = []
    prev_equity: int | None = None

    for date in ordered:
        day_end = date + "T99"  # 'YYYY-MM-DDT...' 보다 항상 큼 → 당일 포함
        before = [t for t in rows if (t.get("timestamp") or "") < (date + "T00:00:00")]
        upto = [t for t in rows if (t.get("timestamp") or "") < day_end]

        led_before = BotLedger.from_trades(budget, before)
        led_end = BotLedger.from_trades(budget, upto)

        val_before, est_b = _value_positions(led_before.positions, date, close_price)
        val_end, est_e = _value_positions(led_end.positions, date, close_price)
        equity_before = led_before.cash + val_before   # 이전 보유분을 당일 종가로 평가
        equity_end = led_end.cash + val_end

        base = prev_equity if prev_equity is not None else equity_before
        daily_pnl = equity_end - base
        return_pct = (daily_pnl / base * 100) if base > 0 else None
        realized = led_end.realized_pnl - led_before.realized_pnl

        out.append({
            "date": date,
            "equity_start": base,
            "equity_end": equity_end,
            "realized": realized,
            "unrealized_change": daily_pnl - realized,
            "daily_pnl": daily_pnl,
            "return_pct": return_pct,
            "price_estimated": est_b or est_e,
        })
        prev_equity = equity_end

    return out


def period_return(daily: list[dict]) -> dict:
    """일별 결과 → 기간 누적손익 + 시간가중 복리수익률(TWR).

    return_pct가 None인 날은 복리에서 제외.
    """
    cum_pnl = sum(int(d.get("daily_pnl") or 0) for d in daily)
    factor = 1.0
    n = 0
    eq_sum = 0
    for d in daily:
        rp = d.get("return_pct")
        if rp is not None:
            factor *= (1 + rp / 100)
            n += 1
        if d.get("equity_start"):
            eq_sum += d["equity_start"]
    twr_pct = (factor - 1) * 100 if n else 0.0
    avg_equity = eq_sum // len(daily) if daily else 0
    return {"cum_pnl": cum_pnl, "twr_pct": twr_pct, "avg_equity": avg_equity}
```

주의: 테스트 `test_carried_position_next_day_unrealized_change`에서 d0의 base는 prev_equity가 없으므로 equity_before(=100,000), d1의 base는 prev_equity(=d0 equity_end=100,500). 위 구현이 이를 만족하는지 확인하라. `test_zero_budget_bot_with_holding_has_valid_return`에서 d0 base=equity_before(orphan 보유 55,000)이어야 한다 — before는 6/10 orphan-assign buy 포함(< 6/12), 6/12 종가로 평가.

- [ ] **Step 4: PASS 확인**

Run: `.venv/bin/python -m pytest tests/test_bot_equity.py -v`
Expected: 8 passed

- [ ] **Step 5: 커밋**

```bash
git add src/services/bot_equity.py tests/test_bot_equity.py
git commit -m "feat(equity): daily equity reconstruction for fair bot comparison"
```

---

### Task 2: 종가 캐시 헬퍼 (routes)

**Files:**
- Modify: `src/web/routes.py` (기존 `_fetch_benchmark_daily` 근처)
- Test: `tests/test_performance_endpoints.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_performance_endpoints.py` 생성:

```python
"""성과 엔드포인트 — equity 기반 정렬·수익률. KISMarket mock."""

from unittest.mock import patch


def test_build_close_table_normalizes_dates():
    from src.web.routes import _build_close_table
    # get_daily_price는 date를 'YYYYMMDD'로 준다 → 'YYYY-MM-DD'로 정규화
    fake = {"AAA": [{"date": "20260612", "close": 1100},
                    {"date": "20260613", "close": 1200}]}
    with patch("src.web.routes._fetch_daily_closes", side_effect=lambda c, n: fake.get(c, [])):
        table = _build_close_table(["AAA"], days=5)
    assert table[("AAA", "2026-06-12")] == 1100
    assert table[("AAA", "2026-06-13")] == 1200
    assert table.get(("AAA", "2026-06-14")) is None
```

- [ ] **Step 2: FAIL 확인**

Run: `.venv/bin/python -m pytest tests/test_performance_endpoints.py::test_build_close_table_normalizes_dates -v`
Expected: `ImportError: cannot import name '_build_close_table'`

- [ ] **Step 3: 구현**

`src/web/routes.py`의 `_fetch_benchmark_daily` 정의 바로 뒤에 추가:

```python
def _fetch_daily_closes(code: str, count: int) -> list[dict]:
    """단일 종목 일봉 — 테스트 monkeypatch 지점."""
    from src.kis.market import KISMarket
    m = KISMarket()
    try:
        return m.get_daily_price(code, period="D", count=count) or []
    finally:
        try:
            m.close()
        except Exception:
            pass


def _build_close_table(codes: list[str], days: int) -> dict:
    """{(code, 'YYYY-MM-DD'): close} 캐시. 코드별 1회 조회.

    get_daily_price의 date는 'YYYYMMDD' → 'YYYY-MM-DD'로 정규화.
    """
    table: dict = {}
    count = max(2, days + 3)  # 주말·여유분
    for code in set(c for c in codes if c):
        for row in _fetch_daily_closes(code, count):
            raw = str(row.get("date") or "")
            if len(raw) == 8:
                norm = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            else:
                norm = raw
            close = int(row.get("close") or 0)
            if norm and close > 0:
                table[(code, norm)] = close
    return table
```

- [ ] **Step 4: PASS 확인**

Run: `.venv/bin/python -m pytest tests/test_performance_endpoints.py::test_build_close_table_normalizes_dates -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/web/routes.py tests/test_performance_endpoints.py
git commit -m "feat(perf): daily close-price cache helper for equity calc"
```

---

### Task 3: `/bot-performance/daily` equity 기반 교체

**Files:**
- Modify: `src/web/routes.py` (`bots_daily_performance`, line ~2517)
- Test: `tests/test_performance_endpoints.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_performance_endpoints.py`에 추가:

```python
def _make_trades_db(rows):
    """get_db() mock — _conn.execute(...).fetchall()와 get_trades 지원."""
    from unittest.mock import MagicMock
    db = MagicMock()
    # bots_daily_performance는 _conn.execute(...).fetchall() 사용
    cur = MagicMock()
    cur.fetchall.return_value = rows
    db._conn.execute.return_value = cur
    return db


def test_bot_performance_sorts_by_return_not_amount():
    """자본 작은 봇이 금액은 적어도 수익률이 높으면 위로."""
    import src.web.routes as routes
    # A: 큰 자본, B: 작은 자본인데 수익률 높음 — 정렬이 %인지 검증
    # 라우트 내부 동작을 통합 검증하기 어려우면, 정렬 키가 twr_pct임을 보장하는
    # 최소 통합: per_bot 결과에 twr_pct가 있고 bots[0]가 더 높은 twr인지.
    # (실제 구현에서 _build_close_table·StrategyManager·benchmark는 patch)
    import asyncio
    rows = [
        {"bot_id": "big", "day": "2026-06-12", "trades": 1, "wins": 1,
         "losses": 0, "pnl": 2000},
        {"bot_id": "small", "day": "2026-06-12", "trades": 1, "wins": 1,
         "losses": 0, "pnl": 1000},
    ]
    db = _make_trades_db(rows)
    trades_for_equity = [
        {"timestamp": "2026-06-12T09:00:00", "side": "buy", "stock_code": "X",
         "quantity": 10, "price": 1000, "source": "big", "success": True},
        {"timestamp": "2026-06-12T10:00:00", "side": "sell", "stock_code": "X",
         "quantity": 10, "price": 1200, "source": "big", "success": True},
        {"timestamp": "2026-06-12T09:00:00", "side": "buy", "stock_code": "Y",
         "quantity": 10, "price": 100, "source": "small", "success": True},
        {"timestamp": "2026-06-12T10:00:00", "side": "sell", "stock_code": "Y",
         "quantity": 10, "price": 200, "source": "small", "success": True},
    ]
    db.get_trades.return_value = trades_for_equity

    class FakeStrat:
        id = "strat-001"
        budget = 300000
        bots = [{"bot_id": "big", "ratio": 0.9},
                {"bot_id": "small", "ratio": 0.1}]

    with (
        patch("src.web.routes.get_db", return_value=db),
        patch("src.web.routes.StrategyManager") as SM,
        patch("src.web.routes._build_close_table", return_value={}),
        patch("src.web.routes._benchmark_daily_returns", return_value={}),
        patch("src.web.routes._benchmark_period_anchor", return_value={}),
        patch("src.web.routes._fetch_benchmark_daily", return_value=[]),
        patch("src.web.routes._fetch_benchmark_quote", return_value={}),
    ):
        SM.return_value.list_strategies.return_value = [FakeStrat()]
        result = asyncio.get_event_loop().run_until_complete(
            routes.bots_daily_performance(days=14, pipe_id="strat-001"))

    # big: +2000 on 270k base ≈ +0.74% ; small: +1000 on 30k base ≈ +3.33%
    # 금액은 big이 크지만 수익률은 small이 높다 → small이 먼저여야 한다
    ids = [b["bot_id"] for b in result["bots"]]
    assert ids[0] == "small", f"수익률 정렬 실패: {ids}"
    assert result["bots"][0]["totals"]["twr_pct"] > result["bots"][1]["totals"]["twr_pct"]
```

주의: 실제 `bots_daily_performance` 시그니처/내부 헬퍼 이름과 위 patch 대상이 일치하는지 먼저 라우트를 읽고 맞춰라. `_build_close_table`이 빈 dict면 cost basis 폴백으로 평가된다 (매도 완료라 보유 0 → equity는 현금만; base는 budget). big base=270,000, small base=30,000으로 계산되는지 확인.

- [ ] **Step 2: FAIL 확인**

Run: `.venv/bin/python -m pytest tests/test_performance_endpoints.py::test_bot_performance_sorts_by_return_not_amount -v`
Expected: FAIL (현재 금액 정렬이라 ids[0]=="big")

- [ ] **Step 3: 구현 — equity 기반으로 교체**

`bots_daily_performance` 내부를 수정한다 (현재 `roi_pct = pnl/budget` 및 `bots.sort(key=pnl)` 부분):

1. 봇별 budget 계산부 뒤에, 봇별 equity 일별 재구성 추가. 각 봇의 거래일(`entry["days"]`의 date)과 보유 코드를 모아 종가 테이블을 한 번 만든다:

```python
    from src.services.bot_equity import reconstruct_daily_equity, period_return

    all_trades = [t for t in db.get_trades(limit=10000)
                  if t.get("success", True) and t.get("source")]
    by_src: dict[str, list] = {}
    for t in all_trades:
        by_src.setdefault(t["source"], []).append(t)

    # equity 평가용 종가 테이블 (봇들이 보유한 모든 코드)
    held_codes = {t.get("stock_code") for t in all_trades if t.get("stock_code")}
    close_table = _build_close_table(list(held_codes), days)
    close_fn = lambda code, date: close_table.get((code, date))
```

2. 각 봇 `entry`에 대해, `entry["days"]`의 날짜로 equity 재구성하고 `roi_pct`·`twr_pct`를 교체:

```python
    for bid, entry in per_bot.items():
        day_dates = sorted(d["date"] for d in entry["days"])
        eq_rows = reconstruct_daily_equity(
            by_src.get(bid, []), entry["budget"], day_dates, close_fn)
        eq_by_date = {r["date"]: r for r in eq_rows}
        # 일별 roi를 equity 기반으로 덮어쓰기
        for d in entry["days"]:
            er = eq_by_date.get(d["date"])
            d["roi_pct"] = round(er["return_pct"], 3) if er and er["return_pct"] is not None else None
            d["equity_start"] = er["equity_start"] if er else 0
        pr = period_return(eq_rows)
        t = entry["totals"]
        t["twr_pct"] = round(pr["twr_pct"], 3)
        t["cum_pnl"] = pr["cum_pnl"]
        t["avg_equity"] = pr["avg_equity"]
        t["roi_pct"] = t["twr_pct"]  # 기간 대표 수익률 = TWR
        t["price_estimated_days"] = sum(1 for r in eq_rows if r["price_estimated"])
        # win_rate 등 기존 계산 유지
        ...
```

기존 `t["roi_pct"] = pnl/budget` 라인과 일별 `roi_pct = pnl/budget` 라인은 제거. alpha 계산은 `t["roi_pct"]`(=twr) 기반이라 그대로 동작하되, 일별 alpha의 `d["roi_pct"]`가 None일 수 있으니 None 가드 추가:

```python
            if br is None or d.get("roi_pct") is None:
                d["alpha_pct"] = None
            else:
                d["alpha_pct"] = round(d["roi_pct"] - br, 3)
```

3. 정렬 교체:

```python
    bots.sort(key=lambda b: b["totals"].get("twr_pct", 0), reverse=True)
```

(기존 `key=lambda b: b["totals"]["pnl"]` 제거)

- [ ] **Step 4: PASS + 회귀**

Run: `.venv/bin/python -m pytest tests/test_performance_endpoints.py tests/test_bot_daily_perf.py -v`
Expected: 신규 PASS + 기존 `test_bot_daily_perf.py`가 깨지면 새 의미론(금액→%)에 맞게 갱신(사유 기록). 그 후 `.venv/bin/python -m pytest tests/ -q`.

- [ ] **Step 5: 커밋**

```bash
git add src/web/routes.py tests/test_performance_endpoints.py
git commit -m "fix(perf): equity-based ROI and TWR sort in bot-performance"
```

---

### Task 4: `/pnl/daily` + 오늘 손익 위젯에 수익률

**Files:**
- Modify: `src/web/routes.py` (`get_daily_pnl`, line ~2730)
- Modify: `src/web/static/index.html` (오늘 손익 위젯, line ~700)
- Test: `tests/test_performance_endpoints.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_performance_endpoints.py`에 추가:

```python
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
        patch("src.web.routes.get_db", return_value=db),
        patch("src.web.routes.StrategyManager") as SM,
        patch("src.web.routes._build_close_table", return_value={}),
    ):
        SM.return_value.list_strategies.return_value = [FakeStrat()]
        result = asyncio.get_event_loop().run_until_complete(
            routes.get_daily_pnl(date="2026-06-12"))

    bots = result["bots"]
    assert all("return_pct" in b for b in bots)
    # small 수익률(+3.33%) > big(+0.74%) → small 먼저
    assert bots[0]["source"] == "small"
```

- [ ] **Step 2: FAIL 확인**

Run: `.venv/bin/python -m pytest tests/test_performance_endpoints.py::test_daily_pnl_includes_return_pct_and_sorts_by_it -v`
Expected: FAIL (`return_pct` 키 없음 / 금액 정렬)

- [ ] **Step 3: 구현 — get_daily_pnl에 return_pct**

`get_daily_pnl` 내부. 각 봇 realized 계산 뒤, 당일 equity 기준 수익률 추가. 봇 budget은 StrategyManager에서, 종가는 `_build_close_table`로:

```python
    from src.services.bot_equity import reconstruct_daily_equity
    from src.strategy import StrategyManager

    # 봇별 allocated budget
    bot_budget: dict[str, int] = {}
    try:
        for s in StrategyManager().list_strategies():
            for b in (s.bots or []):
                bid = b.get("bot_id", "")
                if bid:
                    bot_budget[bid] = bot_budget.get(bid, 0) + int(s.budget * float(b.get("ratio", 0) or 0))
    except Exception:
        pass

    held = {t.get("stock_code") for t in trades if t.get("stock_code")}
    close_table = _build_close_table(list(held), days=2)
    close_fn = lambda code, date: close_table.get((code, date))
```

각 source 루프에서 `realized` 계산 직후:

```python
        eq = reconstruct_daily_equity(rows, bot_budget.get(src, 0), [date], close_fn)
        ret = eq[0]["return_pct"] if eq else None
        bots.append({
            "source": src,
            "realized": realized,
            "return_pct": round(ret, 3) if ret is not None else None,
            "buys": buys,
            "sells": sells,
        })
```

정렬 교체 (line ~2777):

```python
    bots.sort(key=lambda b: (b["return_pct"] is not None, b["return_pct"] or 0), reverse=True)
```

(기존 `key=lambda b: b["realized"]` 제거)

- [ ] **Step 4: UI — 오늘 손익 위젯에 % 표시**

`src/web/static/index.html`의 오늘 손익 봇 행 렌더 (line ~703):

```javascript
    `<span style="color:${pnlColor(b.realized)};font-weight:600">${pnlFmt(b.realized)}원</span>` +
    (b.return_pct != null ? ` <span style="color:${pnlColor(b.return_pct)};font-size:12px">(${b.return_pct > 0 ? '+' : ''}${b.return_pct}%)</span>` : '') +
    ` <span style="color:var(--muted);font-size:11px">${b.source} ${b.buys}매수/${b.sells}매도</span>`
```

(기존 행 템플릿의 위치·클래스에 맞춰 조정 — 먼저 현재 렌더 코드를 읽고 일치시켜라.)

- [ ] **Step 5: PASS + JS 체크 + 회귀**

Run: `.venv/bin/python -m pytest tests/test_performance_endpoints.py tests/test_daily_pnl.py -v`
기존 `test_daily_pnl.py`가 정렬 의미론 변경으로 깨지면 갱신. JS: inline 스크립트 추출 후 `node --check`. 그 후 `.venv/bin/python -m pytest tests/ -q`.

- [ ] **Step 6: 커밋**

```bash
git add src/web/routes.py src/web/static/index.html tests/test_performance_endpoints.py
git commit -m "fix(pnl): per-bot daily return_pct in pnl endpoint and widget"
```

---

### Task 5: 통합 검증 + 실DB 스모크

**Files:** 없음 (검증)

- [ ] **Step 1: 전체 테스트** — `.venv/bin/python -m pytest tests/ -q` 전체 PASS

- [ ] **Step 2: 실 엔드포인트 스모크** (서버 실행 중일 때)

```bash
.venv/bin/python -c "
from fastapi.testclient import TestClient
from src.web.app import app
c = TestClient(app)
d = c.get('/api/pnl/daily').json()
print('pnl 정렬:', [(b['source'], b['realized'], b.get('return_pct')) for b in d['bots']])
p = c.get('/api/bot-performance/daily?days=14').json()
print('perf 정렬:', [(b['bot_id'], b['totals'].get('twr_pct'), b['totals']['pnl']) for b in p['bots']])"
```
Expected: 두 목록 모두 **수익률(%) 내림차순**. mirinae-2(ratio 0%)가 0%가 아니라 실제 보유 수익률로 표시. 금액 순서와 % 순서가 다를 수 있음(그게 정상 — 버그 수정의 증거).

- [ ] **Step 3: 브랜치 마무리** — superpowers:finishing-a-development-branch

---

## Self-Review 메모
- 스펙 커버: equity 정의(T1), 일별 수익률(T1), 0/없음 처리(T1), TWR 기간(T1), 종가 폴백(T1), 두 엔드포인트 %교체·정렬(T3·T4), UI %(T4), mirinae-2 0% 회귀(T1 `test_zero_budget_bot...`) — 전부 매핑
- 시그니처 일관: `reconstruct_daily_equity(trades, budget, days, close_price)`, `period_return(daily)`, `_build_close_table(codes, days)`, `_fetch_daily_closes(code, count)` — 태스크 전반 동일
- 영속 스냅샷 테이블은 스펙대로 범위 제외
