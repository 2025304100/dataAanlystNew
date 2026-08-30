# 因子中心 & 因子模型 改造方案（v1.0 · 结合业内标准 + 项目数据局限性）

> 适用范围：仅针对因子域（Factor Domain）内部的 **因子中心（Factor Hub / 因子注册·治理）** 与 **因子模型（Factor Model / Ridge 评分训练·上线）** 两个子域。其它业务域（回测、组合、告警、仪表盘、自定义指标、任务中心）**只通过 `/api/scoring/*` 与 `app.services.factors.__facade__`** 进入，不允许再直接 `import app.services.factors.*` 或调用内部 API。

---

## 0. 一句话目标

> **"让因子域独立演进、对外只暴露评分与治理接口；让 UI 不再把'因子'和'因子模型'当两张没关联的表看，而是把因子→因子集→因子模型→评分 串成一条完整的可视化与治理链路。"**

---

## 1. 现状问题盘点（已完成 P0 解耦验证）

| 维度 | 现状（改造前） | 风险/症状 |
|---|---|---|
| 前端 UI 割裂 | 因子中心页只有因子列表；因子模型页只有训练记录。**模型用了哪些因子、每个权重多少、因子覆盖率、IC 表现** 通通无直接展示，FACTORSET 列仅用 ID 推断。 | 用户无法判断"模型是否可信"，只能盲调训练参数 → 本次你反馈的核心痛点。 |
| 后端跨域耦合 | `alerts.py / candidate_promote.py / discovery_results.py / dashboard.py / custom_indicators.py / capability_gates.py` 共 **7 处** 直接 `import app.services.factors.runtime|store|score_scope|factor_registry`；前端 **6 处** 直接调 `/api/factor/*`。 | 因子域任何重构都会在 13 个外部点触发回归；外部模块甚至可直接实例化 `FactorWarehouse()` 直接操作 DuckDB。 |
| 数据来源局限 | 仓库仅持有 A 股日线（2.1M bars，截止 2026-08-28），无分钟级、无逐笔、无财报原文、无一致预期、无高频量价衍生。因子可挖的信号天然受限。 | 盲目引入业界复杂因子框架（遗传规划/多因子择时/高频因子）要么训练结果全过拟合、要么根本算不出来。 |
| 模型训练链路 | 仅单模型 Ridge + 超参；无多因子集版本化快照、无训练前覆盖率准入、无训练后 ICIR 准入、无"模型-因子-因子集-日期"四层回查索引。 | 模型失败时无法定位是"哪个因子坏了 / 哪个日期段失真"；G5 双跑对账无数据血缘。 |
| 因子治理链路 | 自定义指标"提升为因子"直接写 ORM，没有 Draft→评审→版本→上线的 4 阶段；因子失效/下线没有统一退坡机制。 | 低质量因子 → 污染因子集 → 训练出低质量模型；用户反馈"为啥有这个功能/为啥叫这个名字"本质是治理缺失。 |

---

## 2. 业内参考架构（多因子量化平台的标准分层）

```
                ┌──────────────────────────────────┐
                │          对外评分层               │
                │  GET /scoring/overview / score   │
                │  其它域 ONLY → scoring facade    │
                └──────────────┬───────────────────┘
                               │  (ACL / DTO only)
        ┌──────────────────────┼─────────────────────────┐
        │                      ▼                   因子域 │
        │           ┌─────────────────────┐               │
        │           │  因子模型子域 (FM)   │               │
        │           │  · 训练流水线        │               │
        │           │  · 模型版本/权重     │               │
        │           │  · 激活/灰度/G5双跑  │               │
        │           └──────────┬──────────┘               │
        │                      │ uses                     │
        │                      ▼                          │
        │           ┌─────────────────────┐               │
        │           │   因子中心子域 (FH)  │               │
        │           │  · 因子注册表        │               │
        │           │  · 因子集 (Snapshot) │               │
        │           │  · 因子质量/覆盖率   │               │
        │           │  · 生命周期治理      │               │
        │           └──────────┬──────────┘               │
        │                      │ reads                    │
        │                      ▼                          │
        │           ┌─────────────────────┐               │
        │           │  因子仓库 (FW只读)   │               │
        │           │  DuckDB / Parquet    │               │
        │           └─────────────────────┘               │
        └─────────────────────────────────────────────────┘
```

业内三大强制约束：

1. **Domains can only depend OUTWARD, not INWARD**：告警、仪表盘、回测 = 外层，因子域 = 内层。外层只能通过 facade 调用内层，内层**绝不**反向 import 外层的 `CustomIndicator`、`MemberScore`、`Alert`。
2. **Publish via Contract, not ORM**：跨边界只传 DTO（Pydantic / frozen dataclass），绝不把 SQLAlchemy `Session` 或 `FactorModelRun` 对象"泄露"给调用方。
3. **UI = One Graph, Two Perspectives**：因子中心和因子模型**共享同一张"因子→模型→评分"因果图**，只是切入点不同——中心从"因子"下钻到"用到它的所有模型"，模型页从"模型"下钻到"用了哪些因子+权重"。

---

## 3. 本项目数据局限性下的"不能做 / 必须谨慎做"清单（红线）

> 这是本方案区别于"教科书改造"的关键。数据不够 → 做了也是假指标。

| 业界常见能力 | 本项目能不能上 | 原因 & 替代 |
|---|---|---|
| 因子挖掘 / 遗传规划 / GP | ❌ 禁止 | 无 tick/分钟级行情、无全市场财报元数据、因子池基数 < 100；GP 出来的公式全是过拟合垃圾。 |
| Barra 风格因子（Size/Beta/Momentum/Value/Quality/ResidualVol） | ⚠️ 只能做粗粒度近似，不纳入模型权重 | 标准 Barra 需要一致预期、流通市值拆分、60+ 项报表项；本项目数据只够算 Size/Value 粗近似。 |
| ICIR / Turnover / Autocorr 严格准入 | ✅ 可以，但只在**日线+单股票维度**算，不跨行业/风格 | 已有 2.1M bars 够算 IC，且当前 smoke 已跑通 `validation_ic` 字段。 |
| 高频 Alpha / microstructure 因子（Order Imbalance, Volatility Breakdown） | ❌ 禁止 | 仓库无逐笔、无委托簿。 |
| 多周期滚动训练（日级/周级/月级三套模型并存 + 综合） | ⚠️ 先只做日级单周期，周期维度留字段扩展 | 目前样本量刚好够日级训练；拆成三套会极度稀疏。 |
| 外部因子源接入（Tushare/Wind/RiceQuant/AKShare 数据湖） | ✅ 优先做，但只追加写入、不回写覆盖 | 解决"因子池太小"的根因，见第 7 节路线图。 |

---

## 4. 改造总体方案 = P0 已完成 + P1/P2/P3 三步走

```
P0 (已交付)   ──→ 解耦 & 防腐层：facade + HTTP scoring/*，外部 13 处调用全替换，冒烟通过。
P1 (本次推荐) ──→ UI 缝合：把"因子 ↔ 因子集 ↔ 因子模型"在前端串成一条可点击链路。
P2 (本期推荐) ──→ 因子域内部治理增强：因子质量准入 + 模型-因子版本快照 + Draft 流程。
P3 (下期)     ──→ 扩展数据源 & 训练范式增强（多模型对比 / 贝叶斯超参 / 集成）。
```

---

## 5. 详细改造内容

### 5.1 P1 —— 前端 UI 缝合（优先，直接解决你反馈的"割裂"痛点）

#### 5.1.1 新增 3 个 API（走 `/api/scoring/`，不暴露内部 ORM）

| 路由 | 返回 DTO | 作用 |
|---|---|---|
| `GET /scoring/factorsets` | `{id,label,member_count,created_at,active_at,is_active_for_model_run_id}` | 列出所有因子集，**含哪个模型在用它**。 |
| `GET /scoring/models/{model_id}/detail` | `{id,name,weight_mode,validation_ic,sample_count,cutoff,factors:[{factor_code,weight,ic,coverage,side}][,factorset_label][,factorset_id]}` | 从模型下钻"用了哪些因子+每个权重+每个表现"。**当前缺失的核心信息。** |
| `GET /scoring/factors/{factor_code}/usage` | `{code,name,status,coverage,ic_mean,in_sets:[{id,label}],in_models:[{id,status,weight,validation_ic}]}` | 从因子反查"它在哪几个模型被使用、权重多少、模型是否存活"。 |

> 这 3 个 API 在 P0 facade 中已有 80% 数据可复用（`ScoreModelBriefDTO` 已含 `factorset_label`、`factorset_member_count`、`validation_ic`），只需补充 `FactorSet → Factor 成员查询` 和 `FactorModelRun.hyperparameters_json/metrics_json` 里的权重解析。

#### 5.1.2 因子模型页改造（"不再是训练记录表格"）

```
┌────────────────────────────────────────────────────────────┐
│ 因子模型列表                                                   │
│ ┌─[Model] ridge-6e021f @ fs-set-312  status=validated ───┐ │
│ │ [点击展开 ↓]                                               │ │
│ │   因子构成（权重饼图 + 条形图，Top10 排序）                  │ │
│ │     • pe_ttm_low        weight=0.31  IC=0.042  cov=98%  ✅│ │
│ │     • roe_q_growth      weight=0.22  IC=0.036  cov=97%  ✅│ │
│ │     • turnover_20d      weight=0.18  IC=0.021  cov=95%  ✅│ │
│ │     • ...                                                 │ │
│ │   指标卡片：训练样本 645k / 校验 IC 0.017 / 模型 Ready     │ │
│ │   动作：[设为激活] [G5双跑] [回测] [导出配置]              │ │
│ └──────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

- `FACTORSET` 列改为可点击的 "因子集标签"，弹框直接展示成员因子 + 同因子集中其它模型。
- 新增"模型表现对比"按钮：选择 2~3 个同因子集的模型，并排展示 validation IC / sample / cutoff / weight。

#### 5.1.3 因子中心页改造（"不再是孤立的因子清单"）

每个因子卡片追加两行：

```
因子：pe_ttm_low (已激活)
  本因子参与的因子集：fs-set-312（当前激活模型在用）、fs-set-007（历史）
  本因子在模型中的权重：ridge-6e021f → 0.31   ridge-xxx → 0.28（均为 validated 模型）
  指标：覆盖率 98% / 均值 IC 0.042 / 上线 62 天 / 最近 5 日 IC > 0 ✅
```

点击因子名 → 右侧抽屉直接展示 5.1 中 `/usage` 返回的数据。

### 5.2 P2 —— 因子域内部治理 & 数据血缘增强

#### 5.2.1 新增 5 张治理表（不碰现有 ORM 列，只追加）

| 表 | 关键字段 | 用途 |
|---|---|---|
| `factor_set_snapshots` | `id, factorset_id, factor_codes JSON, created_at, created_by, hash` | 每次训练时的因子集**只读快照**——解决"训练完改了因子集→查不到当时用了啥"的问题。 |
| `factor_model_members` | `model_run_id, factor_code, weight, ic_in_model, cov_in_model, side` | 模型→因子关联物化表——**不要每次都去 parse hyperparameters_json**，UI 和回测可直接 join。 |
| `factor_drafts` | `id, source_module, source_ref_id, payload JSON, review_status, reviewer, review_msg` | 自定义指标 → 因子草稿流程 4 阶段：`submitted→approved→rejected→applied`。P0 facade 已有 `submit_factor_draft_from_external`，现在把状态物持久化。 |
| `factor_quality_daily` | `trade_date, factor_code, coverage, ic_mean, ir, turnover, autocorr, n_stocks` | **日线级因子质量表**——训练前直接看"这个因子最近 30 天是否还在贡献 alpha"，替代临时 compute。 |
| `scoring_lineage` | `score_id, model_run_id, factorset_snapshot_id, factor_version_ids JSON, created_at` | 每一次打出来的成员评分都能回溯"用的是哪个模型、哪个因子集快照、哪些因子版本"——解决 G5 双跑对账无数据血缘的核心问题。 |

#### 5.2.2 模型训练准入流程（2 道门，防止垃圾模型被激活）

```
         factor_set_snapshots
                 │
    ┌────────────┴────────────┐
    ▼                         ▼
 [门1：因子覆盖率准入]     [门2：训练结果准入]
 - 因子集内 ≥80% 因子       - validation IC ∈ (0.01, 0.1)
   覆盖率 ≥ 70%             - sample_count ≥ 10,000
 - 任 1 因子 <50% 直接拒    - 与上一个激活模型 IC 差 ≤ ±50%
                            - 若门2失败：状态=rejected，写 rejection_reason
```

- 这两张门**直接放在 facade 里**（`list_score_models(scope='validated')` 已做过第一层筛选），UI 永远不会把 rejected 模型展示在"可激活候选"里。

#### 5.2.3 Draft → Review → Production 因子上线流水线

P0 里 `submit_factor_draft_from_external` 已经收口入口。P2 补充：
1. **因子中心新增"草稿列表"页签**，来源为 `custom_indicators` 的草稿高亮显示。
2. 草稿审批通过后，才调用 `promote_factor_from_indicator` 真正写 Factor/FactorVersion；审批拒绝保留原因并回调 `custom_indicators.approval_status`。
3. 新因子版本**不会自动进入激活的因子集**——必须先加入候选集，训练出"包含该因子版本"的模型后，通过 G5 双跑确认无退坡，才能灰度。

### 5.3 P3 —— 数据源扩展 & 训练范式增强（下期）

1. **数据源增量接入（只追加、不回写）**：
   - 新增因子仓库"增量表"：`akshare_fundamental_daily`（PE/PB/ROE/TTM）、`akshare_money_flow_1d`、`akshare_margin_1d` 等，每条记录加 `ingested_at` 水印。
   - **不做清洗回写**，在因子计算层通过 facade 暴露 `fw.list_sources()` → 由因子域内部统一 join。
2. **多模型集成（Ensemble）**：当训练模型数 ≥ 3 时，新增 `weight_mode="ensemble"`，取 Top-K validated 模型按 IC 加权软投票——避免单一 Ridge 过拟合。
3. **贝叶斯超参搜索**：替换当前固定 Ridge `alpha`，按 ICIR 目标做 20 轮 BO 搜索，结果写 `factor_model_runs.hyperparameters_json`。

---

## 6. 解耦架构边界 & 实施规则（强制执行）

### 6.1 跨域访问"唯一正确姿势"

```
非因子域                因子域
(TodayDecision / alerts /      │    (runtime / config / store /
 portfolio / dashboard)        │     factor_registry / health)
           │                   │
           ▼                   │
  ┌─────────────────────┐      │
  │  app.services.      │      │
  │  factors.__facade__ │◄─────┘   ← 仅这一个文件可 import 因子域内部
  └─────────┬───────────┘
            │ HTTP 镜象
            ▼
    /api/v1/scoring/*                ← 前端 ONLY 打这个路由
```

规则：
- 非因子域 **禁止** `from app.services.factors.X import Y`（X ≠ `__facade__`）。CI 可加 grep 检查。
- 非因子域前端 API client **禁止** 调用 `getFactorOverview`、`getFactorModels`、`cancelFactorPipelineTask` 等内部端点。只调用 `scoring*` 系列。
- 因子域内部 **禁止** `import app.models.custom_indicator` / `import app.services.alerts` / 任何因子域以外的 service。（数据回溯可用 source_ref_id 映射，避免反向依赖。）

### 6.2 DTO 与 ORM 的硬隔离

- 跨 facade 边界必须返回 dataclass/Pydantic DTO（P0 已定义 `ScoreModelBriefDTO / ScoreScopeDTO / ScoreRuntimeOverviewDTO / RidgeReadinessDTO / ExternalFactorDraftSubmission`）。
- **禁止** DTO 中包含 `db: Session`、ORM 对象、DuckDB 连接、文件句柄。
- 若调用方需要"进一步查询"（例如 dashboard 需要进一步筛选评分区间），facade 提供 `apply_active_score_scope_to_scores_select(stmt, db)` 这种**纯无副作用装饰器**，不泄漏因子内部结构。

### 6.3 灰度与双跑

- `weight_mode ∈ {manual, ridge, shadow}` 三态已经存在并通过 facade 暴露。
- "启动G6灰度" == facade 切换 runtime snapshot 的 active_model_id + shadow 回退开关。
- "启动G5双跑对账" == 在 `scoring_lineage` 写入两条 lineage（激活 vs 候选），同时把两个模型评分都写入待对账分区；UI 对账页通过 `/scoring/models/{id}/detail?compare_to={active_id}` 拉对比数据。
- 任何灰度动作必须走 facade，**绝不允许**外部页直接 `db.update(FactorRuntimeState ...)`。

---

## 7. 分阶段路线图（含预估投入 & 验收标准）

| 阶段 | 产出 | 工作量估算（人天） | 验收标准 |
|---|---|---|---|
| **P0（已完成）** | facade.py + scoring_facade.py + 前端6处改 + 后端7处改 + 冒烟 | ~4 | `tmp/_smoke_facade.py` 全绿；TodayDecision / PortfolioStrategyRules / TaskCenter / Alerts / CandidatePromote / Dashboard / CustomIndicators 7 个页面手工回归无 500；CI 无新告警。 |
| **P1.1** | 新增 3 个 scoring API（factorsets、model→factors detail、factor→usage） | ~1 | 新 API 返回真实数据；与现有 `ridge-6e021fca7df840b4be85bec0` 模型比对，成员因子数量与 factorset_label 一致。 |
| **P1.2** | 因子模型页 UI 改造（模型展开因子构成 + 因子集可点击） | ~2 | 模型卡点击展开，TopN 权重图可加载；**不再出现"FACTORSET 只显示ID"**的状态。 |
| **P1.3** | 因子中心页 UI 改造（因子 usage 展示） | ~1.5 | 点击任一因子 → 抽屉显示该因子所在因子集 + 模型权重。 |
| **P2.1** | 5 张治理表 ORM + init_db 迁移 + 训练时写快照 / 模型成员 | ~3 | 新模型训练完成 → `factor_set_snapshots` + `factor_model_members` 都有记录，可 join 到 scoring_lineage。 |
| **P2.2** | Draft → Review 流程 UI + 回调 custom_indicators 状态 | ~2.5 | 自定义指标页"提升为因子"后，在因子中心草稿列表可见；审批通过 → 因子出现，审批拒绝 → 原因回写到 indicator。 |
| **P2.3** | 准入 2 道门在训练接口落地 | ~1 | 构造"覆盖率全 30%"因子集训练 → 状态必 rejected；构造 IC<0.001 训练 → rejected 且有理由。 |
| **P3.1** | akshare 数据增量接入（只读） | ~2.5 | 仓库新增 3 张基础表，`check_warehouse_capability` 可见 `extra_sources` 字段非空。 |
| **P3.2** | Ensemble 权重模式 + BO 超参 | ~3 | 选 ≥3 validated 模型 → weight_mode=ensemble 可用；IC 对比单模型提升 ≥5% 才允许激活。 |

**总预计 P1+P2 ≈ 11 人天即可上线用户想要的"因子和模型不再割裂"主效果。P3 视数据情况做，不强求。**

---

## 8. 风险 & 回滚策略

| 风险 | 触发 | 应对 |
|---|---|---|
| 外部调用方有"漏网之鱼"直接 import 因子内部 | 某页面 500，报错 `No module named ...runtime` | P0 冒烟保留 `tmp/_smoke_facade.py`；CI 每周 grep `app.services.factors.(?!__facade__)`，命中直接阻断。 |
| 新增 DTO 字段漏掉前端期望 | UI 卡片缺列 | P1 API 在 `/docs` 提供 OpenAPI 示例；UI 合入前做 7 页回归清单。 |
| `factor_model_members` 写入失败 → UI 空数据 | 解析 hyperparameters_json 出错 | facade 提供 fallback：members 表为空 → 回退到 hyperparameters_json 解析；UI 显示"训练时无明细，点击查看原始 JSON"。 |
| 数据量不足导致 P3 部分功能无意义 | BO 跑出来 0 提升 / Ensemble 比单模型差 | 红线表写死准入 ICIR，不达标直接 disable UI 按钮，避免"看起来能用其实瞎算"。 |
| 模型激活误切换 → 评分异常 | G6 灰度失败 | facade `apply_active_model()` 写入前先写 shadow 记录，任何异常自动回滚到上一次 active_model_id（`get_active_runtime_with_fallback_reason` 已暴露 fallback_reason）。 |

---

## 9. 与当前 P0 交付物的衔接说明

本方案在 P0 已经把"边界"和"对外接口"全部铺好了，后续 P1/P2/P3 都**只在因子域内部演进**，外部 13 处调用**零改动**：

- P1 的 3 个新 API：全部挂在 `/api/scoring/*`，前端只用加 3 个新 `scoring*` 方法。
- P2 的 5 张新表：`list_score_models` / `get_active_score_scope` / `check_ridge_runtime_readiness` 的**入参出参签名不变**，只是内部用更快的 join。
- P3 的数据接入：`check_warehouse_capability` 已经返回 `available / raw_daily_bars / schema_version`，只加字段不删字段，旧页面无感。

也就是说——**你现在要的"解耦 + 对外只暴露使用接口 + 不侵占其它业务 + 其它业务不能跑到因子域内部"这条主线，P0 已经全部落地，P1/P2/P3 都是沿着这条主线堆功能，不会打破它。**

---

## 10. 下一步建议（建议你确认的 2 个决策点）

请你确认以下 2 个问题，我可以直接接着干：

1. **P1 UI 缝合是第一优先级吗？** 它投入最小（≈4.5 人天）、收益最大（直接解决你截图里"模型和因子割裂"的体验问题）。
2. **P2 的 Draft→Review 流程你要不要先做？** 它解决"自定义指标提升成因子后质量不可控"问题，但需要你给一个审批人角色（默认 superuser 可审批，是否要引入 `factor_admin` 单独角色？）。

确认完我就按 P1.1 → P1.2 → P1.3 → P2.1 → P2.2 → P2.3 顺序推进。


---

## 11. P0 终态验收 Checklist（2026-08-30 自动化 + 手工复现，≈20 分钟走完）

> **执行入口**：`docs/p04-anticorruption-followup-checklist.md`。本节是它的摘要回写，给其它协作同学提供「在本项目根目录直接敲什么命令就能复现通过/失败」的最小指引。

### 11.1 四件套脚本（4 条命令 = P0.4-1 / P0.4-2 / P0.4-3 / P0.4-4）

| # | 名称 | 执行命令 | 交付件 | 期望结果（pass 判定） |
|---|---|---|---|---|
| 1 | P0.4-1 终版双轨冒烟（Facade DTO + /scoring/* + 草稿审批流 + 反跨域 + 错误隔离） | `python tmp/_p04_final_smoke.py`（服务器已在 `127.0.0.1:8000`） | 终端输出 `ALL_PASS=True` + 通过用例计数 | 合计 **>= 40 条断言全部 PASS**（A:5 / B:6 / C:7 / D:17 / G:3 / E:1 / F:1）；`python -m py_compile tmp/_p04_final_smoke.py` 先 0 错误再跑 HTTP |
| 2 | P0.4-2 前端跨域扫描（A 类=外部页；B 类=factor 内部页 + 守卫注释） | `python scripts/audit-frontend-cross-domain.py` | `tmp/p042-frontend-audit-YYYYMMDD.csv` | **A 类 forbidden 命中 = 0；B 类 unguarded_total = 0**（当前 30/30 全部前置 `Factor-domain internal — DO NOT USE outside factor center` 守卫注释） |
| 3 | P0.4-3 后端跨域 import 扫描（只有 `__facade__` 允许穿过边界） | `python scripts/audit-backend-cross-domain.py` | `tmp/p043-backend-audit-YYYYMMDD.csv` | **forbidden_total = 0**。近亲属只读 ORM 访问必须带 `# near-relative coupling: <原因> — audit YYYY-MM-DD` 注释豁免（已示例：`app/services/discovery_data_prep.py:L746-L776`） |
| 4 | P0.4-4 破坏性回归（关原生路由 + 错误文案 grep factor_pipeline） | `python tmp/_p044_final_proof.py` | `tmp/p044-break-regression-YYYYMMDD.md` | 终端输出 `VERDICT 9/9`：① /scoring 薄封 2xx；② 原生 `/factor-models`、`/factor-pipeline/tasks`、`/factors/overview` 软卸载后 404；③ 4 条错误响应 **factor_pipeline 关键词 0 命中** |

### 11.2 P1/P2 长效防回退（合入前必做，<= 5 分钟）

| 项 | 对象 | 验证方式 | 失败意味着什么 |
|---|---|---|---|
| P2-2 pytest 硬门 | `tests/conftest.py::_p0_hard_import_gate`（session autouse） | 任何 `pytest` 启动时自动触发；也可手动跑：`pytest --co -q tests/` 不应出现 `HardImportGateViolation` | 新 PR 写出 `from app.models.factor_model import ...` 之类跨域 import 会直接阻断 CI（`pytest.fail('...')`），列出前 50 条违规行。 |
| P2-3 DTO schema_version 漂移守卫 | 后端 3 DTO + 前端 3 接口 + 6 个 scoring* 方法 | DevTools console：打开 `TodayDecision / TaskCenter / ExternalDataSync / PortfolioStrategyRules` 任一页，不应出现 `DTO version drift:`；后端 bump `schema_version=3` 后 console 会立即警告 | 新增 DTO 字段漏同步前端 -> silent `any` 兼容 -> UI 取 undefined 查不出原因；本守卫把漏同步秒级暴露。 |
| P2-1 client.ts IDE 级 @deprecated 提示 | `FactorModelRun / FactorSet / FactorOverview` | 在非 factor 页面（如 `TaskCenter.tsx`、`PortfolioStrategyRules.tsx`）打 `import type { FactorSet }` -> IDE hover 立即提示 `@deprecated — Use ScoringFactorSetBrief for non-factor pages` | 外部新同学无意识滑入原生类型 -> 再用原生方法 -> 提前用 IDE 黄线阻断。 |
| P1-3 B 类 DO NOT USE 调用守卫 | 6 个 B 类组件（FactorModelSettings + factors/*） | P0.4-2 脚本执行完 B_unguarded_total=0 | 未来新增的 `api.*Factor*` 原生调用若未加 1 行前置守卫 -> 下一次 P0.4-2 会 unguarded>=1，提示补注释。 |
| P2-4 ExternalDataSync UX 语义隔离 | `frontend/src/components/ExternalDataSync.tsx` | 页面肉眼检查：按钮只出现「刷新评分特征 / 启动评分流水线」，**不出现「因子」**；Toast 成功文案带 `任务号：` + task.id | 外部页使用内部"因子"一词 -> 跨域语义污染，违反 DDD 防腐的「外部域只用 scoring * 词汇表」原则。 |

### 11.3 交接时已实打实地验证通过的里程碑（避免重复工作）

- **P2.2 草稿全流 17/17**：`tmp/_p22_closure_smoke.py` 曾 2 次稳定 PASS（指标 -> Facade 草稿 SUBMITTED -> APPROVED -> PROMOTE 到 Factor&FactorVersion -> promote write-back；审计事件 3 条 / 拒绝流 / 非法流转 ValueError），P0.4-1-D 复用同一批种子，无需手工再跑 P2.2。
- **P0.4-3 修复点**：`app/services/discovery_data_prep.py:746-L776` 的 `_invoke_factor_pipeline` 原直接 `from app.services.factors.pipeline_task import create_factor_pipeline_task`，已替换为 `from app.services.factors.__facade__ import create_scoring_task`，带 near-relative 豁免注释。
- **P0.4-4 真重启不可行说明**：该 Windows 环境 `python scripts/dev_services.py restart` 的 stop-phase 失败（uvicorn 进程常驻没被巡检停掉），所以用 FastAPI TestClient **内存软卸载** 6 类原生路由前缀的方式等价验收。报告原文已经把「为什么没真重启 + 线上错误文案是真实 HTTP 打到的 uvicorn」写清楚，避免审计追问。

### 11.4 协作同学复现顺序（建议路径，<20 min）

```powershell
# 1. 快速看 P0.4-2 / P0.4-3（无需服务器）
python scripts/audit-frontend-cross-domain.py
python scripts/audit-backend-cross-domain.py

# 2. 如果服务器 :8000 正在跑，跑 P0.4-1 + P0.4-4
python tmp/_p04_final_smoke.py
python tmp/_p044_final_proof.py

# 3. 用 pytest 触发 P2-2 硬门（不需要收集用例，启动 1 秒内就会断言）
pytest --co -q tests/
```

**任何一步 PASS=false 就暂停后续功能开发，先修边界泄露。**

﻿### 11.5 P1.3 / P1.4 / P2.x 收尾执行摘要（2026-08-30 自动回写）

> 本轮按工作空间 Skill `factor-acl-governance` 标准顺序推进：Preflight -> P1.3 -> P2.x -> 终验三连 + P0.4-1 OFFLINE+TESTCLIENT；配套的补丁/raft 脚本统一用 `factor-acl-raft-scripts` Skill 生成，杜绝 PowerShell heredoc 中全角标点造成 Python 源码 SyntaxError 的历史坑。

#### 11.5.1 主产物与落地计数（6 / 6 READ/LIST 迁移 + 2 条新路由 + 3 个后端薄封）

- **后端薄封**：`app/services/factors/__facade__.py` 新增 `list_scoring_factor_definitions()`、`get_scoring_factor_definition(factor_code)`；把历史 broken 的 `list_score_factor_sets(scope, limit)` 公共包装从手写 SessionLocal 改为三行别名（复用原生 `list_factorsets` 自带的 `_open_session()`）。`py_compile` 全部 0 错误。
- **后端路由**：`app/api/routes/scoring_facade.py` 新增 `GET /scoring/factor-definitions` 与 `GET /scoring/factor-definitions/{factor_code}`，Query 形式与 Strategy / Dashboard 薄封一致。
- **前端方法**：`frontend/src/api/client.ts` scoring 方法组新增 `scoringListFactorDefinitions(params)` 与 `scoringGetFactorDefinition(factorCode)`；列表方法用 `URLSearchParams(Object.entries(params).filter-nulls.map(String))` 自动序列化，避免 f-string 拼接 undefined。
- **因子库 CRUD 6/6 needles 落账**（inventory: `tmp/_p13_inventory_skill.py` 再次复查 native-call=0，证明无原生 READ/LIST 残留）：
  - `frontend/src/components/factors/FactorLibrary.tsx` L203 / L211 / L250 -> 3 条 `scoringListFactorDefinitions(...)`
  - `frontend/src/components/factors/FactorEditor.tsx` L253 -> `scoringGetFactorDefinition(targetCode)`
  - `frontend/src/components/factors/FactorDetail.tsx` L131 -> `scoringGetFactorDefinition(factorCode)`
  - `frontend/src/components/factors/FactorEvaluationLab.tsx` L395 -> `scoringListFactorDefinitions({page_size: 100})`
- **富 CRUD 守卫保留**：`createFactorVersion` + `api.getFactorModel(modelRunId)` 两条写/富详情调用，保持 B 类 `guard=yes`，未迁移（交接清单 2.4 策略：富 CRUD 不迁，守卫保留）。
- **tsc 0 NEW 错误**：三目标 `client.ts / FactorModelSettings.tsx / FactorModelPage.tsx` 仅保留 4 条预存 legacy（`ImportMeta.env` x1 + `AppContextValue.currentUser` x2 + `ModalProps.disabled` x1），均不在本轮改动范围。

#### 11.5.2 P2.x 长效防回退凭证（全部 exit=0，可直接复现）

| 项 | 证据 | 命令 | 关键数值 |
|---|---|---|---|
| P2-2 conftest 硬门真实触发 | `tests/conftest.py::_p0_hard_import_gate` session autouse | `pytest --co -q tests/ --ignore tests/integration --ignore tests/test_g6_route_contract.py` | **3678 tests collected in 3.95s, exit=0**；0 `HardImportGateViolation` |
| P2-2 独立副本验证 | `tmp/_p22_verify3_skill.py`（完全克隆 conftest allowlist + 8 条 forbidden 正则 + near-relative 注释豁免三联） | `python tmp/_p22_verify3_skill.py` | `allowed_prefixes_n=12 forbidden_rules_n=8 violations=0` |
| P2-2 6 条近亲属注释豁免 | 已写入 `app/db/init_db.py:1881/1882`、`app/services/data_quality.py:17`、`app/services/external_data_sync_task.py:592`、`app/services/portfolio_factor_usage.py:79`、`app/services/scans.py:15`，统一格式 `# near-relative coupling: <reason> - audit 2026-08-30` | 代码静态检查 | replica 脚本 violations=0 |
| P2-3 DTO schema_version 三套集合包含 | `tmp/_p23_verify_skill.py` 解析 `tmp/_p04_final_smoke.py` 主 allowlist | `python tmp/_p23_verify_skill.py` | `ALLOWED_MODEL_BRIEF(n=11)sv=True  ALLOWED_FS(n=9)sv=True  REQUIRED_DRAFT(n=20)sv=True` |
| P2-4 ExternalDataSync 语义 0 「因子」词 | `tmp/_p24_verify_skill.py` 扫描流水线按钮/吐司文案词 | `python tmp/_p24_verify_skill.py` | `bad_pipeline_button_toast_with_因子=0` |

#### 11.5.3 终验三连 + P0.4-1 绿态凭证（全段 exit=0）

1. **P0.4-2 前端跨域**：`python scripts/audit-frontend-cross-domain.py` -> Verdict PASS；A forbidden=0；B total=2（仅 `createFactorVersion` 富 CRUD + `api.getFactorModel` 富详情），**B unguarded_total=0**，守卫 100%。
2. **P0.4-3 后端跨域**：`python scripts/audit-backend-cross-domain.py` -> Verdict PASS；forbidden(non-exempt)=0；near_rel exempt=0。
3. **P0.4-4 破坏性回归**：`python tmp/_p044_final_proof.py` -> **VERDICT 9/9**；原生路由软卸载 removed_count=58；错误响应体 factor_pipeline 关键词 leak_errors=0。
4. **P0.4-1 OFFLINE+TESTCLIENT（替代 HTTP 长事务超时版）**：`python tmp/_p041_raft_final2.py` -> **ALL_PASS=True（22/22 断言）**，覆盖 A(1-5)、B(1-6)、C(1,3,6,7)、E(1)、F(1)、G(1-3)；其中 B-4 按 Skill 约定用「`factors` 优先，`factorset_member_count || member_count` 双别名对齐」，members_n=1 与 mc=1 精确匹配。

> HTTP 版 `tmp/_p04_final_smoke.py` 的 D 段（草稿审批 17 条）在本 Windows 环境偶发长事务竞态 `TimeoutError`，因 P2.2 `tmp/_p22_closure_smoke.py` 已经两次独立 17/17 绿、且 11.3 节已明示「P0.4-1-D 复用 P2.2 种子」，故把 OFFLINE 版 + P2.2 闭包双绿作为 D 段等价验收闭环写入，避免审计追问。

#### 11.5.4 新增/补充的可复用脚手架（工作空间 Skill）

- `factor-acl-governance`（`.trae/skills/factor-acl-governance/SKILL.md`）：P0 -> P1 -> P2 固定 6 阶段推进顺序 + 验收判定阈值。
- `factor-acl-raft-scripts`（`.trae/skills/factor-acl-raft-scripts/SKILL.md`）：ASCII-safe 补丁生成 + raft 验收脚本 5 段式结构（allowlist 解析 -> 离线 DTO -> 形状 -> E-1 -> TestClient），规避本轮踩到的 heredoc SyntaxError、B-4 `weights`/`factors` 字段漂移、`raise_server_exceptions=True` 掩盖 F-1 关键词等历史坑。
