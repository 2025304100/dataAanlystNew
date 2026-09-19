"""WP8 绩效归因 API schemas。

定义归因报告与复盘记录的 Pydantic 模型，供 API 端点请求/响应使用。
归因报告结构灵活（维度可选），使用 dict 字段承载各维度结果，
避免每个维度都定义严格 schema 导致扩展困难。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


# ============================================================================
# 归因报告
# ============================================================================


class AttributionReport(BaseModel):
    """归因报告响应 schema。

    各维度字段为 dict（而非严格 schema），原因：
    - 维度结果结构相似但字段名不同（member_id / execution_mode / source_type 等）
    - 后续可能新增维度字段，严格 schema 反而限制扩展
    - 前端按 dimensions 参数动态渲染，不需要预知所有字段
    """

    model_config = ConfigDict(from_attributes=True)

    portfolio_id: int
    start_date: str | None = None
    end_date: str | None = None
    by_member: dict[str, Any] | None = None
    by_execution_mode: dict[str, Any] | None = None
    by_source: dict[str, Any] | None = None
    by_rule_signal: dict[str, Any] | None = None
    backtest_vs_sim: dict[str, Any] | None = None
    cost_impact: dict[str, Any] | None = None
    summary: str = ""


# ============================================================================
# 复盘记录
# ============================================================================


class ReviewCreate(BaseModel):
    """创建复盘记录请求。"""

    start_date: date
    end_date: date
    note: str | None = None
    title: str | None = None
    # 归因报告快照（可选，若提供则保存为 JSON）
    report_snapshot: dict[str, Any] | None = None
    # 回测运行 ID（可选，用于触发 backtest_vs_sim 维度计算）
    backtest_run_id: int | None = None
    # 指定归因维度（可选，默认全部）
    dimensions: list[str] | None = None


class ReviewRead(BaseModel):
    """复盘记录响应 schema。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    start_date: date
    end_date: date
    note: str | None = None
    title: str | None = None
    report_snapshot_json: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


__all__ = ["AttributionReport", "ReviewCreate", "ReviewRead"]
