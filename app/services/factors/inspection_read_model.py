"""Factor-domain read adapter for a portfolio's exact frozen dependencies.

Never resolve the global active model as a substitute for the portfolio binding.
The supplied session is used, so inspection can suppress autoflush throughout.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor import Factor, FactorValue
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorVersion


def read_inspection_factor_dependencies(db: Session, factor_set_id: str, *, current_read: bool = False) -> dict:
    factor_set = db.get(FactorSet, factor_set_id, with_for_update=current_read, populate_existing=current_read)
    if factor_set is None:
        return {"status": "MISSING", "reason": "绑定的冻结因子集不存在", "members": []}
    statement = select(FactorSetMember, Factor, FactorVersion).outerjoin(
        Factor, Factor.id == FactorSetMember.factor_id,
    ).outerjoin(FactorVersion, FactorVersion.id == FactorSetMember.factor_version_id).where(
        FactorSetMember.factor_set_id == factor_set_id,
        FactorSetMember.role == "feature",
        FactorSetMember.excluded_reason.is_(None),
    ).order_by(FactorSetMember.factor_id, FactorSetMember.id)
    if current_read:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    rows = db.execute(statement).all()
    members = [{
        "factor_id": member.factor_id, "code": member.factor_code,
        "name": factor.name if factor else member.factor_code,
        "version": member.factor_version, "version_id": member.factor_version_id,
        "frequency": factor.frequency if factor else None,
        "missing_policy": member.missing_policy,
        "version_valid": bool(factor and version and version.factor_id == member.factor_id
            and version.version == member.factor_version and version.validation_status == "valid"),
        "source_dependencies": version.data_dependencies_json if version else None,
    } for member, factor, version in rows]
    valid = factor_set.status == "frozen" and bool(members)
    return {"status": "PASSED" if valid else "UNKNOWN",
            "reason": "已读取绑定因子集的冻结成员" if valid else "因子集未冻结或没有有效特征成员",
            "factor_set_id": factor_set_id, "content_hash": factor_set.content_hash,
            "members": members}


def read_inspection_factor_value(db: Session, symbol_id: int, factor_id: int, *, current_read: bool = False):
    """Return scalar evidence instead of exposing factor ORM outside the domain."""
    statement = select(FactorValue).where(FactorValue.symbol_id == symbol_id,
        FactorValue.factor_id == factor_id).order_by(FactorValue.trade_date.desc(),
            FactorValue.id.desc()).limit(1)
    if current_read:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = db.execute(statement).scalars().first()
    if row is None:
        return None
    return {key: getattr(row, key) for key in ("id", "trade_date", "raw_value", "normalized_value", "created_at")}
