# -*- coding: utf-8 -*-
"""创建 2026-09-19.md 并追加 T37 节（新的一天，append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

header = """# 工作日志 · 2026-09-19

> 排期总表与裁决以 `.workbuddy/mining/PROGRESS.json` / `tasks.json` 为准；
> 前一日（09-18）记录见同目录 `2026-09-18.md`（当晚连续闭环 9 张卡，收尾于 00:18 的 T37）。

"""

body = """
## T37 · F1 列表页 + 等级标签 + 证据抽屉 ✅（00:18，45/47）—— **G6 门禁达成**

### 交付
- `result/GradeEvidenceDrawer.tsx`：**恰好 3 Tab**（定级证据 / 统计检验 8 项 / 血缘与来源，
  not_do「不做 4 Tab」）；Tab1 等级大标签 + 一句话理由 + 8 维度明细 + 阈值来源（默认/自定义）；
  Tab2 **月频时 Bootstrap/置换两行 `data-disabled=true` 灰掉并标注「样本不足，未计算」**
  + 顶部「月频最高 B 级」提示；Tab3 进化路径 + 经济逻辑；人工调整等级（原因 **≥10 字**强校验）
  → 提示「季度重评不会自动覆盖」+「恢复自动」；季度重评横幅（升级 up / 降级 down、
  移出 FactorSet 追加）+ 评级历史时间轴
- `config/MiningExperiencePage.tsx`：等级 chip 筛选 + 点击打开抽屉 +
  **D 级默认不进批量选择集**（「全选（不含 D 级）」）+ 等级变更 7 天「新」角标
- `test_grading.py` 新增 `TestEvidenceDrawerContract` **6 用例**（理由须点出等级、
  硬约束理由可解释、thresholds_source 透传、degraded/monthly 压 B、
  **D 级也有完整证据**、**「恢复自动」后季度重新接管**）
- 结果：DoD(G6) **58 passed**（52+6）；前端新增 **24 passed**；translations 3 passed；
  前端全量 **762 passed / 44 failed**（零引入）；后端 mining 全量 **1090 passed**；
  收工三连 done @00:18 → selfcheck ALL OK → 看板 699 行

### 观察与教训
- **writes 含既有测试文件**（`test_grading.py`/`test_statistical_tests.py`）→ 说明卡片的
  DoD 是**门禁抽样**（G6 = M2 算法全链路），交付重心在别处（本卡是前端 3 Tab）。
  遇到这种卡：先跑 DoD 基线（本次 52 passed 已绿），再把**本卡真正引入的能力**补成用例，
  不要为了"让 DoD 变绿"而写重复用例（月频降级基础用例 `test_monthly_degraded` 已存在，
  故只补抽屉契约相关的 6 条）。
- 又一次踩到 **Python 长中文字符串里的 ASCII 双引号**（`防"全灰"蒙混`）→ SyntaxError；
  **写收工脚本后先 `py_compile`** 已成必要动作（T38 踩 3 处、本次踩 1 处）。
- 前端卡 writes 漏 i18n **第七张**（T27 有 / T28~T37 连补六张）→ 见下「待办」。

### G6 与剩余门禁
- **G6（M2 算法：选择/繁殖/统计/分级全链路）** 抽样通过 ✅
- **G5（5 步向导可点通到结果页）仍未闭环**：5 步组件与结果页都已交付并有测试，
  但向导容器 `MiningShell.tsx`（T27 写权限）尚未接线 —— 需一次容器装配动作。

### 剩余 2 张卡
| 卡 | 内容 | 状态 |
|---|---|---|
| T39 | 拆分 Settings.tsx（M） | DoD 要求 `npm test` **全绿**，被既有 44 项红阻断，需先决策 |
| T40 | en-US 补齐（M） | 依赖已满足，低风险 |
"""

if not p.exists():
    p.write_text(header, encoding="utf-8")
    print("created", p.name)
with p.open("a", encoding="utf-8") as f:
    f.write(body)
print("appended T37 section ->", p.name)
