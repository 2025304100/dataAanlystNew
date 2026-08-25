"""Read-only inventory of portfolio replay source databases.

The scanner deliberately excludes test_output/tmp and never calls application
initialization or migration code. It supports SQLite URLs and the configured
MySQL URL so an empty local SQLite file cannot mask the business database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {"portfolios", "strategy_execution_snapshots", "portfolio_members", "daily_bars", "scores"}
EXCLUDED = {"test_output", "tmp", ".git", ".venv", "node_modules"}


def _excluded_path(path: Path) -> bool:
    return any(part in EXCLUDED or part.startswith(".tmp") for part in path.parts)


def _sqlite_urls(root: Path) -> list[str]:
    urls: list[str] = []
    for path in root.rglob("*.db"):
        if not path.is_file():
            continue
        if _excluded_path(path):
            continue
        urls.append("sqlite:///" + path.resolve().as_posix())
    for suffix in ("*.sqlite", "*.sqlite3"):
        for path in root.rglob(suffix):
            if not path.is_file():
                continue
            if _excluded_path(path):
                continue
            urls.append("sqlite:///" + path.resolve().as_posix())
    return sorted(set(urls))

def _configured_urls() -> list[str]:
    urls: list[str] = []
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        urls.append(explicit)
    cfg = ROOT / "config" / "db_config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            mysql = data.get("mysql") or {}
            if data.get("use_mysql") and mysql.get("database"):
                user = quote_plus(str(mysql.get("user", "")))
                password = quote_plus(str(mysql.get("password", "")))
                host = mysql.get("host", "127.0.0.1")
                port = mysql.get("port", 3306)
                urls.append(f"mysql+pymysql://{user}:{password}@{host}:{port}/{mysql['database']}")
        except (OSError, ValueError, TypeError):
            pass
    return urls


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _consecutive_windows(days: list[date], minimum: int = 10) -> list[list[str]]:
    if not days:
        return []
    days = sorted(set(days))
    windows: list[list[str]] = []
    current = [days[0]]
    for item in days[1:]:
        if item - current[-1] <= timedelta(days=4):
            current.append(item)
        else:
            if len(current) >= minimum:
                windows.append([x.isoformat() for x in current])
            current = [item]
    if len(current) >= minimum:
        windows.append([x.isoformat() for x in current])
    return windows


def scan_url(url: str) -> dict[str, Any]:
    result: dict[str, Any] = {"database_url": url, "read_only": True, "error": None, "required_tables": sorted(REQUIRED), "portfolios": []}
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 3} if url.startswith(("mysql://", "mysql+")) else {})
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        result["tables_present"] = sorted(REQUIRED & tables)
        result["business_database"] = REQUIRED <= tables
        if not result["business_database"]:
            return result
        with engine.connect() as db:
            global_bars = db.execute(text("SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM daily_bars")).one()
            global_scores = db.execute(text("SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM scores")).one()
            result["daily_bars"] = {"earliest": _iso(global_bars[0]), "latest": _iso(global_bars[1]), "trading_days": int(global_bars[2] or 0)}
            result["scores"] = {"earliest": _iso(global_scores[0]), "latest": _iso(global_scores[1]), "trading_days": int(global_scores[2] or 0)}
            portfolios = db.execute(text("SELECT id, name, is_test FROM portfolios ORDER BY id")).mappings().all()
            bar_dates = [r[0] for r in db.execute(text("SELECT DISTINCT trade_date FROM daily_bars ORDER BY trade_date")).all()]
            for p in portfolios:
                pid = int(p["id"])
                members = db.execute(text("SELECT symbol_id, effective_from, effective_to, status FROM portfolio_members WHERE portfolio_id=:pid ORDER BY symbol_id"), {"pid": pid}).mappings().all()
                snapshots = db.execute(text("SELECT id, effective_from, created_at FROM strategy_execution_snapshots WHERE portfolio_id=:pid ORDER BY effective_from"), {"pid": pid}).mappings().all()
                member_ids = sorted({int(row["symbol_id"]) for row in members})
                member_dates = [_iso(row["effective_from"]) for row in members]
                candidate_days: list[date] = []
                missing: dict[str, list[str]] = {}
                for td in bar_dates:
                    if not member_ids:
                        missing.setdefault(_iso(td) or "", []).append("portfolio_members")
                        continue
                    active = db.execute(text("SELECT COUNT(DISTINCT symbol_id) FROM portfolio_members WHERE portfolio_id=:pid AND symbol_id IN :ids AND effective_from < :next_day AND (effective_to IS NULL OR effective_to >= :day)" ).bindparams(), {"pid": pid, "ids": tuple(member_ids), "day": td, "next_day": td + timedelta(days=1)}).scalar() if False else len(member_ids)
                    # IN expansion is dialect-specific; use a bounded per-symbol check for portability.
                    active = sum(1 for row in members if row["symbol_id"] in member_ids and row["effective_from"].date() <= td and (row["effective_to"] is None or row["effective_to"].date() >= td))
                    if active != len(member_ids):
                        missing.setdefault(_iso(td) or "", []).append("member_snapshot")
                        continue
                    bar_count = db.execute(text("SELECT COUNT(DISTINCT symbol_id) FROM daily_bars WHERE trade_date=:td AND symbol_id IN (SELECT symbol_id FROM portfolio_members WHERE portfolio_id=:pid)"), {"td": td, "pid": pid}).scalar() or 0
                    score_count = db.execute(text("SELECT COUNT(DISTINCT symbol_id) FROM scores WHERE trade_date=:td AND symbol_id IN (SELECT symbol_id FROM portfolio_members WHERE portfolio_id=:pid)"), {"td": td, "pid": pid}).scalar() or 0
                    if int(bar_count) < len(member_ids):
                        missing.setdefault(_iso(td) or "", []).append(f"daily_bars:{len(member_ids)-int(bar_count)}")
                    if int(score_count) < len(member_ids):
                        missing.setdefault(_iso(td) or "", []).append(f"scores:{len(member_ids)-int(score_count)}")
                    if int(bar_count) == len(member_ids) and int(score_count) == len(member_ids) and snapshots:
                        candidate_days.append(td)
                result["portfolios"].append({"portfolio_id": pid, "name": p["name"], "is_test": bool(p["is_test"]), "strategy_snapshot_ids": [x["id"] for x in snapshots], "member_count": len(member_ids), "member_snapshot_date_range": [min(member_dates) if member_dates else None, max(member_dates) if member_dates else None], "available_trade_days": len(candidate_days), "continuous_10_day_windows": _consecutive_windows(candidate_days), "missing_sample": dict(list(missing.items())[:50])})
    except Exception as exc:  # scanner must report unreachable candidates without stopping all scans
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db-url", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    urls = list(dict.fromkeys([*_sqlite_urls(args.root), *_configured_urls(), *args.db_url]))
    payload = {"scan_type": "business_database_inventory", "read_only": True, "excluded_paths": sorted(EXCLUDED), "scanned_at": datetime.utcnow().isoformat() + "Z", "databases": [scan_url(url) for url in urls]}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
