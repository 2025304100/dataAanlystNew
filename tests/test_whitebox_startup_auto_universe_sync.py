"""白盒 - 启动自动同步开关 AUTO_UNIVERSE_SYNC_ON_STARTUP 的语义。

为什么要有这条：应用 lifespan 一看 universe_symbols 为空就自动发起「全市场基础数据
初始化同步」，实测（调用栈取证）它会从 universe_sync_task._run_universe_sync_init
里真的去访问 akshare。生产要这个行为（新机器开箱可用），但测试/CI 每次都是空库，
于是每轮都变成"和外网赛跑"：无关用例的等待被拖爆（integration 的 15s 任务终态超时），
前端还会弹出同步进度模态遮挡导航点击（e2e 间歇红）。

这里把两端都钉住，防止有人日后"顺手"把默认改成关闭：
- 默认必须是开启（生产行为不变）；
- 关闭时**一个同步任务都不许发起**（这才是测试隔离生效的证明）；
- 开启且库为空时确实发起（不是把功能悄悄删掉）。
"""
from __future__ import annotations

import pytest
from unittest import mock

from app.core.config import Settings
from app.main import _auto_start_universe_init

pytestmark = pytest.mark.whitebox


def test_default_is_enabled_so_production_unchanged(monkeypatch):
    """默认开启：新机器启动仍能自动开箱同步。"""
    monkeypatch.delenv("AUTO_UNIVERSE_SYNC_ON_STARTUP", raising=False)
    assert Settings().AUTO_UNIVERSE_SYNC_ON_STARTUP is True


def test_env_var_can_disable(monkeypatch):
    monkeypatch.setenv("AUTO_UNIVERSE_SYNC_ON_STARTUP", "0")
    assert Settings().AUTO_UNIVERSE_SYNC_ON_STARTUP is False


def test_disabled_skips_sync_entirely(monkeypatch):
    """关闭时不得发起同步任务，也不该去碰数据库 —— 这是测试/CI 隔离的真正判据。"""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "AUTO_UNIVERSE_SYNC_ON_STARTUP", False, raising=False)

    with mock.patch("app.services.universe_sync_task.start_universe_sync_init") as starter, \
         mock.patch("app.services.universe_sync.get_universe_stats") as stats, \
         mock.patch.object(main_mod, "_get_session_local") as session_factory:
        _auto_start_universe_init()
        starter.assert_not_called()
        # 关闭时连“探测表空不空”都不该做：整条路径直接跳过
        stats.assert_not_called()
        session_factory.assert_not_called()


def test_enabled_still_auto_starts_when_universe_empty(monkeypatch):
    """开启且 universe 为空 → 仍然自动发起（证明我们只是加了开关，没删功能）。

    注意要同时桩掉会话工厂：函数会先 SessionLocal() 再查 stats，不桩就会先撞
    “DatabaseManager 未初始化”异常，被外层 except 吞掉 → 断言看起来对了、
    其实测的是报错分支（上一版就踩过这个：另一条用例因此是假绿）。
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "AUTO_UNIVERSE_SYNC_ON_STARTUP", True, raising=False)

    with mock.patch("app.services.universe_sync_task.start_universe_sync_init") as starter, \
         mock.patch("app.services.universe_sync.get_universe_stats") as stats, \
         mock.patch.object(main_mod, "_get_session_local", return_value=mock.MagicMock()):
        stats.return_value = {"is_empty": True, "total_symbols": 0, "synced_symbols": 0}
        _auto_start_universe_init()
        assert starter.call_count == 1, (
            f"开启时应当且只发起一次自动同步，实际 call_count={starter.call_count}"
        )


def test_enabled_but_data_present_does_not_start(monkeypatch):
    """开启但库里已有数据 → 不该重复同步（原语义保持）。"""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "AUTO_UNIVERSE_SYNC_ON_STARTUP", True, raising=False)

    with mock.patch("app.services.universe_sync_task.start_universe_sync_init") as starter, \
         mock.patch("app.services.universe_sync.get_universe_stats") as stats, \
         mock.patch.object(main_mod, "_get_session_local", return_value=mock.MagicMock()):
        stats.return_value = {"is_empty": False, "total_symbols": 5000, "synced_symbols": 4800}
        _auto_start_universe_init()
        starter.assert_not_called()
        # 真走到了“库不空所以跳过”分支，而不是靠报错走到的（否则本条是假绿）
        stats.assert_called_once()
