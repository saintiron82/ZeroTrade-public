"""MCP audit KPI report tests."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.db import DB


def recent_time() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat()


def old_time(days: int = 60) -> str:
    return (datetime.now(ZoneInfo("Asia/Seoul")) - timedelta(days=days)).isoformat()


def add_outcome(
    db: DB,
    run_id: str,
    *,
    bot_id: str = "mirinae-4",
    stock_code: str = "102780",
    realized_pnl: float = 0,
    return_pct: float = 0,
    outcome_status: str = "closed",
    entry_time: str | None = None,
    exit_time: str | None = None,
) -> None:
    db.upsert_decision_outcome(
        run_id=run_id,
        bot_id=bot_id,
        stock_code=stock_code,
        entry_time=entry_time if entry_time is not None else recent_time(),
        exit_time=exit_time,
        entry_price=10000,
        exit_price=10000 + realized_pnl,
        quantity=1,
        realized_pnl=realized_pnl,
        return_pct=return_pct,
        outcome_status=outcome_status,
    )


def test_bot_audit_kpi_groups_closed_outcomes(tmp_path):
    db = DB(tmp_path / "ff.db")
    add_outcome(
        db,
        "r1",
        stock_code="102780",
        realized_pnl=300,
        return_pct=3.0,
    )
    add_outcome(
        db,
        "r2",
        stock_code="305720",
        realized_pnl=-100,
        return_pct=-1.0,
    )

    kpi = db.get_audit_kpi(bot_id="mirinae-4")

    assert kpi["bot_id"] == "mirinae-4"
    assert kpi["closed"] == 2
    assert kpi["wins"] == 1
    assert kpi["losses"] == 1
    assert kpi["win_rate"] == 0.5
    assert kpi["total_pnl"] == 200
    assert kpi["avg_pnl"] == 100
    assert kpi["avg_return_pct"] == 1.0
    assert kpi["profit_factor"] == 3.0
    assert kpi["by_code"] == {
        "102780": {"closed": 1, "pnl": 300, "wins": 1, "losses": 0},
        "305720": {"closed": 1, "pnl": -100, "wins": 0, "losses": 1},
    }
    db.close()


def test_audit_kpi_does_not_include_open_outcomes(tmp_path):
    db = DB(tmp_path / "ff.db")
    add_outcome(db, "closed", realized_pnl=300, return_pct=3.0)
    add_outcome(db, "open", realized_pnl=900, return_pct=9.0, outcome_status="open")

    kpi = db.get_audit_kpi(bot_id="mirinae-4")

    assert kpi["closed"] == 1
    assert kpi["wins"] == 1
    assert kpi["total_pnl"] == 300
    assert kpi["profit_factor"] is None
    db.close()


def test_audit_kpi_includes_recently_closed_long_hold(tmp_path):
    db = DB(tmp_path / "ff.db")
    add_outcome(
        db,
        "long-hold",
        realized_pnl=300,
        entry_time=old_time(days=90),
        exit_time=recent_time(),
    )

    kpi = db.get_audit_kpi(bot_id="mirinae-4", days=30)

    assert kpi["closed"] == 1
    assert kpi["total_pnl"] == 300
    db.close()


def test_audit_kpi_filters_by_bot_id(tmp_path):
    db = DB(tmp_path / "ff.db")
    add_outcome(db, "target", bot_id="mirinae-4", realized_pnl=300)
    add_outcome(db, "other", bot_id="other-bot", realized_pnl=-100)

    kpi = db.get_audit_kpi(bot_id="mirinae-4")
    all_kpi = db.get_audit_kpi()

    assert kpi["closed"] == 1
    assert kpi["total_pnl"] == 300
    assert all_kpi["closed"] == 2
    assert all_kpi["total_pnl"] == 200
    db.close()


def test_mcp_audit_kpi_formatter_includes_korean_summary(monkeypatch, tmp_path):
    import src.api_log as api_log
    import src.db as db_module
    from src.mcp_server import get_audit_kpi

    db = DB(tmp_path / "ff.db")
    add_outcome(db, "win", realized_pnl=300, return_pct=3.0)
    add_outcome(db, "loss", stock_code="305720", realized_pnl=-100, return_pct=-1.0)
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(api_log, "log_mcp_call", lambda *args, **kwargs: None)

    report = get_audit_kpi(bot_id="mirinae-4", days=30)

    assert "[감사 DB KPI - mirinae-4 최근 30일]" in report
    assert "청산: 2건 / 승 1 / 패 1" in report
    assert "승률: 50.0%" in report
    assert "총손익: +200원" in report
    assert "거래당 손익: +100원" in report
    assert "거래당 수익률: +1.00%" in report
    assert "Profit Factor: 3.00" in report
    assert "102780: 1건, 승률 100.0%, 손익 +300원" in report
    assert "305720: 1건, 승률 0.0%, 손익 -100원" in report
    db.close()


def test_mcp_audit_kpi_formatter_empty_message(monkeypatch, tmp_path):
    import src.api_log as api_log
    import src.db as db_module
    from src.mcp_server import get_audit_kpi

    db = DB(tmp_path / "ff.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(api_log, "log_mcp_call", lambda *args, **kwargs: None)

    assert get_audit_kpi(bot_id="mirinae-4") == "감사 DB 청산 결과가 없습니다."
    db.close()


def test_mcp_audit_kpi_formatter_displays_undefined_profit_factor(monkeypatch, tmp_path):
    import src.api_log as api_log
    import src.db as db_module
    from src.mcp_server import get_audit_kpi

    db = DB(tmp_path / "ff.db")
    add_outcome(db, "win", realized_pnl=300, return_pct=3.0)
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(api_log, "log_mcp_call", lambda *args, **kwargs: None)

    report = get_audit_kpi(bot_id="mirinae-4", days=30)

    assert "Profit Factor: N/A" in report
    db.close()
