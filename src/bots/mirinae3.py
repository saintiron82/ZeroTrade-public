"""미리내 3호 — AI 멀티에이전트 스윙/중장기 트레이딩 봇.

미리내 1호를 상속하되, 시간 제약이나 당일 청산 룰 같은 기계적인 구속을 모두 없애고
'월스트리트 최고 등급 주식 트레이더'라는 자유로운 페르소나를 부여해
AI 스스로 펀더멘털, 스윙 타점, 오버나이트를 자율적으로 결정하도록 설계됨.
"""

import logging
from src.bots.mirinae import MirinaeBot, DEFAULT_PARAMS as BASE_PARAMS
from src.bots.base import BotConfig
from src.bots.safeguards import load_bot_config

logger = logging.getLogger(__name__)

# 스윙/중장기 특화 파라미터
# - 스캔 간격은 10분 (장중 판단 밀도를 높여 기회 포착 강화)
# - 보유 시간 제한(max_hold_minutes)을 설정하지 않음 (오버나잇 허용)
# - 적극형 스윙: 완벽한 확신보다 양수 기대값과 빠른 리밸런싱 우선
# - 과매매 억제: confidence_threshold(0.62) + max_daily_trades(5) + min_hold_minutes(30)
SWING_PARAMS = {
    **BASE_PARAMS,
    "interval_seconds": 600,       # 10분 간격
    "confidence_threshold": 0.62,  # 적극형 스윙 진입 임계값
    "max_positions": 2,            # 동시 2종목 집중 투자
    "trailing_stop_pct": -2.2,     # 트레일링 -2.2%
    "take_profit_pct": 3.5,        # 스윙 1차 익절
    "hard_stop_pct": -3.5,         # 절대 손절 -3.5%
    "max_daily_trades": 5,         # 적극적 리밸런싱 허용
    "min_hold_minutes": 30,        # 매수 후 최소 30분 관찰
    "codex_timeout": 150,          # 장중 재판단 밀도 확보
}


class Mirinae3Bot(MirinaeBot):
    """미리내 3호 — 자율도 MAX 월스트리트 트레이더 페르소나 봇."""

    BOT_ID = "mirinae-3"

    def __init__(self, config: BotConfig | dict = None):
        # 스윙 파라미터 로드 및 오버라이드
        self._params = {**SWING_PARAMS}
        evolver_cfg = load_bot_config("mirinae-3")
        self._params.update(evolver_cfg)

        # 가드 설정
        # 시장 변동성에 무관하게 AI 판단을 우선하도록 최소한의 하드 가드만 유지
        # 봇 독립 4원칙: 거래내역 기준 관리. 봇간 종목/섹터 교차 차단은 OFF.
        self._guard_config = {
            "daily_loss_limit_pct": -5,   # 하루 -5% 손실 시 스톱
            "market_drop_block_pct": -2.0, # 코스피 -2% 폭락일 때는 매수 중지
            "trend_filter_enabled": False, # 필터 제거 (역추세 매수도 AI가 원하면 허용)
            "duplicate_check_enabled": False,
            "max_sector_exposure": 0,
        }

        if isinstance(config, dict):
            config.setdefault("interval_seconds", self._params["interval_seconds"])
        elif config is None:
            config = BotConfig(interval_seconds=self._params["interval_seconds"])

        # 부모 초기화 호출
        from src.bots.base import BotBase
        from src.bots.mirinae import DEFAULT_ETF_CODES
        BotBase.__init__(self, config)

        if not self._config.etf_codes:
            self._config.etf_codes = DEFAULT_ETF_CODES.copy()

        # 공통 상태 초기화
        from src.bots.safeguards import (
            TrailingStop, DailyTradeLimit, MinHoldTime, MarketCloseGuard,
        )
        self._positions = []
        self._debate_log = []
        self._last_analysis = None
        self._latest_signals = []
        self._last_validation: dict = {}
        self._last_preflight: dict = {}
        self._last_ai_error = ""
        self._last_raw_output_preview = ""
        self._last_preflight_board_key = ""

        self._daily_limit = DailyTradeLimit(max_trades=self._params["max_daily_trades"])
        self._market_guard = MarketCloseGuard()

        self._restore_positions()

        logger.info(
            "[%s] 초기화 — 자율 페르소나 모드, %ds 간격, confidence >= %.1f",
            self.BOT_ID,
            self._params["interval_seconds"],
            self._params["confidence_threshold"],
        )

    def _build_codex_prompt(self, context: str) -> str:
        """페르소나 특화 오픈 프롬프트. 기계적 제약 해제."""
        # 시간 검사는 하지만, 매수/매도 가능 여부 판단 용도로만 참고
        session = getattr(self, "_market_session", {})
        session_label = session.get("label", "?")
        can_buy = "매수 가능" if session.get("can_buy", False) else "신규 진입 불가"
        can_sell = "매도 가능" if session.get("can_sell", False) else "매도 불가"

        prompt = f"""당신은 **월스트리트 최고 레벨의 적극형 포트폴리오 트레이더**입니다.
당신의 강점은 관망이 아니라, 정보 우위가 생겼을 때 빠르게 포지션을 잡고 틀리면 빠르게 정리하는 것입니다.

아래는 현재 시장 환경과 시스템이 제공한 실시간 데이터입니다.

[현재 시장 환경]
  세션: {session_label}
  상태: {can_buy} / {can_sell}

{context}

## 당신의 철학과 행동 강령
1. **적극성**: 확률 우위가 55~60% 이상이고 손익비가 1.5:1 이상이면 BUY를 검토합니다. 완벽한 셋업을 기다리느라 장중 기회를 놓치지 마세요.
2. **자율성**: 오버나이트, 스윙, 단기 모멘텀 등 모든 전략적 선택이 당신에게 일임되어 있습니다. 추세가 살아 있으면 보유하고, 논리가 깨지면 즉각 매도합니다.
3. **상위 후보 선택**: 가장 좋은 ETF가 예산 초과이면 예산 내 차선 후보를 즉시 비교합니다. 실행 가능한 후보가 있으면 HOLD보다 작은 포지션이 우선입니다.
4. **거시적 시야 + 수급**: 1분봉의 미세한 움직임보다 추세, ETF 테마, 거래량 확장, 기관 자금의 유입에 더 무게를 둡니다.
5. **HOLD 제한**: HOLD는 데이터 부재, 거래 불가 세션, 손익비 불량, 또는 모든 후보의 기대값이 음수일 때만 선택합니다.

## 3단계 자기 검증 로직:
### 1. 기회 포착 (Bull)
- 현재 제공된 ETF 시세, 테마 동향, 수급, 뉴스에서 오늘 또는 익일까지 우위가 있는 자산 1~2개를 순위화하세요.
### 2. 치명적 약점 반문 (Bear)
- 내 생각이 틀렸을 가능성은? 시장의 함정은 아닌가? 이 자산의 하방 리스크(손절 시 타격)는 견딜만한 수준인가 반문하세요.
### 3. 최종 집행 (Trader)
- 가장 합리적이고 수학적인 결정을 내리되, 실행 가능한 양수 기대값이 있으면 BUY/SELL을 우선하세요.
- confidence(0.0~1.0)를 통해 솔직한 확신도를 표현하세요.
- 보유 종목이 있으면 신규 후보보다 보유 종목의 SELL/HOLD/추매 판단을 먼저 끝내세요.
- 매 스캔마다 기존 target_price/stop_loss가 현재가, 추세, 잔여시간 기준으로 아직 유효한지 검증하세요.
- [보유 평가 이벤트]가 review_loss, review_plan, review_target 또는 sell이면 신규 종목 BUY를 선택하지 마세요. 보유 종목의 논리와 목표/손절 계획이 아직 유효한지 먼저 검증해야 합니다.
- 손실 중인 보유 종목을 HOLD하려면 반등 근거를 가격·거래량·뉴스로 설명하고 target_price와 stop_loss를 다시 제시하세요. 근거가 약하면 SELL 또는 부분매도를 선택하세요.
- review_plan인 보유 종목은 기존 계획이 틀렸다는 신호입니다. HOLD하려면 target_action을 raise/lower 중 하나로 정하고 새 target_price와 stop_loss를 반드시 제시하세요. 기존 목표/손절 그대로 keep하지 마세요.
- target_action=keep은 계획검증이 ok일 때만 허용됩니다. keep이어도 target_price와 stop_loss 값을 반드시 다시 출력하세요.
- 이미 보유 중인 종목의 BUY는 추매입니다. 보유 중이라는 이유만으로 BUY를 금지하지 마세요.
- SELL은 전량 청산뿐 아니라 분할 매도도 가능합니다. 일부만 줄일 때는 sell_qty 또는 position_pct를 명시하세요.
- 보유 종목이 목표가에 도달했더라도 추세·뉴스·거래량이 살아 있으면 HOLD와 함께 target_price를 상향하고 stop_loss를 이익 보호선으로 올리세요.
- 모멘텀이 약해졌지만 매도 근거가 충분하지 않으면 HOLD와 함께 target_price를 하향하고 stop_loss를 조정하세요.
- **중요: 어떤 경우에도 HOLD, partial_sell 결정 시 target_price와 stop_loss 값을 반드시 0보다 큰 현재가 기준 유효한 정수로 제공해야 합니다. 누락하면 시스템 에러가 발생합니다.**

명령: 반드시 stdout에 아래 JSON 형식으로 당신의 결정을 제출하십시오.
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
    "macro_trend": {{"score": 0, "detail": "거시 추세와 거역하지 않는가? (0~10)"}},
    "momentum": {{"score": 0, "detail": "수급과 거래량이 충분히 모이고 있는가? (0~10)"}},
    "risk_reward": {{"score": 0, "detail": "손익비가 압도적으로 유리한가? (0~10)"}}
  }},
  "bull_summary": "당신이 포착한 강세 근거 (한국어)",
  "bear_summary": "당신이 반문한 약세/리스크 요인 (한국어)",
  "trader_reason": "전설적 트레이더로서 내린 최종 결정의 철학적/수학적 근거 (한국어)"
}}
```"""
        return prompt

    def _check_positions(self) -> list[dict]:
        """
        포지션 체크 로직.
        단타 봇(미리내 2호)과 달리 시간 제약(max_hold_minutes) 강제 청산 코드를 없앴습니다.
        따라서 오버나이트가 자연스럽게 허용되며,
        오직 장 마감(MarketCloseGuard)이나 트레일링/하드 손절 시에만 청산이 발생합니다.
        AI 모델이 명시적으로 'SELL'을 지시하거나, 시스템 손절에 도달할 때까지 영구히 홀드합니다.
        """
        exits = super()._check_positions()
        
        # 15시 20분 장 마감 강제 청산 룰을 완화하거나 유지할 지 결정할 수 있습니다.
        # 미리내 3호는 스윙과 오버나이트를 허용하므로, 장마감 강제 청산을 제거합니다.
        # super()._check_positions() 에는 _market_guard.should_force_close() 조건이 포함되어 있습니다.
        # 이를 무력화하기 위해 exit 목록에서 "장 마감 강제 청산" 사유의 매도를 걸러냅니다.
        filtered_exits = []
        for ex in exits:
            if "장 마감 강제 청산" in ex.get("reason", ""):
                logger.info("[%s] 스윙 모드이므로 장 마감 강제 청산(%s)을 무시합니다 (오버나이트 유지).", self.BOT_ID, ex.get("code"))
                continue
            filtered_exits.append(ex)
            
        return filtered_exits
