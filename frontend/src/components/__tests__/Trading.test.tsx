import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock context
const { mockContext } = vi.hoisted(() => ({
  mockContext: {
    workbench: null as any,
    detail: null as any,
    simQuantity: "",
    simPrice: "",
    activeSymbolId: null as number | null,
    // 子组件 PortfolioBacktestPanel / AutoTradePanel 调用 ctx.portfolios.find(...)
    portfolios: [] as any[],
    setSimQuantity: vi.fn((v: string) => {}),
    setSimPrice: vi.fn((v: string) => {}),
    submitSimOrder: vi.fn(async (_side: string) => {}),
    showToast: vi.fn((_type: string, _msg: string) => {}),
    loadSymbolDetail: vi.fn(async (_id: number) => {}),
    // 能力门禁字段：CapabilityGateButton 通过 useApp 读取这些方法
    capabilities: null as any,
    capabilitiesLoading: false,
    getCapability: vi.fn((_: string) => undefined),
    isCapabilityBlocked: vi.fn((_: string) => false),
    loadCapabilities: vi.fn(async () => {}),
  },
}));

// Mock i18n: t 返回 key，sideLabel 返回输入值
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  sideLabel: (v: string | null | undefined) => v ?? "-",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

// Mock antd message
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

import Trading from "../Trading";

describe("Trading 组件渲染测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 重置 mock context 到默认空状态
    mockContext.workbench = null;
    mockContext.detail = null;
    mockContext.simQuantity = "";
    mockContext.simPrice = "";
    mockContext.activeSymbolId = null;
  });

  it("should render empty state when workbench is null", () => {
    render(<Trading />);
    expect(screen.getByText("noScanYet")).toBeInTheDocument();
  });

  it("should render account summary when workbench is present", async () => {
    mockContext.workbench = {
      account_summary: {
        total_equity: 100000,
        available_cash: 50000,
        market_value: 50000,
        invested_pct: 0.5,
        realized_pnl: 1000,
        unrealized_pnl: 500,
        trade_count_7d: 3,
        last_trade_at: "2026-07-04T10:00:00Z",
      },
      positions: [],
      recent_trades: [],
    };
    render(<Trading />);
    await waitFor(() => {
      expect(screen.getByText("accountSummary")).toBeInTheDocument();
    });
    expect(screen.getByText("simAccount")).toBeInTheDocument();
  });

  it("should render order panel with title and current price", async () => {
    mockContext.workbench = {
      account_summary: {
        total_equity: 100000,
        available_cash: 50000,
        market_value: 50000,
        invested_pct: 0.5,
        realized_pnl: 0,
        unrealized_pnl: 0,
        trade_count_7d: 0,
        last_trade_at: null,
      },
      positions: [],
      recent_trades: [],
    };
    mockContext.detail = {
      symbol: { symbol: "000001", name: "平安银行" },
      position: null,
      bars: [{ close: 10.5 }],
    } as any;
    render(<Trading />);
    await waitFor(() => {
      const title = screen.getByText(/000001/);
      expect(title).toBeInTheDocument();
    });
    // 当前价格应该显示
    expect(screen.getByText("orderTitle")).toBeInTheDocument();
  });

  it("should render quick quantity buttons", async () => {
    mockContext.workbench = {
      account_summary: {
        total_equity: 100000,
        available_cash: 50000,
        market_value: 50000,
        invested_pct: 0.5,
        realized_pnl: 0,
        unrealized_pnl: 0,
        trade_count_7d: 0,
        last_trade_at: null,
      },
      positions: [],
      recent_trades: [],
    };
    render(<Trading />);
    await waitFor(() => {
      expect(screen.getByText("25%")).toBeInTheDocument();
    });
    expect(screen.getByText("33%")).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(screen.getByText("All")).toBeInTheDocument();
  });

  it("should render buy and sell buttons", async () => {
    mockContext.workbench = {
      account_summary: {
        total_equity: 100000,
        available_cash: 50000,
        market_value: 50000,
        invested_pct: 0.5,
        realized_pnl: 0,
        unrealized_pnl: 0,
        trade_count_7d: 0,
        last_trade_at: null,
      },
      positions: [],
      recent_trades: [],
    };
    render(<Trading />);
    await waitFor(() => {
      expect(screen.getByText("simBuy")).toBeInTheDocument();
    });
    expect(screen.getByText("simSell")).toBeInTheDocument();
  });

  it("should call submitSimOrder when buy button clicked", async () => {
    mockContext.workbench = {
      account_summary: {
        total_equity: 100000,
        available_cash: 50000,
        market_value: 50000,
        invested_pct: 0.5,
        realized_pnl: 0,
        unrealized_pnl: 0,
        trade_count_7d: 0,
        last_trade_at: null,
      },
      positions: [],
      recent_trades: [],
    };
    render(<Trading />);
    await waitFor(() => {
      expect(screen.getByText("simBuy")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("simBuy"));
    await waitFor(() => {
      expect(mockContext.submitSimOrder).toHaveBeenCalledWith("buy");
    });
  });

  it("should render holdings section with title", async () => {
    mockContext.workbench = {
      account_summary: {
        total_equity: 100000,
        available_cash: 50000,
        market_value: 50000,
        invested_pct: 0.5,
        realized_pnl: 0,
        unrealized_pnl: 0,
        trade_count_7d: 0,
        last_trade_at: null,
      },
      positions: [
        {
          symbol_id: 1,
          symbol: "000001",
          name: "平安银行",
          quantity: 100,
          avg_cost: 10.5,
          latest_price: 11.0,
          market_value: 1100,
          position_pct: 0.011,
          unrealized_pnl: 50,
          unrealized_pnl_pct: 0.0476,
        },
      ],
      recent_trades: [],
    };
    render(<Trading />);
    await waitFor(() => {
      expect(screen.getAllByText("holdings").length).toBeGreaterThan(0);
    });
    // 持仓表格中应显示股票代码
    expect(screen.getByText("000001")).toBeInTheDocument();
  });
});

describe("Trading 交互测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.workbench = {
      account_summary: {
        total_equity: 100000,
        available_cash: 50000,
        market_value: 50000,
        invested_pct: 0.5,
        realized_pnl: 0,
        unrealized_pnl: 0,
        trade_count_7d: 0,
        last_trade_at: null,
      },
      positions: [],
      recent_trades: [],
    };
    mockContext.detail = null;
    mockContext.simQuantity = "";
    mockContext.simPrice = "";
    mockContext.activeSymbolId = null;
  });

  it("should call submitSimOrder with sell when sell button clicked", async () => {
    const user = userEvent.setup();
    render(<Trading />);
    await waitFor(() => {
      expect(screen.getByText("simSell")).toBeInTheDocument();
    });
    await user.click(screen.getByText("simSell"));
    await waitFor(() => {
      expect(mockContext.submitSimOrder).toHaveBeenCalledWith("sell");
    });
  });

  it("should call setSimQuantity when quick qty button clicked", async () => {
    mockContext.detail = {
      symbol: { symbol: "000001", name: "平安银行", region: "cn" },
      position: null,
      bars: [{ close: 10.5 }],
    } as any;
    render(<Trading />);
    await waitFor(() => {
      expect(screen.getByText("25%")).toBeInTheDocument();
    });
    // qty = floor(50000 * 0.25 / 10.5 / 100) * 100 = floor(11.9) * 100 = 1100
    fireEvent.click(screen.getByText("25%"));
    await waitFor(() => {
      expect(mockContext.setSimQuantity).toHaveBeenCalledWith("1100");
    });
  });

  it("should call loadSymbolDetail when position row is clicked", async () => {
    mockContext.workbench = {
      account_summary: {
        total_equity: 100000,
        available_cash: 50000,
        market_value: 50000,
        invested_pct: 0.5,
        realized_pnl: 0,
        unrealized_pnl: 0,
        trade_count_7d: 0,
        last_trade_at: null,
      },
      positions: [
        {
          symbol_id: 1,
          symbol: "000001",
          name: "平安银行",
          quantity: 100,
          avg_cost: 10.5,
          latest_price: 11.0,
          market_value: 1100,
          position_pct: 0.011,
          unrealized_pnl: 50,
          unrealized_pnl_pct: 0.0476,
        },
      ],
      recent_trades: [],
    };
    const { container } = render(<Trading />);
    await waitFor(() => {
      expect(screen.getAllByText("holdings").length).toBeGreaterThan(0);
    });
    const rows = container.querySelectorAll("tr.ant-table-row");
    expect(rows.length).toBeGreaterThan(0);
    fireEvent.click(rows[0]);
    await waitFor(() => {
      expect(mockContext.loadSymbolDetail).toHaveBeenCalledWith(1, { focus: true });
    });
  });

  it("should show error toast when sell order fails", async () => {
    mockContext.submitSimOrder.mockImplementation(async () => {
      throw new Error("network error");
    });
    render(<Trading />);
    await waitFor(() => {
      expect(screen.getByText("simSell")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("simSell"));
    await waitFor(() => {
      expect(mockContext.showToast).toHaveBeenCalledWith("error", "orderFailed: network error");
    });
  });
});
