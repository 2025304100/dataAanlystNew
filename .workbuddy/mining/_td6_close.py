# -*- coding: utf-8 -*-
"""TD6 收工：PROGRESS.json TD6 -> done（含 artifacts/evidence），tasks.json TD6 -> done。
幂等；改后由 _selfcheck_conflict.py 把关。
"""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\ai_project\dataAanlystNew")
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

# ── PROGRESS.json ──────────────────────────────────────────────
PP = ROOT / ".workbuddy/mining/PROGRESS.json"
with open(PP, encoding="utf-8") as f:
    prog = json.load(f)

tasks = prog.get("tasks") or {}
td6 = tasks.get("TD6") or prog.get("TD6")
assert td6 is not None, "TD6 entry missing in PROGRESS.json"
td6["status"] = "done"
td6["finished_at"] = NOW
td6["artifacts"] = [
    "ORM 权威声明统一（C2 裁决）：app/models/decision_engine.py 补 3 列照 DB 实态"
    "（usage_binding_id int 可空无FK / snapshot_json Text NOT NULL default='{}' / "
    "content_hash String(64) NOT NULL default=''）+ 3 独立索引（ix_..._snapshot_no/"
    "_usage_binding_id/_content_hash）；app/models/strategy_execution_snapshot.py 壳化为 "
    "re-export（消除新草案类 extend_existing 偶然合并三列的『import 才不炸』假绿机制）",
    "C1 裁决落地：app/core/text_charset.py 全局 4 字节字符写入守卫（Session.before_flush，"
    "只扫 session.new+dirty 本次变更字符串列新值 history.added，报错带 类名.字段名+坏字符"
    "码点去重限5/8字段）；app/db/session.py import 即安装（幂等）；tests/test_text_charset_guard.py 12 用例",
    "迁移链缺口闭环：alembic/versions/2026_09_18_0060_wps_0023_056_td6_snapshot_binding_columns.py "
    "幂等防御式补三列+三索引（MySQL 三段式 nullable→回填→MODIFY NOT NULL；SQLite NOT NULL "
    "DEFAULT ''；线上三列已在=零操作），文件编号 0060（0058=T26、0059=T36 预留，selfcheck C15 抓撞车）",
    "docs/技术债-全库schema漂移核对报告.md：C1/C2 置为已裁决+落地记录新 §8；"
    "遗留 service id 契约缺口（int(snap.id) vs varchar PK）挂账 P1 新卡",
]
td6["evidence"] = [
    "DoD① verify_schema_drift.py --tables strategy_execution_snapshots：col_db_only/"
    "extra_idx 全清零，仅剩 portfolio_id RESTRICT vs NO ACTION（B3 既有等价声明漂移，"
    "DoD expect 原文允许；_td6_dod1_final.txt）",
    "DoD② pytest tests/test_text_charset_guard.py：12/12 绿（含 E2E before_flush 独立 "
    "declarative_base 探针表：合法中文放行/emoji 拦截带字段名/dirty 同值 touch 不误伤）",
    "DoD③ 4 文件回归 **31 passed**（_td6_regress2.txt；修复前 7 failed/24 passed 于 "
    "_td6_regress1.txt，基线 _td6_g1_baseline.txt 为 g1 单文件 10 passed）",
    "迁移修订双向验证（_td6_mig_out.txt，scratch sqlite 全链）：upgrade head → 30 列+"
    "3 新索引齐、head=wps_0023_056；downgrade -1 → 27 列回退干净、新索引全清（遵守"
    "「禁 downgrade base」铁律只验 -1）",
    "7 红根因链（3 探针定位）：conftest.db_session 走 auto-align（create_all 按当前 "
    "metadata，表带三列，健康——_td6_probe2_out.txt）；G1 契约测试 tmp_alembic_db 走"
    "纯迁移链（0026 显式 DDL 无三列）→ no such column；直接构造不填 snapshot_json → "
    "NOT NULL。历史成因：三列靠历史进程 auto-align 补进线上，alembic 从无记录（TD5 "
    "谜团闭环）。修复前全绿=『ORM 与迁移链双错一致』假绿",
    "selfcheck ALL OK（_td6_selfcheck4.txt）：TD6 豁免 9 产物（granularity_note 说明第9"
    "产物为闭环必需）；迁移编号唯一性检查驱动 0058→0060 两次改名避让 T26/T36 预留",
]

with open(PP, "w", encoding="utf-8") as f:
    json.dump(prog, f, ensure_ascii=False, indent=2)
    f.write("\n")

# ── tasks.json ─────────────────────────────────────────────────
TP = ROOT / ".workbuddy/mining/tasks.json"
with open(TP, encoding="utf-8") as f:
    tk = json.load(f)
card = next(t for t in tk["tasks"] if t["id"] == "TD6")
card["status"] = "done"
card["finished_at"] = NOW
with open(TP, "w", encoding="utf-8") as f:
    json.dump(tk, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("TD6 closed at", NOW)
