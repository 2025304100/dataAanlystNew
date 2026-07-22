// WP-MSG.6：ChannelConfig 组件测试
//
// 覆盖：
// - 渠道列表渲染：名称/类型/状态正确显示
// - 测试按钮：点击调用 /test API
// - 启用/禁用开关：切换调用 PATCH API
// - 删除：点击删除 + 确认调用 DELETE API
// - 创建渠道：点击"添加渠道"打开弹窗，填写表单 + 提交调用 POST
// - 错误状态：mock reject 显示错误信息
// - 空状态：mock 返回空数组显示 channelsEmpty
// - 状态显示：各状态（unconfigured/pending_test/test_success/test_failed/enabled/disabled）正确显示
//
// 约束：
// - 不修改已稳定组件实现
// - mock fetch（requestJson），不调用真实后端
// - i18n mock：t(key) 返回 key，便于按 key 断言
// - 错误消息不暴露敏感信息（仅展示后端返回的脱敏消息）
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockRequestJson } = vi.hoisted(() => ({
  mockRequestJson: vi.fn(),
}));

// Mock i18n：t 返回 key
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

// Mock api/client：仅暴露 requestJson
vi.mock("../../../api/client", () => ({
  requestJson: mockRequestJson,
  api: {},
}));

import { ChannelConfig } from "../ChannelConfig";

/** 构造一份完整的 Channel，支持部分覆盖 */
function makeChannel(overrides: Partial<Record<string, unknown>> = {}): any {
  return {
    id: 1,
    name: "测试渠道",
    channel_type: "wxpusher",
    enabled: true,
    status: "test_success",
    config_mask_json: null,
    verified_at: "2026-07-01T10:00:00Z",
    last_test_at: "2026-07-01T10:00:00Z",
    last_test_success: true,
    last_error_code: null,
    last_error_message: null,
    created_at: "2026-07-01T10:00:00Z",
    updated_at: null,
    ...overrides,
  };
}

describe("ChannelConfig 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 彻底重置 requestJson mock：清除 Once 队列和默认实现
    mockRequestJson.mockReset();
  });

  // Modal.confirm / Popconfirm 通过 portal 渲染到 document.body，不会随组件 unmount 自动清理
  // 需要在每个测试后手动清理，避免残留的 mask 拦截后续测试的点击
  afterEach(() => {
    document.body.innerHTML = "";
  });

  // 1. 渠道列表渲染：名称/类型/状态
  it("应渲染渠道表格，包含名称/类型/状态", async () => {
    mockRequestJson.mockResolvedValue([makeChannel()]);
    render(<ChannelConfig />);
    // 等待数据加载
    await waitFor(() => {
      expect(screen.getByText("测试渠道")).toBeInTheDocument();
    });
    // 表头应包含这些列（i18n key）
    expect(screen.getAllByText("channelColumnName").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("channelColumnType").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("channelColumnStatus").length).toBeGreaterThanOrEqual(1);
    // 渠道类型标签应显示
    expect(screen.getByText("channelTypeWxPusher")).toBeInTheDocument();
    // 状态标签应显示
    expect(screen.getByText("channelStatusTestSuccess")).toBeInTheDocument();
    // 已启用渠道应显示 enabled 标签
    expect(screen.getByText("enabled")).toBeInTheDocument();
    // fetch 被调用且 URL 正确
    expect(mockRequestJson).toHaveBeenCalled();
    const firstCallUrl = String(mockRequestJson.mock.calls[0][0]);
    expect(firstCallUrl).toBe("/api/v1/notifications/channels");
  });

  // 2. 测试按钮：点击调用 /test API
  it("点击测试按钮应调用 /test API", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([makeChannel()]);
    render(<ChannelConfig />);
    await waitFor(() => {
      expect(screen.getByText("测试渠道")).toBeInTheDocument();
    });
    // 清空调用记录，便于断言 test 调用
    mockRequestJson.mockClear();
    // test 接口返回成功，refresh 返回空数组
    mockRequestJson
      .mockResolvedValueOnce({ success: true }) // test response
      .mockResolvedValueOnce([]); // refresh fetch

    // 点击测试按钮（第三方渠道才显示测试按钮）
    const testBtn = screen.getByText("test");
    await user.click(testBtn);

    // 断言 test API 被调用
    await waitFor(() => {
      const testCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) =>
          typeof url === "string" && url.endsWith("/test") && options?.method === "POST",
      );
      expect(testCalls.length).toBeGreaterThanOrEqual(1);
    });
  });

  // 3. 启用/禁用开关：切换调用 PATCH API
  it("点击启用/禁用开关应调用 PATCH API", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([makeChannel({ enabled: true })]);
    render(<ChannelConfig />);
    await waitFor(() => {
      expect(screen.getByText("测试渠道")).toBeInTheDocument();
    });
    // 清空调用记录，便于断言 patch 调用
    mockRequestJson.mockClear();
    // patch 接口返回更新后的渠道，refresh 返回空数组
    mockRequestJson
      .mockResolvedValueOnce(makeChannel({ enabled: false })) // patch response
      .mockResolvedValueOnce([]); // refresh fetch

    // 定位 Switch：表格 actions 列内的 ant-switch
    const switchEl = document.querySelector(".ant-switch") as HTMLElement;
    expect(switchEl).toBeTruthy();
    await user.click(switchEl);

    // 断言 PATCH API 被调用，且 body 包含 enabled
    await waitFor(() => {
      const patchCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) =>
          typeof url === "string" &&
          url.includes("/api/v1/notifications/channels/1") &&
          options?.method === "PATCH",
      );
      expect(patchCalls.length).toBeGreaterThanOrEqual(1);
      const body = JSON.parse(patchCalls[0][1].body);
      expect(body).toHaveProperty("enabled");
    });
  });

  // 4. 删除：点击删除 + 确认调用 DELETE API
  it("点击删除按钮并确认应调用 DELETE API", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([makeChannel()]);
    render(<ChannelConfig />);
    await waitFor(() => {
      expect(screen.getByText("测试渠道")).toBeInTheDocument();
    });
    // 清空调用记录，便于断言 delete 调用
    mockRequestJson.mockClear();
    // delete 接口返回 ok，refresh 返回空数组
    mockRequestJson
      .mockResolvedValueOnce({ ok: true }) // delete response
      .mockResolvedValueOnce([]); // refresh fetch

    // 点击删除按钮（触发 Popconfirm）
    const deleteBtn = screen.getByText("delete");
    await user.click(deleteBtn);
    // Popconfirm 出现，点击确认
    await waitFor(() => {
      const okBtn = document.querySelector(
        ".ant-popconfirm-buttons .ant-btn-primary",
      ) as HTMLElement;
      expect(okBtn).toBeTruthy();
    });
    const okBtn = document.querySelector(".ant-popconfirm-buttons .ant-btn-primary") as HTMLElement;
    await user.click(okBtn);

    // 断言 DELETE API 被调用
    await waitFor(() => {
      const deleteCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) =>
          typeof url === "string" &&
          url === "/api/v1/notifications/channels/1" &&
          options?.method === "DELETE",
      );
      expect(deleteCalls.length).toBeGreaterThanOrEqual(1);
    });
  });

  // 5. 创建渠道：点击"添加渠道"打开弹窗，填写表单 + 提交调用 POST
  it("点击添加渠道应打开弹窗，填写表单并提交应调用 POST API", async () => {
    const user = userEvent.setup();
    mockRequestJson.mockResolvedValue([makeChannel()]);
    render(<ChannelConfig />);
    await waitFor(() => {
      expect(screen.getByText("测试渠道")).toBeInTheDocument();
    });
    // 点击"添加渠道"按钮
    const addBtn = screen.getByText("addChannel");
    await user.click(addBtn);
    // 弹窗应显示（Modal 标题 createChannel）
    await waitFor(() => {
      expect(screen.getAllByText("createChannel").length).toBeGreaterThan(0);
    });
    // 填写表单：name 必填
    const nameInput = screen.getByLabelText("channelColumnName") as HTMLInputElement;
    expect(nameInput).toBeTruthy();
    await user.type(nameInput, "新渠道");
    // 切换渠道类型为 wxpusher，触发配置字段渲染
    const typeSelector = document.querySelectorAll(".ant-select-selector")[0] as HTMLElement;
    fireEvent.mouseDown(typeSelector);
    await waitFor(() => {
      const opts = document.querySelectorAll(".ant-select-item-option");
      expect(opts.length).toBeGreaterThan(0);
    });
    const opts = Array.from(document.querySelectorAll(".ant-select-item-option"));
    const wxpusherOption = opts.find((o) => o.textContent === "channelTypeWxPusher") as HTMLElement;
    expect(wxpusherOption).toBeTruthy();
    fireEvent.click(wxpusherOption);
    // 等待 wxpusher 配置字段渲染（app_token/uid 必填）
    await waitFor(() => {
      expect(screen.getByLabelText("channelConfigAppToken")).toBeInTheDocument();
    });
    // 填写 app_token 和 uid（必填字段）
    const appTokenInput = screen.getByLabelText("channelConfigAppToken") as HTMLInputElement;
    await user.type(appTokenInput, "token-value");
    const uidInput = screen.getByLabelText("channelConfigUid") as HTMLInputElement;
    await user.type(uidInput, "uid-value");

    // 点击弹窗 OK 按钮
    mockRequestJson
      .mockResolvedValueOnce(makeChannel({ id: 2, name: "新渠道" })) // create response
      .mockResolvedValueOnce([]); // refresh fetch
    const modalOkBtn = document.querySelector(".ant-modal-footer .ant-btn-primary") as HTMLElement;
    expect(modalOkBtn).toBeTruthy();
    await user.click(modalOkBtn);
    // 断言 POST API 被调用
    await waitFor(() => {
      const createCalls = mockRequestJson.mock.calls.filter(
        ([url, options]: any[]) =>
          typeof url === "string" &&
          url === "/api/v1/notifications/channels" &&
          options?.method === "POST",
      );
      expect(createCalls.length).toBeGreaterThanOrEqual(1);
      // 验证请求体包含 name 和 channel_type
      const createCall = createCalls[0];
      const body = JSON.parse(createCall[1].body);
      expect(body.name).toBe("新渠道");
      expect(body.channel_type).toBe("wxpusher");
      expect(body.config).toHaveProperty("app_token", "token-value");
      expect(body.config).toHaveProperty("uid", "uid-value");
    });
  });

  // 6. 错误状态：mock fetch reject，显示错误信息
  it("接口 reject 时应显示错误 Alert", async () => {
    mockRequestJson.mockRejectedValue(new Error("network error"));
    render(<ChannelConfig />);
    await waitFor(() => {
      // Alert 标题：notificationChannelsLoadFailed
      expect(screen.getByText("notificationChannelsLoadFailed")).toBeInTheDocument();
      // 错误描述应可见
      expect(screen.getByText("network error")).toBeInTheDocument();
    });
    // 不应渲染表格
    expect(document.querySelector(".ant-table")).not.toBeTruthy();
  });

  // 7. 空状态：mock 返回空数组显示 channelsEmpty
  it("接口返回空数组时应显示 channelsEmpty 空状态", async () => {
    mockRequestJson.mockResolvedValue([]);
    render(<ChannelConfig />);
    await waitFor(() => {
      expect(screen.getByText("channelsEmpty")).toBeInTheDocument();
    });
    // 不应渲染表格
    expect(document.querySelector(".ant-table")).not.toBeTruthy();
  });

  // 8. 状态显示：各状态正确显示对应标签
  it("应正确显示各渠道状态标签", async () => {
    const channels = [
      makeChannel({ id: 1, name: "未配置渠道", status: "unconfigured", enabled: false }),
      makeChannel({ id: 2, name: "测试中渠道", status: "pending_test", enabled: false }),
      makeChannel({ id: 3, name: "测试成功渠道", status: "test_success", enabled: false }),
      makeChannel({ id: 4, name: "测试失败渠道", status: "test_failed", enabled: false }),
      makeChannel({ id: 5, name: "已启用渠道", status: "enabled", enabled: true }),
      makeChannel({ id: 6, name: "已禁用渠道", status: "disabled", enabled: false }),
    ];
    mockRequestJson.mockResolvedValue(channels);
    render(<ChannelConfig />);
    await waitFor(() => {
      expect(screen.getByText("未配置渠道")).toBeInTheDocument();
    });
    // 各状态标签应正确显示
    expect(screen.getByText("channelStatusUnconfigured")).toBeInTheDocument();
    expect(screen.getByText("channelStatusPendingTest")).toBeInTheDocument();
    expect(screen.getByText("channelStatusTestSuccess")).toBeInTheDocument();
    expect(screen.getByText("channelStatusTestFailed")).toBeInTheDocument();
    expect(screen.getByText("channelStatusEnabled")).toBeInTheDocument();
    expect(screen.getByText("channelStatusDisabled")).toBeInTheDocument();
    // 只有 enabled=true 的渠道才显示 enabled 标签（仅"已启用渠道"一项）
    const enabledTags = screen.getAllByText("enabled");
    expect(enabledTags.length).toBe(1);
  });

  // 附加：in_app 渠道不显示测试按钮（约束验证）
  it("in_app 渠道不应显示测试按钮", async () => {
    mockRequestJson.mockResolvedValue([
      makeChannel({ id: 1, name: "站内渠道", channel_type: "in_app" }),
    ]);
    render(<ChannelConfig />);
    await waitFor(() => {
      expect(screen.getByText("站内渠道")).toBeInTheDocument();
    });
    // in_app 渠道不显示测试按钮
    expect(screen.queryByText("test")).not.toBeInTheDocument();
    // 但应显示编辑/删除按钮
    expect(screen.getByText("edit")).toBeInTheDocument();
    expect(screen.getByText("delete")).toBeInTheDocument();
  });

  // 附加：加载中显示 Spin
  it("加载中应显示 Spin 加载动画", async () => {
    // 让 fetch 永不 resolve，保持 loading 状态
    mockRequestJson.mockImplementation(() => new Promise(() => {}));
    render(<ChannelConfig />);
    await waitFor(() => {
      const spin = document.querySelector(".ant-spin");
      expect(spin).toBeTruthy();
    });
  });
});
