import { useState, useEffect, useCallback } from "react";
import { useApp } from "../../context/AppContext";
import { t, template, factorLabel, factorCategoryLabel, factorDirectionLabel } from "../../i18n";
import { api, type FactorDefinition } from "../../api/client";
import { Card, Table, Input, Select, Button, Space, Tag, Empty, Spin, App, Row, Col } from "antd";
import { PlusOutlined, FilterOutlined } from "@ant-design/icons";

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

  const isZh = ctx.locale.startsWith("zh");

  // 计算统计数据
  const loadStats = useCallback(async () => {
    setStatsLoading(true);
    try {
      const pageSize = 100;
      const resp = await api.listFactorDefinitions({
        page: 1,
        page_size: pageSize,
      });
      const allItems = [...(resp.items || [])];
      const expectedTotal = Math.max(resp.total || 0, allItems.length);
      for (let statsPage = 2; allItems.length < expectedTotal; statsPage += 1) {
        const next = await api.listFactorDefinitions({ page: statsPage, page_size: pageSize });
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
      const resp = await api.listFactorDefinitions({
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

  const columns = [
    {
      title: t("factorColFactor"),
      dataIndex: "code",
      key: "factor",
      width: 200,
      render: (_: string, record: FactorDefinition) => (
        <div>
          <div
            className="factor-name-cell"
            style={{ fontWeight: 600, fontSize: 16, color: "#0f766e", cursor: "pointer" }}
            onClick={() => onViewDetail(record.code)}
          >
            {factorLabel(record.code, record.name)}
          </div>
          <div style={{ color: "#64748b", fontSize: 12, marginTop: 2 }}>{record.code}</div>
        </div>
      ),
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
              scroll={{ x: 1100 }}
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
    </div>
  );
}
