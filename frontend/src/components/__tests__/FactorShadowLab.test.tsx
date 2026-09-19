// WP6-06：FactorShadowLab 组件测试
//
// 覆盖：
// - 渲染：未输入 factorVersionId 时显示空状态提示
// - 加载数据：显示观察汇总、健康状态、IC 曲线、观察记录表
// - 门禁状态：complete/incomplete 不同 Tag
// - 健康告警：warn/critical 告警展示，隔离建议
// - 审批 Modal：打开、输入原因、提交成功/失败
// - 原因为空时显示警告
//
// 约束：
// - mock api/client（api），不调用真实后端
// - mock antd 的 message（兼容 App.useApp()）
// - mock echarts-for-react 避免 canvas 副作用
// - i18n mock：t(key) 返回 key
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import type { ReactNode } from "react";

const { mockContext, mockApi, mockMessage } = vi.hoisted(() => ({
  mockContext: {
    locale: "zh-CN",
    showToast: vi.fn(),
  },
  mockApi: {
    listShadowObservations: vi.fn(),
    getShadowObservationSummary: vi.fn(),
    getShadowHealth: vi.fn(),
    requestActivation: vi.fn(),
    approveActivation: vi.fn(),
    rejectActivation: vi.fn(),
    quarantineFactor: vi.fn(),
  },
  mockMessage: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: mockMessage,
    App: {
      ...actual.App,
      useApp: () => ({
        message: mockMessage,
        notification: mockMessage,
        modal: {},
      }),
    },
  };
});

vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string) => key,
}));

vi.mock("../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

vi.mock("../../api/client", () => ({
  api: mockApi,
}));

vi.mock("echarts-for-react", () => ({
  default: () => <div data-testid="mock-chart" />,
}));

import FactorShadowLab from "../factors/FactorShadowLab";

/** 构造一条观察记录。 */
function makeObservation(overrides: Partial<any> = {}): any {
  return {
    id: 1,
    trade_date: "2026-08-01",
    is_valid_day: true,
    invalid_reason: null,
    ic_value: 0.05,
    coverage: 0.92,
    turnover: 0.35,
    completeness_ratio: 0.96,
    observed_symbols: 4800,
    expected_symbols: 5000,
    health_status: "healthy",
    health_reason: null,
    metrics: {},
    ...overrides,
  };
}

/** 构造观察期汇总。 */
function makeSummary(overrides: Partial<any> = {}): any {
  return {
    factor_version_id: 1,
    valid_days: 25,
    min_required_days: 20,
    is_complete: true,
    reason: null,
    ...overrides,
  };
}

/** 构造健康报告（无告警）。 */
function makeHealthyReport(overrides: Partial<any> = {}): any {
  return {
    factor_version_id: 1,
    health_status: "healthy",
    alerts: [],
    recent_ic_mean: 0.05,
    recent_ic_std: 0.12,
    historical_ic_mean: 0.04,
    recent_coverage_mean: 0.92,
    historical_coverage_mean: 0.90,
    n_valid_days: 25,
    n_total_days: 25,
    should_quarantine: false,
    ...overrides,
  };
}

/** 构造带告警的健康报告。 */
function makeDegradedReport(overrides: Partial<any> = {}): any {
  return {
    factor_version_id: 1,
    health_status: "degraded",
    alerts: [
      {
        alert_type: "ic_decay",
        severity: "warn",
        message: "IC 衰减：近期 0.0200 仅为历史 0.0500 的 40.0%",
        current_value: 0.02,
        threshold: 0.025,
        window_days: 5,
      },
    ],
    recent_ic_mean: 0.02,
    recent_ic_std: 0.15,
    historical_ic_mean: 0.05,
    recent_coverage_mean: 0.92,
    historical_coverage_mean: 0.90,
    n_valid_days: 25,
    n_total_days: 25,
    should_quarantine: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FactorShadowLab", () => {
  it("renders empty state hint when no factor version id entered", () => {
    render(<FactorShadowLab />);
    expect(screen.getByText("shadowLabInputHint")).toBeInTheDocument();
  });

  it("loads and displays observation summary, health, and observations after entering factor version id", async () => {
    mockApi.listShadowObservations.mockResolvedValue([makeObservation()]);
    mockApi.getShadowObservationSummary.mockResolvedValue(makeSummary());
    mockApi.getShadowHealth.mockResolvedValue(makeHealthyReport());

    render(<FactorShadowLab />);

    // 输入 factor version id 触发加载
    const inputs = screen.getAllByRole("spinbutton");
    // 第二个 InputNumber 是 factorVersionId
    const fvInput = inputs[1];
    await act(async () => {
      fireEvent.change(fvInput, { target: { value: "1" } });
    });

    await waitFor(() => {
      expect(mockApi.listShadowObservations).toHaveBeenCalledWith(1, { limit: 200 });
    });

    // 观察汇总卡片标题
    await waitFor(() => {
      expect(screen.getByText("shadowLabObservationSummary")).toBeInTheDocument();
    });

    // 门禁状态 complete
    expect(screen.getByText("shadowLabGateComplete")).toBeInTheDocument();

    // 观察记录表标题
    expect(screen.getByText("shadowLabObservations")).toBeInTheDocument();

    // 观察记录中有交易日数据
    expect(screen.getByText("2026-08-01")).toBeInTheDocument();
  });

  it("shows incomplete gate tag when observation not complete", async () => {
    mockApi.listShadowObservations.mockResolvedValue([]);
    mockApi.getShadowObservationSummary.mockResolvedValue(
      makeSummary({ is_complete: false, valid_days: 10, reason: "需要至少 20 个有效交易日" }),
    );
    mockApi.getShadowHealth.mockResolvedValue(makeHealthyReport());

    render(<FactorShadowLab />);
    const inputs = screen.getAllByRole("spinbutton");
    await act(async () => {
      fireEvent.change(inputs[1], { target: { value: "1" } });
    });

    await waitFor(() => {
      expect(screen.getByText("shadowLabGateIncomplete")).toBeInTheDocument();
    });
  });

  it("displays health alerts when degraded", async () => {
    mockApi.listShadowObservations.mockResolvedValue([makeObservation()]);
    mockApi.getShadowObservationSummary.mockResolvedValue(makeSummary());
    mockApi.getShadowHealth.mockResolvedValue(makeDegradedReport());

    render(<FactorShadowLab />);
    const inputs = screen.getAllByRole("spinbutton");
    await act(async () => {
      fireEvent.change(inputs[1], { target: { value: "1" } });
    });

    await waitFor(() => {
      // 告警消息展示
      expect(screen.getByText(/IC 衰减/)).toBeInTheDocument();
    });
  });

  it("shows quarantine recommendation when should_quarantine is true", async () => {
    mockApi.listShadowObservations.mockResolvedValue([makeObservation()]);
    mockApi.getShadowObservationSummary.mockResolvedValue(makeSummary());
    mockApi.getShadowHealth.mockResolvedValue(
      makeDegradedReport({
        health_status: "blocked",
        should_quarantine: true,
        alerts: [
          {
            alert_type: "constant_factor",
            severity: "critical",
            message: "因子常数化",
          },
        ],
      }),
    );

    render(<FactorShadowLab />);
    const inputs = screen.getAllByRole("spinbutton");
    await act(async () => {
      fireEvent.change(inputs[1], { target: { value: "1" } });
    });

    await waitFor(() => {
      expect(screen.getByText("shadowLabQuarantineRecommended")).toBeInTheDocument();
    });
  });

  it("shows error message when loading fails", async () => {
    mockApi.listShadowObservations.mockRejectedValue(new Error("network error"));
    mockApi.getShadowObservationSummary.mockResolvedValue(makeSummary());
    mockApi.getShadowHealth.mockResolvedValue(makeHealthyReport());

    render(<FactorShadowLab />);
    const inputs = screen.getAllByRole("spinbutton");
    await act(async () => {
      fireEvent.change(inputs[1], { target: { value: "1" } });
    });

    await waitFor(() => {
      expect(mockMessage.error).toHaveBeenCalledWith(
        expect.stringContaining("shadowLabLoadFailed"),
      );
    });
  });

  it("opens approval modal and submits request activation successfully", async () => {
    mockApi.listShadowObservations.mockResolvedValue([makeObservation()]);
    mockApi.getShadowObservationSummary.mockResolvedValue(makeSummary());
    mockApi.getShadowHealth.mockResolvedValue(makeHealthyReport());
    mockApi.requestActivation.mockResolvedValue({
      success: true,
      factor_id: 1,
      from_status: "shadow",
      to_status: "active",
      actor: "local_user",
      audit_id: 100,
      error: null,
      valid_days: 25,
    });

    render(<FactorShadowLab />);
    const inputs = screen.getAllByRole("spinbutton");
    await act(async () => {
      fireEvent.change(inputs[1], { target: { value: "1" } });
    });

    await waitFor(() => {
      expect(screen.getByText("shadowLabRequestActivation")).toBeInTheDocument();
    });

    // 点击申请激活按钮
    const requestBtn = screen.getByText("shadowLabRequestActivation").closest("button")!;
    await act(async () => {
      fireEvent.click(requestBtn);
    });

    // Modal 出现
    await waitFor(() => {
      expect(screen.getByText("shadowLab_request")).toBeInTheDocument();
    });

    // 输入原因
    const textarea = screen.getByPlaceholderText("shadowLabReasonPlaceholder");
    await act(async () => {
      fireEvent.change(textarea, { target: { value: "测试审批原因" } });
    });

    // 点击确认
    const confirmBtn = screen.getByText("shadowLabConfirm").closest("button")!;
    await act(async () => {
      fireEvent.click(confirmBtn);
    });

    await waitFor(() => {
      expect(mockApi.requestActivation).toHaveBeenCalledWith(
        expect.objectContaining({
          factor_version_id: 1,
          reason: "测试审批原因",
        }),
      );
    });

    await waitFor(() => {
      expect(mockMessage.success).toHaveBeenCalledWith("shadowLab_requestSuccess");
    });
  });

  it("warns when reason is empty on submit", async () => {
    mockApi.listShadowObservations.mockResolvedValue([makeObservation()]);
    mockApi.getShadowObservationSummary.mockResolvedValue(makeSummary());
    mockApi.getShadowHealth.mockResolvedValue(makeHealthyReport());

    render(<FactorShadowLab />);
    const inputs = screen.getAllByRole("spinbutton");
    await act(async () => {
      fireEvent.change(inputs[1], { target: { value: "1" } });
    });

    await waitFor(() => {
      expect(screen.getByText("shadowLabRequestActivation")).toBeInTheDocument();
    });

    const requestBtn = screen.getByText("shadowLabRequestActivation").closest("button")!;
    await act(async () => {
      fireEvent.click(requestBtn);
    });

    await waitFor(() => {
      expect(screen.getByText("shadowLab_request")).toBeInTheDocument();
    });

    // 不输入原因直接确认
    const confirmBtn = screen.getByText("shadowLabConfirm").closest("button")!;
    await act(async () => {
      fireEvent.click(confirmBtn);
    });

    expect(mockMessage.warning).toHaveBeenCalledWith("shadowLabReasonRequired");
    expect(mockApi.requestActivation).not.toHaveBeenCalled();
  });

  it("shows error when approval operation fails", async () => {
    mockApi.listShadowObservations.mockResolvedValue([makeObservation()]);
    mockApi.getShadowObservationSummary.mockResolvedValue(makeSummary());
    mockApi.getShadowHealth.mockResolvedValue(makeHealthyReport());
    mockApi.approveActivation.mockResolvedValue({
      success: false,
      factor_id: 1,
      from_status: null,
      to_status: "active",
      actor: "local_user",
      audit_id: null,
      error: "insufficient_valid_days",
    });

    render(<FactorShadowLab />);
    const inputs = screen.getAllByRole("spinbutton");
    await act(async () => {
      fireEvent.change(inputs[1], { target: { value: "1" } });
    });

    await waitFor(() => {
      expect(screen.getByText("shadowLabApprove")).toBeInTheDocument();
    });

    const approveBtn = screen.getByText("shadowLabApprove").closest("button")!;
    await act(async () => {
      fireEvent.click(approveBtn);
    });

    await waitFor(() => {
      expect(screen.getByText("shadowLab_approve")).toBeInTheDocument();
    });

    const textarea = screen.getByPlaceholderText("shadowLabReasonPlaceholder");
    await act(async () => {
      fireEvent.change(textarea, { target: { value: "批准" } });
    });

    const confirmBtn = screen.getByText("shadowLabConfirm").closest("button")!;
    await act(async () => {
      fireEvent.click(confirmBtn);
    });

    await waitFor(() => {
      expect(mockMessage.error).toHaveBeenCalledWith(
        expect.stringContaining("insufficient_valid_days"),
      );
    });
  });

  it("renders IC chart when observations exist", async () => {
    mockApi.listShadowObservations.mockResolvedValue([
      makeObservation({ trade_date: "2026-08-01", ic_value: 0.05 }),
      makeObservation({ id: 2, trade_date: "2026-08-02", ic_value: 0.06 }),
    ]);
    mockApi.getShadowObservationSummary.mockResolvedValue(makeSummary());
    mockApi.getShadowHealth.mockResolvedValue(makeHealthyReport());

    render(<FactorShadowLab />);
    const inputs = screen.getAllByRole("spinbutton");
    await act(async () => {
      fireEvent.change(inputs[1], { target: { value: "1" } });
    });

    await waitFor(() => {
      expect(screen.getByTestId("mock-chart")).toBeInTheDocument();
    });
  });
});
