"""KIS WebSocket — 국내주식/ETF 실시간 시세 수신."""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import websockets

from src.config import settings
from src.kis.auth import KISAuth

logger = logging.getLogger(__name__)

# KIS 실시간 체결가 데이터 필드 (H0STCNT0 응답, | 구분)
TICK_FIELDS = [
    "mksc_shrn_iscd",   # 0  유가증권단축종목코드
    "stck_cntg_hour",   # 1  주식체결시간 (HHMMSS)
    "stck_prpr",        # 2  주식현재가
    "prdy_vrss_sign",   # 3  전일대비부호
    "prdy_vrss",        # 4  전일대비
    "prdy_ctrt",        # 5  전일대비율
    "wghn_avrg_stck_prc",  # 6  가중평균주식가격
    "stck_oprc",        # 7  시가
    "stck_hgpr",        # 8  고가
    "stck_lwpr",        # 9  저가
    "askp1",            # 10 매도호가1
    "bidp1",            # 11 매수호가1
    "cntg_vol",         # 12 체결거래량
    "acml_vol",         # 13 누적거래량
    "acml_tr_pbmn",     # 14 누적거래대금
    "seln_cntg_csnu",   # 15 매도체결건수
    "shnu_cntg_csnu",   # 16 매수체결건수
    "ntby_cntg_csnu",   # 17 순매수체결건수
    "cttr",             # 18 체결강도
    "seln_cntg_smtn",   # 19 총매도수량
    "shnu_cntg_smtn",   # 20 총매수수량
    "ccld_dvsn",        # 21 체결구분 (1:매수, 5:매도)
    "shnu_rate",        # 22 매수비율
    "prdy_vol_vrss_acml_vol_rate",  # 23 전일거래량대비등락율
    "oprc_hour",        # 24 시가시간
    "oprc_vrss_prpr_sign",  # 25 시가대비부호
    "oprc_vrss_prpr",   # 26 시가대비
    "hgpr_hour",        # 27 최고가시간
    "hgpr_vrss_prpr_sign",  # 28 고가대비부호
    "hgpr_vrss_prpr",   # 29 고가대비
    "lwpr_hour",        # 30 최저가시간
    "lwpr_vrss_prpr_sign",  # 31 저가대비부호
    "lwpr_vrss_prpr",   # 32 저가대비
    "bsop_date",        # 33 영업일자
    "new_mkop_cls_code",  # 34 신장운영구분코드
    "trht_yn",          # 35 거래정지여부
    "askp_rsqn1",       # 36 매도호가잔량1
    "bidp_rsqn1",       # 37 매수호가잔량1
    "total_askp_rsqn",  # 38 총매도호가잔량
    "total_bidp_rsqn",  # 39 총매수호가잔량
    "vol_tnrt",         # 40 거래량회전율
    "prdy_smns_hour_acml_vol",  # 41 전일동시간누적거래량
    "prdy_smns_hour_acml_vol_rate",  # 42 전일동시간누적거래량비율
    "hour_cls_code",    # 43 시간구분코드
    "mrkt_trtm_cls_code",  # 44 임의종료구분코드
    "vi_stnd_prc",      # 45 정적VI발동기준가
]


@dataclass
class Tick:
    """실시간 체결 데이터."""
    stock_code: str
    time: str           # HHMMSS
    price: int
    change_sign: str    # 1:상한, 2:상승, 3:보합, 4:하한, 5:하락
    change: int
    change_rate: float
    open_price: int
    high_price: int
    low_price: int
    volume: int         # 체결거래량
    acc_volume: int     # 누적거래량
    acc_amount: int     # 누적거래대금
    strength: float     # 체결강도

    @classmethod
    def from_raw(cls, fields: list[str]) -> "Tick":
        return cls(
            stock_code=fields[0],
            time=fields[1],
            price=int(fields[2]),
            change_sign=fields[3],
            change=int(fields[4]),
            change_rate=float(fields[5]),
            open_price=int(fields[7]),
            high_price=int(fields[8]),
            low_price=int(fields[9]),
            volume=int(fields[12]),
            acc_volume=int(fields[13]),
            acc_amount=int(fields[14]),
            strength=float(fields[18]) if fields[18] else 0.0,
        )


# 구독 요청 메시지 생성
def _subscribe_msg(approval_key: str, tr_id: str, stock_code: str, subscribe: bool = True) -> str:
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {
            "input": {
                "tr_id": tr_id,
                "tr_key": stock_code,
            }
        }
    })


class KISWebSocket:
    """KIS 실시간 시세 WebSocket 클라이언트.

    사용법:
        ws = KISWebSocket()
        ws.on_tick = my_callback  # Tick 수신 콜백 설정
        await ws.connect(["005930", "069500"])  # 삼성전자, KODEX 200
    """

    TR_TICK = "H0STCNT0"  # 실시간 체결가

    def __init__(self):
        self._auth = KISAuth()
        self._ws = None
        self._stock_codes: list[str] = []
        self._running = False
        self.on_tick: Callable[[Tick], None] | None = None

    async def connect(self, stock_codes: list[str] | None = None):
        """WebSocket 연결 및 종목 구독 시작. 빈 리스트로도 연결 가능 (이후 add_stock으로 추가)."""
        self._stock_codes = stock_codes or []
        self._running = True
        ws_url = settings.kis.ws_url
        approval_key = self._auth.approval_key

        logger.info("WebSocket 연결 시작: %s (%d종목)", ws_url, len(self._stock_codes))

        while self._running:
            try:
                async with websockets.connect(ws_url, ping_interval=30) as ws:
                    self._ws = ws
                    # 종목 구독
                    for code in stock_codes:
                        msg = _subscribe_msg(approval_key, self.TR_TICK, code)
                        await ws.send(msg)
                        logger.info("구독: %s", code)

                    # 메시지 수신 루프
                    async for raw in ws:
                        self._handle_message(raw)

            except websockets.ConnectionClosed:
                if self._running:
                    logger.warning("WebSocket 연결 끊김, 3초 후 재연결...")
                    await asyncio.sleep(3)
            except Exception as e:
                if self._running:
                    logger.error("WebSocket 오류: %s, 5초 후 재연결...", e)
                    await asyncio.sleep(5)

    def _handle_message(self, raw: str):
        """수신 메시지 파싱."""
        # KIS WebSocket 응답: 첫 문자가 '0' 또는 '1'이면 실시간 데이터
        # JSON 형태면 구독 확인 응답
        if raw.startswith("{"):
            data = json.loads(raw)
            header = data.get("header", {})
            if header.get("tr_id") == "PINGPONG":
                # PINGPONG 응답
                return
            logger.debug("구독 응답: %s", header.get("tr_id"))
            return

        # 실시간 데이터: 구분자 | 로 분리
        parts = raw.split("|")
        if len(parts) < 4:
            return

        tr_id = parts[1]
        count = int(parts[2])
        data_str = parts[3]

        if tr_id == self.TR_TICK:
            fields = data_str.split("^")
            try:
                tick = Tick.from_raw(fields)
                if self.on_tick:
                    self.on_tick(tick)
            except (IndexError, ValueError) as e:
                logger.warning("체결가 파싱 실패: %s", e)

    async def disconnect(self):
        """WebSocket 연결 종료."""
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("WebSocket 연결 종료")

    async def add_stock(self, stock_code: str):
        """실시간 종목 추가 구독."""
        if self._ws and stock_code not in self._stock_codes:
            msg = _subscribe_msg(self._auth.approval_key, self.TR_TICK, stock_code)
            await self._ws.send(msg)
            self._stock_codes.append(stock_code)

    async def remove_stock(self, stock_code: str):
        """종목 구독 해제."""
        if self._ws and stock_code in self._stock_codes:
            msg = _subscribe_msg(self._auth.approval_key, self.TR_TICK, stock_code, subscribe=False)
            await self._ws.send(msg)
            self._stock_codes.remove(stock_code)
