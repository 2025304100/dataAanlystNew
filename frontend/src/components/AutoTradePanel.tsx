import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Empty,
  InputNumber,
  Modal,
  Skeleton,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { InfoCircleOutlined, QuestionCircleOutlined, RollbackOutlined, SyncOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import type { ApiError } from "../api/client";
import { enumLabel, sideLabel, t } from "../i18n";
import { money, formatRelativeTime } from "../utils/format";
import type {
  AutoTradePlanItem,
  AutoTradeReadiness,
  AutoTradeResult,
  ReadinessIssue,
} from "../types";
// WP-S-FIX.4：阻断操作就地处理（按钮替换为 CapabilityGateButton）
import { CapabilityGateButton } from "./capability/CapabilityGateButton";

/**
 * P0-AutoTrade：自动交易执行面板（对齐最终收口方案）。
 *
 * - 真实状态：不再只看 auto_trade_enabled；以 GET readiness 接口 ready 为真正"可运行"。
 * - 先预演再执行：默认 dry_run=true；dry_run=false 按钮强制 ready=true 或 已完成一次 dry_run。
 * - 错误结构化：execute 409 BUSINESS_BLOCKED 读取 err.detail.extras.blockers 渲染。
 */

interface MemberStatusItem {
  member_id: number;
  symbol_id: number;
  symbol: string | null;
  status: string;
  execution_mode: string;
  source_type: string;
  manual_lock: boolean;
  has_position: boolean;
  position_quantity: number;
  latest_order: {
    order_id: number;
    side: string;
    status: string;
    created_at: string | null;
    source_type: string | null;
    signal_id: number | null;
    execution_mode: string | null;
    client_order_key: string | null;
    rejection_code: string | null;
    rejection_detail: string | null;
  } | null;
  data_health: {
    healthy: boolean;
    reason: string;
    kline_latest_at: string | null;
    score_latest_at: string | null;
    rule_version_id: number | null;
  };
  risk_blocked: boolean;
  data_expired: boolean;
}

interface DryRunDiffItem {
  symbol_id: number;
  side: string;
  old_action: string | null;
  new_action: string | null;
  reason: string;
  detail: string;
}

interface MemberSourceStatus {
  portfolio_id: number;
  enabled: boolean;
  env_var_name: string;
  env_flag: string;
  whitelist_match: boolean;
  blacklist_match: boolean;
  whitelist: number[];
  blacklist: number[];
}

function formatShanghai(dt: string | null): string {
  if (!dt) return "-";
  try {
    const d = new Date(dt);
    if (Number.isNaN(d.getTime())) return String(dt);
    return d.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
  } catch {
    return String(dt);
  }
}

function executionModeLabel(mode: string): string {
  if (mode === "manual") return t("modeManual");
  if (mode === "confirm") return t("modeConfirm");
  if (mode === "auto") return t("modeAuto");
  return mode;
}

function memberStatusLabel(status: string): string {
  if (status === "active") return t("statusActive");
  if (status === "paused") return t("statusPaused");
  if (status === "archived") return t("statusArchived");
  return status;
}

function diffReasonLabel(reason: string): string {
  switch (reason) {
    case "member_missing":
      return t("autoTradeMember.reasonCodes.memberMissing");
    case "member_paused":
      return t("autoTradeMember.reasonCodes.memberPaused");
    case "signal_diff":
      return t("autoTradeMember.reasonCodes.signalDiff");
    case "data_expired":
      return t("autoTradeMember.reasonCodes.dataExpired");
    case "risk_blocked":
      return t("autoTradeMember.reasonCodes.riskBlocked");
    default:
      return reason;
  }
}

function diffReasonColor(reason: string): string {
  switch (reason) {
    case "member_missing":
      return "orange";
    case "member_paused":
      return "gold";
    case "signal_diff":
      return "blue";
    case "data_expired":
      return "red";
    case "risk_blocked":
      return "volcano";
    default:
      return "default";
  }
}

/* ==========================================================================
 * 就绪检查 code → 中文名 + 修复指引
 *   · 页面显示统一为「中文名称（原英文枚举 ID）」
 *     → 用户先看中文，开发排错仍能定位原 code
 *   · 每个 code 配 fix[]：直接告诉用户"下一步该点哪里"
 * ======================================================================== */
type ReadinessCodeMetaLocal = { cn: string; category: "开关" | "账户" | "来源" | "规则" | "数据" | "范围" | "调度" | "其他"; fix: string[] };
const READINESS_CODE_META: Record<string, ReadinessCodeMetaLocal> = {
  PORTFOLIO_DISABLED: { cn: "自动交易开关未开", category: "开关", fix: ["打开「自动接管」右侧总开关；或前往治理 Tab 启用自动交易。"] },
  AUTO_TRADE_DISABLED: { cn: "自动交易开关未开", category: "开关", fix: ["打开「自动接管」右侧总开关；或前往治理 Tab 启用自动交易。"] },
  ACCOUNT_NOT_SIMULATED: {
    cn: "账户非模拟", category: "账户",
    fix: [
      "自动交易目前仅支持「模拟」账户（AccountType=simulated）。",
      "操作：打开左上角「组合选择器→编辑组合」，把账户类型切为模拟；若需实盘自动，请先完整跑通模拟流程后再升级。",
    ],
  },
  SOURCE_MODE_INVALID: {
    cn: "来源模式非法", category: "来源",
    fix: ["前往「治理 Tab→自动交易来源」：把 auto_trade_source_mode 选为合法值 portfolio / members_only / legacy_scan。"],
  },
  SOURCE_LEGACY_SCAN: {
    cn: "使用旧全局扫描来源", category: "来源",
    fix: ["属于警告，不阻断执行，但建议迁移：在「治理 Tab→自动交易来源」切为 portfolio 或 members_only，可获得更稳定的新鲜度检查与范围。"],
  },
  WARNING_SOURCE_MODE_LEGACY_SCAN: {
    cn: "使用旧全局扫描来源", category: "来源",
    fix: ["属于警告，不阻断执行，但建议迁移：在「治理 Tab→自动交易来源」切为 portfolio 或 members_only。"],
  },
  SOURCE_ENV_OVERRIDE: {
    cn: "成员来源被环境变量熔断", category: "来源",
    fix: [
      "检查部署环境：是否设置了 `MEMBER_SOURCE_ENABLED=false` 一类的全局熔断开关（紧急时期使用）。",
      "如非紧急，把该开关改回 true；否则真实执行会回退到旧扫描逻辑。",
    ],
  },
  WARNING_SOURCE_ENV_OVERRIDE: {
    cn: "成员来源被环境变量熔断", category: "来源",
    fix: ["检查部署环境：成员来源全局熔断是否被打开；如非紧急，关闭熔断以消除本警告。"],
  },
  RULE_NOT_CONFIGURED: {
    cn: "未配置激活策略规则", category: "规则",
    fix: [
      "切换到「策略规则」Tab：选择选股池、风控阈值、调仓周期，然后**点击保存**生成激活版规则快照。",
      "若走路径B绑定训练产出：先在「因子模型训练页」训练一个「已验证」模型，再回本页下拉选中。",
    ],
  },
  NO_RULE: { cn: "缺少激活规则", category: "规则", fix: ["切到「策略规则」Tab，保存一次激活版的规则。"] },
  RULE_INVALID_PARAMS: {
    cn: "策略规则参数非法", category: "规则",
    fix: [
      "前往「策略规则」Tab 修复标红字段：通常是 max_single_position_pct / max_loss_per_trade_pct 等百分比超出 0~1。",
      "另一个常见原因：「阶段限制 stage_limits_json」为空——请在风控阶段限制模块里保存有效 JSON。",
    ],
  },
  MARKET_DATA_STALE: {
    cn: "行情或评分数据过期", category: "数据",
    fix: [
      "① 前往「设置 → 数据中心 → 行情底座」：点击「智能同步」或「每日同步」，把 A 股股票和指数行情拉到今日。",
      "② 切换到「因子输入」Tab：点「同步/重算」候选的估值、财报历史、人气榜等因子依赖，直到候选新鲜度全部变为「今日」。",
      "③ 完成后点本页上方「刷新状态」按钮（readiness 会立即重算）。",
    ],
  },
  NO_FRESH_SCORES: {
    cn: "最新评分缺失", category: "数据",
    fix: ["前往「因子输入 Tab」或「策略规则 Tab」：点「同步候选评分」，重跑一轮候选打分并刷新到今日。"],
  },
  NO_TRADEABLE_RANGE: {
    cn: "无可交易范围", category: "范围",
    fix: [
      "当前既没持仓可卖，也没有任何可执行买入计划。",
      "操作：① 先在「候选/持仓成员」Tab 手动添加几只股票；② 同步行情+评分（修复 MARKET_DATA_STALE）；③ 点下方「Dry Run 预演」让系统产生买入信号。",
    ],
  },
  SCHEDULE_DISABLED: {
    cn: "定时任务未创建或暂停", category: "调度",
    fix: [
      "前往「治理 → 调度任务」：创建或启用 `task_type=portfolio_auto_trade` 的定时任务，推荐每天 20:00 之后运行（对齐 HG2 调度时间窗）。",
      "注：手动 Dry Run / 真实执行不受调度影响，调度仅负责「自动按天跑」。",
    ],
  },
  NO_SCHEDULED_TASK: {
    cn: "未创建自动交易定时任务", category: "调度",
    fix: ["前往「治理 → 调度任务」，新建一条 type=portfolio_auto_trade 的定时任务。"],
  },
  SCHEDULED_TASK_DISABLED: {
    cn: "定时任务已暂停", category: "调度",
    fix: ["前往「治理 → 调度任务」，把对应的定时任务切为「启用」。"],
  },
  SCHEDULE_WINDOW: {
    cn: "不在调度时间窗", category: "调度",
    fix: ["调度型就绪检查仅在每日允许时间段（默认 20:00 之后）判定通过。手动执行 Dry Run / 真实下单不受此限。"],
  },
  NO_AUTO_MEMBERS: {
    cn: "无自动模式成员", category: "范围",
    fix: [
      "前往「持仓成员」Tab：把希望自动管理成员的「执行模式」从「手动/确认」切为「自动」。",
      "若「自动交易来源 = portfolio」：即使没有自动成员，也可以直接靠候选池走自动买入（会自动建成员）。",
    ],
  },
  NO_CANDIDATES: {
    cn: "无候选也无持仓", category: "范围",
    fix: [
      "点击页面右上角「+ 添加候选标的」，先加入若干只股票作为自动交易选股范围。",
      "没有候选也没有持仓的组合，自动交易没有工作对象，不会产生动作。",
    ],
  },
  NO_BUY_SIGNALS_TODAY: {
    cn: "今日暂无买入信号", category: "范围",
    fix: [
      "属于「今日节奏」类警告（非阻塞）：说明当前候选打分没有产生买入 action，属于正常节奏。",
      "如果希望主动产生信号：① 扩大选股池/加入更多候选；② 在「策略规则」调整因子权重或降低风控阈值；③ 先 Dry Run 查看打分明细。",
    ],
  },
  DECISION_ENGINE_SOURCE_REQUIRED: {
    cn: "决策引擎来源缺失", category: "来源",
    fix: ["前往「设置 → 决策引擎」：打开成员来源订单计划的全局开关 members_source_enabled。"],
  },
  STRATEGY_SNAPSHOT_REQUIRED: {
    cn: "缺少策略执行快照", category: "规则",
    fix: ["先在「策略规则 Tab」保存一次激活规则，再跑一轮 Dry Run 或回测，系统会自动生成并应用快照。"],
  },
  NOT_READY: {
    cn: "通用未就绪", category: "其他",
    fix: ["查看下方 blockers 列表按 code 逐项修复；或先执行一次 Dry Run 看到更详细的阻断明细。"],
  },
};

function getReadinessCodeMeta(code: string): { cn: string; dev: string; fix: string[]; category: string } {
  const m = READINESS_CODE_META[code];
  if (m) return { cn: m.cn, dev: code, fix: m.fix, category: m.category };
  const cn = code.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
  return {
    cn, dev: code, category: "其他",
    fix: ["属于未知阻塞项，请把下方「message + code」一起截图发给技术支持定位。"],
  };
}

function issueCodeCN(code: string): string {
  // 保持向后兼容：原来 issueCodeToText 是返回「数据/来源/调度」大类，现在改为返回 code 的中文名称
  return getReadinessCodeMeta(code).cn;
}

/** @deprecated 请直接使用 getReadinessCodeMeta(code).cn；保留兼容用于 window.confirm 文案 */
function issueCodeToText(code: string): string {
  return issueCodeCN(code);
}

type UnifiedErrorExtras = Record<string, unknown> & { blockers?: ReadinessIssue[]; warnings?: ReadinessIssue[] };

export default function AutoTradePanel() {
  const ctx = useApp();
  const portfolioId = ctx.portfolioId;
  const currentPortfolio = ctx.portfolios.find((p) => p.id === portfolioId);
  const isSimulated = currentPortfolio?.account_type === "simulated";
  const autoTradeEnabled = Number(currentPortfolio?.auto_trade_enabled) === 1;

  const [dryRun, setDryRun] = useState(true);
  const [buyCandidateLimit, setBuyCandidateLimit] = useState(10);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AutoTradeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  // P0-AutoTrade：记录最新一次结构化 blockers（409 时展示）
  const [errorBlockers, setErrorBlockers] = useState<ReadinessIssue[] | null>(null);
  // P0-AutoTrade：dry run 先跑一次的闸门
  const [hasDryRunThisSession, setHasDryRunThisSession] = useState(false);

  // P0-AutoTrade：readiness 状态
  const [readiness, setReadiness] = useState<AutoTradeReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [readinessError, setReadinessError] = useState<string | null>(null);

  // WP6.6：成员级 / 双跑 diff / 来源状态
  const [members, setMembers] = useState<MemberStatusItem[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);
  const [membersError, setMembersError] = useState<string | null>(null);
  const [diffs, setDiffs] = useState<DryRunDiffItem[]>([]);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [sourceStatus, setSourceStatus] = useState<MemberSourceStatus | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [rollingBack, setRollingBack] = useState(false);

  const loadReadiness = useCallback(async () => {
    if (!portfolioId) {
      setReadiness(null);
      setReadinessError(null);
      return;
    }
    setReadinessLoading(true);
    setReadinessError(null);
    try {
      const data = (await api.getAutoTradeReadiness(portfolioId, { for_schedule: false })) as AutoTradeReadiness;
      setReadiness(data);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setReadinessError(msg || t("autoTradeFailed"));
      setReadiness(null);
    } finally {
      setReadinessLoading(false);
    }
  }, [portfolioId]);

  const execute = useCallback(async () => {
    if (!portfolioId) return;

    const isReal = !dryRun;
    const readyNow = readiness?.ready === true;
    const blockersNow = readiness?.blockers ?? [];

    // P0-AutoTrade：真实执行必须先 Dry Run 过 或 readiness.ready=true
    if (isReal && !readyNow && !hasDryRunThisSession) {
      Modal.warning({
        title: t("autoTradeRequireDryRunFirst"),
        content: t("autoTradeNotReadyHint"),
        okText: t("confirm"),
      });
      return;
    }
    if (isReal && !readyNow && blockersNow.length > 0) {
      const lines = blockersNow.map((b, i) => `${i + 1}. [${issueCodeToText(b.code)}] ${b.message}`).join("\n");
      const ok = window.confirm(
        `${t("autoTradeNotReadyHint")}\n\n${t("autoTradeBlockers").replace("{count}", String(blockersNow.length))}:\n${lines}\n\n${t("autoTradeExecuteConfirm")}`,
      );
      if (!ok) return;
    } else if (isReal) {
      if (!window.confirm(t("autoTradeExecuteConfirm"))) return;
    }

    setRunning(true);
    setError(null);
    setErrorBlockers(null);
    try {
      const data = (await api.executeAutoTrade(portfolioId, {
        dry_run: dryRun,
        buy_candidate_limit: buyCandidateLimit,
      })) as AutoTradeResult;
      setResult(data);
      if (dryRun) setHasDryRunThisSession(true);

      // 若 execute 返回内联 readiness，则用它覆盖最新 readiness（包含最新 blockers）
      if (data.readiness) {
        setReadiness({
          portfolio_id: data.portfolio_id,
          ready: !!data.readiness.ready,
          enabled: !!data.readiness.enabled,
          account_ready: !!data.readiness.account_ready,
          data_ready: !!data.readiness.data_ready,
          source_ready: !!data.readiness.source_ready,
          schedule_ready: !!data.readiness.schedule_ready,
          blockers: data.readiness.blockers ?? [],
          warnings: data.readiness.warnings ?? [],
          source_mode: data.readiness.source_mode,
          for_schedule: data.readiness.for_schedule,
          executed_at: data.readiness.executed_at ?? data.executed_at,
        });
      }

      ctx.showToast("success", t("autoTradeSuccess"));
      if (!dryRun) {
        await ctx.loadWorkbench();
      }
    } catch (err: any) {
      const apiErr = err as ApiError | undefined;
      const detail = apiErr?.detail as any;
      const extras: UnifiedErrorExtras | undefined = detail?.extras;
      const blockers: ReadinessIssue[] | undefined = extras?.blockers ?? detail?.blockers;
      const userMessage = apiErr?.user_message ?? apiErr?.message ?? String(err ?? t("autoTradeFailed"));

      setError(userMessage);
      if (blockers && Array.isArray(blockers)) {
        setErrorBlockers(blockers as ReadinessIssue[]);
      }
      const firstBlockerMsg = blockers?.[0]?.message ?? userMessage;
      ctx.showToast("error", t("autoTradeFailed") + ": " + firstBlockerMsg);
    } finally {
      setRunning(false);
    }
  }, [portfolioId, dryRun, buyCandidateLimit, readiness, hasDryRunThisSession, ctx]);

  const loadMembers = useCallback(async () => {
    if (!portfolioId) {
      setMembers([]);
      return;
    }
    setMembersLoading(true);
    setMembersError(null);
    try {
      const data = await api.getAutoTradeMemberStatus(portfolioId);
      setMembers(data.members ?? []);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setMembersError(msg || t("autoTradeMember.loadMembersFailed"));
    } finally {
      setMembersLoading(false);
    }
  }, [portfolioId]);

  const loadDiffs = useCallback(async () => {
    if (!portfolioId) {
      setDiffs([]);
      return;
    }
    setDiffLoading(true);
    setDiffError(null);
    try {
      const data = await api.getAutoTradeDryRunDiff(portfolioId);
      setDiffs(data.diffs ?? []);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setDiffError(msg || t("autoTradeMember.loadDiffFailed"));
    } finally {
      setDiffLoading(false);
    }
  }, [portfolioId]);

  const loadSourceStatus = useCallback(async () => {
    if (!portfolioId) {
      setSourceStatus(null);
      return;
    }
    setSourceLoading(true);
    setSourceError(null);
    try {
      const data = await api.getAutoTradeMemberSourceStatus(portfolioId);
      setSourceStatus(data);
    } catch (err: any) {
      const msg = err?.message || String(err);
      setSourceError(msg || t("autoTradeMember.loadSourceStatusFailed"));
    } finally {
      setSourceLoading(false);
    }
  }, [portfolioId]);

  const handleRollback = useCallback(async () => {
    if (!portfolioId) return;
    Modal.confirm({
      title: t("autoTradeMember.rollbackToOldSource"),
      content: t("autoTradeMember.rollbackConfirmDesc"),
      okText: t("autoTradeMember.rollbackToOldSource"),
      cancelText: t("cancel"),
      okButtonProps: { danger: true },
      onOk: async () => {
        setRollingBack(true);
        try {
          await api.rollbackAutoTradeToOldSource(portfolioId);
          ctx.showToast("success", t("autoTradeMember.rollbackSuccess"));
          await loadSourceStatus();
          await loadReadiness();
        } catch (err: any) {
          const msg = err?.message || String(err);
          ctx.showToast("error", t("autoTradeMember.rollbackFailed") + ": " + msg);
        } finally {
          setRollingBack(false);
        }
      },
    });
  }, [portfolioId, ctx, loadSourceStatus, loadReadiness]);

  // 组合切换/开关切换：重置 + 加载依赖
  useEffect(() => {
    setResult(null);
    setError(null);
    setErrorBlockers(null);
    setHasDryRunThisSession(false);
    if (portfolioId && isSimulated) {
      // readiness 无论是否开启 autoTradeEnabled 都拉（让 ready=false 原因更直观）
      void loadReadiness();
      if (autoTradeEnabled) {
        void loadMembers();
        void loadDiffs();
        void loadSourceStatus();
      } else {
        setMembers([]);
        setDiffs([]);
        setSourceStatus(null);
      }
    } else {
      setReadiness(null);
      setMembers([]);
      setDiffs([]);
      setSourceStatus(null);
    }
  }, [
    portfolioId,
    isSimulated,
    autoTradeEnabled,
    loadReadiness,
    loadMembers,
    loadDiffs,
    loadSourceStatus,
  ]);

  // 派生：ready 语义（允许 UI 在 readiness 加载中给出不同提示）
  const ready = readiness?.ready === true;
  const blockers = readiness?.blockers ?? [];
  const warnings = readiness?.warnings ?? [];

  // 派生：真实执行按钮是否 disabled
  const realExecDisabledReason = useMemo<string | null>(() => {
    if (dryRun) return null;
    if (!ready && !hasDryRunThisSession) return t("autoTradeRequireDryRunFirst");
    return null;
  }, [dryRun, ready, hasDryRunThisSession]);

  if (!portfolioId) return null;
  if (!isSimulated) {
    return (
      <section className="band auto-trade-band">
        <div className="panel">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("autoTradePanelTitle")}</p>
              <h2>{t("autoTradePanelTitle")}</h2>
            </div>
          </div>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t("autoTradeOnlySimulated")}
          />
        </div>
      </section>
    );
  }

  const sells = result?.sells ?? [];
  const buys = result?.buys ?? [];
  const errors = result?.errors ?? [];

  // ----------------------------------------------------------------------------
  // 列定义
  // ----------------------------------------------------------------------------
  const sellColumns = [
    {
      title: t("symbol"),
      dataIndex: "symbol",
      key: "symbol",
      render: (v: string, record: AutoTradePlanItem) => (
        <span>
          {v}
          <span style={{ color: "#94a3b8", marginLeft: 6, fontSize: 12 }}>{record.name}</span>
        </span>
      ),
    },
    {
      title: t("action"),
      dataIndex: "action",
      key: "action",
      render: (v: string) => <Tag color={v === "exit" ? "red" : "orange"}>{v}</Tag>,
    },
    {
      title: t("quantity"),
      key: "qty",
      render: (_: any, record: AutoTradePlanItem) => (
        <span>{record.sell_quantity} / {record.held_quantity}</span>
      ),
    },
    { title: t("price"), dataIndex: "ref_price", key: "ref_price", render: (v: number) => money(v, 2) },
    {
      title: t("status"),
      key: "status",
      render: (_: any, record: AutoTradePlanItem) =>
        record.executed ? <Tag color="green">{t("autoTradeExecuted")}</Tag> : <Tag>{t("autoTradePlanned")}</Tag>,
    },
    { title: t("fee"), dataIndex: "fee", key: "fee", render: (v: number | null) => (v != null ? money(v, 2) : "-") },
  ];

  const buyColumns = [
    {
      title: t("symbol"),
      dataIndex: "symbol",
      key: "symbol",
      render: (v: string, record: AutoTradePlanItem) => (
        <span>
          {v}
          <span style={{ color: "#94a3b8", marginLeft: 6, fontSize: 12 }}>{record.name}</span>
        </span>
      ),
    },
    { title: t("action"), dataIndex: "action", key: "action", render: (v: string) => <Tag color="green">{v}</Tag> },
    { title: t("price"), dataIndex: "ref_price", key: "ref_price", render: (v: number) => money(v, 2) },
    {
      title: t("quantity"),
      dataIndex: "buy_quantity",
      key: "buy_quantity",
      render: (v: number | null) => (v != null ? v : "-"),
    },
    {
      title: t("autoTradeBlocked"),
      key: "blocked",
      render: (_: any, record: AutoTradePlanItem) => {
        if (record.can_open === true || record.executed) {
          return record.executed ? (
            <Tag color="green">{t("autoTradeExecuted")}</Tag>
          ) : (
            <Tag>{t("autoTradePlanned")}</Tag>
          );
        }
        return (
          <Tooltip title={record.blocked_reasons?.join(", ") || record.decision || ""}>
            <Tag color="red">{t("autoTradeBlocked")}</Tag>
          </Tooltip>
        );
      },
    },
    { title: t("fee"), dataIndex: "fee", key: "fee", render: (v: number | null) => (v != null ? money(v, 2) : "-") },
  ];

  const memberColumns: ColumnsType<MemberStatusItem> = [
    {
      title: t("symbol"),
      key: "symbol",
      width: 140,
      render: (_v, record) => <span>{record.symbol ?? `#${record.symbol_id}`}</span>,
    },
    {
      title: t("portfolioMemberColumnStatus"),
      dataIndex: "status",
      key: "status",
      width: 90,
      render: (status: string) => {
        const color = status === "active" ? "green" : status === "paused" ? "orange" : "default";
        return <Tag color={color}>{memberStatusLabel(status)}</Tag>;
      },
    },
    {
      title: t("portfolioMemberColumnExecutionMode"),
      dataIndex: "execution_mode",
      key: "execution_mode",
      width: 90,
      render: (mode: string) => executionModeLabel(mode),
    },
    {
      title: t("autoTradeMember.hasPosition"),
      key: "has_position",
      width: 80,
      render: (_v, record) =>
        record.has_position ? (
          <Tag color="blue">{t("autoTradeMember.hasPositionYes")}</Tag>
        ) : (
          <span style={{ color: "#94a3b8", fontSize: 12 }}>-</span>
        ),
    },
    {
      title: (
        <span>
          {t("autoTradeMember.riskBlocked")}
          <Tooltip title={t("autoTradeMember.riskBlockedHint")}>
            <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
          </Tooltip>
        </span>
      ),
      key: "risk_blocked",
      width: 100,
      render: (_v, record) =>
        record.risk_blocked ? (
          <Tooltip
            title={
              record.latest_order?.rejection_detail ||
              record.latest_order?.rejection_code ||
              t("autoTradeMember.riskBlocked")
            }
          >
            <Tag color="red">{t("autoTradeMember.riskBlocked")}</Tag>
          </Tooltip>
        ) : (
          <Tag color="green">{t("autoTradeMember.normal")}</Tag>
        ),
    },
    {
      title: (
        <span>
          {t("autoTradeMember.dataExpired")}
          <Tooltip title={t("autoTradeMember.dataExpiredHint")}>
            <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
          </Tooltip>
        </span>
      ),
      key: "data_expired",
      width: 100,
      render: (_v, record) =>
        record.data_expired ? (
          <Tooltip title={record.data_health?.reason || t("autoTradeMember.dataExpired")}>
            <Tag color="red">{t("autoTradeMember.dataExpired")}</Tag>
          </Tooltip>
        ) : (
          <Tag color="green">{t("autoTradeMember.normal")}</Tag>
        ),
    },
    {
      title: t("autoTradeMember.latestOrder"),
      key: "latest_order",
      render: (_v, record) => {
        if (!record.latest_order) {
          return <span style={{ color: "#94a3b8", fontSize: 12 }}>{t("autoTradeMember.noLatestOrder")}</span>;
        }
        return (
          <Tooltip
            title={
              <div style={{ fontSize: 12 }}>
                <div>{t("autoTradeMember.orderId")}: {record.latest_order.order_id}</div>
                <div>{t("autoTradeMember.orderSide")}: {sideLabel(record.latest_order.side)}</div>
                <div>{t("autoTradeMember.orderStatus")}: {enumLabel("orderStatus", record.latest_order.status)}</div>
                <div>{t("autoTradeMember.orderSourceType")}: {enumLabel("sourceType", record.latest_order.source_type)}</div>
                <div>{t("autoTradeMember.orderSignalId")}: {record.latest_order.signal_id ?? "-"}</div>
                <div>{t("autoTradeMember.orderClientKey")}: {record.latest_order.client_order_key ?? "-"}</div>
                {record.latest_order.rejection_code && (
                  <div style={{ color: "#ffccc7" }}>
                    {t("autoTradeMember.orderRejection")}: {record.latest_order.rejection_code}
                    {record.latest_order.rejection_detail ? ` - ${record.latest_order.rejection_detail}` : ""}
                  </div>
                )}
              </div>
            }
          >
            <span style={{ fontSize: 12 }}>
              #{record.latest_order.order_id} · {record.latest_order.side} ·{" "}
              {formatShanghai(record.latest_order.created_at)}
            </span>
          </Tooltip>
        );
      },
    },
  ];

  const diffColumns: ColumnsType<DryRunDiffItem> = [
    { title: t("symbol"), dataIndex: "symbol_id", key: "symbol_id", width: 100, render: (v: number) => `#${v}` },
    {
      title: t("autoTradeMember.diffSide"),
      dataIndex: "side",
      key: "side",
      width: 80,
      render: (v: string) => <Tag color={v === "buy" ? "green" : "red"}>{v}</Tag>,
    },
    {
      title: t("autoTradeMember.diffOldAction"),
      dataIndex: "old_action",
      key: "old_action",
      width: 110,
      render: (v: string | null) => v ?? <span style={{ color: "#94a3b8" }}>-</span>,
    },
    {
      title: t("autoTradeMember.diffNewAction"),
      dataIndex: "new_action",
      key: "new_action",
      width: 110,
      render: (v: string | null) => v ?? <span style={{ color: "#94a3b8" }}>-</span>,
    },
    {
      title: t("autoTradeMember.diffReason"),
      dataIndex: "reason",
      key: "reason",
      width: 140,
      render: (v: string) => (
        <Tooltip title={diffReasonLabel(v)}>
          <Tag color={diffReasonColor(v)}>{diffReasonLabel(v)}</Tag>
        </Tooltip>
      ),
    },
    { title: t("autoTradeMember.diffDetail"), dataIndex: "detail", key: "detail", render: (v: string) => <span style={{ fontSize: 12 }}>{v}</span> },
  ];

  // ==========================================================================
  // 渲染
  // ==========================================================================
  return (
    <section className="band auto-trade-band">
      <div className="panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{t("autoTradePanelTitle")}</p>
            <h2>
              {t("autoTradePanelTitle")}
              <Tooltip title={t("autoTradePanelHint")}>
                <QuestionCircleOutlined style={{ marginLeft: 8, fontSize: 14, color: "#94a3b8" }} />
              </Tooltip>
            </h2>
          </div>

          {/* P0-AutoTrade：执行控件组：dry_run 默认，真实执行二次闸门 */}
          <div
            className="panel-meta"
            style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}
          >
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <Switch size="small" checked={dryRun} onChange={setDryRun} />
              <span style={{ fontSize: 12 }}>
                {dryRun ? t("autoTradeDryRun") : t("autoTradeExecute")}
              </span>
              <Tooltip title={dryRun ? t("autoTradeDryRunHint") : t("autoTradeExecuteHint")}>
                <QuestionCircleOutlined style={{ fontSize: 11, color: "#94a3b8" }} />
              </Tooltip>
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 12 }}>{t("autoTradeBuyCandidateLimit")}</span>
              <InputNumber
                size="small"
                min={1}
                max={50}
                value={buyCandidateLimit}
                onChange={(v) => setBuyCandidateLimit(Number(v) || 10)}
                style={{ width: 70 }}
              />
            </span>
            <Tooltip title={realExecDisabledReason || (dryRun ? t("autoTradeDryRunHint") : t("autoTradeExecuteHint"))}>
              <span>
                <CapabilityGateButton
                  capabilityKey="auto_trade"
                  size="small"
                  type="primary"
                  onClick={execute}
                  loading={running}
                  danger={!dryRun}
                  disabled={!!realExecDisabledReason}
                >
                  {dryRun ? t("autoTradeDryRun") : t("autoTradeExecute")}
                </CapabilityGateButton>
              </span>
            </Tooltip>
            <Button
              size="small"
              icon={<SyncOutlined />}
              onClick={loadReadiness}
              loading={readinessLoading}
            >
              {t("autoTradeRefreshReadiness")}
            </Button>
          </div>
        </div>

        {/* 上次执行时间 */}
        {currentPortfolio?.auto_trade_last_run_at && (
          <p className="panel-meta" style={{ fontSize: 11, marginBottom: 8 }}>
            {t("autoTradeLastRunAt")}: {formatRelativeTime(currentPortfolio.auto_trade_last_run_at)}
          </p>
        )}

        {/* =====================================================================
            P0-AutoTrade：就绪状态卡片（取代仅 autoTradeEnabled 的单色语义）
            ===================================================================== */}
        <div style={{ marginBottom: 12 }}>
          <div
            style={{
              display: "flex",
              alignItems: "stretch",
              gap: 12,
              flexWrap: "wrap",
              padding: 12,
              borderRadius: 12,
              border: "1px solid var(--pt-border, #e5e7eb)",
              background: "var(--pt-surface-2, #f9fafb)",
            }}
          >
            <div style={{ flex: "1 1 240px", minWidth: 200 }}>
              <Statistic
                title={
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    {t("autoTradeReadiness")}
                    <Tooltip
                      title={
                        readinessLoading
                          ? t("autoTradeRunning")
                          : !readiness
                          ? t("autoTradeDisabled")
                          : ready
                          ? t("autoTradeStatusReady")
                          : blockers.length
                          ? t("autoTradeStatusNotReady")
                          : t("autoTradeStatusEnabledOnly")
                      }
                    >
                      <InfoCircleOutlined style={{ fontSize: 11, color: "#94a3b8" }} />
                    </Tooltip>
                  </span>
                }
                valueRender={() => {
                  if (readinessLoading || !readiness) {
                    return (
                      <Badge status="default" text={readinessLoading ? t("autoTradeRunning") : "-"} />
                    );
                  }
                  if (ready) {
                    return <Badge status="success" text={t("autoTradeReady") + " · " + t("autoTradeStatusReady")} />;
                  }
                  if (blockers.length) {
                    return (
                      <Badge status="error" text={t("autoTradeNotReady") + " · " + t("autoTradeStatusNotReady")} />
                    );
                  }
                  return (
                    <Badge status="warning" text={t("autoTradePartialReady") + " · " + t("autoTradeStatusEnabledOnly")} />
                  );
                }}
              />
            </div>

            {readiness && (
              <Space size={16} wrap style={{ flex: "1 1 380px", alignContent: "center" }}>
                <Statistic title={t("autoTradeSubStatusEnabled")} valueRender={() => <Badge status={readiness.enabled ? "success" : "default"} text={readiness.enabled ? "OK" : "NO"} />} />
                <Statistic title={t("autoTradeSubStatusAccount")} valueRender={() => <Badge status={readiness.account_ready ? "success" : "error"} text={readiness.account_ready ? "OK" : "NO"} />} />
                <Statistic title={t("autoTradeSubStatusData")} valueRender={() => <Badge status={readiness.data_ready ? "success" : "error"} text={readiness.data_ready ? "OK" : "NO"} />} />
                <Statistic title={t("autoTradeSubStatusSource")} valueRender={() => <Badge status={readiness.source_ready ? "success" : "error"} text={readiness.source_ready ? "OK" : "NO"} />} />
                <Statistic title={t("autoTradeSubStatusSchedule")} valueRender={() => <Badge status={readiness.schedule_ready ? "success" : "warning"} text={readiness.schedule_ready ? "OK" : "NO"} />} />
                {readiness.source_mode && (
                  <Statistic title="Source Mode" value={readiness.source_mode} />
                )}
              </Space>
            )}
          </div>

          <div style={{ marginTop: 8 }}>
            {readinessError && (
              <Alert type="warning" message={t("autoTradeFailed") + " (readiness)"} description={readinessError} showIcon style={{ marginBottom: 8 }} />
            )}
            {blockers.length > 0 && (
              <Alert
                type="error"
                showIcon
                message={t("autoTradeBlockers").replace("{count}", String(blockers.length))}
                description={
                  <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12, lineHeight: 1.7 }}>
                    {blockers.map((b, i) => {
                      const meta = getReadinessCodeMeta(b.code);
                      return (
                        <li key={`b-${i}`} style={{ marginBottom: 10 }}>
                          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
                            <Tag color="red" style={{ margin: 0 }}>
                              <span style={{ fontWeight: 600 }}>{meta.cn}</span>
                              <span style={{ fontSize: 10, color: "#9ca3af", marginLeft: 4, fontStyle: "italic" }}>
                                （{meta.dev}）
                              </span>
                            </Tag>
                            <span>{b.message}</span>
                            {typeof b.detail === "string" && b.detail ? (
                              <span style={{ color: "#94a3b8" }}>（{b.detail}）</span>
                            ) : null}
                          </div>
                          <ul style={{ margin: "4px 0 0 0", paddingLeft: 18, listStyle: "circle" }}>
                            {meta.fix.map((f, fi) => (
                              <li key={fi} style={{ color: "#0e7490" }}>
                                <span style={{ color: "#0891b2", fontWeight: 600 }}>修复指引：</span>
                                {f}
                              </li>
                            ))}
                          </ul>
                        </li>
                      );
                    })}
                  </ul>
                }
                style={{ marginBottom: 8 }}
              />
            )}
            {warnings.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={t("autoTradeWarnings").replace("{count}", String(warnings.length))}
                description={
                  <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12, lineHeight: 1.7 }}>
                    {warnings.map((w, i) => {
                      const meta = getReadinessCodeMeta(w.code);
                      return (
                        <li key={`w-${i}`} style={{ marginBottom: 10 }}>
                          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
                            <Tag color="orange" style={{ margin: 0 }}>
                              <span style={{ fontWeight: 600 }}>{meta.cn}</span>
                              <span style={{ fontSize: 10, color: "#78716c", marginLeft: 4, fontStyle: "italic" }}>
                                （{meta.dev}）
                              </span>
                            </Tag>
                            <span>{w.message}</span>
                          </div>
                          <ul style={{ margin: "4px 0 0 0", paddingLeft: 18, listStyle: "circle" }}>
                            {meta.fix.map((f, fi) => (
                              <li key={fi} style={{ color: "#92400e" }}>
                                <span style={{ color: "#d97706", fontWeight: 600 }}>建议：</span>
                                {f}
                              </li>
                            ))}
                          </ul>
                        </li>
                      );
                    })}
                  </ul>
                }
                style={{ marginBottom: 8 }}
              />
            )}
          </div>
        </div>

        {running && <Skeleton active paragraph={{ rows: 3 }} />}

        {/* P0-AutoTrade：execute 失败时结构化展示 blockers */}
        {!running && error && (
          <Alert
            type="error"
            message={errorBlockers?.length ? t("autoTradeExecBlockedTitle") : t("autoTradeFailed")}
            description={
              <div>
                <div style={{ marginBottom: 6 }}>
                  {errorBlockers?.length ? t("autoTradeExecBlockedDesc") + " " + t("autoTradeExecBlockedNextStep") : error}
                </div>
                {errorBlockers?.length ? (
                  <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12, lineHeight: 1.7 }}>
                    {errorBlockers.map((b, i) => {
                      const meta = getReadinessCodeMeta(b.code);
                      return (
                        <li key={`eb-${i}`} style={{ marginBottom: 10 }}>
                          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
                            <Tag color="red" style={{ margin: 0 }}>
                              <span style={{ fontWeight: 600 }}>{meta.cn}</span>
                              <span style={{ fontSize: 10, color: "#9ca3af", marginLeft: 4, fontStyle: "italic" }}>
                                （{meta.dev}）
                              </span>
                            </Tag>
                            <span>{b.message}</span>
                          </div>
                          <ul style={{ margin: "4px 0 0 0", paddingLeft: 18, listStyle: "circle" }}>
                            {meta.fix.map((f, fi) => (
                              <li key={fi} style={{ color: "#0e7490" }}>
                                <span style={{ color: "#0891b2", fontWeight: 600 }}>修复指引：</span>
                                {f}
                              </li>
                            ))}
                          </ul>
                        </li>
                      );
                    })}
                  </ul>
                ) : null}
              </div>
            }
            showIcon
            style={{ marginBottom: 12 }}
          />
        )}

        {!running && !error && !result && (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <div>
                <p>{t("autoTradeDryRunHint")}</p>
                {!dryRun ? <p style={{ color: "#94a3b8", marginTop: 4 }}>{t("autoTradeRequireDryRunFirst")}</p> : null}
              </div>
            }
          />
        )}

        {!running && !error && result && (
          <>
            <div style={{ marginBottom: 8, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Tag color={result.dry_run ? "blue" : "green"}>
                {result.dry_run ? t("autoTradeDryRunBadge") : t("autoTradeExecutedBadge")}
              </Tag>
              <span style={{ fontSize: 12, color: "#94a3b8" }}>
                {new Date(result.executed_at).toLocaleString()}
              </span>
              {result.executed_source && (
                <Tag color={result.executed_source === "new" ? "geekblue" : "default"}>
                  Source: {result.executed_source}
                </Tag>
              )}
              {result.source_mode && <Tag color="purple">Mode: {result.source_mode}</Tag>}
              {result.blockers?.length ? (
                <Tag color="red">
                  {t("autoTradeBlockers").replace("{count}", String(result.blockers.length))}
                </Tag>
              ) : null}
              {result.warnings?.length ? (
                <Tag color="gold">
                  {t("autoTradeWarnings").replace("{count}", String(result.warnings.length))}
                </Tag>
              ) : null}
            </div>

            {/* 卖出计划 */}
            <div style={{ marginBottom: 16 }}>
              <h3 style={{ fontSize: 14, marginBottom: 8 }}>
                {t("autoTradeSells").replace("{count}", String(sells.length))}
              </h3>
              {sells.length === 0 ? (
                <div className="empty" style={{ fontSize: 12, color: "#94a3b8" }}>
                  {t("autoTradeNoSells")}
                </div>
              ) : (
                <Table
                  size="small"
                  rowKey={(record, idx) => `sell-${record.symbol_id}-${idx}`}
                  dataSource={sells}
                  columns={sellColumns}
                  pagination={false}
                />
              )}
            </div>

            {/* 买入计划 */}
            <div style={{ marginBottom: 16 }}>
              <h3 style={{ fontSize: 14, marginBottom: 8 }}>
                {t("autoTradeBuys").replace("{count}", String(buys.length))}
              </h3>
              {buys.length === 0 ? (
                <div className="empty" style={{ fontSize: 12, color: "#94a3b8" }}>
                  {t("autoTradeNoBuys")}
                </div>
              ) : (
                <Table
                  size="small"
                  rowKey={(record, idx) => `buy-${record.symbol_id}-${idx}`}
                  dataSource={buys}
                  columns={buyColumns}
                  pagination={false}
                />
              )}
            </div>

            {/* 错误列表 */}
            {errors.length > 0 && (
              <div>
                <h3 style={{ fontSize: 14, marginBottom: 8, color: "#dc2626" }}>
                  {t("autoTradeErrors").replace("{count}", String(errors.length))}
                </h3>
                <ul style={{ fontSize: 12, color: "#dc2626", paddingLeft: 20 }}>
                  {errors.map((err, idx) => (
                    <li key={`err-${idx}`}>{err}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* 差异列表（execute 内联 diffs） */}
            {result.diffs?.length ? (
              <div style={{ marginTop: 16 }}>
                <h3 style={{ fontSize: 14, marginBottom: 8 }}>{t("autoTradeMember.dryRunDiff")} ({result.diffs.length})</h3>
                <Table
                  size="small"
                  rowKey={(record, idx) => `diffx-${record.symbol_id}-${record.side}-${idx}`}
                  dataSource={result.diffs as any}
                  columns={diffColumns as any}
                  pagination={false}
                />
              </div>
            ) : null}
          </>
        )}

        {/* ========================================================================
            WP6.6 新增分区：成员状态 / 双跑差异 / 开关管理
            ======================================================================== */}
        {autoTradeEnabled && (
          <>
            <div style={{ marginTop: 24, borderTop: "1px dashed #e5e7eb", paddingTop: 16 }}>
              <h3 style={{ fontSize: 14, marginBottom: 8, display: "flex", alignItems: "center" }}>
                {t("autoTradeMember.memberStatus")}
                <Tooltip title={t("autoTradeMember.memberStatusHint")}>
                  <QuestionCircleOutlined style={{ marginLeft: 6, fontSize: 12, color: "#94a3b8" }} />
                </Tooltip>
                <Button
                  size="small"
                  type="link"
                  onClick={loadMembers}
                  loading={membersLoading}
                  style={{ marginLeft: "auto", padding: 0 }}
                >
                  {t("refresh")}
                </Button>
              </h3>
              {membersLoading && <Skeleton active paragraph={{ rows: 2 }} />}
              {!membersLoading && membersError && (
                <Alert type="error" message={t("autoTradeMember.loadMembersFailed")} description={membersError} showIcon style={{ marginBottom: 8 }} />
              )}
              {!membersLoading && !membersError && members.length === 0 && (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("autoTradeMember.noMembers")} />
              )}
              {!membersLoading && !membersError && members.length > 0 && (
                <Table size="small" rowKey={(record) => `member-${record.member_id}`} dataSource={members} columns={memberColumns} pagination={false} />
              )}
            </div>

            <div style={{ marginTop: 24 }}>
              <h3 style={{ fontSize: 14, marginBottom: 8, display: "flex", alignItems: "center" }}>
                {t("autoTradeMember.dryRunDiff")}
                <Tooltip title={t("autoTradeMember.dryRunDiffHint")}>
                  <QuestionCircleOutlined style={{ marginLeft: 6, fontSize: 12, color: "#94a3b8" }} />
                </Tooltip>
                <Button
                  size="small"
                  type="link"
                  onClick={loadDiffs}
                  loading={diffLoading}
                  style={{ marginLeft: "auto", padding: 0 }}
                >
                  {t("refresh")}
                </Button>
              </h3>
              {diffLoading && <Skeleton active paragraph={{ rows: 2 }} />}
              {!diffLoading && diffError && (
                <Alert type="error" message={t("autoTradeMember.loadDiffFailed")} description={diffError} showIcon style={{ marginBottom: 8 }} />
              )}
              {!diffLoading && !diffError && diffs.length === 0 && (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("autoTradeMember.noDiffs")} />
              )}
              {!diffLoading && !diffError && diffs.length > 0 && (
                <Table
                  size="small"
                  rowKey={(record, idx) => `diff-${record.symbol_id}-${record.side}-${idx}`}
                  dataSource={diffs}
                  columns={diffColumns}
                  pagination={false}
                />
              )}
            </div>

            <div style={{ marginTop: 24 }}>
              <h3 style={{ fontSize: 14, marginBottom: 8, display: "flex", alignItems: "center" }}>
                {t("autoTradeMember.sourceSwitch")}
                <Tooltip title={t("autoTradeMember.sourceSwitchHint")}>
                  <QuestionCircleOutlined style={{ marginLeft: 6, fontSize: 12, color: "#94a3b8" }} />
                </Tooltip>
                <Button
                  size="small"
                  type="link"
                  onClick={loadSourceStatus}
                  loading={sourceLoading}
                  style={{ marginLeft: "auto", padding: 0 }}
                >
                  {t("refresh")}
                </Button>
              </h3>
              {sourceLoading && <Skeleton active paragraph={{ rows: 2 }} />}
              {!sourceLoading && sourceError && (
                <Alert type="error" message={t("autoTradeMember.loadSourceStatusFailed")} description={sourceError} showIcon style={{ marginBottom: 8 }} />
              )}
              {!sourceLoading && !sourceError && sourceStatus && (
                <div>
                  <div
                    style={{
                      display: "flex",
                      gap: 24,
                      flexWrap: "wrap",
                      alignItems: "center",
                      marginBottom: 12,
                    }}
                  >
                    <Statistic
                      title={
                        <span>
                          {t("autoTradeMember.sourceEnabled")}
                          <Tooltip title={t("autoTradeMember.sourceEnabledHint")}>
                            <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
                          </Tooltip>
                        </span>
                      }
                      valueRender={() => (
                        <Badge
                          status={sourceStatus.enabled ? "success" : "default"}
                          text={
                            sourceStatus.enabled
                              ? t("autoTradeMember.sourceEnabledOn")
                              : t("autoTradeMember.sourceEnabledOff")
                          }
                        />
                      )}
                    />
                    <Statistic title={t("autoTradeMember.envFlag")} value={sourceStatus.env_flag} />
                    <Statistic
                      title={
                        <span>
                          {t("autoTradeMember.whitelistMatch")}
                          <Tooltip title={t("autoTradeMember.whitelistMatchHint")}>
                            <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
                          </Tooltip>
                        </span>
                      }
                      valueRender={() => (
                        <Tag color={sourceStatus.whitelist_match ? "green" : "default"}>
                          {sourceStatus.whitelist_match ? "Yes" : "No"}
                        </Tag>
                      )}
                    />
                    <Statistic
                      title={
                        <span>
                          {t("autoTradeMember.blacklistMatch")}
                          <Tooltip title={t("autoTradeMember.blacklistMatchHint")}>
                            <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
                          </Tooltip>
                        </span>
                      }
                      valueRender={() => (
                        <Tag color={sourceStatus.blacklist_match ? "red" : "default"}>
                          {sourceStatus.blacklist_match ? "Yes" : "No"}
                        </Tag>
                      )}
                    />
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <Button
                      type="primary"
                      danger
                      icon={<RollbackOutlined />}
                      onClick={handleRollback}
                      loading={rollingBack}
                      disabled={!sourceStatus.enabled && !sourceStatus.whitelist_match}
                    >
                      {t("autoTradeMember.rollbackToOldSource")}
                    </Button>
                    <Tooltip title={t("autoTradeMember.rollbackButtonHint")}>
                      <QuestionCircleOutlined style={{ fontSize: 12, color: "#94a3b8" }} />
                    </Tooltip>
                    <span style={{ fontSize: 12, color: "#94a3b8" }}>
                      {t("autoTradeMember.whitelistLabel")}: [{sourceStatus.whitelist.join(", ") || "-"}]
                    </span>
                    <span style={{ fontSize: 12, color: "#94a3b8" }}>
                      {t("autoTradeMember.blacklistLabel")}: [{sourceStatus.blacklist.join(", ") || "-"}]
                    </span>
                  </div>
                </div>
              )}
              {!sourceLoading && !sourceError && !sourceStatus && (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("autoTradeMember.noSourceStatus")} />
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
