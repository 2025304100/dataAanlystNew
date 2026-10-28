# 缺陷复现证据

这是一轮代码审查的红用例证据，不是一次通过的部署验收。

最终批次：5 条用例，4 failure、1 pass、0 error、0 skip。
四个失败对应报告中的四项已确认缺陷；499 条历史记录的边界对照通过。
所有模型和通知 transport 均未调用；应用网络连接被测试装置拦截，
仅允许 Windows 标准库 socketpair 的内部唤醒连接。

原独立副本：D:/ai_project/dataAanlystNew/tmp/market-signals-bug-review-20261027/workspace
在该副本中执行的命令（PowerShell）：

```powershell
$env:DATABASE_URL = 'sqlite:///./data/review_bootstrap.db'
$env:APP_VALIDATION_MODE = '0'
$env:AUTO_UNIVERSE_SYNC_ON_STARTUP = '0'
$env:AUTO_TRADE_CALENDAR_REFRESH = '0'
$env:FACTOR_WAREHOUSE_PATH = 'D:/ai_project/dataAanlystNew/tmp/market-signals-bug-review-20261027/review.duckdb'
& C:/Python312/python.exe -m pytest tests/test_additional_bug_review.py --junitxml=../review-final.xml --tb=short -q
```

源代码未修复时退出码应为 1。用例依赖同项目 tests/conftest.py 的临时 SQLite fixture
和 tests/test_market_signals_api.py 的通用测试辅助函数。复验应继续使用独立副本，
不复制业务 .env/config、不启动调度器、不连接业务库。

review-initial.* 是探针初始化阶段的原始记录；其中 socketpair 被防联网装置误拦截，
不能作为业务失败数量。review-final.* 是修正测试防联网装置后的有效结果。

source-files 保存了七个相关源文件的审查快照；source-baseline.json 同时验证业务工作区
和隔离副本的文件一致，审查未修改这些业务文件。运行端口检查为只读补充，退出原因待查。
此前部署目录、验收报告、证据清单均未改写。
