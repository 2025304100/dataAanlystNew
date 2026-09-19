# -*- coding: utf-8 -*-
"""T34 收工：PROGRESS.json T34 -> done + artifacts/evidence。

防波及数字实时读取落盘结果（正则取 `Tests X failed | Y passed (Z)` 全量行，
勿抓单文件行数——T27 教训）。
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


txt = read("t34_sweep.txt")
m_exit = re.search(r"EXIT=(\d+)", txt)
# pytest 全绿时汇总行为「N passed, M warnings」**不含 failed**；
# 且必须取**末尾**那次匹配（汇总行），勿抓单文件行数（T27 教训）。
passed_all = re.findall(r"(\d+) passed", txt)
failed_all = re.findall(r"(\d+) failed", txt)
files_all = re.findall(r"Test Files\s+(?:(\d+) failed \| )?(\d+) passed", txt)
exit_code = int(m_exit.group(1)) if m_exit else -1
passed = int(passed_all[-1]) if passed_all else -1
failed = int(failed_all[-1]) if failed_all else 0
ftotal = -1
ffailed = 0
if files_all:
    ffailed = int(files_all[-1][0]) if files_all[-1][0] else 0
    ftotal = int(files_all[-1][1])
fail_files = sorted(set(re.findall(r"FAILED\s+([^\s:]+)", txt)))
print(f"sweep: {passed} passed / {failed} failed | exit {exit_code} | "
      f"files {ffailed}/{ftotal} red")
assert exit_code == 0 and failed == 0 and passed > 0, (
    f"防波及未全绿，禁止收工：exit={exit_code} passed={passed} failed={failed}")

MINE = {"tests/services/factors/mining/test_reproduction.py"}
assert not (MINE & set(fail_files)), "本卡测试文件失败"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t34 = prog["tasks"]["T34"]
assert t34.get("status") == "in_progress", f"T34 状态异常: {t34.get('status')}"

t34["status"] = "done"
t34["finished_at"] = NOW
t34["artifacts"] = [
    "app/services/factors/mining/reproduction/scheduler.py（**C1 唯一调参者**）："
    "五状态调节表（early 变异0.70/注入0.12/结构字段为主/跨赛道0.30、mid 0.60/0.10/四类型均衡"
    "/0.20、late 0.45/0.05/参数微调为主/0.10、stall_rescue 0.75/0.15/结构字段拉高/0.40、"
    "fast_progress 0.45/0.05/参数微调/0.15）；**阶段态（early/mid/late）由代数直通不算防抖对象**，"
    "**异常态（自救/快速进步）连续 2 代才切换**（向导 §6.6.3 滞回）；单步限幅 ≤0.10 逼近目标；"
    "三率先限步→夹 GUARD 区间→`normalize_rates` 归一（交叉率=1−变异−注入，越界回补，"
    "兜底按比例缩放）保证**和恒为 1**；输出 `contracts.Strategy`（复用冻结契约，不新建类型）；"
    "not_do 守成立：C1 只出参数、不执行变异/交叉（无 offspring 字段）",
    "app/services/factors/mining/diversity.py（**D3 只观测**）：三层合成 health∈[0,1]——"
    "基因型层 `1 − dedup.structural_similarity` 两两均值（复用去重树距离，不重复算，"
    "超 60 个体抽样防 O(n²)）、表现型层 ICIR 归一化极差 `spread/(1+spread)`、类型层"
    " category_evenness 由调用方传入（A1 产出）；默认等权、任一层偏低即拉低 health；"
    "**不导出任何改率函数、不返回任何率字段**（not_do：D3 不改任何率）",
    "app/services/factors/mining/convergence.py（**D1 只判停止**）：`stall_count` = 连续"
    " |delta|<0.01 的代数；进度显著→归零；**先自救**（stall≥2 → should_rescue，交 C1 拉高探索）；"
    "**后停止**（stall≥3 → should_stop，进最终验证），避免在高原期误停；"
    "not_do 守成立：D1 不含任何率字段（无 mutation_rate/random_rate）",
    "app/services/factors/mining/reproduction/mutation.py（C2 四变异）：param 只改数值"
    "（tree_shape_key 不变）、op 同优先级算子互换（+ ↔ −、* ↔ /）、field **目标字段必须落在"
    "用户已选字段域内**、struct 包裹算子或二元组合（最激进）；"
    "`allocate_mutation_types` 用**最大余数法**按 C1 distribution 精确分配名额"
    "（小样本不漂移、余数并列按固定类型序可复现）+ 洗牌防扎堆；"
    "not_do 守成立：C2 不自定比例、不定变异率",
    "app/services/factors/mining/reproduction/crossover.py（C3 子树交叉）：`should_cross_category`"
    " 按 C1 ratio 伯努利抽签；`crossover_formula` 括号匹配剪两侧 `name(args)` 子树交换拼接，"
    "纯字段/常量无子树时退化为二元组合、替换退化时兜底，保证恒产出新个体；"
    "后代 category 由调用方按 §6.5.2 重新归类（不在本模块硬编码）；not_do 守成立：不自定跨赛道比例",
    "app/services/factors/mining/reproduction/injection.py（D2 随机注入）：`injection_quota`"
    " = round(size × C1 random_rate) 夹 [0,size]（不再固定 10%）；`inject_individuals` 复用"
    " `random_generator.generate_random_candidates`（与初始种群同源，不另起生成器），"
    "生成器异常返回空列表**不抛**（注入失败≠进化失败，按 shortfall 处理）；"
    "not_do 守成立：不自定注入率",
    "tests/services/factors/mining/test_reproduction.py：35 用例（C1 护栏：三率区间+和1+"
    "极端输入夹取+单步限幅+滞回2代+自救拉高探索+late 参数为主+early 结构为主+输出为契约 Strategy"
    "+C1 不执行繁殖；D3：同构→0/异构>同构/表现型离散/health 边界与等权/单层偏低拉低/D3 不返回率；"
    "D1：进度归零/stall 累加/stall=2 自救不停/stall=3 才停/阈值边界/D1 不调参；"
    "C2：四类型齐备+param 不改结构+field 落在 allowed+op 只换算子+struct 改结构+最大余数精确分配"
    "+零名额；C3：ratio 0/1 两端+0.4 统计压力+产出新个体；D2：名额按率+夹取+不超 ceil；"
    "贯通：D3/D1 观测 → C1 决策 → C2/C3/D2 执行 全链路）",
]
t34["evidence"] = [
    "DoD pytest tests/services/factors/mining/test_reproduction.py -q → **35 passed, exit 0**"
    "（TDD 全程：红（ModuleNotFoundError: convergence）→ 六模块实现 → 1 failed 实现侧定性修复 → 全绿）",
    "修复 1 处**实现 bug**（非测试问题）：`_mutate_field` 原要求「源字段也在 allowed 池内才替换」，"
    "导致 `ts_mean(close,5)` + allowed=(open,volume) 时静默原样返回——语义应为"
    "「源字段取公式实际用到的字段、目标才必须在已选域内」。已修正并在 docstring 记录实测坑",
    f"防波及：pytest tests/services/factors/mining/ 全量 → **{passed} passed / {failed} failed,"
    f" exit {exit_code}**（{ftotal} 个测试文件，含本卡新增 35 用例；warnings 均为既有"
    f" SAWarning/Deprecation 非本卡引入）",
    "C5 红线扫描（_t34_c5_scan.py）：6 个新模块仅依赖 stdlib + mining 内部"
    "（dedup.structural_similarity / contracts.Strategy / random_generator），"
    "factor_experience/F1 model/SQLAlchemy/pandas/路由零触碰；引用方仅 reproduction 自身与"
    "本卡测试（GA 主循环接入属后续卡，符合排期单向依赖）",
    "自研可调参唯一性验证：三率归一 `normalize_rates` 在 early/mid/late/rescue/fast 五状态"
    "及极端输入下均满足区间内且和=1（35 用例含逐代循环断言，非抽样）",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/迁移编号不占用/无重复键；"
    "T34 reproduction/ 与 T33 selection/ 无写重叠）",
    "纯算法卡零 DB 依赖：无迁移、无模型、无路由——不触碰 MySQL/duckdb（S/R 审计不适用）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS T34 -> done @", NOW)
