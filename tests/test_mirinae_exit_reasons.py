"""청산 사유 라벨 세분화 테스트 — 실제 트리거별로 구분된 라벨이 붙는지 검증.

배경: mirinae-3의 14일 매도 13건 중 12건이 일괄 "AI 손절가 도달"로 기록됨
(+7,100원 수익 매도 포함). 라벨은 청산 판정 분기를 그대로 따라가야 하며,
수익 청산이 "손절" 라벨을 받으면 안 된다.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from src.bots.base import BotConfig
from src.bots.mirinae import MirinaeBot, kst_now
from src.bots.mirinae2 import Mirinae2Bot
from src.bots.safeguards import MinHoldTime, TrailingStop


def _bare_bot(cls=MirinaeBot, bot_id="mirinae-3"):
    bot = object.__new__(cls)
    bot.BOT_ID = bot_id
    bot._config = BotConfig(instance_id=f"strat-001:{bot_id}")
    bot._params = {
        "trailing_stop_pct": -1.2,
        "take_profit_pct": 1.8,
        "hard_stop_pct": -1.3,
        "min_hold_minutes": 3,
        "max_hold_minutes": 60,
    }
    bot._positions = []
    bot._last_position_reviews = []
    bot._save_state = MagicMock()
    bot._market_guard = MagicMock()
    bot._market_guard.should_force_close.return_value = False
    return bot


def _position(entry=10000, target=0, stop=0, *, take_profit=1.8, peak=None):
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=take_profit, hard_stop_pct=-1.3)
    trailing.activate(entry)
    if peak:
        trailing.update(peak)
    return {
        "code": "102780",
        "name": "KODEX 삼성그룹",
        "qty": 2,
        "price": entry,
        "trailing_stop": trailing,
        "hold_timer": MinHoldTime(min_minutes=3),
        "target_price": target,
        "stop_loss": stop,
    }


def _run_check(bot, current_price):
    market = MagicMock()
    market.get_price.return_value = {"price": current_price}
    market.get_minute_price.return_value = []  # 추세 스냅샷 비활성
    bot._get_market = MagicMock(return_value=market)
    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": True, "can_buy": True, "label": "장중"},
    ):
        return bot._check_positions()


# ───────────────────────────────────────────
# 1. AI 손절가(stop_price) 터치
# ───────────────────────────────────────────

def test_ai_stop_touch_at_loss_labeled_stop_loss_with_pnl():
    bot = _bare_bot()
    bot._positions = [_position(entry=10000, target=10500, stop=9800)]

    actions = _run_check(bot, current_price=9750)

    assert len(actions) == 1
    reason = actions[0]["reason"]
    assert reason.startswith("AI 손절가 도달")
    assert "-2.50%" in reason
    assert bot._last_position_reviews[0]["reason"] == reason
    assert bot._last_position_reviews[0]["decision"] == "sell"


def test_ai_stop_touch_at_profit_never_labeled_stop_loss():
    """회귀: +7,100원 수익 매도가 'AI 손절가 도달'로 기록되던 케이스.

    AI가 손절가를 진입가 위로 올린 상태에서 스탑 터치 → 수익 청산.
    라벨에 '손절'이 포함되면 안 되고, 손익 방향(+)이 드러나야 한다.
    """
    bot = _bare_bot()
    # 진입 10,000 / AI가 스탑을 10,500으로 상향 / 현재가 10,450 → +4.50% 수익 청산
    bot._positions = [_position(entry=10000, target=11000, stop=10500)]

    actions = _run_check(bot, current_price=10450)

    assert len(actions) == 1
    reason = actions[0]["reason"]
    assert "손절" not in reason
    assert "+4.50%" in reason
    assert "이익 보전" in reason
    assert bot._last_position_reviews[0]["reason"] == reason


# ───────────────────────────────────────────
# 2. 하드 손절 (진입가 대비 절대 방어선)
# ───────────────────────────────────────────

def test_hard_stop_labeled_with_entry_pnl():
    bot = _bare_bot()
    bot._positions = [_position(entry=10000)]  # stop_loss=0 → AI 스탑 분기 미발동

    actions = _run_check(bot, current_price=9850)  # -1.50% <= hard_stop -1.3%

    assert len(actions) == 1
    reason = actions[0]["reason"]
    assert reason.startswith("하드 손절")
    assert "-1.50%" in reason


# ───────────────────────────────────────────
# 3. 트레일링 스탑 (고점 대비)
# ───────────────────────────────────────────

def test_trailing_stop_labeled_with_peak_drawdown_and_pnl():
    bot = _bare_bot()
    # 진입 10,000 → 고점 10,300 → 현재 10,150 (고점 대비 -1.46%, 손익 +1.50%)
    bot._positions = [_position(entry=10000, peak=10300)]

    actions = _run_check(bot, current_price=10150)

    assert len(actions) == 1
    reason = actions[0]["reason"]
    assert reason.startswith("트레일링 스탑")
    assert "고점 대비 -1.46%" in reason
    assert "+1.50%" in reason
    # 회귀: 수익 상태의 트레일링 청산이 '손절'로 기록되면 안 된다
    assert "손절" not in reason


# ───────────────────────────────────────────
# 4. 보유시간 제한 (mirinae-2)
# ───────────────────────────────────────────

def test_time_limit_exit_labeled_with_held_minutes_and_pnl():
    bot = _bare_bot(Mirinae2Bot, bot_id="mirinae-2")
    pos = _position(entry=10000)
    pos["bought_at"] = (kst_now() - timedelta(minutes=125)).isoformat()
    bot._positions = [pos]

    actions = _run_check(bot, current_price=10050)  # +0.50% 수익 보유 중 시간 초과

    assert len(actions) == 1
    reason = actions[0]["reason"]
    assert reason.startswith("보유시간 제한 청산")
    assert "125분 > 60분" in reason
    assert "+0.50%" in reason
    assert "손절" not in reason


# ───────────────────────────────────────────
# 5. 장 마감 강제 청산 — 기존 라벨 유지 (mirinae-3 필터가 의존)
# ───────────────────────────────────────────

def test_market_close_label_unchanged():
    bot = _bare_bot()
    pos = _position(entry=10000)
    pos["entry_time"] = kst_now().replace(hour=10, minute=0, second=0, microsecond=0)
    bot._positions = [pos]
    bot._market_guard.should_force_close.return_value = True
    bot._market_guard.was_close_attempted.return_value = False
    bot._market_guard.get_order_type.return_value = "MARKET"

    actions = _run_check(bot, current_price=10050)

    force = [a for a in actions if a.get("force_close")]
    assert len(force) == 1
    assert force[0]["reason"] == "장 마감 강제 청산 (MARKET)"


# ───────────────────────────────────────────
# 6. 라벨 포맷터 단위 검증
# ───────────────────────────────────────────

def test_format_exit_label_each_trigger():
    fmt = MirinaeBot._format_exit_label
    assert fmt("ai_stop", -2.5, current_price=9750, stop=9800) == \
        "AI 손절가 도달 -2.50% (9,750 <= 9,800)"
    assert fmt("ai_stop", 4.5, current_price=10450, stop=10500) == \
        "AI 스탑 도달 +4.50% 이익 보전 (10,450 <= 10,500)"
    assert fmt("hard_stop", -1.5) == "하드 손절 -1.50%"
    assert fmt("trailing", 1.5, peak_drawdown_pct=-1.46) == \
        "트레일링 스탑 (고점 대비 -1.46%, 손익 +1.50%)"
    assert fmt("time_limit", 0.5, held_minutes=125, max_hold=60) == \
        "보유시간 제한 청산 (125분 > 60분, 손익 +0.50%)"


def test_format_exit_label_handles_missing_pnl():
    fmt = MirinaeBot._format_exit_label
    # pnl 미산출 시에도 라벨은 깨지지 않아야 한다
    assert fmt("ai_stop", None, current_price=9750, stop=9800) == \
        "AI 손절가 도달 (9,750 <= 9,800)"
    assert fmt("time_limit", None, held_minutes=125, max_hold=60) == \
        "보유시간 제한 청산 (125분 > 60분)"
    # peak/pnl 둘 다 없으면 원본 사유 유지
    assert fmt("trailing", None, raw_reason="트레일링 -1.46% (한도 -1.20%)") == \
        "트레일링 -1.46% (한도 -1.20%)"
    assert fmt("unknown", 1.0, raw_reason="원본 사유") == "원본 사유"
