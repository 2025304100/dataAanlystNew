"""WP0-6 / Q8 T7：决策时点规则（防止一天两份可执行决策）。

tasks.md TR-06.6 规则单源真理：

1. 唯一可执行自动决策 = auto_simulation @ 20:30:
   - decision_at = T 20:30 local
   - data_cutoff_at = T 20:00 local
   - data_as_of_trade_date = T
   - 唯一允许写 order_plan + 推进 last_decision_trade_date。

2. 15:05 只允许 dry_run（非执行预览）：
   - decision_at = T 15:05 local
   - data_cutoff_at = (T-1) 15:00 local（T日收盘数据未稳定，禁止越界）
   - data_as_of_trade_date = T-1
   - 严禁写 order_plan / 推进 last_decision_trade_date / 触发 auto_simulation 告警。

3. 双决策硬禁止：
   - 代码/配置中任何 auto_simulation 触发 decision_at.hour<20 → SCHEDULE_TOO_EARLY。
   - today_preview 只是 GET 纯视图：复用 auto_simulation 结果，不创建 DecisionRun，
     不调 DecisionEngine.evaluate。

交易日历：使用 SQL trade_calendar；若无可用 trade_calendar 表，退化为 naive
版本：自动跳过周末，T-1 = max(T-工作日集合) 取最近一个非周六/周日。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

RUN_TYPE_DRY_RUN_PREVIEW: Literal["dry_run_preview"] = "dry_run_preview"
RUN_TYPE_AUTO_SIMULATION: Literal["auto_simulation"] = "auto_simulation"
RUN_TYPE_BACKTEST: Literal["backtest"] = "backtest"
RUN_TYPE_DRY_RUN: Literal["dry_run"] = "dry_run"

ScheduleRunType = Literal["dry_run_preview", "auto_simulation", "backtest", "dry_run"]


class ScheduleError(ValueError):
    """决策时点违反 T7 的业务错误。"""


def _previous_weekday(d: date) -> date:
    """朴素 T-1：若 d 是周一 → 周五；其余 → d-1。

    仅当没有 trade_calendar 表时做兜底。生产链路应显式注入
    trade_calendar.previous_trade_date() 覆盖此函数。
    """
    # weekday(): Monday=0, Sunday=6.
    # 周一 → 回退 3 天到周五；周日 → 回退 2 天到周五；其余 → 1 天。
    if d.weekday() == 0:
        delta = 3
    elif d.weekday() == 6:
        delta = 2
    else:
        delta = 1
    return d - timedelta(days=delta)


def compute_decision_schedule(
    *,
    run_type: ScheduleRunType,
    decision_local_date: date,
    decision_tz_offset_hours: int = 8,  # CST = UTC+8
) -> dict:
    """按 T7 规则计算 data_cutoff_at / data_as_of_trade_date 并返回结构化对象。

    返回字段（对齐 WP0-6 TR-06.6 断言需求）：
      - run_type: 输入
      - decision_at: datetime naive（本地时区），方便后续写入
      - data_cutoff_at: datetime naive（本地时区）
      - data_as_of_trade_date: date
      - schedule_blocked_reason: None 或 SCHEDULE_TOO_EARLY / DUAL_EXECUTION_RISK_PROHIBITED
    """
    if run_type == RUN_TYPE_AUTO_SIMULATION:
        # 20:30 正式推演；T-1 兜底用于 data_as_of 计算时不做加减（正式用当日本身）
        decision_at_local = datetime.combine(decision_local_date, time(20, 30, 0))
        data_cutoff_at_local = datetime.combine(decision_local_date, time(20, 0, 0))
        data_as_of_trade_date = decision_local_date
        # 双决策硬禁止：auto_simulation 正式任务小时 < 20 拦截
        # 本函数无法直接读取 cron，但对外暴露 allow_hour_ge_20 check 供调用方做拦截
        schedule_blocked_reason = None
        return {
            "run_type": run_type,
            "decision_at": decision_at_local,
            "data_cutoff_at": data_cutoff_at_local,
            "data_as_of_trade_date": data_as_of_trade_date,
            "schedule_blocked_reason": schedule_blocked_reason,
            "decision_tz_offset_hours": decision_tz_offset_hours,
        }

    if run_type in (RUN_TYPE_DRY_RUN_PREVIEW, RUN_TYPE_DRY_RUN):
        # 15:05 预览；data_as_of_trade_date = 最近一个真实交易日（朴素版跳过周末）
        as_of_date = _previous_weekday(decision_local_date)
        decision_at_local = datetime.combine(decision_local_date, time(15, 5, 0))
        data_cutoff_at_local = datetime.combine(as_of_date, time(15, 0, 0))
        return {
            "run_type": run_type,
            "decision_at": decision_at_local,
            "data_cutoff_at": data_cutoff_at_local,
            "data_as_of_trade_date": as_of_date,
            "schedule_blocked_reason": None,
            "decision_tz_offset_hours": decision_tz_offset_hours,
        }

    if run_type == RUN_TYPE_BACKTEST:
        # 回测：每个交易日 D 使用 D 日 15:00 作为 data_cutoff_at（收盘稳定可得）
        decision_at_local = datetime.combine(decision_local_date, time(15, 5, 0))
        data_cutoff_at_local = datetime.combine(decision_local_date, time(15, 0, 0))
        return {
            "run_type": run_type,
            "decision_at": decision_at_local,
            "data_cutoff_at": data_cutoff_at_local,
            "data_as_of_trade_date": decision_local_date,
            "schedule_blocked_reason": None,
            "decision_tz_offset_hours": decision_tz_offset_hours,
        }

    raise ScheduleError(f"未知 run_type: {run_type!r}")


def guard_auto_simulation_hour(decision_at_local: datetime) -> None:
    """双决策硬禁止：auto_simulation 在 <20:00 触发返回 409/400。

    调用位置：
      - 20:30 cron 任务入口（调度器设置 hour≥20 才运行，本函数做双层兜底）
      - 任何手工触发 auto_simulation 的 API
    """
    if decision_at_local.hour < 20:
        raise ScheduleError(
            "SCHEDULE_TOO_EARLY: auto_simulation 正式自动推演仅允许 20:00 后运行，"
            "避免一天两份决策。"
        )
