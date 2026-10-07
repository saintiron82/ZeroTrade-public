"""Bot decision audit recorder."""

import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def make_run_id(bot_id: str, pipe_id: str | None, scan_count: int) -> str:
    """Create a unique audit run id for one bot scan."""
    pipe_part = pipe_id or "no-pipe"
    timestamp = datetime.now(_KST).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{bot_id}:{pipe_part}:{timestamp}:{int(scan_count or 0)}:{suffix}"


def make_action_id(target_type: str, target: str) -> str:
    """개선 액션 고유 ID 생성. 형식: target_type:target:timestamp:uuid6"""
    timestamp = datetime.now(_KST).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{target_type}:{target}:{timestamp}:{suffix}"


class AuditRecorder:
    """Small wrapper that keeps audit DB failures out of trading flow."""

    def __init__(
        self,
        db,
        bot_id: str,
        pipe_id: str | None = None,
        scan_count: int = 0,
        mode: str = "",
    ):
        self.db = db
        self.bot_id = bot_id
        self.pipe_id = pipe_id
        self.scan_count = int(scan_count or 0)
        self.mode = mode
        self.run_id = make_run_id(bot_id, pipe_id, self.scan_count)

    def _safe_write(self, operation: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.warning("Audit write failed during %s: %s", operation, exc)
            return None

    def start(self):
        return self._safe_write(
            "start",
            self.db.start_decision_run,
            self.run_id,
            pipe_id=self.pipe_id or "",
            bot_id=self.bot_id,
            scan_count=self.scan_count,
            mode=self.mode,
        )

    def evidence(
        self,
        source: str,
        payload=None,
        stock_code: str = "",
        summary: str = "",
        success: bool = True,
        error: str = "",
    ):
        return self._safe_write(
            "evidence",
            self.db.log_evidence_snapshot,
            self.run_id,
            source,
            stock_code=stock_code,
            payload=payload,
            summary=summary,
            success=success,
            error=error,
        )

    def ai_call(
        self,
        provider: str = "",
        model: str = "",
        prompt: str = "",
        raw_response: str = "",
        parsed=None,
        parse_success: bool = False,
        parse_error: str = "",
        tokens: int = 0,
        latency_ms: int = 0,
    ):
        parsed_dict = parsed if isinstance(parsed, dict) else {}
        return self._safe_write(
            "ai_call",
            self.db.log_ai_audit,
            self.run_id,
            bot_id=self.bot_id,
            provider=provider,
            model=model,
            prompt=prompt,
            raw_response=raw_response,
            parsed=parsed,
            parse_success=parse_success,
            parse_error=parse_error,
            confidence=parsed_dict.get("confidence", 0),
            criteria=parsed_dict.get("criteria"),
            bull_summary=parsed_dict.get("bull_summary", ""),
            bear_summary=parsed_dict.get("bear_summary", ""),
            trader_reason=parsed_dict.get("trader_reason", ""),
            tokens=tokens,
            latency_ms=latency_ms,
        )

    def preflight(
        self,
        check_name: str,
        passed: bool = True,
        severity: str = "info",
        message: str = "",
        payload=None,
        stock_code: str = "",
    ):
        return self._safe_write(
            "preflight",
            self.db.log_preflight_check,
            self.run_id,
            check_name,
            stock_code=stock_code,
            passed=passed,
            severity=severity,
            message=message,
            payload=payload,
        )

    def finish(
        self,
        status: str = "completed",
        action: str = "",
        stock_code: str = "",
        decision_id: int = 0,
        trade_id: int = 0,
        error: str = "",
    ):
        return self._safe_write(
            "finish",
            self.db.update_decision_run,
            self.run_id,
            status=status,
            action=action,
            stock_code=stock_code,
            decision_id=decision_id,
            trade_id=trade_id,
            error=error,
        )
