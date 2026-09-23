import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { t } from "../../../i18n";
import { factorMiningApi } from "../../../api/factorMining";
import { dataMirrorApi } from "../../../api/dataMirror";
import { MINING_STEPS } from "../../../types/mining";
import type {
  MiningCandidate,
  MiningFrequency,
  MiningRun,
  MiningRunCreate,
  MiningStepKey,
  SplitBudget,
} from "../../../types/mining";
import type { FieldDataMode, MiningField } from "./wizard/step3/fieldTypes";
import type { MiningLockStatus } from "./wizard/step4/evoTypes";
import { DEFAULT_EVO_CONFIG } from "./wizard/step4/evoTypes";
import MiningEvoParamStep from "./wizard/step4/MiningEvoParamStep";
import MiningFieldStep from "./wizard/step3/MiningFieldStep";
import MiningPoolStep from "./wizard/step1/MiningPoolStep";
import MiningTimeTargetStep from "./wizard/step2/MiningTimeTargetStep";
import F1ExperiencePage from "./experience/F1ExperiencePage";
import TemplateConfigPage from "./config/TemplateConfigPage";
import ResultGotoFactorModelButton from "./result/ResultGotoFactorModelButton";
import type { MiningResultContext } from "./result/resultTypes";
import FactorMiningRunTrack from "./wizard/step5/FactorMiningRunTrack";
import PerfProbePanel from "./wizard/step5/PerfProbePanel";
import type {
  CurvePoint,
  PerfProbeGeneration,
  RunProgress,
  RunStatus,
  TopFactor,
} from "./wizard/step5/runTypes";

/**
 * 因子挖掘壳（设计 §9.2）：子页签「向导 / 批次列表」。
 *
 * **A4 接线（2026-09-20，经用户授权）**：在 G5 的 5 步可点通之上，
 * step1~step4 把配置（候选池快照 id / 时间与切分 / 字段 / 进化参数）经
 * `onConfig` 回调上提到本壳（`wizardConfig`），step4 提交时由本壳组装 payload
 * 调 `POST /factor-mining/runs`（真实后端 A3），成功后流转第 5 步并
 * 5s 轮询 `GET /runs/{id}` 更新进化跟踪；step5 的三操作（中断/停止/放弃）
 * 也接到真实接口。
 *
 * 批次列表：按契约调 `GET /factor-mining/runs`（A3 已实现），三态容错
 * （加载中 / 空 / 错误），接口异常不崩页。
 *
 * 文案全部走 t()，禁止硬编码中文（需求 §3.4 / 开发 §6）。
 */
const STEP_LABEL_KEYS: Record<MiningStepKey, string> = {
  pool: "miningStepPool",
  "time-target": "miningStepTimeTarget",
  field: "miningStepField",
  evolution: "miningStepEvolution",
  run: "miningStepRun",
};

type ShellTab = "wizard" | "runs" | "experience" | "templates";

/** 批次状态 → 语义标签（未知状态按中性展示，不改写后端取值） */
function runStatusChipClass(status: string | null | undefined): string {
  const v = String(status ?? "").toLowerCase();
  if (v === "succeeded" || v === "converged" || v === "done") {
    return "mining-chip mining-chip--success";
  }
  if (v === "running" || v === "validating" || v === "queued") {
    return "mining-chip mining-chip--info";
  }
  if (v === "failed" || v === "cancelled") return "mining-chip mining-chip--danger";
  if (v === "paused" || v === "invalidated") return "mining-chip mining-chip--warn";
  return "mining-chip";
}

/** 批次错误码 → 用户可读说明（未知码原样透出，便于排查） */
function runErrorHint(code: string | null | undefined): string | undefined {
  const v = String(code ?? "").toUpperCase();
  if (!v) return undefined;
  if (v === "STALLED") return t("miningRunErrorStalled");
  return v;
}

/** 批次状态 → 中文展示（后端枚举原值不改写，仅显示层映射；未知状态回落到原值） */
function runStatusLabel(status: string | null | undefined): string {
  const v = String(status ?? "").toLowerCase();
  const map: Record<string, string> = {
    running: "miningRunStatusRunning",
    queued: "miningRunStatusQueued",
    validating: "miningRunStatusValidating",
    succeeded: "miningRunStatusSucceeded",
    converged: "miningRunStatusConverged",
    done: "miningRunStatusDone",
    failed: "miningRunStatusFailed",
    cancelled: "miningRunStatusCancelled",
  };
  return map[v] ? t(map[v] as string) : String(status ?? "-");
}

/** 32 位批次号 → 可读短号（悬浮仍可见完整 id） */
function shortRunId(id: string): string {
  return `#${String(id ?? "").slice(0, 8)}`;
}

/** 时间串 → `YYYY-MM-DD HH:mm`（后端返回 `2026-08-21T00:00:00` 之类，界面不直出原始串） */
function fmtDateTime(value: string | null | undefined): string {
  const s = String(value ?? "").trim();
  if (!s) return "-";
  const [d, hm] = s.split("T");
  return hm ? `${d} ${hm.slice(0, 5)}` : d;
}

/** 时间串 → 仅日期（兼容 `2026-08-21T00:00:00` 与 `2025-01-01 00:00:00` 两种后端格式） */
function fmtDate(value: string | null | undefined): string {
  const s = String(value ?? "").trim();
  if (!s) return "-";
  return s.split("T")[0].split(" ")[0];
}

/** 后端逐代汇总 → 曲线点（generation 升序） */
function toCurve(gens: Array<Record<string, unknown>>): CurvePoint[] {
  return (gens ?? [])
    .map((g) => ({
      generation: Number(g.generation ?? 0),
      best_icir: Number(g.best_icir ?? 0),
      avg_icir: Number(g.avg_icir ?? 0),
    }))
    .sort((a, b) => a.generation - b.generation);
}

/**
 * 总代数：`total_generations` 为主，后端早期返回名 `max_generation` 兜底。
 * 缺此兜底时进度分母恒为 0，进度列永远显示「0」。
 */
function totalGenerationsOf(run: MiningRun): number {
  const typed = run.total_generations;
  if (typeof typed === "number" && typed > 0) return typed;
  const legacy = (run as unknown as Record<string, unknown>).max_generation;
  if (typeof legacy === "number" && legacy > 0) return legacy;
  return 0;
}

/** 向导各步上报的配置（A4 上提） */
export interface WizardConfig {
  candidate_pool_snapshot_id?: string | null;
  data_cutoff_at?: string | null;
  start_date?: string;
  end_date?: string;
  rebalance_frequency?: MiningFrequency;
  target_horizon?: number;
  train_ratio?: number;
  validation_ratio?: number;
  selected_fields?: string[];
  evolution_params?: Record<string, unknown>;
  /** C1：跳转因子模型页所需的因子集上下文（入组后由外层注入） */
  factor_set_id?: string | null;
}

/** 轮询间隔：与任务中心口径一致（5s，not_do：不引入 WebSocket） */
const POLL_INTERVAL_MS = 5000;

/** 停滞提示阈值（秒）：running 状态下代数超过这么久没变化就提示用户 */
const STALL_HINT_SECONDS = 90;

/** 「最近活跃会话」localStorage 键：提交/查看后刷新或重进页面自动恢复该 run 视图 */
const LAST_RUN_KEY = "mining_last_run_id";

export interface MiningShellProps {
  /** 运行数据（step5 用；无则不臆造，显示空态） */
  runProgress?: RunProgress | null;
  /** step4 提交成功后的回调（由外层接后端创建批次） */
  onSubmitted?: () => void;
}

function toIsoOrFallback(raw: string | null | undefined, fallback: Date): string {
  if (!raw) return fallback.toISOString();
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? fallback.toISOString() : d.toISOString();
}

/** 镜像区间 → 覆盖年数（向导 §4.1 月频引导用；无法解析返回 0） */
function mirrorYearsOf(from: string | null | undefined, to: string | null | undefined): number {
  if (!from || !to) return 0;
  const a = new Date(from);
  const b = new Date(to);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return 0;
  return Math.max(0, Math.round((b.getTime() - a.getTime()) / (365.25 * 24 * 3600 * 1000)));
}

/** GET /runs/{id}（MiningRun）→ Step5 的 RunProgress（缺省字段用上次值保位） */
function toRunProgress(run: MiningRun, prev: RunProgress): RunProgress {
  return {
    run_id: run.id,
    status: String(run.status ?? prev.status) as RunStatus,
    generation: run.current_generation ?? prev.generation,
    max_generations: run.total_generations ?? prev.max_generations,
    best_icir: run.best_icir ?? prev.best_icir ?? null,
    avg_icir: prev.avg_icir ?? null,
    diversity: prev.diversity ?? null,
    convergence_threshold: prev.convergence_threshold,
    converged: String(run.status) === "converged" || prev.converged,
    curve: prev.curve ?? [],
    eta_seconds: prev.eta_seconds ?? null,
  };
}

export default function MiningShell({
  runProgress = null,
  onSubmitted,
}: MiningShellProps) {
  const [tab, setTab] = useState<ShellTab>("wizard");
  const [currentStep, setCurrentStep] = useState(0);
  const [runs, setRuns] = useState<MiningRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  // A4：向导配置上提 + 提交后运行状态
  const [wizardConfig, setWizardConfig] = useState<WizardConfig>({});
  // P0-1：候选池 id（Step1 物化后由 onPoolCreated 上报；重复生成走既有池）
  const [poolId, setPoolId] = useState("");
  const [runState, setRunState] = useState<RunProgress | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // P0-3：提交错误文案（null=无错）；缺快照/接口失败给出可关闭的明确提示
  const [submitError, setSubmitError] = useState<string | null>(null);
  // A5：#20 结果页数据 —— run 成功后拉取真实候选列表
  const [resultCandidates, setResultCandidates] = useState<MiningCandidate[] | null>(null);
  // P0-2：Step3 字段目录（挂载时调候选池 filter-fields 映射注入，真实可选字段）
  const [fieldCatalog, setFieldCatalog] = useState<MiningField[]>([]);
  // P1-8：双锁状态（Step4 提交弹窗三态；后端 GET /factor-mining/locks/status）
  const [lockStatus, setLockStatus] = useState<MiningLockStatus | null>(null);
  // P1-9：Step2 切分预算（后端 POST /factor-mining/split-budget）+ 镜像区间
  const [splitBudget, setSplitBudget] = useState<SplitBudget | null>(null);
  const [mirrorRange, setMirrorRange] = useState<{ from: string | null; to: string | null }>({ from: null, to: null });
  // §8.3.6：性能探针面板（调试态）—— `?debug=perf` 开启，5s 轮询每代探针
  const debugPerf =
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).get("debug") === "perf";
  const [probeGens, setProbeGens] = useState<PerfProbeGeneration[]>([]);
  /** 停滞观测：上次代数变化的时间点（用于"卡住了"提示） */
  const stallRef = useRef<{ gen: number; at: number } | null>(null);
  const [stallSeconds, setStallSeconds] = useState(0);
  /** 草稿：已保存的 draft_id（二次保存走更新）+ 最近一次保存结果提示 */
  const [draftId, setDraftId] = useState<string | null>(null);
  const [draftNote, setDraftNote] = useState<{ ok: boolean; text: string } | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  /** 「已恢复最近任务」提示条（选项3）：仅 localStorage 自动恢复时展示，可关闭；点「新建任务」回 Step1 并停轮询 */
  const [restoredNotice, setRestoredNotice] = useState(false);

  // props 注入优先于内部提交产生的状态（测试仍可外部注入 runProgress）
  const activeProgress = runState ?? runProgress;

  const patchConfig = useCallback((patch: Partial<WizardConfig>) => {
    setWizardConfig((c) => ({ ...c, ...patch }));
  }, []);

  const loadRuns = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const page = await factorMiningApi.listRuns({ page: 1, page_size: 20 });
      setRuns(Array.isArray(page?.items) ? page.items : []);
    } catch {
      // 网络异常：不崩，落错误态
      setRuns([]);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tab === "runs") void loadRuns();
  }, [tab, loadRuns]);

  /** 批次列表 → Step5：把该批次载入进化跟踪（否则列表里的批次点不开） */
  const viewRun = useCallback((run: MiningRun) => {
    setRunState({
      run_id: run.id,
      status: String(run.status ?? "running") as RunStatus,
      generation: run.current_generation ?? 0,
      max_generations: totalGenerationsOf(run),
      best_icir: run.best_icir ?? null,
      avg_icir: null,
      diversity: null,
      converged: String(run.status) === "converged",
      curve: [],
      eta_seconds: null,
    });
    setCurrentStep(4);
    setTab("wizard");
    // 显式「查看」不提示恢复（提示条仅 localStorage 自动恢复时展示）
    setRestoredNotice(false);
    // 记住「最近活跃会话」：提交/查看后刷新或重进页面可自动恢复该 run 视图
    try {
      window.localStorage.setItem(LAST_RUN_KEY, run.id);
    } catch { /* 隐私/存储不可用则跳过，不影响功能 */ }
  }, []);

  /** 批次列表行内「放弃」：调 discard 后刷新列表（失败不崩，列表保留原状） */
  const discardRunRow = useCallback(
    async (run: MiningRun) => {
      try {
        await factorMiningApi.discardRun(run.id);
      } catch {
        // 静默：刷新后状态未变即可看出失败
      }
      void loadRuns();
    },
    [loadRuns],
  );

  // P0-2：进入 Step3 时拉取挖掘字段目录（一次即可，缓存于 state）。
  // 2026-09-21 修复：目录改由 `GET /factor-mining/fields`（DSL 注册字段：
  // close/pe_ttm/roe_ttm…）供给，不再用候选池筛选字段（filter-fields：
  // avg_amount/board…）——后者不是公式/模板引用的字段，勾选后经典模板
  // 全部跳过、初始种群为空导致 worker crash（贯通阻断）。
  useEffect(() => {
    if (currentStep !== 2 || fieldCatalog.length > 0) return;
    let alive = true;
    void factorMiningApi
      .listMiningFields()
      .then((res) => {
        if (!alive) return;
        const list = Array.isArray(res?.fields) ? res.fields : [];
        setFieldCatalog(
          list.map((f) => ({
            code: String(f.field ?? ""),
            name_zh: String(f.label_zh ?? f.field ?? ""),
            group: String(f.group ?? ""),
            category_label_zh:
              f.group_label_zh != null ? String(f.group_label_zh) : null,
            source_table: f.source_table != null ? String(f.source_table) : null,
            data_mode:
              f.availability === "blocked"
                ? "blocked"
                : (f.data_mode as FieldDataMode) ?? "continuous",
            blocked_reason:
              f.availability === "blocked" && f.blocked_reason_zh != null
                ? String(f.blocked_reason_zh)
                : null,
            // 开发口径单独带出：界面正文用 blocked_reason，悬浮提示用 blocked_detail
            blocked_detail: f.blocked_detail_zh != null ? String(f.blocked_detail_zh) : null,
          })),
        );
      })
      .catch(() => setFieldCatalog([]));
    return () => {
      alive = false;
    };
  }, [currentStep, fieldCatalog.length]);

  // P1-8：进入 Step4 时拉取双锁状态（提交弹窗三态文案；失败保持默认「无冲突」）
  useEffect(() => {
    if (currentStep !== 3) return;
    let alive = true;
    void factorMiningApi
      .getLockStatus()
      .then((s) => {
        if (alive) setLockStatus(s);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [currentStep]);

  // P1-9：镜像区间（Step2 引导横幅用；失败保持默认 null → 引导降级）
  useEffect(() => {
    let alive = true;
    void dataMirrorApi
      .getStatus()
      .then((s) => {
        if (!alive) return;
        const rec = s as { mirrored_from?: string | null; mirrored_to?: string | null };
        setMirrorRange({ from: rec.mirrored_from ?? null, to: rec.mirrored_to ?? null });
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  // P1-9：进入 Step2 且有日期时拉取切分预算（日期/频率/持有期变化时重拉）
  useEffect(() => {
    if (currentStep !== 1) return;
    if (!wizardConfig.start_date || !wizardConfig.end_date) return;
    let alive = true;
    void factorMiningApi
      .computeSplitBudget({
        start_date: wizardConfig.start_date,
        end_date: wizardConfig.end_date,
        frequency: wizardConfig.rebalance_frequency ?? "daily",
        target_horizon: wizardConfig.target_horizon ?? 5,
        train_ratio: wizardConfig.train_ratio ?? 0.6,
        validation_ratio: wizardConfig.validation_ratio ?? 0.2,
      })
      .then((b) => {
        if (alive) setSplitBudget(b);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [
    currentStep,
    wizardConfig.start_date,
    wizardConfig.end_date,
    wizardConfig.rebalance_frequency,
    wizardConfig.target_horizon,
    wizardConfig.train_ratio,
    wizardConfig.validation_ratio,
  ]);

  /** P1-9：去镜像 → 跳到「设置 → 数据中心 → 数据镜像」Tab */
  const goMirror = useCallback(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem("settings_data_center_tab", "mirror");
    window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "data-center" }));
  }, []);

  // 卸载时停止轮询
  useEffect(
    () => () => {
      if (pollRef.current) clearInterval(pollRef.current);
    },
    [],
  );

  // A5：#20 结果页数据 —— run 完成（succeeded/converged，含「提前停止」保留结果）
  // 后拉取真实候选（列表契约 A3 已实现）。§8.3.5：converged 同样保留已进化候选。
  const activeRunId = activeProgress?.run_id ?? null;
  const runDone =
    activeProgress?.status === "succeeded" || activeProgress?.status === "converged";
  useEffect(() => {
    if (!runDone || !activeRunId) return;
    void factorMiningApi
      .listCandidates(activeRunId, { page_size: 50 })
      .then((page) => {
        setResultCandidates(Array.isArray(page?.items) ? page.items : []);
      })
      .catch(() => setResultCandidates([]));
  }, [runDone, activeRunId]);

  // §8.3.6：性能探针面板（调试态）—— `?debug=perf` 时 5s 轮询每代探针数据
  useEffect(() => {
    if (!debugPerf || !activeRunId) return;
    const loadProbes = () => {
      void factorMiningApi
        .listGenerations(activeRunId)
        .then((rows) => {
          setProbeGens(Array.isArray(rows) ? (rows as PerfProbeGeneration[]) : []);
        })
        .catch(() => undefined);
    };
    loadProbes();
    const iv = setInterval(loadProbes, POLL_INTERVAL_MS);
    return () => clearInterval(iv);
  }, [debugPerf, activeRunId]);

  const resultContext: MiningResultContext | null =
    wizardConfig.candidate_pool_snapshot_id
      ? {
          factor_set_id: wizardConfig.factor_set_id ?? null,
          data_cutoff_at: wizardConfig.data_cutoff_at ?? null,
          candidate_pool_snapshot_id: wizardConfig.candidate_pool_snapshot_id ?? null,
          rebalance_frequency: wizardConfig.rebalance_frequency ?? null,
        }
      : null;

  /**
   * 步骤条状态（设计 §2）—— 四态：已完成 / 当前（含进度）/ 阻断（红）/ 未到（灰）。
   *
   * **只根据壳层真实持有的配置推导**，不臆造「进度」：
   * 各步的完成判据就是该步上报过（`onConfig`/`onSnapshot`）必需配置；
   * 阻断只认壳层确实知道的硬缺失（缺候选池快照导致提交被拒）。
   */
  const stepDone: boolean[] = [
    Boolean(wizardConfig.candidate_pool_snapshot_id),
    Boolean(wizardConfig.start_date && wizardConfig.end_date),
    (wizardConfig.selected_fields?.length ?? 0) > 0,
    wizardConfig.evolution_params != null,
    runState != null,
  ];
  const stepBlocked: boolean[] = [
    Boolean(submitError) && !wizardConfig.candidate_pool_snapshot_id,
    false,
    false,
    false,
    false,
  ];
  /** 当前步骤的真实进度（仅运行步有可量化的代数进度，其余不臆造） */
  const runStepPercent =
    runState != null
      ? Math.min(
          100,
          Math.round(
            (runState.generation / Math.max(1, runState.max_generations)) * 100,
          ),
        )
      : null;

  const stepStateText = (idx: number): string => {
    if (stepBlocked[idx]) return t("miningStepStateBlocked");
    if (idx === currentStep) return t("miningStepStateActive");
    if (stepDone[idx]) return t("miningStepStateDone");
    if (idx > currentStep) return t("miningStepStateTodo");
    return t("miningStepStatePending");
  };

  // F2 §8.3.3：真实候选 → 当前种群 Top 因子（按 generation_rank 升序，取前 10）
  const topCandidates: TopFactor[] = useMemo(() => {
    if (!Array.isArray(resultCandidates) || resultCandidates.length === 0) return [];
    return resultCandidates
      .filter((c) => c.generation_rank != null)
      .sort((a, b) => (a.generation_rank ?? 0) - (b.generation_rank ?? 0))
      .slice(0, 10)
      .map((c) => ({
        rank: c.generation_rank ?? 0,
        formula: c.canonical_formula ?? c.formula_expr ?? c.formula ?? c.id,
        source: c.operation ?? null,
        icir: c.generation_icir ?? c.icir ?? null,
        coverage: c.generation_coverage ?? null,
      }));
  }, [resultCandidates]);

  const buildPayload = (cfg: WizardConfig): MiningRunCreate => {
    const cutoff = cfg.data_cutoff_at ? new Date(cfg.data_cutoff_at) : new Date();
    const cutoffIso = Number.isNaN(cutoff.getTime())
      ? new Date().toISOString()
      : toIsoOrFallback(cfg.data_cutoff_at, new Date());
    const oneYearAgo = new Date(cutoff);
    oneYearAgo.setFullYear(oneYearAgo.getFullYear() - 1);
    const evo = { ...DEFAULT_EVO_CONFIG, ...(cfg.evolution_params ?? {}) };
    return {
      candidate_pool_snapshot_id: cfg.candidate_pool_snapshot_id ?? "",
      data_cutoff_at: cutoffIso,
      start_date: toIsoOrFallback(cfg.start_date, oneYearAgo),
      end_date: toIsoOrFallback(cfg.end_date, cutoff),
      rebalance_frequency: cfg.rebalance_frequency ?? "daily",
      target_horizon: cfg.target_horizon ?? 5,
      train_ratio: cfg.train_ratio ?? 0.6,
      validation_ratio: cfg.validation_ratio ?? 0.2,
      random_seed: 42,
      evolution_params: evo,
      filter_config: { selected_fields: cfg.selected_fields ?? [] },
    };
  };

  const refreshRun = useCallback((runId: string) => {
    void factorMiningApi
      .getRun(runId)
      .then((run) => {
        setRunState((prev) => (prev ? toRunProgress(run, prev) : prev));

        // 停滞检测：running 但代数长时间不动 → 用户得知道"卡住了"而不是干等。
        // （后端 worker 未推进/探针未上报时，进度会永久停在 0/20）
        const gen = run.current_generation ?? 0;
        const now = Date.now();
        const mark = stallRef.current;
        if (!mark || mark.gen !== gen) stallRef.current = { gen, at: now };
        setStallSeconds(
          String(run.status ?? "") === "running"
            ? Math.floor((now - (stallRef.current?.at ?? now)) / 1000)
            : 0,
        );

        // 逐代曲线/日志：后端把每代汇总写在 factor_mining_generations，
        // 此前前端从不拉取 → ICIR 曲线与运行日志永远空白。
        void factorMiningApi
          .listGenerations(runId)
          .then((gens) => {
            setRunState((prev) => (prev ? { ...prev, curve: toCurve(gens) } : prev));
          })
          .catch(() => undefined);

        // 终态（含失败/取消）后停止轮询，避免无意义请求（R3）
        if (["succeeded", "converged", "failed", "cancelled"].includes(String(run.status ?? ""))) {
          if (pollRef.current) {
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
        }
      })
      .catch(() => undefined);
  }, []);

  /** 启动 5s 轮询（先清旧定时器；提交后与 URL 会话直达共用） */
  const startPolling = useCallback((runId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(() => refreshRun(runId), POLL_INTERVAL_MS);
  }, [refreshRun]);

  // 会话恢复：URL `?run=<id>`（或 ?run_id=）优先，其次恢复 localStorage 里
  // 的「最近活跃会话」——提交/查看后退出前台、重新进入时自动回到该 run 的
  // Step5（实时轮询动态与结果）。进展持久化在 factor_mining_runs/generations。
  // 健壮性（2026-09-22 评审修复）：
  //   R2 失效 id → getRun 失败时清除本地残留，避免每次进入重复无效请求；
  //   R3 终态 run → 恢复后不再轮询（refreshRun 也会在到达终态时自停）；
  //   R7 恢复时回填快照上下文，让结果区「跳转因子模型页」可用。
  useEffect(() => {
    if (typeof window === "undefined") return;
    const urlRun = new URLSearchParams(window.location.search).get("run")
      ?? new URLSearchParams(window.location.search).get("run_id");
    const fromUrl = Boolean(urlRun);
    const restoreId = urlRun || (() => {
      try { return window.localStorage.getItem(LAST_RUN_KEY) || ""; } catch { return ""; }
    })();
    if (!restoreId) return;
    let alive = true;
    void factorMiningApi
      .getRun(restoreId)
      .then((run) => {
        if (!alive) return;
        viewRun(run);
        // R7：快照上下文回填（filter 频率/截止日/持有期）→ 「跳转因子模型页」可用
        patchConfig({
          candidate_pool_snapshot_id: run.candidate_pool_snapshot_id ?? null,
          data_cutoff_at: run.data_cutoff_at ?? null,
          rebalance_frequency: run.rebalance_frequency ?? undefined,
          target_horizon: run.target_horizon ?? undefined,
        });
        // R3：终态只展示不轮询；进行中才启动实时刷新
        // R-3：轮询目标用响应里的 run.id（而非入参 restoreId），保证展示与轮询同一 run
        const targetId = run.id || restoreId;
        if (!["succeeded", "converged", "failed", "cancelled"].includes(String(run.status ?? ""))) {
          startPolling(targetId);
        }
        // 选项3：仅 localStorage 自动恢复时展示可关闭的「已恢复最近任务」提示条
        // （URL `?run=` 直达是用户显式意图，不打扰）
        if (!fromUrl) setRestoredNotice(true);
      })
      .catch((e) => {
        // R2：URL 直达不清理（用户显式带的）。
        if (fromUrl) return;
        // R-2：仅当 id 真正失效（404）才清本地残留；网络(0)/超时(408)等瞬时错误
        // 是 retryable 的，误清会丢掉有效的「最近会话」，下次进入再试即可。
        const neverFound =
          (e as { status_code?: number })?.status_code === 404;
        if (neverFound) {
          try { window.localStorage.removeItem(LAST_RUN_KEY); } catch { /* 忽略 */ }
        }
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewRun, startPolling, patchConfig]);

  /**
   * 「保存草稿」：按步组装当前向导配置落库（后端 `POST /factor-mining/drafts` 已实现）。
   *
   * 2026-09-23 修复：此前本壳**从未注入** `onSaveDraft`，按钮 `onClick` 实为 `undefined`
   * → 点「保存草稿」零反应（验收报告 P1-1 的根因）。首次保存新建，之后复用 `draftId` 更新。
   */
  const handleSaveDraft = useCallback(async () => {
    const cfg = wizardConfig;
    try {
      const res = await factorMiningApi.saveDraft({
        draft_id: draftId,
        current_step: currentStep + 1,
        candidate_pool_snapshot_id: cfg.candidate_pool_snapshot_id ?? null,
        // 步骤键固定 step1~step4（后端 DRAFT_STEP_KEYS，不含运行步）
        steps: {
          step1: {
            candidate_pool_snapshot_id: cfg.candidate_pool_snapshot_id ?? null,
            pool_id: poolId || null,
          },
          step2: {
            start_date: cfg.start_date ?? null,
            end_date: cfg.end_date ?? null,
            data_cutoff_at: cfg.data_cutoff_at ?? null,
            rebalance_frequency: cfg.rebalance_frequency ?? null,
            target_horizon: cfg.target_horizon ?? null,
            train_ratio: cfg.train_ratio ?? null,
            validation_ratio: cfg.validation_ratio ?? null,
          },
          step3: { selected_fields: cfg.selected_fields ?? [] },
          step4: { evolution_params: { ...DEFAULT_EVO_CONFIG, ...(cfg.evolution_params ?? {}) } },
        },
      });
      const id = String(res?.draft_id ?? "");
      if (id) setDraftId(id);
      setDraftNote({
        ok: true,
        text: t("miningDraftSaved").replace("{id}", id ? shortRunId(id) : "-"),
      });
    } catch (e) {
      setDraftNote({
        ok: false,
        text: t("miningDraftSaveFailed").replace(
          "{msg}",
          e instanceof Error ? e.message : String(e ?? ""),
        ),
      });
    }
  }, [wizardConfig, draftId, currentStep, poolId]);

  const handleSubmit = async () => {
    const cfg = wizardConfig;
    if (!cfg.candidate_pool_snapshot_id) {
      // P0-3：缺快照 → 明确提示 + 跳回第 1 步（不再静默 return 卡死弹窗）
      setSubmitError(t("miningSubmitNeedSnapshot"));
      setCurrentStep(0);
      return;
    }
    setSubmitError(null);
    setSubmitting(true);
    try {
      const created = await factorMiningApi.createRun(buildPayload(cfg));
      const evo = { ...DEFAULT_EVO_CONFIG, ...(cfg.evolution_params ?? {}) };
      setRunState({
        run_id: created.run_id,
        status: "queued",
        generation: 0,
        max_generations: Number(evo.max_generations) || 20,
        convergence_threshold: 0.01,
        converged: false,
        curve: [],
        eta_seconds: created.eta_seconds ?? null,
      });
      setCurrentStep(MINING_STEPS.length - 1);
      onSubmitted?.();
      // 提交的是新 run，清除「已恢复最近任务」提示，避免盖在新运行页上
      setRestoredNotice(false);
      // R-1：提交的新 run 覆盖「最近活跃会话」——否则刷新/重进恢复的是更早的查看记录
      try {
        window.localStorage.setItem(LAST_RUN_KEY, created.run_id);
      } catch { /* 隐私/存储不可用则跳过，不影响功能 */ }
      // 提交后启动 5s 轮询批次详情，更新第 5 步进度
      startPolling(created.run_id);
    } catch {
      setSubmitError(t("miningSubmitError"));
    } finally {
      setSubmitting(false);
    }
  };

  /** 第 5 步三操作（中断/继续/停止/放弃）接真实接口，操作后刷新一次 */
  const runRemote = useCallback(
    (fn: (id: string) => Promise<unknown>) => {
      const id = runState?.run_id;
      if (!id) return;
      void fn(id)
        .then(() => refreshRun(id))
        .catch(() => undefined);
    },
    [runState?.run_id, refreshRun],
  );

  /** 提示条「新建任务」：离开当前 run（停轮询）+ 回到向导第 1 步 + 收起提示 */
  const handleNewTaskFromNotice = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    setRestoredNotice(false);
    setCurrentStep(0);
  }, []);

  return (
    <div data-mining-shell className="settings-indicator-stack mining-shell">
      <div className="sub-tabs" aria-label={t("factorMiningTabTitle")}>
        <button
          type="button"
          className={`sub-tab ${tab === "wizard" ? "active" : ""}`}
          aria-current={tab === "wizard" ? "page" : undefined}
          onClick={() => setTab("wizard")}
        >
          {t("miningTabWizard")}
        </button>
        <button
          type="button"
          className={`sub-tab ${tab === "runs" ? "active" : ""}`}
          aria-current={tab === "runs" ? "page" : undefined}
          onClick={() => setTab("runs")}
        >
          {t("miningTabRuns")}
        </button>
        <button
          type="button"
          className={`sub-tab ${tab === "experience" ? "active" : ""}`}
          aria-current={tab === "experience" ? "page" : undefined}
          onClick={() => setTab("experience")}
        >
          {t("miningExpTab")}
        </button>
        <button
          type="button"
          className={`sub-tab ${tab === "templates" ? "active" : ""}`}
          aria-current={tab === "templates" ? "page" : undefined}
          onClick={() => setTab("templates")}
        >
          {t("miningTplTab")}
        </button>
      </div>

      <div className="sub-tab-container" hidden={tab !== "wizard"}>
        <ol className="mining-step-bar" data-mining-step-bar>
          {MINING_STEPS.map((key, idx) => {
            const visual = stepBlocked[idx]
              ? "blocked"
              : idx === currentStep
                ? "current"
                : stepDone[idx]
                  ? "done"
                  : "todo";
            const showProgress = idx === currentStep && idx === 4 && runStepPercent != null;
            return (
              <li
                key={key}
                className={`mining-step ${visual}`}
                data-mining-step={key}
                data-mining-step-state={visual}
                aria-current={idx === currentStep ? "step" : undefined}
              >
                <button
                  type="button"
                  className="mining-step-btn"
                  data-mining-step-jump={key}
                  onClick={() => setCurrentStep(idx)}
                >
                  <span className="mining-step-index" aria-hidden={visual === "done"}>
                    {visual === "done" ? "✓" : idx + 1}
                  </span>
                  <span className="mining-step-label">{t(STEP_LABEL_KEYS[key])}</span>
                  <span className="mining-step-state">
                    {stepStateText(idx)}
                    {stepBlocked[idx] && submitError
                      ? ` · ${t("miningStepErrorCount").replace("{n}", "1")}`
                      : ""}
                  </span>
                  {showProgress && (
                    <span
                      className="mining-step-progress"
                      data-mining-step-progress={key}
                      role="progressbar"
                      aria-valuenow={runStepPercent}
                      aria-valuemin={0}
                      aria-valuemax={100}
                    >
                      <span style={{ width: `${runStepPercent}%` }} />
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ol>

        {/* G5+A4：5 步内容接线（step1~step5，配置上提 wizardConfig） */}
        <div className="mining-wizard-body" data-mining-wizard-body>
          {currentStep === 0 && (
            <div data-mining-step-panel="pool">
              <MiningPoolStep
                poolId={poolId}
                onNext={() => setCurrentStep(1)}
                onPoolCreated={(id) => setPoolId(id)}
                onSnapshot={(snap) =>
                  patchConfig({
                    candidate_pool_snapshot_id: snap?.id ?? null,
                    // 优先用后端快照自带的 data_cutoff_at；回落到分析时间/截止日
                    data_cutoff_at:
                      snap != null
                        ? (snap.data_cutoff_at ?? snap.analyzed_at ?? snap.as_of_date ?? null)
                        : null,
                  })
                }
              />
            </div>
          )}
          {currentStep === 1 && (
            <div data-mining-step-panel="time-target">
              <MiningTimeTargetStep
                budget={splitBudget}
                dataCutoffAt={wizardConfig.data_cutoff_at}
                mirroredFrom={mirrorRange.from}
                mirroredTo={mirrorRange.to}
                mirroredYears={mirrorYearsOf(mirrorRange.from, mirrorRange.to)}
                onGoMirror={goMirror}
                onChange={(cfg) =>
                  patchConfig({
                    start_date: cfg.start_date,
                    end_date: cfg.end_date,
                    rebalance_frequency: cfg.rebalance_frequency,
                    target_horizon: cfg.target_horizon,
                    train_ratio: cfg.ratios.train / 100,
                    validation_ratio: cfg.ratios.val / 100,
                  })
                }
              />
            </div>
          )}
          {currentStep === 2 && (
            <div data-mining-step-panel="field">
              <MiningFieldStep
                fields={fieldCatalog}
                selected={wizardConfig.selected_fields}
                onChange={(selected) => patchConfig({ selected_fields: selected })}
              />
            </div>
          )}
          {currentStep === 3 && (
            <div data-mining-step-panel="evolution">
              {draftNote && (
                <p
                  className={`mining-draft-note${draftNote.ok ? "" : " mining-draft-note--error"}`}
                  data-mining-draft-note
                  role="status"
                >
                  {draftNote.text}
                </p>
              )}
              <MiningEvoParamStep
                onSubmit={() => void handleSubmit()}
                onSaveDraft={() => void handleSaveDraft()}
                onConfig={(evo) => patchConfig({ evolution_params: { ...evo } })}
                lockStatus={lockStatus}
                etaSeconds={null}
                resourcesOk={true}
              />
            </div>
          )}
          {currentStep === 4 && (
            <div data-mining-step-panel="run">
              {restoredNotice && runState && (
                <div className="mining-restored-notice" data-mining-restored-notice role="status">
                  <span className="mining-restored-text">
                    {t("miningRestoredNotice").replace("{id}", shortRunId(runState.run_id))}
                  </span>
                  <button
                    type="button"
                    className="mining-restored-new"
                    data-mining-restored-new
                    onClick={handleNewTaskFromNotice}
                  >
                    {t("miningRestoredNewTask")}
                  </button>
                  <button
                    type="button"
                    className="mining-restored-close"
                    data-mining-restored-close
                    aria-label={t("miningResClose")}
                    onClick={() => setRestoredNotice(false)}
                  >
                    {t("miningResClose")}
                  </button>
                </div>
              )}
              {stallSeconds >= STALL_HINT_SECONDS && (
                <p className="mining-run-stalled" data-mining-run-stalled role="status">
                  {t("miningRunStalled").replace("{s}", String(stallSeconds))}
                </p>
              )}
              {activeProgress ? (
                <FactorMiningRunTrack
                  progress={activeProgress}
                  topCandidates={topCandidates}
                  onPause={
                    activeProgress.run_id
                      ? () => runRemote(factorMiningApi.pauseRun)
                      : undefined
                  }
                  onResume={
                    activeProgress.run_id
                      ? () => runRemote(factorMiningApi.resumeRun)
                      : undefined
                  }
                  onStop={
                    activeProgress.run_id
                      ? () => runRemote(factorMiningApi.stopRun)
                      : undefined
                  }
                  onDiscard={
                    activeProgress.run_id
                      ? () => runRemote(factorMiningApi.discardRun)
                      : undefined
                  }
                  onCancelQueue={
                    activeProgress.status === "queued" && activeProgress.run_id
                      ? () => runRemote(factorMiningApi.cancelRun)
                      : undefined
                  }
                />
              ) : (
                <p className="mining-placeholder" data-mining-run-empty>
                  {t("miningWizardNoRun")}
                </p>
              )}

              {/* A5：#20 结果页数据 —— run 成功后展示真实候选（排行榜预览） */}
              {activeProgress && resultCandidates != null && (
                <div className="mining-result-section" data-mining-result-section>
                  <div className="mining-result-head-row">
                    <h4>{t("miningResultSectionTitle")}</h4>
                    {/* C1：4 上下文跳转因子模型页（缺任一禁用，复用 settings:navigate） */}
                    <ResultGotoFactorModelButton
                      context={resultContext}
                    />
                  </div>
                  {resultCandidates.length === 0 ? (
                    <p data-mining-result-empty>{t("miningResultEmpty")}</p>
                  ) : (
                    <table className="mining-result-table" data-mining-result-rows>
                      <thead>
                        <tr>
                          <th>{t("miningResultColFormula")}</th>
                          <th>{t("miningResultColGen")}</th>
                          <th>{t("miningResultColSource")}</th>
                          <th>{t("miningResultColIc")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {resultCandidates.map((c) => (
                          <tr key={c.id}>
                            <td>{c.canonical_formula ?? c.formula_expr ?? c.formula ?? c.id}</td>
                            <td>{c.generation ?? 0}</td>
                            <td>{c.operation ?? "-"}</td>
                            <td>
                              {c.generation_icir != null
                                ? Number(c.generation_icir).toFixed(4)
                                : "-"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}

              {/* §8.3.6：性能探针面板（调试态，?debug=perf 开启；默认折叠在资源区下方） */}
              {debugPerf && activeRunId && <PerfProbePanel generations={probeGens} />}
            </div>
          )}
        </div>

        <div className="mining-wizard-nav">
          <button
            type="button"
            data-mining-prev
            disabled={currentStep === 0}
            onClick={() => setCurrentStep((s) => Math.max(0, s - 1))}
          >
            {t("miningWizardPrev")}
          </button>
          <button
            type="button"
            data-mining-next
            disabled={currentStep >= MINING_STEPS.length - 1}
            onClick={() => setCurrentStep((s) => Math.min(MINING_STEPS.length - 1, s + 1))}
          >
            {t("miningWizardNext")}
          </button>
        </div>

        {(submitting || submitError) && (
          <div className="mining-submit-msg">
            {submitting && (
              <p className="mining-submit-pending" data-mining-submit-pending>
                {t("miningSubmitPending")}
              </p>
            )}
            {submitError && (
              <p
                className="mining-submit-error"
                data-mining-submit-error
                role="alert"
              >
                {submitError}
                <button
                  type="button"
                  className="mining-submit-dismiss"
                  data-mining-submit-dismiss
                  onClick={() => setSubmitError(null)}
                >
                  {t("miningResClose")}
                </button>
              </p>
            )}
          </div>
        )}
      </div>

      <div className="sub-tab-container" hidden={tab !== "runs"}>
        {loading && <p data-mining-runs-loading>{t("miningRunsLoading")}</p>}
        {!loading && failed && <p data-mining-runs-error>{t("miningRunsError")}</p>}
        {!loading && !failed && runs.length === 0 && (
          <p data-mining-runs-empty>{t("miningRunsEmpty")}</p>
        )}
        {!loading && !failed && runs.length > 0 && (
          <div className="mining-table-wrap">
            <table className="mining-runs-table" data-mining-runs-table>
              <thead>
                <tr>
                  <th>{t("miningRunsColRun")}</th>
                  <th>{t("miningRunsColStatus")}</th>
                  <th style={{ minWidth: 180 }}>{t("miningRunsColProgress")}</th>
                  <th>{t("miningRunsColRange")}</th>
                  <th>{t("miningRunsColActions")}</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const gen = run.current_generation ?? 0;
                  const total = totalGenerationsOf(run);
                  const percent = total > 0 ? Math.min(100, Math.round((gen / total) * 100)) : 0;
                  return (
                    <tr key={run.id}>
                      <td>
                        <div style={{ display: "grid", gap: 2 }}>
                          <span className="mining-cell-formula" title={run.id}>
                            {shortRunId(run.id)}
                          </span>
                          <span className="mining-card-sub">
                            {fmtDateTime(run.created_at)}
                          </span>
                        </div>
                      </td>
                      <td>
                        <span
                          className={runStatusChipClass(run.status)}
                          title={runErrorHint(run.error_code)}
                          data-mining-run-error={run.error_code ?? undefined}
                        >
                          {runStatusLabel(run.status)}
                        </span>
                      </td>
                      <td>
                        <div style={{ display: "grid", gap: 4, minWidth: 140 }}>
                          <span className="mining-meter mining-meter--sm">
                            <span
                              className="mining-meter-fill"
                              style={{ width: `${percent}%` }}
                            />
                          </span>
                          <span className="mining-card-sub">
                            {total > 0 ? `${gen} / ${total} · ${percent}%` : `${gen}`}
                          </span>
                        </div>
                      </td>
                      <td>
                        <div style={{ display: "grid", gap: 2 }}>
                          <span>
                            {fmtDate(run.start_date)} ~ {fmtDate(run.end_date)}
                          </span>
                          <span className="mining-card-sub">
                            {run.rebalance_frequency ?? ""}
                          </span>
                        </div>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                          <button
                            type="button"
                            className="mining-pool-btn"
                            data-mining-run-view={run.id}
                            onClick={() => viewRun(run)}
                          >
                            {t("miningRunsActionView")}
                          </button>
                          <button
                            type="button"
                            className="mining-pool-btn danger"
                            data-mining-run-discard={run.id}
                            disabled={String(run.status) === "cancelled" || String(run.status) === "succeeded"}
                            onClick={() => void discardRunRow(run)}
                          >
                            {t("miningRunsActionDiscard")}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* B3：F1 经验库（挂载到挖掘域导航；契约 GET /factor-experience） */}
      <div className="sub-tab-container" hidden={tab !== "experience"}>
        <F1ExperiencePage active={tab === "experience"} />
      </div>

      {/* C2：因子模板配置（B4；契约 /factor-mining/templates*） */}
      <div className="sub-tab-container" hidden={tab !== "templates"}>
        <TemplateConfigPage active={tab === "templates"} />
      </div>
    </div>
  );
}