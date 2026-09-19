"""因子公式模板 ORM（设计文档 §5.2 / §6.15，模块 M15）。

⚠️ 与 `factor_mining_templates` 的区别（容易混）：
  - `factor_formula_templates`  ← **公式模板**：25 个经典因子公式（含 `{n1}` 占位符），
    是初始种群「经典底座」的来源。本文件。
  - `factor_mining_templates`   ← **实验模板**：一整套挖掘配置的快照模板（种群/代数/三率…），
    在 `app/models/factor_mining.py` 中定义。

⚠️ 建表归属：本表由 alembic 修订 `0053_wps_0023_051_factor_mining_core` 创建（随 T01），
   本 ORM 与之一一对应。种子数据（25 个系统预设）由模块 M15 / T15 灌入。

> 系统预设 25 个（trend 5 / reversal 4 / volatility 4 / valuation 4 / quality 4 /
> volume_price 4）；依赖未采集字段 `gross_margin` / `asset_turnover` 的 2 个质量类模板
> **保持禁用**（`enabled=0`），且**不计入** M1 验收的编译率分母。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FactorFormulaTemplate(Base):
    """公式模板：`formula_template` 含 `{n1}` / `{n2}` 占位符，按 `param_definitions_json` 展开。"""

    __tablename__ = "factor_formula_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))

    #: 6 类之一：trend / reversal / volatility / valuation / quality / volume_price
    category: Mapped[str] = mapped_column(String(24), index=True)

    #: 带占位符的公式，如 `mean(close,{n1})/mean(close,{n2})-1`
    formula_template: Mapped[str] = mapped_column(Text)

    #: 占位符定义与取值网格，如 [{"name":"n1","values":[5,10,20]}, ...]
    param_definitions_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 经济逻辑说明；AI 生成因子的 `economic_logic` 与此对标
    economic_logic: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 数值越小优先级越高（「优先级覆盖」策略按此排序）
    priority: Mapped[int] = mapped_column(Integer, default=100)

    #: system（不可删改，可复制）/ personal（可增删改）
    scope: Mapped[str] = mapped_column(String(16), default="system", index=True)

    #: 来源：system / ai_generated / manual
    source: Mapped[str | None] = mapped_column(String(24), nullable=True)

    #: 算子数（复杂度，供配额分配的「复杂度升序」排序使用）
    complexity: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 0 = 禁用（如依赖未采集字段的模板），不参与初始种群生成
    enabled: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<FactorFormulaTemplate {self.id} {self.name} ({self.category})>"


__all__ = ["FactorFormulaTemplate"]
