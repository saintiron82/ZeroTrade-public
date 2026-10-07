"""KIS REST API 시세 조회 — 국내주식/ETF 현재가, 등락률 순위 등."""

import json
import logging
import time
from pathlib import Path

import httpx

from src.config import settings, PROJECT_ROOT

logger = logging.getLogger(__name__)

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
REAL_TOKEN_CACHE = PROJECT_ROOT / ".kis_real_token.json"


class _RealAuth:
    """실전투자 키로 별도 토큰 관리 (시세 조회 전용)."""

    def __init__(self):
        self._cfg = settings.kis
        self._client = httpx.Client(base_url=REAL_BASE_URL, timeout=10)
        self._token: str | None = None
        self._expires_at: float = 0
        self._load_cached()

    def _load_cached(self):
        if REAL_TOKEN_CACHE.exists():
            try:
                data = json.loads(REAL_TOKEN_CACHE.read_text())
                if data.get("expires_at", 0) > time.time() + 3600:
                    self._token = data["access_token"]
                    self._expires_at = data["expires_at"]
            except (json.JSONDecodeError, KeyError):
                pass

    @property
    def access_token(self) -> str:
        if self._token and time.time() < self._expires_at - 3600:
            return self._token
        self._issue()
        return self._token

    def _issue(self):
        resp = self._client.post(
            "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._cfg.real_app_key,
                "appsecret": self._cfg.real_app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + 86400
        REAL_TOKEN_CACHE.write_text(
            json.dumps({"access_token": self._token, "expires_at": self._expires_at})
        )

    @property
    def headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.access_token}",
            "appkey": self._cfg.real_app_key,
            "appsecret": self._cfg.real_app_secret,
            "Content-Type": "application/json; charset=utf-8",
        }

    def close(self):
        self._client.close()


# _RealAuth 싱글톤 — 모든 KISMarket 인스턴스가 같은 토큰 공유
_real_auth_instance: _RealAuth | None = None

def _get_real_auth() -> _RealAuth:
    global _real_auth_instance
    if _real_auth_instance is None:
        _real_auth_instance = _RealAuth()
    return _real_auth_instance


class KISMarket:
    """KIS 국내주식/ETF 시세 + 계좌 조회.

    시세: 항상 실전 서버 사용 (모의투자 서버는 시세 미지원)
    계좌(잔고/체결/미체결): 모의/실전 설정에 따라 분기
    """

    def __init__(self):
        from src.kis.auth import KISAuth
        self._auth = _get_real_auth()   # 시세 전용 (싱글톤 — 토큰 공유)
        self._acct_auth = KISAuth()     # 계좌 전용 (모의/실전 키)
        self._cfg = settings.kis
        self._client = httpx.Client(base_url=REAL_BASE_URL, timeout=10)
        self._acct_client = httpx.Client(base_url=self._cfg.base_url, timeout=10)

    def _get(self, path: str, tr_id: str, params: dict) -> dict | None:
        """시세 조회 (실전 서버) — 캐시 적용."""
        from src.kis.market_cache import get_cache
        cache_key = f"{tr_id}:{json.dumps(params, sort_keys=True)}"
        cached = get_cache().get(cache_key)
        if cached is not None:
            return cached

        from src.api_log import log_api_call
        from src.kis.rate_limiter import get_limiter
        headers = {**self._auth.headers, "tr_id": tr_id}
        
        for attempt in range(3):
            get_limiter().wait()
            start = time.time()
            try:
                resp = self._client.get(path, headers=headers, params=params)
                duration = int((time.time() - start) * 1000)
                log_api_call("GET", path, tr_id, params, resp.status_code, duration, "real")
                resp.raise_for_status()
                data = resp.json()
                if data.get("rt_cd") != "0":
                    logger.error("API 오류: %s", data.get("msg1", ""))
                    return None
                get_cache().set(cache_key, data)
                return data
            except httpx.TimeoutException as e:
                logger.warning("시세 조회 타임아웃 (%d/3): %s", attempt + 1, e)
                if attempt == 2:
                    return None
                time.sleep(1.0)
            except httpx.HTTPError as e:
                duration = int((time.time() - start) * 1000)
                log_api_call("GET", path, tr_id, params, 0, duration, "real")
                logger.error("시세 조회 오류: %s", e)
                return None
        return None

    def _get_account(self, path: str, tr_id: str, params: dict, use_real: bool = False) -> dict | None:
        """계좌 조회. use_real=True면 실전 서버/키 사용."""
        from src.api_log import log_api_call
        from src.kis.rate_limiter import get_limiter
        if use_real:
            headers = {**self._auth.headers, "tr_id": tr_id, "custtype": "P"}
            client = self._client
            server = "real"
        else:
            headers = {**self._acct_auth.headers, "tr_id": tr_id, "custtype": "P"}
            client = self._acct_client
            server = "paper" if self._cfg.is_paper else "real"
            
        for attempt in range(3):
            get_limiter().wait()
            start = time.time()
            try:
                resp = client.get(path, headers=headers, params=params)
                duration = int((time.time() - start) * 1000)
                log_api_call("GET", path, tr_id, params, resp.status_code, duration, server)
                resp.raise_for_status()
                data = resp.json()
                if data.get("rt_cd") != "0":
                    logger.error("계좌 API 오류: %s", data.get("msg1", ""))
                    return None
                return data
            except httpx.TimeoutException as e:
                logger.warning("계좌 조회 타임아웃 (%d/3): %s", attempt + 1, e)
                if attempt == 2:
                    return None
                time.sleep(1.0)
            except httpx.HTTPError as e:
                duration = int((time.time() - start) * 1000)
                log_api_call("GET", path, tr_id, params, 0, duration, server)
                logger.error("계좌 조회 오류: %s", e)
                return None
        return None

    def get_price(self, stock_code: str) -> dict | None:
        """종목 현재가 조회."""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
        )
        if not data:
            return None
        o = data.get("output", {})
        return {
            "stock_code": stock_code,
            "name": o.get("rprs_mrkt_kor_name", ""),
            "price": int(o.get("stck_prpr", 0)),
            "change": int(o.get("prdy_vrss", 0)),
            "change_rate": float(o.get("prdy_ctrt", 0)),
            "volume": int(o.get("acml_vol", 0)),
            "high": int(o.get("stck_hgpr", 0)),
            "low": int(o.get("stck_lwpr", 0)),
            "open": int(o.get("stck_oprc", 0)),
            "market_cap": o.get("hts_avls", ""),
            "per": o.get("per", ""),
            "pbr": o.get("pbr", ""),
        }

    def get_ranking(
        self,
        sort_by: str = "rise",
        market: str = "ALL",
        count: int = 10,
    ) -> list[dict]:
        """등락률 순위 조회.

        Args:
            sort_by: "rise" (상승) 또는 "fall" (하락)
            market: "ALL", "STK" (코스피), "KSQ" (코스닥), "ETF"
            count: 조회 개수
        """
        # fid_rank_sort_cls_code: 0=상승률, 1=하락률
        sort_code = "0" if sort_by == "rise" else "1"

        # 시장 구분
        mrkt_map = {"ALL": "0000", "STK": "0001", "KSQ": "1001", "ETF": "2001"}
        mrkt_code = mrkt_map.get(market, "0000")

        data = self._get(
            "/uapi/domestic-stock/v1/ranking/fluctuation",
            tr_id="FHPST01700000",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20170",
                "fid_input_iscd": mrkt_code,
                "fid_rank_sort_cls_code": sort_code,
                "fid_input_cnt_1": "0",
                "fid_prc_cls_code": "0",
                "fid_input_price_1": "",
                "fid_input_price_2": "",
                "fid_vol_cnt": "",
                "fid_trgt_cls_code": "0",
                "fid_trgt_exls_cls_code": "0",
                "fid_div_cls_code": "0",
                "fid_rsfl_rate1": "",
                "fid_rsfl_rate2": "",
            },
        )
        if not data:
            return []

        results = []
        for item in data.get("output", [])[:count]:
            results.append({
                "rank": item.get("data_rank", ""),
                "stock_code": item.get("stck_shrn_iscd", ""),
                "name": item.get("hts_kor_isnm", ""),
                "price": int(item.get("stck_prpr", 0)),
                "change_rate": float(item.get("prdy_ctrt", 0)),
                "volume": int(item.get("acml_vol", 0)),
            })
        return results

    def get_etf_ranking(self, sort_by: str = "rise", count: int = 10) -> list[dict]:
        """ETF 등락률 순위."""
        return self.get_ranking(sort_by=sort_by, market="ETF", count=count)

    def get_daily_price(self, stock_code: str, period: str = "D", count: int = 30) -> list[dict]:
        """기간별 시세 (일/주/월/년봉).

        Args:
            period: "D"(일), "W"(주), "M"(월), "Y"(년)
            count: 조회 개수
        """
        from datetime import datetime, timedelta
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")

        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id="FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": end,
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        if not data:
            return []
        results = []
        for item in data.get("output2", [])[:count]:
            results.append({
                "date": item.get("stck_bsop_date", ""),
                "open": int(item.get("stck_oprc", 0)),
                "high": int(item.get("stck_hgpr", 0)),
                "low": int(item.get("stck_lwpr", 0)),
                "close": int(item.get("stck_clpr", 0)),
                "volume": int(item.get("acml_vol", 0)),
                "amount": int(item.get("acml_tr_pbmn", 0)),
            })
        return results

    def get_minute_price(self, stock_code: str, time_unit: str = "1") -> list[dict]:
        """당일 분봉 조회.

        Args:
            time_unit: "1"(1분), "5"(5분), "10"(10분), "30"(30분), "60"(60분)
        """
        from datetime import datetime
        now = datetime.now().strftime("%H%M%S")

        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            tr_id="FHKST03010200",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
                "FID_INPUT_HOUR_1": now,
                "FID_PW_DATA_INCU_YN": "N",
                "FID_ETC_CLS_CODE": "",
            },
        )
        if not data:
            return []
        results = []
        for item in data.get("output2", [])[:30]:
            price = int(item.get("stck_prpr", 0))
            results.append({
                "time": item.get("stck_cntg_hour", ""),
                "price": price,
                "open": int(item.get("stck_oprc", 0)) or price,
                "close": price,
                "high": int(item.get("stck_hgpr", 0)) or price,
                "low": int(item.get("stck_lwpr", 0)) or price,
                "volume": int(item.get("cntg_vol", 0)),
                "acc_volume": int(item.get("acml_vol", 0)),
            })
        return results

    def get_orderbook(self, stock_code: str) -> dict | None:
        """호가 조회 (매수/매도 10단계)."""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            tr_id="FHKST01010200",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
        )
        if not data:
            return None
        o = data.get("output1", {})
        asks, bids = [], []
        for i in range(1, 11):
            asks.append({
                "price": int(o.get(f"askp{i}", 0)),
                "volume": int(o.get(f"askp_rsqn{i}", 0)),
            })
            bids.append({
                "price": int(o.get(f"bidp{i}", 0)),
                "volume": int(o.get(f"bidp_rsqn{i}", 0)),
            })
        return {
            "stock_code": stock_code,
            "asks": asks,
            "bids": bids,
            "total_ask_volume": int(o.get("total_askp_rsqn", 0)),
            "total_bid_volume": int(o.get("total_bidp_rsqn", 0)),
        }

    def get_investor_trend(self, stock_code: str) -> list[dict]:
        """투자자별 매매동향 (개인/외국인/기관)."""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            tr_id="FHKST01010900",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
        )
        if not data:
            return []
        results = []
        for item in data.get("output", []):
            results.append({
                "date": item.get("stck_bsop_date", ""),
                "individual_net": int(item.get("prsn_ntby_qty", 0)),
                "foreign_net": int(item.get("frgn_ntby_qty", 0)),
                "institution_net": int(item.get("orgn_ntby_qty", 0)),
            })
        return results

    def get_volume_ranking(self, count: int = 10) -> list[dict]:
        """거래량 순위."""
        data = self._get(
            "/uapi/domestic-stock/v1/ranking/volume",
            tr_id="FHPST01710000",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20171",
                "fid_input_iscd": "0000",
                "fid_div_cls_code": "0",
                "fid_blng_cls_code": "0",
                "fid_trgt_cls_code": "111111111",
                "fid_trgt_exls_cls_code": "000000",
                "fid_input_price_1": "",
                "fid_input_price_2": "",
                "fid_vol_cnt": "",
                "fid_input_date_1": "",
            },
        )
        if not data:
            return []
        results = []
        for item in data.get("output", [])[:count]:
            results.append({
                "rank": item.get("data_rank", ""),
                "stock_code": item.get("mksc_shrn_iscd", ""),
                "name": item.get("hts_kor_isnm", ""),
                "price": int(item.get("stck_prpr", 0)),
                "change_rate": float(item.get("prdy_ctrt", 0)),
                "volume": int(item.get("acml_vol", 0)),
                "amount": int(item.get("acml_tr_pbmn", 0)),
            })
        return results

    def get_balance(self, use_real: bool = True) -> dict:
        """주식 잔고 조회. use_real=True면 실전 계좌 잔고."""
        if use_real and self._cfg.real_account_no:
            tr_id = "TTTC8434R"
            prefix = self._cfg.real_account_prefix
            suffix = self._cfg.real_account_suffix
        else:
            tr_id = "VTTC8434R" if self._cfg.is_paper else "TTTC8434R"
            prefix = self._cfg.account_prefix
            suffix = self._cfg.account_suffix

        data = self._get_account(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params={
                "CANO": prefix,
                "ACNT_PRDT_CD": suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            use_real=use_real,
        )
        if not data:
            return {"holdings": [], "summary": {}}

        holdings = []
        for item in data.get("output1", []):
            holdings.append({
                "stock_code": item.get("pdno", ""),
                "name": item.get("prdt_name", ""),
                "quantity": int(item.get("hldg_qty", 0)),
                "avg_price": int(float(item.get("pchs_avg_pric", 0))),
                "current_price": int(item.get("prpr", 0)),
                "profit_rate": float(item.get("evlu_pfls_rt", 0)),
                "profit_amount": int(item.get("evlu_pfls_amt", 0)),
                "eval_amount": int(item.get("evlu_amt", 0)),
            })

        output2 = data.get("output2", [{}])
        summary_data = output2[0] if output2 else {}
        summary = {
            "total_eval": int(summary_data.get("tot_evlu_amt", 0)),
            "total_profit": int(summary_data.get("evlu_pfls_smtl_amt", 0)),
            "cash": int(summary_data.get("dnca_tot_amt", 0)),
        }
        return {"holdings": holdings, "summary": summary}

    def get_buyable_qty(self, stock_code: str, price: int, use_real: bool = True) -> dict:
        """매수가능조회 — 특정 종목/가격에서 실제 주문 가능 수량 확인.

        Returns:
            {"max_qty": int, "max_amount": int, "cash": int}
        """
        if use_real and self._cfg.real_account_no:
            tr_id = "TTTC8908R"
            prefix = self._cfg.real_account_prefix
            suffix = self._cfg.real_account_suffix
        else:
            tr_id = "VTTC8908R" if self._cfg.is_paper else "TTTC8908R"
            prefix = self._cfg.account_prefix
            suffix = self._cfg.account_suffix

        data = self._get_account(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=tr_id,
            params={
                "CANO": prefix,
                "ACNT_PRDT_CD": suffix,
                "PDNO": stock_code,
                "ORD_UNPR": str(price),
                "ORD_DVSN": "00",
                "CMA_EVLU_AMT_ICLD_YN": "Y",
                "OVRS_ICLD_YN": "N",
            },
            use_real=use_real,
        )
        if not data:
            return {"max_qty": 0, "max_amount": 0, "cash": 0}

        output = data.get("output", {})
        return {
            "max_qty": int(output.get("nrcvb_buy_qty", 0)),
            "max_amount": int(output.get("nrcvb_buy_amt", 0)),
            "cash": int(output.get("ord_psbl_cash", 0)),
        }

    def get_executions(self, start_date: str = "", end_date: str = "", use_real: bool = True) -> list[dict]:
        """체결 내역 조회. start_date/end_date는 YYYYMMDD 형식. 비면 당일."""
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        start_date = start_date or today
        end_date = end_date or today

        if use_real and self._cfg.real_account_no:
            tr_id = "TTTC8001R"
            prefix = self._cfg.real_account_prefix
            suffix = self._cfg.real_account_suffix
        else:
            tr_id = "VTTC8001R" if self._cfg.is_paper else "TTTC8001R"
            prefix = self._cfg.account_prefix
            suffix = self._cfg.account_suffix

        data = self._get_account(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id=tr_id,
            params={
                "CANO": prefix,
                "ACNT_PRDT_CD": suffix,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": "00",  # 전체
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            use_real=use_real,
        )
        if not data:
            return []
        results = []
        for item in data.get("output1", []):
            results.append({
                "order_date": item.get("ord_dt", ""),
                "order_time": item.get("ord_tmd", ""),
                "stock_code": item.get("pdno", ""),
                "stock_name": item.get("prdt_name", ""),
                "side": "buy" if item.get("sll_buy_dvsn_cd") == "02" else "sell",
                "order_qty": int(item.get("ord_qty", 0)),
                "exec_qty": int(item.get("tot_ccld_qty", 0)),
                "exec_price": int(float(item.get("avg_prvs", 0))),
                "exec_amount": int(item.get("tot_ccld_amt", 0)),
                "order_no": item.get("odno", ""),
                "status": "체결" if int(item.get("tot_ccld_qty", 0)) > 0 else "미체결",
            })
        return results

    def get_pending_orders(self, use_real: bool = True) -> list[dict]:
        """미체결 주문 조회."""
        if use_real and self._cfg.real_account_no:
            tr_id = "TTTC8004R"  # 실전 미체결 조회
            prefix = self._cfg.real_account_prefix
            suffix = self._cfg.real_account_suffix
        else:
            tr_id = "VTTC8001R" if self._cfg.is_paper else "TTTC8004R"
            prefix = self._cfg.account_prefix
            suffix = self._cfg.account_suffix

        data = self._get_account(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=tr_id,
            params={
                "CANO": prefix,
                "ACNT_PRDT_CD": suffix,
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "INQR_DVSN_1": "0",
                "INQR_DVSN_2": "0",
            },
            use_real=use_real,
        )
        if not data:
            return []
        results = []
        for item in data.get("output", []):
            qty = int(item.get("ord_qty", 0))
            exec_qty = int(item.get("tot_ccld_qty", 0))
            remaining = qty - exec_qty
            if remaining <= 0:
                continue
            results.append({
                "order_no": item.get("odno", ""),
                "stock_code": item.get("pdno", ""),
                "stock_name": item.get("prdt_name", ""),
                "side": "buy" if item.get("sll_buy_dvsn_cd") == "02" else "sell",
                "order_qty": qty,
                "exec_qty": exec_qty,
                "remaining_qty": remaining,
                "order_price": int(item.get("ord_unpr", 0)),
                "order_time": item.get("ord_tmd", ""),
            })
        return results

    # ═══════════════════════════════════════════
    # 미국주식
    # ═══════════════════════════════════════════

    def get_us_price(self, symbol: str, exchange: str = "NAS") -> dict | None:
        """미국주식 현재가 조회.

        Args:
            symbol: 티커 (예: AAPL, TSLA, MSFT)
            exchange: NAS(나스닥), NYS(뉴욕), AMS(아멕스)
        """
        excd_map = {"NAS": "NAS", "NYS": "NYS", "AMS": "AMS",
                    "NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        excd = excd_map.get(exchange.upper(), "NAS")

        data = self._get(
            "/uapi/overseas-price/v1/quotations/price",
            tr_id="HHDFS00000300",
            params={"AUTH": "", "EXCD": excd, "SYMB": symbol.upper()},
        )
        if not data:
            return None
        o = data.get("output", {})
        return {
            "symbol": symbol.upper(),
            "exchange": excd,
            "name": o.get("rsym", symbol),
            "price": float(o.get("last", 0)),
            "change": float(o.get("diff", 0)),
            "change_rate": float(o.get("rate", 0)),
            "open": float(o.get("open", 0)),
            "high": float(o.get("high", 0)),
            "low": float(o.get("low", 0)),
            "volume": int(o.get("tvol", 0)),
            "market_cap": o.get("tomv", ""),
            "per": o.get("perx", ""),
            "eps": o.get("epsx", ""),
        }

    def get_us_daily_price(self, symbol: str, exchange: str = "NAS", count: int = 20) -> list[dict]:
        """미국주식 일봉 조회."""
        excd_map = {"NAS": "NAS", "NYS": "NYS", "AMS": "AMS"}
        excd = excd_map.get(exchange.upper(), "NAS")

        data = self._get(
            "/uapi/overseas-price/v1/quotations/dailyprice",
            tr_id="HHDFS76240000",
            params={
                "AUTH": "",
                "EXCD": excd,
                "SYMB": symbol.upper(),
                "GUBN": "0",  # 0:일, 1:주, 2:월
                "BYMD": "",
                "MODP": "1",
            },
        )
        if not data:
            return []
        results = []
        for item in data.get("output2", [])[:count]:
            results.append({
                "date": item.get("xymd", ""),
                "open": float(item.get("open", 0)),
                "high": float(item.get("high", 0)),
                "low": float(item.get("low", 0)),
                "close": float(item.get("clos", 0)),
                "volume": int(item.get("tvol", 0)),
            })
        return results

    def get_us_buyable_qty(self, symbol: str, exchange: str = "NAS", price: float = 0) -> dict | None:
        """미국주식 주문가능수량/금액 조회.

        봇의 원화 예산 계산과 별개로, KIS 계좌가 실제로 해당 해외주식을
        몇 주까지 살 수 있는지 확인한다. 0주면 주문 엔드포인트를 호출하지
        않고 사전에 차단해야 원인 없는 500 오류를 줄일 수 있다.
        """
        import src.trading_mode as tm

        excd_map = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX",
                    "NASDAQ": "NASD", "NYSE": "NYSE", "AMEX": "AMEX"}
        ovrs_excg_cd = excd_map.get(exchange.upper(), "NASD")

        use_real = tm.current_mode() == tm.LIVE and bool(self._cfg.real_account_no)
        if use_real:
            prefix = self._cfg.real_account_prefix
            suffix = self._cfg.real_account_suffix
            tr_ids = ("TTTS3007R", "JTTT3007R")
        else:
            prefix = self._cfg.account_prefix
            suffix = self._cfg.account_suffix
            tr_ids = ("VTTT3007R",)

        params = {
            "CANO": prefix,
            "ACNT_PRDT_CD": suffix,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "OVRS_ORD_UNPR": f"{float(price):.2f}" if price else "0",
            "ITEM_CD": symbol.upper(),
        }

        def to_int(value) -> int:
            try:
                return int(float(str(value or "0").replace(",", "")))
            except (TypeError, ValueError):
                return 0

        def to_float(value) -> float:
            try:
                return float(str(value or "0").replace(",", ""))
            except (TypeError, ValueError):
                return 0.0

        for tr_id in tr_ids:
            data = self._get_account(
                "/uapi/overseas-stock/v1/trading/inquire-psamount",
                tr_id=tr_id,
                params=params,
                use_real=use_real,
            )
            if not data:
                continue

            output = data.get("output", {}) or {}
            return {
                "symbol": symbol.upper(),
                "exchange": ovrs_excg_cd,
                "order_price": to_float(params["OVRS_ORD_UNPR"]),
                "ord_psbl_qty": to_int(output.get("ord_psbl_qty")),
                "max_ord_psbl_qty": to_int(output.get("max_ord_psbl_qty")),
                "ovrs_max_ord_psbl_qty": to_int(output.get("ovrs_max_ord_psbl_qty")),
                "ord_psbl_frcr_amt": to_float(output.get("ord_psbl_frcr_amt")),
                "ovrs_ord_psbl_amt": to_float(output.get("ovrs_ord_psbl_amt")),
                "msg": data.get("msg1", ""),
                "tr_id": tr_id,
            }
        return None

    def close(self):
        self._client.close()
        self._acct_client.close()
        self._acct_auth.close()
