"""Tests for pipe runner bot selection."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.pipe_runner import PipeRunner


def _runner() -> PipeRunner:
    return object.__new__(PipeRunner)


def test_position_custodian_added_for_inactive_open_lot_owner():
    runner = _runner()
    strategy = SimpleNamespace(
        id="strat-001",
        holdings=[{"code": "102970", "qty": 8}],
    )
    db = MagicMock()
    db.get_trades.return_value = [
        {
            "timestamp": "2026-05-04T14:25:00+09:00",
            "source": "mirinae-3",
            "stock_code": "102970",
            "side": "buy",
            "quantity": 2,
            "price": 29695,
            "success": True,
        },
        {
            "timestamp": "2026-05-04T15:09:00+09:00",
            "source": "mirinae-3",
            "stock_code": "102970",
            "side": "buy",
            "quantity": 2,
            "price": 29615,
            "success": True,
        },
    ]

    with patch("src.db.get_db", return_value=db), patch(
        "src.bots.registry.get", return_value=SimpleNamespace(id="mirinae-3")
    ):
        entries = runner._position_custodian_entries(strategy, {"stealth-volume"})

    assert entries == [
        {
            "bot_id": "mirinae-3",
            "ratio": 0.0,
            "budget": 0,
            "params": {
                "position_custodian": True,
                "max_positions": 0,
            },
            "enabled": True,
        }
    ]


def test_position_custodian_not_added_for_active_bot():
    runner = _runner()
    strategy = SimpleNamespace(
        id="strat-001",
        holdings=[{"code": "102970", "qty": 8}],
    )
    db = MagicMock()
    db.get_trades.return_value = [
        {
            "timestamp": "2026-05-04T15:09:00+09:00",
            "source": "mirinae-3",
            "stock_code": "102970",
            "side": "buy",
            "quantity": 2,
            "price": 29615,
            "success": True,
        },
    ]

    with patch("src.db.get_db", return_value=db), patch(
        "src.bots.registry.get", return_value=SimpleNamespace(id="mirinae-3")
    ):
        entries = runner._position_custodian_entries(strategy, {"mirinae-3"})

    assert entries == []


def test_on_command_set_bot_interval_updates_matching_bot():
    runner = _runner()
    runner.pipe_id = "strat-001"

    bot_a = SimpleNamespace(
        BOT_ID="mirinae-2",
        _config=SimpleNamespace(interval_seconds=600),
        _params={"interval_seconds": 600},
    )
    bot_b = SimpleNamespace(
        BOT_ID="stealth-volume",
        _config=SimpleNamespace(interval_seconds=30),
        _params={"interval_seconds": 30},
    )
    runner.bots = [bot_a, bot_b]

    with patch(
        "src.bots.registry.interval_spec",
        return_value={"min": 300, "max": 1800, "default": 600},
    ):
        runner._on_command({
            "service_id": "strat-001",
            "action": "set_bot_interval",
            "config": {"bot_id": "mirinae-2", "interval_seconds": 900},
        })

    assert bot_a._config.interval_seconds == 900
    assert bot_a._params["interval_seconds"] == 900
    assert bot_b._config.interval_seconds == 30  # 다른 봇 불변


def test_on_command_set_bot_interval_clamps_out_of_range():
    runner = _runner()
    runner.pipe_id = "strat-001"
    bot_a = SimpleNamespace(
        BOT_ID="mirinae-2",
        _config=SimpleNamespace(interval_seconds=600),
        _params={"interval_seconds": 600},
    )
    runner.bots = [bot_a]

    with patch(
        "src.bots.registry.interval_spec",
        return_value={"min": 300, "max": 1800, "default": 600},
    ):
        runner._on_command({
            "service_id": "strat-001",
            "action": "set_bot_interval",
            "config": {"bot_id": "mirinae-2", "interval_seconds": 99999},
        })

    assert bot_a._config.interval_seconds == 1800  # max로 클램프


def test_on_command_set_bot_interval_ignores_other_pipe():
    runner = _runner()
    runner.pipe_id = "strat-001"
    bot_a = SimpleNamespace(
        BOT_ID="mirinae-2",
        _config=SimpleNamespace(interval_seconds=600),
        _params={"interval_seconds": 600},
    )
    runner.bots = [bot_a]

    runner._on_command({
        "service_id": "strat-999",  # 다른 파이프
        "action": "set_bot_interval",
        "config": {"bot_id": "mirinae-2", "interval_seconds": 900},
    })

    assert bot_a._config.interval_seconds == 600  # 변경 없음
