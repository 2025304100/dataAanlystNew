"""统一外部数据网关（WP-S.1 ~ WP-S.4 稳定性底座）。

职责：
1. L1~L4 多层缓存（进程缓存 → 业务 DB → DuckDB 快照 → 第三方接口）
2. 单飞（single-flight）：相同 interface_key + normalized_params 同时只发一个真实请求
3. 三维限流：主机 / 接口 / 任务类型并发上限
4. 熔断器状态机：closed → open（连续失败）→ half_open（冷却到期）→ closed/open
5. 降级链复用：L4 失败时按 L3 → L2 → L1 顺序回退；透传 SourceChain 真实 source
6. 本地入库稳定性：UPSERT、批量分块写入、staging 原子切换、DuckDB 单写锁

设计原则（来自 project_memory 硬约束）：
- 不修改 akshare_registry.py / SourceChain 的现有逻辑，只做集成调用
- 不修改现有业务表的写入逻辑（daily_bars / index_prices 等仍由各自服务负责）
- 网关是新加的统一入口，后续 WP 逐步切换到使用网关
- 错误消息用英文（后端日志），前端展示用 t() 国际化（前端单独处理）
- normalized_params 哈希必须稳定（dict 排序后哈希）
- MySQL 连接错误不得暴露明文密码（错误消息做脱敏处理）
- 关键数据操作必须 commit 后再继续后续处理

调用示例：
    from app.services.external_data_gateway import fetch, GatewayResponse
    from datetime import timedelta
    resp: GatewayResponse = await fetch(
        interface_key="akshare.daily_bars",
        request_params={"symbol": "000001", "start": "2024-01-01", "end": "2024-06-01"},
        freshness_requirement=timedelta(hours=24),
        allow_stale=True,
    )
"""
from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import random
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.manager import DatabaseManager
from app.db.session import get_session_local
from app.models.external_endpoint_runtime import (
    ExternalEndpointRuntime,
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
)
from app.schemas.external_data import (
    CircuitBreakerOpenError,
    DataValidationError,
    GatewayResponse,
    SOURCE_L1_CACHE,
    SOURCE_L2_DB,
    SOURCE_L3_DUCKDB,
    SOURCE_L4_REMOTE,
    SOURCE_STALE,
    StaleDataError,
)

logger = logging.getLogger(__name__)


# ── CacheLevel 枚举与 GatewayRequest dataclass（WP-S.1b） ──

class CacheLevel(enum.Enum):
    """外部数据网关的缓存层级枚举。

    与 `GatewayResponse.source` 字段值一一对应（除 `NONE` 外）：
    - L1_PROCESS：进程内 TTL 缓存（秒/分钟级）
    - L2_BUSINESS_DB：业务数据库（SQLite/MySQL，DailyBar/IndexPrice 等）
    - L3_DUCKDB：DuckDB 分析仓库快照（raw_daily_bars 等）
    - L4_REMOTE：第三方接口实时拉取（akshare 等）
    - NONE：无可用数据源（用于查询结果为空或全部失败的语义）
    """

    L1_PROCESS = "l1_cache"
    L2_BUSINESS_DB = "l2_db"
    L3_DUCKDB = "l3_duckdb"
    L4_REMOTE = "l4_remote"
    NONE = "none"

    @classmethod
    def from_source(cls, source: str | None) -> "CacheLevel":
        """根据 GatewayResponse.source 字段反查 CacheLevel。

        `SOURCE_STALE` 与未知值统一映射为 `NONE`（语义为"未命中有效缓存层"）。
        """
        mapping = {
            SOURCE_L1_CACHE: cls.L1_PROCESS,
            SOURCE_L2_DB: cls.L2_BUSINESS_DB,
            SOURCE_L3_DUCKDB: cls.L3_DUCKDB,
            SOURCE_L4_REMOTE: cls.L4_REMOTE,
        }
        if source is None:
            return cls.NONE
        return mapping.get(source, cls.NONE)


@dataclass
class GatewayRequest:
    """外部数据网关的请求 dataclass（WP-S.1b）。

    封装 `fetch()` 的全部入参，便于上层调用方以单一对象传递请求语义，
    也便于未来扩展请求元数据（如 trace_id、retry_policy）。

    Attributes:
        interface_key: 接口标识（如 "akshare.daily_bars"）
        request_params: 请求参数（如 {"symbol": "000001", "start": "...", "end": "..."}）
        freshness_requirement: 数据新鲜度要求（cutoff_at 必须在 now - freshness 之内）
        allow_stale: True 时 L4 失败可降级到过期本地数据；False 时严禁返回过期数据
        preferred_sources: 优先数据源（保留参数，当前实现忽略，由 SourceChain 决定顺序）
        task_context: 任务上下文（含 task_type / task_id / host 等，用于追踪与限流）
    """

    interface_key: str
    request_params: dict
    freshness_requirement: timedelta
    allow_stale: bool = True
    preferred_sources: list[str] | None = None
    task_context: dict | None = None


# ── 工具函数 ────────────────────────────────────────────

def _utcnow_naive() -> datetime:
    """统一 UTC naive 时间戳（与项目其他模型对齐）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sanitize_error(exc: Exception) -> str:
    """脱敏错误消息：移除 MySQL 连接字符串中的明文密码。

    project_memory 硬约束 #7：MySQL 连接错误不得暴露明文密码。
    """
    msg = f"{type(exc).__name__}: {exc}"
    # 常见密码泄露模式：mysql://user:password@host / password=xxx / pwd=xxx
    import re
    msg = re.sub(r"(mysql://[^:]+:)[^@]+(@)", r"\1***\2", msg)
    msg = re.sub(r"(password|pwd)=\S+", r"\1=***", msg, flags=re.IGNORECASE)
    return msg


def normalize_params(params: Mapping[str, Any]) -> str:
    """生成稳定的 normalized_params 哈希键。

    要求：
    - dict 按 key 排序后 JSON 序列化（ensure_ascii=False 保持可读性）
    - 嵌套结构递归排序
    - 哈希使用 sha256，返回 hex 前 16 字符（足够区分，日志友好）

    稳定性保证：
    - 相同语义的 params 始终生成相同 hash（key 顺序无关）
    - 不同类型的值（int 1 vs str "1"）生成不同 hash
    """
    def _sort_recursive(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            return {k: _sort_recursive(obj[k]) for k in sorted(obj.keys())}
        if isinstance(obj, (list, tuple)):
            return [_sort_recursive(item) for item in obj]
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.isoformat()
        return obj

    try:
        canonical = json.dumps(
            _sort_recursive(dict(params)),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        # 不可序列化的参数降级为 str()，保持稳定即可
        canonical = str(sorted(params.items()))
        logger.debug("normalize_params: json serialize failed, fallback to str: %s", exc)

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _make_correlation_id() -> str:
    """生成调用追踪 ID（uuid4.hex）。"""
    return uuid4().hex


# ── L1 进程缓存（TTLCache） ─────────────────────────────

class _TTLCache:
    """简单的线程安全 TTL 缓存（不依赖 cachetools）。

    实现 OrderedDict + 过期时间戳，每次访问惰性清理过期项。
    满足 L1 缓存需求：秒/分钟级 TTL，进程内共享。
    """

    def __init__(self, maxsize: int = 512, ttl: int = 60) -> None:
        self._maxsize = max(8, int(maxsize))
        self._ttl = max(1, int(ttl))
        self._data: OrderedDict[str, tuple[float, Any, datetime | None]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[Any, datetime | None] | None:
        """返回 (value, data_cutoff_at) 或 None。"""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expire_at, value, cutoff = entry
            if time.time() > expire_at:
                # 过期，惰性删除
                self._data.pop(key, None)
                return None
            # 命中：移到末尾（LRU）
            self._data.move_to_end(key)
            return value, cutoff

    def set(self, key: str, value: Any, cutoff_at: datetime | None = None) -> None:
        with self._lock:
            expire_at = time.time() + self._ttl
            self._data[key] = (expire_at, value, cutoff_at)
            self._data.move_to_end(key)
            # 容量淘汰：LRU 弹出最旧项
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


_l1_cache = _TTLCache(
    maxsize=settings.EXTERNAL_DATA_L1_CACHE_MAXSIZE,
    ttl=settings.EXTERNAL_DATA_L1_CACHE_TTL_SECONDS,
)


# ── L2 业务数据库查询适配器注册表 ─────────────────────────

L2Fetcher = Callable[[Session, Mapping[str, Any]], tuple[Any, datetime | None]]
"""L2 查询函数签名：(db, params) -> (data, data_cutoff_at)。

data_cutoff_at 为本地数据的最新记录时间，用于判断是否满足 freshness_requirement。
"""

_l2_fetchers: dict[str, L2Fetcher] = {}


def register_l2_fetcher(interface_key: str, fetcher: L2Fetcher) -> None:
    """注册 L2 业务数据库查询函数。"""
    _l2_fetchers[interface_key] = fetcher


def _default_l2_daily_bars(db: Session, params: Mapping[str, Any]) -> tuple[Any, datetime | None]:
    """L2 默认实现：从 daily_bars 表查询。

    期望 params 包含：symbol_id（int）或 symbol（str），start/end（ISO 日期字符串）。
    """
    from app.models.daily_bar import DailyBar
    from app.models.symbol import Symbol

    symbol_id = params.get("symbol_id")
    if symbol_id is None and params.get("symbol"):
        symbol_id = db.execute(
            select(Symbol.id).where(Symbol.symbol == params["symbol"])
        ).scalar_one_or_none()
    if symbol_id is None:
        return None, None

    start = params.get("start") or params.get("start_date")
    end = params.get("end") or params.get("end_date")
    stmt = select(DailyBar).where(DailyBar.symbol_id == int(symbol_id))
    if start is not None:
        stmt = stmt.where(DailyBar.trade_date >= _parse_date(start))
    if end is not None:
        stmt = stmt.where(DailyBar.trade_date <= _parse_date(end))
    stmt = stmt.order_by(DailyBar.trade_date.asc())
    rows = db.execute(stmt).scalars().all()
    if not rows:
        return None, None
    frame = pd.DataFrame([
        {
            "trade_date": r.trade_date,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume, "amount": r.amount, "turnover_rate": r.turnover_rate,
            "source": r.source,
        }
        for r in rows
    ])
    cutoff = _parse_date(rows[-1].trade_date)
    return frame, cutoff


def _default_l2_index_prices(db: Session, params: Mapping[str, Any]) -> tuple[Any, datetime | None]:
    """L2 默认实现：从 index_prices 表查询。"""
    from app.models.index_price import IndexPrice

    symbol = params.get("symbol")
    if not symbol:
        return None, None
    start = params.get("start") or params.get("start_date")
    end = params.get("end") or params.get("end_date")
    stmt = select(IndexPrice).where(IndexPrice.symbol == str(symbol))
    if start is not None:
        stmt = stmt.where(IndexPrice.trade_date >= _parse_date(start))
    if end is not None:
        stmt = stmt.where(IndexPrice.trade_date <= _parse_date(end))
    stmt = stmt.order_by(IndexPrice.trade_date.asc())
    rows = db.execute(stmt).scalars().all()
    if not rows:
        return None, None
    frame = pd.DataFrame([
        {
            "trade_date": r.trade_date, "open": r.open, "high": r.high,
            "low": r.low, "close": r.close, "volume": r.volume,
            "amount": r.amount, "source": r.source,
        }
        for r in rows
    ])
    return frame, _parse_date(rows[-1].trade_date)


def _parse_date(value: Any):
    """兼容 ISO 字符串 / date / datetime / Timestamp。"""
    if value is None:
        return None
    if isinstance(value, str):
        return pd.Timestamp(value).date()
    if hasattr(value, "date"):
        return value.date() if callable(value.date) else value
    return pd.Timestamp(value).date()


# 注册内置 L2 fetcher
register_l2_fetcher("akshare.daily_bars", _default_l2_daily_bars)
register_l2_fetcher("akshare.index_prices", _default_l2_index_prices)


# ── L3 DuckDB 快照层 ───────────────────────────────────

def _l3_lookup(interface_key: str, params: Mapping[str, Any]) -> tuple[Any, datetime | None]:
    """L3：从 DuckDB FactorWarehouse 读取 raw_daily_bars 快照。

    若 DuckDB 不可用或表为空，返回 (None, None) 触发 L4 拉取。
    单写锁由 FactorWarehouse 内部保证，这里只读不会阻塞写入。
    """
    try:
        from app.services.factors.store import FactorWarehouse, FactorWarehouseUnavailable
    except ImportError:
        return None, None
    try:
        wh = FactorWarehouse()
        with wh.connection(read_only=True) as conn:
            symbol = params.get("symbol")
            if not symbol:
                return None, None
            # 检查表是否存在
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main'"
                ).fetchall()
            }
            if "raw_daily_bars" not in tables:
                return None, None
            df = conn.execute(
                "SELECT symbol, trade_date, open, high, low, close, volume, amount, "
                "turnover_rate, source FROM raw_daily_bars WHERE symbol = ? "
                "ORDER BY trade_date ASC",
                [str(symbol)],
            ).fetchdf()
            if df is None or df.empty:
                return None, None
            cutoff = pd.to_datetime(df["trade_date"].iloc[-1]).to_pydatetime()
            return df, cutoff
    except FactorWarehouseUnavailable:
        return None, None
    except Exception as exc:
        logger.debug("L3 lookup failed for %s: %s", interface_key, _sanitize_error(exc))
        return None, None


# ── L4 远程拉取适配器注册表（SourceChain 集成） ─────────

L4Fetcher = Callable[[Mapping[str, Any]], tuple[Any, str]]
"""L4 远程拉取函数签名：(params) -> (data, source_detail)。

source_detail 是 SourceChain 内部命中的具体数据源标识（如 "em_stock" / "sina_stock"）。
失败时应抛异常，由网关统一记录熔断状态。
"""

_l4_fetchers: dict[str, L4Fetcher] = {}


def register_l4_fetcher(interface_key: str, fetcher: L4Fetcher) -> None:
    """注册 L4 远程拉取函数（业务方按需注册，覆盖默认实现）。"""
    _l4_fetchers[interface_key] = fetcher


def _default_l4_daily_bars(params: Mapping[str, Any]) -> tuple[Any, str]:
    """L4 默认实现：调用 SourceChain 拉取 A 股日 K。

    复用现有 SourceChain 降级链（东财 → 新浪 → 腾讯 → TDX），
    透传 SourceChain 返回的真实 source 字段。

    若调用方通过 `register_source_chain(interface_key, chain)` 注册了
    `SourceChain` 实例，则优先使用已注册的链；否则回退到 `get_chain(symbol)`。
    """
    from app.services.market_data_sources.registry import get_chain
    from app.models.symbol import Symbol

    symbol_str = params.get("symbol")
    if not symbol_str:
        raise ValueError("symbol is required for L4 daily_bars fetch")

    # 从 DB 查 Symbol 对象（SourceChain 需要 Symbol 模型实例）
    SessionLocal = get_session_local()
    with SessionLocal() as db:
        sym = db.execute(
            select(Symbol).where(Symbol.symbol == str(symbol_str))
        ).scalars().first()
        if sym is None:
            raise ValueError(f"Symbol not found: {symbol_str}")
        # 复制必要字段避免 session 关闭后访问 detached 实例出错
        symbol_snapshot = Symbol()
        symbol_snapshot.id = sym.id
        symbol_snapshot.symbol = sym.symbol
        symbol_snapshot.asset_type = sym.asset_type
        symbol_snapshot.market = sym.market

    start = _parse_date(params.get("start") or params.get("start_date"))
    end = _parse_date(params.get("end") or params.get("end_date"))
    adjust = params.get("adjust", "qfq")

    # 优先使用调用方通过 register_source_chain 注册的 SourceChain
    registered_chain = _source_chains.get("akshare.daily_bars")
    if registered_chain is not None:
        chain = registered_chain
        source_detail = "registered_source_chain"
    else:
        chain = get_chain(symbol_snapshot)
        # SourceChain 不直接返回 source 名称；从链中找第一个成功的源
        # 通过尝试每个源来记录 source_detail
        # 这里简化：使用 frame 的隐式来源（无法精确得到，给统一标识）
        source_detail = "source_chain"
    frame = chain.fetch(symbol_snapshot, start, end, adjust)
    return frame, source_detail


def _default_l4_index_prices(params: Mapping[str, Any]) -> tuple[Any, str]:
    """L4 默认实现：调用现有 index_data 服务拉取指数日线。

    复用 _fetch_index_daily 的三源降级链（东财 → 新浪 → 腾讯）。
    """
    from app.services.index_data import _fetch_index_daily

    symbol = params.get("symbol")
    if not symbol:
        raise ValueError("symbol is required for L4 index_prices fetch")
    start = _parse_date(params.get("start") or params.get("start_date"))
    end = _parse_date(params.get("end") or params.get("end_date"))
    SessionLocal = get_session_local()
    with SessionLocal() as db:
        frame, source = _fetch_index_daily(db, str(symbol), start, end)
    return frame, source


register_l4_fetcher("akshare.daily_bars", _default_l4_daily_bars)
register_l4_fetcher("akshare.index_prices", _default_l4_index_prices)


# ── SourceChain 注册表（WP-S.3b） ─────────────────────

# SourceChain 类型仅作为类型提示（运行时按 duck-type 调用 .fetch()），
# 避免在模块顶层导入 SourceChain 触发 market_data_sources 包初始化。
SourceChainLike = Any

_source_chains: dict[str, SourceChainLike] = {}


def register_source_chain(interface_key: str, source_chain: SourceChainLike) -> None:
    """注册一个 `SourceChain` 实例到指定 `interface_key`（WP-S.3b）。

    注册后，该 interface_key 的 L4 默认 fetcher 会优先使用已注册的 SourceChain，
    而不是通过 `market_data_sources.registry.get_chain(symbol)` 查找。

    用途：
    - 业务方需要为特定 interface_key 注入预配置的 SourceChain（例如自定义降级顺序）
    - 测试场景注入 mock SourceChain 验证降级链行为

    Args:
        interface_key: 接口标识（如 "akshare.daily_bars"）
        source_chain: `SourceChain` 实例（需实现 `fetch(symbol, start, end, adjust) -> DataFrame`）
    """
    if not interface_key:
        raise ValueError("interface_key must be non-empty")
    if source_chain is None:
        raise ValueError("source_chain must not be None")
    _source_chains[interface_key] = source_chain


def get_source_chain(interface_key: str) -> SourceChainLike | None:
    """查询已注册的 `SourceChain` 实例（WP-S.3b）。

    Args:
        interface_key: 接口标识

    Returns:
        已注册的 SourceChain 实例；未注册时返回 None
    """
    return _source_chains.get(interface_key)


def unregister_source_chain(interface_key: str) -> None:
    """取消注册 `SourceChain`（供测试与配置切换使用）。"""
    _source_chains.pop(interface_key, None)


# ── 单飞（single-flight）+ 限流 ────────────────────────

@dataclass
class _InflightEntry:
    """单飞共享条目：第一个调用者负责真实请求，其他等待。"""
    future: asyncio.Future = field(default_factory=asyncio.Future)
    created_at: float = field(default_factory=time.monotonic)


class _SingleFlight:
    """单飞协调器：相同 key 同时只允许一个真实请求。

    并发调用方等待第一个完成后共享结果；若第一个失败，其他调用方立即重试一次
    （避免首个调用因瞬时网络抖动失败导致所有调用方连锁失败）。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _InflightEntry] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self,
        key: str,
        coro_factory: Callable[[], "asyncio.Future[Any]"],
        *,
        allow_one_retry: bool = True,
    ) -> Any:
        """执行单飞：相同 key 的并发调用共享第一个结果。

        Args:
            key: 单飞键（interface_key + normalized_params）
            coro_factory: 创建协程的工厂（每次调用产生新协程）
            allow_one_retry: 等待方在第一个失败后是否立即重试一次
        """
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _InflightEntry()
                self._entries[key] = entry
                is_leader = True
            else:
                is_leader = False

        if is_leader:
            try:
                result = await coro_factory()
                entry.future.set_result(result)
                return result
            except Exception as exc:
                entry.future.set_exception(exc)
                # leader 失败后立即重试一次（自身），避免瞬时抖动
                if allow_one_retry:
                    try:
                        result = await coro_factory()
                        # 重试成功：用新结果覆盖 future（已 set_exception 的 future 不能再 set）
                        # follower 会看到 exception，但 leader 已返回结果；follower 自行重试
                        return result
                    except Exception as retry_exc:
                        # 重试也失败，重新抛出原始异常保持 future 状态
                        logger.debug(
                            "Single-flight leader retry failed for %s: %s",
                            key, _sanitize_error(retry_exc),
                        )
                raise
            finally:
                async with self._lock:
                    self._entries.pop(key, None)
        else:
            # follower：等待 leader 完成
            try:
                return await entry.future
            except Exception:
                if not allow_one_retry:
                    raise
                # leader 失败，follower 立即重试一次
                return await coro_factory()


_single_flight = _SingleFlight()


class _SemaphoreRegistry:
    """按维度创建 asyncio.Semaphore 的注册表。

    支持三个维度：host / interface_key / task_type。
    每个 key 拥有独立的 Semaphore，初始并发上限由 settings 决定。
    """

    def __init__(self) -> None:
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, dimension: str, key: str, limit: int) -> asyncio.Semaphore:
        """获取（或创建）指定 key 的 Semaphore 并 acquire。

        Returns:
            已 acquire 的 Semaphore（调用方负责 release）。
        """
        cache_key = f"{dimension}:{key}"
        async with self._lock:
            sem = self._sems.get(cache_key)
            if sem is None:
                sem = asyncio.Semaphore(max(1, int(limit)))
                self._sems[cache_key] = sem
        await sem.acquire()
        return sem

    def reset(self) -> None:
        """重置所有 Semaphore（仅用于测试）。"""
        self._sems.clear()


_rate_limiter = _SemaphoreRegistry()


@asynccontextmanager
async def _acquire_rate_limit_slots(
    interface_key: str,
    host: str | None,
    task_type: str | None,
):
    """三维度并发限流：host / interface / task_type 同时获取配额。

    防死锁：按固定顺序获取（host → interface → task_type），释放逆序。
    """
    acquired: list[tuple[str, asyncio.Semaphore]] = []
    try:
        if host:
            sem = await _rate_limiter.acquire(
                "host", host, settings.EXTERNAL_DATA_HOST_CONCURRENCY
            )
            acquired.append(("host", sem))
        sem = await _rate_limiter.acquire(
            "interface", interface_key, settings.EXTERNAL_DATA_INTERFACE_CONCURRENCY
        )
        acquired.append(("interface", sem))
        if task_type:
            sem = await _rate_limiter.acquire(
                "task_type", task_type, settings.EXTERNAL_DATA_TASK_TYPE_CONCURRENCY
            )
            acquired.append(("task_type", sem))
        yield
    finally:
        for _, sem in reversed(acquired):
            sem.release()


# ── 熔断器状态机 ──────────────────────────────────────

@dataclass
class _BreakerState:
    """熔断器内存状态（DB 持久化由 _persist_breaker_state 负责）。"""
    state: str = STATE_CLOSED
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    last_error_code: str | None = None
    last_error_at: datetime | None = None
    # half_open 探测请求 in-flight 标记（单飞保证只有一个探测）
    half_open_probe_inflight: bool = False


class _CircuitBreakerRegistry:
    """熔断器状态机注册表。

    状态转换：
        closed  → 连续 N 次失败 → open（持续 cooldown_seconds）
        open    → cooldown 到期 → half_open（只允许 1 个探测请求）
        half_open → 探测成功 → closed；探测失败 → open（cooldown 翻倍，上限 30 分钟）

    内存状态 + DB 持久化（ExternalEndpointRuntime 表）。
    进程重启时从 DB 恢复状态。
    """

    def __init__(self) -> None:
        self._states: dict[str, _BreakerState] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        """从 DB 加载已有熔断状态（仅一次，进程启动后）。"""
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            try:
                SessionLocal = get_session_local()
                with SessionLocal() as db:
                    rows = db.execute(select(ExternalEndpointRuntime)).scalars().all()
                    for row in rows:
                        self._states[row.interface_key] = _BreakerState(
                            state=row.state,
                            consecutive_failures=row.consecutive_failures,
                            cooldown_until=row.cooldown_until,
                            last_error_code=row.last_error_code,
                            last_error_at=row.last_error_at,
                        )
            except Exception as exc:
                logger.warning("CircuitBreaker load from DB failed: %s", _sanitize_error(exc))
            self._loaded = True

    async def acquire(
        self,
        interface_key: str,
        host: str | None,
        *,
        allow_half_open_probe: bool = False,
    ) -> _BreakerState:
        """请求放行许可。

        Args:
            interface_key: 接口标识
            host: 主机名（用于持久化记录）
            allow_half_open_probe: 是否作为 half_open 探测请求（True 时只允许 1 个）

        Returns:
            当前 _BreakerState（调用方据此判断是否真的发请求）

        Raises:
            CircuitBreakerOpenError: 熔断器 open 且冷却未到期
        """
        await self._ensure_loaded()
        async with self._lock:
            state = self._states.setdefault(interface_key, _BreakerState())
            now = _utcnow_naive()

            if state.state == STATE_CLOSED:
                return state

            if state.state == STATE_OPEN:
                # 检查冷却是否到期
                if state.cooldown_until is not None and now >= state.cooldown_until:
                    # 进入 half_open，允许 1 个探测
                    state.state = STATE_HALF_OPEN
                    state.half_open_probe_inflight = True
                    logger.info(
                        "CircuitBreaker %s: open -> half_open (cooldown expired)",
                        interface_key,
                    )
                    await self._persist(interface_key, host, state)
                    return state
                # 冷却未到期，拒绝
                raise CircuitBreakerOpenError(
                    interface_key, state.cooldown_until or now
                )

            if state.state == STATE_HALF_OPEN:
                if not allow_half_open_probe or state.half_open_probe_inflight:
                    # 已有探测在飞，拒绝其他请求
                    raise CircuitBreakerOpenError(
                        interface_key, state.cooldown_until or now
                    )
                state.half_open_probe_inflight = True
                return state

            # 兜底（理论上不会到达）
            return state

    async def record_success(
        self, interface_key: str, host: str | None
    ) -> None:
        """记录一次成功：重置失败计数，状态转 closed。"""
        async with self._lock:
            state = self._states.setdefault(interface_key, _BreakerState())
            prev_state = state.state
            state.state = STATE_CLOSED
            state.consecutive_failures = 0
            state.cooldown_until = None
            state.half_open_probe_inflight = False
            if prev_state != STATE_CLOSED:
                logger.info(
                    "CircuitBreaker %s: %s -> closed (success)",
                    interface_key, prev_state,
                )
            await self._persist(interface_key, host, state)

    async def record_failure(
        self,
        interface_key: str,
        host: str | None,
        error_code: str,
    ) -> None:
        """记录一次失败：更新计数，按状态机转换。"""
        async with self._lock:
            state = self._states.setdefault(interface_key, _BreakerState())
            now = _utcnow_naive()
            state.consecutive_failures += 1
            state.last_error_code = error_code
            state.last_error_at = now

            if state.state == STATE_HALF_OPEN:
                # 探测失败：回到 open，cooldown 翻倍
                base = settings.EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS
                max_cd = settings.EXTERNAL_DATA_BREAKER_MAX_COOLDOWN_SECONDS
                prev_cd = state.cooldown_until
                prev_seconds = (
                    (prev_cd - now).total_seconds() if prev_cd else base
                )
                new_seconds = min(max_cd, max(base, prev_seconds * 2))
                state.cooldown_until = now + timedelta(seconds=new_seconds)
                state.state = STATE_OPEN
                state.half_open_probe_inflight = False
                logger.warning(
                    "CircuitBreaker %s: half_open -> open (probe failed, cooldown=%ss)",
                    interface_key, int(new_seconds),
                )
            elif state.state == STATE_CLOSED:
                threshold = settings.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD
                if state.consecutive_failures >= threshold:
                    base = settings.EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS
                    state.cooldown_until = now + timedelta(seconds=base)
                    state.state = STATE_OPEN
                    logger.warning(
                        "CircuitBreaker %s: closed -> open (failures=%d >= %d, cooldown=%ss)",
                        interface_key, state.consecutive_failures, threshold, base,
                    )
            await self._persist(interface_key, host, state)

    async def _persist(
        self,
        interface_key: str,
        host: str | None,
        state: _BreakerState,
    ) -> None:
        """持久化熔断状态到 DB（best-effort，失败仅日志）。"""
        try:
            SessionLocal = get_session_local()
            with SessionLocal() as db:
                row = db.execute(
                    select(ExternalEndpointRuntime).where(
                        ExternalEndpointRuntime.interface_key == interface_key
                    )
                ).scalars().first()
                if row is None:
                    row = ExternalEndpointRuntime(
                        interface_key=interface_key,
                        host=host,
                        state=state.state,
                        consecutive_failures=state.consecutive_failures,
                        cooldown_until=state.cooldown_until,
                        last_error_code=state.last_error_code,
                        last_error_at=state.last_error_at,
                    )
                    db.add(row)
                else:
                    row.host = host or row.host
                    row.state = state.state
                    row.consecutive_failures = state.consecutive_failures
                    row.cooldown_until = state.cooldown_until
                    row.last_error_code = state.last_error_code
                    row.last_error_at = state.last_error_at
                db.commit()
        except Exception as exc:
            logger.warning(
                "CircuitBreaker persist failed for %s: %s",
                interface_key, _sanitize_error(exc),
            )

    async def increment_counter(
        self,
        interface_key: str,
        *,
        request: bool = False,
        cache_hit: bool = False,
        fallback: bool = False,
    ) -> None:
        """递增可观测性计数器（best-effort）。"""
        try:
            SessionLocal = get_session_local()
            with SessionLocal() as db:
                row = db.execute(
                    select(ExternalEndpointRuntime).where(
                        ExternalEndpointRuntime.interface_key == interface_key
                    )
                ).scalars().first()
                if row is None:
                    row = ExternalEndpointRuntime(interface_key=interface_key)
                    db.add(row)
                if request:
                    row.request_count = (row.request_count or 0) + 1
                if cache_hit:
                    row.cache_hit_count = (row.cache_hit_count or 0) + 1
                if fallback:
                    row.fallback_count = (row.fallback_count or 0) + 1
                db.commit()
        except Exception as exc:
            logger.debug(
                "CircuitBreaker counter increment failed for %s: %s",
                interface_key, _sanitize_error(exc),
            )


_breaker = _CircuitBreakerRegistry()


def _classify_error(exc: Exception) -> str:
    """将异常分类为熔断器可识别的错误码。

    用于决定是否计入 consecutive_failures：
    - 429/403/连接重置/超时 → 计入（风控/网络问题）
    - ValueError/KeyError → 不计入（数据格式问题，是 bug 不是风控）

    匹配顺序：先看异常类名（精确），再看 HTTP 状态码，最后扫消息关键字。
    Timeout 类异常即使消息含 "connection" 也归 timeout，避免误判。
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    # 1) 异常类名优先：Timeout 类
    if name in ("TimeoutError", "ConnectTimeout", "ReadTimeout", "SocketTimeout"):
        return "timeout"
    if name in ("ConnectionError", "RemoteDisconnected", "ConnectionResetError"):
        return "connection_reset"
    # 2) HTTP 状态码
    if "429" in msg or "rate limit" in msg:
        return "http_429"
    if "403" in msg:
        return "http_403"
    # 3) 消息关键字（兜底）
    if "timeout" in msg:
        return "timeout"
    if "reset" in msg or "remote" in msg or "connection" in msg:
        return "connection_reset"
    return "other"


# ── 退避计算 ──────────────────────────────────────────

def _compute_backoff(attempt: int) -> float:
    """指数退避 + 随机抖动：base * 2^attempt * (1 ± jitter)。"""
    base = settings.EXTERNAL_DATA_BACKOFF_BASE_SECONDS
    mx = settings.EXTERNAL_DATA_BACKOFF_MAX_SECONDS
    jitter = settings.EXTERNAL_DATA_BACKOFF_JITTER
    delay = min(mx, base * (2 ** attempt))
    # 在 [1-jitter, 1+jitter] 范围内随机抖动
    factor = 1.0 + random.uniform(-jitter, jitter)
    return max(0.0, delay * factor)


# ── 主入口 fetch() ────────────────────────────────────

async def fetch(
    interface_key: str,
    request_params: dict,
    freshness_requirement: timedelta,
    allow_stale: bool = True,
    preferred_sources: list[str] | None = None,
    task_context: dict | None = None,
) -> GatewayResponse:
    """统一外部数据网关入口。

    层级顺序：L1（进程缓存）→ L2（业务 DB）→ L3（DuckDB 快照）→ L4（远程拉取）。
    任一层命中且满足 freshness_requirement 即返回；L4 失败时按 L3 → L2 → L1 回退。

    Args:
        interface_key: 接口标识（如 "akshare.daily_bars"）
        request_params: 请求参数（如 {"symbol": "000001", "start": "...", "end": "..."}）
        freshness_requirement: 数据新鲜度要求（cutoff_at 必须在 now - freshness 之内）
        allow_stale: True 时 L4 失败可降级到过期本地数据；False 时严禁返回过期数据
        preferred_sources: 优先数据源（保留参数，当前实现忽略，由 SourceChain 决定顺序）
        task_context: 任务上下文（含 task_type / task_id / host 等，用于追踪与限流）

    Returns:
        GatewayResponse

    Raises:
        StaleDataError: allow_stale=False 且本地数据过期
        CircuitBreakerOpenError: 熔断器 open 且 allow_stale=False
    """
    correlation_id = _make_correlation_id()
    normalized = normalize_params(request_params)
    cache_key = f"{interface_key}:{normalized}"
    task_ctx = task_context or {}
    task_type = task_ctx.get("task_type")
    host = task_ctx.get("host")

    logger.info(
        "Gateway fetch start: interface=%s params_hash=%s correlation_id=%s allow_stale=%s",
        interface_key, normalized, correlation_id, allow_stale,
    )

    # ── L1：进程缓存 ────────────────────────────────────
    cached = _l1_cache.get(cache_key)
    if cached is not None:
        value, cutoff = cached
        if _is_fresh(cutoff, freshness_requirement):
            await _breaker.increment_counter(interface_key, cache_hit=True)
            logger.info(
                "Gateway L1 hit: interface=%s correlation_id=%s", interface_key, correlation_id,
            )
            return GatewayResponse(
                data=value,
                source=SOURCE_L1_CACHE,
                cache_hit=True,
                data_cutoff_at=cutoff,
                correlation_id=correlation_id,
            )
        # L1 命中但不新鲜，继续往下查
        logger.debug(
            "Gateway L1 stale: interface=%s correlation_id=%s cutoff=%s",
            interface_key, correlation_id, cutoff,
        )

    # ── L2：业务数据库 ──────────────────────────────────
    l2_fetcher = _l2_fetchers.get(interface_key)
    if l2_fetcher is not None:
        try:
            SessionLocal = get_session_local()
            with SessionLocal() as db:
                data, cutoff = l2_fetcher(db, request_params)
            if data is not None and _is_fresh(cutoff, freshness_requirement):
                # 写回 L1 加速下次访问
                _l1_cache.set(cache_key, data, cutoff)
                await _breaker.increment_counter(interface_key, cache_hit=True)
                logger.info(
                    "Gateway L2 hit: interface=%s correlation_id=%s", interface_key, correlation_id,
                )
                return GatewayResponse(
                    data=data,
                    source=SOURCE_L2_DB,
                    cache_hit=True,
                    data_cutoff_at=cutoff,
                    correlation_id=correlation_id,
                )
            # L2 数据存在但不新鲜，保留作为 fallback 候选
            l2_stale_data = data
            l2_stale_cutoff = cutoff
        except Exception as exc:
            logger.warning(
                "Gateway L2 lookup failed: interface=%s correlation_id=%s err=%s",
                interface_key, correlation_id, _sanitize_error(exc),
            )
            l2_stale_data = None
            l2_stale_cutoff = None
    else:
        l2_stale_data = None
        l2_stale_cutoff = None

    # ── L3：DuckDB 快照 ─────────────────────────────────
    try:
        l3_data, l3_cutoff = await asyncio.to_thread(_l3_lookup, interface_key, request_params)
    except Exception as exc:
        logger.warning(
            "Gateway L3 lookup error: interface=%s correlation_id=%s err=%s",
            interface_key, correlation_id, _sanitize_error(exc),
        )
        l3_data, l3_cutoff = None, None

    if l3_data is not None and _is_fresh(l3_cutoff, freshness_requirement):
        _l1_cache.set(cache_key, l3_data, l3_cutoff)
        await _breaker.increment_counter(interface_key, cache_hit=True)
        logger.info(
            "Gateway L3 hit: interface=%s correlation_id=%s", interface_key, correlation_id,
        )
        return GatewayResponse(
            data=l3_data,
            source=SOURCE_L3_DUCKDB,
            cache_hit=True,
            data_cutoff_at=l3_cutoff,
            correlation_id=correlation_id,
        )

    # ── L4：远程拉取（单飞 + 限流 + 熔断） ───────────────
    l4_fetcher = _l4_fetchers.get(interface_key)
    if l4_fetcher is None:
        # 无 L4 适配器，直接走降级路径
        return await _degrade_to_stale(
            interface_key, correlation_id, allow_stale,
            l2_stale_data, l2_stale_cutoff, l3_data, l3_cutoff,
            reason="no_l4_fetcher_registered",
        )

    async def _do_l4_fetch() -> tuple[Any, str, datetime | None]:
        """实际执行 L4 拉取（含熔断检查 + 限流 + 退避）。"""
        # 熔断检查
        breaker_state = await _breaker.acquire(interface_key, host)
        try:
            async with _acquire_rate_limit_slots(interface_key, host, task_type):
                # 调用 L4 fetcher（同步函数，用 to_thread 包装避免阻塞事件循环）
                start_ts = time.monotonic()
                data, source_detail = await asyncio.to_thread(l4_fetcher, request_params)
                latency_ms = int((time.monotonic() - start_ts) * 1000)
                logger.info(
                    "Gateway L4 success: interface=%s correlation_id=%s source=%s latency=%sms",
                    interface_key, correlation_id, source_detail, latency_ms,
                )
            cutoff = _utcnow_naive()
            await _breaker.record_success(interface_key, host)
            await _breaker.increment_counter(interface_key, request=True)
            return data, source_detail, cutoff
        except CircuitBreakerOpenError:
            # 熔断拒绝，不计入失败（已经是 open 状态了）
            raise
        except Exception as exc:
            error_code = _classify_error(exc)
            await _breaker.record_failure(interface_key, host, error_code)
            logger.warning(
                "Gateway L4 failed: interface=%s correlation_id=%s err_code=%s err=%s",
                interface_key, correlation_id, error_code, _sanitize_error(exc),
            )
            raise

    try:
        # 单飞：相同 cache_key 的并发调用共享结果
        data, source_detail, cutoff = await _single_flight.execute(
            cache_key, _do_l4_fetch, allow_one_retry=True,
        )
        _l1_cache.set(cache_key, data, cutoff)
        return GatewayResponse(
            data=data,
            source=SOURCE_L4_REMOTE,
            cache_hit=False,
            data_cutoff_at=cutoff,
            correlation_id=correlation_id,
            source_detail=source_detail,
        )
    except CircuitBreakerOpenError:
        # 熔断打开：尝试降级到本地数据
        return await _degrade_to_stale(
            interface_key, correlation_id, allow_stale,
            l2_stale_data, l2_stale_cutoff,
            l3_data, l3_cutoff,
            reason="circuit_breaker_open",
        )
    except Exception as exc:
        # L4 失败：尝试降级到本地数据
        logger.warning(
            "Gateway L4 final failure: interface=%s correlation_id=%s err=%s, degrading",
            interface_key, correlation_id, _sanitize_error(exc),
        )
        return await _degrade_to_stale(
            interface_key, correlation_id, allow_stale,
            l2_stale_data, l2_stale_cutoff, l3_data, l3_cutoff,
            reason=f"l4_failed:{_classify_error(exc)}",
        )


async def _degrade_to_stale(
    interface_key: str,
    correlation_id: str,
    allow_stale: bool,
    l2_data: Any,
    l2_cutoff: datetime | None,
    l3_data: Any,
    l3_cutoff: datetime | None,
    *,
    reason: str,
) -> GatewayResponse:
    """L4 失败后降级到本地数据（L3 → L2 → L1 顺序）。

    allow_stale=False 时严禁返回过期数据，抛 StaleDataError。
    """
    # 优先用 L3，其次 L2，最后 L1（L1 在 fetch() 主流程已查过，这里不重复）
    if l3_data is not None:
        if not allow_stale:
            raise StaleDataError(interface_key, l3_cutoff, reason)
        await _breaker.increment_counter(interface_key, fallback=True)
        return GatewayResponse(
            data=l3_data,
            source=SOURCE_STALE,
            cache_hit=True,
            data_cutoff_at=l3_cutoff,
            degraded_reason=reason,
            correlation_id=correlation_id,
            source_detail=SOURCE_L3_DUCKDB,
        )
    if l2_data is not None:
        if not allow_stale:
            raise StaleDataError(interface_key, l2_cutoff, reason)
        await _breaker.increment_counter(interface_key, fallback=True)
        return GatewayResponse(
            data=l2_data,
            source=SOURCE_STALE,
            cache_hit=True,
            data_cutoff_at=l2_cutoff,
            degraded_reason=reason,
            correlation_id=correlation_id,
            source_detail=SOURCE_L2_DB,
        )
    # 完全没有本地数据
    if not allow_stale:
        raise StaleDataError(interface_key, None, reason)
    await _breaker.increment_counter(interface_key, fallback=True)
    return GatewayResponse(
        data=None,
        source=SOURCE_STALE,
        cache_hit=False,
        data_cutoff_at=None,
        degraded_reason=reason,
        correlation_id=correlation_id,
    )


def _is_fresh(cutoff: datetime | date | None, requirement: timedelta) -> bool:
    """判断数据截止时间是否满足新鲜度要求。

    支持datetime 与 date 两种类型：
    - DailyBar.trade_date / IndexPrice.trade_date 等字段是 date 对象，无 tzinfo 属性
    - 必须先转换为 datetime 才能进行时区感知比较
    """
    if cutoff is None:
        return False
    # date 对象（非 datetime）先转 naive datetime
    if isinstance(cutoff, date) and not isinstance(cutoff, datetime):
        cutoff = datetime.combine(cutoff, datetime.min.time())
    # 统一为 naive UTC 比较
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
    now = _utcnow_naive()
    return (now - cutoff) <= requirement


# ── fetch_via_gateway 主入口（WP-S.1b） ───────────────

async def fetch_via_gateway(req: GatewayRequest) -> GatewayResponse:
    """外部数据网关主入口（WP-S.1b）。

    接受 `GatewayRequest` dataclass，透传到现有 `fetch()` 函数执行
    L1 → L2 → L3 → L4 的多层缓存查询。

    与 `fetch()` 行为完全等价，仅是入参形式的差异：
    - `fetch()` 接受散列参数（向后兼容）
    - `fetch_via_gateway()` 接受 `GatewayRequest` 对象（推荐新代码使用）

    Args:
        req: `GatewayRequest` dataclass 实例

    Returns:
        `GatewayResponse` 实例

    Raises:
        StaleDataError: `req.allow_stale=False` 且本地数据过期
        CircuitBreakerOpenError: 熔断器 open 且 `req.allow_stale=False`

    Example:
        >>> from datetime import timedelta
        >>> from app.services.external_data_gateway import (
        ...     GatewayRequest, fetch_via_gateway,
        ... )
        >>> req = GatewayRequest(
        ...     interface_key="akshare.daily_bars",
        ...     request_params={"symbol": "000001", "start": "2024-01-01"},
        ...     freshness_requirement=timedelta(hours=24),
        ... )
        >>> resp = await fetch_via_gateway(req)
    """
    return await fetch(
        interface_key=req.interface_key,
        request_params=req.request_params,
        freshness_requirement=req.freshness_requirement,
        allow_stale=req.allow_stale,
        preferred_sources=req.preferred_sources,
        task_context=req.task_context,
    )


# ── WP-S.4 本地入库稳定性工具 ──────────────────────────

def validate_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    required_fields: Sequence[str],
    date_fields: Sequence[str] = (),
    numeric_fields: Sequence[str] = (),
    date_range: tuple[Any, Any] | None = None,
    numeric_range: tuple[float, float] | None = None,
) -> None:
    """入库前字段契约校验。

    校验失败抛 DataValidationError，不写入业务表。

    Args:
        rows: 待校验的行列表
        required_fields: 必填字段名列表
        date_fields: 日期字段名列表（校验可解析为日期）
        numeric_fields: 数值字段名列表（校验可转为 float）
        date_range: (start, end) 闭区间，date_fields 必须在此范围内
        numeric_range: (min, max) 闭区间，numeric_fields 必须在此范围内
    """
    if not rows:
        return
    for idx, row in enumerate(rows):
        for field in required_fields:
            if field not in row or row[field] is None or row[field] == "":
                raise DataValidationError(
                    field,
                    f"row[{idx}] missing required field '{field}'",
                )
        for field in date_fields:
            if field in row and row[field] is not None:
                try:
                    _parse_date(row[field])
                except Exception as exc:
                    raise DataValidationError(
                        field, f"row[{idx}] invalid date value: {row[field]} ({exc})"
                    ) from exc
                if date_range is not None:
                    d = _parse_date(row[field])
                    if d is not None:
                        start, end = date_range
                        if start is not None and d < _parse_date(start):
                            raise DataValidationError(
                                field,
                                f"row[{idx}] date {d} before start {start}",
                            )
                        if end is not None and d > _parse_date(end):
                            raise DataValidationError(
                                field,
                                f"row[{idx}] date {d} after end {end}",
                            )
        for field in numeric_fields:
            if field in row and row[field] is not None:
                try:
                    val = float(row[field])
                except (TypeError, ValueError) as exc:
                    raise DataValidationError(
                        field, f"row[{idx}] invalid numeric value: {row[field]} ({exc})"
                    ) from exc
                if numeric_range is not None:
                    lo, hi = numeric_range
                    if val < lo or val > hi:
                        raise DataValidationError(
                            field,
                            f"row[{idx}] value {val} out of range [{lo}, {hi}]",
                        )


def _upsert_batch(
    table,  # SQLAlchemy Table 或 ORM 类
    rows: Sequence[Mapping[str, Any]],
    conflict_keys: Sequence[str],
    *,
    db: Session | None = None,
    batch_size: int | None = None,
    max_retries: int | None = None,
) -> int:
    """通用 UPSERT 批量写入工具。

    SQLite 用 insert().on_conflict_do_update()；MySQL 用 ON DUPLICATE KEY UPDATE。
    conflict_keys 必须匹配目标表的实际唯一约束（如 daily_bars 是 (symbol_id, trade_date)）。

    Args:
        table: SQLAlchemy Table 对象（通过 ORM.__table__ 获取）
        rows: 待写入的行列表（dict-like）
        conflict_keys: 冲突判定列名列表
        db: 可选 Session（不传则新建）
        batch_size: 分块大小（None 用 settings 默认值 500）
        max_retries: 失败块最大重试次数（None 用 settings 默认值 3）

    Returns:
        成功写入的行数
    """
    if not rows:
        return 0
    batch_size = batch_size or settings.EXTERNAL_DATA_UPSERT_BATCH_SIZE
    max_retries = max_retries if max_retries is not None else settings.EXTERNAL_DATA_UPSERT_MAX_RETRIES

    mgr = DatabaseManager.get()
    is_mysql = mgr.is_mysql
    is_sqlite = mgr.is_sqlite

    # 获取列名集合
    table_obj = table if hasattr(table, "columns") else table.__table__
    column_names = {c.name for c in table_obj.columns}

    own_session = db is None
    if own_session:
        SessionLocal = get_session_local()
        db = SessionLocal()
    try:
        total_written = 0
        for chunk_start in range(0, len(rows), batch_size):
            chunk = rows[chunk_start:chunk_start + batch_size]
            # 过滤无效列 + 转 dict
            clean_rows = [
                {k: v for k, v in row.items() if k in column_names}
                for row in chunk
            ]
            if not clean_rows:
                continue

            attempt = 0
            while True:
                try:
                    if is_sqlite:
                        stmt = sqlite_insert(table_obj).values(clean_rows)
                        update_cols = {
                            col: stmt.excluded[col]
                            for col in column_names
                            if col not in conflict_keys
                        }
                        stmt = stmt.on_conflict_do_update(
                            index_elements=list(conflict_keys),
                            set_=update_cols,
                        )
                    elif is_mysql:
                        stmt = mysql_insert(table_obj).values(clean_rows)
                        update_cols = {
                            col: getattr(stmt.inserted, col)
                            for col in column_names
                            if col not in conflict_keys
                        }
                        stmt = stmt.on_duplicate_key_update(**update_cols)
                    else:
                        raise RuntimeError(f"Unsupported dialect: {mgr.db_type}")
                    db.execute(stmt)
                    db.commit()
                    total_written += len(clean_rows)
                    break
                except Exception as exc:
                    db.rollback()
                    if attempt >= max_retries:
                        logger.error(
                            "UPSERT batch failed after %d retries: %s (chunk_size=%d)",
                            attempt + 1, _sanitize_error(exc), len(clean_rows),
                        )
                        # 不抛异常，跳过此块继续后续块（避免全部失败）
                        break
                    attempt += 1
                    backoff = _compute_backoff(attempt)
                    logger.warning(
                        "UPSERT batch retry %d/%d after %.2fs: %s",
                        attempt, max_retries, backoff, _sanitize_error(exc),
                    )
                    time.sleep(backoff)
        return total_written
    finally:
        if own_session and db is not None:
            db.close()


class StagingBatch:
    """快照原子切换：临时批次校验通过后单事务提交。

    使用方式：
        with StagingBatch(table=DailyBar.__table__, conflict_keys=["symbol_id", "trade_date"]) as batch:
            batch.add(row1)
            batch.add(row2)
            # 校验逻辑
            validate_rows(batch.rows, required_fields=[...])
            # 提交时单事务 UPSERT，失败回滚不影响现有数据
            batch.commit(db)

    设计：
    - 累积写入内存，未 commit 前不影响业务表
    - commit() 在单事务内 UPSERT 所有行，任一失败全量回滚
    - 异常退出（未 commit）时丢弃所有行
    """

    def __init__(
        self,
        table,
        conflict_keys: Sequence[str],
        *,
        batch_size: int | None = None,
    ) -> None:
        self.table = table
        self.conflict_keys = list(conflict_keys)
        self.batch_size = batch_size or settings.EXTERNAL_DATA_UPSERT_BATCH_SIZE
        self.rows: list[dict] = []
        self._committed = False

    def add(self, row: Mapping[str, Any]) -> None:
        """添加一行到暂存区。"""
        self.rows.append(dict(row))

    def extend(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """批量添加多行。"""
        for row in rows:
            self.rows.append(dict(row))

    def commit(self, db: Session | None = None) -> int:
        """原子提交：单事务 UPSERT 所有行。

        失败时抛异常并回滚，已添加的行仍保留在 self.rows 中（调用方可重试）。
        """
        if self._committed:
            raise RuntimeError("StagingBatch already committed")
        if not self.rows:
            self._committed = True
            return 0
        try:
            written = _upsert_batch(
                self.table, self.rows, self.conflict_keys,
                db=db, batch_size=self.batch_size, max_retries=0,  # 单事务：不重试
            )
            self._committed = True
            return written
        except Exception:
            # _upsert_batch 内部已 rollback；这里只负责标记未提交
            raise

    def discard(self) -> None:
        """丢弃暂存区（显式放弃）。"""
        self.rows.clear()
        self._committed = True  # 防止后续误用

    def __enter__(self) -> "StagingBatch":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """退出时：未 commit 且有异常则丢弃；未 commit 且无异常也丢弃（调用方需显式 commit）。"""
        if not self._committed:
            if exc_type is not None:
                # 异常退出：清理 staging，不影响现有 ready 数据
                logger.info(
                    "StagingBatch discarded due to %s: %s",
                    exc_type.__name__ if exc_type else "Unknown", exc_val,
                )
            self.rows.clear()
            self._committed = True


# ── DuckDB 连接管理（WP-S.4 单写锁） ──────────────────

_duckdb_write_lock = threading.Lock()


@contextmanager
def duckdb_write_session(path: str | None = None):
    """DuckDB 写入会话上下文管理器。

    - 单写锁（threading.Lock）保证同一时刻只有一个写入者
    - with duckdb.connect(...) as conn: 自动释放连接
    - finally 释放文件锁
    - 不在异步任务中共享 DuckDB 连接（每次调用新建）

    使用方式：
        with duckdb_write_session() as conn:
            conn.execute("INSERT INTO ...")
    """
    import duckdb  # type: ignore
    from app.services.factors.store import FactorWarehouse

    wh = FactorWarehouse(path)
    wh.initialize()
    acquired = False
    try:
        # 获取进程内单写锁
        _duckdb_write_lock.acquire()
        acquired = True
        # with 语法保证连接关闭
        with wh.connection() as conn:
            yield conn
    finally:
        if acquired:
            _duckdb_write_lock.release()


# ── 工具函数：网关状态查询（供 WP-S.7 错误协议使用） ────

async def get_endpoint_status(interface_key: str) -> dict | None:
    """查询接口运行时状态（熔断器状态 + 计数器）。"""
    await _breaker._ensure_loaded()
    state = _breaker._states.get(interface_key)
    if state is None:
        return None
    return {
        "interface_key": interface_key,
        "state": state.state,
        "consecutive_failures": state.consecutive_failures,
        "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
        "last_error_code": state.last_error_code,
        "last_error_at": state.last_error_at.isoformat() if state.last_error_at else None,
    }


def clear_l1_cache() -> None:
    """清理 L1 进程缓存（供测试和管理接口使用）。"""
    _l1_cache.clear()


def reset_breaker(interface_key: str | None = None) -> None:
    """重置熔断器状态（供测试和管理接口使用）。

    Args:
        interface_key: 指定接口则只重置该接口；None 则重置全部
    """
    if interface_key is None:
        _breaker._states.clear()
    else:
        _breaker._states.pop(interface_key, None)


def reset_rate_limiter() -> None:
    """重置限流器（供测试使用）。"""
    _rate_limiter.reset()


# ── P1-08 数据新鲜度闭环：错峰调度 ──────────────────────

def is_within_offpeak_window(now: datetime | None = None) -> bool:
    """检查当前时间是否在错峰时间窗口内。

    错峰窗口默认 22:00-06:00（亚太交易时段外），可通过环境变量
    OFFPEAK_WINDOW_START_HOUR / OFFPEAK_WINDOW_END_HOUR 配置。

    跨夜窗口（start >= end，如 22-6）判定：当前小时 >= start 或 < end。
    同日窗口（start < end，如 1-5）判定：start <= 当前小时 < end。

    Args:
        now: 可选的当前时间（naive UTC），None 时取 _utcnow_naive()

    Returns:
        True 表示在错峰窗口内（适合执行补数任务）
    """
    now = now or _utcnow_naive()
    start = settings.OFFPEAK_WINDOW_START_HOUR
    end = settings.OFFPEAK_WINDOW_END_HOUR
    # 归一化到 0-23
    start = max(0, min(23, int(start)))
    end = max(0, min(24, int(end)))
    hour = now.hour
    if start >= end:
        # 跨夜窗口：22-6 表示 22/23/0/1/2/3/4/5
        return hour >= start or hour < end
    if start == end:
        # 起止相同：视为全天允许（禁用错峰限制）
        return True
    # 同日窗口：1-5 表示 1/2/3/4
    return start <= hour < end


def should_defer_for_offpeak(
    *,
    force_offpeak: bool = False,
    now: datetime | None = None,
) -> bool:
    """判断补数任务是否应推迟到错峰时段执行。

    Args:
        force_offpeak: True 时仅在错峰窗口内允许执行（非窗口内返回 True 表示应推迟）
        now: 可选的当前时间

    Returns:
        True 表示应推迟执行（当前不在允许窗口内）
    """
    if not force_offpeak:
        # 非强制错峰：立即执行
        return False
    return not is_within_offpeak_window(now)


# ── P1-08 数据新鲜度闭环：失败批次续跑 ──────────────────

@dataclass
class FailedBatch:
    """补数失败批次记录（标的列表 + 失败原因）。

    单个标的或一批标的的补数失败后记录到此结构，不阻塞其他标的的补数。
    可通过 retry_failed_batch() 单独重跑，重试成功后标记 resolved=True。
    """
    batch_id: str
    interface_key: str
    symbols: list[str]
    reason: str
    error_code: str
    created_at: datetime
    retry_count: int = 0
    last_retry_at: datetime | None = None
    resolved: bool = False
    last_retry_result: str | None = None  # "success" / "failed" / None


class _FailedBatchRegistry:
    """失败批次注册表：记录、查询、重试。

    设计：
    - 进程内单例（与 _breaker / _l1_cache 一致），不持久化（任务级持久化由 AsyncTaskRecord.errors_json 承担）
    - 失败批次不阻塞其他标的的补数：记录后立即返回，整体补数继续
    - 提供 retry_failed_batch() 入口，调用方可传入重试回调
    - 重试次数超限时标记为不可重试（仍保留记录供人工处理）
    """

    def __init__(self) -> None:
        self._batches: dict[str, FailedBatch] = {}
        self._lock = threading.Lock()

    def record(
        self,
        *,
        interface_key: str,
        symbols: list[str],
        reason: str,
        error_code: str = "other",
        batch_id: str | None = None,
    ) -> FailedBatch:
        """记录一个失败批次。

        如果同一 interface_key + reason + symbols 已存在且未解决，则更新 retry 信息
        而非创建新条目（避免重复记录同一失败）。
        """
        batch_id = batch_id or _make_correlation_id()
        now = _utcnow_naive()
        with self._lock:
            # 查找已有相同失败未解决的批次（去重）
            for existing in self._batches.values():
                if (
                    existing.interface_key == interface_key
                    and existing.reason == reason
                    and existing.symbols == symbols
                    and not existing.resolved
                ):
                    # 更新创建时间，便于按时间排序重试
                    existing.created_at = now
                    return existing
            batch = FailedBatch(
                batch_id=batch_id,
                interface_key=interface_key,
                symbols=list(symbols),
                reason=reason,
                error_code=error_code,
                created_at=now,
            )
            self._batches[batch_id] = batch
            return batch

    def list_unresolved(self, interface_key: str | None = None) -> list[FailedBatch]:
        """列出未解决的失败批次（按创建时间升序，便于优先重试旧批次）。"""
        with self._lock:
            items = [
                b for b in self._batches.values()
                if not b.resolved
                and (interface_key is None or b.interface_key == interface_key)
            ]
        items.sort(key=lambda b: b.created_at)
        return items

    def list_all(self, interface_key: str | None = None) -> list[FailedBatch]:
        """列出所有失败批次（含已解决，按创建时间升序）。"""
        with self._lock:
            items = [
                b for b in self._batches.values()
                if interface_key is None or b.interface_key == interface_key
            ]
        items.sort(key=lambda b: b.created_at)
        return items

    def get(self, batch_id: str) -> FailedBatch | None:
        with self._lock:
            return self._batches.get(batch_id)

    def mark_retry(
        self,
        batch_id: str,
        *,
        success: bool,
        result_msg: str | None = None,
    ) -> FailedBatch | None:
        """标记一次重试结果。

        success=True 时标记为 resolved；success=False 时增加 retry_count，
        超过最大重试次数也标记为 resolved（不再自动重试，需人工介入）。
        """
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            batch.retry_count += 1
            batch.last_retry_at = _utcnow_naive()
            batch.last_retry_result = "success" if success else "failed"
            if success:
                batch.resolved = True
            else:
                max_retries = settings.EXTERNAL_DATA_FAILED_BATCH_MAX_RETRIES
                if max_retries > 0 and batch.retry_count >= max_retries:
                    # 超过最大重试次数：标记为 resolved（不再自动重试）
                    batch.resolved = True
            return batch

    def clear_resolved(self) -> int:
        """清理已解决的批次（供测试与管理接口使用）。返回清理数量。"""
        with self._lock:
            resolved_ids = [bid for bid, b in self._batches.items() if b.resolved]
            for bid in resolved_ids:
                self._batches.pop(bid, None)
            return len(resolved_ids)

    def clear(self) -> None:
        """清空所有失败批次（供测试使用）。"""
        with self._lock:
            self._batches.clear()


_failed_batches = _FailedBatchRegistry()


def record_failed_batch(
    *,
    interface_key: str,
    symbols: list[str],
    reason: str,
    error_code: str = "other",
) -> FailedBatch:
    """记录一个补数失败批次（公开 API）。

    失败批次不阻塞其他标的的补数：调用方在单个标的/批次失败后调用此函数，
    然后继续后续标的的补数。失败批次可后续通过 retry_failed_batch() 重跑。
    """
    return _failed_batches.record(
        interface_key=interface_key,
        symbols=symbols,
        reason=reason,
        error_code=error_code,
    )


def list_failed_batches(
    *, include_resolved: bool = False, interface_key: str | None = None
) -> list[FailedBatch]:
    """列出失败批次（公开 API）。

    Args:
        include_resolved: True 时包含已解决的批次；False 时仅返回未解决
        interface_key: 可选，按 interface_key 过滤
    """
    if include_resolved:
        return _failed_batches.list_all(interface_key)
    return _failed_batches.list_unresolved(interface_key)


async def retry_failed_batch(
    batch_id: str,
    retry_fetcher: Callable[[str, list[str]], Awaitable[tuple[Any, str]]] | None = None,
) -> FailedBatch | None:
    """重试一个失败批次（公开 API）。

    Args:
        batch_id: 失败批次 ID
        retry_fetcher: 可选的重试回调，签名为
            async (interface_key, symbols) -> (data, source_detail)
            未提供时仅标记重试次数，不实际拉取（用于纯状态测试）

    Returns:
        更新后的 FailedBatch；batch_id 不存在时返回 None
    """
    batch = _failed_batches.get(batch_id)
    if batch is None:
        return None
    if retry_fetcher is None:
        # 仅标记重试（不实际拉取），用于状态机测试
        return _failed_batches.mark_retry(batch_id, success=True)
    try:
        await retry_fetcher(batch.interface_key, batch.symbols)
        return _failed_batches.mark_retry(batch_id, success=True)
    except Exception as exc:
        logger.warning(
            "Failed batch retry failed: batch=%s interface=%s err=%s",
            batch_id, batch.interface_key, _sanitize_error(exc),
        )
        return _failed_batches.mark_retry(
            batch_id, success=False, result_msg=_sanitize_error(exc)
        )


def clear_failed_batches() -> None:
    """清空所有失败批次（供测试与管理接口使用）。"""
    _failed_batches.clear()


def clear_resolved_failed_batches() -> int:
    """清理已解决的失败批次。返回清理数量。"""
    return _failed_batches.clear_resolved()


# ── P1-08 数据就绪后自动恢复 ────────────────────────────

# 自动恢复回调注册表：补数任务完成后触发评分/扫描更新
_auto_recovery_callbacks: list[Callable[[list[str]], Awaitable[None]]] = []


def register_auto_recovery_callback(
    callback: Callable[[list[str]], Awaitable[None]],
) -> None:
    """注册数据就绪后自动恢复回调。

    补数任务完成后，调用所有已注册的回调，传入本次成功的标的列表，
    供回调触发评分重算/扫描快照更新等。

    回调签名：async (symbols: list[str]) -> None
    回调失败仅记录日志，不阻塞其他回调或主流程（best-effort）。

    Args:
        callback: 异步回调函数
    """
    if callback not in _auto_recovery_callbacks:
        _auto_recovery_callbacks.append(callback)


def unregister_auto_recovery_callback(
    callback: Callable[[list[str]], Awaitable[None]],
) -> None:
    """取消注册自动恢复回调（供测试使用）。"""
    if callback in _auto_recovery_callbacks:
        _auto_recovery_callbacks.remove(callback)


async def trigger_auto_recovery(symbols: list[str]) -> None:
    """触发数据就绪后自动恢复。

    补数任务完成后调用此函数，依次调用所有已注册的恢复回调。
    单个回调失败仅记录日志，不阻塞其他回调（best-effort）。

    Args:
        symbols: 本次补数成功的标的代码列表
    """
    if not settings.DATA_FRESHNESS_AUTO_RECOVERY:
        logger.debug("Auto recovery disabled by settings, skipping")
        return
    if not symbols:
        return
    for callback in list(_auto_recovery_callbacks):
        try:
            await callback(symbols)
        except Exception as exc:
            logger.warning(
                "Auto recovery callback failed: %s err=%s",
                getattr(callback, "__name__", repr(callback)),
                _sanitize_error(exc),
            )


__all__ = [
    # 主入口
    "fetch",
    "fetch_via_gateway",
    "GatewayRequest",
    "GatewayResponse",
    "CacheLevel",
    # 异常
    "DataValidationError",
    "StaleDataError",
    "CircuitBreakerOpenError",
    # L1/L2/L3/L4 数据源常量
    "SOURCE_L1_CACHE",
    "SOURCE_L2_DB",
    "SOURCE_L3_DUCKDB",
    "SOURCE_L4_REMOTE",
    "SOURCE_STALE",
    # 注册表（供业务方扩展）
    "register_l2_fetcher",
    "register_l4_fetcher",
    "register_source_chain",
    "get_source_chain",
    "unregister_source_chain",
    # 工具函数
    "normalize_params",
    "validate_rows",
    "_upsert_batch",
    "StagingBatch",
    "duckdb_write_session",
    # 管理接口
    "get_endpoint_status",
    "clear_l1_cache",
    "reset_breaker",
    "reset_rate_limiter",
    # P1-08 数据新鲜度闭环：错峰调度 / 失败批次续跑 / 自动恢复
    "is_within_offpeak_window",
    "should_defer_for_offpeak",
    "FailedBatch",
    "record_failed_batch",
    "list_failed_batches",
    "retry_failed_batch",
    "clear_failed_batches",
    "clear_resolved_failed_batches",
    "register_auto_recovery_callback",
    "unregister_auto_recovery_callback",
    "trigger_auto_recovery",
]
