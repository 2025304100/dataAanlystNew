"""ai_sessions.surface: 能力面预埋（P2-13，Batch 1）。

背景（既定约束 3）：公式 AI 暂不做会话持久化，只在 ai_sessions 增加 surface
字段做能力预埋——当前所有会话都由全局助手创建（surface='assistant'），
公式 AI 若将来接入会话化，写入 'formula' 即可按面区分。

本地链路说明（2026-10-25 实测）：
- 本机 DB 的 alembic_version = wps_0024_078_request_origin；
- 079~088 属于另一条进行中的工作流（组合准入/校验），尚未应用到本机，
  且只读验收实例硬校验 078，不能提前 upgrade head；
- 因此本次通过幂等桥接脚本先落列（保持 version=078，验收实例可继续运行），
  迁移本身保持幂等：将来链路推进到 088 后执行 `upgrade head`，本迁移检测到列
  已存在会自动 no-op，不会重复 ALTER。
"""
import sqlalchemy as sa
from alembic import op

revision = "wps_0024_089_ai_session_surface"
down_revision = "wps_0024_088_sector_concept_configs"
branch_labels = depends_on = None

_TABLE = "ai_sessions"
_COLUMN = "surface"
_DEFAULT = "assistant"


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return  # create_all 环境由模型直接建表
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        return  # 幂等：桥接脚本已落列
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=16), nullable=False, server_default=_DEFAULT),
    )
    # 存量行回填默认值（server_default 已覆盖 MySQL，这里显式补齐以兼容 SQLite）
    conn.execute(sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = :v WHERE {_COLUMN} IS NULL"), {"v": _DEFAULT})


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
