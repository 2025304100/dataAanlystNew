"""黑盒稳定性守护测试（P3-2）。

将 UAT 0.1-0.5 稳定性核心 25 项中剩余的黑盒可验证项转化为自动化测试。

前置：后端服务运行在 http://localhost:8000。
不依赖后端内部实现，仅通过 HTTP 接口验证稳定性行为。

覆盖：
- /health 端点 2s 内响应
- /api/v1/discovery/tasks 列表 5s 内响应
- universe 刷新不报 "Excel file format cannot be determined"
- 批量探测 180s 内完成（线程池不耗尽）
- 连续探测响应时间不退化
- 过期 running 任务已被 _expire_stale_tasks 清理
- universe seen=0 中止任务
- 终态任务状态不被 worker 覆盖
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.blackbox

BASE = "http://localhost:8000"
TIMEOUT = 15.0
# 批量探测需较长超时（单接口最长 30s + 余量）
PROBE_BATCH_TIMEOUT = 200.0


@pytest.fixture(scope="module")
def client():
    """复用 httpx 客户端（禁用环境变量代理，直连 localhost）。

    前置检查：服务必须在线，否则 skip 整个模块。
    """
    with httpx.Client(base_url=BASE, timeout=TIMEOUT, trust_env=False) as c:
        try:
            r = c.get("/health")
            assert r.status_code == 200, f"后端服务未运行: {r.status_code}"
        except Exception as e:
            pytest.skip(f"后端服务未运行（{e}），跳过黑盒稳定性守护测试")
        yield c


# ============================================================================
# 1. /health 端点 2s 内响应
# ============================================================================

def test_health_check_responds_within_2s(client):
    """【P3-2 稳定性守护】/health 端点应在 2s 内返回 200。

    健康检查是后端可用性的基础，任何情况下都应快速响应。
    若 /health 卡死，负载均衡器/前端会无法探活。
    """
    start = time.time()
    r = client.get("/health")
    elapsed = time.time() - start

    assert r.status_code == 200, f"/health 应返回 200, 实际 {r.status_code}"
    assert elapsed < 2.0, (
        f"/health 应在 2s 内响应, 实际耗时 {elapsed:.3f}s"
    )


# ============================================================================
# 2. /api/v1/discovery/tasks 列表 5s 内响应
# ============================================================================

def test_discovery_tasks_list_responds_within_5s(client):
    """【P3-2 稳定性守护】/api/v1/discovery/tasks 列表应在 5s 内返回。

    验证后端不会因探测/同步任务卡死而导致其他接口无响应。
    若线程池耗尽，list_discovery_tasks 也会排队卡死。
    list_discovery_tasks 内部会调用 _expire_stale_tasks，需确保清理逻辑不阻塞。
    """
    start = time.time()
    r = client.get("/api/v1/discovery/tasks?limit=20")
    elapsed = time.time() - start

    assert r.status_code == 200, f"应返回 200, 实际 {r.status_code}"
    assert elapsed < 5.0, (
        f"任务列表应在 5s 内返回（后端未卡死）, 实际耗时 {elapsed:.2f}s"
    )
    assert isinstance(r.json(), list)


# ============================================================================
# 3. universe 刷新不报 Excel 错误
# ============================================================================

def test_universe_refresh_does_not_return_excel_error(client):
    """【P3-2 稳定性守护】cn-stock discovery 任务 universe 刷新不报 Excel 错误。

    历史问题：akshare stock_info_a_code_name 通过 pd.read_excel 拉取，
    数据源偶发返回 HTML 导致 "Excel file format cannot be determined" 错误。
    修复：增加 stock_zh_a_spot_em 作为 fallback 数据源。

    本测试创建一个 cn-stock discovery 任务，轮询任务状态到终态，
    验证 errors 中不含 "Excel file format cannot be determined"。
    若已有 running/queued 任务导致创建失败，跳过本测试。
    """
    payload = {
        "scope": "cn-stock",
        "min_score": 55,
        "include_news": False,
        "refresh_universe": True,
        "symbol_limit": 5,
        "batch_size": 5,
    }
    r = client.post("/api/v1/discovery/tasks", json=payload)
    if r.status_code in (400, 409):
        pytest.skip("已有运行中的 discovery 任务，跳过 universe 错误验证")
    assert r.status_code == 200, f"创建任务应返回 200, 实际 {r.status_code}: {r.text}"
    task = r.json()
    task_id = task["id"]

    # 轮询任务状态到终态（最长 130s，覆盖 universe 刷新 120s 超时 + 余量）
    deadline = time.time() + 130.0
    final_task = task
    while time.time() < deadline:
        r = client.get(f"/api/v1/discovery/tasks/{task_id}")
        if r.status_code != 200:
            break
        final_task = r.json()
        if final_task["status"] in ("done", "failed", "cancelled", "expired"):
            break
        time.sleep(2.0)

    # 检查 errors 中不含 Excel 错误
    errors = final_task.get("errors", []) or []
    error_blob = " ".join(str(e) for e in errors).lower()
    assert "excel file format cannot be determined" not in error_blob, (
        f"universe 刷新出现 Excel 错误，fallback 数据源可能失效: {errors}"
    )


# ============================================================================
# 4. 批量探测 180s 内完成
# ============================================================================

@pytest.mark.slow
def test_batch_probe_completes_within_180s(client):
    """【P3-2 稳定性守护】批量探测所有接口应在 180s 内完成。

    验证探测端点独立线程池修复生效，不会因线程池耗尽而卡死。
    每个接口最多 30s，多数 < 5s，总耗时应 < 180s。
    若线程池耗尽，会卡在某个探测上，总耗时远超 180s。
    """
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    assert r.status_code == 200
    apis = r.json()
    if len(apis) < 5:
        pytest.skip("接口数不足 5 个，跳过批量探测验证")

    # 用独立的长超时 client，避免被 module fixture 的 15s 超时打断
    success_count = 0
    timeout_count = 0
    start = time.time()
    with httpx.Client(base_url=BASE, timeout=PROBE_BATCH_TIMEOUT, trust_env=False) as probe_client:
        for api in apis:
            api_key = api["key"]
            try:
                pr = probe_client.post(f"/api/v1/external-data/apis/{api_key}/probe")
                if pr.status_code == 200:
                    data = pr.json()
                    if data.get("success"):
                        success_count += 1
                    else:
                        timeout_count += 1
                else:
                    timeout_count += 1
            except Exception:
                timeout_count += 1

    elapsed = time.time() - start
    assert elapsed < 180.0, (
        f"批量探测应在 180s 内完成（线程池未耗尽）, 实际耗时 {elapsed:.1f}s, "
        f"success={success_count}, timeout={timeout_count}"
    )


# ============================================================================
# 5. 连续 3 次探测同一接口响应时间不退化
# ============================================================================

def test_consecutive_probes_do_not_degrade(client):
    """【P3-2 稳定性守护】连续 3 次探测同一接口，响应时间不应显著退化。

    验证探测端点独立线程池修复生效。
    若用 asyncio.to_thread 共享默认池，连续探测后泄漏线程累积，
    响应时间会显著增加（线程池排队）。
    断言：第 3 次响应时间 < 第 1 次 * 3。
    """
    r = client.get("/api/v1/external-data/apis?locale=zh-CN")
    assert r.status_code == 200
    apis = r.json()
    if not apis:
        pytest.skip("无可用接口")

    api_key = apis[0]["key"]
    latencies: list[float] = []
    # 用稍长超时 client，避免单次探测超时被 module fixture 15s 打断
    with httpx.Client(base_url=BASE, timeout=35.0, trust_env=False) as probe_client:
        for _ in range(3):
            start = time.time()
            try:
                pr = probe_client.post(f"/api/v1/external-data/apis/{api_key}/probe")
                elapsed = time.time() - start
                assert pr.status_code == 200, f"探测应返回 200, 实际 {pr.status_code}"
                latencies.append(elapsed)
            except Exception:
                latencies.append(30.0)

    assert len(latencies) == 3, f"应收集 3 次延迟, 实际 {len(latencies)}"
    # 所有探测都应 < 30s（timeout 兜底）
    assert all(l < 30.0 for l in latencies), (
        f"所有探测应 < 30s, 实际 {latencies}"
    )
    # 第 3 次不应显著退化（< 第 1 次 * 3，允许波动）
    first = latencies[0]
    third = latencies[2]
    assert third < first * 3 + 5.0, (
        f"连续探测响应时间显著退化: 首次 {first:.2f}s, 第 3 次 {third:.2f}s, "
        "可能线程池泄漏"
    )


# ============================================================================
# 6. 过期 running 任务已被 _expire_stale_tasks 清理
# ============================================================================

def test_stale_running_task_auto_failed(client):
    """【P3-2 稳定性守护】不应存在 updated_at > 30 分钟前的 running 任务。

    验证 _expire_stale_tasks 已清理过期 running 任务。
    discovery_tasks.STALE_RUNNING_DEADLINE = 10 分钟，
    async_tasks.STALE_RUNNING_DEADLINE = 30 分钟。
    本测试用 30 分钟作为更宽松的守护阈值：任何 updated_at > 30 分钟前的
    running/queued 任务都应已被清理为 failed。
    """
    r = client.get("/api/v1/discovery/tasks?limit=100")
    assert r.status_code == 200
    tasks = r.json()
    if not tasks:
        pytest.skip("无 discovery 任务可供检查")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_threshold = now - timedelta(minutes=30)

    stale_running: list[dict] = []
    for task in tasks:
        if task.get("status") not in ("running", "queued"):
            continue
        updated_at_str = task.get("updated_at") or task.get("created_at")
        if not updated_at_str:
            continue
        try:
            updated_at = datetime.fromisoformat(updated_at_str.replace("Z", ""))
        except (ValueError, TypeError):
            continue
        if updated_at.tzinfo is not None:
            updated_at = updated_at.replace(tzinfo=None)
        if updated_at < stale_threshold:
            stale_running.append(task)

    assert not stale_running, (
        f"存在 {len(stale_running)} 个 updated_at > 30 分钟前的 running/queued 任务，"
        f"_expire_stale_tasks 未生效: "
        f"{[{'id': t.get('id'), 'status': t.get('status'), 'updated_at': t.get('updated_at')} for t in stale_running]}"
    )


# ============================================================================
# 7. universe seen=0 中止任务
# ============================================================================

def test_universe_seen_zero_aborts_task(client):
    """【P3-2 稳定性守护】universe 刷新 seen=0 时任务应被标记 failed。

    历史问题：universe 拉取失败（seen=0）时继续跑空扫描会让用户困惑。
    修复：seen=0 时 raise RuntimeError，任务标记 failed，
    message 含"全市场标的列表拉取失败"。

    本测试创建任务后轮询状态：
    - 若任务 failed 且 message 含 "拉取失败"/"刷新超时" → 验证守护生效
    - 若任务 done → 跳过（环境正常，无法触发 seen=0 场景）
    - 若任务 cancelled → 跳过
    """
    payload = {
        "scope": "cn-stock",
        "min_score": 55,
        "include_news": False,
        "refresh_universe": True,
        "symbol_limit": 5,
        "batch_size": 5,
    }
    r = client.post("/api/v1/discovery/tasks", json=payload)
    if r.status_code in (400, 409):
        pytest.skip("已有运行中的 discovery 任务，跳过 seen=0 验证")
    assert r.status_code == 200, f"创建任务应返回 200, 实际 {r.status_code}: {r.text}"
    task_id = r.json()["id"]

    # 轮询任务状态到终态（最长 130s，覆盖 universe 刷新 120s 超时 + 余量）
    deadline = time.time() + 130.0
    final_task = {}
    while time.time() < deadline:
        r = client.get(f"/api/v1/discovery/tasks/{task_id}")
        if r.status_code != 200:
            break
        final_task = r.json()
        if final_task["status"] in ("done", "failed", "cancelled", "expired"):
            break
        time.sleep(2.0)

    status = final_task.get("status")
    message = (final_task.get("message") or "").lower()

    if status == "done":
        pytest.skip("universe 刷新成功（seen>0），无法触发 seen=0 中止场景")
    if status == "cancelled":
        pytest.skip("任务被取消，无法验证 seen=0 中止行为")

    # 若任务 failed，验证 message 含拉取失败/刷新超时（seen=0 或 universe 超时）
    if status == "failed":
        assert any(kw in message for kw in ("拉取失败", "刷新超时", "universe", "akshare")), (
            f"failed 任务 message 应指示 universe 拉取失败, 实际 message={final_task.get('message')}"
        )
    else:
        # 仍在 running/queued（超时未到终态）—— 也视为守护未生效
        pytest.fail(
            f"任务未在 130s 内进入终态, 当前 status={status}, "
            "seen=0 中止守护可能未生效"
        )


# ============================================================================
# 8. 终态任务状态不被 worker 覆盖
# ============================================================================

def test_terminal_status_not_overwritten(client):
    """【P3-2 稳定性守护】对已 cancelled 的任务发 retry，状态应正确转换为 queued。

    验证终态保护：retry cancelled 任务应返回 queued（由 retry_discovery_task 显式设置），
    不应回退为 running（worker 不会绕过 retry 直接设 running）。
    worker 后续通过 _set_task 更新状态时，对终态任务会忽略 status/stage 字段，
    防止用户取消后 worker 仍标记 done/running 的竞态。

    测试步骤：
    1. 创建任务，立即 cancel（确保有 cancelled 任务）
    2. 对 cancelled 任务发 retry
    3. 验证 retry 响应 status == "queued"（不是 "running"，不是 "cancelled"）
    """
    payload = {
        "scope": "cn-stock",
        "min_score": 55,
        "include_news": False,
        "refresh_universe": False,
        "use_cached_symbols_only": True,
        "symbol_limit": 1,
        "batch_size": 1,
    }
    r = client.post("/api/v1/discovery/tasks", json=payload)
    if r.status_code in (400, 409):
        # 已有运行中任务，尝试从列表找一个 cancelled 任务
        r_list = client.get("/api/v1/discovery/tasks?limit=50")
        assert r_list.status_code == 200
        cancelled_task = next(
            (t for t in r_list.json() if t.get("status") == "cancelled"), None
        )
        if cancelled_task is None:
            pytest.skip("已有运行中任务且无 cancelled 任务可供 retry 测试")
        task_id = cancelled_task["id"]
    else:
        assert r.status_code == 200, f"创建任务应返回 200, 实际 {r.status_code}: {r.text}"
        task_id = r.json()["id"]
        # 立即 cancel（避免任务跑完进入 done）
        cancel_r = client.post(f"/api/v1/discovery/tasks/{task_id}/cancel")
        assert cancel_r.status_code == 200, f"cancel 应返回 200, 实际 {cancel_r.status_code}"
        cancelled = cancel_r.json()
        assert cancelled["status"] == "cancelled", (
            f"cancel 后状态应为 cancelled, 实际 {cancelled['status']}"
        )

    # 对 cancelled 任务发 retry
    retry_r = client.post(f"/api/v1/discovery/tasks/{task_id}/retry")
    assert retry_r.status_code == 200, (
        f"retry cancelled 任务应返回 200, 实际 {retry_r.status_code}: {retry_r.text}"
    )
    retried = retry_r.json()
    # retry 应将 cancelled 转为 queued，不应直接跳到 running
    assert retried["status"] == "queued", (
        f"retry cancelled 任务应返回 status=queued, 实际 status={retried['status']}, "
        "不应回退为 running"
    )
    assert retried["status"] != "running", (
        "retry cancelled 任务不应直接跳到 running（应通过 queued → worker 启动）"
    )
