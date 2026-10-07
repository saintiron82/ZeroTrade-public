import json
import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

from src.bots.audit import AuditRecorder
from src.bots.base import BotConfig
from src.bots.mirinae import DEFAULT_PARAMS, MirinaeBot
from src.db import DB


def _bare_bot() -> MirinaeBot:
    bot = object.__new__(MirinaeBot)
    bot.BOT_ID = "mirinae"
    bot._config = BotConfig(instance_id="strat-001:mirinae", etf_codes=["069500"], interval_seconds=1)
    bot._params = {**DEFAULT_PARAMS, "ai_provider": "codex", "ai_model": "gpt-5"}
    bot._scan_count = 10
    bot._last_ai_error = ""
    bot._last_preflight = {}
    bot._last_analysis = {}
    bot._last_raw_output_preview = ""
    bot._last_ai_call_meta = {}
    bot._last_collected_data = {}
    bot._last_position_reviews = []
    bot._last_preflight_board_key = ""
    bot._debate_log = []
    bot._trade_log = []
    bot._realized_pnl = 0
    bot._positions = []
    return bot


def _started_recorder(db: DB, scan_count: int = 10) -> AuditRecorder:
    recorder = AuditRecorder(db, "mirinae", "strat-001", scan_count, "paper")
    recorder.start()
    return recorder


def _prepare_one_scan_loop_bot(bot: MirinaeBot):
    bot._scan_count = 0
    bot._running = True
    bot._daily_limit = MagicMock()
    bot._daily_limit._count = 0
    bot._daily_limit.max_trades = 5
    bot._market_guard = MagicMock()
    bot._market_guard.is_market_open.return_value = True
    bot._reload_params_if_needed = MagicMock()
    bot._reconcile_positions_with_trades = MagicMock()
    bot._check_positions = MagicMock(return_value=[])
    bot._should_skip_ai = MagicMock(return_value="")
    bot._build_codex_prompt = MagicMock(return_value="prompt")
    bot._invoke_codex = MagicMock(return_value="raw output")
    bot._save_state = MagicMock()
    bot._cleanup_old_decisions = MagicMock()
    bot._after_scan = MagicMock()
    bot._validate_ai_performance = MagicMock()
    bot._publish_event = MagicMock()
    bot._post_to_board = MagicMock()


def _sleep_once(bot: MirinaeBot):
    async def stop_after_scan(_seconds):
        bot._running = False

    return stop_after_scan


def test_mirinae_records_ai_audit_from_prompt_response_and_decision(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    recorder = _started_recorder(db)

    decision = {
        "decision": "BUY",
        "confidence": 0.81,
        "code": "102780",
        "criteria": {"market_trend": {"score": 8, "detail": "상승"}},
        "bull_summary": "강세",
        "bear_summary": "약세",
        "trader_reason": "최종 판단",
    }
    bot._audit_record_ai(
        recorder,
        prompt="prompt text",
        output='{"decision":"BUY","confidence":0.81}',
        ai_decision=decision,
        provider="codex",
        model="gpt-5",
        tokens=100,
        latency_ms=20,
    )

    row = db.get_ai_audit(recorder.run_id)
    assert row["prompt"] == "prompt text"
    assert row["raw_response"].startswith("{")
    assert row["provider"] == "codex"
    assert row["model"] == "gpt-5"
    assert row["tokens"] == 100
    assert row["latency_ms"] == 20
    assert row["confidence"] == 0.81
    assert json.loads(row["criteria_json"]) == decision["criteria"]
    assert row["bull_summary"] == "강세"
    assert row["bear_summary"] == "약세"
    assert row["trader_reason"] == "최종 판단"
    db.close()


def test_mirinae_records_blocking_preflight_when_no_buyable_candidates(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    recorder = _started_recorder(db)

    preflight = {
        "reason": "실제 주문 가능 후보 없음",
        "buyable_candidates": [],
        "blocked_candidates": [{"code": "102780", "reason": "중복 진입 차단"}],
    }
    bot._audit_record_preflight(recorder, preflight, stock_code="102780")

    checks = db.get_preflight_checks(recorder.run_id)
    assert len(checks) == 1
    assert checks[0]["check_name"] == "buyable_candidates"
    assert checks[0]["stock_code"] == "102780"
    assert checks[0]["passed"] == 0
    assert checks[0]["severity"] == "block"
    assert checks[0]["message"] == "실제 주문 가능 후보 없음"
    assert json.loads(checks[0]["payload_json"]) == preflight
    db.close()


def test_mirinae_records_structured_evidence_snapshots(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    recorder = _started_recorder(db)
    bot._last_preflight = {
        "reason": "OK",
        "buyable_candidates": [{"code": "102780", "qty": 2, "price": 10000}],
    }
    data = {
        "etf_prices": [{"code": "102780", "price": 10000}],
        "holdings_account": {"cash": 50000},
        "news": [{"title": "반도체 강세"}],
        "board_context": {"opinions": [{"bot_id": "other", "code": "102780"}]},
        "etf_signals": {"102780": {"score": 7}},
    }

    bot._audit_record_evidence(recorder, "formatted context", data)

    snapshots = db.get_evidence_snapshots(recorder.run_id)
    by_source = {row["source"]: row for row in snapshots}
    assert "mirinae_context" in by_source
    assert "prices" in by_source
    assert json.loads(by_source["prices"]["payload_json"]) == data["etf_prices"]
    assert "balance" in by_source
    assert json.loads(by_source["balance"]["payload_json"]) == data["holdings_account"]
    assert "news" in by_source
    assert "board_context" in by_source
    assert "signals" in by_source
    assert "preflight_input" in by_source
    db.close()


def test_mirinae_finish_run_maps_status_and_persists_links(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()

    blocked = _started_recorder(db, 1)
    bot._audit_finish_run(
        blocked,
        action="blocked_buy",
        stock_code="102780",
        decision_id=5,
        result={"success": False, "reason": "매수 가능 수량 없음"},
    )
    blocked_run = db.get_decision_run(blocked.run_id)
    assert blocked_run["status"] == "blocked"
    assert blocked_run["action"] == "blocked_buy"
    assert blocked_run["stock_code"] == "102780"
    assert blocked_run["decision_id"] == 5
    assert blocked_run["error"] == "매수 가능 수량 없음"

    ordered = _started_recorder(db, 2)
    bot._audit_finish_run(
        ordered,
        action="buy",
        stock_code="102780",
        decision_id=6,
        result={"success": True, "trade_id": 42},
    )
    ordered_run = db.get_decision_run(ordered.run_id)
    assert ordered_run["status"] == "ordered"
    assert ordered_run["trade_id"] == 42

    completed = _started_recorder(db, 3)
    bot._audit_finish_run(
        completed,
        action="hold",
        stock_code="",
        decision_id=7,
        result={"success": True, "reason": "관망"},
    )
    completed_run = db.get_decision_run(completed.run_id)
    assert completed_run["status"] == "completed"
    assert completed_run["decision_id"] == 7

    failed = _started_recorder(db, 4)
    bot._audit_finish_run(
        failed,
        action="buy",
        stock_code="102780",
        decision_id=8,
        result={"success": True, "trade_id": 43},
        error="boom",
    )
    failed_run = db.get_decision_run(failed.run_id)
    assert failed_run["status"] == "failed"
    assert failed_run["trade_id"] == 43
    assert failed_run["error"] == "boom"
    db.close()


def test_mirinae_records_open_outcome_for_successful_buy(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    recorder = _started_recorder(db)
    bot._positions = [{"code": "102780", "qty": 3, "price": 10000}]

    bot._audit_record_outcome(
        recorder,
        action="buy",
        ai_decision={"decision": "BUY", "code": "102780"},
        result={"success": True, "trade_id": 31, "qty": 3, "price": 10000},
        decision_id=17,
        stock_code="102780",
    )

    row = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", (recorder.run_id,)
    ).fetchone()
    assert row["decision_id"] == 17
    assert row["buy_trade_id"] == 31
    assert row["bot_id"] == "mirinae"
    assert row["stock_code"] == "102780"
    assert row["entry_price"] == 10000
    assert row["quantity"] == 3
    assert row["outcome_status"] == "open"
    assert bot._positions[0]["audit_outcome_run_id"] == recorder.run_id
    db.close()


def test_mirinae_add_buy_full_sell_closes_all_tracked_open_outcomes(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    bot._positions = [{"code": "102780", "qty": 2, "price": 10000}]
    first_buy = _started_recorder(db, 1)
    add_buy = _started_recorder(db, 2)
    sell_recorder = _started_recorder(db, 3)

    bot._audit_record_outcome(
        first_buy,
        action="buy",
        ai_decision={"decision": "BUY", "code": "102780"},
        result={"success": True, "trade_id": 31, "qty": 2, "price": 10000},
        decision_id=17,
        stock_code="102780",
    )
    bot._positions[0]["qty"] = 5
    bot._positions[0]["price"] = 10600
    bot._audit_record_outcome(
        add_buy,
        action="add_buy",
        ai_decision={"decision": "BUY", "code": "102780"},
        result={"success": True, "trade_id": 32, "qty": 3, "price": 11000},
        decision_id=18,
        stock_code="102780",
    )

    bot._audit_record_outcome(
        sell_recorder,
        action="sell",
        ai_decision={"decision": "SELL", "code": "102780"},
        result={
            "success": True,
            "trade_id": 33,
            "sell_price": 12000,
            "realized_pnl": 7000,
            "sell_basis": {"entry_price": 10600, "quantity": 5},
        },
        decision_id=19,
        stock_code="102780",
    )

    rows = [
        dict(row) for row in db._conn.execute(
            "SELECT * FROM decision_outcomes WHERE bot_id=? AND stock_code=? ORDER BY buy_trade_id",
            ("mirinae", "102780"),
        ).fetchall()
    ]
    assert [row["run_id"] for row in rows] == [first_buy.run_id, add_buy.run_id]
    assert [row["outcome_status"] for row in rows] == ["closed", "closed"]
    assert [row["sell_trade_id"] for row in rows] == [33, 33]
    assert [row["realized_pnl"] for row in rows] == [2800, 4200]
    assert sum(row["realized_pnl"] for row in rows) == 7000
    open_rows = db._conn.execute(
        "SELECT COUNT(*) AS count FROM decision_outcomes "
        "WHERE bot_id=? AND stock_code=? AND outcome_status='open'",
        ("mirinae", "102780"),
    ).fetchone()
    assert open_rows["count"] == 0
    assert bot._positions[0]["audit_outcome_runs"] == []
    db.close()


def test_mirinae_clamped_ai_sell_closes_only_executed_lot_quantity(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    hold_timer = MagicMock()
    hold_timer.can_sell.return_value = True
    bot._daily_limit = MagicMock()
    lot1_run_id = "lot-1-run"
    lot2_run_id = "lot-2-run"
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 5,
            "price": 10600,
            "target_price": 12000,
            "stop_loss": 10000,
            "hold_timer": hold_timer,
            "trailing_stop": MagicMock(),
            "audit_outcome_run_id": lot2_run_id,
            "audit_buy_trade_id": 32,
            "audit_outcome_runs": [
                {
                    "run_id": lot1_run_id,
                    "buy_trade_id": 31,
                    "qty": 2,
                    "remaining_qty": 2,
                    "entry_price": 10000,
                    "entry_time": "2026-06-03T09:00:00+09:00",
                },
                {
                    "run_id": lot2_run_id,
                    "buy_trade_id": 32,
                    "qty": 3,
                    "remaining_qty": 3,
                    "entry_price": 11000,
                    "entry_time": "2026-06-03T09:10:00+09:00",
                },
            ],
        }
    ]
    db.upsert_decision_outcome(
        lot1_run_id,
        buy_trade_id=31,
        bot_id="mirinae",
        stock_code="102780",
        entry_price=10000,
        quantity=2,
        outcome_status="open",
    )
    db.upsert_decision_outcome(
        lot2_run_id,
        buy_trade_id=32,
        bot_id="mirinae",
        stock_code="102780",
        entry_price=11000,
        quantity=3,
        outcome_status="open",
    )
    bot.sell = MagicMock(return_value={
        "success": True,
        "trade_id": 33,
        "sell_price": 12000,
        "realized_pnl": 3000,
        "sell_basis": {"entry_price": 10600, "quantity": 5},
        "clamped_qty": 3,
    })
    sell_recorder = _started_recorder(db, 3)
    decision = {
        "decision": "SELL",
        "code": "102780",
        "sell_qty": 5,
        "price": 12000,
        "trader_reason": "전량 매도 요청",
    }

    result = bot._execute_decision(decision)
    bot._audit_record_outcome(
        sell_recorder,
        action=result["action"],
        ai_decision=decision,
        result=result,
        decision_id=19,
        stock_code="102780",
    )

    lot1 = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", (lot1_run_id,)
    ).fetchone()
    lot2_open = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", (lot2_run_id,)
    ).fetchone()
    lot2_close = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", (f"{lot2_run_id}:sell:33",)
    ).fetchone()

    assert result["action"] == "partial_sell"
    assert bot._positions[0]["qty"] == 2
    assert bot._positions[0]["audit_outcome_runs"] == [
        {
            "run_id": lot2_run_id,
            "buy_trade_id": 32,
            "qty": 3,
            "remaining_qty": 2,
            "entry_price": 11000.0,
            "entry_time": "2026-06-03T09:10:00+09:00",
        }
    ]
    assert lot1["outcome_status"] == "closed"
    assert lot1["quantity"] == 2
    assert lot1["realized_pnl"] == 2000
    assert lot2_open["outcome_status"] == "open"
    assert lot2_open["quantity"] == 2
    assert lot2_close["outcome_status"] == "closed"
    assert lot2_close["quantity"] == 1
    assert lot2_close["realized_pnl"] == 1000
    assert lot1["realized_pnl"] + lot2_close["realized_pnl"] == 3000
    db.close()


def test_mirinae_clamped_position_exit_builds_closes_from_executed_quantity():
    bot = _bare_bot()
    hold_timer = MagicMock()
    hold_timer.can_sell.return_value = True
    bot._positions = [
        {
            "code": "102780",
            "name": "KODEX 삼성그룹",
            "qty": 5,
            "price": 10600,
            "target_price": 12000,
            "stop_loss": 10000,
            "hold_timer": hold_timer,
            "trailing_stop": MagicMock(),
            "audit_outcome_run_id": "lot-2-run",
            "audit_buy_trade_id": 32,
            "audit_outcome_runs": [
                {
                    "run_id": "lot-1-run",
                    "buy_trade_id": 31,
                    "qty": 2,
                    "remaining_qty": 2,
                    "entry_price": 10000,
                    "entry_time": "2026-06-03T09:00:00+09:00",
                },
                {
                    "run_id": "lot-2-run",
                    "buy_trade_id": 32,
                    "qty": 3,
                    "remaining_qty": 3,
                    "entry_price": 11000,
                    "entry_time": "2026-06-03T09:10:00+09:00",
                },
            ],
        }
    ]
    bot.sell = MagicMock(return_value={
        "success": True,
        "trade_id": 33,
        "sell_price": 12000,
        "realized_pnl": 3000,
        "sell_basis": {"entry_price": 10600, "quantity": 5},
        "clamped_qty": 3,
    })

    result = bot._execute_position_exit({
        "action": "sell",
        "code": "102780",
        "qty": 5,
        "current_price": 12000,
        "reason": "자동 청산",
    })

    assert result["action"] == "partial_sell"
    assert bot._positions[0]["qty"] == 2
    assert [
        (close["run_id"], close["closed_qty"], close["remaining_after"])
        for close in result["audit_outcome_closes"]
    ] == [
        ("lot-1-run", 2, 0),
        ("lot-2-run", 1, 2),
    ]


def test_mirinae_records_closed_kpi_ready_outcome_for_successful_sell(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    buy_recorder = _started_recorder(db, 1)
    sell_recorder = _started_recorder(db, 2)
    db.upsert_decision_outcome(
        buy_recorder.run_id,
        decision_id=11,
        buy_trade_id=21,
        bot_id="mirinae",
        stock_code="102780",
        entry_time="2026-06-03T09:00:00+09:00",
        entry_price=10000,
        quantity=3,
        outcome_status="open",
    )

    bot._audit_record_outcome(
        sell_recorder,
        action="sell",
        ai_decision={"decision": "SELL", "code": "102780"},
        result={
            "success": True,
            "trade_id": 32,
            "sell_price": 10300,
            "realized_pnl": 900,
            "sell_basis": {
                "entry_price": 10000,
                "quantity": 3,
                "outcome_run_id": buy_recorder.run_id,
                "entry_time": "2026-06-03T09:00:00+09:00",
            },
        },
        decision_id=18,
        stock_code="102780",
    )

    row = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", (buy_recorder.run_id,)
    ).fetchone()
    assert row["sell_trade_id"] == 32
    assert row["exit_price"] == 10300
    assert row["realized_pnl"] == 900
    assert row["return_pct"] == 3.0
    assert row["outcome_status"] == "closed"

    kpi = db.get_audit_kpi(bot_id="mirinae")
    assert kpi["closed"] == 1
    assert kpi["total_pnl"] == 900
    db.close()


def test_mirinae_sell_outcome_falls_back_to_sell_run_when_buy_run_missing(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    recorder = _started_recorder(db)

    bot._audit_record_outcome(
        recorder,
        action="partial_sell",
        ai_decision={"decision": "SELL", "code": "102780"},
        result={
            "success": True,
            "trade_id": 33,
            "sell_price": 9800,
            "realized_pnl": -400,
            "sell_basis": {"entry_price": 10000, "quantity": 2},
        },
        decision_id=19,
        stock_code="102780",
    )

    row = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", (recorder.run_id,)
    ).fetchone()
    assert row["sell_trade_id"] == 33
    assert row["stock_code"] == "102780"
    assert row["quantity"] == 2
    assert row["realized_pnl"] == -400
    assert row["return_pct"] == -2.0
    assert row["outcome_status"] == "closed"
    assert db.get_audit_kpi(bot_id="mirinae")["closed"] == 1
    db.close()


def test_mirinae_loop_skip_does_not_write_ai_audit(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    bot._scan_count = 0
    bot._running = True
    bot._daily_limit = MagicMock()
    bot._daily_limit._count = 0
    bot._daily_limit.max_trades = 5
    bot._market_guard = MagicMock()
    bot._market_guard.is_market_open.return_value = True
    bot._reload_params_if_needed = MagicMock()
    bot._reconcile_positions_with_trades = MagicMock()
    bot._check_positions = MagicMock(return_value=[])
    bot._should_skip_ai = MagicMock(return_value="주문 비활성화 상태 + 보유 없음")
    bot._collect_data = MagicMock()
    bot._invoke_codex = MagicMock()
    bot._save_state = MagicMock()
    bot._cleanup_old_decisions = MagicMock()
    bot._after_scan = MagicMock()
    bot._validate_ai_performance = MagicMock()
    bot._publish_event = MagicMock()

    async def stop_after_scan(_seconds):
        bot._running = False

    with (
        patch("src.db.get_db", return_value=db),
        patch("src.bots.mirinae.write_bot_log"),
        patch("src.bots.mirinae.asyncio.sleep", side_effect=stop_after_scan),
        patch("src.bots.mirinae.kst_now", return_value=datetime(2026, 6, 3, 10, 0, 0)),
    ):
        import asyncio

        asyncio.run(bot._loop())

    bot._invoke_codex.assert_not_called()
    bot._collect_data.assert_not_called()

    runs = db._conn.execute("SELECT * FROM decision_runs").fetchall()
    assert len(runs) == 1
    run = dict(runs[0])
    assert run["status"] == "completed"
    assert run["action"] == "skip"
    assert run["error"] == "주문 비활성화 상태 + 보유 없음"
    assert db.get_ai_audit(run["run_id"]) == {}
    db.close()


def test_mirinae_loop_successful_buy_writes_open_outcome(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    _prepare_one_scan_loop_bot(bot)

    def collect_data():
        bot._last_collected_data = {"etf_prices": [{"code": "102780", "price": 10000}]}
        return "context"

    bot._collect_data = MagicMock(side_effect=collect_data)
    bot._parse_decision = MagicMock(return_value={
        "decision": "BUY",
        "confidence": 0.9,
        "code": "102780",
        "trader_reason": "강세",
    })
    bot._execute_decision = MagicMock(return_value={
        "success": True,
        "action": "buy",
        "trade_id": 44,
        "qty": 2,
        "price": 10000,
        "code": "102780",
    })

    with (
        patch("src.db.get_db", return_value=db),
        patch("src.bots.mirinae.write_bot_log"),
        patch("src.bots.mirinae.asyncio.sleep", side_effect=_sleep_once(bot)),
        patch("src.bots.mirinae.kst_now", return_value=datetime(2026, 6, 3, 10, 0, 0)),
    ):
        asyncio.run(bot._loop())

    row = db._conn.execute("SELECT * FROM decision_outcomes").fetchone()
    assert row["buy_trade_id"] == 44
    assert row["stock_code"] == "102780"
    assert row["entry_price"] == 10000
    assert row["quantity"] == 2
    assert row["outcome_status"] == "open"
    assert db.get_audit_kpi(bot_id="mirinae")["closed"] == 0
    db.close()


def test_mirinae_loop_saves_state_after_successful_buy_outcome_linkage(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    _prepare_one_scan_loop_bot(bot)
    saved_positions = []

    def save_state_snapshot():
        saved_positions.append([dict(pos) for pos in bot._positions])

    bot._save_state = MagicMock(side_effect=save_state_snapshot)
    bot._collect_data = MagicMock(return_value="context")
    bot._parse_decision = MagicMock(return_value={
        "decision": "BUY",
        "confidence": 0.9,
        "code": "102780",
        "trader_reason": "강세",
    })

    def execute_buy(_decision):
        bot._positions.append({"code": "102780", "qty": 2, "price": 10000})
        return {
            "success": True,
            "action": "buy",
            "trade_id": 44,
            "qty": 2,
            "price": 10000,
            "code": "102780",
        }

    bot._execute_decision = MagicMock(side_effect=execute_buy)

    with (
        patch("src.db.get_db", return_value=db),
        patch("src.bots.mirinae.write_bot_log"),
        patch("src.bots.mirinae.asyncio.sleep", side_effect=_sleep_once(bot)),
        patch("src.bots.mirinae.kst_now", return_value=datetime(2026, 6, 3, 10, 0, 0)),
    ):
        asyncio.run(bot._loop())

    assert saved_positions
    assert saved_positions[-1][0]["audit_outcome_run_id"]
    assert saved_positions[-1][0]["audit_outcome_runs"][0]["run_id"]
    db.close()


def test_mirinae_loop_successful_sell_writes_closed_kpi_outcome(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = _bare_bot()
    _prepare_one_scan_loop_bot(bot)
    buy_run_id = "buy-run"
    db.upsert_decision_outcome(
        buy_run_id,
        bot_id="mirinae",
        stock_code="102780",
        entry_time="2026-06-03T09:00:00+09:00",
        entry_price=10000,
        quantity=2,
        outcome_status="open",
    )

    bot._collect_data = MagicMock(return_value="context")
    bot._parse_decision = MagicMock(return_value={
        "decision": "SELL",
        "confidence": 0.88,
        "code": "102780",
        "trader_reason": "목표 도달",
    })
    bot._execute_decision = MagicMock(return_value={
        "success": True,
        "action": "sell",
        "trade_id": 45,
        "sell_price": 10500,
        "realized_pnl": 1000,
        "sell_basis": {
            "entry_price": 10000,
            "quantity": 2,
            "outcome_run_id": buy_run_id,
            "entry_time": "2026-06-03T09:00:00+09:00",
        },
    })

    with (
        patch("src.db.get_db", return_value=db),
        patch("src.bots.mirinae.write_bot_log"),
        patch("src.bots.mirinae.asyncio.sleep", side_effect=_sleep_once(bot)),
        patch("src.bots.mirinae.kst_now", return_value=datetime(2026, 6, 3, 10, 30, 0)),
    ):
        asyncio.run(bot._loop())

    row = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", (buy_run_id,)
    ).fetchone()
    assert row["sell_trade_id"] == 45
    assert row["exit_price"] == 10500
    assert row["realized_pnl"] == 1000
    assert row["return_pct"] == 5.0
    assert row["outcome_status"] == "closed"
    kpi = db.get_audit_kpi(bot_id="mirinae")
    assert kpi["closed"] == 1
    assert kpi["total_pnl"] == 1000
    db.close()


def test_mirinae_link_decision_trade_requires_explicit_trade_id():
    bot = _bare_bot()
    db = MagicMock()
    db.get_trades.return_value = [{"id": 99}]

    with patch("src.db.get_db", return_value=db):
        linked = bot._link_decision_trade_from_result(12, {"success": True, "action": "buy"})

    assert linked == 0
    db.get_trades.assert_not_called()
    db.link_decision_trade.assert_not_called()
