import React, { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, Briefcase, ChevronDown, Zap, Trophy, Plus, AlertTriangle, Lock, Activity, Database, ShieldCheck, CheckCircle2 } from "lucide-react";
import { Tooltip } from "antd";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t } from "../../i18n";
import type { AutoTradeReadiness, PortfolioStatePermissions, PortfolioStatus, PortfolioStatusResponse } from "../../types";
import PortfolioOverview from "./PortfolioOverview";
import PortfolioMembersTable from "./PortfolioMembersTable";
import PortfolioStrategyRules from "./PortfolioStrategyRules";
import PortfolioBacktestCenter from "./PortfolioBacktestCenter";
import PortfolioGovernanceTab from "./PortfolioGovernanceTab";
import CreatePortfolioModal from "./CreatePortfolioModal";
import AddCandidateModal from "./AddCandidateModal";
import PortfolioRankingDrawer from "./PortfolioRankingDrawer";
import PortfolioSelectorDropdown from "./PortfolioSelectorDropdown";

/**
 * PortfolioTradingShell — 组合交易主壳
 *
 * 一比一还原原型图「应用主框架.html」的组合头部 + 4 子 Tab 导航结构。
 * 主题根节点 .pt-theme-scope 提供 scoped 深色青色主题（Task 1 已在 workbench.css 落地）。
 *
 * 结构：
 *   <div className="pt-theme-scope">
 *     <PortfolioHeader />        // 组合头部
 *     <SubTabNav />              // 4 子 Tab nav
 *     <SubTabContent />          // 子 Tab 内容路由
 *     <CreatePortfolioModal />   // 浮层
 *     <AddCandidateModal />
 *     <PortfolioRankingDrawer />
 *     <PortfolioSelectorDropdown />
 *   </div>
 *
 * i18n：本组件使用 t('portfolioTrading.xxx') 形式调用，对应 key 由 Task 13 补全，
 * 缺失时 t() 返回 key 字符串，不阻塞编译。
 */

type SubTabKey = "overview" | "members" | "strategy" | "backtest" | "governance";

const VALID_SUB_TABS: SubTabKey[] = ["overview", "members", "strategy", "backtest", "governance"];

/** 将 AppContext.activeSubTab 规整为 4 子 Tab 之一，非法值回退 'overview'。 */
function resolveSubTab(raw: string | undefined): SubTabKey {
  if (raw && (VALID_SUB_TABS as string[]).includes(raw)) {
    return raw as SubTabKey;
  }
  return "overview";
}

/** 实盘/模拟标签：account_type='manual' → 实盘，其余 → 模拟（默认模拟）。 */
function accountTypeLabel(accountType: string | undefined): string {
  return accountType === "manual"
    ? t("portfolioTrading.tag.live")
    : t("portfolioTrading.tag.simulated");
}

/* ==========================================================================
 * 就绪检查 code → 中文名 + 修复指引
 *   · 用户看到的 tooltip 中方括号改为「中文（原英文code）」格式
 *     → 用户先看中文，开发排错仍保留原枚举线索
 *   · 每个 code 配一组 fix[]：直接告诉用户"现在我该点哪里"
 * ======================================================================== */
type ReadinessCodeMeta = { cn: string; category: "开关" | "账户" | "来源" | "规则" | "数据" | "范围" | "调度" | "其他"; fix: string[] };
const READINESS_CODE_META: Record<string, ReadinessCodeMeta> = {
  PORTFOLIO_DISABLED: {
    cn: "自动交易开关未开", category: "开关",
    fix: ["打开「自动接管」卡片右侧总开关；或前往治理 Tab 启用自动交易。"],
  },
  AUTO_TRADE_DISABLED: {
    cn: "自动交易开关未开", category: "开关",
    fix: ["打开「自动接管」卡片右侧总开关；或前往治理 Tab 启用自动交易。"],
  },
  ACCOUNT_NOT_SIMULATED: {
    cn: "账户非模拟", category: "账户",
    fix: [
      "当前自动交易仅支持「模拟」账户类型。",
      "操作：打开左上角「组合选择器→编辑组合」，把账户类型改为「模拟」；若需实盘自动，请先跑通模拟全流程后再升级。",
    ],
  },
  SOURCE_MODE_INVALID: {
    cn: "来源模式非法", category: "来源",
    fix: [
      "前往「组合→设置→自动交易来源」或「治理 Tab」：把 auto_trade_source_mode 选为合法值 portfolio / members_only / legacy_scan。",
    ],
  },
  SOURCE_LEGACY_SCAN: {
    cn: "使用旧全局扫描来源", category: "来源",
    fix: [
      "属于警告，不阻断执行，但建议迁移：在「治理 Tab→自动交易来源」切为 portfolio 或 members_only，可获得更稳定的新鲜度检查与范围。",
    ],
  },
  WARNING_SOURCE_MODE_LEGACY_SCAN: {
    cn: "使用旧全局扫描来源", category: "来源",
    fix: [
      "属于警告，不阻断执行，但建议迁移：在「治理 Tab→自动交易来源」切为 portfolio 或 members_only。",
    ],
  },
  SOURCE_ENV_OVERRIDE: {
    cn: "成员来源被环境变量熔断", category: "来源",
    fix: [
      "检查部署环境：是否设置了 `MEMBER_SOURCE_ENABLED=false`（紧急时期用于全局熔断成员来源）。",
      "如非紧急情形，把开关改回 true；否则真实执行会回退到旧扫描逻辑。",
    ],
  },
  WARNING_SOURCE_ENV_OVERRIDE: {
    cn: "成员来源被环境变量熔断", category: "来源",
    fix: [
      "检查部署环境：是否设置了成员来源全局熔断开关；如非紧急，打开后可消除本警告。",
    ],
  },
  RULE_NOT_CONFIGURED: {
    cn: "未配置激活策略规则", category: "规则",
    fix: [
      "切换到「策略规则」Tab：选好选股池、风控阈值、调仓周期，然后**点击保存**，生成一条激活版本的规则快照。",
      "如果用路径B绑定训练产出：请先在「因子模型训练页」训练一个「已验证」状态的模型，再回到本页下拉选中。",
    ],
  },
  NO_RULE: {
    cn: "缺少激活规则", category: "规则",
    fix: ["切到「策略规则」Tab，保存一次激活版的规则。"],
  },
  RULE_INVALID_PARAMS: {
    cn: "策略规则参数非法", category: "规则",
    fix: [
      "前往「策略规则」Tab 修复标红字段：通常是 max_single_position_pct / max_loss_per_trade_pct 等百分比超出 0~1 合法范围。",
      "另一个常见原因：「阶段限制 stage_limits_json」为空——请在风控阶段限制模块里保存有效 JSON。",
    ],
  },
  MARKET_DATA_STALE: {
    cn: "行情或评分数据过期", category: "数据",
    fix: [
      "① 前往「设置 → 数据中心 → 行情底座」：点击「智能同步」或「每日同步」，把 A 股股票和指数行情拉到今天。",
      "② 切换到「因子输入」Tab：点「同步/重算」候选的股票估值、财报历史、人气榜等因子依赖，直到候选新鲜度全部变为「今日」。",
      "③ 同步完成后，点击页面中间「就绪检查」旁的「刷新状态」，readiness 会立即重算。",
    ],
  },
  NO_FRESH_SCORES: {
    cn: "最新评分缺失", category: "数据",
    fix: [
      "前往「因子输入 Tab」或「策略规则 Tab」：点「同步候选评分」，重新跑一轮候选打分，让评分更新时间刷新到今日。",
    ],
  },
  NO_TRADEABLE_RANGE: {
    cn: "无可交易范围", category: "范围",
    fix: [
      "当前既没持仓可卖，也没有任何可执行买入计划。",
      "操作：① 先在「候选/持仓成员」Tab 手动添加几只股票；② 同步行情+评分（修复 MARKET_DATA_STALE）；③ 点「Dry Run 预演」让系统产生买入信号。",
    ],
  },
  SCHEDULE_DISABLED: {
    cn: "定时任务未创建或暂停", category: "调度",
    fix: [
      "前往「治理 → 调度任务」：创建或启用 `task_type=portfolio_auto_trade` 的定时任务，推荐每天 20:00 之后运行（对齐调度时间窗 HG2）。",
      "注：手动 Dry Run / 真实执行不受调度影响，调度只负责「自动按天跑」。",
    ],
  },
  NO_SCHEDULED_TASK: {
    cn: "未创建自动交易定时任务", category: "调度",
    fix: [
      "前往「治理 → 调度任务」，新建一条 type=portfolio_auto_trade 的定时任务。",
    ],
  },
  SCHEDULED_TASK_DISABLED: {
    cn: "定时任务已暂停", category: "调度",
    fix: ["前往「治理 → 调度任务」，把对应的定时任务切为「启用」。"],
  },
  SCHEDULE_WINDOW: {
    cn: "不在调度时间窗", category: "调度",
    fix: ["调度型就绪检查只会在每日允许时间段（默认 20:00 之后）判定为通过。手动执行 Dry Run / 真实下单不受此限。"],
  },
  NO_AUTO_MEMBERS: {
    cn: "无自动模式成员", category: "范围",
    fix: [
      "前往「持仓成员」Tab：找到希望自动管理的成员行，把「执行模式」从「手动/确认」切为「自动」。",
      "如果「自动交易来源 = portfolio」：即使没有自动成员，也可以直接靠候选池走自动买入（建仓时会自动创建成员）。",
    ],
  },
  NO_CANDIDATES: {
    cn: "无候选也无持仓", category: "范围",
    fix: [
      "点击页面右上角「+ 添加候选标的」，先加入若干只股票作为自动交易的选股范围。",
      "没有候选也没有已有持仓的组合，自动交易没有工作对象，不会产生任何买卖动作。",
    ],
  },
  NO_BUY_SIGNALS_TODAY: {
    cn: "今日暂无买入信号", category: "范围",
    fix: [
      "属于「今日节奏」类警告（非阻塞）：说明当前候选打分没有产生买入 action，属于正常节奏。",
      "如果希望主动产生信号：① 扩大选股池 / 加入更多候选；② 在「策略规则」调整因子权重或降低风控阈值；③ 先执行 Dry Run 查看真实打分明细。",
    ],
  },
  DECISION_ENGINE_SOURCE_REQUIRED: {
    cn: "决策引擎来源缺失", category: "来源",
    fix: ["前往「设置 → 决策引擎」：打开成员来源订单计划的全局开关 members_source_enabled。"],
  },
  STRATEGY_SNAPSHOT_REQUIRED: {
    cn: "缺少策略执行快照", category: "规则",
    fix: [
      "先在「策略规则 Tab」保存一次激活规则，然后跑一轮「Dry Run」或「回测」，系统会自动生成一份策略执行快照并应用。",
    ],
  },
  NOT_READY: {
    cn: "通用未就绪", category: "其他",
    fix: ["查看 blockers 列表，按对应 code 逐项修复；或先执行一次 Dry Run 查看更详细的阻断明细。"],
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

/* ==========================================================================
 * FR-P0-10 / FR-P1-8a HG1：9 状态配色（与 PortfolioSelectorDropdown/GovernanceTab 严格对齐）
 * ======================================================================== */
const PORTFOLIO_STATUS_STYLES: Partial<Record<PortfolioStatus | string, {
  bg: string; fg: string; border: string; dot: string;
  title: string; hint: string;
}>> = {
  PENDING_INITIAL_REVIEW: { bg: "#fef3c7", fg: "#7c2d12", border: "#f59e0b", dot: "#f59e0b", title: "待初始审查", hint: "新建组合尚未通过管理员合规审查，禁止所有交易动作。" },
  READY:                    { bg: "#ecfdf5", fg: "#065f46", border: "#10b981", dot: "#10b981", title: "生产就绪", hint: "HG1 门禁全项通过，可正常交易。" },
  RUNNING_AUTO_SIMULATION:  { bg: "#eff6ff", fg: "#1e3a8a", border: "#3b82f6", dot: "#3b82f6", title: "自动推演中", hint: "20:30 自动推演（auto-simulation）运行中，买单能力同 READY。" },
  RUNNING_BACKTEST:         { bg: "#eef2ff", fg: "#3730a3", border: "#6366f1", dot: "#6366f1", title: "回测中", hint: "后台回测运行中，不影响前台交易。" },
  DATA_INCOMPLETE_PAUSED:   { bg: "#fef9c3", fg: "#713f12", border: "#eab308", dot: "#eab308", title: "数据缺失暂停", hint: "数据缺口触发软暂停：禁止 NEW_BUY，允许 RISK_EXIT。" },
  RECONCILIATION_BLOCKED:   { bg: "#fee2e2", fg: "#7f1d1d", border: "#ef4444", dot: "#ef4444", title: "对账差异阻塞", hint: "对账差异非零，禁止新买单，需治理 Tab 单人确认。" },
  MODEL_INACTIVE:           { bg: "#f3f4f6", fg: "#1f2937", border: "#6b7280", dot: "#6b7280", title: "模型未激活", hint: "绑定模型已退役/未激活，禁止新买单。" },
  SCORE_STALE:              { bg: "#fff7ed", fg: "#7c2d12", border: "#f97316", dot: "#f97316", title: "Score 不新鲜", hint: "Score 覆盖率/新鲜度未通过门禁，禁止新买单。" },
  INTERRUPTED:              { bg: "#fae8ff", fg: "#701a75", border: "#d946ef", dot: "#d946ef", title: "任务异常中断", hint: "Worker 心跳超时，恢复扫描器将尝试自动接续。" },
  ADMIN_PAUSED:             { bg: "#fef2f2", fg: "#7f1d1d", border: "#dc2626", dot: "#dc2626", title: "管理员刹车", hint: "紧急暂停：禁止新买单/风险退出/自动恢复。" },
};
const PORTFOLIO_STATUS_FALLBACK = { bg: "#f3f4f6", fg: "#374151", border: "#9ca3af", dot: "#9ca3af", title: "状态未知", hint: "未从治理 API 读取到组合状态。" };

/** FR-P1-8a 9×4 权限矩阵静态兜底（API 未返回 permissions 时使用，与 GovernanceTab 对齐）。 */
const HG1_FALLBACK_MATRIX: Record<PortfolioStatus | string, PortfolioStatePermissions> = {
  PENDING_INITIAL_REVIEW:  { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  READY:                   { allow_new_buys: true,  allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  RUNNING_AUTO_SIMULATION: { allow_new_buys: true,  allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  RUNNING_BACKTEST:        { allow_new_buys: true,  allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  DATA_INCOMPLETE_PAUSED:  { allow_new_buys: false, allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  RECONCILIATION_BLOCKED:  { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  MODEL_INACTIVE:          { allow_new_buys: false, allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  SCORE_STALE:             { allow_new_buys: false, allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  INTERRUPTED:             { allow_new_buys: false, allow_risk_exits: true,  allow_auto_recovery: true,  requires_manual_ack: false },
  ADMIN_PAUSED:            { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  UNKNOWN:                 { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
};
function resolveShellPermissions(status: PortfolioStatusResponse | null): PortfolioStatePermissions {
  if (!status) return HG1_FALLBACK_MATRIX.UNKNOWN;
  if (status.permissions) return status.permissions;
  return HG1_FALLBACK_MATRIX[status.current_state] ?? HG1_FALLBACK_MATRIX.UNKNOWN;
}

interface SubTabDef {
  key: SubTabKey;
  label: string;
}

const SUB_TABS: SubTabDef[] = [
  { key: "overview", label: t("portfolioTrading.subtab.overview") },
  { key: "members", label: t("portfolioTrading.subtab.members") },
  { key: "strategy", label: t("portfolioTrading.subtab.strategy") },
  { key: "backtest", label: t("portfolioTrading.subtab.backtest") },
  { key: "governance", label: t("portfolioTrading.subtab.governance") },
];

const PortfolioTradingShell: React.FC = () => {
  const { portfolios, portfolioId, switchPortfolio, activeSubTab, setActiveSubTab, loadWorkbench } = useApp();

  // 浮层状态
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [addCandidateModalOpen, setAddCandidateModalOpen] = useState(false);
  const [rankingDrawerOpen, setRankingDrawerOpen] = useState(false);
  const [selectorDropdownOpen, setSelectorDropdownOpen] = useState(false);
  const [membersRevision, setMembersRevision] = useState(0);

  // 子 Tab 状态：与 AppContext.activeSubTab 同步
  const [localSubTab, setLocalSubTab] = useState<SubTabKey>(() => resolveSubTab(activeSubTab));

  // 当前组合（从 portfolios 派生）
  const currentPortfolio = useMemo(
    () => portfolios.find((p) => p.id === portfolioId) ?? portfolios[0] ?? null,
    [portfolios, portfolioId],
  );
  const currentPortfolioName = currentPortfolio?.name ?? t("portfolioTrading.header.noPortfolio");
  const currentAccountTypeLabel = accountTypeLabel(currentPortfolio?.account_type);
  const currentAssetScopeLabel =
    currentPortfolio?.asset_scope === "stock"
      ? "股票组合"
      : currentPortfolio?.asset_scope === "etf"
        ? "ETF 组合"
        : "混合组合";
  const effectivePortfolioId = currentPortfolio?.id ?? 0;
  // P0-FIX: 从真实组合 auto_trade_enabled 派生自动接管状态（0=关闭，1=开启）
  const autoTradeEnabled = currentPortfolio ? Number(currentPortfolio.auto_trade_enabled) === 1 : false;
  const isSimulated = currentPortfolio?.account_type === "simulated";

  // FR-P0-10/P1-8a：组合治理状态 + 权限
  const [portfolioStatus, setPortfolioStatus] = useState<PortfolioStatusResponse | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const loadPortfolioStatus = useCallback(async () => {
    if (!effectivePortfolioId) {
      setPortfolioStatus(null);
      return;
    }
    setStatusLoading(true);
    try {
      const data = await api.getPortfolioStatus(effectivePortfolioId);
      setPortfolioStatus(data ?? null);
    } catch {
      setPortfolioStatus(null);
    } finally {
      setStatusLoading(false);
    }
  }, [effectivePortfolioId]);
  useEffect(() => {
    void loadPortfolioStatus();
    let handle: ReturnType<typeof setInterval> | null = null;
    if (effectivePortfolioId) handle = setInterval(() => void loadPortfolioStatus(), 45000);
    return () => { if (handle) clearInterval(handle); };
  }, [effectivePortfolioId, loadPortfolioStatus]);
  // 从 Portfolio 接口派生的状态（当 governance API 还没返回时，优先用列表接口里的 portfolio_status 显示色）
  const portfolioStatusRaw: PortfolioStatus | string | null | undefined =
    portfolioStatus?.current_state ?? currentPortfolio?.portfolio_status;
  const perm = useMemo<PortfolioStatePermissions>(
    () => resolveShellPermissions(portfolioStatus),
    [portfolioStatus],
  );
  // FR-P1-8a：综合判定治理层是否阻断操作（需要人工确认的终态/严重态）
  const governanceBlocked = useMemo<boolean>(() => {
    if (perm.requires_manual_ack) return true;
    if (!portfolioStatusRaw) return false;
    return ["ADMIN_PAUSED", "RECONCILIATION_BLOCKED", "PENDING_INITIAL_REVIEW"].includes(String(portfolioStatusRaw));
  }, [perm, portfolioStatusRaw]);

  // P0-AutoTrade：真实就绪状态 readdy（取代仅 autoTradeEnabled 的单色显示）
  const [readiness, setReadiness] = useState<AutoTradeReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const loadReadiness = useCallback(async () => {
    if (!effectivePortfolioId || !isSimulated) {
      setReadiness(null);
      return;
    }
    setReadinessLoading(true);
    try {
      const data = (await api.getAutoTradeReadiness(effectivePortfolioId, { for_schedule: true })) as AutoTradeReadiness;
      setReadiness(data);
    } catch {
      setReadiness(null);
    } finally {
      setReadinessLoading(false);
    }
  }, [effectivePortfolioId, isSimulated]);
  useEffect(() => {
    void loadReadiness();
    let handle: ReturnType<typeof setInterval> | null = null;
    if (effectivePortfolioId && isSimulated) {
      handle = setInterval(() => void loadReadiness(), 30000);
    }
    return () => {
      if (handle) clearInterval(handle);
    };
  }, [effectivePortfolioId, isSimulated, loadReadiness]);

  const ready = readiness?.ready === true;
  const hasBlockers = (readiness?.blockers?.length ?? 0) > 0;

  // 切换子 Tab：同步本地 + AppContext
  const handleSubTabChange = useCallback(
    (tab: SubTabKey) => {
      setLocalSubTab(tab);
      setActiveSubTab(tab);
    },
    [setActiveSubTab],
  );

  // 组合选择下拉 → 切换组合
  const handleSelectPortfolio = useCallback(
    async (id: number) => {
      setSelectorDropdownOpen(false);
      try {
        await switchPortfolio(id);
      } catch {
        /* switchPortfolio 内部已 toast */
      }
    },
    [switchPortfolio],
  );

  // 组合选择下拉 → 新建组合
  const handleCreateNewFromSelector = useCallback(() => {
    setSelectorDropdownOpen(false);
    setCreateModalOpen(true);
  }, []);

  // 新建组合成功 → 切换到新组合
  const handleCreateSuccess = useCallback(
    async (id: number) => {
      setCreateModalOpen(false);
      try {
        await switchPortfolio(id);
      } catch {
        /* ignore */
      }
    },
    [switchPortfolio],
  );

  // 排名抽屉 → 选中组合
  const handleRankingSelect = useCallback(
    async (id: number) => {
      setRankingDrawerOpen(false);
      try {
        await switchPortfolio(id);
      } catch {
        /* ignore */
      }
    },
    [switchPortfolio],
  );

  const handleCandidatesAdded = useCallback(() => {
    setMembersRevision((value) => value + 1);
    void loadWorkbench();
  }, [loadWorkbench]);

  return (
    <div className="pt-theme-scope pt-portfolio-workbench">
      {/* scoped 动画（脉冲圆点 / 闪电呼吸），自包含不污染全局 */}
      <style>{`
        @keyframes pt-dot-pulse {
          0%, 100% { box-shadow: 0 0 0 0 rgba(16,185,129,0.55); }
          50% { box-shadow: 0 0 0 5px rgba(16,185,129,0); }
        }
        .pt-status-dot-pulse { animation: pt-dot-pulse 2s ease-in-out infinite; }
        @keyframes pt-bolt-pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.55; }
        }
        .pt-bolt-pulse { animation: pt-bolt-pulse 2s ease-in-out infinite; }
      `}</style>

      {/* ========== 组合头部 ========== */}
      <PortfolioHeader
        portfolioName={currentPortfolioName}
        accountTypeLabel={currentAccountTypeLabel}
        assetScopeLabel={currentAssetScopeLabel}
        autoTradeEnabled={autoTradeEnabled}
        autoTradeReady={ready}
        autoTradeHasBlockers={hasBlockers}
        autoTradeLoading={readinessLoading}
        autoTradeReadiness={readiness}
        onRefreshReadiness={loadReadiness}
        onOpenSelector={() => setSelectorDropdownOpen((v) => !v)}
        onOpenRanking={() => setRankingDrawerOpen(true)}
        onOpenAddCandidate={() => setAddCandidateModalOpen(true)}
        selectorOpen={selectorDropdownOpen}
        portfolioStatusRaw={portfolioStatusRaw}
        statusLoading={statusLoading}
        governanceBlocked={governanceBlocked}
        allowNewBuys={perm.allow_new_buys}
        selectorDropdown={
          <PortfolioSelectorDropdown
            open={selectorDropdownOpen}
            onClose={() => setSelectorDropdownOpen(false)}
            onSelect={handleSelectPortfolio}
            onCreateNew={handleCreateNewFromSelector}
            currentPortfolioId={currentPortfolio?.id}
          />
        }
      />

      {/* ========== 4 子 Tab nav ========== */}
      <SubTabNav active={localSubTab} onChange={handleSubTabChange} />

      {/* ========== 子 Tab 内容路由 ========== */}
      <SubTabContent
        active={localSubTab}
        portfolioId={effectivePortfolioId}
        onNavigate={(tab) => handleSubTabChange(resolveSubTab(tab))}
        autoTradeEnabled={autoTradeEnabled}
        membersRevision={membersRevision}
        portfolioStatus={portfolioStatus}
        perm={perm}
      />

      {/* ========== 浮层 ========== */}
      <CreatePortfolioModal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onSuccess={handleCreateSuccess}
      />
      <AddCandidateModal
        open={addCandidateModalOpen}
        onClose={() => setAddCandidateModalOpen(false)}
        portfolioId={effectivePortfolioId}
        onSuccess={handleCandidatesAdded}
        portfolioStatus={portfolioStatus}
        perm={perm}
        statusLoading={statusLoading}
      />
      <PortfolioRankingDrawer
        open={rankingDrawerOpen}
        onClose={() => setRankingDrawerOpen(false)}
        onSelectPortfolio={handleRankingSelect}
      />
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* 组合头部 —— 一比一对齐原型图 portfolio-header                       */
/* ------------------------------------------------------------------ */

interface PortfolioHeaderProps {
  portfolioName: string;
  accountTypeLabel: string;
  assetScopeLabel: string;
  autoTradeEnabled: boolean;
  autoTradeReady?: boolean;
  autoTradeHasBlockers?: boolean;
  autoTradeLoading?: boolean;
  autoTradeReadiness?: AutoTradeReadiness | null;
  onRefreshReadiness?: () => void;
  selectorOpen: boolean;
  onOpenSelector: () => void;
  onOpenRanking: () => void;
  onOpenAddCandidate: () => void;
  /** 组合选择下拉面板（在触发按钮的 relative 容器内渲染，保证 position:absolute 浮在按钮正下方） */
  selectorDropdown?: React.ReactNode;
  // FR-P0-10/P1-8a HG1 字段
  portfolioStatusRaw?: PortfolioStatus | string | null;
  statusLoading?: boolean;
  governanceBlocked?: boolean;
  allowNewBuys?: boolean;
}

const PortfolioHeader: React.FC<PortfolioHeaderProps> = ({
  portfolioName,
  accountTypeLabel,
  assetScopeLabel,
  autoTradeEnabled,
  autoTradeReady,
  autoTradeHasBlockers,
  autoTradeLoading,
  autoTradeReadiness,
  onRefreshReadiness,
  selectorOpen,
  onOpenSelector,
  onOpenRanking,
  onOpenAddCandidate,
  selectorDropdown,
  portfolioStatusRaw,
  statusLoading,
  allowNewBuys = true,
}) => {
  // P0-AutoTrade：语义颜色
  // ── FR-P1-8a 修正：先看 portfolio_status 是否阻塞（ADMIN_PAUSED/RECONCILIATION_BLOCKED → 红 notReady） ──
  // enabled=false → 灰色 off
  // enabled=true + (ADMIN_PAUSED/RECONCILIATION_BLOCKED/PENDING_INITIAL_REVIEW) → 红 blockedByGovernance
  // enabled=true + ready=true → 绿色 ready&enabled
  // enabled=true + ready=false + blockers → 红色 notReady
  // enabled=true + ready=false + no blockers + readiness loaded → 黄色 enabledOnly
  // readiness still loading → 蓝色/未知
  const loaded = autoTradeReadiness != null;
  const governanceBlocked =
    portfolioStatusRaw === "ADMIN_PAUSED" ||
    portfolioStatusRaw === "RECONCILIATION_BLOCKED" ||
    portfolioStatusRaw === "PENDING_INITIAL_REVIEW";
  const statusColor: "off" | "ready" | "notReady" | "enabledOnly" | "unknown" = !autoTradeEnabled
    ? "off"
    : governanceBlocked
    ? "notReady"
    : autoTradeLoading && !loaded
    ? "unknown"
    : autoTradeReady
    ? "ready"
    : loaded && autoTradeHasBlockers
    ? "notReady"
    : loaded
    ? "enabledOnly"
    : "unknown";

  const colorMap: Record<typeof statusColor, { bg: string; fg: string; dot: string; pulseClassName: string }> = {
    off: {
      bg: "var(--pt-surface-3)",
      fg: "var(--pt-muted-foreground)",
      dot: "var(--pt-muted-foreground)",
      pulseClassName: "",
    },
    ready: {
      bg: "var(--pt-state-success-dim)",
      fg: "var(--pt-state-success)",
      dot: "var(--pt-state-success)",
      pulseClassName: "pt-status-dot-pulse",
    },
    notReady: {
      bg: "rgba(239,68,68,0.12)",
      fg: "var(--pt-state-error, #ef4444)",
      dot: "var(--pt-state-error, #ef4444)",
      pulseClassName: "",
    },
    enabledOnly: {
      bg: "rgba(245,158,11,0.14)",
      fg: "var(--pt-state-warning, #f59e0b)",
      dot: "var(--pt-state-warning, #f59e0b)",
      pulseClassName: "",
    },
    unknown: {
      bg: "rgba(59,130,246,0.12)",
      fg: "var(--pt-primary, #3b82f6)",
      dot: "var(--pt-primary, #3b82f6)",
      pulseClassName: "",
    },
  };
  const palette = colorMap[statusColor];

  const statusText =
    !autoTradeEnabled
      ? t("portfolioTrading.status.paused")
      : autoTradeReady
      ? t("portfolioTrading.status.running")
      : t("portfolioTrading.status.notReady");

  const pillText =
    statusColor === "off"
      ? t("portfolioTrading.autotrade.off")
      : statusColor === "ready"
      ? t("portfolioTrading.autotrade.readyAndEnabled")
      : statusColor === "notReady"
      ? t("portfolioTrading.autotrade.notReady")
      : statusColor === "enabledOnly"
      ? t("portfolioTrading.autotrade.enabledOnly")
      : t("portfolioTrading.autotrade.unknown");

  // —— 顶栏「待就绪」徽章的 Tooltip：结构化（中文 code 名 + 原英文括号小字 + 每项修复指引）
  const pillHint: React.ReactNode = useMemo(() => {
    if (!autoTradeEnabled) return <span>{t("autoTradeDisabled")}</span>;
    const subStatuses: Array<{ label: string; no: boolean }> = autoTradeReadiness
      ? [
          { label: t("autoTradeSubStatusEnabled"), no: !autoTradeReadiness.enabled },
          { label: t("autoTradeSubStatusAccount"), no: !autoTradeReadiness.account_ready },
          { label: t("autoTradeSubStatusData"), no: !autoTradeReadiness.data_ready },
          { label: t("autoTradeSubStatusSource"), no: !autoTradeReadiness.source_ready },
          { label: t("autoTradeSubStatusSchedule"), no: !autoTradeReadiness.schedule_ready },
        ]
      : [];
    const subNoMap: Record<string, boolean> = {};
    subStatuses.forEach((s) => (subNoMap[s.label] = s.no));

    const headLine =
      statusColor === "ready"
        ? t("autoTradeStatusReady")
        : statusColor === "notReady"
        ? t("autoTradeStatusNotReady")
        : statusColor === "enabledOnly"
        ? t("autoTradeStatusEnabledOnly")
        : t("autoTradeRunning");

    const blockers = autoTradeReadiness?.blockers?.slice(0, 6) ?? [];
    const warnings = autoTradeReadiness?.warnings?.slice(0, 4) ?? [];

    return (
      <div style={{ maxWidth: 460, fontSize: 12, lineHeight: 1.7, color: "#e5e7eb" }}>
        {/* 1. HG1 门禁优先 */}
        {governanceBlocked && (() => {
          const meta = portfolioStatusRaw
            ? PORTFOLIO_STATUS_STYLES[portfolioStatusRaw] ?? PORTFOLIO_STATUS_FALLBACK
            : PORTFOLIO_STATUS_FALLBACK;
          return (
            <div
              style={{
                marginBottom: 8,
                padding: "6px 8px",
                borderRadius: 6,
                background: "rgba(220,38,38,0.14)",
                border: "1px solid rgba(220,38,38,0.4)",
              }}
            >
              <span style={{ color: "#fca5a5", fontWeight: 600 }}>🔒 HG1 门禁：{meta.title}</span>
              <div style={{ color: "#f3f4f6", marginTop: 2 }}>{meta.hint}</div>
            </div>
          );
        })()}

        {/* 2. 大状态行 + 5 个子状态（开关/账户/数据/来源/调度） */}
        <div style={{ fontWeight: 600, marginBottom: 6, color: "#fecaca" }}>{headLine}</div>
        {subStatuses.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 10px", marginBottom: 8 }}>
            {subStatuses.map((s) => (
              <span key={s.label} style={{ color: s.no ? "#fca5a5" : "#86efac", whiteSpace: "nowrap" }}>
                {s.label}: <strong style={{ fontWeight: 700 }}>{s.no ? "NO" : "OK"}</strong>
              </span>
            ))}
          </div>
        )}

        {/* 3. Blockers（阻塞项）：每项「中文名（原code）」+ 后端 message + 修复指引分步骤 */}
        {blockers.length > 0 && (
          <div style={{ marginBottom: 8 }}>
            <div style={{ fontWeight: 600, color: "#fca5a5", marginBottom: 4 }}>
              ❌ 阻塞项（{blockers.length}，真实下单会被拒绝）
            </div>
            <ul style={{ margin: 0, paddingLeft: 18, listStyle: "disc" }}>
              {blockers.map((b, i) => {
                const meta = getReadinessCodeMeta(b.code);
                // 把「数据: NO · [中文名（code）]」格式拼出来，和用户截图旧格式习惯对齐
                const subForCode = subNoMap[meta.category]
                  ? `${meta.category}: NO · `
                  : "";
                return (
                  <li key={`blk-${i}`} style={{ marginBottom: 6 }}>
                    <div>
                      <span style={{ color: "#fca5a5" }}>{subForCode}[</span>
                      <span style={{ color: "#fff", fontWeight: 600 }}>{meta.cn}</span>
                      <span style={{ color: "#94a3b8", fontStyle: "italic" }}>（{meta.dev}）</span>
                      <span style={{ color: "#fca5a5" }}>]</span>{" "}
                      <span>{b.message}</span>
                      {typeof (b as any).detail === "string" && (b as any).detail ? (
                        <span style={{ color: "#94a3b8", marginLeft: 6 }}>（{(b as any).detail}）</span>
                      ) : null}
                    </div>
                    {/* 修复指引 */}
                    <ul style={{ margin: "4px 0 0 0", paddingLeft: 16, listStyle: "circle" }}>
                      {meta.fix.map((f, fi) => (
                        <li key={fi} style={{ color: "#a5f3fc" }}>
                          <span style={{ color: "#22d3ee", fontWeight: 600 }}>修复指引：</span>
                          {f}
                        </li>
                      ))}
                    </ul>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {/* 4. Warnings（警告项，非阻塞） */}
        {warnings.length > 0 && (
          <div>
            <div style={{ fontWeight: 600, color: "#fcd34d", marginBottom: 4 }}>
              ⚠️ 注意事项（{warnings.length}，暂不阻断，但建议修复）
            </div>
            <ul style={{ margin: 0, paddingLeft: 18, listStyle: "disc" }}>
              {warnings.map((w, i) => {
                const meta = getReadinessCodeMeta(w.code);
                return (
                  <li key={`warn-${i}`} style={{ marginBottom: 6 }}>
                    <div>
                      <span style={{ color: "#fcd34d" }}>[</span>
                      <span style={{ color: "#fff", fontWeight: 600 }}>{meta.cn}</span>
                      <span style={{ color: "#94a3b8", fontStyle: "italic" }}>（{meta.dev}）</span>
                      <span style={{ color: "#fcd34d" }}>]</span>{" "}
                      <span>{w.message}</span>
                    </div>
                    <ul style={{ margin: "4px 0 0 0", paddingLeft: 16, listStyle: "circle" }}>
                      {meta.fix.map((f, fi) => (
                        <li key={fi} style={{ color: "#fde68a" }}>
                          <span style={{ color: "#f59e0b", fontWeight: 600 }}>建议：</span>
                          {f}
                        </li>
                      ))}
                    </ul>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {/* 5. 兜底：没有任何 blocker/warning 且未就绪，给下一步建议 */}
        {statusColor !== "ready" && blockers.length === 0 && warnings.length === 0 && (
          <div style={{ color: "#94a3b8", marginTop: 4 }}>
            若状态异常：点「就绪检查」旁的「刷新状态」，或先跑一次 Dry Run 检查 readiness 接口明细。
          </div>
        )}
      </div>
    );
  }, [autoTradeEnabled, autoTradeReadiness, statusColor, governanceBlocked, portfolioStatusRaw]);

  return (
    <header
      className="pt-portfolio-header"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "16px 24px",
        background: "var(--pt-surface-1)",
        borderBottom: "1px solid var(--pt-border)",
      }}
    >
      {/* 左侧：组合选择器 + 运行状态徽章 + 自动接管 pill */}
      <div className="pt-portfolio-header-primary" style={{ display: "flex", alignItems: "center", gap: 12 }}>
        {/* 组合选择器按钮 */}
        <div style={{ position: "relative" }}>
          <button
            type="button"
            className="pt-portfolio-selector"
            onClick={onOpenSelector}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              height: 36,
              padding: "0 12px",
              background: "var(--pt-surface-2)",
              border: "1px solid var(--pt-border)",
              borderRadius: "var(--pt-radius-md)",
              color: "var(--pt-foreground)",
              cursor: "pointer",
              transition: "background 0.15s ease, border-color 0.15s ease",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "var(--pt-surface-3)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "var(--pt-surface-2)";
            }}
          >
            <Briefcase size={16} style={{ color: "var(--pt-muted-foreground)" }} />
            <span className="pt-portfolio-name" style={{ fontSize: 15, fontWeight: 600, color: "var(--pt-white)" }}>{portfolioName}</span>
            {/* 实盘/模拟标签 */}
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                fontSize: 11,
                fontWeight: 500,
                padding: "2px 6px",
                borderRadius: "var(--pt-radius-sm)",
                background: "var(--pt-state-success-dim)",
                color: "var(--pt-state-success)",
              }}
            >
              {accountTypeLabel}
            </span>
            <span
              title="组合标的范围：候选、成员、持仓、回测与买入交易均受此限制"
              style={{
                display: "inline-flex",
                alignItems: "center",
                fontSize: 11,
                fontWeight: 500,
                padding: "2px 6px",
                borderRadius: "var(--pt-radius-sm)",
                background: "rgba(59, 130, 246, 0.12)",
                color: "var(--pt-primary)",
              }}
            >
              {assetScopeLabel}
            </span>
            {/* FR-P0-10/P1-8a 9 状态徽章：与 GovernanceTab/SelectorDropdown 色板一致 */}
            {(() => {
              const meta = portfolioStatusRaw
                ? PORTFOLIO_STATUS_STYLES[portfolioStatusRaw] ?? PORTFOLIO_STATUS_FALLBACK
                : PORTFOLIO_STATUS_FALLBACK;
              const isEmphasis =
                portfolioStatusRaw === "ADMIN_PAUSED" || portfolioStatusRaw === "RECONCILIATION_BLOCKED";
              return (
                <Tooltip title={`HG1 组合状态 · ${meta.title}\n${meta.hint}`}>
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      fontSize: 11,
                      fontWeight: 500,
                      padding: "2px 10px",
                      borderRadius: "var(--pt-radius-sm)",
                      border: `1px solid ${meta.border}`,
                      background: meta.bg,
                      color: meta.fg,
                      boxShadow: isEmphasis ? "0 0 0 1px rgba(220,38,38,0.08)" : undefined,
                    }}
                  >
                    {statusLoading ? (
                      <Activity size={10} style={{ opacity: 0.7 }} />
                    ) : portfolioStatusRaw === "READY" ? (
                      <CheckCircle2 size={10} />
                    ) : portfolioStatusRaw === "ADMIN_PAUSED" ? (
                      <Lock size={10} />
                    ) : portfolioStatusRaw === "DATA_INCOMPLETE_PAUSED" || portfolioStatusRaw === "SCORE_STALE" ? (
                      <Database size={10} />
                    ) : portfolioStatusRaw === "RUNNING_AUTO_SIMULATION" || portfolioStatusRaw === "RUNNING_BACKTEST" ? (
                      <Activity size={10} />
                    ) : portfolioStatusRaw === "RECONCILIATION_BLOCKED" ? (
                      <AlertTriangle size={10} />
                    ) : portfolioStatusRaw === "READY" ? (
                      <ShieldCheck size={10} />
                    ) : (
                      <AlertTriangle size={10} />
                    )}
                    {meta.title}
                  </span>
                </Tooltip>
              );
            })()}
            <ChevronDown
              size={16}
              style={{
                color: selectorOpen ? "var(--pt-primary)" : "var(--pt-muted-foreground)",
                transition: "color 0.15s ease",
              }}
            />
          </button>
          {/* 组合选择下拉面板：在 relative 容器内渲染，position:absolute 浮在按钮正下方 */}
          {selectorDropdown}
        </div>

        {/* P0-AutoTrade: 运行状态徽章——按 readiness 语义变色，非仅 autoTradeEnabled */}
        <Tooltip title={pillHint}>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              borderRadius: "var(--pt-radius-full)",
              background: palette.bg,
              color: palette.fg,
              fontSize: 12,
              fontWeight: 500,
            }}
          >
            <span
              className={palette.pulseClassName}
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: palette.dot,
                flexShrink: 0,
              }}
            />
            {statusText}
          </span>
        </Tooltip>

        {/* P0-AutoTrade: 自动接管 pill——语义颜色 + 阻塞告警圆点 + 点击刷新 */}
        <Tooltip title={pillHint}>
          <button
            type="button"
            className="pt-autotrade-pill"
            onClick={onRefreshReadiness}
            title={t("autoTradeRefreshReadiness")}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 12px",
              borderRadius: "var(--pt-radius-full)",
              border: `1px solid ${palette.bg}`,
              background: palette.bg,
              color: palette.fg,
              fontSize: 12,
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            <Zap size={14} className={statusColor === "ready" ? "pt-bolt-pulse" : ""} />
            {pillText}
            {statusColor === "notReady" && (
              <AlertCircle size={12} style={{ opacity: 0.9 }} />
            )}
          </button>
        </Tooltip>
      </div>

      {/* 右侧：组合排名 + 添加候选标的 */}
      <div className="pt-portfolio-header-actions" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <button
          type="button"
          className="pt-btn pt-btn-secondary"
          onClick={onOpenRanking}
        >
          <Trophy size={16} style={{ color: "var(--pt-state-warning)" }} />
          <span>{t("portfolioTrading.header.ranking")}</span>
        </button>
        {/* FR-P1-8a：添加候选标的入口（NEW_BUY 入口），allow_new_buys=false 时禁用并给出明确原因 */}
        {(() => {
          const meta = portfolioStatusRaw
            ? PORTFOLIO_STATUS_STYLES[portfolioStatusRaw] ?? PORTFOLIO_STATUS_FALLBACK
            : PORTFOLIO_STATUS_FALLBACK;
          const reason = allowNewBuys
            ? undefined
            : `当前组合 HG1 状态「${meta.title}」禁止 NEW_BUY（新增候选/买单）。\n${meta.hint}`;
          const btn = (
            <button
              type="button"
              className="pt-btn pt-btn-primary"
              onClick={onOpenAddCandidate}
              disabled={!allowNewBuys}
              style={{ opacity: !allowNewBuys ? 0.55 : 1, cursor: !allowNewBuys ? "not-allowed" : undefined }}
            >
              <Plus size={16} />
              <span>{t("portfolioTrading.header.addCandidate")}</span>
            </button>
          );
          return !allowNewBuys ? <Tooltip title={reason ?? ""} placement="bottomRight">{btn}</Tooltip> : btn;
        })()}
      </div>
    </header>
  );
};

/* ------------------------------------------------------------------ */
/* 4 子 Tab nav                                                        */
/* ------------------------------------------------------------------ */

interface SubTabNavProps {
  active: SubTabKey;
  onChange: (tab: SubTabKey) => void;
}

const SubTabNav: React.FC<SubTabNavProps> = ({ active, onChange }) => {
  return (
    <nav
      className="pt-portfolio-subtabs"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 24,
        height: 44,
        padding: "0 24px",
        background: "var(--pt-surface-1)",
        borderBottom: "1px solid var(--pt-border)",
      }}
    >
      {SUB_TABS.map((tab) => (
        <button
          key={tab.key}
          type="button"
          className={`pt-tab${active === tab.key ? " active" : ""}`}
          onClick={() => onChange(tab.key)}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
};

/* ------------------------------------------------------------------ */
/* 子 Tab 内容路由                                                     */
/* ------------------------------------------------------------------ */

interface SubTabContentProps {
  active: SubTabKey;
  portfolioId: number;
  onNavigate: (tab: string) => void;
  autoTradeEnabled: boolean;
  membersRevision: number;
  portfolioStatus: PortfolioStatusResponse | null;
  perm: PortfolioStatePermissions;
}

const SubTabContent: React.FC<SubTabContentProps> = ({ active, portfolioId, onNavigate, autoTradeEnabled, membersRevision, portfolioStatus, perm }) => {
  switch (active) {
    case "overview":
      return <PortfolioOverview portfolioId={portfolioId} onNavigate={onNavigate} portfolioStatus={portfolioStatus} perm={perm} />;
    case "members":
      return <PortfolioMembersTable key={`${portfolioId}-${membersRevision}`} portfolioId={portfolioId} onNavigate={onNavigate} portfolioStatus={portfolioStatus} perm={perm} />;
    case "strategy":
      return <PortfolioStrategyRules portfolioId={portfolioId} onNavigate={onNavigate} portfolioStatus={portfolioStatus} perm={perm} />;
    case "backtest":
      return (
        <PortfolioBacktestCenter
          portfolioId={portfolioId}
          onNavigate={onNavigate}
          autoTradeEnabled={autoTradeEnabled}
        />
      );
    case "governance":
      return <PortfolioGovernanceTab portfolioId={portfolioId} onNavigate={onNavigate} />;
    default:
      return <PortfolioOverview portfolioId={portfolioId} onNavigate={onNavigate} portfolioStatus={portfolioStatus} perm={perm} />;
  }
};

export default PortfolioTradingShell;
