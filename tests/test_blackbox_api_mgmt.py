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
# PT-DEF-18 已收口：探测不再由 HTTP 请求等上游（改成提交任务 + 心跳轮询），
# 所以这里不存在"读超时必须大于产品探测预算"这件事了 —— 历史上的
# 30 / 50 / 55 三边不一致正是从这里消失的。TIMEOUT 现在只是一个普通的
# 网络保护值；若哪天有人再把 probe 改回同步等上游，
# test_probe_submit_is_instant_even_with_slow_upstream 会第一时间红。
TIMEOUT = 30.0

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
            # 本地 skip，CI（REQUIRE_LIVE_BACKEND=1）直接失败：不能让“全体 skip
            # 后 0 failed”被当成 blackbox 通过（实测后端挂掉时会发生）。
            from tests._live_backend_guard import skip_or_fail_no_live_backend

            skip_or_fail_no_live_backend(e)
        yield c


# ============================================================================
# 1. GET /external-data/apis 列表接口
# ============================================================================

def test_list_apis_zh_keeps_original_registry_entries(client):
    """接口扩展后仍应完整保留最初注册的 18 项。"""
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 18, f"接口注册表不应少于 18 项, 实际 {len(data)}"


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
    expected = client.get(
        "/api/v1/external-data/apis?locale=zh-CN"
    ).json()
    r = client.get("/api/v1/external-data/apis?locale=invalid_locale")
    assert r.status_code == 200
    data = r.json()
    assert [item["key"] for item in data] == [
        item["key"] for item in expected
    ]


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


def test_probe_known_key_returns_task_receipt(client):
    """POST /{key}/probe → 202 任务回执（PT-DEF-18：不再同步等上游）。"""
    r = client.post(f"/api/v1/external-data/apis/{KNOWN_API_KEY}/probe", timeout=TIMEOUT)
    assert r.status_code == 202, f"提交探测应 202, 实际 {r.status_code}: {r.text[:200]}"
    data = r.json()
    assert data.get("task_id"), f"回执必须带 task_id, 实际 {data}"
    assert data.get("api_key") == KNOWN_API_KEY
    assert data.get("status") in ("queued", "running"), f"初始状态异常: {data}"
    assert isinstance(data.get("reused"), bool)


def _poll_probe_until_rest(client, task_id: str, *, max_wait: float = 120.0) -> dict:
    """轮询到"界面能给出结论"为止：终态，或心跳已断（可疑）。

    这里的 max_wait 是**测试自己收束**的上限，不是对产品时长做任何断言 ——
    这正是本次改造的意义：产品侧不再有"必须在 N 秒内返回"的赌注。
    """
    deadline = time.time() + max_wait
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/api/v1/external-data/apis/probe/{task_id}", timeout=TIMEOUT)
        assert r.status_code == 200, f"轮询任务应 200, 实际 {r.status_code}: {r.text[:200]}"
        last = r.json()
        if last.get("status") in ("done", "failed", "cancelled", "interrupted"):
            return last
        if last.get("heartbeat_stale"):
            return last
        time.sleep(1.0)
    pytest.fail(
        f"探测任务 {task_id} 在测试上限 {max_wait:.0f}s 内既没终态也没报心跳异常，"
        f"最后一次状态：{last}"
    )


def test_probe_task_reaches_conclusion_with_heartbeat(client):
    """提交后轮询：必须走到终态或明确报"心跳可疑"，绝不能停在无结论的 running。"""
    r = client.post(f"/api/v1/external-data/apis/{KNOWN_API_KEY}/probe", timeout=TIMEOUT)
    task_id = r.json()["task_id"]

    final = _poll_probe_until_rest(client, task_id)

    assert final.get("heartbeat_at"), f"任务应至少有一次心跳: {final}"
    assert final.get("status") != "running" or final.get("heartbeat_stale"), (
        f"停在 running 却不报心跳异常，界面会无限转圈: {final}"
    )
    if final.get("status") == "done":
        result = final.get("result")
        assert isinstance(result, dict), f"done 却没带可展示的结果: {final}"
        assert "success" in result and "latency_ms" in result and "error" in result


# ============================================================================
# 4. PUT /external-data/apis/{key} 配置更新
#
# 口径重划（体检报告 §十五）：本节以前故意“不显式设 Content-Type，复刻前端行为”，
# 后果是两类假结果：
#   • 正向用例拿不到 200 → 直接 pytest.skip，于是“保存是否生效”从来未被验证；
#   • 负向用例写 `status in (400, 422)` → 缺头造成的 422 也能让它绿，等于什么也没校。
# 现在统一用 `json=` 发标准 application/json，断言才落在产品自己的校验上。
# （浏览器默认对字符串 body 发 text/plain 也会被后端拒：那是前端 bug PT-DEF-17，
# 已在 api/client.ts 统一补 Content-Type；下方仍留一条用例钉住“缺/错类型会被拒”。）
# ============================================================================

def _put_config(client, payload, key=None):
    """按标准 JSON 发 PUT（显式 Content-Type 由 httpx 的 json= 负责）。"""
    return client.put(
        f"/api/v1/external-data/apis/{key or KNOWN_API_KEY}?locale=zh-CN",
        json=payload,
    )


def test_update_config_unknown_key_returns_404(client):
    """PUT 未知 key → 404（路由内检查，不再被 422 混淆）。"""
    r = _put_config(client, {"enabled": True}, key=UNKNOWN_KEY)
    assert r.status_code == 404, f"未知 key 应返回 404, 实际 {r.status_code}: {r.text}"


def test_update_config_invalid_strategy_returns_400(client):
    """anti_risk_strategy='invalid' → 400（产品自己拦的，不是框架解不了 body）。"""
    r = _put_config(client, {"anti_risk_strategy": "invalid_strategy"})
    assert r.status_code == 400, f"无效策略应返回 400, 实际 {r.status_code}: {r.text}"


def _get_strategy(client):
    """读回当前保存的风控策略（用于验证“被拒的请求确实没落库”）。"""
    items = client.get("/api/v1/external-data/apis?locale=zh-CN").json()
    return next(x for x in items if x["key"] == KNOWN_API_KEY)


def test_update_config_custom_min_gt_max_returns_400(client):
    """custom + min=2000/max=500 → 被拒，并且不得落库。

    400 还是 422 取决于校验落在路由内还是 Pydantic 边界，两者都是合法表达；
    真正要钉的是“非法区间没被写进去”（旧写法只校状态码，写不进去看不出来）。
    """
    before = _get_strategy(client)
    r = _put_config(client, {
        "anti_risk_strategy": "custom",
        "delay_min_ms": 2000,
        "delay_max_ms": 500,
    })
    assert r.status_code in (400, 422), f"min>max 应被拒, 实际 {r.status_code}: {r.text}"
    after = _get_strategy(client)
    assert after["anti_risk_strategy"] == before["anti_risk_strategy"], (
        "非法区间不应被落库"
    )


def test_update_config_delay_out_of_range_returns_422(client):
    """delay_min_ms=70000 → 被拒且不落库（le=60000 边界）。"""
    before = _get_strategy(client)
    r = _put_config(client, {"delay_min_ms": 70000})
    assert r.status_code in (422, 400), f"超范围 delay 应被拒, 实际 {r.status_code}"
    assert _get_strategy(client)["delay_min_ms"] == before["delay_min_ms"], (
        "超范围值不应被写入配置"
    )


def test_update_config_negative_delay_returns_422(client):
    """delay_min_ms=-1 → 被拒且不落库（ge=0 边界）。"""
    before = _get_strategy(client)
    r = _put_config(client, {"delay_min_ms": -1})
    assert r.status_code in (422, 400), f"负数 delay 应被拒, 实际 {r.status_code}"
    assert _get_strategy(client)["delay_min_ms"] == before["delay_min_ms"], (
        "负数值不应被写入配置"
    )


def test_put_config_without_content_type_is_rejected(client):
    """契约记录：缺 Content-Type 的字符串 body 会被后端拒（422）。

    浏览器对字符串 body 默认发 text/plain，也会被拒——所以前端曾在
    `updateAkshareApiConfig` 上“保存永远 422”（PT-DEF-17，已修：api/client.ts
    统一补 Content-Type）。本用例钉住后端行为，一旦后端改为宽容接受
    text/plain，这里会提醒我们两边口径变了。
    """
    r = client.put(
        f"/api/v1/external-data/apis/{KNOWN_API_KEY}?locale=zh-CN",
        content=json.dumps({"enabled": True}),
    )
    assert r.status_code == 422, (
        f"无 Content-Type 的字符串 body 应被拒 422, 实际 {r.status_code}: {r.text}"
    )


def test_update_config_enabled_takes_effect_immediately(client):
    """PUT enabled=False → 缓存即时更新（GET 列表反映）。

    旧写法拿不到 200 就 `pytest.skip(“确认 Content-Type bug 存在”)`，
    等于“保存是否生效”从来没被验证过；现在走标准 JSON，必须真绿。
    用例自带 finally 恢复原值，不把开关状态留在开发环里。
    """
    before = _get_strategy(client)["enabled"]
    try:
        target = False if before else True
        r = _put_config(client, {"enabled": target})
        assert r.status_code == 200, f"PUT enabled 应返回 200, 实际 {r.status_code}: {r.text}"
        assert _get_strategy(client)["enabled"] == target, "enabled 变更应即时生效"
    finally:
        _put_config(client, {"enabled": bool(before)})


def test_update_config_strategy_takes_effect_immediately(client):
    """PUT strategy=conservative → 配置真的落库并可读回；结束后恢复原值。"""
    before = _get_strategy(client)["anti_risk_strategy"]
    try:
        r = _put_config(client, {"anti_risk_strategy": "conservative"})
        assert r.status_code == 200, f"PUT strategy 应返回 200, 实际 {r.status_code}: {r.text}"
        assert _get_strategy(client)["anti_risk_strategy"] == "conservative"
    finally:
        _put_config(client, {"anti_risk_strategy": before})


def test_update_config_partial_update(client):
    """仅传 enabled → 其他字段保持原值（部分更新不得清空 strategy）。

    旧写法同样在拿不到 200 时 skip，所以“部分更新”语义从未被验证。
    """
    item_before = _get_strategy(client)
    strategy_before = item_before["anti_risk_strategy"]
    enabled_before = item_before["enabled"]
    flipped = False if enabled_before else True
    try:
        r = _put_config(client, {"enabled": flipped})
        assert r.status_code == 200, f"部分更新应返回 200, 实际 {r.status_code}: {r.text}"
        assert r.json()["anti_risk_strategy"] == strategy_before, "部分更新不应影响其他字段"
    finally:
        _put_config(client, {"enabled": bool(enabled_before)})


# ============================================================================
# 6. P0 稳定性回归（PT-DEF-18 之后）：提交快 + 不饿死别的接口
#
# 旧这一节靠"探测必须在 30s 内返回"来证线程池没耗尽，前提已随同步接口一起消失。
# 现在真正要守住的是两件事：① 提交必须即时（它不再等上游）；② 探测在后台跑的时候，
# 别的接口不能被饿死。两条都不需要赌上游快慢，因此摘掉了 xfail_dev_hardware
# —— 那顶帽子过去只会让这条守护在 CI 上永远"预期失败"，等于没测。
# ============================================================================

def test_probe_submit_is_instant_even_with_slow_upstream(client):
    """【P0】提交探测应即时返回，不等上游：这是"前端不再猜超时"的前提。"""
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    apis = r.json()
    if not apis:
        pytest.skip("无可用接口")

    api_key = apis[0]["key"]
    start = time.time()
    r = client.post(f"/api/v1/external-data/apis/{api_key}/probe", timeout=TIMEOUT)
    elapsed = time.time() - start

    assert r.status_code == 202, f"提交应 202, 实际 {r.status_code}: {r.text[:200]}"
    assert elapsed < 2.0, (
        f"提交接口应在 2s 内回执（它不该等上游），实际 {elapsed:.1f}s —— "
        "说明 POST 又退化成同步探测了"
    )


def test_probes_in_flight_do_not_starve_sibling_endpoints(client):
    """【P0】一批探测正在后台跑时，其它接口必须照常响应（原来会排队卡死）。"""
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    apis = r.json()
    if len(apis) < 5:
        pytest.skip("接口数不足 5 个，跳过并发探测验证")

    submitted = []
    for api in apis[:6]:
        resp = client.post(f"/api/v1/external-data/apis/{api['key']}/probe", timeout=TIMEOUT)
        if resp.status_code == 202:
            submitted.append(resp.json()["task_id"])
    assert submitted, "至少应成功提交一个探测任务"

    try:
        start = time.time()
        tasks = client.get("/api/v1/discovery/tasks?limit=10", timeout=TIMEOUT)
        elapsed = time.time() - start
        assert tasks.status_code == 200, f"任务列表应 200, 实际 {tasks.status_code}"
        assert elapsed < 5.0, (
            f"探测在后台执行期间，任务列表应在 5s 内返回（后端未被拖死），实际 {elapsed:.1f}s"
        )

        # 每个已提交的任务都必须查得到，且状态可解释
        for task_id in submitted:
            st = client.get(f"/api/v1/external-data/apis/probe/{task_id}", timeout=TIMEOUT)
            assert st.status_code == 200, f"已提交的任务不应 404: {task_id}"
            body = st.json()
            assert body.get("status") in (
                "queued", "running", "done", "failed", "cancelled", "interrupted",
            ), f"任务状态不可识别: {body}"
    finally:
        # 不把一堆未收束的探测留给下一条用例（它们会继续打上游）
        for task_id in submitted:
            try:
                client.get(f"/api/v1/external-data/apis/probe/{task_id}", timeout=5.0)
            except Exception:
                pass


def test_repeated_probe_submits_stay_fast(client):
    """【P0】连续提交同一接口 5 次：每次都即时返回，不出现排队恶化。

    旧实现连续探测会累积泄漏线程导致响应变慢；现在探测在 worker/线程池里跑，
    提交路径只是一次 INSERT + 起线程，因此可以直接守住"不恶化"。
    """
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    apis = r.json()
    if not apis:
        pytest.skip("无可用接口")

    api_key = apis[0]["key"]
    elapsed_list = []
    for _ in range(5):
        start = time.time()
        resp = client.post(f"/api/v1/external-data/apis/{api_key}/probe", timeout=TIMEOUT)
        elapsed_list.append(time.time() - start)
        assert resp.status_code == 202

    assert max(elapsed_list) < 3.0, (
        f"连续提交的单次耗时应保持在秒级内，实际 {['%.2f' % e for e in elapsed_list]}"
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
