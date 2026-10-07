# Agent Ops 설계 — 거래 투명성 시스템 + 자가 개선 루프

## 목적

두 가지 목표를 하나의 서브시스템으로 통합한다.

1. **투명성**: 거래의 전 과정 — 봇이 무엇을 조회했고, AI가 무엇을 보고 무엇이라 답했으며, 시스템이 무엇을 실행/차단했고, 그 결과가 어땠는지 — 를 사용자가 대시보드와 채팅 질의로 완전히 추적할 수 있어야 한다.
2. **자가 개선**: 에이전트가 축적된 판단·성과 데이터를 분석하여 승률(정확히는 기대값 EV)을 높이는 변경을 스스로 적용하고, 그 변경 자체도 투명하게 추적·검증·롤백한다.

핵심 원칙: **개선 루프의 변경도 "판단"이다.** 봇의 매매 판단과 동일하게 근거·검증결과·사후 성과가 audit DB에 남는다. 이것이 두 목표를 하나의 시스템으로 묶는 연결점이다.

## 결정된 범위

- 투명성: 데이터 계층(audit DB) + MCP/채팅 질의 + **대시보드 UI 모두 포함**
- 자가 개선 자율성: **자동 적용 + 사후 보고** (사전 승인 불필요)
- 자동 변경 대상: **파라미터, confidence 게이트, 룰 파일/프롬프트, 봇 비율·예산 재배분 전부**
- 접근 방식: 통합 동시 설계 (단일 스펙), 구현은 단계 실행

## 전체 아키텍처

```
[봇 실행 계층]  mirinae-2/3, stealth-volume, (미래 mirinae-4)
      │  AuditRecorder가 매 스캔을 기록 (decision-audit-db 워크트리에 구현됨)
      ▼
[데이터 계층 — 단일 진실원천: data/ff.db]
   decision_runs / evidence_snapshots / ai_audit_logs
   / preflight_checks / decision_outcomes        (구현됨, 머지 대기)
   + improvement_actions (신규)                   ← 개선 루프 변경 이력
      ▼
[소비 계층]
   A. 투명성 대시보드 (web/static/agent-ops.html)
   B. Insight Engine (개선 루프 v2 — evolver 재건)
```

선행 조건: `.worktrees/decision-audit-db`의 기존 구현(5개 audit 테이블, `AuditRecorder`, MCP `get_audit_kpi`)을 완성하여 main에 머지한다.

## 데이터 계층

### `improvement_actions` (신규 테이블)

개선 루프의 모든 변경 이력. 한 행 = 하나의 변경 시도.

| 필드 | 설명 |
|------|------|
| `id`, `action_id` | PK, 고유 텍스트 ID |
| `timestamp` | 생성 시각 |
| `target_type` | `param` / `gate` / `rule` / `ratio` |
| `target` | 대상 식별 (예: `mirinae-2:confidence_threshold`, `strat-001:ratios`) |
| `before_json`, `after_json` | 변경 전/후 값 (rule은 git 커밋 해시) |
| `rationale` | 분석 근거 (어떤 audit 데이터가 이 변경을 지지하는가) |
| `validation_json` | 검증 게이트 결과 (표본수, EV 추정, 변경 폭) |
| `status` | `proposed` → `validated` → `applied` → `confirmed` / `rolled_back` / `rejected` |
| `tracking_window_json` | 추적 기준 (N거래 또는 N일) |
| `result_json` | 추적 윈도우 종료 후 성과 (변경 전후 EV·승률 비교) |

규칙:
- 이 테이블은 삭제·압축하지 않는다 (audit 보존 원칙과 동일).
- `status=rejected`는 검증 게이트에서 차단된 제안 — 차단도 기록한다.
- 인덱스: `action_id`, `target_type`, `status`, `timestamp`.

### 비-Mirinae 봇 audit 연동

stealth-volume에도 `AuditRecorder`를 적용한다. AI 호출이 없으므로 `evidence` + `preflight` + 판단 사유(`decision_runs.action` + 사유 텍스트)만 기록하고 `ai_audit_logs`는 비운다.

## 투명성 대시보드

기존 웹 패턴(정적 HTML + `/api/*` + SSE)을 따른다. 신규 페이지: `src/web/static/agent-ops.html` (+ 필요 시 상세 페이지 분리).

### ① 판단 타임라인 (메인 뷰)
- `decision_runs` 시간순 스트림. 필터: 봇 / 종목 / 액션(buy·sell·hold·skip) / 상태(blocked·ordered·failed)
- 행 구성: 시각, 봇, 종목, 판단, confidence, 결과(체결가·손익), 차단 사유 뱃지
- SSE로 신규 run 실시간 추가

### ② 판단 리플레이 뷰 (run 클릭 → 상세)
한 판단의 전 과정을 단계별로 재현한다:
1. 수집 증거 (`evidence_snapshots` — 시세·차트·뉴스·잔고, 원본 JSON 펼침 가능)
2. AI 프롬프트 전문 (`ai_audit_logs.prompt`)
3. AI 원문 응답 + 파싱된 판단 (`raw_response`, `parsed_json`, confidence)
4. preflight 체크 결과 (통과/차단 사유별)
5. 주문 결과 (`trades` 링크)
6. 사후 성과 (`decision_outcomes` — 보유시간, 수익률, 최대유리폭/최대불리폭)

### ③ KPI 보드
- 봇별: 승률, profit factor, 일일 수익률, 거래당 평균 손익 (기존 `get_audit_kpi` 재사용)
- **confidence vs 실수익 산점도** — AI 신뢰도의 유효성 시각 검증
- 증거 타입별 예측력, 차단 사유 Top N, 종목별 성패 패턴

### ④ 개선 이력 뷰
- `improvement_actions` 타임라인: 무엇이 언제 왜 바뀌었고, 추적 윈도우 결과(변경 전후 성과 비교), 롤백 여부
- Insight Engine **일시정지/재개 토글** (kill switch)

### API
- `GET /api/audit/runs` (필터·페이징), `GET /api/audit/runs/{run_id}` (리플레이 전체), `GET /api/audit/kpi`, `GET /api/improvements`, `POST /api/insight-engine/toggle`

## Insight Engine (개선 루프 v2)

기존 evolver의 codex 서브프로세스 의존을 제거하고, audit DB를 입력으로 하는 메타봇으로 재작성한다. AI 호출은 `src/bots/ai_caller.py` 경유(멀티 프로바이더). 실행은 orchestrator 스케줄(파이프 연결 불필요).

주기: 장중 1시간 간격 경량 점검 + 장 마감 후 일일 심층 분석.

### 6단계 사이클

```
① 분석   audit DB 질의 (코드 + SQL):
         confidence 버킷별 승률, 증거 타입별 예측력,
         파라미터-성과 상관, 봇별 EV, 차단 사유 분포
② 제안   AI가 분석 결과를 받아 변경안 생성
         → improvement_actions(status=proposed)
③ 검증   통계 게이트 (순수 코드, AI 판단 아님):
         - 최소 표본수: 해당 변경과 관련된 거래 20건 미만 → rejected
         - EV 추정: 과거 trades/decisions 기반 변경 후 EV > 0 추정 실패 → rejected
         - 변경 폭: 수치 파라미터 ±20%/회 초과 → rejected
         - 빈도: 봇당 일일 2회 초과 → rejected
         - 추적 중 재변경: 추적 윈도우 미종료 대상 → rejected
④ 적용   target_type별 적용기:
         - param: data/bot_config/*.json (변경 전 값을 before_json에 보존)
         - gate: confidence 구간 매수 차단 설정 (bot_config 내 항목)
         - rule: data/bot_rules/*.md — 변경마다 git 자동 커밋 (diff·롤백 보장)
         - ratio: strategies.json 비율·예산 재배분 (다음 스캔 사이클부터 반영)
⑤ 추적   적용 후 N거래(기본 20) 또는 N일(기본 5) 윈도우 성과 기록
⑥ 롤백   추적 윈도우에서 변경 전 대비 EV·승률 악화 →
         자동 원복 + status=rolled_back + 대시보드 표시
```

핵심: **AI는 제안만, 게이트는 코드가 결정.** 잘못된 방향의 룰 변경 위험을 통계 게이트(사전)와 자동 롤백(사후)으로 이중 방어한다.

### 관찰 모드 선행

초기 배포 시 자동 적용을 끄고(proposed까지만 진행) 대시보드에서 제안 품질을 관찰한 뒤, 사용자가 자동 적용을 활성화한다. 활성화 이후는 자동 적용 + 사후 보고.

## 안전장치

- **개선 루프는 주문 불가** — 설정·룰 파일 변경만. TradeEngine 접근 금지.
- **audit 기록 실패는 매매를 막지 않음** (기존 원칙 유지). 실패는 로그 + 대시보드 경고.
- **룰 파일 변경 = git 자동 커밋** — diff 확인·롤백 항상 가능. config JSON은 `before_json` 스냅샷 보존.
- **ratio 변경은 다음 스캔 사이클부터** — 보유 포지션 강제 청산 없음.
- **kill switch** — 대시보드 토글 + `data/bot_config/insight-engine.json`의 `enabled` 플래그. 정지 중에도 분석·제안은 계속 쌓인다.
- **연쇄 변경 방지** — 추적 윈도우 미종료 대상 재변경 금지 (한 번에 한 변수 검증).

## 테스트

단위:
- `improvement_actions` 마이그레이션 idempotent
- 검증 게이트: 표본 부족 / EV<0 / 폭 초과 / 빈도 초과 / 추적 중 재변경 각각 rejected
- 적용기 roundtrip: param·gate·rule·ratio 각각 적용 → 롤백 시 원상복구
- 추적 윈도우 판정 로직 (confirmed vs rolled_back)

통합:
- 가짜 audit 데이터로 분석→제안→검증→적용→추적→롤백 전체 사이클 재현
- 대시보드 API 응답 스키마 검증
- kill switch 활성 시 ④ 적용 단계 미실행 확인

## 구현 순서

| 단계 | 내용 | 비고 |
|------|------|------|
| 0 | decision-audit-db 워크트리 완성·머지 | 기존 플랜 마무리. 전체의 선행 조건 |
| 1 | `improvement_actions` 테이블 + stealth-volume audit 연동 | 데이터 계층 완결 |
| 2 | 대시보드 (타임라인 → 리플레이 → KPI → 개선이력 순) | 1단계와 병행 가능 |
| 3 | Insight Engine (분석·게이트 → 적용기 → 추적·롤백) | 관찰 모드로 시작, 검증 후 자동 적용 활성화 |

## 범위 제외

- 기존 `trades`/`decisions`/봇 로그의 재작성·삭제
- mirinae-4 신규 봇 구현 (이 시스템은 그 평가 기반을 제공할 뿐)
- 전체 JSONL 로그의 과거 데이터 backfill
- LIVE 모드 매매 규칙 변경
