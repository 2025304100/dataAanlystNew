# 前后端存活修复证据

最终49个不同pytest场景全部通过：regression-final-cli 48条，native-complete 1条。
工作区applied-final-cli 48条、applied-native-final 1条为复核，不重复计数。
新增托管边界17条及原生任务1条，其余为既有启动/策略/验收保护。

nested-job-repro是额外真实故障复现：旧启动器脱离内层后仍受外层kill-on-close约束。
default/escalated诊断进程均只睡眠并自动结束；未关闭真实执行器Job。
native-proofs为临时任务健康监听在调用方Job关闭后仍存活的真实结果，任务均清理。
actual-task.xml和任务清单为唯一正式最低权限按需任务，无登录/开机触发器。

早期regression/native等批次可能包含已修正的失败。具体包含工作目录创建、
SID名称规范化、esbuild直属conhost保护和文件入口导入路径；最终结果仅采用上述绿色批次。
第一次前端迁移被身份守卫拒绝并保留服务，事件保留在host-events；随后精确修复、受控迁移通过。

managed-restore是首版接管，managed-restore-final及business-runtime为最终独立托管。
readonly-ui仅GET检查，业务数据、0092、模型通知计数、原任务/分区/190证据均不变。
全程无真实模型/通知，业务后台调度关闭。当前仍需用户登录，重登后按需恢复。

applied-sources保留7个应用文件，live-backup保留3个旧脚本，prior-reports保留4份旧文档。
未归档数据库、.env或连接凭据。host-events为结构化运维记录，不复制含请求令牌的原日志。
历史Application日志查询资源错误及Security无匹配事件均保留，不作无崩溃证明。
