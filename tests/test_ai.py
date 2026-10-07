"""AI 분석기 테스트."""

import json

import pytest

from src.ai.parser import parse_ai_response, _extract_json, _validate, save_conditions
from src.ai.prompt import build_analysis_prompt, build_feedback_prompt


def test_extract_json_from_code_block():
    """```json 블록에서 JSON 추출."""
    text = 'Here is the result:\n```json\n{"version": "1.0", "conditions": []}\n```'
    result = _extract_json(text)
    assert result is not None
    data = json.loads(result)
    assert data["version"] == "1.0"


def test_extract_json_raw():
    """순수 JSON 텍스트에서 추출."""
    text = '{"version": "1.0", "conditions": []}'
    result = _extract_json(text)
    assert result is not None


def test_parse_valid_response():
    """유효한 AI 응답 파싱."""
    response = json.dumps({
        "version": "1.0",
        "analysis": "테스트",
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
    })
    data = parse_ai_response(response)
    assert data is not None
    assert len(data["conditions"]) == 1


def test_parse_invalid_stock_code():
    """잘못된 종목코드 검증."""
    response = json.dumps({
        "conditions": [{
            "id": "c1", "stock_code": "ABC", "action": "buy",
            "rules": [{"indicator": "rsi", "operator": "<", "value": 30}],
        }]
    })
    data = parse_ai_response(response)
    assert data is None


def test_parse_invalid_indicator():
    """잘못된 지표명 검증."""
    response = json.dumps({
        "conditions": [{
            "id": "c1", "stock_code": "005930", "action": "buy",
            "rules": [{"indicator": "invalid_indicator", "operator": "<", "value": 30}],
        }]
    })
    data = parse_ai_response(response)
    assert data is None


def test_parse_garbage_input():
    """무의미한 입력 시 None 반환."""
    assert parse_ai_response("this is not json at all") is None
    assert parse_ai_response("") is None


def test_build_analysis_prompt():
    """분석 프롬프트 생성."""
    prompt = build_analysis_prompt(
        market_data={"005930": {"price": 70000, "rsi": 45}},
        target_stocks=["005930"],
        user_rules="RSI 30 이하에서만 매수",
    )
    assert "005930" in prompt
    assert "RSI 30 이하" in prompt
    assert "JSON" in prompt


def test_build_feedback_prompt():
    """피드백 프롬프트 생성."""
    prompt = build_feedback_prompt(
        trade_result={"stock_code": "005930", "side": "buy", "success": True},
        current_conditions={"conditions": []},
        market_data={"005930": {"price": 72000}},
    )
    assert "005930" in prompt
    assert "매매 결과" in prompt


def test_save_conditions(tmp_path):
    """조건식 파일 저장."""
    from unittest.mock import patch

    mock_settings = type("S", (), {
        "conditions_dir": tmp_path,
        "ensure_dirs": lambda self: None,
    })()

    with patch("src.ai.parser.settings", mock_settings):
        data = {"version": "1.0", "conditions": []}
        path = save_conditions(data, "test.json")
        assert path.exists()
        saved = json.loads(path.read_text())
        assert saved["version"] == "1.0"
