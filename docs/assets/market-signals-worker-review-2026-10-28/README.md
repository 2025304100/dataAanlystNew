# 多worker、发送中断与重复推送审查证据

本轮没有修复/部署业务代码。19个新增探针中11通过、8个断言失败，
失败对应3个已确认根因在SQLite及MySQL中的复现；另33条既有边界回归通过。
controls额外包含重复执行的SQLite双进程探针，已从52个不同场景合计中去重。
summary的quality_gate_passed=false表示缺陷仍在，不能解读为验收通过。

最终结论使用offline-probes-final、mysql-probes-final及controls。
mysql-probes初轮两条手动重试用例受DATETIME秒精度影响、未进入目标窗口，
其失败不作缺陷证据；最终冻结整秒时钟后才确认2次替身调用。
offline-probes最初12条不含独立进程控制，最终为13条。中间记录保留，不重复累计。

测试调用的send_message全部是本地替身，不执行真实通知适配器，不发送HTTP通知。
MySQL只连接新建codex_worker_review_*库；业务数据、原规则版本、模型通知账目不变。
business-final的passed仅指审查隔离和数据保留检查通过，不指业务缺陷已经修复。
当前8000/5173无监听，本轮未启停服务、未执行真实页面验收。
source-files为审查时未改动源码，未归档.env、连接凭据或数据库文件。
