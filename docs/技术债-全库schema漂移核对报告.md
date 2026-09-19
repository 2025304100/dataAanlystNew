# 技术债 · 全库 schema 漂移核对报告（TD5，48/127 起底）

> 生成：2026-09-18 · 工具：`verify_schema_drift.py` v2（TD5 扩展）
> 范围：MySQL `127.0.0.1:3306/gpfx` 全库 127 张 ORM 表 · 方法：库实际结构 ↔ ORM 声明比对（不启动应用、不跑 auto-align）
> 证据链：`evidence/schema_drift_audit_2026-09.json`（DoD① exit 0，46KB）· `_td1_drift_baseline.txt`（TD1 基线 48/127）· `_td5_default_run.txt`（DoD② 默认模式全输出）· `_td5_selftest2.txt`（防假绿自检）
> 边界（not_do）：本卡只出报告与工具扩展；**逐条修复另立任务卡，不动线上表结构**。

---

## 1. 结论速览

| 指标 | 值 |
|---|---|
| 漂移表 | **49 / 127**（TD1 基线 48，详见 §2 diff） |
| 完全一致 | 78 张 |
| 覆盖类别 | 列 / 索引 / 唯一约束 / 外键 / **引擎** / **字符集** / **检查约束**（共 7 类，后 3 类为本次新增） |

| 漂移类别（by_class） | 表数 | 项数 | 分类去向 |
|---|---|---|---|
| 缺外键 missing_fk | 18 | 30 个 FK | A1（P1） |
| 外键不符 fk_mismatch | 12 | 12 项 | A2（2 项 P1）+ B3（10 项 P3） |
| 缺唯一约束 missing_uq | 1 | 1 | A3（P2） |
| 缺检查约束 missing_check | 11 | 19 个 CHECK | A4（P2） |
| ORM 多列 col_orm_only | 1 | 1 列 | A5（P1） |
| 多索引 extra_idx | 26 | 47 个索引 | B1（P3） |
| 库多列 col_db_only | 10 | 28 列 | B2（P3）/ C2 / C3 |
| 引擎不符 engine | **0** | 0 | —— 全库 127/127 InnoDB ✅ |
| 缺索引 missing_idx / 多外键 extra_fk / 缺表 / 缺模型 | 0 | 0 | —— |

**三大系统性发现**（本次新覆盖抓到）：

1. **CHECK 约束全库裸奔**：11 张表 19 个 ORM 声明的 CHECK 约束在库里无一落实（MySQL 8.0.16+ 才实施 CHECK；建表/auto-align 时都没带上）。枚举与取值范围只剩应用层防线。
2. **引擎债务已清零**：127 张表全部 InnoDB——T01（MyISAM 14 表）/T07（转引擎后 FK 永久丢失）两轮修复的成果经受住了全库复核。
3. **全库字符集是 utf8mb3，不是 utf8mb4**：127/127 张表 `utf8_general_ci`，库默认 `utf8`，而连接串用 `charset=utf8mb4`。4 字节字符（emoji、部分生僻字/金融符号）写入会报 `Incorrect string value`。当前无表级告警（因为全库一致地"错"），属 C1 待裁决大迁移。

---

## 2. 与 TD1 基线的 diff（49 vs 48）

| 变化 | 表 | 原因 |
|---|---|---|
| **新增 2** | `governance_events_outbox`、`task_idempotencies` | 本次新增的 CHECK 检查类抓到（此前工具不查这类，缺陷一直隐形——印证"漂移检查漏掉哪类，那类缺陷就隐形"） |
| **消失 1** | `factor_audit_logs` | 其 `model_run_id` 外键已在库中落实（TD1 起底后 T26/0058 迁移成果），复核通过 |

其余 47 张表漂移类别与基线一致，工具四类既有检查输出稳定。

---

## 3. A 类 · 真实缺陷（建议修复，另立任务卡）

### A1 缺外键 —— 18 表 30 个（P1）
历史 MyISAM 静默吞 FK 的存量债。当前无库级引用完整性，孤儿行风险已现实存在；**加 FK 前必须先清孤儿数据（1452），否则迁移必炸**。

| 表 | 缺失外键 |
|---|---|
| sim_orders | member_id, portfolio_id, symbol_id |
| sim_trades | order_id, portfolio_id, symbol_id |
| trade_setups | portfolio_id, scan_run_id, score_id, symbol_id |
| scan_runs | portfolio_id, portfolio_rule_id, preset_id |
| watchlist_items | symbol_id, target_portfolio_id, watchlist_id |
| factor_values | factor_id, symbol_id |
| positions | portfolio_id, symbol_id |
| scan_results | scan_run_id, symbol_id |
| backtest_trades | decision_evidence_id, exit_evidence_id |
| factors | active_version_id, shadow_version_id |
| backtest_runs | strategy_snapshot_id |
| cash_ledger / custom_indicator_versions / daily_bars / portfolio_rules / signal_rules / scores / idempotency_records | 各 1 个（portfolio_id / indicator_id / symbol_id / portfolio_id / portfolio_id / symbol_id / portfolio_id） |

### A2 外键行为反转 —— 2 项（P1）
`portfolio_candidates`：库实际是 **CASCADE**，ORM 要 **RESTRICT**——删 portfolio/symbol 会**级联删掉候选记录**而非拒绝，与声明意图相反，是 12 项 FK 不符中唯一的行为级差异（数据丢失风险）。

### A3 缺唯一约束 —— 1 项（P2）
`alert_events.uq_alert_events_dedupe_incident` 未落实 → 告警去重防线失效，同 incident 可重复入库。

### A4 缺检查约束 —— 11 表 19 个（P2；若应用层校验完备可降 P3）
| 表 | 缺失 CHECK |
|---|---|
| portfolios | ck_portfolios_status_model_values, ck_portfolios_status_score_values, ck_portfolios_status_reconciliation_values, ck_portfolios_status_data_values |
| portfolio_cron_schedules | ck_portfolio_cron_sched_hour, ck_portfolio_cron_sched_minute, ck_portfolio_cron_sched_type |
| alert_events | ck_alert_events_status, ck_alert_events_severity_level |
| decision_runs | ck_decision_runs_run_type_3values, ck_decision_runs_status_6values |
| governance_events_outbox | ck_gov_outbox_retry_count_range, ck_gov_outbox_status_values |
| decision_evidence / manual_price_overrides / outbox_events / portfolio_factor_usage / data_governance_audit_events / task_idempotencies | 各 1 个（action 6 值 / resolved_mode 3 值 / status 值 / binding_status 值 / dg action 值 / task_type 4 值） |

### A5 ORM/库主键错位 —— 1 项（P1）
`idempotency_records`：ORM 声明自增 `id` 主键（WP0-2 契约），**库里没有该列**（DB 仍以 idempotency_key 唯一键形态存在）。任何走 ORM 的 INSERT 都会撞 `Unknown column 'id'`；现网未炸说明写入走的是裸 SQL/旧路径——需核实写入路径后，补迁移加列或修正模型。

---

## 4. B 类 · 声明过期 / 无害冗余（改迁移或登记豁免）

### B1 多索引 —— 26 表 47 个（P3）
冗余索引拖慢写入、占空间。处置原则：**先 EXPLAIN 确认无查询依赖，再 DROP**；不可贸然清。
- **典型冗余**：`factors`（`code` + `ix_factors_code` 同列两遍）；`backtest_rule_templates/portfolios/scan_presets/watchlists` 的 `name`；`factor_drafts.draft_no`；`discovery_candidates.universe_symbol_id`；`factor_set_members / factor_transition_audits / scan_runs / journal_entries.trade_setup_id` 的裸列索引
- **ORM 历史声明残留**（ix_* 命名但现 ORM 未声明）：async_tasks、backtest_runs（3 个）、data_governance_audit_events、decision_evidence（4 个）、decision_runs（3 个）、idempotency_records（2 个）、notification_templates、portfolio_cron_schedules（2 个）、portfolio_members（3 个）、scores（4 个）、sim_orders（4 个）、strategy_execution_snapshots（3 个）、trade_setups、watchlist_items（4 个）、factor_weight_snapshots
- **命名漂移（无害）**：`uq_decision_evidence_idempotency_key`——ORM 的 UQ 以不同名落实，唯一性本身已成立

### B2 库多列 —— 10 表 28 列（P3，按证据三档）
| 档 | 列 | 依据 |
|---|---|---|
| **有意保留（豁免）** | idempotency_records.result_json | 模型 docstring 明说"同表两列共存，老代码不破坏"（response_json 为新契约列，result_json 保数据）；portfolios.portfolio_status | `portfolio_state_machine.py` 以裸 SQL 探测+读写该列（运行时自加列模式） |
| **无引用残留（可清理）** | factor_evaluation_runs.test_start_date/test_end_date；notification_deliveries 5 列（billing_units/delivery_trace_id/provider_response_json/recipient_info_json/retry_of_delivery_id）；notification_templates 6 列（content_footer_template/default_channel_ids_json/icon_key/jump_url_template/language_code/template_category）；portfolio_members 2 列（deviation_pct/target_weight_pct）；positions.target_weight_pct；sim_orders.sim_account_id | app/ 全域 grep 零引用 |
| **低置信残留（待裁决）** | portfolio_rules 4 列（factor_weights_json/rebalance_frequency/universe_key/weighting_method） | rebalance_frequency 在别表有活引用，本表引用未见 |

### B3 FK RESTRICT vs NO ACTION —— 10 项（P3）
MySQL 中 `RESTRICT` 与 `NO ACTION` **语义等价**（都是立即拒绝，无延迟检查）。backtest_filter_events、decision_evidence、decision_order_plans、decision_runs、manual_price_overrides、outbox_events、portfolio_cron_schedules、portfolio_factor_usage、portfolio_factor_usages、security_status_daily、strategy_execution_snapshots 的此类"不符"属纯声明漂移：改迁移对齐字面量，或登记豁免。**注意与 A2（CASCADE）严格区分。**

---

## 5. C 类 · 无法判定（列证据待裁决）

| # | 事项 | 证据 | 裁决点 |
|---|---|---|---|
| C1 | **全库字符集 utf8mb3** | 127/127 表 `utf8_general_ci`；库默认 `utf8`；连接串 `charset=utf8mb4` | ✅ **已裁决并落地（TD6）**：豁免大迁移 + 全局 4 字节字符写入守卫（`app/core/text_charset.py` 挂 `Session.before_flush`，只扫本次变更的字符串列新值，报错带 `类名.字段名`+坏字符码点）；未来需要 4 字节存储时再立迁移卡（先摘守卫） |
| C2 | **strategy_execution_snapshots 模型分裂** | DB 有 content_hash/snapshot_json/usage_binding_id 3 列；新模型文件 `strategy_execution_snapshot.py` 被 `app/models/__init__.py:95` **显式排除注册**；而 `portfolio_factor_usage.py` 正在写 usage_binding_id | ✅ **已裁决并落地（TD6）**：权威声明统一到 decision_engine 旧类并照 DB 实态补 3 列（usage_binding_id int 可空**无 FK**；snapshot_json/content_hash NOT NULL + Python default "{}"/""）+ 3 独立索引；新模型文件壳化为 re-export；迁移链缺口补幂等防御式修订 `wps_0023_056`（线上三列已在=no-op，scratch sqlite 全链 upgrade + downgrade -1 双向验证）。**遗留 P1 挂账**：service id 契约缺口（save_and_apply 不生成 id + `int(snap.id)` 与 varchar PK 冲突，生产进程必 TypeError），另立卡 |
| C3 | idempotency_records.resource_type / status 库多列 | 模型 docstring 只豁免了 result_json，这两列无说明 | 保留/清理？ |
| C4 | portfolio_rules 4 列 | 见 B2 低置信档 | 同 B2 |

---

## 6. 工具扩展与防假绿验证（DoD 证据）

**`verify_schema_drift.py` v2 改动**：
- 核心循环抽成 `check_table()`（main 与 selftest 共用同一套比对逻辑，杜绝"自检走另一条路"的假绿）
- 新增 3 类覆盖：引擎（ORM 显式声明则精确比对，未声明则兜底必须 InnoDB）、字符集/排序规则（ORM 声明则硬比对；未声明只做 warning 级——库默认本身非 utf8mb4 时不至于 127 表全误报）、CHECK（约束名 + 归一化 SQL 文本双轨比对，规避 MySQL 自动命名）
- 新增 `--json`（结构化报告：summary/drift_tables/warn_rows/table_options 全库引擎字符集明细；**报告成功产出即 exit 0**，漂移详情看 JSON，便于 DoD 管道）与 `--selftest`（内存 sqlite 手工建"已知缺陷表"回灌 + 表选项纯函数 7 用例，**不连任何真实库**）
- 附带修正：列差异区分"库多/ORM 多"两个方向；TextClause `__bool__` 陷阱；`[OK]` 分支 NameError（`--json` 路径盲区，由 DoD② 默认模式首跑抓出——两条路径都要跑的价值实证）

**防假绿三验证（pitfall 铁律执行）**：
1. `--selftest`：15 用例全过——7 类覆盖全部"能报出"（缺 FK/缺 UQ/缺 CHECK/多索引/缺索引/列双向差异/FK ondelete 不符/MyISAM/字符集/排序规则），正常样本零误报
2. DoD①：`--json` exit 0，evidence 覆盖 7 类，127 表 table_options 全量在案
3. DoD②：默认模式 49 DRIFT 与 JSON 名单**逐一一致**；TD1 已知缺陷样本（alert_events/backtest_runs/backtest_trades/watchlist_items/portfolio_candidates/idempotency_records/sim_orders/trade_setups）全部仍被报出

---

## 7. 处置建议汇总（供排卡参考，本卡不动库）

| 建议卡 | 内容 | 优先级 | 前置 |
|---|---|---|---|
| FK 补齐批 | A1 30 个 + A2 CASCADE→RESTRICT + A3 唯一约束 + A5 id 列；先孤儿清查（1452）再迁移 | P1 | 孤儿数据清查脚本 |
| CHECK 补齐批 | A4 19 个 ADD CONSTRAINT（MySQL 8.0.16+ 已支持） | P2 | 确认各表应用层校验现状 |
| 索引/列清理批 | B1 冗余索引 + B2 无引用残留列；先 EXPLAIN/引用面复核再 DROP | P3 | 每条逐一确认 |
| 声明对齐批 | B3 10 项 RESTRICT 字面量对齐（或豁免登记） | P3 | 可与 FK 批合并 |
| utf8mb4 可研 | C1 全库字符集迁移评估 | 已裁决暂缓（TD6：豁免+写入守卫） | —— |
| ~~模型分裂裁决~~ → service id 契约缺口 | C2 已裁决落地（TD6，见 §5）；P1 缺口已修复（TD7：`save_and_apply_usage_atomic` snapshot id 服务端生成 + snapshot_id 契约 int→str + 行为级测试 3 用例）；**双轨整合**（现役 factor_usage_service vs atomic 原型、单/复数两套 usage 表）另行排期 | P3 排期 | —— |

---

## 8. TD6 裁决落地记录（2026-09-18）

C1/C2 两项裁决已由 TD6 卡执行完毕，此处留档关键结论：

- **C2 真相定位**：三列「无 alembic add_column 记录」的历史成因 = 线上靠历史进程 `_auto_align_all_schema` 自动补列，迁移链（0026 显式 `op.create_table`）产物一直是旧结构。TD6 把 ORM 修对之后，该缺口在「纯迁移链」环境（G1 契约测试 fixture `tmp_alembic_db`、CI 新装 sqlite）显性化：TD6 修复前回归 7 红（`no such column: usage_binding_id` ×4、`NOT NULL constraint failed: snapshot_json` 等）。**修复前测试全绿是「ORM 与迁移链双错一致」的假绿**——service 新契约代码属于未跟踪新文件，只在 import 过三列声明的进程里才不炸。
- **落地动作**：①权威类补 3 列+3 索引+Python default；②壳文件 re-export 化（消除 extend_existing 偶然合并的假绿机制）；③补迁移修订 `wps_0023_056`（MySQL 三段式加列：nullable → 回填 → MODIFY NOT NULL；SQLite 直接 `NOT NULL DEFAULT ''`；幂等防御式，线上=零操作）；④C1 守卫 `text_charset.py` + 12 用例。
- **验收**：DoD① 该表 drift 清零（仅剩 B3 既有 RESTRICT≡NO ACTION 等价声明漂移）；DoD② 守卫测试 12/12；DoD③ 回归 **31 passed**（修复前 7 failed/24 passed，基线为 g1 单文件 10 passed）；迁移修订双向验证（upgrade head 30 列+3 索引 / downgrade -1 干净回退）。

### TD7 追记（2026-09-18，P1 缺口修复 + 定性修正）

- **原挂账定性修正**：TD6 挂账「service save_and_apply 无 id 生成 + `int(snap.id)` vs varchar PK → 生产必 TypeError」**不成立**——生产路由与 G1 契约测试走的是现役 `factor_usage_service.py`（snapshot id 由 `content_hash("ses", ...)` 确定性生成，健康）。真实问题：`portfolio_factor_usage.py`（WP02「原子 7 步」TDD 骨架实现，app/ 零引用）的 DB 路径从未被执行——原 `test_wp02_factor_usage_and_fsm_tdd.py` 只有签名级验证；且 `factor_set_id` int 强转对应 FK 目标 `factor_sets.id` 为 String PK、snapshot 构造缺 4 个 NOT NULL 列，FK 开启的库上全路径必炸。
- **TD7 修复**：①snapshot id 服务端生成（`content_hash("ses", snap_hash, correlation_id)`，现役同款）并补齐 decision_clock_json/member_snapshot_json/snapshot_hash/effective_from 四个 NOT NULL 列；②snapshot_id 契约 int→str 三处联动（dataclass 字段/幂等重放/outbox payload）；③新增行为级测试 3 用例（正常路径两表落数+outbox 契约 / 幂等重放同 snapshot_id / 注入失败全回滚无副作用），`test_wp02_factor_usage_and_fsm_tdd.py` 17→**20 passed**；防波及回归 g1+backtest_detail **11 passed**。
- **遗留（P3 排期）**：双轨整合——`portfolio_factor_usage.atomic` 原型与现役 `factor_usage_service` 功能重叠；单数 `portfolio_factor_usage` 表（Integer id）与复数 `portfolio_factor_usages` 表（String id）两套并存，`factor_set_id` Integer→String PK 的 FK 亲和依赖（生产 MySQL 靠弱类型比较）是潜在数据一致性风险，需专项裁决统一。

> 复核方式：修复卡完成后重跑 `.venv/Scripts/python.exe .workbuddy/mining/verify_schema_drift.py`（逐表）与 `--json`（全量），漂移数应单调下降。
