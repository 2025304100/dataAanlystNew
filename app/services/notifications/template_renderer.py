"""消息模板渲染（WP-MSG.2）。

支持变量替换与转义，防止 Markdown/Webhook 注入。

project_memory 硬约束（spec line 130）：
- 模板变量转义，防止 Markdown/Webhook 注入

实现策略：
- 模板使用 {variable_name} 占位符（与 str.format 不同，避免格式攻击）
- 所有变量值通过 escape_value 转义：
  - 移除控制字符
  - 转义 Markdown 特殊字符：\\ * _ ` [ ] # < > &
  - 转义 HTML 特殊字符（与 Markdown 共用，统一为反斜杠前缀）
"""
from __future__ import annotations

import re
from typing import Any

# 变量名：字母数字下划线。兼容历史模板的 {name} 与 {{name}} 两种写法。
_VAR_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}|\{(\w+)\}")

# 需要转义的特殊字符（含 Markdown 与 HTML）
_ESCAPE_CHARS = ("\\", "*", "_", "`", "[", "]", "#", "<", ">", "&")

# 控制字符：除 \n \t 外的 C0 控制字符
_CTRL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def render_template(template: str, variables: dict[str, Any]) -> str:
    """渲染模板，替换 {variable} 占位符。

    所有变量值通过 escape_value 转义，防止注入。

    Args:
        template: 模板字符串，含 {name} 占位符
        variables: 变量字典

    Returns:
        渲染后的字符串；未匹配的 {name} 占位符原样保留
    """
    if not template:
        return ""

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1) or match.group(2)
        if var_name not in variables:
            # 未提供变量：保留原占位符
            return match.group(0)
        value = variables[var_name]
        return escape_value("" if value is None else str(value))

    return _VAR_PATTERN.sub(replacer, template)


def escape_value(value: str) -> str:
    """转义变量值，防止 Markdown / HTML / Webhook 注入。

    - 移除控制字符（保留 \\n \\t）
    - 转义特殊字符：\\ * _ ` [ ] # < > &
    """
    if not value:
        return ""
    if not isinstance(value, str):
        value = str(value)

    # 移除控制字符（保留 \n \t）
    value = _CTRL_PATTERN.sub("", value)

    # 转义特殊字符：反斜杠本身要先转义
    for char in _ESCAPE_CHARS:
        value = value.replace(char, "\\" + char)

    return value


__all__ = ["render_template", "escape_value"]
