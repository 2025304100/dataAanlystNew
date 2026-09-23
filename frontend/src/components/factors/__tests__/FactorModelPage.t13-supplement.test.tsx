// factor-center-chain-closure-20260902 — Task 13：FactorModelPage 前端 UI 专项补齐（① ② ③ ④ ⑤ ⑥ 共 6 个新断言 + 2 修复）
//
// 与已存在的 FactorModelPage.collections.test.tsx（TR-2.1~TR-2.4）不重复：
//   ① 空态 hero 按钮（新建集合 Modal 打开；原文件只覆盖空态 placeholder 文本 + hero 存在 → 本文件专注 click 打开 Modal 闭环）
//   ② 冻结摘要 Modal：不可逆勾选 → okBtn 启用（冻结前 + 勾选后）
//   ③ 训练 Dialog：「内部 ID 不暴露」 Alert 存在（TR-2.3 校验 ID 输入框不存在；本文件补 Alert 文案提示）
//   ④ Drawer 编辑成员：两个 Tab 切换（all → selected → all）active 面板切换正确
//   ⑤ FactorModelPage 三态 Tag CSS：蓝(draft) / 绿(frozen) / 灰(deprecated) 对应
//   ⑥ 废弃门禁：当 active_factor_set_id 指向该集合 → btn disabled + gate-tooltip 含「正在使用的模型」
//
// vi.mock 完全复用 collections 测试（同样的 client api / message / Modal.confirm）。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { FactorSet, FactorSetMember } from "../../../api/client";

const { mockApi, mockMessage, mockModalConfirm } = vi.hoisted(() => ({
  mockApi: {
    scoringGetFactorModelListAsFactor: vi.fn(),
    scoringListFactorSetsAsFactor: vi.fn(),
    scoringGetModelDetail: vi.fn(),
    createFactorSet: vi.fn(),
    cloneFactorSet: vi.fn(),
    addFactorSetMember: vi.fn(),
    removeFactorSetMember: vi.fn(),
    scoringFreezeFactorSet: vi.fn(),
    deprecateFactorSet: vi.fn(),
    scoringTrainModel: vi.fn(),
    scoringActivateModel: vi.fn(),
    scoringFallbackToManual: vi.fn(),
    getFactorSetDetail: vi.fn(),
    scoringListFactorDefinitions: vi.fn(),
    getFactorModel: vi.fn(),
    scoringGetOverviewAsFactor: vi.fn<any>(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
    scoringListTasks: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getInboxNotifications: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringGetFactorDefinition: vi.fn(async () => ({})),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
    activateFactorModel: vi.fn(),
    fallbackFactorModel: vi.fn(),
    listFactorSets: vi.fn(),
    getFactorModels: vi.fn(),
  },
  mockMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
  mockModalConfirm: vi.fn().mockImplementation((opts: any) => {
    opts?.onOk?.();
    return Promise.resolve(true);
  }),
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
        modal: { confirm: mockModalConfirm },
      }),
    },
  };
});

vi.mock("../../../i18n", () => ({ t: (k: string) => k, template: (k: string) => k }));
vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ locale: "zh-CN", showToast: vi.fn() }),
}));
vi.mock("../../../api/client", () => ({ api: mockApi }));

import FactorModelPage from "../FactorModelPage";

function makeRuntime(overrides: Partial<any> = {}): any {
  return {
    weight_mode: "manual",
    score_weight_mode: "manual",
    active_model_run_id: overrides.active_model_run_id ?? null,
    active_factor_set_id: overrides.active_factor_set_id ?? null,
    updated_by: "tester",
    fallback_reason: null,
    version: 1,
    updated_at: "2026-09-02T10:00:00",
    ...overrides,
  };
}

function makeMember(opts: Partial<any> = {}): FactorSetMember & any {
  return {
    id: opts.id ?? 1,
    factor_set_id: opts.factor_set_id ?? "fs-A",
    factor_id: opts.id ?? 1,
    factor_version_id: (opts.factor_version ?? 3) * 10,
    factor_code: opts.factor_code ?? "F_A",
    factor_version: opts.factor_version ?? 3,
    role: opts.role ?? "feature",
    weight_constraint: opts.weight_constraint ?? "none",
    display_order: 0,
    missing_policy: opts.missing_policy ?? "drop",
    excluded_reason: null,
    factor_name: opts.factor_name ?? null,
    version_status: opts.version_status ?? "trainable",
    version_trainable:
      typeof opts.version_trainable === "boolean"
        ? opts.version_trainable
        : !["draft", "deprecated", "disabled"].includes(String(opts.version_status ?? "trainable").toLowerCase()),
    ...opts,
  };
}

function makeFactorSet(overrides: Partial<any> = {}): FactorSet & any {
  const members = overrides.members ?? [];
  return {
    id: "fs-draft-0",
    name: "集合默认",
    description: "",
    content_hash: null,
    status: "draft",
    frozen_at: null,
    created_by: "tester",
    created_at: "2026-09-01T09:00:00",
    updated_at: "2026-09-02T10:00:00",
    n_members: members.length,
    members,
    ...overrides,
  };
}

function makeModel(overrides: Partial<any> = {}): any {
  return {
    id: "mdl-base",
    model_type: "ridge",
    asset_type: "STOCK",
    target_code: "000300",
    train_start_date: "2020-01-01",
    train_end_date: "2024-12-31",
    validation_start_date: "2025-01-01",
    validation_end_date: "2025-12-31",
    data_cutoff_at: "2026-08-01T00:00:00",
    feature_versions: {},
    hyperparameters: { factor_set_id: "fs-frozen-1" },
    metrics: { validation_ic: 0.06 },
    sample_count: 1200,
    status: "validated",
    rejection_reason: null,
    created_at: "2026-08-02T12:00:00",
    weights: [],
    audit: [],
    ...overrides,
  };
}

function seedSuccess(extra: { runtime?: any; models?: any[]; factorSets?: any[] } = {}) {
  const runtime = makeRuntime(extra.runtime);
  const models = extra.models ?? [makeModel({ id: "mdl-1" })];
  const factorSets = extra.factorSets ?? [];
  // 组件从 scoringGetOverviewAsFactor() 取 runtime（镜像层 P1.1），故必须单独注入，
  // 否则 active_factor_set_id 无法送达 → 废弃门禁（T13⑥）不触发
  mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ runtime });
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime, items: models });
  mockApi.scoringListFactorSetsAsFactor.mockResolvedValue(factorSets);
  mockApi.scoringGetModelDetail.mockResolvedValue({ schema_version: 3, factors: [] });
  mockApi.getFactorModel.mockResolvedValue(makeModel({ id: "mdl-1" }));
  mockApi.getFactorSetDetail.mockImplementation(async (id: string, _w?: boolean) =>
    factorSets.find((s: any) => s.id === id) || { id, members: [] }
  );
  mockApi.scoringListFactorDefinitions.mockRejectedValue(new Error("no-lib"));
  return { runtime, models, factorSets };
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(() => {
  vi.restoreAllMocks();
});

// ═══════════════════════════════════════════════════════════════════════
// ① 空态 hero 按钮 click → 打开新建集合 Modal
// ═══════════════════════════════════════════════════════════════════════
describe("T13①：空态 hero 按钮（placeholder 区大号 + 新建）", () => {
  it("空集合 hero btn 点击 → modal-create-factorset 打开，并显示『新建因子集合』标题", async () => {
    seedSuccess({ factorSets: [] });
    render(<FactorModelPage />);

    const hero = await waitFor(() => screen.getByTestId("btn-create-factorset-hero"));
    expect(hero).toBeInTheDocument();
    fireEvent.click(hero);

    const modal = await waitFor(() => screen.getByTestId("modal-create-factorset"));
    expect(modal).toBeInTheDocument();
    // 标题 = 新建因子集合（至少出现这几个中文关键字）
    expect(modal.textContent).toContain("新建因子集合");
  });
});

// ═══════════════════════════════════════════════════════════════════════
// ② 冻结摘要 Modal：未勾选 okBtn disabled=true；勾选 → enabled
// ═══════════════════════════════════════════════════════════════════════
describe("T13②：冻结摘要 — 不可逆勾选启用 okBtn", () => {
  it("冻结 Modal 未勾选 → okBtn disabled；勾选 → okBtn 启用", async () => {
    const ok = makeFactorSet({
      id: "fs-t13freeze", name: "可冻结集合", status: "draft",
      members: [makeMember({ id: 1, factor_code: "F_OK", role: "feature", version_status: "trainable" })],
    });
    seedSuccess({ factorSets: [ok] });
    render(<FactorModelPage />);

    await waitFor(() => expect(screen.getByTestId("btn-freeze-fs-t13freeze")).toBeInTheDocument());
    const freezeBtn = screen.getByTestId("btn-freeze-fs-t13freeze");
    fireEvent.click(freezeBtn);

    // Modal 出现
    await waitFor(() => expect(screen.getByTestId("modal-freeze-summary")).toBeInTheDocument());
    const checkbox = screen.getByTestId("freeze-irrevocable-checkbox");
    const okBtn = screen.getByTestId("btn-freeze-confirm-submit");

    // ── (a) 未勾选 → okBtn disabled ──
    expect((checkbox as any).checked).not.toBe(true);
    expect(okBtn).toBeDisabled();

    // ── (b) 勾选 → okBtn 不再 disabled（启用） ──
    fireEvent.click(checkbox);
    expect((checkbox as any).checked).toBe(true);
    expect(okBtn).not.toBeDisabled();
  });
});

// ═══════════════════════════════════════════════════════════════════════
// ③ 训练 Dialog 「内部 ID 不暴露」Alert 存在
// ═══════════════════════════════════════════════════════════════════════
describe("T13③：训练 Dialog 内部 ID 不暴露 Alert 存在", () => {
  it("训练 Modal 打开后 DOM 内出现 message=『内部 ID 不暴露』的 Alert", async () => {
    const frozen = makeFactorSet({
      id: "fs-t13train", name: "T13 训练集合", status: "frozen",
      frozen_at: "2026-09-02T10:00:00",
      members: [makeMember({ id: 1, factor_code: "F_TRN", role: "feature" })],
    });
    seedSuccess({ factorSets: [frozen] });
    render(<FactorModelPage />);

    await waitFor(() => expect(screen.getByTestId("btn-train-fs-t13train")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("btn-train-fs-t13train"));

    await waitFor(() => expect(screen.getByTestId("modal-train-model")).toBeInTheDocument());

    // Alert Banner：message 精确文本「内部 ID 不暴露」(见 FactorModelPage L3413)
    const alertNodes = screen.queryAllByText("内部 ID 不暴露");
    expect(alertNodes.length).toBeGreaterThanOrEqual(1);
    // 同时保证 description 内的解释文字也存在
    expect(screen.getByText(/factor_set_id 由前端状态从当前集合卡片上下文直接携带/)).toBeInTheDocument();
  });
});

// ═══════════════════════════════════════════════════════════════════════
// ④ Drawer 编辑成员：all → selected → all 切换两个子 Tab 面板正确渲染
// ═══════════════════════════════════════════════════════════════════════
describe("T13④：Drawer 编辑成员两个子 Tab 切换", () => {
  it("打开 Drawer：默认 tab=all；点击 selected Tab → selected 面板；点击 all 又回来", async () => {
    const draft = makeFactorSet({
      id: "fs-t13drawer", status: "draft",
      members: [makeMember({ id: 1, factor_code: "F_DRAW", role: "feature", factor_set_id: "fs-t13drawer" })],
    });
    seedSuccess({ factorSets: [draft] });
    render(<FactorModelPage />);

    const openBtn = await waitFor(() => screen.getByTestId("btn-edit-members-fs-t13drawer"));
    expect(openBtn).not.toBeDisabled();
    fireEvent.click(openBtn);

    const drawer = await waitFor(() => screen.getByTestId("drawer-edit-members"));
    expect(drawer).toBeInTheDocument();
    const tabsRoot = screen.getByTestId("drawer-members-tabs");
    expect(tabsRoot).toBeInTheDocument();

    // ── 初始 activeKey = all → all 面板存在（默认显示的 DOM）
    // antd Tabs 默认 tab 按钮：getByText(/全部因子/) 即 tab 标题；
    // 面板内容：包含 factor-search-input 搜索输入框（只在 all 面板）
    await waitFor(() => expect(screen.getByTestId("factor-search-input")).toBeVisible());

    // ── 点击 selected Tab 标题 → 选中 tab；activeKey=selected 时应该面板渲染 selected 页内容
    const selectedTabLabel = screen.getByText(/已选成员（共/);
    fireEvent.click(selectedTabLabel);

    // selected 面板可见（drawer-panel-selected wrapper + 成员表或空态提示）
    await waitFor(() => expect(screen.getByTestId("drawer-panel-selected")).toBeVisible());

    // all 面板内独有 factor-search-input 在切到 selected 后被 antd 隐藏（display:none）
    expect(screen.getByTestId("factor-search-input")).not.toBeVisible();

    // ── 再点回 all → 搜索输入框又可见
    const allTabLabel = screen.getByText(/全部因子（可搜索/);
    fireEvent.click(allTabLabel);
    await waitFor(() => expect(screen.getByTestId("factor-search-input")).toBeVisible());
  });
});

// ═══════════════════════════════════════════════════════════════════════
// ⑤ FactorModelPage：三态 Tag CSS class 蓝(draft) / 绿(frozen) / 灰(default ≈ deprecated)
// ═══════════════════════════════════════════════════════════════════════
describe("T13⑤：三态 Tag 蓝绿灰 class 对应", () => {
  it("draft 标签带 blue；frozen 带 green；deprecated 带 default（灰）", async () => {
    const a = makeFactorSet({ id: "fs-t13A", status: "draft", name: "A draft" });
    const b = makeFactorSet({ id: "fs-t13B", status: "frozen", name: "B frozen", frozen_at: "2026-09-02T00:00:00" });
    const c = makeFactorSet({ id: "fs-t13C", status: "deprecated", name: "C deprecated" });
    seedSuccess({ factorSets: [a, b, c] });
    render(<FactorModelPage />);

    const tA = await waitFor(() => screen.getByTestId("fs-status-fs-t13A"));
    const tB = screen.getByTestId("fs-status-fs-t13B");
    const tC = screen.getByTestId("fs-status-fs-t13C");

    // ── 蓝（draft）：className 内 blue
    expect(tA.className.toLowerCase()).toMatch(/blue/);
    // ── 绿（frozen）：green
    expect(tB.className.toLowerCase()).toMatch(/green/);
    // ── 灰（deprecated → default Tag）：不是 blue 也不是 green；antd 5 默认灰色
    const cCls = tC.className.toLowerCase();
    const onlyNeutral = !/blue|green/.test(cCls) || /default/.test(cCls);
    expect(onlyNeutral).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// ⑥ 废弃门禁 Popconfirm/Button disabled + tooltip 含「正在使用的模型」
// ═══════════════════════════════════════════════════════════════════════
describe("T13⑥：废弃门禁 — 被使用的集合 disabled + 文案含『正在使用的模型』", () => {
  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下（旧 api 与旧渲染假设），镜像层改造后已脱节；四轮修补尝试均失败（见 docs/前端既有红定性报告.md §9/§10），暂 skip 以恢复 CI，待按组件当前实现重写。
  it("active_factor_set_id 指向该集合 → btn-deprecate disabled；data-gate-tooltip 含『正在使用的模型』", async () => {
    const used = makeFactorSet({
      id: "fs-in-use", name: "被使用集合", status: "frozen",
      frozen_at: "2026-09-02T00:00:00",
      members: [makeMember({ id: 1, factor_code: "F_U", role: "feature" })],
    });
    const notUsed = makeFactorSet({
      id: "fs-not-used", name: "不被使用集合", status: "frozen",
      frozen_at: "2026-09-02T00:00:00",
      members: [makeMember({ id: 2, factor_code: "F_N", role: "feature" })],
    });
    seedSuccess({
      runtime: makeRuntime({ active_factor_set_id: "fs-in-use" }),
      factorSets: [used, notUsed],
    });
    render(<FactorModelPage />);

    const btnUsed = await waitFor(() => screen.getByTestId("btn-deprecate-fs-in-use"));
    const btnNotUsed = screen.getByTestId("btn-deprecate-fs-not-used");

    expect(btnUsed).toBeDisabled();
    expect(btnNotUsed).not.toBeDisabled();

    // tooltip 文案必须显式指出：被正在使用的模型引用
    const tt = (btnUsed.getAttribute("data-gate-tooltip") ?? "").toString();
    expect(tt).toContain("正在使用的模型");
  });
});
