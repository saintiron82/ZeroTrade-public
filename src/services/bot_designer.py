"""봇 디자이너 — 자연어 → 조건 스펙 JSON 변환 (Claude CLI 사용).

사용자의 전략 설명을 분석하여 TemplateBot용 스펙 JSON을 생성.
"""

import json
import logging
import re

from src.bots.condition_engine import INDICATOR_META
from src.bots.template_bot import save_spec, SPEC_DIR

logger = logging.getLogger(__name__)

# AI에게 전달할 프롬프트
_DESIGNER_PROMPT = """\
당신은 ZeroTrade 봇 설계자입니다.
사용자의 자연어 전략 설명을 분석하여, 아래 JSON 템플릿의 빈 칸을 채워주세요.

## 사용 가능한 지표 (indicator)

| indicator | 설명 | operator | 값 범위 |
|-----------|------|----------|---------|
| rsi | RSI (0~100) | <=, >= | 0~100, period=14 |
| vol_spike | 거래량 배율 | >= | 1.0~10.0 |
| bb_position | 볼린저밴드 %B | <=, >= | -1~2, period=20 |
| price_above_vwap | VWAP 위 여부 | ==, != | true/false |
| price_change_pct | 가격 변동률(%) | >=, <= | -10~10 |
| macd_cross | MACD 크로스 | == | "golden"/"dead" |
| sma_cross | SMA 크로스(5/20) | == | "golden"/"dead" |
| price_vs_sma | SMA 이격도(%) | >=, <= | -10~10, period=20 |

## JSON 템플릿

```json
{{
  "bot_id": "kebab-case-id",
  "name": "한글 봇 이름",
  "description": "전략 한 줄 설명",
  "category": "모멘텀|역추세|커스텀",
  "interval_seconds": 60,
  "target_codes": ["069500", "091160", "305720"],
  "entry": {{
    "logic": "AND",
    "conditions": [
      {{"indicator": "...", "operator": "...", "value": ..., "period": ...}}
    ]
  }},
  "exit": {{
    "trailing_stop_pct": -0.8,
    "take_profit_pct": 1.5,
    "hard_stop_pct": -2.0,
    "conditions": []
  }},
  "limits": {{
    "max_daily_trades": 3,
    "min_hold_minutes": 15,
    "max_positions": 1,
    "market_close_force": true
  }}
}}
```

## 규칙
- entry.conditions에 최소 1개 이상 조건 필수
- indicator는 위 표의 값만 사용
- operator는 >=, <=, >, <, ==, != 만 사용
- bot_id는 영문 kebab-case (예: rsi-oversold-bounce)
- target_codes는 한국 ETF 코드 (기본: 069500, 091160, 305720, 102110, 122630)
- category는 "모멘텀", "역추세", "커스텀" 중 하나
- JSON만 출력하세요. 설명이나 마크다운 없이 순수 JSON만.

## 사용자 요청

{description}
"""

# 프리셋 전략
PRESETS = {
    "거래량_폭발": {
        "bot_id": "vol-spike-preset",
        "name": "거래량 폭발 추적",
        "description": "거래량 3배 폭발 + 가격 상승 시 매수",
        "category": "모멘텀",
        "interval_seconds": 30,
        "target_codes": ["069500", "091160", "102110", "122630", "305720"],
        "entry": {
            "logic": "AND",
            "conditions": [
                {"indicator": "vol_spike", "operator": ">=", "value": 3.0},
                {"indicator": "price_change_pct", "operator": ">=", "value": 0.1},
            ]
        },
        "exit": {
            "trailing_stop_pct": -0.8,
            "take_profit_pct": 1.5,
            "hard_stop_pct": -2.0,
            "conditions": []
        },
        "limits": {"max_daily_trades": 3, "min_hold_minutes": 20, "max_positions": 1, "market_close_force": True}
    },
    "RSI_과매도": {
        "bot_id": "rsi-oversold-preset",
        "name": "RSI 과매도 반등",
        "description": "RSI 30 이하 과매도 구간에서 매수, 70 이상에서 매도",
        "category": "역추세",
        "interval_seconds": 60,
        "target_codes": ["069500", "091160", "102110", "122630", "305720"],
        "entry": {
            "logic": "AND",
            "conditions": [
                {"indicator": "rsi", "operator": "<=", "value": 30, "period": 14},
                {"indicator": "vol_spike", "operator": ">=", "value": 1.5},
            ]
        },
        "exit": {
            "trailing_stop_pct": -1.0,
            "take_profit_pct": 2.0,
            "hard_stop_pct": -1.5,
            "conditions": [
                {"indicator": "rsi", "operator": ">=", "value": 70}
            ]
        },
        "limits": {"max_daily_trades": 3, "min_hold_minutes": 15, "max_positions": 1, "market_close_force": True}
    },
    "BB_하단_반등": {
        "bot_id": "bb-bounce-preset",
        "name": "볼린저밴드 하단 반등",
        "description": "BB 하단 이탈 후 VWAP 위로 복귀 시 매수",
        "category": "역추세",
        "interval_seconds": 60,
        "target_codes": ["069500", "091160", "102110", "122630", "305720"],
        "entry": {
            "logic": "AND",
            "conditions": [
                {"indicator": "bb_position", "operator": "<=", "value": 0.0, "period": 20},
                {"indicator": "price_above_vwap", "operator": "==", "value": True},
            ]
        },
        "exit": {
            "trailing_stop_pct": -0.8,
            "take_profit_pct": 1.5,
            "hard_stop_pct": -1.0,
            "conditions": [
                {"indicator": "bb_position", "operator": ">=", "value": 0.5}
            ]
        },
        "limits": {"max_daily_trades": 4, "min_hold_minutes": 15, "max_positions": 2, "market_close_force": True}
    },
    "골든크로스": {
        "bot_id": "golden-cross-preset",
        "name": "SMA 골든크로스",
        "description": "SMA 5/20 골든크로스 + 거래량 증가 시 매수",
        "category": "모멘텀",
        "interval_seconds": 300,
        "target_codes": ["069500", "091160", "102110", "122630", "305720"],
        "entry": {
            "logic": "AND",
            "conditions": [
                {"indicator": "sma_cross", "operator": "==", "value": "golden"},
                {"indicator": "vol_spike", "operator": ">=", "value": 2.0},
            ]
        },
        "exit": {
            "trailing_stop_pct": -1.0,
            "take_profit_pct": 2.0,
            "hard_stop_pct": -1.5,
            "conditions": [
                {"indicator": "sma_cross", "operator": "==", "value": "dead"}
            ]
        },
        "limits": {"max_daily_trades": 2, "min_hold_minutes": 30, "max_positions": 1, "market_close_force": True}
    },
}


class BotDesigner:
    """자연어 → 봇 스펙 JSON 생성 + 저장 + 등록."""

    def design(self, description: str) -> dict:
        """자연어 → 스펙 JSON (Claude CLI 사용)."""
        prompt = _DESIGNER_PROMPT.format(description=description)

        try:
            from src.ai.analyzer import AIAnalyzer
            analyzer = AIAnalyzer()
            response, _ = analyzer.analyze(prompt)

            # JSON 추출
            spec = self._parse_json(response)
            if not spec:
                return {"success": False, "error": "AI 응답에서 JSON을 추출할 수 없습니다."}

            # 유효성 검증
            ok, err = self.validate(spec)
            if not ok:
                return {"success": False, "error": f"스펙 검증 실패: {err}", "spec": spec}

            return {"success": True, "spec": spec}

        except Exception as e:
            logger.error("봇 설계 실패: %s", e)
            return {"success": False, "error": str(e)}

    def design_from_preset(self, preset_name: str, overrides: dict = None) -> dict:
        """프리셋 기반 스펙 생성."""
        preset = PRESETS.get(preset_name)
        if not preset:
            return {"success": False, "error": f"프리셋 '{preset_name}' 없음. 가능: {list(PRESETS.keys())}"}

        import copy
        spec = copy.deepcopy(preset)
        if overrides:
            for k, v in overrides.items():
                if k in spec:
                    spec[k] = v
                elif k in spec.get("limits", {}):
                    spec["limits"][k] = v
                elif k in spec.get("exit", {}):
                    spec["exit"][k] = v

        return {"success": True, "spec": spec}

    def save_and_register(self, spec: dict) -> dict:
        """스펙 저장 + 봇 레지스트리 등록."""
        bot_id = save_spec(spec)

        # 레지스트리 등록
        try:
            from src.bots.registry import (
                register, BotInfo, BotCategory, BotType, MarketSession, BotUISpec, Widget, WidgetType,
            )
            cat_map = {"모멘텀": BotCategory.MOMENTUM, "역추세": BotCategory.MEAN_REVERT, "커스텀": BotCategory.CUSTOM}
            register(BotInfo(
                id=bot_id,
                name=spec.get("name", bot_id),
                version="1.0.0",
                category=cat_map.get(spec.get("category", ""), BotCategory.CUSTOM),
                bot_type=BotType.POLLING,
                market_session=MarketSession.REGULAR,
                description=spec.get("description", ""),
                strategy=f"조건 기반 자동 생성 봇 ({len(spec.get('entry', {}).get('conditions', []))}개 진입 조건)",
                risk_level=3,
                tags=["템플릿", spec.get("category", "")],
                author="Bot Designer",
                module=f"template:{bot_id}",
                ui=BotUISpec(grid_cols=2, widgets=[
                    Widget("status", WidgetType.BADGE, "상태", "1x1", "running", color_rule="status"),
                    Widget("scan_count", WidgetType.STAT, "스캔 횟수", "1x1", "scan_count", format="count"),
                    Widget("realized", WidgetType.STAT, "실현 손익", "1x1", "pnl.realized", format="won", color_rule="pnl"),
                    Widget("total_pnl", WidgetType.STAT, "총 손익", "1x1", "pnl.total", format="won", color_rule="pnl"),
                    Widget("holding", WidgetType.TEXT, "보유 종목", "2x1", "holding"),
                    Widget("scans", WidgetType.LIST, "최근 판단", "2x2", "recent_trades"),
                ]),
            ))
        except Exception as e:
            logger.warning("레지스트리 등록 실패: %s", e)

        return {"success": True, "bot_id": bot_id, "file": str(SPEC_DIR / f"{bot_id}.json")}

    def validate(self, spec: dict) -> tuple[bool, str]:
        """스펙 유효성 검증."""
        if not spec.get("bot_id"):
            return False, "bot_id 필수"
        if not spec.get("entry", {}).get("conditions"):
            return False, "진입 조건이 없습니다"

        valid_indicators = set(INDICATOR_META.keys())
        for cond in spec.get("entry", {}).get("conditions", []):
            if cond.get("indicator") not in valid_indicators:
                return False, f"알 수 없는 지표: {cond.get('indicator')}"

        for cond in spec.get("exit", {}).get("conditions", []):
            if cond.get("indicator") not in valid_indicators:
                return False, f"알 수 없는 청산 지표: {cond.get('indicator')}"

        return True, ""

    def _parse_json(self, text: str) -> dict | None:
        """AI 응답에서 JSON 추출."""
        # 코드 블록 내 JSON
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # 순수 JSON
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        # { } 블록 추출
        match = re.search(r'\{[^{}]*"bot_id"[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None
