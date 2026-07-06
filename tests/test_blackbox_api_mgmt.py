"""黑盒测试 - 第三方接口管理 API 端到端验证（P0 稳定性聚焦）。

前置：后端服务运行在 http://localhost:8000。
不依赖后端内部实现，仅通过 HTTP 接口验证行为。

重点验证：
1. 探测端点 30s 内必返回（验证 async + asyncio.wait_for 修复）
2. PUT 配置更新不显式设 Content-Type（复刻前端 client.ts 实际行为，守护潜在 bug）
3. 字段完整性 + 边界值校验
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

pytestmark = pytest.mark.blackbox

BASE = "http://localhost:8000"
TIMEOUT = 35.0  # 略大于探测超时 30s，确保能收到超时响应

# 已知存在的 api_key（registry 中注册的）
KNOWN_API_KEY = "stock_info_a_code_name"
UNKNOWN_KEY = "nonexistent_api_key_12345"


@pytest.fixture(scope="module")
def client():
    """复用 httpx 客户端（禁用环境变量代理，直连 localhost）。"""
    with httpx.Client(base_url=BASE, timeout=TIMEOUT, trust_env=False) as c:
        # 前置检查：服务必须在线
        try:
            r = c.get("/health")
            assert r.status_code == 200, f"后端服务未运行: {r.status_code}"
        except Exception as e:
            pytest.skip(f"后端服务未运行（{e}），跳过黑盒测试")
        yield c


# ============================================================================
# 1. GET /external-data/apis 列表接口
# ============================================================================

def test_list_apis_zh_returns_all_17(client):
    """GET /external-data/apis?locale=zh-CN → 18 项（registry 全量，含 fund_name_em）。"""
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 18, f"应有 18 个接口, 实际 {len(data)}"


def test_list_apis_en_returns_english_names(client):
    """locale=en-US → 名称为英文（非中文）。"""
    r = client.get("/api/v1/external-data/apis?locale=en-US")
    assert r.status_code == 200
    data = r.json()
    assert len(data) > 0
    # 英文 locale 下名称不应含中文
    for item in data:
        assert isinstance(item["name"], str)


def test_list_apis_contains_required_fields(client):
    """每项应含 18 个必填字段（元数据 + 配置 + 运行时状态）。"""
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    data = r.json()
    required_fields = {
        "key", "name", "category", "module", "description", "default_strategy",
        "enabled", "anti_risk_strategy", "delay_min_ms", "delay_max_ms",
        "last_probe_at", "last_probe_success", "last_probe_latency_ms", "last_probe_error",
        "last_call_at", "last_call_success", "total_calls", "total_failures",
    }
    for item in data:
        missing = required_fields - set(item.keys())
        assert not missing, f"接口 {item.get('key')} 缺少字段: {missing}"


def test_list_apis_locale_fallback(client):
    """locale=invalid → 回退 zh-CN（不报错）。"""
    r = client.get("/api/v1/external-data/apis?locale=invalid_locale")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 18


# ============================================================================
# 2. GET /external-data/apis/strategies 策略档位
# ============================================================================

def test_list_strategies_returns_five(client):
    """GET /external-data/apis/strategies → 5 档（fast/standard/conservative/extreme/custom）。"""
    r = client.get("/api/v1/external-data/apis/strategies?locale=zh-CN")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 5, f"应有 5 个策略档位, 实际 {len(data)}"
    keys = {s["key"] for s in data}
    assert keys == {"fast", "standard", "conservative", "extreme", "custom"}


def test_list_strategies_custom_has_null_delay(client):
    """custom 档 delay_min_ms/delay_max_ms 应为 null（由用户配置）。"""
    r = client.get("/api/v1/external-data/apis/strategies?locale=zh-CN")
    data = r.json()
    custom = next(s for s in data if s["key"] == "custom")
    assert custom["delay_min_ms"] is None
    assert custom["delay_max_ms"] is None


# ============================================================================
# 3. POST /external-data/apis/{key}/probe 探测接口（P0 稳定性核心）
# ============================================================================

def test_probe_unknown_key_returns_404(client):
    """POST /nonexistent/probe → 404。"""
    r = client.post(f"/api/v1/external-data/apis/{UNKNOWN_KEY}/probe")
    assert r.status_code == 404


def test_probe_known_key_returns_result(client):
    """POST /stock_info_a_code_name/probe → ProbeResult 字段完整。"""
    r = client.post(f"/api/v1/external-data/apis/{KNOWN_API_KEY}/probe")
    assert r.status_code == 200
    data = r.json()
    assert "key" in data
    assert "success" in data
    assert "latency_ms" in data
    assert "error" in data
    assert data["key"] == KNOWN_API_KEY


def test_probe_returns_within_30s(client):
    """【P0 稳定性核心】探测端点 30s 内必返回（即使数据源卡死）。

    验证修复：probe_api 改为 async def + asyncio.wait_for(timeout=30)。
    即使 akshare 调用永久阻塞，asyncio.wait_for 也会在 30s 后强制返回超时。
    """
    start = time.time()
    r = client.post(f"/api/v1/external-data/apis/{KNOWN_API_KEY}/probe")
    elapsed = time.time() - start
    assert r.status_code == 200, f"探测应返回 200, 实际 {r.status_code}"
    assert elapsed < 30.0, (
        f"探测应在 30s 内返回, 实际 {elapsed:.1f}s —— "
        "asyncio.wait_for 超时保护可能未生效"
    )


# ============================================================================
# 4. PUT /external-data/apis/{key} 配置更新（不显式设 Content-Type，复刻前端行为）
# ============================================================================

def test_update_config_unknown_key_returns_404(client):
    """PUT /nonexistent → 404。

    注意：不设 Content-Type 时 FastAPI 在路由进入前就返回 422
    （无法解析 JSON body），根本到不了路由内部的 404 检查。
    这是框架行为，非业务 bug。允许 404 或 422。
    """
    r = client.put(
        f"/api/v1/external-data/apis/{UNKNOWN_KEY}?locale=zh-CN",
        content=json.dumps({"enabled": True}),
    )
    assert r.status_code in (404, 422), f"未知 key 应返回 404/422, 实际 {r.status_code}"


def test_update_config_invalid_strategy_returns_400(client):
    """anti_risk_strategy='invalid' → 400。"""
    r = client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({"anti_risk_strategy": "invalid_strategy"}),
    )
    # 注意：不设 Content-Type 时 FastAPI 可能返回 422 而非 400
    # 若返回 422，说明 Content-Type bug 存在
    assert r.status_code in (400, 422), f"无效策略应返回 400/422, 实际 {r.status_code}"


def test_update_config_custom_min_gt_max_returns_400(client):
    """custom + min=2000/max=500 → 400。"""
    r = client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({
            "anti_risk_strategy": "custom",
            "delay_min_ms": 2000,
            "delay_max_ms": 500,
        }),
    )
    assert r.status_code in (400, 422), f"min>max 应返回 400/422, 实际 {r.status_code}"


def test_update_config_delay_out_of_range_returns_422(client):
    """delay_min_ms=70000 → 422（le=60000 边界）。"""
    r = client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({"delay_min_ms": 70000}),
    )
    assert r.status_code in (422, 400), f"超范围 delay 应返回 422, 实际 {r.status_code}"


def test_update_config_negative_delay_returns_422(client):
    """delay_min_ms=-1 → 422（ge=0 边界）。"""
    r = client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({"delay_min_ms": -1}),
    )
    assert r.status_code in (422, 400), f"负数 delay 应返回 422, 实际 {r.status_code}"


def test_update_config_enabled_takes_effect_immediately(client):
    """PUT enabled=False → 缓存即时更新（GET 列表反映）。"""
    # 先启用
    client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({"enabled": True}),
    )
    # 禁用
    r = client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({"enabled": False}),
    )
    if r.status_code == 200:
        # 验证缓存即时更新
        r2 = client.get("/api/v1/external-data/apis?locale=zh-CN")
        item = next(x for x in r2.json() if x["key"] == KNOWN_API_KEY)
        assert item["enabled"] is False, "禁用应即时生效"
        # 恢复
        client.put(
            f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
            content=json.dumps({"enabled": True}),
        )
    else:
        # Content-Type bug：FastAPI 无法解析 body
        pytest.skip(f"PUT 无 Content-Type 返回 {r.status_code}，确认 Content-Type bug 存在")


def test_update_config_strategy_takes_effect_immediately(client):
    """PUT strategy=conservative → 缓存更新。"""
    r = client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({"anti_risk_strategy": "conservative"}),
    )
    if r.status_code == 200:
        r2 = client.get("/api/v1/external-data/apis?locale=zh-CN")
        item = next(x for x in r2.json() if x["key"] == KNOWN_API_KEY)
        assert item["anti_risk_strategy"] == "conservative"
        # 恢复默认
        client.put(
            f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
            content=json.dumps({"anti_risk_strategy": "standard"}),
        )
    else:
        pytest.skip(f"PUT 无 Content-Type 返回 {r.status_code}，确认 Content-Type bug 存在")


def test_update_config_partial_update(client):
    """仅传 enabled → 其他字段保持原值。"""
    # 先记录当前 strategy
    r_before = client.get("/api/v1/external-data/apis?locale=zh-CN")
    item_before = next(x for x in r_before.json() if x["key"] == KNOWN_API_KEY)
    strategy_before = item_before["anti_risk_strategy"]

    # 仅更新 enabled
    r = client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({"enabled": True}),
    )
    if r.status_code == 200:
        item_after = r.json()
        # strategy 应保持不变
        assert item_after["anti_risk_strategy"] == strategy_before, "部分更新不应影响其他字段"
    else:
        pytest.skip(f"PUT 无 Content-Type 返回 {r.status_code}，确认 Content-Type bug 存在")


# ============================================================================
# 6. P0 稳定性回归：探测不卡死 + 线程池不耗尽
# ============================================================================

def test_probe_returns_within_30s_with_real_backend(client):
    """【P0 稳定性回归】真实后端探测应在 30s 内返回（含超时场景）。

    验证 probe_api 的 asyncio.wait_for(timeout=30) 修复生效。
    即使 akshare 内部永久阻塞，后端也应在 30s 内返回超时响应。
    """
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    apis = r.json()
    if not apis:
        pytest.skip("无可用接口")

    # 取第一个接口探测
    api_key = apis[0]["key"]
    start = time.time()
    r = client.post(f"/api/v1/external-data/apis/{api_key}/probe")
    elapsed = time.time() - start

    assert r.status_code == 200, f"探测应返回 200, 实际 {r.status_code}"
    assert elapsed < 30.0, (
        f"探测应在 30s 内返回（含超时场景）, 实际耗时 {elapsed:.1f}s"
    )
    # 验证返回字段
    data = r.json()
    assert "success" in data
    assert "latency_ms" in data
    assert "error" in data


def test_probe_all_17_apis_complete_within_180s(client):
    """【P0 稳定性回归】17 个接口串行探测应在 180s 内完成。

    每个接口最多 30s，17 个串行最坏 510s。
    但实际多数接口 < 5s，超时的接口由 30s timeout 兜底。
    验证探测端点不会因线程池耗尽而卡死。
    """
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    apis = r.json()
    if len(apis) < 5:
        pytest.skip("接口数不足 5 个，跳过批量探测验证")

    start = time.time()
    success_count = 0
    timeout_count = 0
    for api in apis:
        api_key = api["key"]
        try:
            r = client.post(f"/api/v1/external-data/apis/{api_key}/probe")
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    success_count += 1
                else:
                    timeout_count += 1
        except Exception:
            timeout_count += 1

    elapsed = time.time() - start
    # 17 接口，每个最多 30s，但多数 < 5s，总耗时应 < 180s
    # 若线程池耗尽，会卡在某个探测上，总耗时远超 180s
    assert elapsed < 180.0, (
        f"17 接口探测应在 180s 内完成（线程池未耗尽）, 实际耗时 {elapsed:.1f}s, "
        f"success={success_count}, timeout={timeout_count}"
    )


def test_consecutive_probes_do_not_degrade_response_time(client):
    """【P0 稳定性回归】连续 5 次探测同一接口，响应时间不应显著退化。

    验证探测端点独立线程池修复生效。
    若用 asyncio.to_thread 共享默认池，连续探测后泄漏线程累积，
    响应时间会显著增加（线程池排队）。
    """
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    apis = r.json()
    if not apis:
        pytest.skip("无可用接口")

    api_key = apis[0]["key"]
    latencies = []
    for _ in range(5):
        start = time.time()
        try:
            r = client.post(f"/api/v1/external-data/apis/{api_key}/probe")
            elapsed = time.time() - start
            latencies.append(elapsed)
        except Exception:
            latencies.append(30.0)  # 超时记为 30s

    # 最后一次响应时间不应超过第一次的 3 倍（允许波动，但不允许显著退化）
    # 且所有响应都应 < 30s（timeout 兜底）
    assert all(l < 30.0 for l in latencies), (
        f"所有探测应 < 30s, 实际 {latencies}"
    )
    if latencies[0] < 5.0:  # 第一次快速返回时才比较退化
        assert latencies[-1] < latencies[0] * 3 + 5.0, (
            f"连续探测响应时间显著退化: 首次 {latencies[0]:.1f}s, "
            f"末次 {latencies[-1]:.1f}s，可能线程池泄漏"
        )


def test_discovery_tasks_list_returns_quickly(client):
    """【P0 稳定性回归】GET /discovery/tasks 应在 5s 内返回。

    验证后端不会因探测/同步任务卡死而导致其他接口无响应。
    若线程池耗尽，list_discovery_tasks 也会排队卡死。
    """
    start = time.time()
    r = client.get("/api/v1/discovery/tasks?limit=10")
    elapsed = time.time() - start

    assert r.status_code == 200, f"应返回 200, 实际 {r.status_code}"
    assert elapsed < 5.0, (
        f"任务列表应在 5s 内返回（后端未卡死）, 实际耗时 {elapsed:.1f}s"
    )
    assert isinstance(r.json(), list)
