"""투자 모드 중앙 관리 — DISABLED / PAPER / LIVE 3단계."""

import json
import logging
import random
import string
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT
from src.decision_log import DecisionLog

logger = logging.getLogger(__name__)

MODE_FILE = PROJECT_ROOT / "data" / "trading_mode.json"

DISABLED = "disabled"  # 주문 완전 차단 (시세/분석만)
PAPER = "paper"        # 모의투자
LIVE = "live"          # 실전투자

VALID_MODES = {DISABLED, PAPER, LIVE}

# 기본 최대 주문액 (원)
DEFAULT_MAX_ORDER = 500_000


def _load() -> dict:
    if MODE_FILE.exists():
        try:
            return json.loads(MODE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "mode": PAPER,
        "confirmed_at": "",
        "confirmed_by": "",
        "live_unlock_code": "",
        "max_order_amount": DEFAULT_MAX_ORDER,
    }


def _save(data: dict):
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def current_mode() -> str:
    """현재 투자 모드."""
    return _load().get("mode", PAPER)


def mode_info() -> dict:
    """현재 모드 + 설정 전체."""
    return _load()


def max_order_amount() -> int:
    """최대 주문 허용액."""
    return _load().get("max_order_amount", DEFAULT_MAX_ORDER)


def set_max_order_amount(amount: int):
    """최대 주문액 설정."""
    data = _load()
    data["max_order_amount"] = amount
    _save(data)


def derivative_etf_enabled() -> bool:
    """파생ETF(인버스/레버리지) 거래 허용 여부. 기본 OFF.

    계좌에 파생상품ETF 권한이 신청된 경우에만 상급 옵션에서 켠다.
    """
    return bool(_load().get("allow_derivative_etf", False))


def set_derivative_etf(enabled: bool) -> dict:
    """파생ETF 거래 허용 토글 (상급 옵션)."""
    data = _load()
    data["allow_derivative_etf"] = bool(enabled)
    _save(data)
    logger.info("파생ETF 거래 허용 → %s", "ON" if enabled else "OFF")
    return data


# ── 모드 전환 ──

def set_disabled():
    """DISABLED 모드 — 즉시 전환 (안전하므로 확인 불필요)."""
    data = _load()
    data["mode"] = DISABLED
    data["confirmed_at"] = datetime.now().isoformat()
    data["confirmed_by"] = "user"
    data["live_unlock_code"] = ""
    _save(data)
    DecisionLog().log("mode_change", action_taken="set_disabled")
    logger.info("투자 모드 → DISABLED")


def set_paper():
    """PAPER 모드 — 즉시 전환 (안전하므로 확인 불필요)."""
    data = _load()
    data["mode"] = PAPER
    data["confirmed_at"] = datetime.now().isoformat()
    data["confirmed_by"] = "user"
    data["live_unlock_code"] = ""
    _save(data)
    DecisionLog().log("mode_change", action_taken="set_paper")
    logger.info("투자 모드 → PAPER")


def request_live() -> str:
    """LIVE 전환 1단계 — 확인 코드 생성.

    Returns:
        확인 코드 (예: "LIVE-7293")
    """
    code = "LIVE-" + "".join(random.choices(string.digits, k=4))
    data = _load()
    data["live_unlock_code"] = code
    _save(data)
    logger.warning("LIVE 모드 전환 요청: 확인 코드 %s", code)
    return code


def confirm_live(code: str) -> tuple[bool, str]:
    """LIVE 전환 2단계 — 확인 코드 검증.

    Returns:
        (성공여부, 메시지)
    """
    data = _load()
    expected = data.get("live_unlock_code", "")

    if not expected:
        return False, "LIVE 전환 요청이 없습니다. 먼저 request_live를 호출하세요."

    if code != expected:
        DecisionLog().log("mode_change", action_taken="live_confirm_failed",
                          note=f"잘못된 코드: {code}")
        return False, f"확인 코드가 일치하지 않습니다. 예상: {expected}"

    data["mode"] = LIVE
    data["confirmed_at"] = datetime.now().isoformat()
    data["confirmed_by"] = "user_confirmed"
    data["live_unlock_code"] = ""
    _save(data)

    DecisionLog().log("mode_change", action_taken="set_live",
                      note="3단계 확인 완료")
    logger.warning("투자 모드 → LIVE (실전투자)")
    return True, "LIVE 모드 활성화. 실전투자가 시작됩니다."


# ── 주문 검증 ──

def validate_order(stock_code: str, amount: int, strategy_id: str = "") -> tuple[bool, str]:
    """모든 주문 경로에서 사용하는 공통 검증.

    Returns:
        (허용여부, 메시지)
    """
    mode = current_mode()

    # 1. 모드 검증
    if mode == DISABLED:
        return False, "주문 비활성화 상태입니다. 웹 UI에서 모드를 변경하세요."

    # 2. 최대 주문액 검증
    max_amt = max_order_amount()
    if amount > max_amt:
        return False, f"최대 주문액 {max_amt:,}원 초과 (요청: {amount:,}원)"

    # 3. LIVE 모드면 추가 경고 로그
    if mode == LIVE:
        DecisionLog().log(
            "live_order_attempt",
            input_data={"stock_code": stock_code, "amount": amount, "strategy_id": strategy_id},
            action_taken="order_validated_for_live",
        )

    return True, f"검증 통과 [{mode}]"


def is_order_allowed() -> bool:
    """주문 가능 여부 (DISABLED면 False)."""
    return current_mode() != DISABLED
