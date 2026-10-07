"""자동 조율 봇 — AI CLI로 봇 성과를 분석하고 파라미터를 조정.

AI가 AGENTS.md를 읽고, 봇 로그/config 파일을 직접 읽고,
curl로 API를 호출하여 분석한 뒤, config 파일을 직접 수정합니다.

파이프에 연결하여 1시간 간격으로 실행.
내부적으로 ai_caller.invoke_ai()를 호출합니다.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from src.bots.safeguards import (
    kst_now,
    load_bot_config,
    EVOLUTION_DIR,
)
from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# AI에 전달할 프롬프트
_CODEX_PROMPT = (
    "AGENTS.md를 읽고, 에볼버 모드로 실행하세요. "
    "MCP 도구(ff-trading)를 사용하여 봇 룰/성과/뉴스/시장 데이터를 조회하세요. "
    "data/bot_rules/evolver.md의 룰을 반드시 먼저 읽으세요. "
    "\n** 거래 데이터 필터링 필수: trades에서 reason='phantom-cleanup' 행은 합성 보정 거래이므로 "
    "성과·수익 계산에서 반드시 제외하세요 (BotLedger와 동일 기준). 포함 시 수익이 부풀어집니다. **\n"
    "\n** 봇 간 비교 기준: 누적 절대 손익으로 봇을 비교하지 마세요. "
    "(1) 분모는 '할당 예산'이 아니라 '실제 투입 자본(peak_capital = max cumulative_buy - cumulative_sell)'을 사용. "
    "(2) 봇별 '거래일수(distinct DATE(timestamp))'로 정규화한 '일평균 수익률'로 비교. "
    "(3) A·B 비교 시 공통 거래 날짜 범위만 사용. 가동 기간 차이를 절대 무시하지 마세요. **\n"
    "\n** AI 설정 조정 가능: ai_provider (claude/codex/copilot), ai_model **\n"
    "분석 결과를 data/evolution/gen_{next_gen:04d}.json에 저장하세요. "
    "파라미터 조정이 필요하면 save_bot_config() MCP 도구를 사용하세요. "
    "결과를 한국어로 간결하게 stdout에 보고하세요."
)


class Evolver:
    """자동 조율 봇 — AI CLI를 호출하여 AI 판단으로 파라미터 조정.

    파이프에 연결하여 사용. 직접 매매하지 않음 (예산 0원).
    1시간마다 AI를 호출하여 분석/조정.
    """

    BOT_ID = "evolver"
    MANAGED_BOTS = ["stealth-volume", "band-rebound"]
    DEFAULT_INTERVAL = 3600  # 1시간

    def __init__(self, config=None):
        self._running = False
        self._task = None
        self._generation = self._load_generation()
        self._interval = self.DEFAULT_INTERVAL
        self._last_result = None
        logger.info(
            "[자동 조율] 초기화 — generation=%d, 관리 봇=%s",
            self._generation,
            self.MANAGED_BOTS,
        )

    # ─────────────────────────────────────
    # 라이프사이클 (파이프 연동)
    # ─────────────────────────────────────

    async def start(self):
        """봇 시작 — 파이프에서 호출."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[자동 조율] 시작 (간격: %ds)", self._interval)

    async def stop(self):
        """봇 정지."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[자동 조율] 정지")

    async def _loop(self):
        """1시간 간격으로 AI CLI 호출."""
        while self._running:
            try:
                result = await self._invoke_ai()
                self._last_result = {
                    "time": kst_now().isoformat(),
                    "generation": self._generation,
                    "result": result,
                }
                self._generation = self._load_generation()
                logger.info("[자동 조율] 분석 완료 (gen %d)", self._generation)
            except Exception:
                logger.exception("[자동 조율] AI 호출 중 오류")

            await asyncio.sleep(self._interval)

    # ─────────────────────────────────────
    # AI CLI 호출
    # ─────────────────────────────────────

    async def _invoke_ai(self) -> str:
        """AI CLI를 호출하여 분석 실행."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run_codex_sync)

    def _run_codex_sync(self) -> str:
        """동기 AI CLI 호출."""
        from src.bots.ai_caller import invoke_ai

        next_gen = self._generation + 1
        prompt = _CODEX_PROMPT.format(next_gen=next_gen)

        provider = self._params.get("ai_provider", "codex") if hasattr(self, '_params') else "codex"
        logger.info("[자동 조율] AI 호출 시작 (gen %d, provider=%s)...", next_gen, provider)

        output = invoke_ai(prompt, self._params if hasattr(self, '_params') else {}, timeout=300, bot_id="evolver")

        if not output:
            return "ERROR: AI 호출 실패"

        # gen 파일 저장
        gen_file = EVOLUTION_DIR / f"gen_{next_gen:04d}.json"
        if not gen_file.exists():
            self._save_analysis_log(output)

        logger.info("[자동 조율] AI 분석 완료 (%d자)", len(output))
        return output

    # ─────────────────────────────────────
    # 수동 실행 (MCP 도구용)
    # ─────────────────────────────────────

    def run(self) -> dict:
        """수동 1회 실행 (동기). run_evolver() MCP 도구에서 호출."""
        result = self._run_codex_sync()
        self._generation = self._load_generation()
        return {"generation": self._generation, "output": result}

    async def run_async(self) -> dict:
        """비동기 1회 실행."""
        result = await self._invoke_ai()
        self._generation = self._load_generation()
        return {"generation": self._generation, "output": result}

    # ─────────────────────────────────────
    # 상태 조회
    # ─────────────────────────────────────

    def get_status(self) -> dict:
        """현재 자동 조율 봇 상태."""
        configs = {}
        for bot_id in self.MANAGED_BOTS:
            configs[bot_id] = load_bot_config(bot_id)
        return {
            "generation": self._generation,
            "managed_bots": self.MANAGED_BOTS,
            "running": self._running,
            "last_result": self._last_result,
            "current_configs": configs,
        }

    def status(self) -> dict:
        """웹 대시보드용 상태."""
        return self.get_status()

    # ─────────────────────────────────────
    # 내부 유틸
    # ─────────────────────────────────────

    def _save_analysis_log(self, output: str):
        """AI 분석 결과를 파일에 기록."""
        log_entry = {
            "generation": self._generation + 1,
            "timestamp": kst_now().isoformat(),
            "type": "ai_analysis",
            "engine": self._params.get("ai_provider", "codex") if hasattr(self, '_params') else "codex",
            "output": output[:5000],
        }
        log_file = EVOLUTION_DIR / f"gen_{self._generation + 1:04d}.json"
        try:
            log_file.write_text(
                json.dumps(log_entry, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("[자동 조율] 분석 로그 저장 실패")

    def _load_generation(self) -> int:
        """마지막 generation 번호 로드."""
        files = sorted(EVOLUTION_DIR.glob("gen_*.json"))
        if files:
            try:
                name = files[-1].stem
                return int(name.split("_")[1])
            except (ValueError, IndexError):
                pass
        return 0
