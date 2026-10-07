"""Bot Runner — 독립 프로세스로 봇 실행.

Usage:
    python -m src.services.bot_runner etf-momentum-top1
    python -m src.services.bot_runner etf-volume-spike
"""

import asyncio
import logging
import os
import signal
import sys
import time

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))

from src.services.redis_bus import get_bus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot-runner")


class BotRunner:
    """독립 프로세스에서 봇 실행."""

    def __init__(self, bot_id: str, config: dict = None):
        self.bot_id = bot_id
        self.config = config or {}
        self.bus = get_bus()
        self.bot = None
        self._running = False

    def _create_bot(self):
        """봇 ID에 맞는 봇 인스턴스 생성."""
        from src.bots.factory import create_bot
        bot = create_bot(self.bot_id, params=self.config if self.config else None)
        if not bot:
            raise ValueError(f"알 수 없는 봇: {self.bot_id}")
        return bot

    def _on_command(self, msg: dict):
        """Redis 명령 수신 핸들러."""
        if msg.get("service_id") != self.bot_id:
            return
        action = msg.get("action", "")
        logger.info("명령 수신: %s", action)

        if action == "stop":
            self._running = False
        elif action == "restart":
            self._running = False  # 루프 종료 후 재시작
        elif action == "config":
            self.config = msg.get("config", {})

    async def _status_loop(self):
        """주기적으로 상태를 Redis에 발행."""
        while self._running:
            try:
                if self.bot:
                    status = self.bot.status()
                    self.bus.set_status("bot", self.bot_id, status, ttl=60)
                    self.bus.heartbeat("bot", self.bot_id)
            except Exception as e:
                logger.warning("상태 발행 실패: %s", e)
            await asyncio.sleep(5)

    async def run(self):
        """봇 실행 메인 루프."""
        logger.info("Bot Runner 시작: %s (PID: %d)", self.bot_id, os.getpid())

        # Redis에 프로세스 등록
        self.bus.register_process("bot", self.bot_id, os.getpid())

        # 명령 채널 구독
        self.bus.subscribe_commands("bot", self._on_command)

        # 봇 생성 + 시작
        self.bot = self._create_bot()
        self._running = True

        # 이벤트 발행
        self.bus.publish_event("system", {"message": f"봇 '{self.bot_id}' 독립 프로세스 시작"})

        # 봇 실행 + 상태 발행 병렬
        bot_task = asyncio.create_task(self.bot.start())
        status_task = asyncio.create_task(self._status_loop())

        # 종료 시그널 처리
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown())

        # 봇이 끝나거나 stop 명령까지 대기
        while self._running:
            await asyncio.sleep(1)

        # 정리
        logger.info("Bot Runner 정지: %s", self.bot_id)
        await self.bot.stop()
        status_task.cancel()
        self.bus.publish_event("system", {"message": f"봇 '{self.bot_id}' 정지"})
        self.bus.close()

    def _shutdown(self):
        logger.info("종료 시그널 수신")
        self._running = False


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.services.bot_runner <bot-id> [config-json]")
        print("  bot-id: etf-momentum-top1, etf-volume-spike")
        sys.exit(1)

    bot_id = sys.argv[1]
    config = {}
    if len(sys.argv) > 2:
        import json
        config = json.loads(sys.argv[2])

    runner = BotRunner(bot_id, config)
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
