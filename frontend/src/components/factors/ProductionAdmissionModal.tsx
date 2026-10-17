import {useEffect,useRef,useState} from 'react';
import {Alert,Button,Descriptions,Modal,Select,Space,Spin,Steps,Table,Tag,Typography} from 'antd';
import {api} from '../../api/client';
import {t} from '../../i18n';
import type {ProductionAdmissionReport} from '../../types/productionAdmission';
import type {VerifiedInputBundle} from '../../types/inputReplay';
import ProductionCandidateStep from './ProductionCandidateStep';

export default function ProductionAdmissionModal({modelId,onClose}:{modelId:string|null;onClose:()=>void}) {
  const [report,setReport]=useState<ProductionAdmissionReport|null>(null);
  const [bundles,setBundles]=useState<VerifiedInputBundle[]>([]);
  const [bundle,setBundle]=useState<string>();
  const [step,setStep]=useState(0);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string>();
  const generation=useRef(0);
  useEffect(()=>{
    const current=++generation.current;
    setReport(null);setError(undefined);setStep(0);setBundles([]);setBundle(undefined);
    if(!modelId)return;
    setBusy(true);
    void api.listVerifiedInputBundles().then(items=>{if(current===generation.current){setBundles(items);setBundle(items[0]?.bundle_id);if(!items.length)setError(t('productionAdmission.noBundle'));}})
      .catch(reason=>{if(current===generation.current)setError(String(reason));})
      .finally(()=>{if(current===generation.current)setBusy(false);});
    return()=>{++generation.current;};
  },[modelId]);
  const load=async()=>{
    if(!modelId||!bundle)return;
    const current=++generation.current;setBusy(true);setError(undefined);setReport(null);
    try {const next=await api.getProductionAdmission(modelId,bundle);if(current===generation.current){
      if(next.model_id!==modelId||next.bundle.bundle_id!==bundle||!next.planning_only)throw new Error(t('productionAdmission.mismatch'));
      setReport(next);setStep(next.frozen_plan?2:0);
    }}catch(reason){if(current===generation.current)setError(String(reason));}finally{if(current===generation.current)setBusy(false);}
  };
  useEffect(()=>{if(modelId&&bundle)void load();},[modelId,bundle]);
  const freeze=async()=>{
    if(!modelId||!bundle||!report||busy)return;
    const reviewed=report;const current=++generation.current;setBusy(true);setError(undefined);
    try {const next=await api.freezeProductionAdmission(modelId,bundle,reviewed.identity_sha256);if(current===generation.current){
      if(next.model_id!==modelId||next.identity_sha256!==reviewed.identity_sha256||!next.frozen_plan||next.frozen_plan.is_training_or_release_certificate)throw new Error(t('productionAdmission.mismatch'));
      setReport(next);setStep(2);
    }}catch(reason){if(current===generation.current){setError(t('productionAdmission.unknownFreeze')+' '+String(reason));setReport(null);setStep(0);}}
    finally{if(current===generation.current)setBusy(false);}
  };
  return <Modal title={t('productionAdmission.title')} open={!!modelId} onCancel={()=>{if(!busy)onClose();}} width={900} footer={null}>
    <Steps current={step} size="small" style={{marginBottom:18}} items={[{title:t('productionAdmission.check')},{title:t('productionAdmission.freeze')},{title:t('productionAdmission.collect')},{title:t('productionCandidate.title')}]}/>
    <Space direction="vertical" size={14} style={{width:'100%'}}>
      <Alert type="info" showIcon message={t('productionAdmission.boundary')} description={t('productionAdmission.boundaryHelp')}/>
      {error&&<Alert type="error" showIcon message={error}/>}
      {busy&&<div role="status"><Spin/> {t('productionAdmission.loading')}</div>}
      {step===0&&<>
        <label>{t('inputReplay.bundle')}<Select aria-label={t('inputReplay.bundle')} style={{width:'100%'}} value={bundle} disabled={busy} onChange={setBundle} options={bundles.map(b=>({value:b.bundle_id,label:b.label}))}/></label>
        {report&&<>
          <Descriptions bordered size="small" column={2} items={[
            {key:'model',label:t('productionAdmission.parent'),children:report.model_id,span:2},
            {key:'observed',label:t('productionAdmission.observed'),children:report.independent_after},
            {key:'days',label:t('productionAdmission.rawDays'),children:`${report.bundle.raw_trade_dates} / 274`},
          ]}/>
          <Table<ProductionAdmissionReport['gates'][number]> rowKey="code" size="small" pagination={false} dataSource={report.gates} columns={[
            {title:t('productionAdmission.requirement'),render:(_,row)=><><strong>{t(`productionAdmission.gate.${row.code}`)}</strong><div style={{fontSize:12}}>{t(`productionAdmission.gateHelp.${row.code}`)}</div></>},
            {title:t('productionAdmission.status'),width:140,render:(_,row)=><Space direction="vertical" size={0}><Tag color={row.passed===true?'green':row.passed===false?'orange':'default'}>{t(row.passed===true?'productionAdmission.passed':row.passed===false?'productionAdmission.missing':'productionAdmission.later')}</Tag>{row.actual!=null&&<span>{row.actual} / {row.required}</span>}</Space>},
          ]}/>
        </>}
        <Space wrap><Button disabled={busy||!bundle} onClick={()=>void load()}>{t('productionAdmission.refresh')}</Button><Button type="primary" disabled={busy||!report?.can_freeze} onClick={()=>setStep(1)}>{t('productionAdmission.reviewPlan')}</Button></Space>
      </>}
      {step===1&&report&&<>
        <Alert type="warning" showIcon message={t('productionAdmission.freezeHelp')}/>
        <Descriptions bordered size="small" column={1} items={[
          {key:'boundary',label:t('productionAdmission.observed'),children:report.independent_after},
          {key:'pool',label:t('productionAdmission.pool'),children:`${report.cohort_change.parent_count} → ${report.cohort_change.planned_count} · ${report.cohort_change.added.join(', ')||'—'}`},
          {key:'window',label:t('productionAdmission.window'),children:'250 / 50 · T+1 → T+5'},
          {key:'set',label:t('productionAdmission.factorSet'),children:report.identity.factor_set_id},
          {key:'fit',label:t('productionAdmission.fit'),children:`${report.identity.fit_policy} · ${report.identity.alphas.join(', ')}`},
          {key:'fp',label:t('productionAdmission.identitySha'),children:<Typography.Text code copyable>{report.identity_sha256}</Typography.Text>},
        ]}/>
        <Space><Button disabled={busy} onClick={()=>setStep(0)}>{t('productionAdmission.back')}</Button><Button type="primary" loading={busy} onClick={()=>void freeze()}>{t('productionAdmission.confirmFreeze')}</Button></Space>
      </>}
      {step===2&&report?.frozen_plan&&<>
        <Alert type="success" showIcon message={t('productionAdmission.frozen')} description={t('productionAdmission.frozenHelp')}/>
        <Descriptions bordered size="small" column={1} items={[
          {key:'id',label:t('productionAdmission.planId'),children:<Typography.Text copyable>{report.frozen_plan.plan_id}</Typography.Text>},
          {key:'date',label:t('productionAdmission.observed'),children:report.frozen_plan.identity.observed_through},
          {key:'days',label:t('productionAdmission.independent'),children:`${report.independent_signal_days_upper_bound} / 50`},
        ]}/>
        <Alert type="warning" showIcon message={t('productionAdmission.waiting')} description={t('productionAdmission.waitingHelp')}/>
        <Space wrap><Button disabled={busy} onClick={()=>setStep(0)}>{t('productionAdmission.back')}</Button><Button loading={busy} onClick={()=>void load()}>{t('productionAdmission.refresh')}</Button></Space>
        {report.bundle.raw_trade_dates>=274&&<Button type="primary" onClick={()=>setStep(3)}>{t('productionCandidate.prepare')}</Button>}
      </>}
      {step===3&&modelId&&bundle&&report?.frozen_plan&&<>
        <ProductionCandidateStep modelId={modelId} bundleId={bundle} planId={report.frozen_plan.plan_id}/>
        <Button onClick={()=>setStep(2)}>{t('productionAdmission.back')}</Button>
      </>}
    </Space>
  </Modal>;
}
