// factor-center-chain-closure-20260902 — Task 13：Settings + ChainStepsBar 前端专项补齐（⑦ ⑧ ⑨ ⑩ 共 5 个新断言）
//
// 不与 FactorModelSettings.pipeline-ui.test.tsx / linked-columns.test.tsx /
// ChainStepsBar.test.tsx / FactorBidirectionalLinks.test.tsx 重复：
//   ⑦ ChainStepsBar Step 8「前往流水线设置」直达按钮 → navigate('/settings/pipeline') 被调用 1 次
//   ⑧ 设置页流水线区 0 集合：Select disabled（已通过 TR-8.3 class 断言；本文件加 aria 属性双保险 + value 不预选）
//   ⑨ 设置页模型表 只读「关联列」：点击行内 linked-fs-cell → navigate 实参 query 含 set_id=
//   ⑩ Usage Tab：两个子 Tab 切换 activeKey 正确变化（sets → models → sets）
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// ── antd message mock（FactorModelSettings 直接使用顶层 message.error/info；必须在此文件 mock）
const mockMessage = vi.hoisted(() => ({
  success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn(), loading: vi.fn(),
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
        modal: { confirm: vi.fn().mockResolvedValue(true) },
      }),
    },
  };
});

// ──────────────────────────────────────────────── 全局 mock（与原 tests 一致）
const OVERVIEW_BASE = {
  config: { feature_enabled: true, warehouse_path: "factor.duckdb", updated_by: "test", updated_at: null },
  feature_enabled: true,
  warehouse_error: null,
  runtime: {
    weight_mode: "manual", score_weight_mode: "manual",
    active_model_run_id: null, active_factor_set_id: null,
    updated_by: "env", fallback_reason: null,
    version: 1, updated_at: null,
  },
  health: { status: "healthy", warehouse_available: true, warehouse_path: "factor.duckdb", schema_version: "1", calc_batch_id: "batch-1", latest_bar_date: "2026-07-14", raw_tables: [], factors: [], reasons: [] },
  latest_trade_date: "2026-07-14",
  factor_coverage: [],
};

const { mockApi, mockNavigate, showToast } = vi.hoisted(() => ({
  mockApi: {
    scoringGetOverviewAsFactor: vi.fn(async (): Promise<any> => OVERVIEW_BASE),
    scoringGetFactorModelListAsFactor: vi.fn(async (): Promise<any> => ({ runtime: OVERVIEW_BASE.runtime, items: [] })),
    scoringListTasks: vi.fn(async () => [] as any[]),
    scoringListFactorSetsAsFactor: vi.fn(async () => [] as any[]),
    scoringGetTask: vi.fn(),
    scoringGetPipelineEta: vi.fn(async () => ({ avg_seconds: 0, median_seconds: 0, sample_count: 0, fallback_seconds: 1200, recommended_seconds: 1200, train_model: true, full_refresh: false })),
    scoringCreateTask: vi.fn(async () => ({ id: "task-1", task_type: "factor_pipeline", status: "queued" })),
    scoringCancelTask: vi.fn(),
    scoringUpdateSystemConfig: vi.fn(async () => ({})),
    scoringInitializeWarehouse: vi.fn(async () => ({})),
    scoringActivateModel: vi.fn(async () => ({})),
    scoringFallbackToManual: vi.fn(async () => ({})),
    scoringGetModelRelations: vi.fn(async (id: string): Promise<any> => ({ schema_version: 2, model_id: id, factors: [] })),
    scoringListFactorDefinitions: vi.fn(),
    scoringGetFactorUsage: vi.fn(),
  } as any,
  mockNavigate: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN" as const, showToast }) }));
vi.mock("../../utils/navigate", () => ({ navigate: (u: string) => mockNavigate(u) }));

import ChainStepsBar from "../factors/ChainStepsBar";
import FactorModelSettings from "../FactorModelSettings";

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.scoringGetOverviewAsFactor.mockResolvedValue(OVERVIEW_BASE);
});

// ═══════════════════════════════════════════════════════════════════════
// ⑦ ChainStepsBar Step 8 直达按钮 → navigate('/settings/pipeline')
// ═══════════════════════════════════════════════════════════════════════
describe("T13⑦：ChainStepsBar Step 8 直达按钮导航", () => {
  it("Step 8 被阻断 → 渲染 前往流水线设置 btn → click 触发 handleGoStep(8) → 父组件 navigate('/settings/pipeline')", async () => {
    // Step 8 = pipelineReady=false → 被阻断 → 渲染直达按钮 chain-btn-goto-pipeline
    const blockedAll = {
      factorWarehouseReady: true,
      anyFactorSetCreated: true,
      anyFactorSetMembersReady: true,
      anyFactorSetFrozen: true,
      anyModelTrained: true,
      anyModelValidated: true,
      decisionModeEqualsFormalActive: true,
      pipelineReady: false,
    };

    const onGoStep = vi.fn((step: number) => {
      if (step === 8) mockNavigate("/settings/pipeline");
    });

    render(<ChainStepsBar state={blockedAll} onGoStep={onGoStep} />);

    const gotoBtn = await waitFor(() => screen.getByTestId("chain-btn-goto-pipeline"));
    expect(gotoBtn).toBeInTheDocument();
    expect(gotoBtn.textContent).toContain("前往流水线设置");

    fireEvent.click(gotoBtn);
    expect(onGoStep).toHaveBeenCalledWith(8);
    expect(mockNavigate).toHaveBeenCalledWith("/settings/pipeline");
  });
});

// ═══════════════════════════════════════════════════════════════════════
// ⑧ 设置页流水线区 0 集合 → Select disabled + 断言（双保险：class + aria-disabled + 无法选中）
// ═══════════════════════════════════════════════════════════════════════
describe("T13⑧：设置页 0 集合 → Select disabled（TR-8.3 的额外断言）", () => {
  it("0 frozen sets 时 factor-set-select 有 disabled class 或 aria-disabled=true，并出现『暂无已冻结集合』文案", async () => {
    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue([]);
    render(<FactorModelSettings />);
    const select = await screen.findByTestId("factor-set-select");
    expect(select).toBeInTheDocument();

    await within(select).findByText("暂无已冻结集合");

    // antd Select disabled → classList 一定含 ant-select-disabled；
    // 此外有时会加 aria-disabled='true'；二者至少一个成立
    const hasCls = /ant-select-disabled/.test(select.className);
    const hasAria = select.getAttribute("aria-disabled") === "true";
    expect(hasCls || hasAria).toBe(true);

    // Select 内部 selection-item 应该是 null（不预选任何）
    const sel = select.querySelector(".ant-select-selection-item");
    expect(sel).toBeNull();
  });
});

// ═══════════════════════════════════════════════════════════════════════
// ⑨ 设置页模型表只读关联列 → 点击行 → navigate 实参 query 含 set_id=
// ═══════════════════════════════════════════════════════════════════════
describe("T13⑨：Settings 只读关联列点击跳转 query set_id 存在", () => {
  it("linked-fs-cell-${id} 点击 → navigate 被调用，参数字符串包含 set_id=encodeURIComponent(id)", async () => {
    // 模型通过同步字段 factor_set_link + 异步 scoringGetModelRelations 两种形式均可；
    // 为保证 click 能拿到 id，我们至少填 factor_set_link。
    const model = {
      id: "MR-T13-LINK",
      model_type: "ridge",
      status: "validated",
      rejection_reason: null,
      metrics: { validation_ic: 0.08 },
      sample_count: 10000,
      data_cutoff_at: "2026-07-14T18:00:00",
      hyperparameters: {
        factor_set_id: "FS-T13-LINK",
        factor_set_name: "T13 关联集合",
        n_members: 3,
      },
      weights: [],
      factor_set_link: {
        id: "FS-T13-LINK",
        name: "T13 关联集合",
        n_members: 3,
      },
    };
    mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({
      runtime: OVERVIEW_BASE.runtime,
      items: [model],
    });
    mockApi.scoringGetModelRelations.mockImplementation(async (mid: string) => {
      if (mid === "MR-T13-LINK") {
        return {
          schema_version: 2,
          model_id: mid,
          factor_set_id: "FS-T13-LINK",
          factor_set_name: "T13 关联集合",
          n_members: 3,
          factors: [],
          unbound_reason: null,
        };
      }
      return { schema_version: 2, model_id: mid, factors: [] };
    });

    render(<FactorModelSettings initialTrainModel={false} />);
    const cell = await waitFor(() => screen.getByTestId("linked-fs-cell-MR-T13-LINK"));
    expect(cell).toBeInTheDocument();
    // cell 内应该显示集合名 + n_members（不显示『—』）
    expect(cell.textContent).toContain("T13 关联集合");

    fireEvent.click(cell);
    expect(mockNavigate).toHaveBeenCalledTimes(1);
    const url = mockNavigate.mock.calls[0][0];
    expect(typeof url).toBe("string");
    // 必须包含 set_id=FS-T13-LINK
    expect(url).toContain("set_id=FS-T13-LINK");
    expect(url.startsWith("/factors")).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// ⑩ Usage Tab 子 Tab 切换 activeKey：sets → models → sets 面板正确切换
//   注意：不与 TR-6.4（两个子 Tab 各 ≥1 卡片）重复；本用例专注 activeKey 变化：
//     - 默认 sets（tab1 aria-selected=true）；
//     - 点击 models 标签 → 该 tab aria-selected=true；
//     - 再点 sets 标签 → sets tab aria-selected=true 回来。
// ═══════════════════════════════════════════════════════════════════════
describe("T13⑩：Usage 子 Tab 切换 activeKey 正确变化", () => {
  it("打开 Usage Drawer → 默认 sets active；切 models → models active；切回 sets → sets active", async () => {
    mockApi.scoringListFactorDefinitions.mockResolvedValue({
      items: [
        {
          code: "F-T13-USAGE",
          factor_name: "T13 切换",
          category: "value",
          coverage: 0.9,
          ic_mean: 0.03,
          latest_version: 1,
          versions: [{ version: 1, label: "v1", status: "trainable" }],
          status: "active",
        },
      ],
      total: 1, page: 1, page_size: 50,
    });
    mockApi["scoringGetFactorUsage"] = vi.fn().mockImplementation((code: string) => {
      if (code === "F-T13-USAGE") {
        return {
          code, name: "T13 切换", status: "active", lifecycle_status: "production",
          origin: "research", category: "value", is_active: true,
          description: "", active_version: 1, coverage_30d: 0.91, ic_mean_30d: 0.03,
          days_in_production: 1,
          in_factor_sets: [
            { factor_set_id: "FS-T13-U1", label: "U1", status: "frozen", role: "feature", version: 1 },
          ],
          in_models: [
            {
              model_id: "MDL-T13-U1", model_name: "U1 模型", status: "validated",
              normalized_weight: 0.5, validation_ic: 0.06, model_validation_ic: 0.07,
              activated_at: "2026-09-02T00:00:00Z",
            },
          ],
        };
      }
      return { in_factor_sets: [], in_models: [] };
    });

    const FactorLibrary = (await import("../factors/FactorLibrary")).default;
    render(
      <FactorLibrary
        onViewDetail={() => {}}
        onEditFactor={() => {}}
        onNewFactor={() => {}}
      />
    );

    await waitFor(() => expect(screen.getByTestId("btn-usage-F-T13-USAGE")).toBeInTheDocument(), { timeout: 15000 });
    fireEvent.click(screen.getByTestId("btn-usage-F-T13-USAGE"));

    await waitFor(() => expect(screen.getByTestId("usage-sub-tabs")).toBeInTheDocument(), { timeout: 15000 });

    // 定位两个 Tab 按钮（antd role=tab）
    const allTabs = screen.getAllByRole("tab");
    const tabSets = allTabs.find((t) => /所属集合|In Sets|sets/i.test(t.textContent ?? ""))!;
    const tabModels = allTabs.find((t) => /关联模型|In Models|models/i.test(t.textContent ?? ""))!;
    expect(tabSets).toBeTruthy();
    expect(tabModels).toBeTruthy();

    // (a) 默认 activeKey = sets
    expect(tabSets.getAttribute("aria-selected")).toBe("true");

    // (b) 点击 models → models active
    fireEvent.click(tabModels);
    await waitFor(() => expect(tabModels.getAttribute("aria-selected")).toBe("true"));
    expect(tabSets.getAttribute("aria-selected")).toBe("false");

    // (c) 切回 sets → sets active
    fireEvent.click(tabSets);
    await waitFor(() => expect(tabSets.getAttribute("aria-selected")).toBe("true"));
    expect(tabModels.getAttribute("aria-selected")).toBe("false");
  }, 40000);
});
