"""AI 판단 이력 기록 — 모든 분석/평가/주문 결정을 추적."""

import json
import logging
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DECISIONS_FILE = PROJECT_ROOT / "logs" / "decisions.json"


class DecisionLog:
    """AI 판단 및 시스템 결정을 시계열로 기록."""

    def __init__(self):
        DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if DECISIONS_FILE.exists():
            try:
                return json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
        return []

    def _save(self):
        DECISIONS_FILE.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def log(
        self,
        entry_type: str,
        strategy_id: str = "",
        input_data: dict | None = None,
        output_data: dict | None = None,
        action_taken: str = "",
        note: str = "",
    ) -> dict:
        """판단 이력 기록.

        Args:
            entry_type: "ai_analysis", "condition_trigger", "order_placed",
                        "order_result", "strategy_created", "strategy_feedback"
            strategy_id: 관련 전략 ID
            input_data: 입력 (프롬프트, 시세 스냅샷 등)
            output_data: 출력 (AI 응답, 평가 결과 등)
            action_taken: 취한 행동 설명
            note: 추가 메모
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": entry_type,
            "strategy_id": strategy_id,
            "input": input_data or {},
            "output": output_data or {},
            "action_taken": action_taken,
            "note": note,
        }
        self._entries.append(entry)
        self._save()
        return entry

    def recent(self, count: int = 20) -> list[dict]:
        return self._entries[-count:]

    def by_strategy(self, strategy_id: str) -> list[dict]:
        return [e for e in self._entries if e["strategy_id"] == strategy_id]

    def by_type(self, entry_type: str) -> list[dict]:
        return [e for e in self._entries if e["type"] == entry_type]
