"""Strict local import of dated status records with an atomic source receipt.

This validates file consistency and retains the person's source declaration. It
cannot certify that an arbitrary provider supplied genuine historical evidence.
No current Symbol fields, missing bars or prior-day rows fill missing statuses.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from decimal import Decimal, InvalidOperation, localcontext
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, event, func, select
from sqlalchemy.orm import Session

from app.models.security_status import SecurityStatusDaily
from app.models.symbol import Symbol
from app.models.market_data import MarketCalendar
from app.schemas.security_status import SecurityStatusFileRequest
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services.security_status.pit_service import SecurityStatusPitService
from app.services.security_status.provenance import is_unverified_status_source, is_verified_status_row

COLUMNS = (
    "symbol", "trade_date", "is_st", "is_suspended", "is_delisting_period",
    "is_listed", "listing_date", "delisting_date",
)
FLAGS = ("is_st", "is_suspended", "is_delisting_period", "is_listed")
VALUE_COLUMNS = (*FLAGS, "listing_date", "delisting_date")
EXECUTION_COLUMNS = ("price_limit_mode", "upper_limit_price", "lower_limit_price", "tick_size")
AUDIT_CHUNK_BYTES = 32_000
MAX_ROWS = 50_000
MAX_BYTES = 20_000_000
SOURCE_WARNING = "文件校验只检查格式和记录是否一致，不能证明提供方资料真实。请先核对原始资料及使用权限。"


class StatusImportError(ValueError):
    pass


def template() -> dict:
    return {
        "csv_text": ",".join((*COLUMNS, *EXECUTION_COLUMNS)) + "\n",
        "required_columns": list(COLUMNS), "optional_columns": list(EXECUTION_COLUMNS),
        "max_bytes": MAX_BYTES, "max_rows": MAX_ROWS,
        "instructions": [
            "模板只有表头，不含任何真实股票状态。请填写原始历史资料，不能用今天的状态填过去。",
            "每行对应一只股票的一个交易日；symbol 必须与本机股票代码相同。",
            "is_st、is_suspended、is_delisting_period、is_listed 必须填写 0 或 1，不能留空。",
            "trade_date、listing_date 使用 YYYY-MM-DD。摘牌后 is_listed 填 0，且 delisting_date 必填。",
            "摘牌日只填在已摘牌的当日及之后记录中；较早日期不得提前填入未来摘牌信息。",
            "没有资料的日期请不要补行；没有行情不能据此填写停牌。",
            "请保留提供方名称、原始文件或资料地址、资料版本，以及提供方注明的更新时间。",
            "可选限价列全部留空时，只导入股票状态，不会据此估算涨跌停价格。",
            "有原始逐日限价时，price_limit_mode 填 LIMITED，并填写 upper_limit_price、lower_limit_price、tick_size。",
            "原资料明确当日不设涨跌幅限制时，price_limit_mode 填 NO_LIMIT，上下限留空，tick_size 仍必填。",
            "导入后仍需检查行情和其他回测资料。",
            SOURCE_WARNING,
        ],
    }


def _fingerprint(payload: SecurityStatusFileRequest) -> str:
    content = SecurityStatusFileRequest.model_validate(
        payload.model_dump(include=set(SecurityStatusFileRequest.model_fields)),
    ).model_dump(mode="json")
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _row_values(row) -> tuple:
    return tuple(getattr(row, column) if not isinstance(row, dict) else row[column] for column in VALUE_COLUMNS)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _status_fingerprint(row: SecurityStatusDaily) -> str:
    values = {"symbol_id": row.symbol_id, "trade_date": row.trade_date.isoformat(),
        "status_source": row.status_source, "batch_id": row.as_of_batch_id,
        "source_updated_at": row.source_updated_at.isoformat(timespec="microseconds") if row.source_updated_at else None}
    for key in VALUE_COLUMNS:
        value = getattr(row, key)
        values[key] = value.isoformat() if isinstance(value, date) else value
    return hashlib.sha256(_canonical(values).encode("utf-8")).hexdigest()


def _execution_values(values: dict) -> dict | None:
    raw = {key: str(values.get(key) or "").strip() for key in EXECUTION_COLUMNS}
    if not any(raw.values()):
        return None
    mode = raw["price_limit_mode"]
    if mode not in {"LIMITED", "NO_LIMIT"}:
        raise ValueError("限价方式必须明确填写 LIMITED 或 NO_LIMIT，不能按当前股票状态估算。")
    required = ("tick_size", "upper_limit_price", "lower_limit_price") if mode == "LIMITED" else ("tick_size",)
    parsed = {}
    for key in required:
        try:
            number = Decimal(raw[key])
            if not number.is_finite() or number <= 0 or not 0 < float(number) < math.inf:
                raise ValueError()
        except (ValueError, InvalidOperation, OverflowError):
            raise ValueError("涨跌停价格和最小报价单位必须是明确的有限正数，不能留空。") from None
        parsed[key] = number
    if mode == "NO_LIMIT":
        if raw["upper_limit_price"] or raw["lower_limit_price"]:
            raise ValueError("明确不设涨跌幅限制时，上下限价格必须留空，不能同时填写限价。")
    else:
        if parsed["lower_limit_price"] >= parsed["upper_limit_price"]:
            raise ValueError("跌停价格必须低于涨停价格。")
        with localcontext() as context:
            context.prec = max(28, *(len(number.as_tuple().digits) + abs(number.as_tuple().exponent) + 5 for number in parsed.values()))
            if any(parsed[key] % parsed["tick_size"] != 0 for key in ("upper_limit_price", "lower_limit_price")):
                raise ValueError("涨跌停价格必须符合填写的最小报价单位，请核对原始资料。")
    return {"price_limit_mode": mode, "tick_size": float(parsed["tick_size"]),
        "upper_limit_price": float(parsed["upper_limit_price"]) if mode == "LIMITED" else None,
        "lower_limit_price": float(parsed["lower_limit_price"]) if mode == "LIMITED" else None}


def _same_source(row: SecurityStatusDaily, receipt: dict, payload: SecurityStatusFileRequest | None = None) -> bool:
    try:
        expected_time = datetime.fromisoformat(receipt["source_updated_at"]).astimezone(timezone.utc).replace(tzinfo=None)
        valid = (row.status_source == f"historical_csv:{receipt['source_name']}"
            and row.source_updated_at == expected_time
            and row.as_of_batch_id == receipt["batch_id"]
            and receipt["batch_id"] == f"status_csv_{receipt['content_sha256'][:24]}"
            and not is_unverified_status_source(receipt["source_name"]))
        return bool(valid and (payload is None or (
            all(receipt[key] == getattr(payload, key) for key in ("source_name", "source_reference", "source_version"))
            and expected_time == payload.source_updated_at.astimezone(timezone.utc).replace(tzinfo=None))))
    except (ValueError, KeyError, TypeError):
        return False


def _prepare(db: Session, payload: SecurityStatusFileRequest) -> tuple[dict, list[dict], dict]:
    digest = _fingerprint(payload)
    result = {
        "can_import": False, "content_sha256": digest, "batch_id": f"status_csv_{digest[:24]}",
        "total_rows": 0, "insert_count": 0, "replace_unknown_count": 0, "unchanged_count": 0,
        "start_date": None, "end_date": None, "symbols": 0, "errors": [], "sample": [],
        "execution_constraint_count": 0, "attach_constraints_count": 0,
        "warnings": [SOURCE_WARNING, "缺少的日期保持未知，不会自动补成正常交易，也不会自动下载历史资料。"],
    }
    errors = result["errors"]

    def error(line: int, message: str):
        if len(errors) < 100:
            errors.append({"line": line, "message": message})

    if len(payload.csv_text.encode("utf-8")) > MAX_BYTES:
        error(0, "文件超过 20 MB，请按日期拆成多个文件。")
        return result, [], {}
    if is_unverified_status_source(payload.source_name):
        error(0, "资料来源不能是测试、示例、未知或当前快照。请填写原始历史资料的提供方。")
    if payload.source_version.lower() in {"latest", "current", "unknown", "最新", "当前", "未知"}:
        error(0, "资料版本需要能回查同一份文件，不能只写最新、当前或未知。")
    if payload.source_updated_at > datetime.now(timezone.utc):
        error(0, "资料更新时间不能晚于当前时间。请填写提供方注明的时间。")
    reader = csv.DictReader(io.StringIO(payload.csv_text.lstrip("\ufeff")), strict=True)
    try:
        headers = reader.fieldnames
        if (not headers or len(headers) != len(set(headers)) or not set(COLUMNS).issubset(headers)
                or not set(headers).issubset((*COLUMNS, *EXECUTION_COLUMNS))):
            error(1, "表头不正确。请下载模板，保留全部列且不要重复列名。")
            return result, [], {}
        raw_rows = []
        for raw in reader:
            if len(raw_rows) >= MAX_ROWS:
                error(reader.line_num, "单次最多导入 50000 行，请按日期拆成多个文件。")
                break
            raw_rows.append((reader.line_num, raw))
    except csv.Error:
        error(reader.line_num, "CSV 格式无法读取，请检查引号和分隔符。")
        return result, [], {}
    result["total_rows"] = len(raw_rows)
    if not raw_rows:
        error(2, "文件只有表头，还没有历史状态记录。")
        return result, [], {}
    names = {str(raw.get("symbol") or "").strip() for _, raw in raw_rows}
    # Chunk IN parameters for SQLite as well as MySQL; do not reach today's master status.
    symbols = {}
    all_names = sorted(names)
    for start in range(0, len(all_names), 500):
        for symbol in db.scalars(select(Symbol).where(Symbol.symbol.in_(all_names[start:start + 500]))):
            symbols[symbol.symbol] = symbol.id
    records: list[dict] = []
    keys = set()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for line, raw in raw_rows:
        if None in raw or any(value is None for value in raw.values()):
            error(line, "列数不正确，请检查分隔符并保留空白的摘牌日期列。")
            continue
        values = {key: value.strip() for key, value in raw.items()}
        name = values["symbol"]
        if name not in symbols:
            error(line, f"本机没有股票代码 {name or '（空白）'}，请先核对代码并添加股票。")
            continue
        try:
            trade_date = date.fromisoformat(values["trade_date"])
            listing_date = date.fromisoformat(values["listing_date"])
            delisting_date = date.fromisoformat(values["delisting_date"]) if values["delisting_date"] else None
            if (values["trade_date"] != trade_date.isoformat() or values["listing_date"] != listing_date.isoformat()
                    or (delisting_date is not None and values["delisting_date"] != delisting_date.isoformat())):
                raise ValueError("non-canonical date")
        except ValueError:
            error(line, "交易日和上市日必须填写有效的 YYYY-MM-DD 日期；摘牌日未知时留空。")
            continue
        if trade_date > today or listing_date > trade_date:
            error(line, "交易日不能晚于今天，上市日不能晚于该行交易日。未上市日期不能填成正常上市。")
            continue
        if any(values[flag] not in {"0", "1"} for flag in FLAGS):
            error(line, "ST、停牌、退市整理和上市状态必须明确填写 0 或 1，不能留空或推断。")
            continue
        flags = {flag: values[flag] == "1" for flag in FLAGS}
        try:
            execution = _execution_values(values)
        except ValueError as failure:
            error(line, str(failure))
            continue
        if delisting_date is not None and delisting_date < listing_date:
            error(line, "摘牌日不能早于上市日。")
            continue
        if delisting_date is not None and delisting_date > trade_date:
            error(line, "该行是较早日期，不能提前填入未来摘牌信息。摘牌前的记录请保留空白摘牌日。")
            continue
        if not flags["is_listed"] and (delisting_date is None or delisting_date > trade_date):
            error(line, "已摘牌记录必须提供不晚于该交易日的摘牌日期。")
            continue
        if flags["is_listed"] and delisting_date is not None and delisting_date <= trade_date:
            error(line, "摘牌日已经过去，该行不能仍填写上市。")
            continue
        if not flags["is_listed"] and flags["is_delisting_period"]:
            error(line, "已经摘牌的记录不能同时填写仍在退市整理期。")
            continue
        key = (symbols[name], trade_date)
        if key in keys:
            error(line, f"{name} 在 {trade_date} 有重复记录，请先在原文件核对后保留一行。")
            continue
        keys.add(key)
        records.append({
            "line": line, "symbol": name, "symbol_id": symbols[name], "trade_date": trade_date,
            **flags, "listing_date": listing_date, "delisting_date": delisting_date,
            "execution": execution, "attach_constraints": False,
        })
    if not records:
        return result, records, {}
    start_date = min(row["trade_date"] for row in records)
    end_date = max(row["trade_date"] for row in records)
    result.update(start_date=start_date, end_date=end_date, symbols=len({row["symbol_id"] for row in records}))
    existing = {}
    ids = sorted({row["symbol_id"] for row in records})
    for start in range(0, len(ids), 500):
        stored = db.scalars(select(SecurityStatusDaily).where(
            SecurityStatusDaily.symbol_id.in_(ids[start:start + 500]),
            SecurityStatusDaily.trade_date >= start_date, SecurityStatusDaily.trade_date <= end_date,
        ))
        for row in stored:
            key = (row.symbol_id, row.trade_date)
            if key in keys:
                existing[key] = row
    for record in records:
        old = existing.get((record["symbol_id"], record["trade_date"]))
        if old is None:
            result["insert_count"] += 1
        elif not is_verified_status_row(old, old.trade_date):
            result["replace_unknown_count"] += 1
        elif _row_values(old) == _row_values(record):
            result["unchanged_count"] += 1
            if record["execution"] is not None:
                previous_constraint = get_imported_execution_constraints(db, old.symbol_id, old.trade_date)
                if previous_constraint is not None:
                    if any(previous_constraint[key] != record["execution"][key] for key in EXECUTION_COLUMNS):
                        error(record["line"], "当日限价与已保存的原始资料不一致，本次不会覆盖，请先核对原资料。")
                else:
                    receipt = get_import_receipt(db, old.as_of_batch_id) if old.as_of_batch_id else None
                    if receipt and _same_source(old, receipt, payload):
                        record["attach_constraints"] = True
                        result["attach_constraints_count"] += 1
                    else:
                        error(record["line"], "已有历史状态的来源或版本无法与此文件对应，不能把新限价拼到旧记录。请核对同一来源的原始资料。")
        else:
            error(record["line"], f"{record['symbol']} 在 {record['trade_date']} 与已保存的历史资料不一致。本次不会覆盖，请先核对两份原始资料。")
        if len(result["sample"]) < 10:
            transient = SecurityStatusDaily(**{column: record[column] for column in ("symbol_id", "trade_date", *VALUE_COLUMNS)},
                status_source=f"historical_csv:{payload.source_name}")
            result["sample"].append({"symbol": record["symbol"], "trade_date": record["trade_date"],
                "status": SecurityStatusPitService._row_to_dto(transient, record["trade_date"]).status})
        if record["execution"] is not None:
            result["execution_constraint_count"] += 1
    result["can_import"] = not errors
    return result, records, existing


def preview_import(db: Session, payload: SecurityStatusFileRequest) -> dict:
    return _prepare(db, payload)[0]


def import_file(db: Session, payload: SecurityStatusFileRequest, *, preview_sha256: str) -> dict:
    preview, records, existing = _prepare(db, payload)
    if preview["content_sha256"] != preview_sha256:
        raise StatusImportError("文件或来源说明已经改变，请重新预览后再导入。")
    if not preview["can_import"]:
        raise StatusImportError("文件校验未通过，未写入任何记录。" + "；".join(error["message"] for error in preview["errors"][:3]))
    prior_receipt = get_import_receipt(db, preview["batch_id"])
    if (prior_receipt is not None and not preview["insert_count"] and not preview["replace_unknown_count"]
            and not preview["attach_constraints_count"]):
        return {**prior_receipt, "inserted": 0, "replaced_unknown": 0, "unchanged": len(records),
            "message": "这些记录已经保存，本次没有重复写入。"}
    source = f"historical_csv:{payload.source_name}"
    previous = []
    replaced_count = 0
    constraint_rows = []
    for record in records:
        old = existing.get((record["symbol_id"], record["trade_date"]))
        if old is not None and is_verified_status_row(old, old.trade_date) and not record["attach_constraints"]:
            continue
        if old is None:
            old = SecurityStatusDaily(symbol_id=record["symbol_id"], trade_date=record["trade_date"])
            db.add(old)
        else:
            if not is_verified_status_row(old, old.trade_date):
                replaced_count += 1
            if len(previous) < 50:
                previous.append({"symbol_id": old.symbol_id, "trade_date": old.trade_date.isoformat(),
                    "status_source": old.status_source, "batch_id": old.as_of_batch_id})
        for column in VALUE_COLUMNS:
            setattr(old, column, record[column])
        old.status_source = source
        old.source_updated_at = payload.source_updated_at.astimezone(timezone.utc).replace(tzinfo=None)
        old.as_of_batch_id = preview["batch_id"]
        if record["execution"] is not None:
            constraint_rows.append({"symbol_id": old.symbol_id, "trade_date": old.trade_date.isoformat(),
                **record["execution"], "status_fingerprint": _status_fingerprint(old)})
    hashes = _write_constraint_chunks(db, preview, constraint_rows)
    receipt = {
        "kind": "security_status_import", "batch_id": preview["batch_id"],
        "content_sha256": preview["content_sha256"], "filename": payload.filename,
        "source_name": payload.source_name, "source_reference": payload.source_reference,
        "source_version": payload.source_version, "source_updated_at": payload.source_updated_at.isoformat(),
        "inserted": preview["insert_count"], "replaced_unknown": preview["replace_unknown_count"],
        "unchanged": preview["unchanged_count"], "total_rows": preview["total_rows"],
        "start_date": preview["start_date"].isoformat(), "end_date": preview["end_date"].isoformat(),
        "source_authenticity": "declared_by_user_not_independently_verified",
        "execution_constraint_rows": len(constraint_rows), "execution_constraint_hashes": hashes,
        "attached_constraints": preview["attach_constraints_count"],
    }
    audit = write_audit_event(db, "DATA_BLOCK_RESOLUTION", operator_id="user",
        business_key=preview["batch_id"], before={"replaced_unknown": replaced_count, "sample": previous}, after=receipt,
        attributes={"kind": "security_status_import", "confirmed_original_source": True},
        note="本人核对原始历史资料后导入；只保存文件中明确列出的日期，不补写空缺。")
    db.info.pop("security_status_constraints_cache", None)
    return {**receipt, "audit_id": audit.event_id, "message": "历史状态已保存。请返回回测页重新检查，缺少的日期仍会提示。"}


def get_import_receipt(db: Session, batch_id: str) -> dict | None:
    audit = db.scalar(select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.action == "DATA_BLOCK_RESOLUTION",
        DataGovernanceAuditEvent.business_key == batch_id,
    ).order_by(DataGovernanceAuditEvent.id.desc()))
    if audit is None:
        return None
    result = json.loads(audit.after_json or "{}")
    return {**result, "audit_id": audit.id} if result.get("kind") == "security_status_import" else None


def _write_constraint_chunks(db: Session, preview: dict, rows: list[dict]) -> list[str]:
    chunks: list[list[dict]] = []
    current = []
    base = {"kind": "security_status_execution_constraints", "batch_id": preview["batch_id"],
        "content_sha256": preview["content_sha256"]}
    for row in rows:
        candidate = [*current, row]
        if current and len(_canonical({**base, "rows": candidate}).encode("utf-8")) > AUDIT_CHUNK_BYTES:
            chunks.append(current)
            current = [row]
        else:
            current = candidate
    if current:
        chunks.append(current)
    hashes = []
    for index, chunk in enumerate(chunks):
        content = {**base, "rows": chunk}
        hashes.append(hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest())
        write_audit_event(db, "DATA_BLOCK_RESOLUTION", operator_id="user",
            business_key=f"{preview['batch_id']}:execution:{index}", after=content,
            attributes={"kind": "security_status_execution_constraints", "chunk_index": index},
            note="与原始历史状态文件一同核对并保存的当日限价；没有提供的日期不估算。")
    return hashes


def _constraint_cache(db: Session, supplied: dict | None) -> dict:
    if not db.info.get("security_status_constraints_listeners"):
        def invalidate(session, *_):
            session.info.pop("security_status_constraints_cache", None)
            session.info["security_status_constraints_epoch"] = session.info.get("security_status_constraints_epoch", 0) + 1
        for name in ("after_flush", "after_commit", "after_rollback"):
            event.listen(db, name, invalidate)
        db.info["security_status_constraints_listeners"] = True
    return supplied if supplied is not None else db.info.setdefault("security_status_constraints_cache", {})


def _constraint_batch(db: Session, batch_id: str, cache: dict) -> tuple[dict, dict] | None:
    key = ("historical_execution", db.get_transaction(), db.info.get("security_status_constraints_epoch", 0), batch_id)
    if key in cache:
        return cache[key]
    cache[key] = None
    try:
        receipt = get_import_receipt(db, batch_id)
        hashes = receipt.get("execution_constraint_hashes") if receipt else None
        if not isinstance(hashes, list) or not hashes or len(hashes) > MAX_ROWS:
            return None
        if any(not isinstance(value, str) or len(value) != 64 for value in hashes):
            return None
        events = db.scalars(select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.action == "DATA_BLOCK_RESOLUTION",
            DataGovernanceAuditEvent.business_key.like(f"{batch_id}:execution:%"),
        ).order_by(DataGovernanceAuditEvent.id)).all()
        by_key = {audit.business_key: audit for audit in events}
        values = {}
        for index, expected_hash in enumerate(hashes):
            audit = by_key.get(f"{batch_id}:execution:{index}")
            if audit is None or len((audit.after_json or "").encode("utf-8")) > AUDIT_CHUNK_BYTES:
                return None
            content = json.loads(audit.after_json or "{}")
            if (hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest() != expected_hash
                    or content.get("batch_id") != batch_id
                    or content.get("content_sha256") != receipt["content_sha256"]
                    or content.get("kind") != "security_status_execution_constraints"):
                return None
            for value in content["rows"]:
                pair = (int(value["symbol_id"]), date.fromisoformat(value["trade_date"]))
                if pair in values:
                    return None
                values[pair] = value
        if len(values) != receipt.get("execution_constraint_rows"):
            return None
        cache[key] = (receipt, values)
        return cache[key]
    except (ValueError, KeyError, TypeError, OverflowError):
        return None


def _constraints_for_row(db: Session, row: SecurityStatusDaily, cache: dict) -> dict | None:
    if not row.as_of_batch_id or not is_verified_status_row(row, row.trade_date):
        return None
    loaded = _constraint_batch(db, row.as_of_batch_id, cache)
    if loaded is None:
        return None
    receipt, values = loaded
    value = values.get((row.symbol_id, row.trade_date))
    if value is None or not _same_source(row, receipt) or value.get("status_fingerprint") != _status_fingerprint(row):
        return None
    try:
        execution = _execution_values(value)
    except (ValueError, TypeError):
        return None
    if execution is None:
        return None
    return {**execution, **{key: receipt[key] for key in (
        "source_name", "source_reference", "source_version", "source_updated_at", "content_sha256", "batch_id")},
        "symbol_id": row.symbol_id, "trade_date": row.trade_date.isoformat(),
        "status_fingerprint": value["status_fingerprint"], "is_st": bool(row.is_st),
        "is_suspended": bool(row.is_suspended), "is_delisting_period": bool(row.is_delisting_period),
        "is_listed": bool(row.is_listed), "is_delisted": not bool(row.is_listed),
        "is_tradable": bool(row.is_listed) and not bool(row.is_suspended)}


def get_imported_execution_constraints(db: Session, symbol_id: int, trade_date: date,
                                      *, cache: dict | None = None) -> dict | None:
    """Return original declared daily limits, or None; there is no estimated fallback."""
    row = db.scalar(select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id == symbol_id,
        SecurityStatusDaily.trade_date == trade_date).execution_options(populate_existing=True))
    if row is None:
        return None
    return _constraints_for_row(db, row, _constraint_cache(db, cache))


def get_imported_execution_constraints_for_range(db: Session, symbol_ids: list[int], start_date: date,
                                                end_date: date, *, cache: dict | None = None) -> dict[tuple[int, date], dict]:
    """Read a fixed scope once; absent or inconsistent pairs are omitted, never filled."""
    if not symbol_ids or start_date > end_date:
        return {}
    rows = db.scalars(select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id.in_(set(symbol_ids)),
        SecurityStatusDaily.trade_date >= start_date, SecurityStatusDaily.trade_date <= end_date)
        .execution_options(populate_existing=True)).all()
    parsed = _constraint_cache(db, cache)
    result = {}
    for row in rows:
        value = _constraints_for_row(db, row, parsed)
        if value is not None:
            result[(row.symbol_id, row.trade_date)] = value
    return result


def get_coverage(db: Session, *, start_date: date | None = None, end_date: date | None = None,
                 symbol_ids: list[int] | None = None) -> dict:
    """Count declared complete records, never treat sparse rows as full coverage."""
    complete_date = SecurityStatusDaily.listing_date <= SecurityStatusDaily.trade_date
    groups = db.execute(select(
        SecurityStatusDaily.status_source, func.count(),
        func.sum(case((complete_date, 1), else_=0)),
        func.min(case((complete_date, SecurityStatusDaily.trade_date))),
        func.max(case((complete_date, SecurityStatusDaily.trade_date))),
    ).group_by(SecurityStatusDaily.status_source)).all()
    sources = []
    first_dates, latest_dates = [], []
    for source, rows, complete, first, latest in groups:
        verified = int(complete or 0) if not is_unverified_status_source(source) else 0
        sources.append({"source": source, "rows": rows, "verified_rows": verified})
        if verified:
            first_dates.append(first)
            latest_dates.append(latest)
    result = {
        "total_records": sum(item["rows"] for item in sources),
        "verified_records": sum(item["verified_rows"] for item in sources),
        "first_date": min(first_dates) if first_dates else None,
        "latest_date": max(latest_dates) if latest_dates else None,
        "calendar_available": None, "total_expected": None, "verified": None,
        "missing_or_unverified": None, "coverage_ratio": None, "missing_sample": [],
        "sources": sources, "warnings": [SOURCE_WARNING],
        "message": ("还没有可用于回测的历史股票状态资料。取得逐日原始资料后，可在这里导入。"
            if not sum(item["verified_rows"] for item in sources)
            else "这里只显示本机保存的历史资料。是否足以回测，请返回回测页重新检查所选范围。"),
    }
    if start_date is None and end_date is None and not symbol_ids:
        return result
    if start_date is None or end_date is None or not symbol_ids:
        raise StatusImportError("查看区间覆盖率时，请同时指定开始日期、结束日期和股票。")
    if start_date > end_date:
        raise StatusImportError("开始日期不能晚于结束日期。")
    ids = sorted(set(symbol_ids))
    if len(ids) > 1000 or (end_date - start_date).days > 7305:
        raise StatusImportError("单次最多查看 1000 只股票、20 年资料，请缩小范围。")
    symbols = dict(db.execute(select(Symbol.id, Symbol.symbol).where(Symbol.id.in_(ids))).all())
    if len(symbols) != len(ids):
        raise StatusImportError("部分股票不在本机列表，请先核对股票代码。")
    calendar = db.execute(select(MarketCalendar.trade_date, MarketCalendar.is_trading_day).where(
        MarketCalendar.trade_date >= start_date, MarketCalendar.trade_date <= end_date,
    ).order_by(MarketCalendar.trade_date)).all()
    if len(calendar) != (end_date - start_date).days + 1:
        result.update(calendar_available=False,
            message="这个区间的本机交易日历不完整，暂时不能计算覆盖率。不会把缺少日历的日期当作休市。")
        return result
    days = [day for day, is_trading in calendar if is_trading]
    expected = len(ids) * len(days)
    if expected > 1_000_000:
        raise StatusImportError("本次需要核对的股票和日期过多，请分批查看。")
    rows = db.scalars(select(SecurityStatusDaily).where(
        SecurityStatusDaily.symbol_id.in_(ids), SecurityStatusDaily.trade_date >= start_date,
        SecurityStatusDaily.trade_date <= end_date,
    )).all()
    stored = {(row.symbol_id, row.trade_date): row for row in rows}
    verified = 0
    missing = []
    for sid in ids:
        for day in days:
            row = stored.get((sid, day))
            if row is not None and is_verified_status_row(row, day):
                verified += 1
            elif len(missing) < 50:
                missing.append({"symbol_id": sid, "symbol": symbols[sid], "trade_date": day,
                    "reason": "未提供当天资料" if row is None else "已有记录缺少可靠历史来源或上市日期"})
    result.update(calendar_available=True, total_expected=expected, verified=verified,
        missing_or_unverified=expected - verified, coverage_ratio=verified / expected if expected else None,
        missing_sample=missing,
        message=("所选股票的逐日历史状态已齐全，回测仍需检查行情和其他资料。" if expected and verified == expected
            else "所选区间没有交易日。" if not expected
            else "所选股票仍有日期缺少历史状态。请核对原始资料后导入，再返回回测页重新检查。"))
    return result
