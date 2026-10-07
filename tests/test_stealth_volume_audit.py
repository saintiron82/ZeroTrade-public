"""stealth-volume audit 연동 테스트."""

from src.bots.stealth_volume import StealthVolumeBot


def make_bot() -> StealthVolumeBot:
    """초기화 부작용 없이 audit 헬퍼만 검증하기 위한 베어 인스턴스."""
    bot = StealthVolumeBot.__new__(StealthVolumeBot)
    bot._config = None
    bot._scan_count = 3
    return bot


def make_db(tmp_path, monkeypatch):
    """테스트용 audit DB 생성 + get_db 패치.

    다른 테스트가 sys.modules에서 src.db를 재로드할 수 있으므로
    모듈을 테스트 실행 시점에 lazy 하게 다시 가져온다.
    """
    import src.db as db_module
    db = db_module.DB(tmp_path / "audit.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    return db


def test_audit_scan_cycle_records_run_evidence_and_finish(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)

    bot = make_bot()
    recorder = bot._audit_start_run()
    assert recorder is not None

    candidates = [
        {"code": "069500", "vol_ratio": 4.2, "price_pct": 0.8},
        {"code": "122630", "vol_ratio": 3.1, "price_pct": 0.5},
    ]
    bot._audit_record_candidates(recorder, candidates)

    decision = {"action": "buy", "code": "069500", "reason": "거래량 4.2배 + 양봉"}
    bot._audit_finish_scan(recorder, decision, decision_id=7, trade_id=11,
                           trade_success=True)

    run = db.get_decision_run(recorder.run_id)
    assert run["bot_id"] == "stealth-volume"
    assert run["scan_count"] == 3
    assert run["status"] == "ordered"
    assert run["action"] == "buy"
    assert run["stock_code"] == "069500"
    assert run["decision_id"] == 7
    assert run["trade_id"] == 11

    snapshots = db.get_evidence_snapshots(recorder.run_id)
    assert len(snapshots) == 1
    assert snapshots[0]["source"] == "signals"


def test_audit_finish_scan_wait_is_completed(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)

    bot = make_bot()
    recorder = bot._audit_start_run()
    bot._audit_finish_scan(recorder, {"action": "wait", "reason": "신호 없음"},
                           decision_id=0, trade_id=0, trade_success=False)

    run = db.get_decision_run(recorder.run_id)
    assert run["status"] == "completed"
    assert run["action"] == "wait"


def test_audit_finish_scan_blocked_buy_records_block_reason(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)

    bot = make_bot()
    recorder = bot._audit_start_run()
    # decision reason은 매수 신호 사유 — 실제 차단 사유는 block_reason으로 전달
    decision = {"action": "buy", "code": "069500", "reason": "거래량 4.2배 + 양봉"}
    bot._audit_finish_scan(recorder, decision, decision_id=0, trade_id=0,
                           trade_success=False,
                           block_reason="매수 가능 수량 없음")

    run = db.get_decision_run(recorder.run_id)
    # 가드/사유에 의한 미체결 = blocked (mirinae와 동일 의미론)
    assert run["status"] == "blocked"

    checks = db.get_preflight_checks(recorder.run_id)
    assert len(checks) == 1
    assert checks[0]["check_name"] == "execution"
    assert checks[0]["passed"] in (0, False)
    assert checks[0]["message"] == "매수 가능 수량 없음"


def test_audit_finish_scan_block_reason_falls_back_to_decision_reason(
        tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)

    bot = make_bot()
    recorder = bot._audit_start_run()
    decision = {"action": "buy", "code": "069500", "reason": "일일 손실 한도 도달"}
    # block_reason이 비어 있으면 decision reason으로 폴백
    bot._audit_finish_scan(recorder, decision, decision_id=0, trade_id=0,
                           trade_success=False)

    checks = db.get_preflight_checks(recorder.run_id)
    assert len(checks) == 1
    assert checks[0]["message"] == "일일 손실 한도 도달"


def test_audit_helpers_tolerate_none_recorder():
    bot = make_bot()
    # recorder가 None이어도 (audit DB 실패 시) 예외 없이 통과해야 함
    bot._audit_record_candidates(None, [])
    bot._audit_finish_scan(None, {"action": "wait"}, 0, 0, False)
