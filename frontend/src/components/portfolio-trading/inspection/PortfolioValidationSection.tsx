import { useCallback, useId, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, CircleHelp, Clock3, History, ListChecks, ShieldAlert, ShieldCheck } from "lucide-react";
import { AcceptanceButton as Button } from "./AcceptanceControls";
import { t } from "../../../i18n";
import { PORTFOLIO_STATE_ALLOWANCE_MATRIX } from "../../../types";
import type { ActionItem, PortfolioStatePermissions, PortfolioStatus, PortfolioStatusResponse } from "../../../types";
import type { InspectionJob, InspectionReconciliationQueue, InspectionRuntime } from "../../../types/portfolioInspection";
import type { AdmissionKind } from "../../../utils/portfolioAdmission";
import { KNOWN_RECON_DIFF_KINDS, reconDimLabel } from "../../../utils/reconDimLabel";

/* ========================================================================== */
/* 组合校验（对应原型 06-portfolio-governance）                                */
/*                                                                            */
/* 结构严格照原型：状态横幅 → 分段导航 → 卡片头 + 全幅表格 → 口径说明。        */
/* 五段视图：组合状态 / 权限矩阵 / 对账连续性 / 行动项 / 操作记录。             */
/* 操作记录只给入口（复用壳层的审计抽屉），本页不重复拉一遍流水。              */
/* 主题沿用组合交易域深色令牌（--pt-*），只搬原型的层级、密度与交互细节。      */
/* 数据来源不变：admission（运行检查）+ status（状态机）+ 自动对账队列/作业   */
/* + 服务端聚合的行动项；不新增接口、不写库。                                  */
/* ========================================================================== */

/** 状态展示顺序：可运行 → 暂态 → 各类阻断 → 一键刹车（与状态机的语义递进一致）。 */
const STATUS_ORDER: PortfolioStatus[] = [
  "READY",
  "PENDING_INITIAL_REVIEW",
  "RUNNING_AUTO_SIMULATION",
  "RUNNING_BACKTEST",
  "DATA_INCOMPLETE_PAUSED",
  "SCORE_STALE",
  "RECONCILIATION_BLOCKED",
  "MODEL_INACTIVE",
  "INTERRUPTED",
  "ADMIN_PAUSED",
];

/**
 * 静态允许表直接用契约常量（types/PORTFOLIO_STATE_ALLOWANCE_MATRIX）。
 * 该常量已按状态机口径校正过 ADMIN_PAUSED 的风险退出（RISK_SELL 任何状态允许），
 * 这里不再做局部覆盖，避免前端出现第二份矩阵。
 */
const STATE_ALLOWANCE: Record<PortfolioStatus, PortfolioStatePermissions> = PORTFOLIO_STATE_ALLOWANCE_MATRIX;

/**
 * 「中文含义」列只放名词态名称（照原型：就绪 / 谨慎 / 行情过期 …）。
 * 完整口径仍是 portfolioStatus.*.desc（整句，进「触发条件」列），
 * 而 portfolioStatus.* 那些运营口径的短句（如 READY=「可以运行」）只进
 * tooltip —— 静态参考表不该替当前组合宣告「可以运行」。
 */
const STATE_NAME: Record<PortfolioStatus, string> = {
  READY: "就绪",
  PENDING_INITIAL_REVIEW: "待初审",
  RUNNING_AUTO_SIMULATION: "自动模拟中",
  RUNNING_BACKTEST: "回测中",
  DATA_INCOMPLETE_PAUSED: "数据缺失暂停",
  SCORE_STALE: "评分待更新",
  RECONCILIATION_BLOCKED: "对账阻断",
  MODEL_INACTIVE: "模型待处理",
  INTERRUPTED: "运行中断",
  ADMIN_PAUSED: "本人暂停",
};

type ViewKey = "state" | "permissions" | "continuity" | "actions" | "audit";
const VIEW_ORDER: ViewKey[] = ["state", "permissions", "continuity", "actions", "audit"];
const VIEW_LABELS: Record<ViewKey, string> = {
  state: "组合状态",
  permissions: "权限矩阵",
  continuity: "对账连续性",
  actions: "行动项",
  audit: "操作记录",
};

/** 逐日核对结论 → 徽章口径。取值来自服务端 verdict，未识别值原样降级为灰底。 */
const DAY_VERDICT: Record<string, { label: string; tone: string }> = {
  PASSED: { label: "已对平", tone: "is-pass" },
  BLOCKED: { label: "有差异待处理", tone: "is-fail" },
  DIFF_FOUND: { label: "有差异待处理", tone: "is-fail" },
  EVIDENCE_INCOMPLETE: { label: "证据不完整", tone: "is-warn" },
  WAITING_EXECUTION: { label: "等待执行证据", tone: "is-warn" },
  EXECUTION_IN_PROGRESS: { label: "执行中", tone: "is-warn" },
  NOT_DUE: { label: "尚未到期", tone: "is-unknown" },
  NOT_CHECKED: { label: "尚未核对", tone: "is-unknown" },
  HISTORICAL_EVIDENCE_UNKNOWN: { label: "历史证据未知", tone: "is-unknown" },
  READ_ERROR: { label: "读取失败", tone: "is-fail" },
};

/** 作业状态（异步任务 + 逐日工作项）→ 中文。未知值原样返回，不猜。 */
const WORK_STATE: Record<string, string> = {
  queued: "排队中",
  running: "核对中",
  waiting_data: "等待数据",
  waiting_execution: "等待执行",
  needs_review: "需要人工核查",
  done: "已完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  history_unknown: "历史未知",
};

const SEVERITY_META: Record<string, { label: string; tone: string }> = {
  blocker: { label: "高", tone: "is-fail" },
  warning: { label: "中", tone: "is-warn" },
  info: { label: "低", tone: "is-unknown" },
};

const SOURCE_LABEL: Record<string, string> = {
  readiness: "自动交易门禁",
  hg1_status: "组合状态机",
  g7_ops: "引擎运维",
};

/** 服务端 reason 里可能直接带差异类型枚举：命中索引的换成中文，未收录的原样保留。 */
const humanReason = (reason: string) => {
  const parts = (reason ?? "").split(/[；;]/).map((part) => part.trim()).filter(Boolean);
  if (!parts.length) return "";
  return parts.map((part) => (KNOWN_RECON_DIFF_KINDS.includes(part) ? reconDimLabel(part) : part)).join("；");
};

/** 报告 id 是 UUID，表格里只给可核对的前缀，完整值进 tooltip。 */
const shortId = (value: string | null | undefined) =>
  value ? (value.length > 12 ? `${value.slice(0, 10)}…` : value) : null;

/** 服务端生成时间是 UTC（ISO + Z），界面统一按本地时区显示，避免把 UTC 当成本地时刻。 */
const localTime = (value: string | null | undefined) => {
  if (!value) return null;
  const at = new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
};

const TONE_BY_KIND: Record<AdmissionKind, "ok" | "warn" | "danger"> = {
  allowed: "ok",
  blocked: "warn",
  review: "warn",
  waiting: "warn",
  checking: "danger",
  unknown: "danger",
};

type Flag = boolean | null;

const flagText = (value: Flag, yes: string, no: string) =>
  value === true ? yes : value === false ? no : "未知";

const flagTone = (value: Flag, flavor: "pass-fail" | "ack" = "pass-fail") => {
  if (value === null) return "is-unknown";
  if (flavor === "ack") return value ? "is-warn" : "is-pass";
  return value ? "is-pass" : "is-fail";
};

const Pill: React.FC<{
  label: string;
  value: React.ReactNode;
  tone: string;
  ariaLabel: string;
  title?: string;
  compact?: boolean;
}> = ({ label, value, tone, ariaLabel, title, compact }) => (
  <span
    className={`pi-verify-pill ${tone}${compact ? " pi-verify-pill--sm" : ""}`}
    aria-label={ariaLabel}
    title={title}
  >
    <span className="pi-verify-pill-label">{label}</span>
    <b>{value}</b>
  </span>
);

const AllowCell: React.FC<{ value: Flag; ack?: boolean }> = ({ value, ack }) => {
  if (ack) {
    return (
      <Pill
        compact
        label=""
        ariaLabel="需要人工确认"
        title={value === true ? "该状态下需要本人确认后才能继续" : undefined}
        value={flagText(value, "需要", "不需要")}
        tone={flagTone(value, "ack")}
      />
    );
  }
  return (
    <Pill
      compact
      label=""
      ariaLabel={value === true ? "允许" : value === false ? "不允许" : "未知"}
      value={flagText(value, "是", "否")}
      tone={flagTone(value)}
    />
  );
};

export interface PortfolioValidationSectionProps {
  portfolioId: number;
  /** 运行检查给出的准入结论（新买入已暂停 / 当前运行条件已满足 …） */
  title: string;
  description: string;
  kind: AdmissionKind;
  loading: boolean;
  runtime: InspectionRuntime | null;
  status: PortfolioStatusResponse | null;
  primaryLabel: string;
  primaryDisabled?: boolean;
  onPrimary: () => void;
  /** 自动对账逐日队列（服务端连续覆盖口径），未读取时为 null */
  queue: InspectionReconciliationQueue | null;
  /** 自动对账作业本身：启用状态、上次/下次运行、逐日工作项与重试痕迹 */
  job: InspectionJob | null;
  /** 服务端聚合的行动项（派生清单，不做本地增删） */
  actionItems: ActionItem[];
  /** 行动项清单的服务端生成时间（原样透传，界面按本地时区展示） */
  actionItemsGeneratedAt?: string | null;
  /** 行动项「直达」：沿用壳层既有的面板/子页导航 */
  onOpenAction: (item: ActionItem) => void;
  /** 操作记录入口：沿用壳层既有的审计抽屉，不在本页另起一条流水 */
  onOpenHistory: () => void;
  /** 逐日表点某一行 → 进入该交易日的每日对账（复用既有核对抽屉） */
  onInspectDay: (tradeDate: string) => void;
  /** 队列读不到时，在面板内重读（不必绕去页头） */
  onReloadQueue: () => void;
  /** 没有可点日期时的兜底入口：直接打开对账面板（不指定交易日） */
  onOpenReconciliation: () => void;
}

export default function PortfolioValidationSection({
  portfolioId,
  title,
  description,
  kind,
  loading,
  runtime,
  status,
  primaryLabel,
  primaryDisabled,
  onPrimary,
  queue,
  job,
  actionItems,
  actionItemsGeneratedAt: itemsGeneratedAt,
  onOpenAction,
  onOpenHistory,
  onInspectDay,
  onReloadQueue,
  onOpenReconciliation,
}: PortfolioValidationSectionProps) {
  const [view, setView] = useState<ViewKey>("state");
  /** 面板首次进入才挂载：首屏不必为 5 个视图同时构建几百行 DOM，也避免同一行动项在隐藏面板里重复出现在可访问树里 */
  const [visited, setVisited] = useState<ViewKey[]>(["state"]);
  /** 阻断项可能几十条（逐日对账会一天一条）：先给前 8 条，其余按需展开 */
  const [showAllActions, setShowAllActions] = useState(false);
  const selectView = useCallback((next: ViewKey) => {
    setView(next);
    setVisited((previous) => (previous.includes(next) ? previous : [...previous, next]));
  }, []);
  const tabRefs = useRef<Record<ViewKey, HTMLButtonElement | null>>({ state: null, permissions: null, continuity: null, actions: null, audit: null });
  const baseId = useId();

  /**
   * 「重新检测」与每 30 秒的后台刷新都会把运行检查置为 checking（runtime 瞬时为 null）。
   * 若直接按 checking 渲染，整块会在 1 秒内闪成红色 + 四枚胶囊变「未知」+ 当前行消失，
   * 每半分钟闪一次。这里保留最近一次**已核实**的快照，checking 期间继续展示它，
   * 只加一句「核实中」；真正的读取失败（unknown / error）依旧按未知处理，不享受该兜底。
   */
  const lastGood = useRef<{
    runtime: InspectionRuntime; kind: AdmissionKind; title: string;
    description: string; primaryLabel: string; primaryDisabled?: boolean;
  } | null>(null);
  if (runtime && kind !== "checking" && kind !== "unknown") {
    lastGood.current = { runtime, kind, title, description, primaryLabel, primaryDisabled };
  }
  const refreshing = kind === "checking" && Boolean(lastGood.current);
  const snapshot = refreshing && lastGood.current ? lastGood.current : null;
  const effectiveKind = snapshot ? snapshot.kind : kind;
  const effectiveTitle = snapshot ? snapshot.title : title;
  const effectiveDescription = snapshot ? snapshot.description : description;
  const effectivePrimaryLabel = snapshot ? snapshot.primaryLabel : primaryLabel;
  const effectivePrimaryDisabled = snapshot ? snapshot.primaryDisabled : primaryDisabled;
  const effectiveRuntime = snapshot ? snapshot.runtime : runtime;

  /**
   * 当前命中的状态只在「运行检查读得通」时才敢标。
   * 运行检查不可读（unknown）而持久化 /status 仍有值时，不能拿旧状态
   * 在矩阵里钉一个「当前」，否则横幅说「待核实」、表格说「就是它」，两句话打架。
   * 取值也以运行检查为准（utils/portfolioAdmission 的既定口径：运行检查是唯一展示权威，
   * 持久化状态只作审计与兜底），避免 /status 落后时横幅与矩阵互相矛盾。
   */
  const currentVerified = effectiveKind !== "unknown" && effectiveKind !== "checking";
  const current = (currentVerified ? effectiveRuntime?.current_state ?? status?.current_state ?? null : null) as PortfolioStatus | null;
  const currentKnown = Boolean(current && (STATUS_ORDER as string[]).includes(current));
  const permissions = effectiveRuntime?.permissions;
  const reasons = effectiveRuntime?.capability_reasons ?? {};

  const reasonOf = useCallback(
    (key: string, fallback: string) => {
      const rows = reasons[key];
      return Array.isArray(rows) && rows.length ? rows.join("；") : fallback;
    },
    [reasons],
  );

  const order = useMemo<ViewKey[]>(() => VIEW_ORDER, []);
  const move = (from: ViewKey, step: number) => {
    const next = order[(order.indexOf(from) + step + order.length) % order.length];
    selectView(next);
    tabRefs.current[next]?.focus();
  };

  const tone = TONE_BY_KIND[effectiveKind];
  const BannerIcon = tone === "ok" ? ShieldCheck : tone === "warn" ? AlertTriangle : ShieldAlert;

  const needsReview = permissions?.requires_manual_ack ?? null;
  const canConfirm = effectiveRuntime?.recovery_summary?.can_confirm ?? null;

  const permissionRows: { action: string; value: Flag; ack: boolean; condition: string; scope: string }[] = [
    {
      action: "新建买入单",
      value: permissions?.allow_new_buys ?? null,
      ack: false,
      condition: reasonOf("allow_new_buys", "读取运行检查后确认是否允许新买入"),
      scope: "当前组合",
    },
    {
      action: "风险退出单",
      value: permissions?.allow_risk_exits ?? null,
      ack: false,
      condition: reasonOf("allow_risk_exits", "读取运行检查后确认是否允许风险退出"),
      scope: "当前组合",
    },
    {
      action: "自动恢复",
      value: permissions?.allow_auto_recovery ?? null,
      ack: false,
      condition: reasonOf("allow_auto_recovery", "读取运行检查后确认能否自动恢复"),
      scope: "当前组合",
    },
    {
      action: "本人审核确认",
      value: needsReview,
      ack: true,
      condition:
        canConfirm === null
          ? "审核前置条件尚未读取"
          : canConfirm
            ? "前置条件已满足，可提交本人审核"
            : (effectiveRuntime?.recovery_summary?.blockers ?? []).slice(0, 3).join("；") || "仍有未通过或未知的前置条件",
      scope: "本次审核",
    },
  ];

  /* --- 对账连续性：只用服务端队列事实，不推算差异金额 --------------------- */
  const days = queue?.days ?? [];
  const openDays = days.filter((day) => day.status !== "PASSED" && !day.history_only);
  const gapCount = openDays.length;
  /**
   * 这张表要能当「每日对账」用，排序必须服务"现在能核对哪天"：
   *   0 目标日（最早未通过日，唯一能动手的那天）→ 置顶并高亮
   *   1 目标日之后的待核对日（按日期升序，体现"不跳日"）→ 依次排
   *   2 已对平日（倒序，作为最近结果）
   * 「历史未知」日（legacy 期间没有完整结论的旧日期）不进这张表：
   * 它们既不参与当前核对，又会把目标日挤出可见范围，单独计数提示走历史未知入口。
   */
  const targetDate = queue?.target_trade_date ?? null;
  const visibleDays = days.filter((day) => !day.history_only && day.status !== "HISTORICAL_EVIDENCE_UNKNOWN");
  const hiddenHistoryDays = days.length - visibleDays.length;
  const recentDays = useMemo(() => {
    const rank = (day: InspectionReconciliationQueue["days"][number]) =>
      day.trade_date === targetDate ? 0 : day.status === "PASSED" ? 2 : 1;
    return [...visibleDays]
      .sort((a, b) => {
        const diff = rank(a) - rank(b);
        if (diff !== 0) return diff;
        return rank(a) === 2 ? b.trade_date.localeCompare(a.trade_date) : a.trade_date.localeCompare(b.trade_date);
      })
      .slice(0, 12);
  }, [visibleDays, targetDate]);
  const workByDate = useMemo(() => {
    const map = new Map<string, NonNullable<InspectionJob["work_items"]>[number]>();
    for (const row of job?.work_items ?? []) map.set(row.trade_date, row);
    return map;
  }, [job]);
  const jobEnabled = job?.enabled ?? null;
  const summary = job?.check_summary;

  /* --- 行动项：服务端派生清单，按严重级别稳定排序 ------------------------- */
  const actions = useMemo(() => {
    const rank: Record<string, number> = { blocker: 0, warning: 1, info: 2 };
    return [...actionItems].sort((a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9));
  }, [actionItems]);
  const actionCount = {
    blocker: actions.filter((item) => item.severity === "blocker").length,
    warning: actions.filter((item) => item.severity === "warning").length,
    info: actions.filter((item) => item.severity === "info").length,
  };
  const ACTION_PAGE = 8;
  const visibleActions = showAllActions ? actions : actions.slice(0, ACTION_PAGE);

  return (
    <section className="pi-verify" aria-label="组合校验" data-testid="pi-validation">
      {/* ① 状态横幅：先讲结论，再讲能不能动手 */}
      <div className="pi-verify-banner" data-tone={tone} data-admission={effectiveKind}>
        <BannerIcon size={20} aria-hidden="true" />
        <div className="pi-verify-banner-main">
          <p className="pi-verify-kicker">
            当前状态 · 组合 #{portfolioId}
            {refreshing && <span className="pi-verify-refreshing">核实中…</span>}
          </p>
          <h1>{effectiveTitle}</h1>
          <p className="pi-verify-reason">{effectiveDescription}</p>
        </div>
        <div className="pi-verify-pills">
          <Pill
            label="新买单"
            ariaLabel="新买入权限"
            value={flagText(permissions?.allow_new_buys ?? null, "允许", "不允许")}
            tone={flagTone(permissions?.allow_new_buys ?? null)}
          />
          <Pill
            label="风险退出"
            ariaLabel="风险退出权限"
            value={flagText(permissions?.allow_risk_exits ?? null, "允许", "不允许")}
            tone={flagTone(permissions?.allow_risk_exits ?? null)}
          />
          <Pill
            label="自动恢复"
            ariaLabel="自动恢复权限"
            value={flagText(permissions?.allow_auto_recovery ?? null, "允许", "不允许")}
            tone={flagTone(permissions?.allow_auto_recovery ?? null)}
          />
          <Pill
            label="人工确认"
            ariaLabel="人工确认权限"
            value={flagText(needsReview, "需要", "不需要")}
            tone={flagTone(needsReview, "ack")}
          />
        </div>
      </div>

      {/* ② 分段导航 + 下一步（「重新检测」已上提到页头动作区） */}
      <div className="pi-verify-toolbar">
        <div className="pi-verify-seg" role="tablist" aria-label="组合校验视图">
          {order.map((key) => (
            <button
              key={key}
              type="button"
              role="tab"
              id={`${baseId}-tab-${key}`}
              aria-selected={view === key}
              aria-controls={`${baseId}-panel-${key}`}
              tabIndex={view === key ? 0 : -1}
              ref={(node) => {
                tabRefs.current[key] = node;
              }}
              onClick={() => selectView(key)}
              onKeyDown={(event) => {
                if (event.key === "ArrowRight") {
                  event.preventDefault();
                  move(key, 1);
                } else if (event.key === "ArrowLeft") {
                  event.preventDefault();
                  move(key, -1);
                } else if (event.key === "Home" || event.key === "End") {
                  event.preventDefault();
                  const next = event.key === "Home" ? order[0] : order[order.length - 1];
                  selectView(next);
                  tabRefs.current[next]?.focus();
                }
              }}
            >
              {VIEW_LABELS[key]}
            </button>
          ))}
        </div>
        <div className="pi-verify-toolbar-side">
          <span className="pi-verify-note">状态由运行检查结果驱动，每 30 秒自动刷新</span>
          <Button
            type="primary"
            size="small"
            disabled={effectivePrimaryDisabled}
            icon={<ArrowRight size={13} />}
            onClick={onPrimary}
          >
            {effectivePrimaryLabel}
          </Button>
        </div>
      </div>

      {/* ③ 组合状态 */}
      <div
        role="tabpanel"
        id={`${baseId}-panel-state`}
        data-testid="pi-validation-state"
        aria-labelledby={`${baseId}-tab-state`}
        hidden={view !== "state"}
      >
        <div className="pi-verify-card">
          <div className="pi-verify-card-head">
            <h3>
              状态矩阵
              <small>{STATUS_ORDER.length} 种状态 · 互斥，同一时刻仅命中一个</small>
            </h3>
            <span className="pi-verify-card-meta">
              当前命中
              <b className={currentKnown ? "pi-verify-current-code" : "pi-warning-text"}>
                {currentKnown ? current : "状态待核实"}
              </b>
            </span>
          </div>
          <div className="pi-verify-table-wrap">
            <table className="pi-verify-table">
              <colgroup>
                <col style={{ width: 208 }} />
                <col style={{ width: 124 }} />
                <col />
                <col style={{ width: 100 }} />
                <col style={{ width: 112 }} />
                <col style={{ width: 64 }} />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">状态码</th>
                  <th scope="col">中文含义</th>
                  <th scope="col">触发条件</th>
                  <th scope="col">允许新买单</th>
                  <th scope="col">允许风险退出</th>
                  <th scope="col">当前</th>
                </tr>
              </thead>
              <tbody>
                {STATUS_ORDER.map((code) => {
                  const row = STATE_ALLOWANCE[code];
                  const isCurrent = currentKnown && current === code;
                  return (
                    <tr key={code} className={isCurrent ? "is-current" : undefined} data-state={code}>
                      <td>
                        <span className="pi-verify-code">{code}</span>
                      </td>
                      <td className="pi-verify-name" title={t(`portfolioStatus.${code}` as const)}>
                        {STATE_NAME[code]}
                      </td>
                      <td className="pi-verify-cond" title={t(`portfolioStatus.${code}.desc` as const)}>
                        {t(`portfolioStatus.${code}.desc` as const)}
                      </td>
                      <td>
                        <AllowCell value={row.allow_new_buys} />
                      </td>
                      <td>
                        <AllowCell value={row.allow_risk_exits} />
                      </td>
                      <td>
                        {isCurrent ? (
                          <Pill compact label="" ariaLabel="当前状态" value="当前" tone="is-current" />
                        ) : (
                          <span className="pi-verify-dash" aria-hidden="true">
                            —
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        <div className="pi-verify-callout">
          <CircleHelp size={15} aria-hidden="true" />
          <div>
            <strong>怎么读这张表</strong>
            <p>
              一行是一种状态，左侧状态码是服务端状态机的真实枚举；「当前」列标出的那一行就是本组合此刻命中的状态。
              允许表只说明该状态下的门禁口径，具体委托仍需通过执行时门禁。状态只由服务端运行检查计算，前端不会按日期或旧审批推定。
            </p>
          </div>
        </div>
      </div>

      {/* ④ 权限矩阵 */}
      <div
        role="tabpanel"
        id={`${baseId}-panel-permissions`}
        data-testid="pi-validation-permissions"
        aria-labelledby={`${baseId}-tab-permissions`}
        hidden={view !== "permissions"}
      >
        {visited.includes("permissions") && (
          <>
        <div className="pi-verify-card">
          <div className="pi-verify-card-head">
            <h3>
              权限矩阵
              <small>当前状态下实际生效的四类动作</small>
            </h3>
            <span className="pi-verify-card-meta">
              {localTime(effectiveRuntime?.generated_at) ? `读取于 ${localTime(effectiveRuntime?.generated_at)}` : "尚未读取运行检查"}
            </span>
          </div>
          <div className="pi-verify-table-wrap">
            <table className="pi-verify-table pi-verify-table--perm">
              <colgroup>
                <col style={{ width: 132 }} />
                <col style={{ width: 96 }} />
                <col />
                <col style={{ width: 96 }} />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">动作</th>
                  <th scope="col">是否允许</th>
                  <th scope="col">限制条件</th>
                  <th scope="col">生效范围</th>
                </tr>
              </thead>
              <tbody>
                {permissionRows.map((row) => (
                  <tr key={row.action}>
                    <td className="pi-verify-name">{row.action}</td>
                    <td>
                      <AllowCell value={row.value} ack={row.ack} />
                    </td>
                    <td className="pi-verify-cond pi-verify-cond--clamp" title={row.condition}>
                      {row.condition}
                    </td>
                    <td className="pi-verify-scope">{row.scope}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="pi-verify-callout">
          {tone === "ok" ? <CheckCircle2 size={15} aria-hidden="true" /> : <AlertTriangle size={15} aria-hidden="true" />}
          <div>
            <strong>权限怎么用</strong>
            <p>
              新买入、自动恢复与风险退出分别核验，互不替代；任何一项显示「未知」都按不可用处理，不沿用上一次的绿灯。
              要改变权限，请先处理下方数据修复与状态审核，再由本人提交审核。
            </p>
          </div>
        </div>
          </>
        )}
      </div>

      {/* ⑤ 对账连续性：逐日队列 + 调度/恢复/一致性/异常四条保障 */}
      <div
        role="tabpanel"
        id={`${baseId}-panel-continuity`}
        data-testid="pi-validation-continuity"
        aria-labelledby={`${baseId}-tab-continuity`}
        hidden={view !== "continuity"}
      >
        {visited.includes("continuity") && (
          <>
        <div className="pi-verify-callout pi-verify-callout--first">
          <CircleHelp size={15} aria-hidden="true" />
          <div>
            <strong>对账数据来源与推进方式</strong>
            <p>
              券商回报与本地账本双向比对。「组合自动对账」按排程轮询，只核对<b>已到期且尚未通过</b>的交易日，
              从最早未通过的日期逐个往后推进，<b>不跳日</b>；差异只写进报告，不自动改账、不代下单。
            </p>
          </div>
        </div>

        <dl className="pi-verify-facts">
          <div>
            <dt>已核对至</dt>
            <dd>{queue?.verified_through ?? "—"}</dd>
          </div>
          <div>
            <dt>最早待核对</dt>
            <dd>{queue?.target_trade_date ?? "—"}</dd>
          </div>
          <div>
            <dt>待核对交易日</dt>
            <dd data-tone={gapCount ? "warn" : "ok"} title="按连续覆盖计：含尚未轮到核对的前序日期">
              {queue ? (gapCount ? `${gapCount} 个` : "0 个") : "未读取"}
            </dd>
          </div>
          <div>
            <dt>作业调度</dt>
            <dd>
              {jobEnabled === null
                ? "未读取"
                : jobEnabled
                  ? `运行中 · 下次 ${localTime(job?.next_run_at) ?? "待排期"}`
                  : "已停用"}
            </dd>
          </div>
          <div>
            <dt>交易日历</dt>
            <dd>{queue ? (queue.calendar_status === "KNOWN" ? "已核实" : "未核实（不猜应核对日期）") : "—"}</dd>
          </div>
        </dl>

        <div className="pi-verify-card">
          <div className="pi-verify-card-head">
            <h3>
              对账连续性
              <small>
                {recentDays.length
                  ? `${recentDays.length}${visibleDays.length > recentDays.length ? ` / ${visibleDays.length}` : ""} 个交易日 · 待核对优先`
                  : "逐日连续覆盖，缺口不可跳过"}
              </small>
            </h3>
            <span className="pi-verify-card-meta" data-tone={effectiveKind === "unknown" ? "warn" : undefined}>
              {effectiveKind === "unknown"
                ? "运行检查未读取 · 下表来自最近一次队列读取"
                : summary
                  ? `最近一次作业：通过 ${summary.passed_count} · 等待 ${summary.waiting_count} · 差异 ${summary.difference_count} · 异常 ${summary.error_count}`
                  : queue
                    ? `缺口 ${gapCount} 天`
                    : "尚未读取自动对账队列"}
            </span>
          </div>
          <div className="pi-verify-table-wrap">
            <table className="pi-verify-table pi-verify-table--continuity">
              <colgroup>
                <col style={{ width: 120 }} />
                <col style={{ width: 130 }} />
                <col style={{ width: 118 }} />
                <col style={{ width: 190 }} />
                <col />
                <col style={{ width: 104 }} />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">交易日</th>
                  <th scope="col">核对结论</th>
                  <th scope="col">作业状态</th>
                  <th scope="col">重试 / 下次检查</th>
                  <th scope="col">依据与原因</th>
                  <th scope="col">每日对账</th>
                </tr>
              </thead>
              <tbody>
                {recentDays.length === 0 && (
                  <tr>
                    <td colSpan={6} className="pi-verify-empty-cell">
                      <span>
                        {loading ? "正在读取自动对账队列…" : queue ? "当前覆盖范围内还没有可核对的交易日" : "还未读取到自动对账队列"}
                      </span>
                      {/* 兜底入口：队列没读到 / 没有可点日期时，仍要能打开对账面板（不指定交易日） */}
                      <Button size="small" onClick={onOpenReconciliation}>打开对账</Button>
                      {!queue && (
                        <Button size="small" disabled={loading} onClick={onReloadQueue}>
                          重新读取
                        </Button>
                      )}
                    </td>
                  </tr>
                )}
                {recentDays.map((day) => {
                  const verdict = DAY_VERDICT[day.status];
                  const work = workByDate.get(day.trade_date);
                  const attempts = work?.attempt_count ?? 0;
                  const nextCheck = localTime(work?.next_check_at);
                  // 队列按连续覆盖推进：排在最早待核对日之后的日期还没轮到，不能写成「排队中」装成正在跑
                  const behindTarget = Boolean(
                    queue?.target_trade_date &&
                      day.status !== "PASSED" &&
                      day.trade_date > queue.target_trade_date,
                  );
                  return (
                    <tr
                      key={day.trade_date}
                      data-status={day.status}
                      className={targetDate === day.trade_date ? "is-target" : undefined}
                    >
                      <td>
                        <span className="pi-verify-code">{day.trade_date}</span>
                      </td>
                      <td>
                        <Pill
                          compact
                          label=""
                          ariaLabel={verdict?.label ?? day.status}
                          value={verdict?.label ?? day.status}
                          tone={verdict?.tone ?? "is-unknown"}
                        />
                      </td>
                      <td className="pi-verify-state-text">
                        {behindTarget ? "等待前序日期" : (WORK_STATE[day.work_state ?? ""] ?? (day.work_state || "—"))}
                      </td>
                      <td className="pi-verify-cond">
                        {attempts > 0 ? (
                          <span data-tone={work?.retry_exhausted ? "fail" : undefined}>
                            第 {attempts} 次{work?.last_error_code ? ` · ${work.last_error_code}` : ""}
                            {work?.retry_exhausted ? " · 已转人工核查" : ""}
                          </span>
                        ) : (
                          <span className="pi-verify-dash">—</span>
                        )}
                        {nextCheck && <small className="pi-verify-sub">下次 {nextCheck}</small>}
                      </td>
                      <td className="pi-verify-cond">
                        <span>{humanReason(day.reason) || "—"}</span>
                        {(day.report_id || day.report_version) && (
                          <small className="pi-verify-sub" title={day.report_id ?? undefined}>
                            报告 {shortId(day.report_id) ?? "—"} · 版本 {shortId(day.report_version) ?? "—"}
                          </small>
                        )}
                      </td>
                      <td>
                        <Button
                          size="small"
                          type="text"
                          aria-label={`${day.trade_date} 每日对账`}
                          title={`进入 ${day.trade_date} 的每日对账`}
                          onClick={() => onInspectDay(day.trade_date)}
                        >
                          查看
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {hiddenHistoryDays > 0 && (
            <p className="pi-verify-footnote">
              另有 {hiddenHistoryDays} 个历史日期保持「历史未知」（旧核验期前没有完整结论），不参与本次核对，也不推进已核验日期。
            </p>
          )}
        </div>

        <details className="pi-verify-details">
          <summary>连续性是怎么保证的（调度 · 中断恢复 · 一致性 · 异常）</summary>
          <dl className="pi-verify-mechanism">
            <div>
              <dt>调度</dt>
              <dd>
                「组合自动对账」按排程轮询，只取<b>已到期且未通过</b>的交易日；目标日恒为最早未通过日，
                后面的日期不会越过它先跑。队列还区分「等待执行证据」与「尚未到期」，避免把没到点的日子算成缺口。
              </dd>
            </div>
            <div>
              <dt>中断恢复</dt>
              <dd>
                核对作业跑在持久化异步任务上，逐日工作项带状态。后端重启后按工作项恢复，未完成的日期重新入队；
                被重启打断的这一天按<b>中断</b>处理并重新核对，而不是直接判失败，也不重复生成第二份报告。
              </dd>
            </div>
            <div>
              <dt>数据一致性</dt>
              <dd>
                报告按「组合 + 交易日 + 覆盖代次」持久化，带报告版本与证据版本，同一天重复核对是幂等的；
                校验以券商回报与本地账本双向比对为准，差异只记录、不自动改账；账户基线换代会开启新覆盖范围，
                旧期间保持历史未知，不会被新报告顺带洗白。
              </dd>
            </div>
            <div>
              <dt>异常处理</dt>
              <dd>
                瞬时读取失败按递增间隔重试，连续失败到上限后转「需要人工核查」，不再空转；
                交易日历或覆盖来源无法核实时不推断应核对日期，直接标为历史未知。
                {queue?.limitations?.length ? `当前保留限制：${queue.limitations.length} 项（见「执行与对账 → 自动对账作业」）。` : ""}
              </dd>
            </div>
          </dl>
        </details>
          </>
        )}
      </div>

      {/* ⑥ 行动项：定义、来源、跟踪与闭环 */}
      <div
        role="tabpanel"
        id={`${baseId}-panel-actions`}
        data-testid="pi-validation-actions"
        aria-labelledby={`${baseId}-tab-actions`}
        hidden={view !== "actions"}
      >
        {visited.includes("actions") && (
          <>
        <div className="pi-verify-callout pi-verify-callout--first">
          <ListChecks size={15} aria-hidden="true" />
          <div>
            <strong>行动项是什么</strong>
            <p>
              行动项不是手工建的任务单，而是服务端把<b>自动交易门禁、组合状态机、交易执行来源与引擎运维条件</b>
              归并去重后派生出的待办：每一项都对应一个会挡住交易的真实条件，附带影响范围与唯一直达入口。
              它<b>没有「忽略 / 作废」</b>——修复后重新读取校验结果，条件不再成立时该项自动消失；所有变更进操作记录可回溯。
            </p>
          </div>
        </div>

        <div className="pi-verify-card">
          <div className="pi-verify-card-head">
            <h3>
              待处理行动项
              <small>
                {actions.length
                  ? `高 ${actionCount.blocker} · 中 ${actionCount.warning} · 低 ${actionCount.info} · 按优先级排序`
                  : "服务端派生，按优先级排序"}
              </small>
            </h3>
            <span className="pi-verify-card-meta">{localTime(itemsGeneratedAt) ? `生成于 ${localTime(itemsGeneratedAt)}` : "尚未读取待办"}</span>
          </div>
          <div className="pi-verify-actions">
            {actions.length === 0 && (
              <p className="pi-verify-empty-text">
                {loading ? "正在读取待办…" : "当前没有待办。条件由服务端实时判定，修复后无需手动销项。"}
              </p>
            )}
            {visibleActions.map((item) => {
              const meta = SEVERITY_META[item.severity] ?? SEVERITY_META.info;
              // 服务端有时把差异类型原样塞进 detail（如 DECISION_RUN_NOT_FOUND）：
              // 界面上仍按「禁印枚举」口径换成中文名，原值只进 tooltip。
              const rawDetail = (item.detail ?? "").trim();
              const detailIsCode = rawDetail.length > 0 && KNOWN_RECON_DIFF_KINDS.includes(rawDetail);
              // family 有时是可读分组（data-freshness），有时是内部标识（readiness:XXX）：
              // 只把可读的摆出来，内部标识降到 tooltip，避免在用户清单里堆机器码。
              const familyReadable = Boolean(item.family && /^[a-z][a-z0-9-]*$/.test(item.family));
              return (
                <div className="pi-verify-action-row" key={item.id} data-severity={item.severity}>
                  <Pill compact label="" ariaLabel={`优先级${meta.label}`} value={meta.label} tone={meta.tone} />
                  <div className="pi-verify-action-main">
                    <strong>{item.title}</strong>
                    <p title={detailIsCode ? rawDetail : undefined}>
                      {detailIsCode ? reconDimLabel(rawDetail) : rawDetail || item.impact}
                    </p>
                    <small title={`${item.code}${item.family ? ` · ${item.family}` : ""}`}>
                      {SOURCE_LABEL[item.source] ?? item.source}
                      {familyReadable ? ` · ${item.family}` : ""}
                      {item.trade_date ? ` · ${item.trade_date}` : ""}
                    </small>
                  </div>
                  <Button size="small" disabled={!item.primary_action} onClick={() => onOpenAction(item)}>
                    直达
                  </Button>
                </div>
              );
            })}
            {actions.length > ACTION_PAGE && (
              <div className="pi-verify-actions-more">
                <span>按优先级排列，已显示 {visibleActions.length} / {actions.length} 项</span>
                <Button size="small" type="text" onClick={() => setShowAllActions((value) => !value)}>
                  {showAllActions ? "收起" : `展开其余 ${actions.length - ACTION_PAGE} 项`}
                </Button>
              </div>
            )}
          </div>
        </div>

        <div className="pi-verify-callout">
          <Clock3 size={15} aria-hidden="true" />
          <div>
            <strong>跟踪与闭环</strong>
            <p>
              跟踪只看三处：本清单（还差什么）、「数据修复与状态审核」（怎么修）、「操作记录」（谁在什么时候动了什么）。
              闭环不是勾掉，而是<b>条件复位</b>：修复 → 重新读取校验结果 → 服务端重新计算 → 该项自然消失；
              对账类行动项还要走完差异确认与本人审核，状态才会回到就绪。恢复后若来源或依据变化，同一条件会重新出现，
              不会因为「上次关过」而豁免。
            </p>
          </div>
        </div>
          </>
        )}
      </div>

      {/* ⑦ 操作记录：本页只给入口，完整流水仍在独立抽屉里分页回看 */}
      <div
        role="tabpanel"
        id={`${baseId}-panel-audit`}
        data-testid="pi-validation-audit"
        aria-labelledby={`${baseId}-tab-audit`}
        hidden={view !== "audit"}
      >
        {visited.includes("audit") && (
          <>
        <div className="pi-verify-card">
          <div className="pi-verify-card-head">
            <h3>
              操作记录
              <small>按标的、交易日回看旧处理与审核</small>
            </h3>
            <span className="pi-verify-card-meta">含系统自动操作</span>
          </div>
          <div className="pi-verify-audit-entry">
            <p>审计流水按标的与交易日筛选，完整记录在独立抽屉里分页回看，不在本页重复拉一遍。</p>
            <Button type="primary" size="small" icon={<History size={13} />} onClick={onOpenHistory}>
              打开操作记录
            </Button>
          </div>
        </div>

        <div className="pi-verify-callout">
          <Clock3 size={15} aria-hidden="true" />
          <div>
            <strong>记录怎么用</strong>
            <p>
              这里只说明去哪看。谁在什么时候动了什么、系统自动做了什么，都以该抽屉里的服务端记录为准；
              历史记录只用于追溯，旧审批与旧处理不会变成当前授权。
            </p>
          </div>
        </div>
          </>
        )}
      </div>
    </section>
  );
}
