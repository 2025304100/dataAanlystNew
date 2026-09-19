"""T21 · 性能探针 契约测试（DoD）。

覆盖
====
- **列名与模型一一对应**（R30 式守卫）：9 个 `probe_*` + 2 个 `cache_validation_*`
  必须都能在 `factor_mining_generations` 里找到；且**不漏列**
- **计时语义**：正常计时、同阶段多次进入累加、**异常也计时并原样抛出**、
  嵌套阶段各自计时、未知阶段直接报错
- **归一化**：毫秒取整非负；NaN/Inf/None → 0（**绝不写 NULL**）
- **与 T20 同口径**：`absorb_cache` 从 `SubexpressionCache.probes()` 吸收三指标，
  不重算
- **抽样校验两列**：通过/不通过/无样本三种情形
- **落库**：写入既有代际行；行不存在 → 明确报错；未知列 → 报错
- **零开销**：`enabled=False` 时不取时钟
- **不引入新依赖**（任务卡「明确不做」）
"""
from __future__ import annotations

import math
import pathlib
import re

import pytest

from app.services.factors.mining import performance_probe as PP


# ══════════════════════════════════════════════════════════
# 1. 常量与模型列对齐（R30 式守卫）
# ══════════════════════════════════════════════════════════


class TestColumnsMatchModel:
    def test_six_stages(self):
        assert PP.STAGES == ("data_load", "ast_eval", "subexpr_compute",
                             "factor_assemble", "metric_calc", "db_write")
        assert PP.STAGE_COLUMNS["data_load"] == "probe_data_load_ms"

    def test_probe_columns_count(self):
        assert len(PP.PROBE_COLUMNS) == 9

    def test_every_module_column_exists_in_model(self):
        """🚨 逐名校验：拼错列名的失败形态是 `TypeError`/静默丢列，必须提前挡住。

        注意两个方向用的列集**不同**：
        - 存在性（本用例）→ 模型的**全部**列（`evaluation_duration_ms` 不以 probe_ 开头）
        - 反向覆盖（下一个用例）→ 只看模型的 `probe_*` / `cache_validation_*`
        """
        model_cols = set(PP.generation_model_columns())
        mine = set(PP.PROBE_COLUMNS) | set(PP.VALIDATION_COLUMNS) | {PP.DURATION_COLUMN}
        missing = sorted(mine - model_cols)
        assert missing == [], f"模型里没有这些列：{missing}"

    def test_model_probe_columns_all_covered(self):
        """反向：模型里的 probe_* / cache_validation_* 列**都**要被本模块覆盖。

        防的是「模型加了列但探针没埋」→ 该列永远是默认值，事后无法回填。
        """
        model_cols = set(PP.generation_probe_columns())
        mine = set(PP.PROBE_COLUMNS) | set(PP.VALIDATION_COLUMNS)
        uncovered = sorted(model_cols - mine)
        assert uncovered == [], f"模型有列但探针未覆盖：{uncovered}"

    def test_validate_columns_against_model_passes(self):
        PP.validate_columns_against_model()      # 不抛即通过

    def test_validate_raises_on_unknown_column(self, monkeypatch):
        monkeypatch.setattr(PP, "PROBE_COLUMNS",
                            PP.PROBE_COLUMNS + ("probe_typo_ms",))
        with pytest.raises(ValueError, match="不存在"):
            PP.validate_columns_against_model()


# ══════════════════════════════════════════════════════════
# 2. 计时语义
# ══════════════════════════════════════════════════════════


class _FakeClock:
    """可控时钟（每次调用前进 `step` 毫秒）。"""

    def __init__(self, step_ms: float = 10.0) -> None:
        self.now = 0.0
        self.step = step_ms / 1000.0
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        value = self.now
        self.now += self.step
        return value


class TestTiming:
    def test_stage_records_elapsed(self):
        clock = _FakeClock(step_ms=25.0)
        probe = PP.new_probe(clock=clock)
        with probe.stage("data_load"):
            pass
        # 进入取一次时钟、退出取一次 → 差 25ms
        assert probe.stage_ms("data_load") == pytest.approx(25.0)
        assert probe.as_dict()["stages"]["data_load"]["calls"] == 1

    def test_same_stage_accumulates(self):
        clock = _FakeClock(step_ms=10.0)
        probe = PP.new_probe(clock=clock)
        with probe.stage("db_write"):
            pass
        with probe.stage("db_write"):
            pass
        assert probe.stage_ms("db_write") == pytest.approx(20.0)
        assert probe.as_dict()["stages"]["db_write"]["calls"] == 2

    def test_exception_is_timed_and_reraised(self):
        """🚨 失败的那一代**同样要留下耗时数据**（否则最需要定位的一代没有数据）。"""
        clock = _FakeClock(step_ms=30.0)
        probe = PP.new_probe(clock=clock)
        with pytest.raises(RuntimeError, match="boom"):
            with probe.stage("ast_eval"):
                raise RuntimeError("boom")
        assert probe.stage_ms("ast_eval") == pytest.approx(30.0)

    def test_nested_stages_each_timed(self):
        clock = _FakeClock(step_ms=5.0)
        probe = PP.new_probe(clock=clock)
        with probe.stage("subexpr_compute"):
            with probe.stage("factor_assemble"):
                pass
        assert probe.stage_ms("subexpr_compute") > 0
        assert probe.stage_ms("factor_assemble") > 0

    def test_unknown_stage_rejected(self):
        probe = PP.new_probe()
        with pytest.raises(ValueError, match="未知阶段"):
            with probe.stage("nope"):
                pass

    def test_manual_add(self):
        probe = PP.new_probe()
        probe.add("metric_calc", 12.4)
        probe.add("metric_calc", 7.6)
        assert probe.stage_ms("metric_calc") == pytest.approx(20.0)

    def test_disabled_probe_does_not_touch_clock(self):
        """`enabled=False` 时零开销（不取时钟）。"""
        clock = _FakeClock()
        probe = PP.new_probe(enabled=False, clock=clock)
        with probe.stage("data_load"):
            pass
        assert clock.calls == 0
        assert probe.stage_ms("data_load") == 0.0

    def test_measure_stage_with_none_probe(self):
        with PP.measure_stage(None, "data_load"):
            pass      # 不应抛

    def test_measure_stage_with_probe(self):
        clock = _FakeClock(step_ms=8.0)
        probe = PP.new_probe(clock=clock)
        with PP.measure_stage(probe, "data_load"):
            pass
        assert probe.stage_ms("data_load") == pytest.approx(8.0)

    def test_total_ms_sums_stages(self):
        clock = _FakeClock(step_ms=10.0)
        probe = PP.new_probe(clock=clock)
        with probe.stage("data_load"):
            pass
        with probe.stage("db_write"):
            pass
        assert probe.total_ms == pytest.approx(20.0)


# ══════════════════════════════════════════════════════════
# 3. 归一化（毫秒取整非负，绝不写 NULL）
# ══════════════════════════════════════════════════════════


class TestNormalization:
    @pytest.mark.parametrize("raw,expected", [
        (0, 0), (1.4, 1), (1.6, 2), (12.5, 12), (-3.0, 0),
        (None, 0), ("abc", 0), (float("nan"), 0), (float("inf"), 0),
        (float("-inf"), 0),
    ])
    def test_safe_ms(self, raw, expected):
        assert PP._safe_ms(raw) == expected

    def test_safe_float(self):
        assert PP._safe_float(0.5) == 0.5
        assert PP._safe_float(float("nan")) == 0.0
        assert PP._safe_float(None) == 0.0
        assert PP._safe_float(float("inf"), default=-1.0) == -1.0

    def test_to_columns_all_int_ms_non_negative(self):
        probe = PP.new_probe()
        probe.add("data_load", -5.0)           # 负数 → 0
        probe.add("db_write", 3.7)             # 四舍五入 → 4
        cols = probe.to_columns()
        assert set(cols) == set(PP.PROBE_COLUMNS)
        for stage, column in PP.STAGE_COLUMNS.items():
            assert isinstance(cols[column], int), column
            assert cols[column] >= 0
        assert cols["probe_db_write_ms"] == 4
        assert cols["probe_data_load_ms"] == 0

    def test_g2_hit_rate_is_float(self):
        probe = PP.new_probe()
        probe.g2_hit_rate = 0.375
        cols = probe.to_columns()
        assert isinstance(cols["probe_g2_hit_rate"], float)
        assert cols["probe_g2_hit_rate"] == pytest.approx(0.375)

    def test_missing_stages_default_to_zero_not_null(self):
        """没埋到的阶段写 0（**不是 NULL**）：否则「没埋点」与「耗时 0」不可区分。"""
        cols = PP.new_probe().to_columns()
        assert all(cols[c] == 0 for c in PP.STAGE_COLUMNS.values())


# ══════════════════════════════════════════════════════════
# 4. 与 T20 缓存同口径
# ══════════════════════════════════════════════════════════


class TestAbsorbCache:
    def _cache(self):
        import numpy as np

        from app.services.factors.mining import subexpr_cache as SC

        batch = SC.extract_batch([{"id": "f1", "formula": "close+volume"}])
        cache = SC.SubexpressionCache(scope=SC.SCOPE_PRESCREEN,
                                      data_snapshot_version="s")
        SC.precompute(cache, batch["unique_texts"],
                      compute_fn=lambda t: np.array([1.0, 2.0]))
        cache.get("close")            # 命中
        cache.get("nope")             # 未命中
        return cache

    def test_absorbs_three_cache_metrics(self):
        cache = self._cache()
        probe = PP.new_probe()
        cols = probe.absorb_cache(cache)
        truth = cache.probes()
        assert cols["probe_subexpr_total"] == truth["probe_subexpr_total"]
        assert cols["probe_subexpr_unique"] == truth["probe_subexpr_unique"]
        assert cols["probe_g2_hit_rate"] == pytest.approx(truth["probe_g2_hit_rate"])

    def test_absorbs_subexpr_compute_ms_into_stage(self):
        cache = self._cache()
        probe = PP.new_probe()
        probe.absorb_cache(cache)
        # 缓存给出的 compute_ms 应并入 subexpr_compute 阶段
        assert probe.stage_ms("subexpr_compute") == pytest.approx(
            cache.probes()["probe_subexpr_compute_ms"], abs=1e-6)

    def test_absorb_broken_cache_does_not_raise(self):
        """探针不得因吸收失败而中断挖掘。"""
        class _Broken:
            def probes(self):
                raise RuntimeError("cache exploded")

        probe = PP.new_probe()
        cols = probe.absorb_cache(_Broken())
        assert cols["probe_subexpr_total"] == 0

    def test_absorb_empty_cache(self):
        from app.services.factors.mining import subexpr_cache as SC

        cache = SC.SubexpressionCache(scope=SC.SCOPE_PRESCREEN,
                                      data_snapshot_version="s")
        cols = PP.new_probe().absorb_cache(cache)
        assert cols["probe_subexpr_total"] == 0
        assert cols["probe_g2_hit_rate"] == 0.0


# ══════════════════════════════════════════════════════════
# 5. 抽样校验两列
# ══════════════════════════════════════════════════════════


class TestAbsorbValidation:
    def test_passed(self):
        probe = PP.new_probe()
        probe.absorb_validation({"checked": 5, "mismatches": []})
        cols = probe.to_validation_columns()
        assert cols["cache_validation_passed"] == 1
        assert cols["cache_validation_max_diff"] == 0.0

    def test_failed(self):
        probe = PP.new_probe()
        probe.absorb_validation({"checked": 5, "mismatches": [
            {"factor": "f1", "max_abs_diff": 1e-3},
            {"factor": "f2", "max_abs_diff": 5e-4},
        ]})
        cols = probe.to_validation_columns()
        assert cols["cache_validation_passed"] == 0
        assert cols["cache_validation_max_diff"] == pytest.approx(1e-3)

    def test_no_sample_is_not_passed(self):
        """无样本（checked=0）**不算通过** —— 没校验过就不能声称通过。"""
        probe = PP.new_probe()
        probe.absorb_validation({"checked": 0, "mismatches": []})
        assert probe.to_validation_columns()["cache_validation_passed"] == 0

    def test_uncomparable_shape_counts_as_max_diff(self):
        probe = PP.new_probe()
        probe.absorb_validation({"checked": 1, "mismatches": [
            {"factor": "f1", "reason": "cache_miss"}]})
        cols = probe.to_validation_columns()
        assert cols["cache_validation_passed"] == 0
        assert cols["cache_validation_max_diff"] == PP.INCOMPARABLE_DIFF
        assert math.isfinite(cols["cache_validation_max_diff"]), \
            "必须是有限值：PyMySQL 客户端就拒收 Inf"
        assert cols["cache_validation_max_diff"] <= PP.MYSQL_FLOAT_MAX, \
            "必须落在 MySQL FLOAT 安全区（1e308 会被 1264 Out of range 拒绝）"

    def test_none_result_writes_nothing(self):
        """未收集时不返回该列（避免把既有值覆盖成 NULL）。"""
        probe = PP.new_probe()
        probe.absorb_validation(None)
        assert probe.to_validation_columns() == {}

    def test_end_to_end_with_t20_verify_sample(self):
        import numpy as np

        from app.services.factors.mining import subexpr_cache as SC

        batch = SC.extract_batch([{"id": "f1", "formula": "close+volume"}])
        cache = SC.SubexpressionCache(scope=SC.SCOPE_PRESCREEN,
                                      data_snapshot_version="s")
        SC.precompute(cache, batch["unique_texts"],
                      compute_fn=lambda t: np.array([1.0]))

        ok = SC.verify_sample(["f1"], cache=cache,
                              per_factor=batch["per_factor"],
                              direct_compute=lambda k: (
                                  SC.assemble(cache, batch["per_factor"]["f1"])))
        probe = PP.new_probe()
        probe.absorb_cache(cache)
        probe.absorb_validation(ok)
        cols = probe.to_generation_fields()
        assert cols["cache_validation_passed"] == 1
        assert cols["cache_validation_max_diff"] == 0.0


# ══════════════════════════════════════════════════════════
# 5b. MySQL FLOAT 列范围约束（2026-09-17 真实库实测得出的回归保护）
# ══════════════════════════════════════════════════════════


class TestMysqlFloatColumnLimits:
    """`cache_validation_max_diff` 的真实列型是 MySQL **`float`（单精度）**。

    实测（MySQL 5.7.26，临时表与真实列各验一次）：

    | 值 | DOUBLE 列 | **FLOAT 列（本列）** | SQLite REAL |
    |---|---|---|---|
    | `inf` | ❌ PyMySQL 客户端拒 | ❌ 同样拒 | ✅ |
    | `1e308` | ✅ | **❌ 1264 Out of range** | ✅ |
    | `3e38` | ✅ | ✅ | ✅ |

    ⚠️ **SQLite 测试天然放行这类问题**（REAL 是 8 字节 DOUBLE 且不做范围检查），
    所以这些断言必须**基于列型上界**写，不能靠「单测跑绿」判断安全。
    """

    def test_sentinel_within_float_range(self):
        assert math.isfinite(PP.INCOMPARABLE_DIFF)
        assert PP.INCOMPARABLE_DIFF <= PP.MYSQL_FLOAT_MAX, \
            "哨兵必须 ≤ 单精度上限（3.4028235e38）"

    def test_sentinel_is_large_enough_to_mean_incomparable(self):
        """语义：哨兵要远大于任何真实的绝对值差（否则「不可比较」会与普通差异混淆）。"""
        assert PP.INCOMPARABLE_DIFF >= 1e10

    def test_old_sentinel_1e308_is_now_clamped(self):
        """回归保护：`1e308` 在 FLOAT 列上会被 1264 拒绝 —— 绝不能再出现在写入路径。"""
        assert PP.clamp_to_float_column(1e308) == PP.INCOMPARABLE_DIFF
        assert PP.clamp_to_float_column(1e308) != 1e308

    def test_inf_and_nan_never_reach_column(self):
        assert PP.clamp_to_float_column(float("inf")) == PP.INCOMPARABLE_DIFF
        assert PP.clamp_to_float_column(float("-inf")) == 0.0
        assert PP.clamp_to_float_column(float("nan")) == 0.0

    def test_clamp_keeps_reasonable_values(self):
        assert PP.clamp_to_float_column(0.0) == 0.0
        assert PP.clamp_to_float_column(2e-3) == pytest.approx(2e-3)
        assert PP.clamp_to_float_column(1e10) == pytest.approx(1e10)
        assert PP.clamp_to_float_column(-1.0) == 0.0

    def test_clamp_rejects_non_numeric(self):
        assert PP.clamp_to_float_column("abc") == 0.0
        assert PP.clamp_to_float_column(None) == 0.0

    def test_validation_columns_always_storable(self):
        """任意异常输入经 `to_validation_columns` 后都必须可写入 FLOAT 列。"""
        for raw in (float("inf"), 1e308, 1e39, float("nan"), -5, None):
            probe = PP.new_probe()
            probe.absorb_validation({"checked": 1, "mismatches": [
                {"factor": "f", "max_abs_diff": raw}]})
            cols = probe.to_validation_columns()
            if "cache_validation_max_diff" in cols:
                v = cols["cache_validation_max_diff"]
                assert math.isfinite(v) and 0.0 <= v <= PP.MYSQL_FLOAT_MAX, raw

    def test_reported_sqlite_does_not_enforce_range(self):
        """把「SQLite 为什么发现不了」写成可执行断言（防后人误以为单测已覆盖）。

        SQLite 的 REAL 是 8 字节 DOUBLE 且不做范围检查 → inf/1e308 都能存。
        """
        import sqlite3

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (v REAL)")
            for value in (float("inf"), 1e308):
                conn.execute("INSERT INTO t (v) VALUES (?)", (value,))
            n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            assert n == 2, "SQLite 应能存下 inf 与 1e308 —— 这正是单测的盲区"
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════
# 6. 落库
# ══════════════════════════════════════════════════════════


@pytest.fixture
def gen_row(db_session):
    """建一条 run + generation 行（探针只写既有行）。"""
    from app.models.factor_mining import FactorMiningGeneration, FactorMiningRun

    run = FactorMiningRun(
        id="run-probe-1", status="running",
        candidate_pool_snapshot_id="snap-1",
        data_cutoff_at=__import__("datetime").datetime(2026, 9, 17),
        start_date=__import__("datetime").datetime(2026, 1, 1),
        end_date=__import__("datetime").datetime(2026, 9, 1),
        rebalance_frequency="daily", split_method="ratio",
        split_algorithm_version="v1",
    )
    db_session.add(run)
    db_session.flush()
    gen = FactorMiningGeneration(run_id=run.id, generation=0)
    db_session.add(gen)
    db_session.commit()
    return run.id, gen


class TestWriteGenerationProbe:
    def test_writes_all_probe_columns(self, db_session, gen_row):
        run_id, gen = gen_row
        probe = PP.new_probe()
        probe.add("data_load", 12.0)
        probe.add("metric_calc", 30.4)
        probe.subexpr_total = 252
        probe.subexpr_unique = 102
        probe.g2_hit_rate = 0.875

        fields = PP.write_generation_probe(db_session, run_id=run_id,
                                          generation=0, probe=probe)
        assert set(PP.PROBE_COLUMNS) <= set(fields)
        db_session.refresh(gen)
        assert gen.probe_data_load_ms == 12
        assert gen.probe_metric_calc_ms == 30
        assert gen.probe_subexpr_total == 252
        assert gen.probe_subexpr_unique == 102
        assert gen.probe_g2_hit_rate == pytest.approx(0.875)
        assert gen.evaluation_duration_ms == 42

    def test_writes_validation_columns(self, db_session, gen_row):
        run_id, gen = gen_row
        probe = PP.new_probe()
        probe.absorb_validation({"checked": 3, "mismatches": [
            {"factor": "f", "max_abs_diff": 2e-3}]})
        PP.write_generation_probe(db_session, run_id=run_id, generation=0,
                                  probe=probe)
        db_session.refresh(gen)
        assert gen.cache_validation_passed == 0
        assert gen.cache_validation_max_diff == pytest.approx(2e-3)

    def test_missing_generation_row_raises(self, db_session, gen_row):
        run_id, _ = gen_row
        with pytest.raises(ValueError, match="代际行不存在"):
            PP.write_generation_probe(db_session, run_id=run_id, generation=99,
                                      probe=PP.new_probe())

    def test_unknown_extra_field_raises(self, db_session, gen_row):
        run_id, _ = gen_row
        with pytest.raises(ValueError, match="不存在的列"):
            PP.write_generation_probe(db_session, run_id=run_id, generation=0,
                                      probe=PP.new_probe(),
                                      extra_fields={"no_such_col": 1})

    def test_extra_fields_allowed(self, db_session, gen_row):
        run_id, gen = gen_row
        PP.write_generation_probe(db_session, run_id=run_id, generation=0,
                                  probe=PP.new_probe(),
                                  extra_fields={"population_size": 60})
        db_session.refresh(gen)
        assert gen.population_size == 60

    def test_second_write_does_not_null_out_validation(self, db_session, gen_row):
        """第二次只写探针（未吸收校验）时，**不得**把校验列覆盖成 NULL。"""
        run_id, gen = gen_row
        p1 = PP.new_probe()
        p1.absorb_validation({"checked": 2, "mismatches": []})
        PP.write_generation_probe(db_session, run_id=run_id, generation=0, probe=p1)

        PP.write_generation_probe(db_session, run_id=run_id, generation=0,
                                  probe=PP.new_probe())
        db_session.refresh(gen)
        assert gen.cache_validation_passed == 1, "既有校验结果被 NULL 覆盖了"


# ══════════════════════════════════════════════════════════
# 7. 不引入新依赖（任务卡「明确不做」）
# ══════════════════════════════════════════════════════════


class TestNoNewDependencies:
    def test_module_imports_are_stdlib(self):
        assert PP.import_stdlib_only_check() == []

    def test_source_only_imports_allowed_modules(self):
        """源码级检查：顶层 import 只允许 stdlib + 项目内（延迟 import 不受限）。"""
        src = pathlib.Path(PP.__file__).read_text(encoding="utf-8")
        allowed = ("__future__", "math", "time", "contextlib", "dataclasses",
                   "typing", "sqlalchemy", "app")
        # 只看**模块级** import（行首无缩进）；函数体内的延迟 import 不受限
        # （ORM/项目内模块刻意延迟到调用时导入，避免探针热路径拖入 ORM）
        for line in src.splitlines():
            if not line or line[0].isspace():
                continue
            m = re.match(r"^(?:import|from)\s+([\w.]+)", line.strip())
            if not m:
                continue
            root = m.group(1).split(".")[0]
            assert root in allowed, f"出现了未预期依赖：{line.strip()}"

    def test_no_psutil(self):
        """`psutil` 属 M3（G1 并行）依赖，本任务不得引入。"""
        src = pathlib.Path(PP.__file__).read_text(encoding="utf-8")
        assert "psutil" not in src
