// WP2.7：ObservationPool 组件测试（WP2.4 正式观察池页面）
//
// 覆盖：
// - 列表渲染：标的/来源/加入时间/优先级/状态
// - 筛选 status / origin_type：触发带参数的 fetch
// - 空状态：显示"观察池暂无项目"
// - 错误状态：显示错误信息 + 重试按钮
// - 加载状态：显示加载动画
// - 点击"查看详情"打开弹窗：显示来源/原因/评分快照/当前评分
// - 批量选择 + 批量归档：调用 archive API
// - OpportunityStatusBadges 在每行渲染
// - 降级标记：degraded=true 在详情弹窗中显示
//
// 约束：
// - 不修改已稳定组件实现
// - mock fetch（requestJson），不调用真实后端
// - i18n mock：t(key) 返回 key，便于按 key 断言
// - OpportunityStatusBadges 通过 mock 占位以隔离依赖
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockRequestJson } = vi.hoisted(() => ({
  mockContext: {
    workbench: null as any,
    activeWatchlistId: 1 as number | null,
  },
  mockRequestJson: vi.fn(),
}));

// Mock i18n：t 返回 key，template 返回拼接后的字符串
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

// Mock antd：保留组件库，仅覆盖 message
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

// Mock AppContext：提供 workbench（含 watchlists）与 activeWatchlistId
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client：仅暴露 requestJson（ObservationPool 唯一调用入口）
vi.mock("../../api/client", () => ({
  requestJson: mockRequestJson,
  api: {},
}));

// Mock OpportunityStatusBadges：渲染可识别占位，隔离 useSymbolRelationships 依赖
vi.mock("../opportunity/OpportunityStatusBadges", () => ({
  __esModule: true,
  default: ({ symbolId }: { symbolId: number }) => (
    <div
      data-testid="opportunity-status-badges"
      data-symbol-id={symbolId}
      className="opportunity-status-badges"
    />
  ),
  OpportunityStatusBadges: ({ symbolId }: { symbolId: number }) => (
    <div
      data-testid="opportunity-status-badges"
      data-symbol-id={symbolId}
      className="opportunity-status-badges"
    />
  ),
}));

import ObservationPool from "../opportunity/ObservationPool";

/** 构造一份完整的 ObservationItem，支持部分覆盖 */
function makeItem(overrides: Partial<Record<string, unknown>> = {}): any {
  return {
    watchlist_item_id: 1,
    watchlist_id: 1,
    watchlist_name: "core",
    symbol_id: 100,
    symbol: "600000",
    added_at: "2026-07-01T10:00:00Z",
    updated_at: null,
    archived_at: null,
    origin_type: "manual",
    origin_id: null,
    reason: { note: "test reason" },
    score_snapshot: { total: 80 },
    status: "watching",
    priority: 50,
    tags: [],
    note: null,
    target_portfolio_id: null,
    target_portfolio_name: null,
    latest_price: 10.5,
    latest_price_date: "2026-07-15",
    price_change_pct: 1.2,
    latest_total_score: 80.5,
    latest_quality_score: 80,
    latest_timing_score: 81,
    latest_score_date: "2026-07-15",
    data_credibility: "high",
    bar_count: 250,
    has_position: false,
    position_portfolio_name: null,
    degraded: false,
    degraded_reason: null,
    ...overrides,
  };
}

describe("ObservationPool 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 默认提供一个 list_type="watch" 的主观察池
    mockContext.workbench = {
      watchlists: [{ id: 1, name: "core", list_type: "watch" }],
    };
    mockContext.activeWatchlistId = 1;
  });

  // 1. 列表渲染：表格显示标的/来源/加入时间/优先级/状态
  it("应渲染观察池表格，包含标的/来源/加入时间/优先级/状态", async () => {
    mockRequestJson.mockResolvedValue([makeItem()]);
    render(<ObservationPool />);
    // 等待数据加载
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 表头应包含这些列（i18n key）。
    // 注意：antd Table 在 scroll.x 模式下可能渲染重复的表头用于列宽计算，
    // 因此使用 getAllByText 断言至少出现一次。
    expect(screen.getAllByText("observationPoolColumnSymbol").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("observationPoolColumnOrigin").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("observationPoolColumnAddedAt").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("observationPoolColumnPriority").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("observationPoolColumnStatus").length).toBeGreaterThanOrEqual(1);
    // 来源应为 manual tag
    expect(screen.getByText("manual")).toBeInTheDocument();
    // 状态应为 watching tag
    expect(screen.getByText("watching")).toBeInTheDocument();
    // 加入时间应显示
    expect(screen.getByText("2026-07-01T10:00:00Z")).toBeInTheDocument();
    // fetch 被调用且包含 watchlist id
    expect(mockRequestJson).toHaveBeenCalled();
    const firstCallUrl = String(mockRequestJson.mock.calls[0][0]);
    expect(firstCallUrl).toContain("/api/v1/watchlists/1/observations");
    expect(firstCallUrl).toContain("limit=200");
  });

  // 2. 筛选 status=archived
  it("选择 status=archived 应触发带 status 参数的 fetch", async () => {
    // 第一次返回初始列表；第二次（筛选后）返回 archived 列表
    mockRequestJson.mockResolvedValue([makeItem()]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 清空调用记录，便于断言下一次 fetch
    mockRequestJson.mockClear();
    mockRequestJson.mockResolvedValue([makeItem({ status: "archived", symbol: "600001" })]);

    // antd Select 通过 placeholder span 渲染占位文本，需通过 class 定位 selector 后 mouseDown 打开下拉
    // 找到 status 筛选 Select：通过其内部的 placeholder span 文本定位
    const statusPlaceholders = document.querySelectorAll(".ant-select-selection-placeholder");
    const statusPlaceholder = Array.from(statusPlaceholders).find(
      (el) => el.textContent === "observationPoolFilterStatus",
    ) as HTMLElement;
    expect(statusPlaceholder).toBeTruthy();
    // 找到对应的 selector（向上查找 .ant-select-selector）
    const statusSelector = statusPlaceholder.closest(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(statusSelector);
    // 选项应出现
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    // 点击 archived 选项
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const archivedOption = opts.find((o) => o.textContent === "archived") as HTMLElement;
    expect(archivedOption).toBeTruthy();
    fireEvent.click(archivedOption);

    // 断言 fetch 被调用且带 status=archived 参数
    await waitFor(() => {
      expect(mockRequestJson).toHaveBeenCalled();
      const url = String(mockRequestJson.mock.calls[0][0]);
      expect(url).toContain("status=archived");
    });
  });

  // 3. 筛选 origin_type=candidate
  it("选择 origin_type=candidate 应触发带 origin_type 参数的 fetch", async () => {
    mockRequestJson.mockResolvedValue([makeItem()]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    mockRequestJson.mockClear();
    mockRequestJson.mockResolvedValue([makeItem({ origin_type: "candidate", symbol: "600001" })]);

    // 定位 origin_type 筛选 Select
    const placeholders = document.querySelectorAll(".ant-select-selection-placeholder");
    const originPlaceholder = Array.from(placeholders).find(
      (el) => el.textContent === "observationPoolFilterOrigin",
    ) as HTMLElement;
    expect(originPlaceholder).toBeTruthy();
    const originSelector = originPlaceholder.closest(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(originSelector);
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const candidateOption = opts.find((o) => o.textContent === "candidate") as HTMLElement;
    expect(candidateOption).toBeTruthy();
    fireEvent.click(candidateOption);

    await waitFor(() => {
      expect(mockRequestJson).toHaveBeenCalled();
      const url = String(mockRequestJson.mock.calls[0][0]);
      expect(url).toContain("origin_type=candidate");
    });
  });

  // 4. 空状态
  it("接口返回空数组时应显示 observationPoolEmpty 空状态", async () => {
    mockRequestJson.mockResolvedValue([]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("observationPoolEmpty")).toBeInTheDocument();
    });
    // 同时显示空状态提示
    expect(screen.getByText("observationPoolEmptyHint")).toBeInTheDocument();
  });

  // 5. 错误状态：显示错误信息 + 重试按钮
  it("接口 reject 时应显示错误信息和重试按钮", async () => {
    mockRequestJson.mockRejectedValue(new Error("network error"));
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("observationPoolError")).toBeInTheDocument();
    });
    // 重试按钮：可能多处出现（Alert action 与顶部 toolbar）
    // 使用 getAllByText 断言至少一个重试按钮存在
    expect(screen.getAllByText("observationPoolRetry").length).toBeGreaterThanOrEqual(1);
  });

  // 6. 加载状态：fetch 延迟时应显示加载动画
  it("加载中应显示 Spin 加载动画", async () => {
    // 让 fetch 永不 resolve，保持 loading 状态
    mockRequestJson.mockImplementation(() => new Promise(() => {}));
    render(<ObservationPool />);
    // 应显示 Spin 组件（ant-spin 类）
    await waitFor(() => {
      const spin = document.querySelector(".ant-spin");
      expect(spin).toBeTruthy();
    });
  });

  // 7. 点击"查看详情"打开弹窗：显示来源/原因/评分快照/当前评分
  it("点击查看详情应打开弹窗并显示来源/原因/评分快照/当前评分", async () => {
    const item = makeItem({
      reason: { note: "测试原因" },
      score_snapshot: { total: 85 },
      latest_total_score: 90.5,
      degraded_reason: null,
    });
    mockRequestJson.mockResolvedValue([item]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 点击"查看详情"按钮（observationPoolDetailView）
    // 通过文本找到按钮元素（closest("button")），确保点击的是 button 本身
    const viewBtnText = screen.getByText("observationPoolDetailView");
    const viewBtn = viewBtnText.closest("button") as HTMLElement;
    expect(viewBtn).toBeTruthy();
    fireEvent.click(viewBtn);
    // 等待弹窗 DOM 出现且子内容已渲染
    // 直接检查 .ant-modal-body 的 textContent，更可靠地捕获嵌套文本（避免 antd Modal portal 时机问题）
    let modalBody: HTMLElement | null = null;
    await waitFor(() => {
      modalBody = document.querySelector(".ant-modal-body") as HTMLElement | null;
      expect(modalBody).toBeTruthy();
      const text = modalBody?.textContent ?? "";
      // 弹窗应显示来源/原因/评分快照/当前评分四个标签
      expect(text).toContain("observationPoolDetailOrigin");
      expect(text).toContain("observationPoolDetailReason");
      expect(text).toContain("observationPoolDetailScoreSnapshot");
      expect(text).toContain("observationPoolDetailCurrentScore");
      // reason JSON 应可见（makeItem 中 note: "测试原因"）
      expect(text).toContain("测试原因");
      // 当前评分 90.5 应可见
      expect(text).toContain("90.5");
    }, { timeout: 5000 });
  });

  // 8. 批量选择 + 批量归档
  it("批量选择多行并点击批量归档应调用 archive API", async () => {
    const user = userEvent.setup();
    const item1 = makeItem({ watchlist_item_id: 1, symbol: "600000", symbol_id: 100 });
    const item2 = makeItem({ watchlist_item_id: 2, symbol: "600001", symbol_id: 101 });
    // 初始加载返回 2 项
    mockRequestJson.mockResolvedValueOnce([item1, item2]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // 全选 checkbox（表头选择所有行）
    const checkboxes = document.querySelectorAll('input[type="checkbox"]');
    expect(checkboxes.length).toBeGreaterThan(0);
    // 表头全选 checkbox 是第一个
    const selectAllCheckbox = checkboxes[0] as HTMLInputElement;
    await user.click(selectAllCheckbox);
    // 等待选中状态出现（批量操作 Alert 显示）
    await waitFor(() => {
      expect(screen.getByText(/observationPoolBatchSelected/)).toBeInTheDocument();
    });
    // 点击"批量归档"按钮（触发 Popconfirm）
    const batchArchiveBtn = screen.getByText("observationPoolBatchArchive");
    await user.click(batchArchiveBtn);
    // Popconfirm 出现，点击确认
    await waitFor(() => {
      // Popconfirm 的 OK 按钮
      const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
      expect(okBtn).toBeTruthy();
    });
    const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
    // 后续 archive 调用与 refresh 调用
    mockRequestJson.mockResolvedValueOnce(makeItem()) // archive item1
      .mockResolvedValueOnce(makeItem()) // archive item2
      .mockResolvedValueOnce([]); // refresh fetch
    await user.click(okBtn);
    // 断言 archive API 被调用（POST 方法）
    await waitFor(() => {
      const archiveCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) => typeof url === "string" && url.includes("/archive") && options?.method === "POST",
      );
      expect(archiveCalls.length).toBeGreaterThanOrEqual(1);
    });
  });

  // 9. OpportunityStatusBadges 在每行渲染
  it("每行应渲染 OpportunityStatusBadges 组件", async () => {
    const item1 = makeItem({ watchlist_item_id: 1, symbol: "600000", symbol_id: 100 });
    const item2 = makeItem({ watchlist_item_id: 2, symbol: "600001", symbol_id: 101 });
    mockRequestJson.mockResolvedValue([item1, item2]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // mock 的 OpportunityStatusBadges 应在每行渲染（2 个）
    const badges = screen.getAllByTestId("opportunity-status-badges");
    expect(badges.length).toBeGreaterThanOrEqual(2);
    // 验证 symbol_id 被传递
    const symbolIds = badges.map((b) => b.getAttribute("data-symbol-id"));
    expect(symbolIds).toContain("100");
    expect(symbolIds).toContain("101");
  });

  // 10. 降级显示：degraded=true 时在详情弹窗中显示降级标记
  it("degraded=true 的观察项在详情弹窗中应显示降级标记", async () => {
    const item = makeItem({
      degraded: true,
      degraded_reason: "数据缺失",
    });
    mockRequestJson.mockResolvedValue([item]);
    render(<ObservationPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 点击"查看详情"打开弹窗
    const viewBtnText = screen.getByText("observationPoolDetailView");
    const viewBtn = viewBtnText.closest("button") as HTMLElement;
    fireEvent.click(viewBtn);
    await waitFor(() => {
      // degraded_reason 应作为 Tag 显示在弹窗中
      expect(screen.getByText("数据缺失")).toBeInTheDocument();
    });
  });
});
