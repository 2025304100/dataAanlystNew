# -*- coding: utf-8 -*-
"""TD6 修订文件改名 0059 -> 0060（59=T36 预留；60 是 tasks.json 预留+实占之外的真空位）。幂等。"""
import json
import shutil
from pathlib import Path

ROOT = Path(r"D:\ai_project\dataAanlystNew")
OLD = ROOT / "alembic/versions/2026_09_18_0059_wps_0023_056_td6_snapshot_binding_columns.py"
NEW = ROOT / "alembic/versions/2026_09_18_0060_wps_0023_056_td6_snapshot_binding_columns.py"
MIG_OLD = "alembic/versions/2026_09_18_0059_wps_0023_056_td6_snapshot_binding_columns.py"
MIG_NEW = "alembic/versions/2026_09_18_0060_wps_0023_056_td6_snapshot_binding_columns.py"

if OLD.exists() and not NEW.exists():
    shutil.move(str(OLD), str(NEW))

P = ROOT / ".workbuddy/mining/tasks.json"
with open(P, encoding="utf-8") as f:
    data = json.load(f)
card = next(t for t in data["tasks"] if t["id"] == "TD6")
if MIG_OLD in card["writes"]:
    card["writes"][card["writes"].index(MIG_OLD)] = MIG_NEW
with open(P, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("renamed:", NEW.exists(), "| card writes:", MIG_NEW in card["writes"])
