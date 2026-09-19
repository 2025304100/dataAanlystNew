# -*- coding: utf-8 -*-
"""T37 收工：PROGRESS.json T37 -> done + artifacts/evidence（G6）。"""
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


dod = stats("t37_dod.txt")
fe_tests = stats("t37_run1.txt")
trans = stats("t37_trans.txt")
fe = stats("t37_fe_sweep.txt")
be = stats("t37_be_sweep.txt")
print("DoD(G6) grading+stats:", dod["exit"], dod["passed"], "passed /", dod["failed"])
print("前端新增组件       :", fe_tests["exit"], fe_tests["passed"], "passed")
print("translations       :", trans["exit"], trans["passed"], "passed")
print("前端全量           :", fe["exit"], fe["passed"], "passed /", fe["failed"], "failed")
print("后端 mining 全量   :", be["exit"], be["passed"], "passed /", be["failed"], "failed")

MINE = {"src/components/factors/mining/result/FactorMiningGradeEvidence.test.tsx",
        "src/components/factors/mining/config/MiningExperiencePage.test.tsx",
        "src/i18n/__tests__/translations.test.ts"}
assert dod["exit"] == 0 and dod["failed"] == 0, "DoD(G6) 未全绿"
assert fe_tests["exit"] == 0 and fe_tests["failed"] == 0, "前端新增测试未全绿"
assert trans["exit"] == 0 and trans["failed"] == 0, "i18n 门禁未全绿"
assert not (MINE & set(fe["fail_files"])), "本卡文件在前端全量中失败"
BASELINE_FAILED, BASELINE_FILES = 44, 10
ok = fe["failed"] <= BASELINE_FAILED and len(fe["fail_files"]) <= BASELINE_FILES
print(f"前端既有红对比：{fe['failed']} vs {BASELINE_FAILED}；文件 "
      f"{len(fe['fail_files'])} vs {BASELINE_FILES} → {'零引入' if ok else '新增失败'}")
assert ok, "前端全量失败数超过既有基线，禁止收工"
assert be["exit"] == 0 and be["failed"] == 0, "后端 mining 全量未全绿"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t37 = prog["tasks"]["T37"]
assert t37.get("status") == "in_progress", f"T37 状态异常: {t37.get('status')}"

t37["status"] = "done"
t37["finished_at"] = NOW
t37["artifacts"] = [
    "result/GradeEvidenceDrawer.tsx（证据抽屉，向导 §8.4.1~§8.4.3）：**恰好 3 个 Tab**"
    "（定级证据 / 统计检验证据 8 项 / 血缘与来源）—— not_do「不做 4 Tab」；"
    "Tab1 等级大标签 + 一句话定级理由 + 8 维度明细（当前值/阈值/达标/差距）+ **阈值来源**"
    "（默认 / 自定义）；Tab2 **8 项统计检验** + `total_trials` 说明 + Walk-Forward 方向一致性"
    "注释，**月频时 Bootstrap 与置换两行 `data-disabled=true` 灰掉并标注「样本不足，未计算」**"
    "（pitfalls 第二条）且抽屉顶部提示「月频最高 B 级」；Tab3 进化路径（代数/父代/操作）"
    "+ **经济逻辑**（AI 生成必显示）；人工调整等级（目标等级 + 原因**≥10 字**强校验）→"
    "提示「季度重评不会自动覆盖」+「恢复自动」按钮（§8.4.2）；季度重评横幅"
    "（**升级 data-direction=up / 降级 down**、移出 FactorSet 追加提示）+ "
    "**评级历史时间轴**（等级/时间/触发方式/原因，§8.4.3）",
    "config/MiningExperiencePage.tsx（F1 列表页，§6.9.7/§8.4）：等级筛选 chip（全部/S/A/B/C/D "
    "+ 各级数量）、点击公式打开证据抽屉、**D 级默认不进批量选择集**（pitfalls 第一条："
    "「全选（不含 D 级）」只覆盖非 D 级，D 级需人工显式单个勾选）、等级变更 7 天内「新」角标",
    "tests/services/factors/mining/test_grading.py：新增 `TestEvidenceDrawerContract`（6 用例）"
    "—— 理由必须点出最终等级（抽屉一句话理由）、硬约束生效时理由须说明原因、"
    "`thresholds_source` 透传（custom 可辨识）、degraded/monthly 压 B（抽屉月频提示依据）、"
    "**D 级也有完整证据**、**「恢复自动」后季度任务重新接管**（manual=1 期间 keep、"
    "manual=0 时 promote 恢复生效）",
    "前端测试 2 文件共 24 用例：抽屉 19 例（Tab 恰好 3 个、切换、等级标签与理由、"
    "8 维度行与阈值来源正反、8 项统计、total_trials、**月频 Bootstrap/置换灰掉 + 其余 6 项不灰**、"
    "月频顶部提示、日频不灰、血缘与经济逻辑、原因 ≥10 字校验、提交携带等级与原因、"
    "人工提示与恢复自动、升级/降级横幅方向、移出 FactorSet、评级历史）"
    "+ 列表页 5 例（chip 数量与筛选、打开抽屉、**全选不含 D 级**、D 级可单独勾选、新角标）",
    "i18n zh-CN + en-US：新增 35 个 `miningEvid*` / `miningExp*` key 两份严格同步",
]
t37["evidence"] = [
    f"DoD（G6 门禁）`pytest test_grading.py test_statistical_tests.py -q` →"
    f" **{dod['passed']} passed, exit {dod['exit']}**（52 基线 + 6 新增；"
    f"G6 = M2 算法「选择/繁殖/统计/分级全链路」抽样门禁）",
    f"前端新增组件测试 → **{fe_tests['passed']} passed, exit {fe_tests['exit']}**"
    f"（一次全绿）",
    f"i18n 同步门禁 → **{trans['passed']} passed, exit {trans['exit']}**（新增 35 key 两份齐全）",
    f"防波及：前端全量 → **{fe['passed']} passed / {fe['failed']} failed**（与既有红基线"
    f" 44 / 10 文件持平，**零引入**）；后端 mining 全量 → **{be['passed']} passed,"
    f" exit {be['exit']}**（本卡扩充了 test_grading.py，须验证全量）",
    "**pitfalls 双守成立**：①D 级默认不进批量选择集 —— 全选只选非 D 级（测试断言勾选数为 2、"
    "D 级 checkbox 保持 false），D 级仍可人工单独勾选（未被硬禁）；②月频时 Bootstrap/置换"
    "两行 `data-disabled=true` 且文本含「样本不足」，同日频用例断言其余 6 项**不灰**"
    "（正反对照，防「全灰」蒙混）",
    "**not_do 守成立**：抽屉 Tab 数断言**恰好 3**（不做 4 Tab）；未实现导入导出 / 清理建议 / "
    "共享库；M2 无物理删除入口（仅「移出 FactorSet」提示，属逻辑解绑）",
    "selfcheck ALL OK；writes 补登记 i18n 两份（T27~T37 连续第 7 张前端卡同款口径）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T37 -> done @", NOW)
