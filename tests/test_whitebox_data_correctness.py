"""白盒测试 - 数据正确性守护 (P0 回归)。

验证：
1. ProbeResult / _sync_one_symbol 返回值字段完整性（真实行为）
2. universe 刷新返回值结构（真实行为）
3. watchdog 终态保护（真实行为）
4. _fetch_history 重试次数（真实行为，验证每源只调用 1 次）
5. 子线程独立 Session（真实行为）
6. Content-Type header（schema 约束）

这是 P0 稳定性修复的数据正确性守护测试，防止：
- 返回值字段缺失 → 前端解析报错
- 重试次数回退 → 总耗时超 90s 超时
- 子线程共享 Session → 死锁或脏数据

本文件已从源码字符串断言（inspect.getsource）升级为真实行为测试，
避免重构时断言失效。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.api.routes.akshare_apis import ProbeResult, ApiConfigUpdate
from app.services import discovery_tasks, market_data

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. ProbeResult 字段完整性（已是真实行为测试，保留）
# ============================================================================

def test_probe_result_fields_complete():
    """【P0 数据正确性】ProbeResult 应包含 key/success/latency_ms/error 字段。

    前端依赖这些字段渲染探测结果，缺失会导致 UI 报错。
    """
    result = ProbeResult(key="test_api", success=True, latency_ms=150, error=None)
    assert result.key == "test_api"
    assert result.success is True
    assert result.latency_ms == 150
    assert result.error is None

    # 验证字段是可选的（latency_ms 和 error 可为 None）
    failed = ProbeResult(key="test_api", success=False, latency_ms=None, error="timeout")
    assert failed.latency_ms is None
    assert failed.error == "timeout"


def test_probe_latency_ms_is_int_or_null():
    """【P0 数据正确性】latency_ms 应为 int 或 None。

    前端用 latency_ms 显示延迟，非 int/None 会导致渲染异常。
    """
    # int
    r1 = ProbeResult(key="test", success=True, latency_ms=150)
    assert isinstance(r1.latency_ms, int) or r1.latency_ms is None

    # None（超时场景）
    r2 = ProbeResult(key="test", success=False, latency_ms=None, error="timeout")
    assert r2.latency_ms is None


# ============================================================================
# 2. _sync_one_symbol 返回值结构（真实行为测试）
# ============================================================================

def test_sync_one_symbol_returns_ok_dict(monkeypatch, db_session):
    """【P0 数据正确性】_sync_one_symbol 成功时返回 dict 含 status=ok。

    使用 cached_bars 路径，mock _latest_bar / calculate_symbol_score / upsert_trade_setup，
    验证返回值结构正确，调用方依赖 result["status"] 判断成功/失败/空数据。
    """
    from app.models.symbol import Symbol
    from app.schemas.discovery import DiscoveryTaskCreate

    # 创建测试 symbol
    symbol = Symbol(symbol="TEST001", name="Test", asset_type="stock", market="sh", board="main", is_active=1)
    db_session.add(symbol)
    db_session.commit()
    db_session.refresh(symbol)

    # mock _latest_bar 返回一个 bar
    mock_bar = MagicMock()
    mock_bar.trade_date = date(2024, 1, 1)
    monkeypatch.setattr(discovery_tasks, "_latest_bar", lambda db, s: mock_bar)

    # mock calculate_symbol_score 返回简单 score
    mock_score = MagicMock()
    mock_score.quality_score = 80
    mock_score.timing_score = 70
    mock_score.stage = "accumulate"
    mock_score.action = "buy"
    monkeypatch.setattr(discovery_tasks, "calculate_symbol_score", lambda **kwargs: mock_score)

    # mock upsert_trade_setup（portfolio_id=None 时不会调用，但 mock 防止意外）
    monkeypatch.setattr(discovery_tasks, "upsert_trade_setup", lambda **kwargs: None)

    payload = DiscoveryTaskCreate(
        scope="cn-stock",
        use_cached_bars_first=True,
        start_date="2024-01-01",
        end_date="2024-12-31",
    )

    result, scored = discovery_tasks._sync_one_symbol(db_session, symbol, payload, portfolio_id=None)
    assert isinstance(result, dict)
    assert "status" in result, "_sync_one_symbol 返回的 dict 应包含 'status' 键"
    assert result["status"] == "ok", "cached_bars 路径应返回 status='ok'"
    assert scored is True, "cached_bars 路径应返回 scored=True"
    assert result["source"] == "cached_bars"
    assert "latest_score" in result


def test_sync_one_symbol_returns_failed_on_sync_error(monkeypatch, db_session):
    """【P0 数据正确性】_sync_one_symbol 同步失败时返回 status=failed。

    mock sync_symbol_daily_bars 返回 failed 状态，验证 result["status"]=="failed"。
    """
    from app.models.symbol import Symbol
    from app.schemas.discovery import DiscoveryTaskCreate

    symbol = Symbol(symbol="TEST002", name="Test", asset_type="stock", market="sh", board="main", is_active=1)
    db_session.add(symbol)
    db_session.commit()
    db_session.refresh(symbol)

    # mock _latest_bar 返回 None（不走 cached_bars 路径）
    monkeypatch.setattr(discovery_tasks, "_latest_bar", lambda db, s: None)
    # mock refresh_symbol_name
    monkeypatch.setattr(discovery_tasks, "refresh_symbol_name", lambda s: None)
    # mock sync_symbol_daily_bars 返回 failed
    monkeypatch.setattr(
        discovery_tasks,
        "sync_symbol_daily_bars",
        lambda **kwargs: ({"status": "failed", "error": "sync error"}, False)[0],
    )

    payload = DiscoveryTaskCreate(
        scope="cn-stock",
        use_cached_bars_first=False,
        start_date="2024-01-01",
        end_date="2024-12-31",
    )

    result, scored = discovery_tasks._sync_one_symbol(db_session, symbol, payload, portfolio_id=None)
    assert isinstance(result, dict)
    assert result["status"] == "failed"
    assert scored is False


# ============================================================================
# 3. _sync_one_symbol_with_timeout 超时抛出 TimeoutError（真实行为测试）
# ============================================================================

def test_sync_one_symbol_with_timeout_raises_on_block(monkeypatch, db_session):
    """【P0 数据正确性】_sync_one_symbol_with_timeout 在阻塞时抛出 TimeoutError。

    调用方（_run_discovery_task）捕获 TimeoutError 后记录 failed_count += 1。
    本测试验证超时机制生效，是 failed_count 正确递增的前提。
    """
    from app.models.symbol import Symbol
    from app.schemas.discovery import DiscoveryTaskCreate

    symbol = Symbol(symbol="TEST003", name="Test", asset_type="stock", market="sh", board="main", is_active=1)
    db_session.add(symbol)
    db_session.commit()
    db_session.refresh(symbol)

    # mock _sync_one_symbol 阻塞（sleep 时间略大于 timeout）
    def blocking_sync(db, sym, payload, portfolio_id):
        time.sleep(2)
        return {"status": "ok"}, True

    monkeypatch.setattr(discovery_tasks, "_sync_one_symbol", blocking_sync)
    monkeypatch.setattr(discovery_tasks, "SYNC_ONE_SYMBOL_TIMEOUT_SECONDS", 0.5)

    payload = DiscoveryTaskCreate(scope="cn-stock", start_date="2024-01-01", end_date="2024-12-31")

    with pytest.raises(TimeoutError):
        discovery_tasks._sync_one_symbol_with_timeout(db_session, symbol, payload, None)


# ============================================================================
# 4. universe 刷新返回值结构（真实行为测试）
# ============================================================================

def test_universe_refresh_returns_seen_and_created(monkeypatch, db_session):
    """【P0 数据正确性】_refresh_cn_stock_universe 返回 dict 含 seen/created。

    mock _cn_stock_universe_frame 返回 DataFrame，验证返回 dict 含 seen/created 字段。
    调用方依赖 seen 判断是否失败（seen=0 中断任务）。
    """
    # mock _cn_stock_universe_frame 返回 2 行 DataFrame
    df = pd.DataFrame([
        {"code": "600000", "name": "测试股票1"},
        {"code": "000001", "name": "测试股票2"},
    ])
    monkeypatch.setattr(discovery_tasks, "_cn_stock_universe_frame", lambda db: df)

    result = discovery_tasks._refresh_cn_stock_universe(db_session)
    assert isinstance(result, dict), "_refresh_cn_stock_universe 应返回 dict"
    assert "seen" in result, "应返回 dict 含 'seen'"
    assert "created" in result, "应返回 dict 含 'created'"
    assert result["seen"] == 2, "2 行有效数据 → seen=2"


def test_universe_refresh_seen_zero_when_empty_frame(monkeypatch, db_session):
    """【P0 数据正确性】universe 刷新空 DataFrame 时 seen=0，调用方据此中止任务。"""
    df = pd.DataFrame(columns=["code", "name"])
    monkeypatch.setattr(discovery_tasks, "_cn_stock_universe_frame", lambda db: df)

    result = discovery_tasks._refresh_cn_stock_universe(db_session)
    assert result["seen"] == 0


# ============================================================================
# 5. _fetch_history 重试次数（真实行为测试）
# ============================================================================

def test_fetch_history_retries_only_once_per_source(monkeypatch):
    """【P0 数据正确性】_fetch_history 每个数据源只尝试 1 次（range(1)）。

    历史问题：原 range(3) 导致 3 源 × 3 次 = 9 次 × 20s = 180s > 90s 超时。
    修复：改为 range(1)，3 源 × 1 次 = 60s < 90s。
    本测试 mock 所有源失败，验证每源只调用 1 次。
    """
    from app.models.symbol import Symbol

    symbol = Symbol(symbol="600000", name="Test", asset_type="stock", market="sh", board="main", is_active=1)

    # 计数器：每个源的调用次数
    call_counts = {"em": 0, "sina": 0, "tx": 0}

    def failing_em(**kwargs):
        call_counts["em"] += 1
        raise Exception("em failed")

    def failing_sina(**kwargs):
        call_counts["sina"] += 1
        raise Exception("sina failed")

    def failing_tx(**kwargs):
        call_counts["tx"] += 1
        raise Exception("tx failed")

    # mock akshare 三个源都失败
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", failing_em)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_daily", failing_sina)
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist_tx", failing_tx)

    # _fetch_history 内部有 _proxy_bypass 和 quiet_akshare_output 上下文管理器，需确保不报错
    with pytest.raises(Exception):
        market_data._fetch_history(symbol, date(2024, 1, 1), date(2024, 12, 31), "qfq")

    # 每个源只调用 1 次（range(1)），总调用 3 次
    assert call_counts["em"] == 1, f"em 源应只调用 1 次，实际 {call_counts['em']}"
    assert call_counts["sina"] == 1, f"sina 源应只调用 1 次，实际 {call_counts['sina']}"
    assert call_counts["tx"] == 1, f"tx 源应只调用 1 次，实际 {call_counts['tx']}"


# ============================================================================
# 6. 子线程独立 Session（真实行为测试）
# ============================================================================

def test_sync_one_symbol_uses_independent_db_session(monkeypatch, db_session):
    """【P0 数据正确性】_sync_one_symbol_with_timeout 子线程使用独立 Session。

    历史问题：原实现把主线程 db 传给子线程，Session 非线程安全，
    超时后主子线程并发操作同一 Session 导致死锁或脏数据。
    修复：子线程内创建独立 sub_db = SessionLocal()。
    本测试验证 _sync_one_symbol 被调用时收到的 db 不是主线程的 db_session。
    """
    from app.models.symbol import Symbol
    from app.schemas.discovery import DiscoveryTaskCreate

    symbol = Symbol(symbol="TEST004", name="Test", asset_type="stock", market="sh", board="main", is_active=1)
    db_session.add(symbol)
    db_session.commit()
    db_session.refresh(symbol)

    # 记录 _sync_one_symbol 被调用时的 db 参数
    captured_dbs = []

    def capture_db(db, sym, payload, portfolio_id):
        captured_dbs.append(db)
        return {"status": "ok"}, True

    monkeypatch.setattr(discovery_tasks, "_sync_one_symbol", capture_db)

    payload = DiscoveryTaskCreate(scope="cn-stock", start_date="2024-01-01", end_date="2024-12-31")
    discovery_tasks._sync_one_symbol_with_timeout(db_session, symbol, payload, None)

    # 验证子线程使用了独立 Session（不是主线程的 db_session）
    assert len(captured_dbs) == 1, "_sync_one_symbol 应被调用 1 次"
    assert captured_dbs[0] is not db_session, "子线程应使用独立 Session，不是主线程的 db_session"


# ============================================================================
# 7. watchdog 终态保护（真实行为测试）
# ============================================================================

def test_watchdog_does_not_overwrite_terminal_status(monkeypatch, db_session):
    """【P0 数据正确性】watchdog 遇到终态任务时立即退出，不更新 updated_at。

    防止 watchdog 把 cancelled/failed 任务的 updated_at 刷新，导致终态保护失效。
    本测试创建一个 cancelled 任务，启动 watchdog，验证 updated_at 不变。
    """
    from app.models.discovery import DiscoveryTaskRecord

    # 创建一个已 cancelled 的任务（终态）
    old_updated = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    task = DiscoveryTaskRecord(
        id="test-watchdog-terminal",
        status="cancelled",
        stage="cancelled",
        percent=100,
        message="已取消",
        scope="cn-stock",
        updated_at=old_updated,
        payload_json='{"scope":"cn-stock","start_date":"2024-01-01","end_date":"2024-12-31"}',
        processed_symbol_ids_json="[]",
        synced_symbol_ids_json="[]",
        errors_json="[]",
    )
    db_session.add(task)
    db_session.commit()

    # 启动 watchdog（缩短心跳间隔加速测试）
    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.2)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=discovery_tasks._watchdog_heartbeat,
        args=("test-watchdog-terminal", stop_event),
        daemon=True,
    )
    thread.start()

    # 等待 2 轮心跳（0.4s），确保 watchdog 有机会运行
    time.sleep(0.5)
    stop_event.set()
    thread.join(timeout=2)

    # 验证 updated_at 未被更新（终态任务 watchdog 立即退出）
    db_session.expire_all()
    task_after = db_session.get(DiscoveryTaskRecord, "test-watchdog-terminal")
    assert task_after is not None
    assert task_after.updated_at == old_updated, (
        f"终态任务 updated_at 不应被 watchdog 更新，"
        f"预期 {old_updated}，实际 {task_after.updated_at}"
    )


def test_watchdog_updates_heartbeat_for_running_task(monkeypatch, db_session):
    """【P0 数据正确性】watchdog 对 running 任务（非 prepare 阶段）应正常更新 updated_at。

    防止 watchdog 失效导致正常任务被 _expire_stale_tasks 误判为 stale。
    """
    from app.models.discovery import DiscoveryTaskRecord

    old_updated = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    task = DiscoveryTaskRecord(
        id="test-watchdog-running",
        status="running",
        stage="sync",  # 非 prepare 阶段，应正常心跳
        percent=50,
        message="同步中",
        scope="cn-stock",
        updated_at=old_updated,
        payload_json='{"scope":"cn-stock","start_date":"2024-01-01","end_date":"2024-12-31"}',
        processed_symbol_ids_json="[]",
        synced_symbol_ids_json="[]",
        errors_json="[]",
    )
    db_session.add(task)
    db_session.commit()

    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.2)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=discovery_tasks._watchdog_heartbeat,
        args=("test-watchdog-running", stop_event),
        daemon=True,
    )
    thread.start()

    time.sleep(0.5)
    stop_event.set()
    thread.join(timeout=2)

    # 验证 updated_at 已被更新（前进）
    db_session.expire_all()
    task_after = db_session.get(DiscoveryTaskRecord, "test-watchdog-running")
    assert task_after is not None
    assert task_after.updated_at > old_updated, (
        f"running 任务（sync 阶段）updated_at 应被 watchdog 更新，"
        f"原 {old_updated}，现 {task_after.updated_at}"
    )


def test_watchdog_skips_heartbeat_in_prepare_stage(monkeypatch, db_session):
    """【P0 数据正确性】watchdog 在 prepare 阶段不更新 updated_at，让 _expire_stale_tasks 兜底。

    prepare 阶段卡死（universe 刷新永久阻塞）时，updated_at 不再刷新，
    10min 后 _expire_stale_tasks 会标记 failed。
    """
    from app.models.discovery import DiscoveryTaskRecord

    old_updated = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    task = DiscoveryTaskRecord(
        id="test-watchdog-prepare",
        status="running",
        stage="prepare",  # prepare 阶段，应跳过心跳
        percent=8,
        message="刷新 universe 中",
        scope="cn-stock",
        updated_at=old_updated,
        payload_json='{"scope":"cn-stock","start_date":"2024-01-01","end_date":"2024-12-31"}',
        processed_symbol_ids_json="[]",
        synced_symbol_ids_json="[]",
        errors_json="[]",
    )
    db_session.add(task)
    db_session.commit()

    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.2)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=discovery_tasks._watchdog_heartbeat,
        args=("test-watchdog-prepare", stop_event),
        daemon=True,
    )
    thread.start()

    time.sleep(0.5)
    stop_event.set()
    thread.join(timeout=2)

    # 验证 updated_at 未被更新（prepare 阶段跳过心跳）
    db_session.expire_all()
    task_after = db_session.get(DiscoveryTaskRecord, "test-watchdog-prepare")
    assert task_after is not None
    assert task_after.updated_at == old_updated, (
        f"prepare 阶段 updated_at 不应被 watchdog 更新，"
        f"预期 {old_updated}，实际 {task_after.updated_at}"
    )


# ============================================================================
# 8. universe 刷新超时返回 None（已是真实行为测试，保留）
# ============================================================================

def test_universe_refresh_timeout_returns_none_and_records_error(monkeypatch):
    """【P0 数据正确性】_refresh_universe_with_timeout 超时返回 None，调用方标记 task=failed。

    防止超时后任务继续跑空扫描。
    """
    monkeypatch.setattr(discovery_tasks, "UNIVERSE_REFRESH_TIMEOUT_SECONDS", 0.3)

    def _block(db, payload):
        time.sleep(10)
        return {"seen": 100}

    monkeypatch.setattr(discovery_tasks, "_refresh_discovery_universe", _block)

    from app.schemas.discovery import DiscoveryTaskCreate
    payload = DiscoveryTaskCreate(
        scope="cn-stock",
        refresh_universe=True,
        start_date="2024-01-01",
        end_date="2024-12-31",
    )

    result = discovery_tasks._refresh_universe_with_timeout(MagicMock(), payload)
    assert result is None, "超时应返回 None"


# ============================================================================
# 9. Content-Type header 验证（已是真实行为测试，保留）
# ============================================================================

def test_probe_akshare_api_content_type_header():
    """【P0 数据正确性】PUT /external-data/apis/{key} 应能被 FastAPI 正确解析。

    前端 client.ts 的 updateAkshareApiConfig 用 body: JSON.stringify(payload)
    未显式设 Content-Type，FastAPI 仍应能解析（pydantic 兼容）。
    本测试验证 ApiConfigUpdate schema 的字段约束。
    """
    # 验证 ApiConfigUpdate 的字段约束
    fields = ApiConfigUpdate.model_fields
    assert "enabled" in fields, "应支持 enabled 字段"
    assert "anti_risk_strategy" in fields, "应支持 anti_risk_strategy 字段"
    assert "delay_min_ms" in fields, "应支持 delay_min_ms 字段"
    assert "delay_max_ms" in fields, "应支持 delay_max_ms 字段"

    # 验证合法值能通过
    config = ApiConfigUpdate(enabled=True, delay_min_ms=300, delay_max_ms=800)
    assert config.enabled is True
    assert config.delay_min_ms == 300
    assert config.delay_max_ms == 800

    # 验证非法值被拒绝
    with pytest.raises(Exception):
        ApiConfigUpdate(delay_min_ms=-1)  # ge=0
    with pytest.raises(Exception):
        ApiConfigUpdate(delay_min_ms=70000)  # le=60000
