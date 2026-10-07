# Decision Audit DB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every bot decision as a replayable audit run that links queried evidence, AI prompt/response, preflight checks, execution, trades, and outcomes.

**Architecture:** Add an audit layer beside the existing `trades` and `decisions` tables. `run_id` is the top-level key for one bot scan, and a small `AuditRecorder` isolates bots from SQL details. Wire the shared `MirinaeBot._loop()` first so Mirinae 2, 3, and 4 inherit the same audit trail.

**Tech Stack:** Python 3.14, SQLite via `sqlite3`, pytest, existing FF `DB`, `TradeEngine`, `MirinaeBot`, and MCP server patterns.

---

## File Structure

- Modify: `src/db.py`
  - Create audit tables and helper methods.
  - Keep all migrations idempotent.
- Create: `src/bots/audit.py`
  - Provide `AuditRecorder` and `make_run_id`.
  - Catch audit failures without breaking trading.
- Modify: `src/bots/ai_caller.py`
  - Add `AIResult` and `invoke_ai_result`.
  - Keep existing `invoke_ai` API as a compatibility wrapper.
- Modify: `src/bots/mirinae.py`
  - Start an audit run per scan.
  - Store collected evidence, AI prompt/response, parsed decision, preflight context, and execution result.
  - Link `decision_id` and most recent `trade_id`.
- Modify: `src/bots/engine.py`
  - Accept optional `run_id` and `decision_id` metadata.
  - Return `trade_id` from buy/sell results.
- Modify: `src/mcp_server.py`
  - Add a read-only audit KPI/reporting tool.
- Test: `tests/test_decision_audit_db.py`
- Test: `tests/test_audit_recorder.py`
- Test: `tests/test_ai_caller_result.py`
- Test: `tests/test_mirinae_audit_flow.py`
- Test: `tests/test_mcp_audit_report.py`

---

### Task 1: DB Audit Tables And Helpers

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_decision_audit_db.py`

- [ ] **Step 1: Write failing tests for migration and core helpers**

Create `tests/test_decision_audit_db.py`:

```python
import json

from src.db import DB


def test_decision_audit_tables_are_created_idempotently(tmp_path):
    path = tmp_path / "ff.db"
    db = DB(path)
    DB(path).close()

    tables = {
        r["name"]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    assert "decision_runs" in tables
    assert "evidence_snapshots" in tables
    assert "ai_audit_logs" in tables
    assert "preflight_checks" in tables
    assert "decision_outcomes" in tables
    db.close()


def test_decision_run_lifecycle_and_evidence(tmp_path):
    db = DB(tmp_path / "ff.db")

    run_id = "mirinae-4:strat-001:20260603T132501:184:9f3a2c"
    row_id = db.start_decision_run(
        run_id=run_id,
        pipe_id="strat-001",
        bot_id="mirinae-4",
        scan_count=184,
        mode="paper",
    )

    assert row_id > 0

    db.log_evidence_snapshot(
        run_id=run_id,
        source="price",
        stock_code="102780",
        payload={"price": 33690, "change_rate": 1.2},
        summary="102780 33,690원 +1.2%",
        success=True,
    )
    db.update_decision_run(
        run_id,
        status="decided",
        action="buy",
        stock_code="102780",
    )

    run = db.get_decision_run(run_id)
    evidence = db.get_evidence_snapshots(run_id)

    assert run["status"] == "decided"
    assert run["action"] == "buy"
    assert evidence[0]["source"] == "price"
    assert json.loads(evidence[0]["payload_json"])["price"] == 33690
    db.close()


def test_ai_audit_preflight_and_outcome_helpers(tmp_path):
    db = DB(tmp_path / "ff.db")
    run_id = "mirinae-4:strat-001:20260603T132501:184:9f3a2c"
    db.start_decision_run(run_id, pipe_id="strat-001", bot_id="mirinae-4")

    ai_id = db.log_ai_audit(
        run_id=run_id,
        bot_id="mirinae-4",
        provider="codex",
        model="gpt-5",
        prompt="prompt text",
        raw_response='{"decision":"BUY","confidence":0.82}',
        parsed={"decision": "BUY", "confidence": 0.82},
        parse_success=True,
        confidence=0.82,
        criteria={"risk_reward": {"score": 8}},
        bull_summary="강세",
        bear_summary="약세",
        trader_reason="최종",
        tokens=1234,
        latency_ms=2500,
    )
    check_id = db.log_preflight_check(
        run_id=run_id,
        check_name="daily_loss_limit",
        stock_code="102780",
        passed=False,
        severity="block",
        message="일일 손실 한도 도달",
        payload={"limit": -2000, "pnl": -2500},
    )
    outcome_id = db.upsert_decision_outcome(
        run_id=run_id,
        decision_id=11,
        buy_trade_id=22,
        bot_id="mirinae-4",
        stock_code="102780",
        entry_price=33000,
        quantity=1,
        outcome_status="open",
    )

    assert ai_id > 0
    assert check_id > 0
    assert outcome_id > 0
    assert db.get_preflight_checks(run_id)[0]["severity"] == "block"
    assert db.get_ai_audit(run_id)["parse_success"] == 1
    db.close()
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_decision_audit_db.py -q
```

Expected: FAIL with missing methods such as `start_decision_run`.

- [ ] **Step 3: Add table migrations**

In `src/db.py`, call a new migration at the end of `_create_tables()`:

```python
self._migrate_decision_audit()
```

Add this method to `DB`:

```python
def _migrate_decision_audit(self):
    self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS decision_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            timestamp TEXT NOT NULL,
            pipe_id TEXT DEFAULT '',
            bot_id TEXT DEFAULT '',
            scan_count INTEGER DEFAULT 0,
            mode TEXT DEFAULT '',
            status TEXT DEFAULT 'started',
            action TEXT DEFAULT '',
            stock_code TEXT DEFAULT '',
            decision_id INTEGER DEFAULT 0,
            trade_id INTEGER DEFAULT 0,
            error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_decision_runs_run ON decision_runs(run_id);
        CREATE INDEX IF NOT EXISTS idx_decision_runs_bot_ts ON decision_runs(bot_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_decision_runs_code_ts ON decision_runs(stock_code, timestamp);

        CREATE TABLE IF NOT EXISTS evidence_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            stock_code TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            summary TEXT DEFAULT '',
            success INTEGER DEFAULT 1,
            error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence_snapshots(run_id);
        CREATE INDEX IF NOT EXISTS idx_evidence_source_ts ON evidence_snapshots(source, timestamp);

        CREATE TABLE IF NOT EXISTS ai_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            bot_id TEXT DEFAULT '',
            provider TEXT DEFAULT '',
            model TEXT DEFAULT '',
            prompt TEXT DEFAULT '',
            raw_response TEXT DEFAULT '',
            parsed_json TEXT DEFAULT '{}',
            parse_success INTEGER DEFAULT 0,
            parse_error TEXT DEFAULT '',
            confidence REAL DEFAULT 0,
            criteria_json TEXT DEFAULT '{}',
            bull_summary TEXT DEFAULT '',
            bear_summary TEXT DEFAULT '',
            trader_reason TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ai_audit_run ON ai_audit_logs(run_id);
        CREATE INDEX IF NOT EXISTS idx_ai_audit_bot_ts ON ai_audit_logs(bot_id, timestamp);

        CREATE TABLE IF NOT EXISTS preflight_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            check_name TEXT NOT NULL,
            stock_code TEXT DEFAULT '',
            passed INTEGER DEFAULT 1,
            severity TEXT DEFAULT 'info',
            message TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_preflight_run ON preflight_checks(run_id);
        CREATE INDEX IF NOT EXISTS idx_preflight_check ON preflight_checks(check_name);

        CREATE TABLE IF NOT EXISTS decision_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            decision_id INTEGER DEFAULT 0,
            buy_trade_id INTEGER DEFAULT 0,
            sell_trade_id INTEGER DEFAULT 0,
            bot_id TEXT DEFAULT '',
            stock_code TEXT DEFAULT '',
            entry_time TEXT DEFAULT '',
            exit_time TEXT DEFAULT '',
            hold_minutes REAL DEFAULT 0,
            entry_price INTEGER DEFAULT 0,
            exit_price INTEGER DEFAULT 0,
            quantity INTEGER DEFAULT 0,
            realized_pnl INTEGER DEFAULT 0,
            return_pct REAL DEFAULT 0,
            max_favorable_pct REAL DEFAULT 0,
            max_adverse_pct REAL DEFAULT 0,
            outcome_status TEXT DEFAULT 'open',
            notes TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_outcomes_bot_ts ON decision_outcomes(bot_id, entry_time);
        CREATE INDEX IF NOT EXISTS idx_outcomes_code ON decision_outcomes(stock_code);
    """)
    self._conn.commit()
```

- [ ] **Step 4: Add helper methods**

Add these methods to `DB`:

```python
def _json_dumps(self, value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)

def start_decision_run(self, run_id: str, pipe_id: str = "", bot_id: str = "",
                       scan_count: int = 0, mode: str = "",
                       status: str = "started") -> int:
    cur = self._conn.execute(
        """INSERT OR IGNORE INTO decision_runs
           (run_id, timestamp, pipe_id, bot_id, scan_count, mode, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_kst_now().isoformat(), run_id, pipe_id, bot_id, scan_count, mode, status),
    )
    self._conn.commit()
    if cur.lastrowid:
        return cur.lastrowid
    row = self._conn.execute(
        "SELECT id FROM decision_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    return int(row["id"]) if row else 0

def update_decision_run(self, run_id: str, **fields):
    allowed = {
        "status", "action", "stock_code", "decision_id", "trade_id", "error", "mode"
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k}=?" for k in updates)
    self._conn.execute(
        f"UPDATE decision_runs SET {assignments} WHERE run_id=?",
        [*updates.values(), run_id],
    )
    self._conn.commit()

def get_decision_run(self, run_id: str) -> dict:
    row = self._conn.execute(
        "SELECT * FROM decision_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    return dict(row) if row else {}

def log_evidence_snapshot(self, run_id: str, source: str, stock_code: str = "",
                          payload: dict | list | None = None, summary: str = "",
                          success: bool = True, error: str = "") -> int:
    cur = self._conn.execute(
        """INSERT INTO evidence_snapshots
           (run_id, timestamp, source, stock_code, payload_json, summary, success, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (_kst_now().isoformat(), run_id, source, stock_code,
         self._json_dumps(payload), summary, 1 if success else 0, error),
    )
    self._conn.commit()
    return cur.lastrowid

def get_evidence_snapshots(self, run_id: str) -> list[dict]:
    return [
        dict(r) for r in self._conn.execute(
            "SELECT * FROM evidence_snapshots WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    ]
```

Also add `log_ai_audit`, `get_ai_audit`, `log_preflight_check`, `get_preflight_checks`, and `upsert_decision_outcome` with the columns used in Step 1. Use `INSERT ... ON CONFLICT(run_id) DO UPDATE` for `decision_outcomes`.

- [ ] **Step 5: Run DB tests**

Run:

```bash
pytest tests/test_decision_audit_db.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit DB helpers**

Run:

```bash
git add src/db.py tests/test_decision_audit_db.py
git commit -m "feat: add decision audit db tables"
```

---

### Task 2: Audit Recorder Wrapper

**Files:**
- Create: `src/bots/audit.py`
- Test: `tests/test_audit_recorder.py`

- [ ] **Step 1: Write failing recorder tests**

Create `tests/test_audit_recorder.py`:

```python
import json

from src.bots.audit import AuditRecorder, make_run_id
from src.db import DB


def test_make_run_id_contains_bot_pipe_scan_and_unique_suffix():
    run_id = make_run_id("mirinae-4", "strat-001", 184)

    assert run_id.startswith("mirinae-4:strat-001:")
    assert ":184:" in run_id
    assert len(run_id.split(":")[-1]) == 6


def test_recorder_logs_run_evidence_ai_and_preflight(tmp_path):
    db = DB(tmp_path / "ff.db")
    recorder = AuditRecorder(
        db=db,
        bot_id="mirinae-4",
        pipe_id="strat-001",
        scan_count=184,
        mode="paper",
    )

    recorder.start()
    recorder.evidence("price", {"price": 33690}, stock_code="102780", summary="price ok")
    recorder.ai_call(
        provider="codex",
        model="gpt-5",
        prompt="prompt",
        raw_response='{"decision":"BUY","confidence":0.8}',
        parsed={"decision": "BUY", "confidence": 0.8, "criteria": {"risk": {"score": 8}}},
        parse_success=True,
        tokens=12,
        latency_ms=50,
    )
    recorder.preflight("cash", True, "info", "cash ok", {"cash": 100000}, "102780")
    recorder.finish(status="decided", action="buy", stock_code="102780")

    assert db.get_decision_run(recorder.run_id)["status"] == "decided"
    assert json.loads(db.get_evidence_snapshots(recorder.run_id)[0]["payload_json"])["price"] == 33690
    assert db.get_ai_audit(recorder.run_id)["confidence"] == 0.8
    assert db.get_preflight_checks(recorder.run_id)[0]["check_name"] == "cash"
    db.close()


def test_recorder_swallows_db_errors_without_raising():
    class BrokenDB:
        def start_decision_run(self, *args, **kwargs):
            raise RuntimeError("db down")

    recorder = AuditRecorder(BrokenDB(), "mirinae-4", "strat-001", 1, "paper")

    recorder.start()
    recorder.finish(status="failed", error="x")
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest tests/test_audit_recorder.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'src.bots.audit'`.

- [ ] **Step 3: Implement recorder**

Create `src/bots/audit.py`:

```python
"""Decision audit helpers for bot scans."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def make_run_id(bot_id: str, pipe_id: str, scan_count: int) -> str:
    ts = datetime.now(_KST).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    pipe = pipe_id or "no-pipe"
    return f"{bot_id}:{pipe}:{ts}:{int(scan_count or 0)}:{suffix}"


class AuditRecorder:
    def __init__(self, db, bot_id: str, pipe_id: str = "",
                 scan_count: int = 0, mode: str = ""):
        self.db = db
        self.bot_id = bot_id
        self.pipe_id = pipe_id
        self.scan_count = int(scan_count or 0)
        self.mode = mode or ""
        self.run_id = make_run_id(bot_id, pipe_id, self.scan_count)

    def _safe(self, label: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.warning("[%s] audit %s failed: %s", self.bot_id, label, exc)
            return 0

    def start(self):
        return self._safe(
            "start",
            self.db.start_decision_run,
            self.run_id,
            pipe_id=self.pipe_id,
            bot_id=self.bot_id,
            scan_count=self.scan_count,
            mode=self.mode,
        )

    def evidence(self, source: str, payload, stock_code: str = "",
                 summary: str = "", success: bool = True, error: str = ""):
        return self._safe(
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

    def ai_call(self, provider: str, model: str, prompt: str, raw_response: str,
                parsed: dict | None, parse_success: bool, parse_error: str = "",
                tokens: int = 0, latency_ms: int = 0):
        parsed = parsed or {}
        criteria = parsed.get("criteria") if isinstance(parsed, dict) else {}
        return self._safe(
            "ai_call",
            self.db.log_ai_audit,
            run_id=self.run_id,
            bot_id=self.bot_id,
            provider=provider,
            model=model,
            prompt=prompt,
            raw_response=raw_response,
            parsed=parsed,
            parse_success=parse_success,
            parse_error=parse_error,
            confidence=float(parsed.get("confidence", 0) or 0),
            criteria=criteria or {},
            bull_summary=str(parsed.get("bull_summary", "") or ""),
            bear_summary=str(parsed.get("bear_summary", "") or ""),
            trader_reason=str(parsed.get("trader_reason", "") or ""),
            tokens=int(tokens or 0),
            latency_ms=int(latency_ms or 0),
        )

    def preflight(self, check_name: str, passed: bool, severity: str,
                  message: str, payload: dict | None = None, stock_code: str = ""):
        return self._safe(
            "preflight",
            self.db.log_preflight_check,
            self.run_id,
            check_name,
            stock_code=stock_code,
            passed=passed,
            severity=severity,
            message=message,
            payload=payload or {},
        )

    def finish(self, status: str, action: str = "", stock_code: str = "",
               decision_id: int = 0, trade_id: int = 0, error: str = ""):
        return self._safe(
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
```

- [ ] **Step 4: Run recorder tests**

Run:

```bash
pytest tests/test_audit_recorder.py tests/test_decision_audit_db.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit recorder**

Run:

```bash
git add src/bots/audit.py tests/test_audit_recorder.py
git commit -m "feat: add bot audit recorder"
```

---

### Task 3: AI Result Metadata

**Files:**
- Modify: `src/bots/ai_caller.py`
- Test: `tests/test_ai_caller_result.py`

- [ ] **Step 1: Write failing AI metadata test**

Create `tests/test_ai_caller_result.py`:

```python
from types import SimpleNamespace
from unittest.mock import patch

from src.bots.ai_caller import invoke_ai_result


def test_invoke_ai_result_returns_content_and_metadata():
    fake_response = SimpleNamespace(
        content="hello",
        provider="codex",
        model="gpt-5",
        error="",
        error_type="",
        tokens=SimpleNamespace(total_tokens=321),
    )

    with patch("src.bots.ai_caller._get_llm_client") as get_client:
        get_client.return_value.chat.return_value = fake_response
        result = invoke_ai_result(
            "prompt",
            {"ai_provider": "codex", "ai_model": "gpt-5"},
            timeout=3,
            bot_id="mirinae-4",
        )

    assert result.content == "hello"
    assert result.provider == "codex"
    assert result.model == "gpt-5"
    assert result.tokens == 321
    assert result.latency_ms >= 0
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest tests/test_ai_caller_result.py -q
```

Expected: FAIL with missing `invoke_ai_result`.

- [ ] **Step 3: Implement `AIResult` and wrapper**

In `src/bots/ai_caller.py`, add imports:

```python
import time
from dataclasses import dataclass
```

Add after `_resolve_params`:

```python
@dataclass
class AIResult:
    content: str
    provider: str
    model: str
    tokens: int
    latency_ms: int
    error: str = ""
    error_type: str = ""
```

Add:

```python
def invoke_ai_result(prompt: str, params: dict, *, timeout: int = 180,
                     bot_id: str = "", conversation_id: str = "",
                     alias: str = "", cwd: str | None = None) -> AIResult:
    provider, model, conversation_id = _resolve_params(
        params, bot_id, conversation_id, alias)
    fallback = _bool_param(params, "ai_fallback_enabled", True)
    client = _get_llm_client()
    started = time.monotonic()
    response = client.chat(
        prompt,
        provider=provider, model=model,
        owner=bot_id or "unknown",
        conversation_id=conversation_id, alias=alias,
        timeout=timeout, cwd=cwd,
        fallback=fallback,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    tokens = int(getattr(response.tokens, "total_tokens", 0) or 0)
    if tokens > 0:
        _log_token_usage(
            bot_id or "unknown", response.provider, response.model,
            tokens, len(prompt), len(response.content))
    if not response.content:
        logger.warning(
            "[AI] 응답 없음 bot=%s provider=%s fallback=%s error=%s",
            bot_id or "unknown", response.provider or provider, fallback,
            response.error or response.error_type or "no-content",
        )
    return AIResult(
        content=response.content or "",
        provider=response.provider or provider,
        model=response.model or model,
        tokens=tokens,
        latency_ms=latency_ms,
        error=response.error or "",
        error_type=response.error_type or "",
    )
```

Replace `invoke_ai` body with:

```python
def invoke_ai(prompt: str, params: dict, *, timeout: int = 180, bot_id: str = "",
              conversation_id: str = "", alias: str = "",
              cwd: str | None = None) -> str:
    result = invoke_ai_result(
        prompt,
        params,
        timeout=timeout,
        bot_id=bot_id,
        conversation_id=conversation_id,
        alias=alias,
        cwd=cwd,
    )
    return result.content
```

- [ ] **Step 4: Run AI caller tests**

Run:

```bash
pytest tests/test_ai_caller_result.py -q
```

Expected: PASS.

- [ ] **Step 5: Run compatibility tests**

Run:

```bash
pytest tests/test_ai_caller_bridge.py tests/test_ai_models_endpoint.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit AI metadata**

Run:

```bash
git add src/bots/ai_caller.py tests/test_ai_caller_result.py
git commit -m "feat: return ai call metadata"
```

---

### Task 4: TradeEngine Trade ID Linking

**Files:**
- Modify: `src/bots/engine.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_engine_cash.py`

- [ ] **Step 1: Write failing test for returned trade id**

Append to `tests/test_engine.py`:

```python
from unittest.mock import MagicMock, patch

from src.bots.engine import TradeEngine
from src.kis.order import OrderResult, OrderType


def test_trade_engine_sell_returns_trade_id_from_db():
    eng = TradeEngine(bot_id="mirinae-4", instance_id="strat-001:mirinae-4", budget=100000)
    eng._order = MagicMock()
    eng._order.sell.return_value = OrderResult(
        success=True,
        order_no="ORD-1",
        message="ok",
        stock_code="102780",
        side="sell",
        quantity=1,
        price=33000,
    )
    eng._market = MagicMock()
    eng._market.get_balance.return_value = {"holdings": [{"stock_code": "102780", "quantity": 1}]}

    with patch.object(eng, "_own_ledger_qty", return_value=1), \
         patch.object(eng, "_sync_pipe_sell"), \
         patch.object(eng, "_post_to_board"), \
         patch("src.db.get_db") as get_db:
        get_db.return_value.log_trade.return_value = 77
        result = eng.sell("102780", 1, entry_price=32000, order_type=OrderType.MARKET)

    assert result["success"] is True
    assert result["trade_id"] == 77
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest tests/test_engine.py::test_trade_engine_sell_returns_trade_id_from_db -q
```

Expected: FAIL because `trade_id` is missing.

- [ ] **Step 3: Return trade id from `_log_trade`, buy, and sell**

In `TradeEngine.buy`, after `_log_trade`:

```python
trade_id = self._log_trade("buy", code, qty, price, result.order_no)
```

Return:

```python
"trade_id": trade_id,
```

In `TradeEngine.sell`, keep:

```python
trade_id = self._log_trade("sell", code, clamped_qty, sell_price, result.order_no, realized, trade_reason)
```

Return:

```python
"trade_id": trade_id,
```

- [ ] **Step 4: Run engine tests**

Run:

```bash
pytest tests/test_engine.py tests/test_engine_cash.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit trade id linking**

Run:

```bash
git add src/bots/engine.py tests/test_engine.py
git commit -m "feat: return trade ids from trade engine"
```

---

### Task 5: Wire Audit Into Shared Mirinae Flow

**Files:**
- Modify: `src/bots/mirinae.py`
- Test: `tests/test_mirinae_audit_flow.py`

- [ ] **Step 1: Write focused tests for Mirinae audit helper methods**

Create `tests/test_mirinae_audit_flow.py`:

```python
from unittest.mock import MagicMock

from src.bots.audit import AuditRecorder
from src.bots.mirinae import MirinaeBot
from src.db import DB


def test_mirinae_records_ai_audit_from_prompt_response_and_decision(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = MirinaeBot()
    recorder = AuditRecorder(db, "mirinae", "strat-001", 10, "paper")
    recorder.start()

    decision = {
        "decision": "BUY",
        "confidence": 0.81,
        "code": "102780",
        "criteria": {"market_trend": {"score": 8}},
        "bull_summary": "강세",
        "bear_summary": "약세",
        "trader_reason": "최종 판단",
    }
    bot._audit_record_ai(
        recorder,
        prompt="prompt text",
        output='{"decision":"BUY","confidence":0.81}',
        ai_decision=decision,
        provider="codex",
        model="gpt-5",
        tokens=100,
        latency_ms=20,
    )

    row = db.get_ai_audit(recorder.run_id)
    assert row["prompt"] == "prompt text"
    assert row["raw_response"].startswith("{")
    assert row["confidence"] == 0.81
    db.close()


def test_mirinae_records_preflight_and_finish(tmp_path):
    db = DB(tmp_path / "ff.db")
    bot = MirinaeBot()
    recorder = AuditRecorder(db, "mirinae", "strat-001", 10, "paper")
    recorder.start()

    bot._audit_record_preflight(
        recorder,
        {"reason": "실제 주문 가능 후보 없음", "buyable_candidates": []},
        stock_code="102780",
    )
    bot._audit_finish_run(
        recorder,
        action="blocked_buy",
        stock_code="102780",
        decision_id=5,
        result={"success": False, "reason": "매수 가능 수량 없음"},
    )

    run = db.get_decision_run(recorder.run_id)
    checks = db.get_preflight_checks(recorder.run_id)
    assert run["status"] == "blocked"
    assert checks[0]["severity"] == "block"
    db.close()
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest tests/test_mirinae_audit_flow.py -q
```

Expected: FAIL with missing `_audit_record_ai`.

- [ ] **Step 3: Add Mirinae audit helper methods**

In `src/bots/mirinae.py`, add helper methods inside `MirinaeBot`:

```python
def _audit_pipe_id(self) -> str:
    return self._config.instance_id.split(":")[0] if ":" in self._config.instance_id else ""

def _audit_mode(self) -> str:
    try:
        import src.trading_mode as tm
        return str(tm.mode_info().get("mode", ""))
    except Exception:
        return ""

def _audit_start_run(self):
    try:
        from src.db import get_db
        from src.bots.audit import AuditRecorder
        recorder = AuditRecorder(
            get_db(),
            self.BOT_ID,
            self._audit_pipe_id(),
            self._scan_count,
            self._audit_mode(),
        )
        recorder.start()
        return recorder
    except Exception as exc:
        logger.warning("[%s] audit start failed: %s", self.BOT_ID, exc)
        return None

def _audit_record_evidence(self, recorder, context: str):
    if not recorder:
        return
    recorder.evidence(
        "mirinae_context",
        {"context": context},
        summary=f"context chars={len(context or '')}",
        success=bool(context),
    )
    if getattr(self, "_last_preflight", None):
        recorder.evidence(
            "preflight_input",
            self._last_preflight,
            summary=self._format_buy_preflight_summary(self._last_preflight),
            success=True,
        )

def _audit_record_ai(self, recorder, prompt: str, output: str, ai_decision: dict | None,
                     provider: str = "", model: str = "", tokens: int = 0, latency_ms: int = 0):
    if not recorder:
        return
    parsed = ai_decision or {}
    recorder.ai_call(
        provider=provider or self._params.get("ai_provider", ""),
        model=model or self._params.get("ai_model", ""),
        prompt=prompt or "",
        raw_response=output or "",
        parsed=parsed,
        parse_success=bool(ai_decision),
        parse_error="" if ai_decision else (self._last_ai_error or "parse failed"),
        tokens=tokens,
        latency_ms=latency_ms,
    )

def _audit_record_preflight(self, recorder, preflight: dict, stock_code: str = ""):
    if not recorder or not preflight:
        return
    buyable = preflight.get("buyable_candidates") or []
    passed = bool(buyable) or preflight.get("reason") == "OK"
    recorder.preflight(
        "buyable_candidates",
        passed=passed,
        severity="info" if passed else "block",
        message=preflight.get("reason", ""),
        payload=preflight,
        stock_code=stock_code,
    )

def _audit_finish_run(self, recorder, action: str, stock_code: str = "",
                      decision_id: int = 0, result: dict | None = None, error: str = ""):
    if not recorder:
        return
    result = result or {}
    success = bool(result.get("success", True))
    trade_id = int(result.get("trade_id", 0) or 0)
    if error:
        status = "failed"
    elif action.startswith("blocked_") or not success:
        status = "blocked"
    elif trade_id:
        status = "ordered"
    elif action in ("hold", "skip", "adjust_plan"):
        status = "completed"
    else:
        status = "decided"
    recorder.finish(
        status=status,
        action=action,
        stock_code=stock_code,
        decision_id=decision_id,
        trade_id=trade_id,
        error=error or str(result.get("reason") or result.get("message") or ""),
    )
```

- [ ] **Step 4: Use `invoke_ai_result` in `_invoke_codex`**

Change `_invoke_codex` to store metadata:

```python
from src.bots.ai_caller import invoke_ai_result

result = invoke_ai_result(prompt, self._params, timeout=timeout, bot_id=self.BOT_ID)
self._last_ai_call_meta = {
    "provider": result.provider,
    "model": result.model,
    "tokens": result.tokens,
    "latency_ms": result.latency_ms,
}
output = result.content
```

Keep existing error handling by using `result.error or result.error_type`.

- [ ] **Step 5: Wire helpers into `_loop`**

In `_loop`, after `self._reconcile_positions_with_trades()`:

```python
audit = self._audit_start_run()
```

After `context = await loop.run_in_executor(None, self._collect_data)`:

```python
self._audit_record_evidence(audit, context)
```

After `ai_decision = self._parse_decision(output)`:

```python
meta = getattr(self, "_last_ai_call_meta", {}) or {}
self._audit_record_ai(
    audit,
    prompt=prompt if "prompt" in locals() else "",
    output=output,
    ai_decision=ai_decision,
    provider=meta.get("provider", ""),
    model=meta.get("model", ""),
    tokens=meta.get("tokens", 0),
    latency_ms=meta.get("latency_ms", 0),
)
```

After preflight summary is prepared:

```python
self._audit_record_preflight(
    audit,
    self._last_preflight,
    stock_code=ai_decision.get("code", "") if ai_decision else "",
)
```

After `decision_id` and trade linking:

```python
trade_id = 0
if decision_id and action in ("buy", "sell", "partial_sell") and result and result.get("success"):
    trade_id = int(result.get("trade_id", 0) or 0)
    if not trade_id:
        try:
            from src.db import get_db
            recent = get_db().get_trades(source=self.BOT_ID, limit=1)
            if recent:
                trade_id = recent[0]["id"]
                get_db().link_decision_trade(decision_id, trade_id)
        except Exception:
            pass
self._audit_finish_run(
    audit,
    action=action,
    stock_code=ai_decision.get("code", "") if ai_decision else (exit_results[-1]["code"] if exit_results else ""),
    decision_id=decision_id,
    result={**(result or {}), "trade_id": trade_id or int((result or {}).get("trade_id", 0) or 0)},
)
```

- [ ] **Step 6: Run Mirinae audit tests**

Run:

```bash
pytest tests/test_mirinae_audit_flow.py tests/test_decision_audit_db.py tests/test_audit_recorder.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Mirinae audit wiring**

Run:

```bash
git add src/bots/mirinae.py tests/test_mirinae_audit_flow.py
git commit -m "feat: audit mirinae decision runs"
```

---

### Task 6: Outcome Derivation And KPI Query

**Files:**
- Modify: `src/db.py`
- Modify: `src/mcp_server.py`
- Test: `tests/test_mcp_audit_report.py`

- [ ] **Step 1: Write failing tests for KPI helper**

Create `tests/test_mcp_audit_report.py`:

```python
from src.db import DB


def test_bot_audit_kpi_groups_closed_outcomes(tmp_path):
    db = DB(tmp_path / "ff.db")
    db.upsert_decision_outcome(
        run_id="r1",
        bot_id="mirinae-4",
        stock_code="102780",
        entry_price=10000,
        exit_price=10300,
        quantity=1,
        realized_pnl=300,
        return_pct=3.0,
        outcome_status="closed",
    )
    db.upsert_decision_outcome(
        run_id="r2",
        bot_id="mirinae-4",
        stock_code="305720",
        entry_price=10000,
        exit_price=9900,
        quantity=1,
        realized_pnl=-100,
        return_pct=-1.0,
        outcome_status="closed",
    )

    kpi = db.get_audit_kpi(bot_id="mirinae-4")

    assert kpi["closed"] == 2
    assert kpi["wins"] == 1
    assert kpi["losses"] == 1
    assert kpi["total_pnl"] == 200
    assert kpi["avg_pnl"] == 100
    assert kpi["profit_factor"] == 3.0
    db.close()
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest tests/test_mcp_audit_report.py -q
```

Expected: FAIL with missing `get_audit_kpi`.

- [ ] **Step 3: Add `get_audit_kpi` to `DB`**

Add:

```python
def get_audit_kpi(self, bot_id: str = "", days: int = 30) -> dict:
    since = (_kst_now() - timedelta(days=days)).isoformat()
    sql = "SELECT * FROM decision_outcomes WHERE outcome_status='closed' AND entry_time >= ?"
    params = [since]
    if bot_id:
        sql += " AND bot_id=?"
        params.append(bot_id)
    rows = [dict(r) for r in self._conn.execute(sql, params).fetchall()]
    pnls = [int(r.get("realized_pnl", 0) or 0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    by_code = {}
    for r in rows:
        code = r.get("stock_code", "")
        item = by_code.setdefault(code, {"closed": 0, "pnl": 0, "wins": 0, "losses": 0})
        pnl = int(r.get("realized_pnl", 0) or 0)
        item["closed"] += 1
        item["pnl"] += pnl
        item["wins"] += 1 if pnl > 0 else 0
        item["losses"] += 1 if pnl < 0 else 0
    return {
        "bot_id": bot_id,
        "closed": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(rows) if rows else 0,
        "total_pnl": total,
        "avg_pnl": total / len(rows) if rows else 0,
        "avg_return_pct": sum(float(r.get("return_pct", 0) or 0) for r in rows) / len(rows) if rows else 0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else 0,
        "by_code": by_code,
    }
```

- [ ] **Step 4: Add MCP read-only tool**

In `src/mcp_server.py`, add near performance tools:

```python
@mcp.tool()
@mcp_logged
def get_audit_kpi(bot_id: str = "", days: int = 30) -> str:
    """감사 DB 기반 봇 KPI를 조회합니다. bot_id를 비우면 전체."""
    from src.db import get_db
    k = get_db().get_audit_kpi(bot_id=bot_id, days=days)
    if k["closed"] == 0:
        return "감사 DB 청산 결과가 없습니다."
    lines = [
        f"[감사 DB KPI — {bot_id or '전체'} 최근 {days}일]",
        f"청산: {k['closed']}건 / 승 {k['wins']} / 패 {k['losses']}",
        f"승률: {k['win_rate']:.1%}",
        f"총손익: {k['total_pnl']:+,}원",
        f"거래당 손익: {k['avg_pnl']:+,.0f}원",
        f"거래당 수익률: {k['avg_return_pct']:+.2f}%",
        f"Profit Factor: {k['profit_factor']:.2f}",
        "",
        "[종목별]",
    ]
    for code, item in sorted(k["by_code"].items(), key=lambda x: x[1]["pnl"]):
        wr = item["wins"] / item["closed"] if item["closed"] else 0
        lines.append(
            f"  {code}: {item['closed']}건, 승률 {wr:.1%}, 손익 {item['pnl']:+,}원"
        )
    return "\n".join(lines)
```

- [ ] **Step 5: Run KPI tests**

Run:

```bash
pytest tests/test_mcp_audit_report.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit KPI helper**

Run:

```bash
git add src/db.py src/mcp_server.py tests/test_mcp_audit_report.py
git commit -m "feat: report decision audit kpi"
```

---

### Task 7: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
pytest \
  tests/test_decision_audit_db.py \
  tests/test_audit_recorder.py \
  tests/test_ai_caller_result.py \
  tests/test_mirinae_audit_flow.py \
  tests/test_mcp_audit_report.py \
  tests/test_db_board.py \
  tests/test_engine.py \
  tests/test_engine_cash.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run import/compile check**

Run:

```bash
python -m py_compile src/db.py src/bots/audit.py src/bots/ai_caller.py src/bots/mirinae.py src/bots/engine.py src/mcp_server.py
```

Expected: no output and exit code 0.

- [ ] **Step 3: Smoke test MCP tool list**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command='/Users/saintiron/Projects/FF/.venv/bin/python',
        args=['/Users/saintiron/Projects/FF/src/mcp_server.py'],
        env={'PYTHONPATH': '/Users/saintiron/Projects/FF'},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            assert 'get_audit_kpi' in tools
            print('get_audit_kpi available')

asyncio.run(main())
PY
```

Expected: `get_audit_kpi available`.

- [ ] **Step 4: Inspect git diff for unintended files**

Run:

```bash
git status --short
```

Expected: only intended files are modified by this implementation on top of the pre-existing dirty worktree.

- [ ] **Step 5: Final commit if verification required changes**

If Step 4 shows intended uncommitted files from fixes, run:

```bash
git add src/db.py src/bots/audit.py src/bots/ai_caller.py src/bots/mirinae.py src/bots/engine.py src/mcp_server.py tests/test_decision_audit_db.py tests/test_audit_recorder.py tests/test_ai_caller_result.py tests/test_mirinae_audit_flow.py tests/test_mcp_audit_report.py
git commit -m "test: verify decision audit db"
```

Expected: either a small verification commit or no commit because previous task commits already include all changes.

---

## Self-Review

Spec coverage:

- Durable audit trail: Task 1 and Task 2.
- Query evidence snapshots: Task 1, Task 2, Task 5.
- AI prompt/raw response/parsed JSON: Task 3 and Task 5.
- Preflight and block reasons: Task 1, Task 2, Task 5.
- Trade linking: Task 4 and Task 5.
- KPI reporting: Task 6.
- Retention safety: Task 1 creates separate audit tables and Task 5 does not use old decision cleanup on audit rows.

Type consistency:

- `run_id` is always a string.
- `payload_json`, `parsed_json`, and `criteria_json` are stored as JSON text.
- `success`, `parse_success`, and `passed` use integer booleans in SQLite.
- Existing `invoke_ai` remains string-returning.
- New `invoke_ai_result` returns `AIResult`.

Execution constraints:

- All new tools are read-only except audit logging.
- No order placement behavior changes are required.
- Existing `trades`, `decisions`, and bot logs are not rewritten.
