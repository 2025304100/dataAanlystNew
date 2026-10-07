from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event, engine_from_config, pool
from sqlalchemy import inspect as sa_inspect

from app.core.config import settings, load_db_config, build_mysql_url
from app.db.base import Base

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
    # Stage 2: durable data-quality quarantine ledger
    data_governance_quarantine,
)
from app.models import factor_evaluation  # noqa: F401
# 因子挖掘域（M1a）：显式登记以保持与 init_db.py 的追溯性一致。
# 注：import app.models 包时会执行 __init__.py 的 _auto_discover_models()，
#     故即使漏写此处新表也能被发现；显式列出是为可读性与审计。
from app.models import factor_mining, mining_candidate_pool, task_lock  # noqa: F401

config = context.config


def _resolve_sqlite_absolute(url: str) -> str:
    """将 SQLite URL 中的相对数据库路径解析为绝对路径。

    若 URL 形如 `sqlite:///relative/path.db` 或 `sqlite:///%(here)s/...`，
    则以项目根目录（alembic.ini 所在目录）为基准解析为绝对路径，
    并在写入前保证父目录存在，避免不同 cwd 下 sqlite3 打开 DB 失败。
    """
    if not url.startswith("sqlite:///"):
        return url
    raw_path = url[len("sqlite:///"):]
    # 使用 Path.is_absolute() 统一判定 Unix(/foo) 和 Windows(C:/foo 或 C:\foo) 绝对路径
    try:
        p = Path(raw_path)
        if p.is_absolute():
            # 已是绝对路径，父目录不存在时创建（避免首次打开失败）
            p.parent.mkdir(parents=True, exist_ok=True)
            # 统一用正斜杠 as_posix()，避免反斜杠转义问题
            return f"sqlite:///{p.resolve().as_posix()}"
    except Exception:
        pass
    alembic_ini_parent = Path(config.config_file_name).resolve().parent if config.config_file_name else Path(__file__).resolve().parent.parent
    abs_path = (alembic_ini_parent / raw_path).resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{abs_path.as_posix()}"


# target_metadata = Base.metadata 放到模块级（Base.metadata 只需要构建一次）
target_metadata = Base.metadata

# Logger setup only if we have an alembic.ini
if config.config_file_name is not None:
    # disable_existing_loggers=False：fileConfig 默认会把调用时刻已存在的全部 logger
    # 置为 disabled。在测试进程内跑 alembic（g0 / migration chain / g4 审计等用例）
    # 后，应用自己的 logger 会被整体静默，导致后续所有“应记到 WARNING 日志”
    # 的断言集体假失败（跨文件污染）。迁移不得改变宿主进程的日志拓扑。
    fileConfig(config.config_file_name, disable_existing_loggers=False)


# ---------------------------------------------------------------------------
# 幂等操作补丁（G0 spec T2 要求 migration 幂等可重复执行）
# ---------------------------------------------------------------------------
_idempotent_patch_installed = False


def _install_idempotent_operations_patch() -> None:
    global _idempotent_patch_installed
    if _idempotent_patch_installed:
        return
    import functools
    from sqlalchemy import inspect as _sqla_inspect
    from alembic.operations import Operations

    # --- create_table ---
    _orig_create_table = Operations.create_table

    @functools.wraps(_orig_create_table)
    def _create_table(self, table_name, *columns, **kw):
        bind = self.get_bind()
        if bind is not None and _sqla_inspect(bind).has_table(table_name):
            return None
        return _orig_create_table(self, table_name, *columns, **kw)

    Operations.create_table = _create_table

    # --- drop_table ---
    _orig_drop_table = Operations.drop_table

    @functools.wraps(_orig_drop_table)
    def _drop_table(self, table_name, **kw):
        bind = self.get_bind()
        if bind is not None and not _sqla_inspect(bind).has_table(table_name):
            return None
        return _orig_drop_table(self, table_name, **kw)

    Operations.drop_table = _orig_drop_table

    # --- create_index ---
    _orig_create_index = Operations.create_index

    @functools.wraps(_orig_create_index)
    def _create_index(self, index_name, table_name, columns, **kw):
        bind = self.get_bind()
        if bind is not None:
            insp = _sqla_inspect(bind)
            if insp.has_table(table_name):
                existing_columns = {col["name"] for col in insp.get_columns(table_name)}
                if any(str(column) not in existing_columns for column in columns):
                    return None
                idxs = insp.get_indexes(table_name)
                if any(i.get("name") == index_name for i in idxs):
                    return None
        # T3 修复：MySQL 不支持 CREATE INDEX IF NOT EXISTS 语法。
        # env.py 已用 insp.has_table + get_indexes 做了等价的前置存在性检查，
        # 此处必须剥离 if_not_exists kwarg，避免生成非法 SQL。
        kw.pop("if_not_exists", None)
        return _orig_create_index(self, index_name, table_name, columns, **kw)

    Operations.create_index = _create_index

    # --- drop_index ---
    _orig_drop_index = Operations.drop_index

    @functools.wraps(_orig_drop_index)
    def _drop_index(self, index_name, table_name=None, **kw):
        bind = self.get_bind()
        if bind is not None and table_name:
            insp = _sqla_inspect(bind)
            if insp.has_table(table_name):
                idxs = insp.get_indexes(table_name)
                if not any(i.get("name") == index_name for i in idxs):
                    return None
        return _orig_drop_index(self, index_name, table_name=table_name, **kw)

    Operations.drop_index = _orig_drop_index

    # --- add_column ---
    _orig_add_column = Operations.add_column

    @functools.wraps(_orig_add_column)
    def _add_column(self, table_name, column, schema=None, **kw):
        bind = self.get_bind()
        if bind is not None:
            insp = _sqla_inspect(bind)
            if insp.has_table(table_name, schema=schema):
                cols = [c["name"] for c in insp.get_columns(table_name, schema=schema)]
                if column.name in cols:
                    return None
        return _orig_add_column(self, table_name, column, schema=schema, **kw)

    Operations.add_column = _orig_add_column

    # --- drop_column ---
    _orig_drop_column = Operations.drop_column

    @functools.wraps(_orig_drop_column)
    def _drop_column(self, table_name, column_name, schema=None, **kw):
        bind = self.get_bind()
        if bind is not None:
            insp = _sqla_inspect(bind)
            if not insp.has_table(table_name, schema=schema):
                return None
            cols = [c["name"] for c in insp.get_columns(table_name, schema=schema)]
            if column_name not in cols:
                return None
        return _orig_drop_column(self, table_name, column_name, schema=schema, **kw)

    Operations.drop_column = _orig_drop_column

    # --- create_unique_constraint ---
    _orig_create_unique = Operations.create_unique_constraint

    @functools.wraps(_orig_create_unique)
    def _create_unique(self, name, table_name, *a, **kw):
        bind = self.get_bind()
        if bind is not None and name is not None:
            insp = _sqla_inspect(bind)
            if insp.has_table(table_name):
                uqs = insp.get_unique_constraints(table_name)
                if any(u.get("name") == name for u in uqs):
                    return None
        try:
            return _orig_create_unique(self, name, table_name, *a, **kw)
        except ValueError as ve:
            if name is None and "name" in str(ve).lower():
                return None
            raise
        except Exception:
            if name is None:
                return None
            raise

    Operations.create_unique_constraint = _create_unique

    # --- create_check_constraint ---
    _orig_create_check = getattr(Operations, "create_check_constraint", None)
    if _orig_create_check is not None:

        @functools.wraps(_orig_create_check)
        def _create_check(self, name, table_name, *a, **kw):
            bind = self.get_bind()
            if bind is not None and name is not None:
                insp = _sqla_inspect(bind)
                if insp.has_table(table_name):
                    try:
                        cks = insp.get_check_constraints(table_name)
                        if any(c.get("name") == name for c in cks):
                            return None
                    except Exception:
                        pass
            try:
                return _orig_create_check(self, name, table_name, *a, **kw)
            except NotImplementedError:
                return None
            except ValueError as ve:
                if name is None and "name" in str(ve).lower():
                    return None
                raise
            except Exception:
                if name is None:
                    return None
                raise

        Operations.create_check_constraint = _create_check

    # --- create_foreign_key ---
    _orig_create_fk = Operations.create_foreign_key

    @functools.wraps(_orig_create_fk)
    def _create_fk(self, name, source, referent, *a, **kw):
        bind = self.get_bind()
        if bind is not None and name is not None:
            insp = _sqla_inspect(bind)
            if insp.has_table(source):
                fks = insp.get_foreign_keys(source)
                if any(f.get("name") == name for f in fks):
                    return None
        return _orig_create_fk(self, name, source, referent, *a, **kw)

    Operations.create_foreign_key = _create_fk

    _idempotent_patch_installed = True


def _apply_db_url_from_env_or_settings() -> str:
    """Resolve DB URL for each migration run. Priority:

    1) Explicit env-var ALEMBIC_DATABASE_URL (highest priority, used by tests).
    2) config/db_config.json when use_mysql=True (project DB-config GUI flow).
    3) Fallback to settings.database_url (defaults to SQLite tempfile for dev).

    This must remain per-call rather than module-level so tests can swap DBs.
    """
    raw_db_url = os.getenv("ALEMBIC_DATABASE_URL")
    if raw_db_url:
        db_url = _resolve_sqlite_absolute(raw_db_url)
        config.set_main_option("sqlalchemy.url", db_url)
        return db_url

    # --- 经验 133459 修复：Alembic 必须跟应用 DB-config 同源 ------------------
    # 之前这里直接退回 settings.database_url（默认 SQLite tempfile），导致
    # 即便 config/db_config.json 里 use_mysql=True，alembic upgrade head 仍
    # 然连到 SQLite，跟 FastAPI 应用实际的 MySQL 库分离。
    try:
        db_cfg = load_db_config()
        if bool(db_cfg.get("use_mysql")) and isinstance(db_cfg.get("mysql"), dict):
            raw_db_url = build_mysql_url(db_cfg)
            config.set_main_option("sqlalchemy.url", raw_db_url)
            return raw_db_url
    except Exception:
        # db_config.json 损坏时回退，不阻止后续 SQLite 迁移运行
        pass

    raw_db_url = settings.database_url
    db_url = _resolve_sqlite_absolute(raw_db_url)
    config.set_main_option("sqlalchemy.url", db_url)
    return db_url


def _attach_sqlite_pragmas(connectable) -> None:
    """Configure SQLite migration connections for schema rewrite operations."""
    url = str(getattr(connectable, "url", ""))
    if not url.startswith("sqlite"):
        return

    @event.listens_for(connectable, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()


def _ensure_version_table_width(connection) -> None:
    """保证 alembic_version.version_num 能装下本项目的 revision id。

    实测问题：alembic 1.15.2 建出来的 version_num 是 VARCHAR(32)，而本项目 revision id
    最长 52 字（如 wps_0023_027_g3_data_governance_and_portfolio_status）。
    SQLite 不校验长度，所以本地永不受影响；MySQL 则在升级到中途直接
    `1406 Data too long for column 'version_num'`，结果是**没有任何环境能从空库重放整条链**
    （CI / 新部署 / 灾备重建均不可用）。
    已核 `context.configure(version_table_length=...)` 在 1.15.2 上不生效（参数被忽略），
    所以在此直接建表/加宽；对已存在的宽列是 no-op。
    """
    wanted = 128
    insp = sa_inspect(connection)
    if "alembic_version" not in insp.get_table_names():
        connection.exec_driver_sql(
            f"CREATE TABLE alembic_version (version_num VARCHAR({wanted}) NOT NULL, "
            "PRIMARY KEY (version_num))"
        )
        return

    dialect = connection.engine.dialect.name
    if dialect == "sqlite":
        return  # SQLite 不校验长度，不去碰它（避免重写表）
    if dialect == "mysql":
        row = connection.exec_driver_sql(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'alembic_version' "
            "AND COLUMN_NAME = 'version_num'"
        ).first()
        current = int(row[0]) if row and row[0] else 0
        if current < wanted:
            connection.exec_driver_sql(
                f"ALTER TABLE alembic_version MODIFY version_num VARCHAR({wanted}) NOT NULL"
            )


def run_migrations_offline() -> None:
    _install_idempotent_operations_patch()
    url = _apply_db_url_from_env_or_settings()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    _install_idempotent_operations_patch()
    db_url = _apply_db_url_from_env_or_settings()
    ini_section = config.get_section(config.config_ini_section, {}) or {}
    ini_section = dict(ini_section)
    ini_section["sqlalchemy.url"] = db_url
    connectable = engine_from_config(
        ini_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    _attach_sqlite_pragmas(connectable)

    with connectable.connect() as connection:
        dialect_name = connection.dialect.name
        if dialect_name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if dialect_name == "mysql":
            # 实测：有些 MySQL 实例的 default_storage_engine 是 MyISAM（本机 5.7 就是），
            # 那么从空库重放建出来的表全是 MyISAM：没有事务、外键直接被丢弃，
            # 与生产库（gpfx 全部 InnoDB、136 个外键生效）完全不是一个形状；
            # 还会先在 VARCHAR(255) 索引上撞 1071（MyISAM 索引上限 1000 字节）。
            # 只改本次会话，不动服务器配置。
            connection.exec_driver_sql("SET default_storage_engine=InnoDB")
        # 先保证版本表列宽能装下 revision id，否则 MySQL 上会在迁移中途 1406。
        _ensure_version_table_width(connection)
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
