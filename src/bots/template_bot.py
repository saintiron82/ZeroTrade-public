"""TemplateBot — JSON 스펙 기반 범용 봇.

코드 생성 없이 조건 스펙(JSON)만으로 동작하는 봇.
BotBase를 상속하며, ConditionEngine으로 진입/청산을 평가.
"""

import json
import logging
from pathlib import Path

from src.bots.base import BotBase, BotConfig
from src.bots.condition_engine import evaluate_conditions, evaluate_exit, calc_indicator
from src.bots.safeguards import (
    TrailingStop, DailyTradeLimit, MinHoldTime, MarketCloseGuard,
    write_bot_log,
)

logger = logging.getLogger(__name__)

DEFAULT_ETF_CODES = [
    "069500", "091160", "091170", "091180", "102110", "102780",
    "102970", "114800", "117460", "117680", "117700", "122630",
    "123310", "123320", "130680", "132030", "133690", "139260", "305720",
]


class TemplateBot(BotBase):
    """조건 스펙 기반 범용 봇. JSON 스펙으로 scan/decide/execute를 수행."""

    def __init__(self, spec: dict, config: BotConfig | dict = None):
        # BOT_ID를 먼저 설정 (BotBase.__init__에서 사용)
        self.BOT_ID = spec.get("bot_id", "template-bot")
        self._spec = spec

        # config 설정
        if config is None:
            config = BotConfig(
                interval_seconds=spec.get("interval_seconds", 60),
                budget=0,
                etf_codes=spec.get("target_codes", DEFAULT_ETF_CODES),
            )
        super().__init__(config)

        if not self._config.etf_codes:
            self._config.etf_codes = spec.get("target_codes", DEFAULT_ETF_CODES)

        # 제한 규칙 (safeguards)
        limits = spec.get("limits", {})
        exit_spec = spec.get("exit", {})
        self._trailing = TrailingStop(
            stop_pct=exit_spec.get("trailing_stop_pct", -0.8),
            take_profit_pct=exit_spec.get("take_profit_pct", 1.5),
            hard_stop_pct=exit_spec.get("hard_stop_pct", -2.0),
        )
        self._daily_limit = DailyTradeLimit(max_trades=limits.get("max_daily_trades", 3))
        self._min_hold = MinHoldTime(min_minutes=limits.get("min_hold_minutes", 15))
        self._market_guard = MarketCloseGuard()
        self._max_positions = limits.get("max_positions", 1)

        logger.info("[%s] 템플릿 봇 초기화 (조건 %d개, 대상 %d종목)",
                    self.BOT_ID,
                    len(spec.get("entry", {}).get("conditions", [])),
                    len(self._config.etf_codes))

    def scan(self) -> list[dict]:
        """대상 종목 분봉 조회 → 지표 계산 → 진입 조건 충족 종목 반환."""
        market = self._get_market()
        candidates = []

        for code in self._config.etf_codes:
            try:
                minutes = market.get_minute_price(code)
                if not minutes or len(minutes) < 5:
                    continue

                # 진입 조건 평가
                entry_spec = self._spec.get("entry", {})
                conditions = entry_spec.get("conditions", [])
                logic = entry_spec.get("logic", "AND")

                met, reasons = evaluate_conditions(conditions, logic, minutes)
                if met:
                    current = minutes[-1]
                    price = current.get("close", current.get("stck_prpr", 0))
                    candidates.append({
                        "code": code,
                        "price": price,
                        "reasons": reasons,
                        "score": len(reasons),
                    })
            except Exception as e:
                logger.debug("[%s] 스캔 오류 (%s): %s", self.BOT_ID, code, e)

        return candidates

    def decide(self, candidates: list[dict]) -> dict:
        """진입/청산 판단."""

        # ── 장 마감 강제 청산 ──
        if self._spec.get("limits", {}).get("market_close_force", True):
            if self._market_guard.should_force_close() and self._holding_code:
                return {
                    "action": "sell",
                    "code": self._holding_code,
                    "reason": "장 마감 강제 청산",
                    "detail": "장 마감 강제 청산",
                }

        # ── 보유 중 → 청산 조건 확인 ──
        if self._holding_code and self._holding_qty > 0:
            try:
                market = self._get_market()
                info = market.get_price(self._holding_code)
                current_price = info.get("price", 0) if info else 0

                if current_price > 0:
                    # 트레일링 업데이트
                    self._trailing.update(current_price)

                    # 최소 보유 시간 확인
                    if not self._min_hold.can_sell():
                        return {
                            "action": "hold",
                            "detail": f"최소 보유 시간 대기 ({self._min_hold.remaining_minutes():.0f}분 남음)",
                        }

                    # 트레일링/익절/손절 체크
                    should_exit, reason = self._trailing.should_exit(current_price)
                    if should_exit:
                        return {"action": "sell", "code": self._holding_code,
                                "reason": reason, "detail": reason}

                    # 스펙 기반 청산 조건
                    minutes = market.get_minute_price(self._holding_code)
                    if minutes:
                        exit_spec = self._spec.get("exit", {})
                        position = {"price": self._holding_price, "peak_price": self._trailing._peak or self._holding_price}
                        should_exit, reason = evaluate_exit(exit_spec, position, current_price, minutes)
                        if should_exit:
                            return {"action": "sell", "code": self._holding_code,
                                    "reason": reason, "detail": reason}
            except Exception as e:
                logger.warning("[%s] 청산 판단 오류: %s", self.BOT_ID, e)

            return {"action": "hold", "detail": "보유 유지"}

        # ── 매수 판단 ──
        if not self._daily_limit.can_trade():
            return {"action": "skip", "detail": f"일일 거래 제한 ({self._daily_limit.remaining()}건 남음)"}

        if not self._market_guard.is_tradeable():
            return {"action": "skip", "detail": "거래 시간 외"}

        if not candidates:
            return {"action": "skip", "detail": "진입 조건 충족 종목 없음"}

        # 점수 높은 종목 선택
        best = max(candidates, key=lambda c: c.get("score", 0))
        reasons_str = " + ".join(best.get("reasons", []))
        return {
            "action": "buy",
            "code": best["code"],
            "price": best["price"],
            "reason": reasons_str,
            "detail": f"진입: {reasons_str}",
        }

    def execute(self, decision: dict) -> dict:
        """주문 실행."""
        action = decision.get("action")
        code = decision.get("code", "")

        if action == "buy" and code:
            price = decision.get("price", 0)
            qty = self.get_buyable_qty(code, price) if price > 0 else 0
            if qty <= 0:
                return {"success": False, "reason": "매수 가능 수량 0"}

            result = self.buy(code, qty, price)
            if result.get("success"):
                self._trailing.activate(price)
                self._min_hold.mark_entry()
                self._daily_limit.record_trade()
                write_bot_log(self.BOT_ID, {
                    "action": "buy", "code": code, "qty": qty, "price": price,
                    "reason": decision.get("reason", ""),
                })
            return result

        elif action == "sell" and code:
            result = self.sell(code)
            if result.get("success"):
                self._trailing.deactivate()
                self._min_hold.clear()
                self._daily_limit.record_trade()
                write_bot_log(self.BOT_ID, {
                    "action": "sell", "code": code,
                    "reason": decision.get("reason", ""),
                })
            return result

        return {"success": False, "reason": "실행 불가"}

    def status(self) -> dict:
        """현재 상태 + 스펙 정보."""
        base = super().status()
        base["spec"] = {
            "entry_conditions": len(self._spec.get("entry", {}).get("conditions", [])),
            "exit_conditions": len(self._spec.get("exit", {}).get("conditions", [])),
            "category": self._spec.get("category", ""),
        }
        return base


# ══════════════════════════════════════════
# 스펙 로드 유틸
# ══════════════════════════════════════════

SPEC_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "bot_specs"


def load_spec(bot_id: str) -> dict | None:
    """data/bot_specs/{bot_id}.json에서 스펙 로드."""
    spec_file = SPEC_DIR / f"{bot_id}.json"
    if not spec_file.exists():
        return None
    try:
        return json.loads(spec_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("스펙 로드 실패 (%s): %s", bot_id, e)
        return None


def save_spec(spec: dict) -> str:
    """스펙 저장. bot_id 반환."""
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    bot_id = spec.get("bot_id", "unnamed")
    spec_file = SPEC_DIR / f"{bot_id}.json"
    spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("봇 스펙 저장: %s", spec_file)
    return bot_id


def list_specs() -> list[dict]:
    """저장된 모든 스펙 목록."""
    if not SPEC_DIR.exists():
        return []
    specs = []
    for f in SPEC_DIR.glob("*.json"):
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
            specs.append({
                "bot_id": spec.get("bot_id", f.stem),
                "name": spec.get("name", ""),
                "description": spec.get("description", ""),
                "category": spec.get("category", ""),
            })
        except Exception:
            pass
    return specs
