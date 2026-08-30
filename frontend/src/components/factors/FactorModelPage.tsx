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
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Popover,
  Progress,
  Row,
  Col,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  ReloadOutlined,
  ThunderboltOutlined,
  RollbackOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
  InfoCircleOutlined,
  ExclamationCircleOutlined,
} from "@ant-design/icons";
import { Snowflake } from "lucide-react";
import { t } from "../../i18n";
import {
  api,
  type FactorRuntime,
  type FactorModelRun,
  type FactorSet,
  type ScoringModelDetail,
  type ScoringModelFactorMember,
} from "../../api/client";

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

/** 权重模式颜色映射。 */
function weightModeColor(mode: string): string {
  const map: Record<string, string> = {
    manual: "default",
    shadow: "orange",
    ridge: "green",
  };
  return map[mode] || "default";
}

/** FactorSet 状态颜色映射。 */
function factorSetStatusColor(status: string): string {
  const map: Record<string, string> = {
    draft: "default",
    frozen: "blue",
    deprecated: "red",
  };
  return map[status] || "default";
}

function factorSetDisplayName(name: string | null | undefined, id: string): string {
  const normalized = name?.trim() || "";
  return normalized && !/^[?\uFFFD\s]+$/.test(normalized) ? normalized : id;
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

  // P1.2.2：模型对比（最多 3 个）
  const [compareSelectedIds, setCompareSelectedIds] = useState<string[]>([]);
  const [compareOpen, setCompareOpen] = useState(false);
  const [compareLoading, setCompareLoading] = useState(false);

  // 激活 Modal
  const [activateModalOpen, setActivateModalOpen] = useState(false);
  const [activateTarget, setActivateTarget] = useState<FactorModelRun | null>(null);
  const [activateMode, setActivateMode] = useState<"shadow" | "ridge">("shadow");
  const [activateNote, setActivateNote] = useState("");
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
  const [training, setTraining] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [modelList, fsList] = await Promise.all([
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringGetFactorModelListAsFactor(20),
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringListFactorSetsAsFactor("any", 50),
      ]);
      setRuntime(modelList.runtime);
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

  /** P1: 用户在模型行点 FactorSet → 滚动到 FactorSet 表并高亮对应行。 */
  const jumpToFactorSet = useCallback((fsId: string) => {
    setHighlightFactorSetId(fsId);
    if (factorSetTableRef.current) {
      factorSetTableRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    window.setTimeout(() => setHighlightFactorSetId(null), 2600);
  }, []);

  // 打开激活 Modal
  const openActivateModal = (model: FactorModelRun, mode: "shadow" | "ridge") => {
    setActivateTarget(model);
    setActivateMode(mode);
    setActivateNote("");
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

  // WP0-8：冻结 FactorSet 确认
  const handleFreeze = async () => {
    if (!freezeTarget) return;
    setFreezing(true);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const updated = await api.scoringFreezeFactorSet(freezeTarget.id, freezeReason.trim() || "E2E 冻结：离线训练前", "factor_center:freeze");
      setFactorSets((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
      message.success(`FactorSet 已冻结：${updated.id}`);
      setFreezeModalOpen(false);
      setFreezeReason("E2E 冻结：离线训练前");
      await loadData();
    } catch (err) {
      message.error("冻结 FactorSet 失败：" + String(err));
    } finally {
      setFreezing(false);
    }
  };

  // WP0-8：训练模型确认（离线最小 / 仓库）
  const handleTrain = async () => {
    if (!trainTarget) return;
    if (!trainTarget.id) {
      message.warning("请先选择要训练的 FactorSet");
      return;
    }
    if (trainTarget.status !== "frozen" && trainMode === "warehouse") {
      const ok = await new Promise<boolean>((resolve) => {
        modal.confirm({
          title: "FactorSet 未冻结",
          content: `当前 FactorSet（${trainTarget.id}）状态为 ${trainTarget.status}。生产环境建议先冻结以保证版本一致性，是否仍继续训练？`,
          okText: "仍继续",
          cancelText: "取消",
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
      if (!ok) return;
    }
    setTraining(true);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const created = await api.scoringTrainModel(trainTarget.id, trainMode, "factor_center:train");
      message.success(
        trainMode === "offline_minimal"
          ? `离线最小模型训练成功：${created.id.slice(0, 12)}…（status=${created.status}）`
          : `仓库训练已提交：${created.id.slice(0, 12)}…`,
      );
      setTrainModalOpen(false);
      setSelectedModel(created);
      await loadData();
    } catch (err: any) {
      const errMsg = err?.message ? String(err.message) : String(err);
      message.error("训练失败：" + errMsg);
    } finally {
      setTraining(false);
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
      title: "FactorSet",
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
                {record.status}
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
      expandedRowRender: (record: FactorModelRun) => {
        const detail = modelDetailMap[record.id];
        const loading = detailLoadingSet.has(record.id);
        if (loading && !detail) {
          return (
            <div style={{ padding: "4px 12px 12px" }}>
              <Spin size="small" tip="加载因子构成…" />
            </div>
          );
        }
        if (!detail) {
          return (
            <div style={{ padding: "4px 12px 12px" }}>
              <Text type="secondary">暂无权重详情</Text>
            </div>
          );
        }
        const factors: ScoringModelFactorMember[] = detail.factors ?? [];
        const maxAbsW = factors.reduce(
          (m, f) => Math.max(m, Math.abs(f.normalized_weight || 0)),
          0
        );
        const sumAbsW = factors.reduce(
          (m, f) => m + Math.abs(f.normalized_weight || 0),
          0
        );

        return (
          <div style={{ padding: 4, marginBottom: 8 }}>
            {/* 顶部指标卡片 */}
            <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
              <Col xs={12} sm={8} md={6}>
                <Card size="small" variant="borderless" style={{ background: "var(--pt-accent-soft)" }}>
                  <Statistic
                    title={<span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>校验 IC</span>}
                    value={detail.validation_ic ?? undefined}
                    precision={4}
                    valueStyle={{ color: icColor(detail.validation_ic) }}
                    suffix={
                      <Tooltip title="训练期 IC（同一模型上的）">
                        <InfoCircleOutlined style={{ color: "var(--pt-muted-foreground)" }} />
                      </Tooltip>
                    }
                  />
                  <div style={{ marginTop: 2 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      训练 IC：<span style={{ color: icColor(detail.train_ic) }}>
                        {formatNumber(detail.train_ic)}
                      </span>
                    </Text>
                  </div>
                </Card>
              </Col>
              <Col xs={12} sm={8} md={6}>
                <Card size="small" variant="borderless" style={{ background: "var(--pt-accent-soft)" }}>
                  <Statistic
                    title={<span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>样本数</span>}
                    value={detail.sample_count || 0}
                    valueStyle={{ color: "var(--pt-foreground)" }}
                  />
                  <div style={{ marginTop: 2 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      交易日 {detail.trade_date_count || 0} · 个股 {detail.symbol_count || 0}
                    </Text>
                  </div>
                </Card>
              </Col>
              <Col xs={12} sm={8} md={6}>
                <Card size="small" variant="borderless" style={{ background: "var(--pt-accent-soft)" }}>
                  <Statistic
                    title={<span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                      因子集 / 因子数
                    </span>}
                    value={detail.factorset_member_count || factors.length || 0}
                    prefix={<Snowflake size={14} style={{ color: "var(--pt-muted-foreground)" }} />}
                    valueStyle={{ color: "var(--pt-foreground)" }}
                  />
                  <div style={{ marginTop: 2 }}>
                    <Tooltip title={detail.factorset_id ?? ""}>
                      <Tag color="blue" style={{ maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {detail.factorset_label || detail.factorset_id || "（未关联）"}
                      </Tag>
                    </Tooltip>
                  </div>
                </Card>
              </Col>
              <Col xs={12} sm={8} md={6}>
                <Card size="small" variant="borderless" style={{ background: "var(--pt-accent-soft)" }}>
                  <Statistic
                    title={<span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>校验 R² / ICIR</span>}
                    value={detail.validation_r2 == null ? "-" : detail.validation_r2}
                    precision={detail.validation_r2 == null ? 0 : 4}
                    valueStyle={{ color: "var(--pt-foreground)" }}
                  />
                  <div style={{ marginTop: 2 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      ICIR：
                      <span style={{ color: icColor(detail.validation_icir) }}>
                        {formatNumber(detail.validation_icir)}
                      </span>
                    </Text>
                  </div>
                </Card>
              </Col>
            </Row>

            {/* 因子权重列表：名称+code+版本 / 方向标签 / 权重条（绝对值）/ 归一化权重 / Val IC / 覆盖率 */}
            {factors.length === 0 ? (
              <Empty description="模型暂无权重快照（可能是训练中或该模型未持久化权重）" />
            ) : (
              <div
                style={{
                  border: "1px solid var(--pt-border)",
                  borderRadius: 8,
                  padding: 12,
                  background: "var(--pt-card)",
                }}
              >
                <Text strong style={{ display: "block", marginBottom: 8 }}>
                  因子构成（{factors.length} 个，按 |权重| 降序，|权重| 和 = {formatNumber(sumAbsW, 3)}）
                </Text>
                <Table
                  size="small"
                  rowKey="factor_code"
                  pagination={factors.length > 12 ? { pageSize: 12, size: "small", hideOnSinglePage: true } : false}
                  columns={[
                    {
                      title: "因子（名称 / code / 版本）",
                      key: "factor",
                      render: (_: unknown, f: ScoringModelFactorMember) => (
                        <Space direction="vertical" size={2}>
                          <Text strong>{f.factor_name || f.factor_code}</Text>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {f.factor_code} · v{f.factor_version}
                          </Text>
                        </Space>
                      ),
                    },
                    {
                      title: "多/空",
                      key: "side",
                      width: 90,
                      render: (_: unknown, f: ScoringModelFactorMember) => {
                        if (f.side === "long")
                          return (
                            <Tag color="green" icon={<ArrowUpOutlined />}>
                              LONG
                            </Tag>
                          );
                        if (f.side === "short")
                          return (
                            <Tag color="red" icon={<ArrowDownOutlined />}>
                              SHORT
                            </Tag>
                          );
                        return <Tag>—</Tag>;
                      },
                    },
                    {
                      title: (
                        <Tooltip title="Ridge 原始回归系数（未归一化）。">
                          <span>原始系数 <InfoCircleOutlined /></span>
                        </Tooltip>
                      ),
                      key: "coef",
                      width: 130,
                      align: "right" as const,
                      render: (_: unknown, f: ScoringModelFactorMember) => (
                        <Text code style={{ fontSize: 12 }}>
                          {Number.isFinite(f.coefficient) ? f.coefficient.toExponential(2) : "-"}
                        </Text>
                      ),
                    },
                    {
                      title: "权重 |w|（相对）",
                      key: "weight_bar",
                      minWidth: 240,
                      render: (_: unknown, f: ScoringModelFactorMember) => {
                        const wAbs = Math.abs(f.normalized_weight || 0);
                        const pct = maxAbsW > 0 ? (wAbs / maxAbsW) * 100 : 0;
                        return (
                          <Space direction="vertical" size={2} style={{ width: "100%" }}>
                            <div
                              style={{
                                height: 10,
                                background: "var(--pt-muted)",
                                borderRadius: 5,
                                overflow: "hidden",
                                opacity: 0.6,
                              }}
                            >
                              <div
                                style={{
                                  width: `${pct}%`,
                                  height: "100%",
                                  background: sideColor(f.side),
                                  transition: "width .2s",
                                }}
                              />
                            </div>
                            <Progress
                              percent={sumAbsW > 0 ? (wAbs / sumAbsW) * 100 : 0}
                              size="small"
                              showInfo
                              strokeColor={sideColor(f.side)}
                              style={{ margin: 0 }}
                              format={(p) =>
                                `${wAbs.toFixed(3)} ${p != null ? `(${p.toFixed(1)}%)` : ""}`
                              }
                            />
                          </Space>
                        );
                      },
                    },
                    {
                      title: "校验 IC",
                      key: "val_ic",
                      width: 100,
                      align: "right" as const,
                      render: (_: unknown, f: ScoringModelFactorMember) => (
                        <Tooltip title="该因子在本次模型训练中的校验集 IC（非全局因子 IC）">
                          <span style={{ color: icColor(f.validation_ic), fontWeight: 500 }}>
                            {formatNumber(f.validation_ic)}
                          </span>
                        </Tooltip>
                      ),
                    },
                    {
                      title: "覆盖率",
                      key: "coverage",
                      width: 150,
                      render: (_: unknown, f: ScoringModelFactorMember) => {
                        const pct = coveragePercent(f.coverage);
                        return pct > 0 ? (
                          <Space size={6}>
                            <Progress
                              percent={pct}
                              size="small"
                              strokeColor={coverageColor(pct)}
                              showInfo={false}
                              style={{ width: 80, margin: 0 }}
                            />
                            <Text style={{ fontSize: 12, color: coverageColor(pct), minWidth: 42 }}>
                              {pct.toFixed(1)}%
                            </Text>
                          </Space>
                        ) : (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            <Tooltip title="无当日因子健康数据（仓库/特征未就绪）">
                              - <InfoCircleOutlined />
                            </Tooltip>
                          </Text>
                        );
                      },
                    },
                  ]}
                  dataSource={factors}
                />
              </div>
            )}
            {detail.status === "rejected" && !!detail.rejection_reason && (
              <Alert
                style={{ marginTop: 12 }}
                type="error"
                showIcon
                message="模型被门禁拒绝"
                description={detail.rejection_reason}
              />
            )}
          </div>
        );
      },
      onExpand: async (expanded: boolean, record: FactorModelRun) => {
        if (expanded) await ensureModelDetail(record.id);
      },
      rowExpandable: () => true,
    }),
    [detailLoadingSet, ensureModelDetail, factorSets, modelDetailMap]
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
                              {m.status}
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
        <Tag color={factorSetStatusColor(status)}>{status}</Tag>
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
            {record.new_mode ?? "-"}
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
                  {runtime.weight_mode}
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

        {/* FactorSet 列表 */}
        <Card
          size="small"
          title={t("factorModelFactorSets")}
          style={{ marginBottom: 16 }}
        >
          {factorSets.length > 0 ? (
            <div ref={factorSetTableRef}>
              <Table
                size="small"
                columns={factorSetColumns}
                dataSource={factorSets}
                rowKey="id"
                expandable={factorSetExpandable}
                rowClassName={(record) =>
                  highlightFactorSetId && record.id === highlightFactorSetId
                    ? "row-highlight-pulse"
                    : ""
                }
                pagination={{ pageSize: 5, size: "small" }}
              />
              {/* highlight 动画：短暂黄色呼吸描边 */}
              <style>{`
                .row-highlight-pulse > td {
                  background-color: rgba(250, 204, 21, 0.22) !important;
                  transition: background-color 0.2s linear;
                  animation: rowHighLightPulse 1.1s ease-out 2;
                  box-shadow: inset 0 0 0 1px var(--pt-primary);
                }
                @keyframes rowHighLightPulse {
                  0% { background-color: rgba(250, 204, 21, 0.45); }
                  100% { background-color: rgba(250, 204, 21, 0.12); }
                }
              `}</style>
            </div>
          ) : (
            <Empty description={t("factorModelNoFactorSets")} />
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
            <Table
              size="small"
              columns={modelColumns}
              dataSource={models}
              rowKey="id"
              expandable={modelExpandable as any}
              rowClassName={(record) => {
                const base = "";
                const rejected = (record as FactorModelRun).status === "rejected";
                if (rejected) return `${base} row-rejected-redgate`;
                return base;
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
                  {selectedModel.status}
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
                {activateTarget.id}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorModelRuntimeMode")}>
                <Tag color={weightModeColor(activateMode)}>{activateMode}</Tag>
              </Descriptions.Item>
            </Descriptions>
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

      {/* WP0-8：冻结 FactorSet Modal */}
      <Modal
        title={<Space><Snowflake />冻结 FactorSet（保证版本可溯源）</Space>}
        open={freezeModalOpen}
        onOk={handleFreeze}
        onCancel={() => setFreezeModalOpen(false)}
        confirmLoading={freezing}
        okText="确认冻结"
        cancelText={t("cancel")}
      >
        {freezeTarget && (
          <>
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label="FactorSet ID">{freezeTarget.id}</Descriptions.Item>
              <Descriptions.Item label="名称 / 状态">
                {factorSetDisplayName(freezeTarget.name, freezeTarget.id)}
                <Tag style={{ marginLeft: 8 }} color={factorSetStatusColor(freezeTarget.status)}>{freezeTarget.status}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="成员数 / version">
                {freezeTarget.n_members ?? "—"} 人 · v{freezeTarget.version ?? 1}
              </Descriptions.Item>
            </Descriptions>
            <Input.TextArea
              value={freezeReason}
              onChange={(e) => setFreezeReason(e.target.value)}
              placeholder="冻结原因（会写入 audit，例如：E2E 冻结/生产前冻结）"
              rows={3}
              maxLength={500}
            />
          </>
        )}
      </Modal>

      {/* WP0-8：训练模型 Modal（offline_minimal / warehouse） */}
      <Modal
        title={<Space><ThunderboltOutlined />训练 FactorModel（基于冻结 FactorSet）</Space>}
        open={trainModalOpen}
        onOk={handleTrain}
        onCancel={() => setTrainModalOpen(false)}
        confirmLoading={training}
        okText={trainMode === "offline_minimal" ? "立即训练（离线最小）" : "提交仓库训练"}
        cancelText={t("cancel")}
      >
        {trainTarget && (
          <>
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label="FactorSet ID / 状态">
                {trainTarget.id}
                <Tag
                  style={{ marginLeft: 8 }}
                  color={factorSetStatusColor(trainTarget.status)}
                >
                  {trainTarget.status}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="名称 / version / 成员">
                {factorSetDisplayName(trainTarget.name, trainTarget.id)}
                {" · v"}{trainTarget.version ?? 1}
                {" · "}{trainTarget.n_members ?? 0} 因子
              </Descriptions.Item>
              {trainTarget.status !== "frozen" ? (
                <Descriptions.Item label="⚠️ 未冻结提示">
                  <Text type="warning">当前 FactorSet 非 frozen。若使用"仓库真实 PIT 训练"请先冻结；离线最小（offline_minimal）模式仍可执行 E2E 闭环。</Text>
                </Descriptions.Item>
              ) : null}
            </Descriptions>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <div>
                <label style={{ ...fieldLabelStyle, display: "block", marginBottom: 4 }}>资产类型</label>
                <select
                  className="pt-select"
                  style={{ width: "100%" }}
                  value={trainAssetType}
                  onChange={(e) => setTrainAssetType(e.target.value as any)}
                >
                  <option value="STOCK">STOCK（A 股）</option>
                  <option value="ETF">ETF</option>
                  <option value="US_STOCK">US_STOCK</option>
                  <option value="HK_STOCK">HK_STOCK</option>
                </select>
              </div>
              <div>
                <label style={{ ...fieldLabelStyle, display: "block", marginBottom: 4 }}>训练模式（WP0-7 真实路由契约）</label>
                <select
                  className="pt-select"
                  style={{ width: "100%" }}
                  value={trainMode}
                  onChange={(e) => setTrainMode(e.target.value as any)}
                  title="offline_minimal：不需要 FactorWarehouse；warehouse：真实 PIT 训练，需 ENABLE_FACTOR_MODEL_WAREHOUSE_TRAIN=1"
                >
                  <option value="offline_minimal">offline_minimal（推荐 E2E / 不依赖仓库）</option>
                  <option value="warehouse">warehouse（真实 PIT 训练）</option>
                </select>
              </div>
            </div>
            {trainMode === "offline_minimal" ? (
              <Alert
                style={{ marginTop: 12 }}
                type="info"
                showIcon
                message="offline_minimal 行为"
                description="直接基于 FactorSet 成员生成 status=validated 的最小 Ridge 模型 + 等权 FactorWeightSnapshot，并写 hyperparameters.factor_set_id。适合 E2E 闭环与前端联调。"
              />
            ) : (
              <Alert
                style={{ marginTop: 12 }}
                type="warning"
                showIcon
                message="warehouse 行为"
                description="调用真实 PIT 训练（若后端环境未启用会返回 503 CAPABILITY_BLOCKED，并给出 next_actions 指引）。生产环境使用需先保证 FactorWarehouse 可用。"
              />
            )}
          </>
        )}
      </Modal>
    </div>
  );
}
