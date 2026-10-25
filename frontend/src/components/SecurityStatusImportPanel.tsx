import { AcceptanceButton as Button } from "./portfolio-trading/inspection/AcceptanceControls";
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Checkbox, Input, Space, Table, Typography } from "antd";
import { api, type SecurityStatusCoverage, type SecurityStatusImportInput, type SecurityStatusImportPreview, type SecurityStatusTemplate } from "../api/client";

const EMPTY_SOURCE = { source_name: "", source_reference: "", source_version: "", updated_at: "" };
const statusLabels: Record<string, string> = { LISTED: "正常上市", ST: "ST", SUSPENDED: "停牌", DELISTING: "退市整理", NOT_LISTED: "未上市或已退市", UNKNOWN: "未知" };
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

export default function SecurityStatusImportPanel() {
  const [coverage, setCoverage] = useState<SecurityStatusCoverage | null>(null);
  const [template, setTemplate] = useState<SecurityStatusTemplate | null>(null);
  const [coverageError, setCoverageError] = useState("");
  const [templateError, setTemplateError] = useState("");
  const [reading, setReading] = useState(false);
  const [filename, setFilename] = useState("");
  const [csv, setCsv] = useState("");
  const [source, setSource] = useState(EMPTY_SOURCE);
  const [preview, setPreview] = useState<SecurityStatusImportPreview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [checking, setChecking] = useState(false);
  const [importing, setImporting] = useState(false);
  const [fileError, setFileError] = useState("");
  const [success, setSuccess] = useState("");
  const mounted = useRef(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const fileSequence = useRef(0);
  const previewSequence = useRef(0);
  const coverageSequence = useRef(0);

  const loadCoverage = useCallback(async () => {
    const sequence = ++coverageSequence.current;
    setReading(true);
    try {
      const result = await api.getSecurityStatusCoverage();
      if (mounted.current && sequence === coverageSequence.current) { setCoverage(result); setCoverageError(""); }
    } catch (error) {
      if (mounted.current && sequence === coverageSequence.current) setCoverageError(errorText(error));
    } finally {
      if (mounted.current && sequence === coverageSequence.current) setReading(false);
    }
  }, []);
  const loadTemplate = useCallback(async () => {
    try {
      const result = await api.getSecurityStatusTemplate();
      if (mounted.current) { setTemplate(result); setTemplateError(""); }
    } catch (error) { if (mounted.current) setTemplateError(errorText(error)); }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void loadCoverage(); void loadTemplate();
    return () => { mounted.current = false; ++fileSequence.current; ++previewSequence.current; ++coverageSequence.current; };
  }, [loadCoverage, loadTemplate]);

  const invalidatePreview = () => { ++previewSequence.current; setPreview(null); setConfirmed(false); setChecking(false); setFileError(""); setSuccess(""); };
  const changeSource = (key: keyof typeof EMPTY_SOURCE, value: string) => {
    if (importing) return;
    invalidatePreview(); setSource((previous) => ({ ...previous, [key]: value }));
  };
  const selectFile = (file?: File) => {
    if (!file || importing) return;
    const sequence = ++fileSequence.current;
    invalidatePreview(); setCsv(""); setFilename(""); setSource(EMPTY_SOURCE);
    if (!file.name.toLowerCase().endsWith(".csv")) { setFileError("请选择CSV文件。可以先下载表头，核对文件格式。"); return; }
    if (!template || file.size > template.max_bytes) { setFileError("文件过大，请按页面显示的上限分成多个文件。"); return; }
    setFilename(file.name); setSource({ ...EMPTY_SOURCE, source_reference: file.name });
    const reader = new FileReader();
    reader.onload = () => { if (mounted.current && sequence === fileSequence.current) setCsv(String(reader.result ?? "").replace(/^\uFEFF/, "")); };
    reader.onerror = () => { if (mounted.current && sequence === fileSequence.current) setFileError("文件读取失败，请重新选择文件。"); };
    reader.readAsText(file, "utf-8");
  };
  const payload = (): SecurityStatusImportInput => ({
    csv_text: csv, filename, source_name: source.source_name.trim(), source_reference: source.source_reference.trim(),
    source_version: source.source_version.trim(), source_updated_at: new Date(`${source.updated_at}+08:00`).toISOString(),
  });
  const ready = Boolean(csv.trim() && source.source_name.trim() && source.source_reference.trim() && source.source_version.trim()
    && source.updated_at && Number.isFinite(Date.parse(`${source.updated_at}+08:00`)));
  const checkFile = async () => {
    if (!ready || checking || importing) return;
    const sequence = ++previewSequence.current;
    setChecking(true); setFileError(""); setPreview(null); setConfirmed(false); setSuccess("");
    try {
      const result = await api.previewSecurityStatusImport(payload());
      if (mounted.current && sequence === previewSequence.current) setPreview(result);
    } catch (error) { if (mounted.current && sequence === previewSequence.current) setFileError(`文件检查未完成：${errorText(error)}`); }
    finally { if (mounted.current && sequence === previewSequence.current) setChecking(false); }
  };
  const importFile = async () => {
    if (!preview?.can_import || !confirmed || importing) return;
    setImporting(true); setFileError("");
    try {
      const result = await api.importSecurityStatus({ ...payload(), preview_sha256: preview.content_sha256, confirmed: true });
      if (!mounted.current) return;
      setSuccess(result.message); setPreview(null); setConfirmed(false); setCsv(""); setFilename(""); setSource(EMPTY_SOURCE);
      void loadCoverage();
    } catch (error) {
      if (mounted.current) setFileError(`暂时无法确认导入结果：${errorText(error)}。请重新读取状态后再试。`);
    } finally { if (mounted.current) setImporting(false); }
  };
  const downloadTemplate = () => {
    if (!template) return;
    const url = URL.createObjectURL(new Blob(["\uFEFF", template.csv_text], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = "历史股票状态表头.csv";
    document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
  };

  return <section aria-label="历史股票状态" style={{ marginBottom: 24, padding: 16, border: "1px solid var(--pt-border, #303846)", borderRadius: 8 }}>
    <Typography.Title level={5} style={{ marginTop: 0 }}>历史股票状态</Typography.Title>
    <Typography.Paragraph type="secondary">回测需要逐日的停牌、ST、上市和退市资料。缺失日期仍按未知处理，不会用今天的状态补过去。</Typography.Paragraph>
    {coverageError ? <Alert type="error" showIcon message="历史股票状态读取失败" description={coverageError} />
      : coverage ? <Typography.Paragraph>{coverage.message}{coverage.verified_records > 0 && <><br />可用记录 {coverage.verified_records.toLocaleString()} 条，日期 {coverage.first_date || "未提供"} 至 {coverage.latest_date || "未提供"}。</>}</Typography.Paragraph>
      : <Typography.Paragraph>正在读取已有资料…</Typography.Paragraph>}
    {templateError && <Alert type="error" showIcon message="文件格式说明读取失败" description={templateError} action={<Button onClick={() => void loadTemplate()}>重新读取说明</Button>} />}
    <Space wrap style={{ margin: "10px 0" }}>
      <Button disabled={!template || importing} onClick={downloadTemplate}>下载表头</Button>
      <Button disabled={!template || importing} onClick={() => fileInput.current?.click()}>选择CSV文件</Button>
      <Button loading={reading} onClick={() => void loadCoverage()}>重新读取状态</Button>
    </Space>
    <input ref={fileInput} aria-label="历史股票状态文件" type="file" accept=".csv,text/csv" hidden disabled={importing} onChange={(event) => { selectFile(event.target.files?.[0]); event.target.value = ""; }} />
    {template && <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>每个文件最多 {template.max_rows.toLocaleString()} 行、{(template.max_bytes / 1000000).toLocaleString()} MB。表头只提供格式，不包含虚构的股票状态。</Typography.Paragraph>}
    {template?.optional_columns?.length ? <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>表头末尾的涨跌停价格等资料可留空；只有股票状态时仍可导入，但不会据此估算当天限价。</Typography.Paragraph> : null}
    {template && <details style={{ marginBottom: 12 }}><summary>查看文件填写说明</summary><ol style={{ paddingLeft: 22 }}>{template.instructions.map((instruction, index) => <li key={index}>{instruction}</li>)}</ol></details>}
    {filename && <>
      <Typography.Paragraph>已选文件：{filename}</Typography.Paragraph>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: 12 }}>
        <label>资料来源<Input aria-label="资料来源" value={source.source_name} disabled={importing} onChange={(event) => changeSource("source_name", event.target.value)} placeholder="填写交易所或提供方名称" /></label>
        <label>原文件或资料地址<Input aria-label="原文件或资料地址" value={source.source_reference} disabled={importing} onChange={(event) => changeSource("source_reference", event.target.value)} /></label>
        <label>资料版本或文件批次<Input aria-label="资料版本或文件批次" value={source.source_version} disabled={importing} onChange={(event) => changeSource("source_version", event.target.value)} placeholder="按提供方原资料填写" /></label>
        <label>资料注明的更新时间（北京时间）<Input type="datetime-local" aria-label="资料注明的更新时间（北京时间）" value={source.updated_at} disabled={importing} onChange={(event) => changeSource("updated_at", event.target.value)} /></label>
      </div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>请填写原资料的更新时间，不能用今天的上传时间代替。</Typography.Paragraph>
      <Button mutation onClick={() => void checkFile()} loading={checking} disabled={!ready || importing}>检查文件</Button>
    </>}
    {fileError && <Alert type="error" showIcon message={fileError} style={{ marginTop: 12 }} />}
    {success && <Alert type="success" showIcon message={success} description="已有记录已更新。回到回测页重新检查资料；这里不会自动启动回测或交易。" style={{ marginTop: 12 }} />}
    {preview && <div style={{ marginTop: 12 }}>
      <Alert type={preview.can_import ? "info" : "error"} showIcon message={preview.can_import ? `文件检查通过：${preview.total_rows} 条，${preview.start_date || "未提供"} 至 ${preview.end_date || "未提供"}` : "文件还有问题，未导入记录"}
        description={preview.can_import ? `新增 ${preview.insert_count} 条，替换未知记录 ${preview.replace_unknown_count} 条，已有相同记录 ${preview.unchanged_count} 条。` : preview.errors.slice(0, 12).map((item, index) => <div key={index}>{item.line > 0 ? `第 ${item.line} 行：` : "文件："}{item.message}</div>)} />
      {preview.warnings.map((warning, index) => <Typography.Paragraph key={index} type="secondary" style={{ margin: "6px 0" }}>{warning}</Typography.Paragraph>)}
      {preview.can_import && preview.execution_constraint_count !== undefined && <Typography.Paragraph>其中 {preview.execution_constraint_count} 条提供了当日限价资料。其余记录只保存股票状态，不会估算涨跌停价格。</Typography.Paragraph>}
      {preview.can_import && <>
        <Table size="small" rowKey={(row) => `${row.symbol}-${row.trade_date}`} dataSource={preview.sample} pagination={false} columns={[
          { title: "股票代码", dataIndex: "symbol" }, { title: "日期", dataIndex: "trade_date" }, { title: "当日状态", dataIndex: "status", render: (value: string) => statusLabels[value] || "未知" },
        ]} />
        <Space wrap style={{ marginTop: 12 }}>
          <Checkbox checked={confirmed} disabled={importing} onChange={(event) => setConfirmed(event.target.checked)}>我已核对资料来源和历史日期</Checkbox>
          <Button mutation type="primary" disabled={!confirmed} loading={importing} onClick={() => void importFile()}>导入记录</Button>
        </Space>
      </>}
    </div>}
  </section>;
}
