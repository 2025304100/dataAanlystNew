"""T18 · 受约束随机生成器 契约测试（DoD）。

覆盖
====
- **类型系统**：字段分类型、算子签名输出类型、`+`/`-` 同类型、截面算子无量纲
- **复杂度三重上限**：算子 ≤4 / 嵌套 ≤2 / 字段引用 ≤3（任务卡硬规则）
- **同结构限变体**：同 AST 结构最多 3 个
- **实时哈希拦截**：与已有（经典+AI+随机）撞哈希立即弃用
- **确定性**：同 seed 同结果；不同 seed 不同结果
- **冗余与取 Top**：raw_target = 目标 × 2.0；final = 目标 + 补位
- **多样性排序**：相似度低者优先；同分时**复杂度降序**（平凡式不占前排）
- **真实编译**：0 编译错误（生成器只用编译器真实存在的算子）
- **边界**：无可用字段 / 上限为 0 / 受约束导致产出不足（不硬凑）
"""
from __future__ import annotations

import re

import pytest

from app.services.factors.factor_compiler import FUNCTION_CATALOG
from app.services.factors.mining import random_generator as RG


# ── 假编译器：让大多数用例不依赖真编译（确定性 + 快）──


class _Plan:
    def __init__(self, formula: str) -> None:
        self.formula = formula
        self.complexity_score = float(len(formula))
        self.node_count = RG.count_operators(formula) + 1
        self.ast_depth = RG.count_nesting(formula)
        self.max_lookback = 20
        self.direction = "higher_better"


class _Result:
    def __init__(self, formula: str, *, ok: bool = True) -> None:
        self.success = ok
        self.execution_plan = _Plan(formula) if ok else None
        self.errors = [] if ok else [type("E", (), {
            "error_code": "fake", "message": "rejected"})()]


def fake_compiler(*, reject_contains: str | None = None):
    def _c(*, formula: str, params: dict | None = None):
        return _Result(formula, ok=not (reject_contains
                                        and reject_contains in formula))
    return _c


ALL_SELECTED = ["close", "open", "high", "low", "volume", "amount",
                "turnover_rate", "pe_ttm", "pb", "roe_ttm"]


# ══════════════════════════════════════════════════════════
# 1. 类型系统
# ══════════════════════════════════════════════════════════


class TestTypeSystem:
    def test_field_types_cover_catalog(self):
        assert RG.FIELD_TYPES["close"] == RG.T_PRICE
        assert RG.FIELD_TYPES["volume"] == RG.T_VOLUME
        assert RG.FIELD_TYPES["turnover_rate"] == RG.T_TURNOVER
        assert RG.FIELD_TYPES["pe_ttm"] == RG.T_VALUATION
        assert RG.FIELD_TYPES["roe_ttm"] == RG.T_QUALITY

    def test_all_signature_ops_exist_in_compiler(self):
        """签名里的算子必须是编译器真实存在的（拼错会全量编译失败）。"""
        unknown = [n for n in RG.OP_SIGNATURES if n not in FUNCTION_CATALOG]
        assert unknown == [], f"签名含编译器不存在的算子: {unknown}"

    def test_all_field_types_exist_in_compiler(self):
        from app.services.factors.factor_compiler import FIELD_CATALOG

        unknown = [f for f in RG.FIELD_TYPES if f not in FIELD_CATALOG]
        assert unknown == [], f"类型表含编译器不存在的字段: {unknown}"

    def test_cross_section_ops_are_dimensionless_or_same(self):
        """判据不是名字前缀，而是「**是否除以了同量纲的量**」。

        归一化类（单位相消 → 无量纲且丢域）：
          cs_rank（百分位）/ cs_zscore（(x-μ)/σ）/ cs_scale（x/Σ|x|）/ cs_quantile（组号）
        保形类（保单位保域）：
          cs_demean（x-μ）/ cs_winsorize（截尾）

        ⚠️ T18 复核修正：`cs_scale` 曾被我按「保持量纲」登记 —— 错的。
        实现是 `values / Σ|x|`（`dsl/cross_section.py`），单位相消，实为无量纲。
        """
        assert RG.OP_SIGNATURES["cs_rank"].output == RG.T_DIMENSIONLESS
        assert RG.OP_SIGNATURES["cs_zscore"].output == RG.T_DIMENSIONLESS
        assert RG.OP_SIGNATURES["cs_quantile"].output == RG.T_DIMENSIONLESS
        assert RG.OP_SIGNATURES["cs_scale"].output == RG.T_DIMENSIONLESS
        # demean / winsorize 保持量纲
        assert RG.OP_SIGNATURES["cs_demean"].output == "same"
        assert RG.OP_SIGNATURES["cs_winsorize"].output == "same"

    def test_cs_scale_matches_implementation_semantics(self):
        """把「为什么 cs_scale 是无量纲」钉在**真实实现**上：
        它的分母是同一量纲的 Σ|x|，故 x/Σ|x| 单位相消。"""
        import inspect

        from app.services.factors.dsl import cross_section as CS

        src = inspect.getsource(CS.cs_scale)
        assert "abs().sum()" in src, "cs_scale 实现变了，类型表需重新裁决"
        assert RG.OP_SIGNATURES["cs_scale"].output == RG.T_DIMENSIONLESS

    def test_rolling_ops_keep_type_stddev_and_pct_change_are_dimensionless(self):
        assert RG.OP_SIGNATURES["mean"].resolve_output([RG.T_PRICE, "window"]) == \
            RG.T_PRICE
        assert RG.OP_SIGNATURES["stddev"].output == RG.T_DIMENSIONLESS
        assert RG.OP_SIGNATURES["pct_change"].output == RG.T_DIMENSIONLESS
        assert RG.OP_SIGNATURES["count"].output == RG.T_DIMENSIONLESS

    def test_log_exp_excluded_with_reason(self):
        assert set(RG.EXCLUDED_FUNCTIONS) == {"log", "exp"}
        assert all(v for v in RG.EXCLUDED_FUNCTIONS.values())

    def test_fields_grouped_by_type(self):
        grouped = RG.available_fields_by_type(["close", "volume", "pe_ttm"])
        assert grouped[RG.T_PRICE] == ["close"]
        assert grouped[RG.T_VOLUME] == ["volume"]
        assert grouped[RG.T_VALUATION] == ["pe_ttm"]

    def test_unknown_fields_ignored(self):
        grouped = RG.available_fields_by_type(["close", "not_a_field"])
        assert grouped == {RG.T_PRICE: ["close"]}


# ══════════════════════════════════════════════════════════
# 2. 复杂度度量与结构键
# ══════════════════════════════════════════════════════════


class TestMetrics:
    def test_count_operators(self):
        assert RG.count_operators("close") == 0
        assert RG.count_operators("mean(close,5)") == 1
        assert RG.count_operators("cs_rank(mean(close,5))") == 2

    def test_count_nesting(self):
        assert RG.count_nesting("close") == 0
        assert RG.count_nesting("mean(close,5)") == 0
        assert RG.count_nesting("cs_rank(mean(close,5))") == 1
        assert RG.count_nesting("cs_rank(mean(stddev(close,5),20))") == 2

    def test_referenced_fields_dedup_and_order(self):
        assert RG.referenced_fields("mean(close,5)/mean(close,20)") == ["close"]
        assert RG.referenced_fields("close/volume") == ["close", "volume"]
        # 算子名与数字不算字段
        assert RG.referenced_fields("cs_rank(close)") == ["close"]

    def test_ast_structure_key_blanks_numbers(self):
        a = RG.ast_structure_key("mean(close,5)/mean(close,20)")
        b = RG.ast_structure_key("mean(close,10)/mean(close,60)")
        assert a == b == "mean(close,#)/mean(close,#)"
        c = RG.ast_structure_key("mean(close,5)-mean(close,20)")
        assert c != a

    def test_structure_similarity_bounds(self):
        f = "mean(close,5)"
        assert RG.structure_similarity(f, f) == 1.0
        other = "cs_rank(pe_ttm)"
        sim = RG.structure_similarity(f, other)
        assert 0.0 <= sim < 1.0

    def test_structure_similarity_prefers_shared_structure(self):
        base = "mean(close,5)/mean(close,20)"
        close_variant = "mean(close,10)/mean(close,60)"
        far = "cs_rank(pe_ttm)"
        assert RG.structure_similarity(base, close_variant) > \
            RG.structure_similarity(base, far)


# ══════════════════════════════════════════════════════════
# 3. 复杂度三重上限（任务卡硬规则）
# ══════════════════════════════════════════════════════════


class TestComplexityLimits:
    def test_constants(self):
        assert (RG.MAX_OPERATORS, RG.MAX_NESTING, RG.MAX_FIELD_REFS) == (4, 2, 3)

    def test_within_limits(self):
        cfg = RG.RandomGeneratorConfig(target_count=1)
        for f in ("mean(close,5)", "cs_rank(mean(close,5))",
                  "cs_rank(mean(stddev(close,5),20))"):
            ok, why = RG.within_complexity_limits(f, cfg=cfg)
            assert ok, f"{f} 应通过: {why}"

    def test_too_many_operators(self):
        cfg = RG.RandomGeneratorConfig(target_count=1)
        ok, why = RG.within_complexity_limits(
            "mean(close,5)+mean(close,10)+mean(close,20)+mean(volume,5)"
            "+mean(amount,5)", cfg=cfg)
        assert not ok and "算子数" in why

    def test_too_deep(self):
        cfg = RG.RandomGeneratorConfig(target_count=1)
        ok, why = RG.within_complexity_limits(
            "cs_rank(mean(stddev(mean(close,5),20),20))", cfg=cfg)
        assert not ok and "嵌套" in why

    def test_too_many_fields(self):
        cfg = RG.RandomGeneratorConfig(target_count=1)
        ok, why = RG.within_complexity_limits(
            "cs_rank(close+volume+pe_ttm+roe_ttm)", cfg=cfg)
        assert not ok and ("字段引用" in why or "算子数" in why)

    def test_pure_field_rejected(self):
        cfg = RG.RandomGeneratorConfig(target_count=1)
        ok, why = RG.within_complexity_limits("close", cfg=cfg)
        assert not ok and "无算子" in why

    def test_every_generated_candidate_within_limits(self):
        cfg = RG.RandomGeneratorConfig(target_count=40, seed=3)
        res = RG.generate_random_candidates(cfg=cfg, selected_fields=ALL_SELECTED,
                                            compiler=fake_compiler())
        for c in res.candidates:
            ok, why = RG.within_complexity_limits(c["formula"], cfg=cfg)
            assert ok, f"{c['formula']} 越界: {why}"


# ══════════════════════════════════════════════════════════
# 4. 同结构限变体 + 实时哈希拦截
# ══════════════════════════════════════════════════════════


class TestStructureCapAndHashInterception:
    def test_variant_cap_three(self):
        cfg = RG.RandomGeneratorConfig(target_count=200, seed=11,
                                       max_variants_per_structure=3)
        res = RG.generate_random_candidates(cfg=cfg, selected_fields=ALL_SELECTED,
                                            compiler=fake_compiler())
        from collections import Counter
        counts = Counter(c["structure_key"] for c in res.candidates)
        assert counts, "应产出候选"
        assert max(counts.values()) <= 3, f"结构变体超限: {counts.most_common(3)}"

    def test_hash_interception_skips_existing(self):
        first = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=20, seed=5),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        existing = [c["formula"] for c in first.candidates]
        second = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=20, seed=5),
            selected_fields=ALL_SELECTED, existing_formulas=existing,
            compiler=fake_compiler())
        assert not (set(c["formula"] for c in second.candidates) & set(existing))
        assert second.stats["rejected_hash_collision"] > 0

    def test_existing_hashes_are_honored(self):
        base = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=10, seed=9),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        hashes = [c["formula_hash"] for c in base.candidates]
        again = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=10, seed=9),
            selected_fields=ALL_SELECTED, existing_hashes=hashes,
            compiler=fake_compiler())
        assert not (set(c["formula_hash"] for c in again.candidates)
                    & set(hashes))

    def test_formula_hash_is_stable_and_distinct(self):
        assert RG._formula_hash("mean(close,5)") == \
            RG._formula_hash("mean(close,5)")
        assert RG._formula_hash("mean(close,5)") != \
            RG._formula_hash("mean(close,10)")

    def test_hash_reuses_config_hash_implementation(self):
        """R28：哈希复用 `config_hash.compute_config_hash`，不另起一套。"""
        from app.services.factors.mining import config_hash as CH
        f = "cs_rank(mean(close,5))"
        assert RG._formula_hash(f) == CH.compute_config_hash({"formula": f})


# ══════════════════════════════════════════════════════════
# 5. 确定性与冗余/取 Top
# ══════════════════════════════════════════════════════════


class TestDeterminismAndTargets:
    def test_same_seed_same_result(self):
        a = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=15, seed=101),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        b = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=15, seed=101),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        assert [c["formula"] for c in a.candidates] == \
            [c["formula"] for c in b.candidates]

    def test_different_seed_differs(self):
        a = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=15, seed=1),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        b = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=15, seed=2),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        assert [c["formula"] for c in a.candidates] != \
            [c["formula"] for c in b.candidates]

    def test_redundancy_raw_target(self):
        cfg = RG.RandomGeneratorConfig(target_count=10)
        assert cfg.redundancy == 2.0
        assert cfg.raw_target == 20
        assert cfg.final_target == 10

    def test_backfill_extends_final_target(self):
        """承担 AI 不足时的补位（需求 §6.1）。"""
        cfg = RG.RandomGeneratorConfig(target_count=5, backfill_count=4)
        assert cfg.final_target == 9

    def test_selected_count_respects_final_target(self):
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=8, seed=4),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        assert len(res.candidates) <= 8

    def test_zero_target(self):
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=0),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        assert res.candidates == []
        assert res.stats["raw_target"] == 0


# ══════════════════════════════════════════════════════════
# 6. 多样性排序
# ══════════════════════════════════════════════════════════


class TestDiversityOrdering:
    def test_low_similarity_first(self):
        cands = [
            {"formula": "mean(close,5)", "complexity": 5.0},   # 与参照相似
            {"formula": "cs_rank(pe_ttm)", "complexity": 5.0},  # 与参照不像
        ]
        ordered = RG.diversity_order(cands, reference_formulas=["mean(close,5)"])
        assert ordered[0]["formula"] == "cs_rank(pe_ttm)"
        assert ordered[0]["max_similarity_to_existing"] < \
            ordered[1]["max_similarity_to_existing"]

    def test_tie_break_prefers_higher_complexity(self):
        """无参照集（相似度全 0）时，同分优先更复杂者 ——
        否则 `abs(roe_ttm)` 这类平凡式会占满前排。"""
        cands = [
            {"formula": "abs(roe_ttm)", "complexity": 1.0},
            {"formula": "cs_rank(mean(stddev(close,5),20))", "complexity": 9.0},
        ]
        ordered = RG.diversity_order(cands)
        assert ordered[0]["formula"] == "cs_rank(mean(stddev(close,5),20))"

    def test_deterministic_order(self):
        cands = [{"formula": f, "complexity": 3.0}
                 for f in ("mean(close,5)", "mean(close,10)", "mean(volume,5)")]
        a = [c["formula"] for c in RG.diversity_order(cands)]
        b = [c["formula"] for c in RG.diversity_order(cands)]
        assert a == b

    def test_records_operator_count_and_similarity(self):
        ordered = RG.diversity_order([{"formula": "cs_rank(mean(close,5))",
                                       "complexity": 3.0}])
        assert ordered[0]["operator_count"] == 2
        assert ordered[0]["max_similarity_to_existing"] == 0.0

    def test_diversity_used_in_generation_end_to_end(self):
        """给定参照集，产出应显著区别于参照（相似度低者优先入选）。"""
        refs = ["mean(close,5)/mean(close,20)", "stddev(close,20)"]
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=6, seed=21),
            selected_fields=ALL_SELECTED, reference_formulas=refs,
            compiler=fake_compiler())
        sims = [c["max_similarity_to_existing"] for c in res.candidates]
        assert sims == sorted(sims), "应满足「相似度低者在前」"


# ══════════════════════════════════════════════════════════
# 7. 候选契约 / 真实编译 / 边界
# ══════════════════════════════════════════════════════════


class TestCandidateContract:
    def test_candidate_fields(self):
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=5, seed=6),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        for c in res.candidates:
            assert c["operation"] == "random"
            assert c["source"] == RG.SOURCE_RANDOM
            assert c["generation"] == 0
            assert c["compile_ok"] is True
            assert c["formula"] and c["structure_key"]
            assert isinstance(c["required_fields"], list)
            assert c["formula_hash"]

    def test_result_to_dict(self):
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=3, seed=8),
            selected_fields=ALL_SELECTED, compiler=fake_compiler())
        d = res.to_dict()
        assert set(d) == {"candidates", "stats", "notes_zh"}
        assert d["stats"]["accepted"] >= len(d["candidates"])

    def test_real_compiler_no_errors(self):
        """真实编译器：生成器只用编译器真实存在的算子 → 必须 0 编译错误。"""
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=40, seed=13),
            selected_fields=ALL_SELECTED)
        assert res.stats["rejected_compile"] == 0, \
            f"存在编译失败: {res.stats}"
        assert res.candidates

    def test_compile_rejection_counted(self):
        """全部公式都被编译器拒绝 → 计数正确且不产出候选。

        （第一版用 `reject_contains="cs_rank"` 断言，但该 token 在给定 seed 下
        可能一个都不出现 —— 断言依赖随机分布，不可靠。改为拒绝全部算子调用。）
        """
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=10, seed=14),
            selected_fields=ALL_SELECTED,
            compiler=fake_compiler(reject_contains="("))
        assert res.stats["rejected_compile"] > 0
        assert res.candidates == []

    def test_no_available_fields(self):
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=5),
            selected_fields=["not_a_field"], compiler=fake_compiler())
        assert res.candidates == []
        assert any("没有可用字段" in n for n in res.notes_zh)

    def test_insufficient_not_padded(self):
        """受约束下产出不足时如实返回（不硬凑、不放松约束）。"""
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=500, seed=15,
                                         max_attempts_per_formula=1),
            selected_fields=["close"], compiler=fake_compiler())
        assert res.stats["selected"] <= 500
        if res.stats["selected"] < 500:
            assert any("不足不硬凑" in n for n in res.notes_zh)

    def test_generated_formulas_only_use_selected_fields(self):
        res = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=30, seed=17),
            selected_fields=["close", "volume"], compiler=fake_compiler())
        for c in res.candidates:
            assert set(c["required_fields"]) <= {"close", "volume"}, c["formula"]
