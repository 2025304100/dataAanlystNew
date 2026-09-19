"""恢复被静默覆盖的 T04 完成记录，并给两个自检脚本加「重复键」检测。

事故复盘（2026-09-16）
----------------------
1. T03 收工时，我在 T03 记录**后面**追加了占位 `"T04": {"status": "pending"}`。
2. T04 开工时，我又在 `tasks` **开头**插入了新的 `"T04"` 块 → 同一对象里出现**两个 "T04" 键**。
3. 完成 T04 后用 Edit 更新的是**开头**那个；文件里仍留着后面那个 `pending` 占位。
4. 随后一个改 observations 的脚本执行 `json.loads` → `json.dumps`：Python 的 dict
   解析时**后者覆盖前者**（"T04" 变成 pending），重序列化又把重复键**去重**，
   于是 T04 的 done 内容**无声消失**，文件本身看不出异常。

教训
----
- **不要在 JSON 对象里手工追加同名键**。加任务条目前先搜一遍是否已有占位。
- **JSON 的「重复键 = 后者优先」是静默的**，必须用 raw-text 自检兜住；
  仅靠 `json.loads` 校验语法**永远发现不了**这类问题。
- 任何「读 JSON → 改 → 写回」的脚本都会**顺带去重**，从而把重复键事故掩盖成
  「数据莫名丢失」。所以重复键检测必须放在**写之前**跑。
"""
from __future__ import annotations

import json
import pathlib

PROGRESS = pathlib.Path(".workbuddy/mining/PROGRESS.json")

T04_ENTRY = {
    "status": "done",
    "agent": "agent-senior-dev",
    "started_at": "2026-09-16T15:00:00+08:00",
    "finished_at": "2026-09-16T15:45:00+08:00",
    "artifacts": [
        "app/services/factors/dsl/time_series.py",
        "app/services/factors/factor_catalog_placeholder_removed",
        "app/services/factors/factor_compiler.py",
        "app/services/factors/formula_catalog.py",
        "app/services/indicator_ast_sandbox.py",
        "tests/services/factors/mining/test_dsl_time_series.py",
    ],
    "evidence": [
        "cmd: .venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_dsl_time_series.py -q  -> exit 0 · 103 passed in 0.88s（0 warnings）",
        "编译实测：ts_delta_bars(close,20)=04018f5cfaace6df / ts_delta_periods(roe_ttm,4)=2f96bcf1acb15972 / ts_atr(close,14)=d2944819857f9daf / ts_atr(close,14)/close=ed5cf77d3d8230ac",
        "向导 §6.3.3 的 7 条真实 ts_ 模板式子全部编译通过（含 -ts_delta_bars(close,5)*cs_rank(volume)、ts_atr(close,14)/close）",
        "非法入参：ts_delta_bars(close)->function_arg_count；ts_delta_bars(close,0)->negative_lag；ts_delta_bars(close,volume)->function_window_invalid；ts_delta_bars(close,300)->lookback_window_exceeded",
        "content_hash 回归：11 条既有（非 cs_/ts_）公式 hash 逐条不变（读 .workbuddy/mining/evidence/T03_content_hash_golden.json）",
        "公式目录端到端：build_formula_catalog(None) -> 26 个 functions（含 3 个 timeseries）；FUNCTION_CATALOG 与 _FUNCTION_META 键完全对齐（无 KeyError）",
        "单位差异量化：n=4 时日频跨 4 天、季频跨 364 天，倍数 91.0（= DAYS_PER_UNIT 比值）",
        "沙箱跨实现一致性：ATR_SEQUENCE=[10,11,13,12,15] 下 n=2/3，沙箱标量 == 面板末值（delta 2.0/4.0，atr 2.0/2.0）",
        "方向标注混合轴：cs_rank(volume)-cs_rank(ts_delta_bars(close,5)) -> [cs_rank/cross_section, ts_delta_bars/timeseries, cs_rank/cross_section]（从内到外）",
        "回归：tests/services/factors/mining + 3 个白盒（factor_compiler/formula_catalog/discovery_filter） -> exit 0",
        "回归：tests/factors -> exit 0",
    ],
    "decisions": [
        "★ `ts_atr` 采用 **close-to-close 口径**（TR=|close-ref(close,1)| 的 n 期均值），**不是**含 high/low 的经典 ATR。理由：执行器的数据读取是**按依赖字段裁剪的 SELECT**（`factor_executor.py:680` 的 `table_fields` → `:746` 的 `_select_field_expressions`），而签名 `ts_atr(close, n)` 的依赖里没有 high/low。若实现偷偷取它们，列不会被查出来 → context 缺失 → `_eval_node` 返回 None → `execute()` 落成 `pd.Series(nan)`，即**静默全 NaN**（与 prev_close 同一失败形态）。需求 §6.0 与向导 §6.3.3 的模板 `ts_atr(close,{n1})/close`（ATR 比率）在 close-to-close 下依然成立。已用「只给单列 DataFrame 也能算出正确值」的测试守死。**此为需求口径存疑点，已上报**",
        "★ `ts_delta_bars` / `ts_delta_periods` 实现**逐字相同**（都按位置位移 n），差异只在 n 的口径与传入序列的采样语义。用机器可读的 `TS_N_UNIT`（trading_day / report_period）+ 签名文案 + 91 倍日历跨度断言把差异钉死。刻意不让 periods 做隐式「交易日→报告期」换算 —— 那正是需求 §6.0「逻辑缺口 2」要消除的隐式猜测",
        "★ `_validate_n` **拒绝非整数**（1.9 不再静默截断成 1），并接受 numpy 整数（`isinstance(np.int64(2), int)` 为 False，故按数值判定）。沙箱侧 `_positive_int` 同口径",
        "★ `ts_atr` 用 `min_periods=n`（前 n 期 NaN，**不造值**）；既有 `_eval_rolling_func` 对 sma/mean 用 `min_periods=1`。**刻意不统一**（需求 §3.5 不得填零/降级），已写进 T05 坑位防止被「对齐」",
        "★ 沙箱 `ts_` **不复用** cs_ 的拒绝占位，提供真实现（D-A 只针对需要面板上下文的截面算子）。另加**跨实现一致性测试**（沙箱标量 == 面板末值），因为两套实现刻意不互相 import（保持 app/services/ 跨域边界干净），必须由测试兜住漂移",
        "★ 沙箱 `_ALLOWED_NODES` **未改**（不含 ast.Compare）。实测 `ts_delta_bars(close,2) > 0` 报 `unsupported AST node: Compare`。这**不是缺陷**：`discovery_fast_scan.py:474-481` 拿公式结果与 `ind_def` 的 `min_value`/`max_value` 比较，阈值本就由指标定义承载，公式只需产出数值。已加测试固定该分工",
        "★ `FunctionSpec.category` 统一为 **`timeseries`**（与 `DIRECTION_TIMESERIES` 同拼写）；T03 里写的 `time_series` 已更正。两套拼写会让按 category 分派的下游静默漏掉分支",
        "★ T05 必须加**两个**分支（cross_section + timeseries），且需要 **Series↔面板 DataFrame 适配层**（`_eval_ast` 的 context 是按 index 对齐的 Series，而 dsl 层要求 MultiIndex(trade_date, symbol)）。已把 8 条坑位写进 T05 卡，标题也从「截面分支」更正为「截面/时序分支」",
    ],
    "notes": "T04 完成，103 passed。ts_ 族三算子已四处注册；n 单位差异用元数据 + 日历倍数钉死；ts_atr 只依赖传入序列（不重蹈 prev_close 的静默全 NaN）；content_hash 零变化。G1 门禁剩 T05（方言归一 + 执行器分支）与 T06（字段注册 +3/-1 + 25 模板编译率）。",
}


def main() -> int:
    doc = json.loads(PROGRESS.read_text(encoding="utf-8"))
    doc["tasks"]["T04"] = T04_ENTRY
    # 占位条目名不副实，去掉
    artifacts = T04_ENTRY["artifacts"]
    T04_ENTRY["artifacts"] = [a for a in artifacts if "placeholder" not in a]
    doc["updated_at"] = "2026-09-16T15:50:00+08:00"
    PROGRESS.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("T04 记录已恢复 ->", doc["tasks"]["T04"]["status"])
    print("artifacts:", doc["tasks"]["T04"]["artifacts"])
    print()
    print("=== tasks ===")
    for tid, entry in doc["tasks"].items():
        print(f"  {tid}: {entry['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
