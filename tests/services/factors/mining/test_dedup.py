"""T19 · 去重四层（前 3 层）契约测试（DoD）。

覆盖
====
- **规范化**：交换律归一并排序（`+`/`*`/`min`/`max`）、非交换算子保持顺序
  （`-`/`/`）、去冗余括号、数字归一、方言归一（复用编译器）
- **`formula_hash`**：吃掉写法差异；摘要复用 `config_hash`（R28 单一实现）
- **第 2 层**：完全相同只留一个；**跨来源优先级 经典 > AI > 随机**
- **第 3 层**：`>0.9` 归一组留 1 代表（向导 §6.3.8 的原例：
  `mean(close,20)` 与 `mean(close,10)` 必须被判为重复）
- **淘汰不物理删除**：`input == kept + eliminated`；带 `eliminated_reason` 与 `duplicate_of`
- **第 1 层辅助**：`build_avoidance_index` 供生成器拦截
- **边界**：空输入、阈值严格大于、`use_similarity=False`、未识别来源排最后
"""
from __future__ import annotations

import pytest

from app.services.factors.mining import config_hash as CH
from app.services.factors.mining import dedup as DD


# ══════════════════════════════════════════════════════════
# 1. 规范化：交换律归一
# ══════════════════════════════════════════════════════════


class TestCanonicalizeCommutative:
    @pytest.mark.parametrize("a,b", [
        ("close+volume", "volume+close"),
        ("close*volume", "volume*close"),
        ("min(close,volume)", "min(volume,close)"),
        ("max(close,volume)", "max(volume,close)"),
        ("mean(close,5)+mean(volume,5)", "mean(volume,5)+mean(close,5)"),
        ("close+volume+amount", "amount+close+volume"),
        ("close*volume*amount", "amount*volume*close"),
        # 嵌套：内层也要归一
        ("(close+volume)*amount", "(volume+close)*amount"),
        ("cs_rank(close+volume)", "cs_rank(volume+close)"),
    ])
    def test_commutative_pairs_normalize_equal(self, a, b):
        assert DD.canonicalize_formula(a) == DD.canonicalize_formula(b)

    @pytest.mark.parametrize("a,b", [
        ("close/volume", "volume/close"),      # 除法非交换
        ("close-volume", "volume-close"),      # 减法非交换
        ("ts_delta_bars(close,5)/mean(close,20)",
         "mean(close,20)/ts_delta_bars(close,5)"),
    ])
    def test_non_commutative_ops_keep_order(self, a, b):
        assert DD.canonicalize_formula(a) != DD.canonicalize_formula(b)

    def test_commutative_sort_is_stable_regardless_of_input_order(self):
        variants = ["a_c+close", "close+a_c", "a_c+close"]
        outs = {DD.canonicalize_formula(v) for v in variants}
        assert len(outs) == 1


class TestCanonicalizeStructure:
    @pytest.mark.parametrize("raw,expected", [
        ("(close)", "close"),
        ("((close))", "close"),
        ("((close+volume))", "close+volume"),
        ("((close+volume))*2", "(close+volume)*2"),
        ("close + volume", "close+volume"),
        ("mean( close , 20 )", "mean(close,20)"),
        ("mean(close,20.0)", "mean(close,20)"),
        ("mean(close,0.50)", "mean(close,0.5)"),
    ])
    def test_parens_whitespace_numbers(self, raw, expected):
        assert DD.canonicalize_formula(raw) == expected

    def test_nested_flattening_same_op(self):
        assert DD.canonicalize_formula("(close+volume)+amount") == \
            DD.canonicalize_formula("close+(volume+amount)") == \
            "amount+close+volume"

    def test_precedence_parens_preserved(self):
        """去冗余括号不等于去所有括号：优先级必需的必须留。

        注意 `close+volume*2` 的规范串是 `2*volume+close` —— 交换律归一会在
        `+` 层排序操作数、在 `*` 层排序操作数（两者都数学等价）。
        真正要守住的不变式是：**分组不被交换律打乱**
        （`(close+volume)*2` 与 `close+volume*2` 必须仍是不同的串）。
        """
        assert DD.canonicalize_formula("(close+volume)*2") == "(close+volume)*2"
        assert DD.canonicalize_formula("close+volume*2") == "2*volume+close"
        assert DD.canonicalize_formula("close+volume*2") == \
            DD.canonicalize_formula("volume*2+close")
        assert DD.canonicalize_formula("(close+volume)*2") != \
            DD.canonicalize_formula("close+volume*2"), "分组不可被交换律抹平"

    def test_unary_minus(self):
        assert DD.canonicalize_formula("-ts_delta_bars(close,5)") == \
            "-ts_delta_bars(close,5)"

    def test_empty_and_garbage_do_not_raise(self):
        """脏公式不能中断整批去重。"""
        assert DD.canonicalize_formula("") == ""
        assert DD.canonicalize_formula("close + ") == "close+"

    def test_dialect_normalization_reused(self):
        """方言归一复用编译器实现（不自己维护别名表）。"""
        assert DD.canonicalize_formula("rank(close)") == "cs_rank(close)"
        assert DD.canonicalize_formula("std(close,20)") == "stddev(close,20)"
        assert DD.canonicalize_formula("llv(low,20)") == "lowest(low,20)"
        assert DD.canonicalize_formula("hhv(high,20)") == "highest(high,20)"
        assert DD.canonicalize_formula("turnover") == "turnover_rate"

    def test_delta_disambiguated_by_field_source(self):
        """`delta(close,5)` 的行情/财报歧义由编译器按字段来源裁决。"""
        assert DD.canonicalize_formula("delta(close,5)") == "ts_delta_bars(close,5)"

    def test_normalize_dialect_safe_never_raises(self):
        text, why = DD.normalize_dialect_safe("mean(close,5)")
        assert text == "mean(close,5)" and why is None


# ══════════════════════════════════════════════════════════
# 2. formula_hash
# ══════════════════════════════════════════════════════════


class TestFormulaHash:
    def test_same_for_equivalent_writings(self):
        assert DD.formula_hash("close+volume") == DD.formula_hash("volume+close")
        assert DD.formula_hash("(close)") == DD.formula_hash("close")
        assert DD.formula_hash("mean(close,20.0)") == DD.formula_hash("mean(close,20)")
        assert DD.formula_hash("rank(close)") == DD.formula_hash("cs_rank(close)")

    def test_differs_for_real_change(self):
        assert DD.formula_hash("mean(close,20)") != DD.formula_hash("mean(close,10)")
        assert DD.formula_hash("close+volume") != DD.formula_hash("close-volume")

    def test_stable(self):
        f = "cs_rank(close)/stddev(close,20)"
        assert DD.formula_hash(f) == DD.formula_hash(f)

    def test_digest_reuses_config_hash(self):
        """R28：摘要算法复用 `config_hash.compute_config_hash`，不另起一套。"""
        f = "close+volume"
        assert DD.formula_hash(f) == CH.compute_config_hash(
            {"formula": DD.canonicalize_formula(f)})


# ══════════════════════════════════════════════════════════
# 3. 第 2 层：精确去重 + 跨来源优先级
# ══════════════════════════════════════════════════════════


class TestLayer2Exact:
    def test_exact_duplicates_collapse(self):
        res = DD.dedup_candidates([
            {"formula": "close+volume", "source": "classic"},
            {"formula": "volume+close", "source": "ai"},   # 交换律等价
        ])
        assert res.kept_count == 1
        assert len(res.eliminated) == 1
        assert res.eliminated[0]["eliminated_reason"] == DD.ELIM_REASON_DUPLICATE
        assert res.stats["exact_duplicates"] == 1

    def test_classic_beats_ai_beats_random(self):
        """🚨 任务卡硬规则：跨来源重复保留优先级 经典 > AI > 随机。"""
        res = DD.dedup_candidates([
            {"formula": "close+volume", "source": "random"},
            {"formula": "volume+close", "source": "ai"},
            {"formula": "close+volume", "source": "classic"},
        ])
        assert res.kept_count == 1
        assert res.kept[0]["source"] == "classic"

    def test_priority_via_operation_and_logic_source(self):
        """来源字段可能是 operation / logic_source（契约 Individual 的三个字段）。"""
        assert DD.source_priority({"source": "classic"}) == 0
        assert DD.source_priority({"operation": "enumerated"}) == 0
        assert DD.source_priority({"logic_source": "template"}) == 0
        assert DD.source_priority({"operation": "ai_generated"}) == 1
        assert DD.source_priority({"source": "random"}) == 2

    def test_unknown_source_goes_last(self):
        """未识别来源排最后 —— 不让未知来源抢代表位。"""
        res = DD.dedup_candidates([
            {"formula": "close+volume", "source": "mystery"},
            {"formula": "volume+close", "source": "random"},
        ])
        assert res.kept[0]["source"] == "random"

    def test_lower_complexity_wins_within_same_source(self):
        """同来源时低复杂度优先（向导 §6.3.8 初始种群口径）。"""
        res = DD.dedup_candidates([
            {"formula": "close+volume", "source": "classic", "complexity": 9.0},
            {"formula": "volume+close", "source": "classic", "complexity": 2.0},
        ])
        assert res.kept[0]["complexity"] == 2.0

    def test_duplicate_of_points_to_representative(self):
        res = DD.dedup_candidates([
            {"formula": "close+volume", "source": "classic"},
            {"formula": "volume+close", "source": "ai"},
        ])
        assert res.eliminated[0]["duplicate_of"] == "close+volume"

    def test_deterministic(self):
        rows = [{"formula": f, "source": "ai"} for f in
                ("close+volume", "volume+close", "close*volume")]
        a = DD.dedup_candidates(rows)
        b = DD.dedup_candidates(rows)
        assert [c["formula_hash"] for c in a.kept] == \
            [c["formula_hash"] for c in b.kept]
        assert [c["formula_hash"] for c in a.eliminated] == \
            [c["formula_hash"] for c in b.eliminated]


# ══════════════════════════════════════════════════════════
# 4. 第 3 层：结构相似度聚类
# ══════════════════════════════════════════════════════════


class TestLayer3Similarity:
    def test_param_variants_are_clustered_within_source(self):
        """🚨 向导 §6.3.8 的原例：`mean(close,20)` 与 `mean(close,10)`
        哈希不同，但结构相同 → **同来源内**归为一组，只留 1 个代表。

        （2026-09-17 裁决 ①：第 3 层默认只在**同来源内**聚类 ——
         同一模板的参数变体是「参数族」，不是「重复」；跨来源同一因子由第 2 层管。
         故本用例的三个变体都给了同一来源 `classic`。）
        """
        res = DD.dedup_candidates([
            {"formula": "mean(close,20)", "source": "classic", "complexity": 3.0},
            {"formula": "mean(close,10)", "source": "classic", "complexity": 2.0},
            {"formula": "mean(close,60)", "source": "classic", "complexity": 4.0},
        ])
        assert res.kept_count == 1
        assert res.kept[0]["complexity"] == 2.0        # 同来源内低复杂度优先
        assert res.stats["similar_duplicates"] == 2
        assert all(c["eliminated_reason"] == DD.ELIM_REASON_SIMILAR
                   for c in res.eliminated)
        assert all(c["similarity"] >= DD.SIMILARITY_THRESHOLD
                   for c in res.eliminated)

    def test_default_scope_is_same_source(self):
        """裁决 ① 的默认值：第 3 层只在同来源内聚类。"""
        assert DD.SIMILARITY_SCOPE_SAME_SOURCE == "same_source"
        res = DD.dedup_candidates([{"formula": "close+volume", "source": "classic"}])
        assert res.stats["similarity_scope"] == DD.SIMILARITY_SCOPE_SAME_SOURCE

    def test_cross_source_same_structure_kept_under_same_source(self):
        """跨来源同结构：`same_source` 下**各自保留**（参数族不互相吃）。"""
        rows = [
            {"formula": "mean(close,20)/mean(close,60)-1", "source": "classic"},
            {"formula": "mean(close,15)/mean(close,90)-1", "source": "ai"},
            {"formula": "mean(close,30)/mean(close,30)-1", "source": "random"},
        ]
        same = DD.dedup_candidates(rows,
                                   similarity_scope=DD.SIMILARITY_SCOPE_SAME_SOURCE)
        assert same.kept_count == 3, "同源内分组后应保留三个代表"
        assert same.stats["similar_duplicates"] == 0

        glob = DD.dedup_candidates(rows,
                                   similarity_scope=DD.SIMILARITY_SCOPE_GLOBAL)
        assert glob.kept_count == 1, "global 下跨来源同结构被聚掉（旧行为）"
        assert glob.kept[0]["source"] == "classic"
        assert glob.stats["similar_duplicates"] == 2

    def test_exact_duplicates_still_cross_source(self):
        """第 2 层**始终跨来源**：精确重复仍按「经典 > AI > 随机」淘汰。"""
        res = DD.dedup_candidates([
            {"formula": "close+volume", "source": "random"},
            {"formula": "volume+close", "source": "classic"},
        ], similarity_scope=DD.SIMILARITY_SCOPE_SAME_SOURCE)
        assert res.kept_count == 1
        assert res.kept[0]["source"] == "classic"
        assert res.stats["exact_duplicates"] == 1

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValueError):
            DD.dedup_candidates([{"formula": "close", "source": "classic"}],
                                similarity_scope="bogus")

    def test_representative_is_lowest_complexity_within_source(self):
        res = DD.dedup_candidates([
            {"formula": "mean(close,20)", "source": "classic", "complexity": 5.0},
            {"formula": "mean(close,10)", "source": "classic", "complexity": 1.0},
        ])
        assert res.kept[0]["complexity"] == 1.0

    def test_different_structure_not_clustered(self):
        res = DD.dedup_candidates([
            {"formula": "mean(close,20)", "source": "classic"},
            {"formula": "cs_rank(pe_ttm)", "source": "ai"},
            {"formula": "stddev(close,20)/mean(close,60)", "source": "random"},
        ])
        assert res.kept_count == 3
        assert res.stats["similar_duplicates"] == 0

    def test_threshold_is_strictly_greater(self):
        """「>0.9」是**严格大于**；恰好等于阈值也不聚类。

        这对的相似度实测 = 0.75（`mean(close,#)` vs `cs_rank(mean(close,#))`），
        故 0.75 作为阈值时应**不**聚类，0.74 才聚。
        """
        # 两行给**同一来源**：本用例测的是相似度阈值语义，
        # 不该被「同源内聚类」的 scope 规则干扰
        rows = [
            {"formula": "mean(close,20)", "source": "classic"},
            {"formula": "cs_rank(mean(close,20))", "source": "classic"},
        ]
        exact = DD.structural_similarity("mean(close,20)",
                                         "cs_rank(mean(close,20))")
        assert exact == 0.75

        assert DD.dedup_candidates(rows).kept_count == 2      # 0.9 阈值：分
        assert DD.dedup_candidates(
            rows, similarity_threshold=exact).kept_count == 2, "恰好等于阈值不聚类"
        assert DD.dedup_candidates(
            rows, similarity_threshold=0.74).kept_count == 1, "低于相似度才聚类"

    @pytest.mark.parametrize("a,b,should_cluster,why", [
        ("mean(close,20)", "mean(close,10)", True, "同形状换参数（§6.3.8 原例）"),
        ("mean(close,20)/mean(close,60)-1", "mean(close,10)/mean(close,120)-1",
         True, "同模板参数变体"),
        ("close/mean(close,20)", "close/mean(close,18)", True, "同形状"),
        ("mean(close,5)/mean(close,20)-1", "(mean(close,5)-mean(close,20))/20",
         False, "趋势 vs 斜率：算子字段集合相同但树形不同"),
        ("stddev(close,20)/mean(close,60)", "mean(close,20)/mean(close,60)-1",
         False, "波动 vs 趋势"),
        ("cs_rank(pe_ttm)", "-cs_rank(pe_ttm)", False, "符号不同（方向不同）"),
        ("cs_rank(mean(close,20))", "mean(close,20)", False, "嵌套 vs 单层"),
        ("mean(volume,5)/mean(volume,20)-1", "mean(close,5)/mean(close,20)-1",
         False, "仅字段不同 → 跨类别（volume_price vs trend）"),
    ])
    def test_structural_similarity_discrimination(self, a, b, should_cluster, why):
        """判别力对照表。前两条是「必须聚」的规范要求；
        其余是「必须分」——其中「趋势 vs 斜率」「跨类别字段」两例是我第一版
        **误判**过的（只比算子/字段集合 → 相似度 1.0 → 错杀一个因子）。"""
        sim = DD.structural_similarity(a, b)
        assert (sim > DD.SIMILARITY_THRESHOLD) is should_cluster, \
            f"{why}: sim={sim}"

    def test_shape_key_keeps_field_names(self):
        """形状键**保留字段名**：字段不同 = 不同数据域，不能合并。"""
        assert DD.tree_shape_key("mean(close,20)") == "mean(close,#)"
        assert DD.tree_shape_key("mean(volume,20)") == "mean(volume,#)"
        assert DD.tree_shape_key("mean(close,20)") != \
            DD.tree_shape_key("mean(volume,20)")
        # 数字被抽象（参数变体才可聚）
        assert DD.tree_shape_key("mean(close,10)") == \
            DD.tree_shape_key("mean(close,60)")

    def test_use_similarity_false_only_exact(self):
        res = DD.dedup_candidates([
            {"formula": "mean(close,20)", "source": "classic"},
            {"formula": "mean(close,10)", "source": "ai"},
        ], use_similarity=False)
        assert res.kept_count == 2
        assert res.stats["similar_duplicates"] == 0
        assert res.stats["similarity_enabled"] is False

    def test_cluster_chain_does_not_merge_distinct_structures(self):
        """贪心聚类：代表之间不互相比较，避免链式合并把不同结构并掉。"""
        res = DD.dedup_candidates([
            {"formula": "mean(close,20)", "source": "classic"},
            {"formula": "cs_rank(mean(close,20))", "source": "ai"},
            {"formula": "cs_rank(mean(close,10))", "source": "random"},
        ], similarity_threshold=0.9,
            similarity_scope=DD.SIMILARITY_SCOPE_GLOBAL)
        # mean(close,*) 与 cs_rank(mean(close,*)) 聚为一组；
        # 两个 cs_rank 变体结构相同 → 只留一个代表
        assert res.kept_count == 2


# ══════════════════════════════════════════════════════════
# 5. 不物理删除 / 结果契约
# ══════════════════════════════════════════════════════════


class TestNoPhysicalDeletion:
    def test_conservation_on_various_inputs(self):
        cases = [
            [],
            [{"formula": "close+volume", "source": "classic"}],
            [{"formula": "close+volume", "source": "classic"},
             {"formula": "volume+close", "source": "ai"},
             {"formula": "mean(close,20)", "source": "random"},
             {"formula": "mean(close,10)", "source": "random"},
             {"formula": "cs_rank(pe_ttm)", "source": "ai"}],
        ]
        for rows in cases:
            res = DD.dedup_candidates(rows)
            assert res.stats["input_count"] == len(rows)
            assert res.kept_count + len(res.eliminated) == len(rows), rows

    def test_eliminated_rows_carry_reason_and_target(self):
        res = DD.dedup_candidates([
            {"formula": "close+volume", "source": "classic"},
            {"formula": "volume+close", "source": "ai"},
        ])
        row = res.eliminated[0]
        assert row["status"] == "eliminated"
        assert row["eliminated_reason"] in (DD.ELIM_REASON_DUPLICATE,
                                            DD.ELIM_REASON_SIMILAR)
        assert row["eliminated_reason_zh"]
        assert row["duplicate_of"]

    def test_kept_rows_are_representatives(self):
        res = DD.dedup_candidates([{"formula": "close+volume", "source": "classic"}])
        assert res.kept[0]["status"] == "active"
        assert res.kept[0]["is_representative"] is True

    def test_kept_rows_have_canonical_and_hash(self):
        res = DD.dedup_candidates([{"formula": "volume+close", "source": "classic"}])
        assert res.kept[0]["canonical_formula"] == "close+volume"
        assert res.kept[0]["formula_hash"] == DD.formula_hash("close+volume")
        assert res.kept[0]["structure_key"]

    def test_stats_shape(self):
        res = DD.dedup_candidates([
            {"formula": "close+volume", "source": "classic"},
            {"formula": "volume+close", "source": "ai"},
        ])
        for key in ("input_count", "exact_duplicates", "similar_duplicates",
                    "kept_count", "eliminated_count", "dedup_rate",
                    "unique_ratio", "by_source", "similarity_threshold"):
            assert key in res.stats
        assert 0.0 <= res.stats["dedup_rate"] <= 1.0
        assert res.stats["by_source"]["classic"] == 1

    def test_to_dict(self):
        res = DD.dedup_candidates([{"formula": "close+volume", "source": "classic"}])
        d = res.to_dict()
        assert set(d) == {"kept", "eliminated", "stats"}

    def test_empty_input(self):
        res = DD.dedup_candidates([])
        assert res.kept == [] and res.eliminated == []
        assert res.stats["input_count"] == 0
        assert res.stats["dedup_rate"] == 0.0


# ══════════════════════════════════════════════════════════
# 6. 第 1 层辅助 + 三来源合并
# ══════════════════════════════════════════════════════════


class TestAvoidanceIndexAndMerge:
    def test_avoidance_index_hashes_and_structure_counts(self):
        idx = DD.build_avoidance_index([
            {"formula": "mean(close,20)"},
            {"formula": "mean(close,10)"},        # 同结构
            {"formula": "close+volume"},
        ])
        assert idx["count"] == 3
        assert DD.formula_hash("mean(close,60)") not in idx["hashes"]
        counts = sorted(idx["structure_counts"].values())
        assert counts == [1, 2], "mean(close,#) 结构应计 2 次"

    def test_avoidance_index_honors_given_hash(self):
        idx = DD.build_avoidance_index([
            {"formula": "close+volume", "formula_hash": "preset-hash"}])
        assert "preset-hash" in idx["hashes"]

    def test_avoidance_index_skips_empty_formula(self):
        idx = DD.build_avoidance_index([{"formula": ""}, {"other": 1}])
        assert idx["count"] == 0

    def test_merge_across_sources_priority(self):
        res = DD.dedup_across_sources(
            classic=[{"formula": "close+volume"}],
            ai=[{"formula": "volume+close"}],
            random=[{"formula": "close+volume"}],
        )
        assert res.kept_count == 1
        assert res.kept[0]["source"] == "classic"
        assert res.stats["exact_duplicates"] == 2

    def test_merge_marks_default_sources(self):
        res = DD.dedup_across_sources(classic=[{"formula": "mean(close,20)"}],
                                      ai=[], random=[])
        assert res.kept[0]["source"] == "classic"

    def test_merge_all_empty(self):
        res = DD.dedup_across_sources()
        assert res.kept_count == 0 and res.stats["input_count"] == 0


# ══════════════════════════════════════════════════════════
# 7. 与上游生成器的协作（真实样本）
# ══════════════════════════════════════════════════════════


class TestWithUpstreamGenerators:
    def test_classic_templates_dedup_keeps_one_per_structure(self):
        """经典底座：9 个均线趋势参数变体 → 1 个代表（§6.3.8 第 3 层）。"""
        from app.services.factors.mining import initial_population as IP

        cands = IP.build_initial_population(
            population_size=200,
            selected_fields=["close", "volume", "pe_ttm"],
            enabled_categories=["trend"], template_limit=40).candidates
        assert len(cands) > 5
        res = DD.dedup_candidates(cands)
        assert res.kept_count < len(cands)
        # 保留的都是「活跃代表」
        assert all(c["is_representative"] for c in res.kept)
        # 均线趋势只留 1 个
        ma = [c for c in res.kept if c.get("template_name") == "均线趋势"]
        assert len(ma) == 1

    def test_random_candidates_survive_dedup(self):
        from app.services.factors.mining import random_generator as RG

        rnd = RG.generate_random_candidates(
            cfg=RG.RandomGeneratorConfig(target_count=12, seed=5),
            selected_fields=["close", "volume", "pe_ttm"]).candidates
        res = DD.dedup_candidates(rnd)
        assert res.kept_count > 0
        assert res.stats["exact_duplicates"] == 0, \
            "T18 已做实时拦截，产物不该再有精确重复"
        assert res.kept_count + len(res.eliminated) == len(rnd)

    def test_avoidance_index_from_classic_blocks_exact_rerun(self):
        """第 1 层闭环：把经典底座喂给生成器做回避索引 → 不产出同哈希公式。"""
        from app.services.factors.mining import initial_population as IP

        classic = IP.build_initial_population(
            population_size=200, selected_fields=["close", "volume"],
            enabled_categories=["volume_price"], template_limit=40).candidates
        idx = DD.build_avoidance_index(classic)
        assert idx["hashes"], "应有可回避的哈希"
        for c in classic:
            assert DD.formula_hash(c["formula"]) in idx["hashes"]
