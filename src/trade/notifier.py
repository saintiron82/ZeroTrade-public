"""슬랙/디스코드 웹훅 알림."""

import json
import logging

import httpx

from src.config import settings
from src.kis.order import OrderResult
from src.monitor.evaluator import EvalResult

logger = logging.getLogger(__name__)


class Notifier:
    """슬랙/디스코드 웹훅 알림 발송."""

    def __init__(self):
        self._cfg = settings.notify
        self._client = httpx.Client(timeout=5)

    def notify_trigger(self, eval_result: EvalResult):
        """조건 충족 알림."""
        cond = eval_result.condition
        lines = [
            f"**[조건 충족]** {cond.stock_name} ({cond.stock_code})",
            f"  액션: {cond.action.upper()}",
            f"  사유: {cond.reason}",
            f"  충족 규칙:",
        ]
        for rule_desc in eval_result.matched_rules:
            lines.append(f"    - {rule_desc}")
        lines.append(f"  지표 스냅샷: {json.dumps(eval_result.indicator_snapshot)}")

        self._send("\n".join(lines))

    def notify_order(self, result: OrderResult, condition_id: str = ""):
        """주문 결과 알림."""
        status = "성공" if result.success else "실패"
        msg = (
            f"**[주문 {status}]** {result.stock_code} "
            f"{result.side.upper()} {result.quantity}주\n"
            f"  주문번호: {result.order_no}\n"
            f"  메시지: {result.message}"
        )
        if condition_id:
            msg += f"\n  조건ID: {condition_id}"
        self._send(msg)

    def notify_text(self, text: str):
        """일반 텍스트 알림."""
        self._send(text)

    def _send(self, message: str):
        """슬랙/디스코드 웹훅으로 메시지 전송."""
        sent = False

        slack_url = self._cfg.slack_webhook_url
        if slack_url and slack_url.startswith("https://"):
            try:
                self._client.post(slack_url, json={"text": message})
                sent = True
            except httpx.HTTPError as e:
                logger.warning("슬랙 알림 실패: %s", e)

        discord_url = self._cfg.discord_webhook_url
        if discord_url and discord_url.startswith("https://"):
            try:
                self._client.post(discord_url, json={"content": message})
                sent = True
            except httpx.HTTPError as e:
                logger.warning("디스코드 알림 실패: %s", e)

        if not sent:
            logger.debug("웹훅 미설정 — 웹 UI SSE로만 알림: %s", message[:80])

    def close(self):
        self._client.close()
