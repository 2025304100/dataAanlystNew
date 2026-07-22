"""受控上下文包（WP-AI.3）。

按页面构造给 AI 的受控上下文包：
- 用户问题 + 页面来源
- 标的/候选/观察/组合/任务 ID 引用
- 数据截止/来源/可信度/缺失项
- 评分配置/模型版本/因子贡献
- 当前能力门禁/允许下一步
- 相关行情/回测/绩效摘要
- 不可信内容（新闻/第三方文本，标记 is_untrusted）
- 元数据（data_as_of/model_version/rule_version/based_on）

核心约束：
- 上下文包总大小不超过 max_context_tokens（默认 8192）
- 去除所有敏感信息（API Key、Webhook URL、邮箱密码、数据库密码）
- 新闻和第三方文本标记 `is_untrusted: true`
- AI 回复必须附 `data_as_of` / `model_version` / `rule_version` / `based_on`

project_memory 硬约束：
- AI 失败不阻塞任何业务流程（best-effort，异常吞掉返回 None / 默认值）
- 永不返回明文 Secret 到 AI prompt
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── 默认配置 ────────────────────────────────────────────────

# 单个上下文包的最大 token 估算数（与 AIProfile.max_context_tokens 默认值一致）
DEFAULT_MAX_CONTEXT_TOKENS = 8192

# 估算 token 数的简单方法：1 个 token ≈ 3.5 个字符（中英混合保守值）
_CHARS_PER_TOKEN = 3.5

# 不可信来源（新闻/第三方文本）
UNTRUSTED_SOURCES = ("news", "third_party", "rss", "external")


# ── 脱敏正则 ────────────────────────────────────────────────

# API Key 形态：sk-xxx、key-xxx、AKIA-xxx 等
_API_KEY_PATTERN = re.compile(
    r"(?i)\b(sk-[a-zA-Z0-9_\-]{8,}|key-[a-zA-Z0-9_\-]{8,}|AKIA[A-Z0-9]{12,}|"
    r"api[_\-]?key[_\-]?[\w\-]*[=:]\s*[\"\']?[a-zA-Z0-9_\-]{8,})"
)
# Bearer Token
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{16,}")
# Webhook URL（含密钥段，如 /webhook/xxxx、?token=xxxx）
_WEBHOOK_PATTERN = re.compile(
    r"(?i)(https?://[^\s\"']*?(webhook|hook|notify|callback)[^\s\"']*)"
)
# 邮箱密码（password=xxx、pwd=xxx）
_PASSWORD_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|token)[_\-]?[\w]*[=:]\s*[\"\']?[^\s\"\',}]{6,}"
)
# 数据库连接串中的密码（postgres://user:pwd@host、mysql://user:pwd@host）
_DB_URL_PATTERN = re.compile(
    r"(?i)(postgres|mysql|mongodb|redis)://[^\s:@/]+:[^\s@/]+@"
)
# 邮箱地址（仅出现在显式 password 字段附近时考虑，但保守起见对邮箱地址本身不脱敏，
# 只对 password=xxx 形态脱敏）


def _redact_value(value: Any) -> Any:
    """对单个值脱敏：str 走正则替换，dict/list 递归。"""
    if isinstance(value, str):
        v = _API_KEY_PATTERN.sub("[REDACTED_API_KEY]", value)
        v = _BEARER_PATTERN.sub("Bearer [REDACTED]", v)
        v = _WEBHOOK_PATTERN.sub("[REDACTED_WEBHOOK]", v)
        v = _PASSWORD_PATTERN.sub(r"\1=[REDACTED]", v)
        v = _DB_URL_PATTERN.sub(r"\1://[REDACTED_USER]:[REDACTED]@", v)
        return v
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


def _estimate_tokens(obj: Any) -> int:
    """估算对象的 token 数（基于 JSON 序列化后的字符数）。"""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _truncate_to_tokens(obj: Any, max_tokens: int) -> Any:
    """如果对象超出 token 上限，截断列表字段并附加 truncation 标记。"""
    if _estimate_tokens(obj) <= max_tokens:
        return obj
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        budget = max_tokens
        for k, v in obj.items():
            v_tokens = _estimate_tokens(v)
            if v_tokens > budget:
                # 单字段就超预算：尝试截断
                if isinstance(v, list):
                    # 保留前 N 条使总和逼近 budget
                    kept: list[Any] = []
                    running = 0
                    for item in v:
                        item_tokens = _estimate_tokens(item)
                        if running + item_tokens > budget:
                            break
                        kept.append(item)
                        running += item_tokens
                    if len(kept) < len(v):
                        kept.append({"_truncated": True, "omitted": len(v) - len(kept)})
                    result[k] = kept
                else:
                    result[k] = {"_truncated": True, "reason": "exceeds_remaining_budget"}
                budget = 0
            else:
                result[k] = v
                budget -= v_tokens
        result["_size_truncated"] = True
        return result
    if isinstance(obj, list):
        kept_items: list[Any] = []
        running = 0
        for item in obj:
            item_tokens = _estimate_tokens(item)
            if running + item_tokens > max_tokens:
                break
            kept_items.append(item)
            running += item_tokens
        if len(kept_items) < len(obj):
            kept_items.append({"_truncated": True, "omitted": len(obj) - len(kept_items)})
        return kept_items
    return obj


# ── ContextPack 数据类 ──────────────────────────────────────


@dataclass
class ContextPack:
    """受控上下文包。

    所有字段在 build_context_pack 中按 best-effort 填充，缺失数据时为 None / 空集合。
    构造完成后通过 sanitize_context 脱敏，再通过 to_prompt_dict 转换为 AI prompt。
    """

    user_question: str
    source_page: str  # discovery/research/portfolio/backtest/task
    references: dict[str, Any] = field(default_factory=dict)
    data_status: dict[str, Any] = field(default_factory=dict)
    scoring_info: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    market_summary: dict[str, Any] | None = None
    backtest_summary: dict[str, Any] | None = None
    performance_summary: dict[str, Any] | None = None
    untrusted_content: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── 构造函数 ────────────────────────────────────────────────


def _utcnow_naive() -> datetime:
    """当前 UTC 时间（naive，与项目其它模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_call(fn, *args, default=None, **kwargs):
    """best-effort 调用：异常吞掉，返回 default。"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.debug("context_pack safe_call failed: %s: %s", fn.__name__, exc)
        return default


def _build_data_status(db: Session, references: dict[str, Any]) -> dict[str, Any]:
    """数据健康状态：最新数据日期/来源/可信度/缺失项/过期项。"""
    status: dict[str, Any] = {
        "data_cutoff": None,
        "source": "internal",
        "credibility": "high",  # 内部数据默认高可信
        "missing_items": [],
        "stale_items": [],
    }

    # 最新行情日期
    def _latest_bar_date():
        from app.models.daily_bar import DailyBar
        return db.execute(select(func.max(DailyBar.trade_date))).scalar_one_or_none()

    latest = _safe_call(_latest_bar_date, default=None)
    if latest is not None:
        status["data_cutoff"] = latest.isoformat() if hasattr(latest, "isoformat") else str(latest)
    else:
        status["missing_items"].append("daily_bars")

    # 缺失项检查（基于 references 中的 ID 是否能查到对应记录）
    symbol_id = references.get("symbol_id")
    if symbol_id is not None:
        def _has_symbol():
            from app.models.symbol import Symbol
            return db.execute(
                select(func.count(Symbol.id)).where(Symbol.id == symbol_id)
            ).scalar_one_or_none()
        if not _safe_call(_has_symbol, default=0):
            status["missing_items"].append(f"symbol_id={symbol_id}")

    portfolio_id = references.get("portfolio_id")
    if portfolio_id is not None:
        def _has_portfolio():
            from app.models.portfolio import Portfolio
            return db.execute(
                select(func.count(Portfolio.id)).where(Portfolio.id == portfolio_id)
            ).scalar_one_or_none()
        if not _safe_call(_has_portfolio, default=0):
            status["missing_items"].append(f"portfolio_id={portfolio_id}")

    return status


def _build_scoring_info(db: Session, references: dict[str, Any]) -> dict[str, Any]:
    """评分信息：score_config/model_version/factor_contribution。"""
    info: dict[str, Any] = {
        "score_config": None,
        "model_version": None,
        "factor_contribution": None,
    }

    symbol_id = references.get("symbol_id")
    if symbol_id is not None:
        def _latest_score():
            from app.models.score import Score
            return db.execute(
                select(Score)
                .where(Score.symbol_id == symbol_id)
                .order_by(Score.trade_date.desc())
            ).scalars().first()
        score = _safe_call(_latest_score, default=None)
        if score is not None:
            info["score_config"] = {
                "scoring_config_id": score.scoring_config_id,
                "scoring_preset_key": score.scoring_preset_key,
                "scoring_preset_name": score.scoring_preset_name,
                "scoring_config_version": score.scoring_config_version,
                "weight_mode": score.weight_mode,
            }
            info["model_version"] = score.factor_model_run_id
            # 因子贡献（仅维度评分，factor_scores_json 太长不放进上下文）
            info["factor_contribution"] = {
                "quality_score": score.quality_score,
                "timing_score": score.timing_score,
                "priority_score": score.priority_score,
                "trend_score": score.trend_score,
                "momentum_score": score.momentum_score,
                "volatility_score": score.volatility_score,
                "liquidity_score": score.liquidity_score,
                "breadth_score": score.breadth_score,
                "event_score": score.event_score,
                "breakout_score": score.breakout_score,
                "pullback_score": score.pullback_score,
                "overheat_penalty": score.overheat_penalty,
                "data_credibility": score.data_credibility,
            }

    return info


def _build_capabilities(db: Session, source_page: str) -> dict[str, Any]:
    """能力门禁：current_gate/allowed_next_step。"""
    caps: dict[str, Any] = {
        "current_gate": None,
        "allowed_next_step": [],
        "source_page": source_page,
    }

    def _get_overall_status():
        from app.services.capability_gates import get_all_capabilities
        resp = get_all_capabilities(db)
        return resp
    resp = _safe_call(_get_overall_status, default=None)
    if resp is None:
        caps["current_gate"] = "unknown"
        return caps

    caps["current_gate"] = resp.overall_status
    # 根据 source_page 与门禁状态推断 allowed_next_step
    gate_to_actions = {
        "ready": ["draft", "explain", "search"],
        "degraded": ["explain", "search"],  # degraded 时只允许解释/查询，不允许草稿
        "blocked": ["explain"],
        "unknown": ["explain"],
    }
    caps["allowed_next_step"] = gate_to_actions.get(
        resp.overall_status, ["explain"]
    )
    return caps


def _build_market_summary(db: Session, references: dict[str, Any]) -> dict[str, Any] | None:
    """相关行情摘要。"""
    symbol_id = references.get("symbol_id")
    if symbol_id is None:
        return None

    def _latest_bar():
        from app.models.daily_bar import DailyBar
        return db.execute(
            select(DailyBar)
            .where(DailyBar.symbol_id == symbol_id)
            .order_by(DailyBar.trade_date.desc())
        ).scalars().first()
    bar = _safe_call(_latest_bar, default=None)
    if bar is None:
        return None
    return {
        "symbol_id": symbol_id,
        "trade_date": bar.trade_date.isoformat() if bar.trade_date else None,
        "close": float(bar.close) if bar.close is not None else None,
        "open": float(bar.open) if bar.open is not None else None,
        "high": float(bar.high) if bar.high is not None else None,
        "low": float(bar.low) if bar.low is not None else None,
        "volume": float(bar.volume) if bar.volume is not None else None,
        "amount": float(bar.amount) if bar.amount is not None else None,
        "turnover_rate": float(bar.turnover_rate) if bar.turnover_rate is not None else None,
    }


def _build_backtest_summary(db: Session, references: dict[str, Any]) -> dict[str, Any] | None:
    """最近回测摘要。"""
    portfolio_id = references.get("portfolio_id")
    if portfolio_id is None:
        return None

    def _latest_run():
        from app.models.backtest import BacktestRun
        stmt = select(BacktestRun).where(BacktestRun.portfolio_id == portfolio_id)
        if references.get("backtest_id") is not None:
            stmt = stmt.where(BacktestRun.id == references["backtest_id"])
        return stmt.order_by(BacktestRun.id.desc())

    run = _safe_call(lambda: db.execute(_latest_run()).scalars().first(), default=None)
    if run is None:
        return None
    return {
        "backtest_id": run.id,
        "run_name": run.run_name,
        "status": run.status,
        "start_date": run.start_date.isoformat() if run.start_date else None,
        "end_date": run.end_date.isoformat() if run.end_date else None,
        "initial_capital": float(run.initial_capital) if run.initial_capital is not None else None,
        "total_return_pct": float(run.total_return_pct) if run.total_return_pct is not None else None,
        "max_drawdown_pct": float(run.max_drawdown_pct) if run.max_drawdown_pct is not None else None,
        "sharpe_ratio": float(run.sharpe_ratio) if run.sharpe_ratio is not None else None,
        "win_rate": float(run.win_rate) if run.win_rate is not None else None,
        "trade_count": run.trade_count,
        "engine_name": run.engine_name,
        "engine_version": run.engine_version,
    }


def _build_performance_summary(db: Session, references: dict[str, Any]) -> dict[str, Any] | None:
    """组合绩效摘要（best-effort，失败返回 None）。"""
    portfolio_id = references.get("portfolio_id")
    if portfolio_id is None:
        return None

    def _compute():
        from app.services.portfolio_performance import compute_portfolio_performance
        return compute_portfolio_performance(db, portfolio_id)
    perf = _safe_call(_compute, default=None)
    if perf is None or not isinstance(perf, dict):
        return None

    # 只保留摘要字段，避免净值曲线等大对象污染上下文
    return {
        "portfolio_id": perf.get("portfolio_id"),
        "initial_capital": perf.get("initial_capital"),
        "snapshot_count": perf.get("snapshot_count"),
        "date_range": perf.get("date_range"),
        "stats": perf.get("stats"),
        "benchmark_name": perf.get("benchmark_name"),
    }


def _build_untrusted_content(db: Session, references: dict[str, Any]) -> list[dict[str, Any]]:
    """新闻/第三方文本（标记 is_untrusted: true）。"""
    items: list[dict[str, Any]] = []
    symbol_id = references.get("symbol_id")
    if symbol_id is None:
        return items

    def _recent_news():
        from app.models.news_event import NewsEvent
        return db.execute(
            select(NewsEvent)
            .where(NewsEvent.symbol_id == symbol_id)
            .order_by(NewsEvent.published_at.desc().nulls_last())
            .limit(5)
        ).scalars().all()
    news_list = _safe_call(_recent_news, default=[])
    for n in news_list or []:
        items.append({
            "type": "news",
            "source": n.source,
            "title": n.title,
            "url": n.url,
            "event_type": n.event_type,
            "sentiment": n.sentiment,
            "risk_level": n.risk_level,
            "published_at": n.published_at.isoformat() if n.published_at else None,
            "is_untrusted": True,  # 关键：标记不可信
        })
    return items


def _build_metadata(
    data_status: dict[str, Any],
    scoring_info: dict[str, Any],
    references: dict[str, Any],
) -> dict[str, Any]:
    """元数据：data_as_of/model_version/rule_version/based_on_objects。"""
    based_on: list[str] = []
    for k, v in references.items():
        if v is not None:
            based_on.append(f"{k}={v}")

    return {
        "data_as_of": data_status.get("data_cutoff"),
        "model_version": scoring_info.get("model_version"),
        "rule_version": (
            scoring_info.get("score_config", {}) or {}).get("scoring_config_version"),
        "based_on": based_on,
        "built_at": _utcnow_naive().isoformat(),
    }


# ── 公共 API ────────────────────────────────────────────────


def build_context_pack(
    db: Session,
    user_question: str,
    source_page: str,
    references: dict[str, Any] | None = None,
    *,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
) -> ContextPack:
    """构建受控上下文包。

    Args:
        db: 数据库会话
        user_question: 用户问题原文
        source_page: 来源页面 discovery/research/portfolio/backtest/task
        references: 引用对象 ID 字典：
            symbol_id/candidate_id/watchlist_item_id/portfolio_id/task_id/backtest_id
        max_context_tokens: 上下文 token 上限（默认 8192）

    Returns:
        ContextPack（已脱敏，已限制大小）

    所有数据拉取均 best-effort：单个数据源失败不影响整体构建。
    """
    refs = dict(references or {})

    data_status = _safe_call(lambda: _build_data_status(db, refs), default={
        "data_cutoff": None, "source": "internal", "credibility": "high",
        "missing_items": [], "stale_items": [],
    }) or {}
    scoring_info = _safe_call(lambda: _build_scoring_info(db, refs), default={}) or {}
    capabilities = _safe_call(lambda: _build_capabilities(db, source_page), default={
        "current_gate": "unknown", "allowed_next_step": ["explain"], "source_page": source_page,
    }) or {}
    market_summary = _safe_call(lambda: _build_market_summary(db, refs), default=None)
    backtest_summary = _safe_call(lambda: _build_backtest_summary(db, refs), default=None)
    performance_summary = _safe_call(
        lambda: _build_performance_summary(db, refs), default=None
    )
    untrusted_content = _safe_call(lambda: _build_untrusted_content(db, refs), default=[]) or []
    metadata = _build_metadata(data_status, scoring_info, refs)

    pack = ContextPack(
        user_question=user_question,
        source_page=source_page,
        references=refs,
        data_status=data_status,
        scoring_info=scoring_info,
        capabilities=capabilities,
        market_summary=market_summary,
        backtest_summary=backtest_summary,
        performance_summary=performance_summary,
        untrusted_content=untrusted_content,
        metadata=metadata,
    )

    # 脱敏（必须在构造完成后立即执行）
    pack = sanitize_context(pack)
    # 限制大小
    pack = _limit_size(pack, max_context_tokens)
    return pack


def sanitize_context(pack: ContextPack) -> ContextPack:
    """脱敏：去除 API Key/Webhook/邮箱密码/数据库密码等。

    遍历所有字段，对 str/dict/list 中的值应用正则替换。
    不可信内容（untrusted_content）也被脱敏（但其 is_untrusted 标记保留）。
    """
    pack.user_question = _redact_value(pack.user_question)  # type: ignore[assignment]
    pack.references = _redact_value(pack.references)  # type: ignore[assignment]
    pack.data_status = _redact_value(pack.data_status)  # type: ignore[assignment]
    pack.scoring_info = _redact_value(pack.scoring_info)  # type: ignore[assignment]
    pack.capabilities = _redact_value(pack.capabilities)  # type: ignore[assignment]
    if pack.market_summary is not None:
        pack.market_summary = _redact_value(pack.market_summary)
    if pack.backtest_summary is not None:
        pack.backtest_summary = _redact_value(pack.backtest_summary)
    if pack.performance_summary is not None:
        pack.performance_summary = _redact_value(pack.performance_summary)
    # 不可信内容也脱敏，但保留 is_untrusted 标记
    pack.untrusted_content = [
        _redact_value(item) for item in pack.untrusted_content
    ]
    pack.metadata = _redact_value(pack.metadata)  # type: ignore[assignment]
    return pack


def _limit_size(pack: ContextPack, max_tokens: int) -> ContextPack:
    """限制上下文包大小：若超出上限，截断大字段并附加 truncation 标记。"""
    as_dict = asdict(pack)
    if _estimate_tokens(as_dict) <= max_tokens:
        return pack

    # 按优先级降序尝试截断大字段
    # 优先级：untrusted_content > performance_summary > backtest_summary > market_summary
    if pack.untrusted_content:
        # 逐条删除直到达标
        while pack.untrusted_content and _estimate_tokens(asdict(pack)) > max_tokens:
            pack.untrusted_content.pop()
        pack.metadata["_size_truncated"] = True
        pack.metadata["_truncated_fields"] = ["untrusted_content"]

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 截断绩效摘要中的 stats（最可能膨胀的字段）
    if pack.performance_summary and isinstance(pack.performance_summary.get("stats"), dict):
        # 仅保留关键统计
        stats = pack.performance_summary["stats"]
        pack.performance_summary["stats"] = {
            k: stats.get(k) for k in (
                "total_return_pct", "max_drawdown_pct", "sharpe_ratio",
                "win_rate", "trade_count",
            ) if k in stats
        }
        pack.metadata.setdefault("_truncated_fields", []).append("performance_summary.stats")

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 最后兜底：清空 performance_summary
    if pack.performance_summary is not None:
        pack.performance_summary = {"_truncated": True, "reason": "size_limit"}
        pack.metadata.setdefault("_truncated_fields", []).append("performance_summary")

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 继续截断 scoring_info.factor_contribution（保留关键评分维度）
    fc = (pack.scoring_info or {}).get("factor_contribution")
    if isinstance(fc, dict):
        keep_keys = (
            "quality_score", "timing_score", "priority_score", "data_credibility",
        )
        pack.scoring_info["factor_contribution"] = {
            k: fc.get(k) for k in keep_keys if k in fc
        }
        pack.metadata.setdefault("_truncated_fields", []).append(
            "scoring_info.factor_contribution"
        )

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 截断 market_summary（仅保留核心字段）
    if pack.market_summary and isinstance(pack.market_summary, dict):
        keep_ms_keys = ("symbol_id", "trade_date", "close")
        pack.market_summary = {
            k: pack.market_summary.get(k) for k in keep_ms_keys
            if k in pack.market_summary
        }
        pack.metadata.setdefault("_truncated_fields", []).append("market_summary")

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 截断 data_status.missing_items / stale_items（保留 data_cutoff/source/credibility）
    if pack.data_status and isinstance(pack.data_status, dict):
        pack.data_status["missing_items"] = []
        pack.data_status["stale_items"] = []
        pack.metadata.setdefault("_truncated_fields", []).append("data_status.lists")

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 极端兜底：清空 scoring_info 中的非关键字段
    if pack.scoring_info:
        fc = pack.scoring_info.get("factor_contribution") or {}
        pack.scoring_info = {
            "factor_contribution": fc,  # 已截断
            "model_version": pack.scoring_info.get("model_version"),
        }
        pack.metadata.setdefault("_truncated_fields", []).append("scoring_info.score_config")

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 最后兜底：将 _truncated_fields 折叠为计数，减少自身元数据膨胀
    if isinstance(pack.metadata.get("_truncated_fields"), list):
        pack.metadata["_truncated_fields_count"] = len(
            pack.metadata["_truncated_fields"]
        )
        pack.metadata["_truncated_fields"] = ["<collapsed>"]

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 极限兜底：进一步精简元数据与默认值字段
    # 1) built_at 仅保留日期部分
    built_at = pack.metadata.get("built_at")
    if isinstance(built_at, str) and "T" in built_at:
        pack.metadata["built_at"] = built_at.split("T", 1)[0]

    # 2) 移除 data_status 中的常量字段（source/credibility 为默认值，可推断）
    if pack.data_status and isinstance(pack.data_status, dict):
        pack.data_status.pop("source", None)
        pack.data_status.pop("credibility", None)

    # 3) 移除 metadata 中的 None 值字段
    if pack.metadata:
        for k in list(pack.metadata.keys()):
            if pack.metadata.get(k) is None:
                pack.metadata.pop(k, None)

    # 4) 移除 _truncated_fields（只保留 _truncated_fields_count）
    if pack.metadata.get("_truncated_fields_count") is not None:
        pack.metadata.pop("_truncated_fields", None)

    if _estimate_tokens(asdict(pack)) <= max_tokens:
        return pack

    # 最终兜底：精简 capabilities，仅保留 current_gate
    if pack.capabilities and isinstance(pack.capabilities, dict):
        gate = pack.capabilities.get("current_gate")
        pack.capabilities = {"current_gate": gate}

    return pack


def to_prompt_dict(pack: ContextPack) -> dict[str, Any]:
    """转换为 AI prompt 用的字典。

    - 去除 None 字段（market_summary/backtest_summary/performance_summary 为 None 时不出现）
    - 空字典 / 空列表保留（前端/AI 需要明确"无数据"信号）
    - 大小已在 build_context_pack 阶段限制，此处不再二次截断
    """
    result: dict[str, Any] = {
        "user_question": pack.user_question,
        "source_page": pack.source_page,
        "references": pack.references,
        "data_status": pack.data_status,
        "scoring_info": pack.scoring_info,
        "capabilities": pack.capabilities,
        "untrusted_content": pack.untrusted_content,
        "metadata": pack.metadata,
    }
    if pack.market_summary is not None:
        result["market_summary"] = pack.market_summary
    if pack.backtest_summary is not None:
        result["backtest_summary"] = pack.backtest_summary
    if pack.performance_summary is not None:
        result["performance_summary"] = pack.performance_summary
    return result


def has_sensitive_info(pack: ContextPack) -> bool:
    """检测上下文包中是否仍残留敏感信息（用于自检）。

    返回 True 表示仍含敏感信息（脱敏未完成）；False 表示已清洁。
    """
    text = json.dumps(asdict(pack), ensure_ascii=False, default=str)
    for pattern in (
        _API_KEY_PATTERN, _BEARER_PATTERN, _WEBHOOK_PATTERN,
        _PASSWORD_PATTERN, _DB_URL_PATTERN,
    ):
        if pattern.search(text):
            return True
    return False


__all__ = [
    "ContextPack",
    "DEFAULT_MAX_CONTEXT_TOKENS",
    "UNTRUSTED_SOURCES",
    "build_context_pack",
    "sanitize_context",
    "to_prompt_dict",
    "has_sensitive_info",
]
