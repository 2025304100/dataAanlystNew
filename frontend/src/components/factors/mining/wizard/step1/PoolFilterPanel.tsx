import { Fragment, useEffect, useMemo, useState } from "react";
import { t } from "../../../../../i18n";
import { Badge, Button, Checkbox, Collapse, InputNumber, Select, Tabs, Tag, Tooltip } from "antd";
import { fetchFilterFields, fetchFilterPresets } from "./poolApi";
import type { FilterField, FilterPreset } from "./poolApi";

/**
 * 条件筛选面板（向导 §3.2 完整分层布局，antd 控件重制版）。
 *
 * 布局：左「常用区间预设（Tabs）」+ 右「分类条件（Collapse）」双栏。
 * - 左栏：4 组预设 Tabs（市值/估值/流动性/上市时间），服务端分位边界一键应用；
 *   blocked 预设禁点 + Tooltip 原因（§3.2：阈值由服务端算，前端不得自算）。
 * - 右栏：7 分类 Collapse；**已有条件的分类自动展开**（B4）；分类标题行
 *   Tag 显示「已选 N 项」+ 清除本分类按钮（B3）；数值字段 InputNumber min/max
 *   + Select 缺失处理；市场/板块 Checkbox.Group；ST/退市 Checkbox。
 * - 预设应用（B1）：写入 filter_config 后右栏对应分类自动展开、输入框由
 *   value 驱动天然回填边界值，可继续细调。
 *
 * `filter_config` 契约：`{markets, boards, exclude_st, exclude_delisting,
 *   valuation: {field: {min,max,missing}}, liquidity: {window_days,
 *   field:{min,max}}, profitability: {roe_ttm, loss}}`。
 * 锁定态全部控件禁用（§3.7.3）。
 */

export interface PoolFilterPanelProps {
  locked: boolean;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}

/** 数值范围字段 → filter_config 槽位 */
const RANGE_SLOT: Record<string, string> = {
  total_market_cap: "valuation",
  circulating_market_cap: "valuation",
  pe_ttm: "valuation",
  pb: "valuation",
  dividend_yield: "valuation",
  avg_amount: "liquidity",
  avg_volume: "liquidity",
  avg_turnover_rate: "liquidity",
  roe_ttm: "profitability",
};

/** 布尔开关字段 */
const BOOLEAN_FIELDS = new Set(["exclude_st", "exclude_delisting"]);

/** 已由 markets/boards 多选控件承载的字段（避免在分类内重复渲染为灰行） */
const MULTI_HOSTED_FIELDS = new Set(["board", "market"]);

const MARKET_OPTIONS = [
  { code: "sh", labelKey: "miningPoolMarketSh" },
  { code: "sz", labelKey: "miningPoolMarketSz" },
  { code: "bj", labelKey: "miningPoolMarketBj" },
];

const BOARD_OPTIONS = [
  { code: "main_sh", labelKey: "miningPoolBoardMainSh" },
  { code: "main_sz", labelKey: "miningPoolBoardMainSz" },
  { code: "chinext", labelKey: "miningPoolBoardChinext" },
  { code: "star", labelKey: "miningPoolBoardStar" },
  { code: "bj", labelKey: "miningPoolBoardBj" },
];

function asObj(v: unknown): Record<string, unknown> {
  return v != null && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

export default function PoolFilterPanel({ locked, value, onChange }: PoolFilterPanelProps) {
  const [presets, setPresets] = useState<FilterPreset[]>([]);
  const [groups, setGroups] = useState<Array<{ group: string; label_zh: string }>>([]);
  const [fields, setFields] = useState<FilterField[]>([]);
  const [categories, setCategories] = useState<Array<{ category: string; label_zh: string }>>([]);
  const [activeGroup, setActiveGroup] = useState("market_cap");
  // B4：已有条件的分类自动展开（受控 Collapse；新分类/条件变化时并入）
  const [openKeys, setOpenKeys] = useState<string[]>([DEFAULT_OPEN_CATEGORY]);
  // B2：预设「自定义」标记 —— 应用后记录 preset_code；用户手工修改对应字段后
  // 标记为自定义态，不得再误显示为「已应用」（设计 §3.2：手工修改后显示"自定义"）。
  const [appliedPreset, setAppliedPreset] = useState<Record<string, string>>({});
  const [customizedPreset, setCustomizedPreset] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let alive = true;
    void Promise.allSettled([fetchFilterPresets(), fetchFilterFields()]).then(([p, f]) => {
      if (!alive) return;
      if (p.status === "fulfilled") {
        setPresets(Array.isArray(p.value.presets) ? p.value.presets : []);
        setGroups(Array.isArray(p.value.groups) && p.value.groups.length > 0 ? p.value.groups : []);
        if (p.value.groups?.[0]?.group) setActiveGroup(p.value.groups[0].group);
      }
      if (f.status === "fulfilled") {
        setFields(Array.isArray(f.value.fields) ? f.value.fields : []);
        setCategories(Array.isArray(f.value.categories) ? f.value.categories : []);
      }
    });
    return () => {
      alive = false;
    };
  }, []);

  // ── 写入 filter_config ──────────────────────────────────────────
  const patch = (next: Record<string, unknown>) => onChange({ ...value, ...next });

  const patchRange = (slot: string, fieldName: string, cond: Record<string, unknown>) => {
    // B2：预设有应用该字段 → 任何手工修改（min/max/缺失处理）都标记为自定义态
    // （预设只走 patch，不走 patchRange，因此这里必是用户编辑）
    if (appliedPreset[fieldName]) {
      setCustomizedPreset((prev) => ({ ...prev, [fieldName]: true }));
    }
    const slotObj = asObj(value[slot]);
    patch({ [slot]: { ...slotObj, [fieldName]: { ...asObj(slotObj[fieldName]), ...cond } } });
  };

  const clearSlot = (slot: string) => {
    const next = { ...value };
    delete next[slot];
    patch(next);
  };

  const applyPreset = (p: FilterPreset) => {
    if (locked || p.applyable === false || !p.field) return;
    const fieldName: string = p.field;
    const slot = RANGE_SLOT[fieldName];
    if (!slot) return;
    const slotObj = asObj(value[slot]);
    const inner = { ...asObj(slotObj[fieldName]) };
    // 阈值来自服务端分位数计算，会带浮点尾数（如 2219266000.0000005），
    // 回填前按量级收敛，避免把浮点噪声直接摆到用户面前。
    const tidyNumber = (v: number): number => {
      const abs = Math.abs(v);
      if (abs >= 1000) return Math.round(v);
      if (abs >= 1) return Number(v.toFixed(4));
      return Number(v.toFixed(6));
    };
    if (p.operator === "all") {
      delete inner.min;
      delete inner.max;
    } else {
      if (p.min_value != null) inner.min = tidyNumber(p.min_value);
      if (p.max_value != null) inner.max = tidyNumber(p.max_value);
      if (p.operator === "ge") { delete inner.max; }
      if (p.operator === "le") { delete inner.min; }
    }
    patch({ [slot]: { ...slotObj, [fieldName]: inner } });
    // B2：记录已应用预设（预设只写值，不置自定义态）
    setAppliedPreset((prev) => ({ ...prev, [fieldName]: p.preset_code }));
    setCustomizedPreset((prev) => ({ ...prev, [fieldName]: false }));
  };

  // ── B4：已有条件的分类自动展开 ─────────────────────────────────
  const categoryCount = useMemo(() => {
    const map: Record<string, number> = {};
    for (const f of fields) {
      if (extendsCondition(f)) map[f.category] = (map[f.category] ?? 0) + 1;
    }
    // 市场与交易状态：markets/boards 多选与 exclude_delisting 开关都归属该分类
    // （分类 key 与后端 filter-fields 对齐：market_status，而非 universe）
    const mk = value.markets as unknown[] | undefined;
    if (mk?.length) map.market_status = (map.market_status ?? 0) + 1;
    const bd = value.boards as unknown[] | undefined;
    if (bd?.length) map.market_status = (map.market_status ?? 0) + 1;
    if (value.exclude_st) map.risk = (map.risk ?? 0) + 1;
    if (value.exclude_delisting) map.market_status = (map.market_status ?? 0) + 1;
    return map;
  }, [fields, value]);

  function extendsCondition(f: FilterField): boolean {
    const slot = RANGE_SLOT[f.field];
    if (!slot) return false;
    const cond = asObj(asObj(value[slot])[f.field]);
    return cond.min != null || cond.max != null;
  }

  const activeKeys = useMemo(() => {
    const open = new Set(openKeys);
    for (const cat of categories) if (categoryCount[cat.category] > 0) open.add(cat.category);
    if (!open.has(DEFAULT_OPEN_CATEGORY)) open.add(DEFAULT_OPEN_CATEGORY);
    return Array.from(open);
  }, [categories, categoryCount, openKeys]);

  // ── 左栏 ────────────────────────────────────────────────────────
  const tabs = groups.length > 0 ? groups : [{ group: "market_cap", label_zh: "市值" }];
  const activePresets = presets.filter((p) => p.group === activeGroup);

  // ── 右栏控件渲染 ────────────────────────────────────────────────
  const rangeControl = (f: FilterField) => {
    const slot = RANGE_SLOT[f.field];
    const blocked = f.availability === "blocked" || f.data_mode === "blocked";
    const cond = asObj(asObj(value[slot])[f.field]);
    const min =
      typeof cond.min === "number" ? (cond.min as number) : undefined;
    const max =
      typeof cond.max === "number" ? (cond.max as number) : undefined;
    return (
      <div className={`mining-filter-row ${blocked ? "is-disabled" : ""}`} data-filter-range={f.field}>
        <span className="mining-filter-row-label">{f.label_zh}</span>
        <div className="mining-filter-range-inputs">
          <div data-pool-filter-input={f.field === "total_market_cap" ? "" : undefined}>
            <InputNumber
              value={min}
              min={0}
              placeholder={t("miningPoolRangeMin")}
              disabled={locked || blocked}
              onChange={(v) => patchRange(slot, f.field, { min: v ?? null })}
            />
          </div>
          <span className="mining-filter-sep">~</span>
          <InputNumber
            value={max}
            min={0}
            placeholder={t("miningPoolRangeMax")}
            disabled={locked || blocked}
            onChange={(v) => patchRange(slot, f.field, { max: v ?? null })}
          />
          <Select
            value={String(cond.missing ?? "exclude")}
            disabled={locked || blocked}
            style={{ width: 110 }}
            options={[
              { value: "exclude", label: t("miningPoolMissingExclude") },
              { value: "keep", label: t("miningPoolMissingKeep") },
            ]}
            onChange={(v) => patchRange(slot, f.field, { missing: v })}
          />
        </div>
        {blocked && f.blocked_reason_zh ? (
          // 正文只给用户句；开发口径（表名/实测行数）放悬浮，避免界面泄漏内部标识
          <span
            className="mining-filter-blocked-hint"
            title={f.blocked_detail_zh ?? undefined}
          >
            {f.blocked_reason_zh}
          </span>
        ) : null}
      </div>
    );
  };

  const booleanControl = (fieldName: string, label: string) => {
    const on = Boolean(value[fieldName]);
    return (
      <div className="mining-filter-row" data-filter-bool={fieldName}>
        <Checkbox
          checked={on}
          disabled={locked}
          onChange={(e) => patch({ [fieldName]: e.target.checked })}
        >
          {label}
        </Checkbox>
      </div>
    );
  };

  const multiControl = (key: string, label: string, options: Array<{ code: string; labelKey: string }>) => {
    const current = Array.isArray(value[key]) ? (value[key] as string[]) : [];
    return (
      <div className="mining-filter-row" data-filter-multi={key}>
        <span className="mining-filter-row-label">{label}</span>
        <Checkbox.Group
          value={current}
          disabled={locked}
          options={options.map((o) => ({ value: o.code, label: t(o.labelKey) }))}
          onChange={(vals) => patch({ [key]: vals })}
        />
      </div>
    );
  };

  // 已选条件数（B3：分类标题行 Tag）
  const countFor = (cat: string) => categoryCount[cat] ?? 0;

  const collapseItems = categories.map((cat) => {
    const catFields = fields.filter((f) => f.category === cat.category);
    // 分类 key 与后端 filter-fields.categories 对齐：市场与交易状态 = market_status
    // （此前误写为 universe，导致 markets/boards 多选永不渲染）
    const isMarketStatus = cat.category === "market_status";
    const count = countFor(cat.category);
    return {
      key: cat.category,
      label: (
        <span className="mining-filter-cat-head">
          <span>{cat.label_zh}</span>
          {count > 0 ? (
            <Badge count={count} size="small" style={{ backgroundColor: "#1677ff" }} />
          ) : null}
          <Button
            type="link"
            size="small"
            data-filter-clear={cat.category}
            disabled={locked || count === 0}
            onClick={(e) => {
              e.stopPropagation();
              clearCategory(cat.category);
            }}
          >
            {t("miningPoolClearCategory")}
          </Button>
        </span>
      ),
      children: (
        <div data-filter-category={cat.category}>
          {isMarketStatus && multiControl("markets", t("miningPoolMarkets"), MARKET_OPTIONS)}
          {isMarketStatus && multiControl("boards", t("miningPoolBoards"), BOARD_OPTIONS)}
          {catFields.map((f) => {
            // board/market 已由上方 markets/boards 多选控件承载，不再重复渲染
            if (MULTI_HOSTED_FIELDS.has(f.field)) return null;
            // 布尔开关按后端分类归属渲染（exclude_st→风险标记、exclude_delisting→市场与交易状态）
            if (BOOLEAN_FIELDS.has(f.field)) return booleanControl(f.field, f.label_zh);
            if (RANGE_SLOT[f.field]) {
              return (
                <Fragment key={f.field}>{rangeControl(f)}</Fragment>
              );
            }
            return (
              <div className="mining-filter-row is-disabled" data-filter-blocked={f.field} key={f.field}>
                <span className="mining-filter-row-label">{f.label_zh}</span>
                <span
                  className="mining-filter-blocked-hint"
                  title={f.blocked_detail_zh ?? undefined}
                >
                  {f.blocked_reason_zh ?? t("miningPoolFieldUnavailable")}
                </span>
              </div>
            );
          })}
        </div>
      ),
    };
  });

  function clearCategory(cat: string) {
    const next = { ...value };
    if (cat === "market_status") {
      delete next.markets;
      delete next.boards;
      delete next.exclude_delisting;
    } else if (cat === "risk") {
      delete next.exclude_st;
    } else {
      delete next[cat];
    }
    // B2：清除该分类下字段的预设标记（已应用/自定义一并复位）
    setAppliedPreset((prev) => {
      const rest = { ...prev };
      for (const f of fields) if (RANGE_SLOT[f.field] === cat) delete rest[f.field];
      return rest;
    });
    setCustomizedPreset((prev) => {
      const rest = { ...prev };
      for (const f of fields) if (RANGE_SLOT[f.field] === cat) delete rest[f.field];
      return rest;
    });
    patch(next);
  }

  return (
    <div className="mining-pool-filter" data-pool-filter-panel>
      <div className="mining-pool-filter-layout">
        {/* 左栏：常用区间预设 */}
        <aside className="mining-pool-filter-left" data-filter-left>
          <div className="mining-filter-left-title">{t("miningPoolFilterTitle")}</div>
          <Tabs
            tabPosition="left"
            size="small"
            activeKey={activeGroup}
            onChange={setActiveGroup}
            items={tabs.map((g) => ({
              key: g.group,
              label: g.label_zh,
              children: (
                <div className="mining-filter-preset-list">
                  {activePresets.map((p) => {
                    const blocked = p.applyable === false || p.availability === "blocked";
                    // B2：预设态 —— applied=已应用（primary 高亮 + 已应用 Tag），
                    // customized=自定义（默认按钮 + 自定义 Tag，不得再显示为已应用）
                    const fieldName: string | null = p.field;
                    const applied =
                      fieldName != null && appliedPreset[fieldName] === p.preset_code;
                    const customized = Boolean(
                      fieldName != null &&
                        appliedPreset[fieldName] &&
                        customizedPreset[fieldName],
                    );
                    const presetBtn = (
                      <Button
                        size="small"
                        type={applied && !customized ? "primary" : "default"}
                        data-filter-preset={p.preset_code}
                        data-filter-preset-state={customized ? "custom" : applied && !customized ? "applied" : undefined}
                        disabled={locked || blocked}
                        onClick={() => applyPreset(p)}
                      >
                        {p.label_zh}
                        {applied && (
                          <Tag
                            color={customized ? "orange" : "green"}
                            style={{ marginInlineStart: 6 }}
                            data-filter-preset-tag={customized ? "custom" : "applied"}
                          >
                            {customized ? t("miningPoolPresetCustom") : t("miningPoolPresetApplied")}
                          </Tag>
                        )}
                      </Button>
                    );
                    return (
                      <Tooltip
                        key={p.preset_code}
                        // 禁用必须给原因（验收报告 P1-2「沉默的失败」）：
                        // 后端未给 blocked_reason_zh 时兜底说明，避免悬浮为空
                        title={
                          blocked
                            ? (p.blocked_reason_zh ?? p.basis_zh ?? t("miningPoolPresetUnavailable"))
                            : customized
                              ? t("miningPoolPresetCustomTip")
                              : p.basis_zh
                        }
                      >
                        {presetBtn}
                      </Tooltip>
                    );
                  })}
                  {activePresets.length === 0 && (
                    <p className="mining-filter-empty" data-filter-no-preset>
                      {t("miningPoolPresetEmpty")}
                    </p>
                  )}
                </div>
              ),
            }))}
          />
        </aside>

        {/* 右栏：分类条件 Accordion */}
        <div className="mining-pool-filter-right" data-filter-right>
          <Collapse
            size="small"
            ghost
            activeKey={activeKeys}
            onChange={(keys) => setOpenKeys(keys as string[])}
            items={collapseItems}
            expandIconPosition="end"
          />
        </div>
      </div>
    </div>
  );
}

const DEFAULT_OPEN_CATEGORY = "market_status";