"""T17 · initial_population 经典底座 + 6 类归类 契约测试（DoD）。

覆盖
====
- **归类优先级**（任务卡硬规则）：quality > valuation > volatility >
  volume_price > reversal > trend；多类命中取最高；声明类别不得越权
- **25 模板**：数量/分布/2 条禁用/方言归一（无旧算子名）
- **网格展开**：笛卡尔积、保序、上限截断
- **过滤**：字段缺失跳过（不报错）、类别未启用跳过、禁用模板跳过
- **校验**：真实编译器冒烟 + 注入假编译器（配额测试要确定性）
- **名额分配**：均衡覆盖基础名额 + 余数 + **缺口流转**；优先级覆盖排序
- **上限**：默认 40、绝对 40、种群 60% 硬约束（不静默）
- **端到端**：候选字段契约（operation=enumerated / generation=0）
"""
from __future__ import annotations

import pytest

from app.services.factors.mining import category as CAT
from app.services.factors.mining import initial_population as IP


# ══════════════════════════════════════════════════════════
# 假编译器（配额类测试要确定性，不依赖真编译）
# ══════════════════════════════════════════════════════════


class _FakePlan:
    def __init__(self, formula: str, params: dict) -> None:
        self.formula = formula
        self.params = params
        self.complexity_score = float(len(formula))
        self.node_count = formula.count("(") + 1
        self.ast_depth = 2
        self.max_lookback = max([int(v) for v in params.values()] or [1])
        self.direction = "higher_better"
        self.compiler_version = "fake-1"


class _FakeResult:
    def __init__(self, formula: str, params: dict, *, ok: bool = True) -> None:
        self.success = ok
        self.execution_plan = _FakePlan(formula, params) if ok else None
        self.errors = [] if ok else [type("E", (), {
            "error_code": "fake.boom", "message": "injected failure"})()]


def fake_compiler(*, fail_contains: str | None = None):
    def _compile(*, formula: str, params: dict | None = None):
        p = dict(params or {})
        ok = not (fail_contains and fail_contains in formula)
        return _FakeResult(formula, p, ok=ok)
    return _compile


# ══════════════════════════════════════════════════════════
# 1. 归类优先级（任务卡硬规则）
# ══════════════════════════════════════════════════════════


class TestCategoryPriority:
    def test_priority_order_is_contract(self):
        assert CAT.CATEGORY_PRIORITY == (
            "quality", "valuation", "volatility", "volume_price",
            "reversal", "trend")
        assert CAT.priority_of("quality") < CAT.priority_of("trend")

    def test_single_field_hits(self):
        assert CAT.classify(fields=["roe_ttm"]).category == "quality"
        assert CAT.classify(fields=["pe_ttm"]).category == "valuation"
        assert CAT.classify(fields=["turnover_rate"]).category == "volume_price"
        assert CAT.classify(fields=["close"]).category == "trend"
        assert CAT.classify(functions=["stddev"]).category == "volatility"

    def test_quality_beats_valuation(self):
        c = CAT.classify(fields=["roe_ttm", "pe_ttm"])
        assert c.category == "quality"
        assert set(c.all_matches) == {"quality", "valuation"}

    def test_valuation_beats_volatility_and_volume_price(self):
        c = CAT.classify(fields=["pe_ttm", "volume"], functions=["stddev"])
        assert c.category == "valuation"

    def test_volatility_beats_volume_price(self):
        c = CAT.classify(fields=["volume", "close"], functions=["ts_atr"])
        assert c.category == "volatility"

    def test_volume_price_beats_reversal_and_trend(self):
        # 量价背离模板：负号（反转）+ volume（量价）+ close（趋势）
        c = CAT.classify(fields=["close", "volume"],
                         functions=["ts_delta_bars", "cs_rank"], negated=True)
        assert c.category == "volume_price"
        assert "reversal" in c.all_matches and "trend" in c.all_matches

    def test_reversal_beats_trend(self):
        c = CAT.classify(fields=["close"], negated=True)
        assert c.category == "reversal"

    def test_all_matches_sorted_by_priority(self):
        c = CAT.classify(fields=["roe_ttm", "pb", "volume", "close"],
                         functions=["stddev"], negated=True)
        assert list(c.all_matches) == sorted(
            c.all_matches, key=CAT.priority_of)
        assert c.category == c.all_matches[0]

    def test_declared_cannot_override_priority(self):
        """声明类别只在**无任何命中**时兜底 —— 否则配额会被声明操纵。"""
        c = CAT.classify(fields=["close"], declared="quality")
        assert c.category == "trend"          # 不被声明骗到 quality

    def test_declared_used_when_no_hit(self):
        c = CAT.classify(fields=["unknown_field_xyz"], declared="quality")
        assert c.category == "quality"
        assert c.matched_rule == "declared_fallback"

    def test_default_fallback_is_trend(self):
        c = CAT.classify(fields=["unknown_field_xyz"])
        assert c.category == "trend"
        assert c.matched_rule == "default_fallback"

    def test_classification_carries_reason(self):
        c = CAT.classify(fields=["roe_ttm"])
        assert "roe_ttm" in c.reason_zh
        assert c.to_dict()["category_label_zh"] == "质量"

    def test_classify_candidate_helper(self):
        assert CAT.classify_candidate({"fields": ["pb"]}) == "valuation"
        assert CAT.classify_candidate({"required_fields": ["volume"]}) == "volume_price"

    def test_count_by_category(self):
        items = [{"fields": ["roe_ttm"]}, {"fields": ["pb"]}, {"fields": ["close"]}]
        counts = CAT.count_by_category(items)
        assert counts["quality"] == 1 and counts["valuation"] == 1
        assert counts["trend"] == 1


# ══════════════════════════════════════════════════════════
# 2. 25 模板数据
# ══════════════════════════════════════════════════════════


class TestTemplateCatalog:
    def test_exactly_25_templates(self):
        assert IP.TEMPLATE_COUNT == 25

    def test_category_distribution(self):
        dist: dict[str, int] = {}
        for t in IP.CLASSIC_TEMPLATES:
            dist[t.category] = dist.get(t.category, 0) + 1
        assert dist == {"trend": 5, "reversal": 4, "volatility": 4,
                        "valuation": 4, "quality": 4, "volume_price": 4}

    def test_two_quality_templates_disabled(self):
        """gross_margin / asset_turnover 数据未接入（M2）→ 默认禁用。"""
        disabled = {t.name for t in IP.CLASSIC_TEMPLATES if not t.enabled}
        assert disabled == {"毛利率变化", "资产周转率"}

    def test_names_unique(self):
        names = [t.name for t in IP.CLASSIC_TEMPLATES]
        assert len(names) == len(set(names))

    def test_every_template_has_economy_logic(self):
        for t in IP.CLASSIC_TEMPLATES:
            assert t.economy_logic_zh, t.name

    def test_no_legacy_operator_names(self):
        """v3.2 方言归一：不得残留**裸**旧算子名。

        ⚠️ 不能用 `"rank(" not in formula` —— `cs_rank(` 会命中子串，
        归一后的正确写法被误判。要用「前面不是 cs_/ts_ 下划线标识符」的边界。
        """
        import re
        legacy = ("delta", "llv", "hhv", "std", "atr", "rank", "turnover(?!_rate)")
        for tpl in IP.CLASSIC_TEMPLATES:
            low = tpl.formula.lower()
            for bad in legacy:
                # 允许 cs_xxx / ts_xxx 前缀（归一后的正确算子）；turnover 只允许 turnover_rate
                pat = rf"(?<![a-z_]){bad}\("
                assert not re.search(pat, low), \
                    f"{tpl.name} 残留旧算子 {bad}(: {tpl.formula}"
            # turnover 只允许带 _rate
            assert "turnover(" not in low, f"{tpl.name} 应为 turnover_rate"

    def test_grid_size_matches_param_dimensions(self):
        tpl = next(t for t in IP.CLASSIC_TEMPLATES if t.name == "均线趋势")
        assert tpl.grid_size() == 3 * 3
        bare = next(t for t in IP.CLASSIC_TEMPLATES if t.name == "PE反转")
        assert bare.grid_size() == 1


# ══════════════════════════════════════════════════════════
# 3. 网格展开
# ══════════════════════════════════════════════════════════


class TestExpand:
    def test_expand_cartesian_and_ordered(self):
        tpl = next(t for t in IP.CLASSIC_TEMPLATES if t.name == "MACD")
        rows = IP.expand_template(tpl)
        assert len(rows) == 1
        assert rows[0].params == {"n1": 12, "n2": 26}

    def test_expand_grid_count_and_substitution(self):
        tpl = next(t for t in IP.CLASSIC_TEMPLATES if t.name == "价格动量")
        rows = IP.expand_template(tpl)
        assert [r.params["n1"] for r in rows] == [10, 20, 60]
        assert all("{" not in r.formula for r in rows)
        assert rows[0].formula == "ts_delta_bars(close,10)"

    def test_expand_stable_across_calls(self):
        tpl = next(t for t in IP.CLASSIC_TEMPLATES if t.name == "均线趋势")
        a = [(r.formula, r.param_index) for r in IP.expand_template(tpl)]
        b = [(r.formula, r.param_index) for r in IP.expand_template(tpl)]
        assert a == b

    def test_expand_cap(self):
        tpl = IP.ClassicTemplate(
            name="爆炸模板", category="trend", formula="mean(close,{n1})",
            params={"n1": tuple(range(200))}, required_fields=("close",),
            economy_logic_zh="测试用")
        rows = IP.expand_template(tpl)
        assert len(rows) == IP.MAX_GRID_PER_TEMPLATE


# ══════════════════════════════════════════════════════════
# 4. 过滤
# ══════════════════════════════════════════════════════════


class TestFiltering:
    def test_missing_field_skips_template_without_error(self):
        cands, skipped = IP.build_candidates(
            selected_fields=["close"], enabled_categories=["quality"],
            templates=[t for t in IP.CLASSIC_TEMPLATES
                       if t.category == "quality"])
        assert cands == []
        reasons = {s.reason for s in skipped}
        # ROE 模板在跳过列表里（需要 roe_ttm）
        assert IP.SKIP_FIELD_MISSING in reasons

    def test_selected_field_enables_template(self):
        cands, skipped = IP.build_candidates(
            selected_fields=["roe_ttm"], enabled_categories=["quality"])
        names = {c["template_name"] for c in cands}
        assert "ROE变化" in names and "盈利加速度" in names
        # 禁用模板仍被跳过
        assert any(s.reason == IP.SKIP_TEMPLATE_DISABLED for s in skipped)

    def test_category_filter(self):
        cands, skipped = IP.build_candidates(
            selected_fields=["close", "pe_ttm"], enabled_categories=["valuation"])
        assert {c["category"] for c in cands} == {"valuation"}
        assert any(s.reason == IP.SKIP_CATEGORY_DISABLED for s in skipped)

    def test_no_selected_fields_means_no_field_filter(self):
        cands, _skipped = IP.build_candidates(selected_fields=None)
        assert len(cands) > 30

    def test_candidate_carries_category_reason(self):
        cands, _ = IP.build_candidates(
            selected_fields=["pe_ttm", "pb"], enabled_categories=["valuation"])
        assert all(c["category_reason_zh"] for c in cands)


# ══════════════════════════════════════════════════════════
# 5. 校验（可注入编译器）
# ══════════════════════════════════════════════════════════


class TestValidation:
    def test_injected_compiler_marks_ok(self):
        cands, _ = IP.build_candidates(selected_fields=["close"],
                                       enabled_categories=["trend"])
        ok, errors = IP.validate_candidates(cands, compiler=fake_compiler())
        assert errors == []
        assert all(c["compile_ok"] and c["canonical_formula"] for c in ok)
        assert all("complexity" in c and "max_lookback" in c for c in ok)

    def test_injected_compiler_reports_failures(self):
        cands, _ = IP.build_candidates(selected_fields=["close"],
                                       enabled_categories=["reversal"])
        ok, errors = IP.validate_candidates(
            cands, compiler=fake_compiler(fail_contains="-"))
        assert errors and all(e["errors"] for e in errors)

    def test_real_compiler_smoke(self):
        """真实编译器：25 模板的归一后公式必须全部可编译（T06 交付的标准）。"""
        cands, _ = IP.build_candidates(
            selected_fields=["close", "high", "low", "volume", "amount",
                             "turnover_rate", "pe_ttm", "pb", "roe_ttm"])
        ok, errors = IP.validate_candidates(cands)
        assert errors == [], f"存在不可编译模板: {errors[:2]}"
        assert len(ok) == len(cands)


# ══════════════════════════════════════════════════════════
# 6. 名额分配：均衡覆盖 + 缺口流转
# ══════════════════════════════════════════════════════════


def _rows(category: str, n: int, *, priority: int = 1) -> list[dict]:
    return [{"category": category, "priority": priority, "complexity": float(i),
             "param_index": i, "formula": f"{category}-{i}"} for i in range(n)]


class TestAllocation:
    def test_balanced_base_quota(self):
        pools = {c: _rows(c, 10) for c in CAT.CATEGORY_PRIORITY}
        picked, unselected = IP.allocate_balanced(pools, limit=12)
        assert len(picked) == 12
        # 6 类各 2 个（12 // 6 = 2，余数 0）
        counts = {}
        for r in picked:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        assert set(counts.values()) == {2}

    def test_gap_transfer_when_category_short(self):
        """🚨 任务卡硬规则：某类候选不足时缺口流转给其他类。"""
        pools = {
            "quality": _rows("quality", 1),        # 只有 1 个
            "valuation": _rows("valuation", 1),
            "volatility": _rows("volatility", 20),
            "volume_price": _rows("volume_price", 20),
            "reversal": _rows("reversal", 20),
            "trend": _rows("trend", 20),
        }
        picked, _un = IP.allocate_balanced(pools, limit=30)
        assert len(picked) == 30, "缺口未被流转 → 名额浪费"
        counts = {}
        for r in picked:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        assert counts["quality"] == 1 and counts["valuation"] == 1
        # 缺口按优先级流转：volatility 先于 volume_price 先于 reversal 先于 trend
        assert counts["volatility"] >= counts["trend"]

    def test_balanced_respects_total_available(self):
        pools = {"trend": _rows("trend", 3), "quality": _rows("quality", 2)}
        picked, unselected = IP.allocate_balanced(pools, limit=50)
        assert len(picked) == 5
        assert unselected == []

    def test_balanced_zero_limit(self):
        pools = {c: _rows(c, 5) for c in CAT.CATEGORY_PRIORITY}
        picked, unselected = IP.allocate_balanced(pools, limit=0)
        assert picked == [] and len(unselected) == 30

    def test_balanced_empty_pools(self):
        assert IP.allocate_balanced({}, limit=10) == ([], [])

    def test_balanced_sorted_within_category(self):
        pools = {"trend": [
            {"category": "trend", "priority": 3, "complexity": 1.0, "param_index": 0},
            {"category": "trend", "priority": 1, "complexity": 9.0, "param_index": 1},
            {"category": "trend", "priority": 1, "complexity": 2.0, "param_index": 2},
        ]}
        picked, _ = IP.allocate_balanced(pools, limit=3)
        assert [(r["priority"], r["complexity"]) for r in picked] == [
            (1, 2.0), (1, 9.0), (3, 1.0)]

    def test_priority_strategy_orders_by_category_priority(self):
        rows = (_rows("trend", 3) + _rows("quality", 3) + _rows("volume_price", 3))
        picked, unselected = IP.allocate_priority(rows, limit=4)
        assert len(picked) == 4 and len(unselected) == 5
        cats = [r["category"] for r in picked]
        # quality 最优先 → 前 3 个都是 quality，第 4 个是 volume_price
        assert cats[:3] == ["quality"] * 3
        assert cats[3] == "volume_price"

    def test_priority_strategy_zero_limit(self):
        assert IP.allocate_priority(_rows("trend", 3), limit=0) == ([], _rows("trend", 3))


# ══════════════════════════════════════════════════════════
# 7. 上限解析
# ══════════════════════════════════════════════════════════


class TestTemplateLimit:
    def test_default_is_40(self):
        limit, notes = IP.resolve_template_limit(population_size=200)
        assert limit == 40 and notes == []

    def test_population_share_cap_is_not_silent(self):
        """种群 60 → 40 > 60×0.6=36 → 降为 36 并给出提示（不静默）。"""
        limit, notes = IP.resolve_template_limit(population_size=60)
        assert limit == 36
        assert notes and "60%" in notes[0]

    def test_absolute_cap(self):
        limit, notes = IP.resolve_template_limit(population_size=1000,
                                                 requested=100)
        assert limit == IP.MAX_TEMPLATE_LIMIT
        assert notes

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            IP.resolve_template_limit(population_size=50, requested=-1)

    def test_zero_population_no_share_cap(self):
        limit, _notes = IP.resolve_template_limit(population_size=0)
        assert limit == 40


# ══════════════════════════════════════════════════════════
# 8. 端到端
# ══════════════════════════════════════════════════════════


class TestEndToEnd:
    def test_candidate_contract(self):
        res = IP.build_initial_population(
            population_size=100, selected_fields=["close", "pe_ttm"],
            compiler=fake_compiler())
        assert res.candidates
        for c in res.candidates:
            assert c["operation"] == "enumerated"
            assert c["generation"] == 0
            assert c["category"] in CAT.ALL_CATEGORIES
            assert c["formula"] and "{" not in c["formula"]
            assert c["economy_logic_zh"]

    def test_remaining_slots_is_population_minus_classic(self):
        res = IP.build_initial_population(
            population_size=100, selected_fields=["close"],
            template_limit=10, compiler=fake_compiler())
        assert len(res.candidates) == 10
        assert res.remaining_slots == 90

    def test_insufficient_not_padded(self):
        """不足不硬凑：候选少于上限时如实返回，缺口留给 AI/随机。"""
        res = IP.build_initial_population(
            population_size=100, selected_fields=["pe_ttm"],
            enabled_categories=["valuation"], template_limit=30,
            compiler=fake_compiler())
        # 只选 pe_ttm → 「PB反转」需 pb、「估值比价」需 pe_ttm+pb 都被跳过，
        # 只剩「PE反转」「盈利收益率」两条（跳过**不报错**）
        assert len(res.candidates) == 2
        assert res.insufficient is True
        assert any("不足不硬凑" in n for n in res.notes_zh)
        assert res.remaining_slots == 98
        skipped_reasons = {s["reason"] for s in res.skipped}
        assert IP.SKIP_FIELD_MISSING in skipped_reasons

    def test_skip_reason_limit_reached_recorded(self):
        res = IP.build_initial_population(
            population_size=100, selected_fields=["close"],
            enabled_categories=["trend"], template_limit=3,
            compiler=fake_compiler())
        assert len(res.candidates) == 3
        assert any(s["reason"] == IP.SKIP_LIMIT_REACHED for s in res.skipped)

    def test_invalid_strategy_rejected(self):
        with pytest.raises(ValueError):
            IP.build_initial_population(population_size=10, strategy="bogus")

    def test_priority_strategy_end_to_end(self):
        res = IP.build_initial_population(
            population_size=100, strategy=IP.STRATEGY_PRIORITY,
            selected_fields=["close", "pe_ttm"], template_limit=6,
            compiler=fake_compiler())
        assert res.strategy == IP.STRATEGY_PRIORITY
        assert len(res.candidates) == 6

    def test_disabled_templates_never_appear(self):
        res = IP.build_initial_population(
            population_size=100, selected_fields=None,
            compiler=fake_compiler())
        names = {c["template_name"] for c in res.candidates}
        assert "毛利率变化" not in names and "资产周转率" not in names

    def test_no_dedup_by_design(self):
        """任务卡「明确不做去重本身」：同名模板不同参数的候选都保留
        （去重是 T19 的职责）。"""
        res = IP.build_initial_population(
            population_size=100, selected_fields=["pe_ttm"],
            enabled_categories=["valuation"], compiler=fake_compiler())
        roe_like = [c for c in res.candidates if c["template_name"] == "PE反转"]
        assert len(roe_like) == 1     # 该模板无参数
        factor = IP.build_initial_population(
            population_size=100, selected_fields=["close"],
            enabled_categories=["trend"], template_limit=40,
            compiler=fake_compiler())
        momentum = [c for c in factor.candidates
                    if c["template_name"] == "价格动量"]
        assert len(momentum) == 3     # n1 ∈ {10,20,60} 三组参数都保留
