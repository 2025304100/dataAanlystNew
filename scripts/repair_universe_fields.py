"""Explicit isolated-DB preview/apply for existing universe history field gaps."""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--symbol-ids", required=True, help="Comma-separated universe_symbols IDs")
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--fields", default="turnover_rate")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-preview-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session
    from app.services.universe_field_repair import repair_universe_fields

    raw = os.environ.get("FIELD_REPAIR_DATABASE_URL")
    if not raw:
        parser.error("FIELD_REPAIR_DATABASE_URL must be explicitly set; configuration fallback is disabled")
    url = make_url(raw)
    if (url.get_backend_name() != "mysql" or url.database != args.expected_database
            or not args.expected_database.startswith("codex_combination_")):
        parser.error("Only explicitly named codex_combination_* isolated MySQL databases are allowed")
    if args.output.exists():
        parser.error("Refusing to overwrite an existing evidence file")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            if db.scalar(text("SELECT DATABASE()")) != args.expected_database:
                raise ValueError("database_identity_mismatch")
            result = repair_universe_fields(db, symbol_ids=[int(value) for value in args.symbol_ids.split(",")],
                start_date=args.start_date, end_date=args.end_date, fields=args.fields.split(","),
                apply=args.apply, expected_preview_sha256=args.expected_preview_sha256)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({key: result[key] for key in ("outcome", "applied", "filled_fields", "preview_sha256")}))
        return 0 if result["outcome"] == "success" else 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
