"""T03 · `cs_` 截面算子族 + 注册四处联动

阶段 PH1 ｜ 门禁 G1 ｜ 模块 M2（设计文档 §6.2，需求 §6.0，开发文档 §3.14）

**本文件守三件事：**

1. **注册四处联动** —— 新增算子缺任何一处都会炸，且炸法各不相同：
   | # | 位置 | 缺了会怎样 |
   |---|------|-----------|
   | 1 | `factor_compiler.FUNCTION_CATALOG` | 编译期 `function_not_in_catalog` |
   | 2 | `factor_compiler.FUNCTION_DIRECTION` | 求值轴未知，执行器无法分组 |
   | 3 | `formula_catalog._FUNCTION_META` | **`KeyError`** → 公式目录 API 500 |
   | 4 | `indicator_ast_sandbox.ALLOWED_FUNCS` | 沙箱报「不支持的函数」 |

   > 第 3 处是任务卡原文（「三处联动」）**漏掉**的那一处。`build_formula_catalog()`
   > 用 `meta = _FUNCTION_META[key]` **硬索引**遍历 `FUNCTION_CATALOG`
   > （`formula_catalog.py:534`），漏键直接 `KeyError`。
   > 实测：加 `FUNCTION_CATALOG['cs_rank']` 后调 `build_formula_catalog()` → `KeyError: 'cs_rank'`。

2. **`content_hash` 不变**（去重键回归）—— `ExecutionPlan.content_hash()` 是
   `factor_versions.execution_plan_hash` 的**去重身份键**（`factor_registry.py:236/267`）。
   本次只往目录**追加**算子，未改 `_serialize_ast` / `to_canonical_json`，
   因此不使用新算子的公式 hash 必须**逐字节不变**。本文件用 golden hash 钉死这一点。

3. **与后处理 `rank` 语义可区分** —— 后处理只对最终因子值处理一次，无法表达
   `cs_rank(A) - cs_rank(B)`；两者**并存**、互不约束（设计文档 §6.2 作用域边界）。

跑法（DoD）：

    .venv/Scripts/python.exe -m pytest \
        tests/services/factors/mining/test_dsl_cross_section.py -q
"""
from __future__ import annotations

import ast
import math

import numpy as np
import pandas as pd
import pytest

from app.services.factors.dsl.cross_section import (
    CROSS_SECTION_OPS,
    eval_cross_section,
)
from app.services.factors.factor_compiler import (
    DIRECTION_CROSS_SECTION,
    DIRECTION_TIMESERIES,
    FIELD_CATALOG,
    FUNCTION_CATALOG,
    FUNCTION_DIRECTION,
    collect_directional_calls,
    compile_formula,
    get_function_direction,
)
from app.services.factors.factor_executor import apply_postprocess
from app.services.factors.formula_catalog import (
    _FUNCTION_META,
    build_formula_catalog,
)
from app.services.indicator_ast_sandbox import (
    ALLOWED_FUNCS,
    CROSS_SECTION_ONLY_FUNCS,
    IndicatorFormulaEvaluator,
)

pytestmark = pytest.mark.whitebox

CS_OPS = sorted(CROSS_SECTION_OPS)

#: 每个算子的一个合法调用（用于批量断言）
LEGAL_CALLS = {
    "cs_rank": "cs_rank(close)",
    "cs_zscore": "cs_zscore(close)",
    "cs_demean": "cs_demean(close)",
    "cs_scale": "cs_scale(close)",
    "cs_quantile": "cs_quantile(close, 5)",
    "cs_winsorize": "cs_winsorize(close, 3)",
}

#: 改动前（未注册 cs_ 族）即已存在的公式 → content_hash。
#: 由「把 cs_ 键从 FUNCTION_CATALOG 摘掉」模拟改动前状态实跑得到（2026-09-16）。
#: ⚠️ 一旦这些值变了，说明 `_serialize_ast` / `to_canonical_json` / `_collect_dependencies`
#:    被改动 —— 那会让**所有既有因子版本的 execution_plan_hash 失效、去重退化为重复建版本**。
GOLDEN_HASHES: dict[str, str] = {
    "if(pe_ttm > 0, 1 / pe_ttm, 0)": "9b9ab0fc32ec3287",
    "pct_change(close, 20)": "e6669e8aec7e098d",
    "(turnover_rate - mean(turnover_rate, 20)) / max(stddev(turnover_rate, 20), 0.000001)": "7c70c5eb9f088b38",
    "close / sma(close, 20) - 1": "f1df54468567c543",
    "close >= highest(high, 20)": "577594c327c2aee5",
    "stddev(close, 20) / mean(close, 20)": "d075c189492c3026",
    "highest(high, 20) - lowest(low, 20)": "c0aceefb47c2f35a",
    "ref(close, 1)": "9aa2968976f1a572",
    "if(close > sma(close,20), 1, 0)": "3637e6d854de57e7",
    "abs(close - sma(close, 10)) / stddev(close, 10)": "e0f714c5623ed230",
    "log(volume + 1)": "a20875cc0eb73d47",
}


# ══════════════════════════════════════════════════════════
# A. 注册四处联动
# ══════════════════════════════════════════════════════════


def test_cross_section_ops_frozen():
    """算子名集合冻结（改这里等于改 DSL 表面）。"""
    assert CS_OPS == [
        "cs_demean",
        "cs_quantile",
        "cs_rank",
        "cs_scale",
        "cs_winsorize",
        "cs_zscore",
    ]


@pytest.mark.parametrize("op", CS_OPS)
def test_registered_in_function_catalog(op):
    """① 编译期白名单：category 必须标为 cross_section（执行器按 category 分派）。"""
    spec = FUNCTION_CATALOG.get(op)
    assert spec is not None, f"{op} 未注册进 FUNCTION_CATALOG"
    assert spec.category == "cross_section"
    assert spec.name == op


@pytest.mark.parametrize("op", CS_OPS)
def test_registered_in_function_direction(op):
    """② 计算方向标注：决定算子在哪根轴上聚合。"""
    assert FUNCTION_DIRECTION.get(op) == DIRECTION_CROSS_SECTION
    assert get_function_direction(op) == DIRECTION_CROSS_SECTION


@pytest.mark.parametrize("op", CS_OPS)
def test_registered_in_function_meta(op):
    """③ 公式目录元数据。**这是任务卡原文漏掉的第四处。**"""
    meta = _FUNCTION_META.get(op)
    assert meta is not None, f"{op} 缺 _FUNCTION_META 条目 → build_formula_catalog() KeyError"
    for key in ("label_zh", "label_en", "signature", "snippet", "params"):
        assert meta.get(key), f"{op}._FUNCTION_META.{key} 缺失"


@pytest.mark.parametrize("op", CS_OPS)
def test_registered_in_sandbox_whitelist(op):
    """④ 沙箱白名单：登记以便自省，但**不可求值**（见 G 段）。"""
    assert op in ALLOWED_FUNCS
    assert op in CROSS_SECTION_ONLY_FUNCS


def test_function_meta_covers_every_catalog_function():
    """★ 通用哨兵：`_FUNCTION_META` 必须覆盖 `FUNCTION_CATALOG` 的**每一个键**。

    这条比逐个断言 cs_* 更有价值 —— 它拦住的是**将来**任何人往目录加算子却忘了
    （或不知道要加）目录元数据的场景。`build_formula_catalog()` 是硬索引，漏了必炸。
    """
    missing = sorted(set(FUNCTION_CATALOG) - set(_FUNCTION_META))
    assert not missing, (
        f"FUNCTION_CATALOG 有 {len(missing)} 个键缺 _FUNCTION_META: {missing}；"
        "build_formula_catalog()（formula_catalog.py:534）会在这些键上抛 KeyError"
    )


def test_function_direction_keys_are_all_real_functions():
    """方向标注的键必须是真实注册的算子，防止改名后留下悬空标注。"""
    dangling = sorted(set(FUNCTION_DIRECTION) - set(FUNCTION_CATALOG))
    assert not dangling, f"FUNCTION_DIRECTION 指向未注册的函数: {dangling}"
    for name, direction in FUNCTION_DIRECTION.items():
        assert direction in (DIRECTION_TIMESERIES, DIRECTION_CROSS_SECTION), (
            f"{name} 的方向 {direction!r} 不在允许取值内"
        )


def test_elementwise_functions_have_no_direction():
    """math / rolling 是逐元素求值，不得被标成有方向。"""
    for name in ("abs", "round", "log", "sqrt", "sma", "ref", "highest"):
        assert get_function_direction(name) is None, f"{name} 不应有计算方向"
    assert get_function_direction("不存在的算子") is None


def test_build_formula_catalog_succeeds_and_exposes_cross_section_ops():
    """端到端：公式目录 API 必须能构建（曾经这里是 KeyError → 500）。"""
    catalog = build_formula_catalog(None)
    by_key = {f["key"]: f for f in catalog["functions"]}
    for op in CS_OPS:
        assert op in by_key, f"{op} 未出现在公式目录中"
        entry = by_key[op]
        assert entry["category"] == "cross_section"
        assert entry["enabled"] is True
        assert entry["label_zh"] and entry["label_en"] and entry["snippet"]


# ══════════════════════════════════════════════════════════
# B. 编译（DoD 第 2 条：cs_rank(A) - cs_rank(B) 编译成功）
# ══════════════════════════════════════════════════════════


def test_cs_rank_difference_compiles():
    """★ DoD 核心断言：后处理做不到的式子，公式内截面算子可以表达。"""
    result = compile_formula(formula="cs_rank(close) - cs_rank(volume)")
    assert result.success, [e.error_code for e in result.errors]
    plan = result.execution_plan
    assert plan is not None
    assert plan.data_dependencies["functions"] == ["cs_rank"]
    assert plan.data_dependencies["fields"] == ["close", "volume"]


@pytest.mark.parametrize("op", CS_OPS)
def test_every_cross_section_op_compiles(op):
    result = compile_formula(formula=LEGAL_CALLS[op])
    assert result.success, f"{LEGAL_CALLS[op]} -> {[e.error_code for e in result.errors]}"


def test_nested_cross_section_and_rolling_compiles():
    """内层 rolling、外层截面：`cs_rank(sma(close, 20))`。"""
    result = compile_formula(formula="cs_rank(sma(close, 20))")
    assert result.success, [e.error_code for e in result.errors]
    deps = result.execution_plan.data_dependencies
    assert deps["functions"] == ["cs_rank", "sma"]
    assert result.execution_plan.max_lookback == 20


def test_cross_section_ops_compose_with_math():
    result = compile_formula(formula="abs(cs_zscore(close))")
    assert result.success, [e.error_code for e in result.errors]


@pytest.mark.parametrize(
    "formula,expected_code",
    [
        ("cs_rank()", "function_arg_count"),
        ("cs_rank(close, 5)", "function_arg_count"),
        ("cs_quantile(close)", "function_arg_count"),
        ("cs_quantile(close, volume)", "function_window_invalid"),
        ("cs_quantile(close, 0)", "negative_lag"),
        ("cs_rank(not_a_field)", "field_not_in_catalog"),
    ],
)
def test_cross_section_argument_validation(formula, expected_code):
    result = compile_formula(formula=formula)
    assert not result.success
    assert expected_code in [e.error_code for e in result.errors], (
        f"{formula} 应报 {expected_code}，实得 {[e.error_code for e in result.errors]}"
    )


def test_cross_section_n_exceeding_lookback_limit_is_rejected():
    """cs_* 复用 window_arg_index 校验，n 超上限同样被拦。

    注：`cs_winsorize(close, 300)` 会报**两条** lookback_window_exceeded
    （一条来自函数参数校验、一条来自依赖收集的 max_lookback），故只断言包含。
    """
    codes = [e.error_code for e in compile_formula(formula="cs_winsorize(close, 300)").errors]
    assert "lookback_window_exceeded" in codes


# ══════════════════════════════════════════════════════════
# C. 计算方向标注：嵌套从内到外
# ══════════════════════════════════════════════════════════


def test_directional_calls_innermost_first():
    """★ 设计文档 §6.2「嵌套时从内到外逐层计算」。

    `cs_rank(cs_demean(close))`：必须先做 demean（内层）再做 rank（外层）。
    """
    tree = ast.parse("cs_rank(cs_demean(close))", mode="eval")
    assert collect_directional_calls(tree) == [
        ("cs_demean", DIRECTION_CROSS_SECTION),
        ("cs_rank", DIRECTION_CROSS_SECTION),
    ]


def test_directional_calls_skips_elementwise_inner_functions():
    """内层是逐元素/滚动算子（无方向）时应被跳过，只留截面算子。"""
    tree = ast.parse("cs_rank(sma(close, 20))", mode="eval")
    assert collect_directional_calls(tree) == [("cs_rank", DIRECTION_CROSS_SECTION)]


def test_directional_calls_lists_siblings_in_source_order():
    tree = ast.parse("cs_rank(close) - cs_rank(volume)", mode="eval")
    assert collect_directional_calls(tree) == [
        ("cs_rank", DIRECTION_CROSS_SECTION),
        ("cs_rank", DIRECTION_CROSS_SECTION),
    ]


@pytest.mark.parametrize("formula", ["sma(close, 20)", "abs(close)", "log(volume + 1)"])
def test_directional_calls_empty_without_directional_ops(formula):
    assert collect_directional_calls(ast.parse(formula, mode="eval")) == []


def test_direction_is_not_the_same_as_factor_direction():
    """`FUNCTION_DIRECTION`（求值轴）与 `ExecutionPlan.direction`（因子方向）是两个概念。

    因子方向取 higher_better / lower_better；计算方向取 timeseries / cross_section。
    混用会让「方向调优」误判算子分组。
    """
    result = compile_formula(
        formula="cs_rank(close)", direction="lower_better"
    )
    assert result.success
    assert result.execution_plan.direction == "lower_better"
    assert get_function_direction("cs_rank") == "cross_section"
    assert result.execution_plan.direction != get_function_direction("cs_rank")


# ══════════════════════════════════════════════════════════
# D. content_hash 不变（去重键回归）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("formula,expected_hash", sorted(GOLDEN_HASHES.items()))
def test_content_hash_unchanged_for_existing_formulas(formula, expected_hash):
    """★ 去重键回归哨兵。

    `content_hash` 存进 `factor_versions.execution_plan_hash`，`factor_registry` 靠它
    判「同公式已存在 → 复用旧版本」。本次新增算子若改动了哈希输入
    （`_serialize_ast` / `to_canonical_json` / `_collect_dependencies`），
    既有因子版本的哈希会全部失效，同一公式会被**重复建版本**。
    """
    result = compile_formula(formula=formula)
    assert result.success, [e.error_code for e in result.errors]
    assert result.execution_plan.content_hash() == expected_hash


def test_new_cross_section_formula_gets_its_own_hash():
    """反向确认：用了新算子的公式才需要新哈希（此前是编译失败）。"""
    result = compile_formula(formula="cs_rank(close) - cs_rank(volume)")
    assert result.success
    assert result.execution_plan.content_hash() == "373e7f5d90d66524"


def test_content_hash_is_deterministic_across_calls():
    """同一公式重复编译必须得到同一哈希（canonical JSON 已排序键）。"""
    a = compile_formula(formula="cs_rank(close) - cs_rank(volume)").execution_plan
    b = compile_formula(formula="cs_rank(close) - cs_rank(volume)").execution_plan
    assert a.content_hash() == b.content_hash()


# ══════════════════════════════════════════════════════════
# E. 截面语义（按 trade_date 分组）
# ══════════════════════════════════════════════════════════

D1 = pd.Timestamp("2026-01-05")
D2 = pd.Timestamp("2026-01-06")
SYMBOLS = ["A", "B", "C", "D"]


def _panel(values_by_date: dict) -> pd.DataFrame:
    index = pd.MultiIndex.from_product(
        [list(values_by_date), SYMBOLS], names=["trade_date", "symbol"]
    )
    flat = [v for date in sorted(values_by_date) for v in values_by_date[date]]
    return pd.DataFrame({"x": flat}, index=index)


CLOSE = _panel({D1: [10.0, 20.0, 30.0, 40.0], D2: [15.0, 25.0, 35.0, 45.0]})
VOLUME = _panel({D1: [100.0, 1.0, 50.0, 10.0], D2: [5.0, 100.0, 1.0, 50.0]})


def test_cs_rank_is_computed_per_trade_date():
    """★ 截面 = 同一交易日。每天独立排名，不与其它日期混在一起。"""
    ranked = eval_cross_section("cs_rank", CLOSE)["x"]
    assert ranked.loc[(D1, "A")] == pytest.approx(0.25)
    assert ranked.loc[(D1, "D")] == pytest.approx(1.00)
    assert ranked.loc[(D2, "A")] == pytest.approx(0.25)
    # 若误按整段序列排名，D2 的 A 会落在 5/8 而不是 0.25
    assert ranked.loc[(D2, "A")] != pytest.approx(5 / 8)


def test_cs_rank_difference_matches_expected_values():
    """cs_rank(A) - cs_rank(B) 的实测值（跨日独立）。"""
    diff = (eval_cross_section("cs_rank", CLOSE) - eval_cross_section("cs_rank", VOLUME))["x"]
    assert diff.loc[(D1, "A")] == pytest.approx(-0.75)
    assert diff.loc[(D1, "B")] == pytest.approx(0.25)
    assert diff.loc[(D1, "C")] == pytest.approx(0.00)
    assert diff.loc[(D1, "D")] == pytest.approx(0.50)


def test_cs_zscore_degenerate_cross_section_returns_nan():
    """★ 冻结行为（任务卡明确「不要修正」）。

    组内全部同值 → std=0。`cs_zscore` 返回 **NaN**，不是 0。
    理由：组内无差异时给 0 会向进化器注入伪信号；NaN 会被覆盖率统计正确识别为无效截面。
    """
    flat = _panel({D1: [5.0, 5.0, 5.0, 5.0]})
    out = eval_cross_section("cs_zscore", flat)["x"]
    assert out.isna().all(), f"退化截面应全 NaN，实得 {out.tolist()}"


def test_cs_zscore_normal_values():
    """非退化截面的 cs_zscore：均值 0、样本标准差归一（ddof=1，见下条测试）。"""
    out = eval_cross_section("cs_zscore", CLOSE)["x"]
    day1 = out.loc[D1]
    assert float(day1.mean()) == pytest.approx(0.0, abs=1e-12)
    # ⚠️ cs_zscore 走 pandas 的 `groupby().transform("std")`，默认 **ddof=1**
    #    因此标准化后 std(ddof=0) = sqrt((n-1)/n)，n=4 时 = sqrt(3/4)。
    assert float(day1.std(ddof=0)) == pytest.approx(math.sqrt(3 / 4))
    assert float(day1.std(ddof=1)) == pytest.approx(1.0)
    assert [round(float(v), 6) for v in day1] == [
        -1.161895,
        -0.387298,
        0.387298,
        1.161895,
    ]


def test_cs_zscore_ddof_differs_from_postprocess_default():
    """⚠️ 实测口径差异（**刻意冻结，不要「对齐」**）：

    | 位置 | 实现 | ddof |
    |------|------|------|
    | 公式内 `cs_zscore` | `groupby().transform("std")` | **1**（pandas 默认，样本标准差） |
    | 后处理 `zscore` | `factor_executor._zscore(..., ddof=0)` | **0**（总体标准差） |

    设计文档 §6.2 明确两者「语义不同、并存、互不约束」。所以这不是 bug：
    - 改后处理默认 ddof → 会改变**既有因子版本**的因子值，破坏历史可比性
    - 改公式内 cs_zscore → 会让已评估过的截面因子结论不可复现

    要判断是否「对齐」，得先有产品决策，不能由实现顺手改。
    """
    panel = _panel({D1: [10.0, 20.0, 30.0, 40.0]})
    cs = eval_cross_section("cs_zscore", panel)["x"]

    _, post_default = apply_postprocess(panel["x"], {"zscore": {}})
    _, post_ddof0 = apply_postprocess(panel["x"], {"zscore": {"ddof": 0}})
    _, post_ddof1 = apply_postprocess(panel["x"], {"zscore": {"ddof": 1}})

    # 后处理不传 ddof 时等价于 ddof=0
    assert list(post_default) == list(post_ddof0)
    # cs_zscore 与后处理默认**不同**
    assert not np.allclose(cs.values, post_default.values)
    # cs_zscore 与后处理 ddof=1 **相同**
    assert np.allclose(cs.values, post_ddof1.values)


def test_cs_rank_keeps_nan():
    panel = pd.DataFrame(
        {"x": [10.0, np.nan, 30.0, 40.0]},
        index=pd.MultiIndex.from_product([[D1], SYMBOLS], names=["trade_date", "symbol"]),
    )
    out = eval_cross_section("cs_rank", panel)["x"]
    assert np.isnan(out.loc[(D1, "B")])


@pytest.mark.parametrize(
    "op", ["cs_rank", "cs_zscore", "cs_demean", "cs_scale"]
)
def test_eval_cross_section_dispatches_single_arg_ops(op):
    out = eval_cross_section(op, CLOSE)
    assert isinstance(out, pd.DataFrame)
    assert out.shape == CLOSE.shape


def test_eval_cross_section_requires_n_for_n_ops():
    with pytest.raises(ValueError):
        eval_cross_section("cs_quantile", CLOSE)
    with pytest.raises(ValueError):
        eval_cross_section("cs_winsorize", CLOSE)


def test_eval_cross_section_rejects_unknown_operator():
    with pytest.raises(ValueError):
        eval_cross_section("cs_not_a_real_op", CLOSE)


def test_cs_quantile_requires_at_least_two_groups():
    with pytest.raises(ValueError):
        eval_cross_section("cs_quantile", CLOSE, n=1)


def test_cs_quantile_produces_group_ids_within_range():
    out = eval_cross_section("cs_quantile", CLOSE, n=2)["x"]
    assert set(out.dropna().astype(int).unique()) <= {0, 1}


def test_cs_winsorize_clips_outlier():
    panel = pd.DataFrame(
        {"x": [1.0, 2.0, 3.0, 1000.0]},
        index=pd.MultiIndex.from_product([[D1], SYMBOLS], names=["trade_date", "symbol"]),
    )
    out = eval_cross_section("cs_winsorize", panel, n=3.0)["x"]
    assert out.loc[(D1, "D")] < 1000.0


# ══════════════════════════════════════════════════════════
# F. 与后处理 rank 可区分 + 后处理回归（DoD 第 3 条）
# ══════════════════════════════════════════════════════════


def test_cross_section_rank_differs_from_postprocess_rank():
    """★ DoD 关键区分：`cs_rank(A) - cs_rank(B)` 与「对 A−B 做后处理 rank」不是一回事。

    前者先各自截面排名再相减（结果落在 [-1, 1]），后者对差值整体排名（落在 [0, 1]）。
    """
    cs_diff = (
        eval_cross_section("cs_rank", CLOSE) - eval_cross_section("cs_rank", VOLUME)
    )["x"]

    raw_diff = CLOSE["x"] - VOLUME["x"]
    _, post_ranked = apply_postprocess(raw_diff, {"rank": {"method": "percentile", "ascending": True}})

    assert not np.allclose(cs_diff.values, post_ranked.values, equal_nan=True)
    assert float(cs_diff.min()) < 0.0, "公式内截面差应出现负值（后处理 rank 恒 ≥ 0）"
    assert float(post_ranked.min()) >= 0.0


def test_cross_section_rank_is_per_date_while_postprocess_rank_is_whole_series():
    """后处理 `apply_postprocess` 的 rank 作用于**整条序列**；
    截面算子作用于**每个交易日**。这是「语义不同」最直接的体现。

    （需要按日分组的后处理请用 `apply_postprocess_cross_sectional`。）
    """
    per_date = eval_cross_section("cs_rank", CLOSE)["x"]
    _, whole = apply_postprocess(
        CLOSE["x"], {"rank": {"method": "percentile", "ascending": True}}
    )
    assert per_date.loc[(D1, "A")] == pytest.approx(0.25)
    assert whole.loc[(D1, "A")] != pytest.approx(0.25), (
        "整条序列排名会把 D2 点也算进去（1/8=0.125），与按日截面不同"
    )


@pytest.mark.parametrize(
    "ascending,expected",
    [
        (True, [0.6, 0.3, 0.8, 0.3, 1.0]),
        (False, [0.6, 0.9, 0.4, 0.9, 0.2]),
    ],
)
def test_postprocess_rank_behavior_unchanged(ascending, expected):
    """★ 回归：后处理 rank 的既有行为不得被本次改动影响（T03 明确不做该语义变更）。"""
    series = pd.Series([3.0, 1.0, 4.0, 1.0, 5.0], index=list("abcde"))
    _, normalized = apply_postprocess(
        series, {"rank": {"method": "percentile", "ascending": ascending}}
    )
    assert [round(float(v), 6) for v in normalized] == expected


def test_postprocess_passthrough_when_config_is_none_unchanged():
    series = pd.Series([3.0, 1.0, 4.0, 1.0, 5.0], index=list("abcde"))
    winsorized, normalized = apply_postprocess(series, None)
    assert list(winsorized) == list(series)
    assert list(normalized) == list(series)


def test_postprocess_rank_config_still_accepted_by_compiler():
    """编译期仍接受后处理 rank 配置，且规范化结果不变。"""
    result = compile_formula(
        formula="close / sma(close, 20)",
        postprocess={"rank": {"method": "percentile", "ascending": True}},
    )
    assert result.success, [e.error_code for e in result.errors]
    assert result.execution_plan.postprocess == {
        "rank": {"ascending": True, "method": "percentile"}
    }


def test_postprocess_zscore_degenerate_is_zero_while_cs_zscore_is_nan():
    """两者退化处理**不同、并存、互不约束**（设计文档 §6.2）。

    后处理 `zscore` 在 std=0 时返回 0.0；公式内 `cs_zscore` 返回 NaN。
    这是一处**刻意的**不一致：后处理作用于最终因子值（必须产出可用数值），
    公式内截面算子参与进化适应度（无差异必须显式标为无效）。
    """
    flat_series = pd.Series([5.0, 5.0, 5.0], index=list("abc"))
    _, post = apply_postprocess(flat_series, {"zscore": {"ddof": 0}})
    assert [float(v) for v in post] == [0.0, 0.0, 0.0]

    cs = eval_cross_section("cs_zscore", _panel({D1: [5.0, 5.0, 5.0, 5.0]}))["x"]
    assert cs.isna().all()


# ══════════════════════════════════════════════════════════
# G. 沙箱：登记但诚实拒绝（不得静默返回 None）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("op", CS_OPS)
def test_sandbox_rejects_cross_section_ops_at_construction(op):
    """★ 沙箱是**逐标的**上下文，截面算子无法求值。

    要求：构造期显式 `ValueError`，**不是**静默返回 None。
    静默 None 会让高级筛选条件悄悄失效却不报错 —— 最危险的一类失败。
    """
    with pytest.raises(ValueError) as ei:
        IndicatorFormulaEvaluator(f"{op}(close)")
    message = str(ei.value)
    assert op in message
    assert "cross-section" in message, f"错误信息应说明原因，实得：{message}"


def test_sandbox_cross_section_placeholder_never_returns_a_value():
    """即使绕过 `_validate` 直接取占位实现，也必须失败而不是产出数值。"""
    func = ALLOWED_FUNCS["cs_rank"]
    with pytest.raises(ValueError):
        func(1.0)


def test_sandbox_still_accepts_pre_existing_functions():
    """既有 9 个函数不受影响。"""
    for formula, expected in [
        ("abs(-5)", 5),
        ("10 - 3", 7),
        ("min(3, 5)", 3),
        ("round(3.14159)", 3),
    ]:
        assert IndicatorFormulaEvaluator(formula).evaluate({}) == expected

    from app.services.indicator_ast_sandbox import ALLOWED_FUNCS as funcs

    for name in ("abs", "min", "max", "round", "sum", "len", "log", "sqrt", "exp"):
        assert name in funcs, f"既有函数 {name} 被误删"
        assert name not in CROSS_SECTION_ONLY_FUNCS


def test_sandbox_unknown_function_message_unchanged():
    """未登记函数仍报 unsupported function（与截面算子的报错可区分）。"""
    with pytest.raises(ValueError) as ei:
        IndicatorFormulaEvaluator("totally_unknown_fn(close)")
    assert "unsupported function" in str(ei.value)


# ══════════════════════════════════════════════════════════
# H. 与字段目录无冲突
# ══════════════════════════════════════════════════════════


def test_cross_section_ops_do_not_shadow_field_names():
    """算子名不得与字段目录撞名（撞名会让 `cs_rank` 被解析成字段）。"""
    overlap = sorted(set(CROSS_SECTION_OPS) & set(FIELD_CATALOG))
    assert not overlap, f"算子名与字段名冲突: {overlap}"


def test_disabled_placeholders_untouched():
    """`_DISABLED_FUNCTIONS` 保留原有 3 条占位（清理属 T06；前端测试依赖 rank_cs 占位）。"""
    catalog = build_formula_catalog(None)
    keys = [d["key"] for d in catalog["disabled_functions"]]
    assert keys == ["rank_cs", "winsorize", "neutralize"]
