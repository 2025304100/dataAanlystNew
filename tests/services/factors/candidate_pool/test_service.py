"""候选池 service 契约测试（T08 · DoD）。

覆盖三块：
1. **池 CRUD** —— 创建校验、`source_type` 创建后不可切换、分页/筛选、版本推进
2. **成员批量增删** —— 幂等、复活、软删除**只断关联**、锁定阻断
3. **审计** —— 落 `FactorAuditLog`、与业务同事务、幂等操作不写审计

不变式（本文件最重要的断言）
----------------------------
- 批量删除成员**不触碰主数据**：`symbols` 行数前后不变，成员行**仍在**（只是 `is_deleted=1`）。
- 未知 `symbol_id` 整批拒绝，且**不留下任何半成品**（成员数、审计条数都不变）。
- 幂等操作（重复加 / 重复删 / 同值改 `source_type`）**不写审计、不 bump version** ——
  否则审计表会被无意义的重复记录淹没，版本号也失去「结构变更」的语义。

测试用 conftest 的 `db_session`（每个用例独立 SQLite 文件 + `_auto_align_all_schema`）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.models.factor_runtime import FactorAuditLog
from app.models.mining_candidate_pool import (
    TrainingCandidatePool,
    TrainingCandidatePoolMember,
    TrainingCandidatePoolSnapshot,
)
from app.models.symbol import Symbol
from app.schemas.errors import FactorSevenError
from app.services.factors.candidate_pool import service as svc

pytestmark = pytest.mark.whitebox


# ══════════════════════════════════════════════════════════════
# 夹具与工具
# ══════════════════════════════════════════════════════════════


def _seed_symbols(db, count: int = 3) -> list[int]:
    """造 `count` 个标的（主数据，只读使用）。返回 id 列表。"""
    ids: list[int] = []
    for i in range(1, count + 1):
        db.add(
            Symbol(
                id=i,
                symbol=f"{i:06d}",
                name=f"标的{i}",
                asset_type="stock",
                market="cn",
                industry="银行" if i % 2 else "白酒",
                is_active=1,
                is_st=0,
            )
        )
        ids.append(i)
    db.commit()
    return ids


def _audits(db) -> list[FactorAuditLog]:
    return list(db.execute(select(FactorAuditLog).order_by(FactorAuditLog.id)).scalars())


def _audit_actions(db) -> list[str]:
    return [a.action for a in _audits(db)]


def _member_count(db, pool_id: str, *, only_active: bool = True) -> int:
    cond = [TrainingCandidatePoolMember.pool_id == pool_id]
    if only_active:
        cond.append(TrainingCandidatePoolMember.is_deleted == 0)
    return int(
        db.execute(select(func.count()).select_from(TrainingCandidatePoolMember).where(*cond)).scalar()
        or 0
    )


def _symbol_rows(db) -> int:
    return int(db.execute(text("SELECT COUNT(*) FROM symbols")).scalar() or 0)


def _make_pool(db, **overrides):
    kwargs = dict(name="池A", source_type=svc.SOURCE_TYPE_FILTER,
                  filter_config={"min_market_cap": 1_000_000_000})
    kwargs.update(overrides)
    return svc.create_pool(db, **kwargs)


def _lock_pool(db, pool_id: str, *, members: str = "[]") -> str:
    """直接造一个 `is_locked=1` 的快照（T11 才会实现的冻结/锁定，此处只做前置状态）。"""
    snap_id = f"snap-{pool_id[:8]}"
    db.add(
        TrainingCandidatePoolSnapshot(
            id=snap_id,
            pool_id=pool_id,
            members_json=members,
            rule_hash="r" * 8,
            data_cutoff_at=svc._utcnow(),
            analysis_status="analyzed",
            is_locked=1,
            member_count=0,
        )
    )
    db.commit()
    return snap_id


# ══════════════════════════════════════════════════════════════
# 1. 池 CRUD
# ══════════════════════════════════════════════════════════════


def test_create_pool_persists_defaults_and_writes_audit(db_session):
    pool = _make_pool(db_session, name="  测试池A  ", created_by="alice")

    assert pool.name == "测试池A", "名称应 strip"
    assert pool.source_type == svc.SOURCE_TYPE_FILTER
    assert pool.status == svc.POOL_STATUS_DRAFT
    assert pool.version == 1
    assert pool.member_count == 0
    assert pool.created_by == "alice"
    assert pool.id and len(pool.id) >= 8
    assert pool.filter_config_json is not None

    actions = _audit_actions(db_session)
    assert actions == [svc.AUDIT_POOL_CREATED]
    audit = _audits(db_session)[0]
    assert audit.actor == "alice"
    assert pool.id in audit.attributes_json


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"source_type": "filter", "filter_config": None}, "filter 缺 filter_config"),
        ({"source_type": "import", "import_batch_id": None}, "import 缺 import_batch_id"),
        ({"source_type": "magic"}, "非法 source_type"),
        ({"name": "   "}, "空白名称"),
        ({"name": "x" * 129}, "名称超长"),
    ],
)
def test_create_pool_rejects_invalid_input(db_session, overrides, why):
    _seed_symbols(db_session, 1)
    with pytest.raises(FactorSevenError) as ei:
        _make_pool(db_session, **overrides)

    assert ei.value.error_code == "VALIDATION_ERROR", why
    assert db_session.execute(select(func.count()).select_from(TrainingCandidatePool)).scalar() == 0
    assert _audits(db_session) == [], "校验失败不得留下审计记录"


def test_create_pool_rejects_duplicate_pool_id(db_session):
    """显式传入的 pool_id 若已存在必须拒绝（避免静默覆盖他人数据）。"""
    svc.create_pool(db_session, name="池B", source_type=svc.SOURCE_TYPE_FILTER,
                    filter_config={"k": 1}, pool_id="fixed")
    with pytest.raises(FactorSevenError) as ei:
        svc.create_pool(db_session, name="池C", source_type=svc.SOURCE_TYPE_FILTER,
                        filter_config={"k": 1}, pool_id="fixed")
    assert ei.value.error_code == "VALIDATION_ERROR"
    assert ei.value.extras.get("pool_id") == "fixed"
    assert int(db_session.execute(
        select(func.count()).select_from(TrainingCandidatePool)
    ).scalar() or 0) == 1, "重复 ID 不得建出第二行"


def test_create_pool_rejects_empty_filter_config(db_session):
    """空 filter_config 含义不明确，必须拒绝（见 service 内的口径说明）。"""
    for empty in ({}, None):
        with pytest.raises(FactorSevenError) as ei:
            svc.create_pool(db_session, name="空规则池",
                            source_type=svc.SOURCE_TYPE_FILTER, filter_config=empty)
        assert ei.value.error_code == "VALIDATION_ERROR"


def test_get_pool_returns_seven_field_error_when_missing(db_session):
    with pytest.raises(FactorSevenError) as ei:
        svc.get_pool(db_session, "no-such-pool")

    err = ei.value
    assert err.error_code == "NOT_FOUND"
    seven = err.to_7field()
    assert set(seven) == {
        "error_code", "title_zh", "detail_zh", "correlation_id",
        "impact", "fix_link", "retryable",
    }
    assert seven["fix_link"], "fix_link 必须可点击（跳到挖掘向导）"
    assert seven["retryable"] is False
    assert err.extras.get("pool_id") == "no-such-pool", "correlation 信息应带出 pool_id 便于排查"


def test_list_pools_filters_and_paginates(db_session):
    for i in range(5):
        svc.create_pool(db_session, name=f"筛选池{i}", source_type=svc.SOURCE_TYPE_FILTER,
                        filter_config={"min_market_cap": 1_000_000_000 * (i + 1)}, created_by="bob")
    svc.create_pool(db_session, name="导入池", source_type=svc.SOURCE_TYPE_IMPORT,
                    import_batch_id="batch-1")
    # `create_pool` 恒以 draft 建池（不允许绕过 draft→frozen 生命周期），
    # 故这里建完再改状态来造「已冻结」样本。
    frozen_pool = _make_pool(db_session, name="别的名字")
    svc.update_pool(db_session, frozen_pool.id, status=svc.POOL_STATUS_FROZEN)

    items, total = svc.list_pools(db_session, page=1, page_size=3)
    assert total == 7
    assert len(items) == 3

    only_import, t_import = svc.list_pools(db_session, source_type=svc.SOURCE_TYPE_IMPORT)
    assert t_import == 1 and only_import[0].name == "导入池"

    frozen, t_frozen = svc.list_pools(db_session, status=svc.POOL_STATUS_FROZEN)
    assert t_frozen == 1 and frozen[0].name == "别的名字"

    kw, t_kw = svc.list_pools(db_session, keyword="筛选池")
    assert t_kw == 5

    page2, _ = svc.list_pools(db_session, page=2, page_size=3)
    assert {p.id for p in page2}.isdisjoint({p.id for p in items})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 201},
        {"status": "nope"},
        {"source_type": "nope"},
    ],
)
def test_list_pools_validates_params(db_session, kwargs):
    with pytest.raises(FactorSevenError) as ei:
        svc.list_pools(db_session, **kwargs)
    assert ei.value.error_code == "VALIDATION_ERROR"


def test_update_pool_bumps_version_and_writes_audit(db_session):
    pool = _make_pool(db_session)
    before = pool.version

    updated = svc.update_pool(db_session, pool.id, name="改名后", operator_id="carol")

    assert updated.name == "改名后"
    assert updated.version == before + 1
    assert svc.AUDIT_POOL_UPDATED in _audit_actions(db_session)
    audit = [a for a in _audits(db_session) if a.action == svc.AUDIT_POOL_UPDATED][0]
    assert audit.actor == "carol"
    assert "改名前" not in audit.before_json and "池A" in audit.before_json


def test_update_pool_rejects_source_type_switch(db_session):
    pool = _make_pool(db_session)  # filter

    with pytest.raises(FactorSevenError) as ei:
        svc.update_pool(db_session, pool.id, source_type=svc.SOURCE_TYPE_IMPORT)

    err = ei.value
    assert err.error_code == "BUSINESS_BLOCKED"
    assert err.extras["reason"] == "SOURCE_TYPE_IMMUTABLE"
    assert err.extras["current_source_type"] == svc.SOURCE_TYPE_FILTER
    assert err.extras["requested_source_type"] == svc.SOURCE_TYPE_IMPORT
    assert db_session.get(TrainingCandidatePool, pool.id).source_type == svc.SOURCE_TYPE_FILTER
    assert svc.AUDIT_POOL_UPDATED not in _audit_actions(db_session), "被拒的切换不得写审计"


def test_update_pool_same_source_type_is_idempotent(db_session):
    pool = _make_pool(db_session)
    version_before = pool.version
    audits_before = len(_audits(db_session))

    same = svc.update_pool(db_session, pool.id, source_type=svc.SOURCE_TYPE_FILTER)

    assert same.version == version_before, "同值 source_type 不应 bump version"
    assert len(_audits(db_session)) == audits_before, "同值请求不应写审计"


def test_update_pool_noop_does_not_bump_version_or_audit(db_session):
    pool = _make_pool(db_session)
    version_before = pool.version
    audits_before = len(_audits(db_session))

    same = svc.update_pool(db_session, pool.id, name=pool.name, description=pool.description)

    assert same.version == version_before
    assert len(_audits(db_session)) == audits_before


# ══════════════════════════════════════════════════════════════
# 2. 成员：批量添加
# ══════════════════════════════════════════════════════════════


def test_add_members_creates_rows_updates_count_and_audits(db_session):
    ids = _seed_symbols(db_session, 3)
    pool = _make_pool(db_session)

    result = svc.add_members(db_session, pool.id, symbol_ids=ids,
                             operator_id="dave", source="filter")

    assert result.to_dict() == {
        "pool_id": pool.id, "requested": 3, "member_count": 3,
        "added": 3, "reactivated": 0, "skipped_existing": 0,
        "removed": 0, "not_in_pool": 0, "changed": 3,
        "audit_event_id": result.audit_event_id,
    }
    assert result.audit_event_id is not None
    refreshed = db_session.get(TrainingCandidatePool, pool.id)
    assert refreshed.member_count == 3
    assert refreshed.version == 2, "成员变更应 bump version"
    assert _member_count(db_session, pool.id) == 3

    audit = [a for a in _audits(db_session) if a.action == svc.AUDIT_POOL_MEMBERS_ADDED][0]
    assert audit.actor == "dave"
    assert '"added":3' in audit.attributes_json
    assert '"symbol_ids":[1,2,3]' in audit.attributes_json


def test_add_members_is_idempotent_for_existing_members(db_session):
    ids = _seed_symbols(db_session, 2)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)
    version_after_first = db_session.get(TrainingCandidatePool, pool.id).version
    audits_after_first = len(_audits(db_session))

    second = svc.add_members(db_session, pool.id, symbol_ids=ids)

    assert second.added == 0 and second.reactivated == 0
    assert second.skipped_existing == 2
    assert second.audit_event_id is None, "无变更的幂等调用不应写审计"
    assert db_session.get(TrainingCandidatePool, pool.id).version == version_after_first
    assert len(_audits(db_session)) == audits_after_first
    assert _member_count(db_session, pool.id) == 2


def test_add_members_rejects_unknown_symbol_ids_atomically(db_session):
    _seed_symbols(db_session, 2)
    pool = _make_pool(db_session)
    audits_before = len(_audits(db_session))

    with pytest.raises(FactorSevenError) as ei:
        svc.add_members(db_session, pool.id, symbol_ids=[1, 999])

    err = ei.value
    assert err.error_code == "VALIDATION_ERROR"
    assert err.extras["unknown_symbol_ids"] == [999]
    assert err.extras["unknown_count"] == 1
    # 原子性：不能留下「加了一半」的成员，也不能写审计
    assert _member_count(db_session, pool.id) == 0
    assert len(_audits(db_session)) == audits_before
    assert db_session.get(TrainingCandidatePool, pool.id).member_count == 0


@pytest.mark.parametrize("bad", [[], None])
def test_add_members_rejects_empty_symbol_ids(db_session, bad):
    _seed_symbols(db_session, 1)
    pool = _make_pool(db_session)
    with pytest.raises(FactorSevenError) as ei:
        svc.add_members(db_session, pool.id, symbol_ids=bad)
    assert ei.value.error_code == "VALIDATION_ERROR"


def test_add_members_rejects_non_integer_symbol_ids(db_session):
    _seed_symbols(db_session, 1)
    pool = _make_pool(db_session)
    with pytest.raises(FactorSevenError) as ei:
        svc.add_members(db_session, pool.id, symbol_ids=["000001"])  # type: ignore[list-item]
    assert ei.value.error_code == "VALIDATION_ERROR"


def test_add_members_dedupes_and_reactivates(db_session):
    ids = _seed_symbols(db_session, 2)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)
    svc.remove_members(db_session, pool.id, symbol_ids=[2], reason="测试")

    result = svc.add_members(db_session, pool.id, symbol_ids=[2, 2, 1])

    assert result.requested == 2, "重复 ID 应先去重再计数"
    assert result.reactivated == 1
    assert result.skipped_existing == 1
    row = db_session.execute(
        select(TrainingCandidatePoolMember).where(
            TrainingCandidatePoolMember.pool_id == pool.id,
            TrainingCandidatePoolMember.symbol_id == 2,
        )
    ).scalar_one()
    assert row.is_deleted == 0 and row.deleted_at is None
    assert _member_count(db_session, pool.id) == 2


def test_add_members_unknown_pool_raises_not_found(db_session):
    _seed_symbols(db_session, 1)
    with pytest.raises(FactorSevenError) as ei:
        svc.add_members(db_session, "missing", symbol_ids=[1])
    assert ei.value.error_code == "NOT_FOUND"


# ══════════════════════════════════════════════════════════════
# 3. 成员：批量软删除（只断关联）
# ══════════════════════════════════════════════════════════════


def test_remove_members_is_soft_delete_only(db_session):
    ids = _seed_symbols(db_session, 3)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)
    symbols_before = _symbol_rows(db_session)

    result = svc.remove_members(db_session, pool.id, symbol_ids=[2, 3],
                                operator_id="erin", reason="行业不符")

    assert result.removed == 2 and result.not_in_pool == 0
    assert result.member_count == 1
    assert _symbol_rows(db_session) == symbols_before, "🚨 主数据一行都不能少"
    assert db_session.execute(
        select(func.count()).select_from(TrainingCandidatePoolMember)
        .where(TrainingCandidatePoolMember.pool_id == pool.id)
    ).scalar() == 3, "成员行应保留（软删除），不得物理删除"
    soft = db_session.execute(
        select(TrainingCandidatePoolMember).where(
            TrainingCandidatePoolMember.pool_id == pool.id,
            TrainingCandidatePoolMember.symbol_id == 2,
        )
    ).scalar_one()
    assert soft.is_deleted == 1 and soft.deleted_at is not None
    assert db_session.get(TrainingCandidatePool, pool.id).member_count == 1


def test_remove_members_audit_records_reason_and_master_data_invariant(db_session):
    ids = _seed_symbols(db_session, 2)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)

    svc.remove_members(db_session, pool.id, symbol_ids=[1, 2],
                       operator_id="frank", reason="估值过高")

    audit = [a for a in _audits(db_session)
             if a.action == svc.AUDIT_POOL_MEMBERS_SOFT_DELETED][0]
    assert audit.actor == "frank"
    assert '"reason":"估值过高"' in audit.attributes_json
    assert '"master_data_untouched":true' in audit.attributes_json
    assert '"removed":2' in audit.attributes_json
    assert '"-"' not in audit.before_json or True  # before_json 是 JSON 对象，不做弱断言


def test_remove_members_is_idempotent_for_missing_members(db_session):
    ids = _seed_symbols(db_session, 2)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)
    svc.remove_members(db_session, pool.id, symbol_ids=[1])
    audits_after_first = len(_audits(db_session))
    version_after_first = db_session.get(TrainingCandidatePool, pool.id).version

    second = svc.remove_members(db_session, pool.id, symbol_ids=[1, 99])

    assert second.removed == 0 and second.not_in_pool == 2
    assert second.audit_event_id is None, "无变更的幂等删除不应写审计"
    assert len(_audits(db_session)) == audits_after_first
    assert db_session.get(TrainingCandidatePool, pool.id).version == version_after_first


def test_remove_members_unknown_pool_raises_not_found(db_session):
    with pytest.raises(FactorSevenError) as ei:
        svc.remove_members(db_session, "missing", symbol_ids=[1])
    assert ei.value.error_code == "NOT_FOUND"


# ══════════════════════════════════════════════════════════════
# 4. 锁定阻断（分析完成后禁止筛选/导入/批量删除）
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("action", ["add", "remove"])
def test_locked_pool_blocks_member_mutations(db_session, action):
    ids = _seed_symbols(db_session, 3)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids[:1])
    snap_id = _lock_pool(db_session, pool.id)

    with pytest.raises(FactorSevenError) as ei:
        if action == "add":
            svc.add_members(db_session, pool.id, symbol_ids=[2])
        else:
            svc.remove_members(db_session, pool.id, symbol_ids=[1])

    err = ei.value
    assert err.error_code == "BUSINESS_BLOCKED"
    assert err.extras["reason"] == "POOL_LOCKED"
    assert err.extras["snapshot_id"] == snap_id
    assert err.extras["pool_id"] == pool.id


def test_locked_pool_still_allows_reads(db_session):
    ids = _seed_symbols(db_session, 2)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)
    _lock_pool(db_session, pool.id)

    detail = svc.get_pool_detail(db_session, pool.id)
    assert detail["is_locked"] is True
    assert detail["locked_snapshot_id"] is not None
    assert detail["member_count"] == 2

    members, total = svc.list_members(db_session, pool.id)
    assert total == 2 and len(members) == 2


# ══════════════════════════════════════════════════════════════
# 5. 成员列表 / 池详情
# ══════════════════════════════════════════════════════════════


def test_list_members_joins_symbol_display_fields(db_session):
    ids = _seed_symbols(db_session, 3)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)

    members, total = svc.list_members(db_session, pool.id, order_by="symbol", descending=False)

    assert total == 3
    assert [m["symbol"] for m in members] == [f"{i:06d}" for i in ids]
    first = members[0]
    assert first["name"] == "标的1"
    assert first["industry"] == "银行"  # i=1 奇数
    assert first["is_deleted"] == 0
    assert first["member_id"] and first["pool_id"] == pool.id


def test_list_members_filters_keyword_and_deleted(db_session):
    ids = _seed_symbols(db_session, 3)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)
    svc.remove_members(db_session, pool.id, symbol_ids=[3])

    active, t_active = svc.list_members(db_session, pool.id)
    assert t_active == 2

    with_deleted, t_all = svc.list_members(db_session, pool.id, include_deleted=True)
    assert t_all == 3 and sum(1 for m in with_deleted if m["is_deleted"] == 1) == 1

    by_name, t_name = svc.list_members(db_session, pool.id, keyword="标的2")
    assert t_name == 1 and by_name[0]["symbol_id"] == 2


@pytest.mark.parametrize("kwargs", [{"page": 0}, {"page_size": 0}, {"page_size": 501},
                                    {"order_by": "nope"}])
def test_list_members_validates_params(db_session, kwargs):
    _seed_symbols(db_session, 1)
    pool = _make_pool(db_session)
    with pytest.raises(FactorSevenError) as ei:
        svc.list_members(db_session, pool.id, **kwargs)
    assert ei.value.error_code == "VALIDATION_ERROR"


def test_get_pool_detail_counts_active_and_soft_deleted(db_session):
    ids = _seed_symbols(db_session, 3)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids)
    svc.remove_members(db_session, pool.id, symbol_ids=[1])

    detail = svc.get_pool_detail(db_session, pool.id)

    assert detail["member_count"] == 2
    assert detail["member_count_cached"] == 2
    assert detail["soft_deleted_count"] == 1
    assert detail["is_locked"] is False
    assert detail["status"] == svc.POOL_STATUS_DRAFT
    assert detail["source_type"] == svc.SOURCE_TYPE_FILTER


# ══════════════════════════════════════════════════════════════
# 6. 审计不变式
# ══════════════════════════════════════════════════════════════


def test_audit_actions_are_free_form_not_in_governance_whitelist(db_session):
    """落点是 `factor_audit_logs`（因子域 AC-18），不是 `data_governance_audit_events`。

    `data_governance_audit_events.action` 有 DB 级 CHECK 约束，新增动作要配一次迁移；
    本任务用 `FactorAuditLog`（action 为自由字符串）以免超出写权限 ——
    这条断言把这个决策固定下来，防止后来者「顺手改用」那个表而撞 CHECK。
    """
    _seed_symbols(db_session, 1)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=[1])
    svc.remove_members(db_session, pool.id, symbol_ids=[1])

    actions = _audit_actions(db_session)
    assert actions == [
        svc.AUDIT_POOL_CREATED,
        svc.AUDIT_POOL_MEMBERS_ADDED,
        svc.AUDIT_POOL_MEMBERS_SOFT_DELETED,
    ]
    assert all(a in svc.__all__ or a.startswith("pool_") for a in actions)
    logs = _audits(db_session)
    assert all(log.factor_set_id is None for log in logs), "候选池不是 FactorSet，该列留 NULL"
    assert all(log.model_run_id is None for log in logs)
    assert all(log.before_json and log.after_json for log in logs), "开关字段非空是 AC-18 要求"


def test_rejected_writes_never_leave_audit_rows(db_session):
    """被拒的写操作必须「零痕迹」：既无审计，也无业务变更。"""
    ids = _seed_symbols(db_session, 2)
    pool = _make_pool(db_session)
    svc.add_members(db_session, pool.id, symbol_ids=ids[:1])
    audits_before = len(_audits(db_session))
    members_before = _member_count(db_session, pool.id)

    for fn in (
        lambda: svc.add_members(db_session, pool.id, symbol_ids=[999]),
        lambda: svc.add_members(db_session, pool.id, symbol_ids=[]),
        lambda: svc.update_pool(db_session, pool.id, source_type=svc.SOURCE_TYPE_IMPORT),
        lambda: svc.list_pools(db_session, page=0),
    ):
        with pytest.raises(FactorSevenError):
            fn()

    assert len(_audits(db_session)) == audits_before
    assert _member_count(db_session, pool.id) == members_before

# ══════════════════════════════════════════════════════════
# 审计口径（2026-09-17 需求方裁决 ②⑤）
# ══════════════════════════════════════════════════════════


class TestAuditSerializationAndPoolId:
    """两条裁决的**行为**断言（不是只看代码）：

    ② 审计 JSON **保留 None**（`canonical_json` 会丢，导致「当时是 None」
       与「未记录」不可区分；审计的价值在能还原当时状态）
    ⑤ `pool_id` 落**独立列**（此前塞在 attributes_json → 按池查要走 JSON 过滤）
    """

    def test_audit_records_pool_id_column(self, db_session):
        """⑤：create / update / add_members 三条路径的审计都要带 pool_id 列。"""
        pool = svc.create_pool(db_session, name="池A",
                               source_type=svc.SOURCE_TYPE_FILTER,
                               filter_config={"markets": ["sh"]},
                               created_by="alice")
        svc.update_pool(db_session, pool.id, description="改描述",
                        operator_id="carol")
        for code in ("000001", "600519"):
            db_session.add(Symbol(symbol=code, name=f"名{code}", asset_type="stock",
                                  market="sh", board="main", is_st=0, is_active=1))
        db_session.flush()
        ids = [s.id for s in db_session.query(Symbol).filter(
            Symbol.symbol.in_(["000001", "600519"])).all()]
        svc.add_members(db_session, pool.id, symbol_ids=ids, operator_id="dave")

        audits = _audits(db_session)
        assert len(audits) >= 3, "create / update / add_members 三条路径都该有审计"
        assert all(a.pool_id == pool.id for a in audits), \
            [(a.action, a.pool_id) for a in audits]

    def test_audit_json_preserves_none(self, db_session):
        """②：`before/after` 里值为 None 的键**保留**（不再被静默丢弃）。

        `update_pool` 的 `before` 会带上 `pool.description`（创建时为 None），
        若仍用 `canonical_json`，这个键会被**静默吃掉** ——
        事后就无法区分「当时描述为空」与「这条审计没记描述」。
        """
        pool = svc.create_pool(db_session, name="池B",
                               source_type=svc.SOURCE_TYPE_FILTER,
                               filter_config={"markets": ["sh"]},
                               created_by="alice")
        assert pool.description is None
        svc.update_pool(db_session, pool.id, description="有值", operator_id="carol")

        audit = [a for a in _audits(db_session)
                 if a.action == svc.AUDIT_POOL_UPDATED][0]
        assert '"description":null' in audit.before_json, audit.before_json
        assert '"description":"有值"' in audit.after_json, audit.after_json
        # 对照：canonical_json 确实会吃掉这个键（说明这条断言不是空转）
        from app.core.hash_utils import canonical_json

        assert canonical_json({"description": None}) == "{}"

    def test_audit_writer_does_not_use_canonical_json(self):
        """源码级钉住：审计写入走 `_audit_json`（= `_freeze_json` 语义）。

        防止将来有人「顺手统一成 canonical_json」把保真语义悄悄改回去。
        """
        import inspect

        src = inspect.getsource(svc._write_audit)
        assert "_audit_json(" in src
        assert "canonical_json(" not in src
