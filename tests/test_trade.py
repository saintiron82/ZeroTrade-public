"""매매 실행기 테스트."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.kis.order import OrderResult
from src.monitor.evaluator import Condition, EvalResult, Rule
from src.monitor.indicators import IndicatorResult


def test_trade_logger_records(tmp_path):
    """매매 이력이 정상 기록되는지 확인."""
    with patch("src.trade.logger.settings") as mock_settings:
        mock_settings.logs_dir = tmp_path
        mock_settings.ensure_dirs = MagicMock()

        from src.trade.logger import TradeLogger

        tl = TradeLogger()
        result = OrderResult(
            success=True, order_no="0001",
            stock_code="005930", side="buy",
            quantity=10, price=70000,
        )
        entry = tl.log_trade(result, condition_id="c1", reason="RSI 과매도")

        assert entry["stock_code"] == "005930"
        assert entry["success"] is True

        # 파일에 저장 확인
        saved = json.loads((tmp_path / "trades.json").read_text())
        assert len(saved) == 1


def test_trade_logger_summary(tmp_path):
    """매매 요약 통계."""
    with patch("src.trade.logger.settings") as mock_settings:
        mock_settings.logs_dir = tmp_path
        mock_settings.ensure_dirs = MagicMock()

        from src.trade.logger import TradeLogger

        tl = TradeLogger()
        for i in range(3):
            tl.log_trade(OrderResult(
                success=True, order_no=str(i),
                stock_code="005930", side="buy", quantity=1, price=70000,
            ))
        tl.log_trade(OrderResult(
            success=False, stock_code="005930", side="sell", quantity=1, price=70000,
        ))

        summary = tl.summary()
        assert summary["total"] == 4
        assert summary["buys"] == 3
        assert summary["sells"] == 1
        assert summary["success_rate"] == 0.75


def test_executor_dry_run():
    """dry_run 모드에서 실제 주문이 실행되지 않는지 확인."""
    with patch("src.trade.executor.KISOrder"), \
         patch("src.trade.executor.TradeLogger"), \
         patch("src.trade.executor.Notifier"):
        from src.trade.executor import TradeExecutor

        executor = TradeExecutor(dry_run=True)
        cond = Condition(
            id="c1", stock_code="005930", stock_name="삼성전자",
            action="buy", rules=[], logic="AND", amount=100000,
        )
        eval_result = EvalResult(
            condition=cond, triggered=True,
            matched_rules=["rsi < 30"], indicator_snapshot={"price": 70000},
        )
        result = executor.execute(eval_result)
        assert result is True  # dry_run은 항상 True
