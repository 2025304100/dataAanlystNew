import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Input, Progress, Space, Table, Typography } from 'antd';
import { api, ApiError, type ETFTrainingList, type ETFTrainingListScope, type ETFTrainingPreparationInput, type ETFTrainingPreparationRead } from '../api/client';
import { ETF_HISTORY_FOCUS_EVENT, openETFTrainingChecks, readETFTrainingSelection, type ETFHistoryFocus } from '../utils/etfTrainingNavigation';

const requestKey = 'etf-training-history:pending:v1';
const draftKey = 'etf-training-history:input:v1';
const active = (value: ETFTrainingPreparationRead | null) => !!value?.task && ['queued', 'running'].includes(value.task.status);
type Receipt = { request: ETFTrainingPreparationInput; list_sha256: string };
type InputDraft = { version: 1; list_sha256: string; codes: string; start: string; end: string };

function readInputDraft(): InputDraft | null {
  const raw = localStorage.getItem(draftKey);
  if (raw === null) return null;
  const value = JSON.parse(raw);
  if (value?.version !== 1 || !/^[a-f0-9]{64}$/.test(value.list_sha256)
      || typeof value.codes !== 'string' || value.codes.length > 2000
      || typeof value.start !== 'string' || !/^(\d{4}-\d{2}-\d{2})?$/.test(value.start)
      || typeof value.end !== 'string' || !/^(\d{4}-\d{2}-\d{2})?$/.test(value.end)) throw new Error();
  return value;
}
type HistoryRow = Pick<ETFTrainingListScope['symbols'][number], 'symbol' | 'name' | 'row_count' | 'missing_trade_dates' | 'reason'>;
function savedRequest(): Receipt | null {
  const text = localStorage.getItem(requestKey);
  if (!text) return null;
  const saved = JSON.parse(text);
  if (!saved?.request || !/^[a-f0-9]{64}$/.test(saved.list_sha256)
      || !/^[a-f0-9-]{32,36}$/.test(saved.request.request_id)
      || !/^[a-f0-9]{64}$/.test(saved.request.expected_scope_sha256)
      || !/^\d{4}-\d{2}-\d{2}$/.test(saved.request.start_date) || !/^\d{4}-\d{2}-\d{2}$/.test(saved.request.end_date)) {
    throw new Error('本地保存的准备记录不完整，暂不能确认上次任务，请先查看更新记录。');
  }
  return saved;
}
function checkScope(value: ETFTrainingListScope) {
  if (!value || !Array.isArray(value.symbols) || !/^[a-f0-9]{64}$/.test(value.list_sha256)
      || !Array.isArray(value.errors) || !Array.isArray(value.reasons)) throw new Error();
  return value;
}

function MissingDates({ values }: { values: string[] | null }) {
  if (values === null) return <span>暂不能确认</span>;
  if (!values.length) return <span>所查范围没有缺失日期</span>;
  return <><span>{values.slice(0, 5).join('、')}</span>{values.length > 5 && <details>
    <summary>查看全部 {values.length} 个缺失日期</summary>{values.join('、')}</details>}</>;
}

export default function ETFTrainingHistoryPanel() {
  const [scope, setScope] = useState<ETFTrainingListScope | null>(null);
  const [savedList, setSavedList] = useState<ETFTrainingList | null>(null);
  const [codes, setCodes] = useState('');
  const [dates, setDates] = useState({ start: '', end: '' });
  const [reading, setReading] = useState(true), [busy, setBusy] = useState(false);
  const [checkingDates, setCheckingDates] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [draftWarning, setDraftWarning] = useState('');
  const [olderDraft, setOlderDraft] = useState<InputDraft | null>(null);
  const [focusTarget, setFocusTarget] = useState<ETFHistoryFocus | null>(null);
  const [datesOpen, setDatesOpen] = useState(false);
  const section = useRef<HTMLElement | null>(null);
  const [result, setResult] = useState<ETFTrainingPreparationRead | null>(null);
  const [unknown, setUnknown] = useState(false);
  const [pending, setPending] = useState<Receipt | null>(null);
  const [pollVersion, setPollVersion] = useState(0);
  const generation = useRef(0), working = useRef(false);
  const accept = useCallback((value: ETFTrainingPreparationRead, listSha256: string, token: number, currentListSha256 = listSha256) => {
    if (generation.current !== token) return;
    if (!value.task || value.report && (value.report.scope_kind !== 'training_list' || value.report.list_sha256 !== listSha256)) {
      throw new Error('历史数据准备记录与这份ETF清单不一致，请重新读取。');
    }
    if (listSha256 !== currentListSha256) {
      setResult(null); setError('');
      if (active(value)) {
        setUnknown(true);
        setNotice('上次清单的历史数据还在准备，请稍后读取进度。当前清单尚未检查。');
      } else {
        setUnknown(false); setPending(null);
        try { localStorage.removeItem(requestKey); } catch { /* The same finished request can be read again. */ }
        setNotice('上次任务已结束，训练清单后来有修改，请按当前清单重新检查。');
      }
      return;
    }
    setResult(value); setUnknown(false); setPending(null);
    try { localStorage.removeItem(requestKey); } catch { /* A retained receipt will recover this same task next time. */ }
    if (value.report) setDates({ start: value.report.start_date, end: value.report.end_date });
    else if (value.task.payload_json) {
      const payload = JSON.parse(value.task.payload_json);
      if (payload.scope_kind === 'training_list' && payload.list_sha256 === listSha256)
        setDates({ start: payload.start_date, end: payload.end_date });
    }
  }, []);
  const load = useCallback(async () => {
    const token = ++generation.current;
    setReading(true); setError(''); setNotice(''); setDraftWarning(''); setOlderDraft(null);
    setScope(null); setSavedList(null); setResult(null); setUnknown(false); setPending(null);
    let previous: Receipt | null = null;
    let draft: InputDraft | null = null;
    let taskActive = false;
    let storageRead = false;
    try {
      previous = savedRequest();
      storageRead = true;
      if (previous) { setPending(previous); setUnknown(true); }
      else {
        try { draft = readInputDraft(); }
        catch { setDraftWarning('之前未保存的输入暂时读不到，当前显示已保存清单。'); }
      }
      const draftDates = draft && draft.start && draft.end && draft.start <= draft.end ? { start: draft.start, end: draft.end } : undefined;
      let value: ETFTrainingListScope;
      let rejectedDraftDates = false;
      try { value = checkScope(await api.getETFTrainingListScope(draftDates)); }
      catch (failure) {
        if (!draftDates || !(failure instanceof ApiError) || ![400, 422].includes(failure.status_code || 0)) throw failure;
        if (generation.current !== token) return;
        value = checkScope(await api.getETFTrainingListScope());
        rejectedDraftDates = true;
      }
      if (generation.current !== token) return;
      if (rejectedDraftDates) setDraftWarning('上次填写的日期未通过检查，请调整后重新检查。');
      setScope(value); setCodes(value.symbols.map(row => row.symbol).join('\n'));
      setDates({ start: value.start_date, end: value.end_date });
      if (previous) {
        if (previous.list_sha256 === value.list_sha256)
          setDates({ start: previous.request.start_date, end: previous.request.end_date });
        const recovered = await api.findETFTrainingPreparation(previous.request.request_id);
        if (generation.current !== token) return;
        if (recovered.task) accept(recovered, previous.list_sha256, token, value.list_sha256);
        else if (recovered.report === null) { setUnknown(false); setError('还没有查到这次准备，可以重试准备，已保留原来的日期和请求。'); }
        else throw new Error();
      } else if (value.latest_task) {
        setResult({ task: value.latest_task, report: null });
        const recovered = await api.getETFTrainingPreparation(value.latest_task.id);
        if (generation.current !== token) return;
        taskActive = active(recovered);
        accept(recovered, value.list_sha256, token);
      }
      if (draft && !taskActive) {
        if (draft.list_sha256 === value.list_sha256) {
          setCodes(draft.codes); setDates({ start: draft.start, end: draft.end });
          setNotice('已恢复上次填写的内容。');
        } else {
          setOlderDraft(draft);
          setNotice('其他页面已修改清单，当前显示已保存内容。之前未保存的输入保留在下方。');
        }
      }
    } catch (failure) { if (generation.current === token) {
      setUnknown(!storageRead || !!previous);
      setError(!storageRead ? '无法读取本地准备记录，请恢复浏览器的本地存储后重试。确认上次任务前不能再次准备。'
        : previous ? 'ETF训练准备记录暂时读不到，请重试读取进度。'
        : 'ETF训练清单或准备记录暂时读不到，请点“读取已保存清单”重试。');
    } }
    finally { if (generation.current === token) setReading(false); }
  }, [accept]);
  useEffect(() => { void load(); return () => { ++generation.current; }; }, [load]);
  const currentList = savedList || scope;
  const dirty = !!currentList && codes !== currentList.symbols.map(row => row.symbol).join('\n');
  const running = active(result), task = result?.task;
  const report = result?.report?.list_sha256 === scope?.list_sha256 ? result?.report : null;
  const checkedDates = report || scope;
  const dateDirty = !!savedList || !!checkedDates && (dates.start !== checkedDates.start_date || dates.end !== checkedDates.end_date);
  const inputsDirty = dirty || dateDirty;
  useEffect(() => {
    const receive = (event?: Event) => {
      try {
        const target = event instanceof CustomEvent ? event.detail : localStorage.getItem(ETF_HISTORY_FOCUS_EVENT);
        if (target === 'codes' || target === 'dates') setFocusTarget(target);
      } catch { /* The page remains usable when a focus preference cannot be read. */ }
    };
    receive();
    window.addEventListener(ETF_HISTORY_FOCUS_EVENT, receive);
    return () => window.removeEventListener(ETF_HISTORY_FOCUS_EVENT, receive);
  }, []);
  useEffect(() => {
    if (!focusTarget || !scope || reading || busy || running || unknown || pending) return;
    if (focusTarget === 'dates' && !datesOpen) { setDatesOpen(true); return; }
    const input = section.current?.querySelector<HTMLTextAreaElement | HTMLInputElement>(focusTarget === 'codes'
      ? 'textarea[aria-label="ETF训练代码"]' : 'input[aria-label="训练历史结束日期"]');
    if (!input || input.disabled) return;
    section.current?.scrollIntoView?.({ block: 'start' });
    input.focus();
    if (document.activeElement !== input) return;
    try { localStorage.removeItem(ETF_HISTORY_FOCUS_EVENT); } catch { /* Repeating focus is harmless. */ }
    setFocusTarget(null);
  }, [focusTarget, scope, reading, busy, running, unknown, pending, datesOpen]);
  const keepInputs = (listSha256: string, nextCodes: string, nextDates: { start: string; end: string }) => {
    try {
      localStorage.setItem(draftKey, JSON.stringify({ version: 1, list_sha256: listSha256, codes: nextCodes, ...nextDates }));
      setDraftWarning('');
    } catch { setDraftWarning('当前输入暂时无法保存在本机，请保存清单后再离开页面。'); }
  };
  const editInputs = (nextCodes: string, nextDates: { start: string; end: string }) => {
    setCodes(nextCodes); setDates(nextDates); setOlderDraft(null); setNotice('');
    if (currentList) keepInputs(currentList.list_sha256, nextCodes, nextDates);
  };
  useEffect(() => {
    if (!task || !running || !scope || reading) return;
    const token = generation.current, listSha256 = scope.list_sha256;
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const value = await api.getETFTrainingPreparation(task.id);
        if (stopped || generation.current !== token) return;
        accept(value, listSha256, token); setError('');
        if (active(value)) timer = setTimeout(() => void poll(), 1500);
        else if (value.report?.prepared) {
          const updated = await api.getETFTrainingListScope({ start: value.report.start_date, end: value.report.end_date });
          if (!stopped && generation.current === token) setScope(updated);
        }
      } catch { if (!stopped && generation.current === token) setError('准备进度读取失败，任务可能仍在运行。请重试读取进度。'); }
    };
    void poll();
    return () => { stopped = true; if (timer) clearTimeout(timer); };
  }, [task?.id, task?.status, running, reading, scope?.list_sha256, pollVersion, accept]);
  const save = async () => {
    if (!scope || working.current || running || unknown) return;
    const token = generation.current;
    working.current = true; setBusy(true); setError('');
    let acknowledged = false;
    try {
      const saved = await api.saveETFTrainingList(codes, currentList!.list_sha256);
      if (generation.current !== token) return;
      if (!Array.isArray(saved.symbols) || !/^[a-f0-9]{64}$/.test(saved.list_sha256)) throw new Error();
      acknowledged = true;
      setSavedList(saved); setCodes(saved.symbols.map(row => row.symbol).join('\n'));
      keepInputs(saved.list_sha256, saved.symbols.map(row => row.symbol).join('\n'), dates);
      setResult(null); setNotice('清单已保存。');
      const value = checkScope(await api.getETFTrainingListScope(dates));
      if (generation.current !== token) return;
      if (value.list_sha256 !== saved.list_sha256) throw new Error();
      setScope(value); setSavedList(null);
    } catch (failure) {
      if (generation.current === token) setError(acknowledged ? '清单已保存，日期检查未完成，请重试检查。'
        : failure instanceof ApiError ? failure.message : '清单保存结果暂时无法确认，请读取已保存清单后重试。');
    } finally { if (generation.current === token) { working.current = false; setBusy(false); } }
  };
  const prepare = async () => {
    if (!scope || working.current || running || dirty || dateDirty || unknown) return;
    const token = generation.current;
    working.current = true; setBusy(true); setError('');
    try {
      const checked = checkScope(await api.getETFTrainingListScope(dates));
      if (generation.current !== token) return;
      if (checked.list_sha256 !== scope.list_sha256) throw new Error('ETF训练清单已修改，请读取已保存清单后重试。');
      setScope(checked);
      const payload: ETFTrainingPreparationInput = pending?.request ?? { request_id: crypto.randomUUID(), start_date: dates.start,
        end_date: dates.end, expected_scope_sha256: checked.scope_sha256 };
      localStorage.setItem(requestKey, JSON.stringify({ request: payload, list_sha256: scope.list_sha256 }));
      setPending({ request: payload, list_sha256: scope.list_sha256 });
      setResult(null);
      try { accept(await api.startETFTrainingPreparation(payload), scope.list_sha256, token); }
      catch (failure) {
        if (generation.current !== token) return;
        if (failure instanceof ApiError && failure.status_code && failure.status_code >= 400 && failure.status_code < 500 && failure.status_code !== 408) {
          localStorage.removeItem(requestKey); setPending(null); setError(failure.message);
        } else { setUnknown(true); setError('尚未确认准备是否开始，请先重试读取进度。'); }
      }
    } catch (failure) {
      if (generation.current === token) setError(failure instanceof Error && /[\u3400-\u9fff]/.test(failure.message)
        ? failure.message : '清单或日期检查失败，请重新读取后重试。');
    } finally { if (generation.current === token) { working.current = false; setBusy(false); } }
  };
  const checkDates = async () => {
    if (!scope || working.current || running || dirty || unknown || pending) return;
    const token = generation.current;
    working.current = true; setBusy(true); setCheckingDates(true); setError('');
    try {
      const checked = checkScope(await api.getETFTrainingListScope(dates));
      if (generation.current !== token) return;
      if (checked.list_sha256 !== currentList!.list_sha256) throw new Error('ETF训练清单已修改，请读取已保存清单后重试。');
      setScope(checked); setSavedList(null); setResult(null); setNotice('');
      keepInputs(checked.list_sha256, codes, dates);
    } catch (failure) {
      if (generation.current === token) setError(savedList ? '清单已保存，日期检查未完成，请重试检查。'
        : failure instanceof Error && /[\u3400-\u9fff]/.test(failure.message)
        ? failure.message : '日期覆盖暂时查不到，请重试检查。');
    } finally { if (generation.current === token) { working.current = false; setBusy(false); setCheckingDates(false); } }
  };
  const recover = async () => {
    if (working.current) return;
    if (!scope || !pending && !task) { void load(); return; }
    const token = generation.current;
    working.current = true; setBusy(true);
    try {
      const value = pending ? await api.findETFTrainingPreparation(pending.request.request_id) : await api.getETFTrainingPreparation(task!.id);
      if (generation.current !== token) return;
      if (value.task) { accept(value, pending?.list_sha256 || scope.list_sha256, token, scope.list_sha256); setError(''); setPollVersion(value => value + 1); }
      else if (pending && value.report === null) { setUnknown(false); setError('还没有查到这次准备，可以重试准备，已保留原来的日期和请求。'); }
      else throw new Error();
    } catch { if (generation.current === token) setError('准备记录仍未读到，请稍后重试读取进度。'); }
    finally { if (generation.current === token) { working.current = false; setBusy(false); } }
  };
  const coverage = inputsDirty ? null : report || scope;
  const expectedDates = report?.expected_trade_date_count;
  const datesKnown = typeof expectedDates === 'number' && Number.isInteger(expectedDates) && expectedDates > 0;
  const datesComplete = (row: HistoryRow) => datesKnown && row.row_count === expectedDates
    && Array.isArray(row.missing_trade_dates) && row.missing_trade_dates.length === 0;
  const needsAttention = (row: HistoryRow) => !datesComplete(row) || !!row.reason && !report?.errors.includes(row.reason);
  const rows: HistoryRow[] = inputsDirty ? [] : report ? report.symbols.map(row => ({ ...row, reason: row.error }))
    .sort((a, b) => Number(needsAttention(b)) - Number(needsAttention(a))) : scope?.symbols || [];
  const completeDates = rows.filter(datesComplete).length;
  const resultSummary = report && !running && !inputsDirty && !unknown
    ? `本次共${rows.length}只ETF，${completeDates}只行情日期齐全${completeDates < rows.length
      ? `，${rows.length - completeDates}只日期需补齐或核实。` : report.prepared ? '。' : '；整份资料仍未核对通过。'}` : null;
  const rowReason = (row: HistoryRow) => {
    if (!report) return row.reason || '所查日期已核对';
    if (!datesComplete(row)) return row.reason || '行情日期暂不能确认，请查看缺失日期和准备结果。';
    if (row.reason && !report.errors.includes(row.reason)) return row.reason;
    return report.prepared ? '所查日期已核对' : '行情日期齐全，整份资料仍需核对。';
  };
  const trainingDataset = report ? report.prepared ? report.training : null : scope?.training_dataset;
  const viewTrainingChecks = () => {
    try {
      if (!trainingDataset || !scope) throw new Error();
      const { dataset_id, manifest_sha256, start_date, end_date, trade_date_count, symbol_count } = trainingDataset;
      const selected = readETFTrainingSelection(JSON.stringify({ version: 1, selection: {
        dataset_id, manifest_sha256, start_date, end_date, trade_date_count, symbol_count, list_sha256: scope.list_sha256,
      } }));
      if (!selected) throw new Error();
      openETFTrainingChecks(selected);
    } catch { setError('本次资料记录不完整，请先读取已保存清单，仍无法读取时重新准备历史数据。'); }
  };

  return <section ref={section} aria-label="ETF训练清单和历史数据" style={{ padding: 16, marginBottom: 20, border: '1px solid var(--pt-border, #303846)', borderRadius: 8 }}>
    <Typography.Title level={5} style={{ marginTop: 0 }}>ETF训练清单和历史数据</Typography.Title>
    <Typography.Paragraph type="secondary">训练用的ETF单独保存在这里，不随顶部组合切换。填写代码后准备历史数据，再检查共同日期和缺失原因。</Typography.Paragraph>
    {reading && <p role="status">正在读取ETF训练清单…</p>}
    {checkingDates && <p role="status">正在检查本地已有行情的日期…</p>}
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 8 }} />}
    {notice && <Alert type="info" showIcon message={notice} style={{ marginBottom: 8 }} />}
    {draftWarning && <Alert type="warning" showIcon message={draftWarning} style={{ marginBottom: 8 }} />}
    {olderDraft && <details style={{ marginBottom: 8 }}><summary>查看之前未保存的内容</summary>
      <p>原填写日期：{olderDraft.start || '未填写'} 至 {olderDraft.end || '未填写'}</p>
      <pre style={{ whiteSpace: 'pre-wrap' }}>{olderDraft.codes}</pre>
    </details>}
    <label>ETF代码 <Input.TextArea aria-label="ETF训练代码" value={codes} rows={3} maxLength={2000}
      placeholder="粘贴六位ETF代码，用空格、逗号或换行分开" disabled={reading || busy || running || unknown || !!pending || !scope} onChange={event => editInputs(event.target.value, dates)} /></label>
    <Typography.Paragraph type="secondary" style={{ marginTop: 6 }}>可保存最多{scope?.max_symbols ?? 50}只ETF，重复代码只保留一次。训练仍需至少20只ETF、共同覆盖至少274个交易日。</Typography.Paragraph>
    <Space wrap style={{ marginBottom: 12 }}>
      <Button type={dirty ? 'primary' : 'default'} onClick={() => void save()} loading={busy && !checkingDates} disabled={reading || running || unknown || !scope || !dirty}>保存清单并检查</Button>
      {savedList && <Button type="primary" loading={checkingDates} disabled={reading || busy || running || unknown || dirty || !!pending}
        onClick={() => void checkDates()}>重试检查</Button>}
      <Button type={!dirty && !dateDirty ? 'primary' : 'default'} loading={busy && !checkingDates} disabled={reading || busy || running || unknown || dirty || dateDirty || !scope?.can_prepare || !dates.start || !dates.end || dates.start > dates.end}
        onClick={() => void prepare()}>{task && !running && task.status !== 'done' || pending && !unknown ? '重试准备' : '准备历史数据'}</Button>
      {(unknown || error && task) && <Button type="primary" disabled={busy || reading} onClick={() => void recover()}>重试读取进度</Button>}
      <Button disabled={reading || busy || running || unknown} onClick={() => void load()}>读取已保存清单</Button>
      {scope && scope.symbol_count > 0 && (scope.dataset_id || report?.prepared) && <Button
        disabled={reading || busy || running || unknown || dirty || dateDirty || !!pending}
        onClick={viewTrainingChecks}>查看训练条件</Button>}
    </Space>
    {dirty && <p>代码已修改，请先保存清单。</p>}
    {scope && <>
      {!inputsDirty && <p>{report ? report.prepared ? report.training?.training_message || '历史行情已核对，训练前还会检查清单数量、日期和所选特征。'
        : '本次历史数据没有准备完整，请查看缺失日期和原因后重试。' : scope.training_message}</p>}
      {dateDirty && !savedList && !running && !unknown && !pending && <p>{dirty ? '日期已修改，保存清单后会一并检查日期。' : '日期已修改，请先检查日期覆盖。'}</p>}
      {!inputsDirty && <p>{coverage?.common_trade_date_count == null ? '共同覆盖日期暂不能确认，准备历史数据后再检查。'
        : `共同覆盖${coverage.common_trade_date_count}个交易日：${coverage.common_start_date || '无'} 至 ${coverage.common_end_date || '无'}。所查范围共${coverage.expected_trade_date_count ?? '尚未核实的'}个交易日。`}</p>
      }
      {!inputsDirty && scope.errors.map(message => <Alert key={message} type="warning" message={message} />)}
      <details open={datesOpen} onToggle={event => setDatesOpen(event.currentTarget.open)} style={{ marginBottom: 12 }}><summary>调整准备日期</summary><Space wrap style={{ marginTop: 8 }}>
        <label>开始日期 <Input type="date" aria-label="训练历史开始日期" value={dates.start} disabled={busy || running || unknown || !!pending} onChange={event => editInputs(codes, { ...dates, start: event.target.value })} /></label>
        <label>结束日期 <Input type="date" aria-label="训练历史结束日期" value={dates.end} disabled={busy || running || unknown || !!pending} onChange={event => editInputs(codes, { ...dates, end: event.target.value })} /></label>
        {dateDirty && !savedList && <Button type="primary" loading={checkingDates} disabled={reading || running || unknown || !!pending || dirty || !dates.start || !dates.end || dates.start > dates.end}
          onClick={() => void checkDates()}>检查日期覆盖</Button>}
      </Space></details>
      {task && (running || !inputsDirty) && <div role="status"><Alert showIcon type={running ? 'info' : report?.prepared ? 'success' : 'warning'} message={report?.prepared ? '这份清单的历史数据已准备并核对。' : task.message} />
        {running && <Progress aria-label="ETF训练历史准备进度" percent={Math.round(task.percent)} />}</div>}
      {resultSummary && <p role="status" aria-label="ETF准备结果汇总">{resultSummary}</p>}
      {!inputsDirty && report?.errors.map(message => <Typography.Paragraph type="danger" key={message}>{message}</Typography.Paragraph>)}
      {!inputsDirty && report && <p>本次检查日期：{report.start_date} 至 {report.end_date}</p>}
      {!inputsDirty && trainingDataset && <p>训练采用整份已核对资料：{trainingDataset.start_date} 至 {trainingDataset.end_date}。</p>}
      {!!rows.length && <Table<HistoryRow> size="small" rowKey="symbol" pagination={false} scroll={{ x: 760 }} dataSource={rows} columns={[
        { title: 'ETF代码', dataIndex: 'symbol', width: 100 },
        { title: report ? '本次取得' : '已核对行情', render: (_, row) => `${row.row_count}天`, width: 110 },
        { title: '缺失日期', render: (_, row) => <MissingDates values={report && !datesKnown ? null : row.missing_trade_dates} /> },
        { title: '原因', render: (_, row) => rowReason(row) },
      ]} />}
    </>}
    <Typography.Paragraph type="secondary" style={{ margin: '10px 0 0' }}>这里只准备历史数据，不会训练新模型或改动组合。历史停牌、ST和逐日涨跌停资料不全时，成交回测仍不能运行。</Typography.Paragraph>
  </section>;
}
