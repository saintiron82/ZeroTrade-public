"""Winner-1 — 완전 자율 트레이딩 에이전트.

룰·전략 지시 없음. 매 스캔 Claude CLI 세션이 읽기 전용 도구로 자율 탐색하고
작업장에서 자기 도구를 만들어 쓰며, 최종 판단 JSON만 반환한다.
주문 집행은 봇이 TradeEngine 경유로 수행한다 (하드 가드 적용).
스펙: docs/superpowers/specs/2026-06-11-zero-autonomous-agent-design.md
"""

import asyncio
import json
import logging
from pathlib import Path

from src.bots.ai_caller import invoke_ai_result
from src.bots.base import BotBase, BotConfig
from src.bots.safeguards import MarketCloseGuard, kst_now
from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "ai_provider": "claude",
    "ai_model": "",
    "session_timeout_sec": 600,   # 세션 타임아웃 (스펙: 기본 10분)
    "min_scan_minutes": 5,
    "max_scan_minutes": 240,
    "dry_run": False,             # True면 주문 미집행 (검증 모드)
    "workspace_root": "",         # 테스트 격리용. 빈 값이면 data/agent_workspace
}

MCP_SERVER_PATH = str(PROJECT_ROOT / "src" / "mcp_server.py")
VENV_PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")

VALID_ACTIONS = {"buy", "sell", "hold"}


def clamp_next_scan(value, lo: int, hi: int) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, minutes))


def parse_decision(text: str) -> dict:
    """세션 출력에서 마지막 JSON 객체를 추출·검증. 실패 시 hold 폴백."""
    fallback = {"action": "hold", "code": "", "qty": 0,
                "reason": "", "next_scan_minutes": None}

    def fail(msg: str) -> dict:
        return {**fallback, "error": f"parse: {msg}", "reason": msg}

    if not text:
        return fail("빈 응답")
    # 꼬리 앵커: trailing 공백·마크다운 펜스(```) 제거 후 '}'로 끝나는 경우에만 채택.
    # prose로 끝나는 응답 본문에 인용된 예시 JSON을 주문으로 오채택하지 않기 위함.
    tail = text.rstrip()
    while tail.endswith("```"):
        tail = tail[:-3].rstrip()
    if not tail.endswith("}"):
        return fail("꼬리 JSON 아님")
    # 꼬리 '}'에서 끝나는 JSON 객체를 역방향 백트래킹으로 추출 (중첩 JSON 대응)
    start = tail.rfind("{")
    while start != -1:
        try:
            data = json.loads(tail[start:])
            break
        except json.JSONDecodeError:
            start = tail.rfind("{", 0, start)
    else:
        return fail("JSON 미발견")

    action = str(data.get("action", "")).lower()
    if action not in VALID_ACTIONS:
        return fail(f"알 수 없는 action: {action!r}")

    code = str(data.get("code", "") or "")
    qty_raw = data.get("qty", 0)
    # 정수만 허용 — bool(true)·float(1.5) 차단, 문자열은 ASCII 정수("3")만 허용
    if qty_raw is None:
        qty = 0
    elif isinstance(qty_raw, bool) or isinstance(qty_raw, float):
        return fail(f"qty 불량: {qty_raw!r}")
    elif isinstance(qty_raw, int):
        qty = qty_raw
    elif isinstance(qty_raw, str) and qty_raw.isascii() and qty_raw.isdigit():
        qty = int(qty_raw)
    else:
        return fail(f"qty 불량: {qty_raw!r}")
    if action in ("buy", "sell"):
        if not (code.isascii() and code.isdigit() and len(code) == 6):
            return fail(f"종목코드 불량: {code!r}")
        if qty <= 0:
            return fail(f"qty는 양수여야 함: {qty}")

    return {
        "action": action,
        "code": code,
        "qty": qty,
        "reason": str(data.get("reason", "") or ""),
        "next_scan_minutes": data.get("next_scan_minutes"),
    }


class Winner1Bot(BotBase):
    """완전 자율 AI 에이전트 봇 — _loop 전면 오버라이드 예정 (Task 5)."""

    BOT_ID = "winner-1"
    BOT_NAME = "Winner 1호"
    # 스캔이 드물어(5~240분) hold도 매 사이클 decisions에 기록
    LOG_HOLD_EVERY_SCAN = True

    def __init__(self, config: BotConfig | dict = None):
        # dict config의 params 키는 BotConfig 필드가 아니므로 분리한다
        extra_params: dict = {}
        if isinstance(config, dict):
            config = dict(config)
            extra_params = config.pop("params", None) or {}
        self._params = {**DEFAULT_PARAMS, **extra_params}

        # 가드 설정 (super().__init__ 전에 설정해야 TradeEngine에 전달됨)
        # 계좌 보호선(일일손실 -2%)만 opt-in — 전략성 가드는 전부 미사용 (스펙: 자율 경계)
        # 엔진 컨벤션: 음수 pct (_check_daily_loss_limit — 손실 누적이 음수)
        self._guard_config = {"daily_loss_limit_pct": -2.0}

        super().__init__(config)

        self.workspace = self._bootstrap_workspace()
        self._next_scan_minutes = self._params["min_scan_minutes"]

    # ═══════════════════════════════════════════
    # 작업장 (agent workspace)
    # ═══════════════════════════════════════════

    def _bootstrap_workspace(self) -> Path:
        """작업장 생성 — idempotent, 에이전트가 만든 파일은 절대 덮어쓰지 않는다."""
        root = self._params.get("workspace_root") or str(
            PROJECT_ROOT / "data" / "agent_workspace")
        ws = Path(root) / self.BOT_ID
        (ws / "tools").mkdir(parents=True, exist_ok=True)
        (ws / ".claude").mkdir(parents=True, exist_ok=True)

        journal = ws / "journal.md"
        if not journal.exists():
            journal.write_text(
                "# Winner-1 Journal\n\n자기 전략 메모·복기 기록. 자유롭게 사용.\n",
                encoding="utf-8")

        mcp_cfg = ws / ".mcp.json"
        if not mcp_cfg.exists():
            mcp_cfg.write_text(json.dumps({
                "mcpServers": {
                    "ff-trading-readonly": {
                        "command": VENV_PYTHON,
                        "args": [MCP_SERVER_PATH],
                        "env": {
                            "PYTHONPATH": str(PROJECT_ROOT),
                            "FF_MCP_READONLY": "1",
                        },
                    }
                }
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        settings = ws / ".claude" / "settings.json"
        if not settings.exists():
            settings.write_text(json.dumps(
                {"enableAllProjectMcpServers": True},
                ensure_ascii=False, indent=2), encoding="utf-8")
        return ws

    # ═══════════════════════════════════════════
    # 세션 — 프롬프트 조립 + CLI 호출
    # ═══════════════════════════════════════════

    def build_prompt(self, cash: int, positions: list[dict]) -> str:
        """물리적 사실만 — 전략 지시·지표 추천·페르소나 없음 (스펙)."""
        pos_lines = "\n".join(
            f"- {p['code']}: {p['qty']}주 @ 평단 {p['avg_price']:,}원"
            for p in positions) or "- 없음"
        return f"""너는 실전 계좌의 자율 트레이더다. 목표는 수익 극대화.

[현재 상태 — 사실]
- 가용 예산(가상 계좌 현금): {cash:,}원
- 보유 포지션:
{pos_lines}

[물리적 제약 — 사실]
- 매매 주문은 이 세션에서 직접 실행할 수 없다. 아래 출력 스키마의 JSON이 유일한 주문 수단이다.
- 매도는 보유 수량 내에서만 체결된다.
- 이 계좌는 파생ETF(인버스·레버리지) 거래 미신청 상태라 해당 종목 매수는 거부된다.
- 한국 정규장 시간에만 체결된다.

[작업 환경]
- 현재 디렉터리는 너의 영속 작업장이다. tools/에 필요한 파이썬 스크립트를 자유롭게
  만들고 실행하고 다음 세션에서 재사용해도 된다 ({VENV_PYTHON} 사용).
- journal.md는 너의 기록장이다. 원하는 대로 관리하라.
- 작업장 밖의 파일은 수정하지 마라.
- MCP 도구(ff-trading-readonly)로 시세·차트·호가·순위·뉴스·잔고를 조회할 수 있다.

[출력 — 응답 마지막에 반드시 이 JSON 하나]
{{"action": "buy"|"sell"|"hold", "code": "6자리", "qty": 정수, "reason": "근거",
  "next_scan_minutes": 다음 판단까지 분(5~240)}}"""

    def run_session(self, cash: int, positions: list[dict]) -> tuple[dict, dict]:
        prompt = self.build_prompt(cash, positions)
        result = invoke_ai_result(
            prompt,
            self._params,
            timeout=int(self._params["session_timeout_sec"]),
            bot_id=self.BOT_ID,
            conversation_id=f"bot:{self.BOT_ID}",
            cwd=str(self.workspace),
        )
        meta = {
            "prompt": prompt,
            "raw_response": result.content,
            "provider": result.provider,
            "model": result.model,
            "tokens": result.tokens,
            "latency_ms": result.latency_ms,
            "error": result.error,
        }
        if result.error or not result.content:
            return ({"action": "hold", "code": "", "qty": 0,
                     "reason": f"세션 오류: {result.error or 'no-content'}",
                     "next_scan_minutes": None,
                     "error": result.error or "no-content"}, meta)
        return parse_decision(result.content), meta

    # ═══════════════════════════════════════════
    # 자율 사이클 — 세션 → 파싱 → 집행 → audit
    # ═══════════════════════════════════════════

    def _current_state(self) -> tuple[int, list[dict]]:
        """봇 가상 계좌 현금·포지션 (BotLedger 기준 — 자기 trades만)."""
        try:
            from src.db import get_db
            from src.bots.ledger import BotLedger
            # limit은 엔진 _own_ledger_qty와 동일하게 10000
            trades = get_db().get_trades(
                pipe_id=self._get_pipe_id(), source=self.BOT_ID, limit=10000)
            led = BotLedger.from_trades(self._config.budget, trades)
            return led.cash, led.positions
        except Exception as exc:
            logger.warning("[%s] 상태 조회 실패: %s", self.BOT_ID, exc)
            return self._config.budget, []

    def run_scan_cycle(self) -> dict:
        """한 번의 자율 판단 사이클 — 세션 → 파싱 → 집행 → audit.

        예외가 나도 audit run은 반드시 failed로 마감된다 (I2).
        """
        self._scan_count += 1  # 직접 호출 시에도 증가 (M4)
        recorder = self._audit_start_run()
        try:
            return self._scan_cycle(recorder)
        except Exception as exc:
            if recorder is not None:
                recorder.finish(status="failed", error=str(exc))
            raise

    def _scan_cycle(self, recorder) -> dict:
        cash, positions = self._current_state()
        ws_before = self._workspace_snapshot()

        decision, meta = self.run_session(cash, positions)

        # AI 호출 기록 (프롬프트·원문 전문)
        if recorder is not None:
            recorder.ai_call(
                provider=meta.get("provider", ""), model=meta.get("model", ""),
                prompt=meta.get("prompt", ""),
                raw_response=meta.get("raw_response", ""),
                parsed=decision, parse_success="error" not in decision,
                parse_error=decision.get("error", ""),
                tokens=int(meta.get("tokens") or 0),
                latency_ms=int(meta.get("latency_ms") or 0),
            )

        # 집행 (TradeEngine 경유 — 하드 가드 적용)
        action = decision["action"]
        dry_run = bool(self._params.get("dry_run"))
        result = {}
        if action in ("buy", "sell") and not dry_run:
            if action == "buy":
                # C1: price=0으로 주문하면 trades에 0원 매수가 기록돼
                # 예산 체크·ledger cash·매도 PnL이 전부 붕괴한다.
                # mirinae/SV 관례대로 현재가 조회 후 실제 가격으로 주문.
                # engine.buy의 reason 파라미터는 없음 (시그니처: code, qty, price).
                price = self._lookup_price(decision["code"])
                if price <= 0:
                    result = {"success": False, "order_no": "",
                              "message": "현재가 조회 실패 — 주문 포기"}
                else:
                    result = self.engine.buy(
                        decision["code"], decision["qty"], price) or {}
            else:
                entry = next((p["avg_price"] for p in positions
                              if p["code"] == decision["code"]), 0)
                result = self.engine.sell(decision["code"], decision["qty"],
                                          entry_price=entry,
                                          reason=decision["reason"]) or {}
            if result.get("success"):
                self._publish_event("bot_trade", {
                    "action": action,
                    "code": decision.get("code", ""),
                    "result": result,
                })

        # next_scan 클램프 — 루프 반영 전에 반드시 적용 (파서는 원본 반환)
        self._next_scan_minutes = clamp_next_scan(
            decision.get("next_scan_minutes"),
            int(self._params["min_scan_minutes"]),
            int(self._params["max_scan_minutes"]))

        # UI 판단 로그 (registry "최근 판단" 위젯 → status().recent_trades)
        self._trade_log.append({
            "scan": self._scan_count,
            "time": kst_now().strftime("%H:%M:%S"),
            "action": action,
            "detail": decision.get("reason", ""),
            "result": decision,
        })
        if len(self._trade_log) > 50:
            self._trade_log = self._trade_log[-50:]

        # DB decisions 기록 (CLAUDE.md 규칙 10) — winner-1은 hold도 매 사이클 기록
        decision_id = self._log_decision({
            "action": action,
            "code": decision.get("code", ""),
            "reason": decision.get("reason", ""),
            "confidence": 0,
            "indicators": {"next_scan_minutes": self._next_scan_minutes,
                           "dry_run": dry_run},
            "trade_id": int(result.get("trade_id") or 0),
        })

        # 증거: 판단 JSON + 작업장 변경분 + next_scan
        if recorder is not None:
            recorder.evidence(
                "decision",
                payload={"decision": decision,
                         "workspace_changes": self._workspace_diff(ws_before),
                         "next_scan_minutes": self._next_scan_minutes,
                         "dry_run": dry_run},
                stock_code=decision.get("code", ""),
                summary=f"{action} {decision.get('code', '')} → next {self._next_scan_minutes}분",
            )
            if action in ("buy", "sell") and not dry_run:
                status = "ordered" if result.get("success") else "blocked"
                if not result.get("success"):
                    recorder.preflight("execution", passed=False, severity="block",
                                       message=str(result.get("message", "")),
                                       stock_code=decision.get("code", ""))
            elif decision.get("error"):
                status = "failed"
            else:
                status = "completed"
            recorder.finish(status=status, action=action,
                            stock_code=decision.get("code", ""),
                            decision_id=int(decision_id or 0),
                            trade_id=int(result.get("trade_id") or 0),
                            error=decision.get("error", ""))

        self._save_state()
        return decision

    def _lookup_price(self, code: str) -> int:
        """현재가 조회 — 실패 시 0 (주문 포기 신호)."""
        try:
            info = self._get_market().get_price(code)
            return int(info.get("price", 0) or 0) if info else 0
        except Exception as exc:
            logger.warning("[%s] 현재가 조회 실패: %s %s", self.BOT_ID, code, exc)
            return 0

    # ── 작업장 변경 추적 ──

    def _workspace_snapshot(self) -> dict:
        try:
            return {str(p.relative_to(self.workspace)): p.stat().st_mtime
                    for p in self.workspace.rglob("*") if p.is_file()}
        except OSError as exc:
            logger.warning("[%s] 작업장 스냅샷 실패: %s", self.BOT_ID, exc)
            return {}

    def _workspace_diff(self, before: dict) -> list[str]:
        after = self._workspace_snapshot()
        return sorted([k for k in after if after[k] != before.get(k)])

    # ═══════════════════════════════════════════
    # 동적 폴링 루프 — 에이전트가 다음 스캔 주기를 스스로 결정
    # ═══════════════════════════════════════════

    async def _loop(self):
        guard = MarketCloseGuard()
        logger.info("[%s] 자율 루프 시작 — 예산 %s원",
                    self.BOT_ID, f"{self._config.budget:,}")
        while self._running:
            try:
                # 주말·장외 시간은 스캔하지 않음 (5분 후 재확인)
                if kst_now().weekday() >= 5 or not guard.is_market_open():
                    await asyncio.sleep(300)
                    continue
                self.run_scan_cycle()  # _scan_count는 내부에서 증가
            except Exception as exc:
                logger.error("[%s] 사이클 오류: %s", self.BOT_ID, exc, exc_info=True)
            await asyncio.sleep(self._next_scan_minutes * 60)

    # ═══════════════════════════════════════════
    # 상태 — registry UI 위젯용 필드 추가
    # ═══════════════════════════════════════════

    def status(self) -> dict:
        base = super().status()
        cash, positions = self._current_state()
        pos_view = []
        for p in positions:
            unrealized = 0
            try:
                info = self._get_market().get_price(p["code"])
                if info and info.get("price"):
                    unrealized = (info["price"] - p["avg_price"]) * p["qty"]
            except Exception:
                pass
            pos_view.append({"code": p["code"], "qty": p["qty"],
                             "avg_price": p["avg_price"],
                             "unrealized": unrealized})
        base["positions"] = pos_view          # registry positions 위젯 data_key
        base["cash"] = cash
        base["next_scan_minutes"] = self._next_scan_minutes
        base["last_reason"] = (self._trade_log[-1].get("detail", "")
                               if self._trade_log else "")
        return base

    # ═══════════════════════════════════════════
    # BotBase 추상 메서드 — winner-1은 _loop를 직접 오버라이드하므로 형식상 구현
    # ═══════════════════════════════════════════

    def scan(self) -> list[dict]:
        return []

    def decide(self, candidates: list[dict]) -> dict:
        return {"action": "hold", "reason": "loop override"}

    def execute(self, decision: dict) -> dict:
        return {}
