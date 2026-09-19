# 历史初始化范围控制方案：挖掘后自动清理 + 按来源选择

## 一、背景与问题

### 1.1 现象
用户只有十几只关注标的，但历史初始化同步了 7000+ 个。根因：挖掘任务（同步模式）的 `_refresh_cn_stock_universe` / `_refresh_cn_etf_universe` 把全市场 A 股 + ETF（~7000 条）灌进 Symbol 表且全部 `is_active=1`，挖掘跑完后没有清理不符合的标的，导致历史初始化 `WHERE is_active=1` 把这 7000+ 全跑了。

### 1.2 挖掘流程现状

```
refresh_universe（全市场入库 is_active=1，~7000 条）
  → 逐个 sync（同步 K 线 + 评分）
    → run_scan（筛选 executable 候选）
      → 结果：只有几十个 executable，其余 7000-几十=僵尸标的
```

挖掘的 `refresh_universe` 是必要的（需要对全市场重新评分筛选），但**跑完后不符合的标的应该清理**，不能留在 `is_active=1` 拖累后续初始化。

### 1.3 用户的操作依赖确认

| 操作 | 依赖 | 是否受清理影响 |
|---|---|---|
| 移入观察池 | `addSymbolToPrimaryWatchlist(symbolId)` — 只操作 executable 列表里的标的 | 不受影响，executable 标的保持 is_active=1 |
| 冻结 | `updateDiscoveryResult(resultId, { is_frozen })` — 只操作 ScanResult 记录 | 不受影响，ScanResult 独立于 Symbol.is_active |
| 再次跑挖掘 | `refresh_universe=true` 会重新 `_upsert_symbol`，把标的 `is_active=1` | 不受影响，下次挖掘会重新激活全市场 |

**结论：挖掘跑完后清理不符合标的（is_active=0）是安全的。**

---

## 二、方案设计

### 2.1 方案 A：挖掘任务清理（完成/中断/定时兜底）

#### 2.1.1 清理时机总览

挖掘任务有 5 种终态/中断态，每种都需要考虑是否清理：

| 时机 | 触发位置 | 保留范围 | 说明 |
|---|---|---|---|
| **done（完成）** | scan 完成后 | executable 候选 + 观察池 + 持仓 | 正常完成，清理不符合的 |
| **cancelled（取消）** | `_check_stop_state` 返回 "cancelled" | 观察池 + 持仓 | 用户明确放弃，scan 可能没跑完，没有 executable |
| **failed（异常）** | except 分支 | 观察池 + 持仓 | 异常终止，scan 可能没跑完 |
| **paused（暂停）** | **不清理** | — | 用户可能续跑（24h 内），续跑完成后走 done 清理 |
| **定时兜底** | 每天凌晨 | 观察池 + 持仓 | 清理 paused 超 24h 或 running 僵死超 30m 的遗留僵尸标的 |

#### 2.1.2 清理函数（抽取复用）

把清理逻辑抽成独立函数，done/cancelled/failed/定时任务都调它：

```python
# discovery_tasks.py 新增

def _cleanup_discovery_symbols(
    db: Session,
    *,
    scoped_symbol_ids: set[int],
    preserve_extra_ids: set[int] | None = None,
) -> int:
    """
    清理挖掘范围内的僵尸标的（is_active=0）。
    
    保留：executable 候选（由 preserve_extra_ids 传入）+ 观察池 + 持仓。
    清理：scoped_symbol_ids 中不在保留集合内的标的。
    
    返回：清理数量
    """
    if not scoped_symbol_ids:
        return 0

    # 收集需要保留的 symbol_id
    preserve_ids: set[int] = set()

    # 1. executable 候选（done 时传入，中断时为空）
    if preserve_extra_ids:
        preserve_ids |= preserve_extra_ids

    # 2. 观察池标的
    wl_rows = db.execute(select(WatchlistItem.symbol_id).distinct()).all()
    preserve_ids |= {r[0] for r in wl_rows}

    # 3. 持仓标的
    pos_rows = db.execute(select(Position.symbol_id).distinct()).all()
    preserve_ids |= {r[0] for r in pos_rows}

    # 不符合的标的 is_active=0
    deactivate_ids = scoped_symbol_ids - preserve_ids
    if not deactivate_ids:
        return 0

    db.execute(
        update(Symbol)
        .where(Symbol.id.in_(deactivate_ids))
        .values(is_active=0)
    )
    db.commit()
    return len(deactivate_ids)
```

#### 2.1.3 done 时清理

在 [行 701](file:///d:/ai_project/dataAanlystNew/app/services/discovery_tasks.py#L701) `db.commit()` 之后、[行 702](file:///d:/ai_project/dataAanlystNew/app/services/discovery_tasks.py#L702) `_set_task` 之前：

```python
# done 清理：保留 executable 候选 + 观察池 + 持仓
cleanup_count = _cleanup_discovery_symbols(
    db,
    scoped_symbol_ids=set(synced_symbol_ids),
    preserve_extra_ids={row.symbol_id for row in executable_rows},
)
_set_task(db, task_id, cleanup_count=cleanup_count, ...)
```

#### 2.1.4 cancelled 时清理

在 `_check_stop_state` 返回 "cancelled" 后，`_run_discovery_task` 的两处 stop 检查点（sync 循环内 [行 668-670](file:///d:/ai_project/dataAanlystNew/app/services/discovery_tasks.py#L668-L670)、sync 循环后 [行 668-670](file:///d:/ai_project/dataAanlystNew/app/services/discovery_tasks.py#L668-L670)）需要补充清理。

但 `_check_stop_state` 在循环内返回时，`synced_symbol_ids` 可能只有部分。改为在 `_run_discovery_task` 的 finally 块统一处理：

```python
def _run_discovery_task(task_id: str) -> None:
    db = SessionLocal()
    try:
        # ... 原有逻辑 ...

        # done 时清理（在 scan 完成后）
        if task.status == "done":
            cleanup_count = _cleanup_discovery_symbols(
                db,
                scoped_symbol_ids=set(synced_symbol_ids),
                preserve_extra_ids={row.symbol_id for row in executable_rows},
            )
            _set_task(db, task_id, cleanup_count=cleanup_count, ...)

    except Exception as exc:
        db.rollback()
        task = db.get(DiscoveryTaskRecord, task_id)
        if task is not None:
            # failed 时清理：没有 executable，只保留观察池 + 持仓
            if task.status == "failed":
                cleanup_count = _cleanup_discovery_symbols(
                    db,
                    scoped_symbol_ids=set(_json_loads(task.synced_symbol_ids_json, [])),
                    preserve_extra_ids=None,
                )
            task.status = "failed"
            task.stage = "failed"
            ...
    finally:
        # cancelled/paused 时清理
        task = db.get(DiscoveryTaskRecord, task_id)
        if task and task.status == "cancelled":
            # cancelled：清理，只保留观察池 + 持仓
            _cleanup_discovery_symbols(
                db,
                scoped_symbol_ids=set(_json_loads(task.synced_symbol_ids_json, [])),
                preserve_extra_ids=None,
            )
        # paused：不清理，等续跑或定时任务
        db.close()
```

**注意**：paused **不清理**，因为用户可能 24h 内续跑。续跑完成后走 done 清理。如果超 24h 未续跑，由定时任务清理。

#### 2.1.5 定时任务兜底清理

新增 `app/services/symbol_cleanup.py`：

```python
"""
定时清理挖掘任务遗留的僵尸标的。
覆盖场景：
  - paused 超过 RESUME_DEADLINE（24h）未续跑的任务
  - running 超过 STALE_RUNNING_DEADLINE（30m）僵死的任务
"""
from datetime import timedelta
from sqlalchemy import select, update
from app.db.session import SessionLocal
from app.models.discovery import DiscoveryTaskRecord
from app.models.symbol import Symbol
from app.services.discovery_tasks import (
    _now, _json_loads, RESUME_DEADLINE, STALE_RUNNING_DEADLINE,
    _cleanup_discovery_symbols,
)


def cleanup_stale_discovery_symbols(db) -> dict:
    """清理暂停超时/僵死任务遗留的僵尸标的，返回清理统计"""
    now = _now()
    cleaned_task_ids: list[str] = []
    total_cleaned = 0

    # 1. paused 超过 RESUME_DEADLINE（24h）
    stale_paused = db.execute(
        select(DiscoveryTaskRecord)
        .where(
            DiscoveryTaskRecord.status == "paused",
            DiscoveryTaskRecord.paused_at.is_not(None),
            DiscoveryTaskRecord.paused_at < now - RESUME_DEADLINE,
        )
    ).scalars().all()

    for task in stale_paused:
        synced_ids = set(_json_loads(task.synced_symbol_ids_json, []))
        cleaned = _cleanup_discovery_symbols(
            db, scoped_symbol_ids=synced_ids, preserve_extra_ids=None
        )
        if cleaned:
            total_cleaned += cleaned
            cleaned_task_ids.append(task.id)
        # 标记任务为 expired，防止重复清理
        task.status = "expired"
        task.finished_at = task.finished_at or now
        db.commit()

    # 2. running 超过 STALE_RUNNING_DEADLINE（30m）僵死
    stale_running = db.execute(
        select(DiscoveryTaskRecord)
        .where(
            DiscoveryTaskRecord.status == "running",
            DiscoveryTaskRecord.started_at.is_not(None),
            DiscoveryTaskRecord.started_at < now - STALE_RUNNING_DEADLINE,
        )
    ).scalars().all()

    for task in stale_running:
        synced_ids = set(_json_loads(task.synced_symbol_ids_json, []))
        cleaned = _cleanup_discovery_symbols(
            db, scoped_symbol_ids=synced_ids, preserve_extra_ids=None
        )
        if cleaned:
            total_cleaned += cleaned
            cleaned_task_ids.append(task.id)
        task.status = "failed"
        task.message = "任务僵死超时，已自动清理"
        task.finished_at = task.finished_at or now
        db.commit()

    return {
        "cleaned_task_ids": cleaned_task_ids,
        "total_cleaned": total_cleaned,
    }
```

#### 2.1.6 定时任务注册

在 `app/services/scheduler.py`（或现有的定时任务注册位置）新增每日清理任务：

```python
# 每天凌晨 03:00 清理僵死/超时的挖掘遗留标的
@scheduler.task("cron", id="cleanup_stale_symbols", hour=3, minute=0)
def cleanup_stale_symbols():
    db = SessionLocal()
    try:
        result = cleanup_stale_discovery_symbols(db)
        if result["total_cleaned"] > 0:
            logger.info(f"清理 {result['total_cleaned']} 个僵尸标的，涉及任务 {result['cleaned_task_ids']}")
    finally:
        db.close()
```

#### 2.1.7 新增 API 手动触发清理

`app/api/symbols.py` 新增端点，让用户可以手动触发清理（不用等定时任务）：

```python
@router.post("/symbols/cleanup-stale")
def cleanup_stale_symbols_api(db: Session = Depends(get_db)):
    """手动清理挖掘遗留的僵尸标的"""
    result = cleanup_stale_discovery_symbols(db)
    return {
        "ok": True,
        "cleaned_count": result["total_cleaned"],
        "cleaned_tasks": result["cleaned_task_ids"],
    }
```

#### 2.1.8 DiscoveryTaskRecord 新增字段

`app/models/discovery.py` 新增 `cleanup_count` 字段，记录每个任务清理了多少僵尸标的：

```python
cleanup_count = Column(Integer, default=0, nullable=False)  # 清理的僵尸标的数量
```

前端 task 列表/详情展示"已清理 N 个不符合标的"。

### 2.2 方案 B：历史初始化按来源选择（长期 + 灵活）

即使方案 A 清理了僵尸标的，用户仍可能需要按来源选择初始化范围（例如只跑观察池的十几只，或只跑持仓的几只）。

#### 2.2.1 后端 Schema
`app/schemas/market_data.py`

```python
HistoryInitializationSymbolSource = Literal[
    "all",          # 全部 is_active=1（默认）
    "watchlist",    # 观察池标的
    "positions",    # 持仓标的
    "scored",       # 有评分的标的
    "candidates",   # 最新扫描候选
    "cn-stock",     # A股
    "cn-etf",       # CN ETF
]

class HistoryInitializationRequest(BaseModel):
    preset: HistoryInitializationPreset = "1y"
    adjust: str = "qfq"
    asset_types: list[str] | None = None
    symbol_ids: list[int] | None = None
    symbol_source: HistoryInitializationSymbolSource = "all"  # 新增
    repair_mode: HistoryInitializationRepairMode = "both"
    auto_scan: bool = False                        # 新增：评分后触发扫描
    portfolio_id: int | None = None                # 新增：持仓/候选来源需要
    watchlist_id: int | None = None                # 新增：观察池来源可选指定
```

#### 2.2.2 后端解析逻辑
`app/services/market_data.py` — 新增 `_resolve_symbols_by_source`

```python
def _resolve_symbols_by_source(db, source, asset_types, portfolio_id, watchlist_id) -> list[Symbol]:
    """按来源类型解析标的列表"""
    if source == "all":
        return db.execute(
            select(Symbol).where(Symbol.is_active == 1, Symbol.asset_type.in_(asset_types))
            .order_by(Symbol.id.asc())
        ).scalars().all()

    symbol_ids: set[int] = set()

    if source == "watchlist":
        wl_id = watchlist_id
        if not wl_id:
            wl = db.execute(select(Watchlist).where(Watchlist.list_type == "watch")).scalars().first()
            wl_id = wl.id if wl else None
        if wl_id:
            rows = db.execute(select(WatchlistItem.symbol_id).where(WatchlistItem.watchlist_id == wl_id)).all()
            symbol_ids = {r[0] for r in rows}

    elif source == "positions":
        if not portfolio_id:
            raise ValueError("portfolio_id is required for source='positions'")
        rows = db.execute(select(Position.symbol_id).where(Position.portfolio_id == portfolio_id)).all()
        symbol_ids = {r[0] for r in rows}

    elif source == "scored":
        rows = db.execute(select(Score.symbol_id).distinct()).all()
        symbol_ids = {r[0] for r in rows}

    elif source == "candidates":
        latest_run = db.execute(
            select(ScanRun).where(ScanRun.status == "done")
            .order_by(ScanRun.id.desc()).limit(1)
        ).scalars().first()
        if latest_run:
            rows = db.execute(
                select(ScanResult.symbol_id).where(
                    ScanResult.scan_run_id == latest_run.id,
                    ScanResult.result_type == "executable"
                )
            ).all()
            symbol_ids = {r[0] for r in rows}

    elif source in ("cn-stock", "cn-etf"):
        from app.services.discovery_tasks import DISCOVERY_SCOPE_CONFIG
        config = DISCOVERY_SCOPE_CONFIG[source]
        from app.services.regions import REGION_MARKETS
        markets = REGION_MARKETS.get(config["region"], set())
        return db.execute(
            select(Symbol).where(
                Symbol.is_active == 1,
                Symbol.asset_type == config["asset_type"],
                Symbol.market.in_(markets)
            ).order_by(Symbol.id.asc())
        ).scalars().all()

    if not symbol_ids:
        return []
    return db.execute(
        select(Symbol).where(
            Symbol.is_active == 1,
            Symbol.id.in_(symbol_ids),
            Symbol.asset_type.in_(asset_types) if asset_types else True
        ).order_by(Symbol.id.asc())
    ).scalars().all()
```

`run_history_initialization_task` 的 prepare 阶段改为调用 `_resolve_symbols_by_source`。

#### 2.2.3 auto_scan 阶段
在 `calc_scores` 之后、`finalize` 之前，新增 `scan` 阶段：

```python
HISTORY_INIT_STAGE_KEYS = ("prepare", "sync_bars", "calc_scores", "scan", "finalize")
```

```python
if snapshot.get("auto_scan") and repair_mode != "bars":
    _update_history_stage(task_id, "scan", status="running", ...)
    try:
        from app.services.scans import run_scan
        scan_run = run_scan(db, symbols=symbols, ...)
        db.commit()
        summary["scan_run_id"] = scan_run.id
        ...
    except Exception as exc:
        db.rollback()
        _update_history_stage(task_id, "scan", status="failed", ...)
else:
    _update_history_stage(task_id, "scan", status="completed", done=0, total=0,
        message="Skipped scan")
```

#### 2.2.4 前端 UI
`frontend/src/components/HistoryInitSection.tsx`

```
┌─ 同步范围 ─────────────────────────────────┐
│ 来源类型：[全部标的 ▼]                       │
│   ├ 全部标的（is_active）                    │
│   ├ 观察池标的                               │
│   ├ 持仓标的                                 │
│   ├ 有评分的标的                             │
│   ├ 最新候选                                 │
│   ├ A股标的（cn-stock）                      │
│   └ CN ETF标的（cn-etf）                     │
│                                             │
│ [✓] 同步后触发扫描（auto_scan）              │
│   评分完成后对所选标的执行一次扫描            │
└─────────────────────────────────────────────┘
```

- 选"持仓标的"时自动从 AppContext 取当前 portfolio_id
- 选"观察池标的"时可下拉选具体观察池（默认 primary watchlist）
- 选"全部标的"时 auto_scan 禁用并提示"标的过多不建议触发扫描"

---

## 三、改动清单

### 方案 A：挖掘任务清理（完成/中断/定时兜底）

| # | 文件 | 改动类型 | 说明 |
|---|---|---|---|
| 1 | `app/services/discovery_tasks.py` | 修改 | 新增 `_cleanup_discovery_symbols` 函数；done/cancelled/failed 时调用清理 |
| 2 | `app/services/symbol_cleanup.py` | **新增** | `cleanup_stale_discovery_symbols`：清理 paused 超 24h / running 僵死超 30m 的遗留僵尸标的 |
| 3 | `app/services/scheduler.py` | 修改 | 注册每日 03:00 定时清理任务 |
| 4 | `app/api/symbols.py` | 修改 | 新增 `POST /symbols/cleanup-stale` 手动触发清理 |
| 5 | `app/models/discovery.py` | 修改 | DiscoveryTaskRecord 新增 `cleanup_count` 字段 |
| 6 | `frontend/src/i18n/index.ts` | 修改 | 新增"已清理 N 个不符合标的"文案 |

### 方案 B：历史初始化按来源选择 + auto_scan

| # | 文件 | 改动类型 | 说明 |
|---|---|---|---|
| 3 | `app/schemas/market_data.py` | 修改 | Request 增加 symbol_source/auto_scan/portfolio_id/watchlist_id；Summary 增加 scan 计数 |
| 4 | `app/services/market_data.py` | 修改 | 新增 `_resolve_symbols_by_source`；stage 增加 scan；worker 调用解析函数 |
| 5 | `frontend/src/types/index.ts` | 修改 | Task/Summary 增加新字段 |
| 6 | `frontend/src/api/client.ts` | 修改 | 入参增加新字段 |
| 7 | `frontend/src/components/HistoryInitSection.tsx` | 修改 | 来源类型 Select + auto_scan 开关 |
| 8 | `frontend/src/i18n/index.ts` | 修改 | 新增 histSource* / histAutoScan* 键 |

---

## 四、使用场景

### 场景 1：挖掘后自动清理
- 跑挖掘同步模式 → 评分筛选完 → executable 30 个保持 is_active=1，其余 6970 个 is_active=0
- 下次历史初始化默认只跑 30 个，不再跑 7000+

### 场景 2：只同步观察池里的十几只
- 历史初始化面板 → 来源类型选"观察池标的" + preset=1m + auto_scan=true
- 后端从 WatchlistItem 解析出 symbol_ids，只跑这十几只
- K线 → 评分 → 扫描，约 1-2 分钟

### 场景 3：再次跑挖掘（不管换不换条件）
- 挖掘本身就是全量重新评分筛选，每次都需要对全市场重跑
- refresh_universe=true 重新把全市场 is_active=1
- 全量评分 → run_scan 筛选 → 自动清理不符合的 is_active=0
- 每次挖掘后 Symbol 表只有 executable 候选 + 观察池 + 持仓保持 is_active=1
- 用户无感知，行为正确

### 场景 4：补持仓标的的数据
- 历史初始化面板 → 来源类型选"持仓标的" + preset=1q
- 后端从 Position 表解析出当前组合的持仓 symbol_ids

### 场景 5：挖掘任务被取消
- 用户中途取消挖掘 → cancelled → 自动清理已同步部分，只保留观察池 + 持仓
- 不符合的标的不会残留为 is_active=1

### 场景 6：挖掘任务异常失败
- 任务抛异常 → failed → 自动清理已同步部分，只保留观察池 + 持仓
- 僵尸标的不会残留

### 场景 7：挖掘任务暂停后超时
- 用户暂停后忘记续跑 → 24h 后无法续跑
- 每日 03:00 定时任务自动清理，标记 task 为 expired
- 用户也可通过 `POST /symbols/cleanup-stale` 手动触发

### 场景 8：挖掘任务进程僵死
- 任务 running 但实际卡死超 30m
- 每日定时任务检测到后标记 failed 并清理僵尸标的

---

## 五、验证方案

1. **done 清理**：跑挖掘同步模式完成，确认 is_active=1 数量 = executable 数 + 观察池数 + 持仓数 - 交集
2. **cancelled 清理**：跑挖掘中途取消，确认已同步部分被清理，只保留观察池 + 持仓
3. **failed 清理**：模拟异常，确认 failed 后已同步部分被清理
4. **paused 不清理**：暂停后确认 is_active 不变，续跑完成后正常清理
5. **定时清理**：构造 paused 超 24h 的任务，手动调 `POST /symbols/cleanup-stale`，确认清理成功
6. **定时清理**：构造 running 超 30m 的僵死任务，手动触发清理，确认标记 failed 并清理
7. **再次挖掘**：确认 refresh_universe 重新激活全市场，行为不变
8. **方案 B - watchlist**：选观察池来源，确认 prepare 阶段 N = 观察池 item 数
9. **方案 B - auto_scan**：auto_scan=true 时 scan 阶段生成 ScanRun + ScanResult
10. **回归**：source=all + auto_scan=false，行为与改动前完全一致
11. **类型检查**：`tsc --noEmit` + `npm run build` + `pytest`

---

## 六、风险与对策

| 风险 | 对策 |
|---|---|
| 清理误伤已移入观察池的标的 | 保留集合包含 watchlist_symbol_ids，不会误伤 |
| 清理误伤持仓标的 | 保留集合包含 position_symbol_ids，不会误伤 |
| 下次挖掘换条件需要全市场 | refresh_universe 会重新 is_active=1，无影响 |
| 冻结的标的是否保留 | 冻结标的一定是 executable 候选，在 preserve_ids 里 |
| paused 被误清理 | paused 不立即清理，等续跑或超 24h 后定时任务处理 |
| cancelled/failed 时 synced_symbol_ids 不完整 | finally 块从 task 记录读取 synced_symbol_ids_json，保证拿到已同步的 |
| 定时任务重复清理 | 清理后标记 task 为 expired/failed，下次查询自然排除 |
| 定时任务与正在运行的挖掘冲突 | 定时任务只处理 paused 超 24h / running 超 30m 的任务，正常 running 的不会触碰 |
| 来源解析查不到标的 | 返回空列表 + 明确提示"该来源无标的"，不启动任务 |
| portfolio_id 缺失 | 后端校验报错；前端自动填入 AppContext 当前 portfolio |
| run_scan 签名不匹配 | 先读 scans.py 确认参数签名 |
| auto_scan 标的过多 | 选"全部标的"时禁用 auto_scan 开关 |
