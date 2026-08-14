"""Part D: 幂等指纹 + 评价口径选择 + 方向取反 纯函数测试。

覆盖用例：
- test_idempotency_different_horizon：同 factor_code，horizon 5 vs 10 → 两个不同 fingerprint
- test_idempotency_same_fingerprint：两次完全相同 payload → 同一个 fingerprint
- test_resolve_evaluation_column：zscore→normalized；仅 winsorize→winsorized；空→raw
- test_direction_lower_better_negates：aligned_features 被取反
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest


class TestIdempotencyFingerprint:
    """compute_payload_fingerprint 幂等性验证。"""

    def test_idempotency_different_horizon(self):
        """同 factor_code，horizon 5 vs 10 → fingerprint 不同（=不同任务）。"""
        from app.services.factors.wp5_eval_task import compute_payload_fingerprint

        base = {
            "factor_code": "idem_001",
            "factor_version_id": None,
            "universe": "all_a_shares",
            "start_date": None,
            "end_date": None,
            "target_horizon": 5,
            "n_groups": 5,
            "cost_rate": 0.001,
            "direction": "higher_better",
        }
        fp_h5 = compute_payload_fingerprint(base)
        base_h10 = dict(base)
        base_h10["target_horizon"] = 10
        fp_h10 = compute_payload_fingerprint(base_h10)

        assert isinstance(fp_h5, str)
        assert len(fp_h5) == 64, "SHA256 hex should be 64 chars"
        # 关键断言：两个 horizon 必须是不同 fingerprint
        assert fp_h5 != fp_h10, (
            "horizon 5 vs 10 应该产出不同 fingerprint，但两者相同"
        )

    def test_idempotency_same_fingerprint(self):
        """两次完全相同 payload（key 顺序不同、有 None 字段）→ fingerprint 相同。"""
        from app.services.factors.wp5_eval_task import compute_payload_fingerprint

        # 顺序不同，key 相同 + None 键顺序不同 + 日期是 date vs 字符串
        payload_a = {
            "factor_code": "idem_same",
            "factor_version_id": None,
            "universe": "all_a_shares",
            "start_date": date(2026, 1, 5),
            "end_date": None,
            "target_horizon": 5,
            "n_groups": 5,
            "cost_rate": 0.001,
            "direction": "higher_better",
            # 不在幂等 key 中的字段应忽略
            "created_by": "alice",
            "factor_kind": "continuous",
        }
        payload_b = {
            "created_by": "bob",     # 不同 created_by 不应影响
            "factor_kind": "event",  # 不同 factor_kind 不在幂等键
            "target_horizon": 5,
            "factor_code": "idem_same",
            "cost_rate": 0.001,
            "n_groups": 5,
            "end_date": None,
            "direction": "higher_better",
            "factor_version_id": None,
            "universe": "all_a_shares",
            # date 对象 vs isoformat string → 规范化后应一致
            "start_date": "2026-01-05",
        }
        fp_a = compute_payload_fingerprint(payload_a)
        fp_b = compute_payload_fingerprint(payload_b)
        assert fp_a == fp_b, (
            "两个语义相同但 key 顺序/created_by 不同的 payload 应该得到相同 fingerprint"
            f"\nA: {json.dumps(payload_a, default=str, sort_keys=True)}"
            f"\nB: {json.dumps(payload_b, default=str, sort_keys=True)}"
        )

        # 同 fingerprint 对应同一个 create_evaluation_task 返回（API 契约：同 payload 两次调用不创建两个 queued 任务）
        # 这里只验证纯函数，不在 DB 层模拟 worker 调度
        assert isinstance(fp_a, str)
        assert len(fp_a) == 64


class TestResolveEvaluationColumn:
    """resolve_evaluation_column 评价口径选择。"""

    def test_resolve_evaluation_column(self):
        """zscore/rank→normalized；仅 winsorize→winsorized；空/None→raw。"""
        from app.services.factors.wp5_eval_task import resolve_evaluation_column

        # 规则 1：zscore OR rank → normalized
        assert resolve_evaluation_column({"steps": [{"name": "zscore"}]}) == "normalized_value"
        assert resolve_evaluation_column({"zscore": True}) == "normalized_value"
        assert resolve_evaluation_column({"steps": [{"name": "winsorize"}, {"name": "rank"}]}) == "normalized_value"
        # 规则 2：仅 winsorize（无 zscore/rank）→ winsorized
        assert resolve_evaluation_column({"steps": [{"name": "winsorize"}]}) == "winsorized_value"
        assert resolve_evaluation_column({"winsorize": {"lower": 0.01, "upper": 0.01}}) == "winsorized_value"
        # 规则 3：空 → raw
        assert resolve_evaluation_column(None) == "raw_value"
        assert resolve_evaluation_column({}) == "raw_value"
        assert resolve_evaluation_column({"steps": []}) == "raw_value"
        # 规则 4：不识别的 steps 不影响 → raw
        assert resolve_evaluation_column({"steps": [{"name": "neutralize"}]}) == "raw_value"

        # 混合：winsorize + zscore = normalized（zscore 优先）
        assert resolve_evaluation_column({
            "winsorize": {"q": 0.02},
            "zscore": True,
        }) == "normalized_value"

        # 边界：不支持的旧格式不崩
        assert resolve_evaluation_column("not a dict") == "raw_value"  # type: ignore[arg-type]


class TestDirectionAlignment:
    """apply_direction_alignment 方向统一契约。"""

    def _make_features(self):
        idx = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
        cols = ["S0001", "S0002", "S0003"]
        data = np.array([
            [0.5, -0.3, 0.0],
            [1.2, 0.4, -0.7],
            [-0.2, 2.0, 1.0],
        ])
        return pd.DataFrame(data, index=idx, columns=cols)

    def test_direction_lower_better_negates(self):
        """lower_better → -aligned_features，blockers 为空。"""
        from app.services.factors.wp5_eval_task import apply_direction_alignment

        feats = self._make_features()
        adjusted, blockers = apply_direction_alignment(feats, "lower_better")

        # 取反：adjusted 应该等于 -feats
        pd.testing.assert_frame_equal(adjusted, -feats, check_names=True)
        # 不应该有非空 blocker
        assert len(blockers) == 0, "lower_better 不应产生 blocker"

    def test_direction_higher_better_preserved(self):
        """higher_better → 原值不变（copy，不是同一引用）。"""
        from app.services.factors.wp5_eval_task import apply_direction_alignment

        feats = self._make_features()
        adjusted, blockers = apply_direction_alignment(feats, "higher_better")
        pd.testing.assert_frame_equal(adjusted, feats)
        assert adjusted is not feats  # 必须是 copy
        assert len(blockers) == 0

    def test_direction_nonlinear_emits_warn(self):
        """nonlinear → 返回不变 + 1 个 warn blocker（门禁降级）。"""
        from app.services.factors.wp5_eval_task import apply_direction_alignment

        feats = self._make_features()
        adjusted, blockers = apply_direction_alignment(feats, "nonlinear")
        pd.testing.assert_frame_equal(adjusted, feats)
        assert len(blockers) >= 1
        b = blockers[0]
        assert b.get("severity") == "warn"
        assert "nonlinear" in b.get("code", "")
        assert b["evidence"].get("direction") == "nonlinear"

    def test_direction_unknown_warns(self):
        """未知字符串 → 返回不变 + 1 个 warn blocker。"""
        from app.services.factors.wp5_eval_task import apply_direction_alignment

        feats = self._make_features()
        adjusted, blockers = apply_direction_alignment(feats, "upside_down")
        pd.testing.assert_frame_equal(adjusted, feats)
        assert any(b.get("severity") == "warn" for b in blockers)

    def test_direction_case_and_trim(self):
        """大小写和空格处理：' Higher_Better ' 等同于 'higher_better'。"""
        from app.services.factors.wp5_eval_task import apply_direction_alignment

        feats = self._make_features()
        adjusted, blockers = apply_direction_alignment(feats, " Higher_Better ")
        pd.testing.assert_frame_equal(adjusted, feats)
        assert len(blockers) == 0

        adjusted2, blockers2 = apply_direction_alignment(feats, "  Lower_Better  ")
        pd.testing.assert_frame_equal(adjusted2, -feats)
        assert len(blockers2) == 0

    def test_direction_empty_fallback(self):
        """None / '' → 默认 higher_better，不产生方向 blocker（不崩）。"""
        from app.services.factors.wp5_eval_task import apply_direction_alignment

        feats = self._make_features()
        adjusted_none, b_none = apply_direction_alignment(feats, None)
        adjusted_empty, b_empty = apply_direction_alignment(feats, "")
        pd.testing.assert_frame_equal(adjusted_none, feats)
        pd.testing.assert_frame_equal(adjusted_empty, feats)
        # None → 默认 higher_better，不应有 warn（向后兼容）
        assert all(b is None or b.get("severity") != "warn" for b in b_none), (
            "None 方向应默认 higher_better，不该 warn"
        )
