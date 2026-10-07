"""Bot Builder — AI가 봇 코드를 설계/생성/등록하는 에이전트.

채팅에서 "RSI 30 이하에서 매수하는 봇 만들어줘" →
AI가 BotBase 상속 코드 + BotInfo + BotUISpec을 생성하여 시스템에 등록.
"""

import json
import logging
import re
from pathlib import Path

from src.config import PROJECT_ROOT
from src.bots.ai_caller import invoke_ai

logger = logging.getLogger(__name__)

GENERATED_DIR = PROJECT_ROOT / "src" / "bots" / "generated"
CUSTOM_REGISTRY = PROJECT_ROOT / "data" / "bot_registry_custom.json"

BUILDER_PROMPT = """당신은 ZeroTrade 봇 빌더입니다.
사용자의 전략 설명을 Python 봇 코드로 변환합니다.

## 규칙
1. BotBase를 상속하여 scan(), decide(), execute() 구현
2. self._config.budget 범위 안에서만 주문 (self.check_budget() 사용)
3. self.get_buyable_qty()로 실제 매수가능수량 확인
4. self.buy(code, qty, price), self.sell()로 매매 (DB 자동 기록)
5. BotUISpec 위젯을 정의하여 대시보드 UI 자동 생성

## BotBase 구조
```python
from src.bots.base import BotBase, BotConfig

class MyBot(BotBase):
    BOT_ID = "my-bot-id"  # 고유 ID

    def __init__(self, config=None):
        super().__init__(config or BotConfig(interval_seconds=300))
        # 봇 고유 상태 초기화

    def scan(self) -> list[dict]:
        market = self._get_market()
        # 시세 조회: market.get_price(code), market.get_minute_price(code)
        # market.get_etf_ranking(), market.get_daily_price(code)
        return [{"code": "069500", "score": 1.5, ...}]

    def decide(self, candidates) -> dict:
        # 매수/매도/홀드/스킵 결정
        return {"action": "buy", "code": "069500", "reason": "..."}

    def execute(self, decision) -> dict:
        if decision["action"] == "buy":
            qty = self.get_buyable_qty(decision["code"], price)
            return self.buy(decision["code"], qty, price)
        elif decision["action"] == "sell":
            return self.sell()
        return {"action": "skip"}
```

## 출력 형식 (반드시 JSON 블록으로)
```json
{
  "bot_id": "kebab-case-id",
  "name": "한글 봇 이름",
  "description": "한 줄 설명",
  "strategy": "전략 상세 설명",
  "risk_level": 3,
  "category": "모멘텀|역추세|차익|배당/수익|매크로|커스텀",
  "tags": ["ETF", "모멘텀"],
  "code": "전체 Python 코드 (imports 포함)",
  "ui_widgets": [
    {"id": "status", "type": "badge", "label": "상태", "size": "1x1", "data_key": "running", "color_rule": "status"},
    {"id": "pnl", "type": "stat", "label": "총 손익", "size": "1x1", "data_key": "pnl.total", "format": "won", "color_rule": "pnl"}
  ],
  "params": [
    {"name": "interval_seconds", "label": "스캔 간격", "type": "int", "default": 300, "unit": "초"}
  ]
}
```

## 사용자 전략 설명
"""


class BotBuilder:
    """AI 봇 빌더."""

    def __init__(self, timeout: int = 600):
        self._timeout = timeout

    def build(self, description: str) -> dict:
        """전략 설명 → AI가 봇 코드 생성 → 파일 저장 → registry 등록.

        Returns:
            {"success": True, "bot_id": "...", "name": "...", "file": "..."}
        """
        logger.info("봇 빌더 시작: %s", description[:80])

        # 1. AI 호출
        prompt = BUILDER_PROMPT + description
        response = self._call_ai(prompt)
        if not response:
            return {"success": False, "error": "AI 응답 없음"}

        # 2. JSON 파싱
        bot_spec = self._parse_response(response)
        if not bot_spec:
            return {"success": False, "error": "봇 스펙 파싱 실패", "raw": response[:500]}

        # 3. 코드 저장
        bot_id = bot_spec["bot_id"]
        file_path = self._save_code(bot_id, bot_spec.get("code", ""))
        if not file_path:
            return {"success": False, "error": "코드 저장 실패"}

        # 4. 레지스트리 등록
        self._register(bot_spec)

        logger.info("봇 생성 완료: %s (%s)", bot_id, bot_spec.get("name", ""))
        return {
            "success": True,
            "bot_id": bot_id,
            "name": bot_spec.get("name", ""),
            "description": bot_spec.get("description", ""),
            "file": str(file_path),
        }

    def _call_ai(self, prompt: str) -> str:
        """agentcli 경유 AI 호출 (Claude 기본, 봇 빌더 전용 conversation)."""
        try:
            return invoke_ai(
                prompt,
                params={"ai_provider": "claude"},
                timeout=self._timeout,
                bot_id="bot-builder",
                alias="bot-builder",
                cwd=str(PROJECT_ROOT),
            )
        except Exception as e:
            logger.error("AI 호출 실패: %s", e)
            return ""

    def _parse_response(self, response: str) -> dict | None:
        """AI 응답에서 봇 스펙 JSON 추출."""
        # ```json ... ``` 블록 추출
        match = re.search(r"```json\s*\n?(.*?)\n?\s*```", response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # 순수 JSON
        match = re.search(r"\{[\s\S]*\}", response)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None

    def _save_code(self, bot_id: str, code: str) -> Path | None:
        """봇 코드를 파일로 저장."""
        if not code:
            return None
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"bot_{bot_id.replace('-', '_')}.py"
        path = GENERATED_DIR / filename
        path.write_text(code, encoding="utf-8")
        logger.info("봇 코드 저장: %s", path)
        return path

    def _register(self, spec: dict):
        """커스텀 레지스트리에 등록 (영속)."""
        # 파일 기반 영속 레지스트리
        registry = []
        if CUSTOM_REGISTRY.exists():
            try:
                registry = json.loads(CUSTOM_REGISTRY.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                registry = []

        # 중복 제거
        registry = [r for r in registry if r.get("bot_id") != spec["bot_id"]]
        registry.append({
            "bot_id": spec["bot_id"],
            "name": spec.get("name", ""),
            "description": spec.get("description", ""),
            "strategy": spec.get("strategy", ""),
            "risk_level": spec.get("risk_level", 3),
            "category": spec.get("category", "커스텀"),
            "tags": spec.get("tags", []),
            "ui_widgets": spec.get("ui_widgets", []),
            "params": spec.get("params", []),
            "module": f"src.bots.generated.bot_{spec['bot_id'].replace('-', '_')}",
            "created_at": __import__("datetime").datetime.now().isoformat(),
        })

        CUSTOM_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        CUSTOM_REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")

        # 메모리 레지스트리에도 등록
        try:
            from src.bots.registry import register, BotInfo, BotCategory, BotType, MarketSession, BotUISpec, Widget, WidgetType, BotParam
            cat_map = {"모멘텀": BotCategory.MOMENTUM, "역추세": BotCategory.MEAN_REVERT, "커스텀": BotCategory.CUSTOM}
            register(BotInfo(
                id=spec["bot_id"],
                name=spec.get("name", ""),
                version="1.0.0",
                category=cat_map.get(spec.get("category", ""), BotCategory.CUSTOM),
                bot_type=BotType.POLLING,
                market_session=MarketSession.REGULAR,
                description=spec.get("description", ""),
                strategy=spec.get("strategy", ""),
                risk_level=spec.get("risk_level", 3),
                tags=spec.get("tags", []),
                author="AI Builder",
                module=f"src.bots.generated.bot_{spec['bot_id'].replace('-', '_')}",
                params=[BotParam(**p) for p in spec.get("params", [])],
                ui=BotUISpec(widgets=[
                    Widget(
                        id=w["id"], type=WidgetType(w["type"]), label=w["label"],
                        size=w.get("size", "1x1"), data_key=w.get("data_key", ""),
                        format=w.get("format", ""), color_rule=w.get("color_rule", ""),
                        columns=w.get("columns", []),
                    ) for w in spec.get("ui_widgets", [])
                ]),
            ))
        except Exception as e:
            logger.warning("메모리 레지스트리 등록 실패: %s", e)
