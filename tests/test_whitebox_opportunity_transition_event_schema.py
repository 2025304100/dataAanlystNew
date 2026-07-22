"""白盒测试 - WP3.1 OpportunityTransitionEvent 审计表。

覆盖机会状态流转审计事件的 schema、默认值、唯一约束、取值范围、
SQLite/MySQL schema 补丁幂等性以及审计链查询能力。

测试维度：
1. 表存在性（init_db 后表存在）
2. 字段完整性 + 默认值
3. actor_type 默认值 'user'
4. 幂等键唯一索引（重复键抛 IntegrityError）
5. event_type 各取值
6. source_type / target_type 各取值
7. actor_type 各取值
8. reason_json 读写 + 解析
9. SQLite schema 补丁幂等
10. MySQL 补丁函数存在且可调用
11. 索引存在性
12. 审计链查询（按 symbol_id 查询多条事件，按 created_at 排序）
13. 按 idempotency_key 查询
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db.init_db import (
    _ensure_mysql_opportunity_transition_events_table,
    _ensure_sqlite_opportunity_transition_events_table,
    init_db,
)
from app.models.opportunity_transition_event import (
    ACTOR_MIGRATION,
    ACTOR_SYSTEM,
    ACTOR_USER,
    EVENT_CANDIDATE_TO_OBSERVATION,
    EVENT_CANDIDATE_TO_PORTFOLIO,
    EVENT_EXCLUDE,
    EVENT_EXPIRE,
    EVENT_MEMBER_ARCHIVE_TO_OBSERVATION,
    EVENT_OBSERVATION_TO_PORTFOLIO,
    EVENT_RESTORE,
    OpportunityTransitionEvent,
    TYPE_CANDIDATE,
    TYPE_OBSERVATION,
    TYPE_PORTFOLIO_MEMBER,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_event(
    *,
    symbol_id: int = 1,
    event_type: str = EVENT_CANDIDATE_TO_OBSERVATION,
    source_type: str = TYPE_CANDIDATE,
    source_id: int = 100,
    target_type: str = TYPE_OBSERVATION,
    target_id: int = 200,
    from_status: str | None = None,
    to_status: str = "watching",
    reason_json: str | None = None,
    idempotency_key: str = "idem-001",
    actor_type: str | None = None,
) -> OpportunityTransitionEvent:
    """构造一条审计事件（actor_type=None 时使用模型默认值）。"""
    kwargs: dict = {
        "symbol_id": symbol_id,
        "event_type": event_type,
        "source_type": source_type,
        "source_id": source_id,
        "target_type": target_type,
        "target_id": target_id,
        "from_status": from_status,
        "to_status": to_status,
        "reason_json": reason_json,
        "idempotency_key": idempotency_key,
    }
    if actor_type is not None:
        kwargs["actor_type"] = actor_type
    return OpportunityTransitionEvent(**kwargs)


# ----------------------------------------------------------------------------
# 1. 表存在性
# ----------------------------------------------------------------------------


def test_table_exists_after_init_db(tmp_sqlite_url):
    """【WP3.1】init_db() 后 opportunity_transition_events 表存在。"""
    engine = create_engine(tmp_sqlite_url)
    try:
        # 直接调用 init_db 不现实（依赖全局 DatabaseManager），
        # 这里通过 Base.metadata.create_all + 补丁函数验证表创建。
        Base.metadata.create_all(engine)
        _ensure_sqlite_opportunity_transition_events_table(engine)

        inspector = inspect(engine)
        assert "opportunity_transition_events" in inspector.get_table_names()
    finally:
        engine.dispose()


def test_init_db_creates_table(tmp_sqlite_url):
    """【WP3.1】init_db() 调用后 opportunity_transition_events 表存在。

    通过 DatabaseManager 让 init_db 使用临时 SQLite。
    """
    from app.db.manager import DatabaseManager

    mgr = DatabaseManager.get()
    try:
        mgr.dispose()
    except Exception:
        pass
    mgr.initialize(tmp_sqlite_url, db_type="sqlite")

    try:
        init_db()

        engine = mgr.engine
        inspector = inspect(engine)
        assert "opportunity_transition_events" in inspector.get_table_names()
    finally:
        try:
            mgr.dispose()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 2. 字段完整性 + 默认值
# ----------------------------------------------------------------------------


def test_event_fields_writable(db_session):
    """【WP3.1】所有字段可写入指定值并读回。"""
    event = _make_event(
        symbol_id=42,
        event_type=EVENT_CANDIDATE_TO_OBSERVATION,
        source_type=TYPE_CANDIDATE,
        source_id=100,
        target_type=TYPE_OBSERVATION,
        target_id=200,
        from_status="pending",
        to_status="watching",
        reason_json='{"note":"用户手动加入"}',
        idempotency_key="idem-fields-001",
        actor_type=ACTOR_USER,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.id is not None
    assert event.symbol_id == 42
    assert event.event_type == EVENT_CANDIDATE_TO_OBSERVATION
    assert event.source_type == TYPE_CANDIDATE
    assert event.source_id == 100
    assert event.target_type == TYPE_OBSERVATION
    assert event.target_id == 200
    assert event.from_status == "pending"
    assert event.to_status == "watching"
    assert event.reason_json == '{"note":"用户手动加入"}'
    assert event.idempotency_key == "idem-fields-001"
    assert event.actor_type == ACTOR_USER
    assert event.created_at is not None


def test_event_fields_all_present(db_session):
    """【WP3.1】表中所有声明的字段都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("opportunity_transition_events")}
    expected = {
        "id",
        "symbol_id",
        "event_type",
        "source_type",
        "source_id",
        "target_type",
        "target_id",
        "from_status",
        "to_status",
        "reason_json",
        "idempotency_key",
        "actor_type",
        "created_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


# ----------------------------------------------------------------------------
# 3. 默认值（actor_type）
# ----------------------------------------------------------------------------


def test_actor_type_default_is_user(db_session):
    """【WP3.1】不传 actor_type 时默认值 'user'。"""
    event = _make_event(idempotency_key="idem-default-001")
    # 不设置 actor_type，使用模型默认值
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.actor_type == "user"


def test_created_at_default_set(db_session):
    """【WP3.1】created_at 默认值被设置（非 None）。"""
    event = _make_event(idempotency_key="idem-default-002")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.created_at is not None


# ----------------------------------------------------------------------------
# 4. 幂等键唯一索引
# ----------------------------------------------------------------------------


def test_idempotency_key_unique_constraint(db_session):
    """【WP3.1】相同 idempotency_key 的第二条记录抛出 IntegrityError。"""
    event1 = _make_event(idempotency_key="dup-key-001")
    db_session.add(event1)
    db_session.commit()

    event2 = _make_event(
        idempotency_key="dup-key-001",
        symbol_id=999,  # 不同 symbol 仍然应触发唯一约束
    )
    db_session.add(event2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_idempotency_key_unique_index_exists(db_session):
    """【WP3.1】idempotency_key 字段存在唯一索引。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("opportunity_transition_events")
    # SQLite 中 unique=True 的列会自动创建唯一索引（也包含在 indexes 列表中）
    # 同时显式 index=True 也会创建索引
    idem_indexes = [
        idx for idx in indexes
        if "idempotency_key" in idx.get("column_names", [])
    ]
    assert len(idem_indexes) >= 1, f"idempotency_key 索引不存在: {indexes}"
    # 至少有一个是唯一索引
    has_unique = any(idx.get("unique") for idx in idem_indexes)
    assert has_unique, f"idempotency_key 缺少唯一索引: {idem_indexes}"


# ----------------------------------------------------------------------------
# 5. event_type 各取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type,idx",
    [
        (EVENT_CANDIDATE_TO_OBSERVATION, 1),
        (EVENT_CANDIDATE_TO_PORTFOLIO, 2),
        (EVENT_OBSERVATION_TO_PORTFOLIO, 3),
        (EVENT_EXCLUDE, 4),
        (EVENT_RESTORE, 5),
        (EVENT_EXPIRE, 6),
        (EVENT_MEMBER_ARCHIVE_TO_OBSERVATION, 7),
    ],
)
def test_event_type_values(db_session, event_type, idx):
    """【WP3.1】event_type 支持所有合法取值。"""
    event = _make_event(
        event_type=event_type,
        idempotency_key=f"idem-et-{idx:03d}",
        symbol_id=idx,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.event_type == event_type


# ----------------------------------------------------------------------------
# 6. source_type / target_type 各取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "type_value,idx",
    [
        (TYPE_CANDIDATE, 1),
        (TYPE_OBSERVATION, 2),
        (TYPE_PORTFOLIO_MEMBER, 3),
    ],
)
def test_source_type_values(db_session, type_value, idx):
    """【WP3.1】source_type 支持所有合法取值。"""
    event = _make_event(
        source_type=type_value,
        idempotency_key=f"idem-st-{idx:03d}",
        symbol_id=idx + 100,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.source_type == type_value


@pytest.mark.parametrize(
    "type_value,idx",
    [
        (TYPE_CANDIDATE, 1),
        (TYPE_OBSERVATION, 2),
        (TYPE_PORTFOLIO_MEMBER, 3),
    ],
)
def test_target_type_values(db_session, type_value, idx):
    """【WP3.1】target_type 支持所有合法取值。"""
    event = _make_event(
        target_type=type_value,
        idempotency_key=f"idem-tt-{idx:03d}",
        symbol_id=idx + 200,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.target_type == type_value


# ----------------------------------------------------------------------------
# 7. actor_type 各取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "actor_type,idx",
    [
        (ACTOR_USER, 1),
        (ACTOR_SYSTEM, 2),
        (ACTOR_MIGRATION, 3),
    ],
)
def test_actor_type_values(db_session, actor_type, idx):
    """【WP3.1】actor_type 支持所有合法取值。"""
    event = _make_event(
        actor_type=actor_type,
        idempotency_key=f"idem-at-{idx:03d}",
        symbol_id=idx + 300,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.actor_type == actor_type


# ----------------------------------------------------------------------------
# 8. reason_json 读写
# ----------------------------------------------------------------------------


def test_reason_json_read_write(db_session):
    """【WP3.1】reason_json 可写入 JSON 字符串并读回解析为 dict。"""
    payload = {"note": "用户手动加入", "ui_source": "candidate_pool"}
    event = _make_event(
        reason_json=json.dumps(payload, ensure_ascii=False),
        idempotency_key="idem-reason-001",
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.reason_json is not None
    parsed = json.loads(event.reason_json)
    assert parsed == payload
    assert isinstance(parsed, dict)
    assert parsed["note"] == "用户手动加入"
    assert parsed["ui_source"] == "candidate_pool"


def test_reason_json_nullable(db_session):
    """【WP3.1】reason_json 可为 None。"""
    event = _make_event(
        reason_json=None,
        idempotency_key="idem-reason-002",
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.reason_json is None


# ----------------------------------------------------------------------------
# 9. SQLite schema 补丁幂等
# ----------------------------------------------------------------------------


def test_ensure_sqlite_patch_idempotent_when_table_exists(db_session):
    """【WP3.1】表已存在时多次调用 SQLite 补丁不报错。"""
    engine = db_session.bind
    # 表已由 create_all 创建
    _ensure_sqlite_opportunity_transition_events_table(engine)
    # 第二次调用不应报错（幂等）
    _ensure_sqlite_opportunity_transition_events_table(engine)

    inspector = inspect(engine)
    assert "opportunity_transition_events" in inspector.get_table_names()


def test_ensure_sqlite_patch_creates_table_when_missing():
    """【WP3.1】表不存在时 SQLite 补丁创建表。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_ote_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 仅创建 Base.metadata 中的其他表，但不创建 opportunity_transition_events
        # 通过先 create_all 然后手动 drop 目标表模拟"表不存在"场景
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text(
                "DROP TABLE IF EXISTS opportunity_transition_events"
            ))

        inspector = inspect(engine)
        assert "opportunity_transition_events" not in inspector.get_table_names()

        # 调用补丁函数
        _ensure_sqlite_opportunity_transition_events_table(engine)

        # 表应已创建
        inspector = inspect(engine)
        assert "opportunity_transition_events" in inspector.get_table_names()

        # 再次调用（幂等）
        _ensure_sqlite_opportunity_transition_events_table(engine)
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 10. MySQL 补丁函数存在且可调用
# ----------------------------------------------------------------------------


def test_mysql_patch_function_exists_and_callable():
    """【WP3.1】_ensure_mysql_opportunity_transition_events_table 函数存在且可调用。

    不需要真实 MySQL 实例，仅验证函数定义存在且可被调用。
    """
    assert callable(_ensure_mysql_opportunity_transition_events_table)
    import inspect as _inspect
    sig = _inspect.signature(_ensure_mysql_opportunity_transition_events_table)
    params = list(sig.parameters.keys())
    assert len(params) == 1
    assert params[0] == "engine"


# ----------------------------------------------------------------------------
# 11. 索引存在性
# ----------------------------------------------------------------------------


def test_indexes_exist(db_session):
    """【WP3.1】所有声明的索引都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("opportunity_transition_events")
    index_names = {idx["name"] for idx in indexes}

    expected_indexes = {
        "idx_ote_symbol_event",
        "idx_ote_source",
        "idx_ote_target",
        "idx_ote_created",
    }
    assert expected_indexes.issubset(index_names), (
        f"缺失索引: {expected_indexes - index_names}; 实际: {index_names}"
    )


def test_column_indexes_exist(db_session):
    """【WP3.1】字段级 index=True 的索引存在（symbol_id / event_type / source_id / target_id / idempotency_key / created_at）。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("opportunity_transition_events")

    # 收集所有被索引覆盖的列
    indexed_columns = set()
    for idx in indexes:
        for col in idx.get("column_names", []) or []:
            indexed_columns.add(col)

    # 这些列在模型中声明了 index=True 或 unique=True
    expected_indexed_columns = {
        "symbol_id",
        "event_type",
        "source_id",
        "target_id",
        "idempotency_key",
        "created_at",
    }
    assert expected_indexed_columns.issubset(indexed_columns), (
        f"缺失列索引: {expected_indexed_columns - indexed_columns}; "
        f"实际索引列: {indexed_columns}"
    )


# ----------------------------------------------------------------------------
# 12. 审计链查询
# ----------------------------------------------------------------------------


def test_audit_chain_query_by_symbol(db_session):
    """【WP3.1】按 symbol_id 查询审计链，返回多条事件并按 created_at 排序。"""
    symbol_id = 7777

    # 创建 3 条同 symbol_id 不同 event_type 的事件
    events_data = [
        {
            "event_type": EVENT_CANDIDATE_TO_OBSERVATION,
            "idempotency_key": "chain-001",
            "to_status": "watching",
            "source_id": 100,
            "target_id": 200,
        },
        {
            "event_type": EVENT_OBSERVATION_TO_PORTFOLIO,
            "idempotency_key": "chain-002",
            "to_status": "active",
            "source_id": 200,
            "target_id": 300,
        },
        {
            "event_type": EVENT_EXCLUDE,
            "idempotency_key": "chain-003",
            "to_status": "excluded",
            "source_id": 300,
            "target_id": 300,
        },
    ]

    for data in events_data:
        event = _make_event(
            symbol_id=symbol_id,
            event_type=data["event_type"],
            idempotency_key=data["idempotency_key"],
            to_status=data["to_status"],
            source_id=data["source_id"],
            target_id=data["target_id"],
        )
        db_session.add(event)
        db_session.commit()

    # 按 symbol_id 查询，按 created_at 排序
    audit_chain = (
        db_session.query(OpportunityTransitionEvent)
        .filter_by(symbol_id=symbol_id)
        .order_by(OpportunityTransitionEvent.created_at.asc())
        .all()
    )

    assert len(audit_chain) == 3
    # 验证事件按创建顺序排列
    assert audit_chain[0].event_type == EVENT_CANDIDATE_TO_OBSERVATION
    assert audit_chain[1].event_type == EVENT_OBSERVATION_TO_PORTFOLIO
    assert audit_chain[2].event_type == EVENT_EXCLUDE


def test_audit_chain_isolates_by_symbol(db_session):
    """【WP3.1】按 symbol_id 查询不会返回其他 symbol 的事件。"""
    # symbol_id=1 的事件
    e1 = _make_event(symbol_id=1, idempotency_key="iso-001")
    # symbol_id=2 的事件
    e2 = _make_event(symbol_id=2, idempotency_key="iso-002")
    db_session.add_all([e1, e2])
    db_session.commit()

    chain = (
        db_session.query(OpportunityTransitionEvent)
        .filter_by(symbol_id=1)
        .all()
    )
    assert len(chain) == 1
    assert chain[0].symbol_id == 1
    assert chain[0].idempotency_key == "iso-001"


# ----------------------------------------------------------------------------
# 13. 按 idempotency_key 查询
# ----------------------------------------------------------------------------


def test_query_by_idempotency_key(db_session):
    """【WP3.1】按 idempotency_key 查询返回对应记录。"""
    event = _make_event(
        symbol_id=8888,
        idempotency_key="query-idem-001",
        event_type=EVENT_RESTORE,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    found = (
        db_session.query(OpportunityTransitionEvent)
        .filter_by(idempotency_key="query-idem-001")
        .one()
    )
    assert found.id == event.id
    assert found.symbol_id == 8888
    assert found.event_type == EVENT_RESTORE


def test_query_by_idempotency_key_returns_none_when_not_found(db_session):
    """【WP3.1】按不存在的 idempotency_key 查询返回空结果。"""
    found = (
        db_session.query(OpportunityTransitionEvent)
        .filter_by(idempotency_key="non-existent-key-99999")
        .first()
    )
    assert found is None


# ----------------------------------------------------------------------------
# 综合场景：from_status / to_status 流转对
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_status,to_status,idx",
    [
        (None, "watching", 1),
        ("watching", "ready", 2),
        ("ready", "active", 3),
        ("active", "archived", 4),
        ("archived", "watching", 5),
    ],
)
def test_status_transitions(db_session, from_status, to_status, idx):
    """【WP3.1】from_status（含 None）与 to_status 流转对可写入。"""
    event = _make_event(
        from_status=from_status,
        to_status=to_status,
        idempotency_key=f"idem-status-{idx:03d}",
        symbol_id=idx + 500,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.from_status == from_status
    assert event.to_status == to_status
