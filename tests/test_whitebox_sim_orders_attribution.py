"""白盒测试 - WP6.1 SimOrder 归因字段扩展。

覆盖 SimOrder 模型新增归因字段的 schema、默认值、外键（member_id SET NULL）、
唯一索引（client_order_key）、JSON 快照存储以及 SQLite/MySQL schema 补丁幂等性。

测试维度：
1. 新字段存在性
2. 现有字段不变（向后兼容）
3. 新字段可读写
4. member_id 外键约束
5. member_id SET NULL on delete
6. client_order_key 唯一索引
7. client_order_key NULL 允许多条
8. source_type/source_id 索引
9. signal_id 索引
10. signal_snapshot_json 存储
11. decision_snapshot_json 存储
12. rejection_code/rejection_detail 存储
13. execution_mode 取值（manual/confirm/auto）
14. schema 补丁幂等
15. MySQL 补丁函数存在
16. 索引存在性
17. 历史订单兼容
"""
from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db.init_db import (
    _ensure_mysql_sim_orders_attribution_columns,
    _ensure_sqlite_sim_orders_attribution_columns,
    init_db,
)
from app.models.portfolio_member import PortfolioMember
from app.models.portfolio import Portfolio
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_portfolio(db_session, name: str = "QA-SimOrder") -> Portfolio:
    """创建一个组合（SimOrder 外键依赖）。"""
    p = Portfolio(
        name=name,
        account_type="simulated",
        total_capital=100000.0,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _make_symbol(db_session, code: str = "600010") -> Symbol:
    """创建一个标的（SimOrder 外键依赖）。"""
    sym = Symbol(symbol=code, name=f"测试-{code}", asset_type="stock", market="sh")
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_member(db_session, portfolio_id: int, symbol_id: int) -> PortfolioMember:
    """创建一个 PortfolioMember（member_id 外键依赖）。"""
    m = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
    )
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


def _make_order(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    side: str = "buy",
    quantity: float = 100.0,
    submitted_price: float = 10.0,
    member_id: int | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    signal_id: int | None = None,
    signal_snapshot_json: str | None = None,
    rule_version_id: int | None = None,
    execution_mode: str | None = None,
    client_order_key: str | None = None,
    decision_snapshot_json: str | None = None,
    rejection_code: str | None = None,
    rejection_detail: str | None = None,
) -> SimOrder:
    """构造一个 SimOrder（未传字段使用模型默认值）。"""
    kwargs: dict = {
        "portfolio_id": portfolio_id,
        "symbol_id": symbol_id,
        "side": side,
        "quantity": quantity,
        "submitted_price": submitted_price,
    }
    if member_id is not None:
        kwargs["member_id"] = member_id
    if source_type is not None:
        kwargs["source_type"] = source_type
    if source_id is not None:
        kwargs["source_id"] = source_id
    if signal_id is not None:
        kwargs["signal_id"] = signal_id
    if signal_snapshot_json is not None:
        kwargs["signal_snapshot_json"] = signal_snapshot_json
    if rule_version_id is not None:
        kwargs["rule_version_id"] = rule_version_id
    if execution_mode is not None:
        kwargs["execution_mode"] = execution_mode
    if client_order_key is not None:
        kwargs["client_order_key"] = client_order_key
    if decision_snapshot_json is not None:
        kwargs["decision_snapshot_json"] = decision_snapshot_json
    if rejection_code is not None:
        kwargs["rejection_code"] = rejection_code
    if rejection_detail is not None:
        kwargs["rejection_detail"] = rejection_detail
    order = SimOrder(**kwargs)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


# ----------------------------------------------------------------------------
# 1. 新字段存在性
# ----------------------------------------------------------------------------


def test_attribution_columns_present_after_init_db(tmp_sqlite_url):
    """【WP6.1】init_db() 后 sim_orders 表存在所有归因字段。"""
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
        columns = {col["name"] for col in inspector.get_columns("sim_orders")}
        expected = {
            "member_id",
            "source_type",
            "source_id",
            "signal_id",
            "signal_snapshot_json",
            "rule_version_id",
            "execution_mode",
            "client_order_key",
            "decision_snapshot_json",
            "rejection_code",
            "rejection_detail",
        }
        assert expected.issubset(columns), f"缺失字段: {expected - columns}"
    finally:
        try:
            mgr.dispose()
        except Exception:
            pass


def test_attribution_columns_present_after_metadata_create_all(db_session):
    """【WP6.1】Base.metadata.create_all 后 sim_orders 表存在所有归因字段。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("sim_orders")}
    expected = {
        "member_id",
        "source_type",
        "source_id",
        "signal_id",
        "signal_snapshot_json",
        "rule_version_id",
        "execution_mode",
        "client_order_key",
        "decision_snapshot_json",
        "rejection_code",
        "rejection_detail",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


# ----------------------------------------------------------------------------
# 2. 现有字段不变（向后兼容）
# ----------------------------------------------------------------------------


def test_legacy_fields_unchanged(db_session):
    """【WP6.1】不传任何新字段时，现有字段保持原值，新字段为 NULL。"""
    p = _make_portfolio(db_session, name="QA-Legacy-1")
    sym = _make_symbol(db_session, code="600001")

    order = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        side="buy",
        quantity=200.0,
        submitted_price=12.5,
    )

    # 现有字段
    assert order.id is not None
    assert order.portfolio_id == p.id
    assert order.symbol_id == sym.id
    assert order.side == "buy"
    assert order.order_type == "market"
    assert order.quantity == 200.0
    assert order.submitted_price == 12.5
    assert order.status == "filled"
    assert order.filled_quantity == 0
    assert order.filled_price == 0
    assert order.filled_amount == 0
    assert order.fee == 0
    assert order.note is None
    assert order.created_at is not None
    assert order.filled_at is None

    # 新字段全部为 NULL（向后兼容）
    assert order.member_id is None
    assert order.source_type is None
    assert order.source_id is None
    assert order.signal_id is None
    assert order.signal_snapshot_json is None
    assert order.rule_version_id is None
    assert order.execution_mode is None
    assert order.client_order_key is None
    assert order.decision_snapshot_json is None
    assert order.rejection_code is None
    assert order.rejection_detail is None


# ----------------------------------------------------------------------------
# 3. 新字段可读写
# ----------------------------------------------------------------------------


def test_all_attribution_fields_writable(db_session):
    """【WP6.1】所有归因字段可写入指定值并读回。"""
    p = _make_portfolio(db_session, name="QA-Write-1")
    sym = _make_symbol(db_session, code="600002")
    member = _make_member(db_session, portfolio_id=p.id, symbol_id=sym.id)

    order = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        member_id=member.id,
        source_type="member",
        source_id=member.id,
        signal_id=42,
        signal_snapshot_json='{"action":"open","score":0.8}',
        rule_version_id=1001,
        execution_mode="auto",
        client_order_key="pf-1-m-1-2026-07-21-buy-v1",
        decision_snapshot_json='{"reason":"auto buy","data_freshness":"2026-07-21"}',
        rejection_code="DATA_EXPIRED",
        rejection_detail="K线数据过期",
    )

    assert order.id is not None
    assert order.member_id == member.id
    assert order.source_type == "member"
    assert order.source_id == member.id
    assert order.signal_id == 42
    assert order.signal_snapshot_json == '{"action":"open","score":0.8}'
    assert order.rule_version_id == 1001
    assert order.execution_mode == "auto"
    assert order.client_order_key == "pf-1-m-1-2026-07-21-buy-v1"
    assert order.decision_snapshot_json == '{"reason":"auto buy","data_freshness":"2026-07-21"}'
    assert order.rejection_code == "DATA_EXPIRED"
    assert order.rejection_detail == "K线数据过期"


# ----------------------------------------------------------------------------
# 4. member_id 外键
# ----------------------------------------------------------------------------


def test_member_id_foreign_key_accepts_valid(db_session):
    """【WP6.1】member_id 指向存在的 PortfolioMember.id 时可成功写入。"""
    p = _make_portfolio(db_session, name="QA-FK-1")
    sym = _make_symbol(db_session, code="600003")
    member = _make_member(db_session, portfolio_id=p.id, symbol_id=sym.id)

    order = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        member_id=member.id,
    )

    assert order.member_id == member.id


# ----------------------------------------------------------------------------
# 5. member_id SET NULL on delete
# ----------------------------------------------------------------------------


def test_member_id_set_null_on_delete(tmp_sqlite_url):
    """【WP6.1】删除 PortfolioMember 后，关联 SimOrder 仍存在且 member_id=NULL。

    SQLite 默认不启用 FK 约束，需通过 PRAGMA foreign_keys=ON 启用。
    """
    from app.db.manager import DatabaseManager

    mgr = DatabaseManager.get()
    try:
        mgr.dispose()
    except Exception:
        pass
    mgr.initialize(tmp_sqlite_url, db_type="sqlite")
    engine = mgr.engine

    # 启用 SQLite 外键约束（PRAGMA foreign_keys=ON）
    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _connection_record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    try:
        Base.metadata.create_all(engine)
        SessionLocal = mgr.session_factory

        with SessionLocal() as session:
            p = Portfolio(
                name="QA-SetNull-1",
                account_type="simulated",
                total_capital=100000.0,
                investable_ratio=0.9,
                cash_reserve_ratio=0.1,
                currency="CNY",
                is_default=0,
            )
            session.add(p)
            session.commit()
            session.refresh(p)
            portfolio_id = p.id

            sym = Symbol(symbol="600004", name="测试-600004", asset_type="stock", market="sh")
            session.add(sym)
            session.commit()
            session.refresh(sym)

            member = PortfolioMember(portfolio_id=p.id, symbol_id=sym.id)
            session.add(member)
            session.commit()
            session.refresh(member)
            member_id = member.id

            order = SimOrder(
                portfolio_id=p.id,
                symbol_id=sym.id,
                side="buy",
                quantity=100.0,
                submitted_price=10.0,
                member_id=member.id,
            )
            session.add(order)
            session.commit()
            session.refresh(order)
            order_id = order.id

        # 删除 PortfolioMember（应触发 SET NULL）
        with SessionLocal() as session:
            member_to_delete = session.get(PortfolioMember, member_id)
            session.delete(member_to_delete)
            session.commit()

        # 验证 SimOrder 仍存在且 member_id=NULL
        with SessionLocal() as session:
            survived = session.get(SimOrder, order_id)
            assert survived is not None
            assert survived.member_id is None
            assert survived.id == order_id
            assert survived.portfolio_id == portfolio_id
    finally:
        try:
            event.remove(engine, "connect", _enable_fk)
        except Exception:
            pass
        try:
            mgr.dispose()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 6. client_order_key 唯一索引
# ----------------------------------------------------------------------------


def test_client_order_key_unique_constraint(db_session):
    """【WP6.1】client_order_key 重复时抛 IntegrityError。"""
    p = _make_portfolio(db_session, name="QA-Unique-1")
    sym = _make_symbol(db_session, code="600005")

    _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        client_order_key="pf-1-m-1-2026-07-21-buy-v1",
    )

    # 第二个相同 client_order_key 的订单应失败
    order2 = SimOrder(
        portfolio_id=p.id,
        symbol_id=sym.id,
        side="buy",
        quantity=100.0,
        submitted_price=10.0,
        client_order_key="pf-1-m-1-2026-07-21-buy-v1",
    )
    db_session.add(order2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ----------------------------------------------------------------------------
# 7. client_order_key NULL 允许多条
# ----------------------------------------------------------------------------


def test_client_order_key_null_allows_multiple(db_session):
    """【WP6.1】client_order_key 为 NULL 时允许多条共存（部分唯一索引）。"""
    p = _make_portfolio(db_session, name="QA-Null-1")
    sym = _make_symbol(db_session, code="600006")

    o1 = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        side="buy",
        quantity=100.0,
        submitted_price=10.0,
    )
    o2 = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        side="sell",
        quantity=100.0,
        submitted_price=11.0,
    )

    assert o1.id is not None
    assert o2.id is not None
    assert o1.id != o2.id
    assert o1.client_order_key is None
    assert o2.client_order_key is None


# ----------------------------------------------------------------------------
# 8. source_type/source_id 索引
# ----------------------------------------------------------------------------


def test_query_by_source_type_and_id(db_session):
    """【WP6.1】按 source_type + source_id 查询可命中订单。"""
    p = _make_portfolio(db_session, name="QA-Source-1")
    sym = _make_symbol(db_session, code="600007")

    _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        source_type="member",
        source_id=999,
    )
    # 干扰订单：不同 source_type
    _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        side="sell",
        source_type="scan",
        source_id=999,
    )

    results = (
        db_session.query(SimOrder)
        .filter(SimOrder.source_type == "member")
        .filter(SimOrder.source_id == 999)
        .all()
    )
    assert len(results) == 1
    assert results[0].source_type == "member"
    assert results[0].source_id == 999


# ----------------------------------------------------------------------------
# 9. signal_id 索引
# ----------------------------------------------------------------------------


def test_query_by_signal_id(db_session):
    """【WP6.1】按 signal_id 查询可命中订单。"""
    p = _make_portfolio(db_session, name="QA-Signal-1")
    sym = _make_symbol(db_session, code="600008")

    _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        signal_id=42,
    )
    # 干扰订单：不同 signal_id
    _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        side="sell",
        signal_id=43,
    )

    results = (
        db_session.query(SimOrder)
        .filter(SimOrder.signal_id == 42)
        .all()
    )
    assert len(results) == 1
    assert results[0].signal_id == 42


# ----------------------------------------------------------------------------
# 10. signal_snapshot_json 存储
# ----------------------------------------------------------------------------


def test_signal_snapshot_json_storage(db_session):
    """【WP6.1】signal_snapshot_json 可存储 JSON 字符串。"""
    p = _make_portfolio(db_session, name="QA-SigSnap-1")
    sym = _make_symbol(db_session, code="600009")

    payload = '{"action":"open","score":0.8,"stage":"start"}'
    order = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        signal_snapshot_json=payload,
    )

    assert order.signal_snapshot_json == payload


# ----------------------------------------------------------------------------
# 11. decision_snapshot_json 存储
# ----------------------------------------------------------------------------


def test_decision_snapshot_json_storage(db_session):
    """【WP6.1】decision_snapshot_json 可存储 JSON 字符串。"""
    p = _make_portfolio(db_session, name="QA-DecSnap-1")
    sym = _make_symbol(db_session, code="600010")

    payload = '{"reason":"auto buy","data_freshness":"2026-07-21","rule_version":3}'
    order = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        decision_snapshot_json=payload,
    )

    assert order.decision_snapshot_json == payload


# ----------------------------------------------------------------------------
# 12. rejection_code/rejection_detail 存储
# ----------------------------------------------------------------------------


def test_rejection_fields_storage(db_session):
    """【WP6.1】rejection_code 与 rejection_detail 可写入并读回。"""
    p = _make_portfolio(db_session, name="QA-Reject-1")
    sym = _make_symbol(db_session, code="600011")

    order = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        rejection_code="DATA_EXPIRED",
        rejection_detail="K线数据过期，订单被拒绝",
    )

    assert order.rejection_code == "DATA_EXPIRED"
    assert order.rejection_detail == "K线数据过期，订单被拒绝"


# ----------------------------------------------------------------------------
# 13. execution_mode 取值
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,code_suffix",
    [
        ("manual", 12),
        ("confirm", 13),
        ("auto", 14),
    ],
)
def test_execution_mode_values(db_session, mode, code_suffix):
    """【WP6.1】execution_mode 支持 manual/confirm/auto 三种取值。"""
    p = _make_portfolio(db_session, name=f"QA-Mode-{mode}")
    sym = _make_symbol(db_session, code=f"6001{code_suffix:02d}")

    order = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        execution_mode=mode,
    )

    assert order.execution_mode == mode


# ----------------------------------------------------------------------------
# 14. schema 补丁幂等
# ----------------------------------------------------------------------------


def test_sqlite_patch_idempotent_when_table_exists(db_session):
    """【WP6.1】表已存在时多次调用 SQLite 补丁不报错。"""
    engine = db_session.bind
    # 表已由 create_all 创建
    _ensure_sqlite_sim_orders_attribution_columns(engine)
    # 第二次调用不应报错（幂等）
    _ensure_sqlite_sim_orders_attribution_columns(engine)

    inspector = inspect(engine)
    assert "sim_orders" in inspector.get_table_names()


def test_sqlite_patch_idempotent_when_table_missing():
    """【WP6.1】表不存在时 SQLite 补丁跳过且不报错（幂等）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_sim_orders_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 不创建 sim_orders 表
        _ensure_sqlite_sim_orders_attribution_columns(engine)
        # 再次调用（幂等）
        _ensure_sqlite_sim_orders_attribution_columns(engine)

        inspector = inspect(engine)
        # sim_orders 表不应被本补丁创建（仅补列）
        assert "sim_orders" not in inspector.get_table_names()
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


def test_sqlite_patch_adds_columns_when_missing():
    """【WP6.1】表存在但缺归因列时，SQLite 补丁补齐列与索引（幂等）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_sim_orders_cols_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 手动创建旧版 sim_orders（仅含原始字段，无归因字段）
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE sim_orders ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "portfolio_id INTEGER NOT NULL, "
                "symbol_id INTEGER NOT NULL, "
                "side VARCHAR(8), "
                "order_type VARCHAR(16) DEFAULT 'market', "
                "quantity FLOAT, "
                "limit_price FLOAT, "
                "submitted_price FLOAT, "
                "status VARCHAR(16) DEFAULT 'filled', "
                "filled_quantity FLOAT DEFAULT 0, "
                "filled_price FLOAT DEFAULT 0, "
                "filled_amount FLOAT DEFAULT 0, "
                "fee FLOAT DEFAULT 0, "
                "note VARCHAR(255), "
                "created_at DATETIME, "
                "filled_at DATETIME)"
            ))

        # 补丁前：归因字段不存在
        inspector = inspect(engine)
        before_cols = {col["name"] for col in inspector.get_columns("sim_orders")}
        assert "member_id" not in before_cols
        assert "client_order_key" not in before_cols

        # 调用补丁
        _ensure_sqlite_sim_orders_attribution_columns(engine)

        # 补丁后：归因字段已存在
        inspector = inspect(engine)
        after_cols = {col["name"] for col in inspector.get_columns("sim_orders")}
        for col_name in [
            "member_id", "source_type", "source_id", "signal_id",
            "signal_snapshot_json", "rule_version_id", "execution_mode",
            "client_order_key", "decision_snapshot_json",
            "rejection_code", "rejection_detail",
        ]:
            assert col_name in after_cols, f"缺失列: {col_name}"

        # 索引应已创建
        after_idx = {idx["name"] for idx in inspector.get_indexes("sim_orders")}
        for idx_name in [
            "idx_sim_orders_member", "idx_sim_orders_source",
            "idx_sim_orders_signal", "idx_sim_orders_client_key",
        ]:
            assert idx_name in after_idx, f"缺失索引: {idx_name}"

        # 再次调用（幂等）不报错
        _ensure_sqlite_sim_orders_attribution_columns(engine)
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 15. MySQL 补丁函数存在且可调用
# ----------------------------------------------------------------------------


def test_mysql_patch_function_exists_and_callable():
    """【WP6.1】_ensure_mysql_sim_orders_attribution_columns 函数存在且可调用。

    不需要真实 MySQL 实例，仅验证函数定义存在且可被调用。
    """
    assert callable(_ensure_mysql_sim_orders_attribution_columns)
    import inspect as _inspect
    sig = _inspect.signature(_ensure_mysql_sim_orders_attribution_columns)
    params = list(sig.parameters.keys())
    assert len(params) == 1
    assert params[0] == "engine"


# ----------------------------------------------------------------------------
# 16. 索引存在性
# ----------------------------------------------------------------------------


def test_indexes_exist(db_session):
    """【WP6.1】所有声明的索引都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("sim_orders")
    index_names = {idx["name"] for idx in indexes}

    expected_indexes = {
        "idx_sim_orders_member",
        "idx_sim_orders_source",
        "idx_sim_orders_signal",
        "idx_sim_orders_client_key",
    }
    assert expected_indexes.issubset(index_names), (
        f"缺失索引: {expected_indexes - index_names}; 实际: {index_names}"
    )


def test_client_order_key_index_marked_unique(db_session):
    """【WP6.1】idx_sim_orders_client_key 为唯一索引。

    SQLite inspector 返回的 unique 字段可能是 1/0 而非 True/False，
    使用真值断言以保持跨版本兼容（与 WP4.1 风格一致）。
    """
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("sim_orders")
    client_key_idx = [idx for idx in indexes if idx["name"] == "idx_sim_orders_client_key"]
    assert len(client_key_idx) == 1
    assert client_key_idx[0].get("unique"), f"索引非唯一: {client_key_idx[0]}"


# ----------------------------------------------------------------------------
# 17. 历史订单兼容
# ----------------------------------------------------------------------------


def test_legacy_order_queryable(db_session):
    """【WP6.1】模拟历史订单（不传任何新字段）可正常查询。"""
    p = _make_portfolio(db_session, name="QA-History-1")
    sym = _make_symbol(db_session, code="600099")

    order = _make_order(
        db_session,
        portfolio_id=p.id,
        symbol_id=sym.id,
        side="buy",
        quantity=500.0,
        submitted_price=8.88,
    )

    # 直接通过查询取回
    results = db_session.query(SimOrder).filter(SimOrder.id == order.id).all()
    assert len(results) == 1
    fetched = results[0]
    assert fetched.id == order.id
    assert fetched.portfolio_id == p.id
    assert fetched.symbol_id == sym.id
    assert fetched.side == "buy"
    assert fetched.quantity == 500.0
    assert fetched.submitted_price == 8.88

    # 历史订单的所有归因字段应为 NULL
    assert fetched.member_id is None
    assert fetched.source_type is None
    assert fetched.source_id is None
    assert fetched.signal_id is None
    assert fetched.signal_snapshot_json is None
    assert fetched.rule_version_id is None
    assert fetched.execution_mode is None
    assert fetched.client_order_key is None
    assert fetched.decision_snapshot_json is None
    assert fetched.rejection_code is None
    assert fetched.rejection_detail is None
