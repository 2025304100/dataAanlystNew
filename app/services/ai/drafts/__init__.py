"""AI 第二阶段草稿工具（WP-AI.5）。

6 种草稿工具：
- draft_indicator：自定义指标草稿
- draft_filter：筛选方案草稿
- draft_alert：告警规则草稿
- draft_note：观察备注/标签草稿
- draft_review：复盘草稿
- draft_order：模拟订单草稿

三步确认流程（每种草稿都包含）：
1. draft_xxx(db, context, ai_suggestion) → AI 建议（不写 DB）
   返回 {draft_type, suggested_payload, validation_status, validation_errors, preview, requires_confirmation: True}
2. preview_xxx(db, suggested_payload) → 系统规则校验和变更预览（dry-run，不写 DB）
   返回 {is_valid, errors, changes: [...]}
3. execute_xxx(db, audit_id) → 用户确认后执行（调用普通业务 API）
   先检查 audit.user_confirmed=True，否则拒绝执行

关键约束：
- 三步流程严格分离：AI 只生成建议，系统校验后生成预览，用户确认后才执行
- 不确认时不产生任何数据库变化
- 模拟订单草稿必须重新经过所有交易校验（现金/手数/T+1/涨跌停/数据健康/组合风控）
- 自动交易永远不由对话触发
- 所有草稿操作通过 AIActionAudit 记录审计
"""
from __future__ import annotations

from app.services.ai.drafts.alert import draft_alert, execute_alert, preview_alert
from app.services.ai.drafts.filter import draft_filter, execute_filter, preview_filter
from app.services.ai.drafts.indicator import draft_indicator, execute_indicator, preview_indicator
from app.services.ai.drafts.note import draft_note, execute_note, preview_note
from app.services.ai.drafts.order import draft_order, execute_order, preview_order
from app.services.ai.drafts.review import draft_review, execute_review, preview_review


# 草稿工具注册表：draft_type -> (draft_fn, preview_fn, execute_fn)
DRAFT_REGISTRY = {
    "draft_indicator": (draft_indicator, preview_indicator, execute_indicator),
    "draft_filter": (draft_filter, preview_filter, execute_filter),
    "draft_alert": (draft_alert, preview_alert, execute_alert),
    "draft_note": (draft_note, preview_note, execute_note),
    "draft_review": (draft_review, preview_review, execute_review),
    "draft_order": (draft_order, preview_order, execute_order),
}


def get_draft_functions(draft_type: str):
    """获取指定草稿类型的三步函数。"""
    return DRAFT_REGISTRY.get(draft_type)


__all__ = [
    "DRAFT_REGISTRY",
    "get_draft_functions",
    # 各草稿三步函数
    "draft_indicator", "preview_indicator", "execute_indicator",
    "draft_filter", "preview_filter", "execute_filter",
    "draft_alert", "preview_alert", "execute_alert",
    "draft_note", "preview_note", "execute_note",
    "draft_review", "preview_review", "execute_review",
    "draft_order", "preview_order", "execute_order",
]
