"""Pipe Runner — 독립 프로세스로 파이프 실행.

Usage:
    python -m src.services.pipe_runner strat-001
"""

import asyncio
import logging
import os
import signal
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))

from src.services.redis_bus import get_bus
from src.strategy import StrategyManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pipe-runner")


class PipeRunner:
    """독립 프로세스에서 파이프 실행 (전략 + 복수 봇)."""

    def __init__(self, pipe_id: str):
        self.pipe_id = pipe_id
        self.bus = get_bus()
        self.bots: list = []  # 복수 봇 인스턴스
        self._running = False

    def _on_command(self, msg: dict):
        if msg.get("service_id") != self.pipe_id:
            return
        action = msg.get("action", "")
        logger.info("명령 수신: %s", action)
        if action == "stop":
            self._running = False
        elif action == "set_bot_interval":
            self._apply_bot_interval(msg.get("config", {}))

    def _apply_bot_interval(self, config: dict):
        """실행 중 봇의 분석 주기를 라이브 갱신 (다음 사이클부터 적용)."""
        from src.bots import registry
        bot_id = config.get("bot_id", "")
        try:
            seconds = int(config.get("interval_seconds"))
        except (TypeError, ValueError):
            logger.warning("set_bot_interval: 잘못된 interval_seconds: %r",
                           config.get("interval_seconds"))
            return
        spec = registry.interval_spec(bot_id)
        if spec:
            seconds = max(spec["min"], min(spec["max"], seconds))
        for bot in self.bots:
            if getattr(bot, "BOT_ID", None) == bot_id:
                bot._config.interval_seconds = seconds
                bot._params["interval_seconds"] = seconds
                logger.info("[%s] 분석 주기 변경 → %d초 (다음 사이클부터)",
                            bot_id, seconds)
                return

    def _entries_with_position_custodians(self, strategy) -> list[dict]:
        """Return active bot entries plus inactive bots that still own open lots."""
        entries = list(strategy.active_bots())
        active_ids = {entry.get("bot_id") for entry in entries}
        entries.extend(self._position_custodian_entries(strategy, active_ids))
        return entries

    def _position_custodian_entries(self, strategy, active_ids: set[str]) -> list[dict]:
        """Find removed/disabled bots that still have unsold pipe holdings."""
        holdings = {
            h.get("code"): int(h.get("qty", 0) or 0)
            for h in (strategy.holdings or [])
            if h.get("code") and int(h.get("qty", 0) or 0) > 0
        }
        if not holdings:
            return []

        try:
            from src.bots.registry import get as get_bot_info
            from src.db import get_db
        except Exception as e:
            logger.warning("포지션 관리 봇 조회 준비 실패: %s", e)
            return []

        try:
            trades = get_db().get_trades(pipe_id=strategy.id, limit=1000)
        except Exception as e:
            logger.warning("포지션 관리 봇 거래 이력 조회 실패: %s", e)
            return []

        lots: dict[tuple[str, str], list[dict]] = {}
        for trade in sorted(trades, key=lambda t: t.get("timestamp", "")):
            source = trade.get("source", "")
            code = trade.get("stock_code", "")
            qty = int(trade.get("quantity", 0) or 0)
            price = int(trade.get("price", 0) or 0)
            if not trade.get("success", True) or not source or not code or qty <= 0:
                continue
            key = (source, code)
            if trade.get("side") == "buy":
                lots.setdefault(key, []).append({"qty": qty, "price": price})
                continue
            if trade.get("side") == "sell":
                remaining = qty
                for lot in lots.get(key, []):
                    if remaining <= 0:
                        break
                    take = min(remaining, int(lot.get("qty", 0) or 0))
                    lot["qty"] -= take
                    remaining -= take

        custodians = []
        seen = set(active_ids)
        for (bot_id, code), code_lots in lots.items():
            if bot_id in seen or not get_bot_info(bot_id):
                continue
            owned_qty = sum(int(lot.get("qty", 0) or 0) for lot in code_lots)
            if owned_qty <= 0 or holdings.get(code, 0) <= 0:
                continue
            custodians.append({
                "bot_id": bot_id,
                "ratio": 0.0,
                "budget": 0,
                "params": {
                    "position_custodian": True,
                    "max_positions": 0,
                },
                "enabled": True,
            })
            seen.add(bot_id)

        if custodians:
            logger.warning(
                "파이프 %s 포지션 관리 전용 봇 추가: %s",
                strategy.id,
                ", ".join(entry["bot_id"] for entry in custodians),
            )
        return custodians

    async def _status_loop(self):
        """파이프 상태를 Redis에 발행."""
        while self._running:
            try:
                mgr = StrategyManager()
                summary = mgr.pipe_summary(self.pipe_id)
                if summary:
                    bot_statuses = {b.BOT_ID: b.status() for b in self.bots}
                    summary["bot_statuses"] = bot_statuses
                    self.bus.set_status("pipe", self.pipe_id, summary, ttl=300)
                    self.bus.heartbeat("pipe", self.pipe_id)
            except Exception as e:
                logger.warning("상태 발행 실패: %s", e)
            await asyncio.sleep(10)

    async def run(self):
        logger.info("Pipe Runner 시작: %s (PID: %d)", self.pipe_id, os.getpid())

        # 전략 로드
        mgr = StrategyManager()
        strategy = mgr.get(self.pipe_id)
        if not strategy:
            logger.error("파이프 %s 없음", self.pipe_id)
            return

        # Redis 등록
        self.bus.register_process("pipe", self.pipe_id, os.getpid())
        self.bus.subscribe_commands("pipe", self._on_command)

        # 봇 인스턴스 생성 전, KIS 실잔고를 truth로 잡고 phantom 정리.
        # 봇 생성 전에 하면 봇들이 깨끗한 ledger에서 _reconcile_*을 돌리므로
        # phantom이 재생성되지 않는다. KIS 조회 실패 시 pipe.holdings로 폴백.
        try:
            from src.services.phantom_cleanup import cleanup_pipe
            from src.kis.market import KISMarket
            holdings = {h.get("code", ""): int(h.get("qty", 0) or 0)
                        for h in (strategy.holdings or [])}
            holdings = {k: v for k, v in holdings.items() if k}

            kis_balance = None
            try:
                m = KISMarket()
                try:
                    bal = m.get_balance() or {}
                    # 에러 응답이 dict로 돌아오는 경우(holdings 키 없음)는 실패로 취급
                    if "holdings" not in bal:
                        raise RuntimeError(f"잔고 응답 형식 이상: keys={list(bal.keys())}")
                    kis_balance = {h.get("stock_code", ""): int(h.get("quantity", 0) or 0)
                                   for h in (bal.get("holdings") or [])
                                   if h.get("stock_code")}
                finally:
                    m.close()
            except Exception as e:
                logger.warning("[%s] KIS 잔고 조회 실패 → pipe.holdings로 폴백: %s",
                               strategy.id, e)

            # 장애 의심 가드 (2026-06-12 실사고): KIS가 '성공'하면서 빈 보유를
            # 반환했는데 파이프는 보유를 기억하고 있다면, 잔고 API 장애일 가능성이
            # 크므로 phantom 정리를 건너뛴다. 빈 응답을 truth로 믿으면 봇들의
            # 정상 lot 전부에 합성 SELL이 기입된다.
            if kis_balance is not None and not kis_balance and holdings:
                logger.warning(
                    "[%s] KIS 잔고 빈 응답 + pipe 보유 %d종목 — 장애 의심, phantom 정리 생략",
                    strategy.id, len(holdings))
            else:
                report = cleanup_pipe(pipe_id=strategy.id,
                                      pipe_holdings=holdings,
                                      kis_balance=kis_balance)
                if report["cleanups"]:
                    logger.warning("[%s] phantom 정리 %d건 적용",
                                   strategy.id, len(report["cleanups"]))
        except Exception as e:
            logger.warning("phantom 정리 실패: %s", e)

        # 복수 봇 생성 (strategy.bots 리스트 + 열린 포지션 관리 봇 순회)
        from src.bots.factory import create_bot
        bot_entries = self._entries_with_position_custodians(strategy)
        if not bot_entries:
            logger.warning("파이프 %s에 연결된 봇이 없음", self.pipe_id)
            return

        bot_names = []
        for entry in bot_entries:
            bot_id = entry["bot_id"]
            ratio = entry.get("ratio", 1.0)
            bot_budget = int(strategy.budget * ratio)
            instance_id = f"{strategy.id}:{bot_id}"
            params = entry.get("params", {})

            bot = create_bot(bot_id, budget=bot_budget, instance_id=instance_id, params=params)
            if bot:
                # ratio 0% 봇은 보유분 관리(매도) 전용 — 신규 매수 금지.
                # budget 0이 엔진에서 '무제한'으로 해석되는 것을 차단 (2026-06-12 실사고).
                if bot_budget <= 0:
                    bot.engine.buy_block_reason = "ratio 0% — 매수 비활성 (보유분 관리 전용)"
                # 봇 가상 계좌 cash를 자기 trades에서 재계산 (재시작 시 ledger 복원)
                try:
                    bot.engine.refresh_cash()
                except Exception as e:
                    logger.warning("[%s] cash refresh 실패: %s", bot_id, e)
                self.bots.append(bot)
                bot_names.append(
                    f"{bot_id}({int(ratio*100)}%={bot_budget:,}원, cash={bot.engine.cash:,}원)"
                )
                asyncio.create_task(bot.start())
            else:
                logger.warning("봇 생성 실패: %s", bot_id)

        self.bus.publish_event("system", {
            "message": f"파이프 '{strategy.name}' 시작 — {', '.join(bot_names)}"
        })

        self._running = True
        status_task = asyncio.create_task(self._status_loop())

        # 종료 시그널
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: setattr(self, '_running', False))

        while self._running:
            await asyncio.sleep(1)

        # 정리: 모든 봇 정지
        for bot in self.bots:
            await bot.stop()
        status_task.cancel()
        self.bus.publish_event("system", {"message": f"파이프 '{strategy.name}' 정지"})
        self.bus.close()
        logger.info("Pipe Runner 정지: %s (%d개 봇 종료)", self.pipe_id, len(self.bots))



def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.services.pipe_runner <pipe-id>")
        sys.exit(1)
    runner = PipeRunner(sys.argv[1])
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
