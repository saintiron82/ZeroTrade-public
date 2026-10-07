# 봇 분석 주기 조절 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 웹 대시보드에서 파이프 인스턴스별로 봇의 분석 주기(`interval_seconds`)를 조절하고, 실행 중인 봇에 다음 사이클부터 반영한다.

**Architecture:** 접근법 A. 웹이 `strategies.json`의 `bots[].params.interval_seconds`에 저장하고, 기존 Redis 명령 채널로 파이프 프로세스에 `set_bot_interval` 명령을 발행한다. `PipeRunner._on_command`가 해당 봇 인스턴스의 `_config.interval_seconds`를 라이브 갱신한다. 봇 공통 루프는 수정하지 않는다(현재 sleep 종료 후 다음 사이클부터 새 주기 적용).

**Tech Stack:** Python, FastAPI, Redis Pub/Sub, pytest, Vanilla JS(pipes.html).

---

## 파일 구조

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/bots/registry.py` | 봇 메타데이터. `interval_seconds` 범위/기본값의 단일 진실원천 | `interval_spec(bot_id)` 추가 |
| `src/strategy.py` | `strategies.json` 영속화 | `set_bot_interval()` 추가 |
| `src/services/pipe_runner.py` | 파이프 프로세스, 봇 인스턴스 보유, Redis 명령 수신 | `_on_command`에 `set_bot_interval` 분기 |
| `src/web/routes.py` | HTTP API | `PUT /pipes/{id}/bot-interval` + `bot-status` 응답에 interval 필드 |
| `src/web/static/pipes.html` | 봇 행 UI | 주기 입력 + `commitInterval()` |
| `tests/test_*.py` | 단위 테스트 | 신규 테스트 |

---

## Task 1: registry.interval_spec() — 주기 범위/기본값 조회

`interval_seconds` BotParam이 등록된 봇만 `{min, max, default}`를 반환하고, 미등록(`analyst`)이나 BotBase 비상속(`evolver`)은 `None`을 반환한다. 이 함수가 라우트 검증과 PipeRunner 클램프의 공통 소스다.

**Files:**
- Modify: `src/bots/registry.py` (파일 끝의 함수 영역, `def get(bot_id: str)` 근처)
- Test: `tests/bots/test_registry_interval.py` (Create)

- [ ] **Step 1: Write the failing test**

Create `tests/bots/test_registry_interval.py`:

```python
"""interval_spec — 봇별 분석 주기 범위/기본값 조회."""

from src.bots import registry


def test_interval_spec_for_registered_bot():
    spec = registry.interval_spec("mirinae-2")
    assert spec == {"min": 300, "max": 1800, "default": 600}


def test_interval_spec_for_stealth_volume():
    spec = registry.interval_spec("stealth-volume")
    assert spec == {"min": 10, "max": 300, "default": 30}


def test_interval_spec_none_for_unregistered_interval_bot():
    # analyst: interval_seconds BotParam 미등록 → None
    assert registry.interval_spec("analyst") is None


def test_interval_spec_none_for_unknown_bot():
    assert registry.interval_spec("does-not-exist") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/bots/test_registry_interval.py -v`
Expected: FAIL — `AttributeError: module 'src.bots.registry' has no attribute 'interval_spec'`

- [ ] **Step 3: Implement `interval_spec`**

In `src/bots/registry.py`, add after the `get` function:

```python
def interval_spec(bot_id: str) -> dict | None:
    """봇의 interval_seconds 범위/기본값. 미등록 봇/파라미터면 None.

    라우트 검증과 PipeRunner 클램프의 단일 진실원천.
    """
    info = get(bot_id)
    if not info:
        return None
    for p in info.params:
        if p.name == "interval_seconds":
            return {"min": p.min_value, "max": p.max_value, "default": p.default}
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/bots/test_registry_interval.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/bots/registry.py tests/bots/test_registry_interval.py
git commit -m "feat(registry): interval_spec lookup for bot scan interval bounds"
```

---

## Task 2: StrategyManager.set_bot_interval() — strategies.json 영속화

`bots[].params.interval_seconds`만 기록하고 `ratio`와 다른 봇 항목은 건드리지 않는다. 봇을 찾으면 `True`, 못 찾으면 `False`를 반환한다.

**Files:**
- Modify: `src/strategy.py` (`set_bot_ratio_rebalance` 메서드 근처)
- Test: `tests/test_strategy.py` (기존 파일에 추가)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_strategy.py`:

```python
def test_set_bot_interval_writes_params_only(tmp_path):
    """주기 변경은 params.interval_seconds만 기록하고 ratio/다른 봇은 불변."""
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"):
        from src.strategy import StrategyManager

        mgr = StrategyManager()
        mgr.set_total_budget(1_000_000)
        s = mgr.create_strategy("테스트", budget=500_000)
        s.bots = [
            {"bot_id": "mirinae-2", "ratio": 0.6, "params": {}},
            {"bot_id": "stealth-volume", "ratio": 0.4, "params": {}},
        ]
        mgr._save()

        found = mgr.set_bot_interval(s.id, "mirinae-2", 900)
        assert found is True

        reloaded = mgr.get(s.id)
        m2 = next(b for b in reloaded.bots if b["bot_id"] == "mirinae-2")
        sv = next(b for b in reloaded.bots if b["bot_id"] == "stealth-volume")
        assert m2["params"]["interval_seconds"] == 900
        assert m2["ratio"] == 0.6          # ratio 불변
        assert sv["params"] == {}          # 다른 봇 불변


def test_set_bot_interval_missing_bot_returns_false(tmp_path):
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"):
        from src.strategy import StrategyManager

        mgr = StrategyManager()
        mgr.set_total_budget(1_000_000)
        s = mgr.create_strategy("테스트", budget=500_000)
        s.bots = [{"bot_id": "mirinae-2", "ratio": 1.0, "params": {}}]
        mgr._save()

        assert mgr.set_bot_interval(s.id, "not-linked", 900) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy.py::test_set_bot_interval_writes_params_only tests/test_strategy.py::test_set_bot_interval_missing_bot_returns_false -v`
Expected: FAIL — `AttributeError: 'StrategyManager' object has no attribute 'set_bot_interval'`

- [ ] **Step 3: Implement `set_bot_interval`**

In `src/strategy.py`, add immediately after `set_bot_ratio_rebalance`:

```python
def set_bot_interval(self, strategy_id: str, bot_id: str, seconds: int) -> bool:
    """봇 인스턴스의 분석 주기를 params.interval_seconds에 기록.

    ratio·다른 봇 항목은 건드리지 않는다. 봇을 찾으면 True, 없으면 False.
    """
    s = self.get(strategy_id)
    if not s or not s.bots:
        return False
    for b in s.bots:
        if b.get("bot_id") == bot_id:
            params = b.setdefault("params", {})
            params["interval_seconds"] = int(seconds)
            self._save()
            return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_strategy.py::test_set_bot_interval_writes_params_only tests/test_strategy.py::test_set_bot_interval_missing_bot_returns_false -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/strategy.py tests/test_strategy.py
git commit -m "feat(strategy): set_bot_interval persists per-instance scan interval"
```

---

## Task 3: PipeRunner._on_command — 실행 중 봇에 라이브 반영

`set_bot_interval` 명령을 받으면 `self.bots` 중 `BOT_ID`가 일치하는 봇의 `_config.interval_seconds`와 `_params["interval_seconds"]`를 갱신한다. registry 범위로 방어적 클램프한다.

**Files:**
- Modify: `src/services/pipe_runner.py` (`_on_command` 메서드)
- Test: `tests/test_pipe_runner.py` (기존 파일에 추가)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pipe_runner.py`:

```python
def test_on_command_set_bot_interval_updates_matching_bot():
    runner = _runner()
    runner.pipe_id = "strat-001"

    bot_a = SimpleNamespace(
        BOT_ID="mirinae-2",
        _config=SimpleNamespace(interval_seconds=600),
        _params={"interval_seconds": 600},
    )
    bot_b = SimpleNamespace(
        BOT_ID="stealth-volume",
        _config=SimpleNamespace(interval_seconds=30),
        _params={"interval_seconds": 30},
    )
    runner.bots = [bot_a, bot_b]

    with patch(
        "src.bots.registry.interval_spec",
        return_value={"min": 300, "max": 1800, "default": 600},
    ):
        runner._on_command({
            "service_id": "strat-001",
            "action": "set_bot_interval",
            "config": {"bot_id": "mirinae-2", "interval_seconds": 900},
        })

    assert bot_a._config.interval_seconds == 900
    assert bot_a._params["interval_seconds"] == 900
    assert bot_b._config.interval_seconds == 30  # 다른 봇 불변


def test_on_command_set_bot_interval_clamps_out_of_range():
    runner = _runner()
    runner.pipe_id = "strat-001"
    bot_a = SimpleNamespace(
        BOT_ID="mirinae-2",
        _config=SimpleNamespace(interval_seconds=600),
        _params={"interval_seconds": 600},
    )
    runner.bots = [bot_a]

    with patch(
        "src.bots.registry.interval_spec",
        return_value={"min": 300, "max": 1800, "default": 600},
    ):
        runner._on_command({
            "service_id": "strat-001",
            "action": "set_bot_interval",
            "config": {"bot_id": "mirinae-2", "interval_seconds": 99999},
        })

    assert bot_a._config.interval_seconds == 1800  # max로 클램프


def test_on_command_set_bot_interval_ignores_other_pipe():
    runner = _runner()
    runner.pipe_id = "strat-001"
    bot_a = SimpleNamespace(
        BOT_ID="mirinae-2",
        _config=SimpleNamespace(interval_seconds=600),
        _params={"interval_seconds": 600},
    )
    runner.bots = [bot_a]

    runner._on_command({
        "service_id": "strat-999",  # 다른 파이프
        "action": "set_bot_interval",
        "config": {"bot_id": "mirinae-2", "interval_seconds": 900},
    })

    assert bot_a._config.interval_seconds == 600  # 변경 없음
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipe_runner.py -k set_bot_interval -v`
Expected: FAIL — interval은 그대로 600 (분기 미구현이라 명령 무시됨)

- [ ] **Step 3: Implement the `set_bot_interval` branch**

In `src/services/pipe_runner.py`, replace the `_on_command` method:

```python
    def _on_command(self, msg: dict):
        if msg.get("service_id") != self.pipe_id:
            return
        action = msg.get("action", "")
        logger.info("명령 수신: %s", action)
        if action == "stop":
            self._running = False
        elif action == "set_bot_interval":
            self._apply_bot_interval(msg.get("config", {}))

    def _apply_bot_interval(self, config: dict):
        """실행 중 봇의 분석 주기를 라이브 갱신 (다음 사이클부터 적용)."""
        from src.bots import registry
        bot_id = config.get("bot_id", "")
        try:
            seconds = int(config.get("interval_seconds"))
        except (TypeError, ValueError):
            logger.warning("set_bot_interval: 잘못된 interval_seconds: %r",
                           config.get("interval_seconds"))
            return
        spec = registry.interval_spec(bot_id)
        if spec:
            seconds = max(spec["min"], min(spec["max"], seconds))
        for bot in self.bots:
            if getattr(bot, "BOT_ID", None) == bot_id:
                bot._config.interval_seconds = seconds
                bot._params["interval_seconds"] = seconds
                logger.info("[%s] 분석 주기 변경 → %d초 (다음 사이클부터)",
                            bot_id, seconds)
                return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pipe_runner.py -k set_bot_interval -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/services/pipe_runner.py tests/test_pipe_runner.py
git commit -m "feat(pipe-runner): live-apply bot scan interval via redis command"
```

---

## Task 4: 웹 API — PUT bot-interval + bot-status interval 필드

라우트가 registry 범위로 검증(1차 게이트)하고, 통과 시 `strategies.json` 저장 + Redis 명령 발행. `bot-status`는 각 봇의 유효 주기와 범위를 함께 내려 프론트가 컨트롤을 그릴 수 있게 한다.

**Files:**
- Modify: `src/web/routes.py` (`set_bot_ratio` 라우트 근처에 신규 라우트; `get_bot_status`의 `results.append` 2곳)
- Test: `tests/test_bot_interval_endpoint.py` (Create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_bot_interval_endpoint.py`:

```python
"""PUT /api/pipes/{id}/bot-interval — 파이프 인스턴스별 분석 주기 변경."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _client():
    from src.web.app import app
    return TestClient(app)


def _seed_pipe(tmp_path):
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    mgr.set_total_budget(1_000_000)
    s = mgr.create_strategy("테스트", budget=500_000)
    s.bots = [{"bot_id": "mirinae-2", "ratio": 1.0, "params": {}}]
    mgr._save()
    return s.id


def test_bot_interval_valid_persists_and_sends_command(tmp_path):
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"), \
         patch("src.services.redis_bus.get_bus") as get_bus:
        bus = MagicMock()
        get_bus.return_value = bus
        pipe_id = _seed_pipe(tmp_path)

        resp = _client().put(
            f"/api/pipes/{pipe_id}/bot-interval",
            json={"bot_id": "mirinae-2", "interval_seconds": 900},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["interval_seconds"] == 900

        # strategies.json에 저장됐는지
        from src.strategy import StrategyManager
        s = StrategyManager().get(pipe_id)
        assert s.bots[0]["params"]["interval_seconds"] == 900

        # Redis 명령 발행됐는지
        bus.send_command.assert_called_once()
        args, kwargs = bus.send_command.call_args
        assert args[0] == "pipe"
        assert args[1] == pipe_id
        assert args[2] == "set_bot_interval"


def test_bot_interval_out_of_range_rejected(tmp_path):
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"), \
         patch("src.services.redis_bus.get_bus") as get_bus:
        get_bus.return_value = MagicMock()
        pipe_id = _seed_pipe(tmp_path)

        resp = _client().put(
            f"/api/pipes/{pipe_id}/bot-interval",
            json={"bot_id": "mirinae-2", "interval_seconds": 5},  # min=300 미만
        )
        data = resp.json()
        assert data["success"] is False

        # 저장 안 됨
        from src.strategy import StrategyManager
        s = StrategyManager().get(pipe_id)
        assert "interval_seconds" not in s.bots[0]["params"]


def test_bot_interval_unsupported_bot_rejected(tmp_path):
    with patch("src.strategy.STRATEGIES_FILE", tmp_path / "strategies.json"), \
         patch("src.services.redis_bus.get_bus") as get_bus:
        get_bus.return_value = MagicMock()
        from src.strategy import StrategyManager
        mgr = StrategyManager()
        mgr.set_total_budget(1_000_000)
        s = mgr.create_strategy("테스트", budget=500_000)
        s.bots = [{"bot_id": "analyst", "ratio": 1.0, "params": {}}]
        mgr._save()

        resp = _client().put(
            f"/api/pipes/{s.id}/bot-interval",
            json={"bot_id": "analyst", "interval_seconds": 600},
        )
        assert resp.json()["success"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bot_interval_endpoint.py -v`
Expected: FAIL — 404 (라우트 없음)

- [ ] **Step 3: Implement the route**

In `src/web/routes.py`, add immediately after the `set_bot_ratio` function (after the `@router.put("/pipes/{pipe_id}/bot-ratio")` handler):

```python
class SetBotIntervalRequest(BaseModel):
    bot_id: str
    interval_seconds: int


@router.put("/pipes/{pipe_id}/bot-interval")
async def set_bot_interval(pipe_id: str, req: SetBotIntervalRequest):
    """봇 인스턴스의 분석 주기 변경 + 실행 중이면 라이브 반영.

    registry 범위로 검증 → strategies.json 저장 → Redis 명령 발행.
    """
    from src.strategy import StrategyManager
    from src.bots import registry
    from src.services.redis_bus import get_bus

    spec = registry.interval_spec(req.bot_id)
    if not spec:
        return {"success": False, "error": f"{req.bot_id}는 주기 조절을 지원하지 않습니다"}
    if not (spec["min"] <= req.interval_seconds <= spec["max"]):
        return {"success": False,
                "error": f"주기는 {spec['min']}~{spec['max']}초 범위여야 합니다"}

    mgr = StrategyManager()
    if not mgr.set_bot_interval(pipe_id, req.bot_id, req.interval_seconds):
        return {"success": False, "error": "파이프에 해당 봇이 없습니다"}

    try:
        get_bus().send_command("pipe", pipe_id, "set_bot_interval", {
            "bot_id": req.bot_id,
            "interval_seconds": req.interval_seconds,
        })
    except Exception as e:
        # 저장은 됐으므로 다음 가동 시 적용. 명령 실패는 치명적이지 않음.
        logger.warning("set_bot_interval Redis 명령 실패: %s", e)

    return {"success": True, "interval_seconds": req.interval_seconds}
```

- [ ] **Step 4: Add interval fields to `get_bot_status`**

In `src/web/routes.py`, inside `get_bot_status`, helper to compute the effective interval. Add this helper right after the `_widget_dict` function definition:

```python
    def _interval_fields(bot_id: str, params: dict) -> dict:
        spec = bot_reg.interval_spec(bot_id)
        if not spec:
            return {}
        effective = params.get("interval_seconds", spec["default"])
        return {
            "interval_seconds": int(effective),
            "interval_min": spec["min"],
            "interval_max": spec["max"],
        }
```

Then in the **active** bots `results.append(...)` block, add `**_interval_fields(...)`:

```python
        results.append({
            "bot_id": bot_id,
            "bot_name": bot_info.name if bot_info else bot_id,
            "ratio": ab["ratio"],
            "budget": ab["budget"],
            "ui": ui_spec,
            "status": status,
            **_interval_fields(bot_id, ab.get("params", {})),
        })
```

And in the **inactive** bots `results.append(...)` block:

```python
            results.append({
                "bot_id": ab["bot_id"],
                "bot_name": bot_info.name if bot_info else ab["bot_id"],
                "ratio": ab["ratio"],
                "budget": ab["budget"],
                "ui": None,
                "status": {"running": False, "enabled": False, "scan_count": 0,
                           "holding": {}, "pnl": {"realized": 0, "unrealized": 0, "total": 0}},
                **_interval_fields(ab["bot_id"], ab.get("params", {})),
            })
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_bot_interval_endpoint.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add src/web/routes.py tests/test_bot_interval_endpoint.py
git commit -m "feat(api): PUT bot-interval + expose interval bounds in bot-status"
```

---

## Task 5: pipes.html — 봇 행 주기 입력 UI

`bot-status` 응답에 `interval_max`가 있는 봇만 ratio 슬라이더 아래에 주기 입력을 그린다. 변경 시 PUT 호출, 실패하면 이전 값으로 롤백.

**Files:**
- Modify: `src/web/static/pipes.html` (봇 행 렌더링부 + JS 함수 영역)
- Test: 수동 검증 (JS 단위 테스트 프레임워크 없음)

- [ ] **Step 1: 봇 행 렌더링에서 봇 데이터 구조 확인**

봇 행은 `bot-ratio-ctrl` 마크업으로 렌더링된다(현재 `pipes.html` 약 606행). 그 행을 만드는 함수에서 봇 객체(`b`)가 `b.bot_id`, `b.ratio`를 갖는다. `bot-status` 응답을 쓰는 곳이면 `b.interval_seconds`/`b.interval_max`도 들어온다. 먼저 `grep -n "bot-ratio-ctrl\|ratio-slider\|interval" src/web/static/pipes.html`로 정확한 렌더 위치와 봇 객체 변수명을 확인한다.

- [ ] **Step 2: ratio 컨트롤 아래에 주기 입력 추가**

`bot-ratio-ctrl` div를 닫은 직후(같은 봇 행 안)에 조건부 주기 입력을 삽입한다. `${b.interval_max}`가 있을 때만 렌더:

```javascript
${b.interval_max ? `
<div class="bot-interval-ctrl" style="display:flex;align-items:center;gap:6px;margin-top:4px;font-size:11px;color:var(--muted)">
  <span>주기</span>
  <input type="number" class="interval-input"
    min="${b.interval_min}" max="${b.interval_max}" step="10"
    value="${b.interval_seconds}"
    data-pipe="${p.id}" data-bot="${b.bot_id}" data-prev="${b.interval_seconds}"
    style="width:72px;padding:2px 4px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--fg)"
    onchange="commitInterval(this)">
  <span>초 (${b.interval_min}~${b.interval_max})</span>
</div>` : ''}
```

> 참고: `p.id`와 봇 객체 변수명(`b`)은 Step 1에서 확인한 실제 변수명에 맞춘다. ratio 슬라이더가 `${p.id}`/`${b.bot_id}`를 쓰고 있으면 그대로 동일하게 쓴다.

- [ ] **Step 3: `commitInterval` 함수 추가**

`commitRatio` 함수 근처(JS 영역)에 추가:

```javascript
async function commitInterval(input) {
  const pipeId = input.dataset.pipe;
  const botId = input.dataset.bot;
  const sec = parseInt(input.value, 10);
  const prev = parseInt(input.dataset.prev, 10);
  const min = parseInt(input.min, 10);
  const max = parseInt(input.max, 10);

  if (isNaN(sec) || sec < min || sec > max) {
    input.value = prev;  // 범위 밖 → 롤백
    return;
  }
  try {
    const res = await fetch(API + `/api/pipes/${pipeId}/bot-interval`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bot_id: botId, interval_seconds: sec }),
    });
    const data = await res.json();
    if (data.success) {
      input.dataset.prev = String(sec);
    } else {
      input.value = prev;  // 서버 거부 → 롤백
      alert(data.error || '주기 변경 실패');
    }
  } catch (e) {
    input.value = prev;
    alert('주기 변경 요청 실패');
  }
}
```

- [ ] **Step 4: 수동 검증**

웹 서버를 띄우고(또는 이미 떠 있으면) 브라우저에서 파이프 상세를 연다.
1. `mirinae-2`/`stealth-volume`이 연결된 파이프에서 ratio 아래 "주기" 입력이 보이는지.
2. 값을 범위 안(예: mirinae-2를 900)으로 바꾸면 유지되고, 새로고침 후에도 900으로 보이는지(`strategies.json` 저장 확인).
3. 범위 밖(예: 5) 입력 시 이전 값으로 롤백되는지.
4. `analyst`만 연결된 파이프에서는 주기 입력이 안 보이는지.

확인 명령(저장 검증):

```bash
python -c "import json; d=json.load(open('data/strategies.json')); print([(b['bot_id'], b.get('params',{}).get('interval_seconds')) for s in d.get('strategies',d if isinstance(d,list) else []) for b in s.get('bots',[])])"
```

> 참고: `strategies.json` 최상위 구조는 Step 1에서 파일을 열어 확인한 실제 형태에 맞춰 위 명령을 조정한다.

- [ ] **Step 5: Commit**

```bash
git add src/web/static/pipes.html
git commit -m "feat(ui): per-pipe bot scan interval control in pipe detail"
```

---

## Task 6: 전체 회귀 테스트

- [ ] **Step 1: 전체 테스트 실행**

Run: `pytest tests/ -q`
Expected: 모든 테스트 PASS (신규 4개 파일/케이스 포함, 기존 테스트 회귀 없음)

- [ ] **Step 2: 실패 시 처리**

기존 테스트가 깨졌다면 변경한 파일(`registry.py`, `strategy.py`, `pipe_runner.py`, `routes.py`)의 시그니처/동작 회귀를 먼저 의심한다. `set_bot_interval`이 `ratio`를 건드리지 않는지, `_on_command`가 기존 `stop` 동작을 유지하는지 재확인한다.

---

## Self-Review (작성자 체크 완료)

- **스펙 커버리지**: 적용 대상 봇 필터(Task 1·4의 `interval_spec`/`_interval_fields`), 파이프 인스턴스 저장(Task 2), 라이브 반영(Task 3), 검증/엣지(Task 4 라우트 + 범위 밖/미지원 봇/파이프 정지 시 명령 실패 허용), UI(Task 5) 모두 태스크에 매핑됨.
- **플레이스홀더**: 없음. 모든 코드 블록은 실제 구현 코드.
- **타입 일관성**: `interval_spec` 반환 `{"min","max","default"}`을 라우트·PipeRunner·bot-status가 동일 키로 사용. `set_bot_interval(strategy_id, bot_id, seconds)->bool` 시그니처를 라우트가 그대로 호출. Redis 명령 action 문자열 `"set_bot_interval"`을 발행(routes)·수신(pipe_runner) 양쪽 동일.
