"""Read-only evidence of registered inputs in business SQL and default DuckDB."""
from datetime import date
from sqlalchemy import select,func
from app.models.daily_bar import DailyBar
from app.models.financial_report import StockFinancialReport
from app.models.capital_flow import CapitalFlow
from app.models.symbol import Symbol
from app.services.factors.input_replay import _bundle_dir,_json,_sha,ID,PROJECT
from app.services.factors.verified_input_import import SOURCE,BAR_SOURCE
from app.services.factors.config import get_factor_system_config
from app.services.factors.store import FactorWarehouse
from app.services.factors.health import get_factor_health

def read_verified_input_business_status(db,bundle_id):
    root=_bundle_dir(bundle_id);bundle=_json(root/'manifest.json')
    dataset_id=bundle['dataset_id']
    if not ID.fullmatch(dataset_id):raise ValueError('verified_import_invalid_dataset_id')
    from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT
    directory=TRUSTED_HISTORY_DATASET_ROOT/dataset_id
    if not directory.resolve().is_relative_to(TRUSTED_HISTORY_DATASET_ROOT.resolve()):raise ValueError('verified_import_dataset_path_escape')
    if _sha(directory/'manifest.json')!=bundle['dataset_manifest_sha256']:raise ValueError('verified_import_dataset_changed')
    manifest=_json(directory/'manifest.json');codes=manifest['cohort_symbols']
    if not codes or len(codes)>1000 or len(codes)!=len(set(codes)):raise ValueError('verified_import_invalid_cohort')
    first,last=map(date.fromisoformat,(manifest['calendar'][0],manifest['calendar'][-1]))
    with db.no_autoflush:
        ids=list(db.scalars(select(Symbol.id).where(Symbol.symbol.in_(codes))))
        business={
            'bars':db.scalar(select(func.count(DailyBar.id)).where(DailyBar.symbol_id.in_(ids),DailyBar.trade_date>=first,DailyBar.trade_date<=last,DailyBar.source==BAR_SOURCE)),
            'financial':db.scalar(select(func.count(StockFinancialReport.id)).where(StockFinancialReport.symbol_id.in_(ids),StockFinancialReport.source==SOURCE)),
            'flow':db.scalar(select(func.count(CapitalFlow.id)).where(CapitalFlow.symbol_id.in_(ids),CapitalFlow.trade_date>=first,CapitalFlow.trade_date<=last,CapitalFlow.source==SOURCE)),
        }
        cfg=get_factor_system_config(db)
    warehouse=FactorWarehouse(cfg.warehouse_path);health=get_factor_health(warehouse)
    marks=','.join('?'for _ in codes)
    with warehouse.connection(read_only=True)as conn:
        counts={table:conn.execute(f'SELECT COUNT(*) FROM {table} WHERE symbol IN ({marks}) AND '+where,[*codes,*params]).fetchone()[0]
            for table,where,params in (
                ('raw_daily_bars','trade_date BETWEEN ? AND ? AND source=?',[first,last,BAR_SOURCE]),
                ('raw_financial_reports','source=?',[SOURCE]),
                ('raw_fund_flows','trade_date BETWEEN ? AND ? AND source=?',[first,last,SOURCE]))}
        values=[dict(symbol=s,code=c,version=v,raw_value=raw,normalized_value=norm,eligible=bool(ok))for s,c,v,raw,norm,ok in conn.execute(
            f'''SELECT symbol,factor_code,factor_version,raw_value,normalized_value,eligible FROM factor_values
            WHERE calc_batch_id=? AND trade_date=? AND symbol IN ({marks}) ORDER BY symbol,factor_code,factor_version''',
            [health.calc_batch_id,last,*codes]).fetchall()]
    coverage=[dict(code=f.factor_code,eligible=len({r['symbol']for r in values if r['code']==f.factor_code and r['eligible']}),
        denominator=len(codes))for f in health.factors]
    return dict(bundle_id=bundle_id,bundle_manifest_sha256=_sha(root/'manifest.json'),dataset_manifest_sha256=_sha(directory/'manifest.json'),
        first_date=str(first),last_date=str(last),fixed_symbols=len(codes),business_counts=business,warehouse_counts=counts,
        calc_batch_id=health.calc_batch_id,calculation_date=max((f.latest_trade_date for f in health.factors if f.latest_trade_date),default=None),
        market_coverage=[f.__dict__ for f in health.factors],fixed_pool_coverage=coverage,factor_observations=values,
        read_only=True,historical_asof_certified=False,model_activation_performed=False,production_score_publication_performed=False)
