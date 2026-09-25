"""子表达式缓存 · NaN 阈值口径回归哨兵（2026-09-23）。

**实测缺陷**（真实数据，区间 2025-06-01~2025-12-31，面板 146 交易日 × 5461 股）：

    roe_ttm   → bad = 89.17%   ← 被缓存拒写
    pe_ttm    → bad = 70.00%   ← 被缓存拒写
    volume / amount / turnover_rate → bad ≈ 0.98%  ✓ 正常

**根因**：缓存写入用一个**绝对** `max_nan_ratio=0.5` 判定「值是否可缓存」，
但**财报/估值类字段在面板口径下天然高缺失** —— 季报只在披露日附近有值，
未披露日与新股就是 NaN；估值快照同理。于是**任何引用 roe_ttm / pe_ttm 的因子**
（含模板与随机探索层生成的大量合法因子）都被判为「污染值」拒之门外：

- 缓存命中率被压到极低 → 每代重复全量计算（性能灾难）
- `verify_sample` 无样本可比 → `cache_validation_passed` 长期为 0/None

**口径修正**：拒绝写入只应针对「**完全不可用**」（全 NaN / 空 / 全 Inf），
高缺失 ≠ 不可用 —— 因子有效性由**覆盖率**（IC 按有效截面计算）负责体现，
不该由缓存层用面板非空率一刀切。

三条哨兵：
1. 财报/估值形态的高缺失面板（89% / 70%）在**默认配置**下可入缓存
2. 完全空（全 NaN）仍拒绝 —— 那是真不可用
3. 含 Inf 仍一律拒绝 —— Inf 会污染下游计算
"""
from __future__ import annotations

import numpy as np

from app.services.factors.mining import subexpr_cache as SC


def _cache(**kw):
    return SC.SubexpressionCache(scope=SC.SCOPE_PRESCREEN,
                                 data_snapshot_version="s-1", **kw)


def _panel_with_nan_ratio(ratio: float, size: int = 1000) -> np.ndarray:
    arr = np.full(size, np.nan)
    arr[: max(1, int(round(size * (1.0 - ratio))))] = 1.0
    return arr


def test_default_threshold_allows_high_missing_financial_panels():
    """哨兵 1：roe_ttm(89.17%) / pe_ttm(70%) 形态的高缺失面板必须可入缓存。"""
    c = _cache()
    roe = c.put("roe_ttm", _panel_with_nan_ratio(0.8917))
    assert roe.status == SC.STATUS_VALID, (
        f"财报字段被误拒：{roe.error}（高缺失 ≠ 不可用，实测 roe_ttm 面板缺失 89% 是数据常态）")
    pe = c.put("pe_ttm", _panel_with_nan_ratio(0.70))
    assert pe.status == SC.STATUS_VALID, f"估值字段被误拒：{pe.error}"
    assert c.rejected == 0


def test_fully_nan_still_rejected():
    """哨兵 2：全 NaN（真不可用）仍拒绝。"""
    c = _cache()
    rec = c.put("all_nan", np.full(100, np.nan))
    assert rec.status == SC.STATUS_FAILED
    assert rec.value is None


def test_inf_counted_into_bad_ratio():
    """哨兵 3：Inf 与 NaN 同口径计入 bad 比例 —— 少量 Inf 放行，全 Inf 拒绝。

    （原实现「含 Inf 一律拒绝」：缓存拒写后调用方回退直算，值照样带 Inf 进下游，
    保护是假的；代价是真的 —— 实测 `sqrt(amount)/ts_delta_periods(amount,20)`
    仅 0.0002% Inf 就整式子失去缓存资格。）
    """
    c = _cache()
    few = c.put("few_inf", np.array([1.0, np.nan, np.inf, 2.0]))   # bad = 50%
    assert few.status == SC.STATUS_VALID, f"少量 Inf 被误拒：{few.error}"
    all_inf = c.put("all_inf", np.array([np.inf, -np.inf]))
    assert all_inf.status == SC.STATUS_FAILED
    assert all_inf.value is None


def test_default_threshold_value_is_documented_and_sane():
    """默认阈值必须落在 (0.9, 1.0)：能放行财报类高缺失，又拦得住"几乎全空"。

    原值 0.5 是「按行情字段（缺失<1%）想当然定的」，跨字段类型复用即失效。
    """
    assert 0.9 < SC.DEFAULT_MAX_NAN_RATIO < 1.0, SC.DEFAULT_MAX_NAN_RATIO


def test_explicit_threshold_still_honored():
    """显式传阈值时行为不变（可配置性未被破坏）。"""
    c = _cache(max_nan_ratio=0.5)
    assert c.put("x", _panel_with_nan_ratio(0.7)).status == SC.STATUS_FAILED
    assert c.put("y", _panel_with_nan_ratio(0.3)).status == SC.STATUS_VALID
