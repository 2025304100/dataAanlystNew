# P0 防腐层后续工作清单与验收标准

> **生成时间**：2026-08-30
> **基线版本**：P2.2 治理流程闭环（17/17 smoke PASS） + P0.2 前端跨域盘点 + P0.3 防腐落地（A-1/A-2/B-4 已完成）
> **适用范围**：因子域与外部模块（数据同步、策略规则、任务中心、今日决策、回测、仪表盘、自定义指标）的接口边界治理
> **核心原则**：外部模块 **只** 通过 `app.services.factors.__facade__.py` + HTTP `/api/v1/scoring/*` 访问因子域；禁止反向依赖、禁止 `as any` 类型穿越、禁止组件层裸 `requestJson('/api/v1/factor*')`

---

## 0. 现状快照（P0.3 完成时点）

### 0.1 已落地的 Facade / 路由 / 前端薄封

| 领域 | 后端 Facade 函数 | HTTP 路由 | 前端 client.ts 方法 | 调用方 |
|------|------------------|-----------|----------------------|--------|
| 模型下拉 (策略/回测) | `list_score_models` | `GET /scoring/models` | `scoringListModels(scope, limit)` | PortfolioStrategyRules ✅ |
| 运行态总览（仪表盘/今日） | `get_score_runtime_overview` | `GET /scoring/overview` | `scoringGetOverview()` | TodayDecision ✅ |
| 激活作用域（告警/挖掘/策略） | `get_active_runtime_with_fallback_reason` | `GET /scoring/active-scope` | `scoringGetActiveScope()` | 通用 |
| 异步任务取消 | `cancel_opaque_async_task` | `POST /scoring/tasks/{id}/cancel` | `scoringCancelTask(taskId)` | TaskCenter ✅ |
| 异步任务创建（P0.3-A1 新增） | `create_scoring_task` | `POST /scoring/tasks` | `scoringCreateTask(payload)` | ExternalDataSync ✅ |
| 异步任务查询（P0.3-A1 新增） | `get_scoring_task` | `GET /scoring/tasks/{id}` | `scoringGetTask(taskId)` | ExternalDataSync ✅ |
| 因子输入就绪 | `ensure_feature_inputs_ready` | `POST /scoring/feature-inputs/ensure-ready` | `scoringEnsureFeatureInputsReady()` | DiscoveryDataPrep / 外部脚本 |
| 草稿外部提交（仅一条受信写入路径） | `submit_factor_draft_from_external` | `POST /scoring/external-draft/submit` | `scoringSubmitExternalDraft()` | CustomIndicator 审批流 |
| 因子集展示（UI 缝合） | `list_factorsets` | `GET /scoring/factorsets` | `scoringListFactorSets(scope, limit)` | PortfolioStrategyRules ✅ |
| 模型详情（含因子权重/对比） | `get_model_detail` | `GET /scoring/models/{id}` | `scoringGetModelDetail(id)` | FactorModelPage 对比 ✅ |
| 因子使用情况 | `get_factor_usage` | `GET /scoring/factors/{code}` | `scoringGetFactorUsage(code)` | 通用 |
| 草稿审批链路（治理 P2.2） | `list/get/approve/reject_factor_draft` | `/scoring/drafts*` × 4 条 | `scoring*FactorDraft*` × 4 | FactorLibrary 草稿 Tab ✅ |
| 训练准入门禁（治理 P2.3） | `run_training_eligibility_gates` | `POST /scoring/gates/training-eligibility` | `scoringRunTrainingEligibilityGates()` | 训练控制台预检查 |

### 0.2 DTO 对外契约（不可随意删字段，只能追加 + 版本号）

- **ScoringModelBrief**：`id / name / status / validation_ic / sample_count / data_cutoff_at / factorset_id / factorset_label / factorset_member_count / created_at`
- **ScoringFactorSetBrief**：`id / label / status / member_count / created_at / frozen_at / is_active_for_model_run_ids / description`
- **ScoringModelDetail / ScoreFactorSetDTO / FactorDraftDTO / ScoringOverview / ScoringActiveScope** — 见 `client.ts` L2850+ 与 `__facade__.py` L36+

---

## 1. 🏁 P0.4 终态验收（必做，阻塞交付）

**P0.4 总目标**：把 P0.2 盘点 → P0.3 落地 的成果固化成 **可重复验证的基线**，任何 MR 再次引入跨域引用时能被自动/半自动拦截。

### 1.1 P0.4-1 终版冒烟脚本（HTTP + facade 双轨，≥ 34 条用例）

| 子模块 | 用例数 | 断言 |
|--------|--------|------|
| Facade 层 DTO 结构 | 5 | `ScoreModelBriefDTO.factorset_id` 非空、`ScoringFactorSetBrief.label/description` 返回、非法 id 抛 404、DTO asdict 序列化无未知字段 |
| `/scoring/models` + `/scoring/models/{id}` 链路 | 6 | validated scope 仅 validated、factorset_id 与 FactorSet.id 对得上、detail 返回 weights 数量 = factorset_member_count、非法 id → 404 |
| `/scoring/tasks` 创建/查询/取消 | 7 | POST 创建成功（或被流水线并发保护捕获返回已有 running）、GET 404 分支、cancel 成功 / cancel 二次幂等 / cancel on done terminal 返回 no-op |
| 草稿审批全链路（复用 tmp/_p22_closure_smoke.py 17 条） | 17 | submitted → applied → indicator 回写 + 3 条审计；reject → REJECTED 审计；非法 transition |
| **反跨域断言（新加）** | 1 | 脚本内部 monkey-patch 或 access log grep：**外部域 3 页触发的 HTTP 请求中 0 条命中 `/api/factor*` / `/factor-pipeline*` / `/factor-models*`** |
| 错误文案隔离 | 1 | `/scoring/tasks` 404 的 response body 不含 `"factor_pipeline"` 关键词；统一为 `task_not_found` / `illegal_transition` |
| **合计** | **≥ 37** | |

- **交付物**：`tmp/_p04_final_smoke.py`（复用 P2.2 脚本的 db/open_session 工具）
- **验收标准**：脚本 `ALL_PASS = True` + 打印反跨域断言 PASS；本地 2 次运行稳定不 flaky
- **依赖**：uvicorn `:8000` 运行、MySQL + DuckDB 在线、tmp/_p22_closure_smoke 的测试数据 seed 仍可用

### 1.2 P0.4-2 前端跨域回归扫描（CI 式 grep + CSV 报告）

- **扫描范围（A 类外部域）**：`frontend/src/components/` 下所有**不属于** `factors/` 的组件，重点：
  ```
  TaskCenter.tsx
  ExternalDataSync.tsx
  portfolio-trading/PortfolioStrategyRules.tsx
  TodayDecision.tsx
  symbol-research/**/*.tsx
  Dashboard*.tsx
  */backtest/**
  alerts/**
  candidate/**
  custom-indicator/**
  ```
- **forbidden 关键词**（命中即告警）：
  ```
  createFactorPipelineTask / getFactorPipelineTask / listFactorPipelineTasks / getFactorPipelineEta
  getFactorModels / getFactorModel / activateFactorModel / fallbackFactorModel / trainFactorModel
  listFactorSets / freezeFactorSet / deprecateFactorSet
  getFactorOverview / updateFactorSystemConfig / initializeFactorWarehouse
  listFactorDefinitions / getFactorDefinition / createFactorVersion
  import type { FactorModelRun }    ← 跨域类型引用
  /api/v1/factor                    ← 组件层裸 URL（必须走 client.ts 封装）
  requestJson(.../factors/          ← 绕过 client.ts 裸调用
  ```
- **B 类因子内部页**（`factors/*`, FactorModelSettings, FactorCenter）的命中必须在同一行或上一行包含注释：`// Factor-domain internal — DO NOT USE outside factor center`
- **交付物**：`scripts/audit-frontend-cross-domain.ps1`（或 Python 脚本）+ 报告 `tmp/p042-frontend-audit-YYYYMMDD.csv`
- **验收标准**：`A 类外部域命中数 = 0`；B 类无注释命中 ≤ 5 且逐条人工确认后补注释

### 1.3 P0.4-3 后端跨域 import 回归

- **扫描路径（非 factor 域）**：`app/services/alerts.py`, `app/services/candidate_promote.py`, `app/services/candidate_*.py`, `app/services/discovery_data_prep.py`, `app/routes/portfolio*.py`, `app/routes/backtest*.py`, `app/routes/dashboard*.py`, `app/routes/custom_indicator*.py`
- **forbidden import**：直接 import 以下任一条（白名单：仅 `from app.services.factors.__facade__ import ...`）
  ```
  app.models.factor_model          ← 直接访问 FactorModelRun ORM
  app.models.factor                ← 直接访问 Factor/FactorVersion
  app.models.factor_runtime.ActiveScoreScope  ← 必须用 get_active_runtime_with_fallback_reason()
  app.services.factors.ridge_model            ← 训练逻辑必须经门禁 Facade
  app.services.factors.pipeline_task          ← 必须经 create_scoring_task / ensure_feature_inputs_ready
  app.services.factors.warehouse_locks        ← 内部锁机制不得外泄
  ```
- **交付物**：`tmp/p043-backend-audit-YYYYMMDD.csv` + `scripts/audit-backend-cross-domain.py`
- **验收标准**：forbidden list 命中数 = 0；近亲耦合（如 `from app.models.factor_evaluation import FactorSet` 只读查询）必须加 `# near-relative coupling: <reason> — audit 2026-08-30` 注释豁免

### 1.4 P0.4-4 破坏性回归（"真的挡住了"验证）

1. **操作 A — 关原生路由**：临时在 `app/api/router.py` 注释掉 `factor-models` / `factor-pipeline` / `factors`（保留 `/scoring*`），重启后 5 分钟内验证：
   - 数据同步页 ExternalDataSync 首屏 + "提交评分流水线"按钮不白屏，error toast 不出现 "factor_pipeline"
   - 策略规则 PortfolioStrategyRules：模型下拉不为空 → 选中 → 溯源链只读映射正常展示（已通过 scoringListModels/ListFactorSets 拿）
   - 今日决策 TodayDecision：模型概览卡正常、IC/样本数显示为非空
   - 任务中心 TaskCenter：取消按钮仍可用、无 404 崩 UI
2. **操作 B — 错误文案 grep**：从 3 页触发异常（未知 task id cancel / 重复提交 task / 空 scope 查 models）抓所有 HTTP response body，断言：
   ```powershell
   $all_error_bodies | Select-String -Pattern "factor_pipeline" | Should -Be $null
   ```
- **交付物**：测试记录 `tmp/p044-break-regression-YYYYMMDD.md`
- **验收标准**：操作 A 无白屏 + 操作 B 关键词 0 命中；操作后必须还原注释

### 1.5 P0.4-5 验收入口写回总方案（本条目仅在你明确要更新主方案 md 时执行）

- **目标文件**：`docs/factors-refactor-plan.md` 末尾追加 "P0 终态验收 Checklist"
- **内容**：每条 checklist 关联「文件路径 + 验证命令 + 预期结果」
- **验收标准**：其他团队成员按 checklist 步骤 ≈ 20min 可独立复现 PASS/FAIL

---

## 2. 🔶 P1 深化（因子中心内部页继续收敛，不阻塞交付）

> **策略**：因子管理员工作台仍允许"富接口"（因为 CRUD + 生命周期迁移 + 审计要更细节），但会增加一层 Facade 薄封；**禁止新增原生路由的"直接外部调用"**。

### 2.1 P1-1 FactorModelSettings.tsx 12 处原生 → scoring 薄封

| 当前原生方法 | 建议新增 Facade | HTTP | 风险评估 |
|--------------|-----------------|------|----------|
| `listFactorSets` | 复用 `list_factorsets`（已完成） | ✅ 已有 | 无 |
| `getFactorOverview` | 复用 `get_score_runtime_overview` | ✅ 已有 | 无 |
| `getFactorModels` | 复用 `list_score_models` | ✅ 已有 | 无 |
| `listFactorPipelineTasks` | `list_scoring_tasks(task_type='factor_pipeline', limit=N)` | 需新增 `GET /scoring/tasks?type=&limit=` | 低 |
| `getFactorPipelineTask` | 复用 `get_scoring_task`（已完成） | ✅ 已有 | 无 |
| `getFactorPipelineEta` | `get_scoring_pipeline_eta(train_model, full_refresh)` | 需新增 | 低 |
| `createFactorPipelineTask` | 复用 `create_scoring_task`（已完成） | ✅ 已有 | 无 |
| `updateFactorSystemConfig` | `update_scoring_system_config(patch_dict)` — 必须做 schema 白名单（不允许改 feature_enabled 以外的风险字段？） | 需新增 | **中**：配置员权限、写审计；Facade 内先读 FactorSystemConfig 做权限 pre-check |
| `initializeFactorWarehouse` | `init_scoring_warehouse(force=False)` — 先 `get_scoring_overview().warehouse_available` 拦截重复调用 | 需新增 | **中**：敏感初始化操作，必须并发锁 |
| `cancelFactorPipelineTask` | 复用 `cancel_opaque_async_task` | ✅ 已有 | 无 |
| `activateFactorModel` | `activate_scoring_model(model_id)` — 内部走 ActiveScoreScope 原子切换 + 审计 | 需新增 | **高**：生产切换，必须复用 activateFactorModel 内所有门（含 production→shadow fallback） |
| `fallbackFactorModel` | `fallback_scoring_model(reason)` — 记录 audit 事件 SWITCH_SCORE_MODEL | 需新增 | **高**：同上 |

- **工作量**：1~1.5d（集中在高风险 3 条配置/初始化/激活/降级）
- **验收标准**：FactorModelSettings 所有按钮功能未退化 + 删除原生 import 后页面仍能跑通

### 2.2 P1-2 FactorModelPage.tsx 训练 / 冻结 / 激活 链路 scoring 化

- 训练入口：`trainFactorModel(factorsetId, params)` → `scoringTrainModel(factorsetId, params)`。**必须把 P2.3 门禁（coverage≥70 / IC∈[0.01,0.10] / 样本≥10000 / IC差±50%）** 搬到 Facade 层调用，避免"页面跳过门禁直接训练"的绕过风险
- 冻结因子集：`freezeFactorSet(id)` → `scoringFreezeFactorSet(id)` — 并发锁防止训练中被冻结
- 激活 / 降级模型：复用 P1-1 的 activate/fallback Facade
- **工作量**：1d（训练门禁搬运是最大块）
- **验收标准**：在 P2.2 + P2.3 冒烟的 17 + N 条中，再新增 6 条训练门禁"通过 facade 调用"的镜像用例，结果必须与原生调用完全一致

### 2.3 P1-3 因子 CRUD 类（B-3）13 处 → **不迁移，仅加注释守卫**

- **不改 API**：FactorLibrary / FactorEditor / FactorEvaluationLab 需要表单态数据、草稿版 transitions、公式 AST 校验等，迁移成本 >> 收益
- **只改注释**：在 `client.ts` 中把 native factors 方法分组加：
  ```ts
  // ═══════════════════════════════════════════════════════════
  // Factor-domain internal API  —  DO NOT USE outside factor center
  // 外部域页面（策略 / 回测 / 仪表盘 / 数据同步）只能用 Scoring 薄封
  // ═══════════════════════════════════════════════════════════
  listFactorDefinitions: () => ...
  ```
- 组件层 `FactorLibrary.tsx` import 的 api.*调用处 1~2 行补 `// internal: factor-center only`
- **工作量**：< 2h
- **验收标准**：P0.4-2 扫描的 B 类无注释命中 = 0

### 2.4 P1-4 FactorDetail.tsx 裸调用 → 已完成 ✅（P0.3-B4）

---

## 3. 🟡 P2 预防性工作（不影响功能但避免解耦回退）

### 3.1 P2-1 前端 `DO NOT USE` 注释固化

- 在 `client.ts` 中对 **所有非 scoring 方法**（`/factor*`、`/factor-models*`、`/factor-pipeline*`）上方统一加 4 行注释守卫
- 在 TS 层做类型级 hint：给 FactorModelRun / FactorSet 接口上方加 `@deprecated — Use ScoringModelBrief for non-factor pages`（不强制报错，仅 IDE hover 提示）
- **验收**：IDE hover 外部域组件的 FactorModelRun 类型引用时能看到 deprecate warning

### 3.2 P2-2 backend hard_import_gate.py（CI 防回退钩子）

- 在 `conftest.py` 里新增 pytest fixture，跑 pytest 时扫描 `app/**/*.py`（除 `app/services/factors/` 与 `app/models/factor*.py` 之外），assert forbidden import 命中数 = 0
- 首次跑会有 near-relative coupling（如 FactorSet 只读）→ 用 allowlist 显式豁免 + 代码里加 `# near-relative coupling: ...` 注释
- **验收**：新 PR 里若有人误写 `from app.models.factor_model import FactorModelRun`，CI pytest 直接红

### 3.3 P2-3 DTO 版本号机制

- 在 `ScoreModelBriefDTO` / `ScoringFactorSetBrief` / `FactorDraftDTO` 追加 `schema_version: int = 2`（当前第一次加 = 2，1 为无字段版本）
- 前端 API 层 `scoringListModels` 返回后加断言：`if ((x as any).schema_version !== 2) { console.warn('DTO version drift:', x) }`
- **验收**：下次后端加字段（如 factorset_id 之后加 active_weight）时升级到 version=3，前端立刻能在 dev console 看到漂移警告，不会默默 `any` 兼容成 bug

### 3.4 P2-4 ExternalDataSync 语义提示（UX 改进，不碰业务逻辑）

- 之前你提过"启动 G6 灰度"命名不直观 → 改成 **"启动评分流水线"** 或 **"刷新评分特征"**
- 提交成功 toast：`"评分流水线任务已提交，任务号: XXX"` （不出现"因子"内部语义词）
- 状态轮询 error fallback text：当前是"best-effort ignore" → 加 tooltip "评分输入仍在准备中，稍后刷新任务中心查看"

---

## 4. 推荐执行顺序与总工期估算

| 顺序 | 项目 | 工期 | 阻塞关系 | 优先级 |
|------|------|------|----------|--------|
| ① | P0.4-1 终版冒烟脚本 ≥ 37 条 | 0.5d | — | 🔴 必 |
| ② | P0.4-2 前端扫描 + P0.4-3 后端扫描 | 0.5d | — | 🔴 必 |
| ③ | P0.4-4 破坏性回归（关路由 5min） | 0.5d | ①② 通过后再做（避免假 fail） | 🔴 必 |
| ④ | P2-1 DO NOT USE 注释 + P2-3 DTO schema_version | 0.5d | ① | 🟡 建议 |
| ⑤ | P1-1 12 处 Settings 配置页 scoring 化 | 1~1.5d | ④ 先做完守卫，防止同时改动 | 🟠 P1 |
| ⑥ | P1-2 训练 / 冻结 / 激活 走 Facade + 门禁迁移 | 1d | ⑤ | 🟠 P1 |
| ⑦ | P2-2 pytest import 钩子（长效防回退） | 0.5d | ①②③ 终态稳定后再放 CI | 🟡 建议 |
| ⑧ | P0.4-5 写回 factors-refactor-plan.md | < 1h | ⑦ 之前任意时间点，你确认需写时才执行 | ⬜ 可选 |
| **合计** | | **~5d** | — | |

---

## 5. 验收记录模板（执行 P0.4 时在本文件末尾追加）

```markdown
### 验收执行：YYYY-MM-DD
- 执行人：
- P0.4-1 脚本路径：tmp/_p04_final_smoke.py，结果：37/37 PASS / FAIL（附截图或日志）
- P0.4-2 报告：tmp/p042-frontend-audit-YYYYMMDD.csv，A 类命中：N，B 类未注释：N
- P0.4-3 报告：tmp/p043-backend-audit-YYYYMMDD.csv，forbidden：N，near-relative 豁免：N
- P0.4-4 破坏回归：① 关原生路由（5min）外部域 0 白屏 / 否；② error 文案 0 命中 factor_pipeline / 否
- 遗留项：
- 下一步：
```
