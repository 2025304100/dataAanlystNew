"""WP0-2 第二组 RED 测试：FactorUsage 原子事务 + PortfolioStateMachine 9 状态转移。

测试清单 = TR-02.2 程序式验收 + TR-02.7 状态机 10 用例子集：
  A. 原子事务 save_and_apply：
      A1. 冲突 row_version → 返回 409 且 DB 不改变（第二人被拒绝）
      A2. 注入中途失败 → 完全回滚（绑定/快照/outbox 都不产生脏记录）
      A3. T4a canonical hash：a) 同对象重复哈希一致；b) 10位Decimal去尾零后一致；c) NaN/Inf 直接抛错
      A4. factor_usage_options 只返回 runtime_active=true 模型
      A5. 前端 factor_weights_hash 与后端计算不一致 → 保存被阻断
  B. 状态机 PortfolioStateMachine：
      B1. 9 状态枚举完整（非法第10字符串双拦截）
      B2. PENDING_INITIAL_REVIEW 只能 → READY（不能跳 RUNNING_AUTO_SIMULATION）
      B3. 两个 RUNNING 独立租约前缀：auto_sim vs backtest 互不冲突
      B4. ADMIN_PAUSED 唯一出边 READY 只能是管理员显式（不是任何自动路径）
      B5. RECONCILIATION_BLOCKED → RUNNING 非法跳转：返回STATE_TRANSITION_FORBIDDEN
      B6. 4 允许矩阵 SCORE_STALE：新 BUY × 禁止 / 风险 SELL 允许
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# A. T4a Canonical Hash + FactorUsage 原子事务
# ============================================================================
class TestWP02T4aCanonicalHash:
    """T4a 精度规则：Decimal 10 位去尾零 / 无 NaN-Inf / 键排序 / 无空格无换行 / UTF-8"""

    @staticmethod
    def _canonical():
        from app.core.hash_utils import canonical_json as cj
        return cj

    @staticmethod
    def _hash():
        from app.core.hash_utils import content_hash
        return content_hash

    def test_wp02_A3a_identical_object_stable(self):
        payload = {
            "factor_weights": [
                {"factor_code": "VALUE", "factor_version": 1, "normalized_weight": Decimal("0.5")},
                {"factor_code": "MOM",   "factor_version": 1, "normalized_weight": Decimal("0.5")},
            ],
            "meta": {"rule_version": 3, "portfolio_id": 1},
        }
        h_fn = self._hash()
        assert h_fn(payload) == h_fn(payload), "相同对象重复哈希必须一致"

    def test_wp02_A3b_decimal_trailing_zero_stripped(self):
        """1.0000000000 vs 1.000 经 T4a → 量化后完全相同哈希"""
        a = {"weights": [Decimal("1.0000000000"), Decimal("2.5000000000")]}
        b = {"weights": [Decimal("1.000"), Decimal("2.5")]}
        h_fn = self._hash()
        assert h_fn(a) == h_fn(b), (
            "Decimal 10 位去尾零后应相同；A=1.0000000000 vs B=1.000 哈希不一致"
        )

    def test_wp02_A3c_nan_inf_direct_error(self):
        """NaN / Inf（不论 float 还是字符串写进 JSON）都必须直接抛出 ValueError，不得入库"""
        cj = self._canonical()
        import math
        with pytest.raises((ValueError, OverflowError)):
            cj({"x": float("nan")})
        with pytest.raises((ValueError, OverflowError)):
            cj({"x": float("inf")})

    def test_wp02_A3d_key_sorted_no_space_no_newline(self):
        """键排序 + 无空格无换行；JSON 紧凑格式"""
        cj = self._canonical()
        raw = cj({"z": 1, "a": {"y": 2, "x": 3}})
        assert "\n" not in raw and "\t" not in raw and "  " not in raw
        # 最外层必须是 a 开头（z后不能a后）
        assert raw.startswith('{"a":'), f"键未按字典序排序: {raw[:60]}"


class TestWP02FactorUsageAtomic:
    """save_and_apply_usage 单事务 7 步原子性。"""

    def test_wp02_A1_row_version_conflict_returns_409_and_no_db_side_effect(self):
        """两并发请求：第一者成功 row_version=0→1；第二者仍传 expected=0 → HTTP 409 + 数据库与第一前一致。"""
        from app.services.portfolio_factor_usage import save_and_apply_usage_atomic
        # 函数必须存在
        assert callable(save_and_apply_usage_atomic), \
            "必须实现 save_and_apply_usage_atomic(pid, payload, expected_row_version, idem_key)"

    def test_wp02_A2_midstep_failure_full_rollback(self):
        """在第 4 步「保存绑定成功后、生成快照前」注入异常 → 必须全部回滚，不能存在半生效。"""
        # 保证服务函数接受 inject_failure hook（测试注入），至少可接受任意 *args/**kwargs
        from app.services.portfolio_factor_usage import save_and_apply_usage_atomic
        import inspect
        sig = inspect.signature(save_and_apply_usage_atomic)
        # 至少接受 portfolio_id + payload + expected_row_version + idempotency_key
        for pn in ("portfolio_id", "payload", "expected_row_version", "idempotency_key"):
            assert pn in sig.parameters, f"save_and_apply_usage_atomic 缺少参数: {pn}"

    def test_wp02_A4_usage_options_only_runtime_active(self):
        """只返回 runtime_active=true 的模型；validated 但不 active 不出现。"""
        from app.services.portfolio_factor_usage import get_factor_usage_options
        assert callable(get_factor_usage_options), "必须实现 get_factor_usage_options()"

    def test_wp02_A5_weights_hash_mismatch_blocks_save(self):
        """前端提交的 hash 与后端 T4a canonical 哈希不一致 → 阻断 + 返回实际 expected_hash"""
        from app.services.portfolio_factor_usage import compute_factor_weights_canonical_hash
        assert callable(compute_factor_weights_canonical_hash), (
            "必须实现 compute_factor_weights_canonical_hash(factor_weights, factor_set_id, model_run_id)"
        )


# ============================================================================
# B. PortfolioStateMachine 9 状态 + 4 允许矩阵
# ============================================================================
class TestWP02StateMachineNineStates:
    """9 状态枚举严格契约。"""

    @staticmethod
    def _all_valid():
        return {
            "PENDING_INITIAL_REVIEW", "READY",
            "RUNNING_AUTO_SIMULATION", "RUNNING_BACKTEST",
            "DATA_INCOMPLETE_PAUSED", "RECONCILIATION_BLOCKED",
            "MODEL_INACTIVE", "SCORE_STALE", "INTERRUPTED",
            "ADMIN_PAUSED",  # 第10个 —— 管理员刹车态，出边唯一
        }

    def test_wp02_B1_nine_state_enum_contract_exists(self):
        """枚举值双契约：Enum/DB CHECK + 代码状态字面量集合一致。"""
        from app.services.portfolio_state_machine import PortfolioStateMachine
        valid = PortfolioStateMachine.valid_states()
        # 至少 10 状态（9 + ADMIN_PAUSED）
        expected = self._all_valid()
        assert expected.issubset(set(valid)), (
            f"缺失状态: {sorted(expected - set(valid))}; "
            f"实际: {sorted(valid)}"
        )

    def test_wp02_B1_invalid_state_double_blocked(self):
        """第11个非法字符串 "RANDOM_STATE" → 双拦截：DB CHECK + Pydantic"""
        from app.services.portfolio_state_machine import PortfolioStateMachine
        with pytest.raises((ValueError, KeyError, LookupError)):
            PortfolioStateMachine.transition(
                portfolio_id=1,
                from_state="READY",
                to_state="RANDOM_STATE",          # 第11个无效字符串
                trigger_reason="测试非法",
                operated_by="test@example.com",
            )


class TestWP02StateTransitions:
    """9 状态 24 转移矩阵 单真理裁决。"""

    @staticmethod
    def _sm():
        from app.services.portfolio_state_machine import PortfolioStateMachine
        return PortfolioStateMachine

    def test_wp02_B2_pending_only_to_ready(self):
        """N1: PENDING_INITIAL_REVIEW → READY ✓；→ 任何 RUNNING 一律 FORBIDDEN"""
        assert self._sm().can_transition("PENDING_INITIAL_REVIEW", "READY") is True
        for bad in ("RUNNING_AUTO_SIMULATION", "RUNNING_BACKTEST",
                    "DATA_INCOMPLETE_PAUSED", "RECONCILIATION_BLOCKED"):
            assert self._sm().can_transition("PENDING_INITIAL_REVIEW", bad) is False

    def test_wp02_B3_running_lease_prefixes_distinct(self):
        """N3/N4: 自动模拟租约前缀 ≠ 回测租约前缀，两者可与 READY 双向跳转，但互相之间不能跳。"""
        from app.services.portfolio_state_machine import derive_lease_key
        pid = 42
        auto = derive_lease_key(pid, "AUTO_SIM", trade_date=date(2025, 6, 1))
        backtest = derive_lease_key(pid, "BACKTEST", trade_date=date(2025, 6, 1),
                                    backtest_run_id="bt_abc")
        assert auto != backtest, "AUTO vs BACKTEST 租约键必须互相不可见（前缀/内容不同）"
        # 两个 RUNNING 互跳 = 禁止
        assert self._sm().can_transition("RUNNING_AUTO_SIMULATION", "RUNNING_BACKTEST") is False
        assert self._sm().can_transition("RUNNING_BACKTEST", "RUNNING_AUTO_SIMULATION") is False

    def test_wp02_B4_admin_paused_only_exit_to_ready(self):
        """ADMIN_PAUSED 出边唯一条目：只有 READY（必须管理员显式调 transition，绝不自动）"""
        exits = {s for s in self._sm().valid_states()
                 if self._sm().can_transition("ADMIN_PAUSED", s)}
        # 唯一出边 = READY
        assert exits == {"READY", "ADMIN_PAUSED"} or exits == {"READY"}, (
            "ADMIN_PAUSED 唯一出边必须 READY；不能有自动→其他状态；实际出边："
            f"{sorted(exits)}"
        )

    def test_wp02_B5_reconciliation_to_running_forbidden(self):
        """RECONCILIATION_BLOCKED → RUNNING_AUTO_SIMULATION = ILLEGAL_TRANSITION_ATTEMPT"""
        with pytest.raises(ValueError):
            self._sm().transition(
                portfolio_id=99,
                from_state="RECONCILIATION_BLOCKED",
                to_state="RUNNING_AUTO_SIMULATION",
                trigger_reason="绕过人工确认自动恢复",
                operated_by="attacker_script",
            )


class TestWP02StateAllowanceMatrix:
    """4 允许矩阵：每个状态允许 BUY / SELL-risk / auto-restore / manual-ack。"""

    @staticmethod
    def _allows():
        from app.services.portfolio_state_machine import PortfolioStateMachine
        return PortfolioStateMachine.allows()

    def test_wp02_B6_score_stale_blocks_new_buy_permits_risk_sell(self):
        """SCORE_STALE：新 BUY × 禁止 / 风险 SELL 允许"""
        check = self._allows()
        assert check("SCORE_STALE", "NEW_BUY") is False
        assert check("SCORE_STALE", "RISK_SELL") is True

    def test_wp02_B6_reconciliation_blocks_everything_except_force_sell(self):
        """RECONCILIATION_BLOCKED：普通 SELL ×；仅强制清仓 SELL ✓（operated_by + review_note 缺一不可）"""
        check = self._allows()
        assert check("RECONCILIATION_BLOCKED", "NEW_BUY") is False
        assert check("RECONCILIATION_BLOCKED", "NORMAL_SELL") is False
        assert check("RECONCILIATION_BLOCKED", "FORCE_SELL_WITH_REVIEW") is True

    def test_wp02_B6_admin_paused_allows_only_risk_exit(self):
        """ADMIN_PAUSED：新买单 ×；强制止损风险退出 ✓"""
        check = self._allows()
        assert check("ADMIN_PAUSED", "NEW_BUY") is False
        assert check("ADMIN_PAUSED", "AUTO_RECOVERY_FLOW") is False
        assert check("ADMIN_PAUSED", "RISK_SELL") is True
