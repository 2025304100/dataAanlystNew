# 施工规范（内部文档 · 5 位并行构建师共用）

> 本文件是原型构建的施工合同。所有页面必须照此执行，质量审查会机械校验。
> 交付物目录：`D:/ai_project/dataAanlystNew/ui-redesign-2026-10-27/`
> **严禁修改 `frontend/` 或项目任何既有文件。**

---

## 一、基础层（已就绪，只读，不要改）

| 文件 | 作用 |
|---|---|
| `assets/tokens.css` | 76 个设计令牌（色彩/排版/间距/圆角/阴影/字体） |
| `assets/proto.css` | 全部组件与骨架样式，1162 行 |
| `assets/proto.js` | 仅 Tab / 分段控件切换，零依赖 |
| `_template.html` | **页面骨架，每个页面都必须从它复制** |
| `00-design-system.html` | 设计系统说明书，底部有 5 条硬规则原文 |

动手前必读：`_template.html` 全文、`assets/tokens.css`、`00-design-system.html` 的组件区。

---

## 二、页面骨架：复制 `_template.html`，只改 4 处

1. `<title>` → `页面名 · 量化工作台`
2. 顶栏 `.crumb-current` → 当前页面名（上级面包屑按页面归属写）
3. 侧栏 6 个 `.side-item` 中，给当前一级入口加 `is-active`（其余去掉）
4. 替换 `.content` 内部

侧栏 6 项与固定顺序：今日决策 `01-decision.html` / 组合交易 `02-portfolio-overview.html` / 机会中心 `07-opportunity.html` / 宏观数据 `09-macro.html` / 行情消息 `10-news.html` / 设置 `11-settings.html`

---

## 三、类名契约（`proto.css` 已定义，直接用）

**禁止新增类名、禁止内联 `style="`、禁止 `<style>` 块。**

- **骨架** `.app` `.sidebar` `.main` `.topbar` `.content`
- **侧栏** `.side-brand` `.side-brand-mark` `.side-brand-text` `.side-brand-title` `.side-brand-sub` `.side-nav` `.side-item`(+`.is-active`) `.side-label` `.side-foot` `.side-foot-dot` `.side-foot-text`
- **顶栏** `.crumb` `.crumb-sep` `.crumb-current` `.topbar-right` `.icon-btn`(+`.icon-btn--ai`) `.ai-badge` `.icon-dot` `.divider-v`
- **页头** `.page-head` `.page-kicker` `.page-title` `.page-sub` `.page-actions`
- **导航** `.tabs2` `.tab2`(+`.is-active`) `.seg` `.seg-item`(+`.is-active`)
- **卡片/栅格** `.card` `.card--flush` `.card-head` `.card-title` `.card-sub` `.card-body` `.card-body--flush` `.grid` `.col-3` `.col-4` `.col-6` `.col-8` `.col-9` `.col-12`
- **表格** `.table-wrap` `.table` `.num` `.col-sticky` `.cell-strong` `.val-up` `.val-down` `.val-flat` `tr.is-selected`
- **KPI** `.kpi-row` `.kpi` `.kpi-label` `.kpi-value` `.kpi-unit` `.kpi-delta`(+`.is-up`/`.is-down`/`.is-flat`)
- **状态** `.pill` `.pill--pass` `.pill--warn` `.pill--fail` `.pill--pending` `.pill--blocked`
- **按钮** `.btn` `.btn--primary` `.btn--secondary` `.btn--ghost` `.btn--danger` `.btn--sm` `.btn--lg`
- **表单** `.field` `.input` `.select-wrap` `.select` `.select-chevron` `.filter-bar` `.filter-grow` `.filter-end`
- **图表** `.chart` `.chart--240` `.chart--320` `.chart--420` `.chart-head` `.chart-title` `.chart-sub` `.chart-legend` `.legend-dot`(+`--1`~`--5`/`--up`/`--down`) `.chart-body` `.chart-empty`
  - SVG 内只能用：`.c-grid` `.c-axis` `.c-line-1` `.c-line-2` `.c-line-3` `.c-area-1` `.c-area-up` `.c-bar-up` `.c-bar-down` `.c-dot` `.c-dot-strong` `.c-label` `.c-label-strong`
- **其它** `.empty` `.empty-title` `.empty-desc` `.danger-zone` `.danger-zone-title` `.danger-zone-desc` `.alert` `.alert--info` `.alert--warn` `.alert--danger` `.alert-title` `.alert-body` `.tag` `.mono` `.muted` `.text-1` `.text-2` `.hr` `.sr-only`
- **工具** `.row` `.between` `.wrap` `.flex-1` `.items-start` `.gap-4` `.gap-8` `.gap-12` `.gap-16` `.mt-4` `.mt-8` `.mt-12` `.mt-16` `.mt-24` `.mb-4` `.mb-8` `.mb-12` `.mb-16` `.mb-24`

---

## 四、多子页必须可点（Tab 切换）

`<head>` 中在 `proto.css` 之后加：`<script src="assets/proto.js" defer></script>`

```html
<nav class="tabs2" data-tabs>
  <button class="tab2 is-active" data-tab="a" aria-selected="true" type="button">候选池</button>
  <button class="tab2" data-tab="b" aria-selected="false" type="button">观察池</button>
</nav>
<div data-tab-scope>
  <section data-panel="a">…</section>
  <section data-panel="b" hidden>…</section>
</div>
```

分段控件同理：`.seg` 容器加 `data-tabs`，`.seg-item` 加 `data-tab`。

---

## 五、5 条硬规则（违反即不合格）

1. 除 `tokens.css` 外，任何文件 `#` 开头的硬编码色值 **0 次**；`!important` **0 次**；行内 `style="` **0 次**；`<style>` 块 **0 次**。
2. **涨 = `.val-up`（#D92D20）、跌 = `.val-down`（#087F45）**，只许出现在右对齐数值单元格（`.num`）；任何 `.pill` / `.tag` 内不得出现涨跌色。
3. 所有数值列必须 `class="num"`；字号只用 11/12/13/15/20/28/36（用现成类，不要写 px）。
4. 圆角只 4/6/10/999；同一元素不同时用 border 与 shadow；状态一律 `.pill`，**emoji 0 个**。
5. 主色只用 `--brand`；单页深色（`.chart`）面积 ≤ 35%，且不得与侧栏/顶栏相接。

---

## 六、内容质量：这是「原型图」不是「线框图」

- **真实感**：用真实 A 股代码与名称 —— 600519 贵州茅台、000858 五粮液、300750 宁德时代、601318 中国平安、002594 比亚迪、600036 招商银行、601899 紫金矿业、002415 海康威视、600276 恒瑞医药、688981 中芯国际、000651 格力电器、000001 平安银行。
- **自洽**：价格、市值、占比要量级合理且互相对得上（如持仓市值合计 ≈ 总资产 × 仓位；占比合计 ≈ 100%）。
- **涨红跌绿**：涨用 `.val-up` 带 `+`，跌用 `.val-down` 带 `-`，平盘用 `.val-flat`。
- **密度**：表格至少 8–12 行；KPI 至少 4 项；不留大片空白。
- **图表全部手绘内联 SVG**：折线 `<polyline class="c-line-1">`、面积 `<path class="c-area-1">`、网格 `<line class="c-grid">`、轴标签 `<text class="c-label">`。**不引 ECharts 或任何 JS 库。**
- **状态完整性**：每页至少展示一处 `.empty` 或 `.alert` 或 `.danger-zone`。
- 中文界面；数字用 `.mono` 或 `.num`。

---

## 七、自检（写完必须逐条跑）

```bash
f=你的文件.html
grep -c 'style="' "$f"                                  # 必须 0
grep -o '#[0-9a-fA-F]\{3,6\}' "$f" | wc -l             # 必须 0
grep -c '!important' "$f"                               # 必须 0
grep -c '<style' "$f"                                   # 必须 0
grep -c 'http' "$f"                                     # 必须 0
grep -cE '✅|❌|🔄|🚫|📈|📉|⭐|⚠' "$f"                  # 必须 0
```

回报时必须附上这 6 条命令的实际输出数字。
