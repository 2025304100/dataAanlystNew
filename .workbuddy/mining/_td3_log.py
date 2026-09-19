# -*- coding: utf-8 -*-
"""TD3 完成段追加到每日日志（幂等：## TD3 完成： 标记判重）。"""
import json
import pathlib

LOG = pathlib.Path(r".workbuddy/memory/2026-09-18.md")
MARK = "## TD3 完成："

body = """
## TD3 完成：metrics_json NaN 字面量合规化（2026-09-18 14:47）

**改动**
- `db_numeric.clean_json_tree`（递归清洗：dict/list/tuple 展开、NaN/±Inf→null、
  超 3.4e38→None、1e999 溢出 inf 兜底；int **不转 float**——首版把 7 变 7.0 真踩到已修）。
- 5 写入点统一 `json.dumps(clean_json_tree(m), …, allow_nan=False)` 双保险：
  factor_evaluator:181 / wp5:2174（嵌套 stress_test）/ factor_shadow:148 /
  ridge_model:947 / factor_models:398。
- 新增 `test_metrics_json_hygiene.py` 12 用例（严格解析判定器 parse_constant=reject、
  2 写入点端到端、5 文件文本级守卫防回退/新点漏配）。
- `_backfill_metrics_json_nan.py`（dry-run/apply；**判定不用正则**——parse_constant
  钩子只在 NaN/Infinity/-Infinity 出现时被调用；坏行单独计数不修；幂等复扫）。

**DoD**：① hygiene 12 passed/exit 0；② dry-run 三表存量 0 行（NaN 字面量是潜在风险
非已发生——价值在写入侧防复发+前端安全）；③ apply exit 0+复扫 0。
**读取侧侦查**：null 全路径兼容且多数是修复（_as_float 不过滤 NaN→透传 DTO→前端炸，
null 反而救了它）。
**回归**：批1 whitebox+mining 1261 passed+13 既有失败；批2 串行 93 passed+6 既有失败；
TD3 相关零异常（allow_nan/越界 0 次）。

**教训**
- 13+6 既有失败靠「还原对照」定性：临时还原本卡 2 处改动重跑失败集，名单一致=既有
  实锤，再从备份恢复（哈希比对）。比考古谁改的快得多。
- **大批回归必须串行**：批1/批2 并行 → e2e 撞 DuckDB 独占锁（factor_warehouse.duckdb
  被批1 进程持有）→ IOException 500 假红；串行复跑全绿。
- Grep/Glob 显示路径可能**丢中间层级**（显示 .workbuddy\\tasks.json，实为
  .workbuddy\\mining\\tasks.json）——照显示路径 open 会炸，用 os.walk 实证。
- PROGRESS.json 的 tasks 是「开工时加入」——开工登记要同时写 tasks.json（卡）与
  PROGRESS.json（条目），漏了会在收工时断言炸（本卡收工脚本已做补齐兜底）。
- 13 失败根因挂裁决：build_time_split 自身默认 min_val_days=50→derived 300 拒
  100/80 点小样本；SPLIT_MINIMUMS 仍是 D-I (252,40,40)；evaluation_adapter mtime
  今天 09:23（上会话动作）——50 与 40 的矛盾待需求方裁定。
"""

if MARK in LOG.read_text(encoding="utf-8"):
    print("already logged, skip (idempotent)")
else:
    txt = LOG.read_text(encoding="utf-8")
    LOG.write_text(txt.rstrip("\n") + "\n" + body, encoding="utf-8")
    chk = LOG.read_text(encoding="utf-8")
    assert chk.count(MARK) == 1, "marker count != 1"
    assert chk.endswith("待需求方裁定。\n") or chk.rstrip().endswith("待需求方裁定。")
    print("TD3 完成段已追加，日志行数：", len(chk.splitlines()))
