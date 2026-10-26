import { useEffect, useMemo, useState } from "react";
import { Alert, Button, DatePicker, Divider, Input, InputNumber, Modal, Select, Space, Switch, Tabs, Tag } from "antd";
import { LinkOutlined, PlusOutlined, SaveOutlined, ThunderboltOutlined } from "@ant-design/icons";
import dayjs from "dayjs";

import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import { t } from "../i18n";
import type { MarketEvent, SectorConceptConfigItem, Symbol } from "../types";

interface ThemeItem {
  id: number;
  code: string;
  name: string;
  description?: string | null;
}

interface ThemeManagementModalProps {
  open: boolean;
  onClose: () => void;
}

/** P1-6/P2-12：主题管理弹窗 —— i18n 化 + 新增"板块关键词"页签（DB 覆盖内置，调词不发版） */
export default function ThemeManagementModal({ open, onClose }: ThemeManagementModalProps) {
  const { showToast } = useApp();
  const [themes, setThemes] = useState<ThemeItem[]>([]);
  const [events, setEvents] = useState<MarketEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [themeCode, setThemeCode] = useState("");
  const [themeName, setThemeName] = useState("");
  const [themeDescription, setThemeDescription] = useState("");
  const [selectedThemeId, setSelectedThemeId] = useState<number | undefined>();
  const [symbolKeyword, setSymbolKeyword] = useState("");
  const [mappingConfidence, setMappingConfidence] = useState(80);
  const [confirmed, setConfirmed] = useState(true);
  const [eventId, setEventId] = useState<number | undefined>();
  const [catalystTitle, setCatalystTitle] = useState("");
  const [catalystScore, setCatalystScore] = useState(70);
  const [expiresOn, setExpiresOn] = useState("");

  // P2-12：板块关键词配置
  const [sectorConfigs, setSectorConfigs] = useState<SectorConceptConfigItem[]>([]);
  const [sectorDrafts, setSectorDrafts] = useState<Record<string, string>>({});
  const [sectorSavingId, setSectorSavingId] = useState<string | null>(null);

  const selectedTheme = useMemo(
    () => themes.find((theme) => theme.id === selectedThemeId),
    [selectedThemeId, themes],
  );

  const load = async () => {
    setLoading(true);
    try {
      const [themeRows, eventResult] = await Promise.all([
        api.getInvestmentThemes(),
        api.getMarketEvents({ limit: 50, sort_by: "published_at" }),
      ]);
      const items = themeRows as ThemeItem[];
      setThemes(items);
      setEvents((eventResult as { events?: MarketEvent[] }).events ?? []);
      setSelectedThemeId((current) => current ?? items[0]?.id);
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : t("theme_modal_load_failed"));
    } finally {
      setLoading(false);
    }
  };

  const loadSectorConfigs = async () => {
    try {
      const rows = await api.getSectorConcepts();
      setSectorConfigs(rows);
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : t("theme_modal_sectors_load_failed"));
    }
  };

  useEffect(() => {
    if (open) {
      void load();
      void loadSectorConfigs();
    }
  }, [open]);

  const createTheme = async () => {
    if (!themeCode.trim() || !themeName.trim()) {
      showToast("error", t("theme_modal_need_code_name"));
      return;
    }
    setSaving(true);
    try {
      const created = await api.createInvestmentTheme({
        code: themeCode.trim(),
        name: themeName.trim(),
        description: themeDescription.trim() || null,
        source_type: "manual",
      }) as ThemeItem;
      setThemes((current) => [...current, created].sort((a, b) => a.name.localeCompare(b.name, "zh-CN")));
      setSelectedThemeId(created.id);
      setThemeCode("");
      setThemeName("");
      setThemeDescription("");
      showToast("success", t("theme_modal_created").replace("{name}", created.name));
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : t("theme_modal_create_failed"));
    } finally {
      setSaving(false);
    }
  };

  const mapSymbol = async () => {
    if (!selectedThemeId || !symbolKeyword.trim()) {
      showToast("error", t("theme_modal_need_theme_symbol"));
      return;
    }
    setSaving(true);
    try {
      const symbols = await api.getSymbols(symbolKeyword.trim(), { pageSize: 20 }) as Symbol[];
      const normalized = symbolKeyword.trim().toLowerCase();
      const symbol = symbols.find((item) => item.symbol.toLowerCase() === normalized)
        ?? symbols.find((item) => item.name.toLowerCase() === normalized)
        ?? symbols[0];
      if (!symbol) {
        throw new Error(t("theme_modal_symbol_not_found"));
      }
      await api.mapInvestmentThemeSymbol(selectedThemeId, {
        symbol_id: symbol.id,
        source_type: "manual",
        source_ref: t("theme_modal_source_ref"),
        confidence: mappingConfidence / 100,
        is_confirmed: confirmed,
      });
      setSymbolKeyword("");
      showToast("success", t("theme_modal_mapped")
        .replace("{name}", symbol.name).replace("{symbol}", symbol.symbol).replace("{theme}", selectedTheme?.name ?? t("theme_modal_theme_fallback")));
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : t("theme_modal_map_failed"));
    } finally {
      setSaving(false);
    }
  };

  const addCatalyst = async () => {
    if (!selectedThemeId) {
      showToast("error", t("theme_modal_need_theme"));
      return;
    }
    const event = events.find((item) => item.id === eventId);
    const title = catalystTitle.trim() || event?.title;
    if (!title) {
      showToast("error", t("theme_modal_need_catalyst_title"));
      return;
    }
    setSaving(true);
    try {
      await api.addInvestmentThemeCatalyst(selectedThemeId, {
        market_event_id: eventId ?? null,
        title,
        catalyst_score: catalystScore,
        confidence: 1,
        source_type: event ? event.source : "manual",
        source_ref: event?.source_url ?? t("theme_modal_manual_source"),
        published_at: event?.published_at ?? new Date().toISOString(),
        expires_at: expiresOn ? `${expiresOn}T23:59:59` : null,
      });
      setEventId(undefined);
      setCatalystTitle("");
      setExpiresOn("");
      showToast("success", t("theme_modal_catalyst_added").replace("{theme}", selectedTheme?.name ?? t("theme_modal_theme_fallback")));
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : t("theme_modal_catalyst_failed"));
    } finally {
      setSaving(false);
    }
  };

  // P2-12：保存某板块关键词（空输入=恢复内置）
  const saveSectorKeywords = async (sectorId: string) => {
    const draft = (sectorDrafts[sectorId] ?? "").trim();
    const keywords = draft ? draft.split(/[,，]/).map((w) => w.trim()).filter(Boolean) : [];
    setSectorSavingId(sectorId);
    try {
      await api.updateSectorConcepts(sectorId, keywords);
      await loadSectorConfigs();
      setSectorDrafts((current) => {
        const next = { ...current };
        delete next[sectorId];
        return next;
      });
      showToast("success", keywords.length ? t("theme_modal_sectors_saved") : t("theme_modal_sectors_reset"));
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : t("theme_modal_sectors_save_failed"));
    } finally {
      setSectorSavingId(null);
    }
  };

  const sectorLabel = (sectorId: string) => {
    const key = `market_news_sector_${sectorId}`;
    const label = t(key);
    return label === key ? sectorId : label;
  };

  const themeTab = (
    <>
      <Alert
        type="info"
        showIcon
        message={t("theme_modal_hint_title")}
        description={t("theme_modal_hint_desc")}
        style={{ marginBottom: 16 }}
      />

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <section>
          <strong>{t("theme_modal_step_create")}</strong>
          <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 10 }}>
            <Input value={themeName} onChange={(event) => setThemeName(event.target.value)} placeholder={t("theme_modal_name_ph")} />
            <Input value={themeCode} onChange={(event) => setThemeCode(event.target.value)} placeholder={t("theme_modal_code_ph")} />
            <Input.TextArea value={themeDescription} onChange={(event) => setThemeDescription(event.target.value)} placeholder={t("theme_modal_desc_ph")} rows={2} />
            <Button type="primary" icon={<PlusOutlined />} loading={saving} onClick={createTheme}>{t("theme_modal_create_btn")}</Button>
          </Space>
        </section>

        <section>
          <strong>{t("theme_modal_existing")}</strong>
          <div style={{ marginTop: 10, minHeight: 120, border: "1px solid #f0f0f0", borderRadius: 6, padding: 10 }}>
            {loading ? t("theme_modal_loading") : themes.length === 0 ? t("theme_modal_empty") : themes.map((theme) => (
              <Tag key={theme.id} color={selectedThemeId === theme.id ? "blue" : "default"} style={{ marginBottom: 6, cursor: "pointer" }} onClick={() => setSelectedThemeId(theme.id)}>
                {theme.name}
              </Tag>
            ))}
          </div>
        </section>
      </div>

      <Divider />
      <strong>{t("theme_modal_step_map").replace("{theme}", selectedTheme?.name ?? t("theme_modal_need_theme"))}</strong>
      <Space wrap style={{ marginTop: 10 }}>
        <Input value={symbolKeyword} onChange={(event) => setSymbolKeyword(event.target.value)} placeholder={t("theme_modal_symbol_ph")} style={{ width: 260 }} />
        <InputNumber value={mappingConfidence} onChange={(value) => setMappingConfidence(Number(value ?? 80))} min={60} max={100} addonAfter={t("theme_modal_confidence_suffix")} />
        <Switch checked={confirmed} onChange={setConfirmed} checkedChildren={t("theme_modal_confirmed")} unCheckedChildren={t("theme_modal_unconfirmed")} />
        <Button icon={<LinkOutlined />} loading={saving} onClick={mapSymbol}>{t("theme_modal_map_btn")}</Button>
      </Space>

      <Divider />
      <strong>{t("theme_modal_step_catalyst")}</strong>
      <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 10 }}>
        <Select
          value={eventId}
          onChange={(value) => setEventId(value)}
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder={t("theme_modal_event_ph")}
          options={events.map((event) => ({ value: event.id, label: event.title }))}
        />
        <Space wrap>
          <Input value={catalystTitle} onChange={(event) => setCatalystTitle(event.target.value)} placeholder={t("theme_modal_catalyst_title_ph")} style={{ width: 280 }} />
          <InputNumber value={catalystScore} onChange={(value) => setCatalystScore(Number(value ?? 70))} min={60} max={100} addonAfter={t("theme_modal_catalyst_score_suffix")} />
          <DatePicker
            value={expiresOn ? dayjs(expiresOn) : null}
            onChange={(d) => setExpiresOn(d ? d.format("YYYY-MM-DD") : "")}
            format="YYYY-MM-DD"
            placeholder={t("theme_modal_expire_ph")}
            style={{ width: 150 }}
          />
          <Button type="primary" icon={<ThunderboltOutlined />} loading={saving} onClick={addCatalyst}>{t("theme_modal_catalyst_btn")}</Button>
        </Space>
      </Space>
    </>
  );

  const sectorsTab = (
    <>
      <Alert
        type="info"
        showIcon
        message={t("theme_modal_sectors_hint_title")}
        description={t("theme_modal_sectors_hint_desc")}
        style={{ marginBottom: 12 }}
      />
      <div style={{ maxHeight: 420, overflowY: "auto" }}>
        {sectorConfigs.map((config) => {
          const draft = sectorDrafts[config.sector_id];
          const original = config.keywords.join(",");
          const dirty = draft !== undefined && draft.trim() !== original;
          return (
            <div key={config.sector_id} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
              <Tag color={config.source === "db" ? "geekblue" : "default"} style={{ minWidth: 110, textAlign: "center" }} title={config.source === "db" ? t("theme_modal_sectors_db_source") : t("theme_modal_sectors_builtin_source")}>
                {sectorLabel(config.sector_id)}
              </Tag>
              <Input
                value={draft ?? original}
                onChange={(e) => setSectorDrafts((current) => ({ ...current, [config.sector_id]: e.target.value }))}
                placeholder={t("theme_modal_sectors_input_ph")}
                style={{ flex: 1 }}
                size="small"
              />
              <Button size="small" icon={<SaveOutlined />} disabled={!dirty} loading={sectorSavingId === config.sector_id}
                onClick={() => void saveSectorKeywords(config.sector_id)}>
                {t("theme_modal_sectors_save")}
              </Button>
            </div>
          );
        })}
      </div>
    </>
  );

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={780} title={t("theme_modal_title")} destroyOnHidden>
      <Tabs
        items={[
          { key: "themes", label: t("theme_modal_tab_themes"), children: themeTab },
          { key: "sectors", label: t("theme_modal_tab_sectors"), children: sectorsTab },
        ]}
      />
    </Modal>
  );
}
