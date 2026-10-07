# Agent Ops Phase 1 — 데이터 계층 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 개선 루프 변경 이력 테이블(`improvement_actions`)과 stealth-volume 봇의 audit 연동을 추가하여 Agent Ops 데이터 계층을 완결한다.

**Architecture:** 기존 decision-audit-db 레이어(5개 audit 테이블 + `AuditRecorder`) 옆에 `improvement_actions` 테이블을 추가하고, Mirinae에만 있던 audit 시작 헬퍼 3개를 `BotBase`로 끌어올려 stealth-volume이 같은 경로로 판단을 기록하게 한다. 스펙: `docs/superpowers/specs/2026-06-10-agent-ops-design.md`.

**Tech Stack:** Python 3.14, SQLite(`sqlite3`), pytest. 기존 FF `DB`, `AuditRecorder`, `BotBase`, `StealthVolumeBot`.

**선행 조건:** `.worktrees/decision-audit-db` 브랜치가 main에 머지되어 있어야 한다 (audit 테이블·`src/bots/audit.py`·Mirinae 연동 포함). 머지 전이면 이 플랜을 시작하지 말 것.

**작업 브랜치:** main에서 `feat/agent-ops-phase1` 분기 후 작업.

**테스트 실행:** 가상환경 필수 — 모든 pytest는 `.venv/bin/python -m pytest`로 실행.

---

## File Structure

- Modify: `src/db.py`
  - `_migrate_improvement_actions()` 마이그레이션 (idempotent)
  - CRUD 헬퍼 4개: `create_improvement_action`, `update_improvement_action`, `get_improvement_action`, `get_improvement_actions`
- Modify: `src/bots/audit.py`
  - `make_action_id()` 추가 (개선 액션 ID 생성)
- Modify: `src/bots/base.py`
  - `_audit_pipe_id` / `_audit_mode` / `_audit_start_run` 헬퍼를 BotBase로 이동 (Mirinae에서 끌어올림)
- Modify: `src/bots/mirinae.py`
  - 위 3개 메서드 삭제 (BotBase 상속으로 대체)
- Modify: `src/bots/stealth_volume.py`
  - `_loop()`에 audit run 시작 → 증거 기록 → finish 연동
- Test: `tests/test_improvement_actions_db.py`
- Test: `tests/test_stealth_volume_audit.py`

---

### Task 1: `improvement_actions` 테이블 마이그레이션

**Files:**
- Modify: `src/db.py` (`_create_tables` 내 `self._migrate_decision_audit()` 호출 직후, 메서드 본문은 `_migrate_decision_audit` 정의 뒤)
- Test: `tests/test_improvement_actions_db.py`

- [ ] **Step 1: 실패하는 마이그레이션 테스트 작성**

`tests/test_improvement_actions_db.py` 생성:

```python
"""improvement_actions 테이블 마이그레이션·헬퍼 테스트."""

import json

from src.db import DB


EXPECTED_COLUMNS = {
    "id", "action_id", "timestamp", "updated_at",
    "target_type", "target", "before_json", "after_json",
    "rationale", "validation_json", "status",
    "tracking_window_json", "result_json",
}


def column_names(db: DB, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def index_names(db: DB) -> set[str]:
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_improvement_actions_table_created_idempotently(tmp_path):
    db = DB(tmp_path / "ops.db")
    db._create_tables()
    db._create_tables()

    assert EXPECTED_COLUMNS <= column_names(db, "improvement_actions")
    assert {
        "idx_improvement_actions_action_id",
        "idx_improvement_actions_target",
        "idx_improvement_actions_status",
        "idx_improvement_actions_ts",
    } <= index_names(db)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_improvement_actions_db.py -v`
Expected: FAIL — `improvement_actions` 테이블 없음 (PRAGMA 결과 빈 집합)

- [ ] **Step 3: 마이그레이션 구현**

`src/db.py`의 `_migrate_decision_audit` 메서드 정의 바로 뒤에 추가:

```python
    def _migrate_improvement_actions(self):
        """개선 루프 변경 이력 테이블 생성."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS improvement_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target TEXT NOT NULL,
                before_json TEXT DEFAULT '{}',
                after_json TEXT DEFAULT '{}',
                rationale TEXT DEFAULT '',
                validation_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'proposed',
                tracking_window_json TEXT DEFAULT '{}',
                result_json TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_improvement_actions_action_id
                ON improvement_actions(action_id);
            CREATE INDEX IF NOT EXISTS idx_improvement_actions_target
                ON improvement_actions(target_type, target);
            CREATE INDEX IF NOT EXISTS idx_improvement_actions_status
                ON improvement_actions(status);
            CREATE INDEX IF NOT EXISTS idx_improvement_actions_ts
                ON improvement_actions(timestamp);
        """)
        self._conn.commit()
```

그리고 `_create_tables` 안에서 `self._migrate_decision_audit()` 호출 바로 다음 줄에 추가:

```python
        self._migrate_improvement_actions()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_improvement_actions_db.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/db.py tests/test_improvement_actions_db.py
git commit -m "feat(db): add improvement_actions audit table"
```

---

### Task 2: `improvement_actions` CRUD 헬퍼

**Files:**
- Modify: `src/db.py` (Task 1의 `_migrate_improvement_actions` 뒤, `# ── 개선 이력 ──` 섹션으로 추가)
- Test: `tests/test_improvement_actions_db.py`

- [ ] **Step 1: 실패하는 CRUD 테스트 작성**

`tests/test_improvement_actions_db.py`에 추가:

```python
def test_create_and_get_improvement_action(tmp_path):
    db = DB(tmp_path / "ops.db")
    row_id = db.create_improvement_action(
        "act-001",
        target_type="param",
        target="mirinae-2:confidence_threshold",
        before={"value": 0.6},
        after={"value": 0.65},
        rationale="confidence 0.6 구간 승률 28%",
        validation={"sample": 34, "ev_estimate": 120.5},
        tracking_window={"trades": 20, "days": 5},
    )
    assert row_id > 0

    action = db.get_improvement_action("act-001")
    assert action["target_type"] == "param"
    assert action["target"] == "mirinae-2:confidence_threshold"
    assert action["status"] == "proposed"
    assert json.loads(action["before_json"]) == {"value": 0.6}
    assert json.loads(action["after_json"]) == {"value": 0.65}
    assert json.loads(action["validation_json"])["sample"] == 34
    assert json.loads(action["tracking_window_json"])["trades"] == 20


def test_create_improvement_action_is_idempotent_per_action_id(tmp_path):
    db = DB(tmp_path / "ops.db")
    first = db.create_improvement_action("act-001", target_type="param", target="a:b")
    second = db.create_improvement_action("act-001", target_type="param", target="a:b")
    assert first == second


def test_update_improvement_action_status_and_result(tmp_path):
    db = DB(tmp_path / "ops.db")
    db.create_improvement_action("act-001", target_type="ratio", target="strat-001:ratios")

    db.update_improvement_action("act-001", status="applied")
    assert db.get_improvement_action("act-001")["status"] == "applied"

    db.update_improvement_action(
        "act-001", status="rolled_back",
        result={"before_ev": 50, "after_ev": -20, "verdict": "worse"},
    )
    action = db.get_improvement_action("act-001")
    assert action["status"] == "rolled_back"
    assert json.loads(action["result_json"])["verdict"] == "worse"


def test_update_improvement_action_rejects_unknown_status(tmp_path):
    db = DB(tmp_path / "ops.db")
    db.create_improvement_action("act-001", target_type="param", target="a:b")
    db.update_improvement_action("act-001", status="bogus")
    assert db.get_improvement_action("act-001")["status"] == "proposed"


def test_get_improvement_actions_filters(tmp_path):
    db = DB(tmp_path / "ops.db")
    db.create_improvement_action("act-001", target_type="param", target="m2:x")
    db.create_improvement_action("act-002", target_type="ratio", target="strat-001:ratios")
    db.update_improvement_action("act-002", status="applied")

    assert len(db.get_improvement_actions()) == 2
    assert [a["action_id"] for a in db.get_improvement_actions(target_type="param")] == ["act-001"]
    assert [a["action_id"] for a in db.get_improvement_actions(status="applied")] == ["act-002"]
    assert [a["action_id"] for a in db.get_improvement_actions(target="m2:x")] == ["act-001"]
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_improvement_actions_db.py -v`
Expected: FAIL — `AttributeError: 'DB' object has no attribute 'create_improvement_action'`

- [ ] **Step 3: 헬퍼 구현**

`src/db.py`에 추가 (`_migrate_improvement_actions` 뒤). 기존 audit 헬퍼와 동일한 스타일 (`_kst_now`, `_json_text`, allowed-필드 화이트리스트):

```python
    # ── 개선 이력 (improvement_actions) ──

    IMPROVEMENT_STATUSES = {
        "proposed", "validated", "applied",
        "confirmed", "rolled_back", "rejected",
    }

    def create_improvement_action(self, action_id: str, target_type: str = "",
                                  target: str = "", before=None, after=None,
                                  rationale: str = "", validation=None,
                                  tracking_window=None,
                                  status: str = "proposed") -> int:
        now = _kst_now().isoformat()
        self._conn.execute(
            """INSERT OR IGNORE INTO improvement_actions
               (action_id, timestamp, updated_at, target_type, target,
                before_json, after_json, rationale, validation_json,
                status, tracking_window_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action_id, now, now, target_type, target,
             self._json_text(before), self._json_text(after), rationale,
             self._json_text(validation), status,
             self._json_text(tracking_window))
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM improvement_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        return row["id"]

    def update_improvement_action(self, action_id: str, **fields) -> None:
        allowed = {"status", "after", "rationale", "validation",
                   "tracking_window", "result"}
        json_fields = {"after": "after_json", "validation": "validation_json",
                       "tracking_window": "tracking_window_json",
                       "result": "result_json"}
        updates = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "status":
                if value not in self.IMPROVEMENT_STATUSES:
                    continue
                updates["status"] = value
            elif key == "rationale":
                updates["rationale"] = value
            else:
                updates[json_fields[key]] = self._json_text(value)
        if not updates:
            return

        updates["updated_at"] = _kst_now().isoformat()
        set_clause = ", ".join(f"{key}=?" for key in updates)
        params = list(updates.values()) + [action_id]
        self._conn.execute(
            f"UPDATE improvement_actions SET {set_clause} WHERE action_id=?",
            params
        )
        self._conn.commit()

    def get_improvement_action(self, action_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM improvement_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        return dict(row) if row else {}

    def get_improvement_actions(self, target_type: str = "", target: str = "",
                                status: str = "", days: int = 0,
                                limit: int = 100) -> list[dict]:
        query = "SELECT * FROM improvement_actions WHERE 1=1"
        params: list = []
        if target_type:
            query += " AND target_type=?"
            params.append(target_type)
        if target:
            query += " AND target=?"
            params.append(target)
        if status:
            query += " AND status=?"
            params.append(status)
        if days > 0:
            query += " AND timestamp >= datetime('now', ?)"
            params.append(f"-{days} days")
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_improvement_actions_db.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/db.py tests/test_improvement_actions_db.py
git commit -m "feat(db): improvement_actions CRUD helpers"
```

---

### Task 3: `make_action_id` 헬퍼

**Files:**
- Modify: `src/bots/audit.py` (`make_run_id` 바로 아래)
- Test: `tests/test_improvement_actions_db.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_improvement_actions_db.py`에 추가:

```python
def test_make_action_id_format_and_uniqueness():
    from src.bots.audit import make_action_id

    first = make_action_id("param", "mirinae-2:confidence_threshold")
    second = make_action_id("param", "mirinae-2:confidence_threshold")

    assert first.startswith("param:mirinae-2:confidence_threshold:")
    assert first != second  # uuid suffix
    assert len(first.split(":")) == 5  # type:bot:key:timestamp:uuid
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_improvement_actions_db.py::test_make_action_id_format_and_uniqueness -v`
Expected: FAIL — `ImportError: cannot import name 'make_action_id'`

- [ ] **Step 3: 구현**

`src/bots/audit.py`의 `make_run_id` 정의 아래에 추가:

```python
def make_action_id(target_type: str, target: str) -> str:
    """Create a unique improvement action id."""
    timestamp = datetime.now(_KST).strftime("%Y%m%dT%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{target_type}:{target}:{timestamp}:{suffix}"
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_improvement_actions_db.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/bots/audit.py tests/test_improvement_actions_db.py
git commit -m "feat(audit): make_action_id for improvement actions"
```

---

### Task 4: audit 시작 헬퍼를 BotBase로 이동

Mirinae에만 있는 `_audit_pipe_id` / `_audit_mode` / `_audit_start_run`을 `BotBase`로 끌어올려 stealth-volume(및 미래 봇)이 재사용하게 한다. 메서드 본문은 그대로 이동 — 동작 변경 없음.

**Files:**
- Modify: `src/bots/base.py` (BotBase 클래스 내부, `_save_state` 근처)
- Modify: `src/bots/mirinae.py` (`_audit_pipe_id`, `_audit_mode`, `_audit_start_run` 3개 메서드 삭제 — `_audit_record_*` 메서드들은 Mirinae 전용이므로 유지)
- Test: 기존 `tests/test_mirinae_audit_flow.py` (회귀 확인용, 수정 없음)

- [ ] **Step 1: BotBase에 메서드 추가**

`src/bots/base.py`의 BotBase 클래스에 추가 (mirinae.py 339-371줄 본문 그대로, `logger`는 base.py 모듈 로거 사용):

```python
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
```

- [ ] **Step 2: mirinae.py에서 동일 메서드 3개 삭제**

`src/bots/mirinae.py`에서 `_audit_pipe_id`, `_audit_mode`, `_audit_start_run` 메서드 정의를 삭제한다 (호출부는 그대로 — 상속으로 해결). `_audit_record_structured_evidence`, `_audit_record_evidence`, `_audit_record_ai`, `_audit_record_preflight`는 Mirinae 전용이므로 남긴다.

- [ ] **Step 3: 기존 Mirinae audit 테스트로 회귀 확인**

Run: `.venv/bin/python -m pytest tests/test_mirinae_audit_flow.py tests/test_audit_recorder.py -v`
Expected: PASS (전체 통과 — 동작 변경 없음)

- [ ] **Step 4: 커밋**

```bash
git add src/bots/base.py src/bots/mirinae.py
git commit -m "refactor(bots): move audit run helpers to BotBase"
```

---

### Task 5: stealth-volume audit 연동

**Files:**
- Modify: `src/bots/stealth_volume.py` (`_loop()` — "1. 스캔" ~ "5. DB 판단 기록" 구간)
- Test: `tests/test_stealth_volume_audit.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_stealth_volume_audit.py` 생성:

```python
"""stealth-volume audit 연동 테스트."""

import src.db as db_module
from src.db import DB
from src.bots.stealth_volume import StealthVolumeBot


def make_bot() -> StealthVolumeBot:
    """초기화 부작용 없이 audit 헬퍼만 검증하기 위한 베어 인스턴스."""
    bot = StealthVolumeBot.__new__(StealthVolumeBot)
    bot._config = None
    bot._scan_count = 3
    return bot


def test_audit_scan_cycle_records_run_evidence_and_finish(tmp_path, monkeypatch):
    db = DB(tmp_path / "audit.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)

    bot = make_bot()
    recorder = bot._audit_start_run()
    assert recorder is not None

    candidates = [
        {"code": "069500", "vol_ratio": 4.2, "price_pct": 0.8},
        {"code": "122630", "vol_ratio": 3.1, "price_pct": 0.5},
    ]
    bot._audit_record_candidates(recorder, candidates)

    decision = {"action": "buy", "code": "069500", "reason": "거래량 4.2배 + 양봉"}
    bot._audit_finish_scan(recorder, decision, decision_id=7, trade_id=11,
                           trade_success=True)

    run = db.get_decision_run(recorder.run_id)
    assert run["bot_id"] == "stealth-volume"
    assert run["scan_count"] == 3
    assert run["status"] == "ordered"
    assert run["action"] == "buy"
    assert run["stock_code"] == "069500"
    assert run["decision_id"] == 7
    assert run["trade_id"] == 11

    snapshots = db.get_evidence_snapshots(recorder.run_id)
    assert len(snapshots) == 1
    assert snapshots[0]["source"] == "signals"


def test_audit_finish_scan_wait_is_completed(tmp_path, monkeypatch):
    db = DB(tmp_path / "audit.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)

    bot = make_bot()
    recorder = bot._audit_start_run()
    bot._audit_finish_scan(recorder, {"action": "wait", "reason": "신호 없음"},
                           decision_id=0, trade_id=0, trade_success=False)

    run = db.get_decision_run(recorder.run_id)
    assert run["status"] == "completed"
    assert run["action"] == "wait"


def test_audit_finish_scan_failed_buy_records_block_reason(tmp_path, monkeypatch):
    db = DB(tmp_path / "audit.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)

    bot = make_bot()
    recorder = bot._audit_start_run()
    decision = {"action": "buy", "code": "069500", "reason": "일일 손실 한도 도달"}
    bot._audit_finish_scan(recorder, decision, decision_id=0, trade_id=0,
                           trade_success=False)

    run = db.get_decision_run(recorder.run_id)
    assert run["status"] == "failed"

    checks = db.get_preflight_checks(recorder.run_id)
    assert len(checks) == 1
    assert checks[0]["check_name"] == "execution"
    assert checks[0]["passed"] in (0, False)
    assert "손실 한도" in checks[0]["message"]


def test_audit_helpers_tolerate_none_recorder():
    bot = make_bot()
    # recorder가 None이어도 (audit DB 실패 시) 예외 없이 통과해야 함
    bot._audit_record_candidates(None, [])
    bot._audit_finish_scan(None, {"action": "wait"}, 0, 0, False)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m pytest tests/test_stealth_volume_audit.py -v`
Expected: FAIL — `AttributeError: ... '_audit_record_candidates'`

- [ ] **Step 3: 헬퍼 구현**

`src/bots/stealth_volume.py`의 `_loop` 정의 위에 추가:

```python
    # ── 판단 감사 (decision audit) ──

    def _audit_record_candidates(self, recorder, candidates: list[dict]):
        """스캔 결과 상위 신호를 증거로 기록."""
        if recorder is None:
            return
        try:
            top = candidates[:5]
            recorder.evidence(
                "signals",
                payload={"candidates": top, "count": len(candidates)},
                summary=", ".join(
                    f"{c.get('code', '')} x{c.get('vol_ratio', 0):.1f}"
                    for c in top
                ) or "신호 없음",
            )
        except Exception as exc:
            logger.warning("[%s] audit candidates failed: %s", self.BOT_ID, exc)

    def _audit_finish_scan(self, recorder, decision: dict,
                           decision_id: int, trade_id: int,
                           trade_success: bool):
        """스캔 종료 상태를 audit run에 기록."""
        if recorder is None:
            return
        try:
            action = decision.get("action", "")
            if action in ("buy", "sell") and trade_success:
                status = "ordered"
            elif action in ("buy", "sell"):
                status = "failed"
                # 주문 미체결 사유를 preflight 행으로 보존 (차단 사유 통계용)
                recorder.preflight(
                    "execution",
                    passed=False,
                    severity="block",
                    message=decision.get("reason", ""),
                    stock_code=decision.get("code", ""),
                )
            else:
                status = "completed"
            recorder.finish(
                status=status,
                action=action,
                stock_code=decision.get("code", ""),
                decision_id=decision_id or 0,
                trade_id=trade_id or 0,
                error=decision.get("error", ""),
            )
        except Exception as exc:
            logger.warning("[%s] audit finish failed: %s", self.BOT_ID, exc)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_stealth_volume_audit.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: `_loop()`에 연동**

`src/bots/stealth_volume.py`의 `_loop()` 내부를 다음과 같이 수정한다.

`self._scan_count += 1` 직후에 recorder 생성:

```python
                self._scan_count += 1
                now = kst_now()
                audit = self._audit_start_run()
```

`candidates = self.scan()` 직후에 증거 기록:

```python
                # 1. 스캔
                candidates = self.scan()
                self._audit_record_candidates(audit, candidates)
```

기존 "판단↔거래 연결" 블록을 trade_id를 변수로 들고 있도록 수정하고, 그 뒤에 finish 호출 추가. 기존 코드:

```python
                # 판단↔거래 연결
                if decision_id and result and result.get("success"):
                    try:
                        from src.db import get_db
                        # 최근 이 봇의 거래를 찾아서 연결
                        recent = get_db().get_trades(source=self.BOT_ID, limit=1)
                        if recent:
                            get_db().link_decision_trade(decision_id, recent[0]["id"])
                    except Exception:
                        pass
```

를 다음으로 교체:

```python
                # 판단↔거래 연결
                linked_trade_id = 0
                if decision_id and result and result.get("success"):
                    try:
                        from src.db import get_db
                        # 최근 이 봇의 거래를 찾아서 연결
                        recent = get_db().get_trades(source=self.BOT_ID, limit=1)
                        if recent:
                            linked_trade_id = recent[0]["id"]
                            get_db().link_decision_trade(decision_id, linked_trade_id)
                    except Exception:
                        pass

                # 판단 감사 종료 기록
                self._audit_finish_scan(
                    audit, decision,
                    decision_id=decision_id or 0,
                    trade_id=linked_trade_id,
                    trade_success=bool(result and result.get("success")),
                )
```

- [ ] **Step 6: 전체 테스트로 회귀 확인**

Run: `.venv/bin/python -m pytest tests/test_stealth_volume_audit.py tests/ -k "stealth or audit" -v`
Expected: PASS (stealth-volume 기존 테스트 + audit 테스트 전체 통과)

- [ ] **Step 7: 커밋**

```bash
git add src/bots/stealth_volume.py tests/test_stealth_volume_audit.py
git commit -m "feat(stealth-volume): record decision audit runs"
```

---

### Task 6: 전체 검증 및 마무리

**Files:** 없음 (검증만)

- [ ] **Step 1: 전체 테스트 스위트 실행**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: 전체 PASS. 실패 시 해당 태스크로 돌아가 수정 (이 플랜 외 기존 실패가 있으면 main에서도 실패하는지 확인 후 별도 보고).

- [ ] **Step 2: 마이그레이션 실DB 스모크 테스트**

Run:
```bash
.venv/bin/python -c "
from src.db import get_db
db = get_db()
print('improvement_actions' in {
    r['name'] for r in db._conn.execute(
        \"SELECT name FROM sqlite_master WHERE type='table'\")
})"
```
Expected: `True` — 기존 `data/ff.db`에 무손실 추가 확인 (기존 테이블 변경 없음)

- [ ] **Step 3: 브랜치 마무리**

superpowers:finishing-a-development-branch 스킬에 따라 머지/PR 결정.

---

## Phase 2·3 플랜 (후속 작성)

스펙의 나머지는 별도 플랜으로 작성한다 — 각 플랜은 독립적으로 동작 가능한 소프트웨어를 산출한다:

- **Phase 2 — 투명성 대시보드**: `GET /api/audit/runs`(필터·페이징), `GET /api/audit/runs/{run_id}`(리플레이), `GET /api/improvements`, `agent-ops.html` (타임라인 → 리플레이 → KPI → 개선이력). Phase 1 머지 후 작성.
  - 백로그(Phase 1 리뷰에서 발견): 봇 `_loop`에서 scan/decide/execute 예외 시 audit run이 `status='started'`로 영구 잔존함. Phase 2에서 (a) 타임라인이 오래된 `started` run을 stale로 표시하고, (b) 정리 쿼리(예: 1시간 경과 started → `failed`로 마킹) 추가 검토.
- **Phase 3 — Insight Engine**: 분석 질의 모듈 → 통계 검증 게이트 → target_type별 적용기 → 추적·롤백 → 스케줄 연동 → kill switch. Phase 2의 개선이력 뷰가 관찰 모드 검증 수단이므로 Phase 2 이후 작성.
