"""StrategyExecutionSnapshot 兼容入口（TD6 裁决：模型分裂终结，2026-09-18）。

历史背景
--------
本文件曾是「新契约草案」（BigInteger 自增 id + 9 列），与实际注册模型
`decision_engine.StrategyExecutionSnapshot`（varchar(64) PK + 20+ 列）分叉，
一度被 `app/models/__init__.py:95` 显式排除注册。

TD5 全库审计 + TD6 `SHOW CREATE TABLE` 实证：

- DB 表 = 旧类结构 + **迁移体系外加宽的 3 列**
  （usage_binding_id int NULL 无 FK / snapshot_json text NOT NULL /
  content_hash varchar(64) NOT NULL，alembic 无任何 add_column 记录）；
- 本草案与 DB 严重不齐（id 类型、列集只有 9 列、FK 目标还写成单数表
  `portfolio_factor_usage`），**不具备权威性**；
- tests/test_wp02_g0_contracts_schemas_tdd.py 曾 import 本文件，其
  `extend_existing=True` 把三列偶然合并进同一个 Table——只有恰好 import 过
  本文件的测试进程里 save_and_apply 才不炸，生产进程必 TypeError（假绿现场）。

裁决结果
--------
**权威声明 = decision_engine.StrategyExecutionSnapshot（TD6 已补齐三列 + 索引）**。
本文件降级为 re-export 兼容壳，唯一存在意义是让历史 import 路径不断链：

    from app.models.strategy_execution_snapshot import StrategyExecutionSnapshot

（tests/test_wp02_g0_contracts_schemas_tdd.py 在用。）壳不再 declare 任何
ORM 结构，extend_existing 偶然性随之消除。

挂账（另立 P1 卡）：service `portfolio_factor_usage.save_and_apply` 仍按
「自增整型 id」契约写（构造不传 id + `int(snap.id)`），与 DB varchar(64) PK
冲突——该修复涉及契约变更，不在 TD6 边界内。
"""
from app.models.decision_engine import StrategyExecutionSnapshot  # noqa: F401

__all__ = ["StrategyExecutionSnapshot"]
