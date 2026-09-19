# -*- coding: utf-8 -*-
"""TD7 收工：PROGRESS.json TD7 -> done（含 artifacts/evidence），tasks.json TD7 -> done。幂等。"""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\ai_project\dataAanlystNew")
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

PP = ROOT / ".workbuddy/mining/PROGRESS.json"
with open(PP, encoding="utf-8") as f:
    prog = json.load(f)
td7 = (prog.setdefault("tasks", {})).setdefault("TD7", {})
td7["status"] = "done"
td7["finished_at"] = NOW
td7["artifacts"] = [
    "app/services/portfolio_factor_usage.py：save_and_apply_usage_atomic 的 snapshot id 改为"
    "服务端确定性生成（content_hash('ses', snap_hash, correlation_id)，现役 factor_usage_service"
    ".py:563 同款模式）并补齐 4 个 NOT NULL 列（decision_clock_json/member_snapshot_json/"
    "snapshot_hash/effective_from，照 0026 DDL）；snapshot_id 契约 int→str 三处联动"
    "（SaveFactorUsageResult 字段 / 幂等重放 int(data[...]) / outbox payload）",
    "tests/test_wp02_factor_usage_and_fsm_tdd.py 新增 TestWP02FactorUsageAtomicBehavior "
    "行为级 3 用例（17→20 passed）：正常路径两表落数+outbox 契约、幂等重放同 snapshot_id、"
    "注入失败（Step6 前）全回滚无副作用",
    "docs/技术债-全库schema漂移核对报告.md §7/§8：挂账定性修正（原『生产必 TypeError』不成立）"
    "+ TD7 追记（修复内容/遗留双轨整合 P3 排期）",
]
td7["evidence"] = [
    "DoD① pytest tests/test_wp02_factor_usage_and_fsm_tdd.py → **20 passed**（17 既有 + 3 行为级"
    "，_td7_t2.txt；首跑 3 failed 为 FactorSet import 路径错，修 app.models.factor_evaluation 后全绿）",
    "DoD② pytest tests/test_g1_factor_usage.py tests/test_backtest_detail_snapshot_contract.py"
    " → **11 passed**（_td7_t3.txt，现役 factor_usage_service 路径零波及）",
    "DoD③ _td7_no_int_snap.py → exit 0：无 int(snap.id)/int(data['snapshot_id']) 残留",
    "定性侦查三连：service 全文无 id 生成逻辑（grep uuid/next_id）；app/ 内对 portfolio_factor_usage"
    ".py 零引用（routes+tests 实际走 factor_usage_service.py:35）；原测试仅签名级（A1~A5 只验"
    " callable/参数名，0.76s 无 DB 路径）——挂账 P1 从『修生产坏点』改判为『半成品原型修复+测试升级』",
    "FK 强制开启实证（manager.py:105/session.py:93 PRAGMA foreign_keys=ON）：测试前置须造 "
    "FactorSet(id='1')——factor_set_id Integer 与 String PK 的 FK 靠 sqlite 父列亲和转换匹配；"
    "生产 MySQL 靠弱类型比较——两套 usage 表/类型错位已登记 P3 双轨整合专项",
    "selfcheck ALL OK（_td7_selfcheck3.txt）：TD7 5 writes（DoD② 测试按 C17 登记）+ 共享测试文件"
    "high_conflict_files 登记（C18）",
]
with open(PP, "w", encoding="utf-8") as f:
    json.dump(prog, f, ensure_ascii=False, indent=2)
    f.write("\n")

TP = ROOT / ".workbuddy/mining/tasks.json"
with open(TP, encoding="utf-8") as f:
    tk = json.load(f)
card = next(t for t in tk["tasks"] if t["id"] == "TD7")
card["status"] = "done"
card["finished_at"] = NOW
with open(TP, "w", encoding="utf-8") as f:
    json.dump(tk, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("TD7 closed at", NOW)
