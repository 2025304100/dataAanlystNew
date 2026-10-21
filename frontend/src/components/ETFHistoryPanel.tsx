import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { Alert, Button, Input, Progress, Space, Table, Typography } from 'antd';
import { api, ApiError, type ETFHistoryInput, type ETFHistoryRead, type ETFHistoryScope } from '../api/client';
import { AppContext } from '../context/AppContext';

const key = (id: number) => `etf-history-request:${id}`;
const active = (value: ETFHistoryRead | null) => !!value?.task && ['queued', 'running'].includes(value.task.status);
type HistoryRow = ETFHistoryScope['symbols'][number] | NonNullable<ETFHistoryRead['report']>['symbols'][number];
function savedRequest(id: number): ETFHistoryInput | null {
  try {
    const saved = JSON.parse(localStorage.getItem(key(id)) || 'null');
    return saved?.portfolio_id === id && typeof saved.request_id === 'string' && typeof saved.start_date === 'string'
      && typeof saved.end_date === 'string' && typeof saved.expected_scope_sha256 === 'string' ? saved : null;
  } catch { return null; }
}
function Dates({ values, complete }: { values: string[] | null; complete: string }) {
  if (values === null) return <span>交易日历尚未核实，暂不能确认</span>;
  if (!values.length) return <span>{complete}</span>;
  return <><span>{values.slice(0, 6).join('、')}</span>{values.length > 6 && <details><summary>查看全部 {values.length} 个日期</summary>{values.join('、')}</details>}</>;
}

export default function ETFHistoryPanel({ portfolioId: selectedId }: { portfolioId?: number | null }) {
  const context = useContext(AppContext);
  const portfolioId = selectedId === undefined ? context?.portfolioId ?? null : selectedId;
  const [scope, setScope] = useState<ETFHistoryScope | null>(null);
  const [result, setResult] = useState<ETFHistoryRead | null>(null);
  const [dates, setDates] = useState({ start: '', end: '' });
  const [error, setError] = useState('');
  const [reading, setReading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<ETFHistoryInput | null>(null);
  const [unknown, setUnknown] = useState(false);
  const [pollVersion, setPollVersion] = useState(0);
  const generation = useRef(0), working = useRef(false), selected = useRef(portfolioId);
  selected.current = portfolioId;
  const current = useCallback((id: number, token: number) => selected.current === id && generation.current === token, []);

  const accept = useCallback((value: ETFHistoryRead, id: number, token: number) => {
    if (!current(id, token)) return;
    if (value.report && value.report.portfolio_id !== id) throw new Error('补充记录不属于当前组合，请重新读取。');
    setResult(value);
    if (value.report && !value.report.imported) setDates({ start: value.report.start_date, end: value.report.end_date });
    if (value.task) {
      setUnknown(false); setPending(null);
      try { localStorage.removeItem(key(id)); } catch { /* The server has acknowledged this request. */ }
    }
  }, [current]);

  const load = useCallback(async () => {
    const id = portfolioId, token = ++generation.current;
    setScope(null); setResult(null); setPending(null); setUnknown(false); setError(''); setReading(true); setBusy(false); working.current = false;
    if (id === null) { setReading(false); return; }
    const saved = savedRequest(id);
    if (saved) { setPending(saved); setDates({ start: saved.start_date, end: saved.end_date }); }
    try {
      const value = await api.getETFHistoryScope(id);
      if (!current(id, token)) return;
      setScope(value);
      if (!saved) setDates({ start: value.start_date, end: value.end_date });
      if (saved) {
        accept(await api.findETFHistoryRequest(id, saved.request_id), id, token);
      } else if (value.latest_task) {
        accept(await api.getETFHistoryPreparation(value.latest_task.id), id, token);
      }
    } catch {
      if (current(id, token)) { setError('ETF历史数据或补充记录读取失败，请重新读取。'); setUnknown(!!saved); }
    } finally { if (current(id, token)) setReading(false); }
  }, [portfolioId, accept, current]);
  useEffect(() => { void load(); return () => { ++generation.current; }; }, [load]);

  const validScope = scope?.portfolio_id === portfolioId ? scope : null;
  const visible = validScope ? result : null;
  const task = visible?.task, report = visible?.report;
  useEffect(() => {
    if (!task || !active(visible) || portfolioId === null || reading) return;
    const id = portfolioId, token = generation.current;
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const value = await api.getETFHistoryPreparation(task.id);
        if (stopped || !current(id, token)) return;
        accept(value, id, token); setError('');
        if (active(value)) timer = setTimeout(() => void poll(), 1500);
        else if (value.report?.imported) {
          const updated = await api.getETFHistoryScope(id, { start: value.report.start_date, end: value.report.end_date });
          if (current(id, token)) setScope(updated);
        }
      } catch { if (!stopped && current(id, token)) setError('补充进度读取失败，任务可能仍在运行。请重试读取进度。'); }
    };
    void poll();
    return () => { stopped = true; if (timer) clearTimeout(timer); };
  }, [task?.id, task?.status, reading, portfolioId, pollVersion, accept, current]);

  const submit = async () => {
    if (portfolioId === null || !validScope || working.current || active(visible) || unknown) return;
    const id = portfolioId, token = generation.current;
    const payload = pending ?? { portfolio_id: id, request_id: crypto.randomUUID(), start_date: dates.start, end_date: dates.end, expected_scope_sha256: validScope.scope_sha256 };
    try { localStorage.setItem(key(id), JSON.stringify(payload)); }
    catch { setError('浏览器无法保存本次请求，请检查存储设置后重试。'); return; }
    working.current = true; setBusy(true); setPending(payload); setError('');
    try { accept(await api.startETFHistoryPreparation(payload), id, token); }
    catch (failure) {
      if (!current(id, token)) return;
      if (failure instanceof ApiError && failure.status_code && failure.status_code !== 408 && failure.status_code >= 400 && failure.status_code < 500) {
        setError(failure.message); setPending(null); localStorage.removeItem(key(id));
      } else { setUnknown(true); setError('尚未确认补充是否开始，请先重试读取进度。'); }
    } finally { if (current(id, token)) { working.current = false; setBusy(false); } }
  };
  const recover = async () => {
    if (portfolioId === null || working.current) return;
    const id = portfolioId, token = generation.current;
    working.current = true; setBusy(true);
    try {
      const value = pending ? await api.findETFHistoryRequest(id, pending.request_id) : task ? await api.getETFHistoryPreparation(task.id) : null;
      if (!current(id, token)) return;
      if (value) accept(value, id, token);
      setUnknown(false); setError(''); setPollVersion(v => v + 1);
    } catch { if (current(id, token)) setError('补充记录仍未读到，请稍后重试读取进度。'); }
    finally { if (current(id, token)) { working.current = false; setBusy(false); } }
  };
  const running = active(visible);
  const checkedDates = report || validScope;
  const dateDirty = !!checkedDates && (dates.start !== checkedDates.start_date || dates.end !== checkedDates.end_date);
  const rows = dateDirty ? [] : report ? report.symbols : validScope?.symbols ?? [];
  return <section aria-label="ETF历史数据" style={{ marginBottom: 20, padding: 16, border: '1px solid var(--pt-border, #303846)', borderRadius: 8 }}>
    <Typography.Title level={5} style={{ marginTop: 0 }}>ETF历史数据</Typography.Title>
    <Typography.Paragraph type="secondary">补充当前组合中ETF的历史行情，核对原始价、前复权价和已有分红公告。下载期间可以继续使用其他页面。</Typography.Paragraph>
    {portfolioId === null ? <p>请先在顶部选择组合。</p> : reading ? <p role="status">正在读取ETF历史数据…</p> : validScope && <p>{validScope.portfolio_name} · {dates.start} 至 {dates.end}</p>}
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 8 }} />}
    {validScope?.reasons.map(reason => <p key={reason}>{reason}</p>)}
    {!dateDirty && validScope?.errors.map(reason => <Alert type="warning" key={reason} message={reason} />)}
    {!dateDirty && validScope && <p>{validScope.training_message}</p>}
    {dateDirty && !running && <p>日期已修改，请按新日期补充历史数据。</p>}
    <Space wrap style={{ marginBottom: 12 }}>
      <Button type="primary" loading={busy} disabled={!validScope?.can_prepare || reading || running || busy || unknown || !dates.start || !dates.end || dates.start > dates.end}
        onClick={() => void submit()}>{task && !running && task.status !== 'done' || pending && !unknown ? '重试补充' : '补充ETF历史数据'}</Button>
      {(unknown || (error && task)) && <Button disabled={busy} onClick={() => void recover()}>重试读取进度</Button>}
      {!running && <Button disabled={busy || reading || portfolioId === null} onClick={() => void load()}>重新读取</Button>}
    </Space>
    {validScope && <details style={{ marginBottom: 12 }}><summary>调整补充日期</summary><Space wrap style={{ marginTop: 8 }}>
      <label>开始日期 <Input aria-label="ETF历史开始日期" type="date" value={dates.start} disabled={running || busy || !!pending} onChange={e => setDates(d => ({ ...d, start: e.target.value }))} /></label>
      <label>结束日期 <Input aria-label="ETF历史结束日期" type="date" value={dates.end} disabled={running || busy || !!pending} onChange={e => setDates(d => ({ ...d, end: e.target.value }))} /></label>
      <Typography.Text type="secondary">只补充已结束的日期，每次最多{validScope.max_days}天。</Typography.Text>
    </Space></details>}
    {task && (running || !dateDirty) && <div role="status"><Alert showIcon type={running ? 'info' : report?.imported ? 'success' : 'warning'} message={report?.imported ? '本次行情已补齐并核对。' : task.stage === 'interrupted' || task.stage === 'graceful_interrupted' ? '应用上次关闭时补充尚未完成，请重试补充。' : task.message} />
      {running && <Progress aria-label="ETF历史数据补充进度" percent={Math.round(task.percent)} />}</div>}
    {!dateDirty && report?.errors.map(reason => <Typography.Paragraph type="danger" key={reason}>{reason}</Typography.Paragraph>)}
    {!dateDirty && report && <Typography.Paragraph style={{ marginTop: 12 }}>本次补充范围：{report.start_date} 至 {report.end_date}</Typography.Paragraph>}
    {!!rows.length && <Table<HistoryRow> size="small" rowKey="symbol" pagination={false} dataSource={rows} scroll={{ x: 650 }} columns={[
      { title: 'ETF', render: (_, row) => `${row.symbol} ${row.name}` },
      { title: report ? '本次取得' : '本地已有', render: (_, row) => `${row.row_count}天` },
      { title: '缺失日期', render: (_, row) => <Dates values={row.missing_trade_dates} complete="所查范围没有缺失日期" /> },
      { title: '检查结果', render: (_, row) => 'error' in row ? row.error || '原始价和前复权价已核对' : <Dates values={row.unreviewed_dates} complete="已有历史资料已核对" /> },
    ]} />}
    <Typography.Paragraph type="secondary" style={{ marginTop: 10, marginBottom: 0 }}>行情齐全不代表可以成交。交易资料不完整时，成交回测仍不能运行；补充行情不会训练或启用模型，也不会改动账户。</Typography.Paragraph>
  </section>;
}
