# -*- coding: utf-8 -*-
"""修正 T27 evidence 中防波及数字：正则误抓单文件行数，应取总览行。"""
import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"

raw = (M / "t27_sweep.txt").read_bytes()
txt = re.sub(r"\x1b\[[0-9;]*m", "", raw.decode("utf-8-sig", errors="replace"))
m_total = re.search(r"Tests\s+(\d+) failed \| (\d+) passed \((\d+)\)", txt)
m_files = re.search(r"Test Files\s+(\d+) failed \| (\d+) passed \((\d+)\)", txt)
assert m_total and m_files, "总览行解析失败"
failed, passed, total = (int(m_total.group(1)), int(m_total.group(2)), int(m_total.group(3)))
ffailed, fpassed, ftotal = (int(m_files.group(1)), int(m_files.group(2)), int(m_files.group(3)))
print(f"总览: {passed} passed / {failed} failed ({total} tests); "
      f"文件 {fpassed} passed / {ffailed} failed ({ftotal})")

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t27 = prog["tasks"]["T27"]
new_line = (
    f"防波及：前端全量 `npm test` → **{passed} passed / {failed} failed**"
    f"（{ftotal} 个测试文件：{fpassed} 绿 / {ffailed} 红）；10 个失败文件"
    f"（ExternalDataSync / FactorEditor / FactorEvaluationLab / FactorLibrary /"
    f" FactorModelPage / FactorModelSettings / TodayDecision.repair /"
    f" FactorBidirectionalLinks / FactorModelPage.collections /"
    f" FactorModelPage.t13-supplement）**全部为既有因子域测试**，错误集中在"
    f" `unifiedErrorNetworkMessage` 与 api mock 缺口（`is not a function`），"
    f"与本卡 3 个目标文件**无交集（零引入校验通过）**"
)
replaced = False
for i, ev in enumerate(t27["evidence"]):
    if ev.startswith("防波及："):
        t27["evidence"][i] = new_line
        replaced = True
        break
assert replaced, "未找到待修正的防波及 evidence"
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("evidence 已修正：", new_line[:120], "...")
