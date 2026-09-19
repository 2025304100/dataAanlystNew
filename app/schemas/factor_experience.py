"""F1 历史经验库 Pydantic schemas（T26，3 接口契约）。

路由层只做 DTO 转换与校验；业务规则全在
`app/services/factors/experience/service.py`。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CategoryLiteral = Literal[
    "trend", "reversal", "volatility", "valuation", "quality", "volume_price",
]
SourceLiteral = Literal["ai_generated", "enumerated", "random", "manual"]


class ExperienceMetricIn(BaseModel):
    """存回时的指标项。"""

    metric_type: Literal["icir", "coverage", "turnover", "ic"]
    value: float | None = None
    period: str | None = None
    is_oos: int = 0


class StoreExperienceRequest(BaseModel):
    """POST /factor-experience 存回请求。

    formula_ast 为 `factor_compiler._serialize_ast` 产物的 JSON dict。
    category 缺省时由 AST 启发式自动分类（`fingerprint.infer_category`）。
    """

    formula_ast: dict[str, Any]
    source: SourceLiteral
    category: CategoryLiteral | None = None
    metrics: list[ExperienceMetricIn] | None = None
    is_negative_sample: int = 0
    origin_project_id: str | None = None
    #: 场景上下文（market_env / stock_pool）→ 自动落 tags 供抽取时匹配
    task_context: dict[str, Any] | None = None
    #: 字段分层映射 {"close": "A", ...}；缺省全 "A"
    field_layers: dict[str, str] | None = None


class StoreExperienceResponse(BaseModel):
    experience_id: str
    #: created / duplicate（duplicate = 三层指纹命中已存在经验，幂等）
    status: str


class SampleExperienceItem(BaseModel):
    experience_id: str
    #: 参数实例化后的具体公式
    formula: str
    template: str
    placeholders: list[dict[str, Any]] = Field(default_factory=list)
    category: str
    source: str
    score: float


class SampleExperienceResponse(BaseModel):
    items: list[SampleExperienceItem] = Field(default_factory=list)


class RelatedExperienceItem(BaseModel):
    experience_id: str
    template: str
    category: str
    source: str
    success_rate: float
    avg_icir: float | None = None
    use_count: int


class RelatedExperienceResponse(BaseModel):
    items: list[RelatedExperienceItem] = Field(default_factory=list)


__all__ = [
    "StoreExperienceRequest",
    "StoreExperienceResponse",
    "SampleExperienceItem",
    "SampleExperienceResponse",
    "RelatedExperienceItem",
    "RelatedExperienceResponse",
]
