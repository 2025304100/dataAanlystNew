"""统一用户错误协议（WP-S.6）。

目标：所有 API 异常返回结构化的可理解错误，普通用户看到中文文案 + 影响 + 下一步，
技术详情折叠在 `technical_details`（前端默认隐藏）。

设计原则：
- 同一 `error_code` 在所有页面使用一致中文文案（来自 `ERROR_CODE_LIBRARY`）
- 普通用户默认只看 `user_message`/`impact`/`next_actions`
- 技术堆栈、HTTP 响应、SQL 错误折叠在 `technical_details`
- 禁止裸露 `Request failed`、`HTTP 500`、`NoneType`、数据库锁或第三方原始 HTML
- 后台日志不得包含 API Key、Webhook、SMTP 密码或完整用户数据（脱敏由 `error_sanitizer` 负责）

模块结构：
- `NextAction`：下一步建议操作
- `TechnicalDetails`：技术详情（前端折叠）
- `UserError`：统一错误响应主体
- `ERROR_CODE_LIBRARY`：预置错误码字典
- `build_user_error()`：根据 error_code 构造 UserError 的工厂函数
- `UnifiedErrorException`：可被 raise 的异常，由全局异常处理器捕获
"""
from __future__ import annotations

import logging
from typing import Any, Literal
import typing
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── 子模型 ────────────────────────────────────────────


class NextAction(BaseModel):
    """下一步建议操作。

    前端渲染为按钮，用户点击后执行 `action_type` 对应动作。
    """

    label: str
    action_type: Literal["retry", "redirect", "configure", "dismiss", "view_details", "sync"]
    target: str | None = None
    reason: str | None = None


class TechnicalDetails(BaseModel):
    """技术详情（默认前端折叠，仅高级用户/开发者展开）。

    所有字符串字段必须先经过 `app.schemas.error_sanitizer.sanitize_message` 脱敏。
    """

    exception_type: str | None = None
    status_code: int | None = None
    error_message: str | None = None
    stack_summary: str | None = None
    db_error_code: str | None = None
    upstream_response: str | None = None
    request_id: str | None = None


class UserError(BaseModel):
    """统一用户错误协议（WP-S.6）。

    所有 API 异常返回此结构，普通用户默认只看 `user_message`/`impact`/`next_actions`，
    `technical_details` 折叠在前端"技术详情"中（默认隐藏）。
    """

    error_code: str
    user_message: str
    impact: str
    retryable: bool
    completed: float = 0.0
    next_actions: list[NextAction] = Field(default_factory=list)
    technical_details: TechnicalDetails | None = None
    correlation_id: str
    # P0-AutoTrade：当 HTTPException.detail 为 dict 时，透传的扩展字段（如 blockers、readiness）。
    extras: dict[str, typing.Any] | None = None


# ── 错误码字典 ────────────────────────────────────────

ERROR_CODE_LIBRARY: dict[str, dict[str, Any]] = {
    "DATA_SYNC_TIMEOUT": {
        "user_message": "数据同步超时，部分标的未完成",
        "impact": "已同步数据可正常使用，但本轮新增数据可能不完整",
        "retryable": True,
        "next_actions": [
            {"label": "重试同步", "action_type": "retry", "reason": "继续处理未完成的标的"}
        ],
    },
    "CIRCUIT_BREAKER_OPEN": {
        "user_message": "外部接口暂时不可用",
        "impact": "正在使用本地缓存数据，可能不是最新",
        "retryable": True,
        "next_actions": [{"label": "稍后重试", "action_type": "retry"}],
    },
    "DATA_VALIDATION_FAILED": {
        "user_message": "数据格式不符合预期",
        "impact": "本条数据已跳过，其他数据继续处理",
        "retryable": False,
    },
    "DB_CONNECTION_FAILED": {
        "user_message": "数据库暂时不可用",
        "impact": "本次操作未完成",
        "retryable": True,
        "next_actions": [{"label": "重试", "action_type": "retry"}],
    },
    "DB_LOCK_TIMEOUT": {
        "user_message": "数据库繁忙，请稍后再试",
        "impact": "本次操作未完成",
        "retryable": True,
    },
    "DB_INTEGRITY_VIOLATION": {
        "user_message": "数据冲突，已存在相同记录",
        "impact": "本次写入未完成，请检查是否重复提交",
        "retryable": False,
    },
    "UNAUTHORIZED": {
        "user_message": "未授权或会话已过期",
        "impact": "需要重新登录或配置授权",
        "retryable": False,
        "next_actions": [
            {"label": "去配置", "action_type": "redirect", "target": "/settings"}
        ],
    },
    "NOT_FOUND": {
        "user_message": "请求的资源不存在",
        "impact": "请检查输入或返回列表查看",
        "retryable": False,
        "next_actions": [
            {"label": "返回观察池", "action_type": "dismiss", "reason": "关闭错误提示，返回观察池列表"},
        ],
    },
    "DATA_NOT_READY": {
        "user_message": "数据未准备好，暂时无法加载观察池",
        "impact": "请先完成基础数据同步或运行增量同步",
        "retryable": True,
        "next_actions": [
            {"label": "前往基础数据", "action_type": "redirect", "target": "macro", "reason": "完成基础数据同步"},
            {"label": "运行增量同步", "action_type": "sync", "reason": "同步最新行情数据"},
            {"label": "数据就绪后重试", "action_type": "retry", "reason": "同步完成后重新加载观察池"},
        ],
    },
    "VALIDATION_ERROR": {
        "user_message": "输入参数有误",
        "impact": "请修改后重试",
        "retryable": False,
    },
    "UNKNOWN_ERROR": {
        "user_message": "服务暂时不可用，请稍后重试",
        "impact": "本次操作未完成",
        "retryable": True,
        "next_actions": [{"label": "重试", "action_type": "retry"}],
    },
    "BUSINESS_BLOCKED": {
        "user_message": "当前业务条件未满足，操作被阻止",
        "impact": "本次操作未执行，请按下方提示修复后重试",
        "retryable": True,
        "next_actions": [{"label": "查看 readiness 接口获取详细阻塞原因", "action_type": "dismiss"}],
    },
    "STALE_DATA": {
        "user_message": "本地数据已过期",
        "impact": "正在使用过期数据，结果可能不准确",
        "retryable": True,
    },
    "RATE_LIMITED": {
        "user_message": "请求过于频繁",
        "impact": "已触发上游限流，请稍候",
        "retryable": True,
    },
    "TASK_INTERRUPTED": {
        "user_message": "后台任务被中断",
        "impact": "已完成的部分仍保留，未完成的可恢复",
        "retryable": True,
        "next_actions": [{"label": "恢复任务", "action_type": "retry"}],
    },
    "TASK_STALLED": {
        "user_message": "后台任务在某阶段卡住",
        "impact": "可能因数据量过大或外部接口慢",
        "retryable": True,
        "next_actions": [
            {"label": "恢复", "action_type": "retry"},
            {"label": "取消任务", "action_type": "dismiss"},
        ],
    },
    "PORTFOLIO_RULE_INVALID": {
        "user_message": "组合规则配置不完整",
        "impact": "无法执行自动交易或回测",
        "retryable": False,
        "next_actions": [
            {
                "label": "去配置规则",
                "action_type": "redirect",
                "target": "/portfolio/rules",
            }
        ],
    },
    "AI_CONFIG_MISSING": {
        "user_message": "AI 助手未配置",
        "impact": "AI 功能不可用，其他功能正常",
        "retryable": False,
        "next_actions": [
            {"label": "去配置 AI", "action_type": "redirect", "target": "/settings/ai"}
        ],
    },
    "CAPABILITY_BLOCKED": {
        "user_message": "功能前置条件未满足，当前操作已被阻断",
        "impact": "扫描无法执行，请先完成数据准备",
        "retryable": True,
        "next_actions": [
            {"label": "去基础数据", "action_type": "redirect", "target": "macro"},
            {"label": "运行增量同步", "action_type": "sync", "reason": "同步最新行情数据"},
            {"label": "数据就绪后自动扫描", "action_type": "retry", "reason": "启动数据准备任务，完成后自动扫描"},
        ],
    },
    # ── Factor Center Chain Closure (Task 4 / spec 20260902) P0 gates ─
    "FACTOR_SET_EMPTY": {
        "user_message": "因子集合为空（0 个成员），冻结和训练已被门禁阻断",
        "impact": "本次冻结/训练未执行；空集合无法生成任何有效因子模型候选",
        "retryable": True,
        "fix_link": "/settings/factor-center?open=members",
        "next_actions": [
            {"label": "打开编辑成员抽屉", "action_type": "redirect",
             "target": "/settings/factor-center?open=members"},
        ],
    },
    "FACTOR_SET_NO_FEATURE": {
        "user_message": "因子集合没有 feature 角色成员（所有成员都是 control）",
        "impact": "训练缺少可解释特征，生成模型无区分度；冻结/训练被门禁阻断",
        "retryable": True,
        "fix_link": "/settings/factor-center?open=members",
        "next_actions": [
            {"label": "至少把 1 个成员角色改为 feature", "action_type": "dismiss"},
        ],
    },
    "FACTOR_SET_MEMBER_VERSION_NOT_TRAINABLE": {
        "user_message": "至少有 1 个因子成员版本不可训练（状态=草稿/废弃/禁用）",
        "impact": "冻结后集合版本不合法；训练若继续会把非可训练版本写进快照溯源链",
        "retryable": True,
        "fix_link": "/settings/factor-center?tab=versions",
        "next_actions": [
            {"label": "升级到可训练版本后重试", "action_type": "retry"},
        ],
    },
    "FACTOR_SET_NOT_FROZEN": {
        "user_message": "因子集合未冻结（status=draft），训练门禁未通过",
        "impact": "不冻结直接训练无法生成内容签名 content_hash，模型溯源链缺失",
        "retryable": True,
        "fix_link": "/settings/factor-center?action=freeze",
    },
    "FACTOR_SET_NOT_FOUND": {
        "user_message": "因子集合不存在",
        "impact": "本次操作未执行",
        "retryable": False,
        "fix_link": "/settings/factor-center",
    },
    "FACTOR_SET_DEPRECATED": {
        "user_message": "因子集合已废弃，禁止冻结/训练",
        "impact": "废弃集合仅用于历史回溯；请复制生成新集合",
        "retryable": False,
        "fix_link": "/settings/factor-center",
    },
    "TRAIN_GATE_COVERAGE_LOW": {
        "user_message": "训练门禁失败：集合整体覆盖率低于 0.70 阈值",
        "impact": "低覆盖率导致训练样本偏置严重，验证集 IC 大概率漂",
        "retryable": True,
        "fix_link": "/settings/factor-center#gates",
    },
    "TRAIN_GATE_IC_OUT_OF_RANGE": {
        "user_message": "训练门禁失败：集合特征 IC 不在 [0.01, 0.10] 区间",
        "impact": "IC 过小无预测力；IC 过大接近标签泄漏风险",
        "retryable": True,
        "fix_link": "/settings/factor-center#gates",
    },
    "TRAIN_GATE_LOOKBACK_SHORT": {
        "user_message": "训练门禁失败：可用 lookback 小于 30 天",
        "impact": "样本量不足，模型参数方差过大，流水线训练会不稳定",
        "retryable": True,
        "fix_link": "/settings/factor-center#gates",
    },
    "PIPELINE_FACTOR_SET_NOT_READY": {
        "user_message": "流水线使用的因子集合 readiness 未通过（版本不可训练/content hash 空等兜底）",
        "impact": "流水线创建被阻断；不会创建训练/评分任务",
        "retryable": True,
        "fix_link": "/settings/pipeline",
    },
    "PIPELINE_NO_FACTOR_SET": {
        "user_message": "流水线未指定因子集合",
        "impact": "流水线创建被阻断；训练模式需要显式选择一个已冻结因子集合",
        "retryable": False,
        "fix_link": "/settings/factors",
    },
    "PIPELINE_FACTOR_SET_NOT_FOUND": {
        "user_message": "指定的因子集合不存在（DB 查无此行）",
        "impact": "流水线创建被阻断；请检查集合 ID 或前往因子中心重新选择",
        "retryable": False,
        "fix_link": "/settings/factor-center",
    },
    "PIPELINE_FACTOR_SET_NOT_FROZEN": {
        "user_message": "因子集合未冻结（status=draft/deprecated 或 frozen_at IS NULL）",
        "impact": "流水线创建被阻断；训练需要使用已冻结集合保证内容签名与溯源链完整",
        "retryable": True,
        "fix_link": "/settings/factor-center?action=freeze",
    },
    "PIPELINE_FACTOR_SET_EMPTY": {
        "user_message": "因子集合为空（成员数 == 0）",
        "impact": "流水线创建被阻断；空集合无法生成任何有效特征，不会写入新模型",
        "retryable": True,
        "fix_link": "/settings/factor-center?open=members",
    },
    "PIPELINE_FACTOR_SET_NO_FEATURE_FACTORS": {
        "user_message": "因子集合没有 feature 角色成员（所有成员 role != feature）",
        "impact": "流水线创建被阻断；训练缺少可解释特征，生成模型无区分度",
        "retryable": True,
        "fix_link": "/settings/factor-center?open=members",
    },
    "PIPELINE_INCONSISTENT_FACTOR_SET": {
        "user_message": "三处 factor_set_id 写入不一致（payload / hyperparameters / factor_scope）",
        "impact": "流水线创建被阻断；同一请求中因子集合 ID 必须完全相同，避免溯源链断裂",
        "retryable": False,
        "fix_link": "/settings/pipeline",
    },
    # ── 因子挖掘（Factor Mining，M1a）─────────────────────────────────
    "MINING_DOMAIN_BUSY": {
        "user_message": "已有挖掘任务进行中",
        "impact": "本次提交被拒绝，未创建任务（挖掘域同一时间只允许一个任务，不排队）",
        "retryable": True,
        "fix_link": "/settings/factor-mining",
    },
    "MINING_QUEUE_FULL": {
        "user_message": "写队列异常，暂时无法排队",
        "impact": "本次提交未创建任务；DuckDB 写任务队列状态异常，请稍后重试或联系管理员",
        "retryable": True,
        "next_actions": [{"label": "查看锁状态", "action_type": "redirect",
                          "target": "/settings/factor-mining?tab=locks"}],
    },
    "MINING_SAMPLE_INSUFFICIENT": {
        "user_message": "有效调仓点不足，无法完成三段切分",
        "impact": "本次挖掘被阻断，未创建任务；样本量达不到统计要求，强行运行会产出不可信结论",
        "retryable": True,
        "fix_link": "/settings/factor-mining?step=2",
        "next_actions": [
            {"label": "调整时间范围或调仓频率", "action_type": "redirect",
             "target": "/settings/factor-mining?step=2"},
            {"label": "去镜像 10 年数据", "action_type": "redirect",
             "target": "/settings/data-center"},
        ],
    },
    "MINING_POOL_TOO_SMALL": {
        "user_message": "候选池有效标的少于 50，无法启动挖掘",
        "impact": "本次挖掘被阻断；50 是系统硬下限，不可通过前端参数降低",
        "retryable": True,
        "fix_link": "/settings/factor-mining?step=1",
    },
    "MINING_VALIDATION_EXPIRED": {
        "user_message": "字段校验结果已过期（超过 24 小时）",
        "impact": "本次提交被阻断；需重新执行字段校验后才能启动挖掘",
        "retryable": True,
        "fix_link": "/settings/factor-mining?step=3",
    },
    "MINING_AI_BUDGET_EXCEEDED": {
        "user_message": "AI 生成已达次数/成本上限，剩余名额已回退为随机生成",
        "impact": "本次挖掘不受影响（属预期降级行为）；AI 实际入库占比可能低于配置值",
        "retryable": False,
    },
    "MINING_TEST_LOCKED": {
        "user_message": "该挖掘任务的 test 段已评估过，不可回炉重挖",
        "impact": "本次操作被拒绝；test 段全程锁定、仅最终确认时评估一次，这是防过拟合的硬约束",
        "retryable": False,
        "fix_link": "/settings/factor-mining",
    },
    "MINING_SNAPSHOT_NOT_FOUND": {
        "user_message": "候选池快照不存在或已失效",
        "impact": "本次提交被拒绝，未创建挖掘任务；请回到 Step1 重新生成挖掘物料后再提交",
        "retryable": False,
        "fix_link": "/settings/factor-mining?step=1",
    },
}


# ── BFG 项目统一 7 要素结构化错误 (FR-13 / Task 4 item 5) ────────────
# 字段：error_code / title_zh / detail_zh / correlation_id / impact / fix_link / retryable


class FactorSevenError(ValueError):
    """所有 factor-center 写操作抛此异常 → 全局异常处理器 / 路由捕获后转 HTTP 400。"""

    def __init__(
        self,
        error_code: str,
        *,
        title_zh: str | None = None,
        detail_zh: str | None = None,
        correlation_id: str | None = None,
        impact: str | None = None,
        fix_link: str | None = None,
        retryable: bool | None = None,
        extras: dict[str, Any] | None = None,
    ) -> None:
        tpl = ERROR_CODE_LIBRARY.get(error_code) or ERROR_CODE_LIBRARY.get(
            "UNKNOWN_ERROR"
        )
        self.error_code = error_code
        self.title_zh = title_zh or tpl.get("user_message") or error_code
        self.detail_zh = detail_zh or tpl.get("user_message") or ""
        self.impact = impact if impact is not None else tpl.get("impact", "")
        self.fix_link = (
            fix_link if fix_link is not None else tpl.get("fix_link", "")
        )
        self.retryable = (
            retryable if retryable is not None else bool(tpl.get("retryable", False))
        )
        self.correlation_id = correlation_id or uuid4().hex
        self.extras = dict(extras or {})
        super().__init__(f"[{error_code}] {self.detail_zh}")

    def to_7field(self) -> dict[str, Any]:
        """严格按 BFG 7 要素返回字典（不含 extras 等扩展字段；调试放 extras）。"""
        return {
            "error_code": self.error_code,
            "title_zh": self.title_zh,
            "detail_zh": self.detail_zh,
            "correlation_id": self.correlation_id,
            "impact": self.impact,
            "fix_link": self.fix_link,
            "retryable": self.retryable,
        }

    def to_dict(self) -> dict[str, Any]:
        """HTTP JSON body：7 要素 + extras（extras 为调试明细，前端可折叠展示）。"""
        out = self.to_7field()
        if self.extras:
            out["extras"] = self.extras
        return out


def factor_structured_error(
    code: str,
    *,
    title_zh: str | None = None,
    detail_zh: str | None = None,
    impact: str | None = None,
    fix_link: str | None = None,
    retryable: bool | None = None,
    correlation_id: str | None = None,
    extras: dict[str, Any] | None = None,
) -> FactorSevenError:
    """快速构建 7 要素 factor 结构化错误。函数形式便于链式调用。"""
    return FactorSevenError(
        code,
        title_zh=title_zh,
        detail_zh=detail_zh,
        correlation_id=correlation_id,
        impact=impact,
        fix_link=fix_link,
        retryable=retryable,
        extras=extras,
    )


# ── 工厂函数 ──────────────────────────────────────────


def build_user_error(
    error_code: str,
    *,
    correlation_id: str | None = None,
    completed: float = 0.0,
    technical_details: TechnicalDetails | None = None,
    override_user_message: str | None = None,
    override_impact: str | None = None,
    extra_next_actions: list[NextAction] | None = None,
    extras: dict[str, typing.Any] | None = None,
) -> UserError:
    """根据 `error_code` 从字典构造 `UserError`，允许覆盖部分字段。

    不存在的 `error_code` 默认降级到 `UNKNOWN_ERROR`（不抛 `KeyError`），
    保证全局异常处理器不会因字典缺失而二次崩溃。

    Args:
        error_code: 稳定错误码（如 "DATA_SYNC_TIMEOUT"）
        correlation_id: 调用链追踪 ID；None 时自动生成 uuid4.hex
        completed: 已完成比例（0.0~1.0），用于部分完成场景
        technical_details: 折叠的技术详情；通常包含脱敏后的异常消息
        override_user_message: 覆盖默认中文文案（仅在业务确有定制需要时使用）
        override_impact: 覆盖默认影响说明
        extra_next_actions: 追加到默认 `next_actions` 之后的额外操作
        extras: 透传扩展字典（readiness/blockers 等业务字段）。

    Returns:
        构造好的 `UserError` 实例
    """
    template = ERROR_CODE_LIBRARY.get(error_code)
    if template is None:
        # 降级到 UNKNOWN_ERROR，避免字典缺失导致二次崩溃
        logger.warning(
            "Unknown error_code '%s' fallback to UNKNOWN_ERROR", error_code,
        )
        template = ERROR_CODE_LIBRARY["UNKNOWN_ERROR"]
        # 保留原始 error_code 用于业务方排查，但文案使用 UNKNOWN_ERROR
        # 这里仍然使用 template 的文案，error_code 字段单独保留
        user_message = template["user_message"]
        impact = template["impact"]
        retryable = template["retryable"]
        next_actions_data = list(template.get("next_actions", []))
    else:
        user_message = template["user_message"]
        impact = template["impact"]
        retryable = template["retryable"]
        next_actions_data = list(template.get("next_actions", []))

    if override_user_message is not None:
        user_message = override_user_message
    if override_impact is not None:
        impact = override_impact

    # 合并默认 next_actions 与 extra_next_actions
    next_actions: list[NextAction] = [NextAction(**na) for na in next_actions_data]
    if extra_next_actions:
        next_actions.extend(extra_next_actions)

    # 限制 completed 在 [0.0, 1.0]
    try:
        completed_clamped = max(0.0, min(1.0, float(completed)))
    except (TypeError, ValueError):
        completed_clamped = 0.0

    return UserError(
        error_code=error_code,
        user_message=user_message,
        impact=impact,
        retryable=retryable,
        completed=completed_clamped,
        next_actions=next_actions,
        technical_details=technical_details,
        correlation_id=correlation_id or uuid4().hex,
        extras=extras,
    )


# ── 可被 raise 的异常 ─────────────────────────────────


class UnifiedErrorException(Exception):
    """可被 raise 的统一错误异常，由全局异常处理器捕获并转为 `UserError` 响应。

    业务方在需要返回结构化错误时使用：

        raise UnifiedErrorException(
            "DATA_SYNC_TIMEOUT",
            status_code=503,
            completed=0.5,
            technical_details=TechnicalDetails(
                exception_type="TimeoutError",
                error_message=sanitize_message(str(exc)),
            ),
        )

    全局异常处理器 `unified_error_handler` 会捕获此异常并返回
    `JSONResponse(status_code=exc.status_code, content=user_error.model_dump(mode="json"))`。
    """

    def __init__(
        self,
        error_code: str,
        *,
        status_code: int = 500,
        completed: float = 0.0,
        technical_details: TechnicalDetails | None = None,
        override_user_message: str | None = None,
        override_impact: str | None = None,
        extra_next_actions: list[NextAction] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code
        self.completed = completed
        self.technical_details = technical_details
        self.override_user_message = override_user_message
        self.override_impact = override_impact
        self.extra_next_actions = extra_next_actions
        self.correlation_id = correlation_id or uuid4().hex


__all__ = [
    "UserError",
    "NextAction",
    "TechnicalDetails",
    "ERROR_CODE_LIBRARY",
    "build_user_error",
    "UnifiedErrorException",
    "FactorSevenError",
    "factor_structured_error",
]
