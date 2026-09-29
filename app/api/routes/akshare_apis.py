"""P2-E：第三方 akshare 接口管理 API。

提供：
1. GET /external-data/apis - 列出所有接口的元数据 + 用户配置 + 运行时状态
2. GET /external-data/apis/strategies - 列出所有防风控策略档位
3. POST /external-data/apis/{key}/probe - **提交探测任务**（202 返回 task_id，不阻塞等上游）
   GET  /external-data/apis/probe/{task_id} - 轮询任务状态与心跳（PT-DEF-18）
4. PUT /external-data/apis/{key} - 更新单个接口的配置（enabled/strategy/delay_min/max_ms）

接口元数据（名称/分类/默认档位）放代码 registry 中不可修改；
用户可配置的只有 enabled、anti_risk_strategy、delay_min/max_ms（custom 时）。

为什么探测改成"提交 + 心跳轮询"（PT-DEF-18）：
旧实现是一个同步请求里 `asyncio.wait_for(_PROBE_TIMEOUT_SECONDS)`，前端再设一个
更短的 `timeoutMs` —— 于是"上游到底能慢多久"变成必须由**人猜的常数**：猜小了慢而
成功的探测被误判失败（且客户端先断开、后端仍在占探测线程池），猜大了用户干等。
现在探测跑在 AsyncTaskRecord worker 里，每若干秒刷一次心跳；界面看的是"任务还活不
活着"，不再看任何秒数魔法。worker 侧只保留一个很宽的硬上限当**线程不外泄的安全网**。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import akshare as ak
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db, get_session_local
from app.models.akshare_api_config import AkshareApiConfig
from app.services.akshare_registry import (
    AKSHARE_API_REGISTRY,
    ANTI_RISK_STRATEGIES,
    get_registry_entry,
    record_probe_result,
    refresh_config_cache_for,
)
from app.services.akshare_utils import quiet_akshare_output
from app.services.async_tasks import (
    _set_task,
    _start_worker,
    create_async_task,
    get_async_task,
    is_worker_stop_requested,
)
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


# ── 探测任务化（PT-DEF-18）─────────────────────────────────────────────────
TASK_TYPE_EXTERNAL_API_PROBE = "external_api_probe"

# 心跳间隔：worker 每等这么久就刷一次 heartbeat_at。它是"多久报一次平安"，
# **不是**"多久算超时" —— 上游变慢只会多刷几次心跳，不会把慢而成功的探测误判成失败。
_PROBE_HEARTBEAT_SECONDS = 5.0

# 硬上限：只防"上游永久挂死导致探测线程外泄"，不是给用户看的时限。
# 超过它就把任务显式判为 timeout 并写下可读原因；正常的慢接口（几十秒）远在它之下。
_PROBE_HARD_LIMIT_SECONDS = 300.0

# 同一 api_key 被并发重复点击时复用一个任务。单实例部署下进程内登记即可，
# 进程重启自然清空；查不到就照常新建，绝不因为残留映射而拒绝用户。
_ACTIVE_PROBE_TASKS: dict[str, str] = {}
_ACTIVE_PROBE_LOCK = threading.Lock()


def _probe_now() -> datetime:
    """naive UTC，与 AsyncTaskRecord.heartbeat_at 的存法一致（_as_utc 再补时区）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime | None) -> datetime | None:
    """把库里读回的心跳/更新时间归一成 naive UTC。

    必要而不是洁癖：AsyncTaskRecord 的时间列在不同后端回来时 naive 与 aware 都会出现
    （框架自己的 `_as_utc` 就是为此存在），而 `_probe_now()` 是 naive —— 直接相减会
    `TypeError: can't subtract offset-naive and offset-aware datetimes`，
    表现是"轮询接口 500"，探测界面永远停在"探测中"。
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value

# P0 稳定性：探测专用独立线程池
# 避免探测超时后泄漏的子线程耗尽 FastAPI 默认线程池（anyio 默认 40 线程），
# 导致其他异步路由的 asyncio.to_thread 调用排队无响应。
# 独立池限制最大泄漏数为 8，且不阻塞其他异步路由。
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="akshare-probe")

# 探测重试退避（秒）：首次失败后等待此时间再重试，给 WAF 冷却窗口
_PROBE_RETRY_BACKOFF_SECONDS = 3.0


def _is_retryable_probe_error(exc: Exception) -> bool:
    """判断探测异常是否为可重试的网络瞬时错误（与 call_akshare_with_retry 对齐）。"""
    exc_name = type(exc).__name__
    exc_msg = str(exc).lower()
    return (
        exc_name in ("ConnectionError", "RemoteDisconnected", "TimeoutError",
                      "ConnectTimeout", "ReadTimeout", "ProtocolError")
        or "remote" in exc_msg
        or "connection" in exc_msg
        or "timeout" in exc_msg
        or "reset" in exc_msg
        or "broken pipe" in exc_msg
    )


def _run_probe(func: Any, probe_args: dict[str, Any], api_key: str) -> Any:
    """在线程中执行同步 akshare 调用（供 asyncio.to_thread 包装）。

    探测前应用防风控延时（apply_delay），避免批量探测时高频请求触发 WAF。
    遇到网络瞬时错误（RemoteDisconnected/ConnectionError/Timeout）重试 1 次，
    退避 3 秒给 WAF 冷却窗口，能区分"瞬时风控"和"持续不可用"。
    """
    from app.services.akshare_registry import apply_delay

    # 探测前应用防风控延时，降低 WAF 触发概率
    try:
        apply_delay(api_key)
    except Exception:
        logger.debug("probe %s: apply_delay failed, proceeding without delay", api_key)

    max_attempts = 2  # 首次 + 重试 1 次
    for attempt in range(max_attempts):
        try:
            with _proxy_bypass(), quiet_akshare_output():
                return func(**probe_args)
        except Exception as exc:
            if not _is_retryable_probe_error(exc) or attempt == max_attempts - 1:
                raise
            logger.info(
                "probe %s attempt %d failed (%s: %s), retrying in %.0fs",
                api_key, attempt + 1, type(exc).__name__, exc,
                _PROBE_RETRY_BACKOFF_SECONDS,
            )
            time.sleep(_PROBE_RETRY_BACKOFF_SECONDS)
    # 理论不可达
    raise RuntimeError("probe loop exhausted without result")


class ProbeTaskAccepted(BaseModel):
    """POST /probe 的响应：只回执任务，不携带探测结论。"""

    task_id: str
    api_key: str
    status: str
    reused: bool = Field(False, description="True 表示复用了同一接口正在跑的任务（重复点击）")


class ProbeTaskStatus(BaseModel):
    """轮询响应。`heartbeat_at` 是"任务还活着"的证据，不是倒计时。"""

    task_id: str
    api_key: str
    status: str
    stage: str | None = None
    percent: int = 0
    message: str | None = None
    heartbeat_at: datetime | None = None
    updated_at: datetime | None = None
    seconds_since_heartbeat: float | None = None
    # 判"疑似中断"由后端统一给结论，避免每个前端各自猜秒数
    heartbeat_stale: bool = False
    result: ProbeResult | None = None


def _forget_active_probe(api_key: str, task_id: str) -> None:
    with _ACTIVE_PROBE_LOCK:
        if _ACTIVE_PROBE_TASKS.get(api_key) == task_id:
            _ACTIVE_PROBE_TASKS.pop(api_key, None)


def _probe_akshare_api(task_id: str, api_key: str) -> None:
    """AsyncTaskRecord worker：真实跑一次探测，全程刷心跳，结束时落库结果。

    线程模型：调用仍交给 `_PROBE_EXECUTOR`（保住"最多 8 个并发探测"这个上限，
    避免批量点全部时把线程数打飞），worker 线程只做**分段等待 + 刷心跳**：
    每 `_PROBE_HEARTBEAT_SECONDS` 醒一次，写 heartbeat 与已等时长；超过
    `_PROBE_HARD_LIMIT_SECONDS` 才判 timeout（安全网，不是 UX 时限）。
    """
    db = get_session_local()()
    try:
        entry = get_registry_entry(api_key)
        func = getattr(ak, api_key, None)
        if entry is None or func is None:
            reason = f"unknown api_key: {api_key}" if entry is None else f"akshare has no attribute: {api_key}"
            _set_task(db, task_id, status="failed", stage="failed", percent=100,
                      message=reason, finished_at=_probe_now())
            _forget_active_probe(api_key, task_id)
            return

        probe_args = entry.get("probe_args", {}) or {}
        started = time.time()
        _set_task(
            db, task_id,
            status="running", stage="calling", percent=10,
            message=f"calling {api_key}",
            heartbeat_at=_probe_now(),
        )

        future = _PROBE_EXECUTOR.submit(_run_probe, func, probe_args, api_key)
        while True:
            if is_worker_stop_requested(task_id):
                # 优雅停机：宁可留一条 cancelled，也不要静默消失让界面永远转圈
                future.cancel()
                _set_task(db, task_id, status="cancelled", stage="cancelled", percent=100,
                          message="探测被停机中断", finished_at=_probe_now())
                _forget_active_probe(api_key, task_id)
                return
            try:
                result = future.result(timeout=_PROBE_HEARTBEAT_SECONDS)
                break
            except FutureTimeout:
                waited = time.time() - started
                if waited > _PROBE_HARD_LIMIT_SECONDS:
                    future.cancel()
                    latency_ms = int(waited * 1000)
                    err = f"probe exceeded hard limit {int(_PROBE_HARD_LIMIT_SECONDS)}s"
                    record_probe_result(db, api_key, False, latency_ms, err)
                    db.commit()
                    logger.warning("probe %s HARD LIMIT after %ds", api_key, int(waited))
                    _set_task(
                        db, task_id,
                        status="failed", stage="timeout", percent=100, message=err,
                        result_json=json.dumps({"key": api_key, "success": False,
                                               "latency_ms": latency_ms, "error": err}),
                        finished_at=_probe_now(),
                    )
                    _forget_active_probe(api_key, task_id)
                    return
                # 还在等上游：报平安，顺带把已等时长透给界面
                _set_task(db, task_id, heartbeat_at=_probe_now(), percent=50,
                          message=f"waiting upstream {int(waited)}s")
            except Exception as exc:
                latency_ms = int((time.time() - started) * 1000)
                err = f"{type(exc).__name__}: {exc}"
                record_probe_result(db, api_key, False, latency_ms, err)
                db.commit()
                logger.warning("probe %s EXC: %s", api_key, exc, exc_info=True)
                _set_task(
                    db, task_id,
                    status="failed", stage="failed", percent=100, message=err,
                    result_json=json.dumps({"key": api_key, "success": False,
                                            "latency_ms": latency_ms, "error": err}),
                    finished_at=_probe_now(),
                )
                _forget_active_probe(api_key, task_id)
                return

        latency_ms = int((time.time() - started) * 1000)
        success = True
        if hasattr(result, "empty"):
            success = not result.empty
        elif isinstance(result, (list, tuple)):
            success = len(result) > 0
        error = None if success else "Empty result"
        record_probe_result(db, api_key, success, latency_ms, error)
        db.commit()
        logger.info("probe %s done: success=%s latency=%dms", api_key, success, latency_ms)
        _set_task(
            db, task_id,
            status="done", stage="done", percent=100, message="done",
            heartbeat_at=_probe_now(),
            result_json=json.dumps({"key": api_key, "success": success,
                                    "latency_ms": latency_ms, "error": error}),
            finished_at=_probe_now(),
        )
        _forget_active_probe(api_key, task_id)
    except Exception as exc:  # worker 自身异常也必须落终态，不能留 running 僵尸
        logger.exception("probe worker crashed for %s: %s", api_key, exc)
        try:
            db.rollback()
        except Exception:
            pass
        try:
            _set_task(db, task_id, status="failed", stage="failed", percent=100,
                      message=f"probe worker error: {exc}", finished_at=_probe_now())
        except Exception:
            pass
        _forget_active_probe(api_key, task_id)
    finally:
        db.close()


@router.post(
    "/external-data/apis/{api_key}/probe",
    response_model=ProbeTaskAccepted,
    status_code=202,
)
def submit_probe(api_key: str):
    """提交一次接口探测，立即返回 task_id（不阻塞等上游）。

    前端拿到 task_id 后轮询 `GET /external-data/apis/probe/{task_id}`，
    以 `heartbeat_at` / `heartbeat_stale` 判断进度，而不是猜一个 HTTP 超时秒数。
    同一接口正在探测时复用同一任务（`reused=true`），不重复打上游。
    """
    if get_registry_entry(api_key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown api_key: {api_key}")

    with _ACTIVE_PROBE_LOCK:
        running_task_id = _ACTIVE_PROBE_TASKS.get(api_key)

    if running_task_id:
        existing = get_async_task(running_task_id, use_control_plane=True)
        if existing is not None and existing.status in ("queued", "running"):
            return ProbeTaskAccepted(task_id=existing.id, api_key=api_key,
                                     status=existing.status, reused=True)
        _forget_active_probe(api_key, running_task_id)

    task = create_async_task(
        TASK_TYPE_EXTERNAL_API_PROBE,
        {"api_key": api_key},
        use_control_plane=True,
        force_new=True,  # 每次点击都该真探一次；探测不是幂等投递场景
    )
    with _ACTIVE_PROBE_LOCK:
        _ACTIVE_PROBE_TASKS[api_key] = task.id

    _start_worker(task.id, lambda tid: _probe_akshare_api(tid, api_key))
    return ProbeTaskAccepted(task_id=task.id, api_key=api_key, status=task.status, reused=False)


@router.get("/external-data/apis/probe/{task_id}", response_model=ProbeTaskStatus)
def get_probe_status(task_id: str):
    """轮询探测任务：状态、心跳、结果。走控制平面连接池，不与重任务排队争用。"""
    task = get_async_task(task_id, use_control_plane=True)
    if task is None:
        raise HTTPException(status_code=404, detail=f"probe task not found: {task_id}")

    heartbeat = _as_naive_utc(getattr(task, "heartbeat_at", None))
    now = _probe_now()
    since = (now - heartbeat).total_seconds() if heartbeat else None
    terminal = task.status in ("done", "failed", "cancelled")
    # 只在非终态判"疑似中断"：连续 3 个心跳周期没动静 = worker 很可能已经没了
    stale = bool(not terminal and since is not None and since > _PROBE_HEARTBEAT_SECONDS * 3)

    result: ProbeResult | None = None
    # get_async_task 返回的是 AsyncTaskRead 视图：原始列叫 result_json 的话这里永远是
    # None（我第一版就踩了这个：视图模型只暴露已解析的 `result`）。两种形态都兼容，
    # 坏 JSON 时视图会给 None 或字符串，绝不能让它把整个轮询接口打挂。
    raw = getattr(task, "result", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            logger.warning("probe task %s has unparsable result", task_id)
            raw = None
    if isinstance(raw, dict):
        try:
            result = ProbeResult(**raw)
        except Exception:
            logger.warning("probe task %s result does not match ProbeResult", task_id)
            result = None

    api_key = ""
    payload_raw = getattr(task, "payload_json", None)
    if payload_raw:
        try:
            api_key = str(json.loads(payload_raw).get("api_key") or "")
        except Exception:
            api_key = ""

    return ProbeTaskStatus(
        task_id=task.id,
        api_key=api_key,
        status=task.status,
        stage=getattr(task, "stage", None),
        percent=int(getattr(task, "percent", 0) or 0),
        message=getattr(task, "message", None),
        heartbeat_at=heartbeat,
        updated_at=getattr(task, "updated_at", None),
        seconds_since_heartbeat=round(since, 1) if since is not None else None,
        heartbeat_stale=stale,
        result=result,
    )


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
