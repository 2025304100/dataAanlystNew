# -*- coding: utf-8 -*-
import json, pathlib

with open(r"D:\ai_project\dataAanlystNew\.workbuddy\mining\tasks.json", encoding="utf-8") as f:
    data = json.load(f)
t26 = next(t for t in data["tasks"] if t["id"] == "T26")
out = ["T26 keys=" + ",".join(t26.keys())]
out.append("T26 status=" + str(t26.get("status") or t26.get("state")))
out.append("T26 alembic writes:")
out += ["  " + w for w in t26.get("writes", []) if "alembic" in w]
out.append("versions dir tail:")
out += ["  " + p.name for p in sorted(pathlib.Path(r"D:\ai_project\dataAanlystNew\alembic\versions").glob("*.py"))[-6:]]
with open(r"D:\ai_project\dataAanlystNew\.workbuddy\mining\_td6_t26.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
print("OK")
