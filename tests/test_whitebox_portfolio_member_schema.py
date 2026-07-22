"""白盒测试 - WP4.1 PortfolioMember 组合成员表。

覆盖组合成员模型的 schema、默认值、部分唯一索引（同一组合同一标的只能
存在一条当前有效成员关系）、取值范围以及 SQLite/MySQL schema 补丁幂等性。

测试维度：
1. 表存在性（init_db 后表存在）
2. 字段完整性
3. 默认值
4. 部分唯一索引（同一组合同一标的只能存在一条当前有效成员）
5. 不同组合可以有相同标的
6. 归档成员后可重新创建
7. status 各取值
8. execution_mode 各取值
9. source_type 各取值
10. manual_lock 字段
11. SQLite schema 补丁幂等
12. MySQL 补丁函数存在且可调用
13. 索引存在性
14. 查询有效成员（effective_to IS NULL）
15. effective_from 默认值
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db.init_db import (
    _ensure_mysql_portfolio_members_table,
    _ensure_sqlite_portfolio_members_table,
    init_db,
)
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    EXECUTION_CONFIRM,
    EXECUTION_MANUAL,
    PortfolioMember,
    SOURCE_CANDIDATE,
    SOURCE_LEGACY_POSITION,
    SOURCE_MANUAL,
    SOURCE_OBSERVATION,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_PAUSED,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_member(
    *,
    portfolio_id: int = 1,
    symbol_id: int = 1,
    status: str | None = None,
    execution_mode: str | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    entry_rule_version_id: int | None = None,
    exit_rule_version_id: int | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    manual_lock: bool | None = None,
    priority: int | None = None,
    note: str | None = None,
) -> PortfolioMember:
    """构造一个 PortfolioMember（未传字段使用模型默认值）。"""
    kwargs: dict = {
        "portfolio_id": portfolio_id,
        "symbol_id": symbol_id,
    }
    if status is not None:
        kwargs["status"] = status
    if execution_mode is not None:
        kwargs["execution_mode"] = execution_mode
    if source_type is not None:
        kwargs["source_type"] = source_type
    if source_id is not None:
        kwargs["source_id"] = source_id
    if entry_rule_version_id is not None:
        kwargs["entry_rule_version_id"] = entry_rule_version_id
    if exit_rule_version_id is not None:
        kwargs["exit_rule_version_id"] = exit_rule_version_id
    if effective_from is not None:
        kwargs["effective_from"] = effective_from
    if effective_to is not None:
        kwargs["effective_to"] = effective_to
    if manual_lock is not None:
        kwargs["manual_lock"] = manual_lock
    if priority is not None:
        kwargs["priority"] = priority
    if note is not None:
        kwargs["note"] = note
    return PortfolioMember(**kwargs)


# ----------------------------------------------------------------------------
# 1. 表存在性
# ----------------------------------------------------------------------------


def test_table_exists_after_init_db(tmp_sqlite_url):
    """【WP4.1】init_db() 后 portfolio_members 表存在。"""
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
        assert "portfolio_members" in inspector.get_table_names()
    finally:
        try:
            mgr.dispose()
        except Exception:
            pass


def test_table_exists_after_metadata_create_all(db_session):
    """【WP4.1】Base.metadata.create_all 后 portfolio_members 表存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    assert "portfolio_members" in inspector.get_table_names()


# ----------------------------------------------------------------------------
# 2. 字段完整性
# ----------------------------------------------------------------------------


def test_member_all_fields_writable(db_session):
    """【WP4.1】所有字段可写入指定值并读回。"""
    effective_from = datetime(2024, 1, 1, 10, 0, 0)
    effective_to = datetime(2024, 6, 1, 12, 0, 0)

    member = _make_member(
        portfolio_id=10,
        symbol_id=20,
        status=STATUS_PAUSED,
        execution_mode=EXECUTION_CONFIRM,
        source_type=SOURCE_CANDIDATE,
        source_id=99,
        entry_rule_version_id=1001,
        exit_rule_version_id=1002,
        effective_from=effective_from,
        effective_to=effective_to,
        manual_lock=True,
        priority=7,
        note="测试成员",
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)

    assert member.id is not None
    assert member.portfolio_id == 10
    assert member.symbol_id == 20
    assert member.status == STATUS_PAUSED
    assert member.execution_mode == EXECUTION_CONFIRM
    assert member.source_type == SOURCE_CANDIDATE
    assert member.source_id == 99
    assert member.entry_rule_version_id == 1001
    assert member.exit_rule_version_id == 1002
    assert member.effective_from == effective_from
    assert member.effective_to == effective_to
    assert member.manual_lock is True
    assert member.priority == 7
    assert member.note == "测试成员"
    assert member.created_at is not None


def test_member_all_columns_present(db_session):
    """【WP4.1】表中所有声明的字段都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("portfolio_members")}
    expected = {
        "id",
        "portfolio_id",
        "symbol_id",
        "status",
        "execution_mode",
        "source_type",
        "source_id",
        "entry_rule_version_id",
        "exit_rule_version_id",
        "effective_from",
        "effective_to",
        "manual_lock",
        "priority",
        "note",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


# ----------------------------------------------------------------------------
# 3. 默认值
# ----------------------------------------------------------------------------


def test_member_defaults(db_session):
    """【WP4.1】不传 status / execution_mode / source_type / manual_lock / priority 时默认值正确。"""
    member = _make_member(portfolio_id=1, symbol_id=1)
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)

    assert member.status == STATUS_ACTIVE
    assert member.execution_mode == EXECUTION_MANUAL
    assert member.source_type == SOURCE_MANUAL
    assert member.source_id is None
    assert member.entry_rule_version_id is None
    assert member.exit_rule_version_id is None
    assert member.effective_to is None
    assert member.manual_lock is False
    assert member.priority == 0
    assert member.note is None
    assert member.updated_at is None


# ----------------------------------------------------------------------------
# 4. 部分唯一索引（同一组合同一标的只能存在一条当前有效成员）
# ----------------------------------------------------------------------------


def test_partial_unique_index_blocks_duplicate_active(db_session):
    """【WP4.1】同一组合同一标的的第二个有效成员（effective_to IS NULL）抛 IntegrityError。"""
    member1 = _make_member(portfolio_id=1, symbol_id=1)
    db_session.add(member1)
    db_session.commit()

    member2 = _make_member(portfolio_id=1, symbol_id=1)
    db_session.add(member2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_partial_unique_index_allows_archived_duplicate(db_session):
    """【WP4.1】归档成员（effective_to 非 NULL）可与新建有效成员共存。"""
    # 创建一个归档成员
    archived = _make_member(
        portfolio_id=1, symbol_id=1,
        effective_to=datetime(2024, 1, 1, 0, 0, 0),
    )
    db_session.add(archived)
    db_session.commit()

    # 再创建一个有效成员（effective_to IS NULL）应成功
    active = _make_member(portfolio_id=1, symbol_id=1)
    db_session.add(active)
    db_session.commit()
    db_session.refresh(active)

    assert archived.id is not None
    assert active.id is not None
    assert archived.id != active.id


def test_partial_unique_index_allows_multiple_archived(db_session):
    """【WP4.1】多条归档成员（effective_to 非 NULL）可共存。"""
    a1 = _make_member(
        portfolio_id=1, symbol_id=1,
        effective_to=datetime(2024, 1, 1, 0, 0, 0),
    )
    a2 = _make_member(
        portfolio_id=1, symbol_id=1,
        effective_to=datetime(2024, 2, 1, 0, 0, 0),
    )
    db_session.add_all([a1, a2])
    db_session.commit()
    assert a1.id is not None
    assert a2.id is not None
    assert a1.id != a2.id


# ----------------------------------------------------------------------------
# 5. 不同组合可以有相同标的
# ----------------------------------------------------------------------------


def test_different_portfolios_same_symbol(db_session):
    """【WP4.1】不同组合可以同时拥有同一标的的有效成员。"""
    m1 = _make_member(portfolio_id=1, symbol_id=1)
    m2 = _make_member(portfolio_id=2, symbol_id=1)
    db_session.add_all([m1, m2])
    db_session.commit()

    assert m1.id is not None
    assert m2.id is not None
    assert m1.id != m2.id


# ----------------------------------------------------------------------------
# 6. 归档成员后可重新创建
# ----------------------------------------------------------------------------


def test_archive_then_recreate(db_session):
    """【WP4.1】归档旧成员后可重新创建新有效成员。"""
    # 创建有效成员
    member = _make_member(portfolio_id=1, symbol_id=1)
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    member_id_old = member.id

    # 归档（设置 effective_to）
    member.effective_to = datetime.utcnow()
    db_session.commit()

    # 再创建新有效成员应成功
    new_member = _make_member(portfolio_id=1, symbol_id=1)
    db_session.add(new_member)
    db_session.commit()
    db_session.refresh(new_member)

    assert new_member.id is not None
    assert new_member.id != member_id_old
    assert new_member.effective_to is None


# ----------------------------------------------------------------------------
# 7. status 取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,idx",
    [
        (STATUS_ACTIVE, 1),
        (STATUS_PAUSED, 2),
        (STATUS_ARCHIVED, 3),
    ],
)
def test_status_values(db_session, status, idx):
    """【WP4.1】status 支持所有合法取值。"""
    member = _make_member(
        portfolio_id=idx, symbol_id=idx,
        status=status,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)

    assert member.status == status


# ----------------------------------------------------------------------------
# 8. execution_mode 取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,idx",
    [
        (EXECUTION_MANUAL, 1),
        (EXECUTION_CONFIRM, 2),
        (EXECUTION_AUTO, 3),
    ],
)
def test_execution_mode_values(db_session, mode, idx):
    """【WP4.1】execution_mode 支持所有合法取值。"""
    member = _make_member(
        portfolio_id=idx + 100, symbol_id=idx + 100,
        execution_mode=mode,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)

    assert member.execution_mode == mode


# ----------------------------------------------------------------------------
# 9. source_type 取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_type,idx",
    [
        (SOURCE_LEGACY_POSITION, 1),
        (SOURCE_CANDIDATE, 2),
        (SOURCE_OBSERVATION, 3),
        (SOURCE_MANUAL, 4),
    ],
)
def test_source_type_values(db_session, source_type, idx):
    """【WP4.1】source_type 支持所有合法取值。"""
    member = _make_member(
        portfolio_id=idx + 200, symbol_id=idx + 200,
        source_type=source_type,
        source_id=idx,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)

    assert member.source_type == source_type
    assert member.source_id == idx


# ----------------------------------------------------------------------------
# 10. manual_lock
# ----------------------------------------------------------------------------


def test_manual_lock_true(db_session):
    """【WP4.1】manual_lock=True 可写入并读回。"""
    member = _make_member(portfolio_id=1, symbol_id=1, manual_lock=True)
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)

    assert member.manual_lock is True


def test_manual_lock_default_false(db_session):
    """【WP4.1】不传 manual_lock 时默认为 False。"""
    member = _make_member(portfolio_id=1, symbol_id=1)
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)

    assert member.manual_lock is False


# ----------------------------------------------------------------------------
# 11. SQLite schema 补丁幂等
# ----------------------------------------------------------------------------


def test_ensure_sqlite_patch_idempotent_when_table_exists(db_session):
    """【WP4.1】表已存在时多次调用 SQLite 补丁不报错。"""
    engine = db_session.bind
    # 表已由 create_all 创建
    _ensure_sqlite_portfolio_members_table(engine)
    # 第二次调用不应报错（幂等）
    _ensure_sqlite_portfolio_members_table(engine)

    inspector = inspect(engine)
    assert "portfolio_members" in inspector.get_table_names()


def test_ensure_sqlite_patch_creates_table_when_missing():
    """【WP4.1】表不存在时 SQLite 补丁创建表。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_pm_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 先 create_all，再 drop 目标表，模拟"表不存在"场景
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS portfolio_members"))

        inspector = inspect(engine)
        assert "portfolio_members" not in inspector.get_table_names()

        # 调用补丁函数
        _ensure_sqlite_portfolio_members_table(engine)

        # 表应已创建
        inspector = inspect(engine)
        assert "portfolio_members" in inspector.get_table_names()

        # 再次调用（幂等）
        _ensure_sqlite_portfolio_members_table(engine)
        inspector = inspect(engine)
        assert "portfolio_members" in inspector.get_table_names()
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


def test_ensure_sqlite_patch_creates_partial_index_when_missing():
    """【WP4.1】表存在但缺部分唯一索引时，补丁补建索引（幂等）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_pm_idx_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 手动创建表（不含部分唯一索引）模拟"表已存在但缺索引"
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE portfolio_members ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "portfolio_id INTEGER NOT NULL, "
                "symbol_id INTEGER NOT NULL, "
                "status VARCHAR(16) NOT NULL DEFAULT 'active', "
                "execution_mode VARCHAR(16) NOT NULL DEFAULT 'manual', "
                "source_type VARCHAR(32) NOT NULL DEFAULT 'manual', "
                "source_id INTEGER, "
                "entry_rule_version_id INTEGER, "
                "exit_rule_version_id INTEGER, "
                "effective_from DATETIME NOT NULL, "
                "effective_to DATETIME, "
                "manual_lock BOOLEAN NOT NULL DEFAULT 0, "
                "priority INTEGER NOT NULL DEFAULT 0, "
                "note TEXT, "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME)"
            ))

        # 补丁前：索引不存在
        inspector = inspect(engine)
        before_idx = {idx["name"] for idx in inspector.get_indexes("portfolio_members")}
        assert "idx_portfolio_members_active" not in before_idx

        # 调用补丁
        _ensure_sqlite_portfolio_members_table(engine)

        # 补丁后：索引存在
        inspector = inspect(engine)
        after_idx = {idx["name"] for idx in inspector.get_indexes("portfolio_members")}
        assert "idx_portfolio_members_active" in after_idx

        # 再次调用（幂等）
        _ensure_sqlite_portfolio_members_table(engine)
        inspector = inspect(engine)
        after_idx2 = {idx["name"] for idx in inspector.get_indexes("portfolio_members")}
        assert "idx_portfolio_members_active" in after_idx2
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 12. MySQL 补丁函数存在且可调用
# ----------------------------------------------------------------------------


def test_mysql_patch_function_exists_and_callable():
    """【WP4.1】_ensure_mysql_portfolio_members_table 函数存在且可调用。

    不需要真实 MySQL 实例，仅验证函数定义存在且可被调用。
    """
    assert callable(_ensure_mysql_portfolio_members_table)
    import inspect as _inspect
    sig = _inspect.signature(_ensure_mysql_portfolio_members_table)
    params = list(sig.parameters.keys())
    assert len(params) == 1
    assert params[0] == "engine"


# ----------------------------------------------------------------------------
# 13. 索引存在性
# ----------------------------------------------------------------------------


def test_indexes_exist(db_session):
    """【WP4.1】所有声明的索引都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("portfolio_members")
    index_names = {idx["name"] for idx in indexes}

    expected_indexes = {
        "idx_portfolio_members_active",
        "idx_portfolio_members_status",
        "idx_portfolio_members_source",
        "idx_portfolio_members_execution",
    }
    assert expected_indexes.issubset(index_names), (
        f"缺失索引: {expected_indexes - index_names}; 实际: {index_names}"
    )


def test_partial_unique_index_marked_unique(db_session):
    """【WP4.1】idx_portfolio_members_active 为唯一索引。

    SQLite inspector 返回的 unique 字段可能是 1/0 而非 True/False，
    使用真值断言以保持跨版本兼容（与 WP3.1 test_idempotency_key_unique_index_exists
    风格一致）。
    """
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("portfolio_members")
    active_idx = [idx for idx in indexes if idx["name"] == "idx_portfolio_members_active"]
    assert len(active_idx) == 1
    assert active_idx[0].get("unique"), f"索引非唯一: {active_idx[0]}"


def test_column_indexes_exist(db_session):
    """【WP4.1】字段级 index=True 的索引存在（portfolio_id / symbol_id / source_id / created_at）。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("portfolio_members")

    # 收集所有被索引覆盖的列
    indexed_columns = set()
    for idx in indexes:
        for col in idx.get("column_names", []) or []:
            indexed_columns.add(col)

    # 这些列在模型中声明了 index=True
    expected_indexed_columns = {
        "portfolio_id",
        "symbol_id",
        "source_id",
        "created_at",
    }
    assert expected_indexed_columns.issubset(indexed_columns), (
        f"缺失列索引: {expected_indexed_columns - indexed_columns}; "
        f"实际索引列: {indexed_columns}"
    )


# ----------------------------------------------------------------------------
# 14. 查询有效成员（effective_to IS NULL）
# ----------------------------------------------------------------------------


def test_query_active_members(db_session):
    """【WP4.1】查询 effective_to IS NULL 返回所有当前有效成员。"""
    # 创建 3 个成员：2 active + 1 archived，使用不同组合避免部分唯一索引冲突
    active1 = _make_member(portfolio_id=1, symbol_id=1)
    active2 = _make_member(portfolio_id=2, symbol_id=1)
    archived = _make_member(
        portfolio_id=3, symbol_id=1,
        effective_to=datetime.utcnow(),
    )
    db_session.add_all([active1, active2, archived])
    db_session.commit()

    # 查询有效成员（effective_to IS NULL）
    active_members = (
        db_session.query(PortfolioMember)
        .filter(PortfolioMember.effective_to.is_(None))
        .all()
    )
    assert len(active_members) == 2
    active_ids = {m.id for m in active_members}
    assert active1.id in active_ids
    assert active2.id in active_ids
    assert archived.id not in active_ids


def test_query_archived_members(db_session):
    """【WP4.1】查询 effective_to IS NOT NULL 返回所有归档成员。"""
    archived1 = _make_member(
        portfolio_id=1, symbol_id=1,
        effective_to=datetime.utcnow(),
    )
    archived2 = _make_member(
        portfolio_id=2, symbol_id=1,
        effective_to=datetime.utcnow(),
    )
    active = _make_member(portfolio_id=3, symbol_id=1)
    db_session.add_all([archived1, archived2, active])
    db_session.commit()

    archived_members = (
        db_session.query(PortfolioMember)
        .filter(PortfolioMember.effective_to.is_not(None))
        .all()
    )
    assert len(archived_members) == 2
    archived_ids = {m.id for m in archived_members}
    assert archived1.id in archived_ids
    assert archived2.id in archived_ids
    assert active.id not in archived_ids


# ----------------------------------------------------------------------------
# 15. effective_from 默认值
# ----------------------------------------------------------------------------


def test_effective_from_default_set(db_session):
    """【WP4.1】不传 effective_from 时默认值为当前时间（非空）。"""
    before = datetime.utcnow()
    member = _make_member(portfolio_id=1, symbol_id=1)
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    after = datetime.utcnow()

    assert member.effective_from is not None
    # 默认值应在 [before, after] 区间内（允许 1 秒容差以应对 SQLite 存储精度）
    assert before - timedelta(seconds=1) <= member.effective_from <= after + timedelta(seconds=1)


def test_effective_from_explicit_value(db_session):
    """【WP4.1】effective_from 可写入指定值并读回。"""
    ts = datetime(2024, 3, 15, 9, 30, 0)
    member = _make_member(portfolio_id=1, symbol_id=1, effective_from=ts)
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)

    assert member.effective_from == ts
