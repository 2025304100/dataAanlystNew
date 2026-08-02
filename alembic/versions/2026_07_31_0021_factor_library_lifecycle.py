"""WP1-03: factor library lifecycle schema migration.

Adds lifecycle, governance, and evaluation columns to factors/factor_versions,
creates evaluation_runs/transition_audits/factor_sets/factor_set_members tables,
and idempotently backfills 8 system factors with origin=system, lifecycle_status=active.

Revision ID: wps_0023_021_factor_library_lifecycle
Revises: wps_0801_001_universe_incremental_index
Create Date: 2026-07-31

对齐 docs/专业因子库开发计划.md §WP1-03 和 docs/因子设置与专业因子库改造方案.md §7。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wps_0023_021_factor_library_lifecycle"
down_revision: Union[str, None] = "wps_0801_001_universe_incremental_index"  # 代码实际 head，非底稿记录
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── 常量 ──────────────────────────────────────────────────

# 8 个系统因子的 factor_kind 映射（对齐 app/services/factors/definitions.py）
FACTOR_KIND_MAPPING = {
    "ep_ttm": "continuous",
    "negative_pb": "continuous",
    "roe_yoy_growth": "continuous",
    "main_inflow_5d_ratio": "continuous",
    "turnover_z20": "continuous",
    "lhb_institution_net_ratio": "event",
    "hot_rank_attention": "event",
    "tail_accumulation_proxy": "continuous",
}

SYSTEM_FACTOR_CODES = sorted(FACTOR_KIND_MAPPING.keys())

MIGRATION_NOTE = "wps_0023_021_factor_library_lifecycle"
LEGACY_FACTOR_SET_ID = "legacy-system-v1"

# factors 表新增列（除 FK 列单独处理）
# (col_name, column_type, server_default)
_FACTORS_NEW_COLUMNS = [
    ("origin", sa.String(length=32), None),
    ("lifecycle_status", sa.String(length=32), None),
    ("owner", sa.String(length=128), None),
    ("thesis", sa.Text(), None),
    ("factor_kind", sa.String(length=32), None),
    ("asset_scope_json", sa.Text(), None),
    ("active_version_id", sa.Integer(), None),
    ("shadow_version_id", sa.Integer(), None),
    ("risk_level", sa.String(length=16), None),
    ("archived_at", sa.DateTime(), None),
]

_FACTORS_NEW_INDEXES = [
    ("ix_factors_origin", ["origin"]),
    ("ix_factors_lifecycle_status", ["lifecycle_status"]),
]

# active_version_id / shadow_version_id 的 FK 约束名
_FACTORS_FK_CONSTRAINTS = [
    ("fk_factors_active_version_id", "active_version_id", "factor_versions", "id"),
    ("fk_factors_shadow_version_id", "shadow_version_id", "factor_versions", "id"),
]

# factor_versions 表新增列
_FACTOR_VERSIONS_NEW_COLUMNS = [
    ("formula_ast_json", sa.Text(), None),
    ("postprocess_json", sa.Text(), None),
    ("parameter_schema_json", sa.Text(), None),
    ("data_dependencies_json", sa.Text(), None),
    ("compiler_version", sa.String(length=32), None),
    ("execution_plan_hash", sa.String(length=64), None),
    ("complexity_score", sa.Float(), None),
    ("created_by", sa.String(length=128), None),
    ("created_via", sa.String(length=32), None),
    ("ai_provenance_json", sa.Text(), None),
    ("validation_status", sa.String(length=32), None),
    ("validation_errors_json", sa.Text(), None),
]

_FACTOR_VERSIONS_NEW_INDEXES = [
    ("ix_factor_versions_execution_plan_hash", ["execution_plan_hash"]),
    ("ix_factor_versions_validation_status", ["validation_status"]),
]


# ── 幂等工具函数 ──────────────────────────────────────────


def _utcnow_naive():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _fk_exists(inspector, table_name: str, fk_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return fk_name in {
        fk["name"] for fk in inspector.get_foreign_keys(table_name) if fk.get("name")
    }


# ── Phase 3: 新表创建 ─────────────────────────────────────


def _create_evaluation_runs(inspector) -> None:
    if _table_exists(inspector, "factor_evaluation_runs"):
        return
    op.create_table(
        "factor_evaluation_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("factor_version_id", sa.Integer(), nullable=False),
        sa.Column("universe_snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("data_cutoff_at", sa.DateTime(), nullable=True),
        sa.Column("target_code", sa.String(length=32), nullable=True),
        sa.Column("train_start_date", sa.DateTime(), nullable=True),
        sa.Column("train_end_date", sa.DateTime(), nullable=True),
        sa.Column("validation_start_date", sa.DateTime(), nullable=True),
        sa.Column("validation_end_date", sa.DateTime(), nullable=True),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column("gate_result", sa.String(length=24), nullable=True),
        sa.Column("rejection_reasons_json", sa.Text(), nullable=True),
        sa.Column("artifact_path", sa.String(length=512), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("selected_trade_date", sa.String(length=10), nullable=True),
        sa.Column("observed_symbols", sa.Integer(), nullable=True),
        sa.Column("expected_symbols", sa.Integer(), nullable=True),
        sa.Column("completeness_ratio", sa.Float(), nullable=True),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["factor_version_id"], ["factor_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for idx_name, idx_cols in [
        ("ix_factor_evaluation_runs_factor_version_id", ["factor_version_id"]),
        ("ix_factor_evaluation_runs_data_cutoff_at", ["data_cutoff_at"]),
        ("ix_factor_evaluation_runs_gate_result", ["gate_result"]),
        ("ix_factor_evaluation_runs_task_id", ["task_id"]),
        ("ix_evaluation_runs_factor_cutoff", ["factor_version_id", "data_cutoff_at"]),
    ]:
        if not _index_exists(inspector, "factor_evaluation_runs", idx_name):
            op.create_index(idx_name, "factor_evaluation_runs", idx_cols, unique=False)


def _create_transition_audits(inspector) -> None:
    if _table_exists(inspector, "factor_transition_audits"):
        return
    op.create_table(
        "factor_transition_audits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("factor_id", sa.Integer(), nullable=False),
        sa.Column("factor_version_id", sa.Integer(), nullable=True),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_run_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("migration_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["factor_id"], ["factors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["factor_version_id"], ["factor_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_run_id"],
            ["factor_evaluation_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for idx_name, idx_cols in [
        ("ix_factor_transition_audits_factor_id", ["factor_id"]),
        ("ix_factor_transition_audits_created_at", ["created_at"]),
        ("ix_transition_audits_factor_created", ["factor_id", "created_at"]),
    ]:
        if not _index_exists(inspector, "factor_transition_audits", idx_name):
            op.create_index(idx_name, "factor_transition_audits", idx_cols, unique=False)


def _create_factor_sets(inspector) -> None:
    if _table_exists(inspector, "factor_sets"):
        return
    op.create_table(
        "factor_sets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("frozen_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for idx_name, idx_cols in [
        ("ix_factor_sets_content_hash", ["content_hash"]),
        ("ix_factor_sets_status", ["status"]),
    ]:
        if not _index_exists(inspector, "factor_sets", idx_name):
            op.create_index(idx_name, "factor_sets", idx_cols, unique=False)


def _create_factor_set_members(inspector) -> None:
    if _table_exists(inspector, "factor_set_members"):
        return
    op.create_table(
        "factor_set_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("factor_set_id", sa.String(length=64), nullable=False),
        sa.Column("factor_id", sa.Integer(), nullable=False),
        sa.Column("factor_version_id", sa.Integer(), nullable=False),
        sa.Column("factor_code", sa.String(length=64), nullable=False),
        sa.Column("factor_version", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.Column("weight_constraint", sa.String(length=32), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("missing_policy", sa.String(length=32), nullable=True),
        sa.Column("excluded_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["factor_set_id"], ["factor_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["factor_id"], ["factors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["factor_version_id"], ["factor_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "factor_set_id", "factor_id", name="uq_factor_set_member"
        ),
    )
    if not _index_exists(inspector, "factor_set_members", "ix_factor_set_members_factor_set_id"):
        op.create_index(
            "ix_factor_set_members_factor_set_id",
            "factor_set_members",
            ["factor_set_id"],
            unique=False,
        )


# ── Phase 4: 系统因子回填 ─────────────────────────────────


def _backfill_system_factors(bind, inspector) -> None:
    """幂等回填 8 个系统因子的生命周期/治理字段，并写入迁移审计记录。"""
    if not _table_exists(inspector, "factors"):
        return
    # 幂等门：是否存在任一系统因子 lifecycle_status 为 NULL
    pending = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM factors "
            "WHERE lifecycle_status IS NULL AND code IN :codes"
        ).bindparams(sa.bindparam("codes", expanding=True)),
        {"codes": SYSTEM_FACTOR_CODES},
    ).scalar()
    if not pending:
        return

    now = _utcnow_naive()

    # 取 8 个系统因子及其 is_latest 版本
    rows = bind.execute(
        sa.text(
            "SELECT f.id, f.code, f.active_version_id, fv.id AS version_id, fv.version "
            "FROM factors f "
            "LEFT JOIN factor_versions fv "
            "  ON fv.factor_id = f.id AND fv.is_latest = 1 "
            "WHERE f.code IN :codes "
            "ORDER BY f.code"
        ).bindparams(sa.bindparam("codes", expanding=True)),
        {"codes": SYSTEM_FACTOR_CODES},
    ).fetchall()

    for fid, code, active_vid, version_id, version_num in rows:
        kind = FACTOR_KIND_MAPPING.get(code, "continuous")
        # 1) 更新 factors 生命周期字段（仅当 lifecycle_status IS NULL）
        bind.execute(
            sa.text(
                "UPDATE factors SET origin = :origin, lifecycle_status = :ls, "
                "factor_kind = :kind, risk_level = :risk, asset_scope_json = :scope "
                "WHERE id = :fid AND lifecycle_status IS NULL"
            ),
            {
                "origin": "system",
                "ls": "active",
                "kind": kind,
                "risk": "medium",
                "scope": '["cn-stock"]',
                "fid": fid,
            },
        )
        # 2) 更新 factor_versions 校验/来源字段（仅当 validation_status IS NULL）
        if version_id is not None:
            bind.execute(
                sa.text(
                    "UPDATE factor_versions SET validation_status = :vs, "
                    "created_by = :cb, created_via = :cv, compiler_version = :cvv "
                    "WHERE id = :vid AND validation_status IS NULL"
                ),
                {
                    "vs": "valid",
                    "cb": "system",
                    "cv": "import",
                    "cvv": "legacy-v1",
                    "vid": version_id,
                },
            )
        # 3) 设置 active_version_id（仅当当前为 NULL）
        if active_vid is None and version_id is not None:
            bind.execute(
                sa.text(
                    "UPDATE factors SET active_version_id = :vid "
                    "WHERE id = :fid AND active_version_id IS NULL"
                ),
                {"vid": version_id, "fid": fid},
            )
        # 4) 写入迁移审计记录（幂等：同 factor_id + migration_note 不重复）
        if _table_exists(inspector, "factor_transition_audits"):
            exists = bind.execute(
                sa.text(
                    "SELECT 1 FROM factor_transition_audits "
                    "WHERE factor_id = :fid AND migration_note = :note"
                ),
                {"fid": fid, "note": MIGRATION_NOTE},
            ).first()
            if not exists:
                bind.execute(
                    sa.text(
                        "INSERT INTO factor_transition_audits "
                        "(factor_id, factor_version_id, from_status, to_status, "
                        " actor, reason, migration_note, created_at) "
                        "VALUES (:fid, :vid, NULL, :ts, :actor, :reason, :note, :ts_created)"
                    ),
                    {
                        "fid": fid,
                        "vid": version_id,
                        "ts": "active",
                        "actor": "system_migration",
                        "reason": "Alembic 0021 backfill: system factor lifecycle initialization",
                        "note": MIGRATION_NOTE,
                        "ts_created": now,
                    },
                )


# ── Phase 5: legacy-system-v1 FactorSet ──────────────────


def _create_legacy_factor_set(bind, inspector) -> None:
    """幂等创建 legacy-system-v1 因子集及其成员，并计算 content_hash。"""
    import hashlib

    if not _table_exists(inspector, "factor_sets"):
        return
    if not _table_exists(inspector, "factor_set_members"):
        return
    if not _table_exists(inspector, "factors"):
        return

    now = _utcnow_naive()

    existing = bind.execute(
        sa.text("SELECT 1 FROM factor_sets WHERE id = :sid"),
        {"sid": LEGACY_FACTOR_SET_ID},
    ).first()
    if not existing:
        bind.execute(
            sa.text(
                "INSERT INTO factor_sets "
                "(id, name, description, status, frozen_at, created_by, "
                " created_at, updated_at) "
                "VALUES (:id, :name, :desc, :status, :frozen, :cb, :ca, :ua)"
            ),
            {
                "id": LEGACY_FACTOR_SET_ID,
                "name": "Legacy System Factors v1",
                "desc": "8 system factors frozen at migration wps_0023_021",
                "status": "frozen",
                "frozen": now,
                "cb": "system_migration",
                "ca": now,
                "ua": now,
            },
        )

    # 取 8 个系统因子及其 is_latest 版本（按 code 字母序）
    rows = bind.execute(
        sa.text(
            "SELECT f.id, f.code, fv.id AS version_id, fv.version "
            "FROM factors f "
            "JOIN factor_versions fv "
            "  ON fv.factor_id = f.id AND fv.is_latest = 1 "
            "WHERE f.code IN :codes "
            "ORDER BY f.code"
        ).bindparams(sa.bindparam("codes", expanding=True)),
        {"codes": SYSTEM_FACTOR_CODES},
    ).fetchall()

    member_pairs = []
    for display_order, (fid, code, version_id, version_num) in enumerate(rows):
        # 幂等：同 factor_set_id + factor_id 不重复
        already = bind.execute(
            sa.text(
                "SELECT 1 FROM factor_set_members "
                "WHERE factor_set_id = :sid AND factor_id = :fid"
            ),
            {"sid": LEGACY_FACTOR_SET_ID, "fid": fid},
        ).first()
        if not already and version_id is not None:
            bind.execute(
                sa.text(
                    "INSERT INTO factor_set_members "
                    "(factor_set_id, factor_id, factor_version_id, factor_code, "
                    " factor_version, role, display_order, missing_policy, created_at) "
                    "VALUES (:sid, :fid, :vid, :code, :ver, :role, :ord, :mp, :ca)"
                ),
                {
                    "sid": LEGACY_FACTOR_SET_ID,
                    "fid": fid,
                    "vid": version_id,
                    "code": code,
                    "ver": version_num,
                    "role": "feature",
                    "ord": display_order,
                    "mp": "exclude",
                    "ca": now,
                },
            )
        member_pairs.append(f"{code}:{version_num}")

    # 计算 content_hash（SHA256 of sorted member factor_code:version pairs）
    pairs_sorted = sorted(member_pairs)
    content_hash = hashlib.sha256(
        "|".join(pairs_sorted).encode("utf-8")
    ).hexdigest()
    bind.execute(
        sa.text(
            "UPDATE factor_sets SET content_hash = :ch, updated_at = :ua "
            "WHERE id = :sid AND (content_hash IS NULL OR content_hash <> :ch)"
        ),
        {"ch": content_hash, "ua": now, "sid": LEGACY_FACTOR_SET_ID},
    )


# ── upgrade / downgrade ───────────────────────────────────


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    # ── Phase 1: factors 表幂等增列 ──
    if _table_exists(inspector, "factors"):
        for col_name, col_type, server_default in _FACTORS_NEW_COLUMNS:
            if not _column_exists(inspector, "factors", col_name):
                op.add_column(
                    "factors",
                    sa.Column(col_name, col_type, nullable=True, server_default=server_default),
                )
        for idx_name, idx_cols in _FACTORS_NEW_INDEXES:
            if not _index_exists(inspector, "factors", idx_name):
                op.create_index(idx_name, "factors", idx_cols, unique=False)
        # FK 约束：MySQL 直接加；SQLite 不支持给已有表追加 FK（默认不强制），跳过
        if dialect != "sqlite":
            for fk_name, col, ref_table, ref_col in _FACTORS_FK_CONSTRAINTS:
                if not _fk_exists(inspector, "factors", fk_name):
                    op.create_foreign_key(
                        fk_name,
                        "factors",
                        ref_table,
                        [col],
                        [ref_col],
                        ondelete="SET NULL",
                    )

    # ── Phase 2: factor_versions 表幂等增列 ──
    if _table_exists(inspector, "factor_versions"):
        for col_name, col_type, server_default in _FACTOR_VERSIONS_NEW_COLUMNS:
            if not _column_exists(inspector, "factor_versions", col_name):
                op.add_column(
                    "factor_versions",
                    sa.Column(col_name, col_type, nullable=True, server_default=server_default),
                )
        for idx_name, idx_cols in _FACTOR_VERSIONS_NEW_INDEXES:
            if not _index_exists(inspector, "factor_versions", idx_name):
                op.create_index(idx_name, "factor_versions", idx_cols, unique=False)

    # ── Phase 3: 创建新表 ──
    _create_evaluation_runs(inspector)
    _create_transition_audits(inspector)
    _create_factor_sets(inspector)
    _create_factor_set_members(inspector)

    # 重新构造 inspector 以反映新表
    inspector = sa.inspect(bind)

    # ── Phase 4: 系统因子幂等回填 ──
    _backfill_system_factors(bind, inspector)

    # ── Phase 5: legacy-system-v1 FactorSet ──
    _create_legacy_factor_set(bind, inspector)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    # ── 1-4: 删除新表（按依赖反向顺序）──
    if _table_exists(inspector, "factor_set_members"):
        op.drop_table("factor_set_members")
    if _table_exists(inspector, "factor_sets"):
        op.drop_table("factor_sets")
    if _table_exists(inspector, "factor_transition_audits"):
        op.drop_table("factor_transition_audits")
    if _table_exists(inspector, "factor_evaluation_runs"):
        op.drop_table("factor_evaluation_runs")

    # ── 5: 删除 Phase 1/2 新增索引 ──
    if _table_exists(inspector, "factor_versions"):
        for idx_name, _ in _FACTOR_VERSIONS_NEW_INDEXES:
            if _index_exists(inspector, "factor_versions", idx_name):
                op.drop_index(idx_name, table_name="factor_versions")
    if _table_exists(inspector, "factors"):
        for idx_name, _ in _FACTORS_NEW_INDEXES:
            if _index_exists(inspector, "factors", idx_name):
                op.drop_index(idx_name, table_name="factors")

    # ── 6: 删除 factors 表 FK 约束 ──
    if _table_exists(inspector, "factors") and dialect != "sqlite":
        for fk_name, _, _, _ in _FACTORS_FK_CONSTRAINTS:
            if _fk_exists(inspector, "factors", fk_name):
                op.drop_constraint(fk_name, "factors", type_="foreignkey")

    # ── 7: 删除 factor_versions 新增列 ──
    if _table_exists(inspector, "factor_versions"):
        for col_name, _, _ in reversed(_FACTOR_VERSIONS_NEW_COLUMNS):
            if _column_exists(inspector, "factor_versions", col_name):
                op.drop_column("factor_versions", col_name)

    # ── 8: 删除 factors 新增列 ──
    if _table_exists(inspector, "factors"):
        if dialect == "sqlite":
            # SQLite: ALTER TABLE DROP COLUMN 在表存在 FK 定义时会失败
            # （"unknown column ... in foreign key definition"），
            # 使用 batch_alter_table 重建表以正确移除列及其 FK 定义。
            with op.batch_alter_table("factors") as batch_op:
                for col_name, _, _ in reversed(_FACTORS_NEW_COLUMNS):
                    if _column_exists(inspector, "factors", col_name):
                        batch_op.drop_column(col_name)
        else:
            for col_name, _, _ in reversed(_FACTORS_NEW_COLUMNS):
                if _column_exists(inspector, "factors", col_name):
                    op.drop_column("factors", col_name)
