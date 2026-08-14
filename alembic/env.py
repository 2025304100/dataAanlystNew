from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.db.base import Base
from app.db.init_db import (
    _auto_align_all_schema,
    _auto_repair_basic_data_integrity,
)

# 导入全部模型，确保 Base.metadata 包含所有表定义（与 app/db/init_db.py 保持一致）
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
    universe, discovery_candidate, portfolio_candidate,
    # P0-8：组合每日净值快照（绩效统计基础数据）
    portfolio_equity_snapshot,
    # P3+：市场指数日线（Benchmark 对比曲线基础设施）
    index_price,
    # WP-S：外部接口运行时状态（熔断器 + 计数器）
    external_endpoint_runtime,
)
from app.models import factor_evaluation  # noqa: F401

config = context.config

# 从 app/core/config.py 读取数据库 URL，覆盖 alembic.ini 中的静态配置
# 支持通过 DATABASE_URL 环境变量或 config/db_config.json 切换 SQLite/MySQL
db_url = os.getenv("ALEMBIC_DATABASE_URL") or settings.database_url
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # alembic 迁移前也做一次通用自动对齐 + 基础数据修复兜底：
    # 如果迁移脚本缺失导致漏表/漏列，先靠自动对齐补齐，再跑迁移，避免迁移前/迁移中查询报错。
    # （迁移本身仍是唯一真相源；自动对齐只做"增量补全"，不会删除/改名/破坏数据。）
    try:
        _auto_align_all_schema(connectable)
        _auto_repair_basic_data_integrity(connectable)
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "alembic env: auto-align skipped (non-fatal)", exc_info=True)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
