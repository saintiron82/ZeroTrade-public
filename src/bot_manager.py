"""BotManager — 다중 전략 봇 동시 실행 관리."""

import asyncio
import logging
from dataclasses import dataclass, field

from src.kis.websocket import KISWebSocket, Tick
from src.monitor.evaluator import ConditionEvaluator
from src.monitor.indicators import IndicatorEngine
from src.bots.events import publish as emit_event
from src.strategy import StrategyManager
from src.trade.executor import TradeExecutor

logger = logging.getLogger(__name__)


@dataclass
class StrategyRunner:
    """전략 하나의 실행 상태."""
    strategy_id: str
    evaluator: ConditionEvaluator
    triggered: set = field(default_factory=set)
    stock_codes: list = field(default_factory=list)


class BotManager:
    """모든 전략 봇을 통합 관리하는 싱글톤.

    - 공유 WebSocket (1개) + 공유 지표 엔진 + 공유 매매 실행기
    - 전략별 독립 ConditionEvaluator
    - 종목 구독 참조 카운트로 동적 관리
    """

    def __init__(self, dry_run: bool = True):
        self._ws = KISWebSocket()
        self._indicators = IndicatorEngine()
        self._executor = TradeExecutor(dry_run=dry_run)
        self._strategy_mgr = StrategyManager()
        self._runners: dict[str, StrategyRunner] = {}
        self._stock_refcount: dict[str, int] = {}
        self._running = False
        self._ws_task: asyncio.Task | None = None
        self._dry_run = dry_run

    def activate(self, strategy_id: str) -> tuple[bool, str]:
        """전략 봇 시작 — 조건식 로드 + WebSocket 종목 추가."""
        if strategy_id in self._runners:
            return True, f"전략 {strategy_id} 이미 활성화됨"

        strategy = self._strategy_mgr.get(strategy_id)
        if not strategy:
            return False, f"전략 {strategy_id} 없음"
        if not strategy.conditions_file:
            return False, f"전략 {strategy_id}에 조건식 파일이 연결되지 않음"

        # 조건식 로드
        evaluator = ConditionEvaluator()
        from src.config import settings
        from pathlib import Path
        cond_path = Path(strategy.conditions_file)
        if not cond_path.exists():
            cond_path = settings.conditions_dir / strategy.conditions_file
        if not cond_path.exists():
            return False, f"조건식 파일 없음: {strategy.conditions_file}"

        evaluator.load_from_file(str(cond_path))
        stock_codes = evaluator.stock_codes()

        if not stock_codes:
            return False, "조건식에 감시 종목이 없음"

        # Runner 생성
        runner = StrategyRunner(
            strategy_id=strategy_id,
            evaluator=evaluator,
            stock_codes=stock_codes,
        )
        self._runners[strategy_id] = runner

        # WebSocket 종목 구독 (참조 카운트)
        for code in stock_codes:
            if code not in self._stock_refcount or self._stock_refcount[code] == 0:
                self._stock_refcount[code] = 0
                if self._running and self._ws._ws:
                    asyncio.create_task(self._ws.add_stock(code))
            self._stock_refcount[code] += 1

        # 전략 상태 저장
        self._strategy_mgr.update_strategy(strategy_id, active=True)

        logger.info("전략 활성화: %s (%d종목)", strategy_id, len(stock_codes))
        return True, f"전략 {strategy_id} 활성화 ({len(stock_codes)}종목 감시)"

    def deactivate(self, strategy_id: str) -> tuple[bool, str]:
        """전략 봇 정지 — Runner 제거 + 미사용 종목 구독 해제."""
        runner = self._runners.pop(strategy_id, None)
        if not runner:
            return False, f"전략 {strategy_id} 활성화되어 있지 않음"

        # 종목 참조 카운트 감소
        for code in runner.stock_codes:
            if code in self._stock_refcount:
                self._stock_refcount[code] -= 1
                if self._stock_refcount[code] <= 0:
                    del self._stock_refcount[code]
                    if self._running and self._ws._ws:
                        asyncio.create_task(self._ws.remove_stock(code))

        self._strategy_mgr.update_strategy(strategy_id, active=False)
        logger.info("전략 비활성화: %s", strategy_id)
        return True, f"전략 {strategy_id} 비활성화"

    def on_tick(self, tick: Tick):
        """실시간 체결 데이터 → 모든 활성 전략 평가."""
        # 지표 계산 (1회, 공유)
        ind = self._indicators.update(tick.stock_code, tick.price, tick.volume)

        # 이벤트 발행 (SSE/Redis 자동 분기)
        emit_event("tick", {
            "stock_code": tick.stock_code,
            "price": tick.price,
            "change_rate": tick.change_rate,
            "volume": tick.volume,
            "acc_volume": tick.acc_volume,
            "rsi": ind.rsi,
            "macd": ind.macd,
        })

        # 모든 활성 전략 평가
        for strategy_id, runner in self._runners.items():
            results = runner.evaluator.evaluate(tick.stock_code, ind)
            for result in results:
                if result.triggered and result.condition.id not in runner.triggered:
                    runner.triggered.add(result.condition.id)
                    emit_event("trigger", {
                        "strategy_id": strategy_id,
                        "condition_id": result.condition.id,
                        "stock_code": result.condition.stock_code,
                        "stock_name": result.condition.stock_name,
                        "action": result.condition.action,
                        "reason": result.condition.reason,
                        "matched_rules": result.matched_rules,
                    })

                    success = self._executor.execute(result)

                    emit_event("order", {
                        "strategy_id": strategy_id,
                        "stock_code": result.condition.stock_code,
                        "action": result.condition.action,
                        "success": success,
                    })

    async def start(self):
        """WebSocket 연결 시작 + 활성 전략 종목 구독."""
        if self._running:
            return

        self._running = True
        self._ws.on_tick = self.on_tick

        # 구독할 종목 수집
        all_codes = list(self._stock_refcount.keys())

        if all_codes:
            logger.info("BotManager 시작: %d개 전략, %d개 종목", len(self._runners), len(all_codes))
            self._ws_task = asyncio.create_task(self._ws.connect(all_codes))
        else:
            logger.info("BotManager 시작: 활성 전략 없음 (대기 중)")
            # 빈 상태로 대기 — 전략 활성화 시 종목 추가됨
            self._ws_task = asyncio.create_task(self._ws.connect([]))

    # 폴링 봇 슬롯 (레거시 ETF 봇 제거 후에도 호환성 위해 유지)
    _polling_bots: dict = {}

    async def stop(self):
        """전체 정지."""
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
        await self._ws.disconnect()
        # 폴링 봇도 정지
        for bot in list(self._polling_bots.values()):
            await bot.stop()
        self._polling_bots.clear()
        self._executor.close()
        logger.info("BotManager 정지")

    def status(self) -> dict:
        """전체 상태."""
        strategies = []
        for sid, runner in self._runners.items():
            s = self._strategy_mgr.get(sid)
            strategies.append({
                "strategy_id": sid,
                "name": s.name if s else "",
                "stock_codes": runner.stock_codes,
                "triggered_count": len(runner.triggered),
                "conditions_count": len(runner.evaluator.conditions),
            })
        return {
            "running": self._running,
            "dry_run": self._dry_run,
            "active_strategies": len(self._runners),
            "total_stocks": len(self._stock_refcount),
            "strategies": strategies,
            "polling_bots": {
                name: bot.status() for name, bot in self._polling_bots.items()
            },
        }

    async def auto_restore(self):
        """서버 시작 시 활성 전략/파이프 자동 복구."""
        restored = 0
        for s in self._strategy_mgr.list_strategies():
            if not getattr(s, 'active', False):
                continue

            # 1. 조건식 기반 전략 복구
            if s.conditions_file:
                ok, msg = self.activate(s.id)
                if ok:
                    logger.info("전략 복구: %s", msg)
                    restored += 1

        if self._runners:
            await self.start()

        logger.info("자동 복구 완료: %d개 항목", restored)


# 싱글톤
_manager: BotManager | None = None


def get_bot_manager(dry_run: bool = True) -> BotManager:
    global _manager
    if _manager is None:
        _manager = BotManager(dry_run=dry_run)
    elif _manager._dry_run != dry_run:
        # dry_run 상태가 다르면 업데이트 (싱글톤 초기화 순서 버그 방지)
        _manager._dry_run = dry_run
        _manager._executor = TradeExecutor(dry_run=dry_run)
        logger.info("BotManager dry_run 상태 변경: %s", dry_run)
    return _manager
