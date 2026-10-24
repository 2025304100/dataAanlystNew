# main 历史核查发现的自动交易派发热修

日期：2026-10-23。本记录独立于检查与对账交互重构、G5 真实性及准入热修。

## 问题和原始证据

`create_portfolio_auto_trade_task` 原来只把 `dry_run` 和 `buy_candidate_limit` 交给通用幂等任务服务。同样设置的负载哈希长期相同，即使返回的是旧终态任务，仍调用 worker；worker 只排除不存在或取消的任务，无法防止旧完成任务重入。

10-20 完整数据库备份中 47 条计划 11 的派发指向任务 `e55ff07040ab479cb4065d42e43f36b8`。该任务 08-18 创建，在备份中的起止时间为 10-19，当前保存值变成 10-22；结果保留 main 的数据过期错误。**不能据此断言每天均未执行，也不能用这些 `done` 派发证明每天已成功执行。**

证据见 [task-reuse-snapshots.json](main-history-evidence/task-reuse-snapshots.json)，完整来源哈希见[主核查记录](main-history-recovery-and-candidates.md)。

## 前向修复

| 位置 | 最终行为 |
|---|---|
| `app/services/auto_trade_task.py` 创建任务 | 加入北京时间实际 `dispatch_date`；同日同负载去重，跨日生成独立任务；不是任意传入的历史决策日期 |
| 创建入口返回值 | 返回终态任务时不再启动 worker |
| worker 入口 | 仅允许未终态锁定的 queued/running 任务进入执行 |
| `app/services/portfolio_execution_history.py` | 只读输出调度及确切原任务上下文，区分跨日引用和已知过期终态；不产生执行凭证 |
| 对账及页面 | 上下文纳入证据指纹并原样留审计，展示“不能证明本日执行”；不清除缺决策、不放行确认 |

没有修改通用异步任务的幂等契约，避免破坏核对作业的证据版本去重。没有改历史 task、dispatch、运行、订单或成交记录。

## 验证与部署边界

- 隔离用例验证同日终态复用不重入、北京时间午夜跨日创建新任务、终态锁定任务不进入组合执行。
- 自动交易相关回归 42 passed / 1 个既有 xfailed。
- 调度来源及对账、历史、步骤相关最后回归 108 passed。
- 真实 main 13 日只进行核对，新的报告仍为证据不完整。
- 本地服务仅加载 `scripts/serve_inspection_acceptance.py`，调度允许类型只有 `portfolio_reconciliation`；已启用的其他原计划没有在本次被派发。

**没有用真实自动交易、补单或恢复交易验证该热修。** 将来若允许普通运行环境重新启用交易调度，仍须先完成模型、绑定、账户与交易门禁审核，不能把本记录视为启用许可。
