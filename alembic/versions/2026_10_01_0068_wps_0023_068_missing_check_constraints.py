"""补齐 19 个「ORM 已声明、库里未落实」的 CHECK 约束（含 1 处数据修正）。

Revision ID: wps_0023_068_missing_check_constraints
Revises: wps_0023_067_missing_foreign_keys

背景
====
TD5《技术债-全库schema漂移核对报告》§A4：11 张表有 19 个 ORM 声明的 CHECK 约束
**在库里无一落实**（MySQL 8.0.16+ 才真正实施 CHECK，而建表/auto-align 当时都没带上）。
枚举与取值范围只剩应用层防线。

前置清查（2026-10-01，只读）
============================
用 `WHERE NOT (<check sqltext>)` 对 19 个 CHECK 逐条统计违规行，结果：
**15 个零违规**（可直接加），**4 个全部违规** —— 全在 `portfolios`：

    ck_portfolios_status_data_values / status_model / status_score / status_reconciliation
    违规 12 / 12 行

根因：这 4 列在 `app/models/portfolio.py` 里声明为
`default=PORTFOLIO_STATUS_READY`、`server_default=PORTFOLIO_STATUS_READY`（='READY'），
**但库里 12 行全是空字符串 `''`** —— 建列时 server_default 没落到 DDL，
MySQL 对 NOT NULL VARCHAR 给了隐式默认 `''`。

这不是小事：`app/services/portfolio_status.py` 把这 4 列强类型成 `PortfolioStatus`
枚举（`"status_score": self.status_score.value`），读到 `''` 会构造不出枚举值。
即这 4 列**从建列起就没被正确初始化过**。

处置
====
1. **先把 12 行的 `''` 修正为 `'READY'`** —— 与 ORM 声明的 default 对齐，
   恢复该列被强类型读取时的可用性。（改前已全表备份到
   `.workbuddy/mining/backups/`）
2. 再补 19 个 CHECK。

实现要点
========
- **SQLite 只做第 1 步**：第 2 步的 `ALTER TABLE ADD CONSTRAINT` SQLite 不支持，
  打印「预期行为」后跳过（沿用 0054/0056/0067 先例）；
- **幂等**：CHECK 用 `inspector.get_check_constraints` 判存在；
  数据修正用 `WHERE col = ''` 限定，重跑无副作用。

⚠️ 实测告知：本机 MySQL 是 **5.7.26**，CHECK 在此版本**不生效**
============================================================
执行本迁移后实测（2026-10-01）：

    $ SHOW CREATE TABLE portfolios   → DDL 里没有任何 CHECK 子句
    $ SELECT VERSION()               → 5.7.26

MySQL **5.7 及以前会「解析并忽略」CHECK 约束**（parsed but ignored，不报错也不生效），
**8.0.16+ 才真正实施**。因此本次 19 个 `ADD CONSTRAINT ... CHECK` 实际是 **no-op**：
迁移打印「新建 19 个」且无异常，但 `verify_schema_drift.py` 的 `missing_check`
仍是 11 —— 这**不是迁移写错**，而是目标库版本不支持。

TD5 报告 §A4 写着「MySQL 8.0.16+ 已支持」，但当时没核实本机版本。

**结论**：CHECK 这条线在当前环境**做不了**，要么升级 MySQL 到 8.0.16+，
要么按报告 §A4 括号里那半句「若应用层校验完备可降 P3」→ 登记为已知豁免。

**本迁移真正有效的是第 1 步**：`portfolios` 的 4 个 status_* 列，
12 行 × 4 列 = **48 个空串已修正为 'READY'**（与 ORM 的 `default='READY'` 对齐）。
这修掉了一个真实隐患 —— `app/services/portfolio_status.py` 把这 4 列强类型成
`PortfolioStatus` 枚举，读到 `''` 会构造不出枚举值。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "wps_0023_068_missing_check_constraints"
down_revision = "wps_0023_067_missing_foreign_keys"
branch_labels = None
depends_on = None

# 需要修正空串的列（均为 NOT NULL VARCHAR，ORM 声明 default='READY'）
_FIX_EMPTY_TO_READY = (
    "status_data",
    "status_model",
    "status_score",
    "status_reconciliation",
)

# (表, CHECK 名, 条件 SQL)
# 由 `.workbuddy/mining/_dump_check_defs.py` 从 ORM 的 CheckConstraint 提取后
# **内联**在此处 —— 迁移必须自包含，不能依赖工作区里的中间文件。
_CHECK_DEFS: list[tuple[str, str, str]] = [
    ("alert_events", "ck_alert_events_severity_level",
     "severity_level IN ('L3','L2','L1')"),
    ("alert_events", "ck_alert_events_status",
     "status IN ('ACTIVE','ACKNOWLEDGED','RESOLVED','SUPPRESSED')"),
    ("data_governance_audit_events", "ck_dg_audit_action_values",
     "action IN ('PORTFOLIO_CANDIDATE_SCD2_CHANGE','BENCHMARK_SOURCE_FAILOVER',"
     "'AUTO_SIMULATION_RESULT','RECONCILIATION_RESULT','ILLEGAL_STATE_TRANSITION',"
     "'FACTOR_USAGE_APPLIED','OUTBOX_EVENT_DISPATCHED','DATA_BLOCK_RESOLUTION',"
     "'DATA_SOURCE_FAILOVER','DATA_QUALITY_QUARANTINE','G6_ROLLOUT_STARTED',"
     "'G6_ROLLOUT_ROLLED_BACK','FACTOR_DRAFT_SUBMITTED','FACTOR_DRAFT_APPROVED',"
     "'FACTOR_DRAFT_REJECTED','FACTOR_VERSION_PROMOTED','UNKNOWN_AUDIT_ACTION')"),
    ("decision_evidence", "ck_decision_evidence_action_6values",
     "action IN ('BUY','SELL','HOLD','NO_ACTION','REJECTED','DATA_BLOCKED')"),
    ("decision_runs", "ck_decision_runs_run_type_3values",
     "run_type IN ('research_preflight','backtest','auto_simulation')"),
    ("decision_runs", "ck_decision_runs_status_6values",
     "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','INTERRUPTED','CANCELLED')"),
    ("governance_events_outbox", "ck_gov_outbox_retry_count_range",
     "retry_count >= 0 AND retry_count <= 200"),
    ("governance_events_outbox", "ck_gov_outbox_status_values",
     "status IN ('READY','IN_PROGRESS','DELIVERED','DEAD_LETTERED')"),
    ("manual_price_overrides", "ck_manual_price_override_resolved_mode_3values",
     "resolved_mode IN ('confirm_manual_price','continue_forward','keep_paused')"),
    ("outbox_events", "ck_outbox_events_status_values",
     "status IN ('PENDING','SENT','DEAD')"),
    ("portfolio_cron_schedules", "ck_portfolio_cron_sched_hour",
     "schedule_hour BETWEEN 0 AND 23"),
    ("portfolio_cron_schedules", "ck_portfolio_cron_sched_minute",
     "schedule_minute BETWEEN 0 AND 59"),
    ("portfolio_cron_schedules", "ck_portfolio_cron_sched_type",
     "schedule_type IN ('auto_simulation','daily_preview_dry_run')"),
    ("portfolio_factor_usage", "ck_pfu_binding_status_values",
     "binding_status IN ('DRAFT','APPLIED','RETIRED')"),
    ("portfolios", "ck_portfolios_status_data_values",
     "status_data IN ('READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED',"
     "'MODEL_INACTIVE','RECONCILIATION_BLOCKED')"),
    ("portfolios", "ck_portfolios_status_model_values",
     "status_model IN ('READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED',"
     "'MODEL_INACTIVE','RECONCILIATION_BLOCKED')"),
    ("portfolios", "ck_portfolios_status_reconciliation_values",
     "status_reconciliation IN ('READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED',"
     "'MODEL_INACTIVE','RECONCILIATION_BLOCKED')"),
    ("portfolios", "ck_portfolios_status_score_values",
     "status_score IN ('READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED',"
     "'MODEL_INACTIVE','RECONCILIATION_BLOCKED')"),
    ("task_idempotencies", "ck_task_idempotencies_task_type_4values",
     "task_type IN ('backtest','factor_calc','model_train','auto_trade')"),
]


def _existing_check_names(conn, table: str) -> set[str]:
    try:
        return {
            (ck.get("name") or "").strip()
            for ck in sa.inspect(conn).get_check_constraints(table)
        }
    except Exception:
        return set()


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    # ── 第 1 步：数据修正（两种方言都做） ──
    fixed = 0
    for col in _FIX_EMPTY_TO_READY:
        res = conn.execute(sa.text(
            f"UPDATE portfolios SET `{col}` = 'READY' WHERE `{col}` = ''"
        ))
        fixed += res.rowcount or 0
    print(f"[0068] portfolios 状态列空串修正：{fixed} 行 -> 'READY'")

    # ── 第 2 步：补 CHECK（仅 MySQL） ──
    if dialect != "mysql":
        print(
            f"[0068] dialect={dialect}：不支持 ALTER TABLE ADD CONSTRAINT，"
            "跳过补 CHECK（预期行为）"
        )
        return

    created = 0
    skipped = 0
    for table, name, cond in _CHECK_DEFS:
        if name in _existing_check_names(conn, table):
            skipped += 1
            continue
        op.create_check_constraint(name, table, cond)
        created += 1
    print(f"[0068] CHECK 补齐：新建 {created} 个，跳过已存在 {skipped} 个")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "mysql":
        return
    for table, name, _cond in _CHECK_DEFS:
        if name not in _existing_check_names(conn, table):
            continue
        try:
            op.drop_constraint(name, table, type_="check")
        except Exception:
            pass
