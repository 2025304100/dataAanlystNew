"""T04 · 时序算子族 `ts_delta_bars` / `ts_delta_periods` / `ts_atr`

阶段 PH1 ｜ 门禁 G1 ｜ 模块 M2（设计文档 §6.2，需求 §6.0，开发文档 §3.14）

**本文件守五件事：**

1. **注册四处联动** —— 与 T03 同理，缺任何一处都会炸，且炸法不同（详见 T03 的同名测试）：
   `FUNCTION_CATALOG` / `FUNCTION_DIRECTION` / `formula_catalog._FUNCTION_META`（硬索引，漏了
   `KeyError` → 公式目录 API 500）/ `indicator_ast_sandbox.ALLOWED_FUNCS`。

2. **`n` 的单位差异**（任务卡明确要求各自断言）——
   `ts_delta_bars` 的 `n` 是**交易日**，`ts_delta_periods` 的 `n` 是**报告期**。
   两者实现逐字相同，差异只在口径；用 `TS_N_UNIT` 元数据 + 日历跨度倍数（91×）钉死。

3. **`ts_atr` 是 close-to-close 口径，不需要 high/low** ——
   执行器按依赖字段裁剪 SELECT（`factor_executor.py:680/746`），若 `ts_atr(close, n)`
   偷偷取 `high`/`low`，列不会被查出来 → context 缺失 → **静默产出全 NaN 因子**。
   这正是 `prev_close`（D-D）的同一失败形态，本文件用「只传单列也能算出正确值」来守。

4. **沙箱里 `ts_` 必须真的是可用的实现**（与 `cs_` 相反）——
   `cs_` 需要面板上下文，故登记但拒绝（D-A）；`ts_` 在**序列**上下文中有确切语义，
   必须给出真值。另断言沙箱实现与面板实现**数值一致**，防止两套实现漂移。

5. **`content_hash` 不变**（去重键回归）——
   只往目录追加算子，不使用新算子的公式 hash 必须逐字节不变。

跑法（DoD）：

    .venv/Scripts/python.exe -m pytest \
        tests/services/factors/mining/test_dsl_time_series.py -q
"""
from __future__ import annotations

import ast
import json
import pathlib

import pandas as pd
import pytest

from app.services.factors.dsl.time_series import (
    DAYS_PER_UNIT,
    SYMBOL_LEVEL,
    TIME_SERIES_OPS,
    TS_N_UNIT,
    eval_time_series,
    ts_atr,
    ts_delta_bars,
    ts_delta_periods,
)
from app.services.factors.factor_compiler import (
    DIRECTION_CROSS_SECTION,
    DIRECTION_TIMESERIES,
    FUNCTION_CATALOG,
    FUNCTION_DIRECTION,
    collect_directional_calls,
    compile_formula,
    get_function_direction,
)
from app.services.factors.formula_catalog import (
    _FUNCTION_META,
    build_formula_catalog,
)
from app.services.indicator_ast_sandbox import (
    ALLOWED_FUNCS,
    CROSS_SECTION_ONLY_FUNCS,
    TIME_SERIES_FUNCS,
    IndicatorFormulaEvaluator,
)

pytestmark = pytest.mark.whitebox

TS_OPS = sorted(TIME_SERIES_OPS)

#: 每个算子的一个合法调用
LEGAL_CALLS = {
    "ts_delta_bars": "ts_delta_bars(close, 20)",
    "ts_delta_periods": "ts_delta_periods(roe_ttm, 4)",
    "ts_atr": "ts_atr(close, 14)",
}

#: 向导 §6.3.3 的 25 个模板里用到 ts_ 算子的真实式子（占位参数已替换为具体值）
TEMPLATE_FORMULAS = [
    "ts_delta_bars(close, 20)",
    "-ts_delta_bars(close, 5)",
    "-ts_delta_bars(close, 5) * cs_rank(volume)",
    "cs_rank(volume) - cs_rank(ts_delta_bars(close, 5))",
    "ts_delta_periods(roe_ttm, 4)",
    "ts_delta_periods(roe_ttm, 1) - ts_delta_periods(roe_ttm, 4)",
    "ts_atr(close, 14) / close",
]

#: T03 已固化的 golden hash（**不含 cs_ / ts_**）→ 本次必须逐条不变
_T03_GOLDEN = (
    pathlib.Path(__file__).resolve().parents[4]
    / ".workbuddy" / "mining" / "evidence" / "T03_content_hash_golden.json"
)


def _golden_hashes() -> dict[str, str]:
    """读取 T03 归档的 golden hash；文件缺失时退化为空（不静默跳过断言）。"""
    if not _T03_GOLDEN.exists():  # pragma: no cover - 归档丢失时给出明确信号
        return {}
    data = json.loads(_T03_GOLDEN.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if "cs_" not in k and "ts_" not in k}


# ══════════════════════════════════════════════════════════
# 样本构造
# ══════════════════════════════════════════════════════════

DAILY_DATES = list(pd.date_range("2026-01-05", periods=6, freq="D"))
QUARTER_DATES = list(pd.date_range("2024-03-31", periods=5, freq="91D"))


def _panel(values_by_symbol: dict[str, list[float]], dates) -> pd.DataFrame:
    """构造 index = (trade_date, symbol) 的竖表。"""
    index = pd.MultiIndex.from_product(
        [list(dates), list(values_by_symbol)], names=["trade_date", "symbol"]
    )
    flat: list[float] = []
    for date in dates:
        for symbol in values_by_symbol:
            flat.append(values_by_symbol[symbol][dates.index(date)])
    return pd.DataFrame({"x": flat}, index=index)


#: A 与 B 的量级刻意差 10 倍：若按标的分组出错（串值），结果会立刻暴露
TWO_SYMBOL = _panel(
    {"A": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], "B": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]},
    DAILY_DATES,
)
ONE_SYMBOL = _panel({"A": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}, DAILY_DATES)
ATR_SEQUENCE = [10.0, 11.0, 13.0, 12.0, 15.0]
ATR_PANEL = _panel({"A": ATR_SEQUENCE}, DAILY_DATES[: len(ATR_SEQUENCE)])


# ══════════════════════════════════════════════════════════
# A. 注册四处联动
# ══════════════════════════════════════════════════════════


def test_time_series_ops_frozen():
    assert TS_OPS == ["ts_atr", "ts_delta_bars", "ts_delta_periods"]


@pytest.mark.parametrize("op", TS_OPS)
def test_registered_in_function_catalog(op):
    """① 编译期白名单：category 必须是 `timeseries`（执行器按 category 分派）。"""
    spec = FUNCTION_CATALOG.get(op)
    assert spec is not None, f"{op} 未注册进 FUNCTION_CATALOG"
    assert spec.category == "timeseries", (
        f"{op}.category={spec.category!r}，必须与 DIRECTION_TIMESERIES 拼写一致（"
        "写成 time_series 会让按 category 分派的下游静默漏掉分支）"
    )
    assert spec.min_args == 2 and spec.max_args == 2
    assert spec.window_arg_index == 1, "第 2 个参数（期数）必须走常量正整数校验"


@pytest.mark.parametrize("op", TS_OPS)
def test_registered_in_function_direction(op):
    """② 计算方向：沿时间轴、对单个标的求值。"""
    assert FUNCTION_DIRECTION.get(op) == DIRECTION_TIMESERIES
    assert get_function_direction(op) == DIRECTION_TIMESERIES


@pytest.mark.parametrize("op", TS_OPS)
def test_registered_in_function_meta(op):
    """③ 公式目录元数据（硬索引，漏了 KeyError → 公式目录 API 500）。"""
    meta = _FUNCTION_META.get(op)
    assert meta is not None, f"{op} 缺 _FUNCTION_META 条目"
    for key in ("label_zh", "label_en", "signature", "snippet", "params"):
        assert meta.get(key), f"{op}._FUNCTION_META.{key} 缺失"


@pytest.mark.parametrize("op", TS_OPS)
def test_registered_in_sandbox_whitelist(op):
    """④ 沙箱白名单。"""
    assert op in ALLOWED_FUNCS
    assert op in TIME_SERIES_FUNCS


@pytest.mark.parametrize("op", TS_OPS)
def test_sandbox_entry_is_a_real_callable_not_the_cross_section_reject(op):
    """★ 与 `cs_` 的关键区别：`ts_` 必须是**真实现**，不能复用 cs_ 的拒绝占位。

    若复用，高级筛选里所有 delta/atr 公式会直接报错 ——
    而这两个算子正是需求 §6.0 说「25 个模板中 10 个不可行」的根因。
    """
    reject_placeholder = ALLOWED_FUNCS["cs_rank"]
    assert ALLOWED_FUNCS[op] is not reject_placeholder, f"{op} 复用了 cs_ 的拒绝占位"


def test_function_meta_covers_every_catalog_function():
    """通用哨兵：`_FUNCTION_META` 必须覆盖 `FUNCTION_CATALOG` 的每一个键。"""
    missing = sorted(set(FUNCTION_CATALOG) - set(_FUNCTION_META))
    assert not missing, f"缺 _FUNCTION_META 的键: {missing}"


def test_function_direction_keys_are_all_real_functions():
    dangling = sorted(set(FUNCTION_DIRECTION) - set(FUNCTION_CATALOG))
    assert not dangling, f"FUNCTION_DIRECTION 指向未注册的函数: {dangling}"


def test_build_formula_catalog_exposes_time_series_ops():
    catalog = build_formula_catalog(None)
    by_key = {f["key"]: f for f in catalog["functions"]}
    for op in TS_OPS:
        assert op in by_key, f"{op} 未出现在公式目录中"
        assert by_key[op]["category"] == "timeseries"
        assert by_key[op]["enabled"] is True


def test_cross_section_ops_still_registered():
    """回归：T04 不得改动 cs_ 族的三处注册。"""
    for op in CROSS_SECTION_ONLY_FUNCS:
        assert op in FUNCTION_CATALOG
        assert op in _FUNCTION_META
        assert op in ALLOWED_FUNCS


# ══════════════════════════════════════════════════════════
# B. 编译
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("op", TS_OPS)
def test_every_time_series_op_compiles(op):
    result = compile_formula(formula=LEGAL_CALLS[op])
    assert result.success, f"{LEGAL_CALLS[op]} -> {[e.error_code for e in result.errors]}"


@pytest.mark.parametrize("formula", TEMPLATE_FORMULAS)
def test_template_formulas_compile(formula):
    """向导 §6.3.3 的真实模板式子必须可编译（这是「10 个模板不可行」的修复验证）。"""
    result = compile_formula(formula=formula)
    assert result.success, f"{formula} -> {[e.error_code for e in result.errors]}"


def test_nested_cross_section_and_time_series_compiles():
    """混合轴嵌套：内层时序、外层截面。"""
    result = compile_formula(formula="cs_rank(ts_delta_bars(close, 5))")
    assert result.success, [e.error_code for e in result.errors]
    deps = result.execution_plan.data_dependencies
    assert deps["functions"] == ["cs_rank", "ts_delta_bars"]


@pytest.mark.parametrize(
    "formula,expected_code",
    [
        ("ts_delta_bars(close)", "function_arg_count"),
        ("ts_atr(close, 14, 2)", "function_arg_count"),
        ("ts_delta_bars(close, 0)", "negative_lag"),
        ("ts_delta_bars(close, volume)", "function_window_invalid"),
        ("ts_delta_bars(not_a_field, 5)", "field_not_in_catalog"),
    ],
)
def test_argument_validation(formula, expected_code):
    result = compile_formula(formula=formula)
    assert not result.success
    assert expected_code in [e.error_code for e in result.errors], (
        f"{formula} 应报 {expected_code}，实得 {[e.error_code for e in result.errors]}"
    )


def test_n_exceeding_lookback_limit_is_rejected():
    codes = [e.error_code for e in compile_formula(formula="ts_delta_bars(close, 300)").errors]
    assert "lookback_window_exceeded" in codes


# ══════════════════════════════════════════════════════════
# C. `n` 的单位差异（任务卡明确要求）
# ══════════════════════════════════════════════════════════


def test_ts_n_unit_metadata_frozen():
    """单位表是「两个 delta 算子唯一差别」的机器可读表达。"""
    assert TS_N_UNIT == {
        "ts_delta_bars": "trading_day",
        "ts_delta_periods": "report_period",
        "ts_atr": "trading_day",
    }
    assert set(TS_N_UNIT) == set(TIME_SERIES_OPS)


def test_days_per_unit_frozen():
    assert DAYS_PER_UNIT["trading_day"] == 1.0
    assert DAYS_PER_UNIT["report_period"] == 91.0


@pytest.mark.parametrize(
    "op,expected_hint",
    [
        ("ts_delta_bars", "trading_days"),
        ("ts_delta_periods", "report_periods"),
        ("ts_atr", "trading_days"),
    ],
)
def test_catalog_signature_states_the_unit(op, expected_hint):
    """★ 单位必须在**签名里对用户可见**，否则用户无法判断该传哪种 n。"""
    signature = _FUNCTION_META[op]["signature"]
    assert expected_hint in signature, (
        f"{op} 的 signature={signature!r} 未写明 n 的单位（应为 {expected_hint}）"
    )


def test_same_n_means_different_calendar_spans():
    """★ 单位差异的量化证明：同一个 n=4，日频跨 4 天、季频跨 364 天（差 91 倍）。

    若把两者合并成一个 `ts_delta`，`n=4` 到底是 4 天还是 4 个季度就只能靠 AST 猜，
    质量类模板必然算错 —— 这就是需求 §6.0「逻辑缺口 2」要拆算子的原因。
    """
    bars_input = _panel({"A": [1.0, 2.0, 3.0, 4.0, 5.0]}, DAILY_DATES[:5])
    periods_input = _panel({"A": [0.10, 0.11, 0.12, 0.13, 0.14]}, QUARTER_DATES)

    n = 4
    span_bars = (
        bars_input.index.get_level_values(0)[-1] - bars_input.index.get_level_values(0)[0]
    ).days
    span_periods = (
        periods_input.index.get_level_values(0)[-1] - periods_input.index.get_level_values(0)[0]
    ).days

    assert span_bars == n * DAYS_PER_UNIT[TS_N_UNIT["ts_delta_bars"]]
    assert span_periods == n * DAYS_PER_UNIT[TS_N_UNIT["ts_delta_periods"]]
    assert span_periods / span_bars == 91.0


def test_delta_bars_and_periods_agree_numerically_on_the_same_input():
    """两者实现逐字相同 —— 差异**只在口径**，不在算法。

    这条断言是刻意的：它把「不能靠实现差异来区分两者」写进契约，
    防止后人「顺手给 periods 加个交易日→报告期的换算」而引入隐式猜测。
    """
    assert ts_delta_bars(TWO_SYMBOL, 2).equals(ts_delta_periods(TWO_SYMBOL, 2))


# ══════════════════════════════════════════════════════════
# D. 数值正确性
# ══════════════════════════════════════════════════════════


def test_delta_bars_matches_manual_shift():
    result = ts_delta_bars(ONE_SYMBOL, 2)["x"]
    # x = [1,2,3,4,5,6] → x - shift(2) = [nan,nan,2,2,2,2]
    assert result.iloc[:2].isna().all()
    assert [float(v) for v in result.iloc[2:]] == [2.0, 2.0, 2.0, 2.0]


def test_delta_bars_does_not_cross_symbols():
    """★ 按 symbol（level=1）分组：A 与 B 的量级差 10 倍，串值会立刻暴露。"""
    result = ts_delta_bars(TWO_SYMBOL, 2)["x"]
    a_values = [float(v) for v in result.loc[(slice(None), "A")].dropna()]
    b_values = [float(v) for v in result.loc[(slice(None), "B")].dropna()]
    assert a_values == [2.0, 2.0, 2.0, 2.0]
    assert b_values == [20.0, 20.0, 20.0, 20.0]


def test_symbol_level_constant_matches_multiindex_layout():
    assert SYMBOL_LEVEL == 1
    assert TWO_SYMBOL.index.names[SYMBOL_LEVEL] == "symbol"


def test_atr_matches_manual_close_to_close():
    """ATR = 最近 n 期 |x[i] - x[i-1]| 的均值。"""
    result = ts_atr(ATR_PANEL, 2)["x"]
    # 序列 [10,11,13,12,15] → |diff| = [nan,1,2,1,3] → rolling(2) 均值 = last: (1+3)/2 = 2
    assert float(result.iloc[-1]) == pytest.approx(2.0)
    assert float(result.iloc[-2]) == pytest.approx((2 + 1) / 2)
    # 前 2 期不足窗口 → NaN（min_periods=n，不填零、不用部分窗口）
    assert result.iloc[:2].isna().all()


def test_atr_leading_window_is_nan_not_zero():
    """前 n 期必须 NaN，**不得填 0**（需求 §3.5：不得自动剔除/降级/填零）。"""
    result = ts_atr(ONE_SYMBOL, 3)["x"]
    assert result.iloc[:3].isna().all()
    assert not (result.iloc[:3] == 0.0).any()


def test_atr_insufficient_data_returns_all_nan():
    result = ts_atr(_panel({"A": [1.0, 2.0]}, DAILY_DATES[:2]), 5)["x"]
    assert result.isna().all()


def test_atr_only_depends_on_the_passed_series():
    """★ `ts_atr` 必须**只依赖传入的那一条序列**（close-to-close 口径）。

    背景：执行器按依赖字段裁剪 SELECT（`factor_executor.py:680/746`）。
    如果实现偷偷去取 `high`/`low`，那些列根本不会被查出来 → context 缺失 →
    `_eval_node` 返回 None → `execute()` 落成全 NaN。这正是 `prev_close`（D-D）的失败形态。

    因此：一个**只有一列**的 DataFrame 必须能算出正确值。
    """
    spec = FUNCTION_CATALOG["ts_atr"]
    assert spec.min_args == 2 and spec.max_args == 2
    single_column = _panel({"A": ATR_SEQUENCE}, DAILY_DATES[: len(ATR_SEQUENCE)])
    assert list(single_column.columns) == ["x"]
    assert float(ts_atr(single_column, 2)["x"].iloc[-1]) == pytest.approx(2.0)


@pytest.mark.parametrize("op", TS_OPS)
def test_ops_preserve_shape(op):
    result = eval_time_series(op, TWO_SYMBOL, n=2)
    assert isinstance(result, pd.DataFrame)
    assert result.shape == TWO_SYMBOL.shape
    assert list(result.columns) == list(TWO_SYMBOL.columns)


@pytest.mark.parametrize(
    "bad_n",
    [0, -1, -5, 1.9, 2.5, "x", None, True],
)
def test_validate_n_rejects_non_positive_or_non_integer(bad_n):
    """`n` 必须整数且 ≥1；**拒绝非整数而不是静默截断**（1.9 截成 1 极难发现）。"""
    with pytest.raises(ValueError):
        ts_delta_bars(TWO_SYMBOL, bad_n)


def test_validate_n_accepts_numpy_integer():
    """numpy 整数应被接受（`isinstance(np.int64(2), int)` 为 False，故按数值判定）。"""
    import numpy as np

    assert ts_delta_bars(ONE_SYMBOL, np.int64(2)).equals(ts_delta_bars(ONE_SYMBOL, 2))
    assert ts_delta_bars(ONE_SYMBOL, 2.0).equals(ts_delta_bars(ONE_SYMBOL, 2))


@pytest.mark.parametrize("op", TS_OPS)
def test_eval_time_series_dispatch(op):
    result = eval_time_series(op, ONE_SYMBOL, n=2)
    assert result.shape == ONE_SYMBOL.shape


@pytest.mark.parametrize("op", TS_OPS)
def test_eval_time_series_requires_n(op):
    with pytest.raises(ValueError):
        eval_time_series(op, ONE_SYMBOL)


def test_eval_time_series_rejects_unknown_operator():
    with pytest.raises(ValueError):
        eval_time_series("ts_not_a_real_op", ONE_SYMBOL, n=2)


# ══════════════════════════════════════════════════════════
# E. 沙箱：`ts_` 真实可用（与 `cs_` 的「登记但拒绝」相反）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("op", TS_OPS)
def test_sandbox_accepts_time_series_at_construction(op):
    """构造期**不得**报错（`cs_` 会报，`ts_` 不会）。"""
    IndicatorFormulaEvaluator(LEGAL_CALLS[op])


def test_sandbox_rejects_cross_section_ops_still():
    """★ 回归（D-A 未变）：T04 不得顺手把 `cs_` 也放开。"""
    for op in sorted(CROSS_SECTION_ONLY_FUNCS):
        with pytest.raises(ValueError) as ei:
            IndicatorFormulaEvaluator(f"{op}(close)")
        assert "cross-section" in str(ei.value)


def test_sandbox_computes_time_series_from_series_context():
    """给出**序列**上下文时，`ts_` 必须算出真实标量值。"""
    context = {"close": ATR_SEQUENCE, "roe_ttm": [0.10, 0.11, 0.13, 0.12, 0.15]}
    assert IndicatorFormulaEvaluator("ts_delta_bars(close, 2)").evaluate(context) == pytest.approx(2.0)
    assert IndicatorFormulaEvaluator("ts_delta_bars(close, 3)").evaluate(context) == pytest.approx(4.0)
    assert IndicatorFormulaEvaluator("ts_atr(close, 2)").evaluate(context) == pytest.approx(2.0)
    assert IndicatorFormulaEvaluator("ts_atr(close, 3)").evaluate(context) == pytest.approx(2.0)


def test_sandbox_delta_periods_behaves_as_positional_delta():
    """单标的序列无法区分「报告期」与「交易日」，只能按位置差分。

    这是**已知且已记录的语义限制**：报告期语义由因子流水线（字段所属
    `source_table`）保证，沙箱侧不猜。断言其数值等于 bars 版本，防止有人
    在这里偷偷加一个「交易日→报告期」的隐式换算。
    """
    context = {"roe_ttm": [0.10, 0.11, 0.13, 0.12, 0.15]}
    bars = IndicatorFormulaEvaluator("ts_delta_bars(roe_ttm, 2)").evaluate(context)
    periods = IndicatorFormulaEvaluator("ts_delta_periods(roe_ttm, 2)").evaluate(context)
    assert bars == periods == pytest.approx(0.02)


@pytest.mark.parametrize("n", [2, 3])
def test_sandbox_and_panel_agree_on_last_value(n):
    """★ 跨实现一致性：沙箱（标量）与面板（pandas）必须给出同一个末期值。

    两套实现刻意不互相 import（保持 `app/services/` 跨域边界干净），
    所以必须由测试来兜住数值漂移。
    """
    sandbox_delta = ALLOWED_FUNCS["ts_delta_bars"](ATR_SEQUENCE, n)
    panel_delta = float(ts_delta_bars(ATR_PANEL, n)["x"].iloc[-1])
    assert sandbox_delta == pytest.approx(panel_delta)

    sandbox_atr = ALLOWED_FUNCS["ts_atr"](ATR_SEQUENCE, n)
    panel_atr = float(ts_atr(ATR_PANEL, n)["x"].iloc[-1])
    assert sandbox_atr == pytest.approx(panel_atr)


def test_sandbox_returns_none_for_scalar_context_documenting_caller_degradation():
    """⚠ 记录一个**调用方侧**的既有退化路径（非 T04 引入，未修，见 PROGRESS.json observations）。

    沙箱的**唯一**调用方 `discovery_fast_scan._advanced_filter` 构造的 context 是
    **最新一根 K 线的标量**（`close = float(latest_bar.close)`，`discovery_fast_scan.py:436-446`），
    **没有序列**。于是任何需要序列的函数（含本族 `ts_`）拿到标量 → 返回 None →
    调用方 `if result is None: continue` → 该指标**不参与筛选（视为通过）**，
    即**筛选条件静默失效**。

    这对 `ts_` 是新增的影响面（此前列表类函数如 `sum(close)` 已存在同一问题）。
    修复点不在 T04 写权限内（需改 `discovery_fast_scan` 传序列），故此处只固化现状。

    注意：**不得**把本行为改成「构造期拒绝」 —— 沙箱自身的设计契约是支持序列
    （`IndicatorFormulaEvaluator` docstring 的示例即 `{"close": [10, 11, 12]}`），
    拒绝会让正确的序列化调用方也被挡住。
    """
    for op in TS_OPS:
        assert ALLOWED_FUNCS[op](3.5, 2) is None, f"{op} 对标量输入应返回 None（无法求值）"


def test_sandbox_time_series_invalid_args_return_none():
    """参数不合法时返回 None（沙箱的统一约定：不抛异常，交由调用方判定）。"""
    assert ALLOWED_FUNCS["ts_delta_bars"](ATR_SEQUENCE, 0) is None
    assert ALLOWED_FUNCS["ts_delta_bars"](ATR_SEQUENCE, 1.5) is None
    assert ALLOWED_FUNCS["ts_delta_bars"](ATR_SEQUENCE, 99) is None  # 期数超过序列长度
    assert ALLOWED_FUNCS["ts_atr"](ATR_SEQUENCE, 99) is None
    assert ALLOWED_FUNCS["ts_delta_bars"]([1.0, None, 3.0], 1) is None


def test_sandbox_comparison_nodes_are_still_forbidden():
    """记录既有设计：沙箱 DSL 是**算术式**，阈值由指标定义的 `min_value`/`max_value` 承载。

    `discovery_fast_scan.py:474-481` 拿公式结果与 `ind_def` 里的阈值比较，
    所以公式里不需要（也不允许）写 `> 0`。这不是缺陷，是分工。
    """
    with pytest.raises(ValueError) as ei:
        IndicatorFormulaEvaluator("ts_delta_bars(close, 2) > 0")
    assert "Compare" in str(ei.value)


def test_sandbox_existing_functions_unchanged():
    assert IndicatorFormulaEvaluator("abs(-5)").evaluate({}) == 5
    assert IndicatorFormulaEvaluator("10 - 3").evaluate({}) == 7
    assert IndicatorFormulaEvaluator("min(3, 5)").evaluate({}) == 3
    assert IndicatorFormulaEvaluator("round(3.14159)").evaluate({}) == 3
    for name in ("abs", "min", "max", "round", "sum", "len", "log", "sqrt", "exp"):
        assert name in ALLOWED_FUNCS, f"既有函数 {name} 被误删"
        assert name not in TIME_SERIES_FUNCS
        assert name not in CROSS_SECTION_ONLY_FUNCS


def test_sandbox_unknown_function_message_unchanged():
    with pytest.raises(ValueError) as ei:
        IndicatorFormulaEvaluator("totally_unknown_fn(close)")
    assert "unsupported function" in str(ei.value)


# ══════════════════════════════════════════════════════════
# F. content_hash 不变（去重键回归）
# ══════════════════════════════════════════════════════════


def test_golden_hash_archive_is_readable():
    """先确认归档可读，否则下面的回归断言会「静默通过」。"""
    golden = _golden_hashes()
    assert len(golden) >= 10, f"golden hash 归档条目过少（{len(golden)}），归档可能丢失"


@pytest.mark.parametrize("formula,expected_hash", sorted(_golden_hashes().items()))
def test_content_hash_unchanged_for_existing_formulas(formula, expected_hash):
    """★ 去重键回归：不使用新算子的公式 hash 必须逐字节不变。

    `content_hash` → `factor_versions.execution_plan_hash` 是因子去重身份键。
    """
    result = compile_formula(formula=formula)
    assert result.success, [e.error_code for e in result.errors]
    assert result.execution_plan.content_hash() == expected_hash


@pytest.mark.parametrize(
    "formula,expected_hash",
    [
        ("ts_delta_bars(close, 20)", "04018f5cfaace6df"),
        ("ts_delta_periods(roe_ttm, 4)", "2f96bcf1acb15972"),
        ("ts_atr(close, 14)", "d2944819857f9daf"),
    ],
)
def test_new_time_series_formula_gets_its_own_hash(formula, expected_hash):
    """反向确认：用新算子的公式才需要新哈希（此前是 `function_not_in_catalog`）。"""
    result = compile_formula(formula=formula)
    assert result.success
    assert result.execution_plan.content_hash() == expected_hash


def test_content_hash_is_deterministic_across_calls():
    a = compile_formula(formula="ts_atr(close, 14) / close").execution_plan
    b = compile_formula(formula="ts_atr(close, 14) / close").execution_plan
    assert a.content_hash() == b.content_hash()


# ══════════════════════════════════════════════════════════
# G. 方向标注：混合轴、从内到外
# ══════════════════════════════════════════════════════════


def test_directional_calls_mixes_both_axes_innermost_first():
    """★ 内外层轴不同时，仍必须从内到外：先算时序差分，再做截面排序。"""
    tree = ast.parse("cs_rank(volume) - cs_rank(ts_delta_bars(close, 5))", mode="eval")
    assert collect_directional_calls(tree) == [
        ("cs_rank", DIRECTION_CROSS_SECTION),
        ("ts_delta_bars", DIRECTION_TIMESERIES),
        ("cs_rank", DIRECTION_CROSS_SECTION),
    ]


def test_time_series_ops_are_not_cross_section():
    for op in TS_OPS:
        assert get_function_direction(op) != DIRECTION_CROSS_SECTION
        assert get_function_direction(op) == DIRECTION_TIMESERIES


def test_elementwise_functions_still_have_no_direction():
    for name in ("abs", "round", "log", "sma", "ref", "pct_change"):
        assert get_function_direction(name) is None


def test_direction_is_not_stored_in_execution_plan():
    """★ D-B 回归：方向标注**不得**进入 ExecutionPlan / content_hash。

    若把方向写进 `to_canonical_json()` / `formula_ast`，**全量**既有
    `factor_versions.execution_plan_hash` 会失效、同公式被重复建版本。
    """
    result = compile_formula(formula="ts_atr(close, 14) / close")
    plan = result.execution_plan
    canonical = plan.to_canonical_json()
    assert "timeseries" not in canonical
    assert "cross_section" not in canonical
    assert "FUNCTION_DIRECTION" not in canonical
    # `direction` 是**因子方向**（higher_better / lower_better），与计算方向同名不同义
    assert plan.direction == "higher_better"
    assert plan.data_dependencies["functions"] == ["ts_atr"]
