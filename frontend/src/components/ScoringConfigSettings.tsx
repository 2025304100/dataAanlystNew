import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  Drawer,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import { QuestionCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { t } from "../i18n";
import { api } from "../api/client";

type AssetType = "stock" | "etf";

interface Factor {
  key: string;
  source?: string;
  weight: number;
  direction?: string;
}

interface Dimension {
  key: string;
  name: string;
  enabled: boolean;
  score_bucket: "quality" | "timing" | "news";
  weight: number;
  filter: { enabled: boolean; operator: string; value: number };
  factors: Factor[];
}

interface ScoringConfig {
  preset_key: string;
  name: string;
  asset_type: AssetType;
  preset_source: "system" | "user";
  final_weights: { quality: number; timing: number; news: number };
  dimensions: Dimension[];
}

interface PresetRow {
  id: number;
  asset_type: AssetType;
  preset_key: string;
  name: string;
  description: string | null;
  version: number;
  config_json: string;
  preset_source: string;
  is_system: number;
  is_active: number;
  is_latest: number;
  base_preset_key: string | null;
  config: ScoringConfig | null;
  created_at: string;
  updated_at: string;
}

interface VersionRow {
  id: number;
  asset_type: string;
  preset_key: string;
  name: string;
  version: number;
  config_json: string;
  is_active: number;
  is_latest: number;
  created_at: string;
}

const BUCKET_OPTIONS = [
  { value: "quality", label: () => t("scDimBucketQuality") },
  { value: "timing", label: () => t("scDimBucketTiming") },
  { value: "news", label: () => t("scDimBucketNews") },
];

const OP_OPTIONS = [
  { value: "gte", label: () => t("scOpGte") },
  { value: "lte", label: () => t("scOpLte") },
  { value: "gt", label: () => t("scOpGt") },
  { value: "lt", label: () => t("scOpLt") },
];

const DIRECTION_OPTIONS = [
  { value: "higher_better", label: () => t("scDirHigherBetter") },
  { value: "lower_better", label: () => t("scDirLowerBetter") },
  { value: "range_better", label: () => t("scDirRangeBetter") },
  { value: "lower_or_range_better", label: () => t("scDirLowerOrRangeBetter") },
];

function deepClone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v));
}

export default function ScoringConfigSettings() {
  const [assetType, setAssetType] = useState<AssetType>("stock");
  const [presets, setPresets] = useState<PresetRow[]>([]);
  const [activePreset, setActivePreset] = useState<PresetRow | null>(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<PresetRow | null>(null);
  const [draft, setDraft] = useState<ScoringConfig | null>(null);
  const [versionsDrawer, setVersionsDrawer] = useState<{ open: boolean; rows: VersionRow[]; title: string }>({
    open: false,
    rows: [],
    title: "",
  });
  const [dupModal, setDupModal] = useState<{ source: PresetRow; newKey: string; newName: string; newDesc: string } | null>(null);
  const [createModal, setCreateModal] = useState<{ newKey: string; newName: string; newDesc: string } | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [list, active] = await Promise.all([
        api.listScoringConfigs(assetType),
        api.getActiveScoringConfig(assetType),
      ]);
      setPresets(list as PresetRow[]);
      setActivePreset(active as PresetRow | null);
    } catch (e: any) {
      message.error(e.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assetType]);

  const enabledWeightSum = useMemo(() => {
    if (!draft) return 0;
    return draft.dimensions.filter((d) => d.enabled).reduce((s, d) => s + (Number(d.weight) || 0), 0);
  }, [draft]);

  const handleActivate = async (row: PresetRow) => {
    try {
      await api.activateScoringConfig(row.id);
      message.success(`${t("scActivePresetTip")} ${row.name}`);
      load();
    } catch (e: any) {
      message.error(e.message);
    }
  };

  const handleDuplicateOpen = (row: PresetRow) => {
    setDupModal({
      source: row,
      newKey: `${row.preset_key}_copy`,
      newName: `${row.name} ${t("presetCopySuffix")}`,
      newDesc: row.description || "",
    });
  };

  const handleDuplicateConfirm = async () => {
    if (!dupModal) return;
    if (!dupModal.newKey.trim()) return message.warning(t("scNeedKey"));
    if (!dupModal.newName.trim()) return message.warning(t("scNeedName"));
    try {
      await api.duplicateScoringConfig(dupModal.source.id, {
        new_preset_key: dupModal.newKey.trim(),
        new_name: dupModal.newName.trim(),
        new_description: dupModal.newDesc.trim() || null,
      });
      message.success(t("scBtnDuplicate"));
      setDupModal(null);
      load();
    } catch (e: any) {
      message.error(e.message);
    }
  };

  const handleDelete = async (row: PresetRow) => {
    if (row.is_system === 1) return message.warning(t("scSystemPresetNoDelete"));
    if (row.is_active === 1) return message.warning(t("scActivePresetNoDelete"));
    Modal.confirm({
      title: t("scBtnDelete"),
      content: row.name,
      okType: "danger",
      onOk: async () => {
        try {
          await api.deleteScoringConfig(row.id);
          message.success(t("scBtnDelete"));
          load();
        } catch (e: any) {
          message.error(e.message);
        }
      },
    });
  };

  const handleEdit = (row: PresetRow) => {
    if (row.is_system === 1) return message.warning(t("scSystemPresetNoEdit"));
    setEditing(row);
    setDraft(deepClone(row.config as ScoringConfig));
  };

  const handleCreateOpen = () => {
    setCreateModal({ newKey: "", newName: "", newDesc: "" });
  };

  const handleCreateConfirm = async () => {
    if (!createModal) return;
    if (!createModal.newKey.trim()) return message.warning(t("scNeedKey"));
    if (!createModal.newName.trim()) return message.warning(t("scNeedName"));
    // 以当前资产类型的均衡预设作为模板
    const template = presets.find((p) => p.preset_key === (assetType === "stock" ? "balanced_opportunity" : "etf_balanced"));
    const config = template?.config
      ? deepClone(template.config)
      : {
          preset_key: createModal.newKey.trim(),
          name: createModal.newName.trim(),
          asset_type: assetType,
          preset_source: "user",
          final_weights: { quality: 0.4, timing: 0.5, news: 0.1 },
          dimensions: [],
        };
    config.preset_key = createModal.newKey.trim();
    config.name = createModal.newName.trim();
    config.asset_type = assetType;
    config.preset_source = "user";
    try {
      await api.createScoringConfig({
        asset_type: assetType,
        preset_key: createModal.newKey.trim(),
        name: createModal.newName.trim(),
        description: createModal.newDesc.trim() || null,
        config,
        base_preset_key: template?.preset_key || null,
      });
      message.success(t("scBtnNew"));
      setCreateModal(null);
      load();
    } catch (e: any) {
      message.error(e.message);
    }
  };

  const handleSaveDraft = async () => {
    if (!editing || !draft) return;
    try {
      await api.updateScoringConfig(editing.id, {
        name: draft.name,
        description: editing.description,
        config: draft,
      });
      message.success(t("scBtnSave"));
      setEditing(null);
      setDraft(null);
      load();
    } catch (e: any) {
      message.error(e.message);
    }
  };

  const handleShowVersions = async (row: PresetRow) => {
    try {
      const rows = await api.listScoringConfigVersions(row.id);
      setVersionsDrawer({ open: true, rows: rows as VersionRow[], title: `${t("scVersionsTitle")} - ${row.name}` });
    } catch (e: any) {
      message.error(e.message);
    }
  };

  const updateDim = (idx: number, patch: Partial<Dimension>) => {
    if (!draft) return;
    const next = deepClone(draft);
    next.dimensions[idx] = { ...next.dimensions[idx], ...patch };
    setDraft(next);
  };

  const updateFactor = (dimIdx: number, fIdx: number, patch: Partial<Factor>) => {
    if (!draft) return;
    const next = deepClone(draft);
    next.dimensions[dimIdx].factors[fIdx] = { ...next.dimensions[dimIdx].factors[fIdx], ...patch };
    setDraft(next);
  };

  const columns = [
    {
      title: t("scPresetName"),
      dataIndex: "name",
      render: (v: string, row: PresetRow) => (
        <Space>
          <span>{v}</span>
          {row.is_system === 1 && <Tag color="blue">{t("scSystemPresetBadge")}</Tag>}
          {row.is_active === 1 && <Tag color="green">{t("scActive")}</Tag>}
        </Space>
      ),
    },
    { title: t("scPresetKey"), dataIndex: "preset_key" },
    { title: t("scSource"), dataIndex: "preset_source", render: (v: string) => (v === "system" ? t("scSourceSystem") : t("scSourceUser")) },
    { title: t("scVersion"), dataIndex: "version" },
    {
      title: t("scActions"),
      render: (_v: unknown, row: PresetRow) => (
        <Space size="small" wrap>
          <Button size="small" type={row.is_active === 1 ? "primary" : "default"} disabled={row.is_active === 1} onClick={() => handleActivate(row)}>
            {t("scBtnActivate")}
          </Button>
          <Button size="small" onClick={() => handleDuplicateOpen(row)}>
            {t("scBtnDuplicate")}
          </Button>
          <Button size="small" disabled={row.is_system === 1} onClick={() => handleEdit(row)}>
            {t("scBtnEdit")}
          </Button>
          <Button size="small" onClick={() => handleShowVersions(row)}>
            {t("scBtnVersions")}
          </Button>
          <Button size="small" danger disabled={row.is_system === 1 || row.is_active === 1} onClick={() => handleDelete(row)}>
            {t("scBtnDelete")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="scoring-config-section">
      <div style={{ marginBottom: 12, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <Space>
          <Button type={assetType === "stock" ? "primary" : "default"} onClick={() => setAssetType("stock")}>
            {t("scStockTab")}
          </Button>
          <Button type={assetType === "etf" ? "primary" : "default"} onClick={() => setAssetType("etf")}>
            {t("scEtfTab")}
          </Button>
        </Space>
        <Tooltip title={t("scDefaultDesc")}>
          <span style={{ color: "#888" }}>
            <QuestionCircleOutlined /> {t("scActivePresetTip")} {activePreset?.name || "-"} v{activePreset?.version || "-"}
          </span>
        </Tooltip>
        <Space style={{ marginLeft: "auto" }}>
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            {t("scRefresh")}
          </Button>
          <Button type="primary" onClick={handleCreateOpen}>
            {t("scBtnNew")}
          </Button>
        </Space>
      </div>

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={presets}
        columns={columns}
        pagination={false}
        expandedRowRender={(row: PresetRow) => (
          <div style={{ padding: "8px 16px", color: "#666" }}>
            {row.description || "-"}
            {row.config && (
              <div style={{ marginTop: 6 }}>
                <strong>{t("scDimensions")}:</strong> {row.config.dimensions.map((d) => `${d.name}(${d.weight})`).join(" / ")}
              </div>
            )}
          </div>
        )}
      />

      {/* 编辑/新建 用户预设 Drawer */}
      <Drawer
        open={!!editing}
        title={t("scEditTitle")}
        width={760}
        onClose={() => {
          setEditing(null);
          setDraft(null);
        }}
        footer={
          draft && (
            <div style={{ textAlign: "right" }}>
              <Tooltip title={t("scSaveAffectsTip")}>
                <span style={{ marginRight: 12, color: "#faad14" }}>
                  <QuestionCircleOutlined /> {t("scSaveAffectsTip")}
                </span>
              </Tooltip>
              <Button onClick={() => { setEditing(null); setDraft(null); }} style={{ marginRight: 8 }}>
                {t("scBtnCancel")}
              </Button>
              <Button type="primary" onClick={handleSaveDraft}>
                {t("scBtnSave")}
              </Button>
            </div>
          )
        }
      >
        {draft && (
          <div>
            <div style={{ marginBottom: 12 }}>
              <label style={{ marginRight: 8 }}>{t("scPresetName")}</label>
              <Input value={draft.name} style={{ width: 260 }} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            </div>
            <div style={{ marginBottom: 12 }}>
              <label style={{ marginRight: 8 }}>{t("scFinalWeights")}</label>
              <Space>
                <span>{t("scFinalQuality")}</span>
                <InputNumber min={0} max={1} step={0.05} value={draft.final_weights.quality} onChange={(v) => setDraft({ ...draft, final_weights: { ...draft.final_weights, quality: Number(v) || 0 } })} />
                <span>{t("scFinalTiming")}</span>
                <InputNumber min={0} max={1} step={0.05} value={draft.final_weights.timing} onChange={(v) => setDraft({ ...draft, final_weights: { ...draft.final_weights, timing: Number(v) || 0 } })} />
                <span>{t("scFinalNews")}</span>
                <InputNumber min={0} max={1} step={0.05} value={draft.final_weights.news} onChange={(v) => setDraft({ ...draft, final_weights: { ...draft.final_weights, news: Number(v) || 0 } })} />
              </Space>
            </div>

            <Table
              rowKey="key"
              size="small"
              pagination={false}
              dataSource={draft.dimensions}
              columns={[
                {
                  title: t("scDimEnabled"),
                  dataIndex: "enabled",
                  render: (v: boolean, _r: Dimension, idx: number) => (
                    <Checkbox checked={v} onChange={(e) => updateDim(idx, { enabled: e.target.checked })} />
                  ),
                },
                { title: t("scDimName"), dataIndex: "name" },
                {
                  title: t("scDimBucket"),
                  dataIndex: "score_bucket",
                  render: (v: string, _r: Dimension, idx: number) => (
                    <Select
                      size="small"
                      value={v}
                      style={{ width: 100 }}
                      options={BUCKET_OPTIONS.map((o) => ({ value: o.value, label: o.label() }))}
                      onChange={(val) => updateDim(idx, { score_bucket: val as Dimension["score_bucket"] })}
                    />
                  ),
                },
                {
                  title: t("scDimWeight"),
                  dataIndex: "weight",
                  render: (v: number, _r: Dimension, idx: number) => (
                    <InputNumber size="small" min={0} max={1} step={0.01} value={v} onChange={(val) => updateDim(idx, { weight: Number(val) || 0 })} />
                  ),
                },
                {
                  title: t("scDimFilter"),
                  render: (_v: unknown, r: Dimension, idx: number) => (
                    <Space size="small">
                      <Checkbox
                        checked={r.filter.enabled}
                        onChange={(e) => updateDim(idx, { filter: { ...r.filter, enabled: e.target.checked } })}
                      />
                      <Select
                        size="small"
                        value={r.filter.operator}
                        style={{ width: 70 }}
                        options={OP_OPTIONS.map((o) => ({ value: o.value, label: o.label() }))}
                        onChange={(val) => updateDim(idx, { filter: { ...r.filter, operator: val } })}
                      />
                      <InputNumber
                        size="small"
                        min={0}
                        max={100}
                        value={r.filter.value}
                        onChange={(val) => updateDim(idx, { filter: { ...r.filter, value: Number(val) || 0 } })}
                      />
                    </Space>
                  ),
                },
                {
                  title: t("scFactors"),
                  render: (_v: unknown, r: Dimension, idx: number) => (
                    <div>
                      {r.factors.map((f, fIdx) => (
                        <div key={f.key} style={{ marginBottom: 4 }}>
                          <Space size="small">
                            <span style={{ minWidth: 120, display: "inline-block" }}>{f.key}</span>
                            <span>{t("scFactorWeight")}:</span>
                            <InputNumber size="small" min={0} max={1} step={0.05} value={f.weight} onChange={(val) => updateFactor(idx, fIdx, { weight: Number(val) || 0 })} />
                            <Select
                              size="small"
                              value={f.direction || "higher_better"}
                              style={{ width: 130 }}
                              options={DIRECTION_OPTIONS.map((o) => ({ value: o.value, label: o.label() }))}
                              onChange={(val) => updateFactor(idx, fIdx, { direction: val })}
                            />
                          </Space>
                        </div>
                      ))}
                    </div>
                  ),
                },
              ]}
            />

            <div style={{ marginTop: 12, color: "#888" }}>
              <Tooltip title={t("scWeightSumTip")}>
                <span>
                  <QuestionCircleOutlined /> {t("scWeightSumTip")} ({(enabledWeightSum * 100).toFixed(0)}%)
                </span>
              </Tooltip>
            </div>
          </div>
        )}
      </Drawer>

      {/* 复制预设 Modal */}
      <Modal
        open={!!dupModal}
        title={t("scDuplicateTitle")}
        onCancel={() => setDupModal(null)}
        onOk={handleDuplicateConfirm}
        okText={t("scBtnDuplicate")}
        cancelText={t("scBtnCancel")}
      >
        {dupModal && (
          <div style={{ paddingTop: 12 }}>
            <div style={{ marginBottom: 8 }}>
              <label style={{ display: "block", marginBottom: 4 }}>{t("scNewPresetKey")}</label>
              <Input value={dupModal.newKey} onChange={(e) => setDupModal({ ...dupModal, newKey: e.target.value })} />
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ display: "block", marginBottom: 4 }}>{t("scNewPresetName")}</label>
              <Input value={dupModal.newName} onChange={(e) => setDupModal({ ...dupModal, newName: e.target.value })} />
            </div>
            <div>
              <label style={{ display: "block", marginBottom: 4 }}>{t("scNewPresetDesc")}</label>
              <Input.TextArea rows={2} value={dupModal.newDesc} onChange={(e) => setDupModal({ ...dupModal, newDesc: e.target.value })} />
            </div>
          </div>
        )}
      </Modal>

      {/* 新建用户预设 Modal */}
      <Modal
        open={!!createModal}
        title={t("scCreateTitle")}
        onCancel={() => setCreateModal(null)}
        onOk={handleCreateConfirm}
        okText={t("scBtnNew")}
        cancelText={t("scBtnCancel")}
      >
        {createModal && (
          <div style={{ paddingTop: 12 }}>
            <div style={{ marginBottom: 8 }}>
              <label style={{ display: "block", marginBottom: 4 }}>{t("scNewPresetKey")}</label>
              <Input value={createModal.newKey} onChange={(e) => setCreateModal({ ...createModal, newKey: e.target.value })} />
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ display: "block", marginBottom: 4 }}>{t("scNewPresetName")}</label>
              <Input value={createModal.newName} onChange={(e) => setCreateModal({ ...createModal, newName: e.target.value })} />
            </div>
            <div>
              <label style={{ display: "block", marginBottom: 4 }}>{t("scNewPresetDesc")}</label>
              <Input.TextArea rows={2} value={createModal.newDesc} onChange={(e) => setCreateModal({ ...createModal, newDesc: e.target.value })} />
            </div>
          </div>
        )}
      </Modal>

      {/* 历史版本 Drawer */}
      <Drawer
        open={versionsDrawer.open}
        title={versionsDrawer.title}
        width={600}
        onClose={() => setVersionsDrawer({ ...versionsDrawer, open: false })}
      >
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={versionsDrawer.rows}
          columns={[
            { title: t("scVersion"), dataIndex: "version" },
            { title: t("scPresetName"), dataIndex: "name" },
            {
              title: t("scActive"),
              dataIndex: "is_active",
              render: (v: number) => (v === 1 ? <Tag color="green">{t("scActive")}</Tag> : <Tag>{t("scInactive")}</Tag>),
            },
            { title: "created_at", dataIndex: "created_at", render: (v: string) => new Date(v).toLocaleString() },
          ]}
        />
      </Drawer>
    </div>
  );
}
