import { useEffect, useState, useCallback } from "react";
import {
  Button, Card, Select, Switch, Space, Tooltip, Table, Tag, InputNumber, message, Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  QuestionCircleOutlined, ReloadOutlined, ThunderboltOutlined, CheckCircleTwoTone, CloseCircleTwoTone,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { t, template } from "../i18n";
import { api } from "../api/client";
import type { AkshareApiStatus, AkshareStrategyInfo, AkshareApiConfigUpdate } from "../api/client";

const { Text, Paragraph } = Typography;

function formatDateTime(iso: string | null): string {
  if (!iso) return "-";
  try {
    return dayjs(iso).format("YYYY-MM-DD HH:mm:ss");
  } catch {
    return iso;
  }
}

export default function AkshareApiManager() {
  const [apis, setApis] = useState<AkshareApiStatus[]>([]);
  const [strategies, setStrategies] = useState<AkshareStrategyInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [probingKeys, setProbingKeys] = useState<Set<string>>(new Set());
  const [probingAll, setProbingAll] = useState(false);
  // 本地编辑态：{ [apiKey]: { strategy, delay_min, delay_max, enabled } }
  const [edits, setEdits] = useState<Record<string, AkshareApiConfigUpdate>>({});

  const locale = (typeof window !== "undefined" && (localStorage.getItem("locale") === "en")) ? "en" : "zh-CN";
  const localeParam = locale === "en" ? "en-US" : "zh-CN";

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [apiList, stratList] = await Promise.all([
        api.listAkshareApis(localeParam),
        api.listAkshareStrategies(localeParam),
      ]);
      setApis(apiList);
      setStrategies(stratList);
      setEdits({});
    } catch (e: any) {
      message.error(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [localeParam]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // 纯逻辑：调用 API + 更新行数据，返回结果（无 UI 副作用，供单个/批量复用）
  const probeOne = async (apiKey: string): Promise<{ success: boolean; latency_ms: number | null; error: string | null }> => {
    const result = await api.probeAkshareApi(apiKey);
    setApis((prev) => prev.map((a) => (a.key === apiKey ? {
      ...a,
      last_probe_at: new Date().toISOString(),
      last_probe_success: result.success,
      last_probe_latency_ms: result.latency_ms,
      last_probe_error: result.error,
    } : a)));
    return result;
  };

  // 单个接口探测（带 UI 反馈）
  const handleProbe = async (apiKey: string) => {
    setProbingKeys((prev) => new Set(prev).add(apiKey));
    try {
      const result = await probeOne(apiKey);
      if (result.success) {
        message.success(template("apiProbeSuccess", { ms: result.latency_ms ?? 0 }));
      } else {
        message.error(template("apiProbeFailed", { error: result.error || "Unknown" }));
      }
    } catch (e: any) {
      message.error(template("apiProbeFailed", { error: e?.message || String(e) }));
    } finally {
      setProbingKeys((prev) => {
        const next = new Set(prev);
        next.delete(apiKey);
        return next;
      });
    }
  };

  // 批量探测（3 个一组并发，平衡速度与风控；批量期间禁用所有单个按钮）
  const handleProbeAll = async () => {
    setProbingAll(true);
    // 标记所有 key 为 probing，禁用单个按钮防重复点击
    setProbingKeys(new Set(apis.map(a => a.key)));
    let successCount = 0;
    let failCount = 0;
    try {
      const CONCURRENCY = 3;
      for (let i = 0; i < apis.length; i += CONCURRENCY) {
        const batch = apis.slice(i, i + CONCURRENCY);
        const results = await Promise.allSettled(batch.map(a => probeOne(a.key)));
        for (const r of results) {
          if (r.status === "fulfilled" && r.value.success) successCount++;
          else failCount++;
        }
      }
      if (failCount === 0) {
        message.success(`${t("apiMgmtProbeAll")} OK (${successCount})`);
      } else {
        message.warning(`${t("apiMgmtProbeAll")}: ${successCount} OK, ${failCount} failed`);
      }
    } catch (e: any) {
      message.error(e?.message || String(e));
    } finally {
      setProbingAll(false);
      setProbingKeys(new Set());
    }
  };

  // 更新单个接口配置
  const handleUpdate = async (apiKey: string, payload: AkshareApiConfigUpdate) => {
    try {
      const updated = await api.updateAkshareApiConfig(apiKey, payload, localeParam);
      setApis((prev) => prev.map((a) => (a.key === apiKey ? updated : a)));
      setEdits((prev) => {
        const next = { ...prev };
        delete next[apiKey];
        return next;
      });
      message.success(t("apiUpdateSuccess"));
    } catch (e: any) {
      message.error(template("apiUpdateFailed", { error: e?.message || String(e) }));
    }
  };

  // 本地编辑态读写
  const getEdit = (apiKey: string, field: keyof AkshareApiConfigUpdate, currentVal: any) => {
    const ed = edits[apiKey];
    if (ed && field in ed) return (ed as any)[field];
    return currentVal;
  };
  const setEdit = (apiKey: string, field: keyof AkshareApiConfigUpdate, val: any, row: AkshareApiStatus) => {
    setEdits((prev) => ({
      ...prev,
      [apiKey]: {
        ...(prev[apiKey] || {}),
        [field]: val,
      } as AkshareApiConfigUpdate,
    }));
    // enabled 字段立即提交（Switch 体验）
    if (field === "enabled") {
      handleUpdate(apiKey, { enabled: val });
    }
    // strategy 切换到非 custom 时立即提交
    if (field === "anti_risk_strategy" && val !== "custom") {
      handleUpdate(apiKey, { anti_risk_strategy: val });
    }
    if (field === "anti_risk_strategy" && val === "custom") {
      // 切到 custom，保留当前 edit 状态等待用户调整延时后手动保存
      // 若已有延时值则自动提交一次以应用 custom 档位
      const minV = getEdit(apiKey, "delay_min_ms", row.delay_min_ms);
      const maxV = getEdit(apiKey, "delay_max_ms", row.delay_max_ms);
      handleUpdate(apiKey, { anti_risk_strategy: "custom", delay_min_ms: minV, delay_max_ms: maxV });
    }
  };

  const columns: ColumnsType<AkshareApiStatus> = [
    {
      title: t("apiColKey"),
      key: "key",
      width: 220,
      fixed: "left",
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text strong>{r.name}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{r.key}</Text>
        </Space>
      ),
    },
    {
      title: t("apiColCategory"),
      dataIndex: "category",
      key: "category",
      width: 110,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: t("apiColStatus"),
      key: "status",
      width: 110,
      render: (_, r) => {
        if (!r.enabled) return <Tag color="default">{t("apiStatusDisabled")}</Tag>;
        if (r.last_probe_success === null) return <Tag color="default">{t("apiStatusNever")}</Tag>;
        return r.last_probe_success
          ? <Tag color="success" icon={<CheckCircleTwoTone twoToneColor="#52c41a" />}>{t("apiStatusSuccess")}</Tag>
          : <Tag color="error" icon={<CloseCircleTwoTone twoToneColor="#ff4d4f" />}>{t("apiStatusFailed")}</Tag>;
      },
    },
    {
      title: (
        <Space size={4}>
          {t("apiColStrategy")}
          <Tooltip title={t("apiTipTooltip")}>
            <QuestionCircleOutlined style={{ color: "#999" }} />
          </Tooltip>
        </Space>
      ),
      key: "strategy",
      width: 150,
      render: (_, r) => (
        <Select
          size="small"
          style={{ width: 120 }}
          value={getEdit(r.key, "anti_risk_strategy", r.anti_risk_strategy)}
          onChange={(v) => setEdit(r.key, "anti_risk_strategy", v, r)}
          options={strategies.map((s) => ({ value: s.key, label: s.name }))}
        />
      ),
    },
    {
      title: t("apiColDelay"),
      key: "delay",
      width: 200,
      render: (_, r) => {
        const isCustom = getEdit(r.key, "anti_risk_strategy", r.anti_risk_strategy) === "custom";
        const minV = getEdit(r.key, "delay_min_ms", r.delay_min_ms);
        const maxV = getEdit(r.key, "delay_max_ms", r.delay_max_ms);
        if (!isCustom) {
          // 显示档位对应的固定延时区间
          const strat = strategies.find((s) => s.key === r.anti_risk_strategy);
          const dmin = strat?.delay_min_ms ?? minV;
          const dmax = strat?.delay_max_ms ?? maxV;
          return <Text type="secondary">{dmin}-{dmax} {t("apiDelayUnit")}</Text>;
        }
        return (
          <Space size={4}>
            <InputNumber
              size="small"
              style={{ width: 80 }}
              min={0}
              max={60000}
              value={minV}
              onChange={(v) => setEdit(r.key, "delay_min_ms", v ?? 0, r)}
            />
            <Text type="secondary">-</Text>
            <InputNumber
              size="small"
              style={{ width: 80 }}
              min={0}
              max={60000}
              value={maxV}
              onChange={(v) => setEdit(r.key, "delay_max_ms", v ?? 0, r)}
            />
            <Button
              size="small"
              type="primary"
              onClick={() => handleUpdate(r.key, { anti_risk_strategy: "custom", delay_min_ms: minV, delay_max_ms: maxV })}
            >
              {t("apiMgmtRefresh")}
            </Button>
          </Space>
        );
      },
    },
    {
      title: t("apiColLastProbe"),
      key: "lastProbe",
      width: 180,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text style={{ fontSize: 12 }}>{formatDateTime(r.last_probe_at)}</Text>
          {r.last_probe_latency_ms !== null && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {template("apiLatencyMs", { ms: r.last_probe_latency_ms })}
            </Text>
          )}
          {r.last_probe_error && (
            <Tooltip title={r.last_probe_error}>
              <Text type="danger" style={{ fontSize: 12, maxWidth: 160, display: "inline-block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {r.last_probe_error}
              </Text>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: t("apiColLastCall"),
      key: "lastCall",
      width: 160,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text style={{ fontSize: 12 }}>{formatDateTime(r.last_call_at)}</Text>
          {r.last_call_at && (
            <Text type={r.last_call_success ? "success" : "danger"} style={{ fontSize: 12 }}>
              {r.last_call_success ? t("apiStatusSuccess") : t("apiStatusFailed")}
            </Text>
          )}
          {!r.last_call_at && <Text type="secondary" style={{ fontSize: 12 }}>{t("apiNeverCalled")}</Text>}
        </Space>
      ),
    },
    {
      title: t("apiColStats"),
      key: "stats",
      width: 140,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text style={{ fontSize: 12 }}>{template("apiCallsTotal", { total: r.total_calls })}</Text>
          {r.total_failures > 0 && (
            <Text type="danger" style={{ fontSize: 12 }}>{template("apiCallsFailures", { failed: r.total_failures })}</Text>
          )}
        </Space>
      ),
    },
    {
      title: t("apiColActions"),
      key: "actions",
      width: 200,
      fixed: "right",
      render: (_, r) => (
        <Space size={8}>
          <Switch
            size="small"
            checked={getEdit(r.key, "enabled", r.enabled)}
            onChange={(v) => setEdit(r.key, "enabled", v, r)}
          />
          <Text style={{ fontSize: 12 }}>{t("apiEnabledToggle")}</Text>
          <Button
            size="small"
            icon={<ThunderboltOutlined />}
            loading={probingKeys.has(r.key) || probingAll}
            disabled={probingAll || probingKeys.has(r.key)}
            onClick={() => handleProbe(r.key)}
          >
            {t("apiMgmtProbe")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Card
      title={
        <Space>
          <span>{t("apiMgmtSectionTitle")}</span>
          <Tooltip title={t("apiMgmtSectionDesc")}>
            <QuestionCircleOutlined style={{ color: "#999" }} />
          </Tooltip>
        </Space>
      }
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={loadAll}>
            {t("apiMgmtRefresh")}
          </Button>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={probingAll}
            onClick={handleProbeAll}
          >
            {t("apiMgmtProbeAll")}
          </Button>
        </Space>
      }
    >
      <Paragraph type="secondary" style={{ marginBottom: 12 }}>
        {t("apiMgmtSectionDesc")}
      </Paragraph>
      <Table<AkshareApiStatus>
        rowKey="key"
        columns={columns}
        dataSource={apis}
        loading={loading}
        size="small"
        scroll={{ x: 1400 }}
        pagination={false}
      />
    </Card>
  );
}
