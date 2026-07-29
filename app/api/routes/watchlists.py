from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.watchlist import (
    ObservationBatchImport,
    ObservationBatchImportResult,
    ObservationCandidateCreate,
    ObservationCreate,
    ObservationRead,
    ObservationUpdate,
    WatchlistCreate,
    WatchlistItemCreate,
    WatchlistItemRead,
    WatchlistRead,
)
from app.services.observations import (
    add_candidate_to_observation,
    archive_observation,
    get_observation_rich,
    idempotent_add_observation,
    list_observations_rich,
    restore_observation,
    update_observation,
)
from app.services.opportunity_transitions import (
    exclude_observation as _audit_exclude_observation,
    restore_observation as _audit_restore_observation,
    transition_candidate_to_observation,
)


router = APIRouter()


@router.get("/watchlists", response_model=list[WatchlistRead])
def list_watchlists(db: Session = Depends(get_db)):
    return db.execute(select(Watchlist).order_by(Watchlist.id.desc())).scalars().all()


@router.post("/watchlists", response_model=WatchlistRead)
def create_watchlist(payload: WatchlistCreate, db: Session = Depends(get_db)):
    existing = db.execute(select(Watchlist).where(Watchlist.name == payload.name)).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Watchlist already exists")
    watchlist = Watchlist(**payload.model_dump())
    db.add(watchlist)
    db.commit()
    db.refresh(watchlist)
    return watchlist


@router.get("/watchlists/{watchlist_id}/items", response_model=list[WatchlistItemRead])
def list_watchlist_items(watchlist_id: int, db: Session = Depends(get_db)):
    watchlist = db.get(Watchlist, watchlist_id)
    if watchlist is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return db.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == watchlist_id).order_by(WatchlistItem.id.desc())
    ).scalars().all()


@router.post("/watchlists/{watchlist_id}/items", response_model=WatchlistItemRead)
def add_watchlist_item(watchlist_id: int, payload: WatchlistItemCreate, db: Session = Depends(get_db)):
    watchlist = db.get(Watchlist, watchlist_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if watchlist is None or symbol is None:
        raise HTTPException(status_code=404, detail="Watchlist or symbol not found")

    existing = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.symbol_id == payload.symbol_id,
        )
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Symbol already in watchlist")

    item = WatchlistItem(watchlist_id=watchlist_id, symbol_id=payload.symbol_id, note=payload.note)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/watchlists/{watchlist_id}/items/{symbol_id}")
def delete_watchlist_item(watchlist_id: int, symbol_id: int, db: Session = Depends(get_db)):
    item = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.symbol_id == symbol_id,
        )
    ).scalars().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    db.delete(item)
    db.commit()
    return {"deleted": True}


# ============================================================================
# WP2.3 观察池接口（仅追加，不修改上方已稳定端点）
# ============================================================================


@router.get(
    "/watchlists/{watchlist_id}/observations",
    response_model=list[ObservationRead],
    tags=["observations"],
)
def list_observations(
    watchlist_id: int,
    status: str | None = None,
    origin_type: str | None = None,
    tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[ObservationRead]:
    """列出观察项富读（WP2.3）。

    支持 status / origin_type / tag 筛选。
    默认不返回 archived。
    """
    richs = list_observations_rich(
        db,
        watchlist_id=watchlist_id,
        status=status,
        origin_type=origin_type,
        tag=tag,
        limit=limit,
        offset=offset,
    )
    return [ObservationRead(**r.to_dict()) for r in richs]


@router.post(
    "/watchlists/{watchlist_id}/observations",
    response_model=ObservationRead,
    tags=["observations"],
)
def add_observation(
    watchlist_id: int,
    payload: ObservationCreate,
    db: Session = Depends(get_db),
) -> ObservationRead:
    """幂等加入观察项（WP2.3）。

    同 watchlist_id + symbol_id 重复请求返回已有记录（非 409）。
    路径参数 watchlist_id 优先于 payload 中的同名属性。
    """
    item = idempotent_add_observation(
        db,
        watchlist_id=watchlist_id,
        symbol_id=payload.symbol_id,
        origin_type=payload.origin_type,
        origin_id=payload.origin_id,
        reason=payload.reason,
        score_snapshot=payload.score_snapshot,
        priority=payload.priority,
        tags=payload.tags,
        target_portfolio_id=payload.target_portfolio_id,
        note=payload.note,
    )
    rich = get_observation_rich(db, watchlist_item_id=item.id)
    return ObservationRead(**rich.to_dict())


@router.post(
    "/watchlists/{watchlist_id}/observations/from-candidate",
    response_model=ObservationRead,
    tags=["observations"],
)
def add_observation_from_candidate(
    watchlist_id: int,
    payload: ObservationCandidateCreate,
    db: Session = Depends(get_db),
) -> ObservationRead:
    """候选加入观察池（WP2.3 + WP3.2，单事务写来源、评分快照与审计事件）。

    路径参数 watchlist_id 优先于 payload 中的同名属性。
    幂等：同一候选重复请求返回已有观察项，不重复创建审计事件。
    归档/恢复：归档后再次候选加入会自动恢复为 watching 并补写审计事件。

    UAT-PAGES.1 P1-02：服务层对"候选不存在/标的不存在"抛 ValueError，
    路由层捕获并转为 HTTPException(404)，全局异常处理器包装为
    WP-S.6 统一错误协议（error_code=NOT_FOUND），不再返回 500 UNKNOWN_ERROR。
    """
    try:
        item, _event = transition_candidate_to_observation(
            db,
            candidate_id=payload.candidate_id,
            watchlist_id=watchlist_id,
            note=payload.note,
            priority=payload.priority,
            tags=payload.tags,
            target_portfolio_id=payload.target_portfolio_id,
        )
    except ValueError as exc:
        # 候选不存在 / 标的无法解析 → 404 NOT_FOUND（统一错误协议）
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rich = get_observation_rich(db, watchlist_item_id=item.id)
    return ObservationRead(**rich.to_dict())


@router.patch(
    "/watchlists/{watchlist_id}/observations/{observation_id}",
    response_model=ObservationRead,
    tags=["observations"],
)
def update_observation_endpoint(
    watchlist_id: int,
    observation_id: int,
    payload: ObservationUpdate,
    db: Session = Depends(get_db),
) -> ObservationRead:
    """更新观察项字段（WP2.3）。"""
    item = update_observation(
        db,
        watchlist_item_id=observation_id,
        priority=payload.priority,
        tags=payload.tags,
        reason=payload.reason,
        target_portfolio_id=payload.target_portfolio_id,
        note=payload.note,
        status=payload.status,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Observation not found")
    rich = get_observation_rich(db, watchlist_item_id=item.id)
    return ObservationRead(**rich.to_dict())


@router.post(
    "/watchlists/{watchlist_id}/observations/{observation_id}/archive",
    response_model=ObservationRead,
    tags=["observations"],
)
def archive_observation_endpoint(
    watchlist_id: int,
    observation_id: int,
    db: Session = Depends(get_db),
) -> ObservationRead:
    """归档观察项（WP2.3 + WP3.2，不物理删除，单事务写审计事件）。

    审计：写入 opportunity_transition_events（event_type='exclude'），幂等。
    """
    item = archive_observation(db, watchlist_item_id=observation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Observation not found")
    # 写审计事件（幂等，重复归档不重复写事件）
    try:
        _audit_exclude_observation(
            db,
            watchlist_item_id=observation_id,
            reason="manual archive",
        )
    except ValueError:
        # 审计服务判定对象不存在时不影响已完成的归档状态
        pass
    rich = get_observation_rich(db, watchlist_item_id=item.id)
    return ObservationRead(**rich.to_dict())


@router.post(
    "/watchlists/{watchlist_id}/observations/{observation_id}/restore",
    response_model=ObservationRead,
    tags=["observations"],
)
def restore_observation_endpoint(
    watchlist_id: int,
    observation_id: int,
    db: Session = Depends(get_db),
) -> ObservationRead:
    """恢复归档的观察项（WP2.3 + WP3.2，单事务写审计事件）。

    审计：写入 opportunity_transition_events（event_type='restore'），幂等。
    """
    item = restore_observation(db, watchlist_item_id=observation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Observation not found")
    # 写审计事件（幂等，重复恢复不重复写事件）
    try:
        _audit_restore_observation(
            db,
            watchlist_item_id=observation_id,
        )
    except ValueError:
        pass
    rich = get_observation_rich(db, watchlist_item_id=item.id)
    return ObservationRead(**rich.to_dict())


@router.post(
    "/watchlists/{watchlist_id}/observations/batch-import",
    response_model=ObservationBatchImportResult,
    tags=["observations"],
)
def batch_import_observations(
    watchlist_id: int,
    payload: ObservationBatchImport,
    db: Session = Depends(get_db),
) -> ObservationBatchImportResult:
    """批量幂等导入观察项（WP2.3，用于本地收藏迁移）。

    返回 imported / existing / failed 统计。
    幂等：相同 (watchlist_id, symbol_id) 视为已存在并计入 existing，不重复创建。
    """
    imported = 0
    existing = 0
    failed = 0
    errors: list[dict] = []

    for item_data in payload.items:
        try:
            # 先查询是否已存在（含 archived 状态，避免迁移过程产生重复项）
            existing_item = db.execute(
                select(WatchlistItem).where(
                    WatchlistItem.watchlist_id == watchlist_id,
                    WatchlistItem.symbol_id == item_data.symbol_id,
                )
            ).scalars().first()

            if existing_item is not None:
                existing += 1
                continue

            idempotent_add_observation(
                db,
                watchlist_id=watchlist_id,
                symbol_id=item_data.symbol_id,
                origin_type=item_data.origin_type,
                origin_id=item_data.origin_id,
                reason=item_data.reason,
                priority=item_data.priority,
                tags=item_data.tags,
                target_portfolio_id=item_data.target_portfolio_id,
                note=item_data.note,
            )
            imported += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
            failed += 1
            errors.append({
                "symbol_id": item_data.symbol_id,
                "error": str(exc),
            })

    return ObservationBatchImportResult(
        imported=imported,
        existing=existing,
        failed=failed,
        errors=errors,
    )
