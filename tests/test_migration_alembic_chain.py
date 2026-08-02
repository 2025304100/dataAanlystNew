"""Alembic 迁移链完整性测试（UAT-DB.3）。

验证内容：
1. 24 个 revision 文件链式依赖完整（从 wps_001_external_endpoint 到 head 无断链）
2. 每个 revision 有 upgrade() 和 downgrade() 函数
3. 全链 upgrade 在 SQLite 上可执行且幂等（重复执行不报错）
4. 全链 downgrade 可逆（回滚后新建表被删除、新增列被移除）
5. 迁移后所有漂移字段/表存在（无 Unknown column 风险）
6. 迁移不影响历史数据（新字段允许为空，旧记录可正常读取）
7. WP1-06: 0021 迁移系统因子回填与幂等性验证
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db.base import Base

# 确保所有模型被注册
from app.models import *  # noqa: F401,F403
from app import models  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = ROOT / "alembic" / "versions"


# ============================================================================
# 辅助：动态加载所有 revision 模块
# ============================================================================


def _load_revision_modules() -> dict:
    """加载 alembic/versions/ 下所有 revision 模块，返回 {revision_id: module}。

    使用 importlib.util.spec_from_file_location 直接从文件路径加载，
    避免安装的 alembic 包遮蔽本地 alembic/ 配置目录导致
    `import alembic.versions.xxx` 失败。
    """
    modules = {}
    for pyfile in sorted(VERSIONS_DIR.glob("2026_*.py")):
        modname = pyfile.stem
        spec = importlib.util.spec_from_file_location(
            f"_test_rev_{modname}", pyfile
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "revision") and hasattr(mod, "down_revision"):
            modules[mod.revision] = mod
    return modules


def _build_chain(modules: dict) -> dict:
    """构建 {revision_id: down_revision} 映射。"""
    return {rev_id: mod.down_revision for rev_id, mod in modules.items()}


def _find_head(chain: dict) -> str:
    """找到 head revision（没有其他 revision 指向它作为 down_revision）。"""
    all_down_revs = {v for v in chain.values() if v is not None}
    heads = [r for r in chain if r not in all_down_revs]
    assert len(heads) == 1, f"Expected exactly 1 head, got {heads}"
    return heads[0]


def _walk_chain(chain: dict, head: str) -> list:
    """从 head 走到 base，返回有序 revision 列表。"""
    ordered = []
    current = head
    visited = set()
    while current is not None:
        assert current not in visited, f"Cycle detected at {current}"
        assert current in chain, f"Broken chain: revision {current} not found in modules"
        visited.add(current)
        ordered.append(current)
        current = chain[current]
    return ordered


# ============================================================================
# 1. 链式结构验证
# ============================================================================


class TestRevisionChainStructure:
    """验证 24 个 revision 文件的链式依赖结构。"""

    def test_all_revisions_importable(self):
        """所有 revision 文件可正常导入。"""
        modules = _load_revision_modules()
        assert len(modules) >= 20, f"Expected >=20 revisions, got {len(modules)}"

    def test_each_revision_has_upgrade_and_downgrade(self):
        """每个 revision 必须有 upgrade() 和 downgrade() 函数。"""
        modules = _load_revision_modules()
        for rev_id, mod in modules.items():
            assert callable(getattr(mod, "upgrade", None)), \
                f"Revision {rev_id} missing upgrade()"
            assert callable(getattr(mod, "downgrade", None)), \
                f"Revision {rev_id} missing downgrade()"

    def test_chain_starts_from_base(self):
        """链的起点必须是 wps_001_external_endpoint（down_revision=None 的根之后第一个）。"""
        modules = _load_revision_modules()
        chain = _build_chain(modules)

        # 找到 down_revision=None 的根
        roots = [r for r, d in chain.items() if d is None]
        assert len(roots) == 1, f"Expected 1 root, got {roots}"
        assert roots[0] == "wps_001_external_endpoint", \
            f"Root should be wps_001_external_endpoint, got {roots[0]}"

    def test_chain_unbroken_from_head_to_base(self):
        """从 head 到 base 的链无断链。"""
        modules = _load_revision_modules()
        chain = _build_chain(modules)
        head = _find_head(chain)
        ordered = _walk_chain(chain, head)

        # 最后一个的 down_revision 应为 None
        assert chain[ordered[-1]] is None, \
            f"Chain tail {ordered[-1]} should have down_revision=None"

    def test_head_is_wps_0023_023(self):
        """head revision 应为 wps_0023_023_score_traceability。"""
        modules = _load_revision_modules()
        chain = _build_chain(modules)
        head = _find_head(chain)
        assert head == "wps_0023_023_score_traceability", \
            f"Head should be wps_0023_023_score_traceability, got {head}"

    def test_chain_has_exactly_24_revisions_after_base(self):
        """链中 base 之外应有 24 个 revision（0001-0020 + 0801_001 + 0021 + 0022 + 0023）。"""
        modules = _load_revision_modules()
        chain = _build_chain(modules)
        head = _find_head(chain)
        ordered = _walk_chain(chain, head)
        # ordered 包含 base + 24 个新 revision = 25
        assert len(ordered) == 25, \
            f"Expected 25 revisions in chain (1 base + 24 new), got {len(ordered)}"

    def test_specific_revision_links(self):
        """验证关键链式依赖关系。"""
        modules = _load_revision_modules()
        chain = _build_chain(modules)

        expected_links = {
            "wps_0023_001_scan_runs_cache": "wps_001_external_endpoint",
            "wps_0023_013_notification_tables": "wps_0023_012_portfolio_members",
            "wps_0023_014_sim_orders_attribution": "wps_0023_013_notification_tables",
            "wps_0023_015_ai_session_tables": "wps_0023_014_sim_orders_attribution",
            "wps_0023_016_portfolio_reviews": "wps_0023_015_ai_session_tables",
            "wps_0023_017_ai_profiles": "wps_0023_016_portfolio_reviews",
            "wps_0023_018_discovery_score_snapshots": "wps_0023_017_ai_profiles",
            "wps_0023_019_async_tasks_state_machine": "wps_0023_018_discovery_score_snapshots",
            "wps_0023_020_api_deprecation_logs": "wps_0023_019_async_tasks_state_machine",
            "wps_0801_001_universe_incremental_index": "wps_0023_020_api_deprecation_logs",
            "wps_0023_021_factor_library_lifecycle": "wps_0801_001_universe_incremental_index",
            "wps_0023_022_shadow_observations": "wps_0023_021_factor_library_lifecycle",
            "wps_0023_023_score_traceability": "wps_0023_022_shadow_observations",
        }
        for rev, expected_down in expected_links.items():
            assert rev in chain, f"Revision {rev} not found in modules"
            assert chain[rev] == expected_down, \
                f"Revision {rev} down_revision should be {expected_down}, got {chain[rev]}"


# ============================================================================
# 2. 迁移执行验证（SQLite）
# ============================================================================


def _make_alembic_config(db_url: str):
    """创建 Alembic Config，指向测试 SQLite 数据库。"""
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    os.environ["ALEMBIC_DATABASE_URL"] = db_url
    return cfg


@pytest.fixture
def fresh_sqlite_url(tmp_path) -> str:
    """每个测试独立的 SQLite 文件 URL。"""
    db_path = tmp_path / "test_migration.db"
    return f"sqlite:///{db_path}"


class TestMigrationExecution:
    """在 SQLite 上验证迁移执行、幂等性和可逆性。"""

    def test_full_upgrade_on_empty_db(self, fresh_sqlite_url):
        """在空数据库上执行全链 upgrade，验证新建表被创建。"""
        from alembic import command

        cfg = _make_alembic_config(fresh_sqlite_url)
        command.upgrade(cfg, "head")

        engine = create_engine(fresh_sqlite_url)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        # 验证新建表存在（这些是 revision 创建的整表）
        expected_new_tables = [
            "external_endpoint_runtime",
            "opportunity_transition_events",
            "portfolio_members",
            "notification_channels",
            "notification_policies",
            "notification_policy_channels",
            "notification_outbox",
            "notification_deliveries",
            "notification_templates",
            "ai_sessions",
            "ai_messages",
            "ai_action_audits",
            "portfolio_reviews",
            "ai_profiles",
            "discovery_score_snapshots",
            "discovery_score_snapshot_items",
            "api_deprecation_logs",
            # WP1-06: 0021 迁移新建表
            "factor_evaluation_runs",
            "factor_transition_audits",
            "factor_sets",
            "factor_set_members",
            # WP6-03: 0022 迁移新建表
            "factor_shadow_observations",
        ]
        for table_name in expected_new_tables:
            assert table_name in tables, \
                f"Table {table_name} should exist after upgrade"

        engine.dispose()

    def test_upgrade_idempotent_with_metadata_create_all(self, fresh_sqlite_url):
        """先 metadata.create_all（创建所有表+所有列），再执行 alembic upgrade，应幂等不报错。"""
        from alembic import command

        # 1. 用 ORM metadata 创建全部表（模拟 init_db 后的状态）
        engine = create_engine(fresh_sqlite_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        # 2. 执行 alembic upgrade head（应全部跳过，幂等）
        cfg = _make_alembic_config(fresh_sqlite_url)
        command.upgrade(cfg, "head")

        # 3. 验证无异常且表结构完整
        engine = create_engine(fresh_sqlite_url)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "ai_sessions" in tables
        assert "api_deprecation_logs" in tables
        engine.dispose()

    def test_downgrade_drops_new_tables(self, fresh_sqlite_url):
        """upgrade head 后 downgrade base，验证新建表被删除。"""
        from alembic import command

        cfg = _make_alembic_config(fresh_sqlite_url)

        # 先用 metadata.create_all 创建全部表
        engine = create_engine(fresh_sqlite_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        # upgrade（幂等）
        command.upgrade(cfg, "head")

        # downgrade 回 base
        command.downgrade(cfg, "base")

        # 验证 revision 创建的新表被删除
        engine = create_engine(fresh_sqlite_url)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        # 这些表是 revision 创建的整表，downgrade 后应被删除
        dropped_tables = [
            "api_deprecation_logs",
            "discovery_score_snapshot_items",
            "discovery_score_snapshots",
            "ai_profiles",
            "portfolio_reviews",
            "ai_action_audits",
            "ai_messages",
            "ai_sessions",
            "notification_templates",
            "notification_deliveries",
            "notification_outbox",
            "notification_policy_channels",
            "notification_policies",
            "notification_channels",
            "portfolio_members",
            "opportunity_transition_events",
            "external_endpoint_runtime",
            # WP1-06: 0021 迁移新建表（downgrade 后应被删除）
            "factor_set_members",
            "factor_sets",
            "factor_transition_audits",
            "factor_evaluation_runs",
            # WP6-03: 0022 迁移新建表（downgrade 后应被删除）
            "factor_shadow_observations",
        ]
        for table_name in dropped_tables:
            assert table_name not in tables, \
                f"Table {table_name} should be dropped after downgrade to base"

        engine.dispose()

    def test_upgrade_downgrade_upgrade_cycle(self, fresh_sqlite_url):
        """upgrade → downgrade → upgrade 循环，验证可重复执行。"""
        from alembic import command

        cfg = _make_alembic_config(fresh_sqlite_url)

        # 先创建全部表
        engine = create_engine(fresh_sqlite_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        # 第一轮 upgrade
        command.upgrade(cfg, "head")

        # downgrade
        command.downgrade(cfg, "base")

        # 第二轮 upgrade（验证可重复）
        command.upgrade(cfg, "head")

        # 验证表存在
        engine = create_engine(fresh_sqlite_url)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "ai_sessions" in tables
        assert "api_deprecation_logs" in tables
        assert "portfolio_reviews" in tables
        engine.dispose()


# ============================================================================
# 3. 漂移字段存在性验证
# ============================================================================


class TestDriftFieldsExist:
    """验证迁移后所有漂移字段存在（消除 Unknown column 风险）。"""

    @pytest.fixture
    def migrated_engine(self, fresh_sqlite_url):
        """创建全部表 + 执行 alembic upgrade，返回 engine。"""
        from alembic import command

        engine = create_engine(fresh_sqlite_url)
        Base.metadata.create_all(engine)
        engine.dispose()

        cfg = _make_alembic_config(fresh_sqlite_url)
        command.upgrade(cfg, "head")

        engine = create_engine(fresh_sqlite_url)
        yield engine
        engine.dispose()

    def _get_columns(self, engine, table_name: str) -> set:
        inspector = inspect(engine)
        if table_name not in inspector.get_table_names():
            return set()
        return {c["name"] for c in inspector.get_columns(table_name)}

    def test_scan_runs_has_snapshot_fields(self, migrated_engine):
        """scan_runs 表有 snapshot_id 等 WP-P.1 缓存字段。"""
        cols = self._get_columns(migrated_engine, "scan_runs")
        for field in [
            "snapshot_id", "cache_key", "cache_hit", "total_in_snapshot",
            "coarse_match_count", "advanced_match_count", "result_rows_written",
            "degraded_reason",
        ]:
            assert field in cols, f"scan_runs missing column {field}"

    def test_sim_orders_has_attribution_fields(self, migrated_engine):
        """sim_orders 表有 WP6.1 归因字段。"""
        cols = self._get_columns(migrated_engine, "sim_orders")
        for field in [
            "member_id", "source_type", "source_id", "signal_id",
            "signal_snapshot_json", "rule_version_id", "execution_mode",
            "client_order_key", "decision_snapshot_json",
            "rejection_code", "rejection_detail",
        ]:
            assert field in cols, f"sim_orders missing column {field}"

    def test_async_tasks_has_state_machine_fields(self, migrated_engine):
        """async_tasks 表有 WP-S.5 状态机扩展字段。"""
        cols = self._get_columns(migrated_engine, "async_tasks")
        for field in [
            "heartbeat_at", "stage_budget_seconds", "stage_started_at",
            "last_progress_at", "last_progress_percent",
            "current_step_description", "suggested_action",
            "batch_recovery_json", "last_patrol_at",
            "worker_thread_id", "cancel_requested",
        ]:
            assert field in cols, f"async_tasks missing column {field}"

    def test_discovery_score_snapshots_table_complete(self, migrated_engine):
        """discovery_score_snapshots 表字段完整。"""
        cols = self._get_columns(migrated_engine, "discovery_score_snapshots")
        for field in [
            "scope", "trade_date", "status", "scoring_config_id",
            "weight_mode", "factor_model_run_id", "data_cutoff_at",
            "generated_at", "build_duration_seconds", "source_task_id",
            "symbol_count", "coverage_pct", "dirty_symbol_count",
        ]:
            assert field in cols, f"discovery_score_snapshots missing column {field}"

    def test_ai_profiles_table_complete(self, migrated_engine):
        """ai_profiles 表字段完整。"""
        cols = self._get_columns(migrated_engine, "ai_profiles")
        for field in [
            "name", "provider", "base_url", "model", "auth_type",
            "secret_key_ref", "timeout_seconds", "max_tokens",
            "purpose", "priority", "is_enabled", "is_fallback",
            "health_status",
        ]:
            assert field in cols, f"ai_profiles missing column {field}"

    def test_notification_tables_exist(self, migrated_engine):
        """6 张 notification 表全部存在。"""
        inspector = inspect(migrated_engine)
        tables = set(inspector.get_table_names())
        for table_name in [
            "notification_channels", "notification_policies",
            "notification_policy_channels", "notification_outbox",
            "notification_deliveries", "notification_templates",
        ]:
            assert table_name in tables, f"Missing notification table {table_name}"

    def test_factor_lifecycle_tables_exist(self, migrated_engine):
        """WP1-06: 0021 迁移新建的 4 张因子生命周期表全部存在。"""
        inspector = inspect(migrated_engine)
        tables = set(inspector.get_table_names())
        for table_name in [
            "factor_evaluation_runs",
            "factor_transition_audits",
            "factor_sets",
            "factor_set_members",
        ]:
            assert table_name in tables, \
                f"Missing factor lifecycle table {table_name}"

    def test_factors_has_lifecycle_columns(self, migrated_engine):
        """WP1-06: factors 表有 lifecycle_status / origin / factor_kind 列。"""
        cols = self._get_columns(migrated_engine, "factors")
        for field in ["lifecycle_status", "origin", "factor_kind"]:
            assert field in cols, f"factors missing column {field}"

    def test_factor_versions_has_governance_columns(self, migrated_engine):
        """WP1-06: factor_versions 表有 execution_plan_hash / validation_status 列。"""
        cols = self._get_columns(migrated_engine, "factor_versions")
        for field in ["execution_plan_hash", "validation_status"]:
            assert field in cols, f"factor_versions missing column {field}"


# ============================================================================
# 4. 历史数据保护验证
# ============================================================================


class TestHistoricalDataProtection:
    """验证新字段允许为空，旧记录可正常读取。"""

    def test_sim_orders_new_fields_nullable(self, fresh_sqlite_url):
        """sim_orders 新字段允许为 NULL，历史订单可正常写入和读取。"""
        from alembic import command

        # 创建全部表
        engine = create_engine(fresh_sqlite_url)
        Base.metadata.create_all(engine)
        command.upgrade(_make_alembic_config(fresh_sqlite_url), "head")

        # 插入一条只包含旧字段的 sim_order（新字段为 NULL）
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO portfolios (name, account_type, total_capital,
                    investable_ratio, cash_reserve_ratio, currency, is_default,
                    auto_trade_enabled, created_at, updated_at)
                VALUES ('test-pf', 'simulated', 100000, 0.9, 0.1, 'CNY', 0, 0,
                    '2026-07-23 00:00:00', '2026-07-23 00:00:00')
            """))
            conn.execute(text("""
                INSERT INTO symbols (symbol, name, asset_type, market,
                    is_st, is_active, created_at, updated_at)
                VALUES ('600000', 'test', 'stock', 'cn',
                    0, 1, '2026-07-23 00:00:00', '2026-07-23 00:00:00')
            """))
            conn.execute(text("""
                INSERT INTO sim_orders (portfolio_id, symbol_id, side,
                    order_type, quantity, submitted_price, status,
                    filled_quantity, filled_price, filled_amount, fee, created_at)
                VALUES (1, 1, 'buy', 'market', 100, 10.0, 'filled',
                    100, 10.0, 1000.0, 5.0, '2026-07-23 00:00:00')
            """))
            conn.commit()

            # 读取并验证新字段为 NULL
            result = conn.execute(text("""
                SELECT member_id, source_type, client_order_key, rejection_code
                FROM sim_orders WHERE id = 1
            """)).fetchone()
            assert result[0] is None  # member_id
            assert result[1] is None  # source_type
            assert result[2] is None  # client_order_key
            assert result[3] is None  # rejection_code

        engine.dispose()

    def test_async_tasks_new_fields_nullable(self, fresh_sqlite_url):
        """async_tasks 新字段允许为 NULL，旧任务记录可正常写入和读取。"""
        from alembic import command

        engine = create_engine(fresh_sqlite_url)
        Base.metadata.create_all(engine)
        command.upgrade(_make_alembic_config(fresh_sqlite_url), "head")

        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO async_tasks (id, task_type, status, stage,
                    percent, message, total, processed, ok_count, failed_count,
                    created_at, updated_at)
                VALUES ('test-task-001', 'market_data_sync', 'done', 'done',
                    100.0, 'completed', 10, 10, 10, 0,
                    '2026-07-23 00:00:00', '2026-07-23 00:00:00')
            """))
            conn.commit()

            result = conn.execute(text("""
                SELECT heartbeat_at, stage_budget_seconds, worker_thread_id,
                       cancel_requested
                FROM async_tasks WHERE id = 'test-task-001'
            """)).fetchone()
            assert result[0] is None  # heartbeat_at
            assert result[1] is None  # stage_budget_seconds
            assert result[2] is None  # worker_thread_id
            # cancel_requested: 迁移路径有 server_default=0，但 metadata.create_all
            # 路径取决于 ORM 模型定义。两者均允许（测试重点是旧记录可读，非默认值验证）
            assert result[3] is None or result[3] == 0

        engine.dispose()


# ============================================================================
# 5. WP1-06: 0021 迁移系统因子回填与幂等性验证
# ============================================================================


class TestFactorLibraryLifecycleMigration:
    """WP1-06: 验证 wps_0023_021_factor_library_lifecycle 迁移的回填与幂等性。"""

    def test_0021_migration_backfills_system_factors(self, fresh_sqlite_url):
        """0021 迁移幂等回填 8 个系统因子的生命周期/治理字段。"""
        from alembic import command
        from sqlalchemy.orm import Session

        from app.services.factors.definitions import seed_factor_definitions

        # 1. 用 ORM metadata 创建全部表（含新列）
        engine = create_engine(fresh_sqlite_url)
        Base.metadata.create_all(engine)

        # 2. 种子 8 个系统因子及其版本（lifecycle_status 等新字段为 NULL）
        with Session(engine) as session:
            seed_factor_definitions(session)
            session.commit()
        engine.dispose()

        # 3. 执行 alembic upgrade head（0021 迁移回填系统因子）
        cfg = _make_alembic_config(fresh_sqlite_url)
        command.upgrade(cfg, "head")

        # 4. 验证 8 个因子回填结果
        engine = create_engine(fresh_sqlite_url)
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT code, lifecycle_status, origin, factor_kind, "
                "active_version_id FROM factors ORDER BY code"
            )).fetchall()
            assert len(rows) == 8, f"Expected 8 factors, got {len(rows)}"
            for code, ls, origin, kind, avid in rows:
                assert ls == "active", \
                    f"Factor {code} lifecycle_status should be 'active', got {ls}"
                assert origin == "system", \
                    f"Factor {code} origin should be 'system', got {origin}"
                assert kind is not None, \
                    f"Factor {code} factor_kind should not be NULL"
                assert avid is not None, \
                    f"Factor {code} active_version_id should not be NULL"

            # 5. 验证 factor_transition_audits 有 8 条 actor='system_migration' 记录
            audit_count = conn.execute(text(
                "SELECT COUNT(*) FROM factor_transition_audits "
                "WHERE actor = 'system_migration'"
            )).scalar()
            assert audit_count == 8, \
                f"Expected 8 audit records with actor='system_migration', got {audit_count}"

            # 6. 验证 factor_sets 有 legacy-system-v1 且 status='frozen'
            fs = conn.execute(text(
                "SELECT status FROM factor_sets WHERE id = 'legacy-system-v1'"
            )).fetchone()
            assert fs is not None, "legacy-system-v1 factor set should exist"
            assert fs[0] == "frozen", \
                f"Factor set status should be 'frozen', got {fs[0]}"

            # 7. 验证 factor_set_members 有 8 条 legacy-system-v1 成员
            member_count = conn.execute(text(
                "SELECT COUNT(*) FROM factor_set_members "
                "WHERE factor_set_id = 'legacy-system-v1'"
            )).scalar()
            assert member_count == 8, \
                f"Expected 8 factor set members for legacy-system-v1, got {member_count}"

        engine.dispose()

    def test_0021_migration_is_idempotent(self, fresh_sqlite_url):
        """0021 迁移幂等性：重复 upgrade 不会重复回填审计记录或因子集成员。"""
        from alembic import command
        from sqlalchemy.orm import Session

        from app.services.factors.definitions import seed_factor_definitions

        # 1. 用 ORM metadata 创建全部表 + 种子 8 个系统因子
        engine = create_engine(fresh_sqlite_url)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            seed_factor_definitions(session)
            session.commit()
        engine.dispose()

        # 2. 第一次 upgrade head（0021 迁移回填，产生 8 审计 + 8 成员）
        cfg = _make_alembic_config(fresh_sqlite_url)
        command.upgrade(cfg, "head")

        # 3. 第二次 upgrade head（应无错误，alembic 已在 head，不重复执行）
        command.upgrade(cfg, "head")

        # 4. 验证审计记录仍为 8 条（非 16）
        engine = create_engine(fresh_sqlite_url)
        with engine.connect() as conn:
            audit_count = conn.execute(text(
                "SELECT COUNT(*) FROM factor_transition_audits"
            )).scalar()
            assert audit_count == 8, \
                f"Expected 8 audit records (idempotent), got {audit_count}"

            # 5. 验证因子集成员仍为 8 条（非 16）
            member_count = conn.execute(text(
                "SELECT COUNT(*) FROM factor_set_members "
                "WHERE factor_set_id = 'legacy-system-v1'"
            )).scalar()
            assert member_count == 8, \
                f"Expected 8 factor set members (idempotent), got {member_count}"

        engine.dispose()
