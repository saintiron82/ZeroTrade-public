# Winner-1 자율 트레이딩 에이전트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 가이드 없이 AI 판단만으로 매매하는 자율 봇 winner-1을 구현한다 — 읽기 전용 도구 자율 탐색 + 런타임 도구 작업장 + 판단 JSON 집행.

**Architecture:** BotBase 직접 상속, `_loop` 전면 오버라이드(동적 스캔 주기). 매 스캔 agentcli Claude CLI 세션(`cwd=작업장`, `conversation_id="bot:winner-1"`)으로 자율 탐색 → 최종 판단 JSON을 봇이 파싱해 TradeEngine 경유 집행. 정보 접근은 읽기 전용 MCP(`FF_MCP_READONLY=1`) + 작업장 시드 스크립트(로컬 웹 API 조회 폴백). 스펙: `docs/superpowers/specs/2026-06-11-zero-autonomous-agent-design.md` (winner-1로 개명됨).

**Tech Stack:** Python 3.14, agentcli(claude provider, `--permission-mode bypassPermissions`, `--resume` 세션), FastMCP, 기존 FF BotBase/TradeEngine/AuditRecorder, pytest.

**작업 브랜치:** main에서 `feat/winner1-agent` 분기.
**테스트 실행:** `.venv/bin/python -m pytest ...` (실 AI 호출·실거래 절대 금지 — 전부 mock).

---

## File Structure

- Modify: `src/mcp_server.py` — `FF_MCP_READONLY=1`이면 쓰기성 도구 미등록
- Create: `src/bots/winner1.py` — Winner1Bot (BotBase 상속): 작업장 부트스트랩, 프롬프트 조립, 판단 파서, 동적 `_loop`, audit 연동
- Modify: `src/bots/registry.py` — BotInfo 등록 (mirinae-3 패턴, line 269-306 참고)
- Modify: `src/bots/factory.py` — bot_id 매핑 추가
- Test: `tests/test_mcp_readonly.py`
- Test: `tests/test_winner1_bot.py`

작업장(코드가 첫 가동 시 생성, repo 커밋 아님): `data/agent_workspace/winner-1/{tools/, journal.md, .mcp.json, .claude/settings.json}`

---

### Task 1: MCP 읽기 전용 모드

**Files:**
- Modify: `src/mcp_server.py`
- Test: `tests/test_mcp_readonly.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_mcp_readonly.py` 생성:

```python
"""FF_MCP_READONLY=1이면 쓰기성 MCP 도구가 등록되지 않는다."""

import importlib
import sys


WRITE_TOOLS = {
    "buy_stock", "sell_stock", "buy_us_stock", "sell_us_stock",
    "create_strategy", "update_pipe", "start_pipe", "stop_pipe",
    "link_bot", "set_bot_ratio", "set_total_budget",
    "create_bot", "design_bot", "save_bot_config", "run_evolver",
}
READ_TOOLS_SAMPLE = {"get_price", "get_balance", "get_daily_chart", "search_stock"}


def _load_mcp_tools(monkeypatch, readonly: str | None):
    if readonly is None:
        monkeypatch.delenv("FF_MCP_READONLY", raising=False)
    else:
        monkeypatch.setenv("FF_MCP_READONLY", readonly)
    sys.modules.pop("src.mcp_server", None)
    mod = importlib.import_module("src.mcp_server")
    # FastMCP 도구 목록 — 구현 시 실제 속성 확인 (mcp._tool_manager._tools 등)
    return set(mod.mcp._tool_manager._tools.keys()), mod


def test_readonly_mode_excludes_write_tools(monkeypatch):
    tools, _ = _load_mcp_tools(monkeypatch, "1")
    assert WRITE_TOOLS & tools == set(), f"readonly인데 등록됨: {WRITE_TOOLS & tools}"
    assert READ_TOOLS_SAMPLE <= tools


def test_default_mode_includes_write_tools(monkeypatch):
    tools, _ = _load_mcp_tools(monkeypatch, None)
    assert WRITE_TOOLS <= tools
    assert READ_TOOLS_SAMPLE <= tools
```

주의: `mcp._tool_manager._tools` 접근 경로는 FastMCP 버전에 따라 다르다 — 구현 전에 실제 객체를 inspect해서 테스트 헬퍼를 맞춰라 (async `mcp.list_tools()`도 가능). 모듈 재import 패턴은 `tests/test_ai_caller_bridge.py`의 sys.modules 조작 관례를 따른다.

- [ ] **Step 2: FAIL 확인**

Run: `.venv/bin/python -m pytest tests/test_mcp_readonly.py -v`
Expected: `test_readonly_mode_excludes_write_tools` FAIL (쓰기 도구가 등록되어 있음)

- [ ] **Step 3: 구현**

`src/mcp_server.py` 상단에 (기존 `os.getenv` 사용부 근처):

```python
# 읽기 전용 모드 — winner-1 등 자율 에이전트 세션용. 쓰기성 도구 미등록.
MCP_READONLY = os.getenv("FF_MCP_READONLY", "") == "1"
```

쓰기성 도구 15개(`buy_stock`, `sell_stock`, `buy_us_stock`, `sell_us_stock`, `create_strategy`, `update_pipe`, `start_pipe`, `stop_pipe`, `link_bot`, `set_bot_ratio`, `set_total_budget`, `create_bot`, `design_bot`, `save_bot_config`, `run_evolver`)의 **함수 정의 전체**를 `if not MCP_READONLY:` 블록으로 감싼다 (데코레이터가 정의 시점에 등록하므로 정의 자체를 조건화해야 함). 들여쓰기 일괄 변경 시 함수 본문 손상 주의 — 도구별로 확인.

- [ ] **Step 4: PASS 확인 + 전체 회귀**

Run: `.venv/bin/python -m pytest tests/test_mcp_readonly.py tests/ -q`
Expected: 신규 2개 포함 전체 PASS (베이스라인 292)

- [ ] **Step 5: 커밋**

```bash
git add src/mcp_server.py tests/test_mcp_readonly.py
git commit -m "feat(mcp): FF_MCP_READONLY mode excludes write tools"
```

---

### Task 2: Winner1Bot 골격 + 작업장 부트스트랩 + 등록

**Files:**
- Create: `src/bots/winner1.py`
- Modify: `src/bots/registry.py` (line 269-306의 mirinae-3 등록 블록 뒤)
- Modify: `src/bots/factory.py` (기본 봇 매핑부)
- Test: `tests/test_winner1_bot.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_winner1_bot.py` 생성:

```python
"""Winner1Bot 골격·작업장·파서 테스트. 실 AI 호출 없음."""

import json
from pathlib import Path

from src.bots.winner1 import Winner1Bot, DEFAULT_PARAMS


def make_bot(tmp_path, **params) -> Winner1Bot:
    merged = {**params, "workspace_root": str(tmp_path)}
    bot = Winner1Bot({"budget": 90000, "instance_id": "strat-001:winner-1",
                      "params": merged})
    return bot


def test_bot_identity_and_guards(tmp_path):
    bot = make_bot(tmp_path)
    assert bot.BOT_ID == "winner-1"
    assert bot.engine.budget == 90000
    # 일일 손실 한도 가드 opt-in (계좌 보호선)
    assert bot.engine._guard["daily_loss_limit_pct"] == -2.0


def test_workspace_bootstrap(tmp_path):
    bot = make_bot(tmp_path)
    ws = bot.workspace
    assert ws == Path(tmp_path) / "winner-1"
    assert (ws / "tools").is_dir()
    assert (ws / "journal.md").exists()
    mcp_cfg = json.loads((ws / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp_cfg["mcpServers"]["ff-trading-readonly"]
    assert server["env"]["FF_MCP_READONLY"] == "1"
    settings = json.loads((ws / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["enableAllProjectMcpServers"] is True


def test_workspace_bootstrap_idempotent_preserves_agent_files(tmp_path):
    bot = make_bot(tmp_path)
    journal = bot.workspace / "journal.md"
    journal.write_text("내 전략 메모", encoding="utf-8")
    tool = bot.workspace / "tools" / "screener.py"
    tool.write_text("# my tool", encoding="utf-8")

    bot2 = make_bot(tmp_path)  # 재부트스트랩
    assert journal.read_text(encoding="utf-8") == "내 전략 메모"
    assert tool.read_text(encoding="utf-8") == "# my tool"
```

- [ ] **Step 2: FAIL 확인**

Run: `.venv/bin/python -m pytest tests/test_winner1_bot.py -v`
Expected: `ModuleNotFoundError: src.bots.winner1`

- [ ] **Step 3: 구현 — src/bots/winner1.py 골격**

```python
"""Winner-1 — 완전 자율 트레이딩 에이전트.

룰·전략 지시 없음. 매 스캔 Claude CLI 세션이 읽기 전용 도구로 자율 탐색하고
작업장에서 자기 도구를 만들어 쓰며, 최종 판단 JSON만 반환한다.
주문 집행은 봇이 TradeEngine 경유로 수행한다 (하드 가드 적용).
스펙: docs/superpowers/specs/2026-06-11-zero-autonomous-agent-design.md
"""

import json
import logging
from pathlib import Path

from src.config import PROJECT_ROOT
from src.bots.base import BotBase, BotConfig

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


class Winner1Bot(BotBase):
    BOT_ID = "winner-1"
    BOT_NAME = "Winner 1호"

    def __init__(self, config: BotConfig | dict = None):
        cfg = config if isinstance(config, dict) else (config.__dict__ if config else {})
        params = {**DEFAULT_PARAMS, **(cfg.get("params") or {})}
        self._params = params
        # 계좌 보호선만 opt-in — 전략성 가드는 전부 미사용 (스펙: 자율 경계)
        self._guard_config = {"daily_loss_limit_pct": -2.0}  # 엔진 부호 규약: 음수=손실 한도
        super().__init__(config)
        self.workspace = self._bootstrap_workspace()
        self._next_scan_minutes = self._params["min_scan_minutes"]

    # ── 작업장 ──

    def _bootstrap_workspace(self) -> Path:
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
```

(scan/decide/execute는 BotBase 추상이므로 일단 최소 구현으로 채운다 — Task 4-5에서 세션 사이클이 대체:)

```python
    # BotBase 추상 메서드 — winner-1은 _loop를 직접 오버라이드하므로 형식상 구현
    def scan(self) -> list[dict]:
        return []

    def decide(self, candidates: list[dict]) -> dict:
        return {"action": "hold", "reason": "loop override"}

    def execute(self, decision: dict) -> dict:
        return {}
```

`__init__`의 BotConfig/dict 처리·`_guard_config` 주입 순서는 `src/bots/stealth_volume.py`의 `__init__` 패턴을 먼저 읽고 동일하게 맞춰라 (super().__init__ 전에 _guard_config 설정 필수, base.py:64-69가 engine에 전달).

- [ ] **Step 4: 레지스트리·팩토리 등록**

`src/bots/registry.py` — mirinae-3 블록(line 269-306) 바로 뒤에 동일 패턴으로:

```python
register(BotInfo(
    id="winner-1",
    name="Winner 1호",
    version="1.0.0",
    category=BotCategory.CUSTOM,
    bot_type=BotType.POLLING,
    market_session=MarketSession.REGULAR,
    description="완전 자율 AI 에이전트 — 룰 없이 도구 탐색·자작 도구로 판단",
    strategy="매 스캔 Claude CLI 세션이 읽기 전용 도구로 시장을 자율 탐색하고, "
             "작업장에 자기 도구를 만들어 축적하며 진입·청산·주기를 전부 스스로 결정. "
             "시스템 개입은 예산·일일손실 한도·파생ETF 토글뿐.",
    risk_level=5,
    params=[
        BotParam("session_timeout_sec", "세션 타임아웃(초)", 600, 120, 1800),
        BotParam("min_scan_minutes", "최소 스캔 간격(분)", 5, 5, 60),
        BotParam("max_scan_minutes", "최대 스캔 간격(분)", 240, 30, 480),
    ],
    tags=["autonomous", "ai", "experimental"],
    module="src.bots.winner1.Winner1Bot",
    ui=BotUISpec(...),  # mirinae-3의 ui 블록을 참고해 상태배지/스캔수/포지션/판단로그 위젯 구성
))
```

`BotParam`/`BotUISpec` 실제 시그니처는 registry.py 상단 정의를 읽고 맞춰라. `src/bots/factory.py`의 기본 봇 매핑(`_match` 분기)에 `winner` 키워드 → Winner1Bot 생성 추가 (mirinae-3 분기 복제).

- [ ] **Step 5: PASS 확인**

Run: `.venv/bin/python -m pytest tests/test_winner1_bot.py -v`
Expected: 3 passed. 추가 확인: `.venv/bin/python -c "from src.bots.factory import create_bot; b=create_bot('winner-1', budget=90000, instance_id='t:winner-1'); print(b.BOT_ID)"` → `winner-1`

- [ ] **Step 6: 커밋**

```bash
git add src/bots/winner1.py src/bots/registry.py src/bots/factory.py tests/test_winner1_bot.py
git commit -m "feat(winner1): bot skeleton, workspace bootstrap, registry"
```

---

### Task 3: 판단 파서 + 클램프 (순수 함수)

**Files:**
- Modify: `src/bots/winner1.py`
- Test: `tests/test_winner1_bot.py`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
from src.bots.winner1 import parse_decision, clamp_next_scan


def test_parse_decision_valid():
    text = '분석 결과... 최종 판단:\n```json\n{"action":"buy","code":"005930","qty":2,"reason":"수급 개선","next_scan_minutes":45}\n```'
    d = parse_decision(text)
    assert d == {"action": "buy", "code": "005930", "qty": 2,
                 "reason": "수급 개선", "next_scan_minutes": 45}


def test_parse_decision_bad_inputs_fall_back_to_hold():
    for text in ("", "json 없음", '{"action":"explode"}',
                 '{"action":"buy"}',                      # code/qty 누락
                 '{"action":"buy","code":"12345","qty":1}',   # 코드 5자리
                 '{"action":"buy","code":"005930","qty":0}'):  # qty 0
        d = parse_decision(text)
        assert d["action"] == "hold"
        assert "parse" in d.get("error", "") or d.get("error")


def test_parse_decision_hold_needs_no_code():
    d = parse_decision('{"action":"hold","reason":"관망","next_scan_minutes":10}')
    assert d["action"] == "hold"
    assert d["next_scan_minutes"] == 10


def test_clamp_next_scan():
    assert clamp_next_scan(45, 5, 240) == 45
    assert clamp_next_scan(1, 5, 240) == 5
    assert clamp_next_scan(999, 5, 240) == 240
    assert clamp_next_scan(None, 5, 240) == 5
    assert clamp_next_scan(-3, 5, 240) == 5
    assert clamp_next_scan("30", 5, 240) == 30
```

- [ ] **Step 2: FAIL 확인** — `ImportError: parse_decision`

- [ ] **Step 3: 구현 (winner1.py 모듈 레벨 함수)**

```python
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
    # 마지막 { ... } 블록 추출 (멀티턴 출력의 끝에 판단 JSON이 오는 관례)
    start = text.rfind("{")
    while start != -1:
        chunk = text[start:]
        end = chunk.rfind("}")
        if end == -1:
            return fail("JSON 미발견")
        try:
            data = json.loads(chunk[:end + 1])
            break
        except json.JSONDecodeError:
            start = text.rfind("{", 0, start)
    else:
        return fail("JSON 미발견")

    action = str(data.get("action", "")).lower()
    if action not in VALID_ACTIONS:
        return fail(f"알 수 없는 action: {action!r}")

    code = str(data.get("code", "") or "")
    qty = data.get("qty", 0)
    try:
        qty = int(qty or 0)
    except (TypeError, ValueError):
        return fail(f"qty 불량: {qty!r}")
    if action in ("buy", "sell"):
        if not (code.isdigit() and len(code) == 6):
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
```

주의: `rfind` 후보가 중첩 JSON이면 첫 시도가 실패할 수 있어 역방향으로 `{`를 옮겨가며 재시도하는 위 구조를 유지하라.

- [ ] **Step 4: PASS 확인** — 신규 4개 포함 파일 전체 PASS

- [ ] **Step 5: 커밋** — `feat(winner1): decision parser and scan clamp`

---

### Task 4: 프롬프트 조립 + 세션 호출

**Files:**
- Modify: `src/bots/winner1.py`
- Test: `tests/test_winner1_bot.py`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
from unittest.mock import patch


def test_build_prompt_contains_physical_facts_only(tmp_path):
    bot = make_bot(tmp_path)
    prompt = bot.build_prompt(cash=90000, positions=[
        {"code": "005930", "qty": 2, "avg_price": 71000}])
    # 물리적 사실
    assert "90,000" in prompt or "90000" in prompt
    assert "005930" in prompt
    assert "next_scan_minutes" in prompt          # 출력 스키마
    assert "인버스" in prompt                       # 파생ETF 제약 고지
    # 전략 지시 금지 — 대표 금칙어가 들어있지 않아야 한다
    for banned in ("RSI", "SMA", "손절", "트레일링", "추천 종목", "월스트리트"):
        assert banned not in prompt, f"전략 지시 누출: {banned}"


def test_run_session_returns_parsed_decision(tmp_path):
    bot = make_bot(tmp_path)
    fake = type("R", (), {"content": '{"action":"hold","reason":"관망","next_scan_minutes":30}',
                          "provider": "claude", "model": "", "tokens": 1234,
                          "latency_ms": 5000, "error": "", "error_type": ""})()
    with patch("src.bots.winner1.invoke_ai_result", return_value=fake) as call:
        decision, meta = bot.run_session(cash=90000, positions=[])
    assert decision["action"] == "hold"
    assert meta["tokens"] == 1234
    kwargs = call.call_args.kwargs
    assert kwargs["cwd"] == str(bot.workspace)
    assert kwargs["conversation_id"] == "bot:winner-1"
    assert kwargs["timeout"] == bot._params["session_timeout_sec"]


def test_run_session_error_falls_back_to_hold(tmp_path):
    bot = make_bot(tmp_path)
    fake = type("R", (), {"content": "", "provider": "claude", "model": "",
                          "tokens": 0, "latency_ms": 0,
                          "error": "timeout", "error_type": "TimeoutError"})()
    with patch("src.bots.winner1.invoke_ai_result", return_value=fake):
        decision, meta = bot.run_session(cash=90000, positions=[])
    assert decision["action"] == "hold"
    assert meta["error"] == "timeout"
```

- [ ] **Step 2: FAIL 확인** — `AttributeError: build_prompt`

- [ ] **Step 3: 구현**

```python
from src.bots.ai_caller import invoke_ai_result
```

```python
    # ── 세션 ──

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
```

- [ ] **Step 4: PASS 확인**

- [ ] **Step 5: 스파이크 — 실 CLI에서 MCP 도구 동작 검증 (수동 1회, 커밋 없음)**

```bash
cd data/agent_workspace 2>/dev/null || true
.venv/bin/python -c "
from src.bots.winner1 import Winner1Bot
bot = Winner1Bot({'budget': 1000, 'instance_id': 'spike:winner-1'})
print(bot.workspace)"
# 생성된 작업장에서:
cd data/agent_workspace/winner-1 && claude -p "ff-trading-readonly MCP의 get_price 도구로 005930 현재가를 조회해서 숫자만 답해" --permission-mode bypassPermissions --output-format json 2>&1 | tail -3
```

Expected: 삼성전자 현재가 숫자 응답. **실패 시(MCP 승인 문제 등)**: (a) `claude` 실행에 `--mcp-config .mcp.json` 플래그가 필요한지 확인 — 필요하면 agentcli claude provider가 cwd의 .mcp.json을 자동 인식하는지 점검 후, 안 되면 `~/Projects/agentcli` claude.py에 mcp-config 전달 옵션 추가(별도 커밋, FF 테스트와 무관). (b) 그래도 안 되면 작업장 `tools/ff_api.py` 시드 스크립트(로컬 `http://localhost:8080/api/*` 읽기 호출 래퍼)를 부트스트랩에 추가하고 프롬프트의 MCP 안내를 스크립트 안내로 교체 — 동등 기능 폴백. 결과를 보고에 명시.

- [ ] **Step 6: 커밋** — `feat(winner1): prompt assembly and CLI session`

---

### Task 5: 동적 루프 + 집행 + audit 연동

**Files:**
- Modify: `src/bots/winner1.py`
- Test: `tests/test_winner1_bot.py`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
import src.db as db_module
from src.db import DB


def test_scan_cycle_buy_executes_and_audits(tmp_path, monkeypatch):
    db = DB(tmp_path / "audit.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    bot = make_bot(tmp_path)
    bot._scan_count = 1

    decision = {"action": "buy", "code": "005930", "qty": 1,
                "reason": "테스트", "next_scan_minutes": 60}
    meta = {"prompt": "p", "raw_response": "r", "provider": "claude",
            "model": "m", "tokens": 10, "latency_ms": 100, "error": ""}
    with (
        patch.object(bot, "run_session", return_value=(decision, meta)),
        patch.object(bot.engine, "buy",
                     return_value={"success": True, "trade_id": 7}) as buy,
    ):
        bot.run_scan_cycle()

    buy.assert_called_once_with("005930", 1, reason="테스트")
    assert bot._next_scan_minutes == 60

    runs = db._conn.execute("SELECT * FROM decision_runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["bot_id"] == "winner-1"
    assert runs[0]["status"] == "ordered"
    ai = db.get_ai_audit(runs[0]["run_id"])
    assert ai["prompt"] == "p"
    assert ai["raw_response"] == "r"


def test_scan_cycle_hold_and_clamp(tmp_path, monkeypatch):
    db = DB(tmp_path / "audit.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    bot = make_bot(tmp_path)
    decision = {"action": "hold", "code": "", "qty": 0,
                "reason": "관망", "next_scan_minutes": 999}
    meta = {"prompt": "p", "raw_response": "r", "provider": "claude",
            "model": "m", "tokens": 1, "latency_ms": 1, "error": ""}
    with patch.object(bot, "run_session", return_value=(decision, meta)):
        bot.run_scan_cycle()
    assert bot._next_scan_minutes == 240  # max 클램프
    run = db._conn.execute("SELECT * FROM decision_runs").fetchone()
    assert run["status"] == "completed"
    assert run["action"] == "hold"


def test_scan_cycle_dry_run_skips_orders(tmp_path, monkeypatch):
    db = DB(tmp_path / "audit.db")
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    bot = make_bot(tmp_path, dry_run=True)
    decision = {"action": "buy", "code": "005930", "qty": 1,
                "reason": "테스트", "next_scan_minutes": 30}
    meta = {"prompt": "p", "raw_response": "r", "provider": "claude",
            "model": "m", "tokens": 1, "latency_ms": 1, "error": ""}
    with (
        patch.object(bot, "run_session", return_value=(decision, meta)),
        patch.object(bot.engine, "buy") as buy,
    ):
        bot.run_scan_cycle()
    buy.assert_not_called()
    run = db._conn.execute("SELECT * FROM decision_runs").fetchone()
    assert run["status"] == "completed"  # dry-run은 주문 없음
```

주의: 테스트 모듈 상단에서 `src.db`를 import하지 말 것 — 호출 시점 lazy 패턴(`tests/test_stealth_volume_audit.py`의 make_db 주석 참고). cash/positions 조회는 run_scan_cycle 내부에서 engine/ledger를 사용하므로 테스트에서 적절히 mock (`patch.object(bot, "_current_state", return_value=(90000, []))` 형태로 분리 구현 권장).

- [ ] **Step 2: FAIL 확인** — `AttributeError: run_scan_cycle`

- [ ] **Step 3: 구현**

```python
    def _current_state(self) -> tuple[int, list[dict]]:
        """봇 가상 계좌 현금·포지션 (BotLedger 기준)."""
        try:
            from src.db import get_db
            from src.bots.ledger import BotLedger
            trades = [t for t in get_db().get_trades(limit=3000)
                      if t.get("source") == self.BOT_ID and t.get("success")]
            trades.sort(key=lambda t: t["timestamp"])
            led = BotLedger.from_trades(self._config.budget, trades)
            return led.cash, led.positions
        except Exception as exc:
            logger.warning("[%s] 상태 조회 실패: %s", self.BOT_ID, exc)
            return self._config.budget, []

    def run_scan_cycle(self):
        """한 번의 자율 판단 사이클 — 세션 → 파싱 → 집행 → audit."""
        recorder = self._audit_start_run()
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

        # 집행
        action = decision["action"]
        result = {}
        if action in ("buy", "sell") and not self._params.get("dry_run"):
            if action == "buy":
                result = self.engine.buy(decision["code"], decision["qty"],
                                         reason=decision["reason"]) or {}
            else:
                result = self.engine.sell(decision["code"], decision["qty"],
                                          reason=decision["reason"]) or {}

        # next_scan 반영
        self._next_scan_minutes = clamp_next_scan(
            decision.get("next_scan_minutes"),
            int(self._params["min_scan_minutes"]),
            int(self._params["max_scan_minutes"]))

        # 증거: 판단 JSON + 작업장 변경분 + next_scan
        if recorder is not None:
            recorder.evidence(
                "decision",
                payload={"decision": decision,
                         "workspace_changes": self._workspace_diff(ws_before),
                         "next_scan_minutes": self._next_scan_minutes,
                         "dry_run": bool(self._params.get("dry_run"))},
                stock_code=decision.get("code", ""),
                summary=f"{action} {decision.get('code','')} → next {self._next_scan_minutes}분",
            )
            if action in ("buy", "sell") and not self._params.get("dry_run"):
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
                            trade_id=int(result.get("trade_id") or 0),
                            error=decision.get("error", ""))

        self._save_state()
        return decision

    # ── 작업장 변경 추적 ──

    def _workspace_snapshot(self) -> dict:
        return {str(p.relative_to(self.workspace)): p.stat().st_mtime
                for p in self.workspace.rglob("*") if p.is_file()}

    def _workspace_diff(self, before: dict) -> list[str]:
        after = self._workspace_snapshot()
        return sorted([k for k in after if after[k] != before.get(k)])
```

engine.buy/sell의 실제 시그니처·반환 dict 키(`success`, `message`, trade_id 유무)는 `src/bots/engine.py:137-233`을 먼저 읽고 맞춰라 — 시그니처가 다르면 **엔진이 진실**이고 테스트를 거기 맞춘다. `_loop` 오버라이드:

```python
    async def _loop(self):
        import asyncio
        from src.bots.safeguards import MarketCloseGuard
        guard = MarketCloseGuard()
        logger.info("[%s] 자율 루프 시작 — 예산 %s원", self.BOT_ID, f"{self._config.budget:,}")
        while self._running:
            try:
                if not guard.is_market_open():
                    await asyncio.sleep(300)
                    continue
                self._scan_count += 1
                self.run_scan_cycle()
            except Exception as exc:
                logger.error("[%s] 사이클 오류: %s", self.BOT_ID, exc, exc_info=True)
            await asyncio.sleep(self._next_scan_minutes * 60)
```

`MarketCloseGuard.is_market_open` 존재·시그니처는 safeguards.py에서 확인 (stealth_volume._loop가 쓰는 것과 동일 패턴).

- [ ] **Step 4: PASS + 전체 회귀**

Run: `.venv/bin/python -m pytest tests/test_winner1_bot.py tests/ -q`
Expected: 전체 PASS (베이스라인 292 + 신규 ~13)

- [ ] **Step 5: 커밋** — `feat(winner1): autonomous scan cycle with audit and dynamic interval`

---

### Task 6: dry-run 통합 검증 + 가동 준비

**Files:** 없음 (검증·운영 절차)

- [ ] **Step 1: 전체 테스트** — `.venv/bin/python -m pytest tests/ -q` 전체 PASS

- [ ] **Step 2: dry-run 실세션 1회** (실주문 없음, 실 AI 호출 1회 발생)

```bash
.venv/bin/python -c "
from src.bots.winner1 import Winner1Bot
bot = Winner1Bot({'budget': 90000, 'instance_id': 'dryrun:winner-1',
                  'params': {'dry_run': True, 'session_timeout_sec': 600}})
bot._scan_count = 1
d = bot.run_scan_cycle()
print('판단:', d)
print('다음 스캔:', bot._next_scan_minutes, '분')"
```

Expected: 에이전트가 도구 탐색 후 hold/buy 판단 JSON 반환, decision_runs에 기록, 주문 없음. 작업장에 journal/도구 생성 여부 관찰. 실패 시 Task 4 Step 5의 폴백 적용.

- [ ] **Step 3: 머지·가동 (컨트롤러/사용자 게이트)**

main 머지 후: `link_bot(strat-001, winner-1, 0.3)` → **사용자 확인** → 파이프 재시작. (이 단계는 구현 에이전트가 하지 않는다 — 보고만.)

---

## Self-Review 메모

- 스펙 커버: 읽기전용 MCP(T1), 작업장+journal+자작도구(T2,T4), 전략지시 0 프롬프트(T4 금칙어 테스트), 판단 JSON+클램프(T3), 하드가드·dry-run·audit(T5,T6), 동적 주기(T5) — 전부 매핑됨
- MCP headless 승인 리스크: T4 Step 5 스파이크 + 이중 폴백 명시
- 실 AI 호출은 T4 Step 5(수동 스파이크)와 T6 Step 2(dry-run)에서만
