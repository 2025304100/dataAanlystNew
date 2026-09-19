# -*- coding: utf-8 -*-
"""TD3 开工登记：tasks.json TD3 -> in_progress + started_at。幂等。"""
import json
from datetime import datetime

PATH = r".workbuddy/mining/tasks.json"

with open(PATH, encoding="utf-8") as f:
    data = json.load(f)

card = None
for t in data.get("tasks", []):
    if t.get("id") == "TD3":
        card = t
        break
assert card is not None, "TD3 card not found"

if card.get("status") == "in_progress":
    print("TD3 already in_progress, skip (idempotent)")
else:
    assert card.get("status") in (None, "pending"), f"unexpected status: {card.get('status')}"
    card["status"] = "in_progress"
    card["started_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    # 回读校验
    with open(PATH, encoding="utf-8") as f:
        chk = json.load(f)
    c2 = [t for t in chk["tasks"] if t["id"] == "TD3"][0]
    assert c2["status"] == "in_progress" and c2["started_at"] == card["started_at"]
    print("TD3 -> in_progress @", card["started_at"])
