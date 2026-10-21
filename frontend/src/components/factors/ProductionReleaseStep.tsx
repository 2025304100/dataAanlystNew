import {useEffect,useRef,useState} from 'react';
import {Alert,Button,Checkbox,Descriptions,Space} from 'antd';
import {api} from '../../api/client';
import {t} from '../../i18n';
import type {AsyncTaskRead} from '../../types';
import type {ProductionIndependentReview,ProductionReleaseReview} from '../../types/productionAdmission';

function readableError(reason:unknown) {
  const message=reason instanceof Error?reason.message:String(reason);
  return /[\u4e00-\u9fff]/.test(message)?message:t('productionCandidate.releaseRetryHelp');
}

export default function ProductionReleaseStep({planId,bundleId}:{planId:string;bundleId:string}) {
  const [review,setReview]=useState<ProductionReleaseReview>();
  const [independent,setIndependent]=useState<ProductionIndependentReview>();
  const [task,setTask]=useState<AsyncTaskRead>();
  const [checked,setChecked]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string>();
  const generation=useRef(0);
  const load=async(current=generation.current)=>{
    const [releaseResult,independentResult]=await Promise.allSettled([
      api.getProductionReleaseReview(planId,bundleId),api.getProductionIndependentReview(planId,bundleId),
    ]);
    if(current!==generation.current)return;
    if(releaseResult.status==='rejected')throw releaseResult.reason;
    const next=releaseResult.value;
    if(next.plan_id!==planId)throw new Error(t('productionAdmission.mismatch'));
    setReview(next);setChecked(false);
    if(independentResult.status==='fulfilled'){
      setIndependent(independentResult.value);setError(undefined);
    }else{
      setIndependent(undefined);setError(next.blockers.length?undefined:readableError(independentResult.reason));
    }
  };
  useEffect(()=>{
    const current=++generation.current;setReview(undefined);setIndependent(undefined);setTask(undefined);setChecked(false);setBusy(true);
    void load(current).catch(reason=>{if(current===generation.current)setError(readableError(reason));})
      .finally(()=>{if(current===generation.current)setBusy(false);});
    return()=>{++generation.current;};
  },[planId,bundleId]);
  useEffect(()=>{
    if(!task||!['queued','running','cancelling'].includes(task.status))return;
    const current=generation.current;
    const timer=window.setTimeout(()=>void api.getProductionIndependentTask(task.id).then(async next=>{
      if(current!==generation.current)return;
      setTask(next);setError(undefined);
      if(next.status==='done')await load(current);
    }).catch(reason=>{if(current===generation.current)setError(readableError(reason));}),3000);
    return()=>window.clearTimeout(timer);
  },[task,planId,bundleId]);
  const refresh=async()=>{
    const current=generation.current;setBusy(true);setError(undefined);
    try{if(task){const next=await api.getProductionIndependentTask(task.id);if(current===generation.current)setTask(next);}await load(current);}
    catch(reason){if(current===generation.current)setError(readableError(reason));}
    finally{if(current===generation.current)setBusy(false);}
  };
  const evaluate=async()=>{
    if(!independent?.ready||busy)return;
    const current=generation.current;setBusy(true);setError(undefined);
    try{const next=await api.submitProductionIndependentEvaluation(planId,bundleId,independent.sha256);
      if(current===generation.current)setTask(next);}
    catch{if(current===generation.current)setError(t('productionCandidate.releaseEvaluationUnknown'));}
    finally{if(current===generation.current)setBusy(false);}
  };
  const confirm=async()=>{
    if(!review?.ready||!checked||busy)return;
    const current=generation.current;setBusy(true);setError(undefined);
    try{const next=await api.confirmProductionRelease(planId,bundleId,review.sha256);
      if(next.plan_id!==planId)throw new Error(t('productionAdmission.mismatch'));
      if(current===generation.current){setReview(next);setChecked(false);}}
    catch(reason){
      // Resolve an unknown save by reading, never repeat the confirmation POST.
      try{const recovered=await api.getProductionReleaseReview(planId,bundleId);
        if(recovered.plan_id!==planId||!recovered.qualification_issued)throw reason;
        if(current===generation.current){setReview(recovered);setChecked(false);}}
      catch{if(current===generation.current){setChecked(false);setError(readableError(reason));}}
    }finally{if(current===generation.current)setBusy(false);}
  };
  const missing=(code:string)=>{
    if(code==='FIRST_50_T_PLUS_5_OUTCOMES_NOT_MATURE')return t('productionCandidate.releaseMaturing').replace('{count}',String(independent?.mature_signal_count??0));
    if(code==='FIRST_50_POSTBOUNDARY_SIGNAL_DAYS_REQUIRED')return t('productionCandidate.releaseNeedDates');
    if(code==='MISSING_PROSPECTIVE_JOURNALS_NO_DATE_SUBSTITUTION')return t('productionCandidate.releaseMissingPredictions').replace('{count}',String(independent?.missing_journal_dates.length??0));
    return code;
  };
  return <Space direction="vertical" size={12} style={{width:'100%'}}>
    {error&&<Alert type="error" showIcon message={t('productionCandidate.releaseReadError')} description={error}/>}
    {review?.qualification_issued?<Alert type="success" showIcon
      message={review.model_activated?t('productionCandidate.releaseDone'):t('productionCandidate.releaseConfirmed')}
      description={review.message}/>:<>
      {review?.blockers.map((code,index)=><Alert key={index} type="info" showIcon message={missing(code)}/>)}
      {review?.ready&&<>
        <Descriptions size="small" bordered column={1} items={[
          {key:'model',label:t('productionCandidate.releaseModel'),children:review.candidate_model_id},
          {key:'dates',label:t('productionCandidate.releaseDates'),children:'50'},
          {key:'ic',label:t('productionCandidate.releaseCorrelation'),children:review.result?.mean_daily_ic.toFixed(4)},
          {key:'net',label:t('productionCandidate.releaseNet'),children:review.result?`${(review.result.net_return*100).toFixed(2)}%`:''},
        ]}/>
        <Alert type="warning" showIcon message={t('productionCandidate.releaseEffect')}/>
        <Checkbox disabled={busy} checked={checked} onChange={event=>setChecked(event.target.checked)}>{t('productionCandidate.releaseCheck')}</Checkbox>
        <Button type="primary" loading={busy} disabled={!checked} onClick={()=>void confirm()}>{t('productionCandidate.releaseConfirm')}</Button>
      </>}
      {independent?.ready&&!review?.ready&&<Button loading={busy} disabled={!!task&&['queued','running','cancelling'].includes(task.status)} onClick={()=>void evaluate()}>{t('productionCandidate.releaseEvaluate')}</Button>}
      {task&&<Alert type={task.status==='failed'?'error':'info'} showIcon message={task.message}/>}
    </>}
    <Button loading={busy} onClick={()=>void refresh()}>{t('productionCandidate.releaseRefresh')}</Button>
  </Space>;
}
