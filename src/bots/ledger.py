"""BotLedger — bot의 자기 거래 이력에서 cash/positions/pnl을 계산.

봇은 독립 가상 계좌. 자기 trades(source=bot_id)만 본다.
다른 봇의 보유, 파이프 집계는 보지 않는다.
"""

from dataclasses import dataclass, field


@dataclass
class Lot:
    """한 번의 매수로 생긴 보유 lot. FIFO 매도 시 차감."""
    qty: int
    price: int


@dataclass
class BotLedger:
    """봇의 가상 계좌 상태. trades에서 결정론적으로 재계산 가능."""

    budget: int
    cash: int = 0
    realized_pnl: int = 0
    positions: list[dict] = field(default_factory=list)

    @classmethod
    def from_trades(cls, budget: int, trades: list[dict]) -> "BotLedger":
        """체결 이력을 시간순 누적해 cash/positions/pnl을 계산."""
        cash = budget
        realized = 0
        lots: dict[str, list[Lot]] = {}

        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", ""))
        for trade in sorted_trades:
            if not trade.get("success", True):
                continue
            code = trade.get("stock_code", "")
            qty = int(trade.get("quantity", 0) or 0)
            price = int(trade.get("price", 0) or 0)
            if not code or qty <= 0:
                continue

            side = trade.get("side", "")
            reason = trade.get("reason", "") or ""
            if side == "buy":
                # orphan-assign: 봇 ledger에 lot만 추가하고 cash는 차감하지 않는다.
                # 브로커에 이미 존재하는 "주인 없는 주식"을 봇에 귀속시키는 attribution.
                if reason != "orphan-assign":
                    cash -= qty * price
                lots.setdefault(code, []).append(Lot(qty=qty, price=price))
            elif side == "sell":
                remaining = qty
                # phantom-cleanup: remove lots but do not credit cash or realize pnl.
                # Used by the phantom-cleanup sweep to reconcile bot ledger against
                # KIS reality without distorting cash or realized_pnl.
                is_cleanup = reason == "phantom-cleanup"
                for lot in lots.get(code, []):
                    if remaining <= 0:
                        break
                    take = min(remaining, lot.qty)
                    if not is_cleanup:
                        realized += (price - lot.price) * take
                        cash += take * price
                    lot.qty -= take
                    remaining -= take

        positions = []
        for code, code_lots in lots.items():
            qty = sum(lot.qty for lot in code_lots)
            if qty <= 0:
                continue
            cost = sum(lot.qty * lot.price for lot in code_lots)
            avg_price = cost // qty if qty > 0 else 0
            remaining_lots = [{"qty": lot.qty, "price": lot.price}
                              for lot in code_lots if lot.qty > 0]
            positions.append({"code": code, "qty": qty, "avg_price": avg_price,
                              "lots": remaining_lots})

        return cls(budget=budget, cash=cash, realized_pnl=realized, positions=positions)
