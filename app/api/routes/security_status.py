"""Local historical-status imports and bounded source downloads."""
from datetime import date
from uuid import UUID
import base64
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.security_status import SecurityStatusFileRequest, SecurityStatusImportRequest, SecurityStatusDownloadRequest
from app.services.security_status import local_import, history_download

router = APIRouter(prefix="/security-status")


@router.get("/download-scope")
def history_download_scope(portfolio_id: int = Query(ge=1), db: Session = Depends(get_db)):
    try:
        return history_download.download_scope(db, portfolio_id)
    except history_download.HistoryDownloadError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/downloads", status_code=202)
def start_history_download(payload: SecurityStatusDownloadRequest, db: Session = Depends(get_db)):
    try:
        return history_download.create_download(db, payload)
    except history_download.HistoryDownloadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/downloads/requests/{request_id}")
def history_download_request(request_id: UUID, portfolio_id: int = Query(ge=1), db: Session = Depends(get_db)):
    try:
        return history_download.find_request(db, portfolio_id, request_id)
    except history_download.HistoryDownloadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/downloads/{task_id}")
def history_download_result(task_id: str, db: Session = Depends(get_db)):
    try:
        result = history_download.read_download(db, task_id)
        if result is None:
            raise HTTPException(status_code=404, detail="没有找到这次历史资料下载，请重新读取组合范围。")
        return result
    except history_download.HistoryDownloadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/downloads/{task_id}/files")
def history_download_files(task_id: str, encoding: Literal["base64"] | None = None, db: Session = Depends(get_db)):
    try:
        content = history_download.download_files(db, task_id)
        if encoding == "base64":
            return {"filename": f"history-{task_id}.zip", "content_base64": base64.b64encode(content).decode("ascii")}
        return Response(content, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="history-{task_id}.zip"'})
    except history_download.HistoryDownloadError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/coverage")
def status_coverage(start_date: date | None = None, end_date: date | None = None,
                    symbol_ids: list[int] | None = Query(default=None), db: Session = Depends(get_db)):
    try:
        return local_import.get_coverage(db, start_date=start_date, end_date=end_date, symbol_ids=symbol_ids)
    except local_import.StatusImportError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/template")
def status_import_template():
    return local_import.template()


@router.post("/import/preview")
def preview_status_file(payload: SecurityStatusFileRequest, db: Session = Depends(get_db)):
    return local_import.preview_import(db, payload)


@router.post("/import")
def import_status_file(payload: SecurityStatusImportRequest, db: Session = Depends(get_db)):
    try:
        result = local_import.import_file(db, payload, preview_sha256=payload.preview_sha256)
        db.commit()
        return result
    except local_import.StatusImportError as error:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception:
        db.rollback()
        raise


@router.get("/imports/{batch_id}")
def status_import_receipt(batch_id: str, db: Session = Depends(get_db)):
    result = local_import.get_import_receipt(db, batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="没有找到这次历史状态导入记录。")
    return result


@router.get("/{symbol_id}/execution-constraints")
def status_execution_constraints(symbol_id: int, trade_date: date, db: Session = Depends(get_db)):
    constraints = local_import.get_imported_execution_constraints(db, symbol_id, trade_date)
    return {"available": constraints is not None, "constraints": constraints,
        "message": "已找到与当天历史状态和原文件一致的限价记录。" if constraints is not None
            else "缺少与当天历史状态和原文件一致的限价记录，不会按当前股票状态估算。"}
