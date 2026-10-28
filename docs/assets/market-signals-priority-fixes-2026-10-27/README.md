# 四项缺陷修复及验收环境恢复证据

275 个不同 pytest 用例全部通过；applied-regression 的 31 条是其中用例的工作区复核，
不重复累计。另有 8000 真实生产页面规则验收和 5173 只读页面验收。

ms01/ms02/cf01/vr01 的 red 文件是修复前失败复现；priority-green 是导入错误的中间批次，
最终结论使用 signal-regression-final、fund-regression-final、fund-mysql-final、signal-mysql、isolated-ui。
vite-readonly/result.json 是定位器歧义的首轮记录，最终结果为 vite-readonly-final/result.json。
所有真实业务模型调用/通知计数均未改变，只允许新增暂停 shadow 验收规则及版本。

applied-sources 和 live-code-backup 分别保留整合后的候选与原工作区文件；applied.patch 为本轮差异。
prior-reports 保留本轮更新前的变更记录、使用说明和历史验收报告，以便核对此前证据中的历史文档哈希。
旧阶段资产未改写。系统 Application 日志查询资源错误保留在 system-exit-evidence 中，不当作没有崩溃记录。
