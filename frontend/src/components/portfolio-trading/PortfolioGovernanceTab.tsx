import React, { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRightLeft, CheckCircle2, Compass, HelpCircle, History, Lock, PlayCircle, RefreshCw, ShieldCheck, X } from "lucide-react";
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
  // —— 5 个子 Tab 排序：按「日常巡检 → 问题处置 → 前置验收 → 决策执行 → 审计追溯」的执行顺序排列
  //   ① 对账守恒：每天先做的事，zero-sum 不通过，状态机直接打到阻断（最前置）
  //   ② 运行保障：G7 readiness 检查，阻断解除后/准备 G6 前必须先看 blocker 清单
  //   ③ G5 双跑对账：G6 灰度准入的前置验收（双跑不通过，ready_for_expansion 直接为 false）
  //   ④ 系统状态：看完上面 3 个之后，看状态、做转移、执行最终决策（最核心动作放在靠后，和"先检查再决策"的顺序一致）
  //   ⑤ 审计事件：发生过什么、谁动了什么，事后/追溯用（最末尾）
  const tabs: { key: GovSubTabKey; label: string; icon: React.ReactNode; hint?: string }[] = [
    { key: "reconciliation", label: t("portfolioTrading.governance.subtab.reconciliation"), icon: <CheckCircle2 size={14} />, hint: "① 对账守恒（先清账，再谈其它）" },
    { key: "operations", label: t("portfolioTrading.governance.subtab.operations"), icon: <AlertTriangle size={14} />, hint: "② 运行保障（看 blocker）" },
    { key: "g5", label: t("portfolioTrading.governance.subtab.g5DualRun"), icon: <PlayCircle size={14} />, hint: "③ G5 双跑对账（G6 前置验收）" },
    { key: "state", label: t("portfolioTrading.governance.subtab.state"), icon: <ShieldCheck size={14} />, hint: "④ 系统状态（最终决策 / 调整运行状态）" },
    { key: "audit", label: t("portfolioTrading.governance.subtab.audit"), icon: <History size={14} />, hint: "⑤ 审计事件（追溯）" },
  ];
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
      {tabs.map((tab, idx) => (
        <Tooltip
          key={tab.key}
          title={tab.hint}
          placement="bottom"
        >
          <button
            type="button"
            className={`sub-tab ${active === tab.key ? "active" : ""}`}
            onClick={() => onChange(tab.key)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "8px 14px",
              minHeight: 36,
              borderRadius: active === tab.key ? 8 : 8,
              border: "1px solid transparent",
              background:
                active === tab.key
                  ? "linear-gradient(180deg, rgba(6,182,212,0.18), rgba(6,182,212,0.08))"
                  : "rgba(31,41,55,0.55)",
              borderColor:
                active === tab.key
                  ? "rgba(6,182,212,0.55)"
                  : "rgba(71,85,105,0.55)",
              color:
                active === tab.key
                  ? "var(--pt-primary)"
                  : "var(--pt-muted-foreground)",
              fontWeight: active === tab.key ? 600 : 500,
              fontSize: 13,
              boxShadow:
                active === tab.key
                  ? "0 0 0 1px rgba(6,182,212,0.25) inset, 0 2px 8px rgba(6,182,212,0.12)"
                  : "none",
            }}
          >
            <span
              aria-hidden
              style={{
                width: 18,
                height: 18,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                borderRadius: 999,
                fontSize: 10,
                fontWeight: 700,
                lineHeight: 1,
                color: active === tab.key ? "var(--pt-primary-foreground)" : "#cbd5e1",
                background:
                  active === tab.key
                    ? "var(--pt-primary)"
                    : "rgba(71,85,105,0.75)",
                marginRight: 2,
              }}
            >
              {idx + 1}
            </span>
            {tab.icon}
            {tab.label}
          </button>
        </Tooltip>
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
      {/* —— 🪜 系统状态 Tab 使用说明（放在最上面，先告诉用户"这一页两步就能用起来"）—— */}
      <div
        style={{
          padding: "10px 12px",
          borderRadius: 8,
          background:
            "linear-gradient(90deg, rgba(59,130,246,0.07), rgba(139,92,246,0.07))",
          border: "1px solid rgba(59,130,246,0.22)",
          fontSize: 12,
          lineHeight: 1.8,
          color: "var(--pt-foreground)",
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 2, color: "#3b82f6" }}>
          🪜 系统状态 Tab：两步就能上手（你现在 90% 懵逼的点下面都解释了）
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
          <div>
            <div style={{ fontWeight: 600, color: "var(--pt-state-info)", marginBottom: 2 }}>
              Step 1 · 看权限（只读）
            </div>
            <div style={{ color: "var(--pt-foreground)" }}>
              绿色高亮出的那一行就是<b>「当前状态」</b>。向右看 4 列：<br />
              「✓允许/✗禁止」就是：<b>能不能买/能不能卖/能否自动恢复/是否需要你写审批备注</b>。
            </div>
          </div>
          <div>
            <div style={{ fontWeight: 600, color: "var(--pt-state-success)", marginBottom: 2 }}>
              Step 2 · 想切状态？点「调整运行状态」
            </div>
            <div style={{ color: "var(--pt-foreground)" }}>
              下面「允许目标状态」那几个<b>彩色胶囊是展示用的，不是按钮！</b>要切状态：<br />
              点右上角「<b>调整运行状态</b>」→ 弹窗里选目标 → 填触发原因 + （需要时写≥10字审批备注）→ 确认。
            </div>
          </div>
          <div>
            <div style={{ fontWeight: 600, color: "#d97706", marginBottom: 2 }}>
              💡 小技巧：hover 就能看解释
            </div>
            <div style={{ color: "var(--pt-foreground)" }}>
              ① <b>状态徽章</b> hover → 这状态是怎么触发的、怎么恢复<br />
              ② <b>4 列表头</b> hover → 这一列到底啥意思<br />
              ③ <b>裸字段（日期/资格那几行）</b>hover → 字段是用来干嘛的
            </div>
          </div>
        </div>
      </div>

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
                  <b>刷新状态</b>：重新拉一次治理/状态机接口，<b style={{ color: "#dc2626" }}>不会触发任何转移/对账/审计</b>。<br />
                  建议在以下情况点一下：<br />
                  ① 数据同步完成（MARKET_DATA_STALE 已解决）<br />
                  ② 对账差异刚刚确认完<br />
                  ③ 模型激活/绑定刚刚改完
                </div>
              }
              placement="top"
            >
              <button type="button" className="pt-btn-ghost" onClick={fetchStatus} disabled={loading} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <RefreshCw size={14} /> {loading ? "…" : t("common.refresh")}
              </button>
            </Tooltip>
            <Tooltip
              title={
                allowedTargets.length === 0 ? (
                  t("governance.transition.noTargets")
                ) : (
                  <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 360 }}>
                    <div style={{ fontWeight: 600, marginBottom: 4 }}>
                      🚦 调整运行状态（把组合从当前状态转到「允许目标状态」里的某一个）
                    </div>
                    <div>① 点开会弹对话框：先选「目标状态」（只会列出允许转移的）</div>
                    <div>② 填「触发原因」（例：一键刹车 / 解除对账阻断 / 心跳恢复）</div>
                    <div style={{ color: "#d97706" }}>
                      ③ 下列 3 种情况「审查备注」必填 ≥10 字：<br />
                      · 从 ADMIN_PAUSED / RECONCILIATION_BLOCKED 转出<br />· 转入 READY
                    </div>
                    <div>④ 点确认后写入<b>审计事件</b>（可在「审计事件」Tab 用 audit_event_id 溯源）</div>
                    <div style={{ color: "#94a3b8", marginTop: 4 }}>
                      注：转移必须严格按允许矩阵/状态机规则，被拒绝 = 不合法，不扣状态。
                    </div>
                  </div>
                )
              }
              placement="top"
            >
              <button
                type="button"
                className="pt-btn-primary"
                onClick={handleOpenTransition}
                disabled={allowedTargets.length === 0 || !current}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  // ↓↓↓ 内联硬 override：就算 pt-btn-primary 被暗色主题覆盖，也强制显示主按钮层级（蓝底白字+阴影）
                  background: allowedTargets.length === 0 || !current ? undefined : "linear-gradient(180deg,#3b82f6,#2563eb)",
                  color: "#fff",
                  border: "1px solid #1d4ed8",
                  padding: "7px 14px",
                  fontWeight: 600,
                  fontSize: 13,
                  borderRadius: 8,
                  boxShadow: allowedTargets.length === 0 || !current ? undefined : "0 2px 6px rgba(37,99,235,0.35)",
                }}
              >
                <ArrowRightLeft size={15} /> {t("governance.transition.applyBtn")}
              </button>
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
                    <span style={{ color: "#059669", fontWeight: 600 }}>✓ 允许</span> —— 该列业务动作正常放行<br />
                    <span style={{ color: "#b91c1c" }}>✗ 禁止</span> —— HG1 门禁直接阻断（自动接管 / 手动下单都会被拒）<br />
                    <span style={{ padding: "1px 6px", borderRadius: 999, border: "1px solid #f59e0b", background: "#fffbeb", color: "#78350f", fontSize: 11 }}>REQUIRED</span> —— 状态进入/离开时必须人工写审批备注（≥10字），留审计
                  </div>
                </div>
              }
            >
              <HelpCircle size={13} style={{ cursor: "help", color: "var(--pt-muted-foreground)" }} />
            </Tooltip>
          </h4>
          <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 11, color: "var(--pt-muted-foreground)" }}>
            <span style={{ color: "#059669" }}>✓ 允许</span>
            <span style={{ color: "#b91c1c" }}>✗ 禁止</span>
            <span style={{ padding: "1px 6px", borderRadius: 999, border: "1px solid #f59e0b", background: "#fffbeb", color: "#78350f" }}>REQUIRED 需审批</span>
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
                          <span style={{ color: "#b91c1c" }}>✗ 禁止时：任何买入会被 HG1 直接拒绝</span>，即使你点了也不会下到模拟账户里。
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
                          <span style={{ color: "#059669" }}>即使「新买单」被禁止，大部分暂停状态仍允许风险退出</span>——防止暂停后持仓无法止损。
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
                          如果问题<b>自动解除</b>了，系统是否允许<b>从暂停态自己跳回 READY / 推演中</b>？<br />
                          <span style={{ color: "#059669" }}>✓ 允许自动恢复 = 不用你管</span>（例：数据缺失→同步成功后自动恢复）<br />
                          <span style={{ color: "#b91c1c" }}>✗ 禁止 = 必须人工介入</span>（例：对账阻断 / 管理员暂停 → 必须点「调整运行状态」写审批备注转回）
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
                          进入 / 离开 这一行状态时，<b>是否需要人写审批备注</b>（并留审计事件）。<br />
                          <span style={{ padding: "1px 6px", borderRadius: 999, border: "1px solid #f59e0b", background: "#fffbeb", color: "#78350f", fontSize: 11 }}>REQUIRED</span>
                          ：<b>必须填 ≥10 个字符的「审查备注」</b>，否则「调整运行状态」弹窗会拒绝你提交。<br />
                          <span style={{ color: "#94a3b8" }}>（设计初衷：对「对账阻断解除 / 管理员刹车解除 / 第一次进入生产就绪」等高敏感动作留痕）</span>
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
                      <Tooltip title={t(`portfolioStatus.${k}.desc` as const)} placement="right">
                        <span>
                          <StatusBadge status={k} size="sm" showDesc={false} />
                          <span style={{ fontSize: 10, color: "#94a3b8", marginLeft: 6, fontStyle: "italic" }}>
                            {k}
                          </span>
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
                              <div style={{ fontWeight: 600, marginBottom: 4 }}>🔒 进出此状态必须人工审批（≥10 字审查备注）</div>
                              <div>
                                当状态转移命中以下 <b>3 种高敏感场景</b> 时，「调整运行状态」弹窗会强制你填审查备注，
                                并写入<b>审计事件</b>（见「审计事件」子Tab）：
                              </div>
                              <div style={{ marginTop: 6 }}>
                                ① 从 <b>管理员强制暂停 (ADMIN_PAUSED)</b> 转出（解除刹车）<br />
                                ② 从 <b>对账差异阻断 (RECONCILIATION_BLOCKED)</b> 转出（解除阻断）<br />
                                ③ 转入 <b>生产就绪 (READY)</b>（任何路径恢复到可交易态）
                              </div>
                              <div style={{ color: "#94a3b8", marginTop: 6 }}>
                                备注少于 10 字 → 弹窗的「确认」按钮会被禁用，不允许你提交。
                              </div>
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
          padding: "3px 10px",
          borderRadius: 999,
          border: "1px solid #f59e0b",
          background: "linear-gradient(180deg,#fffbeb,#fef3c7)",
          color: "#78350f",
          fontSize: 11.5,
          fontWeight: 700,
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
        }}
      >
        🔒 REQUIRED · 需审批
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

                // —— 对「元维度」行做特殊视觉：对账服务在这些分支里直接 short-circuit，
                //    expected/actual 本来就不会有数值（例如未执行当日决策），
                //    继续显示「—」会让人误会为"前端 bug/后端没传数"，
                //    改为 info 色底 + N/A + 小字说明，整行视觉上和「数值差异红底」区分开。
                const metaKinds: Array<string> = ["DECISION_RUN_NOT_FOUND", "NOT_CHECKED", "NAV_BROKEN"];
                const isMetaRow =
                  typeof dim === "string" && metaKinds.some((k) => dim.indexOf(k) >= 0);

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
                        color: "#1e3a8a",
                        background: "rgba(59,130,246,0.18)",
                        border: "1px solid rgba(59,130,246,0.45)",
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
                      {rowBadge}
                      {dimLabel}
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
                        <span style={{ fontWeight: 600, color: deltaQty > 0 ? "#15803d" : deltaQty < 0 ? "#b91c1c" : "inherit" }}>
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
                        <span style={{ color: diff > 0 ? "#15803d" : diff < 0 ? "#b91c1c" : undefined, fontWeight: 600 }}>
                          {diff > 0 ? "+" : ""}{fmtNum(diff)}
                        </span>
                      ) : (
                        fmtNum(diff)
                      )}
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
            <select
              className="pt-select"
              value={eventType}
              onChange={(e) => setEventType(e.target.value)}
              style={{ padding: "6px 10px" }}
            >
              {AUDIT_EVENT_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{t("governance.audit.filter.severity")}</label>
            <select className="pt-select" value={severity} onChange={(e) => setSeverity((e.target.value || "") as any)} style={{ padding: "6px 10px" }}>
              <option value="">— 全部 —</option>
              <option value="INFO">INFO（信息）</option>
              <option value="WARNING">WARNING（警告）</option>
              <option value="L1">L1（提醒）</option>
              <option value="L2">L2（警告）</option>
              <option value="L3">L3（严重）</option>
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
            {(() => {
              const g6Ready = Boolean(status?.ready_for_expansion);
              const isDisabled = Boolean(actionLoading) || !g6Ready;
              const blockerReasons = !g6Ready ? (status?.operational_blockers ?? []).slice(0, 3) : [];
              const tooltip = !g6Ready
                ? `无法启动 G6 灰度，需先解决以下问题：${blockerReasons.join("；") || "条件未满足"}`
                : "";
              return (
                <Tooltip title={isDisabled ? tooltip : undefined} placement="bottom">
                  <button
                    type="button"
                    className="pt-btn-primary"
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
                      void runG6Action("start");
                    }}
                    style={
                      isDisabled
                        ? { opacity: 0.5, cursor: "not-allowed", filter: "grayscale(0.3)" }
                        : undefined
                    }
                  >
                    <PlayCircle size={14} /> {actionLoading === "start" ? t("loading") : t("governance.g7.startG6")}
                  </button>
                </Tooltip>
              );
            })()}
            <button type="button" className="pt-btn-ghost" onClick={() => void runG6Action("rollback")} disabled={Boolean(actionLoading)} style={Boolean(actionLoading) ? { opacity: 0.5, cursor: "not-allowed" } : undefined}>
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
          <h2 style={{ margin: 0, fontSize: 18, display: "flex", alignItems: "center", gap: 8 }}>
            <Compass size={18} color="var(--pt-primary)" />
            {t("portfolioTrading.subtab.governance")}
          </h2>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--pt-muted-foreground)", marginTop: 4 }}>
            <span>组合风控状态机总控 · 5 个子 Tab · 审计 & 对账 & 双跑中枢</span>
            <Tooltip
              title={
                <div style={{ fontSize: 12, lineHeight: 1.7, maxWidth: 360 }}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>原英文需求编号说明（给开发/审计看的契约）：</div>
                  <div>• FR-P0-10：9 状态状态机（HG1 门禁的权限矩阵）</div>
                  <div>• FR-P1-8a：治理端 4 个 API（查状态/状态转移/对账差异确认/审计列表）</div>
                  <div>• FR-P1-2：终端锁（避免多终端重复触发自动交易）</div>
                  <div>• G5 dual-run (10 trading days)：模拟 vs 生产双跑 10 个交易日一致，通过后才可接入 G6</div>
                </div>
              }
            >
              <HelpCircle size={13} style={{ cursor: "help", color: "var(--pt-muted-foreground)" }} />
            </Tooltip>
          </div>
        </div>
      </div>

      <SubTabNav active={sub} onChange={setSub} />

      {/* —— 🧭 整页级：治理 Tab 是干嘛的 / 5 个子 Tab 各管什么 / 典型 3 种使用场景 —— */}
      <div
        style={{
          padding: "12px 14px",
          borderRadius: 10,
          background:
            "linear-gradient(90deg, rgba(99,102,241,0.08), rgba(16,185,129,0.08))",
          border: "1px solid rgba(99,102,241,0.25)",
          lineHeight: 1.8,
          fontSize: 12,
          color: "var(--pt-foreground)",
          marginBottom: 16,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 6, color: "var(--pt-primary)", fontSize: 13 }}>
          🧭 「治理」是干嘛的？一句话：<b style={{ color: "#6366f1" }}>组合风控状态机的总控面板</b>（决定"现在能买吗/能卖吗/能自动恢复吗"）
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1.1fr 1fr 1fr", gap: 12, marginTop: 8 }}>
          {/* 5 个子 Tab 各管什么 */}
          <div
            style={{
              padding: "8px 10px",
              borderRadius: 6,
              background: "rgba(99,102,241,0.06)",
              border: "1px dashed rgba(99,102,241,0.35)",
            }}
          >
            <div style={{ fontWeight: 600, color: "var(--pt-state-info)", marginBottom: 4 }}>
              🗂️ 5 个子 Tab 分工
            </div>
            <div style={{ color: "var(--pt-foreground)" }}>
              ① <b>对账守恒</b>：持仓/现金/订单/成交 10 列明细，zero-sum 零和检查 + 差异确认（每天先看这里）<br />
              ② <b>运行保障</b>：G7 运行态（可扩张？已停止新单？可恢复？+ blocker 清单）<br />
              ③ <b>G5 双跑对账</b>：模拟 vs 生产 10 交易日双跑启动 + 6×6 混淆矩阵 + 通过/失败结果（G6 前置）<br />
              ④ <b>系统状态</b>：当前状态徽章 + 允许转移目标 + 9×4 允许矩阵 + 调整运行状态（最终决策入口）<br />
              ⑤ <b>审计事件</b>：谁什么时候改了什么（转移/调仓/配置）按严重度过滤 + correlation_id 溯源
            </div>
          </div>
          {/* 3 种典型使用场景 */}
          <div
            style={{
              padding: "8px 10px",
              borderRadius: 6,
              background: "rgba(16,185,129,0.06)",
              border: "1px dashed rgba(16,185,129,0.35)",
            }}
          >
            <div style={{ fontWeight: 600, color: "var(--pt-state-success)", marginBottom: 4 }}>
              🧪 3 种典型使用场景
            </div>
            <div style={{ color: "var(--pt-foreground)" }}>
              ① <b>先对账（日常）</b>：<b>对账守恒 Tab</b> → zero-sum 清零 → 点「差异确认」把 RECONCILIATION_BLOCKED 解锁<br />
              ② <b>再验收（G6 前置）</b>：<b>G5 双跑对账 Tab</b> → 连续 10 日 P0/P1 全通过 + <b>运行保障 Tab</b> blocker 清零 → 才具备 G6 准入<br />
              ③ <b>后决策（转移状态）</b>：以上都过 → <b>系统状态 Tab</b> → 看「允许目标状态」里 READY 是否出现 → 点「调整运行状态」→ 填 ≥10 字审查备注 → 恢复/刹车
            </div>
          </div>
          {/* 状态流转心智模型 */}
          <div
            style={{
              padding: "8px 10px",
              borderRadius: 6,
              background: "rgba(245,158,11,0.06)",
              border: "1px dashed rgba(245,158,11,0.35)",
            }}
          >
            <div style={{ fontWeight: 600, color: "#d97706", marginBottom: 4 }}>
              ♻️ 状态流转心智模型
            </div>
            <div style={{ color: "var(--pt-foreground)" }}>
              <b>正常路径</b>：初始审查中 → <b>生产就绪 (READY)</b> → 20:30自动推演/回测 → 回到 READY<br />
              <b>自动保护（可自动恢复）</b>：READY → 数据缺失 / Score过期 / 心跳中断 → <b>下一次同步成功/心跳恢复 → 自动跳回 READY</b><br />
              <b>人工干预（必须调整运行状态）</b>：对账差异阻断 / 管理员强制暂停 → <b>不允许自动恢复</b>，必须走「调整运行状态」弹窗填 ≥10 字审查备注，经审计记录后才能转回 READY
            </div>
          </div>
        </div>
      </div>

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
