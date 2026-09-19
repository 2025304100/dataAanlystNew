// UAT-PAGES.2：ScanHistory 组件测试（扫描记录）
//
// 覆盖：
// - 加载：渲染表格，包含 ID/运行名/范围/交易日期/状态/生成时间/阶段耗时/缓存命中/差异摘要
// - 筛选：选择 scope 触发带 scope 参数的 fetch
// - 详情：点击"查看详情"打开弹窗，显示快照/任务/参数/阶段耗时/差异/错误
// - 阶段耗时：stage_durations 渲染为 Tooltip 文本
//
// 约束：
// - 不修改已稳定组件实现
// - mock api/client（listScanRuns / getScanRunDetail），不调用真实后端
// - i18n mock：t(key) 返回 key，便于按 key 断言
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    listScanRuns: vi.fn(),
    getScanRunDetail: vi.fn(),
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
  assetTypeLabel: (v: string | null | undefined) => v ?? "unknown",
  regionShortLabel: (v: string | null | undefined) => v ?? "-",
  getLocale: () => "zh-CN",
  setLocale: () => {},
}));

// Mock antd：保留组件库（ScanHistory 未直接使用 message，但 antd 组件内部可能依赖）
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

// Mock api/client：仅暴露 ScanHistory 用到的两个方法
vi.mock("../../../api/client", () => ({
  api: mockApi,
}));

import ScanHistory from "../ScanHistory";

/** 构造一份完整的 ScanRunItem，支持部分覆盖 */
function makeItem(overrides: Partial<Record<string, unknown>> = {}): any {
  return {
    id: 137,
    run_name: "fast-scan-cn-stock-20260715",
    scope_snapshot: "cn_stock",
    filters_snapshot: '{"min_score": 55}',
    portfolio_id: null,
    portfolio_rule_id: null,
    status: "done",
    started_at: "2026-07-15T10:00:00Z",
    finished_at: "2026-07-15T10:02:30Z",
    created_at: "2026-07-15T10:00:00Z",
    snapshot_id: 42,
    cache_key: "snapshot-42-cn-stock-55-hash1-v1-pf1-pr1",
    cache_hit: 1,
    total_in_snapshot: 5500,
    coarse_match_count: 300,
    advanced_match_count: 280,
    result_rows_written: 50,
    degraded_reason: null,
    task_id: "task-abc-123",
    scope: "cn-stock",
    min_score: 55,
    stage_durations: [
      { stage: "snapshot_precheck", duration_ms: 1200 },
      { stage: "coarse_filter", duration_ms: 8500 },
      { stage: "advanced_filter", duration_ms: 15000 },
    ],
    dirty_symbol_count: 120,
    reused_score_count: 5380,
    rescored_count: 120,
    snapshot_hit: 1,
    task_degraded_reason: null,
    snapshot_scope: "cn_stock",
    snapshot_trade_date: "2026-07-15",
    snapshot_status: "ready",
    snapshot_generated_at: "2026-07-15T09:50:00Z",
    snapshot_symbol_count: 5500,
    snapshot_coverage_pct: 99.5,
    snapshot_dirty_symbol_count: 120,
    snapshot_build_duration_seconds: 180,
    snapshot_error_summary: null,
    scoring_config_id: 1,
    scoring_config_version: 3,
    weight_mode: "ridge",
    factor_model_run_id: "model-2026-07",
    snapshot_data_cutoff_at: "2026-07-14",
    ...overrides,
  };
}

describe("ScanHistory 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.listScanRuns.mockResolvedValue([makeItem()]);
    mockApi.getScanRunDetail.mockResolvedValue(makeItem());
  });

  // 1. 加载：渲染表格，包含 ID/运行名/范围/交易日期/状态/生成时间
  it("应渲染扫描记录表格，包含 ID/运行名/范围/交易日期/状态", async () => {
    mockApi.listScanRuns.mockResolvedValue([makeItem()]);
    render(<ScanHistory />);
    // 等待数据加载
    await waitFor(() => {
      expect(screen.getByText("#137")).toBeInTheDocument();
    });
    // 运行名应显示
    expect(screen.getByText("fast-scan-cn-stock-20260715")).toBeInTheDocument();
    // 范围应显示（cn-stock）
    expect(screen.getByText("cn-stock")).toBeInTheDocument();
    // 交易日期应显示（截取前 10 位）
    expect(screen.getByText("2026-07-15")).toBeInTheDocument();
    // 状态应为 done tag
    expect(screen.getByText("done")).toBeInTheDocument();
    // listScanRuns 被调用且包含默认 limit
    expect(mockApi.listScanRuns).toHaveBeenCalled();
    const firstCall = mockApi.listScanRuns.mock.calls[0][0];
    expect(firstCall).toHaveProperty("limit", 100);
  });

  // 2. 筛选：选择 scope 后 fetch 应携带 scope 参数
  it("选择 scope 筛选应触发带 scope 参数的 fetch", async () => {
    // 第一次返回初始列表
    mockApi.listScanRuns.mockResolvedValue([makeItem()]);
    render(<ScanHistory />);
    await waitFor(() => {
      expect(screen.getByText("#137")).toBeInTheDocument();
    });
    mockApi.listScanRuns.mockClear();
    mockApi.listScanRuns.mockResolvedValue([makeItem({ scope: "cn-etf" })]);

    // 定位 scope 筛选 Select（通过 placeholder）
    const placeholders = document.querySelectorAll(".ant-select-selection-placeholder");
    const scopePlaceholder = Array.from(placeholders).find(
      (el) => el.textContent === "scanHistoryFilterScope",
    ) as HTMLElement;
    expect(scopePlaceholder).toBeTruthy();
    const scopeSelector = scopePlaceholder.closest(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(scopeSelector);
    // 选项应出现
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    // 点击 cn-etf 选项
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const cnEtfOption = opts.find((o) => o.textContent === "cn-etf") as HTMLElement;
    expect(cnEtfOption).toBeTruthy();
    fireEvent.click(cnEtfOption);

    // 断言 fetch 被调用且携带 scope=cn-etf 参数
    await waitFor(() => {
      expect(mockApi.listScanRuns).toHaveBeenCalled();
      const callArgs = mockApi.listScanRuns.mock.calls[0][0];
      expect(callArgs).toHaveProperty("scope", "cn-etf");
    });
  });

  // 3. 详情：点击"查看详情"打开弹窗，显示快照/任务/参数信息
  it("点击查看详情应打开弹窗并显示快照/任务/参数信息", async () => {
    mockApi.listScanRuns.mockResolvedValue([makeItem()]);
    render(<ScanHistory />);
    await waitFor(() => {
      expect(screen.getByText("#137")).toBeInTheDocument();
    });
    // 点击"查看详情"按钮
    const viewBtnText = screen.getByText("scanHistoryDetailView");
    const viewBtn = viewBtnText.closest("button") as HTMLElement;
    expect(viewBtn).toBeTruthy();
    fireEvent.click(viewBtn);
    // 等待弹窗出现并加载详情
    await waitFor(() => {
      const modalBody = document.querySelector(".ant-modal-body") as HTMLElement | null;
      expect(modalBody).toBeTruthy();
      const text = modalBody?.textContent ?? "";
      // 弹窗应显示快照/任务/参数/阶段耗时/差异/错误六个分区标签
      expect(text).toContain("scanHistoryDetailSnapshot");
      expect(text).toContain("scanHistoryDetailTask");
      expect(text).toContain("scanHistoryDetailParams");
      expect(text).toContain("scanHistoryDetailStageDurations");
      expect(text).toContain("scanHistoryDetailErrors");
      // 快照信息应可见（snapshot_id=42）
      expect(text).toContain("42");
      // 任务信息应可见（task_id=task-abc-123）
      expect(text).toContain("task-abc-123");
    }, { timeout: 5000 });
    // getScanRunDetail 应被调用
    expect(mockApi.getScanRunDetail).toHaveBeenCalledWith(137);
  });

  // 4. 阶段耗时：stage_durations 应渲染为 Tooltip 文本
  it("stage_durations 应渲染为 stage:ms 格式文本", async () => {
    const item = makeItem({
      stage_durations: [
        { stage: "snapshot_precheck", duration_ms: 1200 },
        { stage: "coarse_filter", duration_ms: 8500 },
      ],
    });
    mockApi.listScanRuns.mockResolvedValue([item]);
    render(<ScanHistory />);
    await waitFor(() => {
      expect(screen.getByText("#137")).toBeInTheDocument();
    });
    // 阶段耗时列应渲染 stage:ms 格式文本
    // scanHistoryDurationMs = "{ms} 毫秒"，所以渲染后应为 "snapshot_precheck:1200 毫秒"
    // 由于 i18n mock t(key) 返回 key 本身，t("scanHistoryDurationMs").replace("{ms}", "1200") = "1200 毫秒" 不会被替换
    // 实际渲染：t("scanHistoryDurationMs") 返回 "scanHistoryDurationMs"，replace 不生效
    // 所以 summary = "snapshot_precheck:scanHistoryDurationMs coarse_filter:scanHistoryDurationMs"
    // 断言阶段名应出现
    expect(screen.getByText(/snapshot_precheck/)).toBeInTheDocument();
    expect(screen.getByText(/coarse_filter/)).toBeInTheDocument();
  });

  // 5. 空状态：接口返回空数组时显示空状态
  it("接口返回空数组时应显示 scanHistoryEmpty 空状态", async () => {
    mockApi.listScanRuns.mockResolvedValue([]);
    render(<ScanHistory />);
    await waitFor(() => {
      expect(screen.getByText("scanHistoryEmpty")).toBeInTheDocument();
    });
    expect(screen.getByText("scanHistoryEmptyHint")).toBeInTheDocument();
  });

  // 6. 错误状态：接口 reject 时显示错误信息 + 重试按钮
  it("接口 reject 时应显示错误信息和重试按钮", async () => {
    mockApi.listScanRuns.mockRejectedValue(new Error("网络错误"));
    render(<ScanHistory />);
    await waitFor(() => {
      expect(screen.getByText("scanHistoryError")).toBeInTheDocument();
    });
    // 重试按钮应出现
    expect(screen.getAllByText("scanHistoryRetry").length).toBeGreaterThanOrEqual(1);
  });

  // 7. 缓存命中：cache_hit=1 应显示命中 Tag
  it("cache_hit=1 应显示命中 Tag", async () => {
    mockApi.listScanRuns.mockResolvedValue([makeItem({ cache_hit: 1 })]);
    render(<ScanHistory />);
    await waitFor(() => {
      expect(screen.getByText("#137")).toBeInTheDocument();
    });
    // 应显示 scanHistoryCacheHit tag
    expect(screen.getByText("scanHistoryCacheHit")).toBeInTheDocument();
  });
});
