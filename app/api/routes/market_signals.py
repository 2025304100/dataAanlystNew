"""Public API for subscribed, verifiable market-event alerts."""
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.market_signal import SignalRuleConfig, SignalRulePatch, SignalEvidenceCreate, SignalSettingsPatch, SignalAnalyzeRequest, SignalRunRequest, SignalJobRequest
from app.services import market_signals as service
from app.services import market_signal_sources as sources
from app.services import market_signal_analysis as analysis
from app.services import market_signal_engine as engine
from app.core.async_utils import run_sync

class SignalRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            from app.services.market_event_collection_lock import MarketEventCollectionBusy
            try:
                return await original(request)
            except MarketEventCollectionBusy as exc:
                raise HTTPException(409, "异动任务正在执行，请稍后重试") from exc
        return handle


router = APIRouter(prefix="/market-signals", route_class=SignalRoute)


@router.get("/rules")
def list_rules(db: Session = Depends(get_db)):
    return service.list_rules(db)


@router.post("/rules", status_code=201)
def create_rule(payload: SignalRuleConfig, db: Session = Depends(get_db)):
    return service.create_rule(db, payload)


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, payload: SignalRulePatch, db: Session = Depends(get_db)):
    return service.update_rule(db, rule_id, payload)


@router.get("/rules/{rule_id}/versions")
def rule_versions(rule_id: int, db: Session = Depends(get_db)):
    return service.rule_versions(db, rule_id)


@router.get("/evidence")
def list_evidence(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return sources.list_evidence(db, offset=offset, limit=limit)


@router.post("/evidence", status_code=201)
def add_evidence(payload: SignalEvidenceCreate, db: Session = Depends(get_db)):
    return sources.add_evidence(db, payload)


@router.post("/evidence/{evidence_id}/enrich")
async def enrich_evidence(evidence_id: int, db: Session = Depends(get_db)):
    from app.services.market_signal_operations import enrich_evidence
    return await run_sync(enrich_evidence, db, evidence_id)


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return service.get_settings(db)


@router.put("/settings")
def update_settings(payload: SignalSettingsPatch, db: Session = Depends(get_db)):
    return service.update_settings(db, payload)


@router.post("/analyze")
async def analyze(payload: SignalAnalyzeRequest, db: Session = Depends(get_db)):
    return await run_sync(analysis.analyze, db, payload)


@router.get("/assessments")
def assessments(rule_id: int | None = None, offset: int = Query(0, ge=0),
                limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return analysis.list_assessments(db, rule_id=rule_id, offset=offset, limit=limit)


@router.post("/run")
async def run(payload: SignalRunRequest, db: Session = Depends(get_db)):
    return await run_sync(engine.run, db, payload)


@router.get("/predictions")
def predictions(rule_id: int | None = None, offset: int = Query(0, ge=0),
                limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return engine.list_predictions(db, rule_id=rule_id, offset=offset, limit=limit)


@router.get("/dashboard")
def dashboard(rule_id: int | None = None, db: Session = Depends(get_db)):
    return engine.dashboard(db, rule_id=rule_id)


@router.post("/predictions/{prediction_id}/recheck")
def recheck(prediction_id: int, db: Session = Depends(get_db)):
    return engine.recheck(db, prediction_id)


@router.get("/predictions/{prediction_id}/outcomes")
def outcome_history(prediction_id: int, db: Session = Depends(get_db)):
    return engine.outcome_history(db, prediction_id)


@router.get("/metadata")
def metadata(keyword: str = Query("", max_length=60), db: Session = Depends(get_db)):
    return service.metadata(db, keyword)


@router.post("/jobs", status_code=202)
def create_job(payload: SignalJobRequest):
    from app.services.market_signal_jobs import create_job
    return create_job(payload)


@router.get("/jobs/{task_id}")
def get_job(task_id: str):
    from app.services.market_signal_jobs import get_job
    return get_job(task_id)
