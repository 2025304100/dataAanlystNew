"""白盒测试 - WP2.6 现有 core 名单保留。

覆盖 spec Scenario "现有 core 名单保留"：
- core 观察池及 6 个观察项原样保留
- 历史无来源项标记 legacy/manual_unknown，禁止伪造来源

测试维度：
1. core 名单存在性（_ensure_core_watchlist_exists 创建）
2. core 名单幂等创建（多次调用只创建一个）
3. legacy 迁移不影响显式 origin_type（candidate 不被改）
4. legacy 迁移只影响 manual 无 origin_id（manual+origin_id 不被改）
5. 6 个观察项保留（_ensure_core_watchlist_seed_items 创建 6 项，origin_type=legacy_manual_unknown）
6. core 名单观察项幂等保留（多次调用不产生重复）
7. core 名单观察项不被分层清理删除（WP-P.7 cleanup 不影响 watchlist_items）
8. 禁止伪造来源（富读接口返回 legacy_manual_unknown，不伪造为 candidate 等）
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.init_db import (
    _ensure_core_watchlist_exists,
    _ensure_core_watchlist_seed_items,
    _migrate_legacy_watchlist_items_origin_type,
)
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services import observations as obs_svc
from app.services.discovery_retention import cleanup_expired_discovery_results_layered


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


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


def _make_symbols(db_session, count: int = 6) -> list[Symbol]:
    """创建 count 个 symbols。"""
    syms = []
    for i in range(count):
        sym = _make_symbol(db_session, symbol=f"600{i:03d}")
        syms.append(sym)
    return syms


def _get_core_watchlist_id(db_session) -> int | None:
    """查询 core 名单 ID。"""
    return db_session.execute(
        text("SELECT id FROM watchlists WHERE name = 'core' LIMIT 1")
    ).scalar()


def _count_core_items(db_session) -> int:
    """查询 core 名单的观察项数量。"""
    return db_session.execute(
        text(
            "SELECT COUNT(*) FROM watchlist_items wi "
            "JOIN watchlists w ON wi.watchlist_id = w.id "
            "WHERE w.name = 'core'"
        )
    ).scalar() or 0


# ----------------------------------------------------------------------------
# 1. core 名单存在性
# ----------------------------------------------------------------------------


def test_ensure_core_watchlist_exists_creates_watchlist(db_session):
    """【WP2.6】_ensure_core_watchlist_exists 创建 core 名单。"""
    engine = db_session.bind

    # 调用前：确认 core 名单不存在
    assert _get_core_watchlist_id(db_session) is None

    # 调用函数
    _ensure_core_watchlist_exists(engine)

    # 调用后：core 名单存在
    db_session.expire_all()
    row = db_session.execute(
        text("SELECT name, list_type, description FROM watchlists WHERE name = 'core'")
    ).first()
    assert row is not None
    assert row.name == "core"
    assert row.list_type == "observation"
    assert row.description is not None


# ----------------------------------------------------------------------------
# 2. core 名单幂等创建
# ----------------------------------------------------------------------------


def test_ensure_core_watchlist_exists_idempotent(db_session):
    """【WP2.6】多次调用 _ensure_core_watchlist_exists 只创建一个 core 名单。"""
    engine = db_session.bind

    # 多次调用
    _ensure_core_watchlist_exists(engine)
    _ensure_core_watchlist_exists(engine)
    _ensure_core_watchlist_exists(engine)

    # 验证：core 名单只有一个
    db_session.expire_all()
    count = db_session.execute(
        text("SELECT COUNT(*) FROM watchlists WHERE name = 'core'")
    ).scalar()
    assert count == 1


# ----------------------------------------------------------------------------
# 3. legacy 迁移不影响显式 origin_type
# ----------------------------------------------------------------------------


def test_migrate_legacy_does_not_affect_candidate_origin(db_session):
    """【WP2.6】legacy 迁移不影响 origin_type='candidate' 的记录（WP2.2+ 创建的）。"""
    engine = db_session.bind

    # 创建 core 名单 + candidate 来源的观察项
    _ensure_core_watchlist_exists(engine)
    db_session.expire_all()
    core_id = _get_core_watchlist_id(db_session)

    sym = _make_symbol(db_session, symbol="600100")
    item = WatchlistItem(
        watchlist_id=core_id,
        symbol_id=sym.id,
        origin_type="candidate",
        origin_id=42,
    )
    db_session.add(item)
    db_session.commit()

    # 调用迁移
    _migrate_legacy_watchlist_items_origin_type(engine)

    # 验证：origin_type 仍为 'candidate'，未被伪造为其他来源
    db_session.expire_all()
    refreshed = db_session.query(WatchlistItem).filter_by(id=item.id).one()
    assert refreshed.origin_type == "candidate"
    assert refreshed.origin_id == 42


# ----------------------------------------------------------------------------
# 4. legacy 迁移只影响 manual 无 origin_id
# ----------------------------------------------------------------------------


def test_migrate_legacy_only_affects_manual_without_origin_id(db_session):
    """【WP2.6】legacy 迁移只影响 manual+origin_id=NULL 的记录。

    显式 manual+origin_id 有值的记录（WP2.2+ 通过 observations 服务创建）
    不被迁移，保持 manual 来源不变。
    """
    engine = db_session.bind

    _ensure_core_watchlist_exists(engine)
    db_session.expire_all()
    core_id = _get_core_watchlist_id(db_session)

    # 旧记录：manual + origin_id=None（应被迁移为 legacy_manual_unknown）
    sym1 = _make_symbol(db_session, symbol="600200")
    item_old = WatchlistItem(
        watchlist_id=core_id,
        symbol_id=sym1.id,
        origin_type="manual",
        origin_id=None,
    )
    db_session.add(item_old)

    # 显式 manual + origin_id=123（不应被迁移，WP2.2+ 创建的记录）
    sym2 = _make_symbol(db_session, symbol="600201")
    item_explicit = WatchlistItem(
        watchlist_id=core_id,
        symbol_id=sym2.id,
        origin_type="manual",
        origin_id=123,
    )
    db_session.add(item_explicit)
    db_session.commit()

    # 调用迁移
    _migrate_legacy_watchlist_items_origin_type(engine)

    # 验证
    db_session.expire_all()
    old = db_session.query(WatchlistItem).filter_by(id=item_old.id).one()
    explicit = db_session.query(WatchlistItem).filter_by(id=item_explicit.id).one()

    # 旧记录被标记为 legacy_manual_unknown
    assert old.origin_type == "legacy_manual_unknown"
    assert old.origin_id is None

    # 显式 manual 来源记录保持不变
    assert explicit.origin_type == "manual"
    assert explicit.origin_id == 123


# ----------------------------------------------------------------------------
# 5. 6 个观察项保留
# ----------------------------------------------------------------------------


def test_ensure_core_watchlist_seed_items_creates_six(db_session):
    """【WP2.6】_ensure_core_watchlist_seed_items 创建 6 个种子观察项。

    spec Scenario "现有 core 名单保留" 要求：6 个观察项原样保留。
    种子项 origin_type='legacy_manual_unknown'（禁止伪造来源）。
    """
    engine = db_session.bind

    # 先创建 core 名单
    _ensure_core_watchlist_exists(engine)

    # 创建 6 个 symbols（种子项来源）
    _make_symbols(db_session, count=6)

    # 调用 seed_items
    _ensure_core_watchlist_seed_items(engine)

    # 验证：core 名单有 6 个观察项
    db_session.expire_all()
    assert _count_core_items(db_session) == 6

    # 验证：所有种子项 origin_type='legacy_manual_unknown'（不伪造来源）
    items = db_session.execute(
        text(
            "SELECT wi.origin_type FROM watchlist_items wi "
            "JOIN watchlists w ON wi.watchlist_id = w.id "
            "WHERE w.name = 'core'"
        )
    ).fetchall()
    for item in items:
        assert item[0] == "legacy_manual_unknown"


def test_ensure_core_watchlist_seed_items_idempotent(db_session):
    """【WP2.6】多次调用 _ensure_core_watchlist_seed_items 不产生重复项。"""
    engine = db_session.bind

    _ensure_core_watchlist_exists(engine)
    _make_symbols(db_session, count=6)

    # 第一次调用
    _ensure_core_watchlist_seed_items(engine)
    # 第二次调用（幂等）
    _ensure_core_watchlist_seed_items(engine)
    # 第三次调用（幂等）
    _ensure_core_watchlist_seed_items(engine)

    # 验证：仍只有 6 项，不产生重复
    db_session.expire_all()
    assert _count_core_items(db_session) == 6


# ----------------------------------------------------------------------------
# 6. core 名单观察项不被清理删除
# ----------------------------------------------------------------------------


def test_core_watchlist_items_not_deleted_by_cleanup(db_session):
    """【WP2.6】WP-P.7 分层清理不删除 core 名单的观察项。

    分层清理服务只清理 scan_results / discovery_candidates / scan_runs /
    snapshot_items，不影响 watchlist_items。
    """
    engine = db_session.bind

    _ensure_core_watchlist_exists(engine)
    _make_symbols(db_session, count=6)
    _ensure_core_watchlist_seed_items(engine)

    # 调用前：确认有 6 项
    db_session.expire_all()
    before = _count_core_items(db_session)
    assert before == 6

    # 调用 WP-P.7 分层清理
    report = cleanup_expired_discovery_results_layered(db_session)

    # 调用后：core 名单观察项不被删除
    db_session.expire_all()
    after = _count_core_items(db_session)
    assert after == 6

    # 验证：清理报告不包含 watchlist_items 删除
    report_dict = report.to_dict()
    assert "deleted_watchlist_items" not in report_dict


# ----------------------------------------------------------------------------
# 7. 禁止伪造来源
# ----------------------------------------------------------------------------


def test_legacy_manual_unknown_not_faked_in_rich_read(db_session):
    """【WP2.6】legacy_manual_unknown 记录在富读接口中保持原值，不伪造为 candidate 等来源。

    spec Scenario "现有 core 名单保留" 要求：历史无来源项标记为
    legacy/manual_unknown，不伪造来源。
    """
    engine = db_session.bind

    _ensure_core_watchlist_exists(engine)
    db_session.expire_all()
    core_id = _get_core_watchlist_id(db_session)

    sym = _make_symbol(db_session, symbol="600300")
    item = WatchlistItem(
        watchlist_id=core_id,
        symbol_id=sym.id,
        origin_type="legacy_manual_unknown",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    # 富读
    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)

    # 禁止伪造：origin_type 必须是 legacy_manual_unknown
    assert rich.origin_type == "legacy_manual_unknown"
    # 不能被伪造为其他来源
    assert rich.origin_type != "candidate"
    assert rich.origin_type != "scan_result"
    assert rich.origin_type != "alert"
    assert rich.origin_type != "manual"

    # 验证序列化也保持原值
    data = rich.to_dict()
    assert data["origin_type"] == "legacy_manual_unknown"


# ----------------------------------------------------------------------------
# 8. 集成：完整初始化流程保留 core 名单
# ----------------------------------------------------------------------------


def test_full_init_flow_preserves_core_watchlist(db_session):
    """【WP2.6】完整初始化流程：创建 core 名单 → 添加种子项 → 迁移 → 不破坏。

    模拟 init_db() 中的调用顺序：
    1. _ensure_core_watchlist_exists
    2. _ensure_core_watchlist_seed_items
    3. _migrate_legacy_watchlist_items_origin_type（再次调用，幂等）
    """
    engine = db_session.bind

    # 准备：创建 6 个 symbols
    _make_symbols(db_session, count=6)

    # 步骤 1：创建 core 名单
    _ensure_core_watchlist_exists(engine)

    # 步骤 2：添加 6 个种子观察项
    _ensure_core_watchlist_seed_items(engine)

    # 步骤 3：再次调用迁移（模拟重启后再次初始化）
    _migrate_legacy_watchlist_items_origin_type(engine)

    # 再次调用 ensure 函数（模拟重启后再次初始化）
    _ensure_core_watchlist_exists(engine)
    _ensure_core_watchlist_seed_items(engine)

    # 验证：core 名单仍存在
    db_session.expire_all()
    core_id = _get_core_watchlist_id(db_session)
    assert core_id is not None

    # 验证：core 名单仍只有 6 项（不重复创建）
    assert _count_core_items(db_session) == 6

    # 验证：所有项 origin_type='legacy_manual_unknown'
    items = db_session.execute(
        text(
            "SELECT wi.origin_type FROM watchlist_items wi "
            "JOIN watchlists w ON wi.watchlist_id = w.id "
            "WHERE w.name = 'core'"
        )
    ).fetchall()
    assert len(items) == 6
    for item in items:
        assert item[0] == "legacy_manual_unknown"
