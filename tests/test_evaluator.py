"""조건식 평가 엔진 테스트."""

import json

import pytest

from src.monitor.evaluator import Condition, ConditionEvaluator, Rule
from src.monitor.indicators import IndicatorResult


@pytest.fixture
def evaluator():
    return ConditionEvaluator()


def _make_indicators(stock_code: str, **kwargs) -> IndicatorResult:
    return IndicatorResult(stock_code=stock_code, **kwargs)


def test_and_condition_all_match(evaluator):
    """AND 조건 — 모든 규칙 충족 시 triggered."""
    cond = Condition(
        id="c1", stock_code="005930", stock_name="삼성전자",
        action="buy",
        rules=[
            Rule(indicator="rsi", operator="<", value=30),
            Rule(indicator="price", operator="<", value=70000),
        ],
        logic="AND", amount=100000,
    )
    evaluator.load_conditions([cond])
    ind = _make_indicators("005930", rsi=25.0, price=68000)
    results = evaluator.evaluate("005930", ind)
    assert len(results) == 1
    assert results[0].triggered is True


def test_and_condition_partial_match(evaluator):
    """AND 조건 — 일부만 충족 시 not triggered."""
    cond = Condition(
        id="c1", stock_code="005930", stock_name="삼성전자",
        action="buy",
        rules=[
            Rule(indicator="rsi", operator="<", value=30),
            Rule(indicator="price", operator="<", value=70000),
        ],
        logic="AND", amount=100000,
    )
    evaluator.load_conditions([cond])
    ind = _make_indicators("005930", rsi=25.0, price=75000)  # price 미충족
    results = evaluator.evaluate("005930", ind)
    assert results[0].triggered is False


def test_or_condition(evaluator):
    """OR 조건 — 하나만 충족해도 triggered."""
    cond = Condition(
        id="c1", stock_code="005930", stock_name="삼성전자",
        action="buy",
        rules=[
            Rule(indicator="rsi", operator="<", value=30),
            Rule(indicator="price", operator="<", value=70000),
        ],
        logic="OR", amount=100000,
    )
    evaluator.load_conditions([cond])
    ind = _make_indicators("005930", rsi=25.0, price=75000)
    results = evaluator.evaluate("005930", ind)
    assert results[0].triggered is True


def test_stock_codes(evaluator):
    """감시 대상 종목코드 추출."""
    evaluator.load_conditions([
        Condition(id="c1", stock_code="005930", stock_name="삼성전자",
                  action="buy", rules=[], logic="AND", amount=0),
        Condition(id="c2", stock_code="069500", stock_name="KODEX200",
                  action="buy", rules=[], logic="AND", amount=0),
        Condition(id="c3", stock_code="005930", stock_name="삼성전자",
                  action="sell", rules=[], logic="AND", amount=0),
    ])
    codes = evaluator.stock_codes()
    assert set(codes) == {"005930", "069500"}


def test_load_from_json(evaluator, tmp_path):
    """JSON 파일에서 조건식 로드."""
    data = {
        "version": "1.0",
        "conditions": [
            {
                "id": "cond-001",
                "stock_code": "005930",
                "stock_name": "삼성전자",
                "action": "buy",
                "rules": [
                    {"indicator": "rsi", "operator": "<", "value": 30, "period": 14}
                ],
                "logic": "AND",
                "amount": 100000,
                "reason": "RSI 과매도",
            }
        ]
    }
    f = tmp_path / "conditions.json"
    f.write_text(json.dumps(data, ensure_ascii=False))
    evaluator.load_from_file(f)
    assert len(evaluator.conditions) == 1
    assert evaluator.conditions[0].stock_code == "005930"


def test_missing_indicator_returns_not_triggered(evaluator):
    """지표 데이터 없으면 triggered=False."""
    cond = Condition(
        id="c1", stock_code="005930", stock_name="삼성전자",
        action="buy",
        rules=[Rule(indicator="rsi", operator="<", value=30)],
        logic="AND", amount=100000,
    )
    evaluator.load_conditions([cond])
    ind = _make_indicators("005930", rsi=None, price=70000)
    results = evaluator.evaluate("005930", ind)
    assert results[0].triggered is False
