"""Tests for board post enrichment."""

from src.db import DB


def test_get_board_adds_full_trade_content_from_trade_reason(tmp_path):
    db = DB(tmp_path / "ff.db")
    long_reason = (
        "mirinae-3 매도 (실현:+10,268원) | "
        "사유:이 포지션은 이미 계획된 목표를 달성한 승자다. "
        "하지만 승자가 계속 달리는 조건인 단기 추세가 약해지고 있으므로 절반을 매도한다. | "
        "기준:진입:29,558, 판단현재:32,125, 체결:32,125, 목표:30,600, 손절:29,250"
    )
    db.log_trade(
        pipe_id="strat-001",
        stock_code="102970",
        stock_name="KODEX 증권",
        side="sell",
        quantity=4,
        price=32125,
        order_no="ORD",
        reason=long_reason,
        source="mirinae-3",
        realized_pnl=10268,
    )
    db.post_board(
        "strat-001",
        "mirinae-3",
        "trade",
        "매도 102970 4주 @32,125원 (손익 +10,268원) 이 포지션은 이미 계획된 목표를 달성한 승자다. 하지만 승자가 계속 달리",
        "102970",
    )

    posts = db.get_board("strat-001", 1)

    assert "full_content" in posts[0]
    assert "하지만 승자가 계속 달리는 조건" in posts[0]["full_content"]
    assert "기준:진입:29,558" in posts[0]["full_content"]
    db.close()


def test_get_board_adds_full_opinion_content_from_decision(tmp_path):
    db = DB(tmp_path / "ff.db")
    full_reason = (
        "이미 절반 수익을 실현했으므로 남은 포지션은 공짜 옵션에 가깝게 운용한다. "
        "전량 매도할 만큼 추세가 깨지지는 않았지만 기존 손절은 말이 안 된다. "
        "목표를 33500원으로 올리고 손절을 31800원으로 끌어올린다."
    )
    db.log_decision(
        type="bot_hold",
        pipe_id="strat-001",
        output_summary=f"hold: {full_reason}",
        action_taken="mirinae-3:hold",
        bot_id="mirinae-3",
        stock_code="102970",
        action="hold",
        confidence=0.69,
    )
    db.post_board(
        "strat-001",
        "mirinae-3",
        "opinion",
        "[HOLD] 이미 절반 수익을 실현했으므로 남은 포지션은 공짜 옵션에 가깝게 운용한다. 전량 매도할 만큼 추세가",
        "102970",
    )

    posts = db.get_board("strat-001", 1)

    assert posts[0]["full_content"] == f"[HOLD] {full_reason}"
    db.close()


def test_get_board_does_not_attach_mismatched_opinion_decision(tmp_path):
    db = DB(tmp_path / "ff.db")
    db.log_decision(
        type="bot_hold",
        pipe_id="strat-001",
        output_summary="hold: 최신 판단 전문",
        action_taken="mirinae-3:hold",
        bot_id="mirinae-3",
        stock_code="102970",
        action="hold",
        confidence=0.69,
    )
    board_id = db.post_board(
        "strat-001",
        "mirinae-3",
        "opinion",
        "[HOLD] 과거에 잘린 판단",
        "102970",
    )
    db._conn.execute(
        "UPDATE board SET timestamp=? WHERE id=?",
        ("2000-01-01T00:00:00+09:00", board_id),
    )
    db._conn.commit()

    posts = db.get_board("strat-001", 1)

    assert "full_content" not in posts[0]
    db.close()
