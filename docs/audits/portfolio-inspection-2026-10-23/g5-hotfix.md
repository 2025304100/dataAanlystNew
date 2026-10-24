# G5 双跑真实性与新准入安全核查

核查日期：2026-10-23。范围：独立 P0 热修清单 HF-01～HF-07、主设计第 8、10.4、11 节及 v10 双跑交互。本文基于本工作区源文件逐项核查和隔离数据库回归，不以主设计第 20 节或既有测试通过作为完成证明。

**当前仍没有真实双链执行器、部署构建注册解析器和完整账户/订单计划/风控证明。不能宣布真实双跑已经交付，也不能开放正式新链准入。** 当前策略明确为 `engine-source-v1 / decision_actions_only / admission_supported=false`：真实来源可核验的动作归档可读，正式 `g5_eligible_for_g6` 始终为 false；即使审核释放 HF-07 的治理冻结，也不会补出这些缺失证明。

本轮没有连接运行库盘点报告、执行生产迁移、隔离实际报告、发布版本或提交交易。下面“代码完成”均只指可审查的实现与隔离回归；发布与真实环境验收另列。

## 1. 核查依据与完成口径

| 来源 | 本轮逐条核查范围 |
|---|---|
| `docs/组合交易双跑验收-真实性与准入安全-P0热修清单-2026-10-22.md:31` | §2.1 来源、样本、状态三层证据政策；空分母不适用；版本化最低样本 |
| 同文件 `:85` | HF-01～HF-07 全七条，及其下追加隔离、无旧报告回退、无全局停机等约束 |
| 同文件 `:111`、`:135` | H1～H19 回归与发布顺序、影响盘点、保留原件和回滚约束 |
| `docs/组合交易检查与对账-功能闭环与交互重构设计-2026-10-21.md:613` | §8.1～8.6：目的、引擎链与独立模型输入、基线/候选来源、预检、真实证据、审核与空态 |
| 同文件 `:906`、`:957` | §10.4 接口；§10.8 两类基线中“明确接受的执行方案基线”来源，不以账户基线替代 |
| 同文件 `:1010`、`:1023`、`:1043` | A11/A12/A24～A28/A30/A34/A41 与 §11.1 双跑体验验收 |
| `docs/prototypes/portfolio-reconciliation-v10.fragment.html:459`、`:632` | `dualBody` 与演示启动：引擎来源、无手填、同输入、逐日/逐标的差异、缺能力拒绝、独立切换 |

“缺失”表示当前无真实能力；“部分”表示已补安全边界/可核验证据，但完整条款仍缺依赖；“完成（代码）”不代表已发布或已处置遗留数据。

## 2. HF-01～HF-07 逐条结论

| 条款 | 核查结论 | 本轮实现证据与回归 | 依赖与剩余边界 |
|---|---|---|---|
| HF-01：不存在/跨组合报告 404，不造通过结果 | **完成（代码）** | `app/api/routes/g5_dual_run.py:272` 查询先按组合限定，缺失明确 `G5_REPORT_NOT_FOUND`；`report:{typed_id}` 允许对损坏原件返回真实无效结果。`tests/test_g5_report_safety_http.py` 的 missing/another_portfolio 用例覆盖。 | `replay_id` 仍位于旧 JSON，常规精确查询要遍历；稳定独立索引未建设，不能称主设计 §10.8 索引目标完成。 |
| HF-02：内容无效与存储异常分开，真实任务状态，禁止乐观默认 | **完成当前安全路径（代码）** | `g5_dual_run_audit.py:146`、`:369` 要求显式状态/执行模式、实际异步任务完成状态；查询区分内容 409 与存储 503，pending 只回真实任务状态且统计为 null。`tests/test_g5_report_safety_http.py:266`、`:290`。 | 没有真实 runner 的排队/运行端到端能力；测试任务是隔离持久任务，不是已执行生产双跑。 |
| HF-03：正式入口禁合成；能力发现先于动态 ID；缺 runner 拒绝 | **拒绝支线完成；真实执行器缺失** | `g5_dual_run.py:163` capabilities、`:181` precheck 均声明不可运行；`:247` launch 固定拒绝 `G5_REAL_RUNNER_UNAVAILABLE`，不建报告或假任务；路由排序在 `:272` 动态 ID 前。旧字符串/run_async 仅兼容弃用字段，OpenAPI 不再承诺固定 20 日异步能力。 | 无部署执行器注册、实际构建解析、候选链列表与真实预执行器；不临时拼空 runner。 |
| HF-04：来源/日期/有效样本/真实运行/输入证明 | **部分；正式准入保持关闭** | `g5_dual_run_audit.py:61`、`:95`、`:163`、`:298` 回源接受审计、双侧快照、ScanRun/DecisionRun/DecisionEvidence、Bar/Score、相同 cutoff、PIT、可信日历与版本、实际动作及覆盖；`g5_dual_run_replay.py:40` 版本化 10 日/10 symbol-days 策略；`:253` 空分母 null/NOT_APPLICABLE。`test_g5_report_safety_http.py:243`、`:305` 及 framework `:347`、`:363`。 | 未证明部署产物真实性，未冻结完整期初现金/持仓/未决义务、隔离订单计划/目标仓位、风控输入、模型权重与因子原始依赖。`g5_dual_run_audit.py:452` 显式列五项 `acceptance_blockers`，结果为 `EVIDENCE_VERIFIED_NOT_ADMISSIBLE`；不得把动作来源核验等价完整双链通过。 |
| HF-05：查询、G6、G7、来源切换共用真值，不回退旧报告 | **统一校验与拒绝路径完成（代码）；真实合法准入未交付** | `g5_dual_run_audit.py:369`、`:502` 共用校验 typed/JSON/哈希/范围/策略/来源，始终只取最新报告；G6 `g6_graduated_rollout.py:52`、G7 `g7_operational_readiness.py:89`、来源切换 `auto_trade_dual_run.py:802` 均消费该结果，来源门禁不再复制固定天数/P0 真值。最终 G6 写入使用 current_read、治理锁、报告 ID/hash/当前来源和本人理由。`test_g6_rollout_actions.py:122`、`:132`、`:139`。 | 当前任何真实归档均不得正式准入；G6 原子切换正向仅用明确受控的“未来完整适配器”mock 测服务边界，不是 runner 验收。MySQL RR 与真实并发锁验证仍待发布环境。 |
| HF-06：有效性、样本/分母、嵌套矩阵、全区间/逐日、404 终态 | **后端契约完成；界面由总体验收收口** | `g5_dual_run.py:50`、`:84`、`:145` 保留 policy/scope/source refs/input manifest/样本/分母/symbol_diffs/acceptance_blockers；矩阵为嵌套二维，带 action_order 与全区间聚合；`:217` 提供真实归档列表免手填 ID。界面本轮已有 `PortfolioGovernanceTab.tsx:1773` 报告列表、`:1850` 404 终态、`:1883` 严格 eligible、`:2058` 按日/全区间、实际分母和来源展示。 | 本审计未代替前端 owner 的构建/浏览器验收。自动归因标为 REQUIRES_REVIEW，不是持久人工审核/复验；完整差异处置与复验接口仍缺。列表目前最多 100 条无游标翻页，深历史检索不是完整交付。 |
| HF-07：先只读盘点，冻结新准入，追加隔离，逐组合影响审核与回滚材料 | **能力已实现（代码）；真实盘点/处置/发布均未执行** | 新 `g5_legacy_inventory.py:249`、`g5_admission_governance.py:57`、`:89`、`:189`、`:243`；新模型/迁移 0075 与 `/g5-admission` 路由。默认未审核/缺存储即冻结；三消费者同拒绝。报告隔离/恢复仅追加原 ID/hash、理由、操作者、时间、快照、审计和回滚材料，不重写报告或交易事实。库存 14 项、治理最终 26 项通过。 | 缺 schema 先部署并验证；真实遗留清单与逐组合处置方案必须另行读取、评审。不得把本轮没有运行库数据的事实写成“没有遗留影响”。查询角色仍依赖既有身份约定，见第 5 节。 |

## 3. 主设计与 v10 的具体差距

| 条款/验收 | 当前结论 | 实际证据 / 下一步依赖 |
|---|---|---|
| §8.1 引擎链为主、模型独立固定；仅限制新切换 | **安全边界已实现；完整版本发现缺失** | 服务明确 engine_source，固定双方模型/因子集/规则/配置；HF07 不改变运行状态或交易账本。G7 日常人工暂停恢复与新准入分开，`test_g7_operational_readiness.py:100`。v10 固定候选仅为演示，不复制到真实能力列表。 |
| §8.2、§10.8、A12/A41 接受基线、真实候选、退役/初次基线分支 | **部分** | 验证基线用最后 `FACTOR_USAGE_APPLIED / MODEL_BINDING_CONFIRMED` 接受审计；候选核对本组合最新 save_and_apply。两侧快照/hash变化会失效。缺实际 runner/build registry、可回放范围、退役可回放性及完整初次引擎准入流程。 |
| §8.3 预检逐项实际值/要求/原因/动作、锁与可修复问题 | **部分** | 纯读预检已有日历、所选日期、策略、接受基线/候选实际结果；runner 明确失败，输入证据未知，`can_run=false/precheck_snapshot_id=null`。缺真实执行器可调用性、同一期初账户、实际依赖覆盖、互斥任务和带版本的可执行预检计划；不产生假预检快照。 |
| §8.4、§10.4、A11/A24 选定区间、真实隔离执行、计划/完成/失败日、幂等任务 | **真实执行缺失；拒绝路径完成** | 归档核验/预检按可信 SSE 来源和实际日期行枚举，不用 weekday；无真实 runner 故无 12 日/跨国庆的生产执行正向可验。完整 comparison_kind+双快照+precheck version+idempotency 启动契约尚未启用；legacy launch 为兼容拒绝入口。 |
| §8.4 无正式订单/成交/现金/对账游标写入 | **本轮安全路径完成** | G5 launch/precheck/history 无真实执行；库存只读；HF07 写入只落治理事件/控制游标/治理审计。未借用日常交易入口补造双跑。 |
| §8.5、A25/A26/A27 真实样本、来源差异、范围失效 | **基础归档核验完成；完整报告/审核仍部分** | 零样本不造 100%；Bar/Score、accepted baseline、build/source 元数据、可信日历修订会使原报告失效，原 JSON/hash保留。未有真实订单计划/风控/完整因子血缘差异、持久人工解释/复验链。 |
| §8.5、A29/A30/A34 独立本人切换审核与材料冲突 | **服务写入边界已实现；真实准入正向受缺能力阻断** | G6 最终复读 exact report ID/hash、当前来源和快照范围，理由必填且后台 actor 不可代签；治理冻结与切换串行。HF07 有请求幂等、版本冲突、审计失败 savepoint 回滚；G6 重复已处同一来源可 NOOP，但没有覆盖所有动作的统一 request_key 收据体系。 |
| §8.6、A28 缺能力/缺报告/遗留虚拟空态，不锁死已有日常恢复 | **缺能力与拒绝路径完成；真实接入缺失** | report 不存在/损坏/虚拟/空样本均不准入；仍允许独立日常恢复与风险回滚。当前不能把 v10 的“runner 就绪”示例当生产状态。 |
| §11.1 双跑预检→真实证据→审核完整链 | **未完成** | 当前只验得缺能力拒绝、归档基础来源核验、独立审核的受控服务边界；真实引擎端到端链是单独工程依赖，不能据本轮测试数量宣布完成。 |

## 4. HF-07 实现契约与数据保留

新增接口已由主代理注册：

```text
GET  /g5-admission/inventory?portfolio_id=...
GET  /g5-admission/control?portfolio_id=...
POST /g5-admission/control
POST /g5-admission/reports/{report_id}/invalidation
```

- 盘点默认只读且不 autoflush；真实 report typed ID、原 `report_hash`、原文本 SHA、损坏 JSON、明确采用审计及未能证明的关联分别保留。当前 members_only 模式或无报告 ID 的回滚不能反推“某份报告已采用”。G7 未持久化独立采用审计的历史事实明确未知，不编造引用。来源/治理表读失败返回 `storage_errors/reviewable=false/counts_complete=false`，不是干净空清单。
- 全局初始冻结，单组合释放不能覆盖全局冻结；缺失/不可读存储、控制头与事件链不一致都拒绝新准入。冻结不自动更改已采用组合、余额、持仓、订单或运行状态。
- 控制请求带 `expected_version/review_note/request_key/acknowledge_only_new_admission`；释放必须核对当前稳定 `inventory_snapshot_id`，无效报告须按同一原哈希追加隔离，每个受影响组合须带 disposition、理由和 evidence_refs。它是治理冻结的释放，**不是放宽 G5 正式证据策略**。
- 单报告隔离/恢复还要求 `expected_report_hash`；需处于冻结窗口，恢复重新校验证据而不能由备注把坏报告变好，恢复不自动解冻。原件哈希变化、清单变化、行版本变化均拒绝，重复幂等键内容不同拒绝。
- 模型 `app/models/g5_admission_governance.py:24` 保留追加事件，ORM update/delete 拒绝；服务事务失败不留半条控制/审计。此为应用层约束，数据库管理员或任意原生 SQL 权限不在其防护内，需发布环境写权限控制。
- 迁移 `alembic/versions/2026_10_23_0075_wps_0023_075_g5_admission_governance.py:16` 只加两表/索引，不扫数据、不改旧报告、不自动释放；downgrade 明确拒绝删除治理历史。回退应用应保持冻结并保留表与审计。

## 5. 发布、权限与实际环境验收依赖

1. 本轮只在内存 SQLite 验证了服务/HTTP与新增 0075 DDL；**未执行运行库迁移、真实库存盘点、遗留隔离、部署、线上通过报告或真实 runner 验收**。部署前应确定版本/可恢复制品，按热修清单 §5 先落实新准入冻结，再独立发布保护代码、读取实际清单并逐组合评审。
2. 当前项目 `_ensure_portfolio_access` 是明确的默认放行占位（`app/api/routes/portfolio_governance.py:941`），无可依赖的组合 owner ACL。HF07 所有控制/隔离写入额外要求 admin；全局读取要求 admin/auditor。`X-User/X-Role` 仍是既有 query 身份约定，可信网关身份/RBAC/角色防伪是部署依赖，不能据 HTTP 403 单测称生产认证隔离完成。G6 使用实际请求 actor，不信 body.operator_id，但同样依赖该认证边界。
3. `auto_trade_dual_run.py:529` 的环境开关/白名单仍参与既有实际来源选择，未记录环境何时改变。HF07 封的是新 G6/G7/API 来源切换，不能阻止管理员直接改 DB/env；冻结窗口禁止未经评审改变部署开关与白名单。未因缺 G5 对所有既有组合停机，也未静默迁移既有来源。
4. 真实 runner 工程至少需部署产物解析、两侧可隔离回放、同一期初账户与义务、订单计划/目标仓位/风控、完整模型/因子依赖冻结、可信历史可见性、完整日期计划、持久幂等任务/报告索引、人工差异处置与复验。完成后应新建 policy 版本和真实环境验收，不能直接把当前 `admission_supported` 改 true。
5. MySQL 当前读/锁顺序、并发冻结与切换、表/审计写权限、真实日历来源更新、生产迁移与回滚、长历史报告分页和浏览器界面需要各自验收记录。本轮 SQLite FOR UPDATE 编译/参数覆盖不能替代 MySQL RR 竞争验证。

## 6. 本轮验证记录

合并回归命令（以下 11 文件）完成：**157 passed，60.35 秒**。该次收集时 HF07 治理文件为 25 项；随后新增并运行了隔离 0075 迁移保留原件测试，HF07 文件最终 **26 passed，5.06 秒**。因此当前版本共 **158 个不同用例已通过**，不是把两次重复运行相加。

```text
pytest -q --tb=short
  tests/test_g5_admission_governance.py
  tests/test_g5_legacy_inventory.py
  tests/test_g5_report_safety_http.py
  tests/test_g5_dual_run_audit.py
  tests/test_g5_dual_run_replay_framework.py
  tests/test_g5_dual_run_report.py
  tests/test_g6_graduated_rollout.py
  tests/test_g6_rollout_actions.py
  tests/test_g6_route_contract.py
  tests/test_g7_operational_readiness.py
  tests/test_whitebox_auto_trade_dual_run.py
```

重点新增证据：

| 测试位置 | 验证结果 |
|---|---|
| `tests/test_g5_report_safety_http.py:115`、`:146` | 已核验动作归档仍不可正式准入；JSON 与 typed eligible 同时强改 true 也拒绝 |
| 同文件 `:243`、`:266`、`:290`、`:305`、`:341` | 策略/范围/样本/输入/日历/cutoff、真实任务状态、pending stub、原始来源修订、纯读 history/precheck |
| `tests/test_g5_dual_run_replay_framework.py:347`、`:363` | 空分母不适用；HOLD 仅双侧真实检查才计有效样本 |
| `tests/test_g5_legacy_inventory.py:69`、`:97`、`:171`、`:205`、`:247`、`:272` | 损坏原件/真实引用、无写入/无 autoflush、来源缺失不是空清单、current_read锁与稳定快照 |
| `tests/test_g5_admission_governance.py:118`、`:129`、`:143`、`:163`、`:173` | 幂等/版本/清单冲突、原哈希隔离、逐组合影响审核、全局与组合冻结 |
| 同文件 `:223`、`:233`、`:251`、`:266`、`:286`、`:304`、`:324` | 无效原件不得恢复、追加恢复不解冻、事件不可覆写、审计失败回滚、三准入一致冻结/风险回滚、写入角色、隔离迁移保留原件 |
| `tests/test_g6_rollout_actions.py:101`、`:122`、`:132`、`:139` | 受控未来完整边界的原子切换失败回滚/快照冲突/本人理由；实际当前动作归档继续拒绝真实切换 |
| `tests/test_g7_operational_readiness.py:100`、`:128` | 日常人工暂停恢复独立于 G5；扩大范围仍受独立数据/模型门禁约束 |

测试夹具 `tests/g5_report_fixtures.py` 只证明隔离持久来源契约，不执行或实现真实引擎。事务测试中明确 mock 的完整来源边界不保存假通过报告，不计作真实 runner 正向验收。回归警告为 SQLite/Python 日期适配弃用、Starlette TestClient 和兼容弃用请求字段提示，未出现失败；前端构建与浏览器结果由本轮总审计汇总。

## 7. 总体接入的追加复核（同日）

主代理本轮已接入 G6 请求的冻结报告 ID/hash、来源、本人理由及实际请求 actor；冻结治理异常返回结构化 code/message。追加复核发现回滚仍信 body.operator_id 后，主代理也已将回滚改用实际 actor 并重验组合访问；回滚仍不依赖 G5 正式准入，不因治理冻结失去紧急出口。界面最新 readiness.eligible=false 时显示本次 blockers 并禁用确认，避免旧 G7 快照可扩大就邀请用户提交已无效审核。

检查分析接入还发现并补了三个独立问题，均不改变 G5 证据政策：

- `app/api/routes/ai_sessions.py:61`、`:74`：创建检查会话时即在已有 `AISession.context_summary` 保存服务端原组合/actor关联，不保存 raw 大材料；通用列表隐藏他人及无可靠关联的旧检查会话，详情/messages/audit/delete/stream 续聊重验原 actor 与当前组合访问。续聊忽略客户端组合重定向，重新读取原范围当前证据；普通通用会话保留原契约，没有新增迁移。
- 同文件 `:121`：最终预算处理原地更新工作流持有的同一 ContextPack；快照重新按实际 `{scope, material}` 计算，evidence_refs 仅指向保留材料，unknowns 明示截断；无事实材料时不调用模型，也不产生虚假证据或执行草稿。敏感字段名和常见 secret 字符串均在送模型前清洗。
- 同文件 `:168`、`:359`、`:554`：受控分析的 scope/as-of/hash/unknowns/白名单/调用信息由服务端最终覆盖，模型不能伪造；只保留服务端白名单导航，模型 URL、业务 payload 与 draft 不进入执行或审批。

追加隔离验证：新 `tests/test_inspection_ai_session_scope.py` 12 项、`tests/test_inspection_ai_controlled_metadata.py` 6 项；连同旧 AI HTTP 为 **27 passed / 20.22s**。旧 AI audit/session、检查分析与新检查集为 **77 passed / 42.90s**；旧协议/流式/主备/配额回归为 **16 passed / 9.63s**。这些集合有重叠，不累加为不重复数量，也不计作真实 AI 服务或交易执行验收。

生产身份仍依赖项目现有 query actor / 可信网关与组合 ACL，本文第 5 节的认证部署限制继续适用。普通通用 AI 的既有全局 context 取数权限没有在本轮扩为全系统 RBAC 重构；不能据此宣称所有 AI 会话及所有历史业务入口均已完成权限治理。
