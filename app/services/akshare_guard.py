"""进程级供应商护栏：全局限速 + 熔断冷却。

为什么需要这一层
----------------
现有防风控只有 ``akshare_registry.apply_delay()``，它做的是"调用前睡一个随机
毫秒数"。这是**每线程各睡各的**：8 个 worker 同时跑，实际 QPS = 8 × (1/延时)，
对东财/新浪的 WAF 来说就是一个爬虫在并发抓取——这是本项目被限流的主要成因。

同时，源失败后没有任何冷却：某个源刚被 WAF 拒绝，下一个标的仍然按优先级先打它，
等于在被封期间持续敲门，只会延长封禁时间并拖慢整批任务。

本模块提供两件事
----------------
1. **Pacer（全局限速）**：按 api_key 维护"下一个可用时间片"，用预约方式
   （reserve-then-sleep）而不是"睡完再抢"，避免惊群和忙等。并发线程共享同一个
   时间轴，因此实际 QPS 与线程数无关，只由配置的间隔决定。
2. **Breaker（熔断冷却）**：连续失败到阈值即打开，冷却期内直接快速失败（不发
   任何网络请求），冷却到期后只放行一个探测请求（半开），成功则关闭，失败则
   冷却翻倍。

设计取舍
--------
- 全部是**进程内状态**，不落库、不依赖 DB：重启即复位。重启本身就会重置请求
  节奏，因此不需要持久化；持久化反而会让"历史失败"在修复后继续误伤。
- 熔断键是 ``api_key``（= 供应商端点，如 ``stock_zh_a_hist``），这是 WAF 实际
  封禁的粒度。网关层已有一个按 ``interface_key`` 的粗粒度熔断器
  （``external_data_gateway``），两者互补：网关管"这条业务链路还通不通"，本模块
  管"这个供应商端点还能不能碰"。
- 默认全部开启，可用环境变量关闭，便于测试与排障。
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from itertools import count
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("invalid %s=%r, fallback to %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# 全局限速：间隔下限来自每个 api_key 的风控档位 delay_min_ms；
# 这里只放"总开关"和一个排队上限，避免极端积压把请求拖到超时。
_PACER_ENABLED = _env_bool("AKSHARE_PACER_ENABLED", True)
_PACER_MAX_WAIT_S = _env_float("AKSHARE_PACER_MAX_WAIT_S", 30.0)
# 时间片抖动：完全等间隔的请求节律本身就是机器特征，加 ±15% 抖动更接近人工操作。
_PACER_JITTER = _env_float("AKSHARE_PACER_JITTER", 0.15)

# 熔断：连续失败阈值 / 基础冷却 / 冷却上限
_BREAKER_ENABLED = _env_bool("AKSHARE_BREAKER_ENABLED", True)
_BREAKER_THRESHOLD = _env_int("AKSHARE_BREAKER_THRESHOLD", 5)
_BREAKER_SEVERE_THRESHOLD = _env_int("AKSHARE_BREAKER_SEVERE_THRESHOLD", 2)
_BREAKER_COOLDOWN_S = _env_float("AKSHARE_BREAKER_COOLDOWN_S", 60.0)
_BREAKER_MAX_COOLDOWN_S = _env_float("AKSHARE_BREAKER_MAX_COOLDOWN_S", 900.0)
# 半开探测若因异常路径没有回报结果，超过该时长自动释放探测权，避免永久卡死。
_BREAKER_PROBE_TIMEOUT_S = _env_float("AKSHARE_BREAKER_PROBE_TIMEOUT_S", 120.0)


class SupplierCoolingDown(RuntimeError):
    """供应商处于熔断冷却期，本次调用被快速拒绝（未发出任何网络请求）。

    语义上属于"暂时不可用"，不是数据错误：上层 adapter 会把它转成
    ``SourceUnavailable`` 触发降级链 next，但不会重试、不会计为供应商失败。
    """

    def __init__(self, api_key: str, retry_after_s: float) -> None:
        self.api_key = api_key
        self.retry_after_s = max(0.0, float(retry_after_s))
        super().__init__(
            f"supplier {api_key} cooling down, retry after {self.retry_after_s:.0f}s"
        )


# ---------------------------------------------------------------------------
# Pacer：全局时间片预约
# ---------------------------------------------------------------------------


@dataclass
class _PacerState:
    next_at: float = 0.0
    reserved: int = 0


_pacer_lock = threading.Lock()
_pacers: dict[str, _PacerState] = {}


def pacing_enabled() -> bool:
    return _PACER_ENABLED


def acquire(api_key: str, *, min_interval_ms: int | float | None) -> float:
    """预约一个请求时间片并阻塞到该时刻。

    Args:
        api_key: 供应商端点标识，作为限速桶的键。
        min_interval_ms: 该端点的最小请求间隔（毫秒）。<=0 表示不限速。

    Returns:
        实际等待的秒数（便于调用方打点）。
    """
    if not _PACER_ENABLED or not api_key:
        return 0.0
    try:
        interval_ms = float(min_interval_ms or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if interval_ms <= 0:
        return 0.0

    interval = interval_ms / 1000.0
    if _PACER_JITTER > 0:
        interval *= 1.0 + random.uniform(-_PACER_JITTER, _PACER_JITTER)

    with _pacer_lock:
        state = _pacers.setdefault(api_key, _PacerState())
        now = time.monotonic()
        start = state.next_at if state.next_at > now else now
        wait = start - now
        if wait > _PACER_MAX_WAIT_S:
            # Truncating a reserved wait would contact the supplier too early
            # and bunch overloaded workers at the cap. Reject without taking
            # another slot; a later caller can reserve the original next slot.
            raise SupplierCoolingDown(api_key, wait)
        state.next_at = start + interval
        state.reserved += 1

    if wait <= 0:
        return 0.0
    time.sleep(wait)
    return wait


# ---------------------------------------------------------------------------
# Breaker：熔断冷却
# ---------------------------------------------------------------------------


_lease_ids = count(1)


@dataclass(frozen=True)
class SupplierPermit:
    api_key: str
    generation: int
    probe_id: int | None = None

    def __bool__(self):
        return self.probe_id is not None


@dataclass
class _BreakerState:
    generation: int = field(default_factory=lambda: next(_lease_ids))
    probe_id: int | None = None
    stale_results_ignored: int = 0
    consecutive_failures: int = 0
    open_until: float = 0.0          # monotonic 截止时间；0 表示 closed
    cooldown_s: float = 0.0
    probe_in_flight: bool = False
    probe_started_at: float = 0.0
    total_failures: int = 0
    total_opens: int = 0
    last_error: str | None = None
    _warned: bool = field(default=False)


_breaker_lock = threading.Lock()
_breakers: dict[str, _BreakerState] = {}


def _probe_stale(state: _BreakerState, now: float) -> bool:
    return state.probe_in_flight and (now - state.probe_started_at) > _BREAKER_PROBE_TIMEOUT_S


def ensure_available(api_key: str) -> SupplierPermit | None:
    """熔断检查。open 且未到期 -> 抛 ``SupplierCoolingDown``。

    冷却到期时只放行一个探测请求；其余线程继续快速失败，避免恢复瞬间被并发打爆。
    """
    if not _BREAKER_ENABLED or not api_key:
        return None
    with _breaker_lock:
        state = _breakers.get(api_key)
        if state is None:
            state = _breakers[api_key] = _BreakerState()
        if state.open_until <= 0:
            return SupplierPermit(api_key, state.generation)
        now = time.monotonic()
        if _probe_stale(state, now):
            logger.warning("breaker %s: stale probe released after %.0fs", api_key, _BREAKER_PROBE_TIMEOUT_S)
            state.probe_in_flight = False
            state.probe_id = None
        if now < state.open_until:
            raise SupplierCoolingDown(api_key, state.open_until - now)
        if state.probe_in_flight:
            raise SupplierCoolingDown(api_key, max(state.cooldown_s, 1.0))
        state.probe_in_flight = True
        state.probe_id = next(_lease_ids)
        state.probe_started_at = now
        logger.info("breaker %s: open -> half_open (probe granted)", api_key)
        return SupplierPermit(api_key, state.generation, state.probe_id)


def _current_permit(api_key, state, permit):
    # Missing permits are retained for explicit operator/test state control;
    # the production wrapper always carries its issued attempt permit.
    if permit is None:
        return True
    valid = permit.api_key == api_key and permit.generation == state.generation
    if permit.probe_id is not None:
        valid = valid and state.probe_in_flight and permit.probe_id == state.probe_id
    else:
        valid = valid and not state.probe_in_flight and state.open_until <= 0
    if not valid:
        state.stale_results_ignored += 1
    return valid


def release_probe(api_key: str, *, permit: SupplierPermit | None = None) -> None:
    """Return an owned probe when pacing rejects it before any supplier I/O."""
    with _breaker_lock:
        state = _breakers.get(api_key)
        if state is not None and _current_permit(api_key, state, permit):
            state.probe_in_flight = False
            state.probe_started_at = 0.0
            state.probe_id = None


def record_success(api_key: str, *, permit: SupplierPermit | None = None) -> None:
    """成功即关闭熔断并清零连续失败计数。"""
    if not _BREAKER_ENABLED or not api_key:
        return
    with _breaker_lock:
        state = _breakers.get(api_key)
        if state is None:
            return
        if not _current_permit(api_key, state, permit):
            return
        was_open = state.open_until > 0
        if was_open:
            state.generation = next(_lease_ids)
        state.consecutive_failures = 0
        state.open_until = 0.0
        state.cooldown_s = 0.0
        state.probe_in_flight = False
        state.probe_id = None
        state.last_error = None
        if was_open:
            logger.info("breaker %s: -> closed (probe succeeded)", api_key)


def record_failure(api_key: str, *, severe: bool = False, error: str | None = None,
                   permit: SupplierPermit | None = None) -> None:
    """记录一次供应商失败；达到阈值即打开熔断。

    Args:
        severe: True 表示"限流/风控"类失败（429/403/频繁），这类失败用更低的阈值
            和更长的冷却，因为 WAF 封禁通常持续数分钟而不是几秒。
    """
    if not _BREAKER_ENABLED or not api_key:
        return
    with _breaker_lock:
        state = _breakers.setdefault(api_key, _BreakerState())
        if not _current_permit(api_key, state, permit):
            return
        now = time.monotonic()
        state.total_failures += 1
        state.last_error = error

        if state.probe_in_flight:
            state.probe_in_flight = False
            state.probe_id = None
            state.generation = next(_lease_ids)
            state.cooldown_s = min(
                max(state.cooldown_s, _BREAKER_COOLDOWN_S) * 2.0,
                _BREAKER_MAX_COOLDOWN_S,
            )
            state.open_until = now + state.cooldown_s
            state.total_opens += 1
            logger.warning(
                "breaker %s: half_open -> open (probe failed, cooldown=%.0fs)", api_key, state.cooldown_s
            )
            return

        state.consecutive_failures += 1
        threshold = _BREAKER_SEVERE_THRESHOLD if severe else _BREAKER_THRESHOLD
        if state.consecutive_failures < max(1, threshold):
            return
        base = _BREAKER_COOLDOWN_S * (4.0 if severe else 1.0)
        state.cooldown_s = min(max(state.cooldown_s, base), _BREAKER_MAX_COOLDOWN_S)
        state.open_until = now + state.cooldown_s
        state.generation = next(_lease_ids)
        state.total_opens += 1
        logger.warning(
            "breaker %s: closed -> open (failures=%d >= %d, cooldown=%.0fs, severe=%s): %s",
            api_key, state.consecutive_failures, threshold, state.cooldown_s, severe, error,
        )


def is_open(api_key: str) -> bool:
    """只读查询：该端点当前是否处于冷却期（供探测接口/前端展示用）。"""
    if not _BREAKER_ENABLED or not api_key:
        return False
    with _breaker_lock:
        state = _breakers.get(api_key)
        return bool(state and state.open_until > time.monotonic())


def snapshot() -> dict[str, dict[str, float | int | str | bool | None]]:
    """导出当前护栏状态快照（排障用，不含任何凭证）。"""
    now = time.monotonic()
    with _breaker_lock:
        breakers = {
            key: {
                "consecutive_failures": state.consecutive_failures,
                "open": state.open_until > now,
                "cooldown_remaining_s": round(max(0.0, state.open_until - now), 1),
                "total_failures": state.total_failures,
                "total_opens": state.total_opens,
                "last_error": state.last_error,
                "generation": state.generation,
                "probe_in_flight": state.probe_in_flight,
                "stale_results_ignored": state.stale_results_ignored,
            }
            for key, state in _breakers.items()
        }
    with _pacer_lock:
        pacers = {
            key: {
                "reserved": state.reserved,
                "backlog_s": round(max(0.0, state.next_at - now), 2),
            }
            for key, state in _pacers.items()
        }
    return {"breakers": breakers, "pacers": pacers, "enabled": {
        "pacer": _PACER_ENABLED, "breaker": _BREAKER_ENABLED,
    }}


def reset_all() -> None:
    """清空全部护栏状态（进程内测试隔离、运维手动解封）。"""
    with _breaker_lock:
        _breakers.clear()
    with _pacer_lock:
        _pacers.clear()
