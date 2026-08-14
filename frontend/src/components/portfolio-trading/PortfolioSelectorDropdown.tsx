import React, { useMemo, useState, useCallback, useEffect } from "react";
import { Search, Plus, Check, Trash2 } from "lucide-react";
import { Modal } from "antd";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import type { Portfolio } from "../../types";

/**
 * PortfolioSelectorDropdown — 组合选择下拉面板
 *
 * 一比一还原原型图「组合选择下拉.html」：320px 下拉面板 = 搜索框 + 「我的组合 (N)」分组头
 * + 组合列表（选中项左侧 3px 青色竖条 + 浅青背景 + 收益率红绿 + 实盘/模拟标签 + check 图标）
 * + 底部「新建组合」按钮。
 *
 * 数据派生：AppContext.portfolios（account_type/created_at 取真值；returnPct/symbolCount 按 id
 * 取模分配稳定的 mock 值，与 PortfolioRankingDrawer 一致 —— 无专用 performance/标的数 API）。
 *
 * 仅渲染面板内容（不含触发按钮）。面板 position:absolute/top:100%/left:0，由父组件的 relative
 * 容器决定落点；open=false 返回 null。
 *
 * i18n key 前缀：portfolioTrading.selector.* 与 portfolioTrading.tag.*（Task 13 补全，缺失时
 * t()/template() 返回 key 字符串，不阻塞编译）。
 */

interface PortfolioSelectorDropdownProps {
  open: boolean;
  onClose: () => void;
  onSelect: (id: number) => void;
  onCreateNew: () => void;
  currentPortfolioId?: number;
}

interface PortfolioItem {
  id: number;
  name: string;
  assetScope: "stock" | "etf" | "mixed";
  isLive: boolean;
  isDefault: boolean;
  establishedDays: number;
  symbolCount: number;
  returnPct: number;
}

/* 真实组合无 performance/标的数时的 mock 池（按 id 取模分配，保证稳定；与 PortfolioRankingDrawer 一致） */
const MOCK_PERF_RETURNS = [35.2, 28.7, 22.1, 18.5, 12.3, 8.9, 3.2, -2.1];
const MOCK_SYMBOL_COUNTS = [8, 12, 6, 10, 15, 7, 9, 11];

/** 将真实 Portfolio 派生为下拉项（returnPct/symbolCount 用 mock 池稳定分配）。 */
function deriveItem(p: Portfolio): PortfolioItem {
  const isLive = p.account_type === "manual";
  // 成立天数：从 created_at 计算，解析失败回退 90 天
  let establishedDays = 90;
  try {
    const created = new Date(p.created_at);
    if (!isNaN(created.getTime())) {
      establishedDays = Math.max(1, Math.floor((Date.now() - created.getTime()) / 86400000));
    }
  } catch {
    /* ignore */
  }
  const seed = Math.abs(p.id) % MOCK_PERF_RETURNS.length;
  return {
    id: p.id,
    name: p.name,
    assetScope: p.asset_scope ?? "mixed",
    isLive,
    isDefault: Number(p.is_default) === 1,
    establishedDays,
    symbolCount: MOCK_SYMBOL_COUNTS[seed],
    returnPct: MOCK_PERF_RETURNS[seed],
  };
}

/** 收益率格式化：正绿负红，正值带 + 号。 */
function formatReturn(value: number): { text: string; color: string } {
  const sign = value > 0 ? "+" : "";
  return {
    text: `${sign}${value.toFixed(1)}%`,
    color: value >= 0 ? "var(--pt-state-success)" : "var(--pt-state-error)",
  };
}

const PortfolioSelectorDropdown: React.FC<PortfolioSelectorDropdownProps> = ({
  open,
  onClose,
  onSelect,
  onCreateNew,
  currentPortfolioId,
}) => {
  const { portfolios, deletePortfolio } = useApp();
  const [query, setQuery] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<PortfolioItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  // 真实指标覆盖：id → { returnPct?, symbolCount? }，异步加载后填充
  const [perfOverride, setPerfOverride] = useState<Record<number, { returnPct?: number; symbolCount?: number }>>({});

  // 派生组合项（从 AppContext.portfolios，returnPct/symbolCount 优先用真实覆盖值，回退 mock）
  const items = useMemo<PortfolioItem[]>(() => {
    return (portfolios ?? []).map((p) => {
      const base = deriveItem(p);
      const ov = perfOverride[p.id];
      if (ov) {
        return {
          ...base,
          returnPct: ov.returnPct ?? base.returnPct,
          symbolCount: ov.symbolCount ?? base.symbolCount,
        };
      }
      return base;
    });
  }, [portfolios, perfOverride]);

  // 异步加载真实 returnPct（getPortfolioPerformance）+ symbolCount（getPositions）
  // Promise.allSettled 容错，单组合失败保留 mock 兜底，不阻塞 UI
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const loadPerf = async () => {
      const list = portfolios ?? [];
      const results = await Promise.allSettled(
        list.map(async (p) => {
          const [perfRes, posRes] = await Promise.allSettled([
            api.getPortfolioPerformance(p.id, {}),
            api.getPositions(p.id),
          ]);
          const returnPct =
            perfRes.status === "fulfilled"
              ? (perfRes.value as { stats?: { total_return_pct?: number | null } })?.stats?.total_return_pct ?? null
              : null;
          const symbolCount =
            posRes.status === "fulfilled" ? (posRes.value as unknown[]).length : null;
          return { id: p.id, returnPct, symbolCount };
        }),
      );
      if (cancelled) return;
      const next: Record<number, { returnPct?: number; symbolCount?: number }> = {};
      results.forEach((r, i) => {
        if (r.status === "fulfilled") {
          const { id, returnPct, symbolCount } = r.value;
          next[id] = {
            ...(returnPct != null && Number.isFinite(Number(returnPct)) ? { returnPct: Number(returnPct) } : {}),
            ...(symbolCount != null ? { symbolCount } : {}),
          };
        }
      });
      setPerfOverride(next);
    };
    loadPerf();
    return () => {
      cancelled = true;
    };
  }, [open, portfolios]);

  // 搜索过滤：组合名 includes 匹配，不区分大小写
  const filtered = useMemo<PortfolioItem[]>(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => item.name.toLowerCase().includes(q));
  }, [items, query]);

  // 选择组合 → 关闭下拉 + 通知父组件切换；catch 块用 t() 兜底
  const handleSelect = useCallback(
    (id: number) => {
      onClose();
      try {
        onSelect(id);
      } catch (err) {
        console.warn(t("portfolioTrading.selector.switchFailed"), err);
      }
    },
    [onClose, onSelect],
  );

  // 新建组合 → 关闭下拉 + 通知父组件打开新建弹窗；catch 块用 t() 兜底
  const handleCreateNew = useCallback(() => {
    onClose();
    try {
      onCreateNew();
    } catch (err) {
      console.warn(t("portfolioTrading.selector.createFailed"), err);
    }
  }, [onClose, onCreateNew]);

  const handleDelete = useCallback(async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    const deleted = await deletePortfolio(deleteTarget.id);
    setDeleting(false);
    if (deleted) {
      setDeleteTarget(null);
      onClose();
    }
  }, [deleteTarget, deletePortfolio, deleting, onClose]);

  // ESC 关闭（无障碍）
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 打开时重置搜索框
  useEffect(() => {
    if (open) setQuery("");
  }, [open]);

  if (!open) return null;

  const totalCount = items.length;

  return (
    <>
      {/* 透明遮罩：点击外部关闭下拉（不可见，不影响原型视觉） */}
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, zIndex: 99 }}
        aria-hidden
      />

      {/* 下拉面板 — 320px，pt-dropdown 风格（背景 surface-2 / 边框 / 圆角 lg / 阴影 lg） */}
      <div
        role="listbox"
        aria-label={t("portfolioTrading.selector.groupTitle")}
        className="pt-dropdown"
        style={{
          top: "100%",
          left: 0,
          marginTop: 4,
          width: 320,
          minWidth: "auto",
          padding: 0,
          background: "var(--pt-surface-2)",
          overflow: "hidden",
          zIndex: 100,
        }}
      >
        {/* ========== 搜索框区域 ========== */}
        <div style={{ padding: 12, borderBottom: "1px solid var(--pt-border)" }}>
          <div style={{ position: "relative" }}>
            <Search
              size={16}
              style={{
                position: "absolute",
                left: 10,
                top: "50%",
                transform: "translateY(-50%)",
                color: "var(--pt-slate-500)",
                pointerEvents: "none",
              }}
            />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("portfolioTrading.selector.searchPlaceholder")}
              className="pt-input"
              style={{
                width: "100%",
                height: 36,
                paddingLeft: 32,
                paddingRight: 12,
                background: "var(--pt-surface-3)",
                border: "1px solid var(--pt-border)",
                borderRadius: "var(--pt-radius-md)",
                color: "var(--pt-foreground)",
                fontSize: 12,
                outline: "none",
                transition: "border-color 0.15s ease",
              }}
              onFocus={(e) => {
                e.currentTarget.style.borderColor = "var(--pt-primary)";
              }}
              onBlur={(e) => {
                e.currentTarget.style.borderColor = "var(--pt-border)";
              }}
            />
          </div>
        </div>

        {/* ========== 分组头：「我的组合」 + 组合数 ========== */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "10px 12px 8px",
          }}
        >
          <span
            style={{
              fontSize: 11,
              fontWeight: 500,
              color: "var(--pt-slate-400)",
              letterSpacing: "0.02em",
            }}
          >
            {t("portfolioTrading.selector.groupTitle")}
          </span>
          <span style={{ fontSize: 11, color: "var(--pt-slate-500)" }}>
            {totalCount}
          </span>
        </div>

        {/* ========== 组合列表（可滚动，thin-scrollbar） ========== */}
        <div
          className="thin-scrollbar"
          style={{ maxHeight: 420, overflowY: "auto" }}
        >
          {filtered.length === 0 && (
            <div
              style={{
                padding: "32px 12px",
                textAlign: "center",
                color: "var(--pt-slate-500)",
                fontSize: 12,
              }}
            >
              {t("portfolioTrading.selector.empty")}
            </div>
          )}

          {filtered.map((item) => {
            const selected = item.id === currentPortfolioId;
            const ret = formatReturn(item.returnPct);
            return (
              <div
                key={item.id}
                role="option"
                aria-selected={selected}
                onClick={() => handleSelect(item.id)}
                style={{
                  position: "relative",
                  display: "flex",
                  alignItems: "center",
                  padding: selected ? "10px 12px 10px 0" : "10px 12px 10px 15px",
                  cursor: "pointer",
                  background: selected ? "rgba(6, 182, 212, 0.08)" : "transparent",
                  transition: "background-color 0.15s ease",
                }}
                onMouseEnter={(e) => {
                  if (!selected) e.currentTarget.style.background = "var(--pt-surface-3)";
                }}
                onMouseLeave={(e) => {
                  if (!selected) e.currentTarget.style.background = "transparent";
                }}
              >
                {/* 选中项左侧 3px 青色竖条 */}
                {selected && (
                  <div
                    style={{
                      position: "absolute",
                      left: 0,
                      top: 8,
                      bottom: 8,
                      width: 3,
                      background: "var(--pt-primary)",
                      borderRadius: "0 2px 2px 0",
                    }}
                  />
                )}

                {/* 左侧信息区：组合名 + 成立天数/标的数 */}
                <div
                  style={{
                    flex: 1,
                    minWidth: 0,
                    marginLeft: selected ? 12 : 0,
                  }}
                >
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 500,
                      color: "var(--pt-foreground)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {item.name}
                    <span
                      title="组合标的范围"
                      style={{
                        marginLeft: 6,
                        fontSize: 10,
                        fontWeight: 500,
                        color: "var(--pt-primary)",
                      }}
                    >
                      {item.assetScope === "stock" ? "股票" : item.assetScope === "etf" ? "ETF" : "混合"}
                    </span>
                  </div>
                  <div
                    style={{
                      marginTop: 2,
                      fontSize: 11,
                      color: "var(--pt-slate-500)",
                    }}
                  >
                    {template("portfolioTrading.selector.established", { days: item.establishedDays })}
                    {" · "}
                    {template("portfolioTrading.selector.symbols", { count: item.symbolCount })}
                  </div>
                </div>

                {/* 收益率（正绿负红，pt-mono） */}
                <div
                  className="pt-mono"
                  style={{
                    marginRight: 10,
                    flexShrink: 0,
                    fontSize: 12,
                    fontWeight: 600,
                    color: ret.color,
                  }}
                >
                  {ret.text}
                </div>

                {/* 实盘/模拟标签 + 选中项 check 图标 */}
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    flexShrink: 0,
                  }}
                >
                  <button
                    type="button"
                    aria-label={`删除组合 ${item.name}`}
                    title={
                      item.isDefault
                        ? "默认组合不可删除，请先设置其他组合为默认"
                        : totalCount <= 1
                          ? "至少保留一个组合"
                          : `删除组合 ${item.name}`
                    }
                    disabled={item.isDefault || totalCount <= 1}
                    onClick={(event) => {
                      event.stopPropagation();
                      setDeleteTarget(item);
                    }}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      width: 24,
                      height: 24,
                      padding: 0,
                      border: 0,
                      borderRadius: "var(--pt-radius-sm)",
                      background: "transparent",
                      color: item.isDefault || totalCount <= 1 ? "var(--pt-slate-600)" : "var(--pt-slate-400)",
                      cursor: item.isDefault || totalCount <= 1 ? "not-allowed" : "pointer",
                    }}
                    onMouseEnter={(event) => {
                      if (!item.isDefault && totalCount > 1) {
                        event.currentTarget.style.color = "var(--pt-state-error)";
                        event.currentTarget.style.background = "rgba(239,68,68,0.1)";
                      }
                    }}
                    onMouseLeave={(event) => {
                      event.currentTarget.style.color = item.isDefault || totalCount <= 1 ? "var(--pt-slate-600)" : "var(--pt-slate-400)";
                      event.currentTarget.style.background = "transparent";
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                  <span
                    className={`pt-tag ${item.isLive ? "pt-tag-success" : "pt-tag-warning"}`}
                    style={{ fontSize: 10, padding: "2px 6px" }}
                  >
                    {item.isLive
                      ? t("portfolioTrading.tag.live")
                      : t("portfolioTrading.tag.simulated")}
                  </span>
                  {selected && (
                    <Check size={16} style={{ color: "var(--pt-primary)" }} />
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* ========== 底部新建组合按钮 ========== */}
        <div
          style={{
            borderTop: "1px solid var(--pt-border)",
            padding: 12,
          }}
        >
          <button
            type="button"
            onClick={handleCreateNew}
            className="pt-btn"
            style={{
              width: "100%",
              background: "transparent",
              border: "1px solid var(--pt-border)",
              color: "var(--pt-muted-foreground)",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "var(--pt-surface-3)";
              e.currentTarget.style.color = "var(--pt-foreground)";
              e.currentTarget.style.borderColor = "var(--pt-slate-500)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "transparent";
              e.currentTarget.style.color = "var(--pt-muted-foreground)";
              e.currentTarget.style.borderColor = "var(--pt-border)";
            }}
          >
            <Plus size={16} />
            <span>{t("portfolioTrading.selector.create")}</span>
          </button>
        </div>
      </div>

      <Modal
        open={deleteTarget != null}
        title="删除组合"
        okText="删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        confirmLoading={deleting}
        onCancel={() => {
          if (!deleting) setDeleteTarget(null);
        }}
        onOk={() => void handleDelete()}
      >
        <p>
          确定删除组合“{deleteTarget?.name ?? ""}”吗？
        </p>
        <p style={{ color: "var(--pt-state-error)", marginBottom: 0 }}>
          删除后将清除该组合的持仓、候选标的、规则、订单及回测记录，且不可恢复。
        </p>
      </Modal>
    </>
  );
};

export default PortfolioSelectorDropdown;
