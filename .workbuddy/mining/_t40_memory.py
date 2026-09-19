# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 T40 节。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## T40 · en-US 补齐 ✅（00:59，46/47）—— **前置完成 + 证明型收工**

### 结论
T40 的 artifacts「en-US 与 zh-CN key 集合一致」**开工前就已达成**：
我在 T28~T37 每张前端卡都同步维护两份（并已用 `_card_i18n_autofix.py` 固化为
写权限层面的强制），所以本卡**无缺口可补、代码改动为零**。

### 证明（运行时探针，非正则扫源码）
临时测试 `__probe_t40.test.ts`（**用后即删**，证据 JSON 保留在
`.workbuddy/mining/t40_probe_result.json`）实测：
- **zh-CN 5060 键 = en-US 5060 键**，`onlyZh` / `onlyEn` 差集**均为空**；
- zh-CN 无空值；
- en-US 中唯一含中文字符的值是 `langZhCN = \"简体中文\"` —— 属**语言选择器的母语自称**，
  是正确设计而非未翻译残留（已在收工脚本里白名单化并断言其余为 0）。

### 验证
- DoD（括号内注明目标为 `translations.test.ts`）→ **3 passed, exit 0**；
- 前端全量 **791 passed / 22 failed**（失败 22 vs 基线 22、文件 7 vs 7）→ **零引入**；
- 收工三连 done @00:59 → selfcheck ALL OK → 看板 695 行。

### 有价值的副产物：**「DoD 括号内写真实目标」这一细节救了本卡**
T40 的 DoD 命令字面是 `npm test` expect exit 0，但括号注明了「（translations.test.ts 绿）」。
若只看命令，本卡会因**同 T39 一样被既有 22 项 C 类红阻断**而无法收工；
按括号内意图执行（且全量对比基线证明零引入）才是正确的读法。
> 建议：后续卡片把 DoD 写成 `npm test -- <pattern>`（如 T32 的 `FactorMining`）或
> 显式写「不新增失败」，避免「全量绿」这种在既有红存在时不可达的门槛。

### 进度
- 排期卡 **46/47**，仅剩 **T39（拆分 Settings.tsx）**；
- T39 的 DoD 要求 `npm test` 全绿 → **须先完成 `TD-FE-RED-2`（C 类 22 项定性）**。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T40 section ->", p.name)
