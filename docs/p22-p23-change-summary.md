# 因子治理 P2.2 + P2.3 变更摘要

- **适用范围**：因子域（Factor Center / Factor Model）治理链路，外部仅通过 `app/services/factors/__facade__.py` 与 `app/api/routes/scoring_facade.py` 交互
- **落地日期**：2026-08-29
- **验证状态**：冒烟测试 31/31 全部通过（见本文末尾 §8）
- **总体原则**：
  - 只做"纯副作用"接入：**不修改 legacy 门禁与业务逻辑**，仅在 rejection_reasons 追加原因、在模型仍被持久化的前提下改变 status、写审计
  - 所有新阈值均可通过 `FactorSystemConfig.extra_json["gates"]` 覆写，未配置走 DEFAULT 硬编码（§4.2）
  - 训练永远创建 `FactorModelRun` 记录（即使门禁 fail 也只是 `status=rejected`），避免「点击训练 UI 无响应」回归

---

## 1. P2.2 草稿治理闭环（custom_indicators → factor promotion pipeline）

> 原改造计划 P2.2：外部草稿提交 → 审批 → 上线。把 custom_indicators「保存即自动 promote」拆为：提交仅落草稿、审批通过才真正 promote、失败/驳回回写 custom_indicators 审批态。

### 1.1 入口与状态机

- **提交入口**：`submit_factor_draft_from_external(source_module, source_ref_id, payload, actor)` → DTO `ExternalFactorDraftSubmission`
  - 仅允许 `source_module ∈ {"custom_indicators"}` 走 CI 专用分支（校验 custom_indicators 存在、value_type=number、与 factors.code 去重）
  - 任一前置校验失败：草稿 `review_status = "rejected"` + `review_msg` + 返回 `rejected_draft`，HTTP 路由封装为 422 + extras 带 draft_id
- **状态枚举**（`FactorDraft.review_status` DTO 为 `review_status`）
  - `submitted → approved / rejected`
  - `approved → applied`（promote 成功）或回滚回 `submitted`（promote 失败 + `review_msg` 记录错误）
  - `rejected / applied / deleted` 为终止态，`reject_factor_draft` 二次调用幂等，向终止态做非法跃迁（如 `rejected→approved`）抛 `ValueError: illegal_transition:<from>→<to>`
- **终止态外部化**：CI 表审批态与 factor_drafts.review_status **双向一致**，见 §1.4

### 1.2 2.2a 提交流程（不再自动 promote）

旧行为：custom_indicators promote 在保存时立刻做，错漏无人工复核。

新行为（`__facade__.py:submit_factor_draft_from_external`，source_module=custom_indicators 分支）：
1. **3 项低成本前置校验**（失败直接 rejected_draft，不进审批队列）
   - `source_ref_id` 必须为数字型 custom_indicators.id（`_parse_indicator_id` 解析）
   - custom_indicators 记录存在 + `value_type == "number"`（boolean/string 类公式不具备因子信号能力）
   - 建议 `factor_code` 与 `factors.code` 不冲突（promote 阶段会再查一次，先拦截无效草稿）
2. **草稿落库**：`factor_drafts` 写 `submitted`，`payload_json` 保存自定义指标 formula/display_name/category 等
3. **回写 CI**：`custom_indicators.approval_status = "submitted"` + `factor_draft_no = <draft_no>`（`_update_custom_indicator` 工具函数，静默失败）

### 1.3 2.2b 审批 → promote

`approve_factor_draft(draft_no, reviewer, review_msg?)`：
- 先做状态跃迁合法性（`LEGAL_TRANSITIONS: submitted → {approved, rejected, applied}`）
- 对 source_module=custom_indicators：调用 `promote_factor_from_indicator(indicator_id, suggested_code, ...)`
  - promote 成功 → `review_status = "applied"` + CI 回写 `approval_status="applied"` + `promoted_factor_code=<新因子code>`
  - promote 失败 → **回滚** review_status 回到 `submitted` + `review_msg=异常信息` + CI 回写 `approval_status="promote_failed"`，避免 promote 失败把草稿卡死在 approved

### 1.4 2.2c 驳回 → 回写 CI

`reject_factor_draft(draft_no, reviewer, review_msg)`：
- 仅允许 `submitted → rejected`；终止态幂等返回
- CI 回写 `approval_status="rejected"` + `approval_review_msg=review_msg`
- 前端「草稿」Tab 展示时，从 `FactorDraftDTO.review_msg` 取出原始驳回原因

### 1.5 2.2d 前端接入（FactorLibrary「草稿审批」Tab）

- TS DTO / 客户端 API：`frontend/src/api/client.ts:ScoringDraftListItem` + `approveFactorDraft / rejectFactorDraft / submitFactorDraftExternal / listScoringDrafts / getScoringDraft`
- UI：FactorLibrary.tsx
  - 草稿 Tab：按 status 过滤（all / submitted / approved / rejected / applied）+ 行操作 approve / reject（必填原因）+ drawer 打开 payload_json 详情
  - 错误态：`ILLEGAL_TRANSITION` / `rejected_draft` 统一走 ant-design message.error，中文提示

---

## 2. P2.3 训练门禁（Pre 2 道 + Post 2 道 + 审计）

> 原改造计划 P2.3：硬接入训练质量门槛。核心设计：门禁失败不阻断模型落库，只改 status+rejection_reason+审计（fail-closed for quality, fail-open for ops）。

### 2.1 阈值来源（两级覆写）

1. `app/services/factors/__facade__.py:DEFAULT_GATE_CONFIG`（硬编码默认值，§4.2）
2. `FactorSystemConfig(id=1).extra_json["gates"]`：`{"coverage_threshold":0.75,...}` 会覆盖同名键；非法数值自动过滤；行不存在走 #1

### 2.2 P2.3a 前置门禁（2 道）

**插入位置**：[ridge_model.py:L615-L675](file:///d:/ai_project/dataAanlystNew/app/services/factors/ridge_model.py#L615-L675)，在 `_load_samples` 之前。

1. **覆盖率门禁 `gate_factor_coverage`**：
   - `lookback_days` 内每个 factor_codes 成员的 `factor_quality_daily.coverage` 平均值 ≥ `coverage_threshold`（默认 0.70）
   - 空列表 `passed=False` + reason=`empty_factor_list`（空集合无法训练，显式失败）
   - 因子完全无观测：fail-soft 降级为 `passed=True`（避免质量快照刚迁移无数据时整个训练链路瘫痪）
2. **IC 合理性 `gate_factor_ic`**：
   - `|validation_ic| ∈ [ic_min=0.01, ic_max=0.10]`（<0.01 像噪声 / >0.10 疑似未来函数/过拟合陷阱）
   - 因子无观测同样 fail-soft
3. **聚合入口 `run_training_eligibility_gates`**：可接受 `factor_codes / factorset_id / model_run_id` 三种传参，恒返回长度 2 的列表

**`has_hard_pre_fail` 分支（line 645）**：只要任一 gate 的 `passed=False`，就跳过 Ridge 拟合，把所有结果变量（`sample_count=0`、`coefficients={}`、`validation_ic=None`、`final_intercept=None` 等）初始化空值。**仍然创建 FactorModelRun**（`status=rejected`，rejection_reason 带 `[P2-G Pre] ...`），让用户在「因子模型」页看到训练失败原因而非「没反应」。

### 2.3 P2.3b 后置门禁（2 道 + 审计）

**插入位置**：[ridge_model.py:L809-L911](file:///d:/ai_project/dataAanlystNew/app/services/factors/ridge_model.py#L809-L911)，在 `evaluate_model_gate()` 之后、`status = ...` 之前。

修复的 P2.3a 遗留 bug：
```python
# line 814 — pre_gate_reasons 之前只用来判断 has_hard_pre_fail，从未写入最终 rejection_reasons
rejection_reasons = list(pre_gate_reasons) + list(rejection_reasons)
```

1. **后置① min_sample_count（治理硬门槛，叠加 legacy 的 500）**
   - 阈值：`gate_cfg["min_sample_count"]`（默认 10000）
   - 失败 → `[P2-G Post] sample_count:N<10000(治理硬门槛)`
2. **后置② 与 active 模型 IC 相对差**
   - 先取 `FactorRuntimeState(id=1).active_model_run_id` → 读 `FactorModelRun.metrics_json["validation_ic"]` 作为基准
   - 新模型 ic 来自 evaluate_model_gate 已计算好的 `validation_ic`
   - 公式：`rel_delta = |new_ic - active_ic| / max(|active_ic|, 1e-15)`
   - 阈值：`gate_cfg["max_active_ic_delta_pct"]`（默认 0.50 = 50%）
   - 失败 → `[P2-G Post] ic_delta:67.20%>50% (new_ic=0.0345, active_ic=0.0206)`
   - 异常：DB 读失败 / active_ic 为 0 / None，降级为 `active_ic_compare_warn`，不阻断

3. **审计写入（best-effort）**：任一 `pre_gate_reasons` 或 `post_gate_reasons` 非空，写 `data_governance_audit_events`：
   - `action = DATA_QUALITY_QUARANTINE`（在 `ck_dg_audit_action_values` 允许列表内）
   - `business_key = run_id`，`operator_id = ridge_train`
   - `before_json`：`pre_gate_reasons + legacy gate.minimum_samples/minimum_validation_ic`
   - `after_json`：`post_gate_reasons + final_status + sample_count + new_validation_ic + active_validation_ic + p2_gate_cfg`
   - `attributes_json`：`gate_stage=pre+post + model_run_id + factor_set_id + target_code`
   - `note`：中文提示"P2-G 训练门禁失败，模型标记为 rejected" / "…存在告警"
   - 写入异常：捕获所有 Exception 打 warning，**绝不抛异常**，保证门禁判定与模型记录不被审计链路故障拖垮（best-effort）

---

## 3. 关键设计决策（风险控制）

| # | 决策 | 理由 |
|---|---|---|
| 1 | 门禁失败仍创建 FactorModelRun（rejected）而非静默跳过 | 避免 UI 点击训练无响应；用户能看到 rejection_reason 调参 |
| 2 | 治理表 snapshot/member 写入用 SAVEPOINT `db.begin_nested()` 隔离 | 治理写入 FK 故障不回滚模型主记录（模型记录 ≥ 治理可读性） |
| 3 | 审计写入 best-effort | 审计表出故障 / 缺列时不阻断训练决策 |
| 4 | `min_sample_count` 与 legacy `minimum_samples` 双重叠加 | 不破坏 WP7-04 基础门禁；在其之上加 P2 治理硬门槛 |
| 5 | active 模型 IC 对比使用 **相对差** 而非绝对差 | IC 接近 0 时绝对差无意义；相对差更稳定反映模型稳定性 |
| 6 | `active_ic≈0` 时 IC 对比自动跳过 | 防 div0；active_ic<1e-15 视为基线不存在 |
| 7 | 草稿 promote 失败 → 状态回滚到 submitted（不是留在 approved） | approved 是"等待被应用"中间态，失败不回滚会导致后续 promote 无法重跑 |
| 8 | 非法跃迁 raise ValueError（不是返回错误 DTO） | 防止上层误吞错状态；HTTP 路由统一转 422 中文信息 |
| 9 | 前置 `coverage / ic` 门禁观测缺失时 passed=True（fail-soft） | 迁移期 `factor_quality_daily` 可能还没回填；否则整个训练不可用 |

---

## 4. 配置项 & 数据模型

### 4.1 数据模型变更（init_db auto-align 已自动补齐）

- `custom_indicators` 新增 4 列 + 3 索引：
  - `approval_status VARCHAR(24) NOT NULL DEFAULT 'pending'` — pending/submitted/approved/rejected/promote_failed
  - `approval_review_msg TEXT NULLABLE` — 驳回 / promote 失败原因
  - `promoted_factor_code VARCHAR(64) NULL` — promote 成功后因子代码回写（含 FK 到 factors.code 的语义外键，未开物理 FK 避免 factors 删除阻塞 CI）
  - `factor_draft_no VARCHAR(64) NULL` — 跳转到 /factor-center/drafts/:draft_no
  - 索引：`ix_custom_indicators_promoted_factor_code / approval_status / factor_draft_no`
- `factor_system_config.extra_json TEXT NOT NULL DEFAULT '{}'` — 放 `{"gates":{...}}`（§4.2）
- 复用表（P2.1 已建，本次纯写入）：
  - `factor_set_snapshots` + `factor_model_members`（ridge_model.py SAVEPOINT 写入）
  - `factor_drafts`（submit/approve/reject）
  - `data_governance_audit_events`（DATA_QUALITY_QUARANTINE）

### 4.2 DEFAULT_GATE_CONFIG（__facade__.py:1908-1915）

```python
DEFAULT_GATE_CONFIG = {
    "coverage_threshold":      0.70,  # 单因子 30 日覆盖 ≥70%
    "ic_min":                  0.01,  # |IC| ≥0.01（排除噪声信号）
    "ic_max":                  0.10,  # |IC| ≤0.10（防过拟合 / 未来函数）
    "min_sample_count":        10000, # Post 治理硬门槛（叠加 legacy minimum_samples=500）
    "max_active_ic_delta_pct": 0.50,  # Post 新旧模型 IC 相对差 ≤±50%
    "lookback_days":           30,    # 质量观测回看窗口
}
```

**覆写方式**：`UPDATE factor_system_config SET extra_json = '{"gates":{"min_sample_count":20000,"max_active_ic_delta_pct":0.30}}' WHERE id=1;`（key 缺失走默认；非法数值被 `float(v)` 失败时忽略）。

---

## 5. API 变更（HTTP 路由 scoring_facade.py）

| 方法 | 路径 | 说明 | 主要返回/副作用 |
|---|---|---|---|
| GET  | `/api/v1/scoring/drafts?status=&source_module=&limit=` | 草稿列表 | `list[FactorDraftDTO]` |
| GET  | `/api/v1/scoring/drafts/{draft_no}` | 草稿详情（含 payload + review_msg） | dict；404 不存在 |
| POST | `/api/v1/scoring/drafts/{draft_no}/approve` | 审批通过（可促发 promote） | `body = DTO`；非法跃迁 → 422 |
| POST | `/api/v1/scoring/drafts/{draft_no}/reject` | 审批驳回（`review_msg` 必填） | `body = DTO` |
| POST | `/api/v1/scoring/external-draft/submit` | 外部提交草稿（promote 前唯一合法入口） | 202：submitted；422：precheck rejected → 错误中间件封装 extras 含 `draft_id/review_status` |
| POST | `/api/v1/scoring/gates/training-eligibility` | 聚合执行 2 道前置门禁（factor_codes / factorset_id / model_run_id 三选一） | `list[dict]` 长度 2，每项含 gate_name/passed/reasons/thresholds |

> P2.3 Post 门禁**无单独 HTTP 路由**：它在训练链路中自动执行，其结果可在 `get_model_detail(model_id).rejection_reason` + 审计事件中回溯。若后续 UI 需模型门禁重跑，可复用 `run_training_eligibility_gates(model_run_id=...)` 入口。

---

## 6. Facade 层受影响函数签名（供非因子域调用）

> 外部模块应**只** `from app.services.factors.__facade__ import <function>`，严禁 ORM 直连。

```python
# P2.2 草稿
submit_factor_draft_from_external(*, source_module: str,
                                  source_ref_id: int | str,
                                  payload: dict[str, Any],
                                  actor: str = "external:untrusted",
                                  ) -> ExternalFactorDraftSubmission
approve_factor_draft(draft_no: str, reviewer: str, review_msg: str | None = None) -> FactorDraftDTO
reject_factor_draft(draft_no: str, reviewer: str, review_msg: str) -> FactorDraftDTO
list_factor_drafts(*, status="any", source_module=None, limit=100) -> list[FactorDraftDTO]
get_factor_draft(draft_no: str) -> FactorDraftDTO | None

# P2.3 门禁
run_training_eligibility_gates(*, factor_codes=None, factorset_id=None, model_run_id=None,
    coverage_threshold=0.70, ic_min=0.01, ic_max=0.10, lookback_days=30) -> list[GovernanceGateResultDTO]
gate_factor_coverage(factor_codes, *, threshold=0.70, lookback_days=30) -> GovernanceGateResultDTO
gate_factor_ic(factor_codes, *, min_ic=0.01, max_ic=0.10, lookback_days=30) -> GovernanceGateResultDTO
_load_gate_config_from_db(*, db_supplied=None) -> dict[str, float]  # 调试/配置面板用
```

---

## 7. 跨域边界检查（防耦合回退）

硬约束：**非因子域只从 `__facade__.py` 导入、只调 `/api/v1/scoring/*`**；因子域绝不反向 import custom_indicators / alerts 模块。

本次变更前已验证的跨域替换点（仍保持不变）：
- TodayDecision / PortfolioStrategyRules / TaskCenter → 使用 `GET /scoring/overview` 与 `/scoring/active-scope`
- alerts.py / candidate_promote.py → 使用 `submit_factor_draft_from_external(source_module="custom_indicators", ...)` 而非直调 promote

P2.2 + P2.3 未引入任何反向 import，仅增加：
- ridge_model.py → `from app.services.data_governance_audit import write_audit_event`（属于基础设施审计模块，不是业务域，允许）

---

## 8. 验证：31/31 冒烟用例清单

脚本：`tmp/_p22_p23_smoke.py`，执行 `python tmp\_p22_p23_smoke.py`。

### §[0] P0/P1 回归（7/7）
1. `list_score_models(scope="any", limit=5)` 返回 list
2. `DEFAULT_GATE_CONFIG` 包含 `coverage_threshold/ic_min/ic_max/min_sample_count/max_active_ic_delta_pct` 5 键
3. `_load_gate_config_from_db` 默认值范围合理（0.70、0.01<0.10、样本≥100、IC 差≤2）
4. `list_factorsets(limit=5)` 返回 list
5. `get_factor_usage("NO_SUCH_FACTOR_XYZ")` 返回 None（缺码降级）
6. `list_factor_drafts(limit=3)` 返回 `list[FactorDraftDTO]`
7. `get_model_detail(<已存在run_id>)` DTO 有 `id + factors` 字段

### §[1] P2.2 CustomIndicators 全流程（10/10）
8. 创建 custom_indicator 成功（正确字段：key/formula/value_type/promoted_factor_code/...，无 factor_code/created_by 伪字段）
9. `submit_factor_draft_from_external` 返回 `review_status=submitted` + `draft_id`
10. CI 行 `approval_status=submitted` + `factor_draft_no=<draft_id>` 回写
11. `get_factor_draft(draft_id)` 返回 `status=submitted` + payload dict
12. `approve` 流程：终止态 rejected 自动跳过不报错；若 submitted 则 review_status∈{applied, submitted, approved}
13. CI 审批后 approval_status 在允许集内
14. `reject` 已处于终止态 → 幂等 ok（或抛 ValueError 非法跃迁，均视为通过）
15. 第二份草稿（非 numeric source_ref_id）→ `review_status=rejected_draft`
16. 已在 rejected/terminal → skip explicit reject
17. review_msg 字段已写入（manual reject smoke 或 precheck reason）

### §[2] P2.3a 训练 PRE-GATES（3/3）
18. `gate_factor_coverage([])` → `passed=False` + `empty_factor_list` 原因
19. `gate_factor_ic(["NO_SUCH_FACTOR_CODE_ZZZ"])` → 不 crash，reasons 为 list
20. `run_training_eligibility_gates` 返回长度 2，每项含 `passed`

### §[3] P2.3a/b 集成（7/7）
21. `DATA_QUALITY_QUARANTINE` 初始审计计数可读
22. 使用 DB 中现有 FactorSet（`fs-set-3126b70f`）做训练
23. `train_rolling_ridge` 返回真实 `run_id + status=rejected`
24. 7 条 rejection_reasons 含 `[P2-G Pre]` / `[P2-G Post]` 前缀
25. `status=rejected` ↔ reasons_len>0 一致性
26. `FactorModelRun` 持久化后 `rejection_reason` 含 P2-G 标记
27. 审计事件 +1（`DATA_QUALITY_QUARANTINE` 动作）

### §[4] HTTP 端到端（4/4）
28. `GET /scoring/drafts?limit=2` → 200 + `list[dict]`
29. `POST /scoring/gates/training-eligibility` → 200 + 长度 2 列表
30. `POST /scoring/external-draft/submit` → 422 + `extras.draft_id` 非空（rejected_draft 经全局错误中间件封装）
31. `GET /scoring/factorsets?limit=1` → 200

---

## 9. 后续可优化 Backlog（非必须，按 ROI 排）

- P1.2.2 模型版本对比：把 rejection_reason 中 `[P2-G Pre] / [P2-G Post]` 分色显示，与 legacy reasons 视觉区分
- P2.4 factor_quality_daily 每日回填任务：为 coverage/IC 门禁提供真实观测数据，避免 fail-soft 降级
- P2.5 FactorSystemConfig 管理面板：前端直接改 gates 阈值（默认值热更新，不重启）
- P2.6 审计事件 Tab 过滤：新增 `MODEL_TRAINING_GATE` action 专门展示 P2 门禁失败（当前复用 `DATA_QUALITY_QUARANTINE`，可通过 `attributes.gate_stage` 筛选）
