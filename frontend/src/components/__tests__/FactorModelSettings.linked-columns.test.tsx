// factor-center-chain-closure-20260902 — Task 7：TR-7.1~7.4
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const OV = { config: { feature_enabled: true, warehouse_path: "x" }, feature_enabled: true, warehouse_error: null,
  runtime: { weight_mode: "manual", score_weight_mode: "manual", active_model_run_id: null, updated_by: "env", fallback_reason: null, version: 1, updated_at: null },
  health: { status: "healthy", warehouse_available: true, warehouse_path: "x", schema_version: "1", calc_batch_id: "b1", latest_bar_date: "2026-07-14", raw_tables: [], factors: [], reasons: [] },
  latest_trade_date: "2026-07-14", factor_coverage: [{ factor_code: "ep_ttm", latest_trade_date: "2026-07-14", universe_symbols: 100, eligible_symbols: 90, imputed_symbols: 0, coverage: 0.9 }] };

// AC-7 常量（200+ 字），DTO 专用
const AC7 = "未关联：该历史模型没有记录训练使用的因子集合，无法确认因子来源。请重新基于已冻结因子集合训练，或执行历史数据迁移。为了避免不精确匹配导致的审计失真，当前不做自动化猜测。具体请参考『因子中心与因子模型优化方案 - 闭环验收通过说明 §11.3 迁移流程』：先运行 GET /factor-models/migration-report 得到三类统计（已关联/唯一可匹配/未知），再对 case(2) 唯一匹配项经审计签名后通过 POST /factor-models/apply-migration-candidate 写入，case(3) 未知项必须手工复核再决定是否写入，严禁脚本自动批量回填。";

const { mockApi, showToast, mockNavigate } = vi.hoisted(() => ({
  mockApi: {
    scoringGetOverviewAsFactor: vi.fn(async () => OV),
    // Explicit typing: items[] defaults to FactorModelRun shape. Otherwise empty array literal
    // `[]` is inferred as `never[]` and later mockResolvedValue calls fail TS2322.
    scoringGetFactorModelListAsFactor: vi.fn(async (): Promise<{
      runtime: typeof OV.runtime;
      items: any[];
    }> => ({ runtime: OV.runtime, items: [] })),
    scoringListTasks: vi.fn(async () => [] as any[]),
    scoringListFactorSetsAsFactor: vi.fn(async () => [] as any[]),
    scoringGetTask: vi.fn(),
    scoringGetPipelineEta: vi.fn(async () => ({ recommended_seconds: 1200, sample_count: 0, train_model: true, full_refresh: false })),
    scoringCreateTask: vi.fn(), scoringCancelTask: vi.fn(),
    scoringUpdateSystemConfig: vi.fn(async () => ({})),
    scoringInitializeWarehouse: vi.fn(async () => ({})),
    scoringActivateModel: vi.fn(async () => ({})),
    scoringFallbackToManual: vi.fn(async () => ({})),
    // Explicit signature: permit any (mid: string) implementation. Declared as
    // (...args: any[]) => any so the mock's procedure normalizer doesn't reject a
    // narrower concrete implementation bound later via mockImplementation.
    scoringGetModelRelations: vi.fn(async (..._args: any[]): Promise<any> => null as any),
  },
  showToast: vi.fn(), mockNavigate: vi.fn() as (u: string) => void,
}));
vi.mock("../../api/client", () => ({ api: mockApi }));
vi.mock("../../context/AppContext", () => ({ useApp: () => ({ locale: "zh-CN", showToast }) }));
vi.mock("../../utils/navigate", () => ({ navigate: (u: string) => mockNavigate(u) }));
import FactorModelSettings from "../FactorModelSettings";

const mkModel = (o: any = {}) => ({
  id: o.id ?? "m1", model_type: "ridge", status: "validated", rejection_reason: null,
  metrics: { validation_ic: 0.08 }, sample_count: 10000, data_cutoff_at: "2026-07-14T18:00:00",
  hyperparameters: { factor_set_name: o.name ?? "核心集", factor_set_id: o.sid ?? "FS1", n_members: o.n ?? 8 },
  weights: [], ...o,
});

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime: OV.runtime, items: [] });
  mockApi.scoringGetModelRelations.mockReset();
});

// TR-7.1: 3 模型 relations full DTO → 关联因子集合列 name/id/n 非空 ≠ "—"
it("TR-7.1 三行 relations 返回 full DTO → linked-fs-cell 三列非空，『—』不出现", async () => {
  const A = mkModel({ id: "mA", name: "动量集", sid: "FSM", n: 5 });
  const B = mkModel({ id: "mB", name: "估值集", sid: "FSV", n: 12 });
  const C = mkModel({ id: "mC", name: "成长集", sid: "FSG", n: 7 });
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime: OV.runtime, items: [A, B, C] });
  mockApi.scoringGetModelRelations.mockImplementation(async (mid: string) => {
    const s: any = { mA: A, mB: B, mC: C }[mid] || A;
    return { model_id: mid, status: s.status, factor_set_id: s.hyperparameters.factor_set_id,
      factor_set_name: s.hyperparameters.factor_set_name, n_members: s.hyperparameters.n_members,
      factors: [], data_cutoff_at: s.data_cutoff_at, created_at: null, unbound_reason: null };
  });
  render(<FactorModelSettings initialTrainModel={false} />);
  await waitFor(() => screen.getByTestId("linked-fs-cell-mA"));
  for (const id of ["mA", "mB", "mC"]) {
    expect(screen.queryByTestId(`linked-fs-cell-${id}`)).not.toBeNull();
    expect(screen.queryByTestId(`linked-fs-dash-${id}`)).toBeNull();
    const t = screen.getByTestId(`linked-fs-cell-${id}`).textContent!;
    expect(t.length).toBeGreaterThan(3); expect(t).not.toContain("—");
  }
  // 验证 relations mock 返回值三列非空（TR-7.1 rule 要求）
  const r: any = await (mockApi.scoringGetModelRelations as any).getMockImplementation()("mA");
  expect(r.factor_set_name).toBe("动量集"); expect(r.factor_set_id).toBe("FSM"); expect(r.n_members).toBe(5);
});

// TR-7.2: Drawer 6 列；null 字段 → N/A 文本不出现 0
it("TR-7.2 Drawer 6 列渲染 + coef_raw=null 显示 N/A 不出现 0 占位", async () => {
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime: OV.runtime, items: [mkModel({ id: "m72" })] });
  mockApi.scoringGetModelRelations.mockResolvedValue({
    model_id: "m72", status: "validated", factor_set_id: "FS1", factor_set_name: "核心集", n_members: 8,
    factors: [
      { factor_code: "ep_ttm",       factor_version_id: "v3", coef_raw: 0.123456, weight_norm: 0.25, validation_ic: 0.038, coverage: 0.92, role: "feature" },
      { factor_code: "roe_ttm",      factor_version_id: "v5", coef_raw: null,      weight_norm: 0.20, validation_ic: 0.042, coverage: 0.88, role: "feature" },
      { factor_code: "momentum_12m", factor_version_id: null, coef_raw: -0.0042,  weight_norm: null, validation_ic: null,  coverage: null, role: "feature" },
    ], data_cutoff_at: "2026-07-14T18:00:00", created_at: null, unbound_reason: null,
  });
  render(<FactorModelSettings initialTrainModel={false} />);
  await waitFor(() => screen.getByTestId("view-relations-m72"));
  fireEvent.click(screen.getByTestId("view-relations-m72"));
  await waitFor(() => screen.getByTestId("relations-details-table"));
  const tbl = screen.getByTestId("relations-details-table");
  ["factor_code","factor_version_id","coef_raw","weight_norm","validation_ic","coverage"].forEach(h => expect(tbl.textContent).toContain(h));
  // ≥5 个 N/A（roe coef=null + momentum 4 个 null），绝不用 0 占位
  expect(screen.queryAllByText("N/A").length).toBeGreaterThanOrEqual(5);
  const exactlyZero = tbl.innerHTML.match(/>\s*0(?:\.0+)?\s*<\//g) ?? [];
  expect(exactlyZero.length).toBe(0);
});

// TR-7.3: unbound_reason = AC7 全文 → Alert warning + 原文 + 迁移按钮
it("TR-7.3 unbound_reason=AC7(200+字) → Alert warning 出现 + 文案含原文 + 查看迁移报告按钮", async () => {
  expect(AC7.length).toBeGreaterThan(200);
  const um = { ...mkModel({ id: "mu73" }), hyperparameters: {} };
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime: OV.runtime, items: [um] });
  mockApi.scoringGetModelRelations.mockResolvedValue({
    model_id: "mu73", status: "validated", factor_set_id: null, factor_set_name: null, n_members: null,
    factors: [], data_cutoff_at: null, created_at: null, unbound_reason: AC7,
  });
  render(<FactorModelSettings initialTrainModel={false} />);
  await waitFor(() => screen.getByTestId("view-relations-mu73"));
  fireEvent.click(screen.getByTestId("view-relations-mu73"));
  const a = await waitFor(() => screen.getByTestId("relations-unbound-alert"));
  const at = a.textContent!;
  for (const s of ["没有记录训练使用的因子集合","已冻结因子集合训练","case(2) 唯一匹配","case(3) 未知项必须手工复核"]) {
    expect(at).toContain(s);
  }
  expect(at.length).toBeGreaterThan(200);
  const b = screen.getByTestId("view-migration-report");
  expect(b).toBeInTheDocument(); expect(b.textContent).toContain("查看迁移报告");
});

// TR-7.4: 点击 ↗前往因子中心 → navigate 参数精确 '/factors'
it("TR-7.4 前往因子中心按钮 → navigate('/factors') 精确命中 1 次", async () => {
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime: OV.runtime, items: [mkModel({ id: "m74" })] });
  render(<FactorModelSettings initialTrainModel={false} />);
  const btn = await waitFor(() => screen.getByTestId("goto-factor-center-global"));
  expect(btn.textContent).toContain("前往因子中心"); expect(mockNavigate).not.toHaveBeenCalled();
  fireEvent.click(btn);
  expect(mockNavigate).toHaveBeenCalledTimes(1);
  expect(mockNavigate).toHaveBeenCalledWith("/factors");
});
