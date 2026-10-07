# 파이프 인스턴스별 봇 분석 주기 조절 UI — 설계

- 작성일: 2026-05-22
- 범위: 봇의 분석 주기(`interval_seconds`)를 웹 대시보드에서 조절하는 기능
- 승인된 접근법: **A (최소 변경, 다음 사이클 적용)**

## 배경

봇의 분석/스캔 주기(`interval_seconds`)는 현재 코드 기본값(`DEFAULT_PARAMS`/`SCALP_PARAMS`/`SWING_PARAMS`)과
`data/bot_config/{bot_id}.json` 파일로만 조절 가능하다. 웹 UI에서 직접 바꿀 방법이 없다.

런타임 구조상:
- 파이프는 별도 subprocess(`python -m src.services.pipe_runner <pipe_id>`)로 실행되고, 봇들은 그 프로세스 안의 asyncio task다.
- 봇 메인 루프(`src/bots/base.py:525`)는 `await asyncio.sleep(self._config.interval_seconds)`로 대기하며, `interval_seconds`를 시작 시 1회만 읽는다.
- 웹↔봇 통신은 Redis Pub/Sub 명령 채널(`CH_PIPE_COMMAND`, `CH_BOT_COMMAND`)이 이미 존재하며 현재 `stop`/`restart`만 처리한다.
- `PipeRunner`는 `self.bots`로 봇 인스턴스 참조를 보유하고(`b.BOT_ID`로 식별), `_on_command`로 Redis 명령을 받는다.
- `strategies.json`의 `bots[].params`는 이미 존재하나 현재 거의 비어 있다. `factory.py`는 봇 생성 시 `params.interval_seconds`를 `_config.interval_seconds`에 주입한다(이미 지원).

## 결정 사항 (브레인스토밍 결과)

1. **범위**: `interval_seconds`만 조절. 다른 봇 파라미터는 범위 밖.
2. **적용 단위**: 파이프 인스턴스별. `strategies.json`의 `bots[].params.interval_seconds`에 저장 → 같은 봇이라도 파이프마다 다른 주기 가능. 봇 전역(`data/bot_config`)은 건드리지 않는다.
3. **반영 시점**: 즉시 반영(접근법 A). 실행 중인 봇 인스턴스의 `_config.interval_seconds`를 Redis 명령으로 라이브 갱신. 봇은 현재 sleep 종료 후 **다음 사이클**부터 새 주기를 사용한다(현재 자고 있는 sleep은 끊지 않음 → 주기를 줄일 때는 현재 sleep만큼 지연될 수 있음). 봇 공통 루프는 수정하지 않는다.

## 적용 대상 봇

`registry.py`에 `interval_seconds` BotParam(min/max)이 등록된 봇만 UI 컨트롤을 노출한다.

| 봇 | 범위 |
|---|---|
| `stealth-volume` | 10 ~ 300초 |
| `mirinae-2` | 300 ~ 1800초 |
| `mirinae-3` | 300 ~ 7200초 |
| `wallstreet-us-ai` | 300 ~ 1800초 |

제외:
- `analyst` — `interval_seconds` BotParam 메타데이터 미등록.
- `evolver` — `BotBase` 비상속, `DEFAULT_INTERVAL` 클래스 상수 사용, 독립 라이프사이클이라 라이브 갱신 경로가 없음.

이 두 봇은 컨트롤 자체가 렌더링되지 않는다(bot-status가 `interval_min/max`를 내려주지 않으면 프론트가 컨트롤을 숨김).

## 데이터 흐름

```
[pipes.html 봇 행] ratio 슬라이더 옆 주기 입력 (number, min/max=registry, 단위 "초")
      │ onchange
      ▼
PUT /api/pipes/{pipe_id}/bot-interval   { bot_id, interval_seconds }
      │
      ├─ 1) registry BotParam min/max로 검증 (벗어나면 400, {success:false})
      ├─ 2) StrategyManager.set_bot_interval() → strategies.json bots[].params.interval_seconds 저장
      └─ 3) bus.send_command("pipe", pipe_id, "set_bot_interval", {bot_id, interval_seconds})
              │ (Redis Pub/Sub — 기존 채널 재사용)
              ▼
        PipeRunner._on_command(): self.bots 중 BOT_ID 일치 봇을 찾아
              bot._config.interval_seconds 갱신 + bot._params["interval_seconds"] 갱신
              ▼
        봇이 현재 sleep 종료 후 다음 사이클부터 새 주기 적용
```

## 변경 파일

| 파일 | 변경 |
|---|---|
| `src/strategy.py` | `set_bot_interval(pipe_id, bot_id, sec)` 추가 — `bots[].params.interval_seconds` 기록 후 `_save()`. ratio·다른 봇 미변경 |
| `src/web/routes.py` | `PUT /pipes/{id}/bot-interval` 핸들러 추가 (검증→저장→Redis 명령). `GET /pipes/{id}/bot-status` 응답에 `interval_seconds`(유효값)·`interval_min`·`interval_max` 추가 |
| `src/services/pipe_runner.py` | `_on_command`에 `set_bot_interval` 분기 추가 — `self.bots` 중 일치 봇의 `_config.interval_seconds`/`_params["interval_seconds"]` 라이브 갱신 + 방어적 클램프 |
| `src/web/static/pipes.html` | 봇 행에 주기 입력 UI + `commitInterval()` JS (ratio 슬라이더 commit 패턴 재사용, 실패 시 이전 값 롤백) |
| 테스트 | 아래 참조 |

## 검증 규칙 / 범위(min/max) 소스

- 단일 진실원천은 `registry.py`의 해당 봇 `interval_seconds` BotParam의 `min_value`/`max_value`.
- 라우트가 검증의 1차 게이트(범위 밖 거부). `PipeRunner._on_command`는 방어적으로 한 번 더 클램프(웹을 우회한 명령 대비).

## 엣지 / 에러 처리

- **범위 밖 값**: 라우트 400 + `{success:false, error}`. 프론트는 입력을 직전 값으로 롤백.
- **파이프 정지 상태**: Redis 구독자 없음 → 명령은 무해한 no-op. `strategies.json`에는 저장되므로 다음 가동 시 `factory.py`가 `params.interval_seconds`를 주입하여 적용.
- **봇이 파이프에 없음**: 저장은 수행되되 Redis 명령은 no-op.
- **대상 외 봇(analyst/evolver)**: bot-status가 `interval_min/max`를 안 내려주므로 컨트롤이 렌더링되지 않음. 라우트도 등록 메타데이터가 없으면 거부.

## 테스트

- `StrategyManager.set_bot_interval`: `bots[].params.interval_seconds`에 기록되고 `ratio`·다른 봇 항목 불변.
- 라우트 검증: registry min/max 밖 값이면 `{success:false}` 반환, `strategies.json` 미변경.
- `PipeRunner._on_command`: 가짜 봇 리스트 주입 후 `set_bot_interval` 명령 시 일치하는 봇의 `_config.interval_seconds`/`_params`만 갱신되고 다른 봇은 불변. 범위 밖 값은 클램프됨. (기존 `test_*` 스타일 준수)

## 비목표 (YAGNI)

- 봇 전역(`data/bot_config`) 주기 변경 UI — 범위 밖.
- `interval_seconds` 외 다른 파라미터 편집기 — 범위 밖.
- 주기를 줄일 때 현재 sleep을 즉시 끊는 인터럽트 가능 sleep(접근법 B) — 채택 안 함.
- `analyst`/`evolver` 주기 조절 — 대상 외.
