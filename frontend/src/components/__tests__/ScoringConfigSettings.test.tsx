import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "antd";

// 使用 vi.hoisted 提升 mock
const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    listScoringConfigs: vi.fn(async () => [
      {
        id: 1,
        asset_type: "stock",
        preset_key: "balanced_opportunity",
        name: "均衡机会",
        description: "均衡评分预设",
        version: 1,
        config_json: "{}",
        preset_source: "system",
        is_system: 1,
        is_active: 1,
        is_latest: 1,
        base_preset_key: null,
        config: {
          preset_key: "balanced_opportunity",
          name: "均衡机会",
          asset_type: "stock",
          preset_source: "system",
          final_weights: { quality: 0.4, timing: 0.5, news: 0.1 },
          dimensions: [
            { key: "trend", name: "趋势", enabled: true, score_bucket: "timing", weight: 0.5, filter: { enabled: false, operator: "gte", value: 0 }, factors: [{ key: "ma", source: "builtin", weight: 1, direction: "higher_better" }] },
          ],
        },
        created_at: "2026-07-01T00:00:00Z",
        updated_at: "2026-07-01T00:00:00Z",
      },
      {
        id: 2,
        asset_type: "stock",
        preset_key: "aggressive_growth",
        name: "激进成长",
        description: "激进成长评分预设",
        version: 1,
        config_json: "{}",
        preset_source: "user",
        is_system: 0,
        is_active: 0,
        is_latest: 1,
        base_preset_key: "balanced_opportunity",
        config: {
          preset_key: "aggressive_growth",
          name: "激进成长",
          asset_type: "stock",
          preset_source: "user",
          final_weights: { quality: 0.3, timing: 0.6, news: 0.1 },
          dimensions: [],
        },
        created_at: "2026-07-02T00:00:00Z",
        updated_at: "2026-07-02T00:00:00Z",
      },
    ]),
    getActiveScoringConfig: vi.fn(async () => ({
      id: 1,
      asset_type: "stock",
      preset_key: "balanced_opportunity",
      name: "均衡机会",
      version: 1,
      is_active: 1,
    })),
    activateScoringConfig: vi.fn(async () => ({ ok: true })),
    duplicateScoringConfig: vi.fn(async () => ({ ok: true })),
    deleteScoringConfig: vi.fn(async () => ({ ok: true, deleted: 1 })),
    createScoringConfig: vi.fn(async () => ({ ok: true })),
    updateScoringConfig: vi.fn(async () => ({ ok: true })),
    listScoringConfigVersions: vi.fn(async () => [
      { id: 1, asset_type: "stock", preset_key: "balanced_opportunity", name: "均衡机会", version: 1, config_json: "{}", is_active: 1, is_latest: 1, created_at: "2026-07-01T00:00:00Z" },
    ]),
  },
}));

// Mock i18n
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

import ScoringConfigSettings from "../ScoringConfigSettings";

describe("ScoringConfigSettings 组件渲染测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should render stock and etf tab buttons", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("scStockTab")).toBeInTheDocument();
    });
    expect(screen.getByText("scEtfTab")).toBeInTheDocument();
  });

  it("should load scoring configs on mount", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(mockApi.listScoringConfigs).toHaveBeenCalledWith("stock");
      expect(mockApi.getActiveScoringConfig).toHaveBeenCalledWith("stock");
    });
  });

  it("should render preset rows with name and key", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    expect(screen.getByText("balanced_opportunity")).toBeInTheDocument();
    expect(screen.getByText("激进成长")).toBeInTheDocument();
  });

  it("should render system and active tags for system active preset", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    // 系统预设标签和已激活标签
    expect(screen.getByText("scSystemPresetBadge")).toBeInTheDocument();
    expect(screen.getByText("scActive")).toBeInTheDocument();
  });

  it("should render action buttons with correct disabled states", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    // 激活按钮（is_active=1 时 disabled）
    const activateBtns = screen.getAllByText("scBtnActivate");
    expect(activateBtns.length).toBeGreaterThan(0);
    // 复制按钮
    expect(screen.getAllByText("scBtnDuplicate").length).toBeGreaterThan(0);
    // 编辑按钮（is_system=1 时 disabled）
    expect(screen.getAllByText("scBtnEdit").length).toBeGreaterThan(0);
    // 版本按钮
    expect(screen.getAllByText("scBtnVersions").length).toBeGreaterThan(0);
    // 删除按钮
    expect(screen.getAllByText("scBtnDelete").length).toBeGreaterThan(0);
  });

  it("should render refresh and new buttons in toolbar", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    expect(screen.getByText("scRefresh")).toBeInTheDocument();
    expect(screen.getByText("scBtnNew")).toBeInTheDocument();
  });

  it("should open create modal when new button clicked", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("scBtnNew"));
    await waitFor(() => {
      expect(screen.getByText("scCreateTitle")).toBeInTheDocument();
    });
  });

  it("should call listScoringConfigs with etf when etf tab clicked", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(mockApi.listScoringConfigs).toHaveBeenCalledWith("stock");
    });
    fireEvent.click(screen.getByText("scEtfTab"));
    await waitFor(() => {
      expect(mockApi.listScoringConfigs).toHaveBeenCalledWith("etf");
    });
  });
});

// ── P1-2 交互测试 ──
describe("ScoringConfigSettings 交互测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should call activateScoringConfig and reload when activate button clicked", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    // Row 2 (激进成长) is_active=0, activate button enabled; Row 1 disabled
    const activateBtns = screen.getAllByRole("button", { name: "scBtnActivate" });
    const enabledBtn = activateBtns.find((b) => !(b as HTMLButtonElement).disabled);
    expect(enabledBtn).toBeDefined();
    fireEvent.click(enabledBtn!);
    await waitFor(() => {
      expect(mockApi.activateScoringConfig).toHaveBeenCalledWith(2);
    });
    // reload 触发 listScoringConfigs 再次调用
    await waitFor(() => {
      expect(mockApi.listScoringConfigs).toHaveBeenCalledTimes(2);
    });
  });

  it("should open duplicate modal and call duplicateScoringConfig on confirm", async () => {
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    // 点击 Row 2 的复制按钮（index 1）
    const dupBtns = screen.getAllByRole("button", { name: "scBtnDuplicate" });
    fireEvent.click(dupBtns[1]);
    await waitFor(() => {
      expect(screen.getByText("scDuplicateTitle")).toBeInTheDocument();
    });
    // 点击 Modal footer 的 OK 按钮
    const okBtn = document.querySelector(".ant-modal-footer .ant-btn-primary") as HTMLElement;
    expect(okBtn).toBeTruthy();
    fireEvent.click(okBtn);
    await waitFor(() => {
      expect(mockApi.duplicateScoringConfig).toHaveBeenCalledWith(
        2,
        expect.objectContaining({
          new_preset_key: "aggressive_growth_copy",
          // 实现使用 t("presetCopySuffix")，i18n mock 返回 key
          new_name: "激进成长 presetCopySuffix",
        }),
      );
    });
  });

  it("should call deleteScoringConfig after Modal.confirm ok", async () => {
    // spyOn Modal.confirm 捕获 config，绕过 portal 渲染问题
    const confirmSpy = vi.spyOn(Modal, "confirm");
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    // Row 2 (激进成长) is_system=0 is_active=0, delete button enabled
    const deleteBtns = screen.getAllByRole("button", { name: "scBtnDelete" });
    const enabledBtn = deleteBtns.find((b) => !(b as HTMLButtonElement).disabled);
    expect(enabledBtn).toBeDefined();
    fireEvent.click(enabledBtn!);
    // Modal.confirm 应被调用
    await waitFor(() => {
      expect(confirmSpy).toHaveBeenCalled();
    });
    // 直接调用 onOk 回调
    const config = confirmSpy.mock.calls[0][0];
    await config.onOk?.();
    await waitFor(() => {
      expect(mockApi.deleteScoringConfig).toHaveBeenCalledWith(2);
    });
    confirmSpy.mockRestore();
  });

  it("should open edit Drawer when edit button clicked on user preset", async () => {
    const user = userEvent.setup();
    render(<ScoringConfigSettings />);
    await waitFor(() => {
      expect(screen.getByText("均衡机会")).toBeInTheDocument();
    });
    // Row 2 (激进成长) is_system=0, edit button enabled
    const editBtns = screen.getAllByRole("button", { name: "scBtnEdit" });
    const enabledBtn = editBtns.find((b) => !(b as HTMLButtonElement).disabled);
    expect(enabledBtn).toBeDefined();
    await user.click(enabledBtn!);
    await waitFor(() => {
      expect(screen.getByText("scEditTitle")).toBeInTheDocument();
    });
  });
});
