"""Restructure idempotency_records to the WP0-2 contract shape.

Bug: the ORM (app/models/idempotency_record.py) declares an autoincrement
``id`` primary key plus UNIQUE ``idempotency_key`` ("WP0-2 契约需要一个独立
autoincrement id 列"), but every migration-built database (MySQL gpfx and
clones; fresh alembic-only SQLite builds) still has the legacy shape with
``idempotency_key`` as PRIMARY KEY and no ``id`` column. Startup auto-align
cannot add primary keys, so any ORM query against IdempotencyRecord fails on
MySQL with "Unknown column 'idempotency_records.id'" (verified 2026-10-24 on
gpfx_validation). The service is currently dormant (no runtime importer), so
this fixes a latent P0 before it fires.

Data safety does not depend on any database being empty. The frozen helper
validates original request/response evidence before DDL, preserves all legacy
columns, copies explicit mappings, and supports interrupted primary-key work.
SQLite rebuilds reflect the existing table. Revision 0083 compensates already
applied earlier versions of this revision. All writers must be stopped during
the migration because MySQL DDL commits implicitly.
"""
import sqlalchemy as sa
from alembic import op

revision = "wps_0024_081_idempotency_pk_contract"
down_revision = "wps_0024_080_orm_alignment_catchup"
branch_labels = depends_on = None

_TABLE = "idempotency_records"
_ORM_KEY_INDEX = "ix_idempotency_records_idempotency_key"


def upgrade():
    from app.db.migration_contracts_v83 import ensure_idempotency_contract

    ensure_idempotency_contract(op.get_bind())


def downgrade():
    # 主键结构变更不回滚：回退会破坏 WP0-2 幂等契约并使 ORM 查询再度失败。
    raise RuntimeError("idempotency_records 主键契约不随迁移降级回退")
