"""F 簇技术债收口守护：`FactorWeightSnapshot` 全库只指 per-model 聚合快照类。

背景（体检报告 §五 F 簇 / §六.4）：rev 049 把 `factor_weight_snapshots` 表从
per-factor rows 重建为 per-model aggregate（PK=model_id，新 ORM 类定义在
`app/models/factor_weight_snapshot.py`），但 `app/models/factor_model.py` 长期保留
`FactorWeightSnapshot = FactorModelMember` 这个过渡别名（FactorModelMember 才是
per-factor 物化表 `factor_model_members`）。两个名字同存已实际造成错 import：
按 `factor_model.FactorWeightSnapshot` 取到的其实是另一张表的类，用例构造
aggregate 行时字段全部对不上。

别名已于 2026-09-27 删除，本用例锁死它不再回潮。
"""
from __future__ import annotations

import pytest

import app.models.factor_model as factor_model_module
from app.models.factor_governance import FactorModelMember
from app.models.factor_weight_snapshot import FactorWeightSnapshot

pytestmark = pytest.mark.whitebox


def test_legacy_alias_removed_from_factor_model():
    """`factor_model.FactorWeightSnapshot` 别名不得复活。"""
    assert not hasattr(factor_model_module, "FactorWeightSnapshot"), (
        "app.models.factor_model 又出现了 FactorWeightSnapshot 别名：它与 "
        "app.models.factor_weight_snapshot 的同名 aggregate 类指向两张不同的表，"
        "调用方按名字取表会拿到另一张表（F 簇根因）。per-factor 权重行请直接用 "
        "FactorModelMember。"
    )


def test_two_names_map_to_two_different_tables():
    """同名不同表的两类必须可区分：聚合快照 vs per-factor 物化成员。"""
    assert FactorWeightSnapshot is not FactorModelMember
    assert FactorWeightSnapshot.__tablename__ == "factor_weight_snapshots"
    assert FactorModelMember.__tablename__ == "factor_model_members"


def test_factor_model_still_registers_member_class_for_relationship():
    """FactorModelRun.weights relationship 依赖 FactorModelMember 已被 import 注册。"""
    assert factor_model_module.FactorModelMember is FactorModelMember
    assert "weights" in factor_model_module.FactorModelRun.__mapper__.relationships
