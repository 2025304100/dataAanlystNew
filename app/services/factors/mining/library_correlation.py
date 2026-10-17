"""Final-test novelty evidence against the active factor library, on the same pool/dates."""
from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.factor_correlation import compute_correlation_matrix
from app.services.factors.store import FactorWarehouse


def compare_active_library(db: Any, *, ctx: Any, candidate_panel: pd.DataFrame,
                           candidate_factor_version_id: int | None = None) -> dict[str, Any]:
    """Return a measured max |Spearman|; incomplete comparisons stay unknown, never zero."""
    references = []
    candidate_version = db.get(FactorVersion, candidate_factor_version_id) if candidate_factor_version_id else None
    candidate_factor_id = candidate_version.factor_id if candidate_version is not None else None
    excluded_self_code = None
    for factor in db.execute(select(Factor).where(Factor.is_active == 1)).scalars():
        state = str(factor.lifecycle_status or factor.status or '').lower()
        if state not in ('active', 'shadow') or factor.archived_at is not None:
            continue
        if candidate_factor_id is not None and factor.id == candidate_factor_id:
            excluded_self_code = factor.code
            continue
        version = db.get(FactorVersion, factor.active_version_id) if factor.active_version_id else None
        if version is None and not factor.active_version_id:
            version = db.execute(select(FactorVersion).where(
                FactorVersion.factor_id == factor.id, FactorVersion.is_latest == 1)
                .order_by(FactorVersion.version.desc())).scalars().first()
        invalid_pin = bool(factor.active_version_id) and (
            version is None or version.factor_id != factor.id)
        formula = None if invalid_pin else (
            version.formula_expr if version is not None else factor.formula_expr)
        params = {}
        if version is not None:
            try:
                params = json.loads(version.params_json or '{}')
            except (ValueError, TypeError):
                params = None
        references.append((factor.code, factor.active_version_id or (
            version.id if version is not None else None), formula, params))
    evidence: dict[str, Any] = {
        'status': 'empty_library' if not references else 'complete',
        'method': 'cross_section_rank_spearman', 'reference_codes': [r[0] for r in references],
        'reference_version_ids': [r[1] for r in references],
        'reference_count': len(references), 'compared_reference_count': 0,
        'excluded_self_code': excluded_self_code,
        'max_abs_correlation': 0.0 if not references else None, 'comparisons': [], 'errors': [],
    }
    if not references:
        return evidence
    panel = candidate_panel.copy()
    panel.index = pd.to_datetime(panel.index).date
    panel = panel.loc[(panel.index >= ctx.split.test_start) & (panel.index <= ctx.split.test_end)]
    if ctx.candidate_symbols is not None:
        panel = panel.reindex(columns=list(ctx.candidate_symbols))
    panel = panel.replace([np.inf, -np.inf], np.nan)
    dates = list(panel.index)
    if not dates:
        evidence.update(status='incomplete', errors=[{'reason': 'candidate_panel_empty'}])
        return evidence
    wh = FactorWarehouse(str(ctx.warehouse_path))
    measured = []
    for code, version_id, formula, params in references:
        try:
            if not formula or params is None:
                raise ValueError('active_reference_formula_unavailable')
            result = compile_formula(formula=str(formula), params=params)
            if not result.success or result.execution_plan is None:
                raise ValueError('active_reference_formula_invalid')
            output = FactorExecutor(wh).execute_panel(result.execution_plan,
                start_date=min(dates), end_date=max(dates), symbols=ctx.candidate_symbols)
            if output.read_errors or output.factors_long is None or output.factors_long.empty:
                raise ValueError('active_reference_panel_unavailable')
            reference = output.factors_long.pivot(index='trade_date', columns='symbol', values='raw_value')
            reference.index = pd.to_datetime(reference.index).date
            reference = reference.reindex(index=panel.index, columns=panel.columns)
            pair = pd.concat([panel.rank(axis=1, pct=True).stack().rename('candidate'),
                reference.rank(axis=1, pct=True).stack().rename('reference')], axis=1).dropna()
            if len(pair) < 30 or pair.index.get_level_values(0).nunique() < 3:
                raise ValueError('active_reference_overlap_insufficient')
            rho = compute_correlation_matrix(pair, method='spearman').get('candidate', 'reference')
            if rho is None or not math.isfinite(rho):
                raise ValueError('active_reference_correlation_undefined')
            measured.append(abs(rho))
            evidence['comparisons'].append({'code': code, 'version_id': version_id,
                'correlation': rho, 'paired_samples': len(pair),
                'dates': int(pair.index.get_level_values(0).nunique())})
        except Exception as exc:
            evidence['errors'].append({'code': code, 'version_id': version_id,
                'reason': f'{type(exc).__name__}: {exc}'[:240]})
    evidence['compared_reference_count'] = len(measured)
    if len(measured) == len(references):
        evidence['max_abs_correlation'] = max(measured)
    else:
        evidence['status'] = 'incomplete'
    return evidence
