import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getAppSettings: vi.fn(),
    putAppSetting: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ api: mockApi }));

import AppSettingsSection from "../settings/AppSettingsSection";

const SWITCH_KEY = "discovery.auto_data_prep_enabled";

const settingItem = (overrides: Record<string, unknown> = {}) => ({
  key: SWITCH_KEY,
  type: "bool",
  section: "discovery",
  value: true,
  source: "default",
  default: true,
  env_var: "DISCOVERY_AUTO_DATA_PREP_ENABLED",
  label_zh: "机会扫描：数据未就绪时自动补齐",
  description_zh: "关闭后扫描不会自动拉数据。",
  updated_by: null,
  updated_at: null,
  ...overrides,
});

describe("AppSettingsSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.getAppSettings.mockResolvedValue([settingItem()]);
    mockApi.putAppSetting.mockResolvedValue({
      key: SWITCH_KEY,
      value: false,
      source: "table",
      updated_by: "settings-page:tester",
    });
  });

  it("渲染后端下发的开关与来源标注（前端不写死清单）", async () => {
    render(<AppSettingsSection />);

    await waitFor(() => {
      expect(screen.getByText("机会扫描：数据未就绪时自动补齐")).toBeInTheDocument();
    });
    expect(screen.getByText("关闭后扫描不会自动拉数据。")).toBeInTheDocument();
    // source=default → 显示"未修改，使用默认值"；同时把 env 变量名露出来
    expect(screen.getByText(/未修改，使用默认值/)).toBeInTheDocument();
    // env 变量名带全角括号渲染，用正则而不是全字匹配
    expect(
      screen.getByText(/DISCOVERY_AUTO_DATA_PREP_ENABLED/),
    ).toBeInTheDocument();
    expect(screen.getByRole("switch")).toBeInTheDocument();
  });

  it("切换开关会按 key 调用 PUT，并把来源改为「已在本页保存」", async () => {
    render(<AppSettingsSection />);
    await waitFor(() => expect(screen.getByRole("switch")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => {
      expect(mockApi.putAppSetting).toHaveBeenCalledWith(SWITCH_KEY, false);
    });
    await waitFor(() => {
      expect(screen.getByText(/已在本页保存/)).toBeInTheDocument();
    });
  });

  it("保存失败时回读后端真相，不把未存成功的乐观状态留在界面", async () => {
    // GET 先给 true；PUT 失败；重新 GET 仍是 true（后端没变）
    mockApi.getAppSettings
      .mockResolvedValueOnce([settingItem({ value: true })])
      .mockResolvedValueOnce([settingItem({ value: true, source: "default" })]);
    mockApi.putAppSetting.mockRejectedValueOnce(new Error("boom"));

    render(<AppSettingsSection />);
    await waitFor(() => expect(screen.getByRole("switch")).toBeInTheDocument());

    const switchEl = screen.getByRole("switch");
    fireEvent.click(switchEl);

    await waitFor(() => {
      expect(mockApi.putAppSetting).toHaveBeenCalledTimes(1);
    });
    // 错误可见，且标题必须是“保存失败”而不是误报“读取失败”
    await waitFor(() => {
      expect(screen.getByText("设置保存失败")).toBeInTheDocument();
    });
    // 第二次 GET 发生（回读真相），开关回到后端值 true
    // （antd Switch 是 button，状态在 aria-checked 上，不是 input.checked）
    await waitFor(() => {
      expect(mockApi.getAppSettings).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(switchEl).toHaveAttribute("aria-checked", "true");
    });
  });

  it("后端没有可配置项时给空态而不是空壳页面", async () => {
    mockApi.getAppSettings.mockResolvedValueOnce([]);

    render(<AppSettingsSection />);

    await waitFor(() => {
      expect(screen.getByText("暂无可配置项。")).toBeInTheDocument();
    });
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });
});
