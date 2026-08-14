import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Search, X, Check, Info } from "lucide-react";
import { api, requestJson } from "../../api/client";
import { useApp } from "../../context/AppContext";
import { t } from "../../i18n";
import ThemeManagementModal from "../ThemeManagementModal";

/**
 * AddCandidateModal — 添加候选标的弹窗
 *
 * 添加候选标的采用宽版弹窗，让候选依据无需在列表中被过度截断。
 * 遮罩 rgba(0,0,0,0.6) + backdrop-filter blur(4px)。
 *
 * 三段结构：搜索筛选（搜索框 + 候选池下拉 + 4 子 Tab）/ 候选列表 / 底部选择确认。
 *
 * 选择来源复用机会中心候选；确认后补充到当前组合专属候选池，不会直接创建持仓成员。
 *
 * i18n key 前缀：portfolioTrading.addCandidate.*（由 Task 13 补全，缺失时 t() 返回 key 字符串，
 * tt() 提供 inline 中文回退，不阻塞编译且保证一比一文案）。
 */

interface AddCandidateModalProps {
  open: boolean;
  onClose: () => void;
  portfolioId: number;
  onSuccess?: () => void;
}

type PoolKey = "all" | "watchlist" | "strategy" | "watch";
type TabKey = "all" | "factor" | "technical" | "theme";
type AssetFilter = "all" | "stock" | "etf";
type RegionFilter = "all" | "cn" | "hk" | "us" | "other";
type BoardFilter = "all" | "sh_main" | "sz_main" | "chinext" | "star" | "bse";

interface CandidateRow {
  symbol_id: number;
  candidate_id: number | null;
  scan_run_id: number | null;
  symbol: string;
  name: string;
  score: number;
  assetType: "stock" | "etf";
  region: RegionFilter;
  board: Exclude<BoardFilter, "all"> | null;
  signalTags: string[];
  poolMemberships: Exclude<TabKey, "all">[];
  evidence: string;
}

interface CandidatePoolItem {
  symbol_id: number;
  candidate_id?: number | null;
  scan_run_id?: number | null;
  symbol?: string;
  name?: string;
  priority_score?: number | null;
  quality_score?: number | null;
  trend_score?: number | null;
  momentum_score?: number | null;
  event_score?: number | null;
  stage?: string | null;
  action?: string | null;
  theme?: string | null;
  reason_tags?: string[];
  pool_memberships?: Exclude<TabKey, "all">[];
  asset_type?: "stock" | "etf";
  region?: RegionFilter;
  board?: Exclude<BoardFilter, "all"> | null;
  factor?: { preset_name?: string | null };
  theme_opportunity?: {
    name?: string | null;
    mapping?: { confidence?: number | null; source_type?: string | null };
    catalyst?: { title?: string | null; score?: number | null; source_type?: string | null };
  } | null;
}

interface CandidatePoolResponse {
  counts: Record<TabKey, number>;
  items: CandidatePoolItem[];
}

/** t() + inline 回退：key 缺失时返回 fallback，保证原型图文案一比一可见。 */
function tt(key: string, fallback: string): string {
  const v = t(key);
  return v === key ? fallback : v;
}

/** stage → 中文标签（WorkbenchCandidate.stage 映射信号标签）。 */
const STAGE_LABEL: Record<string, string> = {
  accel: "趋势加速",
  cooldown: "降温观察",
  overheat: "高位过热",
  start: "启动确认",
};

const REGION_LABEL: Record<RegionFilter, string> = {
  all: "全部地区",
  cn: "A股",
  hk: "港股",
  us: "美股",
  other: "其他市场",
};

const BOARD_LABEL: Record<Exclude<BoardFilter, "all">, string> = {
  sh_main: "上证主板",
  sz_main: "深证主板",
  chinext: "创业板",
  star: "科创板",
  bse: "北交所",
};

/** 信号标签彩色映射：趋势加速=primary / 量价齐升=success / 超跌反弹=warning / 突破=info。 */
function toneForSignal(tag: string): { className: string; style?: React.CSSProperties } {
  switch (tag) {
    case "趋势加速":
      return { className: "pt-tag pt-tag-primary" };
    case "量价齐升":
    case "稳健防御":
      return { className: "pt-tag pt-tag-success" };
    case "超跌反弹":
    case "周期上行":
      return { className: "pt-tag pt-tag-warning" };
    case "突破":
    case "突破信号":
    case "价值回归":
      return { className: "pt-tag pt-tag-info" };
    case "高位过热":
      return { className: "pt-tag pt-tag-error" };
    default:
      return {
        className: "pt-tag",
        style: { background: "rgba(148,163,184,0.12)", color: "var(--pt-muted-foreground)" },
      };
  }
}

/** 评分 pill 映射：≥80 success / 60-79 warning / <60 muted。 */
function toneForScore(score: number): { className: string; style?: React.CSSProperties } {
  if (score >= 80) return { className: "pt-tag pt-tag-success" };
  if (score >= 60) return { className: "pt-tag pt-tag-warning" };
  return {
    className: "pt-tag",
    style: { background: "rgba(148,163,184,0.12)", color: "var(--pt-muted-foreground)" },
  };
}

/** WorkbenchCandidate → 展示行。候选池接口不提供实时行情，列表只展示可解释的入池证据。 */
function poolEvidence(c: CandidatePoolItem, memberships: Exclude<TabKey, "all">[]): string {
  const evidence: string[] = [];
  if (memberships.includes("factor")) {
    evidence.push(`因子 ${Math.round(Number(c.quality_score ?? 0))}`);
  }
  if (memberships.includes("technical")) {
    evidence.push(`技术 ${STAGE_LABEL[c.stage ?? ""] ?? c.stage ?? "待确认"}/${c.action ?? "-"} · 趋势 ${Math.round(Number(c.trend_score ?? 0))} · 动量 ${Math.round(Number(c.momentum_score ?? 0))}`);
  }
  if (memberships.includes("theme")) {
    const opportunity = c.theme_opportunity;
    evidence.push(`主题 ${opportunity?.name ?? "未标注"} · 催化 ${opportunity?.catalyst?.title ?? "-"} · 评分 ${Math.round(Number(opportunity?.catalyst?.score ?? 0))}`);
  }
  return evidence.join("；");
}

function mapCandidate(c: CandidatePoolItem): CandidateRow {
  const tags: string[] = [];
  if (c.reason_tags && c.reason_tags.length > 0) {
    tags.push(...c.reason_tags.slice(0, 3));
  } else if (c.stage) {
    tags.push(STAGE_LABEL[c.stage] ?? c.stage);
  }
  const score = Number(c.priority_score ?? c.quality_score ?? 0);
  const memberships = (c.pool_memberships ?? []).filter((item): item is Exclude<TabKey, "all"> => item === "factor" || item === "technical" || item === "theme");
  return {
    symbol_id: c.symbol_id,
    candidate_id: c.candidate_id ?? null,
    scan_run_id: c.scan_run_id ?? null,
    symbol: c.symbol ?? `#${c.symbol_id}`,
    name: c.name ?? "",
    score,
    assetType: c.asset_type ?? "stock",
    region: c.region ?? "other",
    board: c.board ?? null,
    signalTags: tags.length > 0 ? tags : memberships.map((item) => ({ factor: "因子评分", technical: "技术信号", theme: "主题机会" })[item]),
    poolMemberships: memberships,
    evidence: poolEvidence(c, memberships),
  };
}

const POOLS: { key: PoolKey; fallback: string }[] = [
  { key: "all", fallback: "全部候选池" },
  { key: "watchlist", fallback: "自选候选池" },
  { key: "strategy", fallback: "策略候选池" },
  { key: "watch", fallback: "关注候选池" },
];

const TABS: { key: TabKey; fallback: string }[] = [
  { key: "all", fallback: "全部" },
  { key: "factor", fallback: "因子评分池" },
  { key: "technical", fallback: "技术信号池" },
  { key: "theme", fallback: "主题机会池" },
];

const TAB_HELP: Record<TabKey, { title: string; description: string; rule: string }> = {
  all: {
    title: "全部候选",
    description: "同一轮发现快照中满足至少一个入池规则的标的。一个标的可以同时属于多个池。",
    rule: "服务端统一计算归属，页面只做搜索和展示。",
  },
  factor: {
    title: "因子评分池",
    description: "依赖质量因子评分快照，不是简单按名称或标签匹配。",
    rule: "质量评分 ≥ 60 且优先级评分 ≥ 60，并且存在评分配置/维度快照。",
  },
  technical: {
    title: "技术信号池 · 怎么看",
    description: "它回答“现在是不是值得重点观察的时点”：只保留走势刚启动或仍在加速、且趋势/动量数据有效的标的；不是自动买入指令。",
    rule: "入池条件：启动（start）/加速（accel）+ 建仓（open）/持有（hold）/回调买入（buy_dip）+ 有趋势或动量评分。已有仓位可重点跟踪；未持仓仍应结合风险和仓位判断。",
  },
  theme: {
    title: "主题机会池 · 怎么看",
    description: "它用来发现“某个主题此刻有催化、值得进一步研究”的标的。主题名称只是分类；没有当期事件证据，不会因为贴了热门主题就入池，也不是追热点或自动买入指令。",
    rule: "入池条件：已确认的标的-主题映射（置信度 ≥ 60%）+ 未过期催化评分 ≥ 60。每条结果都展示映射来源、催化事件和有效期。",
  },
};

const FilterSelect: React.FC<{
  label: string;
  value: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  options: Array<readonly [string, string]>;
}> = ({ label, value, disabled = false, onChange, options }) => (
  <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--pt-muted-foreground)" }}>
    <span>{label}</span>
    <select
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      style={{
        height: 30,
        minWidth: 92,
        padding: "0 8px",
        borderRadius: 6,
        border: "1px solid var(--pt-border)",
        background: "var(--pt-surface-3)",
        color: "var(--pt-foreground)",
        fontSize: 11,
        opacity: disabled ? 0.75 : 1,
      }}
    >
      {options.map(([optionValue, optionLabel]) => (
        <option key={optionValue} value={optionValue}>{optionLabel}</option>
      ))}
    </select>
  </label>
);

const AddCandidateModal: React.FC<AddCandidateModalProps> = ({ open, onClose, portfolioId, onSuccess }) => {
  const { showToast, portfolios } = useApp();
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState<TabKey>("all");
  const [assetFilter, setAssetFilter] = useState<AssetFilter>("all");
  const [regionFilter, setRegionFilter] = useState<RegionFilter>("all");
  const [boardFilter, setBoardFilter] = useState<BoardFilter>("all");
  const [rows, setRows] = useState<CandidateRow[]>([]);
  const [counts, setCounts] = useState<Record<TabKey, number>>({ all: 0, factor: 0, technical: 0, theme: 0 });
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [themeManagerOpen, setThemeManagerOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // Some lightweight render contexts (including unit tests while the app is
  // bootstrapping) do not populate the portfolio directory yet.  Treat that
  // state as mixed rather than failing to open the candidate dialog.
  const portfolioScope = (portfolios ?? []).find((portfolio) => portfolio.id === portfolioId)?.asset_scope ?? "mixed";
  const effectiveAssetType: "stock" | "etf" | undefined =
    portfolioScope === "mixed"
      ? assetFilter === "all" ? undefined : assetFilter
      : portfolioScope;
  const effectiveRegion = regionFilter === "all" ? undefined : regionFilter;
  const effectiveBoard = regionFilter === "cn" && effectiveAssetType !== "etf" && boardFilter !== "all" ? boardFilter : undefined;

  // 复用机会中心的真实候选池，并排除当前组合中已有的成员。
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setLoadFailed(false);
      try {
        const candidateRequest = effectiveAssetType || effectiveRegion || effectiveBoard
          ? api.getDiscoveryCandidatePools("all", 0, 200, effectiveAssetType, effectiveRegion, effectiveBoard)
          : api.getDiscoveryCandidatePools("all", 0, 200);
        const [candidateResult, portfolioCandidateResult] = await Promise.allSettled([
          candidateRequest,
          portfolioId > 0 ? api.getPortfolioCandidates(portfolioId) : Promise.resolve([]),
        ]);
        if (cancelled) return;
        if (candidateResult.status === "rejected") throw candidateResult.reason;

        const portfolioCandidateIds = new Set<number>(
          portfolioCandidateResult.status === "fulfilled"
            ? (portfolioCandidateResult.value as Array<{ symbol_id?: number }>).map((item) => Number(item.symbol_id))
            : [],
        );
        const poolResult = candidateResult.value as CandidatePoolResponse;
        const uniqueCandidates = new Map<number, CandidatePoolItem>();
        for (const candidate of poolResult.items ?? []) {
          const symbolId = Number(candidate.symbol_id);
          if (!Number.isInteger(symbolId) || symbolId <= 0 || portfolioCandidateIds.has(symbolId)) continue;
          if (!uniqueCandidates.has(symbolId)) uniqueCandidates.set(symbolId, candidate);
        }
        setRows(Array.from(uniqueCandidates.values()).map(mapCandidate));
        setCounts(poolResult.counts ?? { all: 0, factor: 0, technical: 0, theme: 0 });
        setSelectedIds(new Set());
      } catch {
        if (!cancelled) {
          setRows([]);
          setLoadFailed(true);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [open, portfolioId, portfolioScope, effectiveAssetType, effectiveRegion, effectiveBoard, reloadVersion]);

  // 关闭时重置状态
  useEffect(() => {
    if (open) return;
    setSearch("");
    setTab("all");
    setAssetFilter("all");
    setRegionFilter("all");
    setBoardFilter("all");
    setSelectedIds(new Set());
    setSubmitting(false);
    setRows([]);
    setCounts({ all: 0, factor: 0, technical: 0, theme: 0 });
    setLoadFailed(false);
  }, [open]);

  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 搜索过滤（代码 / 名称）
  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const tabRows = tab === "all" ? rows : rows.filter((row) => row.poolMemberships.includes(tab));
    if (!q) return tabRows;
    return tabRows.filter((r) => r.symbol.toLowerCase().includes(q) || r.name.toLowerCase().includes(q));
  }, [rows, search, tab]);

  const emptyHint = tab === "technical"
    ? "当前扫描没有标的同时满足：启动/加速、open/hold/buy_dip，且具有趋势或动量评分。"
    : tab === "theme"
      ? "当前扫描没有标的同时满足：主题映射已确认且置信度达标，并存在未过期的高分催化。"
      : tab === "factor"
        ? "当前扫描没有满足质量评分、优先级评分及评分快照要求的标的。"
        : "当前扫描暂无满足任一入池规则的候选标的。";

  const toggleSelect = useCallback((id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectedRows = useMemo(
    () => rows.filter((r) => selectedIds.has(r.symbol_id)),
    [rows, selectedIds],
  );

  // 确认添加：补充到当前组合的候选池（不会自动加入持仓成员）。
  const handleConfirm = useCallback(async () => {
    if (selectedRows.length === 0 || submitting) return;
    // 过滤无效 symbol_id（<=0 为占位/mock 数据，不可提交）
    const validRows = selectedRows.filter((r) => Number(r.symbol_id) > 0);
    if (validRows.length === 0) {
      showToast("error", tt("portfolioTrading.addCandidate.confirmFailed", "添加失败"));
      return;
    }
    setSubmitting(true);
    const payload = validRows.map((r) => ({
      symbol_id: r.symbol_id,
      source_candidate_id: r.candidate_id,
      source_type: tab === "all" ? r.poolMemberships[0] ?? "manual" : tab,
      source_scan_run_id: r.scan_run_id,
      pool_memberships: r.poolMemberships,
      priority_score: r.score,
      factor_tag: r.poolMemberships.includes("factor") ? "因子评分池" : null,
    }));
    try {
      const results = await Promise.allSettled(
        payload.map((p) =>
          requestJson(`/api/v1/portfolios/${portfolioId}/candidates`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(p),
          }),
        ),
      );
      const failed = results.filter((r) => r.status === "rejected").length;
      if (failed === 0) {
        showToast("success", tt("portfolioTrading.addCandidate.confirmSuccess", "添加成功"));
        onSuccess?.();
        onClose();
      } else if (failed < results.length) {
        showToast("error", tt("portfolioTrading.addCandidate.partialFailed", "部分标的添加失败"));
        onSuccess?.();
        onClose();
      } else {
        showToast("error", tt("portfolioTrading.addCandidate.confirmFailed", "添加失败"));
        setSubmitting(false);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "";
      showToast("error", msg || tt("portfolioTrading.addCandidate.confirmFailed", "添加失败"));
      setSubmitting(false);
    }
  }, [selectedRows, submitting, portfolioId, showToast, onClose, onSuccess]);

  if (!open) return null;

  return (
    <>
      <style>{`
        @keyframes pt-acm-fade-in { from { opacity: 0; } to { opacity: 1; } }
        @keyframes pt-acm-scale-in {
          from { opacity: 0; transform: scale(0.96) translateY(-4px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }
        .pt-acm-backdrop { animation: pt-acm-fade-in 0.25s ease-out forwards; }
        .pt-acm-panel { animation: pt-acm-scale-in 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
        .pt-acm-no-scrollbar::-webkit-scrollbar { display: none; }
        .pt-acm-no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
      `}</style>

      {/* 背景模糊遮罩 */}
      <div
        className="pt-acm-backdrop"
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 1000,
          background: "rgba(0, 0, 0, 0.6)",
          backdropFilter: "blur(4px)",
          WebkitBackdropFilter: "blur(4px)",
        }}
      />

      {/* 居中模态框 */}
      <div
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 1001,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 16,
          pointerEvents: "none",
        }}
      >
        <div
          className="pt-acm-panel"
          style={{
            pointerEvents: "auto",
            width: "min(780px, calc(100vw - 32px))",
            maxHeight: "calc(100vh - 80px)",
            display: "flex",
            flexDirection: "column",
            background: "var(--pt-surface-2)",
            border: "1px solid var(--pt-border)",
            borderRadius: 12,
            boxShadow: "0 20px 40px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(255, 255, 255, 0.02)",
          }}
        >
          {/* ===== Header ===== */}
          <div
            style={{
              display: "flex",
              alignItems: "flex-start",
              justifyContent: "space-between",
              padding: "20px 20px 16px",
              borderBottom: "1px solid var(--pt-border)",
            }}
          >
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <h2
                style={{
                  margin: 0,
                  fontSize: 18,
                  fontWeight: 600,
                  color: "var(--pt-white)",
                  letterSpacing: "-0.01em",
                }}
              >
                {tt("portfolioTrading.addCandidate.title", "添加候选标的")}
              </h2>
              <p style={{ margin: 0, fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                {tt("portfolioTrading.addCandidate.subtitle", "从候选池中选择标的加入当前组合")}
              </p>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <button
                type="button"
                className="pt-btn pt-btn-ghost pt-btn-sm"
                onClick={() => setThemeManagerOpen(true)}
                title="创建主题、关联标的和添加催化证据"
              >
                设置主题
              </button>
              <button
                type="button"
                onClick={onClose}
                aria-label={tt("portfolioTrading.addCandidate.close", "关闭")}
                style={{
                  width: 32,
                  height: 32,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  borderRadius: 6,
                  background: "var(--pt-surface-3)",
                  color: "var(--pt-muted-foreground)",
                  border: "none",
                  cursor: "pointer",
                  flexShrink: 0,
                  marginTop: 2,
                  transition: "all 0.15s ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--pt-surface-4)";
                  e.currentTarget.style.color = "var(--pt-foreground)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "var(--pt-surface-3)";
                  e.currentTarget.style.color = "var(--pt-muted-foreground)";
                }}
              >
                <X size={16} />
              </button>
            </div>
          </div>

          {/* ===== Search & Filter ===== */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 12,
              padding: "12px 20px",
              borderBottom: "1px solid var(--pt-border)",
            }}
          >
            <div style={{ position: "relative", flex: 1, display: "flex", alignItems: "center" }}>
              <Search
                size={14}
                style={{
                  position: "absolute",
                  left: 12,
                  color: "var(--pt-muted-foreground)",
                  pointerEvents: "none",
                }}
              />
              <input
                type="text"
                className="pt-input"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={tt("portfolioTrading.addCandidate.searchPlaceholder", "输入代码或名称搜索")}
                style={{ width: "100%", paddingLeft: 32 }}
              />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <FilterSelect
                label="资产类型"
                value={portfolioScope === "mixed" ? assetFilter : portfolioScope}
                disabled={portfolioScope !== "mixed"}
                onChange={(value) => {
                  const next = value as AssetFilter;
                  setAssetFilter(next);
                  if (next === "etf") setBoardFilter("all");
                }}
                options={[
                  ["all", "全部资产"],
                  ["stock", "股票"],
                  ["etf", "ETF"],
                ]}
              />
              <FilterSelect
                label="地区"
                value={regionFilter}
                onChange={(value) => {
                  const next = value as RegionFilter;
                  setRegionFilter(next);
                  if (next !== "cn") setBoardFilter("all");
                }}
                options={[
                  ["all", "全部地区"],
                  ["cn", "A股"],
                  ["hk", "港股"],
                  ["us", "美股"],
                  ["other", "其他"],
                ]}
              />
              {regionFilter === "cn" && effectiveAssetType !== "etf" && (
                <FilterSelect
                  label="A股板块"
                  value={boardFilter}
                  onChange={(value) => setBoardFilter(value as BoardFilter)}
                  options={[
                    ["all", "全部板块"],
                    ["sh_main", "上证主板"],
                    ["sz_main", "深证主板"],
                    ["chinext", "创业板"],
                    ["star", "科创板"],
                    ["bse", "北交所"],
                  ]}
                />
              )}
            </div>
          </div>

          {/* ===== Tab Navigation ===== */}
          <div
            style={{
              display: "flex",
              gap: 24,
              padding: "0 20px",
              borderBottom: "1px solid var(--pt-border)",
            }}
          >
            {TABS.map((tb) => (
              <button
                key={tb.key}
                type="button"
                className={`pt-tab${tab === tb.key ? " active" : ""}`}
                onClick={() => setTab(tb.key)}
                title={`${TAB_HELP[tb.key].description}\n${TAB_HELP[tb.key].rule}`}
              >
                {tt(`portfolioTrading.addCandidate.tab.${tb.key}`, tb.fallback)}
                <span style={{ marginLeft: 5, opacity: 0.7, fontSize: 11 }}>{counts[tb.key] ?? 0}</span>
              </button>
            ))}
          </div>

          {/* 当前页签的匹配口径：把服务端规则直接展示给用户，避免“数字池”黑箱。 */}
          <div
            role="note"
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 8,
              margin: "10px 20px 2px",
              padding: "9px 11px",
              borderRadius: 8,
              background: "rgba(56,189,248,0.07)",
              border: "1px solid rgba(56,189,248,0.18)",
              color: "var(--pt-muted-foreground)",
              fontSize: 11,
              lineHeight: 1.55,
            }}
          >
            <Info size={14} style={{ color: "var(--pt-primary)", flexShrink: 0, marginTop: 2 }} />
            <div>
              <div style={{ color: "var(--pt-foreground)", fontWeight: 600 }}>{TAB_HELP[tab].title} · 怎么匹配</div>
              <div>{TAB_HELP[tab].description}</div>
              <div style={{ color: "var(--pt-primary)" }}>{TAB_HELP[tab].rule}</div>
            </div>
          </div>

          {/* ===== Candidate List ===== */}
          <div
            className="thin-scrollbar"
            style={{ flex: 1, overflowY: "auto", minHeight: 0 }}
          >
            {loading ? (
              <div
                style={{
                  padding: "40px 0",
                  textAlign: "center",
                  color: "var(--pt-muted-foreground)",
                  fontSize: 12,
                }}
              >
                {tt("portfolioTrading.addCandidate.loading", "加载中...")}
              </div>
            ) : loadFailed ? (
              <div
                style={{
                  padding: "40px 0",
                  textAlign: "center",
                  color: "var(--pt-muted-foreground)",
                  fontSize: 12,
                }}
              >
                <div>{tt("portfolioTrading.addCandidate.loadFailed", "候选池加载失败")}</div>
                <button
                  type="button"
                  className="pt-btn pt-btn-ghost pt-btn-sm"
                  onClick={() => setReloadVersion((value) => value + 1)}
                  style={{ marginTop: 12 }}
                >
                  {tt("portfolioTrading.addCandidate.retry", "重试")}
                </button>
              </div>
            ) : filteredRows.length === 0 ? (
              <div
                style={{
                  padding: "40px 0",
                  textAlign: "center",
                  color: "var(--pt-muted-foreground)",
                  fontSize: 12,
                }}
              >
                {search.trim()
                  ? tt("portfolioTrading.addCandidate.empty", "未找到匹配的候选标的")
                  : emptyHint}
              </div>
            ) : (
              filteredRows.map((r) => {
                const checked = selectedIds.has(r.symbol_id);
                const scoreTone = toneForScore(r.score);
                return (
                  <div
                    key={r.symbol_id}
                    onClick={() => toggleSelect(r.symbol_id)}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                      padding: "0 20px",
                      minHeight: 72,
                      borderBottom: "1px solid var(--pt-border)",
                      cursor: "pointer",
                      transition: "background 0.15s ease",
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.background = "var(--pt-surface-3)";
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.background = "transparent";
                    }}
                  >
                    {/* 自定义复选框（青色选中） */}
                    <div
                      role="checkbox"
                      aria-checked={checked}
                      style={{
                        width: 16,
                        height: 16,
                        borderRadius: 3,
                        border: `1.5px solid ${checked ? "var(--pt-primary)" : "var(--pt-slate-500)"}`,
                        background: checked ? "var(--pt-primary)" : "transparent",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        flexShrink: 0,
                        transition: "all 0.15s ease",
                      }}
                    >
                      {checked && (
                        <Check size={12} color="var(--pt-primary-foreground)" strokeWidth={3} />
                      )}
                    </div>

                    {/* 代码 + 名称 + 市场分类：分类必须紧贴标的本身，不能被长证据文本挤掉。 */}
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "flex-start",
                        gap: 5,
                        width: 190,
                        minWidth: 0,
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0, width: "100%" }}>
                        <span
                          className="pt-mono"
                          style={{ fontSize: 12, color: "var(--pt-primary)", flexShrink: 0 }}
                        >
                          {r.symbol}
                        </span>
                        <span
                          style={{
                            fontSize: 12,
                            fontWeight: 500,
                            color: "var(--pt-white)",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {r.name}
                        </span>
                      </div>
                      <span
                        className={r.region === "cn" && r.board ? "pt-tag pt-tag-info" : "pt-tag"}
                        style={{ fontSize: 10, flexShrink: 0 }}
                      >
                        {`${REGION_LABEL[r.region]} · ${r.assetType === "etf" ? "ETF" : r.board ? BOARD_LABEL[r.board] : "股票"}`}
                      </span>
                    </div>

                    {/* 评分 pill + 信号标签 */}
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        flex: 1,
                        minWidth: 0,
                      }}
                    >
                      <span
                        className={scoreTone.className}
                        style={{
                          minWidth: 28,
                          height: 20,
                          justifyContent: "center",
                          padding: "0 6px",
                          borderRadius: 10,
                          ...scoreTone.style,
                        }}
                      >
                        {r.score}
                      </span>
                      {r.signalTags.map((tag, i) => {
                        const tone = toneForSignal(tag);
                        return (
                          <span
                            key={i}
                            className={tone.className}
                            style={{ fontSize: 11, ...tone.style }}
                          >
                            {tag}
                          </span>
                        );
                      })}
                      <span
                        style={{
                          flex: 1,
                          minWidth: 0,
                          fontSize: 10,
                          color: "var(--pt-muted-foreground)",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {r.evidence}
                      </span>
                    </div>

                  </div>
                );
              })
            )}
          </div>

          {/* ===== Bottom Selection Bar ===== */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              padding: "12px 20px",
              borderTop: "1px solid var(--pt-border)",
              background: "var(--pt-surface-2)",
              borderRadius: "0 0 12px 12px",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                flex: 1,
                minWidth: 0,
                overflow: "hidden",
              }}
            >
              <span
                style={{
                  fontSize: 12,
                  color: "var(--pt-muted-foreground)",
                  flexShrink: 0,
                }}
              >
                {tt("portfolioTrading.addCandidate.selected", "已选")}{" "}
                <span style={{ color: "var(--pt-primary)", fontWeight: 500 }}>
                  {selectedRows.length}
                </span>{" "}
                {tt("portfolioTrading.addCandidate.selectedUnit", "只")}
              </span>
              <div
                className="pt-acm-no-scrollbar"
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  flex: 1,
                  minWidth: 0,
                  overflowX: "auto",
                }}
              >
                {selectedRows.map((r) => (
                  <span
                    key={r.symbol_id}
                    className="pt-tag pt-tag-primary"
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                      flexShrink: 0,
                    }}
                  >
                    {r.name}
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleSelect(r.symbol_id);
                      }}
                      aria-label={tt("portfolioTrading.addCandidate.remove", "移除")}
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        background: "transparent",
                        border: "none",
                        color: "var(--pt-primary)",
                        cursor: "pointer",
                        padding: 0,
                        marginLeft: 2,
                      }}
                    >
                      <X size={12} />
                    </button>
                  </span>
                ))}
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
              <button
                type="button"
                className="pt-btn pt-btn-ghost"
                onClick={onClose}
                disabled={submitting}
              >
                {tt("portfolioTrading.addCandidate.cancel", "取消")}
              </button>
              <button
                type="button"
                className="pt-btn pt-btn-primary"
                onClick={handleConfirm}
                disabled={selectedRows.length === 0 || submitting}
              >
                {submitting
                  ? tt("portfolioTrading.addCandidate.adding", "添加中...")
                  : tt("portfolioTrading.addCandidate.confirm", "确认添加")}
              </button>
            </div>
          </div>
        </div>
      </div>
      <ThemeManagementModal open={themeManagerOpen} onClose={() => setThemeManagerOpen(false)} />
    </>
  );
};

export default AddCandidateModal;
