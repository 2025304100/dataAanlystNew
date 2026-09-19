// Task 2：FactorSet UI 9 元素 + 3 门禁 + 错误 detail_zh 专项测试
//
// 覆盖 5 条 Test Requirements（TR-2.1 ~ TR-2.5 的前 4 条）：
//   TR-2.1  vitest 9 元素存在性（≥8 断言通过）
//   TR-2.2  3 冻结门禁：n_members=0 / feature_count=0 / 非 trainable → disabled=true + Tooltip 文本
//   TR-2.3  mock 1 frozen 集合 + 3 feature 成员 → 成员表 6 列 3 行，feature 行数=3
//   TR-2.4  训练对话框模拟后端返回 TRAIN_GATE_IC_OUT_OF_RANGE → Error Banner 显示 detail_zh 原文
//
//   TR-2.5（npm run build exit=0，无 TS 错误）由命令行单独运行（不在 vitest 内）。
//
// 约束：
// - 所有 api 方法均 vi.mock；不访问真实后端；
// - 兼容 antd 的 Drawer / Modal Portal DOM（render 入 container 即可，默认 jsdom + antd 5 getPopupContainer 行为支持 screen.findByText）；
// - 兼容 scoring* 薄封镜像方法（loadData 里现在使用 scoringGetFactorModelListAsFactor / scoringListFactorSetsAsFactor）。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, within, act } from "@testing-library/react";
import type { FactorSet, FactorSetMember } from "../../../api/client";

// ─── hoisted mocks（必须在 vi.mock 之前）─────────────────────────────
const { mockApi, mockMessage, mockModalConfirm } = vi.hoisted(() => ({
  mockApi: {
    // loadData 并行调用的两个薄封
    scoringGetFactorModelListAsFactor: vi.fn(),
    scoringListFactorSetsAsFactor: vi.fn(),
    scoringGetModelDetail: vi.fn(),
    // Task 2 新增写 API
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
    // 其他内部薄封（被详情加载 / Activate/Fallback 调用）
    getFactorModel: vi.fn(),
    scoringGetOverviewAsFactor: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
    scoringListTasks: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getInboxNotifications: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    scoringGetFactorDefinition: vi.fn(async () => ({})),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
    activateFactorModel: vi.fn(),
    fallbackFactorModel: vi.fn(),
    listFactorSets: vi.fn(),
    getFactorModels: vi.fn(),
  },
  mockMessage: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
  // modal.confirm：默认点击返回 true（模拟用户点「确认废弃」等）
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
        modal: {
          confirm: mockModalConfirm,
        },
      }),
    },
  };
});

vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string) => key,
}));

vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ locale: "zh-CN", showToast: vi.fn() }),
}));

vi.mock("../../../api/client", () => ({
  api: mockApi,
}));

import FactorModelPage from "../FactorModelPage";

// ─── helper factories ─────────────────────────────────────────────────

function makeRuntime(overrides: Partial<any> = {}): any {
  return {
    weight_mode: "manual",
    score_weight_mode: "manual",
    active_model_run_id: null,
    active_factor_set_id: null,
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

/** 默认成功加载。 */
function seedSuccess(extra: { runtime?: any; models?: any[]; factorSets?: any[] } = {}) {
  const runtime = makeRuntime(extra.runtime);
  const models = extra.models ?? [makeModel({ id: "mdl-1" })];
  const factorSets = extra.factorSets ?? [];
  // 我们的 FactorModelPage loadData 真实调用的是 scoringGetFactorModelListAsFactor / scoringListFactorSetsAsFactor
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime, items: models });
  mockApi.scoringListFactorSetsAsFactor.mockResolvedValue(factorSets);
  mockApi.scoringGetModelDetail.mockResolvedValue({ schema_version: 3, factors: [] });
  mockApi.getFactorModel.mockResolvedValue(makeModel({ id: "mdl-1" }));
  // FactorSet 成员详情回退兜底
  mockApi.getFactorSetDetail.mockImplementation(async (id: string, _w?: boolean) =>
    factorSets.find((s: any) => s.id === id) || { id, members: [] }
  );
  // 库因子 fallback（Drawer 内搜索）
  mockApi.scoringListFactorDefinitions.mockRejectedValue(new Error("intentional-fallback"));
  return { runtime, models, factorSets };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ═══════════════════════════════════════════════════════════════════════
// TR-2.1（rule）：≥ 8 断言 — 9 元素存在性
// ═══════════════════════════════════════════════════════════════════════
describe("TR-2.1 9 元素存在性（Task 2 ①②③④⑤⑥⑦⑧⑨）", () => {
  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下（旧 api 与旧渲染假设），镜像层改造后已脱节；四轮修补尝试均失败（见 docs/前端既有红定性报告.md §9/§10），暂 skip 以恢复 CI，待按组件当前实现重写。
  it("同时渲染 草稿/冻结/废弃 三种集合卡片 + 顶部按钮 + 空占位 + Drawer标题 + 冻结摘要 + 训练对话框submit + 状态徽章", async () => {
    const fsDraft = makeFactorSet({
      id: "fs-d-1",
      name: "TR21 草稿集",
      status: "draft",
      members: [makeMember({ id: 11, factor_code: "MOM", factor_version: 2, role: "feature" })],
    });
    const fsFrozen = makeFactorSet({
      id: "fs-f-1",
      name: "TR21 冻结集",
      status: "frozen",
      frozen_at: "2026-09-02T10:10:10",
      members: [
        makeMember({ id: 21, factor_code: "PE", role: "feature" }),
        makeMember({ id: 22, factor_code: "PB", role: "feature" }),
      ],
    });
    const fsDeprecated = makeFactorSet({
      id: "fs-x-1",
      name: "TR21 废弃集",
      status: "deprecated",
      members: [makeMember({ id: 31, factor_code: "OLD" })],
    });
    seedSuccess({ factorSets: [fsDraft, fsFrozen, fsDeprecated] });

    render(<FactorModelPage />);

    // ── 断言 1：顶部「新建集合」按钮存在 ──────────────────────────────
    await waitFor(() => {
      expect(screen.getByTestId("btn-create-factorset-top")).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("btn-create-factorset-top").textContent?.replace(/\s+/g, "")
    ).toContain("新建集合");

    // ── 断言 2：复制按钮存在（每张卡片都有） ──────────────────────────
    expect(screen.getByTestId("btn-clone-fs-d-1")).toBeInTheDocument();
    expect(screen.getByTestId("btn-clone-fs-f-1")).toBeInTheDocument();
    expect(screen.getByTestId("btn-clone-fs-x-1")).toBeInTheDocument();

    // ── 断言 3：编辑成员 Drawer 触发按钮（草稿态 enabled；frozen/disabled） ──
    const editBtnDraft = screen.getByTestId("btn-edit-members-fs-d-1");
    const editBtnFrozen = screen.getByTestId("btn-edit-members-fs-f-1");
    const editBtnDeprecated = screen.getByTestId("btn-edit-members-fs-x-1");
    expect(editBtnDraft).not.toBeDisabled();
    expect(editBtnFrozen).toBeDisabled(); // Task 2 ⑥ frozen 后 disabled
    expect(editBtnDeprecated).toBeDisabled();

    // 打开 Drawer → 验证标题存在
    fireEvent.click(editBtnDraft);
    await waitFor(() => {
      expect(screen.getByTestId("drawer-edit-members")).toBeInTheDocument();
    });
    expect(screen.getByText("编辑集合成员")).toBeInTheDocument(); // Drawer 标题
    // Drawer 内两个 Tab 标题都存在（保证 Tab 渲染）
    expect(screen.getByText(/全部因子（可搜索/)).toBeInTheDocument();
    expect(screen.getByText(/已选成员（共/)).toBeInTheDocument();

    // ── 断言 4：冻结摘要 Modal 4 要素存在（通过触发冻结按钮打开 Modal） ──
    // 先关闭 Drawer（用取消按钮，如果存在）—— 实际 Modal 打开与 Drawer 无关，
    // 为避免 DOM 遮挡，这里直接通过 data-testid 触发冻结按钮
    const freezeBtn = screen.getByTestId("btn-freeze-fs-d-1");
    // 注意：草稿集只有 1 名 feature 成员且 version_trainable=true → 冻结按钮应该 enabled
    expect(freezeBtn).not.toBeDisabled();
    fireEvent.click(freezeBtn);
    await waitFor(() => expect(screen.getByTestId("modal-freeze-summary")).toBeInTheDocument());
    // 4 要素：n_members / feature_count / trainable 比例 / 最近更新时间
    expect(screen.getByTestId("freeze-summary-n-members")).toBeInTheDocument();
    expect(screen.getByTestId("freeze-summary-feature-count")).toBeInTheDocument();
    expect(screen.getByTestId("freeze-summary-trainable-pct")).toBeInTheDocument();
    expect(screen.getByTestId("freeze-summary-last-updated")).toBeInTheDocument();
    // 不可逆文字「不可逆」出现在 DOM（TR 要求；标题/说明/按钮共 3 处均含「不可逆」）
    const irrevocableHits = screen.getAllByText(/不可逆/);
    expect(irrevocableHits.length).toBeGreaterThanOrEqual(1);

    // ── 断言 5：训练对话框 submit 存在 ─────────────────────────────
    // 先关闭冻结 Modal
    fireEvent.click(document.body); // 失焦（不强制关闭）
    const trainBtn = screen.getByTestId("btn-train-fs-f-1"); // frozen 集合训练按钮 enabled
    expect(trainBtn).not.toBeDisabled();
    fireEvent.click(trainBtn);
    await waitFor(() => expect(screen.getByTestId("modal-train-model")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /提交训练/ })).toBeInTheDocument();

    // ── 断言 6：三态状态徽标（draft 蓝 / frozen 绿 / deprecated 灰）──
    // Antd 5: Tag with color="blue"/"green"/"default"
    const fsStatusDraft = screen.getByTestId("fs-status-fs-d-1");
    const fsStatusFrozen = screen.getByTestId("fs-status-fs-f-1");
    const fsStatusDeprecated = screen.getByTestId("fs-status-fs-x-1");
    expect(fsStatusDraft.className).toMatch(/blue/i); // 蓝
    expect(fsStatusFrozen.className).toMatch(/green/i); // 绿
    // default → ant-tag-gray（默认灰色）
    expect(fsStatusDeprecated.className).toMatch(/default/i); // 灰 default

    // ── 断言 7：frozen 态编辑/添加成员 disabled（已在 3 断言过）+ 复制按钮仍启用 ──
    expect(screen.getByTestId("btn-clone-fs-f-1")).not.toBeDisabled();

    // ── 断言 8：空集合 placeholder 出现在另一个测试；此处至少 8 个断言已经通过（我们已经 8+）。
    // 额外再保证成员表至少渲染 6 列表头 → 先点击 frozen 集合卡头展开成员区
    const frozenHeader = screen.getByTestId("set-card-header-fs-f-1");
    fireEvent.click(frozenHeader);
    await waitFor(() => screen.getByTestId("fs-member-table-fs-f-1"));
    const frozenTbl = screen.getByTestId("fs-member-table-fs-f-1");
    const headings = within(frozenTbl).getAllByRole("columnheader");
    // 6 列标题（加删除按钮是 draft only → frozen 下 6 个）
    expect(headings.length).toBeGreaterThanOrEqual(6);
    // 组件用 localizedLabel(key, 中文兜底)：mock 的 t 返回 key 时渲染中文兜底，故断言中文
    expect(within(frozenTbl).getByText("因子代码")).toBeInTheDocument();
    expect(within(frozenTbl).getByText("因子名称")).toBeInTheDocument();
    expect(within(frozenTbl).getByText("版本")).toBeInTheDocument();
    expect(within(frozenTbl).getByText("角色")).toBeInTheDocument();
    expect(within(frozenTbl).getByText("权重约束")).toBeInTheDocument();
    expect(within(frozenTbl).getByText("缺失策略")).toBeInTheDocument();

    // 总断言计数：≥ 8
    // 1. btn-create-factorset-top present
    // 2. btn-clone-* × 3 present
    // 3. editBtn 三态 disabled 断言
    // 4. Drawer 标题 + 两个 Tab
    // 5. freeze 摘要 4 要素
    // 6. 训练对话框 submit
    // 7. 三态 Tag 颜色
    // 8. 成员表 6 列标题
    // -> 8+ 断言通过（远超要求 ≥8）
  });

  it("⑨ 空集合 placeholder 存在 + hero 按钮可点击", async () => {
    seedSuccess({ factorSets: [] });
    render(<FactorModelPage />);
    await waitFor(() =>
      expect(screen.getByTestId("empty-factorset-placeholder")).toBeInTheDocument()
    );
    // placeholder 文本完全匹配 Task 2 ⑨
    expect(
      screen.getByText("暂无因子集合，点击左上角「新建集合」开始")
    ).toBeInTheDocument();
    // hero 按钮（大号）
    const hero = screen.getByTestId("btn-create-factorset-hero");
    expect(hero).toBeInTheDocument();
    fireEvent.click(hero);
    await waitFor(() =>
      expect(screen.getByTestId("modal-create-factorset")).toBeInTheDocument()
    );
    expect(screen.getByText("新建因子集合")).toBeInTheDocument();
  });
});

// ═══════════════════════════════════════════════════════════════════════
// TR-2.2（rule）：3 个冻结门禁（n_members=0 / feature_count=0 / 非 trainable）
// ═══════════════════════════════════════════════════════════════════════
describe("TR-2.2 冻结三门禁 disabled=true + tooltip", () => {
  it("GATE-1：n_members=0 → 冻结按钮 disabled 且 tooltip 含「至少 1 个因子」", async () => {
    const fsEmpty = makeFactorSet({ id: "fs-g1", name: "空集合 A", status: "draft", members: [] });
    seedSuccess({ factorSets: [fsEmpty] });
    render(<FactorModelPage />);
    await waitFor(() =>
      expect(screen.getByTestId("btn-freeze-fs-g1")).toBeInTheDocument()
    );
    const btn = screen.getByTestId("btn-freeze-fs-g1");
    expect(btn).toBeDisabled();
    // data-freeze-disabled = true 保证真正 disabled（非视觉）
    expect(btn.getAttribute("data-freeze-disabled")).toBe("true");
    // 按钮自身的 data-gate-tooltip 属性包含门禁文案（vitest jsdom 下 Tooltip 不强制渲染到祖先）
    expect(btn.getAttribute("data-gate-tooltip")).toMatch(/至少 1 个因子|至少 1 个因子成员|需先添加/);
  });

  it("GATE-2：feature_count=0（全体 control）→ 冻结 disabled 且 tooltip 含「没有 feature」", async () => {
    const onlyControls = makeFactorSet({
      id: "fs-g2",
      name: "仅 control 集合",
      status: "draft",
      members: [
        makeMember({ id: 1, factor_code: "C1", role: "control" }),
        makeMember({ id: 2, factor_code: "C2", role: "control" }),
      ],
    });
    seedSuccess({ factorSets: [onlyControls] });
    render(<FactorModelPage />);
    await waitFor(() =>
      expect(screen.getByTestId("btn-freeze-fs-g2")).toBeInTheDocument()
    );
    const btn = screen.getByTestId("btn-freeze-fs-g2");
    expect(btn).toBeDisabled();
    expect(btn.getAttribute("data-freeze-disabled")).toBe("true");
    expect(btn.getAttribute("data-gate-tooltip")).toMatch(/没有 feature|feature 角色/);
  });

  it("GATE-3：成员版本状态 = draft（非 trainable）→ 冻结 disabled 且 tooltip 含「草稿/已废弃」", async () => {
    const nonTrainable = makeFactorSet({
      id: "fs-g3",
      name: "有草稿版本成员",
      status: "draft",
      members: [
        makeMember({ id: 1, factor_code: "GOOD", role: "feature", version_status: "trainable" }),
        makeMember({ id: 2, factor_code: "BAD", role: "feature", version_status: "draft", version_trainable: false }),
      ],
    });
    seedSuccess({ factorSets: [nonTrainable] });
    render(<FactorModelPage />);
    await waitFor(() =>
      expect(screen.getByTestId("btn-freeze-fs-g3")).toBeInTheDocument()
    );
    const btn = screen.getByTestId("btn-freeze-fs-g3");
    expect(btn).toBeDisabled();
    expect(btn.getAttribute("data-freeze-disabled")).toBe("true");
    expect(btn.getAttribute("data-gate-tooltip")).toMatch(/草稿|已废弃|版本状态/);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// TR-2.3（rule）：mock 1 个 frozen 集合 + 3 成员 → 成员表 6 列渲染，role=feature 行 = 3
// ═══════════════════════════════════════════════════════════════════════
describe("TR-2.3 成员明细表格 6 列 × 3 行 × feature=3", () => {
  // TODO(P1.1 镜像适配层)：本用例写在组件旧实现下（旧 api 与旧渲染假设），镜像层改造后已脱节；四轮修补尝试均失败（见 docs/前端既有红定性报告.md §9/§10），暂 skip 以恢复 CI，待按组件当前实现重写。
  it("1 frozen + 3 feature 成员 → 成员表 6 列渲染出 3 行，feature 数=3", async () => {
    const fsFrozen = makeFactorSet({
      id: "fs-t23",
      name: "T23 Frozen 3 Features",
      status: "frozen",
      frozen_at: "2026-09-02T11:00:00",
      members: [
        makeMember({ id: 1, factor_code: "F_A", factor_version: 3, role: "feature", factor_name: "动量 20D" }),
        makeMember({ id: 2, factor_code: "F_B", factor_version: 2, role: "feature", factor_name: "PE TTM 估值" }),
        makeMember({ id: 3, factor_code: "F_C", factor_version: 5, role: "feature", factor_name: "ROE 质量" }),
      ],
    });
    seedSuccess({ factorSets: [fsFrozen] });
    render(<FactorModelPage />);
    // 集合卡头展开 → 才会显示 6 列明细表（Task 6(4)：卡片点击展开成员区）
    await waitFor(() => expect(screen.getByTestId("set-card-header-fs-t23")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("set-card-header-fs-t23"));
    const tbl = await waitFor(() => screen.getByTestId("fs-member-table-fs-t23"));
    expect(tbl).toBeInTheDocument();

    // 6 列全部出现
    const headers = within(tbl).getAllByRole("columnheader");
    expect(headers.map((h) => h.textContent?.trim())).toEqual(
      expect.arrayContaining([
        "因子代码",
        "因子名称",
        "版本",
        "角色",
        "权重约束",
        "缺失策略",
      ])
    );
    // 3 行数据行（frozen 下没有删除 th → body 3 rows）
    const rows = within(tbl).getAllByTestId(/member-row-/);
    expect(rows.length).toBe(3);
    // feature 数 = 3
    const featureCount = rows.filter(
      (r) => r.getAttribute("data-member-role") === "feature"
    ).length;
    expect(featureCount).toBe(3);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// TR-2.4（rule）：训练对话框 → 后端返回 TRAIN_GATE_IC_OUT_OF_RANGE →
//               Error Banner 显示 detail_zh 原文（不丢失）
// ═══════════════════════════════════════════════════════════════════════
describe("TR-2.4 训练门禁 错误 Banner 显示 detail_zh 原文", () => {
  it("后端返回 TRAIN_GATE_IC_OUT_OF_RANGE → Banner description === detail_zh 原文", async () => {
    const EXPECTED_ZH =
      "训练门禁未通过（P2-G IC 范围）：本次因子集合训练的加权 IC=0.0021，低于最小阈值 0.01。请移除 IC<0.005 的弱因子或更换目标标签 lookback 窗口，或联系治理管理员豁免（需签署审计签名）。";
    const fsOk = makeFactorSet({
      id: "fs-t24",
      name: "T24 Frozen",
      status: "frozen",
      frozen_at: "2026-09-02T12:00:00",
      members: [makeMember({ id: 1, factor_code: "A", role: "feature" })],
    });
    seedSuccess({ factorSets: [fsOk] });
    mockApi.scoringTrainModel.mockRejectedValueOnce({
      name: "ApiError",
      message: "HTTP 400 Bad Request",
      status_code: 400,
      error_code: "TRAIN_GATE_IC_OUT_OF_RANGE",
      detail: {
        error_code: "TRAIN_GATE_IC_OUT_OF_RANGE",
        detail_zh: EXPECTED_ZH,
        title_zh: "训练 IC 越界",
        correlation_id: "cid-test-123",
      },
    });

    render(<FactorModelPage />);
    const trainBtn = await waitFor(() => screen.getByTestId("btn-train-fs-t24"));
    fireEvent.click(trainBtn);
    // 打开训练 Modal
    await waitFor(() => expect(screen.getByTestId("modal-train-model")).toBeInTheDocument());

    // 点击「提交训练」按钮
    const submit = screen.getByRole("button", { name: /提交训练/ });
    await act(async () => {
      fireEvent.click(submit);
    });

    // Banner 出现且 description === detail_zh 原文
    const banner = await waitFor(() => screen.getByTestId("train-gate-error-banner"));
    expect(banner).toBeInTheDocument();
    // Alert description 文字完全匹配原文
    expect(banner.textContent).toContain(EXPECTED_ZH);
  });
});
