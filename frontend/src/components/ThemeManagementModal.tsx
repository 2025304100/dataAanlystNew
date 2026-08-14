import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Divider, Input, InputNumber, Modal, Select, Space, Switch, Tag } from "antd";
import { LinkOutlined, PlusOutlined, ThunderboltOutlined } from "@ant-design/icons";

import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import type { MarketEvent, Symbol } from "../types";

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
      showToast("error", error instanceof Error ? error.message : "主题数据加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) void load();
  }, [open]);

  const createTheme = async () => {
    if (!themeCode.trim() || !themeName.trim()) {
      showToast("error", "请填写主题代码和主题名称");
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
      showToast("success", `已创建主题：${created.name}`);
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : "创建主题失败");
    } finally {
      setSaving(false);
    }
  };

  const mapSymbol = async () => {
    if (!selectedThemeId || !symbolKeyword.trim()) {
      showToast("error", "请选择主题并输入标的代码或名称");
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
        throw new Error("未找到该标的，请先在标的库中添加或同步它");
      }
      await api.mapInvestmentThemeSymbol(selectedThemeId, {
        symbol_id: symbol.id,
        source_type: "manual",
        source_ref: "主题管理人工确认",
        confidence: mappingConfidence / 100,
        is_confirmed: confirmed,
      });
      setSymbolKeyword("");
      showToast("success", `已将 ${symbol.name}（${symbol.symbol}）关联到 ${selectedTheme?.name ?? "主题"}`);
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : "关联标的失败");
    } finally {
      setSaving(false);
    }
  };

  const addCatalyst = async () => {
    if (!selectedThemeId) {
      showToast("error", "请先选择主题");
      return;
    }
    const event = events.find((item) => item.id === eventId);
    const title = catalystTitle.trim() || event?.title;
    if (!title) {
      showToast("error", "请选择市场事件或填写催化标题");
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
        source_ref: event?.source_url ?? "主题管理人工录入",
        published_at: event?.published_at ?? new Date().toISOString(),
        expires_at: expiresOn ? `${expiresOn}T23:59:59` : null,
      });
      setEventId(undefined);
      setCatalystTitle("");
      setExpiresOn("");
      showToast("success", `已为 ${selectedTheme?.name ?? "主题"} 添加催化证据`);
    } catch (error) {
      showToast("error", error instanceof Error ? error.message : "添加催化失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={780} title="主题管理" destroyOnClose>
      <Alert
        type="info"
        showIcon
        message="主题机会池只使用已确认映射与有效催化"
        description="创建主题后，需要至少关联一个标的，并添加一条评分不低于 60 的未过期催化，标的才会进入主题机会池。"
        style={{ marginBottom: 16 }}
      />

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <section>
          <strong>1. 创建投资主题</strong>
          <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 10 }}>
            <Input value={themeName} onChange={(event) => setThemeName(event.target.value)} placeholder="主题名称，例如：创新药" />
            <Input value={themeCode} onChange={(event) => setThemeCode(event.target.value)} placeholder="主题代码，例如：innovative-drug" />
            <Input.TextArea value={themeDescription} onChange={(event) => setThemeDescription(event.target.value)} placeholder="投资逻辑或覆盖范围（可选）" rows={2} />
            <Button type="primary" icon={<PlusOutlined />} loading={saving} onClick={createTheme}>创建主题</Button>
          </Space>
        </section>

        <section>
          <strong>现有主题</strong>
          <div style={{ marginTop: 10, minHeight: 120, border: "1px solid #f0f0f0", borderRadius: 6, padding: 10 }}>
            {loading ? "加载中…" : themes.length === 0 ? "暂无主题，请先创建。" : themes.map((theme) => (
              <Tag key={theme.id} color={selectedThemeId === theme.id ? "blue" : "default"} style={{ marginBottom: 6, cursor: "pointer" }} onClick={() => setSelectedThemeId(theme.id)}>
                {theme.name}
              </Tag>
            ))}
          </div>
        </section>
      </div>

      <Divider />
      <strong>2. 关联标的到“{selectedTheme?.name ?? "请先选择主题"}”</strong>
      <Space wrap style={{ marginTop: 10 }}>
        <Input value={symbolKeyword} onChange={(event) => setSymbolKeyword(event.target.value)} placeholder="输入代码或名称，例如 300760" style={{ width: 260 }} />
        <InputNumber value={mappingConfidence} onChange={(value) => setMappingConfidence(Number(value ?? 80))} min={60} max={100} addonAfter="% 置信度" />
        <Switch checked={confirmed} onChange={setConfirmed} checkedChildren="已确认" unCheckedChildren="待确认" />
        <Button icon={<LinkOutlined />} loading={saving} onClick={mapSymbol}>关联标的</Button>
      </Space>

      <Divider />
      <strong>3. 添加主题催化</strong>
      <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 10 }}>
        <Select
          value={eventId}
          onChange={(value) => setEventId(value)}
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="关联一条已有市场事件（推荐）"
          options={events.map((event) => ({ value: event.id, label: event.title }))}
        />
        <Space wrap>
          <Input value={catalystTitle} onChange={(event) => setCatalystTitle(event.target.value)} placeholder="或手工填写催化标题" style={{ width: 280 }} />
          <InputNumber value={catalystScore} onChange={(value) => setCatalystScore(Number(value ?? 70))} min={60} max={100} addonAfter="催化评分" />
          <Input type="date" value={expiresOn} onChange={(event) => setExpiresOn(event.target.value)} style={{ width: 150 }} />
          <Button type="primary" icon={<ThunderboltOutlined />} loading={saving} onClick={addCatalyst}>添加催化</Button>
        </Space>
      </Space>
    </Modal>
  );
}
