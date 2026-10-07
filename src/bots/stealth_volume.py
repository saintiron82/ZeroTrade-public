"""ETF 스텔스 볼륨 봇 -- 거래량 급증을 감지하되, 재확인 대기 후 진입.

일반 거래량 급등 봇과 달리 confirm_delay_sec 동안 대기 후
조건이 여전히 유효한 경우에만 진입하여 허위 신호를 걸러냄.
"""

import logging
from datetime import datetime

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

logger = logging.getLogger(__name__)

DEFAULT_ETF_CODES = [
    "069500", "091160", "091170", "091180", "102110", "102780",
    "102970", "114800", "117460", "117680", "117700", "122630",
    "123310", "123320", "130680", "132030", "133690", "139260", "305720",
]

# 기본 파라미터 (에볼버가 갱신 가능)
# 2026-05-15: 측정 결과 3일간 0거래 — vol_threshold=3.0이 ETF 분봉 균질성과
# 안 맞아 신호 자체가 발사 안 됨. 측정 불가 → 중간 완화로 데이터 구축 우선.
DEFAULT_PARAMS = {
    "interval_seconds": 30,
    "vol_threshold": 1.8,           # 3.0 → 1.8 (평균 대비 1.8배만 돼도 진입 후보)
    "price_up_min_pct": 0.2,        # 0.3 → 0.2 (작은 움직임도 후보)
    "confirm_delay_sec": 60,
    "trailing_stop_pct": -1.5,
    "take_profit_pct": None,
    "hard_stop_pct": -2.0,
    "max_daily_trades": 3,
    "min_hold_minutes": 60,
    # 2026-05-13: 측정 결과 max 보유 4638분(3일+) 사례가 수익원의 70%를 차지.
    # 추세추종이라 트레일링은 그대로 두되, "잊혀진 다일 보유"는 강제 청산.
    "max_hold_minutes": 480,    # 8시간 = 거의 하루 장 안에 강제 청산
    "loss_cooldown_hours": 2,
    "atr_multiplier": 2.0,
}


class StealthVolumeBot(BotBase):
    """스텔스 볼륨 봇 -- 거래량 급증 재확인 후 진입."""

    BOT_ID = "stealth-volume"

    def __init__(self, config: BotConfig | dict = None):
        # 에볼버 파라미터 로드 (기본값 위에 덮어쓰기)
        self._params = {**DEFAULT_PARAMS}
        evolver_cfg = load_bot_config("stealth-volume")
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
            if config.interval_seconds == 300:  # 기본값이면 덮어쓰기
                config.interval_seconds = self._params["interval_seconds"]

        super().__init__(config)

        if not self._config.etf_codes:
            self._config.etf_codes = DEFAULT_ETF_CODES.copy()

        # 안전장치 초기화
        self._trailing_stop = TrailingStop(
            stop_pct=self._params["trailing_stop_pct"],
            take_profit_pct=self._params["take_profit_pct"],
            hard_stop_pct=self._params["hard_stop_pct"],
        )
        self._daily_limit = DailyTradeLimit(max_trades=self._params["max_daily_trades"])
        self._min_hold = MinHoldTime(min_minutes=self._params["min_hold_minutes"])
        self._market_guard = MarketCloseGuard()

        # 보유 복원 시 트레일링 스탑 재활성화
        if self._holding_code and self._holding_price > 0:
            self._trailing_stop.activate(self._holding_price)

        # 오버나잇 판단용 진입 시각
        self._holding_entry_time: datetime | None = None

        # 손실 쿨다운: {code: last_loss_time} — 같은 종목 반복 손실 방지
        self._loss_cooldown: dict[str, datetime] = {}

        # 재확인 대기 큐: {code: {"first_seen": datetime, "signal": dict}}
        self._pending_confirm: dict[str, dict] = {}

    def _reload_params_if_needed(self):
        """100스캔마다 에볼버 설정 리로드."""
        if self._scan_count % 100 != 0:
            return
        evolver_cfg = load_bot_config("stealth-volume")
        if not evolver_cfg:
            return
        self._params.update(evolver_cfg)
        # 안전장치 파라미터 갱신
        self._trailing_stop.stop_pct = self._params["trailing_stop_pct"]
        self._trailing_stop.take_profit_pct = self._params["take_profit_pct"]
        self._trailing_stop.hard_stop_pct = self._params["hard_stop_pct"]
        self._daily_limit.max_trades = self._params["max_daily_trades"]
        self._min_hold.min_minutes = self._params["min_hold_minutes"]
        self._config.interval_seconds = self._params["interval_seconds"]
        logger.info("[%s] 파라미터 리로드 완료", self.BOT_ID)

    # ═══════════════════════════════════════════
    # scan
    # ═══════════════════════════════════════════

    def scan(self) -> list[dict]:
        """분봉 기반 거래량 급증 ETF 탐색."""
        self._reload_params_if_needed()

        # 장 운영시간 체크
        if not self._market_guard.is_market_open():
            return []

        market = self._get_market()

        # 보유 중이면 현재가 조회 -> 트레일링 스탑 업데이트
        if self._holding_code and self._holding_qty > 0:
            try:
                info = market.get_price(self._holding_code)
                if info and info.get("price"):
                    self._trailing_stop.update(info["price"])
            except Exception as e:
                logger.warning("[%s] 보유 종목 시세 조회 실패: %s", self.BOT_ID, e)

        candidates = []
        vol_threshold = self._params["vol_threshold"]
        price_up_min = self._params["price_up_min_pct"]

        # 파생ETF 토글 OFF면 인버스/레버리지를 스캔 후보에서 제외
        for code in self._filter_derivative_etfs(self._config.etf_codes):
            try:
                data = market.get_minute_price(code)
                minutes = data if isinstance(data, list) else data.get("minutes", []) if isinstance(data, dict) else []
                if not minutes or len(minutes) < 30:
                    continue

                # 최근 5분 거래량 합산
                recent_5 = minutes[:5]
                recent_vol = sum(m.get("volume", 0) for m in recent_5)

                # 직전 25분 (5~30) 평균 거래량
                baseline = minutes[5:30]
                baseline_vol = sum(m.get("volume", 0) for m in baseline)
                baseline_avg = baseline_vol / len(baseline) if baseline else 0

                if baseline_avg <= 0:
                    continue

                # 거래량 배율: 최근 5분 분당평균 / 직전 25분 분당평균
                recent_avg = recent_vol / 5
                vol_ratio = recent_avg / baseline_avg

                # 최근 5분 가격 변동률
                price_now = recent_5[0].get("close", 0) or recent_5[0].get("price", 0)
                price_5ago = recent_5[-1].get("close", 0) or recent_5[-1].get("price", 0)
                if price_5ago <= 0 or price_now <= 0:
                    continue
                price_pct = (price_now - price_5ago) / price_5ago * 100

                # 조건: 거래량 배율 충족 + 가격 상승 + 양봉 확인
                if vol_ratio >= vol_threshold and price_pct >= price_up_min:
                    # 양봉 확인 — 매수세인지 매도세인지 구분
                    latest = recent_5[0] if recent_5 else {}
                    close = latest.get("close", 0) or latest.get("price", 0)
                    open_ = latest.get("open", 0) or latest.get("price", 0)
                    if close > 0 and open_ > 0 and close <= open_:
                        continue  # 음봉 = 매도 물량 → skip
                    candidates.append({
                        "code": code,
                        "price": price_now,
                        "vol_ratio": round(vol_ratio, 2),
                        "price_pct": round(price_pct, 3),
                        "recent_vol": recent_vol,
                        "baseline_avg": int(baseline_avg),
                    })

            except Exception as e:
                logger.warning("[%s] ETF %s 스캔 실패: %s", self.BOT_ID, code, e)

        # 거래량 배율 내림차순 정렬
        candidates.sort(key=lambda c: c["vol_ratio"], reverse=True)
        return candidates

    # ═══════════════════════════════════════════
    # decide
    # ═══════════════════════════════════════════

    def decide(self, candidates: list[dict]) -> dict:
        """매매 판단: 보유 시 청산 체크, 미보유 시 재확인 후 진입."""

        now = kst_now()

        if (not self._holding_code or self._holding_qty <= 0):
            self._reconcile_with_trades()
        if self._holding_code and self._holding_price > 0 and not self._trailing_stop._active:
            self._trailing_stop.activate(self._holding_price)

        # --- 보유 중 ---
        if self._holding_code and self._holding_qty > 0:
            # 장 마감 강제 청산 (15:00 이후 진입은 오버나잇 허용)
            if self._market_guard.should_close_position(self._holding_entry_time) and not self._market_guard.was_close_attempted():
                order_type = self._market_guard.get_order_type()
                if order_type != "NONE":
                    return {
                        "action": "sell",
                        "code": self._holding_code,
                        "reason": f"장 마감 강제 청산 ({order_type})",
                        "force_close": True,
                        "order_type": order_type,
                    }

            # max_hold 시간제한 강제 청산 — "잊혀진 다일 보유" 차단.
            # entry_time이 없으면 (재시작/reconcile 직후) 현재 시각으로 초기화해
            # 다음 스캔부터 카운트 시작. 그 전 보유분이 부지불식간에 누적되어 발사.
            max_hold = self._params.get("max_hold_minutes", 0)
            if max_hold:
                if self._holding_entry_time is None:
                    self._holding_entry_time = now
                else:
                    held_min = (now - self._holding_entry_time).total_seconds() / 60
                    if held_min >= max_hold:
                        return {
                            "action": "sell",
                            "code": self._holding_code,
                            "reason": f"시간 제한 청산 ({held_min:.0f}분 >= {max_hold}분)",
                        }

            # 트레일링 스탑 체크
            try:
                info = self._get_market().get_price(self._holding_code)
                current_price = info.get("price", 0) if info else 0
            except Exception:
                current_price = 0

            if current_price > 0:
                should_exit, exit_reason = self._trailing_stop.should_exit(current_price)
                if should_exit:
                    return {
                        "action": "sell",
                        "code": self._holding_code,
                        "reason": exit_reason,
                        "current_price": current_price,
                    }

            return {
                "action": "hold",
                "code": self._holding_code,
                "reason": "보유 유지",
                "trailing_stop": {
                    "entry": self._trailing_stop.entry_price,
                    "peak": self._trailing_stop.peak_price,
                    "current": current_price,
                },
            }

        # --- 미보유 ---
        if self._params.get("position_custodian"):
            self._pending_confirm.clear()
            return {"action": "skip", "reason": "포지션 관리 전용 모드 — 신규 매수 금지"}

        # 일일 거래 제한 체크
        if not self._daily_limit.can_trade():
            self._pending_confirm.clear()
            return {"action": "skip", "reason": f"일일 거래 제한 도달 ({self._daily_limit.max_trades}회)"}

        # 점심시간 직전 진입 회피 (11:30-12:30) — 거래량 감소 + 모멘텀 약화
        from datetime import time as dtime
        cur_time = now.time()
        if dtime(11, 30) <= cur_time <= dtime(12, 30):
            self._pending_confirm.clear()
            return {"action": "skip", "reason": "점심 직전/직후 진입 회피 (11:30-12:30)"}

        # 후보 없으면 대기 큐 초기화 + skip
        if not candidates:
            self._pending_confirm.clear()
            return {"action": "skip", "reason": "거래량 급증 신호 없음"}

        # 현재 후보 코드 집합
        candidate_codes = {c["code"] for c in candidates}

        # 대기 큐에서 조건 미충족 종목 제거
        expired = [code for code in self._pending_confirm if code not in candidate_codes]
        for code in expired:
            del self._pending_confirm[code]

        # 손실 쿨다운 만료 정리
        cooldown_hours = self._params.get("loss_cooldown_hours", 2)
        for code in list(self._loss_cooldown):
            if (now - self._loss_cooldown[code]).total_seconds() > cooldown_hours * 3600:
                del self._loss_cooldown[code]

        # 대기 큐 확인: confirm_delay_sec 경과 + 여전히 조건 충족 -> buy
        confirm_delay = self._params["confirm_delay_sec"]
        for cand in candidates:
            code = cand["code"]

            # 손실 쿨다운 중인 종목 스킵
            if code in self._loss_cooldown:
                if code in self._pending_confirm:
                    del self._pending_confirm[code]
                continue

            if code in self._pending_confirm:
                pending = self._pending_confirm[code]
                elapsed = (now - pending["first_seen"]).total_seconds()
                if elapsed >= confirm_delay:
                    # ETF 시그널 점수 체크 — 시그널+추세 종합 판단
                    try:
                        from src.services.etf_signal import get_etf_signals
                        signals = get_etf_signals()
                        sig = signals.get(code, {})
                        sig_score = sig.get("score", 0)
                        alignment = sig.get("alignment", "NEUTRAL")
                        mom_score = sig.get("momentum_score", 0)

                        # 1) 종합 하락이면 매수 보류
                        if sig_score < -2:
                            del self._pending_confirm[code]
                            return {
                                "action": "skip",
                                "code": code,
                                "reason": f"시그널 하락({sig_score:+.1f}) — 매수 보류",
                            }
                        # 2) 시그널 UP인데 가격 하락 중이면 주의
                        if alignment == "DIVERGENT" and mom_score < -2:
                            del self._pending_confirm[code]
                            return {
                                "action": "skip",
                                "code": code,
                                "reason": f"시그널≠추세 불일치 (뉴스{sig.get('news_score',0):+.1f} vs 가격{mom_score:+.1f}) — 대기",
                            }
                    except Exception:
                        sig_score = 0

                    # 보드 체크: 다른 봇이 같은 종목에 부정적 의견이면 주의
                    board = self._get_board_context()
                    for op in board.get("opinions", [])[:5]:
                        if op["code"] == code and "[SELL]" in op["content"]:
                            del self._pending_confirm[code]
                            return {
                                "action": "skip",
                                "code": code,
                                "reason": f"다른 봇({op['bot_id']}) SELL 의견 — 진입 보류",
                            }

                    # 뉴스 체크: 강한 악재(-4 이하)가 있으면 진입 보류
                    news = self._get_news_context(count=3)
                    severe_bad = [n for n in news if n.get("score", 0) <= -4]
                    if severe_bad:
                        del self._pending_confirm[code]
                        return {
                            "action": "skip",
                            "code": code,
                            "reason": f"강한 악재 뉴스 감지: {severe_bad[0]['title'][:40]}",
                        }

                    # 재확인 통과 -> 매수
                    del self._pending_confirm[code]
                    overnight_note = ""
                    if cur_time >= dtime(14, 30):
                        overnight_note = " [오버나잇 후보]"
                    return {
                        "action": "buy",
                        "code": code,
                        "price": cand["price"],
                        "reason": (
                            f"재확인 통과 ({elapsed:.0f}초 경과): "
                            f"거래량 x{cand['vol_ratio']}, 가격 +{cand['price_pct']:.2f}%"
                            f"{overnight_note}"
                        ),
                        "signal": cand,
                        "vol_ratio": cand["vol_ratio"],
                    }
            else:
                # 신규 등록
                self._pending_confirm[code] = {
                    "first_seen": now,
                    "signal": cand,
                }
                logger.info(
                    "[%s] 재확인 대기 등록: %s (거래량 x%s, 가격 +%s%%)",
                    self.BOT_ID, code, cand["vol_ratio"], cand["price_pct"],
                )

        # 대기 중
        pending_info = ", ".join(
            f"{code}({(now - p['first_seen']).total_seconds():.0f}s)"
            for code, p in self._pending_confirm.items()
        )
        return {"action": "wait", "reason": f"재확인 대기 중: {pending_info}"}

    # ═══════════════════════════════════════════
    # execute
    # ═══════════════════════════════════════════

    def execute(self, decision: dict) -> dict:
        """매매 실행."""
        action = decision.get("action", "")
        code = decision.get("code", "")
        now = kst_now()

        if action == "buy":
            if self._params.get("position_custodian"):
                return {"success": False, "reason": "포지션 관리 전용 모드 — 신규 매수 금지"}

            price = decision.get("price", 0)
            if price <= 0:
                return {"success": False, "reason": "가격 정보 없음"}

            qty = self.get_buyable_qty(code, price)
            if qty <= 0:
                return {"success": False, "reason": "매수 가능 수량 없음"}

            result = self.buy(code, qty, price)
            if result.get("success"):
                self._holding_entry_time = kst_now()
                self._trailing_stop.activate(price)
                # ATR 기반 동적 트레일링
                try:
                    candles = self._get_market().get_minute_price(code)
                    if candles and len(candles) > 15:
                        from src.bots.safeguards import calc_atr
                        atr = calc_atr(candles, 14)
                        if atr > 0:
                            self._trailing_stop.atr_pct = atr
                            self._trailing_stop.atr_multiplier = self._params.get("atr_multiplier", 2.0)
                except Exception:
                    pass
                self._min_hold.mark_entry()
                self._daily_limit.record_trade()
                logger.info(
                    "[%s] 매수 완료: %s %d주 @%d원",
                    self.BOT_ID, code, qty, price,
                )
            return result

        if action == "sell":
            force_close = decision.get("force_close", False)

            # 최소 보유 시간 체크 (장 마감 강제 청산은 무시)
            if not force_close and not self._min_hold.can_sell():
                remaining = self._min_hold.remaining_minutes()
                logger.info(
                    "[%s] 최소 보유 시간 미달 (%.1f분 남음), 매도 보류",
                    self.BOT_ID, remaining,
                )
                return {"success": False, "reason": f"최소 보유 시간 미달 ({remaining:.1f}분 남음)"}

            # 장 마감 시 시간외 종가 주문으로 전환
            from src.kis.order import OrderType
            order_type_str = decision.get("order_type", "MARKET")
            order_type = OrderType.AFTER_HOURS_CLOSE if order_type_str == "AFTER_HOURS" else OrderType.MARKET
            result = self.sell(order_type=order_type)

            if result.get("success"):
                # 손실 시 쿨다운 기록 — 같은 종목 반복 진입 방지
                if result.get("realized_pnl", 0) < 0:
                    self._loss_cooldown[code] = kst_now()
                self._holding_entry_time = None
                self._trailing_stop.deactivate()
                self._min_hold.clear()
                if force_close:
                    self._market_guard.mark_close_attempted()
                logger.info(
                    "[%s] 매도 완료: %s (%s)",
                    self.BOT_ID, code, decision.get("reason", ""),
                )
            elif force_close:
                # 장 마감 매도 실패 — 무한 재시도 방지
                self._market_guard.mark_close_attempted()
                logger.warning(
                    "[%s] 장 마감 매도 실패: %s — 재시도 중단 (다음 장에서 처리)",
                    self.BOT_ID, result.get("message", ""),
                )
            return result

        return {"success": True, "action": action, "reason": decision.get("reason", "")}

    # ── 판단 감사 (decision audit) ──

    def _audit_record_candidates(self, recorder, candidates: list[dict]):
        """스캔 결과 상위 신호를 증거로 기록."""
        if recorder is None:
            return
        try:
            top = candidates[:5]
            recorder.evidence(
                "signals",
                payload={"candidates": top, "count": len(candidates)},
                summary=", ".join(
                    f"{c.get('code', '')} x{c.get('vol_ratio', 0):.1f}"
                    for c in top
                ) or "신호 없음",
            )
        except Exception as exc:
            logger.warning("[%s] audit candidates failed: %s", self.BOT_ID, exc)

    def _audit_finish_scan(self, recorder, decision: dict,
                           decision_id: int, trade_id: int,
                           trade_success: bool,
                           block_reason: str = ""):
        """스캔 종료 상태를 audit run에 기록.

        block_reason: 주문 실패 시 실제 차단 사유 (execute 결과 메시지).
        비어 있으면 decision reason으로 폴백.
        """
        if recorder is None:
            return
        try:
            action = decision.get("action", "")
            if action in ("buy", "sell") and trade_success:
                status = "ordered"
            elif action in ("buy", "sell"):
                # 가드/사유에 의한 미체결 = blocked (mirinae와 동일 의미론)
                status = "blocked"
                # 주문 미체결 사유를 preflight 행으로 보존 (차단 사유 통계용)
                recorder.preflight(
                    "execution",
                    passed=False,
                    severity="block",
                    message=block_reason or decision.get("reason", ""),
                    stock_code=decision.get("code", ""),
                )
            else:
                status = "completed"
            recorder.finish(
                status=status,
                action=action,
                stock_code=decision.get("code", ""),
                decision_id=decision_id or 0,
                trade_id=trade_id or 0,
                error=decision.get("error", ""),
            )
        except Exception as exc:
            logger.warning("[%s] audit finish failed: %s", self.BOT_ID, exc)

    # ═══════════════════════════════════════════
    # 폴링 루프 오버라이드 (로그 기록 추가)
    # ═══════════════════════════════════════════

    async def _loop(self):
        """메인 루프: scan -> decide -> execute + 로그 기록."""
        import asyncio

        logger.info(
            "[%s] 시작 -- %d개 ETF, %ds 간격, 거래량 x%.1f, 확인대기 %ds",
            self.BOT_ID,
            len(self._config.etf_codes),
            self._config.interval_seconds,
            self._params["vol_threshold"],
            self._params["confirm_delay_sec"],
        )

        while self._running:
            try:
                # 주말 체크 — 토/일은 스캔하지 않음
                if kst_now().weekday() >= 5:  # 5=토, 6=일
                    await asyncio.sleep(300)  # 5분 대기 후 재확인
                    continue

                self._scan_count += 1
                now = kst_now()
                audit = self._audit_start_run()

                # 1. 스캔
                candidates = self.scan()
                self._audit_record_candidates(audit, candidates)

                # 2. 판단
                decision = self.decide(candidates)

                # 3. 실행
                result = {}
                if decision.get("action") in ("buy", "sell"):
                    result = self.execute(decision)
                    self._publish_event("bot_trade", {
                        "action": decision["action"],
                        "code": decision.get("code", ""),
                        "result": result,
                    })

                # 4. 로그 기록
                action = decision.get("action", "")
                log_entry = {
                    "scan": self._scan_count,
                    "time": now.isoformat(),
                    "holding": self._holding_code or None,
                    "holding_qty": self._holding_qty,
                    "signals": [
                        {"code": c["code"], "vol_ratio": c["vol_ratio"], "price_pct": c["price_pct"]}
                        for c in candidates[:5]
                    ],
                    "action": action,
                    "detail": decision.get("reason", ""),
                    "trailing_stop": {
                        "entry": self._trailing_stop.entry_price,
                        "peak": self._trailing_stop.peak_price,
                        "current": decision.get("current_price", 0),
                    },
                    "daily_trades": {
                        "used": self._daily_limit._count,
                        "max": self._daily_limit.max_trades,
                    },
                }

                # 매 스캔 로그 기록 (skip/hold는 10회당 1회)
                if action in ("buy", "sell", "wait") or self._scan_count % 10 == 0:
                    write_bot_log("stealth-volume", log_entry)

                # 5. DB 판단 기록 (구조화)
                indicators = {}
                if candidates:
                    top = candidates[0]
                    indicators = {
                        "vol_ratio": top.get("vol_ratio", 0),
                        "price_pct": top.get("price_pct", 0),
                        "trailing_entry": self._trailing_stop.entry_price,
                        "trailing_peak": self._trailing_stop.peak_price,
                    }
                decision_id = self._log_decision({
                    "action": action,
                    "code": decision.get("code", ""),
                    "reason": decision.get("reason", ""),
                    "confidence": decision.get("vol_ratio", 0) / 10 if action == "buy" else 0,
                    "indicators": indicators,
                    "trade_id": result.get("trade_id", 0) if result else 0,
                })

                # 판단↔거래 연결
                linked_trade_id = 0
                if decision_id and result and result.get("success"):
                    try:
                        from src.db import get_db
                        # 최근 이 봇의 거래를 찾아서 연결
                        recent = get_db().get_trades(source=self.BOT_ID, limit=1)
                        if recent:
                            linked_trade_id = recent[0]["id"]
                            get_db().link_decision_trade(decision_id, linked_trade_id)
                    except Exception:
                        pass

                # 판단 감사 종료 기록
                # 실패 사유: execute() 자체 차단은 "reason", 엔진 차단은 "message"
                exec_failed = bool(result) and not result.get("success")
                block_reason = (
                    (result.get("reason") or result.get("message", ""))
                    if exec_failed else ""
                )
                self._audit_finish_scan(
                    audit, decision,
                    decision_id=decision_id or 0,
                    trade_id=linked_trade_id,
                    trade_success=bool(result and result.get("success")),
                    block_reason=block_reason,
                )

                # 내부 trade_log 관리
                self._trade_log.append({
                    "scan": self._scan_count,
                    "time": now.strftime("%H:%M:%S"),
                    "action": action,
                    "detail": decision.get("reason", ""),
                    "result": result or decision,
                })
                if len(self._trade_log) > 50:
                    self._trade_log = self._trade_log[-50:]
                self._save_state()

            except Exception as e:
                logger.error("[%s] 스캔 오류: %s", self.BOT_ID, e, exc_info=True)

            # 장 외 시간엔 스캔 간격 크게 늘림 (API 낭비 방지)
            if not self._market_guard.is_market_open():
                await asyncio.sleep(max(self._config.interval_seconds * 10, 300))
            else:
                await asyncio.sleep(self._config.interval_seconds)
