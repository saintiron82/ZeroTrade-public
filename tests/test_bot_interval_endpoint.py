"""PUT /api/pipes/{id}/bot-interval — 파이프 인스턴스별 분석 주기 변경."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _client():
    from src.web.app import app
    return TestClient(app)


def _seed_pipe(tmp_path):
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    mgr.set_total_budget(1_000_000)
    s = mgr.create_strategy("테스트", budget=500_000)
    s.bots = [{"bot_id": "mirinae-2", "ratio": 1.0, "params": {}}]
    mgr._save()
    return s.id


def test_bot_interval_valid_persists_and_sends_command(tmp_path):
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"), \
         patch("src.services.redis_bus.get_bus") as get_bus:
        bus = MagicMock()
        get_bus.return_value = bus
        pipe_id = _seed_pipe(tmp_path)

        resp = _client().put(
            f"/api/pipes/{pipe_id}/bot-interval",
            json={"bot_id": "mirinae-2", "interval_seconds": 900},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["interval_seconds"] == 900

        # strategies.json에 저장됐는지
        from src.strategy import StrategyManager
        s = StrategyManager().get(pipe_id)
        assert s.bots[0]["params"]["interval_seconds"] == 900

        # Redis 명령 발행됐는지
        bus.send_command.assert_called_once()
        args, kwargs = bus.send_command.call_args
        assert args[0] == "pipe"
        assert args[1] == pipe_id
        assert args[2] == "set_bot_interval"


def test_bot_interval_out_of_range_rejected(tmp_path):
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"), \
         patch("src.services.redis_bus.get_bus") as get_bus:
        get_bus.return_value = MagicMock()
        pipe_id = _seed_pipe(tmp_path)

        resp = _client().put(
            f"/api/pipes/{pipe_id}/bot-interval",
            json={"bot_id": "mirinae-2", "interval_seconds": 5},  # min=300 미만
        )
        data = resp.json()
        assert data["success"] is False

        # 저장 안 됨
        from src.strategy import StrategyManager
        s = StrategyManager().get(pipe_id)
        assert "interval_seconds" not in s.bots[0]["params"]


def test_bot_interval_unsupported_bot_rejected(tmp_path):
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"), \
         patch("src.services.redis_bus.get_bus") as get_bus:
        get_bus.return_value = MagicMock()
        from src.strategy import StrategyManager
        mgr = StrategyManager()
        mgr.set_total_budget(1_000_000)
        s = mgr.create_strategy("테스트", budget=500_000)
        s.bots = [{"bot_id": "analyst", "ratio": 1.0, "params": {}}]
        mgr._save()

        resp = _client().put(
            f"/api/pipes/{s.id}/bot-interval",
            json={"bot_id": "analyst", "interval_seconds": 600},
        )
        assert resp.json()["success"] is False
