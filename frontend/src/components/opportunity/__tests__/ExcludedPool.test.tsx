// UAT-PAGES.2：ExcludedPool 组件测试（已排除候选池）
//
// 覆盖：
// - 加载：渲染表格，包含标的/名称/分数/排除时间/原因/操作来源
// - 筛选：输入标的代码触发带参数的 fetch
// - 详情：点击"查看详情"打开弹窗，显示候选与排除事件信息
// - 恢复：点击"恢复"打开弹窗，确认后调用 restore API
//
// 约束：
// - 不修改已稳定组件实现
// - mock api/client（listExcludedCandidates / restoreDiscoveryCandidate），不调用真实后端
// - mock AppContext 提供 workbench.watchlists
// - i18n mock：t(key) 返回 key，便于按 key 断言
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockApi } = vi.hoisted(() => ({
  mockContext: {
    workbench: {
      watchlists: [{ id: 1, name: "core", list_type: "watch" }],
    },
  },
  mockApi: {
    listExcludedCandidates: vi.fn(),
    restoreDiscoveryCandidate: vi.fn(),
  },
}));

// Mock i18n：t 返回 key，template 返回拼接后的字符串
vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
  stageLabel: (v: string | null | undefined) => v ?? "-",
  actionLabel: (v: string | null | undefined) => v ?? "-",
  enumLabel: (prefix: string, code: string | null | undefined, fallback = "-") => String(code ?? fallback),
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

// Mock AppContext：提供 workbench.watchlists（恢复到观察池需要）
vi.mock("../../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client：仅暴露 ExcludedPool 用到的两个方法
vi.mock("../../../api/client", () => ({
  api: mockApi,
}));

import ExcludedPool from "../ExcludedPool";

/** 构造一份完整的 ExcludedItem，支持部分覆盖 */
function makeItem(overrides: Partial<Record<string, unknown>> = {}): any {
  return {
    candidate_id: 10,
    symbol: "600000",
    name: "测试银行",
    asset_type: "stock",
    scan_run_id: 137,
    quality_score: 75.5,
    timing_score: 80.0,
    priority_score: 78.0,
    stage: "breakout",
    action: "buy",
    created_at: "2026-07-01T10:00:00Z",
    event_id: 501,
    excluded_at: "2026-07-15T12:00:00Z",
    actor_type: "user",
    exclude_reason: { reason: "数据不足" },
    from_status: "new",
    to_status: "excluded",
    idempotency_key: "exc-10-2026-07-15",
    ...overrides,
  };
}

describe("ExcludedPool 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.listExcludedCandidates.mockResolvedValue([makeItem()]);
    mockApi.restoreDiscoveryCandidate.mockResolvedValue({
      ok: true,
      candidate_id: 10,
      event_id: 602,
      restored_at: "2026-07-23T00:00:00Z",
      target: "candidate",
      observation_item_id: null,
      already_restored: false,
    });
  });

  // 1. 加载：表格渲染标的/名称/分数/排除时间/原因/操作来源
  it("应渲染已排除候选表格，包含标的/名称/分数/排除时间/原因/操作来源", async () => {
    mockApi.listExcludedCandidates.mockResolvedValue([makeItem()]);
    render(<ExcludedPool />);
    // 等待数据加载
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 名称应显示
    expect(screen.getByText("测试银行")).toBeInTheDocument();
    // 分数 Tag 应显示（Q75.5/T80.0/P78.0）
    expect(screen.getByText(/Q75\.5/)).toBeInTheDocument();
    expect(screen.getByText(/T80\.0/)).toBeInTheDocument();
    expect(screen.getByText(/P78\.0/)).toBeInTheDocument();
    // 排除时间应显示
    expect(screen.getByText("2026-07-15T12:00:00Z")).toBeInTheDocument();
    // 操作来源应为 user tag
    expect(screen.getByText("user")).toBeInTheDocument();
    // 扫描记录 id 应显示
    expect(screen.getByText("#137")).toBeInTheDocument();
    // listExcludedCandidates 被调用
    expect(mockApi.listExcludedCandidates).toHaveBeenCalled();
    const firstCall = mockApi.listExcludedCandidates.mock.calls[0][0];
    expect(firstCall).toHaveProperty("limit", 200);
  });

  // 2. 筛选：输入标的代码后 fetch 应携带 symbol 参数
  it("输入标的代码筛选应触发带 symbol 参数的 fetch", async () => {
    // 第一次返回初始列表
    mockApi.listExcludedCandidates.mockResolvedValue([makeItem()]);
    render(<ExcludedPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 清空调用记录，便于断言下一次 fetch
    mockApi.listExcludedCandidates.mockClear();
    mockApi.listExcludedCandidates.mockResolvedValue([makeItem({ symbol: "600001" })]);

    // 定位标的筛选 Input（通过 placeholder）
    const symbolInput = screen.getByPlaceholderText("excludedPoolFilterSymbol") as HTMLInputElement;
    expect(symbolInput).toBeTruthy();
    fireEvent.change(symbolInput, { target: { value: "600001" } });

    // 等待 fetch 被调用且携带 symbol 参数
    await waitFor(() => {
      expect(mockApi.listExcludedCandidates).toHaveBeenCalled();
      const callArgs = mockApi.listExcludedCandidates.mock.calls[0][0];
      expect(callArgs).toHaveProperty("symbol", "600001");
    });
  });

  // 3. 详情：点击"查看详情"打开弹窗，显示候选与排除事件信息
  it("点击查看详情应打开弹窗并显示候选与排除事件信息", async () => {
    mockApi.listExcludedCandidates.mockResolvedValue([makeItem()]);
    render(<ExcludedPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 点击"查看详情"按钮
    const viewBtnText = screen.getByText("excludedPoolDetailView");
    const viewBtn = viewBtnText.closest("button") as HTMLElement;
    expect(viewBtn).toBeTruthy();
    fireEvent.click(viewBtn);
    // 等待弹窗出现
    await waitFor(() => {
      const modalBody = document.querySelector(".ant-modal-body") as HTMLElement | null;
      expect(modalBody).toBeTruthy();
      const text = modalBody?.textContent ?? "";
      // 弹窗应显示候选信息标签和排除事件信息标签
      expect(text).toContain("excludedPoolDetailCandidate");
      expect(text).toContain("excludedPoolDetailEvent");
      // 候选基础信息应可见
      expect(text).toContain("600000");
      expect(text).toContain("测试银行");
      // 排除事件信息应可见
      expect(text).toContain("excludedPoolDetailExcludedAt");
      expect(text).toContain("user");
      // 幂等键应可见（makeItem 中 idempotency_key 为 "exc-10-2026-07-15"）
      expect(text).toContain("exc-10-2026-07-15");
    }, { timeout: 5000 });
  });

  // 4. 恢复：点击"恢复"打开弹窗，确认后调用 restore API
  it("点击恢复并确认应调用 restoreDiscoveryCandidate API", async () => {
    mockApi.listExcludedCandidates.mockResolvedValue([makeItem()]);
    render(<ExcludedPool />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 点击"恢复"按钮（打开恢复弹窗）
    const restoreBtnText = screen.getByText("excludedPoolRestore");
    const restoreBtn = restoreBtnText.closest("button") as HTMLElement;
    expect(restoreBtn).toBeTruthy();
    fireEvent.click(restoreBtn);
    // 恢复弹窗应出现，包含"恢复到候选池"按钮
    await waitFor(() => {
      expect(screen.getByText("excludedPoolRestoreToCandidate")).toBeInTheDocument();
    });
    // 点击"恢复到候选池"触发 Popconfirm
    const restoreCandidateBtn = screen.getByText("excludedPoolRestoreToCandidate").closest("button") as HTMLElement;
    fireEvent.click(restoreCandidateBtn);
    // Popconfirm 出现，点击确认
    await waitFor(() => {
      const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
      expect(okBtn).toBeTruthy();
    });
    const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
    fireEvent.click(okBtn);
    // 断言 restoreDiscoveryCandidate 被调用，target="candidate"
    await waitFor(() => {
      expect(mockApi.restoreDiscoveryCandidate).toHaveBeenCalled();
      const callArgs = mockApi.restoreDiscoveryCandidate.mock.calls[0];
      expect(callArgs[0]).toBe(10); // candidate_id
      expect(callArgs[1]).toHaveProperty("target", "candidate");
    });
  });

  // 5. 空状态：接口返回空数组时显示空状态
  it("接口返回空数组时应显示 excludedPoolEmpty 空状态", async () => {
    mockApi.listExcludedCandidates.mockResolvedValue([]);
    render(<ExcludedPool />);
    await waitFor(() => {
      expect(screen.getByText("excludedPoolEmpty")).toBeInTheDocument();
    });
    // 同时显示空状态提示
    expect(screen.getByText("excludedPoolEmptyHint")).toBeInTheDocument();
  });

  // 6. 错误状态：接口 reject 时显示错误信息 + 重试按钮
  it("接口 reject 时应显示错误信息和重试按钮", async () => {
    mockApi.listExcludedCandidates.mockRejectedValue(new Error("网络错误"));
    render(<ExcludedPool />);
    await waitFor(() => {
      expect(screen.getByText("excludedPoolError")).toBeInTheDocument();
    });
    // 重试按钮应出现（Alert action 与顶部 toolbar 至少一个）
    expect(screen.getAllByText("excludedPoolRetry").length).toBeGreaterThanOrEqual(1);
  });
});
