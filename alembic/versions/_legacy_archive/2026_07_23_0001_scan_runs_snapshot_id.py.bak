"""WP-P.6: scan_runs cache columns (snapshot_id + cache_key + summary stats)

Revision ID: wps_0023_001_scan_runs_cache
Revises: wps_001_external_endpoint
Create Date: 2026-07-23 00:01:00.000000

修复启动日志 `Unknown column 'scan_runs.snapshot_id' in 'field list'` 报错根因：
init_db.py 的 `_ensure_sqlite_scan_run_cache_columns` 仅覆盖 SQLite 路径，
MySQL 路径完全缺失。本 revision 为 SQLite/MySQL 双库正式补字段。

新增字段（全部 nullable，向后兼容旧库）：
- snapshot_id / cache_key：缓存命中查找键
- cache_hit / total_in_snapshot / coarse_match_count /
  advanced_match_count / result_rows_written：摘要统计
- degraded_reason：降级原因

注意：本迁移只新增列与索引，不修改既有数据，可安全回滚。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_001_scan_runs_cache"
down_revision: Union[str, None] = "wps_001_external_endpoint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _index_exists(inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return index_name in {i["name"] for i in inspector.get_indexes(table_name)}


# (column_name, column_type) — 类型对 SQLite/MySQL 均兼容
_NEW_COLUMNS = [
    ("snapshot_id", sa.Integer()),
    ("cache_key", sa.String(length=128)),
    ("cache_hit", sa.Integer()),
    ("total_in_snapshot", sa.Integer()),
    ("coarse_match_count", sa.Integer()),
    ("advanced_match_count", sa.Integer()),
    ("result_rows_written", sa.Integer()),
    ("degraded_reason", sa.String(length=64)),
]

_NEW_INDEXES = [
    ("ix_scan_runs_snapshot_id", ["snapshot_id"]),
    ("ix_scan_runs_cache_key", ["cache_key"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "scan_runs"):
        # 旧库可能连表都不存在（依赖 Base.metadata.create_all 创建），跳过
        return

    for col_name, col_type in _NEW_COLUMNS:
        if not _column_exists(inspector, "scan_runs", col_name):
            op.add_column("scan_runs", sa.Column(col_name, col_type, nullable=True))

    for idx_name, idx_cols in _NEW_INDEXES:
        if not _index_exists(inspector, "scan_runs", idx_name):
            op.create_index(idx_name, "scan_runs", idx_cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "scan_runs"):
        return

    for idx_name, _ in _NEW_INDEXES:
        if _index_exists(inspector, "scan_runs", idx_name):
            op.drop_index(idx_name, table_name="scan_runs")

    for col_name, _ in reversed(_NEW_COLUMNS):
        if _column_exists(inspector, "scan_runs", col_name):
            op.drop_column("scan_runs", col_name)
