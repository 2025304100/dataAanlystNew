"""Small historical-source receipts; incomplete data never enters the PIT table."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import zipfile

from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import PortfolioMember
from app.models.symbol import Symbol
from app.services import async_tasks
from app.core.config import settings
from app.db.session import get_session_local
from app.services.security_status import baostock_source

TASK_TYPE = "security_status_download"
MAX_SYMBOLS = 20
MAX_DAYS = 366
REPORT_VERSION = 2
ARTIFACT_ROOT = settings.base_dir / "tmp" / "security-status-downloads"
_DISPATCH_LOCK = threading.Lock()
MISSING_FIELDS = ["is_delisting_period", "daily_price_limits", "source_updated_at"]


class HistoryDownloadError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def download_scope(db, portfolio_id: int) -> dict:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HistoryDownloadError("没有找到当前组合，请重新选择组合。")
    ids = set(db.scalars(select(PortfolioMember.symbol_id).where(
        PortfolioMember.portfolio_id == portfolio_id, PortfolioMember.effective_to.is_(None),
        PortfolioMember.status.in_(("active", "paused")))))
    ids.update(db.scalars(select(Position.symbol_id).where(
        Position.portfolio_id == portfolio_id, Position.quantity > 0)))
    rows = list(db.scalars(select(Symbol).where(Symbol.id.in_(ids)).order_by(Symbol.symbol))) if ids else []
    symbols = []
    for row in rows:
        market = row.market.upper()
        supported = (market in {"SH", "SZ"} and row.asset_type in {"stock", "etf"}
                     and bool(re.fullmatch(r"\d{6}", row.symbol)))
        symbols.append({"symbol_id": row.id, "symbol": row.symbol, "name": row.name, "asset_type": row.asset_type,
                        "provider_code": f"{market.lower()}.{row.symbol}" if supported else None,
                        "supported": supported})
    reasons = []
    if len(rows) != len(ids):
        reasons.append("组合中有找不到的标的，请先检查组合成员。")
    if not symbols:
        reasons.append("组合还没有成员或持仓，请先在组合中添加标的。")
    if len(symbols) > MAX_SYMBOLS:
        reasons.append(f"这项下载一次最多支持 {MAX_SYMBOLS} 个标的，当前组合有 {len(symbols)} 个。请先缩小组合范围。")
    if any(not s["supported"] for s in symbols):
        reasons.append("当前来源只接入了沪深股票和ETF；组合中有暂不支持的标的，不能按全组合下载。")
    latest = None
    for task in db.scalars(select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == TASK_TYPE)
                           .order_by(AsyncTaskRecord.created_at.desc())):
        if json.loads(task.payload_json or "{}").get("portfolio_id") == portfolio_id:
            latest = async_tasks._task_to_read(task).model_dump(mode="json")
            break
    end = datetime.now(timezone(timedelta(hours=8))).date() - timedelta(days=1)
    return {"portfolio_id": portfolio_id, "portfolio_name": portfolio.name, "symbols": symbols,
            "scope_sha256": hashlib.sha256(_canonical(symbols).encode()).hexdigest(),
            "start_date": (end - timedelta(days=364)).isoformat(), "end_date": end.isoformat(),
            "max_symbols": MAX_SYMBOLS, "max_days": MAX_DAYS,
            "can_download": not reasons, "reasons": reasons, "latest_task": latest}


def _request_id(portfolio_id, request_id):
    return hashlib.sha256(f"{TASK_TYPE}|{portfolio_id}|{request_id}".encode()).hexdigest()[:32]


def _read_task(db, task_id):
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        return None
    row = db.get(AsyncTaskRecord, task_id, populate_existing=True)
    if row is None or row.task_type != TASK_TYPE:
        return None
    return async_tasks._task_to_read(row).model_dump(mode="json")


def find_request(db, portfolio_id, request_id):
    task = _read_task(db, _request_id(portfolio_id, request_id))
    return {"task": task, "report": None} if task is None else read_download(db, task["id"])


def create_download(db, payload):
    request = payload.model_dump(mode="json")
    task_id = _request_id(payload.portfolio_id, payload.request_id)
    fingerprint = hashlib.sha256(_canonical(request).encode()).hexdigest()
    with _DISPATCH_LOCK:
        existing = db.get(AsyncTaskRecord, task_id, populate_existing=True)
        if existing is not None:
            if existing.task_type != TASK_TYPE or existing.idempotency_key != "request:" + fingerprint:
                raise HistoryDownloadError("这次下载请求的内容已改变，请重新读取组合范围后再下载。")
            return read_download(db, task_id)
        scope = download_scope(db, payload.portfolio_id)
        if not scope["can_download"]:
            raise HistoryDownloadError(" ".join(scope["reasons"]))
        if scope["scope_sha256"] != payload.expected_scope_sha256:
            raise HistoryDownloadError("组合成员或持仓已改变，请重新读取范围后再下载。")
        today = datetime.now(timezone(timedelta(hours=8))).date()
        if payload.start_date > payload.end_date or payload.end_date >= today:
            raise HistoryDownloadError("请选择已经结束的日期，开始日期不能晚于结束日期。")
        if (payload.end_date - payload.start_date).days + 1 > MAX_DAYS:
            raise HistoryDownloadError(f"一次最多下载 {MAX_DAYS} 天，请缩短日期范围。")
        active = db.scalar(select(AsyncTaskRecord.id).where(AsyncTaskRecord.task_type == TASK_TYPE,
                            AsyncTaskRecord.status.in_(("queued", "running"))).limit(1))
        if active:
            raise HistoryDownloadError("已有历史资料正在下载，请等它结束后再下载。")
        request["symbols"] = scope["symbols"]
        request["portfolio_name"] = scope["portfolio_name"]
        task, created = async_tasks.create_async_task_once(TASK_TYPE, request, task_id=task_id,
                                                         request_fingerprint=fingerprint)
        if created:
            with get_session_local()() as status_db:
                row = async_tasks._set_task(status_db, task.id, message="正在等待下载历史股票状态")
                task = async_tasks._task_to_read(row)
            async_tasks._start_worker(task.id, _download_worker)
        return {"task": task.model_dump(mode="json"), "report": None}


def _save_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _raw_query(directory, capture, key, endpoint, parameters, columns, errors):
    receipts = [q for q in capture.get("queries", []) if q.get("key") == key]
    try:
        if len(receipts) != 1:
            raise ValueError("没有取得唯一的原始返回")
        receipt = receipts[0]
        if receipt["file"] != key + ".json":
            raise ValueError("原始文件名称不符")
        data = (directory / receipt["file"]).read_bytes()
        if len(data) > 8_000_000 or hashlib.sha256(data).hexdigest() != receipt["sha256"]:
            raise ValueError("原始文件校验未通过")
        value = json.loads(data)
        if (not isinstance(value, dict) or not isinstance(value.get("parameters"), dict)
            or value.get("endpoint") != endpoint or any(value["parameters"].get(k) != v for k, v in parameters.items())):
            raise ValueError("返回结果与本次请求不符")
        raw = value["raw"]
        if not isinstance(raw, dict) or raw.get("error_code") != "0":
            raise ValueError("数据来源返回失败，未按空数据处理")
        fields, rows = raw["fields"], raw["rows"]
        if not isinstance(fields, list) or len(set(fields)) != len(fields) or not set(columns).issubset(fields):
            raise ValueError("返回字段缺失或重复")
        if not isinstance(rows, list) or len(rows) > MAX_DAYS or any(not isinstance(r, list) or len(r) != len(fields) for r in rows):
            raise ValueError("返回行数或格式不符")
        return [dict(zip(fields, row)) for row in rows]
    except (OSError, ValueError, TypeError, KeyError):
        label = "交易日历" if key == "calendar" else ("上市和退市日期" if key.startswith("basic_") else "逐日状态")
        errors.append(f"{label}下载失败或返回格式不符，请查看原始文件。")
        return []


def _iso_day(value):
    return date.fromisoformat(value) if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def _build_report(directory, request, source_error):
    try:
        capture = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        capture = {"queries": [], "complete": False}
    errors = []
    start, end = date.fromisoformat(request["start_date"]), date.fromisoformat(request["end_date"])
    raw_calendar = _raw_query(directory, capture, "calendar", "query_trade_dates",
        {"start_date": request["start_date"], "end_date": request["end_date"]},
        ["calendar_date", "is_trading_day"], errors)
    calendar = {}
    for row in raw_calendar:
        try:
            day = _iso_day(row["calendar_date"])
            if day is None or not start <= day <= end or day in calendar or row["is_trading_day"] not in {"0", "1"}:
                raise ValueError()
            calendar[day] = row["is_trading_day"] == "1"
        except (TypeError, ValueError):
            errors.append("交易日历有重复、越界日期或未知状态。")
    calendar_complete = len(calendar) == (end - start).days + 1 and not errors
    if not calendar_complete:
        errors.append("所选日期的交易日历不完整，不能确认状态资料是否齐全。")
    summaries = []
    for symbol in request["symbols"]:
        code, raw_code = symbol["symbol"], symbol["provider_code"]
        issues = []
        basic = _raw_query(directory, capture, "basic_" + code, "query_stock_basic", {"code": raw_code},
                           ["code", "ipoDate", "outDate"], issues)
        ipo = out = None
        try:
            if len(basic) != 1 or basic[0]["code"] != raw_code:
                raise ValueError()
            ipo, out = _iso_day(basic[0]["ipoDate"]), _iso_day(basic[0]["outDate"])
            if ipo is None or (basic[0]["outDate"] and out is None) or (out and out <= ipo):
                raise ValueError()
        except (ValueError, TypeError):
            ipo = out = None
            issues.append("上市或退市日期没有通过检查；未使用当前名称或当前上市状态回填历史。")
        rows = _raw_query(directory, capture, "daily_" + code, "query_history_k_data_plus",
            {"code": raw_code, "start_date": request["start_date"], "end_date": request["end_date"],
             "frequency": "d", "adjustflag": "3"}, ["date", "code", "tradestatus", "isST", "adjustflag"], issues)
        # This source contract documents stock status flags. ETF raw rows may
        # return isST=1 for long periods; do not turn that into verified ST days
        # (or silently change it to zero) without an ETF-specific status source.
        if symbol.get("asset_type") != "stock":
            issues.append("ETF的历史状态字段尚未核实，已保留原始文件，暂不按股票状态计算。")
            summaries.append({"symbol": code, "symbol_id": symbol["symbol_id"], "display_name": symbol["name"],
                "asset_type": symbol.get("asset_type"), "raw_daily_rows": len(rows), "valid_daily_rows": 0,
                "st_days": None, "suspended_days": None, "missing_trade_dates": None,
                "errors": list(dict.fromkeys(issues)), "days": []})
            continue
        days, seen = [], set()
        for row in rows:
            try:
                day = _iso_day(row["date"])
                if (day is None or not start <= day <= end or day in seen or row["code"] != raw_code
                    or row["adjustflag"] != "3" or row["tradestatus"] not in {"0", "1"}
                    or row["isST"] not in {"0", "1"} or (calendar_complete and not calendar.get(day))
                    or (ipo and day < ipo) or (out and day >= out)):
                    raise ValueError()
                seen.add(day)
                days.append({"trade_date": day.isoformat(), "is_st": row["isST"] == "1",
                    "is_suspended": row["tradestatus"] == "0", "is_delisting_period": None,
                    "is_listed": True if ipo else None, "listing_date": ipo.isoformat() if ipo else None,
                    "delisting_date": out.isoformat() if out and out <= day else None,
                    "price_limit_mode": None, "upper_limit": None, "lower_limit": None, "price_tick": None})
            except (ValueError, TypeError):
                issues.append("逐日返回有重复、越界日期、代码不符或未知状态；该行仅保留原始返回。")
        missing = (sorted(d.isoformat() for d, trading in calendar.items() if trading and d not in seen
                          and (not ipo or d >= ipo) and (not out or d < out)) if calendar_complete else None)
        if missing:
            issues.append(f"缺少 {len(missing)} 个交易日的逐日状态。")
        summaries.append({"symbol": code, "symbol_id": symbol["symbol_id"], "display_name": symbol["name"],
            "raw_daily_rows": len(rows), "valid_daily_rows": len(days), "st_days": sum(d["is_st"] for d in days),
            "suspended_days": sum(d["is_suspended"] for d in days), "missing_trade_dates": missing,
            "errors": list(dict.fromkeys(issues)), "days": sorted(days, key=lambda d: d["trade_date"])})
    # A checked receipt still lacks the three required sources; it cannot be imported.
    receipts = []
    for query in capture.get("queries", []):
        filename = query.get("file", "")
        if re.fullmatch(r"(?:calendar|(?:basic|daily)_\d{6})\.json", filename):
            path = directory / filename
            if path.is_file():
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() == query.get("sha256"):
                    receipts.append({"file": filename, "sha256": query["sha256"], "bytes": len(data)})
    return {"report_version": REPORT_VERSION, "provider": "BaoStock", "sdk_version": "0.9.4", "portfolio_id": request["portfolio_id"],
        "portfolio_name": request["portfolio_name"], "start_date": request["start_date"], "end_date": request["end_date"],
        "saved_at": datetime.now(timezone.utc).isoformat(), "source_updated_at": None,
        "can_import": False, "backtest_ready": False, "missing_fields": MISSING_FIELDS,
        "calendar_complete": calendar_complete, "valid_daily_rows": sum(s["valid_daily_rows"] for s in summaries),
        "symbols": summaries, "errors": list(dict.fromkeys(errors)), "source_error": source_error,
        "capture_complete": capture.get("complete") is True, "raw_files": receipts,
        "message": "原始返回已保存；仍缺退市整理状态、每日限价和资料原始更新时间，尚不能用于正式回测。"}


def _download_worker(task_id):
    db = get_session_local()()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status != "queued" or task.cancel_requested:
            return
        request = json.loads(task.payload_json)
        directory = ARTIFACT_ROOT / task_id
        directory.mkdir(parents=True, exist_ok=False)
        _save_json(directory / "request.json", request)
        total = 1 + len(request["symbols"]) * 2
        async_tasks._set_task(db, task_id, status="running", stage="download", total=total,
            message="正在下载历史股票状态，原始返回会保存在本机", started_at=async_tasks._now(),
            heartbeat_at=async_tasks._now(), stage_budget_seconds=150)

        def progress(processed, current):
            db.expire_all()
            row = db.get(AsyncTaskRecord, task_id)
            if (row.cancel_requested or row.status not in {"queued", "running"}
                or async_tasks.is_worker_stop_requested(task_id)):
                raise baostock_source.DownloadCancelled()
            async_tasks._set_task(db, task_id, processed=min(total, processed), current_item=current[:64],
                percent=min(95, processed / total * 95), heartbeat_at=async_tasks._now(),
                message=f"正在下载历史股票状态（{min(processed, total)}/{total}）")

        source_error, cancelled = None, False
        try:
            baostock_source.capture_history(request, directory, progress)
            progress(total, "检查原始返回")
        except baostock_source.DownloadCancelled:
            source_error, cancelled = "下载已停止，已取得的原始文件仍保留。", True
        except baostock_source.SourceDownloadError as error:
            source_error = str(error)
        except Exception:
            source_error = "这次下载未完成，已取得的原始文件仍保留，请稍后重试。"
        report = _build_report(directory, request, source_error)
        _save_json(directory / "report.json", report)
        complete = (not source_error and report["capture_complete"] and not report["errors"]
                    and not any(s["errors"] for s in report["symbols"]))
        status = "cancelled" if cancelled else "done" if complete else "partial" if report["valid_daily_rows"] else "failed"
        message = ("下载完成，历史资料仍不完整。" if complete else source_error or "原始文件已保存，部分状态仍待核对。")
        db.expire_all()
        row = db.get(AsyncTaskRecord, task_id)
        if row.cancel_requested or async_tasks.is_worker_stop_requested(task_id):
            status, message = "cancelled", "下载已停止，已取得的原始文件仍保留。"
        summary = {"report_sha256": hashlib.sha256((directory / "report.json").read_bytes()).hexdigest(),
                   "can_import": False, "backtest_ready": False, "valid_daily_rows": report["valid_daily_rows"]}
        if status not in {"done", "cancelled"}:
            async_tasks._append_error(row, {"code": "HISTORY_SOURCE_INCOMPLETE", "title_zh": "历史资料没有下载完整",
                "detail_zh": message, "retryable": True})
        async_tasks._set_task(db, task_id, status=status, stage=status, message=message, percent=100,
            ok_count=sum(not s["errors"] for s in report["symbols"]), failed_count=sum(bool(s["errors"]) for s in report["symbols"]),
            result_json=json.dumps(summary), finished_at=async_tasks._now())
    finally:
        db.close()


def read_download(db, task_id):
    task = _read_task(db, task_id)
    if task is None:
        return None
    report = None
    expected = (task.get("result") or {}).get("report_sha256")
    if expected:
        try:
            data = (ARTIFACT_ROOT / task_id / "report.json").read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError()
            report = json.loads(data)
            if report.get("report_version") != REPORT_VERSION:
                raise HistoryDownloadError("这次记录使用的检查规则已更新，请重新下载核对。")
            for receipt in report["raw_files"]:
                if not re.fullmatch(r"(?:calendar|(?:basic|daily)_\d{6})\.json", receipt["file"]):
                    raise ValueError()
                raw = (ARTIFACT_ROOT / task_id / receipt["file"]).read_bytes()
                if len(raw) != receipt["bytes"] or hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
                    raise ValueError()
        except HistoryDownloadError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HistoryDownloadError("下载记录的原始文件缺失或已改变，请重新下载。") from error
    return {"task": task, "report": report}


def recover_interrupted_downloads(db):
    """Startup-only receipt recovery; never retry a network request on a read."""
    rows = list(db.scalars(select(AsyncTaskRecord).where(AsyncTaskRecord.task_type == TASK_TYPE,
                         AsyncTaskRecord.status == "failed", AsyncTaskRecord.stage == "interrupted")))
    message = "应用上次关闭时下载尚未完成，已取得的原始文件仍保留，请重新下载。"
    for row in rows:
        previous = json.loads(row.result_json or "{}")
        if previous.get("report_sha256"):
            continue
        row.message = message
        if not re.fullmatch(r"[0-9a-f]{32}", row.id):
            continue
        directory = ARTIFACT_ROOT / row.id
        if not directory.is_dir():
            continue
        try:
            request = json.loads(row.payload_json)
            report = _build_report(directory, request, message)
            _save_json(directory / "report.json", report)
            row.result_json = json.dumps({**previous,
                "report_sha256": hashlib.sha256((directory / "report.json").read_bytes()).hexdigest(),
                "can_import": False, "backtest_ready": False, "valid_daily_rows": report["valid_daily_rows"]})
        except (OSError, ValueError, KeyError, TypeError):
            row.message = "应用上次关闭时下载尚未完成，原始文件没有通过检查，请重新下载。"
    if rows:
        db.commit()


def download_files(db, task_id):
    result = read_download(db, task_id)
    if result is None or result["report"] is None:
        raise HistoryDownloadError("这次下载还没有可保存的文件，请先查看下载进度。")
    output = io.BytesIO()
    directory = ARTIFACT_ROOT / task_id
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(directory / "report.json", "report.json")
        for receipt in result["report"]["raw_files"]:
            archive.write(directory / receipt["file"], receipt["file"])
    return output.getvalue()
