# -*- coding: utf-8 -*-
"""T35 收工：PROGRESS.json T35 -> done + artifacts/evidence（防波及数字实时读取）。"""
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

# ── 防波及结果解析（必须 exit 0 才允许收工）──────────────────────────
sweep_txt = (ROOT / ".workbuddy/mining/t35_sweep.txt").read_text(encoding="utf-8")
m_exit = re.search(r"EXIT=(\d+)", sweep_txt)
m_pass = re.search(r"(\d+) passed", sweep_txt)
m_fail = re.search(r"(\d+) failed", sweep_txt)
exit_code = int(m_exit.group(1)) if m_exit else -1
passed = int(m_pass.group(1)) if m_pass else -1
failed = int(m_fail.group(1)) if m_fail else 0
assert exit_code == 0 and failed == 0 and passed > 0, (
    f"防波及未全绿，禁止收工：exit={exit_code} passed={passed} failed={failed}")

ppath = ROOT / ".workbuddy/mining/PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t35 = prog["tasks"]["T35"]
assert t35.get("status") == "in_progress", f"T35 状态异常: {t35.get('status')}"

t35["status"] = "done"
t35["finished_at"] = NOW
t35["artifacts"] = [
    "app/services/factors/mining/statistical_tests.py（M2 统计层 8 方法，设计 §7.9 /"
    "需求 §3.2 / 开发 §3.17）：①t 检验 scipy.stats.ttest_1samp(ic,0)；"
    "②Bonferroni 自研 p_adj=min(p×total_trials,1)（<0.001 判过）；③FDR(BH) 自研"
    " p 升序 p×n/rank 自后向前单调化（q<0.1 判过，q≥p 数值安全 clip）——**实现路径唯一**"
    "不做双路径开关；④Bootstrap 有放回 1000 次 → ICIR 2.5%/97.5% 分位；⑤置换检验逐列"
    "打乱时间轴 1000 次重算 ICIR 经验分布（add-one p，截面有效样本<3 跳期）；"
    "⑥DSR（Bailey & López de Prado 2015：total_trials + IC 偏度/峰度校正，E[max SR]"
    " 欧拉常数公式，trial 间 SR 方差缺省 Bootstrap 估计、可注入）；⑦衰减率 test/train"
    " ICIR（<0.5 疑似过拟合不得 B 级以上，test 反向负值照实）；⑧Walk-Forward 3 滚动窗"
    "（步长=总长/窗口数、窗长=一半、每窗内后 20% 算 ICIR）**只做方向一致性计数不做跨窗"
    "合并检验**（重叠窗口非独立样本，合并高估显著性）；总装 compute_stats →"
    " contracts.StatResult 11 字段冻结契约 import 复用（R3），写"
    " factor_evaluation_runs.metrics_json.stats 不新建表；月频降级（§7.9.4）跳过"
    " Bootstrap/置换/DSR/WF 仅保留 t 检验参考值 degraded=True 分级最高 B；NaN/None"
    " 语义（不许归 0）由落库链路 clean_json_tree（TD3）统一转换",
    "tests/services/factors/mining/test_statistical_tests.py：30 用例"
    "（BH 经典手算向量+单调性+q≥p、Bonferroni 封顶、t 检验显著/噪声双向、Bootstrap"
    " 区间含点估计+固定种子复现、置换强信号低 p/噪声高 p+复现、DSR 概率域+试验数惩罚"
    "效应+质量/噪声分离、衰减率含负值与无效输入 NaN、WF 全正/混合多数方向/无跨窗合并"
    "字段/过短空返、compute_stats 日频全路径/月频降级断言/total_trials=1 恒等/"
    "同种子确定性/StatResult 经 clean_json_tree 后 allow_nan=False 序列化含"
    " \"decay_ratio\": null）",
    "requirements.txt：scipy>=1.13（M1a 已显式声明）注释补 M2 统计层 8 方法直接消费方"
    "声明（t 检验/偏度/峰度/正态分位），版本值不变",
]
t35["evidence"] = [
    f"DoD pytest tests/services/factors/mining/test_statistical_tests.py -q →"
    f" **30 passed, exit 0**（TDD 全程：红（ModuleNotFoundError）→ 实现 → 3 failed"
    f" 测试侧定性修复 → 全绿；3 处均为测试素材/代码问题而非实现 bug："
    f"①DSR 惩罚效应素材 observed_icir=2.5 过强 → Φ(z) 两端饱和 1.0，改边际水平 0.4"
    f"+注入固定 sr_variance=0.01 隔离 Bootstrap 波动、加防饱和断言差值>0.1；"
    f"②WF 混合方向常数段素材 std 仅剩浮点误差 → ICIR 爆炸 7e15 且方向随机，且窗 1"
    f" test 段[176:200] 实际落第三正段——改噪声素材并把负段挪到[180:210] 对齐窗口"
    f"切发布局（窗长=120/步长=80/每窗取后 20%）；③同种子确定性断言共享同一 rng 对象"
    f"（s1 消费推进态污染 s2）——改两次调用各自 random.Random(77)）",
    f"防波及：pytest tests/services/factors/mining/ 全量 → **{passed} passed,"
    f" exit {exit_code}**（含本卡新增 30 用例；warnings 均为既有 SAWarning/"
    f"Deprecation 非本卡引入）",
    "C5 红线扫描（_t35_c5_scan.py）：statistical_tests.py 仅 import stdlib+numpy/"
    "scipy+contracts.StatResult（R3 契约交互合规），factor_experience/F1 model/"
    "SQLAlchemy/pandas 零触碰；全仓引用方仅测试文件自身（GA/评估链路消费方由后续卡"
    "接入，符合排期单向依赖设计）",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/迁移编号不占用/无重复键；T35 与"
    "相邻卡无写冲突）",
    "纯算法卡零 DB 依赖：无迁移、无模型、无路由——不触碰 MySQL/duckdb（S/R 审计不适用）；"
    "存储口径实测合规：metrics_json.stats 走 clean_json_tree 后 allow_nan=False 可序列化",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS T35 -> done @", NOW)
print(f"sweep: {passed} passed, exit {exit_code}")
