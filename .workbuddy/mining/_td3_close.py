# -*- coding: utf-8 -*-
"""TD3 收工登记：PROGRESS.json TD3 -> done + artifacts + evidence + observation。幂等。"""
import json
from datetime import datetime

PPATH = r".workbuddy/mining/PROGRESS.json"

ARTIFACTS = [
    "app/core/db_numeric.py（新增 clean_json_tree：递归清洗 JSON 形值树——dict/list/tuple 逐层展开，NaN/±Inf→None（D-F/R33 口径，不许归 0），超 MYSQL_FLOAT_MAX→None，1e999 这类合法字面量溢出为 inf 的值一并兜底；int/bool/str/None 原样（int **不转 float**——转 float 会把 7 变 7.0 且大整数溢出，首版真踩到已修）；set 不处理保持原语义。clean_numeric_fields 只洗顶层标量，而 wp5 的 stress_test 子 dict 实测有嵌套，故递归版独立存在）",
    "写入侧 5 点统一改造（均 json.dumps(clean_json_tree(metrics), …, allow_nan=False) 双保险）：factor_evaluator.py finalize_evaluation_run:181、wp5_eval_task.py:2174 压力指标合并（嵌套 stress_test 覆盖）、factor_shadow.py:148 record_shadow_observation、ridge_model.py:947 训练落库、api/routes/factor_models.py:398 离线最小训练",
    "tests/services/factors/mining/test_metrics_json_hygiene.py（12 用例哨兵：clean_json_tree 单元递归/有限值保真/超界→None 不归 0/numpy 标量归一/严格 dumps；json.loads(parse_constant=reject) 严格解析判定器——NaN/Infinity/-Infinity 字面量出现即炸；finalize_evaluation_run 与 record_shadow_observation 两个真实写入点端到端（FK 父行 seed）；5 写入点文件文本级守卫——clean_json_tree( + allow_nan=False + dumps 首参必须是清洗结果（正则跨行命中），防回退/新点漏配）",
    ".workbuddy/mining/_backfill_metrics_json_nan.py（--dry-run 只读报告 / --apply 清洗+复扫；判定用 json.loads(parse_constant=reject) 严格模式（**不用正则改写**），修复=parse_constant→None 重解析 + clean_json_tree 兜底重序列化；非法 JSON 坏行单独计数不修改；幂等可重跑）",
]

EVIDENCE = [
    "DoD① pytest tests/services/factors/mining/test_metrics_json_hygiene.py -q → 12 passed in 8.23s（EXIT=0；_td3_hygiene_run3.txt）",
    "DoD② --dry-run → 三张表（factor_evaluation_runs / factor_shadow_observations / factor_model_runs）存量 NaN/Infinity 字面量 0 行、invalid_json 0（EXIT=0；_td3_backfill_dryrun.txt）——非标准 JSON 是「潜在风险」而非「已发生」：历史写入值均为有限数，本卡价值在写入侧防复发 + 前端解析安全",
    "DoD③ --apply → EXIT=0、复扫 0 行（幂等可重跑；_td3_backfill_apply.txt）；另做 _clean_row 函数级语义验证：'{\"a\": NaN, \"b\": [1, Infinity, -Infinity], \"d\": 1e999}' → '{\"a\": null, \"b\": [1, null, null], \"d\": null}'，严格重解析无字面量残留",
    "读取侧兼容侦查（pitfall②，2026-09-18）：null 读回 None 与历史 NaN 读回 float('nan') 的行为差异逐点核对——__facade__._as_float 对 NaN **不过滤**（NaN 透传 DTO → FastAPI 响应序列化 NaN 字面量 → 前端 JSON.parse 炸），null→None 反而是修复；_fo 显式 f==f→None 等价；ridge_model IC 对比分支 NaN 与 None 均走「跳过比较」等价；factor_evaluator 复用路径只取日期字符串；wp5 读回合并重写 None 无影响。结论：**全部读取方 null 兼容且多数改善**",
    "跨域回归批1（whitebox+mining 29 文件覆盖集 14 项，23m13s）：13 failed, 1261 passed（EXIT=1；_td3_regress_b1.txt）",
    "跨域回归批2 串行（integration+路由+wp5 件 10 项，3m06s）：6 failed, 93 passed（EXIT=1；_td3_regress_b2_solo.txt）；integration e2e 全绿（TD2 收工 27/27 基线保持）",
    "13+6 失败全部定性为既有、与 TD3 无关，证据链三条：① wp5 6 失败还原对照——临时还原 factor_evaluator.py 的 2 处 TD3 改动后 TestTimeSplit+TestRunEvaluation 同样 6 failed 名单一致（_td3_fe_baseline.txt），已恢复（哈希比对一致）；② stage3 6 失败还原对照——还原 factor_models.py 2 处改动后同样 6 failed（_td3_stage3_baseline.txt），已恢复；③ ridge 7 失败同族形态（样本不足→训练 rejected→断言 validated 失败）且批1 全文 allow_nan/Out of range/ProgrammingError 异常 0 次。根因=build_time_split 自身默认 min_val_days=50 → derived_min_total=300，拒绝测试的 100/80 点小样本；SPLIT_MINIMUMS 仍是 D-I 三元组 (252,40,40)，evaluation_adapter.py mtime=2026-09-18 09:23（TD3 开工 13:59 之前的动作，非本卡改动）。已上报观察待裁决",
    "环境坑（已记 MEMORY）：大批回归**必须串行**——批1/批2 并行时 e2e 撞 DuckDB 独占锁（factor_warehouse.duckdb 被 .venv python 进程持有 → IOException 500），串行复跑 e2e 全绿；另 Grep/Glob 显示路径可能丢中间层级（显示 .workbuddy\\tasks.json 实为 .workbuddy\\mining\\tasks.json），定位文件用 os.walk 实证",
]

OBSERVATION = (
    "📋 待裁决（TD3 回归定性，既有非引入）：**切分地板 vs 旧测试样本 13+6 失败**——"
    "factor_evaluator.build_time_split 自身默认 min_val_days=50 → derived_min_total=300，"
    "拒绝 test_whitebox_wp5_evaluation(6)/test_whitebox_ridge_model(2)/test_whitebox_wp7_*(5)/"
    "test_stage3_factor_binding_contract(6，签名错配形态：Session 当 request 传+409vs422) 的小样本（100/80 点）。"
    "裁定方向：(a) 旧测试样本扩到 ≥300 点对齐新地板；(b) 测试显式传低地板参数保留小样本快测意图；"
    "(c) 确认 50 是否应为 40（D-I 定值）——evaluation_adapter mtime 2026-09-18 09:23 早于 TD3 开工，"
    "谁改的需查当日日志。另挂账：**大批回归必须串行**（并行撞 DuckDB 独占锁 e2e 假红）。"
)

with open(PPATH, encoding="utf-8") as f:
    data = json.load(f)

tasks = data.get("tasks", {})
td3 = tasks.get("TD3")
if td3 is None:
    # 开工登记漏项补齐：从任务卡取 started_at（PROGRESS 条目应在开工时建立）
    with open(r".workbuddy/mining/tasks.json", encoding="utf-8") as f:
        card = [t for t in json.load(f)["tasks"] if t["id"] == "TD3"][0]
    started = card.get("started_at")
    assert started, "TD3 card has no started_at"
    td3 = {"status": "in_progress", "agent": "agent-senior-dev",
           "started_at": started}
    tasks["TD3"] = td3
    print("TD3 entry created in PROGRESS (started_at =", started + ")")

if td3.get("status") == "done" and any("DoD① pytest" in e for e in data.get("evidence", [])[-20:]):
    print("TD3 already closed, skip (idempotent)")
else:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    td3["status"] = "done"
    td3["finished_at"] = now
    td3["artifacts"] = ARTIFACTS
    td3["evidence"] = EVIDENCE
    data.setdefault("observations", []).append(OBSERVATION)
    with open(PPATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    # 回读校验
    with open(PPATH, encoding="utf-8") as f:
        chk = json.load(f)
    t2 = chk["tasks"]["TD3"]
    assert t2["status"] == "done" and len(t2["evidence"]) == len(EVIDENCE)
    assert chk["observations"][-1] == OBSERVATION
    print("TD3 -> done @", now, "| evidence:", len(EVIDENCE), "| artifacts:", len(ARTIFACTS))
