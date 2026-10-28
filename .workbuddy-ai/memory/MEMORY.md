# 项目长期记忆

## 运行环境（务必先看，容易踩坑）

- **数据库不是 SQLite**：`config/db_config.json` 指定 `use_mysql=true`，实际连的是
  `mysql://root:root@127.0.0.1:3306/gpfx`（MySQL **5.7.26**，168 张表）。
  `app/core/config.py` 里那个 `%TEMP%/quant_workbench.db` 默认 SQLite 是 **0 字节空文件**，别对着它查数据。
- **MySQL 5.7 不支持 CTE（WITH）和窗口函数（LAG/ROW_NUMBER）**，跨行/环比分析必须用 pandas 在内存里算。
- Python 用项目 venv：`.venv/Scripts/python.exe`（pandas 2.3.3 / sqlalchemy 2.0.48 / pymysql 2.2.8 已装）。
- 前端测试：`cd frontend && npx vitest run <path>`；类型检查 `npx tsc -b --noEmit`。

## 行情数据层（2026-10 审查结论，改数据相关代码前必读）

- 存在**两套并行实现**，只有旧的在线：
  - 在用：`app/services/market_data.py::_fetch_history`（内联 EM→Sina→Tencent→TDX fallback）
  - 未接线：`app/services/market_data_sources/`（SourceChain + 单位契约）+ `app/services/external_data_gateway.py`
    （`fetch_via_gateway` 无任何生产调用点）
- `app/services/akshare_guard.py` 的 Pacer/Breaker 是**死代码**：`acquire` / `record_success` /
  `record_failure` 全库 0 调用点，`ensure_available` 因 `_breakers` 恒空而永不熔断。
- 单位契约（`market_history_contract.py`）：CN 股票/ETF 的 `source` 必须形如
  `provider:endpoint:region:vN:adjust`；**裸 `akshare` 表示单位未知**，不可当作已知单位使用。
- 已确认的单位陷阱：东财 成交量=手、换手率=比率；新浪个股=股；新浪 ETF=手（未转换）；
  东财指数=手、新浪指数=股（相差 100 倍）。
- 两张行情表：`universe_daily_bars`（1,173 万，权威候选）与 `daily_bars`（156 万，业务层），
  同一份 frame 双写、无对账，已出现同标的同日 turnover 差 100 倍。

## 项目约定

- 文档读者可能是 AI Agent 时，按「可执行规格」写（Facts 带 file:line、TASK 带 VERIFY、失败即回滚），
  而非汇报稿。范式见根目录 `评分链路改造-实施规格.md`。
- 写方案类文档放 `docs/`，文件名带日期后缀。
