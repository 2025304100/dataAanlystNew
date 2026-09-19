"""T05 · 方言归一 + `factor_executor` 截面/时序分支

阶段 PH1 ｜ 门禁 G1 ｜ 模块 M2（SD-v2.0 §6.2，需求 §6.0，开发文档 §3.14 步骤 4/6）

**本文件守六件事：**

1. **方言归一表**逐条正确，且**不过度替换** —— `cs_rank` 里的 `rank`、
   `turnover_rate` 里的 `turnover`、`hot_rank_pct` 里的 `rank` 都不能被误改。

2. **`delta` 判不出来就报错，不猜** —— 它是归一表里唯一需要推断的规则
   （按首参字段所属表判定行情/财报）。判不出来必须给可行动的错误，而不是猜个单位。

3. **方言形态与规范形态得到同一个 `content_hash`** ——
   这保证同一因子不会因为「写法不同」而产生两个版本、破坏去重。

4. **作用域边界** —— 归一**仅在公式内生效**：公式里的 `rank(x)` 变 `cs_rank(x)`，
   但后处理配置项 `rank` 完全不受影响，两者并存。

5. **执行器必须真的接上两类算子** —— 分派点若漏了 `cross_section` / `timeseries`
   分支，会落到 `return None` → 因子值**全 NaN 且不报错**。
   本文件用「数值精确断言」而不是「不是 None」来守（后者挡不住静默 NaN）。

6. **R2 兼容** —— 新参数 `panel_index` 是**关键字可选**、默认 None，
   不传时行为与改动前逐字一致。

跑法（DoD）：

    .venv/Scripts/python.exe -m pytest \
        tests/services/factors/mining/test_dialect_and_executor.py -q
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
import warnings

import pandas as pd
import pytest

from app.services.factors.dsl.dialect import (
    DELTA_BY_SOURCE_TABLE,
    DELTA_DIALECT_NAME,
    DIALECT_ALIASES,
    DIALECT_ERROR_CODE,
    DIRECT_FUNCTION_RENAMES,
    DialectError,
    normalize_dialect,
)
from app.services.factors.factor_compiler import (
    ERROR_CODES,
    compile_formula,
)
from app.services.factors.factor_executor import (
    PANEL_SYMBOL_LEVEL,
    PANEL_TRADE_DATE_LEVEL,
    _build_panel_index,
    _coerce_periods,
    _eval_ast,
    apply_postprocess,
)

pytestmark = pytest.mark.whitebox

_T03_GOLDEN = (
    pathlib.Path(__file__).resolve().parents[4]
    / ".workbuddy" / "mining" / "evidence" / "T03_content_hash_golden.json"
)


def _golden_hashes() -> dict[str, str]:
    """T03 归档的既有公式 hash（不含 cs_ / ts_），必须逐条不变。"""
    if not _T03_GOLDEN.exists():  # pragma: no cover
        return {}
    data = json.loads(_T03_GOLDEN.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if "cs_" not in k and "ts_" not in k}


# ══════════════════════════════════════════════════════════
# A. 方言归一表
# ══════════════════════════════════════════════════════════

#: (方言写法, 期望归一结果) —— 前 10 条应改写，后 8 条**不得**改写
REWRITE_CASES = [
    ("std(close, 20)", "stddev(close, 20)"),
    ("stdev(close, 20)", "stddev(close, 20)"),
    ("llv(low, 20)", "lowest(low, 20)"),
    ("hhv(high, 20)", "highest(high, 20)"),
    ("turnover / mean(turnover, 20)", "turnover_rate / mean(turnover_rate, 20)"),
    ("atr(close, 14)", "ts_atr(close, 14)"),
    ("zscore(close)", "cs_zscore(close)"),
    ("rank(close) - rank(volume)", "cs_rank(close) - cs_rank(volume)"),
    ("delta(close, 20)", "ts_delta_bars(close, 20)"),
    ("delta(roe_ttm, 4)", "ts_delta_periods(roe_ttm, 4)"),
]

NO_REWRITE_CASES = [
    "stddev(close, 20)",              # 已是规范名
    "cs_rank(close)",                 # 含 rank，但不是独立标识符
    "ts_atr(close, 14)",              # 含 atr，但不是独立标识符
    "turnover_rate / close",          # 含 turnover，但不是独立标识符
    "hot_rank_pct",                   # 字段名里含 rank
    "ts_delta_bars(close, 20)",       # 已是规范名
    "close / sma(close, 20) - 1",     # 无方言 token
    "highest(high, 20) - lowest(low, 20)",
]


@pytest.mark.parametrize("dialect_form,canonical_form", REWRITE_CASES)
def test_dialect_rewrites_to_canonical(dialect_form, canonical_form):
    assert normalize_dialect(dialect_form) == canonical_form


@pytest.mark.parametrize("formula", NO_REWRITE_CASES)
def test_canonical_forms_are_left_untouched(formula):
    """★ 反向断言：归一必须**幂等且不过度替换**。

    只测「该改的改了」是不够的 —— 一个把 `cs_rank` 改成 `cs_cs_rank`
    的实现也能通过正向测试。
    """
    assert normalize_dialect(formula) == formula


def test_dialect_is_idempotent():
    """归一两次 == 归一一次（否则 compile 与多处调用会漂移）。"""
    for dialect_form, _ in REWRITE_CASES:
        once = normalize_dialect(dialect_form)
        assert normalize_dialect(once) == once


def test_quoted_strings_are_preserved():
    """引号内的内容不得被改写（与 `_normalize_dsl_surface` 同一约定）。"""
    assert normalize_dialect("'std(close,20)'") == "'std(close,20)'"
    assert normalize_dialect('"llv(low,5)"') == '"llv(low,5)"'
    assert normalize_dialect("if(close > 0, 'rank', 'llv')") == "if(close > 0, 'rank', 'llv')"


def test_alias_and_rename_tables_frozen():
    """两张表是冻结契约，改动需走裁决。"""
    assert DIALECT_ALIASES == {
        "std": "stddev",
        "stdev": "stddev",
        "llv": "lowest",
        "hhv": "highest",
        "turnover": "turnover_rate",
    }
    assert DIRECT_FUNCTION_RENAMES == {
        "rank": "cs_rank",
        "zscore": "cs_zscore",
        "atr": "ts_atr",
    }
    assert DELTA_BY_SOURCE_TABLE == {
        "raw_daily_bars": "ts_delta_bars",
        "raw_financial_reports": "ts_delta_periods",
    }
    assert DELTA_DIALECT_NAME == "delta"


# ══════════════════════════════════════════════════════════
# B. `delta` 的单位判定（需求 §6.0「逻辑缺口 2」）
# ══════════════════════════════════════════════════════════


def test_delta_uses_bars_for_quote_fields():
    """行情字段（raw_daily_bars）→ `ts_delta_bars`，n 单位=交易日。"""
    assert normalize_dialect("delta(close, 20)") == "ts_delta_bars(close, 20)"
    assert normalize_dialect("delta(volume, 5)") == "ts_delta_bars(volume, 5)"


def test_delta_uses_periods_for_financial_fields():
    """财报字段（raw_financial_reports）→ `ts_delta_periods`，n 单位=报告期。"""
    assert normalize_dialect("delta(roe_ttm, 4)") == "ts_delta_periods(roe_ttm, 4)"


def test_delta_table_mapping_is_derived_from_field_catalog():
    """判定依据必须真的来自 `FIELD_CATALOG.source_table`，不是硬编码字段名清单。"""
    from app.services.factors.factor_compiler import FIELD_CATALOG

    assert FIELD_CATALOG["close"].source_table in DELTA_BY_SOURCE_TABLE
    assert FIELD_CATALOG["roe_ttm"].source_table in DELTA_BY_SOURCE_TABLE
    assert DELTA_BY_SOURCE_TABLE[FIELD_CATALOG["close"].source_table] == "ts_delta_bars"
    assert DELTA_BY_SOURCE_TABLE[FIELD_CATALOG["roe_ttm"].source_table] == "ts_delta_periods"


@pytest.mark.parametrize(
    "formula",
    [
        "delta(not_a_field, 5)",   # 完全未知的字段
        "delta(pe_ttm, 5)",        # 所属表既非行情也非财报（raw_valuation_snapshots）
        "delta(close * 2, 5)",     # 首参不是标识符
        "delta(close + volume, 5)",
    ],
)
def test_delta_raises_when_unit_cannot_be_determined(formula):
    """★ 判定不出来必须报错，**不猜**。

    猜错的后果是静默的（n 被当错单位），比报错难发现得多。
    """
    with pytest.raises(DialectError) as ei:
        normalize_dialect(formula)
    message = str(ei.value)
    assert "ts_delta_bars" in message and "ts_delta_periods" in message, (
        "错误信息必须给出可行动的替代写法"
    )


def test_delta_error_carries_structured_detail():
    with pytest.raises(DialectError) as ei:
        normalize_dialect("delta(pe_ttm, 5)")
    assert ei.value.detail.get("field") == "pe_ttm"
    assert ei.value.detail.get("source_table") == "raw_valuation_snapshots"


# ══════════════════════════════════════════════════════════
# C. 编译集成
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("dialect_form,_canonical", REWRITE_CASES)
def test_dialect_forms_compile(dialect_form, _canonical):
    result = compile_formula(formula=dialect_form)
    assert result.success, f"{dialect_form} -> {[e.error_code for e in result.errors]}"


@pytest.mark.parametrize("dialect_form,canonical_form", REWRITE_CASES)
def test_dialect_and_canonical_share_the_same_hash(dialect_form, canonical_form):
    """★ 同一因子的两种写法必须得到**同一个去重键**。

    `content_hash` → `factor_versions.execution_plan_hash` 是去重身份键。
    若两种写法哈希不同，同一因子会被重复建版本，导致「因子库出现孪生条目」。
    """
    a = compile_formula(formula=dialect_form)
    b = compile_formula(formula=canonical_form)
    assert a.success and b.success
    assert a.execution_plan.content_hash() == b.execution_plan.content_hash()


def test_dialect_error_becomes_structured_compile_error():
    """`DialectError` 必须被编译层转成结构化 `CompileError`，而不是向上抛。"""
    result = compile_formula(formula="delta(not_a_field, 5)")
    assert not result.success
    codes = [e.error_code for e in result.errors]
    assert codes == [DIALECT_ERROR_CODE]
    assert DIALECT_ERROR_CODE in ERROR_CODES, "错误码必须登记进 ERROR_CODES"
    # 错误信息要能落到用户可读的 fix 建议
    assert "ts_delta_bars" in result.errors[0].message


def test_dialect_error_reports_position_when_possible():
    """能定位就定位：`delta` 在原文里存在，token 应被找到。"""
    result = compile_formula(formula="close + delta(not_a_field, 5)")
    assert not result.success
    error = result.errors[0]
    assert error.token == "not_a_field" or error.start is not None


@pytest.mark.parametrize(
    "formula,expected_hash",
    [
        ("std(close, 20)", "da2cc5c5d4e7cb87"),
        ("llv(low, 20)", "3724624a34c5c1e7"),
        ("hhv(high, 20)", "333218bace594300"),
        ("turnover / close", "d6108d4c608b0c9c"),
        ("atr(close, 14)", "d2944819857f9daf"),
        ("delta(close, 20)", "04018f5cfaace6df"),
        ("delta(roe_ttm, 4)", "2f96bcf1acb15972"),
        ("rank(close) - rank(volume)", "373e7f5d90d66524"),
        ("zscore(close)", "169febc60d2c5dce"),
    ],
)
def test_dialect_forms_golden_hash(formula, expected_hash):
    result = compile_formula(formula=formula)
    assert result.success
    assert result.execution_plan.content_hash() == expected_hash


# ══════════════════════════════════════════════════════════
# D. 作用域边界：公式内 vs 后处理（需求 §6.0 明确「归一仅在公式内生效」）
# ══════════════════════════════════════════════════════════


def test_formula_rank_becomes_cross_section_but_postprocess_rank_is_untouched():
    """★ 作用域边界（最容易做错的一条）。

    公式里的 `rank(x)` → `cs_rank(x)`（**求值阶段**的横截面排名）；
    后处理配置项 `rank` 保持不变（**对最终因子值**再做一次归一化），两者并存。
    """
    in_formula = compile_formula(formula="rank(close)")
    assert in_formula.success
    assert in_formula.execution_plan.formula == "cs_rank(close)"
    assert in_formula.execution_plan.postprocess is None

    with_postprocess = compile_formula(
        formula="close / sma(close, 20)",
        postprocess={"rank": {"method": "percentile", "ascending": True}},
    )
    assert with_postprocess.success
    # 方言归一**不得**碰后处理配置
    assert with_postprocess.execution_plan.postprocess == {
        "rank": {"ascending": True, "method": "percentile"}
    }
    # 公式本身无方言 token，应原样保留
    assert with_postprocess.execution_plan.formula == "close / sma(close, 20)"


def test_both_rank_paths_can_coexist():
    """公式内 cs_rank + 后处理 rank 同时使用是合法的（两者语义不同、并存）。"""
    result = compile_formula(
        formula="rank(close) - rank(volume)",
        postprocess={"rank": {"method": "percentile"}},
    )
    assert result.success
    assert result.execution_plan.formula == "cs_rank(close) - cs_rank(volume)"
    assert result.execution_plan.postprocess == {"rank": {"method": "percentile"}}


def test_postprocess_rank_behaviour_unchanged():
    """回归：后处理 `apply_postprocess` 的 rank 行为不得被本次改动影响。"""
    series = pd.Series([3.0, 1.0, 4.0, 1.0, 5.0], index=list("abcde"))
    _, normalized = apply_postprocess(
        series, {"rank": {"method": "percentile", "ascending": True}}
    )
    assert [round(float(v), 6) for v in normalized] == [0.6, 0.3, 0.8, 0.3, 1.0]


def test_dialect_never_touches_postprocess_payload():
    """即使公式与后处理同时出现 `rank`，也只有公式侧被改写。"""
    result = compile_formula(
        formula="rank(close)",
        postprocess={"rank": {"method": "ordinal", "ascending": False}},
    )
    assert result.success
    assert result.execution_plan.postprocess == {
        "rank": {"ascending": False, "method": "ordinal"}
    }


# ══════════════════════════════════════════════════════════
# E. content_hash 回归（既有公式零变化）
# ══════════════════════════════════════════════════════════


def test_golden_hash_archive_is_readable():
    assert len(_golden_hashes()) >= 10, "golden hash 归档条目过少，回归断言会假绿"


@pytest.mark.parametrize("formula,expected_hash", sorted(_golden_hashes().items()))
def test_content_hash_unchanged_for_existing_formulas(formula, expected_hash):
    """★ 方言归一不得改动任何**既有合法公式**的哈希。

    归一表里的目标名（`stddev`/`lowest`/`highest`/`turnover_rate`/`cs_*`/`ts_*`）
    在改动前要么已存在、要么是不可编译的新算子，故不应影响任何旧公式。
    """
    result = compile_formula(formula=formula)
    assert result.success, [e.error_code for e in result.errors]
    assert result.execution_plan.content_hash() == expected_hash


# ══════════════════════════════════════════════════════════
# F. 执行器：面板索引
# ══════════════════════════════════════════════════════════

DATES = list(pd.date_range("2026-01-05", periods=4, freq="D"))
CLOSE_BY_SYMBOL = {
    "A": [1.0, 2.0, 3.0, 4.0],
    "B": [2.0, 3.0, 4.0, 5.0],
    "C": [3.0, 4.0, 5.0, 6.0],
    "D": [4.0, 5.0, 6.0, 7.0],
}
VOLUME_BY_SYMBOL = {
    "A": [100.0, 1.0, 50.0, 10.0],
    "B": [1.0, 50.0, 10.0, 100.0],
    "C": [50.0, 10.0, 100.0, 1.0],
    "D": [10.0, 100.0, 1.0, 50.0],
}


def _source_data() -> pd.DataFrame:
    """模拟 `_read_source_data` 的返回：**多日 × 多标的**，按 (symbol, trade_date) 排序。"""
    rows = []
    for symbol in CLOSE_BY_SYMBOL:
        for index, trade_date in enumerate(DATES):
            rows.append({
                "trade_date": trade_date,
                "symbol": symbol,
                "close": CLOSE_BY_SYMBOL[symbol][index],
                "volume": VOLUME_BY_SYMBOL[symbol][index],
            })
    return pd.DataFrame(rows)


SOURCE_DATA = _source_data()
CONTEXT = {
    column: SOURCE_DATA[column]
    for column in SOURCE_DATA.columns
    if column not in ("symbol", "trade_date")
}
SYMBOL_SERIES = SOURCE_DATA["symbol"]
PANEL_INDEX = _build_panel_index(SOURCE_DATA)


def _evaluate(formula: str, *, panel: bool = True):
    """直接调 `_eval_ast`（不经 DB），复现 `_evaluate` 的装配方式。"""
    tree = ast.parse(formula, mode="eval")
    return _eval_ast(
        tree.body,
        CONTEXT,
        symbol_series=SYMBOL_SERIES,
        panel_index=PANEL_INDEX if panel else None,
    )


def test_build_panel_index_preserves_row_order():
    """面板索引必须与 source_data 行序**逐行一致**（不是 from_product 的笛卡尔序）。"""
    assert PANEL_INDEX is not None
    assert PANEL_INDEX.names == ["trade_date", "symbol"]
    assert len(PANEL_INDEX) == len(SOURCE_DATA)
    assert list(PANEL_INDEX.get_level_values("symbol")) == list(SOURCE_DATA["symbol"])
    assert list(PANEL_INDEX.get_level_values("trade_date")) == list(SOURCE_DATA["trade_date"])


def test_build_panel_index_levels_and_constants():
    assert PANEL_TRADE_DATE_LEVEL == 0
    assert PANEL_SYMBOL_LEVEL == 1
    assert PANEL_INDEX.get_level_values(PANEL_TRADE_DATE_LEVEL).name == "trade_date"
    assert PANEL_INDEX.get_level_values(PANEL_SYMBOL_LEVEL).name == "symbol"


def test_build_panel_index_returns_none_without_required_columns():
    assert _build_panel_index(pd.DataFrame({"close": [1.0, 2.0]})) is None
    assert _build_panel_index(pd.DataFrame({"symbol": ["A"], "close": [1.0]})) is None


# ══════════════════════════════════════════════════════════
# G. 执行器：两个分支都必须真的接上（否则静默全 NaN）
# ══════════════════════════════════════════════════════════


def test_cross_section_branch_is_wired():
    """★ `cross_section` 分支：数值必须精确正确，不能只看「不是 None」。

    用精确排名断言，顺带守住一个真实踩过的坑：
    若把 Series 直接交给 `pd.DataFrame(series, index=multiindex)`，
    pandas 会按**标签对齐** → 整列 NaN。那时「不是 None」依然成立，
    只有数值断言能发现。
    """
    result = _evaluate("cs_rank(close)")
    assert result is not None
    assert not result.isna().any(), "截面排名出现 NaN 说明面板构造按标签对齐了（会静默全 NaN）"
    # 行序是「按 symbol 分组」的（见 _source_data），而排名是**按 trade_date**算的。
    # 每个交易日内 A<B<C<D，且各标的的 close 每日等差递增 → 每个标的的排名恒定：
    #   A=0.25, B=0.5, C=0.75, D=1.0，各占 4 行（4 个交易日）
    expected = []
    for rank_value in (0.25, 0.5, 0.75, 1.0):      # A, B, C, D
        expected.extend([rank_value] * len(DATES))
    assert [round(float(v), 6) for v in result] == expected


def test_timeseries_branch_is_wired():
    """★ `timeseries` 分支：按标的位移，不与其它标的串值。"""
    result = _evaluate("ts_delta_bars(close, 2)")
    assert result is not None
    # 每个标的 [1,2,3,4] → x - shift(2) = [nan, nan, 2, 2]
    assert [None if pd.isna(v) else float(v) for v in result] == [
        None, None, 2.0, 2.0,
    ] * 4


def test_ts_atr_branch_values():
    result = _evaluate("ts_atr(close, 2)")
    assert result is not None
    # |diff| = [nan,1,1,1] → rolling(2, min_periods=2).mean() = [nan, nan, 1, 1]
    assert [None if pd.isna(v) else float(v) for v in result] == [
        None, None, 1.0, 1.0,
    ] * 4


def test_mixed_axis_nesting_is_wired():
    """★ 内外层轴不同：先按标的做时序差分，再按交易日做截面排名。"""
    result = _evaluate("cs_rank(volume) - cs_rank(ts_delta_bars(close, 2))")
    assert result is not None
    values = [None if pd.isna(v) else round(float(v), 4) for v in result]
    # 每标的前 2 期时序结果 NaN → 外层 cs_rank 对该期全 NaN → 结果也是 NaN
    assert values == [
        None, None, 0.125, -0.125,
        None, None, -0.125, 0.375,
        None, None, 0.375, -0.375,
        None, None, -0.375, 0.125,
    ]


def test_result_index_matches_source_data():
    """返回值必须按 `source_data.index` 对齐，否则下游后处理会错位。"""
    for formula in ("cs_rank(close)", "ts_delta_bars(close, 2)", "cs_zscore(close)"):
        result = _evaluate(formula)
        assert result is not None
        assert result.index.equals(SOURCE_DATA.index), formula


def test_cross_section_and_timeseries_do_not_group_on_the_same_axis():
    """★ 分组轴不能写反。

    - `cs_rank` 按 **trade_date**：同一标的不同日期的排名互不影响 →
      若按 symbol 分组，A 在 4 天内的排名会随日期递增（0.25→1.0），与本断言矛盾。
    - `ts_delta_bars` 按 **symbol**：同一日期不同标的互不影响 →
      若按 trade_date 分组，同一交易日内的差分会跨标的串值。
    """
    cross = _evaluate("cs_rank(close)")
    by_symbol_a = [float(v) for v in cross[SOURCE_DATA["symbol"] == "A"]]
    assert by_symbol_a == [0.25, 0.25, 0.25, 0.25], "cs_rank 按 symbol 分组了（轴写反）"

    time_series = _evaluate("ts_delta_bars(close, 2)")
    by_date_first = [None if pd.isna(v) else float(v) for v in time_series[SOURCE_DATA["trade_date"] == DATES[0]]]
    assert by_date_first == [None, None, None, None], (
        "同一日期首日应全部 NaN（各标的窗口不足），若串值会出现非 NaN"
    )


def test_no_panel_index_falls_back_to_none():
    """★ R2：不传 `panel_index` 时，两类算子返回 None（与改动前行为一致）。

    这也是「只加可选参数、默认值保持原行为」的可执行证据。
    """
    for formula in ("cs_rank(close)", "ts_delta_bars(close, 2)", "ts_atr(close, 14)"):
        assert _evaluate(formula, panel=False) is None


def test_cross_section_on_valid_panel_emits_no_numpy_warning():
    """★ 全 NaN 的典型信号是 numpy 的 `Mean of empty slice` RuntimeWarning。

    把它当错误捕获，可以在数值断言之外多一层探测。
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _evaluate("cs_rank(close)")
    runtime = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    assert not runtime, f"出现 RuntimeWarning（疑似面板全 NaN）：{[str(w.message) for w in runtime]}"
    assert result is not None and not result.isna().any()


# ══════════════════════════════════════════════════════════
# H. 执行器：既有算子与签名兼容（R2）
# ══════════════════════════════════════════════════════════


def test_eval_ast_signature_adds_only_an_optional_keyword():
    """★ R2：`panel_index` 必须是**关键字可选**且默认 None。"""
    parameters = inspect.signature(_eval_ast).parameters
    assert "panel_index" in parameters
    assert parameters["panel_index"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["panel_index"].default is None
    # 既有参数一个都不能少
    assert list(parameters)[:3] == ["node", "context", "symbol_series"]
    assert parameters["symbol_series"].default is None


def test_existing_rolling_and_math_operators_unchanged():
    """回归：既有算子（rolling / math）行为不得变化。"""
    assert [float(v) for v in _evaluate("sma(close, 2)")] == [
        1.0, 1.5, 2.5, 3.5,
        2.0, 2.5, 3.5, 4.5,
        3.0, 3.5, 4.5, 5.5,
        4.0, 4.5, 5.5, 6.5,
    ]
    ref = _evaluate("ref(close, 1)")
    assert [None if pd.isna(v) else float(v) for v in ref] == [
        None, 1.0, 2.0, 3.0,
        None, 2.0, 3.0, 4.0,
        None, 3.0, 4.0, 5.0,
        None, 4.0, 5.0, 6.0,
    ]
    # A@d1 的 close=2.0 → abs(2-1)=1.0（行序按 symbol，第 1 行是 A 的第 2 个交易日）
    assert float(_evaluate("abs(close - 1)")[1]) == 1.0
    assert float(_evaluate("abs(close - 1)")[0]) == 0.0


def test_min_periods_is_deliberately_not_unified():
    """★ `ts_atr` 的前 n 期是 NaN（不造值），而 `sma` 用 min_periods=1（允许部分窗口）。

    这是**有意的不一致**（需求 §3.5：不得自动剔除/降级/填零）。
    若有人「顺手统一」min_periods，本断言会红。
    """
    sma = _evaluate("sma(close, 3)")
    atr = _evaluate("ts_atr(close, 3)")
    # sma 第 1 期就有值（min_periods=1）
    assert not pd.isna(sma.iloc[0])
    # ts_atr 前 3 期必须是 NaN（min_periods=3），且**不得填 0**
    assert atr.iloc[:3].isna().all()
    assert not (atr.iloc[:3] == 0.0).any()


def test_coerce_periods_rules():
    assert _coerce_periods(2) == 2
    assert _coerce_periods(2.0) == 2
    assert _coerce_periods(pd.Series([3.0, 3.0])) == 3
    assert _coerce_periods(0) is None, "0 必须被拒：下游 cs_quantile 会因此抛 ValueError"
    assert _coerce_periods(-1) is None
    assert _coerce_periods(1.5) is None
    assert _coerce_periods("x") is None
    assert _coerce_periods(None) is None
    assert _coerce_periods(True) is None
    assert _coerce_periods(pd.Series(dtype=float)) is None


def test_panel_func_fails_soft_instead_of_raising():
    """★ 兜底：dsl 层抛出的值域错误（如 `cs_quantile` 要求 n>=2）必须被吞成 None。

    编译期已用 `window_arg_index` 拦下绝大多数非法入参，但「值域下限」只有
    dsl 层知道（`cs_quantile` 的 n 必须 >= 2）。若让它冒泡，`execute()` 会 500；
    返回 None 则与其它失败分支一致地落成 NaN。
    """
    # 直接构造一个 n=1 的 cs_quantile 调用（编译期不可达，走运行时兜底路径）
    tree = ast.parse("cs_quantile(close, 1)", mode="eval")
    assert _eval_ast(
        tree.body, CONTEXT, symbol_series=SYMBOL_SERIES, panel_index=PANEL_INDEX
    ) is None

    # 正常 n=2 仍要能算出值（确认兜底没有把正常路径也吞掉）
    ok = _evaluate("cs_quantile(close, 2)")
    assert ok is not None and set(ok.dropna().astype(int).unique()) <= {0, 1}


def test_unknown_operator_still_returns_none():
    """未注册函数仍返回 None（不因新增分支而改变失败约定）。"""
    tree = ast.parse("not_a_real_func(close)", mode="eval")
    assert _eval_ast(
        tree.body, CONTEXT, symbol_series=SYMBOL_SERIES, panel_index=PANEL_INDEX
    ) is None
