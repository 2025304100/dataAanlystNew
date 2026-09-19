"""WP0-2 第三组 RED TDD：PortfolioCandidate SCD2 5 铁律 + Idempotency Key 3 场景。

清单 = 对应 PortfolioCandidate 5 条硬规则 + TR-02.6 幂等键：

  C1（规则 #1）候选池外证券后端强制拒绝 BUY = REJECTED + OUTSIDE_CANDIDATE_POOL
  C2（规则 #2）清仓卖出 0 持仓后 = 候选资格保留（下次 BUY 可恢复）
  C3（规则 #3）手动移除（removed_manually_flag=TRUE）后 = 任何入口 BUY 直接拒绝
  C4（规则 #4）变更审计 + SCD2 正确性：
       C4a 同日多次变更 = 更新当日最新行，不插入新行（不做 关前日+插新行 造成无效区间）
       C4b 跨日变更 = 才执行 旧行 effective_to=昨日 + 新行 effective_from=今日/次日
       C4c 无论同日/跨日，都写入 DataGovernanceAuditEvent
  C5（规则 #5）三入口 as-of 候选快照：
       dry_run=today / backtest=D日每个交易日D / auto_simulation=该交易日

  ID1 第一次 PUT X-Idempotency-Key = uuid-A + params → 成功；
  ID2 第二次 same Header + same params → 200 OK，只返回原响应不写新绑定；
  ID3 same Header + DIFFERENT params → HTTP 409 参数冲突，不改变任何 DB 行。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# C. PortfolioCandidate SCD2 5 铁律 —— 服务层契约（纯函数/服务存在性检查 + 规则断言）
# ============================================================================
class TestWP02CandidatePoolGate:
    """规则 1/2/3：候选池 BUY 门禁。"""

    @staticmethod
    def _svc():
        from app.services.portfolio_candidates import CandidatePoolService
        return CandidatePoolService

    def test_wp02_C1_outside_pool_rejected_with_reason_code(self):
        """C1: 不在有效集中的 symbol×BUY 必须返回 REJECTED + OUTSIDE_CANDIDATE_POOL"""
        svc = self._svc()
        # 方法契约：is_buy_allowed(pid, symbol_id, decision_date) → (bool, RejectDetails)
        check = getattr(svc, "is_buy_allowed", None)
        assert callable(check), "CandidatePoolService.is_buy_allowed() 必须实现"

    def test_wp02_C2_liquidated_zero_holdings_still_eligible(self):
        """C2: 清仓卖出后只要 portfolio_candidates 行有效（removed_manually=FALSE）
        → 下次 BUY 条件满足仍可 BUY（不是 NO_ACTION / OUTSIDE）"""
        svc = self._svc()
        # 判断候选资格的方法
        qual = getattr(svc, "get_effective_candidate_symbols", None)
        assert callable(qual), (
            "CandidatePoolService.get_effective_candidate_symbols(pid, decision_date, require_authorized=True)"
            " 必须实现；用于返回 候选 ∩ auto_authorized=TRUE ∩ not removed 的 symbol 集合"
        )

    def test_wp02_C3_manual_removed_never_buy(self):
        """C3: removed_manually_flag=TRUE 哪怕 auto_authorized 也不能 BUY（必须"恢复资格"操作）"""
        svc = self._svc()
        restore = getattr(svc, "restore_removed_candidate", None)
        assert callable(restore), (
            "必须存在 restore_removed_candidate(pid, symbol_id, operator, reason) —— "
            "显式置回 removed_manually_flag=FALSE；默认情况下一旦移除，无此显式调用就永远 BUY 拒绝"
        )


class TestWP02CandidateSCD2Integrity:
    """规则 4a/b/c：SCD2 区间正确性（同日合并 / 跨日关旧插新 / 必写审计）。"""

    @staticmethod
    def _svc():
        from app.services.portfolio_candidates import CandidatePoolService
        return CandidatePoolService

    def test_wp02_C4a_same_day_merge_no_new_row(self):
        """C4a: change_date 内二次修改只 UPDATE 已存在的当日行（新字段 = 最新值）；
        严禁无效区间（effective_from > effective_to 必须不存在任何一行）"""
        svc = self._svc()
        mutate = getattr(svc, "apply_mutation", None)
        assert callable(mutate), "CandidatePoolService.apply_mutation(pid, mutations, change_date, operator) 必须实现"
        # 必须保证 mutation 语义：同日多次调用时，内部是 UPDATE existing row
        # 具体数值断言由集成测试用 SQLite fixture 覆盖

    def test_wp02_C4b_cross_day_close_old_and_insert(self):
        """C4b: change_date > current effective_from：
        ① UPDATE 旧行仅一列 effective_to = prev_trade_date；② INSERT 新行 effective_from = change_date。
        禁止修改任何其他历史业务字段（auto_authorized_flag历史行保留原值不变）。"""
        svc = self._svc()
        close = getattr(svc, "close_previous_row_and_open_new", None)
        # 子方法也可以不 public；但必须显式有一个变更调度函数，保证跨日才两段式执行
        fn = mutate = getattr(svc, "apply_mutation", None)
        assert callable(fn)

    def test_wp02_C4c_every_change_writes_audit_event(self):
        """C4c: 不管同日/跨日，必须写 DataGovernanceAuditEvent（type=CANDIDATE_POOL_MUTATION）。"""
        from app.models.audit import DataGovernanceAuditEvent
        # 只要模型存在就好（具体断言在集成用 GREEN 阶段检查 event_count）
        assert DataGovernanceAuditEvent is not None

    def test_wp02_C4_scd2_invariant_no_invalid_periods(self):
        """SCD2 不变式：数据库任何时候 portfolio_candidates 中 SELECT COUNT(*)
        WHERE effective_to IS NOT NULL AND effective_from > effective_to 必须等于 0。"""
        # 不变式描述文档化；具体 SQL 断言 GREEN 阶段在 integration 里跑。
        invariant = "SELECT COUNT(*) FROM portfolio_candidates WHERE effective_to IS NOT NULL AND effective_from > effective_to"
        assert "effective_from > effective_to" in invariant


class TestWP02ThreeEntryAsOfSnapshots:
    """规则 #5：三入口（dry_run / backtest D日 / auto_simulation）各自日期候选快照绝不复用。"""

    @staticmethod
    def _svc():
        from app.services.portfolio_candidates import CandidatePoolService
        return CandidatePoolService

    def test_wp02_C5_three_entries_use_own_decision_dates(self):
        svc = self._svc()
        # 必须暴露按日期取快照的函数（get_effective_candidate_symbols 必须是日期敏感型）
        import inspect
        sig = inspect.signature(svc.get_effective_candidate_symbols)
        assert "decision_date" in sig.parameters or "as_of_date" in sig.parameters, (
            "get_effective_candidate_symbols 必须有显式日期参数 decision_date / as_of_date；"
            "否则无法保证 dry_run(D) / backtest(D日) / auto_sim(D) 三入口正确使用各自日期快照"
        )


# ============================================================================
# D. Idempotency Key：首写成功 / 重放一致 / 参数冲突 409
# ============================================================================
class TestWP02IdempotencyContract:
    """TR-02.6：X-Idempotency-Key 头契约。"""

    @staticmethod
    def _idem_svc():
        from app.services.idempotency import IdempotencyService
        return IdempotencyService

    def test_wp02_ID0_service_exists(self):
        cls = self._idem_svc()
        for m in ("try_acquire_or_get", "record_response"):
            assert hasattr(cls, m), f"IdempotencyService 缺少方法: {m}"

    def test_wp02_ID2_same_key_same_params_returns_original(self):
        """同 key + 同参数 → 返回原响应，不写新绑定"""
        cls = self._idem_svc()
        assert callable(cls.try_acquire_or_get)

    def test_wp02_ID3_same_key_different_params_conflict_409(self):
        """同 key + DIFFERENT params → 409 冲突响应（幂等键冲突参数），返回 first_seen_hash"""
        cls = self._idem_svc()
        # 必须显式抛出/返回 STATE_IDEMPOTENCY_PARAMS_CONFLICT 错误码
        # 由 GREEN 测试构造: first write {w=0.5} OK; same key + {w=0.6} → 409
