import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Descriptions, Input, Modal, Select, Space, Spin, Typography } from 'antd';
import { api, ApiError, type FactorPipelineTask, type FactorSet, type ETFTrainingOptions, type ETFTrainingRequest, type ETFTrainingSelection } from '../../api/client';
import { openETFHistory } from '../../utils/etfTrainingNavigation';
import { clearETFTrainingRequest, readETFTrainingRequest, sameETFTrainingRequest, saveETFTrainingRequest } from '../../utils/etfTrainingRequest';
import { enumLabel } from '../../i18n';

export { openETFHistory } from '../../utils/etfTrainingNavigation';

export default function ETFTrainingModal({ open, factorSets, initialFactorSetId, historySelection, historySelectionError, onClose, onSubmitted, onPrepared }: {
  open: boolean; factorSets: FactorSet[]; initialFactorSetId?: string;
  historySelection?: ETFTrainingSelection; historySelectionError?: string;
  onClose: () => void; onSubmitted: (task: FactorPipelineTask) => void;
  onPrepared?: (factorSet: FactorSet) => void;
}) {
  const [factorSetId, setFactorSetId] = useState('');
  const [options, setOptions] = useState<ETFTrainingOptions | null>(null);
  const [datasetId, setDatasetId] = useState('');
  const [reading, setReading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [pending, setPending] = useState<ETFTrainingRequest | null>(null);
  const [recoveredTask, setRecoveredTask] = useState<FactorPipelineTask | null>(null);
  const [recoveryState, setRecoveryState] = useState<'checking' | 'found' | 'not_found' | 'unknown' | null>(null);
  const [storageBlocked, setStorageBlocked] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [note, setNote] = useState('');
  const [preparedSet, setPreparedSet] = useState<FactorSet | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [preparationError, setPreparationError] = useState('');
  const [prepareRevision, setPrepareRevision] = useState(0);
  const prepareGeneration = useRef(0);
  const generation = useRef(0), inFlight = useRef(false);
  const allSets = [...(preparedSet ? [preparedSet] : []), ...factorSets];
  const frozen = allSets.filter((item, index) => item.status === 'frozen'
    && (item.asset_type === 'etf' || item.id === initialFactorSetId || item.id === pending?.factor_set_id)
    && allSets.findIndex(value => value.id === item.id) === index);
  const storageError = () => {
    setStorageBlocked(true);
    setError('无法保存或读取这次训练的记录，请先恢复浏览器的本地存储，再重新打开。为避免重复训练，本次暂停提交。');
  };
  const recover = async (request: ETFTrainingRequest, token: number, rejectedMessage?: string) => {
    setRecoveryState('checking'); setRecoveredTask(null);
    try {
      const value = await api.scoringRecoverETFTraining(request.request_id);
      if (generation.current !== token) return;
      if (value.request_id !== request.request_id || !['found', 'not_found'].includes(value.state)) throw new Error();
      if (value.state === 'found') {
        if (!value.request || !sameETFTrainingRequest(request, value.request) || !value.task?.id
            || !['queued', 'running', 'done', 'partial', 'failed', 'cancelled'].includes(value.task.status)) throw new Error();
        setRecoveredTask(value.task); setRecoveryState('found'); setError('');
      } else {
        if (value.task !== null || value.request !== null) throw new Error();
        if (rejectedMessage) {
          clearETFTrainingRequest(request); setPending(null); setRecoveryState(null); setError(rejectedMessage);
        } else {
          setRecoveryState('not_found'); setError('还没有查到这次训练，可以用原编号重试。训练参数已保留。');
        }
      }
    } catch {
      if (generation.current === token) {
        setRecoveryState('unknown'); setError('暂时无法确认这次训练的结果，请重新读取。确认前不会再次提交。');
      }
    }
  };
  useEffect(() => {
    const token = ++prepareGeneration.current;
    setFactorSetId(''); setPreparedSet(null); setNote(''); setPreparationError(''); setPreparing(false);
    if (!open) return;
    try {
      const previous = readETFTrainingRequest();
      if (previous) { setFactorSetId(previous.factor_set_id); return; }
    } catch { storageError(); return; }
    if (initialFactorSetId) { setFactorSetId(initialFactorSetId); return; }
    setPreparing(true);
    void Promise.resolve().then(() => api.scoringPrepareETFTrainingSet()).then(value => {
      if (prepareGeneration.current !== token) return;
      if (!value?.id || value.asset_type !== 'etf' || value.status !== 'frozen' || !value.members?.length
          || value.members.some(member => member.role !== 'feature' || member.missing_policy !== 'exclude' || member.version_trainable !== true)) throw new Error();
      setPreparedSet(value); setFactorSetId(value.id); onPrepared?.(value);
    }).catch(failure => {
      if (prepareGeneration.current === token) setPreparationError(failure instanceof ApiError && /[\u3400-\u9fff]/.test(failure.message)
        ? failure.message : 'ETF价量特征准备失败，请重新检查。准备完成前不会提交训练。');
    }).finally(() => { if (prepareGeneration.current === token) setPreparing(false); });
    return () => { ++prepareGeneration.current; };
  }, [open, initialFactorSetId, prepareRevision]);
  useEffect(() => {
    const token = ++generation.current;
    setOptions(null); setDatasetId(''); setError(''); setPending(null); setRecoveredTask(null);
    setRecoveryState(null); setStorageBlocked(false); setReading(false);
    if (!open) return;
    try {
      const previous = readETFTrainingRequest();
      if (previous) {
        setPending(previous); setNote(previous.note); void recover(previous, token);
        return () => { ++generation.current; };
      }
    } catch {
      storageError(); return () => { ++generation.current; };
    }
    if (historySelectionError) { setError(historySelectionError); return () => { ++generation.current; }; }
    if (!factorSetId || preparing) return () => { ++generation.current; };
    setReading(true);
    void Promise.resolve().then(() => api.getETFTrainingOptions(factorSetId, true, historySelection)).then(value => {
      if (generation.current !== token) return;
      if (value.factor_set_id !== factorSetId || !Array.isArray(value.items)) throw new Error('训练检查结果不属于当前集合');
      if (historySelection) {
        const selected = value.items.find(item => item.dataset_id === historySelection.dataset_id);
        if (value.list_sha256 !== historySelection.list_sha256 || !selected
            || ['manifest_sha256', 'start_date', 'end_date', 'trade_date_count', 'symbol_count'].some(
              key => selected[key as keyof typeof selected] !== historySelection[key as keyof ETFTrainingSelection])) {
          throw new Error('本次准备的资料或训练清单已有变化，请回到历史数据页重新检查。');
        }
        setOptions({ ...value, items: [selected] }); setDatasetId(selected.dataset_id);
      } else { setOptions(value); setDatasetId(value.items[0]?.dataset_id || ''); }
    }).catch(failure => {
      if (generation.current === token) setError(failure instanceof Error && /[\u3400-\u9fff]/.test(failure.message)
        ? failure.message : 'ETF历史资料读取失败，请重新检查。');
    }).finally(() => { if (generation.current === token) setReading(false); });
    return () => { ++generation.current; };
  }, [open, factorSetId, revision, preparing, historySelection, historySelectionError]);
  const chosen = options?.factor_set_id === factorSetId ? options.items.find(item => item.dataset_id === datasetId) : null;
  const ready = !!chosen && options?.factor_ready === true && chosen.training_ready === true && chosen.feature_ready === true
    && chosen.symbol_count >= chosen.minimum_symbols && chosen.trade_date_count >= 274 && /^[a-f0-9]{64}$/.test(chosen.manifest_sha256);
  const listCount = options?.list_symbol_count;
  const missingSymbols = listCount === undefined ? 0 : Math.max(0, (options?.minimum_symbols ?? 20) - listCount);
  const missingDates = !missingSymbols && chosen ? Math.max(0, 274 - chosen.trade_date_count) : 0;
  const historyAction = listCount === 0 ? '填写ETF代码' : missingSymbols ? '补充ETF代码' : missingDates ? '调整准备日期' : '补充ETF历史数据';
  const submit = async () => {
    if (inFlight.current || storageBlocked || preparing || preparationError || recoveryState === 'checking' || recoveryState === 'unknown') return;
    if (recoveredTask) { onSubmitted(recoveredTask); return; }
    if (pending ? recoveryState !== 'not_found' : !ready || !chosen) return;
    const token = generation.current;
    let request: ETFTrainingRequest;
    try {
      const previous = readETFTrainingRequest();
      if (previous && (!pending || !sameETFTrainingRequest(previous, pending))) {
        setPending(previous); setNote(previous.note); void recover(previous, token); return;
      }
      if (pending && !previous) throw new Error();
      request = previous ?? { request_id: crypto.randomUUID().replace(/-/g, ''), factor_set_id: factorSetId,
        dataset_id: chosen!.dataset_id, manifest_sha256: chosen!.manifest_sha256, note: note.trim() };
      saveETFTrainingRequest(request); setPending(request); setRecoveredTask(null);
    } catch { storageError(); return; }
    inFlight.current = true; setSubmitting(true); setError('');
    try {
      const task = await api.scoringSubmitETFTraining(request);
      if (!task?.id) throw new Error();
      if (generation.current === token) onSubmitted(task);
    } catch (failure) {
      if (generation.current !== token) return;
      const rejectedMessage = failure instanceof ApiError && [400, 422].includes(failure.status_code || 0) ? failure.message : undefined;
      await recover(request, token, rejectedMessage);
    } finally { inFlight.current = false; setSubmitting(false); }
  };
  return <Modal open={open} title="ETF训练" onCancel={() => { if (!inFlight.current) onClose(); }} onOk={() => void submit()}
    okText={recoveredTask ? '查看这次训练' : pending ? '重试同一次训练' : '提交ETF训练'} cancelText="关闭" confirmLoading={submitting}
    okButtonProps={{ disabled: preparing || !!preparationError || reading || storageBlocked || (pending ? !['found', 'not_found'].includes(recoveryState || '') : !ready) }}
    cancelButtonProps={{ disabled: submitting }} maskClosable={!submitting} closable={!submitting} width={650}>
    <Typography.Paragraph>使用已核对的ETF历史价格和成交量。沿用250天训练、50天验证，训练只生成待检查的模型，不会自动启用。</Typography.Paragraph>
    {pending && <Alert type={recoveredTask ? 'success' : 'warning'} showIcon
      message={recoveredTask ? '已找到这次训练，不会重复提交。' : '这次训练的提交记录已保留。'}
      description={recoveredTask ? `任务状态：${enumLabel('taskStatus', recoveredTask.status)}。可以查看这次训练。` : '关闭弹窗或重新进入后，会继续查找同一次训练。'} />}
    {recoveryState === 'checking' && <p role="status"><Spin size="small" /> 正在读取这次训练的结果…</p>}
    {preparing && <p role="status"><Spin size="small" /> 正在准备ETF价量特征…</p>}
    {preparationError && <Alert type="error" showIcon message={preparationError} />}
    {!pending && !preparing && !preparationError && (!frozen.length ? <Alert type="warning" message="还没有可训练的特征集合，请先添加价量特征并冻结集合。" />
      : <label>训练特征 <Select aria-label="ETF训练特征集合" value={factorSetId || undefined} disabled={submitting || storageBlocked} style={{ width: '100%', margin: '6px 0 12px' }}
        options={frozen.map(item => ({ value: item.id, label: item.name || item.id }))} onChange={setFactorSetId} /></label>)}
    {reading && <p role="status"><Spin size="small" /> 正在检查ETF历史资料和训练特征…</p>}
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 10 }} />}
    {options?.errors.map(message => <Alert key={message} type="warning" message={message} />)}
    {options?.list_message && <Alert type={options.items.length ? 'info' : 'warning'} message={options.list_message} style={{ marginBottom: 10 }} />}
    {!pending && !!missingSymbols && listCount !== 0 && <p>还差{missingSymbols}只ETF，请先补充代码。</p>}
    {!pending && !!missingDates && <p>还差{missingDates}个交易日，请调整准备日期后重新检查。</p>}
    {options && !options.items.length && !options.list_message && <Alert type="warning" message="还没有核对完成的ETF历史资料，请先补充ETF历史数据。" />}
    {options && options.items.length > 1 && <label>历史资料 <Select aria-label="ETF训练历史资料" style={{ width: '100%', marginBottom: 12 }} value={datasetId}
      disabled={submitting || !!pending} onChange={setDatasetId} options={options.items.map(item => ({ value: item.dataset_id,
        label: `${item.symbol_count}只ETF · ${item.start_date} 至 ${item.end_date}` }))} /></label>}
    {chosen && <>
      {historySelection && <Typography.Paragraph type="secondary">使用历史数据页核对的这份资料，训练采用以下全部日期。</Typography.Paragraph>}
      <Descriptions size="small" bordered column={1} style={{ marginBottom: 12 }}>
        <Descriptions.Item label="资料日期">{chosen.start_date} 至 {chosen.end_date}</Descriptions.Item>
        <Descriptions.Item label="已有资料">{chosen.symbol_count}只ETF · {chosen.trade_date_count}个交易日</Descriptions.Item>
      </Descriptions>
      <details style={{ marginBottom: 12 }}><summary>查看资料记录</summary>
        <p>资料编号：{chosen.dataset_id}</p><p style={{ overflowWrap: 'anywhere' }}>文件核对记录：{chosen.manifest_sha256}</p>
      </details>
      <Alert type={ready ? 'success' : 'warning'} showIcon message={chosen.training_message} style={{ marginBottom: 10 }} />
      <Typography.Paragraph type={chosen.feature_ready ? 'secondary' : 'danger'}>{chosen.feature_message}</Typography.Paragraph>
      <Input.TextArea aria-label="ETF训练备注" placeholder="备注（可选）" value={note} disabled={submitting || !!pending} maxLength={300} rows={2} onChange={e => setNote(e.target.value)} />
    </>}
    <Space wrap style={{ marginTop: 12 }}>
      {!pending && <Button type={missingSymbols || missingDates ? 'primary' : 'default'} disabled={submitting || reading || preparing}
        onClick={() => { onClose(); openETFHistory(missingDates ? 'dates' : 'codes'); }}>{historyAction}</Button>}
      {!pending && (!options || !!error || !missingSymbols && !missingDates) && <Button type={error || preparationError ? 'primary' : 'default'} loading={reading || preparing} disabled={submitting}
        onClick={() => preparationError ? setPrepareRevision(v => v + 1) : setRevision(v => v + 1)}>重新检查</Button>}
      {pending && <Button type={recoveredTask ? 'default' : 'primary'} loading={recoveryState === 'checking'} disabled={submitting} onClick={() => void recover(pending, generation.current)}>重新读取训练结果</Button>}
      {pending && recoveredTask && ['done', 'partial', 'failed', 'cancelled'].includes(recoveredTask.status) && <Button disabled={submitting} onClick={() => {
        try {
          clearETFTrainingRequest(pending); setPending(null); setRecoveredTask(null); setRecoveryState(null);
          setOptions(null); setNote(''); setError(''); setFactorSetId(''); setPrepareRevision(value => value + 1);
        } catch { storageError(); }
      }}>开始新的训练</Button>}
    </Space>
    <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>缺少历史停牌和逐日涨跌停资料时，成交回测继续停止。行情补齐不会解除这项限制。</Typography.Paragraph>
  </Modal>;
}
