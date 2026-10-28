# 并发、跨日与重试边界审查证据

本轮为审查，不是修复或发布验收。11个探针中5个断言失败，对应报告中的五个遗漏边界；
六个对照通过，包含普通原子抢占只有一个worker获胜。没有调用任何通知transport，
成功送达仅为独立fixture数据库中的模拟回执；没有真实模型或供应商调用。

原独立副本：D:/ai_project/dataAanlystNew/tmp/market-signals-boundary-review-20261028/workspace
执行命令：

```powershell
$env:DATABASE_URL = 'sqlite:///./data/review_bootstrap.db'
$env:APP_VALIDATION_MODE = '0'
$env:AUTO_UNIVERSE_SYNC_ON_STARTUP = '0'
$env:AUTO_TRADE_CALENDAR_REFRESH = '0'
$env:FACTOR_WAREHOUSE_PATH = 'D:/ai_project/dataAanlystNew/tmp/market-signals-boundary-review-20261028/review.duckdb'
& C:/Python312/python.exe -m pytest tests/test_boundary_review_20261028.py --junitxml=../boundary-review.xml --tb=short -q
```

未修复时退出码为1。用例依赖项目已有的SQLite fixture、前一轮回归测试辅助函数及禁止
应用联网的fixture；只允许Windows标准库socketpair内部唤醒连接。复验须继续使用独立副本，
不复制业务.env或连接配置。测试不放入业务根目录的正式回归集。

business-before/after完全一致，source-files为九份当前源码的只读快照；SHA清单可核验。
此前修复及验收目录未改写。sending不自动回收是原代码的明确设计，报告另述操作限制，
没有用自动改状态或重发来演示该风险。
