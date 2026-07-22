"""白盒测试 - WP2.1 WatchlistItem 扩展字段。

覆盖正式观察池扩展字段的 schema、默认值、取值范围、外键行为以及
SQLite/MySQL schema 补丁与 legacy 数据迁移的幂等性。

测试维度：
1. 新字段存在性 + 默认值
2. SQLite schema 补丁幂等（表已含全部列时）
3. SQLite schema 补丁能为旧表追加缺失列
4. score_snapshot_json（WP-P.7）字段读写
5. origin_type 各取值
6. status 各取值
7. tags_json 读写
8. target_portfolio_id 外键关联 + ondelete=SET NULL
9. legacy 数据迁移幂等
10. MySQL 补丁函数存在且可调用
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.init_db import (
    _ensure_mysql_watchlist_item_columns,
    _ensure_sqlite_watchlist_item_columns,
    _migrate_legacy_watchlist_items_origin_type,
)
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_watchlist(db_session, name: str = "QA-WP21-WL") -> Watchlist:
    wl = Watchlist(name=name, list_type="custom", description="qa")
    db_session.add(wl)
    db_session.commit()
    db_session.refresh(wl)
    return wl


def _make_symbol(db_session, symbol: str = "600000") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type="stock",
        market="cn",
        theme="测试",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_portfolio(db_session, name: str = "QA-WP21-PF") -> Portfolio:
    pf = Portfolio(
        name=name,
        account_type="cash",
        total_capital=100000.0,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
    )
    db_session.add(pf)
    db_session.commit()
    db_session.refresh(pf)
    return pf


# ----------------------------------------------------------------------------
# 1. 新字段存在性 + 默认值
# ----------------------------------------------------------------------------


def test_watchlist_item_new_fields_defaults(db_session):
    """【WP2.1】新字段可读写，默认值正确。"""
    wl = _make_watchlist(db_session, name="QA-Defaults")
    sym = _make_symbol(db_session, symbol="600001")

    item = WatchlistItem(watchlist_id=wl.id, symbol_id=sym.id)
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    # WP2.1 新字段默认值
    assert item.origin_type == "manual"
    assert item.origin_id is None
    assert item.reason_json is None
    assert item.status == "watching"
    assert item.priority == 0
    assert item.tags_json is None
    assert item.target_portfolio_id is None
    assert item.updated_at is None
    assert item.archived_at is None


def test_watchlist_item_fields_writable(db_session):
    """【WP2.1】新字段可写入指定值并读回。"""
    wl = _make_watchlist(db_session, name="QA-Writable")
    sym = _make_symbol(db_session, symbol="600002")
    pf = _make_portfolio(db_session, name="QA-PF-Writable")

    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type="candidate",
        origin_id=42,
        reason_json='{"action":"buy"}',
        status="ready",
        priority=5,
        tags_json='["科技","龙头"]',
        target_portfolio_id=pf.id,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    assert item.origin_type == "candidate"
    assert item.origin_id == 42
    assert json.loads(item.reason_json) == {"action": "buy"}
    assert item.status == "ready"
    assert item.priority == 5
    assert json.loads(item.tags_json) == ["科技", "龙头"]
    assert item.target_portfolio_id == pf.id


# ----------------------------------------------------------------------------
# 2. SQLite schema 补丁幂等
# ----------------------------------------------------------------------------


def test_ensure_sqlite_watchlist_item_columns_idempotent(db_session):
    """【WP2.1】SQLite schema 补丁多次调用不报错（表已含全部列时）。"""
    engine = db_session.bind
    # 表已由 create_all 创建，所有列已存在
    _ensure_sqlite_watchlist_item_columns(engine)
    # 第二次调用不应报错（幂等）
    _ensure_sqlite_watchlist_item_columns(engine)

    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("watchlist_items")}
    expected = {
        "origin_type",
        "origin_id",
        "reason_json",
        "status",
        "priority",
        "tags_json",
        "target_portfolio_id",
        "updated_at",
        "archived_at",
    }
    assert expected.issubset(columns)


def test_ensure_sqlite_watchlist_item_columns_adds_missing_columns():
    """【WP2.1】SQLite schema 补丁能为旧表（仅含原始列）追加新列。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_schema_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 创建仅含原始列的旧表（模拟 WP2.1 前的库结构）
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE watchlists ("
                "id INTEGER PRIMARY KEY, "
                "name VARCHAR(128) UNIQUE, "
                "list_type VARCHAR(32), "
                "description TEXT, "
                "created_at DATETIME, "
                "updated_at DATETIME)"
            ))
            conn.execute(text(
                "CREATE TABLE watchlist_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "watchlist_id INTEGER, "
                "symbol_id INTEGER, "
                "note TEXT, "
                "score_snapshot_json TEXT, "
                "added_at DATETIME, "
                "UNIQUE(watchlist_id, symbol_id))"
            ))

        # 运行补丁前：确认新列不存在
        inspector = inspect(engine)
        before_cols = {col["name"] for col in inspector.get_columns("watchlist_items")}
        assert "origin_type" not in before_cols
        assert "status" not in before_cols

        # 运行补丁
        _ensure_sqlite_watchlist_item_columns(engine)

        # 运行补丁后：新列已添加
        inspector = inspect(engine)
        after_cols = {col["name"] for col in inspector.get_columns("watchlist_items")}
        expected_new = {
            "origin_type",
            "origin_id",
            "reason_json",
            "status",
            "priority",
            "tags_json",
            "target_portfolio_id",
            "updated_at",
            "archived_at",
        }
        assert expected_new.issubset(after_cols)

        # 再次运行（幂等，不报错）
        _ensure_sqlite_watchlist_item_columns(engine)
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 3. score_snapshot_json 字段（WP-P.7 已存在）
# ----------------------------------------------------------------------------


def test_score_snapshot_json_field_exists_and_writable(db_session):
    """【WP-P.7】score_snapshot_json 字段存在且可读写 JSON 字符串。"""
    wl = _make_watchlist(db_session, name="QA-Snapshot")
    sym = _make_symbol(db_session, symbol="600003")

    snapshot = json.dumps({
        "quality_score": 78.0,
        "timing_score": 70.0,
        "priority_score": 82.5,
        "source": "discovery_candidate",
        "candidate_id": 99,
    })
    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        score_snapshot_json=snapshot,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    assert item.score_snapshot_json is not None
    payload = json.loads(item.score_snapshot_json)
    assert payload["quality_score"] == 78.0
    assert payload["source"] == "discovery_candidate"
    assert payload["candidate_id"] == 99


# ----------------------------------------------------------------------------
# 4. origin_type 取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin_type,idx",
    [
        ("manual", 1),
        ("candidate", 2),
        ("scan_result", 3),
        ("alert", 4),
        ("legacy_manual_unknown", 5),
    ],
)
def test_origin_type_values(db_session, origin_type, idx):
    """【WP2.1】origin_type 支持所有合法取值。"""
    wl = _make_watchlist(db_session, name=f"QA-OT-{idx}")
    sym = _make_symbol(db_session, symbol=f"600{idx:03d}")

    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type=origin_type,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    assert item.origin_type == origin_type


# ----------------------------------------------------------------------------
# 5. status 取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,idx",
    [
        ("watching", 1),
        ("ready", 2),
        ("invalid", 3),
        ("archived", 4),
    ],
)
def test_status_values(db_session, status, idx):
    """【WP2.1】status 支持所有合法取值。"""
    wl = _make_watchlist(db_session, name=f"QA-ST-{idx}")
    sym = _make_symbol(db_session, symbol=f"601{idx:03d}")

    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        status=status,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    assert item.status == status


# ----------------------------------------------------------------------------
# 6. tags_json 读写
# ----------------------------------------------------------------------------


def test_tags_json_read_write(db_session):
    """【WP2.1】tags_json 可写入 JSON 数组并读回解析为 list。"""
    wl = _make_watchlist(db_session, name="QA-Tags")
    sym = _make_symbol(db_session, symbol="600004")

    tags = ["科技", "龙头", "高股息"]
    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        tags_json=json.dumps(tags, ensure_ascii=False),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    parsed = json.loads(item.tags_json)
    assert parsed == tags
    assert isinstance(parsed, list)


# ----------------------------------------------------------------------------
# 7. target_portfolio_id 外键
# ----------------------------------------------------------------------------


def test_target_portfolio_id_foreign_key_association(db_session):
    """【WP2.1】target_portfolio_id 可关联到 Portfolio 并查询。"""
    wl = _make_watchlist(db_session, name="QA-FK")
    sym = _make_symbol(db_session, symbol="600005")
    pf = _make_portfolio(db_session, name="QA-FK-PF")

    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        target_portfolio_id=pf.id,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    assert item.target_portfolio_id == pf.id

    # 通过 Portfolio 反查关联的 WatchlistItem（手工查询，未定义 relationship）
    related = (
        db_session.query(WatchlistItem)
        .filter_by(target_portfolio_id=pf.id)
        .all()
    )
    assert len(related) == 1
    assert related[0].id == item.id


def test_target_portfolio_id_fk_constraint_defined(db_session):
    """【WP2.1】target_portfolio_id 外键约束指向 portfolios 表。"""
    engine = db_session.bind
    inspector = inspect(engine)
    fks = inspector.get_foreign_keys("watchlist_items")
    pf_fks = [fk for fk in fks if fk["referred_table"] == "portfolios"]
    assert len(pf_fks) == 1
    assert pf_fks[0]["constrained_columns"] == ["target_portfolio_id"]


def test_target_portfolio_id_ondelete_set_null(tmp_sqlite_url):
    """【WP2.1】删除 Portfolio 后 WatchlistItem.target_portfolio_id 置 NULL（ondelete=SET NULL）。

    SQLite 默认不启用外键约束，需通过 event listener 在每个新连接上
    显式 PRAGMA foreign_keys=ON 才能触发 ON DELETE SET NULL 行为。
    """
    engine = create_engine(tmp_sqlite_url)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        wl = Watchlist(name="QA-DEL-PF", list_type="custom")
        sym = Symbol(
            symbol="600006", name="测试-600006",
            asset_type="stock", market="cn", theme="测试",
        )
        pf = Portfolio(
            name="QA-DEL-PF-Target",
            account_type="cash",
            total_capital=100000.0,
            investable_ratio=0.8,
            cash_reserve_ratio=0.2,
        )
        session.add_all([wl, sym, pf])
        session.commit()

        item = WatchlistItem(
            watchlist_id=wl.id,
            symbol_id=sym.id,
            target_portfolio_id=pf.id,
        )
        session.add(item)
        session.commit()
        item_id = item.id

        # 删除关联的 Portfolio，触发 ON DELETE SET NULL
        session.delete(pf)
        session.commit()

        # 刷新后 target_portfolio_id 应为 NULL，WatchlistItem 仍存在
        session.expire_all()
        refreshed = session.get(WatchlistItem, item_id)
        assert refreshed is not None
        assert refreshed.target_portfolio_id is None
    finally:
        session.close()
        engine.dispose()


# ----------------------------------------------------------------------------
# 8. legacy 数据迁移
# ----------------------------------------------------------------------------


def test_migrate_legacy_watchlist_items_origin_type(db_session):
    """【WP2.1】legacy 迁移将 origin_type='manual' 且无 origin_id 的记录改为 legacy_manual_unknown。"""
    engine = db_session.bind
    wl = _make_watchlist(db_session, name="QA-Legacy")
    sym1 = _make_symbol(db_session, symbol="600010")
    sym2 = _make_symbol(db_session, symbol="600011")
    sym3 = _make_symbol(db_session, symbol="600012")

    # 3 条旧记录（origin_type 默认 manual，origin_id 为 None）
    for sym in [sym1, sym2, sym3]:
        db_session.add(WatchlistItem(watchlist_id=wl.id, symbol_id=sym.id))
    db_session.commit()

    # 调用迁移前：确认 origin_type='manual'
    items_before = (
        db_session.query(WatchlistItem)
        .filter_by(watchlist_id=wl.id)
        .all()
    )
    assert len(items_before) == 3
    for item in items_before:
        assert item.origin_type == "manual"

    # 调用迁移
    _migrate_legacy_watchlist_items_origin_type(engine)

    # 调用迁移后：origin_type='legacy_manual_unknown'
    db_session.expire_all()
    items_after = (
        db_session.query(WatchlistItem)
        .filter_by(watchlist_id=wl.id)
        .all()
    )
    assert len(items_after) == 3
    for item in items_after:
        assert item.origin_type == "legacy_manual_unknown"


def test_migrate_legacy_watchlist_items_idempotent(db_session):
    """【WP2.1】legacy 迁移幂等：第二次调用不修改任何记录。"""
    engine = db_session.bind
    wl = _make_watchlist(db_session, name="QA-Legacy-Idem")
    sym = _make_symbol(db_session, symbol="600013")

    db_session.add(WatchlistItem(watchlist_id=wl.id, symbol_id=sym.id))
    db_session.commit()

    # 第一次迁移：manual → legacy_manual_unknown
    _migrate_legacy_watchlist_items_origin_type(engine)
    db_session.expire_all()
    item = db_session.query(WatchlistItem).filter_by(watchlist_id=wl.id).one()
    assert item.origin_type == "legacy_manual_unknown"

    # 第二次迁移（幂等，不应修改）
    _migrate_legacy_watchlist_items_origin_type(engine)
    db_session.expire_all()
    item = db_session.query(WatchlistItem).filter_by(watchlist_id=wl.id).one()
    assert item.origin_type == "legacy_manual_unknown"


def test_migrate_legacy_does_not_affect_explicit_manual_with_origin_id(db_session):
    """【WP2.1】legacy 迁移不影响显式 manual + origin_id 的记录（WP2.2+ 创建的记录）。"""
    engine = db_session.bind
    wl = _make_watchlist(db_session, name="QA-NoTouch")
    sym = _make_symbol(db_session, symbol="600014")

    # 显式 manual + origin_id（模拟 WP2.2+ 通过 observations 服务创建的记录）
    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type="manual",
        origin_id=999,
    )
    db_session.add(item)
    db_session.commit()

    _migrate_legacy_watchlist_items_origin_type(engine)
    db_session.expire_all()
    refreshed = db_session.query(WatchlistItem).filter_by(watchlist_id=wl.id).one()
    assert refreshed.origin_type == "manual"
    assert refreshed.origin_id == 999


def test_migrate_legacy_skips_non_manual_records(db_session):
    """【WP2.1】legacy 迁移不修改非 manual 来源的记录。"""
    engine = db_session.bind
    wl = _make_watchlist(db_session, name="QA-Skip")
    sym = _make_symbol(db_session, symbol="600015")

    # candidate 来源的记录不应被迁移
    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type="candidate",
        origin_id=42,
    )
    db_session.add(item)
    db_session.commit()

    _migrate_legacy_watchlist_items_origin_type(engine)
    db_session.expire_all()
    refreshed = db_session.query(WatchlistItem).filter_by(watchlist_id=wl.id).one()
    assert refreshed.origin_type == "candidate"


def test_migrate_legacy_no_table_does_not_error():
    """【WP2.1】legacy 迁移在 watchlist_items 表不存在时不报错。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_empty_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 不创建任何表，直接调用迁移函数
        _migrate_legacy_watchlist_items_origin_type(engine)
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 9. MySQL 补丁函数存在
# ----------------------------------------------------------------------------


def test_mysql_patch_function_exists_and_callable():
    """【WP2.1】_ensure_mysql_watchlist_item_columns 函数存在且可调用。

    不需要真实 MySQL 实例，仅验证函数定义存在且可被调用。
    注意：函数内部使用 MySQL 专有的 `SELECT DATABASE()` 与
    `information_schema.COLUMNS/STATISTICS`，无法在 SQLite 上执行，
    故只验证函数签名而不实际调用。
    """
    assert callable(_ensure_mysql_watchlist_item_columns)
    # 验证函数签名接受 engine 参数
    import inspect as _inspect
    sig = _inspect.signature(_ensure_mysql_watchlist_item_columns)
    params = list(sig.parameters.keys())
    assert len(params) == 1
    assert params[0] == "engine"
