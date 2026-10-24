import {AcceptanceButton as Button,AcceptanceModal as Modal} from './inspection/AcceptanceControls';
import PortfolioInspectionWorkbench from "./inspection/PortfolioInspectionWorkbench";
import InspectionDrawer from "./inspection/InspectionDrawer";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRightLeft, CheckCircle2, Compass, HelpCircle, History, ListChecks, Lock, PlayCircle, RefreshCw, ShieldCheck, X } from "lucide-react";
import { Checkbox, DatePicker, Input, Select, Tag, Tooltip } from "antd";
import dayjs from "dayjs";
import { useApp } from "../../context/AppContext";
import { api, ApiError } from "../../api/client";
import { t, template } from "../../i18n";
import type {
  AuditEvent,
  AuditEventFilter,
  AuditEventPageResponse,
  AuditEventSeverity,
  ConfirmReconciliationRequest,
  G5AttributionCategory,
  G5BusinessAction,
  G5DualRunSummaryResponse,
  G7OperationalStatus,
  PortfolioStatePermissions,
  PortfolioStatus,
  PortfolioStatusResponse,
  ReconciliationDiffItem,
  ReconciliationResponse,
  StateTransitionRequest,
} from "../../types";
import {
  classifyReconVerdict,
  reconDimCodeForTooltip,
  reconDimLabel,
  reconDimWhy,
  type ReconVerdict,
} from "../../utils/reconDimLabel";
import GovernanceOverviewPanel from "./GovernanceOverviewPanel";
import { resolvePortfolioNavigation, type GovernanceTabTarget } from "../../utils/actionItemRegistry";

/* ========================================================================== */
/* PortfolioGovernanceTab                                                     */
/*                                                                            */
/* 组合工作台第 5 个主 Tab「治理」(接 overview/members/strategy/backtest)。   */
/* 内含 4 个子 Tab:                                                           */
/*   state          → 9 状态彩色徽章 + ADMIN_PAUSED 红色刹车条 + 9×4 允许矩阵  */
/*                    + allowed_transitions 驱动状态转移 (单人确认)            */
/*   reconciliation → 结论卡(四态分诊) + 差异明细(可读名称) + 折叠的人工解锁区 */
/*   audit          → 审计事件 5 过滤 + 分页 + correlation_id + 展开 attributes */
/*   g5             → G5 双跑启动(连续10交易日校验) + 6×6 混淆矩阵 +            */
/*                    eligible_g6 红绿徽章 + 异步任务行终态锁 disabled          */
/*                                                                            */
/* 契约：FE-2 的 7 个 API client 函数 + FE-3 的 81 条中英 i18n。              */
/* ========================================================================== */

// P1：默认落地「待办总览」；operations 为旧键（已并入 state），保留用于深链兼容。
type GovSubTabKey = "overview" | "state" | "operations" | "reconciliation" | "audit" | "g5";

const GOV_SUB_TAB_ORDER: GovSubTabKey[] = ["overview", "reconciliation", "state", "g5", "audit"];

/** 旧深链键归一：operations（运行保障）已并入 state（运行状态）。 */
function normalizeGovSubTab(raw: string | null | undefined): GovSubTabKey {
  if (raw === "operations") return "state";
  if (raw && (GOV_SUB_TAB_ORDER as string[]).includes(raw)) return raw as GovSubTabKey;
  return "overview";
}

const G5_BUSINESS_ACTIONS: G5BusinessAction[] = [
  "BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED",
];
const G5_ACTION_LABELS: Record<G5BusinessAction, string> = {
  BUY: "买入", SELL: "卖出", HOLD: "持有", NO_ACTION: "无操作", REJECTED: "拒绝", DATA_BLOCKED: "资料不足",
};

const G5_ATTR_CATEGORIES: G5AttributionCategory[] = [
  "PARAMETER_CONFIG_DIFF",
  "RISK_CONFIG_DIFF",
  "SCORING_VERSION_MISMATCH",
  "INPUT_DATA_ROUNDING",
  "TRADE_TIMING_MISMATCH",
  "ENGINE_FALLBACK_CODEPATH",
  "EXECUTION_SIM_MODEL_DIFF",
  "UNKNOWN_BUT_EXPLAINABLE",
  "UNKNOWN_ENGINE_DIFF",
  "DUPLICATE_REPLAY_SIDE_EFFECT",
  "CORRUPTED_SNAPSHOT_OR_EVIDENCE",
];

/** 9 状态色板 (文字色 + 背景 + 边框) */
/* 深色工作台下的状态色板：半透明底 + 同色系亮前景（与暗色 ConfigProvider 对齐） */
const STATUS_STYLES: Record<PortfolioStatus, { bg: string; border: string; fg: string; dot: string }> = {
  PENDING_INITIAL_REVIEW: { bg: "rgba(245,158,11,0.15)", border: "rgba(245,158,11,0.45)", fg: "#fcd34d", dot: "#f59e0b" },
  READY:                    { bg: "rgba(16,185,129,0.15)", border: "rgba(16,185,129,0.45)", fg: "#86efac", dot: "#10b981" },
  RUNNING_AUTO_SIMULATION:  { bg: "rgba(59,130,246,0.16)", border: "rgba(59,130,246,0.45)", fg: "#93c5fd", dot: "#3b82f6" },
  RUNNING_BACKTEST:         { bg: "rgba(99,102,241,0.18)", border: "rgba(99,102,241,0.45)", fg: "#a5b4fc", dot: "#6366f1" },
  DATA_INCOMPLETE_PAUSED:   { bg: "rgba(234,179,8,0.16)", border: "rgba(234,179,8,0.45)", fg: "#fde047", dot: "#eab308" },
  RECONCILIATION_BLOCKED:   { bg: "rgba(239,68,68,0.16)", border: "rgba(239,68,68,0.5)", fg: "#fca5a5", dot: "#ef4444" },
  MODEL_INACTIVE:           { bg: "rgba(107,114,128,0.22)", border: "rgba(107,114,128,0.5)", fg: "#cbd5e1", dot: "#6b7280" },
  SCORE_STALE:              { bg: "rgba(249,115,22,0.16)", border: "rgba(249,115,22,0.45)", fg: "#fdba74", dot: "#f97316" },
  INTERRUPTED:              { bg: "rgba(217,78,239,0.17)", border: "rgba(217,78,239,0.45)", fg: "#f0abfc", dot: "#d946ef" },
  ADMIN_PAUSED:             { bg: "rgba(220,38,38,0.18)", border: "rgba(220,38,38,0.55)", fg: "#fca5a5", dot: "#dc2626" },
};

/** 9×4 允许矩阵静态 (与后端 allowance 矩阵 + 文档 FR-P0-10 严格对齐) */
const ALLOWANCE_MATRIX: Record<PortfolioStatus, PortfolioStatePermissions> = {
  PENDING_INITIAL_REVIEW: {
    allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true,
  },
  READY: {
    allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false,
  },
  RUNNING_AUTO_SIMULATION: {
    allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false,
  },
  RUNNING_BACKTEST: {
    allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false,
  },
  DATA_INCOMPLETE_PAUSED: {
    allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false,
  },
  RECONCILIATION_BLOCKED: {
    allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: true,
  },
  MODEL_INACTIVE: {
    allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false,
  },
  SCORE_STALE: {
    allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false,
  },
  INTERRUPTED: {
    allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false,
  },
  ADMIN_PAUSED: {
    allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: true,
  },
};

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

const pct = (v: number | null | undefined): string => {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(2)}%`;
};

const fmtNum = (v: number | string | null | undefined, digits = 2): string => {
  if (v == null || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

/* -------------------------------------------------------------------------- */
/* 9 状态徽章                                                                  */
/* -------------------------------------------------------------------------- */

const StatusBadge: React.FC<{ status: PortfolioStatus; showDesc?: boolean; size?: "sm" | "md" }> = ({
  status,
  showDesc = true,
  size = "md",
}) => {
  const s = STATUS_STYLES[status];
  const label = t(`portfolioStatus.${status}` as const);
  const desc = t(`portfolioStatus.${status}.desc` as const);
  return (
    <Tooltip title={showDesc ? desc : ""} placement="top">
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          padding: size === "sm" ? "3px 10px" : "6px 12px",
          borderRadius: 999,
          border: `1px solid ${s.border}`,
          background: s.bg,
          color: s.fg,
          fontSize: size === "sm" ? 12 : 13,
          fontWeight: 600,
          lineHeight: 1.4,
        }}
      >
        <span
          aria-hidden
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: s.dot,
            display: "inline-block",
          }}
        />
        {label}
      </span>
    </Tooltip>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab Nav （沿用 FactorCenter 的 sub-tabs / sub-tab 类名，继承主题样式）   */
/* -------------------------------------------------------------------------- */

const GOV_TAB_VIEW: Record<GovSubTabKey, { label: string; icon: React.ReactNode; hint: string }> = {
  overview: { label: t("portfolioTrading.governance.subtab.overview"), icon: <ListChecks size={14} />, hint: t("governance.tabHint.overview") },
  reconciliation: { label: t("portfolioTrading.governance.subtab.reconciliation"), icon: <CheckCircle2 size={14} />, hint: t("governance.tabHint.reconciliation") },
  state: { label: t("portfolioTrading.governance.subtab.state"), icon: <ShieldCheck size={14} />, hint: t("governance.tabHint.state") },
  g5: { label: t("portfolioTrading.governance.subtab.g5DualRun"), icon: <PlayCircle size={14} />, hint: t("governance.tabHint.g5") },
  audit: { label: t("portfolioTrading.governance.subtab.audit"), icon: <History size={14} />, hint: t("governance.tabHint.audit") },
  operations: { label: t("portfolioTrading.governance.subtab.state"), icon: <ShieldCheck size={14} />, hint: t("governance.tabHint.state") },
};

const SubTabNav: React.FC<{ active: GovSubTabKey; onChange: (k: GovSubTabKey) => void }> = ({ active, onChange }) => {
  // P1：按用户任务频率排列 —— 待办总览（默认）→ 每日对账 → 运行状态 → 双跑验收 → 审计记录。
  const tabs: GovSubTabKey[] = GOV_SUB_TAB_ORDER;
  return (
    <div
      className="sub-tabs gov-sub-tabs"
      style={{
        padding: 0,
        marginBottom: 16,
        background: "transparent",
        border: "none",
        gap: 4,
        borderRadius: 10,
      }}
    >
      {tabs.map((tabKey) => {
        const view = GOV_TAB_VIEW[tabKey];
        const isActive = active === tabKey || (tabKey === "state" && active === "operations");
        return (
        <Tooltip
          key={tabKey}
          title={view.hint}
          placement="bottom"
        >
          <button
            type="button"
            className={`sub-tab ${isActive ? "active" : ""}`}
            onClick={() => onChange(tabKey)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "8px 14px",
              minHeight: 36,
              borderRadius: 8,
              border: "1px solid transparent",
              background:
                isActive
                  ? "linear-gradient(180deg, rgba(6,182,212,0.18), rgba(6,182,212,0.08))"
                  : "rgba(31,41,55,0.55)",
              borderColor:
                isActive
                  ? "rgba(6,182,212,0.55)"
                  : "rgba(71,85,105,0.55)",
              color:
                isActive
                  ? "var(--pt-primary)"
                  : "var(--pt-muted-foreground)",
              fontWeight: isActive ? 600 : 500,
              fontSize: 13,
              boxShadow:
                isActive
                  ? "0 0 0 1px rgba(6,182,212,0.25) inset, 0 2px 8px rgba(6,182,212,0.12)"
                  : "none",
            }}
          >
            {view.icon}
            {view.label}
          </button>
        </Tooltip>
        );
      })}
    </div>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab 1：系统状态（Status / Matrix / Transition）                        */
/* -------------------------------------------------------------------------- */

const StatePanel: React.FC<{
  portfolioId: number;
  showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void;
  onGovernanceChanged?: (portfolioId: number) => void | Promise<void>;
}> = ({
  portfolioId,
  showToast,
  onGovernanceChanged,
}) => {
  const [loading, setLoading] = useState(false);
  const [statusResp, setStatusResp] = useState<PortfolioStatusResponse | null>(null);
  const [transitionOpen, setTransitionOpen] = useState(false);
  const [targetState, setTargetState] = useState<PortfolioStatus | null>(null);
  const [transitionReason, setTransitionReason] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // P1：权限矩阵默认只看当前状态行，需要时再展开全部 9 行。
  const [showFullMatrix, setShowFullMatrix] = useState(false);

  const fetchStatus = useCallback(async () => {
    if (!portfolioId) return;
    setLoading(true);
    try {
      setStatusResp(await api.getPortfolioStatus(portfolioId));
    } catch (err: any) {
      showToast("error", (err?.message || String(err)) + " (getPortfolioStatus)");
    } finally {
      setLoading(false);
    }
  }, [portfolioId, showToast]);

  useEffect(() => { void fetchStatus(); }, [fetchStatus]);

  const currentRaw = statusResp?.current_state;
  const current: PortfolioStatus | null = useMemo(() => {
    if (!currentRaw) return null;
    return currentRaw as PortfolioStatus;
  }, [currentRaw]);
  const allowedTargets: PortfolioStatus[] = useMemo(
    () => (statusResp?.allowed_transitions ?? []) as PortfolioStatus[],
    [statusResp],
  );

  const handleOpenTransition = () => {
    if (!current) return;
    setTargetState(allowedTargets[0] ?? null);
    setTransitionReason("");
    setReviewNote("");
    setTransitionOpen(true);
  };

  const noteRequired = current === "ADMIN_PAUSED" || current === "RECONCILIATION_BLOCKED" ||
    targetState === "READY";

  const noteTooShort = noteRequired && reviewNote.trim().length < 10;

  const doTransition = async () => {
    if (!targetState || !current) return;
    if (targetState === current) {
      showToast("info", t("governance.transition.noop"));
      return;
    }
    if (noteTooShort) {
      showToast("error", t("governance.transition.noteMin10"));
      return;
    }
    const payload: StateTransitionRequest = {
      target_state: targetState,
      trigger_reason: transitionReason.trim() || "operator request",
      review_note: reviewNote.trim() || undefined,
      noop_if_already: true,
    };
    setSubmitting(true);
    try {
      const resp = await api.transitionPortfolioState(portfolioId, payload);
      if (resp.illegal_transition_rejected === true) {
        showToast("error", t("governance.transition.forbidden"));
        return;
      } else if (resp.noop_detected === true) {
        showToast("info", t("governance.transition.noop"));
      } else {
        const resultLabel = resp.transition_applied ? "APPLIED" : "NOOP";
        showToast("success", t("governance.transition.applied") + ` (${resultLabel}, audit=${resp.audit_event_id ?? "—"})`);
      }
      setTransitionOpen(false);
      await Promise.allSettled([
        fetchStatus(),
        Promise.resolve().then(() => onGovernanceChanged?.(portfolioId)),
      ]);
    } catch (err: any) {
      showToast("error", (err?.message || String(err)) + " (transition-state)");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* ADMIN_PAUSED 红色刹车条 */}
      {current === "ADMIN_PAUSED" && (
        <div
          role="alert"
          style={{
            padding: "12px 16px",
            borderRadius: 10,
            background: "linear-gradient(90deg, rgba(220,38,38,0.22), rgba(239,68,68,0.08))",
            border: "1px solid rgba(248,113,113,0.5)",
            color: "#fca5a5",
            fontWeight: 600,
            display: "flex",
            alignItems: "flex-start",
            gap: 10,
          }}
        >
          <AlertTriangle size={18} style={{ marginTop: 2, flexShrink: 0 }} />
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <div>{t("governance.adminPausedBrake")}</div>
            <div style={{ fontSize: 12, fontWeight: 500, opacity: 0.85 }}>{t("governance.adminPausedFooter")}</div>
          </div>
        </div>
      )}

      {/* 状态卡 */}
      <div className="pt-panel" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>{t("governance.transition.current")}:</span>
            {loading && !current ? <span style={{ opacity: 0.7 }}>…</span> : current && <StatusBadge status={current} />}
            {statusResp?.last_decision_trade_date && (
              <Tooltip
                title={
                  <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 300 }}>
                    <b>最近决策交易日</b>：自动接管今天（或最近一次 Dry Run / 真实执行）生成下单决策所基于的交易日。<br />
                    <span style={{ color: "#94a3b8" }}>对应英文裸字段：last_decision_trade_date</span>
                  </div>
                }
              >
                <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)", display: "inline-flex", alignItems: "center", gap: 4, cursor: "help" }}>
                  <span>📅 最近决策交易日</span>
                  <HelpCircle size={11} style={{ opacity: 0.7 }} />
                  <b>:</b> <b style={{ color: "var(--pt-foreground)" }}>{statusResp.last_decision_trade_date}</b>
                </span>
              </Tooltip>
            )}
            {statusResp?.last_reconciled_trade_date && (
              <Tooltip
                title={
                  <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 320 }}>
                    <b>最近对账交易日</b>：G5/G6 对账最后一次零和检查通过的交易日。<br />
                    如果这个日期比今天早 2 个交易日以上，可能会被自动打到「对账差异阻断」状态。<br />
                    👉 对账明细去「对账守恒」子 Tab 查。<br />
                    <span style={{ color: "#94a3b8" }}>对应英文裸字段：last_reconciled_trade_date</span>
                  </div>
                }
              >
                <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)", display: "inline-flex", alignItems: "center", gap: 4, cursor: "help" }}>
                  <span>🧾 最近对账交易日</span>
                  <HelpCircle size={11} style={{ opacity: 0.7 }} />
                  <b>:</b> <b style={{ color: "var(--pt-foreground)" }}>{statusResp.last_reconciled_trade_date}</b>
                </span>
              </Tooltip>
            )}
            {typeof statusResp?.is_auto_simulation_eligible === "boolean" && (
              <Tooltip
                title={
                  <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 330 }}>
                    <b>模拟自动接管资格</b>：true = 你可以放心打开「自动接管」右侧开关了（readiness 门禁基本过了）；<br />
                    false = 还差几项前置条件（比如评分还没同步、对账有差异等）。<br />
                    👉 缺啥去「运行保障」子 Tab 看 operational_blockers 清单。<br />
                    <span style={{ color: "#94a3b8" }}>对应英文裸字段：is_auto_simulation_eligible</span>
                  </div>
                }
              >
                <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)", display: "inline-flex", alignItems: "center", gap: 4, cursor: "help" }}>
                  <span>🤖 模拟自动接管资格</span>
                  <HelpCircle size={11} style={{ opacity: 0.7 }} />
                  <b>:</b>{" "}
                  <b style={{ color: statusResp.is_auto_simulation_eligible ? "#10b981" : "#ef4444" }}>
                    {statusResp.is_auto_simulation_eligible ? "✅ 已具备" : "❌ 不具备"}
                  </b>
                </span>
              </Tooltip>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Tooltip
              title={
                <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 260 }}>
                    {t("governance.refreshHint")}
                </div>
              }
              placement="top"
            >
              <Button icon={<RefreshCw size={14} />} onClick={fetchStatus} loading={loading}>
                {t("common.refresh")}
              </Button>
            </Tooltip>
            <Tooltip
              title={
                allowedTargets.length === 0 ? (
                  t("governance.transition.noTargets")
                ) : (
                  <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 360 }}>
                    {t("governance.transition.help")}
                  </div>
                )
              }
              placement="top"
            >
              <Button mutation
                type="primary"
                icon={<ArrowRightLeft size={15} />}
                onClick={handleOpenTransition}
                disabled={allowedTargets.length === 0 || !current}
              >
                {t("governance.transition.applyBtn")}
              </Button>
            </Tooltip>
          </div>
        </div>

        {/* allowed_transitions 清单 */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)", display: "inline-flex", alignItems: "center", gap: 6 }}>
            {t("governance.transition.allowedTargets")}:
            <Tooltip
              title={
                <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 360 }}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>
                    ⚠️ 这几个彩色胶囊 <b style={{ color: "#dc2626" }}>只是展示，不是按钮</b>，点了没用！
                  </div>
                  <div>这是后端状态机根据「当前状态」算出来的：<b>允许你转到哪些状态</b>（白名单）。</div>
                  <div style={{ marginTop: 4 }}>
                    👉 真要切状态：<b>去右上角点「🚦 调整运行状态」</b> → 弹窗里就能在这几个白名单里选目标。
                  </div>
                  <div style={{ color: "#94a3b8", marginTop: 6 }}>
                    （如果这里显示 ∅ = 当前状态没有任何允许目标，通常是「对账差异阻断」必须先去对账守恒Tab点「差异确认」，或者「初始审查中」需要先配策略并保存。）
                  </div>
                </div>
              }
            >
              <HelpCircle size={12} style={{ color: "var(--pt-state-warning)", cursor: "help" }} />
            </Tooltip>
          </span>
          {allowedTargets.length === 0 && (
            <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
              ∅ <span style={{ color: "#94a3b8", marginLeft: 4 }}>（没有允许转移的目标，hover ⚠️ 问号看原因）</span>
            </span>
          )}
          {allowedTargets.map((ts) => (
            <Tooltip
              key={ts}
              title={
                <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 320 }}>
                  <div style={{ fontWeight: 600, marginBottom: 2 }}>{t(`portfolioStatus.${ts}` as const)}</div>
                  <div>{t(`portfolioStatus.${ts}.desc` as const)}</div>
                  <div style={{ color: "#3b82f6", marginTop: 4 }}>👉 想去这个状态 → 点右上角「🚦 调整运行状态」</div>
                </div>
              }
            >
              <StatusBadge status={ts} size="sm" showDesc={false} />
            </Tooltip>
          ))}
        </div>
      </div>

      {/* 9×4 允许矩阵表 */}
      <div className="pt-panel" style={{ padding: 16 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10, flexWrap: "wrap", gap: 8 }}>
          <h4 style={{ margin: 0, fontSize: 14, display: "inline-flex", alignItems: "center", gap: 6 }}>
            {t("governance.matrix.title")}
            <Tooltip
              title={
                <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 420 }}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>📖 怎么读这张表？</div>
                  <div>• <b>一行 = 一种状态</b>：<span style={{ background: "rgba(16,185,129,0.12)" }}>绿色高亮</span>的那一行就是「当前状态」。</div>
                  <div>• <b>向右看 4 列</b>：=「当前状态下，这 4 种业务动作是否允许」</div>
                  <div style={{ marginTop: 4, paddingTop: 4, borderTop: "1px dashed #334155" }}>
                    <b>图例：</b><br />
                    <span style={{ color: "#34d399", fontWeight: 600 }}>✓ 允许</span> —— 该列业务动作正常放行<br />
                    <span style={{ color: "#f87171" }}>✗ {t("governance.matrix.allowFalse")}</span> —— 交易门禁直接阻断（自动接管 / 手动下单都会被拒）<br />
                    <span style={{ padding: "1px 6px", borderRadius: 999, border: "1px solid #f59e0b", background: "rgba(245,158,11,0.16)", color: "#fcd34d", fontSize: 11 }}>{t("governance.matrix.needsAck")}</span> —— {t("governance.matrix.manualAckHint")}
                  </div>
                </div>
              }
            >
              <HelpCircle size={13} style={{ cursor: "help", color: "var(--pt-muted-foreground)" }} />
            </Tooltip>
          </h4>
          <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 11, color: "var(--pt-muted-foreground)" }}>
            <span style={{ color: "#34d399" }}>✓ 允许</span>
            <span style={{ color: "#f87171" }}>✗ 禁止</span>
            <span style={{ padding: "1px 6px", borderRadius: 999, border: "1px solid #f59e0b", background: "rgba(245,158,11,0.16)", color: "#fcd34d" }}>{t("governance.matrix.needsAck")}</span>
            <Button size="small" type="text" onClick={() => setShowFullMatrix((v) => !v)}>
              {showFullMatrix ? t("governance.matrix.collapse") : t("governance.matrix.expand")}
            </Button>
          </div>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 780 }}>
            <thead>
              <tr style={{ background: "var(--pt-surface-1)" }}>
                <th style={thStyle("left")}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    状态名
                    <Tooltip
                      title={
                        <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 260 }}>
                          <b>PortfolioStatus（状态机枚举）</b>：9 种治理状态之一。<br />
                          hover 每一行的<b>彩色徽章</b>可看"这状态触发条件 + 如何恢复"。<br />
                          <span style={{ color: "#94a3b8" }}>原英文列名：PortfolioStatus</span>
                        </div>
                      }
                    >
                      <HelpCircle size={11} style={{ opacity: 0.7 }} />
                    </Tooltip>
                  </span>
                </th>
                <th style={thStyle()}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    {t("governance.matrix.colNewBuys")}
                    <Tooltip
                      title={
                        <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 260 }}>
                          是否允许发起<b>新的买入</b>（自动接管 Dry Run / 真实执行 / 手动下单都会走这个门禁）。<br />
                          <span style={{ color: "#f87171" }}>✗ 禁止时：任何买入会被 HG1 直接拒绝</span>，即使你点了也不会下到模拟账户里。
                        </div>
                      }
                    >
                      <HelpCircle size={11} style={{ opacity: 0.7 }} />
                    </Tooltip>
                  </span>
                </th>
                <th style={thStyle()}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    {t("governance.matrix.colRiskExits")}
                    <Tooltip
                      title={
                        <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 280 }}>
                          是否允许<b>风险退出类动作</b>（止损、止盈、强制减仓、已持有的仓位卖出）。<br />
                          <span style={{ color: "#34d399" }}>即使「新买单」被禁止，大部分暂停状态仍允许风险退出</span>——防止暂停后持仓无法止损。
                        </div>
                      }
                    >
                      <HelpCircle size={11} style={{ opacity: 0.7 }} />
                    </Tooltip>
                  </span>
                </th>
                <th style={thStyle()}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    {t("governance.matrix.colAutoRecovery")}
                    <Tooltip
                      title={
                        <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 320 }}>
                          {t("governance.matrix.autoRecoveryHint")}
                        </div>
                      }
                    >
                      <HelpCircle size={11} style={{ opacity: 0.7 }} />
                    </Tooltip>
                  </span>
                </th>
                <th style={thStyle()}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    {t("governance.matrix.colManualAck")}
                    <Tooltip
                      title={
                        <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 320 }}>
                          {t("governance.matrix.manualAckHint")}
                        </div>
                      }
                    >
                      <HelpCircle size={11} style={{ opacity: 0.7 }} />
                    </Tooltip>
                  </span>
                </th>
              </tr>
            </thead>
            <tbody>
              {(Object.keys(ALLOWANCE_MATRIX) as PortfolioStatus[])
                .filter((k) => showFullMatrix || current === k)
                .map((k) => {
                const row = ALLOWANCE_MATRIX[k];
                const isCurrent = current === k;
                return (
                  <tr
                    key={k}
                    style={{
                      background: isCurrent ? "rgba(16,185,129,0.06)" : undefined,
                      outline: isCurrent ? "1px solid rgba(16,185,129,0.3)" : undefined,
                    }}
                  >
                    <td style={tdStyle("left")}>
                      <Tooltip title={t(`portfolioStatus.${k}.desc` as const)} placement="right">
                        <span>
                          <StatusBadge status={k} size="sm" showDesc={false} />
                        </span>
                      </Tooltip>
                    </td>
                    <td style={tdStyle()}>{allowBadge(row.allow_new_buys)}</td>
                    <td style={tdStyle()}>{allowBadge(row.allow_risk_exits)}</td>
                    <td style={tdStyle()}>{allowBadge(row.allow_auto_recovery)}</td>
                    <td style={tdStyle()}>
                      {row.requires_manual_ack ? (
                        <Tooltip
                          title={
                            <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 340 }}>
                              {t("governance.matrix.manualAckHint")}
                            </div>
                          }
                        >
                          <span>{allowBadge(true, "ack")}</span>
                        </Tooltip>
                      ) : (
                        allowBadge(false)
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* 状态转移 Modal（内联样式 Modal，与 CreatePortfolioModal 风格一致） */}
      <TransitionDialog
        open={transitionOpen}
        currentState={current ?? null}
        allowedTargets={allowedTargets}
        targetState={targetState}
        onTargetChange={setTargetState}
        reason={transitionReason}
        onReasonChange={setTransitionReason}
        reviewNote={reviewNote}
        onReviewNoteChange={setReviewNote}
        noteRequired={noteRequired}
        noteTooShort={noteTooShort}
        onCancel={() => setTransitionOpen(false)}
        onConfirm={doTransition}
        submitting={submitting}
      />
    </div>
  );
};

const thStyle = (align: "left" | "center" = "center"): React.CSSProperties => ({
  padding: "10px 12px",
  borderBottom: "1px solid var(--pt-border)",
  textAlign: align,
  fontWeight: 600,
  color: "var(--pt-muted-foreground)",
  fontSize: 12,
  whiteSpace: "nowrap",
});

const tdStyle = (align: "left" | "center" = "center"): React.CSSProperties => ({
  padding: "8px 12px",
  borderBottom: "1px solid var(--pt-border)",
  textAlign: align,
  verticalAlign: "middle",
});

const allowBadge = (allow: boolean | null, flavor: "allow" | "ack" = "allow") => {
  if (allow == null) {
    return <span style={{ color: "var(--pt-muted-foreground)", fontSize: 12 }}>{t("governance.matrix.allowNA")}</span>;
  }
  if (flavor === "ack") {
    return (
      <span
        style={{
          padding: "3px 10px",
          borderRadius: 999,
          border: "1px solid rgba(245,158,11,0.5)",
          background: "rgba(245,158,11,0.14)",
          color: "#fcd34d",
          fontSize: 11.5,
          fontWeight: 700,
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
        }}
      >
        🔒 {t("governance.matrix.needsAck")}
      </span>
    );
  }
  return allow ? (
    <span style={{ color: "#34d399", fontSize: 12, fontWeight: 600 }}>✓ {t("governance.matrix.allowTrue")}</span>
  ) : (
    <span style={{ color: "#f87171", fontSize: 12, fontWeight: 500, opacity: 0.9 }}>✗ {t("governance.matrix.allowFalse")}</span>
  );
};

/* -------------------------------------------------------------------------- */
/* 状态转移 Modal                                                              */
/* -------------------------------------------------------------------------- */

const TransitionDialog: React.FC<{
  open: boolean;
  currentState: PortfolioStatus | null;
  allowedTargets: PortfolioStatus[];
  targetState: PortfolioStatus | null;
  onTargetChange: (s: PortfolioStatus | null) => void;
  reason: string;
  onReasonChange: (s: string) => void;
  reviewNote: string;
  onReviewNoteChange: (s: string) => void;
  noteRequired: boolean;
  noteTooShort: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  submitting: boolean;
}> = (p) => {
  return (
    <Modal
      open={p.open}
      title={t("governance.transitionTitle")}
      onCancel={p.onCancel}
      width={520}
      destroyOnClose
      maskClosable={!p.submitting}
      footer={
        <>
          <Button onClick={p.onCancel} disabled={p.submitting}>
            {t("common.cancel")}
          </Button>
          <Button mutation
            type="primary"
            onClick={p.onConfirm}
            loading={p.submitting}
            disabled={!p.targetState || p.noteTooShort}
          >
            {t("governance.transition.applyBtn")}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)", minWidth: 88 }}>
            {t("governance.transition.current")}
          </span>
          {p.currentState && <StatusBadge status={p.currentState} size="sm" showDesc={false} />}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <label style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>
            {t("governance.transition.target")}{" "}
            {p.targetState && p.currentState && p.targetState === p.currentState && (
              <span style={{ color: "#fbbf24", marginLeft: 6, fontSize: 12 }}>{t("governance.transition.noop")}</span>
            )}
          </label>
          <Select
            style={{ width: "100%" }}
            placeholder="—"
            value={p.targetState ?? undefined}
            disabled={p.allowedTargets.length === 0}
            onChange={(v) => p.onTargetChange((v as PortfolioStatus) || null)}
            options={p.allowedTargets.map((ts) => ({
              value: ts,
              label: t(`portfolioStatus.${ts}` as const),
            }))}
          />
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <label style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>
            {t("governance.transition.triggerReason")}
          </label>
          <Input
            value={p.reason}
            onChange={(e) => p.onReasonChange(e.target.value)}
            placeholder="e.g. 一键刹车 / 解除对账阻断 / 心跳恢复"
          />
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <label style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>
            {t("governance.transition.reviewNote")}
            {p.noteRequired && <span style={{ color: "#f87171", marginLeft: 6 }}>*</span>}
            {p.noteTooShort && <span style={{ color: "#f87171", marginLeft: 6, fontSize: 12 }}>{t("governance.transition.noteMin10")}</span>}
          </label>
          <Input.TextArea
            rows={4}
            value={p.reviewNote}
            onChange={(e) => p.onReviewNoteChange(e.target.value)}
            placeholder="至少 10 字符；说明变更原因、受影响范围、责任人。"
          />
        </div>
      </div>
    </Modal>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab 2：对账守恒 ReconciliationPanel                                    */
/* -------------------------------------------------------------------------- */

/* 结论卡的四种处境。同一句「守恒失败」背后是完全不同的局面：
   缺基准 / 没接数据源 / 真差异 —— 用户该做的事完全不同，不能混着讲。 */
const RECON_VERDICT_STYLE: Record<
  ReconVerdict,
  { bg: string; border: string; fg: string; icon: typeof CheckCircle2; hasNext: boolean }
> = {
  passed:      { bg: "rgba(16,185,129,0.13)", border: "rgba(16,185,129,0.45)", fg: "#86efac", icon: CheckCircle2, hasNext: false },
  no_decision: { bg: "rgba(59,130,246,0.14)", border: "rgba(59,130,246,0.45)", fg: "#93c5fd", icon: Compass,      hasNext: true },
  unavailable: { bg: "rgba(107,114,128,0.18)", border: "rgba(148,163,184,0.4)", fg: "#cbd5e1", icon: HelpCircle,   hasNext: true },
  broken:      { bg: "rgba(220,38,38,0.15)", border: "rgba(248,113,113,0.5)", fg: "#fca5a5", icon: AlertTriangle, hasNext: true },
};

const ReconciliationPanel: React.FC<{
  portfolioId: number;
  showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void;
  onGovernanceChanged?: (portfolioId: number) => void | Promise<void>;
}> = ({ portfolioId, showToast, onGovernanceChanged }) => {
  const [loading, setLoading] = useState(false);
  const [resp, setResp] = useState<ReconciliationResponse | null>(null);
  const [ackChecked, setAckChecked] = useState(false);
  const forceSkipChecked = false;
  const [rerunFirst, setRerunFirst] = useState(true);
  const [reviewNote, setReviewNote] = useState("");
  const [confirming, setConfirming] = useState(false);
  // 人工确认区默认收起：它是「绕过门禁」的高危动作，不该和结论平铺并列，
  // 否则用户一进来就被推到一排勾选框前，不知道要不要动。
  const [showManual, setShowManual] = useState(false);

  const runReconcile = useCallback(async () => {
    if (!portfolioId) return;
    setLoading(true);
    try {
      const r = await api.previewPortfolioReconciliation(portfolioId);
      setResp(r);
      // 提示与结论卡共用同一套分诊：同一件事不该在横幅和 toast 里说成两种。
      const v = classifyReconVerdict({
        zero_sum_check_passed: r.zero_sum_check_passed,
        items: r.items,
      });
      showToast(
        v === "passed" ? "success" : v === "broken" ? "error" : "info",
        t(`governance.reconcile.verdict.${v}.title`),
      );
    } catch (err: any) {
      showToast("error", (err?.message || String(err)) + " (triggerReconcile)");
    } finally {
      setLoading(false);
    }
  }, [portfolioId, showToast]);

  useEffect(() => { void runReconcile(); }, [runReconcile]);

  const diffs: ReconciliationDiffItem[] = resp?.items ?? [];
  const toNumber = (v: ReconciliationDiffItem["expected_value"]): number => {
    if (v == null) return 0;
    if (typeof v === "number") return v;
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  };
  const hasDiff = resp?.differences_found === true ||
    diffs.some((d) => toNumber(d.diff_value) !== 0);

  const noteShort = reviewNote.trim().length < 10;
  const zeroSum = resp?.zero_sum_check_passed === true;
  const canConfirm = ackChecked && (!hasDiff || forceSkipChecked) && !noteShort;

  // 结论分诊：把「缺基准 / 没接数据源 / 真差异」分开，用户才知道要不要动手。
  const verdict: ReconVerdict = classifyReconVerdict({
    zero_sum_check_passed: resp?.zero_sum_check_passed,
    items: diffs,
  });
  const verdictStyle = RECON_VERDICT_STYLE[verdict];
  const VerdictIcon = verdictStyle.icon;

  const doConfirm = async () => {
    if (!portfolioId) return;
    if (!canConfirm) {
      if (!ackChecked) { showToast("error", t("governance.reconcile.ackRequired")); return; }
      if (hasDiff && !forceSkipChecked) { showToast("error", t("governance.reconcile.notReady")); return; }
      if (noteShort) { showToast("error", t("governance.reconcile.reviewNoteMin10")); return; }
    }
    if (rerunFirst) {
      try {
        const latest = await api.triggerPortfolioReconcile(portfolioId);
        setResp(latest);
        if (!latest.zero_sum_check_passed && !forceSkipChecked) {
          showToast("error", t("governance.reconcile.notReady"));
          return;
        }
      } catch (err: any) {
        showToast("error", (err?.message || String(err)) + " (rerun reconcile)");
        return;
      }
    }
    const payload: ConfirmReconciliationRequest = {
      ack: true,
      review_note: reviewNote.trim(),
      force_skip: false,
      force_rerun_before: rerunFirst || undefined,
    };
    setConfirming(true);
    try {
      const out = await api.confirmPortfolioReconciliation(portfolioId, payload);
      await Promise.allSettled([
        Promise.resolve().then(() => onGovernanceChanged?.(portfolioId)),
      ]);
      const ok = out.portfolio_now_ready === true;
      if (ok) {
        // 不把后端状态别名（RECONCILED_CLEARED / ACKNOWLEDGED）直接给用户，
        // 但关联追踪号是运维溯源要用的，保留。
        const statusText = out.acknowledged_diffs_cleared ? "差异已清零" : "已人工确认";
        showToast("success", `${t("governance.reconcile.confirmed")}（${statusText}，关联追踪号：${out.correlation_id ?? "—"}）`);
        setAckChecked(false);

        setReviewNote("");
        setResp(null);
        setTimeout(() => void runReconcile(), 200);
      } else {
        const hint = out.from_state && out.to_state
          ? `${String(out.from_state)} → ${String(out.to_state)}`
          : "portfolio_now_ready=false";
        showToast("error", `confirm failed: ${hint}`);
      }
    } catch (err: any) {
      // 400 ack_required=false, 409 zero_sum fails, 428 never_run
      const code = err?.status_code ?? err?.detail?.code ?? err?.status;
      const msg: string = err?.detail?.message ?? err?.message ?? String(err);
      if (code === 428) showToast("error", `[428] ${t("governance.reconcile.notReady")}: ${msg}`);
      else if (code === 409) showToast("error", `[409] ${t("governance.reconcile.zeroSumFailed")}: ${msg}`);
      else showToast("error", `confirm: ${code ?? ""} ${msg}`);
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div data-testid="reconcile-panel" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* 顶部卡：先讲清楚「现在什么情况、要不要你动手」，再放数字 */}
      <div className="pt-panel" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)", lineHeight: 1.7 }}>
          {t("governance.reconcile.intro")}
        </div>

        {resp && (
          <div
            role="status"
            data-testid="recon-verdict"
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 10,
              padding: "12px 14px",
              borderRadius: 10,
              background: verdictStyle.bg,
              border: `1px solid ${verdictStyle.border}`,
              color: verdictStyle.fg,
            }}
          >
            <VerdictIcon size={18} style={{ flexShrink: 0, marginTop: 2 }} />
            <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 700 }}>
                {t(`governance.reconcile.verdict.${verdict}.title`)}
              </div>
              <div style={{ fontSize: 12, lineHeight: 1.7 }}>
                {t(`governance.reconcile.verdict.${verdict}.desc`)}
              </div>
              {verdictStyle.hasNext && (
                <div style={{ fontSize: 12, lineHeight: 1.7 }}>
                  <b>{t("governance.reconcile.nextStep")}：</b>
                  {t(`governance.reconcile.verdict.${verdict}.next`)}
                </div>
              )}
            </div>
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 18, flexWrap: "wrap" }}>
            <span style={{ fontSize: 13 }}>
              <span style={{ color: "var(--pt-muted-foreground)" }}>{t("governance.reconcile.targetDate")}：</span>
              <b>{resp?.trade_date ?? "—"}</b>
            </span>
            <span style={{ fontSize: 13 }}>
              <span style={{ color: "var(--pt-muted-foreground)" }}>{t("governance.reconcile.lastSuccessDate")}：</span>
              <b>{resp?.last_reconciled_trade_date ?? "—"}</b>
            </span>
          </div>
          <Button mutation type="primary" icon={<RefreshCw size={14} />} onClick={runReconcile} loading={loading}>
            {t("governance.reconcile.btn")}
          </Button>
        </div>

        {/* 10 列差异表 */}
        {resp?.read_only && (
          <div role="note" style={{ color: "var(--pt-muted-foreground)", fontSize: 12 }}>
            {t("governance.reconcile.previewHint")}
          </div>
        )}
        {resp?.cash_audit?.status === "BROKEN" && (
          <div role="alert" className="pt-panel" style={{ padding: 14 }}>
            <b>{t("governance.reconcile.cashGap")}</b>
            <p>{t("governance.reconcile.cashExpected")}: {fmtNum(resp.cash_audit.expected_cash)} · {t("governance.reconcile.cashActual")}: {fmtNum(resp.cash_audit.actual_cash)} · Δ {fmtNum(resp.cash_audit.delta)}</p>
            {resp.cash_audit.differences.map((row) => (
              <div key={row.ledger_id}>#{row.ledger_id} · {fmtNum(row.expected)} → {fmtNum(row.actual)} · Δ {fmtNum(row.delta, 4)}</div>
            ))}
            <p>{t("governance.reconcile.cashReview")}</p>
          </div>
        )}
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 780 }}>
            <thead>
              <tr style={{ background: "var(--pt-surface-1)" }}>
                <th style={thStyle("left")}>{t("governance.reconcile.colDimension")}</th>
                <th style={thStyle()}>{t("governance.reconcile.colExpected")}</th>
                <th style={thStyle()}>{t("governance.reconcile.colActual")}</th>
                <th style={thStyle()}>Δ qty</th>
                <th style={thStyle()}>{t("governance.reconcile.colDiff")} (Δ amount)</th>
                <th style={thStyle("left")}>{t("governance.reconcile.colNote")}</th>
              </tr>
            </thead>
            <tbody>
              {diffs.length === 0 && (
                <tr>
                  <td colSpan={6} style={{ padding: 16, textAlign: "center", color: "var(--pt-muted-foreground)" }}>
                    {loading ? t("governance.reconcile.previewLoading") : resp ? t("governance.reconcile.noDiff") : "—"}
                  </td>
                </tr>
              )}
              {diffs.map((raw, i) => {
                const cash = raw.dimension === "NAV_BROKEN" ? resp?.cash_audit : null;
                const d = cash ? { ...raw, expected_value: cash.expected_cash ?? null,
                  actual_value: cash.actual_cash ?? null, diff_value: cash.delta ?? null } : raw;
                const dim = d.dimension;
                // 维度列不再把后端枚举原值拼给用户看（「自定义维度 (DECISION_RUN_NOT_FOUND)」
                // 这种行没人读得懂）；内部 code 只留在 tooltip 里。
                const dimLabel = reconDimLabel(dim);
                const dimCode = reconDimCodeForTooltip(dim);
                const dimWhy = reconDimWhy(dim);
                const expected = toNumber(d.expected_value);
                const actual = toNumber(d.actual_value);
                const diff = toNumber(d.diff_value);

                // —— 对「元维度」行做特殊视觉：对账服务在这些分支里直接 short-circuit，
                //    expected/actual 本来就不会有数值（例如未执行当日决策），
                //    继续显示「—」会让人误会为"前端 bug/后端没传数"，
                //    改为 info 色底 + N/A + 小字说明，整行视觉上和「数值差异红底」区分开。
                const metaKinds: Array<string> = ["DECISION_RUN_NOT_FOUND", "NOT_CHECKED", "NAV_BROKEN"];
                const isMetaRow =
                  !cash && typeof dim === "string" && metaKinds.some((k) => dim.indexOf(k) >= 0);

                const qtyApplicable =
                  Number.isFinite(expected) && Number.isFinite(actual) &&
                  (dim === "position_count" || dim === "POSITION_MISMATCH" ||
                    String(dim).indexOf("position") >= 0 ||
                    String(dim).indexOf("POSITION") >= 0);
                const deltaQty = qtyApplicable ? actual - expected : NaN;

                let rowBg: string | undefined;
                let rowBadge: React.ReactNode = null;
                if (isMetaRow) {
                  rowBg = "linear-gradient(90deg, rgba(59,130,246,0.10), rgba(59,130,246,0.04))";
                  rowBadge = (
                    <span
                      aria-label="metadata-row"
                      style={{
                        display: "inline-block",
                        marginRight: 6,
                        padding: "1px 6px",
                        borderRadius: 999,
                        fontSize: 10,
                        fontWeight: 700,
                        color: "#93c5fd",
                        background: "rgba(59,130,246,0.18)",
                        border: "1px solid rgba(96,165,250,0.45)",
                      }}
                    >
                      元检查
                    </span>
                  );
                } else if (Number.isFinite(diff) && diff !== 0) {
                  rowBg = "rgba(239,68,68,0.10)";
                }

                const metaNA = (hint: string) => (
                  <span
                    title={hint}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                      padding: "1px 8px",
                      borderRadius: 999,
                      background: "rgba(148,163,184,0.14)",
                      border: "1px dashed rgba(148,163,184,0.65)",
                      color: "var(--pt-muted-foreground)",
                      fontSize: 11,
                    }}
                  >
                    N/A
                    <span style={{ opacity: 0.75 }}>· 无可比对数据</span>
                  </span>
                );

                return (
                  <tr key={`${String(dim)}-${i}`} style={{ background: rowBg }}>
                    <td style={tdStyle("left")}>
                      {dimCode ? (
                        <Tooltip title={dimCode}>
                          <span>{rowBadge}{dimLabel}</span>
                        </Tooltip>
                      ) : (
                        <span>{rowBadge}{dimLabel}</span>
                      )}
                    </td>
                    <td style={tdStyle()}>
                      {isMetaRow
                        ? metaNA("本维度为短路元检查：不产生理论值（账本）")
                        : fmtNum(d.expected_value)}
                    </td>
                    <td style={tdStyle()}>
                      {isMetaRow
                        ? metaNA("本维度为短路元检查：不产生实际值（执行侧）")
                        : fmtNum(d.actual_value)}
                    </td>
                    <td style={tdStyle()}>
                      {isMetaRow ? (
                        metaNA("元检查维度不涉及数量变化")
                      ) : qtyApplicable ? (
                        <span style={{ fontWeight: 600, color: deltaQty > 0 ? "#34d399" : deltaQty < 0 ? "#f87171" : "inherit" }}>
                          {deltaQty > 0 ? "+" : ""}
                          {fmtNum(deltaQty, 0)}
                        </span>
                      ) : (
                        <span
                          title="该维度是金额/计数/证据哈希维度，不涉及 Δ qty 数量差"
                          style={{
                            color: "var(--pt-muted-foreground)",
                            fontSize: 11,
                            padding: "1px 6px",
                            borderRadius: 4,
                            background: "rgba(148,163,184,0.10)",
                            border: "1px dashed rgba(148,163,184,0.35)",
                          }}
                        >
                          不适用
                        </span>
                      )}
                    </td>
                    <td style={tdStyle()}>
                      {isMetaRow ? (
                        metaNA("元检查维度不做数值比较（见右侧说明列判断严重程度）")
                      ) : Number.isFinite(diff) ? (
                        <span style={{ color: diff > 0 ? "#34d399" : diff < 0 ? "#f87171" : undefined, fontWeight: 600 }}>
                          {diff > 0 ? "+" : ""}{fmtNum(diff)}
                        </span>
                      ) : (
                        fmtNum(diff)
                      )}
                    </td>
                    <td style={tdStyle("left")}>
                      <div style={{ display: "flex", flexDirection: "column", gap: 2, textAlign: "left" }}>
                        {dimWhy && <span>{dimWhy}</span>}
                        {d.explain_note && (
                          <span style={{ fontSize: 11, color: "var(--pt-muted-foreground)", lineHeight: 1.6 }}>
                            {d.explain_note}
                          </span>
                        )}
                        {!dimWhy && !d.explain_note && <span>—</span>}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* 人工处理区：默认收起。force_skip 是「绕过门禁」的高危动作，不该和结论
            平铺并列 —— 否则用户一进来就被推到一排勾选框前，不知道要不要动。 */}
        {verdict !== "passed" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <Button
              size="small"
              icon={<Lock size={13} />}
              onClick={() => setShowManual((v) => !v)}
              aria-expanded={showManual}
              data-testid="recon-manual-toggle"
            >
              {showManual ? t("governance.reconcile.hideManual") : t("governance.reconcile.showManual")}
            </Button>

            {!showManual && (
              <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)", lineHeight: 1.7 }}>
                {t("governance.reconcile.manualHint")}
              </div>
            )}

            {showManual && (
              <div
                style={{
                  padding: 16,
                  borderRadius: 10,
                  border: "1px solid var(--pt-border)",
                  background: "var(--pt-surface-1)",
                  display: "flex",
                  flexDirection: "column",
                  gap: 12,
                }}
              >
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <div style={{ fontSize: 14, fontWeight: 700 }}>{t("governance.reconcile.confirmTitle")}</div>
                  <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)", lineHeight: 1.7 }}>
                    {t("governance.reconcile.confirmSubtitle")}
                  </div>
                </div>

                <Checkbox checked={ackChecked} onChange={(e) => setAckChecked(e.target.checked)}>
                  {t("governance.reconcile.ackRequired")}
                </Checkbox>

                <p>普通对账确认会强制重跑并核验差异清零，不能跳过。</p>

                <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                  <label style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>
                    {t("governance.transition.reviewNote")} <span style={{ color: "#f87171" }}>*</span>
                    {noteShort && <span style={{ color: "#f87171", marginLeft: 6, fontSize: 12 }}>{t("governance.reconcile.reviewNoteMin10")}</span>}
                  </label>
                  <Input.TextArea
                    rows={3}
                    value={reviewNote}
                    onChange={(e) => setReviewNote(e.target.value)}
                    placeholder={t("governance.reconcile.notePlaceholder")}
                  />
                </div>

                <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
                  <Button mutation
                    type="primary"
                    danger={forceSkipChecked}
                    onClick={doConfirm}
                    loading={confirming}
                    disabled={!canConfirm}
                  >
                    {t("governance.reconcile.confirmBtn")}
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab 3：审计事件分页过滤                                                 */
/* -------------------------------------------------------------------------- */

/* 审计事件类型：英文枚举 → 中文（兜底：unknown 就原样显示英文，但灰化）     */
const EVENT_TYPE_CN: Record<string, string> = {
  STATE_TRANSITION: "状态转移",
  PORTFOLIO_REBALANCE: "组合调仓",
  CONFIG_CHANGE: "配置变更",
  TERMINAL_LOCK: "终端锁状态变化",
  G5_DUALRUN_START: "G5双跑启动",
  G5_DUALRUN_RESULT: "G5双跑结果确认",
  RECONCILIATION_DIFF: "对账差异检出",
  RECONCILIATION_ACK: "对账差异确认",
  MODEL_BINDING: "模型绑定变更",
  AUTO_PROTECT_PAUSE: "自动保护（心跳/数据/Score）",
  AUTO_PROTECT_RESUME: "自动保护恢复",
  PORTFOLIO_CREATED: "组合创建",
  PORTFOLIO_ARCHIVED: "组合归档",
  // —— 对齐后端 ORM 允许的 action 集合（DataGovernanceAuditEvent ck 约束里的 12 种）
  PORTFOLIO_CANDIDATE_SCD2_CHANGE: "组合候选SCD2变更",
  BENCHMARK_SOURCE_FAILOVER: "基准源故障切换",
  AUTO_SIMULATION_RESULT: "自动推演结果",
  RECONCILIATION_RESULT: "对账结果",
  ILLEGAL_STATE_TRANSITION: "非法状态转移",
  FACTOR_USAGE_APPLIED: "因子使用变更",
  OUTBOX_EVENT_DISPATCHED: "外箱事件分发",
  DATA_BLOCK_RESOLUTION: "数据阻断解除",
  DATA_SOURCE_FAILOVER: "数据源故障切换",
  DATA_QUALITY_QUARANTINE: "数据质量隔离",
  G6_ROLLOUT_STARTED: "G6灰度启动",
  G6_ROLLOUT_ROLLED_BACK: "G6灰度回滚",
  UNKNOWN_AUDIT_ACTION: "未知审计动作",
};

/* 严重级别：英文枚举 → 中文                                            */
const SEVERITY_CN: Record<string, string> = {
  INFO: "信息",
  WARNING: "警告",
  L1: "L1 提醒",
  L2: "L2 警告",
  L3: "L3 严重",
};

/* 审计事件类型下拉（过滤框用，用户不用手敲英文）                           */
const AUDIT_EVENT_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "— 全部 —" },
  // —— 前端旧状态/组合相关枚举（预留，后续后端补写相应 action 后生效）
  { value: "STATE_TRANSITION", label: "状态转移（STATE_TRANSITION）" },
  { value: "PORTFOLIO_REBALANCE", label: "组合调仓（PORTFOLIO_REBALANCE）" },
  { value: "CONFIG_CHANGE", label: "配置变更（CONFIG_CHANGE）" },
  { value: "TERMINAL_LOCK", label: "终端锁（TERMINAL_LOCK）" },
  { value: "G5_DUALRUN_START", label: "G5双跑启动（G5_DUALRUN_START）" },
  { value: "G5_DUALRUN_RESULT", label: "G5双跑结果（G5_DUALRUN_RESULT）" },
  { value: "RECONCILIATION_DIFF", label: "对账差异检出（RECONCILIATION_DIFF）" },
  { value: "RECONCILIATION_ACK", label: "对账差异确认（RECONCILIATION_ACK）" },
  { value: "MODEL_BINDING", label: "模型绑定变更（MODEL_BINDING）" },
  { value: "AUTO_PROTECT_PAUSE", label: "自动保护暂停（AUTO_PROTECT_PAUSE）" },
  { value: "AUTO_PROTECT_RESUME", label: "自动保护恢复（AUTO_PROTECT_RESUME）" },
  { value: "PORTFOLIO_CREATED", label: "组合创建（PORTFOLIO_CREATED）" },
  { value: "PORTFOLIO_ARCHIVED", label: "组合归档（PORTFOLIO_ARCHIVED）" },
  // —— 对齐后端 ORM ck 约束内真实存在的 12 种 action
  { value: "PORTFOLIO_CANDIDATE_SCD2_CHANGE", label: "组合候选SCD2变更（PORTFOLIO_CANDIDATE_SCD2_CHANGE）" },
  { value: "BENCHMARK_SOURCE_FAILOVER", label: "基准源故障切换（BENCHMARK_SOURCE_FAILOVER）" },
  { value: "AUTO_SIMULATION_RESULT", label: "自动推演结果（AUTO_SIMULATION_RESULT）" },
  { value: "RECONCILIATION_RESULT", label: "对账结果（RECONCILIATION_RESULT）" },
  { value: "ILLEGAL_STATE_TRANSITION", label: "非法状态转移（ILLEGAL_STATE_TRANSITION）" },
  { value: "FACTOR_USAGE_APPLIED", label: "因子使用变更（FACTOR_USAGE_APPLIED）" },
  { value: "OUTBOX_EVENT_DISPATCHED", label: "外箱事件分发（OUTBOX_EVENT_DISPATCHED）" },
  { value: "DATA_BLOCK_RESOLUTION", label: "数据阻断解除（DATA_BLOCK_RESOLUTION）" },
  { value: "DATA_SOURCE_FAILOVER", label: "数据源故障切换（DATA_SOURCE_FAILOVER）" },
  { value: "DATA_QUALITY_QUARANTINE", label: "数据质量隔离（DATA_QUALITY_QUARANTINE）" },
  { value: "G6_ROLLOUT_STARTED", label: "G6灰度启动（G6_ROLLOUT_STARTED）" },
  { value: "G6_ROLLOUT_ROLLED_BACK", label: "G6灰度回滚（G6_ROLLOUT_ROLLED_BACK）" },
];

const AuditPanel: React.FC<{ portfolioId: number; showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void }> = ({
  portfolioId,
  showToast,
}) => {
  const [eventType, setEventType] = useState<string>("");
  const [severity, setSeverity] = useState<AuditEventSeverity | "">("");
  const [startDate, setStartDate] = useState<string>("");
  const [endDate, setEndDate] = useState<string>("");
  const [query, setQuery] = useState<string>("");

  const [page, setPage] = useState(1);
  const pageSize = 15;
  const [resp, setResp] = useState<AuditEventPageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [expandedId, setExpandedId] = useState<number | string | null>(null);

  const runQuery = useCallback(async (p = 1) => {
    if (!portfolioId) return;
    setLoading(true);
    try {
      const filter: AuditEventFilter & { page?: number; page_size?: number } = {
        page: p,
        page_size: pageSize,
      };
      if (eventType) filter.event_type = eventType as any;
      if (severity) filter.severity = severity;
      if (startDate) filter.start_date = startDate;
      if (endDate) filter.end_date = endDate;
      if (query) filter.query = query;
      const r = await api.listPortfolioAuditEvents(portfolioId, filter);
      setResp(r);
      setPage(p);
    } catch (err: any) {
      showToast("error", (err?.message || String(err)) + " (listAuditEvents)");
    } finally {
      setLoading(false);
    }
  }, [portfolioId, eventType, severity, startDate, endDate, query, showToast]);

  useEffect(() => { void runQuery(1); }, [runQuery]);

  const rows: AuditEvent[] = resp?.items ?? [];

  const sevStyle = (s: AuditEventSeverity | undefined): React.CSSProperties => {
    switch (s) {
      case "L3": return { background: "rgba(239,68,68,0.16)", color: "#fca5a5", borderColor: "rgba(248,113,113,0.5)" };
      case "L2": return { background: "rgba(249,115,22,0.16)", color: "#fdba74", borderColor: "rgba(249,115,22,0.45)" };
      case "L1": return { background: "rgba(245,158,11,0.16)", color: "#fcd34d", borderColor: "rgba(245,158,11,0.45)" };
      case "WARNING": return { background: "rgba(234,179,8,0.16)", color: "#fde047", borderColor: "rgba(234,179,8,0.45)" };
      case "INFO": default: return { background: "rgba(59,130,246,0.16)", color: "#93c5fd", borderColor: "rgba(96,165,250,0.45)" };
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="pt-panel" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px,1fr))", gap: 10, alignItems: "flex-end" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.eventType")}</label>
            <Select
              style={{ width: "100%" }}
              value={eventType || undefined}
              onChange={(v) => setEventType((v as string) ?? "")}
              placeholder="— 全部 —"
              allowClear
              options={AUDIT_EVENT_TYPE_OPTIONS.filter((opt) => opt.value).map((opt) => ({ value: opt.value, label: opt.label }))}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.severity")}</label>
            <Select
              style={{ width: "100%" }}
              value={severity || undefined}
              onChange={(v) => setSeverity(((v as string) || "") as AuditEventSeverity)}
              placeholder="— 全部 —"
              allowClear
              options={[
                { value: "INFO", label: "INFO（信息）" },
                { value: "WARNING", label: "WARNING（警告）" },
                { value: "L1", label: "L1（提醒）" },
                { value: "L2", label: "L2（警告）" },
                { value: "L3", label: "L3（严重）" },
              ]}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.startDate")}</label>
            <DatePicker
              style={{ width: "100%" }}
              format="YYYY-MM-DD"
              allowClear
              value={startDate ? dayjs(startDate) : null}
              onChange={(d) => setStartDate(d ? d.format("YYYY-MM-DD") : "")}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.endDate")}</label>
            <DatePicker
              style={{ width: "100%" }}
              format="YYYY-MM-DD"
              allowClear
              value={endDate ? dayjs(endDate) : null}
              onChange={(d) => setEndDate(d ? d.format("YYYY-MM-DD") : "")}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.query")}</label>
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="关键词 / 关联追踪号" allowClear />
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <Button type="primary" onClick={() => runQuery(1)} loading={loading}>
              {t("governance.audit.filter.apply")}
            </Button>
            <Button
              onClick={() => {
                setEventType(""); setSeverity(""); setStartDate(""); setEndDate(""); setQuery("");
                setTimeout(() => runQuery(1), 0);
              }}
            >
              {t("governance.audit.filter.reset")}
            </Button>
          </div>
        </div>
      </div>

      <div className="pt-panel" style={{ padding: 16 }}>
        <h4 style={{ margin: "0 0 10px 0", fontSize: 14 }}>{t("governance.audit.title")}</h4>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 1100 }}>
            <thead>
              <tr style={{ background: "var(--pt-surface-1)" }}>
                <th style={thStyle("left")}>{t("governance.audit.col.id")}</th>
                <th style={thStyle("left")}>{t("governance.audit.col.time")}</th>
                <th style={thStyle("left")}>{t("governance.audit.col.type")}</th>
                <th style={thStyle()}>{t("governance.audit.col.severity")}</th>
                <th style={thStyle("left")}>{t("governance.audit.col.trigger")}</th>
                <th style={thStyle("left")}>{t("governance.audit.col.operatedBy")}</th>
                <th style={thStyle("left")}>{t("governance.audit.col.reviewedAt")}</th>
                <th style={thStyle("left")}>{t("governance.audit.col.fromTo")}</th>
                <th style={thStyle("left")}>{t("governance.audit.col.correlation")}</th>
                <th style={thStyle()}></th>
              </tr>
            </thead>
            <tbody>
              {loading && rows.length === 0 && (
                <tr><td colSpan={10} style={{ padding: 16, textAlign: "center", color: "var(--pt-muted-foreground)" }}>…</td></tr>
              )}
              {!loading && rows.length === 0 && (
                <tr><td colSpan={10} style={{ padding: 16, textAlign: "center", color: "var(--pt-muted-foreground)" }}>{t("governance.audit.empty")}</td></tr>
              )}
              {rows.map((ev) => {
                const rowKey = ev.id ?? `${String(ev.event_type)}-${String((ev as any).created_at || (ev as any).occurred_at)}-${String(Math.random())}`;
                const isOpen = expandedId === rowKey;
                const evAny = ev as any;
                // created_at 兜底后端 ORM 的 occurred_at（旧接口只发 occurred_at）
                const createdAt: string | null | undefined =
                  (ev as any).created_at ?? (ev as any).occurred_at ?? null;
                // 空值行判定：created_at/trigger_reason/operated_by 都没值 = 假数据/未回填行，整体颜色变淡
                const isSkeletonRow =
                  !createdAt && !ev.trigger_reason && !ev.operated_by && !ev.from_state && !ev.to_state;
                const rowOpacity: React.CSSProperties | undefined = isSkeletonRow
                  ? { opacity: 0.7 }
                  : undefined;
                // 事件类型渲染：后端可能带 event_type_cn；没有就走前端 EVENT_TYPE_CN；都不命中 → 英文斜体兜底
                let eventTypeNode: React.ReactNode = <span style={{ color: "var(--pt-muted-foreground)" }}>—</span>;
                if (ev.event_type) {
                  const et = String(ev.event_type);
                  const cn = String(evAny.event_type_cn || "").trim() || EVENT_TYPE_CN[et];
                  eventTypeNode = cn ? (
                    <span>
                      <b style={{ color: "var(--pt-foreground)" }}>{cn}</b>
                      <span style={{ color: "var(--pt-muted-foreground)", fontSize: 10.5, marginLeft: 4 }}>（{et}）</span>
                    </span>
                  ) : (
                    <span style={{ color: "#64748b", fontSize: 11.5, fontStyle: "italic" }}>{et}</span>
                  );
                }
                // 严重级别文字（中文优先）
                const sevRaw = ev.severity || (evAny.severity);
                const severiTyLabel = sevRaw
                  ? SEVERITY_CN[String(sevRaw)] ?? String(sevRaw)
                  : "—";
                // 操作人：后端 _ev_to_read 会把 operator_id 填入 operated_by（字符串兼容，不必强制是数字 uid）
                const opName = evAny?.operated_by_name || evAny?.display_name;
                const opRaw = (ev.operated_by ?? evAny.operator_id) as unknown;
                const opStr = opRaw == null ? "" : String(opRaw);
                const opNode = opStr ? (
                  <span title={`operator_id=${opStr}`}>
                    {opName ? (
                      <>
                        <b>{String(opName)}</b>
                        <span style={{ color: "var(--pt-muted-foreground)", fontSize: 10.5, marginLeft: 4 }}>
                          id={opStr}
                        </span>
                      </>
                    ) : (
                      <>
                        id=<b>{opStr}</b>
                      </>
                    )}
                  </span>
                ) : (
                  <span style={{ color: "var(--pt-muted-foreground)" }}>—</span>
                );
                // 产生时间格式化成 YYYY-MM-DD HH:mm（created_at / occurred_at 任一都可）
                let timeNode: React.ReactNode = <span style={{ color: "var(--pt-muted-foreground)" }}>—</span>;
                if (createdAt) {
                  try {
                    const d = dayjs(createdAt);
                    if (d.isValid()) timeNode = <span>{d.format("YYYY-MM-DD HH:mm")}</span>;
                    else timeNode = <span>{String(ev.created_at)}</span>;
                  } catch {
                    timeNode = <span>{String(ev.created_at)}</span>;
                  }
                }
                // types: attributes_json?: string | null; attributes?: Record<string, unknown> | null
                let attrsObj: Record<string, unknown> | null = null;
                try {
                  if (ev.attributes && typeof ev.attributes === "object" && !Array.isArray(ev.attributes)) {
                    attrsObj = ev.attributes as Record<string, unknown>;
                  } else if (ev.attributes_json && typeof ev.attributes_json === "string") {
                    const parsed = JSON.parse(ev.attributes_json);
                    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                      attrsObj = parsed as Record<string, unknown>;
                    }
                  }
                } catch {
                  /* parse 失败：保留 null */
                }
                return (
                  <React.Fragment key={String(rowKey)}>
                    <tr style={rowOpacity}>
                      <td style={tdStyle("left")}>{String(ev.id ?? "—")}</td>
                      <td style={tdStyle("left")}>{timeNode}</td>
                      <td style={tdStyle("left")}>{eventTypeNode}</td>
                      <td style={tdStyle()}>
                        <span
                          style={{
                            display: "inline-block",
                            padding: "2px 8px",
                            borderRadius: 6,
                            border: "1px solid",
                            fontSize: 11,
                            fontWeight: 600,
                            ...sevStyle(sevRaw as any),
                          }}
                        >
                          {severiTyLabel}
                        </span>
                      </td>
                      <td style={tdStyle("left")}>
                        {ev.trigger_reason ? (
                          <span>{ev.trigger_reason}</span>
                        ) : (
                          <span style={{ color: "var(--pt-muted-foreground)" }}>—</span>
                        )}
                      </td>
                      <td style={tdStyle("left")}>{opNode}</td>
                      <td style={tdStyle("left")}>
                        {ev.reviewed_at ? (
                          <span>{dayjs(ev.reviewed_at).isValid() ? dayjs(ev.reviewed_at).format("YYYY-MM-DD HH:mm") : String(ev.reviewed_at)}</span>
                        ) : (
                          <span style={{ color: "var(--pt-muted-foreground)" }}>—</span>
                        )}
                      </td>
                      <td style={tdStyle("left")}>
                        {ev.from_state || ev.to_state ? (
                          <span>
                            {ev.from_state ? <StatusBadge status={ev.from_state as PortfolioStatus} size="sm" showDesc={false} /> : "∅"}
                            {" → "}
                            {ev.to_state ? <StatusBadge status={ev.to_state as PortfolioStatus} size="sm" showDesc={false} /> : "∅"}
                          </span>
                        ) : (
                          <span style={{ color: "var(--pt-muted-foreground)" }}>—</span>
                        )}
                      </td>
                      <td style={tdStyle("left")}>
                        {ev.correlation_id ? <code style={{ fontSize: 11 }}>{ev.correlation_id}</code> : <span style={{ color: "var(--pt-muted-foreground)" }}>—</span>}
                      </td>
                      <td style={tdStyle()}>
                        <Button
                          type="text"
                          size="small"
                          disabled={!attrsObj || Object.keys(attrsObj).length === 0}
                          onClick={() => setExpandedId(isOpen ? null : rowKey)}
                          title={t("governance.audit.col.expand")}
                        >
                          {isOpen ? <X size={14} /> : "···"}
                        </Button>
                      </td>
                    </tr>
                    {isOpen && attrsObj && (
                      <tr>
                        <td colSpan={10} style={{ padding: "0 16px 10px 16px" }}>
                          <pre
                            style={{
                              margin: 0,
                              padding: 10,
                              background: "var(--pt-surface-3)",
                              border: "1px solid var(--pt-border)",
                              borderRadius: 8,
                              fontSize: 11,
                              whiteSpace: "pre-wrap",
                              wordBreak: "break-word",
                              color: "var(--pt-foreground)",
                            }}
                          >
                            {JSON.stringify(attrsObj, null, 2)}
                          </pre>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* pagination */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 12, flexWrap: "wrap", gap: 8 }}>
          <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
            total: {resp?.total ?? 0} · page {page}/{resp?.page_count ?? 1}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <Button
              size="small"
              disabled={page <= 1 || loading}
              onClick={() => runQuery(page - 1)}
            >
              ← Prev
            </Button>
            <Button
              size="small"
              disabled={page >= (resp?.page_count ?? 1) || loading}
              onClick={() => runQuery(page + 1)}
            >
              Next →
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab 4：G5 双跑对账                                                     */
/* -------------------------------------------------------------------------- */

const G5Panel: React.FC<{ portfolioId: number; showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void; onOverlayChange?: (open:boolean)=>void; visible?:boolean }> = ({
  portfolioId,
  showToast,
  onOverlayChange,
  visible=true,
}) => {
  const MIN_TRADE_DAYS = 10;
  const [reportOpen,setReportOpen]=useState(false);
  useEffect(()=>{onOverlayChange?.(visible&&reportOpen);return()=>onOverlayChange?.(false);},[visible,reportOpen,onOverlayChange]);

  // Launch form
  const [startDate, setStartDate] = useState<string>(() => dayjs().subtract(3, "month").format("YYYY-MM-DD"));
  const [endDate, setEndDate] = useState<string>(() => dayjs().format("YYYY-MM-DD"));
  const [oldEngine, setOldEngine] = useState<string>("");
  const [newEngine, setNewEngine] = useState<string>("");
  const [launching, setLaunching] = useState(false);
  const [replayId, setReplayId] = useState<string | null>(null);
  const [storedSummary, setSummary] = useState<G5DualRunSummaryResponse | null>(null);
  const [reportOptions, setReportOptions] = useState<Array<{ replay_id: string; start_date: string; end_date: string; verdict: string }>>([]);
  const [reportListError, setReportListError] = useState("");
  const [selectedDay, setSelectedDay] = useState<string>("all");
  useEffect(() => { let cancelled = false; setReportOptions([]); setReportListError(""); void Promise.resolve().then(() => api.getG5DualRunReports(portfolioId)).then(value => { if (!value || value.portfolio_id !== portfolioId || !Array.isArray(value.reports)) throw new Error("报告列表材料不完整"); if (!cancelled) setReportOptions(value.reports); }).catch(() => { if (!cancelled) setReportListError("历史双跑报告读取失败，不能把未读取视作无记录。"); }); return () => { cancelled = true; }; }, [portfolioId]);
  const summary = storedSummary?.portfolio_id === portfolioId ? storedSummary : null;
  const [loadingSummary, setLoadingSummary] = useState(false);
  const [capability, setCapability] = useState<{ real_runner_available: boolean; message: string; old_chain?: { id: string; label: string; engine_version: string } | null; candidate_chains?: Array<{ id: string; label: string; engine_version: string }> } | null>(null);
  const [capabilityError, setCapabilityError] = useState("");
  const capabilitySequence = useRef(0);
  const readCapability = useCallback(async () => {
    const token = ++capabilitySequence.current;
    setCapability(null); setCapabilityError("");
    try {
      const value = await api.getG5DualRunCapabilities(portfolioId);
      if (token === capabilitySequence.current) {
        setCapability(value);
        setOldEngine(value.old_chain?.engine_version ?? "");
        setNewEngine(current => value.candidate_chains?.some(chain => chain.engine_version === current) ? current : "");
      }
    } catch {
      if (token === capabilitySequence.current) setCapabilityError("暂时无法确认能否运行，请重新读取。");
    }
  }, [portfolioId]);
  useEffect(() => { void readCapability(); return () => { ++capabilitySequence.current; }; }, [readCapability]);
  const [asyncTaskStatus, setAsyncTaskStatus] = useState<{ is_terminal_locked: boolean; lock_reason: string | null; status: string } | null>(null);

  const daysOk = Boolean(startDate && endDate && dayjs(startDate).isValid() && dayjs(endDate).isValid() && !dayjs(endDate).isBefore(dayjs(startDate)));
  const registeredPair = Boolean(oldEngine && newEngine && oldEngine !== newEngine && capability?.old_chain?.engine_version === oldEngine && capability?.candidate_chains?.some(chain => chain.engine_version === newEngine));

  const [resultError, setResultError] = useState("");
  const [resultReasons, setResultReasons] = useState<string[]>([]);
  const resultSequence = useRef(0);
  const activePortfolio = useRef(portfolioId);
  activePortfolio.current = portfolioId;
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const launchInFlight = useRef(false);
  const stopPoll = useCallback(() => {
    if (pollTimer.current) clearTimeout(pollTimer.current);
    pollTimer.current = null;
  }, []);
  useEffect(() => {
    ++resultSequence.current; stopPoll(); launchInFlight.current = false;
    setReplayId(null); setSummary(null); setResultError(""); setAsyncTaskStatus(null); setReportOpen(false);
    setLoadingSummary(false); setLaunching(false);
    return () => { ++resultSequence.current; stopPoll(); };
  }, [portfolioId, stopPoll]);

  const readResult = useCallback(async (pid: number, id: string, token: number) => {
    const current = () => activePortfolio.current === pid && resultSequence.current === token;
    if (!current()) return;
    setLoadingSummary(true); setSummary(null); setResultReasons([]);
    try {
      const value = await api.queryG5DualRunResult(pid, id);
      if (!current()) return;
      if (value.portfolio_id !== pid) {
        setResultError("这份报告不属于当前组合，请重新查询。");
        return;
      }
      const status = String(value.status ?? "").toUpperCase();
      setAsyncTaskStatus({ is_terminal_locked: value.is_terminal_locked === true, lock_reason: value.terminal_lock_reason ?? null, status });
      if (["SUBMITTED", "QUEUED", "RUNNING"].includes(status) && value.g5_eligible_for_g6 !== true) {
        setSummary(null); setResultError("");
        pollTimer.current = setTimeout(() => void readResult(pid, id, token), 5000);
        return;
      }
      const hasMatrix = Array.isArray(value.daily_reports) && value.daily_reports.length === value.days_replayed
        && value.days_replayed >= MIN_TRADE_DAYS && value.total_days === value.days_replayed && value.skipped_days?.length === 0
        && value.daily_reports.every(day => G5_BUSINESS_ACTIONS.every(a => G5_BUSINESS_ACTIONS.every(b => {
          const count = day.action_confusion_matrix?.[a]?.[b];
          return typeof count === "number" && Number.isInteger(count) && count >= 0;
        })) && G5_BUSINESS_ACTIONS.reduce((sum, a) => sum + G5_BUSINESS_ACTIONS.reduce((rowSum, b) => rowSum + (day.action_confusion_matrix?.[a]?.[b] ?? 0), 0), 0) > 0);
      if (status !== "COMPLETED" || value.evidence_verified !== true || !hasMatrix) {
        setSummary(null); setResultError("报告缺少可核验的真实执行证据，不能据此切换方案。请重新查询。");
        return;
      }
      setSummary(value); setResultError("");
    } catch (error) {
      if (!current()) return;
      setSummary(null); stopPoll();
      const code = error instanceof ApiError ? error.status_code : null;
      const raw = error instanceof ApiError ? error.detail as any : null;
      const reasons = raw?.detail?.reasons ?? raw?.reasons;
      setResultReasons(Array.isArray(reasons) ? reasons.slice(0,40).map(String) : []);
      setResultError(code === 404 ? "报告不存在或已清理，不能据此切换方案。"
        : code === 409 ? "报告内容或真实执行证据未通过核对，不能据此切换方案。原始报告已保留。"
        : "暂时读不到报告，已清除上次通过状态。请重新查询。");
      setAsyncTaskStatus({ is_terminal_locked: false, lock_reason: null, status: code === 404 ? "NOT_FOUND" : "INVALID" });
    } finally { if (current()) setLoadingSummary(false); }
  }, [stopPoll]);

  const doLaunch = async () => {
    if (!portfolioId || capability?.real_runner_available !== true || launchInFlight.current || !daysOk || !registeredPair) return;

    const pid = portfolioId, token = ++resultSequence.current;
    stopPoll(); launchInFlight.current = true; setLaunching(true); setSummary(null); setResultError(""); setReplayId(null);
    try {
      const result = await api.launchG5DualRun(pid, { start_date: startDate, end_date: endDate, old_engine_version: oldEngine.trim(), new_engine_version: newEngine.trim() });
      if (activePortfolio.current !== pid || resultSequence.current !== token) return;
      if (!result.replay_id) { setResultError("尚未取得对比记录，请重新查询。"); return; }
      setReplayId(result.replay_id);setReportOpen(true);
      void readResult(pid, result.replay_id, token);
    } catch {
      if (activePortfolio.current === pid && resultSequence.current === token) setResultError("对比没有开始。请重新读取运行条件后再试。");
    } finally {
      if (activePortfolio.current === pid && resultSequence.current === token) { launchInFlight.current = false; setLaunching(false); }
    }
  };

  const doFetchResult = useCallback(() => {
    if (!portfolioId || !replayId) return;
    stopPoll();setReportOpen(true);
    void readResult(portfolioId, replayId, ++resultSequence.current);
  }, [portfolioId, replayId, readResult, stopPoll]);

  const locked = asyncTaskStatus?.is_terminal_locked === true;
  const lockReason = asyncTaskStatus?.lock_reason ?? "T-LOCK";
  const eligible = !resultError && summary?.status === "COMPLETED" && summary.evidence_verified === true && summary.g5_eligible_for_g6 === true;

  // Display the whole verified interval, using the same daily records as the gate.
  const viewedDays = summary?.daily_reports.filter(day => selectedDay === "all" || day.trade_date === selectedDay) ?? [];
  const report = viewedDays[0] ?? null;
  const matrix = summary ? Object.fromEntries(G5_BUSINESS_ACTIONS.map(a => [a, Object.fromEntries(G5_BUSINESS_ACTIONS.map(b => [b,
    viewedDays.reduce((sum, day) => sum + day.action_confusion_matrix![a]![b]!, 0),
  ]))])) : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {capability?.real_runner_available === false && <div role="status">{capability.message}</div>}
      {capability?.real_runner_available === true && !capability.old_chain && <p>执行器未返回可核实的已登记引擎链，暂不能提交验收。</p>}
      {!reportOpen&&resultError && <div role="alert">{resultError}</div>}{!reportOpen&&resultReasons.length > 0 && <details className="pi-disclosure"><summary>查看未通过的具体证据校验项</summary><p>以下为本次报告的实际校验结果，原始记录继续保留。</p>{resultReasons.map((reason,i) => <p key={i}>{reason}</p>)}</details>}
      {capabilityError && <div role="alert">{capabilityError} <Button onClick={() => void readCapability()}>重新读取</Button></div>}
      <section className="pi-callout"><strong>查看真实留存的验收报告</strong><p>历史报告保留原版本和原始证据；不能将旧验收直接当作当前运行授权。</p>{reportListError && <p role="alert">{reportListError}</p>}<Select aria-label="选择历史双跑报告" style={{width:"100%"}} placeholder={reportOptions.length ? "按实际日期和结果选择报告" : "未读取到可选的留存报告"} value={reportOptions.some(x => x.replay_id === replayId) ? replayId : undefined} options={reportOptions.map(r => ({value:r.replay_id,label:`${r.start_date} 至 ${r.end_date} · ${r.verdict}`}))} onChange={id => { stopPoll(); setReportOpen(true); setReplayId(id); setSelectedDay("all"); void readResult(portfolioId,id,++resultSequence.current); }}/></section>
      {/* 启动表单 */}
      <div className="pt-panel" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
          <div>
            <h4 style={{ margin: 0, fontSize: 14 }}>{t("g5.title")}</h4>
            <p style={{ margin: "4px 0 0 0", fontSize: 12, color: "var(--pt-muted-foreground)", maxWidth: 900 }}>{t("g5.intro")}</p>
          </div>
          {replayId&&<Button onClick={()=>setReportOpen(true)}>查看本次验收报告</Button>}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px,1fr))", gap: 12, alignItems: "flex-end" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.startDate")}</label>
            <DatePicker
              style={{ width: "100%" }}
              format="YYYY-MM-DD"
              allowClear={false}
              disabled={locked}
              value={startDate ? dayjs(startDate) : null}
              onChange={(d) => d && setStartDate(d.format("YYYY-MM-DD"))}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.endDate")}</label>
            <DatePicker
              style={{ width: "100%" }}
              format="YYYY-MM-DD"
              allowClear={false}
              disabled={locked}
              value={endDate ? dayjs(endDate) : null}
              onChange={(d) => d && setEndDate(d.format("YYYY-MM-DD"))}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.oldEngine")}</label>
            <Input aria-label="当前已登记引擎链" value={capability?.old_chain ? `${capability.old_chain.label} · ${capability.old_chain.engine_version}` : "尚无可核实的已登记执行链"} readOnly />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.newEngine")}</label>
            <Select aria-label="候选新引擎链" value={newEngine || undefined} onChange={setNewEngine} placeholder="选择已登记的候选引擎链" disabled={locked || capability?.real_runner_available !== true || !capability?.candidate_chains?.length} options={capability?.candidate_chains?.filter(chain => chain.engine_version !== oldEngine).map(chain => ({ value: chain.engine_version, label: `${chain.label} · ${chain.engine_version}` })) ?? []} />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <Button mutation
              type="primary"
              icon={<PlayCircle size={14} />}
              onClick={doLaunch}
              loading={launching}
              disabled={locked || capability?.real_runner_available !== true || !registeredPair || !daysOk}
            >
              {t("g5.launch")}
            </Button>
            <Button icon={<RefreshCw size={14} />} onClick={doFetchResult} loading={loadingSummary} disabled={!replayId}>
              {t("g5.queryResult")}
            </Button>
            {!daysOk && (
              <span style={{ fontSize: 12, color: "#f87171", fontWeight: 600 }}>
                {template(t("g5.tradeDaysCheck") as string, { minDays: String(MIN_TRADE_DAYS) })}
                {" "}
                (请检查起止日期；实际交易日数由可信日历核实)
              </span>
            )}
          </div>
        </div>

        {/* 异步任务行（含终态锁 disabled + Tooltip） */}
        <Tooltip
          title={
            locked
              ? `${t("terminalLock.rowDisabled")} (${lockReason})`
              : replayId
                ? "正在读取对比结果。"
                : "尚未开始对比。"
          }
        >
          <div
            style={{
              padding: "10px 12px",
              borderRadius: 10,
              border: "1px solid var(--pt-border)",
              background: locked ? "var(--pt-surface-3)" : "var(--pt-surface-2)",
              opacity: locked ? 0.7 : 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              flexWrap: "wrap",
              filter: locked ? "grayscale(0.25)" : undefined,
              pointerEvents: locked ? "none" : undefined,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              {locked && <Lock size={14} style={{ color: "#dc2626" }} />}
              <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.resultId")}:</span>
              <code style={{ fontSize: 12 }}>{replayId ?? "—"}</code>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 700,
                  padding: "2px 8px",
                  borderRadius: 6,
                  border: "1px solid var(--pt-border)",
                  background: "var(--pt-surface-1)",
                  color: "var(--pt-foreground)",
                }}
              >
                status = {asyncTaskStatus?.status ?? "—"}
              </span>
              {locked && (
                <span style={{ color: "#dc2626", fontSize: 12, fontWeight: 600 }}>{t("terminalLock.isLocked")} · {lockReason}</span>
              )}
            </div>
            <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
              {summary
                ? template(t("g5.progress") as string, {
                    replayed: String(summary.days_replayed ?? 0),
                    total: String(summary.total_days ?? 0),
                  })
                : replayId
                  ? t("g5.running")
                  : "—"}
            </div>
          </div>
        </Tooltip>
      </div>

      <InspectionDrawer open={reportOpen&&visible} zIndex={1500} title="双跑验收报告详情" context="检查与对账 / 引擎验收 / 报告预览"
        onClose={()=>setReportOpen(false)} onBack={()=>setReportOpen(false)} backLabel="返回双跑验收"
        footer={<Button onClick={doFetchResult} loading={loadingSummary} disabled={!replayId}>{t("g5.queryResult")}</Button>}>
        {resultError&&<div role="alert" className="pi-callout pi-error">{resultError}</div>}
        {resultReasons.length>0&&<details><summary>查看未通过的具体证据校验项</summary>{resultReasons.map((reason,index)=><p key={index}>{reason}</p>)}</details>}
        {summary&&<Tag color={eligible?"success":"error"}>{eligible?t("g5.eligible"):t("g5.notEligible")}</Tag>}
      {/* 汇总卡 + 6×6 混淆矩阵 + 归因 */}
      {summary && (
        <>
          <div className="pt-panel" style={{ padding: 16 }}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(200px,1fr))",
                gap: 12,
                marginBottom: 14,
              }}
            >
              <StatCard label={t("g5.avgMatch")} value={pct(summary.avg_action_match_rate)} positive={(summary.avg_action_match_rate ?? 0) >= 0.95} />
              <StatCard label={t("g5.jaccard")} value={pct(summary.avg_universe_jaccard)} positive={(summary.avg_universe_jaccard ?? 0) >= 0.85} />
              <StatCard
                label={t("g5.p0.count")}
                value={String(summary.total_p0_unexplained ?? 0)}
                positive={summary.total_p0_unexplained === 0}
                invertGood /* 0 = good */
              />
              <StatCard
                label={t("g5.p1.count")}
                value={String(summary.total_p1_hold_noaction_flip ?? 0)}
                positive={(summary.total_p1_hold_noaction_flip ?? 0) === 0}
                invertGood
              />
              <StatCard label={t("g5.failingDaysP0")} value={String((summary.failing_days_p0 ?? []).length || 0)} positive={(summary.failing_days_p0 ?? []).length === 0} invertGood />
              <StatCard label={t("g5.failingDaysP1")} value={String((summary.failing_days_p1 ?? []).length || 0)} positive={(summary.failing_days_p1 ?? []).length === 0} invertGood />
            </div>

            <div className="pi-callout"><p>验收策略 {summary.acceptance_policy_version ?? "旧记录未保留"} · 有效样本 {summary.valid_symbol_days ?? "未知"} 个标的交易日</p><Select aria-label="双跑报告交易日" value={selectedDay} onChange={setSelectedDay} style={{width:"100%"}} options={[{value:"all",label:"全区间汇总"},...summary.daily_reports.map(d => ({value:d.trade_date,label:d.trade_date}))]}/><p>当前视图真实动作样本分母 {viewedDays.every(d => d.action_denominator != null) ? viewedDays.reduce((n,d) => n+d.action_denominator!,0) : "未提供"} · 已匹配 {viewedDays.every(d => d.action_match_count != null) ? viewedDays.reduce((n,d) => n+d.action_match_count!,0) : "未提供"}</p><details><summary>查看来源、策略与逐标的原始差异</summary><pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{JSON.stringify({policy:summary.acceptance_policy,scope:summary.applicable_scope,provenance:summary.provenance,days:viewedDays.map(d=>({date:d.trade_date,source_a:d.chain_a_meta,source_b:d.chain_b_meta,sample_status:d.sample_status,metric_status:d.metric_status,symbol_diffs:d.symbol_diffs}))},null,2)}</pre></details><p>系统差异归因尚需本人核实，自动分类不代表已审核。</p>{summary.acceptance_blockers?.map((reason,i) => <p key={i} className="pi-warning-text">{reason}</p>)}</div>
            <h5 style={{ margin: "0 0 10px 0", fontSize: 13 }}>{selectedDay === "all" ? "全区间" : selectedDay} · {t("g5.matrix.title")}</h5>
            <div style={{ overflowX: "auto" }}>
              <table style={{ borderCollapse: "collapse", fontSize: 12, minWidth: 560 }}>
                <thead>
                  <tr style={{ background: "var(--pt-surface-1)" }}>
                    <th style={thStyle("left")}>{t("g5.matrix.oldHeader")}</th>
                    {G5_BUSINESS_ACTIONS.map((a) => (
                      <th key={a} style={thStyle("left")}>{G5_ACTION_LABELS[a]}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {G5_BUSINESS_ACTIONS.map((oldAct) => (
                    <tr key={oldAct}>
                      <td style={tdStyle("left")}>{G5_ACTION_LABELS[oldAct]}</td>
                      {G5_BUSINESS_ACTIONS.map((newAct) => {
                        const count = (matrix as any)?.[oldAct]?.[newAct];
                        const isDiagonal = oldAct === newAct;
                        const warnBg = !isDiagonal && count > 0 ? "rgba(239,68,68,0.18)" : undefined;
                        return (
                          <td
                            key={`${oldAct}-${newAct}`}
                            style={{
                              padding: "6px 10px",
                              borderBottom: "1px solid var(--pt-border)",
                              textAlign: "center",
                              background: warnBg,
                              fontWeight: isDiagonal ? 600 : 500,
                              color: isDiagonal ? "#34d399" : count > 0 ? "#fca5a5" : undefined,
                            }}
                          >
                            {count}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 8, display: "flex", gap: 12, flexWrap: "wrap", fontSize: 11, color: "var(--pt-muted-foreground)" }}>
              <span>✓ {t("g5.matrix.diagonal")} = 对角线 (match)</span>
              <span style={{ color: "#f87171" }}>⚠ {t("g5.matrix.off")} = 非对角 (mismatch)</span>
            </div>
          </div>

          {/* 归因分类聚合统计表 */}
          <div className="pt-panel" style={{ padding: 16 }}>
            <h5 style={{ margin: "0 0 10px 0", fontSize: 13 }}>Attribution Aggregate (all days)</h5>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 820 }}>
                <thead>
                  <tr style={{ background: "var(--pt-surface-1)" }}>
                    <th style={thStyle("left")}>Attribution Category</th>
                    <th style={thStyle()}>{t("g5.attr.symbol")}</th>
                    <th style={thStyle()}>{t("g5.attr.summary")}</th>
                    <th style={thStyle()}>{t("g5.attr.severity")}</th>
                    <th style={thStyle("left")}>{t("g5.attr.changeNote")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(() => {
                    const items: { cat: G5AttributionCategory; s: string | null; summary: string; sev: string | null; note: string | null; symbol: string | null }[] = [];
                    for (const r of viewedDays) {
                      for (const a of r.attributions ?? []) {
                        items.push({
                          cat: a.category,
                          s: a.symbol ?? a.symbol_id != null ? String(a.symbol_id) : null,
                          summary: a.summary ?? "",
                          sev: a.severity ?? null,
                          note: a.change_note ?? null,
                          symbol: a.symbol ?? (a.symbol_id != null ? String(a.symbol_id) : null),
                        });
                      }
                    }
                    if (items.length === 0) {
                      return (
                        <tr>
                          <td colSpan={5} style={{ padding: 16, textAlign: "center", color: "var(--pt-muted-foreground)" }}>
                            {t("g5.attr.empty")}
                          </td>
                        </tr>
                      );
                    }
                    return items.slice(0, 100).map((it, i) => {
                      const p0 = it.cat === "UNKNOWN_ENGINE_DIFF" || it.cat === "DUPLICATE_REPLAY_SIDE_EFFECT" || it.cat === "CORRUPTED_SNAPSHOT_OR_EVIDENCE";
                      return (
                        <tr key={i} style={{ background: p0 ? "rgba(239,68,68,0.12)" : undefined }}>
                          <td style={tdStyle("left")}>
                            <span style={{ fontWeight: 600, color: p0 ? "#fca5a5" : undefined }}>
                              {t(`g5.attr.${it.cat}` as any)}
                            </span>
                          </td>
                          <td style={tdStyle()}>{it.symbol ?? "—"}</td>
                          <td style={tdStyle("left")}>{it.summary || "—"}</td>
                          <td style={tdStyle()}>
                            {it.sev ? (
                              <span
                                style={{
                                  padding: "2px 8px",
                                  borderRadius: 6,
                                  border: "1px solid",
                                  fontSize: 11,
                                  fontWeight: 600,
                                  ...sevAttrStyle(it.sev),
                                }}
                              >
                                {it.sev}
                              </span>
                            ) : "—"}
                          </td>
                          <td style={tdStyle("left")}>
                            {it.note ? (
                              <span style={{ whiteSpace: "pre-wrap" }}>{it.note}</span>
                            ) : p0 ? (
                              <span style={{ color: "#f87171", fontWeight: 600 }}>⚠ {t("g5.attr.changeNote")}</span>
                            ) : (
                              "—"
                            )}
                          </td>
                        </tr>
                      );
                    });
                  })()}
                </tbody>
              </table>
            </div>
          </div>

          {/* 已跳过日期列表 */}
          {summary.skipped_days && summary.skipped_days.length > 0 && (
            <div className="pt-panel" style={{ padding: 16, fontSize: 12, color: "var(--pt-muted-foreground)" }}>
              {t("g5.skippedDays")}:{" "}
              <span style={{ fontWeight: 600, color: "var(--pt-foreground)" }}>{summary.skipped_days.join(", ")}</span>
            </div>
          )}
        </>
      )}

      {!summary && !resultError && replayId && !loadingSummary && (
        <div className="pt-panel" style={{ padding: 16, fontSize: 12, color: "var(--pt-muted-foreground)" }}>
          {t("g5.running")}
        </div>
      )}
      </InspectionDrawer>
    </div>
  );
};

const sevAttrStyle = (sev: string): React.CSSProperties => {
  if (sev === "P0") return { background: "rgba(239,68,68,0.16)", color: "#fca5a5", borderColor: "rgba(248,113,113,0.5)" };
  if (sev === "P1") return { background: "rgba(249,115,22,0.16)", color: "#fdba74", borderColor: "rgba(249,115,22,0.45)" };
  if (sev === "P2") return { background: "rgba(245,158,11,0.16)", color: "#fcd34d", borderColor: "rgba(245,158,11,0.45)" };
  return { background: "rgba(59,130,246,0.16)", color: "#93c5fd", borderColor: "rgba(96,165,250,0.45)" };
};

const StatCard: React.FC<{ label: string; value: string; positive?: boolean; invertGood?: boolean }> = ({ label, value, positive = true, invertGood = false }) => {
  const ok = invertGood ? positive : positive;
  return (
    <div
      style={{
        padding: 12,
        borderRadius: 10,
        border: `1px solid ${ok ? "rgba(52,211,153,0.4)" : "rgba(248,113,113,0.45)"}`,
        background: ok ? "rgba(16,185,129,0.10)" : "rgba(239,68,68,0.10)",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <div style={{ fontSize: 12, color: ok ? "#86efac" : "#fca5a5", fontWeight: 500 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: ok ? "#86efac" : "#fca5a5" }}>{value}</div>
    </div>
  );
};

const G7OperationsPanel: React.FC<{
  portfolioId: number;
  showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void;
  onOverlayChange?: (open:boolean)=>void;
  visible?:boolean;
}> = ({ portfolioId, showToast, onOverlayChange,visible=true }) => {
  const [status, setStatus] = useState<G7OperationalStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [readError,setReadError]=useState<string|null>(null);
  const readRequest=useRef(0);
  const readScope=useRef(portfolioId);readScope.current=portfolioId;
  const [actionLoading, setActionLoading] = useState<"start" | "rollback" | null>(null);
  const [rolloutReview, setRolloutReview] = useState<Awaited<ReturnType<typeof api.getG6Readiness>> | null>(null);
  useEffect(()=>{onOverlayChange?.(visible&&Boolean(rolloutReview));return()=>onOverlayChange?.(false);},[visible,rolloutReview,onOverlayChange]);
  const [rolloutNote, setRolloutNote] = useState("");
  const [rolloutAck, setRolloutAck] = useState(false);
  useEffect(()=>{setRolloutReview(null);setRolloutNote("");setRolloutAck(false);},[portfolioId]);

  const load = useCallback(async () => {
    const token=++readRequest.current;
    setLoading(true);setReadError(null);
    try {
      const value=await api.getG7OperationalStatus(portfolioId);
      if(token!==readRequest.current||readScope.current!==portfolioId)return;
      if(value?.portfolio_id!=null&&value.portfolio_id!==portfolioId)throw new Error("运行状态不属于当前组合，请重新读取。");
      setStatus(value);
    } catch (err: any) {
      if(token===readRequest.current&&readScope.current===portfolioId){setStatus(null);setReadError(err?.message||"引擎运行状态读取失败，请重试。");}
    } finally {
      if(token===readRequest.current&&readScope.current===portfolioId)setLoading(false);
    }
  }, [portfolioId]);

  useEffect(() => {setStatus(null);void load();return()=>{readRequest.current++;};}, [load]);

  const runG6Action = useCallback(async (action: "start" | "rollback") => {
    if (action === "start" && (!rolloutReview?.eligible || !rolloutAck || rolloutNote.trim().length < 10)) return;
    if (action === "rollback" && !window.confirm(t("governance.g7.rollbackConfirm"))) return;
    setActionLoading(action);
    try {
      const result = action === "start"
        ? await api.startG6Rollout(portfolioId, { expected_g5_report_id: rolloutReview?.g5_report_id ?? undefined, expected_g5_report_hash: rolloutReview?.g5_report_hash ?? undefined, expected_source_mode: rolloutReview?.current_source_mode ?? undefined, review_note: rolloutNote.trim() })
        : await api.rollbackG6Rollout(portfolioId, { operator_id: "ui:g6", reason: "治理页人工操作" });
      showToast("success", `${action === "start" ? t("governance.g7.startSuccess") : t("governance.g7.rollbackSuccess")} (${result.status}, audit=${result.audit_event_id ?? "-"})`);
      setRolloutReview(null); setRolloutAck(false);
      await load();
    } catch (err: any) {
      showToast("error", err?.message || String(err));
    } finally {
      setActionLoading(null);
    }
  }, [load, portfolioId, showToast, rolloutReview, rolloutNote, rolloutAck]);

  const flagLabel = (key: string) => {
    if (key === "partial") return t("governance.g7.status.partial");
    if (key === "pending_confirmation") return t("governance.g7.status.pending_confirmation");
    if (key === "pending_review") return t("governance.g7.status.pending_review");
    return t("governance.g7.status.other");
  };

  if (!status) {
    return <div className="pt-panel" style={{ padding: 16 }}>{readError?<div role="alert"><strong>引擎运行状态暂不可用</strong><p>{readError}</p></div>:loading?t("loading"):t("governance.g7.empty")}<Button loading={loading} onClick={()=>void load()}>重新读取引擎运行状态</Button></div>;
  }

  const flagEntries = Object.entries(status.pending_orders_by_status || {});
  const flagTone = (ok: boolean) => ok ? "#86efac" : "#fca5a5";
  const flagBg = (ok: boolean) => (ok ? "rgba(16,185,129,0.12)" : "rgba(239,68,68,0.12)");

  return (
    <div data-testid="g7-operational-status" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Modal open={visible&&Boolean(rolloutReview)} zIndex={1600} title="本人审核引擎切换材料" footer={null} onCancel={()=>{if(!actionLoading)setRolloutReview(null);}}><div className="pi-workbench">{rolloutReview && <section className="pi-callout pi-warning"><p>当前来源 {rolloutReview.current_source_mode ?? "未知"} · 双跑报告 {rolloutReview.g5_report_id ?? "未保留"}</p><small>冻结报告校验值 {rolloutReview.g5_report_hash ?? "缺失，不能确认"}</small>{!rolloutReview.eligible && <div role="alert">当前准入尚未通过，不能提交切换。{rolloutReview.blockers?.map((reason,i) => <p key={i}>{reason}</p>)}</div>}<p>仅切换当前组合的新执行来源；验收通过不会自动应用，独立运行门禁继续校验。</p><Input.TextArea aria-label="引擎切换审核理由" value={rolloutNote} onChange={e => setRolloutNote(e.target.value)} placeholder="说明已核实的报告与本次切换理由（至少10字）"/><Checkbox checked={rolloutAck} onChange={e => setRolloutAck(e.target.checked)}>我已核实报告、当前来源与本次切换范围</Checkbox><div><Button onClick={() => setRolloutReview(null)}>取消本次切换</Button><Button mutation type="primary" disabled={!rolloutReview.eligible || !rolloutAck || rolloutNote.trim().length < 10 || !rolloutReview.g5_report_id || !rolloutReview.g5_report_hash || !rolloutReview.current_source_mode} loading={actionLoading === "start"} onClick={() => void runG6Action("start")}>确认切换当前组合引擎</Button></div></section>}</div></Modal>
      <div className="pt-panel" style={{ padding: 16 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
          <div>
            <h3 style={{ margin: 0, fontSize: 16 }}>{t("governance.g7.title")}</h3>
            <div style={{ marginTop: 4, color: "var(--pt-muted-foreground)", fontSize: 12 }}>{t("governance.g7.subtitle")}</div>
          </div>
          <Button icon={<RefreshCw size={14} />} onClick={() => void load()} loading={loading}>
            {t("common.refresh")}
          </Button>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {(() => {
              const g6Ready = Boolean(status?.ready_for_expansion);
              const isDisabled = Boolean(actionLoading) || !g6Ready;
              const blockerReasons = !g6Ready ? (status?.operational_blockers ?? []).slice(0, 3) : [];
              const tooltip = !g6Ready
                ? `无法启动 G6 灰度，需先解决以下问题：${blockerReasons.join("；") || "条件未满足"}`
                : "";
              return (
                <Tooltip title={isDisabled ? tooltip : undefined} placement="bottom">
                  <Button
                    type="primary"
                    icon={<PlayCircle size={14} />}
                    loading={actionLoading === "start"}
                    disabled={isDisabled}
                    onClick={() => {
                      if (isDisabled && !actionLoading) {
                        showToast(
                          "error",
                          !g6Ready
                            ? `G6 灰度启动条件未满足：${blockerReasons.join("；") || "组合尚未就绪"}`
                            : t("loading"),
                        );
                        return;
                      }
                      void api.getG6Readiness(portfolioId).then(value => { setRolloutReview(value); setRolloutAck(false); }).catch(err => showToast("error", err.message));
                    }}
                  >
                    {t("governance.g7.startG6")}
                  </Button>
                </Tooltip>
              );
            })()}
            <Button mutation
              danger
              icon={<ShieldCheck size={14} />}
              onClick={() => void runG6Action("rollback")}
              loading={actionLoading === "rollback"}
            >
              {t("governance.g7.rollbackG6")}
            </Button>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10, marginTop: 16 }}>
          {[
            ["governance.g7.readyForExpansion", status.ready_for_expansion],
            ["governance.g7.newBuysStopped", status.new_buys_stopped],
            ["governance.g7.canResume", status.can_resume],
          ].map(([label, value]) => {
            const ok = label === "governance.g7.newBuysStopped" ? Boolean(value) : Boolean(value);
            return <div key={String(label)} style={{ padding: 12, borderRadius: 8, background: flagBg(ok), color: flagTone(ok) }}>
              <div style={{ fontSize: 12, fontWeight: 600 }}>{t(String(label))}</div>
              <div style={{ marginTop: 4, fontSize: 16, fontWeight: 700 }}>{ok ? t("yes") : t("no")}</div>
            </div>;
          })}
        </div>
      </div>

      <div className="pt-panel" style={{ padding: 16 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 }}>
          <StatCard label={t("governance.g7.pendingOrders")} value={String(status.pending_order_count)} positive={status.pending_order_count === 0} />
          <StatCard label={t("governance.g7.activeAlerts")} value={String(status.active_alert_count)} positive={status.active_alert_count === 0} />
          <StatCard label={t("governance.g7.g5Report")} value={status.g5_report_id == null ? t("governance.g7.notArchived") : `#${status.g5_report_id}`} positive={status.g5_report_id != null} />
        </div>
        {flagEntries.length > 0 && <div style={{ marginTop: 14, display: "flex", flexWrap: "wrap", gap: 8 }}>
          {flagEntries.map(([key, value]) => <span key={key} style={{ padding: "4px 10px", borderRadius: 999, background: "rgba(249,115,22,0.14)", border: "1px solid rgba(249,115,22,0.4)", color: "#fdba74", fontSize: 12 }}>{flagLabel(key)}：{value}</span>)}
        </div>}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16 }}>
        <G7BlockerList title={t("governance.g7.blockers")} items={status.operational_blockers} />
        <G7BlockerList title={t("governance.g7.resumeBlockers")} items={status.resume_blockers} />
      </div>
    </div>
  );
};

const G7BlockerList: React.FC<{ title: string; items: string[] }> = ({ title, items }) => (
  <div className="pt-panel" style={{ padding: 16 }}>
    <h4 style={{ margin: "0 0 10px", fontSize: 14 }}>{title}</h4>
    {items.length === 0 ? <div style={{ color: "var(--pt-muted-foreground)", fontSize: 12 }}>{t("governance.g7.none")}</div> : (
      <ul style={{ margin: 0, paddingLeft: 18, color: "#fca5a5", fontSize: 12 }}>{items.map((item, i) => <li key={`${item}-${i}`}>{item}</li>)}</ul>
    )}
  </div>
);

/* -------------------------------------------------------------------------- */
/* 主组件                                                                      */
/* -------------------------------------------------------------------------- */

export interface PortfolioGovernanceTabProps {
  portfolioId: number;
  /** 由父路由(SUB_TABS navigate)回调跳转其他主 Tab（用于对账 → 修复 → 返回等） */
  onNavigate?: (tab: string) => void;
  /** 来自主壳的治理深链，requestId 允许重复导航到同一目标。 */
  navigation?: { tab: GovernanceTabTarget; requestId: number };
  /** 写操作完成后按组合 ID 同步主壳状态与权限。 */
  onGovernanceChanged?: (portfolioId: number) => void | Promise<void>;
}

const LegacyPortfolioGovernanceTab: React.FC<PortfolioGovernanceTabProps> = ({ portfolioId, onNavigate, navigation, onGovernanceChanged }) => {
  const { showToast } = useApp();
  // P1：默认落地「待办总览」；旧 operations 深链归一到 state。
  const [sub, setSubRaw] = useState<GovSubTabKey>(() => normalizeGovSubTab(navigation?.tab ?? "overview"));
  const setSub = useCallback((k: GovSubTabKey) => setSubRaw(normalizeGovSubTab(k)), []);
  useEffect(() => {
    if (navigation) setSubRaw(normalizeGovSubTab(navigation.tab));
  }, [navigation]);
  const [helpOpen, setHelpOpen] = useState(false);

  const onNavigateSafe = useCallback(
    (tab: string) => {
      const target = resolvePortfolioNavigation(tab);
      if (target?.governanceTab) {
        setSub(target.governanceTab);
        return;
      }
      if (onNavigate) onNavigate(tab);
    },
    [onNavigate, setSub],
  );

  return (
    <div style={{ padding: "16px 24px 32px 24px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4, flexWrap: "wrap", gap: 10 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 18, display: "flex", alignItems: "center", gap: 8 }}>
            <Compass size={18} color="var(--pt-primary)" />
            {t("portfolioTrading.subtab.governance")}
          </h2>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--pt-muted-foreground)", marginTop: 4 }}>
            <span>{t("governance.pageSubtitle")}</span>
            <button
              type="button"
              onClick={() => setHelpOpen(true)}
              style={{ display: "inline-flex", alignItems: "center", gap: 4, background: "none", border: "none", color: "var(--pt-primary)", cursor: "pointer", fontSize: 12, padding: 0 }}
            >
              <HelpCircle size={13} /> {t("governance.helpEntry")}
            </button>
          </div>
        </div>
      </div>

      <SubTabNav active={sub} onChange={setSub} />

      {sub === "overview" && (
        <GovernanceOverviewPanel portfolioId={portfolioId} onNavigate={onNavigateSafe} onGovernanceChanged={onGovernanceChanged} />
      )}
      {(sub === "state" || sub === "operations") && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <StatePanel portfolioId={portfolioId} showToast={showToast} onGovernanceChanged={onGovernanceChanged} />
          {/* 上线前灰度运维属高级信息：默认折叠，避免 G5/G6 等内部代号占据日常视图 */}
          <details
            className="pt-panel"
            style={{ padding: 0, overflow: "hidden" }}
            data-testid="g7-ops-disclosure"
          >
            <summary
              style={{
                cursor: "pointer",
                padding: "12px 16px",
                fontSize: 13,
                fontWeight: 600,
                color: "var(--pt-foreground)",
                listStyle: "none",
                display: "flex",
                alignItems: "center",
                gap: 8,
              }}
            >
              <PlayCircle size={15} color="var(--pt-primary)" />
              {t("governance.g7.collapsibleTitle")}
              <span style={{ fontSize: 11, fontWeight: 400, color: "var(--pt-muted-foreground)" }}>
                {t("governance.g7.collapsibleHint")}
              </span>
            </summary>
            <div style={{ padding: "0 16px 16px" }}>
              <G7OperationsPanel portfolioId={portfolioId} showToast={showToast} />
            </div>
          </details>
        </div>
      )}
      {sub === "reconciliation" && <ReconciliationPanel portfolioId={portfolioId} showToast={showToast} onGovernanceChanged={onGovernanceChanged} />}
      {sub === "audit" && <AuditPanel portfolioId={portfolioId} showToast={showToast} />}
      {sub === "g5" && <G5Panel portfolioId={portfolioId} showToast={showToast} />}

      {/* 帮助：原常驻说明书的知识移入此处，默认不占首屏；技术契约放进折叠的技术区 */}
      <Modal
        open={helpOpen}
        title={t("governance.helpTitle")}
        onCancel={() => setHelpOpen(false)}
        onOk={() => setHelpOpen(false)}
        cancelButtonProps={{ style: { display: "none" } }}
        okText={t("ok")}
        width={720}
      >
        <div style={{ fontSize: 13, lineHeight: 1.9, color: "#e2e8f0", display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <b style={{ color: "#6366f1" }}>{t("governance.help.what.title")}</b>
            <div>{t("governance.help.what.body")}</div>
          </div>
          <div>
            <b style={{ color: "#10b981" }}>{t("governance.help.scenarios.title")}</b>
            <div>{t("governance.help.scenarios.s1")}</div>
            <div>{t("governance.help.scenarios.s2")}</div>
            <div>{t("governance.help.scenarios.s3")}</div>
          </div>
          <div>
            <b style={{ color: "#d97706" }}>{t("governance.help.flow.title")}</b>
            <div>{t("governance.help.flow.normal")}</div>
            <div>{t("governance.help.flow.auto")}</div>
            <div>{t("governance.help.flow.manual")}</div>
          </div>
          <details style={{ borderTop: "1px dashed #334155", paddingTop: 8 }}>
            <summary style={{ cursor: "pointer", color: "#94a3b8", fontSize: 12 }}>{t("governance.help.tech")}</summary>
            <div style={{ color: "#94a3b8", fontSize: 12, marginTop: 6 }}>
              <div>FR-P0-10：9 状态状态机（HG1 门禁权限矩阵）</div>
              <div>FR-P1-8a：治理端接口（查状态/状态转移/对账确认/审计列表）</div>
              <div>FR-P1-2：终端锁（避免多终端重复触发自动交易）</div>
              <div>G5 dual-run (10 trading days)：模拟 vs 生产双跑 10 个交易日，通过后才可灰度放量（G6）</div>
            </div>
          </details>
        </div>
      </Modal>
    </div>
  );
};

const PortfolioGovernanceTab: React.FC<PortfolioGovernanceTabProps> = props => {
  const { showToast } = useApp();
  return <PortfolioInspectionWorkbench {...props}
    renderDualRun={(onOverlayChange,visible) => <G5Panel portfolioId={props.portfolioId} showToast={showToast} onOverlayChange={onOverlayChange} visible={visible}/> }
    renderOperations={(onOverlayChange,visible) => <G7OperationsPanel portfolioId={props.portfolioId} showToast={showToast} onOverlayChange={onOverlayChange} visible={visible}/> }
  />;
};

export default PortfolioGovernanceTab;
export { StatusBadge, G5_BUSINESS_ACTIONS, G5_ATTR_CATEGORIES };
