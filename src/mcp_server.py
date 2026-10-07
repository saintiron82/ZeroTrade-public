"""FF Trading Bot MCP Server — KIS API 전체 기능을 Claude 도구로 제공."""

import json
import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.api_log import mcp_logged
from src.kis.market import KISMarket
from src.kis.master import StockMaster
from src.kis.order import KISOrder, OrderType
from src.trade.logger import TradeLogger

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ff-mcp")

mcp = FastMCP("FF Trading Bot", instructions="한국 주식/ETF 자동매매 — KIS API 도구 모음")

# 읽기 전용 모드 — winner-1 등 자율 에이전트 세션용. 쓰기성 도구 미등록.
_READONLY_RAW = os.getenv("FF_MCP_READONLY", "").strip().lower()
# fail-closed: 변수가 설정되어 있기만 하면(명시적 off 제외) 읽기 전용으로 동작
MCP_READONLY = _READONLY_RAW not in ("", "0", "false", "no", "off")


def _web_api_base() -> str:
    """Internal web API base used by MCP management tools."""
    return os.getenv("FF_WEB_API_BASE") or f"http://127.0.0.1:{os.getenv('FF_WEB_PORT', '8080')}"


# ═══════════════════════════════════════════
# 종목 검색 / 마스터
# ═══════════════════════════════════════════

@mcp.tool()
@mcp_logged
def search_stock(keyword: str, limit: int = 10) -> str:
    """종목명 또는 코드로 검색합니다. 종목코드를 모를 때 사용하세요.
    keyword: 검색어 (예: "삼성", "KODEX", "반도체", "005930")
    limit: 결과 개수"""
    master = StockMaster()
    results = master.search(keyword, limit=min(limit, 20))
    if not results:
        return f"'{keyword}'에 해당하는 종목을 찾을 수 없습니다."
    lines = [f"['{keyword}' 검색 결과 ({len(results)}건)]"]
    for s in results:
        lines.append(f"  {s['code']}  {s['name']}  [{s['type']}]  {s['market']}")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def list_etf_stocks(keyword: str = "", limit: int = 20) -> str:
    """ETF 종목 목록을 조회합니다. keyword로 필터 가능.
    keyword: 필터 (예: "KODEX", "레버리지", "반도체"). 비우면 전체.
    limit: 결과 개수"""
    master = StockMaster()
    etfs = master.list_etf()
    if keyword:
        kw = keyword.lower()
        etfs = [e for e in etfs if kw in e["name"].lower()]
    # 6자리 숫자 코드만 (확장코드 제외)
    etfs = [e for e in etfs if e["code"].isdigit() and len(e["code"]) == 6]
    etfs = etfs[:limit]
    if not etfs:
        return f"'{keyword}' ETF를 찾을 수 없습니다."
    lines = [f"[ETF 목록 ({len(etfs)}건)]"]
    for e in etfs:
        lines.append(f"  {e['code']}  {e['name']}")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def get_stock_master_stats() -> str:
    """종목 마스터 통계를 조회합니다 (총 종목수, ETF수, 업데이트 시간)."""
    master = StockMaster()
    s = master.stats
    return (
        f"종목 마스터 현황\n"
        f"  업데이트: {s['updated_at'][:16]}\n"
        f"  총 종목: {s['total']:,}개\n"
        f"  ETF: {s['etf_count']}개\n"
        f"  KOSPI: {s['kospi_count']:,}개\n"
        f"  KOSDAQ: {s['kosdaq_count']:,}개"
    )


# ═══════════════════════════════════════════
# 시세 조회
# ═══════════════════════════════════════════

@mcp.tool()
@mcp_logged
def get_price(stock_code: str) -> str:
    """종목 현재가를 조회합니다.
    stock_code: 6자리 종목코드 (예: 005930=삼성전자, 069500=KODEX200, 373220=LG에너지솔루션)"""
    market = KISMarket()
    try:
        data = market.get_price(stock_code)
        if not data:
            return f"종목 {stock_code} 시세를 조회할 수 없습니다."
        sign = "+" if data["change_rate"] >= 0 else ""
        return (
            f"{data['name']} ({stock_code})\n"
            f"현재가: {data['price']:,}원 ({sign}{data['change_rate']}%)\n"
            f"전일대비: {data['change']:+,}원\n"
            f"시가: {data['open']:,} / 고가: {data['high']:,} / 저가: {data['low']:,}\n"
            f"거래량: {data['volume']:,}\n"
            f"시가총액: {data['market_cap']}억원\n"
            f"PER: {data['per']} / PBR: {data['pbr']}"
        )
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_daily_chart(stock_code: str, period: str = "D", count: int = 20) -> str:
    """종목의 기간별 시세(캔들)를 조회합니다.
    stock_code: 6자리 종목코드
    period: "D"(일봉), "W"(주봉), "M"(월봉), "Y"(년봉)
    count: 조회 개수 (최대 30)"""
    market = KISMarket()
    try:
        data = market.get_daily_price(stock_code, period=period, count=min(count, 30))
        if not data:
            return f"종목 {stock_code} 기간별 시세를 조회할 수 없습니다."
        period_name = {"D": "일봉", "W": "주봉", "M": "월봉", "Y": "년봉"}.get(period, period)
        lines = [f"[{stock_code} {period_name} 최근 {len(data)}개]"]
        lines.append("날짜       | 시가    | 고가    | 저가    | 종가    | 거래량")
        for d in data:
            lines.append(
                f"{d['date']}  {d['open']:>8,}  {d['high']:>8,}  "
                f"{d['low']:>8,}  {d['close']:>8,}  {d['volume']:>12,}"
            )
        return "\n".join(lines)
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_minute_chart(stock_code: str) -> str:
    """종목의 당일 분봉(1분)을 조회합니다.
    stock_code: 6자리 종목코드"""
    market = KISMarket()
    try:
        data = market.get_minute_price(stock_code)
        if not data:
            return f"종목 {stock_code} 분봉을 조회할 수 없습니다."
        lines = [f"[{stock_code} 당일 분봉]"]
        for d in data:
            t = d["time"]
            lines.append(f"  {t[:2]}:{t[2:4]}:{t[4:]}  {d['price']:,}원  거래량:{d['volume']:,}")
        return "\n".join(lines)
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_orderbook(stock_code: str) -> str:
    """종목의 호가(매수/매도 10단계)를 조회합니다.
    stock_code: 6자리 종목코드"""
    market = KISMarket()
    try:
        data = market.get_orderbook(stock_code)
        if not data:
            return f"종목 {stock_code} 호가를 조회할 수 없습니다."
        lines = [f"[{stock_code} 호가]", "  매도호가          |          매수호가"]
        for i in range(9, -1, -1):
            ask = data["asks"][i]
            lines.append(f"  {ask['volume']:>8,} @ {ask['price']:>8,}  |")
        lines.append("  ─────────────────────────────────")
        for i in range(10):
            bid = data["bids"][i]
            lines.append(f"                     |  {bid['price']:>8,} @ {bid['volume']:>8,}")
        lines.append(f"\n  총매도: {data['total_ask_volume']:,}  총매수: {data['total_bid_volume']:,}")
        return "\n".join(lines)
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_investor_trend(stock_code: str) -> str:
    """종목의 투자자별 매매동향(개인/외국인/기관)을 조회합니다.
    stock_code: 6자리 종목코드"""
    market = KISMarket()
    try:
        data = market.get_investor_trend(stock_code)
        if not data:
            return f"종목 {stock_code} 투자자 동향을 조회할 수 없습니다."
        lines = [f"[{stock_code} 투자자별 순매수]", "날짜       | 개인       | 외국인     | 기관"]
        for d in data[:10]:
            lines.append(
                f"{d['date']}  {d['individual_net']:>+10,}  "
                f"{d['foreign_net']:>+10,}  {d['institution_net']:>+10,}"
            )
        return "\n".join(lines)
    finally:
        market.close()


# ═══════════════════════════════════════════
# 순위 / 랭킹
# ═══════════════════════════════════════════

@mcp.tool()
@mcp_logged
def get_ranking(sort_by: str = "rise", market_type: str = "ALL", count: int = 10) -> str:
    """주식 등락률 순위를 조회합니다.
    sort_by: "rise"(상승) 또는 "fall"(하락)
    market_type: "ALL"(전체), "STK"(코스피), "KSQ"(코스닥), "ETF"
    count: 조회 개수 (최대 20)"""
    market = KISMarket()
    try:
        data = market.get_ranking(sort_by=sort_by, market=market_type, count=min(count, 20))
        if not data:
            return "순위 데이터를 조회할 수 없습니다."
        label = f"{'하락' if sort_by == 'fall' else '상승'}률 TOP {len(data)}"
        if market_type != "ALL":
            label = f"{market_type} {label}"
        lines = [f"[{label}]"]
        for item in data:
            sign = "+" if item["change_rate"] >= 0 else ""
            lines.append(
                f"{item['rank']}. {item['name']} ({item['stock_code']}) "
                f"{item['price']:,}원 {sign}{item['change_rate']}% "
                f"거래량:{item['volume']:,}"
            )
        return "\n".join(lines)
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_etf_ranking(sort_by: str = "rise", count: int = 10) -> str:
    """ETF 등락률 순위를 조회합니다.
    sort_by: "rise"(상승) 또는 "fall"(하락)
    count: 조회 개수"""
    market = KISMarket()
    try:
        data = market.get_etf_ranking(sort_by=sort_by, count=min(count, 20))
        if not data:
            return "ETF 순위를 조회할 수 없습니다."
        label = f"ETF {'하락' if sort_by == 'fall' else '상승'}률 TOP {len(data)}"
        lines = [f"[{label}]"]
        for item in data:
            sign = "+" if item["change_rate"] >= 0 else ""
            lines.append(
                f"{item['rank']}. {item['name']} ({item['stock_code']}) "
                f"{item['price']:,}원 {sign}{item['change_rate']}%"
            )
        return "\n".join(lines)
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_volume_ranking(count: int = 10) -> str:
    """거래량 상위 종목을 조회합니다.
    count: 조회 개수"""
    market = KISMarket()
    try:
        data = market.get_volume_ranking(count=min(count, 20))
        if not data:
            return "거래량 순위를 조회할 수 없습니다."
        lines = ["[거래량 TOP]"]
        for item in data:
            sign = "+" if item["change_rate"] >= 0 else ""
            lines.append(
                f"{item['rank']}. {item['name']} ({item['stock_code']}) "
                f"{item['price']:,}원 {sign}{item['change_rate']}% "
                f"거래량:{item['volume']:,}"
            )
        return "\n".join(lines)
    finally:
        market.close()


# ═══════════════════════════════════════════
# 미국주식
# ═══════════════════════════════════════════

@mcp.tool()
@mcp_logged
def search_us_stock(keyword: str, limit: int = 10) -> str:
    """미국주식을 티커 또는 종목명으로 검색합니다.
    keyword: 검색어 (예: "AAPL", "TESLA", "NVIDIA")
    limit: 결과 개수"""
    master = StockMaster()
    results = master.search_us(keyword, limit=min(limit, 20))
    if not results:
        return f"'{keyword}'에 해당하는 미국주식을 찾을 수 없습니다."
    lines = [f"[미국주식 '{keyword}' 검색 ({len(results)}건)]"]
    for s in results:
        lines.append(f"  {s['code']}  {s['name']}  [{s['market']}]")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def get_us_price(symbol: str, exchange: str = "NAS") -> str:
    """미국주식 현재가를 조회합니다.
    symbol: 티커 (예: AAPL, TSLA, MSFT, AMZN)
    exchange: NAS(나스닥), NYS(뉴욕), AMS(아멕스)"""
    market = KISMarket()
    try:
        data = market.get_us_price(symbol, exchange)
        if not data:
            return f"미국주식 {symbol} 시세를 조회할 수 없습니다."
        sign = "+" if data["change_rate"] >= 0 else ""
        return (
            f"{data['name']} ({symbol}) [{data['exchange']}]\n"
            f"현재가: ${data['price']:.2f} ({sign}{data['change_rate']}%)\n"
            f"전일대비: ${data['change']:+.2f}\n"
            f"시가: ${data['open']:.2f} / 고가: ${data['high']:.2f} / 저가: ${data['low']:.2f}\n"
            f"거래량: {data['volume']:,}\n"
            f"PER: {data['per']} / EPS: {data['eps']}"
        )
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_us_daily_chart(symbol: str, exchange: str = "NAS", count: int = 20) -> str:
    """미국주식 일봉을 조회합니다.
    symbol: 티커
    exchange: NAS/NYS/AMS
    count: 조회 개수"""
    market = KISMarket()
    try:
        data = market.get_us_daily_price(symbol, exchange, count=min(count, 30))
        if not data:
            return f"미국주식 {symbol} 일봉을 조회할 수 없습니다."
        lines = [f"[{symbol} 일봉 최근 {len(data)}개]"]
        lines.append("날짜       | 시가     | 고가     | 저가     | 종가     | 거래량")
        for d in data:
            lines.append(
                f"{d['date']}  ${d['open']:>8.2f}  ${d['high']:>8.2f}  "
                f"${d['low']:>8.2f}  ${d['close']:>8.2f}  {d['volume']:>12,}"
            )
        return "\n".join(lines)
    finally:
        market.close()


# ═══════════════════════════════════════════
# 계좌 / 잔고
# ═══════════════════════════════════════════

@mcp.tool()
@mcp_logged
def get_balance() -> str:
    """내 주식 잔고(보유종목)를 조회합니다."""
    market = KISMarket()
    try:
        data = market.get_balance()
        holdings = data.get("holdings", [])
        summary = data.get("summary", {})
        if not holdings:
            cash = summary.get("cash", 0)
            return f"보유 종목이 없습니다.\n예수금: {cash:,}원"
        lines = ["[내 보유 종목]"]
        for h in holdings:
            lines.append(
                f"  {h['name']} ({h['stock_code']}) {h['quantity']}주\n"
                f"    평균가: {h['avg_price']:,}원 → 현재가: {h['current_price']:,}원\n"
                f"    수익률: {h['profit_rate']:+.2f}% ({h['profit_amount']:+,}원)"
            )
        lines.append(f"\n총 평가금액: {summary.get('total_eval', 0):,}원")
        lines.append(f"총 수익: {summary.get('total_profit', 0):+,}원")
        lines.append(f"예수금: {summary.get('cash', 0):,}원")
        return "\n".join(lines)
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_executions() -> str:
    """오늘의 체결(거래) 내역을 조회합니다."""
    market = KISMarket()
    try:
        data = market.get_executions()
        if not data:
            return "오늘 체결 내역이 없습니다."
        lines = ["[오늘 체결 내역]"]
        for e in data:
            side = "매수" if e["side"] == "buy" else "매도"
            t = e["order_time"]
            time_str = f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else t
            lines.append(
                f"  {time_str} {side} {e['stock_name']} ({e['stock_code']}) "
                f"{e['exec_qty']}주 @{e['exec_price']:,}원 "
                f"(총 {e['exec_amount']:,}원) [{e['status']}]"
            )
        return "\n".join(lines)
    finally:
        market.close()


@mcp.tool()
@mcp_logged
def get_pending_orders() -> str:
    """미체결(대기 중) 주문을 조회합니다."""
    market = KISMarket()
    try:
        data = market.get_pending_orders()
        if not data:
            return "미체결 주문이 없습니다."
        lines = ["[미체결 주문]"]
        for o in data:
            side = "매수" if o["side"] == "buy" else "매도"
            t = o["order_time"]
            time_str = f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else t
            lines.append(
                f"  {time_str} {side} {o['stock_name']} ({o['stock_code']}) "
                f"주문 {o['order_qty']}주 @{o['order_price']:,}원 "
                f"(체결 {o['exec_qty']}주, 잔량 {o['remaining_qty']}주)"
            )
        return "\n".join(lines)
    finally:
        market.close()


# ═══════════════════════════════════════════
# 투자 모드 제어
# ═══════════════════════════════════════════

import src.trading_mode as tm


@mcp.tool()
@mcp_logged
def get_trading_mode() -> str:
    """현재 투자 모드를 확인합니다. 주문 전 반드시 확인하세요."""
    info = tm.mode_info()
    mode = info["mode"]
    labels = {"disabled": "DISABLED (주문 차단)", "paper": "PAPER (모의투자)", "live": "LIVE (실전투자)"}
    lines = [
        f"현재 모드: {labels.get(mode, mode)}",
        f"최대 주문액: {info.get('max_order_amount', 0):,}원",
    ]
    if info.get("confirmed_at"):
        lines.append(f"마지막 변경: {info['confirmed_at'][:16]}")
    return "\n".join(lines)


# ═══════════════════════════════════════════
# 주문 (모드별 검증 적용)
# ═══════════════════════════════════════════

if not MCP_READONLY:
    @mcp.tool()
    @mcp_logged
    def buy_stock(stock_code: str, quantity: int, price: int = 0, after_hours: str = "") -> str:
        """주식/ETF를 매수합니다. 현재 투자 모드에 따라 동작이 달라집니다.
        - DISABLED: 주문 차단
        - PAPER: 모의투자 서버로 주문
        - LIVE: 실전 서버로 주문 (실제 돈 사용!)
        stock_code: 6자리 종목코드
        quantity: 매수 수량
        price: 지정가 (0이면 시장가 주문)
        after_hours: 시간외 주문 ("close"=시간외종가 15:40~16:00, "single"=시간외단일가 16:00~18:00, 빈값=정규장)"""
        mode = tm.current_mode()
        if price > 0:
            est_amount = price * quantity
        else:
            market = KISMarket()
            try:
                info = market.get_price(stock_code)
                cur_price = info.get("price", 50000) if info else 50000
            finally:
                market.close()
            est_amount = cur_price * quantity

        ok, msg = tm.validate_order(stock_code, est_amount)
        if not ok:
            return f"[주문 차단] {msg}"

        order = KISOrder()
        try:
            if after_hours == "close":
                order_type = OrderType.AFTER_HOURS_CLOSE
            elif after_hours == "single":
                order_type = OrderType.AFTER_HOURS_SINGLE
            else:
                order_type = OrderType.MARKET if price == 0 else OrderType.LIMIT
            result = order.buy(stock_code=stock_code, quantity=quantity, price=price, order_type=order_type)
            label = "실전투자" if mode == tm.LIVE else "모의투자"
            ah_label = " [시간외종가]" if after_hours == "close" else " [시간외단일가]" if after_hours == "single" else ""
            # DB 기록
            try:
                from src.db import get_db
                get_db().log_trade("", stock_code, "", "buy", quantity, price,
                                   order_no=result.order_no, success=result.success,
                                   reason="AI 채팅 매수", source="ai-chat")
            except Exception:
                pass
            if result.success:
                return f"[{label}]{ah_label} 매수 주문 성공: {stock_code} {quantity}주 (주문번호: {result.order_no})"
            return f"[{label}]{ah_label} 매수 주문 실패: {result.message}"
        finally:
            order.close()


    @mcp.tool()
    @mcp_logged
    def sell_stock(stock_code: str, quantity: int, price: int = 0, after_hours: str = "") -> str:
        """주식/ETF를 매도합니다. 현재 투자 모드에 따라 동작이 달라집니다.
        - DISABLED: 주문 차단
        - PAPER: 모의투자 서버로 주문
        - LIVE: 실전 서버로 주문 (실제 돈 사용!)
        stock_code: 6자리 종목코드
        quantity: 매도 수량
        price: 지정가 (0이면 시장가 주문)
        after_hours: 시간외 주문 ("close"=시간외종가 15:40~16:00, "single"=시간외단일가 16:00~18:00, 빈값=정규장)"""
        mode = tm.current_mode()
        if price > 0:
            est_amount = price * quantity
        else:
            market = KISMarket()
            try:
                info = market.get_price(stock_code)
                cur_price = info.get("price", 50000) if info else 50000
            finally:
                market.close()
            est_amount = cur_price * quantity

        ok, msg = tm.validate_order(stock_code, est_amount)
        if not ok:
            return f"[주문 차단] {msg}"

        order = KISOrder()
        try:
            if after_hours == "close":
                order_type = OrderType.AFTER_HOURS_CLOSE
            elif after_hours == "single":
                order_type = OrderType.AFTER_HOURS_SINGLE
            else:
                order_type = OrderType.MARKET if price == 0 else OrderType.LIMIT
            result = order.sell(stock_code=stock_code, quantity=quantity, price=price, order_type=order_type)
            label = "실전투자" if mode == tm.LIVE else "모의투자"
            ah_label = " [시간외종가]" if after_hours == "close" else " [시간외단일가]" if after_hours == "single" else ""
            try:
                from src.db import get_db
                get_db().log_trade("", stock_code, "", "sell", quantity, price,
                                   order_no=result.order_no, success=result.success,
                                   reason="AI 채팅 매도", source="ai-chat")
            except Exception:
                pass
            if result.success:
                return f"[{label}]{ah_label} 매도 주문 성공: {stock_code} {quantity}주 (주문번호: {result.order_no})"
            return f"[{label}]{ah_label} 매도 주문 실패: {result.message}"
        finally:
            order.close()


    # ═══════════════════════════════════════════
    # 미국주식 주문
    # ═══════════════════════════════════════════

    @mcp.tool()
    @mcp_logged
    def buy_us_stock(symbol: str, quantity: int, price: float = 0, exchange: str = "NAS", order_type: str = "LIMIT") -> str:
        """미국주식을 매수합니다. 현재 투자 모드에 따라 동작이 달라집니다.
        symbol: 티커 (AAPL, TSLA, MSFT 등)
        quantity: 매수 수량
        price: 지정가 (USD). MARKET이면 0
        exchange: NAS(나스닥), NYS(뉴욕), AMS(아멕스)
        order_type: LIMIT(지정가), MARKET(시장가), LOO(장개시지정), MOO(장개시시장), LOC(장마감지정), MOC(장마감시장)"""
        mode = tm.current_mode()
        est_amount = int(price * quantity * 1400) if price > 0 else quantity * 70000  # 원화 추정

        ok, msg = tm.validate_order(symbol, est_amount)
        if not ok:
            return f"[주문 차단] {msg}"

        order = KISOrder()
        try:
            result = order.buy_us(symbol=symbol, quantity=quantity, price=price, exchange=exchange, order_type=order_type)
            label = "실전투자" if mode == tm.LIVE else "모의투자"
            if result.success:
                return f"[{label}] US 매수 성공: {symbol} {quantity}주 @${price:.2f} (주문번호: {result.order_no})"
            return f"[{label}] US 매수 실패: {result.message}"
        finally:
            order.close()


    @mcp.tool()
    @mcp_logged
    def sell_us_stock(symbol: str, quantity: int, price: float = 0, exchange: str = "NAS", order_type: str = "LIMIT") -> str:
        """미국주식을 매도합니다. 현재 투자 모드에 따라 동작이 달라집니다.
        symbol: 티커 (AAPL, TSLA, MSFT 등)
        quantity: 매도 수량
        price: 지정가 (USD). MARKET이면 0
        exchange: NAS(나스닥), NYS(뉴욕), AMS(아멕스)
        order_type: LIMIT(지정가), MARKET(시장가), LOO, MOO, LOC, MOC"""
        mode = tm.current_mode()
        est_amount = int(price * quantity * 1400) if price > 0 else quantity * 70000

        ok, msg = tm.validate_order(symbol, est_amount)
        if not ok:
            return f"[주문 차단] {msg}"

        order = KISOrder()
        try:
            result = order.sell_us(symbol=symbol, quantity=quantity, price=price, exchange=exchange, order_type=order_type)
            label = "실전투자" if mode == tm.LIVE else "모의투자"
            if result.success:
                return f"[{label}] US 매도 성공: {symbol} {quantity}주 @${price:.2f} (주문번호: {result.order_no})"
            return f"[{label}] US 매도 실패: {result.message}"
        finally:
            order.close()


# ═══════════════════════════════════════════
# 매매 이력
# ═══════════════════════════════════════════

@mcp.tool()
@mcp_logged
def get_trade_history(count: int = 10) -> str:
    """최근 매매 이력을 조회합니다."""
    tl = TradeLogger()
    trades = tl.recent(count)
    if not trades:
        return "매매 이력이 없습니다."
    lines = ["[최근 매매 이력]"]
    for t in trades:
        status = "성공" if t["success"] else "실패"
        lines.append(
            f"{t['timestamp'][:16]} {t['side'].upper()} {t['stock_code']} "
            f"{t['quantity']}주 @{t['price']:,}원 [{status}] {t.get('reason', '')}"
        )
    summary = tl.summary()
    lines.append(f"\n총 {summary['total']}건 / 성공률 {summary['success_rate']:.0%}")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def get_trade_summary() -> str:
    """매매 요약 통계를 조회합니다."""
    tl = TradeLogger()
    s = tl.summary()
    return (
        f"총 매매: {s['total']}건\n"
        f"매수: {s['buys']}건 / 매도: {s['sells']}건\n"
        f"성공률: {s['success_rate']:.0%}"
    )


@mcp.tool()
@mcp_logged
def get_audit_kpi(bot_id: str = "", days: int = 30) -> str:
    """감사 DB 기반 봇 KPI를 조회합니다. bot_id를 비우면 전체."""
    from src.db import get_db

    kpi = get_db().get_audit_kpi(bot_id=bot_id, days=days)
    if kpi["closed"] == 0:
        return "감사 DB 청산 결과가 없습니다."
    profit_factor = kpi["profit_factor"]
    profit_factor_text = "N/A" if profit_factor is None else f"{profit_factor:.2f}"

    lines = [
        f"[감사 DB KPI - {bot_id or '전체'} 최근 {days}일]",
        f"청산: {kpi['closed']}건 / 승 {kpi['wins']} / 패 {kpi['losses']}",
        f"승률: {kpi['win_rate']:.1%}",
        f"총손익: {kpi['total_pnl']:+,.0f}원",
        f"거래당 손익: {kpi['avg_pnl']:+,.0f}원",
        f"거래당 수익률: {kpi['avg_return_pct']:+.2f}%",
        f"Profit Factor: {profit_factor_text}",
        "",
        "[종목별]",
    ]
    for code, item in sorted(kpi["by_code"].items(), key=lambda x: x[1]["pnl"]):
        win_rate = item["wins"] / item["closed"] if item["closed"] else 0
        lines.append(
            f"  {code}: {item['closed']}건, 승률 {win_rate:.1%}, "
            f"손익 {item['pnl']:+,.0f}원"
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════
# 전략 관리
# ═══════════════════════════════════════════

from src.strategy import StrategyManager
from src.decision_log import DecisionLog


if not MCP_READONLY:
    @mcp.tool()
    @mcp_logged
    def create_strategy(name: str, budget: int, description: str = "", bot_id: str = "", bot_params: str = "") -> str:
        """파이프(전략)를 생성합니다. 독립 예산 + 봇 연결.
        name: 파이프명 (예: "ETF 공격형")
        budget: 배정 예산 (원)
        description: 설명
        bot_id: 연결할 봇 ID (예: etf-momentum-top1). list_bots()로 확인.
        bot_params: 봇 설정 JSON (예: '{"interval_seconds": 300}')"""
        mgr = StrategyManager()
        try:
            s = mgr.create_strategy(name=name, budget=budget, description=description)
            if bot_id:
                import json as _json
                params = _json.loads(bot_params) if bot_params else {}
                mgr.link_bot(s.id, bot_id, params)
            return (
                f"파이프 생성 완료!\n"
                f"  ID: {s.id}\n"
                f"  이름: {s.name}\n"
                f"  예산: {s.budget:,}원 (현금: {s.cash:,}원)\n"
                f"  봇: {bot_id or '미연결'}\n"
                f"\n총 예산: {mgr.portfolio.total_budget:,}원 / "
                f"배정: {mgr.portfolio.allocated():,}원 / "
                f"미배정: {mgr.portfolio.unallocated():,}원"
            )
        except ValueError as e:
            return f"파이프 생성 실패: {e}"


@mcp.tool()
@mcp_logged
def list_strategies() -> str:
    """등록된 모든 파이프(전략)를 조회합니다. 각 파이프의 자산 현황 포함."""
    mgr = StrategyManager()
    strategies = mgr.list_strategies()
    if not strategies:
        return "등록된 파이프가 없습니다. create_strategy로 파이프를 만들어주세요."
    lines = [f"[파이프 목록] (총 예산: {mgr.portfolio.total_budget:,}원)"]
    for s in strategies:
        status = "실행중" if s.active else "정지"
        holdings_str = ", ".join(f"{h.get('code','')} {h.get('qty',0)}주" for h in s.holdings) if s.holdings else "없음"
        lines.append(
            f"\n  {s.id}: {s.name} [{status}]\n"
            f"    예산: {s.budget:,}원 / 현금: {s.cash:,}원\n"
            f"    보유: {holdings_str}\n"
            f"    실현손익: {s.realized_pnl:+,}원 / 거래: {len(s.trades)}건\n"
            f"    봇: {s.bot_id or '미연결'}\n"
            f"    설명: {s.description}"
        )
    lines.append(f"\n미배정 예산: {mgr.portfolio.unallocated():,}원")
    return "\n".join(lines)


if not MCP_READONLY:
    @mcp.tool()
    @mcp_logged
    def set_total_budget(amount: int) -> str:
        """총 투자 예산을 설정합니다.
        amount: 총 예산 (원)"""
        mgr = StrategyManager()
        mgr.set_total_budget(amount)
        return f"총 투자 예산 설정: {amount:,}원"


    @mcp.tool()
    @mcp_logged
    def update_pipe(pipe_id: str, budget: int = -1) -> str:
        """파이프 예산을 변경합니다.
        pipe_id: 파이프 ID (예: strat-001). list_strategies()로 확인.
        budget: 새 예산 (원). -1이면 변경 안 함."""
        mgr = StrategyManager()
        s = mgr.get(pipe_id)
        if not s:
            return f"파이프 '{pipe_id}' 없음. list_strategies()로 확인하세요."
        updates = {}
        if budget >= 0:
            updates["budget"] = budget
            updates["cash"] = budget
        if not updates:
            return "변경할 항목이 없습니다."
        mgr.update_strategy(pipe_id, **updates)
        s = mgr.get(pipe_id)
        return (
            f"파이프 '{s.name}' 설정 변경 완료\n"
            f"  예산: {s.budget:,}원"
        )


    @mcp.tool()
    @mcp_logged
    def start_pipe(pipe_id: str) -> str:
        """파이프를 독립 프로세스로 가동합니다. 연결된 봇이 실제 매매를 시작합니다.
        pipe_id: 파이프 ID (예: strat-001). list_strategies()로 확인."""
        import httpx
        mgr = StrategyManager()
        s = mgr.get(pipe_id)
        if not s:
            return f"파이프 '{pipe_id}' 없음."
        if not s.bot_id and not s.bots:
            return f"파이프 '{s.name}'에 연결된 봇이 없습니다. link_bot()으로 봇 연결 후 시도하세요."
        try:
            resp = httpx.post(f"{_web_api_base()}/api/pipes/{pipe_id}/start", timeout=10)
            data = resp.json()
            if data.get("success"):
                return f"파이프 '{s.name}' 가동 완료 (봇: {s.bot_id})"
            return f"파이프 시작 실패: {data.get('message', '알 수 없는 오류')}"
        except Exception as e:
            return f"파이프 시작 오류: {e}"


    @mcp.tool()
    @mcp_logged
    def stop_pipe(pipe_id: str) -> str:
        """파이프를 정지합니다. 실행 중인 봇이 종료됩니다.
        pipe_id: 파이프 ID (예: strat-001)."""
        import httpx
        mgr = StrategyManager()
        s = mgr.get(pipe_id)
        if not s:
            return f"파이프 '{pipe_id}' 없음."
        try:
            resp = httpx.post(f"{_web_api_base()}/api/pipes/{pipe_id}/stop", timeout=10)
            data = resp.json()
            if data.get("success"):
                return f"파이프 '{s.name}' 정지 완료"
            return f"파이프 정지 실패: {data.get('message', '알 수 없는 오류')}"
        except Exception as e:
            return f"파이프 정지 오류: {e}"


    @mcp.tool()
    @mcp_logged
    def link_bot(pipe_id: str, bot_id: str, ratio: float = 1.0, params_json: str = "") -> str:
        """파이프에 봇을 연결합니다. 봇이 이미 있으면 추가(복수 봇), 비율 지정 필수.
        pipe_id: 파이프 ID. bot_id: 봇 ID (list_bots()로 확인). ratio: 투자 비율 (0.0~1.0).
        params_json: 봇 파라미터 JSON 문자열 (예: '{\"interval_seconds\":120}')."""
        import json as _json
        from src.bots import registry as bot_reg
        mgr = StrategyManager()
        s = mgr.get(pipe_id)
        if not s:
            return f"파이프 '{pipe_id}' 없음."
        bot_info = bot_reg.get(bot_id)
        if not bot_info:
            registered = [b.id for b in bot_reg.list_all()]
            return f"봇 '{bot_id}' 없음. 등록된 봇: {registered}"
        params = {}
        if params_json:
            try:
                params = _json.loads(params_json)
            except Exception:
                return "params_json JSON 파싱 오류. 올바른 JSON 문자열을 입력하세요."
        ratio = max(0.01, min(1.0, ratio))
        if s.bots:
            mgr.add_bot(pipe_id, bot_id, ratio, params)
        else:
            mgr.link_bot(pipe_id, bot_id, params)
            if ratio < 1.0:
                mgr.set_bot_ratio_rebalance(pipe_id, bot_id, ratio)
        s = mgr.get(pipe_id)
        bots_summary = ", ".join(f"{b['bot_id']}({int(b['ratio']*100)}%)" for b in s.bots)
        return f"파이프 '{s.name}'에 '{bot_id}' 연결 완료\n  현재 봇: {bots_summary}"


    @mcp.tool()
    @mcp_logged
    def set_bot_ratio(pipe_id: str, bot_id: str, ratio: float) -> str:
        """봇 투자 비율을 변경합니다. 나머지 봇들은 자동으로 비율이 재조정됩니다.
        pipe_id: 파이프 ID. bot_id: 봇 ID. ratio: 새 비율 (0.01~0.99)."""
        mgr = StrategyManager()
        s = mgr.get(pipe_id)
        if not s:
            return f"파이프 '{pipe_id}' 없음."
        if not any(b.get("bot_id") == bot_id for b in s.bots):
            return f"봇 '{bot_id}'가 파이프 '{pipe_id}'에 없습니다."
        mgr.set_bot_ratio_rebalance(pipe_id, bot_id, ratio)
        s = mgr.get(pipe_id)
        lines = [f"파이프 '{s.name}' 봇 비율 재조정:"]
        for b in s.bots:
            budget = int(s.budget * b["ratio"])
            lines.append(f"  {b['bot_id']}: {int(b['ratio']*100)}% ({budget:,}원)")
        return "\n".join(lines)


@mcp.tool()
@mcp_logged
def get_strategy_performance(strategy_id: str) -> str:
    """특정 전략의 성과를 조회합니다.
    strategy_id: 전략 ID (예: strat-001)"""
    mgr = StrategyManager()
    s = mgr.get(strategy_id)
    if not s:
        return f"전략 {strategy_id}을 찾을 수 없습니다."

    tl = TradeLogger()
    trades = tl.by_strategy(strategy_id)
    summary = tl.summary(strategy_id)

    lines = [f"[{s.name} 성과]"]
    lines.append(f"  예산: {s.budget:,}원 / 사용: {s.used:,}원")
    lines.append(f"  총 매매: {summary['total']}건")
    lines.append(f"  매수: {summary['buys']}건 / 매도: {summary['sells']}건")
    lines.append(f"  성공률: {summary['success_rate']:.0%}")

    if trades:
        lines.append("\n  최근 거래:")
        for t in trades[-5:]:
            status = "O" if t["success"] else "X"
            lines.append(
                f"    [{status}] {t['timestamp'][:16]} {t['side']} "
                f"{t['stock_code']} {t['quantity']}주"
            )
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def get_portfolio_status() -> str:
    """포트폴리오 현황을 조회합니다 (전체 전략 자금 배분)."""
    mgr = StrategyManager()
    p = mgr.portfolio
    lines = [
        f"[포트폴리오 현황]",
        f"  총 예산: {p.total_budget:,}원",
        f"  배정: {p.allocated():,}원",
        f"  미배정: {p.unallocated():,}원",
        f"  전략 수: {len(p.strategies)}개",
    ]
    for s in p.strategies.values():
        pct = (s.used / s.budget * 100) if s.budget > 0 else 0
        bar_len = 20
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"\n  {s.name} ({s.id})")
        lines.append(f"    [{bar}] {pct:.0f}% ({s.used:,}/{s.budget:,}원)")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def get_decision_log(count: int = 10, strategy_id: str = "") -> str:
    """AI 판단 이력을 조회합니다.
    count: 조회 개수
    strategy_id: 특정 전략만 (비워두면 전체)"""
    dl = DecisionLog()
    entries = dl.by_strategy(strategy_id) if strategy_id else dl.recent(count)
    if not entries:
        return "판단 이력이 없습니다."
    lines = ["[AI 판단 이력]"]
    for e in entries[-count:]:
        lines.append(
            f"  {e['timestamp'][:16]} [{e['type']}] "
            f"전략:{e['strategy_id'] or '-'} → {e['action_taken']}"
        )
        if e.get("note"):
            lines.append(f"    메모: {e['note']}")
    return "\n".join(lines)


# ═══════════════════════════════════════════
# 봇 레지스트리 / ETF 모멘텀 봇
# ═══════════════════════════════════════════

import asyncio
from dataclasses import asdict
from src.bot_manager import get_bot_manager
from src.bots import registry as bot_registry


if not MCP_READONLY:
    @mcp.tool()
    @mcp_logged
    def create_bot(description: str) -> str:
        """AI가 새 봇을 설계하고 생성합니다. 전략 설명을 자연어로 전달하면 봇 코드가 자동 생성됩니다.
        description: 봇 전략 설명 (예: "RSI 30 이하에서 ETF 매수, 70 이상에서 매도하는 봇")"""
        from src.services.bot_builder import BotBuilder
        builder = BotBuilder()
        result = builder.build(description)
        if result.get("success"):
            return (
                f"봇 생성 성공!\n"
                f"  ID: {result['bot_id']}\n"
                f"  이름: {result['name']}\n"
                f"  설명: {result['description']}\n"
                f"  코드: {result['file']}\n"
                f"\n파이프에 연결하여 사용: link_bot(pipe_id, '{result['bot_id']}')"
            )
        return f"봇 생성 실패: {result.get('error', '알 수 없는 오류')}"


    @mcp.tool()
    @mcp_logged
    def design_bot(description: str, preset: str = "") -> str:
        """자연어로 봇을 설계합니다. AI가 조건을 분석하여 템플릿 봇을 자동 생성합니다.
        코드를 생성하지 않고, 검증된 템플릿에 조건만 채워 안전한 봇을 만듭니다.
        description: 전략 설명 (예: "RSI 30 이하에서 ETF 매수, 거래량 2배 이상일 때만")
        preset: 프리셋 사용 시 이름 (거래량_폭발, RSI_과매도, BB_하단_반등, 골든크로스). 비우면 AI 설계."""
        import json
        from src.services.bot_designer import BotDesigner
        designer = BotDesigner()

        if preset:
            result = designer.design_from_preset(preset)
        else:
            result = designer.design(description)

        if not result.get("success"):
            return f"봇 설계 실패: {result.get('error', '알 수 없는 오류')}"

        spec = result["spec"]

        # 저장 + 등록
        save_result = designer.save_and_register(spec)
        if not save_result.get("success"):
            return f"봇 저장 실패: {save_result.get('error', '')}"

        entry_conds = spec.get("entry", {}).get("conditions", [])
        exit_conds = spec.get("exit", {}).get("conditions", [])
        cond_summary = "\n".join(f"  - {c['indicator']} {c['operator']} {c['value']}" for c in entry_conds)
        exit_summary = "\n".join(f"  - {c['indicator']} {c['operator']} {c['value']}" for c in exit_conds)

        return (
            f"봇 설계 완료!\n"
            f"  ID: {spec['bot_id']}\n"
            f"  이름: {spec.get('name', '')}\n"
            f"  설명: {spec.get('description', '')}\n"
            f"  카테고리: {spec.get('category', '')}\n"
            f"\n진입 조건 ({spec.get('entry', {}).get('logic', 'AND')}):\n{cond_summary}\n"
            f"\n청산 조건:\n"
            f"  트레일링: {spec.get('exit', {}).get('trailing_stop_pct', '')}%\n"
            f"  익절: +{spec.get('exit', {}).get('take_profit_pct', '')}%\n"
            f"  손절: {spec.get('exit', {}).get('hard_stop_pct', '')}%\n"
            f"{f'  조건: {exit_summary}' if exit_summary else ''}\n"
            f"\n파이프에 연결: link_bot(pipe_id, '{spec['bot_id']}')"
        )


@mcp.tool()
@mcp_logged
def list_bots() -> str:
    """등록된 모든 자동매매 봇 목록을 조회합니다. 이름, 카테고리, 위험도, 설명을 확인할 수 있습니다."""
    bots = bot_registry.list_all()
    if not bots:
        return "등록된 봇이 없습니다."
    lines = [f"[등록된 봇 — {len(bots)}개]"]
    for b in bots:
        risk = "+" * b.risk_level + "-" * (5 - b.risk_level)
        lines.append(
            f"\n  [{b.id}] {b.name} v{b.version}"
            f"\n    카테고리: {b.category.value} | 실행: {b.bot_type.value} | 시간대: {b.market_session.value}"
            f"\n    위험도: [{risk}] ({b.risk_level}/5)"
            f"\n    설명: {b.description}"
            f"\n    태그: {', '.join(b.tags)}"
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════
# 뉴스
# ═══════════════════════════════════════════

@mcp.tool()
@mcp_logged
def get_news(count: int = 10, source: str = "") -> str:
    """최근 증권/경제 뉴스를 조회합니다. 자동 수집된 RSS 뉴스.
    count: 조회 개수
    source: 소스 필터 (한국경제_증권, 매일경제_증권 등. 비우면 전체)"""
    from src.db import get_db
    news = get_db().get_news(count=count, source=source)
    if not news:
        return "수집된 뉴스가 없습니다. 서버 시작 후 10분 뒤 자동 수집됩니다."
    lines = [f"[증권 뉴스 최근 {len(news)}건]"]
    for n in news:
        ts = n.get("published_iso", n.get("timestamp", ""))[:16]
        src = n.get("source", "")
        pin = "PIN " if n.get("kept") else ""
        lines.append(f"\n[{pin}{src}] {ts}")
        lines.append(f"  {n.get('title', '')}")
        summary = n.get("summary", "")
        if summary:
            lines.append(f"  {summary[:100]}...")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def search_news(keyword: str, count: int = 10) -> str:
    """뉴스를 키워드로 검색합니다.
    keyword: 검색어 (예: "삼성전자", "반도체", "금리")
    count: 결과 개수"""
    from src.db import get_db
    news = get_db().search_news(keyword=keyword, count=count)
    if not news:
        return f"'{keyword}' 관련 뉴스가 없습니다."
    lines = [f"['{keyword}' 뉴스 검색 ({len(news)}건)]"]
    for n in news:
        lines.append(f"  [{n.get('source','')}] {n.get('title','')}")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def search_news_semantic(query: str, count: int = 10) -> str:
    """뉴스를 의미(시맨틱) 기반으로 검색합니다. 키워드 일치가 아닌 의미적 유사도로 찾습니다.
    query: 검색 질의 (예: "반도체 수출 실적", "미국 금리 인하 영향", "전기차 배터리 공급망")
    count: 결과 개수"""
    from src.news.search import get_search_engine
    results = get_search_engine().hybrid_search(query=query, top_k=count)
    if not results:
        return f"'{query}' 관련 뉴스를 찾을 수 없습니다."
    lines = [f"['{query}' 시맨틱 검색 ({len(results)}건)]"]
    for n in results:
        score = n.get("relevance", 0)
        cat = n.get("category", "")
        sent = n.get("sentiment", "")
        tags = n.get("tags", "")
        lines.append(f"\n  [{cat}] [{sent}] (유사도:{score:.2f})")
        lines.append(f"  {n.get('title', '')}")
        if n.get("summary"):
            lines.append(f"  {n['summary'][:120]}...")
        if tags:
            lines.append(f"  태그: {tags}")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def get_news_by_category(category: str, count: int = 10) -> str:
    """카테고리별 최신 뉴스를 조회합니다.
    category: 카테고리 (반도체/IT, 금리/환율, 미국증시, 에너지, 자동차, 바이오, 코스피/코스닥, 정책/규제, ETF)
    count: 결과 개수"""
    from src.db import get_db
    news = get_db().get_news_filtered(category=category, count=count)
    if not news:
        return f"'{category}' 카테고리 뉴스가 없습니다."
    lines = [f"[{category} 뉴스 {len(news)}건]"]
    for n in news:
        sent = n.get("sentiment", "")
        sent_mark = {"positive": "+", "negative": "-"}.get(sent, " ")
        pin = "PIN " if n.get("kept") else ""
        lines.append(f"  [{pin}{sent_mark}] {n.get('title', '')}")
        tags = n.get("tags", "")
        if tags:
            lines.append(f"    태그: {tags}")
    return "\n".join(lines)


# ── 에볼버 봇 ──

if not MCP_READONLY:
    @mcp.tool()
    @mcp_logged
    def run_evolver() -> str:
        """자동 조율 봇을 즉시 1회 실행합니다. AI가 봇 성과를 분석하고 파라미터를 조정합니다."""
        from src.bots.evolver import Evolver
        ev = Evolver()
        results = ev.run()
        output = results.get("output", "")
        gen = results.get("generation", 0)
        if output.startswith("ERROR:"):
            return f"자동 조율 실패: {output}"
        # AI 분석 결과를 그대로 반환 (이미 한국어)
        return f"[자동 조율 — 세대 {gen}]\n\n{output}"


@mcp.tool()
@mcp_logged
def evolver_status() -> str:
    """에볼버 현재 상태를 조회합니다 (진화 세대, 관리 봇 목록, 현재 config)."""
    from src.bots.evolver import Evolver
    ev = Evolver()
    status = ev.get_status()
    lines = [f"[에볼버 상태 — 세대 {status['generation']}]"]
    for bot_id, cfg in status.get("current_configs", {}).items():
        lines.append(f"\n  {bot_id}:")
        if cfg:
            for k, v in cfg.items():
                if not k.startswith("_"):
                    lines.append(f"    {k}: {v}")
        else:
            lines.append("    (기본값 사용 중)")
    return "\n".join(lines)


# ── 봇 config 수정 ──

if not MCP_READONLY:
    @mcp.tool()
    @mcp_logged
    def save_bot_config(bot_id: str, params_json: str) -> str:
        """봇의 파라미터를 수정합니다. 에볼버(자동 조율)가 봇 파라미터를 조정할 때 사용합니다.
        bot_id: 봇 ID (예: stealth-volume, band-rebound)
        params_json: 수정할 파라미터 JSON (예: '{"vol_threshold": 3.5, "trailing_stop_pct": -1.0}')
        기존 값에 머지됩니다 (명시한 키만 변경)."""
        import json as _json
        from src.bots.safeguards import load_bot_config, save_bot_config as _save

        try:
            new_params = _json.loads(params_json)
        except _json.JSONDecodeError as e:
            return f"JSON 파싱 오류: {e}"

        if not isinstance(new_params, dict):
            return "파라미터는 JSON 객체여야 합니다."

        # 기존 config 로드 후 머지
        current = load_bot_config(bot_id)
        old_snapshot = {k: v for k, v in current.items() if not k.startswith("_")}

        # ±20% 범위 검증
        violations = []
        for key, new_val in new_params.items():
            if key in current and isinstance(current[key], (int, float)) and isinstance(new_val, (int, float)):
                old_val = current[key]
                if old_val != 0:
                    change_ratio = abs(new_val - old_val) / abs(old_val)
                    if change_ratio > 0.20:
                        violations.append(f"  {key}: {old_val} → {new_val} (변경 {change_ratio:.0%}, 한도 ±20% 초과)")

        if violations:
            return "변경 폭 초과 (±20% 이내만 허용):\n" + "\n".join(violations)

        current.update(new_params)
        from datetime import datetime
        current["_updated_at"] = datetime.now().isoformat()
        _save(bot_id, current)

        # 변경 내역 표시
        changes = []
        for key, new_val in new_params.items():
            old_val = old_snapshot.get(key, "(없음)")
            changes.append(f"  {key}: {old_val} → {new_val}")

        return f"[{bot_id}] config 저장 완료:\n" + "\n".join(changes)


# ── 토큰 사용량 ──

@mcp.tool()
@mcp_logged
def get_token_usage(bot_id: str = "", days: int = 7) -> str:
    """AI 봇의 토큰 사용량을 조회합니다.
    bot_id: 특정 봇만 (비워두면 전체)
    days: 조회 기간 (기본 7일)"""
    from src.bots.ai_caller import get_token_stats
    stats = get_token_stats(bot_id, days)
    if stats["total_calls"] == 0:
        return "토큰 사용 이력이 없습니다."
    lines = [f"[토큰 사용량 — 최근 {days}일]"]
    lines.append(f"  총 토큰: {stats['total_tokens']:,}")
    lines.append(f"  총 호출: {stats['total_calls']}회")
    lines.append(f"  호출당 평균: {stats['total_tokens'] // max(stats['total_calls'], 1):,}")
    if stats["by_bot"]:
        lines.append("\n  봇별:")
        for bid, data in sorted(stats["by_bot"].items(), key=lambda x: x[1]["tokens"], reverse=True):
            lines.append(f"    {bid}: {data['tokens']:,} tokens ({data['calls']}회)")
    return "\n".join(lines)


# ── 시장 시간 ──

@mcp.tool()
@mcp_logged
def get_market_session() -> str:
    """현재 시장 세션 정보를 조회합니다 (KST 기준). 거래 가능 시간, 매수/매도 가능 여부, 마감까지 남은 시간 등."""
    from src.bots.safeguards import get_market_session as _get
    s = _get()
    lines = [f"[시장 세션: {s['label']}] (KST 한국시간 기준)"]
    lines.append(f"  상태: {s['status']}")
    lines.append(f"  세션: {s['session']}")
    lines.append(f"  매수 가능: {'O' if s['can_buy'] else 'X'}")
    lines.append(f"  매도 가능: {'O' if s['can_sell'] else 'X'}")
    lines.append(f"  마감까지: {s['minutes_to_close']:.0f}분")
    lines.append(f"  현재 시간: {s['current_time']}")
    lines.append(f"  한국 정규장: 09:00~15:30 KST")
    lines.append(f"  권장: {s['recommended_action']}")
    return "\n".join(lines)


# ── ETF 시그널 ──

@mcp.tool()
@mcp_logged
def get_etf_signals() -> str:
    """ETF별 뉴스 감성 + 방향 시그널을 조회합니다.
    점수: -10(강한 매도) ~ +10(강한 매수). 뉴스 카테고리와 키워드를 ETF에 매칭하여 산출."""
    from src.services.etf_signal import get_etf_signals as _get
    signals = _get()
    if not signals:
        return "시그널 없음"
    lines = ["[ETF 시그널]"]
    for code, s in sorted(signals.items(), key=lambda x: x[1].get("score", 0), reverse=True):
        score = s.get("score", 0)
        direction = s.get("direction", "?")
        icon = "🟢" if score > 2 else "🔴" if score < -2 else "⚪"
        lines.append(f"  {icon} {code} {s.get('name','')}: {score:+.1f} ({direction})")
    return "\n".join(lines)


# ── 산업 지수 ──


@mcp.tool()
@mcp_logged
def get_industry_index(sector: str = "", sub_sector: str = "") -> str:
    """산업별 종합 지수를 조회합니다. 뉴스 감성/볼륨/속도/집중도를 종합한 복합 점수 (-10~+10).
    sector: 섹터 필터 (예: "반도체/IT"). 비우면 전체.
    sub_sector: 서브섹터 필터 (예: "AI반도체"). 비우면 섹터 전체.
    섹터 목록: 반도체/IT, 에너지, 금리/환율, 미국증시, 자동차, 바이오, 코스피/코스닥, 정책/규제, ETF"""
    from src.services.industry_index import get_industry_index as _get, format_index_for_human
    indices = _get()
    if sector:
        indices = {k: v for k, v in indices.items() if k == sector}
    if not indices:
        return f"'{sector}' 산업 지수 없음"
    return format_index_for_human(indices, sub_sector_filter=sub_sector)


@mcp.tool()
@mcp_logged
def get_index_history(sector: str, hours: int = 24) -> str:
    """산업 지수 변화 이력을 조회합니다. 시간대별 추이 확인용.
    sector: 섹터명 (예: "반도체/IT")
    hours: 조회 ���간 (기본 24시간)"""
    from src.services.industry_index import get_index_history as _get_hist
    history = _get_hist(sector=sector, hours=hours)
    if not history:
        return f"'{sector}' 이력 없음 (스냅샷이 아직 축적되지 않았을 수 있습니다)"
    lines = [f"[{sector} 지수 추이 ({hours}시간)]"]
    for snap in history[-20:]:
        ts = snap["timestamp"][:16]
        lines.append(f"  {ts}  {snap['score']:+.1f} ({snap['direction']})  뉴스{snap['news_count']}건")
    return "\n".join(lines)


# ── 봇 룰 관리 ──

_BOT_RULES_DIR = Path(__file__).resolve().parent.parent / "data" / "bot_rules"


@mcp.tool()
@mcp_logged
def list_bot_rules() -> str:
    """등록된 봇 룰 파일 목록을 조회합니다. 각 봇의 거래 규칙이 정의된 파일입니다."""
    if not _BOT_RULES_DIR.exists():
        return "봇 룰 디렉토리가 없습니다."
    rules = sorted(_BOT_RULES_DIR.glob("*.md"))
    if not rules:
        return "등록된 봇 룰이 없습니다."
    lines = [f"[봇 룰 — {len(rules)}개]"]
    for path in rules:
        bot_id = path.stem
        # 첫 줄에서 제목 추출
        first_line = path.read_text(encoding="utf-8").split("\n")[0].strip("# ").strip()
        lines.append(f"\n  [{bot_id}] {first_line}")
        lines.append(f"    경로: data/bot_rules/{path.name}")
    return "\n".join(lines)


@mcp.tool()
@mcp_logged
def get_bot_rule(bot_id: str) -> str:
    """특정 봇의 거래 룰을 조회합니다.
    bot_id: 봇 ID (예: stealth-volume, band-rebound, evolver)"""
    path = _BOT_RULES_DIR / f"{bot_id}.md"
    if not path.exists():
        available = [p.stem for p in _BOT_RULES_DIR.glob("*.md")] if _BOT_RULES_DIR.exists() else []
        return f"봇 '{bot_id}'의 룰 파일이 없습니다.\n사용 가능: {', '.join(available)}"
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":
    mcp.run()
