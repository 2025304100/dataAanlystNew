# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T34 节（append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T34 · 繁殖自适应 C1/C2/C3/D1/D2/D3 ✅（21:20，37/47）

### 交付（6 模块 + 35 用例）
- `reproduction/scheduler.py`（**C1 唯一调参者**）：五状态调节表（early/mid/late/
  stall_rescue/fast_progress）；**阶段态由代数直通（不算防抖对象）、异常态连续 2 代
  才切**；单步 ≤0.10 逼近；三率限步→夹 GUARD→`normalize_rates` 归一（和恒为 1）；
  输出 `contracts.Strategy`
- `diversity.py`（D3）：三层合成 health（基因型 `1-structural_similarity` 两两均值、
  表现型 ICIR 归一化极差、类型层 category_evenness 由调用方传）；**不返回任何率**
- `convergence.py`（D1）：stall = 连续 |delta|<0.01；**先自救（≥2）后停止（≥3）**；
  不含任何率字段
- `reproduction/mutation.py`（C2 四变异）+ `crossover.py`（C3 子树交换）+
  `injection.py`（D2 复用 random_generator）
- DoD **35 passed**；防波及 **1062 passed / 0 failed**（7m32s）
- 收工三连：PROGRESS done @21:20 → selfcheck ALL OK → 看板 714 行

### 实现侧 1 处 bug（修复）
`_mutate_field` 原要求「源字段也在 allowed 池内才替换」→ `ts_mean(close,5)` +
allowed=(open,volume) 时**静默原样返回**。正确语义：源字段取公式实际用到的字段、
**只有目标**必须在已选域内（变异的目的是把公式拉回合法字段域）。

### 关键设计裁决（写进代码注释）
- 阶段态（early/mid/late）**不走滞回**（由代数确定，不是"调节"，否则首代永远
  切不到 early）；异常态（stall_rescue/fast_progress）才需连续 2 代。
- 三率归一顺序：限步 → 夹区间 → 归一（交叉率 = 1−变异−注入；越界则回补变异/注入，
  兜底按比例缩放）。

### 坑（新增）
- 🚨 收工脚本解析 pytest 汇总：全绿时汇总行为「N passed, M warnings」**不含 failed**，
  且要取**末尾**匹配（`re.findall(...)[-1]`），否则炸 None 或抓到单文件行数。
- 🚨 Python 三元优先级：`a, b if cond else (x,y)` 只对 b 生效，a 会先求值 →
  条件为假时 AttributeError（本次 m_total 为 None 即踩中）。
- 侦查脚本用 ast.unparse 取默认参数：默认值在 `args.defaults`（尾部对齐），
  **不在 arg 对象上**（arg.default 不存在）。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T34 section ->", p.name)
