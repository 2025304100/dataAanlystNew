"""G1-WP0-7: FactorSet 服务与 readiness 测试 (Q24 / WP0-7a WP0-7b)。

场景：
  T_FS_01：创建 draft + add/remove 成员 → 正常
  T_FS_02：冻结后 add/remove 抛错 → 必须 copy-on-write (Q24.3)
  T_FS_03：copy_factor_set_to_new_id 生成新 ID & 保留成员 & status=draft → 可再编辑
  T_FS_04：content_hash 对相同成员集稳定（相同成员→相同哈希）；不同成员→不同哈希
  T_FS_05：freeze 空集合被拒绝（空 FactorSet 不允许冻结）
  T_FS_06：factor_set_readiness 返回 NOT_FROZEN / EMPTY_SET / READY (WP0-7b)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from app.services.factor_set_service import (
    add_member,
    copy_factor_set_to_new_id,
    create_factor_set,
    deprecate_factor_set,
    factor_set_readiness,
    freeze_factor_set,
    get_factor_set,
    list_members,
    remove_member,
)


def _make_factor(db: "Session", code: str, v: int = 1) -> tuple[Factor, FactorVersion]:
    f = Factor(code=code, name=f"因子{code}", category="value",
               direction="higher_better", status="active",
               source_type="manual", is_active=1, description="")
    db.add(f); db.flush()
    fv = FactorVersion(factor_id=f.id, version=v, formula_expr="", params_json="{}",
                       created_by="qa")
    db.add(fv); db.flush()
    db.refresh(f); db.refresh(fv)
    return f, fv


class TestFactorSetLifecycle:
    # ──────────────────────────────────────────────────────────────────
    def test_t_fs_01_draft_add_remove_works(self, db_session):
        f1, fv1 = _make_factor(db_session, "FS01_VAL", 1)
        f2, fv2 = _make_factor(db_session, "FS01_QUAL", 2)

        fs = create_factor_set(db_session, factor_set_id="fs_01", name="FS01", created_by="qa")
        assert fs.status == "draft" and fs.frozen_at is None and fs.content_hash is None

        m1 = add_member(db_session, "fs_01", factor_id=f1.id, factor_version_id=fv1.id,
                        factor_code="FS01_VAL", factor_version=1)
        m2 = add_member(db_session, "fs_01", factor_id=f2.id, factor_version_id=fv2.id,
                        factor_code="FS01_QUAL", factor_version=2)
        assert len(list_members(db_session, "fs_01")) == 2

        remove_member(db_session, "fs_01", m1.id)
        remaining = list_members(db_session, "fs_01")
        assert [m.id for m in remaining] == [m2.id]

    # ──────────────────────────────────────────────────────────────────
    def test_t_fs_02_frozen_blocks_mutation_requires_copy(self, db_session):
        f1, fv1 = _make_factor(db_session, "FS02_A", 1)
        fs = create_factor_set(db_session, factor_set_id="fs_02", name="FS02")
        add_member(db_session, "fs_02", factor_id=f1.id, factor_version_id=fv1.id,
                   factor_code="FS02_A", factor_version=1)
        frozen = freeze_factor_set(db_session, "fs_02")
        assert frozen.status == "frozen"
        assert frozen.content_hash is not None and len(frozen.content_hash) > 10
        assert frozen.frozen_at is not None

        f2, fv2 = _make_factor(db_session, "FS02_B", 1)
        with pytest.raises(RuntimeError, match="Q24.3"):
            add_member(db_session, "fs_02", factor_id=f2.id, factor_version_id=fv2.id,
                       factor_code="FS02_B", factor_version=1)

        mem = list_members(db_session, "fs_02")[0]
        with pytest.raises(RuntimeError, match="Q24.3"):
            remove_member(db_session, "fs_02", mem.id)

    # ──────────────────────────────────────────────────────────────────
    def test_t_fs_03_copy_frozen_to_new_id_and_can_edit(self, db_session):
        f1, fv1 = _make_factor(db_session, "FS03_A", 1)
        f2, fv2 = _make_factor(db_session, "FS03_B", 1)
        src = create_factor_set(db_session, factor_set_id="fs_03_src", name="Source")
        add_member(db_session, "fs_03_src", factor_id=f1.id, factor_version_id=fv1.id,
                   factor_code="FS03_A", factor_version=1)
        freeze_factor_set(db_session, "fs_03_src")

        # 复制
        dup = copy_factor_set_to_new_id(db_session, "fs_03_src", new_factor_set_id="fs_03_dup",
                                        created_by="qa")
        assert dup.id == "fs_03_dup" and dup.status == "draft" and dup.content_hash is None
        assert len(list_members(db_session, "fs_03_dup")) == 1

        # 现在 dup 是 draft，可以再加成员 (Q24.3)
        add_member(db_session, "fs_03_dup", factor_id=f2.id, factor_version_id=fv2.id,
                   factor_code="FS03_B", factor_version=1)
        assert len(list_members(db_session, "fs_03_dup")) == 2

    # ──────────────────────────────────────────────────────────────────
    def test_t_fs_04_content_hash_stable_and_discriminative(self, db_session):
        f1, fv1 = _make_factor(db_session, "FS04_A", 1)
        f2, fv2 = _make_factor(db_session, "FS04_B", 1)

        # 创建两个同内容集合
        def _mk(id_: str, order: str):
            fs = create_factor_set(db_session, factor_set_id=id_, name=id_)
            factors = [("FS04_A", f1, fv1), ("FS04_B", f2, fv2)]
            if order == "ba":
                factors = list(reversed(factors))
            for code, f, fv in factors:
                add_member(db_session, id_, factor_id=f.id, factor_version_id=fv.id,
                           factor_code=code, factor_version=1)
            return freeze_factor_set(db_session, id_)
        a = _mk("fs_04_same_a", "ab")
        b = _mk("fs_04_same_b", "ba")
        assert a.content_hash == b.content_hash, "相同成员+版本，顺序不同→应相同哈希"

        c = create_factor_set(db_session, factor_set_id="fs_04_diff", name="diff")
        add_member(db_session, "fs_04_diff", factor_id=f1.id, factor_version_id=fv1.id,
                   factor_code="FS04_A", factor_version=1)
        # 只有一个成员 → 哈希应不同
        freeze_factor_set(db_session, "fs_04_diff")
        c_hash = get_factor_set(db_session, "fs_04_diff").content_hash
        assert c_hash != a.content_hash, "不同成员集合必须不同哈希"

    # ──────────────────────────────────────────────────────────────────
    def test_t_fs_05_freeze_empty_rejected(self, db_session):
        create_factor_set(db_session, factor_set_id="fs_05_empty", name="Empty")
        with pytest.raises(RuntimeError, match="空成员"):
            freeze_factor_set(db_session, "fs_05_empty")

    # ──────────────────────────────────────────────────────────────────
    def test_t_fs_06_readiness_checks(self, db_session):
        f1, fv1 = _make_factor(db_session, "FS06_A", 1)

        # (a) 不存在
        r = factor_set_readiness(db_session, "fs_NX")
        assert r["ready"] is False and r["code"] == "NOT_FOUND"

        # (b) draft
        fs = create_factor_set(db_session, factor_set_id="fs_06_draft", name="Draft FS")
        add_member(db_session, "fs_06_draft", factor_id=f1.id, factor_version_id=fv1.id,
                   factor_code="FS06_A", factor_version=1)
        r = factor_set_readiness(db_session, "fs_06_draft", require_frozen=True)
        assert r["ready"] is False and r["code"] == "NOT_FROZEN"

        # (c) frozen → READY
        freeze_factor_set(db_session, "fs_06_draft")
        r = factor_set_readiness(db_session, "fs_06_draft")
        assert r["ready"] and r["code"] == "READY"
        assert r["details"]["member_count"] == 1
        assert "content_hash" in r["details"]

        # (d) empty frozen → 不可能；验证 require_frozen=False 时 draft+empty 被 EMPTY_SET 挡住
        create_factor_set(db_session, factor_set_id="fs_06_e", name="E")
        r = factor_set_readiness(db_session, "fs_06_e", require_frozen=False)
        assert r["ready"] is False and r["code"] == "EMPTY_SET"

    # ──────────────────────────────────────────────────────────────────
    def test_t_fs_07_deprecate_is_idempotent_and_blocks_freeze(self, db_session):
        f1, fv1 = _make_factor(db_session, "FS07_A", 1)
        fs = create_factor_set(db_session, factor_set_id="fs_07", name="FS07")
        add_member(db_session, "fs_07", factor_id=f1.id, factor_version_id=fv1.id,
                   factor_code="FS07_A", factor_version=1)
        freeze_factor_set(db_session, "fs_07")
        d = deprecate_factor_set(db_session, "fs_07")
        assert d.status == "deprecated"
        # 幂等
        d2 = deprecate_factor_set(db_session, "fs_07")
        assert d2.status == "deprecated" and d2.id == d.id
        # deprecated 无法冻结
        with pytest.raises(RuntimeError, match="deprecated"):
            freeze_factor_set(db_session, "fs_07")
