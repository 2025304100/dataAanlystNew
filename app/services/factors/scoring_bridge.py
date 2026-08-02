'''Materialize validated factor-model output into immutable Score batches.'''
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor_model import FactorModelRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.definitions import (
    FACTOR_BY_CODE,
    FACTOR_DEFINITIONS,
)
from app.services.factors.macro import MacroRegime, calculate_macro_regime
from app.services.factors.runtime import get_factor_runtime_snapshot
from app.services.factors.store import FactorWarehouse


_VALID_MODES = {'manual', 'shadow', 'ridge'}
_QUALITY_CATEGORIES = {'fundamental'}
_TIMING_CATEGORIES = {'capital_flow', 'sentiment'}
_DYNAMIC_DETAIL_KEY = '_dynamic_model'
# 所有系统定义因子代码（用于解释层数据加载，包含非特征因子如事件因子）
_EXPLANATION_FACTOR_CODES = tuple(
    item.code for item in FACTOR_DEFINITIONS
)
# 自定义因子的默认分类（不匹配 quality/timing，只贡献到 alpha 总分）
_DEFAULT_FACTOR_CATEGORY = 'custom'
_COPY_EXCLUDED_COLUMNS = {
    'id',
    'calc_batch_id',
    'created_at',
    'weight_mode',
    'factor_model_run_id',
    'factor_data_cutoff_at',
    'factor_quality_score',
    'factor_timing_score',
    'model_alpha_score',
    'macro_regime',
    'macro_position_multiplier',
}


def _get_factor_category(code: str) -> str:
    """获取因子分类（WP7-03: 支持自定义因子，不在 FACTOR_BY_CODE 中时返回默认分类）。"""
    definition = FACTOR_BY_CODE.get(code)
    if definition is not None:
        return definition.category
    return _DEFAULT_FACTOR_CATEGORY


@dataclass(frozen=True)
class ScoringBridgeResult:
    mode: str
    trade_date: date
    model_run_id: str | None
    calc_batch_id: str | None
    manual_score_count: int
    materialized_count: int
    skipped_symbol_count: int


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value))
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _grade(score: float) -> str:
    if score >= 80:
        return 'A'
    if score >= 65:
        return 'B'
    if score >= 50:
        return 'C'
    return 'D'


def _clamp_score(value: float) -> float:
    return round(max(0.0, min(100.0, float(value))), 2)


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _percentile_scores(values: dict[str, float]) -> dict[str, float]:
    '''Return tie-aware 0..100 cross-sectional percent ranks.

    Mapping average ranks to both endpoints keeps a constant or one-symbol
    cross-section neutral at 50 instead of treating it as a top signal.
    '''
    if not values:
        return {}
    if len(values) == 1:
        return {next(iter(values)): 50.0}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    ranks: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        percentile = (average_rank - 1.0) / (len(ordered) - 1.0) * 100.0
        for position in range(index, end):
            ranks[ordered[position][0]] = round(percentile, 6)
        index = end
    return ranks


def _score_batch_id(
    *,
    mode: str,
    trade_date: date,
    model_run_id: str,
    factor_calc_batch_id: str,
    pipeline_run_id: str | None,
) -> str:
    identity = '|'.join(
        (
            mode,
            trade_date.isoformat(),
            model_run_id,
            factor_calc_batch_id,
            pipeline_run_id or '',
        )
    )
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]
    return f'factor-{mode}-{trade_date:%Y%m%d}-{digest}'


def _load_manual_scores(
    db: Session,
    *,
    trade_date: date,
    asset_type: str,
    manual_calc_batch_id: str | None,
) -> tuple[list[Score], dict[str, Symbol]]:
    stmt = (
        select(Score)
        .join(Symbol, Symbol.id == Score.symbol_id)
        .where(
            Score.trade_date == trade_date,
            Score.weight_mode == 'manual',
            Symbol.asset_type == asset_type,
        )
    )
    if manual_calc_batch_id is not None:
        stmt = stmt.where(Score.calc_batch_id == manual_calc_batch_id)
    rows = db.execute(
        stmt.order_by(Score.symbol_id.asc(), Score.id.desc())
    ).scalars().all()
    latest: dict[int, Score] = {}
    for row in rows:
        latest.setdefault(row.symbol_id, row)
    scores = list(latest.values())
    if not scores:
        return [], {}
    symbols = db.execute(
        select(Symbol).where(Symbol.id.in_([row.symbol_id for row in scores]))
    ).scalars().all()
    return scores, {symbol.symbol: symbol for symbol in symbols}


def _load_factor_rows(
    warehouse: FactorWarehouse,
    *,
    trade_date: date,
    factor_calc_batch_id: str,
    extra_factor_codes: tuple[str, ...] | None = None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], datetime | None]:
    """加载因子值行（WP7-03: 支持动态特征代码）。

    extra_factor_codes: 模型使用的特征代码（可能包含自定义因子），
    与 _EXPLANATION_FACTOR_CODES 合并后查询。
    """
    # 合并系统定义因子和模型特征代码（去重）
    all_codes_set = set(_EXPLANATION_FACTOR_CODES)
    if extra_factor_codes:
        all_codes_set.update(extra_factor_codes)
    all_codes = tuple(sorted(all_codes_set))

    placeholders = ', '.join('?' for _ in all_codes)
    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute(
            f'''
            SELECT symbol, factor_code, factor_version, raw_value,
                   winsorized_value, normalized_value, is_imputed,
                   imputation_method, data_cutoff_at
            FROM factor_values
            WHERE trade_date = ?
              AND calc_batch_id = ?
              AND eligible
              AND normalized_value IS NOT NULL
              AND factor_code IN ({placeholders})
            ORDER BY symbol, factor_code
            ''',
            [
                trade_date,
                factor_calc_batch_id,
                *all_codes,
            ],
        ).fetchall()
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    cutoffs: list[datetime] = []
    for (
        symbol,
        factor_code,
        factor_version,
        raw_value,
        winsorized_value,
        normalized_value,
        is_imputed,
        imputation_method,
        cutoff,
    ) in rows:
        normalized = float(normalized_value)
        if not math.isfinite(normalized):
            continue
        grouped.setdefault(str(symbol), {})[str(factor_code)] = {
            'factor_version': int(factor_version),
            'raw_value': float(raw_value) if raw_value is not None else None,
            'winsorized_value': (
                float(winsorized_value)
                if winsorized_value is not None
                else None
            ),
            'normalized_value': normalized,
            'is_imputed': bool(is_imputed),
            'imputation_method': imputation_method,
        }
        parsed_cutoff = _as_naive_datetime(cutoff)
        if parsed_cutoff is not None:
            cutoffs.append(parsed_cutoff)
    return grouped, max(cutoffs) if cutoffs else None


def _priority_with_dynamic_scores(
    manual: Score,
    *,
    quality_score: float,
    timing_score: float,
) -> float:
    config = _json_object(manual.scoring_config_snapshot_json)
    weights = config.get('final_weights') or {}
    quality_weight = float(weights.get('quality', 0.4))
    timing_weight = float(weights.get('timing', 0.5))
    news_weight = float(weights.get('news', 0.1))
    total_weight = quality_weight + timing_weight + news_weight
    if total_weight <= 0:
        return _clamp_score(manual.priority_score)
    # Preserve every non-quality/timing contribution already present in the
    # manual score, including any future scoring buckets.
    delta = (
        quality_weight * (quality_score - manual.quality_score)
        + timing_weight * (timing_score - manual.timing_score)
    ) / total_weight
    return _clamp_score(manual.priority_score + delta)


def _macro_detail(state: MacroRegime) -> dict[str, Any]:
    return {
        'as_of': state.as_of.isoformat() if state.as_of else None,
        'regime': state.regime,
        'position_multiplier': state.position_multiplier,
        'available': state.available,
        'cn_10y_change': state.cn_10y_change,
        'us_10y_change': state.us_10y_change,
        'margin_change_ratio': state.margin_change_ratio,
        'market_amount_change_ratio': state.market_amount_change_ratio,
        'market_amount_z20': state.market_amount_z20,
        'advancing_ratio': state.advancing_ratio,
        'margin_amount_divergence': state.margin_amount_divergence,
        'liquidity_score': state.liquidity_score,
        'liquidity_available': state.liquidity_available,
        'missing_indicators': list(state.missing_indicators),
        'missing_liquidity_indicators': list(
            state.missing_liquidity_indicators
        ),
    }


def _clone_manual_score(
    manual: Score,
    *,
    calc_batch_id: str,
    mode: str,
    model_run: FactorModelRun,
    factor_data_cutoff_at: datetime | None,
    macro_state: MacroRegime,
    quality_score: float,
    timing_score: float,
    alpha_score: float,
    factor_detail: dict[str, Any],
    quality_mix: float,
    timing_mix: float,
    factor_set_id: str | None = None,
    factor_member_versions_json: str | None = None,
) -> Score:
    payload = {
        column.name: getattr(manual, column.name)
        for column in Score.__table__.columns
        if column.name not in _COPY_EXCLUDED_COLUMNS
    }
    payload.update(
        {
            'calc_batch_id': calc_batch_id,
            'weight_mode': mode,
            'factor_model_run_id': model_run.id,
            'factor_data_cutoff_at': factor_data_cutoff_at,
            'factor_quality_score': quality_score,
            'factor_timing_score': timing_score,
            'model_alpha_score': alpha_score,
            'macro_regime': macro_state.regime,
            'macro_position_multiplier': macro_state.position_multiplier,
            # WP7-05: Score 解释追溯字段
            'factor_set_id': factor_set_id,
            'factor_member_versions_json': factor_member_versions_json,
            'created_at': _utcnow_naive(),
        }
    )
    config_snapshot = _json_object(manual.scoring_config_snapshot_json)
    config_snapshot['dynamic_weighting'] = {
        'mode': mode,
        'model_run_id': model_run.id,
        'quality_factor_weight': quality_mix,
        'timing_factor_weight': timing_mix,
    }
    payload['scoring_config_snapshot_json'] = json.dumps(
        config_snapshot, ensure_ascii=False, sort_keys=True
    )
    dimensions = _json_object(manual.dimension_scores_json)
    dimensions.update(
        {
            'factor_quality': quality_score,
            'factor_timing': timing_score,
            'model_alpha': alpha_score,
        }
    )
    payload['dimension_scores_json'] = json.dumps(
        dimensions, ensure_ascii=False, sort_keys=True
    )
    details = _json_object(manual.factor_scores_json)
    details[_DYNAMIC_DETAIL_KEY] = factor_detail
    payload['factor_scores_json'] = json.dumps(
        details, ensure_ascii=False, sort_keys=True
    )
    return Score(**payload)


def materialize_factor_scores(
    db: Session,
    warehouse: FactorWarehouse | None,
    *,
    trade_date: date,
    factor_calc_batch_id: str | None = None,
    model_run_id: str | None = None,
    mode: str | None = None,
    manual_calc_batch_id: str | None = None,
    pipeline_run_id: str | None = None,
    quality_factor_weight: float = 0.40,
    timing_factor_weight: float = 0.50,
) -> ScoringBridgeResult:
    '''Create shadow or Ridge Score rows from one validated model snapshot.

    Manual mode deliberately performs no warehouse reads and no Score writes.
    A shadow batch carries the full explanation but preserves every manual
    ranking field. Ridge mode blends the dynamic dimensions into the existing
    final score while retaining the manual scoring configuration and stage.
    '''
    runtime = get_factor_runtime_snapshot(db)
    active_mode = mode or runtime.weight_mode
    model_run_id = model_run_id or runtime.active_model_run_id
    if active_mode not in _VALID_MODES:
        raise ValueError(f'unsupported factor weight mode: {active_mode}')
    if not 0.0 <= quality_factor_weight <= 1.0:
        raise ValueError('quality_factor_weight must be within 0..1')
    if not 0.0 <= timing_factor_weight <= 1.0:
        raise ValueError('timing_factor_weight must be within 0..1')
    if active_mode == 'manual':
        return ScoringBridgeResult(
            mode='manual',
            trade_date=trade_date,
            model_run_id=None,
            calc_batch_id=None,
            manual_score_count=0,
            materialized_count=0,
            skipped_symbol_count=0,
        )
    if warehouse is None or not factor_calc_batch_id or not model_run_id:
        raise ValueError(
            'warehouse, factor_calc_batch_id and model_run_id are required '
            'for shadow/ridge modes'
        )

    model = db.get(FactorModelRun, model_run_id)
    if model is None:
        raise ValueError(f'factor model does not exist: {model_run_id}')
    if model.status != 'validated':
        raise ValueError(
            f'factor model must be validated, got status={model.status}'
        )
    coefficients = {
        weight.factor_code: float(weight.coefficient)
        for weight in model.weights
    }
    normalized_weights = {
        weight.factor_code: float(weight.normalized_weight)
        for weight in model.weights
    }
    # WP7-03: 从 model.weights 动态派生特征列表（替代静态 FEATURE_CODES）
    feature_codes = tuple(
        weight.factor_code for weight in
        sorted(model.weights, key=lambda w: w.factor_code)
    )
    if not feature_codes or set(coefficients) != set(feature_codes) or not all(
        math.isfinite(value) for value in coefficients.values()
    ):
        raise ValueError('validated model has incomplete or invalid coefficients')

    # WP7-05: 从模型记录解析 factor_set_id 和成员版本快照，用于 Score 解释追溯
    hyperparams = _json_object(model.hyperparameters_json)
    factor_set_id = hyperparams.get('factor_set_id')
    # 构造成员版本快照：{factor_code: {"version": int, "coefficient": float, "normalized_weight": float}}
    member_versions_snapshot = {
        weight.factor_code: {
            'version': weight.factor_version,
            'coefficient': float(weight.coefficient),
            'normalized_weight': float(weight.normalized_weight),
        }
        for weight in model.weights
    }
    factor_member_versions_json = (
        json.dumps(member_versions_snapshot, sort_keys=True)
        if member_versions_snapshot
        else None
    )

    manual_scores, symbols_by_code = _load_manual_scores(
        db,
        trade_date=trade_date,
        asset_type=model.asset_type,
        manual_calc_batch_id=manual_calc_batch_id,
    )
    factor_rows, factor_data_cutoff_at = _load_factor_rows(
        warehouse,
        trade_date=trade_date,
        factor_calc_batch_id=factor_calc_batch_id,
        extra_factor_codes=feature_codes,
    )
    manual_by_symbol_id = {score.symbol_id: score for score in manual_scores}
    complete: dict[str, dict[str, dict[str, Any]]] = {
        symbol: values
        for symbol, values in factor_rows.items()
        if set(feature_codes).issubset(values)
        and symbol in symbols_by_code
        and symbols_by_code[symbol].id in manual_by_symbol_id
    }

    quality_raw: dict[str, float] = {}
    timing_raw: dict[str, float] = {}
    alpha_raw: dict[str, float] = {}
    contributions: dict[str, dict[str, float]] = {}
    for symbol, values in complete.items():
        per_factor = {
            code: coefficients[code] * values[code]['normalized_value']
            for code in feature_codes
        }
        contributions[symbol] = per_factor
        quality_raw[symbol] = sum(
            value
            for code, value in per_factor.items()
            if _get_factor_category(code) in _QUALITY_CATEGORIES
        )
        timing_raw[symbol] = sum(
            value
            for code, value in per_factor.items()
            if _get_factor_category(code) in _TIMING_CATEGORIES
        )
        alpha_raw[symbol] = sum(per_factor.values())

    quality_scores = _percentile_scores(quality_raw)
    timing_scores = _percentile_scores(timing_raw)
    alpha_scores = _percentile_scores(alpha_raw)
    macro_state = calculate_macro_regime(warehouse, as_of=trade_date)
    calc_batch_id = _score_batch_id(
        mode=active_mode,
        trade_date=trade_date,
        model_run_id=model.id,
        factor_calc_batch_id=factor_calc_batch_id,
        pipeline_run_id=pipeline_run_id,
    )

    existing_rows = db.execute(
        select(Score).where(
            Score.trade_date == trade_date,
            Score.calc_batch_id == calc_batch_id,
        )
    ).scalars().all()
    existing_by_symbol_id = {row.symbol_id: row for row in existing_rows}
    materialized = 0
    for symbol_code in sorted(complete):
        symbol = symbols_by_code[symbol_code]
        manual = manual_by_symbol_id[symbol.id]
        factor_explanation = {
            'mode': active_mode,
            'model_run_id': model.id,
            'factor_calc_batch_id': factor_calc_batch_id,
            'factor_data_cutoff_at': (
                factor_data_cutoff_at.isoformat()
                if factor_data_cutoff_at
                else None
            ),
            # WP7-05: 追溯字段写入解释 JSON
            'factor_set_id': factor_set_id,
            'factors': {
                code: {
                    **complete[symbol_code][code],
                    'category': _get_factor_category(code),
                    'coefficient': coefficients[code],
                    'normalized_weight': normalized_weights.get(code),
                    'contribution': contributions[symbol_code][code],
                }
                for code in feature_codes
            },
            'event_factors': {
                code: {
                    **factor_rows[symbol_code][code],
                    'category': _get_factor_category(code),
                    'coefficient': None,
                    'normalized_weight': None,
                    'contribution': None,
                }
                for code in sorted(
                    set(factor_rows[symbol_code]) - set(feature_codes)
                )
            },
            'factor_quality_raw': quality_raw[symbol_code],
            'factor_timing_raw': timing_raw[symbol_code],
            'model_alpha_raw': alpha_raw[symbol_code],
            'factor_quality_score': quality_scores[symbol_code],
            'factor_timing_score': timing_scores[symbol_code],
            'model_alpha_score': alpha_scores[symbol_code],
            'blend': {
                'quality_factor_weight': quality_factor_weight,
                'timing_factor_weight': timing_factor_weight,
            },
            'macro': _macro_detail(macro_state),
        }
        score = _clone_manual_score(
            manual,
            calc_batch_id=calc_batch_id,
            mode=active_mode,
            model_run=model,
            factor_data_cutoff_at=factor_data_cutoff_at,
            macro_state=macro_state,
            quality_score=quality_scores[symbol_code],
            timing_score=timing_scores[symbol_code],
            alpha_score=alpha_scores[symbol_code],
            factor_detail=factor_explanation,
            quality_mix=quality_factor_weight,
            timing_mix=timing_factor_weight,
            # WP7-05: Score 解释追溯字段
            factor_set_id=factor_set_id,
            factor_member_versions_json=factor_member_versions_json,
        )
        if active_mode == 'ridge':
            score.quality_score = _clamp_score(
                manual.quality_score * (1.0 - quality_factor_weight)
                + quality_scores[symbol_code] * quality_factor_weight
            )
            score.timing_score = _clamp_score(
                manual.timing_score * (1.0 - timing_factor_weight)
                + timing_scores[symbol_code] * timing_factor_weight
            )
            score.quality_grade = _grade(score.quality_score)
            score.priority_score = _priority_with_dynamic_scores(
                manual,
                quality_score=score.quality_score,
                timing_score=score.timing_score,
            )
        existing = existing_by_symbol_id.get(symbol.id)
        if existing is None:
            db.add(score)
        else:
            for column in Score.__table__.columns:
                if column.name != 'id':
                    setattr(existing, column.name, getattr(score, column.name))
        materialized += 1

    db.flush()
    return ScoringBridgeResult(
        mode=active_mode,
        trade_date=trade_date,
        model_run_id=model.id,
        calc_batch_id=calc_batch_id,
        manual_score_count=len(manual_scores),
        materialized_count=materialized,
        skipped_symbol_count=max(0, len(manual_scores) - materialized),
    )


__all__ = ['ScoringBridgeResult', 'materialize_factor_scores']
