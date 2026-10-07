"""Decision audit DB migration and helper tests."""

import json

from src.db import DB


AUDIT_TABLES = {
    "decision_runs",
    "evidence_snapshots",
    "ai_audit_logs",
    "preflight_checks",
    "decision_outcomes",
}


def table_names(db: DB) -> set[str]:
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


def index_names(db: DB) -> set[str]:
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {row["name"] for row in rows}


def column_names(db: DB, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


class RecordingConnection:
    def __init__(self, conn):
        self.conn = conn
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()


def test_decision_audit_tables_are_created_idempotently(tmp_path):
    db = DB(tmp_path / "audit.db")
    db._create_tables()
    db._create_tables()

    assert AUDIT_TABLES <= table_names(db)
    assert {
        "idx_decision_runs_run_id",
        "idx_decision_runs_bot_ts",
        "idx_decision_runs_stock_ts",
        "idx_evidence_snapshots_run_id",
        "idx_evidence_snapshots_source",
        "idx_ai_audit_logs_run_id",
        "idx_ai_audit_logs_bot_ts",
        "idx_preflight_checks_run_id",
        "idx_preflight_checks_name",
        "idx_decision_outcomes_run_id",
        "idx_decision_outcomes_code",
        "idx_decision_outcomes_bot_ts",
    } <= index_names(db)
    assert "payload_json" in column_names(db, "evidence_snapshots")
    assert "payload" not in column_names(db, "evidence_snapshots")
    assert {"parsed_json", "criteria_json"} <= column_names(db, "ai_audit_logs")
    assert {"parsed", "criteria"}.isdisjoint(column_names(db, "ai_audit_logs"))
    assert "payload_json" in column_names(db, "preflight_checks")
    assert "payload" not in column_names(db, "preflight_checks")

    db.close()


def test_decision_run_lifecycle_and_evidence(tmp_path):
    db = DB(tmp_path / "audit.db")

    row_id = db.start_decision_run(
        "run-1",
        pipe_id="pipe-a",
        bot_id="mirinae",
        scan_count=3,
        mode="PAPER",
    )
    db.update_decision_run(
        "run-1",
        status="completed",
        action="BUY",
        stock_code="091160",
        decision_id=77,
        trade_id=88,
        error="",
        unsupported_field="ignored",
    )

    run = db.get_decision_run("run-1")
    assert run["id"] == row_id
    assert run["run_id"] == "run-1"
    assert run["pipe_id"] == "pipe-a"
    assert run["bot_id"] == "mirinae"
    assert run["scan_count"] == 3
    assert run["mode"] == "PAPER"
    assert run["status"] == "completed"
    assert run["action"] == "BUY"
    assert run["stock_code"] == "091160"
    assert run["decision_id"] == 77
    assert run["trade_id"] == 88
    assert "unsupported_field" not in run

    evidence_id = db.log_evidence_snapshot(
        "run-1",
        "semantic_news",
        stock_code="091160",
        payload={"title": "반도체 회복", "values": [1, 2]},
        summary="뉴스 근거",
    )
    failure_id = db.log_evidence_snapshot(
        "run-1",
        "orderbook",
        success=False,
        error="timeout",
    )

    snapshots = db.get_evidence_snapshots("run-1")
    assert [s["id"] for s in snapshots] == [evidence_id, failure_id]
    assert json.loads(snapshots[0]["payload_json"]) == {"title": "반도체 회복", "values": [1, 2]}
    assert "payload" not in snapshots[0]
    assert snapshots[0]["summary"] == "뉴스 근거"
    assert snapshots[1]["success"] == 0
    assert snapshots[1]["error"] == "timeout"

    db.close()


def test_ai_audit_preflight_and_outcome_helpers(tmp_path):
    db = DB(tmp_path / "audit.db")
    db.start_decision_run("run-2", bot_id="mirinae")

    ai_id = db.log_ai_audit(
        "run-2",
        bot_id="mirinae",
        provider="openai",
        model="gpt-test",
        prompt="분석해줘",
        raw_response='{"decision":"BUY"}',
        parsed={"decision": "BUY", "note": "한글"},
        parse_success=True,
        confidence=0.82,
        criteria={"momentum": 8},
        bull_summary="수급 개선",
        bear_summary="변동성",
        trader_reason="기대값 우위",
        tokens=321,
        latency_ms=1200,
    )
    audit = db.get_ai_audit("run-2")
    assert audit["id"] == ai_id
    assert audit["parse_success"] == 1
    assert audit["confidence"] == 0.82
    assert json.loads(audit["parsed_json"]) == {"decision": "BUY", "note": "한글"}
    assert json.loads(audit["criteria_json"]) == {"momentum": 8}
    assert "parsed" not in audit
    assert "criteria" not in audit

    check_id = db.log_preflight_check(
        "run-2",
        "risk_limit",
        stock_code="091160",
        passed=False,
        severity="warn",
        message="비중 초과",
        payload={"limit": 0.2},
    )
    checks = db.get_preflight_checks("run-2")
    assert checks[0]["id"] == check_id
    assert checks[0]["passed"] == 0
    assert checks[0]["severity"] == "warn"
    assert json.loads(checks[0]["payload_json"]) == {"limit": 0.2}
    assert "payload" not in checks[0]

    outcome_id = db.upsert_decision_outcome(
        "run-2",
        decision_id=10,
        buy_trade_id=20,
        sell_trade_id=21,
        bot_id="mirinae",
        stock_code="091160",
        entry_time="2026-06-03T09:10:00+09:00",
        exit_time="2026-06-03T10:10:00+09:00",
        hold_minutes=60,
        entry_price=10000,
        exit_price=10300,
        quantity=5,
        realized_pnl=1500,
        return_pct=3.0,
        max_favorable_pct=4.2,
        max_adverse_pct=-0.8,
        outcome_status="closed",
        notes="목표가 도달",
    )
    row = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", ("run-2",)
    ).fetchone()
    assert row["id"] == outcome_id
    assert row["stock_code"] == "091160"
    assert row["realized_pnl"] == 1500
    assert row["return_pct"] == 3.0
    assert row["outcome_status"] == "closed"

    db.close()


def test_upsert_decision_outcome_updates_existing_row(tmp_path):
    db = DB(tmp_path / "audit.db")
    db.start_decision_run("run-3", bot_id="mirinae")

    first_id = db.upsert_decision_outcome(
        "run-3",
        bot_id="mirinae",
        stock_code="091160",
        entry_price=10000,
        outcome_status="open",
    )
    second_id = db.upsert_decision_outcome(
        "run-3",
        bot_id="mirinae",
        stock_code="091160",
        entry_price=10000,
        exit_price=9800,
        realized_pnl=-200,
        return_pct=-2.0,
        outcome_status="closed",
        notes="손절",
    )

    rows = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", ("run-3",)
    ).fetchall()
    assert first_id == second_id
    assert len(rows) == 1
    assert rows[0]["exit_price"] == 9800
    assert rows[0]["realized_pnl"] == -200
    assert rows[0]["outcome_status"] == "closed"
    assert rows[0]["notes"] == "손절"

    db.close()


def test_start_decision_run_is_idempotent_after_terminal_status(tmp_path):
    db = DB(tmp_path / "audit.db")

    first_id = db.start_decision_run(
        "run-4",
        pipe_id="pipe-a",
        bot_id="mirinae",
        scan_count=5,
        mode="PAPER",
    )
    db.update_decision_run(
        "run-4",
        status="completed",
        action="BUY",
        stock_code="091160",
        decision_id=101,
        trade_id=202,
        error="",
        mode="PAPER",
    )

    second_id = db.start_decision_run(
        "run-4",
        pipe_id="pipe-b",
        bot_id="other",
        scan_count=0,
        mode="LIVE",
        status="started",
    )

    run = db.get_decision_run("run-4")
    assert second_id == first_id
    assert run["pipe_id"] == "pipe-a"
    assert run["bot_id"] == "mirinae"
    assert run["scan_count"] == 5
    assert run["mode"] == "PAPER"
    assert run["status"] == "completed"
    assert run["action"] == "BUY"
    assert run["stock_code"] == "091160"
    assert run["decision_id"] == 101
    assert run["trade_id"] == 202
    assert run["error"] == ""

    db.close()


def test_upsert_decision_outcome_partial_update_preserves_entry_fields(tmp_path):
    db = DB(tmp_path / "audit.db")
    db.start_decision_run("run-5", bot_id="mirinae")

    first_id = db.upsert_decision_outcome(
        "run-5",
        decision_id=11,
        buy_trade_id=22,
        bot_id="mirinae",
        stock_code="091160",
        entry_time="2026-06-03T09:10:00+09:00",
        entry_price=10000,
        quantity=5,
        outcome_status="open",
    )
    second_id = db.upsert_decision_outcome(
        "run-5",
        sell_trade_id=33,
        exit_time="2026-06-03T10:10:00+09:00",
        hold_minutes=60,
        exit_price=10300,
        realized_pnl=1500,
        return_pct=3.0,
        outcome_status="closed",
        notes="목표가 도달",
    )

    row = db._conn.execute(
        "SELECT * FROM decision_outcomes WHERE run_id=?", ("run-5",)
    ).fetchone()
    assert second_id == first_id
    assert row["decision_id"] == 11
    assert row["buy_trade_id"] == 22
    assert row["bot_id"] == "mirinae"
    assert row["stock_code"] == "091160"
    assert row["entry_time"] == "2026-06-03T09:10:00+09:00"
    assert row["entry_price"] == 10000
    assert row["quantity"] == 5
    assert row["sell_trade_id"] == 33
    assert row["exit_time"] == "2026-06-03T10:10:00+09:00"
    assert row["hold_minutes"] == 60
    assert row["exit_price"] == 10300
    assert row["realized_pnl"] == 1500
    assert row["return_pct"] == 3.0
    assert row["outcome_status"] == "closed"
    assert row["notes"] == "목표가 도달"

    db.close()


def test_upsert_decision_outcome_uses_on_conflict_clause(tmp_path):
    db = DB(tmp_path / "audit.db")
    recorder = RecordingConnection(db._conn)
    db._conn = recorder

    db.upsert_decision_outcome(
        "run-6",
        bot_id="mirinae",
        stock_code="091160",
        entry_time="2026-06-03T09:10:00+09:00",
    )

    normalized = [" ".join(sql.upper().split()) for sql in recorder.statements]
    assert any(
        "INSERT INTO DECISION_OUTCOMES" in sql
        and "ON CONFLICT(RUN_ID) DO UPDATE" in sql
        for sql in normalized
    )

    recorder.conn.close()
