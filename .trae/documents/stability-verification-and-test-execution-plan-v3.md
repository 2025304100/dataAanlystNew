# 稳定性验证与测试执行计划 v3

> 承接 v2 修复计划（`professional-stability-test-plan-v2.md`）。
> v2 的代码修复与测试创建已全部落地，本计划聚焦于 **验证修复有效性 + 执行 4 维度测试 + 缺陷修复 + 最终验收**。

---

## 一、摘要

### 背景
用户反馈第三方接口（akshare）不稳定：接口探测多次无响应、机会挖掘全量同步多次卡在中间。v2 计划定位 3 个致命失效点并完成修复，同时新增 48 个测试用例。本计划是 v2 之后的"验证与验收"阶段。

### 当前状态（已通过代码探索确认）
| 项目 | 状态 | 证据 |
|------|------|------|
| universe refresh timeout 包装（120s） | ✅ 已落地 | `discovery_tasks.py` L72/L841/L898 |
| 探测端点独立线程池（max_workers=8） | ✅ 已落地 | `akshare_apis.py` L189 |
| `_fetch_history` 重试降至 1 次 | ✅ 已落地 | `market_data.py` L216 `range(1)` |
| watchdog 阶段感知心跳 | ✅ 已落地 | `discovery_tasks.py` L799 `task.stage != "prepare"` |
| 13 处日志增强 | ✅ 已落地 | `akshare_utils.py` L201 `logger.info`、`market_data.py` 源切换日志 |
| 4 个新白盒测试文件（27 用例） | ✅ 已创建 | `test_whitebox_universe_refresh.py`(7) + `test_whitebox_probe_thread_pool.py`(4) + `test_whitebox_data_correctness.py`(10) + `test_whitebox_interaction.py`(6) |
| 黑盒测试补强（+4 用例） | ✅ 已创建 | `test_blackbox_api_mgmt.py` 共 21 用例 |
| UAT 清单 v3 | ✅ 已存在 | `docs/uat-checklist.md` 约 100+ 项 |
| **运行测试验证无回归** | ⬜ **未执行** | 本计划核心 |
| **UAT 清单缺口核查** | ⬜ **未执行** | 本计划次要 |

### 本计划目标
1. 运行全量自动化测试（281 用例），验证 v2 修复未引入回归
2. 重点验证 48 个新增用例全部通过（证明修复有效）
3. 核查 UAT 清单是否覆盖 v2 计划要求的 25 稳定性核心 + 15 边界场景，补齐缺口
4. 执行黑盒测试验证真实后端探测/同步不再卡死
5. 修复测试中发现的问题（如有）

---

## 二、当前状态分析

### 2.1 测试文件清单（17 个文件，281 用例）

| 文件 | 用例数 | 类型 | v2 相关 |
|------|--------|------|---------|
| `test_whitebox_universe_refresh.py` | 7 | 白盒 | ✅ 新增 |
| `test_whitebox_probe_thread_pool.py` | 4 | 白盒 | ✅ 新增 |
| `test_whitebox_data_correctness.py` | 10 | 白盒 | ✅ 新增 |
| `test_whitebox_interaction.py` | 6 | 白盒 | ✅ 新增 |
| `test_blackbox_api_mgmt.py` | 21 | 黑盒 | ✅ 补强 +4 |
| `test_whitebox_discovery_timeout.py` | 4 | 白盒 | v1 |
| `test_whitebox_async_tasks.py` | 16 | 白盒 | v1 |
| `test_whitebox_akshare_http.py` | 28 | 白盒 | v1 |
| `test_whitebox_external_factors.py` | 31 | 白盒 | v1 |
| `test_whitebox_api_mgmt.py` | 42 | 白盒 | v1 |
| `test_whitebox_discovery_universe.py` | 29 | 白盒 | v1 |
| `test_whitebox_scoring_config.py` | 4 | 白盒 | v1 |
| `test_blackbox_api.py` | 26 | 黑盒 | v1 |
| `test_whitebox_discovery.py` | 19 | 白盒 | v1 |
| `test_whitebox_backtest_sandbox.py` | 14 | 白盒 | v1 |
| `test_whitebox_analysis.py` | 10 | 白盒 | v1 |
| `test_whitebox_allocation.py` | 10 | 白盒 | v1 |

### 2.2 UAT 清单现状
`docs/uat-checklist.md` 已包含：
- 章节 0：稳定性核心验证（约 15 项）
- 章节 1-5：功能验证（接口管理/机会挖掘/外部数据同步/评分配置/全局回归）
- 章节 6：边界场景（约 16 项）
- 章节 7：潜在 Bug 验证（约 6 项）

**待核查**：v2 计划要求"25 项稳定性核心 + 15 项边界"，当前章节 0 约 15 项 < 25 项，可能存在缺口。

### 2.3 风险点
1. **v2 修复未实际验证**：代码已改但未跑测试，可能存在语法错误、import 缺失、mock 不匹配等问题
2. **黑盒测试依赖后端运行**：`test_blackbox_api_mgmt.py` 需要后端在 `localhost:8000` 运行
3. **UAT 清单可能缺口**：稳定性核心项不足 25 项

---

## 三、提议的验证步骤

### 阶段 1：自动化测试执行（P0）

#### 3.1.1 白盒测试（无需后端运行）
**目标**：验证 48 个新用例 + 既有白盒用例无回归。

**执行命令**：
```bash
python -m pytest tests/ -m "not blackbox and not slow" -v --tb=short 2>&1 | tee pytest-whitebox-result.txt
```

**分批执行策略**（若全量执行超时或报错过多）：
1. 先跑 v2 新增 4 个文件：
   ```bash
   python -m pytest tests/test_whitebox_universe_refresh.py tests/test_whitebox_probe_thread_pool.py tests/test_whitebox_data_correctness.py tests/test_whitebox_interaction.py -v --tb=short
   ```
2. 再跑 v1 既有白盒文件，确认无回归

**验收标准**：
- 48 个新用例全部 PASS
- 既有用例无新增 FAIL（与 v2 修复前对比）

**失败处理流程**：
1. 收集失败用例的 `--tb=short` 输出
2. 分类：A) 测试代码错误（mock 不匹配、断言过严）→ 修测试；B) 业务代码 bug → 修业务代码
3. 修复后重跑该文件
4. 记录到"缺陷修复日志"

#### 3.1.2 黑盒测试（需后端运行）
**目标**：验证真实后端探测/同步不卡死。

**前置条件**：
- 后端服务运行在 `http://localhost:8000`
- 数据库可连接
- 网络可访问 akshare 数据源

**执行命令**：
```bash
python -m pytest tests/test_blackbox_api_mgmt.py -v --tb=short 2>&1 | tee pytest-blackbox-result.txt
```

**重点关注用例**（v2 新增 4 个）：
- `test_probe_returns_within_30s_with_real_backend` — 真实探测 < 30s
- `test_probe_all_17_apis_complete_within_180s` — 17 接口串行 < 180s
- `test_consecutive_probes_do_not_degrade_response_time` — 连续探测不退化
- `test_discovery_tasks_list_returns_quickly` — 任务列表 < 5s

**验收标准**：
- 4 个新用例全部 PASS（证明 v2 修复在真实环境生效）
- 既有黑盒用例 PASS 率 ≥ 90%（允许网络波动导致的偶发失败）

### 阶段 2：UAT 清单缺口核查与补强（P1）

#### 3.2.1 核查 UAT 清单覆盖度
**操作**：读取 `docs/uat-checklist.md`，对照 v2 计划第五章 5.4.1 和 5.4.2 节的清单，逐项核对：

**v2 要求的 25 项稳定性核心**（核查清单）：
- [ ] 探测端点 30s 内返回（单接口）
- [ ] 探测端点 30s 内返回（17 接口串行）
- [ ] 连续探测不退化（5 次）
- [ ] 探测超时返回结构化错误（含 key/success/latency_ms/error）
- [ ] universe 刷新 120s timeout 生效
- [ ] universe 刷新超时返回 None 并记录 ERROR 日志
- [ ] 单 symbol 同步 90s timeout 生效
- [ ] 单 symbol 同步超时 failed_count += 1
- [ ] `_fetch_history` 3 源回退总耗时 < 90s
- [ ] watchdog prepare 阶段不更新 updated_at
- [ ] watchdog sync 阶段正常更新 updated_at
- [ ] watchdog 终态任务立即退出
- [ ] watchdog 异常记录 WARNING 日志（非静默吞没）
- [ ] `_expire_stale_tasks` 10min 阈值触发
- [ ] 子线程独立 Session（避免主子线程死锁）
- [ ] 探测独立线程池 max_workers=8
- [ ] 前端 staleWarning 2min 阈值显示警告
- [ ] 前端 staleWarning 30s 复检
- [ ] 前端 staleWarning 任务非 running 时消失
- [ ] 前端 handleProbeAll CONCURRENCY=3
- [ ] 前端 handleProbeAll 批量期间禁用所有单个按钮
- [ ] 前端 handleProbeAll 完成后恢复按钮
- [ ] 前端 handleProbeAll 汇总提示（OK/failed）
- [ ] 探测日志 L1-L4 输出（start/done/timeout/exc）
- [ ] universe 刷新日志输出（start/done/timeout）

**v2 要求的 15 项边界场景**（核查清单）：
- [ ] 探测未知 api_key → 404
- [ ] 探测已禁用接口 → 仍可探测（禁用不影响探测）
- [ ] PUT 配置无效 strategy → 400/422
- [ ] PUT 配置 custom min>max → 400/422
- [ ] PUT 配置 delay 超范围 → 422
- [ ] PUT 配置 delay 为负 → 422
- [ ] PUT 部分更新不影响其他字段
- [ ] universe 刷新返回 seen=0 → 任务中止
- [ ] 任务取消后 watchdog 不覆盖终态
- [ ] 任务暂停后 24h 内不清理
- [ ] 任务暂停后超 24h 清理
- [ ] Discovery cleanup 保留 watchlist/positions/scored
- [ ] 前端任务卡死 2min 显示警告 + 取消按钮
- [ ] 前端任务卡死 10min 提示自动中断
- [ ] 批量探测中途异常 → finally 恢复按钮状态

#### 3.2.2 补齐缺口
- 若核查发现缺失项 → 在 `docs/uat-checklist.md` 对应章节追加
- 追加格式：`- [ ] [编号] [描述]（验证步骤：...；预期：...）`
- 保持与现有清单风格一致

### 阶段 3：缺陷修复（P0/P1，按发现顺序）

#### 3.3.1 缺陷分级
- **P0 致命**：v2 修复无效（探测仍卡死、同步仍卡死）→ 立即修复
- **P1 严重**：测试用例失败但非核心功能 → 修复后重跑
- **P2 一般**：UAT 清单缺失项 → 补齐即可

#### 3.3.2 修复原则
- 修测试代码：放宽过严断言、修正 mock 路径、补全 import
- 修业务代码：遵循 v2 计划的修复方向，不引入新设计
- 每次修复后重跑相关文件，确认 PASS

### 阶段 4：最终验收（P1）

#### 3.4.1 自动化测试验收
**命令**：
```bash
python -m pytest tests/ -m "not slow" -v --tb=short 2>&1 | tee pytest-final-result.txt
```
**标准**：
- 白盒用例 PASS 率 100%
- 黑盒用例 PASS 率 ≥ 95%（允许网络偶发失败）
- 无 ERROR 级别日志

#### 3.4.2 4 维度验收对照
| 维度 | 验收方式 | 通过标准 |
|------|----------|----------|
| 1. 功能显示正常 | 白盒测试 + UAT 章节 1-5 | 所有显示相关用例 PASS |
| 2. 交互正常且合理 | `test_whitebox_interaction.py` + UAT 交互项 | 6 个交互用例 PASS + UAT 交互项全勾 |
| 3. 数据正确性 | `test_whitebox_data_correctness.py` + UAT 数据项 | 10 个数据用例 PASS + UAT 数据项全勾 |
| 4. UAT 阶段测试 | `docs/uat-checklist.md` 全量执行 | 稳定性核心 25 项 + 边界 15 项全勾 |

#### 3.4.3 输出验收报告
在 `.trae/documents/` 创建 `stability-verification-report-v3.md`，包含：
- 自动化测试结果摘要（总数/通过/失败/跳过）
- 黑盒测试结果摘要
- UAT 清单执行结果
- 4 维度验收对照表
- 发现的缺陷与修复记录
- 结论：是否通过验收

---

## 四、假设与决策

### 4.1 假设
1. v2 代码修复已正确落地（探索已确认，但未实际运行测试）
2. 后端服务可在本机启动（用于黑盒测试）
3. akshare 数据源网络可达（黑盒测试需要）
4. UAT 清单 v3 已基本完整，仅需补齐少量缺口

### 4.2 决策
1. **白盒测试优先**：先跑白盒（无需后端），快速发现代码问题
2. **黑盒测试需手动启动后端**：不自动启动，由执行者手动准备
3. **UAT 清单核查基于 v2 计划要求**：以 v2 计划第五章 5.4.1/5.4.2 为基准
4. **缺陷修复不引入新设计**：仅修复，不重构
5. **测试结果保存到文件**：`pytest-*-result.txt` 便于追溯

---

## 五、验证步骤（执行者指南）

### 步骤 1：运行白盒测试（v2 新增优先）
```bash
cd d:\ai_project\dataAanlystNew
python -m pytest tests/test_whitebox_universe_refresh.py tests/test_whitebox_probe_thread_pool.py tests/test_whitebox_data_correctness.py tests/test_whitebox_interaction.py -v --tb=short
```
- 若全 PASS → 进入步骤 2
- 若有 FAIL → 进入步骤 4（缺陷修复）

### 步骤 2：运行全量白盒测试（确认无回归）
```bash
python -m pytest tests/ -m "not blackbox and not slow" -v --tb=short 2>&1 | tee pytest-whitebox-result.txt
```
- 若 PASS 率 100% → 进入步骤 3
- 若有 FAIL → 进入步骤 4

### 步骤 3：启动后端 + 运行黑盒测试
```bash
# 终端 1：启动后端
cd d:\ai_project\dataAanlystNew
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 终端 2：运行黑盒测试
python -m pytest tests/test_blackbox_api_mgmt.py -v --tb=short 2>&1 | tee pytest-blackbox-result.txt
```
- 重点观察 4 个新用例是否 PASS
- 若有 FAIL → 进入步骤 4

### 步骤 4：缺陷修复（按需）
- 读取失败用例的 `--tb=short` 输出
- 分类修复（测试代码 / 业务代码）
- 修复后重跑相关文件
- 记录修复内容

### 步骤 5：UAT 清单缺口核查
- 读取 `docs/uat-checklist.md`
- 对照本计划第三章 3.2.1 的 25+15 项核查清单
- 补齐缺失项到 UAT 清单

### 步骤 6：输出验收报告
- 创建 `stability-verification-report-v3.md`
- 填写测试结果摘要、4 维度验收对照表、缺陷记录、结论

---

## 六、风险与回滚

### 6.1 风险
1. **v2 修复存在隐藏 bug**：测试可能发现修复未生效 → 按缺陷修复流程处理
2. **黑盒测试因网络波动失败**：akshare 数据源不稳定 → 允许 < 10% 偶发失败，重试即可
3. **UAT 清单缺口较大**：可能需补 10+ 项 → 一次性补齐

### 6.2 回滚方案
- 若 v2 修复引入严重回归 → 通过 git 回滚到 v2 修复前的 commit
- 若个别测试修复无效 → 标记为 `pytest.skip` 并记录到缺陷日志，不阻塞整体验收

---

## 七、完成定义（Definition of Done）

本计划完成的标志：
1. ✅ 281 个自动化用例执行完毕，白盒 PASS 率 100%，黑盒 PASS 率 ≥ 95%
2. ✅ 48 个 v2 新增用例全部 PASS
3. ✅ UAT 清单覆盖 25 稳定性核心 + 15 边界场景（缺口已补齐）
4. ✅ 4 维度验收对照表全部通过
5. ✅ `stability-verification-report-v3.md` 验收报告已输出
6. ✅ 发现的缺陷已修复或已记录（无未处理的 P0/P1 缺陷）
