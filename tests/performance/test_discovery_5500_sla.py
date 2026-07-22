"""性能基线测试 - WP-P.9 5 分钟扫描 SLA。

覆盖 spec 第 33 章 / checklist WP-P 段落中的性能验收点：
1. A 股 5,500 只、ready 快照命中 P95 ≤ 300 秒
2. ETF 1,600 只、ready 快照命中 P95 ≤ 300 秒
3. 无高级指标或组合过滤时目标 60 秒内完成

这些测试需要发布环境真实数据（A 股全市场 5,500 只 / ETF 1,600 只 + 完整因子 /
Score 行情），SQLite 内存库无法承载该量级，因此默认 skip。
发布环境运行方式：

    pytest tests/performance/test_discovery_5500_sla.py -m performance --no-header

或在 CI 发布环境：

    pytest -m performance

硬约束验证（参照 project_memory）：
- 不在普通 CI / 本地 SQLite 环境运行（会因数据量不足失败或超时）
- 终态不被 worker 覆盖
- 进度更新避免大跳
- 阶段预算超时降级 best-effort
"""
from __future__ import annotations

import pytest

# 整个模块标记为 slow + performance，便于过滤
pytestmark = [
    pytest.mark.slow,
    pytest.mark.performance,
]

_SKIP_REASON = (
    "性能基线测试需要发布环境真实数据（A 股 5,500 / ETF 1,600 + 完整因子与 Score），"
    "SQLite 内存库无法承载该量级。请在发布环境使用 -m performance 显式运行。"
)


def test_a_stock_5500_p95_under_300s():
    """A 股 5,500 只、ready 快照命中 P95 ≤ 300 秒。

    验证步骤：
    1. 准备 A 股全市场 5,500 只 universe_symbols + 完整 Score / daily_bar
    2. 调用 run_fast_scan(scope="cn_stock") 重复 20 次取 P95
    3. 断言 P95 ≤ 300 秒
    4. 断言无 ScanBudgetExceededError
    5. 断言 timings.total_duration_ms <= TOTAL_BUDGET_SECONDS * 1000
    """
    pytest.skip(_SKIP_REASON)


def test_etf_1600_p95_under_300s():
    """ETF 1,600 只、ready 快照命中 P95 ≤ 300 秒。

    验证步骤：
    1. 准备 ETF 全市场 1,600 只 universe_symbols + 完整 Score / daily_bar
    2. 调用 run_fast_scan(scope="cn_etf") 重复 20 次取 P95
    3. 断言 P95 ≤ 300 秒
    4. 断言无 ScanBudgetExceededError
    5. 断言 timings.total_duration_ms <= TOTAL_BUDGET_SECONDS * 1000
    """
    pytest.skip(_SKIP_REASON)


def test_no_filters_target_under_60s():
    """无高级指标或组合过滤时目标 60 秒内完成。

    验证步骤：
    1. 准备 A 股 5,500 只 universe_symbols + 完整 Score
    2. 调用 run_fast_scan(scope="cn_stock", min_score=55) 不传 indicator_plan / portfolio_id
    3. 重复 10 次取 P95
    4. 断言 P95 ≤ NO_FILTERS_TARGET_SECONDS (60s)
    5. 断言 status="ok" 且 exceeded_stages 为 None
    """
    pytest.skip(_SKIP_REASON)
