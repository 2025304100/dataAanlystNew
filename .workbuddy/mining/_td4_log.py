#!/usr/bin/env python3
"""向今日工作日志追加 TD4 收工记录（幂等判重 + 回读校验）。"""
from __future__ import annotations

import datetime as _dt
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
MEM = HERE.parent / "memory"
today = _dt.date.today().isoformat()
log_path = MEM / f"{today}.md"

block = """

## TD4 完成：账务 8 列 Float→Double（2026-09-18）

**开工核对**
- 探针确认 8 目标列现状全部 `float`（单精度）、6 表全 InnoDB、行数 2~224（ALTER 秒级）。
- **DoD ③ 修正（TD1 同款教训）**：全量 drift 基线 48/127，6 表中 5 张开工前就带既有漂移
  （cash_ledger 缺FK、portfolios/positions/sim_orders 列差异、backtest_runs 多索引等），
  全量跑必 exit 1 → 收窄为 `--tables portfolio_equity_snapshots`（唯一开工前即零漂移者）。
- **drift 工具不比对列型**——Float→Double 它根本检测不到，这正是本债长期漏报的根源；
  列型正确性只能靠 `_verify_accounting_double.py` 的 DATA_TYPE 检查 + 行为验证。

**施工**
- 迁移 `0057_wps_0023_055_accounting_float_to_double`：8 列 ALTER MODIFY DOUBLE；
  幂等（DATA_TYPE 探测）、MODIFY 显式保留 NULL 性（MySQL MODIFY 不写会重置为 NULL）、
  意外列型（decimal 等）中止交人工、**downgrade 有损守卫**（默认拒绝，需
  ALEMBIC_ALLOW_LOSSY_DOWNGRADE=1）。SQLite 跳过（REAL 本就是 8 字节 DOUBLE）。
- ORM 四文件 8 列 Float→Double（sim_account/portfolio/portfolio_equity_snapshot/backtest）。
- 白盒测试 `tests/test_whitebox_numeric_columns.py`（10 用例）：8 列 Double+NOT NULL
  参数化断言、迁移 _TARGETS 与 ORM 清单一致性、**防走样守卫**（6 表内不允许第 9 个 Double 列）。
- 行为验证：1,200,000,000.01 写入读回精确一致（单精度该量级分辨率 128 必量化）；
  backtest_runs + cash_ledger 两表真实库验证，测试行前后双清扫。
- 回归：迁移链 + 组合/快照/回测/自动交易 5 个测试文件。

**教训**
- f-string 里写 `{PY}` 字面占位符会 NameError——占位符文本别加 f 前缀。
- ORM 类型断言用 `isinstance(col.type, Double)`，别比对类名字符串（类名是 `Double`，
  表名是复数 snapshots / 文件名单数 snapshot，靠打印猜必错）。
- 任务卡写 DoD 前先跑全量基线：全量 drift 本来就红（48/127），「全量 exit 0」一写就是废卡
  （TD1/TD4 连续两卡同款，已成规律）。
"""

if not log_path.exists():
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"# {today} 工作日志\n", encoding="utf-8")

text = log_path.read_text(encoding="utf-8")
if "## TD4 完成" in text:
    print("[log] 今日日志已含 TD4 记录，跳过")
else:
    log_path.write_text(text.rstrip("\n") + "\n" + block.lstrip("\n"), encoding="utf-8")
    back = log_path.read_text(encoding="utf-8")
    assert "0057" in back and "1,200,000,000.01" in back and "48/127" in back, "回读校验失败：内容被截断"
    assert "防走样守卫" in back, "回读校验失败：防走样守卫丢失"
    print(f"[log] 已追加 TD4 收工记录到 {log_path.name}（回读校验通过）")
