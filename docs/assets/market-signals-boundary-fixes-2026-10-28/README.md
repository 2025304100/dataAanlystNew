# 五项并发、跨日、取消与重试边界修复证据

311个不同pytest用例全部通过；applied-regression的64条属于工作区整合后的复核，
已包含在311条中，不重复累计。本轮新增33条普通边界回归和3条MySQL并发回归。

br01/br02/br03/br04/br05的red文件保留修复前失败断言，属于预期的缺陷复现；
initial-green是中间回归。最终结论以signal-regression、fund-regression、fund-mysql、
native、ui-and-legacy及applied-regression为准。3条真实供应商slow探针未选择，
没有把它们计入通过或跳过数。observations.json是隔离回放观测，不代表业务送达。

business-ui为8000真实生产页面验收，vite-readonly为5173只读验收，
isolated-ui为临时数据库/临时端口页面端到端测试。所有真实模型及业务通知投递均为0。
测试内成功/失败回执为本地持久化替身，不执行通知适配器。业务仅允许新增2条具名
验收规则和5个版本，最终均暂停且shadow；0092、190证据、原任务和5367分区、
模型配置与通知账目等指纹均保留。完整核对见business-final和final-check。

applied-sources保留本轮候选；live-code-backup保留5个旧文件；applied.patch仅包含本轮
7个文件的差异。prior-reports保留更新前3份历史文档，旧阶段证据及审查报告未改写。
source-baseline记录准备时整个项目Python源码哈希，未归档业务.env、凭据或数据库文件。
evidence-manifest记录归档文件、四份最新报告和7个实际应用文件的SHA256。
