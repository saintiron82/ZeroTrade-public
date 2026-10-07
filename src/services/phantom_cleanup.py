"""Phantom-cleanup service.

Reconciles per-bot ledgers (computed from trades source=bot_id) against pipe
holdings (KIS truth). When the sum of bot-claimed shares exceeds the pipe's
real holding for a stock, the older buyers' unmatched lots are treated as
phantoms and a synthetic SELL row with reason='phantom-cleanup' is written.

BotLedger.from_trades recognizes the 'phantom-cleanup' reason and removes the
matching lot without affecting cash or realized_pnl. So after cleanup:

    sum(BotLedger.positions across bots) == pipe holdings  (per stock)

The cleanup is idempotent — running it again with the same data produces no
new rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from src.bots.ledger import BotLedger
from src.strategy import StrategyManager

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _kst_iso() -> str:
    return datetime.now(KST).isoformat()


def cleanup_pipe(pipe_id: str,
                 pipe_holdings: dict[str, int] | None = None,
                 kis_balance: dict[str, int] | None = None) -> dict:
    """Sweep phantom positions for one pipe.

    Args:
        pipe_id: pipe identifier (e.g. "strat-001").
        pipe_holdings: {stock_code: qty} pipe-recorded holdings (may be stale).
        kis_balance: {stock_code: qty} actual broker balance (broker truth).
            When provided, it wins over pipe_holdings AND propagates back to
            strategies.json via StrategyManager.sync_holdings so the display
            stops lying about what we own.

    Returns:
        {"pipe_id": ..., "cleanups": [{"source", "code", "qty", "price"}]}.
    """
    from src.db import get_db

    db = get_db()
    pipe_holdings = {k: int(v or 0) for k, v in (pipe_holdings or {}).items()}
    kis_truth = None
    if kis_balance is not None:
        kis_truth = {k: int(v or 0) for k, v in kis_balance.items()}
        # If KIS doesn't list a code that pipe thinks it holds, KIS truth = 0.
        for code in pipe_holdings:
            kis_truth.setdefault(code, 0)
        # KIS truth wins
        truth = kis_truth
    else:
        truth = pipe_holdings

    # Sync pipe.holdings to KIS truth (display + downstream ledger anchor).
    if kis_truth is not None:
        try:
            StrategyManager().sync_holdings(pipe_id, dict(kis_truth))
        except Exception as e:
            logger.warning("sync_holdings 실패: %s", e)

    pipe_holdings = truth

    trades = db.get_trades(pipe_id=pipe_id, limit=10000)
    # Group by source
    by_source: dict[str, list[dict]] = {}
    for t in trades:
        src = t.get("source") or ""
        if not src:
            continue
        by_source.setdefault(src, []).append(t)

    # Compute current per-(source, code) qty after applying existing
    # phantom-cleanup rows (BotLedger already respects them).
    bot_positions: dict[str, dict[str, dict]] = {}  # source -> code -> {qty, lots}
    for source, src_trades in by_source.items():
        ledger = BotLedger.from_trades(budget=0, trades=src_trades)
        for pos in ledger.positions:
            bot_positions.setdefault(source, {})[pos["code"]] = pos

    # Collect every code that any bot claims OR pipe holds
    codes = set(pipe_holdings.keys())
    for code_map in bot_positions.values():
        codes.update(code_map.keys())

    cleanups: list[dict] = []
    for code in codes:
        # Bots that currently claim this code, with their most-recent buy timestamp
        claimants = []  # (latest_buy_ts, source, pos)
        for source, code_map in bot_positions.items():
            pos = code_map.get(code)
            if not pos or pos["qty"] <= 0:
                continue
            # Find this source's most-recent buy for this code
            latest_ts = ""
            for t in by_source.get(source, []):
                if t.get("side") == "buy" and t.get("stock_code") == code and t.get("success", 1):
                    ts = t.get("timestamp", "")
                    if ts > latest_ts:
                        latest_ts = ts
            claimants.append((latest_ts, source, pos))

        if not claimants:
            continue

        total_claimed = sum(p["qty"] for _, _, p in claimants)
        truth = pipe_holdings.get(code, 0)
        excess = total_claimed - truth
        if excess <= 0:
            continue

        # Allocate truth to most-recent buyers; older buyers' shares become phantom.
        claimants.sort(key=lambda x: x[0])  # ascending: oldest first
        remaining_excess = excess
        for latest_ts, source, pos in claimants:
            if remaining_excess <= 0:
                break
            to_clean = min(remaining_excess, pos["qty"])
            # Lots are FIFO oldest-first inside the position; pop from oldest
            # to mirror the BotLedger's FIFO sell logic.
            lots = list(pos.get("lots") or [])
            qty_left = to_clean
            for lot in lots:
                if qty_left <= 0:
                    break
                lot_qty = int(lot.get("qty", 0))
                lot_price = int(lot.get("price", 0))
                take = min(qty_left, lot_qty)
                if take <= 0:
                    continue
                db.log_trade(
                    pipe_id=pipe_id,
                    stock_code=code,
                    stock_name="",
                    side="sell",
                    quantity=take,
                    price=lot_price,
                    source=source,
                    reason="phantom-cleanup",
                    success=True,
                )
                cleanups.append({
                    "source": source,
                    "code": code,
                    "qty": take,
                    "price": lot_price,
                })
                qty_left -= take
                remaining_excess -= take

    if cleanups:
        _invalidate_bot_state(db, pipe_id, cleanups)
        logger.warning("[phantom-cleanup] %s: %d 정리 — %s",
                       pipe_id, len(cleanups), cleanups)
    return {"pipe_id": pipe_id, "cleanups": cleanups}


def _invalidate_bot_state(db, pipe_id: str, cleanups: list[dict]) -> None:
    """Strip cleaned codes from each affected bot's cached bot_state.

    After this, the bot's next _load_state → _reconcile_with_trades will rebuild
    holdings from the now-balanced ledger. realized_pnl and scan_count are kept.
    """
    import json
    by_source: dict[str, set[str]] = {}
    for c in cleanups:
        by_source.setdefault(c["source"], set()).add(c["code"])

    for source, codes in by_source.items():
        state_key = f"{pipe_id}:{source}"
        row = db._conn.execute(
            "SELECT * FROM bot_state WHERE bot_id=?", (state_key,)
        ).fetchone()
        if not row:
            continue
        state = dict(row)

        # Clear single-position fields when they point to a cleaned code
        if state.get("holding_code") in codes:
            state["holding_code"] = ""
            state["holding_qty"] = 0
            state["holding_price"] = 0

        # Filter cleaned codes from multi-position cache (mirinae bots)
        try:
            extra = json.loads(state.get("extra") or "{}")
        except json.JSONDecodeError:
            extra = {}
        if isinstance(extra, dict) and isinstance(extra.get("positions"), list):
            extra["positions"] = [p for p in extra["positions"]
                                  if p.get("code") not in codes]

        db._conn.execute(
            """UPDATE bot_state SET holding_code=?, holding_qty=?, holding_price=?,
               extra=?, updated_at=? WHERE bot_id=?""",
            (state.get("holding_code", ""),
             state.get("holding_qty", 0),
             state.get("holding_price", 0),
             json.dumps(extra, ensure_ascii=False, default=str),
             _kst_iso(),
             state_key),
        )
    db._conn.commit()


def cleanup_all_pipes() -> list[dict]:
    """Run cleanup for every pipe in strategies.json. Returns per-pipe reports."""
    from src.strategy import StrategyManager

    mgr = StrategyManager()
    reports = []
    for strat in mgr.list_strategies():
        holdings = {h.get("code", ""): int(h.get("qty", 0) or 0)
                    for h in (strat.holdings or [])}
        holdings = {k: v for k, v in holdings.items() if k}
        reports.append(cleanup_pipe(pipe_id=strat.id, pipe_holdings=holdings))
    return reports


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    reports = cleanup_all_pipes()
    print(json.dumps(reports, ensure_ascii=False, indent=2))
