"""AI 量化助手服务包（WP-AI.3 / WP-AI.4 / WP-AI.5）。

子模块：
- context_pack：受控上下文包（WP-AI.3）
- tools：第一阶段只读工具集（WP-AI.4）
- drafts：第二阶段草稿工具（WP-AI.5）

设计约束（贯穿三个子模块）：
- AI 失败不阻塞业务流程（best-effort，异常吞掉返回 None / 默认值）
- 受控上下文：去除 API Key / Webhook / 邮箱密码等敏感信息
- 三步确认流程：AI 建议 → 系统规则校验和变更预览 → 用户确认后由业务 API 执行
- 自动交易永远由策略规则和调度器负责，不由对话直接触发
"""

__all__: list[str] = []
