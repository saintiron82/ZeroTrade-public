"""봇 자기자본(equity) 일별 재구성 — 성과 비교용.

순수 함수: trades + 종가 콜백만 받는다 (네트워크/DB 없음).
equity = 현금 + 보유가치(종가×수량). 일별 수익률 = equity 변화 / 시작 equity.
자본 규모가 다른 봇을 공정 비교하기 위한 분모 정규화.
스펙: docs/superpowers/specs/2026-06-15-equity-based-performance.md

한계: phantom-cleanup 발생일에는 lot이 cash/realized 갱신 없이 제거되므로
그 보유분 평가액이 미실현 손실로 귀속될 수 있음(드묾 — 잔고 정정 sweep 시).
환영 정정은 실제 손익이 아니나 equity 체인 상에서는 그날 daily_pnl을 깎는다.
"""

from typing import Callable

from src.bots.ledger import BotLedger


def _prev_close(code: str, date: str,
                close_price: Callable[[str, str], int | None]) -> int | None:
    """date 직전 영업일 종가 — 최대 7일 역탐색, 없으면 None.

    순수: 주어진 날짜 문자열 산술만 한다 (now() 아님).
    """
    from datetime import datetime, timedelta
    d = datetime.strptime(date, "%Y-%m-%d")
    for back in range(1, 8):
        px = close_price(code, (d - timedelta(days=back)).strftime("%Y-%m-%d"))
        if px and px > 0:
            return px
    return None


def _value_positions_prior(positions: list[dict], date: str,
                           close_price: Callable[[str, str], int | None]) -> tuple[int, bool]:
    """이월 보유분을 직전 영업일 종가로 평가 → (시장가치, 추정여부).

    윈도우 첫날 base 산정용. 전일 종가 없으면 평단(cost basis)으로 폴백하고
    estimated=True. 당일(D) 종가가 아니라 D-1 종가를 써서 첫날 미실현 변동이
    base·end에서 상쇄되어 사라지는 버그를 막는다.
    """
    total = 0
    estimated = False
    for p in positions:
        px = _prev_close(p["code"], date, close_price)
        if not px or px <= 0:
            px = int(p.get("avg_price") or 0)
            estimated = True
        total += int(p.get("qty") or 0) * int(px)
    return total, estimated


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


def _net_flow(day_trades: list[dict], date: str,
              close_price: Callable[[str, str], int | None]) -> int:
    """그날의 합성 자본 흐름(net inflow) — equity 평가가 기준.

    orphan-assign buy: 현금 차감 없이 보유분만 +주입 → equity가 그만큼 올라가나
        매매 손익이 아니므로 +value 흐름으로 분류 (daily_pnl에서 차감).
    phantom-cleanup sell: 현금 가산 없이 보유분만 -제거 → equity가 그만큼 내려가나
        매매 손익이 아니므로 -value 흐름으로 분류 (daily_pnl에서 가산).

    평가가격(px)은 equity_end 평가와 일관되게 close(code, date)를 쓰고,
    없으면 행의 체결가(cost basis)로 폴백한다.
    """
    flow = 0
    for t in day_trades:
        if not t.get("success", True):
            continue
        reason = t.get("reason", "") or ""
        side = t.get("side", "")
        code = t.get("stock_code", "")
        qty = int(t.get("quantity", 0) or 0)
        if not code or qty <= 0:
            continue
        if not (
            (side == "buy" and reason == "orphan-assign")
            or (side == "sell" and reason == "phantom-cleanup")
        ):
            continue
        px = close_price(code, date)
        if not px or px <= 0:
            px = int(t.get("price", 0) or 0)
        value = qty * int(px)
        if side == "buy":
            flow += value   # 주입 (inflow)
        else:
            flow -= value   # 제거 (outflow)
    return flow


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
        day_start = date + "T00:00:00"
        day_end = date + "T99"  # 'YYYY-MM-DDT...' 보다 항상 큼 → 당일 포함
        before = [t for t in rows if (t.get("timestamp") or "") < day_start]
        upto = [t for t in rows if (t.get("timestamp") or "") < day_end]
        day_trades = [t for t in rows
                      if day_start <= (t.get("timestamp") or "") < day_end]

        led_before = BotLedger.from_trades(budget, before)
        led_end = BotLedger.from_trades(budget, upto)

        val_end, est_e = _value_positions(led_end.positions, date, close_price)
        equity_end = led_end.cash + val_end

        if prev_equity is not None:
            # 둘째 날부터: base = 전날 end (이미 정확한 체인)
            base = prev_equity
            est_b = False
        else:
            # 윈도우 첫날: 이월 보유분을 직전 영업일(D-1) 종가로 평가
            val_before, est_b = _value_positions_prior(
                led_before.positions, date, close_price)
            base = led_before.cash + val_before

        # 합성 자본 흐름(orphan-assign 주입 / phantom-cleanup 제거)은 매매 손익이
        # 아니므로 numerator(daily_pnl)에서 차감한다 (Modified-Dietz 흐름 처리).
        # 보유분 자체는 equity_start/end(분모)에 그대로 포함된다.
        net_flow = _net_flow(day_trades, date, close_price)
        daily_pnl = (equity_end - base) - net_flow
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
        if d.get("equity_start", 0) > 0:
            eq_sum += d["equity_start"]
    twr_pct = (factor - 1) * 100 if n else 0.0
    avg_equity = eq_sum // len(daily) if daily else 0
    return {"cum_pnl": cum_pnl, "twr_pct": twr_pct, "avg_equity": avg_equity}
