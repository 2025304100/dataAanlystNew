"""G2 缓存抽样校验 · 语义回归哨兵（2026-09-23）。

**实测缺陷**（真实挖掘报告）：20 代里只有 3 代 `cache_validation_passed=1`，日志满是
`G2 采样校验发现 1 处不一致 … reason: 'cache_miss'` + `G2 缓存已废弃：采样校验不一致`。

根因有两条，都在 `verify_sample` 里：

1. **「缓存缺失」被当成「数值不一致」**。子表达式被质量门禁拒绝写入缓存
   （日志：`拒绝写入 cs_zscore(amount)：NaN 比例 100.00% > 上限 50%`）后，
   `assemble` 必然报 `CacheMissError` —— 但「缓存里没有可比对的东西」与
   「缓存结果算错了」是两件事。原实现把前者计入 mismatches，
   于是一旦因子含被拒子表达式就 **废弃整份缓存**；下一代冷启动 →
   再次触发 → **缓存永远无效，每代全量重算**。

2. **NaN ↔ NaN 被当作不一致**。`diff = max(...) if mask.any() else inf` ——
   两个面板全 NaN（如缺失数据/退化截面，`cs_zscore` 的 NaN 是**有意设计**）
   时 mask 为空 → `diff=inf` → 判不一致。全 NaN 对全 NaN 应当是**一致**。

三条哨兵：
1. 缓存缺失（CacheMissError）→ 记 `skipped`，**不计 mismatch、不废弃缓存**
2. 数值差异 → 仍判不一致并废弃（安全语义不得放宽）
3. 全 NaN ↔ 全 NaN → 视为一致
"""
from __future__ import annotations

import numpy as np
import pytest

from app.services.factors.mining import subexpr_cache as SC


def _pool(sample_ok: bool):
    """构造一个最小 pd 面板（列 symbol 级别由调用方无关紧要，只比数值）。"""

    def _compute(_key: str):
        return np.array([1.0, 2.0, 3.0]) if sample_ok else np.array([1.0, 2.0, 4.0])

    return _compute


@pytest.fixture()
def cache():
    return SC.SubexpressionCache(scope=SC.SCOPE_PRESCREEN, data_snapshot_version="s-1")


def test_cache_miss_is_skipped_not_mismatch(cache):
    """哨兵 1：缓存里没有该子式（质量门禁拒写）→ 跳过，不算不一致、不废弃缓存。"""
    # per_factor 指向一个从未写入缓存的子表达式 → assemble 必抛 CacheMissError
    per_factor = {"f1": ["cs_zscore(amount)"]}
    res = SC.verify_sample(
        [{"id": "f1", "formula": "cs_zscore(amount)"}],
        cache=cache, per_factor=per_factor,
        direct_compute=_pool(True),
        sample_size=1,
    )

    assert res["mismatches"] == [], "缓存缺失不得计为数值不一致"
    assert res["discarded"] is False, "缓存缺失不得触发废弃整份缓存"
    assert res["skipped"], "应把缓存缺失单独归类，供探针/日志观察"
    assert res["skipped"][0]["factor"] == "f1"
    assert res["skipped"][0]["reason"] == "cache_miss"


def test_numeric_mismatch_still_fails_and_discards(cache):
    """哨兵 2：真数值差异仍判失败并废弃缓存（安全语义不放宽）。"""
    key = "close"
    cache.put(key, np.array([1.0, 2.0, 3.0]))
    res = SC.verify_sample(
        [{"id": "f1", "formula": key}],
        cache=cache, per_factor={"f1": [key]},
        direct_compute=_pool(False),  # 直算 [1,2,4] ≠ 缓存 [1,2,3]
        sample_size=1,
    )

    assert len(res["mismatches"]) == 1
    assert res["discarded"] is True


def test_all_nan_panels_are_comparable(cache):
    """哨兵 3：两侧 NaN 位置一致 → 视为一致（防御性分支）。

    注：全 NaN 面板**进不了缓存**（质量门禁拒写 NaN 超限的子表达式），
    所以该分支在真实链路上不可达；此处直接验证数值比较逻辑本身，
    确保「没有任何有限值可比」时不会退化成 `diff=inf`（原实现如此）。
    """
    key = "close"
    cache.put(key, np.array([np.nan, np.nan, np.nan]))  # 若拒写则跳过该断言
    if key not in cache.records:
        pytest.skip("该缓存实现对 NaN 面板拒写 —— 分支不可达，无需断言")

    res = SC.verify_sample(
        [{"id": "f1", "formula": key}],
        cache=cache, per_factor={"f1": [key]},
        direct_compute=lambda _k: np.array([np.nan, np.nan, np.nan]),
        sample_size=1,
    )

    assert res["mismatches"] == [], "全 NaN 对全 NaN 不应判为不一致"
    assert res["checked"] == 1
