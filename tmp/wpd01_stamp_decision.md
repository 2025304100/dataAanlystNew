# WPD-01 Alembic 安全 Stamp 决策

| 项目 | 内容 |
|---|---|
| 审计日期 | 2026-08-01 |
| MySQL 版本 | 5.7.26 |
| 数据库 | gpfx (127.0.0.1:3306) |
| 仓库实际 head | `wps_0801_001_universe_incremental_index` |
| 底稿记录 head | `wps_0023_020_api_deprecation_logs`（已被 2026-08-01 18:30 新增覆盖） |
| 活动 MySQL alembic_version 表 | **不存在** |
| Schema 来源 | `init_db.py` 的 `metadata.create_all()`，非 Alembic 迁移 |
| 表数量 | 77 |

## 决策

**在执行 0021 迁移前，必须先 `alembic stamp wps_0801_001_universe_incremental_index`**，但 stamp 前需完成 schema 对账：

1. **不可直接 `alembic upgrade head`**：活动库无 `alembic_version` 表，Alembic 不知道当前 schema 处于哪个 revision，直接 upgrade 会尝试从 base 执行全部迁移，可能因表已存在而报错或产生意外行为。

2. **Stamp 前 schema 对账**：将 `tmp/wpd01_audit_report.json` 中的 `columns_fingerprint` 与 ORM 模型（`app/models/`）及全部 Alembic 迁移期望的最终 schema 进行差异比对。若发现 MySQL 实际 schema 与期望不一致：
   - 差异为"MySQL 多了列/表"：可安全 stamp（多出的列不影响迁移）；
   - 差异为"MySQL 缺少列/表"：**不可 stamp**，需先用 `init_db.py` 或手动补齐缺失项后再 stamp。

3. **Stamp 执行**：对账通过后执行 `alembic stamp wps_0801_001_universe_incremental_index`，在活动库写入 `alembic_version` 表，使后续 0021 迁移可正常 `upgrade`。

4. **0021 迁移**：`revision = wps_0023_021_factor_library_lifecycle`，`down_revision = wps_0801_001_universe_incremental_index`。迁移内容为幂等增列和新表，不修改已有数据。

## 已确认的 Schema 差异（需在 WP1 处理）

| 表 | 差异 | 影响 |
|---|---|---|
| `async_tasks` | 无 `error_code` 列 | WPD-05 需新增 |
| `factor_system_config` | 仅有 id/feature_enabled/warehouse_path/updated_by/updated_at，无 weight_mode/active_model_run_id（这些在 `factor_runtime_state` 表中） | 无需修改，确认运行模式字段位置 |
| `factors` | 无 lifecycle_status/origin/factor_type/owner/active_version_id/shadow_version_id/risk_level/archived_at | WP1-01 需新增 |
| `factor_versions` | 无 formula_ast_json/postprocess_json/parameter_schema_json/data_dependencies_json/compiler_version/execution_plan_hash/complexity_score/validation_status 等 | WP1-01 需新增 |

## 备份状态

- Schema fingerprint：`tmp/wpd01_audit_report.json`（含 19 张因子相关表的列+索引 fingerprint）
- 补充审计：`tmp/wpd01_audit_supplement.json`（含 runtime_state、async_tasks 列、model_runs、audit_logs、recent_tasks）
- 完整 schema SQL dump：mysqldump 不可用，以 JSON fingerprint 替代；WP1 执行前应补一次完整数据备份

## 8 因子快照确认

8 个系统因子（id 1-8）均存在，status=active，is_active=1：
ep_ttm、negative_pb、main_inflow_5d_ratio、turnover_z20、roe_yoy_growth、lhb_institution_net_ratio、hot_rank_attention、tail_accumulation_proxy

## 运行模式确认

- `factor_runtime_state.weight_mode` = `manual`
- `factor_runtime_state.active_model_run_id` = `None`
- `factor_model_runs` 唯一记录 status=`rejected`，sample_count=0

## 数据新鲜度更新（对比底稿）

底稿审计时（2026-08-01 早些）最新交易日横截面不完整（07-28/29 仅 150/218）。本次审计发现数据已补齐：

| 交易日 | 标的数 | 底稿记录 | 状态 |
|---|---:|---|---|
| 2026-07-31 | 4,336 | - | 完整（最新） |
| 2026-07-30 | 4,336 | - | 完整 |
| 2026-07-29 | 4,334 | 218 | **已补齐** |
| 2026-07-28 | 4,335 | 150 | **已补齐** |
| 2026-07-27 | 4,335 | 2,067 | 已补齐 |
| 2026-07-24 | 4,345 | 3,703 | 已补齐 |

WPD-02 的完整交易日算法仍需实现（防止未来再次出现残缺横截面），但当前最新完整交易日已为 2026-07-31。

## 最近流水线任务

| 时间 | 状态 | 百分比 | 说明 |
|---|---|---:|---|
| 2026-08-01 10:10 | done | 100% | 最新成功 |
| 2026-08-01 07:17 | failed | 8.8% | DuckDB 锁 |
| 2026-07-29 14:16 | failed | 19% | DuckDB 锁（底稿记录） |
| 2026-07-29 14:09 | failed | 11.3% | DuckDB 锁（底稿记录） |

失败 message 中文可读（使用 utf8mb4 连接），但 `async_tasks` 无 `error_code` 列，WPD-05 仍需新增。
