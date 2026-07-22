"""AI 第一阶段只读工具集（WP-AI.4）。

7 个工具：
- get_capabilities：当前系统能力门禁和允许的下一步
- get_data_health：数据健康状态
- get_task_status：任务状态
- get_symbol_research：标的研究数据
- get_candidate_explanation：候选解释
- get_portfolio_summary：组合摘要
- get_backtest_explanation：回测解释

每个工具统一签名 `(db: Session, context: ContextPack) -> dict`：
- 只接收 db 和 ContextPack，不接收任意 SQL
- 只读操作，不写数据库
- 返回结构化 dict
- 缺数据时返回 `{"status": "no_data", "message": "无可用数据"}`

设计约束：
- AI 不得自行查询任意数据库或调用第三方接口
- 所有查询都通过本工具集走预定义路径
"""
from __future__ import annotations

from app.services.ai.tools.backtest_explanation import get_backtest_explanation
from app.services.ai.tools.candidate_explanation import get_candidate_explanation
from app.services.ai.tools.capabilities import get_capabilities
from app.services.ai.tools.data_health import get_data_health
from app.services.ai.tools.portfolio_summary import get_portfolio_summary
from app.services.ai.tools.symbol_research import get_symbol_research
from app.services.ai.tools.task_status import get_task_status


# 工具注册表：tool_name -> callable(db, context) -> dict
TOOL_REGISTRY = {
    "get_capabilities": get_capabilities,
    "get_data_health": get_data_health,
    "get_task_status": get_task_status,
    "get_symbol_research": get_symbol_research,
    "get_candidate_explanation": get_candidate_explanation,
    "get_portfolio_summary": get_portfolio_summary,
    "get_backtest_explanation": get_backtest_explanation,
}


def call_tool(tool_name: str, db, context) -> dict:
    """统一工具调用入口。

    Args:
        tool_name: 工具名（必须在 TOOL_REGISTRY 中）
        db: 数据库会话
        context: ContextPack 实例

    Returns:
        工具返回的 dict

    Raises:
        KeyError: 工具名未注册
    """
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        raise KeyError(f"未知工具: {tool_name}（可用: {list(TOOL_REGISTRY.keys())}）")
    return fn(db, context)


__all__ = [
    "TOOL_REGISTRY",
    "call_tool",
    "get_capabilities",
    "get_data_health",
    "get_task_status",
    "get_symbol_research",
    "get_candidate_explanation",
    "get_portfolio_summary",
    "get_backtest_explanation",
]
