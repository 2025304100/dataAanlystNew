from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, literal, text
from sqlalchemy.engine import Engine

# ── 双轨迁移策略说明（WP0.2）─────────────────────────────────
# 本项目数据库 schema 演进由两条轨道并行承担：
#
# 1. Alembic 版本化迁移（项目根目录 `alembic/`）
#    - 用于新表创建、新列添加等可重放、可回滚的版本化变更。
#    - 通过 `alembic revision --autogenerate` 生成修订，
#      `alembic upgrade head` 应用。env.py 从 app.core.config 读取 DB URL。
#
# 2. 启动兼容迁移（显式 `_ensure_*` 补丁 + 通用模型对齐器）
#    - 显式补丁负责需要回填、特殊索引或数据库差异处理的复杂变更。
#    - 通用对齐器自动发现 app.models 下的新模块，并补齐缺表、缺列和普通索引，
#      避免遗漏显式补丁时旧库直接出现 Unknown column。
#    - 删除、改名、改类型、唯一约束等有数据破坏风险的操作仍必须走 Alembic。
#
# 新增表/列优先走 Alembic 修订；运行时兼容补丁继续在此文件维护。
# ────────────────────────────────────────────────────────────

# 合法标识符校验正则：字母或下划线开头，仅含字母、数字、下划线
_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

from app.core.config import settings
from app.db.base import Base
from app.db.manager import DatabaseManager

from app.models import (
    alert, backtest, custom_indicator, daily_bar, discovery, discovery_plan, factor, factor_model, factor_runtime, journal_entry, macro_data, scheduled_task,
    market_event, news_event, portfolio, scan, score, scoring_config, signal_rule,
    sim_account, symbol, trade_setup, watchlist,
    # P2：外部数据因子表
    stock_valuation, capital_flow, etf_indicator, financial_report,
    hot_rank_snapshot, lhb_institution_trade,
    tail_accumulation_snapshot,
    # P2-E：第三方接口管理配置表
    akshare_api_config,
    # 基础数据隔离层：全市场标的元数据 + K线 + 挖掘结果独立存储
    universe, discovery_candidate,
    # P0-8：组合每日净值快照（绩效统计基础数据）
    portfolio_equity_snapshot,
    # P3+：市场指数日线（Benchmark 对比曲线基础设施）
    index_price,
    # WP-S：外部接口运行时状态（熔断器 + 计数器）
    external_endpoint_runtime,
    # WP-P.2：评分快照（挖掘性能改造基础）
    discovery_score_snapshot,
    # WP3.1：机会状态流转审计事件
    opportunity_transition_event,
    # WP4.1：组合成员
    portfolio_member,
    # WP-MSG.1：通知数据模型
    notification,
    # WP-AI.1：AI 会话与审计数据模型
    ai_session,
    # WP-AI.2：AI Profile 多 Profile 主备降级
    ai_profile,
    # WP9.6：API 废弃访问日志
    api_deprecation_log,
    # P2-G：因子治理 5 表（快照/成员/草稿/日质量/评分血缘）
    factor_governance,
)


def _ensure_sqlite_columns(engine, table_name: str, additions: dict[str, str]) -> None:
    """为 SQLite 数据库补充可能缺失的列（兼容旧库升级）。"""
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in columns:
                # 安全说明：table_name/name/ddl 均来自本模块内受信任的硬编码常量
                # （见下方各 _ensure_sqlite_*_columns 调用），非用户输入，无注入风险。
                # 使用双引号包裹标识符以符合 SQLite 规范，进一步加固。
                conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {ddl}'))


def _ensure_universe_incremental_index(engine: Engine) -> None:
    """Ensure existing databases get the covering incremental-sync index."""
    inspector = inspect(engine)
    if "universe_symbols" not in inspector.get_table_names():
        return
    index_name = "ix_universe_incremental_pending"
    existing_indexes = {
        item["name"] for item in inspector.get_indexes("universe_symbols")
    }
    if index_name in existing_indexes:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX ix_universe_incremental_pending "
            "ON universe_symbols "
            "(region, asset_type, is_synced, last_bar_date, last_synced_at, sync_failed)"
        ))


def _ensure_sqlite_scan_result_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "scan_results",
        {
            "warning_days": "INTEGER DEFAULT 3",
            "valid_days": "INTEGER DEFAULT 5",
            "is_frozen": "INTEGER DEFAULT 0",
            # WP-P.7：活动候选展示标志（1=活动展示，0=已隐藏但未删除）
            "is_active": "INTEGER DEFAULT 1",
        },
    )


def _ensure_sqlite_score_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "scores",
        {
            "breakout_score": "REAL",
            "pullback_score": "REAL",
            "overheat_penalty": "REAL",
            # P0：评分配置快照字段
            "scoring_asset_type": "TEXT",
            "scoring_config_id": "INTEGER",
            "scoring_preset_key": "TEXT",
            "scoring_preset_name": "TEXT",
            "scoring_config_version": "INTEGER",
            "scoring_config_snapshot_json": "TEXT",
            "dimension_scores_json": "TEXT",
            "factor_scores_json": "TEXT",
            # 动态因子模型字段
            "weight_mode": "TEXT DEFAULT 'manual'",
            "factor_model_run_id": "TEXT",
            "factor_data_cutoff_at": "DATETIME",
            "factor_quality_score": "REAL",
            "factor_timing_score": "REAL",
            "model_alpha_score": "REAL",
            "macro_regime": "TEXT",
            "macro_position_multiplier": "REAL",
            # WP7-05: Score 解释追溯字段
            "factor_set_id": "TEXT",
            "factor_member_versions_json": "TEXT",
        },
    )


def _ensure_sqlite_factor_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "factors",
        {
            "source_type": "TEXT",
            "frequency": "TEXT",
            "default_missing_policy": "TEXT DEFAULT 'exclude'",
            "is_active": "INTEGER DEFAULT 1",
            # WP1-01: 生命周期与治理字段
            "origin": "TEXT",
            "lifecycle_status": "TEXT",
            "owner": "TEXT",
            "thesis": "TEXT",
            "factor_kind": "TEXT",
            "asset_scope_json": "TEXT",
            "active_version_id": "INTEGER",
            "shadow_version_id": "INTEGER",
            "risk_level": "TEXT",
            "archived_at": "DATETIME",
        },
    )


def _ensure_sqlite_backtest_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        'backtest_runs',
        {
            'score_weight_mode': "TEXT DEFAULT 'manual'",
            'factor_model_run_id': 'TEXT',
            'factor_data_cutoff_at': 'DATETIME',
        },
    )


def _ensure_sqlite_backtest_snapshot_columns(engine) -> None:
    """WP7.2：backtest_runs 表回测快照字段补丁（SQLite，幂等）。

    向后兼容：历史回测的新字段为 NULL，仍可正常读取。
    注：cost_config_json / factor_model_run_id / factor_data_cutoff_at
    已由 _ensure_sqlite_backtest_columns 与模型定义覆盖，此处不重复。
    """
    _ensure_sqlite_columns(
        engine,
        'backtest_runs',
        {
            'member_snapshot_json': 'TEXT',
            'symbol_ids_json': 'TEXT',
            'excluded_members_json': 'TEXT',
            'portfolio_rule_version_id': 'INTEGER',
            'score_mode': 'VARCHAR(32)',
            'data_cutoff_at': 'DATETIME',
            'engine_name': 'VARCHAR(64)',
            'engine_version': 'VARCHAR(32)',
            'source_type': 'VARCHAR(32)',
        },
    )


def _ensure_sqlite_trade_setup_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "trade_setups",
        {
            "manual_overrides_json": "TEXT",
            "field_sources_json": "TEXT",
            "manual_tranche_plan_json": "TEXT",
        },
    )


def _ensure_sqlite_indicator_version_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "custom_indicator_versions",
        {
            "change_note": "TEXT DEFAULT ''",
        },
    )


def _ensure_sqlite_journal_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "journal_entries",
        {
            "review_tags_json": "TEXT",
        },
    )


def _ensure_sqlite_discovery_columns(engine) -> None:
    _ensure_sqlite_columns(
        engine,
        "discovery_tasks",
        {
            "cleanup_count": "INTEGER DEFAULT 0",
        },
    )


def _ensure_sqlite_discovery_task_perf_columns(engine) -> None:
    """WP-P.1：discovery_tasks 性能监控字段补丁。

    与 `_ensure_sqlite_discovery_columns` 共存，不合并。
    所有字段均 nullable，向后兼容旧库；Base.metadata.create_all 不会 ALTER
    已存在表，因此需要在启动时补齐缺失列。
    """
    _ensure_sqlite_columns(
        engine,
        "discovery_tasks",
        {
            "snapshot_id": "INTEGER",
            "snapshot_hit": "INTEGER",
            "stage_durations_json": "TEXT",
            "dirty_symbol_count": "INTEGER",
            "reused_score_count": "INTEGER",
            "rescored_count": "INTEGER",
            "coarse_match_count": "INTEGER",
            "advanced_match_count": "INTEGER",
            "result_rows_written": "INTEGER",
            "cache_key": "TEXT",
            "cache_hit": "INTEGER",
            "degraded_reason": "TEXT",
        },
    )


def _ensure_sqlite_portfolio_columns(engine) -> None:
    """P2-3：组合自动交易开关字段（旧库升级补丁）。"""
    _ensure_sqlite_columns(
        engine,
        "portfolios",
        {
            "auto_trade_enabled": "INTEGER DEFAULT 0",
            "auto_trade_last_run_at": "DATETIME",
        },
    )


def _ensure_sqlite_scan_run_cache_columns(engine) -> None:
    """WP-P.6：scan_runs 表扫描缓存与摘要统计字段补丁。

    所有字段均 nullable，向后兼容旧库；Base.metadata.create_all 不会 ALTER
    已存在表，因此需要在启动时补齐缺失列。

    字段说明：
    - snapshot_id / cache_key：缓存命中查找键
    - cache_hit / total_in_snapshot / coarse_match_count /
      advanced_match_count / result_rows_written：摘要统计
    - degraded_reason：降级原因（与 run_fast_scan 返回值一致）
    """
    _ensure_sqlite_columns(
        engine,
        "scan_runs",
        {
            "snapshot_id": "INTEGER",
            "cache_key": "TEXT",
            "cache_hit": "INTEGER",
            "total_in_snapshot": "INTEGER",
            "coarse_match_count": "INTEGER",
            "advanced_match_count": "INTEGER",
            "result_rows_written": "INTEGER",
            "degraded_reason": "TEXT",
        },
    )


def _ensure_sqlite_watchlist_item_score_snapshot_column(engine) -> None:
    """WP-P.7：watchlist_items.score_snapshot_json 字段补丁。

    最小化补丁：仅添加 score_snapshot_json 一列（用于候选晋升为观察项时复制入选评分）。
    WP2.1 将扩展完整的 origin_type / status / priority 等字段集。
    """
    _ensure_sqlite_columns(
        engine,
        "watchlist_items",
        {
            "score_snapshot_json": "TEXT",
        },
    )


def _ensure_sqlite_watchlist_item_columns(engine) -> None:
    """WP2.1：watchlist_items 表正式观察池扩展字段补丁（SQLite）。

    幂等：列已存在时跳过，不报错（由 _ensure_sqlite_columns 内部判断）。
    注意：score_snapshot_json 由 WP-P.7 的
    _ensure_sqlite_watchlist_item_score_snapshot_column 处理，此处不重复。
    """
    _ensure_sqlite_columns(
        engine,
        "watchlist_items",
        {
            "origin_type": "VARCHAR(32) NOT NULL DEFAULT 'manual'",
            "origin_id": "INTEGER",
            "reason_json": "TEXT",
            "status": "VARCHAR(16) NOT NULL DEFAULT 'watching'",
            "priority": "INTEGER NOT NULL DEFAULT 0",
            "tags_json": "TEXT",
            "target_portfolio_id": "INTEGER",
            "updated_at": "DATETIME",
            "archived_at": "DATETIME",
        },
    )


def _ensure_mysql_watchlist_item_columns(engine) -> None:
    """WP2.1：watchlist_items 表正式观察池扩展字段补丁（MySQL）。

    幂等：通过 information_schema.COLUMNS 检查列是否存在；
    索引通过 information_schema.STATISTICS 检查是否存在。
    注意：score_snapshot_json 由 WP-P.7 处理，此处不重复。
    """
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return

        new_cols = [
            ("origin_type", "VARCHAR(32) NOT NULL DEFAULT 'manual'"),
            ("origin_id", "INTEGER NULL"),
            ("reason_json", "TEXT NULL"),
            ("status", "VARCHAR(16) NOT NULL DEFAULT 'watching'"),
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ("tags_json", "TEXT NULL"),
            ("target_portfolio_id", "INTEGER NULL"),
            ("updated_at", "DATETIME NULL"),
            ("archived_at", "DATETIME NULL"),
        ]
        for col_name, col_ddl in new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'watchlist_items' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(
                        f"ALTER TABLE watchlist_items ADD COLUMN {col_name} {col_ddl}"
                    ))
                    logger.info("Added %s column to watchlist_items", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to watchlist_items: %s", col_name, e)

        # 索引幂等创建（通过 information_schema.STATISTICS 检查）
        new_indexes = [
            ("idx_watchlist_items_origin_id", "origin_id"),
            ("idx_watchlist_items_status", "status"),
            ("idx_watchlist_items_target_portfolio_id", "target_portfolio_id"),
        ]
        for idx_name, col_name in new_indexes:
            result = conn.execute(text(
                "SELECT INDEX_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'watchlist_items' AND INDEX_NAME = :idx"
            ), {"db": db_name, "idx": idx_name})
            if result.first() is None:
                try:
                    conn.execute(text(
                        f"CREATE INDEX {idx_name} ON watchlist_items ({col_name})"
                    ))
                    logger.info("Created index %s on watchlist_items", idx_name)
                except Exception as e:
                    logger.warning("Failed to create index %s on watchlist_items: %s", idx_name, e)


def _migrate_legacy_watchlist_items_origin_type(engine) -> None:
    """WP2.1：将历史 watchlist_items 标记为 legacy_manual_unknown（幂等）。

    旧记录（origin_type 默认为 'manual' 且无 origin_id）的来源无法追溯，
    统一标记为 'legacy_manual_unknown' 以区别于 WP2.2+ 通过 observations 服务
    显式创建的 manual 记录。

    幂等：仅更新 origin_type='manual' AND origin_id IS NULL 的记录；
    已迁移或新创建的 manual+origin_id 记录不受影响。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if not inspector.has_table("watchlist_items"):
        return

    with engine.begin() as conn:
        result = conn.execute(text(
            "SELECT COUNT(*) FROM watchlist_items "
            "WHERE origin_type = 'manual' AND origin_id IS NULL"
        ))
        count = result.scalar() or 0
        if count > 0:
            conn.execute(text(
                "UPDATE watchlist_items SET origin_type = 'legacy_manual_unknown' "
                "WHERE origin_type = 'manual' AND origin_id IS NULL"
            ))
            logger.info("已将 %d 条历史 watchlist_items 标记为 legacy_manual_unknown", count)


def _ensure_core_watchlist_exists(engine) -> None:
    """WP2.6: 确保 core 名单存在（幂等）。

    spec Scenario "现有 core 名单保留" 要求：迁移执行时 core 观察池原样保留。
    本函数确保新部署的环境也有 core 名单；如果已存在则不重复创建。

    幂等：多次调用只创建一次。不创建 6 个观察项（由 _ensure_core_watchlist_seed_items 负责）。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if not inspector.has_table("watchlists"):
        return

    with engine.begin() as conn:
        result = conn.execute(text("SELECT id FROM watchlists WHERE name = 'core' LIMIT 1"))
        existing = result.scalar()
        if existing is not None:
            return  # 已存在，不重复创建

        # 创建 core 名单
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        conn.execute(text(
            "INSERT INTO watchlists (name, list_type, description, created_at, updated_at) "
            "VALUES ('core', 'observation', '默认核心观察名单（系统保留）', :now, :now)"
        ), {"now": now})
        logger.info("WP2.6: 已创建 core 名单")


def _ensure_core_watchlist_seed_items(engine) -> None:
    """WP2.6: 确保 core 名单有 6 个种子观察项（幂等，best-effort）。

    spec Scenario "现有 core 名单保留" 要求：6 个观察项原样保留。
    本函数确保新部署的环境也有 6 个种子观察项（从 symbols 表取前 6 个）。
    历史无来源项标记为 legacy_manual_unknown（禁止伪造来源）。
    生产环境已有的 6 个观察项不会被破坏（幂等：已有 6 项则跳过）。

    best-effort：symbols 表行数不足 6 或 core 名单不存在时跳过，不报错。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if not inspector.has_table("watchlists") or not inspector.has_table("watchlist_items") \
            or not inspector.has_table("symbols"):
        return

    with engine.begin() as conn:
        # 查找 core 名单 ID
        result = conn.execute(text("SELECT id FROM watchlists WHERE name = 'core' LIMIT 1"))
        core_id = result.scalar()
        if core_id is None:
            return  # core 名单不存在

        # 检查 core 名单是否已有 6 项
        result = conn.execute(text(
            "SELECT COUNT(*) FROM watchlist_items WHERE watchlist_id = :wid"
        ), {"wid": core_id})
        count = result.scalar() or 0
        if count >= 6:
            return  # 已有 6 项，不重复创建

        # 查找前 6 个 symbols
        symbols = conn.execute(text("SELECT id FROM symbols ORDER BY id LIMIT 6")).fetchall()
        if len(symbols) < 6:
            return  # symbols 不足 6 个，跳过（不报错）

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for sym_row in symbols:
            sym_id = sym_row[0]
            # 检查是否已存在（unique 约束：watchlist_id + symbol_id）
            existing = conn.execute(text(
                "SELECT id FROM watchlist_items WHERE watchlist_id = :wid AND symbol_id = :sid"
            ), {"wid": core_id, "sid": sym_id}).scalar()
            if existing is not None:
                continue

            conn.execute(text(
                "INSERT INTO watchlist_items "
                "(watchlist_id, symbol_id, origin_type, status, priority, added_at) "
                "VALUES (:wid, :sid, 'legacy_manual_unknown', 'watching', 0, :now)"
            ), {"wid": core_id, "sid": sym_id, "now": now})
        logger.info("WP2.6: 已为 core 名单添加种子观察项")


def _ensure_sqlite_opportunity_transition_events_table(engine) -> None:
    """WP3.1: 确保 opportunity_transition_events 表存在（SQLite）。

    幂等：表已存在时跳过，不报错。
    通常 Base.metadata.create_all 已创建该表，本补丁用于防御性检查
    以及处理表已存在但缺列等边界情况（为后续 WP3.1.2 追加字段预留）。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if inspector.has_table("opportunity_transition_events"):
        return  # 表已存在

    # 通过模型 metadata 创建表（checkfirst=True 保证幂等）
    from app.models.opportunity_transition_event import OpportunityTransitionEvent
    OpportunityTransitionEvent.__table__.create(engine, checkfirst=True)
    logger.info("WP3.1: 已创建 opportunity_transition_events 表")


def _ensure_mysql_opportunity_transition_events_table(engine) -> None:
    """WP3.1: 确保 opportunity_transition_events 表存在（MySQL）。

    幂等：表已存在时跳过，不报错。
    通常 Base.metadata.create_all 已创建该表，本补丁用于防御性检查
    以及处理表已存在但缺列等边界情况（为后续 WP3.1.2 追加字段预留）。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if inspector.has_table("opportunity_transition_events"):
        return

    from app.models.opportunity_transition_event import OpportunityTransitionEvent
    OpportunityTransitionEvent.__table__.create(engine, checkfirst=True)
    logger.info("WP3.1: 已创建 opportunity_transition_events 表")


def _ensure_sqlite_portfolio_members_table(engine) -> None:
    """WP4.1: 确保 portfolio_members 表存在（SQLite）。

    幂等：表已存在时检查部分唯一索引是否需要补；表不存在时创建。
    包含部分唯一索引：同一组合同一标的只能存在一条当前有效成员关系。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if inspector.has_table("portfolio_members"):
        # 表已存在，检查部分唯一索引是否需要补
        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes("portfolio_members")
        }
        if "idx_portfolio_members_active" not in existing_indexes:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_members_active "
                    "ON portfolio_members (portfolio_id, symbol_id) "
                    "WHERE effective_to IS NULL"
                ))
                logger.info("WP4.1: 已创建 idx_portfolio_members_active 部分唯一索引")
        return

    # 表不存在，通过模型 metadata 创建（checkfirst=True 保证幂等）
    from app.models.portfolio_member import PortfolioMember
    PortfolioMember.__table__.create(engine, checkfirst=True)
    logger.info("WP4.1: 已创建 portfolio_members 表")


def _ensure_mysql_portfolio_members_table(engine) -> None:
    """WP4.1: 确保 portfolio_members 表存在（MySQL）。

    幂等：表已存在时跳过，表不存在时创建。
    MySQL 不支持 partial unique index，通过模型 metadata.create_all 创建
    普通索引；同一组合同一标的只能存在一条当前有效成员关系由应用层
    在创建/恢复成员时检查 effective_to IS NULL。
    可选方案（暂未启用）：
        ALTER TABLE portfolio_members
            ADD COLUMN is_active_generated INT AS (IF(effective_to IS NULL, 1, NULL)) STORED;
        CREATE UNIQUE INDEX idx_portfolio_members_active
            ON portfolio_members (portfolio_id, symbol_id, is_active_generated);
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if inspector.has_table("portfolio_members"):
        return

    from app.models.portfolio_member import PortfolioMember
    PortfolioMember.__table__.create(engine, checkfirst=True)
    logger.info("WP4.1: 已创建 portfolio_members 表")


def _ensure_sqlite_notification_tables(engine) -> None:
    """WP-MSG.1: 确保 notification_* 表存在（SQLite，幂等）。

    幂等：表已存在时跳过，表不存在时创建。
    部分唯一索引（event_key + channel_id，status != 'sent'）通过
    _ensure_sqlite_notification_outbox_unique_index 单独补建，
    以处理表已存在但缺索引的旧库场景。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)

    # 延迟导入避免循环
    from app.models.notification import (
        NotificationChannel,
        NotificationDelivery,
        NotificationOutbox,
        NotificationPolicy,
        NotificationPolicyChannel,
        NotificationTemplate,
    )

    models_map = {
        "notification_channels": NotificationChannel,
        "notification_policies": NotificationPolicy,
        "notification_policy_channels": NotificationPolicyChannel,
        "notification_outbox": NotificationOutbox,
        "notification_deliveries": NotificationDelivery,
        "notification_templates": NotificationTemplate,
    }

    for table_name, model_cls in models_map.items():
        if not inspector.has_table(table_name):
            model_cls.__table__.create(engine, checkfirst=True)
            logger.info("WP-MSG.1: 已创建 %s 表", table_name)

    # 部分唯一索引：event_key + channel_id（仅 status != 'sent' 状态）
    _ensure_sqlite_notification_outbox_unique_index(engine)


def _ensure_sqlite_notification_outbox_unique_index(engine) -> None:
    """WP-MSG.1: 为 notification_outbox 创建部分唯一索引（幂等）。

    部分唯一索引：仅对 status != 'sent' 的记录生效，确保同一业务事件
    重放时不向同一渠道重复发送；已发送（sent）记录不参与唯一约束，
    允许历史记录与新 pending 记录共存（如手动重发场景）。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if not inspector.has_table("notification_outbox"):
        return

    existing_indexes = {
        idx["name"] for idx in inspector.get_indexes("notification_outbox")
    }
    if "idx_no_event_channel_pending" not in existing_indexes:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_no_event_channel_pending "
                "ON notification_outbox (event_key, channel_id) "
                "WHERE status != 'sent'"
            ))
            logger.info("WP-MSG.1: 已创建 idx_no_event_channel_pending 部分唯一索引")


def _ensure_mysql_notification_tables(engine) -> None:
    """WP-MSG.1: 确保 notification_* 表存在（MySQL，幂等）。

    幂等：表已存在时跳过，表不存在时创建。
    MySQL 不支持 partial unique index（WHERE 子句），第一阶段通过模型
    metadata.create_all 创建普通唯一索引（event_key + channel_id 全状态唯一）；
    同业务事件重放防重由应用层在写 Outbox 前检查 status != 'sent' 实现。
    后续可考虑使用 MySQL 8.0+ 生成列 + 唯一索引模拟部分唯一索引。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)

    from app.models.notification import (
        NotificationChannel,
        NotificationDelivery,
        NotificationOutbox,
        NotificationPolicy,
        NotificationPolicyChannel,
        NotificationTemplate,
    )

    models_map = {
        "notification_channels": NotificationChannel,
        "notification_policies": NotificationPolicy,
        "notification_policy_channels": NotificationPolicyChannel,
        "notification_outbox": NotificationOutbox,
        "notification_deliveries": NotificationDelivery,
        "notification_templates": NotificationTemplate,
    }

    for table_name, model_cls in models_map.items():
        if not inspector.has_table(table_name):
            model_cls.__table__.create(engine, checkfirst=True)
            logger.info("WP-MSG.1: 已创建 %s 表", table_name)


def _ensure_sqlite_sim_orders_attribution_columns(engine: Engine) -> None:
    """WP6.1: 确保 sim_orders 表有归因字段（SQLite，幂等）。

    向后兼容：历史订单的新字段为 NULL。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if not inspector.has_table("sim_orders"):
        return

    existing_columns = {col["name"] for col in inspector.get_columns("sim_orders")}

    new_columns = [
        ("member_id", "INTEGER"),
        ("source_type", "VARCHAR(32)"),
        ("source_id", "INTEGER"),
        ("signal_id", "INTEGER"),
        ("signal_snapshot_json", "TEXT"),
        ("rule_version_id", "INTEGER"),
        ("execution_mode", "VARCHAR(16)"),
        ("client_order_key", "VARCHAR(128)"),
        ("decision_snapshot_json", "TEXT"),
        ("rejection_code", "VARCHAR(64)"),
        ("rejection_detail", "VARCHAR(500)"),
    ]

    with engine.begin() as conn:
        for col_name, col_type in new_columns:
            if col_name not in existing_columns:
                conn.execute(text(f"ALTER TABLE sim_orders ADD COLUMN {col_name} {col_type}"))
                logger.info("已添加 sim_orders.%s 列", col_name)

    # 补建索引（幂等）
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("sim_orders")}

    indexes_to_create = [
        ("idx_sim_orders_member", "member_id"),
        ("idx_sim_orders_source", "source_type, source_id"),
        ("idx_sim_orders_signal", "signal_id"),
    ]

    with engine.begin() as conn:
        for idx_name, idx_cols in indexes_to_create:
            if idx_name not in existing_indexes:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON sim_orders ({idx_cols})"))
                logger.info("已创建索引 %s", idx_name)

    # client_order_key 唯一索引（部分唯一索引，允许 NULL 并存）
    if "idx_sim_orders_client_key" not in existing_indexes:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sim_orders_client_key "
                "ON sim_orders (client_order_key) WHERE client_order_key IS NOT NULL"
            ))
            logger.info("已创建 idx_sim_orders_client_key 唯一索引")


def _ensure_mysql_sim_orders_attribution_columns(engine: Engine) -> None:
    """WP6.1: 确保 sim_orders 表有归因字段（MySQL，幂等）。"""
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if not inspector.has_table("sim_orders"):
        return

    existing_columns = {col["name"] for col in inspector.get_columns("sim_orders")}

    new_columns = [
        ("member_id", "INT NULL"),
        ("source_type", "VARCHAR(32) NULL"),
        ("source_id", "INT NULL"),
        ("signal_id", "INT NULL"),
        ("signal_snapshot_json", "TEXT NULL"),
        ("rule_version_id", "INT NULL"),
        ("execution_mode", "VARCHAR(16) NULL"),
        ("client_order_key", "VARCHAR(128) NULL"),
        ("decision_snapshot_json", "TEXT NULL"),
        ("rejection_code", "VARCHAR(64) NULL"),
        ("rejection_detail", "VARCHAR(500) NULL"),
    ]

    with engine.begin() as conn:
        for col_name, col_type in new_columns:
            if col_name not in existing_columns:
                conn.execute(text(f"ALTER TABLE sim_orders ADD COLUMN {col_name} {col_type}"))
                logger.info("已添加 sim_orders.%s 列", col_name)

    # 索引（MySQL）
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("sim_orders")}

    indexes_to_create = [
        ("idx_sim_orders_member", "member_id"),
        ("idx_sim_orders_source", "source_type, source_id"),
        ("idx_sim_orders_signal", "signal_id"),
    ]

    with engine.begin() as conn:
        for idx_name, idx_cols in indexes_to_create:
            if idx_name not in existing_indexes:
                conn.execute(text(f"CREATE INDEX {idx_name} ON sim_orders ({idx_cols})"))
                logger.info("已创建索引 %s", idx_name)

    # client_order_key 唯一索引（MySQL 不支持 WHERE 部分索引，使用普通唯一索引）
    if "idx_sim_orders_client_key" not in existing_indexes:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX idx_sim_orders_client_key ON sim_orders (client_order_key)"
            ))
            logger.info("已创建 idx_sim_orders_client_key 唯一索引")


def _ensure_sqlite_ai_session_tables(engine: Engine) -> None:
    """WP-AI.1: 确保 ai_sessions / ai_messages / ai_action_audits 三张表存在（SQLite，幂等）。

    幂等：表已存在时跳过，表不存在时创建。
    Base.metadata.create_all 已创建表，本补丁用于防御性检查，
    保证旧库升级或异常场景下也能补齐 schema。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)

    from app.models.ai_session import AIActionAudit, AIMessage, AISession

    models_map = {
        "ai_sessions": AISession,
        "ai_messages": AIMessage,
        "ai_action_audits": AIActionAudit,
    }

    for table_name, model_cls in models_map.items():
        if not inspector.has_table(table_name):
            model_cls.__table__.create(engine, checkfirst=True)
            logger.info("WP-AI.1: 已创建 %s 表", table_name)


def _ensure_mysql_ai_session_tables(engine: Engine) -> None:
    """WP-AI.1: 确保 ai_sessions / ai_messages / ai_action_audits 三张表存在（MySQL，幂等）。

    幂等：通过 information_schema.TABLES 检查表是否存在，不存在则创建。
    Base.metadata.create_all 已创建表，本补丁用于防御性检查，
    保证旧库升级或异常场景下也能补齐 schema。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)

    from app.models.ai_session import AIActionAudit, AIMessage, AISession

    models_map = {
        "ai_sessions": AISession,
        "ai_messages": AIMessage,
        "ai_action_audits": AIActionAudit,
    }

    for table_name, model_cls in models_map.items():
        if not inspector.has_table(table_name):
            model_cls.__table__.create(engine, checkfirst=True)
            logger.info("WP-AI.1: 已创建 %s 表", table_name)


def _ensure_sqlite_portfolio_reviews_table(engine) -> None:
    """WP8: 确保 portfolio_reviews 表存在（SQLite，幂等）。

    幂等：表已存在时跳过，表不存在时创建。
    通常 Base.metadata.create_all 已创建该表，本补丁用于防御性检查。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if inspector.has_table("portfolio_reviews"):
        return

    from app.models.review import Review
    Review.__table__.create(engine, checkfirst=True)
    logger.info("WP8: 已创建 portfolio_reviews 表")


def _ensure_mysql_portfolio_reviews_table(engine) -> None:
    """WP8: 确保 portfolio_reviews 表存在（MySQL，幂等）。"""
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if inspector.has_table("portfolio_reviews"):
        return

    from app.models.review import Review
    Review.__table__.create(engine, checkfirst=True)
    logger.info("WP8: 已创建 portfolio_reviews 表")


def _ensure_sqlite_ai_profiles_table(engine) -> None:
    """WP-AI.2: 确保 ai_profiles 表存在（SQLite，幂等）。

    幂等：表已存在时跳过，表不存在时创建。
    通常 Base.metadata.create_all 已创建该表，本补丁用于防御性检查，
    以及处理表已存在但缺列等边界情况。
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if not inspector.has_table("ai_profiles"):
        from app.models.ai_profile import AIProfile
        AIProfile.__table__.create(engine, checkfirst=True)
        logger.info("WP-AI.2: 已创建 ai_profiles 表")
        return

    # 表已存在，补齐可能缺失的列（兼容旧库升级）
    _ensure_sqlite_columns(
        engine,
        "ai_profiles",
        {
            "name": "VARCHAR(64) NOT NULL",
            "provider": "VARCHAR(64) NOT NULL",
            "base_url": "VARCHAR(256)",
            "model": "VARCHAR(128) NOT NULL",
            "auth_type": "VARCHAR(32)",
            "secret_key_ref": "VARCHAR(128)",
            "timeout_seconds": "INTEGER DEFAULT 30",
            "max_tokens": "INTEGER DEFAULT 4096",
            "max_context_tokens": "INTEGER DEFAULT 8192",
            "daily_request_limit": "INTEGER DEFAULT 100",
            "max_concurrent": "INTEGER DEFAULT 3",
            "purpose": "VARCHAR(64) NOT NULL DEFAULT 'all'",
            "priority": "INTEGER DEFAULT 0",
            "is_enabled": "INTEGER DEFAULT 1",
            "is_fallback": "INTEGER DEFAULT 0",
            "health_status": "VARCHAR(16) DEFAULT 'unknown'",
            "last_health_check": "DATETIME",
            "daily_request_count": "INTEGER DEFAULT 0",
            "daily_request_reset_at": "DATETIME",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
    )


def _ensure_mysql_ai_profiles_table(engine) -> None:
    """WP-AI.2: 确保 ai_profiles 表存在（MySQL，幂等）。"""
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(engine)
    if not inspector.has_table("ai_profiles"):
        from app.models.ai_profile import AIProfile
        AIProfile.__table__.create(engine, checkfirst=True)
        logger.info("WP-AI.2: 已创建 ai_profiles 表")
        return

    # 表已存在，补齐可能缺失的列（兼容旧库升级）
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return
        new_cols = [
            ("name", "VARCHAR(64) NOT NULL"),
            ("provider", "VARCHAR(64) NOT NULL"),
            ("base_url", "VARCHAR(256) NULL"),
            ("model", "VARCHAR(128) NOT NULL"),
            ("auth_type", "VARCHAR(32) NULL"),
            ("secret_key_ref", "VARCHAR(128) NULL"),
            ("timeout_seconds", "INTEGER NOT NULL DEFAULT 30"),
            ("max_tokens", "INTEGER NOT NULL DEFAULT 4096"),
            ("max_context_tokens", "INTEGER NOT NULL DEFAULT 8192"),
            ("daily_request_limit", "INTEGER NOT NULL DEFAULT 100"),
            ("max_concurrent", "INTEGER NOT NULL DEFAULT 3"),
            ("purpose", "VARCHAR(64) NOT NULL DEFAULT 'all'"),
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ("is_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
            ("is_fallback", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("health_status", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
            ("last_health_check", "DATETIME NULL"),
            ("daily_request_count", "INTEGER NOT NULL DEFAULT 0"),
            ("daily_request_reset_at", "DATETIME NULL"),
            ("created_at", "DATETIME NOT NULL"),
            ("updated_at", "DATETIME NULL"),
        ]
        for col_name, col_ddl in new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'ai_profiles' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(
                        f"ALTER TABLE ai_profiles ADD COLUMN {col_name} {col_ddl}"
                    ))
                    logger.info("Added %s column to ai_profiles", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to ai_profiles: %s", col_name, e)


def _migrate_ai_config_json_to_profiles() -> None:
    """WP-AI.2: 将 ai_config.json 兼容迁移为 AIProfile 记录（幂等）。

    幂等：原文件已标记 migrated=true 或同名 Profile 已存在时跳过。
    原文件不删除，仅添加 migrated=true 标记。
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        from app.db.session import SessionLocal
        from app.services.ai_profile_migration import migrate_ai_config_json
        with SessionLocal() as db:
            count = migrate_ai_config_json(db)
            if count > 0:
                db.commit()
                logger.info("WP-AI.2: migrated %d AI profile(s) from ai_config.json", count)
    except Exception as e:
        logger.warning("WP-AI.2: ai_config.json migration failed: %s", e)


def _ensure_sqlite_external_endpoint_runtime_columns(engine) -> None:
    """WP-S：external_endpoint_runtime 表旧库升级补丁。

    该表为 WP-S 新增表，Base.metadata.create_all 会自动创建。
    本补丁只用于已存在但缺列的旧库（如 alembic 迁移未应用但表已手动建好的情况），
    确保 SQLite 与 MySQL 双库兼容。
    """
    _ensure_sqlite_columns(
        engine,
        "external_endpoint_runtime",
        {
            "host": "TEXT",
            "state": "TEXT DEFAULT 'closed'",
            "consecutive_failures": "INTEGER DEFAULT 0",
            "cooldown_until": "DATETIME",
            "last_error_code": "TEXT",
            "last_error_at": "DATETIME",
            "request_count": "INTEGER DEFAULT 0",
            "cache_hit_count": "INTEGER DEFAULT 0",
            "fallback_count": "INTEGER DEFAULT 0",
        },
    )


def _ensure_sqlite_async_task_columns(engine) -> None:
    """WP-S.5：async_tasks 表任务防卡死状态机字段补丁。

    所有字段均 nullable，向后兼容旧库；Base.metadata.create_all 不会 ALTER
    已存在表，因此需要在启动时补齐缺失列。
    """
    _ensure_sqlite_columns(
        engine,
        "async_tasks",
        {
            "heartbeat_at": "DATETIME",
            "stage_budget_seconds": "INTEGER",
            "stage_started_at": "DATETIME",
            "last_progress_at": "DATETIME",
            "last_progress_percent": "REAL",
            "current_step_description": "TEXT",
            "suggested_action": "TEXT",
            "batch_recovery_json": "TEXT",
            "last_patrol_at": "DATETIME",
            "worker_thread_id": "TEXT",
            "cancel_requested": "INTEGER DEFAULT 0",
        },
    )


def _ensure_mysql_indicator_version_columns(engine) -> None:
    """为 MySQL 中已存在的表补充新增列。"""
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return
        # custom_indicator_versions.change_note
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'custom_indicator_versions' AND COLUMN_NAME = 'change_note'"
        ), {"db": db_name})
        if result.first() is None:
            try:
                conn.execute(text(
                    "ALTER TABLE custom_indicator_versions ADD COLUMN change_note TEXT DEFAULT ''"
                ))
                logger.info("Added change_note column to custom_indicator_versions")
            except Exception as e:
                logger.warning("Failed to add change_note column: %s", e)
        # journal_entries.review_tags_json
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'journal_entries' AND COLUMN_NAME = 'review_tags_json'"
        ), {"db": db_name})
        if result.first() is None:
            try:
                conn.execute(text(
                    "ALTER TABLE journal_entries ADD COLUMN review_tags_json TEXT"
                ))
                logger.info("Added review_tags_json column to journal_entries")
            except Exception as e:
                logger.warning("Failed to add review_tags_json column: %s", e)
        # discovery_tasks.cleanup_count
        result = conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'discovery_tasks' AND COLUMN_NAME = 'cleanup_count'"
        ), {"db": db_name})
        if result.first() is None:
            try:
                conn.execute(text(
                    "ALTER TABLE discovery_tasks ADD COLUMN cleanup_count INTEGER DEFAULT 0"
                ))
                logger.info("Added cleanup_count column to discovery_tasks")
            except Exception as e:
                logger.warning("Failed to add cleanup_count column: %s", e)
        # P0：scores 表评分配置快照字段
        score_new_cols = [
            ("scoring_asset_type", "VARCHAR(16)"),
            ("scoring_config_id", "INTEGER"),
            ("scoring_preset_key", "VARCHAR(64)"),
            ("scoring_preset_name", "VARCHAR(128)"),
            ("scoring_config_version", "INTEGER"),
            ("scoring_config_snapshot_json", "TEXT"),
            ("dimension_scores_json", "TEXT"),
            ("factor_scores_json", "TEXT"),
            ("weight_mode", "VARCHAR(16) DEFAULT 'manual'"),
            ("factor_model_run_id", "VARCHAR(64)"),
            ("factor_data_cutoff_at", "DATETIME"),
            ("factor_quality_score", "DOUBLE"),
            ("factor_timing_score", "DOUBLE"),
            ("model_alpha_score", "DOUBLE"),
            ("macro_regime", "VARCHAR(24)"),
            ("macro_position_multiplier", "DOUBLE"),
            # WP7-05: Score 解释追溯字段
            ("factor_set_id", "VARCHAR(64)"),
            ("factor_member_versions_json", "TEXT"),
        ]
        for col_name, col_ddl in score_new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'scores' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(f"ALTER TABLE scores ADD COLUMN {col_name} {col_ddl}"))
                    logger.info("Added %s column to scores", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to scores: %s", col_name, e)

        backtest_new_cols = [
            ('score_weight_mode', "VARCHAR(16) DEFAULT 'manual'"),
            ('factor_model_run_id', 'VARCHAR(64)'),
            ('factor_data_cutoff_at', 'DATETIME'),
        ]
        for col_name, col_ddl in backtest_new_cols:
            result = conn.execute(text(
                'SELECT COLUMN_NAME FROM information_schema.COLUMNS '
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'backtest_runs' "
                'AND COLUMN_NAME = :col'
            ), {'db': db_name, 'col': col_name})
            if result.first() is None:
                try:
                    conn.execute(text(
                        f'ALTER TABLE backtest_runs ADD COLUMN '
                        f'{col_name} {col_ddl}'
                    ))
                    logger.info(
                        'Added backtest_runs.%s column', col_name
                    )
                except Exception as exc:
                    logger.warning(
                        'Failed to add backtest_runs.%s: %s',
                        col_name,
                        exc,
                    )

        factor_new_cols = [
            ("source_type", "VARCHAR(32)"),
            ("frequency", "VARCHAR(16)"),
            ("default_missing_policy", "VARCHAR(32) DEFAULT 'exclude'"),
            ("is_active", "INTEGER DEFAULT 1"),
            # WP1-01: 生命周期与治理字段
            ("origin", "VARCHAR(32)"),
            ("lifecycle_status", "VARCHAR(32)"),
            ("owner", "VARCHAR(128)"),
            ("thesis", "TEXT"),
            ("factor_kind", "VARCHAR(32)"),
            ("asset_scope_json", "TEXT"),
            ("active_version_id", "INTEGER"),
            ("shadow_version_id", "INTEGER"),
            ("risk_level", "VARCHAR(16)"),
            ("archived_at", "DATETIME"),
        ]
        for col_name, col_ddl in factor_new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'factors' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(f"ALTER TABLE factors ADD COLUMN {col_name} {col_ddl}"))
                    logger.info("Added %s column to factors", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to factors: %s", col_name, e)

        # P2-3：组合自动交易开关字段
        portfolio_new_cols = [
            ("auto_trade_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("auto_trade_last_run_at", "DATETIME NULL"),
        ]
        for col_name, col_ddl in portfolio_new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'portfolios' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(f"ALTER TABLE portfolios ADD COLUMN {col_name} {col_ddl}"))
                    logger.info("Added %s column to portfolios", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to portfolios: %s", col_name, e)

        # WP-S：external_endpoint_runtime 表新增列补丁
        # 该表本身由 Base.metadata.create_all 自动创建；此补丁保证旧库
        # （表已存在但缺列）也能补齐 schema，与 SQLite 路径对称
        endpoint_new_cols = [
            ("host", "VARCHAR(128) NULL"),
            ("state", "VARCHAR(16) NOT NULL DEFAULT 'closed'"),
            ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
            ("cooldown_until", "DATETIME NULL"),
            ("last_error_code", "VARCHAR(64) NULL"),
            ("last_error_at", "DATETIME NULL"),
            ("request_count", "INTEGER NOT NULL DEFAULT 0"),
            ("cache_hit_count", "INTEGER NOT NULL DEFAULT 0"),
            ("fallback_count", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col_name, col_ddl in endpoint_new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'external_endpoint_runtime' "
                "AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(
                        text(f"ALTER TABLE external_endpoint_runtime ADD COLUMN {col_name} {col_ddl}")
                    )
                    logger.info("Added %s column to external_endpoint_runtime", col_name)
                except Exception as e:
                    logger.warning(
                        "Failed to add %s column to external_endpoint_runtime: %s",
                        col_name, e,
                    )

        # WP-S.5：async_tasks 表任务防卡死状态机新增列补丁
        # 所有字段 nullable，保证旧库升级幂等
        async_task_new_cols = [
            ("heartbeat_at", "DATETIME NULL"),
            ("stage_budget_seconds", "INTEGER NULL"),
            ("stage_started_at", "DATETIME NULL"),
            ("last_progress_at", "DATETIME NULL"),
            ("last_progress_percent", "DOUBLE NULL"),
            ("current_step_description", "TEXT NULL"),
            ("suggested_action", "TEXT NULL"),
            ("batch_recovery_json", "TEXT NULL"),
            ("last_patrol_at", "DATETIME NULL"),
            ("worker_thread_id", "VARCHAR(32) NULL"),
            ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col_name, col_ddl in async_task_new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'async_tasks' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(f"ALTER TABLE async_tasks ADD COLUMN {col_name} {col_ddl}"))
                    logger.info("Added %s column to async_tasks", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to async_tasks: %s", col_name, e)

        # WP-P.1：discovery_tasks 性能监控字段补丁
        # 与 SQLite 路径对称；所有字段 nullable，保证旧库升级幂等
        discovery_task_perf_cols = [
            ("snapshot_id", "INTEGER NULL"),
            ("snapshot_hit", "INTEGER NULL"),
            ("stage_durations_json", "TEXT NULL"),
            ("dirty_symbol_count", "INTEGER NULL"),
            ("reused_score_count", "INTEGER NULL"),
            ("rescored_count", "INTEGER NULL"),
            ("coarse_match_count", "INTEGER NULL"),
            ("advanced_match_count", "INTEGER NULL"),
            ("result_rows_written", "INTEGER NULL"),
            ("cache_key", "VARCHAR(128) NULL"),
            ("cache_hit", "INTEGER NULL"),
            ("degraded_reason", "VARCHAR(64) NULL"),
        ]
        for col_name, col_ddl in discovery_task_perf_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'discovery_tasks' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(f"ALTER TABLE discovery_tasks ADD COLUMN {col_name} {col_ddl}"))
                    logger.info("Added %s column to discovery_tasks", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to discovery_tasks: %s", col_name, e)


def _ensure_mysql_scan_result_columns(engine) -> None:
    """scan_results 表字段补丁（MySQL 路径）。

    与 _ensure_sqlite_scan_result_columns 对称；所有字段 nullable，
    保证旧库升级幂等。
    """
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return
        scan_result_new_cols = [
            ("warning_days", "INTEGER DEFAULT 3"),
            ("valid_days", "INTEGER DEFAULT 5"),
            ("is_frozen", "INTEGER DEFAULT 0"),
            ("is_active", "INTEGER DEFAULT 1"),
        ]
        for col_name, col_ddl in scan_result_new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'scan_results' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(f"ALTER TABLE scan_results ADD COLUMN {col_name} {col_ddl}"))
                    logger.info("Added %s column to scan_results", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to scan_results: %s", col_name, e)


def _ensure_mysql_scan_run_cache_columns(engine) -> None:
    """WP-P.6：scan_runs 表扫描缓存与摘要统计字段补丁（MySQL 路径）。

    与 _ensure_sqlite_scan_run_cache_columns 对称；所有字段 nullable，
    保证旧库升级幂等。
    """
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return
        scan_run_new_cols = [
            ("snapshot_id", "INTEGER NULL"),
            ("cache_key", "VARCHAR(128) NULL"),
            ("cache_hit", "INTEGER NULL"),
            ("total_in_snapshot", "INTEGER NULL"),
            ("coarse_match_count", "INTEGER NULL"),
            ("advanced_match_count", "INTEGER NULL"),
            ("result_rows_written", "INTEGER NULL"),
            ("degraded_reason", "VARCHAR(64) NULL"),
        ]
        for col_name, col_ddl in scan_run_new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'scan_runs' AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(f"ALTER TABLE scan_runs ADD COLUMN {col_name} {col_ddl}"))
                    logger.info("Added %s column to scan_runs", col_name)
                except Exception as e:
                    logger.warning("Failed to add %s column to scan_runs: %s", col_name, e)


def _convert_myisam_to_innodb(engine) -> None:
    """将 MySQL 中已存在的 MyISAM 表转为 InnoDB，确保外键兼容。"""
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return
        result = conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = :db AND ENGINE = 'MyISAM'"
        ), {"db": db_name})
        tables = [row[0] for row in result]
        for tbl in tables:
            # 防御性校验：tbl 来自 information_schema，仍需校验表名合法性，避免异常表名注入
            if not _IDENTIFIER_RE.match(tbl):
                logger.warning("跳过非法表名（不匹配标识符规则）: %s", tbl)
                continue
            try:
                conn.execute(text(f"ALTER TABLE `{tbl}` ENGINE=InnoDB"))
                logger.info("Converted table '%s' from MyISAM to InnoDB", tbl)
            except Exception as e:
                logger.warning("Failed to convert table '%s' to InnoDB: %s", tbl, e)


def _ensure_mysql_backtest_snapshot_columns(engine) -> None:
    """WP7.2：backtest_runs 表回测快照字段补丁（MySQL，幂等）。

    通过 information_schema.COLUMNS 检查列是否存在，幂等添加。
    向后兼容：历史回测的新字段为 NULL，仍可正常读取。
    注：cost_config_json / factor_model_run_id / factor_data_cutoff_at
    已由 _ensure_mysql_indicator_version_columns 中的 backtest 段覆盖，此处不重复。
    """
    import logging
    logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        if not db_name:
            return

        new_cols = [
            ("member_snapshot_json", "TEXT NULL"),
            ("symbol_ids_json", "TEXT NULL"),
            ("excluded_members_json", "TEXT NULL"),
            ("portfolio_rule_version_id", "INTEGER NULL"),
            ("score_mode", "VARCHAR(32) NULL"),
            ("data_cutoff_at", "DATETIME NULL"),
            ("engine_name", "VARCHAR(64) NULL"),
            ("engine_version", "VARCHAR(32) NULL"),
            ("source_type", "VARCHAR(32) NULL"),
        ]
        for col_name, col_ddl in new_cols:
            result = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = 'backtest_runs' "
                "AND COLUMN_NAME = :col"
            ), {"db": db_name, "col": col_name})
            if result.first() is None:
                try:
                    conn.execute(text(
                        f"ALTER TABLE backtest_runs ADD COLUMN {col_name} {col_ddl}"
                    ))
                    logger.info("Added %s column to backtest_runs", col_name)
                except Exception as e:
                    logger.warning(
                        "Failed to add backtest_runs.%s: %s", col_name, e
                    )


def _sa_type_to_ddl(col, is_mysql: bool) -> str:
    """将 SQLAlchemy Column 类型翻译为目标数据库（SQLite/MySQL）对应的 DDL 类型片段。"""
    from sqlalchemy import (
        BigInteger, Boolean, Date, DateTime, Float, Integer, String, Text,
    )
    typ = col.type
    try:
        if isinstance(typ, BigInteger):
            base = "BIGINT" if is_mysql else "INTEGER"
        elif isinstance(typ, Integer):
            base = "INTEGER"
        elif isinstance(typ, Boolean):
            base = "TINYINT(1)" if is_mysql else "INTEGER"
        elif isinstance(typ, Float):
            base = "DOUBLE" if is_mysql else "REAL"
        elif isinstance(typ, DateTime):
            base = "DATETIME"
        elif isinstance(typ, Date):
            base = "DATE"
        elif isinstance(typ, Text):
            base = "TEXT"
        elif isinstance(typ, String):
            length = getattr(typ, "length", None)
            if length:
                base = f"VARCHAR({int(length)})"
            else:
                base = "TEXT"
        else:
            base = "TEXT"
    except Exception:
        base = "TEXT"

    null_clause = " NOT NULL" if (not col.nullable and not col.primary_key) else " NULL"
    return base + null_clause


def _sa_default_to_literal(col, is_mysql: bool):
    """提取列上可安全回填的 scalar 默认值（返回 None 表示无法/不应回填）。"""
    if col.primary_key:
        return None
    default = getattr(col, "default", None)
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    if callable(arg):
        return None
    # 允许的标量：字符串 / 整数 / 浮点 / 布尔 / None
    if arg is None or isinstance(arg, (str, int, float, bool)):
        return arg
    return None


def _refresh_model_metadata() -> None:
    """再次确保 app.models 下所有模块都被 import，Base.metadata 里表注册完整。

    说明：from app.models import X 已经在 init_db.py 顶部执行；但某些场景（比如 alembic
    env.py 直接 from app.db.init_db import 没有先 import models），可能 Base.metadata
    缺少新表。这里再兜底扫一遍，保证新模块自动生效。
    """
    import app.models  # noqa: F401
    # models/__init__.py 里的自动扫描器在模块 import 时已执行；此处为幂等二次调用，
    # 确保任何动态添加的子模块也能被发现。
    if hasattr(app.models, "_auto_discover_models"):
        app.models._auto_discover_models()  # type: ignore[attr-defined]


def _auto_align_all_schema(engine) -> None:
    """通用 Schema 自动对齐（一站式）：自动扫模型 → 缺表建 → 缺列加 → 缺索引补。

    执行顺序：
      1. 刷新模型元数据（再 import 一次 app.models，确保新模块都进来）
      2. 缺表：Base.metadata.create_all 先处理（create_all 天然幂等）
      3. 缺列：对已存在但缺列的表，ALTER TABLE ADD COLUMN（增量，不破坏数据）
      4. 缺索引：对每个表，比对 SA Index 与 DB 现存索引，补齐缺失的（幂等）

    安全原则（与之前一致）：
    - 只做 CREATE / ADD，绝不删除 / 改名 / 改类型 / 删索引
    - 标识符（表名/列名/索引名）必须匹配 _IDENTIFIER_RE 正则，防注入
    - 单项 ALTER 失败只打 warning，不中断启动（降级容错）
    """
    import logging
    logger = logging.getLogger(__name__)
    from sqlalchemy import inspect as sa_inspect, Index, text

    # Step 1. 刷新注册
    _refresh_model_metadata()

    inspector = sa_inspect(engine)
    is_mysql = "mysql" in engine.dialect.name
    db_table_names = set(inspector.get_table_names())
    tables = Base.metadata.tables
    total_tables_created = 0
    total_cols_added = 0
    total_indexes_added = 0

    # Step 2. 缺表处理：先 create_all，再对仍缺失的单独兜底（create_all 本身即 checkfirst=True）
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        logger.warning("[auto-align] create_all 部分失败，继续逐表兜底: %s", exc)
    for table_name, sa_table in tables.items():
        if table_name not in inspector.get_table_names():
            try:
                sa_table.create(engine, checkfirst=True)
                logger.info("[auto-align] 创建缺失表: %s", table_name)
                total_tables_created += 1
            except Exception as exc:
                logger.warning("[auto-align] 创建表失败 %s: %s", table_name, exc)

    # 再刷新一次 inspector（表已新建）
    inspector = sa_inspect(engine)

    # Step 3 & 4. 对每个表：补列 + 补索引
    for table_name, sa_table in tables.items():
        if not _IDENTIFIER_RE.match(table_name):
            continue
        if table_name not in inspector.get_table_names():
            continue  # 前面仍没建好，跳过

        # --- 补列 ---
        try:
            db_cols = {c["name"] for c in inspector.get_columns(table_name)}
        except Exception:
            continue

        for col_name, col in sa_table.columns.items():
            if col_name in db_cols or col.primary_key:
                continue
            if not _IDENTIFIER_RE.match(col_name):
                logger.warning("[auto-align] 非法列名跳过: %s.%s", table_name, col_name)
                continue
            ddl_type = _sa_type_to_ddl(col, is_mysql)
            alter_sql = (
                f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {ddl_type}'
                if not is_mysql
                else f'ALTER TABLE `{table_name}` ADD COLUMN `{col_name}` {ddl_type}'
            )
            try:
                with engine.begin() as conn:
                    conn.execute(text(alter_sql))
                logger.info("[auto-align] 补齐缺失列: %s.%s  %s", table_name, col_name, ddl_type)
                total_cols_added += 1
            except Exception as exc:
                logger.warning("[auto-align] 补齐列失败 %s.%s: %s", table_name, col_name, exc)

        # --- 补索引 ---
        try:
            existing_indexes = {idx["name"] for idx in inspector.get_indexes(table_name)}
        except Exception:
            existing_indexes = set()

        for sa_idx in sa_table.indexes:
            idx_name = sa_idx.name
            if idx_name in existing_indexes:
                continue
            if not _IDENTIFIER_RE.match(idx_name):
                logger.warning("[auto-align] 非法索引名跳过: %s@%s", idx_name, table_name)
                continue
            # 构造 DDL
            col_exprs = []
            safe = True
            for col in sa_idx.columns:
                if not _IDENTIFIER_RE.match(col.name):
                    safe = False
                    break
                col_exprs.append(f"`{col.name}`" if is_mysql else f'"{col.name}"')
            if not safe:
                continue
            unique_clause = "UNIQUE " if sa_idx.unique else ""
            create_sql = (
                f"CREATE {unique_clause}INDEX `{idx_name}` ON `{table_name}` ({', '.join(col_exprs)})"
                if is_mysql
                else f'CREATE {unique_clause}INDEX IF NOT EXISTS "{idx_name}" ON "{table_name}" ({", ".join(col_exprs)})'
            )
            try:
                with engine.begin() as conn:
                    conn.execute(text(create_sql))
                logger.info("[auto-align] 补齐缺失索引: %s@%s  (%s)", idx_name, table_name, ", ".join(col_exprs))
                total_indexes_added += 1
            except Exception as exc:
                logger.warning("[auto-align] 补索引失败 %s@%s: %s", idx_name, table_name, exc)

    logger.info(
        "[auto-align] 表/列/索引 对齐完成：新增表%d / 新增列%d / 新增索引%d",
        total_tables_created, total_cols_added, total_indexes_added,
    )


def _auto_repair_basic_data_integrity(engine) -> None:
    """基础数据完整性自动修复（安全、幂等、不删数据）。

    修复范围：
    1) 「NOT NULL 且有 Python scalar 默认值」的列，DB 里却存在 NULL → 按默认值回填。
       典型历史场景：新列先被 ALTER 成 NULL + 后续改 NOT NULL 时 DB 老数据 NULL 残留。
    2) 空字符串/零值不处理（避免破坏业务语义）。

    注：外键孤立记录等高风险操作不自动修，只打 warning 提示人工处理。
    """
    import logging
    logger = logging.getLogger(__name__)
    from sqlalchemy import inspect as sa_inspect, text

    _refresh_model_metadata()
    inspector = sa_inspect(engine)
    is_mysql = "mysql" in engine.dialect.name
    tables = Base.metadata.tables
    total_fixed_rows = 0

    for table_name, sa_table in tables.items():
        if not _IDENTIFIER_RE.match(table_name):
            continue
        if table_name not in inspector.get_table_names():
            continue
        try:
            db_cols = {c["name"] for c in inspector.get_columns(table_name)}
        except Exception:
            continue

        # 收集候选列：nullable=False + 有默认值（scalar） + DB里确实有该列
        fill_candidates = []
        for col_name, col in sa_table.columns.items():
            if col_name not in db_cols or col.primary_key:
                continue
            default_val = _sa_default_to_literal(col, is_mysql)
            if default_val is None:
                # 允许 False/0/"" 这类 falsy 默认值
                arg = getattr(getattr(col, "default", None), "arg", None)
                if not isinstance(arg, (str, int, float, bool)):
                    continue
                default_val = arg
            if col.nullable:
                # 列声明上 nullable=True 就不需要强制回填
                # 但如果 DB 实际 column 是 NOT NULL（类型已改过），也尝试回填：
                try:
                    col_info = next(
                        (c for c in inspector.get_columns(table_name) if c["name"] == col_name), None
                    )
                    if not col_info or col_info.get("nullable", True):
                        continue
                except Exception:
                    continue
            fill_candidates.append((col_name, default_val))

        if not fill_candidates:
            continue

        # 逐列 UPDATE ... WHERE col IS NULL，幂等；单次只改真的为 NULL 的行
        for col_name, default_val in fill_candidates:
            tbl_q = f"`{table_name}`" if is_mysql else f'"{table_name}"'
            col_q = f"`{col_name}`" if is_mysql else f'"{col_name}"'
            try:
                if isinstance(default_val, str):
                    safe_val = default_val.replace("'", "''")
                    set_clause = f"{col_q} = '{safe_val}'"
                elif isinstance(default_val, bool):
                    set_clause = f"{col_q} = {1 if default_val else 0}"
                elif isinstance(default_val, (int, float)):
                    set_clause = f"{col_q} = {default_val}"
                else:
                    continue
                update_sql = f"UPDATE {tbl_q} SET {set_clause} WHERE {col_q} IS NULL"
                with engine.begin() as conn:
                    result = conn.execute(text(update_sql))
                    rows = getattr(result, "rowcount", 0) or 0
                if rows > 0:
                    logger.info(
                        "[auto-repair] 回填 %s.%s IS NULL → 默认值 %s，影响 %d 行",
                        table_name, col_name, repr(default_val), rows,
                    )
                    total_fixed_rows += rows
            except Exception as exc:
                logger.warning("[auto-repair] 回填失败 %s.%s: %s", table_name, col_name, exc)

    if total_fixed_rows > 0:
        logger.info("[auto-repair] 数据完整性修复完成，共回填 %d 行", total_fixed_rows)
    else:
        logger.info("[auto-repair] 数据完整性检查通过，无需修复")


def init_db() -> None:
    """初始化数据库：完整顺序的一站式启动初始化。

    执行顺序（严格保证）：
      0. 预检查 & 环境准备：SQLite 目录、MySQL MyISAM→InnoDB 转换
      1. 通用 Schema 对齐：_auto_align_all_schema（自动扫模型 + 建表 + 补列 + 补索引）
      2. 基础数据修复：_auto_repair_basic_data_integrity（NOT NULL+默认值列的 NULL 回填）
      3. 显式兼容补丁（精细逻辑，如特殊索引、部分唯一索引、字段默认值等）
      4. 数据迁移脚本（历史数据重打标等幂等 UPDATE）
      5. 种子数据：评分预设 / 因子定义 / 调度任务 / core 名单 / AI Profile 迁移 等
    """
    import logging
    logger = logging.getLogger(__name__)

    mgr = DatabaseManager.get()
    eng = mgr.engine

    # --- Step 0: 预检查 ---
    if mgr.is_sqlite:
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    if mgr.is_mysql:
        _convert_myisam_to_innodb(eng)

    # --- Step 1: 通用 Schema 对齐（新增模型无需手动写 patch） ---
    _auto_align_all_schema(eng)

    # --- Step 2: 基础数据修复（再列对齐之后才能做 UPDATE 回填） ---
    _auto_repair_basic_data_integrity(eng)

    # --- Step 3: 显式兼容补丁（需要精细处理的场景） ---
    # 通用索引：universe 增量同步覆盖索引
    _ensure_universe_incremental_index(eng)

    if mgr.is_sqlite:
        _ensure_sqlite_scan_result_columns(eng)
        _ensure_sqlite_score_columns(eng)
        _ensure_sqlite_factor_columns(eng)
        _ensure_sqlite_backtest_columns(eng)
        _ensure_sqlite_backtest_snapshot_columns(eng)
        _ensure_sqlite_trade_setup_columns(eng)
        _ensure_sqlite_indicator_version_columns(eng)
        _ensure_sqlite_journal_columns(eng)
        _ensure_sqlite_discovery_columns(eng)
        _ensure_sqlite_discovery_task_perf_columns(eng)
        _ensure_sqlite_portfolio_columns(eng)
        _ensure_sqlite_external_endpoint_runtime_columns(eng)
        _ensure_sqlite_async_task_columns(eng)
        _ensure_sqlite_scan_run_cache_columns(eng)
        _ensure_sqlite_watchlist_item_score_snapshot_column(eng)
        _ensure_sqlite_watchlist_item_columns(eng)
        with eng.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=30000;"))
    else:
        _ensure_mysql_indicator_version_columns(eng)
        _ensure_mysql_backtest_snapshot_columns(eng)
        _ensure_mysql_watchlist_item_columns(eng)
        _ensure_mysql_scan_result_columns(eng)
        _ensure_mysql_scan_run_cache_columns(eng)

    # --- Step 4: 数据迁移（幂等 UPDATE，历史数据重打标） ---
    _migrate_legacy_watchlist_items_origin_type(eng)

    # --- Step 5: 幂等种子数据 ---
    _ensure_core_watchlist_exists(eng)
    _ensure_core_watchlist_seed_items(eng)

    if mgr.is_sqlite:
        _ensure_sqlite_opportunity_transition_events_table(eng)
        _ensure_sqlite_portfolio_members_table(eng)
        _ensure_sqlite_notification_tables(eng)
        _ensure_sqlite_sim_orders_attribution_columns(eng)
        _ensure_sqlite_portfolio_reviews_table(eng)
        _ensure_sqlite_ai_session_tables(eng)
        _ensure_sqlite_ai_profiles_table(eng)
    elif mgr.is_mysql:
        _ensure_mysql_opportunity_transition_events_table(eng)
        _ensure_mysql_portfolio_members_table(eng)
        _ensure_mysql_notification_tables(eng)
        _ensure_mysql_sim_orders_attribution_columns(eng)
        _ensure_mysql_portfolio_reviews_table(eng)
        _ensure_mysql_ai_session_tables(eng)
        _ensure_mysql_ai_profiles_table(eng)

    # AI profile json -> DB 迁移
    _migrate_ai_config_json_to_profiles()

    # 系统评分预设 + 因子定义 + 因子运行时 + 调度任务
    _seed_system_scoring_configs()
    _seed_factor_definitions()
    _seed_factor_runtime_state()
    _seed_scheduled_tasks()

    # Akshare 第三方接口缓存
    _load_akshare_api_config_cache()

    logger.info("init_db() 全流程结束：schema对齐 → 数据修复 → 种子数据，全部完成")


def _seed_system_scoring_configs() -> None:
    """初始化系统内置评分预设（股票/ETF 各 4 套）。"""
    import logging
    from app.db.session import SessionLocal
    from app.services.scoring_config_engine import seed_system_scoring_configs
    logger = logging.getLogger(__name__)
    try:
        with SessionLocal() as db:
            seed_system_scoring_configs(db)
            db.commit()
    except Exception as e:
        logger.warning("Failed to seed system scoring configs: %s", e)


def _seed_factor_definitions() -> None:
    """初始化系统因子定义及其不可变版本。"""
    import logging
    from app.db.session import SessionLocal
    from app.services.factors.definitions import seed_factor_definitions

    logger = logging.getLogger(__name__)
    try:
        with SessionLocal() as db:
            seed_factor_definitions(db)
            db.commit()
    except Exception as e:
        logger.warning("Failed to seed factor definitions: %s", e)


def _seed_factor_runtime_state() -> None:
    '''Initialize factor runtime state without activating a model.'''
    import logging
    from app.db.session import SessionLocal
    from app.services.factors.config import ensure_factor_system_config# near-relative coupling: app bootstrap ensures system config/runtime pre-warm — audit 2026-08-30
    from app.services.factors.runtime import ensure_factor_runtime_state# near-relative coupling: app bootstrap ensures system config/runtime pre-warm — audit 2026-08-30

    logger = logging.getLogger(__name__)
    try:
        with SessionLocal() as db:
            ensure_factor_runtime_state(db)
            ensure_factor_system_config(db)
            db.commit()
    except Exception as exc:
        logger.warning('Failed to seed factor runtime state: %s', exc)


def _seed_scheduled_tasks() -> None:
    """Seed cross-platform schedules without duplicating existing rows."""
    import logging
    from app.db.session import SessionLocal
    from app.services.scheduled_tasks import seed_default_schedules

    logger = logging.getLogger(__name__)
    try:
        with SessionLocal() as db:
            seed_default_schedules(db)
            db.commit()
    except Exception as exc:
        logger.warning("Failed to seed scheduled tasks: %s", exc)


def _load_akshare_api_config_cache() -> None:
    """启动时加载第三方接口配置到内存缓存。"""
    import logging
    from app.db.session import SessionLocal
    from app.services.akshare_registry import load_config_cache
    logger = logging.getLogger(__name__)
    try:
        with SessionLocal() as db:
            load_config_cache(db)
        logger.info("Akshare API config cache loaded")
    except Exception as e:
        logger.warning("Failed to load akshare API config cache: %s", e)
