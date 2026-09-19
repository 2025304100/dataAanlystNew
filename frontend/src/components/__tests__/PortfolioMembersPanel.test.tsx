// WP4.5：PortfolioMembersPanel 组件测试
//
// 覆盖：
// - 列表渲染：标的/状态/执行模式/来源
// - 状态筛选：选择 status=active 触发带参数的 fetch
// - 空状态：mock 返回空数组显示 portfolioMembersEmpty
// - 错误状态：mock reject 显示错误信息
// - 加载状态：mock 延迟显示 Spin
// - 归档按钮：点击归档调用 archive API
// - 归档持仓冲突 409：显示 Modal.confirm 提示"仅停止买入"或先卖出
// - 暂停按钮：点击暂停调用 pause API
// - 恢复按钮：归档状态下显示恢复按钮，点击调用 restore API
// - 创建成员弹窗：点击"添加成员"打开弹窗，填写表单 + 提交调用 create API
// - 无 portfolioId：显示 portfolioMembersNoPortfolio 空状态
//
// 约束：
// - 不修改已稳定组件实现
// - mock fetch（requestJson），不调用真实后端
// - i18n mock：t(key) 返回 key，便于按 key 断言
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext, mockRequestJson } = vi.hoisted(() => ({
  mockContext: {
    portfolioId: 1 as number | null,
  },
  mockRequestJson: vi.fn(),
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

// Mock AppContext：仅提供 portfolioId
vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

// Mock api/client：仅暴露 requestJson
vi.mock("../../api/client", () => ({
  requestJson: mockRequestJson,
  api: {},
}));

import { PortfolioMembersPanel } from "../PortfolioMembersPanel";

/** 构造一份完整的 PortfolioMember，支持部分覆盖 */
function makeMember(overrides: Partial<Record<string, unknown>> = {}): any {
  return {
    id: 1,
    portfolio_id: 1,
    symbol_id: 100,
    status: "active",
    execution_mode: "manual",
    source_type: "manual",
    source_id: null,
    entry_rule_version_id: null,
    exit_rule_version_id: null,
    effective_from: "2026-07-01T10:00:00Z",
    effective_to: null,
    manual_lock: false,
    priority: 50,
    note: null,
    created_at: "2026-07-01T10:00:00Z",
    updated_at: null,
    symbol: "600000",
    has_position: false,
    ...overrides,
  };
}

describe("PortfolioMembersPanel 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 彻底重置 requestJson mock：清除 Once 队列和默认实现，避免上一个测试的 mockResolvedValue/Once 残留
    mockRequestJson.mockReset();
    mockContext.portfolioId = 1;
  });

  // Modal.confirm 通过 portal 渲染到 document.body，不会随组件 unmount 自动清理
  // 需要在每个测试后手动清理，避免残留的 modal mask 拦截后续测试的点击
  afterEach(() => {
    document.body.innerHTML = "";
  });

  // 1. 列表渲染：标的/状态/执行模式/来源
  it("应渲染组合成员表格，包含标的/状态/执行模式/来源", async () => {
    mockRequestJson.mockResolvedValue([makeMember()]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    // 等待数据加载
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 表头应包含这些列（i18n key）
    expect(screen.getAllByText("portfolioMemberColumnSymbol").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("portfolioMemberColumnStatus").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("portfolioMemberColumnExecutionMode").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("portfolioMemberColumnSource").length).toBeGreaterThanOrEqual(1);
    // 状态/执行模式/来源的本地化文案应显示
    expect(screen.getByText("statusActive")).toBeInTheDocument();
    expect(screen.getByText("modeManual")).toBeInTheDocument();
    expect(screen.getByText("portfolioMemberSourceManual")).toBeInTheDocument();
    // fetch 被调用且包含 portfolio id
    expect(mockRequestJson).toHaveBeenCalled();
    const firstCallUrl = String(mockRequestJson.mock.calls[0][0]);
    expect(firstCallUrl).toContain("/api/v1/portfolios/1/members");
    expect(firstCallUrl).toContain("include_archived=true");
  });

  // 2. 状态筛选：选择 status=active 触发带参数的 fetch
  it("选择 status=active 应触发带 status 参数的 fetch", async () => {
    mockRequestJson.mockResolvedValue([makeMember()]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 清空调用记录，便于断言下一次 fetch
    mockRequestJson.mockClear();
    mockRequestJson.mockResolvedValue([makeMember({ status: "active", symbol: "600001" })]);

    // 定位 status 筛选 Select：通过 placeholder span 文本定位
    const statusPlaceholders = document.querySelectorAll(".ant-select-selection-placeholder");
    const statusPlaceholder = Array.from(statusPlaceholders).find(
      (el) => el.textContent === "portfolioMemberFilterStatus",
    ) as HTMLElement;
    expect(statusPlaceholder).toBeTruthy();
    const statusSelector = statusPlaceholder.closest(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(statusSelector);
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    // 点击 active 选项（label 为 i18n key "statusActive"）
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const activeOption = opts.find((o) => o.textContent === "statusActive") as HTMLElement;
    expect(activeOption).toBeTruthy();
    fireEvent.click(activeOption);

    await waitFor(() => {
      expect(mockRequestJson).toHaveBeenCalled();
      const url = String(mockRequestJson.mock.calls[0][0]);
      expect(url).toContain("status=active");
    });
  });

  // 3. 空状态
  it("接口返回空数组时应显示 portfolioMembersEmpty 空状态", async () => {
    mockRequestJson.mockResolvedValue([]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("portfolioMembersEmpty")).toBeInTheDocument();
    });
  });

  // 4. 错误状态：显示错误信息
  it("接口 reject 时应显示错误信息", async () => {
    mockRequestJson.mockRejectedValue(new Error("network error"));
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("portfolioMembersLoadFailed")).toBeInTheDocument();
    });
    // 错误描述应可见
    expect(screen.getByText("network error")).toBeInTheDocument();
  });

  // 5. 加载状态：显示 Spin 加载动画
  it("加载中应显示 Spin 加载动画", async () => {
    // 让 fetch 永不 resolve，保持 loading 状态
    mockRequestJson.mockImplementation(() => new Promise(() => {}));
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      const spin = document.querySelector(".ant-spin");
      expect(spin).toBeTruthy();
    });
  });

  // 6. 归档按钮：点击归档调用 archive API
  it("点击归档按钮应调用 archive API", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([makeMember()]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 点击归档按钮（触发 Popconfirm）
    const archiveBtn = screen.getByText("archive");
    await user.click(archiveBtn);
    // Popconfirm 出现，点击确认
    await waitFor(() => {
      const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
      expect(okBtn).toBeTruthy();
    });
    // 后续 archive 调用与 refresh 调用
    mockRequestJson.mockResolvedValueOnce(makeMember({ status: "archived" })) // archive response
      .mockResolvedValueOnce([]); // refresh fetch
    const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
    await user.click(okBtn);
    // 断言 archive API 被调用
    await waitFor(() => {
      const archiveCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) =>
          typeof url === "string" && url.includes("/archive") && options?.method === "POST",
      );
      expect(archiveCalls.length).toBeGreaterThanOrEqual(1);
    });
  });

  // 7. 归档持仓冲突 409：显示 Modal.confirm 提示"仅停止买入"
  it("归档返回 409 持仓冲突时应弹出 Modal.confirm 提示", async () => {
    const user = userEvent.setup();
    // 初始返回一个活跃成员
    mockRequestJson.mockResolvedValueOnce([makeMember()]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // archive 返回 409 持仓冲突
    const conflictErr = new Error("member has position") as Error & {
      status?: number;
      detail?: unknown;
    };
    conflictErr.status = 409;
    conflictErr.detail = {
      error: "member_has_position",
      portfolio_id: 1,
      symbol_id: 100,
      quantity: 100,
      message: "member has position",
    };
    mockRequestJson.mockRejectedValueOnce(conflictErr);
    // 点击归档按钮
    const archiveBtn = screen.getByText("archive");
    await user.click(archiveBtn);
    await waitFor(() => {
      const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
      expect(okBtn).toBeTruthy();
    });
    const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
    await user.click(okBtn);
    // 应弹出 Modal.confirm 显示 memberHasPositionTitle / memberHasPositionDesc
    // Modal.confirm 的 title 同时渲染在 .ant-modal-title 和 .ant-modal-confirm-title 中，使用 getAllByText
    await waitFor(() => {
      expect(screen.getAllByText("memberHasPositionTitle").length).toBeGreaterThan(0);
      expect(screen.getByText("memberHasPositionDesc")).toBeInTheDocument();
      // pauseBuyOnly 按钮文本应可见
      expect(screen.getByText("pauseBuyOnly")).toBeInTheDocument();
    });
  });

  // 8. 暂停按钮：点击暂停调用 pause API
  it("点击暂停按钮应调用 pause API", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([makeMember({ status: "active" })]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 点击暂停按钮（不触发 Popconfirm，直接调用）
    const pauseBtn = screen.getByText("pause");
    // 后续 pause 调用与 refresh 调用
    mockRequestJson.mockResolvedValueOnce(makeMember({ status: "paused" })) // pause response
      .mockResolvedValueOnce([]); // refresh fetch
    await user.click(pauseBtn);
    await waitFor(() => {
      const pauseCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) =>
          typeof url === "string" && url.includes("/pause") && options?.method === "POST",
      );
      expect(pauseCalls.length).toBeGreaterThanOrEqual(1);
    });
  });

  // 9. 恢复按钮：归档状态下显示恢复按钮，点击恢复调用 restore API
  it("归档状态成员应显示恢复按钮，点击后调用 restore API", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([makeMember({ status: "archived" })]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 归档状态下应显示恢复按钮，不显示暂停/归档按钮
    const restoreBtn = screen.getByText("restore");
    expect(restoreBtn).toBeInTheDocument();
    // 归档状态下不应显示暂停按钮（不会渲染）
    expect(screen.queryByText("pause")).not.toBeInTheDocument();
    // 点击恢复按钮
    mockRequestJson.mockResolvedValueOnce(makeMember({ status: "active" })) // restore response
      .mockResolvedValueOnce([]); // refresh fetch
    await user.click(restoreBtn);
    await waitFor(() => {
      const restoreCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) =>
          typeof url === "string" && url.includes("/restore") && options?.method === "POST",
      );
      expect(restoreCalls.length).toBeGreaterThanOrEqual(1);
    });
  });

  // 10. 创建成员弹窗：点击"添加成员"打开弹窗，填写表单 + 提交调用 create API
  it("点击添加成员应打开弹窗，填写表单并提交应调用 create API", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([makeMember()]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 点击"添加成员"按钮
    const addBtn = screen.getByText("addMember");
    await user.click(addBtn);
    // 弹窗应显示（Modal 标题和 OK 按钮文本都是 createMember，使用 getAllByText）
    await waitFor(() => {
      expect(screen.getAllByText("createMember").length).toBeGreaterThan(0);
    });
    // 填写表单：symbol_id 必填
    const symbolIdInput = screen.getByLabelText("symbolId") as HTMLInputElement;
    expect(symbolIdInput).toBeTruthy();
    await user.type(symbolIdInput, "100");
    // 点击弹窗 OK 按钮（弹窗底部最后一个 createMember 按钮 = OK 按钮）
    // Modal footer 中的 OK 按钮带有 .ant-btn-primary class
    mockRequestJson.mockResolvedValueOnce(makeMember({ id: 2, symbol_id: 100 })) // create response
      .mockResolvedValueOnce([]); // refresh fetch
    const modalOkBtn = document.querySelector(".ant-modal-footer .ant-btn-primary") as HTMLElement;
    expect(modalOkBtn).toBeTruthy();
    await user.click(modalOkBtn);
    // 断言 create API 被调用
    await waitFor(() => {
      const createCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) =>
          typeof url === "string" &&
          url === "/api/v1/portfolios/1/members" &&
          options?.method === "POST",
      );
      expect(createCalls.length).toBeGreaterThanOrEqual(1);
      // 验证请求体包含 symbol_id
      const createCall = createCalls[0];
      const body = JSON.parse((createCall as any)[1].body);
      expect(body.symbol_id).toBe(100);
    });
  });

  // 11. 无 portfolioId：显示 portfolioMembersNoPortfolio 空状态
  it("无 portfolioId 时应显示 portfolioMembersNoPortfolio 空状态", async () => {
    mockContext.portfolioId = null;
    render(<PortfolioMembersPanel />);
    await waitFor(() => {
      expect(screen.getByText("portfolioMembersNoPortfolio")).toBeInTheDocument();
    });
    // 不应调用 requestJson
    expect(mockRequestJson).not.toHaveBeenCalled();
  });

  // 12. 无持仓成员可以存在：表格可渲染 has_position=false 的成员
  it("无持仓成员可以正常渲染（账户权益不变化）", async () => {
    const noPositionMember = makeMember({ has_position: false, symbol: "600001" });
    mockRequestJson.mockResolvedValue([noPositionMember]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600001")).toBeInTheDocument();
    });
    // 不应显示 hasPosition Tag
    expect(screen.queryByText("hasPosition")).not.toBeInTheDocument();
    // 状态/执行模式/来源应正常渲染
    expect(screen.getByText("statusActive")).toBeInTheDocument();
    expect(screen.getByText("modeManual")).toBeInTheDocument();
    expect(screen.getByText("portfolioMemberSourceManual")).toBeInTheDocument();
  });

  // 13. 有持仓成员显示 hasPosition Tag
  it("有持仓成员应显示 hasPosition Tag", async () => {
    const hasPositionMember = makeMember({ has_position: true, symbol: "600002" });
    mockRequestJson.mockResolvedValue([hasPositionMember]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600002")).toBeInTheDocument();
    });
    // 应显示 hasPosition Tag
    expect(screen.getByText("hasPosition")).toBeInTheDocument();
  });

  // 14. 暂停状态成员不显示暂停按钮，显示归档按钮
  it("暂停状态成员应显示归档按钮但不显示暂停按钮", async () => {
    mockRequestJson.mockResolvedValue([makeMember({ status: "paused" })]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("600000")).toBeInTheDocument();
    });
    // 暂停状态下显示状态标签 statusPaused
    expect(screen.getByText("statusPaused")).toBeInTheDocument();
    // 不应显示暂停按钮
    expect(screen.queryByText("pause")).not.toBeInTheDocument();
    // 应显示归档按钮
    expect(screen.getByText("archive")).toBeInTheDocument();
    // 不应显示恢复按钮
    expect(screen.queryByText("restore")).not.toBeInTheDocument();
  });

  // 15. WP4-FIX：最近信号列渲染 - 含 latest_signal 的成员显示对应文本
  it("含 latest_signal_action=buy 的成员应在最近信号列显示买入文本", async () => {
    const member = makeMember({
      symbol: "610200",
      latest_signal: "买入 2026-07-19",
      latest_signal_at: "2026-07-19T10:00:00Z",
      latest_signal_action: "buy",
    });
    mockRequestJson.mockResolvedValue([member]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("610200")).toBeInTheDocument();
    });
    // 列头应包含"最近信号"i18n key（列头包裹在 Space/Tooltip 中，可能匹配多个祖先元素）
    expect(screen.getAllByText("portfolioMemberColumnLatestSignal").length).toBeGreaterThanOrEqual(1);
    // 应渲染买入动作的 i18n key（由 action + latest_signal_at 构造）
    expect(screen.getByText(/portfolioMemberSignalBuy/)).toBeInTheDocument();
  });

  // 16. WP4-FIX：卖出信号用红色 Tag
  it("含 latest_signal_action=sell 的成员应显示卖出文本", async () => {
    const member = makeMember({
      symbol: "610201",
      latest_signal: "卖出 2026-07-20",
      latest_signal_at: "2026-07-20T10:00:00Z",
      latest_signal_action: "sell",
    });
    mockRequestJson.mockResolvedValue([member]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("610201")).toBeInTheDocument();
    });
    // 应渲染卖出动作的 i18n key
    expect(screen.getByText(/portfolioMemberSignalSell/)).toBeInTheDocument();
  });

  // 17. WP4-FIX：无 latest_signal 的成员显示"-"
  it("无 latest_signal 的成员应在最近信号列显示 -", async () => {
    const member = makeMember({
      symbol: "610202",
      latest_signal: null,
      latest_signal_at: null,
      latest_signal_action: null,
    });
    mockRequestJson.mockResolvedValue([member]);
    render(<PortfolioMembersPanel portfolioId={1} />);
    await waitFor(() => {
      expect(screen.getByText("610202")).toBeInTheDocument();
    });
    // 列头应存在（Space/Tooltip 包裹可能匹配多个祖先元素）
    expect(screen.getAllByText("portfolioMemberColumnLatestSignal").length).toBeGreaterThanOrEqual(1);
    // 不应渲染买入/卖出动作文本
    expect(screen.queryByText(/portfolioMemberSignalBuy/)).not.toBeInTheDocument();
    expect(screen.queryByText(/portfolioMemberSignalSell/)).not.toBeInTheDocument();
    // 应渲染 "-"（最近信号列的空值占位）
    // 表格中可能有多个 "-"，使用 getAllByText 并断言至少 1 个
    const dashes = screen.getAllByText("-");
    expect(dashes.length).toBeGreaterThanOrEqual(1);
  });
});
