"""Prepare the fixed ETF price/volume features in the existing local library."""
from __future__ import annotations

import json
import threading
from sqlalchemy import select

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet
from app.models.factor_model import FactorVersion
from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate, FactorSetCreate, FactorSetMemberCreate
from app.services.factors.factor_registry import create_factor_draft, create_factor_version
from app.services.factors.factor_set_service import create_factor_set, add_member, freeze_factor_set
from app.services.factor_set_service import factor_set_readiness
from app.services.factors.config import get_factor_system_config

SET_ID = 'etf-price-volume-v1'
OWNER = 'etf_training_defaults:v1'
PREPARE_LOCK = threading.Lock()
FEATURES = (
    ('etf_return_5d', 'ETF5日涨幅', 'momentum', 'close / ref(close, 5) - 1', 'close'),
    ('etf_mean_close_20d', 'ETF20日均价', 'momentum', 'iif(count(close - close, 20) == 20, mean(close, 20), None)', 'close'),
    ('etf_mean_volume_20d', 'ETF20日成交量均值', 'liquidity', 'iif(count(volume - volume, 20) == 20, mean(volume, 20), None)', 'volume'),
    ('etf_return_volatility_20d', 'ETF20日收益波动', 'volatility', 'iif(count(close / prev_close - 1, 20) == 20, stddev(close / prev_close - 1, 20), None)', 'close'),
)
POSTPROCESS = {'winsorize': {'method': 'mad', 'mad_multiplier': 3.},
               'zscore': {'ddof': 0}, 'missing_policy': 'exclude'}
CONFLICT = 'ETF价量特征集合与固定版本不一致，请先核对原有记录，本次没有覆盖或创建其他集合。'


def prepare_default_set(db):
    """Create once, or verify the original immutable versions; never fit a model."""
    if not get_factor_system_config(db).feature_enabled:
        raise ValueError('因子功能尚未启用，请先启用后再准备ETF训练集合。')
    existing = db.get(FactorSet, SET_ID)
    if existing is not None:
        verify_default_set(db, existing)
        return existing
    result = create_factor_set(db, payload=FactorSetCreate(id=SET_ID, name='ETF价量特征',
        description='5日涨幅、20日均价、20日成交量均值和20日收益波动。只使用历史价格和成交量，缺失值保留，不启用模型。',
        created_by=OWNER))
    for index, (code, name, category, formula, field) in enumerate(FEATURES):
        if db.scalar(select(Factor).where(Factor.code == code)) is not None:
            raise ValueError(CONFLICT)
        factor = create_factor_draft(db, draft=FactorDraftCreate(code=code, name=name, category=category,
            asset_scope=['cn-etf'], default_missing_policy='exclude', frequency='daily', owner=OWNER,
            description='ETF训练使用的固定价量公式，缺失日期不补零。'))
        version = create_factor_version(db, factor_id=factor.id, request=FactorVersionCreate(
            formula_expr=formula, source_mapping={'table': 'raw_daily_bars', 'field': field},
            postprocess=POSTPROCESS, created_by=OWNER, created_via='template', change_note='接入已核对的ETF历史价量公式。'))
        if version.validation_status != 'valid':
            raise ValueError('ETF价量公式检查未通过，本次没有保存训练集合。')
        add_member(db, factor_set_id=SET_ID, payload=FactorSetMemberCreate(factor_id=factor.id,
            factor_version_id=version.id, role='feature', missing_policy='exclude', display_order=index))
    return freeze_factor_set(db, factor_set_id=SET_ID)


def verify_default_set(db, factor_set):
    if factor_set.created_by != OWNER or not factor_set_readiness(db, SET_ID)['ready']:
        raise ValueError(CONFLICT)
    members = sorted(factor_set.members, key=lambda item: item.display_order)
    if len(members) != len(FEATURES):
        raise ValueError(CONFLICT)
    for index, (member, (code, _, _, formula, field)) in enumerate(zip(members, FEATURES)):
        factor = db.get(Factor, member.factor_id)
        version = db.get(FactorVersion, member.factor_version_id)
        if (factor is None or version is None or factor.code != code or factor.owner != OWNER
                or json.loads(factor.asset_scope_json or '[]') != ['cn-etf']
                or factor.default_missing_policy != 'exclude' or member.factor_code != code
                or member.factor_version != 1 or member.display_order != index
                or member.role != 'feature' or member.missing_policy != 'exclude' or member.weight_constraint != 'free'
                or version.version != 1 or version.formula_expr != formula or version.direction != 'higher_better'
                or json.loads(version.params_json or '{}') != {}
                or json.loads(version.source_mapping_json or '{}') != {'table': 'raw_daily_bars', 'field': field}
                or json.loads(version.postprocess_json or '{}') != POSTPROCESS):
            raise ValueError(CONFLICT)
