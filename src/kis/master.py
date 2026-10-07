"""KIS 종목 마스터 — 다운로드, 파싱, 분류, 검색."""

import io
import json
import logging
import ssl
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DATA_DIR = PROJECT_ROOT / "data"
MASTER_FILE = DATA_DIR / "stock_master.json"
US_MASTER_FILE = DATA_DIR / "us_stock_master.json"

# 마스터 파일 다운로드 URL
KOSPI_URL = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
KOSDAQ_URL = "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"

# 미국 시장 마스터
US_MARKET_URLS = {
    "NAS": "https://new.real.download.dws.co.kr/common/master/nasmst.cod.zip",
    "NYS": "https://new.real.download.dws.co.kr/common/master/nysmst.cod.zip",
    "AMS": "https://new.real.download.dws.co.kr/common/master/amsmst.cod.zip",
}

# 파생ETF 분류 키워드 — 계좌에 파생상품ETF 권한이 없으면 KIS가 주문을 거부한다.
# "선물"(원유선물 130680, 골드선물 132030 등)은 실제 거래 가능했으므로 차단하지 않는다.
DERIVATIVE_KEYWORDS = ("인버스", "레버리지")


def is_derivative_etf(name: str) -> bool:
    """파생ETF(인버스/레버리지) 여부 — 종목명 기반."""
    return any(kw in (name or "") for kw in DERIVATIVE_KEYWORDS)


# 코드→이름 경량 캐시 (마스터 파일 1회 로드, 다운로드 시도 없음)
_code_name_cache: dict | None = None


def stock_name_by_code(code: str) -> str:
    """코드→종목명 경량 조회. 마스터 파일이 없으면 ""(다운로드하지 않음)."""
    global _code_name_cache
    # 빈 캐시 고착 방지: 첫 로드가 실패했어도 파일이 생기면 재시도
    if _code_name_cache is None or (not _code_name_cache and MASTER_FILE.exists()):
        try:
            data = json.loads(MASTER_FILE.read_text(encoding="utf-8"))
            _code_name_cache = {s["code"]: s["name"] for s in data.get("all", [])}
        except Exception:
            _code_name_cache = {}
    return _code_name_cache.get(str(code), "")


def _download_and_extract(url: str) -> bytes:
    """ZIP 파일 다운받아 압축 해제."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=ctx) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        name = zf.namelist()[0]
        return zf.read(name)


def _parse_mst(raw: bytes, market: str) -> list[dict]:
    """종목 마스터 .mst 파일 파싱."""
    stocks = []
    lines = raw.decode("cp949", errors="replace").splitlines()
    for line in lines:
        if len(line) < 30:
            continue
        # 단축코드(9) + 표준코드(12) + 한글명(가변) + 나머지(228)
        short_code = line[:9].strip()
        std_code = line[9:21].strip()
        # 한글명: 21번째부터 나머지228바이트 전까지
        rest_len = 228
        name_part = line[21:len(line) - rest_len] if len(line) > 21 + rest_len else line[21:]
        name = name_part.strip()

        # 6자리 종목코드 추출
        code = short_code[-6:] if len(short_code) >= 6 else short_code

        # ETF 판별: 코드 패턴 + 이름
        is_etf = _is_etf(code, name)

        stocks.append({
            "code": code,
            "name": name,
            "market": market,
            "type": "ETF" if is_etf else "STOCK",
            "std_code": std_code,
        })
    return stocks


def _is_etf(code: str, name: str) -> bool:
    """ETF 여부 판별."""
    etf_keywords = [
        "KODEX", "TIGER", "ARIRANG", "KBSTAR", "SOL", "HANARO",
        "KOSEF", "SMART", "파워", "마이티", "히어로", "BNK",
        "ACE", "PLUS", "TIMEFOLIO", "WOORI",
    ]
    return any(kw in name for kw in etf_keywords)


def _parse_us_cod(raw: bytes, market: str) -> list[dict]:
    """미국주식 .cod 파일 파싱 (탭 구분).

    컬럼 구조: 국가(0), ??(1), 거래소(2), 거래소한글(3), 심볼(4),
              ??(5), 한글명(6), 영문명(7), ...
    """
    stocks = []
    try:
        text = raw.decode("cp949", errors="replace")
        for line in text.splitlines():
            cols = line.split("\t")
            if len(cols) < 8:
                continue
            symbol = cols[4].strip()
            name_kr = cols[6].strip()
            name_en = cols[7].strip()
            if not symbol:
                continue
            stocks.append({
                "code": symbol,
                "name": name_kr or name_en,
                "name_en": name_en,
                "market": market,
                "type": "US_STOCK",
            })
    except Exception as e:
        logger.warning("미국주식 마스터 파싱 오류 (%s): %s", market, e)
    return stocks


def download_us_master() -> dict:
    """미국주식 종목 마스터 다운로드."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    all_stocks = []
    market_counts = {}
    for market, url in US_MARKET_URLS.items():
        logger.info("미국주식 %s 마스터 다운로드 중...", market)
        try:
            raw = _download_and_extract(url)
            stocks = _parse_us_cod(raw, market)
            all_stocks.extend(stocks)
            market_counts[market] = len(stocks)
            logger.info("  %s: %d개", market, len(stocks))
        except Exception as e:
            logger.warning("  %s 다운로드 실패: %s", market, e)
            market_counts[market] = 0

    data = {
        "updated_at": __import__("datetime").datetime.now().isoformat(),
        "total": len(all_stocks),
        "by_market": market_counts,
        "all": all_stocks,
    }
    US_MASTER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("미국주식 마스터 저장: 총 %d개", len(all_stocks))
    return data


def download_master() -> dict:
    """종목 마스터 다운로드 + 파싱 + 저장.

    Returns:
        분류된 종목 데이터
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("KOSPI 종목 마스터 다운로드 중...")
    kospi_raw = _download_and_extract(KOSPI_URL)
    kospi = _parse_mst(kospi_raw, "KOSPI")

    logger.info("KOSDAQ 종목 마스터 다운로드 중...")
    kosdaq_raw = _download_and_extract(KOSDAQ_URL)
    kosdaq = _parse_mst(kosdaq_raw, "KOSDAQ")

    all_stocks = kospi + kosdaq

    # 분류
    etfs = [s for s in all_stocks if s["type"] == "ETF"]
    stocks = [s for s in all_stocks if s["type"] == "STOCK"]
    kospi_stocks = [s for s in stocks if s["market"] == "KOSPI"]
    kosdaq_stocks = [s for s in stocks if s["market"] == "KOSDAQ"]

    data = {
        "updated_at": __import__("datetime").datetime.now().isoformat(),
        "total": len(all_stocks),
        "etf_count": len(etfs),
        "kospi_count": len(kospi_stocks),
        "kosdaq_count": len(kosdaq_stocks),
        "all": all_stocks,
        "etf": etfs,
        "kospi": kospi_stocks,
        "kosdaq": kosdaq_stocks,
    }

    MASTER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "종목 마스터 저장: 총 %d개 (ETF %d, KOSPI %d, KOSDAQ %d)",
        len(all_stocks), len(etfs), len(kospi_stocks), len(kosdaq_stocks),
    )
    return data


class StockMaster:
    """종목 마스터 조회 — 국내 + 미국 검색, 분류, 코드 변환."""

    def __init__(self):
        if not MASTER_FILE.exists():
            download_master()
        self._data = json.loads(MASTER_FILE.read_text(encoding="utf-8"))
        # 미국주식
        self._us_data = None
        if US_MASTER_FILE.exists():
            self._us_data = json.loads(US_MASTER_FILE.read_text(encoding="utf-8"))
        # 코드 → 종목 인덱스
        self._by_code = {s["code"]: s for s in self._data["all"]}
        # 이름 → 종목 인덱스
        self._by_name = {s["name"]: s for s in self._data["all"]}

    def search(self, keyword: str, limit: int = 10) -> list[dict]:
        """종목명/코드로 검색."""
        kw = keyword.lower()
        results = []
        for s in self._data["all"]:
            if kw in s["name"].lower() or kw in s["code"]:
                results.append(s)
                if len(results) >= limit:
                    break
        return results

    def get_by_code(self, code: str) -> dict | None:
        return self._by_code.get(code)

    def get_by_name(self, name: str) -> dict | None:
        return self._by_name.get(name)

    def list_etf(self) -> list[dict]:
        return self._data.get("etf", [])

    def list_kospi(self) -> list[dict]:
        return self._data.get("kospi", [])

    def list_kosdaq(self) -> list[dict]:
        return self._data.get("kosdaq", [])

    def search_us(self, keyword: str, limit: int = 10) -> list[dict]:
        """미국주식 종목명/심볼로 검색. 정확 매칭 우선."""
        if not self._us_data:
            return []
        kw = keyword.upper()
        exact = []
        partial = []
        for s in self._us_data["all"]:
            code_up = s["code"].upper()
            name_up = s["name"].upper()
            name_en = s.get("name_en", "").upper()
            if code_up == kw:
                exact.append(s)
            elif kw in code_up or kw in name_up or kw in name_en:
                partial.append(s)
        return (exact + partial)[:limit]

    def list_us(self, market: str = "") -> list[dict]:
        """미국주식 목록. market: NAS, NYS, AMS 또는 빈 문자열(전체)."""
        if not self._us_data:
            return []
        stocks = self._us_data["all"]
        if market:
            stocks = [s for s in stocks if s["market"] == market]
        return stocks

    @property
    def stats(self) -> dict:
        us_total = self._us_data.get("total", 0) if self._us_data else 0
        return {
            "updated_at": self._data.get("updated_at", ""),
            "total": self._data.get("total", 0),
            "etf_count": self._data.get("etf_count", 0),
            "kospi_count": self._data.get("kospi_count", 0),
            "kosdaq_count": self._data.get("kosdaq_count", 0),
            "us_total": us_total,
        }
