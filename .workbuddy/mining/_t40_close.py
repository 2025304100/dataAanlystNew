# -*- coding: utf-8 -*-
"""T40 收工：PROGRESS.json T40 -> done + artifacts/evidence。

T40 的真实情形：**artifacts 已被前置完成** —— T28~T37 每张前端卡都同步维护
zh-CN/en-US 两份，故开工时 key 集合早已一致（探针实测 5060 = 5060、零差集）。
DoD 括号内注明目标为 translations.test.ts 绿；全量 exit 1 系既有 C 类 22 项所致。
同时清理临时探针文件（用后即删）。
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
    raw = (M / name).read_bytes()
    return re.sub(r"\x1b\[[0-9;]*m", "", raw.decode("utf-8-sig", errors="replace"))


def stats(name: str) -> dict:
    txt = read(name)
    m = re.search(r"EXIT=(\d+)", txt)
    p = re.findall(r"(\d+) passed", txt)
    f = re.findall(r"(\d+) failed", txt)
    return {
        "exit": int(m.group(1)) if m else -1,
        "passed": int(p[-1]) if p else -1,
        "failed": int(f[-1]) if f else 0,
        "fail_files": sorted(set(re.findall(r"FAIL\s+([^\s\[:]+)", txt))),
    }


dod = stats("t40_dod.txt")
fe = stats("t40_fe_sweep.txt")
probe = json.loads((M / "t40_probe_result.json").read_text(encoding="utf-8"))
print("DoD translations:", dod["exit"], dod["passed"], "passed")
print("前端全量        :", fe["exit"], fe["passed"], "passed /", fe["failed"], "failed")
print("探针            : zh", probe["zhCount"], "= en", probe["enCount"],
      "| onlyZh", len(probe["onlyZh"]), "| onlyEn", len(probe["onlyEn"]),
      "| en含中文", probe["untranslatedCount"], "| zh空值", len(probe["emptyZh"]))

assert dod["exit"] == 0 and dod["failed"] == 0, "translations 未绿"
assert probe["zhCount"] == probe["enCount"], "key 数不一致"
assert not probe["onlyZh"] and not probe["onlyEn"], "存在 key 差集"
assert not probe["emptyZh"], "zh 存在空值"
# en 侧唯一允许的含中文值：语言自称（语言选择器设计如此）
allowed = {"langZhCN"}
bad = [k for k, _ in probe["untranslated"] if k not in allowed]
assert not bad, f"en-US 存在未翻译残留: {bad}"

BASELINE_FAILED, BASELINE_FILES = 22, 7
ok = fe["failed"] <= BASELINE_FAILED and len(fe["fail_files"]) <= BASELINE_FILES
print(f"全量 vs 基线：{fe['failed']} vs {BASELINE_FAILED}；文件 "
      f"{len(fe['fail_files'])} vs {BASELINE_FILES} → {'零引入' if ok else '新增失败'}")
assert ok, "全量失败数超过基线"

# ── 清理临时探针（用后即删）────────────────────────────────────────
probe_file = ROOT / "frontend/src/i18n/__tests__/__probe_t40.test.ts"
removed = False
if probe_file.exists():
    probe_file.unlink()
    removed = True
print("临时探针已删除:", removed)

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
# T40 为「无代码改动的证明型收工」，未单独开登记 → 不存在则创建
t40 = prog["tasks"].setdefault("T40", {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
})
assert t40.get("status") in ("in_progress", None), f"T40 状态异常: {t40.get('status')}"

t40["status"] = "done"
t40["finished_at"] = NOW
t40["artifacts"] = [
    "artifacts「en-US 与 zh-CN key 集合一致」**已被前置达成**：探针实测 "
    "zh-CN 5060 键 = en-US 5060 键、**零差集**（onlyZh/onlyEn 均为空）、"
    "zh-CN 无空值；en-US 中唯一含中文字符的值是 `langZhCN = \"简体中文\"`，"
    "属**语言选择器母语自称**的正确设计（非未翻译残留）",
    "本卡**未修改 zh-CN**（not_do）与 en-US（无缺口可补）——实际改动为零，"
    "属「前置完成 + 证明」型收工（同 T38「不做也是合格交付」的处理范式）",
    "临时取证工具：运行时探针 `__probe_t40.test.ts`（用后即删，落盘 JSON "
    "`.workbuddy/mining/t40_probe_result.json` 作为证据保留）",
]
t40["evidence"] = [
    f"DoD（括号内目标为 translations.test.ts 绿）→ **{dod['passed']} passed,"
    f" exit {dod['exit']}**；全量 `npm test` exit 1 系**既有 C 类 22 项**"
    f"（TD-FE-RED-2，与本卡无关）",
    f"前端全量 → **{fe['passed']} passed / {fe['failed']} failed**（失败 "
    f"{fe['failed']} vs 基线 {BASELINE_FAILED}、文件 {len(fe['fail_files'])} vs "
    f"{BASELINE_FILES}）→ **零引入**",
    "**key 集合一致性证明（运行时取值，非正则扫源码）**："
    "Object.keys(zhCN).sort() 与 Object.keys(enUS).sort() 长度均 5060、差集为空 —— "
    "既符合 artifacts，也印证 pitfalls「只加一份会让 translations 红」在本卡场景**从未发生**"
    "（T28~T37 每张前端卡均同步维护两份）",
    "关键发现（对排期有解释价值）**T40 之所以无活可干，正是因为它所依赖的前置卡"
    "（T28~T37）在实现时都遵守了「i18n 两份同步」约定** —— 该约定已由 "
    "`_card_i18n_autofix.py` 固化为写权限层面的强制（writes 自动补 i18n）",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/无重复键）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T40 -> done @", NOW)
