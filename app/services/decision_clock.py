"""G0-WP0-2d: Unified decision clock.

Q1 rules this module implements:
  1. SINGLE source of truth for decision_at / data_cutoff_at / execution_at.
     Services MUST NOT compute these on their own.
  2. All DB storage remains UTC naive. Conversion to/from Asia/Shanghai
     happens EXCLUSIVELY inside this module via the two helpers.
  3. Default schedule (Q1.3, stock/ETF/index unified):
       decision_at    = T 15:05 Asia/Shanghai
       data_cutoff_at = T 15:00 Asia/Shanghai
       execution_at   = T+1 09:30 Asia/Shanghai
  4. Clock resolve takes a (trade_date: date, mode=?) tuple and returns the
     three UTC-naive datetimes for storage.

Why zoneinfo / pytz selection:
  - Prefer stdlib zoneinfo (Python 3.9+). If Asia/Shanghai fails on a broken
    Windows tz install we fall back to pytz.America/New_York then error out
    only if both fail; user guidance is clear in the raised message.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as tz
from enum import Enum
from typing import Literal, Optional

try:  # pragma: no cover - import guard
    from zoneinfo import ZoneInfo  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]

try:  # pragma: no cover - optional dependency
    import pytz  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    pytz = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

ASIA_SHANGHAI = "Asia/Shanghai"
TZ_ERROR_HINT = (
    "Neither zoneinfo.Asia/Shanghai nor pytz.Asia/Shanghai available on this host. "
    "Install tzdata on Windows: `pip install tzdata`."
)

# ═════════════════════════════════════════════════════════════════════════════
# T-C2 Q1.3：三字段默认值集中常量（ETF / 股票 / 指数共用同一套，不单独另设时点）
# ═════════════════════════════════════════════════════════════════════════════
# 收盘时间：A 股下午 15:00 Asia/Shanghai
DEFAULT_MARKET_CLOSE_TIME: time = time(hour=15, minute=0)
# 决策时点：收盘后 5 分钟 = T 15:05 Asia/Shanghai
DEFAULT_DECISION_DELTA_AFTER_CLOSE: timedelta = timedelta(minutes=5)
DEFAULT_DECISION_TIME: time = time(hour=15, minute=5)  # 兼容显式 time 值
# 数据边界：T 15:00 Asia/Shanghai（收盘时刻，之后的数据不算进本交易日决策）
DEFAULT_DATA_CUTOFF_TIME: time = time(hour=15, minute=0)
# 撮合执行：T+1 09:30 Asia/Shanghai = NEXT_OPEN（与 T-B1 口径严格对齐）
DEFAULT_EXECUTION_OFFSET_DAYS: int = 1
DEFAULT_EXECUTION_TIME: time = time(hour=9, minute=30)
DEFAULT_EXECUTION_MODE_NAME: str = "NEXT_OPEN"
# 组合三字段常量别名（前端/治理文档中引用 DEFAULT_EXECUTION 时等价于这套）
DEFAULT_EXECUTION = {
    "execution_mode": DEFAULT_EXECUTION_MODE_NAME,
    "execution_offset_days": DEFAULT_EXECUTION_OFFSET_DAYS,
    "execution_time_asia_shanghai": f"{DEFAULT_EXECUTION_TIME.hour:02d}:{DEFAULT_EXECUTION_TIME.minute:02d}",
    "decision_time_asia_shanghai": f"{DEFAULT_DECISION_TIME.hour:02d}:{DEFAULT_DECISION_TIME.minute:02d}",
    "data_cutoff_time_asia_shanghai": f"{DEFAULT_DATA_CUTOFF_TIME.hour:02d}:{DEFAULT_DATA_CUTOFF_TIME.minute:02d}",
}

# Portfolio rule stage 允许值：stock / etf / index / bond —— 但 T-C2 规定**全部共用同一常量**
PortfolioRuleStage = Literal["stock", "etf", "index", "bond"]


class MatchMode(str, Enum):
    NEXT_OPEN = "NEXT_OPEN"
    T_CLOSE = "T_CLOSE"


DEFAULT_MATCH_MODE = MatchMode.NEXT_OPEN

FORMAL_RUN_MODES = {"production", "strict_pit", "production_pit", "production_sim"}


def validate_match_mode(
    mode: MatchMode | str | None,
    run_mode: str,
    allow_t_close_research_override: bool | None = None,
) -> tuple[MatchMode, list[dict], bool]:
    """Q2.1：撮合模式验证器（双重保险接入点）。

    Args:
        mode: 请求的撮合模式；None 时使用 DEFAULT_MATCH_MODE = NEXT_OPEN
        run_mode: 当前 run_mode (research / production / strict_pit / ...)
        allow_t_close_research_override: 仅研究模式下显式 True 才允许 T_CLOSE

    Returns:
        (resolved_match_mode, degraded_warnings, is_result_production_eligible)

    Raises:
        ValueError: 正式链路使用 T_CLOSE 或研究模式未授权使用 T_CLOSE
            - meta.error_code = USE_OF_T_CLOSE_VIOLATION          (正式链路 T_CLOSE)
            - meta.error_code = RESEARCH_T_CLOSE_REQUIRES_EXPLICIT_OVERRIDE  (研究未授权)
    """
    if mode is None:
        resolved = DEFAULT_MATCH_MODE
    elif isinstance(mode, MatchMode):
        resolved = mode
    else:
        try:
            resolved = MatchMode(str(mode).upper())
        except Exception:
            raise ValueError(f"Invalid match_mode: {mode!r}; expected NEXT_OPEN or T_CLOSE")

    degraded_warnings: list[dict] = []
    is_eligible: bool = True

    if resolved == MatchMode.T_CLOSE:
        is_formal = run_mode in FORMAL_RUN_MODES

        if is_formal:
            err = ValueError("Q2.1 正式链路禁止使用 T_CLOSE 撮合；请用默认 NEXT_OPEN")
            err.__dict__["meta"] = {"error_code": "USE_OF_T_CLOSE_VIOLATION"}
            raise err

        if not allow_t_close_research_override:
            err = ValueError(
                "Q2.1 研究模式使用 T_CLOSE 撮合必须显式设置 allow_t_close_research_override=True"
            )
            err.__dict__["meta"] = {"error_code": "RESEARCH_T_CLOSE_REQUIRES_EXPLICIT_OVERRIDE"}
            raise err

        degraded_warnings.append({
            "code": "RESEARCH_ONLY_T_CLOSE",
            "message": "T_CLOSE 撮合仅用于研究回测，不得用于正式生产链路",
            "detail": {"run_mode": run_mode},
        })
        is_eligible = False

    return resolved, degraded_warnings, is_eligible


def _get_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(ASIA_SHANGHAI)
        except Exception:
            pass
    if pytz is not None:
        try:
            return pytz.timezone("Asia/Shanghai")
        except Exception:
            pass
    raise RuntimeError(TZ_ERROR_HINT)


# -------------------------- Public: zone conversion ---------------------------

def shanghai_to_utc_naive(dt_sh_aware: datetime) -> datetime:
    """Convert an Asia/Shanghai-aware datetime to UTC NAIVE (for DB storage)."""
    if dt_sh_aware.tzinfo is None:
        raise ValueError(
            "dt_sh_aware must be tz-aware (Asia/Shanghai). Got naive datetime.",
        )
    utc_aware = dt_sh_aware.astimezone(tz.utc)
    return utc_aware.replace(tzinfo=None)


def utcnow_aware() -> datetime:
    """当前 UTC 时间（带 tzinfo=UTC）。用于带时区字段的 API 响应等。"""
    return datetime.now(tz.utc)


def utcnow_naive() -> datetime:
    """当前 UTC 时间（naive，无 tzinfo，项目统一写入 DB 的 current_timestamp 风格）。

    T-C1 Q1.1：所有业务服务若需要"当前时间戳"（例如 started_at / consumed_at /
    updated_at，而不是三字段中的决策时点），应使用本函数，不允许在
    auto_trade_member_source / portfolio_backtest / signal_rules / allocation /
    decision_engine 五个核心服务中出现 datetime.now() / datetime.utcnow() /
    pendulum.now() 裸调用。
    """
    return datetime.now(tz.utc).replace(tzinfo=None)


def utcnow_today_utc_date() -> date:
    """当前 UTC 日期（date-only，常用在批次日切边界判断）。"""
    return utcnow_naive().date()


def utc_naive_to_shanghai(dt_utc_naive: datetime) -> datetime:
    """Reverse: take DB-fetched UTC-naive datetime -> Asia/Shanghai aware."""
    if dt_utc_naive.tzinfo is not None:
        raise ValueError(
            f"dt_utc_naive must be UTC naive. Got tzinfo={dt_utc_naive.tzinfo}",
        )
    tz_sh = _get_tz()
    return dt_utc_naive.replace(tzinfo=tz.utc).astimezone(tz_sh)


# -------------------------- Public: clock contract ---------------------------

ScheduleMode = Literal["t_day_close", "intraday_debug"]


@dataclass(frozen=True)
class ResolvedClock:
    """Three-field dataclass (Q1.1). All three fields are UTC naive (DB-safe)."""

    decision_at: datetime
    data_cutoff_at: datetime
    execution_at: datetime

    def as_shanghai_dict(self) -> dict:
        return {
            "decision_at": utc_naive_to_shanghai(self.decision_at).isoformat(),
            "data_cutoff_at": utc_naive_to_shanghai(self.data_cutoff_at).isoformat(),
            "execution_at": utc_naive_to_shanghai(self.execution_at).isoformat(),
        }

    # convenience properties (common in API JSON responses / snapshots)
    @property
    def decision_at_sh(self) -> datetime:
        return utc_naive_to_shanghai(self.decision_at)

    @property
    def data_cutoff_at_sh(self) -> datetime:
        return utc_naive_to_shanghai(self.data_cutoff_at)

    @property
    def execution_at_sh(self) -> datetime:
        return utc_naive_to_shanghai(self.execution_at)


def resolve(
    trade_date: date,
    *,
    portfolio_rule_stage: PortfolioRuleStage = "stock",
    mode: ScheduleMode = "t_day_close",
    decision_time: Optional[time] = None,
    cutoff_time: Optional[time] = None,
    execution_offset_days: int | None = None,
    execution_time: Optional[time] = None,
    override_trade_date: date | None = None,
    explicit_overrides: dict[str, Any] | None = None,
) -> ResolvedClock:
    """Resolve the three clock fields for a given T-day trade_date.

    T-C1 Q1.1 SINGLE source of truth.
    T-C2 Q1.3：ETF / 股票 / 指数共用同一常量（portfolio_rule_stage 参数接受，但不影响默认值）。

    Defaults (Q1.3 unified):
        decision   : T 15:05 Asia/Shanghai  (time_delta = DEFAULT_DECISION_DELTA_AFTER_CLOSE)
        cutoff     : T 15:00 Asia/Shanghai  (data boundary)
        execution  : T+1 09:30 Asia/Shanghai (NEXT_OPEN — 严格对齐 T-B1 MatchMode.NEXT_OPEN)

    Override precedence (higher wins):
        1) explicit_overrides["decision_at" / "data_cutoff_at" / "execution_at"] = UTC naive datetime
        2) decision_time / cutoff_time / execution_time / execution_offset_days  kwargs
        3) override_trade_date (替代入参 trade_date，常用于测试)
        4) T-C2 统一常量
    """
    # T-C2：ETF / 股票 / 指数共用同一套常量，不根据 portfolio_rule_stage 差异化
    # (参数保留用于将来扩展 + 测试断言 resolve(stage=etf)==resolve(stage=stock))
    _ = portfolio_rule_stage

    tz_sh = _get_tz()

    # Override 1: override_trade_date
    effective_trade_date: date = override_trade_date or trade_date

    # Override 2: 时间 / 偏移量 kwargs，未传时从 T-C2 常量取
    if mode == "t_day_close":
        dec_t = decision_time or DEFAULT_DECISION_TIME
        cut_t = cutoff_time or DEFAULT_DATA_CUTOFF_TIME
        exe_t = execution_time or DEFAULT_EXECUTION_TIME
        off_days = execution_offset_days if execution_offset_days is not None else DEFAULT_EXECUTION_OFFSET_DAYS
    elif mode == "intraday_debug":
        # 用于测试场景，常量不耦合 DEFAULT 常量（避免 T-C2 测试误穿透）
        dec_t = decision_time or time(hour=10, minute=30)
        cut_t = cutoff_time or time(hour=10, minute=25)
        exe_t = execution_time or time(hour=10, minute=35)
        off_days = execution_offset_days if execution_offset_days is not None else 0
    else:  # pragma: no cover
        raise ValueError(f"Unknown schedule mode={mode!r}")

    decision_sh = datetime.combine(effective_trade_date, dec_t, tzinfo=tz_sh)
    cutoff_sh = datetime.combine(effective_trade_date, cut_t, tzinfo=tz_sh)
    execution_sh = datetime.combine(
        effective_trade_date + timedelta(days=off_days),
        exe_t,
        tzinfo=tz_sh,
    )

    clock = ResolvedClock(
        decision_at=shanghai_to_utc_naive(decision_sh),
        data_cutoff_at=shanghai_to_utc_naive(cutoff_sh),
        execution_at=shanghai_to_utc_naive(execution_sh),
    )

    # Override 3: explicit_overrides（最高优先级 — 用于 debug / governance 强制指定）
    if explicit_overrides:
        fields = {}
        for k in ("decision_at", "data_cutoff_at", "execution_at"):
            if k in explicit_overrides and explicit_overrides[k] is not None:
                v = explicit_overrides[k]
                if not isinstance(v, datetime):
                    raise ValueError(f"explicit_overrides[{k!r}] must be UTC naive datetime, got {type(v).__name__}")
                if v.tzinfo is not None:
                    raise ValueError(f"explicit_overrides[{k!r}] must be UTC naive (no tzinfo)")
                fields[k] = v
        if fields:
            # ResolvedClock frozen=True，需要 object.__setattr__ 或者重 build
            clock = ResolvedClock(
                decision_at=fields.get("decision_at", clock.decision_at),
                data_cutoff_at=fields.get("data_cutoff_at", clock.data_cutoff_at),
                execution_at=fields.get("execution_at", clock.execution_at),
            )

    return clock
