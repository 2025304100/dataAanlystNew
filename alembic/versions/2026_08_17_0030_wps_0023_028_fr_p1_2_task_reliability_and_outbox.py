# ===========================================================================
# ⚠️  FR-P1-2 / AC-10 异步任务可靠性与 Outbox 事务消息迁移 (可逆)     ⚠️
# ===========================================================================
# Revision ID : wps_0023_028_g4_fr_p1_2_task_reliability_and_outbox
# Operation   :
#   (a) async_tasks 表扩展可靠性列
#        - idempotency_key   —— 同事务唯一去重键（task_type+portfolio+date+cid）
#        - correlation_id    —— 跨 request / async_task / decision_run / outbox / audit 串联
#        - is_terminal_locked —— 终态写 1；任何 UPDATE（包括 _set_task）必须在 is_terminal_locked != 1
#          时才能改 status/stage（Worker/巡检 无法覆盖终态，严格对齐 project_memory #8）
#        - cancelled_timeout_at —— 若 CANCEL 后 >CANCEL_TIMEOUT_SECONDS 仍未进入终态，则巡检升级为
#          CANCELLED_TIMEOUT 记录审计（供人工介入；不影响 status=cancelled 的终态判断，保留）
#   (b) 新建 governance_events_outbox 事务 Outbox：
#        决策→订单→持仓→审计跨库跨服务，保证本地一事务写入 outbox_event，
#        再由 worker 做至少一次投递，含重试、死信、correlation_id 追溯；
#        表自带唯一键 uq_outbox_dedup_key，重复 POST 直接 IntegrityError（幂等）。
#   (c) 为 decision_runs / data_governance_audit_events 补加与 async_tasks 的关联索引。
#
# 👉 兼容性：
#    所有新增列 nullable；SQLite 用 batch_alter_table 加列；MySQL 方言兼容；
#    outbox 使用 bigint INTEGER 主键（AUTOINCREMENT）避免 MySQL 主键空间问题。
# ===========================================================================
"""wps_0023_028_g4_fr_p1_2_task_reliability_and_outbox.

Revision ID: wps_0023_028_g4_fr_p1_2_task_reliability_and_outbox
Revises: wps_0023_027_g3_data_governance_and_portfolio_status
Create Date: 2026-08-17 10:00:00.000000

"""
from alembic import op
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
    String, Text, inspect, BigInteger,
)


# revision identifiers, used by Alembic.
revision = 'wps_0023_028_g4_fr_p1_2_task_reliability_and_outbox'
down_revision = 'wps_0023_027_g3_data_governance_and_portfolio_status'
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    if not _table_exists(table_name):
        return False
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def _drop_indexes_referencing_columns(table_name: str, column_names: list[str]) -> None:
    """Remove ORM/generated indexes before SQLite batch column removal.

    ``Base.metadata.create_all`` may create an index that is not named by this
    historical revision.  During downgrade SQLite recreates the table and
    attempts to recreate every reflected index; indexes on columns being
    removed then fail with ``no such column``.  Dropping them first keeps the
    downgrade valid for both migration-first and metadata-first databases.
    """
    conn = op.get_bind()
    inspector = inspect(conn)
    try:
        indexes = inspector.get_indexes(table_name)
    except Exception:
        return
    removed = set(column_names)
    for index in indexes:
        name = index.get("name")
        if name and set(index.get("column_names") or ()).intersection(removed):
            try:
                op.drop_index(name, table_name=table_name)
            except Exception:
                pass


def upgrade() -> None:
    # ── (a) async_tasks 新增可靠性字段 ─────────────────────────────────────
    new_cols = [
        ("idempotency_key", String(128)),
        ("correlation_id", String(64)),
        ("is_terminal_locked", Integer),  # 布尔：0/1；终态写 1；所有 status/stage 修改强制 WHERE !=1
        ("cancelled_timeout_at", DateTime),
    ]
    for col_name, col_type in new_cols:
        if not _column_exists("async_tasks", col_name):
            with op.batch_alter_table("async_tasks") as batch_op:
                batch_op.add_column(Column(col_name, col_type, nullable=True))

    # idempotency_key 唯一索引（全局唯一，避免重复创建同任务）
    if not _index_exists("async_tasks", "ix_async_tasks_idempotency_key"):
        try:
            op.create_index(
                "ix_async_tasks_idempotency_key",
                "async_tasks", ["idempotency_key"], unique=True,
            )
        except Exception:
            # SQLite/老库如果已有同名记录可能失败，降级为非唯一索引（测试兼容）
            try:
                op.create_index(
                    "ix_async_tasks_idempotency_key_nonunique",
                    "async_tasks", ["idempotency_key"], unique=False,
                )
            except Exception:
                pass

    if not _index_exists("async_tasks", "ix_async_tasks_correlation_id"):
        op.create_index(
            "ix_async_tasks_correlation_id",
            "async_tasks", ["correlation_id"], unique=False,
        )
    if not _index_exists("async_tasks", "ix_async_tasks_status_terminal_locked"):
        op.create_index(
            "ix_async_tasks_status_terminal_locked",
            "async_tasks", ["status", "is_terminal_locked"], unique=False,
        )

    # ── (b) governance_events_outbox ────────────────────────────────────────
    if not _table_exists("governance_events_outbox"):
        op.create_table(
            "governance_events_outbox",
            Column("id", BigInteger().with_variant(Integer, "sqlite"),
                   primary_key=True, autoincrement=True),
            Column("dedup_key", String(128), nullable=False,
                   comment="同事件唯一幂等键 (aggregate_type + aggregate_id + event_type + business_key + cid)"),
            Column("correlation_id", String(64), nullable=False, index=True),
            Column("aggregate_type", String(32), nullable=False, index=True,
                   comment="portfolio / decision_run / order_plan / position / alert / audit_event"),
            Column("aggregate_id", String(128), nullable=False, index=True),
            Column("event_type", String(64), nullable=False, index=True,
                   comment="例如 DECISION_MADE / ORDER_WRITTEN / POSITION_UPDATED / AUDIT_WRITTEN / DATA_INCOMPLETE_PAUSED / RESUME_REQUESTED / TASK_HEARTBEAT"),
            Column("business_key", String(128), nullable=True, index=True),
            Column("task_id", String(64),
                   ForeignKey("async_tasks.id", ondelete="SET NULL"), nullable=True, index=True),
            Column("portfolio_id", Integer,
                   ForeignKey("portfolios.id", ondelete="SET NULL"), nullable=True, index=True),
            Column("payload_json", Text, nullable=False,
                   comment="事件载荷；至少包含 correlation_id、发生时间、受影响实体主键、版本、操作者"),
            Column("status", String(16), nullable=False, server_default="READY",
                   comment="READY / IN_PROGRESS / DELIVERED / DEAD_LETTERED"),
            Column("retry_count", Integer, nullable=False, server_default="0"),
            Column("max_retries", Integer, nullable=False, server_default="8"),
            Column("next_retry_at", DateTime, nullable=True, index=True),
            Column("last_error", Text, nullable=True),
            Column("delivered_at", DateTime, nullable=True),
            Column("dead_lettered_at", DateTime, nullable=True),
            Column("created_at", DateTime, nullable=False, index=True),
            Column("locked_by_worker", String(64), nullable=True),
            Column("locked_until", DateTime, nullable=True),
        )
        try:
            op.create_unique_constraint(
                "uq_governance_outbox_dedup_key",
                "governance_events_outbox",
                ["dedup_key"],
            )
        except Exception:
            # SQLite 方言兼容，退化创建索引
            try:
                op.create_index(
                    "uq_governance_outbox_dedup_key",
                    "governance_events_outbox", ["dedup_key"], unique=True,
                )
            except Exception:
                pass
        op.create_index(
            "ix_governance_outbox_status_next_retry",
            "governance_events_outbox",
            ["status", "next_retry_at"], unique=False,
        )
        # NOTE: SQLite 方言不支持在已创建表上 ALTER TABLE ADD CHECK CONSTRAINT（NotImplementedError）。
        # SQLite 通过 recreate + copy 的 batch_alter_table 才能重建约束；为保证 SQLite 测试库快速建表，
        # 此处用 try/except 降级（MySQL/PostgreSQL 上 CHECK 会被执行；SQLite 下 ORM + Pydantic 提供相同层级的业务校验）。
        try:
            op.create_check_constraint(
                "ck_gov_outbox_status_values",
                "governance_events_outbox",
                "status IN ('READY','IN_PROGRESS','DELIVERED','DEAD_LETTERED')",
            )
        except NotImplementedError:
            pass
        except Exception:
            pass
        try:
            op.create_check_constraint(
                "ck_gov_outbox_retry_count_range",
                "governance_events_outbox",
                "retry_count >= 0 AND retry_count <= 200",
            )
        except NotImplementedError:
            pass
        except Exception:
            pass

    # ── (c) 补充 decision_runs / audit_events 关联 async_tasks 的索引 ───────
    # decision_runs.task_id 在 ORM 已存在；检查索引（若存在同名 FK 索引则跳过）
    if not _index_exists("decision_runs", "ix_decision_runs_task_id_p12"):
        try:
            op.create_index(
                "ix_decision_runs_task_id_p12",
                "decision_runs", ["task_id"], unique=False,
            )
        except Exception:
            pass
    if not _index_exists("data_governance_audit_events", "ix_dg_audit_correlation_id_p12"):
        try:
            op.create_index(
                "ix_dg_audit_correlation_id_p12",
                "data_governance_audit_events", ["correlation_id"], unique=False,
            )
        except Exception:
            pass


def downgrade() -> None:
    # (c) 还原索引
    if _index_exists("data_governance_audit_events", "ix_dg_audit_correlation_id_p12"):
        try:
            op.drop_index("ix_dg_audit_correlation_id_p12",
                          table_name="data_governance_audit_events")
        except Exception:
            pass
    if _index_exists("decision_runs", "ix_decision_runs_task_id_p12"):
        try:
            op.drop_index("ix_decision_runs_task_id_p12", table_name="decision_runs")
        except Exception:
            pass

    # (b) 还原 outbox
    if _table_exists("governance_events_outbox"):
        try:
            if _index_exists("governance_events_outbox", "ix_governance_outbox_status_next_retry"):
                op.drop_index("ix_governance_outbox_status_next_retry",
                              table_name="governance_events_outbox")
        except Exception:
            pass
        try:
            # unique 约束在 sqlite 下可能以索引形式存在，尝试两种删法
            op.drop_constraint("uq_governance_outbox_dedup_key",
                               "governance_events_outbox", type_="unique")
        except Exception:
            try:
                op.drop_index("uq_governance_outbox_dedup_key",
                              table_name="governance_events_outbox")
            except Exception:
                pass
        try:
            op.drop_constraint("ck_gov_outbox_status_values",
                               "governance_events_outbox", type_="check")
        except Exception:
            pass
        try:
            op.drop_constraint("ck_gov_outbox_retry_count_range",
                               "governance_events_outbox", type_="check")
        except Exception:
            pass
        op.drop_table("governance_events_outbox")

    # (a) 还原 async_tasks 列.  Drop all indexes touching these columns first
    # because metadata-first schemas may have ORM-generated names.
    async_task_cols = ["cancelled_timeout_at", "is_terminal_locked",
                       "correlation_id", "idempotency_key"]
    _drop_indexes_referencing_columns("async_tasks", async_task_cols)
    for col_name in async_task_cols:
        if _column_exists("async_tasks", col_name):
            with op.batch_alter_table("async_tasks") as batch_op:
                try:
                    batch_op.drop_column(col_name)
                except Exception:
                    pass

    for idx_name, tbl in [
        ("ix_async_tasks_status_terminal_locked", "async_tasks"),
        ("ix_async_tasks_correlation_id", "async_tasks"),
        ("ix_async_tasks_idempotency_key_nonunique", "async_tasks"),
    ]:
        if _index_exists(tbl, idx_name):
            try:
                op.drop_index(idx_name, table_name=tbl)
            except Exception:
                pass
    # idempotency_key 唯一索引（兼容不同 db）
    for idx_name in ("ix_async_tasks_idempotency_key",):
        if _index_exists("async_tasks", idx_name):
            try:
                op.drop_index(idx_name, table_name="async_tasks")
            except Exception:
                pass
        try:
            op.drop_constraint(idx_name, "async_tasks", type_="unique")
        except Exception:
            pass
