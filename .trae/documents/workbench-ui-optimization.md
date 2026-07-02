# 工作台卡片与数据展示 UI/UX 优化实施计划

## 1. 摘要

本计划基于已部分完成的「工作台卡片与数据展示」交互优化，继续完成剩余工作：补齐缺失的国际化键值、替换剩余原生表格为 Ant Design Table、完善可访问性属性、修复硬编码英文文案、细化 CSS 微交互与响应式断点。所有修改只涉及前端展示层，不改动业务逻辑与后端接口。

## 2. 现状分析

### 2.1 已完成

- `PortfolioWorkbench.tsx`：顶部空状态、今日机会/观察/消息列表空状态已统一使用 `Empty`。
- `Discovery.tsx`：结果表格已替换为 `Table`，支持按最终机会分/消息分/股质/时点/优先分/建议仓位排序，分页 `pageSize: 20`。
- `workbench.css`：已引入 `--accent-soft-hover`、`--focus-ring` 等变量，`.today-item` 与 `.metric-card.metric-action` 具备 hover/focus/active 状态，`.today-grid` 列比例已调整为 `1.1fr 1fr 1.2fr`。
- 部分操作按钮已添加 `aria-label`（如 Discovery 行内加入观察池、冻结、更新按钮）。

### 2.2 待完成

1. **国际化键值缺失**：`Discovery.tsx:1031` 使用 `t("noDiscoveryResults")`，但 `frontend/src/i18n/index.ts` 未定义该键；`Discovery.tsx:702/733` 存在硬编码英文 `"Low"`、`"Matched X/Y"`。
2. **剩余原生表格未替换**：`PortfolioWorkbench.tsx:616` 持仓表、`PortfolioWorkbench.tsx:891` 候选池表仍使用原生 `<table>`，无排序、无统一行交互。
3. **可访问性不足**：`Pool Tab` 按钮无 `aria-pressed`；候选/持仓可点击行无 `role/tabIndex`；`metric-card.metric-action` 无 `aria-label`；今日机会按钮无明确标签。
4. **CSS 细节缺失**：`.discovery-table-row` 缺少 hover/focus 行高亮；`.metric-card.metric-action` 未显示计划中的箭头图标；720px 以下表格横向滚动未完全验证。
5. **加载状态不完整**：持仓列表、组合暴露、候选池在首次加载时无 `Skeleton`。

## 3. 具体实施步骤

### 步骤 1：补齐国际化键值（`frontend/src/i18n/index.ts`）

在 `zh-CN` 与 `en-US` 两段中分别新增/修正以下键：

```ts
// zh-CN
noDiscoveryResults: "暂无挖掘结果",
lowCredibility: "低可信度",
matchedIndicators: "命中 {matched}/{total}",
matchedIndicatorDetail: "条件：{detail}",
viewSymbolDetail: "查看 {symbol} 详情",
positionsTableEmpty: "暂无持仓",
candidatesTableEmpty: "暂无候选",
noLatestScores: "暂无评分",
metricActionView: "查看 {label} 详情",
// en-US
noDiscoveryResults: "No discovery results",
lowCredibility: "Low credibility",
matchedIndicators: "Matched {matched}/{total}",
matchedIndicatorDetail: "Conditions: {detail}",
viewSymbolDetail: "View {symbol} detail",
positionsTableEmpty: "No positions",
candidatesTableEmpty: "No candidates",
noLatestScores: "No scores yet",
metricActionView: "View {label} details",
```

**说明**：`noDiscoveryResults` 为当前运行时缺失键，必须优先补齐；其余键为本次替换表格与增强可访问性所需。

### 步骤 2：修复 `Discovery.tsx` 硬编码与可访问性

#### 2.1 替换硬编码文案

- `Discovery.tsx:702`：
  ```tsx
  // 前
  {isLowCredibility && <span className="credibility-badge credibility-low">Low</span>}
  // 后
  {isLowCredibility && <span className="credibility-badge credibility-low">{t("lowCredibility")}</span>}
  ```

- `Discovery.tsx:733-735`：
  ```tsx
  // 前
  {ctx.locale === "zh-CN"
    ? `Matched ${matchedIndicatorCount}/${indicatorEntries.length}`
    : `Matched ${matchedIndicatorCount}/${indicatorEntries.length}`}
  // 后
  {template("matchedIndicators", {
    matched: matchedIndicatorCount,
    total: indicatorEntries.length,
  })}
  ```

#### 2.2 增强可访问性

- `Pool Tab` 按钮（`Discovery.tsx:957`）增加 `aria-pressed` 与 `aria-label`：
  ```tsx
  <button
    key={key}
    className={`pool-tab ${poolTab === key ? "pool-tab--active" : ""}`}
    aria-pressed={poolTab === key}
    aria-label={template("poolTabLabel", { label: ctx.locale === "zh-CN" ? cfg.zh : cfg.en })}
    ...
  >
  ```

- 表格 `onRow`（`Discovery.tsx:1032`）增加键盘与屏幕阅读器支持：
  ```tsx
  onRow={(record) => ({
    onClick: () => handleRowClick(record.symbol_id),
    onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleRowClick(record.symbol_id); } },
    tabIndex: 0,
    role: "button",
    "aria-label": template("viewSymbolDetail", { symbol: record.symbol }),
    className: [...].filter(Boolean).join(" "),
  })}
  ```

### 步骤 3：替换 `PortfolioWorkbench.tsx` 原生表格为 Ant Design Table

#### 3.1 持仓表（`PortfolioWorkbench.tsx:614-668`）

引入 `Table`：

```tsx
import { Input, InputNumber, Button, Tag, Space, Modal, Dropdown, Empty, Skeleton, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
```

定义 `ColumnsType<Position>`：

```tsx
const positionColumns: ColumnsType<Position> = [
  {
    title: t("symbol"),
    key: "symbol",
    render: (_, item) => (
      <div className="symbol-title">
        <span className="symbol-code">{item.symbol}</span>
        <span className="symbol-name">{item.name}</span>
      </div>
    ),
  },
  { title: t("quantity"), dataIndex: "quantity", key: "quantity" },
  { title: t("avgCost"), dataIndex: "avg_cost", key: "avg_cost", render: (v) => score(v) },
  { title: t("currentPrice"), dataIndex: "latest_price", key: "latest_price", render: (v) => score(v) },
  { title: t("marketValue"), dataIndex: "market_value", key: "market_value", render: (v) => money(v) },
  { title: t("position"), dataIndex: "position_pct", key: "position_pct", render: (v) => percent(v) },
  {
    title: t("unrealizedPnl"),
    dataIndex: "unrealized_pnl",
    key: "unrealized_pnl",
    render: (v) => <span className={pnlClass(v)}>{money(v)}</span>,
  },
  {
    title: t("unrealizedPnlPct"),
    dataIndex: "unrealized_pnl_pct",
    key: "unrealized_pnl_pct",
    render: (v) => <span className={pnlClass(v)}>{percent(v)}</span>,
  },
  {
    title: t("operations"),
    key: "operations",
    render: (_, item) => (
      <Button
        className="delete-btn"
        size="small"
        danger
        loading={deletingPositionSymbolId === item.symbol_id}
        onClick={(e) => { e.stopPropagation(); handleDeletePosition(item.symbol_id); }}
        aria-label={template("deletePositionFor", { symbol: item.symbol })}
      >
        {t("deletePosition")}
      </Button>
    ),
  },
];
```

替换渲染：

```tsx
<Table<Position>
  columns={positionColumns}
  dataSource={positions}
  rowKey="symbol_id"
  pagination={false}
  locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("positionsTableEmpty")} /> }}
  onRow={(record) => ({
    onClick: () => handleSymbolClick(record.symbol_id),
    onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleSymbolClick(record.symbol_id); } },
    tabIndex: 0,
    role: "button",
    "aria-label": template("viewSymbolDetail", { symbol: record.symbol }),
  })}
/>
```

**新增 i18n 键**：`deletePositionFor: "删除 {symbol} 持仓" / "Delete {symbol} position"`。

#### 3.2 候选池表（`PortfolioWorkbench.tsx:890-920`）

定义 `ColumnsType<WorkbenchCandidate>`：

```tsx
const candidateColumns: ColumnsType<WorkbenchCandidate> = [
  {
    title: t("rank"),
    key: "rank",
    width: 70,
    render: (_, item, index) => item.rank_no ?? index + 1,
  },
  {
    title: t("symbol"),
    key: "symbol",
    render: (_, item) => (
      <div className="symbol-title">
        <span className="symbol-code">{item.symbol}</span>
        <span className="symbol-name">{item.name}</span>
      </div>
    ),
  },
  { title: t("quality"), dataIndex: "quality_score", key: "quality_score", sorter: (a, b) => a.quality_score - b.quality_score, render: (v) => score(v), width: 90 },
  { title: t("timing"), dataIndex: "timing_score", key: "timing_score", sorter: (a, b) => a.timing_score - b.timing_score, render: (v) => score(v), width: 90 },
  { title: t("stage"), dataIndex: "stage", key: "stage", render: (v) => <Tag className={badgeClass(v)}>{stageLabel(v)}</Tag>, width: 100 },
  { title: t("action"), dataIndex: "action", key: "action", render: (v) => <Tag className={badgeClass(v)}>{actionLabel(v)}</Tag>, width: 100 },
  { title: t("position"), dataIndex: "recommended_position_pct", key: "recommended_position_pct", sorter: (a, b) => (a.recommended_position_pct ?? 0) - (b.recommended_position_pct ?? 0), render: (v) => percent(v), width: 100 },
];
```

替换渲染：

```tsx
<Table<WorkbenchCandidate>
  columns={candidateColumns}
  dataSource={filteredCandidates}
  rowKey="symbol_id"
  pagination={{ pageSize: 20, hideOnSinglePage: true }}
  locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("candidatesTableEmpty")} /> }}
  onRow={(record) => ({
    onClick: () => handleSymbolClick(record.symbol_id),
    onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleSymbolClick(record.symbol_id); } },
    tabIndex: 0,
    role: "button",
    "aria-label": template("viewSymbolDetail", { symbol: record.symbol }),
    className: `clickable ${ctx.activeSymbolId === record.symbol_id ? "active" : ""}`,
  })}
/>
```

### 步骤 4：增强 `PortfolioWorkbench.tsx` 可访问性与加载态

#### 4.1 今日机会按钮

为 `.today-item` 增加 `aria-label`：

```tsx
<Button
  ...
  aria-label={template("viewSymbolDetail", { symbol: item.symbol })}
>
```

#### 4.2 指标卡片

为 `.metric-card.metric-action` 增加 `aria-label`：

```tsx
<Button
  type="text"
  className="metric-card metric-action"
  onClick={() => openMetricModal("symbols")}
  aria-label={template("metricActionView", { label: t("trackedUniverse") })}
>
```

其余两张卡片同理。

#### 4.3 加载骨架

- 持仓区域：新增 `positionsLoading` 状态，在 `loadPositions` 期间展示 `<Skeleton active paragraph={{ rows: 4 }} />`。
- 组合暴露：在 `loadAllocation` 期间展示 `<Skeleton active paragraph={{ rows: 3 }} />`。
- 候选池：在 `ctx.loadWorkbench()` 全局加载期间，使用 `ctx.globalLoading` 控制 `Table` 的 `loading` 属性。

### 步骤 5：CSS 微交互与响应式收尾（`frontend/src/styles/workbench.css`）

#### 5.1 Discovery 表格行

在 `workbench.css` 中追加：

```css
/* Discovery Table row interaction */
.discovery-table-row {
  cursor: pointer;
  transition: background-color 0.16s ease, box-shadow 0.16s ease;
}

.discovery-table-row:hover td {
  background: var(--accent-soft-hover) !important;
}

.discovery-table-row:focus-visible {
  outline: none;
}

.discovery-table-row:focus-visible td {
  box-shadow: inset 0 0 0 2px rgba(15, 118, 110, 0.35);
}

.discovery-table-row:active td {
  background: rgba(15, 118, 110, 0.08) !important;
}

/* Ensure operation buttons inside row do not trigger row click */
.discovery-table-row .ant-space-item button {
  position: relative;
  z-index: 1;
}
```

#### 5.2 Metric action arrow

在 `.metric-card.metric-action` 样式块中追加：

```css
.metric-card.metric-action::after {
  content: "→";
  position: absolute;
  right: 18px;
  bottom: 16px;
  font-size: 16px;
  color: var(--accent);
  opacity: 0;
  transform: translateX(-4px);
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.metric-card.metric-action:hover::after,
.metric-card.metric-action:focus-visible::after {
  opacity: 1;
  transform: translateX(0);
}

.metric-card.metric-action {
  position: relative;
}
```

#### 5.3 统一焦点环

确保所有可交互元素使用 `--focus-ring`：

```css
button:focus-visible,
a:focus-visible,
[tabindex]:not([tabindex="-1"]):focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
```

#### 5.4 移动端表格

在 `@media (max-width: 720px)` 中追加：

```css
@media (max-width: 720px) {
  .table-wrap {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  .table-wrap .ant-table {
    min-width: 640px;
  }

  .today-grid {
    gap: 10px;
  }

  .today-column {
    padding: 10px;
  }
}
```

### 步骤 6：空状态扫尾

- `TodayDecision.tsx`：已使用 `Empty`，但其内部 `LABELS` 对象与全局 `t()` 并存。本次 **不迁移** `LABELS`（改动面大、非必要），仅确认无新增硬编码中文/英文。
- `InvestmentCenter.tsx`：搜索无结果等场景已使用 `t("noSearchResult")`，无需额外修改。
- 确认 `PortfolioWorkbench.tsx` 所有 `Empty` 描述均使用 `t()` 函数，无遗漏。

## 4. 假设与决策

1. **不迁移 `TodayDecision.tsx` 的本地 `LABELS`**：该组件文案完整且已具备中英文，迁移至全局 i18n 属于额外工作，本次保持现状以控制范围。
2. **候选池表格排序**：仅开放股质、时点、建议仓位排序；阶段/动作为离散值，不排序；排名列为展示序号，不排序。
3. **持仓表格不分页**：持仓数量通常有限，使用 `pagination={false}`，避免多余 UI。
4. **Ant Design Table 样式覆盖**：通过 `.ant-table` 相关类名覆盖，保持与现有设计一致。
5. **不修改业务逻辑**：所有 `onClick`、`onKeyDown` 仅调用已有 `handleSymbolClick` / `handleDeletePosition`，不新增数据流。

## 5. 验证方式

1. **TypeScript 编译**：
   ```bash
   cd d:\ai_project\dataAanlystNew\frontend && npm run typecheck
   ```
   或 `tsc --noEmit`，确保 `Table` 泛型与 `ColumnsType` 类型正确。

2. **启动前端 dev server**：
   ```bash
   npm run dev
   ```

3. **空状态检查**：
   - 工作台无数据时显示 `Empty` + `noScanYet`。
   - Discovery 无结果时显示 `Empty` + `noDiscoveryResults`（不再显示键名）。
   - 持仓为空、候选为空、备份为空时均显示对应文案。

4. **表格检查**：
   - Discovery 表格支持点击表头排序。
   - PortfolioWorkbench 持仓表、候选池表已替换为 Ant Design Table。
   - 点击行可打开标的详情，操作按钮不影响行点击。

5. **可访问性检查**（浏览器 DevTools → Accessibility）：
   - Pool Tab 按钮具备 `aria-pressed`。
   - 可点击表格行具备 `role="button"`、`tabIndex="0"`、`aria-label`。
   - 指标卡片具备 `aria-label`。

6. **交互视觉检查**：
   - `.today-item`、`.metric-card.metric-action`、表格行 hover/focus/active 状态正常。
   - 指标卡片 hover 时右下角出现箭头。
   - 720px 以下表格可横向滚动，布局不溢出。

7. **运行时文案检查**：
   - Discovery 低可信度标签显示中文「低可信度」/ 英文 "Low credibility"。
   - 指标命中文案显示为「命中 X/Y」/ "Matched X/Y"。

8. **功能回归**：
   - 持仓录入、删除、规则保存、备份恢复等现有功能正常。
   - 候选池搜索、排序、点击打开详情正常。
