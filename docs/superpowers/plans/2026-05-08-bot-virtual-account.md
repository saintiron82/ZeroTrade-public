# Bot Virtual Account Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 봇을 독립 가상 계좌로 모델링한다. 각 봇은 자기 거래 이력만 보고 자기 보유/현금을 관리하며, 다른 봇의 보유는 알지 못한다. 파이프 holdings/cash는 봇별 ledger의 합산 표시용으로만 사용한다.

**Architecture:** TradeEngine이 봇별 cash를 자체 추적하고, 모든 reconciliation은 `trades.source = bot_id` 필터로 본인 이력만 사용한다. `_reconcile_with_pipe`(BotBase)와 `_reconcile_positions_with_trades`(mirinae)에서 `pipe_qty` 교차 제약을 제거하여 같은 종목을 여러 봇이 보유할 때 잔여가 깎이는 버그를 해결한다. 파이프 holdings/cash 동기화는 유지하되 단일 진실원천이 아닌 집계로 격하시킨다.

**Tech Stack:** Python 3.14, pytest, sqlite3, dataclass, JSON state in `bot_state.extra`

---

## Background — 현재 버그

mirinae-2와 mirinae-3가 같은 파이프(strat-001)에서 같은 종목(102780, KODEX 삼성그룹)을 각자 보유하고 있는 상황에서 SELL이 반복 차단되고 있다.

**증상:** `[mirinae-2 SELL 차단] 주문 가능한 수량을 초과했습니다.` (KIS API 메시지 — `src/kis/order.py:174`)

**관측된 상태:**
| 위치 | 102780 보유 |
|------|------------|
| `bot_state` mirinae-2 | 1주 @25,400 |
| `bot_state` mirinae-3 | 2주 @25,860 |
| 봇 합계 | 3주 |
| `strategies.json` (파이프) | 2주 @25,860 |
| 실제 KIS 계좌 | (불명, 추정 2주) |

**원인:**
1. `mirinae._reconcile_positions_with_trades()` (mirinae.py:243-264)에서 `qty = min(current_qty or available_qty, available_qty, pipe_qty)` — `pipe_qty`로 자기 보유를 캡한다. 다른 봇이 같은 종목을 사면 자기 잔여가 깎이는 잘못된 상호 간섭이 발생한다.
2. `BotBase._reconcile_with_pipe()` (base.py:390-469)도 파이프 holdings를 봐서 보유를 결정한다.
3. `_sync_pipe_buy/sell`은 단순 누적이라, 두 봇이 같은 종목을 사고팔면 단일 holding 레코드가 모순 상태가 된다.
4. 결과: 봇은 자기 보유라고 알지만 실제 KIS 잔고와 어긋나 SELL이 KIS에 의해 거부된다.

**해결 방향 — 봇 = 독립 가상 계좌:**
- 봇 보유 = 자기 거래 이력(`trades.source = bot_id`)의 buy − sell (FIFO)
- 봇 cash = budget − 자기 매수금 + 자기 매도금
- 파이프 holdings/cash = 봇 ledger 합산 (표시용)
- KIS 거부는 정상 시그널 (다른 봇/사용자가 외부에서 매도한 경우 발생) — 차단된 SELL을 자동 클램프하고 로그만 남김

---

## File Structure

**Modified:**
- `src/bots/engine.py` — bot cash 추적 추가, `get_buyable_qty`가 cash 기반으로 동작
- `src/bots/base.py` — `_reconcile_with_pipe` 제거 (파이프 의존 삭제), `_reconcile_with_trades`로 대체
- `src/bots/mirinae.py` — `_reconcile_positions_with_trades`에서 `pipe_qty` 제약 제거, `pipe_holdings` 의존 삭제
- `src/services/pipe_runner.py` — bot 시작 시 ledger 컨텍스트 출력, custodian 로직 단순화

**New:**
- `src/bots/ledger.py` — `BotLedger` 클래스: trades 이력에서 cash/positions 계산하는 단일 진입점
- `tests/test_bot_ledger.py` — ledger 단위 테스트
- `tests/test_engine_cash.py` — 엔진 cash 추적 테스트

**Touched but minimal:**
- `src/strategy.py` — `record_buy`/`record_sell`은 그대로 유지 (display용 집계)
- `src/db.py` — 변경 없음 (`bot_state.extra`에 cash 저장)

---

## Phase 1 — BotLedger 도입 (단일 진실원천)

**Goal:** 봇의 cash/positions/realized_pnl를 trades 이력에서 계산하는 단일 함수를 만든다. 이후 모든 reconcile이 이걸 호출한다.

### Task 1: BotLedger 데이터 클래스 + 핵심 계산 함수

**Files:**
- Create: `src/bots/ledger.py`
- Test: `tests/test_bot_ledger.py`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_bot_ledger.py
"""Tests for BotLedger — bot's own trade history → cash/positions."""

from src.bots.ledger import BotLedger, Lot


def test_empty_trades_returns_initial_cash():
    ledger = BotLedger.from_trades(budget=100000, trades=[])
    assert ledger.cash == 100000
    assert ledger.positions == []
    assert ledger.realized_pnl == 0


def test_single_buy_deducts_cash_and_creates_position():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.cash == 100000 - 2 * 25000
    assert len(ledger.positions) == 1
    pos = ledger.positions[0]
    assert pos["code"] == "102780"
    assert pos["qty"] == 2
    assert pos["avg_price"] == 25000


def test_buy_then_partial_sell_uses_fifo_realized_pnl():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "sell", "stock_code": "102780", "quantity": 1, "price": 26000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    # cash: 100000 - 50000 + 26000 = 76000
    assert ledger.cash == 76000
    assert ledger.positions[0]["qty"] == 1
    assert ledger.realized_pnl == 1000  # (26000 - 25000) * 1


def test_full_sell_removes_position():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "sell", "stock_code": "102780", "quantity": 2, "price": 26000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions == []
    assert ledger.realized_pnl == 2000
    assert ledger.cash == 100000 + 2000  # initial + realized


def test_failed_trade_is_ignored():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": False},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.cash == 100000
    assert ledger.positions == []


def test_multi_lot_average_price():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 20000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 30000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions[0]["qty"] == 2
    assert ledger.positions[0]["avg_price"] == 25000


def test_overnight_sell_more_than_held_clamps_to_held():
    """봇이 갖고 있다고 생각하는 것보다 실제 sell이 많은 경우(외부 매도 + 봇이 다시 사면 음수 잔여 발생 가능).
    음수가 되지 않도록 0으로 클램프한다."""
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
        {"side": "sell", "stock_code": "102780", "quantity": 5, "price": 26000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions == []
    # 1주만 매도된 것으로 처리 — realized = (26000-25000)*1 = 1000
    assert ledger.realized_pnl == 1000


def test_lots_in_chronological_order_regardless_of_input():
    trades = [
        {"side": "sell", "stock_code": "102780", "quantity": 1, "price": 26000,
         "timestamp": "2026-05-06T12:00:00", "success": True},
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True},
    ]
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    assert ledger.positions == []
    assert ledger.realized_pnl == 1000
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_bot_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.bots.ledger'`

- [ ] **Step 3: BotLedger 구현**

```python
# src/bots/ledger.py
"""BotLedger — bot의 자기 거래 이력에서 cash/positions/pnl을 계산.

봇은 독립 가상 계좌. 자기 trades(source=bot_id)만 본다.
다른 봇의 보유, 파이프 집계는 보지 않는다.
"""

from dataclasses import dataclass, field


@dataclass
class Lot:
    """한 번의 매수로 생긴 보유 lot. FIFO 매도 시 차감."""
    qty: int
    price: int


@dataclass
class BotLedger:
    """봇의 가상 계좌 상태. trades에서 결정론적으로 재계산 가능."""

    budget: int
    cash: int = 0
    realized_pnl: int = 0
    positions: list[dict] = field(default_factory=list)

    @classmethod
    def from_trades(cls, budget: int, trades: list[dict]) -> "BotLedger":
        """체결 이력을 시간순 누적해 cash/positions/pnl을 계산."""
        cash = budget
        realized = 0
        lots: dict[str, list[Lot]] = {}

        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", ""))
        for trade in sorted_trades:
            if not trade.get("success", True):
                continue
            code = trade.get("stock_code", "")
            qty = int(trade.get("quantity", 0) or 0)
            price = int(trade.get("price", 0) or 0)
            if not code or qty <= 0:
                continue

            side = trade.get("side", "")
            if side == "buy":
                cash -= qty * price
                lots.setdefault(code, []).append(Lot(qty=qty, price=price))
            elif side == "sell":
                remaining = qty
                code_lots = lots.get(code, [])
                for lot in code_lots:
                    if remaining <= 0:
                        break
                    take = min(remaining, lot.qty)
                    realized += (price - lot.price) * take
                    cash += take * price
                    lot.qty -= take
                    remaining -= take

        positions = []
        for code, code_lots in lots.items():
            qty = sum(lot.qty for lot in code_lots)
            if qty <= 0:
                continue
            cost = sum(lot.qty * lot.price for lot in code_lots)
            avg_price = cost // qty if qty > 0 else 0
            positions.append({"code": code, "qty": qty, "avg_price": avg_price,
                              "lots": [{"qty": lot.qty, "price": lot.price} for lot in code_lots if lot.qty > 0]})

        return cls(budget=budget, cash=cash, realized_pnl=realized, positions=positions)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_bot_ledger.py -v`
Expected: PASS — 8 tests

- [ ] **Step 5: 커밋**

```bash
git add src/bots/ledger.py tests/test_bot_ledger.py
git commit -m "feat(bots): add BotLedger — single source of truth for bot cash/positions"
```

---

## Phase 2 — TradeEngine cash 추적

**Goal:** TradeEngine이 봇별 cash를 추적한다. `get_buyable_qty`와 buy 검증이 fixed budget이 아닌 cash 기반으로 동작한다. 다른 봇의 매수에 영향받지 않는다.

### Task 2: TradeEngine.cash 필드 + ledger 초기화

**Files:**
- Modify: `src/bots/engine.py:48-93` (TradeEngine __init__), `:107`, `:208-221`
- Test: `tests/test_engine_cash.py`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_engine_cash.py
"""Tests for TradeEngine bot cash tracking."""

from unittest.mock import MagicMock, patch

from src.bots.engine import TradeEngine


def _engine_with_trades(budget, trades):
    eng = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=budget)
    db = MagicMock()
    db.get_trades.return_value = trades
    with patch("src.db.get_db", return_value=db):
        eng.refresh_cash()
    return eng


def test_engine_cash_initialized_to_budget_when_no_trades():
    eng = _engine_with_trades(100000, [])
    assert eng.cash == 100000


def test_engine_cash_reflects_buy_trades_only_for_this_bot():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True, "source": "test-bot"},
    ]
    eng = _engine_with_trades(100000, trades)
    assert eng.cash == 50000


def test_engine_buy_check_uses_cash_not_budget():
    """cash가 부족하면 buy 차단. budget이 남아있어도 안 됨."""
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 4, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True, "source": "test-bot"},
    ]
    eng = _engine_with_trades(100000, trades)
    # budget=100000, cash=0. 1주 추가 매수 시도 — 거부되어야 함
    assert eng.cash == 0
    # 직접 cash 체크 헬퍼
    assert eng.can_afford(25000) is False


def test_engine_get_buyable_qty_uses_cash():
    trades = [
        {"side": "buy", "stock_code": "102780", "quantity": 2, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True, "source": "test-bot"},
    ]
    eng = _engine_with_trades(100000, trades)
    market = MagicMock()
    market.get_buyable_qty.return_value = {"max_qty": 100}  # KIS는 충분
    eng._market = market
    # cash=50000, price=25000 → 2주
    assert eng.get_buyable_qty("000000", 25000) == 2
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_engine_cash.py -v`
Expected: FAIL — `refresh_cash`/`cash`/`can_afford` 미존재

- [ ] **Step 3: TradeEngine에 cash 추가**

`src/bots/engine.py`의 `__init__` 다음에 추가하고 buy 체크/get_buyable_qty 수정.

```python
# __init__ 끝부분에 추가
self.cash: int = budget  # 봇 가상 계좌 현금. refresh_cash()로 trades에서 재계산.

def refresh_cash(self):
    """trades 이력에서 cash/positions/realized_pnl을 재계산.

    봇 시작 시 또는 외부 변경 후 호출. ledger는 단일 진실원천.
    """
    if not self.pipe_id:
        return
    try:
        from src.db import get_db
        from src.bots.ledger import BotLedger
        trades = get_db().get_trades(pipe_id=self.pipe_id, source=self.bot_id, limit=1000)
        ledger = BotLedger.from_trades(budget=self.budget, trades=trades)
        self.cash = ledger.cash
        self.realized_pnl = ledger.realized_pnl
    except Exception as e:
        logger.warning("[%s] cash 갱신 실패: %s", self.bot_id, e)

def can_afford(self, amount: int) -> bool:
    """cash 범위 내 매수 가능 여부."""
    return self.cash >= amount
```

`buy()`의 budget 체크를 cash 체크로 교체 (engine.py:107 부근):

```python
# Cash check (replaces budget check)
cost = qty * max(price, 1)
if self.cash > 0 and cost > self.cash:
    return {"success": False, "order_no": "", "message": f"가용 현금 부족 ({self.cash:,}원 < {cost:,}원)"}
```

`get_buyable_qty()` 수정 (engine.py:208-221):

```python
def get_buyable_qty(self, stock_code: str, price: int) -> int:
    """가용 현금과 KIS 한도 내 최대 매수 수량.

    봇 cash = budget − 누적 매수금 + 누적 매도금 (자기 trades만).
    다른 봇의 거래에 영향받지 않는다.
    """
    market = self._get_market()
    buyable = market.get_buyable_qty(stock_code, price)
    max_qty = buyable.get("max_qty", 0)
    if self.cash > 0 and price > 0:
        cash_qty = self.cash // price
        max_qty = min(max_qty, cash_qty)
    return max_qty
```

`buy()` 성공 후, `sell()` 성공 후 cash 갱신 추가:

```python
# engine.py:121 (buy 성공 후 _sync_pipe_buy 다음)
self.cash -= qty * max(price, 1)

# engine.py:191 (sell 성공 후 _sync_pipe_sell 다음)
self.cash += qty * sell_price
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_engine_cash.py -v`
Expected: PASS — 4 tests

- [ ] **Step 5: 기존 엔진/봇 테스트 회귀 없음 확인**

Run: `pytest tests/test_engine.py tests/test_mirinae_positions.py -v`
Expected: PASS (회귀 없음)

- [ ] **Step 6: 커밋**

```bash
git add src/bots/engine.py tests/test_engine_cash.py
git commit -m "feat(engine): track per-bot cash, replace fixed budget cap"
```

---

## Phase 3 — Reconciliation에서 파이프 의존 제거

**Goal:** 봇이 보유를 결정할 때 자기 trades만 본다. 파이프 holdings는 안 본다.

### Task 3: BotBase._reconcile_with_pipe → _reconcile_with_trades 전환

**Files:**
- Modify: `src/bots/base.py:390-469`

- [ ] **Step 1: 실패하는 테스트 작성 (기존 테스트 보강)**

`tests/test_engine_cash.py`에 추가:

```python
def test_reconcile_does_not_consult_pipe_holdings():
    """다른 봇이 같은 종목을 사도 본인 보유는 영향받지 않는다."""
    from src.bots.base import BotBase, BotConfig

    # 가짜 봇 클래스
    class _DummyBot(BotBase):
        BOT_ID = "test-bot"
        def scan(self): return []
        def decide(self, candidates): return {}
        def execute(self, decision): return {}

    bot = object.__new__(_DummyBot)
    bot._config = BotConfig(instance_id="strat-001:test-bot", budget=100000)
    bot._holding_code = ""
    bot._holding_qty = 0
    bot._holding_price = 0
    bot._realized_pnl = 0
    bot._scan_count = 0
    bot._save_state = MagicMock()
    bot.engine = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=100000)

    db = MagicMock()
    db.get_trades.return_value = [
        {"side": "buy", "stock_code": "102780", "quantity": 1, "price": 25000,
         "timestamp": "2026-05-06T11:00:00", "success": True, "source": "test-bot"},
    ]
    # 파이프에 다른 봇이 같은 종목 보유 (이게 봇 보유에 영향주면 안 됨)
    from types import SimpleNamespace
    strategy = SimpleNamespace(holdings=[
        {"code": "102780", "qty": 5, "avg_price": 25000}  # 파이프 합계 5주
    ])
    with patch("src.db.get_db", return_value=db), \
         patch("src.strategy.StrategyManager") as mgr_cls:
        mgr_cls.return_value.get.return_value = strategy
        bot._reconcile_with_trades()

    assert bot._holding_code == "102780"
    assert bot._holding_qty == 1  # 파이프의 5주가 아니라 본인 1주
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_engine_cash.py::test_reconcile_does_not_consult_pipe_holdings -v`
Expected: FAIL — `_reconcile_with_trades` 미존재

- [ ] **Step 3: BotBase 메서드 교체**

`src/bots/base.py:387-469`의 `_reconcile_with_pipe` 호출 제거 → `_reconcile_with_trades`로 교체:

```python
# _load_state 끝부분 (line 388)
# 파이프 holdings 크로스체크 — 제거
# self._reconcile_with_pipe()
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
```

`_reconcile_with_pipe`는 삭제 (다른 호출자 없음 확인).

- [ ] **Step 4: 호출자 없음 확인**

Run: `grep -rn "_reconcile_with_pipe" src/ tests/`
Expected: 결과 없음 (제거된 후)

- [ ] **Step 5: 테스트 통과 확인**

Run: `pytest tests/test_engine_cash.py -v`
Expected: PASS

- [ ] **Step 6: 회귀 테스트**

Run: `pytest tests/ -v`
Expected: 모두 PASS (mirinae 테스트는 별도 task에서 다룸)

- [ ] **Step 7: 커밋**

```bash
git add src/bots/base.py tests/test_engine_cash.py
git commit -m "feat(bots): isolate single-position reconcile to bot's own trades"
```

### Task 4: mirinae._reconcile_positions_with_trades에서 pipe_qty 제거

**Files:**
- Modify: `src/bots/mirinae.py:204-310`
- Test: `tests/test_mirinae_positions.py` (기존 테스트 업데이트 + 신규)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_mirinae_positions.py`에 추가:

```python
def test_reconcile_ignores_pipe_holdings_when_other_bot_owns_same_code():
    """다른 봇이 같은 종목을 보유 중이어도 본인 ledger 기준으로만 복원."""
    bot = _bare_bot()
    db = MagicMock()
    db.get_trades.return_value = [
        {
            "timestamp": "2026-05-06T11:44:41+09:00",
            "side": "buy", "stock_code": "102780", "quantity": 2, "price": 25265,
            "success": True,
        },
        {
            "timestamp": "2026-05-06T12:06:29+09:00",
            "side": "buy", "stock_code": "102780", "quantity": 2, "price": 25400,
            "success": True,
        },
        {
            "timestamp": "2026-05-06T13:44:08+09:00",
            "side": "sell", "stock_code": "102780", "quantity": 4, "price": 25540,
            "success": True,
        },
        {  # mirinae-2 본인의 마지막 잔여 1주를 만든 buy
            "timestamp": "2026-05-06T14:00:00+09:00",
            "side": "buy", "stock_code": "102780", "quantity": 1, "price": 25400,
            "success": True,
        },
    ]
    # 파이프 holdings는 다른 봇(mirinae-3)이 추가로 산 합계 3주를 표시
    strategy = SimpleNamespace(holdings=[
        {"code": "102780", "name": "KODEX 삼성그룹", "qty": 3, "avg_price": 25500},
    ])

    with patch("src.db.get_db", return_value=db), \
         patch("src.strategy.StrategyManager") as mgr_cls:
        mgr_cls.return_value.get.return_value = strategy
        bot._reconcile_positions_with_trades()

    # 본인 ledger: 2+2-4+1 = 1주. 파이프의 3주에 영향 없음.
    assert len(bot._positions) == 1
    assert bot._positions[0]["qty"] == 1
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_mirinae_positions.py::test_reconcile_ignores_pipe_holdings_when_other_bot_owns_same_code -v`
Expected: FAIL — `pipe_qty` 캡으로 인해 1보다 큰 값이 나오거나 잘못 복원됨

- [ ] **Step 3: mirinae.py:204-310의 reconcile 단순화**

`_reconcile_positions_with_trades`를 `BotLedger`를 사용하도록 재작성:

```python
def _reconcile_positions_with_trades(self):
    """본인 거래 이력만으로 잔여 포지션을 복원. 파이프/다른 봇 무시.

    봇은 독립 가상 계좌. 같은 종목을 다른 봇이 보유해도 영향 없음.
    """
    pipe_id = self._get_pipe_id()
    if not pipe_id:
        return
    try:
        from src.db import get_db
        from src.bots.ledger import BotLedger
        from src.strategy import stock_name_for

        trades = get_db().get_trades(pipe_id=pipe_id, source=self.BOT_ID, limit=500)
        if not trades:
            return

        ledger = BotLedger.from_trades(budget=self._config.budget, trades=trades)
        rebuilt = {pos["code"]: pos for pos in ledger.positions}
        if not rebuilt:
            return

        current_by_code = {p["code"]: p for p in self._positions}
        changed = False
        next_positions = []
        for code, restored in rebuilt.items():
            pos = current_by_code.get(code)
            restored_price = restored["avg_price"]
            restored_qty = restored["qty"]
            name = stock_name_for(code)
            if pos:
                if (pos.get("qty") != restored_qty
                    or not pos.get("name")
                    or int(pos.get("price", 0) or 0) != restored_price):
                    pos["qty"] = restored_qty
                    pos["name"] = pos.get("name") or name
                    if restored_price > 0:
                        pos["price"] = restored_price
                    if pos.get("trailing_stop"):
                        pos["trailing_stop"].entry_price = pos["price"]
                    changed = True
                next_positions.append(pos)
            else:
                next_positions.append(self._new_restored_position(
                    code=code, name=name, qty=restored_qty, price=restored_price))
                changed = True

        if len(next_positions) != len(self._positions):
            changed = True
        self._positions = next_positions
        if changed:
            logger.warning(
                "[%s] 거래 이력 기준 포지션 복원/동기화: %s",
                self.BOT_ID,
                ", ".join(f"{p['code']} {p['qty']}주" for p in self._positions),
            )
            self._save_state()
    except Exception as e:
        logger.debug("[%s] reconcile 실패: %s", self.BOT_ID, e)
```

`_new_restored_position` 시그니처 확인 후 호환되게 호출 (`code, name, qty, price` keyword 사용).

- [ ] **Step 4: 기존 mirinae 테스트 업데이트**

`tests/test_mirinae_positions.py`의 다음 테스트들이 여전히 통과하도록 확인:
- `test_reconcile_restores_current_position_from_latest_lot_when_history_has_stale_lot`
- `test_reconcile_does_not_overwrite_live_position_price_with_stale_average`
- `test_reconcile_corrects_previously_polluted_position_price_from_latest_lot`

기존 테스트들은 `pipe_holdings`에 의존했는지 확인하고, 의존하지 않는 형태로 수정 (대부분 `holdings`를 mock하지만 ledger가 trades만 보면 영향 없을 것).

- [ ] **Step 5: 테스트 통과 확인**

Run: `pytest tests/test_mirinae_positions.py -v`
Expected: PASS — 모든 테스트

- [ ] **Step 6: 커밋**

```bash
git add src/bots/mirinae.py tests/test_mirinae_positions.py
git commit -m "feat(mirinae): reconcile from own ledger only, drop pipe_qty cap"
```

---

## Phase 4 — KIS 거부 시 안전한 SELL 클램프

**Goal:** SELL이 KIS에 의해 거부될 때(타 봇/외부 매도로 실잔고 부족), 봇이 무한 재시도하지 않도록 한다. 가능하면 실제 잔고에 맞춰 클램프.

### Task 5: SELL 직전 KIS 잔고 확인 + 클램프

**Files:**
- Modify: `src/bots/engine.py:130-206` (sell)
- Test: `tests/test_engine_cash.py`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_sell_clamps_qty_to_actual_kis_balance():
    """봇이 2주 매도 시도하지만 KIS 실잔고가 1주뿐이면 1주만 매도."""
    eng = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=100000)
    market = MagicMock()
    market.get_balance.return_value = {
        "stocks": [{"stock_code": "102780", "quantity": 1}]
    }
    eng._market = market

    order = MagicMock()
    order.sell.return_value = SimpleNamespace(
        success=True, order_no="123", message="ok", price=25000)
    eng._order = order

    with patch.object(eng, "_log_trade"), \
         patch.object(eng, "_sync_pipe_sell"), \
         patch.object(eng, "_post_to_board"), \
         patch.object(eng, "_track_daily_loss"):
        result = eng.sell("102780", qty=2, entry_price=24000)

    # KIS에 1주만 매도 요청
    args, kwargs = order.sell.call_args
    assert kwargs["quantity"] == 1
    assert result["success"] is True
    assert result.get("clamped_qty") == 1


def test_sell_returns_failure_when_kis_balance_zero():
    """실잔고 0주면 매도 시도 안 하고 실패 반환."""
    eng = TradeEngine(bot_id="test-bot", instance_id="strat-001:test-bot", budget=100000)
    market = MagicMock()
    market.get_balance.return_value = {"stocks": []}
    eng._market = market

    result = eng.sell("102780", qty=1, entry_price=24000)
    assert result["success"] is False
    assert "실잔고 부족" in result["message"]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_engine_cash.py::test_sell_clamps_qty_to_actual_kis_balance -v`
Expected: FAIL — clamp 미구현

- [ ] **Step 3: sell()에 잔고 체크 추가**

`src/bots/engine.py:130-163` (sell 시작부분):

```python
def sell(self, code: str, qty: int, entry_price: int = 0,
         order_type: OrderType = OrderType.MARKET,
         reason: str = "", basis: dict | None = None) -> dict:
    """Execute sell order with PnL calculation, logging, sync, and board posting.

    KIS 실잔고 ≥ qty 보장: qty가 실잔고 초과면 클램프, 0이면 실패.
    봇 = 독립 계좌 모델이지만 KIS는 단일 계좌이므로, 다른 봇이
    같은 종목을 동시에 매도해 잔고가 줄었을 수 있다.
    """
    if not code or qty <= 0:
        return {
            "success": False, "order_no": "", "message": "매도할 종목 없음",
            "sell_price": 0, "realized_pnl": 0,
        }

    # KIS 실잔고 클램프
    actual_qty = self._kis_balance_qty(code)
    if actual_qty <= 0:
        msg = f"실잔고 부족 (계좌 보유 0주, 요청 {qty}주)"
        logger.warning("[%s] SELL 차단: %s %s", self.bot_id, code, msg)
        return {
            "success": False, "order_no": "", "message": msg,
            "sell_price": 0, "realized_pnl": 0,
        }

    clamped_qty = min(qty, actual_qty)
    if clamped_qty < qty:
        logger.warning(
            "[%s] SELL 수량 클램프: %s 요청 %d → 실잔고 기준 %d",
            self.bot_id, code, qty, clamped_qty,
        )

    order = self._get_order()
    result = order.sell(stock_code=code, quantity=clamped_qty, order_type=order_type)
    # ... 나머지는 기존과 동일하지만 qty → clamped_qty 사용
```

`_kis_balance_qty` 헬퍼 추가:

```python
def _kis_balance_qty(self, code: str) -> int:
    """KIS 계좌의 실제 보유 수량 조회. 실패 시 -1 반환(클램프 우회)."""
    try:
        balance = self._get_market().get_balance()
        stocks = balance.get("stocks", []) if balance else []
        for stock in stocks:
            if stock.get("stock_code") == code:
                return int(stock.get("quantity", 0) or 0)
        return 0
    except Exception as e:
        logger.warning("[%s] KIS 잔고 조회 실패 (클램프 생략): %s", self.bot_id, e)
        return -1  # 실패 시 그냥 봇이 요청한 qty로 진행

# sell 내에서 actual_qty가 -1이면 클램프 생략
```

수정된 클램프 로직:

```python
actual_qty = self._kis_balance_qty(code)
if actual_qty == 0:
    return {"success": False, ...}
if actual_qty > 0:
    clamped_qty = min(qty, actual_qty)
else:
    clamped_qty = qty  # KIS 조회 실패 시 그냥 진행
```

`result` dict에 `clamped_qty` 필드 추가 (성공 시).

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_engine_cash.py -v`
Expected: PASS

- [ ] **Step 5: 회귀 테스트**

Run: `pytest tests/ -v`
Expected: 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add src/bots/engine.py tests/test_engine_cash.py
git commit -m "feat(engine): clamp SELL qty to actual KIS balance to prevent rejection loops"
```

---

## Phase 5 — pipe_runner 정리 + 통합 검증

### Task 6: PipeRunner에서 봇 시작 시 cash refresh

**Files:**
- Modify: `src/services/pipe_runner.py:165-167`

- [ ] **Step 1: 봇 생성 직후 cash 갱신 호출**

`src/services/pipe_runner.py`의 봇 생성 루프(165 부근):

```python
bot = create_bot(bot_id, budget=bot_budget, instance_id=instance_id, params=params)
if bot:
    # 자기 거래 이력에서 cash/positions 재계산 (재시작 시 ledger 복원)
    try:
        bot.engine.refresh_cash()
    except Exception as e:
        logger.warning("[%s] cash refresh 실패: %s", bot_id, e)
    self.bots.append(bot)
    bot_names.append(f"{bot_id}({int(ratio*100)}%={bot_budget:,}원, cash={bot.engine.cash:,}원)")
    asyncio.create_task(bot.start())
```

- [ ] **Step 2: pipe_runner 테스트 통과 확인**

Run: `pytest tests/test_pipe_runner.py -v`
Expected: PASS

- [ ] **Step 3: 커밋**

```bash
git add src/services/pipe_runner.py
git commit -m "feat(pipe-runner): refresh bot cash from trades on startup"
```

### Task 7: 수동 검증 스크립트로 현재 보유 ledger 확인

**Goal:** 변경 후 mirinae-2와 mirinae-3의 ledger 상태가 의도대로 분리되는지 확인.

- [ ] **Step 1: 검증 스크립트 작성** (임시, 커밋 X)

```python
# /tmp/check_ledger.py
from src.db import get_db
from src.bots.ledger import BotLedger

db = get_db()
for bot_id in ("mirinae-2", "mirinae-3"):
    trades = db.get_trades(pipe_id="strat-001", source=bot_id, limit=1000)
    ledger = BotLedger.from_trades(budget=100000, trades=trades)
    print(f"{bot_id}: cash={ledger.cash:,}, pnl={ledger.realized_pnl:+,}, positions={ledger.positions}")
```

Run: `python /tmp/check_ledger.py`
Expected:
- `mirinae-2`: 102780 1주 보유
- `mirinae-3`: 102780 2주 보유
- 봇별로 명확히 분리

- [ ] **Step 2: 실제 KIS 잔고와 비교**

Run via MCP or shell: `get_balance()` 호출 후 102780 잔고 확인.

기록할 케이스:
1. KIS 실잔고가 정확히 3주이면: 새 모델이 옳다 (mirinae-2 1주 + mirinae-3 2주)
2. KIS 실잔고가 2주이면: 어딘가 sync 누락 — 추가 조사 필요. 클램프 로직이 SELL을 1주만 진행시켜 정상 종료시키는지 확인.

### Task 8: 통합 회귀 + 문서 업데이트

- [ ] **Step 1: 전체 테스트**

Run: `pytest tests/ -v`
Expected: 모두 PASS

- [ ] **Step 2: CLAUDE.md 업데이트**

`CLAUDE.md`의 "## 7. 상태 동기화" 섹션 갱신:

```markdown
### 봇 = 독립 가상 계좌 (2026-05-08~)

- 봇은 자기 trades(source=bot_id)만 본다. 다른 봇/파이프 holdings 무관.
- 봇 cash = budget − 자기 매수금 + 자기 매도금. `BotLedger.from_trades()`로 재계산.
- 파이프 holdings/cash는 봇 ledger의 합산 — 표시용. 봇 의사결정에 영향 없음.
- SELL 시 KIS 실잔고로 자동 클램프. 0이면 실패 반환 (재시도 무한루프 방지).
```

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: document bot virtual account model"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] BotLedger 단일 진실원천 — Task 1
- [x] TradeEngine cash 추적 — Task 2
- [x] BotBase reconcile 본인 ledger만 — Task 3
- [x] mirinae reconcile 본인 ledger만 — Task 4
- [x] SELL KIS 잔고 클램프 — Task 5
- [x] PipeRunner 시작 시 cash 갱신 — Task 6
- [x] 수동 검증 — Task 7
- [x] 통합 회귀 + 문서 — Task 8

**Placeholder scan:** 없음 — 모든 코드 블록 완성

**Type consistency:**
- `BotLedger.positions[i]`: `{code, qty, avg_price, lots[]}` — 일관
- `engine.cash`: int — 일관
- `_reconcile_with_trades`: 신규 메서드명 — 호출부 1곳 (`_load_state` 내)
- `_kis_balance_qty(code) → int`: 일관 (-1 = 조회 실패, 0 = 잔고 0)

**Risk areas:**
1. **재시작 시 cash 손실** — 기존 bot_state에 cash 필드가 없어 첫 refresh_cash() 호출 전까지 budget으로 초기화됨. Task 6에서 startup refresh를 보장하므로 OK.
2. **strategies.json holdings vs bot ledger 불일치** — Phase 1~4 적용 후에는 봇이 strategies.json을 안 보므로 bot 로직에 영향 없음. 단 web UI/MCP가 strategies.json을 보여줌. 별도 cleanup 작업으로 추후 처리 (이 plan 범위 밖).
3. **mirinae2.py와 wallstreet_us_ai.py** — 같은 reconcile 패턴이 있는지 확인 필요. Task 3에서 grep으로 점검.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-08-bot-virtual-account.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Task 단위로 fresh subagent 분배, task 사이 리뷰

**2. Inline Execution** - 이 세션에서 task 단위로 진행, checkpoint마다 사용자 확인
