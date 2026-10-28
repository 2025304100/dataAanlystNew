"""Immutable source snapshots captured before legacy title-level deduplication."""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.market_signal import MarketSignalEvidence, utcnow


def normalized(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def capture_evidence(db, raw, *, origin="feed"):
    title = str(raw.get("title") or "").strip()
    text = str(raw.get("full_text") or raw.get("summary") or "").strip()
    if not title:
        return None
    source = str(raw.get("source") or origin)[:64]
    published_at = utc(raw.get("published_at"))
    quality = raw.get("time_quality") or ("supplier" if published_at else "unknown")
    if origin == "manual" and published_at:
        quality = "manual"
    kind = raw.get("kind") or ("calendar" if source in ("baidu", "baidu-report") else "news")
    source_url = raw.get("source_url") or None
    fingerprint = digest(json.dumps([normalized(title), normalized(text), source, source_url,
        published_at.isoformat() if published_at else None, quality], ensure_ascii=False))
    existing = db.scalar(select(MarketSignalEvidence).where(MarketSignalEvidence.fingerprint == fingerprint))
    if existing:
        return existing
    row = MarketSignalEvidence(fingerprint=fingerprint, title=title, text=text,
        source=source, source_url=source_url, published_at=published_at,
        time_quality=quality, kind=kind, metadata_json=json.dumps({"origin": origin,
            "sector_ids": raw.get("sector_ids", []), "symbol_codes": raw.get("symbol_codes", []),
            "parent_evidence_id": raw.get("parent_evidence_id"), "legacy_market_event_id": raw.get("legacy_market_event_id"),
            "content_basis": "full_text" if raw.get("full_text") else "title_summary"}, ensure_ascii=False),
        first_seen_at=utcnow())
    db.add(row)
    db.flush()
    return row


def evidence_read(row):
    return {"id": row.id, "title": row.title, "text": row.text, "source": row.source,
        "source_url": row.source_url, "published_at": row.published_at,
        "time_quality": row.time_quality, "kind": row.kind,
        "metadata": json.loads(row.metadata_json), "first_seen_at": row.first_seen_at}


def list_evidence(db, *, offset=0, limit=50):
    query = select(MarketSignalEvidence).order_by(MarketSignalEvidence.id.desc())
    return {"total": db.scalar(select(func.count()).select_from(MarketSignalEvidence)) or 0,
            "items": [evidence_read(row) for row in db.scalars(query.offset(offset).limit(limit))]}


def add_evidence(db, payload):
    raw = payload.model_dump()
    if raw.get("source_url") and urlsplit(raw["source_url"]).scheme not in ("http", "https"):
        raise HTTPException(422, "来源链接必须使用 HTTP 或 HTTPS")
    raw["full_text"] = raw.pop("text")
    raw["source"] = "manual"
    from app.services.market_event_collection_lock import market_event_write_lock
    with market_event_write_lock(db):
        db.rollback()
        row = capture_evidence(db, raw, origin="manual")
        db.commit()
        db.refresh(row)
        return evidence_read(row)
