"""快速扫描 5 分钟阶段预算与可观测性（WP-P.8）。

阶段预算（参照 spec，总预算 280s + 预留 20s = 300s = 5 分钟）：
- 快照与数据健康预检：10s
- SQL 粗筛排序 Top-K：30s
- 高级指标批量计算：90s
- 组合约束过滤：60s
- 候选快照与摘要写入：60s
- 收尾审计前端返回：30s

无高级指标或组合过滤时目标 60s 内完成。
超过阶段预算显示具体慢在哪一步，允许取消。

设计要点：
- ScanTimings 收集每个阶段的开始/结束时间、耗时、超时标记
- CancelToken 在每阶段开始前检查；不阻塞主流程，仅作为协作式取消点
- 超时降级 best-effort：总预算超时通过 ScanBudgetExceededError 抛出，
  由调用方捕获并转换为 "timeout" 状态响应（不抛异常给用户）
- 错误消息不含 API Key / Token / 密码等敏感信息
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)


# 阶段预算（秒）
STAGE_BUDGETS: dict[str, int] = {
    "snapshot_health_check": 10,
    "sql_coarse_filter": 30,
    "advanced_indicator": 90,
    "portfolio_filter": 60,
    "result_persistence": 60,
    "finalize_audit": 30,
}

# 总预算（5 分钟）：280s 阶段预算 + 20s 预留
TOTAL_BUDGET_SECONDS = 300
# 无高级指标或组合过滤时目标 60s 内完成
NO_FILTERS_TARGET_SECONDS = 60


def _now_naive_utc() -> datetime:
    """UTC 当前时间（naive，与 DB 中其它时间戳一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class StageTiming:
    """单阶段计时信息。"""

    stage: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    budget_ms: float | None = None
    exceeded: bool = False
    item_count: int | None = None  # 该阶段处理的标的数
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": round(self.duration_ms, 2) if self.duration_ms is not None else None,
            "budget_ms": round(self.budget_ms, 2) if self.budget_ms is not None else None,
            "exceeded": self.exceeded,
            "item_count": self.item_count,
            "error": self.error,
        }


@dataclass
class ScanTimings:
    """整次扫描的阶段计时集合。"""

    stages: list[StageTiming] = field(default_factory=list)
    total_duration_ms: float | None = None
    total_budget_ms: float = TOTAL_BUDGET_SECONDS * 1000
    total_exceeded: bool = False
    degraded_reason: str | None = None
    cancel_token_checked: bool = False  # 是否检查过取消令牌

    def to_dict(self) -> dict:
        return {
            "stages": [s.to_dict() for s in self.stages],
            "total_duration_ms": (
                round(self.total_duration_ms, 2)
                if self.total_duration_ms is not None
                else None
            ),
            "total_budget_ms": round(self.total_budget_ms, 2),
            "total_exceeded": self.total_exceeded,
            "degraded_reason": self.degraded_reason,
            "cancel_token_checked": self.cancel_token_checked,
        }

    def add_stage(
        self, stage: str, *, budget_ms: float | None = None
    ) -> StageTiming:
        """开始一个新阶段，返回 StageTiming 用于后续 finish。"""
        timing = StageTiming(
            stage=stage,
            started_at=_now_naive_utc(),
            budget_ms=budget_ms if budget_ms is not None else STAGE_BUDGETS.get(stage, 0) * 1000,
        )
        self.stages.append(timing)
        return timing

    def finish_stage(
        self,
        stage: str,
        *,
        item_count: int | None = None,
        error: str | None = None,
    ) -> StageTiming | None:
        """结束一个阶段（标记最后一个同名未结束阶段）。

        返回该 StageTiming；若找不到匹配项返回 None。
        """
        for timing in reversed(self.stages):
            if timing.stage == stage and timing.finished_at is None:
                timing.finished_at = _now_naive_utc()
                timing.duration_ms = (
                    timing.finished_at - timing.started_at
                ).total_seconds() * 1000
                if timing.budget_ms and timing.duration_ms > timing.budget_ms:
                    timing.exceeded = True
                timing.item_count = item_count
                timing.error = error
                return timing
        return None

    def check_total_exceeded(self) -> bool:
        """检查总预算是否超时，更新 total_duration_ms / total_exceeded。"""
        if not self.stages:
            return False
        total_ms = sum(s.duration_ms or 0 for s in self.stages)
        self.total_duration_ms = total_ms
        self.total_exceeded = total_ms > self.total_budget_ms
        return self.total_exceeded

    def get_exceeded_stages(self) -> list[StageTiming]:
        """返回超时的阶段列表。"""
        return [s for s in self.stages if s.exceeded]


class ScanBudgetExceededError(Exception):
    """扫描总预算超时异常。

    由 run_fast_scan 在总预算超时且 enforce_budget=True 时抛出；
    调用方应捕获并转换为 "timeout" 状态响应（不抛异常给用户）。
    """

    def __init__(
        self,
        message: str,
        *,
        timings: "ScanTimings",
        exceeded_stages: list["StageTiming"],
    ):
        super().__init__(message)
        self.timings = timings
        self.exceeded_stages = exceeded_stages


class ScanCancelledError(Exception):
    """扫描被用户取消异常。

    由 CancelToken.check() 在取消令牌已标记为取消时抛出；
    调用方应捕获并转换为 "cancelled" 状态响应。
    """


@dataclass
class CancelToken:
    """扫描取消令牌。

    允许用户在扫描过程中取消：检查 token 时如果已取消则抛 ScanCancelledError。
    集成到 task_state_machine 的 cancel_requested 字段（通过外部同步）。

    设计：
    - 不阻塞主流程，仅在每个阶段开始前检查
    - 取消是协作式的：worker 在阶段边界检查 token，无法在阶段中间打断
    - 线程安全：cancelled 字段为简单 bool，单写多读场景下 Python GIL 保证可见性
    """

    task_id: str | None = None
    cancelled: bool = False

    def check(self) -> None:
        """检查取消状态，如已取消抛 ScanCancelledError。"""
        if self.cancelled:
            raise ScanCancelledError("Scan cancelled by user")

    def cancel(self) -> None:
        """标记为取消。"""
        self.cancelled = True


def check_cancel_token(token: CancelToken | None) -> None:
    """在每个阶段开始前检查取消令牌。

    若 token 为 None 则直接返回（无取消能力）。
    若 token 已标记取消，抛 ScanCancelledError。
    """
    if token is not None:
        token.check()


__all__ = [
    "STAGE_BUDGETS",
    "TOTAL_BUDGET_SECONDS",
    "NO_FILTERS_TARGET_SECONDS",
    "StageTiming",
    "ScanTimings",
    "CancelToken",
    "ScanBudgetExceededError",
    "ScanCancelledError",
    "check_cancel_token",
]
