// WP5.3：来源上下文工具模块
//
// 设计说明：
// - 提供 SourceContext 的解析、规范化、持久化能力
// - 支持 URL query params（可分享链接）与 sessionStorage（应用内导航）双通道
// - 兼容旧路由 /investment-center?symbol=X（默认 source_type='legacy'）
// - 返回状态（筛选/分页/滚动位置）持久化：key 前缀 research_return_state_
//
// 关键约束：
// - 不破坏现有入口：所有字段缺省时回退到 'legacy' + 'research'
// - SSR 安全：所有 window/sessionStorage 访问均做 typeof window 校验
// - 不暴露敏感信息：错误静默回退到默认值
import type { SourceContext, SourceType } from "../components/symbol-research/types";

/** SourceContext 在 sessionStorage 中的 key */
export const SOURCE_CONTEXT_STORAGE_KEY = "research_source_context";

/** 返回状态在 sessionStorage 中的 key 前缀 */
export const RETURN_STATE_STORAGE_KEY_PREFIX = "research_return_state_";

/** 默认 source_type：未显式传入时回退到此值（向后兼容） */
export const DEFAULT_SOURCE_TYPE: SourceType = "legacy";

/** 默认 return_to：未显式传入时回退到此值 */
export const DEFAULT_RETURN_TO = "research";

/**
 * 合法的 source_type 值集合（含 'backtest' 与 'search' 历史值）。
 */
export const VALID_SOURCE_TYPES: ReadonlySet<string> = new Set([
  "candidate",
  "observation",
  "portfolio_member",
  "position",
  "alert",
  "backtest",
  "search",
  "manual",
  "legacy",
]);

/**
 * 合法的 return_to 值集合。
 */
export const VALID_RETURN_TO: ReadonlySet<string> = new Set([
  "candidate",
  "observation",
  "portfolio",
  "alert",
  "backtest",
  "research",
  "home",
]);

/**
 * return_to 到实际 activeTab 的映射。
 * - candidate / observation → opportunity（机会中心，候选/观察池均为子页签）
 * - portfolio → portfolio
 * - alert → decision（今日决策承载告警入口）
 * - backtest / research → investment
 * - home → decision
 */
export const RETURN_TO_TAB: Record<string, string> = {
  candidate: "opportunity",
  observation: "opportunity",
  portfolio: "portfolio",
  alert: "decision",
  backtest: "investment",
  research: "investment",
  home: "decision",
};

/**
 * 从 URL query string 解析 SourceContext。
 *
 * 支持格式：
 * - `?symbol_id=X&source_type=Y&source_id=Z&portfolio_id=W&return_to=Z`
 * - 旧路由兼容：`?symbol=X`（无 source_type 时默认 'legacy'）
 *
 * @param search URL 查询串，默认取 window.location.search
 * @returns 解析后的 SourceContext；若 URL 未携带相关参数返回 null
 */
export function parseSourceContextFromQuery(
  search?: string,
): SourceContext | null {
  if (typeof window === "undefined") return null;
  const queryStr = search ?? (window.location?.search ?? "");
  if (!queryStr) return null;
  const params = new URLSearchParams(queryStr);

  // 旧路由兼容：/investment-center?symbol=X
  const legacySymbol = params.get("symbol");
  const hasSymbolId = params.has("symbol_id");
  const hasSourceType = params.has("source_type");
  if (legacySymbol && !hasSymbolId && !hasSourceType) {
    return {
      source_type: "legacy",
      return_to: "research",
    };
  }

  if (!hasSymbolId && !hasSourceType) return null;

  const symbolIdRaw = params.get("symbol_id");
  const sourceTypeRaw = params.get("source_type");
  const sourceIdRaw = params.get("source_id");
  const portfolioIdRaw = params.get("portfolio_id");
  const returnToRaw = params.get("return_to");

  const symbol_id = symbolIdRaw != null && symbolIdRaw !== "" ? Number(symbolIdRaw) : undefined;
  const source_type: SourceType =
    sourceTypeRaw && VALID_SOURCE_TYPES.has(sourceTypeRaw)
      ? (sourceTypeRaw as SourceType)
      : DEFAULT_SOURCE_TYPE;
  const source_id =
    sourceIdRaw != null && sourceIdRaw !== "" && !Number.isNaN(Number(sourceIdRaw))
      ? Number(sourceIdRaw)
      : undefined;
  const portfolio_id =
    portfolioIdRaw != null && portfolioIdRaw !== "" && !Number.isNaN(Number(portfolioIdRaw))
      ? Number(portfolioIdRaw)
      : undefined;
  const return_to =
    returnToRaw && VALID_RETURN_TO.has(returnToRaw) ? returnToRaw : DEFAULT_RETURN_TO;

  return {
    symbol_id: symbol_id != null && !Number.isNaN(symbol_id) ? symbol_id : undefined,
    source_type,
    source_id,
    portfolio_id,
    return_to,
  };
}

/**
 * 规范化 SourceContext：补全默认值、校验枚举。
 * 传入 null/undefined 时返回默认 legacy 上下文。
 */
export function normalizeSourceContext(
  ctx: Partial<SourceContext> | null | undefined,
): SourceContext {
  if (!ctx) {
    return { source_type: DEFAULT_SOURCE_TYPE, return_to: DEFAULT_RETURN_TO };
  }
  const source_type: SourceType =
    ctx.source_type && VALID_SOURCE_TYPES.has(ctx.source_type)
      ? ctx.source_type
      : DEFAULT_SOURCE_TYPE;
  const return_to =
    ctx.return_to && VALID_RETURN_TO.has(ctx.return_to) ? ctx.return_to : DEFAULT_RETURN_TO;
  return {
    symbol_id: ctx.symbol_id,
    source_type,
    source_id: ctx.source_id,
    portfolio_id: ctx.portfolio_id,
    return_to,
  };
}

/**
 * 从 sessionStorage 读取 SourceContext（应用内导航通道）。
 * 自动规范化，永不在异常时抛错。
 */
export function readSourceContext(): SourceContext | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(SOURCE_CONTEXT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<SourceContext>;
    return normalizeSourceContext(parsed);
  } catch {
    return null;
  }
}

/**
 * 写入 SourceContext 到 sessionStorage。
 */
export function writeSourceContext(ctx: SourceContext): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.setItem(SOURCE_CONTEXT_STORAGE_KEY, JSON.stringify(ctx));
  } catch {
    /* ignore */
  }
}

/**
 * 清除 sessionStorage 中的 SourceContext（用于"返回后清理"场景）。
 */
export function clearSourceContext(): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.removeItem(SOURCE_CONTEXT_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * 综合解析 SourceContext：prop > URL > sessionStorage > 默认 legacy。
 *
 * 优先级说明：
 * 1. 若显式传入 prop（非 null/undefined），使用 prop（向后兼容测试）
 * 2. 否则尝试 URL query params（可分享链接）
 * 3. 否则尝试 sessionStorage（应用内导航）
 * 4. 都没有时返回默认 legacy 上下文
 */
export function resolveSourceContext(propCtx?: SourceContext | null): SourceContext {
  if (propCtx) return normalizeSourceContext(propCtx);
  const fromUrl = parseSourceContextFromQuery();
  if (fromUrl) return fromUrl;
  const fromStorage = readSourceContext();
  if (fromStorage) return fromStorage;
  return normalizeSourceContext(null);
}

/**
 * 保存返回状态（筛选/分页/滚动位置等任意可序列化数据）。
 *
 * key 格式：`research_return_state_{return_to}`
 */
export function saveReturnState(
  returnTo: string,
  state: Record<string, unknown>,
): void {
  if (typeof window === "undefined") return;
  try {
    const key = `${RETURN_STATE_STORAGE_KEY_PREFIX}${returnTo}`;
    sessionStorage.setItem(key, JSON.stringify(state));
  } catch {
    /* ignore */
  }
}

/**
 * 读取返回状态（不清理）。
 */
export function peekReturnState(
  returnTo: string,
): Record<string, unknown> | null {
  if (typeof window === "undefined") return null;
  try {
    const key = `${RETURN_STATE_STORAGE_KEY_PREFIX}${returnTo}`;
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/**
 * 读取并清除返回状态（一次性消费）。
 */
export function popReturnState(
  returnTo: string,
): Record<string, unknown> | null {
  if (typeof window === "undefined") return null;
  try {
    const key = `${RETURN_STATE_STORAGE_KEY_PREFIX}${returnTo}`;
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    sessionStorage.removeItem(key);
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/**
 * 根据 return_to 查找对应的 activeTab。
 */
export function tabForReturnTo(returnTo: string): string {
  return RETURN_TO_TAB[returnTo] ?? "decision";
}

/**
 * 构造 SourceContext（便捷工厂）。
 * 用于入口组件显式传递完整参数。
 */
export function buildSourceContext(params: {
  symbol_id?: number;
  source_type: SourceType;
  source_id?: number | null;
  portfolio_id?: number | null;
  return_to: string;
}): SourceContext {
  return normalizeSourceContext({
    symbol_id: params.symbol_id,
    source_type: params.source_type,
    source_id: params.source_id ?? undefined,
    portfolio_id: params.portfolio_id ?? undefined,
    return_to: params.return_to,
  });
}

/**
 * AppContext 的最小接口契约（避免直接 import AppContext 造成循环依赖）。
 * 只声明 navigateToResearch 需要的字段。
 */
export interface NavigationContext {
  setActiveTab: (tab: string) => void;
  loadSymbolDetail: (
    symbolId: number,
    options?: { force?: boolean; focus?: boolean; barLimit?: number },
  ) => Promise<unknown>;
  activeTab?: string;
}

/**
 * 跳转到标的研究页面。
 *
 * 步骤：
 * 1. 保存当前页面滚动位置到 sessionStorage（按 return_to 分桶）
 * 2. 写入 SourceContext 到 sessionStorage
 * 3. 调用 ctx.loadSymbolDetail + ctx.setActiveTab("investment")
 *
 * @param ctx AppContext（或最小化 mock）
 * @param sourceContext 来源上下文（含 symbol_id / source_type / return_to 等）
 * @param options.returnState 额外要保存的返回状态（如筛选条件/分页）
 */
export function navigateToResearch(
  ctx: NavigationContext,
  sourceContext: SourceContext,
  options?: {
    returnState?: Record<string, unknown>;
  },
): void {
  // 1. 保存返回状态（滚动位置 + 调用方附加状态）
  const returnTo = sourceContext.return_to ?? DEFAULT_RETURN_TO;
  const scrollY =
    typeof window !== "undefined" && typeof window.scrollY === "number"
      ? window.scrollY
      : 0;
  saveReturnState(returnTo, {
    scrollY,
    ...(options?.returnState ?? {}),
  });

  // 2. 写入 SourceContext（供 Shell 读取）
  writeSourceContext(sourceContext);

  // 3. 加载标的并切换 tab
  if (sourceContext.symbol_id != null) {
    ctx.loadSymbolDetail(sourceContext.symbol_id, { focus: true, barLimit: 500 });
  }
  if (ctx.activeTab !== "investment") {
    ctx.setActiveTab("investment");
  }
}
