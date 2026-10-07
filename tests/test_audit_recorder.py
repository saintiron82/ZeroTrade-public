"""Bot audit recorder tests."""

import json
import re
from unittest.mock import Mock

from src.bots.audit import AuditRecorder, make_run_id
from src.db import DB


def test_make_run_id_shape_and_suffix_length():
    run_id = make_run_id("mirinae-4", "strat-001", "184")

    assert re.fullmatch(
        r"mirinae-4:strat-001:\d{8}T\d{6}:184:[0-9a-f]{6}",
        run_id,
    )
    assert len(run_id.rsplit(":", 1)[1]) == 6


def test_make_run_id_uses_no_pipe_when_pipe_id_missing():
    run_id = make_run_id("evolver", None, 7)

    assert re.fullmatch(
        r"evolver:no-pipe:\d{8}T\d{6}:7:[0-9a-f]{6}",
        run_id,
    )


def test_audit_recorder_persists_run_evidence_ai_preflight_and_finish(tmp_path):
    db = DB(tmp_path / "audit.db")
    recorder = AuditRecorder(
        db,
        bot_id="mirinae-4",
        pipe_id="strat-001",
        scan_count=184,
        mode="PAPER",
    )

    run_row_id = recorder.start()
    evidence_id = recorder.evidence(
        "price",
        stock_code="091160",
        payload={"price": 101000},
        summary="091160 101000",
    )
    ai_id = recorder.ai_call(
        provider="codex",
        model="gpt-5",
        prompt="prompt text",
        raw_response='{"decision":"BUY"}',
        parsed={
            "decision": "BUY",
            "confidence": 0.85,
            "criteria": {"momentum": 9},
            "bull_summary": "강세",
            "bear_summary": "약세",
            "trader_reason": "기대값 우위",
        },
        parse_success=True,
        tokens=321,
        latency_ms=1200,
    )
    check_id = recorder.preflight(
        "cash_available",
        stock_code="091160",
        passed=True,
        severity="info",
        message="주문 가능",
        payload={"cash": 500000},
    )
    recorder.finish(
        status="completed",
        action="BUY",
        stock_code="091160",
        decision_id=77,
        trade_id=88,
    )

    run = db.get_decision_run(recorder.run_id)
    evidence = db.get_evidence_snapshots(recorder.run_id)
    audit = db.get_ai_audit(recorder.run_id)
    checks = db.get_preflight_checks(recorder.run_id)

    assert run_row_id == run["id"]
    assert run["bot_id"] == "mirinae-4"
    assert run["pipe_id"] == "strat-001"
    assert run["scan_count"] == 184
    assert run["mode"] == "PAPER"
    assert run["status"] == "completed"
    assert run["action"] == "BUY"
    assert run["stock_code"] == "091160"
    assert run["decision_id"] == 77
    assert run["trade_id"] == 88
    assert evidence[0]["id"] == evidence_id
    assert json.loads(evidence[0]["payload_json"]) == {"price": 101000}
    assert audit["id"] == ai_id
    assert audit["confidence"] == 0.85
    assert json.loads(audit["criteria_json"]) == {"momentum": 9}
    assert audit["bull_summary"] == "강세"
    assert audit["bear_summary"] == "약세"
    assert audit["trader_reason"] == "기대값 우위"
    assert checks[0]["id"] == check_id
    assert checks[0]["check_name"] == "cash_available"
    assert json.loads(checks[0]["payload_json"]) == {"cash": 500000}

    db.close()


def test_audit_recorder_accepts_plan_positional_evidence_and_preflight(tmp_path):
    db = DB(tmp_path / "audit.db")
    recorder = AuditRecorder(
        db,
        bot_id="mirinae-4",
        pipe_id="strat-001",
        scan_count=184,
        mode="PAPER",
    )
    recorder.start()

    recorder.evidence("price", {"price": 33690}, stock_code="102780")
    recorder.preflight("cash", True, "info", "cash ok", {"cash": 100000}, "102780")

    evidence = db.get_evidence_snapshots(recorder.run_id)
    checks = db.get_preflight_checks(recorder.run_id)

    assert evidence[0]["source"] == "price"
    assert evidence[0]["stock_code"] == "102780"
    assert json.loads(evidence[0]["payload_json"]) == {"price": 33690}
    assert checks[0]["check_name"] == "cash"
    assert checks[0]["stock_code"] == "102780"
    assert checks[0]["passed"] == 1
    assert checks[0]["severity"] == "info"
    assert checks[0]["message"] == "cash ok"
    assert json.loads(checks[0]["payload_json"]) == {"cash": 100000}

    db.close()


def test_audit_recorder_normalizes_none_scan_count_to_zero(tmp_path):
    db = DB(tmp_path / "audit.db")
    recorder = AuditRecorder(
        db,
        bot_id="mirinae-4",
        pipe_id="strat-001",
        scan_count=None,
        mode="PAPER",
    )

    assert recorder.scan_count == 0
    recorder.start()

    run = db.get_decision_run(recorder.run_id)
    assert run["scan_count"] == 0

    db.close()


def test_audit_recorder_swallows_db_failures(caplog):
    failing_db = Mock()
    failing_db.start_decision_run.side_effect = RuntimeError("db down")
    failing_db.log_evidence_snapshot.side_effect = RuntimeError("db down")
    failing_db.log_ai_audit.side_effect = RuntimeError("db down")
    failing_db.log_preflight_check.side_effect = RuntimeError("db down")
    failing_db.update_decision_run.side_effect = RuntimeError("db down")
    recorder = AuditRecorder(
        failing_db,
        bot_id="mirinae-4",
        pipe_id="strat-001",
        scan_count=184,
        mode="PAPER",
    )

    assert recorder.start() is None
    assert recorder.evidence("price") is None
    assert recorder.ai_call(parsed={"confidence": 0.4}) is None
    assert recorder.preflight("cash_available") is None
    assert recorder.finish(status="failed", error="blocked") is None

    assert len(caplog.records) == 5
    assert all(record.levelname == "WARNING" for record in caplog.records)
