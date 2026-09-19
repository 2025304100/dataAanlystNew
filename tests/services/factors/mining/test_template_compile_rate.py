"""T06 · 字段注册（+3）与 25 个经典模板编译率（G1 门禁）。

本文件是 **G1 门禁**的可执行证据，覆盖三件事：

1. **25 个经典模板的编译率 ≥ 90%**
   模板清单与参数范围取自《因子挖掘实验向导-详细设计.md》§6.3.3（25 个系统预设模板）。
   其中 2 个模板依赖尚未采集的字段（`gross_margin` / `asset_turnover`，M2），
   按该节的明确要求**先禁用且不计入分母** → 分母 = 23。

   ⚠️ 分母口径不是小事：需求 §6.3.3 注写明这 2 个模板「用户未选相关字段时自动跳过，
   不报错」。若把它们计入分母，单它们就把上限压到 92%，再有一个编译失败就破线，
   门禁会变成噪声而非信号。

2. **字段注册 +3**：`dividend_yield` / `total_market_cap` / `circulating_market_cap`
   （数据在 `raw_valuation_snapshots`，零采集）。

3. **`prev_close` 保活哨兵**（重要）。
   历史上一度决定「移出 `FIELD_CATALOG`」，前提是「`app/services/factors/` 内无任何派生实现」。
   该前提**是错的**：`FactorExecutor._select_field_expressions`（`factor_executor.py:679-681`）
   会把它投影成 `LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close`，
   两个取数点（`:891` / `:1352`）都走这条投影 —— 它是**可用的虚字段**。
   2026-09-16 在真实数仓端到端实测：`000001` 在 `2026-07-21..24` 的 `prev_close`
   = `10.98 / 10.84 / 10.98 / 11.08`，与上一交易日收盘价逐条相等。
   本文件的哨兵用于防止该错误结论被再次执行。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import pytest

import app.services.factors.formula_catalog as formula_catalog
from app.services.factors.factor_compiler import (
    FIELD_CATALOG,
    compile_formula,
)
from app.services.factors.factor_executor import FactorExecutor


# ══════════════════════════════════════════════════════════════
# 25 个经典模板（向导 §6.3.3 原文转录；勿凭记忆改动）
# ══════════════════════════════════════════════════════════════

COMPILE_RATE_FLOOR = 0.90


@dataclass(frozen=True)
class Template:
    key: str
    name_zh: str
    category: str
    formula: str
    params: dict[str, tuple[int, ...]] = field(default_factory=dict)
    disabled_reason: str | None = None


TEMPLATES: tuple[Template, ...] = (
    # ── 趋势类（5）──
    Template(
        "ma_trend", "均线趋势", "trend",
        "mean(close,{n1})/mean(close,{n2})-1",
        {"n1": (5, 10, 20), "n2": (20, 60, 120)},
    ),
    Template("price_momentum", "价格动量", "trend",
             "ts_delta_bars(close,{n1})", {"n1": (10, 20, 60)}),
    Template("channel_breakout", "通道突破", "trend",
             "(close-lowest(low,{n1}))/(highest(high,{n1})-lowest(low,{n1}))",
             {"n1": (20, 60)}),
    Template("macd", "MACD", "trend", "ema(close,{n1})-ema(close,{n2})",
             {"n1": (12,), "n2": (26,)}),
    Template("ma_slope", "均线斜率", "trend",
             "(mean(close,{n1})-mean(close,{n2}))/{n2}", {"n1": (5, 10), "n2": (20, 60)}),
    # ── 反转类（4）──
    Template("short_reversal", "短期反转", "reversal",
             "-ts_delta_bars(close,{n1})", {"n1": (5, 10)}),
    Template("overbought_oversold", "超买超卖", "reversal",
             "(close-lowest(low,{n1}))/(highest(high,{n1})-lowest(low,{n1}))-0.5",
             {"n1": (14, 20)}),
    Template("mean_reversion", "均值回归", "reversal",
             "-(close/mean(close,{n1})-1)", {"n1": (20, 60)}),
    Template("price_volume_divergence", "量价背离(反转)", "reversal",
             "-ts_delta_bars(close,{n1}) * cs_rank(volume)", {"n1": (5, 10)}),
    # ── 波动率类（4）──
    Template("volatility_contraction", "波动率收缩", "volatility",
             "stddev(close,{n1})/mean(stddev(close,{n2}),{n3})",
             {"n1": (5, 10), "n2": (20, 60), "n3": (20, 60)}),
    Template("volatility_breakout", "波动率突破", "volatility",
             "stddev(close,{n1})/mean(stddev(close,{n2}),{n3})",
             {"n1": (5, 10), "n2": (20, 60), "n3": (20, 60)}),
    Template("atr_ratio", "ATR比率", "volatility", "ts_atr(close,{n1})/close", {"n1": (14, 20)}),
    Template("bollinger_position", "布林带位置", "volatility",
             "(close-mean(close,{n1}))/(2*stddev(close,{n1}))", {"n1": (20,)}),
    # ── 估值类（4）──
    Template("pe_reversal", "PE反转", "valuation", "-cs_rank(pe_ttm)"),
    Template("pb_reversal", "PB反转", "valuation", "-cs_rank(pb)"),
    Template("valuation_spread", "估值比价", "valuation", "cs_rank(pe_ttm)-cs_rank(pb)"),
    Template("earnings_yield", "盈利收益率", "valuation", "cs_rank(1/pe_ttm)"),
    # ── 质量类（4）──
    Template("roe_change", "ROE变化", "quality", "ts_delta_periods(roe_ttm,{n1})",
             {"n1": (1, 2, 4)}),
    Template("earnings_acceleration", "盈利加速度", "quality",
             "ts_delta_periods(roe_ttm,{n1})-ts_delta_periods(roe_ttm,{n2})",
             {"n1": (1,), "n2": (2, 4)}),
    Template("gross_margin_change", "毛利率变化", "quality",
             "ts_delta_periods(gross_margin,{n1})", {"n1": (1, 2, 4)},
             disabled_reason="gross_margin 数据未采集（M2 采集）"),
    Template("asset_turnover_change", "资产周转率", "quality",
             "ts_delta_periods(asset_turnover,{n1})", {"n1": (1, 2, 4)},
             disabled_reason="asset_turnover 数据未采集（M2 采集）"),
    # ── 量价类（4）──
    Template("turnover_anomaly", "换手率异常", "volume_price",
             "cs_rank(turnover_rate)-cs_rank(mean(turnover_rate,{n1}))", {"n1": (20, 60)}),
    Template("volume_trend", "成交量趋势", "volume_price",
             "mean(volume,{n1})/mean(volume,{n2})-1", {"n1": (5, 10), "n2": (20, 60)}),
    Template("volume_price_divergence_2", "量价背离(量价)", "volume_price",
             "cs_rank(volume) - cs_rank(ts_delta_bars(close,{n1}))", {"n1": (5, 10)}),
    Template("money_flow_strength", "资金流强度", "volume_price",
             "amount/mean(amount,{n1})", {"n1": (20, 60)}),
)

ENABLED = tuple(t for t in TEMPLATES if t.disabled_reason is None)
DISABLED = tuple(t for t in TEMPLATES if t.disabled_reason is not None)


def render(template: Template, combo: dict[str, int]) -> str:
    out = template.formula
    for name, value in combo.items():
        out = out.replace("{" + name + "}", str(value))
    return out


def combos(template: Template) -> list[dict[str, int]]:
    if not template.params:
        return [{}]
    names = list(template.params)
    return [
        dict(zip(names, values))
        for values in itertools.product(*(template.params[n] for n in names))
    ]


# ══════════════════════════════════════════════════════════════
# 1. 模板清单口径
# ══════════════════════════════════════════════════════════════


def test_template_inventory_is_25_across_six_categories():
    """向导 §6.3.3 明写「25 个系统预设模板，按 6 类分组」。"""
    assert len(TEMPLATES) == 25
    counts: dict[str, int] = {}
    for t in TEMPLATES:
        counts[t.category] = counts.get(t.category, 0) + 1
    assert counts == {
        "trend": 5, "reversal": 4, "volatility": 4,
        "valuation": 4, "quality": 4, "volume_price": 4,
    }, counts


def test_exactly_two_templates_are_disabled_for_uncollected_fields():
    """禁用必须是**且仅是**未采集字段造成的，且理由写在卡片上。"""
    assert {t.key for t in DISABLED} == {"gross_margin_change", "asset_turnover_change"}
    for t in DISABLED:
        assert t.disabled_reason
        assert "未采集" in (t.disabled_reason or "")


def test_disabled_templates_really_lack_their_fields():
    """禁用不是偷懒：这两条公式在**严格字段模式**下必须真的编译失败。

    如果哪天 `gross_margin` 被注册了，本断言会变红 —— 那正是「该启用这两个模板了」的信号。
    """
    for t in DISABLED:
        src = render(t, {"n1": 1})
        result = compile_formula(formula=src, strict_fields=True)
        assert result.success is False, f"{src} 竟然编译通过了，禁用理由已失效"
        assert any(e.error_code == "field_not_in_catalog" for e in result.errors)


# ══════════════════════════════════════════════════════════════
# 2. 编译率（DoD 本体）
# ══════════════════════════════════════════════════════════════


def test_compile_rate_meets_gate_for_every_parameter_combo():
    """穷举全部声明过的参数组合，整体编译率必须 ≥ 90%。

    口径：**分母 = 23 个启用模板**（2 个禁用模板不计入，见模块 docstring 第 1 点）。
    组合级（更严格）与模板级（全组合通过才算该模板通过）同时断言，并打印实测值供 `-s` 查看。
    """
    per_template: list[tuple[str, int, int]] = []
    combo_ok = combo_total = 0
    failures: list[str] = []

    for t in ENABLED:
        t_ok = 0
        variants = combos(t)
        for combo in variants:
            src = render(t, combo)
            result = compile_formula(formula=src)
            if result.success:
                t_ok += 1
            else:
                failures.append(f"{src} -> {sorted(e.error_code for e in result.errors)}")
        per_template.append((t.name_zh, t_ok, len(variants)))
        combo_ok += t_ok
        combo_total += len(variants)

    fully_ok = [name for name, ok, total in per_template if ok == total]
    variant_rate = combo_ok / combo_total
    template_rate = len(fully_ok) / len(ENABLED)

    print()
    print("=" * 66)
    print("25 个经典模板 · 编译率实测（向导 §6.3.3）")
    print("=" * 66)
    print(f"  模板总数 {len(TEMPLATES)} | 启用 {len(ENABLED)} | 禁用 {len(DISABLED)}（不计入分母）")
    for name, ok, total in per_template:
        print(f"    {'OK ' if ok == total else 'BAD'} {name:16s} {ok:2d}/{total:2d}")
    print("-" * 66)
    print(f"  参数组合级编译率 : {combo_ok}/{combo_total} = {100 * variant_rate:.1f}%")
    print(f"  模板级编译率     : {len(fully_ok)}/{len(ENABLED)} = {100 * template_rate:.1f}%")
    print(f"  门槛             : >= {100 * COMPILE_RATE_FLOOR:.0f}%")
    print("=" * 66)

    assert not failures, "以下变体编译失败：\n  " + "\n  ".join(failures)
    assert variant_rate >= COMPILE_RATE_FLOOR
    assert template_rate >= COMPILE_RATE_FLOOR


def test_compile_rate_holds_in_strict_field_mode():
    """严格字段模式（执行/预检路径）下同样必须全部可编译。

    非严格模式只校验语法；执行路径用 `strict_fields=True`，两者口径要一致，
    否则会出现「编辑器里能存、跑起来报字段不存在」。
    """
    for t in ENABLED:
        for combo in combos(t):
            src = render(t, combo)
            result = compile_formula(formula=src, strict_fields=True)
            assert result.success, f"{src} -> {sorted(e.error_code for e in result.errors)}"


def test_templates_depend_only_on_registered_fields():
    """每条模板的字段依赖都必须落在 `FIELD_CATALOG` 内 —— 这是编译率能达标的根因。"""
    for t in ENABLED:
        first = {k: v[0] for k, v in t.params.items()}
        result = compile_formula(formula=render(t, first))
        assert result.success
        for name in result.execution_plan.data_dependencies["fields"]:
            assert name in FIELD_CATALOG, f"{t.name_zh} 依赖未注册字段 {name}"


# ══════════════════════════════════════════════════════════════
# 3. 字段注册 +3（M1a）
# ══════════════════════════════════════════════════════════════

NEW_FIELDS = ("dividend_yield", "total_market_cap", "circulating_market_cap")


@pytest.mark.parametrize("name", NEW_FIELDS)
def test_new_valuation_fields_are_registered(name: str):
    spec = FIELD_CATALOG[name]
    assert spec.source_table == "raw_valuation_snapshots"
    assert spec.point_in_time is True, "估值快照必须走 PIT，禁止向前填充"
    assert spec.layer == "A"
    assert spec.dtype == "float"


@pytest.mark.parametrize("name", NEW_FIELDS)
def test_new_fields_are_compilable_and_declare_dependencies(name: str):
    result = compile_formula(formula=name, strict_fields=True)
    assert result.success
    assert result.execution_plan.data_dependencies["fields"] == [name]

    ranked = compile_formula(formula=f"cs_rank({name})", strict_fields=True)
    assert ranked.success


@pytest.mark.parametrize("name", NEW_FIELDS)
def test_new_fields_have_english_label_and_data_policy(name: str):
    """目录完整性：缺 label 或 policy 会让公式目录 API 少露出/判错状态。

    与 `test_dsl_cross_section.py::test_function_meta_covers_every_catalog_function`
    同属一类通用哨兵 —— 拦的不是本次，而是**将来**任何只加字段不加元数据的人。
    """
    assert name in formula_catalog._FIELD_LABELS_EN
    assert name in formula_catalog._FIELD_DATA_POLICIES


def test_every_catalog_field_is_fully_described():
    """通用哨兵：`FIELD_CATALOG` 与标签表/策略表必须**完全一致**（双向）。"""
    catalog = set(FIELD_CATALOG)
    assert catalog == set(formula_catalog._FIELD_LABELS_EN)
    assert catalog == set(formula_catalog._FIELD_DATA_POLICIES)


# ══════════════════════════════════════════════════════════════
# 4. dividend_yield 的数据现状必须被诚实标注
# ══════════════════════════════════════════════════════════════

# 2026-09-16 实测 tmp/factor_warehouse.duckdb → raw_valuation_snapshots
REAL_TABLE_ROWS = 3_079_528


def test_dividend_yield_column_exists_but_is_empty_so_it_must_report_blocked():
    """需求 §2.3 写「数据已存在（308 万行）」，但那是**表行数**：

    实测该表 3,079,528 行中 `dividend_yield` 非空 **0 行**（列存在，从未采集）。
    而 `total_market_cap` / `circulating_market_cap` 非空 100%。

    所以前者必须被判 blocked（否则又是一个「能编译但产出全 NaN」的字段），
    且**不能**写死 `data_mode=blocked` —— 那会让补数后仍永久阻断。
    """
    empty = formula_catalog._field_capability(
        "dividend_yield",
        {"table_rows": REAL_TABLE_ROWS, "nonnull_rows": 0, "distinct_dates": 100},
    )
    assert empty["availability"] == "blocked"
    assert empty["evaluation_enabled"] is False
    assert empty["preview_enabled"] is False

    filled = formula_catalog._field_capability(
        "dividend_yield",
        {"table_rows": REAL_TABLE_ROWS, "nonnull_rows": REAL_TABLE_ROWS, "distinct_dates": 100},
    )
    assert filled["availability"] == "limited", "补数后应自动恢复为覆盖受限，无需改代码"
    assert filled["evaluation_enabled"] is True

    assert formula_catalog._FIELD_DATA_POLICIES["dividend_yield"]["data_mode"] != "blocked", (
        "写死 blocked 会让补数后仍永久阻断；应交给覆盖率检测"
    )


@pytest.mark.parametrize("name", ("total_market_cap", "circulating_market_cap"))
def test_populated_valuation_fields_are_limited_not_blocked(name: str):
    capability = formula_catalog._field_capability(
        name,
        {"table_rows": REAL_TABLE_ROWS, "nonnull_rows": REAL_TABLE_ROWS, "distinct_dates": 100},
    )
    assert capability["availability"] == "limited"
    assert capability["evaluation_enabled"] is True


# ══════════════════════════════════════════════════════════════
# 5. prev_close 保活哨兵（防止「撤销注册」的错误结论被再次执行）
# ══════════════════════════════════════════════════════════════

_PREV_CLOSE_SQL = "LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close"


def test_prev_close_stays_registered():
    """它**不是**虚注册。历史上判它「无派生实现」的结论来自一次被截断的搜索。"""
    assert "prev_close" in FIELD_CATALOG
    assert FIELD_CATALOG["prev_close"].source_table == "raw_daily_bars"


def test_executor_projects_prev_close_via_lag():
    """派生实现就在执行器的 SELECT 投影里 —— 这是「可用」的物证。"""
    fields = FactorExecutor._select_field_expressions("raw_daily_bars", ["close", "prev_close"])
    assert fields == ["close", _PREV_CLOSE_SQL]


def test_prev_close_projection_only_applies_to_daily_bars():
    """只有日线表才派生；其它表不得凭空造列（否则会静默造假数据）。"""
    fields = FactorExecutor._select_field_expressions(
        "raw_valuation_snapshots", ["pe_ttm", "prev_close"]
    )
    assert fields == ["pe_ttm", "prev_close"]


def test_prev_close_is_an_available_derived_field():
    capability = formula_catalog._field_capability(
        "prev_close",
        {"table_rows": REAL_TABLE_ROWS, "nonnull_rows": REAL_TABLE_ROWS,
         "distinct_dates": 100, "derived": True},
    )
    assert capability["availability"] == "available"
    assert capability["evaluation_enabled"] is True
    assert capability["derived"] is True
    assert capability["derived_from"] == ["close"]


def test_formula_using_prev_close_compiles_and_keeps_its_dependency():
    """`close / prev_close - 1` 是 DSL 用例书 DSL-001 的原文，必须可编译。"""
    result = compile_formula(formula="close / prev_close - 1", strict_fields=True)
    assert result.success
    assert set(result.execution_plan.data_dependencies["fields"]) == {"close", "prev_close"}
    assert result.execution_plan.content_hash() == "b0bba3ea95362e90"


def test_ts_atr_does_not_depend_on_prev_close():
    """T04 把 ATR 实现为 close-to-close（内部 shift(1)），所以与 prev_close 解耦。

    这条保证「即使将来 prev_close 真的被移除，ATR 仍不受影响」，把两件事的耦合切断。
    """
    result = compile_formula(formula="ts_atr(close, 14)", strict_fields=True)
    assert result.success
    assert result.execution_plan.data_dependencies["fields"] == ["close"]


# ══════════════════════════════════════════════════════════════
# 6. 去重身份键未受影响（新增字段不得改动既有公式 hash）
# ══════════════════════════════════════════════════════════════

_SANITY_FORMULAS = (
    "stddev(close, 20) / mean(close, 20)",
    "close / sma(close, 20) - 1",
    "cs_rank(pe_ttm) - cs_rank(pb)",
)


def test_new_field_registration_does_not_change_existing_hashes():
    """`execution_plan_hash` 是 `factor_versions` 的去重身份键。

    新增字段只扩 `FIELD_CATALOG`，不得改动任何既有公式的序列化结果 ——
    否则同一公式会被判为新版本，因子库里出现孪生条目。
    """
    # 用「摘掉新字段再编译」作为对照，严格隔离本次改动的影响
    saved = {name: FIELD_CATALOG.pop(name) for name in NEW_FIELDS}
    try:
        before = {
            f: compile_formula(formula=f).execution_plan.content_hash()
            for f in _SANITY_FORMULAS
        }
    finally:
        FIELD_CATALOG.update(saved)

    after = {
        f: compile_formula(formula=f).execution_plan.content_hash()
        for f in _SANITY_FORMULAS
    }
    assert before == after, f"新增字段改变了既有公式的 content_hash：{before} -> {after}"
