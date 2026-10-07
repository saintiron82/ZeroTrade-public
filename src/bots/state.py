"""봇 상태 영속화 — 재시작 시 워치리스트/보유 정보 복구."""

import json
import logging
from pathlib import Path
from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

STATE_DIR = PROJECT_ROOT / "data" / "bot_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def save_state(bot_id: str, state: dict):
    """봇 상태를 JSON 파일에 저장."""
    path = STATE_DIR / f"{bot_id}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.warning("봇 상태 저장 실패 (%s): %s", bot_id, e)


def load_state(bot_id: str) -> dict | None:
    """봇 상태를 JSON 파일에서 복구. 없으면 None."""
    path = STATE_DIR / f"{bot_id}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("봇 상태 로드 실패 (%s): %s", bot_id, e)
        return None


def clear_state(bot_id: str):
    """봇 상태 파일 삭제."""
    path = STATE_DIR / f"{bot_id}.json"
    path.unlink(missing_ok=True)
