import { useState, useEffect, useCallback } from "react";
import { useApp } from "../../context/AppContext";
import { t, template, factorLabel, factorCategoryLabel, factorDirectionLabel } from "../../i18n";
import {
  api,
  type FactorDefinition,
  type ScoringFactorDraft,
  type ScoringDraftStatus,
  type ScoringFactorUsage,
  type ScoringFactorUsageModel,
  type ScoringFactorUsageSet,
} from "../../api/client";
import {
  Card,
  Descriptions,
  Divider,
  Drawer,
  List,
  Modal,
  Progress,
  Table,
  Input,
  Select,
  Button,
  Space,
  Tag,
  Empty,
  Spin,
  App,
  Row,
  Col,
  Tabs,
  Tooltip,
  Typography,
  Statistic,
  Badge,
} from "antd";
import {
  PlusOutlined,
  FilterOutlined,
  InfoCircleOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
  LinkOutlined,
  FileTextOutlined,
  CheckOutlined,
  CloseOutlined,
} from "@ant-design/icons";

const { Text, Paragraph } = Typography;

type FactorLibraryProps = {
  onViewDetail: (code: string) => void;
  onEditFactor: (code: string) => void;
  onNewFactor: () => void;
};

type FactorStats = {
  total: number;
  evaluable: number;
  shadow: number;
  active: number;
  updatedThisWeek: number;
  shadowDriftWarning: number;
  shadowGatePassRate: number;
};

const INITIAL_STATS: FactorStats = {
  total: 0,
  evaluable: 0,
  shadow: 0,
  active: 0,
  updatedThisWeek: 0,
  shadowDriftWarning: 0,
  shadowGatePassRate: 0,
};

export default function FactorLibrary({ onViewDetail, onEditFactor, onNewFactor }: FactorLibraryProps) {
  const ctx = useApp();
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<FactorDefinition[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [kindFilter, setKindFilter] = useState<string | undefined>(undefined);
  const [categoryFilter, setCategoryFilter] = useState<string | undefined>(undefined);
  const [stats, setStats] = useState<FactorStats>(INITIAL_STATS);
  const [statsLoading, setStatsLoading] = useState(false);

  // ═══════════════════════════════════════════════════════
  // P1.3 因子→因子集/模型 使用情况（抽屉 + 懒加载缓存）
  // ═══════════════════════════════════════════════════════
  // ── Usage 抽屉 ──────────────────────────────────────────────────────────
  const [usageDrawerOpen, setUsageDrawerOpen] = useState(false);
  const [usageDrawerCode, setUsageDrawerCode] = useState<string | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  /** code -> digest；命中直接使用 */
  const [usageCache, setUsageCache] = useState<Record<string, ScoringFactorUsage>>({});
  /** Set of codes currently loading (dedup). */
  const usageLoadingSetRef = useState<Set<string>>(new Set())[0];

  // ── P2.2d 草稿治理 Tab 状态 ─────────────────────────────────────────────
  const [drafts, setDrafts] = useState<ScoringFactorDraft[]>([]);
  const [draftsLoading, setDraftsLoading] = useState(false);
  const [draftStatus, setDraftStatus] = useState<ScoringDraftStatus | "any">("any");
  const [draftSource, setDraftSource] = useState<string | undefined>(undefined);
  const [draftPayloadOpen, setDraftPayloadOpen] = useState(false);
  const [draftPayloadRow, setDraftPayloadRow] = useState<ScoringFactorDraft | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectRow, setRejectRow] = useState<ScoringFactorDraft | null>(null);
  const [rejectMsg, setRejectMsg] = useState("");
  const [reviewBusy, setReviewBusy] = useState<string | null>(null);

  const icColor = (value: number | null | undefined): string => {
    if (value == null || !Number.isFinite(value as number)) return "var(--pt-muted-foreground)";
    const v = value as number;
    if (v >= 0.03) return "#16a34a";
    if (v >= 0.015) return "#65a30d";
    if (v >= 0) return "#6b7280";
    if (v >= -0.015) return "#ea580c";
    return "#dc2626";
  };
  const coveragePct = (v: number | null | undefined): number =>
    v == null || !Number.isFinite(v as number) ? 0 : Math.max(0, Math.min(100, Number(v) * 100));
  const coverageColor = (p: number): string =>
    p >= 90 ? "#16a34a" : p >= 70 ? "#65a30d" : p >= 50 ? "#ca8a04" : "#dc2626";

  /** 预取单条 usage（幂等 + 去重）。 */
  const prefetchUsage = useCallback(
    async (code: string): Promise<ScoringFactorUsage | null> => {
      if (usageCache[code]) return usageCache[code];
      if (usageLoadingSetRef.has(code)) {
        // 等待已在进行中的请求
        for (let i = 0; i < 40; i += 1) {
          await new Promise((r) => setTimeout(r, 100));
          if (usageCache[code]) return usageCache[code];
          if (!usageLoadingSetRef.has(code)) break;
        }
        return usageCache[code] ?? null;
      }
      usageLoadingSetRef.add(code);
      try {
        const data = await api.scoringGetFactorUsage(code);
        setUsageCache((c) => ({ ...c, [code]: data }));
        return data;
      } catch {
        // 静默：单个 factor 获取失败不影响全局
        return null;
      } finally {
        usageLoadingSetRef.delete(code);
      }
    },
    [usageCache, usageLoadingSetRef]
  );

  /** 批量预取当前页的 usage（并发控制 3）。 */
  const prefetchPageUsages = useCallback(
    async (codes: string[]) => {
      const missing = codes.filter((c) => !usageCache[c] && !usageLoadingSetRef.has(c));
      if (!missing.length) return;
      const worker = async (queue: string[]) => {
        while (queue.length) {
          const code = queue.shift()!;
          await prefetchUsage(code);
        }
      };
      const concurrency = Math.min(3, missing.length);
      const queues: string[][] = Array.from({ length: concurrency }, () => []);
      missing.forEach((c, i) => queues[i % concurrency].push(c));
      await Promise.all(queues.map((q) => worker(q)));
    },
    [prefetchUsage, usageCache, usageLoadingSetRef]
  );

  /** 打开 usage 抽屉（若未命中 cache 则拉取）。 */
  const openUsageDrawer = useCallback(
    async (code: string) => {
      setUsageDrawerCode(code);
      setUsageDrawerOpen(true);
      setUsageLoading(true);
      try {
        await prefetchUsage(code);
      } catch (err) {
        message.error("加载因子使用信息失败：" + String(err));
      } finally {
        setUsageLoading(false);
      }
    },
    [prefetchUsage, message]
  );

  const isZh = ctx.locale.startsWith("zh");

  // 计算统计数据
  const loadStats = useCallback(async () => {
    setStatsLoading(true);
    try {
      const pageSize = 100;
      // Factor-domain internal - DO NOT USE outside factor center
      const resp = await api.scoringListFactorDefinitions({
        page: 1,
        page_size: pageSize,
      });
      const allItems = [...(resp.items || [])];
      const expectedTotal = Math.max(resp.total || 0, allItems.length);
      for (let statsPage = 2; allItems.length < expectedTotal; statsPage += 1) {
        // Factor-domain internal - DO NOT USE outside factor center
        const next = await api.scoringListFactorDefinitions({ page: statsPage, page_size: pageSize });
        if (!next.items?.length) break;
        allItems.push(...next.items);
      }
      const now = new Date();
      const weekAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);

      const updatedThisWeek = allItems.filter((f) => {
        if (!f.updated_at) return false;
        return new Date(f.updated_at) >= weekAgo;
      }).length;

      const evaluable = allItems.filter((f) =>
        f.lifecycle_status === "candidate" || f.lifecycle_status === "testing" || f.lifecycle_status === "shadow" || f.lifecycle_status === "active"
      ).length;

      const shadowCount = allItems.filter((f) => f.lifecycle_status === "shadow").length;
      const activeCount = allItems.filter((f) => f.lifecycle_status === "active").length;

      setStats({
        total: expectedTotal,
        evaluable,
        shadow: shadowCount,
        active: activeCount,
        updatedThisWeek,
        shadowDriftWarning: Math.floor(shadowCount * 0.25), // 模拟值，实际需要漂移检测接口
        shadowGatePassRate: 91, // 模拟值，实际需要统计接口
      });
    } catch {
      // 静默失败，不影响主列表
    } finally {
      setStatsLoading(false);
    }
  }, []);

  const loadFactors = useCallback(async () => {
    setLoading(true);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const resp = await api.scoringListFactorDefinitions({
        search: search || undefined,
        lifecycle_status: statusFilter,
        factor_kind: kindFilter,
        category: categoryFilter || undefined,
        page,
        page_size: pageSize,
      });
      setItems(resp.items || []);
      setTotal(resp.total || 0);
    } catch (err: any) {
      message.error(err?.message || t("factorLibraryLoadFailed"));
    } finally {
      setLoading(false);
    }
  }, [search, statusFilter, kindFilter, categoryFilter, page, pageSize, message]);

  useEffect(() => {
    loadFactors();
    loadStats();
  }, [loadFactors, loadStats]);

  // P1.3.1：items 更新后自动预取当前页所有因子的 usage，用于摘要行/胶囊展示。
  useEffect(() => {
    if (!items.length) return;
    const codes = items.map((f) => f.code).filter(Boolean);
    void prefetchPageUsages(codes);
    // 仅在列表内容刷新时预取；deps 故意不包含 prefetchPageUsages，避免循环触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  // 生命周期状态颜色映射
  const statusColor = (status: string | null): string => {
    if (!status) return "default";
    const map: Record<string, string> = {
      draft: "default",
      candidate: "blue",
      testing: "orange",
      shadow: "gold",
      active: "green",
      quarantined: "red",
      deprecated: "gray",
      rejected: "red",
    };
    return map[status] || "default";
  };

  const statusLabel = (status: string | null): string => {
    if (!status) return "-";
    return t(`factorStatus_${status}`);
  };

  const kindLabel = (kind: string | null): string => {
    if (!kind) return "-";
    return t(`factorKind_${kind}`);
  };

  const formatTime = (value: string | null | undefined): string => {
    if (!value) return "-";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const hours = String(d.getHours()).padStart(2, "0");
    const minutes = String(d.getMinutes()).padStart(2, "0");
    return `${month}-${day} ${hours}:${minutes}`;
  };

  // 模拟数据覆盖率（实际应从因子评估接口获取）
  const getDataCoverage = (_record: FactorDefinition): string => {
    // 这里可以后续接入真实的覆盖率数据
    const mockRates = ["98.7%", "93.2%", "76.4%", "99.1%", "88.0%", "95.6%", "82.3%", "91.8%"];
    const hash = _record.code.split("").reduce((acc, c) => acc + c.charCodeAt(0), 0);
    return mockRates[hash % mockRates.length];
  };

  // 模拟评价结论
  const getEvaluationResult = (record: FactorDefinition): { label: string; color: string } => {
    const status = record.lifecycle_status;
    if (status === "active" || status === "shadow") {
      return { label: isZh ? "通过" : "Pass", color: "green" };
    }
    if (status === "testing") {
      return { label: isZh ? "复核" : "Review", color: "gold" };
    }
    if (status === "rejected" || status === "quarantined") {
      return { label: isZh ? "阻断" : "Blocked", color: "red" };
    }
    if (status === "candidate") {
      return { label: isZh ? "待评价" : "Pending", color: "default" };
    }
    return { label: isZh ? "待评价" : "Pending", color: "default" };
  };

  // 获取分类列表（用于筛选）
  const categoryOptions = Array.from(
    new Set(items.map((f) => f.category).filter(Boolean) as string[])
  ).map((cat) => ({ value: cat, label: factorCategoryLabel(cat) }));

  /** 根据缓存直接读取 usage（用于表格渲染）；没有则返回 null。 */
  const peekUsage = (code: string): ScoringFactorUsage | null => usageCache[code] ?? null;

  const columns = [
    {
      title: t("factorColFactor"),
      dataIndex: "code",
      key: "factor",
      width: 260,
      render: (_: string, record: FactorDefinition) => {
        const u = peekUsage(record.code);
        const setCount = u?.in_factor_sets?.length ?? 0;
        const modelCount = u?.in_models?.length ?? 0;
        const ic = u?.ic_mean_30d;
        const cov = u?.coverage_30d;
        const prodDays = u?.days_in_production;
        return (
          <div>
            <div
              className="factor-name-cell"
              style={{ fontWeight: 600, fontSize: 16, color: "#0f766e", cursor: "pointer" }}
              onClick={() => onViewDetail(record.code)}
            >
              {factorLabel(record.code, record.name)}
            </div>
            <div style={{ color: "#64748b", fontSize: 12, marginTop: 2 }}>{record.code}</div>

            {/* P1.3.1 两行：参与情况 + 质量指标 */}
            <div style={{ marginTop: 6, fontSize: 12, color: "#475569" }}>
              <span
                style={{ cursor: "pointer", color: "#0891b2", textDecoration: "underline dotted" }}
                onClick={() => openUsageDrawer(record.code)}
              >
                参与 {setCount} 个因子集 · {modelCount} 个模型
              </span>
            </div>
            <div style={{ marginTop: 3, display: "flex", flexWrap: "wrap", gap: 6 }}>
              {u ? (
                <>
                  <Tag
                    color={ic != null && ic >= 0.015 ? "green" : ic != null && ic >= 0 ? "default" : "orange"}
                    style={{ marginInlineEnd: 0, fontSize: 11 }}
                  >
                    IC {ic != null && Number.isFinite(ic) ? (ic * 100).toFixed(2) + "%" : "-"}
                  </Tag>
                  <Tag
                    color={cov != null && cov >= 0.7 ? "green" : cov != null && cov >= 0.5 ? "gold" : "red"}
                    style={{ marginInlineEnd: 0, fontSize: 11 }}
                  >
                    覆盖率 {cov != null && Number.isFinite(cov) ? (cov * 100).toFixed(0) + "%" : "-"}
                  </Tag>
                  <Tag style={{ marginInlineEnd: 0, fontSize: 11 }} color="blue">
                    生产 {prodDays != null && Number.isFinite(prodDays) ? prodDays + " 天" : "-"}
                  </Tag>
                </>
              ) : (
                <Tag style={{ marginInlineEnd: 0, fontSize: 11, color: "#94a3b8" }}>
                  点击右侧 🔗 查看使用详情
                </Tag>
              )}
            </div>
          </div>
        );
      },
    },
    {
      title: t("factorColKind"),
      dataIndex: "factor_kind",
      key: "factor_kind",
      width: 100,
      render: (kind: string | null) => kindLabel(kind),
    },
    {
      title: t("factorColCurrentVersion"),
      key: "version",
      width: 100,
      render: (_: unknown, record: FactorDefinition) => {
        const versionId = record.active_version_id ?? record.shadow_version_id;
        return versionId ? `v${versionId}` : "-";
      },
    },
    {
      title: t("factorColDataCoverage"),
      key: "coverage",
      width: 110,
      render: (_: unknown, record: FactorDefinition) => getDataCoverage(record),
    },
    {
      title: t("factorColEvaluationResult"),
      key: "evaluation",
      width: 100,
      render: (_: unknown, record: FactorDefinition) => {
        const result = getEvaluationResult(record);
        return <Tag color={result.color}>{result.label}</Tag>;
      },
    },
    {
      title: t("factorColLifecycle"),
      dataIndex: "lifecycle_status",
      key: "lifecycle_status",
      width: 110,
      render: (status: string | null) => (
        <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>
      ),
    },
    {
      title: t("factorColOwner"),
      dataIndex: "owner",
      key: "owner",
      width: 100,
      render: (owner: string | null) => owner || "-",
    },
    {
      title: t("factorColUpdatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 120,
      render: (value: string | null) => formatTime(value),
    },
    {
      title: "关联",
      key: "usage",
      width: 110,
      render: (_: unknown, record: FactorDefinition) => {
        const u = peekUsage(record.code);
        const setCount = u?.in_factor_sets?.length ?? 0;
        const modelCount = u?.in_models?.length ?? 0;
        return (
          <Tooltip title={setCount + modelCount > 0 ? `已在 ${setCount} 个因子集 / ${modelCount} 个模型中使用，点击查看详情` : "查看该因子参与的因子集与模型"}>
            <Button
              type="link"
              size="small"
              icon={<LinkOutlined />}
              onClick={() => openUsageDrawer(record.code)}
              style={{ paddingInline: 4 }}
            >
              {setCount + modelCount > 0 ? `${setCount}集·${modelCount}模` : "查看"}
            </Button>
          </Tooltip>
        );
      },
    },
    {
      title: t("factorColActions"),
      key: "actions",
      width: 140,
      fixed: "right" as const,
      render: (_: unknown, record: FactorDefinition) => (
        <Space size="small">
          <Button type="link" size="small" onClick={() => onViewDetail(record.code)}>
            {t("factorActionView")}
          </Button>
          <Button type="link" size="small" onClick={() => onEditFactor(record.code)}>
            {t("factorActionEdit")}
          </Button>
        </Space>
      ),
    },
  ];

  /* ═══════════════════════════════════════════
   * P1.3.2 Drawer：因子使用详情
   * ═══════════════════════════════════════════ */
  const curUsage: ScoringFactorUsage | null = usageDrawerCode ? usageCache[usageDrawerCode] ?? null : null;
  const renderUsageDrawer = () => {
    if (!usageDrawerCode) return null;
    const u = curUsage;
    const factorName = u?.name || usageDrawerCode;
    return (
      <Drawer
        title={
          <Space>
            <InfoCircleOutlined style={{ color: "#0891b2" }} />
            <span>
              因子使用详情：<Text strong style={{ color: "#0f766e" }}>{factorLabel(usageDrawerCode, factorName)}</Text>
              {u?.code && <Text type="secondary" style={{ marginLeft: 6 }}>({u.code})</Text>}
            </span>
          </Space>
        }
        open={usageDrawerOpen}
        onClose={() => { setUsageDrawerOpen(false); setUsageDrawerCode(null); }}
        width={780}
        destroyOnClose
      >
        <Spin spinning={usageLoading}>
          {!u && !usageLoading ? (
            <Empty description={isZh ? "暂无使用数据" : "No usage data"} />
          ) : u ? (
            <>
              {/* ① 概览 Statistic 卡片 */}
              <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
                <Col span={6}>
                  <Card size="small">
                    <Statistic
                      title={isZh ? "参与因子集" : "In Factor Sets"}
                      value={u.in_factor_sets?.length ?? 0}
                      valueStyle={{ color: "#0891b2" }}
                      suffix={isZh ? "个" : "sets"}
                    />
                  </Card>
                </Col>
                <Col span={6}>
                  <Card size="small">
                    <Statistic
                      title={isZh ? "模型使用" : "In Models"}
                      value={u.in_models?.length ?? 0}
                      valueStyle={{ color: "#0f766e" }}
                      suffix={isZh ? "个" : "models"}
                    />
                  </Card>
                </Col>
                <Col span={6}>
                  <Card size="small">
                    <Statistic
                      title={isZh ? "30天 IC 均值" : "IC Mean (30d)"}
                      value={u.ic_mean_30d != null && Number.isFinite(u.ic_mean_30d) ? (u.ic_mean_30d * 100).toFixed(2) : "-"}
                      precision={2}
                      valueStyle={{ color: icColor(u.ic_mean_30d ?? 0) }}
                      suffix="%"
                    />
                  </Card>
                </Col>
                <Col span={6}>
                  <Card size="small">
                    <Statistic
                      title={isZh ? "30天覆盖率" : "Coverage (30d)"}
                      value={u.coverage_30d != null && Number.isFinite(u.coverage_30d) ? (u.coverage_30d * 100).toFixed(0) : 0}
                      valueStyle={{ color: coverageColor(coveragePct(u.coverage_30d)) }}
                      suffix="%"
                    />
                  </Card>
                </Col>
              </Row>

              {/* ② 因子描述信息 */}
              <Card size="small" title={isZh ? "因子基本信息" : "Factor Info"} style={{ marginBottom: 16 }}>
                <Descriptions column={2} size="small">
                  <Descriptions.Item label={isZh ? "状态" : "Status"}>
                    <Tag color={statusColor(u.lifecycle_status)}>{statusLabel(u.lifecycle_status)}</Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label={isZh ? "分类" : "Category"}>
                    {u.category ? factorCategoryLabel(u.category) : "-"}
                  </Descriptions.Item>
                  <Descriptions.Item label={isZh ? "来源" : "Origin"}>{u.origin || "-"}</Descriptions.Item>
                  <Descriptions.Item label={isZh ? "当前版本" : "Active Version"}>
                    {u.active_version != null ? `v${u.active_version}` : "-"}
                  </Descriptions.Item>
                  <Descriptions.Item label={isZh ? "生产天数" : "Days in Prod."} span={2}>
                    {u.days_in_production != null && Number.isFinite(u.days_in_production)
                      ? `${u.days_in_production} 天`
                      : "-"}
                  </Descriptions.Item>
                  {u.description && (
                    <Descriptions.Item label={isZh ? "描述" : "Description"} span={2}>
                      {u.description}
                    </Descriptions.Item>
                  )}
                </Descriptions>
              </Card>

              {/* ③ 参与的因子集 */}
              <Card
                size="small"
                title={
                  <Space>
                    <span>{isZh ? "参与的因子集" : "Factor Sets"}</span>
                    <Tag color="cyan">{u.in_factor_sets?.length ?? 0}</Tag>
                  </Space>
                }
                style={{ marginBottom: 16 }}
              >
                {(!u.in_factor_sets || u.in_factor_sets.length === 0) ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={isZh ? "该因子暂未加入任何因子集" : "Not in any factor set"} />
                ) : (
                  <Table<ScoringFactorUsageSet>
                    size="small"
                    pagination={false}
                    rowKey={(r) => `${r.factor_set_id}@${r.version}`}
                    dataSource={u.in_factor_sets}
                    columns={[
                      { title: isZh ? "因子集" : "Factor Set", dataIndex: "label", key: "label", width: 220,
                        render: (v, r) => (
                          <Space>
                            <Text strong>{v || r.factor_set_id}</Text>
                            <Tag style={{ fontSize: 11 }} color="blue">v{r.version}</Tag>
                          </Space>
                        )},
                      { title: isZh ? "ID" : "ID", dataIndex: "factor_set_id", key: "id", ellipsis: true,
                        render: (v) => <Text code style={{ fontSize: 11 }}>{v}</Text> },
                      { title: isZh ? "角色" : "Role", dataIndex: "role", key: "role", width: 100,
                        render: (v) => {
                          const roleMap: Record<string, string> = { feature: isZh ? "特征" : "Feature", target: isZh ? "目标" : "Target", regime: isZh ? "状态" : "Regime" };
                          return <Tag color="purple">{roleMap[v] || v}</Tag>;
                        }},
                      { title: isZh ? "状态" : "Status", dataIndex: "status", key: "status", width: 120,
                        render: (s) => {
                          const map: Record<string, string> = { active: "green", frozen: "gold", retired: "default", draft: "default" };
                          return <Tag color={map[s] || "default"}>{s}</Tag>;
                        }},
                    ]}
                  />
                )}
              </Card>

              <Divider />

              {/* ④ 在模型中的使用详情（权重/IC 条形图） */}
              <Card
                size="small"
                title={
                  <Space>
                    <span>{isZh ? "在模型中的权重与表现" : "Model Weights & Performance"}</span>
                    <Tag color="green">{u.in_models?.length ?? 0}</Tag>
                  </Space>
                }
              >
                {(!u.in_models || u.in_models.length === 0) ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={isZh ? "该因子暂未用于任何模型" : "Not used in any model"} />
                ) : (
                  <List
                    dataSource={u.in_models}
                    rowKey="model_id"
                    renderItem={(m: ScoringFactorUsageModel) => {
                      const wPct = (m.normalized_weight ?? 0) * 100;
                      const modelIC = m.model_validation_ic;
                      const factorIC = m.validation_ic;
                      return (
                        <Card
                          size="small"
                          style={{ marginBottom: 10, border: "1px solid #e2e8f0", borderRadius: 8 }}
                          styles={{ body: { padding: 12 } }}
                        >
                          <Row gutter={[12, 8]} align="middle">
                            <Col flex="280px">
                              <Text strong style={{ fontSize: 14 }}>{m.model_name || m.model_id}</Text>
                              <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>
                                ID: <Text code>{m.model_id}</Text>
                              </div>
                            </Col>
                            <Col flex="auto">
                              <div style={{ fontSize: 12, color: "#475569", marginBottom: 4 }}>
                                {isZh ? "模型中权重" : "Weight"}：
                                <Text strong style={{ color: "#0f766e", marginRight: 8 }}>
                                  {wPct.toFixed(1)}%
                                </Text>
                              </div>
                              <Progress
                                percent={Number(wPct.toFixed(1))}
                                size="small"
                                strokeColor={wPct >= 40 ? "#0f766e" : wPct >= 15 ? "#0891b2" : "#64748b"}
                                showInfo={false}
                              />
                            </Col>
                            <Col flex="160px" style={{ textAlign: "right" }}>
                              <Space direction="vertical" size={2}>
                                <Tag color={icColor(modelIC ?? undefined)} style={{ marginInlineEnd: 0, fontSize: 11 }}>
                                  模型IC {modelIC != null && Number.isFinite(modelIC) ? (modelIC * 100).toFixed(2) + "%" : "-"}
                                </Tag>
                                <Tag color={icColor(factorIC ?? undefined)} style={{ marginInlineEnd: 0, fontSize: 11 }}>
                                  本因子IC {factorIC != null && Number.isFinite(factorIC) ? (factorIC * 100).toFixed(2) + "%" : "-"}
                                </Tag>
                                <Tag style={{ marginInlineEnd: 0, fontSize: 11 }} color="default">
                                  {m.status || "-"}
                                </Tag>
                              </Space>
                            </Col>
                            {m.activated_at && (
                              <Col flex="130px" style={{ textAlign: "right", fontSize: 11, color: "#64748b" }}>
                                {isZh ? "启用时间" : "Activated"}
                                <br />
                                {formatTime(m.activated_at)}
                              </Col>
                            )}
                          </Row>
                        </Card>
                      );
                    }}
                  />
                )}
              </Card>
            </>
          ) : null}
        </Spin>
      </Drawer>
    );
  };

  // ── P2.2d 草稿 Tab: 加载 + 审批/驳回 + payload 抽屉 ───────────────────
  const loadDrafts = useCallback(async () => {
    setDraftsLoading(true);
    try {
      const rows = await api.scoringListFactorDrafts({
        status: draftStatus,
        sourceModule: draftSource,
        limit: 200,
      });
      setDrafts(rows ?? []);
    } catch (err: any) {
      message.error(
        `加载草稿失败: ${err?.message ?? err}`
      );
    } finally {
      setDraftsLoading(false);
    }
  }, [draftStatus, draftSource, message]);

  useEffect(() => {
    loadDrafts();
  }, [loadDrafts]);

  const pendingCount = drafts.filter((d) => d.review_status === "submitted").length;

  const onApproveDraft = async (row: ScoringFactorDraft) => {
    const reviewer = ctx?.currentUser ?? "superuser";
    try {
      setReviewBusy(row.draft_no);
      await api.scoringApproveFactorDraft(row.draft_no, { reviewer });
      message.success(`草稿 ${row.draft_no} 审批通过`);
      loadDrafts();
    } catch (err: any) {
      message.error(
        `审批失败: ${err?.detail ?? err?.message ?? err}`
      );
    } finally {
      setReviewBusy(null);
    }
  };

  const onRejectConfirm = async () => {
    if (!rejectRow) return;
    const reviewer = ctx?.currentUser ?? "superuser";
    try {
      setReviewBusy(rejectRow.draft_no);
      await api.scoringRejectFactorDraft(rejectRow.draft_no, {
        reviewer,
        reviewMsg: rejectMsg,
      });
      message.success(`草稿 ${rejectRow.draft_no} 已驳回`);
      setRejectOpen(false);
      setRejectRow(null);
      setRejectMsg("");
      loadDrafts();
    } catch (err: any) {
      message.error(
        `驳回失败: ${err?.detail ?? err?.message ?? err}`
      );
    } finally {
      setReviewBusy(null);
    }
  };

  const statusTag = (s: ScoringDraftStatus) => {
    switch (s) {
      case "submitted":
        return <Tag color="blue">待审批</Tag>;
      case "approved":
        return <Tag color="cyan">已批准</Tag>;
      case "applied":
        return <Tag color="green">已生产</Tag>;
      case "rejected":
        return <Tag color="red">已驳回</Tag>;
      case "deleted":
        return <Tag>已删除</Tag>;
      default:
        return <Tag>{s}</Tag>;
    }
  };

  const renderDraftPayloadDrawer = () => (
    <Drawer
      title={
        <Space>
          <FileTextOutlined />
          <span>草稿 Payload 详情</span>
          {draftPayloadRow ? (
            <Tag color="default">{draftPayloadRow.draft_no}</Tag>
          ) : null}
        </Space>
      }
      placement="right"
      width={640}
      open={draftPayloadOpen}
      onClose={() => setDraftPayloadOpen(false)}
    >
      {draftPayloadRow ? (
        <>
          <Descriptions column={1} bordered size="small">
            <Descriptions.Item label="草稿号">
              {draftPayloadRow.draft_no}
            </Descriptions.Item>
            <Descriptions.Item label="来源模块">
              <Tag>{draftPayloadRow.source_module}</Tag>
              {draftPayloadRow.source_ref_id ? (
                <Tag color="geekblue">ref: {draftPayloadRow.source_ref_id}</Tag>
              ) : null}
            </Descriptions.Item>
            <Descriptions.Item label="建议因子代码">
              <Text copyable>{draftPayloadRow.suggested_code}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="显示名">
              {draftPayloadRow.suggested_name ?? "-"}
            </Descriptions.Item>
            <Descriptions.Item label="分类">
              {draftPayloadRow.suggested_category ?? "-"}
            </Descriptions.Item>
            <Descriptions.Item label="审批状态">
              {statusTag(draftPayloadRow.review_status)}
            </Descriptions.Item>
            <Descriptions.Item label="提交人 / 审批人">
              <div>提交：{draftPayloadRow.submitted_by ?? "-"}</div>
              <div>审批：{draftPayloadRow.reviewer ?? "-"}</div>
            </Descriptions.Item>
            <Descriptions.Item label="时间线">
              <List size="small">
                {draftPayloadRow.submitted_at ? (
                  <List.Item>
                    <Text type="secondary">Submitted</Text>
                    &nbsp;&nbsp;
                    {draftPayloadRow.submitted_at}
                  </List.Item>
                ) : null}
                {draftPayloadRow.reviewed_at ? (
                  <List.Item>
                    <Text type="secondary">Reviewed</Text>
                    &nbsp;&nbsp;
                    {draftPayloadRow.reviewed_at}
                  </List.Item>
                ) : null}
                {draftPayloadRow.applied_at ? (
                  <List.Item>
                    <Text type="secondary">Applied</Text>
                    &nbsp;&nbsp;
                    {draftPayloadRow.applied_at}
                  </List.Item>
                ) : null}
              </List>
            </Descriptions.Item>
            {draftPayloadRow.review_msg ? (
              <Descriptions.Item label="审批说明">
                <Paragraph type={draftPayloadRow.review_status === "rejected" ? "danger" : undefined}>
                  {draftPayloadRow.review_msg}
                </Paragraph>
              </Descriptions.Item>
            ) : null}
            {draftPayloadRow.promoted_factor_id ? (
              <Descriptions.Item label="生产因子信息">
                <div>factor_id = {draftPayloadRow.promoted_factor_id}</div>
                <div>version = v{draftPayloadRow.promoted_factor_version}</div>
                {/* P2.2c.2: 正式因子 code 可复制 + 跳转因子库 */}
                {draftPayloadRow.promoted_factor_code ? (
                  <Space size={6} style={{ marginTop: 4 }}>
                    <Tag color="green" style={{ marginInlineEnd: 0 }}>
                      正式 code
                    </Tag>
                    <Text copyable strong>
                      {draftPayloadRow.promoted_factor_code}
                    </Text>
                    <Button
                      size="small"
                      type="link"
                      style={{ padding: 0 }}
                      onClick={() => {
                        window.dispatchEvent(
                          new CustomEvent("factor-center:navigate", {
                            detail: { code: draftPayloadRow.promoted_factor_code!, tab: "library" },
                          })
                        );
                      }}
                    >
                      在因子库查看 <LinkOutlined />
                    </Button>
                  </Space>
                ) : null}
                {draftPayloadRow.audit_url ? (
                  <div>
                    <a href={draftPayloadRow.audit_url}>
                      {draftPayloadRow.audit_url} <LinkOutlined />
                    </a>
                  </div>
                ) : null}
              </Descriptions.Item>
            ) : null}
          </Descriptions>
          <Divider orientation="left">Payload（原始 JSON）</Divider>
          <pre className="draft-payload-pre" style={{
            background: "#0f172a",
            color: "#e2e8f0",
            padding: 12,
            borderRadius: 6,
            fontSize: 12,
            overflow: "auto",
            maxHeight: 360,
          }}>
            {JSON.stringify(draftPayloadRow.payload ?? {}, null, 2)}
          </pre>
        </>
      ) : null}
    </Drawer>
  );

  const renderRejectModal = () => (
    <Modal
      title={`驳回草稿 ${rejectRow?.draft_no ?? ""}`}
      open={rejectOpen}
      onCancel={() => { setRejectOpen(false); setRejectRow(null); setRejectMsg(""); }}
      onOk={onRejectConfirm}
      okText="确认驳回"
      cancelText="取消"
      confirmLoading={rejectRow ? reviewBusy === rejectRow.draft_no : false}
      disabled={false}
    >
      <Space direction="vertical" style={{ width: "100%" }} size={12}>
        <div>
          <Text type="secondary">因子代码：</Text>
          <Text strong>{rejectRow?.suggested_code}</Text>
        </div>
        <div>
          <Text type="danger">驳回原因 *：</Text>
        </div>
        <Input.TextArea
          rows={4}
          value={rejectMsg}
          onChange={(e) => setRejectMsg(e.target.value)}
          placeholder="请明确说明驳回原因（如：因子覆盖率不足、公式语法错误、分类不清需重提交等）"
        />
      </Space>
    </Modal>
  );

  return (
    <div className="factor-library-page">
      {/* 页面标题和操作栏 */}
      <div className="factor-library-header">
        <div>
          <h2 className="factor-library-title">{t("factorLibraryPageTitle")}</h2>
          <p className="factor-library-subtitle">{t("factorLibraryPageSubtitle")}</p>
        </div>
        <Space size="middle">
          <Button type="primary" icon={<PlusOutlined />} onClick={onNewFactor} className="factor-new-btn">
            {t("factorLibraryNewFactor")}
          </Button>
        </Space>
      </div>

      <Tabs
        defaultActiveKey="library"
        size="large"
        className="factor-library-tabs"
        items={[
          {
            key: "library",
            label: "因子库",
            children: (
              <>
                {/* 搜索和筛选 */}
                <Card className="factor-library-filter-card" size="small">
                  <Row gutter={[12, 12]} align="middle">
                    <Col xs={24} sm={12} md={8} lg={6}>
                      <div className="filter-label">{t("factorLibrarySearchLabel")}</div>
                      <Input.Search
                        placeholder={t("factorLibrarySearchPlaceholder")}
                        value={search}
                        onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                        onSearch={() => { setPage(1); loadFactors(); }}
                        allowClear
                        enterButton
                      />
                    </Col>
                    <Col xs={12} sm={6} md={4} lg={4}>
                      <div className="filter-label">{t("factorLibraryFilterKind")}</div>
                      <Select
                        value={kindFilter}
                        onChange={(v) => { setKindFilter(v); setPage(1); }}
                        allowClear
                        style={{ width: "100%" }}
                        placeholder={t("factorLibraryAllKinds")}
                        options={[
                          { value: "continuous", label: t("factorKind_continuous") },
                          { value: "event", label: t("factorKind_event") },
                          { value: "regime", label: t("factorKind_regime") },
                        ]}
                      />
                    </Col>
                    <Col xs={12} sm={6} md={4} lg={4}>
                      <div className="filter-label">{t("factorLibraryFilterCategory")}</div>
                      <Select
                        value={categoryFilter}
                        onChange={(v) => { setCategoryFilter(v); setPage(1); }}
                        allowClear
                        style={{ width: "100%" }}
                        placeholder={t("factorLibraryAllCategories")}
                        options={categoryOptions}
                      />
                    </Col>
                    <Col xs={12} sm={6} md={4} lg={4}>
                      <div className="filter-label">{t("factorLibraryFilterStatus")}</div>
                      <Select
                        value={statusFilter}
                        onChange={(v) => { setStatusFilter(v); setPage(1); }}
                        allowClear
                        style={{ width: "100%" }}
                        placeholder={t("factorLibraryAllStatuses")}
                        options={[
                          { value: "draft", label: t("factorStatus_draft") },
                          { value: "candidate", label: t("factorStatus_candidate") },
                          { value: "testing", label: t("factorStatus_testing") },
                          { value: "shadow", label: t("factorStatus_shadow") },
                          { value: "active", label: t("factorStatus_active") },
                          { value: "deprecated", label: t("factorStatus_deprecated") },
                          { value: "rejected", label: t("factorStatus_rejected") },
                        ]}
                      />
                    </Col>
                    <Col xs={12} sm={6} md={4} lg={3}>
                      <div className="filter-label">&nbsp;</div>
                      <Button icon={<FilterOutlined />} style={{ width: "100%" }}>
                        {t("factorLibraryMoreFilters")}
                      </Button>
                    </Col>
                  </Row>
                </Card>

                {/* 统计卡片 */}
                <Row gutter={[12, 12]} className="factor-library-stats">
                  <Col xs={12} sm={12} md={6}>
                    <Card size="small" className="stat-card">
                      <div className="stat-label">{t("factorStatTotal")}</div>
                      <div className="stat-value">{stats.total}</div>
                      <div className="stat-sub">
                        {template("factorStatUpdatedThisWeek", { count: stats.updatedThisWeek })}
                      </div>
                    </Card>
                  </Col>
                  <Col xs={12} sm={12} md={6}>
                    <Card size="small" className="stat-card">
                      <div className="stat-label">{t("factorStatEvaluable")}</div>
                      <div className="stat-value">{stats.evaluable}</div>
                      <div className="stat-sub">{t("factorStatEvaluableDesc")}</div>
                    </Card>
                  </Col>
                  <Col xs={12} sm={12} md={6}>
                    <Card size="small" className="stat-card">
                      <div className="stat-label">{t("factorStatInShadow")}</div>
                      <div className="stat-value">{stats.shadow}</div>
                      <div className="stat-sub warning">
                        {template("factorStatShadowDriftWarning", { count: stats.shadowDriftWarning })}
                      </div>
                    </Card>
                  </Col>
                  <Col xs={12} sm={12} md={6}>
                    <Card size="small" className="stat-card">
                      <div className="stat-label">{t("factorStatActive")}</div>
                      <div className="stat-value">{stats.active}</div>
                      <div className="stat-sub">
                        {template("factorStatGatePassRate", { rate: stats.shadowGatePassRate })}
                      </div>
                    </Card>
                  </Col>
                </Row>

                {/* 因子列表表格 */}
                <Card className="factor-library-table-card" size="small">
                  <Spin spinning={loading}>
                    {items.length === 0 && !loading ? (
                      <Empty description={t("factorLibraryEmpty")} />
                    ) : (
                      <Table
                        columns={columns}
                        dataSource={items}
                        rowKey="id"
                        size="middle"
                        scroll={{ x: 1340 }}
                        pagination={{
                          current: page,
                          pageSize: pageSize,
                          total: total,
                          showSizeChanger: true,
                          showTotal: (n) => template("factorLibraryTotal", { count: n }),
                          onChange: (p, ps) => { setPage(p); setPageSize(ps); },
                        }}
                      />
                    )}
                  </Spin>
                </Card>
              </>
            ),
          },
          {
            key: "drafts",
            label: (
              <Badge count={pendingCount} offset={[6, 2]} size="small">
                <Space>
                  <FileTextOutlined />
                  <span>草稿审批</span>
                </Space>
              </Badge>
            ),
            children: (
              <>
                <Card className="factor-library-filter-card" size="small">
                  <Row gutter={[12, 12]} align="middle">
                    <Col xs={24} sm={12} md={6}>
                      <div className="filter-label">审批状态</div>
                      <Select
                        value={draftStatus}
                        onChange={(v) => setDraftStatus(v as any)}
                        style={{ width: "100%" }}
                        options={[
                          { value: "any", label: "全部" },
                          { value: "submitted", label: "待审批" },
                          { value: "approved", label: "已批准" },
                          { value: "applied", label: "已生产" },
                          { value: "rejected", label: "已驳回" },
                        ]}
                      />
                    </Col>
                    <Col xs={24} sm={12} md={6}>
                      <div className="filter-label">来源模块</div>
                      <Select
                        value={draftSource}
                        onChange={setDraftSource}
                        allowClear
                        style={{ width: "100%" }}
                        placeholder="全部模块"
                        options={[
                          { value: "custom_indicators", label: "自定义指标（promote）" },
                          { value: "ai_assisted", label: "AI 辅助草稿" },
                        ]}
                      />
                    </Col>
                    <Col xs={24} sm={12} md={6}>
                      <div className="filter-label">&nbsp;</div>
                      <Button onClick={loadDrafts} icon={<FilterOutlined />}>
                        刷新列表
                      </Button>
                    </Col>
                  </Row>
                </Card>
                <Card className="factor-library-table-card" size="small">
                  <Spin spinning={draftsLoading}>
                    {drafts.length === 0 && !draftsLoading ? (
                      <Empty description="暂无草稿。可从「自定义指标 → 提升为因子」提交草稿后在此审批。" />
                    ) : (
                      <Table<ScoringFactorDraft>
                        rowKey="draft_no"
                        size="middle"
                        scroll={{ x: 1300 }}
                        dataSource={drafts}
                        columns={[
                          {
                            title: "草稿号",
                            dataIndex: "draft_no",
                            width: 180,
                            render: (v: string) => <Text copyable>{v}</Text>,
                          },
                          {
                            title: "建议因子代码",
                            dataIndex: "suggested_code",
                            width: 220,
                            render: (v, r) => (
                              <Space direction="vertical" size={2}>
                                <Space>
                                  <Text strong>{v}</Text>
                                  {r.suggested_name ? (
                                    <Text type="secondary" className="fs-s-12">
                                      {r.suggested_name}
                                    </Text>
                                  ) : null}
                                </Space>
                                {/* P2.2c.2：已生产 → 显示正式 factor code，可复制 + 跳转因子库 */}
                                {r.review_status === "applied" && r.promoted_factor_code ? (
                                  <Space size={4}>
                                    <Tag color="green" style={{ marginInlineEnd: 0 }}>
                                      正式 code
                                    </Tag>
                                    <Text copyable style={{ fontSize: 12 }}>
                                      {r.promoted_factor_code}
                                    </Text>
                                    <a
                                      style={{ fontSize: 12 }}
                                      onClick={(e) => {
                                        e.preventDefault();
                                        window.dispatchEvent(
                                          new CustomEvent("factor-center:navigate", {
                                            detail: { code: r.promoted_factor_code, tab: "library" },
                                          })
                                        );
                                      }}
                                    >
                                      查看 <LinkOutlined />
                                    </a>
                                  </Space>
                                ) : null}
                              </Space>
                            ),
                          },
                          {
                            title: "来源",
                            width: 180,
                            render: (_, r) => (
                              <Space direction="vertical" size={2}>
                                <Tag>{r.source_module}</Tag>
                                {r.source_ref_id ? (
                                  <Text type="secondary" className="fs-s-12">
                                    ref id: {r.source_ref_id}
                                  </Text>
                                ) : null}
                              </Space>
                            ),
                          },
                          {
                            title: "状态",
                            dataIndex: "review_status",
                            width: 110,
                            render: (s: ScoringDraftStatus) => statusTag(s),
                          },
                          {
                            title: "提交人 / 审批人",
                            width: 160,
                            render: (_, r) => (
                              <Space direction="vertical" size={2}>
                                <div>
                                  <Text type="secondary" className="fs-s-12">提交：</Text>
                                  {r.submitted_by ?? "-"}
                                </div>
                                <div>
                                  <Text type="secondary" className="fs-s-12">审批：</Text>
                                  {r.reviewer ?? "-"}
                                </div>
                              </Space>
                            ),
                          },
                          {
                            title: "时间",
                            width: 160,
                            render: (_, r) => (
                              <Space direction="vertical" size={2}>
                                <div>
                                  <Text type="secondary" className="fs-s-12">提交：</Text>
                                  {r.submitted_at?.slice(0, 16) ?? "-"}
                                </div>
                                {r.applied_at ? (
                                  <div>
                                    <Text type="secondary" className="fs-s-12">生产：</Text>
                                    {r.applied_at.slice(0, 16)}
                                  </div>
                                ) : r.reviewed_at ? (
                                  <div>
                                    <Text type="secondary" className="fs-s-12">审阅：</Text>
                                    {r.reviewed_at.slice(0, 16)}
                                  </div>
                                ) : null}
                              </Space>
                            ),
                          },
                          {
                            title: "最近说明",
                            dataIndex: "review_msg",
                            ellipsis: true,
                            render: (v: string | null) =>
                              v ? (
                                <Tooltip title={v}>
                                  <Text type="secondary">{v}</Text>
                                </Tooltip>
                              ) : (
                                <Text type="secondary">-</Text>
                              ),
                          },
                          {
                            title: "操作",
                            width: 260,
                            fixed: "right",
                            render: (_, r) => {
                              const busy = reviewBusy === r.draft_no;
                              const canReview =
                                r.review_status === "submitted" ||
                                r.review_status === "approved";
                              const isFinal = r.review_status === "applied" || r.review_status === "deleted";
                              return (
                                <Space size={4}>
                                  <Button
                                    size="small"
                                    icon={<InfoCircleOutlined />}
                                    onClick={() => {
                                      setDraftPayloadRow(r);
                                      setDraftPayloadOpen(true);
                                    }}
                                  >
                                    详情
                                  </Button>
                                  <Button
                                    size="small"
                                    type="primary"
                                    icon={<CheckOutlined />}
                                    disabled={!canReview || isFinal}
                                    loading={busy}
                                    onClick={() => onApproveDraft(r)}
                                  >
                                    通过
                                  </Button>
                                  <Button
                                    size="small"
                                    danger
                                    icon={<CloseOutlined />}
                                    disabled={!canReview || isFinal}
                                    loading={busy}
                                    onClick={() => {
                                      setRejectRow(r);
                                      setRejectMsg("");
                                      setRejectOpen(true);
                                    }}
                                  >
                                    驳回
                                  </Button>
                                </Space>
                              );
                            },
                          },
                        ]}
                        pagination={{
                          pageSize: 20,
                          showSizeChanger: true,
                          showTotal: (n) => template("factorLibraryTotal", { count: n }),
                        }}
                      />
                    )}
                  </Spin>
                </Card>
              </>
            ),
          },
        ]}
      />

      {renderUsageDrawer()}
      {renderDraftPayloadDrawer()}
      {renderRejectModal()}
    </div>
  );
}
