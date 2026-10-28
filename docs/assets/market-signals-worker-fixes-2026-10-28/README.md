# 三项P1修复与受保护环境恢复证据

最终门槛为main-regression-final 93条、legacy-regression 206条、mysql-final 16条，
共315个不同用例，失败/错误/跳过均0。applied-regression的94条是其中用例的工作区复核，
不重复累计。新增专项为SQLite 23条和独立MySQL5.7 16条。

baseline-red为三项原缺陷的4条失败断言，completion-red为迟到/重复回执的2条失败断言。
worker-green、main-regression、mysql是中间批次，不能作为最终通过结果；其中恢复了
全新非法JSON的旧契约，校正统一异常类型及MySQL验证会话的旧RR快照读取。
最终源码和测试以applied-sources、应用清单及最终绿色批次为准。

新增运输均用内存替身；旧设置契约使用MockTransport，不执行真实通知运输或真实模型。
readonly-ui为8000及5173真实页面的只读验收，没有新增业务规则或版本。
business-final和closing-check核对原业务状态完全一致，0092未迁移，全部后台调度关闭。

live-code-backup保留4个既有文件；applied.patch包含8个文件本轮变更；prior-reports保留
更新前4份文档以核对旧证据中的历史文档哈希。没有归档.env、连接凭据或数据库文件。
