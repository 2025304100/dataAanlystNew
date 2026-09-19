# -*- coding: utf-8 -*-
"""TD5 DoD① runner：跑 --json 全量审计 → evidence 文件，并回读校验。"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
TOOL = os.path.join(REPO, ".workbuddy", "mining", "verify_schema_drift.py")
OUT = os.path.join(REPO, ".workbuddy", "mining", "evidence", "schema_drift_audit_2026-09.json")
ERR = os.path.join(REPO, ".workbuddy", "mining", "_td5_json_stderr.txt")

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as fo, open(ERR, "w", encoding="utf-8") as fe:
    proc = subprocess.run([PY, TOOL, "--json"], stdout=fo, stderr=fe, cwd=REPO)
print("EXIT =", proc.returncode)

d = json.loads(open(OUT, encoding="utf-8").read())
print("bytes:", os.path.getsize(OUT))
print("db:", d["db"])
print("dialect:", d["dialect"], "| default_charset:", d["db_default_charset"])
print("checks_covered:", d["checks_covered"])
print("summary:", json.dumps(d["summary"], ensure_ascii=False))
print("notes:", d["notes"])
err_tail = open(ERR, encoding="utf-8", errors="replace").read().strip()
if err_tail:
    print("[stderr tail]", err_tail[-500:])
