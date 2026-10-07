# Centralized Trade Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 모든 봇의 매매(매수/매도), 손익 계산, 상태 저장, 파이프 동기화, 로그 기록, 게시판 알림을 하나의 중앙 엔진(`TradeEngine`)으로 통합하여 일관성을 보장한다.

**Architecture:** 현재 `base.py`에 산재된 `buy()`, `sell()`, `_log_trade()`, `_sync_pipe_*()`, `_post_to_board()`, `_save_state()` 등을 독립 클래스 `TradeEngine`으로 추출한다. 모든 봇은 `self.engine.buy()` / `self.engine.sell()` 호출 한 번으로 전체 파이프라인(주문→손익→DB기록→파이프동기화→게시판→상태저장)이 자동 실행된다. 각 봇의 자체 매도 경로(`_sell_with_order_type`, `_execute_position_exit` 등)를 제거한다.

**Tech Stack:** Python 3.11+, SQLite, pytest, pytest-asyncio

---

## 현재 문제 요약

| 문제 | 위치 | 영향 |
|------|------|------|
| SV `_sell_with_order_type()` — board/pipe sync 누락 | stealth_volume.py:348-365 | 시간외 매도 시 게시판 미표시, 파이프 잔고 불일치 |
| mirinae `_execute_position_exit()` — 자체 매도 로직 | mirinae.py:761-818 | 시간외 매도 시 게시판 미표시 |
| `base.sell()` — order_type 하드코딩 MARKET | base.py:157 | 시간외 매도 지원 불가 → 각 봇이 우회 |
| `base.buy()` — `_holding_price` 덮어쓰기 | base.py:134 | 다중 포지션 봇에서 손익 계산 오류 |
| PnL 계산 3곳 중복 | base:169, sv:358, mirinae:793 | 불일치 리스크 |
| `strategy.sell_holding()` — 별도 PnL 계산 | strategy.py:111-124 | 봇 PnL과 파이프 PnL 이중 계산 |

## 파일 구조

| 파일 | 역할 | 변경 |
|------|------|------|
| `src/bots/engine.py` | **NEW** — TradeEngine 중앙 엔진 | 생성 |
| `tests/test_engine.py` | **NEW** — TradeEngine 테스트 | 생성 |
| `src/bots/base.py` | BotBase — engine 위임 | 수정 |
| `src/bots/stealth_volume.py` | SV — `_sell_with_order_type()` 제거 | 수정 |
| `src/bots/mirinae.py` | mirinae — 자체 매도 경로 제거 | 수정 |
| `src/bots/band_rebound.py` | BR — engine 위임 | 수정 |

---

### Task 1: TradeEngine 핵심 — sell with order_type

`base.sell()`이 `OrderType.MARKET` 하드코딩인 것이 모든 우회의 근본 원인이다. `order_type` 파라미터를 추가하면 SV/mirinae의 자체 매도 경로가 불필요해진다.

**Files:**
- Create: `src/bots/engine.py`
- Create: `tests/test_engine.py`

- [ ] **Step 1: 테스트 작성 — sell with order_type**

```python
# tests/test_engine.py
"""TradeEngine 중앙 매매 엔진 테스트."""

from unittest.mock import MagicMock, patch
import pytest

from src.kis.order import OrderResult, OrderType


def _make_engine(budget=500_000, instance_id="strat-001:test-bot", bot_id="test-bot"):
    """테스트용 TradeEngine 인스턴스 생성."""
    with patch("src.bots.engine.get_db") as mock_db, \
         patch("src.bots.engine.KISOrder") as mock_order_cls, \
         patch("src.bots.engine.KISMarket") as mock_market_cls, \
         patch("src.bots.engine.StrategyManager") as mock_mgr_cls:

        mock_db_inst = MagicMock()
        mock_db.return_value = mock_db_inst
        mock_db_inst.log_trade.return_value = 1
        mock_db_inst.log_decision.return_value = 1

        mock_order = MagicMock()
        mock_order_cls.return_value = mock_order

        mock_market = MagicMock()
        mock_market_cls.return_value = mock_market

        mock_mgr = MagicMock()
        mock_mgr_cls.return_value = mock_mgr

        from src.bots.engine import TradeEngine

        engine = TradeEngine(
            bot_id=bot_id,
            instance_id=instance_id,
            budget=budget,
        )
        # inject mocks
        engine._order = mock_order
        engine._market = mock_market
        engine._db = mock_db_inst
        engine._strategy_mgr = mock_mgr

        return engine, mock_order, mock_market, mock_db_inst, mock_mgr


class TestSell:
    """매도 + 손익 계산 + 후처리 파이프라인."""

    def test_sell_market_order(self):
        """시장가 매도: 손익 계산 + DB + board + pipe sync 모두 실행."""
        engine, mock_order, mock_market, mock_db, mock_mgr = _make_engine()

        mock_order.sell.return_value = OrderResult(
            success=True, order_no="001",
            stock_code="069500", side="sell", quantity=10, price=15200,
        )

        result = engine.sell(
            code="069500", qty=10,
            entry_price=15000, order_type=OrderType.MARKET,
        )

        assert result["success"] is True
        # PnL = (15200 - 15000) * 10 = 2000
        assert result["realized_pnl"] == 2000
        # DB 기록
        mock_db.log_trade.assert_called_once()
        call_kwargs = mock_db.log_trade.call_args
        assert call_kwargs[1]["realized_pnl"] == 2000
        # board 메시지
        mock_db.post_board.assert_called_once()
        board_content = mock_db.post_board.call_args[0][3]  # content arg
        assert "손익 +2,000원" in board_content
        # pipe sync
        mock_mgr.record_sell.assert_called_once_with("strat-001", "069500", 10, 15200)

    def test_sell_after_hours(self):
        """시간외 종가 매도: order_type이 AFTER_HOURS_CLOSE로 전달."""
        engine, mock_order, *_ = _make_engine()

        mock_order.sell.return_value = OrderResult(
            success=True, order_no="002",
            stock_code="069500", side="sell", quantity=5, price=15100,
        )

        result = engine.sell(
            code="069500", qty=5,
            entry_price=15000, order_type=OrderType.AFTER_HOURS_CLOSE,
        )

        assert result["success"] is True
        # OrderType 확인
        mock_order.sell.assert_called_once_with(
            stock_code="069500", quantity=5, order_type=OrderType.AFTER_HOURS_CLOSE,
        )

    def test_sell_zero_entry_price(self):
        """entry_price 0이면 realized_pnl = 0 (안전 장치)."""
        engine, mock_order, *_ = _make_engine()

        mock_order.sell.return_value = OrderResult(
            success=True, order_no="003",
            stock_code="069500", side="sell", quantity=3, price=10000,
        )

        result = engine.sell(
            code="069500", qty=3,
            entry_price=0, order_type=OrderType.MARKET,
        )

        assert result["realized_pnl"] == 0

    def test_sell_failure_no_side_effects(self):
        """매도 실패 시 DB/board/sync 호출 없음."""
        engine, mock_order, _, mock_db, mock_mgr = _make_engine()

        mock_order.sell.return_value = OrderResult(
            success=False, order_no="", stock_code="069500",
            side="sell", quantity=10, price=0,
        )

        result = engine.sell(
            code="069500", qty=10,
            entry_price=15000, order_type=OrderType.MARKET,
        )

        assert result["success"] is False
        mock_db.log_trade.assert_not_called()
        mock_db.post_board.assert_not_called()
        mock_mgr.record_sell.assert_not_called()

    def test_sell_price_fallback_to_market(self):
        """체결가 0이면 현재가로 대체."""
        engine, mock_order, mock_market, mock_db, _ = _make_engine()

        mock_order.sell.return_value = OrderResult(
            success=True, order_no="004",
            stock_code="069500", side="sell", quantity=2, price=0,
        )
        mock_market.get_price.return_value = {"price": 15300}

        result = engine.sell(
            code="069500", qty=2,
            entry_price=15000, order_type=OrderType.MARKET,
        )

        assert result["realized_pnl"] == (15300 - 15000) * 2


class TestBuy:
    """매수 + 후처리 파이프라인."""

    def test_buy_limit_order(self):
        """지정가 매수: DB + board + pipe sync."""
        engine, mock_order, _, mock_db, mock_mgr = _make_engine()

        mock_order.buy.return_value = OrderResult(
            success=True, order_no="B01",
            stock_code="005930", side="buy", quantity=5, price=72000,
        )

        result = engine.buy(code="005930", qty=5, price=72000)

        assert result["success"] is True
        mock_order.buy.assert_called_once_with(
            stock_code="005930", quantity=5, price=72000,
            order_type=OrderType.LIMIT,
        )
        mock_db.log_trade.assert_called_once()
        mock_db.post_board.assert_called_once()
        mock_mgr.record_buy.assert_called_once_with("strat-001", "005930", "", 5, 72000)

    def test_buy_market_order(self):
        """시장가 매수: price=0이면 MARKET."""
        engine, mock_order, *_ = _make_engine()

        mock_order.buy.return_value = OrderResult(
            success=True, order_no="B02",
            stock_code="005930", side="buy", quantity=3, price=0,
        )

        engine.buy(code="005930", qty=3, price=0)

        mock_order.buy.assert_called_once_with(
            stock_code="005930", quantity=3, price=0,
            order_type=OrderType.MARKET,
        )

    def test_buy_budget_exceeded(self):
        """예산 초과 시 주문 거부."""
        engine, mock_order, *_ = _make_engine(budget=100_000)

        result = engine.buy(code="005930", qty=10, price=72000)

        assert result["success"] is False
        assert "예산" in result["reason"]
        mock_order.buy.assert_not_called()


class TestPnL:
    """손익 계산 정확성."""

    def test_pnl_positive(self):
        engine, mock_order, *_ = _make_engine()
        mock_order.sell.return_value = OrderResult(
            success=True, order_no="P1",
            stock_code="X", side="sell", quantity=10, price=1100,
        )
        result = engine.sell(code="X", qty=10, entry_price=1000, order_type=OrderType.MARKET)
        assert result["realized_pnl"] == 1000  # (1100-1000)*10

    def test_pnl_negative(self):
        engine, mock_order, *_ = _make_engine()
        mock_order.sell.return_value = OrderResult(
            success=True, order_no="P2",
            stock_code="X", side="sell", quantity=3, price=8655,
        )
        result = engine.sell(code="X", qty=3, entry_price=8665, order_type=OrderType.MARKET)
        assert result["realized_pnl"] == -30  # (8655-8665)*3

    def test_pnl_accumulates(self):
        """여러 매도의 누적 손익."""
        engine, mock_order, *_ = _make_engine()
        mock_order.sell.return_value = OrderResult(
            success=True, order_no="P3",
            stock_code="A", side="sell", quantity=1, price=200,
        )
        engine.sell(code="A", qty=1, entry_price=100, order_type=OrderType.MARKET)
        engine.sell(code="A", qty=1, entry_price=100, order_type=OrderType.MARKET)
        assert engine.realized_pnl == 200  # 100 + 100
```

- [ ] **Step 2: 테스트 실행 — FAIL 확인**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/test_engine.py -v 2>&1 | head -30`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.bots.engine'`

- [ ] **Step 3: TradeEngine 구현**

```python
# src/bots/engine.py
"""TradeEngine — 모든 봇의 매매를 처리하는 중앙 엔진.

모든 매수/매도는 이 엔진을 통해 실행된다.
주문 → 손익 계산 → DB 기록 → 파이프 동기화 → 게시판 → 상태 저장
이 순서가 하나의 트랜잭션으로 보장된다.
"""

import logging

from src.db import get_db
from src.kis.market import KISMarket
from src.kis.order import KISOrder, OrderType

logger = logging.getLogger(__name__)


class TradeEngine:
    """중앙 매매 엔진. 봇은 이 엔진의 buy/sell만 호출한다."""

    def __init__(self, bot_id: str, instance_id: str = "", budget: int = 0):
        self.bot_id = bot_id
        self.instance_id = instance_id
        self.budget = budget
        self.realized_pnl: int = 0

        self._order: KISOrder | None = None
        self._market: KISMarket | None = None
        self._db = None
        self._strategy_mgr = None

    @property
    def pipe_id(self) -> str:
        return self.instance_id.split(":")[0] if ":" in self.instance_id else ""

    def _get_order(self) -> KISOrder:
        if self._order is None:
            self._order = KISOrder()
        return self._order

    def _get_market(self) -> KISMarket:
        if self._market is None:
            self._market = KISMarket()
        return self._market

    def _get_db(self):
        if self._db is None:
            self._db = get_db()
        return self._db

    def _get_strategy_mgr(self):
        if self._strategy_mgr is None:
            from src.strategy import StrategyManager
            self._strategy_mgr = StrategyManager()
        return self._strategy_mgr

    # ───────────────────────────────────────
    # 매수
    # ───────────────────────────────────────

    def buy(self, code: str, qty: int, price: int = 0) -> dict:
        """매수 실행 + 전체 후처리 파이프라인.

        Returns:
            {"success": bool, "order_no": str, "message": str}
        """
        if self.budget > 0 and qty * max(price, 1) > self.budget:
            return {"success": False, "reason": "예산 초과"}

        order_type = OrderType.LIMIT if price > 0 else OrderType.MARKET
        result = self._get_order().buy(
            stock_code=code, quantity=qty, price=price,
            order_type=order_type,
        )

        if result.success:
            self._log_trade("buy", code, qty, price, result.order_no)
            self._sync_pipe_buy(code, qty, price)
            self._post_to_board(f"매수 {code} {qty}주 @{price:,}원", code)

        return {
            "success": result.success,
            "order_no": result.order_no,
            "message": result.message,
        }

    # ───────────────────────────────────────
    # 매도
    # ───────────────────────────────────────

    def sell(self, code: str, qty: int, entry_price: int = 0,
             order_type: OrderType = OrderType.MARKET) -> dict:
        """매도 실행 + 손익 계산 + 전체 후처리 파이프라인.

        Args:
            code: 종목코드
            qty: 매도 수량
            entry_price: 매수 평균단가 (0이면 손익 0으로 처리)
            order_type: 주문 유형 (MARKET, AFTER_HOURS_CLOSE 등)

        Returns:
            {"success": bool, "order_no": str, "message": str,
             "sell_price": int, "realized_pnl": int}
        """
        if not code or qty <= 0:
            return {"success": False, "reason": "매도할 종목 없음",
                    "sell_price": 0, "realized_pnl": 0}

        result = self._get_order().sell(
            stock_code=code, quantity=qty, order_type=order_type,
        )

        if not result.success:
            return {
                "success": False, "order_no": result.order_no,
                "message": result.message,
                "sell_price": 0, "realized_pnl": 0,
            }

        # 체결가 결정 (시장가 매도 시 체결가 0이면 현재가 대체)
        sell_price = result.price
        if not sell_price:
            try:
                info = self._get_market().get_price(code)
                sell_price = info.get("price", 0) if info else 0
            except Exception:
                pass
        sell_price = sell_price or entry_price

        # 손익 계산 (단일 공식, 단일 지점)
        realized = (sell_price - entry_price) * qty if entry_price > 0 else 0
        self.realized_pnl += realized

        # 후처리 파이프라인 (순서 보장)
        self._log_trade("sell", code, qty, sell_price, result.order_no, realized)
        self._sync_pipe_sell(code, qty, sell_price)
        self._post_to_board(
            f"매도 {code} {qty}주 @{sell_price:,}원 (손익 {realized:+,}원)", code,
        )

        return {
            "success": True,
            "order_no": result.order_no,
            "message": result.message,
            "sell_price": sell_price,
            "realized_pnl": realized,
        }

    # ───────────────────────────────────────
    # 공통 유틸
    # ───────────────────────────────────────

    def get_buyable_qty(self, stock_code: str, price: int) -> int:
        """실제 매수 가능 수량 (증거금 + 예산 제한)."""
        market = self._get_market()
        buyable = market.get_buyable_qty(stock_code, price)
        max_qty = buyable.get("max_qty", 0)
        if self.budget > 0:
            budget_qty = self.budget // price if price > 0 else 0
            return min(max_qty, budget_qty)
        return max_qty

    def get_price(self, code: str) -> dict | None:
        """현재가 조회 (캐시 적용)."""
        try:
            return self._get_market().get_price(code)
        except Exception:
            return None

    # ───────────────────────────────────────
    # 내부 후처리
    # ───────────────────────────────────────

    def _log_trade(self, side: str, code: str, qty: int, price: int,
                   order_no: str = "", realized: int = 0) -> int:
        """DB + trades.json 기록."""
        reason = f"{self.bot_id} {'매수' if side == 'buy' else '매도'}"
        if realized:
            reason += f" (실현:{realized:+,}원)"

        trade_id = 0
        try:
            trade_id = self._get_db().log_trade(
                self.pipe_id, code, "", side, qty, price,
                order_no=order_no, success=True,
                reason=reason, source=self.bot_id,
                realized_pnl=realized,
            )
        except Exception as e:
            logger.warning("[%s] DB 기록 실패: %s", self.bot_id, e)

        try:
            from src.kis.order import OrderResult
            from src.trade.logger import TradeLogger
            result = OrderResult(
                success=True, order_no=order_no, message=reason,
                stock_code=code, side=side, quantity=qty, price=price,
            )
            TradeLogger().log_trade(result, reason=reason, strategy_id=self.pipe_id)
        except Exception as e:
            logger.warning("[%s] trades.json 기록 실패: %s", self.bot_id, e)

        return trade_id

    def _sync_pipe_buy(self, code: str, qty: int, price: int):
        if not self.pipe_id:
            return
        try:
            self._get_strategy_mgr().record_buy(self.pipe_id, code, "", qty, price)
        except Exception as e:
            logger.warning("[%s] 파이프 매수 동기화 실패: %s", self.bot_id, e)

    def _sync_pipe_sell(self, code: str, qty: int, price: int):
        if not self.pipe_id:
            return
        try:
            self._get_strategy_mgr().record_sell(self.pipe_id, code, qty, price)
        except Exception as e:
            logger.warning("[%s] 파이프 매도 동기화 실패: %s", self.bot_id, e)

    def _post_to_board(self, content: str, code: str = ""):
        if not self.pipe_id:
            return
        try:
            self._get_db().post_board(
                self.pipe_id, self.bot_id, "trade", content[:100], code,
            )
        except Exception as e:
            logger.warning("[%s] 게시판 등록 실패: %s", self.bot_id, e)

    def close(self):
        """리소스 정리."""
        if self._market:
            self._market.close()
        if self._order:
            self._order.close()
```

- [ ] **Step 4: 테스트 실행 — PASS 확인**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/test_engine.py -v`
Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add src/bots/engine.py tests/test_engine.py
git commit -m "feat: add TradeEngine — centralized buy/sell with order_type support"
```

---

### Task 2: BotBase — TradeEngine 위임

`base.py`의 `buy()`, `sell()`, `_log_trade()`, `_sync_pipe_*()`, `_post_to_board()` 를 `TradeEngine`으로 위임한다.
기존 API(`self.buy()`, `self.sell()`)는 유지하되 내부적으로 `self.engine`을 호출한다.

**Files:**
- Modify: `src/bots/base.py`
- Modify: `tests/test_engine.py` (통합 테스트 추가)

- [ ] **Step 1: 테스트 작성 — BotBase가 engine을 사용하는지 확인**

```python
# tests/test_engine.py 에 추가

class TestBotBaseIntegration:
    """BotBase가 TradeEngine을 통해 매매하는지 검증."""

    def test_base_sell_delegates_to_engine(self):
        """base.sell()이 engine.sell()을 호출."""
        with patch("src.bots.engine.get_db") as mock_db, \
             patch("src.bots.engine.KISOrder") as mock_order_cls, \
             patch("src.bots.engine.KISMarket"), \
             patch("src.bots.engine.StrategyManager"), \
             patch("src.bots.base.get_db"):

            mock_db_inst = MagicMock()
            mock_db.return_value = mock_db_inst
            mock_db_inst.log_trade.return_value = 1
            mock_db_inst.load_bot_state.return_value = None

            mock_order = MagicMock()
            mock_order_cls.return_value = mock_order
            mock_order.sell.return_value = OrderResult(
                success=True, order_no="S01",
                stock_code="005930", side="sell", quantity=5, price=73000,
            )

            # 구체 클래스 생성
            from src.bots.base import BotBase, BotConfig

            class TestBot(BotBase):
                BOT_ID = "test"
                def scan(self): return []
                def decide(self, c): return {"action": "hold"}
                def execute(self, d): return {}

            bot = TestBot(BotConfig(instance_id="strat-001:test"))
            bot._holding_code = "005930"
            bot._holding_qty = 5
            bot._holding_price = 72000

            result = bot.sell()

            assert result["success"] is True
            assert result["realized_pnl"] == 5000  # (73000-72000)*5

    def test_base_sell_with_after_hours(self):
        """base.sell(order_type=AFTER_HOURS_CLOSE) 정상 동작."""
        with patch("src.bots.engine.get_db") as mock_db, \
             patch("src.bots.engine.KISOrder") as mock_order_cls, \
             patch("src.bots.engine.KISMarket"), \
             patch("src.bots.engine.StrategyManager"), \
             patch("src.bots.base.get_db"):

            mock_db_inst = MagicMock()
            mock_db.return_value = mock_db_inst
            mock_db_inst.log_trade.return_value = 1
            mock_db_inst.load_bot_state.return_value = None

            mock_order = MagicMock()
            mock_order_cls.return_value = mock_order
            mock_order.sell.return_value = OrderResult(
                success=True, order_no="S02",
                stock_code="005930", side="sell", quantity=5, price=72500,
            )

            from src.bots.base import BotBase, BotConfig

            class TestBot(BotBase):
                BOT_ID = "test"
                def scan(self): return []
                def decide(self, c): return {"action": "hold"}
                def execute(self, d): return {}

            bot = TestBot(BotConfig(instance_id="strat-001:test"))
            bot._holding_code = "005930"
            bot._holding_qty = 5
            bot._holding_price = 72000

            result = bot.sell(order_type=OrderType.AFTER_HOURS_CLOSE)

            mock_order.sell.assert_called_once_with(
                stock_code="005930", quantity=5,
                order_type=OrderType.AFTER_HOURS_CLOSE,
            )
```

- [ ] **Step 2: 테스트 실행 — FAIL 확인**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/test_engine.py::TestBotBaseIntegration -v`
Expected: FAIL — `sell()` got unexpected keyword argument `order_type`

- [ ] **Step 3: base.py 수정 — engine 위임**

`base.py`에서 변경할 부분:

**1) `__init__`에 engine 생성 추가:**
```python
# base.py __init__ 에 추가 (self._load_state() 전에)
from src.bots.engine import TradeEngine
self.engine = TradeEngine(
    bot_id=self.BOT_ID,
    instance_id=self._config.instance_id,
    budget=self._config.budget,
)
```

**2) `buy()` 교체:**
```python
def buy(self, code: str, qty: int, price: int = 0) -> dict:
    """매수 실행 — TradeEngine 위임."""
    result = self.engine.buy(code, qty, price)
    if result.get("success"):
        self._holding_code = code
        self._holding_qty = qty
        self._holding_price = price or 0
        self._save_state()
    return result
```

**3) `sell()` 교체 — order_type 파라미터 추가:**
```python
def sell(self, code: str = "", qty: int = 0, entry_price: int = 0,
         order_type: OrderType = OrderType.MARKET) -> dict:
    """매도 실행 — TradeEngine 위임."""
    code = code or self._holding_code
    qty = qty or self._holding_qty
    entry_price = entry_price or self._holding_price

    result = self.engine.sell(code, qty, entry_price, order_type)

    if result.get("success"):
        self._realized_pnl = self.engine.realized_pnl
        # 단일 포지션 필드 클리어 (entry_price 미전달 = 단일 포지션 봇)
        if not entry_price or entry_price == self._holding_price:
            self._holding_code = ""
            self._holding_qty = 0
            self._holding_price = 0
        self._save_state()

    return result
```

**4) 제거할 메서드들** (engine으로 이동 완료):
- `_log_trade()` — engine._log_trade()으로 대체
- `_sync_pipe_buy()` — engine._sync_pipe_buy()으로 대체
- `_sync_pipe_sell()` — engine._sync_pipe_sell()으로 대체

**5) 유지할 메서드들** (봇 컨텍스트 의존):
- `_post_to_board()` — opinion/alert 등 매매 외 용도로 봇이 직접 사용
- `_log_decision()` — 봇 판단 기록 (매매와 독립)
- `_save_state()` / `_load_state()` — 봇 상태 영속화
- `check_budget()` / `get_buyable_qty()` — engine에 위임하되 기존 API 유지

- [ ] **Step 4: 테스트 실행 — PASS 확인**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/test_engine.py -v`
Expected: ALL PASS

- [ ] **Step 5: 기존 테스트도 PASS 확인**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/ -v`
Expected: ALL PASS (기존 테스트 깨지지 않음)

- [ ] **Step 6: 커밋**

```bash
git add src/bots/base.py src/bots/engine.py tests/test_engine.py
git commit -m "refactor: BotBase delegates buy/sell to TradeEngine"
```

---

### Task 3: SV 봇 — `_sell_with_order_type()` 제거

SV의 자체 매도 경로를 제거하고 `base.sell(order_type=...)` 을 사용한다.

**Files:**
- Modify: `src/bots/stealth_volume.py`

- [ ] **Step 1: `_sell_with_order_type()` 메서드 전체 삭제**

`stealth_volume.py:348-365`의 `_sell_with_order_type` 메서드를 삭제한다.

- [ ] **Step 2: `execute()` 에서 호출부 변경**

변경 전 (stealth_volume.py 약 line 405-411):
```python
order_type_str = decision.get("order_type", "MARKET")
if order_type_str == "AFTER_HOURS":
    from src.kis.order import OrderType
    result = self._sell_with_order_type(OrderType.AFTER_HOURS_CLOSE)
else:
    result = self.sell()
```

변경 후:
```python
from src.kis.order import OrderType
order_type_str = decision.get("order_type", "MARKET")
order_type = OrderType.AFTER_HOURS_CLOSE if order_type_str == "AFTER_HOURS" else OrderType.MARKET
result = self.sell(order_type=order_type)
```

- [ ] **Step 3: 테스트 실행**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: 커밋**

```bash
git add src/bots/stealth_volume.py
git commit -m "refactor: SV bot uses base.sell(order_type) instead of custom _sell_with_order_type"
```

---

### Task 4: mirinae 봇 — 자체 매도 경로 제거

mirinae의 `_execute_position_exit()` 에서 직접 `order.sell()` 호출을 제거하고 `base.sell(order_type=...)` 을 사용한다.

**Files:**
- Modify: `src/bots/mirinae.py`

- [ ] **Step 1: `_execute_position_exit()` 변경**

변경 전 (mirinae.py 약 line 782-799):
```python
order_type_str = exit_action.get("order_type", "MARKET")
if order_type_str == "AFTER_HOURS":
    from src.kis.order import OrderType
    order_obj = self._get_order()
    result = order_obj.sell(
        stock_code=code, quantity=pos["qty"],
        order_type=OrderType.AFTER_HOURS_CLOSE,
    )
    if result.success:
        sell_price = result.price or pos["price"]
        realized = (sell_price - pos["price"]) * pos["qty"]
        self._realized_pnl += realized
        self._log_trade("sell", code, pos["qty"], sell_price, result.order_no, realized)
        self._sync_pipe_sell(code, pos["qty"], sell_price)
    result_dict = {"success": result.success, "order_no": result.order_no, "message": result.message}
else:
    result_dict = self.sell(code, pos["qty"], entry_price=pos["price"])
```

변경 후:
```python
from src.kis.order import OrderType
order_type_str = exit_action.get("order_type", "MARKET")
order_type = OrderType.AFTER_HOURS_CLOSE if order_type_str == "AFTER_HOURS" else OrderType.MARKET
result_dict = self.sell(code, pos["qty"], entry_price=pos["price"], order_type=order_type)
```

- [ ] **Step 2: 테스트 실행**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: 커밋**

```bash
git add src/bots/mirinae.py
git commit -m "refactor: mirinae bot uses base.sell(order_type) instead of direct order.sell"
```

---

### Task 5: BR 봇 — `_execute_sell`에서 entry_price 명시 확인

BR 봇은 이미 `base.sell(entry_price=pos["price"])`을 사용하므로 구조적 변경은 적다. 하지만 `_execute_sell_all()`에서 장 마감 시 `order_type`을 전달하도록 수정한다.

**Files:**
- Modify: `src/bots/band_rebound.py`

- [ ] **Step 1: `_execute_sell()` 에 order_type 전달 지원**

변경 전 (band_rebound.py 약 line 482):
```python
result = self.sell(code, sell_qty, entry_price=pos["price"])
```

변경 후:
```python
from src.kis.order import OrderType
order_type_str = decision.get("order_type", "MARKET")
order_type = OrderType.AFTER_HOURS_CLOSE if order_type_str == "AFTER_HOURS" else OrderType.MARKET
result = self.sell(code, sell_qty, entry_price=pos["price"], order_type=order_type)
```

- [ ] **Step 2: `_execute_sell_all()` 에서 order_type 전파**

`_execute_sell_all()` 에서 `sell_decision`에 `"order_type"` 필드를 추가한다.
현재 `_execute_sell_all()`의 `decision`에 `order_type`이 전달되면 각 개별 매도에도 전파:

변경 전 (약 line 501-506):
```python
sell_decision = {
    "action": "sell",
    "code": pos["code"],
    "qty": pos["qty"],
    "reason": decision.get("reason", "전체 청산"),
    "price": pos["price"],
}
```

변경 후:
```python
sell_decision = {
    "action": "sell",
    "code": pos["code"],
    "qty": pos["qty"],
    "reason": decision.get("reason", "전체 청산"),
    "price": pos["price"],
    "order_type": decision.get("order_type", "MARKET"),
}
```

- [ ] **Step 3: 테스트 실행**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: 커밋**

```bash
git add src/bots/band_rebound.py
git commit -m "refactor: BR bot passes order_type through sell pipeline"
```

---

### Task 6: base.py 잔여 중복 메서드 정리

engine으로 이동 완료된 메서드들을 base.py에서 제거하고, engine으로 위임하는 래퍼만 남긴다.

**Files:**
- Modify: `src/bots/base.py`

- [ ] **Step 1: 제거할 메서드 확인**

base.py에서 다음 메서드를 삭제 (engine에 이미 존재):
- `_log_trade()` (line 304-338) — `engine._log_trade()`으로 대체
- `_sync_pipe_buy()` (line 188-198) — `engine._sync_pipe_buy()`으로 대체
- `_sync_pipe_sell()` (line 200-210) — `engine._sync_pipe_sell()`으로 대체
- `check_budget()` (line 105-110) — `engine.budget` 사용
- `get_buyable_qty()` (line 112-121) — `engine.get_buyable_qty()` 위임

단, `_post_to_board()`는 매매 외 용도(opinion, alert)로 봇이 직접 사용하므로 **유지**.
`_get_pipe_id()`도 `_post_to_board()`, `_log_decision()` 등에서 사용하므로 **유지**.

- [ ] **Step 2: 래퍼 추가**

```python
def check_budget(self, amount: int) -> bool:
    """예산 범위 검사."""
    return self.engine.budget <= 0 or amount <= self.engine.budget

def get_buyable_qty(self, stock_code: str, price: int) -> int:
    """실제 매수 가능 수량."""
    return self.engine.get_buyable_qty(stock_code, price)
```

- [ ] **Step 3: 다른 봇이 삭제된 메서드를 직접 호출하지 않는지 확인**

Run: `cd /Users/saintiron/Projects/FF && grep -rn "_log_trade\|_sync_pipe_buy\|_sync_pipe_sell" src/bots/ --include="*.py" | grep -v engine.py | grep -v base.py`

모든 호출이 `base.sell()` / `base.buy()` 경유로 변경되었으므로, 직접 호출이 없어야 한다.
만약 남아있다면 해당 봇도 수정한다.

- [ ] **Step 4: 테스트 실행**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add src/bots/base.py
git commit -m "refactor: remove duplicated methods from BotBase, delegate to TradeEngine"
```

---

### Task 7: engine 상태 저장/복원 연동

현재 `_save_state()`/`_load_state()`는 base.py에 있고 `self._realized_pnl`을 저장한다.
engine의 `realized_pnl`과 base의 `_realized_pnl`이 항상 동기화되도록 한다.

**Files:**
- Modify: `src/bots/base.py`

- [ ] **Step 1: `_load_state()`에서 engine.realized_pnl 복원**

```python
def _load_state(self):
    """DB에서 봇 상태 복원."""
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
    self._reconcile_with_pipe()
```

- [ ] **Step 2: `_save_state()`에서 engine.realized_pnl 반영**

```python
def _save_state(self):
    """봇 상태를 DB에 저장."""
    # engine의 최신 pnl 반영
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
```

- [ ] **Step 3: 테스트 실행**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: 커밋**

```bash
git add src/bots/base.py
git commit -m "fix: sync engine.realized_pnl with bot state save/restore"
```

---

### Task 8: 최종 검증 — 전체 매도 경로가 engine 경유인지 확인

**Files:** (읽기 전용 확인)

- [ ] **Step 1: 자체 매도 경로가 없는지 grep**

```bash
# order.sell 직접 호출이 engine.py 외에 없어야 함
cd /Users/saintiron/Projects/FF
grep -rn "order\.sell\|order_obj\.sell\|_get_order()\.sell" src/bots/ --include="*.py" | grep -v engine.py

# _sell_with_order_type이 완전 제거되었는지
grep -rn "_sell_with_order_type" src/bots/ --include="*.py"

# realized 직접 계산이 engine.py 외에 없어야 함
grep -rn "sell_price.*-.*price.*\*.*qty\|realized.*=.*sell.*-" src/bots/ --include="*.py" | grep -v engine.py
```

Expected: 모든 결과가 비어있거나 base.py의 `status()` 같은 조회 전용 코드만 남아야 함.

- [ ] **Step 2: 전체 테스트 실행**

Run: `cd /Users/saintiron/Projects/FF && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: 최종 커밋**

```bash
git add -A
git commit -m "verify: all sell paths go through TradeEngine"
```

---

## 변경 요약

| Before | After |
|--------|-------|
| `base.sell()` — MARKET 하드코딩 | `engine.sell(order_type=...)` — 모든 주문 유형 지원 |
| SV `_sell_with_order_type()` — board/sync 누락 | 삭제 → `base.sell(order_type=AFTER_HOURS_CLOSE)` |
| mirinae `_execute_position_exit()` — 자체 매도 | 단순화 → `base.sell(order_type=...)` |
| PnL 3곳 중복 계산 | `engine.sell()` 내 단일 계산 |
| `_log_trade` / `_sync_pipe_*` base에 산재 | `engine` 내부 private 메서드 |
| 봇별 후처리 누락 리스크 | engine이 순서 보장: 주문→PnL→DB→sync→board |
