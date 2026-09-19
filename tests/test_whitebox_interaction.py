"""白盒测试 - 前端交互逻辑守护 (P0 回归)。

验证 AkshareApiManager.tsx 和 Discovery.tsx 的关键交互逻辑：
1. 批量探测并发控制 + 按钮禁用保护
2. 卡死检测 Alert 显示/消失逻辑
3. 批量探测汇总提示

由于项目无 Jest/Vitest 环境，采用源码断言方式验证关键逻辑存在，
防止重构时丢失交互保护（如 CONCURRENCY=3、disabled 条件、2min 阈值等）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.whitebox

FRONTEND_SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"
AKSHARE_MANAGER = FRONTEND_SRC / "components" / "AkshareApiManager.tsx"
DISCOVERY_TSX = FRONTEND_SRC / "components" / "Discovery.tsx"


@pytest.fixture(scope="module")
def akshare_manager_source():
    """读取 AkshareApiManager.tsx 源码。"""
    return AKSHARE_MANAGER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def discovery_source():
    """读取 Discovery.tsx 源码。"""
    return DISCOVERY_TSX.read_text(encoding="utf-8")


# ============================================================================
# 1. 批量探测并发控制 + 按钮禁用保护
# ============================================================================

def test_batch_probe_disables_all_single_buttons(akshare_manager_source):
    """【P0 交互回归】批量探测时应禁用所有单个探测按钮。

    防止用户在批量探测期间点击单个按钮触发重复请求。
    源码应包含 disabled={probingAll || probingKeys.has(r.key)} 条件。
    """
    assert "disabled={probingAll" in akshare_manager_source, \
        "批量探测时应通过 probingAll 禁用单个按钮"
    assert "probingKeys.has(r.key)" in akshare_manager_source, \
        "应通过 probingKeys.has(r.key) 禁用正在探测的按钮"


def test_batch_probe_releases_buttons_after_done(akshare_manager_source):
    """【P0 交互回归】批量探测完成后应释放按钮禁用状态。

    防止批量探测完成后按钮永久禁用，用户无法再次探测。
    源码应在 finally 块中 setProbingKeys(new Set()) 和 setProbingAll(false)。
    """
    assert "setProbingAll(false)" in akshare_manager_source, \
        "finally 块应 setProbingAll(false) 释放批量探测状态"
    assert "setProbingKeys(new Set())" in akshare_manager_source, \
        "finally 块应 setProbingKeys(new Set()) 释放所有按钮禁用"


def test_batch_probe_concurrency_is_3(akshare_manager_source):
    """【P0 交互回归】批量探测并发数应为 3。

    3 个一组并发平衡速度与风控：
    - 太低（1）：17 接口 × 30s = 510s 太慢
    - 太高（17）：可能触发数据源风控
    源码应有 const CONCURRENCY = 3。
    """
    assert "const CONCURRENCY = 3" in akshare_manager_source, \
        "批量探测并发数应为 const CONCURRENCY = 3"


# ============================================================================
# 2. 卡死检测 Alert 逻辑
# ============================================================================

def test_stale_warning_appears_after_2min(discovery_source):
    """【P0 交互回归】任务 running 且 updated_at 超 2 分钟无变化时应显示卡死警告。

    阈值 120000ms（2 分钟），让用户在任务卡死时尽快感知。
    源码应有 120000 阈值和 setStaleWarning(true) 逻辑。
    """
    assert "120000" in discovery_source, \
        "卡死检测阈值应为 120000（2 分钟）"
    assert "setStaleWarning" in discovery_source, \
        "应有 setStaleWarning 状态设置"


def test_stale_warning_disappears_on_cancel(discovery_source):
    """【P0 交互回归】任务非 running 状态时 staleWarning 应重置为 false。

    防止任务取消/完成后 Alert 仍显示。
    源码应在 task.status !== "running" 时 setStaleWarning(false)。
    """
    assert 'task.status !== "running"' in discovery_source, \
        "应检查 task.status !== 'running'"
    assert "setStaleWarning(false)" in discovery_source, \
        "非 running 状态应 setStaleWarning(false) 隐藏 Alert"


# ============================================================================
# 3. 批量探测汇总提示
# ============================================================================

def test_probe_all_shows_summary_message(akshare_manager_source):
    """【P0 交互回归】批量探测完成后应显示汇总提示（OK 数 + 失败数）。

    让用户知道批量探测的结果，而非静默完成。
    源码应有 message.success 含 OK 和 message.warning 含 failed。
    """
    assert "OK (" in akshare_manager_source, \
        "全成功时应有 message.success 含 'OK (N)'"
    assert "failed" in akshare_manager_source, \
        "有失败时应有 message.warning 含 'N OK, M failed'"
    assert "successCount" in akshare_manager_source, \
        "应统计 successCount"
    assert "failCount" in akshare_manager_source, \
        "应统计 failCount"
