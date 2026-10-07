"""조건식 평가 엔진.

AI가 생성한 JSON 조건식을 파싱하고, 실시간 지표와 비교하여
조건 충족 여부를 판정한다.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.monitor.indicators import IndicatorResult

logger = logging.getLogger(__name__)

OPERATORS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "cross_above": lambda a, b: False,  # 별도 처리 필요
    "cross_below": lambda a, b: False,
}


@dataclass
class Rule:
    """단일 조건 규칙."""
    indicator: str     # price, rsi, macd, macd_signal, sma_5, sma_20 등
    operator: str      # <, >, <=, >=, ==, !=
    value: float       # 비교 대상 값
    period: int = 0    # 지표 기간 (참조용)


@dataclass
class Condition:
    """하나의 매매 조건."""
    id: str
    stock_code: str
    stock_name: str
    action: str         # buy, sell
    rules: list[Rule]
    logic: str          # AND, OR
    amount: int         # 매매 금액 (원)
    reason: str = ""
    strategy_id: str = ""  # 소속 전략 ID


@dataclass
class EvalResult:
    """조건 평가 결과."""
    condition: Condition
    triggered: bool
    matched_rules: list[str]   # 충족된 규칙 설명
    indicator_snapshot: dict   # 평가 시점 지표값


class ConditionEvaluator:
    """조건식 평가 엔진."""

    def __init__(self):
        self._conditions: list[Condition] = []

    def load_from_file(self, path: str | Path):
        """JSON 파일에서 조건식 로드."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # v2: strategy 블록에서 strategy_id 추출
        strategy_block = data.get("strategy", {})
        default_strategy_id = strategy_block.get("id", "")

        self._conditions = []
        for cond in data.get("conditions", []):
            rules = [
                Rule(
                    indicator=r["indicator"],
                    operator=r["operator"],
                    value=float(r["value"]),
                    period=r.get("period", 0),
                )
                for r in cond["rules"]
            ]
            self._conditions.append(Condition(
                id=cond["id"],
                stock_code=cond["stock_code"],
                stock_name=cond.get("stock_name", ""),
                action=cond["action"],
                rules=rules,
                logic=cond.get("logic", "AND"),
                amount=cond.get("amount", 0),
                reason=cond.get("reason", ""),
                strategy_id=cond.get("strategy_id", default_strategy_id),
            ))
        logger.info("조건식 %d개 로드됨", len(self._conditions))

    def load_conditions(self, conditions: list[Condition]):
        """Condition 객체 직접 로드."""
        self._conditions = conditions

    @property
    def conditions(self) -> list[Condition]:
        return self._conditions

    def stock_codes(self) -> list[str]:
        """감시 대상 종목코드 목록."""
        return list(set(c.stock_code for c in self._conditions))

    def evaluate(self, stock_code: str, indicators: IndicatorResult) -> list[EvalResult]:
        """주어진 종목의 조건들을 평가."""
        results = []
        for cond in self._conditions:
            if cond.stock_code != stock_code:
                continue
            result = self._eval_condition(cond, indicators)
            results.append(result)
        return results

    def _eval_condition(self, cond: Condition, ind: IndicatorResult) -> EvalResult:
        """단일 조건 평가."""
        matched = []
        unmatched = []
        snapshot = {}

        for rule in cond.rules:
            actual = self._get_indicator_value(rule.indicator, ind)
            snapshot[rule.indicator] = actual

            if actual is None:
                unmatched.append(f"{rule.indicator}: 데이터 없음")
                continue

            op_func = OPERATORS.get(rule.operator)
            if op_func is None:
                unmatched.append(f"{rule.indicator}: 알 수 없는 연산자 {rule.operator}")
                continue

            if op_func(actual, rule.value):
                matched.append(f"{rule.indicator} {actual} {rule.operator} {rule.value}")
            else:
                unmatched.append(f"{rule.indicator} {actual} not {rule.operator} {rule.value}")

        if cond.logic == "AND":
            triggered = len(matched) == len(cond.rules) and len(cond.rules) > 0
        else:  # OR
            triggered = len(matched) > 0

        return EvalResult(
            condition=cond,
            triggered=triggered,
            matched_rules=matched,
            indicator_snapshot=snapshot,
        )

    def _get_indicator_value(self, indicator: str, ind: IndicatorResult) -> float | None:
        """IndicatorResult에서 지표값 추출."""
        mapping = {
            "price": ind.price,
            "rsi": ind.rsi,
            "macd": ind.macd,
            "macd_signal": ind.macd_signal,
            "macd_histogram": ind.macd_histogram,
            "sma_5": ind.sma_5,
            "sma_20": ind.sma_20,
            "sma_60": ind.sma_60,
            "ema_12": ind.ema_12,
            "ema_26": ind.ema_26,
            "bb_upper": ind.bb_upper,
            "bb_middle": ind.bb_middle,
            "bb_lower": ind.bb_lower,
            "volume": ind.volume,
        }
        return mapping.get(indicator)
