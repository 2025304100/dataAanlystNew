> **2026-10-25 二次评审修复更新**：本文件保留最初 BUG→代码对照，不再作为“已全量闭环”的证明。新增边界、MySQL 5.7 真实并发/迁移/恢复修复见 [P0/P1修复台账](组合校验P0-P1修复台账-2026-10-25.md)，当前操作步骤见 [交接补充](组合校验隔离修复交接补充-2026-10-25.md)。本轮仅隔离验证，正式库未部署。

# 端到端验证修复对照文档（2026-10-24 ~ 10-25）

> 范围：组合交易全链路验证（三轮：首轮验证 + 修复轮 + 回测攻坚轮）发现的全部问题与其代码改动的一一对应关系。
> **最终状态：全部 17 个 BUG 中 16 个已修复闭环；BUG-14（baostock 服务端宕机）以替代数据源管道绕过，回测中心 e2e 已完成**。
> 所有改动均在工作区未提交；验证环境为副本库 `gpfx_validation`（正式库 `gpfx` 零改动）。
> 证据文件根目录：`tmp/validation-2026-10-24/`（api/ 为请求响应原文 JSON，logs/ 为日志与漂移报告）。
> 后续执行（入库/部署/时间线/Runbook）见 `docs/validation-followup-2026-10-25.md`。

## 改动总览

| # | 类型 | 文件 | 修复的问题 | 严重级 |
|---|------|------|-----------|--------|
| 1 | 后端 schema | `app/schemas/portfolio_member.py` | BUG-3 成员状态/来源/执行模式脏值入库 | P0 |
| 2 | 后端 路由 | `app/api/routes/portfolios.py`（两处） | BUG-3 执行模式白名单；BUG-8 重复成员 409 误导文案 | P0/P2 |
| 3 | 后端 schema | `app/schemas/portfolio.py` | BUG-6 组合 account_type 脏值入库 | P1 |
| 4 | 新迁移 | `alembic/versions/2026_10_24_0079_member_active_unique_mysql.py` | BUG-4 归档成员永远无法重新加入 | P1 |
| 5 | 新迁移 | `alembic/versions/2026_10_24_0080_orm_alignment_catchup.py` | BUG-10 纯迁移建库缺 24 表/186 列/99 索引 | P1 |
| 6 | 新迁移 | `alembic/versions/2026_10_24_0081_idempotency_records_pk_contract.py` | BUG-11 幂等表主键契约断裂（潜伏 P0） | P0(潜伏) |
| 7 | 后端 服务 | `app/services/sim_accounts.py` | BUG-9 手动买单绕过状态门禁 | P1 |
| 8 | 后端 入口 | `app/main.py`（1 行） | BUG-7 409 诊断字段为空串 | P2 |
| 9 | 前端 组件 | `frontend/.../PortfolioTradingShell.tsx` | BUG-5 徽章把"未加载"渲染成"0 项待处理" | P1 |
| 10 | 前端 测试 | `__tests__/GovernanceOverviewPanel.test.tsx`、`__tests__/PortfolioGovernanceTab.reconcile.test.tsx` | BUG-12 五个基线失败测试（组件重构后断言过期） | P1 |
| 11 | 后端 测试 | `tests/test_migration_alembic_chain.py` | BUG-13 迁移链降级测试与 append-only 设计冲突 | P1 |
| 12 | 后端 测试 | `tests/test_whitebox_investment_center.py` | （新增）BUG-9 的回归守护用例 ×2 | — |
| 13 | 后端 服务 | `app/services/security_status/baostock_source.py` | BUG-15 受限网络的 BAOSTOCK_PROXY 门控隧道 | P2(能力) |
| 14 | 后端 测试 | `tests/test_security_status_download.py` | BUG-16 时区边界 flake | P2 |
| 15 | 模型+迁移 | `app/models/security_status.py` + `2026_10_25_0082_security_status_datetime6.py` | **BUG-17 MySQL DATETIME 秒截断 → CSV 导入涨跌停约束永不挂接（回测中心 MySQL 从未跑通的深层根因）** | P0(潜伏) |
| 16 | 后端 测试 | `tests/test_security_status_mysql_precision.py` | （新增）BUG-17 回归 | — |

环境项（非代码缺陷）：BUG-14 baostock 10030 服务端宕机（已实证）→ 以第六节替代数据源管道完成回测 e2e；BAOSTOCK_PROXY 隧道留作恢复后的兜底。

---

## BUG-3 ｜ 成员参数无校验，脏状态直接写入 SCD2 成员表

- **现象（修复前）**：`POST /portfolios/1/members` 传 `status:"invalid_status"`、`execution_mode:"bogus"` 均返回 200 并落库（验证时实际造出 id=15 的脏成员）。证据：`api/step28_member_bad2.json`、`api/step33_bad_mode.json`。
- **动机**：`portfolio_members` 是 SCD2 有效成员关系表，下游按 `status in (active/paused/archived)`、`execution_mode in (manual/confirm/auto)` 过滤；脏值一旦入库，成员会被所有下游查询静默漏掉，且无法通过任何正常接口修正。schema 注释声称只允许这些取值，但实际**没有任何校验**。
- **改动**：
  - `app/schemas/portfolio_member.py`：新增 `MemberStatus` / `MemberSourceType` Literal 类型；`PortfolioMemberCreate.status`、`source_type` 与 `PortfolioMemberUpdate.status` 收紧为 Literal（非法值 FastAPI 自动 422）。
  - `execution_mode` **刻意保留 str**（不用 Literal）：既有契约对 `" confirm "`（带空格）要求返回 503 CAPABILITY_BLOCKED（`test_unavailable_confirmation_mode_*` 依赖），Literal 会提前变成 422 改变既定行为。
  - `app/api/routes/portfolios.py` 的 `_require_supported_execution_mode`：在 503 分支之外补全白名单——非 confirm 且不属于 `{manual, auto}` → 422，并给中文提示。
- **修复后**：非法 status/source_type/execution_mode 均 422（`api/step30_member_bad_fixed.json`、`api/after_fix/D_member_status.json`）；合法值不受影响。
- **回归**：`tests/test_whitebox_portfolio_members_api.py` 等 4 个成员套件 96 例全过；`tests/test_whitebox_portfolio_members_api.py` 30 过（含 confirm→503 契约用例）。
- **回滚**：revert 上述两文件。

## BUG-4 ｜ MySQL 无谓词唯一索引导致归档成员永远无法重新加入

- **现象**：成员归档（effective_to 已设置）后，同标的重新 `POST /members` 报 `Duplicate entry '1-3742' for key 'idx_portfolio_members_active'`（409 DB_INTEGRITY_VIOLATION）。证据：`api/step31_member_ok.json`。
- **动机（根因）**：模型 `__table_args__` 定义了**部分唯一索引**（`sqlite_where=effective_to IS NULL`，"同组合同标的有效成员唯一"的 SCD2 语义），并注释"MySQL 不支持 partial index，需由应用层检查实现"；遗留迁移 0012 也明确在 MySQL 上跳过该索引。但 `Base.metadata.create_all` 在 MySQL 上会把这个带 sqlite_where 的索引物化成**无谓词的普通唯一索引**——归档历史行也参与唯一性，与 SCD2 设计直接矛盾。应用层 `create_member` 本就有 `effective_to IS NULL` 检查兜底，数据库层这个坏索引只挡正常操作。
- **改动**：新迁移 `2026_10_24_0079_member_active_unique_mysql.py`：仅 MySQL 方言删除 `idx_portfolio_members_active`（SQLite 的真·部分索引语义正确，不动）；downgrade 在存在多版本成员历史时拒绝恢复（防降级破坏 SCD2）。
- **修复后**：归档成员 601318 重新加入成功（201，新成员 id=17）。证据：`api/step32_member_readmit.json`。
- **回归**：迁移链 26 例全过；漂移校验确认不引入新漂移。
- **注意**：**正式库 gpfx 存在同样的坏索引**，部署 0079 即可修复（本次验证不碰正式库）。
- **回滚**：对验证库 `alembic downgrade wps_0024_078_request_origin`。

## BUG-6 ｜ 组合 account_type 无校验，任意字符串可建组合

- **现象**：`POST /portfolios` 传 `account_type:"bogus_type"` 返回 200 建出脏组合（id=15，验证后已删除）；库内历史上已存在 `simulated/simulation` 两种杂值。证据：`api/step38_bad_account.json`。
- **动机**：模拟交易、自动交易、权益快照等全部链路只认 `account_type=="simulated"`；前端 `CreatePortfolioModal` 的真实取值只有 `simulated`（模拟）/`manual`（真实手动）。脏值组合会静默失去全部模拟功能且无法自愈（account_type 创建后不允许修改）。
- **改动**：`app/schemas/portfolio.py` 的 `PortfolioCreate.account_type`：`str` → `Literal["simulated", "manual"]`。`PortfolioUpdate` 本就不含该字段，无需处理。
- **修复后**：bogus_type → 422；manual → 200 正常。证据：`api/step39_bad_account_fixed.json`、`api/after_fix/E_account_type.json`。
- **回归**：`tests/test_whitebox_portfolio_strategies_api.py` + `tests/test_blackbox_api.py` 42 过；既有用例 `test_create_portfolio_real_account_no_cash_ledger` 同步从非线上取值 `"real"` 修正为线上契约值 `"manual"`（前端 `type==="real" → account_type="manual"`）。
- **回滚**：revert 该 schema 文件（及测试取值修正）。

## BUG-7 ｜ 全局异常处理器吞掉结构化 409 的诊断信息

- **现象**：自动交易 `dry_run=false` 被 409 拒绝时，`technical_details.error_message` 为空串（阻断原因实际藏在 `extras.blockers`）。证据：`api/step44_execute_real_blocked.json`。
- **动机**：`app/main.py` 的 `http_exception_handler` 从结构化 detail 提取人可读技术信息的候选键列表缺 `error_message`（路由层构造的 `{"error_message": ..., "blockers": [...]}` 用的是这个键名），运维排障时诊断字段断裂。
- **改动**：`app/main.py` tech_parts 候选键追加 `detail_dict.get("error_message")`（1 行）。
- **修复后**：同类 409/400 响应诊断字段有值（`api/after_fix/F_409_diag.json` 可见完整原因透传）。
- **回归**：`tests/test_whitebox_error_protocol.py` 22 例全过。
- **回滚**：revert 该行。

## BUG-5 ｜ 前端"自动接管"徽章把加载失败渲染成"0 项待处理"

- **现象**：刷新组合交易页瞬间徽章显示"自动接管 · 0 项待处理"（实际 API 有 3 个 blockers）；数据加载完成后才跳到正确数字。
- **动机**：`blockerCount = currentActionItems?.summary.blockers ?? 0`——action-items 请求未返回/失败时 `currentActionItems=null`，计数恒为 0；而 `governanceBlocked` 又使状态色为 notReady，两者组合把"未加载"渲染成"没有待处理"，误导用户以为一切正常。
- **改动**：`PortfolioTradingShell.tsx` 的 `pillText`：notReady 且聚合未加载（`loaded=false`）时回退"状态未知"文案，不再套用 0 计数模板。
- **修复后**：实测加载中显示"自动接管：状态加载中"，终态"自动接管 · 4 项待处理"（浏览器快照前后对照在会话记录）。
- **回归**：`portfolio-trading` 全目录 29 文件 291 测试全绿。
- **回滚**：revert 该组件改动。

## BUG-9 ｜ 手动模拟买单绕过 T-D4 状态门禁

- **现象**：自动交易链在下单前调用 `order_entry_gate_check`（block_level≥3 拒 BUY、允许风险 SELL），但 `place_sim_order`（手动/AI 通用入口）**不经过**该门禁——UI 明示"🛑 新买单：禁止"的阻断组合，手动买单却可成交（仅被覆盖基线断言拦住无基线组合）。
- **动机**：与系统"阻断状态禁新买单、放行风险退出"的治理语义对齐；门禁缺口会让对账阻断期间的组合通过手动路径继续加仓，破坏阻断的意义。
- **改动**：`app/services/sim_accounts.py` 的 `place_sim_order` buy 分支（覆盖断言之后）追加 `order_entry_gate_check(db, portfolio.id, "BUY", current_read=False)`；不通过 → 409 `{error_code: COMPOSITE_BLOCK_LEVEL_TOO_HIGH, user_message: "组合当前处于阻断状态，新买单已被禁止；风险退出卖单不受影响…"}`。**卖单刻意不过此门**（手动风险退出是人的决策，与 UI 语义一致）。自动交易链原有门禁调用不受影响（幂等复检）。
- **修复后**：阻断组合（构造态）手动买单 409 门禁拒绝且 0 订单/0 成交/0 资金变动；同组合卖单穿过门禁到业务层（400 Not enough position，证明未被门禁拦截）。证据：`api/after_fix/A2_gate_result.json`、`A3_sell_result.json`。
- **回归**：`tests/test_whitebox_investment_center.py` 新增 2 用例（含 fail-closed 断言），21 全过；下单相关 7 套件 94 过；后端大批次 376 过。
- **回滚**：revert `sim_accounts.py` 该段（及新增测试）。

## BUG-10 ｜ 迁移链不完整：纯 alembic 建库缺 24 表/186 列/99 索引

- **现象**：不走 `create_all`/auto-align、仅 `alembic upgrade head` 建的库，缺 `cash_ledger`、`sim_trades`、`portfolio_factor_usage` 等 24 张 ORM 表、186 列、99 索引（探针实测输出在案）。现有环境能跑全靠启动时 `_auto_align_all_schema` 兜底——漂移校验工具自己警告"应用能跑不代表迁移是对的"。
- **动机**：新环境部署/灾备重建若只走迁移链会得到残缺库；把 auto-align 的动作固化为迁移，让迁移链独立成立。
- **改动**：新迁移 `2026_10_24_0080_orm_alignment_catchup.py`：create-only（缺表 create_all / 缺列 ADD COLUMN / 缺索引补建），复用 `init_db` 的 `_IDENTIFIER_RE`、`_sa_type_to_ddl`、`_refresh_model_metadata` 保证与启动对齐同一套口径；**带 where 谓词的部分索引只在对应方言创建**（MySQL 上跳过，避免把 0079 刚删掉的坏索引原样重建）；downgrade 刻意 no-op（对象归模型所有，降级不删业务表）。
- **修复后**：纯迁移构建探针：缺表 0 / 缺索引 0 / 缺列 1（`idempotency_records.id`，主键列不能 ALTER，由 BUG-11 的 081 专项解决）；对已对齐库（gpfx_validation）执行为幂等 no-op（实测新增列 0/索引 0）。
- **回归**：迁移链 26 例全过。
- **回滚**：对验证库降级（幂等 no-op 场景无实际副作用）。
- **部署注意**：正式库 gpfx 应用 080 预期同样是 no-op。

## BUG-11 ｜ 幂等表主键契约断裂（本轮新发现的潜伏 P0）

- **现象**：`select(IdempotencyRecord)` 在 MySQL 上直接 `OperationalError 1054: Unknown column 'idempotency_records.id'`（实测复现）。
- **动机（根因）**：ORM（WP0-2 契约）声明 `id` 自增主键 + `idempotency_key` 唯一键；迁移链建出的表却是 `idempotency_key` 主键、**没有 `id` 列**；auto-align 无法新增主键，缺口永远存在。当前 `IdempotencyService` 尚无生产调用方（仅 TDD 测试引用），且 sqlite 测试经 create_all 建库形态正确——所以一直没炸，属于"接线即炸"的潜伏 P0。
- **改动**：新迁移 `2026_10_24_0081_idempotency_records_pk_contract.py`：MySQL 走 `DROP PRIMARY KEY` + `ADD COLUMN id … AUTO_INCREMENT PRIMARY KEY` + 补 `ix_idempotency_records_idempotency_key` 唯一索引（写库时两库均 0 行，无数据风险；若有行 MySQL 会自动回填自增值）；SQLite 空表按 ORM 重建、非空走 batch 重建；downgrade 刻意拒绝执行（回退会再造断裂）。
- **修复后**：ORM 查询成功；插入探针 `id=1` 自增生效，插入/查询/删除闭环（会话内探针日志）。
- **回归**：迁移链 26 例全过。
- **部署注意**：正式库 gpfx 应用 081 前建议再确认该表 0 行。
- **回滚**：不可逆（设计上拒绝降级）。

## BUG-8 ｜ 重复成员 409 的 next_actions 误导

- **现象**：重复创建成员返回 409，但错误体的操作建议是"查看 readiness 接口获取详细阻塞原因"——与"成员已存在"毫无关系。
- **动机**：路由把 `ValueError` 原文塞进 detail，全局处理器给所有裸 409 统一附加了通用 next_actions。
- **改动**：`create_portfolio_member` 的 ValueError 分支识别"成员已存在"，改抛结构化 detail：`MEMBER_ALREADY_EXISTS` + 冲突成员 id/status + 正确指引（改执行模式用更新；重新加入先归档）。
- **修复后**：`api/after_fix/C_dup_member.json`：error_code=MEMBER_ALREADY_EXISTS，extras.existing_member_id=17，文案可直接指导操作。
- **回归**：成员 API 套件 30 过。
- **回滚**：revert 该分支。

## BUG-12 ｜ 前端 5 个基线失败测试（组件重构后断言过期）

- **现象**：`GovernanceOverviewPanel.test.tsx` 1 例 + `PortfolioGovernanceTab.reconcile.test.tsx` 4 例在干净基线（stash 对照验证）即失败。
- **动机（根因）**：组件演进后测试未跟进——①快捷动作文案 `quickAudit` 从"查审计记录"改为"查看操作记录"；②对账结论卡重构为 `InspectionReconciliationSummary`（保留 `recon-verdict` testid），verdict 文案从"账实不符/核对证据尚不完整/对账通过"演进为"发现账实差异/当天执行情况尚未核实/当天对账已通过"，"仅预览当前账本"改为"只读预览"。
- **改动**：仅改两个测试文件的 6 处断言为新文案；**测试意图全部保留**（中文维度名"当日无决策运行"/"持仓不符"、后端枚举脱敏、"加减和"禁语等断言原样）。
- **修复后**：portfolio-trading 全目录 29 文件 291 测试全绿（原 5 failed | 163 passed）。
- **回滚**：revert 两个测试文件。

## BUG-13 ｜ 迁移链降级测试与 append-only 设计冲突

- **现象**：`test_downgrade_drops_new_tables`、`test_upgrade_downgrade_upgrade_cycle` 必败——`downgrade base` 从 head 走第一步（0078）就被 `RuntimeError: Retain request provenance; do not delete acceptance evidence` 拒绝（实测：从 head 只能降 1 步）。
- **动机**：链上 9 个 append-only 迁移（wps_0023_049/050/073…078）刻意拒绝降级以保留验收证据；"降到 base"结构性不可能，测试设计已过时。
- **改动**：`tests/test_migration_alembic_chain.py` 新增常量 `REVERSIBLE_PREFIX_HEAD = wps_0023_048_factor_audit_logs`（链上最早的不可逆迁移的前一个）：`test_downgrade_drops_new_tables` 改为升级到边界再降 base（38 张断言表全部在边界前创建，断言语义不变）；cycle 测试循环限定前缀、第二轮仍升到 head 覆盖全链。
- **修复后**：2 例通过；迁移链全文件 26 过。
- **回滚**：revert 测试文件。

---

## 环境阻断项（非代码缺陷，未改动代码）

**BUG-14 ｜ baostock 历史状态数据源不可达 → 回测中心完整 e2e 与下载导入受阻**

- 现象：两次历史状态下载任务 0/11 块"等待超时"（`api/step76/step86`）。
- 根因证据链：①本机代理为 TUN fake-ip 模式，`www.baostock.com` DNS 解析到保留段 198.18.0.206；②直接 `bs.login()` 挂起（40 秒探针超时被杀）；③通过 DoH（AliDNS/DNSPod 双源一致）取得真实 IP 114.94.20.92，TCP 直连成功但协议交换失败（WinError 10057 / error_code 10002007 网络接收错误）；④对照源 eastmoney/sina 同机 0.1 秒连通。结论：**代理对 baostock 上游不可达**。
- 应用侧核验：下载服务 fail-closed、报可重试错误、保留已取得原始文件——行为正确；回测预检 70% 覆盖率硬门禁无旁路，且 `is_verified_status_row` 的来源黑名单（unknown/mock/测试/快照/当前…）从设计上禁止用当前快照或伪造来源补历史——这正是不能"造数绕过"的原因。
- **最终处置（2026-10-25）**：根因证据链维持成立（服务端 10030 对所有路径不可达）；**已通过第六节"替代数据源管道"绕过完成回测 e2e**；BUG-15 的 BAOSTOCK_PROXY 隧道留作其恢复后的回切兜底。
- 引擎级保障：回测引擎已由本轮 376 个通过用例中的回测套件覆盖（`test_whitebox_portfolio_backtest.py`、`test_whitebox_backtest_apply.py` 等）。

## BUG-15 ｜ 受限网络下 baostock 数据端口（TCP 10030）不可达 — 代理隧道兜底能力

- **现象（修复前）**：baostock 直连 `www.baostock.com:10030` 超时（80/443 端口正常、DNS 正常）；本地代理 7897 对 CONNECT 返回 200 但上游 ~5 秒断开——用**真实登录报文**（版本 00.8.90+CRC32）与**静默对照**（建隧道不发数据）双重复现，证明与协议内容无关、纯属代理上游连不通。证据：网络探针记录（CHANGES.md 第五节）。
- **动机**：直连被网络丢弃的环境下，应能借道可达的本地代理完成数据端口连接；同时不改变默认行为、不影响其他网络路径。
- **改动**：`app/services/security_status/baostock_source.py` 新增 `_install_proxy_tunnel()`（+49 行）：
  - 仅当环境变量 `BAOSTOCK_PROXY=host:port` 设置时启用；未设置时 `socket.connect` 完全不被替换（已验证）
  - 只在抓取**子进程**内生效、只拦截目标端口 10030 的 connect：先连代理 → 发送 HTTP CONNECT → 校验 200 → 隧道建立后 send/recv 透明复用
  - 配置非法时抛 `SourceDownloadError`（带中文提示）；握手超时 30s，整体时限仍由父进程预算控制
- **修复后验证**：CONNECT 握手逻辑经真实代理验证（200 Connection established）；本机因代理上游不通无法端到端验证数据面——隧道作为 baostock 服务恢复后（或换可达代理时）的即用兜底。无 env 时安全状态相关 94+29 测试全过。
- **回滚**：revert `_install_proxy_tunnel` 函数与 `_capture_worker` 内一行调用。

## BUG-16 ｜ 下载范围测试的时区边界 flake

- **现象（修复前）**：`test_default_scope_includes_current_members_and_remaining_holdings` 在北京 00:00-08:00 运行必败：断言 `end_date < UTC今日`，而 end_date 按"北京昨日"截断——北京凌晨时两者相等。实测复现于回归批次（改动点⑰轮）。
- **动机**：纯测试时区假设错误，属边界 case 兼容；非产品缺陷。
- **改动**：`tests/test_security_status_download.py` 断言改用 `Asia/Shanghai` 时区当日比较（+注释说明边界成因）。
- **修复后验证**：29/29 过。
- **回滚**：revert 该断言。

## BUG-17 ｜ MySQL DATETIME 秒截断 → CSV 导入的涨跌停执行约束永不挂接（潜伏 P0）

- **现象（修复前）**：CSV 状态导入 1205 行成功后，回测预检仍报"缺少历史涨跌停资料，共 964 个股票交易日"。逐环节调试定位：`local_import._same_source` 比对 `row.source_updated_at`（MySQL `DATETIME` 列**截断到秒**：`00:33:20`）与批次回执的完整微秒时间戳（`00:33:20.126864`）**恒不等** → 执行约束（逐日涨跌停）对每一行都解析失败。
- **动机（影响面）**：回执与状态指纹（`timespec="microseconds"`）按微秒精度设计；SQLite 把 DateTime 存字符串不截断 → **测试全绿而 MySQL 生产必炸**。这意味着 MySQL 环境下任何历史状态 CSV 导入都无法支撑回测预检——**这是回测中心在真实 MySQL 数据上从未跑通的深层根因**（比 baostock 不可达更本质：即使数据源恢复，导入也会卡死在这一层）。
- **改动**：
  - `app/models/security_status.py`：`SecurityStatusDaily.source_updated_at` → `DateTime().with_variant(DATETIME(fsp=6), "mysql")`
  - 新迁移 `alembic/versions/2026_10_25_0082_security_status_datetime6.py`：MySQL `MODIFY source_updated_at DATETIME(6)`，其余方言 no-op；downgrade 可回退
  - 新增回归 `tests/test_security_status_mysql_precision.py`：MySQL 方言编译断言（`DATETIME(6)` in compiled），防止模型精度被无意调低
- **修复后验证**：同一导入批次执行约束解析 **25/25 挂接成功**（含完整回执链：限价/tick/来源声明）；安全状态+回测 5 套件 **160 例全过**；随后回测 e2e 直接打通（见下节）。
- **部署注意**：正式库 gpfx 需部署 0082；**历史已导入行的时间戳已被截断，如生产有旧导入需重新导入**才能挂接约束。
- **回滚**：revert 模型+迁移+测试（082 的 downgrade 亦可将列改回，但截断不可逆）。

## 回测中心 e2e 完成记录（2026-10-25，替代数据源管道）

**背景**：BUG-14（baostock 服务端 10030 不可达）阻断内置自动源；用户提议 qstock。结论：qstock 不适用（包装库、无逐日历史 ST/停牌），正确路径是系统**官方导入通道**（`/security-status/import`：人工核对+来源声明+本人确认，本就为多来源设计）。

**数据管道（全部真实证据，来源声明完整留痕于导入回执）**：

| 层 | 方法与验证 |
|---|---|
| 行情缺口 | 官方 `POST /market-data/update` 补齐 600428/600519/601318（另恢复其 `is_active` 标记）→ 五标的窗口 **241/241 零缺口 = 零停牌** |
| ST 核验 | 上交所/深交所官方名录名称均无 ST；600428/600519/601318 单日波幅 10.09%/8.61%/6.40% 超 ST 5% 涨跌停约束直接排除；510300 为 ETF 定义性不适用 |
| 上市日期 | 交易所官方名录（4 只股票）+ 东财基金档案成立日期（ETF，早窗口 13 年） |
| 涨跌停 | **不复权**前收 ±10% 四舍五入至 0.01（除息日基准=前收−每股现金红利，分红明细取自新浪）；**1205 行全部经当日 high/low 边界校验零违例**——期间 qfq 价算出的错误限价（600428 一日违例、茅台系统性偏差 ~28 元）被该校验当场抓获并修正为不复权价 |
| 导入 | 1205 行（5 标的 × 241 交易日）经 preview→confirmed 导入，provenance 抽验 5/5 通过 |

**回测结果**：

- **run 2012（组合 14，manual 评分模式）**：482 检查日、**124 个买入信号日、3 笔真实成交**——含一笔 2026-06-10 入场、06-30 **-8.6% 止损退出**的完整风控闭环；总收益 0.0188%、最大回撤 0.0212%、夏普 -0.27、均持仓 20 天。信号→入场→风控离场→指标计算全链路真实数据跑通。
- run 2011（组合 1，继承 8 月锁定的 ridge 模型绑定）：completed、0 成交——绑定模型在窗口内无评分产出，**模式覆盖被锁定快照拒绝，治理行为正确**。
- UI 验证：回测历史列表（#2011/#2000…）、绩效面板、配置回显（初始资金 50 万/佣金 0.03%/基准沪深300）、曲线全部正常渲染（截图 `screenshots/backtest_e2e_run2011.png`）。

**过程中的附带产物**：组合 14 新增成员 18/19（auto）、候选 4/5、宽松信号规则 id=4（容差 40 → 准入分 40，解开默认 68 分对 50 分评分的过滤）。

## 验证与回滚速查

- **全部回归结果（最终）**：后端核心批次 376 过/0 败；迁移链 26 过；安全状态+BUG-17 回归+回测 160 过；成员/投资中心/下单 21+96+63 过；前端 portfolio-trading 29 文件 291 全绿（原 5 失败清零）；**回测 e2e：run 2012 真实成交 3 笔（含止损闭环）+ UI 全渲染**。漂移报告前后对照 `tmp/validation-2026-10-24/logs/drift_report_{before,after}_fix.txt`（余下 49 表"多索引"= 迁移刻意创建的性能索引 ORM 未镜像，属建模约定差异，P2）。
- **逐项回滚**：代码文件直接 `git checkout -- <file>`；迁移按文件内 docstring 的 downgrade 说明（081/082 不可逆，兜底为备份恢复）。
- **改动全景（最终）**：13 个修改文件（+223/−28）+ 4 个新迁移 + 1 个新回归测试 + 2 篇文档；逐项动机见本文档，操作日志见 `tmp/validation-2026-10-24/CHANGES.md`（改动点①-㉑），后续执行见 `docs/validation-followup-2026-10-25.md`。

## 附：BUG 处置总表（一页速览）

| BUG | 一句话 | 状态 |
|---|---|---|
| BUG-3 | 成员 status/source_type/execution_mode 脏值入库 | ✅ 已修（Literal+路由白名单） |
| BUG-4 | MySQL 无谓词唯一索引 → 归档成员无法重新加入 | ✅ 已修（迁移 0079；正式库待部署） |
| BUG-5 | 徽章把"未加载"渲染成"0 项待处理" | ✅ 已修 |
| BUG-6 | account_type 任意字符串建组合 | ✅ 已修（Literal） |
| BUG-7 | 409 诊断字段空串 | ✅ 已修 |
| BUG-8 | 重复成员 409 文案误导 | ✅ 已修（结构化） |
| BUG-9 | 手动买单绕过 T-D4 门禁 | ✅ 已修（含回归用例） |
| BUG-10 | 纯迁移建库缺 24 表/186 列/99 索引 | ✅ 已修（迁移 080） |
| BUG-11 | 幂等表主键契约断裂（MySQL ORM 查询 1054） | ✅ 已修（迁移 081） |
| BUG-12 | 前端 5 个基线失败测试 | ✅ 已修 |
| BUG-13 | 迁移链降级测试与 append-only 冲突 | ✅ 已修 |
| BUG-14 | baostock 服务端 10030 不可达 | 🔄 环境项：以替代管道绕过，e2e 已完成；隧道兜底待服务恢复 |
| BUG-15 | 受限网络无 baostock 代理通道 | ✅ 已加 BAOSTOCK_PROXY 门控隧道（上游待网络放行验证） |
| BUG-16 | 下载范围测试时区边界 flake | ✅ 已修 |
| BUG-17 | MySQL DATETIME 秒截断 → 涨跌停约束永不挂接 | ✅ 已修（模型+迁移 0082+回归；正式库待部署） |
