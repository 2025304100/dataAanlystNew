// WP2.7：LocalFavoritesMigration 组件测试（WP2.5 本地收藏迁移工具）
//
// 覆盖：
// - 无 ic_favorites 时不显示迁移弹窗
// - 已迁移标记存在时不显示弹窗（即使有 ic_favorites）
// - 检测到 ic_favorites 显示弹窗，含"将导入 N/已存在 N/无效 N"统计
// - 点击"开始迁移"调用 POST /batch-import
// - 迁移成功后记录 ic_favorites_migrated_v1（时间戳）
// - 迁移成功后不删除 ic_favorites（保留回退期）
// - 重复执行不产生重复项（已迁移后再次检测不显示弹窗）
// - 迁移失败显示错误信息
//
// 约束：
// - 不修改已稳定组件实现
// - mock fetch（requestJson），不调用真实后端
// - mock localStorage（jsdom 默认提供，每个用例前清理）
// - 幂等性测试：迁移可重复执行且不产生重复项
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// 使用 vi.hoisted 提升 mock，使其可在 vi.mock factory 内引用
const { mockRequestJson } = vi.hoisted(() => ({
  mockRequestJson: vi.fn(),
}));

// Mock i18n：t 返回 key，template 返回拼接后的字符串
vi.mock("../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, params: Record<string, string | number> = {}) =>
    key.replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? "")),
  DOT: " | ",
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

// Mock api/client：仅暴露 requestJson（LocalFavoritesMigration 唯一调用入口）
vi.mock("../../api/client", () => ({
  requestJson: mockRequestJson,
  api: {},
}));

import { LocalFavoritesMigration } from "../opportunity/LocalFavoritesMigration";

/**
 * 默认后端响应工厂：根据 URL 返回对应 mock 数据。
 * - GET /api/v1/watchlists：返回含 core 名单的列表
 * - GET /api/v1/watchlists/{id}/observations?limit=1000（无 status）：active 观察项
 * - GET /api/v1/watchlists/{id}/observations?status=archived：archived 观察项
 * - POST /api/v1/watchlists/{id}/observations/batch-import：迁移结果
 */
function defaultApiImpl(
  overrides: {
    watchlists?: any[];
    activeObservations?: any[];
    archivedObservations?: any[];
    importResult?: any;
  } = {},
): (url: string, options?: any) => Promise<any> {
  const watchlists = overrides.watchlists ?? [{ id: 1, name: "core", list_type: "watch", description: null, created_at: "2026-01-01T00:00:00Z" }];
  const activeObservations = overrides.activeObservations ?? [];
  const archivedObservations = overrides.archivedObservations ?? [];
  const importResult = overrides.importResult ?? { imported: 3, existing: 0, failed: 0, errors: [] };
  return async (url: string, options?: any) => {
    if (url === "/api/v1/watchlists") {
      return watchlists;
    }
    if (url.includes("/observations/batch-import")) {
      return importResult;
    }
    if (url.includes("/observations?status=archived")) {
      return archivedObservations;
    }
    if (url.includes("/observations?")) {
      return activeObservations;
    }
    return [];
  };
}

describe("LocalFavoritesMigration 组件测试", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // 默认 mock：含 core 名单、无现有观察项、迁移结果 3 全导入
    mockRequestJson.mockImplementation(defaultApiImpl());
  });

  afterEach(() => {
    localStorage.clear();
  });

  // 1. 无 ic_favorites 时不显示弹窗
  it("localStorage 无 ic_favorites 时不应显示迁移弹窗", async () => {
    render(<LocalFavoritesMigration />);
    // 等待 useEffect 触发的 checkMigrationNeeded 完成
    await new Promise((resolve) => setTimeout(resolve, 50));
    // 弹窗标题不应出现
    expect(screen.queryByText("icMigrationTitle")).not.toBeInTheDocument();
    // 不应调用后端
    expect(mockRequestJson).not.toHaveBeenCalled();
  });

  // 2. 已迁移标记存在时不显示弹窗
  it("localStorage 有 ic_favorites_migrated_v1 时即使存在 ic_favorites 也不显示弹窗", async () => {
    localStorage.setItem("ic_favorites", JSON.stringify([100, 101, 102]));
    localStorage.setItem("ic_favorites_migrated_v1", new Date().toISOString());
    render(<LocalFavoritesMigration />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText("icMigrationTitle")).not.toBeInTheDocument();
    // 因已迁移，不应再调用后端查询
    expect(mockRequestJson).not.toHaveBeenCalled();
  });

  // 3. 检测到 ic_favorites 显示迁移弹窗（含统计信息）
  it("检测到 ic_favorites 应显示弹窗并展示将导入/已存在/无效统计", async () => {
    localStorage.setItem("ic_favorites", JSON.stringify([100, 101, 102]));
    // core 已有 1 项（symbol_id=100），所以 stats 应为：toImport=2, existing=1, invalid=0
    mockRequestJson.mockImplementation(
      defaultApiImpl({
        activeObservations: [
          { watchlist_item_id: 10, watchlist_id: 1, symbol_id: 100, status: "watching" },
        ],
      }),
    );
    render(<LocalFavoritesMigration />);
    // 弹窗应出现
    await waitFor(() => {
      expect(screen.getByText("icMigrationTitle")).toBeInTheDocument();
    });
    // 应展示总数 3（icFavoritesMigrationDesc 模板含 count）
    // template("icFavoritesMigrationDesc", { count: 3 }) => "icFavoritesMigrationDesc"（mock 返回 key）
    // 但实际 mock template 会保留 key，仅替换 {count}
    // 应出现 stats 文案
    expect(screen.getByText(/icMigrationStatsToImport/)).toBeInTheDocument();
    expect(screen.getByText(/icMigrationStatsExisting/)).toBeInTheDocument();
    expect(screen.getByText(/icMigrationStatsInvalid/)).toBeInTheDocument();
    // 应有"开始迁移"按钮
    expect(screen.getByText("icMigrationConfirm")).toBeInTheDocument();
    // 应有"稍后"按钮
    expect(screen.getByText("icMigrationLater")).toBeInTheDocument();
  });

  // 4. 点击"开始迁移"调用 POST /batch-import
  it("点击开始迁移应调用 POST /batch-import 接口", async () => {
    localStorage.setItem("ic_favorites", JSON.stringify([100, 101, 102]));
    render(<LocalFavoritesMigration />);
    await waitFor(() => {
      expect(screen.getByText("icMigrationConfirm")).toBeInTheDocument();
    });
    // 清空调用记录，便于断言 batch-import 调用
    mockRequestJson.mockClear();
    mockRequestJson.mockImplementation(defaultApiImpl());
    // 点击"开始迁移"
    fireEvent.click(screen.getByText("icMigrationConfirm"));
    // 应调用 batch-import（POST 方法）
    await waitFor(() => {
      const batchImportCall = mockRequestJson.mock.calls.find(
        ([url, options]: any[]) => typeof url === "string" && url.includes("/observations/batch-import") && options?.method === "POST",
      );
      expect(batchImportCall).toBeTruthy();
    });
    // 验证请求 body 包含 watchlist_id 和 items
    const batchImportCall = mockRequestJson.mock.calls.find(
      ([url, options]: any[]) => typeof url === "string" && url.includes("/observations/batch-import"),
    );
    const options = batchImportCall?.[1] as any;
    expect(options?.headers?.["Content-Type"]).toBe("application/json");
    const body = JSON.parse(options?.body ?? "{}");
    expect(body.watchlist_id).toBe(1);
    expect(Array.isArray(body.items)).toBe(true);
    expect(body.items.length).toBe(3);
    // 每项应有 origin_type=legacy_manual_unknown
    expect(body.items.every((it: any) => it.origin_type === "legacy_manual_unknown")).toBe(true);
  });

  // 5. 迁移成功后记录 ic_favorites_migrated_v1（时间戳）
  it("迁移成功后应在 localStorage 写入 ic_favorites_migrated_v1 时间戳", async () => {
    localStorage.setItem("ic_favorites", JSON.stringify([100, 101, 102]));
    render(<LocalFavoritesMigration />);
    await waitFor(() => {
      expect(screen.getByText("icMigrationConfirm")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("icMigrationConfirm"));
    // 等待迁移完成
    await waitFor(() => {
      expect(localStorage.getItem("ic_favorites_migrated_v1")).not.toBeNull();
    });
    // 应为有效的 ISO 时间戳
    const migrated = localStorage.getItem("ic_favorites_migrated_v1");
    expect(migrated).toBeTruthy();
    const ts = new Date(migrated!);
    expect(ts.getTime()).not.toBeNaN();
    // 应显示成功信息
    expect(screen.getByText("icMigrationSuccess")).toBeInTheDocument();
    // 应出现"完成"按钮
    expect(screen.getByText("icMigrationDone")).toBeInTheDocument();
  });

  // 6. 迁移成功后不删除 ic_favorites（回退期约束）
  it("迁移成功后不应删除 ic_favorites（保留回退期）", async () => {
    const favorites = JSON.stringify([100, 101, 102]);
    localStorage.setItem("ic_favorites", favorites);
    render(<LocalFavoritesMigration />);
    await waitFor(() => {
      expect(screen.getByText("icMigrationConfirm")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("icMigrationConfirm"));
    await waitFor(() => {
      expect(localStorage.getItem("ic_favorites_migrated_v1")).not.toBeNull();
    });
    // 关键约束：ic_favorites 应仍然存在
    expect(localStorage.getItem("ic_favorites")).toBe(favorites);
  });

  // 7. 重复执行不产生重复项（迁移后再检测不显示弹窗）
  it("迁移成功后再次挂载不应再次显示弹窗（幂等）", async () => {
    localStorage.setItem("ic_favorites", JSON.stringify([100, 101, 102]));
    const { unmount } = render(<LocalFavoritesMigration />);
    await waitFor(() => {
      expect(screen.getByText("icMigrationConfirm")).toBeInTheDocument();
    });
    // 第一次迁移
    fireEvent.click(screen.getByText("icMigrationConfirm"));
    await waitFor(() => {
      expect(localStorage.getItem("ic_favorites_migrated_v1")).not.toBeNull();
    });
    // 卸载组件
    unmount();
    // 清除已记录的 mock 调用
    mockRequestJson.mockClear();
    // 再次挂载：因已迁移，不应再显示弹窗
    render(<LocalFavoritesMigration />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    // 弹窗不应出现
    expect(screen.queryByText("icMigrationTitle")).not.toBeInTheDocument();
    // 不应再调用后端（已迁移短路）
    expect(mockRequestJson).not.toHaveBeenCalled();
  });

  // 8. 迁移失败显示错误信息
  it("batch-import 接口 reject 时应显示错误信息", async () => {
    localStorage.setItem("ic_favorites", JSON.stringify([100, 101, 102]));
    render(<LocalFavoritesMigration />);
    await waitFor(() => {
      expect(screen.getByText("icMigrationConfirm")).toBeInTheDocument();
    });
    // mock batch-import 失败：先正常返回 watchlists，然后 batch-import 抛错
    mockRequestJson.mockImplementation(async (url: string, options?: any) => {
      if (url === "/api/v1/watchlists") {
        return [{ id: 1, name: "core", list_type: "watch", description: null, created_at: "2026-01-01T00:00:00Z" }];
      }
      if (url.includes("/observations/batch-import")) {
        throw new Error("network error");
      }
      return [];
    });
    fireEvent.click(screen.getByText("icMigrationConfirm"));
    // 应显示错误信息（来自 message.error 或 Alert）
    await waitFor(() => {
      // 弹窗内应出现 error Alert
      const errorAlert = document.querySelector(".ant-alert-error");
      expect(errorAlert).toBeTruthy();
    });
    // 不应写入迁移完成标记
    expect(localStorage.getItem("ic_favorites_migrated_v1")).toBeNull();
  });
});
