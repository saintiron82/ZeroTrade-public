"""GET /api/holdings/overview — 보유 종합 상황판.

핵심 가치: KIS 실잔고와 봇 장부(BotLedger) 귀속을 나란히 비교하고
불일치(mismatch)를 즉시 드러낸다. KIS 잔고 API 장애 시에도 봇 장부
데이터는 그대로 반환한다 (UI 배너용 ok=false + balance_error).

플래핑 대응 (2026-06-12 실사고): KIS가 500을 삼키고 빈 holdings를
반환하는 경우가 있다 → 장부에 보유가 있는데 KIS가 비면 의심 판정,
1회 재시도, 그래도 실패면 마지막 정상값 캐시를 stale=true로 서빙.

실주문/실조회 절대 금지: KISMarket은 mock, DB는 tmp_path sqlite.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_balance_cache(monkeypatch):
    """최종 정상값 캐시는 모듈 변수 — 테스트 간 격리 필수."""
    from src.web import routes
    monkeypatch.setattr(routes, "_LAST_GOOD_BALANCE", None, raising=False)


@pytest.fixture
def db(monkeypatch, tmp_path):
    db_path = tmp_path / "ff.db"
    monkeypatch.setenv("FF_DB_PATH", str(db_path))
    for mod in ("src.db",):
        if mod in sys.modules:
            del sys.modules[mod]
    import src.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path, raising=False)
    db = db_mod.DB(db_path)
    monkeypatch.setattr(db_mod, "_db_instance", db, raising=False)
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    yield db
    db._conn.close()


def _client():
    from src.web.app import app
    return TestClient(app)


def _log(db, *, source, side, code, qty, price, minutes_ago=0, reason="",
         pipe="strat-001", success=1):
    ts = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    db._conn.execute(
        """INSERT INTO trades (timestamp, pipe_id, stock_code, stock_name,
           side, quantity, price, amount, order_no, success, reason, source,
           realized_pnl, decision_id)
           VALUES (?, ?, ?, '', ?, ?, ?, ?, '', ?, ?, ?, 0, 0)""",
        (ts, pipe, code, side, qty, price, qty * price, success, reason, source),
    )
    db._conn.commit()


def _kis_holding(code, qty, name="", avg=10_000, cur=10_000,
                 profit=0, rate=0.0):
    """KISMarket.get_balance()['holdings'] 항목과 동일한 형태."""
    return {"stock_code": code, "name": name, "quantity": qty,
            "avg_price": avg, "current_price": cur,
            "profit_rate": rate, "profit_amount": profit,
            "eval_amount": qty * cur}


def _market_patch(balance=None, exc=None, seq=None):
    """src.kis.market.KISMarket mock — 라우트가 lazy import하므로 클래스 patch.

    seq: get_balance 연속 호출별 응답 목록 (플래핑 시뮬레이션, 예외 혼합 가능).
    """
    market_cls = MagicMock()
    gb = market_cls.return_value.get_balance
    if seq is not None:
        gb.side_effect = seq
    elif exc is not None:
        gb.side_effect = exc
    else:
        gb.return_value = balance
    return patch("src.kis.market.KISMarket", market_cls), market_cls


def _get(balance=None, exc=None, seq=None):
    market_p, market_cls = _market_patch(balance=balance, exc=exc, seq=seq)
    with market_p:
        resp = _client().get("/api/holdings/overview")
    assert resp.status_code == 200
    return resp.json(), market_cls


# ── 정상 동작 ──

def test_matched_holdings_have_zero_mismatch_and_correct_totals(db):
    """KIS 2종목 + 장부 귀속 일치 → mismatch 0, owners 정확, totals 합산."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    _log(db, source="mirinae-3", side="buy", code="069500", qty=2, price=40_000)
    balance = {
        "holdings": [
            _kis_holding("140670", 6, name="알에스오토메이션",
                         avg=13_680, cur=13_720, profit=240, rate=0.29),
            _kis_holding("069500", 2, name="KODEX 200",
                         avg=40_000, cur=41_000, profit=2_000, rate=2.5),
        ],
        "summary": {"cash": 137_242, "total_eval": 164_360, "total_profit": 2_240},
    }
    d, _ = _get(balance=balance)

    assert d["ok"] is True
    assert d["balance_error"] is None
    assert d["cash"] == 137_242
    assert d["as_of"]

    rows = {h["code"]: h for h in d["holdings"]}
    assert set(rows) == {"140670", "069500"}

    r1 = rows["140670"]
    assert r1["name"] == "알에스오토메이션"
    assert r1["kis_qty"] == 6
    assert r1["ledger_qty"] == 6
    assert r1["mismatch"] == 0
    assert r1["avg_price"] == 13_680
    assert r1["current_price"] == 13_720
    assert r1["profit_amount"] == 240
    assert r1["profit_rate"] == 0.29
    assert [(o["source"], o["qty"]) for o in r1["owners"]] == [("winner-1", 6)]

    r2 = rows["069500"]
    assert r2["mismatch"] == 0
    assert [(o["source"], o["qty"]) for o in r2["owners"]] == [("mirinae-3", 2)]

    assert d["totals"]["eval_amount"] == 6 * 13_720 + 2 * 41_000
    assert d["totals"]["profit_amount"] == 240 + 2_000


def test_unattributed_kis_holding_yields_positive_mismatch(db):
    """KIS 6주 vs 장부 0주 → mismatch 6 (장부 미귀속)."""
    balance = {"holdings": [_kis_holding("140670", 6, name="알에스오토메이션")],
               "summary": {"cash": 0}}
    d, _ = _get(balance=balance)

    assert d["ok"] is True
    (row,) = d["holdings"]
    assert row["kis_qty"] == 6
    assert row["ledger_qty"] == 0
    assert row["owners"] == []
    assert row["mismatch"] == 6


def test_ledger_only_holding_included_with_negative_mismatch(db):
    """KIS 0주 vs 장부 6주 → 행 포함, mismatch -6 (장부 과잉).

    주의: KIS 응답이 통째로 비면 의심-빈-응답 판정에 걸리므로,
    다른 정상 보유가 있는(신뢰 가능한) 응답에서 특정 종목만 빠진 경우.
    """
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    _log(db, source="mirinae-3", side="buy", code="069500", qty=1, price=40_000)
    balance = {"holdings": [_kis_holding("069500", 1, name="KODEX 200")],
               "summary": {"cash": 50_000}}
    with patch("src.kis.master.stock_name_by_code", return_value="알에스오토메이션"):
        d, _ = _get(balance=balance)

    assert d["ok"] is True
    rows = {h["code"]: h for h in d["holdings"]}
    row = rows["140670"]
    assert row["name"] == "알에스오토메이션"  # KIS에 없으니 마스터 폴백
    assert row["kis_qty"] == 0
    assert row["ledger_qty"] == 6
    assert row["mismatch"] == -6
    assert [(o["source"], o["qty"]) for o in row["owners"]] == [("winner-1", 6)]
    # 장부 기준 평단(가중평균)은 표시, 시세 의존 값은 0
    assert row["avg_price"] == 13_680
    assert row["current_price"] == 0
    assert row["eval_amount"] == 0
    # 같이 보인 정상 종목은 일치
    assert rows["069500"]["mismatch"] == 0


def test_multiple_bots_holding_same_stock_listed_as_separate_owners(db):
    """복수 봇 동일 종목 보유 → owners 2개, ledger_qty 합산."""
    _log(db, source="winner-1", side="buy", code="069500", qty=2, price=40_000,
         minutes_ago=10)
    _log(db, source="mirinae-3", side="buy", code="069500", qty=3, price=40_500)
    balance = {"holdings": [_kis_holding("069500", 5, name="KODEX 200")],
               "summary": {"cash": 0}}
    d, _ = _get(balance=balance)

    (row,) = d["holdings"]
    assert row["ledger_qty"] == 5
    assert row["mismatch"] == 0
    owners = {o["source"]: o["qty"] for o in row["owners"]}
    assert owners == {"winner-1": 2, "mirinae-3": 3}


# ── KIS 장애 대응 (오늘 실사고의 직접 교훈) ──

def test_kis_exception_returns_ok_false_with_ledger_data(db):
    """get_balance 연속 예외 → ok false + balance_error + 장부 데이터는 그대로."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    d, market_cls = _get(exc=RuntimeError("KIS API timeout"))

    assert d["ok"] is False
    assert "KIS API timeout" in d["balance_error"]
    # 예외도 1회 재시도 후 포기
    assert market_cls.return_value.get_balance.call_count == 2
    # 장부 데이터는 살아 있어야 한다 — 봇 귀속이 보이는 것이 핵심
    (row,) = d["holdings"]
    assert row["code"] == "140670"
    assert row["ledger_qty"] == 6
    assert row["mismatch"] == -6
    assert [(o["source"], o["qty"]) for o in row["owners"]] == [("winner-1", 6)]


def test_kis_response_without_holdings_key_is_treated_as_error(db):
    """응답에 holdings 키가 없으면 장애로 간주 — 장부 데이터는 반환."""
    _log(db, source="mirinae-3", side="buy", code="069500", qty=2, price=40_000)
    d, _ = _get(balance={"rt_cd": "1", "msg1": "조회 실패"})

    assert d["ok"] is False
    assert d["balance_error"]
    (row,) = d["holdings"]
    assert row["code"] == "069500"
    assert row["ledger_qty"] == 2


# ── BotLedger 특수 행 가산 검증 ──

def test_phantom_cleanup_and_orphan_assign_rows_affect_ledger_qty(db):
    """phantom-cleanup SELL은 lot 제거, orphan-assign BUY는 lot 추가 —
    BotLedger 규칙이 상황판 ledger_qty에 그대로 반영되는지 가산 검증."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680,
         minutes_ago=30)
    _log(db, source="winner-1", side="sell", code="140670", qty=6, price=13_680,
         minutes_ago=20, reason="phantom-cleanup")
    _log(db, source="winner-1", side="buy", code="140670", qty=2, price=13_700,
         minutes_ago=10, reason="orphan-assign")
    # 실패 거래는 무시되어야 한다
    _log(db, source="winner-1", side="buy", code="140670", qty=99, price=13_700,
         minutes_ago=5, success=0)
    balance = {"holdings": [_kis_holding("140670", 2, name="알에스오토메이션")],
               "summary": {"cash": 0}}
    d, _ = _get(balance=balance)

    (row,) = d["holdings"]
    assert row["ledger_qty"] == 2  # 6 - 6(phantom) + 2(assign), 실패행 무시
    assert row["mismatch"] == 0
    assert [(o["source"], o["qty"]) for o in row["owners"]] == [("winner-1", 2)]


# ── 플래핑 대응: 의심-빈-응답 + 재시도 + 최종 정상값 캐시 ──

GOOD_BALANCE = {
    "holdings": [_kis_holding("140670", 6, name="알에스오토메이션",
                              avg=13_680, cur=13_720, profit=240, rate=0.29)],
    "summary": {"cash": 137_242},
}
EMPTY_BALANCE = {"holdings": [], "summary": {"cash": 0}}


def test_suspicious_empty_then_good_retry_recovers(db):
    """(a) 빈 응답 → 재시도 정상 → ok true + 정상 렌더 데이터 (플래핑 실측 패턴)."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    d, market_cls = _get(seq=[EMPTY_BALANCE, GOOD_BALANCE])

    assert market_cls.return_value.get_balance.call_count == 2
    assert d["ok"] is True
    assert d["stale"] is False
    assert d["balance_error"] is None
    assert d["cash"] == 137_242
    (row,) = d["holdings"]
    assert row["kis_qty"] == 6
    assert row["mismatch"] == 0


def test_consecutive_failures_serve_last_good_cache_as_stale(db):
    """(b) 연속 실패 + 캐시 있음 → stale true + 캐시 데이터 서빙."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    # 1차 요청: 정상 → 캐시 적재
    d1, _ = _get(balance=GOOD_BALANCE)
    assert d1["ok"] is True and d1["stale"] is False
    cached_as_of = d1["as_of"]

    # 2차 요청: 연속 빈 응답 (재시도 포함 2회 모두 실패)
    d2, market_cls = _get(seq=[EMPTY_BALANCE, EMPTY_BALANCE])

    assert market_cls.return_value.get_balance.call_count == 2
    assert d2["ok"] is True          # 캐시 기반이므로 빨간 장애 배너는 아님
    assert d2["stale"] is True
    assert d2["balance_error"] == "KIS 잔고 장애 — 마지막 정상값 표시"
    assert d2["as_of"] == cached_as_of  # 캐시 적재 시각
    assert d2["cash"] == 137_242
    (row,) = d2["holdings"]
    assert row["kis_qty"] == 6
    assert row["mismatch"] == 0
    assert [(o["source"], o["qty"]) for o in row["owners"]] == [("winner-1", 6)]


def test_consecutive_failures_without_cache_fall_back_to_ok_false(db):
    """(c) 연속 실패 + 캐시 없음 → 기존 ok false 경로 (장부 데이터는 반환)."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    d, market_cls = _get(seq=[EMPTY_BALANCE, EMPTY_BALANCE])

    assert market_cls.return_value.get_balance.call_count == 2
    assert d["ok"] is False
    assert d["stale"] is False
    assert d["balance_error"]
    (row,) = d["holdings"]
    assert row["code"] == "140670"
    assert row["ledger_qty"] == 6
    assert row["mismatch"] == -6


def test_all_zero_qty_response_is_also_suspicious(db):
    """(d) 전부 qty 0인 응답도 의심 판정 → 재시도로 복구."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    zero_balance = {"holdings": [_kis_holding("140670", 0)], "summary": {"cash": 0}}
    d, market_cls = _get(seq=[zero_balance, GOOD_BALANCE])

    assert market_cls.return_value.get_balance.call_count == 2
    assert d["ok"] is True
    assert d["stale"] is False
    (row,) = d["holdings"]
    assert row["mismatch"] == 0


def test_empty_kis_with_empty_ledger_is_normal_not_suspicious(db):
    """장부도 비어 있으면 빈 KIS 응답은 정상 — 재시도 없이 1회 호출."""
    d, market_cls = _get(balance=EMPTY_BALANCE)

    assert market_cls.return_value.get_balance.call_count == 1
    assert d["ok"] is True
    assert d["stale"] is False
    assert d["holdings"] == []


# ── /api/account/balance — Account 카드도 동일한 플래핑 대응 ──

def _get_account(balance=None, exc=None, seq=None):
    market_p, market_cls = _market_patch(balance=balance, exc=exc, seq=seq)
    with market_p:
        resp = _client().get("/api/account/balance")
    assert resp.status_code == 200
    return resp.json(), market_cls


def test_account_balance_normal_keeps_legacy_shape(db):
    """정상 응답 — 기존 holdings/summary 형태 유지 + stale 필드 추가."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    d, _ = _get_account(balance=GOOD_BALANCE)

    assert d["stale"] is False
    assert d["balance_error"] is None
    assert d["summary"]["cash"] == 137_242
    (h,) = d["holdings"]
    assert h["stock_code"] == "140670"  # KIS 원형 그대로 (드롭다운 호환)
    assert h["quantity"] == 6


def test_account_balance_retries_suspicious_empty_response(db):
    """플래핑 빈 응답 → 재시도 정상 → 정상값 반환."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    d, market_cls = _get_account(seq=[EMPTY_BALANCE, GOOD_BALANCE])

    assert market_cls.return_value.get_balance.call_count == 2
    assert d["stale"] is False
    assert d["summary"]["cash"] == 137_242
    assert d["holdings"][0]["quantity"] == 6


def test_account_balance_serves_cache_as_stale_on_consecutive_failure(db):
    """연속 실패 + 캐시 → stale true + 캐시 holdings/summary 서빙.

    수동 주문 패널의 보유 드롭다운도 이 응답을 먹으므로,
    캐시 서빙 시 holdings가 KIS 원형 그대로 살아 있어야 한다.
    """
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    d1, _ = _get_account(balance=GOOD_BALANCE)  # 캐시 적재
    d2, market_cls = _get_account(seq=[EMPTY_BALANCE, RuntimeError("KIS 500")])

    assert market_cls.return_value.get_balance.call_count == 2
    assert d2["stale"] is True
    assert d2["balance_error"] == "KIS 잔고 장애 — 마지막 정상값 표시"
    assert d2["as_of"] == d1["as_of"]
    assert d2["summary"]["cash"] == 137_242
    (h,) = d2["holdings"]
    assert h["stock_code"] == "140670"
    assert h["quantity"] == 6


def test_account_balance_total_failure_without_cache_returns_empty(db):
    """연속 실패 + 캐시 없음 → 빈 holdings/summary (UI는 기존처럼 '-')."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    d, _ = _get_account(seq=[EMPTY_BALANCE, EMPTY_BALANCE])

    assert d["stale"] is False
    assert d["balance_error"]
    assert d["holdings"] == []
    assert d["summary"] == {}


def test_last_good_cache_shared_between_overview_and_account_balance(db):
    """캐시는 두 라우트가 공유 — 상황판이 적재한 정상값을 Account 카드도 사용."""
    _log(db, source="winner-1", side="buy", code="140670", qty=6, price=13_680)
    d1, _ = _get(balance=GOOD_BALANCE)  # overview가 캐시 적재
    assert d1["ok"] is True

    d2, _ = _get_account(exc=RuntimeError("KIS down"))
    assert d2["stale"] is True
    assert d2["holdings"][0]["quantity"] == 6
    assert d2["summary"]["cash"] == 137_242
