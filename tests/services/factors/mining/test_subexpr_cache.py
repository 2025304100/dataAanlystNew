"""T20 · G2 公共子表达式缓存 契约测试（DoD）。

覆盖（对齐任务卡 4 条坑 + 向导 §6.11.1 兜底三条）
==============================================
- **AST 提取**：后序、规范化去重、跳过常量、跨因子共享
- **SHA-256 强哈希**：64 hex、可复算、**跨进程稳定**（用不同 `PYTHONHASHSEED`
  起子进程比对 —— 这正是「禁用内置 hash」要防的失效）
- **组装零计算**：`assemble` 期间 `compute_fn` 调用次数 **== 0**（任务卡硬要求）
- **NaN/Inf/None 不写缓存并标 failed**
- **作用域不混用**：预筛 / 全量验证各自独立缓存
- **采样校验**：差异 > 1e-6 → 报警 + 废弃缓存
- **版本不兼容** → 恢复时废弃并给原因
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import numpy as np
import pytest

from app.services.factors.factor_compiler import compile_formula
from app.services.factors.mining import config_hash as CH
from app.services.factors.mining import dedup as DD
from app.services.factors.mining import subexpr_cache as SC


def _cache(scope: str = SC.SCOPE_PRESCREEN, snapshot: str = "snap-1",
           **kw) -> SC.SubexpressionCache:
    return SC.SubexpressionCache(scope=scope, data_snapshot_version=snapshot, **kw)


def _plan(formula: str):
    r = compile_formula(formula=formula, params=None)
    assert r.success, (formula, r.errors)
    return r.execution_plan


class _Compute:
    """确定性假计算器（同时数调用次数）。"""

    def __init__(self, *, raise_on: set[str] | None = None,
                 nan_on: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.raise_on = raise_on or set()
        self.nan_on = nan_on or set()

    def __call__(self, text: str):
        self.calls.append(text)
        if text in self.raise_on:
            raise RuntimeError(f"injected failure: {text}")
        if text in self.nan_on:
            return np.full(3, np.nan)
        # 由文本派生确定性值：长度 → 值（保证组装可断言）
        base = float(len(text) % 7) + 1.0
        return np.array([base, base + 1.0, base + 2.0])

    @property
    def count(self) -> int:
        return len(self.calls)


# ══════════════════════════════════════════════════════════
# 1. AST 渲染
# ══════════════════════════════════════════════════════════


class TestRenderAst:
    @pytest.mark.parametrize("formula", [
        "close",
        "close+volume",
        "close-volume",
        "close*2",
        "close/2",
        "-close",
        "min(close,volume)",
        "mean(close,20)",
        "cs_rank(mean(close,20))",
    ])
    def test_render_matches_canonical(self, formula):
        """渲染结果经规范化后应与原式的规范串一致（渲染器是正确的）。"""
        node = _plan(formula).formula_ast
        assert DD.canonicalize_formula(SC.render_ast(node)) == \
            DD.canonicalize_formula(formula)

    def test_unknown_node_type_raises(self):
        with pytest.raises(SC.UnsupportedAstError):
            SC.render_ast({"type": "Weird"})

    def test_unknown_bin_op_raises(self):
        with pytest.raises(SC.UnsupportedAstError):
            SC.render_ast({"type": "BinOp", "op": "MatMult",
                           "left": {"type": "Name", "id": "a"},
                           "right": {"type": "Name", "id": "b"}})

    def test_integer_constant_rendered_without_decimal(self):
        """渲染器给二元表达式加**防御性括号**（保证嵌套正确），
        去冗余括号是 `canonicalize_formula` 的职责（T19），故这里断言
        「渲染后经规范化 == 原式的规范串」。"""
        node = _plan("close*2").formula_ast
        assert SC.render_ast(node) == "(close*2)"
        assert DD.canonicalize_formula(SC.render_ast(node)) == "2*close"


# ══════════════════════════════════════════════════════════
# 2. 子表达式提取
# ══════════════════════════════════════════════════════════


class TestExtract:
    def test_post_order_root_last(self):
        subs = SC.extract_subexpressions(formula="mean(close,20)/mean(close,60)-1")
        assert subs[-1] == "mean(close,20)/mean(close,60)-1", "根必须最后"
        assert "close" in subs
        assert "mean(close,20)" in subs

    def test_constants_skipped_by_default(self):
        """🚨 纯数字常量是编译期常量，不占缓存条目（否则 compute_fn 要去"算" 20）。"""
        subs = SC.extract_subexpressions(formula="mean(close,20)-1")
        assert not any(s.replace(".", "").isdigit() for s in subs), subs
        assert "mean(close,20)" in subs

    def test_constants_included_on_demand(self):
        subs = SC.extract_subexpressions(formula="mean(close,20)-1",
                                         include_constants=True)
        assert any(s == "20" or s == "1" for s in subs)

    def test_commutative_shared_key(self):
        """`close+volume` 与 `volume+close` 提取出同一个子表达式（跨因子共享）。"""
        a = SC.extract_subexpressions(formula="close+volume")
        b = SC.extract_subexpressions(formula="volume+close")
        assert set(a) == set(b) == {"close", "volume", "close+volume"}

    def test_dedup_within_one_formula(self):
        subs = SC.extract_subexpressions(
            formula="mean(close,20)/mean(close,60)-mean(close,20)")
        assert subs.count("mean(close,20)") == 1

    def test_include_root_false(self):
        subs = SC.extract_subexpressions(formula="cs_rank(pe_ttm)",
                                         include_root=False)
        assert subs == ["pe_ttm"]

    def test_accepts_plan_directly(self):
        plan = _plan("close+volume")
        assert SC.extract_subexpressions(plan=plan)[-1] == "close+volume"

    def test_requires_formula_or_plan(self):
        with pytest.raises(ValueError):
            SC.extract_subexpressions()

    def test_uncompilable_formula_raises(self):
        with pytest.raises(SC.UnsupportedAstError):
            SC.extract_subexpressions(formula="unknown_field_xyz+1")

    def test_batch_totals_and_sharing(self):
        res = SC.extract_batch([
            {"id": "f1", "formula": "mean(close,20)/mean(close,60)-1"},
            {"id": "f2", "formula": "cs_rank(mean(close,20))"},
            {"id": "f3", "formula": "mean(close,20)-mean(close,60)"},
        ])
        assert res["total"] > res["unique"]
        assert 0.0 < res["dedup_rate"] < 1.0
        # 三个因子共享 mean(close,20)
        sharing = [k for k, v in res["per_factor"].items() if "mean(close,20)" in v]
        assert len(sharing) == 3

    def test_batch_accepts_plain_strings(self):
        res = SC.extract_batch(["close+volume", "volume+close"])
        assert res["unique"] == 3, "两个因子共用同一组子表达式"


# ══════════════════════════════════════════════════════════
# 3. SHA-256 键（任务卡坑 1）
# ══════════════════════════════════════════════════════════


class TestSha256Key:
    def test_key_is_sha256_hex(self):
        key = _cache().key_of("mean(close,20)")
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_key_recomputable_from_sha256(self):
        """键必须能由 `hashlib.sha256` 复算 —— 证明走的不是内置 hash。"""
        c = _cache(snapshot="snap-1")
        payload = CH.canonical_config_payload({
            "subexpr": DD.canonicalize_formula("close+volume"),
            "data_snapshot_version": "snap-1",
            "schema_version": SC.SCHEMA_VERSION,
        })
        expected = hashlib.sha256(
            f"{SC.HASH_ALGORITHM_VERSION}|{payload}".encode("utf-8")).hexdigest()
        assert c.key_of("close+volume") == expected

    def test_stable_across_instances(self):
        assert _cache().key_of("x") == _cache().key_of("x")

    def test_stable_across_processes_with_different_hash_seed(self):
        """🚨 任务卡坑 1 的行为验证：换 `PYTHONHASHSEED` 键必须不变。

        内置 `hash()` 有随机化种子（铁律 R4），跨进程会漂移；SHA-256 不会。
        """
        code = (
            "import sys; sys.path.insert(0, r'%s');"
            "from app.services.factors.mining import subexpr_cache as SC;"
            "print(SC.SubexpressionCache(scope=SC.SCOPE_PRESCREEN,"
            "data_snapshot_version='snap-1').key_of('close+volume'))"
            % os.getcwd()
        )
        outs = []
        for seed in ("0", "987654"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            r = subprocess.run([sys.executable, "-c", code], env=env,
                               capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, r.stderr[-500:]
            outs.append(r.stdout.strip())
        assert outs[0] == outs[1] == _cache().key_of("close+volume"), outs

    def test_commutative_same_key(self):
        c = _cache()
        assert c.key_of("close+volume") == c.key_of("volume+close")
        assert c.key_of("min(close,volume)") == c.key_of("min(volume,close)")

    def test_data_snapshot_version_changes_key(self):
        """换数据快照 → 全部 miss（不会拿到旧数据的结果）。"""
        assert _cache(snapshot="s1").key_of("close") != \
            _cache(snapshot="s2").key_of("close")

    def test_scope_does_not_change_key(self):
        """作用域不进键：同一个子表达式在两个阶段可以各自缓存同一把键。"""
        assert _cache(scope=SC.SCOPE_PRESCREEN).key_of("close") == \
            _cache(scope=SC.SCOPE_FULL_VALIDATION).key_of("close")


# ══════════════════════════════════════════════════════════
# 4. 值检查 / 缓存污染（任务卡坑 3）
# ══════════════════════════════════════════════════════════


class TestValueQuality:
    def test_ok_array(self):
        ok, ratio, why = SC.value_quality(np.array([1.0, 2.0]))
        assert ok and ratio == 0.0 and why is None

    def test_few_nan_ok(self):
        ok, ratio, why = SC.value_quality(np.array([1.0, np.nan, 3.0, 4.0]))
        assert ok and ratio == pytest.approx(0.25) and why is None

    def test_all_nan_reported_by_ratio_threshold_by_put(self):
        """职责划分：`value_quality` 报**事实**（NaN 比例、是否含 Inf/空），
        阈值判定在 `put`（受 `max_nan_ratio` 配置控制）。
        故全 NaN 时 `ok=True` 但 ratio=1.0，`put` 会因超阈值标 failed。"""
        ok, ratio, why = SC.value_quality(np.array([np.nan, np.nan]))
        assert ok is True and ratio == 1.0 and why is None

        c = _cache()
        rec = c.put("all_nan", np.array([np.nan, np.nan]))
        assert rec.status == SC.STATUS_FAILED
        assert rec.value is None

    def test_inf_treated_as_bad_ratio_not_hard_reject(self):
        """含 Inf 不再「一律拒绝」，而是与 NaN 同口径计入 bad 比例（2026-09-23 修正）。

        理由：缓存拒写后调用方会**回退直算**，含 Inf 的值照样进下游 —— 原「硬拒绝」
        的保护是假的，代价却是真的（该因子在缓存中永久缺失 → 抽样校验无样本 +
        每代重复全量计算）。实测：`sqrt(amount)/ts_delta_periods(amount,20)` 仅
        0.0002% 的 Inf，却让整式子失去缓存资格。

        仍拦得住「完全不可用」：全 Inf / 全 NaN 的 bad = 1.0 > 阈值 → 拒绝。
        """
        ok, ratio, why = SC.value_quality(np.array([1.0, np.inf]))
        assert ok is True, "少量 Inf 不应让整式子失去缓存资格"
        assert ratio == 0.5, ratio
        assert why is None

        c = _cache()
        assert c.put("few_inf", np.array([1.0, np.nan, np.inf, 2.0])).status \
            == SC.STATUS_VALID
        assert c.put("all_inf", np.array([np.inf, np.inf])).status == SC.STATUS_FAILED

    def test_none_and_empty_rejected(self):
        assert SC.value_quality(None)[0] is False
        assert SC.value_quality(np.array([]))[0] is False

    def test_scalar_ok(self):
        assert SC.value_quality(1.5)[0] is True


class TestCacheWrite:
    def test_put_valid_and_get(self):
        c = _cache()
        rec = c.put("close", np.array([1.0, 2.0]))
        assert rec.status == SC.STATUS_VALID
        assert c.get("close") is not None
        assert c.status_of("close") == SC.STATUS_VALID

    def test_put_rejects_nan_and_marks_failed(self):
        c = _cache()
        rec = c.put("x", np.full(4, np.nan))
        assert rec.status == SC.STATUS_FAILED
        assert rec.value is None
        assert rec.error
        assert c.rejected == 1

    def test_put_rejects_all_inf(self):
        """全 Inf = 完全不可用 → 拒绝。

        2026-09-23 口径调整：原断言用 `[1.0, -inf]`（仅一半为 Inf）要求拒绝，
        现按「Inf 与 NaN 同口径计入 bad 比例」不再拒绝 —— 该保护原本就是假的
        （调用方回退直算后 Inf 照样进下游），见
        `test_inf_treated_as_bad_ratio_not_hard_reject`。此处保留「完全不可用」覆盖。
        """
        rec = _cache().put("x", np.full(4, np.inf))
        assert rec.status == SC.STATUS_FAILED

    def test_nan_ratio_boundary_is_inclusive(self):
        """我在实现里用 `> max_nan_ratio` 判失败 → 恰好等于阈值时**通过**。"""
        c = _cache(max_nan_ratio=0.5)
        rec = c.put("x", np.array([1.0, np.nan]))      # ratio = 0.5
        assert rec.status == SC.STATUS_VALID
        rec2 = c.put("y", np.array([1.0, np.nan, np.nan]))   # 0.667 > 0.5
        assert rec2.status == SC.STATUS_FAILED

    def test_failed_value_not_returned_by_get(self):
        c = _cache()
        c.put("x", np.full(2, np.nan))
        assert c.get("x") is None
        assert c.status_of("x") == SC.STATUS_FAILED

    def test_mark_failed_clears_value(self):
        c = _cache()
        c.put("x", np.array([1.0]))
        c.mark_failed("x", "boom")
        assert c.get("x") is None and c.status_of("x") == SC.STATUS_FAILED

    def test_get_missing_counts_miss(self):
        c = _cache()
        assert c.get("nope") is None
        assert c.misses == 1

    def test_hits_counted(self):
        c = _cache()
        c.put("x", np.array([1.0]))
        c.get("x")
        c.get("x")
        assert c.hits == 2

    def test_missing_lists_unready(self):
        c = _cache()
        c.put("a", np.array([1.0]))
        c.put("b", np.full(2, np.nan))
        c.register("c")
        assert sorted(c.missing(["a", "b", "c"])) == ["b", "c"]

    def test_invalidate_single(self):
        c = _cache()
        c.put("x", np.array([1.0]))
        c.invalidate("x")
        assert c.status_of("x") == SC.STATUS_COMPUTING
        assert c.get("x") is None

    def test_discard_clears_everything(self):
        c = _cache()
        c.put("x", np.array([1.0]))
        c.discard("test reason")
        assert c.discarded and c.records == {}
        assert "test reason" in (c.discard_reason or "")


# ══════════════════════════════════════════════════════════
# 5. precompute / assemble：组装零计算（任务卡硬要求）
# ══════════════════════════════════════════════════════════


class TestPrecomputeAndAssemble:
    def _fixture(self):
        batch = SC.extract_batch([
            {"id": "f1", "formula": "mean(close,20)/mean(close,60)-1"},
            {"id": "f2", "formula": "cs_rank(mean(close,20))"},
        ])
        return batch

    def test_precompute_computes_each_unique_once(self):
        batch = self._fixture()
        c = _cache()
        compute = _Compute()
        stats = SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        assert stats["computed"] == batch["unique"]
        assert compute.count == batch["unique"], "同一子表达式不得重复计算"
        assert len(set(compute.calls)) == compute.count

    def test_precompute_skips_ready(self):
        batch = self._fixture()
        c = _cache()
        compute = _Compute()
        SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        before = compute.count
        stats = SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        assert stats["skipped_ready"] == batch["unique"]
        assert compute.count == before

    def test_precompute_force_recomputes(self):
        batch = self._fixture()
        c = _cache()
        compute = _Compute()
        SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        before = compute.count
        SC.precompute(c, batch["unique_texts"], compute_fn=compute, force=True)
        assert compute.count > before

    def test_precompute_isolates_failures(self):
        batch = self._fixture()
        c = _cache()
        target = batch["unique_texts"][0]
        compute = _Compute(raise_on={target})
        stats = SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        assert stats["failed"] == 1
        assert c.status_of(target) == SC.STATUS_FAILED
        assert stats["computed"] == batch["unique"] - 1, "其余仍应算完"

    def test_precompute_marks_nan_as_failed(self):
        batch = self._fixture()
        c = _cache()
        target = batch["unique_texts"][0]
        compute = _Compute(nan_on={target})
        stats = SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        assert stats["failed"] == 1
        assert c.status_of(target) == SC.STATUS_FAILED

    def test_assemble_triggers_zero_compute(self):
        """🚨 任务卡硬要求：组装时 `compute_node` 调用次数为 0。"""
        batch = self._fixture()
        c = _cache()
        compute = _Compute()
        SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        before = compute.count

        value = SC.assemble(c, batch["per_factor"]["f1"])
        assert value is not None
        assert compute.count - before == 0

        out = SC.assemble_batch(c, batch["per_factor"])
        assert out["assembled"] == 2 and out["missing"] == 0
        assert compute.count - before == 0, "批量组装同样不得触发计算"

    def test_assemble_returns_root_value(self):
        c = _cache()
        subs = ["close", "mean(close,20)"]
        c.put("close", np.array([1.0]))
        c.put("mean(close,20)", np.array([9.0]))
        assert SC.assemble(c, subs) == pytest.approx(np.array([9.0]))

    def test_assemble_raises_on_missing(self):
        """缺子表达式 → 抛（**不得静默产出错误因子**）。"""
        c = _cache()
        c.put("close", np.array([1.0]))
        with pytest.raises(SC.CacheMissError) as ei:
            SC.assemble(c, ["close", "mean(close,20)"])
        assert ei.value.missing == ["mean(close,20)"]

    def test_assemble_empty_subexprs(self):
        with pytest.raises(SC.CacheMissError):
            SC.assemble(_cache(), [])

    def test_assemble_batch_reports_missing_factors(self):
        batch = self._fixture()
        c = _cache()
        compute = _Compute()
        # 只算 f2 的子集
        SC.precompute(c, batch["per_factor"]["f2"], compute_fn=compute)
        out = SC.assemble_batch(c, batch["per_factor"])
        assert "f2" in out["values"] and "f1" in out["missing_factors"]
        # 全新缓存里 f2 的 3 个子表达式都要算（无前序共享可复用）
        assert compute.count == len(batch["per_factor"]["f2"])

    def test_assemble_scope_guard(self):
        batch = self._fixture()
        c = _cache(scope=SC.SCOPE_PRESCREEN)
        SC.precompute(c, batch["unique_texts"], compute_fn=_Compute())
        with pytest.raises(SC.ScopeMismatchError):
            SC.assemble(c, batch["per_factor"]["f1"],
                        expected_scope=SC.SCOPE_FULL_VALIDATION)


# ══════════════════════════════════════════════════════════
# 6. 作用域隔离（预筛 / 全量验证不混用）
# ══════════════════════════════════════════════════════════


class TestScopeIsolation:
    def test_registry_separates_scopes(self):
        reg = SC.CacheRegistry(task_id="t1")
        pre = reg.for_scope(SC.SCOPE_PRESCREEN, data_snapshot_version="s1")
        full = reg.for_scope(SC.SCOPE_FULL_VALIDATION, data_snapshot_version="s1")
        assert pre is not full
        assert reg.scopes == sorted([SC.SCOPE_PRESCREEN,
                                     SC.SCOPE_FULL_VALIDATION])

    def test_registry_same_scope_reuses_instance(self):
        reg = SC.CacheRegistry()
        assert reg.for_scope(SC.SCOPE_PRESCREEN, data_snapshot_version="s1") is \
            reg.for_scope(SC.SCOPE_PRESCREEN, data_snapshot_version="s1")

    def test_registry_separates_snapshots(self):
        reg = SC.CacheRegistry()
        assert reg.for_scope(SC.SCOPE_PRESCREEN, data_snapshot_version="s1") is not \
            reg.for_scope(SC.SCOPE_PRESCREEN, data_snapshot_version="s2")

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValueError):
            SC.SubexpressionCache(scope="bogus", data_snapshot_version="s")
        with pytest.raises(ValueError):
            SC.CacheRegistry().for_scope("bogus", data_snapshot_version="s")

    def test_assert_scope_helper(self):
        c = _cache(scope=SC.SCOPE_PRESCREEN)
        SC.assert_scope(c, expected=SC.SCOPE_PRESCREEN)
        with pytest.raises(SC.ScopeMismatchError):
            SC.assert_scope(c, expected=SC.SCOPE_FULL_VALIDATION)

    def test_values_do_not_leak_across_scopes(self):
        """预筛算过的子表达式，全量验证缓存里**仍是 miss**（数据范围不同）。"""
        reg = SC.CacheRegistry()
        pre = reg.for_scope(SC.SCOPE_PRESCREEN, data_snapshot_version="s1")
        full = reg.for_scope(SC.SCOPE_FULL_VALIDATION, data_snapshot_version="s1")
        pre.put("close", np.array([1.0]))
        assert pre.get("close") is not None
        assert full.get("close") is None

    def test_release_clears_caches(self):
        reg = SC.CacheRegistry()
        reg.for_scope(SC.SCOPE_PRESCREEN, data_snapshot_version="s1")
        reg.release()
        assert reg.scopes == []


# ══════════════════════════════════════════════════════════
# 7. 采样校验（哈希碰撞兜底）
# ══════════════════════════════════════════════════════════


class TestVerifySample:
    def _setup(self, *, snapshot: str = "s"):
        batch = SC.extract_batch([
            {"id": f"f{i}", "formula": f}
            for i, f in enumerate([
                "mean(close,20)/mean(close,60)-1",
                "cs_rank(mean(close,20))",
                "mean(close,20)-mean(close,60)",
                "cs_rank(pe_ttm)",
                "close+volume",
                "close-volume",
            ])
        ])
        c = SC.SubexpressionCache(scope=SC.SCOPE_PRESCREEN,
                                  data_snapshot_version=snapshot)
        compute = _Compute()
        SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        assembled = SC.assemble_batch(c, batch["per_factor"])["values"]
        return batch, c, assembled

    def test_consistent_passes(self):
        batch, c, assembled = self._setup()
        res = SC.verify_sample(list(batch["per_factor"]), cache=c,
                               per_factor=batch["per_factor"],
                               direct_compute=lambda k: assembled[k], rng=__import__("random").Random(1))
        assert res["mismatches"] == []
        assert res["discarded"] is False
        assert res["checked"] == res["sampled"]

    def test_mismatch_discards_cache(self):
        batch, c, _ = self._setup()
        res = SC.verify_sample(list(batch["per_factor"]), cache=c,
                               per_factor=batch["per_factor"],
                               direct_compute=lambda k: np.array([999.0]),
                               rng=__import__("random").Random(1))
        assert res["mismatches"]
        assert res["discarded"] is True
        assert c.discarded is True
        assert c.records == {}

    def test_mismatch_without_discard(self):
        batch, c, _ = self._setup()
        res = SC.verify_sample(list(batch["per_factor"]), cache=c,
                               per_factor=batch["per_factor"],
                               direct_compute=lambda k: np.array([999.0]),
                               discard_on_mismatch=False,
                               rng=__import__("random").Random(1))
        assert res["mismatches"] and res["discarded"] is False
        assert c.discarded is False

    def test_tolerance_boundary(self):
        """差异 <= 容差视为一致；> 容差报不一致。"""
        batch, c, assembled = self._setup()
        key = next(iter(batch["per_factor"]))

        def _shifted(k, delta):
            return assembled[k] + delta

        ok = SC.verify_sample([key], cache=c, per_factor=batch["per_factor"],
                              direct_compute=lambda k: _shifted(k, 1e-7),
                              rng=__import__("random").Random(1))
        assert ok["mismatches"] == []

        bad = SC.verify_sample([key], cache=c, per_factor=batch["per_factor"],
                               direct_compute=lambda k: _shifted(k, 1e-3),
                               discard_on_mismatch=False,
                               rng=__import__("random").Random(1))
        assert bad["mismatches"]

    def test_sample_size_capped_by_factors(self):
        batch, c, assembled = self._setup()
        res = SC.verify_sample(list(batch["per_factor"]), cache=c,
                               per_factor=batch["per_factor"],
                               direct_compute=lambda k: assembled[k],
                               sample_size=100,
                               rng=__import__("random").Random(1))
        assert res["sampled"] == len(batch["per_factor"])

    def test_cache_miss_is_skipped_not_mismatch(self):
        """缓存缺失（空缓存 / 子式被质量门禁拒写）→ 归入 `skipped`，不判不一致、不废弃缓存。

        ⚠️ 2026-09-23 语义修正（原断言 `mismatches[0].reason == "cache_miss"` 已废）：
        把「缓存里没有可比对的东西」当成「缓存结果算错了」，会导致
        「废弃整份缓存 → 下一代冷启动 → 再次 cache_miss」的死循环 ——
        而被质量门禁拒绝写入的子表达式**不会因为废弃缓存而变好**。
        真实后果：实测 20 代里只有 3 代 `cache_validation_passed=1`，且每代全量重算。

        安全语义**未放宽**：真实数值差异仍判失败并废弃缓存
        （见 `test_mismatch_discards_cache`）。
        """
        batch = SC.extract_batch([{"id": "f1", "formula": "close+volume"}])
        c = _cache()     # 空缓存 → 组装必缺
        res = SC.verify_sample(["f1"], cache=c, per_factor=batch["per_factor"],
                               direct_compute=lambda k: np.array([1.0]),
                               rng=__import__("random").Random(1))
        assert res["mismatches"] == []
        assert res["skipped"][0]["reason"] == "cache_miss"
        assert res["discarded"] is False
        assert c.discarded is False

    def test_empty_factor_list(self):
        res = SC.verify_sample([], cache=_cache(), per_factor={},
                               direct_compute=lambda k: None)
        assert res["checked"] == 0 and res["discarded"] is False


# ══════════════════════════════════════════════════════════
# 8. 版本不兼容
# ══════════════════════════════════════════════════════════


class TestVersioning:
    def _snapshot(self):
        c = _cache()
        c.put("close", np.array([1.0]))
        return c.snapshot()

    def test_snapshot_shape_and_no_values(self):
        snap = self._snapshot()
        assert snap["schema_version"] == SC.SCHEMA_VERSION
        assert snap["hash_algorithm_version"] == SC.HASH_ALGORITHM_VERSION
        assert snap["records"] and "value" not in snap["records"][0], \
            "快照不持久化数值（内存缓存）"

    def test_restore_ok(self):
        snap = self._snapshot()
        cache, why = SC.restore_cache(snap, scope=SC.SCOPE_PRESCREEN)
        assert why is None
        assert len(cache.records) == 1
        # 值不持久化 → 一律待重算
        assert all(r.status == SC.STATUS_COMPUTING
                   for r in cache.records.values())

    def test_schema_mismatch_discards(self):
        cache, why = SC.restore_cache(dict(self._snapshot(), schema_version=999))
        assert "schema_version" in why
        assert cache.records == {}

    def test_hash_algorithm_mismatch_discards(self):
        cache, why = SC.restore_cache(
            dict(self._snapshot(), hash_algorithm_version="md5-v0"))
        assert "hash_algorithm_version" in why
        assert cache.records == {}

    def test_restore_preserves_scope_and_snapshot(self):
        snap = self._snapshot()
        cache, _ = SC.restore_cache(snap, scope=SC.SCOPE_FULL_VALIDATION)
        assert cache.scope == SC.SCOPE_FULL_VALIDATION
        assert cache.data_snapshot_version == snap["data_snapshot_version"]

    def test_restore_honors_extract_stats(self):
        c = _cache()
        c.record_extraction(total=100, unique=40)
        cache, _ = SC.restore_cache(c.snapshot())
        assert cache.probes()["probe_subexpr_total"] == 100
        assert cache.probes()["probe_subexpr_unique"] == 40


# ══════════════════════════════════════════════════════════
# 9. 探针（向导 §6.11.2）
# ══════════════════════════════════════════════════════════


class TestProbes:
    def test_probe_keys_present(self):
        p = _cache().probes()
        for key in ("probe_subexpr_total", "probe_subexpr_unique",
                    "probe_g2_hit_rate", "probe_subexpr_compute_ms",
                    "probe_g2_compute_calls", "probe_g2_rejected"):
            assert key in p

    def test_hit_rate_math(self):
        c = _cache()
        c.put("x", np.array([1.0]))
        c.get("x")           # hit
        c.get("nope")        # miss
        assert c.probes()["probe_g2_hit_rate"] == pytest.approx(0.5)

    def test_hit_rate_zero_when_no_access(self):
        assert _cache().probes()["probe_g2_hit_rate"] == 0.0

    def test_record_extraction_splits_total_and_unique(self):
        c = _cache()
        c.record_extraction(total=252, unique=102)
        p = c.probes()
        assert p["probe_subexpr_total"] == 252
        assert p["probe_subexpr_unique"] == 102

    def test_compute_probe_accumulates(self):
        batch = SC.extract_batch([{"id": "f1", "formula": "close+volume"}])
        c = _cache()
        SC.precompute(c, batch["unique_texts"], compute_fn=_Compute())
        p = c.probes()
        assert p["probe_g2_compute_calls"] == batch["unique"]
        assert p["probe_subexpr_compute_ms"] >= 0.0


# ══════════════════════════════════════════════════════════
# 10. 与真实模板协作
# ══════════════════════════════════════════════════════════


class TestWithRealTemplates:
    def test_real_25_templates_share_subexpressions(self):
        from app.services.factors.mining import initial_population as IP

        classic = IP.build_initial_population(
            population_size=200,
            selected_fields=["close", "high", "low", "volume", "amount",
                             "turnover_rate", "pe_ttm", "pb", "roe_ttm"],
            template_limit=40).candidates
        batch = SC.extract_batch(classic)
        assert batch["total"] > batch["unique"], "经典模板间应有大量公共子表达式"
        assert batch["dedup_rate"] > 0.3
        # 每个因子的最后一个子表达式是它自身的根
        for c in classic:
            key = str(c.get("id") or c["canonical_formula"])
            assert batch["per_factor"][key][-1] == \
                DD.canonicalize_formula(c["formula"])

    def test_end_to_end_cache_roundtrip_on_real_templates(self):
        from app.services.factors.mining import initial_population as IP

        classic = IP.build_initial_population(
            population_size=200, selected_fields=["close", "volume", "pe_ttm"],
            enabled_categories=["trend", "volume_price"],
            template_limit=20).candidates
        batch = SC.extract_batch(classic)
        c = _cache()
        compute = _Compute()
        SC.precompute(c, batch["unique_texts"], compute_fn=compute)
        before = compute.count
        out = SC.assemble_batch(c, batch["per_factor"])
        assert out["assembled"] == len(classic)
        assert out["missing"] == 0
        assert compute.count == before, "组装必须 0 次计算"
