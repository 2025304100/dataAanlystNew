from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.signal_rule import SignalRule
from app.schemas.signal_rule import SignalRulePreset, SignalRuleRead, SignalRuleUpsert


PRESETS: dict[str, SignalRulePreset] = {
    "conservative": SignalRulePreset(
        mode="conservative",
        rule_name="保守模式",
        description="匹配更严格，适合只相信高相似样本。",
        quality_tolerance=8,
        timing_tolerance=8,
        min_sample_count=10,
        max_samples=60,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=True,
    ),
    "balanced": SignalRulePreset(
        mode="balanced",
        rule_name="均衡模式",
        description="默认方案，兼顾样本数量和相似度。",
        quality_tolerance=12,
        timing_tolerance=12,
        min_sample_count=3,
        max_samples=60,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=True,
    ),
    "aggressive": SignalRulePreset(
        mode="aggressive",
        rule_name="进取模式",
        description="放宽匹配条件，更快获得样本，但可信度要打折。",
        quality_tolerance=18,
        timing_tolerance=18,
        min_sample_count=3,
        max_samples=120,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=False,
    ),
    "expert": SignalRulePreset(
        mode="expert",
        rule_name="专家模式",
        description="自定义匹配参数，适合你明确知道自己要放宽或收紧什么。",
        quality_tolerance=12,
        timing_tolerance=12,
        min_sample_count=5,
        max_samples=120,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=True,
    ),
}


def preset_list() -> list[SignalRulePreset]:
    return list(PRESETS.values())


def default_signal_rule(portfolio_id: int) -> SignalRuleRead:
    preset = PRESETS["balanced"]
    return SignalRuleRead(portfolio_id=portfolio_id, **preset.model_dump(exclude={"description"}))


def serialize_signal_rule(rule: SignalRule) -> SignalRuleRead:
    return SignalRuleRead(
        id=rule.id,
        portfolio_id=rule.portfolio_id,
        rule_name=rule.rule_name,
        mode=rule.mode,
        quality_tolerance=rule.quality_tolerance,
        timing_tolerance=rule.timing_tolerance,
        min_sample_count=rule.min_sample_count,
        max_samples=rule.max_samples,
        same_region=bool(rule.same_region),
        same_asset_type=bool(rule.same_asset_type),
        same_stage=bool(rule.same_stage),
        same_action=bool(rule.same_action),
        is_active=bool(rule.is_active),
    )


def get_active_signal_rule(db: Session, portfolio_id: int) -> SignalRuleRead:
    rule = (
        db.execute(
            select(SignalRule)
            .where(SignalRule.portfolio_id == portfolio_id, SignalRule.is_active == 1)
            .order_by(SignalRule.id.desc())
        )
        .scalars()
        .first()
    )
    return serialize_signal_rule(rule) if rule is not None else default_signal_rule(portfolio_id)


def upsert_active_signal_rule(db: Session, portfolio_id: int, payload: SignalRuleUpsert) -> SignalRuleRead:
    existing = (
        db.execute(
            select(SignalRule)
            .where(SignalRule.portfolio_id == portfolio_id, SignalRule.is_active == 1)
            .order_by(SignalRule.id.desc())
        )
        .scalars()
        .first()
    )
    if existing is None:
        existing = SignalRule(portfolio_id=portfolio_id)
        db.add(existing)

    existing.rule_name = payload.rule_name
    existing.mode = payload.mode
    existing.quality_tolerance = payload.quality_tolerance
    existing.timing_tolerance = payload.timing_tolerance
    existing.min_sample_count = payload.min_sample_count
    existing.max_samples = payload.max_samples
    existing.same_region = 1 if payload.same_region else 0
    existing.same_asset_type = 1 if payload.same_asset_type else 0
    existing.same_stage = 1 if payload.same_stage else 0
    existing.same_action = 1 if payload.same_action else 0
    existing.is_active = 1
    db.commit()
    db.refresh(existing)
    return serialize_signal_rule(existing)
