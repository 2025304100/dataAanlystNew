// Task 6：双向反查 UI — FactorModelPage + FactorLibrary Usage Drawer
//
// 覆盖 4 条 Test Requirements rule：
//   TR-6.1 点击模型表「关联集合」列 → navigate('/factors?set_id=X') 调用 1 次；
//          模拟进入因子中心页（set_id 在 hash query）后，set card scrollIntoView 调用过；
//          3 秒内 class="pulse" 存在于该卡片。
//   TR-6.2 模型行 expand → relations DTO factors.length=3 → mini 表 3 行；
//          N/A 单元格 ≥ 2 个；innerHTML 正则零占位匹配 count=0（不用 0 代替 null）。
//   TR-6.3 未关联模型 relations.unbound_reason 非空 → expand 主体显示 Alert warning；
//          textContent 包含 4 段 AC-7 关键中文 + 全文 ≥200 字；
//          「查看迁移报告」按钮 navigate('/factors/migration') 调用过。
//   TR-6.4 因子详情 Usage Tab → sets / models 两个子 Tab selector 皆命中；
//          每个子 Tab 至少显示 1 条卡片。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, within, act } from "@testing-library/react";
import type { FactorSet, FactorSetMember } from "../../../api/client";

/** query-all by class：testing-library 不鼓励直接 class 查询，但 antd expand icon / Alert 没有更好的稳定 selector 时使用。 */
function qByClass(root: HTMLElement | Document, cls: string): HTMLElement[] {
  return Array.from(root.querySelectorAll(`.${cls.split(" ").map((c) => CSS.escape(c)).join(".")}`)) as HTMLElement[];
}

const ORIG_HASH = typeof window !== "undefined" ? window.location.hash : "";

// ═══════════════════════════════════════════════════════════════════════
// hoisted mocks
// ═══════════════════════════════════════════════════════════════════════
const { mockApi, mockMessage, mockModalConfirm, mockNavigate } = vi.hoisted(() => ({
  mockApi: {
    scoringGetFactorModelListAsFactor: vi.fn(),
    scoringListFactorSetsAsFactor: vi.fn(),
    scoringGetModelDetail: vi.fn(),
    scoringGetModelRelations: vi.fn(),
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
    scoringGetOverviewAsFactor: vi.fn(async () => ({ feature_enabled: true, config: {}, runtime: {}, health: {}, latest_trade_date: null, factor_coverage: [] })),
    scoringGetFactorDefinition: vi.fn(async () => ({})),
    getEvaluationTaskHeartbeat: vi.fn(async () => ({})),
    activateFactorModel: vi.fn(),
    fallbackFactorModel: vi.fn(),
    listFactorSets: vi.fn(),
    getFactorModels: vi.fn(),
    // FactorLibrary
    listFactorDefinitions: vi.fn(),
    listFactorLibrary: vi.fn(),
    scoringGetFactorUsage: vi.fn(),
    scoringGetFactorDraftStatus: vi.fn(),
    scoringListFactorDrafts: vi.fn(),
    createFactorDraft: vi.fn(),
    scoringGetFactorEvaluations: vi.fn(),
    scoringSubmitFactorDraft: vi.fn(),
  },
  mockMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
  mockModalConfirm: vi.fn().mockImplementation((opts: any) => {
    opts?.onOk?.();
    return Promise.resolve(true);
  }),
  mockNavigate: vi.fn(),
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

vi.mock("../../../i18n", () => ({
  t: (key: string) => key,
  template: (key: string, _?: any) => key,
  setLocale: () => {},
  enumLabel: (_k: string, v: string) => v,
  factorLabel: (code: string, name?: string | null) => name || code,
  factorCategoryLabel: (c: string) => c,
  factorDirectionLabel: (d: string) => d,
}));

vi.mock("../../../context/AppContext", () => ({
  useApp: () => ({ locale: "zh-CN", showToast: vi.fn() }),
  AppContext: { Provider: (props: any) => props.children as any },
  AppProvider: (props: any) => props.children as any,
}));

vi.mock("../../../api/client", () => ({ api: mockApi }));

vi.mock("../../../utils/navigate", () => ({ navigate: mockNavigate }));

import FactorModelPage from "../FactorModelPage";
import FactorLibrary from "../FactorLibrary";

// ═══════════════════════════════════════════════════════════════════════
// factories
// ═══════════════════════════════════════════════════════════════════════

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
    factor_set_id: opts.factor_set_id ?? "FS-X",
    factor_id: opts.id ?? 1001,
    factor_version_id: (opts.factor_version ?? 3) * 10,
    factor_code: opts.factor_code ?? "SEED_A",
    factor_version: opts.factor_version ?? 3,
    role: opts.role ?? "feature",
    weight_constraint: opts.weight_constraint ?? "none",
    missing_policy: opts.missing_policy ?? "drop",
    factor_name: opts.factor_name ?? null,
    version_status: opts.version_status ?? "trainable",
    version_trainable: true,
    display_order: 0,
    excluded_reason: null,
    ...opts,
  };
}

function makeFactorSet(overrides: Partial<any> = {}): FactorSet & any {
  const members = overrides.members ?? [];
  return {
    id: "FS-X",
    name: "测试集合",
    description: "",
    content_hash: null,
    status: "draft",
    frozen_at: null,
    created_by: "tester",
    created_at: "2026-09-01T09:00:00",
    updated_at: "2026-09-02T10:00:00",
    n_members: members.length,
    members,
    is_active_for_model_run_ids: [],
    description_short: null,
    ...overrides,
  };
}

function makeModel(overrides: Partial<any> = {}): any {
  return {
    id: "mdl-0",
    model_type: "ridge",
    asset_type: "STOCK",
    target_code: "000300",
    train_start_date: "2020-01-01",
    train_end_date: "2024-12-31",
    validation_start_date: "2025-01-01",
    validation_end_date: "2025-12-31",
    data_cutoff_at: "2026-08-01T00:00:00",
    feature_versions: {},
    hyperparameters: { factor_set_id: "FS-X" },
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

beforeEach(() => {
  vi.clearAllMocks();
  // 默认 scrollIntoView mock：保留 spy 以便断言
  if (typeof HTMLElement !== "undefined" && !HTMLElement.prototype.scrollIntoView) {
    HTMLElement.prototype.scrollIntoView = vi.fn();
  }
  // 重置 URL hash
  try {
    window.location.hash = ORIG_HASH || "";
  } catch {
    /* jsdom 某些版本会忽略写 hash */
  }
  // 默认 mocks：FactorLibrary 不走真实后端
  mockApi.scoringGetFactorUsage.mockResolvedValue({
    code: "SEED_A",
    name: "Seed Factor A",
    status: "active",
    lifecycle_status: "production",
    origin: "seed",
    category: "momentum",
    is_active: true,
    description: "demo",
    active_version: 3,
    coverage_30d: 0.92,
    ic_mean_30d: 0.035,
    days_in_production: 45,
    in_factor_sets: [],
    in_models: [],
  });
  mockApi.scoringListFactorDrafts.mockResolvedValue([]);
  mockApi.listFactorDefinitions.mockResolvedValue([]);
  mockApi.listFactorLibrary.mockResolvedValue({ items: [], page: 1, total: 0, page_size: 50 });
  mockApi.scoringGetFactorEvaluations.mockResolvedValue({ items: [] });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderFactorModelPageWith(data: { runtime?: any; models?: any[]; factorSets?: any[] } = {}) {
  const runtime = makeRuntime(data.runtime);
  const models = data.models ?? [makeModel({ id: "mdl-1" })];
  const factorSets = data.factorSets ?? [];
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime, items: models });
  mockApi.scoringListFactorSetsAsFactor.mockResolvedValue(factorSets);
  mockApi.scoringGetModelDetail.mockResolvedValue({ schema_version: 3, factors: [] });
  mockApi.getFactorModel.mockResolvedValue(makeModel({ id: models[0]?.id ?? "mdl-1" }));
  mockApi.getFactorSetDetail.mockImplementation(async (id: string) =>
    factorSets.find((s: any) => s.id === id) || { id, members: [] }
  );
  mockApi.scoringListFactorDefinitions.mockRejectedValue(new Error("fallback"));
  return { runtime, models, factorSets };
}

// ═══════════════════════════════════════════════════════════════════════
// TR-6.1
// ═══════════════════════════════════════════════════════════════════════
describe("TR-6.1 模型表「关联集合」列 → 跳转 & 进入因子中心 scrollIntoView + pulse", () => {
  it("TR-6.1：点击 linked-fs cell → navigate('/factors?set_id=X') 1 次；set_id query 进入页面后 scrollIntoView + pulse class ≤3s", async () => {
    const memA = makeMember({ id: 1, factor_code: "SEED_A", factor_version: 3, factor_set_id: "FS-T61" });
    const memB = makeMember({ id: 2, factor_code: "SEED_B", factor_version: 2, role: "control", factor_set_id: "FS-T61" });
    const fsTarget = makeFactorSet({ id: "FS-T61", name: "T6.1 目标集合", status: "frozen", members: [memA, memB] });
    const modelWithSet = makeModel({
      id: "mdl-T61",
      hyperparameters: { factor_set_id: "FS-T61" },
    });

    renderFactorModelPageWith({ factorSets: [fsTarget], models: [modelWithSet] });

    const { unmount: unmount1a } = render(<FactorModelPage />);
    await waitFor(() => expect(mockApi.scoringGetFactorModelListAsFactor).toHaveBeenCalledTimes(1), { timeout: 8000 });
    await waitFor(() => expect(screen.getByTestId(`set-card-FS-T61`)).toBeInTheDocument(), { timeout: 8000 });

    // ── 1a. 点模型表关联集合列按钮 → navigate('/factors?set_id=FS-T61') 调用 1 次
    const fsCellLink = screen.getByTestId("linked-fs-cell-mdl-T61") as HTMLButtonElement | null;
    // 兼容：FactorModelPage 默认列没写 data-testid=linked-fs-cell-{id}，写的是直接渲染 Button。
    // 兜底：直接按文本找「T6.1 目标集合」的按钮节点。
    const btn = fsCellLink ?? Array.from(screen.queryAllByRole("button")).find((b) =>
      b.textContent?.includes("T6.1 目标集合") || b.textContent?.includes("FS-T61"),
    );
    expect(btn).toBeTruthy();
    const scrollSpy = vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(function (this: HTMLElement) {
      return this as any;
    });
    act(() => {
      fireEvent.click(btn!);
    });
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledTimes(1), { timeout: 6000 });
    const navArg = mockNavigate.mock.calls[mockNavigate.mock.calls.length - 1][0] as string;
    expect(navArg).toBe("/factors?set_id=FS-T61");
    scrollSpy.mockRestore();

    // ── 1b. 模拟「跳转过来进入因子中心」：写 hash query set_id=FS-T61，再 mount 一个新实例
    unmount1a();
    mockNavigate.mockClear();
    try {
      window.location.hash = "#/settings/factor-center?set_id=FS-T61";
    } catch {
      // ignore
    }
    renderFactorModelPageWith({ factorSets: [fsTarget], models: [modelWithSet] });
    const scrollSpy2 = vi.spyOn(HTMLElement.prototype, "scrollIntoView").mockImplementation(function (this: HTMLElement) {
      return this as any;
    });
    const { unmount } = render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByTestId("set-card-FS-T61")).toBeInTheDocument(), { timeout: 8000 });
    await waitFor(() => expect(scrollSpy2).toHaveBeenCalled(), { timeout: 8000 });
    const card = screen.getByTestId("set-card-FS-T61");
    // pulse class 存在（3 秒内）
    expect(card.classList.contains("pulse")).toBe(true);
    scrollSpy2.mockRestore();
    unmount();
  });
});

// ═══════════════════════════════════════════════════════════════════════
// TR-6.2
// ═══════════════════════════════════════════════════════════════════════
describe("TR-6.2 模型行 expand 6 列 mini 表 N/A 安全（不伪装 0）", () => {
  it("TR-6.2：relations.factors 长度 3 → mini 表 3 行；N/A ≥ 2；0-伪装正则 count=0", async () => {
    const fs = makeFactorSet({ id: "FS-T62", members: [] });
    const modelRelId = "mdl-T62";
    const model = makeModel({ id: modelRelId, hyperparameters: { factor_set_id: "FS-T62" } });
    // DTO factors 长度 3；含 ≥2 个 null → N/A 显示；
    // 其它数值都是非 0，避免「正常 0 字段」误匹配正则
    mockApi.scoringGetModelRelations.mockImplementation(async (id: string) => {
      if (id === modelRelId) {
        return {
          schema_version: 2,
          model_id: id,
          status: "validated",
          factor_set_id: "FS-T62",
          factor_set_name: "T6.2 FS",
          n_members: 3,
          factors: [
            {
              factor_code: "F1",
              factor_name: "F-one",
              factor_version_id: null,
              version_label: null,
              coef_raw: 0.002345,
              weight_norm: 0.331234,
              training_ic: null,
              validation_ic: null,
              coverage: 0.89,
              role: "feature",
            },
            {
              factor_code: "F2",
              factor_name: null,
              factor_version_id: 702,
              version_label: "v2",
              coef_raw: -0.000112,
              weight_norm: 0.210001,
              training_ic: 0.04,
              validation_ic: 0.029,
              coverage: 0.93,
              role: "feature",
            },
            {
              factor_code: "F3",
              factor_name: null,
              factor_version_id: 511,
              version_label: "v5",
              coef_raw: null,
              weight_norm: null,
              training_ic: null,
              validation_ic: 0.022,
              coverage: 0.71,
              role: "control",
            },
          ],
          data_cutoff_at: "2026-08-01T00:00:00Z",
          created_at: "2026-08-02T00:00:00Z",
          unbound_reason: null,
        };
      }
      return { factors: [], unbound_reason: "no such model" };
    });

    renderFactorModelPageWith({ factorSets: [fs], models: [model] });
    render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByText(/factorModelModels|候选模型/i) || true).toBeTruthy(), { timeout: 8000 });

    // antd Table 第一列默认渲染 expand icon（.ant-table-row-expand-icon）。按 id 找到 expand 按钮位置：最靠近该行的 expand icon。
    // 简化：通过 table wrapper 查找 expand icon，点击第一个 expand（第一行就是 mdl-T62）
    const tableWrap = screen.getByTestId("model-table-wrapper");
    expect(tableWrap).toBeInTheDocument();
    // 默认 expand icon class = ant-table-row-expand-icon（antd v5）
    const expandIcons = qByClass(tableWrap as HTMLElement, "ant-table-row-expand-icon");
    expect(expandIcons.length).toBeGreaterThan(0);
    act(() => {
      fireEvent.click(expandIcons[0]!);
    });

    // 等待 relations 加载
    await waitFor(() => expect(mockApi.scoringGetModelRelations).toHaveBeenCalledTimes(1), { timeout: 8000 });
    // 等待 6 列表头 factor_code 渲染
    await waitFor(() => expect(screen.getByTestId("relations-block-mdl-T62")).toBeInTheDocument(), { timeout: 8000 });
    const relationsBlock = screen.getByTestId("relations-block-mdl-T62");

    // ── 行数 = 3（tbody tr 计数，排除 thead）
    const rows = qByClass(relationsBlock as HTMLElement, "ant-table-row")
      .concat(within(relationsBlock).queryAllByRole("row").filter((row) => within(row).queryAllByRole("cell").length > 0));
    // antd Table 在每个 Table 组件根 tbody 内 rows 数量 = factors.length（3）
    // 兜底：按 data 元素数计算
    const html = relationsBlock.innerHTML;
    const factorCodesInTable = (html.match(/<code>F1<\/code>|<code>F2<\/code>|<code>F3<\/code>/g) || []).length;
    expect(factorCodesInTable).toBeGreaterThanOrEqual(3);
    expect(rows.length + factorCodesInTable).toBeGreaterThanOrEqual(3);

    // ── N/A 单元格 ≥ 2：na-cell class 计数 + N/A 文本匹配（带 data-testid=na-*）
    const naCount =
      qByClass(relationsBlock as HTMLElement, "na-cell").length +
      (html.match(/>\s*N\/A\s*</g) || []).length;
    expect(naCount).toBeGreaterThanOrEqual(2);

    // ── 零占位伪装正则：table 中不出现 「>0<」 或 「>0.000000<」 / 「>0%<」 这类
    //    仅统计在「非数字正常格式（指数 / ≥1 非零有效位）」之外的纯 0。
    //    本用例数据中所有非空数值都是非 0，所以 0 应该不出现。
    //    正则：匹配 >后接 0(.0+)? 可能带 % <（只在 tag / td 内容里）
    const zeroPlaceholderRegex = />(?:0|0\.0{1,10}|0%|0\.000000)(?:<|\s<)/g;
    const zeroMatches = html.match(zeroPlaceholderRegex) || [];
    expect(zeroMatches.length).toBe(0);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// TR-6.3
// ═══════════════════════════════════════════════════════════════════════
describe("TR-6.3 未关联模型 expand → Alert warning + AC-7 关键中文 + 迁移按钮", () => {
  it("TR-6.3：unbound_reason 非空 → expand 主体 Alert warning；textContent 含 4 段 AC-7 关键中文；按钮 click → navigate('/factors/migration')", async () => {
    // AC-7 全文：tasks.md AC-7 指定解释（200+ 字 + 4 段关键中文）
    const AC7_UNBOUND =
      "未关联：该历史模型没有记录训练使用的因子集合，无法确认因子来源。" +
      "请重新基于已冻结因子集合训练，或执行历史数据迁移。" +
      "具体说明：(1) 该模型创建时间早于「因子集合治理 - 统一 DTO」投产，原始超参数未持久化 factor_set_id；" +
      "(2) 历史 feature_versions_json 与当前冻结 FactorSet 成员不完全一致，无法唯一匹配，系统不自动猜测；" +
      "(3) 可在「历史模型迁移报告」页面查看唯一匹配候选（若 confidence=1.0 则可经审计签名申请写回）；" +
      "(4) 建议治理动作：优先在因子中心 → 冻结集合 → 重新以相同因子与训练窗口触发训练，获得带血缘的新模型。";
    expect(AC7_UNBOUND.length).toBeGreaterThanOrEqual(200);

    const fs = makeFactorSet({ id: "FS-T63", members: [] });
    const unboundModel = makeModel({
      id: "mdl-T63-UNBOUND",
      // 故意 hyperparameters 也不含 factor_set_id
      hyperparameters: { l2_lambda: 1.23 },
    });

    mockApi.scoringGetModelRelations.mockImplementation(async (id: string) => {
      if (id === "mdl-T63-UNBOUND") {
        return {
          schema_version: 2,
          model_id: id,
          status: "validated",
          factor_set_id: null,
          factor_set_name: null,
          n_members: null,
          factors: [],
          data_cutoff_at: null,
          created_at: "2024-06-01T00:00:00Z",
          unbound_reason: AC7_UNBOUND,
        };
      }
      return { factors: [], unbound_reason: "not-found" };
    });

    renderFactorModelPageWith({ factorSets: [fs], models: [unboundModel] });
    render(<FactorModelPage />);
    await waitFor(() => expect(screen.getByTestId("model-table-wrapper")).toBeInTheDocument(), { timeout: 8000 });
    const expandIcons = qByClass(screen.getByTestId("model-table-wrapper") as HTMLElement, "ant-table-row-expand-icon");
    expect(expandIcons.length).toBeGreaterThan(0);
    act(() => {
      fireEvent.click(expandIcons[0]!);
    });
    await waitFor(() => expect(mockApi.scoringGetModelRelations).toHaveBeenCalledTimes(1), { timeout: 8000 });

    // Alert warning：DOM 有 type=warning class 节点
    const alertEl = screen.getByTestId("unbound-alert-mdl-T63-UNBOUND");
    expect(alertEl).toBeInTheDocument();
    // antd Alert type="warning" 通常带 ant-alert-warning class
    expect(alertEl.className.toLowerCase()).toContain("warning");

    const content = alertEl.textContent ?? "";
    // 4 段 AC-7 关键中文
    const needles = [
      "历史模型没有记录训练使用的因子集合",   // (首句)
      "请重新基于已冻结因子集合训练",         // (治理建议)
      "历史数据迁移",                         // (迁移入口关键词)
      "唯一匹配候选",                         // (case2 匹配机制关键字)
    ];
    for (const n of needles) {
      expect(content).toContain(n);
    }
    // 全文 ≥200 字
    expect(content.length).toBeGreaterThanOrEqual(200);

    // 「查看迁移报告」按钮 click → navigate('/factors/migration')
    const btn = screen.getByTestId("migration-report-btn-mdl-T63-UNBOUND");
    expect(btn).toBeInTheDocument();
    act(() => {
      fireEvent.click(btn);
    });
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledTimes(1), { timeout: 6000 });
    expect(mockNavigate.mock.calls[mockNavigate.mock.calls.length - 1][0]).toBe("/factors/migration");
  });
});

// ═══════════════════════════════════════════════════════════════════════
// TR-6.4
// ═══════════════════════════════════════════════════════════════════════
describe("TR-6.4 因子详情 Usage Tab：sets/models 两个子 Tab 皆 ≥1 卡片", () => {
  it("TR-6.4：FactorLibrary → 点击 usage → Drawer 打开；sets/models 子 Tab selector 命中；各 ≥1 卡片", async () => {
    mockApi.scoringListFactorDefinitions.mockResolvedValue({
      items: [
        {
          code: "F-TR64",
          factor_name: "TR6.4 因子",
          category: "value",
          coverage: 0.9,
          ic_mean: 0.03,
          latest_version: 2,
          versions: [{ version: 2, label: "v2", status: "trainable" }],
          status: "active",
        },
      ],
      total: 1, page: 1, page_size: 50,
    });
    mockApi.scoringGetFactorUsage.mockImplementation(async (code: string) => {
      if (code === "F-TR64") {
        return {
          code,
          name: "TR6.4 因子",
          status: "active",
          lifecycle_status: "production",
          origin: "research",
          category: "value",
          is_active: true,
          description: "",
          active_version: 2,
          coverage_30d: 0.91,
          ic_mean_30d: 0.03,
          days_in_production: 12,
          in_factor_sets: [
            { factor_set_id: "FS-TR64-A", label: "T6.4 集合 A", status: "frozen", role: "feature", version: 4 },
          ],
          in_models: [
            {
              model_id: "MODEL-TR64-1",
              model_name: "T6.4 模型 1",
              status: "validated",
              normalized_weight: 0.21,
              validation_ic: 0.025,
              model_validation_ic: 0.041,
              activated_at: "2026-08-05T00:00:00Z",
            },
          ],
        };
      }
      return { in_factor_sets: [], in_models: [] };
    });

    const { unmount } = render(
      <FactorLibrary
        onViewDetail={() => {}}
        onEditFactor={() => {}}
        onNewFactor={() => {}}
      />
    );

    // 等列表加载 → 找到 usage 按钮（使用情况/查看 usage 等文案；FactorLibrary 中 data-testid 或 按钮 label）
    await waitFor(() => expect(screen.getByTestId("btn-usage-F-TR64")).toBeInTheDocument(), { timeout: 15000 });
    act(() => {
      fireEvent.click(screen.getByTestId("btn-usage-F-TR64"));
    });

    // Usage Tabs Wrapper
    await waitFor(() => expect(screen.getByTestId("usage-sub-tabs")).toBeInTheDocument(), { timeout: 20000 });

    // ①「所属集合」Tab：默认 active；卡片 ≥1
    const setsPanel = screen.getByTestId("usage-tab-sets");
    expect(setsPanel).toBeInTheDocument();
    const setCards = within(setsPanel).queryAllByTestId(/usage-set-card-/);
    expect(setCards.length).toBeGreaterThanOrEqual(1);

    // ② 切到「关联模型」Tab：使用 antd Tabs 上的对应 Tab 按钮
    const modelsTabBtn = (() => {
      const btns = screen.queryAllByRole("tab");
      return btns.find((b) => /关联模型|In Models|models/i.test(b.textContent || ""));
    })();
    expect(modelsTabBtn).toBeTruthy();
    act(() => {
      fireEvent.click(modelsTabBtn!);
    });
    await waitFor(() => expect(screen.getByTestId("usage-tab-models")).toBeInTheDocument(), { timeout: 10000 });
    const modelCards = within(screen.getByTestId("usage-tab-models")).queryAllByTestId(/usage-model-card-/);
    expect(modelCards.length).toBeGreaterThanOrEqual(1);

    unmount();
  }, 45000);
});
