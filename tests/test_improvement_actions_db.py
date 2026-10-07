"""improvement_actions 테이블 마이그레이션·헬퍼 테스트."""

import json

from src.db import DB


EXPECTED_COLUMNS = {
    "id", "action_id", "timestamp", "updated_at",
    "target_type", "target", "before_json", "after_json",
    "rationale", "validation_json", "status",
    "tracking_window_json", "result_json",
}


def column_names(db: DB, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def index_names(db: DB) -> set[str]:
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_improvement_actions_table_created_idempotently(tmp_path):
    db = DB(tmp_path / "ops.db")
    db._create_tables()
    db._create_tables()

    assert EXPECTED_COLUMNS <= column_names(db, "improvement_actions")
    assert {
        "idx_improvement_actions_action_id",
        "idx_improvement_actions_target",
        "idx_improvement_actions_status",
        "idx_improvement_actions_ts",
    } <= index_names(db)


def test_create_and_get_improvement_action(tmp_path):
    db = DB(tmp_path / "ops.db")
    row_id = db.create_improvement_action(
        "act-001",
        target_type="param",
        target="mirinae-2:confidence_threshold",
        before={"value": 0.6},
        after={"value": 0.65},
        rationale="confidence 0.6 구간 승률 28%",
        validation={"sample": 34, "ev_estimate": 120.5},
        tracking_window={"trades": 20, "days": 5},
    )
    assert row_id > 0

    action = db.get_improvement_action("act-001")
    assert action["target_type"] == "param"
    assert action["target"] == "mirinae-2:confidence_threshold"
    assert action["status"] == "proposed"
    assert json.loads(action["before_json"]) == {"value": 0.6}
    assert json.loads(action["after_json"]) == {"value": 0.65}
    assert json.loads(action["validation_json"])["sample"] == 34
    assert json.loads(action["tracking_window_json"])["trades"] == 20


def test_create_improvement_action_is_idempotent_per_action_id(tmp_path):
    db = DB(tmp_path / "ops.db")
    first = db.create_improvement_action("act-001", target_type="param", target="a:b")
    second = db.create_improvement_action("act-001", target_type="param", target="a:b")
    assert first == second


def test_update_improvement_action_status_and_result(tmp_path):
    db = DB(tmp_path / "ops.db")
    db.create_improvement_action("act-001", target_type="ratio", target="strat-001:ratios")

    db.update_improvement_action("act-001", status="applied")
    assert db.get_improvement_action("act-001")["status"] == "applied"

    db.update_improvement_action(
        "act-001", status="rolled_back",
        result={"before_ev": 50, "after_ev": -20, "verdict": "worse"},
    )
    action = db.get_improvement_action("act-001")
    assert action["status"] == "rolled_back"
    assert json.loads(action["result_json"])["verdict"] == "worse"


def test_update_improvement_action_rejects_unknown_status(tmp_path):
    db = DB(tmp_path / "ops.db")
    db.create_improvement_action("act-001", target_type="param", target="a:b")
    db.update_improvement_action("act-001", status="bogus")
    assert db.get_improvement_action("act-001")["status"] == "proposed"


def test_get_improvement_actions_filters(tmp_path):
    db = DB(tmp_path / "ops.db")
    db.create_improvement_action("act-001", target_type="param", target="m2:x")
    db.create_improvement_action("act-002", target_type="ratio", target="strat-001:ratios")
    db.update_improvement_action("act-002", status="applied")

    assert len(db.get_improvement_actions()) == 2
    assert [a["action_id"] for a in db.get_improvement_actions(target_type="param")] == ["act-001"]
    assert [a["action_id"] for a in db.get_improvement_actions(status="applied")] == ["act-002"]
    assert [a["action_id"] for a in db.get_improvement_actions(target="m2:x")] == ["act-001"]


def test_get_improvement_actions_days_filter_uses_kst_cutoff(tmp_path):
    from datetime import timedelta
    from src.db import _kst_now

    db = DB(tmp_path / "ops.db")
    db.create_improvement_action("act-recent", target_type="param", target="m2:x")
    db.create_improvement_action("act-old", target_type="param", target="m2:x")
    db.create_improvement_action("act-boundary", target_type="param", target="m2:x")

    def backdate(action_id: str, dt):
        iso = dt.isoformat()
        db._conn.execute(
            "UPDATE improvement_actions SET timestamp=?, updated_at=? WHERE action_id=?",
            (iso, iso, action_id),
        )
        db._conn.commit()

    # 명확히 윈도우 밖: 10일 전
    backdate("act-old", _kst_now() - timedelta(days=10))
    # 경계 케이스: 7일 + 2시간 전 — UTC 'now' 기반 컷오프(KST보다 9시간 이전)면
    # 잘못 포함되는 구간
    backdate("act-boundary", _kst_now() - timedelta(days=7, hours=2))

    actions = db.get_improvement_actions(days=7)
    assert [a["action_id"] for a in actions] == ["act-recent"]


def test_make_action_id_format_and_uniqueness():
    from src.bots.audit import make_action_id

    first = make_action_id("param", "mirinae-2:confidence_threshold")
    second = make_action_id("param", "mirinae-2:confidence_threshold")

    assert first.startswith("param:mirinae-2:confidence_threshold:")
    assert first != second  # uuid suffix
    assert len(first.split(":")) == 5  # type:bot:key:timestamp:uuid
