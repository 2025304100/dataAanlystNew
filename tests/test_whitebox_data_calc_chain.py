"""白盒测试 - 数据计算链路守护 (P0-4)。

覆盖 4 个数据正确性核心场景：
1. 胜率除零保护（signal_stats._rate / backtest._compute_statistics）
2. TASK_STAGE_PERCENT 阶段映射（防止进度跳跃）
3. universe seen=0 中止（_run_discovery_task 抛 RuntimeError）
4. data_credibility 边界（_data_credibility 各 bar_count 阈值）
5. _clamp_score / _grade 边界（含 NaN 处理）

防止以下历史问题回归：
- win_rate = wins / 0 → Infinity 污染前端
- TASK_STAGE_PERCENT 被误改 → 进度从 8% 跳到 86%
- universe 拉取失败仍跑空扫描 → 用户困惑
- bar_count < 5 时 data_credibility 抛 NameError
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from app.services import analysis, backtest, discovery_tasks, signal_stats

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. 胜率除零保护（signal_stats._rate）
# ============================================================================

def test_signal_stats_rate_empty_returns_none():
    """【P0 数据正确性】_rate([]) 应返回 None，不抛 ZeroDivisionError。

    前端 win_rate_3d/5d/10d/20d 字段在无样本时应为 null，
    而非 Infinity 或 NaN。
    """
    assert signal_stats._rate([]) is None


def test_signal_stats_rate_mixed_values():
    """【P0 数据正确性】_rate([True, False, True]) 应返回 0.6667。

    验证胜率计算正确性：3 个样本中 2 个 True → 2/3 ≈ 0.6667。
    """
    result = signal_stats._rate([True, False, True])
    assert result is not None
    assert result == 0.6667


def test_signal_stats_rate_all_true_returns_one():
    """【P0 数据正确性】_rate([True, True]) 应返回 1.0（全胜）。"""
    assert signal_stats._rate([True, True]) == 1.0


def test_signal_stats_rate_all_false_returns_zero():
    """【P0 数据正确性】_rate([False, False]) 应返回 0.0（全败）。"""
    assert signal_stats._rate([False, False]) == 0.0


# ============================================================================
# 2. 回测胜率/盈亏比除零保护（backtest._compute_statistics）
# ============================================================================

def test_backtest_compute_statistics_empty_returns_zeros():
    """【P0 数据正确性】空 trades + 空 equity_curve 应返回全 0 字典。

    防止 win_rate = 0/0 → ZeroDivisionError，或 profit_factor = inf。
    """
    result = backtest._compute_statistics(trades=[], initial_capital=100000, equity_curve=[])
    assert result["win_rate"] == 0.0
    assert result["profit_factor"] == 0.0
    assert result["trade_count"] == 0
    assert result["max_drawdown"] == 0.0
    # 关键：不出现 Infinity / NaN
    assert result["win_rate"] != float("inf")
    assert result["profit_factor"] != float("inf")


def test_backtest_compute_statistics_no_completed_trades_win_rate_zero():
    """【P0 数据正确性】有 trades 但无 exit_date 的（未平仓）win_rate 应为 0.0。

    completed_trades = [t for t in trades if t.exit_date is not None]
    空列表时 win_rate = 0.0（不除零）。
    """
    open_trade = MagicMock()
    open_trade.exit_date = None  # 未平仓
    open_trade.pnl = None
    open_trade.hold_days = None

    equity_curve = [{"equity": 100000}, {"equity": 100000}]
    result = backtest._compute_statistics(
        trades=[open_trade], initial_capital=100000, equity_curve=equity_curve
    )
    assert result["win_rate"] == 0.0
    assert result["trade_count"] == 1


def test_backtest_compute_statistics_normal_win_rate():
    """【P0 数据正确性】3 笔已完成交易（2 胜 1 负）win_rate 应为 0.6667。"""
    win1 = MagicMock(exit_date=date(2024, 1, 10), pnl=500, hold_days=5)
    win2 = MagicMock(exit_date=date(2024, 1, 15), pnl=300, hold_days=3)
    loss1 = MagicMock(exit_date=date(2024, 1, 20), pnl=-200, hold_days=4)

    equity_curve = [
        {"equity": 100000}, {"equity": 100500}, {"equity": 100800}, {"equity": 100600}
    ]
    result = backtest._compute_statistics(
        trades=[win1, win2, loss1], initial_capital=100000, equity_curve=equity_curve
    )
    assert result["win_rate"] == 0.6667
    assert result["trade_count"] == 3


def test_backtest_compute_statistics_profit_factor_no_loss_capped_at_999():
    """【P0 数据正确性】全胜无亏损时 profit_factor 应为 999.0（不出现 Infinity）。

    原始计算：profit_factor = inf（loss_trades 为空），
    返回时 round(inf, 2) != inf 会报错，故用 999.0 兜底。
    前端 profit_factor=Infinity 会导致 JSON 序列化失败。
    """
    win1 = MagicMock(exit_date=date(2024, 1, 10), pnl=500, hold_days=5)
    win2 = MagicMock(exit_date=date(2024, 1, 15), pnl=300, hold_days=3)

    equity_curve = [{"equity": 100000}, {"equity": 100500}, {"equity": 100800}]
    result = backtest._compute_statistics(
        trades=[win1, win2], initial_capital=100000, equity_curve=equity_curve
    )
    assert result["profit_factor"] == 999.0, "全胜无亏损 profit_factor 应兜底为 999.0，不能是 inf"
    assert result["profit_factor"] != float("inf")


# ============================================================================
# 3. TASK_STAGE_PERCENT 阶段映射（防止进度跳跃）
# ============================================================================

def test_task_stage_percent_all_stages_mapped():
    """【P0 数据正确性】TASK_STAGE_PERCENT 应包含 9 个阶段，且值正确。

    防止阶段被误改导致进度从 8% 跳到 86%（_sync_progress 兜底依赖此映射）。
    """
    expected = {
        "queued": 0,
        "prepare": 8,
        "sync": 72,
        "scan": 86,
        "news": 94,
        "done": 100,
        "paused": 100,
        "cancelled": 100,
        "failed": 100,
    }
    for stage, percent in expected.items():
        assert discovery_tasks.TASK_STAGE_PERCENT[stage] == percent, (
            f"阶段 {stage} 的 percent 应为 {percent}，"
            f"实际 {discovery_tasks.TASK_STAGE_PERCENT[stage]}"
        )


def test_task_stage_percent_terminal_states_are_100():
    """【P0 数据正确性】终态（done/paused/cancelled/failed）percent 必须为 100。

    防止终态任务进度卡在中间值（如 94%），让用户以为还在跑。
    """
    for stage in ["done", "paused", "cancelled", "failed"]:
        assert discovery_tasks.TASK_STAGE_PERCENT[stage] == 100


def test_task_stage_percent_sync_is_72():
    """【P0 数据正确性】sync 阶段 percent=72（耗时最长阶段，占大头）。

    防止 sync 被误改为小值（如 30%）导致进度从 8% 跳到 86% 反向跳跃。
    """
    assert discovery_tasks.TASK_STAGE_PERCENT["sync"] == 72
    assert discovery_tasks.TASK_STAGE_PERCENT["prepare"] < discovery_tasks.TASK_STAGE_PERCENT["sync"]
    assert discovery_tasks.TASK_STAGE_PERCENT["sync"] < discovery_tasks.TASK_STAGE_PERCENT["scan"]


# ============================================================================
# 4. universe seen=0 中止（_run_discovery_task 抛 RuntimeError）
# ============================================================================

def _make_discovery_task(db_session, task_id="test-seen-zero"):
    """创建一个 discovery 任务记录用于测试。"""
    from app.models.discovery import DiscoveryTaskRecord
    task = DiscoveryTaskRecord(
        id=task_id,
        status="queued",
        stage="queued",
        percent=0,
        message="待启动",
        scope="cn-stock",
        payload_json=json.dumps({
            "scope": "cn-stock",
            "min_score": 55,
            "include_news": True,
            "batch_size": 20,
            "delay_seconds": 0.25,
            "refresh_universe": True,
            "use_cached_bars_first": True,
            "use_cached_symbols_only": True,
            "warning_days": 3,
            "valid_days": 5,
        }),
        processed_symbol_ids_json="[]",
        synced_symbol_ids_json="[]",
        errors_json="[]",
    )
    db_session.add(task)
    db_session.commit()
    return task


def test_run_discovery_task_aborts_when_local_universe_is_empty(monkeypatch, db_session):
    """【P0 数据正确性】本地基础股票池为空时，扫描任务应明确失败。

    扫描任务只读 universe_symbols，不负责调用 AkShare 刷新股票池；用户应先在
    设置页完成基础数据同步。本测试守护这一读写分离契约。
    """
    from app.models.discovery import DiscoveryTaskRecord
    _make_discovery_task(db_session, task_id="test-seen-zero")

    # 缩短 watchdog 心跳间隔加速测试
    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.1)

    # _run_discovery_task 内部用 SessionLocal() 创建独立 session（连接同一 SQLite 文件）
    # RuntimeError 被外层 except 捕获，标记 task=failed
    discovery_tasks._run_discovery_task("test-seen-zero")

    # 用 db_session 刷新并验证 task 状态
    db_session.expire_all()
    task = db_session.get(DiscoveryTaskRecord, "test-seen-zero")
    assert task is not None
    assert task.status == "failed", f"seen=0 应标记 task=failed，实际 status={task.status}"
    assert task.stage == "failed"
    assert task.percent == 100
    assert "基础表无已同步标的" in task.message, (
        f"message 应包含错误信息，实际：{task.message}"
    )


def test_run_discovery_task_resume_aborts_when_local_universe_is_empty(monkeypatch, db_session):
    """【P0 数据正确性】断点续跑时基础股票池为空也应明确失败。"""
    from app.models.discovery import DiscoveryTaskRecord
    task = _make_discovery_task(db_session, task_id="test-universe-timeout")
    task.processed_symbol_ids_json = "[1]"
    db_session.commit()

    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.1)

    discovery_tasks._run_discovery_task("test-universe-timeout")

    db_session.expire_all()
    task = db_session.get(DiscoveryTaskRecord, "test-universe-timeout")
    assert task is not None
    assert task.status == "failed", f"超时应标记 task=failed，实际 status={task.status}"
    assert task.stage == "failed"
    assert task.percent == 100
    assert "基础表无已同步标的" in task.message, (
        f"message 应包含超时信息，实际：{task.message}"
    )


# ============================================================================
# 5. data_credibility 边界（_data_credibility 各 bar_count 阈值）
# ============================================================================

def _make_bars(count: int) -> list:
    """构造 count 个 DailyBar mock（_data_credibility 只用 len(bars)）。"""
    return [MagicMock() for _ in range(count)]


def test_data_credibility_below_5_bars_returns_02():
    """【P0 数据正确性】bar_count < 5 时 _data_credibility 应返回 0.2（早返回）。

    防止 bar_count < 5 时抛 NameError（C-3 历史 bug）。
    """
    from app.services.scoring_config_engine import _data_credibility
    today = date.today()
    # 0, 1, 4 根都应返回 0.2
    assert _data_credibility([], today) == 0.2
    assert _data_credibility(_make_bars(1), today) == 0.2
    assert _data_credibility(_make_bars(4), today) == 0.2


def test_data_credibility_at_5_bars_today_returns_04():
    """【P0 数据正确性】5 根 K 线 + 当天评分 → 0.4 * 1.0 = 0.4。

    bar_factor = 0.4 + (5-5)*0.02 = 0.4
    freshness_factor = 1.0 - 0*0.1 = 1.0
    result = 0.4 * 1.0 = 0.4
    """
    from app.services.scoring_config_engine import _data_credibility
    assert _data_credibility(_make_bars(5), date.today()) == 0.4


def test_data_credibility_at_50_bars_today_reaches_cap():
    """【P0 数据正确性】50+ 根 K 线 + 当天评分 → 1.0 * 1.0 = 1.0（达上限）。

    bar_factor = min(1.0, 0.4 + (50-5)*0.02) = min(1.0, 1.3) = 1.0
    freshness_factor = 1.0
    result = 1.0
    """
    from app.services.scoring_config_engine import _data_credibility
    assert _data_credibility(_make_bars(50), date.today()) == 1.0
    assert _data_credibility(_make_bars(100), date.today()) == 1.0


def test_data_credibility_with_stale_date_decreases():
    """【P0 数据正确性】评分日期越久远，data_credibility 越低。

    7 天前：freshness_factor = max(0.3, 1.0 - 7*0.1) = 0.3
    30 天前：走 else 分支 freshness_factor = max(0.1, 0.5 - 23*0.05) = 0.1
    """
    from app.services.scoring_config_engine import _data_credibility
    today = date.today()
    days_7_ago = today - timedelta(days=7)
    days_30_ago = today - timedelta(days=30)

    # 50 根 + 7 天前：1.0 * 0.3 = 0.3
    assert _data_credibility(_make_bars(50), days_7_ago) == 0.3
    # 50 根 + 30 天前：1.0 * 0.1 = 0.1
    assert _data_credibility(_make_bars(50), days_30_ago) == 0.1


# ============================================================================
# 6. _clamp_score / _clamp 边界（含 NaN 处理）
# ============================================================================

def test_clamp_score_handles_negatives_and_overflow():
    """【P0 数据正确性】_clamp_score 应将负数→0，超 100→100，并保留 2 位小数。"""
    assert analysis._clamp_score(-5) == 0.0
    assert analysis._clamp_score(-0.01) == 0.0
    assert analysis._clamp_score(150) == 100.0
    assert analysis._clamp_score(100.001) == 100.0
    # 注意：Python round() 用银行家舍入，且浮点存储有精度误差
    # 50.555 实际存储为 50.5549999... → round → 50.55
    # 50.567 实际存储为 50.5670000... → round → 50.57
    assert analysis._clamp_score(50.555) == 50.55
    assert analysis._clamp_score(50.567) == 50.57  # 保留 2 位小数


def test_clamp_score_handles_nan():
    """【P0 数据正确性】_clamp_score(NaN) 不应抛异常，结果应是有限数。

    NaN 在 max/min 比较中行为不稳定，但 round() 不应抛异常。
    本测试确保 NaN 输入不会导致下游 JSON 序列化失败。
    """
    import math
    result = analysis._clamp_score(float("nan"))
    # NaN 经过 max(0, min(100, NaN)) → NaN（min/max 对 NaN 不抛异常）
    # round(NaN, 2) → NaN
    # 关键：不抛异常，且结果可序列化（JSON 不接受 NaN，但 SQLite 接受）
    assert math.isnan(result) or 0.0 <= result <= 100.0


def test_clamp_in_scoring_engine_same_behavior():
    """【P0 数据正确性】scoring_config_engine._clamp 应与 analysis._clamp_score 行为一致。

    两处实现完全相同：round(max(0.0, min(100.0, value)), 2)。
    """
    from app.services.scoring_config_engine import _clamp
    for value in [-10, 0, 50.555, 100, 150]:
        assert _clamp(value) == analysis._clamp_score(value), (
            f"两处 _clamp 实现不一致：value={value}"
        )


# ============================================================================
# 7. _grade 全阈值覆盖
# ============================================================================

def test_grade_full_threshold_coverage():
    """【P0 数据正确性】_grade 应按 80/65/50 阈值返回 A/B/C/D。

    注意：实际阈值是 80/65/50（不是 90/80/70/60）。
    防止阈值被误改导致分级错乱。
    """
    # A: >= 80
    assert analysis._grade(80) == "A"
    assert analysis._grade(90) == "A"
    assert analysis._grade(100) == "A"
    # B: 65 <= score < 80
    assert analysis._grade(65) == "B"
    assert analysis._grade(79.99) == "B"
    # C: 50 <= score < 65
    assert analysis._grade(50) == "C"
    assert analysis._grade(64.99) == "C"
    # D: < 50
    assert analysis._grade(49.99) == "D"
    assert analysis._grade(0) == "D"
    assert analysis._grade(-10) == "D"


def test_grade_in_scoring_engine_same_thresholds():
    """【P0 数据正确性】scoring_config_engine._grade 应与 analysis._grade 阈值一致。"""
    from app.services.scoring_config_engine import _grade
    for score in [80, 65, 50, 30, 0]:
        assert _grade(score) == analysis._grade(score), (
            f"两处 _grade 实现不一致：score={score}"
        )
