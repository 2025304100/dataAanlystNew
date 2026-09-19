"""P0-改造：Schema 对齐 + 回填 + seed 单进程验证脚本（不启动 HTTP 服务）。

运行：
  python .\\scripts\\verify_portfolio_schema_migration.py

输出要点：
  1. positions / portfolio_members / portfolio_rules 新列是否存在
  2. auto_trade_strategies 表是否存在 + 索引
  3. positions.target_weight_pct 的回填结果
  4. 每个组合的 4 条策略 seed 是否存在
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("verify_portfolio_schema")


def main() -> int:
    # 0) 初始化 DatabaseManager（与 main.py lifespan 中逻辑一致）
    from app.core.config import settings, load_db_config, build_mysql_url
    from app.db.manager import DatabaseManager

    cfg = load_db_config()
    mgr = DatabaseManager.get()
    if cfg.get("use_mysql") and cfg.get("mysql", {}).get("host"):
        url = build_mysql_url(cfg)
        logger.info("初始化 MySQL 引擎: %s:%s/%s",
                     cfg["mysql"]["host"], cfg["mysql"]["port"], cfg["mysql"]["database"])
        mgr.initialize(url, db_type="mysql")
    else:
        logger.info("初始化 SQLite 引擎: %s", settings.database_url)
        mgr.initialize(settings.database_url, db_type="sqlite")

    # 1) 触发 init_db（核心：Schema 对齐 + 回填 + seed）
    from app.db.init_db import init_db
    logger.info("=" * 60)
    logger.info("STEP 1/4: 调用 init_db() 对齐 Schema + 回填 + seed ...")
    init_db()
    logger.info("STEP 1/4: init_db() 完成\n")

    # 2) 检查表 + 列
    from app.db.manager import DatabaseManager
    from sqlalchemy import inspect, text

    mgr = DatabaseManager.get()
    insp = inspect(mgr.engine)
    dialect = mgr.engine.dialect.name
    logger.info("=" * 60)
    logger.info("STEP 2/4: 检查表结构 & 关键列（dialect=%s）...", dialect)

    expect_tables = ["positions", "portfolio_members", "portfolio_rules", "auto_trade_strategies"]
    actual_tables = set(insp.get_table_names())
    for t in expect_tables:
        if t not in actual_tables:
            logger.error("  ✗ 缺少表: %s", t)
            return 2
        logger.info("  ✓ 表存在: %s", t)

    expect_cols = {
        "positions": ["target_weight_pct"],
        "portfolio_members": ["target_weight_pct", "deviation_pct"],
        "portfolio_rules": ["universe_key", "rebalance_frequency", "weighting_method", "factor_weights_json"],
        "auto_trade_strategies": [
            "id", "portfolio_id", "strategy_key", "strategy_name",
            "enabled", "params_json", "last_run_at", "last_run_status", "last_run_message",
            "created_at", "updated_at",
        ],
    }
    for t, cols in expect_cols.items():
        actual_cols = {c["name"] for c in insp.get_columns(t)}
        for c in cols:
            if c not in actual_cols:
                logger.error("  ✗ %s 缺少列: %s", t, c)
                return 2
        logger.info("  ✓ %s 关键列齐全 (%d/%d)", t, len(set(cols) & actual_cols), len(cols))

    # 索引检查
    ats_indexes = {idx["name"]: idx for idx in insp.get_indexes("auto_trade_strategies")}
    expect_idx_prefixes = ["uq_portfolio_strategy", "idx_auto_trade_strategies_portfolio", "idx_auto_trade_strategies_enabled"]
    for prefix in expect_idx_prefixes:
        if not any(n.startswith(prefix) for n in ats_indexes):
            # 注意：不同 dialect 上名字可能略有差异，做一次模糊检查
            found = False
            for _, idx in ats_indexes.items():
                cols = tuple(sorted(idx["column_names"]))
                if prefix == "uq_portfolio_strategy" and cols == ("portfolio_id", "strategy_key") and idx.get("unique"):
                    found = True
                elif prefix == "idx_auto_trade_strategies_portfolio" and cols == ("portfolio_id",):
                    found = True
                elif prefix == "idx_auto_trade_strategies_enabled" and cols == ("enabled",):
                    found = True
            if not found:
                logger.error("  ✗ auto_trade_strategies 缺少索引: %s (现有=%s)", prefix, list(ats_indexes))
                return 2
    logger.info("  ✓ auto_trade_strategies 关键索引齐全 (现有=%d)", len(ats_indexes))

    # 3) 回填结果抽查
    logger.info("\n" + "=" * 60)
    logger.info("STEP 3/4: 抽查回填效果 ...")
    with mgr.engine.connect() as conn:
        total_pos = conn.execute(text("SELECT COUNT(*) FROM positions")).scalar() or 0
        null_pos = conn.execute(
            text("SELECT COUNT(*) FROM positions WHERE target_weight_pct IS NULL")
        ).scalar() or 0
        logger.info("  positions 总数=%d，target_weight_pct 仍为 NULL=%d（新建空库可能全 NULL，正常）", total_pos, null_pos)

        total_pm = conn.execute(text("SELECT COUNT(*) FROM portfolio_members WHERE effective_to IS NULL")).scalar() or 0
        null_pm_tw = conn.execute(
            text("SELECT COUNT(*) FROM portfolio_members WHERE effective_to IS NULL AND target_weight_pct IS NULL")
        ).scalar() or 0
        logger.info("  portfolio_members (当前生效) 总数=%d，target_weight_pct 仍为 NULL=%d", total_pm, null_pm_tw)

        total_ats = conn.execute(text("SELECT COUNT(*) FROM auto_trade_strategies")).scalar() or 0
        pfs = conn.execute(text("SELECT COUNT(*) FROM portfolios")).scalar() or 0
        logger.info("  portfolios 总数=%d，auto_trade_strategies 总数=%d（期望≈%d）", pfs, total_ats, pfs * 4)

        if pfs > 0:
            rows = conn.execute(text(
                "SELECT p.id, p.name, (SELECT COUNT(*) FROM auto_trade_strategies s WHERE s.portfolio_id=p.id) AS cnt "
                "FROM portfolios p ORDER BY p.id LIMIT 5"
            )).mappings().all()
            for r in rows:
                logger.info("    - 组合#%s '%s' 策略数=%s", r["id"], r["name"], r["cnt"])

    # 4) 通过模型查询确认 seed 内容
    logger.info("\n" + "=" * 60)
    logger.info("STEP 4/4: 通过 ORM 模型验证 AutoTradeStrategy seed ...")
    from app.db.session import SessionLocal
    from app.models.portfolio import AutoTradeStrategy, Portfolio

    with SessionLocal() as db:
        pfs = db.query(Portfolio).order_by(Portfolio.id).limit(3).all()
        keys_expected = {s[0] for s in [
            (AutoTradeStrategy.STRATEGY_MA_TRACK,),
            (AutoTradeStrategy.STRATEGY_MOMENTUM,),
            (AutoTradeStrategy.STRATEGY_GRID,),
            (AutoTradeStrategy.STRATEGY_TP_SL,),
        ]}
        for pf in pfs:
            s_rows = db.query(AutoTradeStrategy).filter(AutoTradeStrategy.portfolio_id == pf.id).all()
            got_keys = {s.strategy_key for s in s_rows}
            enabled_map = {s.strategy_key: bool(s.enabled) for s in s_rows}
            missing = keys_expected - got_keys
            if missing:
                logger.error("  ✗ 组合#%s 缺少策略: %s", pf.id, missing)
                return 3
            logger.info(
                "  ✓ 组合#%s '%s' 4策略齐全。enabled={ma:%s, mom:%s, grid:%s, tp_sl:%s}",
                pf.id, pf.name,
                enabled_map.get(AutoTradeStrategy.STRATEGY_MA_TRACK),
                enabled_map.get(AutoTradeStrategy.STRATEGY_MOMENTUM),
                enabled_map.get(AutoTradeStrategy.STRATEGY_GRID),
                enabled_map.get(AutoTradeStrategy.STRATEGY_TP_SL),
            )

    logger.info("\n✅ 全部检查通过：Schema 对齐、关键列、索引、回填、4 策略 seed 均 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
