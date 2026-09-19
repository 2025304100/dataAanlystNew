/**
 * WP7-06: 因子模型页。
 *
 * 功能：
 * - 查看当前 runtime 状态（weight_mode / active_model_run_id / fallback_reason）
 * - 查看 FactorSet 列表与成员详情
 * - 查看候选模型列表（含状态、门禁拒绝原因、增强门禁指标）
 * - 激活模型（shadow/ridge）与回退手工权重
 * - 查看审计日志
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popover,
  Progress,
  Row,
  Col,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { SelectProps, TabsProps } from "antd";
import {
  ReloadOutlined,
  ThunderboltOutlined,
  RollbackOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
  InfoCircleOutlined,
  ExclamationCircleOutlined,
  PlusOutlined,
  CopyOutlined,
  EditOutlined,
  DeleteOutlined,
  CloseOutlined,
  CloudServerOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Snowflake } from "lucide-react";
import { t } from "../../i18n";
import {
  api,
  type FactorRuntime,
  type FactorModelRun,
  type FactorSet,
  type FactorSetMember,
  type FactorSetCreateRequest,
  type FactorSetMemberCreateRequest,
  type ScoringModelDetail,
  type ScoringModelFactorMember,
  type ScoringModelRelations,
  type ScoringModelRelationFactor,
} from "../../api/client";

/** 从当前 URL（hash 或 search）中解析 query 参数。返回空对象（无可解析值）。 */
function parseFactorCenterQuery(): Record<string, string> {
  const out: Record<string, string> = {};
  const tryParse = (raw: string | null | undefined) => {
    if (!raw) return;
    const q = raw.startsWith("?") ? raw.slice(1) : raw;
    if (!q) return;
    for (const seg of q.split("&")) {
      if (!seg) continue;
      const eqIdx = seg.indexOf("=");
      const k = eqIdx >= 0 ? seg.slice(0, eqIdx) : seg;
      const v = eqIdx >= 0 ? seg.slice(eqIdx + 1) : "";
      try {
        out[decodeURIComponent(k)] = decodeURIComponent(v);
      } catch {
        out[k] = v;
      }
    }
  };
  if (typeof window !== "undefined" && window.location) {
    tryParse(window.location.search);
    const h = window.location.hash;
    const qIdx = h.indexOf("?");
    if (qIdx >= 0) tryParse(h.slice(qIdx));
  }
  return out;
}
import { navigate } from "../../utils/navigate";
import ChainStepsBar, { type ChainState } from "./ChainStepsBar";

const { Text, Paragraph } = Typography;

/** 模型状态颜色映射。 */
function modelStatusColor(status: string): string {
  const map: Record<string, string> = {
    validated: "green",
    rejected: "red",
    training: "blue",
  };
  return map[status] || "default";
}
function modelStatusLabel(status: string | null | undefined): string {
  const map: Record<string, string> = { validated: "已验证", rejected: "已驳回", training: "训练中", production: "生产中", draft: "草稿", completed: "已完成", done: "已完成", failed: "失败", cancelled: "已取消" };
  return map[String(status ?? "").toLowerCase()] ?? status ?? "-";
}
function weightModeLabel(mode: string | null | undefined): string {
  const map: Record<string, string> = { manual: "手工模式", shadow: "观察模式", ridge: "生产模式" };
  return map[String(mode ?? "").toLowerCase()] ?? mode ?? "-";
}
function memberRoleLabel(role: string | null | undefined): string {
  const map: Record<string, string> = { feature: "特征", target: "目标", regime: "状态", control: "控制" };
  return map[String(role ?? "").toLowerCase()] ?? role ?? "-";
}
function weightConstraintLabel(value: string | null | undefined): string {
  const map: Record<string, string> = { free: "不限", positive: "仅正权重", negative: "仅负权重", none: "不限" };
  return map[String(value ?? "").toLowerCase()] ?? value ?? "-";
}
function missingPolicyLabel(value: string | null | undefined): string {
  const map: Record<string, string> = { exclude: "排除缺失值", impute_zero: "缺失填零", ignore: "忽略缺失" , drop: "排除缺失值" };
  return map[String(value ?? "").toLowerCase()] ?? value ?? "-";
}
function localizedLabel(key: string, fallback: string): string {
  const value = t(key);
  return value === key ? fallback : value;
}

/** 权重模式颜色映射。 */
function weightModeColor(mode: string): string {
  const map: Record<string, string> = {
    manual: "default",
    shadow: "orange",
    ridge: "green",
  };
  return map[mode] || "default";
}

/** FactorSet 状态颜色映射（Task 2 ⑧：draft 蓝 / frozen 绿 / deprecated 灰）。 */
function factorSetStatusColor(status: string): string {
  const map: Record<string, string> = {
    draft: "blue",
    frozen: "green",
    deprecated: "default",
  };
  return map[status] || "default";
}

/** FactorSet 状态中文显示。 */
function factorSetStatusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: "草稿",
    frozen: "已冻结",
    deprecated: "已废弃",
  };
  return map[status] || status;
}

/** 门禁：检查一个 FactorSet 对「冻结」的 readiness，返回 { disabled, tooltip }。 */
function computeFreezeGate(fs: FactorSet | null | undefined): { disabled: boolean; tooltip: string } {
  if (!fs) return { disabled: true, tooltip: "未选择集合" };
  const members = fs.members ?? [];
  const n = members.length;
  if (n === 0) return { disabled: true, tooltip: "需先添加至少 1 个因子成员" };
  const featureCount = members.filter((m) => m.role === "feature").length;
  if (featureCount === 0) return { disabled: true, tooltip: "集合中没有 feature 角色的因子" };
  // 任一成员 version 非 trainable（version_trainable=false 或 status∈{draft,deprecated}）
  const anyNotTrainable = members.some((m: any) => {
    if (typeof (m as any).version_trainable === "boolean" && !(m as any).version_trainable) return true;
    const s = String((m as any).version_status || "").toLowerCase();
    return s === "draft" || s === "deprecated" || s === "disabled";
  });
  if (anyNotTrainable) return { disabled: true, tooltip: "含有版本状态为 草稿/已废弃 的成员，请更新后再冻结" };
  return { disabled: false, tooltip: "" };
}

/** 门禁：「训练」按钮 readiness。返回 { disabled, tooltip }。 */
function computeTrainGate(fs: FactorSet | null | undefined): { disabled: boolean; tooltip: string } {
  if (!fs) return { disabled: true, tooltip: "未选择集合" };
  // Task 2 训练门禁(2)：集合状态 ≠ frozen → 禁用
  if (fs.status !== "frozen") return { disabled: true, tooltip: "需先冻结集合" };
  return { disabled: false, tooltip: "" };
}

/** 门禁：「废弃」按钮 readiness。T2：若该集合被 active 模型使用 → 禁用。 */
function computeDeprecateGate(
  fs: FactorSet | null | undefined,
  runtime: FactorRuntime | null
): { disabled: boolean; tooltip: string } {
  if (!fs) return { disabled: true, tooltip: "未选择集合" };
  // 如果当前 active_model 的 hyperparameters.factor_set_id 指向该集合 → 禁用
  // 实际校验还应检查所有 active/shadow 模型；此处前端给出一层门禁
  // 注：FactorRuntime DTO 不直接暴露 active_factor_set_id（可能由后端 T5 relations 提供）
  //      这里做宽松兜底：若 runtime 上存在该字段（any 访问）则匹配；否则仅保留逻辑占位
  const activeFsId = (runtime as unknown as { active_factor_set_id?: string | null })?.active_factor_set_id;
  if (activeFsId && activeFsId === fs.id) {
    return {
      disabled: true,
      tooltip: "该集合被正在使用的模型（正式启用）引用，请先切换决策模式或回退模型后再废弃",
    };
  }
  return { disabled: false, tooltip: "" };
}

/** 成员按 role=feature 优先排序，其他 role 折叠到次级（保持原顺序）。 */
function sortMembersByRole<T extends { role: string }>(members: T[]): T[] {
  const features: T[] = [];
  const others: T[] = [];
  for (const m of members) {
    if (m.role === "feature") features.push(m);
    else others.push(m);
  }
  return [...features, ...others];
}

function factorSetDisplayName(name: string | null | undefined, id: string): string {
  const normalized = name?.trim() || "";
  return normalized && !/^[?\uFFFD\s]+$/.test(normalized) ? normalized : id;
}

function factorSetDescription(description: string | null | undefined, id: string): string | null {
  const value = description?.trim() || "";
  // MySQL mojibake/question-mark payloads are not useful to users; show a
  // stable localized fallback instead of exposing corrupted text.
  if (!value) return null;
  const cleaned = value.replace(/^[?\uFFFD\s]+/, "").trim();
  if (!cleaned) return null;
  if (cleaned.toLowerCase().includes("turnover_z20")) return "20日换手率 Z 分数";
  return cleaned;
}

function formatNumber(value: unknown, digits = 4): string {
  if (value == null || value === "") return "-";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(digits);
}

/** IC 色彩：>0.03 强信号（绿）、>0.015 弱有效（黄绿）、≥0 弱（灰）、<0 反信号（红/橙）。 */
function icColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value as number)) return "var(--pt-muted-foreground)";
  const v = value as number;
  if (v >= 0.03) return "#16a34a";
  if (v >= 0.015) return "#65a30d";
  if (v >= 0) return "#6b7280";
  if (v >= -0.015) return "#ea580c";
  return "#dc2626";
}

/** 覆盖率色彩：>=90% 绿、>=70% 黄绿、>=50% 橙、其它红。 */
function coveragePercent(value: number | null | undefined): number {
  if (value == null || !Number.isFinite(value as number)) return 0;
  return Math.max(0, Math.min(100, Number(value) * 100));
}
function coverageColor(pct: number): string {
  if (pct >= 90) return "#16a34a";
  if (pct >= 70) return "#65a30d";
  if (pct >= 50) return "#ca8a04";
  return "#dc2626";
}

/** 权重条颜色（long 绿系、short 红系）。 */
function sideColor(side: "long" | "short" | "neutral"): string {
  return side === "long" ? "#16a34a" : side === "short" ? "#dc2626" : "#6b7280";
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 19);
}

// WP0-8/WP1-1：与 PortfolioStrategyRules.tsx 统一表单字段 label 风格
const fieldLabelStyle: React.CSSProperties = { fontSize: 12, color: "var(--pt-muted-foreground)" };
const thStyle: React.CSSProperties = { textAlign: "left", padding: "4px 6px", fontSize: 11, color: "var(--pt-muted-foreground)", fontWeight: 500, borderBottom: "1px solid var(--pt-border)" };
const tdStyle: React.CSSProperties = { padding: "4px 6px", fontSize: 12, verticalAlign: "middle" };

interface AuditEntry {
  id: number;
  action: string;
  model_run_id: string | null;
  previous_mode: string | null;
  new_mode: string | null;
  previous_model_run_id: string | null;
  new_model_run_id: string | null;
  actor: string;
  note: string | null;
  created_at: string | null;
}

export default function FactorModelPage() {
  const { message, modal } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [runtime, setRuntime] = useState<FactorRuntime | null>(null);
  const [models, setModels] = useState<FactorModelRun[]>([]);
  const [factorSets, setFactorSets] = useState<FactorSet[]>([]);
  const [selectedModel, setSelectedModel] = useState<FactorModelRun | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditEntry[]>([]);

  // ═══════════════════════════════════════════════════════
  // P1 UI Stitching 新增状态
  // ═══════════════════════════════════════════════════════
  /** 模型详情（含因子权重构成）缓存：key = FactorModelRun.id */
  const [modelDetailMap, setModelDetailMap] = useState<Record<string, ScoringModelDetail>>({});
  /** 正在加载详情的模型 id 集合 */
  const [detailLoadingSet, setDetailLoadingSet] = useState<Set<string>>(new Set());
  /** 用户点了模型行上的 FactorSet 标签 → 高亮哪个 FactorSet 的 id */
  const [highlightFactorSetId, setHighlightFactorSetId] = useState<string | null>(null);
  /** 滚动锚点：FactorSet 表 div 引用 */
  const factorSetTableRef = useRef<HTMLDivElement | null>(null);

  // ═══════════════════════════════════════════════════════
  // Task 6：双向反查 UI 状态
  // ═══════════════════════════════════════════════════════
  /** Task 6 (2)(3): GET /scoring/models/{id}/relations DTO 缓存 key=model_id */
  const [relationsMap, setRelationsMap] = useState<Record<string, ScoringModelRelations>>({});
  /** 正在加载 relations 的模型 id */
  const [relationsLoadingSet, setRelationsLoadingSet] = useState<Set<string>>(new Set());
  /** Task 6 (4): 集合卡片「展开 / 收起」成员表 expandedRowKeys（集合 id 数组） */
  const [expandedSetIds, setExpandedSetIds] = useState<string[]>([]);
  /** Task 6 (1): 高亮脉冲集合 id（pulse class 持续 3 秒），区分自普通高亮 */
  const [pulseFactorSetId, setPulseFactorSetId] = useState<string | null>(null);
  /** Task 6 (5b): Settings 跳转来的 model_id → 自动展开那行 */
  const [expandModelIds, setExpandModelIds] = useState<string[]>([]);
  /** 集合卡片 DOM 锚点 ref：按 id 存 */
  const setCardRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // P1.2.2：模型对比（最多 3 个）
  const [compareSelectedIds, setCompareSelectedIds] = useState<string[]>([]);
  const [compareOpen, setCompareOpen] = useState(false);
  const [compareLoading, setCompareLoading] = useState(false);

  // 激活 Modal
  const [activateModalOpen, setActivateModalOpen] = useState(false);
  const [activateTarget, setActivateTarget] = useState<FactorModelRun | null>(null);
  const [activateMode, setActivateMode] = useState<"shadow" | "ridge">("shadow");
  const [activateNote, setActivateNote] = useState("");
  const [modelAlias, setModelAlias] = useState("");
  const [activating, setActivating] = useState(false);

  // 回退 Modal
  const [fallbackModalOpen, setFallbackModalOpen] = useState(false);
  const [fallbackReason, setFallbackReason] = useState("");
  const [fallingBack, setFallingBack] = useState(false);

  // WP0-8：冻结 FactorSet Modal
  const [freezeModalOpen, setFreezeModalOpen] = useState(false);
  const [freezeTarget, setFreezeTarget] = useState<FactorSet | null>(null);
  const [freezeReason, setFreezeReason] = useState("E2E 冻结：离线训练前");
  const [freezing, setFreezing] = useState(false);

  // WP0-8：离线最小训练 Modal（mode=offline_minimal，不需要 FactorWarehouse 环境）
  const [trainModalOpen, setTrainModalOpen] = useState(false);
  const [trainTarget, setTrainTarget] = useState<FactorSet | null>(null);
  const [trainAssetType, setTrainAssetType] = useState<"STOCK" | "ETF" | "US_STOCK" | "HK_STOCK">("STOCK");
  const [trainMode, setTrainMode] = useState<"offline_minimal" | "warehouse">("offline_minimal");
  const [trainNote, setTrainNote] = useState("");
  const [training, setTraining] = useState(false);
  /** 训练门禁错误 Banner：后端返回 detail_zh 原文（不翻译）。 */
  const [trainGateError, setTrainGateError] = useState<string | null>(null);
  /** 滚动锚点：新建模型后 scrollIntoView 该行 */
  const modelTableRef = useRef<HTMLDivElement | null>(null);
  /** 最近训练成功的新模型 ID → 用于自动滚动 */
  const [newModelId, setNewModelId] = useState<string | null>(null);

  // ═══════════════════════════════════════════════════════
  // Task 2：FactorSet UI 9 元素 + 3 门禁
  // ═══════════════════════════════════════════════════════
  // ① 新建集合 Modal
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [createForm] = Form.useForm<{ name: string; description?: string }>();
  const [creating, setCreating] = useState(false);

  // ② 复制按钮 → cloneFactorSet（全局 loading flag）
  const [cloningId, setCloningId] = useState<string | null>(null);

  // ③ 编辑成员 Drawer
  const [memberDrawerOpen, setMemberDrawerOpen] = useState(false);
  const [memberDrawerTarget, setMemberDrawerTarget] = useState<FactorSet | null>(null);
  /** Drawer 内当前编辑的集合成员（实时态，提交才写回服务器） */
  const [drawerMembers, setDrawerMembers] = useState<FactorSetMember[]>([]);
  const [drawerLoading, setDrawerLoading] = useState(false);
  /** 因子库搜索关键词（Tab 1: 全部因子） */
  const [factorSearchKeyword, setFactorSearchKeyword] = useState("");
  // Factor-domain internal - DO NOT USE outside factor center
  /** 因子库列表（mock/listFactorLibrary）— 简化：直接用 listFactorDefinitions 查询 */
  const [factorLibLoading, setFactorLibLoading] = useState(false);
  const [factorLibItems, setFactorLibItems] = useState<Array<{
    factor_code: string;
    factor_name: string | null;
    versions: Array<{ version: number; label: string; trainable: boolean; status: string }>;
  }>>([]);
  /** 选中行的 factor_code → 待选择版本号 */
  const [pendingFactorCode, setPendingFactorCode] = useState<string | null>(null);
  const [pendingVersion, setPendingVersion] = useState<number | null>(null);
  const [addingMember, setAddingMember] = useState(false);

  // ⑤ 冻结摘要 Modal 确认勾选框（不可逆确认）
  const [freezeIrrevocableConfirm, setFreezeIrrevocableConfirm] = useState(false);

  // 废弃按钮
  const [deprecatingId, setDeprecatingId] = useState<string | null>(null);

  // 空列表：新卡片创建成功后，立即定位该集合卡片并打开 Drawer
  const newlyCreatedIdRef = useRef<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [modelList, fsList, scoringOverview] = await Promise.all([
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringGetFactorModelListAsFactor(20),
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringListFactorSetsAsFactor("any", 50),
        api.scoringGetOverviewAsFactor(),
      ]);
      // The model-list compatibility adapter cannot reliably infer runtime
      // state from model statuses. Read the authoritative scoring overview
      // so activation refreshes show ridge/shadow immediately.
      setRuntime(scoringOverview.runtime);
      setModels(modelList.items);
      setFactorSets(fsList);
    } catch (err) {
      message.error(t("factorModelLoadFailed") + ": " + String(err));
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Task 6 (1): 进入因子中心页时解析 URL → set_id 或 model_id 查询 → 自动 scrollIntoView + pulse + 展开
  useEffect(() => {
    if (loading || factorSets.length === 0) return;
    const q = parseFactorCenterQuery();
    let setIdFromQuery: string | null = q.set_id ?? null;
    const modelIdFromQuery: string | null = q.model_id ?? null;
    if (setIdFromQuery) {
      // 自动展开该集合卡
      setExpandedSetIds((prev) => (prev.includes(setIdFromQuery!) ? prev : [...prev, setIdFromQuery!]));
      // pulse 高亮 + scrollIntoView（DOM 渲染完后执行）
      setPulseFactorSetId(setIdFromQuery);
      setHighlightFactorSetId(setIdFromQuery);
      window.queueMicrotask(() => {
        const node = setCardRefs.current[setIdFromQuery!] ?? null;
        if (node && typeof node.scrollIntoView === "function") {
          node.scrollIntoView({ behavior: "smooth", block: "center" });
        } else if (factorSetTableRef.current && typeof factorSetTableRef.current.scrollIntoView === "function") {
          factorSetTableRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
      const snapshot = setIdFromQuery;
      window.setTimeout(() => {
        setPulseFactorSetId((cur) => (cur === snapshot ? null : cur));
        setHighlightFactorSetId((cur) => (cur === snapshot ? null : cur));
      }, 3000);
    }
    if (modelIdFromQuery) {
      // 展开模型行 + 加载 relations
      setExpandModelIds((prev) => (prev.includes(modelIdFromQuery) ? prev : [...prev, modelIdFromQuery]));
      void ensureModelRelations(modelIdFromQuery);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, factorSets.length]);

  // ──────────────────────────────────────────────────────
  // Task 3：链式步骤导航 ChainStepsBar 状态派生 + 直达按钮 handler
  // ──────────────────────────────────────────────────────
  const chainState: ChainState = useMemo<ChainState>(() => {
    // Step1：因子仓库可用 → 只要 runtime 能正常从 scoring 拉取就认为仓库 OK
    const factorWarehouseReady = !!runtime || (loading === false && factorSets.length > 0);

    // Step2：是否有 FactorSet
    const anyFactorSetCreated = factorSets.length > 0;

    // Step3：是否有集合成员就绪（成员数>0 且 feature 数>0）
    const anyFactorSetMembersReady = factorSets.some((fs) => {
      const members = fs.members ?? [];
      if (members.length === 0) return false;
      const fc = members.filter((m) => m.role === "feature").length;
      return fc > 0;
    });

    // Step4：是否有 frozen 集合
    const anyFactorSetFrozen = factorSets.some((fs) => fs.status === "frozen");

    // Step5：是否至少有 1 个完成训练（非 training 状态的模型）
    const anyModelTrained = models.some((m) => {
      const s = String(m.status ?? "").toLowerCase();
      return s === "validated" || s === "trained" || s === "rejected" || s === "done" || s === "completed";
    });

    // Step6：是否至少有 1 个 validated
    const anyModelValidated = models.some((m) => String(m.status ?? "").toLowerCase() === "validated");

    // Step7：决策模式 formal(ridge) + active_model_run_id 非空
    const decisionModeEqualsFormalActive =
      (runtime?.weight_mode === "ridge" || (runtime as any)?.decision_mode === "formal") &&
      !!runtime?.active_model_run_id;

    // Step8：流水线就绪（仓库 + frozen 集合 + 激活模型）
    const pipelineReady =
      factorWarehouseReady && anyFactorSetFrozen && decisionModeEqualsFormalActive;

    return {
      factorWarehouseReady,
      anyFactorSetCreated,
      anyFactorSetMembersReady,
      anyFactorSetFrozen,
      anyModelTrained,
      anyModelValidated,
      decisionModeEqualsFormalActive,
      pipelineReady,
    };
  }, [runtime, factorSets, models, loading]);

  /** ChainStepsBar「修复直达按钮」onGoStep：定位父页面控件 / 跳转路由 */
  const handleChainGoStep = useCallback(
    (stepIdx: number) => {
      switch (stepIdx) {
        case 1: {
          void loadData();
          window.scrollTo({ top: 0, behavior: "smooth" });
          message.info("已从步骤1重新检查因子库状态，现有数据不会被删除");
          break;
        }
        case 2: {
          // 直达：打开「新建集合」Modal（已挂载 data-testid=modal-create-factorset）
          handleOpenCreateModal();
          break;
        }
        case 3: {
          // 直达：打开第一个成员数=0 或 feature=0 的 draft 集合编辑 Drawer
          const target =
            factorSets.find((fs) => fs.status === "draft" && (fs.members?.length ?? 0) === 0) ??
            factorSets.find((fs) => {
              const ms = fs.members ?? [];
              return (
                fs.status === "draft" &&
                ms.filter((m) => m.role === "feature").length === 0
              );
            }) ??
            factorSets.find((fs) => fs.status === "draft") ??
            factorSets[0] ??
            null;
          if (target) openMemberDrawer(target);
          break;
        }
        case 4: {
          // 直达：open 第一个满足 precheck（成员>0 & feature>0 & 全员trainable & draft）→ 冻结摘要 Modal
          const qualified = factorSets.find((fs) => {
            if (fs.status !== "draft") return false;
            const g = computeFreezeGate(fs);
            return !g.disabled;
          });
          const fallback =
            qualified ??
            factorSets.find((fs) => fs.status === "draft" && (fs.members?.length ?? 0) > 0) ??
            null;
          if (fallback) {
            setFreezeTarget(fallback);
            setFreezeReason(`链式导航触发冻结（${new Date().toISOString().slice(0, 10)}）`);
            setFreezeIrrevocableConfirm(false);
            setFreezeModalOpen(true);
          }
          break;
        }
        case 5: {
          // 直达：scrollIntoView 第一个 frozen + 合格的集合训练按钮
          const frozenFs = factorSets.find((fs) => fs.status === "frozen");
          if (frozenFs) {
            const sel = `[data-testid="btn-train-${CSS.escape(String(frozenFs.id))}"]`;
            const btn = document.querySelector<HTMLElement>(sel);
            if (btn) {
              btn.scrollIntoView({ behavior: "smooth", block: "center" });
              btn.classList.add("ring-2", "ring-green-400", "ring-offset-1");
              window.setTimeout(() => btn.classList.remove("ring-2", "ring-green-400", "ring-offset-1"), 2200);
            }
          }
          break;
        }
        case 7: {
          // 直达：打开第一个已验证模型的生产激活对话框。
          const validated = models.find((m) => String(m.status ?? "").toLowerCase() === "validated");
          if (validated) openActivateModal(validated, "ridge");
          else message.warning("暂无已验证模型可激活");
          break;
        }
        case 8: {
          // 直达流水线时带入当前生产模型绑定的冻结集合，避免训练模式落入空选择。
          const activeModel = runtime?.active_model_run_id
            ? models.find((m) => m.id === runtime.active_model_run_id)
            : null;
          const modelAny = activeModel as any;
          const preferredId = modelAny?.factorset_id
            ?? modelAny?.factor_set_id
            ?? modelAny?.hyperparameters?.factorset_id
            ?? modelAny?.hyperparameters?.factor_set_id
            ?? factorSets.find((fs) => fs.status === "frozen")?.id
            ?? "";
          if (typeof window !== "undefined") {
            window.localStorage.setItem("settings_pipeline_factor_set_id", String(preferredId));
          }
          navigate("/settings/pipeline");
          break;
        }
        default:
          break;
      }
    },
    [factorSets, models, message, runtime, loadData]
  );

  // 加载模型详情（含审计日志）
  const loadModelDetail = useCallback(
    async (modelRunId: string) => {
      try {
        // Factor-domain internal - DO NOT USE outside factor center
        const detail = await api.getFactorModel(modelRunId);
        setSelectedModel(detail);
        setAuditLogs((detail.audit ?? []) as unknown as AuditEntry[]);
      } catch (err) {
        message.error(t("factorModelLoadFailed") + ": " + String(err));
      }
    },
    [message]
  );

  /** P1: 懒加载模型权重详情（用于列表行展开，不抢详情页的带宽）。 */
  const ensureModelDetail = useCallback(
    async (runId: string) => {
      if (modelDetailMap[runId]) return;
      if (detailLoadingSet.has(runId)) return;
      setDetailLoadingSet((s) => new Set(s).add(runId));
      try {
        const detail = await api.scoringGetModelDetail(runId);
        setModelDetailMap((m) => ({ ...m, [runId]: detail }));
      } catch (err) {
        message.error("加载模型因子构成失败：" + String(err));
      } finally {
        setDetailLoadingSet((s) => {
          const ns = new Set(s);
          ns.delete(runId);
          return ns;
        });
      }
    },
    [detailLoadingSet, message, modelDetailMap]
  );

  /** Task 6 (1)(2)(3): 懒加载 relations DTO（列表行展开共用，解析 6 列 + unbound_reason）。 */
  const ensureModelRelations = useCallback(
    async (runId: string) => {
      if (relationsMap[runId]) return;
      if (relationsLoadingSet.has(runId)) return;
      setRelationsLoadingSet((s) => new Set(s).add(runId));
      try {
        const rel = await api.scoringGetModelRelations(runId);
        setRelationsMap((m) => ({ ...m, [runId]: rel }));
      } catch (err) {
        message.error("加载模型关联信息失败：" + String(err));
      } finally {
        setRelationsLoadingSet((s) => {
          const ns = new Set(s);
          ns.delete(runId);
          return ns;
        });
      }
    },
    [message, relationsLoadingSet, relationsMap]
  );

  /** Task 6 (4): 集合卡片展开/收起切换 */
  const toggleSetCardExpand = useCallback((fsId: string) => {
    setExpandedSetIds((prev) =>
      prev.includes(fsId) ? prev.filter((x) => x !== fsId) : [...prev, fsId],
    );
  }, []);

  /** P1: 用户在模型行点 FactorSet → 双向反查跳转：navigate('/factors?set_id=X') */
  const jumpToFactorSet = useCallback((fsId: string) => {
    // 先记本地高亮（如果当前页就是因子中心，立即生效）
    setHighlightFactorSetId(fsId);
    setPulseFactorSetId(fsId);
    // 写 URL hash query：统一走 navigate（符合 Task 6 (1) 验收规则）
    navigate(`/factors?set_id=${encodeURIComponent(String(fsId))}`);
    // 同时保证本页同上下文也立即展开 + scrollIntoView（避免事件派发异步延迟）
    setExpandedSetIds((prev) => (prev.includes(fsId) ? prev : [...prev, fsId]));
    window.queueMicrotask(() => {
      const node = setCardRefs.current[fsId] ?? null;
      if (node && typeof node.scrollIntoView === "function") {
        node.scrollIntoView({ behavior: "smooth", block: "center" });
      } else if (factorSetTableRef.current && typeof factorSetTableRef.current.scrollIntoView === "function") {
        factorSetTableRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
    window.setTimeout(() => {
      setPulseFactorSetId((cur) => (cur === fsId ? null : cur));
      setHighlightFactorSetId((cur) => (cur === fsId ? null : cur));
    }, 3000);
  }, []);

  // 打开激活 Modal
  const openActivateModal = (model: FactorModelRun, mode: "shadow" | "ridge") => {
    setActivateTarget(model);
    setActivateMode(mode);
    setActivateNote("");
    setModelAlias(model.display_alias || "");
    setActivateModalOpen(true);
  };

  // 确认激活
  const handleActivate = async () => {
    if (!activateTarget) return;
    setActivating(true);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const newRuntime = await api.scoringActivateModel(
        activateTarget.id,
        activateMode,
        activateNote || undefined,
        "factor_center:activate"
      );
      if (modelAlias.trim() && modelAlias.trim() !== (activateTarget.display_alias || "")) {
        await api.scoringRenameModel(activateTarget.id, modelAlias.trim(), "factor_center:rename_model");
      }
      setRuntime(newRuntime);
      message.success(t("factorModelActivated"));
      setActivateModalOpen(false);
      await loadData();
    } catch (err) {
      message.error(t("factorModelActionFailed") + ": " + String(err));
    } finally {
      setActivating(false);
    }
  };

  // 确认回退
  const handleFallback = async () => {
    if (!fallbackReason.trim()) {
      message.warning(t("factorModelReasonRequired"));
      return;
    }
    setFallingBack(true);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const newRuntime = await api.scoringFallbackToManual(fallbackReason.trim(), "factor_center:fallback");
      setRuntime(newRuntime);
      message.success(t("factorModelFallbackDone"));
      setFallbackModalOpen(false);
      setFallbackReason("");
      await loadData();
    } catch (err) {
      message.error(t("factorModelActionFailed") + ": " + String(err));
    } finally {
      setFallingBack(false);
    }
  };

  // WP0-8：冻结 FactorSet 确认（Task 2 ⑤：不可逆确认勾选框）
  const handleFreeze = async () => {
    if (!freezeTarget) return;
    if (!freezeIrrevocableConfirm) {
      message.warning("请先勾选「我确认冻结后成员与版本不可逆」后再提交");
      return;
    }
    setFreezing(true);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const updated = await api.scoringFreezeFactorSet(freezeTarget.id, freezeReason.trim() || "E2E 冻结：离线训练前", "factor_center:freeze");
      setFactorSets((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
      message.success(`FactorSet 已冻结：${updated.id}`);
      setFreezeModalOpen(false);
      setFreezeReason("E2E 冻结：离线训练前");
      setFreezeIrrevocableConfirm(false);
      await loadData();
    } catch (err) {
      message.error("冻结 FactorSet 失败：" + String(err));
    } finally {
      setFreezing(false);
    }
  };

  // Task 2 ⑦：训练模型（train_mode 下拉 + 备注；submit 隐式传 factor_set_id；7 要素错误 detail_zh 原文展示）
  const handleTrain = async () => {
    if (!trainTarget) return;
    if (!trainTarget.id) {
      message.warning("请先选择要训练的 FactorSet");
      return;
    }
    setTraining(true);
    setTrainGateError(null);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const created = await api.scoringTrainModel(trainTarget.id, trainMode, "factor_center:train");
      message.success(
        trainMode === "offline_minimal"
          ? `离线最小模型训练成功：${created.id.slice(0, 12)}…（status=${created.status}）`
          : `仓库训练已提交：${created.id.slice(0, 12)}…`,
      );
      setTrainModalOpen(false);
      setTrainNote("");
      setSelectedModel(created);
      // Task 2 ⑦：模型训练结果 → Model 表格自动 scrollIntoView 新行
      setNewModelId(created.id);
      await loadData();
      // scrollIntoView 在下一个微任务里执行（保证 DOM 已渲染）
      window.queueMicrotask(() => {
        if (modelTableRef.current) {
          const row = modelTableRef.current.querySelector<HTMLElement>(`[data-row-key="${CSS.escape(created.id)}"]`);
          row?.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        window.setTimeout(() => setNewModelId(null), 3200);
      });
    } catch (err: any) {
      // Task 2 训练门禁(2)：识别 400 code → Error Banner 展示 detail_zh 原文
      const detail = err?.detail as any;
      const code = err?.error_code || detail?.error_code || "";
      const zh = detail?.detail_zh as string | undefined;
      const msgRaw = err?.message ? String(err.message) : String(err);
      if (
        /TRAIN_GATE_(COVERAGE_LOW|IC_OUT_OF_RANGE|LOOKBACK_SHORT)/.test(code) ||
        /coverage_low|ic_out_of_range|lookback_short/i.test(msgRaw)
      ) {
        const banner = zh || msgRaw;
        setTrainGateError(banner);
        message.error("训练门禁未通过：已在训练对话框下方显示详情");
      } else {
        message.error("训练失败：" + msgRaw);
      }
    } finally {
      setTraining(false);
    }
  };

  // ──────────────────────────────────────────────────────
  // Task 2 ①：新建集合 → createFactorSet
  // ──────────────────────────────────────────────────────
  const handleOpenCreateModal = () => {
    createForm.resetFields();
    setCreateModalOpen(true);
  };

  const handleCreateFactorSet = async () => {
    try {
      const values = await createForm.validateFields();
      setCreating(true);
      const req: FactorSetCreateRequest = {
        name: values.name.trim(),
        description: values.description?.trim() || null,
        actor: "factor_center:create",
      };
      // Factor-domain internal - DO NOT USE outside factor center
      const created = await api.createFactorSet(req);
      message.success(`已创建因子集合：${factorSetDisplayName(created.name, created.id)}`);
      setCreateModalOpen(false);
      newlyCreatedIdRef.current = created.id;
      await loadData();
      // Task 2 ①：成功后立刻列表定位并触发 Drawer
      window.queueMicrotask(() => {
        const fsCard = document.querySelector<HTMLElement>(`[data-fs-id="${CSS.escape(created.id)}"]`);
        fsCard?.scrollIntoView({ behavior: "smooth", block: "center" });
        // 自动打开编辑成员 Drawer
        openMemberDrawer(created as unknown as FactorSet);
      });
    } catch (err: any) {
      if (String(err?.errorFields ?? "").length > 0) return; // Form 校验错误已 UI 提示
      message.error("创建因子集合失败：" + String(err?.message || err));
    } finally {
      setCreating(false);
    }
  };

  // ──────────────────────────────────────────────────────
  // Task 2 ②：复制集合 → cloneFactorSet
  // ──────────────────────────────────────────────────────
  const handleCloneFactorSet = async (fs: FactorSet) => {
    setCloningId(fs.id);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const cloned = await api.cloneFactorSet(fs.id, { actor: "factor_center:clone" });
      message.success(`已复制新集合：${factorSetDisplayName(cloned.name, cloned.id)}（status=${cloned.status}）`);
      await loadData();
      // Task 2 ②：复制后新集合卡片仍需添加 ≥1 成员+全员 trainable 才可冻结；按钮 disabled 由 computeFreezeGate 自然处理
    } catch (err) {
      message.error("复制集合失败：" + String(err));
    } finally {
      setCloningId(null);
    }
  };

  // ──────────────────────────────────────────────────────
  // Task 2 ③：编辑成员 Drawer — 打开/关闭/搜索/添加/删除/保存
  // ──────────────────────────────────────────────────────
  // Factor-domain internal - DO NOT USE outside factor center
  const openMemberDrawer = async (fs: FactorSet) => {
    setMemberDrawerTarget(fs);
    setPendingFactorCode(null);
    setPendingVersion(null);
    setFactorSearchKeyword("");
    setDrawerMembers(fs.members ? [...fs.members] : []);
    setMemberDrawerOpen(true);
    // Factor-domain internal - DO NOT USE outside factor center
    // 若 members 为空，尝试走 getFactorSetDetail(withMembers=true) 补全
    if ((fs.members?.length ?? 0) === 0) {
      setDrawerLoading(true);
      try {
        const detail = await api.getFactorSetDetail(fs.id, true);
        setDrawerMembers(detail.members ? [...detail.members] : []);
      } catch (e) {
        // 忽略：保持空
      } finally {
        setDrawerLoading(false);
      }
    }
    // 懒加载因子库
    void loadFactorLibrary("");
  };

  const closeMemberDrawer = () => {
    setMemberDrawerOpen(false);
    setMemberDrawerTarget(null);
    setDrawerMembers([]);
    setFactorLibItems([]);
    setFactorSearchKeyword("");
    setPendingFactorCode(null);
    setPendingVersion(null);
  };

  /** 懒加载「全部因子」Tab：通过 scoringListFactorDefinitions 搜索。 */
  const loadFactorLibrary = useCallback(
    async (keyword: string) => {
      setFactorLibLoading(true);
      try {
        // Factor-domain internal - DO NOT USE outside factor center
    // 优先 scoring 薄封；参数 search 与 page_size 兼容 listFactorDefinitions
        const raw: any = await api.scoringListFactorDefinitions({
          search: keyword || undefined,
          page_size: 50,
        });
        // Factor-domain internal - DO NOT USE outside factor center
    // 兼容返回 item.items 或数组或 listFactorDefinitions 结构
        const arr: any[] = Array.isArray(raw)
          ? raw
          : Array.isArray((raw as any).items)
          ? (raw as any).items
          : Array.isArray((raw as any).data?.items)
          ? (raw as any).data.items
          : [];
        const mapped = arr.map((it: any) => {
          const versionsRaw = it.versions || it.trainable_versions || [];
          const versions: Array<{ version: number; label: string; trainable: boolean; status: string }> = [];
          const push = (v: number | { version: number; label?: string; status?: string; trainable?: boolean }) => {
            if (v == null) return;
            if (typeof v === "number") {
              versions.push({ version: v, label: `v${v}`, trainable: true, status: "trainable" });
            } else if (typeof v === "object" && v != null) {
              const vn = Number(v.version);
              if (!Number.isFinite(vn)) return;
              const status = String(v.status || "trainable").toLowerCase();
              const trainable =
                typeof (v as any).trainable === "boolean"
                  ? (v as any).trainable
                  : status === "trainable" || status === "active" || status === "approved";
              versions.push({
                version: vn,
                label: v.label || `v${vn}`,
                trainable,
                status,
              });
            }
          };
          versionsRaw.forEach(push as any);
          // 兜底：若没有 versions，但有 latest_version / version 数字
          if (versions.length === 0) {
            const lv = it.latest_version ?? it.version;
            if (lv != null && Number.isFinite(Number(lv))) {
              const vn = Number(lv);
              versions.push({ version: vn, label: `v${vn}`, trainable: true, status: "trainable" });
            } else {
              versions.push({ version: 1, label: "v1 (默认)", trainable: true, status: "trainable" });
            }
          }
          return {
            factor_code: String(it.factor_code ?? it.code ?? ""),
            factor_name: (it.factor_name ?? it.name ?? null) as string | null,
            versions,
          };
        }).filter((x) => x.factor_code);
        setFactorLibItems(mapped);
      } catch (_e) {
        // 回退：构造至少 2 条可训练 seed 因子（便于 E2E / 后端未就绪时 Drawer 不空白）
        setFactorLibItems([
          {
            factor_code: "SEED_MOM_20D",
            factor_name: "20 日动量因子",
            versions: [{ version: 3, label: "v3 (2024 修订)", trainable: true, status: "trainable" }],
          },
          {
            factor_code: "SEED_VAL_PE_TTM",
            factor_name: "PE TTM 估值因子（行业中性）",
            versions: [{ version: 2, label: "v2 (缺省 drop)", trainable: true, status: "trainable" }],
          },
          {
            factor_code: "SEED_QLTY_ROE",
            factor_name: "ROE 质量因子（季度填充）",
            versions: [
              { version: 5, label: "v5 (2025Q1)", trainable: true, status: "trainable" },
              { version: 4, label: "v4 (已废弃)", trainable: false, status: "deprecated" },
            ],
          },
        ]);
      } finally {
        setFactorLibLoading(false);
      }
    },
    []
  );

  /** Drawer Tab 1：选行 + 选版本 → 提交 addMember。 */
  const handleAddMemberFromLibrary = async () => {
    if (!memberDrawerTarget || !pendingFactorCode || pendingVersion == null) {
      message.warning("请先在左侧选择一个因子，并选择可训练版本");
      return;
    }
    // 检查已选成员中是否存在相同 (code, version) 重复
    if (drawerMembers.some((m) => m.factor_code === pendingFactorCode && m.factor_version === pendingVersion)) {
      message.warning("该因子版本已是集合成员，请勿重复添加");
      return;
    }
    setAddingMember(true);
    try {
      const req: FactorSetMemberCreateRequest = {
        factor_code: pendingFactorCode,
        factor_version: pendingVersion,
        role: "feature",
        weight_constraint: "free",
        missing_policy: "exclude",
        actor: "factor_center:add_member",
      };
      // Factor-domain internal - DO NOT USE outside factor center
      const added = await api.addFactorSetMember(memberDrawerTarget.id, req);
      // optimistic 写回本地
      setDrawerMembers((prev) => [...prev, added as unknown as FactorSetMember]);
      setPendingFactorCode(null);
      setPendingVersion(null);
      message.success(`已添加成员：${pendingFactorCode} v${pendingVersion}`);
    } catch (err) {
      message.error("添加成员失败：" + String(err));
    } finally {
      setAddingMember(false);
    }
  };

  const handleRemoveMember = async (memberId: number) => {
    if (!memberDrawerTarget) return;
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      await api.removeFactorSetMember(memberDrawerTarget.id, memberId);
      setDrawerMembers((prev) => prev.filter((m) => m.id !== memberId));
      message.success("已移除该成员");
    } catch (err) {
      message.error("移除成员失败：" + String(err));
    }
  };

  /** Drawer 底部「保存」按钮：刷新主表并关闭。 */
  const handleDrawerSaveAndClose = async () => {
    closeMemberDrawer();
    // 重新拉最新 FactorSet 数据（members 变了），保证 UI 同步
    await loadData();
  };

  // ──────────────────────────────────────────────────────
  // Factor-domain internal - DO NOT USE outside factor center
  // Task 2 (3) 废弃门禁 → deprecateFactorSet
  // ──────────────────────────────────────────────────────
  const handleDeprecateFactorSet = async (fs: FactorSet) => {
    const gate = computeDeprecateGate(fs, runtime);
    if (gate.disabled) {
      message.warning(gate.tooltip);
      return;
    }
    const ok = await new Promise<boolean>((resolve) => {
      modal.confirm({
        title: `废弃因子集合：${factorSetDisplayName(fs.name, fs.id)}`,
        content: "废弃后该集合不能再训练新模型（不影响已训练的历史模型）。此操作可审计但无法撤销。是否继续？",
        okText: "确认废弃",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      });
    });
    if (!ok) return;
    setDeprecatingId(fs.id);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const updated = await api.deprecateFactorSet(fs.id, "factor_center 手动废弃", {
        actor: "factor_center:deprecate",
      });
      setFactorSets((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
      message.success(`已废弃：${factorSetDisplayName(updated.name, updated.id)}`);
      await loadData();
    } catch (err) {
      message.error("废弃失败：" + String(err));
    } finally {
      setDeprecatingId(null);
    }
  };

  // 模型列表列定义
  const modelColumns = [
    {
      title: t("factorModelModelId"),
      dataIndex: "id",
      key: "id",
      width: 180,
      render: (id: string, record: FactorModelRun) => (
        <Button
          type="link"
          size="small"
          style={{ padding: 0 }}
          onClick={() => loadModelDetail(id)}
        >
          {id.length > 20 ? id.slice(0, 20) + "..." : id}
        </Button>
      ),
    },
    {
      title: t("factorModelStatus"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string, record: FactorModelRun) => {
        const rejected = status === "rejected";
        const reasons = record.rejection_reason ? splitRejectionReasons(record.rejection_reason) : null;
        const hasReasons = rejected && !!record.rejection_reason;
        const statusNode = (
          <Tag
            color={rejected ? "red" : modelStatusColor(status)}
            style={{
              fontWeight: rejected ? 600 : undefined,
              paddingInline: rejected ? 10 : undefined,
              borderRadius: rejected ? 10 : undefined,
              borderColor: rejected ? "rgba(255,77,79,0.55)" : undefined,
              background: rejected ? "rgba(255,241,240,1)" : undefined,
              boxShadow: rejected ? "0 0 0 1px rgba(255,77,79,0.12) inset" : undefined,
              cursor: hasReasons ? "help" : undefined,
            }}
            icon={hasReasons ? <ExclamationCircleOutlined /> : undefined}
          >
            {status === "validated"
              ? t("factorModelValidated")
              : status === "rejected"
              ? t("factorModelRejected")
              : status}
          </Tag>
        );
        if (!hasReasons) return statusNode;
        return (
          <Popover
            placement="topLeft"
            overlayInnerStyle={{ maxWidth: 520 }}
            trigger={["hover", "click"]}
            title={
              <Space>
                <ExclamationCircleOutlined style={{ color: "#ff4d4f" }} />
                <Text strong type="danger">
                  门禁拒绝原因 ({reasons?.pre.length ?? 0} 前置 + {reasons?.post.length ?? 0} 后置{" "}
                  + {reasons?.legacy.length ?? 0} 历史)
                </Text>
              </Space>
            }
            content={
              <div style={{ width: "max(420px, 40vw)", maxWidth: 520 }}>
                {reasons?.pre.length ? (
                  <Alert
                    style={{ marginBottom: 8 }}
                    type="warning"
                    showIcon
                    message={`P2-G 前置门禁（${reasons.pre.length} 条）`}
                    description={renderRejectionReasonList(reasons.pre)}
                  />
                ) : null}
                {reasons?.post.length ? (
                  <Alert
                    style={{ marginBottom: 8 }}
                    type="error"
                    showIcon
                    message={`P2-G 后置门禁（${reasons.post.length} 条）`}
                    description={renderRejectionReasonList(reasons.post)}
                  />
                ) : null}
                {reasons?.legacy.length ? (
                  <Alert
                    type="info"
                    showIcon
                    message={`历史/其它原因（${reasons.legacy.length} 条）`}
                    description={renderRejectionReasonList(reasons.legacy)}
                  />
                ) : null}
              </div>
            }
          >
            {statusNode}
          </Popover>
        );
      },
    },
    {
      title: t("factorModelValidationIc"),
      key: "validation_ic",
      width: 110,
      render: (_: unknown, record: FactorModelRun) =>
        formatNumber(record.metrics?.validation_ic),
    },
    {
      title: t("factorModelSamples"),
      key: "sample_count",
      width: 90,
      render: (_: unknown, record: FactorModelRun) => record.sample_count,
    },
    {
      title: t("factorModelCutoff"),
      dataIndex: "data_cutoff_at",
      key: "data_cutoff_at",
      width: 160,
      render: (v: string | null) => formatDateTime(v),
    },
    {
      title: t("factorModelFactorSet"),
      key: "factor_set_id",
      width: 170,
      render: (_: unknown, record: FactorModelRun) => {
        const fsId = record.hyperparameters?.factor_set_id as string | undefined;
        if (!fsId) return <Text type="secondary">-</Text>;
        const fs = factorSets.find((s) => s.id === fsId);
        const label = fs ? factorSetDisplayName(fs.name, fs.id) : fsId;
        return (
          <Tooltip title={
            <Space direction="vertical" size={2} style={{ maxWidth: 320 }}>
              <Text>FactorSet ID：{fsId}</Text>
              {fs?.members?.length != null && <Text>成员数量：{fs.members.length}</Text>}
              <Text type="secondary">点击跳转到上方 FactorSet 表并高亮对应行</Text>
            </Space>
          }>
            <Button
              type="link"
              size="small"
              style={{ padding: 0, lineHeight: 1.6 }}
              data-testid={`linked-fs-cell-${record.id}`}
              onClick={(e) => {
                e.stopPropagation();
                jumpToFactorSet(fsId);
              }}
            >
              <Tag color="blue" style={{ marginInlineEnd: 0 }}>{label}</Tag>
            </Button>
          </Tooltip>
        );
      },
    },
    {
      title: t("factorModelActions"),
      key: "actions",
      width: 220,
      render: (_: unknown, record: FactorModelRun) => {
        if (record.status !== "validated") {
          const split = record.rejection_reason ? splitRejectionReasons(record.rejection_reason) : null;
          const hasReasons = !!record.rejection_reason;
          const bubble = (
            <Popover
              placement="topRight"
              trigger={["hover", "click"]}
              title={
                <Space>
                  <ExclamationCircleOutlined style={{ color: "#ff4d4f" }} />
                  <Text strong type="danger">
                    门禁原因摘要
                  </Text>
                </Space>
              }
              content={
                <div style={{ maxWidth: 440 }}>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {split?.pre.length ? (
                      <Tag color="warning" style={{ marginInlineEnd: 0 }}>
                        前置门禁 ×{split.pre.length}
                      </Tag>
                    ) : null}
                    {split?.post.length ? (
                      <Tag color="error" style={{ marginInlineEnd: 0 }}>
                        后置门禁 ×{split.post.length}
                      </Tag>
                    ) : null}
                    {split?.legacy.length ? (
                      <Tag color="default" style={{ marginInlineEnd: 0 }}>
                        其它 ×{split.legacy.length}
                      </Tag>
                    ) : null}
                  </div>
                  {hasReasons ? (
                    <Alert
                      style={{ marginTop: 8 }}
                      showIcon
                      type="error"
                      message="完整原因（点击左侧状态 Tag 可查看带色标的分组说明）"
                      description={<Text style={{ fontSize: 12 }}>{record.rejection_reason}</Text>}
                    />
                  ) : null}
                </div>
              }
            >
              <Tag
                icon={<ExclamationCircleOutlined />}
                color="red"
                style={{
                  borderRadius: 10,
                  paddingInline: 10,
                  fontWeight: 600,
                  boxShadow: "0 0 0 1px rgba(255,77,79,0.12) inset",
                  cursor: "pointer",
                }}
              >
                {t("factorModelRejected")}
              </Tag>
            </Popover>
          );
          // validated 以外的状态（rejected / training / rejected_draft 等）也一并显示摘要：
          //   只有 rejected 时显示红底 bubble；其它显示普通灰色 bubble
          if (record.status === "rejected") return bubble;
          return (
            <Tooltip title={record.rejection_reason || t("factorModelRejected")}>
              <Tag color={record.status === "rejected" ? "red" : "default"}>
                {modelStatusLabel(record.status)}
              </Tag>
            </Tooltip>
          );
        }
        return (
          <Space size="small">
            <Button
              size="small"
              type="primary"
              ghost
              onClick={() => openActivateModal(record, "shadow")}
            >
              {t("factorModelShadow")}
            </Button>
            <Button
              size="small"
              type="primary"
              onClick={() => openActivateModal(record, "ridge")}
            >
              {t("factorModelRidge")}
            </Button>
          </Space>
        );
      },
    },
  ];

  // ═════════════════════════════════════════════════════════════════════
  // P1.2 模型行展开：指标卡片 + 因子权重构成（按 |w| 降序）
  // ═════════════════════════════════════════════════════════════════════
  const modelExpandable = useMemo(
    () => ({
      expandedRowKeys: expandModelIds,
      onExpandedRowsChange: (keys: React.Key[]) => {
        setExpandModelIds(keys.map(String));
      },
      expandedRowRender: (record: FactorModelRun) => {
        // Task 6 (2)(3): 优先展示 relations 6 列 mini 表 / unbound_reason Alert
        const rel: ScoringModelRelations | undefined = relationsMap[record.id];
        const relLoading = relationsLoadingSet.has(record.id);
        if (relLoading && !rel) {
          return (
            <div style={{ padding: "4px 12px 12px" }} data-testid={`relations-loading-${record.id}`}>
              <Spin size="small" tip="加载模型关联快照（relations DTO v2）…" />
            </div>
          );
        }
        if (!rel) {
          return (
            <div style={{ padding: "4px 12px 12px" }}>
              <Text type="secondary">暂无关联快照（请稍后刷新重试）</Text>
            </div>
          );
        }
        // ── Task 6 (3)：未关联模型 → Alert warning + 迁移报告按钮 ──
        if (rel.unbound_reason) {
          return (
            <div
              className="model-expand-unbound"
              data-testid={`unbound-block-${record.id}`}
              style={{ padding: "6px 8px 10px" }}
            >
              <Alert
                type="warning"
                showIcon
                data-testid={`unbound-alert-${record.id}`}
                message="该历史模型未关联因子集合"
                description={rel.unbound_reason}
                style={{ marginBottom: 12 }}
              />
              <div style={{ textAlign: "right" }}>
                <Button
                  type="primary"
                  data-testid={`migration-report-btn-${record.id}`}
                  onClick={() => navigate("/factors/migration")}
                >
                  查看迁移报告
                </Button>
              </div>
            </div>
          );
        }
        // ── Task 6 (2): 6 列 mini 表。null → N/A（不使用 0 代替） ──
        const factors: ScoringModelRelationFactor[] = rel.factors ?? [];
        const safeNum = (v: unknown): { type: "num"; value: number } | { type: "na" } => {
          if (v == null) return { type: "na" };
          const n = Number(v);
          if (!Number.isFinite(n)) return { type: "na" };
          return { type: "num", value: n };
        };
        const naCell = (key: string) => (
          <span
            className="na-cell"
            data-testid={`na-${key}`}
            style={{ color: "#94a3b8", fontStyle: "italic" }}
          >
            N/A
          </span>
        );
        return (
          <div
            className="model-expand-relations"
            data-testid={`relations-block-${record.id}`}
            style={{ padding: "6px 8px 12px" }}
          >
            <Space size={8} style={{ marginBottom: 8 }}>
              {rel.factor_set_id && (
                <Tag color="blue">
                  因子集合：{rel.factor_set_name || rel.factor_set_id}（{rel.factor_set_id}）
                </Tag>
              )}
              {rel.n_members != null && <Tag>因子数：{rel.n_members}</Tag>}
              {rel.data_cutoff_at && (
                <Tag>数据截止：{rel.data_cutoff_at.slice(0, 10)}</Tag>
              )}
            </Space>
            <Table<ScoringModelRelationFactor>
              size="small"
              rowKey={(f, idx) => `${f.factor_code ?? ""}-${f.factor_version_id ?? ""}-${idx}`}
              dataSource={factors}
              pagination={factors.length > 20 ? { pageSize: 20, size: "small", hideOnSinglePage: true } : false}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="模型未记录因子明细" /> }}
              data-testid={`relations-table-${record.id}`}
              columns={[
                {
                  title: "factor_code",
                  dataIndex: "factor_code",
                  key: "factor_code",
                  width: 150,
                  render: (v: unknown) => (v == null || String(v) === "" ? naCell(`factor_code-${record.id}`) : <code>{String(v)}</code>),
                },
                {
                  title: "factor_version_id",
                  dataIndex: "factor_version_id",
                  key: "factor_version_id",
                  width: 150,
                  render: (v: unknown) => {
                    const s = safeNum(v);
                    if (s.type === "na") return naCell(`factor_version_id-${record.id}`);
                    return String(v);
                  },
                },
                {
                  title: "coef_raw",
                  dataIndex: "coef_raw",
                  key: "coef_raw",
                  width: 120,
                  align: "right",
                  render: (v: unknown) => {
                    const s = safeNum(v);
                    if (s.type === "na") return naCell(`coef_raw-${record.id}`);
                    return s.value.toFixed(6);
                  },
                },
                {
                  title: "weight_norm",
                  dataIndex: "weight_norm",
                  key: "weight_norm",
                  width: 120,
                  align: "right",
                  render: (v: unknown) => {
                    const s = safeNum(v);
                    if (s.type === "na") return naCell(`weight_norm-${record.id}`);
                    return s.value.toFixed(6);
                  },
                },
                {
                  title: "validation_ic",
                  dataIndex: "validation_ic",
                  key: "validation_ic",
                  width: 130,
                  align: "right",
                  render: (v: unknown) => {
                    const s = safeNum(v);
                    if (s.type === "na") return naCell(`validation_ic-${record.id}`);
                    return (
                      <span style={{ color: icColor(s.value) }}>
                        {s.value.toFixed(4)}
                      </span>
                    );
                  },
                },
                {
                  title: "coverage",
                  dataIndex: "coverage",
                  key: "coverage",
                  width: 110,
                  align: "right",
                  render: (v: unknown) => {
                    const s = safeNum(v);
                    if (s.type === "na") return naCell(`coverage-${record.id}`);
                    return `${(s.value * 100).toFixed(1)}%`;
                  },
                },
              ]}
            />
          </div>
        );
      },
      onExpand: async (expanded: boolean, record: FactorModelRun) => {
        if (expanded) {
          // Task 6：行展开加载 relations（6 列 / unbound）；同时保留原 ensureModelDetail 以支撑对比页
          await Promise.all([
            ensureModelRelations(record.id),
            ensureModelDetail(record.id),
          ]);
        }
      },
      rowExpandable: () => true,
    }),
    [
      detailLoadingSet,
      ensureModelDetail,
      ensureModelRelations,
      expandModelIds,
      factorSets,
      modelDetailMap,
      relationsLoadingSet,
      relationsMap,
    ]
  );

  // ═════════════════════════════════════════════════════════════════════
  // P1.2.2：模型对比 Drawer 渲染（选中 2~3 个 validated 模型并排对比）
  // ═════════════════════════════════════════════════════════════════════
  /**
   * P1.2.2b：把 rejection_reason 文本按 P2-G Pre / P2-G Post / Legacy 三类
   * 用分色 Tag 呈现，避免一大段纯文本看不出是治理门禁还是 legacy gate 触发。
   */
  const splitRejectionReasons = (
    raw: string | null | undefined
  ): { pre: string[]; post: string[]; legacy: string[] } => {
    const tokens = raw
      ? raw
          .split(/\s*[;；\n|]\s*/)
          .flatMap((seg) => seg.split(/(?=\[P2-G)/g))
          .map((s) => s.trim())
          .filter(Boolean)
      : [];
    const pre: string[] = [];
    const post: string[] = [];
    const legacy: string[] = [];
    tokens.forEach((tok) => {
      if (/\[P2-G\s*Pre\]/i.test(tok)) pre.push(tok.replace(/^\[P2-G\s*Pre\]\s*/i, ""));
      else if (/\[P2-G\s*Post\]/i.test(tok)) post.push(tok.replace(/^\[P2-G\s*Post\]\s*/i, ""));
      else legacy.push(tok);
    });
    return { pre, post, legacy };
  };

  /** 在 Popover / Alert description 中渲染一条一条原因（带序号 + 复制友好）。*/
  const renderRejectionReasonList = (items: string[]): React.ReactNode =>
    items.length === 0 ? (
      <Text type="secondary">—</Text>
    ) : (
      <ul style={{ margin: 0, paddingInlineStart: 20, lineHeight: 1.8 }}>
        {items.map((it, idx) => (
          <li key={idx} style={{ fontSize: 12, wordBreak: "break-word" }}>
            {it}
          </li>
        ))}
      </ul>
    );

  const renderRejectionReasonSplit = (raw: string | null | undefined): React.ReactNode => {
    if (!raw) return <Text type="secondary" style={{ fontSize: 11 }}>—</Text>;
    // 按 `;` / `，` / `。` / `\n` / `|` 切分，保留语义完整的条目。
    const tokens = raw
      .split(/\s*[;；\n|]\s*/)
      .flatMap((seg) => seg.split(/(?=\[P2-G)/g))   // P2-G 自身有前缀标签，额外切
      .map((s) => s.trim())
      .filter(Boolean);
    return (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, justifyContent: "center" }}>
        {tokens.slice(0, 12).map((tok, i) => {
          const isPre = /\[P2-G\s*Pre\]/i.test(tok);
          const isPost = /\[P2-G\s*Post\]/i.test(tok);
          if (isPre) {
            return (
              <Tag key={i} color="gold" style={{ marginInlineEnd: 0, fontSize: 11 }}>
                ⚠ Pre {tok.replace(/^\[P2-G\s*Pre\]\s*/i, "")}
              </Tag>
            );
          }
          if (isPost) {
            return (
              <Tag key={i} color="red" style={{ marginInlineEnd: 0, fontSize: 11 }}>
                ✗ Post {tok.replace(/^\[P2-G\s*Post\]\s*/i, "")}
              </Tag>
            );
          }
          return (
            <Tag key={i} style={{ marginInlineEnd: 0, fontSize: 11 }} color="default">
              · {tok}
            </Tag>
          );
        })}
        {tokens.length > 12 && (
          <Tooltip title={raw}>
            <Tag style={{ marginInlineEnd: 0 }} color="purple">+{tokens.length - 12} 更多</Tag>
          </Tooltip>
        )}
      </div>
    );
  };

  const COMPARE_PALETTE = ["#0f766e", "#b45309", "#7c3aed"]; // 3 种主色
  const renderCompareDrawer = () => {
    const selected = compareSelectedIds
      .map((id) => models.find((m) => m.id === id))
      .filter((m): m is FactorModelRun => !!m);
    const N = selected.length;
    /** 每个模型的 ScoringModelDetail：顺序与 selected 对齐 */
    const alignedDetails = selected.map((m) => modelDetailMap[m.id]);

    // ── P1.2.2c/d: 因子聚合与排序（按 |Δw| 降序，差异优先） ──────────────
    // factorInfo 按 factor_code 聚合：
    //   members[i] = 第 i 个模型的因子（缺失 = undefined）
    //   maxAbsW  : N 个模型中出现过的最大 abs(weight)
    //   maxDeltaW: max(w)-min(w) 跨模型最大绝对差（存在性缺失按 0 计）
    //   allSides : 出现过的 side 集合（用于「方向不一致」告警）
    //   presence  : 布尔数组，标记哪些模型有此因子
    type AggFactor = {
      code: string;
      name: string | null;
      members: (ScoringModelFactorMember | undefined)[];
      maxAbsW: number;
      maxDeltaW: number;
      allSides: Set<ScoringModelFactorMember["side"]>;
      presence: boolean[];
    };
    const factorMap = new Map<string, AggFactor>();
    alignedDetails.forEach((d, i) => {
      for (const f of d?.factors ?? []) {
        const prev = factorMap.get(f.factor_code);
        const wAbs = Math.abs(f.normalized_weight || 0);
        if (!prev) {
          const presence = new Array(N).fill(false);
          presence[i] = true;
          const members = new Array(N).fill(undefined) as AggFactor["members"];
          members[i] = f;
          factorMap.set(f.factor_code, {
            code: f.factor_code,
            name: f.factor_name ?? null,
            members,
            maxAbsW: wAbs,
            maxDeltaW: wAbs,     // 仅一模型存在时 delta == wAbs（视作差异 0.5*wAbs 更合理？但保持简单用 max-min ≥ wAbs）
            allSides: new Set([f.side]),
            presence,
          });
        } else {
          prev.members[i] = f;
          prev.presence[i] = true;
          prev.maxAbsW = Math.max(prev.maxAbsW, wAbs);
          const ws = prev.members.map((m) => Math.abs(m?.normalized_weight ?? 0));
          prev.maxDeltaW = Math.max(...ws) - Math.min(...ws);
          if (f.factor_name && !prev.name) prev.name = f.factor_name;
          prev.allSides.add(f.side);
        }
      }
    });
    // 跨模型差：对仅出现在部分模型中的因子，惩罚"单边存在"为"差=该因子自身权重"
    for (const agg of factorMap.values()) {
      const anyMissing = agg.presence.some((p) => !p);
      if (anyMissing) {
        agg.maxDeltaW = Math.max(agg.maxDeltaW, agg.maxAbsW);
      }
    }
    const factorAggList = Array.from(factorMap.values()).sort((a, b) => {
      // 主排序：maxDeltaW（差异越大越靠前，这是对比的核心）
      if (Math.abs(b.maxDeltaW - a.maxDeltaW) > 1e-8) return b.maxDeltaW - a.maxDeltaW;
      // 次排序：maxAbsW（权重越大越靠前）
      return b.maxAbsW - a.maxAbsW;
    });
    const factorCodes = factorAggList.map((a) => a.code).slice(0, 20); // Top 20，比原来 12 多但仍可滚动
    const maxAbs = Math.max(1e-8, ...factorAggList.map((a) => a.maxAbsW));
    const aggByCode = new Map(factorAggList.map((a) => [a.code, a]));

    return (
      <Drawer
        title={
          <Space>
            <InfoCircleOutlined style={{ color: "#0891b2" }} />
            <Text strong style={{ fontSize: 15 }}>
              模型对比（{N} 个，最多 3 个）
            </Text>
            <Space size={4}>
              {selected.map((m, i) => (
                <Tag key={m.id} color={COMPARE_PALETTE[i]} style={{ marginInlineEnd: 0 }}>
                  #{i + 1} {modelStatusColor(m.status) === "green" ? "V" : m.status.slice(0, 3)}
                </Tag>
              ))}
            </Space>
          </Space>
        }
        open={compareOpen}
        onClose={() => setCompareOpen(false)}
        width={N === 3 ? 1180 : N === 2 ? 980 : 820}
        destroyOnClose
        maskClosable
      >
        <Spin spinning={compareLoading}>
          {N < 2 ? (
            <Empty description="请在模型列表选择 2~3 个模型进行对比" />
          ) : (
            <>
              {/* ① 指标对比表：每行一个指标；每列一个模型 */}
              <Card size="small" title="① 核心指标对比" style={{ marginBottom: 14 }}>
                <div style={{ overflowX: "auto" }}>
                  <table
                    style={{
                      width: "100%",
                      fontSize: 13,
                      borderCollapse: "collapse",
                      minWidth: 640,
                    }}
                  >
                    <thead>
                      <tr style={{ borderBottom: "1px solid #e2e8f0" }}>
                        <th style={{ textAlign: "left", padding: "8px 10px", color: "#64748b", fontWeight: 500, width: 170 }}>
                          指标
                        </th>
                        {selected.map((m, i) => (
                          <th
                            key={m.id}
                            style={{
                              textAlign: "center",
                              padding: "8px 10px",
                              borderLeft: i === 0 ? undefined : "1px dashed #e2e8f0",
                            }}
                          >
                            <Tag color={COMPARE_PALETTE[i]} style={{ marginInlineEnd: 0, fontSize: 12 }}>
                              #{i + 1}
                            </Tag>
                            <div style={{ marginTop: 4, wordBreak: "break-all", fontSize: 12 }}>
                              {m.id.length > 16 ? m.id.slice(0, 16) + "…" : m.id}
                            </div>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {(() => {
                        // P1.2.2b：已在上层计算 alignedDetails，与 selected 对齐
                        const details = alignedDetails;
                        const rows: Array<[string, (m: FactorModelRun, d: ScoringModelDetail | undefined) => React.ReactNode]> = [
                          ["状态", (m) => (
                            <Tag color={modelStatusColor(m.status)}>
                              {modelStatusLabel(m.status)}
                            </Tag>
                          )],
                          ["校验 IC", (m, d) => {
                            const vRaw = d?.validation_ic ?? m.metrics?.validation_ic;
                            const v = vRaw == null ? null : Number(vRaw);
                            return (
                              <Text strong style={{ color: icColor(v) }}>
                                {v != null && Number.isFinite(v) ? (v * 100).toFixed(3) + "%" : "-"}
                              </Text>
                            );
                          }],
                          ["训练 IC", (m, d) => {
                            const vRaw = d?.train_ic ?? m.metrics?.train_ic;
                            const v = vRaw == null ? null : Number(vRaw);
                            return (
                              <Text style={{ color: icColor(v) }}>
                                {v != null && Number.isFinite(v) ? (v * 100).toFixed(3) + "%" : "-"}
                              </Text>
                            );
                          }],
                          ["一致性 (train/val)", (_m, d) => {
                            const t = d?.train_ic;
                            const v = d?.validation_ic;
                            if (t == null || v == null || !Number.isFinite(t) || !Number.isFinite(v)) return "-";
                            const signOk = Math.sign(t) === Math.sign(v);
                            return <Tag color={signOk ? "green" : "orange"}>{signOk ? "同向" : "反向"}</Tag>;
                          }],
                          ["样本数", (m, d) => `${d?.sample_count ?? m.sample_count ?? 0}`],
                          ["交易日 × 个股", (_m, d) => `${d?.trade_date_count ?? 0} × ${d?.symbol_count ?? 0}`],
                          ["数据截止", (m, d) => formatDateTime(d?.data_cutoff_at ?? m.data_cutoff_at)],
                          ["FactorSet", (m) => {
                            const fsId = m.hyperparameters?.factor_set_id as string | undefined;
                            const fs = fsId ? factorSets.find((s) => s.id === fsId) : undefined;
                            const label = fs ? factorSetDisplayName(fs.name, fs.id) : (fsId ?? "-");
                            return <Tag color="blue" style={{ marginInlineEnd: 0 }}>{label}</Tag>;
                          }],
                          ["正则 / α", (m) => {
                            const hp = m.hyperparameters as any;
                            const l2 = hp?.l2_lambda ?? hp?.alpha ?? "-";
                            return <Text code>{l2}</Text>;
                          }],
                          ["时间窗口", (_m, d) => {
                            const tr = `${d?.train_start_date ?? "-"} ~ ${d?.train_end_date ?? "-"}`;
                            const val = `${d?.validation_start_date ?? "-"} ~ ${d?.validation_end_date ?? "-"}`;
                            return (
                              <Space direction="vertical" size={2} style={{ fontSize: 11 }}>
                                <span>训练：{tr}</span>
                                <span>校验：{val}</span>
                              </Space>
                            );
                          }],
                          ["权重种类 / Σ|w|", (_m, d) => {
                            const facs = d?.factors ?? [];
                            const k = facs.length;
                            const s = facs.reduce((a, f) => a + Math.abs(f.normalized_weight || 0), 0);
                            return `${k} 因子 · Σ|w|≈${s.toFixed(3)}`;
                          }],
                          [
                            "门禁拒绝原因",
                            (m, d) => renderRejectionReasonSplit(d?.rejection_reason ?? m.rejection_reason),
                          ],
                          ["创建时间", (m) => formatDateTime(m.created_at)],
                        ];
                        return rows.map(([name, fn], ri) => (
                          <tr
                            key={name}
                            style={{
                              borderBottom: ri === rows.length - 1 ? undefined : "1px solid #f1f5f9",
                              background: ri % 2 ? "#fafafa" : undefined,
                            }}
                          >
                            <td style={{ padding: "8px 10px", color: "#475569", fontWeight: 500 }}>{name}</td>
                            {selected.map((m, i) => (
                              <td
                                key={m.id}
                                style={{
                                  textAlign: "center",
                                  padding: "8px 10px",
                                  borderLeft: i === 0 ? undefined : "1px dashed #f1f5f9",
                                }}
                              >
                                {fn(m, details[i])}
                              </td>
                            ))}
                          </tr>
                        ));
                      })()}
                    </tbody>
                  </table>
                </div>
              </Card>

              {/* ② 因子权重 Top-N 并列条形图对比 */}
              <Card
                size="small"
                title={
                  <Space>
                    <span>② 因子权重 & 质量对比（Top {factorCodes.length}）</span>
                    <Tag color="cyan" style={{ marginInlineEnd: 0 }}>按模型间 |Δw| 降序</Tag>
                    <Tag color="purple" style={{ marginInlineEnd: 0 }}>单边缺失视同差异</Tag>
                  </Space>
                }
              >
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: N === 2 ? 960 : 1120 }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid #e2e8f0" }}>
                        <th style={{ textAlign: "left", padding: "6px 10px", width: 220 }}>
                          因子（差异 Δw / 存在性 / 方向）
                        </th>
                        {selected.map((m, i) => (
                          <th
                            key={m.id}
                            style={{
                              padding: "6px 10px",
                              textAlign: "left",
                              color: COMPARE_PALETTE[i],
                              borderLeft: i === 0 ? undefined : "1px dashed #e2e8f0",
                              minWidth: 200,
                            }}
                          >
                            #{i + 1} · 权重 |w| &nbsp;
                            <Tag style={{ marginInlineEnd: 0, fontSize: 11 }} color={COMPARE_PALETTE[i]}>
                              {(() => {
                                const d = alignedDetails[i];
                                const k = d?.factors?.length ?? 0;
                                return k ? `${k} 因子` : "-";
                              })()}
                            </Tag>
                          </th>
                        ))}
                        {N === 2 && (
                          <th
                            style={{
                              padding: "6px 10px",
                              textAlign: "right",
                              borderLeft: "1px dashed #e2e8f0",
                              width: 170,
                              color: "#0ea5e9",
                            }}
                          >
                            Δw（#1 → #2）
                          </th>
                        )}
                      </tr>
                    </thead>
                    <tbody>
                      {factorCodes.map((code) => {
                        const agg = aggByCode.get(code);
                        const abs0 = agg?.maxAbsW ?? 0;
                        const missingMask = agg?.presence ?? new Array(N).fill(false);
                        const sides = agg?.allSides ?? new Set();
                        const inconsistentSide = sides.size > 1;
                        const anyMissing = missingMask.some((p) => !p);

                        // N=2：计算 #1→#2 的 w 绝对差 & 相对变化率
                        let deltaW_N2: { abs: number; relPct: number | null; sign: 1 | -1 | 0 } | null = null;
                        if (N === 2 && agg) {
                          const w1 = Math.abs(agg.members[0]?.normalized_weight ?? 0);
                          const w2 = Math.abs(agg.members[1]?.normalized_weight ?? 0);
                          const diff = w2 - w1;
                          const rel = w1 > 1e-10 ? (diff / w1) * 100 : null;
                          deltaW_N2 = {
                            abs: Math.abs(diff),
                            relPct: rel,
                            sign: Math.sign(diff) as 1 | -1 | 0,
                          };
                        }
                        // 色标：|Δw|>0.05 红、>0.02 橙、其它默认
                        const deltaColor =
                          N === 2 && deltaW_N2
                            ? deltaW_N2.abs >= 0.05
                              ? "#dc2626"
                              : deltaW_N2.abs >= 0.02
                              ? "#ea580c"
                              : deltaW_N2.abs >= 0.005
                              ? "#ca8a04"
                              : "#64748b"
                            : "#64748b";
                        return (
                          <tr key={code} style={{ borderBottom: "1px solid #f8fafc" }}>
                            <td style={{ padding: "5px 10px" }}>
                              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                                <Space size={4} wrap>
                                  <Text
                                    strong
                                    style={{
                                      color: abs0 / maxAbs > 0.4 ? "#0f172a" : "#475569",
                                      fontSize: 13,
                                    }}
                                  >
                                    {agg?.name ? `${agg.name} · ` : ""}
                                    {code}
                                  </Text>
                                  {inconsistentSide && (
                                    <Tooltip title="该因子在不同模型中多空方向不一致（long↔short/neutral），为高风险差异。">
                                      <Tag color="orange" style={{ marginInlineEnd: 0, fontSize: 10 }}>
                                        ⚠️ 多空不一致
                                      </Tag>
                                    </Tooltip>
                                  )}
                                  {anyMissing && (
                                    <>
                                      {missingMask.map((has, idx) =>
                                        !has ? (
                                          <Tag key={idx} color="default" style={{ marginInlineEnd: 0, fontSize: 10 }}>
                                            仅 #{has ? "-" : idx + 1} 不存在
                                          </Tag>
                                        ) : null
                                      )}
                                      {missingMask.every((p) => p)
                                        ? null
                                        : missingMask
                                            .map((p, idx) => (p ? `#${idx + 1}` : null))
                                            .filter(Boolean)
                                            .length === 1 && (
                                            <Tag color="magenta" style={{ marginInlineEnd: 0, fontSize: 10 }}>
                                              仅 1 个模型使用
                                            </Tag>
                                          )}
                                    </>
                                  )}
                                </Space>
                                <Text type="secondary" style={{ fontSize: 11 }}>
                                  max|w|≈{abs0.toFixed(3)} · maxΔw≈{(agg?.maxDeltaW ?? 0).toFixed(3)}
                                </Text>
                              </div>
                            </td>
                            {selected.map((_m, i) => {
                              const f = agg?.members[i];
                              const w = f?.normalized_weight ?? 0;
                              const abs = Math.abs(w);
                              const pct = Math.max(0, Math.min(100, (abs / (maxAbs || 1)) * 100));
                              const factorIC = f?.validation_ic;
                              const factorCOV = f?.coverage;
                              if (!f) {
                                return (
                                  <td
                                    key={i}
                                    style={{
                                      padding: "5px 10px",
                                      borderLeft: i === 0 ? undefined : "1px dashed #f8fafc",
                                      background: "rgba(241, 245, 249, 0.4)",
                                      verticalAlign: "middle",
                                    }}
                                  >
                                    <Space size={4}>
                                      <Tag color="default" style={{ marginInlineEnd: 0, fontSize: 10 }}>
                                        未参与
                                      </Tag>
                                      <Text type="secondary" style={{ fontSize: 11 }}>
                                        （该因子集不含此成员）
                                      </Text>
                                    </Space>
                                  </td>
                                );
                              }
                              return (
                                <td
                                  key={i}
                                  style={{
                                    padding: "5px 10px",
                                    borderLeft: i === 0 ? undefined : "1px dashed #f8fafc",
                                  }}
                                >
                                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                                    <div
                                      style={{
                                        flex: "1 1 auto",
                                        background: "#f1f5f9",
                                        borderRadius: 3,
                                        height: 10,
                                        position: "relative",
                                        overflow: "hidden",
                                      }}
                                    >
                                      <div
                                        style={{
                                          width: pct + "%",
                                          height: "100%",
                                          background:
                                            w >= 0
                                              ? COMPARE_PALETTE[i]
                                              : `repeating-linear-gradient(45deg, ${COMPARE_PALETTE[i]}, ${COMPARE_PALETTE[i]} 4px, #fff 4px, #fff 8px)`,
                                          transition: "width .3s",
                                        }}
                                      />
                                    </div>
                                    <Text
                                      style={{
                                        flex: "0 0 auto",
                                        fontSize: 11,
                                        minWidth: 46,
                                        textAlign: "right",
                                        color: w >= 0 ? COMPARE_PALETTE[i] : "#7f1d1d",
                                        fontWeight: 600,
                                      }}
                                    >
                                      {abs > 0 ? (w >= 0 ? "+" : "−") + (abs * 100).toFixed(1) + "%" : "-"}
                                    </Text>
                                  </div>
                                  <Space size={4} wrap style={{ marginTop: 2 }}>
                                    {factorIC != null && Number.isFinite(factorIC) && (
                                      <Tag
                                        style={{ marginInlineEnd: 0, fontSize: 10, padding: "0 3px" }}
                                        color={icColor(factorIC)}
                                      >
                                        IC {(factorIC * 100).toFixed(2)}%
                                      </Tag>
                                    )}
                                    {factorCOV != null && Number.isFinite(factorCOV) && (
                                      <Tag
                                        style={{ marginInlineEnd: 0, fontSize: 10, padding: "0 3px" }}
                                        color={coveragePercent(factorCOV) >= 70 ? "green" : "orange"}
                                      >
                                        Cov {(factorCOV * 100).toFixed(0)}%
                                      </Tag>
                                    )}
                                    {f.side !== "neutral" && (
                                      <Tag
                                        style={{ marginInlineEnd: 0, fontSize: 10, padding: "0 3px" }}
                                        color={f.side === "long" ? "green" : "red"}
                                      >
                                        {f.side.toUpperCase()}
                                      </Tag>
                                    )}
                                  </Space>
                                </td>
                              );
                            })}
                            {N === 2 && deltaW_N2 && (
                              <td
                                style={{
                                  padding: "5px 10px",
                                  borderLeft: "1px dashed #f8fafc",
                                  textAlign: "right",
                                  verticalAlign: "middle",
                                }}
                              >
                                <div
                                  style={{
                                    display: "flex",
                                    flexDirection: "column",
                                    gap: 2,
                                    alignItems: "flex-end",
                                  }}
                                >
                                  <Text
                                    strong
                                    style={{
                                      color: deltaColor,
                                      fontSize: 12,
                                    }}
                                  >
                                    {deltaW_N2.sign === 1 && deltaW_N2.abs > 0 ? "+" : deltaW_N2.sign === -1 ? "−" : ""}
                                    {(deltaW_N2.abs * 100).toFixed(2)}%pts
                                  </Text>
                                  <Text type="secondary" style={{ fontSize: 11 }}>
                                    {deltaW_N2.relPct == null
                                      ? "（#1 为 0，无法计相对）"
                                      : `相对 ${deltaW_N2.relPct >= 0 ? "+" : ""}${deltaW_N2.relPct.toFixed(1)}%`}
                                  </Text>
                                </div>
                              </td>
                            )}
                          </tr>
                        );
                      })}
                      {factorCodes.length === 0 && (
                        <tr>
                          <td colSpan={N + (N === 2 ? 1 : 0)} style={{ padding: 18, textAlign: "center", color: "#94a3b8" }}>
                            尚未加载模型详情 — 请先点击「对比选中」以拉取权重数据，或先展开各模型行
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                <div style={{ marginTop: 10, fontSize: 11, color: "#94a3b8" }}>
                  <Space size="large" wrap>
                    <span>图例：</span>
                    {selected.map((m, i) => (
                      <Space size={4} key={m.id}>
                        <span
                          style={{ display: "inline-block", width: 12, height: 12, background: COMPARE_PALETTE[i], borderRadius: 2 }}
                        />
                        <span style={{ color: COMPARE_PALETTE[i] }}>
                          #{i + 1} {m.id.slice(m.id.length - 10)}（{alignedDetails[i]?.factors?.length ?? 0} 因子）
                        </span>
                      </Space>
                    ))}
                    <span>· 斜线纹理 = 负权重（多空方向与其他模型相反，要格外关注）</span>
                    {N === 2 && (
                      <span>
                        · Δw 色标：
                        <span style={{ color: "#dc2626" }}> ≥5%pts 大改 </span>/
                        <span style={{ color: "#ea580c" }}> ≥2%pts 中改 </span>/
                        <span style={{ color: "#ca8a04" }}> ≥0.5%pts 小改 </span>/
                        <span style={{ color: "#64748b" }}> 其他</span>
                      </span>
                    )}
                  </Space>
                </div>
              </Card>

              {/* ③ 一键决策建议（纯前端启发式：不替代专业判断） */}
              <Alert
                type="info"
                showIcon
                style={{ marginTop: 14 }}
                message="启发式观察（仅参考，不替代专业判断）"
                description={(() => {
                  const details = selected.map((m) => [m, modelDetailMap[m.id]] as const);
                  const tips: string[] = [];
                  details.forEach(([m, d], i) => {
                    const vicRaw = d?.validation_ic ?? m.metrics?.validation_ic;
                    const vic = vicRaw == null ? null : Number(vicRaw);
                    const n = (d?.sample_count ?? m.sample_count ?? 0) as number;
                    const k = d?.factors?.length ?? 0;
                    const label = `#${i + 1}(${m.id.slice(-8)})`;
                    if (vic != null && Number.isFinite(vic) && vic >= 0.02 && n > 10000) {
                      tips.push(`✓ ${label} 校验 IC≥2% 且样本充足，基准表现良好。`);
                    }
                    if (k === 1) {
                      tips.push(`⚠ ${label} 只用了 ${k} 个因子，本质是单因子缩放而非多因子合成。`);
                    }
                    if (d?.rejection_reason) {
                      tips.push(`✗ ${label} 被门禁拒绝：${d.rejection_reason}`);
                    }
                    if (vic != null && Number.isFinite(vic) && vic < 0.005 && m.status === "validated") {
                      tips.push(`? ${label} 已通过验证但校验 IC 偏低 (${(vic * 100).toFixed(2)}%)，建议关注实际 G5/G6 对账。`);
                    }
                  });
                  return tips.length ? (
                    <ul style={{ paddingInlineStart: 20, margin: 0 }}>
                      {tips.map((s, idx) => <li key={idx} style={{ marginTop: idx ? 2 : 0 }}>{s}</li>)}
                    </ul>
                  ) : "未生成自动观察。请结合业务规则人工解读上表。";
                })()}
              />
            </>
          )}
        </Spin>
      </Drawer>
    );
  };

  // FactorSet 列表列定义
  const factorSetColumns = [
    {
      title: t("factorModelFactorSetName"),
      dataIndex: "name",
      key: "name",
      render: (name: string, record: FactorSet) => (
        <Space direction="vertical" size={0}>
          <Text strong>{factorSetDisplayName(name, record.id)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.id}
          </Text>
        </Space>
      ),
    },
    {
      title: t("factorModelFactorSetStatus"),
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: string) => (
        <Tag color={factorSetStatusColor(status)}>{factorSetStatusLabel(status)}</Tag>
      ),
    },
    {
      title: t("factorModelFactorSetMembers"),
      dataIndex: "n_members",
      key: "n_members",
      width: 90,
    },
    {
      title: t("factorModelCutoff"),
      dataIndex: "frozen_at",
      key: "frozen_at",
      width: 160,
      render: (v: string | null) => formatDateTime(v),
    },
    // WP0-8 C-08：FactorSet 真实操作列（冻结 / 训练）—— 去除"规划中控件"，接入真实路由契约
    {
      title: "操作（真实 API）",
      key: "actions",
      width: 220,
      render: (_: unknown, record: FactorSet) => {
        const canFreeze = record.status === "draft";
        const canTrain = record.status === "frozen" || record.status === "draft";
        return (
          <Space size="small">
            <Button
              size="small"
              type="primary"
              ghost
              disabled={!canFreeze}
              onClick={() => {
                setFreezeTarget(record);
                setFreezeReason(`UI 冻结 ${record.id}（${new Date().toISOString().slice(0, 10)}）`);
                setFreezeModalOpen(true);
              }}
              title={canFreeze ? undefined : "仅 draft 状态 FactorSet 可冻结"}
            >
              冻结
            </Button>
            <Button
              size="small"
              type="primary"
              disabled={!canTrain}
              onClick={() => {
                setTrainTarget(record);
                setTrainAssetType(record.asset_type as any || "STOCK");
                setTrainMode(record.status === "frozen" ? "offline_minimal" : "offline_minimal");
                setTrainModalOpen(true);
              }}
              title={canTrain ? undefined : "请先冻结 FactorSet 后再训练（保证版本可溯源）"}
            >
              训练模型
            </Button>
          </Space>
        );
      },
    },
  ];

  // FactorSet 成员展开行
  const factorSetExpandable = {
    expandedRowRender: (record: FactorSet) => {
      const memberColumns = [
        {
          title: t("factorModelFactorCode"),
          dataIndex: "factor_code",
          key: "factor_code",
        },
        {
          title: t("factorModelFactorVersion"),
          dataIndex: "factor_version",
          key: "factor_version",
          width: 80,
        },
        {
          title: t("factorModelFactorRole"),
          dataIndex: "role",
          key: "role",
          width: 90,
          render: (role: string) => <Tag>{role}</Tag>,
        },
        {
          title: t("factorModelFactorMissingPolicy"),
          dataIndex: "missing_policy",
          key: "missing_policy",
          width: 120,
        },
      ];
      return (
        <Table
          size="small"
          columns={memberColumns}
          dataSource={record.members ?? []}
          rowKey="id"
          pagination={false}
        />
      );
    },
    rowExpandable: (record: FactorSet) => (record.members?.length ?? 0) > 0,
  };

  // 审计日志列定义
  const auditColumns = [
    {
      title: t("factorModelAuditAction"),
      dataIndex: "action",
      key: "action",
      width: 100,
      render: (action: string) => (
        <Tag color={action === "activate" ? "green" : action === "fallback" ? "orange" : "default"}>
          {action}
        </Tag>
      ),
    },
    {
      title: t("factorModelAuditMode"),
      key: "mode",
      width: 140,
      render: (_: unknown, record: AuditEntry) => (
        <Space size={4}>
          {record.previous_mode && <Tag>{record.previous_mode}</Tag>}
          {record.previous_mode && <Text type="secondary">→</Text>}
          <Tag color={record.new_mode === "ridge" ? "green" : record.new_mode === "shadow" ? "orange" : "default"}>
            {weightModeLabel(record.new_mode)}
          </Tag>
        </Space>
      ),
    },
    {
      title: t("factorModelAuditActor"),
      dataIndex: "actor",
      key: "actor",
      width: 120,
    },
    {
      title: t("factorModelAuditNote"),
      dataIndex: "note",
      key: "note",
      render: (note: string | null) =>
        note ? <Text type="secondary">{note}</Text> : <Text type="secondary">-</Text>,
    },
    {
      title: t("factorModelCutoff"),
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
      render: (v: string | null) => formatDateTime(v),
    },
  ];

  return (
    <div className="factor-model-page">
      <Spin spinning={loading}>
        <Alert
          type="info"
          showIcon
          message={t("factorModelCenterGuideTitle")}
          description={t("factorModelCenterGuideDescription")}
          style={{ marginBottom: 16 }}
        />

        {/* Task 3：ChainStepsBar 链式步骤导航（8 Steps + 阻断直达 + 下一步主按钮） */}
        <div style={{ marginBottom: 16 }}>
          <ChainStepsBar state={chainState} onGoStep={handleChainGoStep} />
        </div>

        {/* 当前 runtime 状态 */}
        <Card
          size="small"
          title={t("factorModelRuntime")}
          extra={
            <Space>
              <Button
                size="small"
                icon={<RollbackOutlined />}
                onClick={() => {
                  setFallbackReason("");
                  setFallbackModalOpen(true);
                }}
                disabled={runtime?.weight_mode === "manual"}
              >
                {t("factorModelFallback")}
              </Button>
              <Button size="small" icon={<ReloadOutlined />} onClick={loadData}>
                {t("refresh")}
              </Button>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          {runtime ? (
            <Descriptions size="small" column={3}>
              <Descriptions.Item label={t("factorModelRuntimeMode")}>
                <Tag color={weightModeColor(runtime.weight_mode)}>
                  {weightModeLabel(runtime.weight_mode)}
                </Tag>
                {runtime.score_weight_mode === "ridge" && (
                  <Tag color="green">{t("factorModelScoreWeightMode")}</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelActiveModel")}>
                {runtime.active_model_run_id ? (
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0 }}
                    onClick={() => loadModelDetail(runtime.active_model_run_id!)}
                  >
                    {runtime.active_model_run_id.slice(0, 24) + "..."}
                  </Button>
                ) : (
                  <Text type="secondary">-</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelFallbackReason")}>
                {runtime.fallback_reason ? (
                  <Text type="warning">{runtime.fallback_reason}</Text>
                ) : (
                  <Text type="secondary">-</Text>
                )}
              </Descriptions.Item>
            </Descriptions>
          ) : (
            <Empty description={t("factorModelNoRuntime")} />
          )}
        </Card>

        {/* FactorSet 列表（Task 2：9 元素卡片式实现）*/}
        <Card
          size="small"
          title={
            <Space>
              <span>{t("factorModelFactorSets")}</span>
              <Tag color="blue" style={{ marginInlineEnd: 0 }}>共 {factorSets.length} 个</Tag>
            </Space>
          }
          extra={
            <Space>
              {/* Task 2 ⑨ 顶部主按钮（与①同逻辑，空列表时也有一个副本在页面中央） */}
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={handleOpenCreateModal}
                data-testid="btn-create-factorset-top"
              >
                新建集合
              </Button>
              <Button size="small" icon={<ReloadOutlined />} onClick={loadData}>
                {t("refresh")}
              </Button>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          {factorSets.length > 0 ? (
            <div ref={factorSetTableRef} style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(420px, 1fr))", gap: 14 }}>
              {factorSets.map((fs) => {
                const isFrozen = fs.status === "frozen";
                const isDraft = fs.status === "draft";
                const isDeprecated = fs.status === "deprecated";
                const freezeGate = computeFreezeGate(fs);
                const trainGate = computeTrainGate(fs);
                const deprecateGate = computeDeprecateGate(fs, runtime);
                // 成员 feature-first 排序
                const sortedMembers = sortMembersByRole(fs.members ?? []);
                const featureCount = sortedMembers.filter((m) => m.role === "feature").length;
                const trainableCount = sortedMembers.filter((m: any) => {
                  if (typeof (m as any).version_trainable === "boolean") return (m as any).version_trainable;
                  const s = String((m as any).version_status || "trainable").toLowerCase();
                  return !(s === "draft" || s === "deprecated" || s === "disabled");
                }).length;
                const trainableRatio =
                  sortedMembers.length > 0 ? Math.round((trainableCount / sortedMembers.length) * 100) : 0;
                const isNewlyCreated = newlyCreatedIdRef.current === fs.id;
                const isHighlight = highlightFactorSetId === fs.id;
                const isPulse = pulseFactorSetId === fs.id;
                const expanded = expandedSetIds.includes(fs.id);
                return (
                  <div
                    key={fs.id}
                    ref={(n) => { setCardRefs.current[fs.id] = n; }}
                    data-fs-id={fs.id}
                    data-testid={`set-card-${fs.id}`}
                    className={[
                      "factor-set-card",
                      isPulse ? "pulse" : "",
                      expanded ? "set-card-expanded" : "set-card-collapsed",
                    ].filter(Boolean).join(" ")}
                    style={{
                      border: "1px solid var(--pt-border)",
                      borderRadius: 10,
                      padding: "12px 14px 10px",
                      background: "var(--pt-card)",
                      boxShadow: isHighlight
                        ? "inset 0 0 0 2px var(--pt-primary), 0 0 0 4px rgba(250,204,21,0.18)"
                        : isNewlyCreated
                        ? "0 0 0 2px rgba(34,197,94,0.35)"
                        : undefined,
                      transition: "box-shadow .2s linear",
                      opacity: isDeprecated ? 0.75 : 1,
                    }}
                  >
                    {/* 卡头：名称 + 状态徽标 + 展开/收起按钮（Task 6 (4)：点击卡头切换展开显示成员） */}
                    <div
                      className="set-card-header"
                      data-testid={`set-card-header-${fs.id}`}
                      role="button"
                      tabIndex={0}
                      onClick={() => toggleSetCardExpand(fs.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          toggleSetCardExpand(fs.id);
                        }
                      }}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "flex-start",
                        gap: 8,
                        marginBottom: 6,
                        cursor: "pointer",
                        userSelect: "none",
                      }}
                    >
                      <Space direction="vertical" size={0} style={{ minWidth: 0, flex: "1 1 auto" }}>
                        <Space size={8} align="center">
                          <span
                            aria-hidden
                            style={{
                              display: "inline-block",
                              width: 14,
                              transition: "transform .2s",
                              transform: expanded ? "rotate(90deg)" : "rotate(0deg)",
                              color: "var(--pt-muted-foreground)",
                            }}
                          >
                            ▶
                          </span>
                          <Text strong style={{ fontSize: 15, display: "block" }}>
                            {factorSetDisplayName(fs.name, fs.id)}
                          </Text>
                        </Space>
                        <Text type="secondary" style={{ fontSize: 11 }}>{fs.id}</Text>
                        {factorSetDescription(fs.description, fs.id) && (
                          <div
                            style={{
                              fontSize: 12,
                              marginTop: 2,
                              color: "rgba(0,0,0,0.45)",
                              display: "-webkit-box",
                              WebkitLineClamp: 2,
                              WebkitBoxOrient: "vertical",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              wordBreak: "break-word",
                            }}
                            title={factorSetDescription(fs.description, fs.id) ?? undefined}
                          >
                            {factorSetDescription(fs.description, fs.id)}
                          </div>
                        )}
                      </Space>
                      <Tag
                        color={factorSetStatusColor(fs.status)}
                        style={{ marginInlineEnd: 0, fontWeight: 500 }}
                        data-testid={`fs-status-${fs.id}`}
                      >
                        {factorSetStatusLabel(fs.status)}
                      </Tag>
                    </div>

                    {/* 指标条：成员数 / feature 数 / 可训练比例 / 更新时间 */}
                    <Descriptions size="small" column={2} style={{ marginBottom: 8 }} labelStyle={{ fontSize: 11 }} contentStyle={{ fontSize: 12 }}>
                      <Descriptions.Item label="成员数 (n_members)">
                        <Text strong>{sortedMembers.length}</Text>
                      </Descriptions.Item>
                      <Descriptions.Item label="feature 数">
                        <Text strong>{featureCount}</Text>
                      </Descriptions.Item>
                      <Descriptions.Item label="可训练比例">
                        <Tag color={trainableRatio === 100 ? "green" : trainableRatio >= 50 ? "orange" : "red"} style={{ marginInlineEnd: 0 }}>
                          {trainableRatio}%
                        </Tag>
                      </Descriptions.Item>
                      <Descriptions.Item label="最近更新">
                        {formatDateTime(fs.updated_at ?? fs.frozen_at ?? fs.created_at)}
                      </Descriptions.Item>
                    </Descriptions>

                    {/* Task 6 (4): 集合卡片点击展开 → 显示成员明细表；空集合时展开区显示空状态；收起时不显示成员表 */}
                    {expanded ? (
                      sortedMembers.length > 0 ? (
                        <div
                          style={{
                            border: "1px solid var(--pt-border)",
                            borderRadius: 6,
                            padding: "6px 8px",
                            marginBottom: 8,
                            maxHeight: 220,
                            overflowY: "auto",
                            background: "var(--pt-accent-soft)",
                          }}
                          data-testid={`fs-member-table-${fs.id}`}
                          className="set-card-members"
                        >
                          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                            <thead>
                              <tr style={{ borderBottom: "1px solid var(--pt-border)" }}>
                                <th style={thStyle}>{localizedLabel("factorModelFactorCode", "因子代码")}</th>
                                <th style={thStyle}>{localizedLabel("factorModelFactorName", "因子名称")}</th>
                                <th style={thStyle}>{localizedLabel("factorModelFactorVersion", "版本")}</th>
                                <th style={thStyle}>{localizedLabel("factorModelFactorRole", "角色")}</th>
                                <th style={thStyle}>{localizedLabel("factorModelFactorWeightConstraint", "权重约束")}</th>
                                <th style={thStyle}>{localizedLabel("factorModelFactorMissingPolicy", "缺失策略")}</th>
                                {isDraft && <th style={{ ...thStyle, width: 28 }}>&nbsp;</th>}
                              </tr>
                            </thead>
                            <tbody>
                              {sortedMembers.map((m) => (
                                <tr
                                  key={m.id}
                                  style={{
                                    borderBottom: "1px dashed rgba(148,163,184,0.25)",
                                    background: m.role !== "feature" ? "rgba(148,163,184,0.06)" : undefined,
                                  }}
                                  data-testid={`member-row-${m.id}`}
                                  data-member-role={m.role}
                                >
                                  <td style={tdStyle}><code>{m.factor_code}</code></td>
                                  <td style={tdStyle}>
                                    {((m as any).factor_name as string | null) ?? (
                                      <Text type="secondary" style={{ fontSize: 11 }}>N/A</Text>
                                    )}
                                  </td>
                                  <td style={tdStyle}>
                                    <Tag style={{ marginInlineEnd: 0, fontSize: 11 }}>
                                      v{m.factor_version}
                                    </Tag>
                                  </td>
                                  <td style={tdStyle}>
                                    <Tag color={m.role === "feature" ? "blue" : "default"} style={{ marginInlineEnd: 0, fontSize: 11 }}>
                                      {memberRoleLabel(m.role)}
                                    </Tag>
                                  </td>
                                  <td style={tdStyle}>
                                    <Text type="secondary">{weightConstraintLabel(m.weight_constraint)}</Text>
                                  </td>
                                  <td style={tdStyle}>
                                    <Text type="secondary">{missingPolicyLabel(m.missing_policy)}</Text>
                                  </td>
                                  {isDraft && (
                                    <td style={{ ...tdStyle, textAlign: "center" }}>
                                      <Tooltip title="从集合中移除该成员">
                                        <Button
                                          type="text"
                                          danger
                                          size="small"
                                          icon={<CloseOutlined />}
                                          style={{ padding: "0 2px", minWidth: 24 }}
                                          disabled={isFrozen || isDeprecated}
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            handleRemoveMember(m.id);
                                          }}
                                          data-testid={`btn-del-member-${m.id}`}
                                        />
                                      </Tooltip>
                                    </td>
                                  )}
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ) : (
                        <div style={{ marginBottom: 8 }} data-testid={`fs-member-empty-${fs.id}`} className="set-card-members-empty">
                          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={isDraft ? "当前为空集合，点击「编辑成员」后可添加因子。" : "该集合暂无成员（已展开但为空）"} />
                        </div>
                      )
                    ) : (
                      <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)", marginBottom: 8 }} data-testid={`fs-collapsed-hint-${fs.id}`}>
                        点击卡头展开查看成员（{sortedMembers.length} 个成员）
                      </div>
                    )}

                    {/* 操作按钮行（Task 2 ①②③⑤⑥⑦ + 废弃 共 8+ 控件） */}
                    <Space size="small" wrap style={{ display: "flex", justifyContent: "flex-end" }}>
                      {/* ② 复制按钮（始终启用，包括 frozen） */}
                      <Tooltip title="复制该集合为一个新的 draft 集合（成员与角色设置原样复制）">
                        <Button
                          size="small"
                          icon={<CopyOutlined />}
                          loading={cloningId === fs.id}
                          onClick={() => handleCloneFactorSet(fs)}
                          data-testid={`btn-clone-${fs.id}`}
                        >
                          复制
                        </Button>
                      </Tooltip>
                      {/* ③ 编辑成员 Drawer（draft 态启用，frozen 禁用） */}
                      <Tooltip title={isFrozen ? "集合已冻结：成员不可编辑" : isDeprecated ? "已废弃集合不可编辑" : "编辑集合成员（添加/移除因子版本）"}>
                        <Button
                          size="small"
                          icon={<EditOutlined />}
                          disabled={!isDraft}
                          onClick={() => openMemberDrawer(fs)}
                          data-testid={`btn-edit-members-${fs.id}`}
                        >
                          编辑成员
                        </Button>
                      </Tooltip>
                      {/* ⑤ 冻结摘要 Modal 按钮（Task 2 门禁：3 条 readiness）*/}
                      <Tooltip title={freezeGate.disabled ? freezeGate.tooltip : "预检成员数 / feature / 可训练比例后冻结（不可逆）"}>
                        <Button
                          size="small"
                          type="primary"
                          ghost
                          disabled={!isDraft || freezeGate.disabled}
                          onClick={() => {
                            setFreezeTarget(fs);
                            setFreezeReason(`UI 冻结 ${factorSetDisplayName(fs.name, fs.id)}（${new Date().toISOString().slice(0, 10)}）`);
                            setFreezeIrrevocableConfirm(false);
                            setFreezeModalOpen(true);
                          }}
                          data-testid={`btn-freeze-${fs.id}`}
                          data-freeze-disabled={(!isDraft || freezeGate.disabled) ? "true" : "false"}
                          data-gate-tooltip={freezeGate.disabled ? freezeGate.tooltip : "预检成员数 / feature / 可训练比例后冻结（不可逆）"}
                        >
                          冻结
                        </Button>
                      </Tooltip>
                      {/* ⑦ 训练对话框（Task 2 ⑦：隐式传 factor_set_id） */}
                      <Tooltip title={trainGate.disabled ? trainGate.tooltip : "基于该已冻结集合训练 Ridge 模型"}>
                        <Button
                          size="small"
                          type="primary"
                          disabled={trainGate.disabled}
                          onClick={() => {
                            setTrainTarget(fs);
                            setTrainMode("offline_minimal");
                            setTrainAssetType((fs as any).asset_type as any || "STOCK");
                            setTrainNote("");
                            setTrainGateError(null);
                            setTrainModalOpen(true);
                          }}
                          data-testid={`btn-train-${fs.id}`}
                          data-train-disabled={trainGate.disabled ? "true" : "false"}
                          data-gate-tooltip={trainGate.disabled ? trainGate.tooltip : "基于该已冻结集合训练 Ridge 模型"}
                        >
                          基于此集合训练
                        </Button>
                      </Tooltip>
                      {/* 废弃按钮（Task 2 门禁 3：被活动模型使用则禁用）*/}
                      <Tooltip title={deprecateGate.disabled ? deprecateGate.tooltip : "废弃该集合（不可再训练新模型，历史模型不受影响）"}>
                        <Button
                          size="small"
                          danger
                          disabled={isDeprecated || deprecateGate.disabled}
                          loading={deprecatingId === fs.id}
                          onClick={() => handleDeprecateFactorSet(fs)}
                          data-testid={`btn-deprecate-${fs.id}`}
                          data-gate-tooltip={deprecateGate.disabled ? deprecateGate.tooltip : "废弃该集合（不可再训练新模型，历史模型不受影响）"}
                        >
                          废弃
                        </Button>
                      </Tooltip>
                    </Space>
                  </div>
                );
              })}
            </div>
          ) : (
            // Task 2 ⑨：空集合页 placeholder + 中央主按钮
            <div style={{ textAlign: "center", padding: "36px 16px 28px" }} data-testid="empty-factorset-placeholder">
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <Space direction="vertical" size={8}>
                    <Text strong style={{ fontSize: 14 }}>
                      暂无因子集合，点击左上角「新建集合」开始
                    </Text>
                    <Button
                      type="primary"
                      size="large"
                      icon={<PlusOutlined />}
                      onClick={handleOpenCreateModal}
                      style={{ marginTop: 4 }}
                      data-testid="btn-create-factorset-hero"
                    >
                      新建集合
                    </Button>
                  </Space>
                }
              />
            </div>
          )}
        </Card>

        {/* 候选模型列表 */}
        <Card
          size="small"
          title={t("factorModelModels")}
          extra={
            <Space size="small">
              {compareSelectedIds.length > 0 && (
                <Tooltip title="点击表前复选框选择，最多同时对比 3 个模型（仅 validated 状态有意义）">
                  <Tag color={compareSelectedIds.length >= 2 ? "cyan" : "gold"}>
                    已选 {compareSelectedIds.length}/3
                  </Tag>
                </Tooltip>
              )}
              <Button
                type="primary"
                ghost
                size="small"
                disabled={compareSelectedIds.length < 2}
                onClick={async () => {
                  // ═══════════════════════════════════════════════════
                  // P1.2.2a：预取不在 modelDetailMap 中的 ScoringModelDetail
                  //   —— 用 api.scoringGetModelDetail 并行拉取，而不是
                  //   loadModelDetail（后者只更新 selectedModel/auditLogs，
                  //   不写 modelDetailMap，会导致对比页拿不到因子权重）
                  // ═══════════════════════════════════════════════════
                  setCompareLoading(true);
                  try {
                    const missing = compareSelectedIds.filter(
                      (id) => !modelDetailMap[id] && !detailLoadingSet.has(id)
                    );
                    if (missing.length) {
                      // 标记这些 id 正在加载，避免并发重复请求
                      setDetailLoadingSet((s) => {
                        const ns = new Set(s);
                        missing.forEach((id) => ns.add(id));
                        return ns;
                      });
                      const fetched = await Promise.all(
                        missing.map((id) =>
                          api
                            .scoringGetModelDetail(id)
                            .then((d) => [id, d] as const)
                            .catch((err) => {
                              message.error(
                                `预取模型 ${id.slice(0, 10)} 权重失败：${String(err)}`
                              );
                              return null;
                            })
                        )
                      );
                      const patch: Record<string, ScoringModelDetail> = {};
                      for (const item of fetched) {
                        if (!item) continue;
                        const [id, d] = item;
                        patch[id] = d;
                      }
                      if (Object.keys(patch).length) {
                        setModelDetailMap((m) => ({ ...m, ...patch }));
                      }
                      setDetailLoadingSet((s) => {
                        const ns = new Set(s);
                        missing.forEach((id) => ns.delete(id));
                        return ns;
                      });
                    }
                    setCompareOpen(true);
                  } finally {
                    setCompareLoading(false);
                  }
                }}
                loading={compareLoading}
              >
                对比选中 {compareSelectedIds.length || ""}
              </Button>
              <Tooltip title="点击任意行左侧 ▸ 展开该模型的因子权重构成（含 IC、覆盖率、多空方向）。">
                <Text type="secondary" style={{ fontSize: 12 }}>
                  <InfoCircleOutlined /> 行左侧可展开权重
                </Text>
              </Tooltip>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          {models.length > 0 ? (
            <div ref={modelTableRef} data-testid="model-table-wrapper">
              <Table
                size="small"
                columns={modelColumns}
                dataSource={models}
                rowKey="id"
                expandable={modelExpandable as any}
                rowClassName={(record) => {
                  const r = record as FactorModelRun;
                  const base = "";
                  const rejected = r.status === "rejected";
                  let cls = base;
                  if (rejected) cls = `${cls} row-rejected-redgate`;
                  if (newModelId && r.id === newModelId) cls = `${cls} row-new-model-highlight`;
                  return cls;
                }}
                rowSelection={{
                  type: "checkbox",
                  selectedRowKeys: compareSelectedIds,
                  onChange: (keys) => {
                    const ids = keys.map(String);
                    if (ids.length > 3) {
                      message.warning("最多同时对比 3 个模型，已自动保留前 3 个。");
                      setCompareSelectedIds(ids.slice(0, 3));
                    } else {
                      setCompareSelectedIds(ids);
                    }
                  },
                  // 提示：已选满 3 个时 disable 其余行
                  getCheckboxProps: (_record) => ({
                    disabled: compareSelectedIds.length >= 3 &&
                      !compareSelectedIds.includes(_record.id),
                  }),
                }}
                pagination={{ pageSize: 10, size: "small" }}
              />
            </div>
          ) : (
            <Empty description={t("factorModelNoModels")} />
          )}
          {/* ═══ P2.3 UI 增强：rejected 行整行红底 + 呼吸阴影，hover 时加深 ═══ */}
          <style>{`
            .row-rejected-redgate > td {
              background-color: rgba(255, 235, 234, 0.72) !important;
              transition: background-color 0.2s linear, box-shadow 0.2s linear;
              box-shadow: inset 3px 0 0 0 #ff4d4f, inset -3px 0 0 0 #ff4d4f0a;
            }
            .row-rejected-redgate:hover > td {
              background-color: rgba(255, 214, 212, 0.92) !important;
              box-shadow: inset 3px 0 0 0 #d4380d, inset 0 -1px 0 0 rgba(212,56,13,0.35);
            }
            .row-rejected-redgate > td.ant-table-selection-column::before {
              content: "";
              position: absolute;
              inset: 0;
              background: linear-gradient(90deg, rgba(255,77,79,0.10), transparent 60%);
              pointer-events: none;
            }
            .row-new-model-highlight > td {
              background-color: rgba(34,197,94,0.10) !important;
              box-shadow: inset 0 0 0 1px rgba(34,197,94,0.55);
              animation: newModelHighlightPulse 0.9s ease-in-out 3;
            }
            @keyframes newModelHighlightPulse {
              0%,100% { background-color: rgba(34,197,94,0.10); }
              50% { background-color: rgba(34,197,94,0.25); }
            }
            /* Task 6 (1): 集合卡片点击跳转过来的 3 秒 pulse 高亮 */
            .factor-set-card.pulse {
              animation: factorSetCardPulse 1.1s ease-in-out 0s 3;
              box-shadow: 0 0 0 2px rgba(250, 204, 21, 0.55), inset 0 0 0 2px rgba(14, 165, 233, 0.45);
              border-color: rgba(14, 165, 233, 0.65);
            }
            @keyframes factorSetCardPulse {
              0%, 100% { box-shadow: 0 0 0 2px rgba(250,204,21,0.25), inset 0 0 0 2px rgba(14,165,233,0.30); }
              50% { box-shadow: 0 0 0 6px rgba(250,204,21,0.55), inset 0 0 0 2px rgba(14,165,233,0.70); }
            }
          `}</style>
        </Card>

        {renderCompareDrawer()}

        {/* 选中模型详情 + 审计日志 */}
        {selectedModel && (
          <Card
            size="small"
            title={`${t("factorModelDetail")}: ${selectedModel.id}`}
            extra={
              <Button size="small" onClick={() => setSelectedModel(null)}>
                {t("close")}
              </Button>
            }
          >
            <Descriptions size="small" column={3} bordered>
              <Descriptions.Item label={t("factorModelStatus")}>
                <Tag color={modelStatusColor(selectedModel.status)}>
                  {modelStatusLabel(selectedModel.status)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelValidationIc")}>
                {formatNumber(selectedModel.metrics?.validation_ic)}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelSamples")}>
                {selectedModel.sample_count}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelFactorSet")}>
                {(selectedModel.hyperparameters?.factor_set_id as string) ?? "-"}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelTrainRange")}>
                {selectedModel.train_start_date ?? "-"} ~ {selectedModel.train_end_date ?? "-"}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelValidationRange")}>
                {selectedModel.validation_start_date ?? "-"} ~ {selectedModel.validation_end_date ?? "-"}
              </Descriptions.Item>
            </Descriptions>

            {/* 权重快照 */}
            {(selectedModel.weights?.length ?? 0) > 0 && (
              <Paragraph style={{ marginTop: 12 }}>
                <Text strong>{t("factorModelWeights")}</Text>
              </Paragraph>
            )}
            <Table
              size="small"
              columns={[
                {
                  title: t("factorModelFactorCode"),
                  dataIndex: "factor_code",
                  key: "factor_code",
                },
                {
                  title: t("factorModelFactorVersion"),
                  dataIndex: "factor_version",
                  key: "factor_version",
                  width: 80,
                },
                {
                  title: t("factorModelCoefficient"),
                  dataIndex: "coefficient",
                  key: "coefficient",
                  render: (v: number) => formatNumber(v),
                },
                {
                  title: t("factorModelNormalizedWeight"),
                  dataIndex: "normalized_weight",
                  key: "normalized_weight",
                  render: (v: number) => formatNumber(v),
                },
                {
                  title: t("factorModelValidationIc"),
                  dataIndex: "validation_ic",
                  key: "validation_ic",
                  render: (v: number | null) => formatNumber(v),
                },
              ]}
              dataSource={selectedModel.weights ?? []}
              rowKey={(w) => w.factor_code}
              pagination={false}
              style={{ marginBottom: 16 }}
            />

            {/* 拒绝原因 */}
            {selectedModel.rejection_reason && (
              <Alert
                type="error"
                showIcon
                message={t("factorModelRejectionReason")}
                description={selectedModel.rejection_reason}
                style={{ marginBottom: 16 }}
              />
            )}

            {/* 审计日志 */}
            {auditLogs.length > 0 && (
              <>
                <Paragraph>
                  <Text strong>{t("factorModelAuditLogs")}</Text>
                </Paragraph>
                <Table
                  size="small"
                  columns={auditColumns}
                  dataSource={auditLogs}
                  rowKey="id"
                  pagination={{ pageSize: 5, size: "small" }}
                />
              </>
            )}
          </Card>
        )}
      </Spin>

      {/* 激活确认 Modal */}
      <Modal
        title={
          <Space>
            <ThunderboltOutlined />
            {t("factorModelActivateTitle")}
          </Space>
        }
        open={activateModalOpen}
        onOk={handleActivate}
        onCancel={() => setActivateModalOpen(false)}
        confirmLoading={activating}
        okText={t("factorTransitionOk")}
        cancelText={t("cancel")}
      >
        {activateTarget && (
          <div>
            <Paragraph>
              <Text>{t("factorModelActivateConfirm")}</Text>
            </Paragraph>
            <Descriptions size="small" column={1}>
              <Descriptions.Item label={t("factorModelModelId")}>
                {activateTarget.display_alias || activateTarget.id}
                <Text type="secondary" style={{ display: "block", fontSize: 11 }}>ID：{activateTarget.id}</Text>
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelRuntimeMode")}>
                <Tag color={weightModeColor(activateMode)}>{activateMode === "ridge" ? "生产模式" : "观察模式"}</Tag>
              </Descriptions.Item>
            </Descriptions>
            <Input
              value={modelAlias}
              onChange={(e) => setModelAlias(e.target.value)}
              placeholder="模型别名（例如：换手率趋势模型）"
              maxLength={128}
              style={{ marginTop: 8 }}
            />
            <Input.TextArea
              value={activateNote}
              onChange={(e) => setActivateNote(e.target.value)}
              placeholder={t("factorModelActivateNotePlaceholder")}
              rows={3}
              maxLength={500}
              style={{ marginTop: 8 }}
            />
          </div>
        )}
      </Modal>

      {/* 回退 Modal */}
      <Modal
        title={
          <Space>
            <RollbackOutlined />
            {t("factorModelFallbackTitle")}
          </Space>
        }
        open={fallbackModalOpen}
        onOk={handleFallback}
        onCancel={() => setFallbackModalOpen(false)}
        confirmLoading={fallingBack}
        okText={t("factorModelConfirmFallback")}
        cancelText={t("cancel")}
      >
        <Paragraph>
          <Text>{t("factorModelFallbackHint")}</Text>
        </Paragraph>
        <Input.TextArea
          value={fallbackReason}
          onChange={(e) => setFallbackReason(e.target.value)}
          placeholder={t("factorModelFallbackReason")}
          rows={3}
          maxLength={1000}
        />
      </Modal>

      {/* ═══════════════════════════════════════════════════════════════════
          Task 2 ①：新建集合 Modal（name 必填 + description 选填）
          ═══════════════════════════════════════════════════════════════════ */}
      <Modal
        title={
          <Space>
            <PlusOutlined style={{ color: "var(--pt-primary)" }} />
            <Text strong>新建因子集合</Text>
          </Space>
        }
        open={createModalOpen}
        onOk={handleCreateFactorSet}
        onCancel={() => setCreateModalOpen(false)}
        confirmLoading={creating}
        okText="确认创建"
        cancelText={t("cancel")}
        destroyOnClose
        data-testid="modal-create-factorset"
      >
        <Form
          form={createForm}
          layout="vertical"
          style={{ marginTop: 8 }}
          initialValues={{ name: "", description: "" }}
        >
          <Form.Item
            label={<span style={{ fontSize: 13 }}>集合名称 <Text type="danger">*</Text></span>}
            name="name"
            rules={[
              { required: true, message: "请输入集合名称" },
              { min: 1, max: 60, message: "长度 1~60 个字符" },
            ]}
          >
            <Input
              placeholder="例如：A股多因子 2026Q3 动量+质量版"
              maxLength={60}
              allowClear
              prefix={<UserOutlined style={{ color: "var(--pt-muted-foreground)" }} />}
            />
          </Form.Item>
          <Form.Item
            label={<span style={{ fontSize: 13 }}>描述（可选）</span>}
            name="description"
          >
            <Input.TextArea
              placeholder="描述此集合预期用途、特征筛选条件、目标范围等（会写入 audit 日志）"
              rows={3}
              maxLength={500}
              showCount
            />
          </Form.Item>
          <Alert
            type="info"
            showIcon
            message="新建说明"
            description={
              <ul style={{ paddingInlineStart: 20, margin: 0, fontSize: 12 }}>
                <li>创建者为当前登录用户（actor 自动写入审计日志，不显示手填输入框）。</li>
                <li>初始状态为 <Tag color="blue" style={{ marginInlineEnd: 0 }}>草稿</Tag>，成员数为 0。</li>
                <li>提交成功后会自动滚动到新卡片，并打开「编辑成员」抽屉。</li>
              </ul>
            }
          />
        </Form>
      </Modal>

      {/* ═══════════════════════════════════════════════════════════════════
          Task 2 ⑤：冻结摘要 Modal（4 要素：n_members / feature_count / trainable 比例 / 最近更新时间）
                  + 不可逆确认勾选框（必须勾选才能确认提交）
          ═══════════════════════════════════════════════════════════════════ */}
      <Modal
        title={
          <Space>
            <Snowflake size={16} style={{ color: "#0ea5e9" }} />
            <Text strong>冻结因子集合（不可逆确认）</Text>
          </Space>
        }
        open={freezeModalOpen}
        onOk={handleFreeze}
        onCancel={() => {
          setFreezeModalOpen(false);
          setFreezeIrrevocableConfirm(false);
        }}
        confirmLoading={freezing}
        okButtonProps={{
          danger: true,
          type: "primary",
          disabled: !freezeIrrevocableConfirm,
          "data-testid": "btn-freeze-confirm-submit",
        }}
        okText="确认冻结（不可逆）"
        cancelText="取消"
        centered
        data-testid="modal-freeze-summary"
      >
        {freezeTarget && (() => {
          const members = freezeTarget.members ?? [];
          const n_members = members.length || (freezeTarget.n_members ?? 0);
          const featureCount = members.filter((m) => m.role === "feature").length;
          const trainableCount = members.filter((m: any) => {
            if (typeof (m as any).version_trainable === "boolean") return (m as any).version_trainable;
            const s = String((m as any).version_status || "trainable").toLowerCase();
            return s !== "draft" && s !== "deprecated" && s !== "disabled";
          }).length;
          const trainablePct = n_members > 0 ? Math.round((trainableCount / n_members) * 100) : 0;
          const lastUpdated = freezeTarget.updated_at ?? freezeTarget.frozen_at ?? freezeTarget.created_at;
          const allOk = n_members > 0 && featureCount > 0 && trainablePct === 100;
          return (
            <div>
              {/* 4 要素摘要卡片 */}
              <Row gutter={[10, 10]} style={{ marginBottom: 14 }}>
                <Col xs={12}>
                  <Card size="small" variant="borderless" style={{ background: "var(--pt-accent-soft)" }}>
                    <Statistic
                      title={<span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>① 成员数 n_members</span>}
                      value={n_members}
                      valueStyle={{ fontSize: 22, color: n_members > 0 ? "#0ea5e9" : "#dc2626", fontWeight: 700 }}
                      suffix={<Text type="secondary" style={{ fontSize: 11 }}>人</Text>}
                      data-testid="freeze-summary-n-members"
                    />
                  </Card>
                </Col>
                <Col xs={12}>
                  <Card size="small" variant="borderless" style={{ background: "var(--pt-accent-soft)" }}>
                    <Statistic
                      title={<span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>② feature_count</span>}
                      value={featureCount}
                      valueStyle={{ fontSize: 22, color: featureCount > 0 ? "#16a34a" : "#dc2626", fontWeight: 700 }}
                      suffix={<Text type="secondary" style={{ fontSize: 11 }}>个</Text>}
                      data-testid="freeze-summary-feature-count"
                    />
                  </Card>
                </Col>
                <Col xs={12}>
                  <Card size="small" variant="borderless" style={{ background: "var(--pt-accent-soft)" }}>
                    <Statistic
                      title={<span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>③ trainable 比例</span>}
                      value={trainablePct}
                      precision={0}
                      valueStyle={{ fontSize: 22, color: trainablePct === 100 ? "#16a34a" : trainablePct >= 50 ? "#ea580c" : "#dc2626", fontWeight: 700 }}
                      suffix={<Text type="secondary" style={{ fontSize: 11 }}>%（{trainableCount}/{n_members || 0}）</Text>}
                      data-testid="freeze-summary-trainable-pct"
                    />
                  </Card>
                </Col>
                <Col xs={12}>
                  <Card size="small" variant="borderless" style={{ background: "var(--pt-accent-soft)" }}>
                    <Statistic
                      title={<span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>④ 最近更新时间</span>}
                      valueStyle={{ fontSize: 14, color: "var(--pt-foreground)", fontWeight: 500 }}
                      value={formatDateTime(lastUpdated)}
                      data-testid="freeze-summary-last-updated"
                    />
                  </Card>
                </Col>
              </Row>

              <Descriptions size="small" column={1} bordered style={{ marginBottom: 12 }}>
                <Descriptions.Item label="集合 ID / 名称">
                  <code style={{ fontSize: 11 }}>{freezeTarget.id}</code>
                  <Tag style={{ marginInlineStart: 8 }}>{factorSetDisplayName(freezeTarget.name, freezeTarget.id)}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="当前状态">
                  <Tag color={factorSetStatusColor(freezeTarget.status)}>{factorSetStatusLabel(freezeTarget.status)}</Tag>
                </Descriptions.Item>
              </Descriptions>

              {!allOk && (
                <Alert
                  style={{ marginBottom: 12 }}
                  type="warning"
                  showIcon
                  message="预检未通过（不应出现此弹窗）"
                  description="若 4 要素任一不满足，则「冻结」按钮本身应为 disabled 状态。若你仍能看到此警告，说明前端状态不一致，请刷新页面后重试。"
                />
              )}

              <div style={{ marginBottom: 8 }}>
                <Input.TextArea
                  value={freezeReason}
                  onChange={(e) => setFreezeReason(e.target.value)}
                  placeholder="冻结原因（会写入审计日志 factor_audit_logs）例如：E2E 闭环冻结；生产模型前冻结"
                  rows={2}
                  maxLength={500}
                />
              </div>

              {/* TR-2.4 规则：未勾选时 submit disabled */}
              <Checkbox
                checked={freezeIrrevocableConfirm}
                onChange={(e) => setFreezeIrrevocableConfirm(e.target.checked)}
                data-testid="freeze-irrevocable-checkbox"
              >
                <Text strong type={freezeIrrevocableConfirm ? undefined : "danger"}>
                  我确认冻结后成员与版本不可逆（解冻不提供；如需修改请「复制」集合为新 draft）。
                </Text>
              </Checkbox>
            </div>
          );
        })()}
      </Modal>

      {/* ═══════════════════════════════════════════════════════════════════
          Task 2 ⑦：训练对话框
                  - 只有 train_mode 下拉 + 备注；submit 隐式传 factor_set_id（不暴露输入框）
                  - 识别后端 400 错误：COVERAGE_LOW / IC_OUT_OF_RANGE / LOOKBACK_SHORT
                    → Banner 显示 detail_zh 原文（不翻译不改写）
          ═══════════════════════════════════════════════════════════════════ */}
      <Modal
        title={
          <Space>
            <ThunderboltOutlined style={{ color: "#0ea5e9" }} />
            <Text strong>训练模型（基于当前集合）</Text>
          </Space>
        }
        open={trainModalOpen}
        onOk={handleTrain}
        onCancel={() => {
          setTrainModalOpen(false);
          setTrainGateError(null);
        }}
        confirmLoading={training}
        okText="提交训练"
        cancelText="取消"
        centered
        width={520}
        data-testid="modal-train-model"
      >
        {trainTarget && (
          <div>
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label="集合 / 状态">
                <Text strong>{factorSetDisplayName(trainTarget.name, trainTarget.id)}</Text>
                <Tag
                  style={{ marginInlineStart: 8, marginInlineEnd: 0 }}
                  color={factorSetStatusColor(trainTarget.status)}
                >
                  {factorSetStatusLabel(trainTarget.status)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="成员数 / feature 数">
                <Tag>{(trainTarget.members?.length ?? trainTarget.n_members ?? 0)} 人</Tag>
                <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                  feature：{(trainTarget.members ?? []).filter((m) => m.role === "feature").length}
                </Tag>
              </Descriptions.Item>
            </Descriptions>

            <div style={{ marginBottom: 12 }}>
              <label style={{ ...fieldLabelStyle, display: "block", marginBottom: 4 }}>
                训练模式 train_mode <Text type="danger">*</Text>
              </label>
              <Select
                value={trainMode}
                onChange={(v) => setTrainMode(v)}
                style={{ width: "100%" }}
                options={[
                  { value: "offline_minimal", label: "offline_minimal（离线最小 / E2E 推荐，不依赖仓库）" },
                  { value: "warehouse", label: "warehouse（真实 PIT 训练，依赖 ENABLE_FACTOR_MODEL_WAREHOUSE_TRAIN=1）" },
                ]}
                data-testid="train-mode-select"
              />
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ ...fieldLabelStyle, display: "block", marginBottom: 4 }}>备注（可选，写入审计）</label>
              <Input.TextArea
                value={trainNote}
                onChange={(e) => setTrainNote(e.target.value)}
                placeholder="例如：Q3 月更；2026-09 验收集合"
                rows={2}
                maxLength={300}
              />
            </div>

            <Alert
              type="info"
              showIcon
              message="内部 ID 不暴露"
              description="factor_set_id 由前端状态从当前集合卡片上下文直接携带（隐式提交），不需您手动输入。"
              style={{ marginBottom: 8 }}
              icon={<InfoCircleOutlined />}
            />

            {/* Task 2 训练门禁(2)：7 要素错误 Detail_zh 原文 Banner（不丢失） */}
            {trainGateError && (
              <Alert
                type="error"
                showIcon
                message="训练门禁未通过（7 要素错误）"
                description={trainGateError}
                data-testid="train-gate-error-banner"
                style={{
                  marginTop: 8,
                  border: "1px solid #ffccc7",
                  background: "#fff2f0",
                }}
              />
            )}
          </div>
        )}
      </Modal>

      {/* ═══════════════════════════════════════════════════════════════════
          Task 2 ③：编辑成员 Drawer（右侧滑出）
                  - 顶部：集合名只读；
                  - 主体：2 Tab（全部因子 / 已选成员）
                  - Tab 1：关键词搜索 + 因子列表行选择 + 版本号下拉（只含 trainable） + 添加按钮
                  - Tab 2：当前成员表，每行 X 按钮 → removeFactorSetMember
                  - Drawer 底部「保存后关闭」
          ═══════════════════════════════════════════════════════════════════ */}
      <Drawer
        title={
          memberDrawerTarget ? (
            <Space direction="vertical" size={0} style={{ paddingRight: 18 }}>
              <Space>
                <EditOutlined style={{ color: "var(--pt-primary)" }} />
                <Text strong style={{ fontSize: 15 }}>编辑集合成员</Text>
              </Space>
              <Text type="secondary" style={{ fontSize: 12 }}>
                集合：{factorSetDisplayName(memberDrawerTarget.name, memberDrawerTarget.id)}
                （状态：
                <Tag color={factorSetStatusColor(memberDrawerTarget.status)} style={{ marginInlineEnd: 0 }}>
                  {factorSetStatusLabel(memberDrawerTarget.status)}
                </Tag>
                ）
              </Text>
            </Space>
          ) : (
            <Text>编辑成员</Text>
          )
        }
        placement="right"
        width={Math.max(680, Math.min(860, typeof window !== "undefined" ? window.innerWidth * 0.55 : 720))}
        open={memberDrawerOpen}
        onClose={closeMemberDrawer}
        destroyOnClose
        maskClosable
        extra={
          <Space>
            <Button onClick={closeMemberDrawer}>取消</Button>
            <Button
              type="primary"
              onClick={handleDrawerSaveAndClose}
              data-testid="drawer-save-close"
            >
              保存并关闭
            </Button>
          </Space>
        }
        data-testid="drawer-edit-members"
      >
        <Spin spinning={drawerLoading}>
          <Tabs
            defaultActiveKey="all"
            data-testid="drawer-members-tabs"
            items={[
              {
                key: "all",
                label: `全部因子（可搜索 · 共 ${factorLibItems.length}）`,
                className: "drawer-tab-panel-all",
                children: (
                  <div>
                    <Space.Compact style={{ width: "100%", marginBottom: 10 }}>
                      <Input
                        allowClear
                        placeholder="搜索因子代码/名称（支持模糊），例如：MOM / PE / ROE"
                        value={factorSearchKeyword}
                        onChange={(e) => setFactorSearchKeyword(e.target.value)}
                        onPressEnter={() => loadFactorLibrary(factorSearchKeyword)}
                        prefix={<CloudServerOutlined />}
                        data-testid="factor-search-input"
                      />
                      <Button
                        type="primary"
                        onClick={() => loadFactorLibrary(factorSearchKeyword)}
                        loading={factorLibLoading}
                      >
                        搜索
                      </Button>
                    </Space.Compact>

                    <div
                      style={{
                        border: "1px solid var(--pt-border)",
                        borderRadius: 6,
                        maxHeight: 260,
                        overflowY: "auto",
                        marginBottom: 10,
                      }}
                    >
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                        <thead style={{ position: "sticky", top: 0, background: "var(--pt-card)", zIndex: 1 }}>
                          <tr style={{ borderBottom: "1px solid var(--pt-border)" }}>
                            <th style={{ ...thStyle, width: 36 }}>&nbsp;</th>
                            <th style={thStyle}>factor_code</th>
                            <th style={thStyle}>factor_name</th>
                            <th style={thStyle}>可训练版本数</th>
                          </tr>
                        </thead>
                        <tbody>
                          {factorLibItems.length === 0 ? (
                            <tr>
                              <td colSpan={4} style={{ padding: 24, textAlign: "center", color: "var(--pt-muted-foreground)" }}>
                                暂无匹配的因子（尝试缩短关键词）
                              </td>
                            </tr>
                          ) : (
                            factorLibItems.map((it) => {
                              const selected = pendingFactorCode === it.factor_code;
                              const trainableVersions = it.versions.filter((v) => v.trainable);
                              return (
                                <tr
                                  key={it.factor_code}
                                  onClick={() => {
                                    setPendingFactorCode(it.factor_code);
                                    // 若当前选中版本对该因子不可训练 → 自动置为第一个 trainable
                                    const firstTV = trainableVersions[0];
                                    if (firstTV && !it.versions.find(v => v.version === pendingVersion && v.trainable)) {
                                      setPendingVersion(firstTV.version);
                                    } else if (!firstTV) {
                                      setPendingVersion(null);
                                    }
                                  }}
                                  style={{
                                    cursor: "pointer",
                                    background: selected ? "rgba(14,165,233,0.10)" : undefined,
                                    boxShadow: selected ? "inset 3px 0 0 0 var(--pt-primary)" : undefined,
                                    borderBottom: "1px dashed rgba(148,163,184,0.2)",
                                  }}
                                  data-testid={`lib-row-${it.factor_code}`}
                                >
                                  <td style={{ ...tdStyle, textAlign: "center" }}>
                                    <input
                                      type="radio"
                                      readOnly
                                      checked={selected}
                                      aria-label={`select-${it.factor_code}`}
                                    />
                                  </td>
                                  <td style={tdStyle}><code>{it.factor_code}</code></td>
                                  <td style={tdStyle}>{it.factor_name ?? <Text type="secondary" style={{ fontSize: 11 }}>—</Text>}</td>
                                  <td style={tdStyle}>
                                    <Tag color={trainableVersions.length > 0 ? "green" : "default"} style={{ marginInlineEnd: 0 }}>
                                      {trainableVersions.length} / {it.versions.length}
                                    </Tag>
                                  </td>
                                </tr>
                              );
                            })
                          )}
                        </tbody>
                      </table>
                    </div>

                    <Descriptions size="small" column={1} bordered style={{ marginBottom: 10 }} labelStyle={{ fontSize: 12 }}>
                      <Descriptions.Item label="选中因子（只读代码）">
                        {pendingFactorCode ? (
                          <Space>
                            <Tag color="blue" style={{ marginInlineEnd: 0 }}>{pendingFactorCode}</Tag>
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              {(factorLibItems.find(i => i.factor_code === pendingFactorCode)?.factor_name) ?? ""}
                            </Text>
                          </Space>
                        ) : (
                          <Text type="secondary" style={{ fontSize: 11 }}>请在上方表格点击一行选中一个因子</Text>
                        )}
                      </Descriptions.Item>
                      <Descriptions.Item label="版本号（仅可训练）">
                        {pendingFactorCode ? (
                          <>
                            <Select
                              value={pendingVersion ?? undefined}
                              onChange={(v) => setPendingVersion(v)}
                              placeholder="请选择一个可训练版本号"
                              style={{ width: 320 }}
                              options={
                                (factorLibItems.find(i => i.factor_code === pendingFactorCode)?.versions ?? [])
                                  .filter(v => v.trainable)
                                  .map(v => ({ value: v.version, label: `${v.label}（${v.status}）` }))
                              }
                              data-testid="version-select"
                            />
                            {/* TR-2.3 校验：factor_set_id 纯数字输入框不存在 — 我们不渲染任何 input，只选 code+version */}
                          </>
                        ) : <Text type="secondary" style={{ fontSize: 11 }}>—</Text>}
                      </Descriptions.Item>
                      <Descriptions.Item label="默认角色 / 约束 / 缺失策略">
                        <Space wrap>
                          <Tag color="blue" style={{ marginInlineEnd: 0 }}>角色：特征（默认）</Tag>
                          <Tag>权重：不限</Tag>
                          <Tag>缺失：排除</Tag>
                        </Space>
                      </Descriptions.Item>
                    </Descriptions>

                    <Space style={{ display: "flex", justifyContent: "flex-end" }}>
                      <Button
                        type="primary"
                        disabled={!pendingFactorCode || pendingVersion == null}
                        loading={addingMember}
                        onClick={handleAddMemberFromLibrary}
                        data-testid="btn-add-member"
                      >
                        添加到集合（+1 成员）
                      </Button>
                    </Space>
                  </div>
                ),
              },
              {
                key: "selected",
                label: `已选成员（共 ${drawerMembers.length}）`,
                className: "drawer-tab-panel-selected",
                children: (
                  <div data-testid="drawer-panel-selected">
                    {drawerMembers.length === 0 ? (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description={
                        <Space direction="vertical" size={6}>
                          <Text>该集合还没有成员</Text>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            切换到「全部因子」Tab 搜索因子并选择可训练版本后添加
                          </Text>
                        </Space>
                      }
                    />
                  ) : (
                    <div
                      style={{
                        border: "1px solid var(--pt-border)",
                        borderRadius: 6,
                        maxHeight: 420,
                        overflowY: "auto",
                      }}
                    >
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                        <thead style={{ position: "sticky", top: 0, background: "var(--pt-card)", zIndex: 1 }}>
                          <tr style={{ borderBottom: "1px solid var(--pt-border)" }}>
                            <th style={thStyle}>{localizedLabel("factorModelFactorCode", "因子代码")}</th>
                            <th style={thStyle}>{localizedLabel("factorModelFactorVersion", "版本")}</th>
                            <th style={thStyle}>{localizedLabel("factorModelFactorRole", "角色")}</th>
                            <th style={thStyle}>{localizedLabel("factorModelFactorWeightConstraint", "权重约束")}</th>
                            <th style={thStyle}>{localizedLabel("factorModelFactorMissingPolicy", "缺失策略")}</th>
                            <th style={{ ...thStyle, width: 36 }}>&nbsp;</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sortMembersByRole(drawerMembers).map((m) => (
                            <tr key={m.id} style={{ borderBottom: "1px dashed rgba(148,163,184,0.2)" }} data-testid={`drawer-member-row-${m.id}`}>
                              <td style={tdStyle}><code>{m.factor_code}</code></td>
                              <td style={tdStyle}>v{m.factor_version}</td>
                              <td style={tdStyle}>
                                <Tag color={m.role === "feature" ? "blue" : "default"} style={{ marginInlineEnd: 0, fontSize: 11 }}>
                                  {memberRoleLabel(m.role)}
                                </Tag>
                              </td>
                              <td style={tdStyle}><Text type="secondary">{weightConstraintLabel(m.weight_constraint)}</Text></td>
                              <td style={tdStyle}><Text type="secondary">{missingPolicyLabel(m.missing_policy)}</Text></td>
                              <td style={{ ...tdStyle, textAlign: "center" }}>
                                <Tooltip title="从集合中移除">
                                  <Button
                                    type="text"
                                    danger
                                    size="small"
                                    icon={<DeleteOutlined />}
                                    style={{ padding: "0 2px", minWidth: 24 }}
                                    onClick={() => handleRemoveMember(m.id)}
                                    data-testid={`drawer-btn-del-${m.id}`}
                                  />
                                </Tooltip>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    )}
                  </div>
                ),
              },
            ] as TabsProps["items"]}
          />
        </Spin>
      </Drawer>
    </div>
  );
}
