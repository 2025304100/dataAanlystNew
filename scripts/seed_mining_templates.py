"""B4 模板种子：25 系统经典模板入 `factor_mining_templates`（幂等）。

用法：
    .venv/Scripts/python.exe scripts/seed_mining_templates.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db.session import get_session_local  # noqa: E402
from app.services.factors.mining import template_service as TPL  # noqa: E402


def main() -> int:
    db = get_session_local()()
    try:
        res = TPL.seed_system_templates(db)
        print(f"seed: {res['created']} created, system total={res['total_system']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())