# -*- coding: utf-8 -*-
"""TD-FE-RED-2 方案 B 收工：skip 已脱节用例 → CI 恢复；同步更新 T39 的 DoD 状态。

预期（全量）：**exit 0**（22 项转为 skipped），此时 T39 的原 DoD
（`npm test` exit 0）**字面达成**，可撤销此前的口径放宽说明。
"""
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")


def read(name: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "",
                  (M / name).read_bytes().decode("utf-8-sig", errors="replace"))


def stats(name: str) -> dict:
    txt = read(name)
    m = re.search(r"EXIT=(\d+)", txt)
    p = re.findall(r"(\d+) passed", txt)
    f = re.findall(r"(\d+) failed", txt)
    s = re.findall(r"(\d+) skipped", txt)
    return {
        "exit": int(m.group(1)) if m else -1,
        "passed": int(p[-1]) if p else -1,
        "failed": int(f[-1]) if f else 0,
        "skipped": int(s[-1]) if s else 0,
        "fail_files": sorted(set(re.findall(r"FAIL\s+(\S+\.(?:tsx|ts))", txt))),
    }


sub = stats("c_skip_verify.txt")
full = stats("c_skip_fe_sweep.txt")
print("7 文件（skip 后）:", sub["exit"], sub["passed"], "passed /", sub["skipped"], "skipped")
print("全量（skip 后）  :", full["exit"], full["passed"], "passed /",
      full["failed"], "failed /", full["skipped"], "skipped")
assert sub["exit"] == 0 and sub["failed"] == 0, "7 文件未全绿"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))

# ── TD-FE-RED-2 收口 ────────────────────────────────────────────────
d2 = prog["debt"]["TD-FE-RED-2"]
d2["round5_at"] = NOW
d2["status"] = "mitigated (plan B)" if full["exit"] == 0 else "in_progress"
d2["round5_findings"] = {
    "action": (
        "按用户拍板的**方案 B**：用 vitest `--reporter=json` 取得结构化失败清单"
        "（含正确编码的中文用例名），对 **22 个失败用例**精确加 `it.skip` + TODO 注释"
        "（说明「写在组件旧实现下、镜像层改造后脱节、四轮修补失败、待按当前实现重写」）"
    ),
    "matching": "22/22 精确匹配、0 未匹配（先前从终端文本提取会遇中文乱码与空格丢失，故改用 JSON reporter）",
    "result": (
        f"7 个目标文件：**{sub['passed']} passed / {sub['skipped']} skipped / 0 failed，exit {sub['exit']}**；"
        f"全量：**{full['passed']} passed / {full['failed']} failed / {full['skipped']} skipped，"
        f"exit {full['exit']}**"
    ),
    "scope_control": "**只 skip 失败用例**；通过的用例保持执行（未丢失有效覆盖）",
    "residual": (
        "22 个 skip 的用例仍需按组件当前实现（P1.1 镜像层）**重写**；"
        "TODO 注释与报告 §10 已留痕，不视为问题消失"
    ),
}
d2["updated_at"] = NOW

# ── T39 的 DoD 口径回正 ─────────────────────────────────────────────
t39 = prog["tasks"]["T39"]
if full["exit"] == 0:
    t39["dod_note"] = (
        "原 DoD：`npm test` exit 0 —— **现已字面达成**（全量 exit 0，22 项既有红按方案 B "
        "转为 skipped 并留 TODO）。此前因 C 类红存在的口径放宽说明作废，现以原 DoD 为准。"
    )
    t39.setdefault("evidence", []).append(
        f"【DoD 回正】TD-FE-RED-2 方案 B 落地后全量 `npm test` → **exit {full['exit']}**"
        f"（{full['passed']} passed / {full['failed']} failed / {full['skipped']} skipped）——"
        f"T39 原 DoD「npm test exit 0」**字面达成**"
    )
    if "dod_note" in t39:
        t39["dod_note_status"] = "resolved"

prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS：TD-FE-RED-2 已记录方案 B；T39 DoD 状态回正")
print(f"排期完成度：{sum(1 for v in prog['tasks'].values() if v.get('status') == 'done')}/47")
