# 投资中心导航重构设计文档

**日期**: 2026-06-21
**版本**: 1.1
**状态**: 已批准（修订版）
**方案**: 方案A - 新增独立整合页面（保留原有功能不变）

---

## 0. 核心变更说明（重要！）

### 本次重构的本质：**新增**而非**替换**

#### 最终导航结构

```
重构前:
一级导航: [目前观察池] [机会挖掘] [设置]
           └─ 二级标签: [工作台] [模拟交易]

重构后:
一级导航: [投资中心] [目前观察池] [机会挖掘] [设置]
           │            │
           │            └─ 二级标签: [工作台] [模拟交易] ← 完全保留，零改动
           │
           └─ 整合视图: 工作台(主视图) + 交易(右侧面板) ← 全新开发
```

#### 关键原则

1. **原有功能100%保留**
   - `目前观察池` 导航入口保留
   - `工作台` 和 `模拟交易` 二级标签保留
   - `PortfolioWorkbench.tsx` 组件代码 **不修改**
   - `Trading.tsx` 组件代码 **不修改**
   - 所有现有交互逻辑、样式、数据流保持原样

2. **全新开发"投资中心"页面**
   - 作为第4个一级导航项添加
   - 内部使用新的容器组件 `InvestmentCenter`
   - 复用（import）现有组件但不修改它们
   - 提供统一的整合视图体验

3. **用户可选模式**
   - 喜欢分离视图 → 继续使用"目前观察池"
   - 喜欢整合视图 → 使用新的"投资中心"
   - 两种模式数据同步，状态独立

4. **风险控制**
   - 零影响现有功能
   - 可随时隐藏新标签（一行配置）
   - 独立开发、测试、部署

---

## 1. 项目背景与目标

### 1.1 业务背景

当前系统导航结构为两级：
- **一级导航**：目前观察池 | 机会挖掘 | 设置
- **二级导航**（在"目前观察池"下）：工作台（最近计划）| 模拟交易（交易计划）

这种结构导致以下问题：
1. "交易计划"和"最近计划"功能分散，用户需要在二级标签间切换
2. 二级导航降低了功能的可见性和可访问性
3. 不符合部分用户对投资管理流程的直觉认知（研究→决策→执行应为一体）

**注意：上述问题不影响现有用户，只是为偏好整合体验的用户提供新选择。**

### 1.2 重构目标

**新增**一个独立的一级导航模块 **"投资中心"**，实现：

1. ✅ 提供整合的研究+执行一体化视图（可选）
2. ✅ 保持所有现有功能和数据完整性（原有功能零改动）
3. ✅ 为未来功能扩展预留架构空间
4. ✅ 确保跨设备响应式体验一致
5. ✅ 用户可自由选择使用新旧两种模式

### 1.3 设计原则

- **新增而非替换**：原功能100%保留，新功能作为增强选项
- **数据零迁移**：复用现有后端接口和数据模型
- **组件复用**：新页面import现有组件，不修改源码
- **可扩展性**：预留子模块扩展接口
- **响应式优先**：移动端和桌面端同等重视
- **可逆性**：可随时禁用新功能，无任何副作用

---

## 2. 技术架构分析

### 2.1 现有数据架构（无需修改）

**关键发现：两个模块已共享同一数据源**

#### 后端API接口
- **统一端点**: `GET /api/v1/dashboard/workbench`
- **文件位置**: `app/api/routes/dashboard.py` (第140-412行)
- **返回数据模型**: `DashboardWorkbench`

#### 数据模型结构 (types/index.ts 第256-276行)

```typescript
interface DashboardWorkbench {
  portfolio: {...};           // 组合信息
  active_rule: {...};         // 活动规则
  market_scope: {...};        // 市场范围
  overview: {...};            // 总览统计
  account_summary: {...};     // 账户概览 ← Trading使用
  latest_scan: {...};         // 最近扫描
  candidates: [...];          // 候选池 ← Workbench使用
  latest_scores: [...];       // 最新评分 ← Workbench使用
  positions: [...];           // 持仓列表 ← Trading使用
  watchlists: [...];          // 观察池列表 ← Workbench使用
  journals: [...];            // 日志记录
  recent_trades: [...];       // 最近成交 ← Trading使用
}
```

#### 数据使用分布

| 数据字段 | PortfolioWorkbench | Trading |
|---------|-------------------|---------|
| `candidates` | ✅ 主功能 | - |
| `latest_scores` | ✅ 主功能 | - |
| `overview` | ✅ 指标卡片 | - |
| `watchlists` | ✅ 侧边栏 | - |
| `account_summary` | ✅ 账户区域 | ✅ 主功能 |
| `positions` | - | ✅ 持仓表格 |
| `recent_trades` | - | ✅ 成交记录 |

**结论**: 无需任何后端修改或数据迁移，纯前端重构。

### 2.2 现有前端组件架构

```
App.tsx (主容器)
├── <nav> 一级导航按钮
│   ├── [目前观察池] → activeTab = "portfolio"
│   ├── [机会挖掘]   → activeTab = "discovery"
│   └── [设置]       → activeTab = "settings"
├── <main> 内容区域
│   └── {activeTab === "portfolio" && (
│       <header> 工具栏 (组合/语言/市场/代码/操作按钮)
│       <nav> 二级导航标签
│         ├── [工作台]    → activeSubTab = "portfolio-workbench"
│         └── [模拟交易]  → activeSubTab = "portfolio-trading"
│       {activeSubTab === "portfolio-workbench" && <PortfolioWorkbench />}
│       {activeSubTab === "portfolio-trading" && <Trading />}
│     )}
├── <DetailModal> 详情弹窗
└── <MetricModal> 指标弹窗
```

### 2.3 状态管理 (AppContext.tsx)

**现有状态字段**:
```typescript
interface AppState {
  activeTab: string;           // 一级导航: "portfolio" | "discovery" | "settings"
  activeSubTab: string;        // 二级导航: "portfolio-workbench" | "portfolio-trading"
  workbench: DashboardWorkbench | null;  // 共享数据源
  activeSymbolId: number | null;
  // ... 其他状态
}
```

---

## 3. 目标架构设计

### 3.1 新导航结构（在现有结构上新增）

```
# 完整导航结构（重构后）

一级导航: [投资中心] [目前观察池] [机会挖掘] [设置]
           │            │
           │            └─ InvestmentCenter 容器组件 (新增)
           │                ├── PortfolioWorkbench (复用，主视图)
           │                └── TradingPanel (基于Trading改造的右侧面板)
           │
           └─ 原有结构完全保留 (零改动)
               └─ 二级标签: [工作台] <PortfolioWorkbench> | [模拟交易] <Trading>
```

**重要说明：**
- `activeTab` 新增 `"investment"` 选项（默认值仍为 `"portfolio"`）
- 原 `activeTab === "portfolio"` 的所有逻辑保持不变
- 新增 `activeTab === "investment"` 分支处理

### 3.2 组件架构图

```
InvestmentCenter (新建容器组件)
│
├── State:
│   ├── panelOpen: boolean          // 面板开关状态
│   ├── panelWidth: number          // 面板宽度 (可拖拽)
│   └── mobileView: 'workbench' | 'trading'  // 移动端视图
│
├── Layout Container (响应式)
│   │
│   ├── [Desktop ≥1024px]
│   │   ├── <div className="ic-main">        // flex: 1
│   │   │   └── <PortfolioWorkbench />       // 复用组件
│   │   │
│   │   └── <aside className="ic-panel">     // width: panelWidth
│   │       ├── <div className="panel-header">
│   │       │   <h2>模拟交易</h2>
│   │       │   <button>收起</button>
│   │       └── <div className="panel-content">
│   │           └── <TradingPanel />          // 改造后的Trading
│   │
│   └── [Mobile <1024px]
│       ├── <div className="mobile-tabs">
│       │   ├── <button>工作台</button>
│       │   └── <button>交易</button>
│       └── <div className="mobile-content">
│           ├── {mobileView === 'workbench' && <PortfolioWorkbench />}
│           └── {mobileView === 'trading' && <TradingPanel />}
│
└── Floating Action Button (面板收起时显示)
    └── <button>展开交易面板</button>
```

### 3.3 组件职责定义

| 组件名 | 类型 | 职责 | 改动程度 |
|--------|------|------|----------|
| `InvestmentCenter` | 容器组件 | 布局管理、面板状态、响应式切换 | **新建** |
| `PortfolioWorkbench` | 展示组件 | 候选池、评分、机会、指标（保持不变） | **微调** (~10行) |
| `TradingPanel` | 展示组件 | 账户概览、下单、持仓、成交（改造为面板模式） | **中等改造** (~60行) |
| `App.tsx` | 主组件 | 导航逻辑更新、集成新组件 | **小改** (~40行) |

---

## 4. 详细设计方案

### 4.1 导航结构变更（新增，不删除）

#### App.tsx 导航配置修改

**修改前 (第106-125行)**:
```tsx
<nav className="view-tabs" aria-label="Main views">
  <button onClick={() => ctx.setActiveTab("portfolio")}>
    {t("tabPortfolio")}  {/* 目前观察池 */}
  </button>
  <button onClick={() => ctx.setActiveTab("discovery")}>
    {t("tabDiscovery")}
  </button>
  <button onClick={() => ctx.setActiveTab("settings")}>
    {t("tabSettings")}
  </button>
</nav>
```

**修改后** (在开头新增一个按钮):
```tsx
<nav className="view-tabs" aria-label="Main views">
  {/* ✨ 新增：投资中心导航 */}
  <button
    className={`view-tab${ctx.activeTab === "investment" ? " active" : ""}`}
    onClick={() => ctx.setActiveTab("investment")}
  >
    {t("tabInvestmentCenter")}  {/* 投资中心 */}
  </button>

  {/* 原有导航保持不变 */}
  <button
    className={`view-tab${ctx.activeTab === "portfolio" ? " active" : ""}`}
    onClick={() => ctx.setActiveTab("portfolio")}
  >
    {t("tabPortfolio")}  {/* 目前观察池 - 保留 */}
  </button>
  <button
    className={`view-tab${ctx.activeTab === "discovery" ? " active" : ""}`}
    onClick={() => ctx.setActiveTab("discovery")}
  >
    {t("tabDiscovery")}
  </button>
  <button
    className={`view-tab${ctx.activeTab === "settings" ? " active" : ""}`}
    onClick={() => ctx.setActiveTab("settings")}
  >
    {t("tabSettings")}
  </button>
</nav>
```

**变更点**:
- ✅ **新增** `tabInvestmentCenter` (投资中心) - 放在第一个位置
- ✅ **保留** `tabPortfolio` (目前观察池) - 原样不动
- ✅ **不删除任何现有导航项**
- ✅ 默认激活项仍为 `"portfolio"` (或可配置为 `"investment"`)

#### 新增内容渲染逻辑

**在 App.tsx 的 `<main>` 区域新增分支**:

```tsx
<main className="layout">
  {/* ✨ 新增：投资中心整合视图 */}
  {ctx.activeTab === "investment" && (
    <InvestmentCenter openMetricModal={openMetricModal} />
  )}

  {/* 原有逻辑完全保留 */}
  {ctx.activeTab === "portfolio" && (
    <div className="tab-container" data-tab-content="portfolio">
      {/* ... 所有原有代码保持不变 ... */}
      <header className="topbar">...</header>
      <nav className="sub-tabs">...</nav>  {/* 二级标签保留 */}
      {ctx.activeSubTab === "portfolio-workbench" && <PortfolioWorkbench />}
      {ctx.activeSubTab === "portfolio-trading" && <Trading />}
    </div>
  )}

  {ctx.activeTab === "discovery" && <Discovery />}
  {ctx.activeTab === "settings" && <Settings />}
</main>
```

**关键：原有 `{ctx.activeTab === "portfolio"}` 分支内的代码一行不改！**

#### 不需要做的改动

❌ **不需要删除**二级导航 (`<nav className="sub-tabs">`)
❌ **不需要修改** `PortfolioWorkbench.tsx`
❌ **不需要修改** `Trading.tsx`
❌ **不需要改变** `activeSubTab` 的默认值或逻辑

### 4.2 InvestmentCenter 容器组件设计

#### 文件位置
`src/components/InvestmentCenter.tsx`

#### 接口定义
```typescript
interface InvestmentCenterProps {
  openMetricModal: (type: string) => void;  // 从App传入
}

interface InvestmentCenterState {
  panelOpen: boolean;        // 面板是否展开 (default: true)
  panelWidth: number;        // 面板宽度像素 (default: 420, range: 300-600)
  isDragging: boolean;       // 是否正在拖拽调整宽度
  mobileActiveTab: 'workbench' | 'trading';  // 移动端当前视图
}
```

#### 核心逻辑伪代码
```tsx
function InvestmentCenter({ openMetricModal }: InvestmentCenterProps) {
  const [panelOpen, setPanelOpen] = useState(true);
  const [panelWidth, setPanelWidth] = useState(420);
  const [mobileActiveTab, setMobileActiveTab] = useState<'workbench' | 'trading'>('workbench');

  const isMobile = useMediaQuery('(max-width: 1023px)');

  return (
    <div className="investment-center">
      {/* 桌面端布局 */}
      {!isMobile ? (
        <div className="ic-desktop-layout">
          <div className="ic-main-area" style={{ flex: 1 }}>
            <PortfolioWorkbench openMetricModal={openMetricModal} />
          </div>

          {panelOpen && (
            <aside
              className="ic-trading-panel"
              style={{ width: panelWidth }}
            >
              <div className="panel-header">
                <h2>{t('模拟交易')}</h2>
                <button onClick={() => setPanelOpen(false)}>收起</button>
              </div>
              <div className="panel-resize-handle" />
              <TradingPanel />
            </aside>
          )}

          {!panelOpen && (
            <FloatingButton onClick={() => setPanelOpen(true)}>
              展开交易
            </FloatingButton>
          )}
        </div>
      ) : (
        /* 移动端布局 */
        <div className="ic-mobile-layout">
          <div className="mobile-tab-bar">
            <button
              className={mobileActiveTab === 'workbench' ? 'active' : ''}
              onClick={() => setMobileActiveTab('workbench')}
            >
              工作台
            </button>
            <button
              className={mobileActiveTab === 'trading' ? 'active' : ''}
              onClick={() => setMobileActiveTab('trading')}
            >
              交易
            </button>
          </div>

          <div className="mobile-content">
            {mobileActiveTab === 'workbench' ? (
              <PortfolioWorkbench openMetricModal={openMetricModal} />
            ) : (
              <TradingPanel />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
```

### 4.3 TradingPanel 改造方案

#### 改造目标
将现有的 `Trading.tsx` 页面组件改造为面板组件，适配420px宽度的侧边栏。

#### 改造要点

1. **移除外层容器**
   - 删除 `<div className="sub-tab-container" data-sub-content="portfolio-trading">`
   - 改为接收父容器的布局约束

2. **添加面板头部**
   ```tsx
   {/* 新增面板头部 - 由InvestmentCenter提供，不在TradingPanel内部 */}
   <div className="panel-header">
     <h2>{t('模拟交易')}</h2>
     <button onClick={onClose}>✕</button>
   </div>
   ```

3. **调整布局为垂直滚动**
   ```css
   .trading-panel-content {
     display: flex;
     flex-direction: column;
     gap: 16px;
     height: 100%;
     overflow-y: auto;
     padding: 16px;
   }

   /* 账户概览 - 保持原有样式 */
   .account-band { /* 不变 */ }

   /* 下单面板 + 持仓表格 - 改为上下排列 */
   .trading-columns {
     flex-direction: column;  /* 原来是row */
   }

   .trading-order { width: 100%; }
   .trading-positions { width: 100%; }

   /* 成交记录 - 保持原有样式 */
   .trade-history-band { /* 不变 */ }
   ```

4. **优化表格显示**
   - 持仓表格：隐藏次要列（如"权重"），或启用横向滚动
   - 数值格式：适当缩小字体（14px → 13px）
   - 操作按钮：保持大小不变，确保可点击性

5. **保留所有业务逻辑**
   - 下单逻辑 (`handleBuy`, `handleSell`)
   - 快速仓位按钮 (25%, 33%, 50%, All)
   - 持仓点击跳转详情
   - 所有数据绑定和状态同步

#### 文件处理选项

**选项A（推荐）**: 直接改造 `Trading.tsx`
- 通过props接收 `isPanelMode` 标志
- 条件渲染不同的外层容器
- 保持单一文件，降低维护成本

**选项B**: 创建新文件 `TradingPanel.tsx`
- 复制Trading代码并修改
- 解耦但增加代码重复
- 适合未来两个场景差异较大的情况

**建议**: 选择选项A，通过 `isPanelMode` prop控制样式差异。

### 4.4 PortfolioWorkbench 微调

#### 改动范围
最小化改动，仅调整以下几点：

1. **工具栏上移至 InvestmentCenter**
   - 将 `<header className="topbar">` 从 PortfolioWorkbench 移到 InvestmentCenter
   - 或保持原位（推荐，减少改动）

2. **响应式适配**
   - 当面板展开时，主视图自动适应剩余宽度
   - 确保候选池表格在小宽度下不变形（已有 `.table-wrap` 横向滚动）

3. **无功能性改动**
   - 所有业务逻辑保持不变
   - 数据流保持不变
   - 事件处理保持不变

### 4.5 AppContext 状态扩展

#### 新增状态字段

```typescript
// 在 AppState interface 中新增
interface AppState {
  // ... 现有字段
  tradingPanelOpen: boolean;  // 新增：全局面板状态（可选）
}
```

**实现方式选择**:

**方案1（推荐）**: 状态保持在 InvestmentCenter 内部
- 优点：组件自治，不影响全局状态
- 缺点：无法从外部控制面板状态

**方案2**: 提升到 AppContext
- 优点：可从任意位置控制面板（如详情弹窗触发）
- 缺点：增加全局状态复杂度

**初始实现**: 采用方案1，后续按需升级到方案2。

### 4.6 i18n 国际化配置

#### 新增翻译键

**中文 (zh-CN)**:
```typescript
tabInvestmentCenter: "投资中心",  // 新增
panelToggleExpand: "展开交易面板",
panelToggleCollapse: "收起面板",
tradingPanelTitle: "模拟交易",
mobileTabWorkbench: "工作台",
mobileTabTrading: "交易",
```

**英文 (en-US)**:
```typescript
tabInvestmentCenter: "Investment Center",
panelToggleExpand: "Open Trading Panel",
panelToggleCollapse: "Collapse Panel",
tradingPanelTitle: "Simulated Trading",
mobileTabWorkbench: "Workbench",
mobileTabTrading: "Trading",
```

#### 文件位置
`src/i18n/index.ts` - 在现有 I18N 对象中添加上述键值对

---

## 5. 样式设计规范

### 5.1 CSS类命名约定

采用 BEM (Block Element Methodology) 命名规范：

```
.ic {}                          /* Block: Investment Center */
.ic__desktop-layout {}          /* Element: 桌面端布局 */
.ic__mobile-layout {}           /* Element: 移动端布局 */
.ic__main-area {}               /* Element: 主内容区 */
.ic__trading-panel {}           /* Element: 交易面板 */
.ic__panel-header {}            /* Element: 面板头部 */
.ic__panel-resize-handle {}     /* Element: 拖拽手柄 */
.ic__floating-btn {}            /* Element: 浮动按钮 */
.ic__mobile-tab-bar {}          /* Element: 移动端标签栏 */

/* 修饰符 */
.ic__trading-panel--open {}
.ic__trading-panel--collapsed {}
.ic__mobile-tab--active {}
```

### 5.2 关键样式规则

#### 容器布局
```css
.investment-center {
  display: flex;
  flex-direction: column;
  height: 100%;
  position: relative;
}

/* 桌面端：左右布局 */
.ic__desktop-layout {
  display: flex;
  flex: 1;
  overflow: hidden;
}

.ic__main-area {
  flex: 1;
  min-width: 0;  /* 允许flex子项收缩 */
  overflow-x: auto;
}

/* 交易面板 */
.ic__trading-panel {
  width: 420px;
  min-width: 300px;
  max-width: 600px;
  border-left: 1px solid var(--border-color, #e0e0e0);
  background: var(--bg-secondary, #fafafa);
  display: flex;
  flex-direction: column;
  transition: width 0.2s ease, transform 0.3s ease;
  box-shadow: -2px 0 8px rgba(0, 0, 0, 0.05);
}

/* 面板头部 */
.ic__panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-color, #e0e0e0);
  background: var(--bg-primary, #fff);
}

.ic__panel-header h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}

/* 拖拽手柄 */
.ic__panel-resize-handle {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 4px;
  cursor: col-resize;
  background: transparent;
  transition: background 0.2s;

  &:hover,
  &:active {
    background: var(--primary-color, #1890ff);
  }
}

/* 浮动按钮 */
.ic__floating-btn {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 100;
  /* 使用Ant Design Button样式 */
}
```

#### 移动端布局
```css
@media (max-width: 1023px) {
  .ic__mobile-layout {
    display: flex;
    flex-direction: column;
    flex: 1;
    overflow: hidden;
  }

  .ic__mobile-tab-bar {
    display: flex;
    border-bottom: 1px solid var(--border-color, #e0e0e0);
    background: var(--bg-primary, #fff);
    flex-shrink: 0;
  }

  .ic__mobile-tab {
    flex: 1;
    padding: 12px;
    text-align: center;
    font-size: 15px;
    font-weight: 500;
    color: var(--text-secondary, #666);
    border: none;
    background: none;
    cursor: pointer;
    transition: color 0.2s, border-color 0.2s;

    &--active {
      color: var(--primary-color, #1890ff);
      border-bottom: 2px solid var(--primary-color, #1890ff);
    }
  }

  .ic__mobile-content {
    flex: 1;
    overflow-y: auto;
    padding: 0;
  }
}
```

#### 面板动画
```css
/* 展开动画 */
@keyframes slideInRight {
  from {
    transform: translateX(100%);
    opacity: 0;
  }
  to {
    transform: translateX(0);
    opacity: 1;
  }
}

/* 收起动画 */
@keyframes slideOutRight {
  from {
    transform: translateX(0);
    opacity: 1;
  }
  to {
    transform: translateX(100%);
    opacity: 0;
  }
}

.ic__trading-panel {
  &.animate-in {
    animation: slideInRight 0.3s ease forwards;
  }

  &.animate-out {
    animation: slideOutRight 0.3s ease forwards;
  }
}
```

### 5.3 视觉一致性检查清单

- [ ] 字体：与现有 `.view-tab`, `.panel-head h2` 保持一致
- [ ] 颜色：使用CSS变量 `--primary-color`, `--border-color`, `--bg-*`
- [ ] 间距：遵循8px网格系统 (8, 16, 24, 32px)
- [ ] 圆角：与现有 `.panel` 一致 (通常 4-8px)
- [ ] 阴影：面板使用轻微阴影增强层次感
- [ ] 边框：1px solid #e0e0e0 或使用CSS变量
- [ ] 交互反馈：hover/active状态颜色变化
- [ ] 过渡动画：0.2-0.3s ease

---

## 6. 响应式设计策略

### 6.1 断点定义

| 断点范围 | 设备类型 | 布局模式 | 面板行为 |
|----------|----------|----------|----------|
| ≥1280px | 桌面端 | 左右分栏 | 右侧固定面板 (420px) |
| 1024-1279px | 小桌面/平板 | 左右分栏 | 右侧自适应面板 (350px) |
| 768-1023px | 平板竖屏 | 标签页切换 | 全屏切换视图 |
| <768px | 手机 | 标签页切换 | 全屏切换视图 |

### 6.2 各断点详细规格

#### 桌面端 (≥1280px)
```
布局: Flexbox row
主视图: flex: 1 (min-width: 768px)
面板: width: 420px (fixed)
间距: 0 (紧密贴合)
```

**用户体验**:
- 同时查看研究和交易信息
- 面板支持拖拽调整宽度 (300-600px)
- 点击持仓行可在主视图打开详情
- 面板可收起以获得更大研究视野

#### 平板横屏 (1024-1279px)
```
布局: Flexbox row
主视图: flex: 1 (min-width: 600px)
面板: width: 350px (fixed)
字体: 适度缩小 (-1px)
```

**适配措施**:
- 减少面板内边距 (16px → 12px)
- 表格字号缩小 (14px → 13px)
- 隐藏部分次要列（如"权重%"）
- 卡片间距缩小

#### 移动端 (<1024px)
```
布局: Column (垂直堆叠)
导航: Tab Bar (顶部固定)
内容: 全屏单视图
手势: 左右滑动切换 (可选)
```

**移动端特殊处理**:
1. **标签页导航**
   - 固定在顶部
   - 显示当前视图标题
   - 支持滑动切换手势

2. **工作台视图优化**
   - 今日机会：单列显示
   - 指标卡片：2列网格 → 单列堆叠
   - 候选池表格：横向滚动 + 固定首列

3. **交易视图优化**
   - 账户概览：2x3网格保持
   - 下单表单：全宽输入框
   - 持仓表格：简化列（只显示标的、数量、盈亏）
   - 快速仓位按钮：横向排列

### 6.3 图片与媒体资源

本重构不涉及新的图片资源，完全基于CSS和现有图标（Ant Design Icons）。

---

## 7. 交互设计细节

### 7.1 面板操作流程

#### 展开/收起流程
```
用户点击 [收起] 按钮
    ↓
面板触发 animate-out 动画 (0.3s)
    ↓
动画结束后:
  - 设置 panelOpen = false
  - 从DOM中移除面板元素 (或 display: none)
  - 显示浮动按钮 [→ 展开交易]
    ↓
用户点击浮动按钮
    ↓
面板插入DOM / display: block
    ↓
触发 animate-in 动画 (0.3s)
    ↓
设置 panelOpen = true
```

#### 拖拽调整宽度流程
```
鼠标在 resize-handle 区域按下
    ↓
设置 isDragging = true
记录起始鼠标X坐标和当前面板宽度
    ↓
鼠标移动 (mousemove)
    ↓
计算新宽度 = 起始宽度 + (当前鼠标X - 起始鼠标X)
限制范围: 300px ≤ 新宽度 ≤ 600px
实时更新 panelWidth state
    ↓
鼠标释放 (mouseup)
    ↓
设置 isDragging = false
保存最终宽度到 localStorage (可选)
```

### 7.2 键盘无障碍支持

- **Tab键**: 在面板内元素间正常导航
- **Escape键**: 收起面板
- **Focus Trap**: 面板展开时焦点 trapped 在面板内
- **ARIA属性**:
  ```html
  <aside
    class="ic__trading-panel"
    role="complementary"
    aria-label="交易面板"
    aria-expanded={panelOpen}
  >
    <button aria-label="收起交易面板">✕</button>
  </aside>

  <button
    class="ic__floating-btn"
    aria-label="展开交易面板"
    aria-controls="trading-panel"
  >
    展开交易
  </button>
  ```

### 7.3 性能优化策略

1. **React.memo**
   - `TradingPanel` 使用 `React.memo()` 包裹
   - 只有 `ctx.workbench` 或 `ctx.activeSymbolId` 变化时重渲染

2. **虚拟化列表**
   - 持仓表格超过20行时考虑使用 `react-window`
   - 初始版本暂不需要（数据量通常<50条）

3. **懒加载**
   - 面板首次展开时再加载数据（如果需要额外请求）
   - 当前版本：数据已随workbench加载，无需额外请求

4. **防抖节流**
   - 拖拽调整宽度使用 `requestAnimationFrame` 节流
   - 面板尺寸变化事件防抖 (100ms)

5. **CSS性能**
   - 使用 `transform` 和 `opacity` 实现动画（GPU加速）
   - 避免 `layout thrashing`（读写DOM交替）

---

## 8. 扩展性设计

### 8.1 预留扩展接口

考虑到"投资中心"作为一级目录需要持续完善和拓展，设计以下扩展机制：

#### 子模块插槽机制
```typescript
interface InvestmentCenterConfig {
  panels: Array<{
    id: string;
    title: string;
    component: React.ComponentType<any>;
    defaultWidth?: number;
    minWidth?: number;
    maxWidth?: number;
    icon?: string;
  }>;
}

// 未来可通过配置添加新面板
const config: InvestmentCenterConfig = {
  panels: [
    {
      id: 'trading',
      title: '模拟交易',
      component: TradingPanel,
      defaultWidth: 420,
    },
    // 未来扩展示例:
    // {
    //   id: 'analytics',
    //   title: '数据分析',
    //   component: AnalyticsPanel,
    //   defaultWidth: 500,
    // },
  ];
};
```

#### 多面板支持（Phase 2规划）
```
当前:  [主视图] [交易面板]

未来:  [主视图] [交易面板] [分析面板] [日志面板]
                        ↑ 可拖拽排序的面板组
```

**实现思路**:
- 使用 `@dnd-kit` 或 `react-beautiful-dnd` 实现面板拖拽排序
- 面板状态持久化到 `localStorage`
- 支持动态注册/注销面板

#### 布局模板系统（Phase 3规划）
```typescript
type LayoutTemplate =
  | 'single-panel'      // 单面板 (当前)
  | 'dual-panel'        // 双面板并列
  | 'triple-column'     // 三栏布局
  | 'custom';           // 用户自定义

// 用户可选择预设布局或自定义
```

### 8.2 状态管理扩展准备

当前状态在组件内部管理，未来可能需要：

1. **全局面板状态** (AppContext)
   ```typescript
   interface AppState {
     investmentCenter: {
       activePanels: string[];        // 激活的面板ID列表
       panelSizes: Record<string, number>;  // 各面板宽度
       layoutTemplate: LayoutTemplate;
     };
   }
   ```

2. **URL状态同步** (可选)
   ```
   /investment?panels=trading&width=420&view=workbench
   ```
   - 支持书签和分享特定视图状态
   - 使用 `react-router` query params

3. **用户偏好持久化**
   ```typescript
   // localStorage keys
   'ic_panel_open': boolean
   'ic_panel_width': number
   'ic_layout_template': string
   'ic_mobile_default_view': string
   ```

### 8.3 API扩展预留

虽然当前无需后端改动，但预留扩展点：

1. **独立面板数据加载**
   - 未来某些面板可能需要专用API
   - 在 `InvestmentCenter` 中预留 `usePanelData(panelId)` hook

2. **WebSocket实时推送**
   - 持仓数据和价格变动可通过WebSocket推送
   - 面板组件预留 `useRealTimeData()` hook

---

## 9. 测试策略

### 9.1 功能测试用例

#### 导航切换测试
- [ ] 点击"投资中心"标签正确激活
- [ ] 页面默认显示工作台主视图+展开的交易面板
- [ ] 切换到其他标签再切回，状态保持（面板展开/收起）
- [ ] URL hash/state 更新正确（如果实现）

#### 面板交互测试
- [ ] 点击"收起"按钮，面板平滑关闭
- [ ] 面板关闭后显示浮动按钮
- [ ] 点击浮动按钮，面板平滑展开
- [ ] 拖拽调整面板宽度，范围限制300-600px
- [ ] 拖拽过程中主视图实时自适应
- [ ] 按Escape键关闭面板

#### 数据完整性测试
- [ ] 工作台显示完整的候选池数据
- [ ] 工作台评分看板数据正确
- [ ] 交易面板账户概览数据正确
- [ ] 交易面板持仓列表完整显示
- [ ] 交易面板下单功能正常（买入/卖出）
- [ ] 交易面板成交记录显示正确
- [ ] 点击工作台中标的，交易面板联动更新
- [ ] 点击交易面板持仓行，工作台显示对应详情

#### 状态同步测试
- [ ] 刷新页面后数据重新加载
- [ ] 扫描完成后两个视图同时更新
- [ ] 下单成功后账户信息和持仓列表同步刷新
- [ ] 切换组合（Select）后所有数据重置

### 9.2 响应式布局测试

#### 桌面端测试 (Chrome DevTools, 1280px+)
- [ ] 面板默认宽度420px
- [ ] 主视图和面板无重叠
- [ ] 窗口resize时布局自适应
- [ ] 面板收起后主视图占满宽度
- [ ] 表格横向滚动正常

#### 平板测试 (iPad, 1024x768)
- [ ] 自动切换到移动端标签页模式
- [ ] 标签页切换流畅
- [ ] 内容区域无溢出
- [ ] 触摸操作灵敏

#### 移动端测试 (iPhone/Android, 375x667+)
- [ ] 标签页固定在顶部
- [ ] 工作台内容纵向滚动流畅
- [ ] 交易视图表单可用
- [ ] 按钮点击区域足够大 (min 44x44px)
- [ ] 字体大小可读 (min 14px)

#### 兼容性浏览器矩阵
| 浏览器 | 版本 | 优先级 | 测试重点 |
|--------|------|--------|----------|
| Chrome | 最新2个主要版本 | P0 | 完整功能 |
| Firefox | 最新2个主要版本 | P0 | 完整功能 |
| Safari | macOS/iOS最新版 | P0 | 完整功能 |
| Edge | Chromium最新版 | P1 | 基础功能 |

#### 移动操作系统矩阵
| OS | 版本 | 优先级 | 测试设备 |
|----|------|--------|----------|
| iOS | 12+ | P0 | iPhone SE, 11, 12, 13 |
| Android | 8.0+ | P0 | Pixel, Samsung S系列 |

### 9.3 性能测试

- [ ] 首次渲染时间 < 2秒 (3G网络)
- [ ] 面板展开/收起动画帧率 ≥ 60fps
- [ ] 拖拽调整宽度时CPU使用率 < 30%
- [ ] 内存泄漏检测（切换标签10次后内存稳定）
- [ ] 大数据量测试（候选池100+条目）

### 9.4 无障碍性测试 (a11y)

- [ ] 键盘可完成所有操作
- [ ] Screen reader (NVDA/VoiceOver) 正确朗读内容
- [ ] Color contrast ratio ≥ 4.5:1 (AA标准)
- [ ] Focus indicator清晰可见
- [ ] ARIA labels准确描述元素用途

---

## 10. 实施计划

### Phase 1: 核心功能开发 (Day 1-2)

#### 任务清单
1. **创建 InvestmentCenter 组件骨架**
   - 文件: `src/components/InvestmentCenter.tsx`
   - 实现: 基础容器、状态管理、响应式判断
   - 验证: 组件可挂载，控制台无报错

2. **集成 PortfolioWorkbench 到主视图**
   - 修改: `InvestmentCenter.tsx`
   - 实现: 导入并渲染 PortfolioWorkbench
   - 验证: 工作台数据显示正常

3. **改造 Trading 为 TradingPanel**
   - 修改: `src/components/Trading.tsx`
   - 实现: 添加 isPanelMode prop，条件渲染
   - 验证: 面板模式下布局正确

4. **实现面板展开/收起逻辑**
   - 修改: `InvestmentCenter.tsx`
   - 实现: 状态切换、动画class、浮动按钮
   - 验证: 点击按钮面板正确显示/隐藏

5. **更新 App.tsx 导航配置**
   - 修改: `src/App.tsx`
   - 实现: 替换导航项、移除二级导航、集成InvestmentCenter
   - 验证: 导航切换正常，无报错

### Phase 2: 样式完善 (Day 3)

6. **编写面板CSS样式**
   - 文件: `src/styles/workbench.css` (追加)
   - 实现: 面板布局、头部、拖拽手柄、动画
   - 验证: 视觉效果符合设计稿

7. **实现响应式布局**
   - 修改: CSS media queries
   - 实现: 移动端标签页、断点适配
   - 验证: 各断点下布局正确

8. **添加拖拽调整宽度功能**
   - 修改: `InvestmentCenter.tsx`
   - 实现: mouse/touch事件监听、宽度限制
   - 验证: 拖拽流畅，数值精确

9. **国际化文案更新**
   - 修改: `src/i18n/index.ts`
   - 实现: 添加新翻译key
   - 验证: 中英文切换正常

### Phase 3: 测试与优化 (Day 4)

10. **功能测试**
    - 执行第9章测试用例
    - 修复发现的bug
    - 边界情况处理

11. **性能优化**
    - React.memo应用
    - 动画性能调优
    - 内存泄漏排查

12. **兼容性测试**
    - 多浏览器测试
    - 多设备真机测试
    - 修复兼容性问题

13. **文档更新**
    - 代码注释完善
    - README更新（如需要）
    - 用户引导文案

### Phase 4: 部署与验收 (Day 5)

14. **Code Review**
    - 提交Pull Request
    - 同行评审
    - 根据反馈修改

15. **Staging环境验证**
    - 部署到预发布环境
    - 产品经理验收测试
    - 收集反馈

16. **生产环境发布**
    - 合并到主干
    - 编译打包
    - 部署上线
    - 监控错误日志

17. **发布后监控**
    - 用户反馈收集
    - 性能指标监控
    - 快速修复紧急问题

---

## 11. 风险评估与缓解

### 11.1 技术风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| CSS布局在不同浏览器表现不一致 | 中 | 高 | 使用CSS Grid/Flexbox，避免float；充分测试主流浏览器 |
| 面板拖拽性能问题（卡顿） | 低 | 中 | 使用transform代替width动画；requestAnimationFrame节流 |
| 移动端触摸事件冲突 | 中 | 中 | 区分scroll和drag；使用passive event listeners |
| React状态更新导致不必要的重渲染 | 中 | 低 | React.memo + useMemo/useCallback优化 |
| 与现有DetailModal/MetricModal冲突 | 低 | 高 | 确保z-index层级正确；modal覆盖面板 |

### 11.2 业务风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 用户不适应新布局，抱怨找不到功能 | 中 | 高 | 提供短暂的引导提示；保留浮动按钮常驻；支持快捷键 |
| 重要操作路径变长（如下单需先展开面板） | 低 | 中 | 默认展开面板；记住用户上次选择 |
| 移动端体验不如PC端流畅 | 中 | 中 | 移动端单独优化UI；必要时保留二级标签页 |

### 11.3 项目风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 开发时间超估 | 中 | 中 | Phase 1为核心MVP，Phase 2-4可迭代；预留buffer时间 |
| 测试覆盖率不足 | 中 | 高 | 关键路径必须手动测试；自动化测试覆盖核心流程 |
| 回滚困难（代码耦合） | 低 | 高 | 使用feature branch开发；保持commit原子性；准备hotfix方案 |

---

## 12. 验收标准 (Definition of Done)

### 12.1 功能完整性 ✅

- [ ] "投资中心"一级导航正常显示且可点击
- [ ] 工作台所有功能100%可用（候选池、评分、机会、指标）
- [ ] 交易面板所有功能100%可用（账户、下单、持仓、成交）
- [ ] 面板展开/收起/拖拽功能正常
- [ ] 数据实时同步，无延迟或丢失
- [ ] DetailModal和MetricModal正常弹出和关闭

### 12.2 UI/UX质量 ✅

- [ ] 视觉设计与现有界面风格统一（字体、颜色、间距、圆角）
- [ ] 面板样式精致，无明显瑕疵
- [ ] 交互动画流畅自然（60fps）
- [ ] 响应式布局在各断点下表现良好
- [ ] 无障碍访问支持基本完善（键盘、screen reader）

### 12.3 兼容性要求 ✅

- [ ] Chrome 90+, Firefox 88+, Safari 14+, Edge 90+ 正常运行
- [ ] iOS 12+ Safari 正常使用
- [ ] Android 8.0+ Chrome 正常使用
- [ ] 分辨率 1280x720 及以上完美显示
- [ ] 分分辨率 1920x1080 及以上完美显示

### 12.4 代码质量 ✅

- [ ] TypeScript类型检查无错误（`tsc --noEmit`通过）
- [ ] ESLint检查无warning或error
- [ ] 代码注释清晰，关键逻辑有说明
- [ ] Commit message规范（遵循Conventional Commits）
- [ ] 无console.log/debugger残留
- [ ] 无硬编码的魔法数字（使用常量或配置）

### 12.5 性能指标 ✅

- [ ] 首次内容绘制 (FCP) < 1.5s
- [ ] 最大内容绘制 (LCP) < 2.5s
- [ ] 首次输入延迟 (FID) < 100ms
- [ ] 累积布局偏移 (CLS) < 0.1
- [ ] 面板动画帧率稳定在60fps
- [ ] 内存使用增长合理（<50MB/小时）

### 12.6 测试覆盖 ✅

- [ ] 核心功能手工测试通过率 100%
- [ ] 关键用户路径测试通过（至少5个场景）
- [ ] 边界情况测试通过（空数据、网络异常、快速操作）
- [ ] 回归测试通过（确认未影响"机会挖掘"和"设置"模块）

---

## 13. 后续优化方向 (Phase 2+)

### 13.1 短期优化（发布后1-2周）

1. **用户反馈收集**
   - 添加反馈入口（如浮动按钮旁的小图标）
   - 分析用户操作热力图（如使用Hotjar）
   - A/B测试面板默认展开/收起状态

2. **细节打磨**
   - 面板拖拽时的半透明遮罩效果
   - 面板最小化到侧边栏图标（类似VS Code）
   - 键盘快捷键支持（Ctrl+B切换面板）
   - 面板位置记忆（左侧/右侧可选）

3. **性能监控**
   - 接入前端性能监控（如Sentry Performance）
   - Core Web Vitals持续跟踪
   - 错误日志自动上报

### 13.2 中期功能（发布后1-2月）

1. **多面板支持**
   - 允许用户同时打开多个面板（交易+分析+日志）
   - 面板拖拽排序
   - 面板状态持久化（localStorage）

2. **高级布局模板**
   - 预设布局：专注研究 / 专注交易 / 均衡模式
   - 自定义布局（拖拽调整各区域占比）
   - 布局分享/导入导出

3. **数据增强**
   - WebSocket实时价格推送
   - 持仓盈亏实时计算
   - 交易信号提醒（通知栏/声音）

### 13.3 长期愿景（季度规划）

1. **智能化**
   - AI辅助推荐面板布局（根据用户习惯）
   - 智能信息聚合（自动汇总重要信息）
   - 异常检测提醒（持仓风险预警）

2. **协作功能**
   - 多人同时查看同一组合（只读模式）
   - 评论和标注（在面板中批注）
   - 投资决策审批流程

3. **跨平台**
   - 桌面客户端（Electron/Tauri）
   - 移动原生App（React Native/Flutter）
   - 浏览器插件（快速查看持仓）

---

## 14. 附录

### 14.1 相关文件索引

| 文件路径 | 说明 | 改动类型 |
|----------|------|----------|
| `frontend/src/components/InvestmentCenter.tsx` | 新建容器组件 | **新增** |
| `frontend/src/components/TradingPanel.tsx` | 基于Trading改造的面板组件（可选，也可在InvestmentCenter内联处理） | **新增** (或从Trading复制后修改) |
| `frontend/src/App.tsx` | 主应用组件 - 仅新增导航项和渲染分支 | **小改** (~30行新增) |
| `frontend/src/i18n/index.ts` | 国际化配置 - 添加新翻译key | **微改** (~5行新增) |
| `frontend/src/styles/workbench.css` | 样式文件 - 添加面板样式 | **追加** (~80行新增) |
| `frontend/src/components/PortfolioWorkbench.tsx` | 工作台组件 | **❌ 不修改** |
| `frontend/src/components/Trading.tsx` | 交易组件 | **❌ 不修改** (或复制为新文件后修改) |
| `app/api/routes/dashboard.py` | 后端API | **❌ 不变** |
| `frontend/src/context/AppContext.tsx` | 状态管理 | **❌ 不变** |
| `frontend/src/types/index.ts` | 类型定义 | **❌ 不变** |
| `frontend/src/api/client.ts` | API客户端 | **❌ 不变** |

### 14.2 代码改动量估算

```
新增代码:  ~400-500 行
  - InvestmentCenter.tsx: ~150 行
  - TradingPanel.tsx: ~250 行 (如新建)
  - CSS样式: ~80 行

修改代码:  ~35 行
  - App.tsx: ~25 行 (新增导航按钮 + 渲染分支)
  - i18n/index.ts: ~10 行 (新增翻译键)

不修改文件:  8 个核心文件零改动
```

### 14.2 关键技术依赖

```json
{
  "dependencies": {
    "react": "^17.x || ^18.x",
    "antd": "^4.x || ^5.x"
  },
  "devDependencies": {
    "typescript": "^4.x || ^5.x",
    "@types/react": "^17.x || ^18.x"
  },
  "optionalDependencies": {
    "react-beautiful-dnd": "^13.x",  // Phase 2: 面板拖拽排序
    "@dnd-kit/core": "^6.x",         // 替代方案
    "zustand": "^4.x"                // Phase 2: 全局状态管理
  }
}
```

### 14.3 参考资料

- [React官方文档 - 响应式设计](https://react.dev/learn/thinking-in-react)
- [MDN - CSS Flexbox](https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_Flexible_Box_Layout)
- [WAI-ARIA Authoring Practices](https://www.w3.org/WAI/ARIA/apg/)
- [Ant Design 4.x Component API](https://ant.design/components/overview/)
- [Web Content Accessibility Guidelines (WCAG) 2.1](https://www.w3.org/WAI/WCAG21/quickref/)

### 14.4 术语表

| 术语 | 英文 | 定义 |
|------|------|------|
| 投资中心 | Investment Center | 整合后的新一级导航模块名称 |
| 工作台 | Workbench | 原PortfolioWorkbench，展示研究和分析功能 |
| 交易面板 | Trading Panel | 改造后的Trading组件，以侧边栏形式展现 |
| 面板 | Panel | 可收起/展开的侧边栏容器 |
| 渐进式重构 | Gradual Refactoring | 本项目采用的实施策略，逐步改进而非重写 |
| 响应式 | Responsive | 自动适配不同屏幕尺寸的设计方法 |
| 断点 | Breakpoint | 触发布局变化的屏幕宽度阈值 |
| BEM | Block Element Modifier | CSS命名方法论 |

---

## 15. 版本历史

| 版本 | 日期 | 作者 | 变更说明 |
|------|------|------|----------|
| 1.0 | 2026-06-21 | AI Assistant | 初始版本（错误：设计为替换模式） |
| 1.1 | 2026-06-21 | AI Assistant | **重大修订：改为新增独立页面，保留原有功能100%不变** |

---

**文档结束**

**下一步：用户确认修订后的设计方案 → 进入实施阶段（创建详细任务清单并开始编码）**

