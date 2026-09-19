# -*- coding: utf-8 -*-
"""TD5 收工登记：PROGRESS.json TD5 -> done + artifacts/evidence/observation；
tasks.json TD5 卡 -> done；顺手对齐 TD3 卡遗留 in_progress（PROGRESS 已 done）。幂等。"""
import json
from datetime import datetime

PPATH = r".workbuddy/mining/PROGRESS.json"
TPATH = r".workbuddy/mining/tasks.json"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

ARTIFACTS = [
    ".workbuddy/mining/verify_schema_drift.py v2（7 类覆盖：列/索引/唯一约束/外键 + 新增 引擎/字符集/检查约束。"
    "核心比对抽成 check_table()——main 与 --selftest 共用同一套逻辑，杜绝「自检走另一条路」的假绿；"
    "引擎：ORM 显式声明 mysql_engine 则精确比对、未声明则兜底必须 InnoDB（T01/T07 事故形态）；"
    "字符集：ORM 声明则硬比对、未声明只做 warning 级（库默认非 utf8mb4 时避免 127 表全误报），"
    "每表实际引擎/字符集进 JSON table_options 供人工核对；"
    "CHECK：约束名 + 归一化 SQL 文本双轨比对（规避 MySQL 自动命名差异）；"
    "新增 --json（成功产出即 exit 0 的 DoD 管道语义 + 全库 table_options）与 --selftest"
    "（内存 sqlite 手工建已知缺陷表回灌 + 表选项纯函数 7 用例，不连真实库）；"
    "列差异区分「库多/ORM 多」两个方向）",
    "docs/技术债-全库schema漂移核对报告.md（49/127 起底：结论速览 + TD1 基线 diff + "
    "A 真实缺陷（缺FK 18表30个 P1 / portfolio_candidates CASCADE 行为反转 P1 / alert_events 唯一约束 P2 / "
    "CHECK 11表19个 P2 / idempotency_records ORM多id主键列库里没有 P1）/ "
    "B 声明过期冗余（多索引 26表47个 P3 / 库多列 10表28列三档定性 / RESTRICT≡NO ACTION 10项 P3）/ "
    "C 待裁决（utf8mb3 全库字符集 / strategy_execution_snapshots 模型分裂 / idempotency resource_type+status）+ "
    "处置建议排卡表 + DoD 证据链）",
    ".workbuddy/mining/evidence/schema_drift_audit_2026-09.json（全量审计 JSON：summary/drift_tables/warn_rows/"
    "table_options 127 表引擎字符集全量明细）",
]

EVIDENCE = [
    "DoD① --json 全量审计 → exit 0（报告成功产出即 0 的管道语义），7 类覆盖 column/index/unique_constraint/"
    "foreign_key/engine/charset/check_constraint，127 表 table_options 全量在案（46KB；_td5_run_audit.py / "
    "_td5_audit_regen.txt 回读校验：summary total=127 drift=49 ok=78 warn=0）",
    "DoD② 默认模式 → 49 DRIFT/127（exit 1 = 有漂移的既定语义），DRIFT 名单与 JSON 逐一一致；TD1 已知缺陷样本 "
    "8 表（alert_events/backtest_runs/backtest_trades/watchlist_items/portfolio_candidates/idempotency_records/"
    "sim_orders/trade_setups）全部仍被报出（_td5_default_run.txt）",
    "防假绿 selftest：15 用例全过（exit 0）——7 类覆盖全部「能报出」（缺FK/缺UQ/缺CHECK/多索引/缺索引/"
    "列双向差异/FK ondelete 不符/MyISAM/字符集/排序规则），正常样本零误报（_td5_selftest2.txt）",
    "与 TD1 基线 diff：+2 = governance_events_outbox/task_idempotencies（均为 CHECK 新覆盖抓到——此前该类缺陷隐形，"
    "印证 pitfall「漂移检查漏掉哪类那类就隐形」）；-1 = factor_audit_logs（model_run_id FK 已在库落实，T26/0058 迁移成果复核通过）；"
    "其余 47 表类别与基线一致",
    "系统性发现：全库引擎 127/127 InnoDB（T01/T07 修复成果复核通过，engine 类零漂移）；"
    "全库字符集 127/127 utf8_general_ci（utf8mb3）且库默认 utf8，连接串 charset=utf8mb4 → C1 待裁决大迁移",
    "代码引用面侦查（C 类定性）：portfolios.portfolio_status = 活列（portfolio_state_machine.py 裸 SQL 探测+读写，"
    "运行时自加列模式）；idempotency_records.result_json = 有意保留（模型 docstring「同表两列共存」）；"
    "strategy_execution_snapshot.py 被 app/models/__init__.py:95 显式排除注册而 DB 的 usage_binding_id 列正被 "
    "portfolio_factor_usage.py 写入 = 模型分裂待裁决；其余库多列（test_start/end_date、notification_deliveries 5 列、"
    "notification_templates 6 列、portfolio_members 2 列、positions.target_weight_pct、sim_orders.sim_account_id）"
    "app/ 全域 grep 零引用 = 残留实锤",
    "自产 bug 2 枚均由防假绿链抓出后修复：① CheckConstraint.sqltext 是 TextClause，`raw or \"\"` 触发其 __bool__ "
    "TypeError（selftest 抓出，修 _norm_sqltext 先 str 后判空）；② 重构后 [OK] 分支引用已搬进 check_table 的局部变量 "
    "NameError（DoD② 默认模式首跑抓出——--json 路径不走 OK 分支所以审计没炸，selftest 也不经过该分支；"
    "修 check_table 回传 stats）——「--json 与默认两条路径都必须跑」的价值实证",
]

OBSERVATION = (
    "📋 待裁决（TD5）：① C1 全库字符集 utf8mb3（127/127 utf8_general_ci + 库默认 utf8，连接串 utf8mb4；"
    "4 字节字符写入会报 Incorrect string value）——批量转 utf8mb4 是全表重建大迁移，转/豁免？"
    "② C2 strategy_execution_snapshots 模型分裂——新模型文件被 __init__.py:95 排除注册，但 DB 的 "
    "content_hash/snapshot_json/usage_binding_id 已存在且 usage_binding_id 被活代码写入，新旧模型权威归属？"
    "③ A 类修复排卡：FK 补齐批（30 个缺 FK + portfolio_candidates CASCADE→RESTRICT 行为反转 + alert_events 唯一约束 + "
    "idempotency_records id 主键列）P1、前置孤儿清查（1452）；CHECK 批（19 个）P2；索引/列清理批 P3（先 EXPLAIN/引用面复核）；"
    "④ B3 10 项 RESTRICT vs NO ACTION 在 MySQL 语义等价，建议登记豁免或迁移字面量对齐。"
    "本卡 not_do：未动任何线上表结构，逐条修复另立任务卡。"
)

# ── 1) PROGRESS.json ──
with open(PPATH, encoding="utf-8") as f:
    data = json.load(f)

tasks = data.get("tasks", {})
td5 = tasks.get("TD5")
assert td5 is not None, "TD5 PROGRESS entry missing（开工登记应已建立）"

if td5.get("status") == "done" and any("DoD① --json" in e for e in data.get("evidence", [])[-20:]):
    print("TD5 already closed, skip (idempotent)")
else:
    td5["status"] = "done"
    td5["finished_at"] = NOW
    td5["artifacts"] = ARTIFACTS
    td5["evidence"] = EVIDENCE
    data.setdefault("observations", []).append(OBSERVATION)
    with open(PPATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(PPATH, encoding="utf-8") as f:
        chk = json.load(f)
    t2 = chk["tasks"]["TD5"]
    assert t2["status"] == "done" and len(t2["evidence"]) == len(EVIDENCE)
    assert chk["observations"][-1] == OBSERVATION
    print("PROGRESS: TD5 -> done @", NOW, "| evidence:", len(EVIDENCE), "| artifacts:", len(ARTIFACTS))

# ── 2) tasks.json：TD5 卡收工 + TD3 卡状态对齐 ──
with open(TPATH, encoding="utf-8") as f:
    tdata = json.load(f)

with open(PPATH, encoding="utf-8") as f:
    pdone = {tid: t.get("status") for tid, t in json.load(f)["tasks"].items()}

changed = []
for card in tdata["tasks"]:
    cid = card.get("id")
    if cid == "TD5" and card.get("status") != "done":
        card["status"] = "done"
        card["finished_at"] = NOW
        changed.append("TD5")
    # TD3 挂账对齐：PROGRESS 已 done 而卡仍 in_progress
    if cid == "TD3" and card.get("status") != "done" and pdone.get("TD3") == "done":
        card["status"] = "done"
        card["finished_at"] = card.get("finished_at") or pdone.get("TD3_finished") or NOW
        changed.append("TD3(对齐)")

if changed:
    with open(TPATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(tdata, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(TPATH, encoding="utf-8") as f:
        chk = json.load(f)
    for card in chk["tasks"]:
        if card["id"] in ("TD3", "TD5"):
            print(f"tasks.json {card['id']}: status={card['status']}")
else:
    print("tasks.json TD3/TD5 already aligned, skip")
