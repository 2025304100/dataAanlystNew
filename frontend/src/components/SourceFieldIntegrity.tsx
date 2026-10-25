import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Space, Table, Typography } from "antd";
import { api } from "../api/client";

type Report = Awaited<ReturnType<typeof api.getSourceFieldIntegrity>>;
const WARNING_LABELS: Record<string, string> = {
  calendar_unverified: "本地交易日历不足，不能确认应有的最新交易日",
  no_rows_as_of: "截止日以内没有记录，无法判断完整性",
  source_behind_calendar: "源表最新日期落后于已确认交易日，请核对日期缺口",
  cache_date_mismatch: "登记缓存与源表最新日期不一致；本次检查未修改缓存",
  future_source_date: "源表存在晚于今天的日期，需要核查",
};

export default function SourceFieldIntegrity() {
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [asOf, setAsOf] = useState("");
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const read = async () => {
    setLoading(true);
    try {
      const next = await api.getSourceFieldIntegrity(asOf || undefined);
      if (!next || !Array.isArray(next.fields)) throw new Error("源表检查返回不完整，请重试读取。");
      if (alive.current) { setReport(next); setError(null); }
    } catch (reason) {
      if (alive.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally { if (alive.current) setLoading(false); }
  };
  return <section style={{ margin: "18px 0" }}>
    <Typography.Title level={5}>源表关键字段完整性</Typography.Title>
    <Typography.Paragraph type="secondary">
      单独检查财报净利润、营收及同比、ETF 换手率、指数成交额和估值分位。只统计已落库记录的字段，完整率不代表交易日期已补齐。ETF 与估值检查截止日期及此前29个自然日，其余检查全部记录。未选日期时使用上海时间今天，窗口不会随旧数据后退。
    </Typography.Paragraph>
    <Space wrap>
      <label htmlFor="source-integrity-as-of">验收截止日期（可选）</label>
      <Input id="source-integrity-as-of" type="date" value={asOf} disabled={loading} onChange={event => setAsOf(event.target.value)} style={{ width: 180 }} />
      <Button loading={loading} onClick={() => void read()}>检查源表关键字段</Button>
    </Space>
    {error && <Alert type="error" showIcon message="源表检查未更新，已有结果仅供参考" description={error} style={{ marginTop: 8 }} />}
    {report && <>
      <Typography.Paragraph style={{ marginTop: 12 }}>
        验收截止：{report.as_of}（{report.timezone || "Asia/Shanghai"}）；已确认的最近完整交易日：{report.expected_latest_date || "无法确认"}。
      </Typography.Paragraph>
      {report.sources?.map(source => <Alert key={source.table} showIcon
        type={source.warnings.length ? "warning" : "info"} style={{ marginBottom: 8 }}
        message={`${source.label}：源表最新日期 ${source.latest_date || "无记录"}；截止日内最新日期 ${source.latest_date_as_of || "无记录"}`}
        description={<>
          {source.table === "universe_daily_bars" && <div>境内 ETF 登记缓存最大日期：{source.cache_latest_date || "无记录"}。最大日期一致不代表逐标的缓存均一致。</div>}
          {source.warnings.map(warning => <div key={warning}>{WARNING_LABELS[warning] || "检查发现未识别的警告，请查看接口返回"}</div>)}
          <div>源表最新日期不代表所有标的或交易日期完整。</div>
        </>} />)}
    </>}
    {report && <Table<Report["fields"][number]> size="small" pagination={false}
      rowKey={row => `${row.table}:${row.field}`} dataSource={report.fields} scroll={{ x: 900 }}
      columns={[
        { title: "源表 / 字段", render: (_, row) => `${row.table}.${row.field}` },
        { title: "记录数", dataIndex: "total" }, { title: "非空数", dataIndex: "nonnull" },
        { title: "缺失数", dataIndex: "missing", render: value => <Typography.Text type={value ? "danger" : undefined}>{value}</Typography.Text> },
        { title: "非空占比", render: (_, row) => row.coverage_pct === null ? "无记录，无法判断" : `${row.coverage_pct}%` },
        { title: "统计范围", render: (_, row) => row.start_date ? `${row.start_date} ~ ${row.end_date}` : "全部已落库记录" },
        { title: "口径说明", dataIndex: "note" },
      ]} />}
  </section>;
}
