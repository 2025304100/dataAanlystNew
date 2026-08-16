from __future__ import annotations

import json
import math
import statistics
import time
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.factor_library import (
    FactorDraftCreate,
    FactorPreviewRequest,
    FactorTransitionRequest,
    FactorValidateRequest,
    FactorVersionCreate,
    FactorVersionRead,
)
from app.services.factors.health import get_factor_health
from app.services.factors.config import (
    get_factor_system_config,
    update_factor_system_config,
)
from app.services.factors.runtime import get_factor_runtime_snapshot
from app.services.factors.store import FactorWarehouse


router = APIRouter()


class FactorSystemConfigUpdate(BaseModel):
    feature_enabled: bool
    actor: str = Field(default='local_user', min_length=1, max_length=128)


def _factor_overview(db: Session) -> dict:
    config = get_factor_system_config(db)
    health = get_factor_health(
        FactorWarehouse(config.warehouse_path)
    ).to_dict()
    reasons = health.get('reasons') or []
    return {
        'runtime': get_factor_runtime_snapshot(db).to_dict(),
        'config': config.to_dict(),
        'feature_enabled': config.feature_enabled,
        'warehouse_error': reasons[0] if reasons else None,
        'health': health,
        'latest_trade_date': health.get('latest_bar_date'),
        'factor_coverage': health.get('factors', []),
    }


@router.get('/factors/overview')
def get_factors_overview(db: Session = Depends(get_db)):
    return _factor_overview(db)


@router.get('/factors/config')
def get_factor_config(db: Session = Depends(get_db)):
    return get_factor_system_config(db).to_dict()


@router.patch('/factors/config')
def patch_factor_config(
    payload: FactorSystemConfigUpdate,
    db: Session = Depends(get_db),
):
    try:
        snapshot = update_factor_system_config(
            db,
            feature_enabled=payload.feature_enabled,
            actor=payload.actor,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return snapshot.to_dict()


@router.post('/factors/warehouse/initialize')
def initialize_factor_warehouse(db: Session = Depends(get_db)):
    config = get_factor_system_config(db)
    if not config.feature_enabled:
        raise HTTPException(
            status_code=409,
            detail='Enable the factor feature before initializing the warehouse',
        )
    try:
        FactorWarehouse(config.warehouse_path).initialize()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _factor_overview(db)


@router.get('/factors')
def list_factors(
    lifecycle_status: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    factor_kind: str | None = Query(default=None),
    category: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """列出因子（支持筛选与分页，WP2-05）。

    向后兼容：不传任何参数时返回全部因子（仍用新分页结构）。
    """
    from app.schemas.factor_library import FactorRead
    from app.services.factors.factor_registry import list_factors as _list_factors

    no_params = (
        lifecycle_status is None
        and origin is None
        and factor_kind is None
        and category is None
        and search is None
        and page == 1
        and page_size == 20
    )
    if no_params:
        items, total = _list_factors(db, page=1, page_size=10**9)
        page_size = total
    else:
        items, total = _list_factors(
            db,
            lifecycle_status=lifecycle_status,
            origin=origin,
            factor_kind=factor_kind,
            category=category,
            search=search,
            page=page,
            page_size=page_size,
        )

    return {
        'items': [
            FactorRead.model_validate(row).model_dump(mode='json') for row in items
        ],
        'total': total,
        'page': page,
        'page_size': page_size,
    }


@router.get('/factors/{factor_code}/versions')
def list_factor_versions(
    factor_code: str,
    db: Session = Depends(get_db),
):
    factor = db.execute(
        select(Factor).where(Factor.code == factor_code)
    ).scalar_one_or_none()
    if factor is None:
        raise HTTPException(status_code=404, detail='Factor not found')
    versions = db.execute(
        select(FactorVersion)
        .where(FactorVersion.factor_id == factor.id)
        .order_by(desc(FactorVersion.version))
    ).scalars().all()
    return [
        {
            'factor_code': factor.code,
            'version': item.version,
            'formula_expr': item.formula_expr,
            'params': json.loads(item.params_json or '{}'),
            'direction': item.direction,
            'source_mapping': json.loads(item.source_mapping_json or '{}'),
            'effective_from': item.effective_from,
            'change_note': item.change_note,
            'is_latest': bool(item.is_latest),
            'created_at': item.created_at,
        }
        for item in versions
    ]


@router.get('/factors/symbols/{symbol_id}/explanation')
def get_symbol_factor_explanation(
    symbol_id: int,
    trade_date: date | None = Query(default=None),
    model_run_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail='Symbol not found')
    runtime = get_factor_runtime_snapshot(db)
    effective_model_id = model_run_id or runtime.active_model_run_id
    stmt = select(Score).where(Score.symbol_id == symbol_id)
    if trade_date is not None:
        stmt = stmt.where(Score.trade_date == trade_date)
    if effective_model_id:
        stmt = stmt.where(
            Score.factor_model_run_id == effective_model_id,
            Score.weight_mode.in_(('shadow', 'ridge')),
        )
    else:
        stmt = stmt.where(Score.weight_mode.in_(('shadow', 'ridge')))
    score = db.execute(
        stmt.order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()
    if score is None:
        raise HTTPException(
            status_code=404,
            detail='Dynamic factor explanation not found',
        )
    try:
        factor_scores = json.loads(score.factor_scores_json or '{}')
    except (TypeError, json.JSONDecodeError):
        factor_scores = {}
    explanation = factor_scores.get('_dynamic_model')
    if not isinstance(explanation, dict):
        raise HTTPException(
            status_code=404,
            detail='Dynamic factor explanation not found',
        )
    return {
        'symbol_id': symbol.id,
        'symbol': symbol.symbol,
        'name': symbol.name,
        'trade_date': score.trade_date,
        'weight_mode': score.weight_mode,
        'model_run_id': score.factor_model_run_id,
        'factor_data_cutoff_at': score.factor_data_cutoff_at,
        'factor_quality_score': score.factor_quality_score,
        'factor_timing_score': score.factor_timing_score,
        'model_alpha_score': score.model_alpha_score,
        'macro_regime': score.macro_regime,
        'macro_position_multiplier': score.macro_position_multiplier,
        'explanation': explanation,
    }


@router.get('/factors/readiness')
def get_factors_readiness(db: Session = Depends(get_db)):
    """返回 8 因子的 readiness 报告，含完整交易日证据和源表统计。"""
    from app.services.factors.readiness import get_factor_readiness_report
    try:
        report = get_factor_readiness_report(db)
        return report.to_dict()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f'readiness_unavailable: {exc}',
        ) from exc


@router.get('/factors/{factor_code}/readiness')
def get_factor_readiness_detail(
    factor_code: str,
    db: Session = Depends(get_db),
):
    """返回单个因子的 readiness 详情。"""
    from dataclasses import asdict

    from app.services.factors.readiness import get_factor_readiness_report
    report = get_factor_readiness_report(db)
    factor = report.get_factor(factor_code)
    if factor is None:
        raise HTTPException(
            status_code=404,
            detail=f'factor_not_found: {factor_code}',
        )
    return factor.to_dict() if hasattr(factor, 'to_dict') else asdict(factor)


# ---------------------------------------------------------------------------
# WPD-06: Batch audit endpoints
# ---------------------------------------------------------------------------


@router.get('/factors/batches')
def list_factor_batches(
    source_key: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """返回最近 N 条 ingestion_batches 记录。"""
    from app.services.factors.batch_audit import list_recent_batches

    config = get_factor_system_config(db)
    warehouse = FactorWarehouse(config.warehouse_path)
    health = warehouse.health()
    if not health.available:
        return {
            'warehouse_available': False,
            'warehouse_path': str(warehouse.path),
            'batches': [],
            'error': health.error or 'warehouse_unavailable',
        }
    try:
        records = list_recent_batches(
            warehouse, source_key=source_key, limit=limit
        )
        return {
            'warehouse_available': True,
            'warehouse_path': str(warehouse.path),
            'batches': [record.to_dict() for record in records],
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f'batch_list_unavailable: {exc}',
        ) from exc


@router.get('/factors/batches/audit')
def get_batch_audit_report(db: Session = Depends(get_db)):
    """返回完整批次审计报告，含原子提交证据和落后诊断。"""
    from app.services.factors.batch_audit import (
        get_batch_audit_report as _build_report,
    )

    config = get_factor_system_config(db)
    warehouse = FactorWarehouse(config.warehouse_path)
    try:
        report = _build_report(warehouse)
        return report.to_dict()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f'batch_audit_unavailable: {exc}',
        ) from exc


@router.get('/factors/batches/{batch_id}')
def get_batch_detail(
    batch_id: str,
    db: Session = Depends(get_db),
):
    """返回单个批次的审计详情。"""
    from app.services.factors.batch_audit import audit_batch_atomicity

    config = get_factor_system_config(db)
    warehouse = FactorWarehouse(config.warehouse_path)
    health = warehouse.health()
    if not health.available:
        raise HTTPException(
            status_code=503,
            detail=health.error or 'warehouse_unavailable',
        )
    try:
        entry = audit_batch_atomicity(warehouse, batch_id=batch_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f'batch_audit_unavailable: {exc}',
        ) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail='batch_not_found')
    return entry.to_dict()


# ---------------------------------------------------------------------------
# WPD-07: Data source roadmap endpoint
# ---------------------------------------------------------------------------

@router.get('/factors/data-source-roadmap')
def get_data_source_roadmap(db: Session = Depends(get_db)):
    """返回 7 类数据源的补齐路线报告，含策略、限流预算、可达覆盖说明。"""
    from app.services.factors.data_source_roadmap import get_data_source_roadmap as _build_roadmap
    try:
        report = _build_roadmap(db)
        return report.to_dict()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f'data_source_roadmap_unavailable: {exc}',
        ) from exc


# ---------------------------------------------------------------------------
# WP2-05: 公式校验与预览 API
# ---------------------------------------------------------------------------

@router.get('/factors/formula-catalog')
def get_factor_formula_catalog(db: Session = Depends(get_db)):
    """Return compiler capability plus live field-level data readiness."""
    from app.services.factors.formula_catalog import build_formula_catalog

    config = get_factor_system_config(db)
    return build_formula_catalog(FactorWarehouse(config.warehouse_path))


@router.post('/factors/validate')
def validate_factor_formula(
    payload: FactorValidateRequest,
):
    """校验因子公式，返回编译结果和错误码（WP2-05）。

    校验内容：
    - AST 节点白名单（禁止属性访问/导入/lambda/推导式等）
    - 深度/节点数/函数调用数限制
    - 函数白名单和参数范围
    - 字段目录校验（未知字段报错）
    - 回看窗口限制
    - 后处理配置校验

    不访问数据库，纯静态校验。
    """
    from app.schemas.factor_library import FactorValidateResponse
    from app.services.factors.factor_compiler import compile_formula

    result = compile_formula(
        formula=payload.formula_expr,
        params=payload.params,
        direction=payload.direction,
        postprocess=payload.postprocess,
        strict_fields=True,
    )

    return FactorValidateResponse(
        is_valid=result.is_valid,
        execution_plan=result.execution_plan.to_dict() if result.execution_plan else None,
        errors=[e.to_dict() for e in result.errors],
        data_dependencies=(
            result.execution_plan.data_dependencies if result.execution_plan else None
        ),
    ).model_dump()


@router.post('/factors/preview')
def preview_factor_formula(
    payload: FactorPreviewRequest,
    db: Session = Depends(get_db),
):
    """预览因子公式，返回执行计划、数据 readiness 和完整交易日证据（WP2-05）。

    预览默认选择最近完整交易日，不选择残缺横截面。
    返回数据来源、缺失原因和 data_cutoff_at。
    """
    started_at = time.perf_counter()
    from app.schemas.factor_library import (
        FactorPreviewResponse,
        FactorPreviewValueItem,
    )
    from app.services.factors.factor_compiler import compile_formula
    from app.services.factors.trade_calendar import latest_complete_trade_date
    from app.services.factors.readiness import _get_source_table_stats
    from app.services.factors.config import get_factor_system_config

    # 1. 编译公式
    result = compile_formula(
        formula=payload.formula_expr,
        params=payload.params,
        direction=payload.direction,
        postprocess=payload.postprocess,
        strict_fields=True,
    )

    if not result.is_valid:
        return FactorPreviewResponse(
            is_valid=False,
            errors=[e.to_dict() for e in result.errors],
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 3),
        ).model_dump()

    plan = result.execution_plan
    deps = plan.data_dependencies

    # 2. 获取完整交易日证据
    evidence = None
    selected_trade_date = None
    data_cutoff_at = None
    try:
        ctd_evidence = latest_complete_trade_date(db)
        selected_trade_date = payload.trade_date or str(ctd_evidence.selected_trade_date)
        data_cutoff_at = str(ctd_evidence.selected_trade_date)
        evidence = {
            'selected_trade_date': selected_trade_date,
            'latest_complete_trade_date': str(ctd_evidence.selected_trade_date),
            'observed_symbols': ctd_evidence.observed_symbols,
            'expected_symbols': ctd_evidence.expected_symbols,
            'completeness_ratio': ctd_evidence.completeness_ratio,
            'fallback_reason': ctd_evidence.fallback_reason,
        }
    except Exception:
        selected_trade_date = payload.trade_date

    # 3. 评估数据 readiness（基于依赖的源表）
    data_readiness = None
    missing_reasons: dict[str, str] = {}
    data_fix_links: list[dict[str, object]] = []
    blocking_fields: list[dict[str, object]] = []
    readiness_warnings: list[dict[str, object]] = []
    evaluation_supported = True
    evaluation_mode = "continuous"
    warehouse: FactorWarehouse | None = None
    try:
        config = get_factor_system_config(db)
        warehouse = FactorWarehouse(config.warehouse_path)
        source_tables = deps.get('source_tables', [])
        if source_tables:
            with warehouse.connection(read_only=True) as conn:
                table_stats = _get_source_table_stats(conn)
            readiness_fields = {}
            for table in source_tables:
                stats = table_stats.get(table, {})
                rows = int(stats.get('rows', 0))
                readiness_fields[table] = {
                    'rows': rows,
                    'latest_date': stats.get('latest_date'),
                    'has_data': rows > 0,
                }
                if rows == 0:
                    missing_reasons[table] = 'source_table_empty'
                    data_fix_links.append({
                        'section': 'universe' if table == 'raw_daily_bars' else 'external',
                        'source_table': table,
                        'label': '去补充基础行情数据' if table == 'raw_daily_bars' else '去同步外部数据',
                    })
            data_readiness = {
                'source_tables': readiness_fields,
                'point_in_time_fields': deps.get('point_in_time_fields', []),
                'max_lookback': deps.get('max_lookback', 1),
            }

            from app.services.factors.formula_catalog import build_formula_catalog

            live_catalog = build_formula_catalog(warehouse)
            capability_by_field = {
                str(item.get('key')): item
                for item in live_catalog.get('fields', [])
            }
            dependency_capabilities: dict[str, dict[str, object]] = {}
            dependency_names = [str(field) for field in deps.get('fields', [])]
            modes = {
                str(capability_by_field.get(field_name, {}).get(
                    'evaluation_mode',
                    capability_by_field.get(field_name, {}).get('data_mode', 'continuous'),
                ))
                for field_name in dependency_names
                if capability_by_field.get(field_name)
            }
            non_continuous_modes = {
                mode for mode in modes if mode in {'event', 'snapshot'}
            }
            # Event/snapshot formulas are evaluated at their latest actual
            # observation, not at the latest daily-bar date.  This avoids a
            # genuine event being presented as unavailable merely because it
            # did not occur on today's trading session.
            if payload.trade_date is None and len(non_continuous_modes) == 1:
                special_dates = [
                    str(capability_by_field[field_name]['latest_date'])
                    for field_name in dependency_names
                    if str(capability_by_field.get(field_name, {}).get(
                        'evaluation_mode', capability_by_field.get(field_name, {}).get('data_mode', ''),
                    )) in non_continuous_modes
                    and capability_by_field.get(field_name, {}).get('latest_date')
                ]
                if special_dates:
                    selected_trade_date = min(special_dates)
                    data_cutoff_at = selected_trade_date
                    if evidence is not None:
                        evidence['evaluation_source_date'] = selected_trade_date
                        evidence['evaluation_date_policy'] = 'latest_real_event_or_snapshot'
            requested_date = selected_trade_date
            for field_name in deps.get('fields', []):
                capability = capability_by_field.get(str(field_name), {})
                if not capability:
                    continue
                dependency_capabilities[str(field_name)] = capability
                availability = str(capability.get('availability', 'unknown'))
                field_mode = str(capability.get('data_mode', 'continuous'))
                modes.add(str(capability.get('evaluation_mode', field_mode)))
                field_issue = {
                    'field': str(field_name),
                    'availability': availability,
                    'data_mode': field_mode,
                    'reason': str(capability.get('status_reason', '')),
                    'first_date': capability.get('first_date'),
                    'latest_date': capability.get('latest_date'),
                    'nonnull_rows': int(capability.get('nonnull_rows', 0) or 0),
                    'distinct_symbols': int(capability.get('distinct_symbols', 0) or 0),
                    'distinct_dates': int(capability.get('distinct_dates', 0) or 0),
                }
                preview_enabled = bool(capability.get('preview_enabled', False))
                if not preview_enabled or availability in {'blocked', 'unknown'}:
                    blocking_fields.append(field_issue)
                    missing_reasons[f'field:{field_name}'] = str(
                        capability.get('status_reason', 'field_data_unavailable')
                    )
                    continue
                first_date = capability.get('first_date')
                if requested_date and first_date and str(requested_date) < str(first_date):
                    field_issue['reason'] = (
                        f"requested_date_before_coverage: {requested_date} < {first_date}"
                    )
                    blocking_fields.append(field_issue)
                    missing_reasons[f'field:{field_name}'] = str(field_issue['reason'])
                    continue
                if availability != 'available':
                    readiness_warnings.append(field_issue)
                if (
                    not bool(capability.get('evaluation_enabled', False))
                    and field_mode not in {'event', 'snapshot'}
                ):
                    evaluation_supported = False

            if blocking_fields:
                evaluation_supported = False
            if non_continuous_modes:
                evaluation_mode = (
                    next(iter(non_continuous_modes))
                    if len(non_continuous_modes) == 1
                    else 'mixed'
                )
                # A pure event/snapshot formula can be previewed and assessed
                # in its own mode. Mixed event+snapshot expressions remain
                # blocked until an explicit combined-evaluation contract exists.
                if evaluation_mode == 'mixed':
                    evaluation_supported = False
                    missing_reasons['evaluation_mode'] = 'mixed_event_snapshot_not_supported'
            data_readiness['fields'] = dependency_capabilities
            data_readiness['evaluation_supported'] = evaluation_supported
            data_readiness['evaluation_mode'] = evaluation_mode
            data_readiness['evaluation_scope'] = (
                'event_date_only' if evaluation_mode == 'event'
                else 'snapshot_time_only' if evaluation_mode == 'snapshot'
                else 'continuous_trade_dates'
            )
    except Exception as exc:
        missing_reasons['warehouse'] = f'readiness_error: {type(exc).__name__}'
        evaluation_supported = False

    # 4. 构建预览值（WP2-06：接入 DuckDB 兼容执行）
    values: list[FactorPreviewValueItem] = []
    try:
        from app.services.factors.factor_executor import FactorExecutor

        if warehouse is None:
            raise RuntimeError('factor_warehouse_unavailable')
        if blocking_fields:
            raise RuntimeError('formula_data_blocked')
        executor = FactorExecutor(warehouse)
        preview_td = (
            date.fromisoformat(selected_trade_date)
            if selected_trade_date
            else None
        )
        preview_outcome = executor.preview(
            plan,
            trade_date=preview_td,
            limit=payload.max_symbols,
            symbols=payload.symbols,
        )
        for v in preview_outcome.values:
            values.append(FactorPreviewValueItem(
                symbol=v.get("symbol"),
                trade_date=v.get("trade_date"),
                raw_value=v.get("raw_value"),
                winsorized_value=v.get("winsorized_value"),
                normalized_value=v.get("normalized_value"),
                eligible=v.get("eligible", False),
                missing_reason=None if v.get("eligible", False) else "formula_result_missing",
            ))
        # 补充执行错误到 missing_reasons
        for err in preview_outcome.errors:
            missing_reasons[f"preview_error_{len(missing_reasons)}"] = err
    except Exception as exc:
        missing_reasons['preview'] = f'preview_error: {type(exc).__name__}: {exc}'

    # Explicitly requested symbols remain visible even when no source row exists.
    if payload.symbols:
        requested = list(dict.fromkeys(
            str(symbol).strip() for symbol in payload.symbols if str(symbol).strip()
        ))[:payload.max_symbols]
        present = {item.symbol for item in values}
        for symbol in requested:
            if symbol in present:
                continue
            values.append(FactorPreviewValueItem(
                symbol=symbol,
                trade_date=selected_trade_date or "",
                eligible=False,
                missing_reason="symbol_data_missing",
            ))
            missing_reasons[f"symbol:{symbol}"] = "symbol_data_missing"

    attempted_count = len(values)
    valid_values = [
        float(item.raw_value)
        for item in values
        if item.eligible and item.raw_value is not None and math.isfinite(float(item.raw_value))
    ]
    valid_count = len(valid_values)
    missing_count = max(0, attempted_count - valid_count)
    coverage_rate = valid_count / attempted_count if attempted_count else 0.0
    missing_rate = missing_count / attempted_count if attempted_count else 0.0
    if valid_count == 0:
        evaluation_supported = False
        missing_reasons.setdefault('evaluation', 'no_valid_preview_values')
    elif evaluation_mode == 'continuous' and coverage_rate < 0.05:
        evaluation_supported = False
        missing_reasons.setdefault('evaluation', 'preview_coverage_below_5_percent')
    if data_readiness is not None:
        data_readiness['evaluation_supported'] = evaluation_supported

    def _quantile(numbers: list[float], ratio: float) -> float | None:
        if not numbers:
            return None
        ordered = sorted(numbers)
        position = (len(ordered) - 1) * ratio
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    q1 = _quantile(valid_values, 0.25)
    q3 = _quantile(valid_values, 0.75)
    distribution = {
        'min': min(valid_values) if valid_values else None,
        'p25': q1,
        'median': _quantile(valid_values, 0.5),
        'p75': q3,
        'max': max(valid_values) if valid_values else None,
        'mean': statistics.fmean(valid_values) if valid_values else None,
        'stddev': statistics.pstdev(valid_values) if len(valid_values) > 1 else (0.0 if valid_values else None),
    }
    outlier_count = 0
    if q1 is not None and q3 is not None:
        iqr = q3 - q1
        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr
        outlier_count = sum(value < lower_bound or value > upper_bound for value in valid_values)

    if missing_reasons and not data_fix_links:
        source_tables = deps.get('source_tables', [])
        for table in source_tables:
            data_fix_links.append({
                'section': 'universe' if table == 'raw_daily_bars' else 'external',
                'source_table': table,
                'label': '去补充基础行情数据' if table == 'raw_daily_bars' else '去同步外部数据',
            })

    deduped_fix_links = list({
        (str(link.get('section')), str(link.get('source_table'))): link
        for link in data_fix_links
    }.values())

    return FactorPreviewResponse(
        is_valid=True,
        execution_plan=plan.to_dict(),
        errors=[],
        data_cutoff_at=data_cutoff_at,
        selected_trade_date=selected_trade_date,
        complete_trade_day_evidence=evidence,
        data_readiness=data_readiness,
        data_dependencies=deps,
        values=values,
        missing_reasons=missing_reasons,
        attempted_count=attempted_count,
        valid_count=valid_count,
        missing_count=missing_count,
        coverage_rate=round(coverage_rate, 6),
        missing_rate=round(missing_rate, 6),
        distribution=distribution,
        outlier_count=outlier_count,
        elapsed_ms=round((time.perf_counter() - started_at) * 1000, 3),
        data_fix_links=deduped_fix_links,
        evaluation_supported=evaluation_supported,
        evaluation_mode=evaluation_mode,
        blocking_fields=blocking_fields,
        readiness_warnings=readiness_warnings,
    ).model_dump()


# ---------------------------------------------------------------------------
# WP2-05: 因子库 CRUD API（因子详情/草稿/版本/引用/状态迁移）
# ---------------------------------------------------------------------------


@router.get('/factors/{factor_code}')
def get_factor_detail(
    factor_code: str,
    db: Session = Depends(get_db),
):
    """获取单个因子详情。"""
    from app.schemas.factor_library import FactorRead
    from app.services.factors.factor_registry import get_factor_by_code

    factor = get_factor_by_code(db, factor_code)
    if factor is None:
        raise HTTPException(status_code=404, detail='factor_not_found')
    return FactorRead.model_validate(factor).model_dump(mode='json')


def _require_feature_enabled(db: Session):
    """R1-GATE 回滚门禁：新写入口端点必须检查 feature_enabled，确保可通过单一开关关闭。"""
    config = get_factor_system_config(db)
    if not config.feature_enabled:
        raise HTTPException(
            status_code=409,
            detail={
                'error_code': 'factor_feature_disabled',
                'user_message': '因子功能未启用，写入口已关闭',
                'retryable': False,
            },
        )


@router.post('/factors', status_code=201)
def create_factor(
    payload: 'FactorDraftCreate',
    db: Session = Depends(get_db),
):
    """创建因子草稿。"""
    _require_feature_enabled(db)
    from app.schemas.factor_library import FactorDraftCreate, FactorRead
    from app.services.factors.factor_registry import create_factor_draft

    try:
        factor = create_factor_draft(db, draft=payload)
        db.commit()
    except ValueError as exc:
        db.rollback()
        msg = str(exc)
        if msg.startswith('factor_code_conflict'):
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    db.refresh(factor)
    return FactorRead.model_validate(factor).model_dump(mode='json')


@router.post('/factors/{factor_code}/versions', status_code=201)
def create_factor_version_endpoint(
    factor_code: str,
    payload: FactorVersionCreate,
    db: Session = Depends(get_db),
):
    """创建因子版本。"""
    _require_feature_enabled(db)
    from app.services.factors.factor_registry import (
        create_factor_version,
        get_factor_by_code,
    )

    factor = get_factor_by_code(db, factor_code)
    if factor is None:
        raise HTTPException(status_code=404, detail='factor_not_found')
    try:
        version = create_factor_version(
            db, factor_id=factor.id, request=payload,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        msg = str(exc)
        if msg.startswith('version_immutable') or msg.startswith('factor_code_conflict'):
            raise HTTPException(status_code=409, detail=msg) from exc
        if msg.startswith('factor_not_found'):
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    db.refresh(version)
    return FactorVersionRead.model_validate(version).model_dump(mode='json')


@router.get('/factors/{factor_code}/references')
def get_factor_references_endpoint(
    factor_code: str,
    version_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """获取因子版本引用信息。"""
    from app.services.factors.factor_registry import (
        get_factor_by_code,
        get_factor_references,
    )

    factor = get_factor_by_code(db, factor_code)
    if factor is None:
        raise HTTPException(status_code=404, detail='factor_not_found')
    try:
        result = get_factor_references(db, factor.id, version_id)
    except ValueError as exc:
        db.rollback()
        msg = str(exc)
        if msg == 'version_not_found' or msg == 'factor_not_found':
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    return result.model_dump(mode='json')


@router.post('/factors/{factor_code}/transitions')
def execute_factor_transition(
    factor_code: str,
    payload: FactorTransitionRequest,
    db: Session = Depends(get_db),
):
    """执行因子状态迁移。"""
    _require_feature_enabled(db)
    from dataclasses import asdict

    from app.services.factors.factor_lifecycle import execute_transition
    from app.services.factors.factor_registry import get_factor_by_code

    factor = get_factor_by_code(db, factor_code)
    if factor is None:
        raise HTTPException(status_code=404, detail='factor_not_found')
    try:
        result = execute_transition(
            db, factor_id=factor.id, request=payload,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return asdict(result)


@router.get('/factors/{factor_code}/transitions')
def get_factor_transition_history(
    factor_code: str,
    db: Session = Depends(get_db),
):
    """获取因子状态迁移历史。"""
    from app.schemas.factor_library import TransitionAuditRead
    from app.services.factors.factor_lifecycle import get_transition_history
    from app.services.factors.factor_registry import get_factor_by_code

    factor = get_factor_by_code(db, factor_code)
    if factor is None:
        raise HTTPException(status_code=404, detail='factor_not_found')
    audits = get_transition_history(db, factor_id=factor.id)
    return [
        TransitionAuditRead.model_validate(a).model_dump(mode='json')
        for a in audits
    ]
