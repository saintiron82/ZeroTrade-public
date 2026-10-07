"""KIS REST API 주문 모듈 — 국내주식/ETF 매수/매도."""

import logging
from dataclasses import dataclass
from enum import Enum

import httpx

from src.config import settings
from src.kis.auth import KISAuth

logger = logging.getLogger(__name__)


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "01"   # 시장가
    LIMIT = "00"    # 지정가
    AFTER_HOURS_CLOSE = "05"  # 시간외 종가 (15:40~16:00)
    AFTER_HOURS_SINGLE = "06"  # 시간외 단일가 (16:00~18:00)


@dataclass
class OrderResult:
    """주문 결과."""
    success: bool
    order_no: str = ""
    message: str = ""
    stock_code: str = ""
    side: str = ""
    quantity: int = 0
    price: int = 0


class KISOrder:
    """KIS 국내주식/ETF 주문.

    TradingMode에 따라 모의/실전 자동 분기.
    """

    def __init__(self):
        import src.trading_mode as tm
        self._auth = KISAuth()
        self._cfg = settings.kis
        self._mode = tm.current_mode()

        # LIVE면 실전 서버/키 사용
        if self._mode == tm.LIVE and self._cfg.real_app_key:
            from src.kis.market import _RealAuth, REAL_BASE_URL
            self._auth = _RealAuth()
            self._base_url = REAL_BASE_URL
        else:
            self._base_url = self._cfg.base_url
        self._client = httpx.Client(base_url=self._base_url, timeout=10)

    def buy(
        self,
        stock_code: str,
        quantity: int = 0,
        price: int = 0,
        amount: int = 0,
        order_type: OrderType = OrderType.MARKET,
    ) -> OrderResult:
        """매수 주문.

        Args:
            stock_code: 종목코드 (주식/ETF)
            quantity: 수량 (지정 시 사용)
            price: 지정가 (order_type=LIMIT일 때)
            amount: 매수 금액 (수량 대신 금액으로 주문 시)
            order_type: 주문유형 (시장가/지정가)
        """
        if amount > 0 and quantity == 0 and price > 0:
            quantity = amount // price

        return self._place_order(stock_code, OrderSide.BUY, quantity, price, order_type)

    def sell(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        order_type: OrderType = OrderType.MARKET,
    ) -> OrderResult:
        """매도 주문."""
        return self._place_order(stock_code, OrderSide.SELL, quantity, price, order_type)

    def _place_order(
        self,
        stock_code: str,
        side: OrderSide,
        quantity: int,
        price: int,
        order_type: OrderType,
    ) -> OrderResult:
        """주문 실행."""
        # TradingMode 확인 — LIVE면 실전, 그 외 모의
        import src.trading_mode as tm
        mode = tm.current_mode()
        use_paper = mode != tm.LIVE

        if use_paper:
            tr_id = "VTTC0802U" if side == OrderSide.BUY else "VTTC0801U"
        else:
            tr_id = "TTTC0802U" if side == OrderSide.BUY else "TTTC0801U"

        headers = {
            **self._auth.headers,
            "tr_id": tr_id,
            "custtype": "P",
        }

        # LIVE면 실전 계좌번호
        if not use_paper and self._cfg.real_account_no:
            acct_prefix = self._cfg.real_account_prefix
            acct_suffix = self._cfg.real_account_suffix
        else:
            acct_prefix = self._cfg.account_prefix
            acct_suffix = self._cfg.account_suffix

        # 시간외 단일가: 같은 order-cash 엔드포인트 + AFHR_FLPR_YN="Y"
        is_after_hours_single = order_type == OrderType.AFTER_HOURS_SINGLE
        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "PDNO": stock_code,
            "ORD_DVSN": "00" if order_type in (OrderType.LIMIT, OrderType.AFTER_HOURS_CLOSE, OrderType.AFTER_HOURS_SINGLE) else order_type.value,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price) if order_type != OrderType.MARKET else "0",
        }
        if is_after_hours_single:
            body["AFHR_FLPR_YN"] = "Y"

        endpoint = "/uapi/domestic-stock/v1/trading/order-cash"

        from src.kis.rate_limiter import get_limiter
        from src.api_log import log_api_call
        import time
        get_limiter().wait()
        start = time.time()
        try:
            resp = self._client.post(
                endpoint,
                headers=headers,
                json=body,
            )
            duration = int((time.time() - start) * 1000)
            log_api_call("POST", endpoint, tr_id, body, resp.status_code, duration, "paper" if use_paper else "real")
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "1")
            if rt_cd == "0":
                output = data.get("output", {})
                order_no = output.get("ODNO", "")
                logger.info(
                    "%s 주문 성공: %s %s주 (주문번호: %s)",
                    side.value, stock_code, quantity, order_no,
                )
                return OrderResult(
                    success=True,
                    order_no=order_no,
                    message=data.get("msg1", ""),
                    stock_code=stock_code,
                    side=side.value,
                    quantity=quantity,
                    price=price,
                )
            else:
                msg = data.get("msg1", "주문 실패")
                logger.error("%s 주문 실패: %s — %s", side.value, stock_code, msg)
                return OrderResult(success=False, message=msg, stock_code=stock_code, side=side.value)

        except httpx.TimeoutException as e:
            logger.error("주문 응답 타임아웃: 주문 상태 불확실. %s", e)
            return OrderResult(success=False, message="Timeout: 주문 체결 여부 불확실", stock_code=stock_code, side=side.value)
        except httpx.HTTPError as e:
            duration = int((time.time() - start) * 1000)
            log_api_call("POST", endpoint, tr_id, body, 0, duration, "paper" if use_paper else "real")
            logger.error("주문 HTTP 오류: %s", e)
            return OrderResult(success=False, message=str(e), stock_code=stock_code, side=side.value)

    # ═══════════════════════════════════════════
    # 미국주식 주문
    # ═══════════════════════════════════════════

    def buy_us(
        self,
        symbol: str,
        quantity: int,
        price: float = 0,
        exchange: str = "NAS",
        order_type: str = "LOO",
    ) -> OrderResult:
        """미국주식 매수.

        Args:
            symbol: 티커 (AAPL, TSLA 등)
            quantity: 수량
            price: 지정가 (LOO/MOO면 0)
            exchange: NAS/NYS/AMS
            order_type: LOO(장개시지정가), MOO(장개시시장가),
                        LOC(장마감지정가), MOC(장마감시장가),
                        LIMIT(지정가), MARKET(시장가)
        """
        return self._place_us_order(symbol, OrderSide.BUY, quantity, price, exchange, order_type)

    def sell_us(
        self,
        symbol: str,
        quantity: int,
        price: float = 0,
        exchange: str = "NAS",
        order_type: str = "LOO",
    ) -> OrderResult:
        """미국주식 매도."""
        return self._place_us_order(symbol, OrderSide.SELL, quantity, price, exchange, order_type)

    def _place_us_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        price: float,
        exchange: str,
        order_type: str,
    ) -> OrderResult:
        """미국주식 주문 실행."""
        import src.trading_mode as tm
        mode = tm.current_mode()
        use_paper = mode != tm.LIVE

        # 거래소 코드
        excd_map = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}
        ovrs_excg_cd = excd_map.get(exchange.upper(), "NASD")

        # 주문유형
        ord_type_map = {
            "LIMIT": "00", "MARKET": "00",
            "LOO": "32", "MOO": "34",
            "LOC": "33", "MOC": "35",
        }
        sll_type = "00"  # 해외주식 매도유형

        if use_paper:
            tr_id = "VTTT1002U" if side == OrderSide.BUY else "VTTT1001U"
        else:
            tr_id = "JTTT1002U" if side == OrderSide.BUY else "JTTT1006U"

        headers = {**self._auth.headers, "tr_id": tr_id, "custtype": "P"}

        if not use_paper and self._cfg.real_account_no:
            acct_prefix = self._cfg.real_account_prefix
            acct_suffix = self._cfg.real_account_suffix
        else:
            acct_prefix = self._cfg.account_prefix
            acct_suffix = self._cfg.account_suffix

        body = {
            "CANO": acct_prefix,
            "ACNT_PRDT_CD": acct_suffix,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "PDNO": symbol.upper(),
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": f"{price:.2f}" if price > 0 else "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_type_map.get(order_type.upper(), "00"),
        }
        if side == OrderSide.SELL:
            body["SLL_TYPE"] = sll_type

        from src.kis.rate_limiter import get_limiter
        from src.api_log import log_api_call
        import time
        get_limiter().wait()
        start = time.time()
        try:
            resp = self._client.post(
                "/uapi/overseas-stock/v1/trading/order",
                headers=headers,
                json=body,
            )
            duration = int((time.time() - start) * 1000)
            log_api_call("POST", "/uapi/overseas-stock/v1/trading/order", tr_id, body, resp.status_code, duration, "paper" if use_paper else "real")
            resp.raise_for_status()
            data = resp.json()

            rt_cd = data.get("rt_cd", "1")
            if rt_cd == "0":
                output = data.get("output", {})
                order_no = output.get("ODNO", "")
                logger.info("US %s 성공: %s %s주 (주문번호: %s)", side.value, symbol, quantity, order_no)
                return OrderResult(
                    success=True, order_no=order_no,
                    message=data.get("msg1", ""),
                    stock_code=symbol, side=side.value,
                    quantity=quantity, price=int(price),
                )
            else:
                msg = data.get("msg1", "주문 실패")
                logger.error("US %s 실패: %s — %s", side.value, symbol, msg)
                return OrderResult(success=False, message=msg, stock_code=symbol, side=side.value)
        except httpx.TimeoutException as e:
            logger.error("US 주문 응답 타임아웃: 주문 상태 불확실. %s", e)
            return OrderResult(success=False, message="Timeout: 주문 체결 여부 불확실", stock_code=symbol, side=side.value)
        except httpx.HTTPError as e:
            duration = int((time.time() - start) * 1000)
            log_api_call("POST", "/uapi/overseas-stock/v1/trading/order", tr_id, body, 0, duration, "paper" if use_paper else "real")
            logger.error("US 주문 오류: %s", e)
            return OrderResult(success=False, message=str(e), stock_code=symbol, side=side.value)

    def close(self):
        self._client.close()
