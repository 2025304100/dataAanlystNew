# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T35 节（append-only）。"""
import io
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"
now = datetime.now().astimezone().strftime("%H:%M")

text = f"""

---

## T35 · 统计层 8 方法（M2 过拟合防线第二层）✅（{now}，35/47）

### 交付
- `app/services/factors/mining/statistical_tests.py`：8 方法总装 `compute_stats` →
  `contracts.StatResult`（11 字段冻结契约 import 复用，R3），
  写 `factor_evaluation_runs.metrics_json.stats` 不新建表
- `tests/.../test_statistical_tests.py` — **30 passed**（TDD：红→实现→3 failed 定性→全绿）
- 防波及：mining 全量 **1027 passed, exit 0**（5m25s，997 基线+30 新增）
- C5 扫描（_t35_c5_scan.py）：纯算法模块零业务耦合（仅 stdlib+numpy/scipy+contracts），
  全仓引用方仅测试文件（GA/评估链路消费方由后续卡接入）
- 收工三连：PROGRESS done @18:15:40 → selfcheck ALL OK → 看板再生 715 行 7 场景 ALL OK

### 8 方法口径（全部按设计 §7.9 实现路径唯一）
t 检验（scipy.ttest_1samp）/ Bonferroni 自研 min(p×N,1) / FDR(BH) 自研升序 p×n/rank
自后向前单调化 / Bootstrap 1000 次 ICIR 2.5%/97.5% 分位 / 置换逐列打乱时间轴 1000 次
（add-one p）/ DSR（Bailey & López de Prado，total_trials+偏度峰度，sr_variance 可注入）/ 
衰减率 test/train（<0.5 不得 B 级）/ WF 3 窗**只做方向计数不做跨窗合并检验**；
月频降级跳过 Bootstrap/置换/DSR/WF、degraded=True、分级最高 B。

### 3 个 failed 的测试侧教训（实现零 bug）
1. **DSR 惩罚效应素材**：observed_icir=2.5 过强 → Φ(z) 两端饱和 1.0，N=10 vs 5000
   的 E[max SR] 移动不可分辨 → 改边际 0.4 + 注入固定 sr_variance=0.01（隔离 Bootstrap
   波动）+ 防饱和断言（差值>0.1）。「效应量断言必须保证两端落区分区」。
2. **WF 常数段素材**：std 仅剩浮点误差 → ICIR 爆炸 7e15 且方向随机；且窗 1 test 段
   [176:200] 实际落第三正段（负段是 [80:160]）→ 改噪声素材 + 负段挪 [180:210] 对齐
   窗口切发布局。「常数段永远不能当 ICIR 素材；test 段区间要先手算」。
3. **确定性断言共享 rng**：kwargs 里同一个 Random(77) 被两次调用共享，s1 消费推进态
   污染 s2 → 各自新建。「确定性测试要隔离顺序消耗」。

### 环境坑（新增）
- 🚨 pytest 9 会话结束清理 basetemp 时**连 `.tmp` 父目录一起删**（下次重定向失败
  No such file or directory）→ 输出落盘与 basetemp 都放 `.workbuddy/mining/` 安全区。
- 收工脚本 f-string 里 `\\"` 会提前终止字符串（SyntaxError）→ py_compile 先行校验。
- 后台任务首跑失败不重试整条命令：先诊断（.tmp 消失根因），再换安全区重跑。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T35 section ->", p.name)
