# EasyXT 借鉴改良方案

> 本文档基于对开源项目 [EasyXT](https://github.com/quant-king299/EasyXT) 的架构分析,提炼出可借鉴的优点,结合本项目(dataAanlystNew)现状,形成最终改良方案。
>
> **状态**:规划阶段,暂不执行
> **更新日期**:2026-07-05
> **适用项目**:dataAanlystNew(量化分析工作台)

---

## 目录

- [一、背景与动机](#一背景与动机)
- [二、EasyXT 核心优点提炼](#二easyxt-核心优点提炼)
- [三、借鉴点总览与优先级](#三借鉴点总览与优先级)
- [四、P0 方案:数据源降级链 SourceChain 抽象](#四p0-方案数据源降级链-sourcechain-抽象)
- [五、P0 方案:TDX 本地数据源接入](#五p0-方案tdx-本地数据源接入)
- [六、P0 方案:adj_factor 本地复权缓存](#六p0-方案adj_factor-本地复权缓存)
- [七、P1 方案:DuckDB 行情加速层](#七p1-方案duckdb-行情加速层)
- [八、P1 方案:Tushare 可选备选源](#八p1-方案tushare-可选备选源)
- [九、P2 方案:模块化分层 + 核心逻辑独立库](#九p2-方案模块化分层--核心逻辑独立库)
- [十、P2 方案:工程规范与文档治理](#十p2-方案工程规范与文档治理)
- [十一、不借鉴的内容](#十一不借鉴的内容)
- [十二、落地路线图](#十二落地路线图)
- [十三、总体风险评估](#十三总体风险评估)
- [十四、验收标准汇总](#十四验收标准汇总)

---

## 一、背景与动机

### 1.1 本项目现状

本项目(dataAanlystNew)是一套**零门槛量化分析工作台**,核心能力:

- **选股**:机会挖掘(7000 标的全量扫描)
- **评分**:自定义指标公式中心
- **回测**:AST 沙箱隔离的回测引擎
- **模拟**:模拟账户交易
- **数据**:akshare 单一免费数据源

### 1.2 核心痛点

从 project_memory 记录的工程经验看,本项目最大的数据层痛点:

| 痛点 | 表现 | 影响 |
|---|---|---|
| **单源依赖** | 仅 akshare(东财/新浪/腾讯),无备选 | 单点故障 |
| **风控压力** | 东财 push2 接口 IP 频次风控,首次成功后短时间再调直接 RemoteDisconnected | 7000 标的全量同步易中断 |
| **风控黑盒** | 频率限制无明确数字,全靠试 | 难以做容量规划 |
| **行式存储瓶颈** | SQLite/MySQL 存 1050 万行 DailyBar,批量扫描慢 | 评分/回测性能受限 |
| **复权依赖接口** | 每次 sync 都传 adjust=qfq,触发东财服务端复权计算 | 重复消耗风控配额 |
| **历史数据漂移** | 前复权基准随最新价变,历史 close 被反复覆盖 | 回测结果不稳定 |

### 1.3 为什么借鉴 EasyXT

EasyXT 是一套成熟的 QMT 量化工具集,虽然本项目不打算引入 QMT 实盘,但 EasyXT 的**数据获取架构**(多源降级 + 本地缓存 + 增量下载)正好解决本项目痛点,且**零成本可借鉴**(不依赖 QMT 的部分)。

---

## 二、EasyXT 核心优点提炼

### 2.1 数据源体系(最值得借鉴)

```
QMT(券商通道,无限制)
  ↓ 失败
DuckDB 本地缓存(零网络)
  ↓ 未命中
Tushare(积分制,透明风控)
  ↓ 不可用
TDX 通达信本地文件(零网络零频率)
  ↓ 不可用
akshare/qstock(免费,有风控)
```

**核心思想**:本地缓存优先,多源降级兜底,免费源放最末。

### 2.2 DuckDB 列式加速

- 历史数据拉一次存本地,后续零网络调用
- 列式存储 + 向量化执行,批量读取比行式快 10×
- 可选依赖,无 DuckDB 时降级走主库

### 2.3 TDX 本地文件利用

- 通达信客户端免费,本地存 `.day` 二进制行情文件
- 零网络、零频率限制、零成本
- 适合批量历史数据读取

### 2.4 增量下载分类管理

- 复权因子 / 涨跌停 / 停复牌 / 申万行业分别增量下载
- 不全量重算,只拉缺失日期
- 复权因子本地缓存,本地复权优先

### 2.5 模块化分层

- 核心层(easy_xt)零 Web 依赖,可独立 pip install
- 应用层(101 因子平台)完全独立
- 示例层(strategies)依赖核心层

### 2.6 工程规范

- 硬编码账户 ID 清理到 `.env`
- `print()` 全量替换为 `logging`
- `unified_config.json` + `.env` 双轨配置,自动 fallback

---

## 三、借鉴点总览与优先级

| 编号 | 借鉴点 | 优先级 | 改动量 | 风险 | 收益 | 依赖 |
|---|---|---|---|---|---|---|
| 1 | 数据源降级链 SourceChain 抽象 | **P0** | 中 | 低 | 降级解耦,新增源零侵入 | 无 |
| 2 | TDX 本地数据源接入 | **P0** | 中 | 低 | 零网络兜底,缓解风控 | 借鉴点 1(可独立) |
| 3 | adj_factor 本地复权缓存 | **P0** | 中 | 中 | 风控降压 + 数据稳定 | 需接口调研 |
| 4 | DuckDB 行情加速层 | **P1** | 大 | 中 | 批量读取提速 10× | 无 |
| 5 | Tushare 可选备选源 | **P1** | 中 | 低 | 风控可预测 + 数据完整 | 借鉴点 1 |
| 6 | 模块化分层 + 核心库独立 | **P2** | 大 | 高 | 可 CLI / 可复用 | 无 |
| 7 | 工程规范与文档治理 | **P2** | 小 | 低 | 代码质量 + 上手成本 | 无 |

### 3.1 优先级判断原则

- **P0**:风险低、收益直接、解决核心痛点(风控)
- **P1**:收益明确但改动大,或视需求启动
- **P2**:长期价值,暂不紧急

---

## 四、P0 方案:数据源降级链 SourceChain 抽象

### 4.1 方案概述

把 [market_data.py:206-311](file:///d:/ai_project/dataAanlystNew/app/services/market_data.py) `_fetch_history` 内部硬编码的三层嵌套 if/try/except 降级逻辑,重构为**可配置的 SourceChain + adapter 模式**。每个数据源是独立 adapter,实现统一 `HistorySource` Protocol;降级链由 `SourceChain` 编排,配置层声明顺序,业务代码零感知。

### 4.2 现状问题

```python
# 当前 _fetch_history 结构(简化)
def _fetch_history(symbol, start, end, adjust):
    if region == "cn" and asset_type == "stock":
        try:
            return ak.stock_zh_a_hist(...)          # 源1
        except:
            try:
                return ak.stock_zh_a_daily(...)      # 源2
            except:
                return ak.stock_zh_a_hist_tx(...)    # 源3
    elif region == "cn" and asset_type == "etf":
        try:
            return ak.fund_etf_hist_em(...)          # 源1
        except:
            return ak.fund_etf_hist_sina(...)        # 源2
    elif region == "us":
        return ak.stock_us_daily(...)                # 单源,无降级
```

**问题**:
1. 降级逻辑与业务逻辑耦合,三层嵌套 try/except 共 6 处 `call_akshare_with_retry`
2. US 市场单源无降级,失败即全失败
3. 新增数据源要改 `_fetch_history`,违反开闭原则
4. 单元测试需 mock 多层嵌套,无法注入假 source 链

### 4.3 设计方案

#### 4.3.1 文件结构

```
app/services/market_data/
├── sources/
│   ├── __init__.py
│   ├── base.py              # HistorySource Protocol + SourceUnavailable
│   ├── em_source.py         # 东财源
│   ├── sina_source.py       # 新浪源
│   ├── tx_source.py         # 腾讯源
│   ├── tdx_source.py        # 通达信本地源(见方案五)
│   └── us_source.py         # 美股源
├── chain.py                 # SourceChain 编排器
├── registry.py              # 降级链配置 + source 注册
└── tdx_parser.py            # TDX 二进制解析器
```

#### 4.3.2 核心抽象

```python
# app/services/market_data/sources/base.py
from typing import Protocol
from datetime import date
import pandas as pd

class SourceUnavailable(Exception):
    """数据源不可用,触发降级链 next"""
    pass

class HistorySource(Protocol):
    """行情数据源统一接口"""
    name: str
    region: str               # cn / us / hk
    asset_types: list[str]    # stock / etf

    def fetch(self, symbol, start: date, end: date, adjust: str) -> pd.DataFrame:
        """返回标准化 DataFrame,失败抛 SourceUnavailable"""
        ...
```

#### 4.3.3 SourceChain 编排器

```python
# app/services/market_data/chain.py
class SourceChain:
    """降级链编排器:失败 next、成功短路"""
    def __init__(self, sources: list[HistorySource]):
        self._sources = sources

    def fetch(self, symbol, start, end, adjust) -> pd.DataFrame:
        last_error = None
        for source in self._sources:
            if not self._supports(source, symbol):
                continue
            try:
                logger.info("Trying source %s for %s", source.name, symbol.symbol)
                return source.fetch(symbol, start, end, adjust)
            except SourceUnavailable as e:
                logger.info("Source %s failed, trying next", source.name)
                last_error = e
                continue
        raise RuntimeError(f"All sources failed for {symbol.symbol}: {last_error}")
```

#### 4.3.4 配置化降级链

```python
# app/services/market_data/registry.py
_CHAIN_CONFIG = {
    "cn-stock": ["em", "sina", "tx", "tdx"],   # TDX 兜底
    "cn-etf":   ["em", "sina"],
    "us":       ["us_em"],
}

def get_chain(symbol) -> SourceChain:
    key = f"{region_from_market(symbol.market)}-{symbol.asset_type}"
    source_names = _CHAIN_CONFIG.get(key, [])
    sources = [_SOURCE_REGISTRY[n]() for n in source_names]
    return SourceChain(sources)
```

#### 4.3.5 业务侧调用

```python
# market_data.py 重构后
def _fetch_history(symbol, start_date, end_date, adjust) -> pd.DataFrame:
    chain = get_chain(symbol)
    with _proxy_bypass(), quiet_akshare_output():
        return chain.fetch(symbol, start_date, end_date, adjust)
```

### 4.4 实现步骤

| 步骤 | 内容 | 涉及文件 |
|---|---|---|
| 1 | 定义 `HistorySource` Protocol + `SourceUnavailable` | 新增 `sources/base.py` |
| 2 | 拆分 6 处 `call_akshare_with_retry` 为独立 adapter | 新增 `sources/em.py` / `sina.py` / `tx.py` / `us.py` |
| 3 | 实现 `SourceChain` 编排器 | 新增 `chain.py` |
| 4 | 配置化降级链 + source 注册 | 新增 `registry.py` |
| 5 | 重构 `_fetch_history` | 改 [market_data.py:206-311](file:///d:/ai_project/dataAanlystNew/app/services/market_data.py) |
| 6 | 单元测试 | 新增 `tests/test_whitebox_source_chain.py` |
| 7 | 回归测试 | 跑现有 `test_whitebox_universe_refresh` / `test_whitebox_discovery` |

### 4.5 优点

1. **降级逻辑解耦**:业务代码不再关心源数量和顺序
2. **可扩展**:US 市场加 yfinance 源、HK 市场加新源,只需注册 adapter
3. **可测试**:每个 source 独立单测,SourceChain 可注入 mock 链
4. **可配置**:降级顺序按需调整,无需改代码
5. **可观测**:每个 source 成功/失败可统一打点

### 4.6 风险点

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| 行为回归(降级顺序变化) | 中 | 严格回归测试 + 配置开关灰度 |
| adapter 标准化偏差 | 中 | `base.py` 统一校验函数,各 source 返回前过校验 |
| 配置漏配 | 低 | `get_chain` 兜底默认链 + warning |

### 4.7 验收标准

- [ ] `_fetch_history` 函数体不超过 10 行
- [ ] 新增数据源只需 1 个文件 + 1 行配置注册
- [ ] 现有所有白盒测试全绿,无行为回归
- [ ] SourceChain 单测覆盖:成功短路、全失败抛错、不支持 region 跳过、配置缺失兜底

---

## 五、P0 方案:TDX 本地数据源接入

### 5.1 方案概述

把通达信本地 `.day` 二进制文件作为 SourceChain 降级链的最末兜底源。零网络、零频率、零成本,需用户安装通达信客户端(可选,不装自动跳过)。

### 5.2 TDX 简介

- **通达信(TDX)**:免费看盘软件,行情数据下载到本地 `.day` 二进制文件
- **文件路径**:`vipdoc/sh/lday/sh600000.day`(上交所)/ `vipdoc/sz/lday/sz000001.day`(深交所)
- **文件格式**:每条记录 32 字节,大端序
  - `int32 date`(YYYYMMDD)
  - `int32 open`(分,需 /100)
  - `float32 high / low / close`
  - `float32 amount`
  - `int32 volume`
  - `int32 reserved`

### 5.3 设计方案

#### 5.3.1 二进制解析器

```python
# app/services/market_data/tdx_parser.py
import struct
from datetime import date, datetime
from pathlib import Path
import pandas as pd

RECORD_FORMAT = ">iiifffii"   # 大端序,8 个字段
RECORD_SIZE = struct.calcsize(RECORD_FORMAT)  # 32 字节

def parse_day_file(filepath: Path, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """解析 .day 文件,返回 DataFrame"""
    if not filepath.exists():
        return pd.DataFrame(columns=["trade_date", "open", "high", "low", "close", "amount", "volume"])

    records = []
    start_int = int(start.strftime("%Y%m%d")) if start else 0
    end_int = int(end.strftime("%Y%m%d")) if end else 99991231

    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(RECORD_SIZE)
            if len(chunk) < RECORD_SIZE:
                break
            date_int, open_p, high, low, close, amount, volume, _ = struct.unpack(RECORD_FORMAT, chunk)
            if date_int < start_int or date_int > end_int:
                continue
            records.append({
                "trade_date": datetime.strptime(str(date_int), "%Y%m%d").date(),
                "open": open_p / 100.0,
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "amount": float(amount),
                "volume": int(volume),
            })

    if not records:
        return pd.DataFrame(columns=["trade_date", "open", "high", "low", "close", "amount", "volume"])
    return pd.DataFrame(records).sort_values("trade_date").reset_index(drop=True)


def detect_tdx_path() -> str | None:
    """自动检测通达信安装路径:环境变量 > 注册表 > 常见路径"""
    import os
    env_path = os.getenv("TDX_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    # Windows 注册表检测 / 常见路径扫描(略)
    common_paths = [r"C:\new_tdx", r"D:\new_tdx", r"C:\通达信", r"D:\通达信"]
    for p in common_paths:
        if Path(p).exists() and (Path(p) / "vipdoc").exists():
            return p
    return None
```

#### 5.3.2 Source Adapter

```python
# app/services/market_data/sources/tdx_source.py
class TDXHistorySource:
    name = "tdx"
    region = "cn"
    asset_types = ["stock"]   # 首期仅股票,ETF 后续扩展

    def __init__(self, tdx_path: str | None = None):
        self.tdx_path = tdx_path or detect_tdx_path()
        self._available = bool(self.tdx_path and Path(self.tdx_path).exists())

    @property
    def available(self) -> bool:
        if not self._available:
            return False
        return (Path(self.tdx_path) / "vipdoc").exists()

    def fetch(self, symbol, start_date, end_date, adjust: str) -> pd.DataFrame:
        if not self.available:
            raise SourceUnavailable("TDX path not configured")
        code = symbol.symbol.replace(".", "").lower()
        prefix = code[:2]
        filepath = Path(self.tdx_path) / "vipdoc" / prefix / "lday" / f"{code}.day"
        if not filepath.exists():
            raise SourceUnavailable(f"TDX file not found: {filepath}")
        df = parse_day_file(filepath, start=start_date, end=end_date)
        if df.empty:
            raise SourceUnavailable(f"TDX no data in range: {filepath}")
        df["source"] = "tdx"
        df["adjust"] = ""   # TDX 返回不复权价
        return df
```

#### 5.3.3 集成到降级链

```python
# registry.py
_CHAIN_CONFIG = {
    "cn-stock": ["em", "sina", "tx", "tdx"],   # TDX 最末兜底
    "cn-etf":   ["em", "sina"],                 # ETF 暂不支持 TDX
}
```

### 5.4 实现步骤

| 步骤 | 内容 | 依赖 |
|---|---|---|
| 1 | 新建 `tdx_parser.py`,实现二进制解析 | 无 |
| 2 | 新建 `sources/base.py`,定义 Protocol | 无 |
| 3 | 新建 `sources/tdx_source.py`,实现 adapter | 步骤 1、2 |
| 4 | 配置管理:`.env` 加 `TDX_PATH` 说明 | 无 |
| 5 | 单元测试:无需真实 TDX,构造假 .day 文件 | 步骤 1-3 |
| 6 | 集成 SourceChain | 借鉴点 1 |
| 7 | 集成测试:真实通达信环境,交叉验证 | 步骤 5 |

### 5.5 优点

1. **零成本零门槛**:用户装免费通达信即可,不装自动跳过
2. **零网络零风控**:读本地文件,不触发任何 HTTP 风控
3. **读取极快**:7200 标的全量读取约 10 秒(HTTP 方式 30+ 分钟且触发风控)
4. **向下兼容**:不装通达信完全不受影响
5. **数据稳定**:不受 akshare 接口变更影响
6. **历史数据全**:通达信可下载近 30 年 A 股日线
7. **可独立测试**:二进制解析纯函数,无需网络无需 DB

### 5.6 风险点

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| 用户需手动装通达信 | 中 | 文档详细说明;不装也不影响现有功能 |
| 二进制格式解析正确性 | 高 | 单测覆盖 + 与 akshare 交叉验证 |
| 开盘价单位(分→元) | 中 | 单测断言价格区间合理性 |
| ETF 不支持 | 低 | 首期只支持股票,ETF 走 akshare |
| 复权因子缺失 | 中 | adjust 参数忽略;复权从其他源单独拉 |
| 数据时效性 | 低 | 文档提醒用户手动下载;TDX 在降级链最末 |
| Windows 依赖 | 低 | 本项目主要 Windows;Mac/Linux 自动跳过 |

### 5.7 验收标准

- [ ] `tdx_parser.py` 单测全绿(正常/空文件/不存在/日期过滤)
- [ ] `tdx_source.py` 单测全绿(可用/不可用/支持/不支持/读取成功/文件缺失)
- [ ] 真实环境集成测试:读取平安银行日线,与 akshare 交叉验证误差 < 0.01
- [ ] 未配置 TDX_PATH 时,所有现有功能不受影响
- [ ] 7200 标的全量读取 < 30 秒

---

## 六、P0 方案:adj_factor 本地复权缓存

### 6.1 方案概述

行情数据改为存储**原始不复权价**,复权因子单独存表(`AdjFactor`)。查询时按需本地复权,不依赖 akshare 的 `adjust` 参数。同步流程从"每次带 adjust=qfq"改为"拉不复权价 + 拉复权因子,分表存储"。

### 6.2 现状问题

- 每次 `sync_symbol_daily_bars` 都传 `adjust="qfq"`,触发东财服务端复权计算 + 风控计数
- 前复权基准随最新价漂移,历史 close 被反复覆盖更新
- 用户想切换前复权/后复权/不复权,必须重新拉接口

### 6.3 设计方案

#### 6.3.1 新增模型

```python
# app/models/adj_factor.py
class AdjFactor(Base):
    __tablename__ = "adj_factors"
    __table_args__ = (UniqueConstraint("symbol_id", "trade_date", name="uq_adj_factor"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    adj_factor: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="akshare")
```

#### 6.3.2 本地复权算法

```python
# app/services/market_data/adj.py
def local_adjust(bars: pd.DataFrame, adj_factors: pd.DataFrame, adjust: str) -> pd.DataFrame:
    """本地复权计算,不调接口
    adjust: "" 不复权 / "qfq" 前复权 / "hfq" 后复权
    """
    if adjust == "":
        return bars
    merged = bars.merge(adj_factors, on="trade_date", how="left")
    if adjust == "qfq":
        latest_factor = merged["adj_factor"].iloc[-1]
        ratio = merged["adj_factor"] / latest_factor
    elif adjust == "hfq":
        ratio = merged["adj_factor"]
    for col in ["open", "high", "low", "close"]:
        merged[col] = merged[col] * ratio
    return merged.drop(columns=["adj_factor"])
```

#### 6.3.3 同步流程改造

```python
def sync_symbol_daily_bars(db, symbol, start_date, end_date, adjust="qfq"):
    # 1. 同步原始不复权价
    raw_bars = _fetch_history(symbol, start, end, adjust="")
    _upsert_bars(db, symbol, raw_bars)

    # 2. 同步复权因子(单独表)
    adj_df = fetch_adj_factors(symbol, start, end)
    _upsert_adj_factors(db, symbol, adj_df)

    # 3. 查询时本地复权
    return {"inserted": ..., "updated": ...}
```

### 6.4 实现步骤

| 步骤 | 内容 |
|---|---|
| 1 | 新增 `AdjFactor` 模型 + Alembic 迁移 |
| 2 | 调研 akshare 复权因子获取方式(`stock_zh_a_daily` 是否返回 adj_factor) |
| 3 | 新增 `fetch_adj_factors` 服务 |
| 4 | 实现 `local_adjust` 本地复权算法 |
| 5 | 改造 `sync_symbol_daily_bars`:拉不复权价 + 拉复权因子 |
| 6 | 改造查询路径:评分/回测/前端读取时本地复权 |
| 7 | 数据迁移脚本:已存 qfq bar 回填 adj_factor(分批) |
| 8 | 测试:本地复权算法正确性 + 同步流程 + 迁移脚本 |

### 6.5 优点

1. **风控压力降低**:不复权价不触发服务端复权计算,预计调用频次降 50%+
2. **历史数据稳定**:原始价不变,无前复权基准漂移
3. **复权方式可切换**:用户可任意切换前复权/后复权/不复权,无需重新拉接口
4. **回测一致性**:复权基准固定,跨次回测结果一致

### 6.6 风险点

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| akshare 接口适配 | 高 | 步骤 2 先做接口调研,确认可行再推进 |
| ETF 复权因子缺失 | 中 | ETF 通常不分红,adj_factor 恒为 1;或 ETF 路径 fallback 到 adjust=qfq |
| 数据迁移成本 | 高 | 分批迁移(按 symbol_id 分片)+ 低峰期执行 + 失败隔离 |
| 查询路径改造面广 | 中 | 统一封装 `get_bars(adjust=...)` 函数 |
| 算法正确性 | 中 | 单测对比本地复权与 akshare adjust=qfq 结果,误差 < 1e-6 |
| 旧数据兼容 | 中 | bar 表加 `adjust_mode` 字段标记 none/qfq |

### 6.7 验收标准

- [ ] `AdjFactor` 表已建,复权因子可独立查询
- [ ] 本地复权结果与 akshare `adjust=qfq` 对比,误差 < 1e-6
- [ ] `sync_symbol_daily_bars` 不再向 akshare 传 `adjust="qfq"`
- [ ] 用户可在 API 层指定 `adjust=none/qfq/hfq`,无需重新同步
- [ ] 迁移脚本可断点续跑,失败 symbol 不影响其他

---

## 七、P1 方案:DuckDB 行情加速层

### 7.1 方案概述

引入 DuckDB 作为 `DailyBar` 的**只读列存镜像**,主库(SQLite/MySQL)仍是写入源。同步成功后异步镜像到 DuckDB;评分/回测等批量读取热路径优先走 DuckDB,无 DuckDB 时降级走主库。

### 7.2 DuckDB 是什么

- **定位**:分析型数据库的"SQLite"——嵌入式、零部署、列式存储
- **存储模型**:列式存储(OLAP 友好),区别于 SQLite/MySQL 的行式(OLTP)
- **查询引擎**:向量化执行,批量处理列数据
- **零部署**:`pip install duckdb` 即用,无需起服务

**为什么快**:行式存储读 close 列需扫全表,列式存储只读 close 列连续块,IO 量降 5-10×。

### 7.3 设计方案

```python
# app/services/market_data/duckdb_mirror.py
import duckdb

class DuckDBMirror:
    """DailyBar 的只读列存镜像,加速批量读取"""

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("DUCKDB_PATH")
        self.conn = duckdb.connect(self.path) if self.path else None
        if self.conn:
            self._ensure_schema()

    def sync_from_main_db(self, db: Session, since: date | None = None):
        """从主库增量镜像到 DuckDB"""
        # 增量同步逻辑
        pass

    def query_bars(self, symbol_ids: list[int], start: date, end: date) -> pd.DataFrame:
        """批量查询,列式读取,比 SQLite 快 10×"""
        if not self.conn:
            raise RuntimeError("DuckDB not configured")
        # 列式查询
        pass


# 使用:热路径降级
def get_bars_batch(symbol_ids: list[int], start, end) -> pd.DataFrame:
    mirror = DuckDBMirror()
    if mirror.conn:
        return mirror.query_bars(symbol_ids, start, end)  # 10× 加速
    return db.execute(select(DailyBar).where(...)).scalars().all()  # 降级走主库
```

### 7.4 实现步骤

| 步骤 | 内容 |
|---|---|
| 1 | 新增 `duckdb` 可选依赖 + `DUCKDB_PATH` 环境变量 |
| 2 | 实现 `DuckDBMirror` 服务:建表、增量镜像、批量查询 |
| 3 | `sync_symbol_daily_bars` 成功后异步触发镜像 |
| 4 | 改造热路径:评分计算 / 回测引擎,优先 DuckDB 降级主库 |
| 5 | 全量重建镜像管理接口 |
| 6 | 镜像一致性校验脚本 |
| 7 | 测试:镜像正确性、降级路径、并发写入加锁 |

### 7.5 优点

1. **批量读取提速 10×**:列式存储 + 向量化,7000 标的评分从分钟级降到秒级
2. **可选依赖**:无 DuckDB 时降级走主库,不影响现有功能
3. **写入路径不变**:主库仍是源,不引入写入复杂度
4. **回测加速明显**:多标的 × 多年数据批量加载是回测主瓶颈

### 7.6 风险点

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| DuckDB 并发写入限制 | 高 | `threading.Lock` 保护;异步队列串行化镜像 |
| 数据一致性窗口 | 中 | 查询时 DuckDB 无数据则降级走主库 |
| 首次镜像成本 | 中 | 低峰期执行 + 进度展示 + 可中断 |
| 依赖体积(50MB) | 低 | 可选依赖管理 |
| schema 漂移 | 中 | 镜像服务启动时校验 schema |
| 内存占用 | 中 | 查询时分批,避免一次性全量加载 |
| 回测结果一致性 | 中 | 对账测试:两库结果 diff 为空 |

### 7.7 验收标准

- [ ] 无 `DUCKDB_PATH` 时,所有功能正常降级
- [ ] 有 DuckDB 时,7000 标的评分批量读取提速 ≥ 5×
- [ ] DuckDB 与主库对账:行数一致,数据 diff 为空
- [ ] 镜像操作加锁,无并发写冲突
- [ ] 全量重建镜像可中断、可续跑

---

## 八、P1 方案:Tushare 可选备选源

### 8.1 方案概述

把 Tushare 作为 akshare 的备选源,用户配置 token 后启用,不配则跳过。Tushare 积分制风控透明,数据完整度高,可作为降级链的一环或复权因子的补充源。

### 8.2 Tushare 积分制

| 积分 | 能拉什么 | 频率限制 | 成本 |
|---|---|---|---|
| 120(免费注册) | 基础日线、股票列表 | 每分钟 200 次 | 0 元 |
| 2000 | 财务数据 | 每分钟 500 次 | ~150 元 |
| 5000 | 复权因子、涨跌停、停复牌、申万行业 | 每分钟 800 次 | ~400 元 |
| 6000 | 高级分钟线 | 每分钟 1000 次 | ~500 元 |

### 8.3 设计方案

```python
# app/services/market_data/sources/tushare_source.py
class TushareHistorySource:
    name = "tushare"
    region = "cn"
    asset_types = ["stock", "etf"]

    def __init__(self):
        self.token = os.getenv("TUSHARE_TOKEN")
        self._available = bool(self.token)
        if self._available:
            import tushare as ts
            ts.set_token(self.token)
            self.pro = ts.pro_api()

    def fetch(self, symbol, start, end, adjust):
        if not self._available:
            raise SourceUnavailable("Tushare token not configured")
        # 调用 tushare 接口
        pass
```

### 8.4 优点

1. **风控透明**:积分制,频率限制明确
2. **数据完整**:复权因子/涨跌停/停复牌等,akshare 缺失或字段不稳
3. **降级链更稳健**:比 sina 备用更可靠
4. **数据质量**:有校验,akshare 部分字段异常

### 8.5 风险点

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| 高级数据需付费 | 中 | 可选配置,免费档也可用基础日线 |
| 接口适配 | 中 | 先做接口调研 |
| 用户需注册 token | 低 | 文档说明,不配则跳过 |

### 8.6 验收标准

- [ ] 未配置 `TUSHARE_TOKEN` 时自动跳过
- [ ] 配置后可作为降级链一环
- [ ] 与 akshare 交叉验证,数据一致

---

## 九、P2 方案:模块化分层 + 核心逻辑独立库

### 9.1 方案概述

把"业务核心"(评分/回测/数据同步)与"Web 传输层"(FastAPI)分离。核心是纯 Python 库,通过 Repository 接口抽象数据访问;FastAPI 成为薄适配层。核心库可脱离 Web 框架独立使用(CLI 工具、其他项目复用)。

### 9.2 设计方案

```
app/
├── core_lib/              # 新增:纯 Python 核心库(零 Web 依赖)
│   ├── scoring/           # 评分引擎
│   ├── backtest/          # 回测引擎
│   ├── market_data/       # 行情服务
│   └── repository.py      # 仓储抽象(Protocol)
├── db/                    # SQLAlchemy 实现 core_lib.repository
├── api/                   # FastAPI 适配层(薄)
└── schemas/               # Pydantic DTO(仅 API 层)
```

### 9.3 优点

1. **可 CLI 化**:批量评分、数据同步可脚本化
2. **可复用**:其他项目可 pip install 核心库
3. **可测试性提升**:核心逻辑纯 mock repository 测试
4. **架构清晰**:Web 层薄、核心层纯

### 9.4 风险点

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| 改动量极大 | 高 | 分批迁移,每个服务一个 PR |
| 过度设计风险 | 高 | 仅在有 CLI 或复用需求时启动 |
| 接口抽象偏差 | 中 | 先迁 1-2 个服务验证设计 |
| 双轨维护成本 | 中 | 旧函数标记 deprecated,逐步切换 |

### 9.5 启动条件

**仅在以下情况启动**:
- 明确需要 CLI 工具(批量评分/同步脚本化)
- 其他项目需复用核心逻辑
- 核心逻辑测试痛点突出(需纯 mock)

---

## 十、P2 方案:工程规范与文档治理

### 10.1 方案概述

借鉴 EasyXT 的工程规范实践,提升本项目代码质量与文档完整度。

### 10.2 具体措施

#### 10.2.1 配置统一治理

- 扩展 `unified_config.json`,把散落的配置(akshare 重试参数、超时、扫描阈值等)统一管理
- 配置读取统一走 `get_config(key, fallback=...)`,fallback 到 `.env`

#### 10.2.2 logging 全量规范化

- 加 lint 规则(ruff/flake8 禁止 `print` 在 `app/` 下使用)
- CI 守护

#### 10.2.3 文档导航

- README 增加"我是谁,该从哪里开始"决策树
- 增加"数据同步速查表"(对应 EasyXT 的 Tushare 速查表)

### 10.3 优点

1. 配置治理,散落点收敛
2. 代码规范守护
3. 新用户上手成本降低

### 10.4 风险点

- 低风险,改动小

---

## 十一、不借鉴的内容

| 内容 | 原因 |
|---|---|
| xtquant 特殊版本强依赖 | 本项目无实盘交易需求,引入增加部署复杂度 |
| QMT 实盘交易模块 | 与本项目"模拟账户"定位冲突,风险过高 |
| xqshare 远程客户端 | 本项目无跨平台 QMT 需求 |
| PyQt GUI | 本项目 React 前端已成熟 |
| 雪球跟单/网格/通达信预警策略 | 依赖实盘交易,不适合本项目 |

---

## 十二、落地路线图

### 12.1 阶段划分

```
阶段 1(P0,低风险,立即启动)
  ├── 借鉴点 1:SourceChain 抽象
  └── 借鉴点 2:TDX 本地数据源接入
       ├── 纯重构,行为不变
       ├── 现有测试守护
       └── 为后续新增数据源铺路

阶段 2(P0,需调研)
  └── 借鉴点 3:adj_factor 本地复权缓存
       ├── 先做 akshare 接口调研
       ├── 确认可行后,模型 + 迁移 + 算法
       └── 数据迁移分批执行

阶段 3(P1,视需求)
  ├── 借鉴点 4:DuckDB 加速层(性能瓶颈明确时)
  └── 借鉴点 5:Tushare 备选源(风控压力持续大时)

阶段 4(P2,长期)
  ├── 借鉴点 6:模块化分层(CLI/复用需求明确时)
  └── 借鉴点 7:工程规范与文档治理(随时可做)
```

### 12.2 依赖关系

```
借鉴点 1(SourceChain) ──┬──→ 借鉴点 2(TDX)接入
                          ├──→ 借鉴点 5(Tushare)接入
                          └──→ 借鉴点 3(adj_factor)可独立

借鉴点 4(DuckDB) ──────── 独立,可单独做
借鉴点 6(分层) ────────── 独立,可单独做
借鉴点 7(规范) ────────── 独立,可随时做
```

### 12.3 建议执行顺序

1. **先做 SourceChain + TDX**:风险最低,收益直接(缓解风控)
2. **再做 adj_factor**:需接口调研,确认后推进
3. **视需求做 DuckDB/Tushare**:性能或风控压力明确时
4. **长期做分层/规范**:有明确需求时

---

## 十三、总体风险评估

### 13.1 共性风险

| 风险 | 等级 | 对策 |
|---|---|---|
| 回归风险 | 中 | 所有重构必须跑现有 463 个测试,任一失败即阻塞 |
| 风控触发风险 | 中 | 涉及 akshare 调用变更的,联网测试标记 `@pytest.mark.slow` |
| 数据迁移风险 | 高 | adj_factor 迁移必须断点续跑 + 失败隔离 + 低峰期执行 |
| 配置兼容 | 低 | 所有新源支持环境变量配置,支持灰度回滚 |

### 13.2 风险矩阵

| 借鉴点 | 改动量 | 风险 | 收益 | 建议 |
|---|---|---|---|---|
| SourceChain | 中 | 低 | 高 | ✅ 立即做 |
| TDX 源 | 中 | 低 | 高 | ✅ 立即做 |
| adj_factor | 中 | 中 | 高 | ⚠️ 调研后做 |
| DuckDB | 大 | 中 | 高 | ⏳ 视需求 |
| Tushare | 中 | 低 | 中 | ⏳ 视需求 |
| 分层 | 大 | 高 | 中 | ⏳ 视需求 |
| 规范 | 小 | 低 | 中 | ✅ 随时可做 |

---

## 十四、验收标准汇总

### 14.1 通用验收标准

- [ ] 现有所有白盒测试(260+)全绿,无行为回归
- [ ] 现有所有前端测试(216)全绿
- [ ] 现有所有黑盒测试(47)全绿
- [ ] 现有所有 E2E 测试(27)全绿
- [ ] 新增功能有对应单测覆盖
- [ ] 涉及联网的测试标记 `@pytest.mark.slow`

### 14.2 分项验收标准

| 借鉴点 | 核心验收标准 |
|---|---|
| SourceChain | `_fetch_history` 函数体 < 10 行;新增源仅需 1 文件 + 1 行配置 |
| TDX 源 | 7200 标的全量读取 < 30 秒;未配置 TDX_PATH 时自动跳过 |
| adj_factor | 本地复权与 akshare adjust=qfq 误差 < 1e-6;sync 不再传 adjust=qfq |
| DuckDB | 7000 标的评分提速 ≥ 5×;无 DUCKDB_PATH 时正常降级 |
| Tushare | 未配置 token 时自动跳过;配置后数据与 akshare 一致 |
| 分层 | `core_lib/` 无 FastAPI 导入;CLI 可独立运行 |
| 规范 | `app/` 下无 `print`;README 有决策树导航 |

---

## 附录:参考资料

- EasyXT 仓库:https://github.com/quant-king299/EasyXT
- DuckDB 官网:https://duckdb.org/
- Tushare 文档:https://tushare.pro/
- 通达信官网:http://www.tdx.com.cn/

---

**文档结束**

> 本文档为规划阶段产出,实际实施时需根据接口调研结果、性能测试数据、用户反馈等动态调整。
