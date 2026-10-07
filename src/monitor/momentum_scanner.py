"""1시간 모멘텀 ETF 스캐너.

전체 거래 가능 ETF를 대상으로 1시간 수익률을 계산하고,
하락장 회피 규칙과 함께 최적 매수 대상을 선정한다.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from src.kis.market import KISMarket
from src.kis.master import StockMaster

logger = logging.getLogger(__name__)

# ETF 브랜드 키워드 (오분류 방지를 위해 정확한 리스트)
ETF_BRANDS = [
    "KODEX", "TIGER", "ACE", "PLUS", "SOL", "ARIRANG", "KBSTAR",
    "HANARO", "KOSEF", "SMART", "TIMEFOLIO", "WOORI",
]

# 시장 벤치마크
BENCHMARK_CODE = "069500"  # KODEX 200
BENCHMARK_NAME = "KODEX 200"


@dataclass
class ETFMomentum:
    """개별 ETF 모멘텀 결과."""
    code: str
    name: str
    current_price: int
    price_1h_ago: int
    return_1h: float        # 1시간 수익률 (%)
    volume: int = 0         # 누적 거래량
    scan_time: str = ""


@dataclass
class ScanResult:
    """전체 스캔 결과."""
    scan_time: str
    benchmark_return: float          # KODEX 200 1h 수익률
    is_bullish: bool                 # 상승장 여부
    total_scanned: int               # 스캔한 ETF 수
    rankings: list[ETFMomentum] = field(default_factory=list)
    errors: int = 0                  # API 오류 수
    top_pick: ETFMomentum | None = None


# ── ETF 유니버스 필터 ──────────────────────────────────────

def get_etf_universe() -> list[dict]:
    """거래 가능한 실제 ETF 목록 반환.

    필터:
      1. 6자리 숫자 코드 (펀드 코드 제외)
      2. ETF 브랜드 키워드 포함
    """
    master = StockMaster()
    all_etfs = master.list_etf()

    universe = []
    for etf in all_etfs:
        code = etf["code"]
        name = etf["name"]
        # 6자리 숫자만
        if not code.isdigit() or len(code) != 6:
            continue
        # ETF 브랜드 키워드 매칭
        if not any(brand in name for brand in ETF_BRANDS):
            continue
        universe.append(etf)

    logger.info("ETF 유니버스: %d개 (전체 %d개에서 필터)", len(universe), len(all_etfs))
    return universe


# ── 1시간 수익률 계산 ──────────────────────────────────────

def _calc_1h_return(market: KISMarket, stock_code: str) -> ETFMomentum | None:
    """분봉 데이터로 1시간 수익률 계산.

    KIS API는 최근 30건의 분봉을 반환하므로,
    현재가 = 첫 번째 분봉, 60분 전 ≈ 마지막 분봉 근처에서 추정.
    """
    data = market.get_minute_price(stock_code)
    if not data or len(data) < 2:
        return None

    current = data[0]  # 가장 최근
    current_price = current["price"]
    if current_price <= 0:
        return None

    # 60분 전 가격 찾기: 분봉 시간 기준
    now_time = current["time"]  # "HHMMSS"
    try:
        now_h, now_m = int(now_time[:2]), int(now_time[2:4])
        now_minutes = now_h * 60 + now_m
    except (ValueError, IndexError):
        return None

    # 60분 전에 가장 가까운 분봉 찾기
    target_minutes = now_minutes - 60
    best_match = None
    best_diff = float("inf")

    for d in data[1:]:
        t = d["time"]
        try:
            h, m = int(t[:2]), int(t[2:4])
            dm = now_minutes - (h * 60 + m)
            if dm < 0:
                continue
            diff = abs(dm - 60)
            if diff < best_diff and d["price"] > 0:
                best_diff = diff
                best_match = d
        except (ValueError, IndexError):
            continue

    if best_match is None or best_diff > 15:
        # 60분 전 데이터가 없으면 (장 초반 등) 가장 오래된 데이터 사용
        oldest = [d for d in data if d["price"] > 0]
        if not oldest:
            return None
        best_match = oldest[-1]

    price_1h_ago = best_match["price"]
    if price_1h_ago <= 0:
        return None

    return_1h = (current_price - price_1h_ago) / price_1h_ago * 100

    return ETFMomentum(
        code=stock_code,
        name="",  # 나중에 채움
        current_price=current_price,
        price_1h_ago=price_1h_ago,
        return_1h=round(return_1h, 3),
        volume=current.get("acc_volume", 0),
        scan_time=datetime.now().strftime("%H:%M:%S"),
    )


# ── 메인 스캐너 ──────────────────────────────────────────

def scan_etf_momentum(
    min_volume: int = 10000,
    min_return: float = 0.3,
    api_delay: float = 0.05,
) -> ScanResult:
    """전체 ETF 1시간 모멘텀 스캔.

    Args:
        min_volume: 최소 누적 거래량 (유동성 필터)
        min_return: 매수 최소 수익률 (%)
        api_delay: API 호출 간 대기 시간 (초)

    Returns:
        ScanResult: 랭킹 + 시장 상태 + 추천 종목
    """
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    universe = get_etf_universe()
    name_map = {e["code"]: e["name"] for e in universe}
    codes = [e["code"] for e in universe]

    market = KISMarket()
    results: list[ETFMomentum] = []
    errors = 0
    benchmark_return = 0.0

    try:
        # 1) 벤치마크 (KODEX 200) 먼저 스캔
        bm = _calc_1h_return(market, BENCHMARK_CODE)
        if bm:
            bm.name = BENCHMARK_NAME
            benchmark_return = bm.return_1h
            logger.info("벤치마크 %s 1h 수익률: %.3f%%", BENCHMARK_NAME, benchmark_return)
        else:
            logger.warning("벤치마크 스캔 실패 — 하락장으로 간주")
            benchmark_return = -999

        is_bullish = benchmark_return > 0

        # 2) 전체 ETF 스캔
        for i, code in enumerate(codes):
            if code == BENCHMARK_CODE:
                if bm:
                    results.append(bm)
                continue

            try:
                mom = _calc_1h_return(market, code)
                if mom:
                    mom.name = name_map.get(code, code)
                    results.append(mom)
            except Exception as e:
                errors += 1
                logger.debug("ETF %s 스캔 오류: %s", code, e)

            # API 속도 제한
            if api_delay > 0:
                time.sleep(api_delay)

            # 진행률 로그 (50개마다)
            if (i + 1) % 50 == 0:
                logger.info("스캔 진행: %d/%d (오류: %d)", i + 1, len(codes), errors)

        # 3) 거래량 필터 + 수익률 정렬
        results = [r for r in results if r.volume >= min_volume]
        results.sort(key=lambda x: x.return_1h, reverse=True)

        # 4) 매수 추천 (상승장 + 최소 수익률)
        top_pick = None
        if is_bullish and results and results[0].return_1h >= min_return:
            top_pick = results[0]

    finally:
        market.close()

    scan = ScanResult(
        scan_time=scan_time,
        benchmark_return=benchmark_return,
        is_bullish=is_bullish,
        total_scanned=len(results),
        rankings=results,
        errors=errors,
        top_pick=top_pick,
    )

    logger.info(
        "스캔 완료: %d개 ETF, 벤치마크 %.3f%%, 시장 %s, TOP=%s",
        len(results),
        benchmark_return,
        "상승" if is_bullish else "하락",
        f"{top_pick.name}({top_pick.return_1h:+.3f}%)" if top_pick else "없음",
    )
    return scan


# ── 매매 시그널 생성 ─────────────────────────────────────

@dataclass
class TradeSignal:
    """매매 시그널."""
    action: str           # "buy", "sell", "hold"
    stock_code: str       # 대상 종목 ("" if hold)
    stock_name: str
    reason: str
    return_1h: float      # 해당 종목 1시간 수익률
    benchmark_return: float
    scan_result: ScanResult


def generate_signal(
    scan: ScanResult,
    current_holding: str = "",
    min_return: float = 0.3,
) -> TradeSignal:
    """스캔 결과 + 현재 보유 종목 기반 매매 시그널.

    규칙:
      1. 하락장 (KODEX 200 1h ≤ 0%) → 보유 시 매도, 미보유 시 대기
      2. 상승장 + 1위 ETF 수익률 ≥ min_return → 매수 (로테이션)
      3. 이미 1위를 보유 중 → 유지
    """
    # 규칙 1: 하락장 회피
    if not scan.is_bullish:
        if current_holding:
            return TradeSignal(
                action="sell",
                stock_code=current_holding,
                stock_name=_find_name(scan, current_holding),
                reason=f"하락장 회피 — 벤치마크 1h {scan.benchmark_return:+.3f}%",
                return_1h=0,
                benchmark_return=scan.benchmark_return,
                scan_result=scan,
            )
        return TradeSignal(
            action="hold",
            stock_code="",
            stock_name="",
            reason=f"하락장 대기 — 벤치마크 1h {scan.benchmark_return:+.3f}%",
            return_1h=0,
            benchmark_return=scan.benchmark_return,
            scan_result=scan,
        )

    # 규칙 2: 상승장인데 추천 종목 없음
    if not scan.top_pick:
        return TradeSignal(
            action="hold",
            stock_code=current_holding,
            stock_name=_find_name(scan, current_holding),
            reason=f"상승장이나 {min_return}% 이상 종목 없음",
            return_1h=0,
            benchmark_return=scan.benchmark_return,
            scan_result=scan,
        )

    top = scan.top_pick

    # 규칙 3: 이미 1위 보유 중 → 유지
    if current_holding == top.code:
        return TradeSignal(
            action="hold",
            stock_code=top.code,
            stock_name=top.name,
            reason=f"현재 보유 = 1위 유지 ({top.return_1h:+.3f}%)",
            return_1h=top.return_1h,
            benchmark_return=scan.benchmark_return,
            scan_result=scan,
        )

    # 규칙 4: 로테이션 — 새로운 1위 매수
    return TradeSignal(
        action="buy",
        stock_code=top.code,
        stock_name=top.name,
        reason=f"1시간 상승률 1위 ({top.return_1h:+.3f}%)",
        return_1h=top.return_1h,
        benchmark_return=scan.benchmark_return,
        scan_result=scan,
    )


def _find_name(scan: ScanResult, code: str) -> str:
    for r in scan.rankings:
        if r.code == code:
            return r.name
    return code
