"""POST /api/manual/order — 대시보드 수동 매수/매도.

실주문 절대 금지: KISOrder/KISMarket/get_db 전부 mock, 모드 파일은 tmp_path.
"""

import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _client():
    from src.web.app import app
    return TestClient(app)


def _mode_file(tmp_path, mode="paper", max_order=500_000):
    f = tmp_path / "trading_mode.json"
    f.write_text(json.dumps({"mode": mode, "max_order_amount": max_order}),
                 encoding="utf-8")
    return f


def _order_result(success=True, order_no="0001234", price=0, message=""):
    from src.kis.order import OrderResult
    return OrderResult(success=success, order_no=order_no, message=message,
                       price=price)


def _mocks(tmp_path, mode="paper", max_order=500_000, cur_price=70_000):
    """공통 patch 세트 — (mode_file, KISOrder, KISMarket, get_db)."""
    order_cls = MagicMock()
    order = order_cls.return_value
    order.buy.return_value = _order_result()
    order.sell.return_value = _order_result()

    market_cls = MagicMock()
    market_cls.return_value.get_price.return_value = {"price": cur_price}

    db = MagicMock()
    return (
        patch("src.trading_mode.MODE_FILE", _mode_file(tmp_path, mode, max_order)),
        patch("src.kis.order.KISOrder", order_cls),
        patch("src.kis.market.KISMarket", market_cls),
        patch("src.db.get_db", return_value=db),
        order_cls,
        market_cls,
        db,
    )


def test_buy_market_success_logs_manual_trade(tmp_path):
    mode_p, order_p, market_p, db_p, order_cls, market_cls, db = _mocks(tmp_path)
    with mode_p, order_p, market_p, db_p, \
         patch("src.kis.master.stock_name_by_code", return_value="삼성전자"):
        resp = _client().post("/api/manual/order",
                              json={"side": "buy", "code": "005930", "qty": 2, "price": 0})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["order_no"] == "0001234"
    assert data["side"] == "buy"
    assert data["code"] == "005930"
    assert data["qty"] == 2
    # MARKET 체결가 폴백 — OrderResult.price=0이므로 현재가 70,000
    assert data["price"] == 70_000

    # KISOrder.buy 호출 인자 — 시장가(MARKET)
    from src.kis.order import OrderType
    order = order_cls.return_value
    order.buy.assert_called_once()
    kwargs = order.buy.call_args.kwargs
    assert kwargs["stock_code"] == "005930"
    assert kwargs["quantity"] == 2
    assert kwargs["price"] == 0
    assert kwargs["order_type"] == OrderType.MARKET
    order.sell.assert_not_called()
    order.close.assert_called_once()

    # log_trade — pipe_id="manual", source="manual"
    db.log_trade.assert_called_once()
    args = db.log_trade.call_args
    assert args.args[0] == "manual"           # pipe_id
    assert args.args[1] == "005930"           # stock_code
    assert args.args[2] == "삼성전자"          # stock_name
    assert args.args[3] == "buy"              # side
    assert args.args[4] == 2                  # quantity
    assert args.args[5] == 70_000             # price (체결가 폴백)
    assert args.kwargs["source"] == "manual"
    assert args.kwargs["reason"] == "수동 주문 (대시보드)"


def test_sell_limit_uses_limit_type_and_request_price(tmp_path):
    mode_p, order_p, market_p, db_p, order_cls, market_cls, db = _mocks(tmp_path)
    with mode_p, order_p, market_p, db_p:
        resp = _client().post("/api/manual/order",
                              json={"side": "sell", "code": "069500", "qty": 3, "price": 41_000})

    data = resp.json()
    assert data["success"] is True
    assert data["price"] == 41_000

    from src.kis.order import OrderType
    order = order_cls.return_value
    order.sell.assert_called_once()
    kwargs = order.sell.call_args.kwargs
    assert kwargs["stock_code"] == "069500"
    assert kwargs["quantity"] == 3
    assert kwargs["price"] == 41_000
    assert kwargs["order_type"] == OrderType.LIMIT
    order.buy.assert_not_called()

    # 지정가는 현재가 조회 불필요
    market_cls.return_value.get_price.assert_not_called()

    # log_trade 가격 = 요청가
    assert db.log_trade.call_args.args[5] == 41_000


def test_invalid_inputs_rejected_without_order(tmp_path):
    bad_payloads = [
        {"side": "hold", "code": "005930", "qty": 1, "price": 0},   # side
        {"side": "buy", "code": "0593", "qty": 1, "price": 0},      # code 길이
        {"side": "buy", "code": "00593a", "qty": 1, "price": 0},    # code 문자
        {"side": "buy", "code": "00593０", "qty": 1, "price": 0},   # 전각 숫자
        {"side": "buy", "code": "005930", "qty": 0, "price": 0},    # qty
        {"side": "buy", "code": "005930", "qty": -1, "price": 0},   # qty 음수
        {"side": "buy", "code": "005930", "qty": 1, "price": -100}, # price 음수
    ]
    mode_p, order_p, market_p, db_p, order_cls, _, db = _mocks(tmp_path)
    with mode_p, order_p, market_p, db_p:
        client = _client()
        for payload in bad_payloads:
            resp = client.post("/api/manual/order", json=payload)
            assert resp.status_code == 200, payload
            data = resp.json()
            assert data["success"] is False, payload
            assert data["error"], payload

    order_cls.assert_not_called()
    db.log_trade.assert_not_called()


def test_market_order_rejected_when_price_lookup_returns_none(tmp_path):
    mode_p, order_p, market_p, db_p, order_cls, market_cls, db = _mocks(tmp_path)
    market_cls.return_value.get_price.return_value = None
    with mode_p, order_p, market_p, db_p:
        resp = _client().post("/api/manual/order",
                              json={"side": "buy", "code": "005930", "qty": 1, "price": 0})

    data = resp.json()
    assert data["success"] is False
    assert "현재가 조회 실패" in data["error"]
    order_cls.assert_not_called()
    db.log_trade.assert_not_called()


def test_market_order_rejected_when_price_is_zero(tmp_path):
    mode_p, order_p, market_p, db_p, order_cls, market_cls, db = _mocks(tmp_path)
    market_cls.return_value.get_price.return_value = {"price": 0}
    with mode_p, order_p, market_p, db_p:
        resp = _client().post("/api/manual/order",
                              json={"side": "sell", "code": "005930", "qty": 1, "price": 0})

    data = resp.json()
    assert data["success"] is False
    assert "현재가 조회 실패" in data["error"]
    order_cls.assert_not_called()
    db.log_trade.assert_not_called()


def test_disabled_mode_blocks_order(tmp_path):
    mode_p, order_p, market_p, db_p, order_cls, _, db = _mocks(tmp_path, mode="disabled")
    with mode_p, order_p, market_p, db_p:
        resp = _client().post("/api/manual/order",
                              json={"side": "buy", "code": "005930", "qty": 1, "price": 70_000})

    data = resp.json()
    assert data["success"] is False
    assert "비활성화" in data["error"]
    order_cls.assert_not_called()
    db.log_trade.assert_not_called()


def test_validate_order_rejects_over_max_amount(tmp_path):
    # max_order_amount=100,000 < 70,000 × 2 = 140,000
    mode_p, order_p, market_p, db_p, order_cls, _, db = _mocks(tmp_path, max_order=100_000)
    with mode_p, order_p, market_p, db_p:
        resp = _client().post("/api/manual/order",
                              json={"side": "buy", "code": "005930", "qty": 2, "price": 70_000})

    data = resp.json()
    assert data["success"] is False
    assert "최대 주문액" in data["error"]
    order_cls.assert_not_called()
    db.log_trade.assert_not_called()


def test_order_failure_returns_error_and_logs(tmp_path):
    mode_p, order_p, market_p, db_p, order_cls, _, db = _mocks(tmp_path)
    order_cls.return_value.buy.return_value = _order_result(
        success=False, order_no="", message="잔고 부족")
    with mode_p, order_p, market_p, db_p:
        resp = _client().post("/api/manual/order",
                              json={"side": "buy", "code": "005930", "qty": 1, "price": 70_000})

    data = resp.json()
    assert data["success"] is False
    assert "잔고 부족" in data["error"]
    # 실패도 DB에 기록 (success=False)
    db.log_trade.assert_called_once()
    assert db.log_trade.call_args.kwargs["success"] is False
    order_cls.return_value.close.assert_called_once()
