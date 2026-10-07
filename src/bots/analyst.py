"""시장 분석가 봇 — 매매 없이 차트+뉴스 분석 후 짧은 의견을 게시판에 등록.

주식 커뮤니티처럼 다른 봇들이 참고할 수 있는 시장 인사이트 제공.
15분 간격으로 AI 호출, 1~2문장 의견만 게시.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from src.bots.base import BotBase, BotConfig
from src.bots.safeguards import MarketCloseGuard, load_bot_config, write_bot_log
from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "interval_seconds": 900,  # 15분
    "codex_timeout": 90,      # 빠른 응답 (의견만)
    "max_opinions_per_day": 20,
    "ai_provider": "codex",
    "ai_model": "",
}

_ANALYST_PROMPT = """data/bot_rules/analyst.md를 읽으세요.

아래는 실시간 시장 데이터입니다. 이 데이터만으로 짧은 시장 의견을 작성하세요.
외부 API/네트워크 호출하지 마세요.

{context}

## 규칙
- 1~2문장, 최대 100자
- 감성 태그 필수: 🟢 호재 / 🔴 악재 / ⚠️ 주의 / 📊 중립
- 종목코드 포함 (해당되면)
- 여러 개 의견이면 줄바꿈으로 구분

## 출력 형식 (stdout에 JSON)
```json
{{
  "opinions": [
    {{"code": "091160", "content": "🟢 091160 반도체 외인 순매수 확대"}},
    {{"code": "", "content": "⚠️ 전체 시장 변동성 확대, 신중 접근 필요"}}
  ]
}}
```"""


class AnalystBot(BotBase):
    """시장 분석가 — 의견 전용 봇. 매매하지 않음."""

    BOT_ID = "analyst"

    def __init__(self, config: BotConfig | dict = None):
        self._params = {**DEFAULT_PARAMS}
        evolver_cfg = load_bot_config("analyst")
        self._params.update(evolver_cfg)

        if config is None:
            config = BotConfig(interval_seconds=self._params["interval_seconds"])
        elif isinstance(config, BotConfig) and config.interval_seconds in (30, 60, 300):
            config.interval_seconds = self._params["interval_seconds"]

        super().__init__(config)
        self._market_guard = MarketCloseGuard()
        self._opinion_count = 0

    # ─── 데이터 수집 (Python) ───

    def _collect_data(self) -> str:
        """시세+뉴스 수집."""
        market = self._get_market()
        etf_lines = []
        for code in ["069500", "091160", "091170", "102970", "117680",
                      "122630", "130680", "132030", "305720"]:
            try:
                info = market.get_price(code)
                if info:
                    etf_lines.append(
                        f"  {code} {info.get('name','')}: "
                        f"{info.get('price',0):,}원 ({info.get('change_rate',0):+.2f}%) "
                        f"거래량 {info.get('volume',0):,}"
                    )
            except Exception:
                pass

        news_lines = []
        try:
            db_path = PROJECT_ROOT / "data" / "ff.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT title, sentiment FROM news ORDER BY timestamp DESC LIMIT 7"
            ).fetchall()
            news_lines = [f"  [{r['sentiment'] or '?'}] {r['title']}" for r in rows]
            conn.close()
        except Exception:
            pass

        return f"""[ETF 시세]
{chr(10).join(etf_lines) or '  (조회 실패)'}

[최근 뉴스]
{chr(10).join(news_lines) or '  (뉴스 없음)'}
"""

    # ─── AI 호출 ───

    def _invoke_codex(self, context: str) -> list[dict]:
        """AI 호출 → 의견 리스트 반환."""
        from src.bots.ai_caller import invoke_ai
        import re

        prompt = _ANALYST_PROMPT.format(context=context)
        timeout = self._params.get("codex_timeout", 90)
        provider = self._params.get("ai_provider", "codex")

        try:
            output = invoke_ai(prompt, self._params, timeout=timeout, bot_id=self.BOT_ID)
            if not output:
                return []

            # JSON 파싱
            match = re.search(r'\{[\s\S]*"opinions"[\s\S]*\}', output)
            if match:
                data = json.loads(match.group())
                return data.get("opinions", [])
        except Exception as e:
            logger.warning("[%s] AI 호출 실패 (%s): %s", self.BOT_ID, provider, e)
        return []

    # ─── 메인 루프 ───

    def scan(self):
        return []

    def decide(self, candidates):
        return {"action": "skip"}

    def execute(self, decision):
        return {"success": True}

    async def _loop(self):
        """15분 간격으로 시장 의견 게시."""
        logger.info("[%s] 시작 — %ds 간격, 의견 전용", self.BOT_ID, self._config.interval_seconds)

        while self._running:
            try:
                self._scan_count += 1

                if self._market_guard.is_market_open() and self._opinion_count < self._params["max_opinions_per_day"]:
                    loop = asyncio.get_running_loop()

                    # 데이터 수집
                    context = await loop.run_in_executor(None, self._collect_data)

                    # AI 분석
                    opinions = await loop.run_in_executor(None, self._invoke_codex, context)

                    # 게시판에 등록
                    for op in opinions:
                        content = op.get("content", "")
                        code = op.get("code", "")
                        if content:
                            self._post_to_board("opinion", content, code)
                            self._opinion_count += 1
                            logger.info("[%s] 의견 등록: %s", self.BOT_ID, content[:60])

                    # 로그
                    write_bot_log("analyst", {
                        "scan": self._scan_count,
                        "time": datetime.now().isoformat(),
                        "opinions": len(opinions),
                        "details": [o.get("content", "")[:80] for o in opinions],
                    })

                # 첫 스캔 완료 로그
                if self._scan_count == 1:
                    logger.info("[%s] 첫 분석 완료, 이후 %d초 간격", self.BOT_ID, self._config.interval_seconds)

            except Exception as e:
                logger.error("[%s] 오류: %s", self.BOT_ID, e, exc_info=True)

            await asyncio.sleep(self._config.interval_seconds)

    def status(self) -> dict:
        return {
            "running": self._running,
            "scan_count": self._scan_count,
            "opinion_count": self._opinion_count,
            "pnl": {"realized": 0, "unrealized": 0, "total": 0},
        }
