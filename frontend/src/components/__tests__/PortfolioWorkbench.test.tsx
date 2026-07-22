import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
// globals: true 配置下 vi 作为全局变量在 hoisted 回调中可用
const { mockContext, mockApi } = vi.hoisted(() => ({
  mockContext: {
    workbench: null as any,
    newsSnapshot: null as any,
    candidateSearch: "",
    portfolioId: 1,
    activeSymbolId: null as number | null,
    setCandidateSearch: vi.fn((v: string) => {}),
    setActiveTab: vi.fn((tab: string) => {}),
    loadWorkbench: vi.fn(async () => {}),
    showToast: vi.fn((_type: string, _msg: string) => {}),
    loadSymbolDetail: vi.fn(async (_id: number) => {}),
  },
  mockApi: {
    getPositions: vi.fn(async () => []),
    getAllocation: vi.fn(async () => null),
    getSymbols: vi.fn(async () => []),
    createSymbol: vi.fn(async () => ({ id: 1 })),
    upsertPosition: vi.fn(async () => ({ ok: true })),
    deletePosition: vi.fn(async () => ({ ok: true })),
    upsertPortfolioRule: vi.fn(async () => ({ ok: true })),
    backupDatabase: vi.fn(async () => ({ backup_path: "/tmp/backup.db" })),
    listBackups: vi.fn(async () => []),
    restoreDatabase: vi.fn(async () => ({ ok: true })),
  },
}));

// Mock i18n: t 返回 key，template 返回拼接后的字符串
// 必须包含 getLocale / setLocale，因为 utils/format.ts 引用了 getLocale
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

// Mock antd message（保留组件库，仅覆盖 message）
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
  };
});

// Mock AppContext
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client
vi.mock("../../api/client", () => ({ api: mockApi }));

import PortfolioWorkbench from "../PortfolioWorkbench";

// 构造一份完整的 workbench 数据用于带数据场景
function makeWorkbench() {
  return {
    portfolio: {
      id: 1,
      name: "默认组合",
      currency: "CNY",
      total_capital: 100000,
      investable_ratio: 0.9,
      cash_reserve_ratio: 0.1,
    },
    active_rule: {
      id: 1,
      rule_name: "默认规则",
      max_single_position_pct: 0.3,
      max_sector_position_pct: 0.3,
      max_stock_position_pct: 0.6,
      max_etf_position_pct: 0.4,
      max_loss_per_trade_pct: 0.05,
      max_open_positions: 10,
    },
    market_scope: {
      selected_group: "all",
      available_groups: ["all"],
      total_symbols: 100,
      filtered_symbols: 80,
      region_counts: { cn: 80 },
    },
    overview: {
      symbols_count: 100,
      watchlists_count: 2,
      total_position_pct: 0.5,
      cash_pct: 0.5,
      top_candidates: [],
      risk_flags: [],
    },
    account_summary: {
      cash_balance: 50000,
      available_cash: 50000,
      market_value: 50000,
      total_equity: 100000,
      realized_pnl: 1000,
      unrealized_pnl: 500,
      cash_pct: 0.5,
      invested_pct: 0.5,
      position_count: 2,
      trade_count_7d: 3,
      last_trade_at: "2026-07-04T10:00:00Z",
    },
    latest_scan: {
      scan_run_id: 1,
      run_name: "扫描1",
      created_at: "2026-07-04T09:00:00Z",
      executable_count: 3,
      total_results: 5,
      auto_scan: false,
    },
    candidates: [
      {
        symbol_id: 101,
        symbol: "000001",
        name: "平安银行",
        region: "cn",
        asset_type: "stock",
        market: "szse",
        rank_no: 1,
        quality_score: 80,
        timing_score: 70,
        priority_score: 75,
        stage: "start",
        action: "open",
        recommended_position_pct: 0.1,
      },
    ],
    latest_scores: [],
    positions: [
      {
        symbol_id: 201,
        symbol: "600000",
        name: "浦发银行",
        quantity: 100,
        avg_cost: 10.5,
        latest_price: 11.0,
        market_value: 1100,
        position_pct: 0.011,
        unrealized_pnl: 50,
        unrealized_pnl_pct: 0.0476,
      },
    ],
    watchlists: [],
    journals: [],
    recent_trades: [],
  };
}

describe("PortfolioWorkbench 组件渲染测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 重置 mock context 到默认空状态
    mockContext.workbench = null;
    mockContext.newsSnapshot = null;
    mockContext.candidateSearch = "";
    mockContext.activeSymbolId = null;
    // 重置 api mock 默认返回值
    // 注意：getPositions 必须返回持仓数组，否则 useEffect 加载会清空表格
    mockApi.getPositions.mockImplementation(async () => [
      {
        symbol_id: 201,
        symbol: "600000",
        name: "浦发银行",
        quantity: 100,
        avg_cost: 10.5,
        latest_price: 11.0,
        market_value: 1100,
        position_pct: 0.011,
        unrealized_pnl: 50,
        unrealized_pnl_pct: 0.0476,
      },
    ]);
    mockApi.getAllocation.mockImplementation(async () => null);
    mockApi.listBackups.mockImplementation(async () => []);
  });

  it("should render empty state when workbench is null", () => {
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    expect(screen.getByText("noScanYet")).toBeInTheDocument();
  });

  it("should render today opportunities header when workbench is present", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("todayOpportunities")).toBeInTheDocument();
    });
    expect(screen.getByText("decisionPath")).toBeInTheDocument();
  });

  // WP9.4：原"今日机会/观察/消息"三列概览已移除（与机会中心重复），
  // 替换为指向机会中心的链接卡片。
  it("should render opportunity center link card instead of duplicate today columns", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId("goto-opportunity-center")).toBeInTheDocument();
    });
    // 链接卡片文案
    expect(screen.getByText("wp9.viewOpportunityCenter")).toBeInTheDocument();
    // 不再重复呈现机会/观察/消息三列
    expect(screen.queryByText("todayExecutable")).not.toBeInTheDocument();
    expect(screen.queryByText("todayWatch")).not.toBeInTheDocument();
    expect(screen.queryByText("todayMessages")).not.toBeInTheDocument();
  });

  it("should navigate to opportunity center when link card clicked", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByTestId("goto-opportunity-center")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("goto-opportunity-center"));
    expect(mockContext.setActiveTab).toHaveBeenCalledWith("opportunity");
  });

  it("should render account summary section with metrics", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("accountSummary")).toBeInTheDocument();
    });
    expect(screen.getByText("accountEquity")).toBeInTheDocument();
    expect(screen.getByText("accountCash")).toBeInTheDocument();
  });

  it("should render holdings section with position form and table", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getAllByText("holdings").length).toBeGreaterThan(0);
    });
    expect(screen.getByText("holdingsSection")).toBeInTheDocument();
    // 持仓录入表单：addPosition 同时出现在 form-kicker 和按钮文本中
    expect(screen.getAllByText("addPosition").length).toBeGreaterThan(0);
    expect(screen.getByText("symbolCode")).toBeInTheDocument();
    // 持仓表格中应显示已有持仓的代码
    expect(screen.getByText("600000")).toBeInTheDocument();
  });

  it("should render portfolio exposure and rule config section", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("portfolioExposure")).toBeInTheDocument();
    });
    // exposureKicker 同时出现在 panel-kicker 和 exposure-kicker 中
    expect(screen.getAllByText("exposureKicker").length).toBeGreaterThan(0);
    // 规则配置面板
    expect(screen.getByText("ruleConfigPanel")).toBeInTheDocument();
    expect(screen.getByText("totalCapital")).toBeInTheDocument();
    expect(screen.getByText("saveRule")).toBeInTheDocument();
  });

  it("should render data management section with backup/export/restore buttons", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("dataManagement")).toBeInTheDocument();
    });
    expect(screen.getByText("backupNow")).toBeInTheDocument();
    expect(screen.getByText("exportData")).toBeInTheDocument();
    expect(screen.getByText("restoreBackup")).toBeInTheDocument();
  });

  it("should call backupDatabase when backup button clicked", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("backupNow")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("backupNow"));
    await waitFor(() => {
      expect(mockApi.backupDatabase).toHaveBeenCalled();
    });
  });

  it("should render candidate list section with search input", async () => {
    mockContext.workbench = makeWorkbench();
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("candidateList")).toBeInTheDocument();
    });
    // candidates 同时出现在 metric-label 和 panel-kicker 中
    expect(screen.getAllByText("candidates").length).toBeGreaterThan(0);
    // 候选列表中 000001 同时出现在 todayExecutable 和候选表格中
    expect(screen.getAllByText("000001").length).toBeGreaterThan(0);
  });
});

describe("PortfolioWorkbench 交互测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.workbench = makeWorkbench();
    mockContext.newsSnapshot = null;
    mockContext.candidateSearch = "";
    mockContext.activeSymbolId = null;
    mockApi.getPositions.mockImplementation(async () => [
      {
        symbol_id: 201,
        symbol: "600000",
        name: "浦发银行",
        quantity: 100,
        avg_cost: 10.5,
        latest_price: 11.0,
        market_value: 1100,
        position_pct: 0.011,
        unrealized_pnl: 50,
        unrealized_pnl_pct: 0.0476,
      },
    ]);
    mockApi.getAllocation.mockImplementation(async () => null);
    mockApi.listBackups.mockImplementation(async () => []);
    mockApi.getSymbols.mockImplementation(async () => []);
    mockApi.createSymbol.mockImplementation(async () => ({ id: 1 }));
    mockApi.upsertPosition.mockImplementation(async () => ({ ok: true }));
    mockApi.deletePosition.mockImplementation(async () => ({ ok: true }));
    mockApi.upsertPortfolioRule.mockImplementation(async () => ({ ok: true }));
    mockApi.backupDatabase.mockImplementation(async () => ({ backup_path: "/tmp/backup.db" }));
  });

  it("should call upsertPosition and show toast after filling position form", async () => {
    const { container } = render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("holdingsSection")).toBeInTheDocument();
    });

    // Fill symbol code
    const symbolInput = screen.getByPlaceholderText("searchSymbolPlaceholder");
    fireEvent.change(symbolInput, { target: { value: "000001" } });

    // Fill quantity and avg cost InputNumbers
    const formInputs = container.querySelectorAll(".position-form .ant-input-number-input");
    expect(formInputs.length).toBeGreaterThanOrEqual(2);
    fireEvent.change(formInputs[0], { target: { value: "100" } });
    fireEvent.change(formInputs[1], { target: { value: "10.5" } });

    // Add button should be enabled now
    const addBtn = screen.getByRole("button", { name: "addPosition" });
    await waitFor(() => {
      expect(addBtn).not.toBeDisabled();
    });
    fireEvent.click(addBtn);

    await waitFor(() => {
      expect(mockApi.getSymbols).toHaveBeenCalledWith("000001");
    });
    await waitFor(() => {
      expect(mockApi.upsertPosition).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(mockContext.showToast).toHaveBeenCalledWith("success", "positionSaved");
    });
  });

  it("should call upsertPortfolioRule and show toast after clicking save rule", async () => {
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("ruleConfigPanel")).toBeInTheDocument();
    });

    const saveBtn = screen.getByRole("button", { name: "saveRule" });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(mockApi.upsertPortfolioRule).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(mockContext.loadWorkbench).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(mockContext.showToast).toHaveBeenCalledWith("success", "ruleSavedOk");
    });
  });

  it("should show loading state and success toast during backup", async () => {
    let resolveBackup!: (v: any) => void;
    mockApi.backupDatabase.mockImplementation(async () => {
      return new Promise((resolve) => { resolveBackup = resolve; });
    });
    render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("backupNow")).toBeInTheDocument();
    });

    const backupBtn = screen.getByRole("button", { name: "backupNow" });
    fireEvent.click(backupBtn);

    await waitFor(() => {
      expect(backupBtn.className).toContain("ant-btn-loading");
    });

    resolveBackup({ backup_path: "/tmp/backup.db" });

    await waitFor(() => {
      expect(backupBtn.className).not.toContain("ant-btn-loading");
    });
    await waitFor(() => {
      expect(mockContext.showToast).toHaveBeenCalledWith("success", "backupCreated");
    });
  });

  it("should call loadSymbolDetail when candidate table row is clicked", async () => {
    const { container } = render(<PortfolioWorkbench openMetricModal={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("candidateList")).toBeInTheDocument();
    });

    // Find candidate table row containing "000001" (not the position table which has "600000")
    const rows = container.querySelectorAll("tr.ant-table-row");
    const candidateRow = Array.from(rows).find((r) => r.textContent?.includes("000001"));
    expect(candidateRow).toBeDefined();
    fireEvent.click(candidateRow!);

    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(101, { focus: true });
    });
  });
});
