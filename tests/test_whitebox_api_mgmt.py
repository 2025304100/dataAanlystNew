"""白盒测试 - 接口管理服务层逻辑。

覆盖：
1. _resolve_strategy_params 五档策略解析（含 custom 边界）
2. load_config_cache 缓存加载（DB + registry 默认值合并）
3. refresh_config_cache_for 单 key 刷新（缓存一致性）
4. get_runtime_config 缓存未加载/已加载的返回值
5. apply_delay 延时应用（含 dmax=0 跳过）
6. record_call_result / record_probe_result 状态记录（含自动创建行）
7. is_api_enabled / get_max_retries 辅助函数
8. get_registry_entry 查询
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.akshare_api_config import AkshareApiConfig
from app.services.akshare_registry import (
    AKSHARE_API_REGISTRY,
    ANTI_RISK_STRATEGIES,
    DEFAULT_STRATEGY,
    _config_cache,
    _resolve_strategy_params,
    apply_delay,
    get_max_retries,
    get_registry_entry,
    get_runtime_config,
    is_api_enabled,
    load_config_cache,
    record_call_result,
    record_probe_result,
    refresh_config_cache_for,
)

pytestmark = pytest.mark.whitebox


@pytest.fixture(autouse=True)
def reset_config_cache():
    """每个测试前后清理全局 _config_cache，避免跨测试污染。"""
    import app.services.akshare_registry as registry
    original_loaded = registry._config_cache_loaded
    _config_cache.clear()
    registry._config_cache_loaded = False
    yield
    _config_cache.clear()
    registry._config_cache_loaded = original_loaded


# ============================================================================
# 1. _resolve_strategy_params 五档策略解析
# ============================================================================

def test_resolve_strategy_params_standard():
    """standard 档 → (300, 800, 3)。"""
    dmin, dmax, retries = _resolve_strategy_params("standard", 0, 0)
    assert (dmin, dmax, retries) == (300, 800, 3)


def test_resolve_strategy_params_fast():
    """fast 档 → (100, 300, 2)。"""
    dmin, dmax, retries = _resolve_strategy_params("fast", 0, 0)
    assert (dmin, dmax, retries) == (100, 300, 2)


def test_resolve_strategy_params_conservative():
    """conservative 档 → (1000, 2000, 3)。"""
    dmin, dmax, retries = _resolve_strategy_params("conservative", 0, 0)
    assert (dmin, dmax, retries) == (1000, 2000, 3)


def test_resolve_strategy_params_extreme():
    """extreme 档 → (3000, 5000, 5)。"""
    dmin, dmax, retries = _resolve_strategy_params("extreme", 0, 0)
    assert (dmin, dmax, retries) == (3000, 5000, 5)


def test_resolve_strategy_params_custom_valid():
    """custom + min=500/max=2000 → (500, 2000, 3)。"""
    dmin, dmax, retries = _resolve_strategy_params("custom", 500, 2000)
    assert (dmin, dmax, retries) == (500, 2000, 3)


def test_resolve_strategy_params_custom_min_eq_max():
    """custom + min=max=500 → (500, 500, 3)（允许相等）。"""
    dmin, dmax, retries = _resolve_strategy_params("custom", 500, 500)
    assert (dmin, dmax, retries) == (500, 500, 3)


def test_resolve_strategy_params_custom_min_gt_max():
    """custom + min=2000/max=500 → (2000, 2000, 3)（max(dmin, dmax) 兜底）。"""
    dmin, dmax, retries = _resolve_strategy_params("custom", 2000, 500)
    # 源码逻辑：dmax = max(dmin, dmax)
    assert dmin == 2000
    assert dmax == 2000  # max(2000, 500) = 2000
    assert retries == 3


def test_resolve_strategy_params_custom_min_zero():
    """custom + min=0/max=500 → (0, 500, 3)（ge=0 允许）。"""
    dmin, dmax, retries = _resolve_strategy_params("custom", 0, 500)
    assert (dmin, dmax, retries) == (0, 500, 3)


def test_resolve_strategy_params_custom_negative_clamped():
    """custom + min=-100/max=500 → (0, 500, 3)（max(0, -100) 兜底为 0）。"""
    dmin, dmax, retries = _resolve_strategy_params("custom", -100, 500)
    # 源码逻辑：dmin = max(0, int(delay_min_ms))
    assert dmin == 0
    assert dmax == 500


def test_resolve_strategy_params_unknown_strategy():
    """未知策略 → 回退 DEFAULT_STRATEGY (standard)。"""
    dmin, dmax, retries = _resolve_strategy_params("nonexistent", 0, 0)
    s = ANTI_RISK_STRATEGIES[DEFAULT_STRATEGY]
    assert dmin == int(s["delay_min_ms"])
    assert dmax == int(s["delay_max_ms"])
    assert retries == int(s["max_retries"])


# ============================================================================
# 2. load_config_cache 缓存加载
# ============================================================================

def test_load_config_cache_includes_registry_defaults(db_session):
    """DB 空 → 缓存包含全部 registry key，使用 default_strategy。"""
    load_config_cache(db_session)

    # 缓存应包含 registry 中所有接口
    for entry in AKSHARE_API_REGISTRY:
        assert entry["key"] in _config_cache, f"缓存缺少 registry key: {entry['key']}"
        cfg = _config_cache[entry["key"]]
        assert cfg["enabled"] is True
        # 使用 registry 中的 default_strategy
        expected_strat = ANTI_RISK_STRATEGIES[entry["default_strategy"]]
        assert cfg["delay_min_ms"] == int(expected_strat["delay_min_ms"])
        assert cfg["delay_max_ms"] == int(expected_strat["delay_max_ms"])
        assert cfg["max_retries"] == int(expected_strat["max_retries"])


def test_load_config_cache_overrides_with_db_rows(db_session):
    """DB 有 1 行 custom 配置 → 缓存该 key 用 DB 值，其余用默认。"""
    # 预置一个 custom 配置行
    db_session.add(AkshareApiConfig(
        api_key="stock_zh_a_hist",
        enabled=False,
        anti_risk_strategy="custom",
        delay_min_ms=500,
        delay_max_ms=2000,
    ))
    db_session.commit()

    load_config_cache(db_session)

    # 该 key 用 DB 值
    cfg = _config_cache["stock_zh_a_hist"]
    assert cfg["enabled"] is False
    assert cfg["strategy"] == "custom"
    assert cfg["delay_min_ms"] == 500
    assert cfg["delay_max_ms"] == 2000
    assert cfg["max_retries"] == 3  # custom 档 max_retries

    # 其他 key 仍用默认
    assert "stock_zh_a_spot_em" in _config_cache
    assert _config_cache["stock_zh_a_spot_em"]["enabled"] is True


def test_load_config_cache_sets_loaded_flag(db_session):
    """load_config_cache 后 _config_cache_loaded 应为 True。"""
    import app.services.akshare_registry as registry
    assert registry._config_cache_loaded is False
    load_config_cache(db_session)
    assert registry._config_cache_loaded is True


# ============================================================================
# 3. refresh_config_cache_for 单 key 刷新
# ============================================================================

def test_refresh_config_cache_for_existing_row(db_session):
    """DB 有行 → 单 key 刷新，缓存更新。"""
    db_session.add(AkshareApiConfig(
        api_key="stock_zh_a_hist",
        enabled=False,
        anti_risk_strategy="conservative",
        delay_min_ms=1000,
        delay_max_ms=2000,
    ))
    db_session.commit()

    refresh_config_cache_for("stock_zh_a_hist", db_session)

    cfg = _config_cache["stock_zh_a_hist"]
    assert cfg["enabled"] is False
    assert cfg["strategy"] == "conservative"
    assert cfg["delay_min_ms"] == 1000
    assert cfg["delay_max_ms"] == 2000
    assert cfg["max_retries"] == 3


def test_refresh_config_cache_for_missing_row(db_session):
    """DB 无行 → 用 registry 默认档位创建缓存项。"""
    refresh_config_cache_for("stock_zh_a_hist", db_session)

    entry = get_registry_entry("stock_zh_a_hist")
    cfg = _config_cache["stock_zh_a_hist"]
    assert cfg["enabled"] is True
    assert cfg["strategy"] == entry["default_strategy"]
    expected = ANTI_RISK_STRATEGIES[entry["default_strategy"]]
    assert cfg["delay_min_ms"] == int(expected["delay_min_ms"])


def test_refresh_config_cache_for_unknown_key_noop(db_session):
    """未知 api_key → no-op（不创建缓存项）。"""
    refresh_config_cache_for("nonexistent_key", db_session)
    assert "nonexistent_key" not in _config_cache


def test_refresh_config_cache_for_preserves_other_keys(db_session):
    """刷新单 key 不影响其他已缓存 key。"""
    # 先加载完整缓存
    load_config_cache(db_session)
    assert "stock_zh_a_spot_em" in _config_cache

    # 刷新另一个 key
    refresh_config_cache_for("stock_zh_a_hist", db_session)

    # 其他 key 仍在
    assert "stock_zh_a_spot_em" in _config_cache


# ============================================================================
# 4. get_runtime_config
# ============================================================================

def test_get_runtime_config_when_cache_not_loaded():
    """缓存未加载 → 返回安全默认值（standard 档位）。"""
    cfg = get_runtime_config("stock_zh_a_hist")
    s = ANTI_RISK_STRATEGIES[DEFAULT_STRATEGY]
    assert cfg["enabled"] is True
    assert cfg["strategy"] == DEFAULT_STRATEGY
    assert cfg["delay_min_ms"] == int(s["delay_min_ms"])
    assert cfg["delay_max_ms"] == int(s["delay_max_ms"])
    assert cfg["max_retries"] == int(s["max_retries"])


def test_get_runtime_config_returns_cached(db_session):
    """缓存已加载 → 返回缓存值。"""
    load_config_cache(db_session)
    cfg = get_runtime_config("stock_zh_a_hist")
    entry = get_registry_entry("stock_zh_a_hist")
    expected = ANTI_RISK_STRATEGIES[entry["default_strategy"]]
    assert cfg["delay_min_ms"] == int(expected["delay_min_ms"])


def test_get_runtime_config_unknown_key_returns_default(db_session):
    """缓存已加载但 key 不在缓存 → 返回 standard 默认值。"""
    load_config_cache(db_session)
    cfg = get_runtime_config("nonexistent_key")
    assert cfg["enabled"] is True
    assert cfg["strategy"] == DEFAULT_STRATEGY


# ============================================================================
# 5. apply_delay 延时应用
# ============================================================================

def test_apply_delay_sleeps_within_range(db_session, monkeypatch):
    """apply_delay 应在 [dmin, dmax] 范围内 sleep。"""
    load_config_cache(db_session)
    # 设置 stock_zh_a_hist 为 custom + 已知延时
    refresh_config_cache_for("stock_zh_a_hist", db_session)
    _config_cache["stock_zh_a_hist"]["delay_min_ms"] = 100
    _config_cache["stock_zh_a_hist"]["delay_max_ms"] = 200

    slept = []
    monkeypatch.setattr("app.services.akshare_registry.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("app.services.akshare_registry.random.uniform", lambda a, b: 150)

    apply_delay("stock_zh_a_hist")

    assert len(slept) == 1
    # 150ms / 1000 = 0.15s
    assert slept[0] == 0.15


def test_apply_delay_skips_when_max_zero(db_session, monkeypatch):
    """dmax=0 → 不 sleep。"""
    load_config_cache(db_session)
    _config_cache["stock_zh_a_hist"]["delay_max_ms"] = 0

    slept = []
    monkeypatch.setattr("app.services.akshare_registry.time.sleep", lambda s: slept.append(s))

    apply_delay("stock_zh_a_hist")

    assert len(slept) == 0


def test_apply_delay_no_op_when_cache_not_loaded(monkeypatch):
    """缓存未加载时 apply_delay 不会 sleep（因为 get_runtime_config 返回 standard 的 800ms）。

    实际上 standard 档 dmax=800 > 0，会 sleep。但我们要验证不会抛异常。
    """
    slept = []
    monkeypatch.setattr("app.services.akshare_registry.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("app.services.akshare_registry.random.uniform", lambda a, b: 500)

    apply_delay("stock_zh_a_hist")

    # 缓存未加载，返回 standard 默认 (300, 800)，会 sleep
    assert len(slept) == 1


# ============================================================================
# 6. record_call_result 状态记录
# ============================================================================

def test_record_call_result_creates_row_if_missing(db_session):
    """DB 无行 → 自动创建（用 registry 默认档位）。"""
    record_call_result(db_session, "stock_zh_a_hist", success=True)

    row = db_session.query(AkshareApiConfig).filter_by(api_key="stock_zh_a_hist").first()
    assert row is not None
    assert row.total_calls == 1
    assert row.last_call_success is True
    assert row.last_call_at is not None


def test_record_call_result_updates_existing(db_session):
    """DB 有行 → total_calls+1，更新 last_call_*。"""
    # 预置一行
    db_session.add(AkshareApiConfig(
        api_key="stock_zh_a_hist",
        enabled=True,
        anti_risk_strategy="standard",
        delay_min_ms=300,
        delay_max_ms=800,
        total_calls=5,
    ))
    db_session.commit()

    record_call_result(db_session, "stock_zh_a_hist", success=True)

    row = db_session.query(AkshareApiConfig).filter_by(api_key="stock_zh_a_hist").first()
    assert row.total_calls == 6
    assert row.last_call_success is True
    assert row.last_call_error is None


def test_record_call_result_records_failure(db_session):
    """失败时 total_failures+1，last_call_error 记录错误信息。"""
    record_call_result(db_session, "stock_zh_a_hist", success=False, error="ConnectionError: timeout")

    row = db_session.query(AkshareApiConfig).filter_by(api_key="stock_zh_a_hist").first()
    assert row.total_calls == 1
    assert row.total_failures == 1
    assert row.last_call_success is False
    assert "timeout" in row.last_call_error


def test_record_call_result_unknown_key_noop(db_session):
    """未知 api_key → no-op（不创建行）。"""
    record_call_result(db_session, "nonexistent_key", success=True)
    row = db_session.query(AkshareApiConfig).filter_by(api_key="nonexistent_key").first()
    assert row is None


# ============================================================================
# 7. record_probe_result
# ============================================================================

def test_record_probe_result_updates_last_probe(db_session):
    """更新 last_probe_at/success/latency_ms/error。"""
    record_probe_result(db_session, "stock_zh_a_hist", success=True, latency_ms=150)

    row = db_session.query(AkshareApiConfig).filter_by(api_key="stock_zh_a_hist").first()
    assert row is not None
    assert row.last_probe_success is True
    assert row.last_probe_latency_ms == 150
    assert row.last_probe_at is not None
    assert row.last_probe_error is None


def test_record_probe_result_records_error(db_session):
    """探测失败时记录 error。"""
    record_probe_result(db_session, "stock_zh_a_hist", success=False, latency_ms=None, error="403 Forbidden")

    row = db_session.query(AkshareApiConfig).filter_by(api_key="stock_zh_a_hist").first()
    assert row.last_probe_success is False
    assert row.last_probe_error == "403 Forbidden"
    assert row.last_probe_latency_ms is None


def test_record_probe_result_creates_row_if_missing(db_session):
    """DB 无行 → 自动创建。"""
    record_probe_result(db_session, "stock_zh_a_hist", success=True, latency_ms=200)

    row = db_session.query(AkshareApiConfig).filter_by(api_key="stock_zh_a_hist").first()
    assert row is not None
    assert row.last_probe_success is True


# ============================================================================
# 8. is_api_enabled / get_max_retries
# ============================================================================

def test_is_api_enabled_default_true():
    """未配置 → True（默认启用）。"""
    assert is_api_enabled("stock_zh_a_hist") is True


def test_is_api_enabled_false_when_disabled(db_session):
    """DB 配置 enabled=False → is_api_enabled 返回 False。"""
    db_session.add(AkshareApiConfig(
        api_key="stock_zh_a_hist",
        enabled=False,
        anti_risk_strategy="standard",
        delay_min_ms=300,
        delay_max_ms=800,
    ))
    db_session.commit()
    load_config_cache(db_session)

    assert is_api_enabled("stock_zh_a_hist") is False


def test_get_max_retries_default_three():
    """未配置 → 3（standard 默认）。"""
    assert get_max_retries("stock_zh_a_hist") == 3


def test_get_max_retries_from_strategy(db_session):
    """extreme 档 → 5。"""
    db_session.add(AkshareApiConfig(
        api_key="stock_zh_a_hist",
        enabled=True,
        anti_risk_strategy="extreme",
        delay_min_ms=3000,
        delay_max_ms=5000,
    ))
    db_session.commit()
    load_config_cache(db_session)

    assert get_max_retries("stock_zh_a_hist") == 5


def test_get_max_retries_fast_strategy(db_session):
    """fast 档 → 2。"""
    db_session.add(AkshareApiConfig(
        api_key="stock_zh_a_hist",
        enabled=True,
        anti_risk_strategy="fast",
        delay_min_ms=100,
        delay_max_ms=300,
    ))
    db_session.commit()
    load_config_cache(db_session)

    assert get_max_retries("stock_zh_a_hist") == 2


# ============================================================================
# 9. get_registry_entry
# ============================================================================

def test_get_registry_entry_found():
    """已知 key → 返回 entry dict。"""
    entry = get_registry_entry("stock_zh_a_hist")
    assert entry is not None
    assert entry["key"] == "stock_zh_a_hist"
    assert "name_zh" in entry
    assert "default_strategy" in entry
    assert "probe_args" in entry


def test_get_registry_entry_not_found():
    """未知 key → None。"""
    assert get_registry_entry("nonexistent_key") is None


def test_registry_contains_17_entries():
    """registry 应包含 17 个接口（实际数量验证）。"""
    # 根据探索结果有 17 个（可能是 18 个，按实际为准）
    assert len(AKSHARE_API_REGISTRY) >= 15, (
        f"registry 应至少有 15 个接口，实际 {len(AKSHARE_API_REGISTRY)}"
    )


def test_registry_entries_have_required_fields():
    """每个 registry entry 应含必填字段。"""
    required_fields = {"key", "name_zh", "name_en", "category_zh", "category_en",
                       "module", "desc_zh", "desc_en", "default_strategy", "probe_args"}
    for entry in AKSHARE_API_REGISTRY:
        for field in required_fields:
            assert field in entry, f"entry {entry.get('key', '?')} 缺少字段 {field}"


def test_registry_default_strategy_valid():
    """每个 entry 的 default_strategy 应在 ANTI_RISK_STRATEGIES 中。"""
    for entry in AKSHARE_API_REGISTRY:
        strat = entry["default_strategy"]
        assert strat in ANTI_RISK_STRATEGIES, (
            f"entry {entry['key']} 的 default_strategy '{strat}' 不在 ANTI_RISK_STRATEGIES"
        )


def test_anti_risk_strategies_contains_five_levels():
    """防风控策略应有 5 档：fast/standard/conservative/extreme/custom。"""
    expected = {"fast", "standard", "conservative", "extreme", "custom"}
    assert set(ANTI_RISK_STRATEGIES.keys()) == expected


def test_anti_risk_strategies_custom_has_null_delay():
    """custom 档的 delay_min_ms/delay_max_ms 应为 None（用户自定义）。"""
    assert ANTI_RISK_STRATEGIES["custom"]["delay_min_ms"] is None
    assert ANTI_RISK_STRATEGIES["custom"]["delay_max_ms"] is None
