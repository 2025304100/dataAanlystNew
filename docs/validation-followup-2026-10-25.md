> **2026-10-25 评审后实施更新**：0079～0082已修订，新增0083补偿，真实5.7发现并修复MyISAM事务前提、RR门禁及账务旧快照、历史微秒恢复等问题。请以 [修复台账](组合校验P0-P1修复台账-2026-10-25.md) 和 [交接补充](组合校验隔离修复交接补充-2026-10-25.md) 为现行状态；本文以下历史结果与部署命令保留供审计，不自动执行。本轮没有正式部署，仍保留原有未提交改动。

# 验证与修复后续跟进文档（2026-10-25）

> 承接 `docs/validation-fixes-2026-10-24.md`（BUG↔改动一一对照）。本文档面向**后续执行**：
> 代码入库、正式库部署、遗留事项时间线、验证环境处置、关键 Runbook。
> 全程操作日志：`tmp/validation-2026-10-24/CHANGES.md`（改动点①-㉑）。

## 一、改动清单总账（全部未提交，等入库）

**13 个修改文件（+223 / −28）**

| 文件 | 改动 | 对应问题 |
|---|---|---|
| `app/schemas/portfolio_member.py` | status/source_type 收紧 Literal、execution_mode 留路由层 | BUG-3 |
| `app/api/routes/portfolios.py` | execution_mode 白名单 + 重复成员 409 结构化 | BUG-3 / BUG-8 |
| `app/schemas/portfolio.py` | account_type → Literal["simulated","manual"] | BUG-6 |
| `app/services/sim_accounts.py` | 手动买单 T-D4 门禁 | BUG-9 |
| `app/main.py` | 异常处理器补 error_message 键（1 行） | BUG-7 |
| `app/models/security_status.py` | source_updated_at → MySQL DATETIME(6) | BUG-17 |
| `app/services/security_status/baostock_source.py` | BAOSTOCK_PROXY 门控隧道（默认关） | BUG-15 |
| `frontend/.../PortfolioTradingShell.tsx` | 徽章加载态不再显示"0 项待处理" | BUG-5 |
| `frontend/.../__tests__/GovernanceOverviewPanel.test.tsx` | 文案演进断言修正 | BUG-12 |
| `frontend/.../__tests__/PortfolioGovernanceTab.reconcile.test.tsx` | 4 处 verdict 文案断言修正 | BUG-12 |
| `tests/test_migration_alembic_chain.py` | 降级测试适配 append-only（可逆前缀） | BUG-13 |
| `tests/test_security_status_download.py` | 时区边界 flake 修复 | BUG-16 |
| `tests/test_whitebox_investment_center.py` | 新增买/卖门禁回归 2 用例 + real→manual 取值修正 | BUG-9 / BUG-6 |

**7 个新增文件**

| 文件 | 内容 |
|---|---|
| `alembic/versions/2026_10_24_0079_member_active_unique_mysql.py` | BUG-4：删 MySQL 无谓词唯一索引（归档成员可重新加入） |
| `alembic/versions/2026_10_24_0080_orm_alignment_catchup.py` | BUG-10：ORM 对齐收口（纯迁移建库不再缺 24 表/186 列/99 索引） |
| `alembic/versions/2026_10_24_0081_idempotency_records_pk_contract.py` | BUG-11：幂等表主键契约（潜伏 P0） |
| `alembic/versions/2026_10_25_0082_security_status_datetime6.py` | BUG-17：source_updated_at DATETIME(6)（**回测中心 MySQL 可用的关键**） |
| `tests/test_security_status_mysql_precision.py` | BUG-17 回归（方言编译断言） |
| `docs/validation-fixes-2026-10-24.md` | BUG↔改动对照文档 |
| `docs/validation-followup-2026-10-25.md` | 本文档 |

## 二、建议的入库拆分（按风险分 4 个 commit）

1. **feat(db-migrations)**：4 个迁移 + `app/models/security_status.py`（0082 与模型必须同 commit）
2. **fix(api-validation)**：两个 schema + portfolios.py 路由 + main.py + sim_accounts.py（BUG-3/6/7/8/9）
3. **feat(baostock-proxy)**：`baostock_source.py`（独立可回滚）
4. **test+frontend**：全部测试文件与前端 3 个文件（BUG-5/12/13/16 + 新增用例）

## 三、正式库 gpfx 部署清单（唯一必须的运维动作）

```bash
# 前置确认（预期均通过）
#   1) idempotency_records 行数 = 0（写 081 时两库均 0，执行前复核）
#   2) 备份：python scripts/db_backup.py --db-url "mysql+pymysql://root:root@127.0.0.1:3306/gpfx"

python -m alembic upgrade head   # 0079 删坏索引 → 080 预期 no-op → 081 幂等表 → 082 DATETIME(6)
```

**部署后验证**：① `SHOW INDEX FROM portfolio_members` 无 `idx_portfolio_members_active`；② 归档成员重新 POST /members 成功；③ `select(IdempotencyRecord)` 不再 1054；④ `SHOW COLUMNS FROM security_status_daily LIKE 'source_updated_at'` 为 `datetime(6)`。
**回滚**：0079 可 `downgrade`（有多版本成员历史时拒绝）；081/082 设计上不回退（downgrade 拒绝/no-op），恢复备份是兜底。
**注意**：082 之后新导入的 CSV 约束才能挂接；历史已导入行时间戳已被截断，生产如有旧导入需重导。

## 四、遗留事项与时间线

| 事项 | 触发条件 / 时间 | 动作 |
|---|---|---|
| 模型独立评估（50 信号日成熟） | 约 2026-12 下旬 | `POST /scoring/production-candidates/{plan}/independent-evaluation` → 评估 → 激活 → 应用策略 → 首次范围启用 → 覆盖起点(2026-10-26)后自动接管真实执行 |
| baostock 服务恢复 | 其 10030 端口可达（可设 `BAOSTOCK_PROXY` 走可达代理） | 回切内置自动下载源；替代 CSV 管道作为备份保留 |
| 验证环境处置 | 验证收尾 | 见第五节 |
| P2 优化（可选） | 随版本 | ① 49 表"多索引"ORM 镜像；② 漂移工具方向区分；③ state.json 观感；④ 覆盖基线"带 gaps 预览"引导；⑤ 重复候选 201→200+idempotent |

## 五、验证环境现状与切换

- **现状**：可写后端跑在 `gpfx_validation`（`DATABASE_URL` 注入，`AUTO_UNIVERSE_SYNC_ON_STARTUP=false`）；正式库 `gpfx` 全程零改动；验证库定时任务全部停用；验证数据见 `CHANGES.md` 改动点⑨/㉑（组合 14/16、成员 14-19、候选 3-5、run 2011/2012、1205 行状态导入等）。
- **切回只读验收模式**：`python scripts/dev_services.py stop` → `C:\Python312\python.exe scripts\serve_inspection_acceptance.py --no-background-jobs`（指向 gpfx）。
- **清理验证库**（可选）：`DROP DATABASE gpfx_validation` + 删 `tmp/validation-2026-10-24/gpfx_dump_20261024.sql`（5.1GB）；建议先归档 `tmp/validation-2026-10-24/` 证据目录。

## 六、Runbook：历史状态数据（替代源管道）

适用于 baostock 不可达或需补导其他标的时（本次 e2e 的完整复现）：

1. **补行情**：`POST /api/v1/market-data/update`（scope=symbols, symbol_ids, start/end_date）——标的需 `is_active=1`。
2. **核停牌**：窗口内日线对交易日历零缺口即无停牌。
3. **核 ST**：交易所名录名称 + 波幅佐证（>5.5% 超 ST 5% 约束）。
4. **算涨跌停**：**不复权**前收 ±10% 四舍五入至 0.01（除息日基准=前收−每股现金红利）；**必须用 high/low 全量边界校验，零违例才允许导入**（qfq 价会算错，本次曾被校验抓获）。
5. **导入**：`POST /security-status/import/preview` → `POST /security-status/import`（confirmed），来源声明如实写方法。
6. **回测**：`POST /backtest/portfolio/run`（评分模式须与组合锁定快照一致；无绑定组合默认 manual）。

## 七、回归基线（本轮最终数字）

| 套件 | 结果 |
|---|---|
| 后端核心批次（17 文件） | 376 通过 / 0 失败 |
| 迁移链 | 26 通过（含修复后 2 例） |
| 安全状态 + BUG-17 回归 + 回测 | 160 通过 |
| 成员/投资中心/下单 | 21 + 96 + 63 通过 |
| 前端 portfolio-trading | 29 文件 291 全绿（原 5 失败清零） |
| 端到端 | 回测 run 2012：124 信号日 / 3 笔真实成交（含止损闭环）/ UI 全渲染 |
