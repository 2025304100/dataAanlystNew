import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Download, RefreshCw, Search, AlertTriangle, ShieldAlert } from "lucide-react";
import { api, requestJson } from "../../api/client";
import { useApp } from "../../context/AppContext";
import { t } from "../../i18n";
import type { PortfolioStatePermissions, PortfolioStatusResponse, Position, WorkbenchCandidate } from "../../types";

/**
 * PortfolioMembersTable — 组合成员子 Tab（Task 5）
 *
 * 一比一还原原型图「应用主框架.html」组合成员区域：
 *   1. 操作栏：导出持仓(CSV) / 手动触发再平衡 + 「仅显示目标偏离 > 2%」开关 + 代码/名称搜索框
 *   2. 持仓表（10 列）：标的代码 / 标的名称 / 现价 / 成本价 / 持股数量 / 持仓市值 /
 *      实际占比 / 策略目标占比（含偏离值）/ 浮动盈亏（含百分比）/ 操作（调仓/平仓/一键清仓）
 *   3. 候选标的池表（7 列）：代码 / 名称 / 信号分 / 目标权重 / 所属因子 / 加入时间 / 操作（加入组合）
 *
 * 数据来源（Promise.allSettled 容错，单接口失败不阻塞 UI）：
 *   - GET /api/v1/portfolios/{id}/positions   → 持仓列表
 *   - GET /api/v1/portfolios/{id}/candidates → 当前组合的候选标的池
 *   - POST /api/v1/portfolios/{id}/members    → 加入组合
 *
 * target_weight_pct / deviation_pct 当前模型可能无此字段，前端按可选字段处理：
 * 缺失时策略目标占比/偏离显示占位「—」，待调出标的（target_weight_pct === 0）行高亮警告色。
 *
 * i18n：使用 t('portfolioTrading.members.xxx')，key 由 Task 13 补全，缺失时返回 key 字符串不阻塞。
 */
interface PortfolioMembersTableProps {
  portfolioId: number;
  onNavigate?: (tab: string) => void;
  // FR-P1-8a HG1 治理字段
  portfolioStatus?: PortfolioStatusResponse | null;
  perm?: PortfolioStatePermissions;
}

// 扩展 Position：target_weight_pct / deviation_pct 可能由后端附加（前端兼容处理）
interface PositionRow extends Position {
  target_weight_pct?: number | null;
  deviation_pct?: number | null;
  // P1-FIX: 成员数据联表字段（从 /members 合并）
  member_id?: number | null;
  member_status?: string | null;
  execution_mode?: string | null;
  source_type?: string | null;
}

// 组合成员接口返回（精简字段，用于和 Position 合并）
interface PortfolioMemberLite {
  id: number;
  portfolio_id: number;
  symbol_id: number;
  status: string;
  execution_mode: string;
  source_type: string;
  manual_lock?: boolean;
  priority?: number;
  created_at?: string | null;
  updated_at?: string | null;
}

/* ------------------------------------------------------------------ */
/* 格式化工具                                                          */
/* ------------------------------------------------------------------ */

/** 分组数字（无货币符号），digits 控制小数位 —— 用于表格内价格/市值/盈亏金额 */
const fmtNum = (v: number | null | undefined, digits = 2): string => {
  if (v == null || !Number.isFinite(Number(v))) return "--";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(v));
};

/** 带符号分组数字 —— 用于浮动盈亏金额（+8,000.00 / -4,800.00） */
const fmtSignedNum = (v: number | null | undefined, digits = 2): string => {
  if (v == null || !Number.isFinite(Number(v))) return "--";
  const n = Number(v);
  const s = fmtNum(Math.abs(n), digits);
  return n > 0 ? `+${s}` : n < 0 ? `-${s}` : s;
};

/** 百分比（输入为 0-1 小数），缺失显示 --%，可选正号 */
const fmtPct = (v: number | null | undefined, withSign = false): string => {
  if (v == null || !Number.isFinite(Number(v))) return "--%";
  const n = Number(v) * 100;
  const s = `${n.toFixed(1)}%`;
  return withSign && n > 0 ? `+${s}` : s;
};

/** 收益正绿负红，0/缺失 muted */
const pnlColor = (v: number | null | undefined): string => {
  if (v == null || !Number.isFinite(Number(v))) return "var(--pt-muted-foreground)";
  return Number(v) > 0 ? "var(--pt-state-success)" : Number(v) < 0 ? "var(--pt-state-error)" : "var(--pt-muted-foreground)";
};

/** 偏离值颜色：>5% error，>2% warning，其余默认 */
const deviationColor = (v: number | null | undefined): string => {
  if (v == null || !Number.isFinite(Number(v))) return "var(--pt-muted-foreground)";
  const abs = Math.abs(Number(v));
  if (abs > 0.05) return "var(--pt-state-error)";
  if (abs > 0.02) return "var(--pt-state-warning)";
  return "var(--pt-muted-foreground)";
};

/** 因子标签色调映射（所属因子） */
const FACTOR_TONE: Record<string, { bg: string; color: string }> = {
  动量: { bg: "var(--pt-state-info-dim)", color: "var(--pt-state-info)" },
  价值: { bg: "var(--pt-state-success-dim)", color: "var(--pt-state-success)" },
  低波: { bg: "rgba(6,182,212,0.12)", color: "var(--pt-primary)" },
  质量: { bg: "var(--pt-state-warning-dim)", color: "var(--pt-state-warning)" },
  成长: { bg: "var(--pt-state-info-dim)", color: "var(--pt-state-info)" },
};
const factorTone = (tag: string): { bg: string; color: string } =>
  FACTOR_TONE[tag] ?? { bg: "rgba(6,182,212,0.12)", color: "var(--pt-primary)" };

/* ------------------------------------------------------------------ */
/* 主组件                                                              */
/* ------------------------------------------------------------------ */

const PortfolioMembersTable: React.FC<PortfolioMembersTableProps> = ({ portfolioId, onNavigate, portfolioStatus, perm }) => {
  const { showToast } = useApp();
  // FR-P1-8a HG1 权限：fail-closed，未传时默认全部禁止
  const allowNewBuys = perm?.allow_new_buys ?? false;
  const allowRiskExits = perm?.allow_risk_exits ?? false;
  const currentState = portfolioStatus?.current_state ?? "UNKNOWN";

  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [candidates, setCandidates] = useState<WorkbenchCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deviationFilter, setDeviationFilter] = useState(true);
  const [search, setSearch] = useState("");
  const [rebalancing, setRebalancing] = useState(false);
  const [rowActionSymbolId, setRowActionSymbolId] = useState<number | null>(null);

  const load = useCallback(async () => {
    if (!portfolioId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    // P1-FIX: 3 个独立请求并行：成员、持仓、候选标的池；任一失败不阻塞其余
    const [memRes, posRes, candidateRes] = await Promise.allSettled([
      api.getMembers(portfolioId),
      api.getPositions(portfolioId),
      api.getPortfolioCandidates(portfolioId),
    ]);
    const rawPositions: PositionRow[] = posRes.status === "fulfilled"
      ? (Array.isArray(posRes.value) ? posRes.value : []) as PositionRow[]
      : [];
    const members: PortfolioMemberLite[] = memRes.status === "fulfilled"
      ? (Array.isArray(memRes.value) ? memRes.value : []) as PortfolioMemberLite[]
      : [];

    // 合并规则：以 positions 为基础，附加 member 信息；
    // 补充：members 中有但 positions 中无的（已加入成员但尚未建仓）也显示为 0 持仓行
    const posBySym = new Map<number, PositionRow>(rawPositions.map((p) => [p.symbol_id, p]));
    const merged: PositionRow[] = [];
    const seen = new Set<number>();
    // 1) 有持仓的行（来自 positions）优先，附加 member 字段
    for (const p of rawPositions) {
      seen.add(p.symbol_id);
      const m = members.find((m) => m.symbol_id === p.symbol_id);
      merged.push({
        ...p,
        member_id: m?.id ?? null,
        member_status: m?.status ?? null,
        execution_mode: m?.execution_mode ?? null,
        source_type: m?.source_type ?? null,
      });
    }
    // 2) 有成员但无持仓：合成 0 持仓行（symbol/name 字段来自 positions 里同 symbol_id 的数据，否则留空待后端回填）
    for (const m of members) {
      if (seen.has(m.symbol_id)) continue;
      const existing = posBySym.get(m.symbol_id);
      merged.push({
        id: -(m.id),
        portfolio_id: m.portfolio_id,
        symbol_id: m.symbol_id,
        symbol: existing?.symbol ?? null,
        name: existing?.name ?? null,
        quantity: 0,
        avg_cost: 0,
        latest_price: existing?.latest_price ?? 0,
        market_value: 0,
        position_pct: 0,
        asset_type: existing?.asset_type ?? "stock",
        theme: existing?.theme ?? null,
        unrealized_pnl: 0,
        unrealized_pnl_pct: 0,
        member_id: m.id,
        member_status: m.status,
        execution_mode: m.execution_mode,
        source_type: m.source_type,
      } as PositionRow);
    }
    setPositions(merged);
    if (posRes.status !== "fulfilled" && memRes.status !== "fulfilled") {
      setError(t("portfolioTrading.members.loadFailed"));
    }
    if (candidateRes.status === "fulfilled") {
      const uniqueCandidates = new Map<number, WorkbenchCandidate>();
      for (const candidate of candidateRes.value as WorkbenchCandidate[]) {
        const symbolId = Number(candidate.symbol_id);
        if (!Number.isInteger(symbolId) || symbolId <= 0) continue;
        if (!uniqueCandidates.has(symbolId)) uniqueCandidates.set(symbolId, candidate);
      }
      setCandidates(Array.from(uniqueCandidates.values()));
    } else {
      setCandidates([]);
    }
    setLoading(false);
  }, [portfolioId]);

  useEffect(() => {
    load();
  }, [load]);

  // 搜索 + 偏离过滤
  const filteredPositions = useMemo(() => {
    const q = search.trim().toLowerCase();
    return positions.filter((p) => {
      if (q) {
        const code = (p.symbol ?? "").toLowerCase();
        const name = (p.name ?? "").toLowerCase();
        if (!code.includes(q) && !name.includes(q)) return false;
      }
      // 仅显示目标偏离 > 2%：仅对存在 deviation_pct 的行生效，无偏离数据的行始终保留
      if (deviationFilter && p.deviation_pct != null && Number.isFinite(Number(p.deviation_pct))) {
        if (Math.abs(Number(p.deviation_pct)) <= 0.02) return false;
      }
      return true;
    });
  }, [positions, search, deviationFilter]);

  // 导出持仓 CSV
  const handleExportCsv = useCallback(() => {
    const headers = [
      t("portfolioTrading.members.colSymbol"),
      t("portfolioTrading.members.colName"),
      t("portfolioTrading.members.colPrice"),
      t("portfolioTrading.members.colCost"),
      t("portfolioTrading.members.colQuantity"),
      t("portfolioTrading.members.colMarketValue"),
      t("portfolioTrading.members.colActualWeight"),
      t("portfolioTrading.members.colTargetWeight"),
      t("portfolioTrading.members.colDeviation"),
      t("portfolioTrading.members.colPnL"),
      t("portfolioTrading.members.colPnLPct"),
    ];
    const rows = filteredPositions.map((p) => [
      p.symbol ?? "",
      p.name ?? "",
      p.latest_price ?? "",
      p.avg_cost ?? "",
      p.quantity ?? "",
      p.market_value ?? "",
      p.position_pct ?? "",
      p.target_weight_pct ?? "",
      p.deviation_pct ?? "",
      p.unrealized_pnl ?? "",
      p.unrealized_pnl_pct ?? "",
    ]);
    const csv = [headers, ...rows].map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `positions-${portfolioId}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [filteredPositions, portfolioId]);

  // 手动触发再平衡：POST /portfolios/{id}/auto-trade/execute（实际下单），失败 toast
  // P1-FIX: 增加二次确认，dry_run=false 直接真实下单必须确认
  // FR-P1-8a：再平衡 = NEW_BUY + RISK_EXIT 双权限
  const rebalanceDisabled = !allowNewBuys || !allowRiskExits;
  const rebalanceHint = (() => {
    if (!allowNewBuys && !allowRiskExits) return "HG1 门禁：禁止新买单/风险退出，再平衡不可用";
    if (!allowNewBuys) return "HG1 门禁：禁止新买单，再平衡不可用（需 NEW_BUY 权限）";
    if (!allowRiskExits) return "HG1 门禁：禁止风险退出，再平衡不可用（需 RISK_EXIT 权限）";
    return "";
  })();
  const handleRebalance = useCallback(async () => {
    if (!portfolioId) return;
    if (rebalanceDisabled) {
      showToast("error", "HG1 门禁：当前组合状态禁止再平衡（缺少 NEW_BUY 或 RISK_EXIT 权限）");
      return;
    }
    const confirmed = window.confirm(
      "确认对当前组合执行【手动再平衡】？\n\n这将触发自动交易执行（dry_run=false），根据策略规则实时生成买卖单并以模拟价直接撮合成交。\n\n请确认资金、持仓偏离度与风控阈值均符合预期，确认后不可撤销。",
    );
    if (!confirmed) return;
    setRebalancing(true);
    try {
      await api.executeAutoTrade(portfolioId, { dry_run: false });
      showToast("success", t("portfolioTrading.members.rebalanceTriggered"));
      await load();
    } catch (err: any) {
      const msg = err?.message || String(err);
      showToast("error", t("portfolioTrading.members.rebalanceFailed") + ": " + msg);
    } finally {
      setRebalancing(false);
    }
  }, [portfolioId, showToast, load, rebalanceDisabled]);

  // 行内「清仓 / 一键清仓」：提交模拟卖单卖出全部持仓量，成功后删除持仓并刷新
  // P1-FIX: 增加二次确认（全卖单只可能造成踏空/止盈/止损，必须用户确认）
  // FR-P1-8a：HG1 门禁，清仓/风险退出 = RISK_EXIT 权限
  const handleClosePosition = useCallback(
    async (p: PositionRow) => {
      if (!portfolioId) return;
      if (!allowRiskExits) {
        showToast("error", "HG1 门禁：当前组合状态禁止风险退出（缺少 RISK_EXIT 权限）");
        return;
      }
      const qty = Number(p.quantity);
      if (!Number.isFinite(qty) || qty <= 0) {
        showToast("info", t("portfolioTrading.members.closeFailed"));
        return;
      }
      const codeName = `${p.symbol ?? `#${p.symbol_id}`} ${p.name ?? ""}`.trim();
      const confirmed = window.confirm(
        `确认对【${codeName}】执行清仓？\n\n将以市价卖出全部 ${fmtNum(qty, 0)} 股，当前市值约 ${fmtNum(p.market_value)} 元。\n卖出后该标的将从持仓列表移除，但仍保留为组合成员（可在成员页查看）。\n\n请再次确认此操作，成交后不可撤销。`,
      );
      if (!confirmed) return;
      setRowActionSymbolId(p.symbol_id);
      try {
        await api.submitSimOrder(portfolioId, {
          symbol_id: p.symbol_id,
          side: "sell",
          quantity: qty,
          order_type: "market",
          note: "manual close",
        });
        showToast("success", t("portfolioTrading.members.closeSuccess"));
        await load();
      } catch (err: any) {
        const msg = err?.message || String(err);
        showToast("error", t("portfolioTrading.members.closeFailed") + ": " + msg);
      } finally {
        setRowActionSymbolId(null);
      }
    },
    [portfolioId, showToast, load, allowRiskExits],
  );

  // 行内「调仓」：按 target_weight_pct 计算目标股数并提交模拟买/卖单向目标靠拢
  // FR-P1-8a：HG1 门禁，调仓 diff>0 需 NEW_BUY，diff<0 需 RISK_EXIT
  const handleRebalanceRow = useCallback(
    async (p: PositionRow) => {
      if (!portfolioId) return;
      const target = p.target_weight_pct;
      if (target == null || !Number.isFinite(Number(target))) {
        showToast("info", t("portfolioTrading.members.noTargetWeight"));
        return;
      }
      const price = Number(p.latest_price);
      const posPct = Number(p.position_pct);
      const marketValue = Number(p.market_value);
      // 由 market_value / position_pct 反推总资产；缺失则无法计算目标股数
      const totalAssets = posPct > 0 && marketValue > 0 ? marketValue / posPct : 0;
      if (totalAssets <= 0 || !Number.isFinite(price) || price <= 0) {
        showToast("info", t("portfolioTrading.members.noTargetWeight"));
        return;
      }
      const targetQty = (Number(target) * totalAssets) / price;
      const currentQty = Number(p.quantity) || 0;
      const diff = targetQty - currentQty;
      if (Math.abs(diff) < 1e-6) {
        showToast("info", t("portfolioTrading.members.rebalanceRowSuccess"));
        return;
      }
      // FR-P1-8a HG1 检查
      if (diff > 0 && !allowNewBuys) {
        showToast("error", "HG1 门禁：调仓方向为买入，当前组合禁止新买单（需 NEW_BUY 权限）");
        return;
      }
      if (diff < 0 && !allowRiskExits) {
        showToast("error", "HG1 门禁：调仓方向为卖出，当前组合禁止风险退出（需 RISK_EXIT 权限）");
        return;
      }
      setRowActionSymbolId(p.symbol_id);
      try {
        await api.submitSimOrder(portfolioId, {
          symbol_id: p.symbol_id,
          side: diff > 0 ? "buy" : "sell",
          quantity: Math.abs(diff),
          order_type: "market",
          note: "manual rebalance",
        });
        showToast("success", t("portfolioTrading.members.rebalanceRowSuccess"));
        await load();
      } catch (err: any) {
        const msg = err?.message || String(err);
        showToast("error", t("portfolioTrading.members.rebalanceRowFailed") + ": " + msg);
      } finally {
        setRowActionSymbolId(null);
      }
    },
    [portfolioId, showToast, load, allowNewBuys, allowRiskExits],
  );

  // 加入组合：POST /portfolios/{id}/members
  // FR-P1-8a：HG1 门禁，加入组合（从候选池→正式成员）= NEW_BUY
  const handleAddMember = useCallback(
    async (candidate: WorkbenchCandidate) => {
      if (!allowNewBuys) {
        showToast("error", "HG1 门禁：当前组合禁止新增候选/建仓（需 NEW_BUY 权限）");
        return;
      }
      try {
        await requestJson(`/api/v1/portfolios/${portfolioId}/members`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            symbol_id: Number(candidate.symbol_id),
            status: "active",
            execution_mode: "manual",
            source_type: "candidate",
            priority: 0,
            note: null,
          }),
        });
        showToast("success", t("portfolioTrading.members.addMemberSuccess"));
        await load();
      } catch (err) {
        const msg = err instanceof Error ? err.message : "";
        showToast("error", msg || t("portfolioTrading.members.addMemberFailed"));
      }
    },
    [portfolioId, showToast, load, allowNewBuys],
  );

  if (loading) {
    return (
      <section style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
        <style>{`@keyframes pt-sk-pulse{0%,100%{opacity:1}50%{opacity:0.4}}.pt-sk{animation:pt-sk-pulse 1.4s ease-in-out infinite;}`}</style>
        <div className="pt-card" style={{ padding: 12, height: 48 }}>
          <div className="pt-sk" style={{ background: "var(--pt-surface-3)", borderRadius: 4, height: 14, width: "30%" }} />
        </div>
        <div className="pt-card" style={{ height: 260 }} />
        <div className="pt-card" style={{ height: 180 }} />
      </section>
    );
  }

  return (
    <section style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
      {/* FR-P1-8a HG1 顶部提示：非 READY 状态时显示组合状态与风险提示 */}
      {currentState !== "READY" && (
        <div
          style={{
            padding: "10px 14px",
            borderRadius: 10,
            border: `1px solid ${!allowNewBuys || !allowRiskExits ? "rgba(239,68,68,0.35)" : "rgba(245,158,11,0.35)"}`,
            background: !allowNewBuys || !allowRiskExits ? "rgba(239,68,68,0.06)" : "rgba(245,158,11,0.06)",
            display: "flex",
            alignItems: "flex-start",
            gap: 10,
            fontSize: 12.5,
            lineHeight: 1.55,
          }}
        >
          {!allowNewBuys || !allowRiskExits ? (
            <ShieldAlert size={16} style={{ marginTop: 1, color: "var(--pt-state-error, #ef4444)", flexShrink: 0 }} />
          ) : (
            <AlertTriangle size={16} style={{ marginTop: 1, color: "var(--pt-state-warning, #f59e0b)", flexShrink: 0 }} />
          )}
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600, color: (!allowNewBuys || !allowRiskExits) ? "var(--pt-state-error, #ef4444)" : "var(--pt-state-warning, #f59e0b)" }}>
              🔒 HG1 组合治理状态：「{currentState}」
            </div>
            <div style={{ marginTop: 2, color: "var(--pt-muted-foreground)" }}>
              NEW_BUY: {allowNewBuys ? "✅ 允许" : "🚫 禁止"}　|　RISK_EXIT: {allowRiskExits ? "✅ 允许" : "🚫 禁止"}
              &nbsp;&nbsp;·&nbsp;&nbsp;详细说明与解除步骤请前往「治理」子 Tab 查看。
            </div>
          </div>
        </div>
      )}
      {/* ========== SubTask 5.1: 操作栏 ========== */}
      <div className="pt-card" style={{ padding: 12 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
          {/* 左侧：导出持仓 + 手动触发再平衡 */}
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button
              type="button"
              className="pt-btn pt-btn-secondary pt-btn-sm"
              onClick={handleExportCsv}
              disabled={filteredPositions.length === 0}
            >
              <Download size={14} />
              {t("portfolioTrading.members.exportCsv")}
            </button>
            <button
              type="button"
              className="pt-btn pt-btn-primary pt-btn-sm"
              onClick={handleRebalance}
              disabled={rebalanceDisabled || rebalancing}
              title={rebalanceDisabled ? rebalanceHint : undefined}
              style={{ opacity: rebalanceDisabled ? 0.55 : 1, cursor: rebalanceDisabled ? "not-allowed" : undefined }}
            >
              <RefreshCw size={14} />
              {rebalanceDisabled ? "🔒 再平衡（HG1）" : t("portfolioTrading.members.rebalance")}
            </button>
          </div>

          {/* 右侧：偏离开关 + 搜索框 */}
          <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                {t("portfolioTrading.members.deviationFilterLabel")}
              </span>
              <button
                type="button"
                className={`pt-toggle${deviationFilter ? " active" : ""}`}
                aria-pressed={deviationFilter}
                onClick={() => setDeviationFilter((v) => !v)}
                title={t("portfolioTrading.members.deviationFilterLabel")}
              />
            </div>
            <div style={{ position: "relative" }}>
              <Search
                size={14}
                style={{
                  position: "absolute",
                  left: 8,
                  top: "50%",
                  transform: "translateY(-50%)",
                  color: "var(--pt-muted-foreground)",
                  pointerEvents: "none",
                }}
              />
              <input
                type="text"
                className="pt-input"
                placeholder={t("portfolioTrading.members.searchPlaceholder")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                style={{ width: 200, paddingLeft: 28 }}
              />
            </div>
          </div>
        </div>
      </div>

      {/* 错误提示 */}
      {error && (
        <div
          style={{
            padding: "10px 14px",
            borderRadius: "var(--pt-radius-md)",
            background: "var(--pt-state-error-dim)",
            border: "1px solid var(--pt-state-error)",
            color: "var(--pt-state-error)",
            fontSize: 12,
          }}
        >
          {error}
        </div>
      )}

      {/* ========== SubTask 5.2: 持仓表（10 列）========== */}
      <div className="pt-card" style={{ overflow: "hidden" }}>
        <div className="thin-scrollbar" style={{ overflowX: "auto" }}>
          <table className="pt-table">
            <thead>
              <tr>
                <th>{t("portfolioTrading.members.colSymbol")}</th>
                <th>{t("portfolioTrading.members.colName")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.colPrice")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.colCost")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.colQuantity")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.colMarketValue")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.colActualWeight")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.colTargetWeight")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.colPnL")}</th>
                <th style={{ textAlign: "center" }}>{t("portfolioTrading.members.colAction")}</th>
              </tr>
            </thead>
            <tbody>
              {filteredPositions.length === 0 ? (
                <tr>
                  <td colSpan={10} style={{ textAlign: "center", padding: "32px 0", color: "var(--pt-muted-foreground)" }}>
                    {t("portfolioTrading.members.emptyPositions")}
                  </td>
                </tr>
              ) : (
                filteredPositions.map((p) => {
                  // 待调出标的：目标权重为 0 → 警告色高亮 + 一键清仓
                  const isPendingExit = p.target_weight_pct != null && Number(p.target_weight_pct) === 0;
                  const weightPct = Math.max(0, Math.min(100, (Number(p.position_pct) || 0) * 100));
                  return (
                    <tr key={p.symbol_id} className={isPendingExit ? "pt-row-warn" : ""}>
                      {/* 1. 标的代码 */}
                      <td className="pt-mono" style={{ fontWeight: 500, color: "var(--pt-primary)" }}>
                        {p.symbol ?? `#${p.symbol_id}`}
                      </td>
                      {/* 2. 标的名称 */}
                      <td>{p.name ?? "—"}</td>
                      {/* 3. 现价 */}
                      <td className="pt-mono" style={{ textAlign: "right" }}>{fmtNum(p.latest_price)}</td>
                      {/* 4. 成本价 */}
                      <td className="pt-mono" style={{ textAlign: "right", color: "var(--pt-muted-foreground)" }}>
                        {fmtNum(p.avg_cost)}
                      </td>
                      {/* 5. 持股数量 */}
                      <td className="pt-mono" style={{ textAlign: "right" }}>{fmtNum(p.quantity, 0)}</td>
                      {/* 6. 持仓市值 */}
                      <td className="pt-mono" style={{ textAlign: "right" }}>{fmtNum(p.market_value)}</td>
                      {/* 7. 实际占比（百分比 + 迷你进度条）*/}
                      <td style={{ textAlign: "right" }}>
                        <span className="pt-mono">{fmtPct(p.position_pct)}</span>
                        <div className="pt-progress" style={{ marginTop: 4, width: 80, marginLeft: "auto" }}>
                          <div className="pt-progress-fill" style={{ width: `${weightPct}%` }} />
                        </div>
                      </td>
                      {/* 8. 策略目标占比（含偏离值）*/}
                      <td style={{ textAlign: "right" }}>
                        {isPendingExit ? (
                          <>
                            <span className="pt-mono" style={{ fontWeight: 500, color: "var(--pt-state-warning)" }}>
                              {fmtPct(p.target_weight_pct)}
                            </span>
                            <span style={{ fontSize: 11, marginLeft: 4, color: "var(--pt-state-warning)" }}>
                              ({t("portfolioTrading.members.pendingExit")})
                            </span>
                          </>
                        ) : p.target_weight_pct != null ? (
                          <>
                            <span className="pt-mono" style={{ fontWeight: 500, color: "var(--pt-foreground)" }}>
                              {fmtPct(p.target_weight_pct)}
                            </span>
                            {p.deviation_pct != null && Number.isFinite(Number(p.deviation_pct)) && (
                              <span style={{ fontSize: 11, marginLeft: 4, color: deviationColor(p.deviation_pct) }}>
                                ({fmtPct(p.deviation_pct, true)})
                              </span>
                            )}
                          </>
                        ) : (
                          <span className="pt-mono" style={{ color: "var(--pt-muted-foreground)" }}>—</span>
                        )}
                      </td>
                      {/* 9. 浮动盈亏（金额 + 百分比，正绿负红）*/}
                      <td style={{ textAlign: "right" }}>
                        <span className="pt-mono" style={{ color: pnlColor(p.unrealized_pnl) }}>
                          {fmtSignedNum(p.unrealized_pnl)}
                        </span>
                        <span className="pt-mono" style={{ fontSize: 11, marginLeft: 4, color: pnlColor(p.unrealized_pnl_pct) }}>
                          ({fmtPct(p.unrealized_pnl_pct, true)})
                        </span>
                      </td>
                      {/* 10. 操作 */}
                      <td style={{ textAlign: "center", whiteSpace: "nowrap" }}>
                        {isPendingExit ? (
                          <button
                            type="button"
                            className="pt-btn pt-btn-sm"
                            style={{ background: "var(--pt-state-warning)", color: "#000", fontWeight: 600, opacity: !allowRiskExits ? 0.5 : 1, cursor: !allowRiskExits ? "not-allowed" : undefined }}
                            onClick={() => handleClosePosition(p)}
                            disabled={!allowRiskExits || rowActionSymbolId === p.symbol_id}
                            title={!allowRiskExits ? "HG1 门禁：当前组合禁止风险退出" : undefined}
                          >
                            {!allowRiskExits ? "🔒 " : ""}{t("portfolioTrading.members.actionClear")}
                          </button>
                        ) : (
                          <>
                            {/* 调仓：diff>0 需要 NEW_BUY，diff<0 需要 RISK_EXIT；简化判断：当两者任一允许时启用，由 handleRebalanceRow 再细分 */}
                            {(() => {
                              const rowRebalanceDisabled = !allowNewBuys && !allowRiskExits;
                              return (
                                <button
                                  type="button"
                                  className="pt-btn pt-btn-ghost pt-btn-sm"
                                  style={{ color: "var(--pt-primary)", opacity: rowRebalanceDisabled ? 0.5 : 1, cursor: rowRebalanceDisabled ? "not-allowed" : undefined }}
                                  onClick={() => handleRebalanceRow(p)}
                                  disabled={rowRebalanceDisabled || rowActionSymbolId === p.symbol_id}
                                  title={rowRebalanceDisabled ? "HG1 门禁：当前组合禁止调仓（新买单/风险退出均不可用）" : undefined}
                                >
                                  {rowRebalanceDisabled ? "🔒 " : ""}{t("portfolioTrading.members.actionRebalance")}
                                </button>
                              );
                            })()}
                            <button
                              type="button"
                              className="pt-btn pt-btn-ghost pt-btn-sm"
                              style={{ color: "var(--pt-state-error)", opacity: !allowRiskExits ? 0.5 : 1, cursor: !allowRiskExits ? "not-allowed" : undefined }}
                              onClick={() => handleClosePosition(p)}
                              disabled={!allowRiskExits || rowActionSymbolId === p.symbol_id}
                              title={!allowRiskExits ? "HG1 门禁：当前组合禁止风险退出" : undefined}
                            >
                              {!allowRiskExits ? "🔒 " : ""}{t("portfolioTrading.members.actionClose")}
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ========== SubTask 5.3: 候选标的池表 ========== */}
      <div className="pt-card" style={{ overflow: "hidden" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "12px 16px",
            borderBottom: "1px solid var(--pt-border)",
          }}
        >
          <h3 style={{ fontSize: 14, fontWeight: 600, color: "var(--pt-foreground)", margin: 0 }}>
            {t("portfolioTrading.members.candidatePoolTitle")}
          </h3>
          <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
            {candidates.length} {t("portfolioTrading.members.candidatePoolCount")}
          </span>
        </div>
        <div className="thin-scrollbar" style={{ overflowX: "auto" }}>
          <table className="pt-table">
            <thead>
              <tr>
                <th>{t("portfolioTrading.members.candidateColCode")}</th>
                <th>{t("portfolioTrading.members.candidateColName")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.candidateColScore")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.candidateColWeight")}</th>
                <th>{t("portfolioTrading.members.candidateColFactor")}</th>
                <th style={{ textAlign: "right" }}>{t("portfolioTrading.members.candidateColAddedAt")}</th>
                <th style={{ textAlign: "center" }}>{t("portfolioTrading.members.candidateColAction")}</th>
              </tr>
            </thead>
            <tbody>
              {candidates.length === 0 ? (
                <tr>
                  <td colSpan={7} style={{ textAlign: "center", padding: "32px 0", color: "var(--pt-muted-foreground)" }}>
                    {t("portfolioTrading.members.emptyCandidates")}
                  </td>
                </tr>
              ) : (
                candidates.map((c) => {
                  const factorTag = c.reason_tags && c.reason_tags.length > 0
                    ? c.reason_tags[0]
                    : (c as WorkbenchCandidate & { factor_tag?: string }).factor_tag ?? c.stage ?? "";
                  const tone = factorTone(factorTag);
                  return (
                    <tr key={c.symbol_id}>
                      <td className="pt-mono" style={{ fontWeight: 500 }}>
                        {c.symbol ?? `#${c.symbol_id}`}
                      </td>
                      <td>{c.name ?? "—"}</td>
                      <td className="pt-mono" style={{ textAlign: "right", fontWeight: 500, color: "var(--pt-primary)" }}>
                        {c.priority_score != null ? Number(c.priority_score).toFixed(1) : "—"}
                      </td>
                      <td className="pt-mono" style={{ textAlign: "right" }}>{fmtPct(c.recommended_position_pct)}</td>
                      <td>
                        {factorTag ? (
                          <span
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              padding: "2px 8px",
                              borderRadius: "var(--pt-radius-full)",
                              fontSize: 11,
                              fontWeight: 500,
                              background: tone.bg,
                              color: tone.color,
                            }}
                          >
                            {factorTag}
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="pt-mono" style={{ textAlign: "right", color: "var(--pt-muted-foreground)" }}>
                        {c.created_at ? new Date(c.created_at).toLocaleDateString("zh-CN") : "—"}
                      </td>
                      <td style={{ textAlign: "center" }}>
                        <button
                          type="button"
                          className="pt-btn pt-btn-primary pt-btn-sm"
                          onClick={() => handleAddMember(c)}
                          disabled={!allowNewBuys}
                          title={!allowNewBuys ? "HG1 门禁：当前组合禁止新增候选/建仓（需 NEW_BUY 权限）" : undefined}
                          style={{ opacity: !allowNewBuys ? 0.55 : 1, cursor: !allowNewBuys ? "not-allowed" : undefined }}
                        >
                          {!allowNewBuys ? "🔒 " : ""}{t("portfolioTrading.members.candidateActionAdd")}
                        </button>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
};

export default PortfolioMembersTable;
