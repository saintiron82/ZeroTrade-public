"""BotBase — 모든 봇의 추상 기반 클래스.

이 클래스를 상속하면 ZeroTrade 시스템에 자동 등록 가능:
- 폴링 루프 (scan → decide → execute)
- 예산 강제 (budget 초과 시 주문 거부)
- 상태 DB 영속화 (재시작 시 보유종목/손익 유지)
- 매매 DB 기록 (거래 이력 자동 저장)
- SSE 이벤트 발행 (웹 UI 실시간 업데이트)
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from src.bots.engine import TradeEngine
from src.bots.safeguards import kst_now
from src.config import PROJECT_ROOT
from src.kis.market import KISMarket
from src.kis.order import KISOrder, OrderType

logger = logging.getLogger(__name__)


@dataclass
class BotConfig:
    """봇 공통 설정."""
    interval_seconds: int = 300
    budget: int = 0
    etf_codes: list[str] = field(default_factory=list)
    instance_id: str = ""  # 파이프별 고유 키 (예: "strat-001:etf-momentum")


class BotBase(ABC):
    """모든 봇의 기반. scan → decide → execute 패턴."""

    BOT_ID: str = "base"  # 하위 클래스에서 오버라이드

    def __init__(self, config: BotConfig | dict = None):
        if isinstance(config, dict):
            self._config = BotConfig(**config)
        else:
            self._config = config or BotConfig()

        self._market: KISMarket | None = None
        self._order: KISOrder | None = None
        self._running = False
        self._task: asyncio.Task | None = None

        # 보유 상태
        self._holding_code: str = ""
        self._holding_qty: int = 0
        self._holding_price: int = 0
        self._holding_entry_time: datetime | None = None

        # 수익 추적
        self._scan_count: int = 0
        self._realized_pnl: int = 0
        self._trade_log: list[dict] = []

        # TradeEngine — buy/sell 위임 (반드시 _load_state 전에 생성)
        self.engine = TradeEngine(
            bot_id=self.BOT_ID,
            instance_id=self._config.instance_id,
            budget=self._config.budget,
            guard_config=getattr(self, '_guard_config', None),
        )

        # DB 복원
        self._load_state()

    # ═══════════════════════════════════════════
    # 하위 클래스가 반드시 구현
    # ═══════════════════════════════════════════

    @abstractmethod
    def scan(self) -> list[dict]:
        """스캔 로직. 시장 데이터를 조회하여 후보 리스트 반환.

        Returns:
            [{"code": "069500", "name": "KODEX200", "score": 2.3, ...}, ...]
        """

    @abstractmethod
    def decide(self, candidates: list[dict]) -> dict:
        """판단 로직. 스캔 결과에서 매수/매도/홀드 결정.

        Returns:
            {"action": "buy"|"sell"|"hold"|"skip", "code": "...", "reason": "..."}
        """

    @abstractmethod
    def execute(self, decision: dict) -> dict:
        """실행 로직. 판단 결과를 실제 주문으로.

        Returns:
            {"success": True/False, "order_no": "...", ...}
        """

    # ═══════════════════════════════════════════
    # 공통 기능 (오버라이드 불필요)
    # ═══════════════════════════════════════════

    def _get_market(self) -> KISMarket:
        if self._market is None:
            self._market = KISMarket()
        return self._market

    def _get_order(self) -> KISOrder:
        if self._order is None:
            self._order = KISOrder()
        return self._order

    def check_budget(self, amount: int) -> bool:
        """예산 범위 검사."""
        return self.engine.budget <= 0 or amount <= self.engine.budget

    def get_buyable_qty(self, stock_code: str, price: int) -> int:
        """실제 매수 가능 수량."""
        return self.engine.get_buyable_qty(stock_code, price)

    def _filter_derivative_etfs(self, codes: list[str]) -> list[str]:
        """파생ETF 토글 OFF면 인버스/레버리지 코드를 후보에서 제외.

        엔진 가드(_check_derivative_etf)가 최종 차단하지만, universe 단계에서
        미리 걸러 AI/스캔이 거래 불가 종목에 자원을 낭비하지 않게 한다.
        이름 조회에 실패한 코드는 보수적으로 유지한다.
        """
        import src.trading_mode as tm
        if tm.derivative_etf_enabled():
            return codes
        from src.bots.engine import stock_name_for
        from src.kis.master import is_derivative_etf
        kept = [c for c in codes if not is_derivative_etf(stock_name_for(c))]
        if len(kept) != len(codes):
            logger.debug("[%s] 파생ETF %d개 후보 제외 (토글 OFF)",
                         self.BOT_ID, len(codes) - len(kept))
        return kept

    def buy(self, code: str, qty: int, price: int = 0) -> dict:
        """매수 실행 — TradeEngine 위임."""
        result = self.engine.buy(code, qty, price)
        if result.get("success"):
            if self._holding_code == code and self._holding_qty > 0:
                old_qty = self._holding_qty
                old_price = self._holding_price or price or 0
                total_qty = old_qty + qty
                avg_price = int(round(((old_qty * old_price) + (qty * (price or old_price))) / total_qty)) if total_qty > 0 else price or 0
                self._holding_qty = total_qty
                self._holding_price = avg_price
                result["action"] = "add_buy"
                result["total_qty"] = total_qty
                result["avg_price"] = avg_price
            else:
                self._holding_code = code
                self._holding_qty = qty
                self._holding_price = price or 0
                self._holding_entry_time = kst_now()
            self._save_state()
        return result

    def sell(self, code: str = "", qty: int = 0, entry_price: int = 0,
             order_type: OrderType = OrderType.MARKET,
             reason: str = "", basis: dict | None = None) -> dict:
        """매도 실행 — TradeEngine 위임."""
        code = code or self._holding_code
        qty = qty or self._holding_qty
        entry_price = entry_price or self._holding_price

        result = self.engine.sell(code, qty, entry_price, order_type, reason=reason, basis=basis)

        if result.get("success"):
            self._realized_pnl = self.engine.realized_pnl
            # 단일 포지션 필드는 전량 매도면 클리어, 일부 매도면 잔량 유지
            if code == self._holding_code and entry_price == self._holding_price:
                if qty >= self._holding_qty:
                    self._holding_code = ""
                    self._holding_qty = 0
                    self._holding_price = 0
                    self._holding_entry_time = None
                else:
                    self._holding_qty -= qty
                    result["action"] = "partial_sell"
                    result["remaining_qty"] = self._holding_qty
            self._save_state()
        elif "실잔고 부족" in result.get("message", "") and code == self._holding_code:
            # KIS 실잔고 0 → phantom 포지션 정리 (다음 스캔 무한 재시도 방지)
            logger.warning("[%s] phantom 포지션 제거: %s — KIS 실잔고 0", self.BOT_ID, code)
            self._holding_code = ""
            self._holding_qty = 0
            self._holding_price = 0
            self._holding_entry_time = None
            self._save_state()
            result["action"] = "phantom_cleared"

        return result

    def _get_pipe_id(self) -> str:
        """instance_id에서 파이프 ID 추출."""
        iid = self._config.instance_id
        return iid.split(":")[0] if ":" in iid else ""

    def _post_to_board(self, type: str, content: str, code: str = "", extra: str = ""):
        """파이프 게시판에 글 등록 (100자 제한, extra는 500자)."""
        pipe_id = self._get_pipe_id()
        content = str(content or "")
        extra = str(extra or "")
        try:
            from src.db import get_db
            get_db().post_board(pipe_id, self.BOT_ID, type, content, code, extra)
        except Exception as e:
            logger.warning("[%s] 게시판 등록 실패: %s", self.BOT_ID, e)

    def _read_board(self, limit: int = 10) -> list[dict]:
        """파이프 게시판에서 다른 봇의 최근 글 읽기."""
        pipe_id = self._get_pipe_id()
        try:
            from src.db import get_db
            posts = get_db().get_board(pipe_id, limit)
            # 자기 글 제외, 다른 봇 글만
            return [p for p in posts if p.get("bot_id") != self.BOT_ID]
        except Exception:
            return []

    def _get_board_context(self) -> dict:
        """다른 봇의 최근 매매/의견을 구조적으로 요약.

        Returns:
            {
                "recent_trades": [{"bot_id", "code", "side", "content", "time"}],
                "opinions": [{"bot_id", "code", "content", "time"}],
                "active_codes": set(다른 봇이 최근 거래한 종목),
                "warnings": [경고성 메시지],
            }
        """
        posts = self._read_board(limit=20)
        result = {
            "recent_trades": [],
            "opinions": [],
            "active_codes": set(),
            "warnings": [],
        }
        for p in posts:
            entry = {
                "bot_id": p.get("bot_id", ""),
                "code": p.get("code", ""),
                "content": p.get("content", ""),
                "time": (p.get("timestamp", "") or "")[:16],
            }
            ptype = p.get("type", "")
            if ptype == "trade":
                result["recent_trades"].append(entry)
                if entry["code"]:
                    result["active_codes"].add(entry["code"])
            elif ptype == "opinion":
                result["opinions"].append(entry)
            elif ptype == "alert":
                result["warnings"].append(entry)
        return result

    def _get_news_context(self, count: int = 5) -> list[dict]:
        """최근 뉴스 요약 (봇 판단에 활용).

        Returns:
            [{"title", "category", "sentiment", "score", "label", "published"}]
        """
        import json as _json
        try:
            from src.db import get_db
            news = get_db().get_news(count)
            results = []
            for n in news:
                # sentiment가 JSON 문자열인 경우 파싱
                raw_sent = n.get("sentiment", "")
                label = ""
                score = 0
                if raw_sent:
                    try:
                        parsed = _json.loads(raw_sent) if raw_sent.startswith("{") else {}
                        label = parsed.get("label", parsed.get("sentiment", ""))
                        score = parsed.get("score", 0)
                    except (_json.JSONDecodeError, TypeError):
                        label = str(raw_sent)
                results.append({
                    "title": n.get("title", ""),
                    "category": n.get("category", ""),
                    "sentiment": label,
                    "score": score,
                    "published": (n.get("published_iso", "") or "")[:16],
                })
            return results
        except Exception:
            return []

    def _publish_event(self, event_type: str, data: dict):
        """이벤트 발행 (SSE/Redis 자동 분기)."""
        from src.bots.events import publish
        publish(event_type, {"bot_id": self.BOT_ID, **data})

    # ═══════════════════════════════════════════
    # 판단 기록 (decisions 테이블)
    # ═══════════════════════════════════════════

    def _log_decision(self, decision: dict) -> int:
        """봇의 매 판단을 DB decisions 테이블에 구조화 기록.

        decision dict 필드:
            action: buy/sell/hold/skip
            code: 종목코드
            reason: 판단 사유 (텍스트)
            confidence: 확신도 0~1 (선택)
            indicators: dict (선택) - 판단 근거 지표
            trade_id: int (선택) - 체결된 거래 ID

        Returns: decision_id (DB PK)
        """
        try:
            action = decision.get("action", "")
            # skip/hold는 10스캔마다만 기록 (DB 부하 방지).
            # 스캔 주기가 긴 봇은 LOG_HOLD_EVERY_SCAN=True로 매번 기록 (예: winner-1)
            if (action in ("skip", "hold")
                    and not getattr(self, "LOG_HOLD_EVERY_SCAN", False)
                    and self._scan_count % 10 != 0):
                return 0

            pipe_id = self._config.instance_id.split(":")[0] if ":" in self._config.instance_id else ""
            code = decision.get("code", self._holding_code or "")
            reason = decision.get("reason", decision.get("detail", ""))

            input_summary = (
                f"scan={self._scan_count}, "
                f"holding={self._holding_code or 'none'}({self._holding_qty}주), "
                f"code={code}"
            )
            output_summary = f"{action}: {reason}"

            from src.db import get_db
            decision_id = get_db().log_decision(
                type=f"bot_{action}",
                pipe_id=pipe_id,
                input_summary=input_summary,
                output_summary=output_summary,
                action_taken=f"{self.BOT_ID}:{action}",
                # 구조화 필드
                bot_id=self.BOT_ID,
                stock_code=code,
                action=action,
                confidence=decision.get("confidence", 0),
                indicators=decision.get("indicators"),
                trade_id=decision.get("trade_id", 0),
            )
            return decision_id
        except Exception as e:
            logger.warning("[%s] 판단 기록 실패: %s", self.BOT_ID, e)
            return 0

    def _cleanup_old_decisions(self, days: int = 7):
        """오래된 판단 기록 삭제 (기본 7일)."""
        try:
            from src.db import get_db
            cutoff = (kst_now() - timedelta(days=days)).isoformat()
            db = get_db()
            db._conn.execute(
                "DELETE FROM decisions WHERE timestamp < ? AND type LIKE 'bot_%'",
                (cutoff,)
            )
            db._conn.commit()
            logger.info("[%s] %d일 이전 판단 기록 정리 완료", self.BOT_ID, days)
        except Exception as e:
            logger.warning("[%s] 판단 기록 정리 실패: %s", self.BOT_ID, e)

    # ═══════════════════════════════════════════
    # 상태 영속화
    # ═══════════════════════════════════════════

    @property
    def state_key(self) -> str:
        """DB 상태 키. instance_id가 있으면 파이프별 독립."""
        return self._config.instance_id or self.BOT_ID

    def _save_state(self):
        """봇 상태를 DB에 저장."""
        # engine의 최신 realized_pnl 반영
        self._realized_pnl = self.engine.realized_pnl
        try:
            from src.db import get_db
            get_db().save_bot_state(self.state_key, {
                "holding_code": self._holding_code,
                "holding_qty": self._holding_qty,
                "holding_price": self._holding_price,
                "realized_pnl": self._realized_pnl,
                "scan_count": self._scan_count,
            })
        except Exception as e:
            logger.warning("[%s] 상태 저장 실패: %s", self.BOT_ID, e)

    def _load_state(self):
        """DB에서 봇 상태 복원. 파이프 holdings와 크로스체크."""
        try:
            from src.db import get_db
            state = get_db().load_bot_state(self.state_key)
            if state:
                self._holding_code = state.get("holding_code", "")
                self._holding_qty = state.get("holding_qty", 0)
                self._holding_price = state.get("holding_price", 0)
                self._realized_pnl = state.get("realized_pnl", 0)
                self._scan_count = state.get("scan_count", 0)
                # engine과 동기화
                self.engine.realized_pnl = self._realized_pnl
                logger.info("[%s] 상태 복원: %s %d주, 손익 %+d원",
                            self.BOT_ID, self._holding_code, self._holding_qty, self._realized_pnl)
        except Exception:
            pass

        # 본인 거래 이력으로 보유 복원 (봇 = 독립 가상 계좌)
        self._reconcile_with_trades()

    def _reconcile_with_trades(self):
        """봇의 자기 거래 이력만으로 보유 상태를 복원.

        파이프 holdings, 다른 봇의 보유는 보지 않는다 (봇 = 독립 가상 계좌).
        BotLedger를 통해 결정론적으로 재계산.
        """
        pipe_id = self._get_pipe_id()
        if not pipe_id:
            return

        # 봇이 이미 보유 중이면 크로스체크 불필요
        if self._holding_code and self._holding_qty > 0:
            return

        try:
            from src.db import get_db
            from src.bots.ledger import BotLedger
            trades = get_db().get_trades(pipe_id=pipe_id, source=self.BOT_ID, limit=500)
            if not trades:
                return
            ledger = BotLedger.from_trades(budget=self._config.budget, trades=trades)
            if not ledger.positions:
                return

            # 단일 포지션 모델: 가장 큰 보유 종목으로 복원
            primary = max(ledger.positions, key=lambda p: p["qty"])
            self._holding_code = primary["code"]
            self._holding_qty = primary["qty"]
            self._holding_price = primary["avg_price"]
            self._save_state()
            logger.warning(
                "[%s] 보유 복원 (자기 ledger): %s %d주 @%d원",
                self.BOT_ID, self._holding_code, self._holding_qty, self._holding_price,
            )
        except Exception as e:
            logger.debug("[%s] 거래 이력 크로스체크 실패: %s", self.BOT_ID, e)

    # ── 판단 감사 (decision audit) ──

    def _audit_pipe_id(self) -> str:
        try:
            config = getattr(self, "_config", None)
            instance_id = getattr(config, "instance_id", "") if config else ""
            return instance_id.split(":")[0] if ":" in instance_id else ""
        except Exception as exc:
            logger.warning("[%s] audit pipe id failed: %s", self.BOT_ID, exc)
            return ""

    def _audit_mode(self) -> str:
        try:
            import src.trading_mode as tm
            return str(tm.mode_info().get("mode", ""))
        except Exception as exc:
            logger.warning("[%s] audit mode failed: %s", self.BOT_ID, exc)
            return ""

    def _audit_start_run(self):
        try:
            from src.db import get_db
            from src.bots.audit import AuditRecorder
            recorder = AuditRecorder(
                get_db(),
                self.BOT_ID,
                self._audit_pipe_id(),
                getattr(self, "_scan_count", 0),
                self._audit_mode(),
            )
            recorder.start()
            return recorder
        except Exception as exc:
            logger.warning("[%s] audit start failed: %s", self.BOT_ID, exc)
            return None

    # ═══════════════════════════════════════════
    # 폴링 루프
    # ═══════════════════════════════════════════

    async def start(self):
        """봇 시작."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[%s] 시작 (간격 %ds)", self.BOT_ID, self._config.interval_seconds)

    async def stop(self):
        """봇 정지."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._market:
            self._market.close()
        if self._order:
            self._order.close()
        logger.info("[%s] 정지", self.BOT_ID)

    async def _loop(self):
        """메인 폴링 루프: scan → decide → execute."""
        while self._running:
            try:
                # 주말 체크 — 토/일은 스캔하지 않음
                if kst_now().weekday() >= 5:  # 5=토, 6=일
                    await asyncio.sleep(300)  # 5분 대기 후 재확인
                    continue

                self._scan_count += 1
                now = kst_now().strftime("%H:%M:%S")

                # 재시작/상태 누락 후에도 자기 거래 이력으로 잔여 보유를 매도 판단 대상으로 복원한다.
                if not self._holding_code or self._holding_qty <= 0:
                    self._reconcile_with_trades()

                # 1. 스캔
                candidates = self.scan()

                # 2. 판단
                decision = self.decide(candidates)

                # 3. 실행
                if decision.get("action") in ("buy", "sell"):
                    result = self.execute(decision)
                    self._publish_event("bot_trade", {
                        "action": decision["action"],
                        "code": decision.get("code", ""),
                        "result": result,
                    })

                # 로그
                if self._holding_code and self._holding_qty > 0 and decision.get("action") == "hold":
                    logger.info(
                        "[%s] 보유 평가: %s %d주 @%d -- %s",
                        self.BOT_ID,
                        self._holding_code,
                        self._holding_qty,
                        self._holding_price,
                        decision.get("reason", ""),
                    )
                self._trade_log.append({
                    "scan": self._scan_count,
                    "time": now,
                    "action": decision.get("action", ""),
                    "detail": decision.get("reason", ""),
                    "result": decision,
                })
                if len(self._trade_log) > 50:
                    self._trade_log = self._trade_log[-50:]
                self._save_state()

                # DB 판단 기록
                self._log_decision(decision)

                # 매일 1회 오래된 판단 기록 정리 (7일 이전)
                if self._scan_count == 1:
                    self._cleanup_old_decisions()

            except Exception as e:
                logger.error("[%s] 스캔 오류: %s", self.BOT_ID, e, exc_info=True)

            await asyncio.sleep(self._config.interval_seconds)

    def status(self) -> dict:
        """현재 상태."""
        unrealized = 0
        if self._holding_code and self._holding_qty > 0:
            try:
                info = self._get_market().get_price(self._holding_code)
                if info and info.get("price"):
                    unrealized = (info["price"] - self._holding_price) * self._holding_qty
            except Exception:
                pass

        return {
            "running": self._running,
            "bot_id": self.BOT_ID,
            "scan_count": self._scan_count,
            "holding": {
                "code": self._holding_code,
                "qty": self._holding_qty,
                "price": self._holding_price,
            },
            "pnl": {
                "realized": self._realized_pnl,
                "unrealized": unrealized,
                "total": self._realized_pnl + unrealized,
            },
            "config": {
                "interval": self._config.interval_seconds,
                "budget": self._config.budget,
            },
            "recent_trades": self._trade_log[-5:],
        }
