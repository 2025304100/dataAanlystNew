import { useEffect, useState, useCallback } from "react";
import {
  Button, Card, Select, Switch, Space, Tooltip, Table, Tag, InputNumber, Input, message, Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  QuestionCircleOutlined, ReloadOutlined, ThunderboltOutlined, CheckCircleTwoTone, CloseCircleTwoTone,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { t, template, getLocale } from "../i18n";
import { api } from "../api/client";
import type { AkshareApiStatus, AkshareStrategyInfo, AkshareApiConfigUpdate, PolicyPublication } from "../api/client";
import GovernancePolicyEditor from "./GovernancePolicyEditor";

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
  const [savingKeys, setSavingKeys] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [probingKeys, setProbingKeys] = useState<Set<string>>(new Set());
  const [probingAll, setProbingAll] = useState(false);
  // 探测进行中的可读进度：{ apiKey: "已等待 12s" }（PT-DEF-18 心跳轮询的行内反馈）
  const [probeHints, setProbeHints] = useState<Record<string, string>>({});
  // 本地编辑态：{ [apiKey]: { strategy, delay_min, delay_max, enabled } }
  const [edits, setEdits] = useState<Record<string, AkshareApiConfigUpdate>>({});

  const localeParam = getLocale();
  const zh = localeParam === "zh-CN";
  const labels: Record<string, string> = zh ? {
    available_now: "本期候选", permission_unverified: "计划接入", planned_public: "计划接入", prohibited: "不接入",
    implemented: "已有实现", not_implemented: "尚未实现", unknown: "未知", validated: "已验证实现",
    not_required: "无需账号", unconfigured: "凭据未配置", pending: "认证待核验", verified: "认证已核验", expired: "认证已过期", revoked: "认证已撤销",
    observed_success: "最近调用成功", observed_failure: "最近传输失败", cooldown: "冷却中", half_open: "恢复探测中", unobserved: "暂无执行观测",
  } : {};
  const categories: Record<string, string> = zh ? {
    announcement_enrichment: "公告", capital_flow: "资金流", daily_bars: "日线行情", "daily_bars/security_status": "行情与证券状态",
    etf: "ETF", financial: "财报", hot_rank: "人气榜", index: "指数", instrument_metadata: "标的基础信息", lhb: "龙虎榜",
    macro: "宏观", prohibited_minute_tail: "分钟/尾盘（不接入）", proposed_product_see_package: "计划接入", trade_calendar: "交易日历",
    trade_calendar_legacy: "旧交易日历", valuation: "估值",
  } : { proposed_product_see_package: "Planned", prohibited_minute_tail: "Excluded intraday" };
  const description = apis.some(a => a.governance)
    ? (zh ? "管理来源范围、实现、认证、执行策略和统计；生产准入独立核验，配置发布不授予生产许可。" : "Manage scope, implementation, authentication, execution policies and observations. Policy publication does not grant production admission.")
    : t("apiMgmtSectionDesc");
  const visibleApis = apis.filter(a => [a.key, a.name, a.module, a.governance?.access_status, labels[a.governance?.access_status ?? ""]]
    .join(" ").toLowerCase().includes(search.toLowerCase()));

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

  // 纯逻辑：提交探测任务 → 轮询心跳直到终态（无 UI 副作用，供单个/批量复用）。
  // PT-DEF-18：以前这里靠一个 30s 的 HTTP 超时去赌上游快慢，慢而成功的接口会被
  // 误判成失败；现在只判断"任务到终态了没 / 心跳还在不在走"，不再猜总时长。
  const probeOne = async (apiKey: string): Promise<{ success: boolean; latency_ms: number | null; error: string | null }> => {
    const accepted = await api.submitApiProbe(apiKey);
    const beganAt = Date.now();
    let intervalMs = 800;
    for (;;) {
      const st = await api.getApiProbeTask(accepted.task_id);
      const waitedSec = Math.floor((Date.now() - beganAt) / 1000);
      if (st.status === "done" || st.status === "failed" || st.status === "cancelled") {
        const result = st.result ?? {
          key: apiKey,
          success: false,
          latency_ms: null,
          error: st.message || `probe ${st.status}`,
        };
        setApis((prev) => prev.map((a) => (a.key === apiKey ? {
          ...a,
          last_probe_at: new Date().toISOString(),
          last_probe_success: result.success,
          last_probe_latency_ms: result.latency_ms,
          last_probe_error: result.error,
        } : a)));
        return result;
      }
      if (st.heartbeat_stale) {
        // 心跳断了就明确报错交用户重试，绝不无声地一直转圈
        setProbeHints((prev) => {
          const next = { ...prev };
          delete next[apiKey];
          return next;
        });
        throw new Error(t("apiProbeStale"));
      }
      setProbeHints((prev) => ({ ...prev, [apiKey]: template("apiProbeWaiting", { sec: waitedSec }) }));
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
      intervalMs = Math.min(intervalMs + 400, 3000);
    }
  };

  // 单个接口探测（带 UI 反馈）
  const handleProbe = async (apiKey: string) => {
    setProbingKeys((prev) => new Set(prev).add(apiKey));
    try {
      const result = await probeOne(apiKey);
      if (apis.find(a => a.key === apiKey)?.governance) await loadAll();
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
      setProbeHints((prev) => {
        const next = { ...prev };
        delete next[apiKey];
        return next;
      });
    }
  };

  // 批量探测（3 个一组并发，平衡速度与风控；批量期间禁用所有单个按钮）
  const handleProbeAll = async () => {
    const eligible = visibleApis.filter(a => !a.governance || a.governance.can_probe);
    setProbingAll(true);
    // 标记所有 key 为 probing，禁用单个按钮防重复点击
    setProbingKeys(new Set(eligible.map(a => a.key)));
    let successCount = 0;
    let failCount = 0;
    try {
      const CONCURRENCY = 3;
      for (let i = 0; i < eligible.length; i += CONCURRENCY) {
        const batch = eligible.slice(i, i + CONCURRENCY);
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
      setProbeHints({});
      if (apis.some(a => a.governance)) await loadAll();
    }
  };

  const publishPolicy = async (row: AkshareApiStatus, payload: PolicyPublication) => {
    setSavingKeys(p => new Set(p).add(row.key));
    try {
      await api.publishApiPolicy(row.key, payload, localeParam);
      message.success(zh ? "执行策略已发布为新版本" : "Execution policy published");
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e);
      message.error(text.includes("CONFIG_VERSION_CONFLICT") ? (zh ? "配置已变更，已刷新当前版本，请重新检查后提交。" : "Configuration changed. Review the refreshed version before publishing.") : text);
    } finally {
      await loadAll();
      setSavingKeys(p => { const n = new Set(p); n.delete(row.key); return n; });
    }
  };

  // 更新单个接口配置
  const handleUpdate = async (apiKey: string, payload: AkshareApiConfigUpdate) => {
    const row = apis.find(a => a.key === apiKey);
    if (row?.governance?.control_mode === "shared" && row.governance.config_version) {
      await publishPolicy(row, { scope: "endpoint", expected_version: row.governance.config_version, policy: { enabled: payload.enabled } });
      return;
    }
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
          <Text strong>{r.name.replace(r.category, categories[r.category] || r.category)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{r.key}</Text>
        </Space>
      ),
    },
    {
      title: t("apiColCategory"),
      dataIndex: "category",
      key: "category",
      width: 110,
      render: (v: string) => <Tooltip title={categories[v] || v}><Tag style={{ maxWidth: "100%", whiteSpace: "normal", overflowWrap: "anywhere" }}>{categories[v] || v}</Tag></Tooltip>,
    },
    {
      title: zh ? "范围 / 实现 / 认证" : "Scope / implementation / authentication",
      key: "governance", width: 230,
      render: (_, r) => r.governance ? <Space size={2} wrap>
        {[r.governance.access_status, r.governance.implementation_status, r.governance.auth_status].map((value, i) => <Tag key={i}>{labels[value] || value}</Tag>)}
      </Space> : <Text type="secondary">{zh ? "旧版接口信息" : "Legacy metadata"}</Text>,
    },
    {
      title: t("apiColStatus"),
      key: "status",
      width: 110,
      render: (_, r) => {
        if (r.governance) return <Space direction="vertical" size={2}>
          <Tag>{r.governance.production_request_allowed ? (zh ? "准入条件满足" : "Admission conditions met") : (zh ? "未准入" : "Not admitted")}</Tag>
          <Text type="secondary">{labels[r.governance.health_status] || r.governance.health_status}</Text>
          {!r.enabled && <Tag>{t("apiStatusDisabled")}</Tag>}
        </Space>;
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
        r.governance?.network_metrics_applicable === false ? <Text>{zh ? "本地读取控制" : "Local read control"}</Text> :
        r.governance?.control_mode === "shared" ? <Text>{r.governance.config_version ? (zh ? `接口 v${r.governance.config_version.endpoint} / 来源 v${r.governance.config_version.provider}` : `Endpoint v${r.governance.config_version.endpoint} / provider v${r.governance.config_version.provider}`) : (zh ? "执行路径未登记" : "Execution path unregistered")}</Text> :
        <Select
          disabled={r.governance?.can_edit === false}
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
        if (r.governance?.network_metrics_applicable === false) return <Text>{zh ? "不适用" : "Not applicable"}</Text>;
        if (r.governance?.control_mode === "shared") return <Text>{r.governance.control ? `${r.delay_min_ms} ms` : "—"}</Text>;
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
              disabled={r.governance?.can_edit === false}
              max={60000}
              value={minV}
              onChange={(v) => setEdit(r.key, "delay_min_ms", v ?? 0, r)}
            />
            <Text type="secondary">-</Text>
            <InputNumber
              size="small"
              style={{ width: 80 }}
              min={0}
              disabled={r.governance?.can_edit === false}
              max={60000}
              value={maxV}
              onChange={(v) => setEdit(r.key, "delay_max_ms", v ?? 0, r)}
            />
            <Button
              size="small"
              type="primary"
              disabled={r.governance?.can_edit === false}
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
          {r.governance?.control_mode === "shared" && <>
            <Text>{zh ? "共享执行统计" : "Shared execution observations"}</Text>
            <Text>{zh ? "尝试" : "Attempts"}: {r.governance.statistics?.by_kind.call.attempts ?? 0} · {zh ? "重试" : "Retries"}: {r.governance.statistics?.by_kind.call.retry_count ?? 0}</Text>
            <Text>{zh ? "探测" : "Probes"}: {r.governance.statistics?.by_kind.probe.logical_calls ?? 0}</Text>
            <Text>{zh ? "策略拒绝" : "Policy denials"}: {r.governance.statistics?.by_kind.call.policy_denied ?? 0} · {zh ? "契约拒绝" : "Contract errors"}: {r.governance.statistics?.by_kind.call.contract_error ?? 0}</Text>
          </>}
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
            disabled={r.governance?.can_edit === false || savingKeys.has(r.key)}
            checked={getEdit(r.key, "enabled", r.governance?.control?.endpoint_policy.enabled ?? r.enabled)}
            onChange={(v) => setEdit(r.key, "enabled", v, r)}
          />
          <Text style={{ fontSize: 12 }}>{t("apiEnabledToggle")}</Text>
          <Button
            size="small"
            icon={<ThunderboltOutlined />}
            loading={probingKeys.has(r.key) || probingAll}
            disabled={probingAll || probingKeys.has(r.key) || r.governance?.can_probe === false}
            onClick={() => handleProbe(r.key)}
          >
            {probeHints[r.key] || t("apiMgmtProbe")}
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
          <Tooltip title={description}>
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
        {description}
      </Paragraph>
      {apis.some(a => a.governance) && <Paragraph type="secondary">
        {zh ? "目录范围、实现、认证、生产准入与执行观测分别展示。连通成功不代表字段、单位或质量已通过；共享控制仅覆盖已接线调用。" : "Scope, implementation, authentication, admission and observations are independent. Connectivity does not certify units or quality; shared control covers managed calls."}
      </Paragraph>}
      <Input.Search aria-label={zh ? "搜索接口" : "Search endpoints"} placeholder={zh ? "搜索接口、来源或范围" : "Search endpoint, provider or scope"}
        allowClear value={search} onChange={e => setSearch(e.target.value)} style={{ maxWidth: 400, marginBottom: 12 }} />
      <Table<AkshareApiStatus>
        rowKey="key"
        columns={columns}
        dataSource={visibleApis}
        loading={loading}
        size="small"
        scroll={{ x: 1700 }}
        pagination={{ pageSize: 12, showSizeChanger: false, hideOnSinglePage: true }}
        expandable={{
          expandedRowRender: (row) => (
            <Space direction="vertical" style={{ width: "min(100%, calc(100vw - 120px))" }}>
              {row.governance && <>
                <Text>{zh ? "控制模式：" : "Control mode: "}{row.governance.control_mode === "shared" ? (zh ? "共享控制" : "Shared") : (zh ? "旧控制（当前进程）" : "Legacy (current process)")}</Text>
                <Text>{row.governance.network_metrics_applicable ? (zh ? "供应商余额：未知" : "Supplier balance: unknown") : (zh ? "网络认证、供应商配额和网络延迟：不适用（本地读取）" : "Network authentication, supplier quotas and network latency: not applicable (local reads)")}</Text>
                {row.governance.control?.provider_policy.enabled === false && <Text type="warning">{zh ? "来源共享开关已关闭；接口开关单独保存。" : "Provider disabled; the endpoint switch is stored separately."}</Text>}
                <Text>{zh ? "完整性要求：" : "Profiles: "}{row.governance.profile_ids.join(", ") || (zh ? "尚未登记" : "Not declared")}</Text>
                <Text>{zh ? "旧累计（单独保留，不与共享统计相加）：" : "Legacy totals (kept separate): "}{row.governance.legacy_statistics?.available ? row.governance.legacy_statistics.total_calls : (zh ? "未知" : "Unknown")}</Text>
                {row.governance.statistics && <Text>{zh ? "共享观测：调用 / 尝试 / 重试 / 探测" : "Shared observations: calls / attempts / retries / probes"}: {row.governance.statistics.by_kind.call.logical_calls ?? 0} / {row.governance.statistics.by_kind.call.attempts ?? 0} / {row.governance.statistics.by_kind.call.retry_count ?? 0} / {row.governance.statistics.by_kind.probe.logical_calls ?? 0}</Text>}
                {!!row.governance.unit_fields.length && <div>{row.governance.unit_fields.map((f, i) => <Tag key={i}>{f.wire_field} → {f.canonical_target}: {f.native_unit_claim || "unknown"} ({zh ? "源单位声明，待核验" : "declared, unverified"})</Tag>)}</div>}
                {row.governance.control && row.governance.config_version && row.governance.can_edit && <div style={{ display: "flex", gap: 12, flexWrap: "wrap", width: "100%" }}>
                  {(["endpoint", "provider"] as const).map(scope => <GovernancePolicyEditor key={scope} row={row} scope={scope} zh={zh} onPublish={p => publishPolicy(row, p)} />)}
                </div>}
              </>}
              {!!row.data_contracts?.length && <Text type="secondary">{zh ? "字段与单位契约尚未核验；连通性测试不代表数据口径通过。" : "Field and unit contracts are unverified; connectivity does not verify data semantics."}</Text>}
              {!row.data_contracts?.length ? (
                <Text type="secondary">{localeParam === "zh-CN" ? "字段与单位契约尚未核验；连通性测试不代表数据口径通过。" : "Field and unit contracts are unverified; connectivity does not verify data semantics."}</Text>
              ) : row.data_contracts.map((contract) => (
                <div key={`${contract.endpoint}:${contract.instrument_family}`}>
                  <Text strong>{contract.provider} · {contract.dataset} · {contract.asset_type} · {contract.instrument_family}</Text>
                  <div>{contract.fields.map((field) => (
                    <Tag key={field.field}>{field.field}: {field.input_unit} × {field.multiplier} → {field.output_unit}</Tag>
                  ))}</div>
                  {!!contract.unavailable_fields.length && <Text type="secondary">
                    {localeParam === "zh-CN" ? "不提供：" : "Unavailable: "}{contract.unavailable_fields.join(", ")}
                  </Text>}
                  <div><Text type="secondary">{contract.version}</Text></div>
                </div>
              ))}
            </Space>
          ),
        }}
      />
    </Card>
  );
}
