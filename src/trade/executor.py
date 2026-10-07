"""매매 실행기 — 조건 충족 시 전략 자금 검증 + 주문 + 알림 + 기록."""

import logging

from src.decision_log import DecisionLog
from src.kis.order import KISOrder, OrderType
from src.monitor.evaluator import EvalResult
from src.strategy import StrategyManager
from src.trade.logger import TradeLogger
from src.trade.notifier import Notifier

logger = logging.getLogger(__name__)


class TradeExecutor:
    """조건 충족 시 전략별 자금을 검증하고 매매 실행."""

    def __init__(self, dry_run: bool = False, strategy_manager: StrategyManager | None = None):
        self._order = KISOrder()
        self._logger = TradeLogger()
        self._notifier = Notifier()
        self._decisions = DecisionLog()
        self._strategy_mgr = strategy_manager or StrategyManager()
        self._dry_run = dry_run

    def execute(self, eval_result: EvalResult) -> bool:
        """조건 충족 결과를 받아 매매 실행.

        1. 전략 자금 검증
        2. 주문 실행
        3. 알림 + 기록 + 판단 이력
        """
        cond = eval_result.condition
        strategy_id = cond.strategy_id

        # 알림
        self._notifier.notify_trigger(eval_result)

        # 전략 자금 검증
        if strategy_id:
            strategy = self._strategy_mgr.get(strategy_id)
            if strategy and not strategy.can_order(cond.amount):
                logger.warning(
                    "전략 %s 예산 초과: 잔여 %s원, 필요 %s원",
                    strategy_id, f"{strategy.remaining():,}", f"{cond.amount:,}",
                )
                self._decisions.log(
                    "order_rejected",
                    strategy_id=strategy_id,
                    input_data={"condition_id": cond.id, "amount": cond.amount},
                    action_taken="rejected_budget_exceeded",
                )
                return False

        if self._dry_run:
            logger.info("[DRY RUN] %s %s — 실제 주문 안 함", cond.action, cond.stock_code)
            self._decisions.log(
                "order_placed",
                strategy_id=strategy_id,
                input_data={"condition_id": cond.id, "stock_code": cond.stock_code},
                output_data={"dry_run": True},
                action_taken=f"dry_run_{cond.action}",
            )
            return True

        # 수량 계산 + 검증
        price = int(eval_result.indicator_snapshot.get("price", 0))
        if price <= 0:
            logger.warning("현재가 없음 — 주문 불가: %s", cond.stock_code)
            return False

        # 주문 실행
        if cond.action == "buy":
            qty = cond.amount // price if price > 0 else 0
            if qty <= 0:
                logger.warning("매수 수량 0 — 금액 %s원, 가격 %s원", cond.amount, price)
                return False
            result = self._order.buy(
                stock_code=cond.stock_code,
                quantity=qty,
                price=price,
                order_type=OrderType.MARKET,
            )
        elif cond.action == "sell":
            qty = max(1, cond.amount // price) if price > 0 else 0
            if qty <= 0:
                return False
            result = self._order.sell(
                stock_code=cond.stock_code,
                quantity=qty,
                order_type=OrderType.MARKET,
            )
        else:
            logger.warning("알 수 없는 액션: %s", cond.action)
            return False

        # 전략 예산 차감
        if result.success and strategy_id and cond.action == "buy":
            self._strategy_mgr.use_budget(strategy_id, cond.amount)

        # 알림 + 매매 기록
        self._notifier.notify_order(result, cond.id)
        self._logger.log_trade(
            result, condition_id=cond.id, reason=cond.reason, strategy_id=strategy_id
        )

        # DB 기록
        from src.db import get_db
        db = get_db()
        db.log_trade(
            pipe_id=strategy_id, stock_code=cond.stock_code,
            stock_name=cond.stock_name, side=cond.action,
            quantity=result.quantity, price=result.price,
            order_no=result.order_no, success=result.success,
            reason=cond.reason, source="condition-bot",
        )

        # 판단 이력
        self._decisions.log(
            "order_result",
            strategy_id=strategy_id,
            input_data={"condition_id": cond.id, "stock_code": cond.stock_code, "action": cond.action},
            output_data={
                "success": result.success, "order_no": result.order_no,
                "quantity": result.quantity, "price": result.price,
            },
            action_taken=f"{'success' if result.success else 'failed'}_{cond.action}",
        )

        return result.success

    @property
    def trade_logger(self) -> TradeLogger:
        return self._logger

    @property
    def decision_log(self) -> DecisionLog:
        return self._decisions

    def close(self):
        self._order.close()
        self._notifier.close()
