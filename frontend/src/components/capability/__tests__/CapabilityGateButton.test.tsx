// WP-S-FIX.5: CapabilityGateButton 组件测试
//
// 覆盖：
// - ready 状态正常可点击
// - blocked 状态禁用样式 + Tooltip
// - degraded 可点击 + 警告图标
// - 点击 blocked 按钮打开 Modal
// - Modal 展示前置条件列表
// - 推荐操作按钮跳转
//
// 约束：
// - 不修改组件代码
// - mock i18n（t 返回 key）、AppContext、antd
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockContext } = vi.hoisted(() => ({
  mockContext: {
    getCapability: vi.fn(),
    setActiveTab: vi.fn(),
    runSync: vi.fn(),
    loadCapabilities: vi.fn(),
  },
}));

// Mock i18n：t 返回 key
vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
}));

// Mock antd：保留组件库
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return { ...actual };
});

// Mock AppContext
vi.mock("../../../context/AppContext", () => ({
  useApp: () => mockContext,
}));

import { CapabilityGateButton } from "../CapabilityGateButton";
import type { CapabilityItem } from "../../../types";

/** 构造一个 CapabilityItem */
function makeCapability(overrides: Partial<CapabilityItem> = {}): CapabilityItem {
  return {
    key: "market_data",
    label: "基础数据",
    status: "ready",
    reason_code: null,
    user_message: "数据就绪",
    prerequisites: [],
    recommended_actions: [],
    data_cutoff_at: null,
    last_checked_at: "2026-07-22T00:00:00Z",
    ...overrides,
  };
}

describe("CapabilityGateButton WP-S-FIX.5 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockContext.getCapability.mockReturnValue(null); // 默认 ready（未加载）
  });

  // Modal.confirm 通过 portal 渲染到 document.body，需要每个测试后清理
  afterEach(() => {
    document.body.innerHTML = "";
  });

  // 1. ready 状态正常可点击
  // 注意：antd Button 无 icon 时会对两个中文字符自动插入空格（"同步" → "同 步"），
  // 因此用 getByRole("button") 定位而非 getByText
  it("ready 状态时按钮正常可点击", async () => {
    mockContext.getCapability.mockReturnValue(makeCapability({ status: "ready" }));
    const onClick = vi.fn();
    render(
      <CapabilityGateButton capabilityKey="market_data" onClick={onClick}>
        同步
      </CapabilityGateButton>
    );
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(onClick).toHaveBeenCalled());
  });

  // 2. blocked 状态禁用 + Tooltip
  it("blocked 状态时按钮显示禁用样式和 Tooltip", () => {
    mockContext.getCapability.mockReturnValue(
      makeCapability({ status: "blocked", user_message: "标的库为空" })
    );
    render(
      <CapabilityGateButton capabilityKey="market_data" onClick={vi.fn()}>
        同步
      </CapabilityGateButton>
    );
    // 按钮存在且 opacity 为 0.5（模拟 disabled）
    const button = screen.getByText("同步").closest("button")!;
    expect(button.style.opacity).toBe("0.5");
    expect(button.style.cursor).toBe("not-allowed");
  });

  // 3. degraded 可点击 + 警告图标
  it("degraded 状态时按钮可点击并显示警告图标", async () => {
    mockContext.getCapability.mockReturnValue(
      makeCapability({ status: "degraded", user_message: "数据过期" })
    );
    const onClick = vi.fn();
    render(
      <CapabilityGateButton capabilityKey="market_data" onClick={onClick}>
        同步
      </CapabilityGateButton>
    );
    fireEvent.click(screen.getByText("同步"));
    await waitFor(() => expect(onClick).toHaveBeenCalled());
  });

  // 4. 点击 blocked 按钮打开 Modal
  it("点击 blocked 按钮时打开 Modal 而非触发 onClick", async () => {
    mockContext.getCapability.mockReturnValue(
      makeCapability({
        status: "blocked",
        user_message: "标的库为空",
        prerequisites: [
          { key: "symbols_empty", label: "添加标的", satisfied: false, detail: "无标的" },
        ],
      })
    );
    const onClick = vi.fn();
    render(
      <CapabilityGateButton capabilityKey="market_data" onClick={onClick}>
        同步
      </CapabilityGateButton>
    );
    fireEvent.click(screen.getByText("同步"));
    // onClick 不应被调用
    expect(onClick).not.toHaveBeenCalled();
    // Modal 应显示 user_message
    await waitFor(() => {
      expect(screen.getByText("标的库为空")).toBeInTheDocument();
    });
  });

  // 5. Modal 展示前置条件列表
  it("Modal 展示前置条件列表", async () => {
    mockContext.getCapability.mockReturnValue(
      makeCapability({
        status: "blocked",
        user_message: "不可用",
        prerequisites: [
          { key: "p1", label: "前置条件1", satisfied: true, detail: "已满足" },
          { key: "p2", label: "前置条件2", satisfied: false, detail: "未满足" },
        ],
      })
    );
    render(
      <CapabilityGateButton capabilityKey="market_data" onClick={vi.fn()}>
        同步
      </CapabilityGateButton>
    );
    fireEvent.click(screen.getByText("同步"));
    await waitFor(() => {
      expect(screen.getByText("前置条件1")).toBeInTheDocument();
      expect(screen.getByText("前置条件2")).toBeInTheDocument();
    });
  });

  // 6. 推荐操作按钮跳转
  it("推荐操作 redirect 类型点击时调用 setActiveTab", async () => {
    mockContext.getCapability.mockReturnValue(
      makeCapability({
        status: "blocked",
        user_message: "不可用",
        recommended_actions: [
          { label: "去配置", action_type: "redirect", target: "settings", reason: null },
        ],
      })
    );
    render(
      <CapabilityGateButton capabilityKey="market_data" onClick={vi.fn()}>
        同步
      </CapabilityGateButton>
    );
    fireEvent.click(screen.getByText("同步"));
    await waitFor(() => {
      expect(screen.getByText("去配置")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("去配置"));
    expect(mockContext.setActiveTab).toHaveBeenCalledWith("settings");
  });
});
