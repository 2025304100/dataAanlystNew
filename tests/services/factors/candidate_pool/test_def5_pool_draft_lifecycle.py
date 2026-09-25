"""DEF-5/DEF-10 回归 · 候选池生命周期 + 草稿删除（2026-09-24 全面测试报告）。

哨兵：
1. `delete_pool` 删除池 + 成员 + 快照，并写审计；
2. 快照正被**进行中**的挖掘 run 引用 → 409 BUSINESS_BLOCKED（禁止删）；
3. 池不存在 → NOT_FOUND；
4. `find_reusable_pool` 同名+同 rule_hash 命中 / 异名或异规则不命中 / 作废池不复用；
5. `delete_draft` 删除草稿并清校验运行；不存在 → deleted=False（幂等 404 由路由转）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models.factor_mining import FactorDataValidationRun, FactorMiningDraft
from app.models.factor_mining import FactorMiningRun
from app.models.mining_candidate_pool import (
    TrainingCandidatePool,
    TrainingCandidatePoolMember,
    TrainingCandidatePoolSnapshot,
)
from app.services.factors.candidate_pool import service as PS
from app.services.factors.mining import draft_service as DS

pytestmark = pytest.mark.whitebox


def _make_pool(db_session, pool_id: str, name: str = "因子挖掘候选池", *,
               rule_hash: str = "rh-1", status: str = "draft") -> TrainingCandidatePool:
    pool = TrainingCandidatePool(
        id=pool_id, name=name, source_type="filter", status=status,
        version=1, member_count=0, rule_hash=rule_hash,
    )
    db_session.merge(pool)
    db_session.commit()
    return pool


def _make_snapshot(db_session, pool_id: str, snap_id: str) -> str:
    db_session.merge(TrainingCandidatePoolSnapshot(
        id=snap_id, pool_id=pool_id, members_json="[]", rule_hash="rh-1",
        data_cutoff_at=datetime(2026, 11, 10), is_locked=1, member_count=0,
    ))
    db_session.commit()
    return snap_id


# ══════════════════════════════════════════════════════════
# 1. 池删除（DEF-5）
# ══════════════════════════════════════════════════════════


def test_delete_pool_removes_members_and_snapshots(db_session):
    """哨兵 1：删池 → 成员与快照一并删除（此前无任何删除端点）。"""
    _make_pool(db_session, "pool-del")
    _make_snapshot(db_session, "pool-del", "snap-del")
    db_session.merge(TrainingCandidatePoolMember(pool_id="pool-del", symbol_id=1))
    db_session.merge(TrainingCandidatePoolMember(pool_id="pool-del", symbol_id=2))
    db_session.commit()

    out = PS.delete_pool(db_session, pool_id="pool-del", actor="tester")

    assert out["status"] == "deleted"
    assert out["deleted_members"] == 2
    assert out["deleted_snapshots"] == 1
    db_session.expire_all()
    assert db_session.get(TrainingCandidatePool, "pool-del") is None
    assert db_session.get(TrainingCandidatePoolSnapshot, "snap-del") is None
    assert db_session.query(TrainingCandidatePoolMember).filter_by(
        pool_id="pool-del").count() == 0


def test_delete_pool_blocked_when_snapshot_used_by_active_run(db_session):
    """哨兵 2：快照被进行中的 run 引用 → 409（不让 run 的数据来源悬空）。"""
    from app.schemas.errors import FactorSevenError

    _make_pool(db_session, "pool-busy")
    _make_snapshot(db_session, "pool-busy", "snap-busy")
    db_session.merge(FactorMiningRun(
        id="run-busy", status="running",
        candidate_pool_snapshot_id="snap-busy",
        data_cutoff_at=datetime(2026, 11, 10), start_date=datetime(2026, 1, 5),
        end_date=datetime(2026, 11, 1), rebalance_frequency="daily",
    ))
    db_session.commit()

    with pytest.raises(FactorSevenError) as ei:
        PS.delete_pool(db_session, pool_id="pool-busy")
    assert ei.value.error_code == "BUSINESS_BLOCKED"
    assert "run-busy" in (ei.value.detail_zh or "")
    # 未删除
    assert db_session.get(TrainingCandidatePool, "pool-busy") is not None


def test_delete_pool_missing_is_not_found(db_session):
    """哨兵 3：池不存在 → NOT_FOUND（路由层转 404）。"""
    from app.schemas.errors import FactorSevenError

    with pytest.raises(FactorSevenError) as ei:
        PS.delete_pool(db_session, pool_id="no-such-pool")
    assert ei.value.error_code == "NOT_FOUND"


# ══════════════════════════════════════════════════════════
# 2. 池复用（DEF-5：杜绝同名垃圾池）
# ══════════════════════════════════════════════════════════


def test_find_reusable_pool_hits_same_name_and_rule(db_session):
    """哨兵 4：同名 + 同 rule_hash → 命中（前端固定名反复点生成不再建新池）。"""
    _make_pool(db_session, "pool-a", rule_hash="rh-x")
    found = PS.find_reusable_pool(db_session, name="因子挖掘候选池",
                                  rule_hash="rh-x", source_type="filter")
    assert found is not None and found.id == "pool-a"


def test_find_reusable_pool_misses_on_different_hash_or_name(db_session):
    """哨兵 5：规则变了（rule_hash 不同）/ 名字不同 → 不复用（必须建新池）。"""
    _make_pool(db_session, "pool-b", rule_hash="rh-x")
    assert PS.find_reusable_pool(db_session, name="因子挖掘候选池",
                                 rule_hash="rh-other",
                                 source_type="filter") is None
    assert PS.find_reusable_pool(db_session, name="别的池", rule_hash="rh-x",
                                 source_type="filter") is None


def test_find_reusable_pool_skips_invalidated(db_session):
    """哨兵 6：已作废池不复用（规则被判定不可用）。"""
    _make_pool(db_session, "pool-c", rule_hash="rh-x", status="invalidated")
    assert PS.find_reusable_pool(db_session, name="因子挖掘候选池",
                                 rule_hash="rh-x", source_type="filter") is None


# ══════════════════════════════════════════════════════════
# 3. 草稿删除（DEF-5）
# ══════════════════════════════════════════════════════════


def test_delete_draft_removes_row_and_validation_runs(db_session):
    """哨兵 7：删草稿 → 草稿行 + 其校验运行记录一起清（此前无删除端点）。"""
    db_session.merge(FactorMiningDraft(
        id="draft-del", name="草稿", status="draft", current_step=2,
        owner="local_user",
    ))
    db_session.merge(FactorDataValidationRun(
        id="vr-del", draft_id="draft-del", config_hash="h", status="passed",
    ))
    db_session.commit()

    out = DS.delete_draft(db_session, draft_id="draft-del")

    assert out["deleted"] is True
    assert out["deleted_validation_runs"] >= 1
    db_session.expire_all()
    assert db_session.get(FactorMiningDraft, "draft-del") is None
    assert db_session.get(FactorDataValidationRun, "vr-del") is None


def test_delete_draft_missing_is_idempotent(db_session):
    """哨兵 8：草稿不存在 → deleted=False（幂等，路由层转 404）。"""
    out = DS.delete_draft(db_session, draft_id="no-such-draft")
    assert out["deleted"] is False
    assert out["reason"] == "draft_not_found"
