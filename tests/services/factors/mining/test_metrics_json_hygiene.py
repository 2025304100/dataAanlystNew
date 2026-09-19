"""TD3 · metrics_json NaN 字面量合规化哨兵（写入侧统一清洗 + fail-fast 双保险）

**本文件是「哨兵」，不是普通单测。** 它守住 TD3 的三层修复：

  H1  写入侧统一清洗 —— 5 个 `metrics_json` 写入点（factor_evaluator.finalize /
      wp5_eval_task 压力合并 / factor_shadow.record / ridge_model 训练落库 /
      factor_models 离线最小训练）统一走 `db_numeric.clean_json_tree`：
      NaN/±Inf → null（D-F/R33 口径，**不许归 0**），嵌套 dict/list 逐层展开
      （wp5 的 `stress_test` 子 dict 实测有嵌套，`clean_numeric_fields` 只洗顶层不够）。
  H2  fail-fast 双保险 —— 所有写入点 `json.dumps(..., allow_nan=False)`：万一清洗
      被绕过，序列化立即抛 ValueError，**绝不静默写出非标准 JSON**（历史行为：
      `NaN` 字面量落库 → 前端 `JSON.parse` 炸）。
  H3  文本级守卫 —— 5 个写入点源码必须同时含 `clean_json_tree(` 与
      `allow_nan=False`；新增写入点若漏掉其中之一，本测试红。

**读取侧兼容性结论（TD3 前置侦查，2026-09-18）**：null 读回 None 与历史 NaN 读回
`float('nan')` 的行为差异已逐点核对 —— `_as_float`/`_fo` 等读取方对 NaN 本就不做
过滤（NaN 会透传 DTO → 响应序列化出 NaN 字面量 → 前端炸），null 反而是修复；
ridge_model IC 对比分支在 NaN 与 None 下均走「跳过比较」，行为等价。

跑法（DoD①）：

    .venv/Scripts/python.exe -m pytest \
        tests/services/factors/mining/test_metrics_json_hygiene.py -q
"""
from __future__ import annotations

import json
import re
from typing import Any

import numpy as np
import pytest

from app.core.db_numeric import clean_json_tree
from app.services.factors.factor_evaluator import finalize_evaluation_run
from app.services.factors.factor_shadow import (
    ShadowObservationInput,
    record_shadow_observation,
)

pytestmark = pytest.mark.whitebox

# ══════════════════════════════════════════════════════════
# 严格解析：metrics_json 里出现 NaN / Infinity / -Infinity 字面量即炸
# （json.loads 默认接受这三个字面量 —— 那正是历史病灶；parse_constant
#   钩子只在这三个字面量出现时被调用，是「不用正则」的精确判定器）
# ══════════════════════════════════════════════════════════


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON literal: {name}")


def _strict_loads(raw: str) -> Any:
    """严格模式解析 —— 任何 NaN/Infinity 字面量都会抛 ValueError。"""
    return json.loads(raw, parse_constant=_reject_constant)


def _assert_no_special_literals(raw: str) -> Any:
    """断言 raw 是合法 JSON（无特殊字面量）并返回解析结果。"""
    value = _strict_loads(raw)          # 有字面量 → 这里就炸
    assert "NaN" not in raw and "Infinity" not in raw
    return value


def _seed_factor_and_version(db_session, code: str):
    """FK 父行（factor_versions.id 被 EvaluationRun / ShadowObservation non-null 引用）。"""
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion

    factor = Factor(
        code=code,
        name=f"TD3 Hygiene {code}",
        category="test",
        direction="higher_better",
        status="active",
        source_type="daily_bars",
        frequency="daily",
        default_missing_policy="exclude",
        lifecycle_status="testing",
        origin="user",
        is_active=1,
        factor_kind="continuous",
    )
    db_session.add(factor)
    db_session.flush()
    version = FactorVersion(
        factor_id=factor.id, version=1, formula_expr="close/open",
        params_json="{}", postprocess_json=None, direction="higher_better",
        is_latest=1, execution_plan_hash="td3_hygiene_demo",
        formula_ast_json="{}", data_dependencies_json="{}", created_via="manual",
    )
    db_session.add(version)
    db_session.flush()
    return factor, version


# ══════════════════════════════════════════════════════════
# H1 单元：clean_json_tree 递归清洗
# ══════════════════════════════════════════════════════════


class TestCleanJsonTree:
    def test_nan_inf_to_none_recursively(self):
        dirty = {
            "ic": float("nan"),
            "icir": float("inf"),
            "neg": float("-inf"),
            "nested": {"decay": float("nan"), "list": [1.5, float("nan"), "keep"]},
            "tuple_like": (float("inf"), "ok"),
        }
        out = clean_json_tree(dirty)
        assert out["ic"] is None and out["icir"] is None and out["neg"] is None
        assert out["nested"]["decay"] is None
        assert out["nested"]["list"] == [1.5, None, "keep"]
        assert out["tuple_like"] == [None, "ok"]      # tuple → list（与 dumps 一致）

    def test_finite_values_pass_through(self):
        src = {"a": 0.04, "b": 3, "c": "text", "d": True, "e": None,
               "big_but_finite": 1e30, "nested": {"x": -2.5}}
        out = clean_json_tree(src)
        assert out == {"a": 0.04, "b": 3, "c": "text", "d": True, "e": None,
                       "big_but_finite": 1e30, "nested": {"x": -2.5}}

    def test_out_of_range_to_none_not_zero(self):
        # 超单精度界 → None（与 clean_numeric_fields 默认 on_out_of_range=none 对齐）；
        # **绝不归 0** —— IC=0 与「没算出来」语义不同（R33）
        assert clean_json_tree({"v": 1e39})["v"] is None
        assert clean_json_tree({"v": float("nan")})["v"] is None
        assert clean_json_tree({"v": 0.0})["v"] == 0.0          # 真实 0 原样保留

    def test_numpy_scalars_normalized(self):
        out = clean_json_tree({"a": np.float64(float("nan")), "b": np.float64(1.5),
                               "c": np.int64(7)})
        assert out == {"a": None, "b": 1.5, "c": 7}
        assert isinstance(out["b"], float) and isinstance(out["c"], int)

    def test_output_always_strict_dumpable(self):
        dirty = {f"k{i}": v for i, v in enumerate([
            float("nan"), float("inf"), float("-inf"), 1e39,
            {"deep": [float("nan"), {"deeper": (float("inf"),)}]},
        ])}
        raw = json.dumps(clean_json_tree(dirty), allow_nan=False)  # 不许抛
        parsed = _assert_no_special_literals(raw)                  # 双重确认


# ══════════════════════════════════════════════════════════
# H1 服务层：真实写入点（可轻量直调的两处）
# ══════════════════════════════════════════════════════════


class TestWriterEndpoints:
    def test_finalize_evaluation_run_no_nan_literals(self, db_session):
        from app.models.factor_evaluation import EvaluationRun

        _, version = _seed_factor_and_version(db_session, "td3_fin_eval")

        run = EvaluationRun(
            id="eval_td3_nan_1",
            factor_version_id=version.id,
            universe_snapshot_id="usnap_td3",
            target_code="close_5d_ret",
            metrics_json="{}",
        )
        db_session.add(run)
        db_session.flush()

        finalize_evaluation_run(
            db_session,
            run_id="eval_td3_nan_1",
            metrics={
                "ic_mean": float("nan"),
                "icir": float("inf"),
                "turnover": 0.21,
                "stress_test": {                       # 嵌套结构（wp5 同款）
                    "overall_verdict": "unstable",
                    "missing": {"ic_decay_ratio": float("nan")},
                },
            },
            gate_result="warn",
        )
        db_session.flush()

        metrics = _assert_no_special_literals(run.metrics_json)
        assert metrics["ic_mean"] is None and metrics["icir"] is None
        assert metrics["stress_test"]["missing"]["ic_decay_ratio"] is None
        assert metrics["turnover"] == 0.21

    def test_record_shadow_observation_no_nan_literals(self, db_session):
        factor, version = _seed_factor_and_version(db_session, "td3_fin_shadow")
        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date="2026-09-10",
            observed_symbols=10,
            expected_symbols=10,
            completeness_ratio=1.0,
            coverage=1.0,
            ic_value=float("nan"),
            turnover=0.3,
            metrics={"ic": float("nan"), "by_horizon": {"h5": float("inf")}},
        )
        result = record_shadow_observation(db_session, inp=inp)
        raw = result.observation.metrics_json
        metrics = _assert_no_special_literals(raw)
        assert metrics["ic"] is None and metrics["by_horizon"]["h5"] is None


# ══════════════════════════════════════════════════════════
# H3 文本级守卫：5 个写入点必须统一清洗 + fail-fast（防回退/新点漏配）
# ══════════════════════════════════════════════════════════

#: (文件, 该文件 metrics_json 写入行必须命中的锚点片段)
_WRITER_FILES = (
    r"app/services/factors/factor_evaluator.py",
    r"app/services/factors/wp5_eval_task.py",
    r"app/services/factors/factor_shadow.py",
    r"app/services/factors/ridge_model.py",
    r"app/api/routes/factor_models.py",
)


class TestWriterSourceGuard:
    @pytest.mark.parametrize("relpath", _WRITER_FILES)
    def test_writer_uses_clean_json_tree_and_allow_nan_false(self, relpath):
        with open(relpath, encoding="utf-8") as f:
            src = f.read()
        assert "clean_json_tree(" in src, (
            f"{relpath} 未走 clean_json_tree 统一清洗（TD3 H1 契约）")
        assert "allow_nan=False" in src, (
            f"{relpath} 缺 allow_nan=False fail-fast 双保险（TD3 H2 契约）")
        # metrics_json 序列化的第一个参数必须是清洗结果（跨行/任意缩进均命中）
        assert re.search(r"json\.dumps\(\s*clean_json_tree\(", src), (
            f"{relpath} 的 metrics_json 序列化未以 clean_json_tree 为首参，"
            "请人工核对（新增写入点请照抄既有模式）")
