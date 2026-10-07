"""공통 거래 안전장치 — 트레일링 스탑, 거래 제한, 장 마감 청산.

모든 거래 봇이 이 모듈의 클래스를 조합하여 사용.
에볼버 봇이 config를 갱신하면 파라미터가 동적으로 변경됨.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, time
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import PROJECT_ROOT

_KST = ZoneInfo("Asia/Seoul")


def kst_now() -> datetime:
    """KST 현재 시각. 모든 시간 판단에 이 함수를 사용."""
    return datetime.now(_KST)

logger = logging.getLogger(__name__)

# 봇별 설정 저장 경로 (에볼버가 이 파일들을 갱신)
CONFIG_DIR = PROJECT_ROOT / "data" / "bot_config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# 진화 이력 저장 경로
EVOLUTION_DIR = PROJECT_ROOT / "data" / "evolution"
EVOLUTION_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════
# 1. 동적 Config 로더
# ═══════════════════════════════════════════

def load_bot_config(bot_id: str) -> dict:
    """봇 설정 파일 로드. 에볼버가 수정한 최신 파라미터 반영."""
    path = CONFIG_DIR / f"{bot_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def save_bot_config(bot_id: str, config: dict):
    """봇 설정 저장."""
    path = CONFIG_DIR / f"{bot_id}.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════
# 2. 트레일링 스탑
# ═══════════════════════════════════════════

@dataclass
class TrailingStop:
    """트레일링 스탑 — 최고가 대비 하락 시 청산 트리거.

    사용법:
        ts = TrailingStop(stop_pct=-0.8, take_profit_pct=1.5)
        ts.update(current_price)
        if ts.should_exit(current_price):
            # 매도 실행
    """
    stop_pct: float = -0.8        # 트레일링 손절 % (최고가 대비)
    take_profit_pct: float | None = 1.5  # 익절 % (None = 비활성, 트레일링에 위임)
    hard_stop_pct: float = -2.0   # 하드 손절 % (진입가 대비, 절대 방어선)
    atr_multiplier: float = 0      # 0=비활성, 2.0=2×ATR 트레일링
    atr_pct: float = 0             # ATR % (봇이 진입 시 설정)

    entry_price: int = 0          # 진입가
    peak_price: int = 0           # 진입 후 최고가
    _active: bool = False

    def activate(self, entry_price: int):
        """포지션 진입 시 활성화."""
        self.entry_price = entry_price
        self.peak_price = entry_price
        self._active = True

    def deactivate(self):
        """포지션 청산 시 비활성화."""
        self._active = False
        self.entry_price = 0
        self.peak_price = 0

    def update(self, current_price: int):
        """현재가 업데이트 — 최고가 갱신."""
        if self._active and current_price > self.peak_price:
            self.peak_price = current_price

    def should_exit(self, current_price: int) -> tuple[bool, str]:
        """청산 여부 판단.

        Returns:
            (should_exit, reason)
        """
        if not self._active or self.entry_price <= 0:
            return False, ""

        pct_from_entry = (current_price - self.entry_price) / self.entry_price * 100

        # 1) 익절 (None이면 비활성)
        if self.take_profit_pct is not None and pct_from_entry >= self.take_profit_pct:
            return True, f"익절 +{pct_from_entry:.2f}%"

        # 2) 하드 손절 (진입가 대비)
        if pct_from_entry <= self.hard_stop_pct:
            return True, f"하드손절 {pct_from_entry:.2f}%"

        # 3) 트레일링 손절 (최고가 대비)
        if self.peak_price > 0:
            pct_from_peak = (current_price - self.peak_price) / self.peak_price * 100
            # ATR 기반 동적 스탑 (설정 시)
            effective_stop = self.stop_pct
            if self.atr_multiplier > 0 and self.atr_pct > 0:
                atr_stop = -(self.atr_multiplier * self.atr_pct)
                # ATR 기반 스탑이 너무 좁아지는 것을 방지 (기본 stop_pct보다 더 넓게 유지)
                effective_stop = min(self.stop_pct, atr_stop)
            if pct_from_peak <= effective_stop:
                return True, f"트레일링 {pct_from_peak:.2f}% (한도 {effective_stop:.2f}%)"

        return False, ""


def calc_atr(candles: list[dict], period: int = 14) -> float:
    """ATR 계산 (% 기반). candles는 최신순, 각 candle은 {high, low, close}."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(period):
        h = candles[i].get("high", 0)
        l = candles[i].get("low", 0)
        prev_c = candles[i + 1].get("close", 0)
        if prev_c <= 0:
            continue
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr / prev_c * 100)
    return sum(trs) / len(trs) if trs else 0.0


# ═══════════════════════════════════════════
# 3. 일일 거래 제한
# ═══════════════════════════════════════════

@dataclass
class DailyTradeLimit:
    """하루 거래 횟수 제한.

    T+2 결제 문제 방지 + 과도한 로테이션 억제.
    """
    max_trades: int = 3           # 하루 최대 매매 횟수
    _today: str = ""
    _count: int = 0

    def can_trade(self) -> bool:
        """거래 가능 여부."""
        self._check_date_reset()
        return self._count < self.max_trades

    def record_trade(self):
        """거래 1건 기록."""
        self._check_date_reset()
        self._count += 1
        logger.info("일일 거래: %d/%d", self._count, self.max_trades)

    def remaining(self) -> int:
        """남은 거래 횟수."""
        self._check_date_reset()
        return max(0, self.max_trades - self._count)

    def _check_date_reset(self):
        """날짜 변경 시 카운터 리셋."""
        today = kst_now().date().isoformat()
        if self._today != today:
            self._today = today
            self._count = 0


# ═══════════════════════════════════════════
# 4. 최소 보유 시간
# ═══════════════════════════════════════════

@dataclass
class MinHoldTime:
    """매수 후 최소 보유 시간 강제.

    조기 청산(과도한 로테이션) 방지.
    """
    min_minutes: int = 20         # 최소 보유 분
    _entry_time: datetime | None = None

    def mark_entry(self):
        """매수 시점 기록."""
        self._entry_time = kst_now()

    def can_sell(self) -> bool:
        """매도 가능 여부 (최소 보유 시간 경과 확인)."""
        if self._entry_time is None:
            return True
        elapsed = (kst_now() - self._entry_time).total_seconds() / 60
        return elapsed >= self.min_minutes

    def remaining_minutes(self) -> float:
        """매도까지 남은 분."""
        if self._entry_time is None:
            return 0
        elapsed = (kst_now() - self._entry_time).total_seconds() / 60
        return max(0, self.min_minutes - elapsed)

    def clear(self):
        """포지션 청산 시 초기화."""
        self._entry_time = None


# ═══════════════════════════════════════════
# 5. 장 마감 강제 청산
# ═══════════════════════════════════════════

@dataclass
class MarketCloseGuard:
    """장 마감 N분 전 강제 청산 트리거.

    오버나이트 리스크 방지 — 당일 청산 원칙.

    시간대별 매도 전략:
      09:00~15:20  정상 거래 (봇 판단)
      15:20~15:30  시장가 강제 청산
      15:30~15:40  정규장 종료, 매도 불가
      15:40~16:00  시간외 종가 매도
      16:00 이후   매도 포기 (다음 날 처리)
    """
    close_time: time = field(default_factory=lambda: time(15, 20))  # 15:20 (마감 10분 전)
    _close_attempted: bool = field(default=False, repr=False)

    def should_force_close(self) -> bool:
        """강제 청산 시점인지 (15:20~16:00)."""
        now = kst_now().time()
        # 16:00 이후에는 포기 (다음 날 정규장에서 처리)
        if now >= time(16, 0):
            return False
        # 15:30~15:40 사이는 매도 불가 구간 (갭 타임)
        if time(15, 30) < now < time(15, 40):
            return False
        return now >= self.close_time

    def should_close_position(self, entry_time: datetime | None = None) -> bool:
        """포지션별 장 마감 청산 판단.

        15:00 이후 진입한 포지션은 오버나잇 허용 → 청산 제외.
        entry_time이 None이면 기존 동작 (무조건 청산).
        """
        if entry_time and entry_time.time() >= time(15, 0):
            return False  # 오버나잇 대상
        return self.should_force_close()

    def get_order_type(self) -> str:
        """현재 시간에 맞는 주문 유형 반환."""
        now = kst_now().time()
        if now <= time(15, 30):
            return "MARKET"          # 정규장 시장가
        elif time(15, 40) <= now < time(16, 0):
            return "AFTER_HOURS"     # 시간외 종가
        return "NONE"                # 매도 불가

    def mark_close_attempted(self):
        """청산 시도 기록 (무한 재시도 방지)."""
        self._close_attempted = True

    def was_close_attempted(self) -> bool:
        """이미 청산 시도했는지."""
        return self._close_attempted

    def reset_daily(self):
        """일일 초기화."""
        self._close_attempted = False

    def is_market_open(self) -> bool:
        """장 운영 시간인지 (09:00~15:30 KST)."""
        t = kst_now().time()
        return time(9, 0) <= t <= time(15, 30)

    def is_tradeable(self) -> bool:
        """매매 가능 시간인지 (정규장 + 시간외, KST)."""
        t = kst_now().time()
        return (time(9, 0) <= t <= time(15, 30)) or (time(15, 40) <= t < time(16, 0))

    def minutes_to_close(self) -> float:
        """마감까지 남은 분 (KST)."""
        now = kst_now()
        close = datetime.combine(now.date(), time(15, 30), tzinfo=now.tzinfo)
        return max(0, (close - now).total_seconds() / 60)


def get_market_session() -> dict:
    """현재 시장 세션 정보 반환 (KST 기준). 모든 봇/MCP가 이걸 참조."""
    now = kst_now()
    t = now.time()
    weekday = now.weekday()  # 0=월 ~ 6=일

    # 주말
    if weekday >= 5:
        return {
            "status": "CLOSED",
            "session": "weekend",
            "label": "주말 휴장",
            "can_buy": False,
            "can_sell": False,
            "minutes_to_close": 0,
            "minutes_to_open": 0,
            "current_time": now.strftime("%H:%M:%S KST"),
            "recommended_action": "대기",
        }

    sessions = [
        (time(0, 0), time(8, 59), "pre_market", "장 시작 전", False, False, "대기"),
        (time(9, 0), time(9, 5), "opening", "개장 직후 (5분)", False, False, "관망 — 개장 변동성 주의"),
        (time(9, 5), time(9, 30), "early_morning", "오전 초반", True, True, "추세 확인 후 진입 가능"),
        (time(9, 30), time(11, 30), "morning", "오전장", True, True, "정상 거래"),
        (time(11, 30), time(13, 0), "lunch", "점심시간", True, True, "거래량 감소 주의"),
        (time(13, 0), time(15, 0), "afternoon", "오후장", True, True, "정상 거래"),
        (time(15, 0), time(15, 10), "pre_close", "마감 20분 전", True, True, "신규 진입 주의, 빠른 청산 준비"),
        (time(15, 10), time(15, 20), "closing", "마감 직전", False, True, "시장가 청산만"),
        (time(15, 20), time(15, 30), "final", "마감 임박", False, True, "강제 청산"),
        (time(15, 30), time(15, 40), "gap", "매매 불가 (갭타임)", False, False, "주문 불가"),
        (time(15, 40), time(16, 0), "after_hours_close", "시간외 종가", False, True, "시간외 종가 매도만"),
        (time(16, 0), time(23, 59), "closed", "장 종료", False, False, "대기"),
    ]

    for start, end, session, label, can_buy, can_sell, rec in sessions:
        if start <= t <= end:
            close_dt = datetime.combine(now.date(), time(15, 30), tzinfo=_KST)
            mtc = max(0, (close_dt - now).total_seconds() / 60)
            open_dt = datetime.combine(now.date(), time(9, 0), tzinfo=_KST)
            mto = max(0, (open_dt - now).total_seconds() / 60) if t < time(9, 0) else 0

            return {
                "status": "OPEN" if can_buy or can_sell else "CLOSED",
                "session": session,
                "label": label,
                "can_buy": can_buy,
                "can_sell": can_sell,
                "minutes_to_close": round(mtc, 1),
                "minutes_to_open": round(mto, 1),
                "current_time": now.strftime("%H:%M:%S KST"),
                "recommended_action": rec,
            }

    return {"status": "UNKNOWN", "session": "unknown", "can_buy": False, "can_sell": False,
            "current_time": now.strftime("%H:%M:%S KST"), "recommended_action": "대기"}


# ═══════════════════════════════════════════
# 6. 봇 로그 기록
# ═══════════════════════════════════════════

BOT_LOG_DIR = PROJECT_ROOT / "data" / "bot_logs"
BOT_LOG_DIR.mkdir(parents=True, exist_ok=True)


def write_bot_log(bot_id: str, entry: dict):
    """봇 판단 로그를 JSONL 파일에 기록."""
    today = kst_now().date().isoformat()
    log_file = BOT_LOG_DIR / f"{bot_id}_{today}.jsonl"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("[%s] 로그 기록 실패: %s", bot_id, e)


def read_bot_logs(bot_id: str, target_date: str = "") -> list[dict]:
    """봇 로그 읽기. 에볼버가 분석할 때 사용."""
    target = target_date or kst_now().date().isoformat()
    log_file = BOT_LOG_DIR / f"{bot_id}_{target}.jsonl"
    if not log_file.exists():
        return []
    entries = []
    try:
        for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
            if line:
                entries.append(json.loads(line))
    except Exception as e:
        logger.warning("[%s] 로그 읽기 실패: %s", bot_id, e)
    return entries
