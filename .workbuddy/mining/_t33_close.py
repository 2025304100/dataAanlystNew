# -*- coding: utf-8 -*-
"""T33 收工：PROGRESS.json T33 -> done + artifacts/evidence。"""
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
t33 = prog["tasks"]["T33"]
assert t33.get("status") == "in_progress", f"T33 状态异常: {t33.get('status')}"

t33["status"] = "done"
t33["finished_at"] = NOW
t33["artifacts"] = [
    "app/services/factors/mining/selection/multi_objective.py（B1 NSGA-II）："
    "目标池固定 6 目标方向锁死（icir/coverage↑ turnover/complexity↓ + oos_stability/"
    "monotonicity 可选）、启用数 3~5 校验、min 方向取负统一「越大越好」、缺失/NaN 恒 -inf "
    "（统一方向后最差，不乘 sign 防翻转）、dominates 逐维判（无需权重不要求量纲）、"
    "朴素 O(mN²) 非支配排序、拥挤度 CD（两端极值=inf、极差 0/非有限该维贡献 0 防除零 NaN）、"
    "rank_population 稳定排序 (rank 升, -cd 降)、better_than 比较算子"
    "（rank 优先、同 rank 比 CD 降——永不 为多样性选更差因子）",
    "app/services/factors/mining/selection/track.py（A1 赛道竞争）：6 赛道沿 CATEGORY_PRIORITY、"
    "active_tracks 只认有存活个体（空赛道直接关闭不占保底）、update_weak_streaks "
    "（最末层 rank≥种群最大 且 ICIR<预筛门槛 → streak+1，恢复达标即清零，连续 W=3 判弱）、"
    "compute_quotas 动态配额（保底 floor(S×0.6/|C|)+弱赛道减半转出+动态按最优个体质量加权"
    "（rank 优先 ICIR 次之线性位次权）+配额上限=存活数、超出回流迭代重分、Σ配额==S 守恒）、"
    "category_evenness=(1−HHI)/(1−1/n) 均衡度（D3 反馈输入，落 generations 表）",
    "app/services/factors/mining/selection/tournament.py（B2 锦标赛）：K=3 默认 2~7 越界拒绝、"
    "有放回抽组（允许同一被重复抽中——K 大偏收敛极限即全局 Top）、组内严格按 B1 钥匙取最优、"
    "rng 注入固定种子可完全复现（需求验收第 15 条）；精英不参与=调用方传入前剔除（docstring 注明）",
    "selection/__init__.py：三模块公开 API 统一导出（A1/B1/B2 职责互不重叠，单向信号流）",
    "tests/services/factors/mining/test_selection.py：38 用例"
    "（specs 校验/归一与支配/分层/CD 手算/比较钥匙/A1 配额含 50 组随机守恒性质/"
    "弱赛道三连清零恢复/均衡度边界/B2 受控 rng 确定性+统计压力+固定种子复现/B1→A1→B2 端到端 smoke）",
]
t33["evidence"] = [
    "DoD pytest tests/services/factors/mining/test_selection.py -q → **38 passed, exit 0**"
    "（TDD 全程：红（ModuleNotFoundError）→ 实现 → 15 failed 双向定性修复 → 全绿；"
    "实现 2 bug：NaN 归一后被 min 方向 sign 二次翻转成最优、nondominated_sort 把已归一向量"
    "再传 dominates 致 min 方向双重取负；测试 5 处设计错误：支配素材互不支配写错、"
    "期望序按 counts 插入序非 TRACKS 序、假赛道名 A/B/C 不在 6 类池、weak 首代即断言弱、"
    "锦标赛概率断言非确定改受控 _SeqRng）",
    "防波及：pytest tests/services/factors/mining/ 全量 → **997 passed, exit 0**（5min11s，"
    "warnings 均为既有 SAWarning/Deprecation 非本卡引入）",
    "C5 红线扫描（_t33_c5_scan.py）：selection/ 三新文件对 factor_experience/F1 model 零引用；"
    "mining/ 全目录仅既有文件 import 自有 factor_mining/factor_model（合法，不在红线范围）",
    "selfcheck ALL OK（47 任务依赖图无环/写权限声明/迁移编号不占用/无重复键；T33 与 T34 "
    "selection/ vs reproduction/ 无写冲突）",
    "纯算法卡零 DB 依赖：无迁移、无模型、无路由——不触碰 MySQL/duckdb（S/R 审计不适用）",
]
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS T33 -> done @", NOW)
