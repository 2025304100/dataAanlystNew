"""历史行情镜像任务（MySQL → DuckDB 纯本地搬运；向导 §10；任务 T15）。

职责
====
把 `bar_mirror.mirror_daily_bars`（T04 基础设施，纯本地 SQL → DuckDB，
不做 AkShare 采集）包装成**异步任务**：
- 范围选择：5 年 / 10 年 / 全量 / 自定义区间（向导 §10.1）
- 分块续跑：区间按 **年** 切块，每块独立跑一次 mirror（独立 batch），
  完成一块立即落盘进度（`batch_recovery_json`）；**中断续跑跳过已完成块**
- **镜像持有 `duckdb_write` 锁**（任务卡硬规则）：提交时 peek + 获取，
  被占则 **409 拒绝不创建任务**（镜像是重活，排队等待的 UX 不如明确冲突）
- 不占 `mining_domain`
- 预计耗时 / 所需磁盘：**标注「估算」**；磁盘不足阻断（向导 §10.1）

**「不得重复行」由三层保证**：
1. 底层 `mirror_daily_bars` 按 `(symbol, trade_date)` upsert（幂等）
2. 分块区间**互不重叠**（本模块构造块时保证；测试钉死）
3. watermark 按 `base_key|start|end` 记录（`bar_mirror._watermark_key`），
   同一块重跑只补增量
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from app.models.async_task import AsyncTaskRecord
from app.services import task_state_machine as TSM
from app.services import async_tasks as AT
from app.services.async_tasks import (
    _now,
    _set_task,
    _start_worker,
    create_async_task,
)
from app.services.factors.mining import task_lock as TL

logger = logging.getLogger(__name__)

#: 异步任务类型
TASK_TYPE = "data_mirror"

#: 范围预设（向导 §10.1）
RANGE_PRESETS: dict[str, dict[str, Any]] = {
    "5y": {"label_zh": "5 年（默认）", "start": date(2021, 1, 1)},
    "10y": {"label_zh": "10 年", "start": date(2016, 7, 12)},
    "full": {"label_zh": "全量（同 10 年，含早期稀疏年份）",
             "start": date(2016, 7, 12)},
}
VALID_PRESETS = ("5y", "10y", "full", "custom")

#: 分块粒度：按自然年切块（续跑粒度；块数 = 跨年数，5 年 ≤ 6 块）
CHUNK_GRANULARITY = "year"

#: 估算用吞吐（行/秒）。**只用于展示「估算」**，实测以压测为准（向导 §10.3）
ESTIMATED_ROWS_PER_SECOND = 20_000

#: 每行估算磁盘占用（字节，含 DuckDB 压缩前开销）。同样标注「估算」。
ESTIMATED_BYTES_PER_ROW = 800

#: 提交时若仓库磁盘剩余 < 预估占用 → 阻断
DISK_SAFETY_MARGIN = 1.2

MirrorFunc = Callable[..., Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _freeze_json(payload: Any) -> str:
    """保留 None 的紧凑序列化（同 T11/T14，见 canonical_json 丢 None 的教训）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


# ══════════════════════════════════════════════════════════
# 范围解析与分块
# ══════════════════════════════════════════════════════════


def resolve_range(*, preset: str | None = None,
                  start_date: date | None = None,
                  end_date: date | None = None) -> tuple[date, date, str]:
    """解析范围 → `(start, end, preset)`。custom 必须显式给起止。"""
    preset = (preset or "5y").strip().lower()
    if preset not in VALID_PRESETS:
        raise ValueError(
            f"preset 必须是 {list(VALID_PRESETS)} 之一，收到 {preset!r}。")
    if preset == "custom":
        if start_date is None or end_date is None:
            raise ValueError("preset=custom 必须提供 start_date 与 end_date。")
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date。")
        return start_date, end_date, preset
    if start_date or end_date:
        raise ValueError(f"preset={preset} 不接受自定义起止；请用 preset='custom'。")
    start = RANGE_PRESETS[preset]["start"]
    return start, _utcnow().date(), preset


def build_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """按自然年切块；区间**含端点**且**互不重叠**（「不得重复行」的第二层保证）。"""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        year_end = min(date(cur.year, 12, 31), end)
        chunks.append((cur, year_end))
        cur = year_end + timedelta(days=1)
    return chunks


# ══════════════════════════════════════════════════════════
# 估算（全部标注「估算」；向导 §10.1）
# ══════════════════════════════════════════════════════════


def estimate_rows(db: Any, start: date, end: date) -> dict[str, Any]:
    """按 `universe_daily_bars` 估算行数 / 标的数（真实 COUNT，仍标「估算」：
    镜像行数 = 源表行数，但 DuckDB 落盘后可能因去重略有出入）。"""
    from app.models.universe import UniverseDailyBar
    from sqlalchemy import func, select

    stmt = select(
        func.count(UniverseDailyBar.id),
        func.count(func.distinct(UniverseDailyBar.universe_symbol_id)),
    ).where(UniverseDailyBar.trade_date >= start,
            UniverseDailyBar.trade_date <= end)
    rows, symbols = db.execute(stmt).one()
    return {
        "estimated": True,
        "estimated_rows": int(rows or 0),
        "estimated_symbols": int(symbols or 0),
        "estimated_seconds": round((rows or 0) / ESTIMATED_ROWS_PER_SECOND, 1),
        "estimated_bytes": int((rows or 0) * ESTIMATED_BYTES_PER_ROW),
        "throughput_rows_per_second": ESTIMATED_ROWS_PER_SECOND,
        "bytes_per_row": ESTIMATED_BYTES_PER_ROW,
        "note_zh": "以上均为**估算值**（规则：源表 COUNT ÷ 吞吐假设），"
                   "实际以任务完成后展示为准。",
    }


def check_disk_space(warehouse_path: Any, estimated_bytes: int) -> dict[str, Any]:
    """磁盘剩余不足 → 阻断（向导 §10.1「磁盘检查」）。"""
    need = int(estimated_bytes * DISK_SAFETY_MARGIN)
    usage = shutil.disk_usage(str(warehouse_path))
    ok = usage.free >= need
    return {
        "ok": ok,
        "free_bytes": int(usage.free),
        "required_bytes": need,
        "warehouse_path": str(warehouse_path),
        "note_zh": None if ok else (
            f"磁盘剩余 {usage.free / 1e9:.1f} GB，"
            f"预估需要 {need / 1e9:.1f} GB（含 {DISK_SAFETY_MARGIN:.1f}x 安全余量），"
            "不足以完成本次镜像。"
        ),
    }


# ══════════════════════════════════════════════════════════
# 提交
# ══════════════════════════════════════════════════════════


def _peek_duckdb_write_busy(db: Any) -> dict[str, Any] | None:
    status = TL.get_lock_status(db)
    if status.duckdb_write.get("busy"):
        return status.duckdb_write
    return None


def create_mirror_task(
    *, preset: str | None = None,
    start_date: date | None = None, end_date: date | None = None,
    batch_size: int = 1000,
    operator_id: str = "system",
    mirror_func: MirrorFunc | None = None,
    skip_disk_check: bool = False,
) -> dict[str, Any]:
    """创建镜像任务（**同步部分**：peek 锁 → 建任务 → 持有 duckdb_write → 启 worker）。

    Raises:
        ValueError: preset / 区间 / 磁盘空间不合法。
        TL.MiningDomainBusy 同款语义不适用；`duckdb_write` 被占时抛
        `RuntimeError`（路由层转 409 `DUCKDB_WRITE_BUSY`）—— **未创建任务**。
    """
    start, end, resolved_preset = resolve_range(preset=preset,
                                                start_date=start_date,
                                                end_date=end_date)
    chunks = build_chunks(start, end)

    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        est = estimate_rows(db, start, end)
        # 🚨 镜像**持有 duckdb_write**（任务卡硬规则）。被占 → 拒绝且**不创建任务**
        #    （镜像是重活，冲突时排队等待的 UX 不如明确冲突 + 冲突方信息）。
        busy = _peek_duckdb_write_busy(db)
        if busy:
            raise RuntimeError(json.dumps({
                "error_code": "DUCKDB_WRITE_BUSY",
                "owner_task_id": busy.get("taskId"),
                "queue": busy.get("queue") or [],
            }, ensure_ascii=False))
        if not skip_disk_check:
            from app.services.factors.config import get_factor_system_config

            disk = check_disk_space(get_factor_system_config(db).warehouse_path,
                                    est["estimated_bytes"])
            if not disk["ok"]:
                raise ValueError(disk["note_zh"])

        payload = {
            "preset": resolved_preset,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "chunks": [[s.isoformat(), e.isoformat()] for s, e in chunks],
            "batch_size": int(batch_size),
            "operator_id": operator_id,
            "estimated_rows": est["estimated_rows"],
        }
        task = create_async_task(TASK_TYPE, payload, use_control_plane=True,
                                 force_new=True)

        # 持有 duckdb_write（peek 后仍可能被抢 → 竞态兜底：标 failed 留痕）
        try:
            TL.acquire_write_slot(db, task_id=task.id, run_id=task.id)
        except Exception as exc:
            _set_task(db, task.id, status="failed", stage="rejected",
                      message=f"DUCKDB_WRITE_BUSY(race): {exc}",
                      finished_at=_now())
            raise
    finally:
        db.close()

    _start_worker(task.id, _make_worker(mirror_func))
    return {
        "task_id": task.id,
        "preset": resolved_preset,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "chunks": [[s.isoformat(), e.isoformat()] for s, e in chunks],
        "estimated": {k: v for k, v in est.items()},
        "status": task.status,
    }


def _make_worker(mirror_func: MirrorFunc | None) -> Callable[[str], None]:
    def _worker(task_id: str) -> None:
        run_mirror_worker(task_id, mirror_func=mirror_func)
    return _worker


# ══════════════════════════════════════════════════════════
# worker：分块执行 + 断点续跑
# ══════════════════════════════════════════════════════════


def run_mirror_worker(task_id: str, *, mirror_func: MirrorFunc | None = None) -> None:
    """镜像 worker：逐块执行 mirror，每块落盘一次进度（断点续跑依据）。

    `mirror_func` 缺省用 `bar_mirror.mirror_daily_bars`（真实搬运）；
    测试注入 fake 以验证分块/续跑语义而不搬真数据。
    """
    if mirror_func is None:
        from app.services.factors.bar_mirror import mirror_daily_bars

        mirror_func = mirror_daily_bars

    from app.db.session import get_session_local

    db = get_session_local()()
    stop_event = threading.Event()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status in ("cancelled", "done"):
            return
        payload = json.loads(task.payload_json) if task.payload_json else {}
        chunks = [(date.fromisoformat(s), date.fromisoformat(e))
                  for s, e in payload.get("chunks") or []]
        batch_size = int(payload.get("batch_size") or 1000)
        est_rows = int(payload.get("estimated_rows") or 0)

        TSM.transition(task_id, "running", db=db)
        _set_task(db, task_id, status="running", stage="mirroring",
                  message=f"0/{len(chunks)} chunks", started_at=_now())

        recovery = json.loads(task.batch_recovery_json) if task.batch_recovery_json else {}
        done_chunks = {tuple(c) for c in (recovery.get("completed_chunks") or [])}

        total_rows = 0
        batches: list[dict[str, Any]] = []
        for idx, (cstart, cend) in enumerate(chunks):
            db.expire_all()
            current = db.get(AsyncTaskRecord, task_id)
            if current is None or current.status == "cancelled":
                return
            if int(getattr(current, "cancel_requested", 0) or 0) == 1:
                _set_task(db, task_id, status="cancelled", stage="cancelled",
                          message=f"paused by user at chunk {idx}/{len(chunks)}",
                          finished_at=_now())
                return
            if AT.is_worker_stop_requested(task_id=task_id):
                _set_task(db, task_id, status="cancelled", stage="cancelled",
                          message=f"paused at chunk {idx}/{len(chunks)}",
                          finished_at=_now())
                return
            if (cstart.isoformat(), cend.isoformat()) in done_chunks:
                continue                  # 断点续跑：不重复已完成块

            stop_flag = {"stop": False}

            def _should_cancel() -> bool:
                db.expire_all()
                cur = db.get(AsyncTaskRecord, task_id)
                if cur is not None and int(getattr(cur, "cancel_requested", 0) or 0) == 1:
                    stop_flag["stop"] = True
                return stop_flag["stop"] or AT.is_worker_stop_requested(
                    task_id=task_id)

            def _progress(done_rows: int, *_a: Any) -> None:
                # 进度回调：块内粗粒度刷新（不落盘，避免高频 commit）
                db.expire_all()
                _set_task(
                    db, task_id,
                    message=f"chunk {idx + 1}/{len(chunks)} "
                            f"({cstart}~{cend}) rows={done_rows}",
                )

            result = mirror_func(
                db, start_date=cstart, end_date=cend, batch_size=batch_size,
                should_cancel=_should_cancel, progress_callback=_progress,
            )
            result_dict = result.to_dict() if hasattr(result, "to_dict") else {}
            total_rows += int(result_dict.get("rows_written") or 0)
            batches.append({
                "chunk": [cstart.isoformat(), cend.isoformat()],
                "batch_id": result_dict.get("batch_id"),
                "rows_written": result_dict.get("rows_written") or 0,
            })

            # 每块立即落盘进度（断点续跑依据）
            with _progress_lock():
                cur_task = db.get(AsyncTaskRecord, task_id)
                if cur_task is None:
                    return
                rec = json.loads(cur_task.batch_recovery_json) if \
                    cur_task.batch_recovery_json else {}
                done_list = rec.get("completed_chunks") or []
                done_list.append([cstart.isoformat(), cend.isoformat()])
                cur_task.batch_recovery_json = _freeze_json({
                    "completed_chunks": done_list, "completed": len(done_list),
                    "total": len(chunks), "batches": batches,
                })
                cur_task.processed = idx + 1
                cur_task.percent = (int(idx * 100 / len(chunks))
                                    if len(chunks) else 100)
                cur_task.message = f"{idx + 1}/{len(chunks)} chunks done"
                db.commit()

            if stop_flag["stop"]:
                _set_task(db, task_id, status="cancelled", stage="cancelled",
                          message=f"paused by user at chunk {idx + 1}/{len(chunks)}",
                          finished_at=_now())
                return

        percent = 100
        _set_task(
            db, task_id,
            status="done", stage="mirrored", percent=percent,
            message=f"mirrored {total_rows} rows across {len(chunks)} chunks",
            result_json=_freeze_json({
                "total_rows": total_rows,
                "chunks_total": len(chunks),
                "batches": batches,
                "start_date": payload.get("start_date"),
                "end_date": payload.get("end_date"),
                "note_zh": "因子仓库数据区间已更新，可重新运行评估（向导 §10.1）。",
            }),
            finished_at=_now(),
        )
    finally:
        stop_event.set()
        # 🚨 镜像持有 duckdb_write（任务卡硬规则）—— 任何出口都必须释放
        try:
            _release_write_lock(task_id)
        except Exception:  # noqa: BLE001 - 释放失败只记日志，不掩盖主流程
            logger.exception("release duckdb_write failed for %s", task_id)
        db.close()


_progress_lock_obj = threading.Lock()


def _progress_lock() -> threading.Lock:
    return _progress_lock_obj


def _release_write_lock(task_id: str) -> str | None:
    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        status = TL.get_lock_status(db)
        if status.duckdb_write.get("taskId") == task_id:
            return TL.release_lock(db, lock_key=TL.LOCK_DUCKDB_WRITE,
                                   task_id=task_id)
        return None
    finally:
        db.close()


# ══════════════════════════════════════════════════════════
# 状态查询（向导 §10.1「当前状态」区）
# ══════════════════════════════════════════════════════════


def get_mirror_status(db: Any, warehouse: Any | None = None) -> dict[str, Any]:
    """已镜像区间 / 行数 / 标的数 / 低覆盖年份标注（向导 §10.2）。"""
    from app.services.factors.config import get_factor_system_config
    from app.services.factors.candidate_pool import rules as pool_rules

    config = get_factor_system_config(db)
    path = config.warehouse_path
    try:
        import duckdb

        conn = duckdb.connect(str(path), read_only=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(trade_date), "
                "MAX(trade_date) FROM raw_daily_bars"
            ).fetchone()
        finally:
            conn.close()
        available = True
    except Exception as exc:  # noqa: BLE001 - 数仓缺失/被占 → 明示不可用
        return {"available": False, "error": str(exc)[:300],
                "warehouse_path": str(path)}

    # 低覆盖年份（向导 §10.2：标的数 < 4000，2016–2018 + 边界提示）
    low_years: list[dict[str, Any]] = []
    try:
        conn = duckdb.connect(str(path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT year(trade_date) AS y, COUNT(DISTINCT symbol) AS n "
                "FROM raw_daily_bars GROUP BY 1 ORDER BY 1"
            ).fetchall()
        finally:
            conn.close()
        for y, n in rows:
            if n < 4000:
                low_years.append({
                    "year": int(y), "symbols": int(n),
                    "boundary_hint": int(y) in (2019, 2020),
                    "note_zh": "低覆盖年份：默认不参与 S/A 级因子评定；"
                               "注意幸存者偏差风险。",
                })
    except Exception:  # noqa: BLE001 - 标注失败不影响主状态
        pass

    return {
        "available": True,
        "warehouse_path": str(path),
        "total_rows": int(row[0] or 0),
        "total_symbols": int(row[1] or 0),
        "mirrored_from": str(row[2]) if row[2] else None,
        "mirrored_to": str(row[3]) if row[3] else None,
        "low_coverage_years": low_years,
        "note_zh": "低覆盖年份（标的数 < 4000）的样本默认不参与 S/A 级评定；"
                   "固定提示幸存者偏差风险（向导 §10.2）。",
    }


def list_mirror_tasks(db: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    from sqlalchemy import select

    rows = db.execute(
        select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == TASK_TYPE)
        .order_by(AsyncTaskRecord.created_at.desc()).limit(limit)
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = json.loads(r.payload_json) if r.payload_json else {}
        recovery = json.loads(r.batch_recovery_json) if r.batch_recovery_json else {}
        out.append({
            "task_id": r.id, "status": r.status, "stage": r.stage,
            "preset": payload.get("preset"),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "completed_chunks": int(recovery.get("completed") or 0),
            "total_chunks": len(payload.get("chunks") or []),
            "message": r.message,
            "created_at": r.created_at,
            "finished_at": r.finished_at,
        })
    return out


def get_mirror_task_progress(db: Any, task_id: str) -> dict[str, Any]:
    task = db.get(AsyncTaskRecord, task_id)
    if task is None or task.task_type != TASK_TYPE:
        return {"found": False}
    payload = json.loads(task.payload_json) if task.payload_json else {}
    recovery = json.loads(task.batch_recovery_json) if task.batch_recovery_json else {}
    report = json.loads(task.result_json) if task.result_json else None
    return {
        "found": True,
        "task_id": task.id,
        "status": task.status,
        "preset": payload.get("preset"),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "completed_chunks": int(recovery.get("completed") or 0),
        "total_chunks": len(payload.get("chunks") or []),
        "completed_chunk_ranges": recovery.get("completed_chunks") or [],
        "rows_written": (report or {}).get("total_rows"),
        "message": task.message,
        "report": report,
    }


def resume_mirror_task(
    *, task_id: str, mirror_func: MirrorFunc | None = None,
    skip_disk_check: bool = True,
) -> dict[str, Any]:
    """断点续跑入口：对 failed/cancelled 任务以**相同 payload** 新建任务。

    已完成块由 `batch_recovery_json` 天然跳过（不重复执行）；
    新任务继承同 preset/区间 → 同分块边界。
    """
    from app.db.session import get_session_local

    db = get_session_local()()
    old_recovery: str | None = None
    try:
        old = db.get(AsyncTaskRecord, task_id)
        if old is None or old.task_type != TASK_TYPE:
            raise ValueError(f"镜像任务不存在: {task_id}")
        payload = json.loads(old.payload_json) if old.payload_json else {}
        # 🚨 断点续跑的核心：把旧任务的**分块进度**带到新任务。
        #    T14 第一版漏了这步，续跑从零开始、重复执行已完成块 ——
        #    直接违反「watermark 分块续跑不得重复行」硬规则。
        old_recovery = old.batch_recovery_json
    finally:
        db.close()

    task = create_async_task(TASK_TYPE, payload, use_control_plane=True,
                             force_new=True)

    # 进度迁移（先于 worker 启动，避免竞态）
    if old_recovery:
        db = get_session_local()()
        try:
            new_task = db.get(AsyncTaskRecord, task.id)
            if new_task is not None:
                new_task.batch_recovery_json = old_recovery
                db.commit()
        finally:
            db.close()

    # 续跑同样要持有 duckdb_write（终态旧任务已释放；竞态则标 failed 留痕）
    db = get_session_local()()
    try:
        try:
            TL.acquire_write_slot(db, task_id=task.id, run_id=task.id)
        except Exception as exc:
            _set_task(db, task.id, status="failed", stage="rejected",
                      message=f"DUCKDB_WRITE_BUSY(race): {exc}",
                      finished_at=_now())
            raise
    finally:
        db.close()

    _start_worker(task.id, _make_worker(mirror_func))
    return {
        "task_id": task.id, "resumed_from": task_id,
        "preset": payload.get("preset"),
        "status": task.status,
        "total_chunks": len(payload.get("chunks") or []),
    }


__all__ = [
    "TASK_TYPE",
    "RANGE_PRESETS",
    "VALID_PRESETS",
    "ESTIMATED_ROWS_PER_SECOND",
    "ESTIMATED_BYTES_PER_ROW",
    "DISK_SAFETY_MARGIN",
    "resolve_range",
    "build_chunks",
    "estimate_rows",
    "check_disk_space",
    "create_mirror_task",
    "resume_mirror_task",
    "run_mirror_worker",
    "get_mirror_status",
    "list_mirror_tasks",
    "get_mirror_task_progress",
]
