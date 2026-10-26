"""首批对外暴露的只读工具 schema（Batch 2，原生 function calling）。

既定约束（本批）：
- 首批仅开放 `get_data_health` / `get_capabilities` 两个低风险只读工具；
- 其余 5 个工具仍留在 TOOL_REGISTRY 内，但不出现在 EXPOSED_TOOL_NAMES，
  链路稳定后再改本文件灰度放开（无需改对话主链路）。

安全边界：工具只接受「调用哪个工具」，不接受模型自由参数——实际查询参数
绑定在会话的受控上下文包（references 已脱敏），因此这里 parameters 一律为空对象。

名称清单与 description 同步维护；新增工具时：
1) 在 tools/__init__.py 的 TOOL_REGISTRY 注册实现；
2) 在 EXPOSED_TOOL_NAMES + TOOLS_SCHEMA 增加声明；
3) 补一条白单（tool_loop 未知工具拒绝 / 新工具可被调用）。
"""
from __future__ import annotations

EXPOSED_TOOL_NAMES: frozenset[str] = frozenset({
    "get_data_health",
    "get_capabilities",
})

TOOLS_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_data_health",
            "description": (
                "查询当前系统数据健康状态（只读）：最新交易日、标的覆盖率、"
                "过期标的数与缺失项。当用户问「数据健康吗 / 数据是否最新 / 缺哪些数据」时调用；"
                "不要凭记忆作答，必须先调用本工具。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_capabilities",
            "description": (
                "查询当前系统能力门禁与允许的下一步（只读）：哪些能力可用/降级/阻断及原因。"
                "当用户问「现在能做什么 / 某功能为何不可用 / 下一步能做什么」时调用。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def exposed_tools_or_none(enabled: bool = True) -> list[dict] | None:
    """返回可传给 provider 的 tools 列表；关闭时返回 None（退回单轮问答）。"""
    return list(TOOLS_SCHEMA) if enabled else None


__all__ = ["EXPOSED_TOOL_NAMES", "TOOLS_SCHEMA", "exposed_tools_or_none"]
