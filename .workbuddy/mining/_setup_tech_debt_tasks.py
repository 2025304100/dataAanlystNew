#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""立项技术债修复任务 TD1~TD5（2026-09-18，裁决 D-J）。

改动：
  1. tasks.json
     - 追加 5 张 TD 卡（phase=TDB，预算/写权限/DoD/坑俱全）
     - T26 迁移编号 0055 -> 0058（0055 已被 D-H pool_id 迁移实际占用）、deps += TD4
     - T36 迁移编号 0056 -> 0059、deps += T26
  2. PROGRESS.json
     - global_decisions 追加 D-J；observations 追加 3 条；updated_at/updated_by

幂等：任一步骤的标记已存在则跳过。跑完必须再跑 _selfcheck_conflict.py。
"""
import io
import json
from datetime import datetime, timezone, timedelta

BASE = r"D:/ai_project/dataAanlystNew/.workbuddy/mining"
TODAY = "2026-09-18"
MARK_T26 = "RENUMBER-T26-0058"
MARK_T36 = "RENUMBER-T36-0059"
MARK_TD = "TD1-MODEL-RUN-FK"

# ══════════════════════════════════════════════════════════
# 1. tasks.json
# ══════════════════════════════════════════════════════════
tpath = BASE + "/tasks.json"
tdoc = json.loads(io.open(tpath, encoding="utf-8").read())
idx = {t["id"]: t for t in tdoc["tasks"]}
log = []

# ---- 1a. T26 编号重排 + 依赖 + 坑 ----
t26 = idx["T26"]
if "alembic/versions/2026_*_0055_*.py" in t26["writes"]:
    t26["writes"] = [
        "alembic/versions/2026_*_0058_*.py" if w == "alembic/versions/2026_*_0055_*.py" else w
        for w in t26["writes"]
    ]
    if "TD4" not in t26["deps"]:
        t26["deps"] = t26["deps"] + ["TD4"]
    if not any(MARK_T26 in p for p in t26["pitfalls"]):
        t26["pitfalls"].append(
            "★ 迁移编号已重排为 0058（原声明的 0055 已被 D-H pool_id 迁移实际占用，"
            "tasks.json 声明与磁盘漂移）；开工时先 alembic heads 取当前链尾接上，勿硬编码信任卡片编号。"
        )
    log.append("T26: 0055->0058, deps+=TD4")

# ---- 1b. T36 编号重排 + 依赖 + 坑 ----
t36 = idx["T36"]
if "alembic/versions/2026_*_0056_*.py" in t36["writes"]:
    t36["writes"] = [
        "alembic/versions/2026_*_0059_*.py" if w == "alembic/versions/2026_*_0056_*.py" else w
        for w in t36["writes"]
    ]
    if "T26" not in t36["deps"]:
        t36["deps"] = t36["deps"] + ["T26"]
    if not any(MARK_T36 in p for p in t36["pitfalls"]):
        t36["pitfalls"].append(
            "★ 迁移编号已重排为 0059（原 0056 让给 TD1）；开工时先 alembic heads 取当前链尾接上"
            "（此时链尾应为 T26 的 0058）。"
        )
    log.append("T36: 0056->0059, deps+=T26")

# ---- 1c. TD 卡 ----
AUDIT_READS = [
    "docs/因子挖掘系统-系统设计文档-v2.0.md#8.6",
    "docs/因子挖掘系统-开发需求文档-落地版.md#6.4 生命周期与数据落库",
]

TD_CARDS = [
    {
        "id": "TD1",
        "phase": "TDB",
        "title": "债·factor_audit_logs.model_run_id 外键补齐",
        "deps": ["T25"],
        "budget": "S",
        "writes": [
            "alembic/versions/2026_*_0056_*.py",
            ".workbuddy/mining/_verify_audit_model_run_fk.py",
        ],
        "reads": AUDIT_READS,
        "artifacts": ["0056 迁移（补 FK）", "真实库行为验证脚本与报告"],
        "dod": [
            {
                "cmd": "{PY} .workbuddy/mining/_verify_audit_model_run_fk.py",
                "expect": "exit 0：孤儿插入被拒（MySQL 1452）+ 删父行 model_run_id 置 NULL（SET NULL）双验证",
            },
            {
                "cmd": "{PY} .workbuddy/mining/verify_schema_drift.py",
                "expect": "exit 0 且 factor_audit_logs 零漂移（含外键项）",
            },
        ],
        "not_do": [
            "不改 factor_audit_logs 业务逻辑与写入方",
            "不动其它表的既有外键",
        ],
        "pitfalls": [
            "★ MyISAM 会静默吞 FK（T01/T07 两次踩中）：迁移前先确认表引擎是 InnoDB；"
            "MySQL 的 ADD CONSTRAINT 无 IF NOT EXISTS，脚本重跑前先查 information_schema",
            "★ 行为验证是唯一标准（information_schema 有定义 != 被强制）：插孤儿行应被拒（1452）、"
            "删父行应 SET NULL；用 ORM 表对象插入（模型用 Python 端默认值）",
            "★ down_revision 接当前 alembic 链尾（开工时 alembic heads 确认，应为 0055）",
            "ORM 侧早已声明该 FK（SET NULL），本任务只补数据库层，模型文件不用改",
        ],
    },
    {
        "id": "TD4",
        "phase": "TDB",
        "title": "债·账务关键列 Float->Double（8 列 6 表，P1）",
        "deps": ["T25", "TD1"],
        "budget": "S",
        "writes": [
            "alembic/versions/2026_*_0057_*.py",
            "app/models/sim_account.py",
            "app/models/portfolio.py",
            "app/models/portfolio_equity_snapshot.py",
            "app/models/backtest.py",
            "tests/test_whitebox_numeric_columns.py",
            ".workbuddy/mining/_verify_accounting_double.py",
        ],
        "reads": AUDIT_READS,
        "artifacts": ["0057 迁移（8 列 DOUBLE）", "ORM 声明对齐", "精度行为验证"],
        "dod": [
            {
                "cmd": "{PY} -m pytest tests/test_whitebox_numeric_columns.py -q",
                "expect": "exit 0（ORM 声明 = Double 与迁移列清单一致）",
            },
            {
                "cmd": "{PY} .workbuddy/mining/_verify_accounting_double.py",
                "expect": "exit 0：8 列 information_schema DATA_TYPE=double，且 1.2e9+0.01 级精度写入读回一致（真实库）",
            },
            {
                "cmd": "{PY} .workbuddy/mining/verify_schema_drift.py",
                "expect": "6 张表零漂移（模型=库）",
            },
        ],
        "not_do": [
            "只动账务 8 列（cash_ledger.balance_after/amount、portfolios.total_capital、positions.market_value、portfolio_equity_snapshots.market_value/cash_balance、sim_orders.filled_amount、backtest_runs.initial_capital）；其余 40+ 个 float 列按 TD5 报告再裁决",
            "不改业务逻辑与计算口径（纯拓宽无损）",
        ],
        "pitfalls": [
            "★ SQLAlchemy Float（无 precision）在 MySQL 是单精度 float；ORM 侧用 Double，"
            "迁移用 ALTER ... MODIFY ... DOUBLE",
            "★ SQLite REAL=8 字节 DOUBLE，单测对精度天然放行（R34）—— 精度断言必须真实库试探",
            "★ 开工时 alembic heads 确认链尾（应为 0056，由 TD1 产生）",
            "★ 跨域改动（portfolio/sim/backtest 非 M1 域）：float->double 为无损拓宽，读端无需迁移；"
            "回归必须含组合/回测域相关测试",
        ],
    },
    {
        "id": "TD2",
        "phase": "TDB",
        "title": "债·tests/integration 顺序污染治理",
        "deps": ["T25"],
        "budget": "M",
        "writes": ["tests/integration/"],
        "reads": [
            "docs/因子挖掘系统-开发需求文档-落地版.md#6.4 生命周期与数据落库",
        ],
        "artifacts": ["隔离机制修复 + 整目录全绿证据"],
        "dod": [
            {
                "cmd": "{PY} -m pytest tests/integration -q",
                "expect": "exit 0：整目录一次跑，10 个既有失败清零（单跑/整跑一致）",
            },
            {
                "cmd": "{PY} -m pytest tests/integration -q",
                "expect": "连跑第二遍仍 exit 0（顺序无关、可重复）",
            },
        ],
        "not_do": [
            "不许用 try/except 或 skip/xfail 掩盖失败",
            "不许改 app/ 源码迁就测试（根因若在全局单例 DatabaseManager，先上报裁决；本卡写权限不含 app/）",
        ],
        "pitfalls": [
            "已知症状：整目录 10 failed / 单跑全过；失败形态 PendingRollbackError 落在 "
            "async_tasks._expire_stale_tasks 的 UPDATE —— 典型「全局单例 + 跨测试共享 session/事务」",
            "先写最小复现（两条用例固定顺序连跑），再修；修复必须是隔离机制"
            "（每用例独立 engine/session 或显式 teardown），不是删测试",
            "tests/services/** 不建 __init__.py 的约定同样适用于 tests/integration，别顺手加",
            "★ 修完必须全量回归一次（该目录修复常牵动 conftest，影响面不止 integration）",
        ],
    },
    {
        "id": "TD3",
        "phase": "TDB",
        "title": "债·metrics_json NaN 字面量合规化（写入侧 + 存量）",
        "deps": ["T25"],
        "budget": "M",
        "writes": [
            "app/services/factors/factor_evaluator.py",
            "app/services/factors/factor_shadow.py",
            "app/services/factors/wp5_eval_task.py",
            "app/services/factors/ridge_model.py",
            "app/api/routes/factor_models.py",
            "tests/services/factors/mining/test_metrics_json_hygiene.py",
            ".workbuddy/mining/_backfill_metrics_json_nan.py",
        ],
        "reads": [
            "docs/因子挖掘系统-开发需求文档-落地版.md#6.4 生命周期与数据落库",
            "docs/因子挖掘实验向导-详细设计.md#8.4 结果总览·后续操作",
        ],
        "artifacts": ["写入侧统一清洗", "存量 backfill 脚本与清洗报告"],
        "dod": [
            {
                "cmd": "{PY} -m pytest tests/services/factors/mining/test_metrics_json_hygiene.py -q",
                "expect": "exit 0（写入侧 NaN/±Inf -> null，且 dumps allow_nan=False fail-fast）",
            },
            {
                "cmd": "{PY} .workbuddy/mining/_backfill_metrics_json_nan.py --dry-run",
                "expect": "报告存量含 NaN/Infinity 字面量的行数，不写库",
            },
            {
                "cmd": "{PY} .workbuddy/mining/_backfill_metrics_json_nan.py --apply",
                "expect": "exit 0，复扫为 0 行（幂等，可重跑）",
            },
        ],
        "not_do": [
            "不动 data_quality.py 的 metrics_json（数据治理域，跨域待 owner）",
            "不动挖掘域新链路（已过 db_numeric）",
            "前端解析兼容另评估（本卡只保证合法 JSON）",
        ],
        "pitfalls": [
            "★ 口径与 D-F/R33 一致：NaN/±Inf -> null（不许归 0）；写入侧统一走 "
            "db_numeric.clean_numeric_fields + json.dumps(allow_nan=False)",
            "★ 改写入侧前先 grep 所有读 metrics_json 的代码与前端调用，确认 null 与 NaN 字面量的解析差异",
            "★ backfill 必须先 --dry-run 报数再 --apply；判定用 json.loads 严格模式解析失败来识别"
            "（不要正则改写），只改含 NaN/Infinity/-Infinity 字面量的行，幂等可重跑",
        ],
    },
    {
        "id": "TD5",
        "phase": "TDB",
        "title": "债·全库 schema 漂移核对报告（48/127 起底）",
        "deps": ["TD1"],
        "budget": "M",
        "writes": [
            ".workbuddy/mining/verify_schema_drift.py",
            "docs/技术债-全库schema漂移核对报告.md",
            ".workbuddy/mining/evidence/schema_drift_audit_2026-09.json",
        ],
        "reads": AUDIT_READS,
        "artifacts": ["工具覆盖扩展（引擎/字符集/检查约束）", "48+ 项逐条分类核对报告"],
        "dod": [
            {
                "cmd": "{PY} .workbuddy/mining/verify_schema_drift.py --json > .workbuddy/mining/evidence/schema_drift_audit_2026-09.json",
                "expect": "exit 0，覆盖 列/索引/唯一约束/外键/引擎/字符集/检查约束",
            },
            {
                "cmd": "{PY} .workbuddy/mining/verify_schema_drift.py",
                "expect": "已知缺陷样本能被报出（防假绿），报告结论与 evidence 一致",
            },
        ],
        "not_do": [
            "只出报告与工具扩展；逐条修复另立任务卡（避免一张卡写散）",
            "不动线上表结构",
        ],
        "pitfalls": [
            "★ 工具既有覆盖 列/索引/唯一约束/外键；本次补 引擎/字符集/检查约束 —— "
            "每加一类覆盖，必须用已知缺陷样本验证「能报出」（漂移检查漏掉哪类，那类缺陷就隐形）",
            "★ 48+ 项逐条分类：真实缺陷（建议修复+优先级）/ 声明过期（改迁移或登记豁免）/ 无法判定（列证据待裁决）",
            "★ 工具加 --selftest 用已知缺陷样本回灌（防假绿），同时用正常样本验证零误报",
        ],
    },
]

existing_ids = set(idx)
if not any(c["id"] in existing_ids for c in TD_CARDS):
    tdoc["tasks"].extend(TD_CARDS)
    log.append("tasks.json: +TD1/TD4/TD2/TD3/TD5（45 张卡）")

with io.open(tpath, "w", encoding="utf-8") as f:
    json.dump(tdoc, f, ensure_ascii=False, indent=2)
    f.write("\n")

# ══════════════════════════════════════════════════════════
# 2. PROGRESS.json
# ══════════════════════════════════════════════════════════
ppath = BASE + "/PROGRESS.json"
pdoc = json.loads(io.open(ppath, encoding="utf-8").read())

if not any(d.get("id") == "D-J" for d in pdoc["global_decisions"]):
    pdoc["global_decisions"].append(
        {
            "id": "D-J",
            "decided_at": TODAY,
            "decided_by": "需求方",
            "topic": (
                "过程中抓到的既有技术债处置（盘点 8 项：tests/integration 顺序污染 / 48-127 全库漂移 / "
                "factor_audit_logs.model_run_id 外键缺口 / metrics_json NaN 字面量 / 资金类单精度损失 / "
                "因子域审计写入点口径 / 表字符集 / daily_bars 精度）"
            ),
            "resolution": (
                "成立技术债修复批 **TD1~TD5**（phase=TDB）插排进任务链："
                "TD1 model_run_id 外键补齐（S）→ TD4 账务 8 列 Float->Double（S）→ "
                "TD2 tests/integration 顺序污染治理（M）→ TD3 metrics_json NaN 合规化（M）→ "
                "TD5 全库漂移核对报告（M）。**继续挂账不立卡**：字符集统一（裁决④维持待办）、"
                "因子域 7 处审计写入点统一口径（跨域需 owner 确认）、daily_bars 精度对齐（P2）。"
            ),
            "enforcement": (
                "tasks.json 已建卡（写权限/DoD/not_do/pitfalls 俱全，自检通过）；"
                "迁移编号重排 TD1=0056、TD4=0057、T26=0058（原声明 0055 已被 D-H pool_id 迁移占用）、"
                "T36=0059，并加迁移链依赖 TD4<-TD1、T26<-TD4、T36<-T26 保证 alembic 单链不分支；"
                "执行顺序按 TD1→TD4→TD2→TD3→TD5 建议（TD2/TD3 预算 M 按 id 排在 T27 后，由 Agent 手动提前）。"
                "所有 TD 卡 DoD 都把行为验证/真实库试探列为验收标准（R34）。"
            ),
            "source": (
                "2026-09-17 P0 数值扫描与全库 Float/漂移 observations + 2026-09-18 修复状态盘点 → "
                "需求方批复立项"
            ),
        }
    )
    log.append("PROGRESS: +D-J")

OBS_MARK = "TD1=0056"
obs_new = [
    (
        "🚨 T26 卡声明的迁移编号 0055 已被 D-H pool_id 迁移实际占用（tasks.json 声明与磁盘漂移，"
        "「排期卡片次生缺口」又一实例）；已重排：TD1=0056 / TD4=0057 / T26=0058 / T36=0059，"
        "并加迁移链依赖保证 alembic 单链不分支。"
    ),
    (
        "📋 技术债卡 TD1~TD5 已建立（phase=TDB，任务总数 40→45）；"
        "新卡与 T26/T36 重排已通过 _selfcheck_conflict.py 全部检查（含 C14 原始文档引用、C21 编号唯一）。"
    ),
    (
        "📋 继续挂账（有证据、不立卡）：表字符集统一（④ 待办）；factor_audit_logs 等 7 处既有审计写入点"
        "仍用 canonical_json（跨域，35 处测试断言涉及）；daily_bars 精度对齐（P2）。"
    ),
]
if not any(OBS_MARK in o for o in pdoc["observations"]):
    pdoc["observations"].extend(obs_new)
    log.append("PROGRESS: +%d observations" % len(obs_new))

tz = timezone(timedelta(hours=8))
pdoc["updated_at"] = datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00")
pdoc["updated_by"] = "agent-senior-dev"

with io.open(ppath, "w", encoding="utf-8") as f:
    json.dump(pdoc, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("done:")
for line in log:
    print("  -", line)
