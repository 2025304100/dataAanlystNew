import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const { mockContext, mockRequestJson } = vi.hoisted(() => ({
  mockContext: {
    workbench: { watchlists: [{ id: 1, list_type: "watch" }] },
    activeWatchlistId: 1,
    setActiveTab: vi.fn(),
    runSync: vi.fn(),
  },
  mockRequestJson: vi.fn(),
}));

vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string) => key,
  enumLabel: (_prefix: string, value: string | null | undefined) => value ?? "-",
}));
vi.mock("../../../context/AppContext", () => ({ useApp: () => mockContext }));
vi.mock("../../../utils/sourceContext", () => ({ navigateToResearch: vi.fn() }));
vi.mock("../OpportunityStatusBadges", () => ({
  OpportunityStatusBadges: () => <div data-testid="status-badges" />,
}));

// 关键修复：用同步 mock 工厂直接定义 ApiError 类，不用 vi.importActual。
// 这样组件的 toErrorInfo 中的 `err instanceof ApiError` 与测试构造的 ApiError 是同一个类，
// 避免 instanceof 跨模块失效导致 errorInfo 不被设置。
vi.mock("../../../api/client", () => {
  class ApiError extends Error {
    status_code?: number;
    error_code?: string;
    user_message?: string;
    impact?: string;
    retryable?: boolean;
    next_actions?: Array<{
      label: string;
      action_type: string;
      target?: string | null;
      reason?: string | null;
    }>;
    correlation_id?: string;
    detail?: unknown;
    constructor(
      message: string,
      init: {
        status_code?: number;
        error_code?: string;
        user_message?: string;
        impact?: string;
        retryable?: boolean;
        next_actions?: Array<{
          label: string;
          action_type: string;
          target?: string | null;
          reason?: string | null;
        }>;
        correlation_id?: string;
        detail?: unknown;
      } = {},
    ) {
      super(message);
      this.name = "ApiError";
      this.status_code = init.status_code;
      this.error_code = init.error_code;
      this.user_message = init.user_message;
      this.impact = init.impact;
      this.retryable = init.retryable;
      this.next_actions = init.next_actions;
      this.correlation_id = init.correlation_id;
      this.detail = init.detail;
    }
  }
  return { ApiError, requestJson: mockRequestJson };
});

import { ApiError } from "../../../api/client";
import ObservationPool from "../ObservationPool";

// 构造一条最小可渲染的观察项（对齐 ObservationRead 全字段）
function _makeObservation(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    watchlist_item_id: 10,
    watchlist_id: 1,
    watchlist_name: "主观察池",
    symbol_id: 100,
    symbol: "600000",
    added_at: "2024-01-15T00:00:00",
    updated_at: null,
    archived_at: null,
    origin_type: "manual",
    origin_id: null,
    reason: null,
    score_snapshot: null,
    status: "watching",
    priority: 50,
    tags: ["科技"],
    note: "测试",
    target_portfolio_id: null,
    target_portfolio_name: null,
    latest_price: 12.34,
    latest_price_date: "2024-01-15",
    price_change_pct: 1.5,
    latest_total_score: 80.0,
    latest_quality_score: 75.0,
    latest_timing_score: 70.0,
    latest_score_date: "2024-01-15",
    data_credibility: "high",
    bar_count: 200,
    has_position: false,
    position_portfolio_name: null,
    degraded: false,
    degraded_reason: null,
    ...overrides,
  };
}

function _mockReject(err: ApiError) {
  mockRequestJson.mockImplementation(() => Promise.reject(err));
}
function _mockResolve<T>(data: T) {
  mockRequestJson.mockImplementation(() => Promise.resolve(data));
}

describe("ObservationPool", () => {
  beforeEach(() => {
    cleanup();
    document.body.innerHTML = "";
    mockRequestJson.mockClear();
    _mockResolve([]);
    mockContext.setActiveTab.mockClear();
    mockContext.runSync.mockClear();
  });

  afterEach(() => {
    cleanup();
    document.body.innerHTML = "";
  });

  it("加载中显示 Spin", async () => {
    mockRequestJson.mockImplementation(() => new Promise(() => {}));
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("opportunityObservationLoading")).toBeInTheDocument();
    });
  });

  it("成功加载后渲染表格与观察项标的", async () => {
    _mockResolve([_makeObservation()]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
  });

  it("空数据渲染 Empty 占位", async () => {
    _mockResolve([]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("observationPoolEmpty")).toBeInTheDocument();
    });
  });

  it("网络错误渲染统一错误协议 Alert（error_code=NETWORK_ERROR + 重试按钮）", async () => {
    // retryable=false 避免 Alert action prop 渲染重复的 retry 按钮
    _mockReject(
      new ApiError("网络连接失败", {
        status_code: 0,
        error_code: "NETWORK_ERROR",
        user_message: "observationPoolErrorNetworkMessage",
        impact: "observationPoolErrorNetworkImpact",
        retryable: false,
        next_actions: [
          { label: "observationPoolErrorActionRetry", action_type: "retry" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("NETWORK_ERROR")).toBeInTheDocument();
      expect(screen.getByText("observationPoolErrorNetworkMessage")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: /observationPoolErrorActionRetry/ }),
    ).toBeInTheDocument();
  });

  it("DATA_NOT_READY 错误额外渲染前往基础数据 + 运行增量同步按钮", async () => {
    _mockReject(
      new ApiError("数据未准备好", {
        status_code: 503,
        error_code: "DATA_NOT_READY",
        user_message: "observationPoolErrorDataNotReadyMessage",
        impact: "observationPoolErrorDataNotReadyImpact",
        retryable: false,
        next_actions: [
          { label: "observationPoolErrorActionRetry", action_type: "retry" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("DATA_NOT_READY")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: /observationPoolErrorActionGoMarketData/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /observationPoolErrorActionRunSync/ }),
    ).toBeInTheDocument();
  });

  it("点击重试按钮触发重新加载", async () => {
    const networkErr = new ApiError("网络连接失败", {
      status_code: 0,
      error_code: "NETWORK_ERROR",
      user_message: "observationPoolErrorNetworkMessage",
      impact: "",
      retryable: false,
      next_actions: [
        { label: "observationPoolErrorActionRetry", action_type: "retry" },
      ],
    });
    // 第一次调用拒绝，后续调用成功
    mockRequestJson.mockImplementation(() => {
      if (mockRequestJson.mock.calls.length > 1) {
        return Promise.resolve([_makeObservation()]);
      }
      return Promise.reject(networkErr);
    });

    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("NETWORK_ERROR")).toBeInTheDocument();
    });

    fireEvent.click(
      screen.getByRole("button", { name: /observationPoolErrorActionRetry/ }),
    );

    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
  });

  it("点击前往基础数据跳转到 macro tab", async () => {
    _mockReject(
      new ApiError("数据未准备好", {
        status_code: 503,
        error_code: "DATA_NOT_READY",
        user_message: "observationPoolErrorDataNotReadyMessage",
        impact: "",
        retryable: false,
        next_actions: [
          { label: "observationPoolErrorActionRetry", action_type: "retry" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /observationPoolErrorActionGoMarketData/ }),
      ).toBeInTheDocument();
    });

    fireEvent.click(
      screen.getByRole("button", { name: /observationPoolErrorActionGoMarketData/ }),
    );
    expect(mockContext.setActiveTab).toHaveBeenCalledWith("macro");
  });

  it("点击运行增量同步调用 ctx.runSync", async () => {
    _mockReject(
      new ApiError("数据未准备好", {
        status_code: 503,
        error_code: "DATA_NOT_READY",
        user_message: "observationPoolErrorDataNotReadyMessage",
        impact: "",
        retryable: false,
        next_actions: [
          { label: "observationPoolErrorActionRetry", action_type: "retry" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /observationPoolErrorActionRunSync/ }),
      ).toBeInTheDocument();
    });

    fireEvent.click(
      screen.getByRole("button", { name: /observationPoolErrorActionRunSync/ }),
    );
    expect(mockContext.runSync).toHaveBeenCalled();
  });

  it("统一错误协议响应携带 dismiss next_action 时渲染关闭按钮", async () => {
    _mockReject(
      new ApiError("服务不可用", {
        status_code: 500,
        error_code: "INTERNAL_ERROR",
        user_message: "observationPoolErrorServerMessage",
        impact: "服务暂时不可用",
        retryable: false,
        next_actions: [
          { label: "observationPoolErrorActionDismiss", action_type: "dismiss" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("INTERNAL_ERROR")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /observationPoolErrorActionDismiss/ }),
      ).toBeInTheDocument();
    });
  });

  // UAT-PAGES.1 P1-02：NOT_FOUND 错误码（候选加入观察池时候选不存在）
  // 后端 from-candidate 路由捕获 ValueError → HTTPException(404) → 全局异常处理器包装为 NOT_FOUND
  it("NOT_FOUND 错误码渲染 dismiss 关闭按钮且不触发数据准备类按钮", async () => {
    _mockReject(
      new ApiError("请求的资源不存在", {
        status_code: 404,
        error_code: "NOT_FOUND",
        user_message: "observationPoolErrorNotFoundMessage",
        impact: "请检查输入或返回列表查看",
        retryable: false,
        next_actions: [
          {
            label: "observationPoolErrorActionDismiss",
            action_type: "dismiss",
            reason: "关闭错误提示，返回观察池列表",
          },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("NOT_FOUND")).toBeInTheDocument();
      expect(
        screen.getByText("observationPoolErrorNotFoundMessage"),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /observationPoolErrorActionDismiss/ }),
      ).toBeInTheDocument();
    });
    // NOT_FOUND 不应触发数据准备类兜底按钮
    expect(
      screen.queryByRole("button", { name: /observationPoolErrorActionGoMarketData/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /observationPoolErrorActionRunSync/ }),
    ).not.toBeInTheDocument();
  });

  // UAT-PAGES.1 P1-02：重复按钮避免 —— 后端 next_actions 含 redirect 时不重复渲染硬编码按钮
  it("DATA_NOT_READY 后端 next_actions 含 redirect 时不重复渲染前往基础数据按钮", async () => {
    _mockReject(
      new ApiError("数据未准备好", {
        status_code: 503,
        error_code: "DATA_NOT_READY",
        user_message: "observationPoolErrorDataNotReadyMessage",
        impact: "",
        retryable: false,
        next_actions: [
          { label: "后端提供的跳转", action_type: "redirect", target: "macro" },
          { label: "observationPoolErrorActionRetry", action_type: "retry" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("DATA_NOT_READY")).toBeInTheDocument();
    });
    // 后端提供的 redirect 按钮应存在
    expect(
      screen.getByRole("button", { name: /后端提供的跳转/ }),
    ).toBeInTheDocument();
    // 硬编码的"前往基础数据"按钮不应存在（避免重复）
    expect(
      screen.queryByRole("button", { name: /observationPoolErrorActionGoMarketData/ }),
    ).not.toBeInTheDocument();
  });

  // UAT-PAGES.1 P1-02：重复按钮避免 —— 后端 next_actions 含 sync 时不重复渲染硬编码按钮
  it("DATA_NOT_READY 后端 next_actions 含 sync 时不重复渲染运行增量同步按钮", async () => {
    _mockReject(
      new ApiError("数据未准备好", {
        status_code: 503,
        error_code: "DATA_NOT_READY",
        user_message: "observationPoolErrorDataNotReadyMessage",
        impact: "",
        retryable: false,
        next_actions: [
          { label: "后端同步入口", action_type: "sync" },
          { label: "observationPoolErrorActionRetry", action_type: "retry" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("DATA_NOT_READY")).toBeInTheDocument();
    });
    // 后端提供的 sync 按钮应存在
    expect(
      screen.getByRole("button", { name: /后端同步入口/ }),
    ).toBeInTheDocument();
    // 硬编码的"运行增量同步"按钮不应存在（避免重复）
    expect(
      screen.queryByRole("button", { name: /observationPoolErrorActionRunSync/ }),
    ).not.toBeInTheDocument();
  });

  // UAT-PAGES.1 P1-02：重复按钮避免 —— 后端同时提供 redirect+sync 时两个硬编码按钮都不渲染
  it("DATA_NOT_READY 后端 next_actions 含 redirect+sync 时不渲染任何硬编码按钮", async () => {
    _mockReject(
      new ApiError("数据未准备好", {
        status_code: 503,
        error_code: "DATA_NOT_READY",
        user_message: "observationPoolErrorDataNotReadyMessage",
        impact: "",
        retryable: false,
        next_actions: [
          { label: "前往基础数据(后端)", action_type: "redirect", target: "macro" },
          { label: "运行同步(后端)", action_type: "sync" },
          { label: "observationPoolErrorActionRetry", action_type: "retry" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("DATA_NOT_READY")).toBeInTheDocument();
    });
    // 两个硬编码兜底按钮都不应存在
    expect(
      screen.queryByRole("button", { name: /observationPoolErrorActionGoMarketData/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /observationPoolErrorActionRunSync/ }),
    ).not.toBeInTheDocument();
  });

  // UAT-PAGES.1 P1-02：STALE_DATA 错误码同样触发数据准备类兜底按钮
  it("STALE_DATA 错误码渲染前往基础数据 + 运行增量同步兜底按钮", async () => {
    _mockReject(
      new ApiError("数据过期", {
        status_code: 503,
        error_code: "STALE_DATA",
        user_message: "observationPoolErrorStaleDataMessage",
        impact: "",
        retryable: false,
        next_actions: [
          { label: "observationPoolErrorActionRetry", action_type: "retry" },
        ],
      }),
    );
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("STALE_DATA")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: /observationPoolErrorActionGoMarketData/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /observationPoolErrorActionRunSync/ }),
    ).toBeInTheDocument();
  });
});
