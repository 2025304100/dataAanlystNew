"""时序算子族 `ts_` 求值（设计文档 §6.2，需求 §6.0，模块 M2）。

背景：白名单原本没有 `delta` / `atr`，导致需求 §6.0 所述「25 个模板中 10 个不可行」。

⚠️ 为什么拆成 `ts_delta_bars` / `ts_delta_periods` 两个算子
----------------------------------------------------------
行情与财报的采样频率不同（**交易日** vs **报告期**）。若共用一个 `ts_delta`，
AST 只能按字段类型**隐式**解释 `n`，稍有不慎就会把质量类模板算错
（如 `ts_delta_periods(roe_ttm, 4)` 是 4 个**季度**，不是 4 个交易日）。
拆成两个显式算子后语义无歧义，由公式编写者在编写时明确指定。
两者的差异**不在实现**（都是按位置位移 n），而在**传入序列的采样语义** +
`TS_N_UNIT` 元数据 + 前端提示。参见 `TS_N_UNIT`。

⚠️ 索引约定（与 `cross_section.py` 保持一致）
    index = MultiIndex(trade_date, symbol)，仅含 **trade_date** 一个时间层级。
    时序算子按 **symbol（level=1）** 分组，沿 trade_date 方向位移/滚动。
    → 截面算子按 trade_date（level=0）分组，时序算子按 symbol（level=1）分组。
      这是两类算子最本质的区别。

⚠️ 关于 `ts_atr` 与 `prev_close`（SD-v2.0 §12.3 D-D）
    `raw_daily_bars` **没有** `prev_close` 列，配方内也没有物理列可用，
    故 `ts_atr` 内部用 `ref(close, 1)`（即 `close.shift(1)`）自行派生。

    **本实现采用 close-to-close 真实波幅口径**：
        TR_t = |close_t - close_{t-1}|
        ATR  = 最近 n 期 TR 的均值

    ⚠️ 这不是含 high/low 的经典 ATR。原因是**签名只携带一条序列**：
    `factor_executor` 的数据读取是**按依赖字段裁剪的 SELECT**
    （`_read_source_data` → `table_fields` → `_select_field_expressions`，
    `factor_executor.py:680/746`）。若 `ts_atr(close, n)` 偷偷去取 `high`/`low`，
    这些列不会被 SELECT 出来 → `context` 里缺失 → `_eval_node` 返回 None →
    `execute()` 落成 `pd.Series(nan)`，即**静默产出全 NaN 因子**。

    这也是 `prev_close` 那类问题的同一形态。因此本实现坚持**只依赖声明过的参数**，
    把「ATR 需要 high/low」作为**待确认的需求口径**上报，而不是靠隐式取列绕过。
    需求 §6.0 与向导 §6.3.3 的模板 `ts_atr(close,{n1})/close`（ATR 比率、波动率水平）
    在 close-to-close 口径下依然成立。
"""
from __future__ import annotations

import pandas as pd

#: 本模块提供的算子名（供 compilers / sandbox 同步注册）
TIME_SERIES_OPS: tuple[str, ...] = (
    "ts_delta_bars",
    "ts_delta_periods",
    "ts_atr",
)

#: ⚠️ `n` 的单位 —— 这是两个 delta 算子**唯一的语义差异**，做成机器可读，
#: 供测试断言、前端参数提示、以及公式审查使用。
TS_N_UNIT: dict[str, str] = {
    "ts_delta_bars": "trading_day",
    "ts_delta_periods": "report_period",
    "ts_atr": "trading_day",
}

#: 每期代表多少日历天（仅供 UI 展示「回看多久」，不参与计算）
DAYS_PER_UNIT: dict[str, float] = {
    "trading_day": 1.0,
    "report_period": 91.0,
}

#: 竖表索引中 symbol 所在的层级（trade_date 在 0）
SYMBOL_LEVEL = 1


def _validate_n(n: int) -> int:
    """`n` 必须是**整数期数且 ≥ 1**。

    刻意拒绝非整数（如 `1.9`）而不是静默截断成 `1` —— `n` 是「期数」，
    截断会让 `ts_delta_periods(roe_ttm, 1.9)` 悄悄变成「1 个报告期」，
    与编写者意图不符且极难发现。
    同时接受 numpy 整数（`isinstance(np.int64(2), int)` 为 False，故按数值判定）。
    """
    if isinstance(n, bool):
        raise ValueError(f"n must be an integer, got {n!r}")
    try:
        as_float = float(n)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"n must be an integer, got {n!r}") from exc
    if not as_float.is_integer():
        raise ValueError(f"n must be a whole number of periods, got {n!r}")
    value = int(as_float)
    if value < 1:
        raise ValueError(f"n must be >= 1, got {value}")
    return value


def _shift_within_symbol(values: pd.DataFrame, n: int) -> pd.DataFrame:
    """按标的（level=1）沿时间轴位移 n 期，**不跨标的串值**。"""
    return values.groupby(level=SYMBOL_LEVEL, sort=False).shift(int(n))


# ══════════════════════════════════════════════════════════
# 单算子实现（纯 pandas，可用小样本单测）
# ══════════════════════════════════════════════════════════


def ts_delta_bars(values: pd.DataFrame, n: int) -> pd.DataFrame:
    """**行情**字段 N 个交易日差分：`x[t] - x[t-N]`。

    用于 `ts_delta_bars(close, 20)` = 过去 20 个交易日的价格变化。

    ⚠️ 是**绝对差分**（价格差），不是百分比变化。百分比变化请用 `pct_change`。
    """
    k = _validate_n(n)
    return values - _shift_within_symbol(values, k)


def ts_delta_periods(values: pd.DataFrame, n: int) -> pd.DataFrame:
    """**财报**字段 N 个报告期差分：`x[period] - x[period-N]`。

    用于 `ts_delta_periods(roe_ttm, 4)` = 同比（4 个季度前）ROE 变化。

    ⚠️ 实现与 `ts_delta_bars` **逐字相同**（都按位置位移 n），差异只在**口径与调用方**：
        - 本函数要求传入的序列是**按报告期排列**的财报序列，不是日线序列
        - `n` 的单位是**报告期**（见 `TS_N_UNIT`）
        - 因子流水线侧由字段的 `source_table`（`raw_financial_reports`）保证；
          单标的沙箱侧无法区分，只能按位置差分（见 `indicator_ast_sandbox` docstring）

    拆成两个算子的目的正是让「单位」在公式里**显式可见**，而不是靠 AST 猜字段类型。
    """
    k = _validate_n(n)
    return values - _shift_within_symbol(values, k)


def ts_atr(close: pd.DataFrame, n: int) -> pd.DataFrame:
    """平均真实波幅（close-to-close 口径）。

        TR_t = |close_t - close_{t-1}|      ← prev_close 用 shift(1) 派生
        ATR  = 最近 n 期 TR 的简单均值

    `min_periods=n`：前 n 期不足时返回 NaN，**不填零、不用部分窗口**
    （与需求 §3.5「不得自动剔除/降级/填零」的取向一致）。

    ⚠️ 不是含 high/low 的经典 ATR，原因见模块 docstring。
    """
    k = _validate_n(n)
    prev_close = _shift_within_symbol(close, 1)
    true_range = (close - prev_close).abs()
    return true_range.groupby(level=SYMBOL_LEVEL, sort=False).transform(
        lambda series: series.rolling(k, min_periods=k).mean()
    )


# ══════════════════════════════════════════════════════════
# 统一入口（factor_executor 的时序分支调用它）
# ══════════════════════════════════════════════════════════


def eval_time_series(
    op: str,
    values: pd.DataFrame,
    *,
    n: int | None = None,
) -> pd.DataFrame:
    """按算子名分发。`values` 必须 index = (trade_date, symbol)。

    Raises:
        ValueError: 未知算子或参数缺失。
    """
    if op not in TIME_SERIES_OPS:
        raise ValueError(f"unknown time-series operator: {op!r}")
    if n is None:
        raise ValueError(f"{op} requires n")

    if op == "ts_delta_bars":
        return ts_delta_bars(values, n)
    if op == "ts_delta_periods":
        return ts_delta_periods(values, n)
    return ts_atr(values, n)


__all__ = [
    "TIME_SERIES_OPS",
    "TS_N_UNIT",
    "DAYS_PER_UNIT",
    "SYMBOL_LEVEL",
    "ts_delta_bars",
    "ts_delta_periods",
    "ts_atr",
    "eval_time_series",
]
