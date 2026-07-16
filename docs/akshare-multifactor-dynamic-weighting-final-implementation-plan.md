# A股免费多因子动态赋权功能最终落地实施方案

| 项目 | 内容 |
|---|---|
| 文档版本 | V1.10 |
| 编制日期 | 2026-07-15 |
| 实施状态 | Week 1～Week 4 及五项 V1.1 扩展均已落地；待生产影子观察、WxPusher 与 UAT |
| 适用项目 | Personal Quant Workbench / dataAanlystNew |
| 核心目标 | 在不破坏现有决策闭环的前提下，接入免费 AkShare 四类因子、DuckDB 因子仓库和滚动 Ridge 动态赋权 |
| 第一版资产范围 | A股股票优先；ETF 保持现有评分链路，后续单独扩展 |
| 决策周期 | 中短期趋势与多因子截面轮动，预期持有 3～15 个交易日 |

---

## 0. 最终实施结论

本功能不建设成第二套独立量化系统，也不替换现有 FastAPI、React、SQLAlchemy、机会挖掘、交易计划和回测模块。

最终采用以下落地方式：

1. 现有 SQLite/MySQL 继续承担业务数据、配置、任务、评分、扫描、交易计划和回测记录。
2. 新增 DuckDB 作为旁挂式因子分析仓库，只保存高容量历史数据、截面因子、训练标签和模型样本。
3. 复用现有 AkShare 接口管理、随机延时、重试、数据源回退和异步任务能力。
4. 新增因子流水线，将 DuckDB 的因子与模型结果转换为现有 Score，后续扫描和交易计划无需理解 DuckDB 或 Ridge。
5. 第一版先运行影子模式，同时保留当前手工评分；通过数据覆盖和回测验收后再切换正式动态评分。
6. 第一版继续使用现有回测引擎，不强制引入 VectorBT。
7. 第一版使用数据库持久化的应用内调度器，不引入 APScheduler；Linux/Windows 共用设置页管理，操作系统只负责保证后端常驻。
8. WxPusher 只承担结果通知，不承担自动下单。

### 0.3 实际实施进度（2026-07-14）

已完成：

- Week 1：Feature Flag、DuckDB Schema V1、SQL 模型、K 线增量镜像；
- Week 2：AkShare 因子接口注册、字段契约、估值/资金流/宏观本地镜像；
- Week 2：ep_ttm、negative_pb、main_inflow_5d_ratio、turnover_z20；
- Week 2：MAD 去极值、分位回退、每日截面 Z-Score、因子覆盖率；
- Week 2：中美 10 年期收益率与沪深两融的宏观状态/仓位折扣；
- Week 2：首批四项系统因子及 V1 公式的 SQL 元数据幂等初始化；V1.1 阶段二已扩展为五个核心 Ridge 截面因子，阶段三另增一个稀疏事件因子。
- Week 3：T+1 开盘至 T+5 收盘标签、停牌/涨跌停/未来窗口校验；
- Week 3：250 日滚动 Ridge、50 日时间验证、alpha 选择、signed beta；
- Week 3：模型样本/交易日/股票数/IC/系数门禁及 validated/rejected 状态；
- Week 3：manual/shadow/ridge 独立 Score 批次、单股贡献和宏观解释快照；
- Week 3：扫描活动模式隔离、宏观仓位折扣和回测模型版本固定；
- Week 4：因子概览、单股解释、模型列表/详情/激活/回退 API；
- Week 4：持久化活动模式、活动模型和激活/回退审计记录；
- Week 4：可取消、可轮询、只读本地数据的因子流水线任务；
- Week 4：设置页模型管理、流水线进度、手工回退和因子健康；
- Week 4：设置页持久化启用因子功能、初始化 DuckDB，并在未就绪时阻止流水线误提交；
- Week 4：跨平台持久化定时任务、默认计划、启停/增删改/立即执行和调度审计；
- Week 4：今日决策展示活动评分范围、模型版本和平均因子覆盖率；
- Week 4：投资中心展示单股原值、标准化值、系数、贡献及宏观仓位乘数；
- Week 4：生产前端打包完成，Vite 共转换 3682 个模块。
- V1.1 阶段一：DuckDB Schema V2 资产元数据、全市场成交额变化/Z20、上涨家数占比、两融/成交额背离和市场流动性分已完成；仅用于宏观状态、仓位折扣和解释，不作为个股截面特征。
- V1.1 阶段二：公告日对齐 ROE 同比增速已完成；新增财报版本化业务表、AkShare 逐股同步入口、DuckDB 增量镜像、公告日 point-in-time 计算、Ridge/Quality 接入和手工同步界面。
- V1.1 阶段三：龙虎榜机构净买额已完成；使用 stock_lhb_jgmmtj_em 的真实机构席位买卖口径，保存机构买入、卖出、净额、机构数和原始响应，并生成 lhb_institution_net_ratio 稀疏事件因子。
- V1.1 阶段四：东方财富人气榜每日快照已完成；保存当前前100名及原始响应，生成 hot_rank_attention 稀疏情绪因子，并新增默认关闭、可立即执行的每日 16:20 定时计划。
- V1.1 阶段五：候选池尾盘量价代理已完成；DuckDB Schema 升级 V3，按候选范围抓取1分钟行情，使用尾盘活跃度、尾盘收益和收盘位置生成 tail_accumulation_proxy，并新增每日 15:10 定时计划。
- V1.1 自动化补齐：新增每日 16:30 龙虎榜机构同步和每周六 10:00 财报历史同步；财报默认仅同步自选股前20只，所有新增计划默认关闭并支持立即执行。
- 2026-07-15 生产初始化：MySQL gpfx 已完成幂等建表，因子功能已启用，DuckDB V3 已初始化，评分模式保持 manual；已写入约16.3万条 A 股日线、260万级因子历史批次和16.3万条目标标签。
- 真实数据预热：3个关注标的写入336条财报历史和6条公告日对齐估值，龙虎榜机构写入144条，人气榜写入100条；资金流接口及尾盘分钟接口当前受远端主动断开影响，健康状态保持 degraded。
- 当前环境已启用行情、宏观、每日因子、人气榜、龙虎榜和每周财报六项计划；尾盘计划、每周 Ridge 训练和机会挖掘保持关闭。

当前边界：

- 功能仍默认关闭，评分模式仍默认 manual；可在“设置 → 因子模型”持久化启用并初始化仓库；
- 因子计算、宏观状态和健康检查均只读取本地 DuckDB；
- stock_lhb_detail_em 的龙虎榜总净额不用于机构因子；机构因子只读取 stock_lhb_jgmmtj_em 的机构席位统计；
- 新模型只会进入 validated/rejected，必须人工切换 shadow/ridge；
- 生产决策仍需先运行 shadow 观察，不允许因代码完成直接切 ridge；
- 每日/每周自动触发已可在“设置 → 定时任务”启用，也可随时手工立即执行；
- 自动触发依赖 FastAPI 后端持续运行，Linux/Windows 生产环境需配置后端随系统启动；
- WxPusher 仍是待完成的部署接线项；
- 龙虎榜、人气榜和尾盘代理均为稀疏辅助信号，当前不进入 Ridge 核心特征，也不会因缺失阻断核心健康门禁。

### 0.1 当前项目基础

当前项目已经具备：

- AkShare 1.18.30；
- A股、ETF 日线同步；
- 接口注册、探测、随机延时和重试；
- SQLite/MySQL 双数据库模式；
- 基本面 PE/PB 快照；
- 个股资金流和北向资金；
- 中美宏观指标、十年期国债和两融余额；
- Quality Score、Timing Score、Priority Score；
- 评分配置版本和评分快照；
- 机会挖掘、组合过滤、交易计划；
- 自研事件驱动回测，含佣金、印花税、滑点、夏普、最大回撤；
- 异步任务、任务中心、数据诊断和每日 18:00 增量同步。

当前剩余上线项：

- 首批因子历史覆盖回填与 shadow 稳定观察；
- Linux/Windows 后端常驻服务的生产部署与重启演练；
- WxPusher 通知实现和 token 配置；
- 正式 UAT、回滚演练和人工 ridge 激活。

### 0.2 运行环境事实

项目启动脚本使用：

    C:\Python312\python.exe

已验证：

- Python 3.12.2；
- AkShare 1.18.30；
- Pandas 2.3.3；
- SQLAlchemy 2.0.48；
- FastAPI 0.135.1。

第一版需要新增：

    numpy>=1.26,<3
    duckdb>=1.2,<2
    scikit-learn>=1.5,<2

第一版暂不新增：

    vectorbt
    apscheduler

---

## 1. 建设目标与范围

### 1.1 业务目标

在现有决策闭环中加入可追溯的多因子动态评分：

    数据更新
    -> 数据健康检查
    -> 四类因子计算
    -> 截面标准化
    -> 动态权重
    -> Quality / Timing / Priority Score
    -> 机会扫描
    -> 组合约束过滤
    -> 交易计划
    -> 执行记录
    -> 复盘修正

### 1.2 第一版必须完成

1. DuckDB 因子仓库建表、连接管理和幂等写入。
2. 现有 K 线向 DuckDB 增量镜像。
3. 第一批可运行因子：
   - 盈利收益率 E/P；
   - PB 估值；
   - 5日主力净流入占成交额比；
   - 20日换手率异常 Z-Score；
   - 中美10年期国债短期变化；
   - 沪深两融余额短期变化。
4. 财务、资金、情绪、宏观原始数据分层存储。
5. 交易日对齐、MAD/分位去极值、截面 Z-Score。
6. 未来5日可交易收益标签。
7. 过去250个交易日滚动 Ridge 训练。
8. 模型版本、因子版本、权重和数据截止时间追溯。
9. 手工、影子、正式三种评分模式。
10. 动态评分写入现有 Score 并接入机会扫描。
11. 因子健康度、模型状态和单股解释。
12. 每日自动流水线与 WxPusher 通知。
13. 单元、集成、前端和 E2E 测试。

### 1.3 第一版明确不做

1. Level-2 逐笔成交、盘口挂单和毫秒级大单。
2. 日内 T+0 或高频 Alpha。
3. 自动实盘下单。
4. 用 DuckDB 替换全部 SQLite/MySQL 业务表。
5. 一开始抓取全市场所有昂贵的逐股接口。
6. 用绝对值 beta 直接生成权重。
7. 未经过影子运行和回测验收就自动激活动态模型。
8. 对历史退市数据覆盖不足的问题做虚假的“完全消除”承诺。
9. 第一版同时重构现有回测为 VectorBT。
10. 第一版同时为 ETF 构建股票式基本面模型。

---

## 2. 用户使用方式

### 2.1 首次启用

用户通过设置页完成：

1. 在“接口管理”检查所需 AkShare 接口。
2. 在“初始化补数”选择自选股、持仓或有限股票池。
3. 在新增“因子模型”设置中选择：

       运行模式：影子模式
       预测周期：5个交易日
       训练窗口：250个交易日
       重训频率：每周
       评分频率：每日
       回退策略：当前手工评分

4. 启动初始化流水线。
5. 在任务中心查看同步、因子、训练和评分进度。
6. 在数据诊断中检查覆盖率和最新日期。
7. 在回测中比较手工评分与动态评分。
8. 验收后人工切换为正式模式。

### 2.2 每日使用

    查看数据健康
    -> 查看今日决策
    -> 运行或查看机会挖掘
    -> 打开候选股票因子解释
    -> 检查组合约束
    -> 确认交易计划
    -> 手工执行并记录

### 2.3 每周使用

每周模型重训后检查：

- 数据覆盖变化；
- 因子 IC 和方向；
- 权重是否异常漂移；
- 样本外结果；
- 与上一模型版本的回测差异；
- 当前是否发生回退。

---

## 3. 总体架构

~~~mermaid
flowchart LR
    A["现有 AkShare 接口管理、重试、限速"] --> B["因子数据同步任务"]
    C["现有业务库：SQLite / MySQL"] --> D["DuckDB 因子仓库"]
    B --> D

    D --> E["因子引擎：对齐、去极值、截面 Z-Score"]
    E --> F["未来5日收益标签"]
    E --> G["滚动 Ridge 训练"]
    F --> G

    G --> H["模型运行记录与动态权重快照"]
    E --> I["评分桥接层"]
    H --> I

    I --> J["现有 scores 表"]
    J --> K["现有机会扫描"]
    K --> L["现有组合约束与交易计划"]
    L --> M["现有回测、前端、WxPusher"]

    N["每日与每周调度"] --> B
    N --> E
    N --> G
    N --> K
~~~

### 3.1 强制架构边界

1. 网络调用只能发生在数据采集阶段。
2. 因子计算、模型训练、扫描和回测只能读取本地数据。
3. DuckDB 不保存用户密码、组合、持仓和交易计划。
4. SQL 业务库不保存全量历史模型样本。
5. 新模型只有在完整流水线成功后才能被原子激活。
6. 历史 Score、ScanResult 和 BacktestRun 不允许被新模型覆盖。

---

## 4. 存储职责划分

### 4.1 SQLite/MySQL 继续保存

- symbols；
- portfolios；
- portfolio_rules；
- positions；
- trade_setups；
- scores；
- scoring_configs；
- scan_runs；
- scan_results；
- backtest_runs；
- backtest_trades；
- async_tasks；
- alerts；
- 因子定义和因子版本；
- 模型运行记录；
- 模型权重快照；
- 活动模型指针；
- 通知记录。

### 4.2 DuckDB 新增保存

- K线镜像；
- 估值历史；
- 财务报告和公告日期；
- 个股资金流；
- 情绪和人气；
- 宏观时间序列；
- 因子原值；
- 去极值值；
- 截面 Z-Score；
- 收益标签；
- 训练样本；
- 数据批次和来源哈希。

### 4.3 文件位置

开发环境：

    D:\ai_project\dataAanlystNew\tmp\factor_warehouse.duckdb

生产环境建议：

    %LOCALAPPDATA%\QuantWorkbench\data\factor_warehouse.duckdb

通过环境变量覆盖：

    FACTOR_WAREHOUSE_PATH

禁止将生产 DuckDB 文件提交到 Git。

---

## 5. 后端模块设计

### 5.1 新增目录

    app/services/factors/
    ├── __init__.py
    ├── store.py
    ├── definitions.py
    ├── calendar.py
    ├── data_sync.py
    ├── fundamental.py
    ├── capital_flow.py
    ├── sentiment.py
    ├── macro.py
    ├── factor_engine.py
    ├── target_engine.py
    ├── ridge_model.py
    ├── scoring_bridge.py
    ├── health.py
    └── pipeline_task.py

### 5.2 模块职责

| 模块 | 职责 |
|---|---|
| store.py | DuckDB 连接、事务、迁移、UPSERT、并发锁 |
| definitions.py | 因子代码、公式、方向、频率、版本 |
| calendar.py | A股交易日、停牌和有效截面 |
| data_sync.py | 数据源编排、批次、幂等、失败记录 |
| fundamental.py | E/P、PB、ROE、盈利增速 |
| capital_flow.py | 资金流和龙虎榜 |
| sentiment.py | 换手异常和人气排名 |
| macro.py | 国债、两融和市场流动性状态 |
| factor_engine.py | 清洗、去极值、标准化、因子落库 |
| target_engine.py | 未来收益标签和不可交易过滤 |
| ridge_model.py | 训练、验证、权重和指标 |
| scoring_bridge.py | 动态结果转换为现有 Score |
| health.py | 覆盖率、最新日期、失败率、发布门禁 |
| pipeline_task.py | 完整任务状态机和原子发布 |

### 5.3 复用现有模块

| 现有模块 | 复用方式 |
|---|---|
| app/services/akshare_utils.py | 所有 AkShare 调用统一经过重试包装 |
| app/services/akshare_registry.py | 注册接口、限速策略和探测 |
| app/services/async_tasks.py | 复用任务状态和后台线程 |
| app/services/scoring_config_engine.py | 接收动态因子结果并写 Score |
| app/services/scans.py | 不改业务入口，继续读取 Score |
| app/services/trade_plans.py | 继续生成交易计划 |
| app/services/backtest.py | 继续使用指定 Score 批次回测 |
| app/api/routes/system.py | 扩展因子数据健康度 |
| app/main.py | 扩展每日流水线调度 |

---

## 6. AkShare 接口与因子映射

### 6.1 当前版本兼容结论

AkShare 1.18.30 中：

- stock_a_indicator_lg 不存在；
- stock_a_lg_indicator 不存在；
- stock_lrb_em 存在；
- stock_individual_fund_flow 存在；
- stock_lhb_detail_em 存在；
- stock_hot_rank_em 存在；
- stock_zh_a_hist 存在；
- bond_zh_us_rate 存在；
- stock_zh_a_spot_em 存在；
- stock_value_em 存在，但必须先做字段契约测试；
- stock_financial_analysis_indicator_em 存在；
- stock_yjbb_em 存在。

因此，不允许直接照搬 stock_a_indicator_lg。历史估值采用以下策略：

1. 优先探测 stock_value_em 是否能稳定返回日度 PE/PB 序列。
2. 如果字段契约稳定，则作为历史估值主接口。
3. 如果不稳定，则使用 stock_zh_a_spot_em 每日快照从上线日起沉淀。
4. 不具备足够历史长度的估值因子不得进入 Ridge 正式模型。

### 6.2 接口映射表

| 因子域 | 指标 | 主接口 | 备用/现有数据 | 更新频率 |
|---|---|---|---|---|
| F1 | E/P、PB | stock_value_em，契约通过后启用 | stock_zh_a_spot_em 每日快照 | 每日/每周 |
| F1 | ROE、盈利增速 | stock_financial_analysis_indicator_em | stock_lrb_em、stock_yjbb_em | 每季度 |
| F2 | 个股资金流 | stock_individual_fund_flow | 现有 CapitalFlow 表 | 每日 |
| F2 | 龙虎榜 | stock_lhb_detail_em | 无；失败时缺失 | 每日 |
| F3 | 换手率 | stock_zh_a_hist / 现有日线 | universe_daily_bars | 每日 |
| F3 | 人气排名 | stock_hot_rank_em | 无；失败时使用换手因子 | 每日 |
| F4 | 中美10年期国债 | bond_zh_us_rate | 现有 MacroIndicatorValue | 每日 |
| F4 | 沪深两融余额 | macro_china_market_margin_sh/sz | 现有宏观模块 | 每日 |
| F4 | 市场成交额 | stock_zh_a_spot_em 批量汇总 | 现有全市场行情 | 每日 |

### 6.3 请求策略

1. 优先批量接口，避免全市场逐股请求。
2. 逐股接口按有限股票池、分批、增量抓取。
3. 接口默认使用现有 conservative 策略。
4. 每批记录 batch_id、成功、空值、失败和耗时。
5. 重试只针对网络类瞬时错误。
6. 字段缺失和类型错误不盲目重试，记录接口契约失败。
7. 接口格式变化必须先进入隔离区，不直接污染正式表。
8. 回测和训练绝不触发网络请求。

---

## 7. 第一批因子定义

### 7.1 基本面 F1

#### 盈利收益率

    ep_ttm = 1 / pe_ttm

规则：

- PE 为空、等于0或小于0时记为缺失；
- 正向因子；
- 仅使用评分日当时已知的数据；
- 不允许用未来公告内容回填历史日期。

#### PB

    pb_raw = pb

规则：

- PB 为空或小于等于0时记为缺失；
- 原始方向为越低越好；
- 方向统一后使用 negative_pb = -pb 或截面反向分位；
- 不直接把高负债行业和轻资产行业混为一个绝对阈值；
- 第一版先做全市场截面，后续增加行业中性化。

#### ROE同比增速

    roe_yoy_growth = roe_ttm_current - roe_ttm_previous_year

规则：

- V1.1 阶段二已启用，因子代码 roe_yoy_growth，单位为百分点；
- 使用公告日作为可见时间；
- 同一报告期多版本时保留原始版本链，只有新公告日起才使用修订值；
- 当前报告期按年/月/日匹配去年同期，缺少去年同期时标记为不可用；
- 业务库唯一键为标的、报告期、公告日、报告类型和来源，DuckDB 使用同一版本键；
- 因子归入 fundamental/Quality，并作为第五个 Ridge 特征参与动态赋权。

### 7.2 资金面 F2

#### 5日主力净流入占成交额

    main_inflow_5d_ratio =
        sum(main_net_inflow over last 5 trading days)
        / max(sum(amount over last 5 trading days), 1e-8)

规则：

- 正向因子；
- 停牌日不补0，按实际交易日窗口；
- 主力资金缺失时允许回退过去5个有效值均值，但必须标记 imputed = true；
- 不把免费日频资金流描述为 Level-2。

#### 龙虎榜机构净额

    lhb_institution_net = institution_buy - institution_sell

规则：

- V1.1 阶段三已启用，使用 stock_lhb_jgmmtj_em，不使用 stock_lhb_detail_em 的总净额；
- 非上榜股票为0还是缺失必须由因子定义明确；
- 当前实现记为缺失并增加 has_lhb 事件标记，避免把“未上榜”误当成机构净额为0；
- 截面原值使用机构净买额除以当日个股成交额，降低大市值规模偏差；
- 该因子是稀疏事件因子，进入因子仓库、覆盖率和解释链，但暂不进入 Ridge 核心特征，也不参与核心健康门禁；
- 手工同步单次最多 31 个自然日，默认同步近 30 日，历史回填按月分段执行。

#### 候选池尾盘量价抢筹代理

    tail_accumulation_proxy =
        log(tail_avg_amount / pre_tail_avg_amount)
        + 20 * tail_return
        + close_location
        - 0.5

规则：

- V1.1 阶段五已启用，数据源为 stock_zh_a_hist_min_em 的1分钟量价，不是 Level-2 逐笔、大单方向或主买数据；
- 默认只处理最新候选池前20只，单次上限50只，避免逐股分钟接口触发限流；
- 尾盘窗口为14:30至15:00，至少需要180根全天分钟线、20根尾盘分钟线且最后时间不早于14:59，否则记为缺失；
- tail_avg_amount / pre_tail_avg_amount 衡量尾盘每分钟成交额是否放大，tail_return 衡量尾盘价格方向，close_location 衡量收盘是否靠近全天高位；
- 原始分钟响应、各组成项和最终代理分全部版本化保存，便于复核；
- 当前作为候选稀疏辅助信号进入仓库和投资中心解释，不进入 Ridge 核心特征或核心健康门禁；
- 连续3个标的网络失败时自动打开本批次熔断，不再继续请求剩余候选；
- 可在“设置 → 外部数据同步”手工执行，也可启用“每日候选尾盘代理”计划。

### 7.3 情绪面 F3

#### 20日换手异常

    turnover_z20 =
        (turnover_today - mean(turnover over previous 20 trading days))
        / max(std(turnover over previous 20 trading days), 1e-8)

规则：

- 时间序列 Z-Score，不等于最终截面 Z-Score；
- 需要至少15个有效历史交易日；
- 极高值不简单视为正向。

第一版非线性映射：

| turnover_z20 | 含义 | 情绪原始分 |
|---|---|---|
| 小于 -1.5 | 冰点/极度缩量 | 55 |
| -1.5 至 0.5 | 正常或温和修复 | 60 |
| 0.5 至 2.0 | 放量活跃 | 75 |
| 2.0 至 3.0 | 高热 | 60 |
| 大于 3.0 | 极端分歧/见顶风险 | 35 |

最终应结合价格突破状态：

- 放量且突破20日高点：加速；
- 放量但大幅冲高回落：过热扣分；
- 缩量但趋势未破坏：观察；
- 缩量且跌破关键均线：弱势。

#### 人气排名百分位

    hot_rank_attention = 1 - rank / max(total_count, 1)

规则：

- V1.1 阶段四已启用；免费接口只返回调用时刻的前100名，从启用日起逐日积累，不能回填历史；
- 极高人气不是永久正向；
- 与换手、突破和过热组合使用；
- 接口失败时不阻断主评分；
- 当前作为非线性稀疏情绪信号进入仓库和解释链，暂不进入 Ridge 核心特征或核心健康门禁；
- 可在“设置 → 外部数据同步”手工保存，也可启用“每日人气榜快照”定时计划。

### 7.4 宏观面 F4

#### 中美10年期国债5日变化

    cn_10y_change_5d = cn_10y_today - cn_10y_5d_ago
    us_10y_change_5d = us_10y_today - us_10y_5d_ago

#### 两融余额5日变化

    margin_change_5d =
        margin_balance_today / margin_balance_5d_ago - 1

#### 增强版全市场流动性（V1.1 阶段一已完成）

    market_amount_change_5d =
        market_amount_today / market_amount_5d_ago - 1

    market_amount_z20 =
        (market_amount_today - mean(previous_20d_market_amount))
        / max(std(previous_20d_market_amount), 1e-8)

    advancing_ratio = advancing_stock_count / comparable_stock_count

    margin_amount_divergence =
        margin_change_5d - market_amount_change_5d

实现约束：

- 全市场口径只使用 `region=cn`、`asset_type=stock` 的基础股票池，排除 ETF 和美股；
- 资产类型通过 DuckDB `raw_asset_universe` 本地元数据表过滤，不新增网络请求；
- 市场流动性分由成交额 Z20、上涨家数占比、两融变化和成交额变化加权形成；
- 杠杆上升但成交额萎缩时，使用正背离惩罚流动性分；
- 市场级指标只调整 `risk_on/neutral/cautious/defensive` 和仓位乘数，不进入股票截面 Z-Score；
- 所有结果写入动态评分的宏观解释快照，投资中心可查看。

关键设计：

宏观数据在同一交易日对所有股票相同，不能直接做股票截面 Z-Score，也不应直接作为普通截面 Ridge 特征。

第一版将宏观面用于：

1. 市场 risk_on / neutral / cautious / defensive 状态；
2. 交易计划仓位折扣；
3. 高估值因子权重上限；
4. 今日决策和风险提示。

建议仓位折扣：

    macro_position_multiplier = 0.70 + 0.30 * macro_score / 100

结果范围为 0.70～1.00。宏观环境只能降低风险仓位，不突破现有组合规则给出的仓位上限。

---

## 8. 因子计算标准

### 8.1 有效股票截面

每日计算前构造 eligible_universe：

- 当日有有效交易数据；
- 非停牌；
- 收盘价大于0；
- 成交额大于0；
- 上市时间满足最小历史要求；
- 不使用当日之后才披露的数据；
- ST、退市整理和异常标的保留原始数据，但由扫描策略决定是否排除；
- 回测时使用当时可见股票池，不能直接使用今天的存量股票池替代历史股票池。

### 8.2 清洗顺序

    原始数据
    -> 类型转换
    -> 无穷和非法值转空
    -> 时间可见性检查
    -> 缺失处理
    -> 去极值
    -> 方向统一
    -> 每日截面 Z-Score
    -> 因子落库

### 8.3 去极值

默认使用 MAD：

    median = median(x)
    mad = median(abs(x - median))
    lower = median - 5 * 1.4826 * mad
    upper = median + 5 * 1.4826 * mad

当 MAD 接近0时回退到 1% / 99% 分位截断。

所有阈值进入 factor_version.params_json，禁止只写死在代码中。

### 8.4 截面标准化

    z = (x - mean_cross_section) / max(std_cross_section, 1e-8)

要求：

- 按 trade_date 和 factor_version 分组；
- 每日独立计算；
- 少于最小有效股票数时该日不发布；
- 方向在标准化前统一为“值越大越好”；
- 保存 raw_value、winsorized_value、normalized_value；
- 保存 universe_count、valid_count、coverage_pct。

### 8.5 缺失值

优先级：

1. 不缺失；
2. 允许使用本标的过去5个有效值均值；
3. 允许使用行业中位数；
4. 使用全市场中位数；
5. 因子从该样本中排除。

每种因子必须明确 missing_policy，不允许全局统一前值填充。

所有填充值必须保存：

    is_imputed
    imputation_method
    original_is_missing

---

## 9. DuckDB 物理表

### 9.1 数据批次

~~~sql
CREATE TABLE IF NOT EXISTS ingestion_batches (
    batch_id VARCHAR PRIMARY KEY,
    source_key VARCHAR NOT NULL,
    scope_json VARCHAR,
    status VARCHAR NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    rows_received BIGINT DEFAULT 0,
    rows_written BIGINT DEFAULT 0,
    error_json VARCHAR,
    source_contract_version VARCHAR
);
~~~

### 9.2 日线镜像

~~~sql
CREATE TABLE IF NOT EXISTS raw_daily_bars (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    adjust VARCHAR NOT NULL,
    business_symbol_id BIGINT,
    universe_symbol_id BIGINT,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume DOUBLE,
    amount DOUBLE,
    turnover_rate DOUBLE,
    source VARCHAR NOT NULL,
    source_origin VARCHAR NOT NULL,
    source_row_id BIGINT NOT NULL,
    source_updated_at TIMESTAMP,
    ingested_at TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (symbol, trade_date, adjust)
);
~~~

说明：现有 daily_bars 和 universe_daily_bars 使用两个独立整数ID空间，
因此 DuckDB 使用标准化股票代码作为稳定主键，同时保留
business_symbol_id 和 universe_symbol_id 供追溯，禁止直接混用两个ID。

### 9.3 估值快照

~~~sql
CREATE TABLE IF NOT EXISTS raw_valuation_snapshots (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    pe_ttm DOUBLE,
    pb DOUBLE,
    dividend_yield DOUBLE,
    total_market_cap DOUBLE,
    circulating_market_cap DOUBLE,
    source VARCHAR NOT NULL,
    source_hash VARCHAR,
    ingested_at TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (symbol, trade_date, source)
);
~~~

### 9.4 财务报告

~~~sql
CREATE TABLE IF NOT EXISTS raw_financial_reports (
    symbol VARCHAR NOT NULL,
    report_period DATE NOT NULL,
    announcement_date DATE NOT NULL,
    report_type VARCHAR NOT NULL,
    roe_ttm DOUBLE,
    net_profit DOUBLE,
    revenue DOUBLE,
    net_profit_yoy DOUBLE,
    revenue_yoy DOUBLE,
    source VARCHAR NOT NULL,
    source_hash VARCHAR,
    ingested_at TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (
        symbol,
        report_period,
        announcement_date,
        report_type,
        source
    )
);
~~~

### 9.5 个股资金流

~~~sql
CREATE TABLE IF NOT EXISTS raw_fund_flows (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    main_net_inflow DOUBLE,
    main_net_inflow_pct DOUBLE,
    super_large_net_inflow DOUBLE,
    large_net_inflow DOUBLE,
    medium_net_inflow DOUBLE,
    small_net_inflow DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (symbol, trade_date, source)
);
~~~

### 9.6 情绪数据

~~~sql
CREATE TABLE IF NOT EXISTS raw_sentiment (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    hot_rank DOUBLE,
    hot_rank_total DOUBLE,
    hot_rank_pct DOUBLE,
    has_lhb BOOLEAN,
    lhb_institution_net DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (symbol, trade_date, source)
);
~~~

### 9.7 宏观数据

~~~sql
CREATE TABLE IF NOT EXISTS raw_macro (
    indicator_key VARCHAR NOT NULL,
    period DATE NOT NULL,
    value DOUBLE,
    previous_value DOUBLE,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMP NOT NULL,
    batch_id VARCHAR NOT NULL,
    PRIMARY KEY (indicator_key, period, source)
);
~~~

### 9.8 因子值

~~~sql
CREATE TABLE IF NOT EXISTS factor_values (
    symbol VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    factor_code VARCHAR NOT NULL,
    factor_version INTEGER NOT NULL,
    raw_value DOUBLE,
    winsorized_value DOUBLE,
    normalized_value DOUBLE,
    is_imputed BOOLEAN DEFAULT FALSE,
    imputation_method VARCHAR,
    eligible BOOLEAN NOT NULL,
    data_cutoff_at TIMESTAMP NOT NULL,
    calc_batch_id VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (
        symbol,
        trade_date,
        factor_code,
        factor_version,
        calc_batch_id
    )
);
~~~

### 9.9 收益标签

~~~sql
CREATE TABLE IF NOT EXISTS factor_targets (
    symbol VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    entry_date DATE,
    exit_date DATE,
    target_code VARCHAR NOT NULL,
    target_value DOUBLE,
    is_tradable BOOLEAN NOT NULL,
    invalid_reason VARCHAR,
    calc_batch_id VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (
        symbol,
        signal_date,
        target_code,
        calc_batch_id
    )
);
~~~

---

## 10. SQLAlchemy 业务模型

### 10.1 复用 Factor

现有 factors 表继续作为稳定因子字典，code 不随版本变化。

建议补充：

- source_type；
- frequency；
- default_missing_policy；
- is_active；
- updated_at。

### 10.2 新增 FactorVersion

建议表名：

    factor_versions

字段：

| 字段 | 说明 |
|---|---|
| id | 主键 |
| factor_id | 对应 factors.id |
| version | 递增版本 |
| formula_expr | 公式 |
| params_json | 窗口、去极值、缺失策略 |
| direction | higher_better / lower_better / nonlinear |
| source_mapping_json | 接口和字段 |
| effective_from | 生效时间 |
| change_note | 修改说明 |
| is_latest | 是否最新 |
| created_at | 创建时间 |

约束：

    unique(factor_id, version)

### 10.3 新增 FactorModelRun

建议表名：

    factor_model_runs

字段：

| 字段 | 说明 |
|---|---|
| id | UUID或字符串主键 |
| model_type | ridge |
| asset_type | stock |
| target_code | target_5d_return |
| train_start_date | 训练开始 |
| train_end_date | 训练结束 |
| validation_start_date | 验证开始 |
| validation_end_date | 验证结束 |
| data_cutoff_at | 数据截止时间 |
| feature_versions_json | 因子版本集合 |
| hyperparameters_json | alpha等参数 |
| metrics_json | IC、样本外结果等 |
| sample_count | 样本数 |
| symbol_count | 标的数 |
| trade_date_count | 交易日数 |
| status | training/validated/rejected/active/archived |
| rejection_reason | 未发布原因 |
| artifact_path | 可选模型文件 |
| created_at | 创建时间 |
| activated_at | 激活时间 |

### 10.4 新增 FactorWeightSnapshot

建议表名：

    factor_weight_snapshots

字段：

| 字段 | 说明 |
|---|---|
| id | 主键 |
| model_run_id | 模型运行 |
| factor_code | 因子代码 |
| factor_version | 因子版本 |
| coefficient | Ridge 原始系数 |
| normalized_weight | 保留符号的归一化权重 |
| train_ic | 训练 IC |
| validation_ic | 验证 IC |
| created_at | 创建时间 |

约束：

    unique(model_run_id, factor_code, factor_version)

### 10.5 扩展 Score

建议新增：

| 字段 | 说明 |
|---|---|
| weight_mode | manual / shadow / ridge |
| factor_model_run_id | 使用的模型版本 |
| factor_data_cutoff_at | 因子数据截止时间 |
| factor_quality_score | 基本面动态得分 |
| factor_timing_score | 资金与情绪动态得分 |
| model_alpha_score | Ridge总截面分 |
| macro_regime | risk_on / neutral / cautious / defensive |
| macro_position_multiplier | 仓位折扣 |

现有以下字段继续保存完整快照：

- scoring_config_snapshot_json；
- dimension_scores_json；
- factor_scores_json；
- calc_batch_id。

---

## 11. 标签与模型

### 11.1 标签定义

信号在 T 日收盘后产生，默认假设 T+1 开盘可执行：

    target_5d_return =
        close at fifth valid trading day after signal
        / open at first valid trading day after signal
        - 1

同时保存：

- signal_date；
- entry_date；
- exit_date；
- is_tradable；
- invalid_reason。

不可交易情况：

- T+1 停牌；
- 开盘价格无效；
- 连续无成交；
- 未来窗口不足；
- 数据缺失。

### 11.2 训练样本

训练集 JOIN 条件：

    factor_values.symbol = factor_targets.symbol
    and factor_values.trade_date = factor_targets.signal_date

要求：

- 只使用训练结束日之前已知的因子；
- 去掉最后5个尚未形成标签的交易日；
- 同一交易日的所有股票必须进入同一时间分段，不能随机拆分；
- 样本按交易日期排序；
- 不使用未来活动股票池替代历史股票池。

### 11.3 滚动窗口

默认：

    训练总窗口：250个交易日
    参数选择窗口：前200日训练，后50日验证
    预测周期：5个交易日
    重训频率：每周

alpha 候选：

    0.1, 1.0, 10.0, 100.0

选择标准：

1. 验证期 Rank IC；
2. 验证期 IC 稳定性；
3. 权重漂移；
4. 简单多空分组收益；
5. 不是只比较训练集 R²。

选定 alpha 后，用完整250日窗口重新拟合。

### 11.4 系数处理

禁止：

    weight_i = abs(beta_i) / sum(abs(beta))

正确方式：

    normalized_weight_i = beta_i / sum(abs(beta))

保留系数正负方向。

如果因子已按“越大越好”统一方向，而模型连续多个窗口给出显著负系数：

1. 不自动取绝对值；
2. 标记方向异常；
3. 进入模型报告；
4. 可由发布门禁降低或排除该因子；
5. 不静默修改因子含义。

### 11.5 模型分数

    model_alpha_raw = sum(beta_i * factor_z_i)

每日对 model_alpha_raw 计算截面百分位：

    model_alpha_score = percentile_rank(model_alpha_raw) * 100

### 11.6 模型发布门禁

模型至少满足：

- 有效交易日不少于180；
- 每日有效股票不少于300；
- 核心因子覆盖率不低于80%；
- 标签样本不少于30000；
- 无未来数据泄漏测试失败；
- 无 NaN/Inf 系数；
- 系数归一化后绝对值和约等于1；
- 验证期指标已生成；
- 模型状态为 validated。

第一版不自动根据收益指标激活模型。满足工程门禁后进入影子模式，正式激活仍需人工确认。

---

## 12. 与现有评分的桥接

### 12.1 三种模式

| 模式 | 说明 |
|---|---|
| manual | 完全使用当前评分 |
| shadow | 同时计算动态评分，但扫描仍使用 manual |
| ridge | 动态因子进入正式 Quality/Timing/Priority |

默认：

    manual

首次启用：

    shadow

### 12.2 动态维度

基本面模型贡献：

    factor_quality_raw =
        sum(beta_i * z_i for factors in F1)

资金与情绪模型贡献：

    factor_timing_raw =
        sum(beta_i * z_i for factors in F2 and F3)

分别转换成每日截面0～100分。

### 12.3 第一版默认融合

    final_quality_score =
        0.60 * current_quality_score
        + 0.40 * factor_quality_score

    final_timing_score =
        0.50 * current_timing_score
        + 0.50 * factor_timing_score

Priority Score 继续由现有 scoring_config 的 final_weights 聚合，不另建一套硬编码排序。

以上融合比例必须进入评分配置快照，可由后续版本调整。

### 12.4 宏观处理

宏观状态不直接参与股票截面排名。它影响：

- suggested_position_pct；
- risk_level；
- defensive warning；
- 高估值标的提示；
- 今日决策摘要。

现有组合阶段上限仍是最终硬约束：

    final_position =
        min(
            existing_rule_cap,
            suggested_position * macro_position_multiplier
        )

### 12.5 评分批次

建议格式：

    factor-{model_run_id}-{trade_date}-{pipeline_run_id}

同一 trade_date 可以同时存在：

- manual 批次；
- shadow 批次；
- ridge 正式批次。

扫描只读取当前活动评分模式和指定模型版本，避免同日多版本串用。

---

## 13. API 设计

新增路由：

    app/api/routes/factors.py
    app/api/routes/factor_models.py
    app/api/routes/factor_pipeline.py

### 13.1 因子概览

    GET /api/v1/factors/overview

返回：

- 最新数据日期；
- 各因子覆盖率；
- 最新流水线；
- 活动模型；
- 当前模式；
- 是否回退；
- DuckDB 状态。

### 13.2 因子定义

    GET  /api/v1/factors
    GET  /api/v1/factors/{factor_code}/versions
    POST /api/v1/factors/{factor_code}/versions

第一版只允许通过受控表单修改参数，不允许前端任意执行 Python。

### 13.3 单股解释

    GET /api/v1/factors/symbols/{symbol_id}/explanation

参数：

    trade_date
    model_run_id

返回：

- 原始值；
- 去极值值；
- Z-Score；
- beta；
- contribution；
- 数据日期；
- 是否填充；
- 因子版本；
- 模型版本；
- 宏观状态。

### 13.4 模型

    GET  /api/v1/factor-models
    GET  /api/v1/factor-models/latest
    GET  /api/v1/factor-models/{model_run_id}
    POST /api/v1/factor-models/{model_run_id}/activate
    POST /api/v1/factor-models/fallback

激活操作必须：

- 校验状态；
- 记录操作者和时间；
- 原子切换；
- 不修改历史 Score；
- 写入审计日志。

### 13.5 流水线任务

    POST /api/v1/factor-pipeline/tasks
    GET  /api/v1/factor-pipeline/tasks
    GET  /api/v1/factor-pipeline/tasks/{task_id}
    POST /api/v1/factor-pipeline/tasks/{task_id}/cancel

复用现有 async_tasks 状态结构。

---

## 14. 前端承载

### 14.1 不新增一级导航

功能作为底层决策引擎，由现有页面消费。

### 14.2 新增组件

    frontend/src/components/FactorModelSettings.tsx
    frontend/src/components/FactorHealthPanel.tsx
    frontend/src/components/FactorExplanationPanel.tsx
    frontend/src/components/FactorModelBadge.tsx

### 14.3 设置页

在“评分配置”和“外部数据”之间新增“因子模型”：

- 当前模式；
- 活动模型；
- 最近训练时间；
- 因子启停；
- 训练窗口；
- 预测周期；
- 手工/影子/正式切换；
- 立即运行；
- 回退；
- 权重表；
- 覆盖率；
- 最近失败。

### 14.4 今日决策

增加紧凑信息：

- 数据健康状态；
- 当前模型版本；
- 是否发生回退；
- 宏观状态；
- 动态评分候选数；
- 最后成功更新时间。

### 14.5 投资中心

在现有详情中增加：

- 四类因子得分；
- 关键因子贡献；
- 原值、Z-Score和beta；
- 数据日期；
- 模型版本；
- 缺失和填充提示。

不把模型训练控制放进投资中心。

### 14.6 机会挖掘

增加：

- 当前评分模式；
- 当前模型版本；
- 数据截止时间；
- 候选入选的前三项贡献；
- 影子分与正式分对比。

### 14.7 回测

增加：

- 评分模式；
- 固定 model_run_id；
- 因子版本集合；
- 数据截止时间；
- 手工 vs 动态对比入口。

回测创建后不得自动切换到后续模型。

### 14.8 预警中心

WxPusher 配置放入现有预警中心：

- enabled；
- endpoint；
- app_token，环境变量保存；
- uid/topic；
- 推送级别；
- 最近推送；
- 失败原因。

前端不得回显完整 token。

---

## 15. 流水线状态机

### 15.1 状态

    queued
    -> preparing
    -> syncing_bars
    -> syncing_fundamental
    -> syncing_capital
    -> syncing_sentiment
    -> syncing_macro
    -> validating_data
    -> calculating_factors
    -> calculating_targets
    -> training_model
    -> validating_model
    -> calculating_scores
    -> scanning
    -> generating_plans
    -> notifying
    -> done

失败状态：

    failed
    cancelled
    fallback

### 15.2 原子发布

流水线先写入新的 pipeline_run_id：

1. 所有 DuckDB 数据写入新批次；
2. 因子和模型生成新版本；
3. Score 写入新 calc_batch_id；
4. 数据健康和模型门禁通过；
5. 最后一步切换 active model / active score batch；
6. 切换失败则继续使用旧版本。

禁止边算边覆盖当前活动结果。

### 15.3 幂等

相同参数重复运行：

- 不重复插入原始数据；
- 不重复生成同一因子批次；
- 不覆盖不同版本；
- 可以安全续跑未完成阶段；
- 已完成阶段通过校验后跳过。

---

## 16. 调度方案

### 16.1 每日任务

统一由“设置 → 定时任务”中的持久化计划触发：

    18:00 K线增量同步
    -> 18:10 估值、资金、情绪、宏观同步
    -> 18:25 数据健康检查
    -> 18:30 因子计算
    -> 18:40 加载活动模型并评分
    -> 18:45 机会扫描
    -> 18:50 交易计划
    -> 18:55 WxPusher

这些时间是调度顺序目标，不作为硬实时承诺。实际以任务依赖和上一步完成为准。

调度器每 30 秒检查一次到期计划，计划配置和执行记录保存在 SQL 业务库中。多个后端 Worker 同时运行时通过数据库条件更新原子抢占，同一到期时点只由一个 Worker 提交；后端停机期间错过的计划会在重启后补触发一次。所有计划都可以禁用后继续使用“立即执行”。

### 16.2 每周任务

建议周六：

    数据完整性检查
    -> 补缺
    -> 生成最新标签
    -> Ridge重训
    -> 样本外验证
    -> 生成模型报告
    -> 进入 validated 或 rejected

不自动激活新模型。

### 16.3 每季度任务

- 财报同步；
- 公告日期校验；
- ROE和盈利增速重算；
- 财务因子覆盖报告；
- 因子版本变更时重新训练。

---

## 17. 四周实施排期

### Week 1：承载骨架与数据仓库

#### W1-01 依赖和配置

- 修改 requirements.txt；
- 增加 FACTOR_WAREHOUSE_PATH；
- 增加 FACTOR_FEATURE_ENABLED；
- 增加 FACTOR_WEIGHT_MODE；
- 增加 WXPUSHER 环境变量；
- 校验 Windows 路径和写权限。

交付：

- 依赖可安装；
- 配置可读取；
- 功能默认关闭；
- 旧功能无回归。

#### W1-02 DuckDB Store

- 连接管理；
- schema migration；
- 单写锁；
- 事务；
- UPSERT；
- 健康检查；
- 开发临时库。

交付：

    app/services/factors/store.py
    DuckDB schema v1
    tests/test_whitebox_factor_store.py

#### W1-03 SQL 模型

- FactorVersion；
- FactorModelRun；
- FactorWeightSnapshot；
- Score 扩展字段；
- Alembic 迁移；
- 模型序列化 schema。

交付：

    app/models/factor_version.py
    app/models/factor_model.py
    app/schemas/factor.py
    alembic migration

#### W1-04 K线镜像

- 从 DailyBar/UniverseDailyBar 批量读取；
- 增量镜像；
- 行数和价格对账；
- 不触发网络。

验收：

- 重复同步行数不增加；
- SQL 与 DuckDB 主键行一致；
- 随机样本 OHLCVA 一致。

### Week 2：因子数据与因子引擎

#### W2-01 接口注册和契约测试

- stock_value_em；
- stock_lrb_em；
- stock_financial_analysis_indicator_em；
- stock_hot_rank_em；
- stock_lhb_detail_em；
- 复用 bond_zh_us_rate 和 margin 接口；
- 字段别名和类型契约。

#### W2-02 第一批数据适配器

- fundamental.py；
- capital_flow.py；
- sentiment.py；
- macro.py；
- 批次、失败和原始哈希。

#### W2-03 因子定义和计算

- ep_ttm；
- negative_pb；
- main_inflow_5d_ratio；
- turnover_z20；
- cn/us yield change；
- margin change；
- 非线性情绪映射；
- 宏观 regime。

#### W2-04 截面管道

- 有效股票池；
- MAD去极值；
- 分位回退；
- 方向统一；
- 每日截面 Z-Score；
- 缺失策略；
- 覆盖率。

交付：

    app/services/factors/factor_engine.py
    tests/test_whitebox_factor_engine.py
    tests/test_whitebox_factor_data_contracts.py

### Week 3：标签、Ridge与影子评分

#### W3-01 标签

- T+1开盘到T+5收盘；
- 停牌和不可交易过滤；
- 标签幂等；
- 无未来数据。

#### W3-02 Ridge

- 250日窗口；
- 时间分段验证；
- alpha选择；
- signed beta；
- 权重快照；
- 指标报告。

#### W3-03 模型门禁

- 覆盖率；
- 样本数；
- 交易日数；
- 系数合法性；
- 泄漏测试；
- validated/rejected。

#### W3-04 评分桥接

- manual；
- shadow；
- ridge；
- Score批次；
- 模型版本；
- 解释JSON；
- 扫描读取活动批次。

交付：

    app/services/factors/target_engine.py
    app/services/factors/ridge_model.py
    app/services/factors/scoring_bridge.py
    tests/test_whitebox_factor_targets.py
    tests/test_whitebox_ridge_model.py
    tests/test_whitebox_factor_scoring_bridge.py

### Week 4：产品接入、调度和验收

当前状态：W4-01、W4-02、W4-03 的应用内调度已完成；W4-03 仅剩 WxPusher，W4-04 已完成自动化代码回归，生产 shadow 观察、UAT 和回滚演练待执行。

#### W4-01 后端 API

- [x] overview；
- [x] explanation；
- [x] model list/detail；
- [x] activate/fallback；
- [x] pipeline task。

#### W4-02 前端

- [x] 因子模型设置；
- [x] 因子健康；
- [x] 投资中心解释；
- [x] 今日决策状态；
- [x] API 类型包含机会挖掘和回测模型版本；
- [ ] 机会挖掘和回测页面的显式版本标签（不影响后端版本锁定）。

#### W4-03 调度和通知

- [x] 跨平台持久化调度器；
- [x] 设置页查看、增删改、启停和立即执行；
- [x] 每日增量同步与因子评分默认计划；
- [x] 每周训练计划；
- [x] 调度执行记录与业务任务状态关联；
- [ ] WxPusher；
- [x] 失败降级；
- [x] 重启后幂等。

#### W4-04 验收

- 白盒；
- 黑盒；
- 前端 Vitest；
- E2E；
- UAT；
- 影子运行；
- 回滚演练。

交付：

    生产操作SOP
    数据字典
    因子版本说明
    模型报告模板
    UAT执行记录

---

## 18. 具体文件改动清单

### 18.1 修改

| 文件 | 改动 |
|---|---|
| requirements.txt | 增加 numpy、duckdb、scikit-learn |
| app/core/config.py | DuckDB、Feature Flag、WxPusher 配置 |
| app/models/factor.py | 因子稳定字典扩展 |
| app/models/score.py | 模型、因子和宏观字段 |
| app/models/__init__.py | 注册新模型 |
| app/db/init_db.py | 初始化新表、因子字典和默认计划 |
| app/services/akshare_registry.py | 注册新增接口 |
| app/services/scoring_config_engine.py | 动态评分桥接入口 |
| app/services/scans.py | 固定活动模式和模型版本 |
| app/services/backtest.py | 固定 model_run_id 和评分批次 |
| app/services/trade_plans.py | 应用宏观仓位折扣 |
| app/api/router.py | 注册新路由 |
| app/api/routes/system.py | 因子和模型健康度 |
| app/main.py | 启动统一的应用内调度循环 |
| frontend/src/App.tsx | 不新增一级导航，只加载状态 |
| frontend/src/components/Settings.tsx | 新增因子模型和定时任务设置区 |
| frontend/src/components/TaskCenter.tsx | 展示并取消调度器提交的业务任务 |
| frontend/src/components/TodayDecision.tsx | 模型与健康状态 |
| frontend/src/components/InvestmentCenter.tsx | 因子解释 |
| frontend/src/components/Discovery.tsx | 模型标识和贡献 |
| frontend/src/components/BacktestConfig.tsx | 模型选择与锁定 |
| frontend/src/components/AlertCenter.tsx | WxPusher配置 |
| frontend/src/api/client.ts | 新API |
| frontend/src/types/index.ts | 新类型 |
| frontend/src/i18n/zh-CN.ts | 中文文案 |
| frontend/src/i18n/en-US.ts | 英文文案 |

### 18.2 新增

    app/services/factors/*
    app/models/factor_version.py
    app/models/factor_model.py
    app/schemas/factor.py
    app/schemas/factor_model.py
    app/api/routes/factors.py
    app/api/routes/factor_models.py
    app/api/routes/factor_pipeline.py
    app/models/scheduled_task.py
    app/schemas/scheduled_task.py
    app/services/scheduled_tasks.py
    app/api/routes/scheduled_tasks.py
    app/services/notifiers/wxpusher.py
    frontend/src/components/FactorModelSettings.tsx
    frontend/src/components/ScheduledTaskManager.tsx
    frontend/src/components/FactorHealthPanel.tsx
    frontend/src/components/FactorExplanationPanel.tsx
    frontend/src/components/FactorModelBadge.tsx

---

## 19. 测试方案

### 19.1 白盒测试

新增：

    tests/test_whitebox_factor_store.py
    tests/test_whitebox_factor_data_contracts.py
    tests/test_whitebox_factor_engine.py
    tests/test_whitebox_factor_targets.py
    tests/test_whitebox_ridge_model.py
    tests/test_whitebox_factor_scoring_bridge.py
    tests/test_whitebox_factor_pipeline.py
    tests/test_whitebox_factor_health.py
    tests/test_whitebox_scheduled_tasks.py
    tests/test_whitebox_wxpusher.py

必须覆盖：

- DuckDB 幂等；
- 事务回滚；
- 并发写锁；
- 字段变化；
- 空DataFrame；
- MAD为0；
- std为0；
- NaN/Inf；
- 缺失填充标记；
- 停牌；
- 财报公告日期；
- T+1不可交易；
- 日期分段；
- 未来数据泄漏；
- beta符号；
- 模型门禁；
- 影子模式不影响正式扫描；
- 激活和回退；
- 默认计划只初始化一次，删除或改名后重启不恢复；
- 每天/每周/间隔的时区换算与到期原子抢占；
- 计划 CRUD、禁用计划立即执行和调度审计；
- 通知失败不影响评分发布。

### 19.2 黑盒测试

新增：

    tests/test_blackbox_factor_api.py

覆盖：

- overview；
- explanation；
- pipeline；
- model list；
- activate；
- fallback；
- 权限和非法状态；
- 历史模型不可变。

### 19.3 前端测试

新增：

    frontend/src/components/__tests__/FactorModelSettings.test.tsx
    frontend/src/components/__tests__/ScheduledTaskManager.test.tsx
    frontend/src/components/__tests__/FactorHealthPanel.test.tsx
    frontend/src/components/__tests__/FactorExplanationPanel.test.tsx

覆盖：

- manual/shadow/ridge；
- 模型状态；
- 数据缺失；
- 回退提示；
- token脱敏；
- 贡献解释；
- 日期和版本。

### 19.4 E2E

新增：

    tests/e2e/test_factor_pipeline_flow.py
    tests/e2e/test_factor_model_activation_flow.py

### 19.5 验证命令

后端：

    $env:PYTHONDONTWRITEBYTECODE='1'
    C:\Python312\python.exe -m pytest -m whitebox

相关模块快速验证：

    C:\Python312\python.exe -m pytest \
      tests/test_whitebox_factor_store.py \
      tests/test_whitebox_factor_engine.py \
      tests/test_whitebox_ridge_model.py \
      tests/test_whitebox_factor_scoring_bridge.py -q

前端：

    cd frontend
    npm test
    npx tsc -b --pretty false

说明：

本环境下 Vite/esbuild 在受限沙箱内会出现 spawn EPERM；允许子进程后，2026-07-15 已完成生产打包，3682 个模块转换成功。后端最近一次全量收集 647 项，560 passed、85 skipped、1 xfailed、1 xpassed，零真实失败；本轮因子与调度相关回归 66 项全部通过，因子设置白盒 14 项、真实 HTTP 黑盒 3 项全部通过。FactorModelSettings 定向 Vitest 3 项通过，覆盖正常、未启用和仓库未初始化状态。`npx tsc -b --pretty false` 当前仍被既有测试类型问题阻断，错误集中在 `never[]` 推断、旧测试 Mock 返回类型、`HTMLElement.disabled` 和未导出的 `AppContextValue`，本轮新增组件无 TypeScript 错误。前端全量 Vitest 的既有 Discovery 测试夹具、MacroData 超时和 PortfolioWorkbench 旧参数断言仍需单独修复。

---

## 20. 数据健康与可观测性

### 20.1 必须展示

- DuckDB是否可连接；
- 每张原始表最新日期；
- 每个因子最新日期；
- 股票池总数；
- 有效数；
- 覆盖率；
- 填充率；
- 失败接口；
- 最近成功批次；
- 活动模型；
- 模型数据截止时间；
- 当前评分模式；
- 是否回退。

### 20.2 日志字段

统一：

    pipeline_run_id
    batch_id
    model_run_id
    calc_batch_id
    factor_code
    symbol_id
    trade_date
    source_key

### 20.3 健康状态

| 状态 | 条件 | 行为 |
|---|---|---|
| healthy | 核心数据覆盖和日期正常 | 正常动态评分 |
| warn | 个别非核心因子缺失 | 降低可信度，仍可影子计算 |
| degraded | 核心覆盖不足 | 使用上一个有效模型或手工评分 |
| failed | 仓库、因子或发布失败 | 不发布新评分 |

---

## 21. 部署与回滚

### 21.1 上线步骤

1. 备份 SQL 业务库。
2. 创建 DuckDB 目录并验证写权限。
3. 安装新增依赖。
4. 执行 Alembic 迁移。
5. 保持因子功能关闭、manual 模式启动。
6. 在“设置 → 因子模型”启用功能并初始化 DuckDB schema。
7. 确认功能已启用但决策模式仍为 manual。
8. 运行 K线镜像和第一批数据同步。
9. 运行因子回填。
10. 切换 shadow。
11. 至少完成一个稳定观察周期。
12. 完成回测和UAT。
13. 人工激活 ridge。

### 21.2 回滚

立即回滚只需：

    FACTOR_WEIGHT_MODE=manual

回滚时：

- 不删除 DuckDB；
- 不删除模型记录；
- 不修改历史动态 Score；
- 扫描恢复读取 manual 批次；
- 交易计划重新按 manual 评分生成；
- 保留失败批次供排查。

### 21.3 数据备份

- SQL 按现有方案备份；
- DuckDB 在无写事务时复制；
- 模型报告和配置快照进入 SQL；
- WxPusher token 不进入备份明文导出。

---

## 22. 免费数据风险与处理

| 风险 | 影响 | 落地处理 |
|---|---|---|
| 403/429/WAF | 批量同步失败 | 复用接口限速、随机延时、分批、退避 |
| 接口字段变化 | 解析失败或空值 | 契约测试、隔离区、字段别名、失败批次 |
| 历史估值不可回补 | F1训练长度不足 | stock_value_em探测；否则从上线日起沉淀，未达覆盖不入模 |
| 财报时间穿越 | 回测虚高 | 按公告日期生效 |
| 幸存者偏差 | 回测虚高 | 保存历史股票池快照；结果明确标注残余偏差 |
| Level-2缺失 | 无法做日内Alpha | 明确保持3～15日周期 |
| 宏观因子无截面差异 | Ridge伪特征 | 宏观用于regime和仓位，不作为普通截面特征 |
| 模型漂移 | 权重失真 | 每周训练、权重漂移报告、人工激活 |
| DuckDB并发写 | 锁冲突 | 单写锁、事务、读写阶段分离 |
| Windows路径/权限 | 仓库无法创建 | 可配置路径、启动健康检查 |
| 通知失败 | 用户未收到消息 | 不回滚评分，记录失败并可重试 |

---

## 23. 验收标准

### 23.1 数据层

- DuckDB 初始化成功；
- K线镜像可增量、可重复；
- 第一批六项数据可同步；
- 原始数据均有 source、batch_id、ingested_at；
- 财务数据有 announcement_date；
- 训练和回测期间无网络调用；
- 核心因子覆盖率可展示。

### 23.2 因子层

- 每日按截面计算；
- MAD和分位回退正确；
- std为0无Inf；
- 因子方向一致；
- 缺失填充可追溯；
- 相同输入得到相同结果；
- 公式和参数有版本。

### 23.3 模型层

- 标签与交易时点一致；
- 不随机拆分同一交易日；
- 无未来数据泄漏；
- beta保留符号；
- 权重、指标和数据截止时间可追溯；
- 模型门禁可阻止坏模型发布；
- 影子和正式模型可以并存。

### 23.4 业务层

- manual 模式结果与改造前一致；
- shadow 不影响当前扫描；
- ridge Score 可被现有 run_scan 使用；
- 交易计划继续应用组合规则；
- 宏观只降低仓位，不突破现有限额；
- 回测固定模型版本；
- 历史结果不随模型切换改变。

### 23.5 前端

- 设置页可查看和切换模式；
- 任务中心显示完整流水线；
- 今日决策显示模型和健康状态；
- 投资中心可解释单股贡献；
- 机会挖掘显示模型版本；
- 回测显示固定版本；
- 失败和回退清晰可见。

### 23.6 运维

- 每日流水线可自动运行；
- 重启不会重复发布；
- 失败不会覆盖旧模型；
- 一键回退 manual；
- WxPusher失败不影响主流程；
- 有生产SOP和回滚演练记录。

---

## 24. Definition of Done

只有同时满足以下条件，第一版才算完成：

1. 第一批六个指标完成真实数据同步和本地落库。
2. 截面因子计算和标签均有自动化测试。
3. Ridge训练产生可追溯模型和权重。
4. 影子模式至少完成稳定观察和回测。
5. manual 模式无功能回归。
6. 动态 Score 可进入现有扫描和交易计划。
7. 单股因子贡献能够解释。
8. 数据不足时可以自动降级。
9. 人工可以一键回退。
10. 白盒、黑盒、前端、E2E和UAT通过。
11. 生产SOP、数据字典和模型报告齐全。
12. 不存在模型训练或回测期间外发网络请求。

---

## 25. 实际开工顺序

必须按以下顺序实施：

1. Feature Flag 和配置；
2. DuckDB Store；
3. SQL模型和迁移；
4. K线镜像；
5. AkShare接口契约测试；
6. 第一批原始数据适配；
7. 因子计算；
8. 数据健康；
9. 标签；
10. Ridge；
11. 模型门禁；
12. 影子评分桥接；
13. 单股解释API；
14. 设置页和健康面板；
15. 机会挖掘与回测版本锁定；
16. 每日调度；
17. WxPusher；
18. 影子运行；
19. UAT；
20. 人工正式激活。

任何阶段失败，都不得跳过门禁直接进入正式动态评分。

---

## 26. 第一批开发任务建议

第一批提交只完成承载骨架，不同时实现全部因子：

### Commit 1：feature flag 与依赖

- requirements；
- config；
- 默认 manual；
- 启动健康检查。

### Commit 2：DuckDB Store 与迁移

- schema；
- transaction；
- upsert；
- lock；
- tests。

### Commit 3：K线镜像

- SQL读取；
- DuckDB写入；
- 增量；
- 对账；
- tests。

### Commit 4：模型元数据

- FactorVersion；
- FactorModelRun；
- FactorWeightSnapshot；
- Score扩展；
- Alembic；
- tests。

完成这四个提交后，再进入第二周因子数据开发。

---

## 27. 最终原则

本功能的价值不是让系统看起来更复杂，而是让每日决策更可信、更可解释、更可回退。

最终产品应满足：

    用户只看数据健康、候选、解释和交易计划；
    数据同步、因子计算、模型训练和版本管理全部在后台完成；
    任何新模型都不能破坏现有可用的手工评分闭环。

---

## 28. 生产操作 SOP

### 28.1 上线前提

上线窗口内必须同时满足：

1. SQL 业务库已完成可恢复备份。
2. FACTOR_WAREHOUSE_PATH 所在磁盘至少保留 20% 空闲空间。
3. 后端全量测试通过，或已形成与本功能无关的已知失败清单。
4. 前端生产包可以成功生成。
5. 首次上线保持 manual，禁止直接设置 ridge。
6. 生产操作人、复核人和回退负责人已明确。

### 28.2 环境配置

PowerShell 启动前配置（环境变量仅作为首次建库默认值，功能开关后续由设置页持久化管理）：

    $env:FACTOR_FEATURE_ENABLED="false"
    $env:FACTOR_WEIGHT_MODE="manual"
    $env:FACTOR_WAREHOUSE_PATH="D:\quant-data\factor_warehouse.duckdb"
    $env:WXPUSHER_ENABLED="false"

安装依赖：

    C:\Python312\python.exe -m pip install -r requirements.txt

启动后端：

    C:\Python312\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

检查 API：

    Invoke-RestMethod http://127.0.0.1:8000/api/v1/factors/overview
    Invoke-RestMethod http://127.0.0.1:8000/api/v1/factor-models/runtime

注意：

- FACTOR_WEIGHT_MODE 只决定尚无持久化状态时的初始值；
- FACTOR_FEATURE_ENABLED 只决定 factor_system_config 尚不存在时的初始值；
- 后续启用状态以“设置 → 因子模型”和 SQL 中 factor_system_config 为准；
- 一旦通过界面激活或回退，后续以 SQL 中 factor_runtime_state 为准；
- DuckDB 文件不得放在 Git 工作区的跟踪目录；
- WXPusher 当前仅有配置位，通知服务未完成前必须保持 false。

### 28.3 首次初始化

1. 打开“设置 → 基础数据”，完成 A 股股票池和日线初始化。
2. 在“设置 → 外部数据同步”完成估值、财报历史、近30日龙虎榜机构、今日人气榜、候选尾盘代理、主力资金和宏观数据同步；首次优先选择自选股或持仓，尾盘代理固定限制候选前20只。
3. 打开“设置 → 因子模型”。
4. 点击“启用因子功能”，确认功能状态变为“已启用”。
5. 点击“初始化仓库”，确认因子仓库变为“正常”；仓库未就绪时“运行流水线”保持禁用。
6. 保持“训练 Ridge”和“生成评分快照”开启，运行一次本地因子流水线。
7. 等待任务进入 done，检查：

   - 因子仓库为可用；
   - 最新交易日与本地 K 线一致；
   - 五个核心股票截面因子均产生覆盖率；龙虎榜稀疏因子只在真实机构上榜事件日产生有效值；
   - 新模型状态只能是 validated 或 rejected；
   - 当前决策模式仍为 manual。

8. 若模型为 rejected，记录拒绝原因，不得绕过门禁。
9. 若模型为 validated，点击“影子运行”。
10. 关闭“训练 Ridge”，再次运行流水线，为活动模型生成 shadow Score。
11. 在“今日决策”核对模式、模型、因子日期和平均覆盖率。
12. 在“投资中心”抽查至少 10 只股票的因子原值、Z-Score、系数、贡献、宏观状态和数据截止时间。

### 28.4 日常运行

交易日建议顺序：

1. 16:30 后完成行情、估值、资金流和宏观源同步；公告季或首次回填时先同步财报历史。
2. 18:10 后运行因子流水线。
3. 日常任务关闭“训练 Ridge”，只做镜像、因子、标签和活动模型评分快照。
4. 任务完成后检查：

   - status 为 done；
   - failed_count 为 0；
   - 因子最新日期未落后本地最新 K 线；
   - 活动模型 ID 与预期一致；
   - 今日决策仍能读取候选和交易计划。

5. 只有任务完成后才能启动当日机会扫描。
6. 任务失败时继续使用上一批有效 Score，不删除历史批次。

手工 API 触发示例：

    $body = @{
      full_refresh = $false
      train_model = $false
      materialize_scores = $true
      window_days = 250
      validation_days = 50
    } | ConvertTo-Json

    Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/factor-pipeline/tasks -ContentType "application/json" -Body $body

### 28.5 每周训练

每周最后一个交易日收盘数据同步完成后：

1. 开启“训练 Ridge”，运行流水线。
2. 新模型不会自动激活。
3. 检查模型数据截止时间、样本数、股票数、交易日数、validation_ic、validation_r2 和各因子 signed beta。
4. 比较上一活动模型的权重变化；单因子权重剧烈翻转必须说明原因。
5. validated 模型先进入 shadow，不直接进入 ridge。
6. rejected 模型只保留审计记录，不生成激活操作。

### 28.6 Shadow 验收门槛

建议至少观察 20 个交易日，并同时满足：

- DuckDB 连续可用且无损坏；
- 核心因子覆盖率不低于 70%，目标覆盖率不低于 90%；
- 因子日期不落后最新行情日；
- validation_ic 为有限值且通过代码门禁；
- shadow 与 manual 的候选差异可以解释；
- 含佣金、印花税和滑点的回测不出现不可接受的回撤恶化；
- 停牌、涨跌停和缺失数据场景未产生未来数据泄漏；
- 随机抽样单股贡献之和与模型 Alpha 一致；
- 回退演练能够立即恢复 manual 决策查询。

任何一项不满足，继续 shadow 或回退 manual。

### 28.7 正式激活 Ridge

正式激活必须人工完成：

1. 记录拟激活 model_run_id、数据截止时间、验证指标和审批结论。
2. 在设置页点击“正式启用”。
3. 再运行一次关闭训练的流水线，生成该模型固定版本的 ridge Score。
4. 核对今日决策中的运行模式和实际评分来源均为 ridge。
5. 运行一次机会扫描和回测，确认记录中固定：

   - score_weight_mode=ridge；
   - factor_model_run_id 为本次模型；
   - factor_data_cutoff_at 不晚于决策时点。

6. 交易计划的最终仓位仍受原组合规则限制；宏观乘数只能降低仓位。

### 28.8 一键回退

优先使用“设置 → 因子模型 → 回退手工”，必须填写原因。

API 回退示例：

    $body = @{
      actor = "local_operator"
      reason = "production rollback: describe the incident"
    } | ConvertTo-Json

    Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/factor-models/fallback -ContentType "application/json" -Body $body

回退后必须确认：

- runtime.weight_mode=manual；
- runtime.active_model_run_id=null；
- 扫描、预警、信号和交易计划重新读取 manual Score；
- 历史 shadow/ridge Score、模型、回测和审计记录均未删除；
- 故障 DuckDB 保留只读副本用于复盘。

### 28.9 故障处置

| 现象 | 立即动作 | 后续处理 |
|---|---|---|
| DuckDB 无法打开 | 回退 manual，停止流水线 | 检查路径、权限、磁盘和文件锁 |
| 因子覆盖率骤降 | 保持上一有效模型，不激活新模型 | 核对原始表最新日期和接口字段 |
| 模型 rejected | 不做激活 | 查看 rejection_reason 和 validation 指标 |
| 流水线 failed | 不重建历史 Score | 查看任务 errors、stage 和日志 |
| ridge 无可用 Score | 立即回退 manual | 用固定活动模型重跑评分快照 |
| 宏观数据缺失 | 保持中性/防守降级 | 补同步后重跑，不放大仓位 |
| 前端状态与后端不一致 | 以后端 runtime API 为准 | 刷新页面并检查活动状态版本 |
| AkShare 403/429 | 停止连续重试 | 延长随机等待，次批重跑失败源 |

### 28.10 备份与恢复

每日：

- 按现有方案备份 SQL 业务库；
- 保留最近一次成功任务 ID、活动模型 ID 和数据截止时间。

每周：

- 确认无 queued/running 因子任务；
- 停止写入后复制 factor_warehouse.duckdb；
- 对备份执行只读 health 检查；
- 保留至少两份跨周备份。

严禁在 DuckDB 写事务执行期间直接复制文件。

### 28.11 自动调度接线

当前版本不再为每个业务任务分别维护 Linux crontab 或 Windows 任务计划。全部业务计划保存在 SQL 业务库中，并通过同一设置页管理：

1. 打开“设置 → 定时任务”。
2. 查看系统初始化的 9 个默认计划；只有“每日行情增量同步”默认启用，其余默认关闭。
3. 使用开关启停计划；禁用计划不会删除配置和历史业务任务。
4. 点击“立即执行”可手工提交任何计划，包括当前已禁用的计划。
5. 点击编辑可修改每天/每周/间隔、时区、执行时间和任务参数；参数在保存时由后端业务 Schema 校验。
6. 在页面下方查看最近 100 条调度记录；实际业务进度和取消操作在“设置 → 任务中心”查看。

内置默认计划：

| 计划 | 默认时间 | 默认状态 |
|---|---:|---|
| 每日行情增量同步 | 18:00 Asia/Shanghai | 启用 |
| 每日候选尾盘代理 | 15:10 Asia/Shanghai | 关闭 |
| 每日人气榜快照 | 16:20 Asia/Shanghai | 关闭 |
| 每日龙虎榜机构同步 | 16:30 Asia/Shanghai | 关闭 |
| 每周财报历史同步 | 周六 10:00 Asia/Shanghai | 关闭 |
| 每日宏观数据更新 | 17:30 Asia/Shanghai | 关闭 |
| 每日因子评分 | 18:10 Asia/Shanghai | 关闭 |
| 每周因子训练 | 周五 18:30 Asia/Shanghai | 关闭 |
| 每日机会挖掘 | 18:40 Asia/Shanghai | 关闭 |

生产约束：

- 调度器随 FastAPI lifespan 启停，后端停止时不会在后台独立运行；
- Linux 推荐使用 systemd 保证后端随系统启动和异常重启，crontab 仅可用 `@reboot` 启动后端，不再逐项调用业务 API；
- Windows 推荐用“任务计划程序”的“系统启动时”触发器启动后端，同样不再为每个业务计划创建系统任务；
- 多 Worker 部署依靠数据库原子抢占防止同一到期计划重复提交；
- 后端停机期间错过的到期计划，在后端恢复后补触发一次，不逐次补跑所有历史间隔；
- 完成 WxPusher 服务前，不得把 `WXPUSHER_ENABLED=true` 视为已具备通知能力。

调度管理 API：

    GET    /api/v1/scheduled-tasks/definitions
    GET    /api/v1/scheduled-tasks
    POST   /api/v1/scheduled-tasks
    PATCH  /api/v1/scheduled-tasks/{id}
    DELETE /api/v1/scheduled-tasks/{id}
    POST   /api/v1/scheduled-tasks/{id}/run
    GET    /api/v1/scheduled-tasks/runs

### 28.12 用户日常入口

普通使用只需要三个入口：

1. “今日决策”：确认当前模式、活动模型、因子日期和覆盖率。
2. “机会挖掘”：查看经过活动评分范围过滤的候选。
3. “投资中心”：查看单股因子贡献、宏观仓位乘数和最终交易计划。

模型训练、激活和回退在“设置 → 因子模型”执行；自动计划和手工立即执行在“设置 → 定时任务”执行；业务任务的实时进度、错误和取消操作在“设置 → 任务中心”执行。
