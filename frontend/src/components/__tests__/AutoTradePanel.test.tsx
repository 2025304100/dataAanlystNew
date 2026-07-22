// WP6.6：AutoTradePanel 组件测试
//
// 覆盖：
// - 渲染成员级执行状态列表（mock 3 个成员）
// - 渲染 dry-run 差异表格（mock 2 条差异：member_missing + data_expired）
// - 双跑开关显示 env_flag 状态
// - 点击"回退旧来源"按钮调用对应 API
//
// 约束：
// - 不修改已稳定组件实现
// - mock api/client（api + requestJson），不调用真实后端
// - i18n mock：t(key) 返回 key，便于按 key 断言
// - 组合切换时 useEffect 会触发 3 个新 API 调用 + setResult(null) + setError(null)
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi, mockRequestJson, modalConfirmCalls } = vi.hoisted(() => ({
  mockContext: {
    portfolioId: 1 as number | null,
    portfolios: [
      {
        id: 1,
        account_type: "simulated",
        auto_trade_enabled: 1,
        auto_trade_last_run_at: null,
      },
    ],
    showToast: vi.fn(),
    loadWorkbench: vi.fn().mockResolvedValue(undefined),
  },
  mockApi: {
    executeAutoTrade: vi.fn(),
    getAutoTradeMemberStatus: vi.fn(),
    getAutoTradeDryRunDiff: vi.fn(),
    getAutoTradeMemberSourceStatus: vi.fn(),
    rollbackAutoTradeToOldSource: vi.fn(),
  },
  mockRequestJson: vi.fn(),
  // 收集 Modal.confirm 调用配置，便于测试断言与触发 onOk
  modalConfirmCalls: [] as Array<{
    title?: string;
    content?: string;
    okText?: string;
    cancelText?: string;
    onOk?: () => Promise<void> | void;
    onCancel?: () => void;
  }>,
}));

// Mock i18n：t 返回 key
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

// Mock antd：保留组件库，覆盖 message 与 Modal.confirm 静态方法
// Modal.confirm 在 jsdom 中通过 Portal 渲染易受 getComputedStyle 限制影响，
// 这里截获调用配置便于测试直接触发 onOk，避免依赖真实 Portal 异步渲染
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: {
      ...actual.message,
      success: vi.fn(),
      error: vi.fn(),
      warning: vi.fn(),
      info: vi.fn(),
    },
    Modal: {
      ...actual.Modal,
      confirm: (config: any) => {
        modalConfirmCalls.push(config);
        // 返回一个空对象以兼容 antd 类型签名
        return { destroy: () => {}, update: () => {} };
      },
    },
  };
});

// Mock AppContext
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client
vi.mock("../../api/client", () => ({
  requestJson: mockRequestJson,
  api: mockApi,
}));

import AutoTradePanel from "../AutoTradePanel";

/** 构造一个成员状态项 */
function makeMember(overrides: Partial<any> = {}): any {
  return {
    member_id: 1,
    symbol_id: 100,
    symbol: "600000",
    status: "active",
    execution_mode: "auto",
    source_type: "manual",
    manual_lock: false,
    has_position: false,
    position_quantity: 0,
    latest_order: null,
    data_health: {
      healthy: true,
      reason: "",
      kline_latest_at: null,
      score_latest_at: null,
      rule_version_id: null,
    },
    risk_blocked: false,
    data_expired: false,
    ...overrides,
  };
}

/** 构造一个差异项 */
function makeDiff(overrides: Partial<any> = {}): any {
  return {
    symbol_id: 100,
    side: "buy",
    old_action: "open",
    new_action: null,
    reason: "member_missing",
    detail: "新逻辑需 PortfolioMember，但该标的无成员",
    ...overrides,
  };
}

describe("AutoTradePanel WP6.6 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.portfolioId = 1;
    // 清空 Modal.confirm 调用记录
    modalConfirmCalls.length = 0;
    // 默认 mock：所有新 API 返回空结果
    mockApi.getAutoTradeMemberStatus.mockResolvedValue({ portfolio_id: 1, total: 0, members: [] });
    mockApi.getAutoTradeDryRunDiff.mockResolvedValue({ portfolio_id: 1, old_set: { buys: [], sells: [], rejected: [] }, new_set: { buys: [], sells: [], rejected: [] }, diffs: [] });
    mockApi.getAutoTradeMemberSourceStatus.mockResolvedValue({
      portfolio_id: 1,
      enabled: false,
      env_var_name: "AUTO_TRADE_MEMBER_SOURCE_ENABLED",
      env_flag: "false",
      whitelist_match: false,
      blacklist_match: false,
      whitelist: [],
      blacklist: [],
    });
    mockApi.rollbackAutoTradeToOldSource.mockResolvedValue({ ok: true, portfolio_id: 1, message: "ok" });
    mockApi.executeAutoTrade.mockResolvedValue({ portfolio_id: 1, dry_run: true, sells: [], buys: [], errors: [], executed_at: "2026-07-21T10:00:00Z" });
  });

  // Modal.confirm 通过 portal 渲染到 document.body，需要每个测试后清理
  afterEach(() => {
    document.body.innerHTML = "";
  });

  // 1. 渲染成员级执行状态列表（mock 3 个成员）
  it("应渲染成员级执行状态列表（3 个成员）", async () => {
    mockApi.getAutoTradeMemberStatus.mockResolvedValue({
      portfolio_id: 1,
      total: 3,
      members: [
        makeMember({ member_id: 1, symbol_id: 100, symbol: "600000", execution_mode: "auto", status: "active" }),
        makeMember({ member_id: 2, symbol_id: 200, symbol: "600001", execution_mode: "manual", status: "paused" }),
        makeMember({ member_id: 3, symbol_id: 300, symbol: "600002", execution_mode: "confirm", status: "active", has_position: true, position_quantity: 100 }),
      ],
    });
    render(<AutoTradePanel />);

    // 等待成员数据加载
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });

    // 三个成员都应该出现
    expect(screen.getByText("600000")).toBeInTheDocument();
    expect(screen.getByText("600001")).toBeInTheDocument();
    expect(screen.getByText("600002")).toBeInTheDocument();

    // 应调用 member-status API
    expect(mockApi.getAutoTradeMemberStatus).toHaveBeenCalledWith(1);
  });

  // 2. 渲染 dry-run 差异表格（mock 2 条差异：member_missing + data_expired）
  it("应渲染双跑差异表格，含 member_missing 与 data_expired 两条", async () => {
    mockApi.getAutoTradeDryRunDiff.mockResolvedValue({
      portfolio_id: 1,
      old_set: { buys: [], sells: [], rejected: [] },
      new_set: { buys: [], sells: [], rejected: [] },
      diffs: [
        makeDiff({ symbol_id: 100, side: "buy", old_action: "open", new_action: null, reason: "member_missing", detail: "新逻辑需 PortfolioMember，但该标的无成员" }),
        makeDiff({ symbol_id: 200, side: "sell", old_action: "exit", new_action: "hold", reason: "data_expired", detail: "新逻辑数据过期拒绝" }),
      ],
    });
    render(<AutoTradePanel />);

    // 等待差异表加载完成
    await waitFor(() => {
      expect(mockApi.getAutoTradeDryRunDiff).toHaveBeenCalledWith(1);
    });
    // 等待 symbol_id 列渲染
    await waitFor(() => {
      expect(screen.getByText("#100")).toBeInTheDocument();
    });

    // 两条 symbol_id 都应该出现
    expect(screen.getByText("#100")).toBeInTheDocument();
    expect(screen.getByText("#200")).toBeInTheDocument();
    // 差异原因本地化 key 应出现（通过 diffReasonLabel -> t("autoTradeMember.reasonCodes.*")）
    expect(screen.getAllByText("autoTradeMember.reasonCodes.memberMissing").length).toBeGreaterThan(0);
    expect(screen.getAllByText("autoTradeMember.reasonCodes.dataExpired").length).toBeGreaterThan(0);
    // 详细原因文本
    expect(screen.getByText("新逻辑需 PortfolioMember，但该标的无成员")).toBeInTheDocument();
    expect(screen.getByText("新逻辑数据过期拒绝")).toBeInTheDocument();
  });

  // 3. 双跑开关显示 env_flag 状态
  it("双跑开关应显示 env_flag 状态", async () => {
    mockApi.getAutoTradeMemberSourceStatus.mockResolvedValue({
      portfolio_id: 1,
      enabled: true,
      env_var_name: "AUTO_TRADE_MEMBER_SOURCE_ENABLED",
      env_flag: "true",
      whitelist_match: true,
      blacklist_match: false,
      whitelist: [1],
      blacklist: [],
    });
    render(<AutoTradePanel />);

    // 等待开关状态加载
    await waitFor(() => {
      expect(mockApi.getAutoTradeMemberSourceStatus).toHaveBeenCalledWith(1);
    });

    // env_flag 值 "true" 应作为 Statistic value 显示
    await waitFor(() => {
      expect(screen.getByText("true")).toBeInTheDocument();
    });

    // 开关分区标题应显示
    expect(screen.getByText("autoTradeMember.sourceSwitch")).toBeInTheDocument();
    // env_flag 标签应显示
    expect(screen.getByText("autoTradeMember.envFlag")).toBeInTheDocument();
    // 启用状态文案应显示
    expect(screen.getByText("autoTradeMember.sourceEnabledOn")).toBeInTheDocument();
  });

  // 4. 点击"回退旧来源"按钮调用对应 API
  it("点击回退旧来源按钮应弹出确认 Modal 并调用 rollback API", async () => {
    const user = userEvent.setup();
    // 默认 enabled=false、whitelist_match=false，回退按钮应被 disabled
    // 改为 enabled=true 让按钮可点击
    mockApi.getAutoTradeMemberSourceStatus.mockResolvedValue({
      portfolio_id: 1,
      enabled: true,
      env_var_name: "AUTO_TRADE_MEMBER_SOURCE_ENABLED",
      env_flag: "true",
      whitelist_match: true,
      blacklist_match: false,
      whitelist: [1],
      blacklist: [],
    });
    render(<AutoTradePanel />);

    // 等待开关状态加载：env_flag 值 "true" 出现表示开关状态已渲染
    await waitFor(() => {
      expect(screen.getByText("true")).toBeInTheDocument();
    });

    // antd Button 渲染为 <button><span>text</span></button>，
    // 通过 text 定位 span 后取最近 button 祖先
    const rollbackTextEl = screen.getByText("autoTradeMember.rollbackToOldSource");
    let rollbackButton: HTMLElement | null = rollbackTextEl.closest("button");
    if (!rollbackButton) {
      // 兜底：直接查找所有 button，按文本过滤
      const allButtons = Array.from(document.querySelectorAll("button"));
      rollbackButton = allButtons.find((b) =>
        (b.textContent || "").includes("autoTradeMember.rollbackToOldSource"),
      ) as HTMLElement | null;
    }
    expect(rollbackButton).toBeTruthy();
    await user.click(rollbackButton as HTMLElement);

    // Modal.confirm 应被调用一次，配置 title 为回退文案
    await waitFor(() => {
      expect(modalConfirmCalls.length).toBe(1);
    });
    const confirmConfig = modalConfirmCalls[0];
    expect(confirmConfig.title).toBe("autoTradeMember.rollbackToOldSource");
    expect(confirmConfig.content).toBe("autoTradeMember.rollbackConfirmDesc");
    expect(confirmConfig.okText).toBe("autoTradeMember.rollbackToOldSource");

    // 模拟用户点击 Modal 的 OK 按钮：直接触发 onOk 回调
    await act(async () => {
      const ret = confirmConfig.onOk && confirmConfig.onOk();
      if (ret && typeof (ret as Promise<void>).then === "function") {
        await ret;
      }
    });

    // 断言 rollback API 被调用
    await waitFor(() => {
      expect(mockApi.rollbackAutoTradeToOldSource).toHaveBeenCalledWith(1);
    });
  });
});
