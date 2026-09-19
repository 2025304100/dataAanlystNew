import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  X,
  FileSearch,
  Calendar,
  Clock,
  Shield,
  ShieldAlert,
  ShieldCheck,
  TrendingUp,
  TrendingDown,
  Minus,
  AlertTriangle,
  Ban,
  DatabaseZap,
  Hash,
  Layers,
  ChevronLeft,
  ChevronDown,
  ChevronRight,
  Loader2,
  BarChart3,
  Activity,
  Zap,
  Info,
  Copy,
} from "lucide-react";
import {
  Tabs,
  Tag,
  Table,
  Empty,
  Progress,
  Alert,
  Tooltip,
  Badge,
  Collapse,
  Descriptions,
  Typography,
  App as AntApp,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  api,
  type DecisionAction,
  type DecisionEvidenceRead,
  type DecisionRunRead,
} from "../../api/client";
import { useApp } from "../../context/AppContext";

/**
 * DecisionEvidenceDrawer — WP1-1 证据与归因抽屉（680px 右侧抽屉）。
 *
 * 目标：将后端 DecisionRun + DecisionEvidence 契约以「决策可溯源、证据可阅读、归因可展开」
 * 的方式呈现给用户。严格对齐 6 类 action：BUY/SELL/HOLD/NO_ACTION/REJECTED/DATA_BLOCKED
 * 并提供：
 *   ① DecisionRun 概要头（门禁状态 / 决策时钟 / Score 覆盖率 / 版本快照）
 *   ② 动作分类 Tab：每 Tab 展示该类证据，支持 factor_contributions_json 展开（因子归因）
 *                  + constraints_json（仓位约束）+ reason_codes_json（裁决原因码）
 *   ③ 拒绝记录（REJECTED/DATA_BLOCKED）Tab 默认高亮「拒绝原因 + PIT 安全标记」
 *   ④ 支持两种触发方式：(a) 直接指定 decisionRunId 懒加载，(b) 外部传入 run + 预览 evidence 直显
 *
 * Props：
 *   - open / onClose：抽屉开关
 *   - portfolioId：当前组合（用于 symbol_id→symbol 名查找；可为空时退回 "id={symbol_id}"）
 *   - decisionRunId：可选，指定则调用 getDecisionRun + listDecisionRunEvidence 懒加载
 *   - initialRun / initialEvidence：可选，外部直接传入（如 POST evaluate 返回的 preview），避免重复请求
 *   - selectedEvidenceId：可选，标记当前需要定位的证据行
 *   - onSelectedEvidenceIdChange：可选，证据导航后同步外部当前证据 ID
 */

const { Text, Paragraph } = Typography;

// 抽屉宽度，按证据表格宽度设计
export const DECISION_EVIDENCE_DRAWER_WIDTH = 520;

export interface DecisionEvidenceDrawerProps {
  open: boolean;
  onClose: () => void;
  portfolioId: number | null;
  decisionRunId?: string | null;
  initialRun?: DecisionRunRead | null;
  initialEvidence?: DecisionEvidenceRead[];
  /** 可选：打开抽屉后在证据表中定位并标记指定证据。 */
  selectedEvidenceId?: string | null;
  /** 上一条/下一条证据切换后通知外部入口同步当前 ID。 */
  onSelectedEvidenceIdChange?: (evidenceId: string) => void;
}

/* ---------- 辅助：动作 → 颜色 / 图标 / 中文名 ---------- */
const ACTION_META: Record<DecisionAction, {
  color: string;
  bg: string;
  icon: React.ReactNode;
  label: string;
}> = {
  BUY:          { color: "#16a34a", bg: "rgba(22,163,74,0.08)", icon: <TrendingUp size={12} />,   label: "买入 BUY" },
  SELL:         { color: "#dc2626", bg: "rgba(220,38,38,0.08)", icon: <TrendingDown size={12} />, label: "卖出 SELL" },
  HOLD:         { color: "#ca8a04", bg: "rgba(202,138,4,0.08)", icon: <Minus size={12} />,        label: "持有 HOLD" },
  NO_ACTION:    { color: "#64748b", bg: "rgba(100,116,139,0.08)", icon: <Minus size={12} />,       label: "无动作 NO_ACTION" },
  REJECTED:     { color: "#b45309", bg: "rgba(180,83,9,0.08)",  icon: <Ban size={12} />,          label: "拒绝 REJECTED" },
  DATA_BLOCKED: { color: "#7e22ce", bg: "rgba(126,34,206,0.08)", icon: <DatabaseZap size={12} />, label: "数据阻断 DATA_BLOCKED" },
};

function actionTag(a: DecisionAction) {
  const m = ACTION_META[a];
  return (
    <Tag style={{
      borderColor: m.color + "55",
      color: m.color,
      background: m.bg,
      display: "inline-flex",
      alignItems: "center",
      gap: 4,
      paddingInline: 6,
      paddingBlock: 0,
      fontSize: 11,
      margin: 0,
    }}>
      {m.icon}{m.label}
    </Tag>
  );
}

function blockingStatusColor(s: string) {
  switch (s) {
    case "READY": return "#16a34a";
    case "DATA_INCOMPLETE_PAUSED": return "#7e22ce";
    case "RECONCILIATION_BLOCKED": return "#b45309";
    case "MODEL_INACTIVE": return "#dc2626";
    case "SCORE_STALE": return "#ca8a04";
    default: return "#64748b";
  }
}

function pitBadge(f: "PIT_SAFE" | "NOT_PIT_SAFE" | "UNKNOWN") {
  if (f === "PIT_SAFE") {
    return <Tag icon={<ShieldCheck size={10} />} color="green" style={{ fontSize: 11, padding: "0 6px", margin: 0 }}>PIT 安全</Tag>;
  }
  if (f === "NOT_PIT_SAFE") {
    return <Tag icon={<ShieldAlert size={10} />} color="red" style={{ fontSize: 11, padding: "0 6px", margin: 0 }}>非 PIT</Tag>;
  }
  return <Tag icon={<Shield size={10} />} color="default" style={{ fontSize: 11, padding: "0 6px", margin: 0 }}>未知</Tag>;
}

interface OrderPlanExecutionSummary {
  requestedQuantity: number | null;
  filledQuantity: number | null;
  remainingQuantity: number | null;
  status: string | null;
  unfilledReason: string | null;
}

function finiteNumberOrNull(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || !value.trim()) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

/**
 * Matching writes its immutable execution lifecycle into `versions_json`.
 * Keep the reader permissive so historical evidence without those keys still
 * renders normally, while current partial fills are explainable without
 * asking users to inspect raw JSON.
 */
function orderPlanExecutionSummary(rawVersions: unknown): OrderPlanExecutionSummary | null {
  if (!rawVersions || Array.isArray(rawVersions) || typeof rawVersions !== "object") return null;
  const versions = rawVersions as Record<string, unknown>;
  const requestedQuantity = finiteNumberOrNull(versions.order_plan_requested_quantity);
  const filledQuantity = finiteNumberOrNull(versions.order_plan_filled_quantity);
  const remainingQuantity = finiteNumberOrNull(versions.order_plan_remaining_quantity);
  const status = typeof versions.order_plan_status === "string" && versions.order_plan_status
    ? versions.order_plan_status
    : null;
  const unfilledReason = typeof versions.order_plan_unfilled_reason === "string" && versions.order_plan_unfilled_reason
    ? versions.order_plan_unfilled_reason
    : null;
  if (
    requestedQuantity == null
    && filledQuantity == null
    && remainingQuantity == null
    && status == null
    && unfilledReason == null
  ) {
    return null;
  }
  return { requestedQuantity, filledQuantity, remainingQuantity, status, unfilledReason };
}

function formatOrderPlanQuantity(value: number | null): string {
  if (value == null) return "--";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value);
}

function orderPlanStatusLabel(status: string): string {
  switch (status) {
    case "FILLED": return "已成交";
    case "PARTIAL_FILL": return "部分成交";
    case "PARTIAL_FILL_PENDING": return "部分成交，待重试";
    case "PENDING_RETRY": return "待重试";
    case "REJECTED": return "未成交";
    default: return status;
  }
}

function ruleComparisonsFromVersions(rawVersions: unknown): Array<Record<string, unknown>> {
  if (!rawVersions || Array.isArray(rawVersions) || typeof rawVersions !== "object") return [];
  const versions = rawVersions as Record<string, unknown>;
  for (const key of ["rule_comparisons", "rule_traces", "conditions"]) {
    const candidate = versions[key];
    if (Array.isArray(candidate)) {
      const records = candidate.filter(
        (item): item is Record<string, unknown> => !!item && !Array.isArray(item) && typeof item === "object",
      );
      if (records.length > 0) return records;
    }
  }
  return [];
}

function ruleComparisonValue(value: unknown): string {
  if (value == null || value === "") return "--";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/* ---------- 展示 symbol_id → {symbol,name} 映射的 hook ---------- */
function useSymbolMap(portfolioId: number | null) {
  const [symbols, setSymbols] = useState<Map<number, { symbol: string; name: string | null }>>(new Map());
  const { portfolios } = useApp();
  useEffect(() => {
    if (!portfolioId) return;
    const pf = portfolios.find((p) => p.id === portfolioId);
    const members = (pf as any)?.members || [];
    const next = new Map<number, { symbol: string; name: string | null }>();
    for (const m of members) {
      if (m?.symbol_id && m?.symbol) {
        next.set(Number(m.symbol_id), { symbol: String(m.symbol), name: m.name || null });
      }
    }
    setSymbols(next);
  }, [portfolioId, portfolios]);
  const lookup = useCallback(
    (sid: number | string | null | undefined): { symbol: string; name: string | null } => {
      if (sid == null) return { symbol: "-", name: null };
      const n = Number(sid);
      return symbols.get(n) || { symbol: `id=${n}`, name: null };
    },
    [symbols],
  );
  return { lookup, symbols };
}

/* ---------- 漂亮地渲染 JSON（不破坏 JSX 树，Array/Object 折叠、Number 染色） ---------- */
function PrettyJson({ data, depth = 0 }: { data: any; depth?: number }) {
  const pad = { paddingLeft: depth * 16 };
  if (data == null) return <span style={{ color: "#94a3b8" }}>null</span>;
  if (typeof data === "number") {
    const cls = Math.abs(data) >= 1000 ? "#6b21a8" : (data === 0 ? "#64748b" : (data > 0 ? "#0f766e" : "#c026d3"));
    return <span style={{ color: cls, fontFamily: "var(--pt-font-mono)" }}>{data}</span>;
  }
  if (typeof data === "boolean") return <span style={{ color: "#1d4ed8" }}>{String(data)}</span>;
  if (typeof data === "string") {
    const short = data.length > 120 ? data.slice(0, 120) + "…" : data;
    return <span style={{ color: "#0f172a", fontFamily: "var(--pt-font-mono)" }}>"{short}"</span>;
  }
  if (Array.isArray(data)) {
    if (data.length === 0) return <span style={{ color: "#94a3b8" }}>[]</span>;
    if (depth >= 3 && data.length > 8) {
      // 超过 3 层且太长，折叠
      return (
        <Collapse ghost size="small" style={{ margin: 0 }}>
          <Collapse.Panel
            key="0"
            header={`Array(${data.length})`}
            style={{ padding: 0, margin: 0, border: "none" }}
          >
            <div style={pad}>
              {data.slice(0, 20).map((it, i) => (
                <div key={i} style={{ lineHeight: 1.7 }}>
                  <span style={{ color: "#94a3b8" }}>{i}:</span> <PrettyJson data={it} depth={depth + 1} />
                </div>
              ))}
              {data.length > 20 ? <div style={{ color: "#94a3b8" }}>… 共 {data.length} 项</div> : null}
            </div>
          </Collapse.Panel>
        </Collapse>
      );
    }
    return (
      <div style={pad}>
        {data.map((it, i) => (
          <div key={i} style={{ lineHeight: 1.7 }}>
            <span style={{ color: "#94a3b8" }}>{i}:</span> <PrettyJson data={it} depth={depth + 1} />
          </div>
        ))}
      </div>
    );
  }
  if (typeof data === "object") {
    const keys = Object.keys(data);
    if (keys.length === 0) return <span style={{ color: "#94a3b8" }}>{`{}`}</span>;
    return (
      <div style={pad}>
        {keys.map((k) => (
          <div key={k} style={{ lineHeight: 1.7 }}>
            <span style={{ color: "#6d28d9", fontWeight: 500 }}>{k}:</span>{" "}
            <PrettyJson data={data[k]} depth={depth + 1} />
          </div>
        ))}
      </div>
    );
  }
  return <span>{String(data)}</span>;
}

/* ---------- 核心组件 ---------- */
export const DecisionEvidenceDrawer: React.FC<DecisionEvidenceDrawerProps> = ({
  open,
  onClose,
  portfolioId,
  decisionRunId,
  initialRun,
  initialEvidence,
  selectedEvidenceId,
  onSelectedEvidenceIdChange,
}) => {
  const { message } = AntApp.useApp();

  // 决策运行数据
  const [run, setRun] = useState<DecisionRunRead | null>(initialRun || null);
  const [loadingRun, setLoadingRun] = useState(false);

  // 证据严格按服务端分页加载。
  const [evidence, setEvidence] = useState<DecisionEvidenceRead[]>(initialEvidence || []);
  const [loadingEvidence, setLoadingEvidence] = useState(false);
  const [evidenceTotal, setEvidenceTotal] = useState(initialEvidence ? initialEvidence.length : 0);
  const [evidencePage, setEvidencePage] = useState(1);
  const evidencePageSize = 50;

  // 当前激活 Tab 对应的 action 过滤
  const [activeAction, setActiveAction] = useState<DecisionAction | "ALL">("ALL");
  // 展开的 evidence 行（用于显示 factor_contributions_json / constraints_json / reason_codes_json）
  const [expandedRowKeys, setExpandedRowKeys] = useState<React.Key[]>([]);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [activeEvidenceId, setActiveEvidenceId] = useState<string | null>(selectedEvidenceId ?? null);
  const [isNarrowViewport, setIsNarrowViewport] = useState(false);
  const previousOpenRef = useRef(false);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  const { lookup: symbolOf } = useSymbolMap(portfolioId);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(max-width: 520px)");
    const sync = () => setIsNarrowViewport(media.matches);
    sync();
    media.addEventListener?.("change", sync);
    return () => media.removeEventListener?.("change", sync);
  }, []);

  /* ------- 键盘交互：打开时 Escape 关闭，卸载/隐藏时移除监听 ------- */
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  /* ------- 打开时将焦点移入抽屉，关闭后恢复到触发入口 ------- */
  useEffect(() => {
    let focusTimer: number | null = null;
    if (open) {
      if (!previousOpenRef.current) {
        restoreFocusRef.current = document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      }
      focusTimer = window.setTimeout(() => closeButtonRef.current?.focus(), 0);
    } else if (!open && previousOpenRef.current) {
      restoreFocusRef.current?.focus();
      restoreFocusRef.current = null;
    }
    previousOpenRef.current = open;
    return () => {
      if (focusTimer != null) window.clearTimeout(focusTimer);
    };
  }, [open]);

  useEffect(() => {
    if (open) setCopyError(null);
  }, [open, decisionRunId, initialRun?.id]);

  useEffect(() => {
    setActiveEvidenceId(selectedEvidenceId ?? null);
  }, [selectedEvidenceId]);

  const copyTarget = useMemo(() => {
    if (activeEvidenceId) {
      return {
        id: activeEvidenceId,
        buttonLabel: "复制证据 ID",
        ariaLabel: "复制当前证据 ID",
        successText: "已复制当前证据 ID",
        errorText: "复制当前证据 ID 失败",
      };
    }
    const runId = run?.id || decisionRunId;
    if (!runId) return null;
    return {
      id: runId,
      buttonLabel: "复制运行 ID",
      ariaLabel: "复制当前 DecisionRun ID（未选择证据）",
      successText: "已复制当前 DecisionRun ID（未选择证据）",
      errorText: "复制当前 DecisionRun ID 失败",
    };
  }, [activeEvidenceId, decisionRunId, run?.id]);

  const copyCurrentId = useCallback(async () => {
    if (!copyTarget) {
      const errorText = "当前没有可复制的证据 ID 或 DecisionRun ID";
      setCopyError(errorText);
      message.error?.(errorText);
      return;
    }
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(copyTarget.id);
      setCopyError(null);
      message.success?.(copyTarget.successText);
    } catch {
      setCopyError(copyTarget.errorText);
      message.error?.(copyTarget.errorText);
    }
  }, [copyTarget, message]);

  /* ------- 初始化加载：当 decisionRunId 变更且抽屉打开时 ------- */
  useEffect(() => {
    if (!open) return;
    // 外部预览数据优先，直接 set
    if (initialRun && initialEvidence) {
      setRun(initialRun);
      setEvidence(initialEvidence);
      setEvidenceTotal(initialEvidence.length);
      return;
    }
    if (!decisionRunId) return;
    // 否则拉取后端
    const rid = decisionRunId;
    setLoadingRun(true);
    setLoadingEvidence(true);
    let alive = true;
    api.getDecisionRun(rid)
      .then((r) => { if (alive) setRun(r); })
      .catch((e) => { if (alive) message.error("加载 DecisionRun 失败：" + String(e)); })
      .finally(() => { if (alive) setLoadingRun(false); });
    api.listDecisionRunEvidence(rid, {
      action: activeAction === "ALL" ? undefined : activeAction,
      limit: evidencePageSize,
      offset: (evidencePage - 1) * evidencePageSize,
    })
      .then((resp) => {
        if (!alive) return;
        setEvidence(Array.isArray(resp.items) ? resp.items : []);
        setEvidenceTotal(resp.total || 0);
      })
      .catch((e) => { if (alive) message.error("加载 DecisionEvidence 失败：" + String(e)); })
      .finally(() => { if (alive) setLoadingEvidence(false); });
    return () => { alive = false; };
  }, [open, decisionRunId, initialRun, initialEvidence, activeAction, evidencePage]);

  /* ------- 关闭抽屉时重置内部状态，避免 stale ------- */
  useEffect(() => {
    if (open) return;
    const t = setTimeout(() => {
      setExpandedRowKeys([]);
      setActiveAction("ALL");
      setEvidencePage(1);
    }, 250);
    return () => clearTimeout(t);
  }, [open]);

  /* ------- 当前服务端页；总数已按 action 条件聚合 ------- */
  const actionCounts = useMemo(() => {
    const m: Record<string, number> = { ALL: activeAction === "ALL" ? evidenceTotal : 0 };
    if (activeAction !== "ALL") m[activeAction] = evidenceTotal;
    return m;
  }, [activeAction, evidenceTotal]);

  const filteredEvidence = useMemo(() => {
    return evidence;
  }, [evidence]);

  const selectedEvidenceIndex = useMemo(
    () => activeEvidenceId == null
      ? -1
      : filteredEvidence.findIndex((item) => String(item.id) === String(activeEvidenceId)),
    [activeEvidenceId, filteredEvidence],
  );

  const moveEvidence = useCallback((offset: -1 | 1) => {
    if (selectedEvidenceIndex < 0) return;
    const target = filteredEvidence[selectedEvidenceIndex + offset];
    if (!target) return;
    const targetId = String(target.id);
    setActiveEvidenceId(targetId);
    setExpandedRowKeys([]);
    onSelectedEvidenceIdChange?.(targetId);
  }, [filteredEvidence, onSelectedEvidenceIdChange, selectedEvidenceIndex]);

  /* ------- 概要头：门禁/时钟/Score 覆盖率 ------- */
  const scoreCoverageBar = run ? (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 12 }}>
      <div>
        <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)", marginBottom: 4 }}>
          Score 覆盖率（成员×交易日）
        </div>
        <Progress
          percent={Math.round(run.score_coverage_pct ?? 0)}
          size="small"
          status={run.score_coverage_pct == null ? "normal"
            : run.score_coverage_pct >= 95 ? "success"
              : run.score_coverage_pct >= 80 ? "active" : "exception"}
          strokeColor={run.score_coverage_pct == null ? undefined
            : run.score_coverage_pct >= 95 ? "#16a34a"
              : run.score_coverage_pct >= 80 ? "#0ea5e9" : "#dc2626"}
        />
      </div>
      <div>
        <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)", marginBottom: 4 }}>
          Score 最大断档天数 / Score 新鲜度
        </div>
        <Text style={{
          color: (run.score_max_age_days ?? 0) <= 1 ? "#16a34a"
            : (run.score_max_age_days ?? 0) <= 3 ? "#ca8a04" : "#dc2626",
          fontFamily: "var(--pt-font-mono)",
        }}>
          {run.score_max_age_days ?? "-"} 天
          {" · "}
          {run.score_count_actual ?? "-"} / {run.score_count_expected ?? "?"} 实际/预期
        </Text>
      </div>
    </div>
  ) : null;

  /* ------- 证据列表列定义 ------- */
  const evColumns: ColumnsType<DecisionEvidenceRead> = useMemo(() => {
    const cols: ColumnsType<DecisionEvidenceRead> = [
      {
        title: "标的",
        key: "symbol",
        width: 160,
        fixed: "left",
        render: (_, rec) => {
          const s = symbolOf(rec.symbol_id);
          return (
            <div style={{ lineHeight: 1.3 }}>
              <Text strong style={{ fontFamily: "var(--pt-font-mono)", fontSize: 13 }}>
                {s.symbol}
              </Text>
              {s.name ? (
                <div style={{ fontSize: 11, color: "var(--pt-muted-foreground)", marginTop: 2 }}>{s.name}</div>
              ) : null}
            </div>
          );
        },
      },
      {
        title: "动作",
        dataIndex: "action",
        key: "action",
        width: 150,
        render: (a: DecisionAction) => actionTag(a),
      },
      {
        title: "分数/排名",
        key: "score",
        width: 160,
        render: (_, rec) => (
          <div style={{ lineHeight: 1.4 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <Activity size={11} style={{ color: "var(--pt-muted-foreground)" }} />
              <Text style={{ fontFamily: "var(--pt-font-mono)", fontSize: 12 }}>
                {rec.score_value != null ? Number(rec.score_value).toFixed(4) : "-"}
              </Text>
              <span style={{ color: "var(--pt-muted-foreground)", fontSize: 11 }}>
                {rec.score_rank != null ? ` #${rec.score_rank}` : ""}
              </span>
            </div>
            <div style={{ marginTop: 3, display: "flex", alignItems: "center", gap: 6 }}>
              {pitBadge(rec.pit_safe_flag)}
              {rec.legacy_fallback_flag ? (
                <Tag color="orange" style={{ fontSize: 11, padding: "0 6px", margin: 0 }}>legacy_score</Tag>
              ) : null}
            </div>
          </div>
        ),
      },
      {
        title: "目标仓位/数量",
        key: "target",
        width: 180,
        render: (_, rec) => {
          const execution = orderPlanExecutionSummary(rec.versions_json);
          return (
            <div style={{ lineHeight: 1.5 }}>
              <div style={{ fontSize: 12, color: "var(--pt-foreground)" }}>
                仓位：
                <span style={{ fontFamily: "var(--pt-font-mono)" }}>
                  {rec.target_position_pct != null
                    ? `${(rec.target_position_pct * 100).toFixed(2)}%`
                    : "-"}
                </span>
              </div>
              <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                数量：
                <span style={{ fontFamily: "var(--pt-font-mono)" }}>
                  {rec.target_quantity != null ? `${Number(rec.target_quantity).toFixed(0)}` : "-"}
                </span>
                <span style={{ marginLeft: 6, fontSize: 11 }}>(lot {rec.min_lot_size ?? 100})</span>
              </div>
              {execution && (
                <div
                  data-testid={`decision-evidence-order-plan-${rec.id}`}
                  style={{ marginTop: 5, paddingTop: 5, borderTop: "1px solid var(--pt-border)", fontSize: 11 }}
                >
                  <div style={{ fontFamily: "var(--pt-font-mono)", color: "var(--pt-foreground)" }}>
                    计划 {formatOrderPlanQuantity(execution.requestedQuantity)}
                    {" · "}成交 {formatOrderPlanQuantity(execution.filledQuantity)}
                    {execution.remainingQuantity != null ? ` · 剩余 ${formatOrderPlanQuantity(execution.remainingQuantity)}` : ""}
                  </div>
                  {(execution.status || execution.unfilledReason) && (
                    <div style={{ color: "var(--pt-muted-foreground)" }}>
                      {execution.status ? orderPlanStatusLabel(execution.status) : ""}
                      {execution.status && execution.unfilledReason ? " · " : ""}
                      {execution.unfilledReason ? `原因 ${execution.unfilledReason}` : ""}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        },
      },
      {
        title: "意图价/执行价/滑点",
        key: "price",
        width: 200,
        render: (_, rec) => (
          <div style={{ lineHeight: 1.5, fontSize: 12 }}>
            <div>
              意图：
              <span style={{ fontFamily: "var(--pt-font-mono)" }}>
                {rec.intended_price != null ? Number(rec.intended_price).toFixed(3) : "-"}
              </span>
              {" · "}执行：
              <span style={{ fontFamily: "var(--pt-font-mono)" }}>
                {rec.executed_price != null ? Number(rec.executed_price).toFixed(3) : "-"}
              </span>
            </div>
            <div style={{ color: "var(--pt-muted-foreground)", marginTop: 2 }}>
              滑点：
              <span style={{
                fontFamily: "var(--pt-font-mono)",
                color: (rec.slippage_bps ?? 0) > 10 ? "#dc2626" : "inherit",
              }}>
                {rec.slippage_bps != null ? `${Number(rec.slippage_bps).toFixed(1)} bps` : "-"}
              </span>
              <span style={{ marginLeft: 8, fontSize: 11 }}>
                match={rec.match_mode || "NEXT_OPEN"}
              </span>
            </div>
          </div>
        ),
      },
    ];

    // REJECTED / DATA_BLOCKED 专用拒绝原因列
    if (activeAction === "REJECTED" || activeAction === "DATA_BLOCKED" || activeAction === "ALL") {
      cols.push({
        title: activeAction === "DATA_BLOCKED" ? "阻断原因" : "拒绝原因",
        key: "reject",
        width: 260,
        render: (_, rec) => {
          if (rec.action !== "REJECTED" && rec.action !== "DATA_BLOCKED") return null;
          return (
            <Tooltip title={rec.rejection_detail || rec.rejection_reason || "—"}>
              <div style={{ lineHeight: 1.4 }}>
                <div style={{
                  fontSize: 12,
                  color: rec.action === "DATA_BLOCKED" ? "#7e22ce" : "#b45309",
                  fontWeight: 600,
                }}>
                  <AlertTriangle size={11} style={{ marginRight: 4, verticalAlign: "-2px" }} />
                  {rec.rejection_reason || rec.action_subtype || "未标注原因"}
                </div>
                {rec.rejection_detail ? (
                  <div style={{
                    fontSize: 11,
                    color: "var(--pt-muted-foreground)",
                    marginTop: 2,
                    whiteSpace: "nowrap",
                    textOverflow: "ellipsis",
                    overflow: "hidden",
                    maxWidth: 240,
                  }}>
                    {rec.rejection_detail}
                  </div>
                ) : null}
                {rec.stop_loss_triggered ? (
                  <Tag style={{ margin: "4px 0 0", padding: "0 6px", fontSize: 11 }} color="red">
                    止损触发
                    {rec.stop_loss_verified_price_source ? ` · ${rec.stop_loss_verified_price_source}` : ""}
                  </Tag>
                ) : null}
              </div>
            </Tooltip>
          );
        },
      });
    }

    // 卖出动作：SELL/HOLD 展示止损与卖出规则（exit_rules_hit_json）
    if (activeAction === "SELL" || activeAction === "ALL") {
      cols.push({
        title: "卖出裁决/子类型",
        key: "sellSubtype",
        width: 180,
        render: (_, rec) => {
          if (rec.action !== "SELL") return null;
          return (
            <div style={{ fontSize: 12, lineHeight: 1.5 }}>
              <div>
                <Badge status="processing" color="#dc2626" />
                <span style={{ marginLeft: 4 }}>{rec.action_subtype || "—"}</span>
              </div>
              {rec.stop_loss_triggered ? (
                <div style={{ marginTop: 2, color: "#dc2626" }}>
                  <Zap size={10} /> 止损命中 · {rec.stop_loss_verified_price_source || "—"}
                </div>
              ) : null}
            </div>
          );
        },
      });
    }
    return cols;
  }, [symbolOf, activeAction]);

  /* ------- 证据行展开：因子贡献 / 约束步骤 / 原因码 ------- */
  const expandedRowRender = useCallback((rec: DecisionEvidenceRead) => {
    const panels: Array<{ key: string; label: React.ReactNode; children: React.ReactNode }> = [];
    const ruleComparisons = ruleComparisonsFromVersions(rec.versions_json);
    if (ruleComparisons.length > 0) {
      panels.push({
        key: "rules",
        label: (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <ShieldCheck size={12} style={{ color: "#16a34a" }} />
            规则比较 rule_comparisons
          </span>
        ),
        children: (
          <div data-testid={`decision-evidence-rule-comparisons-${rec.id}`} style={{ overflowX: "auto" }}>
            <table className="pt-table" style={{ minWidth: 620, fontSize: 12 }}>
              <thead>
                <tr><th>规则</th><th>实际值</th><th>操作符</th><th>阈值</th><th>通过</th></tr>
              </thead>
              <tbody>
                {ruleComparisons.map((comparison, index) => {
                  const passed = comparison.passed ?? comparison.result ?? comparison.pass;
                  return (
                    <tr key={`${String(comparison.rule_code ?? comparison.rule_id ?? index)}-${index}`}>
                      <td className="pt-mono">{ruleComparisonValue(comparison.rule_code ?? comparison.rule_id ?? comparison.name)}</td>
                      <td className="pt-mono">{ruleComparisonValue(comparison.actual_value ?? comparison.actual)}</td>
                      <td className="pt-mono">{ruleComparisonValue(comparison.operator ?? comparison.op)}</td>
                      <td className="pt-mono">{ruleComparisonValue(comparison.threshold ?? comparison.expected)}</td>
                      <td style={{ color: passed === true ? "var(--pt-state-success)" : passed === false ? "var(--pt-state-error)" : "var(--pt-muted-foreground)" }}>
                        {passed === true ? "通过" : passed === false ? "未通过" : "--"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ),
      });
    }
    const hasFactor = rec.factor_contributions_json &&
      (Array.isArray(rec.factor_contributions_json) ? rec.factor_contributions_json.length > 0
        : Object.keys(rec.factor_contributions_json).length > 0);
    if (hasFactor) {
      panels.push({
        key: "factor",
        label: (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <BarChart3 size={12} style={{ color: "#0ea5e9" }} />
            因子归因 factor_contributions_json
          </span>
        ),
        children: <PrettyJson data={rec.factor_contributions_json} />,
      });
    }
    if (rec.constraints_json && (Array.isArray(rec.constraints_json)
      ? rec.constraints_json.length > 0
      : Object.keys(rec.constraints_json).length > 0)) {
      panels.push({
        key: "constraints",
        label: (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Layers size={12} style={{ color: "#6d28d9" }} />
            顺序约束 clamping 轨迹 constraints_json
          </span>
        ),
        children: <PrettyJson data={rec.constraints_json} />,
      });
    }
    if (rec.reason_codes_json && (Array.isArray(rec.reason_codes_json)
      ? rec.reason_codes_json.length > 0
      : Object.keys(rec.reason_codes_json).length > 0)) {
      panels.push({
        key: "reason",
        label: (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Info size={12} style={{ color: "#b45309" }} />
            裁决原因码 reason_codes_json
          </span>
        ),
        children: <PrettyJson data={rec.reason_codes_json} />,
      });
    }
    if (rec.versions_json && Object.keys(rec.versions_json).length > 0) {
      panels.push({
        key: "versions",
        label: (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Hash size={12} style={{ color: "#64748b" }} />
            版本追溯 versions_json
          </span>
        ),
        children: <PrettyJson data={rec.versions_json} />,
      });
    }
    if (panels.length === 0) {
      return (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
              无 factor_contributions / constraints / reason_codes / versions 证据
            </span>
          }
          style={{ padding: "12px 0" }}
        />
      );
    }
    return (
      <div
        data-testid={`decision-evidence-expanded-${rec.id}`}
        style={{ padding: "4px 12px 12px", background: "rgba(148,163,184,0.05)", borderRadius: 8, marginTop: -8 }}
      >
        <Collapse
          ghost
          size="small"
          defaultActiveKey={panels.map((p) => p.key)}
          items={panels.map((p) => ({
            key: p.key,
            label: p.label,
            children: p.children,
          }))}
        />
      </div>
    );
  }, []);

  /* ------- Tab 项：ALL + 6 种 action ------- */
  const allActions: (DecisionAction | "ALL")[] = [
    "ALL", "BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED",
  ];
  const tabItems = allActions.map((a) => {
    const count = actionCounts[a] || 0;
    const tabLabel = (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
        {a === "ALL" ? (
          <><FileSearch size={12} />全部</>
        ) : (
          <>{ACTION_META[a as DecisionAction].icon}{ACTION_META[a as DecisionAction].label.split(" ")[0]}</>
        )}
        <Badge
          count={count}
          size="small"
          style={{ backgroundColor: a === "ALL" ? "#0ea5e9" : ACTION_META[a as DecisionAction]?.color }}
        />
      </span>
    );
    return {
      key: a,
      label: tabLabel,
      children: (
        <div style={{ marginTop: 12 }}>
          {filteredEvidence.length === 0 && !loadingEvidence ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<span style={{ color: "var(--pt-muted-foreground)", fontSize: 12 }}>暂无该类决策证据</span>}
              style={{ padding: "24px 0" }}
            />
          ) : (
            <Table<DecisionEvidenceRead>
              size="small"
              rowKey="id"
              loading={loadingEvidence}
              columns={evColumns}
              dataSource={filteredEvidence}
              rowClassName={(record) => (
                activeEvidenceId != null && String(record.id) === String(activeEvidenceId)
                  ? "pt-evidence-row-selected"
                  : ""
              )}
              onRow={(record) => {
                const selected = activeEvidenceId != null
                  && String(record.id) === String(activeEvidenceId);
                return {
                  "data-testid": `decision-evidence-row-${record.id}`,
                  "aria-current": selected ? "true" : undefined,
                  style: selected ? { background: "rgba(14,165,233,0.10)" } : undefined,
                };
              }}
              pagination={{
                current: evidencePage,
                pageSize: evidencePageSize,
                total: evidenceTotal,
                showSizeChanger: false,
                onChange: (page) => setEvidencePage(page),
                showTotal: (t) => `${t} 条 · 每页 ${evidencePageSize} 条`,
              }}
              scroll={{ x: 1200 }}
              expandable={{
                expandedRowRender,
                expandedRowKeys,
                onExpandedRowsChange: (rows) =>
                  setExpandedRowKeys(Array.isArray(rows) ? rows as React.Key[] : []),
                expandIcon: ({ expanded, onExpand, record }) => (
                  <button
                    type="button"
                    data-testid={`decision-evidence-expand-${record.id}`}
                    aria-label={expanded ? "收起证据详情" : "展开证据详情"}
                    aria-expanded={expanded}
                    onClick={(event) => onExpand(record, event as any)}
                    style={{
                      width: 24,
                      height: 24,
                      padding: 0,
                      border: 0,
                      borderRadius: 4,
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      background: "transparent",
                      color: "var(--pt-muted-foreground)",
                      cursor: "pointer",
                    }}
                  >
                    {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  </button>
                ),
              }}
            />
          )}
        </div>
      ),
    };
  });

  /* ------- 决策时钟显示 ------- */
  function clockLine(label: string, iso: string | undefined | null) {
    if (!iso) return null;
    try {
      const d = new Date(iso);
      const sh = d.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
      return (
        <Descriptions.Item label={label} span={1}>
          <span style={{ fontFamily: "var(--pt-font-mono)", fontSize: 12 }}>{sh}</span>
        </Descriptions.Item>
      );
    } catch {
      return null;
    }
  }

  return (
    <div
      className="pt-drawer-mask"
      style={{
        position: "fixed", inset: 0,
        overflow: "hidden",
        background: open ? "rgba(15,23,42,0.35)" : "transparent",
        pointerEvents: open ? "auto" : "none",
        opacity: open ? 1 : 0,
        // opacity: 0 is still considered visible by Playwright and screen
        // readers; visibility makes the closed drawer genuinely hidden.
        visibility: open ? "visible" : "hidden",
        transition: "opacity 220ms ease",
        zIndex: 1300,
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      aria-hidden={!open}
    >
      <div
        className="pt-evidence-drawer-panel"
        style={{
          position: "absolute",
          top: 0, right: 0, bottom: 0,
          width: "min(520px, 100vw)",
          background: "var(--pt-background)",
          borderLeft: "1px solid var(--pt-border)",
          boxShadow: "-8px 0 32px rgba(15,23,42,0.12)",
          transform: open ? "translateX(0)" : "translateX(100%)",
          transition: "transform 260ms cubic-bezier(0.4, 0, 0.2, 1)",
          display: "flex", flexDirection: "column",
        }}
      >
        {/* 头部：标题 + 概要 */}
        <div
          className="pt-evidence-drawer-header"
          style={{
            padding: "14px 18px 12px",
            borderBottom: "1px solid var(--pt-border)",
            background: "linear-gradient(180deg, rgba(14,165,233,0.06), transparent)",
          }}
        >
          <div className="pt-evidence-drawer-header-row" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
              <div
                style={{
                  width: 34, height: 34, borderRadius: 10,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  background: "rgba(14,165,233,0.12)",
                  color: "var(--pt-state-info)",
                  flexShrink: 0,
                }}
              >
                <FileSearch size={18} />
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 15, fontWeight: 600, color: "var(--pt-foreground)" }}>
                  <span>决策证据 &amp; 归因</span>
                  {run && (
                    <Tag style={{ margin: 0 }} color={run.run_type === "auto_simulation" ? "blue" : run.run_type === "backtest" ? "purple" : "default"}>
                      {run.run_type}
                    </Tag>
                  )}
                  {run && (
                    <span
                      title={run.blocking_status}
                      style={{
                        fontSize: 12, padding: "2px 8px", borderRadius: 999,
                        color: blockingStatusColor(run.blocking_status),
                        background: blockingStatusColor(run.blocking_status) + "1A",
                        border: `1px solid ${blockingStatusColor(run.blocking_status)}55`,
                      }}
                    >
                      {run.blocking_status}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)", marginTop: 2 }}>
                  WP1-1 · DecisionRun + DecisionEvidence 端到端证据链
                  {run ? (
                    <span style={{ marginLeft: 8, fontFamily: "var(--pt-font-mono)" }}>
                      id={run.id.slice(0, 16)}…
                    </span>
                  ) : null}
                </div>
              </div>
            </div>
            <div className="pt-evidence-drawer-actions" style={{ display: "inline-flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
              <div
                aria-label="证据记录导航"
                style={{ display: "inline-flex", alignItems: "center", gap: 3 }}
              >
                <button
                  type="button"
                  onClick={() => moveEvidence(-1)}
                  aria-label="上一条证据"
                  title="上一条证据"
                  disabled={selectedEvidenceIndex <= 0}
                  style={{
                    border: "1px solid var(--pt-border)", borderRadius: 8,
                    width: 30, height: 32,
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                    background: "var(--pt-surface)", color: "var(--pt-muted-foreground)",
                    cursor: selectedEvidenceIndex > 0 ? "pointer" : "not-allowed",
                    opacity: selectedEvidenceIndex > 0 ? 1 : 0.5,
                  }}
                >
                  <ChevronLeft size={14} />
                </button>
                <span
                  data-testid="decision-evidence-position"
                  aria-live="polite"
                  style={{ minWidth: 38, textAlign: "center", fontSize: 11, color: "var(--pt-muted-foreground)" }}
                >
                  {selectedEvidenceIndex >= 0
                    ? `${selectedEvidenceIndex + 1}/${filteredEvidence.length}`
                    : "-/-"}
                </span>
                <button
                  type="button"
                  onClick={() => moveEvidence(1)}
                  aria-label="下一条证据"
                  title="下一条证据"
                  disabled={selectedEvidenceIndex < 0 || selectedEvidenceIndex >= filteredEvidence.length - 1}
                  style={{
                    border: "1px solid var(--pt-border)", borderRadius: 8,
                    width: 30, height: 32,
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                    background: "var(--pt-surface)", color: "var(--pt-muted-foreground)",
                    cursor: selectedEvidenceIndex >= 0 && selectedEvidenceIndex < filteredEvidence.length - 1
                      ? "pointer" : "not-allowed",
                    opacity: selectedEvidenceIndex >= 0 && selectedEvidenceIndex < filteredEvidence.length - 1 ? 1 : 0.5,
                  }}
                >
                  <ChevronRight size={14} />
                </button>
              </div>
              <button
                type="button"
                onClick={copyCurrentId}
                aria-label={copyTarget?.ariaLabel || "当前没有可复制的证据 ID 或 DecisionRun ID"}
                title={copyTarget?.ariaLabel || "当前没有可复制的证据 ID 或 DecisionRun ID"}
                disabled={!copyTarget}
                style={{
                  border: "1px solid var(--pt-border)", borderRadius: 8,
                  minWidth: 32, height: 32, padding: "0 8px",
                  display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 5,
                  background: "var(--pt-surface)",
                  color: "var(--pt-muted-foreground)",
                  cursor: copyTarget ? "pointer" : "not-allowed",
                  opacity: copyTarget ? 1 : 0.55,
                }}
              >
                <Copy size={14} />
                <span style={{ fontSize: 11 }}>{copyTarget?.buttonLabel || "复制 ID"}</span>
              </button>
              <button
                type="button"
                onClick={onClose}
                aria-label="关闭抽屉"
                ref={closeButtonRef}
                style={{
                  border: "1px solid var(--pt-border)", borderRadius: 8,
                  width: 32, height: 32,
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                  background: "var(--pt-surface)",
                  color: "var(--pt-muted-foreground)",
                  cursor: "pointer",
                }}
              >
                <X size={15} />
              </button>
            </div>
          </div>
          {copyError ? (
            <div
              role="alert"
              data-testid="decision-run-copy-error"
              style={{ marginTop: 8, color: "#dc2626", fontSize: 12 }}
            >
              {copyError}
            </div>
          ) : null}

          {/* 决策时钟 + Score 覆盖率区域 */}
          {run ? (
            <div style={{ marginTop: 12 }}>
              <Descriptions
                className="pt-evidence-summary"
                size="small" column={isNarrowViewport ? 1 : 3} bordered
                labelStyle={{
                  fontSize: 11, color: "var(--pt-muted-foreground)",
                  background: "rgba(148,163,184,0.06)",
                  padding: "6px 10px",
                }}
                contentStyle={{ fontSize: 12, padding: "6px 10px" }}
                style={{ fontSize: 12 }}
              >
                <Descriptions.Item label="交易日">
                  <span style={{ fontFamily: "var(--pt-font-mono)" }}>{run.trade_date}</span>
                </Descriptions.Item>
                <Descriptions.Item label="策略快照">
                  <span style={{ fontFamily: "var(--pt-font-mono)", fontSize: 11 }}>
                    {run.strategy_snapshot_id?.slice(0, 24) || "-"}
                    {run.strategy_snapshot_id && run.strategy_snapshot_id.length > 24 ? "…" : ""}
                  </span>
                </Descriptions.Item>
                <Descriptions.Item label="成员 / 全市场">
                  {run.member_count} / {run.universe_count}
                </Descriptions.Item>
                {clockLine("决策时间", run.decision_at)}
                {clockLine("数据截止", run.data_cutoff_at)}
                {clockLine("执行时间", run.execution_at)}
                <Descriptions.Item label="模式" span={1}>
                  <Tag style={{ margin: 0 }} color={run.run_mode === "production_pit" ? "red" : run.run_mode === "production_sim" ? "orange" : "default"}>
                    {run.run_mode}
                  </Tag>
                  <span style={{ marginLeft: 6, color: "var(--pt-muted-foreground)", fontSize: 11 }}>
                    {run.pit_mode}
                  </span>
                </Descriptions.Item>
                <Descriptions.Item label="耗时 / 可生产" span={1}>
                  {run.duration_ms != null ? `${run.duration_ms} ms` : "-"}
                  {" · "}
                  {run.is_result_production_eligible
                    ? <span style={{ color: "#16a34a" }}>符合生产</span>
                    : <span style={{ color: "#dc2626" }}>不符合生产</span>}
                </Descriptions.Item>
                <Descriptions.Item label="证据总条数" span={1}>
                  <span style={{ fontFamily: "var(--pt-font-mono)" }}>
                    {evidenceTotal}
                  </span>
                  <span style={{ marginLeft: 6, fontSize: 11, color: "var(--pt-muted-foreground)" }}>
                    （{run.member_count} 成员 × 单次决策）
                  </span>
                </Descriptions.Item>
              </Descriptions>

              <div style={{ marginTop: 12 }}>{scoreCoverageBar}</div>

              {/* blocking_reasons_json 若存在则用 Alert 渲染 */}
              {run.blocking_reasons_json &&
                (Array.isArray(run.blocking_reasons_json) ? run.blocking_reasons_json.length > 0
                  : Object.keys(run.blocking_reasons_json).length > 0) ? (
                <Alert
                  style={{ marginTop: 12 }}
                  type={run.blocking_status === "READY" ? "info" : "warning"}
                  showIcon
                  message={run.blocking_status === "READY" ? "门禁检查项（ready）" : `门禁阻断：${run.blocking_status}`}
                  description={<div style={{ marginTop: 4 }}><PrettyJson data={run.blocking_reasons_json} /></div>}
                />
              ) : null}

              {/* versions_json 若存在则折叠展示（版本溯源） */}
              {run.versions_json && Object.keys(run.versions_json).length > 0 ? (
                <Collapse ghost size="small" style={{ marginTop: 8 }}>
                  <Collapse.Panel
                    key="versions"
                    header={
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                        <Hash size={12} />版本溯源 versions_json（factor_model_run_id / factor_set_id / rule_id+version）
                      </span>
                    }
                  >
                    <PrettyJson data={run.versions_json} />
                  </Collapse.Panel>
                </Collapse>
              ) : null}
            </div>
          ) : (
            <div style={{
              marginTop: 12, display: "flex", alignItems: "center", gap: 8,
              padding: "14px 16px",
              border: "1px dashed rgba(14,165,233,0.4)",
              borderRadius: 8,
              color: "var(--pt-muted-foreground)",
              background: "rgba(14,165,233,0.04)",
            }}>
              {loadingRun ? <Loader2 size={16} className="pt-rotate" /> : <Info size={16} />}
              <Text type="secondary" style={{ fontSize: 12 }}>
                {loadingRun ? "正在从后端拉取 DecisionRun 元数据…"
                  : decisionRunId ? "等待加载决策运行数据…"
                    : "未传入 decisionRunId，未加载 DecisionRun。"}
              </Text>
            </div>
          )}
        </div>

        {/* 主体：Tabs + 证据表格 */}
        <div
          style={{
            flex: 1, minHeight: 0,
            padding: "8px 18px 14px",
            overflow: "auto",
          }}
        >
          <Tabs
            activeKey={activeAction}
            onChange={(k) => { setEvidencePage(1); setActiveAction(k as any); }}
            size="small"
            items={tabItems}
            tabBarStyle={{ margin: 0 }}
            style={{ width: "100%" }}
          />
        </div>

        {/* 底部：数据一致性条（WP1-1 审计语义） */}
        <div
          className="pt-evidence-footer"
          style={{
            borderTop: "1px solid var(--pt-border)",
            padding: "8px 18px",
            background: "rgba(148,163,184,0.04)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            fontSize: 11,
            color: "var(--pt-muted-foreground)",
          }}
        >
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <ShieldCheck size={12} />
            <span>证据内容哈希 content_hash 由后端持久化，修改会触发 G3 对账告警</span>
          </div>
          <div style={{ fontFamily: "var(--pt-font-mono)" }}>
            {run?.created_at ? new Date(run.created_at).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }) : ""}
          </div>
        </div>
      </div>
    </div>
  );
};

export default DecisionEvidenceDrawer;
