"""F1 历史经验库 ORM（设计文档 §6.14 / 开发文档 §3.12，模块 M14·T26）。

⚠️ 红线 C5：`mining/**` 不得 import 本文件——挖掘模块只经 HTTP 调 F1 接口
   （存回/抽取），不直接读写 F1 表（静态扫描防回归）。

4 张表：
  - factor_experience            主表：参数泛化模板 + 三层指纹 + 统计回写
  - factor_experience_tags       自动/人工标签
  - factor_experience_metrics    指标历史（icir/coverage/turnover/ic）
  - factor_experience_field_deps 字段依赖（抽取时按 field_scope 硬过滤）

建表归属：alembic 修订 `wps_0023_058_f1_experience_tables`（文件 0058，随 T26）。
子表按设计文档**不建外键**（共享库预留、经验可独立存在），关联一致性由
service 层同事务写入保证。

数值列纪律（P0）：`avg_icir` / `metrics.value` 等写入前必须过
`app.core.db_numeric.to_db_float`（NaN/±Inf → None，不可归 0）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FactorExperience(Base):
    """经验主表：`formula_template` 为参数泛化后的模板（数值 → `{n1}` 占位符）。"""

    __tablename__ = "factor_experience"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: 参数泛化后模板，如 `mean(close,{n1})/mean(close,{n2})-1`
    formula_template: Mapped[str] = mapped_column(Text)

    #: 泛化前原始 AST（`factor_compiler._serialize_ast` 产物 JSON）
    formula_ast: Mapped[str] = mapped_column(Text)

    #: 6 类之一：trend / reversal / volatility / valuation / quality / volume_price
    category: Mapped[str] = mapped_column(String(24))

    #: {"operators": n, "nesting_depth": n, "field_refs": n}
    complexity_json: Mapped[str] = mapped_column(Text)

    #: ai_generated / enumerated / random / manual
    source: Mapped[str] = mapped_column(String(24))

    #: 三层指纹（结构哈希+统计指纹+语义桶），全库唯一去重键
    fingerprint: Mapped[str] = mapped_column(String(64), index=True, unique=True)

    #: 占位符定义与取值范围 [{"name":"n1","value":20,"range":[5,60]}, ...]
    param_placeholders_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 被抽取次数 / 实例化后通过门禁次数 / 派生成功率
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float] = mapped_column(Float, default=0.0)

    #: 历史平均 ICIR（NaN → None，db_numeric 纪律）
    avg_icir: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: normal / premium / archived / need_field / unavailable
    status: Mapped[str] = mapped_column(String(16), default="normal")

    #: 1 = D 级因子负样本（M2 质量分级写入，抽取时自动规避）
    is_negative_sample: Mapped[int] = mapped_column(Integer, default=0)

    #: 来源项目（共享库预留）
    origin_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 结构版本，升级校验用
    schema_version: Mapped[str] = mapped_column(String(8), default="v1")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_factor_experience_category_status", "category", "status"),
        Index("ix_factor_experience_source_success_rate", "source", "success_rate"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<FactorExperience {self.id} {self.category} {self.fingerprint[:8]}>"


class FactorExperienceTag(Base):
    """标签：source=auto（存回时自动打）/ manual。"""

    __tablename__ = "factor_experience_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experience_id: Mapped[str] = mapped_column(String(64), index=True)
    tag_key: Mapped[str] = mapped_column(String(64))
    tag_value: Mapped[str] = mapped_column(String(255), default="")
    #: auto / manual
    source: Mapped[str] = mapped_column(String(16), default="auto")

    __table_args__ = (
        Index("ix_factor_experience_tags_exp_key", "experience_id", "tag_key"),
    )


class FactorExperienceMetric(Base):
    """指标历史：metric_type ∈ {icir, coverage, turnover, ic}。"""

    __tablename__ = "factor_experience_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experience_id: Mapped[str] = mapped_column(String(64), index=True)
    metric_type: Mapped[str] = mapped_column(String(24))
    #: NaN/±Inf → None（db_numeric 纪律，不可归 0）
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    period: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_oos: Mapped[int] = mapped_column(Integer, default=0)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class FactorExperienceFieldDep(Base):
    """字段依赖：抽取按 field_scope 硬过滤（任一依赖超界即排除）。"""

    __tablename__ = "factor_experience_field_deps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experience_id: Mapped[str] = mapped_column(String(64), index=True)
    field_code: Mapped[str] = mapped_column(String(64))
    #: A/B/C（基础行情 / 衍生 / 财务·资金流）；未指明默认 A
    field_layer: Mapped[str] = mapped_column(String(8), default="A")
    is_required: Mapped[int] = mapped_column(Integer, default=1)


__all__ = [
    "FactorExperience",
    "FactorExperienceTag",
    "FactorExperienceMetric",
    "FactorExperienceFieldDep",
]
