"""AI 응답 파싱 — JSON 조건식 추출 및 검증."""

import json
import logging
import re
from pathlib import Path
from datetime import datetime

from src.config import settings

logger = logging.getLogger(__name__)

VALID_INDICATORS = {
    "price", "rsi", "macd", "macd_signal", "macd_histogram",
    "sma_5", "sma_20", "sma_60", "ema_12", "ema_26",
    "bb_upper", "bb_middle", "bb_lower", "volume",
}

VALID_OPERATORS = {"<", "<=", ">", ">=", "==", "!="}
VALID_ACTIONS = {"buy", "sell"}


def parse_ai_response(response: str) -> dict | None:
    """AI 응답에서 JSON을 추출하고 검증.

    Returns:
        검증된 조건식 dict, 실패 시 None
    """
    # JSON 블록 추출 (```json ... ``` 또는 순수 JSON)
    json_str = _extract_json(response)
    if not json_str:
        logger.error("AI 응답에서 JSON을 찾을 수 없음")
        return None

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error("JSON 파싱 실패: %s", e)
        return None

    # 검증
    if not _validate(data):
        return None

    return data


def _extract_json(text: str) -> str | None:
    """텍스트에서 JSON 부분 추출."""
    # ```json ... ``` 블록
    match = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # ``` ... ``` 블록
    match = re.search(r"```\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # 순수 JSON ({...})
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        return match.group(0)

    return None


def _validate(data: dict) -> bool:
    """조건식 데이터 검증."""
    conditions = data.get("conditions", [])
    if not isinstance(conditions, list):
        logger.error("conditions가 리스트가 아님")
        return False

    for i, cond in enumerate(conditions):
        # 필수 필드
        for field in ("id", "stock_code", "action", "rules"):
            if field not in cond:
                logger.error("조건 %d: 필수 필드 '%s' 누락", i, field)
                return False

        # stock_code 형식 (6자리 숫자)
        if not re.match(r"^\d{6}$", cond["stock_code"]):
            logger.error("조건 %d: 잘못된 종목코드 '%s'", i, cond["stock_code"])
            return False

        # action
        if cond["action"] not in VALID_ACTIONS:
            logger.error("조건 %d: 잘못된 action '%s'", i, cond["action"])
            return False

        # rules 검증
        for j, rule in enumerate(cond["rules"]):
            if rule.get("indicator") not in VALID_INDICATORS:
                logger.error("조건 %d 규칙 %d: 잘못된 indicator '%s'", i, j, rule.get("indicator"))
                return False
            if rule.get("operator") not in VALID_OPERATORS:
                logger.error("조건 %d 규칙 %d: 잘못된 operator '%s'", i, j, rule.get("operator"))
                return False

    return True


def save_conditions(data: dict, filename: str | None = None) -> Path:
    """검증된 조건식을 파일로 저장."""
    settings.ensure_dirs()
    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"conditions_{ts}.json"
    path = settings.conditions_dir / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("조건식 저장: %s", path)
    return path
