"""P2-E：第三方 akshare 接口管理 API。

提供：
1. GET /external-data/apis - 列出所有接口的元数据 + 用户配置 + 运行时状态
2. GET /external-data/apis/strategies - 列出所有防风控策略档位
3. POST /external-data/apis/{key}/probe - 探测单个接口（实际调用一次，记录延迟/成功/错误）
4. PUT /external-data/apis/{key} - 更新单个接口的配置（enabled/strategy/delay_min/max_ms）

接口元数据（名称/分类/默认档位）放代码 registry 中不可修改；
用户可配置的只有 enabled、anti_risk_strategy、delay_min/max_ms（custom 时）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import akshare as ak
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.akshare_api_config import AkshareApiConfig
from app.services.akshare_registry import (
    AKSHARE_API_REGISTRY,
    ANTI_RISK_STRATEGIES,
    get_registry_entry,
    record_probe_result,
    refresh_config_cache_for,
)
from app.services.akshare_utils import quiet_akshare_output
from app.services.market_data import _proxy_bypass

logger = logging.getLogger(__name__)

router = APIRouter()


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------

class ApiStatus(BaseModel):
    """单个接口的完整视图（元数据 + 配置 + 状态）。"""
    # 元数据（不可修改）
    key: str
    name: str
    category: str
    module: str
    description: str
    default_strategy: str
    # 用户配置（可修改）
    enabled: bool
    anti_risk_strategy: str
    delay_min_ms: int
    delay_max_ms: int
    # 运行时状态（只读）
    last_probe_at: datetime | None = None
    last_probe_success: bool | None = None
    last_probe_latency_ms: int | None = None
    last_probe_error: str | None = None
    last_call_at: datetime | None = None
    last_call_success: bool | None = None
    last_call_error: str | None = None
    total_calls: int = 0
    total_failures: int = 0


class StrategyInfo(BaseModel):
    key: str
    name: str
    delay_min_ms: int | None
    delay_max_ms: int | None
    max_retries: int
    desc: str


class ApiConfigUpdate(BaseModel):
    """接口配置更新 payload（全部可选，仅这几个字段可改）。"""
    enabled: bool | None = None
    anti_risk_strategy: str | None = Field(None, description="fast/standard/conservative/extreme/custom")
    delay_min_ms: int | None = Field(None, ge=0, le=60000, description="自定义延时下限（ms），仅 custom 生效")
    delay_max_ms: int | None = Field(None, ge=0, le=60000, description="自定义延时上限（ms），仅 custom 生效")


class ProbeResult(BaseModel):
    key: str
    success: bool
    latency_ms: int | None = None
    error: str | None = None


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _to_status(entry: dict[str, Any], cfg: AkshareApiConfig | None, locale: str = "zh-CN") -> ApiStatus:
    is_zh = locale == "zh-CN"
    return ApiStatus(
        key=entry["key"],
        name=entry["name_zh"] if is_zh else entry["name_en"],
        category=entry["category_zh"] if is_zh else entry["category_en"],
        module=entry["module"],
        description=entry["desc_zh"] if is_zh else entry["desc_en"],
        default_strategy=entry["default_strategy"],
        enabled=bool(cfg.enabled) if cfg else True,
        anti_risk_strategy=cfg.anti_risk_strategy if cfg else entry["default_strategy"],
        delay_min_ms=cfg.delay_min_ms if cfg else 300,
        delay_max_ms=cfg.delay_max_ms if cfg else 800,
        last_probe_at=cfg.last_probe_at if cfg else None,
        last_probe_success=cfg.last_probe_success if cfg else None,
        last_probe_latency_ms=cfg.last_probe_latency_ms if cfg else None,
        last_probe_error=cfg.last_probe_error if cfg else None,
        last_call_at=cfg.last_call_at if cfg else None,
        last_call_success=cfg.last_call_success if cfg else None,
        last_call_error=cfg.last_call_error if cfg else None,
        total_calls=cfg.total_calls if cfg else 0,
        total_failures=cfg.total_failures if cfg else 0,
    )


def _get_cfg_row(db: Session, api_key: str) -> AkshareApiConfig | None:
    return db.execute(
        select(AkshareApiConfig).where(AkshareApiConfig.api_key == api_key)
    ).scalars().first()


def _ensure_cfg_row(db: Session, api_key: str) -> AkshareApiConfig:
    """获取或创建配置行（用 registry 默认档位）。"""
    row = _get_cfg_row(db, api_key)
    if row is None:
        entry = get_registry_entry(api_key)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown api_key: {api_key}")
        row = AkshareApiConfig(
            api_key=api_key,
            enabled=True,
            anti_risk_strategy=entry["default_strategy"],
            delay_min_ms=300,
            delay_max_ms=800,
        )
        db.add(row)
        db.flush()
    return row


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@router.get("/external-data/apis", response_model=list[ApiStatus])
def list_apis(locale: str = "zh-CN", db: Session = Depends(get_db)):
    """列出所有第三方接口的元数据 + 配置 + 运行时状态。"""
    rows = {r.api_key: r for r in db.execute(select(AkshareApiConfig)).scalars().all()}
    return [_to_status(entry, rows.get(entry["key"]), locale) for entry in AKSHARE_API_REGISTRY]


@router.get("/external-data/apis/strategies", response_model=list[StrategyInfo])
def list_strategies(locale: str = "zh-CN"):
    """列出所有防风控策略档位。"""
    is_zh = locale == "zh-CN"
    return [
        StrategyInfo(
            key=s["key"],
            name=s["name_zh"] if is_zh else s["name_en"],
            delay_min_ms=s["delay_min_ms"],
            delay_max_ms=s["delay_max_ms"],
            max_retries=s["max_retries"],
            desc=s["desc_zh"] if is_zh else s["desc_en"],
        )
        for s in ANTI_RISK_STRATEGIES.values()
    ]


# 探测端点总体超时（秒）
# 覆盖单次 akshare 调用（连接 5s + 读取 15s = 最多 20s）+ 余量
# 即使 akshare 内部永久阻塞，asyncio.wait_for 也会在 30s 后强制返回超时
_PROBE_TIMEOUT_SECONDS = 30.0

# P0 稳定性：探测专用独立线程池
# 避免探测超时后泄漏的子线程耗尽 FastAPI 默认线程池（anyio 默认 40 线程），
# 导致其他异步路由的 asyncio.to_thread 调用排队无响应。
# 独立池限制最大泄漏数为 8，且不阻塞其他异步路由。
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="akshare-probe")


def _run_probe(func: Any, probe_args: dict[str, Any]) -> Any:
    """在线程中执行同步 akshare 调用（供 asyncio.to_thread 包装）。"""
    with _proxy_bypass(), quiet_akshare_output():
        return func(**probe_args)


@router.post("/external-data/apis/{api_key}/probe", response_model=ProbeResult)
async def probe_api(api_key: str, db: Session = Depends(get_db)):
    """探测单个接口：用 probe_args 实际调用一次，记录延迟/成功/错误。

    使用 asyncio.wait_for + asyncio.to_thread 包装同步 akshare 调用，加 30s 总体超时，
    避免数据源接受连接不响应时永久卡死耗尽 FastAPI 线程池。
    """
    entry = get_registry_entry(api_key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown api_key: {api_key}")

    func = getattr(ak, api_key, None)
    if func is None:
        raise HTTPException(status_code=500, detail=f"akshare has no attribute: {api_key}")

    probe_args = entry.get("probe_args", {}) or {}
    start = time.time()
    logger.info("probe %s start", api_key)
    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(_PROBE_EXECUTOR, _run_probe, func, probe_args),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        latency_ms = int((time.time() - start) * 1000)
        # 简单校验返回值：DataFrame 非空 / list 非空 / dict 有内容
        success = True
        if hasattr(result, "empty"):
            success = not result.empty
        elif isinstance(result, (list, tuple)):
            success = len(result) > 0
        record_probe_result(db, api_key, success, latency_ms, None if success else "Empty result")
        db.commit()
        logger.info("probe %s done: success=%s latency=%dms", api_key, success, latency_ms)
        return ProbeResult(key=api_key, success=success, latency_ms=latency_ms, error=None if success else "Empty result")
    except asyncio.TimeoutError:
        # 探测超时：记录失败，返回明确错误
        latency_ms = int((time.time() - start) * 1000)
        err_msg = f"probe timeout ({_PROBE_TIMEOUT_SECONDS:.0f}s)"
        record_probe_result(db, api_key, False, latency_ms, err_msg)
        db.commit()
        logger.warning("probe %s TIMEOUT after %ds", api_key, _PROBE_TIMEOUT_SECONDS)
        return ProbeResult(key=api_key, success=False, latency_ms=latency_ms, error=err_msg)
    except Exception as exc:
        latency_ms = int((time.time() - start) * 1000)
        err_msg = f"{type(exc).__name__}: {exc}"
        record_probe_result(db, api_key, False, latency_ms, err_msg)
        db.commit()
        logger.warning("probe %s EXC: %s", api_key, exc, exc_info=True)
        return ProbeResult(key=api_key, success=False, latency_ms=latency_ms, error=err_msg)


@router.put("/external-data/apis/{api_key}", response_model=ApiStatus)
def update_api_config(api_key: str, payload: ApiConfigUpdate, locale: str = "zh-CN", db: Session = Depends(get_db)):
    """更新单个接口的配置（enabled/strategy/delay_min/max_ms）。

    元数据（名称/分类/默认档位）不可修改。
    """
    entry = get_registry_entry(api_key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown api_key: {api_key}")

    if payload.anti_risk_strategy is not None and payload.anti_risk_strategy not in ANTI_RISK_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Invalid strategy: {payload.anti_risk_strategy}")

    # custom 模式下校验 delay_min <= delay_max
    new_strategy = payload.anti_risk_strategy
    if new_strategy == "custom":
        row = _get_cfg_row(db, api_key)
        cur_min = row.delay_min_ms if row else 300
        cur_max = row.delay_max_ms if row else 800
        new_min = payload.delay_min_ms if payload.delay_min_ms is not None else cur_min
        new_max = payload.delay_max_ms if payload.delay_max_ms is not None else cur_max
        if new_min > new_max:
            raise HTTPException(status_code=400, detail="delay_min_ms cannot be greater than delay_max_ms")

    row = _ensure_cfg_row(db, api_key)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.anti_risk_strategy is not None:
        row.anti_risk_strategy = payload.anti_risk_strategy
    if payload.delay_min_ms is not None:
        row.delay_min_ms = payload.delay_min_ms
    if payload.delay_max_ms is not None:
        row.delay_max_ms = payload.delay_max_ms
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    # 刷新内存缓存，使新配置立即生效
    refresh_config_cache_for(api_key, db)

    return _to_status(entry, row, locale)
