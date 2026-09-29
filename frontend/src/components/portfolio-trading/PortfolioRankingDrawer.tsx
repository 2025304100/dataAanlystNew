import React, { useMemo, useState, useCallback, useEffect } from "react";
import { X, Calendar, Layers, ChevronRight, Loader2 } from "lucide-react";
import { useApp } from "../../context/AppContext";
import { t, template } from "../../i18n";
import { api } from "../../api/client";

/**
 * PortfolioRankingDrawer — 组合排名抽屉
 *
 * 一比一还原原型图「组合排名抽屉.html」：480px 右侧抽屉，3 子 Tab（收益率/最大回撤/夏普比率）
 * + 排名列表（排名徽章 + 组合名 + 实盘/模拟标签 + 成立天数 + 标的数 + 指标值 + 查看按钮）+ 底部操作栏。
 *
 * 数据派生：只用 AppContext.portfolios（真实组合）+ 真实绩效接口；
 * 绩效拿不到就渲染 "--"，没有组合就走空态——不再用 mock 组合/mock 指标冒充业绩（VIZ-0929-16）。
 *
 * i18n key 前缀：portfolioTrading.ranking.* 与 portfolioTrading.tag.*（由 Task 13 补全，
 * 缺失时 t()/template() 返回 key 字符串，不阻塞编译）。
 */

interface PortfolioRankingDrawerProps {
  open: boolean;
  onClose: () => void;
  onSelectPortfolio?: (id: number) => void;
}

type RankingMetric = "return" | "drawdown" | "sharpe";

interface RankingItem {
  id: number;
  name: string;
  isLive: boolean; // true=实盘 false=模拟
  establishedDays: number;
  symbolCount: number;
  returnPct: number; // 收益率 %
  maxDrawdownPct: number; // 最大回撤 %（负值）
  sharpe: number; // 夏普比率
}


/** 排名徽章样式：1 金 / 2 银 / 3 铜 / 其余默认。圆形 28x28，font-weight 700。 */
function rankBadgeStyle(rank: number): React.CSSProperties {
  const base: React.CSSProperties = {
    width: 28,
    height: 28,
    borderRadius: "50%",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 13,
    fontWeight: 700,
    flexShrink: 0,
    fontFamily: "var(--pt-font-mono)",
  };
  if (rank === 1) return { ...base, background: "#fbbf24", color: "#1a1a1a" };
  if (rank === 2) return { ...base, background: "#cbd5e1", color: "#1a1a1a" };
  if (rank === 3) return { ...base, background: "#d97706", color: "#ffffff" };
  return { ...base, background: "var(--pt-surface-4)", color: "var(--pt-muted-foreground)" };
}

/** 指标值格式化与配色：收益率正绿负红 / 最大回撤红色 / 夏普青色。NaN 显示 -- */
function formatMetric(item: RankingItem, metric: RankingMetric): { value: string; color: string } {
  if (metric === "return") {
    const v = item.returnPct;
    if (!Number.isFinite(v)) return { value: "--", color: "var(--pt-muted-foreground)" };
    const sign = v > 0 ? "+" : "";
    return {
      value: `${sign}${v.toFixed(1)}%`,
      color: v >= 0 ? "var(--pt-state-success)" : "var(--pt-state-error)",
    };
  }
  if (metric === "drawdown") {
    const v = item.maxDrawdownPct;
    if (!Number.isFinite(v)) return { value: "--", color: "var(--pt-muted-foreground)" };
    return {
      value: `${v.toFixed(1)}%`,
      color: "var(--pt-state-error)",
    };
  }
  const v = item.sharpe;
  if (!Number.isFinite(v)) return { value: "--", color: "var(--pt-muted-foreground)" };
  return {
    value: v.toFixed(2),
    color: "var(--pt-primary)",
  };
}

const PortfolioRankingDrawer: React.FC<PortfolioRankingDrawerProps> = ({
  open,
  onClose,
  onSelectPortfolio,
}) => {
  const { portfolios } = useApp();
  const [activeMetric, setActiveMetric] = useState<RankingMetric>("return");
  // P0-FIX: 真实绩效缓存（portfolio_id -> RankingItem），loading 状态表示正在加载
  const [perfMap, setPerfMap] = useState<Record<number, Partial<RankingItem>>>({});
  const [perfLoading, setPerfLoading] = useState(false);

  // 抽屉打开时，为每个组合并行获取真实绩效和持仓数量
  useEffect(() => {
    if (!open) return;
    if (!portfolios || portfolios.length === 0) return;
    let cancelled = false;
    setPerfLoading(true);
    (async () => {
      const pending = portfolios.map(async (p) => {
        // 1) 绩效指标：模拟组合调用 /performance，失败或非模拟 fallback 0/NaN
        let returnPct: number = 0;
        let maxDrawdownPct: number = 0;
        let sharpe: number = 0;
        try {
          const perf = await api.getPortfolioPerformance(p.id);
          if (perf) {
            if (typeof perf.total_return_pct === "number" && Number.isFinite(perf.total_return_pct)) {
              returnPct = perf.total_return_pct * 100; // ratio(0.287) -> %(28.7)
            }
            if (typeof perf.max_drawdown_pct === "number" && Number.isFinite(perf.max_drawdown_pct)) {
              maxDrawdownPct = perf.max_drawdown_pct * 100; // ratio(-0.082) -> %(-8.2)
            }
            if (typeof perf.sharpe_ratio === "number" && Number.isFinite(perf.sharpe_ratio)) {
              sharpe = perf.sharpe_ratio;
            }
          }
        } catch {
          // 非模拟组合返回 400 或其他错误：指标保持 0，会在下方显示 "--"
          returnPct = NaN;
          maxDrawdownPct = NaN;
          sharpe = NaN;
        }
        // 2) 标的数：从 /positions 获取真实持仓数
        let symbolCount: number = 0;
        try {
          const pos = await api.getPositions(p.id);
          symbolCount = Array.isArray(pos) ? pos.length : 0;
        } catch {
          symbolCount = 0;
        }
        return {
          id: p.id,
          data: { returnPct, maxDrawdownPct, sharpe, symbolCount } as Partial<RankingItem>,
        };
      });
      const results = await Promise.all(pending);
      if (cancelled) return;
      const map: Record<number, Partial<RankingItem>> = {};
      for (const r of results) map[r.id] = r.data;
      setPerfMap(map);
      setPerfLoading(false);
    })();
    return () => { cancelled = true; };
  }, [open, portfolios]);

  // 派生排名数据：只用真实组合 + 真实绩效；绩效取不到就是 NaN（渲染成 "--"）。
  // VIZ-0929-16：此前没有组合时会铺 8 条原型图 mock 组合，用户看到的是伪造的收益率/夏普，
  // 现在改为空列表走空态——宁可显示"无数据"，也不给一套看起来像真业绩的数字。
  const items = useMemo<RankingItem[]>(() => {
    const baseList: RankingItem[] = [];
    if (portfolios && portfolios.length > 0) {
      for (const p of portfolios) {
        const isLive = p.account_type === "manual";
        let establishedDays = 90;
        try {
          const created = new Date(p.created_at);
          if (!isNaN(created.getTime())) {
            establishedDays = Math.max(1, Math.floor((Date.now() - created.getTime()) / 86400000));
          }
        } catch { /* ignore */ }
        const real = perfMap[p.id];
        baseList.push({
          id: p.id,
          name: p.name,
          isLive,
          establishedDays,
          // P0-FIX: 真实绩效优先，无则 0 显示为 "--"
          symbolCount: real?.symbolCount ?? 0,
          returnPct: real?.returnPct ?? NaN,
          maxDrawdownPct: real?.maxDrawdownPct ?? NaN,
          sharpe: real?.sharpe ?? NaN,
        });
      }
    }
    return baseList;
  }, [portfolios, perfMap]);

  // 按当前指标排序：收益率/夏普降序；最大回撤越大（越接近 0）越好；NaN 始终排末尾
  const ranked = useMemo<RankingItem[]>(() => {
    const copy = [...items];
    const getVal = (a: RankingItem): number => {
      if (activeMetric === "return") return a.returnPct;
      if (activeMetric === "drawdown") return a.maxDrawdownPct;
      return a.sharpe;
    };
    copy.sort((a, b) => {
      const va = getVal(a);
      const vb = getVal(b);
      const aNaN = !Number.isFinite(va);
      const bNaN = !Number.isFinite(vb);
      if (aNaN && bNaN) return 0;
      if (aNaN) return 1; // NaN 排最后
      if (bNaN) return -1;
      if (activeMetric === "drawdown") return vb - va; // 回撤越接近 0 越靠前
      return vb - va; // 收益率/夏普降序
    });
    return copy;
  }, [items, activeMetric]);

  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 选中某组合：关闭抽屉并通知父组件切换；catch 块用 t() 兜底
  const handleSelect = useCallback(
    (id: number) => {
      onClose();
      try {
        onSelectPortfolio?.(id);
      } catch (err) {
        console.warn(t("portfolioTrading.ranking.switchFailed"), err);
      }
    },
    [onClose, onSelectPortfolio],
  );

  if (!open) return null;

  const TABS: { key: RankingMetric; label: string }[] = [
    { key: "return", label: t("portfolioTrading.ranking.tabReturn") },
    { key: "drawdown", label: t("portfolioTrading.ranking.tabDrawdown") },
    { key: "sharpe", label: t("portfolioTrading.ranking.tabSharpe") },
  ];

  return (
    <>
      {/* scoped 动画 + 滚动条样式（自包含，不污染全局） */}
      <style>{`
        @keyframes pt-drawer-slide-in {
          from { transform: translateX(100%); }
          to { transform: translateX(0); }
        }
        @keyframes pt-drawer-fade-in {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        .pt-ranking-drawer-scrim { animation: pt-drawer-fade-in 0.2s ease-out; }
        .pt-ranking-drawer-panel { animation: pt-drawer-slide-in 0.25s cubic-bezier(0.16, 1, 0.3, 1); }
        .pt-ranking-list::-webkit-scrollbar { width: 6px; }
        .pt-ranking-list::-webkit-scrollbar-track { background: transparent; }
        .pt-ranking-list::-webkit-scrollbar-thumb { background: var(--pt-surface-4); border-radius: 3px; }
        .pt-ranking-list::-webkit-scrollbar-thumb:hover { background: var(--pt-muted-foreground); }
        .pt-ranking-row { transition: background 0.15s ease; }
        .pt-ranking-row:hover { background: var(--pt-surface-3); }
      `}</style>

      {/* 遮罩层 */}
      <div
        className="pt-ranking-drawer-scrim"
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 40,
          background: "rgba(0, 0, 0, 0.6)",
          backdropFilter: "blur(2px)",
          WebkitBackdropFilter: "blur(2px)",
        }}
      />

      {/* 抽屉面板 */}
      <div
        className="pt-ranking-drawer-panel"
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100%",
          width: 480,
          zIndex: 50,
          display: "flex",
          flexDirection: "column",
          background: "var(--pt-surface-2)",
          borderLeft: "1px solid var(--pt-border)",
          boxShadow: "var(--pt-shadow-lg)",
        }}
      >
        {/* ========== 头部 ========== */}
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
            <h2 style={{ fontSize: 18, fontWeight: 600, color: "#ffffff", margin: 0 }}>
              {t("portfolioTrading.ranking.title")}
            </h2>
            <p style={{ fontSize: 12, color: "var(--pt-muted-foreground)", margin: 0 }}>
              {t("portfolioTrading.ranking.subtitle")}
            </p>
          </div>
          <button
            type="button"
            className="pt-btn pt-btn-ghost pt-btn-sm"
            onClick={onClose}
            aria-label={t("portfolioTrading.ranking.close")}
            style={{ width: 32, height: 32, padding: 0, borderRadius: "var(--pt-radius-md)" }}
          >
            <X size={16} />
          </button>
        </div>

        {/* ========== 3 子 Tab nav ========== */}
        <div
          style={{
            display: "flex",
            gap: 24,
            padding: "0 20px",
            borderBottom: "1px solid var(--pt-border)",
          }}
        >
          {TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              className={`pt-tab${activeMetric === tab.key ? " active" : ""}`}
              onClick={() => setActiveMetric(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* ========== 排名列表（可滚动） ========== */}
        <div className="pt-ranking-list" style={{ flex: 1, overflowY: "auto" }}>
          {ranked.length === 0 && (
            <div
              style={{
                padding: "48px 20px",
                textAlign: "center",
                color: "var(--pt-muted-foreground)",
                fontSize: 13,
              }}
            >
              {t("portfolioTrading.ranking.empty")}
            </div>
          )}
          {ranked.map((item, idx) => {
            const rank = idx + 1;
            const metric = formatMetric(item, activeMetric);
            return (
              <div
                key={item.id}
                className="pt-ranking-row"
                onClick={() => handleSelect(item.id)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "14px 20px",
                  borderBottom: "1px solid var(--pt-border)",
                  cursor: "pointer",
                }}
              >
                {/* 排名徽章 */}
                <div style={rankBadgeStyle(rank)}>{rank}</div>

                {/* 组合信息 */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                    <span
                      style={{
                        fontSize: 14,
                        fontWeight: 500,
                        color: "#ffffff",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {item.name}
                    </span>
                    <span className={`pt-tag ${item.isLive ? "pt-tag-success" : "pt-tag-warning"}`}>
                      {item.isLive
                        ? t("portfolioTrading.tag.live")
                        : t("portfolioTrading.tag.simulated")}
                    </span>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                      fontSize: 11,
                      color: "var(--pt-muted-foreground)",
                    }}
                  >
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      <Calendar size={12} />
                      {template("portfolioTrading.ranking.established", { days: item.establishedDays })}
                    </span>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      <Layers size={12} />
                      {template("portfolioTrading.ranking.symbols", { count: item.symbolCount })}
                    </span>
                  </div>
                </div>

                {/* 指标值 + 查看按钮 */}
                <div style={{ display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
                  <div style={{ textAlign: "right" }}>
                    <div
                      className="pt-mono"
                      style={{ fontSize: 16, fontWeight: 700, color: metric.color }}
                    >
                      {metric.value}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="pt-btn pt-btn-secondary pt-btn-sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleSelect(item.id);
                    }}
                  >
                    {t("portfolioTrading.ranking.view")}
                  </button>
                </div>
              </div>
            );
          })}
        </div>

        {/* ========== 底部操作栏 ========== */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "14px 20px",
            borderTop: "1px solid var(--pt-border)",
            background: "var(--pt-surface-2)",
          }}
        >
          <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)", display: "inline-flex", alignItems: "center", gap: 6 }}>
            {perfLoading && <Loader2 size={12} className="pt-animate-spin" />}
            {template("portfolioTrading.ranking.total", { count: ranked.length })}
            {perfLoading && "（指标加载中...）"}
          </span>
          <button type="button" className="pt-btn pt-btn-secondary" onClick={onClose}>
            <span>{t("portfolioTrading.ranking.viewAll")}</span>
            <ChevronRight size={14} />
          </button>
        </div>
      </div>
    </>
  );
};

export default PortfolioRankingDrawer;
