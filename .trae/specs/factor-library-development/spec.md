# 专业因子库与因子设置中心开发 Spec

> 底稿来源：
> - `docs/专业因子库开发计划.md`（V1.1，2026-08-01 现场审计版，下称"计划"）
> - `docs/因子设置与专业因子库改造方案.md`（V1.0，2026-07-31，下称"方案"）
>
> 本 spec 不重新设计，而是把上述两份底稿落为工程执行规格。任何改动必须先核对底稿"当前项目环境与数据基线"，禁止重复建设或重写已稳定底座。当本 spec 与底稿冲突时，以底稿为准；当本 spec 与代码现实冲突时（如 Alembic head），以代码现实为准并在 spec 中标注。

## Why

用户对"AI 自动设置因子"的担忧成立。AI 能降低公式编写门槛，但无法单独保证公式具有经济含义、数据在历史时点真实可见、参数未过拟合、新因子不与已有因子同质、样本外表现稳定、以及修改后不影响已运行的扫描/模型/交易计划。

本次改造的第一优先级不是实现遗传编程或自动挖掘，而是先补齐"因子可设置、可管理、可验证、可追溯、可回退"的底座。自动挖掘只能建立在该底座之上。

当前现场真实瓶颈（来自计划 0A 节审计，2026-08-01）：
1. 活动 MySQL 无 `alembic_version` 基线，不能盲目 upgrade；
2. 最新两日（2026-07-28/29）universe 横截面仅 218/150 个标的，DuckDB 仅 150，不能当作完整交易日；
3. 2026-07-29 最近两次 factor_pipeline 在 11.3%/19% failed，DuckDB 锁冲突未治理；
4. 8 个系统因子中仅 `turnover_z20` 覆盖率达标（94.7%），估值/资金流/尾盘因子覆盖为 0；
5. 失败任务中文 message 已乱码；
6. 唯一 Ridge 运行状态为 rejected，sample_count=0；
7. 自定义数值指标当前为 0，无真实存量样本。

## What Changes

按计划"数据底座 + 三次功能发布"推进，不一次性重写现有因子流水线：

### R0 数据底座修复（WPD）
- MySQL schema 与 Alembic 基线审计：导出 schema fingerprint、安全 stamp/迁移方案、备份校验。
- 完整交易日判定：实现 `latest_complete_trade_date`，当日标的数低于过去 20 日中位数 90% 时标记 incomplete；EvaluationRun 必须保存 `selected_trade_date`、`observed_symbols`、`expected_symbols`、`completeness_ratio`、`fallback_reason`。
- DuckDB 锁和残留进程治理：锁拥有者诊断、跨进程单飞、超时、重启恢复、失败批次保护。
- 数据 readiness API：按因子类型返回 `available/degraded/blocked` 及证据。
- 错误协议与乱码修复：`error_code` 为事实来源，UTF-8 存储，前端 i18n 本地化，历史裸字符串兜底。
- 因子值增量与批次审计：批次清单、输入日期、行数、状态、原子提交证据。
- 数据源补齐路线：估值 6、资金流 0、尾盘 0 的分来源补数任务、限流预算、重试。

### R1 因子库管理 MVP（WP0~WP4）
- WP0 基线冻结：导出 8 系统因子及版本快照、固定 manual/ridge/shadow API 契约、旧计算结果对账样本、任务取消/重跑/异常终态回归。
- WP1 数据模型、迁移与生命周期：扩展 Factor/FactorVersion（lifecycle_status、origin、factor_type、owner、active_version_id、shadow_version_id、risk_level、archived_at；formula_ast_json、postprocess_json、parameter_schema_json、data_dependencies_json、compiler_version、execution_plan_hash、complexity_score、validation_status 等）；新增 EvaluationRun、TransitionAudit、FactorSet 基础表；编写 Alembic 0021（revision `wps_0023_021_factor_library_lifecycle`，**down_revision 指向代码实际 head `wps_0801_001_universe_incremental_index`**，非底稿记录的 `wps_0023_020_api_deprecation_logs`）；因子仓储与 Schema；生命周期服务（合法迁移、硬门禁、actor/reason、409 冲突）；迁移兼容测试。
- WP2 公式编译、校验与预览 API：抽取 AST 编译核心（白名单、节点数、深度、依赖收集、错误码）；原始表达式 DSL；后处理配置（winsorize、rank/zscore、neutralize、missing policy）；稳定执行计划和哈希；校验与预览 API；DuckDB 兼容执行（冻结批次读取、单写锁、失败不覆盖旧批次）。
- WP3 因子中心前端：因子中心壳层（Settings 入口、页签、旧入口兼容）；因子库列表（搜索、筛选、分页、状态、空状态）；因子详情（版本、引用、状态历史、风险提示）；抽取公式编辑组件（从 CustomIndicatorSettings 复用）；因子编辑器（草稿、模板、参数、后处理、错误定位）；国际化与响应式验收。
- WP4 自定义指标提升与 AI 草案：数值指标提升接口（CustomIndicator → Candidate Factor，保存来源映射）；前端提升操作（仅 number 显示入口）；FactorDraft Schema；AI 草案服务（结构化返回、校验、来源、内容哈希）；AI 前端确认流程（应用草案、查看差异、用户主动保存）。

### R2 专业评估与 Shadow（WP5~WP6）
- WP5 科学评估与压力测试：评估运行契约（不可变 EvaluationRun、配置哈希、数据截止时间）；时间切分与样本构造（训练/验证/测试、purge/embargo、目标对齐）；基础指标（Rank IC、ICIR、覆盖、分组单调性、换手、成本后收益）；分类门禁（continuous/event/regime 独立阈值）；参数和样本扰动；异步任务接线（进度、取消、重跑、心跳、终态恢复、幂等）；评估实验室前端。
- WP6 相关性治理、Shadow 与审批：相关矩阵和聚类；残差增量评估；Shadow 每日观测（按因子版本和交易日幂等）；衰减和数据健康告警；人工审批流程；Shadow 前端。

### R3 模型接线与发布（WP7~WP8）
- WP7 FactorSet 与 Ridge 接线：FactorSet 冻结（集合、成员版本、顺序、缺失策略、内容哈希）；动态因子计算；Ridge 样本接线（动态特征、覆盖、排除原因、版本记录）；模型门禁增强；Score 与解释追溯；模型页前端。
- WP8 迁移切换、回退与正式验收：上线顺序（备份→部署新表与只读 API→回填系统因子→双读对账→开放草稿/候选/评估→Shadow 与审批→开放新 FactorSet→人工切换→验收报告）；回滚演练；正式验收门禁。

### **BREAKING** 变更（受控）
- Alembic 0021 新增 `lifecycle_status`、`origin`、`factor_type` 等字段为 Factor/FactorVersion 的新事实来源；现有 `status`/`is_active` 仅作过渡单向兼容，不再承担生命周期语义。
- 禁止通过普通 PATCH 修改 `lifecycle_status`，所有状态变化必须走专用 transition 接口并追加审计。
- Ridge 特征选择移除模块加载时静态 `FEATURE_CODES`，改为按 FactorSet 动态读取。

## Impact

- **Affected specs**: 无（因子库相关首次规格化；与 `opportunity-center-execution-plan`、`opportunity-center-refactor` 无功能交叉，因子仓库明确不在机会中心改造范围内）
- **Affected code - 后端**:
  - `app/models/factor.py`、`app/models/factor_model.py`、`app/models/factor_runtime.py`、`app/models/factor_evaluation.py`（新增）、`app/models/custom_indicator.py`、`app/models/__init__.py`
  - `app/schemas/factor_library.py`（新增）、`app/schemas/custom_indicator.py`
  - `app/api/routes/factors.py`、`app/api/routes/factor_pipeline.py`、`app/api/routes/factor_models.py`、`app/api/routes/custom_indicators.py`、`app/api/routes/ai_config.py`
  - `app/services/indicator_ast_sandbox.py`
  - `app/services/factors/definitions.py`、`factor_registry.py`（新增）、`factor_compiler.py`（新增）、`factor_lifecycle.py`（新增）、`factor_evaluator.py`（新增）、`factor_stress.py`（新增）、`factor_correlation.py`（新增）、`factor_engine.py`、`store.py`、`pipeline_task.py`、`ridge_model.py`、`runtime.py`、`scoring_bridge.py`、`contracts.py`
  - `app/services/ai/drafts/indicator.py`
  - `app/services/migration.py`
  - `alembic/versions/2026_07_31_0021_factor_library_lifecycle.py`（新增，实际 down_revision 调整为 `wps_0801_001_universe_incremental_index`）
- **Affected code - 前端**:
  - `frontend/src/components/Settings.tsx`、`FactorModelSettings.tsx`、`CustomIndicatorSettings.tsx`
  - `frontend/src/components/factors/FactorCenter.tsx`（新增）、`FactorLibrary.tsx`（新增）、`FactorDetail.tsx`（新增）、`FactorEditor.tsx`（新增）、`EvaluationLab.tsx`（后续新增）、`ShadowMonitor.tsx`（后续新增）
  - `frontend/src/i18n/zh-CN.ts`、`en-US.ts`
  - `frontend/src/components/__tests__/FactorLibrary.test.tsx`（新增）、`FactorEditor.test.tsx`（新增）
- **Affected code - 测试**: 新增 `test_whitebox_factor_lifecycle.py`、`test_whitebox_factor_version_immutability.py`、`test_whitebox_factor_compiler.py`、`test_whitebox_factor_evaluation.py`、`test_whitebox_factor_stress.py`、`test_whitebox_factor_correlation.py`、`test_whitebox_factor_set.py`、`test_whitebox_custom_indicator_promotion.py`、`test_blackbox_factor_library.py`、`test_blackbox_factor_ai_safety.py`、`tests/e2e/test_factor_library_flow.py`；扩展 `test_migration_alembic_chain.py`
- **不改写**: 现有 8 个系统因子 ID 与 FactorVersion ID、历史 Score、模型运行、权重快照、manual 闭环、评分算法、扫描算法、回测引擎、模拟成交。

## ADDED Requirements

### Requirement: 数据底座稳定性（R0/WPD）
系统 SHALL 在叠加任何因子库功能前，先让现有 MySQL、DuckDB 和 factor_pipeline 形成可判断、可恢复、可用于技术因子评估的稳定底座。

#### Scenario: 活动 MySQL 无 Alembic 基线时不可盲目 upgrade
- **WHEN** 活动 MySQL 未发现 `alembic_version` 表
- **THEN** 系统先导出 schema fingerprint 并备份，形成经评审的安全 stamp 决策
- **AND** 不得在未审计库上直接执行 `alembic upgrade head`

#### Scenario: 残缺横截面不被当作完整交易日
- **WHEN** 最新交易日（如 2026-07-28/29）标的数仅 150/218，低于过去 20 日中位数 90%
- **THEN** `latest_complete_trade_date` 回退到最近完整交易日（如 2026-07-24）
- **AND** EvaluationRun 记录 `selected_trade_date`、`observed_symbols`、`expected_symbols`、`completeness_ratio`、`fallback_reason`

#### Scenario: DuckDB 锁冲突有明确终态
- **WHEN** factor_pipeline 因 DuckDB 锁冲突失败（如 2026-07-29 在 11.3%/19% failed）
- **THEN** 系统提供锁拥有者诊断、跨进程单飞、超时与重启恢复
- **AND** 失败批次不覆盖上一个成功因子批次

#### Scenario: 0 覆盖因子在预检阶段被阻断
- **WHEN** 因子数据覆盖为 0（如 ep_ttm、main_inflow_5d_ratio、tail_accumulation_proxy）
- **THEN** readiness API 返回 `blocked` 及证据
- **AND** 不扫描 1900 万 factor_values 后才失败

#### Scenario: 失败任务中文 message 不再乱码
- **WHEN** factor_pipeline 失败
- **THEN** 任务记录使用稳定 `error_code` 作为事实来源
- **AND** 中文 message 以 UTF-8 存储，前端用 i18n 本地化，历史裸字符串有兜底显示

### Requirement: 因子生命周期状态机（R1/WP1）
系统 SHALL 建立因子、不可变版本、状态迁移和审计的唯一事实来源，同时兼容历史记录。第一版状态机支持：draft→candidate；candidate→testing；draft/candidate/testing→rejected；rejected→draft（仅通过创建新版本）；任意非 Active 状态→deprecated；Active、Shadow、Quarantined 的完整生产迁移在 WP6 开放。

#### Scenario: 普通 PATCH 无法绕过状态机
- **WHEN** 调用普通 CRUD 接口尝试修改 `lifecycle_status`
- **THEN** 请求被拒绝
- **AND** 状态变化必须调用专用 transition 接口并追加审计

#### Scenario: 非法状态迁移返回 409
- **WHEN** 尝试非法迁移（如 draft 直接 → active）
- **THEN** 返回 409 冲突
- **AND** 字段错误返回 422

#### Scenario: 已引用版本不可修改
- **WHEN** FactorVersion 已被评估或模型引用
- **THEN** 禁止原地更新、删除或重写
- **AND** 修改公式/参数/依赖/方向/缺失策略必须创建新版本

#### Scenario: 同内容版本请求幂等
- **WHEN** 重复提交相同内容的版本
- **THEN** 不生成重复版本，返回既有版本

#### Scenario: 8 系统因子幂等回填为 Active
- **WHEN** 执行 Alembic 0021 迁移
- **THEN** 8 个系统因子回填 `origin=system`、`lifecycle_status=active`
- **AND** 不改写已有 Factor.id 和 FactorVersion.id

### Requirement: Alembic 单 head 与迁移安全（R1/WP1）
系统 SHALL 保持仓库 Alembic 唯一 head，0021 迁移幂等可重复检查，downgrade 路径经过测试。

> **代码现实校准**：底稿记录 head 为 `wps_0023_020_api_deprecation_logs`，但代码实际 head 已为 `wps_0801_001_universe_incremental_index`（2026-08-01 18:30 新增 universe 增量索引）。0021 迁移的 `down_revision` 必须指向实际 head。

#### Scenario: 0021 迁移链指向实际 head
- **WHEN** 创建 `2026_07_31_0021_factor_library_lifecycle.py`
- **THEN** `revision = "wps_0023_021_factor_library_lifecycle"`
- **AND** `down_revision = "wps_0801_001_universe_incremental_index"`（代码实际 head）
- **AND** 仓库保持单 head

#### Scenario: 迁移在多环境通过
- **WHEN** 在空库、历史 SQLite、目标 MySQL 5.7.26 执行
- **THEN** upgrade/downgrade 均通过
- **AND** 不改变已有 Factor.id 和 FactorVersion.id

### Requirement: 公式编译与静态校验（R1/WP2）
系统 SHALL 把现有 AST 沙箱升级为可复用因子编译能力，形成"原始表达式 + 截面后处理"两层执行计划。AST 最大深度默认 4，函数调用数量默认不超过 12，单因子最大回看窗口默认 250 个交易日。

#### Scenario: 危险表达式被拒绝
- **WHEN** 公式包含属性访问、导入、任意函数调用、eval、递归、动态函数名、未来引用或负数 lag
- **THEN** 校验返回稳定错误码并拒绝
- **AND** 不进入 candidate

#### Scenario: 超复杂表达式被拒绝
- **WHEN** AST 深度超过 4 或函数调用超过 12 或回看窗口超过 250
- **THEN** 可保存为 draft
- **AND** 不可提交 candidate

#### Scenario: 相同输入生成相同执行计划
- **WHEN** 相同公式、参数、后处理配置
- **THEN** 生成相同 canonical JSON 和 `content_hash`
- **AND** 相同数据批次得到相同计算结果

#### Scenario: 预览选择完整交易日
- **WHEN** 调用预览 API
- **THEN** 默认选择最近完整交易日，不选择 2026-07-28/29 残缺横截面
- **AND** 返回原值、数据来源、缺失原因、`data_cutoff_at`

### Requirement: 因子中心前端（R1/WP3）
系统 SHALL 在设置页提供完整但克制的因子管理工作台，不把评估实验室提前塞进第一版。页签含因子库、因子详情、因子编辑、模型运行（保留现有）、评估实验室（R1 仅显示未开放状态）。

#### Scenario: 中文环境不显示裸英文枚举
- **WHEN** 中文界面展示因子状态
- **THEN** `draft`/`candidate`/`validation failed` 等枚举有中文本地化
- **AND** 不直接显示后端英文枚举

#### Scenario: 状态按钮按门禁禁用
- **WHEN** 因子当前状态不允许某迁移
- **THEN** 对应按钮禁用
- **AND** 数据阻断因子不显示"提交 testing"按钮

#### Scenario: 请求防重
- **WHEN** 用户重复点击保存/预览/提交候选
- **THEN** 按钮进入稳定 loading，完成前不可二次点击
- **AND** 后端使用幂等键，同一操作不产生两个创建/评估任务

#### Scenario: 小屏不重叠
- **WHEN** 在 1366×768 或 390×844 分辨率下
- **THEN** 表格、编辑器和操作栏不重叠

### Requirement: 自定义指标提升与 AI 草案边界（R1/WP4）
系统 SHALL 打通低门槛入口，但确保指标和 AI 都只能产生候选草案。

#### Scenario: boolean 指标提升被拒绝
- **WHEN** 尝试提升 value_type=boolean 的指标为候选因子
- **THEN** 返回 422/409 拒绝
- **AND** 原指标不被改写或删除

#### Scenario: AI 草案不自动入库
- **WHEN** AI 返回 FactorDraft
- **THEN** 必须通过 Pydantic 和 AST 校验后才返回前端
- **AND** 不自动保存、不自动提交、不自动激活
- **AND** 用户点击"应用到草稿"后才进入编辑器

#### Scenario: AI 无生产权限
- **WHEN** AI 调用 transition/activate/runtime mode/FactorSet 写接口
- **THEN** 请求被拒绝
- **AND** AI 不生成或伪造 IC、ICIR、回测收益
- **AND** API Key 不进入草案、日志和错误详情

#### Scenario: AI 未配置时主流程仍可用
- **WHEN** AI 未配置（enabled=false）
- **THEN** 手工编辑、模板、校验、预览、候选提交全部可用

### Requirement: 科学评估与压力测试（R2/WP5）
系统 SHALL 让 candidate/testing 因子用冻结数据证据决定能否进入 Shadow。第一批只评估 A 层日线技术因子；B 层走事件/状态口径；C 层在 readiness=blocked 时直接停止。

#### Scenario: 评估结果可重复
- **WHEN** 同版本、同快照、同配置重复运行
- **THEN** 结果在容差内一致
- **AND** EvaluationRun 保存完整交易日判定证据

#### Scenario: 时间安全
- **WHEN** 评估涉及财报字段
- **THEN** 按公告日可见，目标退出日不进入特征可见区
- **AND** 参数扰动复用相同 `data_cutoff_at`

#### Scenario: 事件因子不被误用连续门禁
- **WHEN** 评估 lhb_institution_net_ratio 等事件因子
- **THEN** 只对事件日样本计算命中后收益和显著性
- **AND** 不因全市场低覆盖被错误淘汰

#### Scenario: 评估任务终态稳定
- **WHEN** 评估任务被取消或超时或进程重启
- **THEN** 进入 canceled/failed 终态，不永久停留 running
- **AND** 同幂等键不启动两个评估任务
- **AND** 评估结果和原始配置不可更新

### Requirement: 相关性、Shadow 与审批（R2/WP6）
系统 SHALL 识别同质因子，持续观察新因子在真实日常数据中的稳定性，并保留人工 Active 审批。

#### Scenario: Shadow 至少 20 个有效交易日
- **WHEN** 候选因子进入 Shadow 观察
- **THEN** 至少保存连续 20 个有效交易日观测后才允许提交 Active
- **AND** 缺少交易日、横截面完整率低于 90% 或数据异常时不计入有效观察天数
- **AND** 不允许用历史回测结果回填 Shadow 天数

#### Scenario: 高相关候选被识别
- **WHEN** 候选与 Active 因子相关性超过 0.7
- **THEN** 触发残差增量评估
- **AND** 残差无效则 rejected

#### Scenario: Active 必须人工批准
- **WHEN** Shadow 因子申请 Active
- **THEN** 必须由 local_user 明确批准
- **AND** 审批包含评估 run、观察区间、actor、reason

### Requirement: FactorSet 与 Ridge 接线（R3/WP7）
系统 SHALL 让通过审批的 Active 因子只进入新的冻结 FactorSet 和新模型，不改变旧模型和历史解释。当前唯一 Ridge 运行 sample_count=0 且 rejected，WP7 不做旧 Ridge 迁移，第一目标是以 A 层技术因子形成非零、可复现、仍需门禁判断的候选模型。

#### Scenario: FactorSet 发布后不可修改成员
- **WHEN** FactorSet 发布
- **THEN** 成员不可修改
- **AND** 模型产物固定 FactorSet ID 和每个 FactorVersion

#### Scenario: 新 Active 因子不影响旧模型
- **WHEN** 新因子进入 Active
- **THEN** 只影响新模型运行
- **AND** 旧模型重放结果不读取最新因子版本

#### Scenario: 不满足门禁时 Ridge 保持 rejected
- **WHEN** Ridge 训练不满足覆盖和样本门禁
- **THEN** 结果保持 rejected
- **AND** 不为生成可激活模型而降低门禁

### Requirement: 迁移切换与回退（R3/WP8）
系统 SHALL 采用"先写新表、双读比对、最后切读"的上线顺序，并保留完整回退能力。

#### Scenario: 双读数值一致后才开放写
- **WHEN** 对相同 `data_cutoff_at` 双跑旧路径和 FactorSet 路径
- **THEN** 行数、缺失、归一化值、Quality/Timing Score、解释在约定容差内一致
- **AND** 一致后才开放草稿/候选/评估

#### Scenario: 回退后主流程可用
- **WHEN** 关闭因子中心写开关并切回 manual
- **THEN** 扫描、评分解释、交易计划仍可用
- **AND** 清空新决策使用的 `active_model_run_id`
- **AND** 读取路径回到 definitions.py 和旧 factor_engine

#### Scenario: Active 异常不改写历史
- **WHEN** Active 因子异常
- **THEN** 不改写旧 FactorSet 或历史 Score
- **AND** manual 和上一 FactorSet 均可回退

## MODIFIED Requirements

### Requirement: 因子计算事实来源
原有：`FACTOR_DEFINITIONS`（`app/services/factors/definitions.py`）是因子与模型特征的唯一事实源，模块加载时静态 `FEATURE_CODES` 固定 Ridge 特征。

修改为：`FACTOR_DEFINITIONS` 降级为"系统种子 + 灾备定义"，数据库注册表（`factor_registry.py`）成为统一事实来源。Ridge 样本构造从硬编码特征切换到 FactorSetMember，按版本和缺失策略执行。系统因子仍由代码提供专用执行器，数据库保存统一定义和版本。

### Requirement: 自定义指标与因子的关系
原有：自定义指标（`custom_indicators`）只用于筛选和回测，与正式因子割裂。

修改为：明确分为布尔指标（筛选/提醒）、数值研究指标（预览/回测）、候选因子、Shadow 因子、Active 因子。数值指标可通过"提交为候选因子"晋升，提交后复制形成独立的 Factor/FactorVersion，不与原指标共享可变记录。原指标继续可用且不被改写。

### Requirement: 运行模式与功能开关
原有：运行模式 manual/shadow/ridge 直接切换，因子功能由 `FACTOR_FEATURE_ENABLED` 环境默认值控制。

修改为：默认运行模式继续是 manual，新功能由独立开关逐步开放。实际运行配置以数据库 `factor_system_config.feature_enabled` 行优先。新因子中心首次开放时通过独立功能开关默认 manual，不在 schema migration 中自动切换运行模式。

## REMOVED Requirements

### Requirement: AI 直接创建并启用因子
**Reason**: 方案 0 节明确，AI 无法单独保证公式经济含义、数据时点可见性、参数未过拟合、因子不同质、样本外稳定、修改后不影响已运行系统。继续强化"AI 直接创建并启用因子"与产品定位冲突。
**Migration**: AI 改为只生成结构化 FactorDraft 草案，必须经用户确认、AST/依赖校验、评估、Shadow、人工审批后才能 Active。AI 无 transition、activate、runtime mode、FactorSet 写权限。

### Requirement: 8 系统因子硬编码为唯一注册表
**Reason**: 用户无法新增、修改或停用正式因子；数据库中的公式版本不能真正驱动计算；模型特征在代码加载时固定，因子启停和版本变化不能动态形成训练集。
**Migration**: 系统因子继续作为内置种子和灾备定义保留在 `definitions.py`，但统一事实来源迁移到数据库注册表 `factor_registry.py`。8 系统因子幂等回填为 Active，不改 ID 与版本。

### Requirement: 通用 PATCH 直接设置 active=true
**Reason**: 绕过状态机门禁，无法隔离候选、测试、影子、正式状态。
**Migration**: 禁止通用 PATCH 修改 `lifecycle_status`，状态变化必须走专用 transition 接口并校验门禁、追加审计。

## 明确不做（本期范围外）

- 遗传编程、强化学习、无约束表达式生成；
- AI 自动发布 Active；
- ClickHouse、Airflow、Prefect、分布式训练；
- 改变 3~15 个交易日的中短期决策边界；
- 分钟级、高频或自动下单扩展；
- 为提高通过率降低数据/时间安全/模型门禁；
- 将缺失值统一填 0；
- 回测和训练时访问外网；
- Phase 6 模板网格、受控表达式树、LightGBM 非线性候选等自动挖掘能力（Phase 1~5 稳定后再考虑）。
