# -*- coding: utf-8 -*-
"""T26 收工：PROGRESS.json T26 -> done + artifacts/evidence。"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

ppath = ROOT / ".workbuddy/mining/PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t26 = prog["tasks"]["T26"]
assert t26.get("status") == "in_progress", f"T26 状态异常: {t26.get('status')}"

t26["status"] = "done"
t26["finished_at"] = NOW
t26["artifacts"] = [
    "4 张表落地（迁移 0058 wps_0023_058_f1_experience_tables，显式 InnoDB、无 FK 照设计、"
    "索引名与 ORM metadata 一致：fingerprint 唯一 + (category,status) + (source,success_rate) "
    "+ 子表 experience_id/tags 复合索引；真实库已 upgrade head 应用 056+058）",
    "app/models/factor_experience.py：FactorExperience 主表 19 列 + Tags/Metrics/FieldDeps 3 子表"
    "（models/__init__ 自动扫描注册，无需改聚合层）",
    "app/services/factors/experience/：generalization（AST 数值泛化——只泛化 Call 参数位置，"
    "算术操作数是结构常数保留字面，与开发文档 §3.12 示例严格一致；instantiate 回填）、"
    "fingerprint（三层指纹=结构哈希+算子/字段统计+语义桶，全部确定性；complexity_profile、"
    "infer_category 启发式分类）、service（store_experience 四表同事务/指纹幂等去重、"
    "sample_experiences 字段硬过滤+场景打分 2*env+2*pool+success_rate+icir+加权不放回抽取+"
    "参数实例化+use_count 回写、related_experiences）",
    "app/schemas/factor_experience.py + app/api/routes/factor_experience.py + router.py 挂载："
    "3 接口 POST /factor-experience、GET /factor-experience/sample、GET /factor-experience/related"
    "（related 本期实现 F2 才调用）",
    "tests/services/factors/experience/ 5 文件 57 用例（泛化/指纹/存回/抽取/HTTP 全覆盖）",
]
t26["evidence"] = [
    "DoD pytest tests/services/factors/experience/ -q → **57 passed, exit 0**（_t26_dod5.txt；"
    "首跑 11 failed 两大根因：①泛化规则初版把算术操作数 -1 也泛化，与文档示例『-1 保留』冲突——"
    "修 generalization 只泛化 Call 参数位置；②测试造数换 window 同构撞指纹去重——改异构字段造数）",
    "迁移双向验证（_t26_mig_verify.py，scratch sqlite）：upgrade head 4 表+7 索引+主表 19 列与 ORM 对齐，"
    "downgrade -1 四表干净消失",
    "真实库应用：alembic current 由 wps_0023_055 推进至 head（056 TD6 幂等跳过 + 058 新建 4 表，"
    "_t26_upgrade_real.txt）；verify_schema_drift --tables 4 表 → **无漂移，引擎 INNODB**"
    "（_t26_drift2.txt；首跑 charset WARN——迁移显式 utf8mb4 ≠ 库默认 utf8 与既有表惯例冲突，"
    "去掉 mysql_charset 重建后消除）",
    "真实库写入试探（_t26_real_probe.py，db_numeric P0）：store_experience 全链路写 gpfx，"
    "NaN icir → 库中 None（未归 0）、coverage 0.95 保真、4 表同落、sample 场景命中、清理零残留",
    "C5 红线静态扫描：app/services/factors/mining/** + mining/ 对 factor_experience/FactorExperience "
    "零引用（挖掘只经 HTTP 调 F1）",
    "防波及：import app.main OK 且 3 条 /api/v1/factor-experience* 路由挂载在位；"
    "tests/test_g1_factor_usage.py + mining/test_submit_candidate.py → 26 passed 零波及",
    "selfcheck ALL OK（开工登记：T26 writes 补 tests/services/factors/experience/ 满足 C17 + "
    "granularity_exempt 豁免第 9 产物满足 C12）",
]
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS T26 -> done @", NOW)
