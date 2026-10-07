"""SQLite DB — 거래/자산/판단 이력 통합 저장."""

import json
import sqlite3
import logging
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def _kst_now() -> datetime:
    """KST 현재 시각. db.py 내부 전용 (순환 import 방지)."""
    return datetime.now(_KST)

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DB_PATH = PROJECT_ROOT / "data" / "ff.db"


def normalize_published(rfc2822: str) -> str:
    """RFC2822 published → ISO 8601. 실패 시 빈 문자열."""
    if not rfc2822:
        return ""
    try:
        dt = parsedate_to_datetime(rfc2822)
        return dt.isoformat()
    except Exception:
        return ""


class DB:
    """SQLite 기반 거래/자산 추적."""

    def __init__(self, path: str | Path = DB_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pipe_id TEXT DEFAULT '',
                stock_code TEXT NOT NULL,
                stock_name TEXT DEFAULT '',
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                order_no TEXT DEFAULT '',
                success BOOLEAN DEFAULT 1,
                reason TEXT DEFAULT '',
                source TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pipe_id TEXT DEFAULT '',
                cash INTEGER DEFAULT 0,
                holdings_value INTEGER DEFAULT 0,
                total_value INTEGER DEFAULT 0,
                realized_pnl INTEGER DEFAULT 0,
                unrealized_pnl INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pipe_id TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 0,
                avg_price INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                UNIQUE(pipe_id, stock_code)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                type TEXT DEFAULT '',
                pipe_id TEXT DEFAULT '',
                input_summary TEXT DEFAULT '',
                output_summary TEXT DEFAULT '',
                action_taken TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                bot_id TEXT PRIMARY KEY,
                holding_code TEXT DEFAULT '',
                holding_qty INTEGER DEFAULT 0,
                holding_price INTEGER DEFAULT 0,
                realized_pnl INTEGER DEFAULT 0,
                scan_count INTEGER DEFAULT 0,
                today_rotations INTEGER DEFAULT 0,
                rotation_date TEXT DEFAULT '',
                extra TEXT DEFAULT '{}',
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT DEFAULT '',
                url TEXT DEFAULT '',
                source TEXT DEFAULT '',
                published TEXT DEFAULT '',
                news_hash TEXT UNIQUE
            );

            CREATE INDEX IF NOT EXISTS idx_trades_pipe ON trades(pipe_id);
            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_snapshots_pipe ON snapshots(pipe_id);
            CREATE INDEX IF NOT EXISTS idx_holdings_pipe ON holdings(pipe_id);
            CREATE INDEX IF NOT EXISTS idx_news_ts ON news(timestamp);
            CREATE INDEX IF NOT EXISTS idx_news_hash ON news(news_hash);
        """)
        self._conn.commit()

        # news 테이블 컬럼 마이그레이션 (기존 DB 호환)
        for col, typ in [("category", "TEXT DEFAULT ''"),
                         ("tags", "TEXT DEFAULT ''"),
                         ("sentiment", "TEXT DEFAULT ''"),
                         ("embedding", "BLOB DEFAULT NULL"),
                         ("kept", "INTEGER DEFAULT 0"),
                         ("published_iso", "TEXT DEFAULT ''")]:
            try:
                self._conn.execute(f"ALTER TABLE news ADD COLUMN {col} {typ}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # 이미 존재
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_news_category ON news(category)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_news_published_iso ON news(published_iso)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_news_kept ON news(kept)")

        # 산업 지수 스냅샷
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS industry_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                sector TEXT NOT NULL,
                sub_sector TEXT DEFAULT '',
                score REAL NOT NULL,
                sentiment_avg REAL DEFAULT 0,
                news_count INTEGER DEFAULT 0,
                volume_signal REAL DEFAULT 0,
                velocity REAL DEFAULT 0,
                direction TEXT DEFAULT 'FLAT',
                momentum TEXT DEFAULT '{}',
                extra TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_ii_sector_ts ON industry_index(sector, timestamp);
            CREATE INDEX IF NOT EXISTS idx_ii_ts ON industry_index(timestamp);
        """)
        self._conn.commit()

        # 파이프 게시판
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS board (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pipe_id TEXT DEFAULT '',
                bot_id TEXT NOT NULL,
                type TEXT NOT NULL,
                code TEXT DEFAULT '',
                content TEXT NOT NULL,
                extra TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_board_pipe ON board(pipe_id);
            CREATE INDEX IF NOT EXISTS idx_board_ts ON board(timestamp);
        """)
        self._conn.commit()

        # decisions/trades 확장 마이그레이션
        self._migrate_decisions_trades()
        self._migrate_decision_audit()
        self._migrate_improvement_actions()

    def _migrate_decisions_trades(self):
        """decisions/trades 테이블에 구조화 컬럼 추가 (자기 진화용)."""
        # decisions 확장: 구조화된 판단 기록
        for col, typ in [
            ("bot_id", "TEXT DEFAULT ''"),
            ("stock_code", "TEXT DEFAULT ''"),
            ("action", "TEXT DEFAULT ''"),          # buy/sell/hold/skip
            ("confidence", "REAL DEFAULT 0"),
            ("indicators", "TEXT DEFAULT '{}'"),     # JSON: 판단 근거 지표
            ("trade_id", "INTEGER DEFAULT 0"),       # trades FK (체결 연결)
        ]:
            try:
                self._conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # 이미 존재

        # trades 확장: 실현손익 + 판단 연결
        for col, typ in [
            ("realized_pnl", "INTEGER DEFAULT 0"),
            ("decision_id", "INTEGER DEFAULT 0"),    # decisions FK (판단 연결)
        ]:
            try:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass

        # 인덱스
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_bot ON decisions(bot_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(timestamp)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_source ON trades(source)")
        self._conn.commit()

    def _json_text(self, value) -> str:
        """JSON 저장용 문자열 변환."""
        return json.dumps({} if value is None else value, ensure_ascii=False, default=str)

    def _migrate_decision_audit(self):
        """AI 판단 감사 테이블 생성."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS decision_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL,
                updated_at TEXT NOT NULL,
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

            CREATE TABLE IF NOT EXISTS evidence_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                stock_code TEXT DEFAULT '',
                payload_json TEXT DEFAULT '{}',
                summary TEXT DEFAULT '',
                success BOOLEAN DEFAULT 1,
                error TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS ai_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                run_id TEXT NOT NULL,
                bot_id TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                prompt TEXT DEFAULT '',
                raw_response TEXT DEFAULT '',
                parsed_json TEXT DEFAULT '{}',
                parse_success BOOLEAN DEFAULT 0,
                parse_error TEXT DEFAULT '',
                confidence REAL DEFAULT 0,
                criteria_json TEXT DEFAULT '{}',
                bull_summary TEXT DEFAULT '',
                bear_summary TEXT DEFAULT '',
                trader_reason TEXT DEFAULT '',
                tokens INTEGER DEFAULT 0,
                latency_ms INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS preflight_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                run_id TEXT NOT NULL,
                check_name TEXT NOT NULL,
                stock_code TEXT DEFAULT '',
                passed BOOLEAN DEFAULT 1,
                severity TEXT DEFAULT 'info',
                message TEXT DEFAULT '',
                payload_json TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS decision_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                decision_id INTEGER DEFAULT 0,
                buy_trade_id INTEGER DEFAULT 0,
                sell_trade_id INTEGER DEFAULT 0,
                bot_id TEXT DEFAULT '',
                stock_code TEXT DEFAULT '',
                entry_time TEXT DEFAULT '',
                exit_time TEXT DEFAULT '',
                hold_minutes INTEGER DEFAULT 0,
                entry_price REAL DEFAULT 0,
                exit_price REAL DEFAULT 0,
                quantity INTEGER DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                return_pct REAL DEFAULT 0,
                max_favorable_pct REAL DEFAULT 0,
                max_adverse_pct REAL DEFAULT 0,
                outcome_status TEXT DEFAULT 'open',
                notes TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_decision_runs_run_id ON decision_runs(run_id);
            CREATE INDEX IF NOT EXISTS idx_decision_runs_bot_ts ON decision_runs(bot_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_decision_runs_stock_ts ON decision_runs(stock_code, timestamp);
            CREATE INDEX IF NOT EXISTS idx_evidence_snapshots_run_id ON evidence_snapshots(run_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_snapshots_source ON evidence_snapshots(source);
            CREATE INDEX IF NOT EXISTS idx_evidence_snapshots_stock_ts ON evidence_snapshots(stock_code, timestamp);
            CREATE INDEX IF NOT EXISTS idx_ai_audit_logs_run_id ON ai_audit_logs(run_id);
            CREATE INDEX IF NOT EXISTS idx_ai_audit_logs_bot_ts ON ai_audit_logs(bot_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_preflight_checks_run_id ON preflight_checks(run_id);
            CREATE INDEX IF NOT EXISTS idx_preflight_checks_name ON preflight_checks(check_name);
            CREATE INDEX IF NOT EXISTS idx_decision_outcomes_run_id ON decision_outcomes(run_id);
            CREATE INDEX IF NOT EXISTS idx_decision_outcomes_code ON decision_outcomes(stock_code);
            CREATE INDEX IF NOT EXISTS idx_decision_outcomes_bot_ts ON decision_outcomes(bot_id, entry_time);
        """)
        self._conn.commit()

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
        """개선 액션 생성. 동일 action_id 재호출 시 기존 id 반환 (멱등)."""
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
        """개선 액션 상태·결과 업데이트. 허용 필드만 갱신."""
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
        """action_id로 단건 조회."""
        row = self._conn.execute(
            "SELECT * FROM improvement_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        return dict(row) if row else {}

    def get_improvement_actions(self, target_type: str = "", target: str = "",
                                status: str = "", days: int = 0,
                                limit: int = 100) -> list[dict]:
        """필터 기반 개선 액션 목록 조회 (최신순)."""
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
            cutoff = (_kst_now() - timedelta(days=days)).isoformat()
            query += " AND timestamp >= ?"
            params.append(cutoff)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ── AI 판단 감사 ──

    def start_decision_run(self, run_id: str, pipe_id: str = "", bot_id: str = "",
                           scan_count: int = 0, mode: str = "",
                           status: str = "started") -> int:
        now = _kst_now().isoformat()
        self._conn.execute(
            """INSERT OR IGNORE INTO decision_runs
               (run_id, timestamp, updated_at, pipe_id, bot_id, scan_count, mode, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, now, now, pipe_id, bot_id, scan_count, mode, status)
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM decision_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return row["id"]

    def update_decision_run(self, run_id: str, **fields) -> None:
        allowed = {"status", "action", "stock_code", "decision_id", "trade_id", "error", "mode"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return

        updates["updated_at"] = _kst_now().isoformat()
        set_clause = ", ".join(f"{key}=?" for key in updates)
        params = list(updates.values()) + [run_id]
        self._conn.execute(
            f"UPDATE decision_runs SET {set_clause} WHERE run_id=?",
            params
        )
        self._conn.commit()

    def get_decision_run(self, run_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM decision_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else {}

    def log_evidence_snapshot(self, run_id: str, source: str, stock_code: str = "",
                              payload=None, summary: str = "",
                              success: bool = True, error: str = "") -> int:
        cur = self._conn.execute(
            """INSERT INTO evidence_snapshots
               (timestamp, run_id, source, stock_code, payload_json, summary, success, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_kst_now().isoformat(), run_id, source, stock_code,
             self._json_text(payload), summary, success, error)
        )
        self._conn.commit()
        return cur.lastrowid

    def get_evidence_snapshots(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM evidence_snapshots WHERE run_id=? ORDER BY id ASC",
            (run_id,)
        ).fetchall()]

    def log_ai_audit(self, run_id: str, bot_id: str = "", provider: str = "",
                     model: str = "", prompt: str = "", raw_response: str = "",
                     parsed=None, parse_success: bool = False,
                     parse_error: str = "", confidence: float = 0,
                     criteria=None, bull_summary: str = "",
                     bear_summary: str = "", trader_reason: str = "",
                     tokens: int = 0, latency_ms: int = 0) -> int:
        cur = self._conn.execute(
            """INSERT INTO ai_audit_logs
               (timestamp, run_id, bot_id, provider, model, prompt, raw_response,
                parsed_json, parse_success, parse_error, confidence, criteria_json,
                bull_summary, bear_summary, trader_reason, tokens, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_kst_now().isoformat(), run_id, bot_id, provider, model, prompt,
             raw_response, self._json_text(parsed), parse_success, parse_error,
             confidence, self._json_text(criteria), bull_summary, bear_summary,
             trader_reason, tokens, latency_ms)
        )
        self._conn.commit()
        return cur.lastrowid

    def get_ai_audit(self, run_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM ai_audit_logs WHERE run_id=? ORDER BY id DESC LIMIT 1",
            (run_id,)
        ).fetchone()
        return dict(row) if row else {}

    def log_preflight_check(self, run_id: str, check_name: str, stock_code: str = "",
                            passed: bool = True, severity: str = "info",
                            message: str = "", payload=None) -> int:
        cur = self._conn.execute(
            """INSERT INTO preflight_checks
               (timestamp, run_id, check_name, stock_code, passed, severity, message, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_kst_now().isoformat(), run_id, check_name, stock_code, passed,
             severity, message, self._json_text(payload))
        )
        self._conn.commit()
        return cur.lastrowid

    def get_preflight_checks(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM preflight_checks WHERE run_id=? ORDER BY id ASC",
            (run_id,)
        ).fetchall()]

    def upsert_decision_outcome(self, run_id: str, decision_id: int | None = None,
                                buy_trade_id: int | None = None,
                                sell_trade_id: int | None = None,
                                bot_id: str | None = None,
                                stock_code: str | None = None,
                                entry_time: str | None = None,
                                exit_time: str | None = None,
                                hold_minutes: int | None = None,
                                entry_price: float | None = None,
                                exit_price: float | None = None,
                                quantity: int | None = None,
                                realized_pnl: float | None = None,
                                return_pct: float | None = None,
                                max_favorable_pct: float | None = None,
                                max_adverse_pct: float | None = None,
                                outcome_status: str | None = None,
                                notes: str | None = None) -> int:
        now = _kst_now().isoformat()
        fields = {
            "decision_id": decision_id,
            "buy_trade_id": buy_trade_id,
            "sell_trade_id": sell_trade_id,
            "bot_id": bot_id,
            "stock_code": stock_code,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "hold_minutes": hold_minutes,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": quantity,
            "realized_pnl": realized_pnl,
            "return_pct": return_pct,
            "max_favorable_pct": max_favorable_pct,
            "max_adverse_pct": max_adverse_pct,
            "outcome_status": outcome_status,
            "notes": notes,
        }
        defaults = {
            "decision_id": 0,
            "buy_trade_id": 0,
            "sell_trade_id": 0,
            "bot_id": "",
            "stock_code": "",
            "entry_time": "",
            "exit_time": "",
            "hold_minutes": 0,
            "entry_price": 0,
            "exit_price": 0,
            "quantity": 0,
            "realized_pnl": 0,
            "return_pct": 0,
            "max_favorable_pct": 0,
            "max_adverse_pct": 0,
            "outcome_status": "open",
            "notes": "",
        }
        values = {key: defaults[key] if value is None else value for key, value in fields.items()}
        update_clause = ",\n               ".join(
            f"{key}=CASE WHEN ? THEN excluded.{key} ELSE decision_outcomes.{key} END"
            for key in fields
        )
        cur = self._conn.execute(
            """INSERT INTO decision_outcomes
               (timestamp, updated_at, run_id, decision_id, buy_trade_id, sell_trade_id,
                bot_id, stock_code, entry_time, exit_time, hold_minutes, entry_price,
                exit_price, quantity, realized_pnl, return_pct, max_favorable_pct,
                max_adverse_pct, outcome_status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
               updated_at=excluded.updated_at,
               """ + update_clause + """
               RETURNING id""",
            (now, now, run_id, values["decision_id"], values["buy_trade_id"],
             values["sell_trade_id"], values["bot_id"], values["stock_code"],
             values["entry_time"], values["exit_time"], values["hold_minutes"],
             values["entry_price"], values["exit_price"], values["quantity"],
             values["realized_pnl"], values["return_pct"], values["max_favorable_pct"],
             values["max_adverse_pct"], values["outcome_status"], values["notes"],
             *[value is not None for value in fields.values()])
        )
        row = cur.fetchone()
        self._conn.commit()
        return row["id"]

    def get_audit_kpi(self, bot_id: str = "", days: int = 30) -> dict:
        """감사 DB 청산 outcome KPI 조회."""
        since = (_kst_now() - timedelta(days=days)).isoformat()
        time_expr = (
            "COALESCE(NULLIF(exit_time, ''), NULLIF(updated_at, ''), "
            "NULLIF(timestamp, ''), NULLIF(entry_time, ''))"
        )
        sql = (
            "SELECT * FROM decision_outcomes "
            f"WHERE outcome_status = 'closed' AND {time_expr} >= ?"
        )
        params: list = [since]
        if bot_id:
            sql += " AND bot_id = ?"
            params.append(bot_id)
        sql += f" ORDER BY {time_expr} DESC, id DESC"

        rows = [dict(r) for r in self._conn.execute(sql, params).fetchall()]
        pnls = [float(r.get("realized_pnl") or 0) for r in rows]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        total_pnl = sum(pnls)
        closed = len(rows)
        total_return = sum(float(r.get("return_pct") or 0) for r in rows)
        gross_wins = sum(wins)
        gross_losses = abs(sum(losses))
        if gross_losses:
            profit_factor = gross_wins / gross_losses
        elif gross_wins:
            profit_factor = None
        else:
            profit_factor = 0

        by_code: dict[str, dict] = {}
        for row, pnl in zip(rows, pnls):
            code = row.get("stock_code") or ""
            item = by_code.setdefault(
                code, {"closed": 0, "pnl": 0, "wins": 0, "losses": 0}
            )
            item["closed"] += 1
            item["pnl"] += pnl
            if pnl > 0:
                item["wins"] += 1
            elif pnl < 0:
                item["losses"] += 1

        return {
            "bot_id": bot_id,
            "closed": closed,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / closed if closed else 0,
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / closed if closed else 0,
            "avg_return_pct": total_return / closed if closed else 0,
            "profit_factor": profit_factor,
            "by_code": by_code,
        }

    # ── 게시판 ──

    def post_board(self, pipe_id: str, bot_id: str, type: str,
                   content: str, code: str = "", extra: str = "") -> int:
        """게시판에 글 등록. type: trade/opinion/alert/analysis"""
        cur = self._conn.execute(
            "INSERT INTO board (timestamp, pipe_id, bot_id, type, code, content, extra) VALUES (?,?,?,?,?,?,?)",
            (_kst_now().isoformat(), pipe_id, bot_id, type, code, content, extra)
        )
        self._conn.commit()
        return cur.lastrowid

    def get_board(self, pipe_id: str = "", limit: int = 30) -> list[dict]:
        """게시판 글 조회 (최신순)."""
        sql = "SELECT * FROM board"
        params = []
        if pipe_id:
            sql += " WHERE pipe_id = ?"
            params.append(pipe_id)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        posts = [dict(r) for r in self._conn.execute(sql, params).fetchall()]
        self._attach_board_full_content(posts)
        return posts

    def _attach_board_full_content(self, posts: list[dict]):
        """Attach full trade content for board rows saved before long text support."""
        for post in posts:
            if post.get("full_content"):
                continue
            full_content = ""
            if post.get("type") == "trade":
                trade = self._find_board_trade(post)
                if trade:
                    full_content = self._format_board_trade_content(dict(trade))
            elif post.get("type") == "opinion":
                decision = self._find_board_decision(post)
                if decision:
                    full_content = self._format_board_decision_content(dict(decision))
            if full_content:
                post["full_content"] = full_content

    def _find_board_trade(self, post: dict):
        code = post.get("code") or ""
        pipe_id = post.get("pipe_id") or ""
        bot_id = post.get("bot_id") or ""
        if not code or not bot_id:
            return None

        content = post.get("content") or ""
        side = "sell" if "매도" in content else "buy" if "매수" in content else ""
        minute_prefix = (post.get("timestamp") or "")[:16]

        where = "pipe_id=? AND source=? AND stock_code=?"
        params = [pipe_id, bot_id, code]
        if side:
            where += " AND side=?"
            params.append(side)
        if minute_prefix:
            row = self._conn.execute(
                f"SELECT * FROM trades WHERE {where} AND timestamp LIKE ? ORDER BY timestamp DESC LIMIT 1",
                [*params, f"{minute_prefix}%"],
            ).fetchone()
            if row:
                return row

        return self._conn.execute(
            f"SELECT * FROM trades WHERE {where} ORDER BY timestamp DESC LIMIT 1",
            params,
        ).fetchone()

    def _find_board_decision(self, post: dict):
        pipe_id = post.get("pipe_id") or ""
        bot_id = post.get("bot_id") or ""
        code = post.get("code") or ""
        if not bot_id:
            return None

        where = "pipe_id=? AND bot_id=?"
        params = [pipe_id, bot_id]
        if code:
            where += " AND stock_code=?"
            params.append(code)

        minute_prefix = (post.get("timestamp") or "")[:16]
        if minute_prefix:
            row = self._conn.execute(
                f"SELECT * FROM decisions WHERE {where} AND timestamp LIKE ? ORDER BY timestamp DESC LIMIT 1",
                [*params, f"{minute_prefix}%"],
            ).fetchone()
            return row

        return self._conn.execute(
            f"SELECT * FROM decisions WHERE {where} ORDER BY timestamp DESC LIMIT 1",
            params,
        ).fetchone()

    @staticmethod
    def _format_board_trade_content(trade: dict) -> str:
        side = trade.get("side", "")
        label = "매도" if side == "sell" else "매수" if side == "buy" else side
        code = trade.get("stock_code", "")
        qty = int(trade.get("quantity", 0) or 0)
        price = int(trade.get("price", 0) or 0)
        content = f"{label} {code} {qty}주 @{price:,}원".strip()

        realized = int(trade.get("realized_pnl", 0) or 0)
        if side == "sell" and realized:
            content += f" (손익 {realized:+,}원)"

        reason = trade.get("reason") or ""
        if " | 사유:" in reason:
            reason = reason.split(" | 사유:", 1)[1]
        elif reason.startswith(f"{trade.get('source', '')} {label}"):
            reason = ""
        if reason:
            content += f" {reason}"
        return content

    @staticmethod
    def _format_board_decision_content(decision: dict) -> str:
        action = (decision.get("action") or "").upper()
        summary = decision.get("output_summary") or ""
        if not summary:
            return ""

        raw_action = decision.get("action") or ""
        prefix = f"{raw_action}: "
        if raw_action and summary.lower().startswith(prefix.lower()):
            summary = summary[len(prefix):]
        return f"[{action}] {summary}" if action else summary

    def cleanup_board(self, days: int = 7):
        """오래된 게시판 글 삭제."""
        cutoff = (_kst_now() - timedelta(days=days)).isoformat()
        self._conn.execute("DELETE FROM board WHERE timestamp < ?", (cutoff,))
        self._conn.commit()

    # ── 거래 ──

    def log_trade(self, pipe_id: str, stock_code: str, stock_name: str,
                  side: str, quantity: int, price: int,
                  order_no: str = "", success: bool = True,
                  reason: str = "", source: str = "",
                  realized_pnl: int = 0, decision_id: int = 0) -> int:
        cur = self._conn.execute(
            """INSERT INTO trades (timestamp, pipe_id, stock_code, stock_name,
               side, quantity, price, amount, order_no, success, reason, source,
               realized_pnl, decision_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_kst_now().isoformat(), pipe_id, stock_code, stock_name,
             side, quantity, price, quantity * price, order_no, success, reason, source,
             realized_pnl, decision_id)
        )
        self._conn.commit()
        return cur.lastrowid

    def get_trades(self, pipe_id: str = "", start: str = "", end: str = "",
                   side: str = "", source: str = "", limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM trades WHERE 1=1"
        params = []
        if pipe_id:
            sql += " AND pipe_id = ?"
            params.append(pipe_id)
        if source:
            sql += " AND source = ?"
            params.append(source)
        if start:
            sql += " AND timestamp >= ?"
            params.append(start)
        if end:
            sql += " AND timestamp <= ?"
            params.append(end + "T23:59:59")
        if side:
            sql += " AND side = ?"
            params.append(side)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def trade_summary(self, pipe_id: str = "") -> dict:
        where = "WHERE pipe_id = ?" if pipe_id else "WHERE 1=1"
        params = [pipe_id] if pipe_id else []
        row = self._conn.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) as buys,
                SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) as sells,
                SUM(CASE WHEN side='buy' THEN amount ELSE 0 END) as buy_amount,
                SUM(CASE WHEN side='sell' THEN amount ELSE 0 END) as sell_amount,
                SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as successes
            FROM trades {where}
        """, params).fetchone()
        total = row["total"] or 0
        return {
            "total": total,
            "buys": row["buys"] or 0,
            "sells": row["sells"] or 0,
            "buy_amount": row["buy_amount"] or 0,
            "sell_amount": row["sell_amount"] or 0,
            "success_rate": (row["successes"] or 0) / total if total > 0 else 0,
        }

    # ── 스냅샷 ──

    def snapshot(self, pipe_id: str, cash: int, holdings_value: int,
                 total_value: int, realized_pnl: int = 0, unrealized_pnl: int = 0):
        self._conn.execute(
            """INSERT INTO snapshots (timestamp, pipe_id, cash, holdings_value,
               total_value, realized_pnl, unrealized_pnl) VALUES (?,?,?,?,?,?,?)""",
            (_kst_now().isoformat(), pipe_id, cash, holdings_value,
             total_value, realized_pnl, unrealized_pnl)
        )
        self._conn.commit()

    def get_snapshots(self, pipe_id: str = "", days: int = 7) -> list[dict]:
        from datetime import timedelta
        since = (_kst_now() - timedelta(days=days)).isoformat()
        sql = "SELECT * FROM snapshots WHERE timestamp >= ?"
        params = [since]
        if pipe_id:
            sql += " AND pipe_id = ?"
            params.append(pipe_id)
        sql += " ORDER BY timestamp"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ── 보유종목 ──

    def upsert_holding(self, pipe_id: str, stock_code: str, stock_name: str,
                       quantity: int, avg_price: int):
        if quantity <= 0:
            self._conn.execute(
                "DELETE FROM holdings WHERE pipe_id=? AND stock_code=?",
                (pipe_id, stock_code)
            )
        else:
            self._conn.execute(
                """INSERT INTO holdings (pipe_id, stock_code, stock_name, quantity, avg_price, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pipe_id, stock_code)
                   DO UPDATE SET quantity=?, avg_price=?, stock_name=?, updated_at=?""",
                (pipe_id, stock_code, stock_name, quantity, avg_price, _kst_now().isoformat(),
                 quantity, avg_price, stock_name, _kst_now().isoformat())
            )
        self._conn.commit()

    def get_holdings(self, pipe_id: str = "") -> list[dict]:
        if pipe_id:
            rows = self._conn.execute(
                "SELECT * FROM holdings WHERE pipe_id=? AND quantity>0", (pipe_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM holdings WHERE quantity>0").fetchall()
        return [dict(r) for r in rows]

    # ── 판단 이력 ──

    def log_decision(self, type: str, pipe_id: str = "", input_summary: str = "",
                     output_summary: str = "", action_taken: str = "",
                     bot_id: str = "", stock_code: str = "", action: str = "",
                     confidence: float = 0, indicators: dict | None = None,
                     trade_id: int = 0) -> int:
        """판단 기록 (구조화). indicators는 JSON dict로 저장."""
        indicators_json = json.dumps(indicators or {}, ensure_ascii=False)
        cur = self._conn.execute(
            """INSERT INTO decisions (timestamp, type, pipe_id, input_summary,
               output_summary, action_taken, bot_id, stock_code, action,
               confidence, indicators, trade_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_kst_now().isoformat(), type, pipe_id, input_summary,
             output_summary, action_taken,
             bot_id, stock_code, action, confidence, indicators_json, trade_id)
        )
        self._conn.commit()
        return cur.lastrowid

    def link_decision_trade(self, decision_id: int, trade_id: int):
        """판단 ↔ 거래 양방향 연결."""
        self._conn.execute("UPDATE decisions SET trade_id=? WHERE id=?", (trade_id, decision_id))
        self._conn.execute("UPDATE trades SET decision_id=? WHERE id=?", (decision_id, trade_id))
        self._conn.commit()

    def get_decisions(self, pipe_id: str = "", bot_id: str = "",
                      action: str = "", limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM decisions WHERE 1=1"
        params = []
        if pipe_id:
            sql += " AND pipe_id = ?"
            params.append(pipe_id)
        if bot_id:
            sql += " AND bot_id = ?"
            params.append(bot_id)
        if action:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_decision_outcomes(self, bot_id: str, days: int = 7) -> list[dict]:
        """봇의 매수 판단 → 매도 결과를 연결하여 조회 (자기 진화용).

        Returns: [{decision fields, realized_pnl, hold_minutes, ...}]
        """
        cutoff = (_kst_now() - timedelta(days=days)).isoformat()
        rows = self._conn.execute("""
            SELECT
                d.id as decision_id,
                d.timestamp as decision_time,
                d.stock_code,
                d.confidence,
                d.indicators,
                d.action,
                t_buy.price as buy_price,
                t_buy.quantity as buy_qty,
                t_buy.amount as buy_amount,
                t_sell.price as sell_price,
                t_sell.realized_pnl,
                t_sell.timestamp as sell_time,
                ROUND((julianday(t_sell.timestamp) - julianday(t_buy.timestamp)) * 1440) as hold_minutes
            FROM decisions d
            JOIN trades t_buy ON d.trade_id = t_buy.id AND t_buy.side = 'buy'
            LEFT JOIN trades t_sell ON t_sell.source = d.bot_id
                AND t_sell.stock_code = d.stock_code
                AND t_sell.side = 'sell'
                AND t_sell.timestamp > t_buy.timestamp
                AND t_sell.timestamp <= datetime(t_buy.timestamp, '+2 days')
            WHERE d.bot_id = ? AND d.action = 'buy' AND d.timestamp >= ?
            ORDER BY d.timestamp DESC
        """, (bot_id, cutoff)).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["indicators"] = json.loads(d.get("indicators", "{}"))
            except (json.JSONDecodeError, TypeError):
                d["indicators"] = {}
            results.append(d)
        return results

    def get_indicator_stats(self, bot_id: str, indicator_key: str,
                            days: int = 14) -> list[dict]:
        """특정 지표 값 범위별 승률 분석 (자기 진화용).

        Returns: [{range, total, wins, win_rate, avg_pnl}]
        """
        outcomes = self.get_decision_outcomes(bot_id, days)
        if not outcomes:
            return []

        # 지표별 그룹핑
        buckets: dict[str, list] = {}
        for o in outcomes:
            ind = o.get("indicators", {})
            val = ind.get(indicator_key)
            if val is None:
                continue
            # 0-3: low, 4-6: mid, 7-10: high (criteria 점수 기준)
            if isinstance(val, (int, float)):
                if val <= 3:
                    bucket = "low(0-3)"
                elif val <= 6:
                    bucket = "mid(4-6)"
                else:
                    bucket = "high(7-10)"
            else:
                bucket = str(val)
            buckets.setdefault(bucket, []).append(o)

        results = []
        for rng, items in sorted(buckets.items()):
            pnls = [i["realized_pnl"] or 0 for i in items if i.get("sell_price")]
            wins = sum(1 for p in pnls if p > 0)
            total = len(pnls)
            results.append({
                "range": rng,
                "total": total,
                "wins": wins,
                "win_rate": wins / total if total > 0 else 0,
                "avg_pnl": sum(pnls) / total if total > 0 else 0,
            })
        return results

    # ── 뉴스 ──

    def save_news(self, title: str, summary: str, url: str, source: str,
                  published: str, news_hash: str):
        self._conn.execute(
            """INSERT OR IGNORE INTO news (timestamp, title, summary, url, source, published, news_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (_kst_now().isoformat(), title, summary, url, source, published, news_hash)
        )
        self._conn.commit()

    def news_exists(self, news_hash: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM news WHERE news_hash=?", (news_hash,)).fetchone()
        return row is not None

    def get_news(self, count: int = 20, source: str = "") -> list[dict]:
        sql = "SELECT * FROM news"
        params = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        sql += " ORDER BY published_iso DESC, timestamp DESC LIMIT ?"
        params.append(count)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def search_news(self, keyword: str, count: int = 20) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM news WHERE title LIKE ? OR summary LIKE ? ORDER BY published_iso DESC, timestamp DESC LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", count)
        ).fetchall()]

    def save_news_enriched(self, title: str, summary: str, url: str, source: str,
                           published: str, news_hash: str,
                           category: str = "", tags: str = "", sentiment: str = "",
                           embedding: bytes | None = None,
                           published_iso: str = ""):
        """뉴스 저장 + 분류/태그/임베딩 포함."""
        if not published_iso and published:
            published_iso = normalize_published(published)
        self._conn.execute(
            """INSERT OR IGNORE INTO news
               (timestamp, title, summary, url, source, published, news_hash,
                category, tags, sentiment, embedding, published_iso)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_kst_now().isoformat(), title, summary, url, source, published,
             news_hash, category, tags, sentiment, embedding, published_iso)
        )
        self._conn.commit()

    def update_news_enrichment(self, news_id: int, category: str, tags: str,
                               sentiment: str, embedding: bytes | None):
        """기존 뉴스 분류/태그/임베딩 업데이트 (backfill용)."""
        self._conn.execute(
            "UPDATE news SET category=?, tags=?, sentiment=?, embedding=? WHERE id=?",
            (category, tags, sentiment, embedding, news_id)
        )
        self._conn.commit()

    def get_news_for_embedding(self, limit: int = 1000) -> list[dict]:
        """임베딩 없는 뉴스 조회 (backfill 대상)."""
        return [dict(r) for r in self._conn.execute(
            "SELECT id, title, summary, source FROM news WHERE embedding IS NULL LIMIT ?",
            (limit,)
        ).fetchall()]

    def get_all_embeddings(self) -> list[tuple[int, bytes]]:
        """모든 임베딩 벡터 로드 (시맨틱 검색용)."""
        return [(r[0], r[1]) for r in self._conn.execute(
            "SELECT id, embedding FROM news WHERE embedding IS NOT NULL"
        ).fetchall()]

    def get_news_by_ids(self, ids: list[int]) -> list[dict]:
        """ID 목록으로 뉴스 조회."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return [dict(r) for r in self._conn.execute(
            f"SELECT * FROM news WHERE id IN ({placeholders})", ids
        ).fetchall()]

    def get_news_filtered(self, category: str = "", source: str = "",
                          sentiment: str = "", count: int = 30,
                          offset: int = 0) -> list[dict]:
        """필터 기반 뉴스 조회 (UI용)."""
        sql = ("SELECT id, timestamp, title, summary, url, source, published, "
               "category, tags, sentiment FROM news WHERE 1=1")
        params = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if source:
            sql += " AND source = ?"
            params.append(source)
        if sentiment:
            sql += " AND sentiment = ?"
            params.append(sentiment)
        sql += " ORDER BY published_iso DESC, timestamp DESC LIMIT ? OFFSET ?"
        params.extend([count, offset])
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_news_stats(self) -> dict:
        """뉴스 통계 (카테고리별 카운트 + 보존 건수)."""
        cats = self._conn.execute(
            "SELECT category, COUNT(*) as cnt FROM news GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
        total = self._conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        kept = self._conn.execute("SELECT COUNT(*) FROM news WHERE kept=1").fetchone()[0]
        return {"total": total, "kept": kept, "by_category": [dict(r) for r in cats]}

    # ── 뉴스 파기/보존 ──

    def backfill_published_iso(self):
        """기존 뉴스의 published_iso 빈 값을 일괄 정규화."""
        rows = self._conn.execute(
            "SELECT id, published FROM news WHERE published_iso = '' AND published != ''"
        ).fetchall()
        for r in rows:
            iso = normalize_published(r["published"])
            if iso:
                self._conn.execute("UPDATE news SET published_iso=? WHERE id=?", (iso, r["id"]))
        if rows:
            self._conn.commit()
            logger.info("published_iso 백필 완료: %d건", len(rows))

    def mark_kept(self, news_id: int):
        """뉴스를 AI 보존 마킹."""
        self._conn.execute("UPDATE news SET kept=1 WHERE id=?", (news_id,))
        self._conn.commit()

    def purge_old_news(self, cutoff_iso: str) -> int:
        """cutoff보다 오래된 kept=0 뉴스 삭제. 삭제 건수 반환."""
        # published_iso 기준 삭제
        cur = self._conn.execute(
            "DELETE FROM news WHERE kept=0 AND published_iso != '' AND published_iso < ?",
            (cutoff_iso,)
        )
        # published_iso 없는 뉴스는 timestamp(수집시간) 기준 fallback
        cur2 = self._conn.execute(
            "DELETE FROM news WHERE kept=0 AND published_iso = '' AND timestamp < ?",
            (cutoff_iso,)
        )
        self._conn.commit()
        return cur.rowcount + cur2.rowcount

    def get_purge_candidates(self, cutoff_iso: str) -> list[dict]:
        """파기 대상 뉴스 조회 (보존 마킹 전 판정용)."""
        return [dict(r) for r in self._conn.execute(
            "SELECT id, sentiment FROM news WHERE kept=0 AND published_iso != '' AND published_iso < ?",
            (cutoff_iso,)
        ).fetchall()]

    # ── 봇 상태 영속화 ──

    def save_bot_state(self, bot_id: str, state: dict):
        """봇 상태 저장 (UPSERT)."""
        fixed_keys = {
            "holding_code", "holding_qty", "holding_price",
            "realized_pnl", "scan_count", "today_rotations", "rotation_date", "extra",
        }
        extra = dict(state.get("extra", {}) or {})
        for key, value in state.items():
            if key not in fixed_keys:
                extra[key] = value
        self._conn.execute(
            """INSERT INTO bot_state (bot_id, holding_code, holding_qty, holding_price,
               realized_pnl, scan_count, today_rotations, rotation_date, extra, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bot_id) DO UPDATE SET
               holding_code=?, holding_qty=?, holding_price=?,
               realized_pnl=?, scan_count=?, today_rotations=?, rotation_date=?,
               extra=?, updated_at=?""",
            (bot_id,
             state.get("holding_code", ""), state.get("holding_qty", 0), state.get("holding_price", 0),
             state.get("realized_pnl", 0), state.get("scan_count", 0),
             state.get("today_rotations", 0), state.get("rotation_date", ""),
             json.dumps(extra, ensure_ascii=False, default=str), _kst_now().isoformat(),
             # UPDATE
             state.get("holding_code", ""), state.get("holding_qty", 0), state.get("holding_price", 0),
             state.get("realized_pnl", 0), state.get("scan_count", 0),
             state.get("today_rotations", 0), state.get("rotation_date", ""),
             json.dumps(extra, ensure_ascii=False, default=str), _kst_now().isoformat(),
            )
        )
        self._conn.commit()

    def load_bot_state(self, bot_id: str) -> dict | None:
        """봇 상태 로드."""
        row = self._conn.execute("SELECT * FROM bot_state WHERE bot_id=?", (bot_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["extra"] = json.loads(d.get("extra", "{}"))
        except json.JSONDecodeError:
            d["extra"] = {}
        if isinstance(d["extra"], dict):
            for key, value in d["extra"].items():
                d.setdefault(key, value)
        return d

    # ── 산업 지수 ──

    def save_industry_snapshot(self, timestamp: str, sector: str, sub_sector: str,
                               score: float, sentiment_avg: float, news_count: int,
                               volume_signal: float, velocity: float, direction: str,
                               momentum_json: str = "{}", extra_json: str = "{}"):
        """산업 지수 스냅샷 1행 저장."""
        self._conn.execute(
            "INSERT INTO industry_index (timestamp, sector, sub_sector, score, "
            "sentiment_avg, news_count, volume_signal, velocity, direction, momentum, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (timestamp, sector, sub_sector, score, sentiment_avg, news_count,
             volume_signal, velocity, direction, momentum_json, extra_json)
        )
        self._conn.commit()

    def get_industry_snapshots(self, sector: str = "", sub_sector: str = "",
                               hours: int = 24) -> list[dict]:
        """산업 지수 이력 조회."""
        cutoff = (_kst_now() - timedelta(hours=hours)).isoformat()
        if sector:
            rows = self._conn.execute(
                "SELECT * FROM industry_index "
                "WHERE sector = ? AND sub_sector = ? AND timestamp >= ? "
                "ORDER BY timestamp ASC",
                (sector, sub_sector, cutoff)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM industry_index "
                "WHERE sub_sector = '' AND timestamp >= ? "
                "ORDER BY timestamp ASC",
                (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_snapshot(self, sector: str, sub_sector: str = "") -> dict | None:
        """가장 최근 스냅샷 조회."""
        row = self._conn.execute(
            "SELECT * FROM industry_index "
            "WHERE sector = ? AND sub_sector = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (sector, sub_sector)
        ).fetchone()
        return dict(row) if row else None

    def purge_old_industry_index(self, days: int = 7):
        """오래된 산업 지수 스냅샷 삭제."""
        cutoff = (_kst_now() - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM industry_index WHERE timestamp < ?", (cutoff,)
        )
        self._conn.commit()
        if cur.rowcount > 0:
            logger.info("산업 지수 스냅샷 %d건 삭제 (>%d일)", cur.rowcount, days)

    def close(self):
        self._conn.close()


# 싱글톤
_db: DB | None = None


def get_db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db
