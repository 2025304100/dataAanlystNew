import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "antd";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Info,
  ListChecks,
  RefreshCw,
  ScrollText,
  ShieldCheck,
  SlidersHorizontal,
  XCircle,
} from "lucide-react";
import { api } from "../../api/client";
import { useApp } from "../../context/AppContext";
import { t, template } from "../../i18n";
import type { ActionItem, ActionItemsResponse } from "../../types";
import { resolveItemAction, safeImpact, safeTitle } from "../../utils/actionItemRegistry";
import { govStatusLabel } from "../../utils/govStatusLabel";
import { openSettingsSection } from "../../utils/navigate";
import ModelBindingReview from "./ModelBindingReview";

/*
 * 治理「待办总览」：默认落地页。
 * 与修复抽屉同源（同一个 action-items 接口 + 同一个动作注册表），
 * 只负责「在治理页内看到待办并一键跳到对应处理位置」，不在这里做写操作：
 * 对账 → 每日对账子页；调整状态 → 运行状态子页；跨页 → 设置对应分区。
 */

const SEVERITY_STYLE = {
  blocker: { color: "#f87171", Icon: XCircle },
  warning: { color: "#fbbf24", Icon: AlertTriangle },
  info: { color: "#60a5fa", Icon: Info },
} as const;

const STATUS_TONE: Record<string, { color: string }> = {
  READY: { color: "#34d399" },
  RUNNING_AUTO_SIMULATION: { color: "#3b82f6" },
  RUNNING_BACKTEST: { color: "#6366f1" },
  DATA_INCOMPLETE_PAUSED: { color: "#eab308" },
  RECONCILIATION_BLOCKED: { color: "#ef4444" },
  MODEL_INACTIVE: { color: "#6b7280" },
  SCORE_STALE: { color: "#f97316" },
  INTERRUPTED: { color: "#d946ef" },
  ADMIN_PAUSED: { color: "#dc2626" },
  PENDING_INITIAL_REVIEW: { color: "#f59e0b" },
};

interface Props {
  portfolioId: number;
  onNavigate: (tab: string) => void;
  onGovernanceChanged?: (portfolioId: number) => void | Promise<void>;
}

const GovernanceOverviewPanel: React.FC<Props> = ({ portfolioId, onNavigate, onGovernanceChanged }) => {
  const { setActiveTab } = useApp();
  const [data, setData] = useState<ActionItemsResponse | null>(null);
  const [currentState, setCurrentState] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bindingItemId, setBindingItemId] = useState<string | null>(null);
  const requestRef = useRef(0);
  const portfolioIdRef = useRef(portfolioId);
  portfolioIdRef.current = portfolioId;

  const load = useCallback(async () => {
    const requestId = ++requestRef.current;
    const isCurrent = () => requestId === requestRef.current && portfolioId === portfolioIdRef.current;
    if (!portfolioId) return;
    setLoading(true);
    setError(null);
    try {
      const [items, status] = await Promise.all([
        api.getPortfolioActionItems(portfolioId, { for_schedule: true }),
        api.getPortfolioStatus(portfolioId).catch(() => null),
      ]);
      if (isCurrent()) {
        setData(items);
        setCurrentState(status?.current_state ?? null);
      }
    } catch {
      if (isCurrent()) setError(t("repairDrawer.loadFailed"));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [portfolioId]);

  useEffect(() => {
    setData(null);
    setCurrentState(null);
    setBindingItemId(null);
    void load();
    return () => { requestRef.current += 1; };
  }, [load]);

  const handleBindingResolved = useCallback(async () => {
    if (portfolioId !== portfolioIdRef.current) return;
    setBindingItemId(null);
    await Promise.allSettled([
      load(),
      Promise.resolve().then(() => onGovernanceChanged?.(portfolioId)),
    ]);
  }, [load, onGovernanceChanged, portfolioId]);

  const groups = useMemo(() => {
    const items = data?.items ?? [];
    return (["blocker", "warning", "info"] as const).map((sev) => ({
      sev,
      list: items.filter((i) => i.severity === sev),
    }));
  }, [data]);

  const tone = (currentState && STATUS_TONE[currentState]?.color) || "#94a3b8";
  const blockers = data?.summary.blockers ?? 0;
  const ready = data?.summary.ready === true && !blockers;
  const waitingForSignal = data?.summary.waiting_for_signal === true && !blockers;

  // 行动项在治理页内的动作：只做导航，不做写操作。
  const handleItemAction = useCallback(
    (item: ActionItem) => {
      const action = resolveItemAction(item);
      if (action.kind === "subtab") {
        onNavigate(action.tab);
      } else if (action.kind === "guided") {
        onNavigate("governance:reconciliation");
      } else if (action.kind === "redirect") {
        const section = action.target.startsWith("settings:")
          ? action.target.slice("settings:".length)
          : action.target;
        openSettingsSection(section);
        setActiveTab("settings");
      } else if (action.kind === "open-modal" && action.modal === "model-binding-review") {
        setBindingItemId(item.id);
      } else if (action.kind === "toggle" || action.kind === "open-modal") {
        // 开关与添加候选弹窗由组合总览/顶栏承载，治理页不跨层操作。
        onNavigate("overview");
      }
    },
    [onNavigate, setActiveTab],
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* 当前状态 + 高频动作 */}
      <div className="pt-panel" style={{ padding: 18, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          {ready ? <CheckCircle2 size={30} color={tone} /> : <ShieldCheck size={30} color={tone} />}
          <div>
            <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.overview.currentState")}</div>
            <div style={{ fontSize: 20, fontWeight: 700, color: tone }}>
              {currentState ? govStatusLabel(currentState) : t("loading")}
            </div>
            <div style={{ fontSize: 12, color: blockers ? "#fca5a5" : "#86efac", marginTop: 2 }}>
              {waitingForSignal ? t("repairDrawer.header.waitingForSignal") : ready ? t("governance.overview.healthyHint") : template("governance.overview.pendingHint", { n: blockers })}
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Button icon={<ListChecks size={15} />} onClick={() => onNavigate("governance:reconciliation")}>
            {t("governance.overview.quickReconcile")}
          </Button>
          <Button type="primary" ghost icon={<SlidersHorizontal size={15} />} onClick={() => onNavigate("governance:state")}>
            {t("governance.overview.quickState")}
          </Button>
          <Button icon={<ScrollText size={15} />} onClick={() => onNavigate("governance:audit")}>
            {t("governance.overview.quickAudit")}
          </Button>
          <Button icon={<RefreshCw size={14} />} loading={loading} onClick={load}>
            {t("repairDrawer.refresh")}
          </Button>
        </div>
      </div>

      {error && (
        <div className="pt-panel" style={{ padding: 14, borderColor: "rgba(248,113,113,0.4)" }}>
          <span style={{ color: "#fca5a5" }}>{error}</span>
        </div>
      )}

      {!error && !loading && ready && (data?.items.length ?? 0) === 0 && (
        <div className="pt-panel" style={{ padding: 32, textAlign: "center" }}>
          <CheckCircle2 size={30} color="#34d399" />
          <div style={{ color: "#34d399", fontWeight: 600, marginTop: 8 }}>{t("repairDrawer.empty")}</div>
        </div>
      )}

      {groups.map(
        ({ sev, list }) =>
          list.length > 0 && (
            <div key={sev}>
              <div style={{ fontSize: 13, fontWeight: 600, color: SEVERITY_STYLE[sev].color, marginBottom: 8 }}>
                {t(`repairDrawer.${sev === "blocker" ? "blockersTitle" : sev === "warning" ? "warningsTitle" : "infoTitle"}`)}
                （{list.length}）
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(320px,1fr))", gap: 10 }}>
                {list.map((item) => {
                  const { Icon, color } = SEVERITY_STYLE[item.severity];
                  const action = resolveItemAction(item);
                  const actionable = action.kind !== "none";
                  return (
                    <div
                      key={item.id}
                      className="pt-panel"
                      style={{ padding: 14, borderLeft: `3px solid ${color}`, display: "flex", flexDirection: "column", gap: 8 }}
                    >
                      <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                        <Icon size={16} color={color} style={{ marginTop: 2, flexShrink: 0 }} />
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontWeight: 600, fontSize: 14, color: "#f1f5f9" }}>{safeTitle(item)}</div>
                          <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 3, lineHeight: 1.6 }}>{safeImpact(item)}</div>
                        </div>
                      </div>
                      {actionable && (
                        <div style={{ marginTop: "auto" }}>
                          <Button size="small" type={item.severity === "blocker" ? "primary" : "default"} onClick={() => handleItemAction(item)}>
                            {action.kind === "guided"
                              ? t("governance.overview.goReconcile")
                              : action.kind === "open-modal" && action.modal === "model-binding-review"
                              ? t("repairDrawer.binding.open")
                              : action.kind === "toggle" || action.kind === "open-modal"
                              ? t("governance.overview.goOverview")
                              : t("repairDrawer.goProcess")}
                            <ArrowRight size={13} style={{ marginLeft: 4 }} />
                          </Button>
                        </div>
                      )}
                      {bindingItemId === item.id && action.kind === "open-modal" && action.modal === "model-binding-review" && (
                        <ModelBindingReview key={portfolioId} portfolioId={portfolioId} onNavigate={onNavigate}
                          onResolved={handleBindingResolved} />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ),
      )}
    </div>
  );
};

export default GovernanceOverviewPanel;
