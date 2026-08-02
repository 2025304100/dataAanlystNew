import { useState, useEffect, useCallback } from "react";
import { useApp } from "../../context/AppContext";
import { t, template } from "../../i18n";
import { api, type FactorDefinition } from "../../api/client";
import { Card, Table, Input, Select, Button, Space, Tag, Empty, Spin, App } from "antd";

type FactorLibraryProps = {
  onOpenDetail: (code: string) => void;
  onOpenEditor: () => void;
};

export default function FactorLibrary({ onOpenDetail, onOpenEditor }: FactorLibraryProps) {
  const ctx = useApp();
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<FactorDefinition[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [originFilter, setOriginFilter] = useState<string | undefined>(undefined);
  const [kindFilter, setKindFilter] = useState<string | undefined>(undefined);

  const isZh = ctx.locale.startsWith("zh");

  const loadFactors = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await api.listFactorDefinitions({
        search: search || undefined,
        lifecycle_status: statusFilter,
        origin: originFilter,
        factor_kind: kindFilter,
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
  }, [search, statusFilter, originFilter, kindFilter, page, pageSize]);

  useEffect(() => {
    loadFactors();
  }, [loadFactors]);

  // 生命周期状态颜色映射
  const statusColor = (status: string | null): string => {
    if (!status) return "default";
    const map: Record<string, string> = {
      draft: "default",
      candidate: "blue",
      testing: "orange",
      shadow: "purple",
      active: "green",
      quarantined: "red",
      deprecated: "gray",
      rejected: "red",
    };
    return map[status] || "default";
  };

  // 状态 i18n key
  const statusLabel = (status: string | null): string => {
    if (!status) return "-";
    return t(`factorStatus_${status}`);
  };

  // 方向 i18n
  const directionLabel = (dir: string): string => {
    return t(`factorDirection_${dir}`) || dir;
  };

  const columns = [
    {
      title: t("factorColCode"),
      dataIndex: "code",
      key: "code",
      render: (code: string) => (
        <a onClick={() => onOpenDetail(code)}>{code}</a>
      ),
    },
    { title: t("factorColName"), dataIndex: "name", key: "name", ellipsis: true },
    { title: t("factorColCategory"), dataIndex: "category", key: "category" },
    {
      title: t("factorColDirection"),
      dataIndex: "direction",
      key: "direction",
      render: (dir: string) => directionLabel(dir),
    },
    {
      title: t("factorColStatus"),
      dataIndex: "lifecycle_status",
      key: "lifecycle_status",
      render: (status: string | null) => (
        <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>
      ),
    },
    {
      title: t("factorColOrigin"),
      dataIndex: "origin",
      key: "origin",
      render: (origin: string | null) => origin ? t(`factorOrigin_${origin}`) : "-",
    },
    {
      title: t("factorColKind"),
      dataIndex: "factor_kind",
      key: "factor_kind",
      render: (kind: string | null) => kind ? t(`factorKind_${kind}`) : "-",
    },
    {
      title: t("factorColActions"),
      key: "actions",
      render: (_: unknown, record: FactorDefinition) => (
        <Button size="small" onClick={() => onOpenDetail(record.code)}>
          {t("factorActionView")}
        </Button>
      ),
    },
  ];

  return (
    <Card>
      <Space style={{ marginBottom: 16, width: "100%" }} wrap>
        <Input.Search
          placeholder={t("factorLibrarySearchPlaceholder")}
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          onSearch={loadFactors}
          style={{ width: 250 }}
          allowClear
        />
        <Select
          placeholder={t("factorLibraryFilterStatus")}
          value={statusFilter}
          onChange={(v) => { setStatusFilter(v); setPage(1); }}
          allowClear
          style={{ width: 150 }}
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
        <Select
          placeholder={t("factorLibraryFilterOrigin")}
          value={originFilter}
          onChange={(v) => { setOriginFilter(v); setPage(1); }}
          allowClear
          style={{ width: 150 }}
          options={[
            { value: "system", label: t("factorOrigin_system") },
            { value: "user", label: t("factorOrigin_user") },
            { value: "ai_assisted", label: t("factorOrigin_ai_assisted") },
            { value: "imported", label: t("factorOrigin_imported") },
          ]}
        />
        <Select
          placeholder={t("factorLibraryFilterKind")}
          value={kindFilter}
          onChange={(v) => { setKindFilter(v); setPage(1); }}
          allowClear
          style={{ width: 150 }}
          options={[
            { value: "continuous", label: t("factorKind_continuous") },
            { value: "event", label: t("factorKind_event") },
            { value: "regime", label: t("factorKind_regime") },
          ]}
        />
        <Button type="primary" onClick={onOpenEditor}>
          {t("factorLibraryNewFactor")}
        </Button>
      </Space>

      <Spin spinning={loading}>
        {items.length === 0 && !loading ? (
          <Empty description={t("factorLibraryEmpty")} />
        ) : (
          <Table
            columns={columns}
            dataSource={items}
            rowKey="id"
            size="small"
            pagination={{
              current: page,
              pageSize: pageSize,
              total: total,
              showSizeChanger: true,
              showTotal: (n) => template("factorLibraryTotal", { count: n }),
              onChange: (p, ps) => { setPage(p); setPageSize(ps); },
            }}
            onRow={(record) => ({
              onClick: () => onOpenDetail(record.code),
              style: { cursor: "pointer" },
            })}
          />
        )}
      </Spin>
    </Card>
  );
}
