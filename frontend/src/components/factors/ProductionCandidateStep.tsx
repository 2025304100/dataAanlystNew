import {useEffect,useRef,useState} from 'react';
import {Alert,Button,Descriptions,Progress,Select,Space,Typography} from 'antd';
import {api} from '../../api/client';
import {t} from '../../i18n';
import type {AsyncTaskRead,Portfolio} from '../../types';
import type {ProductionExecutionReview,ProductionPredictionStatus} from '../../types/productionAdmission';

export default function ProductionCandidateStep({modelId,bundleId,planId}:{modelId:string;bundleId:string;planId:string}) {
  const [portfolios,setPortfolios]=useState<Portfolio[]>([]);
  const [portfolio,setPortfolio]=useState<number>();
  const [review,setReview]=useState<ProductionExecutionReview>();
  const [task,setTask]=useState<AsyncTaskRead>();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string>();
  const [prediction,setPrediction]=useState<ProductionPredictionStatus>();
  const [predictionError,setPredictionError]=useState<string>();
  const generation=useRef(0);
  useEffect(()=>{
    const current=++generation.current;setReview(undefined);setTask(undefined);setError(undefined);setBusy(true);
    void Promise.all([api.getPortfolios(),api.getProductionCandidateTask(planId).catch(reason=>{
      if(String(reason).includes('production_execution_task_not_found'))return undefined;
      throw reason;
    })]).then(([items,existing])=>{if(current===generation.current){
      setPortfolios(items);setPortfolio(items.find(p=>p.is_default)?.id??items[0]?.id);setTask(existing);
    }}).catch(reason=>{if(current===generation.current)setError(String(reason));})
      .finally(()=>{if(current===generation.current)setBusy(false);});
    return()=>{++generation.current;};
  },[modelId,bundleId,planId]);
  useEffect(()=>{
    if(!task||!['queued','running','cancelling'].includes(task.status))return;
    const current=generation.current;
    const timer=window.setTimeout(()=>{void api.getProductionCandidateTask(planId)
      .then(next=>{if(current===generation.current){setTask(next);setError(undefined);}})
      .catch(reason=>{if(current===generation.current)setError(String(reason));});},3000);
    return()=>window.clearTimeout(timer);
  },[task,planId]);
  useEffect(()=>{
    setPrediction(undefined);setPredictionError(undefined);
    if(task?.status!=='done')return;
    let cancelled=false;
    void api.getProductionPredictionStatus(planId,bundleId).then(next=>{
      if(next.plan_id!==planId)throw new Error(t('productionAdmission.mismatch'));
      if(!cancelled)setPrediction(next);
    }).catch(reason=>{if(!cancelled)setPredictionError(String(reason));});
    return()=>{cancelled=true;};
  },[task,planId,bundleId]);
  const recordPrediction=async()=>{
    if(busy||!prediction?.can_record_today)return;
    const current=generation.current;setBusy(true);setPredictionError(undefined);
    try {
      await api.recordProductionPrediction(planId,bundleId);
      const next=await api.getProductionPredictionStatus(planId,bundleId);
      if(current===generation.current)setPrediction(next);
    }catch(reason){
      // A lost save response is resolved by reading; never repeat the POST.
      try {
        const recovered=await api.getProductionPredictionStatus(planId,bundleId);
        if(recovered.plan_id!==planId||recovered.journal_count<=prediction.journal_count)throw reason;
        if(current===generation.current)setPrediction(recovered);
      }catch {if(current===generation.current)setPredictionError(String(reason));}
    }
    finally{if(current===generation.current)setBusy(false);}
  };
  const reviewCosts=async()=>{
    if(portfolio==null||busy)return;
    const current=++generation.current;setBusy(true);setError(undefined);setReview(undefined);
    try {const next=await api.reviewProductionCandidate(modelId,bundleId,portfolio);
      if(current===generation.current){if(next.contract.plan_id!==planId||next.contract.cost.portfolio_id!==portfolio)throw new Error(t('productionAdmission.mismatch'));setReview(next);}}
    catch(reason){if(current===generation.current)setError(String(reason));}
    finally{if(current===generation.current)setBusy(false);}
  };
  const submit=async()=>{
    if(!review||portfolio==null||busy||task)return;
    const current=++generation.current;setBusy(true);setError(undefined);
    try {const next=await api.submitProductionCandidate(modelId,bundleId,portfolio,review.sha256);
      if(current===generation.current)setTask(next);}
    catch(reason){
      // A lost response is not permission to train a second candidate.
      try {const existing=await api.getProductionCandidateTask(planId);if(current===generation.current)setTask(existing);}
      catch {if(current===generation.current){setReview(undefined);setError(t('productionCandidate.unknown')+' '+String(reason));}}
    }finally{if(current===generation.current)setBusy(false);}
  };
  const cost=review?.contract.cost;
  return <Space direction="vertical" style={{width:'100%'}} size={14}>
    <Alert type="info" showIcon message={t('productionCandidate.title')} description={t('productionCandidate.help')}/>
    {error&&<Alert type="error" showIcon message={error}/>}
    {!task&&<>
      <label>{t('productionCandidate.portfolio')}<Select aria-label={t('productionCandidate.portfolio')} style={{width:'100%'}} disabled={busy} value={portfolio} options={portfolios.map(p=>({value:p.id,label:p.name}))} onChange={value=>{++generation.current;setPortfolio(value);setReview(undefined);}}/></label>
      <Button loading={busy} disabled={portfolio==null} onClick={()=>void reviewCosts()}>{t('productionCandidate.review')}</Button>
      {cost&&<>
        <Descriptions bordered column={1} size="small" items={[
          {key:'capital',label:t('productionCandidate.capital'),children:`${cost.evaluation_capital.toLocaleString()} ${cost.currency}`},
          {key:'cutoff',label:t('productionCandidate.cutoff'),children:review.contract.training_cutoff},
          ...Object.entries(cost.fees).map(([key,value])=>({key,label:t(`productionCandidate.fee.${key}`),children:<span title={cost.provenance[key]}>{value}</span>})),
          {key:'source',label:t('productionCandidate.source'),children:cost.source_snapshot_id??t('productionCandidate.portfolioFallback')},
        ]}/>
        <Alert type="warning" showIcon message={t('productionCandidate.lockHelp')}/>
        <Button type="primary" loading={busy} onClick={()=>void submit()}>{t('productionCandidate.train')}</Button>
      </>}
    </>}
    {task&&<>
      <Typography.Text copyable>{task.id}</Typography.Text>
      <Progress percent={task.percent} status={task.status==='failed'?'exception':task.status==='done'?'success':'active'}/>
      <Alert showIcon type={task.status==='failed'?'error':task.status==='done'?'success':'info'} message={t(`productionCandidate.status.${task.status}`)} description={task.message}/>
      {task.status==='done'&&<Alert type="warning" showIcon message={t('productionCandidate.pending')}/>}
      {prediction&&<Alert type="info" showIcon message={t('productionCandidate.journalCount').replace('{count}',String(prediction.journal_count))}
        description={t('productionCandidate.journalHelp').replace('{boundary}',prediction.independent_after)}/>}
      {prediction?.can_record_today&&<Button loading={busy} onClick={()=>void recordPrediction()}>{t('productionCandidate.recordToday')}</Button>}
      {predictionError&&<Alert type="warning" showIcon message={t('productionCandidate.journalReadError')} description={predictionError}/>}
      <Button onClick={()=>{void api.getProductionCandidateTask(planId).then(setTask).catch(reason=>setError(String(reason)));}}>{t('productionAdmission.refresh')}</Button>
    </>}
  </Space>;
}
