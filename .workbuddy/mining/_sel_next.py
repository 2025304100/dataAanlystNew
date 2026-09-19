# -*- coding: utf-8 -*-
"""Sel-next card: dump T27/T33/T35/T38 key fields from tasks.json."""
import json, os, sys, io

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TASKS = os.path.join(ROOT, ".workbuddy", "mining", "tasks.json")
OUT = os.path.join(ROOT, ".workbuddy", "mining", "_sel_next_out.txt")

with open(TASKS, "r", encoding="utf-8") as f:
    data = json.load(f)

# tasks.json structure probe: find list of cards
cards = None
if isinstance(data, dict):
    for k in ("tasks", "cards", "items"):
        if k in data and isinstance(data[k], list):
            cards = data[k]
            break
    if cards is None:
        # maybe nested
        cards = []
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
                cards = v
                break
elif isinstance(data, list):
    cards = data

want = {"T27", "T33", "T35", "T38"}
lines = []
lines.append("top-level keys: %s" % (list(data.keys()) if isinstance(data, dict) else "LIST"))

def fmt(c):
    buf = []
    for key in ("id", "title", "phase", "size", "status", "deps", "depends_on", "blocked_by"):
        if key in c:
            buf.append("%s: %s" % (key, json.dumps(c[key], ensure_ascii=False)))
    for key in ("writes", "reads", "dod", "pitfalls", "notes", "summary"):
        if key in c:
            buf.append("%s: %s" % (key, json.dumps(c[key], ensure_ascii=False)))
    return "\n".join(buf)

if cards:
    for c in cards:
        cid = str(c.get("id", ""))
        cid = cid.split("-")[0] if "-" in cid else cid
        if cid in want or str(c.get("id", "")) in want:
            lines.append("=" * 60)
            lines.append(fmt(c))

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print("OK %d lines" % len(lines))
