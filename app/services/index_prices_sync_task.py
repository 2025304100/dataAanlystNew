"""多基准/自定义指数批量同步异步任务。

复用通用 async_tasks 基础设施（async_tasks 表 + 后台守护线程），
避免同步 HTTP 请求在长任务时超时，前端通过心跳轮询进度。

与 market_data_sync_task / external_data_sync_task 模式保持一致：
- create_index_prices_sync_task：创建记录 + 启动 worker
- _run_index_prices_sync：逐个 symbol 同步，写进度 percent / total / processed / ok_count
- 结果 JSON 结构与旧同步 IndexPriceSyncResponse 对齐，方便前端直接复用
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.services import index_data
from app.services.async_tasks import (
    _append_error,
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    get_async_task,
    list_async_tasks,
)

logger = logging.getLogger(__name__)
TASK_TYPE = "index_prices_sync"


def _default_symbols(symbols: list[str] | None) -> list[str]:
    """与 universe.py 逻辑一致：None → 默认 5 大基准。"""
    DEFAULT_5 = ["000300", "000905", "399006", "000016", "000688"]
    if symbols:
        return list(symbols)
    return list(DEFAULT_5)


def _name_of(symbol: str) -> str:
    """与 universe.py 逻辑一致。"""
    _BENCHMARK_DEFAULTS = [
        {"symbol": "000300", "name": "沪深300"},
        {"symbol": "000905", "name": "中证500"},
        {"symbol": "399006", "name": "创业板指"},
        {"symbol": "000016", "name": "上证50"},
        {"symbol": "000688", "name": "科创50"},
    ]
    for b in _BENCHMARK_DEFAULTS:
        if b["symbol"] == symbol:
            return b["name"]
    return symbol


def _check_cancelled(db: Session, task_id: str) -> bool:
    task = db.get(AsyncTaskRecord, task_id)
    return task is not None and task.status == "cancelled"


def create_index_prices_sync_task(
    *,
    symbols: list[str] | None = None,
    history_days: int = 1825,
    end_date: date | None = None,
) -> dict:
    """创建批量指数日线同步任务，立即返回任务快照，后台 daemon 线程执行。

    - symbols: None = 默认 5 大基准；否则指定列表（可包含自定义）
    - history_days: 回补天数，默认 5 年
    - end_date: 可选截止日期（默认今天）

    说明：不做并发去重，允许用户连续发起不同范围的同步（彼此独立入库，Upsert 幂等）。

    【性能关键点】use_control_plane=True：创建任务走 NullPool 控制平面引擎，
    不参与主 QueuePool 30 连接池排队（主池通常被 universe_init 8 worker
    长时间持连接扫 K 线）。这样提交 HTTP 接口即使在最忙的数据同步时段也能
    <50ms 返回 task_id，前端不会触发 axios 90s 超时 → 用户截图的
    『提交同步任务失败: 请求超时』不会再出现。
    """
    targets = _default_symbols(symbols)
    end = end_date or date.today()
    start = end - timedelta(days=max(30, int(history_days)))

    payload = {
        "symbols": targets,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "history_days": history_days,
    }

    task_read = create_async_task(TASK_TYPE, payload, use_control_plane=True)
    _start_worker(task_read.id, _run_index_prices_sync)
    return task_read.model_dump()


def get_index_prices_sync_task(task_id: str) -> dict | None:
    """查询指数同步任务状态（心跳轮询）。返回 dict。

    【性能关键点】use_control_plane=True：心跳每 2s 一次，SELECT BY PK 用
    NullPool 新连接直达 DB，不跟主池重任务抢连接，保证 <10ms 响应。
    """
    task_read = get_async_task(task_id, use_control_plane=True)
    if task_read is None:
        return None
    return task_read.model_dump()


def _run_index_prices_sync(task_id: str) -> None:
    """worker 线程：对 payload 中 symbols 逐个同步，实时刷新进度。"""
    SessionLocal = get_session_local()
    db = SessionLocal()
    items_result: list[dict] = []
    success = 0
    failed = 0

    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return

        # ── stage: prepare ─────────────────────────────
        payload = json.loads(task.payload_json or "{}")
        targets: list[str] = list(payload.get("symbols") or [])
        start_str: str | None = payload.get("start_date")
        end_str: str | None = payload.get("end_date")
        start_date = date.fromisoformat(start_str) if start_str else (date.today() - timedelta(days=365 * 5))
        end_date = date.fromisoformat(end_str) if end_str else date.today()
        total = len(targets)

        _set_task(
            db, task_id,
            status="running", stage="prepare",
            percent=1,
            total=total, processed=0, ok_count=0, failed_count=0,
            message=f"准备同步 {total} 个指数（{start_date} ~ {end_date}）...",
            started_at=_now(),
        )
        if _check_cancelled(db, task_id):
            return

        # ── stage: sync ─────────────────────────────────
        for idx, sym in enumerate(targets):
            if _check_cancelled(db, task_id):
                break

            percent = round(5.0 + 93.0 * (idx / max(total, 1)), 1)
            _set_task(
                db, task_id,
                stage="sync",
                percent=percent,
                processed=idx,
                current_item=sym,
                message=f"同步中 [{idx + 1}/{total}] {_name_of(sym)} ({sym})",
            )

            try:
                result = index_data.sync_index_daily(
                    db, sym,
                    start_date=start_date,
                    end_date=end_date,
                )
                first_d, last_d = result.date_range
                item = {
                    "symbol": sym,
                    "name": _name_of(sym),
                    "received": result.received,
                    "written": result.written,
                    "skipped": result.skipped,
                    "first_date": first_d.isoformat() if first_d else None,
                    "last_date": last_d.isoformat() if last_d else None,
                    "error": None,
                }
                items_result.append(item)
                success += 1
            except Exception as exc:
                failed += 1
                err_msg = str(exc)
                item = {
                    "symbol": sym,
                    "name": _name_of(sym),
                    "error": err_msg,
                }
                items_result.append(item)
                _append_error(task, {"scope": sym, "symbol": sym, "error": err_msg})

            # 每处理完一个 symbol 提交一次 DB，保证进度/错误持久化
            db.commit()

        if _check_cancelled(db, task_id):
            _set_task(
                db, task_id,
                status="cancelled", stage="cancelled",
                percent=100,
                processed=success + failed,
                ok_count=success,
                failed_count=failed,
                message=f"已取消（成功 {success} / 失败 {failed}）",
                result_json=json.dumps(
                    {"total": total, "success": success, "failed": failed, "items": items_result},
                    ensure_ascii=False, default=str,
                ),
                finished_at=_now(),
            )
            return

        # ── stage: done ─────────────────────────────────
        total_written = sum((i.get("written") or 0) for i in items_result)
        _set_task(
            db, task_id,
            status="done", stage="done",
            percent=100,
            total=total, processed=total,
            ok_count=success, failed_count=failed,
            current_item=None,
            message=(
                f"同步完成：{success}/{total} 成功，"
                f"累计写入 {total_written} 条，失败 {failed}"
                + ("，可展开详情查看错误" if failed > 0 else "")
            ),
            result_json=json.dumps(
                {"total": total, "success": success, "failed": failed, "items": items_result},
                ensure_ascii=False, default=str,
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("index_prices_sync task %s crashed", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            _append_error(task, {"scope": "worker", "error": str(exc)})
            task.status = "failed"
            task.stage = "failed"
            task.message = f"任务异常：{exc}"
            task.percent = 100
            task.finished_at = _now()
            task.updated_at = _now()
            task.result_json = json.dumps(
                {"total": task.total or 0, "success": task.ok_count or 0,
                 "failed": (task.failed_count or 0) + 1, "items": items_result},
                ensure_ascii=False, default=str,
            )
            db.commit()
    finally:
        db.close()


__all__ = [
    "TASK_TYPE",
    "create_index_prices_sync_task",
    "get_index_prices_sync_task",
]
