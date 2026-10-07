"""미리내 1호 — 멀티 에이전트 AI 트레이딩 봇.

AI CLI (Codex/Claude/Copilot 선택 가능) + MCP 도구를 사용하여 강세/약세 에이전트가 토론하고,
트레이더가 최종 결정을 내리는 구조.

5분 간격으로 실행:
1. _collect_data() -- Python이 KIS API로 시세/뉴스/잔고 수집 → /tmp/mirinae_data.json 저장
2. _invoke_codex() -- AI CLI 호출, 수집된 데이터를 프롬프트에 포함하여 분석
3. _parse_decision() -- AI 출력에서 JSON 결정 파싱
4. _execute_decision() -- confidence 0.7+ 이면 실행
"""

import asyncio
import json
import logging
import re
from datetime import datetime, time as dtime
from pathlib import Path

from src.bots.base import BotBase, BotConfig
from src.bots.safeguards import (
    kst_now,
    TrailingStop,
    DailyTradeLimit,
    MinHoldTime,
    MarketCloseGuard,
    load_bot_config,
    write_bot_log,
)
from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DB_PATH = PROJECT_ROOT / "data" / "ff.db"

DEFAULT_ETF_CODES = [
    "069500", "091160", "091170", "091180", "102110", "102780",
    "102970", "114800", "117460", "117680", "117700", "122630",
    "123310", "123320", "130680", "132030", "133690", "139260", "305720",
]

# 기본 파라미터 (에볼버가 갱신 가능)
DEFAULT_PARAMS = {
    "interval_seconds": 1800,  # 30분
    "confidence_threshold": 0.7,
    "max_positions": 3,
    "trailing_stop_pct": -1.5,
    "take_profit_pct": 2.0,
    "hard_stop_pct": -2.5,
    "loss_review_trend_pct": -0.15,
    "loss_breakdown_trend_pct": -0.35,
    "min_plan_risk_reward": 1.2,
    "max_daily_trades": 5,
    "min_hold_minutes": 10,
    "codex_timeout": 180,
    "ai_provider": "codex",
    "ai_model": "",
    "atr_multiplier": 2.0,
    "consecutive_loss_block": 2,  # 같은 종목 N연속 손절 시 당일 재진입 금지 (0=비활성)
    "symbol_blocklist": [],       # 매수 금지 종목 코드 (매도는 허용)
}


class MirinaeBot(BotBase):
    """미리내 1호 -- 멀티 에이전트 AI 트레이딩 봇."""

    BOT_ID = "mirinae"

    def __init__(self, config: BotConfig | dict = None):
        # 에볼버 파라미터 로드 (기본값 위에 덮어쓰기)
        self._params = {**DEFAULT_PARAMS}
        evolver_cfg = load_bot_config("mirinae")
        self._params.update(evolver_cfg)

        # 가드 설정 (super().__init__ 전에 설정해야 TradeEngine에 전달됨)
        # 봇 독립 4원칙: 거래내역 기준 관리. 봇간 종목/섹터 교차 차단은 OFF.
        self._guard_config = {
            "daily_loss_limit_pct": -2,
            "market_drop_block_pct": -1.0,
            "trend_filter_enabled": True,
            "duplicate_check_enabled": False,
            "max_sector_exposure": 0,
        }

        # BotConfig에 interval_seconds 반영
        if isinstance(config, dict):
            config.setdefault("interval_seconds", self._params["interval_seconds"])
        elif config is None:
            config = BotConfig(interval_seconds=self._params["interval_seconds"])
        else:
            if config.interval_seconds == 300:
                config.interval_seconds = self._params["interval_seconds"]

        super().__init__(config)

        if not self._config.etf_codes:
            self._config.etf_codes = DEFAULT_ETF_CODES.copy()

        # 안전장치 초기화
        self._daily_limit = DailyTradeLimit(max_trades=self._params["max_daily_trades"])
        self._market_guard = MarketCloseGuard()

        # 다중 포지션 관리 (최대 3)
        # [{code, name, qty, price, bought_at, trailing_stop, hold_timer, target_price, stop_loss}, ...]
        self._positions: list[dict] = []

        # 연속 손절 추적 (당일, 종목별) — _save_state/_restore_positions로 영속화
        self._daily_losses: dict[str, int] = {}
        self._daily_losses_date: str = ""

        # AI 분석 결과 (UI용)
        self._last_analysis: dict = {}
        self._debate_log: list[dict] = []
        self._last_validation: dict = {}
        self._last_preflight: dict = {}
        self._last_position_reviews: list[dict] = []
        self._last_ai_error = ""
        self._last_raw_output_preview = ""
        self._last_ai_call_meta: dict = {}
        self._last_collected_data: dict = {}
        self._last_preflight_board_key = ""

        # DB에서 포지션 복원
        self._restore_positions()

    # ===========================================
    # 포지션 영속화
    # ===========================================

    def _restore_positions(self):
        """DB에서 다중 포지션 복원."""
        # 서브클래스가 MirinaeBot.__init__을 우회해도 안전하게 초기화
        if not hasattr(self, "_daily_losses"):
            self._daily_losses = {}
            self._daily_losses_date = ""
        try:
            from src.db import get_db
            state = get_db().load_bot_state(self.state_key)
            if state:
                # 당일 연속 손실 카운트 복원 (재시작 시 차단 상태 유지)
                self._daily_losses = {
                    str(k): int(v)
                    for k, v in (state.get("daily_losses") or {}).items()
                }
                self._daily_losses_date = str(state.get("daily_losses_date", "") or "")
            if state and state.get("positions"):
                for pos in state["positions"]:
                    trailing = TrailingStop(
                        stop_pct=self._params["trailing_stop_pct"],
                        take_profit_pct=self._params["take_profit_pct"],
                        hard_stop_pct=self._params["hard_stop_pct"],
                    )
                    trailing.activate(pos["price"])
                    hold_timer = MinHoldTime(min_minutes=self._params["min_hold_minutes"])
                    if pos.get("bought_at"):
                        hold_timer._entry_time = datetime.fromisoformat(pos["bought_at"])
                    self._positions.append({
                        "code": pos["code"],
                        "name": pos.get("name", ""),
                        "qty": pos["qty"],
                        "price": pos["price"],
                        "bought_at": pos.get("bought_at", ""),
                        "entry_time": datetime.fromisoformat(pos["entry_time"]) if pos.get("entry_time") else None,
                        "trailing_stop": trailing,
                        "hold_timer": hold_timer,
                        "target_price": pos.get("target_price", 0),
                        "stop_loss": pos.get("stop_loss", 0),
                        "audit_outcome_run_id": pos.get("audit_outcome_run_id", ""),
                        "audit_buy_trade_id": pos.get("audit_buy_trade_id", 0),
                        "audit_outcome_runs": pos.get("audit_outcome_runs", []),
                    })
                logger.info(
                    "[%s] %d개 포지션 복원: %s",
                    self.BOT_ID,
                    len(self._positions),
                    ", ".join(p["code"] for p in self._positions),
                )
        except Exception:
            pass
        self._reconcile_positions_with_trades()

    def _new_restored_position(self, code: str, name: str, qty: int, price: int) -> dict:
        trailing = TrailingStop(
            stop_pct=self._params["trailing_stop_pct"],
            take_profit_pct=self._params["take_profit_pct"],
            hard_stop_pct=self._params["hard_stop_pct"],
        )
        trailing.activate(price)
        hold_timer = MinHoldTime(min_minutes=self._params["min_hold_minutes"])
        # 복원 시점을 bought_at으로 사용. 빈 값이면 max_hold 시간제한 검사가
        # 스킵되어 "잊혀진 다일 보유"의 원인이 됨 — 복원 시각 기준 카운트.
        now_iso = kst_now().isoformat()
        return {
            "code": code,
            "name": name,
            "qty": qty,
            "price": price,
            "bought_at": now_iso,
            "entry_time": kst_now(),
            "trailing_stop": trailing,
            "hold_timer": hold_timer,
            "target_price": 0,
            "stop_loss": 0,
            "restored_from_trades": True,
            "audit_outcome_runs": [],
        }

    @staticmethod
    def _latest_lots_for_qty(lots: list[dict], qty: int) -> list[dict]:
        """Return the newest remaining lots needed to represent current holdings.

        Older trade logs can contain stale lots from prior state mismatches. When
        the pipe ledger says fewer shares are currently held than this bot's raw
        trade history implies, the active position should be priced from the most
        recent remaining buys, not averaged with stale historical lots.
        """
        selected = []
        remaining = max(0, int(qty or 0))
        for lot in reversed(lots):
            if remaining <= 0:
                break
            lot_qty = int(lot.get("qty", 0) or 0)
            lot_price = int(lot.get("price", 0) or 0)
            if lot_qty <= 0 or lot_price <= 0:
                continue
            take = min(remaining, lot_qty)
            selected.append({"qty": take, "price": lot_price})
            remaining -= take
        return list(reversed(selected))

    def _reconcile_positions_with_trades(self):
        """본인 거래 이력만으로 잔여 포지션을 복원. 파이프/다른 봇 무시.

        봇은 독립 가상 계좌. 같은 종목을 다른 봇이 보유해도 영향 없음.
        BotLedger가 단일 진실원천.
        """
        pipe_id = self._get_pipe_id()
        if not pipe_id:
            return
        try:
            from src.db import get_db
            from src.bots.ledger import BotLedger
            from src.strategy import stock_name_for

            trades = get_db().get_trades(pipe_id=pipe_id, source=self.BOT_ID, limit=500)
            if not trades:
                return

            ledger = BotLedger.from_trades(budget=self._config.budget, trades=trades)
            rebuilt = {pos["code"]: pos for pos in ledger.positions}
            # rebuilt가 비어 있어도 self._positions를 정리해야 한다.
            # 그렇지 않으면 phantom-cleanup 이후에도 in-memory에 phantom이 잔존.
            if not rebuilt and not self._positions:
                return

            current_by_code = {p["code"]: p for p in self._positions}
            changed = False
            next_positions = []
            for code, restored in rebuilt.items():
                restored_qty = restored["qty"]
                restored_price = restored["avg_price"]
                name = stock_name_for(code)
                pos = current_by_code.get(code)
                if pos:
                    if (pos.get("qty") != restored_qty
                        or not pos.get("name")
                        or int(pos.get("price", 0) or 0) != restored_price):
                        pos["qty"] = restored_qty
                        pos["name"] = pos.get("name") or name
                        if restored_price > 0:
                            pos["price"] = restored_price
                        if pos.get("trailing_stop"):
                            pos["trailing_stop"].entry_price = pos["price"]
                        changed = True
                    next_positions.append(pos)
                else:
                    next_positions.append(self._new_restored_position(
                        code=code, name=name, qty=restored_qty, price=restored_price))
                    changed = True

            if len(next_positions) != len(self._positions):
                changed = True
            self._positions = next_positions
            if changed:
                logger.warning(
                    "[%s] 거래 이력 기준 포지션 복원/동기화: %s",
                    self.BOT_ID,
                    ", ".join(f"{p['code']} {p['qty']}주" for p in self._positions),
                )
                self._save_state()
        except Exception as e:
            logger.debug("[%s] reconcile 실패: %s", self.BOT_ID, e)

    def _sync_single_holding_fields(self):
        """Keep legacy single-position fields aligned with multi-position state."""
        if not self._positions:
            self._holding_code = ""
            self._holding_qty = 0
            self._holding_price = 0
            self._holding_entry_time = None
            return

        primary = max(self._positions, key=lambda p: int(p.get("qty", 0) or 0))
        self._holding_code = primary.get("code", "")
        self._holding_qty = int(primary.get("qty", 0) or 0)
        self._holding_price = int(primary.get("price", 0) or 0)
        self._holding_entry_time = primary.get("entry_time")

    def _save_state(self):
        """봇 상태를 DB에 저장 (다중 포지션 포함)."""
        try:
            from src.db import get_db
            self._sync_single_holding_fields()
            positions_data = []
            for pos in self._positions:
                positions_data.append({
                    "code": pos["code"],
                    "name": pos.get("name", ""),
                    "qty": pos["qty"],
                    "price": pos["price"],
                    "bought_at": pos.get("bought_at", ""),
                    "entry_time": pos["entry_time"].isoformat() if pos.get("entry_time") else None,
                    "target_price": pos.get("target_price", 0),
                    "stop_loss": pos.get("stop_loss", 0),
                    "audit_outcome_run_id": pos.get("audit_outcome_run_id", ""),
                    "audit_buy_trade_id": pos.get("audit_buy_trade_id", 0),
                    "audit_outcome_runs": pos.get("audit_outcome_runs", []),
                })

            get_db().save_bot_state(self.state_key, {
                "holding_code": self._holding_code,
                "holding_qty": self._holding_qty,
                "holding_price": self._holding_price,
                "realized_pnl": self._realized_pnl,
                "scan_count": self._scan_count,
                "positions": positions_data,
                "last_analysis": self._last_analysis or {},
                "last_preflight": self._last_preflight,
                "position_reviews": self._last_position_reviews,
                "debate_log": self._debate_log[-20:],
                "recent_trades": self._trade_log[-50:],
                "daily_losses": getattr(self, "_daily_losses", {}),
                "daily_losses_date": getattr(self, "_daily_losses_date", ""),
            })
        except Exception as e:
            logger.warning("[%s] 상태 저장 실패: %s", self.BOT_ID, e)

    def _reload_params_if_needed(self):
        """100스캔마다 에볼버 설정 리로드."""
        if self._scan_count % 100 != 0:
            return
        evolver_cfg = load_bot_config("mirinae")
        if not evolver_cfg:
            return
        self._params.update(evolver_cfg)
        self._daily_limit.max_trades = self._params["max_daily_trades"]
        self._config.interval_seconds = self._params["interval_seconds"]
        logger.info("[%s] 파라미터 리로드 완료", self.BOT_ID)

    # ===========================================
    # AI 판단 감사
    # ===========================================

    def _audit_record_structured_evidence(self, recorder, data: dict | None):
        if not recorder or not isinstance(data, dict):
            return
        try:
            sources = [
                ("prices", data.get("etf_prices"), "ETF price rows"),
                ("holdings_bot", data.get("holdings_bot"), "bot holdings"),
                ("balance", data.get("holdings_account"), "account balance"),
                ("news", data.get("news"), "news rows"),
                ("investor_trends", data.get("investor_trends"), "investor trends"),
                ("industry_index", data.get("industry_index"), "industry index"),
                ("category_trends", data.get("category_trends"), "category trends"),
                ("board_context", data.get("board_context"), "board context"),
                ("other_opinions", data.get("other_opinions"), "other bot opinions"),
                ("signals", data.get("etf_signals"), "ETF signals"),
                ("trade_summary", data.get("trade_summary"), "trade summary"),
            ]
            for source, payload, label in sources:
                if payload:
                    count = len(payload) if hasattr(payload, "__len__") else 1
                    recorder.evidence(
                        source,
                        payload,
                        summary=f"{label}: {count}",
                        success=True,
                    )
            preflight = getattr(self, "_last_preflight", None)
            if preflight:
                recorder.evidence(
                    "preflight_input",
                    preflight,
                    summary=self._format_buy_preflight_summary(preflight),
                    success=True,
                )
        except Exception as exc:
            logger.warning("[%s] audit structured evidence failed: %s", self.BOT_ID, exc)

    def _audit_record_evidence(self, recorder, context: str, data: dict | None = None):
        if not recorder:
            return
        try:
            recorder.evidence(
                "mirinae_context",
                {"context": context or ""},
                summary=f"context chars={len(context or '')}",
                success=bool(context),
            )
            self._audit_record_structured_evidence(
                recorder,
                data if data is not None else getattr(self, "_last_collected_data", None),
            )
        except Exception as exc:
            logger.warning("[%s] audit evidence failed: %s", self.BOT_ID, exc)

    def _audit_record_ai(
        self,
        recorder,
        prompt: str,
        output: str,
        ai_decision: dict | None,
        provider: str = "",
        model: str = "",
        tokens: int = 0,
        latency_ms: int = 0,
    ):
        if not recorder:
            return
        try:
            params = getattr(self, "_params", {}) or {}
            parsed = ai_decision or {}
            recorder.ai_call(
                provider=provider or params.get("ai_provider", ""),
                model=model or params.get("ai_model", ""),
                prompt=prompt or "",
                raw_response=output or "",
                parsed=parsed,
                parse_success=bool(ai_decision),
                parse_error="" if ai_decision else (getattr(self, "_last_ai_error", "") or "parse failed"),
                tokens=int(tokens or 0),
                latency_ms=int(latency_ms or 0),
            )
        except Exception as exc:
            logger.warning("[%s] audit ai failed: %s", self.BOT_ID, exc)

    def _audit_record_preflight(self, recorder, preflight: dict, stock_code: str = ""):
        if not recorder or not preflight:
            return
        try:
            buyable = preflight.get("buyable_candidates") or []
            passed = bool(buyable) or preflight.get("reason") == "OK"
            recorder.preflight(
                "buyable_candidates",
                passed=passed,
                severity="info" if passed else "block",
                message=preflight.get("reason", ""),
                payload=preflight,
                stock_code=stock_code,
            )
        except Exception as exc:
            logger.warning("[%s] audit preflight failed: %s", self.BOT_ID, exc)

    def _audit_finish_run(
        self,
        recorder,
        action: str,
        stock_code: str = "",
        decision_id: int = 0,
        result: dict | None = None,
        error: str = "",
    ):
        if not recorder:
            return
        try:
            result = result or {}
            success = bool(result.get("success", True))
            trade_id = int(result.get("trade_id", 0) or 0)
            if error:
                status = "failed"
            elif action.startswith("blocked_") or not success:
                status = "blocked"
            elif trade_id:
                status = "ordered"
            elif action in ("hold", "skip", "adjust_plan"):
                status = "completed"
            else:
                status = "decided"
            recorder.finish(
                status=status,
                action=action,
                stock_code=stock_code,
                decision_id=int(decision_id or 0),
                trade_id=trade_id,
                error=error or str(result.get("reason") or result.get("message") or ""),
            )
        except Exception as exc:
            logger.warning("[%s] audit finish failed: %s", self.BOT_ID, exc)

    def _link_decision_trade_from_result(self, decision_id: int, result: dict | None) -> int:
        """Link a decision only to an explicitly returned trade id."""
        if not decision_id or not result:
            return 0
        try:
            trade_id = int(result.get("trade_id", 0) or 0)
        except (TypeError, ValueError):
            return 0
        if not trade_id:
            return 0
        try:
            from src.db import get_db
            get_db().link_decision_trade(decision_id, trade_id)
            return trade_id
        except Exception:
            return 0

    def _audit_iso(self, value=None) -> str:
        if value is None:
            return kst_now().isoformat()
        try:
            if hasattr(value, "isoformat"):
                return value.isoformat()
            if isinstance(value, str):
                return value
        except Exception:
            pass
        return ""

    def _audit_int(self, value, default: int = 0) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return default

    def _audit_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return default

    def _audit_hold_minutes(self, entry_time: str) -> int:
        if not entry_time:
            return 0
        try:
            entry = datetime.fromisoformat(entry_time)
            return int(max(0, (kst_now() - entry).total_seconds() // 60))
        except (TypeError, ValueError):
            return 0

    def _audit_find_position(self, stock_code: str) -> dict:
        for pos in getattr(self, "_positions", []) or []:
            if pos.get("code") == stock_code:
                return pos
        return {}

    def _audit_normalize_lots(self, pos: dict) -> list[dict]:
        if not pos:
            return []

        lots = pos.get("audit_outcome_runs")
        if not isinstance(lots, list):
            lots = []

        normalized = []
        for lot in lots:
            if not isinstance(lot, dict):
                continue
            run_id = str(lot.get("run_id") or "")
            qty = self._audit_int(lot.get("qty"))
            remaining_qty = self._audit_int(lot.get("remaining_qty") or qty)
            if not run_id or remaining_qty <= 0:
                continue
            normalized.append({
                "run_id": run_id,
                "buy_trade_id": self._audit_int(lot.get("buy_trade_id")),
                "qty": qty if qty > 0 else remaining_qty,
                "remaining_qty": remaining_qty,
                "entry_price": self._audit_float(lot.get("entry_price")),
                "entry_time": self._audit_iso(lot.get("entry_time")),
            })

        if not normalized and pos.get("audit_outcome_run_id"):
            qty = self._audit_int(pos.get("qty"))
            normalized.append({
                "run_id": str(pos.get("audit_outcome_run_id") or ""),
                "buy_trade_id": self._audit_int(pos.get("audit_buy_trade_id")),
                "qty": qty,
                "remaining_qty": qty,
                "entry_price": self._audit_float(pos.get("price")),
                "entry_time": self._audit_iso(pos.get("entry_time") or pos.get("bought_at")),
            })

        pos["audit_outcome_runs"] = normalized
        self._audit_sync_latest_lot_fields(pos)
        return normalized

    def _audit_sync_latest_lot_fields(self, pos: dict):
        lots = pos.get("audit_outcome_runs") if isinstance(pos.get("audit_outcome_runs"), list) else []
        if lots:
            latest = lots[-1]
            pos["audit_outcome_run_id"] = latest.get("run_id", "")
            pos["audit_buy_trade_id"] = latest.get("buy_trade_id", 0)
        else:
            pos["audit_outcome_run_id"] = ""
            pos["audit_buy_trade_id"] = 0

    def _audit_append_lot(self, pos: dict, run_id: str, buy_trade_id: int, qty: int,
                          entry_price: float, entry_time: str):
        if not pos or not run_id or qty <= 0:
            return
        lots = [lot for lot in self._audit_normalize_lots(pos) if lot.get("run_id") != run_id]
        lots.append({
            "run_id": run_id,
            "buy_trade_id": buy_trade_id,
            "qty": qty,
            "remaining_qty": qty,
            "entry_price": entry_price,
            "entry_time": entry_time,
        })
        pos["audit_outcome_runs"] = lots
        self._audit_sync_latest_lot_fields(pos)

    def _audit_build_lot_closes(self, pos: dict, sell_qty: int) -> list[dict]:
        remaining = max(0, int(sell_qty or 0))
        closes = []
        if not pos or remaining <= 0:
            return closes

        for lot in self._audit_normalize_lots(pos):
            if remaining <= 0:
                break
            lot_qty = self._audit_int(lot.get("remaining_qty"))
            if lot_qty <= 0:
                continue
            closed_qty = min(remaining, lot_qty)
            closes.append({
                **lot,
                "closed_qty": closed_qty,
                "remaining_after": lot_qty - closed_qty,
            })
            remaining -= closed_qty
        return closes

    def _audit_executed_sell_qty(self, result: dict | None, requested_qty: int = 0) -> int:
        if not isinstance(result, dict):
            return self._audit_int(requested_qty)
        basis = result.get("sell_basis") if isinstance(result.get("sell_basis"), dict) else {}
        for raw_qty in (
            result.get("clamped_qty"),
            basis.get("quantity"),
            result.get("qty"),
            result.get("quantity"),
            requested_qty,
        ):
            qty = self._audit_int(raw_qty)
            if qty > 0:
                return qty
        return 0

    def _audit_trim_lot_closes(self, closes: list[dict], sell_qty: int) -> list[dict]:
        remaining = max(0, self._audit_int(sell_qty))
        if remaining <= 0:
            return []

        trimmed = []
        for close in closes:
            if remaining <= 0:
                break
            if not isinstance(close, dict):
                continue
            available_qty = self._audit_int(
                close.get("remaining_qty")
                or close.get("qty")
                or close.get("closed_qty")
            )
            close_qty = self._audit_int(close.get("closed_qty"))
            if available_qty <= 0 and close_qty <= 0:
                continue
            closed_qty = min(remaining, close_qty if close_qty > 0 else available_qty)
            lot_qty = available_qty if available_qty > 0 else closed_qty
            trimmed.append({
                **close,
                "closed_qty": closed_qty,
                "remaining_after": max(0, lot_qty - closed_qty),
            })
            remaining -= closed_qty
        return trimmed

    def _audit_prepare_sell_result(self, result: dict, requested_qty: int, pos: dict) -> int:
        executed_qty = self._audit_executed_sell_qty(result, requested_qty)
        basis = result.get("sell_basis") if isinstance(result.get("sell_basis"), dict) else {}
        basis["quantity"] = executed_qty
        result["sell_basis"] = basis
        result["qty"] = executed_qty
        result["audit_outcome_closes"] = self._audit_build_lot_closes(pos, executed_qty)
        return executed_qty

    def _audit_apply_lot_closes(self, pos: dict, closes: list[dict]):
        if not pos or not closes:
            return
        close_by_run = {str(close.get("run_id") or ""): close for close in closes}
        next_lots = []
        for lot in self._audit_normalize_lots(pos):
            close = close_by_run.get(lot.get("run_id"))
            if not close:
                next_lots.append(lot)
                continue
            remaining_after = self._audit_int(close.get("remaining_after"))
            if remaining_after > 0:
                lot["remaining_qty"] = remaining_after
                next_lots.append(lot)
        pos["audit_outcome_runs"] = next_lots
        self._audit_sync_latest_lot_fields(pos)

    def _audit_record_outcome(
        self,
        recorder,
        action: str,
        ai_decision: dict | None,
        result: dict | None,
        decision_id: int = 0,
        stock_code: str = "",
    ):
        if not recorder or not result or not result.get("success"):
            return
        action = str(action or result.get("action") or "").lower()
        if action not in ("buy", "add_buy", "sell", "partial_sell"):
            return
        try:
            trade_id = self._audit_int(result.get("trade_id"))
            if trade_id <= 0:
                return
            code = stock_code or result.get("code") or (ai_decision or {}).get("code", "")
            db = getattr(recorder, "db", None)
            if not db:
                return

            if action in ("buy", "add_buy"):
                price = self._audit_float(result.get("price") or (ai_decision or {}).get("price"))
                qty = self._audit_int(result.get("qty"))
                pos = self._audit_find_position(code)
                if not price and pos:
                    price = self._audit_float(pos.get("price"))
                if not qty and pos:
                    qty = self._audit_int(pos.get("qty"))
                entry_time = self._audit_iso()
                db.upsert_decision_outcome(
                    recorder.run_id,
                    decision_id=int(decision_id or 0),
                    buy_trade_id=trade_id,
                    bot_id=self.BOT_ID,
                    stock_code=code,
                    entry_time=entry_time,
                    entry_price=price,
                    quantity=qty,
                    outcome_status="open",
                )
                if pos:
                    self._audit_append_lot(pos, recorder.run_id, trade_id, qty, price, entry_time)
                return

            basis = result.get("sell_basis") if isinstance(result.get("sell_basis"), dict) else {}
            outcome_run_id = str(result.get("outcome_run_id") or basis.get("outcome_run_id") or "")
            run_id = outcome_run_id or recorder.run_id
            entry_price = self._audit_float(
                basis.get("entry_price")
                or result.get("entry_price")
                or (ai_decision or {}).get("entry_price")
            )
            exit_price = self._audit_float(
                result.get("sell_price")
                or result.get("exit_price")
                or basis.get("sell_price")
                or basis.get("current_price")
                or (ai_decision or {}).get("price")
                or (ai_decision or {}).get("current_price")
            )
            qty = self._audit_int(
                result.get("clamped_qty")
                or basis.get("quantity")
                or result.get("qty")
                or result.get("quantity")
                or basis.get("sell_qty")
            )
            realized_pnl = self._audit_float(result.get("realized_pnl") or basis.get("realized_pnl"))
            return_pct = 0.0
            if entry_price > 0 and exit_price > 0:
                return_pct = round((exit_price - entry_price) / entry_price * 100, 4)
            entry_time = self._audit_iso(basis.get("entry_time") or result.get("entry_time"))
            exit_time = self._audit_iso()
            closes = result.get("audit_outcome_closes")
            if not isinstance(closes, list):
                closes = basis.get("audit_outcome_closes")
            if isinstance(closes, list):
                closes = self._audit_trim_lot_closes(closes, qty)
            else:
                pos = self._audit_find_position(code)
                closes = self._audit_build_lot_closes(pos, qty) if pos else []

            if closes:
                total_closed_qty = sum(self._audit_int(close.get("closed_qty")) for close in closes)
                allocated_pnl = 0.0
                pos = self._audit_find_position(code)
                for idx, close in enumerate(closes):
                    close_qty = self._audit_int(close.get("closed_qty"))
                    if close_qty <= 0:
                        continue
                    close_entry_price = self._audit_float(close.get("entry_price")) or entry_price
                    close_entry_time = self._audit_iso(close.get("entry_time") or entry_time)
                    close_return_pct = 0.0
                    if close_entry_price > 0 and exit_price > 0:
                        close_return_pct = round((exit_price - close_entry_price) / close_entry_price * 100, 4)
                    if idx == len(closes) - 1:
                        close_pnl = round(realized_pnl - allocated_pnl, 4)
                    else:
                        close_pnl = round(
                            realized_pnl * (close_qty / total_closed_qty),
                            4,
                        ) if total_closed_qty > 0 else 0.0
                        allocated_pnl += close_pnl

                    remaining_after = self._audit_int(close.get("remaining_after"))
                    if remaining_after > 0:
                        db.upsert_decision_outcome(
                            str(close.get("run_id") or ""),
                            quantity=remaining_after,
                            outcome_status="open",
                        )
                        close_run_id = f"{close.get('run_id')}:sell:{trade_id}"
                    else:
                        close_run_id = str(close.get("run_id") or "")

                    db.upsert_decision_outcome(
                        close_run_id,
                        decision_id=int(decision_id or 0),
                        buy_trade_id=self._audit_int(close.get("buy_trade_id")),
                        sell_trade_id=trade_id,
                        bot_id=self.BOT_ID,
                        stock_code=code,
                        entry_time=close_entry_time,
                        exit_time=exit_time,
                        hold_minutes=self._audit_hold_minutes(close_entry_time),
                        entry_price=close_entry_price,
                        exit_price=exit_price,
                        quantity=close_qty,
                        realized_pnl=close_pnl,
                        return_pct=close_return_pct,
                        outcome_status="closed",
                        notes=str(basis.get("reason") or result.get("reason") or result.get("message") or ""),
                    )
                if pos:
                    self._audit_apply_lot_closes(pos, closes)
                return

            db.upsert_decision_outcome(
                run_id,
                decision_id=int(decision_id or 0),
                sell_trade_id=trade_id,
                bot_id=self.BOT_ID,
                stock_code=code,
                entry_time=entry_time if not outcome_run_id else None,
                exit_time=exit_time,
                hold_minutes=self._audit_hold_minutes(entry_time),
                entry_price=entry_price if not outcome_run_id else None,
                exit_price=exit_price,
                quantity=qty if not outcome_run_id else None,
                realized_pnl=realized_pnl,
                return_pct=return_pct,
                outcome_status="closed",
                notes=str(basis.get("reason") or result.get("reason") or result.get("message") or ""),
            )
        except Exception as exc:
            logger.warning("[%s] audit outcome failed: %s", self.BOT_ID, exc)

    # ===========================================
    # 1. 데이터 수집
    # ===========================================

    def _collect_data(self) -> str:
        """Python이 KIS API로 데이터를 수집하여 파일에 저장. Codex는 이 파일만 읽음."""
        market = self._get_market()
        data_path = Path("/tmp/mirinae_data.json")

        data = {
            "timestamp": kst_now().isoformat(),
            "etf_prices": [],
            "holdings_bot": [],
            "holdings_account": {},
            "news": [],
            "investor_trends": [],
            "trade_summary": {},
        }

        # 1. ETF 시세 (캐시+리미터가 자동 쓰로틀링)
        # 파생ETF 토글 OFF면 인버스/레버리지를 universe에서 제외 — AI가 거래 불가 종목을 추천하지 않게
        etf_codes = self._filter_derivative_etfs(self._config.etf_codes)
        for code in etf_codes:
            try:
                info = market.get_price(code)
                if info:
                    data["etf_prices"].append({
                        "code": code,
                        "name": info.get("name", code),
                        "price": info.get("price", 0),
                        "change_pct": info.get("change_rate", info.get("change_pct", 0)),
                        "volume": info.get("volume", 0),
                    })
            except Exception as e:
                logger.warning("[%s] ETF %s 시세 실패: %s", self.BOT_ID, code, e)

        if not data["etf_prices"]:
            logger.error("[%s] ETF 시세 전체 실패! market=%s", self.BOT_ID, type(market).__name__)

        # 2. 봇 보유 종목 + 현재가
        for pos in self._positions:
            try:
                info = market.get_price(pos["code"])
                current = info.get("price", 0) if info else 0
                entry = pos["price"]
                pnl = ((current - entry) / entry * 100) if entry > 0 else 0
                data["holdings_bot"].append({
                    "code": pos["code"], "name": pos.get("name", ""),
                    "qty": pos["qty"], "entry_price": entry,
                    "current_price": current, "pnl_pct": round(pnl, 2),
                    "target_price": int(pos.get("target_price", 0) or 0),
                    "stop_loss": int(pos.get("stop_loss", 0) or 0),
                })
            except Exception:
                pass

        # 3. 실제 계좌 잔고
        try:
            balance = market.get_balance()
            if balance:
                data["holdings_account"] = balance
        except Exception:
            pass

        # 4. 산업 지수 (서브섹터 포함)
        try:
            from src.services.industry_index import get_industry_index
            data["industry_index"] = get_industry_index()
            # 하위 호환: category_trends 형식도 유지
            from src.services.etf_signal import _convert_index_to_legacy
            data["category_trends"] = _convert_index_to_legacy(data["industry_index"])
        except Exception:
            data["industry_index"] = {}
            try:
                from src.services.etf_signal import get_category_trends
                data["category_trends"] = get_category_trends()
            except Exception:
                data["category_trends"] = {}

        # 5. 외인/기관 동향 (상위 5개)
        for code in etf_codes[:3]:
            try:
                trend = market.get_investor_trend(code)
                if trend:
                    data["investor_trends"].append({"code": code, "trend": str(trend)[:200]})
            except Exception:
                pass

        # 6. 매매 통계 (DB)
        try:
            from src.db import get_db
            data["trade_summary"] = get_db().trade_summary()
        except Exception:
            pass

        # 7. 다른 봇의 게시판 (구조화)
        board_ctx = self._get_board_context()
        data["board_context"] = board_ctx
        # 하위 호환
        data["other_opinions"] = self._read_board(limit=10)

        # 8. ETF 시그널 점수
        try:
            from src.services.etf_signal import get_etf_signals
            data["etf_signals"] = get_etf_signals()
        except Exception:
            data["etf_signals"] = {}

        # 파일 저장
        try:
            data_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass
        self._last_collected_data = data

        # 프롬프트용 텍스트 생성
        etf_lines = "\n".join(
            f"  {e['code']} {e.get('name','')}: {e.get('price',0):,}원 ({e.get('change_pct',0):+.2f}%) 거래량 {e.get('volume',0):,}"
            for e in data["etf_prices"][:15]
        ) or "  (조회 실패)"

        holdings_lines = "\n".join(
            f"  {h['code']} {h.get('name','')}: {h['qty']}주 @{h['entry_price']:,} → {h.get('current_price',0):,}원 "
            f"({h.get('pnl_pct',0):+.2f}%), 목표 {h.get('target_price',0):,}, 손절 {h.get('stop_loss',0):,}"
            for h in data["holdings_bot"]
        ) or "  (보유 종목 없음)"

        def _format_review_line(r: dict) -> str:
            trend = r.get("minute_trend_pct")
            trend_txt = f", 10분 {trend:+.2f}%" if isinstance(trend, (int, float)) else ""
            dd = r.get("recent_high_drawdown_pct")
            dd_txt = f", 고점대비 {dd:+.2f}%" if isinstance(dd, (int, float)) else ""
            rr = r.get("risk_reward")
            rr_txt = f", 손익비 {rr:.2f}:1" if isinstance(rr, (int, float)) else ""
            plan = r.get("plan_warning")
            plan_txt = f", 계획검증 {plan}" if plan else ""
            return (
                f"  {r.get('code','')}: {r.get('decision','hold')} - {r.get('reason','')} "
                f"(손익 {r.get('pnl_pct', 0):+.2f}%{trend_txt}{dd_txt}{rr_txt}{plan_txt}, "
                f"현재 {int(r.get('current_price', 0) or 0):,}, "
                f"목표 {int(r.get('target_price', 0) or 0):,}, "
                f"손절 {int(r.get('stop_loss', 0) or 0):,})"
            )

        review_lines = "\n".join(
            _format_review_line(r)
            for r in self._last_position_reviews
        ) or "  (특이 이벤트 없음)"

        # 산업 지수 (서브섹터 포함)
        idx = data.get("industry_index", {})
        if idx:
            from src.services.industry_index import format_index_for_prompt
            news_lines = format_index_for_prompt(idx)
        else:
            trends = data.get("category_trends", {})
            if trends:
                trend_items = []
                for cat, t in sorted(trends.items(), key=lambda x: abs(x[1].get("score", 0)), reverse=True):
                    score = t.get("score", 0)
                    count = t.get("count", 0)
                    icon = "+" if score > 2 else "-" if score < -2 else "="
                    bd = t.get("breakdown", {})
                    pos = bd.get("positive", 0) + bd.get("strong_positive", 0)
                    neg = bd.get("negative", 0) + bd.get("strong_negative", 0)
                    trend_items.append(f"  [{icon}] {cat}: {score:+.1f} ({count}건, 호재{pos}/악재{neg})")
                news_lines = "\n".join(trend_items)
            else:
                news_lines = "  (트렌드 데이터 없음)"

        trend_lines = "\n".join(
            f"  {t['code']}: {t['trend'][:100]}"
            for t in data.get("investor_trends", [])
        ) or "  (조회 실패)"

        budget_info = f"예산: {self._config.budget:,}원" if self._config.budget > 0 else "예산: 전액 사용 가능"
        budget_amount = self._config.budget if self._config.budget > 0 else 300000

        # 매수 가능 ETF 필터링
        affordable = [e for e in data["etf_prices"]
                      if 0 < e.get("price", 0) <= budget_amount]
        affordable_lines = "\n".join(
            f"  {e['code']} {e.get('name','')}: {e.get('price',0):,}원 ({e.get('change_pct',0):+.2f}%) → {budget_amount // e['price']}주 매수 가능"
            for e in sorted(affordable, key=lambda x: x.get("change_pct", 0), reverse=True)[:10]
        ) or "  (매수 가능 종목 없음)"
        actual_buyable = (getattr(self, "_last_preflight", {}) or {}).get("buyable_candidates", [])
        actual_buyable_lines = "\n".join(
            f"  {c['code']} {c.get('name','')}: {c['price']:,}원, 실제 {c['qty']}주 가능, 주문가능 {c.get('amount', 0):,}원"
            for c in actual_buyable[:10]
        ) or f"  ({(getattr(self, '_last_preflight', {}) or {}).get('reason', '사전 확인 전')})"
        blocked_buyable = (getattr(self, "_last_preflight", {}) or {}).get("blocked_candidates", [])
        blocked_buyable_lines = "\n".join(
            f"  {c['code']} {c.get('name','')}: {c.get('reason','차단')}"
            for c in blocked_buyable[:8]
        ) or "  (없음)"

        return f"""[현재 시각] {kst_now().strftime("%Y-%m-%d %H:%M:%S")} KST (한국시간)

[ETF 시세 (실시간)]
{etf_lines}

[보유 종목]
{holdings_lines}

[보유 평가 이벤트]
{review_lines}

[산업 지수 (-10~+10, 뉴스 종합: 감성+볼륨+속도)]
{news_lines}

[외인/기관 동향]
{trend_lines}

[시장 세션]
{self._format_market_session()}

[제약 조건]
  {budget_info}
  최대 보유 종목: {self._params['max_positions']}개 (현재 {len(self._positions)}개)
  일일 거래 횟수: {self._daily_limit._count}/{self._daily_limit.max_trades}
  실현 손익: {self._realized_pnl:+,}원
  confidence 임계값: {self._params['confidence_threshold']}

[매수 가능 ETF (예산 {budget_amount:,}원 기준)]
{affordable_lines}

[실제 KIS 주문가능 ETF (AI 호출 전 게이트 통과 후보)]
{actual_buyable_lines}

[코드 룰로 사전 차단된 후보]
{blocked_buyable_lines}

[ETF 방향 시그널 (뉴스 기반, -10=강한 하락 ~ +10=강한 상승)]
{self._format_signals(data.get("etf_signals", {}))}

[다른 봇 동향 (보드)]
{self._format_board_context(data.get("board_context", {}))}

[최근 뉴스]
{self._format_news_context(data)}
"""

    def _format_market_session(self) -> str:
        """시장 세션 정보를 프롬프트용 텍스트로."""
        try:
            from src.bots.safeguards import get_market_session
            s = get_market_session()
            can_buy = "가능" if s.get("can_buy") else "불가"
            can_sell = "가능" if s.get("can_sell") else "불가"
            return (
                f"  세션: {s.get('label', '?')}\n"
                f"  매수: {can_buy} | 매도: {can_sell}\n"
                f"  마감까지: {s.get('minutes_to_close', 0):.0f}분\n"
                f"  권장: {s.get('recommended_action', '')}"
            )
        except Exception:
            return "  (조회 실패)"

    def _format_signals(self, signals: dict) -> str:
        """ETF 시그널 점수를 프롬프트용 텍스트로. 뉴스+모멘텀 결합."""
        if not signals:
            return "  (시그널 없음)"
        lines = []
        for code, s in sorted(signals.items(), key=lambda x: x[1].get("score", 0), reverse=True):
            score = s.get("score", 0)
            news = s.get("news_score", 0)
            mom = s.get("momentum_score", 0)
            align = s.get("alignment", "?")
            chg = s.get("change_pct", 0)

            icon = "🟢" if score > 2 else "🔴" if score < -2 else "⚪"
            align_icon = "✅" if align == "ALIGNED" else "⚠️" if align == "DIVERGENT" else ""
            reasons = ", ".join(s.get("reasons", []))

            lines.append(
                f"  {icon} {code} {s.get('name','')}: 종합{score:+.1f} "
                f"(뉴스{news:+.1f} 추세{mom:+.1f} 등락{chg:+.1f}%) "
                f"{align_icon}{reasons}"
            )
        return "\n".join(lines)

    def _format_other_opinions(self, opinions: list[dict]) -> str:
        """다른 봇의 게시판 글을 프롬프트용 텍스트로."""
        if not opinions:
            return "  (다른 봇의 의견 없음)"
        lines = []
        for p in opinions[:5]:
            time = (p.get("timestamp", "") or "")[:16].split("T")[-1] if "T" in p.get("timestamp", "") else ""
            bot = p.get("bot_id", "?")
            content = (p.get("content", "") or "")[:80]
            lines.append(f"  {time} [{bot}] {content}")
        return "\n".join(lines)

    def _format_news_context(self, data: dict) -> str:
        """최근 뉴스를 프롬프트용 텍스트로."""
        news = self._get_news_context(count=5)
        if not news:
            return "  (뉴스 없음)"
        lines = []
        for n in news:
            score = n.get("score", 0)
            label = n.get("sentiment", "중립")
            icon = "🟢" if score >= 2 else "🔴" if score <= -2 else "⚪"
            lines.append(
                f"  {icon} [{n['category']}] {n['title'][:55]} "
                f"({label}, {score:+.0f})"
            )
        return "\n".join(lines)

    def _format_board_context(self, board_ctx: dict) -> str:
        """구조화된 보드 컨텍스트를 프롬프트용 텍스트로."""
        lines = []

        # 최근 매매 (다른 봇)
        trades = board_ctx.get("recent_trades", [])
        if trades:
            lines.append("  [최근 다른 봇 매매]")
            for t in trades[:5]:
                lines.append(f"    {t['time']} [{t['bot_id']}] {t['content'][:60]}")

        # 의견 (SELL 경고 강조)
        opinions = board_ctx.get("opinions", [])
        if opinions:
            lines.append("  [다른 봇 의견]")
            for o in opinions[:5]:
                prefix = "⚠️" if "[SELL]" in o["content"] else ""
                lines.append(f"    {prefix}{o['time']} [{o['bot_id']}] {o['content'][:60]}")

        # 경고
        warnings = board_ctx.get("warnings", [])
        if warnings:
            lines.append("  [경고]")
            for w in warnings[:3]:
                lines.append(f"    {w['time']} [{w['bot_id']}] {w['content'][:60]}")

        # 다른 봇이 거래 중인 종목
        active = board_ctx.get("active_codes", set())
        if active:
            lines.append(f"  [다른 봇 활동 종목] {', '.join(active)}")

        return "\n".join(lines) if lines else "  (보드 정보 없음)"

    # ===========================================
    # 2. AI 프롬프트 생성
    # ===========================================

    def _build_codex_prompt(self, context: str) -> str:
        """수집된 데이터 기반 AI 프롬프트 생성. AI는 네트워크 접근 불가 — 제공된 데이터만 분석."""
        prompt = f"""아래는 Python이 KIS API에서 실시간 수집한 데이터입니다.
이 데이터만으로 분석하세요. 외부 API/네트워크 호출하지 마세요.

{context}

## 3단계 멀티 에이전트 분석을 수행하세요:

### 1단계: 강세 분석가 (Bull)
위 ETF 시세, 뉴스, 외인/기관 동향에서 매수 근거를 찾으세요.
거래량 급증, 가격 상승 추세, 호재 뉴스 등을 분석하세요.

### 2단계: 약세 분석가 (Bear)
강세 분석에 반박하세요. 리스크를 찾으세요.
고점 매수 위험, 악재 뉴스, 외인 매도세 등을 분석하세요.

### 3단계: 트레이더 (Trader)
양측 논거를 비교하여 최종 결정을 내리세요.
확신이 없으면 HOLD (confidence 0.5 미만).
- 보유 종목이 있으면 신규 매수 후보보다 보유 종목의 SELL/HOLD/추매 판단을 먼저 하세요.
- 매 스캔마다 기존 target_price/stop_loss가 현재가, 추세, 잔여시간 기준으로 아직 유효한지 검증하세요.
- [보유 평가 이벤트]가 review_loss, review_plan, review_target 또는 sell이면 신규 종목 BUY를 선택하지 마세요. 먼저 해당 보유 종목의 손실 인정/목표 수정/매도 여부를 결론내야 합니다.
- 손실 중인 보유 종목을 HOLD하려면 "왜 다시 오른다고 보는지"를 가격·거래량·뉴스 근거로 설명하고 target_price와 stop_loss를 반드시 다시 제시하세요.
- review_plan인 보유 종목은 기존 계획이 틀렸다는 신호입니다. HOLD하려면 target_action을 raise/lower 중 하나로 정하고 새 target_price와 stop_loss를 반드시 제시하세요. 기존 목표/손절 그대로 keep하지 마세요.
- target_action=keep은 계획검증이 ok일 때만 허용됩니다. keep이어도 target_price와 stop_loss 값을 반드시 다시 출력하세요.
- 손실 중인 보유 종목의 하락 추세가 이어지고 반등 근거가 약하면 부분 매도 또는 전량 SELL을 우선 검토하세요.
- 이미 보유 중인 종목의 BUY는 추매입니다. 보유 중이라는 이유만으로 BUY를 금지하지 마세요.
- SELL은 전량 청산뿐 아니라 분할 매도도 가능합니다. 일부만 줄일 때는 sell_qty 또는 position_pct를 명시하세요.
- 보유 종목이 목표가에 도달했더라도 자동 익절로 끝내지 말고, 뉴스/거래량/추세가 유지되면 HOLD와 함께 target_price를 상향하고 stop_loss도 이익 보호선으로 상향하세요.
- 목표가를 낮춰야 할 때도 HOLD와 함께 낮춘 target_price/stop_loss를 제시하세요. 논리가 깨졌을 때만 SELL을 선택하세요.
- **중요: 어떤 경우에도 HOLD, partial_sell 결정 시 target_price와 stop_loss 값을 반드시 0보다 큰 현재가 기준 유효한 정수로 제공해야 합니다. 누락하면 시스템 에러가 발생합니다.**

반드시 stdout에 아래 JSON을 출력하세요. 각 criteria에 0~10 점수를 매기세요:
```json
{{
  "decision": "BUY | SELL | HOLD",
  "confidence": 0.0,
  "code": "종목코드 6자리",
  "name": "종목명",
  "target_action": "raise | lower | keep | none",
  "sell_qty": 0,
  "position_pct": 0.0,
  "target_price": 0,
  "stop_loss": 0,
  "criteria": {{
    "market_trend": {{"score": 0, "detail": "시장 방향 (0=하락 5=중립 10=상승)"}},
    "volume_signal": {{"score": 0, "detail": "거래량 (0=감소 10=폭발)"}},
    "news_sentiment": {{"score": 0, "detail": "뉴스 (0=악재 5=중립 10=호재)"}},
    "time_risk": {{"score": 0, "detail": "시간 (0=마감임박 10=여유)"}},
    "entry_price": {{"score": 0, "detail": "가격 (0=고점 10=저점)"}}
  }},
  "bull_summary": "강세 논거 (한국어)",
  "bear_summary": "약세 논거 (한국어)",
  "trader_reason": "최종 판단 (한국어)"
}}
```"""
        return prompt

    def _invoke_codex(self, prompt: str) -> str:
        """AI CLI 호출 (프로바이더/모델 동적 선택)."""
        from src.bots.ai_caller import invoke_ai_result

        timeout = self._params.get("codex_timeout", 180)
        provider = self._params.get("ai_provider", "codex")
        self._last_ai_error = ""
        self._last_raw_output_preview = ""
        self._last_ai_call_meta = {}
        logger.info("[%s] AI 호출 시작 (provider=%s)...", self.BOT_ID, provider)

        try:
            result = invoke_ai_result(prompt, self._params, timeout=timeout, bot_id=self.BOT_ID)
        except Exception as e:
            self._last_ai_error = f"AI 호출 예외(provider={provider}): {e}"
            logger.warning("[%s] %s", self.BOT_ID, self._last_ai_error)
            self._post_to_board(
                "alert",
                f"[{self.BOT_ID} AI 호출 실패] {self._last_ai_error}",
            )
            return ""

        self._last_ai_call_meta = {
            "provider": result.provider,
            "model": result.model,
            "tokens": result.tokens,
            "latency_ms": result.latency_ms,
        }
        output = result.content
        if output:
            self._last_raw_output_preview = output[:500]
            logger.info("[%s] AI 분석 완료 (%d자)", self.BOT_ID, len(output))
        else:
            detail = result.error or result.error_type
            self._last_ai_error = f"AI 응답 없음(provider={provider})"
            if detail:
                self._last_ai_error = f"{self._last_ai_error}: {detail}"
            logger.warning("[%s] %s", self.BOT_ID, self._last_ai_error)
        return output or ""

    # ===========================================
    # 4. 결정 파싱
    # ===========================================

    @staticmethod
    def _extract_json_with_decision(output: str) -> dict | None:
        """응답에서 `"decision"` 키를 포함한 첫 JSON 객체를 추출.

        - 우선 ```json ... ``` 펜스 안의 JSON을 시도
        - 실패 시 전체 응답에서 raw_decode로 모든 `{` 위치를 스캔
          (중첩 객체 / 백틱 없는 응답 모두 처리)
        """
        decoder = json.JSONDecoder()

        # 1) ```json 펜스
        for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL):
            raw = m.group(1)
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "decision" in obj:
                    return obj
            except json.JSONDecodeError:
                continue

        # 2) 전체 응답에서 모든 `{` 위치를 raw_decode로 시도
        i = 0
        n = len(output)
        while i < n:
            j = output.find("{", i)
            if j < 0:
                break
            try:
                obj, end = decoder.raw_decode(output, j)
                if isinstance(obj, dict) and "decision" in obj:
                    return obj
                i = end
            except json.JSONDecodeError:
                i = j + 1
        return None

    def _parse_decision(self, output: str) -> dict | None:
        """AI 출력에서 JSON 결정 블록을 추출하고 검증.

        중첩 객체(criteria 등)가 있는 응답도 처리. 두 가지 시도:
          1) ```json ... ``` 펜스 안의 JSON
          2) 응답 전체에서 `"decision"` 키를 가진 첫 JSON 객체
             (json.JSONDecoder.raw_decode로 중첩까지 정확 파싱)
        """
        if not output:
            if not self._last_ai_error:
                self._last_ai_error = "AI 응답 없음"
            return None

        decision = self._extract_json_with_decision(output)
        if decision is None:
            self._last_ai_error = f"AI 응답 JSON 결정 블록 없음(raw {len(output)}자)"
            self._last_raw_output_preview = output[:500]
            logger.warning("[%s] JSON 결정 블록을 찾을 수 없음", self.BOT_ID)
            return None

        # 필수 필드 검증
        required = ["decision", "confidence"]
        for key in required:
            if key not in decision:
                self._last_ai_error = f"AI 응답 필수 필드 누락: {key} (raw {len(output)}자)"
                logger.warning("[%s] 필수 필드 누락: %s", self.BOT_ID, key)
                return None

        # decision 값 정규화
        decision["decision"] = decision["decision"].upper().strip()
        if decision["decision"] not in ("BUY", "SELL", "HOLD"):
            self._last_ai_error = f"AI 응답 decision 값 오류: {decision['decision']}"
            logger.warning("[%s] 유효하지 않은 decision: %s", self.BOT_ID, decision["decision"])
            return None

        # confidence 범위 확인
        try:
            confidence = float(decision["confidence"])
            decision["confidence"] = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            self._last_ai_error = "AI 응답 confidence 값 오류"
            return None

        # BUY/SELL 시 code 필수
        if decision["decision"] in ("BUY", "SELL"):
            if not decision.get("code"):
                logger.warning("[%s] BUY/SELL인데 종목코드 없음", self.BOT_ID)
                return None

        # confidence 임계값 미달 시 HOLD로 변환
        threshold = self._params.get("confidence_threshold", 0.7)
        if decision["confidence"] < threshold:
            logger.info(
                "[%s] confidence %.2f < %.2f -- HOLD로 변환",
                self.BOT_ID, decision["confidence"], threshold,
            )
            decision["decision"] = "HOLD"

        self._enforce_position_priority(decision)
        self._enforce_plan_update_requirement(decision)
        return decision

    def _enforce_position_priority(self, decision: dict):
        """보유 손실 포지션이 있으면 신규 BUY보다 보유 판단을 우선한다."""
        if not decision or not self._positions:
            return

        held_codes = {p.get("code") for p in self._positions}
        priority_reviews = [
            r for r in self._last_position_reviews
            if r.get("decision") in ("review_loss", "review_plan", "review_target", "sell")
            and r.get("code") in held_codes
        ]
        if not priority_reviews:
            return

        worst = min(priority_reviews, key=lambda r: float(r.get("pnl_pct", 0) or 0))
        action = decision.get("decision", "")
        code = decision.get("code", "")
        original_reason = decision.get("trader_reason", "")

        if action == "BUY" and code not in held_codes:
            decision["decision"] = "HOLD"
            decision["code"] = worst.get("code", "")
            decision["name"] = worst.get("name", "")
            decision["trader_reason"] = (
                f"보유 포지션 재검토 우선: {worst.get('code','')} "
                f"{worst.get('reason','')}. 신규 BUY({code}) 차단. "
                f"AI 원판단: {original_reason}"
            )
            decision["position_priority_blocked_buy"] = code
            logger.warning(
                "[%s] 보유 손실 검토 우선으로 신규 BUY 차단: %s -> %s",
                self.BOT_ID,
                code,
                worst.get("code", ""),
            )
            return

        if action == "HOLD" and not code:
            decision["code"] = worst.get("code", "")
            decision["name"] = worst.get("name", "")
            decision["trader_reason"] = (
                f"보유 포지션 재검토: {worst.get('code','')} "
                f"{worst.get('reason','')}. {original_reason}"
            ).strip()

    @staticmethod
    def _positive_int(value) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    def _enforce_plan_update_requirement(self, decision: dict):
        """계획 재검증 이벤트에서는 AI가 목표/손절의 유효성을 직접 결론내야 한다."""
        if not decision or not self._positions:
            return

        held_codes = {p.get("code") for p in self._positions}
        code = decision.get("code") or ""
        if not code and len(held_codes) == 1:
            code = next(iter(held_codes))
            decision["code"] = code
        if code not in held_codes:
            return

        review = next(
            (
                r for r in self._last_position_reviews
                if r.get("code") == code
                and r.get("decision") in ("review_plan", "review_loss", "review_target")
            ),
            None,
        )
        if not review:
            return

        target = self._positive_int(decision.get("target_price"))
        stop = self._positive_int(decision.get("stop_loss"))
        current = self._positive_int(review.get("current_price"))
        old_target = self._positive_int(review.get("target_price"))
        old_stop = self._positive_int(review.get("stop_loss"))
        target_action = str(decision.get("target_action", "") or "").lower()

        missing_plan = target <= 0 or stop <= 0 or target_action in ("", "none")
        unchanged_invalid_plan = (
            review.get("decision") == "review_plan"
            and not review.get("plan_valid", True)
            and target_action == "keep"
            and target == old_target
            and stop == old_stop
        )
        invalid_price_order = current > 0 and (stop >= current or target <= current)

        if not (missing_plan or unchanged_invalid_plan or invalid_price_order):
            return

        reason_parts = []
        if missing_plan:
            reason_parts.append("target_action/target_price/stop_loss 누락")
        if unchanged_invalid_plan:
            reason_parts.append(f"기존 계획이 유효하지 않은데 그대로 keep ({review.get('plan_warning', '')})")
        if invalid_price_order:
            reason_parts.append("현재가 기준 목표/손절 배치 오류")

        original = decision.get("trader_reason", "")
        decision["decision"] = "HOLD"
        decision["confidence"] = min(float(decision.get("confidence", 0) or 0), 0.49)
        decision["target_action"] = "none"
        decision["plan_update_missing"] = True
        decision["trader_reason"] = (
            f"목표/손절 재검증 실패: {', '.join(reason_parts)}. "
            f"다음 판단에서 {code}의 목표가와 손절가를 새로 제시해야 합니다. "
            f"AI 원판단: {original}"
        )
        logger.warning("[%s] 목표/손절 재검증 실패: %s %s", self.BOT_ID, code, reason_parts)

    # ===========================================
    # 5. 결정 실행
    # ===========================================

    def _resolve_sell_qty(self, decision: dict, pos: dict) -> int:
        """AI SELL 수량 해석. 수량/비중이 없으면 전량 매도한다."""
        held_qty = int(pos.get("qty", 0) or 0)
        if held_qty <= 0:
            return 0

        raw_qty = decision.get("sell_qty", decision.get("qty", decision.get("quantity")))
        try:
            sell_qty = int(raw_qty) if raw_qty not in (None, "") else 0
        except (TypeError, ValueError):
            sell_qty = 0

        if sell_qty <= 0:
            raw_pct = decision.get("sell_pct", decision.get("position_pct", decision.get("portion_pct")))
            try:
                pct = float(raw_pct) if raw_pct not in (None, "") else 0.0
            except (TypeError, ValueError):
                pct = 0.0
            if pct > 1:
                pct /= 100
            if pct > 0:
                sell_qty = max(1, round(held_qty * pct))

        if sell_qty <= 0:
            sell_qty = held_qty
        return max(1, min(held_qty, sell_qty))

    def _update_position_plan_from_decision(self, decision: dict) -> dict:
        """Apply AI target/stop revisions to an existing position."""
        code = decision.get("code", "")
        if not code and len(self._positions) == 1:
            code = self._positions[0]["code"]
        if not code:
            return {}

        pos = next((p for p in self._positions if p.get("code") == code), None)
        if not pos:
            return {}

        changes: dict[str, dict] = {}
        touched_plan = False
        for field in ("target_price", "stop_loss"):
            raw = decision.get(field)
            if raw in (None, "", 0):
                continue
            try:
                new_value = int(float(raw))
            except (TypeError, ValueError):
                continue
            if new_value <= 0:
                continue
            touched_plan = True
            old_value = int(pos.get(field, 0) or 0)
            if new_value != old_value:
                pos[field] = new_value
                changes[field] = {"old": old_value, "new": new_value}

        target_action = str(decision.get("target_action", "") or "").lower()
        should_record_validation = touched_plan or target_action in ("raise", "lower", "keep")
        if not changes and not should_record_validation:
            return {}

        pos["plan_updated_at"] = kst_now().isoformat()
        pos["plan_update_reason"] = decision.get("trader_reason", "")
        pos["target_action"] = target_action or "keep"
        logger.info("[%s] 목표/손절 조정: %s %s", self.BOT_ID, code, changes)
        return {
            "code": code,
            "changes": changes,
            "target_action": pos["target_action"],
            "validated": not bool(changes),
        }

    def _build_sell_basis(
        self,
        pos: dict,
        sell_qty: int,
        current_price: int = 0,
        reason: str = "",
        decision: dict | None = None,
    ) -> dict:
        """매도 판단 시점에 봇이 알고 있던 기준 가격을 보존한다."""
        basis = {
            "entry_price": int(pos.get("price", 0) or 0),
            "current_price": int(current_price or 0),
            "target_price": int(pos.get("target_price", 0) or 0),
            "stop_loss": int(pos.get("stop_loss", 0) or 0),
            "position_qty": int(pos.get("qty", 0) or 0),
            "sell_qty": int(sell_qty or 0),
            "reason": reason,
        }
        if decision:
            for src_key, dst_key in [
                ("price", "decision_price"),
                ("current_price", "decision_current_price"),
                ("target_price", "decision_target_price"),
                ("stop_loss", "decision_stop_loss"),
            ]:
                val = decision.get(src_key)
                if val not in (None, "", 0):
                    try:
                        basis[dst_key] = int(float(val))
                    except (TypeError, ValueError):
                        basis[dst_key] = val
        return basis

    # ===========================================
    # 매수 보호 장치 (연속 손절 차단 + 블록리스트)
    # ===========================================

    def _reset_daily_losses_if_new_day(self):
        """날짜가 바뀌면 당일 연속 손실 카운트 전체 리셋."""
        today = kst_now().strftime("%Y-%m-%d")
        if not hasattr(self, "_daily_losses") or getattr(self, "_daily_losses_date", "") != today:
            self._daily_losses = {}
            self._daily_losses_date = today

    def _track_sell_loss(self, code: str, pnl) -> None:
        """매도 확정 손익을 종목별 당일 연속 손실 카운트에 반영.

        손실이면 카운트 증가, 수익이면 리셋. 날짜가 바뀌면 전체 리셋.
        """
        if not code:
            return
        self._reset_daily_losses_if_new_day()
        if (pnl or 0) < 0:
            self._daily_losses[code] = self._daily_losses.get(code, 0) + 1
            logger.info(
                "[%s] %s 손실 확정 (당일 연속 %d회)",
                self.BOT_ID, code, self._daily_losses[code],
            )
        else:
            self._daily_losses.pop(code, None)  # 수익 시 카운터 리셋

    def _buy_block_reason(self, code: str) -> str:
        """매수 차단 사유 반환. 빈 문자열이면 통과. 매도는 차단하지 않는다."""
        if not code:
            return ""
        # 1. 종목 블록리스트
        blocklist = self._params.get("symbol_blocklist") or []
        if code in blocklist:
            return f"블록리스트 종목: {code}"
        # 2. 같은 종목 N연속 손절 시 당일 재진입 금지 (0=비활성)
        limit = int(self._params.get("consecutive_loss_block", 0) or 0)
        if limit > 0:
            self._reset_daily_losses_if_new_day()
            count = self._daily_losses.get(code, 0)
            if count >= limit:
                return f"연속 손절 차단: {code} 당일 {count}연속 손실"
        return ""

    def _execute_decision(self, decision: dict) -> dict:
        """AI 결정을 실제 매매로 실행."""
        action = decision["decision"]
        code = decision.get("code", "")
        name = decision.get("name", "")

        if action == "HOLD":
            plan_update = self._update_position_plan_from_decision(decision)
            result = {"success": True, "action": "hold", "reason": decision.get("trader_reason", "관망")}
            if plan_update:
                result["action"] = "adjust_plan"
                result["plan_update"] = plan_update
            return result

        if action == "BUY":
            if self._params.get("position_custodian"):
                return {"success": False, "reason": "포지션 관리 전용 모드 — 신규 매수 금지"}

            block_reason = self._buy_block_reason(code)
            if block_reason:
                logger.warning("[%s] BUY 차단 — %s", self.BOT_ID, block_reason)
                return {"success": False, "reason": block_reason}

            pos_idx = next((i for i, p in enumerate(self._positions) if p["code"] == code), None)

            # 포지션 수 제한. 기존 보유 종목은 신규 슬롯이 아니라 추매로 처리한다.
            max_pos = self._params["max_positions"]
            if pos_idx is None and len(self._positions) >= max_pos:
                return {"success": False, "reason": f"최대 포지션 수({max_pos}) 도달"}

            # 일일 거래 제한
            if not self._daily_limit.can_trade():
                return {"success": False, "reason": f"일일 거래 제한({self._daily_limit.max_trades}회) 도달"}

            # 현재가 조회
            try:
                info = self._get_market().get_price(code)
                price = info.get("price", 0) if info else 0
            except Exception:
                price = 0

            if price <= 0:
                return {"success": False, "reason": f"시세 조회 실패: {code}"}

            # 예산 분배: 전체 예산 / 최대 포지션 수
            position_budget = self._config.budget // max_pos if self._config.budget > 0 else 0
            qty = self.get_buyable_qty(code, price)
            if position_budget > 0:
                budget_qty = position_budget // price
                qty = min(qty, budget_qty)

            if qty <= 0:
                return {"success": False, "reason": "매수 가능 수량 없음"}

            # 매수 실행
            result = self.buy(code, qty, price)
            if result.get("success"):
                # 포지션 등록
                trailing = TrailingStop(
                    stop_pct=self._params["trailing_stop_pct"],
                    take_profit_pct=self._params["take_profit_pct"],
                    hard_stop_pct=self._params["hard_stop_pct"],
                )
                trailing.activate(price)
                # ATR 기반 동적 트레일링
                try:
                    candles = self._get_market().get_minute_price(code)
                    if candles and len(candles) > 15:
                        from src.bots.safeguards import calc_atr
                        atr = calc_atr(candles, 14)
                        if atr > 0:
                            trailing.atr_pct = atr
                            trailing.atr_multiplier = self._params.get("atr_multiplier", 2.0)
                except Exception:
                    pass
                hold_timer = MinHoldTime(min_minutes=self._params["min_hold_minutes"])
                hold_timer.mark_entry()

                if pos_idx is not None:
                    pos = self._positions[pos_idx]
                    old_qty = int(pos.get("qty", 0) or 0)
                    old_price = int(pos.get("price", 0) or 0)
                    total_qty = old_qty + qty
                    avg_price = int(round(((old_qty * old_price) + (qty * price)) / total_qty)) if total_qty > 0 else price
                    pos["qty"] = total_qty
                    pos["price"] = avg_price
                    pos["name"] = name or pos.get("name", "")
                    pos["last_added_at"] = kst_now().isoformat()
                    pos["adds"] = int(pos.get("adds", 0) or 0) + 1
                    pos["target_price"] = decision.get("target_price", pos.get("target_price", 0))
                    pos["stop_loss"] = decision.get("stop_loss", pos.get("stop_loss", 0))
                    pos["plan_updated_at"] = kst_now().isoformat()
                    pos["plan_update_reason"] = decision.get("trader_reason", "")
                    pos["target_action"] = decision.get("target_action", "keep")
                    if pos.get("trailing_stop"):
                        pos["trailing_stop"].entry_price = avg_price
                        pos["trailing_stop"].peak_price = max(pos["trailing_stop"].peak_price, price)
                    logger.info(
                        "[%s] 추매 완료: %s(%s) +%d주 @%d원 → 총 %d주 평균 %d원 -- %s",
                        self.BOT_ID, code, name, qty, price, total_qty, avg_price,
                        decision.get("trader_reason", ""),
                    )
                else:
                    self._positions.append({
                        "code": code,
                        "name": name,
                        "qty": qty,
                        "price": price,
                        "bought_at": kst_now().isoformat(),
                        "entry_time": kst_now(),
                        "trailing_stop": trailing,
                        "hold_timer": hold_timer,
                        "target_price": decision.get("target_price", 0),
                        "stop_loss": decision.get("stop_loss", 0),
                        "adds": 0,
                    })
                    logger.info(
                        "[%s] 매수 완료: %s(%s) %d주 @%d원 -- %s",
                        self.BOT_ID, code, name, qty, price,
                        decision.get("trader_reason", ""),
                    )
                self._daily_limit.record_trade()
                result["action"] = "add_buy" if pos_idx is not None else "buy"
                result["code"] = code
                result["qty"] = qty
                result["price"] = price
                if pos_idx is not None:
                    result["total_qty"] = self._positions[pos_idx]["qty"]
                    result["avg_price"] = self._positions[pos_idx]["price"]
            return result

        if action == "SELL":
            # 보유 포지션 확인
            pos_idx = None
            for i, pos in enumerate(self._positions):
                if pos["code"] == code:
                    pos_idx = i
                    break

            if pos_idx is None:
                return {"success": False, "reason": f"보유하지 않은 종목: {code}"}

            pos = self._positions[pos_idx]
            outcome_run_id = pos.get("audit_outcome_run_id", "")
            buy_trade_id = pos.get("audit_buy_trade_id", 0)
            entry_time = self._audit_iso(pos.get("entry_time") or pos.get("bought_at"))

            # 최소 보유 시간 체크
            if not pos["hold_timer"].can_sell():
                remaining = pos["hold_timer"].remaining_minutes()
                return {"success": False, "reason": f"최소 보유 시간 미달 ({remaining:.1f}분 남음)"}

            sell_qty = self._resolve_sell_qty(decision, pos)
            original_qty = int(pos.get("qty", 0) or 0)
            if sell_qty <= 0:
                return {"success": False, "reason": f"매도 가능 수량 없음: {code}"}

            current_price = int(decision.get("price", 0) or decision.get("current_price", 0) or 0)
            if current_price <= 0:
                try:
                    info = self._get_market().get_price(code)
                    current_price = int(info.get("price", 0) or 0) if info else 0
                except Exception:
                    current_price = 0
            reason = decision.get("trader_reason", "AI SELL")
            basis = self._build_sell_basis(
                pos,
                sell_qty,
                current_price=current_price,
                reason=reason,
                decision=decision,
            )
            basis["outcome_run_id"] = outcome_run_id
            basis["buy_trade_id"] = buy_trade_id
            basis["entry_time"] = entry_time

            # 매도 실행 (다중 포지션 — 매수가 명시 전달)
            result = self.sell(code, sell_qty, entry_price=pos["price"], reason=reason, basis=basis)
            if result.get("success"):
                executed_qty = self._audit_prepare_sell_result(result, sell_qty, pos)
                if executed_qty >= original_qty:
                    pos["trailing_stop"].deactivate()
                    pos["hold_timer"].clear()
                    self._positions.pop(pos_idx)
                    result["action"] = "sell"
                else:
                    pos["qty"] = original_qty - executed_qty
                    pos["last_sold_at"] = kst_now().isoformat()
                    pos["sells"] = int(pos.get("sells", 0) or 0) + 1
                    plan_update = self._update_position_plan_from_decision(decision)
                    result["action"] = "partial_sell"
                    result["remaining_qty"] = pos["qty"]
                    if plan_update:
                        result["plan_update"] = plan_update
                self._daily_limit.record_trade()
                self._track_sell_loss(code, result.get("realized_pnl", 0))
                logger.info(
                    "[%s] %s: %s(%s) %d/%d주 -- %s",
                    self.BOT_ID,
                    "분할 매도" if executed_qty < original_qty else "매도 완료",
                    code, pos.get("name", ""), executed_qty, original_qty,
                    decision.get("trader_reason", ""),
                )
            elif "실잔고 부족" in result.get("message", ""):
                # KIS 실잔고 0 → phantom 포지션 정리 (다음 스캔 무한 재시도 방지)
                if pos.get("trailing_stop"):
                    pos["trailing_stop"].deactivate()
                if pos.get("hold_timer"):
                    pos["hold_timer"].clear()
                self._positions.pop(pos_idx)
                self._save_state()
                result["action"] = "phantom_cleared"
                logger.warning(
                    "[%s] phantom 포지션 제거: %s(%s) — KIS 실잔고 0",
                    self.BOT_ID, code, pos.get("name", ""),
                )
            return result

        return {"success": False, "reason": f"알 수 없는 action: {action}"}

    # ===========================================
    # 포지션 관리 (트레일링 스탑 + 장 마감)
    # ===========================================

    def _position_held_minutes(self, pos: dict) -> float:
        raw = pos.get("bought_at")
        entry_time = pos.get("entry_time")
        try:
            if raw:
                return max(0.0, (kst_now() - datetime.fromisoformat(raw)).total_seconds() / 60)
            if entry_time:
                return max(0.0, (kst_now() - entry_time).total_seconds() / 60)
        except (TypeError, ValueError):
            return 0.0
        return 0.0

    def _position_trend_snapshot(self, market, code: str, current_price: int) -> dict:
        """보유 종목의 최근 분봉 하락 압력을 요약한다."""
        try:
            candles = market.get_minute_price(code)
        except Exception:
            return {}
        if not isinstance(candles, list) or len(candles) < 2:
            return {}

        def _price(c: dict) -> int:
            return int(c.get("price") or c.get("close") or 0)

        now_price = int(current_price or _price(candles[0]) or 0)
        older_idx = min(9, len(candles) - 1)
        older_price = _price(candles[older_idx])
        recent_high = max(int(c.get("high") or _price(c) or 0) for c in candles[: min(10, len(candles))])

        snapshot = {}
        if now_price > 0 and older_price > 0:
            snapshot["minute_trend_pct"] = round((now_price - older_price) / older_price * 100, 2)
        if now_price > 0 and recent_high > 0:
            snapshot["recent_high_drawdown_pct"] = round((now_price - recent_high) / recent_high * 100, 2)
        return snapshot

    def _position_loss_thresholds(self, pos: dict) -> dict:
        trailing = pos.get("trailing_stop")
        hard_stop = float(
            getattr(trailing, "hard_stop_pct", self._params.get("hard_stop_pct", -2.0))
        )
        review_pct = self._params.get("loss_review_pct")
        if review_pct is None:
            review_pct = hard_stop * 0.35
        breakdown_pct = self._params.get("loss_breakdown_pct")
        if breakdown_pct is None:
            breakdown_pct = hard_stop * 0.75
        return {
            "review_pct": float(review_pct),
            "breakdown_pct": float(breakdown_pct),
            "review_trend_pct": float(self._params.get("loss_review_trend_pct", -0.15)),
            "breakdown_trend_pct": float(self._params.get("loss_breakdown_trend_pct", -0.35)),
        }

    def _validate_position_plan(self, review: dict) -> dict:
        """목표가/손절가가 현재가 기준으로 계속 유효한지 매번 검증한다."""
        current = int(review.get("current_price", 0) or 0)
        target = int(review.get("target_price", 0) or 0)
        stop = int(review.get("stop_loss", 0) or 0)
        min_rr = float(self._params.get("min_plan_risk_reward", 1.2))

        validation = {
            "plan_valid": True,
            "plan_status": "ok",
            "plan_warning": "",
            "target_gap_pct": 0.0,
            "stop_gap_pct": 0.0,
            "risk_reward": None,
        }
        if current <= 0:
            validation.update({
                "plan_valid": False,
                "plan_status": "unpriced",
                "plan_warning": "현재가 조회 실패로 목표/손절 검증 불가",
            })
            return validation

        if target > 0:
            validation["target_gap_pct"] = round((target - current) / current * 100, 2)
        if stop > 0:
            validation["stop_gap_pct"] = round((current - stop) / current * 100, 2)
        if target > current and 0 < stop < current:
            validation["risk_reward"] = round((target - current) / (current - stop), 2)

        if target <= 0 or stop <= 0:
            validation.update({
                "plan_valid": False,
                "plan_status": "missing",
                "plan_warning": "목표가/손절가 미설정",
            })
        elif stop >= current:
            validation.update({
                "plan_valid": False,
                "plan_status": "invalid_stop",
                "plan_warning": f"손절가가 현재가 이상({stop:,} >= {current:,})",
            })
        elif target <= current:
            validation.update({
                "plan_valid": False,
                "plan_status": "target_reached",
                "plan_warning": f"목표가 도달/초과({target:,} <= {current:,})",
            })
        elif validation["risk_reward"] is not None and validation["risk_reward"] < min_rr:
            validation.update({
                "plan_valid": False,
                "plan_status": "poor_risk_reward",
                "plan_warning": f"손익비 부족({validation['risk_reward']:.2f}:1 < {min_rr:.2f}:1)",
            })

        return validation

    @staticmethod
    def _format_exit_label(
        trigger: str,
        pnl_pct: float | None,
        *,
        current_price: int = 0,
        stop: int = 0,
        peak_drawdown_pct: float | None = None,
        held_minutes: float | None = None,
        max_hold: int | None = None,
        raw_reason: str = "",
    ) -> str:
        """청산 사유 라벨 생성 — 실제 트리거별로 구분하고 판정 시점 손익을 포함한다.

        판정 로직과 무관한 순수 텍스트 포맷터. 라벨만 바꾸고 판정 조건은 건드리지 않는다.
        - ai_stop: AI가 정한 stop_price 터치. 손실이면 "AI 손절가 도달",
          수익이면(스탑을 진입가 위로 올린 경우) "AI 스탑 도달 ... 이익 보전" — '손절' 미포함.
        - hard_stop: 진입가 대비 하드 손절.
        - trailing: 고점 대비 트레일링 스탑.
        - time_limit: 최대 보유시간 초과 청산.
        """
        pnl_txt = f"{pnl_pct:+.2f}%" if isinstance(pnl_pct, (int, float)) else ""

        if trigger == "ai_stop":
            detail = f"({current_price:,} <= {stop:,})"
            if isinstance(pnl_pct, (int, float)) and pnl_pct >= 0:
                return f"AI 스탑 도달 {pnl_txt} 이익 보전 {detail}"
            return f"AI 손절가 도달 {pnl_txt} {detail}".replace("  ", " ")

        if trigger == "hard_stop":
            return f"하드 손절 {pnl_txt}".strip()

        if trigger == "trailing":
            parts = []
            if isinstance(peak_drawdown_pct, (int, float)):
                parts.append(f"고점 대비 {peak_drawdown_pct:.2f}%")
            if pnl_txt:
                parts.append(f"손익 {pnl_txt}")
            if parts:
                return f"트레일링 스탑 ({', '.join(parts)})"
            return raw_reason or "트레일링 스탑"

        if trigger == "time_limit":
            parts = []
            if held_minutes is not None and max_hold is not None:
                parts.append(f"{held_minutes:.0f}분 > {max_hold}분")
            if pnl_txt:
                parts.append(f"손익 {pnl_txt}")
            if parts:
                return f"보유시간 제한 청산 ({', '.join(parts)})"
            return "보유시간 제한 청산"

        return raw_reason

    def _check_positions(self) -> list[dict]:
        """매 루프마다 모든 보유 포지션을 평가한다."""
        actions = []
        reviews = []
        market = self._get_market()
        from src.bots.safeguards import get_market_session
        session = get_market_session()
        can_sell = bool(session.get("can_sell"))
        session_label = session.get("label", "?")

        def append_exit(review: dict, action: dict):
            if can_sell:
                actions.append(action)
                return
            review["decision"] = "blocked_sell"
            review["reason"] = f"매도 불가 세션({session_label}) - {review['reason']}"

        for pos in self._positions:
            try:
                info = market.get_price(pos["code"])
                current_price = info.get("price", 0) if info else 0
            except Exception:
                current_price = 0

            if current_price <= 0:
                reviews.append({
                    "code": pos["code"],
                    "name": pos.get("name", ""),
                    "qty": pos.get("qty", 0),
                    "decision": "unpriced",
                    "reason": "현재가 조회 실패",
                })
                continue

            entry = int(pos.get("price", 0) or 0)
            pnl_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0
            target = int(pos.get("target_price", 0) or 0)
            stop = int(pos.get("stop_loss", 0) or 0)
            review = {
                "code": pos["code"],
                "name": pos.get("name", ""),
                "qty": pos.get("qty", 0),
                "entry_price": entry,
                "current_price": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "held_min": round(self._position_held_minutes(pos), 1),
                "target_price": target,
                "stop_loss": stop,
                "decision": "hold",
                "reason": "보유 조건 유지",
            }
            trend = self._position_trend_snapshot(market, pos["code"], current_price)
            review.update(trend)
            plan_validation = self._validate_position_plan(review)
            review.update(plan_validation)

            if stop > 0 and current_price <= stop:
                review["decision"] = "sell"
                review["reason"] = self._format_exit_label(
                    "ai_stop", pnl_pct, current_price=current_price, stop=stop,
                )
                append_exit(review, {
                    "action": "sell",
                    "code": pos["code"],
                    "reason": review["reason"],
                    "current_price": current_price,
                    "force_close": False,
                })
                reviews.append(review)
                continue

            if not plan_validation.get("plan_valid", True):
                review["decision"] = "review_plan"
                review["reason"] = (
                    f"목표/손절 계획 재검증 필요 - {plan_validation.get('plan_warning', '')}"
                )

            thresholds = self._position_loss_thresholds(pos)
            minute_trend = review.get("minute_trend_pct")
            is_downtrend = (
                isinstance(minute_trend, (int, float))
                and minute_trend <= thresholds["review_trend_pct"]
            )
            is_breakdown = (
                pnl_pct <= thresholds["breakdown_pct"]
                and isinstance(minute_trend, (int, float))
                and minute_trend <= thresholds["breakdown_trend_pct"]
            )
            needs_loss_review = (
                pnl_pct <= thresholds["review_pct"]
                or (pnl_pct < 0 and is_downtrend)
            )

            if is_breakdown:
                review["decision"] = "sell"
                review["reason"] = (
                    f"손실 확대 + 단기 하락 지속 "
                    f"(손익 {pnl_pct:.2f}%, 10분 {minute_trend:.2f}%)"
                )
                append_exit(review, {
                    "action": "sell",
                    "code": pos["code"],
                    "reason": review["reason"],
                    "current_price": current_price,
                    "force_close": False,
                })
                reviews.append(review)
                continue

            if needs_loss_review:
                trend_txt = f", 10분 {minute_trend:.2f}%" if isinstance(minute_trend, (int, float)) else ""
                review["decision"] = "review_loss"
                review["reason"] = (
                    f"손실 보유 재검토 필요 "
                    f"(손익 {pnl_pct:.2f}%{trend_txt}) - SELL/부분매도/추매/HOLD 근거 재판단"
                )

            if target > 0 and current_price >= target:
                review["decision"] = "review_target"
                review["reason"] = (
                    f"AI 목표가 도달 {current_price:,} >= {target:,} "
                    "- 익절/목표 상향/손절 상향 재평가 필요"
                )

            # 트레일링 스탑 업데이트
            pos["trailing_stop"].update(current_price)

            # 청산 체크
            should_exit, exit_reason = pos["trailing_stop"].should_exit(current_price)
            if should_exit:
                review["system_exit_reason"] = exit_reason
                if exit_reason.startswith("익절"):
                    if review["decision"] == "hold":
                        review["decision"] = "review_target"
                        review["reason"] = f"{exit_reason} - 익절/목표 상향/손절 상향 재평가 필요"
                    reviews.append(review)
                    continue
                if exit_reason.startswith("하드손절"):
                    exit_label = self._format_exit_label("hard_stop", pnl_pct)
                elif exit_reason.startswith("트레일링"):
                    peak = getattr(pos["trailing_stop"], "peak_price", 0) or 0
                    peak_dd = (
                        (current_price - peak) / peak * 100 if peak > 0 else None
                    )
                    exit_label = self._format_exit_label(
                        "trailing", pnl_pct,
                        peak_drawdown_pct=peak_dd, raw_reason=exit_reason,
                    )
                else:
                    exit_label = exit_reason
                review["decision"] = "sell"
                review["reason"] = exit_label
                append_exit(review, {
                    "action": "sell",
                    "code": pos["code"],
                    "reason": exit_label,
                    "current_price": current_price,
                    "force_close": False,
                })
            reviews.append(review)

        # 장 마감 강제 청산 (15:00 이후 진입은 오버나잇 허용)
        if self._market_guard.should_force_close() and not self._market_guard.was_close_attempted():
            order_type = self._market_guard.get_order_type()
            if order_type != "NONE":
                for pos in self._positions:
                    entry_t = pos.get("entry_time")
                    if entry_t and entry_t.time() >= dtime(15, 0):
                        continue  # 오버나잇 대상 — 청산 제외
                    actions.append({
                        "action": "sell",
                        "code": pos["code"],
                        "reason": f"장 마감 강제 청산 ({order_type})",
                        "force_close": True,
                        "order_type": order_type,
                    })

        self._last_position_reviews = reviews
        return actions

    def _ai_failure_exit_actions(self) -> list[dict]:
        """Fail closed on profitable target hits when AI cannot review them."""
        from src.bots.safeguards import get_market_session

        session = get_market_session()
        if not session.get("can_sell"):
            return []

        actions = []
        for review in self._last_position_reviews:
            code = review.get("code", "")
            current_price = int(review.get("current_price", 0) or 0)
            system_exit = review.get("system_exit_reason", "")
            target_reached = (
                review.get("decision") == "review_target"
                or review.get("plan_status") == "target_reached"
                or system_exit.startswith("익절")
            )
            if not code or current_price <= 0 or not target_reached:
                continue
            reason = (
                "AI 호출 실패 fallback: 목표/익절 도달 "
                f"(손익 {review.get('pnl_pct', 0):+.2f}%, 현재가 {current_price:,})"
            )
            actions.append({
                "action": "sell",
                "code": code,
                "reason": reason,
                "current_price": current_price,
                "force_close": False,
            })

        if actions:
            self._post_to_board(
                "alert",
                f"[{self.BOT_ID} fallback 청산] AI 불가 중 목표/익절 도달 {len(actions)}건",
                actions[0].get("code", ""),
            )
        return actions

    def _execute_position_exit(self, exit_action: dict) -> dict:
        """포지션 청산 실행."""
        code = exit_action["code"]
        force_close = exit_action.get("force_close", False)

        pos_idx = None
        for i, pos in enumerate(self._positions):
            if pos["code"] == code:
                pos_idx = i
                break

        if pos_idx is None:
            return {"success": False, "reason": f"포지션을 찾을 수 없음: {code}"}

        pos = self._positions[pos_idx]
        outcome_run_id = pos.get("audit_outcome_run_id", "")
        buy_trade_id = pos.get("audit_buy_trade_id", 0)
        entry_time = self._audit_iso(pos.get("entry_time") or pos.get("bought_at"))

        # 최소 보유 시간 (장 마감은 무시)
        if not force_close and not pos["hold_timer"].can_sell():
            remaining = pos["hold_timer"].remaining_minutes()
            return {"success": False, "reason": f"최소 보유 시간 미달 ({remaining:.1f}분 남음)"}

        # 주문 유형 결정 (시간외 종가 or 시장가)
        from src.kis.order import OrderType
        order_type_str = exit_action.get("order_type", "MARKET")
        order_type = OrderType.AFTER_HOURS_CLOSE if order_type_str == "AFTER_HOURS" else OrderType.MARKET
        sell_qty = self._resolve_sell_qty(exit_action, pos)
        original_qty = int(pos.get("qty", 0) or 0)
        reason = exit_action.get("reason", "")
        basis = self._build_sell_basis(
            pos,
            sell_qty,
            current_price=exit_action.get("current_price", 0),
            reason=reason,
            decision=exit_action,
        )
        basis["outcome_run_id"] = outcome_run_id
        basis["buy_trade_id"] = buy_trade_id
        basis["entry_time"] = entry_time
        result_dict = self.sell(
            code,
            sell_qty,
            entry_price=pos["price"],
            order_type=order_type,
            reason=reason,
            basis=basis,
        )

        if result_dict.get("success"):
            executed_qty = self._audit_prepare_sell_result(result_dict, sell_qty, pos)
            if executed_qty >= original_qty:
                pos["trailing_stop"].deactivate()
                pos["hold_timer"].clear()
                self._positions.pop(pos_idx)
                result_dict["action"] = "sell"
            else:
                pos["qty"] = original_qty - executed_qty
                pos["last_sold_at"] = kst_now().isoformat()
                pos["sells"] = int(pos.get("sells", 0) or 0) + 1
                result_dict["action"] = "partial_sell"
                result_dict["remaining_qty"] = pos["qty"]
            self._track_sell_loss(code, result_dict.get("realized_pnl", 0))
            if force_close:
                self._market_guard.mark_close_attempted()
            logger.info(
                "[%s] 포지션 %s: %s %d/%d주 -- %s",
                self.BOT_ID,
                "분할 청산" if executed_qty < original_qty else "청산",
                code, executed_qty, original_qty, exit_action.get("reason", ""),
            )
        elif force_close:
            self._market_guard.mark_close_attempted()
            logger.warning(
                "[%s] 장 마감 청산 실패: %s -- 재시도 중단",
                self.BOT_ID, code,
            )

        return result_dict

    # ===========================================
    # AI 호출 사전 필터링 (토큰 절약)
    # ===========================================

    def _should_skip_ai(self) -> str:
        """AI 호출 전 사전 체크. 스킵 사유 반환 (빈 문자열이면 호출 진행).

        비용: KIS API 0~1건 vs AI 17,000 토큰. 사전 필터링이 압도적 이득.
        """
        from src.bots.safeguards import get_market_session
        import src.trading_mode as tm
        session = get_market_session()
        self._last_preflight = {}

        if self._params.get("position_custodian") and not self._positions:
            return "포지션 관리 전용 모드 + 보유 없음"

        # 1. 매수/매도 둘 다 불가한 세션이면 AI 불필요
        if not session.get("can_buy") and not session.get("can_sell"):
            return f"매매 불가 세션: {session.get('label', '?')}"

        # 2. 주문 비활성화 + 보유 없음이면 AI가 실행할 일이 없음
        if tm.current_mode() == tm.DISABLED and not self._positions:
            return "주문 비활성화 상태 + 보유 없음"

        # 3. 일일 거래 제한 소진 + 보유 포지션 없으면 AI 불필요
        if not self._daily_limit.can_trade() and not self._positions:
            return "일일 거래 제한 소진 + 보유 없음"

        # 4. 마감 10분 전 + 보유 없으면 신규 진입 의미 없음
        if session.get("minutes_to_close", 999) < 10 and not self._positions:
            return "마감 임박 + 보유 없음"

        # 5. 보유가 없고 신규 매수만 가능한 상황이면 실제 주문가능수량부터 확인
        max_pos = int(self._params.get("max_positions", 1) or 1)
        if not self._positions and session.get("can_buy") and self._daily_limit.can_trade() and max_pos > 0:
            preflight = self._domestic_buy_preflight()
            self._last_preflight = preflight
            if not preflight.get("buyable_candidates"):
                return preflight.get("reason", "실제 매수 가능 종목 없음")

        return ""  # AI 호출 진행

    @staticmethod
    def _format_preflight_items(items: list[dict], limit: int = 4) -> str:
        chunks = []
        for item in items[:limit]:
            code = item.get("code", "")
            name = item.get("name", "")
            price = int(item.get("price", 0) or 0)
            qty = int(item.get("qty", 0) or 0)
            reason = item.get("reason", "")
            label = f"{code} {name}".strip()
            if qty > 0:
                chunks.append(f"{label} {qty}주 가능 @{price:,}")
            elif reason:
                chunks.append(f"{label} 차단:{reason}")
            else:
                chunks.append(f"{label} @{price:,}")
        return ", ".join(chunks)

    def _format_buy_preflight_summary(self, preflight: dict | None = None) -> str:
        """Human-readable buy-search audit summary."""
        preflight = preflight or self._last_preflight or {}
        if not preflight:
            return ""

        reason = preflight.get("reason") or "매수 후보 점검"
        cash_cap = int(preflight.get("cash_cap", 0) or 0)
        priced_count = int(preflight.get("priced_count", 0) or 0)
        etf_count = int(preflight.get("etf_count", 0) or 0)
        candidates = preflight.get("buyable_candidates") or []
        blocked = preflight.get("blocked_candidates") or []
        price_errors = preflight.get("price_errors") or []

        parts = [str(reason)]
        if cash_cap:
            parts.append(f"가용캡 {cash_cap:,}원")
        if etf_count:
            parts.append(f"스캔 {etf_count}개")
        parts.append(f"가격 후보 {priced_count}개")
        if candidates:
            parts.append(f"주문가능 {len(candidates)}개: {self._format_preflight_items(candidates)}")
        if blocked:
            parts.append(f"룰 차단 {len(blocked)}개: {self._format_preflight_items(blocked)}")
        if price_errors:
            sample = ", ".join(f"{e.get('code','?')}:{e.get('reason','')}" for e in price_errors[:3])
            parts.append(f"가격조회 실패 {len(price_errors)}개: {sample}")
        return " | ".join(p for p in parts if p)

    def _post_buy_preflight_if_needed(self):
        preflight = self._last_preflight or {}
        if not preflight:
            return
        if preflight.get("buyable_candidates"):
            return

        key = "|".join([
            str(preflight.get("reason", "")),
            str(preflight.get("cash_cap", "")),
            str(preflight.get("priced_count", "")),
            str(len(preflight.get("blocked_candidates") or [])),
            str(len(preflight.get("price_errors") or [])),
        ])
        if key == self._last_preflight_board_key:
            return

        self._last_preflight_board_key = key
        import json as _json
        try:
            extra = _json.dumps(preflight, ensure_ascii=False, default=str)
        except Exception:
            extra = ""
        self._post_to_board(
            "analysis",
            f"[{self.BOT_ID} 매수 후보 점검] {self._format_buy_preflight_summary(preflight)}",
            "",
            extra,
        )

    def _domestic_buy_preflight(self) -> dict:
        """AI 호출 전 국내 ETF 실제 주문가능 후보를 확인한다."""
        import src.trading_mode as tm

        mode = tm.current_mode()
        if mode == tm.DISABLED:
            return {"buyable_candidates": [], "reason": "주문 비활성화 상태"}

        market = self._get_market()
        max_order = tm.max_order_amount()
        budget_cap = self._config.budget if self._config.budget > 0 else max_order
        cash_cap = min(budget_cap, max_order) if max_order > 0 else budget_cap
        base = {
            "budget_cap": budget_cap,
            "max_order": max_order,
            "cash_cap": cash_cap,
            "etf_count": len(self._config.etf_codes),
        }
        if cash_cap <= 0:
            return {**base, "buyable_candidates": [], "priced_count": 0, "reason": "봇 예산 0원"}

        priced = []
        price_errors = []
        blocklist = set(self._params.get("symbol_blocklist") or [])
        # 파생ETF 토글 OFF면 인버스/레버리지를 매수 후보에서 제외
        for code in self._filter_derivative_etfs(self._config.etf_codes):
            if code in blocklist:
                continue  # 블록리스트 종목은 매수 후보에서 제외 (매도는 허용)
            try:
                info = market.get_price(code)
                price = int((info or {}).get("price", 0) or 0)
                if price <= 0 or price > cash_cap:
                    continue
                priced.append({
                    "code": code,
                    "name": (info or {}).get("name", code),
                    "price": price,
                })
            except Exception as e:
                if len(price_errors) < 8:
                    price_errors.append({"code": code, "reason": str(e)})
                continue

        if not priced:
            return {
                **base,
                "buyable_candidates": [],
                "priced_count": 0,
                "price_errors": price_errors,
                "reason": f"예산/최대주문액 내 가격 종목 없음: 가용캡 {cash_cap:,}원",
            }

        candidates = []
        blocked = []
        buyable_errors = []
        for item in sorted(priced, key=lambda x: x["price"])[:8]:
            try:
                block = self._buy_block_reason(item["code"]) \
                    or self.engine._pre_buy_checks(item["code"], item["price"])
                if block:
                    blocked.append({**item, "reason": block})
                    continue
                buyable = market.get_buyable_qty(item["code"], item["price"])
                qty = int(buyable.get("max_qty", 0) or 0)
                budget_qty = cash_cap // item["price"]
                qty = min(qty, budget_qty)
                if qty > 0:
                    candidates.append({
                        **item,
                        "qty": qty,
                        "amount": int(buyable.get("max_amount", 0) or 0),
                        "cash": int(buyable.get("cash", 0) or 0),
                    })
            except Exception as e:
                if len(buyable_errors) < 8:
                    buyable_errors.append({**item, "reason": str(e)})
                continue

        if candidates:
            return {
                **base,
                "buyable_candidates": candidates,
                "blocked_candidates": blocked,
                "priced_count": len(priced),
                "priced_sample": sorted(priced, key=lambda x: x["price"])[:8],
                "price_errors": price_errors,
                "buyable_errors": buyable_errors,
                "reason": "OK",
            }

        return {
            **base,
            "buyable_candidates": [],
            "blocked_candidates": blocked,
            "priced_count": len(priced),
            "priced_sample": sorted(priced, key=lambda x: x["price"])[:8],
            "price_errors": price_errors,
            "buyable_errors": buyable_errors,
            "reason": (
                f"실제 주문 가능 후보 없음: 가용캡 {cash_cap:,}원, "
                f"가격 후보 {len(priced)}개, 룰 차단 {len(blocked)}개"
            ),
        }

    # ===========================================
    # BotBase 추상 메서드 구현
    # ===========================================

    def scan(self) -> list[dict]:
        """스캔 -- 다중 포지션 체크 + 데이터 수집."""
        self._reload_params_if_needed()
        return []  # AI 봇은 scan/decide/execute 패턴 대신 _loop 오버라이드

    def decide(self, candidates: list[dict]) -> dict:
        """판단 -- AI 봇은 _loop에서 직접 처리."""
        return {"action": "skip", "reason": "AI 분석 대기"}

    def execute(self, decision: dict) -> dict:
        """실행 -- AI 봇은 _loop에서 직접 처리."""
        return {"success": True, "action": "skip"}

    # ===========================================
    # 메인 루프 오버라이드
    # ===========================================

    async def _loop(self):
        """메인 루프: 5분 간격으로 AI 분석 실행."""
        logger.info(
            "[%s] 시작 -- %d개 ETF, %ds 간격, confidence >= %.1f, 최대 %d 포지션",
            self.BOT_ID,
            len(self._config.etf_codes),
            self._config.interval_seconds,
            self._params["confidence_threshold"],
            self._params["max_positions"],
        )

        while self._running:
            audit = None
            audit_finished = False
            try:
                # 주말 체크 — 토/일은 스캔하지 않음
                if kst_now().weekday() >= 5:  # 5=토, 6=일
                    await asyncio.sleep(300)  # 5분 대기 후 재확인
                    continue

                self._scan_count += 1
                now = kst_now()
                loop = asyncio.get_running_loop()
                context = ""
                prompt = ""
                output = ""
                ai_attempted = False
                self._last_ai_error = ""
                self._last_raw_output_preview = ""
                self._last_ai_call_meta = {}

                # 에볼버 config 리로드
                self._reload_params_if_needed()
                self._reconcile_positions_with_trades()
                audit = self._audit_start_run()

                # 1. 포지션 트레일링 스탑 / 장 마감 체크
                exit_actions = self._check_positions()
                exit_results = []
                for exit_action in exit_actions:
                    result = self._execute_position_exit(exit_action)
                    if result.get("success"):
                        exit_results.append({
                            "code": exit_action["code"],
                            "reason": exit_action.get("reason", ""),
                            "result": result,
                        })
                        self._publish_event("bot_trade", {
                            "action": "sell",
                            "code": exit_action["code"],
                            "result": result,
                        })

                # 2. AI 분석 (장 운영시간 + 사전 필터링)
                ai_decision = None
                skip_reason = self._should_skip_ai()
                if exit_results:
                    exited = ", ".join(e["code"] for e in exit_results)
                    skip_reason = f"자동 청산 후 동일 스캔 재진입 방지: {exited}"
                if self._market_guard.is_market_open() and not skip_reason:
                    # 데이터 수집 (Python이 KIS API 호출) + 프롬프트 생성
                    context = await loop.run_in_executor(None, self._collect_data)
                    self._audit_record_evidence(audit, context, getattr(self, "_last_collected_data", {}))

                    # AI 호출 (MCP 도구 접근 가능)
                    prompt = self._build_codex_prompt(context)
                    ai_attempted = True
                    output = await loop.run_in_executor(None, self._invoke_codex, prompt)
                elif skip_reason:
                    output = ""
                    logger.info("[%s] AI 호출 생략: %s", self.BOT_ID, skip_reason)
                else:
                    output = ""

                # 결정 파싱과 AI 감사는 실제 AI 호출이 있었을 때만 기록한다.
                if ai_attempted:
                    ai_decision = self._parse_decision(output)
                    meta = getattr(self, "_last_ai_call_meta", {}) or {}
                    self._audit_record_ai(
                        audit,
                        prompt=prompt,
                        output=output,
                        ai_decision=ai_decision,
                        provider=meta.get("provider", ""),
                        model=meta.get("model", ""),
                        tokens=meta.get("tokens", 0),
                        latency_ms=meta.get("latency_ms", 0),
                    )
                if ai_decision is None and not exit_results and not skip_reason:
                    fallback_actions = self._ai_failure_exit_actions()
                    for exit_action in fallback_actions:
                        result = self._execute_position_exit(exit_action)
                        if result.get("success"):
                            exit_results.append({
                                "code": exit_action["code"],
                                "reason": exit_action.get("reason", ""),
                                "result": result,
                            })
                            self._publish_event("bot_trade", {
                                "action": "sell",
                                "code": exit_action["code"],
                                "result": result,
                            })

                # 분석 결과 저장 (UI용)
                self._last_analysis = {
                    "time": now.isoformat(),
                    "scan": self._scan_count,
                    "decision": ai_decision,
                    "position_reviews": self._last_position_reviews,
                    "raw_output_length": len(output) if output else 0,
                }
                if self._last_ai_error:
                    self._last_analysis["ai_error"] = self._last_ai_error
                if self._last_raw_output_preview and ai_decision is None:
                    self._last_analysis["raw_output_preview"] = self._last_raw_output_preview
                preflight_summary = ""
                if self._last_preflight:
                    preflight_summary = self._format_buy_preflight_summary(self._last_preflight)
                    self._last_analysis["buy_preflight"] = self._last_preflight
                    self._last_analysis["buy_preflight_summary"] = preflight_summary
                self._audit_record_preflight(
                    audit,
                    getattr(self, "_last_preflight", {}) or {},
                    stock_code=ai_decision.get("code", "") if ai_decision else "",
                )

                # 토론 기록
                if ai_decision:
                    debate_entry = {
                        "time": now.strftime("%H:%M"),
                        "decision": ai_decision.get("decision", "HOLD"),
                        "confidence": ai_decision.get("confidence", 0),
                        "code": ai_decision.get("code", ""),
                        "bull": ai_decision.get("bull_summary", ""),
                        "bear": ai_decision.get("bear_summary", ""),
                        "trader": ai_decision.get("trader_reason", ""),
                    }
                    self._debate_log.append(debate_entry)
                    if len(self._debate_log) > 20:
                        self._debate_log = self._debate_log[-20:]

                    # 게시판에 AI 의견 등록
                    opinion = f"[{ai_decision['decision']}] {ai_decision.get('trader_reason', '')}"
                    import json as _json
                    extra = ""
                    if ai_decision.get("criteria"):
                        try:
                            extra = _json.dumps(ai_decision, ensure_ascii=False)
                        except Exception:
                            pass
                    self._post_to_board("opinion", opinion, ai_decision.get("code", ""), extra)

                # 결정 실행
                result = {}
                if ai_decision:
                    result = self._execute_decision(ai_decision)
                    if ai_decision["decision"] in ("BUY", "SELL") and result.get("success"):
                        executed_action = result.get("action") or ai_decision["decision"].lower()
                        self._publish_event("bot_trade", {
                            "action": executed_action,
                            "code": ai_decision.get("code", ""),
                            "result": result,
                        })

                # 3. 로그 기록
                action = "hold"
                if not self._market_guard.is_market_open():
                    detail = "장 외 시간 대기"
                elif skip_reason:
                    detail = f"AI 호출 생략: {preflight_summary or skip_reason}"
                elif ai_decision is None:
                    detail = self._last_ai_error or "AI 호출 실패 또는 파싱 실패"
                    if preflight_summary:
                        detail = f"{detail} | 매수 후보 점검: {preflight_summary}"
                else:
                    detail = "판단 대기"
                if exit_results and not ai_decision:
                    action = "sell"
                    detail = "; ".join(
                        f"{e['code']} {e['reason']}" for e in exit_results
                    )
                    if not result:
                        result = exit_results[-1].get("result", {})
                if ai_decision:
                    action = result.get("action") or ai_decision["decision"].lower()
                    detail = ai_decision.get("trader_reason", "")
                    if action in ("buy", "sell") and result and not result.get("success"):
                        action = f"blocked_{action}"
                        detail = (
                            f"주문 차단: {result.get('message') or result.get('reason') or '실행 실패'}"
                            f" | AI: {ai_decision.get('trader_reason', '')}"
                        )
                        self._post_to_board(
                            "alert",
                            f"[{self.BOT_ID} {ai_decision['decision']} 차단] "
                            f"{result.get('message') or result.get('reason') or '실행 실패'}",
                            ai_decision.get("code", ""),
                            detail,
                        )

                log_entry = {
                    "scan": self._scan_count,
                    "time": now.isoformat(),
                    "positions": [
                        {"code": p["code"], "qty": p["qty"], "price": p["price"]}
                        for p in self._positions
                    ],
                    "action": action,
                    "detail": detail,
                    "confidence": ai_decision.get("confidence", 0) if ai_decision else 0,
                    "realized_pnl": self._realized_pnl,
                    "daily_trades": {
                        "used": self._daily_limit._count,
                        "max": self._daily_limit.max_trades,
                    },
                    "position_reviews": self._last_position_reviews,
                }
                if self._last_preflight:
                    log_entry["buy_preflight"] = self._last_preflight
                    log_entry["buy_preflight_summary"] = preflight_summary
                if self._last_ai_error:
                    log_entry["ai_error"] = self._last_ai_error
                if self._last_raw_output_preview and ai_decision is None:
                    log_entry["raw_output_preview"] = self._last_raw_output_preview
                if exit_results:
                    log_entry["exits"] = exit_results
                if result:
                    log_entry["execution"] = result

                # AI 판단 결과는 항상 기록 (HOLD도 포함), 장 외 대기는 10회당 1회
                reviewed_position = bool(self._last_position_reviews)
                preflight_checked = bool(self._last_preflight)
                if ai_decision or reviewed_position or preflight_checked or action in ("buy", "sell", "partial_sell") or self._scan_count % 10 == 0 or self._scan_count == 1:
                    write_bot_log(self.BOT_ID, log_entry)

                # 내부 trade_log 관리
                self._trade_log.append({
                    "scan": self._scan_count,
                    "time": now.strftime("%H:%M:%S"),
                    "action": action,
                    "detail": detail,
                    "result": {
                        "decision": ai_decision or {},
                        "execution": result,
                        "preflight": self._last_preflight or {},
                    },
                })
                if len(self._trade_log) > 50:
                    self._trade_log = self._trade_log[-50:]

                if skip_reason and self._last_preflight:
                    self._post_buy_preflight_if_needed()

                # DB 판단 기록 (구조화)
                indicators = {}
                confidence = 0.0
                if ai_decision:
                    confidence = ai_decision.get("confidence", 0)
                    criteria = ai_decision.get("criteria", {})
                    for key, val in criteria.items():
                        if isinstance(val, dict):
                            indicators[key] = val.get("score", 0)
                        else:
                            indicators[key] = val
                    # 추가 컨텍스트
                    if ai_decision.get("target_price"):
                        indicators["target_price"] = ai_decision["target_price"]
                    if ai_decision.get("stop_loss"):
                        indicators["stop_loss"] = ai_decision["stop_loss"]
                if result.get("sell_basis"):
                    for key, val in result["sell_basis"].items():
                        if isinstance(val, (int, float, str)):
                            indicators[f"sell_{key}"] = val

                decision_id = self._log_decision({
                    "action": action,
                    "code": ai_decision.get("code", "") if ai_decision else (exit_results[-1]["code"] if exit_results else ""),
                    "reason": detail,
                    "confidence": confidence,
                    "indicators": indicators,
                })

                # 판단↔거래 연결
                trade_id = 0
                if decision_id and action in ("buy", "sell", "partial_sell") and result and result.get("success"):
                    trade_id = self._link_decision_trade_from_result(decision_id, result)
                if trade_id and result:
                    result["trade_id"] = trade_id
                outcome_records = []
                if exit_results and not ai_decision:
                    for exit_item in exit_results:
                        exit_result = exit_item.get("result") or {}
                        outcome_records.append((
                            exit_result.get("action") or "sell",
                            exit_item.get("code", ""),
                            exit_result,
                        ))
                else:
                    outcome_records.append((
                        action,
                        ai_decision.get("code", "") if ai_decision else "",
                        {**(result or {}), "trade_id": trade_id or int((result or {}).get("trade_id", 0) or 0)},
                    ))
                for outcome_action, outcome_code, outcome_result in outcome_records:
                    self._audit_record_outcome(
                        audit,
                        action=outcome_action,
                        ai_decision=ai_decision,
                        result=outcome_result,
                        decision_id=decision_id,
                        stock_code=outcome_code,
                    )

                self._save_state()

                audit_action = action
                audit_result = result or {}
                audit_error = ""
                if skip_reason and getattr(self, "_last_preflight", None) and not self._last_preflight.get("buyable_candidates"):
                    audit_action = "blocked_buy"
                    audit_result = {**audit_result, "success": False, "reason": preflight_summary or skip_reason}
                elif skip_reason:
                    audit_action = "skip"
                    audit_result = {**audit_result, "success": True, "reason": preflight_summary or skip_reason}
                elif not self._market_guard.is_market_open() and not ai_decision and not exit_results:
                    audit_action = "skip"
                    audit_result = {**audit_result, "success": True, "reason": "장 외 시간 대기"}
                elif ai_decision is None and not exit_results and getattr(self, "_last_ai_error", ""):
                    audit_error = self._last_ai_error

                self._audit_finish_run(
                    audit,
                    action=audit_action,
                    stock_code=ai_decision.get("code", "") if ai_decision else (exit_results[-1]["code"] if exit_results else ""),
                    decision_id=decision_id,
                    result={
                        **audit_result,
                        "trade_id": trade_id or int((audit_result or {}).get("trade_id", 0) or 0),
                    },
                    error=audit_error,
                )
                audit_finished = True

                # 첫 스캔 시 오래된 판단 기록 정리
                if self._scan_count == 1:
                    self._cleanup_old_decisions()

                # 서브클래스 훅 (mirinae-2 자기 반성 등)
                self._after_scan(ai_decision)

                # AI 판단 검증 (50스캔마다)
                if self._scan_count % 50 == 0 and self._scan_count > 0:
                    self._validate_ai_performance()

            except Exception as e:
                logger.error("[%s] 스캔 오류: %s", self.BOT_ID, e, exc_info=True)
                if not audit_finished:
                    self._audit_finish_run(audit, action="failed", error=str(e))

            # 첫 스캔은 즉시 실행했으므로, 이후부터 간격 대기
            if self._scan_count == 1:
                logger.info("[%s] 첫 판단 완료, 이후 %d초 간격", self.BOT_ID, self._config.interval_seconds)
            # 장 외 시간엔 스캔 간격 조정
            if not self._market_guard.is_market_open():
                # 장 시작 근접(08:30~09:00)이면 60초 간격, 그 외 10분
                from datetime import time as _time
                now_t = kst_now().time()
                if _time(8, 30) <= now_t < _time(9, 0):
                    await asyncio.sleep(60)  # 장 시작 임박 — 1분 간격
                else:
                    await asyncio.sleep(600)  # 장 외 — 10분 간격
            else:
                await asyncio.sleep(self._config.interval_seconds)

    def _validate_ai_performance(self) -> dict:
        """AI 판단 검증 — confidence vs 실제 수익 상관분석."""
        try:
            from src.db import get_db
            outcomes = get_db().get_decision_outcomes(self.BOT_ID, days=14)
            if not outcomes:
                return {"total": 0, "buckets": {}, "recommendation": "데이터 부족"}

            buckets = {}
            for o in outcomes:
                conf = o.get("confidence", 0)
                pnl = o.get("realized_pnl", 0)

                if conf < 0.6:
                    key = "<0.6"
                elif conf < 0.7:
                    key = "0.6-0.7"
                elif conf < 0.8:
                    key = "0.7-0.8"
                elif conf < 0.9:
                    key = "0.8-0.9"
                else:
                    key = "0.9-1.0"

                if key not in buckets:
                    buckets[key] = {"count": 0, "wins": 0, "total_pnl": 0}
                buckets[key]["count"] += 1
                if pnl > 0:
                    buckets[key]["wins"] += 1
                buckets[key]["total_pnl"] += pnl

            # 추천 생성
            recommendation = ""
            for key, b in buckets.items():
                if b["count"] >= 3:
                    win_rate = b["wins"] / b["count"] * 100
                    if win_rate < 30:
                        recommendation = f"confidence {key} 구간 승률 {win_rate:.0f}% — 매수 차단 권장"
                        break

            result = {
                "total": len(outcomes),
                "buckets": {k: {
                    "count": v["count"],
                    "wins": v["wins"],
                    "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] > 0 else 0,
                    "avg_pnl": v["total_pnl"] // v["count"] if v["count"] > 0 else 0,
                } for k, v in buckets.items()},
                "recommendation": recommendation or "양호",
            }
            self._last_validation = result

            # board에 분석 결과 게시
            summary = f"AI검증: {len(outcomes)}건"
            for k, v in result["buckets"].items():
                summary += f" | {k}:{v['win_rate']}%"
            self._post_to_board("analysis", summary)

            logger.info("[%s] AI 판단 검증: %s", self.BOT_ID, result)
            return result
        except Exception as e:
            logger.warning("[%s] AI 검증 실패: %s", self.BOT_ID, e)
            return {}

    def _after_scan(self, ai_decision):
        """스캔 후 훅. 서브클래스에서 오버라이드하여 자기 반성 등 추가 로직."""
        pass

    # ===========================================
    # 상태 조회
    # ===========================================

    def status(self) -> dict:
        """현재 상태 (UI용)."""
        market = self._get_market()
        total_unrealized = 0

        positions_status = []
        for pos in self._positions:
            unrealized = 0
            current_price = pos["price"]
            try:
                info = market.get_price(pos["code"])
                if info and info.get("price"):
                    current_price = info["price"]
                    unrealized = (current_price - pos["price"]) * pos["qty"]
                    total_unrealized += unrealized
            except Exception:
                pass
            held_min = 0
            bought_at = pos.get("bought_at") or pos.get("entry_time")
            if bought_at:
                try:
                    start = datetime.fromisoformat(str(bought_at))
                    now = kst_now()
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=now.tzinfo)
                    held_min = max(0, int((now - start).total_seconds() // 60))
                except (TypeError, ValueError):
                    held_min = 0
            pnl_pct = ((current_price - pos["price"]) / pos["price"] * 100) if pos["price"] > 0 else 0

            positions_status.append({
                "code": pos["code"],
                "name": pos.get("name", ""),
                "qty": pos["qty"],
                "entry_price": pos["price"],
                "current_price": current_price,
                "unrealized": unrealized,
                "pnl_pct": round(pnl_pct, 2),
                "target_price": int(pos.get("target_price", 0) or 0),
                "stop_loss": int(pos.get("stop_loss", 0) or 0),
                "held_min": held_min,
                "bought_at": pos.get("bought_at", ""),
                "plan_updated_at": pos.get("plan_updated_at", ""),
                "plan_update_reason": pos.get("plan_update_reason", ""),
            })

        return {
            "running": self._running,
            "bot_id": self.BOT_ID,
            "scan_count": self._scan_count,
            "holding": {
                "code": self._positions[0]["code"] if self._positions else "",
                "qty": self._positions[0]["qty"] if self._positions else 0,
                "price": self._positions[0]["price"] if self._positions else 0,
            },
            "positions": positions_status,
            "positions_count": len(self._positions),
            "max_positions": self._params["max_positions"],
            "pnl": {
                "realized": self._realized_pnl,
                "unrealized": total_unrealized,
                "total": self._realized_pnl + total_unrealized,
            },
            "config": {
                "interval": self._config.interval_seconds,
                "budget": self._config.budget,
                "confidence_threshold": self._params["confidence_threshold"],
            },
            "last_analysis": self._last_analysis,
            "position_reviews": self._last_position_reviews,
            "ai_validation": self._last_validation,
            "debate_log": self._debate_log[-5:],
            "recent_trades": self._trade_log[-5:],
            "daily_trades": {
                "used": self._daily_limit._count,
                "max": self._daily_limit.max_trades,
            },
        }
