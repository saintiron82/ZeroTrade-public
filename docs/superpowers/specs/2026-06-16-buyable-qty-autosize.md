# 실가용현금 반영 + 매수 수량 자동 축소 설계

## 문제 (2026-06-16)

winner-1이 대우건설 5주 매수를 시도했으나 차단됐다. 원인 2겹:
1. **유령 매수여력**: winner-1은 프롬프트에서 "가용 151,000원"이라 들었지만, 실제 KIS 계좌의 주문가능현금은 그보다 적었다. 파이프가 300k를 봇들에 장부상 배분하나 실계좌가 못 받쳐줌.
2. **수량 미조정**: 5주를 못 사면 살 수 있는 수량(예: 4주)으로 줄이지 않고 통째로 차단. CLAUDE.md "ATR/예산 비례 포지션 크기" 원칙에도 미달.

사용자 지시: ① 가용현금을 실시간 반영 ② 5주 안되면 4주로 자동 구매처리.

## 결정 (사용자 확정)

- 적용 범위: **전체 봇** (engine.buy 중앙 적용)
- 봇의 가용현금 = **min(가상 장부 cash, 실계좌 주문가능현금)**
- 못 살 수량이면 **살 수 있는 최대 수량으로 자동 축소**, 1주도 못 사면 차단

## 핵심 도구

`KISMarket.get_buyable_qty(stock_code, price)` → `{max_qty, max_amount, cash}` — KIS 네이티브 매수가능조회(inquire-psbl-order, TTTC8908R). `max_qty`가 곧 **실계좌 기준 그 종목·그 가격에서 살 수 있는 최대 수량**, `cash`는 주문가능현금. 실계좌 제약을 수량으로 직접 제공.

## 설계

### A. 엔진 매수 수량 자동 축소 (전체 봇)

`src/bots/engine.py` `buy(code, qty, price)` 의 cash 체크를 수량 클램프로 교체:

```
unit_price = price if price > 0 else 현재가(self._get_market().get_price(code))
# 1. 가상 장부 상한
virtual_cap = (self.cash // unit_price) if self.budget > 0 else 무제한
# 2. 실계좌 상한 (KIS 네이티브)
buyable = self._buyable(code, unit_price)   # get_buyable_qty 래퍼, 실패 시 None
real_cap = buyable["max_qty"] if buyable and buyable["max_qty"] > 0 else None
# 3. min
caps = [c for c in (virtual_cap, real_cap) if c is not None]
final_qty = min(qty, *caps) if caps else qty
```
- `final_qty < 1` → 차단 `"가용 현금 부족 — 1주도 매수 불가"` (preflight block 기록).
- `1 <= final_qty < qty` → **축소 매수**: `final_qty`로 주문, 결과 message에 `"수량 조정 {qty}→{final_qty}주 (가용현금)"` 포함. 로그·게시판·audit에 조정 사실 명시.
- `final_qty == qty` → 기존과 동일.
- 시장가(price=0): unit_price=현재가, 슬리피지 버퍼는 KIS `max_qty`가 이미 반영하므로 가상 상한만 현재가로 계산(보수적). 현재가 조회 실패 시 가상 상한 스킵하고 real_cap만, real도 없으면 기존 차단.
- `validate_order`(최대주문액·모드)는 **축소된 final_qty 금액**으로 호출.
- `get_buyable_qty` 실패(None)는 거래를 막지 않음 — 가상 상한만 적용(KIS가 주문 시점에 최종 거부 가능). 단 경고 로그.

기존 cash 체크 라인(`if self.budget>0 and cost>self.cash: return 차단`)은 이 클램프 로직으로 대체. ratio 0% 봇의 `buy_block_reason`(2026-06-12 가드)은 클램프보다 앞에서 그대로 차단 유지.

### B. winner-1 프롬프트 실가용현금 (winner-1 한정)

`src/bots/winner1.py` `_current_state()`가 보고하는 cash를 **min(가상 장부 cash, 실계좌 주문가능현금)**으로:
```
ledger_cash = BotLedger(...).cash
psbl = KISMarket().get_buyable_qty(대표코드 or "", 0) → cash  # ord_psbl_cash
real_cash = psbl["cash"] if 조회성공 else None
report_cash = min(ledger_cash, real_cash) if real_cash is not None else ledger_cash
```
- 종목 무관 주문가능현금만 필요하므로 임의 대표 종목 없이 호출 가능한지 확인 — 불가하면 보유/유니버스 첫 종목으로 조회하거나, 실계좌 cash는 `get_balance().summary.cash`(resilient)로 대체.
- 프롬프트가 실제 살 수 있는 돈을 알려주므로 winner-1이 애초에 맞는 수량으로 주문 → 엔진 축소는 안전망.
- KIS 조회 실패 시 가상 장부값 그대로 (degrade, 매매 중단 없음).

### 데이터 흐름
```
봇 매수 결정 (qty=5)
  → engine.buy(code, 5, price)
     → buy_block_reason 체크 (ratio 0% 차단 유지)
     → unit_price 결정
     → virtual_cap = cash//price, real_cap = get_buyable_qty.max_qty
     → final_qty = min(5, caps)  (예: 4)
     → final<1 차단 / final<5 축소 매수 + 조정 기록 / final==5 정상
     → validate_order(final 금액) → KIS 주문(final) → trades/sync/board
```

## 안전·엣지
- LIVE 실주문 경로 — 축소는 항상 **요청량 이하**로만(절대 증량 없음).
- get_buyable_qty/현재가 조회 실패가 거래를 막지 않되, 둘 다 실패 시 가상 상한만으로 보수적 진행.
- 축소 발생은 반드시 가시화(message·로그·audit) — "조용한 축소"로 사용자가 모르는 일 없게.
- API 호출 빈도: 매수 시에만 get_buyable_qty 1회(매수는 드묾) → 부담 없음. winner _current_state는 스캔당 1회.

## 테스트
`tests/test_engine.py` (KISOrder/KISMarket mock):
- 요청 5주, 실계좌 max_qty 4 → 4주 매수, message에 "5→4"
- 요청 5주, 가상 cash로 3주만 → 3주
- 요청 5주, 둘 다 5 이상 → 5주 (무변동)
- max_qty 0 + 가상도 0 → 차단
- get_buyable_qty 실패(None) → 가상 상한만 적용 (거래 안 막힘)
- 시장가(price=0) → 현재가로 unit_price, 축소 동작
- ratio 0% 봇(buy_block_reason) → 클램프 전에 차단 유지 (회귀)
- 축소 후 validate_order가 축소금액으로 호출되는지

`tests/test_winner1_bot.py`:
- _current_state가 min(가상, 실계좌 cash) 반환, 실계좌 조회 실패 시 가상값 폴백

## 범위 제외
- 봇 간 실계좌 현금 경쟁 조정(동시에 둘이 사면 선착순) — 현 KIS max_qty가 호출 시점 실시간이라 자연 처리, 명시적 락은 과설계
- 매도 수량 축소(이미 실잔고 클램프 존재)
