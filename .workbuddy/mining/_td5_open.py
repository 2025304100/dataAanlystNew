# -*- coding: utf-8 -*-
"""TD5 开工登记：tasks.json 卡置 in_progress + PROGRESS.json 建条目（双登记）。幂等。"""
import json
from datetime import datetime

TPATH = r".workbuddy/mining/tasks.json"
PPATH = r".workbuddy/mining/PROGRESS.json"

now = datetime.now().astimezone().isoformat(timespec="seconds")

# 1) tasks.json 卡
with open(TPATH, encoding="utf-8") as f:
    tdata = json.load(f)
card = [t for t in tdata["tasks"] if t["id"] == "TD5"][0]
if card.get("status") == "in_progress":
    print("tasks.json: TD5 already in_progress (idempotent)")
else:
    assert card.get("status") in (None, "pending"), f"unexpected status {card.get('status')}"
    card["status"] = "in_progress"
    card["started_at"] = now
    with open(TPATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(tdata, f, ensure_ascii=False, indent=2)
        f.write("\n")

# 2) PROGRESS.json 条目
with open(PPATH, encoding="utf-8") as f:
    pdata = json.load(f)
tasks = pdata.setdefault("tasks", {})
if "TD5" in tasks:
    print("PROGRESS: TD5 entry already exists (idempotent)")
else:
    tasks["TD5"] = {
        "status": "in_progress",
        "agent": "agent-senior-dev",
        "started_at": now,
    }
    with open(PPATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(pdata, f, ensure_ascii=False, indent=2)
        f.write("\n")

# 回读校验
with open(TPATH, encoding="utf-8") as f:
    assert [t for t in json.load(f)["tasks"] if t["id"] == "TD5"][0]["status"] == "in_progress"
with open(PPATH, encoding="utf-8") as f:
    assert json.load(f)["tasks"]["TD5"]["status"] == "in_progress"
print("TD5 -> in_progress @", now)
