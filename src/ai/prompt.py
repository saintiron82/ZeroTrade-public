"""AI 프롬프트 생성 — 시장 데이터를 구조화하여 AI에게 전달."""

import json
from datetime import datetime


def build_analysis_prompt(
    market_data: dict,
    trade_history: list[dict] | None = None,
    user_rules: str = "",
    target_stocks: list[str] | None = None,
) -> str:
    """AI 분석용 프롬프트 생성.

    Args:
        market_data: 시장/종목 데이터 (시세, 지표 등)
        trade_history: 과거 매매 이력 (피드백용)
        user_rules: 사용자 정의 규칙
        target_stocks: 분석 대상 종목 리스트
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    prompt = f"""당신은 한국 주식/ETF 자동매매 시스템의 AI 분석가입니다.
현재 시각: {now}

## 역할
- 시장 데이터와 기술지표를 분석하여 매매 조건식을 JSON으로 생성합니다.
- 주식과 ETF 모두 분석 대상입니다.

## 사용자 규칙
{user_rules if user_rules else "특별한 규칙 없음 — 기본 기술적 분석 기반으로 판단"}

## 분석 대상 종목
{json.dumps(target_stocks, ensure_ascii=False) if target_stocks else "시장 전체에서 기회 탐색"}

## 현재 시장 데이터
{json.dumps(market_data, ensure_ascii=False, indent=2)}
"""

    if trade_history:
        prompt += f"""
## 과거 매매 이력 (최근 10건)
{json.dumps(trade_history[-10:], ensure_ascii=False, indent=2)}

과거 매매 결과를 분석하여 전략을 개선해주세요.
"""

    prompt += """
## 응답 형식
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만 출력하세요.

```json
{
  "version": "1.0",
  "analysis": "시장 분석 요약 (1-2문장)",
  "conditions": [
    {
      "id": "cond-001",
      "stock_code": "종목코드 (6자리)",
      "stock_name": "종목명",
      "action": "buy 또는 sell",
      "rules": [
        {"indicator": "rsi|macd|price|sma_5|sma_20|sma_60|ema_12|ema_26|bb_upper|bb_lower|volume", "operator": "<|>|<=|>=|==|!=", "value": 숫자, "period": 기간}
      ],
      "logic": "AND 또는 OR",
      "amount": 매매금액(원),
      "reason": "판단 근거"
    }
  ]
}
```
"""
    return prompt


def build_feedback_prompt(
    trade_result: dict,
    current_conditions: dict,
    market_data: dict,
) -> str:
    """매매 결과 피드백 프롬프트 생성."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""당신은 한국 주식/ETF 자동매매 시스템의 AI 분석가입니다.
현재 시각: {now}

## 최근 매매 결과
{json.dumps(trade_result, ensure_ascii=False, indent=2)}

## 현재 활성 조건식
{json.dumps(current_conditions, ensure_ascii=False, indent=2)}

## 현재 시장 데이터
{json.dumps(market_data, ensure_ascii=False, indent=2)}

## 요청
매매 결과를 분석하고, 현재 조건식을 유지/수정/삭제할지 판단해주세요.
새로운 기회가 있다면 추가 조건식도 제안해주세요.

동일한 JSON 형식으로 응답해주세요 (version, analysis, conditions 포함).
"""
