"""白盒测试 - 基准指数线性偏离度的窗口语义 (VIZ-0930-21)。

`线性偏离度` 在数据中心被明确宣传为「检测基准曲线是否为假直线」，用来判断回测基准
曲线能不能用。它此前零测试覆盖，于是一个静默错误一直留着：`_linearity_dev` 把
`limit=1000` 直接传给 `list_index_prices`，而该服务是 **trade_date 升序 + limit 截头**，
拿到的是"最旧的 1000 根"。实测 000300 因此用 2016-10-10..2020-11-13 算出 28.58%，
而同一行显示的覆盖区间一直到最新交易日 —— 最近几年的劣化/全值填充根本不会被检出。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.api.routes import universe
from app.services import index_data

pytestmark = pytest.mark.whitebox


class _Bar:
    def __init__(self, close: float) -> None:
        self.close = close


def _series(closes: list[float]) -> list[_Bar]:
    return [_Bar(c) for c in closes]


def test_linearity_dev_uses_most_recent_window(monkeypatch):
    """旧段是一条完美直线、新段大幅波动 → 必须报出波动，而不是 0。

    若实现仍取最旧 1000 根，窗口里全是直线，偏离度会是 0.0（判为"疑似假直线"），
    正好把健康数据误报成坏数据、同时把坏掉的近期段判成健康。
    """
    oldest_flat = [100.0 + i * 0.0 for i in range(1000)]        # 完全平的
    newest_wiggly = [100.0 * (1.6 if i % 2 == 0 else 0.4) for i in range(200)]
    bars = _series(oldest_flat + newest_wiggly)

    seen: dict[str, object] = {}

    def fake_list(db, symbol, *, start_date=None, end_date=None, limit=2000):
        seen["limit"] = limit
        return bars[: len(bars)] if not limit else bars[:limit]

    monkeypatch.setattr(index_data, "list_index_prices", fake_list)

    dev = universe._linearity_dev(db=object(), symbol="000300")

    assert seen["limit"] == 0, "必须请求完整序列，由实现自己截取最近窗口"
    assert dev is not None and dev > 20.0, f"应检出近期大幅偏离，实际 {dev}"


def test_linearity_dev_window_size_is_capped(monkeypatch):
    """窗口上限 = _LINEARITY_WINDOW：再多历史也只按最近这么多根算。"""
    n = universe._LINEARITY_WINDOW
    # 最近 n 根刻意做成直线，更早的历史剧烈波动
    older_noisy = [100.0 + (i % 7) * 30.0 for i in range(600)]
    recent_flat = [500.0] * n
    bars = _series(older_noisy + recent_flat)

    def fake_list(db, symbol, *, start_date=None, end_date=None, limit=2000):
        return bars

    monkeypatch.setattr(index_data, "list_index_prices", fake_list)

    assert universe._linearity_dev(db=object(), symbol="000300") == pytest.approx(0.0, abs=1e-6)


def test_linearity_dev_returns_none_when_insufficient_bars(monkeypatch):
    """不足 5 根（或 close 全空）返回 None，前端渲染成 — 而不是 0% 假健康。"""
    monkeypatch.setattr(
        index_data, "list_index_prices",
        lambda db, symbol, **kw: _series([1.0, 2.0, 3.0]),
    )
    assert universe._linearity_dev(db=object(), symbol="000001") is None


def test_linearity_dev_returns_none_when_close_is_null(monkeypatch):
    bars = [_Bar(None) for _ in range(20)]
    monkeypatch.setattr(index_data, "list_index_prices", lambda db, symbol, **kw: bars)
    assert universe._linearity_dev(db=object(), symbol="000001") is None
