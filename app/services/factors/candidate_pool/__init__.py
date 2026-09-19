"""候选池域服务包（SD-v2.0 §6.3 · M3）。

⚠️ 隔离模型（开发文档 §3.0.1）：候选池**不写回** `symbols` / 行情 / 财报主表。
   成员表只存 `symbol_id` + 纳入校验摘要；批量删除只做**关联软删除**，主数据一行不动。
"""
from __future__ import annotations

from app.services.factors.candidate_pool import service

__all__ = ["service"]
