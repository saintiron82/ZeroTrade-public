# Decision Audit DB Design

## Purpose

ZeroTrade bots need a durable audit trail for every decision, not only trade outcomes.
Each bot scan must preserve what was queried, what was checked, what the AI saw, what it answered, what the system executed or blocked, and how the outcome later performed.

The immediate goal is to support Mirinae 4 and future bot evaluation with evidence-based analysis:

- bot-level win rate, daily return, total profit, and profit per trade
- symbol-level success and failure patterns
- pre-trade evidence that led to BUY, SELL, HOLD, or block
- prompt and raw AI response replay
- post-trade outcome attribution back to the original decision

## Current Gaps

The existing system has useful pieces but they are not tied together as a full audit trail.

- `trades` records executions and realized PnL.
- `decisions` records structured decision summaries and indicators.
- `news` and `industry_index` store market context.
- bot JSONL logs keep scan-level details, but are not normalized or reliably linked to trades.
- `DecisionLog` still writes JSON to `logs/decisions.json`.

Main problems:

- prompt text is not stored in SQLite
- raw AI output is not stored in SQLite
- source market snapshots are not attached to a decision id
- preflight checks and block reasons are not normalized
- decision, trade, and later outcome are only partially linked
- old bot decisions can be deleted after seven days

## Design

Add a new audit layer beside the existing tables. Do not replace `trades` or `decisions`.
The new layer uses `run_id` as the top-level identifier for one bot scan.

### `decision_runs`

One row per bot scan or manual decision.

Fields:

- `id`
- `run_id` unique text id
- `timestamp`
- `pipe_id`
- `bot_id`
- `scan_count`
- `mode`: `paper`, `live`, `disabled`, or unknown
- `status`: `started`, `decided`, `blocked`, `ordered`, `failed`, `completed`
- `action`: normalized final action, for example `buy`, `sell`, `hold`, `skip`
- `stock_code`
- `decision_id`
- `trade_id`
- `error`

### `evidence_snapshots`

Stores all data the bot queried before judging.

Fields:

- `id`
- `run_id`
- `timestamp`
- `source`: `price`, `daily_chart`, `minute_chart`, `orderbook`, `investor_trend`, `news`, `industry_index`, `balance`, `board`, `signals`, `preflight_input`
- `stock_code`
- `payload_json`
- `summary`
- `success`
- `error`

Rules:

- Store raw JSON where available.
- Store a short `summary` for dashboard and quick inspection.
- Do not depend on string parsing for later analysis.

### `ai_audit_logs`

Stores the AI call attached to the run.

Fields:

- `id`
- `run_id`
- `timestamp`
- `bot_id`
- `provider`
- `model`
- `prompt`
- `raw_response`
- `parsed_json`
- `parse_success`
- `parse_error`
- `confidence`
- `criteria_json`
- `bull_summary`
- `bear_summary`
- `trader_reason`
- `tokens`
- `latency_ms`

Rules:

- Keep the full prompt and full raw response.
- Store parsed decision separately from raw response.
- Criteria must stay structured so later reports can group by score buckets.

### `preflight_checks`

Stores system-side checks before any order is sent.

Fields:

- `id`
- `run_id`
- `timestamp`
- `check_name`
- `stock_code`
- `passed`
- `severity`: `info`, `warn`, `block`
- `message`
- `payload_json`

Initial checks:

- trading mode
- market session
- cash and buyable quantity
- bot budget
- daily loss limit
- daily trade limit
- min hold time
- duplicate or own-ledger position rule
- symbol cooldown
- symbol blocklist
- target/stop validity

### `decision_outcomes`

Stores post-trade evaluation after sell or after a configured evaluation window.

Fields:

- `id`
- `run_id`
- `decision_id`
- `buy_trade_id`
- `sell_trade_id`
- `bot_id`
- `stock_code`
- `entry_time`
- `exit_time`
- `hold_minutes`
- `entry_price`
- `exit_price`
- `quantity`
- `realized_pnl`
- `return_pct`
- `max_favorable_pct`
- `max_adverse_pct`
- `outcome_status`: `open`, `closed`, `expired`, `unmatched`
- `notes`

Rules:

- This table is derived and can be refreshed.
- It must not overwrite `trades`; it links to them.
- It powers bot KPI reports.

## Data Flow

1. Bot starts scan and creates `decision_runs(status='started')`.
2. Every market/account/news/board query records an `evidence_snapshots` row.
3. Bot builds prompt from collected evidence.
4. AI call records `ai_audit_logs` with prompt, raw response, parsed JSON, criteria, and confidence.
5. Parsed decision is also logged to existing `decisions`.
6. System preflight checks are recorded to `preflight_checks`.
7. If order is blocked, `decision_runs.status='blocked'`.
8. If order is sent, existing `trades` row is created and linked back to the run.
9. When a position closes, `decision_outcomes` links entry decision to sell result.

## Integration Points

### DB module

Add migration methods to `src/db.py`:

- create the five audit tables
- add indexes on `run_id`, `bot_id`, `stock_code`, `timestamp`
- add helper methods:
  - `start_decision_run`
  - `update_decision_run`
  - `log_evidence_snapshot`
  - `log_ai_audit`
  - `log_preflight_check`
  - `upsert_decision_outcome`

### Recorder module

Add `src/bots/audit.py` with an `AuditRecorder`.

Responsibilities:

- generate stable `run_id`
- provide small methods bots can call without knowing SQL
- tolerate failures without breaking trading
- truncate only summaries, never raw JSON payloads

`run_id` format:

- `{bot_id}:{instance_or_pipe}:{timestamp}:{scan_count}:{short_uuid}`
- example: `mirinae-4:strat-001:20260603T132501:184:9f3a2c`
- `short_uuid` prevents collision when a process restarts during the same scan count.

### Mirinae flow

Update the shared Mirinae path first, not each bot separately.

Capture:

- `_collect_data()` payload as evidence
- prompt passed to AI
- raw AI response
- parsed decision
- validation and preflight results
- order result and trade link

### TradeEngine

Add optional `run_id` and `decision_id` inputs to buy/sell paths.

Use them to link:

- `decision_runs.trade_id`
- existing `trades.decision_id`
- `decision_outcomes`

## Reporting Requirements

The first report should answer, per bot:

- closed trades
- win rate
- daily PnL
- daily return on allocated budget
- total PnL
- average PnL per trade
- average return per trade
- profit factor
- best/worst symbol
- best/worst criteria bucket
- top block reasons

For Mirinae 4 readiness, reports must show:

- whether a symbol should be allowed, watched, or blocked
- which evidence types actually predicted profit
- whether AI confidence correlates with profit
- whether news sentiment or industry index improves entries

## Retention

Audit rows must not be deleted after seven days.

Initial retention:

- keep all audit rows indefinitely while capital is small
- allow later compaction into daily summaries
- do not run existing decision cleanup against audit tables
- do not join audit retention to `decisions` retention; audit rows remain valid even if old `decisions` rows are later compacted

## Safety

Audit logging must never place or cancel orders.
If audit writing fails, the bot may continue, but the failure must be logged.

No existing user data or trade history should be rewritten during migration.

## Testing

Unit tests:

- table migration is idempotent
- run creation and status update works
- evidence snapshots store valid JSON
- AI audit stores prompt, raw response, parsed JSON, and criteria
- preflight block is queryable by run
- trade linking updates `decision_runs`
- outcome derivation links buy/sell trades

Integration tests:

- fake Mirinae scan produces one run with evidence and AI audit
- blocked BUY records block checks and no trade id
- successful BUY links decision and trade
- successful SELL creates or updates outcome

## Implementation Order

1. Add DB tables and helper methods.
2. Add `AuditRecorder`.
3. Add unit tests for DB helpers.
4. Wire recorder into the shared Mirinae decision flow.
5. Pass `run_id` through execution and TradeEngine.
6. Add outcome derivation.
7. Add MCP/reporting helper for bot audit KPI.

## Out of Scope

- UI dashboard redesign
- historical backfill from all JSONL logs
- automatic Mirinae 4 implementation
- changing live trading rules
- deleting or rewriting existing `trades`, `decisions`, or bot logs
