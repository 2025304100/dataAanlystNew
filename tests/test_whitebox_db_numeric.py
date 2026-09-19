"""`app/core/db_numeric.py` 契约测试（白盒，核心写入安全工具）。

为什么这个测试必须**基于列型上界**写，而不是只看「跑绿」
====================================================
SQLite 的 `REAL` 是 8 字节 DOUBLE **且不做范围检查** —— `NaN` / `inf` / `1e308` / `1e39`
全能写进去。所以「SQLite 单测全绿」**不能证明**写入安全（铁律 R34）。
本文件因此用两类断言把结论钉住：

1. **列型上界断言**：任何输出都必须 ≤ 该列在 MySQL 的真实上界（单精度/双精度）
2. **不变式断言**：`to_db_float` 的输出**永远** `math.isfinite`（或 None）——
   用一批「脏输入」（NaN/±Inf/1e308/1e39/字符串/None/numpy 标量）遍历验证
"""
from __future__ import annotations

import math
import pathlib
import re
import sqlite3

import pytest
from sqlalchemy import Column, Double, Float, Integer, Numeric, String

from app.core import db_numeric as DN


# ══════════════════════════════════════════════════════════
# 1. 常量与实测矩阵
# ══════════════════════════════════════════════════════════


class TestConstants:
    def test_float_max_matches_mysql_single_precision(self):
        # MySQL FLOAT 上限 3.402823466e38
        assert DN.MYSQL_FLOAT_MAX == pytest.approx(3.4e38)

    def test_double_max_matches_ieee754(self):
        assert DN.MYSQL_DOUBLE_MAX == pytest.approx(1.7e308)

    def test_sentinel_within_single_precision(self):
        """🚨 哨兵必须 ≤ 单精度上限：`1e308` 在真实 `float` 列上被 1264 拒（实测）。"""
        assert DN.INCOMPARABLE_DIFF <= DN.MYSQL_FLOAT_MAX
        assert math.isfinite(DN.INCOMPARABLE_DIFF)
        assert DN.INCOMPARABLE_DIFF >= 1e10, "哨兵要大到足以表达「不可比较」"

    def test_measured_matrix_documented(self):
        """把 T21 的 MySQL 实测矩阵写成可读断言（防后人改回 1e308 / inf）。

        | 值 | float 列 | double 列 |
        |---|---|---|
        | NaN / ±Inf | ❌ PyMySQL 客户端拒 | ❌ 同 |
        | 1e308 | ❌ 1264 | ✅ |
        | 3e38 | ✅ | ✅ |
        """
        assert DN.INCOMPARABLE_DIFF != 1e308, "1e308 超单精度上限，会被 1264 拒"
        assert DN.INCOMPARABLE_DIFF < DN.MYSQL_FLOAT_MAX


# ══════════════════════════════════════════════════════════
# 2. is_missing / is_special
# ══════════════════════════════════════════════════════════


class TestPredicates:
    @pytest.mark.parametrize("v", [None, float("nan")])
    def test_is_missing_true(self, v):
        assert DN.is_missing(v) is True

    @pytest.mark.parametrize("v", [0.0, -1.0, float("inf"), "", "x", 1])
    def test_is_missing_false(self, v):
        assert DN.is_missing(v) is False

    def test_is_missing_with_numpy_and_pandas(self):
        import numpy as np

        assert DN.is_missing(np.nan) is True
        assert DN.is_missing(np.float64("nan")) is True
        try:
            import pandas as pd

            assert DN.is_missing(pd.NA) is True
            assert DN.is_missing(pd.NaT) is True
        except ImportError:  # pragma: no cover
            pass

    @pytest.mark.parametrize("v,expected", [
        (float("nan"), True), (float("inf"), True), (float("-inf"), True),
        (0.0, False), (1e308, False), ("abc", False), (None, False),
    ])
    def test_is_special(self, v, expected):
        assert DN.is_special(v) is expected


# ══════════════════════════════════════════════════════════
# 3. column_max：按**列对象**推断真实上界
# ══════════════════════════════════════════════════════════


class TestColumnMax:
    def test_bare_float_is_single_precision(self):
        """SQLAlchemy `mapped_column(Float)`（无 precision）→ MySQL `float` 单精度。"""
        assert DN.column_max(Column("v", Float)) == DN.MYSQL_FLOAT_MAX

    def test_float_with_precision_53_is_double(self):
        assert DN.column_max(Column("v", Float(precision=53))) == DN.MYSQL_DOUBLE_MAX

    def test_double_type(self):
        assert DN.column_max(Column("v", Double)) == DN.MYSQL_DOUBLE_MAX

    def test_numeric_returns_none(self):
        assert DN.column_max(Column("v", Numeric(18, 4))) is None

    def test_integer_returns_none(self):
        assert DN.column_max(Column("v", Integer)) is None

    def test_accepts_bare_type_object(self):
        assert DN.column_max(Float) == DN.MYSQL_FLOAT_MAX
        assert DN.column_max(Double) == DN.MYSQL_DOUBLE_MAX

    def test_float_precision_24_is_single(self):
        assert DN.column_max(Column("v", Float(precision=24))) == DN.MYSQL_FLOAT_MAX


# ══════════════════════════════════════════════════════════
# 4. clamp_to_range
# ══════════════════════════════════════════════════════════


class TestClampToRange:
    @pytest.mark.parametrize("raw,expected", [
        (0.0, 0.0), (0.5, 0.5), (-0.5, -0.5),
        (1e308, DN.MYSQL_FLOAT_MAX), (-1e39, -DN.MYSQL_FLOAT_MAX),
        (float("inf"), DN.MYSQL_FLOAT_MAX), (float("-inf"), -DN.MYSQL_FLOAT_MAX),
        (float("nan"), 0.0), ("abc", 0.0), (None, 0.0),
    ])
    def test_clamp(self, raw, expected):
        assert DN.clamp_to_range(raw) == expected

    def test_custom_bound(self):
        assert DN.clamp_to_range(1e9, max_abs=100.0) == 100.0

    def test_always_finite(self):
        for raw in (float("nan"), float("inf"), float("-inf"), 1e308, -1e308):
            assert math.isfinite(DN.clamp_to_range(raw))


# ══════════════════════════════════════════════════════════
# 5. to_db_float：默认语义（NaN/Inf → None）
# ══════════════════════════════════════════════════════════


class TestToDbFloatDefaults:
    def test_normal_values_pass_through(self):
        assert DN.to_db_float(0.5) == 0.5
        assert DN.to_db_float(-2) == -2.0
        assert DN.to_db_float(0) == 0.0

    def test_nan_becomes_none(self):
        """🚨 默认 NaN → None（**不是 0**）：项目里 NaN 语义是「无效/缺失」，
        而 IC=0 的含义是「无预测力」—— 归 0 会污染统计。"""
        assert DN.to_db_float(float("nan")) is None

    def test_inf_becomes_none(self):
        assert DN.to_db_float(float("inf")) is None
        assert DN.to_db_float(float("-inf")) is None

    def test_none_stays_none(self):
        assert DN.to_db_float(None) is None

    def test_non_numeric_returns_default(self):
        assert DN.to_db_float("abc") is None
        assert DN.to_db_float("abc", default=-1.0) == -1.0
        assert DN.to_db_float([], default=7.0) == 7.0

    def test_numpy_scalar(self):
        import numpy as np

        assert DN.to_db_float(np.float64(1.5)) == 1.5
        assert DN.to_db_float(np.float32(2.5)) == pytest.approx(2.5)
        assert DN.to_db_float(np.nan) is None
        assert DN.to_db_float(np.inf) is None


class TestToDbFloatRange:
    def test_out_of_range_clamped_with_column(self):
        """1e308 写 `float` 列必被 1264 拒 → 归一化必须夹到单精度上限内。"""
        col = Column("best_icir", Float)
        assert DN.to_db_float(1e308, column=col) == DN.MYSQL_FLOAT_MAX
        assert DN.to_db_float(-1e308, column=col) == -DN.MYSQL_FLOAT_MAX

    def test_double_column_allows_1e308(self):
        col = Column("v", Double)
        assert DN.to_db_float(1e308, column=col) == 1e308

    def test_out_of_range_none_policy(self):
        assert DN.to_db_float(1e308, column=Column("v", Float),
                              on_out_of_range="none") is None

    def test_out_of_range_zero_policy(self):
        assert DN.to_db_float(1e308, column=Column("v", Float),
                              on_out_of_range="zero") == 0.0

    def test_no_bound_defaults_to_single_precision(self):
        """无 `column`/`max_abs` → 按**保守的单精度**处理：夹到 3.4e38。

        保守方向安全：这样的值写任何 MySQL 数值列都不会被 1264 拒。
        （第一版在这里直接报错，会让调用方处处 try —— 归一化工具不应把风险推给调用点。）
        """
        assert DN.to_db_float(1e308) == DN.MYSQL_FLOAT_MAX
        assert DN.resolve_bound() == DN.MYSQL_FLOAT_MAX

    def test_bound_priority(self):
        """上界优先级：显式 `max_abs` > 列型推断 > 保守单精度。"""
        col = Column("v", Double)
        assert DN.resolve_bound(column=col, max_abs=100.0) == 100.0
        assert DN.resolve_bound(column=col) == DN.MYSQL_DOUBLE_MAX
        assert DN.resolve_bound() == DN.MYSQL_FLOAT_MAX

    def test_explicit_max_abs(self):
        assert DN.to_db_float(1e5, max_abs=100.0) == 100.0


class TestToDbFloatPolicies:
    def test_on_nan_clamp_gives_zero_or_bound(self):
        assert DN.to_db_float(float("nan"), on_nan="clamp",
                              max_abs=DN.INCOMPARABLE_DIFF) == 0.0

    def test_on_nan_zero(self):
        assert DN.to_db_float(float("nan"), on_nan="zero") == 0.0

    def test_no_keep_policy_by_design(self):
        """刻意没有 `keep` 策略：保留 NaN/Inf 会破坏「输出永远可写」的契约。"""
        assert "keep" not in DN.VALID_SPECIAL_POLICIES
        with pytest.raises(ValueError):
            DN.to_db_float(float("nan"), on_nan="keep")

    def test_every_policy_yields_finite_or_none(self):
        """**契约级**断言：所有策略组合下，输出都不含 NaN/Inf。"""
        for policy in DN.VALID_SPECIAL_POLICIES:
            for raw in (float("nan"), float("inf"), float("-inf"), 1e400, 1e308):
                got = DN.to_db_float(raw, on_nan=policy, on_inf=policy,
                                     on_out_of_range=policy, max_abs=1e38)
                assert got is None or (
                    isinstance(got, float) and math.isfinite(got)
                ), (policy, raw, got)

    def test_on_inf_clamp_gives_sentinel(self):
        """哨兵语义：`cache_validation_max_diff` 这类列用 clamp 表达「不可比较」。"""
        got = DN.to_db_float(float("inf"), on_inf="clamp",
                             max_abs=DN.INCOMPARABLE_DIFF)
        assert got == DN.INCOMPARABLE_DIFF

    def test_on_inf_clamp_negative(self):
        got = DN.to_db_float(float("-inf"), on_inf="clamp",
                             max_abs=DN.INCOMPARABLE_DIFF)
        assert got == -DN.INCOMPARABLE_DIFF

    def test_on_inf_zero(self):
        assert DN.to_db_float(float("inf"), on_inf="zero") == 0.0

    def test_invalid_policy_rejected(self):
        with pytest.raises(ValueError, match="on_nan"):
            DN.to_db_float(1.0, on_nan="bogus")
        with pytest.raises(ValueError, match="on_inf"):
            DN.to_db_float(1.0, on_inf="bogus")
        with pytest.raises(ValueError, match="on_out_of_range"):
            DN.to_db_float(1.0, on_out_of_range="bogus")

    def test_beyond_double_always_none(self):
        assert DN.to_db_float(1e400) is None


# ══════════════════════════════════════════════════════════
# 6. 不变式：输出永远可写（fuzz）
# ══════════════════════════════════════════════════════════


class TestInvariants:
    DIRTY = [
        float("nan"), float("inf"), float("-inf"), 1e308, -1e308, 1e39, -1e39,
        0.0, -0.0, 1.0, -1.0, 1e-300, 3e38, -3e38, None, "", "abc", [], {},
        True, False, 0, 1, -5,
    ]

    def test_output_always_finite_or_none(self):
        """🚨 核心不变式：任何输入下，输出要么是有限 float，要么是 None。"""
        col = Column("v", Float)
        for raw in self.DIRTY:
            got = DN.to_db_float(raw, column=col)
            if got is None:
                continue
            assert isinstance(got, float), raw
            assert math.isfinite(got), (raw, got)
            assert abs(got) <= DN.MYSQL_FLOAT_MAX, (raw, got)

    def test_output_without_column_is_finite(self):
        for raw in self.DIRTY:
            got = DN.to_db_float(raw)          # 默认策略下不需要上界
            if got is None:
                continue
            assert math.isfinite(got), (raw, got)

    def test_cleaned_values_are_sqlite_insertable(self):
        """离线行为验证：清洗后的值能写进 SQLite（真正的 MySQL 验证见 T21 实测）。"""
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (v REAL)")
            for raw in self.DIRTY:
                conn.execute("INSERT INTO t (v) VALUES (?)",
                             (DN.to_db_float(raw, column=Column("v", Float)),))
            rows = conn.execute("SELECT v FROM t").fetchall()
            assert len(rows) == len(self.DIRTY)
            for (v,) in rows:
                assert v is None or math.isfinite(v), v
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════
# 7. clean_numeric_fields（批量）
# ══════════════════════════════════════════════════════════


class TestCleanNumericFields:
    def test_nan_inf_become_none(self):
        out = DN.clean_numeric_fields({
            "ic": float("nan"), "icir": float("inf"), "sharpe": float("-inf"),
            "coverage": 0.85,
        })
        assert out == {"ic": None, "icir": None, "sharpe": None, "coverage": 0.85}

    def test_non_numeric_untouched(self):
        out = DN.clean_numeric_fields({"name": "x", "flag": True, "n": None})
        assert out == {"name": "x", "flag": True, "n": None}

    def test_does_not_mutate_input(self):
        src = {"ic": float("nan")}
        out = DN.clean_numeric_fields(src)
        assert math.isnan(src["ic"]), "原 dict 不得被修改"
        assert out["ic"] is None

    def test_numpy_scalars_converted_to_native(self):
        import numpy as np

        out = DN.clean_numeric_fields({"a": np.float64(1.5), "b": np.int64(3)})
        assert out["a"] == 1.5 and isinstance(out["a"], float)
        assert out["b"] == 3

    def test_out_of_range_with_columns_defaults_to_none(self):
        """批量清洗默认把「超界」视为**不可用**（None）而不是夹成巨大值 ——
        指标里出现 1e308 本身就是数据损坏，标成缺失比伪装成 3.4e38 更诚实。"""
        cols = {"icir": Column("icir", Float)}
        out = DN.clean_numeric_fields({"icir": 1e308}, columns=cols)
        assert out["icir"] is None
        # 需要保留量级时显式选 clamp
        out2 = DN.clean_numeric_fields({"icir": 1e308}, columns=cols,
                                       on_out_of_range="clamp")
        assert out2["icir"] == DN.MYSQL_FLOAT_MAX

    def test_out_of_range_without_columns_becomes_none(self):
        out = DN.clean_numeric_fields({"icir": 1e308})
        assert out["icir"] is None

    def test_on_inf_clamp_sentinel(self):
        out = DN.clean_numeric_fields({"max_diff": float("inf")}, on_inf="clamp")
        assert out["max_diff"] == DN.MYSQL_FLOAT_MAX

    def test_empty_and_none(self):
        assert DN.clean_numeric_fields(None) == {}
        assert DN.clean_numeric_fields({}) == {}

    def test_all_outputs_writable(self):
        """批量清洗后，所有数值字段都必须是有限值或 None。"""
        payload = {"a": float("nan"), "b": float("inf"), "c": 1e308,
                   "d": 1.5, "e": None, "f": "text"}
        out = DN.clean_numeric_fields(payload)
        for k, v in out.items():
            if isinstance(v, float):
                assert math.isfinite(v), (k, v)
        assert out["d"] == 1.5 and out["f"] == "text"


# ══════════════════════════════════════════════════════════
# 8. 工程约束
# ══════════════════════════════════════════════════════════


class TestModuleHygiene:
    def test_stdlib_only(self):
        """不引入新依赖（任务卡口径）：模块级 import 只用 stdlib。"""
        src = pathlib.Path(DN.__file__).read_text(encoding="utf-8")
        allowed = {"__future__", "math", "sys", "typing"}
        for line in src.splitlines():
            if not line or line[0].isspace():
                continue
            m = re.match(r"^(?:import|from)\s+([\w.]+)", line.strip())
            if m:
                assert m.group(1).split(".")[0] in allowed, line.strip()

    def test_public_api_exports(self):
        for name in ("to_db_float", "to_float_or_none", "clean_numeric_fields",
                     "clamp_to_range", "column_max", "is_missing", "is_special",
                     "MYSQL_FLOAT_MAX", "MYSQL_DOUBLE_MAX", "INCOMPARABLE_DIFF"):
            assert name in DN.__all__, name

    def test_performance_probe_reuses_this_module(self):
        """R28：探针的哨兵/上限必须来自本模块（不各持一份常量）。"""
        from app.services.factors.mining import performance_probe as PP

        assert PP.INCOMPARABLE_DIFF == DN.INCOMPARABLE_DIFF
        assert PP.MYSQL_FLOAT_MAX == DN.MYSQL_FLOAT_MAX

    def test_string_column_unaffected_by_column_max(self):
        assert DN.column_max(Column("v", String(10))) is None
