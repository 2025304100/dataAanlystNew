# 历史查询与账户覆盖接续逐条核查

核查日期：2026-10-23。责任范围：主设计 §5.10、§9 全部、§10.8、相关 §11，以及最终 v10 的历史查询、原报告回看、历史未知接续和返回交互。以下结论基于源文档、原型、当前实现及隔离测试，不以主设计 §20 的实现自述、静态截图或模拟的成功响应代替完成证据。本轮没有访问生产账户、执行生产迁移、补造历史审计或修改交易账本。

## 核查依据和判断边界

- 主设计 `docs/组合交易检查与对账-功能闭环与交互重构设计-2026-10-21.md:405`（§5.10）、`:727`（§9）、`:957`（§10.8）、`:996`（§11）、`:1043`（§11.1）。
- P0 热修清单 `docs/组合交易双跑验收-真实性与准入安全-P0热修清单-2026-10-22.md` 全文，重点 HF-01、HF-02、HF-07 和“原件/原哈希保留、追加式失效、不把历史采用改成当前准入”。本轮不修改 G5 准入或失效逻辑。
- 最终原型 `docs/prototypes/portfolio-reconciliation-v10.fragment.html:458`（历史未知）、`:465` 起（历史分类和材料）、`:518`（历史详情）、`:523`（筛选）、`:539`（关联和返回）、`:586`（基线审核）。该文件明确是演示，未把其中审批人、金额、报告当实际数据。
- 工作区未发现适用 `AGENTS.md`。保留原 dirty 内容；本轮只改 history/coverage owner 文件和对应测试，没有改 Workbench、DataPanel、client、中央 router、runtime、reconciliation、job 或 G5。

状态含义：“完成”指代码及相应隔离测试已满足该条；“部分”指已有有效分支，但缺真实来源、跨模块消费或运行证据；“缺失”指没有可使用的真实能力；“偏差”指修前行为与条款冲突。完成不代表生产已部署或真实数据已迁移。

## 本次发现、修前和修后

| 编号 | 条款 | 修前 | 本次修后及证据 |
|---|---|---|---|
| H-01 | §9.1/9.1.1 原报告和来源可定位 | 部分：保留 report/task 引用和原审计 JSON，但未传 `onOpenReference` 时只有文本，不能打开材料 | 新增 `app/services/portfolio_inspection_evidence.py:64` 的只读定位；`app/api/routes/portfolio_inspection_history.py:110` 提供 `/material`；HistoryPanel 默认原位打开，不要求中央回调。只接受当前组合、当前水位的确切审计引用；错误版本/猜测引用 404。 |
| H-02 | §9.1.1 不可变原报告 | 部分：原索引有版本，但没有读取版本化账户报告的适配器 | 按确切报告 ID/版本读取 immutable report，基线按 ID/内容指纹核查；新报告产生后仍可读旧版。完整原报告缺失仅返回 `partial + audit_snapshot`，说明缺什么，绝不重新生成当前报告。G5 仅在显式 G5 引用/类型上下文中读原 JSON/原哈希，明确“不是当前准入有效性”。 |
| H-03 | §9.2 导出证据包/时间线 | 缺失 | 新增 `portfolio_inspection_evidence.py:201`、history route `/export`。全量筛选后导出所有匹配批次、整批/命中对象、原事件、原版本材料、缺失来源、例外/未知、旧代基线与报告；保留审计水位并标明当前游标/队列属于导出时状态。附件为 JSON，带脱敏后 payload SHA-256，响应 `no-store`。 |
| H-04 | §9 按天时间线和上次对账快捷视图 | 缺失 | HistoryPanel 按北京时间实际发生日分组；`since_last_reconciliation` 仅从同组合真实 `RECONCILIATION_RESULT/status=PASSED` 的发生时间取起点，不取业务日期、最近失败或其他组合结果；无可用审计明确返回筛选错误。 |
| H-05 | §9.1 主体、失败、AI 筛选 | 部分：动作/审核/执行/操作人/关键词已有 | 补 `actor_type/failure_code/ai_assisted`，只按显式结构化字段命中；补 `INSPECTION_AI_ANALYSIS` 中文名称和操作类型。旧 AI 会话缺组合归属不猜造（见依赖 D-03）。 |
| H-06 | §9.1.2 组合级配置的交易日不适用 | 偏差：空对象时仅显示“未知或不适用”，未明确区分 | 批次新增 `trade_date_index_status`，明确的组合级配置/范围审核显示“所属交易日不适用”；仍不参与特定交易日/标的命中，不用事件日期补业务日期。 |
| H-07 | §9.1.2 结构化公共覆盖区间 | 偏差：逐对象缺日期时只继承显式共同单日，遗漏共同 `trade_date_from/to` | `_objects` 现在仅给无个体日期的对象继承明确公共区间；有自己日期的对象不混入公共区间。证券和日期仍在同一对象求交。 |
| H-08 | §9.1.1/9.1.2 批次归集 | 部分：batch/operation/plan/correlation 归集已实现 | 补明确 `task_id` 为真实归集键，不因跨时段/重试新请求而丢链；仍不凭 issue、同名标的或相近发生时间强合并。 |
| H-09 | §9.1.1 分页失败重试 | 偏差：追加页失败后通用重试可能按旧已应用页清空列表 | 保存失败请求的 page/watermark/append 标志；重试同页时保留既有批次并稳定去重。详情、材料、导出都有请求序号和组合作用域校验。 |
| H-10 | §9.1/§10.8 时间口径 | 部分：按 UTC-naive 契约转 +08，保留原时间，但旧来源时区未标 | 新 coverage 写审计明确 `source_timezone=UTC`；旧审计未标源时区展示核查警告并保留原值，不从文字猜时区或静默更改旧事实。 |
| C-01 | §5.10 先核证、不可恢复后申请 | 部分：进入 CoveragePanel 就直接展示申请表 | 先显示核查历史证据与导出；本人点击“已核查仍无法恢复，登记历史缺口并申请新基线”后才展开申请，组合切换重新开始。 |
| C-02 | §5.10/§11.1 新代与旧缺口同时可见 | 部分：只有笼统“旧期未知”提示 | CoveragePanel 展示所有保留基线的实际缺口区间、原因、尝试来源、材料引用、接受审计和指纹；新起点/连续核验为空与旧 UNKNOWN 同时展示。 |
| C-03 | §10.8 日历修订与连续推进 | 偏差：当前日报告会版本化，但重跑晚日仍可能消费旧日历前序报告 | `_continuous_coverage` 对每个前序报告核对当前可信日历来源版本及实际行前缀；修订后停在待重验的早日，重验后恢复连续推进。原报告保留。 |
| C-04 | §9.2 审计前后状态结构化 | 偏差：旧审计序列化器不能处理 Python date，部分游标 before/recalculation after 会退成 `_raw_repr` | `_cursor_snapshot` 输出 ISO 日期，重算 after 显式 canonical；保留结构化前后游标。历史对已有 `_raw_repr` 只警告，不从文本重构通过事实。 |
| C-05 | §9.2 拒绝尝试独立留痕 | 缺失：业务回滚不证明拒绝审计保存 | Coverage 接受路由在业务回滚后用独立事务保存拒绝，返回 `failure_audit_saved`；审计库也失败时明确 false，不能声称已保存。仍缺独立于数据库的持久失败通道（D-02）。 |
| C-06 | A29/A30/A42 版本与幂等 | 部分：同 key 同决定幂等，未专门阻止旧接受收据被当成当前范围 | 原接受已被新代取代时返回 `COVERAGE_ACCEPTANCE_SUPERSEDED`，原收据保留；记录接口支持 `expected_cursor_row_version`，明确失败检查不能记录 PASSED。 |
| C-07 | A36 核验中途有新证据需求 | 偏差：保存时会把最新 requested 全算为已消费，可能吞中途新需求 | 新增 `expected_requested_evidence_version`：只消费核验开始时冻结版，更高 requested 保留 queued，连续游标不推进；相同 hash 的新 demand 也保存相应报告版本，旧冻结版不能覆盖更高 processed。reconciliation/job 透传由 inspection_backend 负责。 |

## §9 逐条验收矩阵

| 条款/要求 | 修后状态 | 当前文件/测试证据 | 尚需的真实依赖或边界 |
|---|---|---|---|
| §9 全部写操作有审计：状态、对账、确认、绑定、G5、修复、处置、证据关联、例外、AI | 部分 | History 仅读实际 DG 事件；`portfolio_inspection_history.py:334` 兼容 original_action 并保留 source_action；coverage 成功/拒绝有真实审计 | 全部写入口由各 owner 执行；查询器不能补造未落库事件。旧 AI 多来源接入见 D-03，禁用例外不得造已授权事件。 |
| §9 审计字段 actor/action/before/after/reason/review_note/corr/trade_date/hash/bypass/outcome/failure/report/task/actor_type 等 | 部分 | `HistoryEvent` 保留原快照/属性/理由；coverage 新写补主体、源时区、bypass；C-04 修复结构化日期 | 历史字段确实缺失保持未知；不能用当前值回填旧事件。各业务写入字段完备性需分别验收。 |
| §9 中文名、按天分组、当前问题相关审计、上次对账快捷视图 | 完成（查询/面板） | `portfolio_inspection_history.py:ACTION_LABELS`、`:512`；`InspectionHistoryPanel.tsx:328`；同组合 passed 审计快捷视图测试 | 当前问题入口和实际导航由 Workbench 集成，见返回项。 |
| §9.1 默认可读的谁/何时/对象/动作/理由/结果，因果链 | 完成（已留痕事实） | `InspectionHistoryPanel.tsx:363`、`:389`，分别显示审核/执行、命中对象、理由、前后状态、关联来源、原审计；完整批次不裁剪 | 未写入的“发现/建议/执行”环节无法凭文本补链。 |
| §9.1 操作人/动作/失败/AI、实际发生时间与所属日分离 | 完成（显式字段） | `HistoryFilters`、`_matches:478`、`queryFrom:55`；主体/AI 只按结构化元数据匹配测试 | 未标 actor_type/ai_assisted 的旧记录不推断。 |
| §9.1 运行/报告/订单/成交/流水可定位，保留状态说明 | 部分→主要适配完成 | `/material`：run/order/trade/ledger/strategy snapshot/decision evidence/task、immutable account baseline/report、显式 G5 原件；`test_exact_retained_source_row_cannot_cross_portfolios_or_masquerade_as_snapshot` | 某些 issue/operation/外部文件/已归档任务只有原索引，明确 unsupported/archived/missing，不开放任意路径或 URL 读取。 |
| §9.1.1 当前组合隔离，切换清引用/材料 | 完成 | `_load_batches:427` 先 portfolio WHERE 后聚合；material 先事件归属、再来源归属；前端 token/scope 校验；旧组合列表/材料迟到测试 | 组合归属级 RBAC 是现有中央访问策略的真实依赖（D-01）。 |
| §9.1.1 最近30天默认、完整日期秒/时区、独立业务日 | 完成（有旧源时区警告） | `normalized_filters:119`、`_at:104`；前端 +08 参数与 Intl Asia/Shanghai；保留 source_occurred_at | 旧默认 `datetime.now()` 写入但无时区 provenance 的来源无法事后判定真实偏移（D-04）。 |
| §9.1.1 本次处理/历史、时间/类型/审核/关键词/操作人/重置 | 完成 | HistoryPanel tabs/filters；实际后端过滤在所有记录上执行 | 没有把当前几条演示数据当实际历史。 |
| §9.1.1 真实批次依据，不凭时间合并 | 完成 | `_groups:287` batch/operation/plan/task/corr；`test_exact_task_identity_groups_retry_events_without_time_coincidence` | 问题 ID 本身不等于同一次审核/处置批次。 |
| §9.1.1 原位详情、当时理由/版本/能力前后/任务 | 完成（已有快照） | `get_inspection_history_batch:554`、BatchDetail、MaterialView；旧报告重检后仍读 v1 的测试 | 原快照没保存完整报告时显示 partial，不冒充完整原报告。 |
| §9.1.1 全查询后分页、水位、展开/去重/返回来源 | 完成；性能部分 | `select_inspection_history_batches:512`→分页；stable event watermark；append 失败同页重试；详情水位缓存 | 目前全量扫描本组合 JSON；大体量索引优化见 D-05。 |
| §9.1.1 审核 pending/approved/rejected/revoked 与执行两列 | 完成 | `_review:185`、`_execution:196`、`_object_overall:316`；“同意但失败”前后端测试 | 同意不映射为成功，旧通过不授予当前能力。 |
| §9.1.1 映射确认单独留痕/冻结身份/核实依据 | 部分（读取兼容） | 原 before/after/attributes 可保留；不把确认映射当市场数据通过 | 实际映射写接口/证据生命周期由数据 owner 验收；历史查询不会替其制造索引或审批。 |
| §9.1.1 0通过且失败/依赖=失败，整批结果不被子集改写 | 完成 | `test_batch_result_uses_complete_object_rechecks_and_zero_pass_is_failure`；修复 original_action/reinspection_status 的真实适配测试 | 差异列表不等于整批对象，旧 diffs-only 标记范围未知。 |
| §9.1.1 历史只读；缺失/归档/读取失败区别 | 完成 | 仅 GET material/detail/export；404/503 分开；retention archived/inaccessible 优先；读异常不返回空页；不写查询审计 | 没有历史事实时明确空/未知，未创建演示历史。 |
| §9.1.2 名称/代码多选、市场身份、OR/维度AND | 完成 | `search_inspection_history_securities:573`、`_object_matches:463`；前后端多选测试 | 当时名称没保存时标 current_catalogue；不会把当前目录名当历史冻结名称。 |
| §9.1.2 证券和日期命中同一对象、未知不猜 | 完成 | A/10-19+B/10-20 不命中 A/10-20；显式共同区间测试；配置日不适用；文本证券/日期不猜测试 | 没有对象索引的旧记录只能不限该维度查到。 |
| §9.1.2 整批N/命中M、对象数/标的数、重试关联 | 完成 | 详情保留 full objects + matched_objects；batch overall 不重新按子集计算；稳定分页测试 | 未保留完整对象范围的旧 diffs 不假报完整成功。 |
| §9.1.2 issue/corr/report等真实关联；来源上下文 | 完成（当前可定位字段） | `_related:449`；`issue_ids/original_action` 适配；Panel 展示实际关联条件 | 没共同引用不把同名证券/时间近当关联。 |
| §9.1.2 清关联保留显式筛选和返回来源 | 完成 | `clearRelation` 仅移除关系参数，不清 security/date/operator；frontend `keeps both time meanings and clears only structured relation conditions` | Workbench 没传清空父 context 的回调，来源上下文保留。 |
| §9.1.2 返回原事项、步骤、滚动、选择、草稿 | 部分→主要链完成 | `PortfolioInspectionWorkbench.tsx` 保持已访问面板 mounted；showHistory/backHistory 保存原 panel/step/trigger；已有 CoverageFlow/Workbench 导航回归 | 真实浏览器跨抽屉滚动/焦点/批量选择和审核草稿由根代理进行截图/交互 QA；不能只凭子组件测试声称整链全部完成。 |
| §9.1.2 无关联/被其他筛选排除/读失败三种空态 | 完成 | relation_status `no_related_history/filtered_out`；对应后端测试和前端失败重试测试 | 保留当前筛选，不短暂显示“无关联”。 |
| §9.2 状态/基线/报告/日期指针/游标/成功审计原子 | 完成（coverage owner） | `accept_reconciliation_coverage:367`、`record_coverage_reconciliation:586` nested transaction；baseline/report/recalculation audit failure rollback 测试 | 其他业务写入的原子性由其 owner 验收；未执行真实 MySQL 多连接压力测试。 |
| §9.2 拒绝/失败/重试独立保存，不能以回滚证明 | 部分→有明确独立事务 | `portfolio_inspection_coverage.py:31`；拒绝独立保存成功及审计中断 false 的测试 | 数据库本身不可用时无独立持久 outbox；明确未确认保存（D-02）。 |
| §9.2 旧事件不重写、只读不制造业务审计 | 完成 | 原 action/source_action 分开、原 JSON 保留；HTTP GET count unchanged 测试；导出/材料不写审计 | 其他系统若自行修改旧行，查询层不承诺防直接 SQL 篡改。 |
| §9.2 证据包/处置线/脱敏/缺失/例外导出 | 部分→功能完成，权限依赖未闭环 | `/export` 完整选择共享，嵌套 secret/credential JSON 脱敏；Panel 下载失败单列；Coverage 导出把未提交预览与正式历史分开 | 现有 `_ensure_portfolio_access` 默认放行，不具备真实角色/归属 ACL，不能称为角色级权限验收完成（D-01）。 |

## §5.10、§10.8 与 v10 覆盖矩阵

| 条款 | 修后状态/实证 | 依赖/限制 |
|---|---|---|
| §5.10 临时读取失败不填零、不推进 | 完成：源表/列缺失形成 blockers；读取异常503；cash/义务未知为 null。`test_missing_source_and_external_account_fail_closed_without_equity_fallback` | 自动退避/升级由 job owner 本轮建设，不能仅因表有 next_check 字段就判断完成。 |
| §5.10 证据存在但未关联时真实展示、校验、关联前后审计 | 部分：历史 material 可定位真实 run/order/trade/ledger；原关联及状态可回看 | 当前核对证据关联写入口、来源可用性核验属于 reconciliation/inspection owner；无真实来源不做快捷“确认已关联”。 |
| §5.10 文件/备份补证单、范围、校验摘要、审核 | 缺失（有真实阻断/导出出口） | 当前没有可读取的备份保管库、恢复任务或带校验的补证单来源；表单 evidence_refs 是申报引用，不能当备份已验证。onRecoverBackup 仅真实接入时显示。 |
| §5.10 不可恢复但当前可核验→登记→本人接受→新代 | 完成（CNY内部模拟账本适配） | `preview_reconciliation_coverage:284` 重建独立现金、逐标的持仓/成本、费用和未决义务；真实来源指纹重验，个人理由/确认；来源不齐无法接受。 |
| §5.10 当前账差/义务/未知不被新基线抹平 | 完成（拒绝）；恢复能力部分 | 有 cash/position/unknown origin/pending order/意图/active task/lease/future source 阻断测试；提供修复/历史核查/导出。真实备份恢复及独立新账户入口未接（D-06）。 |
| §5.10 原缺口与旧代保留、仅新范围核验 | 完成 | 新 baseline supersedes 链、generation+1、verified=NULL、history_gaps 联合保留；不写旧 portfolio checkpoint；old-day/report 验证保留。 |
| §5.10 不顺带模型/运行确认；新范围独立启用 | 完成（已核对跨模块实证） | `app/api/router.py:83` 挂 workflow；`portfolio_inspection_workflow.py:115/122` 激活路由；`portfolio_reconciliation_activation.py:172` 接受源重验。105项联合后端回归及2项CoverageFlow通过，不存在仅凭accept就启用。 |
| v10 主操作先核证、两组申请分支、成功不叫差异清零 | 完成 | CoveragePanel 分段申请；不可接受分支列具体 blockers；成功显示尚未核验任何日/旧期仍未知；可重试网络保留相同幂等键，来源变化要求新预览。 |
| §10.8 两种基线分开 | 完成（账户 owner），策略来源另验 | account_baselines 不用净值汇总或模型激活当期初；策略最后接受事件/冻结执行器由 binding/G5 owner，历史材料只读不改变采用。 |
| §10.8 baseline四表/约束/不可变报告 | 完成（代码及隔离迁移测试） | `app/models/portfolio_reconciliation_coverage.py:20/44/66/93`；Alembic `2026_10_22_0074_wps_0023_074_reconciliation_coverage.py`；唯一键、FK、各代原件、ORM不可变保护；未执行生产迁移。 |
| §10.8 work_state与verdict分层、休市不抹账户异常 | 完成（record分支） | 保存 report 和 day state；休市已有账差阻断、不推进市场交易日；不把 closed market 当零活动。 |
| §10.8 当前代、可信日历、连续而不跨缺口 | 完成 | `_continuous_coverage:532`；晚日先过只存报告，早日解决后连续推进；calendar缺行/未知/修订/未来日拒绝或停止；旧代报告不消费。 |
| §10.8 原子提交、租约、row_version、来源水位 | 完成（服务约束），运行并发部分 | 共同 Portfolio/当前cursor锁、来源范围 current reads、预览指纹、expected cursor version、冻结requested evidence version；所有成功/重算失败回滚测试 | 实际多worker/生产锁与租约运行需要部署后验证，未凭SQLite单测宣布生产并发验收。 |
| §10.8 新基线verified空、next/blocked/pending派生 | 完成（coverage） | record 从当前代日期行和真实日历算 first_blocked/verified，不改旧游标；get_active 校验接受审计/基线hash；新代刚接受无已核验日。 |
| §10.8 证据版本、新事件、重启/补派/有界退避 | 部分，跨 owner 联动中 | C-07 已保护冻结需求；日期表有真实requested/processed/attempt/next/error字段；inspection_backend 本轮负责 job持久demand、1/5/15退避和重启补派 | 必须以其job测试/HTTP真实结果验收，不能以本报告宣告调度全闭环。 |
| §10.8 UTC naive/展示市场时区/业务日不混用 | 完成（新写）；旧源部分 | DB写 `_utc`；API预览UTC-aware，基线as_of输出带UTC；UI Intl Shanghai，日期只从业务/日历字段取 | 旧无源时区 provenance 保留不确定性，外部多市场日历未接（D-04/D-07）。 |
| §10.8 消费者迁移顺序、旧checkpoint不是全史证明 | 部分（代码消费者接续有实证） | activation/runtime/reconciliation由其他owner消费get_active；覆盖返回generation/start/baseline/gaps，不改旧checkpoint；联合HTTP/activation测试通过 | 实库建表、只读盘点、旧数据迁移、发布顺序与现场验证未执行，生产启用仍需独立部署验收。 |

## §11 相关验收结论

| 场景 | 本范围验收证据 | 结论 |
|---|---|---|
| A1 连续下一日真实通过 | coverage continuous pass test | 服务完成；真实账户运行不在本轮操作范围 |
| A5 休市但有账户活动 | `test_generation_boundaries_empty_pass_and_nontrading_account_exception_are_guarded` | 完成，休市账差阻断 |
| A6 日历未知 | 缺真实来源/缺日期/未知值测试；无weekday兜底 | 完成，不能推进 |
| A17 源读取失败 | history503、missing-source preview、未知null | 完成，不填零 |
| A20 晚日先过 | out-of-order reports test | 完成，不跳早日 |
| A22 其他锁独立 | acceptance不恢复；activation联合服务/HTTP测试 | 完成相应跨模块测试；生产事实仍独立判断 |
| A29 新源/版本冲突 | fingerprint/source revalidation、row_version、late-material scope tests | 完成，草稿不当授权 |
| A30 幂等/重复请求 | accept/report retry、same key wrong request拒绝、旧代收据已superseded拒绝 | 完成 |
| A34 审计失败 | baseline/report/recalculation事务回滚 + 独立失败审计保存/失败false | 完成服务行为；独立DB外失败存证仍缺 |
| A36 新证据中途到达/重启/派发失败 | coverage冻结requested保护及专门用例；job归其他owner本轮实现 | 部分，等待job实证，未把模型字段当作已完成 |
| A37 接管关闭仍核验账户 | preview读取真实账本/成交/义务不依赖接管开关 | 账户来源核验完成；自动触发归job owner |
| A38 暂时失败有界退避 | failure不当零；job owner实现持久预算/1、5、15分钟 | 部分，等待job测试结果 |
| A39 当前可信、历史不可恢复 | preview/accept/old generation保存 + workflow activation真实HTTP | 完成CNY内部模拟源路径 |
| A40 当前也不可信 | 参数化真实差额/未知入金/未决单/活跃任务拒绝 | 拒绝完成；备份/独立账户真实出口依赖 |
| A41 已接受策略不能用最新保存代替 | 账户service没有调用最新模型当期初；策略源owner测试另核 | 非本owner判定范围，不重复宣称完成 |
| A42 跨代报告/刚接受无verified | generation guards、oldreport保留、verified空、sourceactivation重验 | 完成 |
| §11.1 新代+旧未知并见、失败分支、前后端/HTTP | CoveragePanel真实区间/原引用；14项组件测试；105项联合后端测试 | 已有实证；未将静态原型/截图当业务验收 |

## 测试记录

已完成的本地隔离回归（测试使用临时 SQLite/组件 mock，不读取或写入生产账户）：

1. `python -m pytest tests/test_portfolio_inspection_history.py tests/test_portfolio_reconciliation_coverage.py tests/test_portfolio_inspection_workflow_http.py tests/test_portfolio_reconciliation_activation.py -q --disable-warnings`：**105 passed**。其中 history 26、coverage 28、workflow HTTP 25、activation 26；包含真实HTTP基线接受和独立启用，非仅UI mock。
2. `npx vitest run src/components/portfolio-trading/inspection/__tests__/InspectionHistoryPanel.test.tsx src/components/portfolio-trading/inspection/__tests__/InspectionCoveragePanel.test.tsx src/components/portfolio-trading/inspection/__tests__/PortfolioInspectionCoverageFlow.test.tsx --maxWorkers=1 --minWorkers=1`：**14 passed**（history 9、coverage 3、flow 2）。新增原报告原位材料、旧组合迟到材料、追加页失败重试、导出失败保留列表；已同步不可恢复确认步骤。保留 root 新 mock 未覆盖。
3. `npx tsc -b --force --pretty false`：通过。
4. 在以上运行后，针对 C-07 新增 `test_event_arriving_during_reconciliation_is_not_consumed_by_older_frozen_demand`，history+coverage **55 passed**（26+29）；最后补强旧冻结版幂等保留更高需求后，专门增量 **1 passed / 28 deselected**。涵盖更高requested保留queued、不提前推进、旧冻结版幂等不吞需求、后续新报告版本和旧版拒绝。root 的最终整体相关回归还需覆盖 job/reconciliation 参数透传。

## 仍需真实依赖，不能由查询层伪造

| 编号 | 依赖/未完成部分 | 当前诚实行为/需要的后续工作 |
|---|---|---|
| D-01 | 实际角色/组合归属 ACL | `_ensure_portfolio_access` (`app/api/routes/portfolio_governance.py:941`) 当前默认放行；新接口复用现有认证/范围校验并做凭证脱敏，但不能声称角色级授权已验收。需要账户归属/角色真值和访问控制策略。 |
| D-02 | 数据库失效时独立持久失败审计 | 业务不成功，`failure_audit_saved=false`；需数据库外的受控失败通道/outbox和恢复对账策略，不能假称独立事务一定写得进去。 |
| D-03 | 旧 AI 审计的明确组合/批次归属 | `ai_action_audits` 自身无portfolio_id或多来源查询水位契约；旧未关联会话不猜。root新增 `/inspection/analysis` 的真实 DG 事件可查询，缺旧关联仍部分。 |
| D-04 | 旧审计源时区 provenance | 保留原source时间，按现有UTC契约展示并警告；不能知道无标记的旧 datetime.now 是哪一时区，不能全量静默偏移。 |
| D-05 | 大数据量持久对象索引与执行计划 | 目前是每组合真实JSON全量取数后结构化过滤/聚合再分页，语义正确，但没有证券/日期联合物化索引和生产级查询性能实证。需增量索引迁移/重建、水位和查询计划验收；不能在当前页文本搜索代替。 |
| D-06 | 可信备份补证、账本修复、独立新模拟账户真实入口 | 已提供历史/材料/完整证据导出以及注入式回调；没有真实备份保管库或可靠新开户来源时不显示能自动恢复的按钮，不继承未核实资产。中央挂载实际入口由root负责。 |
| D-07 | 外部账户、多币种、多市场来源 | 当前只支持人民币内部模拟账本，必须真实完整入金/订单/成交/持仓/义务链可独立核算；外部账户缺独立对账单时明确不可接受，不能拿净值汇总代替。 |
| D-08 | 实库迁移/遗留盘点/发布与真实运行 | 迁移仅隔离测试；未运行生产迁移、未新建真实基线、未调整生产游标/锁/准入。需要按§10.8顺序真实部署和观察；原数据不能被此报告视为已修复。 |

## 本轮新增/兼容接口

- `GET /portfolios/{portfolio_id}/inspection/history/material?source_event_id=...&kind=...&ref=...&version=...&snapshot_event_id=...`：确切审计引用的只读材料；返回保留状态、材料类型、说明、缺失清单、原审计与内容指纹。无真实引用404，数据库读失败503。
- `GET /portfolios/{portfolio_id}/inspection/history/export`：复用原查询过滤和水位，返回 `manifest + payload`，JSON附件；忽略分页只导出完整匹配集合，不能借此跨组合检索。
- 原history查询新增可选 `actor_type/failure_code/ai_assisted/since_last_reconciliation`；原API兼容。
- `InspectionHistoryApi` 新增可选 `getPortfolioInspectionHistoryMaterial` / `exportPortfolioInspectionHistory`。root已接client，组件有默认只读材料回看/下载；接口未接时明确失败，不制造本地演示包。
- `InspectionCoverageApi` 可选复用同一导出方法；`InspectionCoveragePanel` 可注入 `onRecoverBackup/onCreateIndependentAccount`，只有真实连接时显示对应动作。
- `record_coverage_reconciliation` 新增可选 `expected_cursor_row_version` / `expected_requested_evidence_version`，原调用兼容；后者由job在核验开始冻结，由reconciliation透传，不能在保存时重读最新版本冒充已经核验。
