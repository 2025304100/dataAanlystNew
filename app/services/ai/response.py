"""AI 统一响应结构（WP-AI.6 结构化输出与审计）。

定义所有 AI 响应的统一结构：
- answer：文本回答
- evidence：证据列表 [{type, source, content, confidence}]
- warnings：警告列表
- suggested_actions：建议动作 [{action_type, description, draft_id?}]
- draft：可选草稿
- metadata：元信息 {data_as_of, model_version, rule_version, provider_used, latency_ms, tokens}

设计要点：
- 统一结构便于前端解析与审计
- 缺数据时 answer 必须明确说"不知道"，evidence 为空，warnings 提示数据不足
- 永不返回明文 Secret
- to_message_content 输出的 JSON 字符串用于存储到 AIMessage.content

project_memory 硬约束：
- 缺数据时明确说不知道
- 永不返回明文 Secret
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# 缺数据时的标准提示语
NO_DATA_ANSWER_PREFIX = "我不知道"
NO_DATA_ANSWER_FULL = "我不知道：当前无可用数据支持此分析。"
NO_DATA_WARNING = "数据不足，无法给出确切结论"
NO_DATA_SUGGESTED_ACTION = {
    "action_type": "request_more_data",
    "description": "补充数据后再查询",
}


@dataclass
class AIResponse:
    """AI 统一响应结构（WP-AI.6）。

    所有 AI 响应必须构建为此结构，便于：
    1. 前端按统一格式解析（answer/evidence/warnings/suggested_actions/draft）
    2. 后端按统一格式审计（metadata 中记录模型版本与耗时）
    3. 缺数据场景下标准化提示（answer 明确说"不知道"）

    Attributes:
        answer: 文本回答（必填）
        evidence: 证据列表，每项 {type, source, content, confidence}
        warnings: 警告列表（字符串）
        suggested_actions: 建议动作列表，每项 {action_type, description, draft_id?}
        draft: 可选草稿（如待创建的指标/筛选器/提醒等）
        metadata: 元信息 {data_as_of, model_version, rule_version,
                         provider_used, latency_ms, tokens}
    """

    answer: str
    evidence: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggested_actions: list[dict] = field(default_factory=list)
    draft: dict | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转换为 API 响应字典。

        永不返回明文 Secret：调用方在构建 metadata 时需自行确保
        不包含 api_key/secret 等敏感字段。
        """
        return {
            "answer": self.answer,
            "evidence": list(self.evidence),
            "warnings": list(self.warnings),
            "suggested_actions": list(self.suggested_actions),
            "draft": self.draft,
            "metadata": dict(self.metadata),
        }

    def to_message_content(self) -> str:
        """转换为存储到 AIMessage.content 的 JSON 字符串。

        与 to_dict 相同结构，序列化为 ensure_ascii=False 的 JSON，
        便于后续直接读取并解析回 AIResponse。
        """
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "AIResponse":
        """从字典构造 AIResponse（用于从 AIMessage.content 反序列化）。"""
        return cls(
            answer=str(data.get("answer", "")),
            evidence=list(data.get("evidence") or []),
            warnings=list(data.get("warnings") or []),
            suggested_actions=list(data.get("suggested_actions") or []),
            draft=data.get("draft"),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def from_message_content(cls, content: str) -> "AIResponse":
        """从 AIMessage.content 的 JSON 字符串反序列化。

        解析失败时退化为 answer=整个 content 的最小响应。
        """
        if not content:
            return cls(answer="")
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return cls.from_dict(data)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("AIResponse.from_message_content parse failed: %s", exc)
        return cls(answer=content)


def build_response(
    answer: str,
    evidence: list[dict] | None = None,
    warnings: list[str] | None = None,
    suggested_actions: list[dict] | None = None,
    draft: dict | None = None,
    metadata: dict | None = None,
) -> AIResponse:
    """构建统一响应（WP-AI.6）。

    Args:
        answer: 文本回答（必填）
        evidence: 证据列表，每项 {type, source, content, confidence}
        warnings: 警告列表
        suggested_actions: 建议动作列表
        draft: 可选草稿
        metadata: 元信息

    Returns:
        AIResponse 实例
    """
    return AIResponse(
        answer=answer,
        evidence=list(evidence) if evidence else [],
        warnings=list(warnings) if warnings else [],
        suggested_actions=list(suggested_actions) if suggested_actions else [],
        draft=draft,
        metadata=dict(metadata) if metadata else {},
    )


def build_no_data_response(
    reason: str | None = None,
    metadata: dict | None = None,
) -> AIResponse:
    """构建缺数据响应（WP-AI.6 缺数据处理）。

    缺数据时：
    - answer 必须包含"我不知道"或"无可用数据"
    - warnings 添加"数据不足，无法给出确切结论"
    - evidence 为空列表
    - suggested_actions 建议"补充数据后再查询"

    Args:
        reason: 具体缺数据原因（附加到 answer 末尾）
        metadata: 元信息

    Returns:
        AIResponse 缺数据响应
    """
    answer = NO_DATA_ANSWER_FULL
    if reason:
        answer = f"{NO_DATA_ANSWER_FULL} 原因：{reason}"
    return AIResponse(
        answer=answer,
        evidence=[],
        warnings=[NO_DATA_WARNING],
        suggested_actions=[dict(NO_DATA_SUGGESTED_ACTION)],
        draft=None,
        metadata=dict(metadata) if metadata else {},
    )


def parse_llm_response(raw_response: str, context_metadata: dict) -> AIResponse:
    """解析 LLM 原始响应为统一结构（WP-AI.6）。

    优先尝试解析 JSON：
    - 若 JSON 含 answer 字段，按结构化响应解析
    - 若 JSON 不含 answer，将整个 raw_response 作为 answer，JSON 作为 draft
    - JSON 解析失败时，将整个文本作为 answer

    Args:
        raw_response: LLM 返回的原始文本
        context_metadata: 上下文元信息（合并到响应 metadata）

    Returns:
        AIResponse 实例
    """
    base_metadata = dict(context_metadata) if context_metadata else {}

    if not raw_response:
        return build_no_data_response(reason="LLM 返回空响应", metadata=base_metadata)

    # 尝试 JSON 解析
    try:
        data = json.loads(raw_response)
    except (json.JSONDecodeError, TypeError):
        # 纯文本响应
        return build_response(
            answer=raw_response.strip(),
            metadata=base_metadata,
        )

    if not isinstance(data, dict):
        # JSON 但非对象（如数组/字符串）
        return build_response(
            answer=str(data),
            metadata=base_metadata,
        )

    # 结构化 JSON 响应：必须包含 answer
    if "answer" in data:
        response = AIResponse(
            answer=str(data.get("answer", "")),
            evidence=list(data.get("evidence") or []),
            warnings=list(data.get("warnings") or []),
            suggested_actions=list(data.get("suggested_actions") or []),
            draft=data.get("draft"),
            metadata={**base_metadata, **dict(data.get("metadata") or {})},
        )
        # 检测缺数据场景：answer 明确说不知道
        if _is_no_data_answer(response.answer) and not response.warnings:
            response.warnings.append(NO_DATA_WARNING)
        if _is_no_data_answer(response.answer) and not response.suggested_actions:
            response.suggested_actions.append(dict(NO_DATA_SUGGESTED_ACTION))
        return response

    # JSON 但无 answer 字段：将整个 JSON 作为 draft，answer 用 description/summary 兜底
    fallback_answer = (
        str(data.get("description") or data.get("summary") or "AI 已返回结构化数据，请查看 draft 字段")
    )
    return build_response(
        answer=fallback_answer,
        draft=data,
        metadata=base_metadata,
    )


def _is_no_data_answer(answer: str) -> bool:
    """判断 answer 是否表达了"不知道/无数据"语义。"""
    if not answer:
        return True
    text = answer.lower()
    keywords = ("我不知道", "无可用数据", "无数据", "暂无数据", "无法回答", "不知道", "no data", "unknown")
    return any(kw in text for kw in keywords)


__all__ = [
    "AIResponse",
    "NO_DATA_ANSWER_PREFIX",
    "NO_DATA_ANSWER_FULL",
    "NO_DATA_WARNING",
    "NO_DATA_SUGGESTED_ACTION",
    "build_response",
    "build_no_data_response",
    "parse_llm_response",
]
