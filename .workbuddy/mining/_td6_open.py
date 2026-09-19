# -*- coding: utf-8 -*-
"""TD6 开工登记：tasks.json 建 TD6 卡（in_progress）+ PROGRESS.json 建条目。幂等。"""
import json
from datetime import datetime

TPATH = r".workbuddy/mining/tasks.json"
PPATH = r".workbuddy/mining/PROGRESS.json"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

CARD = {
    "id": "TD6",
    "phase": "TDB",
    "title": "裁决落地·C2 模型分裂切换（strategy_execution_snapshots）+ C1 utf8mb3 豁免守卫",
    "deps": ["TD5"],
    "budget": "M",
    "writes": [
        "app/models/decision_engine.py",
        "app/models/strategy_execution_snapshot.py",
        "app/models/__init__.py",
        "app/core/text_charset.py",
        "app/db/session.py",
        "tests/test_text_charset_guard.py",
        "docs/技术债-全库schema漂移核对报告.md",
    ],
    "reads": [
        ".workbuddy/mining/evidence/schema_drift_audit_2026-09.json",
        "app/services/portfolio_factor_usage.py",
        "app/db/session.py",
    ],
    "artifacts": [
        "ORM 补 3 列+索引对齐 DB 实态（col_db_only/extra_idx 清零）+ 新模型壳化",
        "全局 4 字节字符写入守卫（before_flush）+ 守卫测试",
        "报告 C1/C2 状态闭环 + service id 契约缺口挂账",
    ],
    "dod": [
        {
            "cmd": "{PY} .workbuddy/mining/verify_schema_drift.py --tables strategy_execution_snapshots",
            "expect": "col_db_only 与 extra_idx 清零（fk_mismatch 的 portfolio_id RESTRICT vs NO ACTION 属 B3 既有可留）",
        },
        {
            "cmd": "{PY} -m pytest tests/test_text_charset_guard.py -q",
            "expect": "全绿（4 字节拦截带字段名、BMP 文本放行）",
        },
        {
            "cmd": "{PY} -m pytest tests/test_g1_factor_usage.py tests/test_backtest_detail_snapshot_contract.py -q",
            "expect": "不因本卡改动新增红（对比修复前基线 _td6_g1_baseline.txt，名单一致=既有）",
        },
    ],
    "not_do": [
        "不动线上表结构（无迁移）",
        "不修 service id 契约缺口（save_and_apply 无 id 生成 + int(snap.id) 与 varchar PK 冲突——另立 P1 卡挂账）",
        "不做 utf8mb4 全库迁移（C1 裁决=豁免+守卫）",
    ],
    "pitfalls": [
        "★ 旧类补列必须照 DB DDL 实态：usage_binding_id int NULL **无 FK**（声明 FK 会制造 missing_fk 假漂移）；snapshot_json/content_hash NOT NULL；三个索引名照 DB（ix_..._snapshot_no/_usage_binding_id/_content_hash）",
        "★ 守卫只拦 ord>0xFFFF 且只查本次变更新值（history.added），勿全量扫描 dirty 对象旧值",
        "★ strategy_execution_snapshot.py 壳化后 __init__.py:95 的排除行保留（自动扫描无需 import 壳，壳不 declare 不会重复 mapper，但保留排除最小 diff）",
        "★ DB 三列是迁移体系之外的加宽（alembic 无 add_column 记录）——以 SHOW CREATE TABLE 实态为唯一权威",
    ],
    "status": "in_progress",
    "started_at": NOW,
}

with open(TPATH, encoding="utf-8") as f:
    tdata = json.load(f)
if any(t.get("id") == "TD6" for t in tdata["tasks"]):
    print("TD6 card already exists, skip card insert")
else:
    tdata["tasks"].append(CARD)
    with open(TPATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(tdata, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("tasks.json: TD6 card created @", NOW)

with open(PPATH, encoding="utf-8") as f:
    pdata = json.load(f)
if "TD6" in pdata.get("tasks", {}):
    print("TD6 PROGRESS entry already exists, skip")
else:
    pdata.setdefault("tasks", {})["TD6"] = {
        "status": "in_progress",
        "agent": "agent-senior-dev",
        "started_at": NOW,
    }
    with open(PPATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(pdata, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("PROGRESS.json: TD6 entry created")
