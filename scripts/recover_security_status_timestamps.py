"""Preview or apply evidence-preserving recovery in an explicitly named clone.

Set STATUS_RECOVERY_DATABASE_URL explicitly; no application DB configuration
is read. Only codex_combination_<suffix> / gpfx_combination_review databases
are permitted. Preserve an independently retained original request JSON and
its original content_sha256 before running preview. Apply requires that exact
preview hash, --confirm, and a personal --operator. This script never alters
source files, old receipts, old constraint chunks, status IDs or batch IDs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.schemas.security_status import SecurityStatusFileRequest
from app.services.security_status.timestamp_recovery import (
    TimestampRecoveryError, preview_status_timestamp_recovery, apply_status_timestamp_recovery,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--original-request", required=True, type=Path)
    parser.add_argument("--content-sha256", required=True, help="Independent original import receipt digest; not a newly generated digest")
    parser.add_argument("--apply", action="store_true", help="Without this flag the operation is read-only preview")
    parser.add_argument("--preview-sha256")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--operator")
    args = parser.parse_args(argv)
    engine = None
    try:
        raw_url = os.environ.get("STATUS_RECOVERY_DATABASE_URL")
        if not raw_url:
            raise TimestampRecoveryError("An explicit STATUS_RECOVERY_DATABASE_URL is required; there is no configuration fallback")
        url = make_url(raw_url)
        allowed = args.expected_database == "gpfx_combination_review" or re.fullmatch(r"codex_combination_[a-z0-9][a-z0-9_]{0,44}", args.expected_database)
        if not allowed or url.database != args.expected_database or url.get_backend_name() != "mysql":
            raise TimestampRecoveryError("Only an explicitly matching isolated MySQL database is permitted")
        if args.apply and (not args.confirm or not args.preview_sha256 or not args.operator or args.operator.strip() in {"system", "cron_worker"}):
            raise TimestampRecoveryError("Apply requires explicit personal confirmation, operator and the saved preview SHA256")
        if not re.fullmatch(r"[0-9a-f]{64}", args.content_sha256):
            raise TimestampRecoveryError("An independently retained 64-character content SHA256 is required")
        if args.original_request.stat().st_size > 40_000_000:
            raise TimestampRecoveryError("Original request JSON exceeds the permitted input size")
        payload = SecurityStatusFileRequest.model_validate_json(args.original_request.read_text(encoding="utf-8-sig"))
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as connection:
            if not args.apply:
                connection.exec_driver_sql("START TRANSACTION READ ONLY")
            with Session(bind=connection, expire_on_commit=False) as db:
                actual_database = db.scalar(text("SELECT DATABASE()"))
                if actual_database != args.expected_database:
                    raise TimestampRecoveryError("Connected database identity differs from the approved isolated target")
                if args.apply:
                    result = apply_status_timestamp_recovery(db, payload,
                        expected_content_sha256=args.content_sha256, preview_sha256=args.preview_sha256,
                        operator_id=args.operator, confirmed=args.confirm)
                    db.commit()
                    # Session may join a Connection transaction; commit the outer
                    # connection only after every validation and audit succeeded.
                    connection.commit()
                else:
                    result = preview_status_timestamp_recovery(db, payload, expected_content_sha256=args.content_sha256)
                    db.rollback()
                    connection.rollback()
        print(json.dumps({"database": args.expected_database, "mode": "apply" if args.apply else "preview", **result}, ensure_ascii=False, indent=2))
        return 0 if args.apply or result["can_apply"] else 2
    except TimestampRecoveryError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        # Do not echo driver URLs, credentials or arbitrary original file data.
        print(json.dumps({"error": f"Recovery stopped without approval: {type(exc).__name__}. Check the original input and database availability."}))
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
