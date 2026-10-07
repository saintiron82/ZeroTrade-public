"""미리내 2호 — AI 멀티에이전트 단타 트레이딩 봇.

미리내 1호(장기 관점)를 상속하되, 단타(스캘핑/데이트레이딩)에 특화된 파라미터와 프롬프트 사용.
10분 간격, confidence 0.6, 최대 2시간 보유, 당일 청산 원칙.
자기 거래 이력 기반 정책 자동 업데이트 기능 포함.
"""

import json
import logging
from datetime import datetime, time
from pathlib import Path

from src.bots.mirinae import MirinaeBot, DEFAULT_PARAMS as BASE_PARAMS
from src.bots.base import BotConfig
from src.bots.safeguards import kst_now, load_bot_config, save_bot_config, read_bot_logs
from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# 단타 특화 파라미터 — fee-aware (round-trip 0.13% for ETF) + 추세 회복 대기 허용
# 2026-05-13: 2%/2.5% 큰 목표 → 0.5%/-0.4%로 축소. 작은 변동 다수 포착.
# 2026-05-15: 11거래 측정 결과 손익비 1.30→2.01 개선. "추가 이익 기대 시 보유 연장",
# "추세 회복 기대로 손실 만회" 허용 방향으로 미세조정 — 윈도우를 약간 더 넓혀
# AI 판단(매 10분)이 trailing/hard_stop의 기계적 발사보다 우선이 되도록.
SCALP_PARAMS = {
    **BASE_PARAMS,
    "interval_seconds": 600,       # 10분
    "confidence_threshold": 0.58,  # 적극형 단타: 완벽한 자리보다 양수 기대값 우선
    "max_positions": 2,            # 동시 2종목
    "trailing_stop_pct": -0.35,    # 0.25 → 0.35 (노이즈 stop-out 완화)
    "take_profit_pct": 0.8,        # 0.5 → 0.8 (추세 지속 시 더 큰 익절 허용)
    "hard_stop_pct": -0.6,         # -0.4 → -0.6 (추세 회복 가능성 인정, 수습 윈도우)
    "max_daily_trades": 10,        # 하루 10회
    "min_hold_minutes": 3,         # 최소 3분 보유
    "max_hold_minutes": 60,        # 30 → 60 (AI HOLD 신호 시 1시간까지 유지)
    "no_entry_after": "15:10",     # 시장 세션상 신규 매수 금지 기준
    "codex_timeout": 120,          # AI 2분 타임아웃 (빠른 판단)
    "loss_cooldown_hours": 2,      # 손절 후 2시간 재진입 쿨다운
}


class Mirinae2Bot(MirinaeBot):
    """미리내 2호 — 단타 특화 AI 멀티에이전트 트레이딩 봇."""

    BOT_ID = "mirinae-2"

    def __init__(self, config: BotConfig | dict = None):
        # 단타 파라미터로 오버라이드
        self._params = {**SCALP_PARAMS}
        evolver_cfg = load_bot_config("mirinae-2")
        self._params.update(evolver_cfg)

        # 가드 설정 (BotBase.__init__ 전에 설정해야 TradeEngine에 전달됨)
        # 봇 독립 4원칙: 거래내역 기준 관리. 봇간 종목/섹터 교차 차단은 OFF.
        self._guard_config = {
            "daily_loss_limit_pct": -2,
            "market_drop_block_pct": -1.0,
            "trend_filter_enabled": True,
            "duplicate_check_enabled": False,
            "max_sector_exposure": 0,
        }

        if isinstance(config, dict):
            config.setdefault("interval_seconds", self._params["interval_seconds"])
        elif config is None:
            config = BotConfig(interval_seconds=self._params["interval_seconds"])
        else:
            if config.interval_seconds in (300, 1800):
                config.interval_seconds = self._params["interval_seconds"]

        # 부모의 __init__을 우회하여 BotBase.__init__ 직접 호출
        from src.bots.base import BotBase
        from src.bots.mirinae import DEFAULT_ETF_CODES
        BotBase.__init__(self, config)

        # ETF 코드 초기화
        if not self._config.etf_codes:
            self._config.etf_codes = DEFAULT_ETF_CODES.copy()

        # 미리내 공통 초기화
        from src.bots.safeguards import (
            TrailingStop, DailyTradeLimit, MinHoldTime, MarketCloseGuard,
        )
        self._positions = []
        self._debate_log = []
        self._last_analysis = None
        self._latest_signals = []
        self._last_validation: dict = {}
        self._last_preflight: dict = {}
        self._last_position_reviews: list[dict] = []
        self._last_ai_error = ""
        self._last_raw_output_preview = ""
        self._last_preflight_board_key = ""

        self._daily_limit = DailyTradeLimit(max_trades=self._params["max_daily_trades"])
        self._market_guard = MarketCloseGuard()

        # 손절 쿨다운 추적 (연속 손절 차단은 부모 MirinaeBot이 관리)
        self._last_loss_time: dict[str, float] = {}  # code → timestamp

        self._restore_positions()

        logger.info(
            "[%s] 초기화 — 단타모드, %ds 간격, confidence >= %.1f, 최대 %d 포지션, 최대 보유 %d분",
            self.BOT_ID,
            self._params["interval_seconds"],
            self._params["confidence_threshold"],
            self._params["max_positions"],
            self._params["max_hold_minutes"],
        )

    def _build_codex_prompt(self, context: str) -> str:
        """단타 특화 프롬프트."""
        max_hold = self._params.get("max_hold_minutes", 120)
        session = getattr(self, "_market_session", {})
        session_label = session.get("label", "?")
        can_buy = "가능" if session.get("can_buy", False) else "불가"
        can_sell = "가능" if session.get("can_sell", False) else "불가"
        mtc = session.get("minutes_to_close", 0)
        rec = session.get("recommended_action", "")

        prompt = f"""당신은 **월스트리트 최고 레벨의 적극형 데이트레이더**입니다.
아래는 Python이 KIS API에서 실시간 수집한 데이터입니다.

**중요: 시간 규칙은 아래 [시장 세션]이 최종 기준입니다. 룰 파일의 시간 규칙보다 이것을 우선하세요.**

[시장 세션] ★ 최종 기준
  현재: {session_label}
  매수: {can_buy} | 매도: {can_sell}
  마감까지: {mtc:.0f}분
  권장: {rec}
  → 매수 "{can_buy}"이면 신규 진입 가능, "불가"이면 진입 금지

{context}

## 단타 특화 규칙
- **보유 시간 제한**: 매수 후 최대 {max_hold}분 이내 청산
- **고점 추격 재정의**: 3% 이상 상승은 자동 금지가 아닙니다. 거래량 확장, 추세 정렬, 손절폭 대비 목표수익이 모두 맞으면 돌파 추종을 허용합니다.
- **실행 기준**: 독립 근거 2개 이상(가격/거래량/뉴스/수급/시장세션)과 손익비 1.3:1 이상이면 적극적으로 BUY를 검토합니다.
- **후보 선택**: 1순위 ETF가 예산 초과이면 즉시 예산 내 차선 후보를 평가합니다. 예산 초과만으로 HOLD하지 마세요.
- **당일 청산**: 오버나이트 보유 절대 금지

## 3단계 멀티 에이전트 분석:

### 1단계: 강세 분석가 (단타 관점)
- 거래량 급증 + 가격 상승 동반 종목 탐색
- 분봉 신고가 돌파, VWAP 상향 돌파 확인
- 뉴스 호재 + 수급 유입 신호
- **짧은 시간 안에 0.8~1.8% 수익 가능한 실행 가능한 종목을 순위화**

### 2단계: 약세 분석가 (리스크 관점)
- 상승폭이 손절폭 대비 보상받는 추세인지?
- 거래량 감소 추세? (모멘텀 소진)
- 장 마감까지 남은 시간 대비 리스크
- 외인/기관 매도세?

### 3단계: 트레이더 (최종 결정)
- confidence 0.58 이상이면 BUY/SELL을 우선 검토
- 진입가, 목표가(+0.8~1.8%), 손절가(-0.8~-1.3%) 명시
- 보유 종목이 있으면 신규 후보보다 보유 종목의 SELL/HOLD/추매 판단을 먼저 끝내세요.
- 매 스캔마다 기존 target_price/stop_loss가 현재가, 추세, 잔여시간 기준으로 아직 유효한지 검증하세요.
- [보유 평가 이벤트]가 review_loss, review_plan, review_target 또는 sell이면 신규 종목 BUY를 선택하지 마세요. 해당 보유 종목을 손절/부분매도/목표수정/HOLD 중 하나로 먼저 결론내야 합니다.
- 손실 중인 보유 종목을 HOLD하려면 반등 근거를 가격·거래량·뉴스로 설명하고 target_price와 stop_loss를 다시 제시하세요. 근거가 약하면 SELL 또는 부분매도를 선택하세요.
- review_plan인 보유 종목은 기존 계획이 틀렸다는 신호입니다. HOLD하려면 target_action을 raise/lower 중 하나로 정하고 새 target_price와 stop_loss를 반드시 제시하세요. 기존 목표/손절 그대로 keep하지 마세요.
- target_action=keep은 계획검증이 ok일 때만 허용됩니다. keep이어도 target_price와 stop_loss 값을 반드시 다시 출력하세요.
- HOLD는 "거래하지 않음"이 아니라 마지막 선택입니다. 실행 가능한 후보가 있고 기대값이 양수이면 작은 포지션이라도 BUY를 선택하세요.
- 이미 보유 중인 종목의 BUY는 추매입니다. 보유 중이라는 이유만으로 BUY를 금지하지 마세요.
- SELL은 전량 청산뿐 아니라 분할 매도도 가능합니다. 일부만 줄일 때는 sell_qty 또는 position_pct를 명시하세요.
- 보유 종목이 목표가에 도달했더라도 뉴스/거래량/추세가 계속 강하면 HOLD와 함께 target_price를 상향하고 stop_loss를 이익 보호선으로 올리세요.
- 모멘텀이 약해졌지만 즉시 매도할 정도가 아니면 HOLD와 함께 target_price를 낮추고 stop_loss를 조정하세요.
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
    "market_trend": {{"score": 0, "detail": "시장 전체 방향 (0=하락 5=중립 10=상승)"}},
    "volume_signal": {{"score": 0, "detail": "거래량 신호 (0=감소 10=폭발)"}},
    "news_sentiment": {{"score": 0, "detail": "뉴스 감성 (0=악재 5=중립 10=호재)"}},
    "time_risk": {{"score": 0, "detail": "시간 리스크 (0=마감임박 10=여유)"}},
    "entry_price": {{"score": 0, "detail": "진입 가격 적정성 (0=고점 10=저점)"}}
  }},
  "bull_summary": "강세 논거 (한국어)",
  "bear_summary": "약세 논거 (한국어)",
  "trader_reason": "최종 판단 (한국어)"
}}
```"""
        return prompt

    def _check_positions(self) -> list[dict]:
        """포지션 체크 — 부모 + 시간 제한 청산 추가."""
        exits = super()._check_positions()

        # 최대 보유 시간 초과 체크
        max_hold = self._params.get("max_hold_minutes", 120)
        now = kst_now()
        from src.bots.safeguards import get_market_session
        session = get_market_session()
        can_sell = bool(session.get("can_sell"))
        session_label = session.get("label", "?")
        for pos in list(self._positions):
            bought_str = pos.get("bought_at", "")
            if not bought_str:
                continue
            try:
                bought_at = datetime.fromisoformat(bought_str)
                held_minutes = (now - bought_at).total_seconds() / 60
                if held_minutes >= max_hold:
                    pnl_pct = next(
                        (
                            r.get("pnl_pct")
                            for r in self._last_position_reviews
                            if r.get("code") == pos["code"]
                            and isinstance(r.get("pnl_pct"), (int, float))
                        ),
                        None,
                    )
                    reason = self._format_exit_label(
                        "time_limit", pnl_pct,
                        held_minutes=held_minutes, max_hold=max_hold,
                    )
                    decision = "sell"
                    if can_sell:
                        exits.append({
                            "action": "sell",
                            "code": pos["code"],
                            "qty": pos["qty"],
                            "reason": reason,
                        })
                    else:
                        decision = "blocked_sell"
                        reason = f"매도 불가 세션({session_label}) - {reason}"
                    self._last_position_reviews.append({
                        "code": pos["code"],
                        "name": pos.get("name", ""),
                        "qty": pos.get("qty", 0),
                        "decision": decision,
                        "reason": reason,
                    })
            except (ValueError, TypeError):
                pass

        # 진입 금지: 시장 세션 기반 (can_buy=False면 진입 금지)
        from src.bots.safeguards import get_market_session
        session = get_market_session()
        self._no_entry = not session.get("can_buy", False)
        self._market_session = session  # AI 프롬프트에서도 사용

        return exits

    def _execute_position_exit(self, exit_action: dict) -> dict:
        """포지션 청산 — 손절 쿨다운 추적 추가 (연속 손실 카운트는 부모가 관리)."""
        result = super()._execute_position_exit(exit_action)
        if result.get("success"):
            code = exit_action.get("code", "")
            pnl = result.get("realized_pnl", 0)
            if pnl < 0:
                import time
                self._last_loss_time[code] = time.time()
                logger.info("[%s] %s 손실 확정. 쿨다운 시작", self.BOT_ID, code)
        return result

    def _execute_decision(self, decision: dict) -> dict:
        """실행 — 진입 금지 시간 + 손절 쿨다운 차단.

        연속 손절 차단(consecutive_loss_block)·블록리스트는 부모가 처리.
        """
        if decision.get("decision") == "BUY":
            if getattr(self, "_no_entry", False):
                logger.info(
                    "[%s] 시장 세션상 신규 매수 불가 — BUY 차단",
                    self.BOT_ID,
                )
                return {"success": False, "reason": "시장 세션상 신규 매수 불가"}

            code = decision.get("code", "")

            # 쿨다운 검사
            import time
            cooldown_hours = self._params.get("loss_cooldown_hours", 2)
            if code in self._last_loss_time:
                elapsed = (time.time() - self._last_loss_time[code]) / 3600
                if elapsed < cooldown_hours:
                    logger.warning(
                        "[%s] %s 뇌동매매 차단 — 손절 후 %.1fh 경과 (쿨다운 %dh)",
                        self.BOT_ID, code, elapsed, cooldown_hours,
                    )
                    return {"success": False, "reason": f"손절 쿨다운 ({cooldown_hours}h)"}

        return super()._execute_decision(decision)

    # ===========================================
    # 자기 반성 (Self-Review)
    # ===========================================

    _REVIEW_INTERVAL = 5  # 5회 판단마다 자기 반성

    def _after_scan(self, ai_decision):
        """스캔 후 훅 — 자기 반성 실행."""
        if self._should_self_review():
            try:
                logger.info("[%s] 자기 반성 시작 (scan %d)", self.BOT_ID, self._scan_count)
                self._self_review()
            except Exception as e:
                logger.warning("[%s] 자기 반성 실패: %s", self.BOT_ID, e)

    def _should_self_review(self) -> bool:
        """자기 반성 시점인지."""
        return self._scan_count > 0 and self._scan_count % self._REVIEW_INTERVAL == 0

    def _self_review(self):
        """자기 판단-결과 이력을 DB에서 분석하고 정책을 업데이트."""
        from src.db import get_db
        db = get_db()

        # 1. 구조화된 판단→결과 데이터 조회
        outcomes = db.get_decision_outcomes(self.BOT_ID, days=7)

        # 2. 지표별 승률 분석
        indicator_stats = {}
        for key in ("market_trend", "volume_signal", "news_sentiment",
                     "time_risk", "entry_price"):
            stats = db.get_indicator_stats(self.BOT_ID, key, days=14)
            if stats:
                indicator_stats[key] = stats

        # 3. confidence 구간별 승률
        conf_buckets: dict[str, list] = {"low(<0.65)": [], "mid(0.65-0.75)": [], "high(>0.75)": []}
        for o in outcomes:
            c = o.get("confidence", 0)
            pnl = o.get("realized_pnl")
            if pnl is None:
                continue
            if c < 0.65:
                conf_buckets["low(<0.65)"].append(pnl)
            elif c < 0.75:
                conf_buckets["mid(0.65-0.75)"].append(pnl)
            else:
                conf_buckets["high(>0.75)"].append(pnl)

        conf_summary = {}
        for rng, pnls in conf_buckets.items():
            if pnls:
                wins = sum(1 for p in pnls if p > 0)
                conf_summary[rng] = {
                    "total": len(pnls), "wins": wins,
                    "win_rate": wins / len(pnls),
                    "avg_pnl": sum(pnls) / len(pnls),
                }

        # 4. JSONL 로그도 보조 참고 (최근 3일)
        from datetime import timedelta
        logs = []
        for i in range(3):
            d = (kst_now().date() - timedelta(days=i)).isoformat()
            daily = read_bot_logs("mirinae-2", d)
            logs.extend(daily)

        buys = [l for l in logs if l.get("action") == "buy"]
        sells = [l for l in logs if l.get("action") in ("sell", "sell_all")]
        holds = [l for l in logs if l.get("action") == "hold"]

        if len(outcomes) < 2 and len(buys) + len(sells) < 2:
            return  # 데이터 부족

        # 5. AI에게 구조화된 데이터로 자기 반성 요청
        review_prompt = self._build_review_prompt_v2(
            outcomes, indicator_stats, conf_summary,
            logs, buys, sells, holds,
        )

        from src.bots.ai_caller import invoke_ai
        output = invoke_ai(review_prompt, self._params, timeout=120, bot_id=f"{self.BOT_ID}-review")

        if not output:
            return

        self._apply_review(output)

    def _build_review_prompt(self, logs, buys, sells, holds, total_trades, hold_ratio) -> str:
        """레거시 자기 반성 프롬프트 (하위 호환)."""
        return self._build_review_prompt_v2([], {}, {}, logs, buys, sells, holds)

    def _build_review_prompt_v2(self, outcomes, indicator_stats, conf_summary,
                                 logs, buys, sells, holds) -> str:
        """구조화 데이터 기반 자기 반성 프롬프트."""
        hold_ratio = len(holds) / max(len(logs), 1) if logs else 0

        # 현재 파라미터
        params_str = json.dumps({k: v for k, v in self._params.items()
                                  if k in ('confidence_threshold', 'trailing_stop_pct', 'take_profit_pct',
                                           'hard_stop_pct', 'max_hold_minutes', 'no_entry_after')},
                                 ensure_ascii=False, indent=2)

        # 판단→결과 이력 (DB 기반)
        outcome_lines = ""
        for o in outcomes[:15]:
            pnl = o.get("realized_pnl")
            pnl_str = f"{pnl:+,}원" if pnl is not None else "미청산"
            hold_min = o.get("hold_minutes", "?")
            ind = o.get("indicators", {})
            ind_str = ", ".join(f"{k}={v}" for k, v in ind.items() if k not in ("target_price", "stop_loss"))
            outcome_lines += (
                f"  {o.get('decision_time','')[:16]} "
                f"{o.get('stock_code','')} "
                f"conf={o.get('confidence',0):.2f} "
                f"[{ind_str}] "
                f"→ {pnl_str} ({hold_min}분 보유)\n"
            )
        if not outcome_lines:
            outcome_lines = "  (구조화된 이력 없음 — JSONL 참고)\n"

        # 지표별 승률
        indicator_lines = ""
        for key, stats in indicator_stats.items():
            indicator_lines += f"\n  [{key}]\n"
            for s in stats:
                indicator_lines += (
                    f"    {s['range']}: {s['total']}건, "
                    f"승률 {s['win_rate']:.0%}, "
                    f"평균손익 {s['avg_pnl']:+,.0f}원\n"
                )
        if not indicator_lines:
            indicator_lines = "  (지표 분석 데이터 부족)\n"

        # confidence 구간별 승률
        conf_lines = ""
        for rng, s in conf_summary.items():
            conf_lines += (
                f"  {rng}: {s['total']}건, "
                f"승률 {s['win_rate']:.0%}, "
                f"평균손익 {s['avg_pnl']:+,.0f}원\n"
            )
        if not conf_lines:
            conf_lines = "  (데이터 부족)\n"

        # 최근 JSONL 거래 요약 (보조)
        trade_summary = ""
        for l in (buys + sells)[-10:]:
            trade_summary += (
                f"  {l.get('time','')[:16]} {l.get('action','')} "
                f"conf={l.get('confidence',0):.2f} {l.get('detail','')[:60]}\n"
            )
        if not trade_summary:
            trade_summary = "  (거래 없음)\n"

        return f"""당신은 미리내 2호(단타봇)의 자기 반성 모드입니다.
아래 **구조화된 데이터**를 분석하고, 데이터 기반으로 정책 개선안을 제시하세요.

## 1. 판단→결과 이력 (DB 기반, 가장 중요)
{outcome_lines}
## 2. 지표별 승률 분석
{indicator_lines}
## 3. confidence 구간별 승률
{conf_lines}
## 4. 최근 거래 로그 (JSONL, 보조)
  총 판단: {len(logs)}건, 매수 {len(buys)}건, 매도 {len(sells)}건, HOLD {hold_ratio:.0%}
{trade_summary}
## 5. 현재 파라미터
{params_str}

## 분석 지침
1. **지표별 승률**이 핵심: 어떤 지표가 높을 때 수익률이 좋은가?
2. **confidence 구간별 승률**: 임계값을 올려야 하나 내려야 하나?
3. **손실 패턴**: 공통점은? (시간대, 지표 조합, 보유 시간)
4. **파라미터 변경은 데이터 근거 필수** — "느낌"이 아닌 수치 기반

## 출력 (JSON만)
```json
{{
  "analysis": "데이터 기반 분석 요약 (한국어, 3줄 이내)",
  "lessons": ["교훈1 (수치 근거 포함)", "교훈2"],
  "param_changes": {{
    "confidence_threshold": 0.6,
    "trailing_stop_pct": -0.8
  }},
  "reasoning": "변경 이유 (수치 근거)"
}}
```
JSON만 출력하세요."""

    def _apply_review(self, output: str):
        """AI 반성 결과를 적용."""
        import re

        # JSON 파싱
        match = re.search(r'\{[\s\S]*"lessons"[\s\S]*\}', output)
        if not match:
            return

        try:
            review = json.loads(match.group())
        except json.JSONDecodeError:
            return

        lessons = review.get("lessons", [])
        param_changes = review.get("param_changes", {})
        reasoning = review.get("reasoning", "")

        # 1. 교훈을 룰 파일에 추가
        if lessons:
            rules_path = PROJECT_ROOT / "data" / "bot_rules" / "mirinae-2.md"
            try:
                content = rules_path.read_text(encoding="utf-8")
                # "## 자기 반성 기록" 섹션이 있으면 추가, 없으면 생성
                now = kst_now().strftime("%Y-%m-%d %H:%M")
                entry = f"\n### {now}\n"
                for lesson in lessons[:3]:  # 최대 3개
                    entry += f"- {lesson[:80]}\n"
                if reasoning:
                    entry += f"- 변경: {reasoning[:80]}\n"

                if "## 자기 반성 기록" in content:
                    content = content.replace("## 자기 반성 기록", f"## 자기 반성 기록\n{entry}")
                else:
                    content += f"\n\n## 자기 반성 기록\n{entry}"

                rules_path.write_text(content, encoding="utf-8")
                logger.info("[%s] 룰 파일 업데이트: %d개 교훈", self.BOT_ID, len(lessons))
            except Exception as e:
                logger.warning("[%s] 룰 파일 업데이트 실패: %s", self.BOT_ID, e)

        # 2. 파라미터 변경 (±20% 범위 내)
        if param_changes:
            cfg = load_bot_config(self.BOT_ID)
            changed = []
            for key, new_val in param_changes.items():
                if key not in self._params:
                    continue
                old_val = self._params.get(key)
                if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
                    if old_val != 0:
                        change_ratio = abs(new_val - old_val) / abs(old_val)
                        if change_ratio > 0.20:
                            continue  # ±20% 초과 거부
                    cfg[key] = new_val
                    self._params[key] = new_val
                    changed.append(f"{key}: {old_val} → {new_val}")

            if changed:
                cfg["_self_review_at"] = kst_now().isoformat()
                save_bot_config(self.BOT_ID, cfg)
                logger.info("[%s] 파라미터 자동 조정: %s", self.BOT_ID, ", ".join(changed))
                self._post_to_board("alert", f"자기반성: {', '.join(changed[:2])}")
