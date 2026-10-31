"""Install/inspect R2/R3 only in an explicitly supplied disposable candidate.

Pass a URL via a named environment variable, never via command-line credentials.
No application config or automatic production migration is loaded.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine
from app.db.governance_migration import MigrationError, migrate_candidate, migration_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url-env', required=True, help='Name of environment variable containing explicit candidate URL')
    parser.add_argument('--inspect', action='store_true')
    args = parser.parse_args()
    configured = os.environ.get(args.url_env)
    if not configured:
        parser.error('Explicit candidate URL environment variable is missing')
    engine = create_engine(configured, hide_parameters=True)
    try:
        result = migration_status(engine) if args.inspect else migrate_candidate(engine)
        print(json.dumps(result, default=lambda x: x.isoformat(timespec='microseconds') + 'Z' if isinstance(x, datetime) else str(x)))
        return 0
    except MigrationError as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2
    except Exception as error:
        print(json.dumps({'error': 'CANDIDATE_MIGRATION_FAILED', 'type': type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
