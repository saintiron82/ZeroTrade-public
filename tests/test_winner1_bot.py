"""Winner1Bot 골격·작업장·파서 테스트. 실 AI 호출 없음."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.bots.winner1 import Winner1Bot, DEFAULT_PARAMS, parse_decision, clamp_next_scan


def make_bot(tmp_path, **params) -> Winner1Bot:
    merged = {**params, "workspace_root": str(tmp_path)}
    bot = Winner1Bot({"budget": 90000, "instance_id": "strat-001:winner-1",
                      "params": merged})
    return bot


def test_bot_identity_and_guards(tmp_path):
    bot = make_bot(tmp_path)
    assert bot.BOT_ID == "winner-1"
    assert bot.engine.budget == 90000
    # 일일 손실 한도 가드 opt-in (계좌 보호선)
    # 엔진 컨벤션: 음수 pct (engine._check_daily_loss_limit — 손실 누적이 음수).
    # 스펙(2026-06-11-zero-autonomous-agent-design.md): "일일손실 한도(-2%)"
    assert bot.engine._guard["daily_loss_limit_pct"] == -2.0
    # 전략성 가드는 전부 비활성 (자율 경계 — 계좌 보호선만 유지)
    assert bot.engine._guard["trend_filter_enabled"] is False
    assert bot.engine._guard["duplicate_check_enabled"] is False
    assert bot.engine._guard["market_drop_block_pct"] == 0
    assert bot.engine._guard["max_sector_exposure"] == 0


def test_workspace_bootstrap(tmp_path):
    bot = make_bot(tmp_path)
    ws = bot.workspace
    assert ws == Path(tmp_path) / "winner-1"
    assert (ws / "tools").is_dir()
    assert (ws / "journal.md").exists()
    mcp_cfg = json.loads((ws / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp_cfg["mcpServers"]["ff-trading-readonly"]
    assert server["env"]["FF_MCP_READONLY"] == "1"
    settings = json.loads((ws / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["enableAllProjectMcpServers"] is True


def test_workspace_bootstrap_idempotent_preserves_agent_files(tmp_path):
    bot = make_bot(tmp_path)
    journal = bot.workspace / "journal.md"
    journal.write_text("내 전략 메모", encoding="utf-8")
    tool = bot.workspace / "tools" / "screener.py"
    tool.write_text("# my tool", encoding="utf-8")

    bot2 = make_bot(tmp_path)  # 재부트스트랩
    assert journal.read_text(encoding="utf-8") == "내 전략 메모"
    assert tool.read_text(encoding="utf-8") == "# my tool"


def test_parse_decision_valid():
    text = '분석 결과... 최종 판단:\n```json\n{"action":"buy","code":"005930","qty":2,"reason":"수급 개선","next_scan_minutes":45}\n```'
    d = parse_decision(text)
    assert d == {"action": "buy", "code": "005930", "qty": 2,
                 "reason": "수급 개선", "next_scan_minutes": 45}


def test_parse_decision_bad_inputs_fall_back_to_hold():
    for text in ("", "json 없음", '{"action":"explode"}',
                 '{"action":"buy"}',                      # code/qty 누락
                 '{"action":"buy","code":"12345","qty":1}',   # 코드 5자리
                 '{"action":"buy","code":"005930","qty":0}'):  # qty 0
        d = parse_decision(text)
        assert d["action"] == "hold"
        assert "parse" in d.get("error", "") or d.get("error")


def test_parse_decision_hold_needs_no_code():
    d = parse_decision('{"action":"hold","reason":"관망","next_scan_minutes":10}')
    assert d["action"] == "hold"
    assert d["next_scan_minutes"] == 10


def test_parse_decision_rejects_example_json_quoted_in_prose():
    # 응답이 prose로 끝나면 본문에 인용된 결정 형태 JSON을 채택하지 않는다 (꼬리 앵커)
    text = ('출력 예시: {"action":"buy","code":"005930","qty":10,"reason":"r",'
            '"next_scan_minutes":30} 입니다. 하지만 오늘은 관망합니다.')
    d = parse_decision(text)
    assert d["action"] == "hold"
    assert "꼬리" in d.get("error", "")


def test_parse_decision_accepts_fenced_json_at_tail():
    # 마크다운 펜스(+trailing 공백)로 끝나는 정상 JSON은 계속 파싱된다
    text = ('분석 결과...\n```json\n'
            '{"action":"buy","code":"005930","qty":2,"reason":"수급","next_scan_minutes":45}\n'
            '```  \n')
    d = parse_decision(text)
    assert d == {"action": "buy", "code": "005930", "qty": 2,
                 "reason": "수급", "next_scan_minutes": 45}


def test_parse_decision_rejects_fullwidth_code():
    # 전각숫자 종목코드 차단 (isdigit만으로는 통과되는 케이스)
    d = parse_decision('{"action":"buy","code":"００５９３０","qty":1}')
    assert d["action"] == "hold"


def test_parse_decision_qty_type_validation():
    # float qty → hold
    d = parse_decision('{"action":"buy","code":"005930","qty":1.5}')
    assert d["action"] == "hold"
    # bool qty → hold
    d = parse_decision('{"action":"buy","code":"005930","qty":true}')
    assert d["action"] == "hold"
    # 문자열 정수는 허용
    d = parse_decision(
        '{"action":"buy","code":"005930","qty":"3","reason":"r","next_scan_minutes":10}')
    assert d["action"] == "buy"
    assert d["qty"] == 3


def test_clamp_next_scan():
    assert clamp_next_scan(45, 5, 240) == 45
    assert clamp_next_scan(1, 5, 240) == 5
    assert clamp_next_scan(999, 5, 240) == 240
    assert clamp_next_scan(None, 5, 240) == 5
    assert clamp_next_scan(-3, 5, 240) == 5
    assert clamp_next_scan("30", 5, 240) == 30


def test_build_prompt_contains_physical_facts_only(tmp_path):
    bot = make_bot(tmp_path)
    prompt = bot.build_prompt(cash=90000, positions=[
        {"code": "005930", "qty": 2, "avg_price": 71000}])
    # 물리적 사실
    assert "90,000" in prompt or "90000" in prompt
    assert "005930" in prompt
    assert "next_scan_minutes" in prompt          # 출력 스키마
    assert "인버스" in prompt                       # 파생ETF 제약 고지
    # 전략 지시 금지 — 대표 금칙어가 들어있지 않아야 한다
    for banned in ("RSI", "SMA", "손절", "트레일링", "추천 종목", "월스트리트"):
        assert banned not in prompt, f"전략 지시 누출: {banned}"


def test_run_session_returns_parsed_decision(tmp_path):
    bot = make_bot(tmp_path)
    fake = type("R", (), {"content": '{"action":"hold","reason":"관망","next_scan_minutes":30}',
                          "provider": "claude", "model": "", "tokens": 1234,
                          "latency_ms": 5000, "error": "", "error_type": ""})()
    with patch("src.bots.winner1.invoke_ai_result", return_value=fake) as call:
        decision, meta = bot.run_session(cash=90000, positions=[])
    assert decision["action"] == "hold"
    assert meta["tokens"] == 1234
    kwargs = call.call_args.kwargs
    assert kwargs["cwd"] == str(bot.workspace)
    assert kwargs["conversation_id"] == "bot:winner-1"
    assert kwargs["timeout"] == bot._params["session_timeout_sec"]


def test_run_session_error_falls_back_to_hold(tmp_path):
    bot = make_bot(tmp_path)
    fake = type("R", (), {"content": "", "provider": "claude", "model": "",
                          "tokens": 0, "latency_ms": 0,
                          "error": "timeout", "error_type": "TimeoutError"})()
    with patch("src.bots.winner1.invoke_ai_result", return_value=fake):
        decision, meta = bot.run_session(cash=90000, positions=[])
    assert decision["action"] == "hold"
    assert meta["error"] == "timeout"


# ═══════════════════════════════════════════
# run_scan_cycle — 자율 사이클 + audit 연동 (Task 5)
# ═══════════════════════════════════════════


def make_db(tmp_path, monkeypatch):
    """테스트용 audit DB 생성 + get_db 패치.

    다른 테스트가 sys.modules에서 src.db를 재로드할 수 있으므로
    모듈을 테스트 실행 시점에 lazy 하게 다시 가져온다.
    (tests/test_stealth_volume_audit.py 관례)
    """
    import src.db as db_module
    db = db_module.DB(tmp_path / "audit.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    return db


META = {"prompt": "p", "raw_response": "r", "provider": "claude",
        "model": "m", "tokens": 10, "latency_ms": 100, "error": ""}


def fake_market(price: int = 71000) -> MagicMock:
    market = MagicMock()
    market.get_price.return_value = {"price": price} if price > 0 else {}
    return market


def test_scan_cycle_buy_executes_and_audits(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    bot = make_bot(tmp_path)
    bot._scan_count = 1

    decision = {"action": "buy", "code": "005930", "qty": 1,
                "reason": "테스트", "next_scan_minutes": 60}
    with (
        patch.object(bot, "_current_state", return_value=(90000, [])),
        patch.object(bot, "run_session", return_value=(decision, META)),
        patch.object(bot, "_get_market", return_value=fake_market(71000)),
        patch.object(bot.engine, "buy",
                     return_value={"success": True, "order_no": "1",
                                   "message": "ok", "trade_id": 7}) as buy,
    ):
        bot.run_scan_cycle()

    # C1: 매수 전 현재가 조회 → 실제 가격으로 주문 (price=0 회계 오염 방지)
    buy.assert_called_once_with("005930", 1, 71000)
    assert bot._next_scan_minutes == 60
    assert bot._scan_count == 2  # M4: run_scan_cycle 내부에서 증가

    runs = db._conn.execute("SELECT * FROM decision_runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["bot_id"] == "winner-1"
    assert runs[0]["status"] == "ordered"
    assert runs[0]["action"] == "buy"
    assert runs[0]["trade_id"] == 7
    ai = db.get_ai_audit(runs[0]["run_id"])
    assert ai["prompt"] == "p"
    assert ai["raw_response"] == "r"
    assert ai["parse_success"] == 1

    # I1: decisions 테이블 기록 (CLAUDE.md 규칙 10)
    decisions = db.get_decisions(bot_id="winner-1")
    assert len(decisions) == 1
    assert decisions[0]["action"] == "buy"
    assert decisions[0]["stock_code"] == "005930"
    assert decisions[0]["trade_id"] == 7
    assert runs[0]["decision_id"] == decisions[0]["id"]


def test_scan_cycle_buy_without_price_is_blocked(tmp_path, monkeypatch):
    """현재가 조회 실패(0원) → 주문 미호출 + blocked finish."""
    db = make_db(tmp_path, monkeypatch)
    bot = make_bot(tmp_path)
    decision = {"action": "buy", "code": "005930", "qty": 1,
                "reason": "테스트", "next_scan_minutes": 30}
    with (
        patch.object(bot, "_current_state", return_value=(90000, [])),
        patch.object(bot, "run_session", return_value=(decision, META)),
        patch.object(bot, "_get_market", return_value=fake_market(0)),
        patch.object(bot.engine, "buy") as buy,
    ):
        bot.run_scan_cycle()
    buy.assert_not_called()
    run = db._conn.execute("SELECT * FROM decision_runs").fetchone()
    assert run["status"] == "blocked"
    checks = db.get_preflight_checks(run["run_id"])
    assert any("현재가" in c["message"] for c in checks)


def test_scan_cycle_hold_and_clamp(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    bot = make_bot(tmp_path)
    decision = {"action": "hold", "code": "", "qty": 0,
                "reason": "관망", "next_scan_minutes": 999}
    with (
        patch.object(bot, "_current_state", return_value=(90000, [])),
        patch.object(bot, "run_session", return_value=(decision, META)),
    ):
        bot.run_scan_cycle()
    assert bot._next_scan_minutes == 240  # max 클램프
    run = db._conn.execute("SELECT * FROM decision_runs").fetchone()
    assert run["status"] == "completed"
    assert run["action"] == "hold"
    # winner-1은 스캔이 드물어 hold도 매 사이클 decisions에 기록
    decisions = db.get_decisions(bot_id="winner-1")
    assert len(decisions) == 1
    assert decisions[0]["action"] == "hold"


def test_scan_cycle_exception_finishes_failed(tmp_path, monkeypatch):
    """run_session 예외 시에도 decision_runs가 failed로 마감된다 (I2)."""
    db = make_db(tmp_path, monkeypatch)
    bot = make_bot(tmp_path)
    with (
        patch.object(bot, "_current_state", return_value=(90000, [])),
        patch.object(bot, "run_session", side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(RuntimeError):
            bot.run_scan_cycle()
    run = db._conn.execute("SELECT * FROM decision_runs").fetchone()
    assert run["status"] == "failed"
    assert "boom" in run["error"]


def test_scan_cycle_dry_run_skips_orders(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    bot = make_bot(tmp_path, dry_run=True)
    decision = {"action": "buy", "code": "005930", "qty": 1,
                "reason": "테스트", "next_scan_minutes": 30}
    with (
        patch.object(bot, "_current_state", return_value=(90000, [])),
        patch.object(bot, "run_session", return_value=(decision, META)),
        patch.object(bot.engine, "buy") as buy,
    ):
        bot.run_scan_cycle()
    buy.assert_not_called()
    assert bot._next_scan_minutes == 30
    run = db._conn.execute("SELECT * FROM decision_runs").fetchone()
    assert run["status"] == "completed"  # dry-run은 주문 없음
