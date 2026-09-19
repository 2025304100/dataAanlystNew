# -*- coding: utf-8 -*-
"""TD7 开工双登记：tasks.json 建卡 + PROGRESS.json 立条目（in_progress）。幂等。"""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\ai_project\dataAanlystNew")
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

TP = ROOT / ".workbuddy/mining/tasks.json"
with open(TP, encoding="utf-8") as f:
    tk = json.load(f)

if not any(t["id"] == "TD7" for t in tk["tasks"]):
    tk["tasks"].append({
        "id": "TD7",
        "phase": "TDB",
        "title": "P1 修复·portfolio_factor_usage 半成品双轨：snapshot varchar id 生成 + snapshot_id 契约 int→str",
        "deps": ["TD6"],
        "budget": "S",
        "writes": [
            "app/services/portfolio_factor_usage.py",
            "tests/test_wp02_factor_usage_and_fsm_tdd.py",
            "docs/技术债-全库schema漂移核对报告.md",
        ],
        "reads": [
            "docs/因子挖掘系统-系统设计文档-v2.0.md",
            "docs/因子挖掘系统-开发需求文档-落地版.md",
            "docs/技术债-全库schema漂移核对报告.md",
            "app/services/factor_usage_service.py",
            "app/models/decision_engine.py",
            "app/models/portfolio_factor_usage.py",
        ],
        "artifacts": [
            "save_and_apply_usage_atomic snapshot id 服务端生成（content_hash('ses',...) 现役同款）"
            "+ snapshot_id 契约 int→str（dataclass/重放/outbox 三处）",
            "test_wp02_factor_usage_and_fsm_tdd 新增行为级 3 用例（正常两表落数/幂等重放/A2 中途失败无副作用）",
            "报告 §7/§8 挂账定性修正：原『生产必 TypeError』不成立（生产走 factor_usage_service 健康实现），"
            "实为 WP02 原子 7 步 TDD 骨架实现（app/ 零引用）坏点未被任何行为级测试覆盖",
        ],
        "dod": [
            {"cmd": "{PY} -m pytest tests/test_wp02_factor_usage_and_fsm_tdd.py -q",
             "expect": "全绿（17 既有 + 3 新增行为级）"},
            {"cmd": "{PY} -m pytest tests/test_g1_factor_usage.py tests/test_backtest_detail_snapshot_contract.py -q",
             "expect": "不新增红（现役 factor_usage_service 路径防波及）"},
            {"cmd": "{PY} .workbuddy/mining/_td7_no_int_snap.py",
             "expect": "exit 0：portfolio_factor_usage.py 无 int(snap.id) / int(data[\"snapshot_id\"]) 残留"},
        ],
        "not_do": [
            "不切路由、不动现役 factor_usage_service（双轨整合另行排期）",
            "不动 decision_engine 模型与线上表结构",
            "不处理单/复数两套 usage 表的历史包袱（TD5 报告已另列）",
        ],
        "pitfalls": [
            "★ snapshot id 必须服务端确定性生成：content_hash('ses', snap_json, correlation_id)"
            "（现役 factor_usage_service.py:563 同款模式）；PK 是 varchar 无自增，勿造 int id",
            "★ snapshot_id 契约 int→str 三处联动：dataclass 字段 / 幂等重放 int(data['snapshot_id'])"
            " / outbox payload——漏重放路径则二次调用炸",
            "★ 行为级测试用 conftest db_session（auto-align 全表齐）；payload 按 docstring 宽松约定："
            "factor_set_id int 必填、factor_weights 必填、factor_model_run_id 可空",
        ],
    })
with open(TP, "w", encoding="utf-8") as f:
    json.dump(tk, f, ensure_ascii=False, indent=2)
    f.write("\n")

PP = ROOT / ".workbuddy/mining/PROGRESS.json"
with open(PP, encoding="utf-8") as f:
    prog = json.load(f)
tasks = prog.setdefault("tasks", {})
if "TD7" not in tasks:
    tasks["TD7"] = {"status": "in_progress", "agent": "agent-senior-dev", "started_at": NOW}
else:
    tasks["TD7"]["status"] = "in_progress"
with open(PP, "w", encoding="utf-8") as f:
    json.dump(prog, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("TD7 opened at", NOW)
