import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Space, Table, Typography } from "antd";
import { api, ApiError, type SecurityStatusDownloadInput, type SecurityStatusDownloadRead, type SecurityStatusDownloadScope } from "../api/client";
import { AppContext } from "../context/AppContext";

const pendingKey = (id: number) => `security-status-download-request:${id}`;
const isActive = (value: SecurityStatusDownloadRead | null) => !!value?.task && ["queued", "running"].includes(value.task.status);
const gaps: Record<string, string> = {
  is_delisting_period: "退市整理期的逐日状态",
  daily_price_limits: "每天的涨跌停价格、无限制交易日和报价单位",
  source_updated_at: "资料原始更新时间",
};
function readPending(id: number): SecurityStatusDownloadInput | null {
  try {
    const value = JSON.parse(localStorage.getItem(pendingKey(id)) || "null");
    return value?.portfolio_id === id && typeof value.request_id === "string" &&
      typeof value.expected_scope_sha256 === "string" && typeof value.start_date === "string" && typeof value.end_date === "string" ? value : null;
  } catch { return null; }
}
function storePending(id: number, payload: SecurityStatusDownloadInput | null) {
  try { payload ? localStorage.setItem(pendingKey(id), JSON.stringify(payload)) : localStorage.removeItem(pendingKey(id)); } catch { /* In-memory recovery still works. */ }
}

export default function SecurityStatusDownloadPanel({ portfolioId: selectedId, requestedRange }: {
  portfolioId?: number | null; requestedRange?: { start: string; end: string };
}) {
  const context = useContext(AppContext);
  const portfolioId = selectedId === undefined ? context?.portfolioId ?? null : selectedId;
  const [scope, setScope] = useState<SecurityStatusDownloadScope | null>(null);
  const [result, setResult] = useState<SecurityStatusDownloadRead | null>(null);
  const [dates, setDates] = useState({ start: "", end: "" });
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dateFillApplied, setDateFillApplied] = useState(false);
  const dateFieldsRef = useRef<HTMLDetailsElement | null>(null);
  const [reading, setReading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<SecurityStatusDownloadInput | null>(null);
  const [unconfirmed, setUnconfirmed] = useState(false);
  const [pollVersion, setPollVersion] = useState(0);
  const currentPortfolio = useRef(portfolioId);
  currentPortfolio.current = portfolioId;
  const sequence = useRef(0);
  const working = useRef(false);
  const requestedStart = requestedRange?.start;
  const requestedEnd = requestedRange?.end;
  const current = useCallback((id: number, token: number) => currentPortfolio.current === id && sequence.current === token, []);

  const accept = useCallback((value: SecurityStatusDownloadRead, id: number, token: number) => {
    if (!current(id, token)) return;
    if (value.report && value.report.portfolio_id !== id) throw new Error("下载记录不属于当前组合，请重新读取。");
    setResult(value);
    if (value.task) { setPending(null); setUnconfirmed(false); storePending(id, null); }
  }, [current]);

  const load = useCallback(async () => {
    const id = portfolioId, token = ++sequence.current;
    setScope(null); setResult(null); setError(""); setNotice(""); setPending(null); setUnconfirmed(false);
    setDateFillApplied(false);
    setReading(true); setBusy(false); working.current = false;
    if (id === null) { setReading(false); return; }
    try {
      const value = await api.getSecurityStatusDownloadScope(id);
      if (!current(id, token)) return;
      setScope(value); setDates({ start: requestedStart ?? value.start_date, end: requestedEnd ?? value.end_date });
      const saved = readPending(id);
      if (saved) {
        setPending(saved);
        const recovered = await api.findSecurityStatusDownloadRequest(id, saved.request_id);
        accept(recovered, id, token);
        if (current(id, token) && !recovered.task) setNotice("还没找到这次下载记录，可以重试同一次下载。");
      } else if (value.latest_task) {
        accept(await api.getSecurityStatusDownload(value.latest_task.id), id, token);
      }
    } catch (failure) {
      if (current(id, token)) {
        setError(failure instanceof ApiError && failure.status_code === 409 ? failure.message : "下载范围或已有记录读取失败，请重新读取。");
        setUnconfirmed(!!readPending(id));
      }
    } finally { if (current(id, token)) setReading(false); }
  }, [portfolioId, accept, current, requestedStart, requestedEnd]);
  useEffect(() => { void load(); return () => { ++sequence.current; }; }, [load]);

  const task = scope?.portfolio_id === portfolioId ? result?.task : null;
  const report = scope?.portfolio_id === portfolioId ? result?.report : null;
  useEffect(() => {
    if (!task || !isActive(result) || portfolioId === null || reading) return;
    const id = portfolioId, token = sequence.current;
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const value = await api.getSecurityStatusDownload(task.id);
        if (stopped || !current(id, token)) return;
        accept(value, id, token); setError("");
        if (isActive(value)) timer = setTimeout(() => void poll(), 1500);
      } catch {
        if (!stopped && current(id, token)) setError("下载进度读取失败，已取得的文件会保留，请查看下载结果。");
      }
    };
    void poll();
    return () => { stopped = true; if (timer) clearTimeout(timer); };
  }, [task?.id, task?.status, portfolioId, reading, pollVersion, accept, current]);

  const submit = async () => {
    if (portfolioId === null || !scope || working.current || reading || unconfirmed || isActive(result)) return;
    const id = portfolioId, token = sequence.current;
    const payload = pending ?? { portfolio_id: id, request_id: crypto.randomUUID(), start_date: dates.start,
      end_date: dates.end, expected_scope_sha256: scope.scope_sha256 };
    working.current = true; setBusy(true); setError(""); setNotice(""); setPending(payload); storePending(id, payload);
    try {
      accept(await api.startSecurityStatusDownload(payload), id, token);
    } catch (failure) {
      if (!current(id, token)) return;
      if (failure instanceof ApiError && failure.status_code && failure.status_code >= 400 && failure.status_code < 500) {
        setError(failure.message); setScope(null); setPending(null); storePending(id, null);
      } else {
        setUnconfirmed(true); setError("尚未确认下载是否开始，请先查看下载结果。");
      }
    } finally { if (current(id, token)) { working.current = false; setBusy(false); } }
  };
  const recover = async () => {
    if (portfolioId === null || working.current) return;
    if (!scope) { await load(); return; }
    const id = portfolioId, token = sequence.current;
    working.current = true; setBusy(true);
    try {
      if (pending) {
        const value = await api.findSecurityStatusDownloadRequest(id, pending.request_id);
        accept(value, id, token);
        if (current(id, token)) { setUnconfirmed(false); setError(""); if (!value.task) setNotice("还没找到这次下载记录，可以重试同一次下载。"); }
      } else if (task) {
        accept(await api.getSecurityStatusDownload(task.id), id, token);
        if (current(id, token)) { setError(""); setPollVersion(v => v + 1); }
      }
    } catch { if (current(id, token)) setError("下载结果读取失败，请稍后再查看。"); }
    finally { if (current(id, token)) { working.current = false; setBusy(false); } }
  };
  const stop = async () => {
    if (!task || portfolioId === null || working.current) return;
    const id = portfolioId, token = sequence.current;
    working.current = true; setBusy(true);
    try {
      await api.cancelAsyncTask(task.id);
      accept(await api.getSecurityStatusDownload(task.id), id, token);
    } catch { if (current(id, token)) setError("尚未确认下载是否停止，请查看下载结果。"); }
    finally { if (current(id, token)) { working.current = false; setBusy(false); } }
  };
  const saveFiles = async () => {
    if (!task || portfolioId === null || working.current) return;
    const id = portfolioId, token = sequence.current;
    working.current = true; setBusy(true);
    try {
      const files = await api.getSecurityStatusDownloadFiles(task.id);
      if (!current(id, token)) return;
      const bytes = Uint8Array.from(atob(files.content_base64), c => c.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: "application/zip" }));
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = files.filename;
      document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
      setNotice("原始文件已交给浏览器保存，请查看下载记录。");
    } catch { if (current(id, token)) setError("原始文件保存失败，文件可能缺失或已改变。请重新读取下载结果。"); }
    finally { if (current(id, token)) { working.current = false; setBusy(false); } }
  };

  const active = isActive(result) && !!task;
  const validScope = scope?.portfolio_id === portfolioId ? scope : null;
  const requestedDays = (Date.parse(dates.end) - Date.parse(dates.start)) / 86400000 + 1;
  const rangeUnavailable = !!requestedRange && !!validScope && (dates.end > validScope.end_date || requestedDays > validScope.max_days);
  const dayMs = 86400000;
  const allowedEnd = Math.min(Date.parse(dates.end), Date.parse(validScope?.end_date ?? ""));
  const allowedStart = Math.max(Date.parse(dates.start), allowedEnd - ((validScope?.max_days ?? 0) - 1) * dayMs);
  const allowedDates = validScope && validScope.max_days > 0 && Number.isFinite(allowedStart) && Number.isFinite(allowedEnd) && allowedStart <= allowedEnd
    ? { start: new Date(allowedStart).toISOString().slice(0, 10), end: new Date(allowedEnd).toISOString().slice(0, 10) } : null;
  const dateFillDisabled = reading || busy || active || !!pending || unconfirmed || !allowedDates;
  const outsideDownload: string[] = [];
  if (dateFillApplied && requestedRange) {
    if (requestedRange.start < dates.start) outsideDownload.push(`${requestedRange.start} 至 ${new Date(Date.parse(dates.start) - dayMs).toISOString().slice(0, 10)}`);
    if (requestedRange.end > dates.end) outsideDownload.push(`${new Date(Date.parse(dates.end) + dayMs).toISOString().slice(0, 10)} 至 ${requestedRange.end}`);
  }
  const taskMessage = !task ? "" : task.stage === "interrupted" ? "应用上次关闭时下载尚未完成，请重新下载。" : ({
    queued: "正在等待下载…", running: "正在下载历史股票状态…", done: "下载完成，历史资料仍不完整。",
    partial: "原始文件已保存，部分状态仍待核对。", failed: "下载没有完成，请查看下方原因后重试。",
    cancelled: "下载已停止，已取得的原始文件仍保留。",
  } as Record<string, string>)[task.status] ?? "正在读取下载状态…";
  return <section aria-label="下载历史股票状态" style={{ marginBottom: 24, padding: 16, border: "1px solid var(--pt-border, #303846)", borderRadius: 8 }}>
    <Typography.Title level={5} style={{ marginTop: 0 }}>下载历史股票状态</Typography.Title>
    <Typography.Paragraph type="secondary">{requestedRange ? "已带入本次回测日期。下载范围按该组合的成员和持仓确定，可能多于本次回测标的。" : "按当前组合的成员和持仓下载，默认近一年、截至昨日。"}使用免费公开来源 BaoStock，下载期间可以继续使用其他页面。</Typography.Paragraph>
    {portfolioId === null ? <Typography.Paragraph>请先在顶部选择组合，再下载该组合的资料。</Typography.Paragraph>
      : validScope ? <Typography.Paragraph>{validScope.portfolio_name} · {validScope.symbols.length} 个标的 · {dates.start} 至 {dates.end}</Typography.Paragraph>
      : <Typography.Paragraph>{reading ? "正在读取当前组合的下载范围…" : "暂时没有取得当前组合的下载范围。"}</Typography.Paragraph>}
    {error && <Alert type="error" showIcon message={error} />}
    {notice && <Typography.Paragraph role="status">{notice}</Typography.Paragraph>}
    {rangeUnavailable && <Alert type="warning" showIcon message="本次日期超出了来源可提供的范围"
      description={`当前最多下载到 ${validScope!.end_date}，一次最多 ${validScope!.max_days} 天。回测原日期已保留。${allowedDates ? "可按下面的按钮填写下载日期，确认后再下载。" : "所选日期目前没有可填写的下载范围，请调整日期后再试。"}`}
      action={<Button disabled={dateFillDisabled} onClick={() => {
        if (dateFillDisabled || !allowedDates) return;
        setDates(allowedDates); setDateFillApplied(true);
        if (dateFieldsRef.current) dateFieldsRef.current.open = true;
      }}>按可下载日期填写</Button>} />}
    {dateFillApplied && requestedRange && <Typography.Paragraph role="status" style={{ marginTop: 10 }}>
      原回测日期：{requestedRange.start} 至 {requestedRange.end}。本次下载日期：{dates.start} 至 {dates.end}。日期已填好，请确认后点击下载。
      {outsideDownload.length > 0 && <><br />本次不会补充：{outsideDownload.join("、")}。返回回测后仍按原日期检查。</>}
    </Typography.Paragraph>}
    {validScope?.reasons.map(reason => <Typography.Paragraph key={reason} type="warning">{reason}</Typography.Paragraph>)}
    <Space wrap style={{ margin: "10px 0" }}>
      <Button type="primary" loading={busy && !active} disabled={!validScope?.can_download || reading || busy || active || unconfirmed || rangeUnavailable || !dates.start || !dates.end || dates.start > dates.end}
        onClick={() => void submit()}>{pending && !unconfirmed ? "重试下载" : task && !active ? "重新下载当前组合资料" : "下载当前组合资料"}</Button>
      {(unconfirmed || (error && task)) && <Button disabled={busy || reading} onClick={() => void recover()}>查看下载结果</Button>}
      {active && <Button disabled={busy || !!task?.cancel_requested} onClick={() => void stop()}>{task?.cancel_requested ? "正在停止…" : "停止下载"}</Button>}
      {report && report.raw_files.length > 0 && <Button disabled={busy} onClick={() => void saveFiles()}>保存原始文件</Button>}
      {!active && !unconfirmed && <Button disabled={busy || reading || portfolioId === null} onClick={() => void load()}>重新读取范围</Button>}
    </Space>
    {validScope && <details ref={dateFieldsRef} style={{ margin: "4px 0 14px" }}><summary>调整日期</summary><Space wrap style={{ marginTop: 8 }}>
      <label>开始日期 <Input aria-label="历史资料开始日期" type="date" value={dates.start} disabled={active || busy || !!pending}
        onChange={event => { setDateFillApplied(false); setDates(d => ({ ...d, start: event.target.value })); }} /></label>
      <label>结束日期 <Input aria-label="历史资料结束日期" type="date" value={dates.end} max={validScope.end_date} disabled={active || busy || !!pending}
        onChange={event => { setDateFillApplied(false); setDates(d => ({ ...d, end: event.target.value })); }} /></label>
      <Typography.Text type="secondary">每次最多 {validScope.max_symbols} 个标的、{validScope.max_days} 天。</Typography.Text>
    </Space></details>}
    {task && <Alert showIcon type={active ? "info" : task.status === "failed" ? "error" : "warning"}
      message={taskMessage}
      description={active ? "提交时已固定组合范围。切换组合后，回来仍可查看这次进度。" : undefined} />}
    {report && <>
      <Typography.Paragraph style={{ marginTop: 12 }}>{report.valid_daily_rows > 0
        ? <>本次取得 {report.valid_daily_rows} 条逐日状态，日期 {report.start_date} 至 {report.end_date}。文件已保存在本机，尚不能用于正式回测。</>
        : "本次未取得可核对的逐日状态。请查看下方原因后重试，尚不能用于正式回测。"}</Typography.Paragraph>
      <Typography.Text>还缺以下资料：</Typography.Text>
      <ul>{report.missing_fields.map(key => <li key={key}>{gaps[key] ?? "其他历史资料"}</li>)}</ul>
      {report.source_error && <Typography.Paragraph type="danger">{report.source_error}</Typography.Paragraph>}
      {report.errors.map(issue => <Typography.Paragraph type="danger" key={issue}>{issue}</Typography.Paragraph>)}
      {report.valid_daily_rows > 0 && <Table size="small" rowKey="symbol" pagination={false} scroll={{ x: 620 }} dataSource={report.symbols} columns={[
        { title: "代码", dataIndex: "symbol" }, { title: "原始记录（天）", dataIndex: "raw_daily_rows" },
        { title: "可核对状态（天）", dataIndex: "valid_daily_rows" },
        { title: "ST（天）", render: (_, row) => row.st_days ?? "待核对" },
        { title: "停牌（天）", render: (_, row) => row.suspended_days ?? "待核对" },
        { title: "缺失交易日", render: (_, row) => row.missing_trade_dates === null ? "尚不能确认" : row.missing_trade_dates.length },
        { title: "检查结果", render: (_, row) => row.errors.length ? row.errors.join(" ") : "已取得该范围的逐日状态，其他资料仍缺失" },
      ]} />}
    </>}
  </section>;
}
