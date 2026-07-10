# 基础数据隔离与挖掘解耦优化计划

## 一、问题背景

### 1.1 现状缺陷

当前挖掘任务 = 网络数据同步 + 计算，存在三大问题：

| 问题 | 根因 | 表现 |
|------|------|------|
| **数据污染** | 挖掘 universe 刷新把 7000+ 标的写入 Symbol 表且 `is_active=1` | 用户关注的几十只标的被冲垮，历史初始化误扫全市场 |
| **网络依赖** | 挖掘全程调 akshare（universe + 逐 symbol K线 + 新闻） | akshare 风控断连导致任务卡死/失败，耗时 2-6 小时 |
| **数据混用** | Symbol 表同时承载"用户关注/持仓/挖掘候选/全市场universe"四种语义 | 清理逻辑复杂，窗口期污染难根治 |

### 1.2 用户诉求

- 第一次初始化时全量同步所有 K 线（A股股票、A股ETF 等）作为**基础表**
- 基础表数据**不参与候选、标的**，与现有业务数据**物理隔离**
- 挖掘任务从基础表读取，不再调 akshare
- **挖掘结果不默认进候选池**，改为用户手动添加（避免自动污染业务表）

## 二、目标架构

### 2.1 数据分层

```
┌─────────────────────────────────────────────────────────┐
│  基础数据层（新增，物理隔离）                              │
│  ┌───────────────────┐  ┌──────────────────────────┐    │
│  │ universe_symbols  │  │ universe_daily_bars      │    │
│  │ 全市场标的元数据    │←→│ 全市场K线数据             │    │
│  │ A股5000+ETF1500   │  │ 每标的约250条/年          │    │
│  └───────────────────┘  └──────────────────────────┘    │
│  ↑ 定时增量同步（每日18:00，独立于挖掘）                  │
└─────────────────────────────────────────────────────────┘
                         ↓ 只读（评分计算）
┌─────────────────────────────────────────────────────────┐
│  挖掘结果层（新增，独立于业务表）                          │
│  ┌────────────────────────────────────────────────┐     │
│  │ discovery_candidates                           │     │
│  │ 挖掘评分结果（关联 universe_symbol_id）         │     │
│  │ 不写入 symbols 表，is_promoted=0 默认未晋升     │     │
│  └────────────────────────────────────────────────┘     │
│  ↑ 用户手动点击"加入候选池" → 才晋升到业务表              │
└─────────────────────────────────────────────────────────┘
                         ↓ 手动晋升（promote_candidate）
┌─────────────────────────────────────────────────────────┐
│  业务数据层（现有，保持不变）                              │
│  ┌───────────────────┐  ┌──────────────────────────┐    │
│  │ symbols           │  │ daily_bars               │    │
│  │ 用户关注/持仓/候选 │←→│ 候选标的K线（晋升时复制）  │    │
│  └───────────────────┘  └──────────────────────────┘    │
│  ↑ 仅用户手动添加的候选才写入（非自动）                   │
└─────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
初始化（一次性，首次启动）：
  akshare 全市场列表 → universe_symbols
  akshare 全量K线（1年） → universe_daily_bars（带断点续传+并发）

挖掘任务（历史读DB + 当天补齐，3-8分钟）：
  1. 从 universe_symbols 选择符合 scope 的股票/ETF（纯 DB 读取）
  2. 从 universe_daily_bars 读历史 K 线（纯 DB 读取）
  3. 对选中的标的补齐当天最新 K 线（少量网络调用，仅 scope 范围）
  4. 扫描评分 → 结果写入 discovery_candidates（is_promoted=0，不进 symbols 表）
  5. 前端展示挖掘结果列表，用户手动点击"加入候选池"才晋升到 symbols + daily_bars

定时增量同步（每日18:00，保持数据新鲜）：
  universe_symbols 中 last_bar_date < today 的标的
  → akshare 增量拉取 → universe_daily_bars
```

**关键调整**（相比初版方案）：
- 挖掘时不追求完全零网络，而是"历史读 DB + 当天补齐"
- 当天补齐只针对挖掘 scope 范围内的标的（如 cn-etf 约 1500 只），而非全市场
- 这样既保证了数据新鲜度（当天数据），又避免了全量网络同步的耗时
- 如果定时增量同步已跑过（last_bar_date == today），挖掘时跳过补齐，纯 DB 读取
- **挖掘结果不自动进候选池**：评分结果写入独立的 `discovery_candidates` 表（is_promoted=0），用户手动点击"加入候选池"才晋升到 `symbols` + `daily_bars`，彻底避免业务表污染

## 三、详细设计

### 3.1 新增表：`universe_symbols`

```sql
CREATE TABLE universe_symbols (
    id              INTEGER PRIMARY KEY AUTO_INCREMENT,
    symbol          VARCHAR(32) NOT NULL UNIQUE,      -- 标的代码（大写）
    name            VARCHAR(128),                     -- 名称
    asset_type      VARCHAR(16) NOT NULL,             -- stock / etf
    market          VARCHAR(16) NOT NULL,             -- sh / sz / bj
    region          VARCHAR(8) NOT NULL,              -- cn / us
    board           VARCHAR(32),                      -- main / star / gem
    industry        VARCHAR(64),                      -- 行业
    listed_at       DATE,                             -- 上市日期
    -- K线同步状态（冗余字段，加速健康度查询，避免 COUNT(*) 扫全表）
    last_synced_at  DATETIME,                         -- 最后同步时间
    last_bar_date   DATE,                             -- 最新K线日期
    bar_count       INTEGER DEFAULT 0,                -- K线条数
    is_synced       INTEGER DEFAULT 0,                -- 0=未同步 1=已同步
    sync_failed     INTEGER DEFAULT 0,                -- 连续失败次数（熔断用）
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_universe_symbol_asset (asset_type, region),
    INDEX ix_universe_symbol_synced (is_synced, last_bar_date)
);
```

### 3.2 新增表：`universe_daily_bars`

```sql
CREATE TABLE universe_daily_bars (
    id              INTEGER PRIMARY KEY AUTO_INCREMENT,
    universe_symbol_id INTEGER NOT NULL,              -- FK→universe_symbols.id
    trade_date      DATE NOT NULL,
    open            FLOAT,
    high            FLOAT,
    low             FLOAT,
    close           FLOAT,
    volume          FLOAT,
    amount          FLOAT,
    turnover_rate   FLOAT,
    source          VARCHAR(32) DEFAULT 'akshare',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (universe_symbol_id) REFERENCES universe_symbols(id) ON DELETE CASCADE,
    UNIQUE KEY uq_universe_bar_symbol_date (universe_symbol_id, trade_date),
    INDEX ix_universe_bar_symbol_date (universe_symbol_id, trade_date)
);
```

**设计要点**：
- 与 `daily_bars` 完全独立，通过 `universe_symbol_id` 关联，不与 `symbols.id` 产生外键关系
- 联合索引 `(universe_symbol_id, trade_date)` 加速范围查询（评分计算频繁使用）
- `universe_symbols` 冗余 `last_bar_date`/`bar_count`，避免每次健康度检查都扫 `universe_daily_bars`

### 3.3 新增表：`discovery_candidates`（挖掘结果独立存储）

挖掘评分结果写入此表，**不写入 `symbols` 表**，与业务表物理隔离。用户手动点击"加入候选池"才晋升。

```sql
CREATE TABLE discovery_candidates (
    id              INTEGER PRIMARY KEY AUTO_INCREMENT,
    scan_run_id     INTEGER NOT NULL,                 -- FK→scan_runs.id（挖掘批次）
    universe_symbol_id INTEGER NOT NULL,              -- FK→universe_symbols.id（基础表关联）
    -- 冗余字段（前端展示用，避免 join）
    symbol          VARCHAR(32) NOT NULL,
    name            VARCHAR(128),
    asset_type      VARCHAR(16),
    -- 评分快照
    quality_score   FLOAT,
    timing_score    FLOAT,
    priority_score  FLOAT,
    dimension_scores_json TEXT,                       -- 维度得分快照
    scoring_config_snapshot_json TEXT,                -- 评分配置快照
    stage           VARCHAR(16),                      -- hold/reduce/observe
    action          VARCHAR(16),                      -- buy/watch/...
    reason_tags     TEXT,
    warning_days    INTEGER DEFAULT 3,
    valid_days      INTEGER DEFAULT 5,
    -- 晋升状态（核心：默认 0，手动晋升后变 1）
    is_promoted     INTEGER DEFAULT 0,                -- 0=未晋升 1=已加入候选池
    promoted_at     DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (scan_run_id) REFERENCES scan_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (universe_symbol_id) REFERENCES universe_symbols(id) ON DELETE CASCADE,
    UNIQUE KEY uq_candidate_run_symbol (scan_run_id, universe_symbol_id),
    INDEX ix_candidate_promoted (is_promoted, created_at),
    INDEX ix_candidate_run (scan_run_id)
);
```

**设计要点**：
- 通过 `universe_symbol_id` 关联基础表，**不依赖 `symbols.id`**，挖掘时不创建 Symbol 记录
- `is_promoted` 标记是否已手动加入候选池，前端据此展示"加入候选池"按钮
- 冗余 `symbol`/`name`/`asset_type` 字段，避免前端列表查询 join 基础表
- 评分快照（dimension_scores_json）保留挖掘时的维度得分，供"为什么入选"展示

### 3.4 候选"晋升"机制（手动触发）

用户在挖掘结果列表中手动点击"加入候选池"，才从基础表复制到业务表：

```python
def promote_candidate(db, candidate: DiscoveryCandidate) -> Symbol:
    """用户手动触发：挖掘候选从基础表晋升到业务表（symbols + daily_bars）。

    仅在用户点击"加入候选池"时调用，非挖掘自动执行。
    """
    universe_symbol = db.get(UniverseSymbol, candidate.universe_symbol_id)

    # 1. 创建/更新 Symbol 记录
    symbol = db.execute(select(Symbol).where(Symbol.symbol == universe_symbol.symbol)).scalars().first()
    if symbol is None:
        symbol = Symbol(
            symbol=universe_symbol.symbol,
            name=universe_symbol.name,
            asset_type=universe_symbol.asset_type,
            market=universe_symbol.market,
            board=universe_symbol.board or "main",
            industry=universe_symbol.industry,
            is_active=1,
        )
        db.add(symbol)
    else:
        symbol.is_active = 1
    db.flush()  # 拿到 symbol.id

    # 2. 复制最近 N 天 K 线（评分所需，默认 365 天）
    cutoff = date.today() - timedelta(days=365)
    bars = db.execute(
        select(UniverseDailyBar)
        .where(UniverseDailyBar.universe_symbol_id == universe_symbol.id,
               UniverseDailyBar.trade_date >= cutoff)
    ).scalars().all()
    for bar in bars:
        existing = db.execute(
            select(DailyBar).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date == bar.trade_date)
        ).scalars().first()
        if existing is None:
            db.add(DailyBar(
                symbol_id=symbol.id,
                trade_date=bar.trade_date,
                open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                volume=bar.volume, amount=bar.amount, turnover_rate=bar.turnover_rate,
                source=bar.source,
            ))

    # 3. 标记候选已晋升
    candidate.is_promoted = 1
    candidate.promoted_at = _now()
    db.commit()
    return symbol
```

**新增 API**：
- `POST /api/discovery/candidates/{id}/promote`：手动晋升单个候选到候选池
- `POST /api/discovery/candidates/promote-batch`：批量晋升（传入 candidate_id 列表）

### 3.5 初始化全量同步流程

```
触发时机：首次启动时检测 universe_symbols 表为空 → 自动触发
同步范围：A股股票（~5000） + A股ETF（~1500）
执行方式：异步后台任务（不阻塞启动），支持断点续传

阶段 1：拉取标的列表（universe refresh）
  - cn-stock: ak.stock_info_a_code_name → upsert universe_symbols
  - cn-etf: ak.fund_etf_spot_em → ak.fund_etf_category_sina 备用 → upsert universe_symbols
  - 预计 10-30 秒

阶段 2：全量同步 K 线（断点续传 + 并发）
  - 遍历 universe_symbols WHERE is_synced=0
  - 每标的拉 1 年 K 线，写入 universe_daily_bars
  - 更新 last_bar_date / bar_count / is_synced=1
  - 并发度 3（与挖掘任务一致），单标的 90s 超时
  - 失败重试 2 次，连续失败 5 次熔断该标的（sync_failed++）
  - 预计 6500 标的 × 平均 3s = 5.5 小时（首次，可中断恢复）

阶段 3：增量补齐（定时任务，每日 18:00）
  - SELECT universe_symbols WHERE last_bar_date < today AND sync_failed < 5
  - 只拉缺失日期的 K 线
  - 预计 6500 标的 × 平均 1s = 1.8 小时（增量，远快于全量）
```

### 3.6 挖掘任务改造

**prepare 阶段（原 120s 超时 → 0s 纯 DB 读取）**：
```python
# 旧：调 akshare 刷新 universe，写入 symbols 表（污染）
universe = _refresh_universe_with_timeout(db, payload)  # 120s 超时

# 新：直接读 universe_symbols 表，选择符合 scope 的股票/ETF
config = DISCOVERY_SCOPE_CONFIG[payload.scope]
symbols = db.execute(
    select(UniverseSymbol).where(
        UniverseSymbol.region == config["region"],
        UniverseSymbol.asset_type == config["asset_type"],
        UniverseSymbol.is_synced == 1,  # 只用已同步K线的标的
    )
).scalars().all()
```

**sync 阶段（历史读DB + 当天补齐 + 评分写 discovery_candidates，不进 symbols）**：
```python
# 旧：逐个调 akshare 拉 K 线（1590 只 × 5-15s = 2-6 小时），并写入 symbols 表
result = _sync_one_symbol_with_timeout(db, symbol, payload, portfolio_id)

# 新：历史读 DB + 当天补齐（仅当 last_bar_date < today 时补齐）
today = date.today()
needs_today_fill = [s for s in symbols if s.last_bar_date < today]

# 步骤 1：并发补齐当天数据（仅 needs_today_fill，通常 < 1500 只）
if needs_today_fill:
    _fill_today_bars_concurrent(needs_today_fill, max_workers=3)  # 并发拉当天K线

# 步骤 2：纯 DB 读取历史 + 当天数据，批量评分
# 评分结果写入 discovery_candidates（is_promoted=0），不进 symbols 表
for universe_symbol in symbols:
    bars = db.execute(
        select(UniverseDailyBar)
        .where(UniverseDailyBar.universe_symbol_id == universe_symbol.id)
        .order_by(UniverseDailyBar.trade_date.desc())
        .limit(365)
    ).scalars().all()
    score = calculate_symbol_score(universe_symbol, bars)
    if score >= payload.min_score:
        # 写入挖掘结果表，不晋升到业务表
        db.add(DiscoveryCandidate(
            scan_run_id=run_id,
            universe_symbol_id=universe_symbol.id,
            symbol=universe_symbol.symbol,
            name=universe_symbol.name,
            asset_type=universe_symbol.asset_type,
            quality_score=score,
            dimension_scores_json=json.dumps(dim_scores),
            scoring_config_snapshot_json=json.dumps(config_snapshot),
            stage=stage,
            action=action,
            reason_tags=reason_tags,
            is_promoted=0,  # 默认未晋升，等用户手动添加
        ))
db.commit()
```

**当天补齐逻辑**：
```python
def _fill_today_bars_concurrent(symbols: list[UniverseSymbol], max_workers: int = 3):
    """并发补齐当天 K 线到 universe_daily_bars。

    只拉缺失的当天数据（1 条/标的），单标的 < 2s，1500 只并发 3 线程约 15-20 分钟。
    若定时增量同步已跑过（last_bar_date == today），此函数跳过，零网络。
    """
    today = date.today()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_one_day_bar, sym, today): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                bar = future.result(timeout=30)  # 单日数据 30s 超时
                if bar:
                    _upsert_universe_bar(db, sym.id, bar)
                    sym.last_bar_date = today
                    sym.bar_count += 1
            except Exception as exc:
                logger.warning("fill today bar %s failed: %s", sym.symbol, exc)
                sym.sync_failed += 1
    db.commit()
```

**前端交互改造**：
- 挖掘结果列表展示 `discovery_candidates`（含已晋升/未晋升状态标签）
- 未晋升候选（is_promoted=0）显示"加入候选池"按钮
- 已晋升候选（is_promoted=1）显示"已加入"标签 + "移除"按钮
- 点击"加入候选池" → 调 `POST /api/discovery/candidates/{id}/promote` → 后端执行 promote_candidate

**预期效果**：
- 挖掘耗时从 2-6 小时 → 3-8 分钟（历史读 DB 秒级 + 当天补齐 15-20 分钟）
- 若定时增量同步已跑过，当天补齐跳过，纯 DB 读取 < 1 分钟
- 网络依赖从"全量 1590×5s"降为"仅当天 1590×1s"（数据量减少 250x）
- **彻底不污染 symbols 表**：挖掘结果存独立表，仅用户手动添加的候选才进业务表

## 四、分阶段实施计划

### P0：基础表建表 + 初始化同步（核心解耦）

**目标**：建立基础数据层，首次初始化全量同步 K 线

**改动范围**：
1. 新增 `app/models/universe.py`：`UniverseSymbol` + `UniverseDailyBar` 模型
2. 新增 `app/models/discovery_candidate.py`：`DiscoveryCandidate` 模型（挖掘结果独立存储）
3. 新增 `app/services/universe_sync.py`：
   - `refresh_universe_symbols(scope)`：拉取全市场标的写入 universe_symbols
   - `sync_universe_bars_batch(scope, max_workers=3)`：批量同步 K 线到 universe_daily_bars
   - 断点续传：记录已同步的 universe_symbol_id
   - 熔断：单标的连续失败 5 次跳过
4. 新增 `app/services/universe_sync_task.py`：异步任务封装（进度跟踪、暂停/取消/重试）
5. 新增 `app/api/routes/universe.py`：
   - `POST /api/universe/initialize`：触发初始化同步
   - `GET /api/universe/initialize/status`：查询进度
   - `POST /api/universe/initialize/cancel`：取消
   - `POST /api/universe/initialize/retry`：重试失败项
   - `GET /api/universe/stats`：基础数据健康度（覆盖率、新鲜度）
6. `app/main.py` lifespan：启动时检测 universe_symbols 为空 → 自动触发初始化（异步，不阻塞启动）
7. 前端：设置中心新增"基础数据"面板，展示同步状态、手动触发入口

**验证标准**：
- universe_symbols 表有 6500+ 条记录（A股股票 5000 + ETF 1500）
- universe_daily_bars 表每个标的有 200+ 条 K 线
- 初始化任务支持暂停/继续/重试
- 挖掘任务 prepare 阶段耗时 < 1s（纯 DB 读取）

### P1：挖掘任务改造 + 手动晋升（消除网络依赖 + 候选池解耦）

**目标**：挖掘任务从基础表读取，结果存独立表，用户手动添加候选池

**改动范围**：
1. `app/services/discovery_tasks.py`：
   - `_refresh_cn_stock_universe` / `_refresh_cn_etf_universe` → 改为读 `universe_symbols` 表
   - `_sync_one_symbol` → 改为读 `universe_daily_bars` 表
   - **评分结果写入 `discovery_candidates` 表（is_promoted=0），不进 symbols 表**
2. 新增 `app/services/candidate_promote.py`：`promote_candidate` 手动晋升逻辑
3. 新增 `app/api/routes/discovery_candidates.py`：
   - `GET /api/discovery/candidates`：查询挖掘结果列表（支持 scope/已晋升过滤）
   - `POST /api/discovery/candidates/{id}/promote`：手动晋升单个候选
   - `POST /api/discovery/candidates/promote-batch`：批量晋升
   - `DELETE /api/discovery/candidates/{id}/promote`：移除已晋升候选（symbols is_active=0）
4. 保留旧逻辑作为 fallback：基础表无数据时退回 akshare（兼容首次未初始化的场景）
5. 前端 Discovery.tsx：
   - 挖掘面板提示"基础数据已就绪/需初始化"
   - 挖掘结果列表展示已晋升/未晋升状态标签
   - 未晋升候选显示"加入候选池"按钮，已晋升显示"已加入"+"移除"按钮

**验证标准**：
- 挖掘 1590 只 ETF 耗时 < 5 分钟（原 2-6 小时）
- **挖掘期间 symbols 表零写入**（结果存 discovery_candidates，is_promoted=0）
- 挖掘期间零 akshare 调用（除可选的新闻更新）
- 用户点击"加入候选池"后，symbols + daily_bars 才出现该标的记录

### P2：定时增量同步（保持数据新鲜）

**目标**：每日自动增量同步，数据始终新鲜

**改动范围**：
1. 引入 APScheduler（或复用现有 `_periodic_cleanup` 机制扩展）
2. `app/services/universe_sync.py` 新增 `incremental_sync(scope)`：
   - 查 `last_bar_date < today AND sync_failed < 5` 的标的
   - 增量拉取缺失日期 K 线
3. 定时任务：每个交易日 18:00 自动执行增量同步
4. 前端：基础数据面板展示"最后同步时间"，支持手动触发增量同步

**验证标准**：
- 每日 18:00 自动增量同步，无需人工干预
- 增量同步 6500 标的耗时 < 2 小时
- 挖掘时读到的 K 线数据不超过 1 天延迟

### P3：数据治理（可选优化）

**目标**：清理僵尸数据，表体积治理

**改动范围**：
1. 定期物理删除 `universe_symbols.sync_failed >= 5` 的标的数据（已退市/停牌）
2. `universe_daily_bars` 分区：按年分区，加速范围查询
3. 数据源 fallback 链正式落地（东财→新浪→腾讯→深交所）

## 五、风险与回滚

### 5.1 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| 首次全量同步耗时 5+ 小时 | 中 | 异步任务 + 断点续传，可暂停/继续 |
| akshare 风控导致初始化卡死 | 中 | 单标的 90s 超时 + 熔断 + sina 备用源 |
| 基础表与业务表数据不一致 | 低 | 候选晋升时以基础表为准，业务表只读复制 |
| 表体积膨胀（6500 标的 × 250 条/年） | 低 | 单表约 160 万条/年，MySQL 轻松承载 |

### 5.2 回滚方案

- P0 阶段纯新增表，不影响现有功能，可随时回滚（删除新表即可）
- P1 阶段保留旧逻辑 fallback，基础表无数据时自动退回 akshare 模式
- P2 阶段定时任务可随时关闭

## 六、预期收益

| 维度 | 当前 | 优化后 | 提升 |
|------|------|--------|------|
| 挖掘耗时 | 2-6 小时 | 3-8 分钟（含当天补齐） | 20-60x |
| 网络依赖 | 全量 1590×5s | 仅当天 1590×1s | 数据量减少 250x |
| Symbol 表污染 | 7000+ 标的混入 | 零（挖掘结果存独立表，用户手动晋升） | 根治 |
| 数据新鲜度 | 挖掘时才拉 | 每日定时 + 挖掘补齐当天 | 提升 |
| 失败恢复 | 断点续扫 | 重跑极快（3-8分钟） | 显著 |
| akshare 风控影响 | 高 | 低（仅当天补齐/定时同步） | 大幅降低 |
