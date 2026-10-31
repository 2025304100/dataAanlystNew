import { useEffect, useState } from "react";
import { Button, Card, InputNumber, Select, Space, Switch, Typography, message } from "antd";
import { api } from "../api/client";
import type { AkshareApiStatus, ExecutionPolicy, PolicyHistory, PolicyPublication } from "../api/client";

const { Text } = Typography;
const fields: Array<[Exclude<keyof ExecutionPolicy, "enabled">, string, string]> = [
  ["min_interval_s", "最小间隔（秒）", "Minimum interval (s)"], ["max_attempts", "最大尝试次数", "Maximum attempts"],
  ["timeout_s", "调用时限（秒）", "Call budget (s)"], ["failure_threshold", "熔断失败阈值", "Failure threshold"],
  ["cooldown_s", "初始冷却（秒）", "Initial cooldown (s)"], ["max_cooldown_s", "最大冷却（秒）", "Maximum cooldown (s)"],
  ["probe_lease_s", "恢复探测租期（秒）", "Recovery probe lease (s)"], ["window_s", "本地配额窗口（秒）", "Local quota window (s)"],
  ["total_limit", "窗口总限额", "Window total limit"], ["incremental_limit", "增量预留及上限", "Incremental reserve / limit"],
  ["backfill_limit", "回补上限", "Backfill limit"], ["probe_limit", "探测预留及上限", "Probe reserve / limit"],
];

export default function GovernancePolicyEditor({ row, scope, zh, onPublish }: {
  row: AkshareApiStatus; scope: "endpoint" | "provider"; zh: boolean;
  onPublish: (payload: PolicyPublication) => Promise<void>;
}) {
  const g = row.governance!;
  const current = g.control![scope === "endpoint" ? "endpoint_policy" : "provider_policy"];
  const [patch, setPatch] = useState<Partial<ExecutionPolicy>>({});
  const [history, setHistory] = useState<PolicyHistory[]>([]);
  const [restore, setRestore] = useState<number>();
  const [busy, setBusy] = useState(false);
  useEffect(() => { setPatch({}); setHistory([]); setRestore(undefined); }, [g.config_version?.endpoint, g.config_version?.provider]);
  const title = scope === "endpoint" ? (zh ? "本接口策略" : "Endpoint policy") : (zh ? "来源共享策略" : "Provider policy");
  const publish = async (restoring = false) => {
    setBusy(true);
    try {
      await onPublish({ scope, expected_version: g.config_version!, ...(restoring ? { restore_version: restore } : { policy: patch }) });
      setPatch({});
    } finally { setBusy(false); }
  };
  return <Card size="small" title={`${title} · v${g.config_version![scope]}`} style={{ flex: "1 1 380px" }}>
    <section aria-label={title}>
      {scope === "provider" && <Text type="warning">{zh ? "影响该来源的所有已接线接口。" : "Affects every managed endpoint of this provider."}</Text>}
      <Space style={{ display: "flex", margin: "10px 0" }}>
        <Text>{zh ? "执行开关" : "Execution enabled"}</Text>
        <Switch aria-label={`${title} ${zh ? "执行开关" : "enabled"}`} checked={patch.enabled ?? current.enabled}
          disabled={busy} onChange={enabled => setPatch(p => ({ ...p, enabled }))} />
      </Space>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(150px, 1fr))", gap: 10 }}>
        {fields.map(([key, cn, en]) => <label key={key}>
          <div><Text>{zh ? cn : en}</Text></div>
          <InputNumber aria-label={`${title} ${zh ? cn : en}`} style={{ width: "100%" }} disabled={busy}
            value={key in patch ? patch[key] : current[key]} min={key.endsWith("limit") || key === "min_interval_s" ? 0 : .001}
            precision={key.endsWith("limit") || key === "max_attempts" || key === "failure_threshold" ? 0 : 3}
            placeholder={zh ? "未设置本地上限" : "No local limit"}
            onChange={value => { if (value !== null || key.endsWith("limit")) setPatch(p => ({ ...p, [key]: value })); }} />
        </label>)}
      </div>
      <Text type="secondary">{zh ? "本地限额不表示供应商余额；空值表示未设置该本地上限。" : "Local limits are not supplier balances; empty means no limit configured here."}</Text>
      <Space wrap style={{ marginTop: 12 }}>
        <Button type="primary" disabled={!Object.keys(patch).length} loading={busy} onClick={() => void publish()}>{zh ? "发布新版本" : "Publish new version"}</Button>
        <Button disabled={busy} onClick={async () => {
          try { setHistory(await api.getApiConfigurationHistory(row.key, scope)); }
          catch (e) { message.error(e instanceof Error ? e.message : String(e)); }
        }}>{zh ? "查看版本历史" : "Version history"}</Button>
        {!!history.length && <>
          <Select aria-label={`${title} ${zh ? "历史版本" : "history"}`} style={{ width: 180 }} value={restore} onChange={setRestore}
            options={history.map(h => ({ value: h.version, label: `v${h.version}${h.origin === "import_current_only" ? (zh ? "（开始记录）" : " (first recorded)") : ""}` }))} />
          <Button disabled={!restore || busy} onClick={() => void publish(true)}>{zh ? "恢复为新版本" : "Restore as new version"}</Button>
        </>}
      </Space>
    </section>
  </Card>;
}
