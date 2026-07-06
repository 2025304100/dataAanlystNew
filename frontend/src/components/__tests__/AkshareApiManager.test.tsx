import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { message } from "antd";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
// globals: true 配置下 vi 作为全局变量在 hoisted 回调中可用
const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    listAkshareApis: vi.fn(async () => [
      {
        key: "stock_info_sz_name_code",
        name: "深交所股票列表",
        category: "discovery",
        module: "akshare",
        description: "深交所股票代码列表",
        default_strategy: "standard",
        enabled: true,
        anti_risk_strategy: "standard",
        delay_min_ms: 200,
        delay_max_ms: 500,
        last_probe_at: "2026-07-04T10:00:00Z",
        last_probe_success: true,
        last_probe_latency_ms: 120,
        last_probe_error: null,
        last_call_at: "2026-07-04T11:00:00Z",
        last_call_success: true,
        last_call_error: null,
        total_calls: 10,
        total_failures: 0,
      },
      {
        key: "stock_zh_a_spot_em",
        name: "东财实时行情",
        category: "realtime",
        module: "akshare",
        description: "东方财富实时行情",
        default_strategy: "conservative",
        enabled: true,
        anti_risk_strategy: "conservative",
        delay_min_ms: 1000,
        delay_max_ms: 2000,
        last_probe_at: null,
        last_probe_success: null,
        last_probe_latency_ms: null,
        last_probe_error: null,
        last_call_at: null,
        last_call_success: null,
        last_call_error: null,
        total_calls: 0,
        total_failures: 0,
      },
      {
        key: "fund_etf_fund_info_em",
        name: "ETF 基金信息",
        category: "etf",
        module: "akshare",
        description: "ETF 基金指标",
        default_strategy: "fast",
        enabled: false,
        anti_risk_strategy: "fast",
        delay_min_ms: 100,
        delay_max_ms: 200,
        last_probe_at: "2026-07-04T09:00:00Z",
        last_probe_success: false,
        last_probe_latency_ms: null,
        last_probe_error: "HTTP 403 Forbidden",
        last_call_at: "2026-07-04T09:30:00Z",
        last_call_success: false,
        last_call_error: "timeout",
        total_calls: 5,
        total_failures: 3,
      },
    ]),
    listAkshareStrategies: vi.fn(async () => [
      { key: "fast", name: "快速", delay_min_ms: 100, delay_max_ms: 200, max_retries: 1, desc: "快速档位" },
      { key: "standard", name: "标准", delay_min_ms: 200, delay_max_ms: 500, max_retries: 2, desc: "标准档位" },
      { key: "conservative", name: "保守", delay_min_ms: 1000, delay_max_ms: 2000, max_retries: 3, desc: "保守档位" },
      { key: "extreme", name: "极保守", delay_min_ms: 3000, delay_max_ms: 5000, max_retries: 5, desc: "极保守档位" },
      { key: "custom", name: "自定义", delay_min_ms: null, delay_max_ms: null, max_retries: 3, desc: "自定义档位" },
    ]),
    probeAkshareApi: vi.fn(async () => ({ key: "stock_info_sz_name_code", success: true, latency_ms: 150, error: null })),
    updateAkshareApiConfig: vi.fn(async (_k: string, p: unknown) => p),
  },
}));

// Mock i18n: t(key) 返回 key，template 返回拼接后的字符串
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
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

// Mock api/client
vi.mock("../../api/client", () => ({
  api: mockApi,
}));

import AkshareApiManager from "../AkshareApiManager";

describe("AkshareApiManager 组件渲染测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should render section title and description", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("apiMgmtSectionTitle")).toBeInTheDocument();
    });
    expect(screen.getByText("apiMgmtSectionDesc")).toBeInTheDocument();
  });

  it("should render refresh and probe-all buttons in card extra", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("apiMgmtRefresh")).toBeInTheDocument();
    });
    expect(screen.getByText("apiMgmtProbeAll")).toBeInTheDocument();
  });

  it("should load apis and strategies on mount", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(mockApi.listAkshareApis).toHaveBeenCalledWith("zh-CN");
      expect(mockApi.listAkshareStrategies).toHaveBeenCalledWith("zh-CN");
    });
  });

  it("should render api rows with name and key", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    expect(screen.getByText("stock_info_sz_name_code")).toBeInTheDocument();
    expect(screen.getByText("东财实时行情")).toBeInTheDocument();
  });

  it("should render category tag for each row", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("discovery")).toBeInTheDocument();
      expect(screen.getByText("realtime")).toBeInTheDocument();
      expect(screen.getByText("etf")).toBeInTheDocument();
    });
  });

  it("should render status tags based on probe result", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      // 第一行 probe_success=true -> 显示 apiStatusSuccess
      expect(screen.getAllByText("apiStatusSuccess").length).toBeGreaterThan(0);
      // 第二行 probe_success=null -> 显示 apiStatusNever
      expect(screen.getByText("apiStatusNever")).toBeInTheDocument();
      // 第三行 enabled=false -> 显示 apiStatusDisabled
      expect(screen.getByText("apiStatusDisabled")).toBeInTheDocument();
    });
  });

  it("should render column headers", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      // antd Table 固定列会渲染多份 header，用 getAllByText
      expect(screen.getAllByText("apiColKey").length).toBeGreaterThan(0);
      expect(screen.getAllByText("apiColCategory").length).toBeGreaterThan(0);
      expect(screen.getAllByText("apiColStatus").length).toBeGreaterThan(0);
      expect(screen.getAllByText("apiColStrategy").length).toBeGreaterThan(0);
      expect(screen.getAllByText("apiColDelay").length).toBeGreaterThan(0);
      expect(screen.getAllByText("apiColLastProbe").length).toBeGreaterThan(0);
      expect(screen.getAllByText("apiColLastCall").length).toBeGreaterThan(0);
      expect(screen.getAllByText("apiColStats").length).toBeGreaterThan(0);
      expect(screen.getAllByText("apiColActions").length).toBeGreaterThan(0);
    });
  });

  it("should render enabled toggle labels for all rows", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      const labels = screen.getAllByText("apiEnabledToggle");
      expect(labels.length).toBe(3);
    });
  });

  it("should call listAkshareApis when refresh button clicked", async () => {
    render(<AkshareApiManager />);
    // 等待表格内容渲染完成（表示初次加载已结束，loading=false）
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    expect(mockApi.listAkshareApis).toHaveBeenCalledTimes(1);
    // 用 getAllByText 找到刷新按钮并点击
    const candidates = screen.getAllByText("apiMgmtRefresh");
    fireEvent.click(candidates[0]);
    await waitFor(() => {
      expect(mockApi.listAkshareApis).toHaveBeenCalledTimes(2);
    });
  });

  it("should trigger probeAll and call probeAkshareApi for each row", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    const probeAllBtn = screen.getByText("apiMgmtProbeAll");
    fireEvent.click(probeAllBtn);
    await waitFor(() => {
      expect(mockApi.probeAkshareApi).toHaveBeenCalledTimes(3);
    });
  });

  it("should display last call status text based on last_call_success", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      // 第三行 last_call_success=false -> 显示 apiStatusFailed
      expect(screen.getByText("apiStatusFailed")).toBeInTheDocument();
    });
  });

  it("should render apiNeverCalled for rows without last_call_at", async () => {
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("apiNeverCalled")).toBeInTheDocument();
    });
  });
});

describe("AkshareApiManager 交互测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 重置 probeAkshareApi 为默认实现（防止前一个测试的 deferred 实现残留）
    mockApi.probeAkshareApi.mockImplementation(async () => ({
      key: "stock_info_sz_name_code",
      success: true,
      latency_ms: 150,
      error: null,
    }));
    // 覆盖 updateAkshareApiConfig 以返回完整行 + patch，避免行数据被破坏
    mockApi.updateAkshareApiConfig.mockImplementation(async (key: string, payload: any) => {
      const apis = await mockApi.listAkshareApis();
      const row = (apis as any[]).find((a) => a.key === key);
      return { ...row, ...payload };
    });
  });

  it("should call updateAkshareApiConfig and show success toast when Switch toggled", async () => {
    const user = userEvent.setup();
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    const switches = screen.getAllByRole("switch");
    // 第一行 enabled=true，点击后变为 false
    await user.click(switches[0]);
    await waitFor(() => {
      expect(mockApi.updateAkshareApiConfig).toHaveBeenCalledWith(
        "stock_info_sz_name_code",
        { enabled: false },
        "zh-CN",
      );
      expect(message.success).toHaveBeenCalledWith("apiUpdateSuccess");
    });
  });

  it("should call updateAkshareApiConfig when strategy Select switched to conservative", async () => {
    const user = userEvent.setup();
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    // 点击第一行的 Select selector（策略选择器）—— antd Select 用 mouseDown 打开下拉
    const firstSelector = document.querySelectorAll(".ant-select-selector")[0];
    fireEvent.mouseDown(firstSelector as HTMLElement);
    // 在下拉框中点击 "保守" 选项 —— 用 antd 内部 class 定位并 click
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const target = opts.find((o) => o.textContent?.includes("保守"));
    expect(target).toBeDefined();
    fireEvent.click(target!);
    await waitFor(() => {
      expect(mockApi.updateAkshareApiConfig).toHaveBeenCalledWith(
        "stock_info_sz_name_code",
        { anti_risk_strategy: "conservative" },
        "zh-CN",
      );
    });
  });

  it("should show two InputNumbers and save button when strategy switched to custom", async () => {
    const user = userEvent.setup();
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    // 初始无 InputNumber（Switch 列的 Select 不产生 InputNumber）
    expect(document.querySelectorAll(".ant-input-number").length).toBe(0);
    // 切到 custom
    const firstSelector = document.querySelectorAll(".ant-select-selector")[0];
    fireEvent.mouseDown(firstSelector as HTMLElement);
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const target = opts.find((o) => o.textContent?.includes("自定义"));
    expect(target).toBeDefined();
    fireEvent.click(target!);
    // 切到 custom 后应出现两个 InputNumber + 保存按钮
    await waitFor(() => {
      expect(document.querySelectorAll(".ant-input-number").length).toBeGreaterThanOrEqual(2);
    });
    // 保存按钮文本为 apiMgmtRefresh，原本 card extra 有一个，现在应至少 2 个
    expect(screen.getAllByText("apiMgmtRefresh").length).toBeGreaterThanOrEqual(2);
  });

  it("should call updateAkshareApiConfig with custom delays when save button clicked", async () => {
    const user = userEvent.setup();
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    // 切到 custom
    const firstSelector = document.querySelectorAll(".ant-select-selector")[0];
    fireEvent.mouseDown(firstSelector as HTMLElement);
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const target = opts.find((o) => o.textContent?.includes("自定义"));
    expect(target).toBeDefined();
    fireEvent.click(target!);
    await waitFor(() => {
      expect(document.querySelectorAll(".ant-input-number").length).toBeGreaterThanOrEqual(2);
    });
    // 修改 min/max
    const inputs = document.querySelectorAll(".ant-input-number-input");
    await user.clear(inputs[0] as HTMLInputElement);
    await user.type(inputs[0] as HTMLInputElement, "500");
    await user.clear(inputs[1] as HTMLInputElement);
    await user.type(inputs[1] as HTMLInputElement, "2000");
    // 点击保存按钮（最后一个 apiMgmtRefresh）
    const saveButtons = screen.getAllByText("apiMgmtRefresh");
    await user.click(saveButtons[saveButtons.length - 1]);
    await waitFor(() => {
      expect(mockApi.updateAkshareApiConfig).toHaveBeenCalledWith(
        "stock_info_sz_name_code",
        { anti_risk_strategy: "custom", delay_min_ms: 500, delay_max_ms: 2000 },
        "zh-CN",
      );
    });
  });

  it("should call probeAkshareApi when single probe button clicked", async () => {
    const user = userEvent.setup();
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    const probeButtons = screen.getAllByText("apiMgmtProbe");
    await user.click(probeButtons[0]);
    await waitFor(() => {
      expect(mockApi.probeAkshareApi).toHaveBeenCalledWith("stock_info_sz_name_code");
    });
  });

  it("should disable single probe buttons during probeAll", async () => {
    const user = userEvent.setup();
    let resolveProbe!: (value: any) => void;
    const pending = new Promise<any>((resolve) => {
      resolveProbe = resolve;
    });
    mockApi.probeAkshareApi.mockImplementation(async () => pending);
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    const probeAllBtn = screen.getByText("apiMgmtProbeAll");
    await user.click(probeAllBtn);
    // 批量探测期间，单按钮应禁用
    await waitFor(() => {
      const probeBtns = screen
        .getAllByText("apiMgmtProbe")
        .map((el) => el.closest("button"));
      probeBtns.forEach((btn) => expect(btn).toBeDisabled());
    });
    // 释放以结束测试（返回有效结果避免 result.success 报错）
    resolveProbe({ key: "stock_info_sz_name_code", success: true, latency_ms: 150, error: null });
    await waitFor(() => {
      expect(mockApi.probeAkshareApi).toHaveBeenCalledTimes(3);
    });
  });

  it("should re-enable single probe buttons after probeAll completes", async () => {
    const user = userEvent.setup();
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    const probeAllBtn = screen.getByText("apiMgmtProbeAll");
    await user.click(probeAllBtn);
    // 等待批量探测完成（probingAll=false, probingKeys 清空）
    await waitFor(() => {
      expect(mockApi.probeAkshareApi).toHaveBeenCalledTimes(3);
    });
    // 完成后单按钮应恢复可用（等待 loading 状态清除）
    await waitFor(() => {
      const probeBtns = screen
        .getAllByText("apiMgmtProbe")
        .map((el) => el.closest("button"));
      probeBtns.forEach((btn) => {
        expect(btn).not.toBeDisabled();
        expect(btn?.className).not.toContain("ant-btn-loading");
      });
    });
  });

  it("should re-call listAkshareApis and listAkshareStrategies when refresh button clicked", async () => {
    const user = userEvent.setup();
    render(<AkshareApiManager />);
    await waitFor(() => {
      expect(screen.getByText("深交所股票列表")).toBeInTheDocument();
    });
    expect(mockApi.listAkshareApis).toHaveBeenCalledTimes(1);
    expect(mockApi.listAkshareStrategies).toHaveBeenCalledTimes(1);
    // card extra 中的刷新按钮（首个 apiMgmtRefresh）
    const refreshBtn = screen.getAllByText("apiMgmtRefresh")[0];
    await user.click(refreshBtn);
    await waitFor(() => {
      expect(mockApi.listAkshareApis).toHaveBeenCalledTimes(2);
      expect(mockApi.listAkshareStrategies).toHaveBeenCalledTimes(2);
    });
  });
});
