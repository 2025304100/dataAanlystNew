from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event, engine_from_config, pool

from app.core.config import settings
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
    fileConfig(config.config_file_name)


# ---------------------------------------------------------------------------
# 幂等操作补丁（G0 spec T2 要求 migration 幂等可重复执行）
# 为 Alembic Operations 的核心 DDL 方法自动检查“对象是否存在”，避免：
#   - 测试代码先 Base.metadata.create_all 后 command.upgrade 的表冲突
#   - 人为重复执行 alembic upgrade head 时报错
#   - 已通过之前手动步骤（如直接 DDL）创建好的索引/列再次创建失败
# ---------------------------------------------------------------------------
_idempotent_patch_installed = False


def _install_idempotent_operations_patch() -> None:
    global _idempotent_patch_installed
    if _idempotent_patch_installed:
        return
    import functools
    from sqlalchemy import inspect as _sqla_inspect
    from alembic.operations import Operations

    def _safe(tbl=None, idx=None, col=None, ck=None, uq=None, fk=None):
        # 统一返回：(do_run, reason)；True=需要真正执行
        return True, ""

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

    Operations.drop_table = _drop_table

    # --- create_index ---
    _orig_create_index = Operations.create_index

    @functools.wraps(_orig_create_index)
    def _create_index(self, index_name, table_name, columns, **kw):
        bind = self.get_bind()
        if bind is not None:
            insp = _sqla_inspect(bind)
            if insp.has_table(table_name):
                # A metadata-first bootstrap can leave a legacy table with a
                # narrower ORM shape than the historical revision.  Creating
                # an index over a column that is not present would abort the
                # entire upgrade; the later schema-specific revisions remain
                # responsible for adding such columns when they are required.
                existing_columns = {col["name"] for col in insp.get_columns(table_name)}
                if any(str(column) not in existing_columns for column in columns):
                    return None
                idxs = insp.get_indexes(table_name)
                if any(i.get("name") == index_name for i in idxs):
                    return None
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

    Operations.drop_index = _drop_index

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

    Operations.add_column = _add_column

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

    Operations.drop_column = _drop_column

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
            # 某些方言 + batch_alter_table 会要求约束必须命名；
            # 若命名丢失（name=None），降级为跳过（业务侧 ORM Pydantic 保证一致性）。
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
                # SQLite 方言不支持 ALTER TABLE ADD CHECK（需 batch_alter_table
                # recreate + copy）。这里统一降级为 skip（CHECK 约束仅在 DB 层提供额外
                # 校验；同一枚举在 ORM __table_args__ + Pydantic Enum 均有双保险）。
                # PostgreSQL/MySQL 永远不会抛 NotImplementedError，因此不受影响。
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
    """在每次运行迁移时读取数据库 URL 并覆盖 Config。

    注意：此函数必须在 run_migrations_offline/online 内部调用（而非模块级）。
    原因是 alembic.util.pyfiles 会用同一个 Python 进程的 importlib 缓存 env.py 模块，
    若在模块级读取 os.environ[ALEMBIC_DATABASE_URL]，则第一次之后的值被永久缓存，
    会导致 pytest 中每个测试切换独立临时 DB 的场景全部写到同一个文件。
    """
    raw_db_url = os.getenv("ALEMBIC_DATABASE_URL") or settings.database_url
    db_url = _resolve_sqlite_absolute(raw_db_url)
    config.set_main_option("sqlalchemy.url", db_url)
    return db_url


def _attach_sqlite_pragmas(connectable) -> None:
    """Configure SQLite migration connections for schema rewrite operations.

    Historical downgrades recreate and remove tables in revision order.  A
    metadata-first database can contain newer ORM tables whose foreign keys
    point to an older table that the downgrade has already removed.  SQLite
    checks those references while executing DDL, unlike PostgreSQL's deferred
    migration workflow.  Disable checks only on Alembic's short-lived
    migration connection; application connections keep their normal policy.
    """
    url = str(getattr(connectable, "url", ""))
    if not url.startswith("sqlite"):
        return
    # SQLite engine created by engine_from_config — hook connect event.
    @event.listens_for(connectable, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()


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
    # 每次运行迁移都重新从 os.environ/settings 读取并覆盖 sqlalchemy.url
    db_url = _apply_db_url_from_env_or_settings()
    ini_section = config.get_section(config.config_ini_section, {}) or {}
    # Config.get_section() 在部分 Alembic 版本中仅返回 ini 文件原始值，
    # 不会合并 set_main_option 的覆盖；因此必须手动把 db_url 注入到
    # engine_from_config 的参数字典，确保 connectable 连到正确的数据库。
    ini_section = dict(ini_section)
    ini_section["sqlalchemy.url"] = db_url
    connectable = engine_from_config(
        ini_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # SQLite migration DDL must tolerate metadata-first schema dependencies.
    _attach_sqlite_pragmas(connectable)

    # IMPORTANT: per spec T2 + G0-6, Alembic migration is the single source of truth.
    # We explicitly do NOT call any auto-align / auto-repair / create_all helpers here,
    # so any drift between model schema and DB state must be solved by new revisions.

    with connectable.connect() as connection:
        # For SQLite raw connections, also apply the migration-only setting.
        dialect_name = connection.dialect.name
        if dialect_name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        # 显式 commit 防止 SQLite 下部分事务边界导致连接关闭时已写入 DDL 被回滚。
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
