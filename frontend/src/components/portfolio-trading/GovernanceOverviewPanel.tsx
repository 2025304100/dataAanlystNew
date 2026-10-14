import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  Info,
  ListChecks,
  RefreshCw,
  ScrollText,
  ShieldCheck,
  SlidersHorizontal,
  XCircle,
} from "lucide-react";
import { Button } from "antd";
import dayjs from "dayjs";
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

type Severity = "blocker" | "warning" | "info";
type HeroTone = "danger" | "warning" | "ok" | "neutral";

const SEVERITY_META: Record<Severity, { color: string; Icon: typeof XCircle; cssTone: string; chipLabel: string }> = {
  blocker: { color: "#f87171", Icon: XCircle, cssTone: "danger", chipLabel: "阻断" },
  warning: { color: "#fbbf24", Icon: AlertTriangle, cssTone: "warning", chipLabel: "提醒" },
  info: { color: "#60a5fa", Icon: Info, cssTone: "info", chipLabel: "信息" },
};

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

  const blockers = data?.summary.blockers ?? 0;
  const warnings = data?.summary.warnings ?? 0;
  const infoCount = data?.summary.info ?? 0;
  const ready = data?.summary.ready === true && !blockers;
  const waitingForSignal = data?.summary.waiting_for_signal === true && !blockers;

  const heroTone: HeroTone = blockers > 0
    ? "danger"
    : warnings > 0
      ? "warning"
      : ready || waitingForSignal
        ? "ok"
        : "neutral";
  const HeroIcon = ready ? CheckCircle2 : ShieldCheck;
  const heroStateColor = (currentState && STATUS_TONE[currentState]?.color) || "#94a3b8";

  const generatedLabel = useMemo(() => {
    const g = data?.generated_at;
    if (!g) return null;
    const d = dayjs(g);
    return d.isValid() ? d.format("YYYY-MM-DD HH:mm") : null;
  }, [data]);

  const heroHint = waitingForSignal
    ? t("repairDrawer.header.waitingForSignal")
    : ready
      ? t("governance.overview.healthyHint")
      : template("governance.overview.pendingHint", { n: blockers });

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

  const actionLabel = (item: ActionItem): string => {
    const action = resolveItemAction(item);
    if (action.kind === "guided") return t("governance.overview.goReconcile");
    if (action.kind === "open-modal" && action.modal === "model-binding-review") return t("repairDrawer.binding.open");
    if (action.kind === "toggle" || action.kind === "open-modal") return t("governance.overview.goOverview");
    return t("repairDrawer.goProcess");
  };

  /* 首屏加载：骨架屏，避免旧版整片空白 + 跳动 */
  if (loading && !data && !error) {
    return (
      <div className="gov-overview" aria-busy="true">
        <div className="gov-skeleton-card" style={{ gap: 14 }}>
          <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
            <div className="gov-sk-line" style={{ width: 52, height: 52, borderRadius: 14 }} />
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
              <div className="gov-sk-line" style={{ width: 120, height: 12 }} />
              <div className="gov-sk-line" style={{ width: 260, height: 18 }} />
              <div className="gov-sk-line" style={{ width: 200, height: 11 }} />
            </div>
          </div>
        </div>
        <div className="gov-item-grid">
          {[0, 1, 2].map((i) => (
            <div key={i} className="gov-skeleton-card" style={{ minHeight: 132 }}>
              <div style={{ display: "flex", gap: 10 }}>
                <div className="gov-sk-line" style={{ width: 30, height: 30, borderRadius: 9 }} />
                <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
                  <div className="gov-sk-line" style={{ width: "70%", height: 13 }} />
                  <div className="gov-sk-line" style={{ width: "100%", height: 11 }} />
                  <div className="gov-sk-line" style={{ width: "55%", height: 11 }} />
                </div>
              </div>
              <div className="gov-sk-line" style={{ height: 32, borderRadius: 8, marginTop: "auto" }} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="gov-overview">
      {/* 当前状态横幅 + 高频动作 */}
      <div className="gov-hero" data-tone={heroTone} role="status">
        <div className="gov-hero-main">
          <span className="gov-hero-icon">
            <HeroIcon size={28} color={heroStateColor} />
          </span>
          <div style={{ minWidth: 0 }}>
            <div className="gov-hero-eyebrow">{t("governance.overview.currentState")}</div>
            <div className="gov-hero-title" style={heroTone === "neutral" ? { color: heroStateColor } : undefined}>
              {currentState ? govStatusLabel(currentState) : t("loading")}
            </div>
            <div className="gov-hero-sub">{heroHint}</div>
            <div className="gov-chips">
              <span className={`gov-chip${blockers === 0 ? " is-zero" : ""}`} data-tone="danger">
                {SEVERITY_META.blocker.chipLabel} <b>{blockers}</b>
              </span>
              <span className={`gov-chip${warnings === 0 ? " is-zero" : ""}`} data-tone="warning">
                {SEVERITY_META.warning.chipLabel} <b>{warnings}</b>
              </span>
              <span className={`gov-chip${infoCount === 0 ? " is-zero" : ""}`} data-tone="info">
                {SEVERITY_META.info.chipLabel} <b>{infoCount}</b>
              </span>
            </div>
          </div>
        </div>
        <div className="gov-hero-actions">
          <Button
            size="small"
            icon={<ListChecks size={14} />}
            onClick={() => onNavigate("governance:reconciliation")}
          >
            {t("governance.overview.quickReconcile")}
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<SlidersHorizontal size={14} />}
            onClick={() => onNavigate("governance:state")}
          >
            {t("governance.overview.quickState")}
          </Button>
          <Button
            size="small"
            icon={<ScrollText size={14} />}
            onClick={() => onNavigate("governance:audit")}
          >
            {t("governance.overview.quickAudit")}
          </Button>
          <Button
            type="text"
            size="small"
            icon={<RefreshCw size={14} className={loading ? "gov-icon-spin" : undefined} />}
            onClick={load}
            loading={loading}
          >
            {t("repairDrawer.refresh")}
          </Button>
        </div>
      </div>

      {error && (
        <div className="gov-error" role="alert">
          <span>{error}</span>
          <Button size="small" icon={<RefreshCw size={13} />} onClick={load}>
            {t("repairDrawer.refresh")}
          </Button>
        </div>
      )}

      {!error && ready && (data?.items.length ?? 0) === 0 && (
        <div className="gov-empty">
          <span className="gov-empty-icon">
            <CheckCircle2 size={30} />
          </span>
          <div className="gov-empty-title">{t("repairDrawer.empty")}</div>
          <div className="gov-empty-sub">{t("governance.overview.healthyHint")}</div>
        </div>
      )}

      {groups.map(
        ({ sev, list }) =>
          list.length > 0 && (
            <div key={sev}>
              <div className="gov-group-head" data-tone={SEVERITY_META[sev].cssTone}>
                <span className="gov-head-dot" />
                <span className="gov-group-title">
                  {t(`repairDrawer.${sev === "blocker" ? "blockersTitle" : sev === "warning" ? "warningsTitle" : "infoTitle"}`)}
                </span>
                <span className="gov-group-count">{list.length}</span>
              </div>
              <div className="gov-item-grid">
                {list.map((item) => {
                  const meta = SEVERITY_META[item.severity];
                  const { Icon } = meta;
                  const action = resolveItemAction(item);
                  const actionable = action.kind !== "none";
                  return (
                    <div key={item.id} className="gov-item" data-tone={meta.cssTone}>
                      <div className="gov-item-head">
                        <span className="gov-item-icon">
                          <Icon size={16} />
                        </span>
                        <div style={{ minWidth: 0 }}>
                          <div className="gov-item-title">{safeTitle(item)}</div>
                          <div className="gov-item-desc">{safeImpact(item)}</div>
                        </div>
                      </div>
                      {actionable && (
                        <Button
                          block
                          size="small"
                          type={item.severity === "blocker" ? "primary" : "default"}
                          className="gov-item-action"
                          data-tone={item.severity === "blocker" ? "danger" : "default"}
                          onClick={() => handleItemAction(item)}
                        >
                          {actionLabel(item)}
                          <ArrowRight size={13} />
                        </Button>
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

      {generatedLabel && (
        <div className="gov-foot">
          <Clock size={12} />
          <span>更新于 {generatedLabel}</span>
        </div>
      )}
    </div>
  );
};

export default GovernanceOverviewPanel;
