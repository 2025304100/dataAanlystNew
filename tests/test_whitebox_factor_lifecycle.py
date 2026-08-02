"""WP1-06: 因子生命周期状态机白盒测试。

覆盖：
- 状态机迁移规则（合法/非法迁移）
- 因子草稿创建和版本管理
- 状态迁移执行（actor/reason/审计/幂等）
- TransitionAudit 追加式不可变
- 旧字段向后兼容
- 版本不可变性（引用后不可修改）

对齐 docs/专业因子库开发计划.md §WP1-05/06 和 docs/因子设置与专业因子库改造方案.md §5。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.factor import Factor
from app.models.factor_evaluation import EvaluationRun, TransitionAudit
from app.schemas.factor_library import (
    FactorDraftCreate,
    FactorTransitionRequest,
    FactorVersionCreate,
)
from app.services.factors.factor_lifecycle import (
    execute_transition,
    get_transition_history,
    validate_transition,
)
from app.services.factors.factor_registry import (
    check_version_immutable,
    create_factor_draft,
    create_factor_version,
    list_factor_versions,
)

pytestmark = pytest.mark.whitebox


# ── 工具函数 ──────────────────────────────────────────────


def _make_draft(db_session, code: str = "test_factor", **kwargs) -> Factor:
    """创建一个草稿因子，返回 Factor ORM 实例。"""
    draft = FactorDraftCreate(
        code=code, name=f"Factor {code}", category="sentiment", **kwargs
    )
    return create_factor_draft(db_session, draft=draft)


def _make_version(db_session, factor_id: int, formula: str = "close", **kwargs):
    """创建一个因子版本，返回 FactorVersion ORM 实例。"""
    request = FactorVersionCreate(formula_expr=formula, **kwargs)
    return create_factor_version(db_session, factor_id=factor_id, request=request)


def _count_audits(db_session, factor_id: int) -> int:
    return len(
        db_session.execute(
            select(TransitionAudit).where(TransitionAudit.factor_id == factor_id)
        ).scalars().all()
    )


# ── 1. validate_transition（状态机规则）────────────────────


def test_validate_draft_to_candidate_allowed():
    """draft → candidate（submit_candidate）是合法迁移。"""
    is_valid, error = validate_transition(
        current_status="draft", action="submit_candidate"
    )
    assert is_valid is True
    assert error is None


def test_validate_draft_to_testing_forbidden():
    """draft 不能直接进入 testing，必须先经过 candidate。"""
    is_valid, error = validate_transition(
        current_status="draft", action="start_testing"
    )
    assert is_valid is False
    assert error == "transition_forbidden"


def test_validate_candidate_to_testing_allowed():
    """candidate → testing（start_testing）是合法迁移。"""
    is_valid, error = validate_transition(
        current_status="candidate", action="start_testing"
    )
    assert is_valid is True
    assert error is None


def test_validate_deprecated_is_terminal():
    """deprecated 是终态，不允许任何迁出。"""
    is_valid, error = validate_transition(
        current_status="deprecated", action="submit_candidate"
    )
    assert is_valid is False
    assert error == "transition_forbidden"


def test_validate_rejected_to_draft_allowed():
    """rejected 只能通过 revoke_to_draft 回到 draft（再创建新版本）。"""
    is_valid, error = validate_transition(
        current_status="rejected", action="revoke_to_draft"
    )
    assert is_valid is True
    assert error is None


# ── 2. create_factor_draft（注册表）────────────────────────


def test_create_factor_draft_sets_lifecycle_status(db_session):
    """草稿创建后 lifecycle_status='draft'、origin='user'、is_active=0。"""
    factor = _make_draft(db_session, code="my_factor")
    db_session.commit()

    assert factor.lifecycle_status == "draft"
    assert factor.origin == "user"
    assert factor.is_active == 0
    assert factor.status == "draft"  # legacy compat


def test_create_factor_draft_rejects_duplicate_code(db_session):
    """重复 code 创建草稿应抛出 ValueError。"""
    _make_draft(db_session, code="dup_factor")
    db_session.flush()

    with pytest.raises(ValueError, match="factor_code_conflict"):
        _make_draft(db_session, code="dup_factor")


def test_create_factor_draft_validates_code_format(db_session):
    """非法 code 格式应抛出 ValidationError。

    注意：FactorDraftCreate.code_lowercase_snake validator 会先将大写转小写，
    所以纯大写不会报错；但空格、连字符、数字开头等不符合 pattern 的会报错。
    """
    # 空格不符合 pattern（大写+空格 → 小写后仍含空格）
    with pytest.raises(ValidationError):
        FactorDraftCreate(code="Bad Factor", name="Bad", category="sentiment")
    # 数字开头不符合 pattern
    with pytest.raises(ValidationError):
        FactorDraftCreate(code="1bad", name="Bad", category="sentiment")
    # 连字符不符合 pattern
    with pytest.raises(ValidationError):
        FactorDraftCreate(code="bad-factor", name="Bad", category="sentiment")


# ── 3. create_factor_version（注册表）──────────────────────


def test_create_factor_version_sets_is_latest(db_session):
    """创建版本 2 后，版本 2 的 is_latest=1，版本 1 的 is_latest=0。"""
    factor = _make_draft(db_session, code="ver_factor")
    db_session.flush()

    v1 = _make_version(db_session, factor.id, formula="close")
    db_session.flush()
    v2 = _make_version(db_session, factor.id, formula="open")
    db_session.flush()

    assert v1.version == 1
    assert v2.version == 2
    assert v1.is_latest == 0
    assert v2.is_latest == 1


def test_create_factor_version_idempotent_same_content(db_session):
    """同 formula+params+direction 创建版本返回既有版本，不产生重复。"""
    factor = _make_draft(db_session, code="idem_factor")
    db_session.flush()

    v1 = _make_version(
        db_session, factor.id, formula="close", direction="higher_better"
    )
    db_session.flush()
    v2 = _make_version(
        db_session, factor.id, formula="close", direction="higher_better"
    )
    db_session.flush()

    assert v1.id == v2.id  # 同一版本对象
    versions = list_factor_versions(db_session, factor.id)
    assert len(versions) == 1


def test_create_factor_version_rejects_immutable_previous(db_session):
    """前一最新版本被 EvaluationRun 引用时，创建新版本应被拒绝。"""
    factor = _make_draft(db_session, code="imm_factor")
    db_session.flush()

    v1 = _make_version(db_session, factor.id, formula="close")
    db_session.flush()

    # 创建 EvaluationRun 引用 v1，使 v1 不可变
    db_session.add(EvaluationRun(id="eval-imm-1", factor_version_id=v1.id))
    db_session.flush()

    assert check_version_immutable(db_session, v1.id) is True

    with pytest.raises(ValueError, match="version_immutable"):
        _make_version(db_session, factor.id, formula="open")


# ── 4. execute_transition（生命周期）──────────────────────


def test_execute_transition_draft_to_candidate(db_session):
    """draft → candidate 迁移成功，审计记录存在。"""
    factor = _make_draft(db_session, code="t1")
    db_session.flush()

    result = execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="submit_candidate", actor="tester"),
    )

    assert result.success is True
    assert result.to_status == "candidate"
    assert result.from_status == "draft"

    db_session.refresh(factor)
    assert factor.lifecycle_status == "candidate"

    audit = db_session.execute(
        select(TransitionAudit).where(TransitionAudit.factor_id == factor.id)
    ).scalar_one()
    assert audit.to_status == "candidate"
    assert audit.from_status == "draft"


def test_execute_transition_candidate_to_testing(db_session):
    """draft → candidate → testing 链式迁移。"""
    factor = _make_draft(db_session, code="t2")
    db_session.flush()

    execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="submit_candidate"),
    )
    result = execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="start_testing"),
    )

    assert result.to_status == "testing"
    db_session.refresh(factor)
    assert factor.lifecycle_status == "testing"


def test_execute_transition_forbidden_returns_error(db_session):
    """draft → testing 直接迁移应抛出 ValueError。"""
    factor = _make_draft(db_session, code="t3")
    db_session.flush()

    with pytest.raises(ValueError, match="transition_forbidden"):
        execute_transition(
            db_session,
            factor_id=factor.id,
            request=FactorTransitionRequest(action="start_testing"),
        )


@pytest.mark.parametrize(
    "status",
    ["draft", "candidate", "testing", "active", "shadow", "quarantined"],
)
def test_execute_transition_deprecate_any_status(db_session, status):
    """从任意非 deprecated 状态执行 deprecate 应成功。

    注：rejected 状态虽然出现在 deprecate 的 prereqs 中，
    但 ALLOWED_TRANSITIONS['rejected'] = {'draft'} 不含 deprecated，
    因此 rejected → deprecated 会被拒绝。此处不测 rejected。
    """
    factor = _make_draft(db_session, code=f"dep_{status}")
    factor.lifecycle_status = status
    db_session.flush()

    result = execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="deprecate"),
    )

    assert result.to_status == "deprecated"
    db_session.refresh(factor)
    assert factor.lifecycle_status == "deprecated"
    assert factor.is_active == 0


def test_execute_transition_idempotent_request_id(db_session):
    """相同 request_id 的二次调用返回既有审计结果，不新增审计记录。

    幂等检查在 prereq/allowed 检查之后执行，因此需保证因子仍处于
    合法的前置状态。此处手动插入审计记录模拟首次调用已落库。
    """
    factor = _make_draft(db_session, code="t4")
    db_session.flush()

    # 手动插入审计记录，模拟首次调用已成功落库（因子仍处于 draft）
    audit = TransitionAudit(
        factor_id=factor.id,
        from_status="draft",
        to_status="candidate",
        actor="tester",
        reason="first",
        request_id="req-idem-1",
    )
    db_session.add(audit)
    db_session.flush()
    assert factor.lifecycle_status == "draft"  # 因子未被实际迁移

    # 再次调用相同 request_id
    result = execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(
            action="submit_candidate",
            request_id="req-idem-1",
            actor="tester",
        ),
    )

    assert result.success is True
    assert result.audit_id == audit.id
    # 仍然只有一条审计记录
    assert _count_audits(db_session, factor.id) == 1
    # 因子未被迁移（幂等返回，不执行更新）
    db_session.refresh(factor)
    assert factor.lifecycle_status == "draft"


def test_execute_transition_records_actor_and_reason(db_session):
    """迁移审计记录包含 actor 和 reason。"""
    factor = _make_draft(db_session, code="t5")
    db_session.flush()

    execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(
            action="submit_candidate", actor="test_user", reason="unit test"
        ),
    )

    audit = db_session.execute(
        select(TransitionAudit).where(TransitionAudit.factor_id == factor.id)
    ).scalar_one()
    assert audit.actor == "test_user"
    assert audit.reason == "unit test"


# ── 5. TransitionAudit 不可变性 ────────────────────────────


def test_transition_audit_is_append_only(db_session):
    """execute_transition 每次创建新审计记录（追加），不更新既有记录。"""
    factor = _make_draft(db_session, code="a1")
    db_session.flush()

    r1 = execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="submit_candidate"),
    )
    r2 = execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="start_testing"),
    )

    audits = list(
        db_session.execute(
            select(TransitionAudit)
            .where(TransitionAudit.factor_id == factor.id)
            .order_by(TransitionAudit.id)
        ).scalars().all()
    )

    assert len(audits) == 2
    assert audits[0].id == r1.audit_id
    assert audits[1].id == r2.audit_id
    assert audits[0].id != audits[1].id  # 不同 ID = 追加而非更新
    # 第一条记录内容未被修改
    assert audits[0].to_status == "candidate"
    assert audits[1].to_status == "testing"


def test_transition_history_is_ordered(db_session):
    """get_transition_history 按时间倒序返回（最新优先）。"""
    factor = _make_draft(db_session, code="a2")
    db_session.flush()

    execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="submit_candidate"),
    )
    execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="start_testing"),
    )
    execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="deprecate"),
    )

    history = get_transition_history(db_session, factor_id=factor.id)
    assert len(history) == 3
    # 按 created_at desc 排序：最新（deprecated）在前
    assert history[0].to_status == "deprecated"
    assert history[1].to_status == "testing"
    assert history[2].to_status == "candidate"


# ── 6. 旧字段向后兼容 ──────────────────────────────────────


def test_legacy_factor_without_lifecycle_status_treated_as_active(db_session):
    """origin='system' 且 lifecycle_status=None 的旧因子被视为 active。

    _infer_current_status: lifecycle_status is None + origin == 'system' → 'active'。
    从 active 可以 deprecate。
    """
    factor = Factor(
        code="legacy_sys",
        name="Legacy System Factor",
        category="fundamental",
        direction="higher_better",
        status="active",
        is_active=1,
        origin="system",
        lifecycle_status=None,  # 旧因子未迁移
    )
    db_session.add(factor)
    db_session.flush()

    result = execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="deprecate"),
    )

    assert result.to_status == "deprecated"
    assert result.from_status is None  # raw_status 为 None
    db_session.refresh(factor)
    assert factor.lifecycle_status == "deprecated"


def test_transition_syncs_legacy_status_field(db_session):
    """状态迁移同步更新旧字段。

    - candidate 目标不触发 legacy status 同步（仅 active/deprecated 同步）
    - deprecated 目标同步 is_active=0
    """
    factor = _make_draft(db_session, code="legacy_sync")
    db_session.flush()

    # draft → candidate: lifecycle_status 更新，status 不变
    execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="submit_candidate"),
    )
    db_session.refresh(factor)
    assert factor.lifecycle_status == "candidate"
    # candidate 不触发 legacy status 同步，status 保持 'draft'
    assert factor.status == "draft"

    # candidate → deprecated: is_active 同步为 0
    execute_transition(
        db_session,
        factor_id=factor.id,
        request=FactorTransitionRequest(action="deprecate"),
    )
    db_session.refresh(factor)
    assert factor.lifecycle_status == "deprecated"
    assert factor.is_active == 0


# ── 7. 版本不可变性 ────────────────────────────────────────


def test_version_immutable_when_referenced_by_evaluation(db_session):
    """版本被 EvaluationRun 引用时，check_version_immutable 返回 True。"""
    factor = _make_draft(db_session, code="v1")
    db_session.flush()

    v1 = _make_version(db_session, factor.id, formula="close")
    db_session.flush()

    db_session.add(EvaluationRun(id="eval-ref-1", factor_version_id=v1.id))
    db_session.flush()

    assert check_version_immutable(db_session, v1.id) is True


def test_version_not_immutable_when_unreferenced(db_session):
    """版本无任何引用时，check_version_immutable 返回 False。"""
    factor = _make_draft(db_session, code="v2")
    db_session.flush()

    v1 = _make_version(db_session, factor.id, formula="close")
    db_session.flush()

    assert check_version_immutable(db_session, v1.id) is False
