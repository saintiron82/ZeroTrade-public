"""매매 이력 기록."""

import json
import logging
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.kis.order import OrderResult

logger = logging.getLogger(__name__)


class TradeLogger:
    """매매 이력을 JSON 파일에 기록.

    AI 피드백 루프에서 과거 매매 결과를 참조할 수 있도록
    구조화된 형태로 저장한다.
    """

    def __init__(self):
        settings.ensure_dirs()
        self._log_file = settings.logs_dir / "trades.json"
        self._trades: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self._log_file.exists():
            try:
                return json.loads(self._log_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, KeyError):
                return []
        return []

    def _save(self):
        self._log_file.write_text(
            json.dumps(self._trades, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def log_trade(
        self, result: OrderResult, condition_id: str = "", reason: str = "",
        strategy_id: str = "",
    ) -> dict:
        """매매 결과 기록."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "stock_code": result.stock_code,
            "side": result.side,
            "quantity": result.quantity,
            "price": result.price,
            "order_no": result.order_no,
            "success": result.success,
            "message": result.message,
            "condition_id": condition_id,
            "reason": reason,
            "strategy_id": strategy_id,
        }
        self._trades.append(entry)
        self._save()
        logger.info("매매 기록: %s %s %s주", result.side, result.stock_code, result.quantity)
        return entry

    def recent(self, count: int = 10) -> list[dict]:
        """최근 N건의 매매 이력."""
        return self._trades[-count:]

    def by_stock(self, stock_code: str) -> list[dict]:
        """특정 종목의 매매 이력."""
        return [t for t in self._trades if t["stock_code"] == stock_code]

    def by_strategy(self, strategy_id: str) -> list[dict]:
        """특정 전략의 매매 이력."""
        return [t for t in self._trades if t.get("strategy_id") == strategy_id]

    def summary(self, strategy_id: str = "") -> dict:
        """매매 요약 통계. strategy_id가 있으면 해당 전략만."""
        trades = self.by_strategy(strategy_id) if strategy_id else self._trades
        total = len(trades)
        buys = [t for t in trades if t["side"] == "buy"]
        sells = [t for t in trades if t["side"] == "sell"]
        successes = [t for t in trades if t["success"]]
        return {
            "total": total,
            "buys": len(buys),
            "sells": len(sells),
            "success_rate": len(successes) / total if total > 0 else 0,
            "strategy_id": strategy_id,
        }
