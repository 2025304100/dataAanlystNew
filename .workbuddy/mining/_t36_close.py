# -*- coding: utf-8 -*-
"""T36 收工：PROGRESS.json T36 -> done + artifacts/evidence。

守卫：DoD test_grading 全绿 + 调度器白盒测试全绿 + 后端 mining 全量全绿 +
迁移已在真实库应用且 verify_schema_drift 对新表判定 OK。
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
    }


dod = stats("t36_dod.txt")            # test_grading.py
sched = stats("t36_sched.txt")        # 调度器白盒
sweep = stats("t36_sweep.txt")        # 后端 mining 全量
print("DoD test_grading :", dod)
print("调度器白盒       :", sched)
print("后端 mining 全量 :", sweep)
assert dod["exit"] == 0 and dod["failed"] == 0, "DoD 未全绿"
assert sched["exit"] == 0 and sched["failed"] == 0, "调度器测试未全绿"
assert sweep["exit"] == 0 and sweep["failed"] == 0, "后端全量未全绿"

# 迁移验证：新表必须是 OK（既有 49 张表的漂移与本卡无关）
drift = read("t36_drift.txt")
ok_line = [l for l in drift.splitlines() if "factor_grade_history" in l and "[OK]" in l]
assert ok_line, "新增表 factor_grade_history 未通过漂移校验"
print("漂移校验:", ok_line[0].strip())

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t36 = prog["tasks"]["T36"]
assert t36.get("status") == "in_progress", f"T36 状态异常: {t36.get('status')}"

t36["status"] = "done"
t36["finished_at"] = NOW
t36["artifacts"] = [
    "app/services/factors/mining/factor_grading.py：8 维度五级分级**纯函数** `grade()`"
    "（设计 §7.10 签名 `(等级, 理由)`、阈值可注入）——按需求 §6.8 阈值表实现"
    "「**每一级全部满足才定级**」（S→A→B→C 逐级判定，第一个全满足即定级）；"
    "**缺失维度一律视为不满足**（不猜、不填零）；显著性 S/A 用 Bonferroni 校正后 p、"
    "B/C 用原始 p；OOS 判定含区间与同向检查；C 级容忍 OOS 轻微不稳（>-0.1）"
    "而 D 级为明显反向；两条硬约束：`decay_ratio<0.5` → 压至 C 级（不得 B 级以上）、"
    "月频/统计层降级 → 最高 B 级；`review_quarterly` 季度重评（B 连续 2 季稳定升 A；"
    "S/A 连续 2 季下滑即 ICIR 降幅>30% 或 OOS 反转 → 降级并移出 FactorSet；"
    "**`manual_adjusted=1` 不被覆盖**）；`run_quarterly_review(db)` 批量执行入口"
    "（按版本聚合历史、跳过人工调整、每次落一行 source=quarterly、数值过 to_db_float）",
    "app/models/factor_grade_history.py：等级评定历史 ORM（每次评定一行，不覆盖）——"
    "grade/previous_grade/reason/metrics_snapshot_json/thresholds_source/"
    "threshold_snapshot_json/source/action/manual_adjusted/adjusted_by/adjust_reason/"
    "removed_from_factor_set/icir/decay_ratio + 复合索引"
    " ix_factor_grade_history_version_created（重评取最近 N 季）",
    "alembic/versions/2026_09_18_0059_wps_0023_059_factor_grade_history.py（修订"
    " `wps_0023_059`，接链尾 `wps_0023_058`）：显式 `mysql_engine=InnoDB`（R13）、"
    "**不显式声明 charset**（跟随库默认，避免 drift 字符集 WARN）、不建外键"
    "（一致性由 service 同事务保证）、幂等防御（表已存在跳过，downgrade 存在才 drop）",
    "app/services/scheduled_tasks.py：新增任务类型 `factor_grade_review`"
    "（TASK_DEFINITIONS 定义 + `_dispatch_task` 分派分支）——**复用既有调度器**"
    "（not_do：不新建调度器）；source 用 `grading` 区分异步任务，统计写入"
    " `ScheduledTaskRun.message`（重评/升/降/保持/跳过人工/移出 FactorSet）",
    "tests/services/factors/mining/test_grading.py：22 用例（S 全维度达标、"
    "单维不达标降级、A/B/C/D 各级边界、衰减率硬约束与 decay_cap、月频上限与"
    " monthly_cap、degraded 标志、阈值注入、缺失维度归 D、季度升/降/OOS 反转/"
    "manual 不覆盖/无下滑保持、GradeResult 契约）",
]
t36["evidence"] = [
    f"DoD pytest tests/services/factors/mining/test_grading.py -q → **{dod['passed']} passed,"
    f" exit {dod['exit']}**（TDD：红（ModuleNotFoundError）→ 实现 → 全绿；"
    f"纯函数零 DB 依赖）",
    "实现期 1 处语法自纠：模块 docstring 内嵌 ```python 示例中的 `\"\"\"` 提前终止了"
    " docstring（SyntaxError: invalid character '：'）→ 改为注释形式",
    f"调度器回归：tests/test_whitebox_scheduled_tasks.py → **{sched['passed']} passed,"
    f" exit {sched['exit']}**（本卡改动了 TASK_DEFINITIONS 与 _dispatch_task，须验证）",
    f"防波及：pytest tests/services/factors/mining/ 全量 → **{sweep['passed']} passed /"
    f" {sweep['failed']} failed, exit {sweep['exit']}**",
    "迁移已在真实库应用：`alembic upgrade head` → "
    "`wps_0023_058_f1_experience_tables -> wps_0023_059_factor_grade_history`；"
    "`verify_schema_drift` 对新表判定 **[OK] 索引 2 / 列 18 / 引擎 INNODB**（与 ORM 对齐、"
    "零漂移）。既有 49/132 张表的漂移为历史债（缺 FK/索引），**与本卡无关**",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/迁移编号 0059 未重复占用/无重复键）",
    "未违反 not_do：未引入 statsmodels（纯 numpy/标准库实现）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS T36 -> done @", NOW)
