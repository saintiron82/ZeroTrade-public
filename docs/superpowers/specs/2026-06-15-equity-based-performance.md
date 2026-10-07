# 자기자본 기반 봇 성과 비교 엔진 설계

## 문제 (2026-06-15 발견)

봇 성과 비교가 **자본 베이스가 다른 봇을 절대 금액으로 줄세운다**. winner-1(9만원)·mirinae-3(21만원)·mirinae-2(0%·보유분만)는 굴리는 돈이 다른데 "누가 +N원"으로 비교하면 무의미하다.

확인된 3겹 버그:
1. **금액 정렬**: `routes.py:2683`(봇 비교) `sort(key=pnl)`, `routes.py:2777`(오늘 손익) `sort(key=realized)` — ROI%를 계산해놓고 금액으로 정렬. UI는 `+11,440원`만 표시.
2. **분모가 현재 한 시점 budget 스냅샷**: `roi_pct = pnl / 현재budget`. ratio 변경(mirinae-3 100%→70%, 6/12)·신규(winner-1)·0%(mirinae-2 → budget 0 → ROI 0% 오표시)를 반영 못 함.
3. **일별 자본 베이스 이력 없음**: `snapshots` 테이블 3/18 1건 뒤 방치.

## 결정 (사용자 확정)

수익률 분모 = **봇 자기자본(equity), 일별 재구성** — 그날 시작 시점 봇의 실자본 = 현금 + 보유가치(종가×수량). BotLedger + 과거 종가로 조회 시 재구성. 신규 저장 불필요(읽기 시 계산).

## 핵심 알고리즘

### 봇 equity 정의

```
봇 equity(t) = cash(t) + Σ_보유종목 [qty(t) × price(t)]
            = allocated_budget + realized_pnl(t) + unrealized_pnl(t)
```

- `cash(t)` = BotLedger 현금 = allocated_budget − 누적매수금 + 누적매도금 (자기 trades만, FIFO)
- `allocated_budget` = 현재 ratio×파이프예산 (cash 절대값 anchor)
- phantom-cleanup(수량↓·손익무영향)·orphan-assign(수량↑·현금무영향)은 BotLedger가 이미 올바르게 처리 → 그대로 사용 (orphan-assign 보유분도 봇이 실제 관리하는 자본이므로 equity 포함이 맞음)

### 일별 수익률

날짜 D에 대해:
```
positions_start, cash_start = ledger(trades where ts < D_00:00)
positions_end,   cash_end   = ledger(trades where ts ≤ D_23:59)
equity_start(D) = cash_start + Σ qty_start[code] × close(code, D-1)
equity_end(D)   = cash_end   + Σ qty_end[code]   × close(code, D)
daily_pnl(D)    = equity_end(D) − equity_start(D)   # = realized(D) + Δunrealized(D)
return_pct(D)   = daily_pnl(D) / equity_start(D) × 100   (equity_start > 0일 때만)
```

하루 안에는 자본 유입/유출이 없으므로 `equity_end − equity_start`가 그날 총손익(실현+미실현변동)과 정확히 일치. 이것이 시간가중 일일수익률.

### 분모가 0/없음 처리
- `equity_start ≤ 0` (보유 없고 현금 0): 그날 return_pct = null (거래 없으면 행 자체 생략)
- mirinae-2(budget 0)라도 보유 포지션이 있으면 equity_start = 보유가치 > 0 → ROI 정상 산출 (버그 ② 해소)

### 기간 수익률
- **누적 금액**: Σ daily_pnl (기존 표시 유지)
- **시간가중 복리**: Π(1 + return_pct(D)/100) − 1 — KODEX 벤치마크와 동일 기준으로 alpha 비교
- 둘 다 응답에 포함, UI는 % 우선 표시

### 종가 조회
- `price(code, date)` = 해당일 일봉 종가. 기존 `get_daily_chart`(KISMarket) 사용, 코드별 1회 조회 후 캐시(`{code: {date: close}}`).
- 조회 실패/상장폐지/주말: 직전 영업일 종가 → 없으면 해당 lot 평단(cost basis)로 폴백. 폴백 시 행에 `price_estimated: true` 플래그.

## 구현 범위

### 신규: `src/services/bot_equity.py`
순수 함수 모듈 (DB/네트워크 주입식 — 테스트 용이):
```python
def reconstruct_daily_equity(
    trades: list[dict],       # 한 봇의 success trades (시간순)
    budget: int,              # allocated budget (cash anchor)
    days: list[str],          # 평가 대상 날짜 (YYYY-MM-DD, 오름차순)
    close_price: Callable[[str, str], int | None],  # (code, date) → 종가
) -> list[DailyEquity]
# DailyEquity: {date, equity_start, equity_end, realized, unrealized_change,
#               daily_pnl, return_pct, price_estimated}
```
+ `period_return(daily: list[DailyEquity]) -> {cum_pnl, twr_pct, avg_equity}`

### 수정: `/bot-performance/daily` (routes.py:2517)
- `roi_pct`를 equity 기반 일별로 교체 (현재 `pnl/현재budget` 폐기)
- **정렬을 `twr_pct`(시간가중 수익률)로 변경** (2683 금액정렬 제거)
- alpha = bot twr_pct − benchmark period_return_pct (이미 % 기준이라 정합)
- 응답에 `equity_start_avg`, `twr_pct`, `cum_pnl`, `price_estimated_days` 추가

### 수정: `/pnl/daily` (routes.py:2730) + 오늘 손익 위젯
- 각 봇 행에 `return_pct`(당일 equity 기준) 추가
- **정렬을 return_pct로 변경** (2777 금액정렬 제거)
- UI(index.html:703): `+11,440원` → `+11,440원 (+1.16%)` 형식, % 우선 색상

### 종가 조회 헬퍼
- routes.py에 `_bot_equity_close_fn(codes, days)` — 필요한 코드 일봉을 미리 모아 캐시 dict 반환, `close_price` 콜백으로 주입

## 테스트

`tests/test_bot_equity.py` (순수 함수 — 네트워크 0):
- 단순: 1일 1매수 1매도, equity_start=budget → return_pct 정확
- 보유 이월: 어제 매수분이 오늘 종가로 평가, Δunrealized 반영
- ratio 0 봇(budget=0)인데 보유분 있음 → equity_start=보유가치, return_pct 정상 (버그② 회귀)
- 종가 조회 실패 → cost basis 폴백 + price_estimated=true
- 기간 TWR: 일별 +1%, +1% → 복리 2.01%
- equity_start=0 → return_pct null, 기간 계산서 제외
- phantom-cleanup/orphan-assign 행 혼입 → BotLedger 의미론대로 수량 반영, 실현손익 미오염

`tests/test_performance_endpoints.py` (라우트, KISMarket mock):
- 두 엔드포인트가 % 정렬인지, equity 기반 roi인지
- mirinae-2(0%)가 0%가 아니라 실제 보유 수익률로 표시되는지

## 범위 제외 (후속)
- 일별 equity 스냅샷 영속 테이블(`bot_equity_snapshots`) — 지금은 읽기 시 재구성으로 충분. 장기 정확도(과거 ratio 변경 이력)·성능 위해 나중에 추가. 현 시점 핵심 비교(winner vs mirinae, 6/12~ ratio 70/30 안정 구간)는 재구성으로 정확.
- 분(分) 단위 within-day 수익률
