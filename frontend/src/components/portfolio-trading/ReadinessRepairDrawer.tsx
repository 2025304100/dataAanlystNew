import {AcceptanceButton as Button,AcceptanceNotice} from './inspection/AcceptanceControls';
import React, { useEffect, useState } from "react";
import { Drawer } from "antd";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Info,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import type {
  ActionItem,
  ActionItemChecks,
  ActionItemsResponse,
} from "../../types";
import {
  isRawEnum,
  resolveItemAction,
  safeImpact,
  safeTitle,
} from "../../utils/actionItemRegistry";
import { openSettingsSection, requestPortfolioSubtab } from "../../utils/navigate";
import ModelBindingReview from "./ModelBindingReview";
import ReconciliationQuickCheckModal, { validReconciliationDate } from "./inspection/ReconciliationQuickCheckModal";

/* P0 修复抽屉：把三套状态的行动项聚合成「一句话问题 + 一个动作」。 */

const SEVERITY_META = {
  blocker: { color: "#f87171", Icon: XCircle, titleKey: "repairDrawer.blockersTitle" },
  warning: { color: "#fbbf24", Icon: AlertTriangle, titleKey: "repairDrawer.warningsTitle" },
  info: { color: "#60a5fa", Icon: Info, titleKey: "repairDrawer.infoTitle" },
} as const;

const CHECK_KEYS: Array<{ key: keyof ActionItemChecks; labelKey: string }> = [
  { key: "enabled", labelKey: "repairDrawer.light.enabled" },
  { key: "account_ready", labelKey: "repairDrawer.light.account" },
  { key: "data_ready", labelKey: "repairDrawer.light.data" },
  { key: "source_ready", labelKey: "repairDrawer.light.source" },
  { key: "schedule_ready", labelKey: "repairDrawer.light.schedule" },
];

function fmtTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

/* ---------------- 单条行动项卡片 ---------------- */

const ActionCard: React.FC<{
  item: ActionItem;
  portfolioId: number;
  toggling: boolean;
  canToggle: boolean;
  primary?: boolean;
  onToggleEnabled: () => void;
  onOpenAddCandidate: () => void;
  onResolved: () => void;
  onQuickCheck: (tradeDate?: string) => void;
}> = ({ item, portfolioId, toggling, canToggle, primary = false, onToggleEnabled, onOpenAddCandidate, onResolved, onQuickCheck }) => {
  const { setActiveTab } = useApp();
  const [reviewOpen, setReviewOpen] = useState(false);
  const action = resolveItemAction(item);
  const { Icon, color } = SEVERITY_META[item.severity];
  const itemDate = item.trade_date ?? item.scope?.trade_date;
  const invalidDate = itemDate != null && !validReconciliationDate(itemDate);

  const exec = () => {
    if (action.kind === "redirect") {
      const section = action.target.startsWith("settings:")
        ? action.target.slice("settings:".length)
        : action.target;
      openSettingsSection(section);
      setActiveTab("settings");
    } else if (action.kind === "subtab") {
      requestPortfolioSubtab(action.tab);
    } else if (action.kind === "open-modal") {
      if (action.modal === "model-binding-review") setReviewOpen(true);
      else onOpenAddCandidate();
    }
  };

  let actionBtn: React.ReactNode = null;
  if (action.kind === "toggle") {
    actionBtn = canToggle ? (
      <Button mutation size="small" type={primary ? "primary" : "default"} loading={toggling} onClick={onToggleEnabled} icon={<ChevronRight size={13} />}>
        {t("repairDrawer.enable")}
      </Button>
    ) : (
      // HG1 非健康态：不原地绕过门禁，引导到总览（那里有硬禁/软警告二次确认）
      <Button size="small" type={primary ? "primary" : "default"} onClick={() => requestPortfolioSubtab("overview")} icon={<ChevronRight size={13} />}>
        {t("repairDrawer.enableOnOverview")}
      </Button>
    );
  } else if (action.kind === "open-modal") {
    actionBtn = (
      <Button size="small" type={primary ? "primary" : "default"} onClick={exec}>
        {action.modal === "model-binding-review" ? t("repairDrawer.binding.open") : t("portfolioTrading.header.addCandidate")}
      </Button>
    );
  } else if (action.kind === "redirect" || action.kind === "subtab") {
    actionBtn = (
      <Button size="small" type={primary ? "primary" : "default"} onClick={exec} icon={<ChevronRight size={13} />}>
        {t("repairDrawer.goProcess")}
      </Button>
    );
  }

  return (
    <div
      style={{
        border: `1px solid ${color}33`,
        background: `${color}0d`,
        borderRadius: 8,
        padding: "10px 12px",
      }}
    >
      <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
        <Icon size={15} color={color} style={{ marginTop: 2, flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#f1f5f9" }}>{safeTitle(item)}</div>
          <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 2, lineHeight: 1.6 }}>
            {safeImpact(item)}
          </div>
          {actionBtn && <div style={{ marginTop: 8 }}>{actionBtn}</div>}
          {action.kind === "guided" && (
            <div style={{ marginTop: 10 }}><Button size="small" type={primary ? "primary" : "default"}
              disabled={invalidDate} title={invalidDate ? "交易日不明确，请刷新待办后再核对" : undefined}
              aria-label={`${safeTitle(item)} 快捷核对`}
              onClick={() => onQuickCheck(validReconciliationDate(itemDate) ? itemDate : undefined)}>
              {t("repairDrawer.quickCheck")}
            </Button></div>
          )}
          {reviewOpen && action.kind === "open-modal" && action.modal === "model-binding-review" && (
            <ModelBindingReview key={portfolioId} portfolioId={portfolioId} onResolved={onResolved} />
          )}
          <details style={{ marginTop: 8 }}>
            <summary style={{ fontSize: 11, color: "#64748b", cursor: "pointer", userSelect: "none" }}>
              {t("repairDrawer.techInfo")}
            </summary>
            <div style={{ fontSize: 11, color: "#64748b", marginTop: 4, lineHeight: 1.7, wordBreak: "break-all" }}>
              <div>{t("repairDrawer.techCode")}: {item.code}</div>
              <div>{t("repairDrawer.techSource")}: {item.sources.join(" / ")}</div>
              {item.detail && !isRawEnum(item.detail) ? <div>{item.detail}</div> : null}
            </div>
          </details>
        </div>
      </div>
    </div>
  );
};

/* ---------------- 抽屉主体 ---------------- */

export interface ReadinessRepairDrawerProps {
  open: boolean;
  onClose: () => void;
  portfolioId: number;
  data: ActionItemsResponse | null;
  loading: boolean;
  loadError?: string | null;
  isSimulated: boolean;
  toggling: boolean;
  canToggle: boolean;
  onRefresh: () => void;
  onToggleEnabled: () => void;
  onOpenAddCandidate: () => void;
}

const ReadinessRepairDrawer: React.FC<ReadinessRepairDrawerProps> = ({
  open,
  onClose,
  portfolioId,
  data,
  loading,
  loadError,
  isSimulated,
  toggling,
  canToggle,
  onRefresh,
  onToggleEnabled,
  onOpenAddCandidate,
}) => {
  const [quickCheck, setQuickCheck] = useState<{ tradeDate?: string } | null>(null);
  useEffect(() => { setQuickCheck(null); }, [portfolioId, open]);
  const openQuickCheck = (tradeDate?: string) => setQuickCheck({ tradeDate });
  const items = data?.items ?? [];
  const orderedItems = (["blocker", "warning", "info"] as const).flatMap((severity) => items.filter((item) => item.severity === severity));
  const blockers = orderedItems.filter((item) => item.severity === "blocker");
  // 保持接口原有顺序；有下单限制时，不能先让用户开自动交易开关。
  const firstItem = blockers.find((item) => {
    const kind = resolveItemAction(item).kind;
    return kind !== "none" && kind !== "toggle";
  }) ?? blockers[0] ?? orderedItems[0];
  const remainingItems = orderedItems.filter((item) => item !== firstItem);
  const firstItemTitle = firstItem && resolveItemAction(firstItem).kind === "none"
    ? t(SEVERITY_META[firstItem.severity].titleKey)
    : t("repairDrawer.nextStep");
  const groups = (["blocker", "warning", "info"] as const).map((sev) => ({
    sev,
    list: remainingItems.filter((i) => i.severity === sev),
  }));
  const checks = data?.checks ?? null;
  const ready = !!data?.summary.ready && data?.summary.blockers === 0;
  const waitingForSignal = !!data?.summary.waiting_for_signal && data?.summary.blockers === 0;
  const healthy = ready || waitingForSignal;
  const running = data?.summary.execution_state === "running" && data?.summary.blockers === 0;
  const lastCheck = fmtTime(data?.generated_at);

  return (
    <><Drawer
      title={t("repairDrawer.title")}
      placement="right"
      width={460}
      open={open}
      onClose={() => { if (!quickCheck) onClose(); }}
      keyboard={!quickCheck}
      maskClosable={!quickCheck}
      styles={{ body: { padding: 0 } }}
      destroyOnClose
    >
      <div className="pt-theme-scope" style={{ background: "#0b1120", minHeight: "100%", padding: 16, color: "#e2e8f0" }}>
        <AcceptanceNotice compact/>
        {/* 头部状态 */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 10,
            flexWrap: "wrap",
            marginBottom: 12,
          }}
        >
          <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            {healthy ? (
              <CheckCircle2 size={16} color="#34d399" />
            ) : (
              <XCircle size={16} color="#f87171" />
            )}
            <strong style={{ fontSize: 13, color: healthy ? "#34d399" : "#fca5a5" }}>
              {running ? t("repairDrawer.header.running") : waitingForSignal ? t("repairDrawer.header.waitingForSignal") : ready ? t("repairDrawer.header.ready") : t("repairDrawer.header.notReady")}
            </strong>
          </div>
          <Button size="small" onClick={onRefresh} loading={loading} icon={<RefreshCw size={13} />}>
            {t("repairDrawer.refresh")}
          </Button>
        </div>

        {/* 五维指示灯 */}
        {isSimulated && checks && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
            {CHECK_KEYS.map(({ key, labelKey }) => {
              const ok = checks[key];
              return (
                <span key={key} style={{ fontSize: 12, color: ok ? "#34d399" : "#f87171", whiteSpace: "nowrap" }}>
                  {t(labelKey as "repairDrawer.light.enabled")}{" "}
                  <strong>{ok ? t("repairDrawer.checkPassed") : t("repairDrawer.checkPending")}</strong>
                </span>
              );
            })}
          </div>
        )}
        {!isSimulated && (
          <div style={{ fontSize: 12, color: "#94a3b8", marginBottom: 12 }}>{t("repairDrawer.notSimulated")}</div>
        )}

        {loading && items.length === 0 && (
          <div style={{ textAlign: "center", padding: "40px 0", color: "#94a3b8" }}>{t("loading")}</div>
        )}

        {!loading && loadError && (
          <div style={{ border: "1px solid rgba(248,113,113,0.3)", borderRadius: 8, padding: 12, background: "rgba(248,113,113,0.08)" }}>
            <div style={{ color: "#fca5a5", fontSize: 13, marginBottom: 8 }}>{loadError}</div>
            <Button size="small" onClick={onRefresh}>{t("repairDrawer.retry")}</Button>
          </div>
        )}

        {!loading && !loadError && items.length === 0 && healthy && (
          <div style={{ textAlign: "center", padding: "40px 12px" }}>
            <CheckCircle2 size={32} color="#34d399" />
            <div style={{ color: "#34d399", fontSize: 14, fontWeight: 600, marginTop: 10 }}>
              {t("repairDrawer.empty")}
            </div>
          </div>
        )}

        {!loadError && firstItem && <section aria-label={firstItemTitle} style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>{firstItemTitle}</div>
          <ActionCard key={`${portfolioId}:${firstItem.id}`} item={firstItem} portfolioId={portfolioId} toggling={toggling} canToggle={canToggle}
            primary onToggleEnabled={onToggleEnabled} onOpenAddCandidate={onOpenAddCandidate} onResolved={onRefresh} onQuickCheck={openQuickCheck} />
        </section>}

        {!loadError && remainingItems.length > 0 && <details>
          <summary style={{ fontSize: 12, cursor: "pointer", marginBottom: 12 }}>{template("repairDrawer.otherItems", { count: remainingItems.length })}</summary>
        {groups.map(
          ({ sev, list }) =>
            list.length > 0 && (
              <div key={sev} style={{ marginBottom: 14 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: SEVERITY_META[sev].color, marginBottom: 6 }}>
                  {t(SEVERITY_META[sev].titleKey as "repairDrawer.blockersTitle")}（{list.length}）
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {list.map((item) => (
                    <ActionCard
                      key={`${portfolioId}:${item.id}`}
                      item={item}
                      portfolioId={portfolioId}
                      toggling={toggling}
                      canToggle={canToggle}
                      onToggleEnabled={onToggleEnabled}
                      onOpenAddCandidate={onOpenAddCandidate}
                      onResolved={onRefresh}
                      onQuickCheck={openQuickCheck}
                    />
                  ))}
                </div>
              </div>
            ),
        )}
        </details>}

        {lastCheck && (
          <div style={{ fontSize: 11, color: "#475569", marginTop: 16 }}>
            {t("repairDrawer.lastCheck")} {lastCheck}
          </div>
        )}
      </div>
    </Drawer>{open && quickCheck && <ReconciliationQuickCheckModal key={`${portfolioId}:${quickCheck.tradeDate ?? "pending"}`}
      portfolioId={portfolioId} tradeDate={quickCheck.tradeDate} onClose={() => setQuickCheck(null)} onChanged={onRefresh}/>}</>
  );
};

export default ReadinessRepairDrawer;
