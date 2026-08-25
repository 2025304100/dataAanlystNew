import React, { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, History, Lock, PlayCircle, RefreshCw, ShieldCheck, X } from "lucide-react";
import { Tooltip } from "antd";
import dayjs from "dayjs";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import type {
  AuditEvent,
  AuditEventFilter,
  AuditEventPageResponse,
  AuditEventSeverity,
  ConfirmReconciliationRequest,
  G5AttributionCategory,
  G5BusinessAction,
  G5DualRunLaunchRequest,
  G5DualRunSummaryResponse,
  G7OperationalStatus,
  PortfolioStatePermissions,
  PortfolioStatus,
  PortfolioStatusResponse,
  ReconciliationDiffItem,
  ReconciliationResponse,
  StateTransitionRequest,
} from "../../types";

/* ========================================================================== */
/* PortfolioGovernanceTab                                                     */
/*                                                                            */
/* 组合工作台第 5 个主 Tab「治理」(接 overview/members/strategy/backtest)。   */
/* 内含 4 个子 Tab:                                                           */
/*   state          → 9 状态彩色徽章 + ADMIN_PAUSED 红色刹车条 + 9×4 允许矩阵  */
/*                    + allowed_transitions 驱动状态转移 (单人确认)            */
/*   reconciliation → 对账守恒 10 列明细 + zeroSumCheck + force_skip 红警告    */
/*   audit          → 审计事件 5 过滤 + 分页 + correlation_id + 展开 attributes */
/*   g5             → G5 双跑启动(连续10交易日校验) + 6×6 混淆矩阵 +            */
/*                    eligible_g6 红绿徽章 + 异步任务行终态锁 disabled          */
/*                                                                            */
/* 契约：FE-2 的 7 个 API client 函数 + FE-3 的 81 条中英 i18n。              */
/* ========================================================================== */

type GovSubTabKey = "state" | "operations" | "reconciliation" | "audit" | "g5";

const G5_BUSINESS_ACTIONS: G5BusinessAction[] = [
  "BUY",
  "SELL_LIMITED_RISK",
  "HOLDS",
  "REJECTED_OR_BLOCKED",
  "RISK_CLOSED",
  "NO_ACTION_OR_ARCHIVED",
];

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
const STATUS_STYLES: Record<PortfolioStatus, { bg: string; border: string; fg: string; dot: string }> = {
  PENDING_INITIAL_REVIEW: { bg: "#fef3c7", border: "#f59e0b", fg: "#7c2d12", dot: "#f59e0b" },
  READY:                    { bg: "#ecfdf5", border: "#10b981", fg: "#065f46", dot: "#10b981" },
  RUNNING_AUTO_SIMULATION:  { bg: "#eff6ff", border: "#3b82f6", fg: "#1e3a8a", dot: "#3b82f6" },
  RUNNING_BACKTEST:         { bg: "#eef2ff", border: "#6366f1", fg: "#3730a3", dot: "#6366f1" },
  DATA_INCOMPLETE_PAUSED:   { bg: "#fef9c3", border: "#eab308", fg: "#713f12", dot: "#eab308" },
  RECONCILIATION_BLOCKED:   { bg: "#fee2e2", border: "#ef4444", fg: "#7f1d1d", dot: "#ef4444" },
  MODEL_INACTIVE:           { bg: "#f3f4f6", border: "#6b7280", fg: "#1f2937", dot: "#6b7280" },
  SCORE_STALE:              { bg: "#fff7ed", border: "#f97316", fg: "#7c2d12", dot: "#f97316" },
  INTERRUPTED:              { bg: "#fae8ff", border: "#d946ef", fg: "#701a75", dot: "#d946ef" },
  ADMIN_PAUSED:             { bg: "#fef2f2", border: "#dc2626", fg: "#7f1d1d", dot: "#dc2626" },
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

const SubTabNav: React.FC<{ active: GovSubTabKey; onChange: (k: GovSubTabKey) => void }> = ({ active, onChange }) => {
  const tabs: { key: GovSubTabKey; label: string; icon: React.ReactNode }[] = [
    { key: "state", label: t("portfolioTrading.governance.subtab.state"), icon: <ShieldCheck size={14} /> },
    { key: "operations", label: t("portfolioTrading.governance.subtab.operations"), icon: <AlertTriangle size={14} /> },
    { key: "reconciliation", label: t("portfolioTrading.governance.subtab.reconciliation"), icon: <CheckCircle2 size={14} /> },
    { key: "audit", label: t("portfolioTrading.governance.subtab.audit"), icon: <History size={14} /> },
    { key: "g5", label: t("portfolioTrading.governance.subtab.g5DualRun"), icon: <PlayCircle size={14} /> },
  ];
  return (
    <div className="sub-tabs" style={{ padding: "8px 0 0 0", marginBottom: 16 }}>
      {tabs.map((tab) => (
        <button
          key={tab.key}
          type="button"
          className={`sub-tab ${active === tab.key ? "active" : ""}`}
          onClick={() => onChange(tab.key)}
          style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
        >
          {tab.icon}
          {tab.label}
        </button>
      ))}
    </div>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab 1：系统状态（Status / Matrix / Transition）                        */
/* -------------------------------------------------------------------------- */

const StatePanel: React.FC<{ portfolioId: number; showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void }> = ({
  portfolioId,
  showToast,
}) => {
  const [loading, setLoading] = useState(false);
  const [statusResp, setStatusResp] = useState<PortfolioStatusResponse | null>(null);
  const [transitionOpen, setTransitionOpen] = useState(false);
  const [targetState, setTargetState] = useState<PortfolioStatus | null>(null);
  const [transitionReason, setTransitionReason] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [submitting, setSubmitting] = useState(false);

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
      } else if (resp.noop_detected === true) {
        showToast("info", t("governance.transition.noop"));
        setTransitionOpen(false);
      } else {
        const resultLabel = resp.transition_applied ? "APPLIED" : "NOOP";
        showToast("success", t("governance.transition.applied") + ` (${resultLabel}, audit=${resp.audit_event_id ?? "—"})`);
        setTransitionOpen(false);
        void fetchStatus();
      }
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
            background: "linear-gradient(90deg, #fee2e2, #fff1f2)",
            border: "1px solid #ef4444",
            color: "#7f1d1d",
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
              <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                last_decision_trade_date: <b>{statusResp.last_decision_trade_date}</b>
              </span>
            )}
            {statusResp?.last_reconciled_trade_date && (
              <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                last_reconciled_trade_date: <b>{statusResp.last_reconciled_trade_date}</b>
              </span>
            )}
            {typeof statusResp?.is_auto_simulation_eligible === "boolean" && (
              <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                is_auto_simulation_eligible:{" "}
                <b style={{ color: statusResp.is_auto_simulation_eligible ? "#10b981" : "#ef4444" }}>
                  {String(statusResp.is_auto_simulation_eligible)}
                </b>
              </span>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button type="button" className="pt-btn-ghost" onClick={fetchStatus} disabled={loading}>
              <RefreshCw size={14} /> {loading ? "…" : t("common.refresh")}
            </button>
            <Tooltip
              title={
                allowedTargets.length === 0 ? t("governance.transition.noTargets") : ""
              }
              placement="top"
            >
              <button
                type="button"
                className="pt-btn-primary"
                onClick={handleOpenTransition}
                disabled={allowedTargets.length === 0 || !current}
                style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
              >
                <Lock size={14} /> {t("governance.transition.applyBtn")}
              </button>
            </Tooltip>
          </div>
        </div>

        {/* allowed_transitions 清单 */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>{t("governance.transition.allowedTargets")}:</span>
          {allowedTargets.length === 0 && <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>∅</span>}
          {allowedTargets.map((ts) => (
            <StatusBadge key={ts} status={ts} size="sm" showDesc={false} />
          ))}
        </div>
      </div>

      {/* 9×4 允许矩阵表 */}
      <div className="pt-panel" style={{ padding: 16 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
          <h4 style={{ margin: 0, fontSize: 14 }}>{t("governance.matrix.title")}</h4>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 720 }}>
            <thead>
              <tr style={{ background: "var(--pt-surface-1)" }}>
                <th style={thStyle("left")}>PortfolioStatus</th>
                <th style={thStyle()}>{t("governance.matrix.colNewBuys")}</th>
                <th style={thStyle()}>{t("governance.matrix.colRiskExits")}</th>
                <th style={thStyle()}>{t("governance.matrix.colAutoRecovery")}</th>
                <th style={thStyle()}>{t("governance.matrix.colManualAck")}</th>
              </tr>
            </thead>
            <tbody>
              {(Object.keys(ALLOWANCE_MATRIX) as PortfolioStatus[]).map((k) => {
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
                      <StatusBadge status={k} size="sm" showDesc={false} />
                    </td>
                    <td style={tdStyle()}>{allowBadge(row.allow_new_buys)}</td>
                    <td style={tdStyle()}>{allowBadge(row.allow_risk_exits)}</td>
                    <td style={tdStyle()}>{allowBadge(row.allow_auto_recovery)}</td>
                    <td style={tdStyle()}>{row.requires_manual_ack ? allowBadge(true, "ack") : allowBadge(false)}</td>
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
  borderBottom: "1px solid var(--pt-border-subtle)",
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
          padding: "2px 10px",
          borderRadius: 999,
          border: "1px solid #f59e0b",
          background: "#fffbeb",
          color: "#78350f",
          fontSize: 12,
          fontWeight: 600,
        }}
      >
        {allow ? "REQUIRED" : "—"}
      </span>
    );
  }
  return allow ? (
    <span style={{ color: "#059669", fontSize: 12, fontWeight: 600 }}>✓ {t("governance.matrix.allowTrue")}</span>
  ) : (
    <span style={{ color: "#b91c1c", fontSize: 12, fontWeight: 500, opacity: 0.9 }}>✗ {t("governance.matrix.allowFalse")}</span>
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
  if (!p.open) return null;
  return (
    <>
      <div
        aria-hidden
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 1000,
          background: "rgba(0,0,0,0.55)",
          backdropFilter: "blur(3px)",
          WebkitBackdropFilter: "blur(3px)",
        }}
        onClick={p.onCancel}
      />
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
          className="pt-cpm-panel"
          style={{
            pointerEvents: "auto",
            width: 520,
            maxWidth: "100%",
            maxHeight: "calc(100vh - 64px)",
            overflow: "auto",
            padding: 20,
            borderRadius: 14,
            background: "var(--pt-surface-1)",
            color: "var(--pt-foreground)",
            boxShadow: "0 20px 60px rgba(0,0,0,0.35)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
            <h3 style={{ margin: 0, fontSize: 16 }}>{t("governance.transitionTitle")}</h3>
            <button type="button" className="pt-btn-icon" onClick={p.onCancel} aria-label="close">
              <X size={16} />
            </button>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)", minWidth: 88 }}>
                {t("governance.transition.current")}
              </span>
              {p.currentState && <StatusBadge status={p.currentState} size="sm" showDesc={false} />}
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <label style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>
                {t("governance.transition.target")}{" "}
                {p.targetState && p.currentState && p.targetState === p.currentState && (
                  <span style={{ color: "#d97706", marginLeft: 6, fontSize: 12 }}>{t("governance.transition.noop")}</span>
                )}
              </label>
              <select
                className="pt-select"
                value={p.targetState ?? ""}
                disabled={p.allowedTargets.length === 0}
                onChange={(e) => p.onTargetChange((e.target.value as PortfolioStatus) || null)}
                style={{ padding: "8px 10px" }}
              >
                <option value="">—</option>
                {p.allowedTargets.map((ts) => (
                  <option key={ts} value={ts}>
                    {ts} — {t(`portfolioStatus.${ts}` as const)}
                  </option>
                ))}
              </select>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <label style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>
                {t("governance.transition.triggerReason")}
              </label>
              <input
                className="pt-input"
                value={p.reason}
                onChange={(e) => p.onReasonChange(e.target.value)}
                placeholder="e.g. 一键刹车 / 解除对账阻断 / 心跳恢复"
                style={{ padding: "8px 10px" }}
              />
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <label style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>
                {t("governance.transition.reviewNote")}
                {p.noteRequired && <span style={{ color: "#dc2626", marginLeft: 6 }}>*</span>}
                {p.noteTooShort && <span style={{ color: "#dc2626", marginLeft: 6, fontSize: 12 }}>{t("governance.transition.noteMin10")}</span>}
              </label>
              <textarea
                className="pt-input"
                rows={4}
                value={p.reviewNote}
                onChange={(e) => p.onReviewNoteChange(e.target.value)}
                placeholder="至少 10 字符；说明变更原因、受影响范围、责任人。"
                style={{ padding: "8px 10px", resize: "vertical" }}
              />
            </div>
          </div>

          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 18 }}>
            <button type="button" className="pt-btn-ghost" onClick={p.onCancel} disabled={p.submitting}>
              Cancel
            </button>
            <button
              type="button"
              className="pt-btn-primary"
              onClick={p.onConfirm}
              disabled={p.submitting || !p.targetState || p.noteTooShort}
            >
              {p.submitting ? "…" : t("governance.transition.applyBtn")}
            </button>
          </div>
        </div>
      </div>
    </>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab 2：对账守恒 ReconciliationPanel                                    */
/* -------------------------------------------------------------------------- */

const ReconciliationPanel: React.FC<{
  portfolioId: number;
  showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void;
}> = ({ portfolioId, showToast }) => {
  const [loading, setLoading] = useState(false);
  const [resp, setResp] = useState<ReconciliationResponse | null>(null);
  const [ackChecked, setAckChecked] = useState(false);
  const [forceSkipChecked, setForceSkipChecked] = useState(false);
  const [rerunFirst, setRerunFirst] = useState(true);
  const [reviewNote, setReviewNote] = useState("");
  const [confirming, setConfirming] = useState(false);

  const runReconcile = useCallback(async () => {
    if (!portfolioId) return;
    setLoading(true);
    try {
      const r = await api.triggerPortfolioReconcile(portfolioId);
      setResp(r);
      if (r.zero_sum_check_passed) showToast("success", t("governance.reconcile.zeroSum"));
      else showToast("info", t("governance.reconcile.zeroSumFailed"));
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
      force_skip: forceSkipChecked || undefined,
      force_rerun_before: rerunFirst || undefined,
    };
    setConfirming(true);
    try {
      const out = await api.confirmPortfolioReconciliation(portfolioId, payload);
      const ok = out.portfolio_now_ready === true;
      if (ok) {
        const statusAlias = out.acknowledged_diffs_cleared ? "RECONCILED_CLEARED" : "ACKNOWLEDGED";
        showToast("success", `${t("governance.reconcile.confirmed")} (status=${statusAlias}, correlation=${out.correlation_id ?? "—"})`);
        setAckChecked(false);
        setForceSkipChecked(false);
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
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* 顶部卡：最后对账 + 执行按钮 + 守恒通过 */}
      <div className="pt-panel" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>{t("governance.reconcile.last")}:</span>
            <b>{resp?.trade_date ?? resp?.last_reconciled_trade_date ?? "—"}</b>
            {resp?.zero_sum_check_passed != null && (
              resp.zero_sum_check_passed ? (
                <span style={{ color: "#059669", fontSize: 12, fontWeight: 600 }}>
                  ✓ {t("governance.reconcile.zeroSum")}
                </span>
              ) : (
                <span style={{ color: "#b91c1c", fontSize: 12, fontWeight: 600 }}>
                  ⚠ {t("governance.reconcile.zeroSumFailed")}
                </span>
              )
            )}
            {hasDiff && (
              <span style={{ color: "#b45309", fontSize: 12, fontWeight: 600 }}>{t("governance.reconcile.diffFound")}</span>
            )}
          </div>
          <button type="button" className="pt-btn-primary" onClick={runReconcile} disabled={loading}>
            <RefreshCw size={14} /> {loading ? "…" : t("governance.reconcile.btn")}
          </button>
        </div>

        {/* 10 列差异表 */}
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
                <tr><td colSpan={6} style={{ padding: 16, textAlign: "center", color: "var(--pt-muted-foreground)" }}>—</td></tr>
              )}
              {diffs.map((d, i) => {
                const dim = d.dimension;
                const dimLabel =
                  dim === "order_count" ? t("governance.reconcile.dim.orderCount") :
                  dim === "order_amount" ? t("governance.reconcile.dim.orderAmount") :
                  dim === "trade_count" ? t("governance.reconcile.dim.tradeCount") :
                  dim === "trade_amount" ? t("governance.reconcile.dim.tradeAmount") :
                  dim === "position_count" ? t("governance.reconcile.dim.positionCount") :
                  dim === "position_market_value" ? t("governance.reconcile.dim.positionValue") :
                  dim === "cash_balance" ? t("governance.reconcile.dim.cashBalance") :
                  dim === "evidence_items" ? t("governance.reconcile.dim.evidenceItems") :
                  dim === "evidence_hash" ? t("governance.reconcile.dim.evidenceHash") :
                  t("governance.reconcile.dim.unknown") + ` (${String(dim)})`;
                const expected = toNumber(d.expected_value);
                const actual = toNumber(d.actual_value);
                const diff = toNumber(d.diff_value);
                const rowBg = diff !== 0 ? "rgba(254,226,226,0.45)" : undefined;
                return (
                  <tr key={`${String(dim)}-${i}`} style={{ background: rowBg }}>
                    <td style={tdStyle("left")}>{dimLabel}</td>
                    <td style={tdStyle()}>{fmtNum(d.expected_value)}</td>
                    <td style={tdStyle()}>{fmtNum(d.actual_value)}</td>
                    <td style={tdStyle()}>—</td>
                    <td style={tdStyle()}>
                      <span style={{ color: diff > 0 ? "#15803d" : diff < 0 ? "#b91c1c" : undefined, fontWeight: 600 }}>
                        {diff > 0 ? "+" : ""}{fmtNum(diff)}
                      </span>
                    </td>
                    <td style={tdStyle("left")}>{(d.explain_note ?? (`${expected - actual === diff ? "" : ""}`.trim())) || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* 确认区：ack + force_skip(高危红警告) + rerun + review_note + 单人确认按钮 */}
        <div
          style={{
            padding: 14,
            borderRadius: 10,
            border: "1px solid var(--pt-border)",
            background: "var(--pt-surface-0)",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          <div style={{ fontSize: 14, fontWeight: 700 }}>{t("governance.reconcile.confirmTitle")}</div>

          <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
            <input type="checkbox" checked={ackChecked} onChange={(e) => setAckChecked(e.target.checked)} />
            {t("governance.reconcile.ackRequired")}
          </label>

          <label style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 13, color: "#7f1d1d" }}>
            <input
              type="checkbox"
              checked={forceSkipChecked}
              onChange={(e) => {
                setForceSkipChecked(e.target.checked);
                if (e.target.checked && rerunFirst) setRerunFirst(true);
              }}
              style={{ marginTop: 3 }}
            />
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontWeight: 600 }}>{t("governance.reconcile.forceSkip")}</span>
              <span style={{ fontSize: 12, background: "#fef2f2", padding: "4px 8px", borderRadius: 6 }}>
                {t("governance.reconcile.forceSkipWarn")}
              </span>
            </div>
          </label>

          <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
            <input type="checkbox" checked={rerunFirst} onChange={(e) => setRerunFirst(e.target.checked)} />
            {t("governance.reconcile.rerunFirst")}
          </label>

          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>
              {t("governance.transition.reviewNote")} <span style={{ color: "#dc2626" }}>*</span>
              {noteShort && <span style={{ color: "#dc2626", marginLeft: 6, fontSize: 12 }}>{t("governance.reconcile.reviewNoteMin10")}</span>}
            </label>
            <textarea
              className="pt-input"
              rows={3}
              value={reviewNote}
              onChange={(e) => setReviewNote(e.target.value)}
              placeholder="记录修复的具体问题与原因（至少 10 字符）。"
              style={{ padding: "8px 10px", resize: "vertical" }}
            />
          </div>

          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button
              type="button"
              className="pt-btn-primary"
              onClick={doConfirm}
              disabled={confirming || !canConfirm}
              style={forceSkipChecked ? { background: "#dc2626", borderColor: "#991b1b" } : undefined}
            >
              {confirming ? "…" : t("governance.reconcile.confirmBtn")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab 3：审计事件分页过滤                                                 */
/* -------------------------------------------------------------------------- */

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
      case "L3": return { background: "#fee2e2", color: "#7f1d1d", borderColor: "#ef4444" };
      case "L2": return { background: "#ffedd5", color: "#7c2d12", borderColor: "#f97316" };
      case "L1": return { background: "#fef3c7", color: "#78350f", borderColor: "#f59e0b" };
      case "WARNING": return { background: "#fefce8", color: "#713f12", borderColor: "#eab308" };
      case "INFO": default: return { background: "#eff6ff", color: "#1e3a8a", borderColor: "#3b82f6" };
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="pt-panel" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px,1fr))", gap: 10, alignItems: "flex-end" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.eventType")}</label>
            <input className="pt-input" value={eventType} onChange={(e) => setEventType(e.target.value)} placeholder="e.g. STATE_TRANSITION" style={{ padding: "6px 10px" }} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.severity")}</label>
            <select className="pt-select" value={severity} onChange={(e) => setSeverity((e.target.value || "") as any)} style={{ padding: "6px 10px" }}>
              <option value="">—</option>
              <option value="INFO">INFO</option>
              <option value="WARNING">WARNING</option>
              <option value="L1">L1</option>
              <option value="L2">L2</option>
              <option value="L3">L3</option>
            </select>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.startDate")}</label>
            <input type="date" className="pt-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} style={{ padding: "6px 10px" }} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.endDate")}</label>
            <input type="date" className="pt-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} style={{ padding: "6px 10px" }} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.query")}</label>
            <input className="pt-input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="keyword / correlation_id" style={{ padding: "6px 10px" }} />
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" className="pt-btn-primary" onClick={() => runQuery(1)} disabled={loading}>
              {t("governance.audit.filter.apply")}
            </button>
            <button
              type="button"
              className="pt-btn-ghost"
              onClick={() => {
                setEventType(""); setSeverity(""); setStartDate(""); setEndDate(""); setQuery("");
                setTimeout(() => runQuery(1), 0);
              }}
            >
              {t("governance.audit.filter.reset")}
            </button>
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
                const rowKey = ev.id ?? `${String(ev.event_type)}-${ev.created_at}-${String(Math.random())}`;
                const isOpen = expandedId === rowKey;
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
                    <tr>
                      <td style={tdStyle("left")}>{String(ev.id ?? "—")}</td>
                      <td style={tdStyle("left")}>{ev.created_at ?? "—"}</td>
                      <td style={tdStyle("left")}>{String(ev.event_type)}</td>
                      <td style={tdStyle()}>
                        <span
                          style={{
                            display: "inline-block",
                            padding: "2px 8px",
                            borderRadius: 6,
                            border: "1px solid",
                            fontSize: 11,
                            fontWeight: 600,
                            ...sevStyle(ev.severity),
                          }}
                        >
                          {ev.severity ?? "—"}
                        </span>
                      </td>
                      <td style={tdStyle("left")}>{ev.trigger_reason ?? "—"}</td>
                      <td style={tdStyle("left")}>
                        {ev.operated_by ? (
                          <span title={`operated_by_uid=${String(ev.operated_by)}`}>
                            uid=<b>{String(ev.operated_by)}</b>
                          </span>
                        ) : "—"}
                      </td>
                      <td style={tdStyle("left")}>{ev.reviewed_at ?? "—"}</td>
                      <td style={tdStyle("left")}>
                        {ev.from_state || ev.to_state ? (
                          <span>
                            {ev.from_state ? <StatusBadge status={ev.from_state as PortfolioStatus} size="sm" showDesc={false} /> : "∅"}
                            {" → "}
                            {ev.to_state ? <StatusBadge status={ev.to_state as PortfolioStatus} size="sm" showDesc={false} /> : "∅"}
                          </span>
                        ) : "—"}
                      </td>
                      <td style={tdStyle("left")}>
                        {ev.correlation_id ? <code style={{ fontSize: 11 }}>{ev.correlation_id}</code> : "—"}
                      </td>
                      <td style={tdStyle()}>
                        <button
                          type="button"
                          className="pt-btn-icon"
                          disabled={!attrsObj || Object.keys(attrsObj).length === 0}
                          onClick={() => setExpandedId(isOpen ? null : rowKey)}
                          title={t("governance.audit.col.expand")}
                        >
                          {isOpen ? <X size={14} /> : "···"}
                        </button>
                      </td>
                    </tr>
                    {isOpen && attrsObj && (
                      <tr>
                        <td colSpan={10} style={{ padding: "0 16px 10px 16px" }}>
                          <pre
                            style={{
                              margin: 0,
                              padding: 10,
                              background: "var(--pt-surface-0)",
                              border: "1px solid var(--pt-border-subtle)",
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
            <button
              type="button"
              className="pt-btn-ghost"
              disabled={page <= 1 || loading}
              onClick={() => runQuery(page - 1)}
            >
              ← Prev
            </button>
            <button
              type="button"
              className="pt-btn-ghost"
              disabled={page >= (resp?.page_count ?? 1) || loading}
              onClick={() => runQuery(page + 1)}
            >
              Next →
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

/* -------------------------------------------------------------------------- */
/* Sub Tab 4：G5 双跑对账                                                     */
/* -------------------------------------------------------------------------- */

const G5Panel: React.FC<{ portfolioId: number; showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void }> = ({
  portfolioId,
  showToast,
}) => {
  const MIN_TRADE_DAYS = 10;

  // Launch form
  const [startDate, setStartDate] = useState<string>(() => dayjs().subtract(3, "month").format("YYYY-MM-DD"));
  const [endDate, setEndDate] = useState<string>(() => dayjs().format("YYYY-MM-DD"));
  const [oldEngine, setOldEngine] = useState<string>("");
  const [newEngine, setNewEngine] = useState<string>("");
  const [launching, setLaunching] = useState(false);
  const [replayId, setReplayId] = useState<string | null>(null);
  const [summary, setSummary] = useState<G5DualRunSummaryResponse | null>(null);
  const [loadingSummary, setLoadingSummary] = useState(false);
  const [asyncTaskStatus, setAsyncTaskStatus] = useState<{ is_terminal_locked: boolean; lock_reason: string | null; status: string } | null>(null);

  // 简化版连续交易日估算：按工作日(周一~周五)粗略估算 (真实交易日需后端校验)
  const estimateWeekdays = (start: string, end: string): number => {
    if (!start || !end) return 0;
    const s = dayjs(start);
    const e = dayjs(end);
    if (!s.isValid() || !e.isValid() || e.isBefore(s)) return 0;
    let n = 0;
    let cur = s;
    while (cur.isBefore(e) || cur.isSame(e, "day")) {
      const d = cur.day();
      if (d !== 0 && d !== 6) n += 1;
      cur = cur.add(1, "day");
      if (n > 60) break; // 上限，防止死循环
    }
    return n;
  };
  const weekDays = estimateWeekdays(startDate, endDate);
  const daysOk = weekDays >= MIN_TRADE_DAYS;

  const doLaunch = async () => {
    if (!portfolioId) return;
    if (!daysOk) {
      showToast("error", template(t("g5.tradeDaysCheck") as string, { minDays: String(MIN_TRADE_DAYS) }) + ` (当前估算 ${weekDays} 个工作日)`);
      return;
    }
    if (!oldEngine.trim() || !newEngine.trim()) {
      showToast("error", "请填写新旧引擎版本号 / Please fill old & new engine versions.");
      return;
    }
    const payload: G5DualRunLaunchRequest = {
      start_date: startDate,
      end_date: endDate,
      old_engine_version: oldEngine.trim(),
      new_engine_version: newEngine.trim(),
    };
    setLaunching(true);
    try {
      const r = await api.launchG5DualRun(portfolioId, payload);
      setReplayId(r.replay_id);
      setAsyncTaskStatus({
        is_terminal_locked: r.is_terminal_locked === true,
        lock_reason: r.terminal_lock_reason ?? null,
        status: r.status ?? "SUBMITTED",
      });
      showToast("success", `${t("g5.launch")} → replay_id=${r.replay_id} (async_task=${r.async_task_id ?? "—"})`);
      // 立即拉一次，然后 5 秒轮询一次，最多 60 次
      let counter = 0;
      const tick = async () => {
        if (!r.replay_id) return;
        try {
          setLoadingSummary(true);
          const s = await api.queryG5DualRunResult(portfolioId, r.replay_id);
          setSummary(s);
          const locked = s.is_terminal_locked === true;
          setAsyncTaskStatus({
            is_terminal_locked: locked,
            lock_reason: s.terminal_lock_reason ?? null,
            status: s.status ?? "UNKNOWN",
          });
          if (!locked && counter < 60) {
            counter += 1;
            setTimeout(() => void tick(), 5000);
          }
        } catch {
          /* best-effort polling */
        } finally {
          setLoadingSummary(false);
        }
      };
      setTimeout(() => void tick(), 2500);
    } catch (err: any) {
      showToast("error", (err?.message || String(err)) + " (launchG5)");
    } finally {
      setLaunching(false);
    }
  };

  const doFetchResult = useCallback(async () => {
    if (!portfolioId || !replayId) return;
    setLoadingSummary(true);
    try {
      const s = await api.queryG5DualRunResult(portfolioId, replayId);
      setSummary(s);
      setAsyncTaskStatus({
        is_terminal_locked: s.is_terminal_locked === true,
        lock_reason: s.terminal_lock_reason ?? null,
        status: s.status ?? "UNKNOWN",
      });
    } catch (err: any) {
      showToast("error", (err?.message || String(err)) + " (queryG5)");
    } finally {
      setLoadingSummary(false);
    }
  }, [portfolioId, replayId, showToast]);

  const locked = asyncTaskStatus?.is_terminal_locked === true;
  const lockReason = asyncTaskStatus?.lock_reason ?? "T-LOCK";
  const eligible = summary?.g5_eligible_for_g6 === true;

  // 6×6 混淆矩阵 (取首日用于渲染，若无日级数据则显示空矩阵)
  const report = summary?.daily_reports?.[0] ?? null;
  const matrix = report?.action_confusion_matrix ?? {};

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* 启动表单 */}
      <div className="pt-panel" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
          <div>
            <h4 style={{ margin: 0, fontSize: 14 }}>{t("g5.title")}</h4>
            <p style={{ margin: "4px 0 0 0", fontSize: 12, color: "var(--pt-muted-foreground)", maxWidth: 900 }}>{t("g5.intro")}</p>
          </div>
          {summary != null && (
            eligible ? (
              <span
                style={{
                  padding: "6px 12px",
                  borderRadius: 999,
                  background: "#ecfdf5",
                  color: "#065f46",
                  border: "1px solid #10b981",
                  fontWeight: 700,
                  fontSize: 12,
                }}
              >
                {t("g5.eligible")}
              </span>
            ) : (
              <span
                style={{
                  padding: "6px 12px",
                  borderRadius: 999,
                  background: "#fef2f2",
                  color: "#7f1d1d",
                  border: "1px solid #ef4444",
                  fontWeight: 700,
                  fontSize: 12,
                }}
              >
                {t("g5.notEligible")}
              </span>
            )
          )}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px,1fr))", gap: 12, alignItems: "flex-end" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.startDate")}</label>
            <input type="date" className="pt-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} style={{ padding: "6px 10px" }} disabled={locked} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.endDate")}</label>
            <input type="date" className="pt-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} style={{ padding: "6px 10px" }} disabled={locked} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.oldEngine")}</label>
            <input className="pt-input" value={oldEngine} onChange={(e) => setOldEngine(e.target.value)} placeholder="e.g. v1.4.2" style={{ padding: "6px 10px" }} disabled={locked} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("g5.newEngine")}</label>
            <input className="pt-input" value={newEngine} onChange={(e) => setNewEngine(e.target.value)} placeholder="e.g. v2.0.0" style={{ padding: "6px 10px" }} disabled={locked} />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <button
              type="button"
              className="pt-btn-primary"
              onClick={doLaunch}
              disabled={launching || locked}
              style={locked ? { opacity: 0.6, cursor: "not-allowed" } : undefined}
            >
              <PlayCircle size={14} /> {launching ? "…" : t("g5.launch")}
            </button>
            <button type="button" className="pt-btn-ghost" onClick={doFetchResult} disabled={loadingSummary || !replayId}>
              <RefreshCw size={14} /> {loadingSummary ? "…" : "Query Result"}
            </button>
            {!daysOk && (
              <span style={{ fontSize: 12, color: "#dc2626", fontWeight: 600 }}>
                {template(t("g5.tradeDaysCheck") as string, { minDays: String(MIN_TRADE_DAYS) })}
                {" "}
                (当前估算: {weekDays})
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
                ? "Replay is still running; results will update on poll."
                : "Replay not started yet."
          }
        >
          <div
            style={{
              padding: "10px 12px",
              borderRadius: 10,
              border: "1px solid var(--pt-border)",
              background: locked ? "#f9fafb" : "var(--pt-surface-0)",
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

            <h5 style={{ margin: "0 0 10px 0", fontSize: 13 }}>{t("g5.matrix.title")}</h5>
            <div style={{ overflowX: "auto" }}>
              <table style={{ borderCollapse: "collapse", fontSize: 12, minWidth: 560 }}>
                <thead>
                  <tr style={{ background: "var(--pt-surface-1)" }}>
                    <th style={thStyle("left")}>{t("g5.matrix.oldHeader")}</th>
                    {G5_BUSINESS_ACTIONS.map((a) => (
                      <th key={a} style={thStyle("left")}>{t(`g5.action.${a}` as any)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {G5_BUSINESS_ACTIONS.map((oldAct) => (
                    <tr key={oldAct}>
                      <td style={tdStyle("left")}>{t(`g5.action.${oldAct}` as any)}</td>
                      {G5_BUSINESS_ACTIONS.map((newAct) => {
                        const count = (matrix as any)?.[oldAct]?.[newAct] ?? 0;
                        const isDiagonal = oldAct === newAct;
                        const warnBg = !isDiagonal && count > 0 ? "rgba(254,226,226,0.55)" : undefined;
                        return (
                          <td
                            key={`${oldAct}-${newAct}`}
                            style={{
                              padding: "6px 10px",
                              borderBottom: "1px solid var(--pt-border-subtle)",
                              textAlign: "center",
                              background: warnBg,
                              fontWeight: isDiagonal ? 600 : 500,
                              color: isDiagonal ? "#065f46" : count > 0 ? "#7f1d1d" : undefined,
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
              <span style={{ color: "#991b1b" }}>⚠ {t("g5.matrix.off")} = 非对角 (mismatch)</span>
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
                    for (const r of summary.daily_reports ?? []) {
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
                        <tr key={i} style={{ background: p0 ? "rgba(254,226,226,0.4)" : undefined }}>
                          <td style={tdStyle("left")}>
                            <span style={{ fontWeight: 600, color: p0 ? "#7f1d1d" : undefined }}>
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
                              <span style={{ color: "#dc2626", fontWeight: 600 }}>⚠ P0 REQUIRED: {t("g5.attr.changeNote")}</span>
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

      {!summary && replayId && !loadingSummary && (
        <div className="pt-panel" style={{ padding: 16, fontSize: 12, color: "var(--pt-muted-foreground)" }}>
          {t("g5.running")}
        </div>
      )}
    </div>
  );
};

const sevAttrStyle = (sev: string): React.CSSProperties => {
  if (sev === "P0") return { background: "#fee2e2", color: "#7f1d1d", borderColor: "#ef4444" };
  if (sev === "P1") return { background: "#ffedd5", color: "#7c2d12", borderColor: "#f97316" };
  if (sev === "P2") return { background: "#fef3c7", color: "#78350f", borderColor: "#f59e0b" };
  return { background: "#eff6ff", color: "#1e3a8a", borderColor: "#3b82f6" };
};

const StatCard: React.FC<{ label: string; value: string; positive?: boolean; invertGood?: boolean }> = ({ label, value, positive = true, invertGood = false }) => {
  const ok = invertGood ? positive : positive;
  return (
    <div
      style={{
        padding: 12,
        borderRadius: 10,
        border: `1px solid ${ok ? "#10b981" : "#ef4444"}`,
        background: ok ? "#ecfdf5" : "#fef2f2",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <div style={{ fontSize: 12, color: ok ? "#065f46" : "#7f1d1d", fontWeight: 500 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: ok ? "#065f46" : "#7f1d1d" }}>{value}</div>
    </div>
  );
};

const G7OperationsPanel: React.FC<{
  portfolioId: number;
  showToast: (t: "success" | "error" | "info", msg: React.ReactNode) => void;
}> = ({ portfolioId, showToast }) => {
  const [status, setStatus] = useState<G7OperationalStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<"start" | "rollback" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await api.getG7OperationalStatus(portfolioId));
    } catch (err: any) {
      showToast("error", err?.message || String(err));
    } finally {
      setLoading(false);
    }
  }, [portfolioId, showToast]);

  useEffect(() => { void load(); }, [load]);

  const runG6Action = useCallback(async (action: "start" | "rollback") => {
    if (action === "rollback" && !window.confirm(t("governance.g7.rollbackConfirm"))) return;
    setActionLoading(action);
    try {
      const result = action === "start"
        ? await api.startG6Rollout(portfolioId, { operator_id: "ui:g6" })
        : await api.rollbackG6Rollout(portfolioId, { operator_id: "ui:g6", reason: "治理页人工操作" });
      showToast("success", `${action === "start" ? t("governance.g7.startSuccess") : t("governance.g7.rollbackSuccess")} (${result.status}, audit=${result.audit_event_id ?? "-"})`);
      await load();
    } catch (err: any) {
      showToast("error", err?.message || String(err));
    } finally {
      setActionLoading(null);
    }
  }, [load, portfolioId, showToast]);

  const flagLabel = (key: string) => {
    if (key === "partial") return t("governance.g7.status.partial");
    if (key === "pending_confirmation") return t("governance.g7.status.pending_confirmation");
    if (key === "pending_review") return t("governance.g7.status.pending_review");
    return t("governance.g7.status.other");
  };

  if (!status) {
    return <div className="pt-panel" style={{ padding: 16 }}>{loading ? t("loading") : t("governance.g7.empty")}</div>;
  }

  const flagEntries = Object.entries(status.pending_orders_by_status || {});
  const flagTone = (ok: boolean) => ok ? "#065f46" : "#7f1d1d";
  const flagBg = (ok: boolean) => ok ? "#ecfdf5" : "#fef2f2";

  return (
    <div data-testid="g7-operational-status" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="pt-panel" style={{ padding: 16 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
          <div>
            <h3 style={{ margin: 0, fontSize: 16 }}>{t("governance.g7.title")}</h3>
            <div style={{ marginTop: 4, color: "var(--pt-muted-foreground)", fontSize: 12 }}>{t("governance.g7.subtitle")}</div>
          </div>
          <button type="button" className="pt-btn-ghost" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={14} /> {loading ? t("loading") : t("common.refresh")}
          </button>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button type="button" className="pt-btn-primary" onClick={() => void runG6Action("start")} disabled={Boolean(actionLoading) || status?.ready_for_expansion !== true}>
              <PlayCircle size={14} /> {actionLoading === "start" ? t("loading") : t("governance.g7.startG6")}
            </button>
            <button type="button" className="pt-btn-ghost" onClick={() => void runG6Action("rollback")} disabled={Boolean(actionLoading)}>
              <ShieldCheck size={14} /> {actionLoading === "rollback" ? t("loading") : t("governance.g7.rollbackG6")}
            </button>
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
          {flagEntries.map(([key, value]) => <span key={key} style={{ padding: "4px 8px", borderRadius: 6, background: "#fff7ed", color: "#9a3412", fontSize: 12 }}>{flagLabel(key)}：{value}</span>)}
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
      <ul style={{ margin: 0, paddingLeft: 18, color: "#7f1d1d", fontSize: 12 }}>{items.map((item, i) => <li key={`${item}-${i}`}>{item}</li>)}</ul>
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
}

const PortfolioGovernanceTab: React.FC<PortfolioGovernanceTabProps> = ({ portfolioId, onNavigate }) => {
  const { showToast } = useApp();
  const [sub, setSub] = useState<GovSubTabKey>("state");

  const onNavigateSafe = useCallback(
    (tab: string) => {
      if (onNavigate) onNavigate(tab);
    },
    [onNavigate],
  );
  void onNavigateSafe;

  return (
    <div style={{ padding: "16px 24px 32px 24px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4, flexWrap: "wrap", gap: 10 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 18 }}>{t("portfolioTrading.subtab.governance")}</h2>
          <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)", marginTop: 4 }}>
            FR-P0-10 9-state machine · FR-P1-8a 4 governance endpoints · FR-P1-2 terminal locks · G5 dual-run (10 trading days)
          </div>
        </div>
      </div>

      <SubTabNav active={sub} onChange={setSub} />

      {sub === "state" && <StatePanel portfolioId={portfolioId} showToast={showToast} />}
      {sub === "operations" && <G7OperationsPanel portfolioId={portfolioId} showToast={showToast} />}
      {sub === "reconciliation" && <ReconciliationPanel portfolioId={portfolioId} showToast={showToast} />}
      {sub === "audit" && <AuditPanel portfolioId={portfolioId} showToast={showToast} />}
      {sub === "g5" && <G5Panel portfolioId={portfolioId} showToast={showToast} />}
    </div>
  );
};

export default PortfolioGovernanceTab;
export { StatusBadge, G5_BUSINESS_ACTIONS, G5_ATTR_CATEGORIES };
