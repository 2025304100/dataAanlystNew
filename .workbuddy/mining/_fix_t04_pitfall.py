"""修复 T04 的沙箱说明（上一次经 `bash -c` 写入时反引号被 shell 当作命令替换吞掉了）。

状态：**已执行完毕（2026-09-16）**，可安全删除。

教训：**不要把含反引号的字符串经 `bash -c` 传给 Python** —— shell 会先做命令替换。
凡是要写入含 markdown 反引号的文本，一律写成 .py 文件再执行。
"""
from __future__ import annotations

import json
import pathlib

TASKS = pathlib.Path(".workbuddy/mining/tasks.json")

BROKEN_PREFIX = "✅ 沙箱注册与 T03 的 cs_ **不同**（SD-v2.0 §12.3 D-A）"

FIXED = (
    "✅ 沙箱注册与 T03 的 cs_ **不同**（SD-v2.0 §12.3 D-A）：裁决是「cs_ 在单标的沙箱中登记但拒绝」，"
    "因为截面算子需要面板上下文。但 ts_* 有**真实的单标的语义** —— "
    "ts_delta_bars(close, 20) 在逐标的序列上就是 close[i] - close[i-20]，"
    "ts_atr 同理可逐标的算出。因此 T04 必须给 ALLOWED_FUNCS 提供**真正可用的实现**，"
    "不要照抄 T03 的拒绝模式（那会让高级筛选里所有 delta/atr 公式直接报错，"
    "而这两个算子正是需求 §6.0 说「25 模板中 10 个不可行」的根因）。"
    "注意 ts_delta_periods 在单标的 1D 序列上无法区分「报告期」与「交易日」——"
    "沙箱侧只能按位置做 N 期差分，报告期语义由因子流水线（factor_executor）保证。"
)


def main() -> int:
    doc = json.loads(TASKS.read_text(encoding="utf-8"))
    task = next(t for t in doc["tasks"] if t["id"] == "T04")

    hits = [i for i, p in enumerate(task["pitfalls"]) if p.startswith(BROKEN_PREFIX)]
    if not hits:
        print("未找到待修复条目 —— 可能已修复，或无变更")
    for i in hits:
        print(f"  替换 pitfalls[{i}]（原长 {len(task['pitfalls'][i])} 字符）")
        task["pitfalls"][i] = FIXED

    # 校验：不得再有被 shell 吞掉留下的空引号痕迹
    problems = [p for p in task["pitfalls"] if "（ 在逐标的" in p or "在逐标的序列上就" in p]
    print(f"  残留破损条目: {len(problems)}")

    TASKS.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print()
    print("=== T04 pitfalls 复核 ===")
    for p in task["pitfalls"]:
        print(f"  - {p[:150]}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
