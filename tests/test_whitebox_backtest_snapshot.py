"""白盒测试 - 回测快照字段扩展（WP7.2）。

守护 BacktestRun 模型快照字段、init_db schema patch、快照构建函数：
1. BacktestRun 模型包含全部新增快照字段
2. 成员快照 JSON 正确序列化（含 effective_from/effective_to/execution_mode/规则版本）
3. 排除成员快照正确（含 member_id/symbol_id/reason）
4. 成本配置快照正确（含 commission_rate/stamp_duty_rate/slippage/min_commission）
5. SQLite schema patch 幂等（多次调用不报错，列存在即跳过）
6. MySQL schema patch 函数存在且可调用
7. 历史回测（快照字段全为 None）仍可读取
8. 快照字段持久化与回读一致
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.backtest import BacktestRun
from app.models.portfolio import Portfolio
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    EXECUTION_CONFIRM,
    PortfolioMember,
)
from app.services.portfolio_backtest_snapshot import (
    build_cost_config_snapshot,
    build_excluded_members_snapshot,
    build_member_snapshot,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================


def _make_portfolio(db_session, name="QA-Snap-Port") -> Portfolio:
    p = Portfolio(
        name=name,
        account_type="simulated",
        total_capital=100000.0,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
        auto_trade_enabled=1,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _make_member(
    db_session,
    portfolio_id: int,
    symbol_id: int,
    *,
    execution_mode: str = EXECUTION_AUTO,
    entry_rule_version_id: int | None = 1,
    exit_rule_version_id: int | None = 2,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> PortfolioMember:
    m = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        execution_mode=execution_mode,
        entry_rule_version_id=entry_rule_version_id,
        exit_rule_version_id=exit_rule_version_id,
        effective_from=effective_from or datetime(2026, 1, 1),
        effective_to=effective_to,
    )
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


def _make_symbol(db_session, symbol="600999", name="snap-test") -> object:
    from app.models.symbol import Symbol
    sym = Symbol(symbol=symbol, name=name, asset_type="stock", market="SH")
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


# ============================================================================
# 1. 模型字段存在性
# ============================================================================


class TestBacktestRunSnapshotFields:
    """守护 BacktestRun 模型包含 WP7.2 全部新增快照字段。"""

    EXPECTED_NEW_FIELDS = [
        "member_snapshot_json",
        "symbol_ids_json",
        "excluded_members_json",
        "portfolio_rule_version_id",
        "score_mode",
        "data_cutoff_at",
        "engine_name",
        "engine_version",
        "source_type",
    ]

    # 已存在的相关字段（不应被重复声明或破坏）
    EXISTING_RELATED_FIELDS = [
        "cost_config_json",
        "factor_model_run_id",
        "factor_data_cutoff_at",
        "score_weight_mode",
    ]

    def test_backtest_run_has_snapshot_fields(self):
        """BacktestRun 模型类应包含全部新增快照字段属性。"""
        for field in self.EXPECTED_NEW_FIELDS:
            assert hasattr(BacktestRun, field), (
                f"BacktestRun 缺少快照字段: {field}"
            )

    def test_backtest_run_existing_fields_preserved(self):
        """已存在的相关字段不应被破坏。"""
        for field in self.EXISTING_RELATED_FIELDS:
            assert hasattr(BacktestRun, field), (
                f"BacktestRun 已有字段被破坏: {field}"
            )

    def test_backtest_run_snapshot_columns_in_db(self, db_session):
        """数据库 backtest_runs 表应包含全部新增快照列。"""
        engine = db_session.bind
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("backtest_runs")}
        for field in self.EXPECTED_NEW_FIELDS:
            assert field in columns, (
                f"数据库表 backtest_runs 缺少列: {field}"
            )


# ============================================================================
# 2. 成员快照 JSON 序列化
# ============================================================================


class TestMemberSnapshotSerialization:
    """守护 build_member_snapshot 的序列化正确性。"""

    def test_member_snapshot_json_serialization(self, db_session):
        """成员快照应包含全部关键字段且可 JSON 序列化。"""
        p = _make_portfolio(db_session, name="QA-Snap-Member")
        sym = _make_symbol(db_session, symbol="600001", name="A")
        m1 = _make_member(
            db_session, p.id, sym.id,
            execution_mode=EXECUTION_AUTO,
            entry_rule_version_id=10,
            exit_rule_version_id=20,
            effective_from=datetime(2026, 1, 1),
            effective_to=None,
        )
        m2 = _make_member(
            db_session, p.id, sym.id,
            execution_mode=EXECUTION_CONFIRM,
            entry_rule_version_id=None,
            exit_rule_version_id=None,
            effective_from=datetime(2026, 2, 1),
            effective_to=datetime(2026, 6, 30),
        )

        snapshot = build_member_snapshot([m1, m2])

        assert len(snapshot) == 2
        # 第一个成员：auto 模式、有规则版本、effective_to 为 None
        s1 = snapshot[0]
        assert s1["member_id"] == m1.id
        assert s1["symbol_id"] == sym.id
        assert s1["execution_mode"] == EXECUTION_AUTO
        assert s1["entry_rule_version_id"] == 10
        assert s1["exit_rule_version_id"] == 20
        assert s1["effective_from"] == "2026-01-01T00:00:00"
        assert s1["effective_to"] is None
        # 第二个成员：confirm 模式、无规则版本、effective_to 已设置
        s2 = snapshot[1]
        assert s2["execution_mode"] == EXECUTION_CONFIRM
        assert s2["entry_rule_version_id"] is None
        assert s2["exit_rule_version_id"] is None
        assert s2["effective_to"] == "2026-06-30T00:00:00"

        # 可 JSON 序列化（datetime 已转为 isoformat 字符串）
        serialized = json.dumps(snapshot, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert deserialized == snapshot

    def test_member_snapshot_empty_list(self):
        """空成员列表应返回空快照列表。"""
        assert build_member_snapshot([]) == []


# ============================================================================
# 3. 排除成员快照
# ============================================================================


class TestExcludedMembersSnapshot:
    """守护 build_excluded_members_snapshot 的正确性。"""

    def test_excluded_members_snapshot(self, db_session):
        """排除成员快照应包含 member_id/symbol_id/reason。

        注：portfolio_members 有部分唯一索引（同一 portfolio+symbol 仅一条
        effective_to IS NULL 记录），故使用不同 symbol 创建两个成员。
        """
        p = _make_portfolio(db_session, name="QA-Snap-Excluded")
        sym_a = _make_symbol(db_session, symbol="600002", name="B")
        sym_b = _make_symbol(db_session, symbol="600003", name="C-excl")
        m1 = _make_member(db_session, p.id, sym_a.id)
        m2 = _make_member(db_session, p.id, sym_b.id)

        excluded = [
            (m1, "member_archived"),
            (m2, "rule_version_missing"),
        ]

        snapshot = build_excluded_members_snapshot(excluded)

        assert len(snapshot) == 2
        assert snapshot[0] == {
            "member_id": m1.id,
            "symbol_id": sym_a.id,
            "reason": "member_archived",
        }
        assert snapshot[1] == {
            "member_id": m2.id,
            "symbol_id": sym_b.id,
            "reason": "rule_version_missing",
        }

        # 可 JSON 序列化
        json.dumps(snapshot)

    def test_excluded_members_empty_list(self):
        """空排除列表应返回空快照列表。"""
        assert build_excluded_members_snapshot([]) == []


# ============================================================================
# 4. 成本配置快照
# ============================================================================


class TestCostConfigSnapshot:
    """守护 build_cost_config_snapshot 的正确性。"""

    def test_cost_config_snapshot(self, db_session):
        """成本配置快照应包含 4 个费率字段。

        当前 Portfolio 模型未直接持有成本配置字段，使用 getattr 兜底 None。
        本测试验证字段齐全且值为 None（向前兼容后续扩展）。
        """
        p = _make_portfolio(db_session, name="QA-Snap-Cost")
        snapshot = build_cost_config_snapshot(p)

        assert set(snapshot.keys()) == {
            "commission_rate",
            "stamp_duty_rate",
            "slippage",
            "min_commission",
        }
        # Portfolio 未扩展成本字段，应为 None
        for v in snapshot.values():
            assert v is None

    def test_cost_config_snapshot_with_extended_portfolio(self):
        """扩展后的 Portfolio（含成本字段）应正确读取。

        使用 SimpleNamespace 模拟未来 Portfolio 扩展成本字段后的场景。
        """
        fake_portfolio = SimpleNamespace(
            commission_rate=0.0003,
            stamp_duty_rate=0.001,
            slippage=0.001,
            min_commission=5.0,
        )
        snapshot = build_cost_config_snapshot(fake_portfolio)

        assert snapshot == {
            "commission_rate": 0.0003,
            "stamp_duty_rate": 0.001,
            "slippage": 0.001,
            "min_commission": 5.0,
        }


# ============================================================================
# 5. SQLite schema patch 幂等
# ============================================================================


class TestSqlitePatchIdempotent:
    """守护 _ensure_sqlite_backtest_snapshot_columns 的幂等性。"""

    def test_sqlite_patch_idempotent(self, tmp_sqlite_url):
        """SQLite patch 多次调用应幂等：列已存在时跳过，不报错。

        模拟旧库：手动创建只含历史列的 backtest_runs 表，
        调用 patch 应补齐缺失列；再次调用应无副作用。
        """
        from app.db.init_db import _ensure_sqlite_backtest_snapshot_columns

        engine = create_engine(tmp_sqlite_url)
        # 模拟旧库：创建只含历史列的 backtest_runs 表（无 WP7.2 快照列）
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE backtest_runs ("
                "id INTEGER PRIMARY KEY, "
                "portfolio_id INTEGER, "
                "run_name VARCHAR(128), "
                "symbols_json TEXT, "
                "rule_config_json TEXT, "
                "cost_config_json TEXT, "
                "start_date DATE, "
                "end_date DATE, "
                "initial_capital REAL, "
                "status VARCHAR(16))"
            ))

        inspector = inspect(engine)
        columns_before = {c["name"] for c in inspector.get_columns("backtest_runs")}
        # 确认旧库不含快照列
        assert "member_snapshot_json" not in columns_before
        assert "engine_version" not in columns_before

        # 第一次调用：应补齐全部 9 个快照列
        _ensure_sqlite_backtest_snapshot_columns(engine)

        columns_after_first = {
            c["name"] for c in inspect(engine).get_columns("backtest_runs")
        }
        expected_new_cols = {
            "member_snapshot_json",
            "symbol_ids_json",
            "excluded_members_json",
            "portfolio_rule_version_id",
            "score_mode",
            "data_cutoff_at",
            "engine_name",
            "engine_version",
            "source_type",
        }
        for col in expected_new_cols:
            assert col in columns_after_first, f"首次 patch 后仍缺列: {col}"

        # 第二次调用：幂等，不应报错也不应重复添加
        _ensure_sqlite_backtest_snapshot_columns(engine)

        columns_after_second = {
            c["name"] for c in inspect(engine).get_columns("backtest_runs")
        }
        assert columns_after_first == columns_after_second

        # 第三次调用：继续幂等
        _ensure_sqlite_backtest_snapshot_columns(engine)
        columns_after_third = {
            c["name"] for c in inspect(engine).get_columns("backtest_runs")
        }
        assert columns_after_third == columns_after_first

    def test_sqlite_patch_skips_when_table_missing(self, tmp_sqlite_url):
        """表不存在时 patch 应安全跳过，不报错。"""
        from app.db.init_db import _ensure_sqlite_backtest_snapshot_columns

        engine = create_engine(tmp_sqlite_url)
        # 不创建 backtest_runs 表
        _ensure_sqlite_backtest_snapshot_columns(engine)  # 应不报错


# ============================================================================
# 6. MySQL schema patch 函数存在
# ============================================================================


class TestMysqlPatchFunction:
    """守护 _ensure_mysql_backtest_snapshot_columns 函数存在且可调用。"""

    def test_mysql_patch_function_exists(self):
        """MySQL patch 函数应存在且可调用。"""
        from app.db.init_db import _ensure_mysql_backtest_snapshot_columns

        assert callable(_ensure_mysql_backtest_snapshot_columns)

    def test_mysql_patch_function_signature(self):
        """MySQL patch 函数应接收单个 engine 参数。"""
        import inspect as pyinspect
        from app.db.init_db import _ensure_mysql_backtest_snapshot_columns

        sig = pyinspect.signature(_ensure_mysql_backtest_snapshot_columns)
        params = list(sig.parameters.keys())
        assert params == ["engine"]


# ============================================================================
# 7. 历史回测（无快照字段）仍可读
# ============================================================================


class TestLegacyBacktestRunReadable:
    """守护历史回测（快照字段为 None）仍可正常读取。"""

    def test_legacy_backtest_run_without_snapshot_fields(self, db_session):
        """历史回测（仅必填字段，快照字段全为 None）应可创建并读取。"""
        p = _make_portfolio(db_session, name="QA-Snap-Legacy")

        # 仅填充必填字段，所有快照字段保持 None
        run = BacktestRun(
            portfolio_id=p.id,
            run_name="legacy-run",
            symbols_json=json.dumps([1, 2, 3]),
            rule_config_json=json.dumps({"buy_conditions": {}}),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            initial_capital=100000.0,
            status="completed",
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # 验证快照字段全部为 None
        assert run.member_snapshot_json is None
        assert run.symbol_ids_json is None
        assert run.excluded_members_json is None
        assert run.portfolio_rule_version_id is None
        assert run.score_mode is None
        assert run.data_cutoff_at is None
        assert run.engine_name is None
        assert run.engine_version is None
        assert run.source_type is None

        # 重新查询验证可读
        reloaded = db_session.get(BacktestRun, run.id)
        assert reloaded is not None
        assert reloaded.run_name == "legacy-run"
        assert reloaded.member_snapshot_json is None
        assert reloaded.portfolio_rule_version_id is None


# ============================================================================
# 8. 快照字段持久化与回读
# ============================================================================


class TestSnapshotPersistence:
    """守护快照字段可持久化写入并按原值回读。"""

    def test_snapshot_round_trip_persistence(self, db_session):
        """快照字段写入后应按原值回读（同一快照重复运行可对比）。"""
        p = _make_portfolio(db_session, name="QA-Snap-Persist")
        sym = _make_symbol(db_session, symbol="600003", name="C")
        m = _make_member(
            db_session, p.id, sym.id,
            execution_mode=EXECUTION_AUTO,
            entry_rule_version_id=100,
            exit_rule_version_id=200,
        )

        # 构建快照
        member_snapshot = build_member_snapshot([m])
        excluded_snapshot = build_excluded_members_snapshot([
            (m, "test_exclusion_reason"),
        ])
        cost_snapshot = build_cost_config_snapshot(p)

        # 写入 BacktestRun
        run = BacktestRun(
            portfolio_id=p.id,
            run_name="snapshot-persist-test",
            symbols_json=json.dumps([sym.id]),
            symbol_ids_json=json.dumps([sym.id]),
            rule_config_json=json.dumps({}),
            cost_config_json=json.dumps(cost_snapshot),
            member_snapshot_json=json.dumps(member_snapshot, ensure_ascii=False),
            excluded_members_json=json.dumps(excluded_snapshot, ensure_ascii=False),
            portfolio_rule_version_id=42,
            score_mode="combined",
            data_cutoff_at=datetime(2026, 1, 15),
            engine_name="event_driven",
            engine_version="1.0.0",
            source_type="member",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            initial_capital=100000.0,
            status="completed",
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # 重新查询验证回读
        reloaded = db_session.get(BacktestRun, run.id)
        assert reloaded is not None

        # 标量字段
        assert reloaded.portfolio_rule_version_id == 42
        assert reloaded.score_mode == "combined"
        assert reloaded.data_cutoff_at == datetime(2026, 1, 15)
        assert reloaded.engine_name == "event_driven"
        assert reloaded.engine_version == "1.0.0"
        assert reloaded.source_type == "member"

        # JSON 字段
        assert json.loads(reloaded.symbol_ids_json) == [sym.id]
        assert json.loads(reloaded.member_snapshot_json) == member_snapshot
        assert json.loads(reloaded.excluded_members_json) == excluded_snapshot
        assert json.loads(reloaded.cost_config_json) == cost_snapshot

    def test_symbol_ids_json_field_persistence(self, db_session):
        """symbol_ids_json 字段可独立持久化与回读。"""
        p = _make_portfolio(db_session, name="QA-Snap-SymbolIds")
        run = BacktestRun(
            portfolio_id=p.id,
            run_name="symbol-ids-test",
            symbols_json=json.dumps([1, 2, 3]),
            symbol_ids_json=json.dumps([10, 20, 30]),
            rule_config_json="{}",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            initial_capital=50000.0,
            status="completed",
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        reloaded = db_session.get(BacktestRun, run.id)
        assert json.loads(reloaded.symbol_ids_json) == [10, 20, 30]
        # legacy symbols_json 字段不受影响
        assert json.loads(reloaded.symbols_json) == [1, 2, 3]
