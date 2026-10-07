"""전략 관리 + 판단 이력 테스트."""

import json
from unittest.mock import patch

import pytest


def test_strategy_create_and_budget(tmp_path):
    """전략 생성 + 예산 배정."""
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"):
        from src.strategy import StrategyManager

        mgr = StrategyManager()
        mgr.set_total_budget(1_000_000)

        s1 = mgr.create_strategy("반도체 역추세", budget=500_000)
        assert s1.id == "strat-001"
        assert s1.budget == 500_000
        assert mgr.portfolio.unallocated() == 500_000

        s2 = mgr.create_strategy("ETF 모멘텀", budget=300_000)
        assert mgr.portfolio.unallocated() == 200_000


def test_strategy_budget_exceeded(tmp_path):
    """예산 초과 시 에러."""
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"):
        from src.strategy import StrategyManager

        mgr = StrategyManager()
        mgr.set_total_budget(500_000)
        mgr.create_strategy("전략1", budget=400_000)

        with pytest.raises(ValueError, match="예산 부족"):
            mgr.create_strategy("전략2", budget=200_000)


def test_strategy_can_order(tmp_path):
    """주문 가능 여부 검증."""
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"):
        from src.strategy import StrategyManager

        mgr = StrategyManager()
        mgr.set_total_budget(1_000_000)
        s = mgr.create_strategy("테스트", budget=100_000)

        assert s.can_order(50_000) is True
        assert s.can_order(150_000) is False

        mgr.use_budget(s.id, 80_000)
        s = mgr.get(s.id)  # 재로드
        assert s.cash == 20_000
        assert s.can_order(30_000) is False

        mgr.release_budget(s.id, 80_000)
        s = mgr.get(s.id)
        assert s.cash == 100_000


def test_strategy_persistence(tmp_path):
    """전략 데이터 저장/로드."""
    file = tmp_path / "strategies.json"
    with patch("src.strategy.STRATEGIES_FILE", file):
        from src.strategy import StrategyManager

        mgr1 = StrategyManager()
        mgr1.set_total_budget(1_000_000)
        mgr1.create_strategy("테스트", budget=500_000)

    # 새 인스턴스에서 로드
    with patch("src.strategy.STRATEGIES_FILE", file):
        from src.strategy import StrategyManager

        mgr2 = StrategyManager()
        assert mgr2.portfolio.total_budget == 1_000_000
        assert len(mgr2.list_strategies()) == 1
        assert mgr2.list_strategies()[0].name == "테스트"


def test_decision_log(tmp_path):
    """판단 이력 기록."""
    with patch("src.decision_log.DECISIONS_FILE", tmp_path / "decisions.json"):
        from src.decision_log import DecisionLog

        log = DecisionLog()
        log.log(
            "ai_analysis",
            strategy_id="strat-001",
            input_data={"prompt": "삼성전자 분석"},
            output_data={"conditions": 2},
            action_taken="created_conditions",
        )
        log.log(
            "order_placed",
            strategy_id="strat-001",
            input_data={"stock_code": "005930"},
            action_taken="buy_market",
        )

        assert len(log.recent(10)) == 2
        assert len(log.by_strategy("strat-001")) == 2
        assert len(log.by_type("ai_analysis")) == 1


def test_set_bot_interval_writes_params_only(tmp_path):
    """주기 변경은 params.interval_seconds만 기록하고 ratio/다른 봇은 불변."""
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"):
        from src.strategy import StrategyManager

        mgr = StrategyManager()
        mgr.set_total_budget(1_000_000)
        s = mgr.create_strategy("테스트", budget=500_000)
        s.bots = [
            {"bot_id": "mirinae-2", "ratio": 0.6, "params": {}},
            {"bot_id": "stealth-volume", "ratio": 0.4, "params": {}},
        ]
        mgr._save()

        found = mgr.set_bot_interval(s.id, "mirinae-2", 900)
        assert found is True

        reloaded = mgr.get(s.id)
        m2 = next(b for b in reloaded.bots if b["bot_id"] == "mirinae-2")
        sv = next(b for b in reloaded.bots if b["bot_id"] == "stealth-volume")
        assert m2["params"]["interval_seconds"] == 900
        assert m2["ratio"] == 0.6          # ratio 불변
        assert sv["params"] == {}          # 다른 봇 불변


def test_set_bot_interval_missing_bot_returns_false(tmp_path):
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"):
        from src.strategy import StrategyManager

        mgr = StrategyManager()
        mgr.set_total_budget(1_000_000)
        s = mgr.create_strategy("테스트", budget=500_000)
        s.bots = [{"bot_id": "mirinae-2", "ratio": 1.0, "params": {}}]
        mgr._save()

        assert mgr.set_bot_interval(s.id, "not-linked", 900) is False
