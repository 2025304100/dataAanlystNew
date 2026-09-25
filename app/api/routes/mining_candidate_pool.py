"""候选池 HTTP 路由（SD-v2.0 §8.2 / §6.3 M3；任务 T08，T09 追加筛选端点）。

⚠️ 本项目路由范式：`router = APIRouter()` **无 prefix**，装饰器里写**全路径**；
   `/api/v1` 由 `app/api/router.py` 统一挂载。

⚠️ 分层纪律（R5）：本文件只做 DTO 转换与入参校验，
   **不得**出现规则编译、分位数、匹配算法。业务全在
   `app/services/factors/candidate_pool/{service,rules,presets}.py`。

⚠️ 路由声明顺序陷阱（**已落实**，T09 的新端点全部遵守）
   `GET /factor-mining/candidate-pools/{pool_id}` 会**吞掉**后面声明的静态路径。
   FastAPI 按**声明顺序**匹配，所以
     - `GET  /candidate-pools/filter-fields`
     - `GET  /candidate-pools/filter-presets`
     - `POST /candidate-pools/preview`
     - `POST /candidate-pools/from-filter`
   这四条静态路径**必须**声明在 `{pool_id}` 之前，否则例如 `filter-presets`
   会被当成 `pool_id="filter-presets"` → 走「池详情」分支 → 404。
   `_verify_t08_wiring.py` 有顺序断言；T09 的测试里也有一条同款断言。

批量删除的入参形态
------------------
`DELETE /factor-mining/candidate-pools/{id}/members` 的 `symbol_ids` **同时支持两种传法**：
  ① JSON body（优先）：`{"symbol_ids": [1,2,3], "reason": "行业不符"}`
  ② 重复 query 参数（兜底）：`?symbol_ids=1&symbol_ids=2&symbol_ids=3&reason=...`

为什么同时支持：本项目既有的 DELETE 路由都不带 body（18 处全是路径参数），
说明调用方可能按「DELETE 不带 body」实现；而 body 更适合批量。
两种都支持可避免「某些代理/客户端静默丢掉 DELETE body → 收到空列表」的整类问题，
且失败是**显式**的（空列表直接 400，不会静默删错）。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import duckdb
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.errors import FactorSevenError
from app.services.factors.candidate_pool import analysis as pool_analysis
from app.services.factors.candidate_pool import importer
from app.services.factors.candidate_pool import presets as pool_presets
from app.services.factors.candidate_pool import rules as pool_rules
from app.services.factors.candidate_pool import service as pool_service

logger = logging.getLogger(__name__)

router = APIRouter()

#: 数仓忙时的统一降级文案（DEF-8/DEF-4：挖掘占 duckdb_write 时预览/预设不 500）
_WAREHOUSE_BUSY_ZH = "数据仓库正被其他任务占用（如挖掘运行中），本次请求已降级，请稍后重试。"

#: 单次请求的标的数上限（URL 长度 / UI 单页选择量决定；service 层另有 5000 的服务级防御）
MAX_IDS_PER_REQUEST = 500


# ══════════════════════════════════════════════════════════
# 请求/响应 DTO
# ══════════════════════════════════════════════════════════


class CandidatePoolCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    source_type: str = Field(..., description="filter | import（创建后不可切换）")
    description: str | None = None
    filter_config: dict[str, Any] | None = Field(
        default=None, description="source_type=filter 时必填"
    )
    rule_hash: str | None = None
    import_batch_id: str | None = Field(
        default=None, description="source_type=import 时必填"
    )
    created_by: str = "local_user"


class CandidatePoolUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    #: 传与现值不同的 source_type 会被拒绝（BUSINESS_BLOCKED）；传相同值视为幂等
    source_type: str | None = None
    status: str | None = None
    operator_id: str = "system"


class MemberBatchAdd(BaseModel):
    symbol_ids: list[int] = Field(..., min_length=1)
    operator_id: str = "system"
    source: str = Field(default="manual", description="manual | filter | import")


class MemberBatchRemove(BaseModel):
    symbol_ids: list[int] = Field(..., min_length=1)
    operator_id: str = "system"
    reason: str | None = None


class FilterPreviewRequest(BaseModel):
    """条件筛选预览（**不落库**；防抖调用）。

    `filter_config` 的结构见 `rules.FIELD_BINDINGS` 与 `presets.PRESET_QUANTILES`。
    """

    filter_config: dict[str, Any] = Field(default_factory=dict)
    as_of_date: date | None = None


class FilterCreateRequest(BaseModel):
    """按条件筛选结果**生成候选池**（含成员物化）。

    ⚠️ 这是目前**唯一**会写成员的筛选入口：命中 0 / 命中 <50 /
    引用无数据字段 / 覆盖率不足 / 范围冲突，都会返回 4xx 且不写任何东西。
    """

    name: str = Field(..., min_length=1, max_length=128)
    filter_config: dict[str, Any] = Field(..., min_length=1)
    description: str | None = None
    as_of_date: date | None = None
    created_by: str = "local_user"
    operator_id: str = "system"


class SnapshotCreateRequest(BaseModel):
    """「生成挖掘物料」入参（向导 §3.7.1）。

    `analyze=False` 只冻结不分析（不会锁定）；缺省 `True`，即分析完即锁定。
    """

    analyze: bool = True
    as_of_date: date | None = None
    operator_id: str = "system"


class PoolPage(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


# ══════════════════════════════════════════════════════════
# 错误转换（FactorSevenError → HTTP）
# ══════════════════════════════════════════════════════════


def _http_status(code: str) -> int:
    """错误码 → HTTP 状态码。

    `FactorSevenError` 没有全局 handler（`app/main.py` 只处理 UnifiedError/HTTPException/
    SQLAlchemy/未捕获异常），未转换会落到 `unhandled_exception_handler` 变成 500。
    故路由层必须显式转换（与 `scoring_facade.py` 的既有做法一致）。
    """
    return {
        "NOT_FOUND": 404,
        "BUSINESS_BLOCKED": 409,
        "MINING_DOMAIN_BUSY": 409,
        "MINING_TEST_LOCKED": 409,
        "MINING_SNAPSHOT_NOT_FOUND": 404,
        "MINING_SNAPSHOT_NOT_LOCKED": 409,
        "VALIDATION_ERROR": 400,
    }.get(code, 400)


def _raise_http(exc: FactorSevenError) -> None:
    raise HTTPException(status_code=_http_status(exc.error_code), detail=exc.to_dict()) from exc


def _ids_from(body_ids: list[int] | None, query_ids: list[int] | None) -> list[int]:
    """合并 body / query 两种传参（body 优先），并做条数上限校验。"""
    ids = body_ids if body_ids else query_ids
    if not ids:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "VALIDATION_ERROR",
                "title_zh": "缺少 symbol_ids",
                "detail_zh": (
                    "请通过 JSON body {\"symbol_ids\": [...]} 或重复 query 参数 "
                    "?symbol_ids=1&symbol_ids=2 传入至少一个 symbol_id。"
                ),
                "impact": "本次操作未执行",
                "fix_link": "/settings/factor-mining?step=1",
                "retryable": False,
            },
        )
    if len(ids) > MAX_IDS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "VALIDATION_ERROR",
                "title_zh": "单次批量操作标的数超限",
                "detail_zh": (
                    f"单次最多 {MAX_IDS_PER_REQUEST} 个标的，本次 {len(ids)} 个。请分批提交。"
                ),
                "impact": "本次操作未执行",
                "fix_link": "/settings/factor-mining?step=1",
                "retryable": False,
            },
        )
    return ids


# ══════════════════════════════════════════════════════════
# 池 CRUD
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/candidate-pools", response_model=PoolPage)
def list_candidate_pools(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    status: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> PoolPage:
    """分页查询候选池（支持状态 / 来源类型 / 关键词筛选）。"""
    try:
        pools, total = pool_service.list_pools(
            db, page=page, page_size=page_size,
            status=status, source_type=source_type, keyword=keyword,
        )
    except FactorSevenError as exc:
        _raise_http(exc)
    items = [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "source_type": p.source_type,
            "status": p.status,
            "version": p.version,
            "member_count": p.member_count,
            "created_by": p.created_by,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in pools
    ]
    return PoolPage(items=items, total=total, page=page, page_size=page_size)


@router.post("/factor-mining/candidate-pools", status_code=201)
def create_candidate_pool(payload: CandidatePoolCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    """创建候选池（写入 `source_type`；创建后不可切换）。"""
    try:
        pool = pool_service.create_pool(
            db,
            name=payload.name,
            source_type=payload.source_type,
            description=payload.description,
            filter_config=payload.filter_config,
            rule_hash=payload.rule_hash,
            import_batch_id=payload.import_batch_id,
            created_by=payload.created_by,
        )
    except FactorSevenError as exc:
        _raise_http(exc)
    return pool_service.get_pool_detail(db, pool.id)


# ══════════════════════════════════════════════════════════
# 条件筛选（T09）
#
# ⚠️ 声明顺序：以下 4 条都是**静态路径**，必须排在
#    `GET /factor-mining/candidate-pools/{pool_id}` **之前**。
#    FastAPI 按声明顺序匹配，否则 `/filter-presets` 会被当成
#    `pool_id="filter-presets"` → 走「池详情」分支 → 404 且无告警。
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/candidate-pools/filter-fields")
def list_filter_fields() -> dict[str, Any]:
    """筛选字段目录（含**实测可用性**与阻断原因）。

    前端 Accordion 用它渲染每个分类的可选条件、禁用态与「为什么禁用」，
    避免前端维护一份会与后端漂移的字段清单。
    """
    fields = pool_rules.list_available_fields()
    available = [f for f in fields if f["availability"] != "blocked"]
    blocked = [f for f in fields if f["availability"] == "blocked"]
    return {
        "fields": fields,
        "available_count": len(available),
        "blocked_count": len(blocked),
        "categories": [
            {"category": c, "label_zh": pool_rules.CATEGORY_LABELS_ZH[c]}
            for c in pool_rules.CATEGORY_ORDER
        ],
        "min_pool_size": pool_rules.MIN_POOL_SIZE,
    }


@router.get("/factor-mining/candidate-pools/filter-presets")
def get_filter_presets(
    as_of_date: date | None = Query(default=None),
    markets: list[str] | None = Query(default=None),
    window_days: int = Query(default=pool_presets.DEFAULT_PRESET_WINDOW, ge=1,
                             le=pool_rules.MAX_LIQUIDITY_WINDOW),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """左栏预设（市值 / 估值 / 流动性 / 上市时间）。

    分位边界**由服务端按当日目标市场样本实时计算**，前端不得自行计算或缓存 ——
    详见 `presets.py` 模块 docstring。上市时间一组目前**全部阻断**
    （`listed_at` 两表均全空），返回 `availability=blocked` 与实测原因。
    """
    try:
        response = pool_presets.get_filter_presets(
            db, as_of_date=as_of_date, markets=markets, window_days=window_days,
        )
    except FactorSevenError as exc:
        _raise_http(exc)
    except (duckdb.IOException, OSError) as exc:
        # DEF-8/DEF-4：数仓被占用（挖掘持 duckdb_write 等）→ 降级不 500。
        # `available=False` 让前端渲染「加载失败·重试」，而不是「暂无可用预设」。
        logger.warning("filter-presets degraded (warehouse busy): %s", exc)
        return {
            "available": False,
            "reason_zh": _WAREHOUSE_BUSY_ZH,
            "as_of_date": None,
            "as_of_requested": as_of_date.isoformat() if as_of_date else None,
            "as_of_date_adjusted": False,
            "markets": [],
            "window_days": window_days,
            "window_actual_days": 0,
            "groups": [],
            "presets": [],
            "data_version": {},
            "as_of_evidence": {},
            "warnings": [_WAREHOUSE_BUSY_ZH],
        }
    return response.to_dict()


@router.post("/factor-mining/candidate-pools/preview")
def preview_filter(payload: FilterPreviewRequest,
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    """条件筛选预览（**不落库**，供 300ms 防抖调用）。

    永不返回 4xx：命中不足 / 字段无数据 / 范围冲突一律进 `blocking_issues`，
    并给出 `can_generate`。编辑过程中的中间态（例如命中 12 只）必须能正常渲染，
    否则向导 §3.2 的「底部实时更新命中数量」无法实现。
    """
    try:
        result = pool_rules.preview_filter(
            db, filter_config=payload.filter_config, as_of_date=payload.as_of_date,
        )
    except FactorSevenError as exc:
        _raise_http(exc)
    except (duckdb.IOException, OSError) as exc:
        # DEF-8/DEF-4：数仓被占用 → 阻断语义降级（WAREHOUSE_UNAVAILABLE），禁止 500。
        logger.warning("preview degraded (warehouse busy): %s", exc)
        result = pool_rules.preview_unavailable(
            reason_zh=_WAREHOUSE_BUSY_ZH,
            as_of_requested=payload.as_of_date,
            filter_config=payload.filter_config,
        )
    return result.to_dict()


@router.post("/factor-mining/candidate-pools/from-filter", status_code=201)
def create_pool_from_filter(payload: FilterCreateRequest,
                            db: Session = Depends(get_db)) -> dict[str, Any]:
    """按条件筛选结果生成候选池并物化成员。

    硬阻断（命中 0 / <50 / 字段无数据 / 覆盖率不足 / 范围冲突）→ 4xx，
    **在写任何东西之前**完成校验（`assert_pool_generatable`），不会留下半截池子。
    """
    try:
        preview = pool_rules.preview_filter(
            db, filter_config=payload.filter_config, as_of_date=payload.as_of_date,
        )
        pool_rules.assert_pool_generatable(preview)
    except FactorSevenError as exc:
        _raise_http(exc)

    # 命中代码 → `symbols.id`（成员表 ID 空间；两表是两套自增主键）
    symbol_ids, unmapped = pool_rules.resolve_hit_symbol_ids(
        db, preview.outcome.hit_symbols if preview.outcome else []
    )
    if not symbol_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "VALIDATION_ERROR",
                "detail_zh": "命中的标的全部无法映到 symbols 主表（ID 空间不一致），未创建候选池。",
                "extras": {"reason": pool_rules.REASON_INVALID_RULE,
                           "unmapped_count": len(unmapped),
                           "unmapped_sample": unmapped[:20]},
            },
        )

    stored_config = {
        **preview.rules.to_dict(),
        "as_of_date": preview.as_of_date.isoformat() if preview.as_of_date else None,
        "data_version": preview.data_version,
    }
    # DEF-5：同名 + 同 rule_hash 的池直接复用（前端固定名反复点击 → 曾累积
    # 50+ 个垃圾池）。复用时只重写成员，规则语义完全一致。
    reused_pool = pool_service.find_reusable_pool(
        db, name=payload.name, rule_hash=preview.rule_hash,
        source_type=pool_service.SOURCE_TYPE_FILTER,
    )
    try:
        if reused_pool is not None:
            pool = reused_pool
        else:
            pool = pool_service.create_pool(
                db,
                name=payload.name,
                source_type=pool_service.SOURCE_TYPE_FILTER,
                description=payload.description,
                filter_config=stored_config,
                rule_hash=preview.rule_hash,
                created_by=payload.created_by,
            )
    except FactorSevenError as exc:
        _raise_http(exc)

    # 分批写入：service 层单批上限 5000，这里按 1000 切并汇总
    chunks = [symbol_ids[i:i + 1000] for i in range(0, len(symbol_ids), 1000)]
    total = pool_service.MemberChangeResult(pool_id=pool.id, requested=len(symbol_ids),
                                            member_count=0)
    try:
        for chunk in chunks:
            part = pool_service.add_members(
                db, pool.id, symbol_ids=chunk,
                operator_id=payload.operator_id, source=pool_service.SOURCE_TYPE_FILTER,
            )
            total.added += part.added
            total.reactivated += part.reactivated
            total.skipped_existing += part.skipped_existing
            total.member_count = part.member_count
    except FactorSevenError as exc:
        # 池已创建但成员未写全 —— 带上 pool_id 让用户可续做，不要静默吞掉
        exc.extras = {**(exc.extras or {}), "pool_id": pool.id,
                      "member_write_failed": True}
        _raise_http(exc)

    return {
        **pool_service.get_pool_detail(db, pool.id),
        "as_of_date": stored_config["as_of_date"],
        "rule_hash": preview.rule_hash,
        # DEF-5：复用标记（前端据此提示「已复用既有候选池，未新建」）
        "reused": reused_pool is not None,
        "member_write": {
            "requested": len(symbol_ids),
            "chunks": len(chunks),
            "unmapped_count": len(unmapped),
            "unmapped_sample": unmapped[:20],
            **total.to_dict(),
        },
        "preview_blocking_issues": preview.blocking_issues,
    }


# ══════════════════════════════════════════════════════════
# 外部导入（T10）
#
# ⚠️ 声明顺序：以下 2 条是**静态路径**，必须排在
#    `GET /factor-mining/candidate-pools/{pool_id}` **之前**。
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/candidate-pools/import-template")
def download_import_template(
    fmt: str = Query(default=importer.FORMAT_CSV,
                     description="csv | xlsx"),
) -> Response:
    """下载导入模板（`symbol` 必填，`name`/`market`/`note` 可选）。

    xlsx 模板已把首列设为**文本格式**，避免 Excel 打开时吃掉前导零
    （这是导入失败最常见的原因）。
    """
    try:
        payload, filename, media_type = importer.build_template(fmt)
    except FactorSevenError as exc:
        _raise_http(exc)
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _read_upload(request: Request) -> tuple[bytes, str | None]:
    """从请求里取出 `(文件字节, 文件名)`，兼容两种上传方式。

    - `multipart/form-data`（前端 `FormData` 的默认形态）→ 取 `file` 字段
    - 其它 content-type → 把整个 body 当文件，文件名从 `X-Filename` 头取

    ⚠️ **为什么不用 `UploadFile = File(...)`**
    FastAPI 的 `File(...)` 在**路由注册期**（即 import 时）就会检查
    `python-multipart`，缺失时抛 `RuntimeError` —— 结果是**整个后端起不来**，
    而不只是「上传功能不可用」。本项目此前没有任何文件上传端点，
    也就是说这个依赖的缺失会把一个可降级的功能变成全局故障。

    手工解析后：缺依赖 → 该端点返回 503 并给出安装提示；裸 body 上传**照常可用**。
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except RuntimeError as exc:  # python-multipart 未安装
            if "multipart" in str(exc).lower():
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error_code": "VALIDATION_ERROR",
                        "title_zh": "服务端未启用 multipart 解析",
                        "detail_zh": (
                            "解析 multipart/form-data 需要 `python-multipart`："
                            "pip install python-multipart"
                        ),
                        "impact": "本次导入未执行；可用「请求体直接放文件 + X-Filename 头」的方式上传",
                        "fix_link": "/settings/factor-mining?step=1",
                        "retryable": True,
                    },
                ) from exc
            raise
        field = form.get("file")
        if field is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "VALIDATION_ERROR",
                    "detail_zh": "multipart 请求中缺少 `file` 字段。",
                },
            )
        payload = await field.read()
        filename = getattr(field, "filename", None)
        return payload, filename

    payload = await request.body()
    if not payload:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "VALIDATION_ERROR",
                "detail_zh": "请求体为空，未收到导入文件。",
            },
        )
    return payload, request.headers.get("x-filename")


@router.post("/factor-mining/candidate-pools/import-preview")
async def preview_import(
    request: Request,
    fmt: str | None = Query(default=None, description="缺省按扩展名判定"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """导入**预览**：解析 + 逐行匹配，**不写任何东西**。

    逐行结果含六种状态（成功 / 未匹配 / 重复 / 格式错误 / 非股票 / 已停牌），
    只有 `success` 行会入池。前端据此渲染「成功 N 条、失败 M 条」并展示错误明细。
    """
    payload, filename = await _read_upload(request)
    try:
        result = importer.import_members(
            db, pool_id=None, file_bytes=payload, filename=filename,
            fmt=fmt, commit=False,
        )
    except FactorSevenError as exc:
        _raise_http(exc)
    return result.to_dict()


@router.post("/factor-mining/candidate-pools/import-errors")
async def download_import_errors(
    request: Request,
    fmt: str | None = Query(default=None, description="缺省按扩展名判定"),
    db: Session = Depends(get_db),
) -> Response:
    """下载**错误明细 CSV**（向导 §3.1：「用户可下载错误明细，修正后重新导入」）。

    设计取向：本端点**重新解析并匹配同一份文件**，而不是缓存上一次预览的结果。
    理由——项目没有为「逐行结果」建表（T10 无迁移写权限），缓存就要引入新的
    存储层；而匹配成本仅为一次分批 `SELECT ... WHERE symbol IN (...)`，
    重跑是确定性的（同一文件 + 同一时刻主数据 → 同一结果），比引入状态更简单。
    """
    payload, filename = await _read_upload(request)
    try:
        result = importer.import_members(
            db, pool_id=None, file_bytes=payload, filename=filename,
            fmt=fmt, commit=False,
        )
    except FactorSevenError as exc:
        _raise_http(exc)

    csv_bytes = importer.build_error_csv(result)
    out_name = f"import_errors_{result.import_batch_id[:8]}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


@router.post("/factor-mining/candidate-pools/{pool_id}/import")
async def import_pool_members(
    pool_id: str,
    request: Request,
    fmt: str | None = Query(default=None, description="缺省按扩展名判定"),
    import_batch_id: str | None = Query(default=None),
    operator_id: str = Query(default="system"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """把导入文件中的**成功行**写入候选池。

    - 只有 `source_type=import` 的池可以导入（两种方式互斥，创建后不可切换）
    - 未匹配 / 重复 / 格式错误 / 非股票 / 已停牌的行**一律不入池**
    - 错误明细用 `POST /candidate-pools/import-errors` 下载
    """
    payload, filename = await _read_upload(request)
    try:
        result = importer.import_members(
            db, pool_id=pool_id, file_bytes=payload, filename=filename,
            fmt=fmt, operator_id=operator_id,
            import_batch_id=import_batch_id, commit=True,
        )
    except FactorSevenError as exc:
        _raise_http(exc)

    return {
        **result.to_dict(),
        "pool": pool_service.get_pool_detail(db, pool_id),
        "error_csv_endpoint": "/api/v1/factor-mining/candidate-pools/import-errors",
    }


@router.post("/factor-mining/candidate-pools/{pool_id}/snapshot", status_code=201)
async def create_pool_snapshot(
    pool_id: str,
    payload: SnapshotCreateRequest | None = Body(default=None),
    operator_id: str = Query(default="system"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """「生成挖掘物料」（向导 §3.7.1）：冻结快照 → 分析 → 写看板 → **锁定**。

    一步完成（§3.7.6：轻量统计，**同步**计算，不引异步任务）。
    分析完成后筛选/导入/批量删除都会被拒（`POOL_LOCKED`），
    会员需「重新选择」（`DELETE .../snapshot/latest`）才能改。
    """
    payload = payload or SnapshotCreateRequest()
    try:
        if payload.analyze:
            result = pool_analysis.analyze_pool(
                db, pool_id=pool_id, operator_id=operator_id,
                as_of_date=payload.as_of_date,
            )
        else:
            # 只冻结不分析：不锁定，允许用户先冻结再补分析（少见但合法）
            snap = pool_service.freeze_snapshot(
                db, pool_id=pool_id, operator_id=operator_id,
            )
            result = {
                "snapshot_id": snap.id,
                "pool_id": pool_id,
                "analysis_status": snap.analysis_status,
                "is_locked": bool(snap.is_locked),
                "data_cutoff_at": snap.data_cutoff_at,
                "analysis": None,
                "pool": pool_service.snapshot_to_dict(db, snap),
            }
    except FactorSevenError as exc:
        _raise_http(exc)
    return result


@router.get("/factor-mining/candidate-pools/{pool_id}/snapshot/latest")
def get_latest_pool_snapshot(pool_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """最新快照（看板数据源；`analysis` 为 None 表示尚未分析）。"""
    try:
        pool_service.get_pool(db, pool_id)
    except FactorSevenError as exc:
        _raise_http(exc)
    snap = pool_service.get_latest_snapshot(db, pool_id)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "NOT_FOUND",
                "title_zh": "尚无快照",
                "detail_zh": f"候选池 {pool_id} 还没有生成挖掘物料。"
                             "请先执行 POST .../snapshot。",
                "impact": "本次查询无结果",
                "fix_link": "/settings/factor-mining?step=1",
                "retryable": True,
            },
        )
    return pool_service.snapshot_to_dict(db, snap)


@router.get("/factor-mining/candidate-pools/{pool_id}/snapshots")
def list_pool_snapshots(
    pool_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """该池全部快照（新→旧；同一池多次挖掘各生成独立快照，需求 §3.8）。"""
    try:
        snaps = pool_service.list_snapshots(db, pool_id)
    except FactorSevenError as exc:
        _raise_http(exc)
    return {"pool_id": pool_id, "total": len(snaps),
            "items": [pool_service.snapshot_to_dict(db, s) for s in snaps]}


@router.delete("/factor-mining/candidate-pools/{pool_id}/snapshot/latest")
def reset_pool_snapshot(
    pool_id: str,
    operator_id: str = Query(default="system"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """「重新选择」（向导 §3.7.3）：删除最新快照（含分析结果与锁定）。

    锁定在快照上，删快照即自动解锁；成员**保留**（只删分析物）。
    历史快照不删 —— 它们是已挖掘任务的引用物，删了会破坏溯源。
    """
    try:
        return pool_service.reset_analysis(db, pool_id=pool_id, operator_id=operator_id)
    except FactorSevenError as exc:
        _raise_http(exc)


@router.get("/factor-mining/candidate-pools/{pool_id}")
def get_candidate_pool(pool_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """池详情 + 成员统计（含有效/软删除计数与锁定态）。"""
    try:
        return pool_service.get_pool_detail(db, pool_id)
    except FactorSevenError as exc:
        _raise_http(exc)


@router.delete("/factor-mining/candidate-pools/{pool_id}")
def delete_candidate_pool(
    pool_id: str,
    operator_id: str = Query(default="local_user"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """删除候选池（DEF-5：池生命周期闭环）。

    成员**硬删除**（整池都没了，保留孤儿关联无意义）+ 快照一并删除；
    若任一快照正被进行中的挖掘批次引用 → 409 `BUSINESS_BLOCKED`
    （先取消/放弃该批次再删），避免 run 的数据来源悬空。
    """
    try:
        return pool_service.delete_pool(db, pool_id=pool_id, actor=operator_id)
    except FactorSevenError as exc:
        _raise_http(exc)


@router.put("/factor-mining/candidate-pools/{pool_id}")
def update_candidate_pool(
    pool_id: str, payload: CandidatePoolUpdate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """更新池（`source_type` 传不同值会被 409 拒绝）。"""
    try:
        pool_service.update_pool(
            db, pool_id,
            name=payload.name,
            description=payload.description,
            source_type=payload.source_type,
            status=payload.status,
            operator_id=payload.operator_id,
        )
        return pool_service.get_pool_detail(db, pool_id)
    except FactorSevenError as exc:
        _raise_http(exc)


# ══════════════════════════════════════════════════════════
# 成员
# ══════════════════════════════════════════════════════════


@router.get("/factor-mining/candidate-pools/{pool_id}/members", response_model=PoolPage)
def list_pool_members(
    pool_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    keyword: str | None = Query(default=None, description="按代码 / 名称模糊搜索"),
    include_deleted: bool = Query(default=False),
    order_by: str = Query(default="included_at"),
    descending: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> PoolPage:
    """成员列表（join `symbols` 取展示字段；**只读主数据**）。

    默认只返回有效成员（`is_deleted=0`）；`include_deleted=true` 可查看已移除的关联。
    """
    try:
        items, total = pool_service.list_members(
            db, pool_id, page=page, page_size=page_size, keyword=keyword,
            include_deleted=include_deleted, order_by=order_by, descending=descending,
        )
    except FactorSevenError as exc:
        _raise_http(exc)
    return PoolPage(items=items, total=total, page=page, page_size=page_size)


@router.post("/factor-mining/candidate-pools/{pool_id}/members")
def add_pool_members(
    pool_id: str, payload: MemberBatchAdd, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """批量加入成员（幂等：已在池中跳过；软删除过的自动复活）。"""
    try:
        result = pool_service.add_members(
            db, pool_id, symbol_ids=payload.symbol_ids,
            operator_id=payload.operator_id, source=payload.source,
        )
    except FactorSevenError as exc:
        _raise_http(exc)
    return result.to_dict()


@router.delete("/factor-mining/candidate-pools/{pool_id}/members")
def remove_pool_members(
    pool_id: str,
    payload: MemberBatchRemove | None = Body(default=None),
    symbol_ids: list[int] | None = Query(default=None, description="重复参数；body 为空时使用"),
    reason: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """批量**软删除**成员（只断关联，主数据不动；写审计）。

    `symbol_ids` 可放 JSON body（优先）或重复 query 参数（兜底），详见模块 docstring。
    """
    ids = _ids_from(payload.symbol_ids if payload else None, symbol_ids)
    try:
        result = pool_service.remove_members(
            db, pool_id, symbol_ids=ids,
            operator_id=(payload.operator_id if payload else "system"),
            reason=(payload.reason if payload else reason),
        )
    except FactorSevenError as exc:
        _raise_http(exc)
    return result.to_dict()
