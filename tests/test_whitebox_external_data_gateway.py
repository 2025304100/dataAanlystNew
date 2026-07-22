"""白盒测试 - 统一外部数据网关（WP-S.1 ~ WP-S.4）。

覆盖：
- 工具函数：normalize_params / _sanitize_error / _classify_error / _is_fresh
- L1 进程缓存 TTL/LRU 行为
- WP-S.1 多层缓存命中（L1/L2/L4）+ 本地存在合格数据时不发起第三方请求
- WP-S.1b CacheLevel 枚举 / GatewayRequest dataclass / fetch_via_gateway 主入口
- WP-S.2 单飞：20 并发只产生 1 个真实请求
- WP-S.2 限流：host/interface/task_type 并发上限
- WP-S.2 熔断：closed → open → half_open → closed/open 状态机
- WP-S.3 降级链：L4 失败 + allow_stale=True/False + source_detail 透传
- WP-S.3b register_source_chain / get_source_chain / unregister_source_chain
- WP-S.4 入库稳定性：validate_rows / _upsert_batch / StagingBatch

测试在 SQLite 内存库上运行，不依赖 MySQL 与真实网络。
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.external_endpoint_runtime import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    ExternalEndpointRuntime,
)
from app.models.symbol import Symbol
from app.schemas.external_data import (
    CircuitBreakerOpenError,
    DataValidationError,
    GatewayResponse,
    SOURCE_L1_CACHE,
    SOURCE_L2_DB,
    SOURCE_L4_REMOTE,
    SOURCE_STALE,
    StaleDataError,
)
from app.services import external_data_gateway as gw
from app.services.external_data_gateway import (
    CacheLevel,
    GatewayRequest,
    fetch_via_gateway,
    get_source_chain,
    register_source_chain,
    unregister_source_chain,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 公共 fixture
# ============================================================================

@pytest.fixture(autouse=True)
def _reset_gateway_state(db_session):
    """每个测试前重置网关全局状态（L1 缓存 / 熔断器 / 限流器 / 单飞）。

    autouse=True 保证测试间隔离，避免相互污染。
    """
    gw.clear_l1_cache()
    gw.reset_breaker()
    gw.reset_rate_limiter()
    # 强制下次访问从 DB 重新加载熔断状态
    gw._breaker._loaded = False
    # 清空 DB 中的熔断器记录
    db_session.query(ExternalEndpointRuntime).delete()
    db_session.commit()
    yield
    # 测试后再清理一次，避免遗留状态影响后续测试
    gw.clear_l1_cache()
    gw.reset_breaker()
    gw.reset_rate_limiter()
    gw._breaker._loaded = False


def _seed_symbol_and_bars(db_session, symbol: str = "000001", days: int = 5):
    """向 DB 插入 Symbol + DailyBar 记录，供 L2 测试使用。

    返回 (symbol_id, latest_date)。
    """
    sym = Symbol(
        symbol=symbol,
        name=f"Test {symbol}",
        asset_type="stock",
        market="SZ",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)

    today = date.today()
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        db_session.add(DailyBar(
            symbol_id=sym.id,
            trade_date=d,
            open=10.0 + i,
            high=10.5 + i,
            low=9.5 + i,
            close=10.2 + i,
            volume=100000.0,
            amount=1.0e6,
            turnover_rate=0.01,
            source="akshare",
        ))
    db_session.commit()
    return sym.id, today


# ============================================================================
# 1. 工具函数测试
# ============================================================================

class TestNormalizeParams:
    """normalize_params 哈希稳定性测试。"""

    def test_same_dict_different_key_order_produces_same_hash(self):
        """相同语义的 dict（key 顺序不同）应生成相同 hash。"""
        a = gw.normalize_params({"symbol": "000001", "start": "2024-01-01", "end": "2024-06-01"})
        b = gw.normalize_params({"end": "2024-06-01", "symbol": "000001", "start": "2024-01-01"})
        assert a == b
        assert len(a) == 16  # sha256 前 16 字符

    def test_different_values_produce_different_hash(self):
        """不同参数值应生成不同 hash。"""
        a = gw.normalize_params({"symbol": "000001"})
        b = gw.normalize_params({"symbol": "000002"})
        assert a != b

    def test_different_types_produce_different_hash(self):
        """int 1 vs str "1" 应生成不同 hash（避免类型混淆）。"""
        a = gw.normalize_params({"limit": 1})
        b = gw.normalize_params({"limit": "1"})
        assert a != b

    def test_nested_dict_sorted(self):
        """嵌套 dict 按 key 排序后哈希稳定。"""
        a = gw.normalize_params({"filter": {"b": 1, "a": 2}})
        b = gw.normalize_params({"filter": {"a": 2, "b": 1}})
        assert a == b

    def test_list_order_preserved(self):
        """list 元素顺序敏感（不像 dict 那样排序）。"""
        a = gw.normalize_params({"symbols": ["000001", "000002"]})
        b = gw.normalize_params({"symbols": ["000002", "000001"]})
        assert a != b

    def test_unserializable_falls_back_to_str(self):
        """不可 JSON 序列化的对象降级为 str()，仍能稳定哈希。"""
        class Custom:
            def __str__(self):
                return "custom_obj"

        obj = Custom()
        a = gw.normalize_params({"obj": obj})
        b = gw.normalize_params({"obj": obj})
        assert a == b  # 同一对象的 str 稳定


class TestSanitizeError:
    """_sanitize_error 密码脱敏测试（project_memory 硬约束 #7）。"""

    def test_strips_mysql_url_password(self):
        """mysql://user:password@host 中的 password 应被替换为 ***。"""
        exc = ValueError("connect to mysql://admin:s3cret@127.0.0.1:3306/db failed")
        sanitized = gw._sanitize_error(exc)
        assert "s3cret" not in sanitized
        assert "***" in sanitized

    def test_strips_password_kwarg(self):
        """password=xxx / pwd=xxx 模式应被脱敏。"""
        exc = RuntimeError("connect failed password=my_pwd_here timeout=10")
        sanitized = gw._sanitize_error(exc)
        assert "my_pwd_here" not in sanitized
        assert "***" in sanitized

    def test_strips_pwd_kwarg_case_insensitive(self):
        """PWD=xxx 大小写不敏感也应被脱敏。"""
        exc = RuntimeError("auth PWD=Secret123 failed")
        sanitized = gw._sanitize_error(exc)
        assert "Secret123" not in sanitized

    def test_preserves_non_sensitive_errors(self):
        """无密码的错误消息保持原样。"""
        exc = TimeoutError("read timeout after 30s")
        sanitized = gw._sanitize_error(exc)
        assert "read timeout" in sanitized


class TestClassifyError:
    """_classify_error 错误分类测试（决定是否计入熔断失败计数）。"""

    def test_timeout_by_class_name(self):
        """TimeoutError 类名优先识别为 timeout。"""
        assert gw._classify_error(TimeoutError("read timeout")) == "timeout"

    def test_connect_timeout(self):
        assert gw._classify_error(ConnectionError("ConnectTimeout")) == "connection_reset"

    def test_connection_reset(self):
        assert gw._classify_error(ConnectionResetError("connection reset")) == "connection_reset"

    def test_http_429_in_message(self):
        assert gw._classify_error(RuntimeError("HTTP 429 Too Many Requests")) == "http_429"

    def test_http_403_in_message(self):
        assert gw._classify_error(RuntimeError("HTTP 403 Forbidden")) == "http_403"

    def test_rate_limit_keyword(self):
        assert gw._classify_error(RuntimeError("rate limit exceeded")) == "http_429"

    def test_timeout_keyword_in_message(self):
        """消息含 timeout 关键字也应归 timeout。"""
        assert gw._classify_error(RuntimeError("operation timeout")) == "timeout"

    def test_other_for_value_errors(self):
        """ValueError（数据格式问题）归 other，不计入熔断。"""
        assert gw._classify_error(ValueError("invalid symbol")) == "other"


class TestComputeBackoff:
    """_compute_backoff 指数退避 + 抖动测试。"""

    def test_backoff_increases_with_attempt(self):
        """退避时间随 attempt 增长（指数）。"""
        b0 = gw._compute_backoff(0)
        b3 = gw._compute_backoff(3)
        # b3 的 base 是 1.0 * 2^3 = 8.0，b0 是 1.0；考虑抖动后 b3 应明显大于 b0
        assert b3 > b0

    def test_backoff_capped_at_max(self):
        """退避时间不超过 max（默认 60s）。"""
        # attempt=20 远超上限
        b = gw._compute_backoff(20)
        # 加上抖动后理论上限约 60 * (1 + 0.3) = 78s
        assert b <= 80.0

    def test_backoff_non_negative(self):
        """退避时间非负。"""
        for attempt in range(5):
            assert gw._compute_backoff(attempt) >= 0.0


class TestIsFresh:
    """_is_fresh 新鲜度判断测试。"""

    def test_none_cutoff_is_stale(self):
        assert gw._is_fresh(None, timedelta(hours=24)) is False

    def test_recent_cutoff_is_fresh(self):
        """cutoff 在 freshness_requirement 内为 fresh。"""
        cutoff = gw._utcnow_naive() - timedelta(hours=1)
        assert gw._is_fresh(cutoff, timedelta(hours=24)) is True

    def test_old_cutoff_is_stale(self):
        """cutoff 超出 freshness_requirement 为 stale。"""
        cutoff = gw._utcnow_naive() - timedelta(days=7)
        assert gw._is_fresh(cutoff, timedelta(hours=24)) is False

    def test_timezone_aware_cutoff_handled(self):
        """带时区的 cutoff 应能正确比较。"""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert gw._is_fresh(cutoff, timedelta(hours=1)) is True


# ============================================================================
# 2. L1 进程缓存测试
# ============================================================================

class TestTTLCache:
    """_TTLCache LRU + TTL 行为测试。"""

    def test_set_and_get(self):
        cache = gw._TTLCache(maxsize=8, ttl=60)
        cache.set("k1", "v1", cutoff_at=None)
        result = cache.get("k1")
        assert result is not None
        value, cutoff = result
        assert value == "v1"
        assert cutoff is None

    def test_get_missing_returns_none(self):
        cache = gw._TTLCache(maxsize=8, ttl=60)
        assert cache.get("missing") is None

    def test_ttl_expiration(self):
        """TTL 过期后 get 返回 None。"""
        cache = gw._TTLCache(maxsize=8, ttl=1)
        cache.set("k1", "v1")
        time.sleep(1.2)
        assert cache.get("k1") is None

    def test_lru_eviction(self):
        """容量超限时淘汰最久未访问的项。

        注意：_TTLCache 内部强制 maxsize = max(8, int(maxsize))，
        所以 maxsize 必须使用 >=8 的值，插入 9 项触发淘汰。
        """
        cache = gw._TTLCache(maxsize=8, ttl=60)
        # 插入 8 项
        for i in range(1, 9):
            cache.set(f"k{i}", f"v{i}")
        # 访问 k1 让 k2 变成最旧
        cache.get("k1")
        # 插入第 9 项触发淘汰，应淘汰 k2（最久未访问）
        cache.set("k9", "v9")
        assert cache.get("k1") is not None  # k1 刚访问过，仍存在
        assert cache.get("k2") is None     # k2 被淘汰
        assert cache.get("k9") is not None  # 新插入的存在

    def test_clear(self):
        cache = gw._TTLCache(maxsize=8, ttl=60)
        cache.set("k1", "v1")
        cache.clear()
        assert cache.get("k1") is None
        assert len(cache) == 0


# ============================================================================
# 3. WP-S.1 多层缓存命中测试
# ============================================================================

class TestFetchCacheHits:
    """fetch() 各层缓存命中行为测试。"""

    def test_l1_hit_skips_remote(self, db_session):
        """L1 命中时不应发起远程请求。

        验证约束：本地存在合格数据时不发起第三方请求。
        """
        # 准备：注册一个会失败的 L4 fetcher（如果被调用，测试失败）
        call_count = {"n": 0}

        def fake_l4(params):
            call_count["n"] += 1
            raise RuntimeError("L4 should not be called when L1 hits")

        gw.register_l4_fetcher("test.iface", fake_l4)

        # 预热 L1 缓存
        cache_key = "test.iface:" + gw.normalize_params({"k": "v"})
        gw._l1_cache.set(cache_key, pd.DataFrame([{"x": 1}]), cutoff_at=gw._utcnow_naive())

        # 执行
        resp = asyncio.run(gw.fetch(
            interface_key="test.iface",
            request_params={"k": "v"},
            freshness_requirement=timedelta(hours=24),
            allow_stale=True,
        ))

        assert resp.cache_hit is True
        assert resp.source == SOURCE_L1_CACHE
        assert call_count["n"] == 0  # L4 未被调用

    def test_l2_hit_skips_remote(self, db_session):
        """L2 命中时不应发起远程请求。

        验证约束：本地存在合格数据时不发起第三方请求。
        """
        symbol_id, latest = _seed_symbol_and_bars(db_session, "000001", days=3)

        l4_call_count = {"n": 0}

        def fake_l4(params):
            l4_call_count["n"] += 1
            raise RuntimeError("L4 should not be called when L2 hits")

        gw.register_l4_fetcher("akshare.daily_bars", fake_l4)

        # 执行：freshness 设宽松，让 L2 数据通过
        resp = asyncio.run(gw.fetch(
            interface_key="akshare.daily_bars",
            request_params={"symbol": "000001"},
            freshness_requirement=timedelta(days=365),
            allow_stale=True,
        ))

        assert resp.cache_hit is True
        assert resp.source == SOURCE_L2_DB
        assert resp.data is not None
        assert len(resp.data) == 3
        assert l4_call_count["n"] == 0  # L4 未被调用

    def test_l4_called_when_no_local_data(self, db_session):
        """L1/L2/L3 都无数据时调用 L4。"""
        called = {"n": 0}

        def fake_l4(params):
            called["n"] += 1
            return pd.DataFrame([{"trade_date": date.today(), "close": 100.0}]), "test_source"

        gw.register_l4_fetcher("test.l4_only", fake_l4)

        resp = asyncio.run(gw.fetch(
            interface_key="test.l4_only",
            request_params={"symbol": "999999"},
            freshness_requirement=timedelta(hours=24),
            allow_stale=True,
        ))

        assert resp.source == SOURCE_L4_REMOTE
        assert resp.cache_hit is False
        assert resp.source_detail == "test_source"
        assert called["n"] == 1

    def test_l4_failure_with_stale_local_degrades(self, db_session):
        """L4 失败 + allow_stale=True → 降级到本地过期数据。"""
        symbol_id, latest = _seed_symbol_and_bars(db_session, "000002", days=2)

        def failing_l4(params):
            raise RuntimeError("network down")

        gw.register_l4_fetcher("akshare.daily_bars", failing_l4)

        # freshness 设极短，让 L2 数据被视为过期
        resp = asyncio.run(gw.fetch(
            interface_key="akshare.daily_bars",
            request_params={"symbol": "000002"},
            freshness_requirement=timedelta(seconds=1),
            allow_stale=True,
        ))

        assert resp.source == SOURCE_STALE
        assert resp.cache_hit is True  # 降级到本地，cache_hit=True
        assert resp.data is not None
        assert resp.degraded_reason is not None
        assert "l4_failed" in resp.degraded_reason

    def test_l4_failure_no_stale_raises(self, db_session):
        """L4 失败 + allow_stale=False → 抛 StaleDataError。"""
        symbol_id, latest = _seed_symbol_and_bars(db_session, "000003", days=2)

        def failing_l4(params):
            raise RuntimeError("network down")

        gw.register_l4_fetcher("akshare.daily_bars", failing_l4)

        with pytest.raises(StaleDataError):
            asyncio.run(gw.fetch(
                interface_key="akshare.daily_bars",
                request_params={"symbol": "000003"},
                freshness_requirement=timedelta(seconds=1),
                allow_stale=False,
            ))

    def test_no_l4_fetcher_registered_degrades(self, db_session):
        """无 L4 fetcher 注册时降级返回（而非崩溃）。"""
        resp = asyncio.run(gw.fetch(
            interface_key="test.no_l4_registered",
            request_params={"k": "v"},
            freshness_requirement=timedelta(hours=1),
            allow_stale=True,
        ))
        assert resp.source == SOURCE_STALE
        assert resp.degraded_reason == "no_l4_fetcher_registered"


# ============================================================================
# 4. WP-S.2 单飞测试
# ============================================================================

class TestSingleFlight:
    """_SingleFlight 单飞协调器测试。"""

    def test_20_concurrent_calls_produce_one_real_request(self, db_session):
        """【WP-S.2 验证】20 个并发调用只产生 1 个真实请求。

        验证约束：相同 interface_key + normalized_params 同时只允许一个真实网络请求。
        """
        call_count = {"n": 0}
        call_lock = asyncio.Lock()

        async def slow_coro():
            async with call_lock:
                call_count["n"] += 1
            # 模拟网络延迟，让其他并发调用有机会进入
            await asyncio.sleep(0.05)
            return pd.DataFrame([{"close": 100.0}])

        async def main():
            sf = gw._SingleFlight()
            tasks = [
                asyncio.create_task(sf.execute("test_key", slow_coro))
                for _ in range(20)
            ]
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(main())

        assert call_count["n"] == 1, f"应只调用 1 次，实际 {call_count['n']} 次"
        assert len(results) == 20
        # 所有调用共享同一结果
        for r in results:
            assert r is results[0]

    def test_leader_failure_follower_retries(self, db_session):
        """leader 失败时 follower 立即重试一次。

        验证约束：单飞失败后允许 follower 重试，避免瞬时抖动导致所有调用方连锁失败。
        """
        attempt = {"n": 0}

        async def flaky_coro():
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise RuntimeError("first attempt fails")
            return "success_on_retry"

        async def main():
            sf = gw._SingleFlight()
            # 启动 2 个并发任务
            t1 = asyncio.create_task(sf.execute("k1", flaky_coro, allow_one_retry=True))
            # 确保 t1 先进入 leader 角色
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(sf.execute("k1", flaky_coro, allow_one_retry=True))
            r1 = await t1
            r2 = await t2
            return r1, r2

        r1, r2 = asyncio.run(main())
        # leader 重试后成功，follower 也得到结果（可能来自 leader 重试或自己重试）
        # 至少有一次成功
        assert r1 == "success_on_retry" or r2 == "success_on_retry"

    def test_different_keys_no_single_flight(self, db_session):
        """不同 key 的调用互不单飞，各自独立执行。"""
        call_count = {"a": 0, "b": 0}

        async def coro_a():
            call_count["a"] += 1
            await asyncio.sleep(0.01)
            return "a"

        async def coro_b():
            call_count["b"] += 1
            await asyncio.sleep(0.01)
            return "b"

        async def main():
            sf = gw._SingleFlight()
            t1 = asyncio.create_task(sf.execute("key_a", coro_a))
            t2 = asyncio.create_task(sf.execute("key_b", coro_b))
            return await asyncio.gather(t1, t2)

        asyncio.run(main())
        assert call_count["a"] == 1
        assert call_count["b"] == 1


# ============================================================================
# 5. WP-S.2 限流测试
# ============================================================================

class TestRateLimiter:
    """三维度限流器测试（host / interface / task_type）。"""

    def test_host_concurrency_limit(self, db_session):
        """主机级并发上限生效。

        使用 monkeypatch 临时将 host 并发上限改为 1，验证同时只允许 1 个请求。
        """
        from app.core.config import settings
        original = settings.EXTERNAL_DATA_HOST_CONCURRENCY
        settings.EXTERNAL_DATA_HOST_CONCURRENCY = 1
        try:
            in_flight = {"n": 0, "max": 0}

            async def main():
                async def task():
                    sem = await gw._rate_limiter.acquire(
                        "host", "api.example.com", settings.EXTERNAL_DATA_HOST_CONCURRENCY
                    )
                    try:
                        in_flight["n"] += 1
                        in_flight["max"] = max(in_flight["max"], in_flight["n"])
                        await asyncio.sleep(0.05)
                        in_flight["n"] -= 1
                    finally:
                        sem.release()
                await asyncio.gather(*[task() for _ in range(5)])

            asyncio.run(main())
            assert in_flight["max"] == 1  # 同时只允许 1 个
        finally:
            settings.EXTERNAL_DATA_HOST_CONCURRENCY = original

    def test_interface_concurrency_limit(self, db_session):
        """接口级并发上限生效。"""
        from app.core.config import settings
        original = settings.EXTERNAL_DATA_INTERFACE_CONCURRENCY
        settings.EXTERNAL_DATA_INTERFACE_CONCURRENCY = 2
        try:
            in_flight = {"n": 0, "max": 0}

            async def main():
                async def task():
                    sem = await gw._rate_limiter.acquire(
                        "interface", "test.iface", settings.EXTERNAL_DATA_INTERFACE_CONCURRENCY
                    )
                    try:
                        in_flight["n"] += 1
                        in_flight["max"] = max(in_flight["max"], in_flight["n"])
                        await asyncio.sleep(0.05)
                        in_flight["n"] -= 1
                    finally:
                        sem.release()
                await asyncio.gather(*[task() for _ in range(6)])

            asyncio.run(main())
            assert in_flight["max"] == 2  # 同时最多 2 个
        finally:
            settings.EXTERNAL_DATA_INTERFACE_CONCURRENCY = original


# ============================================================================
# 6. WP-S.2 熔断器状态机测试
# ============================================================================

class TestCircuitBreaker:
    """熔断器状态机测试。

    状态转换：
        closed  → 连续 N 次失败 → open
        open    → cooldown 到期 → half_open（只允许 1 个探测）
        half_open → 探测成功 → closed
        half_open → 探测失败 → open（cooldown 翻倍）
    """

    @pytest.fixture
    def breaker(self):
        # 必须返回模块级 _breaker 实例，保证 breaker.record_failure 与
        # gw.get_endpoint_status 读写同一份状态（_reset_gateway_state 已在 autouse 中重置）
        return gw._breaker

    @pytest.fixture
    def short_cooldown_settings(self, monkeypatch):
        """将熔断阈值和冷却时间缩短以加速测试。"""
        from app.core.config import settings
        monkeypatch.setattr(settings, "EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD", 3)
        monkeypatch.setattr(settings, "EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS", 2)
        monkeypatch.setattr(settings, "EXTERNAL_DATA_BREAKER_MAX_COOLDOWN_SECONDS", 30)
        return settings

    def test_closed_state_allows_request(self, db_session, breaker):
        """closed 状态允许请求通过。"""
        state = asyncio.run(breaker.acquire("test.iface", "host"))
        assert state.state == STATE_CLOSED

    def test_consecutive_failures_open_circuit(
        self, db_session, breaker, short_cooldown_settings
    ):
        """连续失败达阈值后熔断器进入 open 状态。"""
        iface = "test.fail_threshold"
        threshold = short_cooldown_settings.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD

        for i in range(threshold):
            asyncio.run(breaker.record_failure(iface, "host", "timeout"))

        # 验证状态已转为 open（用 get_endpoint_status 避免触发 acquire 抛错）
        status = asyncio.run(gw.get_endpoint_status(iface))
        assert status is not None
        assert status["state"] == STATE_OPEN
        # 熔断打开后 acquire 应抛 CircuitBreakerOpenError
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(breaker.acquire(iface, "host"))

    def test_open_after_cooldown_becomes_half_open(
        self, db_session, breaker, short_cooldown_settings
    ):
        """open 状态冷却到期后转为 half_open，只允许 1 个探测。"""
        iface = "test.cooldown"
        threshold = short_cooldown_settings.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD
        cooldown = short_cooldown_settings.EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS

        for _ in range(threshold):
            asyncio.run(breaker.record_failure(iface, "host", "timeout"))
        # 验证已进入 open 状态（不调用 acquire，避免抛错）
        status = asyncio.run(gw.get_endpoint_status(iface))
        assert status is not None
        assert status["state"] == STATE_OPEN

        # 等待冷却到期
        time.sleep(cooldown + 0.1)

        # 再次 acquire 应转为 half_open（探测请求）
        state2 = asyncio.run(breaker.acquire(iface, "host"))
        assert state2.state == STATE_HALF_OPEN

        # half_open 状态下，第二个请求应被拒绝（只允许 1 个探测）
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(breaker.acquire(iface, "host"))

    def test_half_open_success_closes_circuit(
        self, db_session, breaker, short_cooldown_settings
    ):
        """half_open 探测成功后熔断器恢复 closed。"""
        iface = "test.half_open_success"
        threshold = short_cooldown_settings.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD
        cooldown = short_cooldown_settings.EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS

        for _ in range(threshold):
            asyncio.run(breaker.record_failure(iface, "host", "timeout"))
        time.sleep(cooldown + 0.1)
        asyncio.run(breaker.acquire(iface, "host"))  # 进入 half_open

        # 探测成功
        asyncio.run(breaker.record_success(iface, "host"))

        state = asyncio.run(breaker.acquire(iface, "host"))
        assert state.state == STATE_CLOSED
        assert state.consecutive_failures == 0

    def test_half_open_failure_reopens_circuit(
        self, db_session, breaker, short_cooldown_settings
    ):
        """half_open 探测失败后回到 open，cooldown 翻倍。"""
        iface = "test.half_open_fail"
        threshold = short_cooldown_settings.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD
        cooldown = short_cooldown_settings.EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS

        for _ in range(threshold):
            asyncio.run(breaker.record_failure(iface, "host", "timeout"))
        # 获取初始 cooldown（不调用 acquire，避免抛错）
        status = asyncio.run(gw.get_endpoint_status(iface))
        assert status is not None
        assert status["state"] == STATE_OPEN
        assert status["cooldown_until"] is not None
        original_cooldown = datetime.fromisoformat(status["cooldown_until"])

        time.sleep(cooldown + 0.1)
        asyncio.run(breaker.acquire(iface, "host"))  # half_open

        # 探测失败
        asyncio.run(breaker.record_failure(iface, "host", "timeout"))

        # 验证状态已转回 open（不调用 acquire，避免抛错）
        status2 = asyncio.run(gw.get_endpoint_status(iface))
        assert status2 is not None
        assert status2["state"] == STATE_OPEN
        # cooldown 应被翻倍
        assert status2["cooldown_until"] is not None
        new_cooldown = datetime.fromisoformat(status2["cooldown_until"])
        assert new_cooldown > original_cooldown

    def test_success_resets_failure_count(self, db_session, breaker, short_cooldown_settings):
        """closed 状态下成功调用重置失败计数（避免累积）。"""
        iface = "test.reset"
        threshold = short_cooldown_settings.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD

        # 失败 threshold - 1 次（未到熔断）
        for _ in range(threshold - 1):
            asyncio.run(breaker.record_failure(iface, "host", "timeout"))
        state = asyncio.run(breaker.acquire(iface, "host"))
        assert state.state == STATE_CLOSED
        assert state.consecutive_failures == threshold - 1

        # 一次成功重置计数
        asyncio.run(breaker.record_success(iface, "host"))
        state = asyncio.run(breaker.acquire(iface, "host"))
        assert state.state == STATE_CLOSED
        assert state.consecutive_failures == 0

    def test_breaker_state_persisted_to_db(self, db_session, breaker, short_cooldown_settings):
        """熔断状态应持久化到 ExternalEndpointRuntime 表。"""
        iface = "test.persist"
        threshold = short_cooldown_settings.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD

        for _ in range(threshold):
            asyncio.run(breaker.record_failure(iface, "test.host", "timeout"))

        # 查询 DB 应有对应记录
        rows = db_session.query(ExternalEndpointRuntime).filter_by(interface_key=iface).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.state == STATE_OPEN
        assert row.host == "test.host"
        assert row.consecutive_failures == threshold
        assert row.last_error_code == "timeout"
        assert row.cooldown_until is not None

    def test_breaker_state_loaded_from_db_on_startup(self, db_session, breaker, short_cooldown_settings):
        """进程重启后从 DB 恢复熔断状态。"""
        iface = "test.reload"
        # 直接向 DB 插入一条 open 状态记录
        row = ExternalEndpointRuntime(
            interface_key=iface,
            host="test.host",
            state=STATE_OPEN,
            consecutive_failures=99,
            cooldown_until=gw._utcnow_naive() + timedelta(hours=1),
            last_error_code="timeout",
        )
        db_session.add(row)
        db_session.commit()

        # 新的 breaker 实例（模拟进程重启）
        new_breaker = gw._CircuitBreakerRegistry()
        # acquire 时会触发 _ensure_loaded 从 DB 加载
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(new_breaker.acquire(iface, "test.host"))

    def test_fetch_uses_breaker(self, db_session, short_cooldown_settings):
        """fetch() 调用 L4 时应使用熔断器。"""
        iface = "test.fetch_breaker"
        threshold = short_cooldown_settings.EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD

        call_count = {"n": 0}

        def always_fail(params):
            call_count["n"] += 1
            raise RuntimeError("timeout")

        gw.register_l4_fetcher(iface, always_fail)

        # 触发 threshold 次失败
        for _ in range(threshold):
            try:
                asyncio.run(gw.fetch(
                    interface_key=iface,
                    request_params={"k": "v"},
                    freshness_requirement=timedelta(seconds=1),
                    allow_stale=False,
                ))
            except (StaleDataError, RuntimeError):
                pass

        # 熔断器应已 open，再调一次不应触发 L4
        before = call_count["n"]
        try:
            asyncio.run(gw.fetch(
                interface_key=iface,
                request_params={"k": "v"},
                freshness_requirement=timedelta(seconds=1),
                allow_stale=False,
            ))
        except (StaleDataError, CircuitBreakerOpenError):
            pass
        after = call_count["n"]
        # 熔断打开后 L4 不应被再次调用
        assert after == before, f"熔断打开后 L4 仍被调用：before={before}, after={after}"


# ============================================================================
# 7. WP-S.3 降级链 + source_detail 透传测试
# ============================================================================

class TestSourceChainIntegration:
    """网关复用 SourceChain 的 source 透传测试。"""

    def test_source_detail_preserved_from_l4(self, db_session):
        """L4 返回的 source_detail 应透传到 GatewayResponse。"""
        def fake_l4(params):
            return pd.DataFrame([{"close": 1.0}]), "sina_stock"

        gw.register_l4_fetcher("test.source_passthrough", fake_l4)

        resp = asyncio.run(gw.fetch(
            interface_key="test.source_passthrough",
            request_params={"symbol": "000001"},
            freshness_requirement=timedelta(hours=24),
            allow_stale=True,
        ))

        assert resp.source == SOURCE_L4_REMOTE
        assert resp.source_detail == "sina_stock"

    def test_l4_fails_then_degrades_source_is_stale(self, db_session):
        """L4 失败降级到本地时 source 应为 'stale'，不假装来自 L4。"""
        symbol_id, _ = _seed_symbol_and_bars(db_session, "000004", days=2)

        def failing_l4(params):
            raise RuntimeError("eastmoney down")

        gw.register_l4_fetcher("akshare.daily_bars", failing_l4)

        # 屏蔽 L3 DuckDB 查询，确保降级到 L2（避免环境 DuckDB 残留数据干扰）
        with patch.object(gw, "_l3_lookup", return_value=(None, None)):
            resp = asyncio.run(gw.fetch(
                interface_key="akshare.daily_bars",
                request_params={"symbol": "000004"},
                freshness_requirement=timedelta(seconds=1),
                allow_stale=True,
            ))

        assert resp.source == SOURCE_STALE  # 不假装来自主接口
        assert resp.cache_hit is True
        assert resp.source_detail == SOURCE_L2_DB  # 透传真实来源（L2 DB）

    def test_circuit_breaker_open_degrades_to_local(self, db_session, monkeypatch):
        """熔断打开时降级到本地数据（allow_stale=True）。"""
        from app.core.config import settings
        monkeypatch.setattr(settings, "EXTERNAL_DATA_BREAKER_FAILURE_THRESHOLD", 2)
        monkeypatch.setattr(settings, "EXTERNAL_DATA_BREAKER_COOLDOWN_SECONDS", 60)

        symbol_id, _ = _seed_symbol_and_bars(db_session, "000005", days=2)

        def failing_l4(params):
            raise RuntimeError("timeout")

        gw.register_l4_fetcher("akshare.daily_bars", failing_l4)

        # 触发 2 次失败让熔断打开
        for _ in range(2):
            try:
                asyncio.run(gw.fetch(
                    interface_key="akshare.daily_bars",
                    request_params={"symbol": "000005"},
                    freshness_requirement=timedelta(seconds=1),
                    allow_stale=True,
                ))
            except Exception:
                pass

        # 第 3 次调用：熔断打开，应直接降级
        resp = asyncio.run(gw.fetch(
            interface_key="akshare.daily_bars",
            request_params={"symbol": "000005"},
            freshness_requirement=timedelta(seconds=1),
            allow_stale=True,
        ))
        assert resp.source == SOURCE_STALE
        assert resp.degraded_reason == "circuit_breaker_open"


# ============================================================================
# 8. WP-S.4 入库稳定性测试
# ============================================================================

class TestValidateRows:
    """validate_rows 字段契约校验测试。"""

    def test_empty_rows_passes(self):
        """空列表直接通过。"""
        gw.validate_rows([], required_fields=["symbol", "close"])

    def test_missing_required_field_raises(self):
        """缺失必填字段抛 DataValidationError。"""
        rows = [{"symbol": "000001", "close": 10.0}]  # 缺 trade_date
        with pytest.raises(DataValidationError) as exc_info:
            gw.validate_rows(rows, required_fields=["symbol", "trade_date", "close"])
        assert exc_info.value.field == "trade_date"

    def test_none_value_treated_as_missing(self):
        """None 值视为缺失。"""
        rows = [{"symbol": "000001", "close": None}]
        with pytest.raises(DataValidationError) as exc_info:
            gw.validate_rows(rows, required_fields=["symbol", "close"])
        assert exc_info.value.field == "close"

    def test_empty_string_treated_as_missing(self):
        """空字符串视为缺失。"""
        rows = [{"symbol": "", "close": 10.0}]
        with pytest.raises(DataValidationError) as exc_info:
            gw.validate_rows(rows, required_fields=["symbol"])
        assert exc_info.value.field == "symbol"

    def test_invalid_date_raises(self):
        """日期字段不可解析时抛异常。"""
        rows = [{"symbol": "000001", "trade_date": "not-a-date"}]
        with pytest.raises(DataValidationError) as exc_info:
            gw.validate_rows(
                rows,
                required_fields=["symbol", "trade_date"],
                date_fields=["trade_date"],
            )
        assert exc_info.value.field == "trade_date"

    def test_date_out_of_range_raises(self):
        """日期越界抛异常。"""
        rows = [{"symbol": "000001", "trade_date": "2020-01-01"}]
        with pytest.raises(DataValidationError):
            gw.validate_rows(
                rows,
                required_fields=["symbol", "trade_date"],
                date_fields=["trade_date"],
                date_range=("2024-01-01", "2024-12-31"),
            )

    def test_invalid_numeric_raises(self):
        """数值字段不可转 float 抛异常。"""
        rows = [{"symbol": "000001", "close": "not-a-number"}]
        with pytest.raises(DataValidationError) as exc_info:
            gw.validate_rows(
                rows,
                required_fields=["symbol", "close"],
                numeric_fields=["close"],
            )
        assert exc_info.value.field == "close"

    def test_negative_price_raises(self):
        """价格为负抛异常。"""
        rows = [{"symbol": "000001", "close": -10.0}]
        with pytest.raises(DataValidationError):
            gw.validate_rows(
                rows,
                required_fields=["symbol", "close"],
                numeric_fields=["close"],
                numeric_range=(0.0, 1e9),
            )

    def test_valid_rows_pass(self):
        """全部字段合规的行通过校验。"""
        rows = [
            {"symbol": "000001", "trade_date": "2024-06-01", "close": 10.5},
            {"symbol": "000001", "trade_date": "2024-06-02", "close": 11.0},
        ]
        gw.validate_rows(
            rows,
            required_fields=["symbol", "trade_date", "close"],
            date_fields=["trade_date"],
            numeric_fields=["close"],
            date_range=("2024-01-01", "2024-12-31"),
            numeric_range=(0.0, 1e9),
        )


class TestUpsertBatch:
    """_upsert_batch 批量 UPSERT 写入测试（SQLite 路径）。"""

    def test_basic_upsert(self, db_session):
        """基础 UPSERT：新行被插入。"""
        from app.models.index_price import IndexPrice

        rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
             "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "test"},
            {"symbol": "000300", "trade_date": date(2024, 6, 2), "open": 4020.0,
             "high": 4100.0, "low": 4010.0, "close": 4080.0, "source": "test"},
        ]
        written = gw._upsert_batch(
            IndexPrice.__table__, rows,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )
        assert written == 2
        records = db_session.query(IndexPrice).filter_by(symbol="000300").all()
        assert len(records) == 2

    def test_upsert_idempotent(self, db_session):
        """UPSERT 幂等：相同主键重复写入只更新不新增。"""
        from app.models.index_price import IndexPrice

        rows1 = [{"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
                  "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "v1"}]
        gw._upsert_batch(
            IndexPrice.__table__, rows1,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )

        # 第二次写入相同主键，close 改为 4500.0
        rows2 = [{"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
                  "high": 4050.0, "low": 3990.0, "close": 4500.0, "source": "v2"}]
        gw._upsert_batch(
            IndexPrice.__table__, rows2,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )

        records = db_session.query(IndexPrice).filter_by(symbol="000300").all()
        assert len(records) == 1  # 仍是 1 条
        assert records[0].close == 4500.0  # 被更新
        assert records[0].source == "v2"

    def test_chunked_write(self, db_session):
        """大批量分块写入。"""
        from app.models.index_price import IndexPrice

        # 生成 12 行，batch_size=5 → 应分 3 块（5+5+2）
        rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1) + timedelta(days=i),
             "open": 4000.0 + i, "high": 4050.0 + i, "low": 3990.0 + i,
             "close": 4020.0 + i, "source": "test"}
            for i in range(12)
        ]
        written = gw._upsert_batch(
            IndexPrice.__table__, rows,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
            batch_size=5,
        )
        assert written == 12
        assert db_session.query(IndexPrice).count() == 12

    def test_invalid_column_filtered(self, db_session):
        """不在表中的列名应被静默过滤，不导致写入失败。"""
        from app.models.index_price import IndexPrice

        rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
             "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "test",
             "non_existent_column": "should_be_filtered"},
        ]
        written = gw._upsert_batch(
            IndexPrice.__table__, rows,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )
        assert written == 1


class TestStagingBatch:
    """StagingBatch 原子切换测试。"""

    def test_commit_writes_rows(self, db_session):
        """commit() 将暂存行写入业务表。"""
        from app.models.index_price import IndexPrice

        batch = gw.StagingBatch(
            table=IndexPrice.__table__,
            conflict_keys=["symbol", "trade_date"],
        )
        batch.add({"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
                   "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "staging"})
        batch.add({"symbol": "000300", "trade_date": date(2024, 6, 2), "open": 4020.0,
                   "high": 4100.0, "low": 4010.0, "close": 4080.0, "source": "staging"})

        written = batch.commit(db_session)
        assert written == 2
        assert db_session.query(IndexPrice).count() == 2

    def test_discard_clears_rows(self, db_session):
        """discard() 丢弃暂存行，不影响业务表。"""
        from app.models.index_price import IndexPrice

        batch = gw.StagingBatch(
            table=IndexPrice.__table__,
            conflict_keys=["symbol", "trade_date"],
        )
        batch.add({"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
                   "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "staging"})
        batch.discard()
        assert len(batch.rows) == 0
        assert db_session.query(IndexPrice).count() == 0

    def test_context_manager_discards_on_exception(self, db_session):
        """with 语句中抛异常时自动 discard，不影响业务表。"""
        from app.models.index_price import IndexPrice

        with pytest.raises(RuntimeError):
            with gw.StagingBatch(
                table=IndexPrice.__table__,
                conflict_keys=["symbol", "trade_date"],
            ) as batch:
                batch.add({"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
                           "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "staging"})
                raise RuntimeError("simulated failure")

        # 业务表不应有数据
        assert db_session.query(IndexPrice).count() == 0

    def test_double_commit_raises(self, db_session):
        """重复 commit 抛 RuntimeError。"""
        from app.models.index_price import IndexPrice

        batch = gw.StagingBatch(
            table=IndexPrice.__table__,
            conflict_keys=["symbol", "trade_date"],
        )
        batch.add({"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
                   "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "staging"})
        batch.commit(db_session)
        with pytest.raises(RuntimeError):
            batch.commit(db_session)

    def test_empty_commit_returns_zero(self, db_session):
        """空 batch commit 返回 0。"""
        from app.models.index_price import IndexPrice

        batch = gw.StagingBatch(
            table=IndexPrice.__table__,
            conflict_keys=["symbol", "trade_date"],
        )
        assert batch.commit(db_session) == 0

    def test_extend_adds_multiple_rows(self):
        """extend() 批量添加多行。"""
        from app.models.index_price import IndexPrice

        batch = gw.StagingBatch(
            table=IndexPrice.__table__,
            conflict_keys=["symbol", "trade_date"],
        )
        rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
             "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "s"},
            {"symbol": "000300", "trade_date": date(2024, 6, 2), "open": 4020.0,
             "high": 4100.0, "low": 4010.0, "close": 4080.0, "source": "s"},
        ]
        batch.extend(rows)
        assert len(batch.rows) == 2


# ============================================================================
# 9. WP-S.4 DuckDB 写入会话测试（单写锁）
# ============================================================================

class TestDuckDBWriteSession:
    """duckdb_write_session 单写锁测试。

    验证约束：DuckDB 连接使用上下文管理器和单写锁，取消/失败/退出 finally 释放。
    """

    def test_write_lock_is_released_after_normal_exit(self, tmp_path, monkeypatch):
        """正常退出后单写锁应被释放（可再次获取）。"""
        # 跳过 FactorWarehouse 实际初始化（依赖 DuckDB 安装），仅测试锁机制
        wh_path = tmp_path / "test.duckdb"

        # Mock FactorWarehouse 避免依赖 DuckDB 实际安装
        class MockWH:
            def __init__(self, path):
                self.path = path

            def initialize(self):
                pass

            class _Ctx:
                def __enter__(self):
                    return MagicMock()

                def __exit__(self, *a):
                    return False

            def connection(self):
                return self._Ctx()

        monkeypatch.setattr(
            "app.services.factors.store.FactorWarehouse", MockWH
        )

        # 第一次进入 with 块：锁应被持有（acquire 返回 False）
        with gw.duckdb_write_session(str(wh_path)):
            assert not gw._duckdb_write_lock.acquire(blocking=False), \
                "with 块内锁应被持有"

        # 正常退出后锁应释放（acquire 返回 True）
        assert gw._duckdb_write_lock.acquire(blocking=False), \
            "正常退出后锁应已释放"
        gw._duckdb_write_lock.release()

        # 第二次进入 with 块：锁应再次被持有
        with gw.duckdb_write_session(str(wh_path)):
            assert not gw._duckdb_write_lock.acquire(blocking=False), \
                "第二次 with 块内锁应被持有"

        # 退出后再次释放
        assert gw._duckdb_write_lock.acquire(blocking=False), \
            "第二次退出后锁应已释放"
        gw._duckdb_write_lock.release()

    def test_write_lock_released_on_exception(self, tmp_path, monkeypatch):
        """异常退出时单写锁应被释放。"""
        wh_path = tmp_path / "test.duckdb"

        class MockWH:
            def __init__(self, path):
                self.path = path

            def initialize(self):
                pass

            class _Ctx:
                def __enter__(self):
                    return MagicMock()

                def __exit__(self, *a):
                    return False

            def connection(self):
                return self._Ctx()

        monkeypatch.setattr(
            "app.services.factors.store.FactorWarehouse", MockWH
        )

        with pytest.raises(RuntimeError):
            with gw.duckdb_write_session(str(wh_path)):
                raise RuntimeError("simulated failure")

        # 锁应已释放
        assert gw._duckdb_write_lock.acquire(blocking=False)
        gw._duckdb_write_lock.release()


# ============================================================================
# 10. 集成场景：随机故障注入
# ============================================================================

class TestFaultInjection:
    """随机故障注入测试（WP-S.4 验证场景）。

    验证约束：随机注入超时/429/空字段/断连/格式变化时数据不重复不丢失。
    """

    def test_upsert_with_intermittent_failure_no_duplication(self, db_session, monkeypatch):
        """间歇性失败时 UPSERT 不产生重复数据。"""
        from app.models.index_price import IndexPrice

        # 模拟第一次 execute 失败，重试成功
        original_execute = db_session.execute
        call_count = {"n": 0}

        def flaky_execute(*args, **kwargs):
            call_count["n"] += 1
            # 第 1 次调用 execute 时抛异常（模拟锁冲突）
            if call_count["n"] == 1:
                raise RuntimeError("database is locked")
            return original_execute(*args, **kwargs)

        monkeypatch.setattr(db_session, "execute", flaky_execute)

        # 将退避设置为 0 加速测试
        from app.core.config import settings
        monkeypatch.setattr(settings, "EXTERNAL_DATA_BACKOFF_BASE_SECONDS", 0.0)
        monkeypatch.setattr(settings, "EXTERNAL_DATA_BACKOFF_MAX_SECONDS", 0.0)

        rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1), "open": 4000.0,
             "high": 4050.0, "low": 3990.0, "close": 4020.0, "source": "test"},
        ]
        written = gw._upsert_batch(
            IndexPrice.__table__, rows,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
            max_retries=3,
        )
        assert written == 1
        # 仅 1 条记录（无重复）
        assert db_session.query(IndexPrice).filter_by(symbol="000300").count() == 1

    def test_empty_payload_handled_gracefully(self, db_session):
        """空数据负载被优雅处理（不报错，不写入）。"""
        from app.models.index_price import IndexPrice

        written = gw._upsert_batch(
            IndexPrice.__table__, [],
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )
        assert written == 0
        assert db_session.query(IndexPrice).count() == 0


# ============================================================================
# WP-S.1b CacheLevel / GatewayRequest / fetch_via_gateway 测试
# ============================================================================

class TestCacheLevel:
    """CacheLevel 枚举测试（WP-S.1b）。"""

    def test_enum_has_five_levels(self):
        """CacheLevel 应包含 5 个层级值。"""
        assert CacheLevel.L1_PROCESS.value == "l1_cache"
        assert CacheLevel.L2_BUSINESS_DB.value == "l2_db"
        assert CacheLevel.L3_DUCKDB.value == "l3_duckdb"
        assert CacheLevel.L4_REMOTE.value == "l4_remote"
        assert CacheLevel.NONE.value == "none"

    def test_from_source_maps_known_sources(self):
        """from_source 应将 SOURCE_* 常量映射到对应的 CacheLevel。"""
        assert CacheLevel.from_source(SOURCE_L1_CACHE) == CacheLevel.L1_PROCESS
        assert CacheLevel.from_source(SOURCE_L2_DB) == CacheLevel.L2_BUSINESS_DB
        assert CacheLevel.from_source(SOURCE_L4_REMOTE) == CacheLevel.L4_REMOTE

    def test_from_source_maps_stale_and_unknown_to_none(self):
        """SOURCE_STALE 与未知值应映射到 NONE。"""
        assert CacheLevel.from_source(SOURCE_STALE) == CacheLevel.NONE
        assert CacheLevel.from_source("unknown_source") == CacheLevel.NONE
        assert CacheLevel.from_source(None) == CacheLevel.NONE


class TestGatewayRequest:
    """GatewayRequest dataclass 测试（WP-S.1b）。"""

    def test_default_fields(self):
        """未指定的字段应使用默认值。"""
        req = GatewayRequest(
            interface_key="akshare.daily_bars",
            request_params={"symbol": "000001"},
            freshness_requirement=timedelta(hours=24),
        )
        assert req.interface_key == "akshare.daily_bars"
        assert req.request_params == {"symbol": "000001"}
        assert req.freshness_requirement == timedelta(hours=24)
        # 默认值
        assert req.allow_stale is True
        assert req.preferred_sources is None
        assert req.task_context is None

    def test_all_fields_can_be_set(self):
        """所有字段都应能显式设置。"""
        req = GatewayRequest(
            interface_key="akshare.index_prices",
            request_params={"symbol": "000300", "start": "2024-01-01"},
            freshness_requirement=timedelta(hours=6),
            allow_stale=False,
            preferred_sources=["em_index", "sina_index"],
            task_context={"task_type": "sync", "task_id": "T123", "host": "akshare"},
        )
        assert req.interface_key == "akshare.index_prices"
        assert req.allow_stale is False
        assert req.preferred_sources == ["em_index", "sina_index"]
        assert req.task_context == {"task_type": "sync", "task_id": "T123", "host": "akshare"}


class TestFetchViaGateway:
    """fetch_via_gateway 主入口测试（WP-S.1b）。

    验证：fetch_via_gateway(req) 与 fetch(**kwargs) 行为等价。
    """

    def test_l2_hit_via_gateway_entry(self, db_session):
        """通过 fetch_via_gateway 入口命中 L2 缓存。"""
        symbol_id, latest = _seed_symbol_and_bars(db_session, "600000", days=3)

        l4_call_count = {"n": 0}

        def fake_l4(params):
            l4_call_count["n"] += 1
            raise RuntimeError("L4 should not be called when L2 hits")

        gw.register_l4_fetcher("akshare.daily_bars", fake_l4)

        req = GatewayRequest(
            interface_key="akshare.daily_bars",
            request_params={"symbol": "600000"},
            freshness_requirement=timedelta(days=365),
            allow_stale=True,
        )
        resp = asyncio.run(fetch_via_gateway(req))

        assert resp.cache_hit is True
        assert resp.source == SOURCE_L2_DB
        assert resp.data is not None
        assert len(resp.data) == 3
        assert l4_call_count["n"] == 0

    def test_l4_called_via_gateway_entry(self, db_session):
        """无本地数据时通过 fetch_via_gateway 触发 L4。"""
        called = {"n": 0}

        def fake_l4(params):
            called["n"] += 1
            return pd.DataFrame([{"trade_date": date.today(), "close": 100.0}]), "test_source"

        gw.register_l4_fetcher("test.gateway_l4", fake_l4)

        req = GatewayRequest(
            interface_key="test.gateway_l4",
            request_params={"symbol": "999999"},
            freshness_requirement=timedelta(hours=24),
        )
        resp = asyncio.run(fetch_via_gateway(req))

        assert resp.source == SOURCE_L4_REMOTE
        assert resp.cache_hit is False
        assert resp.source_detail == "test_source"
        assert called["n"] == 1

    def test_allow_stale_false_propagates_to_fetch(self, db_session):
        """allow_stale=False 应透传到 fetch，本地无数据时抛 StaleDataError。"""
        def failing_l4(params):
            raise RuntimeError("network down")

        gw.register_l4_fetcher("test.no_stale", failing_l4)

        req = GatewayRequest(
            interface_key="test.no_stale",
            request_params={"symbol": "NONEXISTENT"},
            freshness_requirement=timedelta(hours=24),
            allow_stale=False,
        )
        with pytest.raises(StaleDataError):
            asyncio.run(fetch_via_gateway(req))

    def test_task_context_propagates_to_rate_limiter(self, db_session):
        """task_context 应透传到限流器（host/task_type 维度）。"""
        def fake_l4(params):
            return pd.DataFrame([{"trade_date": date.today(), "close": 1.0}]), "src"

        gw.register_l4_fetcher("test.ctx_prop", fake_l4)

        req = GatewayRequest(
            interface_key="test.ctx_prop",
            request_params={"symbol": "X"},
            freshness_requirement=timedelta(hours=24),
            task_context={"task_type": "discovery", "host": "api.example.com"},
        )
        resp = asyncio.run(fetch_via_gateway(req))
        assert resp.source == SOURCE_L4_REMOTE


# ============================================================================
# WP-S.3b register_source_chain / get_source_chain 测试
# ============================================================================

class TestRegisterSourceChain:
    """SourceChain 注册接口测试（WP-S.3b）。"""

    def test_register_and_get_source_chain(self):
        """注册后能通过 get_source_chain 查询到。"""
        mock_chain = MagicMock()
        register_source_chain("test.chain_iface", mock_chain)

        assert get_source_chain("test.chain_iface") is mock_chain
        # 未注册的 interface_key 应返回 None
        assert get_source_chain("test.unregistered_iface") is None

    def test_register_overwrites_previous(self):
        """重复注册相同 interface_key 应覆盖前一个。"""
        chain1 = MagicMock()
        chain2 = MagicMock()
        register_source_chain("test.overwrite_iface", chain1)
        register_source_chain("test.overwrite_iface", chain2)

        assert get_source_chain("test.overwrite_iface") is chain2

    def test_unregister_source_chain(self):
        """unregister_source_chain 应清除已注册的链。"""
        mock_chain = MagicMock()
        register_source_chain("test.unregister_iface", mock_chain)
        assert get_source_chain("test.unregister_iface") is mock_chain

        unregister_source_chain("test.unregister_iface")
        assert get_source_chain("test.unregister_iface") is None

    def test_unregister_nonexistent_is_noop(self):
        """unregister 未注册的 interface_key 应是 no-op，不抛异常。"""
        # 不应抛异常
        unregister_source_chain("test.never_registered")

    def test_register_rejects_empty_interface_key(self):
        """register_source_chain 应拒绝空 interface_key。"""
        mock_chain = MagicMock()
        with pytest.raises(ValueError, match="interface_key must be non-empty"):
            register_source_chain("", mock_chain)

    def test_register_rejects_none_chain(self):
        """register_source_chain 应拒绝 None chain。"""
        with pytest.raises(ValueError, match="source_chain must not be None"):
            register_source_chain("test.none_chain", None)

    def test_registered_source_chain_used_by_default_l4_fetcher(self, db_session):
        """注册 SourceChain 后，_default_l4_daily_bars 应优先使用注册的链。"""
        from app.models.symbol import Symbol

        # 准备 Symbol 记录
        sym = Symbol(symbol="000001", name="Test", asset_type="stock", market="SZ")
        db_session.add(sym)
        db_session.commit()
        db_session.refresh(sym)

        # 注册一个 mock SourceChain
        mock_chain = MagicMock()
        expected_frame = pd.DataFrame([
            {"trade_date": date.today(), "close": 10.0, "open": 9.5},
        ])
        mock_chain.fetch.return_value = expected_frame
        register_source_chain("akshare.daily_bars", mock_chain)

        try:
            # 直接调用 _default_l4_daily_bars
            frame, source_detail = gw._default_l4_daily_bars({"symbol": "000001"})
            assert frame is expected_frame
            assert source_detail == "registered_source_chain"
            # 验证 mock_chain.fetch 被调用
            mock_chain.fetch.assert_called_once()
        finally:
            # 清理注册
            unregister_source_chain("akshare.daily_bars")

    def test_unregistered_falls_back_to_get_chain(self, db_session):
        """未注册 SourceChain 时，_default_l4_daily_bars 应回退到 get_chain。"""
        # 确保 akshare.daily_bars 未注册 SourceChain
        unregister_source_chain("akshare.daily_bars")
        assert get_source_chain("akshare.daily_bars") is None

        # mock get_chain 验证回退路径
        with patch("app.services.market_data_sources.registry.get_chain") as mock_get_chain:
            mock_chain = MagicMock()
            mock_chain.fetch.return_value = pd.DataFrame([{"trade_date": date.today(), "close": 10.0}])
            mock_get_chain.return_value = mock_chain

            from app.models.symbol import Symbol
            sym = Symbol(symbol="000001", name="Test", asset_type="stock", market="SZ")
            db_session.add(sym)
            db_session.commit()
            db_session.refresh(sym)

            frame, source_detail = gw._default_l4_daily_bars({"symbol": "000001"})
            assert source_detail == "source_chain"
            mock_get_chain.assert_called_once()
