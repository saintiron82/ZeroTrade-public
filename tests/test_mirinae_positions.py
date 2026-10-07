"""Tests for Mirinae multi-position reconciliation and exit guards."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.bots.ai_caller import AIResult
from src.bots.base import BotConfig
from src.bots.mirinae import MirinaeBot
from src.bots.safeguards import MinHoldTime, TrailingStop


def _bare_bot() -> MirinaeBot:
    bot = object.__new__(MirinaeBot)
    bot.BOT_ID = "mirinae-2"
    bot._config = BotConfig(instance_id="strat-001:mirinae-2")
    bot._params = {
        "trailing_stop_pct": -1.2,
        "take_profit_pct": 1.8,
        "hard_stop_pct": -1.3,
        "min_hold_minutes": 3,
    }
    bot._positions = []
    bot._holding_code = ""
    bot._holding_qty = 0
    bot._holding_price = 0
    bot._holding_entry_time = None
    bot._save_state = MagicMock()
    bot._last_ai_error = ""
    bot._last_raw_output_preview = ""
    bot._last_preflight = {}
    bot._last_preflight_board_key = ""
    bot._last_position_reviews = []
    bot._debate_log = []
    bot._trade_log = []
    bot._realized_pnl = 0
    bot._scan_count = 0
    return bot


def test_invoke_codex_records_empty_ai_output_reason():
    bot = _bare_bot()
    bot._post_to_board = MagicMock()

    with patch(
        "src.bots.ai_caller.invoke_ai_result",
        return_value=AIResult("", "codex", "", 0, 0),
    ):
        output = bot._invoke_codex("prompt")

    assert output == ""
    assert bot._last_ai_error == "AI 응답 없음(provider=codex)"
    assert bot._last_raw_output_preview == ""
    bot._post_to_board.assert_not_called()


def test_invoke_codex_records_ai_exception_reason():
    bot = _bare_bot()
    bot._post_to_board = MagicMock()

    with patch("src.bots.ai_caller.invoke_ai_result", side_effect=RuntimeError("provider down")):
        output = bot._invoke_codex("prompt")

    assert output == ""
    assert bot._last_ai_error == "AI 호출 예외(provider=codex): provider down"
    bot._post_to_board.assert_called_once()


def test_parse_decision_records_json_parse_reason():
    bot = _bare_bot()
    output = "분석은 했지만 JSON 결정 블록이 없습니다."

    decision = bot._parse_decision(output)

    assert decision is None
    assert bot._last_ai_error == f"AI 응답 JSON 결정 블록 없음(raw {len(output)}자)"
    assert bot._last_raw_output_preview == output


def test_buy_preflight_summary_includes_search_audit():
    bot = _bare_bot()
    preflight = {
        "reason": "실제 주문 가능 후보 없음",
        "cash_cap": 111168,
        "etf_count": 19,
        "priced_count": 2,
        "buyable_candidates": [],
        "blocked_candidates": [
            {"code": "069500", "name": "KODEX 200", "price": 51000, "reason": "중복 진입 차단"}
        ],
        "price_errors": [{"code": "091160", "reason": "timeout"}],
    }

    summary = bot._format_buy_preflight_summary(preflight)

    assert "가용캡 111,168원" in summary
    assert "스캔 19개" in summary
    assert "가격 후보 2개" in summary
    assert "룰 차단 1개" in summary
    assert "091160:timeout" in summary


def test_save_state_persists_buy_search_context():
    bot = _bare_bot()
    bot._save_state = MirinaeBot._save_state.__get__(bot, MirinaeBot)
    bot._last_analysis = {"scan": 7}
    bot._last_preflight = {"reason": "예산/최대주문액 내 가격 종목 없음", "cash_cap": 111168}
    bot._last_position_reviews = [{"code": "102970", "decision": "review_target"}]
    bot._debate_log = [{"decision": "HOLD"}]
    bot._trade_log = [{"action": "hold", "detail": "AI 호출 생략"}]
    db = MagicMock()

    with patch("src.db.get_db", return_value=db):
        bot._save_state()

    saved = db.save_bot_state.call_args.args[1]
    assert saved["last_preflight"] == bot._last_preflight
    assert saved["position_reviews"] == bot._last_position_reviews
    assert saved["debate_log"] == bot._debate_log
    assert saved["recent_trades"] == bot._trade_log


def test_reconcile_restores_position_from_own_ledger_summing_unsold_lots():
    """본인 ledger의 매도되지 않은 모든 lot을 합산해 qty/avg_price를 복원."""
    bot = _bare_bot()
    db = MagicMock()
    db.get_trades.return_value = [
        {
            "timestamp": "2026-04-07T12:29:24+09:00",
            "side": "buy",
            "stock_code": "102780",
            "quantity": 1,
            "price": 18730,
            "success": True,
        },
        {
            "timestamp": "2026-05-04T10:42:13+09:00",
            "side": "buy",
            "stock_code": "102780",
            "quantity": 1,
            "price": 23170,
            "success": True,
        },
    ]
    # 파이프 holdings는 다른 봇 영향으로 1주만 표시 — 본인 ledger와 무관.
    strategy = SimpleNamespace(
        holdings=[{"code": "102780", "name": "KODEX 삼성그룹", "qty": 1, "avg_price": 23170}]
    )

    with patch("src.db.get_db", return_value=db), patch("src.strategy.StrategyManager") as mgr_cls:
        mgr_cls.return_value.get.return_value = strategy
        bot._reconcile_positions_with_trades()

    # 본인 ledger 기준: 2주 매수, 0주 매도 → 2주 보유, 평균가 (18730+23170)//2
    assert len(bot._positions) == 1
    assert bot._positions[0]["code"] == "102780"
    assert bot._positions[0]["qty"] == 2
    assert bot._positions[0]["price"] == (18730 + 23170) // 2


def test_reconcile_syncs_live_position_qty_and_price_to_ledger_truth():
    """본인 ledger가 진실원천. live position이 ledger와 다르면 ledger로 갱신."""
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(23170)
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 1,
            "price": 23170,
            "bought_at": "2026-05-04T10:42:13+09:00",
            "entry_time": None,
            "trailing_stop": trailing,
            "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 23500,
            "stop_loss": 22900,
        }
    ]
    db = MagicMock()
    db.get_trades.return_value = [
        {
            "timestamp": "2026-04-07T12:29:24+09:00",
            "side": "buy",
            "stock_code": "102780",
            "quantity": 1,
            "price": 18730,
            "success": True,
        },
        {
            "timestamp": "2026-05-04T10:42:13+09:00",
            "side": "buy",
            "stock_code": "102780",
            "quantity": 1,
            "price": 23170,
            "success": True,
        },
    ]
    strategy = SimpleNamespace(
        holdings=[{"code": "102780", "name": "KODEX 삼성그룹", "qty": 1, "avg_price": 23170}]
    )

    with patch("src.db.get_db", return_value=db), patch("src.strategy.StrategyManager") as mgr_cls:
        mgr_cls.return_value.get.return_value = strategy
        bot._reconcile_positions_with_trades()

    expected_avg = (18730 + 23170) // 2
    assert bot._positions[0]["qty"] == 2
    assert bot._positions[0]["price"] == expected_avg
    assert bot._positions[0]["trailing_stop"].entry_price == expected_avg


def test_reconcile_corrects_position_price_from_own_ledger_average():
    """오염된 live price를 본인 ledger 기준 평균가로 교정."""
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(23207)
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 1,
            "price": 23207,  # 잘못된 가격
            "bought_at": "2026-05-04T10:53:52+09:00",
            "entry_time": None,
            "trailing_stop": trailing,
            "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 23530,
            "stop_loss": 23000,
        }
    ]
    db = MagicMock()
    db.get_trades.return_value = [
        {
            "timestamp": "2026-04-07T12:29:24+09:00",
            "side": "buy",
            "stock_code": "102780",
            "quantity": 1,
            "price": 18730,
            "success": True,
        },
        {
            "timestamp": "2026-05-04T10:53:52+09:00",
            "side": "buy",
            "stock_code": "102780",
            "quantity": 1,
            "price": 23245,
            "success": True,
        },
    ]
    strategy = SimpleNamespace(
        holdings=[{"code": "102780", "name": "KODEX 삼성그룹", "qty": 1, "avg_price": 23245}]
    )

    with patch("src.db.get_db", return_value=db), patch("src.strategy.StrategyManager") as mgr_cls:
        mgr_cls.return_value.get.return_value = strategy
        bot._reconcile_positions_with_trades()

    expected_avg = (18730 + 23245) // 2
    assert bot._positions[0]["qty"] == 2
    assert bot._positions[0]["price"] == expected_avg
    assert bot._positions[0]["trailing_stop"].entry_price == expected_avg


def test_reconcile_clears_positions_when_ledger_empty_after_cleanup():
    """phantom-cleanup이 ledger를 비우면 in-memory _positions도 비워야 한다.

    버그: 과거에는 rebuilt가 empty면 early return → in-memory phantom 잔존.
    """
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(25000)
    bot._positions = [
        {
            "code": "102780", "name": "KODEX 삼성그룹", "qty": 1, "price": 25000,
            "bought_at": "2026-05-06T11:00:00+09:00", "entry_time": None,
            "trailing_stop": trailing, "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 0, "stop_loss": 0,
        }
    ]
    db = MagicMock()
    # ledger = buy 1 + phantom-cleanup sell 1 → empty positions
    db.get_trades.return_value = [
        {"timestamp": "2026-05-06T11:00:00+09:00",
         "side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "success": True},
        {"timestamp": "2026-05-07T10:00:00+09:00",
         "side": "sell", "stock_code": "102780", "quantity": 1, "price": 25000,
         "success": True, "reason": "phantom-cleanup"},
    ]

    with patch("src.db.get_db", return_value=db):
        bot._reconcile_positions_with_trades()

    assert bot._positions == []


def test_reconcile_uses_own_ledger_qty_even_when_pipe_holdings_smaller():
    """봇 ledger가 3주이고 파이프 holdings가 1주여도 본인 ledger 3주 사용.

    구버그 (`min(ledger, pipe_qty)`): 1주로 잘못 캡됨.
    신규 동작: ledger 3주 그대로. 파이프는 조회조차 하지 않음.
    """
    bot = _bare_bot()
    db = MagicMock()
    db.get_trades.return_value = [
        {"timestamp": "2026-05-06T11:00:00+09:00",
         "side": "buy", "stock_code": "102780", "quantity": 3, "price": 25000,
         "success": True},
    ]
    # 파이프 holdings = 1주. 구버그면 min(3, 1) = 1주가 됨.
    strategy = SimpleNamespace(holdings=[
        {"code": "102780", "name": "KODEX 삼성그룹", "qty": 1, "avg_price": 25000},
    ])

    with patch("src.db.get_db", return_value=db), \
         patch("src.strategy.StrategyManager") as mgr_cls:
        mgr_cls.return_value.get.return_value = strategy
        bot._reconcile_positions_with_trades()

    assert len(bot._positions) == 1
    assert bot._positions[0]["code"] == "102780"
    assert bot._positions[0]["qty"] == 3  # ledger 그대로
    # StrategyManager는 호출조차 안 됨 — 파이프 격리 검증
    mgr_cls.assert_not_called()


def test_position_exit_is_blocked_when_session_cannot_sell():
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(19300)
    bot._positions = [
        {
            "code": "117460",
            "name": "KODEX 에너지화학",
            "qty": 2,
            "price": 19300,
            "trailing_stop": trailing,
            "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 0,
            "stop_loss": 0,
        }
    ]
    market = MagicMock()
    market.get_price.return_value = {"price": 18925}
    bot._get_market = MagicMock(return_value=market)
    bot._market_guard = MagicMock()
    bot._market_guard.should_force_close.return_value = False

    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": False, "label": "장 시작 전"},
    ):
        actions = bot._check_positions()

    assert actions == []
    assert bot._last_position_reviews[0]["decision"] == "blocked_sell"
    assert "매도 불가 세션" in bot._last_position_reviews[0]["reason"]


def test_position_exit_passes_sell_basis_to_engine():
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(70000)
    hold_timer = MinHoldTime(min_minutes=3)
    bot._positions = [
        {
            "code": "005930",
            "name": "삼성전자",
            "qty": 2,
            "price": 70000,
            "trailing_stop": trailing,
            "hold_timer": hold_timer,
            "target_price": 73000,
            "stop_loss": 69500,
        }
    ]
    bot.sell = MagicMock(
        return_value={
            "success": True,
            "sell_price": 69100,
            "realized_pnl": -1800,
            "sell_basis": {},
        }
    )

    result = bot._execute_position_exit({
        "action": "sell",
        "code": "005930",
        "qty": 2,
        "current_price": 69200,
        "reason": "트레일링 손절",
    })

    assert result["success"] is True
    kwargs = bot.sell.call_args.kwargs
    assert kwargs["entry_price"] == 70000
    assert kwargs["reason"] == "트레일링 손절"
    assert kwargs["basis"]["entry_price"] == 70000
    assert kwargs["basis"]["current_price"] == 69200
    assert kwargs["basis"]["target_price"] == 73000
    assert kwargs["basis"]["stop_loss"] == 69500
    assert kwargs["basis"]["sell_qty"] == 2


def test_target_reached_requests_review_instead_of_auto_sell():
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(70000)
    bot._positions = [
        {
            "code": "005930",
            "name": "삼성전자",
            "qty": 2,
            "price": 70000,
            "trailing_stop": trailing,
            "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 73000,
            "stop_loss": 69500,
        }
    ]
    market = MagicMock()
    market.get_price.return_value = {"price": 73100}
    bot._get_market = MagicMock(return_value=market)
    bot._market_guard = MagicMock()
    bot._market_guard.should_force_close.return_value = False

    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": True, "label": "장중"},
    ):
        actions = bot._check_positions()

    assert actions == []
    assert bot._last_position_reviews[0]["decision"] == "review_target"
    assert "목표가 도달" in bot._last_position_reviews[0]["reason"]


def test_ai_failure_fallback_sells_target_reached_position():
    bot = _bare_bot()
    bot._post_to_board = MagicMock()
    bot._last_position_reviews = [
        {
            "code": "005930",
            "decision": "review_target",
            "current_price": 73100,
            "pnl_pct": 4.43,
            "reason": "익절 +4.43% - 익절/목표 상향/손절 상향 재평가 필요",
        }
    ]

    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": True, "label": "장중"},
    ):
        actions = bot._ai_failure_exit_actions()

    assert actions == [
        {
            "action": "sell",
            "code": "005930",
            "reason": "AI 호출 실패 fallback: 목표/익절 도달 (손익 +4.43%, 현재가 73,100)",
            "current_price": 73100,
            "force_close": False,
        }
    ]
    bot._post_to_board.assert_called_once()


def test_ai_failure_fallback_does_not_sell_when_session_cannot_sell():
    bot = _bare_bot()
    bot._post_to_board = MagicMock()
    bot._last_position_reviews = [
        {
            "code": "005930",
            "decision": "review_target",
            "current_price": 73100,
            "pnl_pct": 4.43,
        }
    ]

    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": False, "label": "장 시작 전"},
    ):
        actions = bot._ai_failure_exit_actions()

    assert actions == []
    bot._post_to_board.assert_not_called()


def test_losing_position_with_downtrend_requests_loss_review():
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(10000)
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 2,
            "price": 10000,
            "trailing_stop": trailing,
            "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 10200,
            "stop_loss": 0,
        }
    ]
    market = MagicMock()
    market.get_price.return_value = {"price": 9950}
    market.get_minute_price.return_value = [
        {"price": 9950, "high": 9970},
        {"price": 9960, "high": 9980},
        {"price": 9970, "high": 9990},
        {"price": 9980, "high": 10000},
    ]
    bot._get_market = MagicMock(return_value=market)
    bot._market_guard = MagicMock()
    bot._market_guard.should_force_close.return_value = False

    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": True, "label": "장중"},
    ):
        actions = bot._check_positions()

    assert actions == []
    review = bot._last_position_reviews[0]
    assert review["decision"] == "review_loss"
    assert "손실 보유 재검토" in review["reason"]
    assert review["minute_trend_pct"] < 0


def test_loss_breakdown_creates_defensive_sell_action():
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(10000)
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 2,
            "price": 10000,
            "trailing_stop": trailing,
            "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 10200,
            "stop_loss": 0,
        }
    ]
    market = MagicMock()
    market.get_price.return_value = {"price": 9900}
    market.get_minute_price.return_value = [
        {"price": 9900, "high": 9920},
        {"price": 9920, "high": 9940},
        {"price": 9940, "high": 9960},
        {"price": 9960, "high": 9980},
    ]
    bot._get_market = MagicMock(return_value=market)
    bot._market_guard = MagicMock()
    bot._market_guard.should_force_close.return_value = False

    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": True, "label": "장중"},
    ):
        actions = bot._check_positions()

    assert actions
    assert actions[0]["action"] == "sell"
    assert actions[0]["code"] == "102780"
    assert "손실 확대" in actions[0]["reason"]


def test_missing_target_or_stop_requests_plan_review():
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(10000)
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 2,
            "price": 10000,
            "trailing_stop": trailing,
            "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 0,
            "stop_loss": 0,
        }
    ]
    market = MagicMock()
    market.get_price.return_value = {"price": 10050}
    market.get_minute_price.return_value = [
        {"price": 10050, "high": 10060},
        {"price": 10045, "high": 10055},
    ]
    bot._get_market = MagicMock(return_value=market)
    bot._market_guard = MagicMock()
    bot._market_guard.should_force_close.return_value = False

    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": True, "label": "장중"},
    ):
        actions = bot._check_positions()

    assert actions == []
    review = bot._last_position_reviews[0]
    assert review["decision"] == "review_plan"
    assert review["plan_status"] == "missing"
    assert "목표가/손절가 미설정" in review["plan_warning"]


def test_poor_risk_reward_requests_plan_review():
    bot = _bare_bot()
    trailing = TrailingStop(stop_pct=-1.2, take_profit_pct=1.8, hard_stop_pct=-1.3)
    trailing.activate(10000)
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 2,
            "price": 10000,
            "trailing_stop": trailing,
            "hold_timer": MinHoldTime(min_minutes=3),
            "target_price": 10020,
            "stop_loss": 9900,
        }
    ]
    market = MagicMock()
    market.get_price.return_value = {"price": 10000}
    market.get_minute_price.return_value = [
        {"price": 10000, "high": 10010},
        {"price": 10000, "high": 10010},
    ]
    bot._get_market = MagicMock(return_value=market)
    bot._market_guard = MagicMock()
    bot._market_guard.should_force_close.return_value = False

    with patch(
        "src.bots.safeguards.get_market_session",
        return_value={"can_sell": True, "label": "장중"},
    ):
        actions = bot._check_positions()

    assert actions == []
    review = bot._last_position_reviews[0]
    assert review["decision"] == "review_plan"
    assert review["plan_status"] == "poor_risk_reward"
    assert review["risk_reward"] == 0.2


def test_position_priority_blocks_new_buy_when_loss_review_exists():
    bot = _bare_bot()
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 2,
            "price": 10000,
        }
    ]
    bot._last_position_reviews = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "decision": "review_loss",
            "pnl_pct": -0.6,
            "reason": "손실 보유 재검토 필요",
        }
    ]

    decision = bot._parse_decision(
        '{"decision":"BUY","confidence":0.9,"code":"102970","name":"KODEX 증권","trader_reason":"신규 모멘텀"}'
    )

    assert decision["decision"] == "HOLD"
    assert decision["code"] == "102780"
    assert decision["position_priority_blocked_buy"] == "102970"
    assert "신규 BUY" in decision["trader_reason"]


def test_invalid_review_plan_rejects_keep_without_revision():
    bot = _bare_bot()
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 2,
            "price": 10000,
        }
    ]
    bot._last_position_reviews = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "decision": "review_plan",
            "current_price": 10000,
            "target_price": 10020,
            "stop_loss": 9900,
            "plan_valid": False,
            "plan_warning": "손익비 부족",
            "pnl_pct": 0.0,
            "reason": "목표/손절 계획 재검증 필요",
        }
    ]

    decision = bot._parse_decision(
        '{"decision":"HOLD","confidence":0.8,"code":"102780","name":"KODEX 삼성그룹",'
        '"target_action":"keep","target_price":10020,"stop_loss":9900,'
        '"trader_reason":"그대로 유지"}'
    )

    assert decision["decision"] == "HOLD"
    assert decision["plan_update_missing"] is True
    assert decision["confidence"] == 0.49
    assert "기존 계획이 유효하지 않은데 그대로 keep" in decision["trader_reason"]


def test_review_plan_accepts_ai_revised_target_and_stop():
    bot = _bare_bot()
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 2,
            "price": 10000,
        }
    ]
    bot._last_position_reviews = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "decision": "review_plan",
            "current_price": 10000,
            "target_price": 10020,
            "stop_loss": 9900,
            "plan_valid": False,
            "plan_warning": "손익비 부족",
            "pnl_pct": 0.0,
            "reason": "목표/손절 계획 재검증 필요",
        }
    ]

    decision = bot._parse_decision(
        '{"decision":"HOLD","confidence":0.8,"code":"102780","name":"KODEX 삼성그룹",'
        '"target_action":"raise","target_price":10300,"stop_loss":9920,'
        '"trader_reason":"목표를 상향해 손익비를 회복"}'
    )

    assert decision["decision"] == "HOLD"
    assert "plan_update_missing" not in decision
    assert decision["target_action"] == "raise"


def test_hold_decision_can_adjust_target_and_stop():
    bot = _bare_bot()
    bot._positions = [
        {
            "code": "005930",
            "name": "삼성전자",
            "qty": 2,
            "price": 70000,
            "target_price": 73000,
            "stop_loss": 69500,
        }
    ]

    result = bot._execute_decision({
        "decision": "HOLD",
        "code": "005930",
        "target_price": 76000,
        "stop_loss": 72000,
        "trader_reason": "추세 유지, 목표 상향",
    })

    assert result["success"] is True
    assert result["action"] == "adjust_plan"
    assert bot._positions[0]["target_price"] == 76000
    assert bot._positions[0]["stop_loss"] == 72000


def test_partial_sell_updates_remaining_position_plan():
    bot = _bare_bot()
    hold_timer = MagicMock()
    hold_timer.can_sell.return_value = True
    bot._positions = [
        {
            "code": "102970",
            "name": "KODEX 증권",
            "qty": 4,
            "price": 29655,
            "target_price": 30600,
            "stop_loss": 29250,
            "hold_timer": hold_timer,
            "trailing_stop": MagicMock(),
        }
    ]
    bot.sell = MagicMock(return_value={"success": True, "sell_price": 32600})
    bot._daily_limit = MagicMock()

    result = bot._execute_decision({
        "decision": "SELL",
        "code": "102970",
        "sell_qty": 2,
        "target_action": "raise",
        "target_price": 33800,
        "stop_loss": 31800,
        "trader_reason": "절반 익절 후 잔여 목표/손절 상향",
    })

    assert result["success"] is True
    assert result["action"] == "partial_sell"
    assert result["remaining_qty"] == 2
    assert bot._positions[0]["qty"] == 2
    assert bot._positions[0]["target_price"] == 33800
    assert bot._positions[0]["stop_loss"] == 31800
    assert result["plan_update"]["changes"]["target_price"] == {"old": 30600, "new": 33800}
    assert result["plan_update"]["changes"]["stop_loss"] == {"old": 29250, "new": 31800}


def test_position_report_prefers_latest_decision_plan_over_stale_review():
    from src.web import routes

    status = {
        "positions": [{
            "code": "102970",
            "name": "KODEX 증권",
            "qty": 2,
            "entry_price": 29655,
            "current_price": 32625,
            "target_price": 30600,
            "stop_loss": 29250,
            "unrealized": 5940,
            "pnl_pct": 10.02,
        }],
        "last_analysis": {
            "decision": {
                "decision": "SELL",
                "code": "102970",
                "confidence": 0.68,
                "target_action": "raise",
                "target_price": 33800,
                "stop_loss": 31800,
                "trader_reason": "남은 2주는 목표 33800원과 손절 31800원으로 운용",
            },
            "position_reviews": [{
                "code": "102970",
                "target_price": 30600,
                "stop_loss": 29250,
                "decision": "review_target",
                "plan_valid": False,
                "plan_status": "target_reached",
                "plan_warning": "목표가 도달/초과",
            }],
        },
        "position_reviews": [{
            "code": "102970",
            "target_price": 30600,
            "stop_loss": 29250,
            "decision": "review_target",
            "plan_valid": False,
            "plan_status": "target_reached",
            "plan_warning": "목표가 도달/초과",
        }],
    }

    with patch.object(routes, "_refresh_position_prices", side_effect=lambda positions: positions):
        report = routes._build_position_reports("mirinae-3", status)[0]

    assert report["target_price"] == 33800
    assert report["stop_loss"] == 31800
    assert report["decision"] == "SELL"
    assert report["plan_status"] != "target_reached"
