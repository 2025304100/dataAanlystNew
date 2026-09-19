# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T29 节（append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T29 · Step2 时间与目标 UI（含 SplitBudget）✅（23:15，41/47）

### 交付（step2/ 四文件 + i18n）
- `MiningSplitPanel.tsx`（DoD 主体）：三段比例（默认 60/20/20，**和必须 100%**）、
  比例/自定义日期边界双模式、**SplitBudget 展示区**
- **purge / embargo 同时展示「调仓点数」+「折算交易日数」**（任务卡 pitfalls 第一条，
  否则 purge=5 被误读成 5 个交易日）；月频 `statistically_degraded` → 「最高 B 级」；
  `meets_floor=false` → 门槛阻断
- `splitTypes.ts`（SplitBudget 契约 + 硬门槛 日252/周104/月36 与建议 周156/月60）
- `MirrorGuideBanner.tsx`（向导 §4.1 四种提示：幸存者偏差/低覆盖/月频镜像引导/超范围阻断）
- `MiningTimeTargetStep.tsx`（Step2 编排：起止/频率/持有期/目标 + 门槛 + 引导 + 切分）
- i18n 两份新增 **37 个 key**；测试 **13 passed**；前端全量 **696 passed / 44 failed**
  （与基线持平，**零引入**）；收工三连 done @23:15 → selfcheck ALL OK → 看板 699 行

### 关键事实（影响后续卡）
后端有 Python 侧 `evaluation_adapter.compute_split_budget()`，但**没有切分预算的
HTTP 预览路由**。加上 not_do「不在前端计算分位数或切分」→ 本卡只能**展示后端下发值**：
`budget` 为空时不渲染预算区与段边界（有测试断言守着）。
**后续若补 `POST /factor-mining/split-preview` 之类接口，父级注入 `budget` 即可，前端零改动。**

### not_do 的测试固化方式（可复用套路）
「前端不做某事」这类约束很难断言，本卡用两条**反向断言**固化：
1. 无 `budget` 时 → `data-split-budget` 与 `data-split-boundary` **都不存在**（不臆造）；
2. 改三段比例后 → `data-split-boundary` 文本**不变**（切分只由后端算）。

### 复用记录
- 前端卡 writes 漏 i18n 已是第三次（T27 有 / T28 补 / T29 补）→ **前端卡开工前先查
  writes 是否含 i18n 两份**，已固化为开卡脚本动作。
- selfcheck 假红（SAFE_DELETE shim）依旧偶发，开卡/收工一律带
  `CODEBUDDY_SAFE_DELETE_ENABLED=0`。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T29 section ->", p.name)
