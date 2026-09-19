"""把 T04 的两条观察记录插到 PROGRESS.json 的 observations 最前面，并更新 updated_at。

用脚本而不是直接改 JSON 文本，因为 observations 里的条目很长、
含反引号与特殊字符，手写 old_string 易失配。
"""
from __future__ import annotations

import json
import pathlib

PROGRESS = pathlib.Path(".workbuddy/mining/PROGRESS.json")
UPDATED_AT = "2026-09-16T15:45:00+08:00"

NEW_OBSERVATIONS = [
    "⚠ 既有退化路径（T04 发现，未修，修复点不在任何 M1 任务写权限内）：沙箱的唯一调用方 "
    "discovery_fast_scan._advanced_filter 构造的 context 是**最新一根 K 线的标量**"
    "（close = float(latest_bar.close)，discovery_fast_scan.py:436-446），**没有序列**。"
    "于是任何需要序列的函数（sum/min/max/len，以及新增的 ts_delta_bars/ts_delta_periods/ts_atr）"
    "拿到标量 → 返回 None → discovery_fast_scan.py:461-464 的 `if result is None: continue` "
    "→ **该指标不参与筛选（视为通过）**，即**筛选条件静默失效、用户却以为在生效**。"
    "T04 对 ts_ 是新增影响面（列表类函数此前已如此）。修复方向：让该调用方传序列"
    "（该文件已自述为「先实现逐标的+单公式评估、未启用向量化」的降级路径）。"
    "注意**不要**改成「构造期拒绝 ts_」—— 沙箱自身契约支持序列"
    "（IndicatorFormulaEvaluator docstring 示例即 {\"close\": [10, 11, 12]}），拒绝会把正确用法也挡住。"
    "哨兵：tests/services/factors/mining/test_dsl_time_series.py::"
    "test_sandbox_returns_none_for_scalar_context_documenting_caller_degradation",

    "🚨 `ts_atr` 口径存疑（T04 发现，已按 close-to-close 实现，**待需求方确认**）："
    "需求 §6.0 与开发文档 §3.14 把 ts_atr(x, n) 描述为「平均真实波幅」，但签名只携带**一条**序列"
    "（归一规则为 atr → ts_atr(close, n)、向导模板为 ts_atr(close,{n1})/close）。"
    "经典 ATR 需要 high/low/close 三条。已在 T04 实现为 close-to-close"
    "（TR=|close-ref(close,1)| 的 n 期均值），理由是执行器按依赖字段裁剪 SELECT"
    "（factor_executor.py:680/746），隐式取 high/low 会因列缺失**静默产出全 NaN**。"
    "若需求方要经典 ATR，需同时决定签名（改成三参数，或引入「隐式字段依赖」机制并改依赖收集），"
    "属契约变更。哨兵：tests/services/factors/mining/test_dsl_time_series.py::"
    "test_atr_only_depends_on_the_passed_series",
]


def main() -> int:
    doc = json.loads(PROGRESS.read_text(encoding="utf-8"))
    existing = doc.get("observations") or []

    added = [o for o in NEW_OBSERVATIONS if o not in existing]
    doc["observations"] = added + existing
    doc["updated_at"] = UPDATED_AT

    PROGRESS.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"observations: {len(existing)} -> {len(doc['observations'])}（新增 {len(added)} 条）")
    print(f"updated_at -> {doc['updated_at']}")
    print()
    print("=== observations 摘要 ===")
    for index, item in enumerate(doc["observations"], 1):
        print(f"  [{index}] {item[:88]}")
    print()
    print("=== tasks ===")
    for tid, entry in doc["tasks"].items():
        print(f"  {tid}: {entry['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
