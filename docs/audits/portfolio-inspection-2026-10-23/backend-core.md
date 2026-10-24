# 后端核心逐条核查（2026-10-23）

核查依据为主设计《组合交易检查与对账—功能闭环与交互重构设计》§3–7、§10、§11，独立 P0 热修清单，以及最终 `docs/prototypes/portfolio-reconciliation-v10.fragment.html` 的数据、对账、恢复交互。本报告直接查服务、模型、中央挂载接口与隔离数据库测试，不将主设计 §20 的实施自述作为完成证据。未运行真实环境修复、迁移、调度或交易；所有已有未提交修改保留。

状态含义：**完成**为已实现且有对应分支测试；**部分**为安全路径已有、完整业务依赖尚缺；**缺失**为真实能力未接通；**其他 owner**为仅只读核查、需合并该 owner 的证据。测试通过不代表已部署或真实数据已满足条件。

## 本轮修补前后

| 缺口 | 修补前 | 本轮实现 / 证据 |
|---|---|---|
| 修复任务返回后无法重新定位、无取消入口 | 只有单 ID 查询和重试；页面内保存 ID | 新增只读 `GET /inspection/repairs?limit&cursor`，真实持久任务按组合过滤与稳定游标分页；`POST /repairs/{id}/cancel` 自然幂等。运行中取消只停止未开始对象，保留成功产物；审计使用真实取消者。见 `portfolio_inspection_repair.py:124`、`:157`、`portfolio_inspection.py:61`。 |
| worker 崩溃 / 终态后仍存活的重试 | 重启记录会失败，但接口没有恢复说明 | 复用真实 `interrupt_orphaned_async_tasks` 持久中断；GET 返回 `recovery` 与未完成重试范围；活 worker 尚未退出时拒绝重试。成功对象不重做。见 repair `:85`、`:276`；`test_cancel_between_objects_preserves_success_and_restart_exposes_only_unfinished_retry`。 |
| 未来覆盖起点、正常等待被当作恢复入口 | 只有 `can_confirm=false` 和兼容动作 | runtime 增加 `phase/recommended_action/waiting_reason/next_check_at`，分别表示等待起点、等待执行、可审、阻断、无需审核；正常 READY 不再产生“没有待恢复审核”的假阻断。见 runtime `:103`；`test_activated_future_scope_waits_for_start_without_disabling_risk_exit`、`test_legitimate_execution_wait_exposes_wait_instead_of_manual_recovery`。 |
| 任一标的缺口导致整组合数据门禁升级 | runtime 用全部候选健康，而 readiness 仍为至少一个健康 | 保留明确 `existing_any_healthy_symbol_v1`：作用域读取失败仍阻断；坏标的继续受逐标的门禁，至少一个健康标的保留组合数据可用性。返回覆盖数量、受限标的和全部证据。见 runtime `:24`；`test_runtime_keeps_existing_any_healthy_symbol_aggregation_and_restricts_the_bad_symbol`。 |
| 对账队列凭日历行完整度宣称 KNOWN | legacy/coverage queue 只看行数 | 两条队列读取共同的可信交易所日历、版本和应到日；来源未知不编日期；当前日未到期不列为逾期。见 resume `:67`、`:122`；`test_raw_calendar_rows_do_not_claim_known_reconciliation_dates`。 |
| 历史 resume 能补下真实交易 | `daily_fill_gap` 为所有旧日建任务，worker resume_mode 跳过当前日期校验 | 新增 `review_resume_execution_date`，历史无当前有效冻结计划时只供诊断、禁止派发；worker 再核验。仅当前业务日，或原定今日执行的真实未完成冻结计划可进入原执行门禁。execution task `:232` 使用已经核实的原执行日为行情上界，不再把合法 T+1 恢复锁在旧信号日；不读后日行情。见 resume `:170`、`:429`，execution task `:186`；历史隔离服务测试及 `test_public_resume_continues_only_the_real_unfinished_frozen_plan_due_today_once`。 |
| 缺自动记录只有笼统缺证据 | 没有逐来源活动结果与真实出口 | 对账报告增加真实订单、成交、现金、当前持仓活动的独立查询结果、来源引用、三项检查及未知历史执行义务。无运行但有真实活动为 `UNVERIFIED_ACTIVITY`；失败尝试为 `EXECUTION_FAILED`；未执行处置政策未批准时明确 disabled，保留真实当前账户覆盖预览入口。见 reconciliation `:142`、`:175`；`test_missing_run_exposes_real_activity_and_an_honestly_disabled_historical_resolution`。 |
| ActionItem 缺结构契约、准入混入日常计数 | legacy 三源标题、严重级和导航 | 结构化 evidence/actions/scope/capabilities/完成条件/快照/任务审计关联；数据组保留全部逐标的事实，对账按基线、代次、日期分项；新增 daily 与 engine admission 独立计数，保留旧字段。见 action_items `:282`、`:431`、`:471`。 |
| A36/A38 持久工作、退避 | 模型已有字段，job 尚未消费 | 日期工作引擎已落地：派发前保存工作行；请求版本冻结后只消费该版本；新需求保持 queued；读取异常按 1/5/15 分钟再试，第4次失败转人工；等待不消耗错误次数；失败派发、重启、取消均保留真实报告。见 job `:128`、`:144`、`:161`、`:434`、`:573` 及下方测试。全系统事件 hook / 原子写保证仍部分。 |
| 数据产物已有效但旧维度仍暂停 | `try_auto_recover_dimensions` 无调用，真实数据已补齐后仍被持久 data/score 状态挡住 | 已确认 repair 的最终重检改为 Portfolio 根锁 + current_read，与 runtime 共用纯 `evaluate_inspection_admission_scope`。仅真实入场范围满足现行聚合时清 data/score，完整保留 model/recon/admin/runtime；结果和审计含 `dimension_recovery`。健康、旧日、READ_ERROR 三分支有隔离 DB 测试。 |

## 主设计逐条核查

| 条款 | 状态 | 实际代码 / 测试 / 剩余依赖 |
|---|---|---|
| §3.1-1 一个原因、一份证据、一套规则 | 部分 | diagnostics `inspection_admission_failures`（`:191`）由 safety/runtime 复用；ActionItem 直接读取 diagnostics/runtime/queue。实际执行 readiness 的“任一健康”由 runtime 同源保留。仍有旧 `PortfolioStatusResponse` 只返回有限字段，尚非一个统一快照覆盖所有旧客户端。 |
| §3.1-2 未知不等于零 | 完成于诊断与新增证据 | diagnostics 明确 MISSING/STALE/READ_ERROR/FUTURE_DATA/CALENDAR_UNKNOWN；activity `query_status=READ_ERROR,count=null`，当前持仓注明 CURRENT_ONLY。旧对账失败查询可能返回整体错误，不能当 0；逐来源完整容错仍见后续限制。 |
| §3.1-3 只解除解决条件 | 完成于已接通恢复守卫 | state machine `:528` 单独清对账维度，复核实时数据和绑定，保留本人暂停及其他维度；repair `:418` 的真实重检只恢复已证明 data/score，审计和状态同事务，任务产物成功不代替证据通过。测试 `test_confirmation_only_clears_reconciliation_and_audits_exact_report`、`test_reconciliation_confirmation_retains_runtime_lock_for_current_data_failure`、`test_confirmed_repair_recovers_only_proven_data_dimensions_and_keeps_independent_locks`（3分支）。 |
| §3.1-4/5 历史不改写、每次点击有结果 | 完成于数据修复；历史处置部分 | repair 有范围、逐对象产物、重检、审计、失败项重试；resume 历史补单被封堵。历史证据关联/补证单没有真实写接口，必须明确禁用，不得模拟成功。 |
| §3.2 D1 / §7.5 默认关闭例外 | 完成于默认拒绝 | 普通 `confirm_reconciliation_fixed` 拒绝 `force_skip_re_reconcile`；runtime `force_recovery_enabled=false`。未建设批准后的例外授权流程；范围、角色、期限策略仍待业务批准。 |
| §3.1-6 / §3.2 D2 | 其他 owner | 双跑无数据不造结论、以引擎链/冻结清单而非两个模型为主对象，属 G5/HF01–HF07 owner；本模块不写双跑报告或准入例外。须合并其代码、真实消费者和 P0 独立报告后判定。 |
| §4.1/4.2 可信日历与应到日 | 部分 | diagnostics `:98` 验来源、同步者、年度完整日期、合法 flag、开市数量；不回退星期或自然日昨天；`resolve_inspection_calendar` 为公开同源入口。只支持已接入的 CN，其他市场明确 UNKNOWN；跨年度完整可信日历仓与独立实际行情覆盖冲突清单未接齐。 |
| §4.3/4.4 业务日期/可用时点/交易日滞后 | 完成于现有数据；PIT 部分 | diagnostics `:163`、`:309`，safety `check_data_health` 不再以 created_at 当新鲜业务日；模型/配置/冻结依赖真实匹配。日K缺独立发布时间和复权标记，FactorValue 缺版本发布时间，明确 unknown/limitation，不能宣称历史 PIT 完整。 |
| §4.5 双跑时间与样本 | 其他 owner | 实际交易日区间、休市剔除、双侧共同有效证据与 coverage 为 G5 owner。可信 CN 日历 resolver 可复用，但本报告不将诊断日历测试当双跑区间验收。 |
| §4.6 全量逐标的诊断、限定修复、重试取消 | 完成于已接通执行器 | diagnostics `:511` 生成实际 scoped plan；repair `:238` 预览快照/15分钟/依赖/幂等校验；`:303` 仅行情同步、明确规则配置评分。冻结模型批量评分和外部映射无安全执行器时 disabled。测试涵盖两标的全部异常、当前配置突变、同步无产物、只重试失败评分、取消和重启。 |
| §5.0/5.1/5.3 三层检查与判断树 | 部分 | reconciliation 三项 checks、真实差异优先、缺证据保留、合法执行等待。完整历史接管/调度有效区间来源不存在时 execution_expected=null；不由今日开关推断过去。NO_TRADING_ACTIVITY/MISSED_EXECUTION 尚无足够历史输入可合法自动判定。 |
| §5.2 结论枚举 | 部分 | 支持 PASSED、DIFF_FOUND、EVIDENCE_INCOMPLETE、UNVERIFIED_ACTIVITY、EXECUTION_FAILED、NOT_DUE/IN_PROGRESS。MARKET_CLOSED/NOT_APPLICABLE 在完整对账服务尚未成为独立关闭分支；历史未知由 coverage 缺口处理；NOT_EXECUTED_RESOLVED 政策未批准而禁用。 |
| §5.4/5.5 连续游标与队列 | 当前代完成；legacy 部分 | active coverage 的四表和连续推进为 history owner；本服务调用其 expected_generation、expected_cursor_row_version，队列读取当前基线/代次/版本。后日先报告不跳早缺口。legacy `_checkpoint_progression` 仍是明确标注的兼容适配，不是全历史核验或已接受账户基线的证明；部署迁移前不能将其当完整新契约。 |
| §5.6/5.7 缺记录真实处理出口 | 部分 | 新增真实活动和来源检查、当前账户覆盖入口；历史回放/未执行处置不写真实交易。尚缺持久补证单、验证真实运行后关联的写接口，以及经批准的未执行处置服务；相应动作不得显示已成功或假排队。 |
| §5.8 报告版本、确认、剩余阻断 | 完成于现有确认 | `confirm_reconciliation_fixed:471` 写事务重查 exact report fingerprint；中央接口要求 ID/version，冲突409；current_read 强制刷新 identity map 与 FOR UPDATE；账本变化拒绝，保留独立门禁。测试中央 HTTP 与服务各分支。 |
| §5.9 持久自动作业 | 日期工作引擎完成；全链路部分 | job `:128/144/161/224/434/573` 已实际消费持久日期行、请求/处理版本、失败退避、重启与取消；工作状态和业务 verdict 分开，真实 job records/read_error 可查询。`scheduled_tasks.py:369` 有幂等默认5分钟 seed 与派发。仍缺所有来源写事务的精确事件 hook、完整撮合/结算截止政策、自动核验失联运维 ActionItem；是否已部署且 scheduler 在运行未验证。详见独立边界。 |
| §5.10 当前新基线出口 | 其他 owner / 已有集成测试 | coverage preview/accept + 单独 activation review/activate，旧期未知、新代空游标、资产与未决义务核验、旧代报告拒绝由 coverage/history owner实现；中央 HTTP 回归实际挂载并验证资产不变。真实环境未迁移、未新建基线。 |
| §6.1–6.3 ActionItem 统一只读和字段 | 部分 | 新聚合读取同一 diagnostics/runtime/queue，并返回结构字段。没有调度/状态写副作用；`test_real_orchestration_does_not_create_legacy_column_or_flush_pending_work`。四类已接通来源真实，旧 readiness 文案仍为兼容字段；完整 required_role 与真实角色策略依赖中央权限系统。 |
| §6.4–6.6 根因、作用范围、计数、依赖 | 部分 | 同族合并保留逐指标事实、不同日期独立条目、任务/报告/审计结构引用；日常与引擎准入计数分开。修复步骤依赖为 `repair_dependencies`，不伪造 ActionItem ID 依赖。跨页定位/筛选保持由前端 owner验收。 |
| §7.1/7.2 运行状态与四项权限 | 部分 | runtime返回实际 buys/risk exits/recovery 和原因、所有独立维度；未来与等待明确；并未给 ordinary sell 建立独立于 risk sell 的完整权限对象，完整 status/reason_codes/evidence/blocking IDs contract仍需扩展，不能声称四项齐备。 |
| §7.3/7.4 审核材料、通用转移守卫 | 部分 | 当前绑定实际审查、对账 exact report、实时数据和连续范围被确认守卫复验；普通转 READY 不能绕过模型/对账/首次覆盖审核。人工暂停期间完整活动材料与状态快照指纹并非每个旧入口都齐备；本人备注不代替缺事实。 |
| §10.1 状态响应 | 部分 | 独立runtime响应扩展真实状态原因与恢复阶段；旧中央 `PortfolioStatusResponse` 未齐备 snapshot/expires/ready_for_daily/rollout/can_review。两者不能被当同一保证原子一致的快照。 |
| §10.2/10.3 行动项和对账契约 | 部分 | 本轮补结构 ActionItem；ReconciliationResponse透传 activity/checks/findings/execution expected/window/status/nextactions/confirmblockers/evidencehash/generated。完整 cursor/pending/settlement opening/closing ref、scope/capabilities 尚非所有旧响应都有。 |
| §10.4 双跑接口 | 其他 owner | 请求预检/双侧冻结清单、能力发现、真实 job/verdict/矩阵、区间适用性和未知拒绝由 G5 owner验证；不能以本模块 repair task 接口存在作为双跑已实现证据。 |
| §10.5/10.6 审计、动作与错误 | 部分 | repair 采用真实白名单兼容 envelope + original_action，实际 actor/correlation/issue/task/report refs；state transition 审计保留备注。数据修复幂等完备；普通恢复和对账确认仍未在所有服务入口统一 action_id/snapshot/expected_version/idempotency_key，通用错误 envelope也有兼容差异。 |
| §10.7/10.8 持久化与事务 | 当前代部分完成 | baseline/report不可变、generation/current pointer/cursor row version与审计原子写由 coverage提供；repair逐对象输出与checkpoint提交，最终维度恢复与任务审计同事务。job 冻结 requested 版本传入 `reconcile_trade_date`→coverage，保留更高未处理需求。历史执行开关与补证处置单未持久化；来源写入+需求没有全面原子 hook。未运行真实环境迁移，不能证明生产表已存在。 |

## §11 后端验收矩阵

| 验收 | 状态与直接证据 |
|---|---|
| A1 | 当前代完成：`test_accepted_baseline_replay_advances_only_real_current_generation`、真实 job funded account 测试。 |
| A2 | 部分：没有可证明目标日关闭的完整接管/调度历史，不能合法产 NO_TRADING_ACTIVITY；不是填零可解决。 |
| A3 | 部分：历史义务 UNKNOWN 时不判 MISSED；历史补单已封堵，缺真实配置时间线。 |
| A4 | 完成于识别/阻断：新增真实手工订单无run为 UNVERIFIED_ACTIVITY；按来源完成手工核验的处置接口仍部分。 |
| A5 | 部分：日历、账户活动独立证据已有；MARKET_CLOSED 尚非完整正式对账分支。 |
| A6 | 当前代完成；legacy部分：未知不造队列/准入，风险退出保持；legacy checkpoint仍需迁移收口。 |
| A7/A8 | 完成于真实已有来源：缺历史持仓 NOT_CHECKED；真实现金/持仓差异保留，重检+确认。 |
| A9/A13/A14/A16 | 完成于现有源，diag测试覆盖周末、盘中、导入旧日、错配置/未来发布时间/依赖与实际产物。独立 PIT 数据源不足见上表。 |
| A10 | 完成默认关闭/拒绝绕过；批准后的例外流程尚无政策，不宣称可用。 |
| A11/A12/A24–A27 | G5 owner；独立 P0 热修 HF01–HF07 需其报告与真实消费者测试，本报告不代为认证。 |
| A15 | 完成：现行任一健康聚合保留，坏标的限制、覆盖与证据明确；新增runtime与ActionItem测试。 |
| A17 | 部分：逐来源活动 READ_ERROR不是0；关键源整体异常会报错拒绝，完整多源容错仍需补齐。 |
| A18/A35 | 已有执行时点等待完成；完整截止政策部分：实际 execution_at未来等待，不提前缺单，不写暂停或重做整日；尚无市场逐类撮合/结算终止时限和超过业务截止转人工的完整规则。 |
| A19 | 部分：不取当前开关推过去，但历史开关源尚缺，返回UNKNOWN。 |
| A20 | 其他owner：四表当前代连续推进与日历前缀版本复验，history owner测试；legacy不是新代证明。 |
| A21 | 政策依赖：明确 disabled `MISSED_EXECUTION_POLICY_NOT_APPROVED`，不可写假运行或假关闭。 |
| A22/A23/A28 | 完成既有守卫：只清已解决维度、保留人工暂停/数据/绑定；新方案准入独立于日常恢复。 |
| A29 | 完成对账/模型/coverage当前确认；通用旧状态入口完整快照契约仍部分。 |
| A30 | 完成repair任务/重试/取消幂等；对账确认成功后重放统一幂等响应仍部分。 |
| A31/A32 | root AI owner；本服务提供事实和受控动作，不另建绕过写接口。 |
| A33 | 后端重检/稳定refs完成；前端返回定位由root测试。 |
| A34 | 状态/coverage已有失败回滚测试；repair请求/取消补回滚保护，逐对象输出审计同事务。 |
| A36 | 日期工作引擎完成，源事件保障部分：`test_dispatch_failure_and_backend_restart_keep_persistent_work_for_later_scan`、`test_event_during_check_keeps_a_new_requested_version_and_requires_a_new_task`；freeze requested→coverage只消费该版，更高版本继续queued。所有来源事务 hook 与精确日期覆盖仍缺，见下文。 |
| A37 | 周期job不按自动开关排除已覆盖账户，真实资金账户用例证明不下单也存真实报告；手工成交/现金/持仓所有写入口的事件 hook 尚未齐备，不宣称实时触发全覆盖。 |
| A38 | 读取失败退避完成：`test_transient_read_failures_persist_one_five_fifteen_minute_retries_then_need_review`、`test_structured_source_read_error_uses_retry_budget_instead_of_becoming_a_completed_business_check`、`test_waiting_execution_has_a_persistent_next_check_without_consuming_error_budget`。首次失败后3次自动重试，1/5/15分；第4次失败 needs_review/next=null，GET返回次数/错误/下次计划。调度关闭时不会假称已经重派。 |
| A39/A40/A41/A42 | coverage/binding/history owner的独立核验、接受审计确切绑定、旧代拒绝；本轮中央HTTP集成验证授权/指纹/资产不变/单独启用。 |

## 最终 v10 对应后端核查

| 原型位置 | 后端实际支持与边界 |
|---|---|
| `fragment.html:336 reconBody`，执行链/持仓/资金分开 | `ReconciliationReport`/中央schema透传真正三项 checks、来源活动、差异与缺证据，preview只读；无真实历史义务时 execution_expected=null。缺记录可导航当前覆盖审核，未批准漏跑处置明确 disabled；补证单与关联写接口尚缺。 |
| `fragment.html:345 batchStats`、`:360 batchResultBody`、`:364 dataBody` | 逐标的/逐指标全部事实、冻结范围预览、真实任务和重检；任务成功/重检结果分开，失败或依赖未完成只重试对应对象。新增任务持久列表与取消；返回原条目靠真实 issue_id/task/report/audit refs。冻结模型/外部映射执行器不足时 disabled，不复刻原型演示成功。 |
| `fragment.html:301 queue`，作业等待/失败/人工复核 | queue同源可信日历、当前基线/代次与最早缺口；job持久工作/退避/实际task结果可查，不按接管开关排除账户。当前闭环与A36/A38剩余边界如下。 |
| `fragment.html:278–290` 的 healthy/waiting/error/readonly 场景 | runtime的phase/recommended_action明确无需审核、等待覆盖起点、等待执行与阻断；READ_ERROR不填0或放行。HTTP按现有actor/access hook调用，但中央`_ensure_portfolio_access:960`默认兼容空实现，尚非完整账户级RBAC；严格身份开关和hook隔离测试不能证明生产角色系统已建设。 |
| 原型历史未知→当前可信资产→新基线→单独启用 | coverage/activation为其他owner；本模块真实HTTP覆盖接受后尚未启用、当前范围审核、旧报告冲突、资产不变与未知日历不猜。接受基线不等于自动恢复、旧期通过或G5准入。 |
| `fragment.html:518 historyDetail` 与恢复审核 | repair审计保留实际actor、UTC记录/业务日期、逐对象重检和dimension_recovery；state确认仅解除对应维度。完整历史抽屉与真实来源由history/root owner核验；本报告不将演示时间线当真实历史。 |

## A36/A38 的实际保证与未接通边界

- 需求写入 helper 的事务由调用者提交；即使没有启用 schedule，已有当前覆盖的需求仍落日期行，不伪造任务已创建。只推进匹配的真实 enabled schedule；GET 状态不做巡检、不修改任务。
- 任务创建前持久化日期工作，绑定真实 `task_id`；派发失败和后端启动孤儿标记为失败后，后续真实扫描读取工作行再派。仅重启且 scheduler 未继续运行不等于已重派；本轮没有在运行环境启用作业。
- 核验开始冻结 `requested_evidence_version`；提交传 `expected_requested_evidence_version` 与 cursor row version/generation。coverage owner 保证只消费冻结版本，新需求保留并阻断连续推进。本 job 不会将更高需求覆盖成 done。对应不可变报告测试由 coverage owner及8文件组合回归共同覆盖。
- 取消只影响未完成工作；已保存报告和成功审计保留。取消期间新需求更高时保留 queued；自然重放不复活旧 cancelled 任务，人工重新检查可以产生新请求。
- 全局五分钟任务已有注册、默认 seed 和派发代码，但真实调度运行、部署表结构、重启巡检进程是否运行不能用隔离测试替代。
- 当前 `request_reconciliation_check` 调用仅见 `portfolios.py:608`、`auto_trade_task.py:554`、`portfolio_execution_task.py:281`，且为成功来源提交后的独立 best-effort 事务。撮合/终结、手工现金与持仓、补证完成、失败/取消任务的所有来源并未原子接入；来源已提交而 hook 前崩溃的窗口仍存在。默认 helper 选择最早未核清日，旧已核清日的晚到事件还需传入准确 `trade_date`。因此 **§5.9 主触发和A36全系统不丢事件仍标部分**。
- 市场业务截止/结算政策、调度失联专用运维 ActionItem 尚未实现；不可把普通 waiting 无限重试当作完整超时治理。

## 已执行验证

1. 本轮新 repair history/cancel/restart 的3个隔离服务测试通过；runtime阶段2项、聚合规则1项通过；ActionItem engine-only语义测试通过。
2. 第一组5文件回归143条：134通过，9项暴露旧mock未提供新增读源及跨午夜日期fixture。已修fixture，不放宽生产校验；随后真实中央HTTP+ActionItem API **32条通过**。
3. 扩展回归发现旧 job 正向fixture伪造 `calendar-real` 版本，改为真实可信日历计算版本；另测出 ORM date 与 task JSON ISO 日期比较不一致，生产改规范化指纹后重跑，未放松代次校验。
4. 最终8文件组合 **231 passed in 172.96s**：`tests/test_portfolio_inspection.py`、`test_portfolio_inspection_reconciliation_services.py`、`test_whitebox_portfolio_action_items.py`、`test_whitebox_portfolio_action_items_api.py`、`test_portfolio_inspection_workflow_http.py`、`test_portfolio_reconciliation_job.py`、`test_portfolio_reconciliation_coverage.py`、`test_portfolio_reconciliation_activation.py`。包含job真实资金账户报告、版本中途追加、派发/重启/退避/取消/读取失败、current_read、跨组合隔离，以及coverage/activation集成。
5. 数据维度恢复补丁后受影响3文件 **103 passed in 49.61s**：inspection37、reconciliation_services41、workflow_http25；新增健康/旧数据/READ_ERROR三分支，部分任务已产生健康标的时按现行策略恢复data/score，但独立锁保留。
6. `python -m pytest tests/test_automatic_plan_resume_public.py -q`：**14 passed in 11.13s**。根合并431项暴露的11项旧resume断言要求多天历史真实补单，与§5.7冲突；已逐条改为历史只诊断，逐表比较订单/成交/现金/持仓/决策/冻结计划不变，涵盖旧全成/部分成、取消、相反信号/止损、补旧评分和新标的；原3项低层真实幂等/部分成交测试保留。当前有效T+1原计划的正向（`:326`）使用真实来源完整日历、实际 ridge 评分/配置/冻结模型和未完成原意图，公开 API→真实 worker→原单真实成交，重复请求不再成交、不新增决策或计划；未 mock 下单或生产门禁。

真实运行环境、数据库部署、历史源数据可恢复性、定时任务实际启用和全系统源事务hook未验证，仍按上述真实依赖判部分。
