"""Exercise only the pure administrator process-selection policy with synthetic CIM records."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.whitebox

ROOT = Path(__file__).resolve().parents[1]


def _assert_policy_cases(root, cases):
    shell = shutil.which("powershell.exe")
    assert shell
    helper = str(ROOT / "scripts" / "dev_services_process_policy.ps1").replace("'", "''")
    command = f". '{helper}'; $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json; $results = @($payload.cases | ForEach-Object {{ Test-ProjectServiceProcess -Process ([pscustomobject]@{{Name=$_.exe;CommandLine=$_.command;ProcessId=101;CreationDate=$_.creation}}) -ProjectRoot $payload.root -ManagedRecord $_.record }}); ConvertTo-Json -Compress -InputObject $results"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], input=json.dumps({"root": root, "cases": cases}), text=True, capture_output=True, check=True, timeout=30)
    actual = json.loads(result.stdout)
    for case, selected in zip(cases, actual, strict=True):
        assert selected is case["expected"], case["name"]


@pytest.mark.skipif(os.name != "nt", reason="Windows administrator policy uses Windows paths")
def test_service_selection_requires_project_identity_and_exact_script_tokens():
    root = r"D:\workspace\quant project"
    vite = root + r"\frontend\node_modules\vite\bin\vite.js"
    config = root + r"\frontend\vite.codex.config.ts"
    standalone = root + r"\frontend\scripts\universe-standalone-server.mjs"
    cases = [
        {"name": "unrelated uvicorn without state", "command": 'python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000', "exe": "python.exe", "expected": False},
        {"name": "relative vite without state", "command": 'node ./node_modules/vite/bin/vite.js --config vite.codex.config.ts --host 127.0.0.1 --port 5173', "exe": "node.exe", "expected": False},
        {"name": "unrelated absolute vite", "command": 'node D:\\other\\frontend\\node_modules\\vite\\bin\\vite.js --config vite.codex.config.ts --host 127.0.0.1 --port 5173', "exe": "node.exe", "expected": False},
        {"name": "empty command line", "command": "", "exe": "python.exe", "expected": False},
        {"name": "exact project vite", "command": f'node "{vite}" --config "{config}" --host 127.0.0.1 --port 5173', "exe": "node.exe", "expected": True},
        {"name": "exact standalone", "command": f'node "{standalone}"', "exe": "node.exe", "expected": True},
        {"name": "project path prefix collision", "command": f'node "{root}-other\\frontend\\scripts\\universe-standalone-server.mjs"', "exe": "node.exe", "expected": False},
        {"name": "script suffix collision", "command": f'node "{standalone}.backup"', "exe": "node.exe", "expected": False},
        {"name": "project path only in unrelated argument", "command": f'node D:\\other\\universe-standalone-server.mjs --note "{standalone}"', "exe": "node.exe", "expected": False},
    ]
    _assert_policy_cases(root, cases)


@pytest.mark.skipif(os.name != "nt", reason="Windows administrator policy uses Windows paths")
def test_relative_service_commands_require_matching_managed_instance():
    root = r"D:\workspace\quant project"
    command = [r"C:\Python312\python.exe", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
    record = {"pid": 101, "name": "backend", "mode": "uvicorn", "port": 8000, "cwd": root, "command": command,
              "started_at": "2026-10-15 09:23:22", "stdout_log": root + r"\tmp\dev-services\backend.out.log", "stderr_log": root + r"\tmp\dev-services\backend.err.log"}
    base = {"command": '"C:\\Python312\\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000', "exe": "python.exe", "creation": "2026-10-15T09:23:21.200", "record": record}
    cases = [
        {**base, "name": "confirmed managed backend", "expected": True},
        {**base, "name": "PID has been reused", "creation": "2026-10-15T10:00:00", "expected": False},
        {**base, "name": "state PID differs", "record": {**record, "pid": 102}, "expected": False},
        {**base, "name": "another project cwd", "record": {**record, "cwd": root + "-other"}, "expected": False},
        {**base, "name": "different command", "record": {**record, "command": command[:-1] + ["8001"]}, "expected": False},
        {**base, "name": "no process creation time", "creation": None, "expected": False},
        {**base, "name": "old record without start time", "record": {**record, "started_at": None}, "expected": False},
        {**base, "name": "different project log", "record": {**record, "stderr_log": r"D:\other\backend.err.log"}, "expected": False},
        {**base, "name": "unreadable command despite state", "command": "", "expected": False},
        {**base, "name": "unexpected executable despite command", "exe": "node.exe", "expected": False},
    ]
    vite_command = ["node", "./node_modules/vite/bin/vite.js", "--config", "vite.codex.config.ts", "--host", "127.0.0.1", "--port", "5173"]
    cases.append({**base, "name": "confirmed managed relative vite", "exe": "node.exe", "command": " ".join(vite_command), "expected": True,
                  "record": {**record, "name": "frontend", "mode": "vite", "port": 5173, "cwd": root + r"\frontend", "command": vite_command,
                             "stdout_log": root + r"\tmp\dev-services\frontend.out.log", "stderr_log": root + r"\tmp\dev-services\frontend.err.log"}})
    standalone_command = ["node", "frontend/scripts/universe-standalone-server.mjs"]
    cases.append({**base, "name": "confirmed managed relative standalone", "exe": "node.exe", "command": " ".join(standalone_command), "expected": True,
                  "record": {**record, "name": "frontend", "mode": "standalone", "port": 5173, "command": standalone_command,
                             "stdout_log": root + r"\tmp\dev-services\frontend.out.log", "stderr_log": root + r"\tmp\dev-services\frontend.err.log"}})
    _assert_policy_cases(root, cases)
