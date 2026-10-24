import {AcceptanceButton as Button} from './AcceptanceControls';
import {useCallback,useEffect,useRef,useState} from 'react';

import {api} from '../../../api/client';
import {readRecheckSubmission,recoverRecheckSubmission,subscribeRecheckSubmission,type RecheckReceipt} from './recheckSubmissionJournal';

export default function InspectionRecheckRequest({portfolioId,onRetry}:{portfolioId:number;onRetry:()=>void}){
  const [receipt,setReceipt]=useState<RecheckReceipt|null>(null),[error,setError]=useState<string|null>(null),[exists,setExists]=useState(false),[loading,setLoading]=useState(false),[notFound,setNotFound]=useState(false);
  const sequence=useRef(0);
  const load=useCallback(async()=>{const seq=++sequence.current;setLoading(true);setError(null);setNotFound(false);
    try{const record=readRecheckSubmission(portfolioId);setExists(Boolean(record));setReceipt(null);if(!record)return;
      const result=await recoverRecheckSubmission(record,api.getPortfolioRecheckRequest);
      if(sequence.current===seq)setReceipt(result);
    }catch(e){if(sequence.current===seq){setExists(true);setError(e instanceof Error?e.message:'原请求读取失败，记录继续保留。');const failure=e as {status_code?:number;error_code?:string;detail?:{error_code?:string}};setNotFound(failure.status_code===404&&(failure.error_code??failure.detail?.error_code)==='RECHECK_REQUEST_NOT_FOUND');}}
    finally{if(sequence.current===seq)setLoading(false);}
  },[portfolioId]);
  useEffect(()=>{void load();const read=()=>void load();const unsub=subscribeRecheckSubmission(portfolioId,read);
    const poll=window.setInterval(()=>{if(document.visibilityState!=='hidden'&&navigator.onLine!==false)read();},10000);
    window.addEventListener('online',read);window.addEventListener('focus',read);window.addEventListener('pageshow',read);
    return()=>{sequence.current++;unsub();window.clearInterval(poll);window.removeEventListener('online',read);window.removeEventListener('focus',read);window.removeEventListener('pageshow',read);};
  },[load]);
  if(!exists)return null;
  return <section className="pi-callout" aria-label="原对账请求恢复"><strong>原对账请求与任务</strong>
    <p>{receipt?`${receipt.trade_date??'最早待核对日期'} · ${receipt.task_id&&receipt.task_status?'已找回原请求对应任务':'重检需求已登记，尚未确认任务派发'}`:'原请求已保留，重新进入只读取回执，不自动重新提交。'}</p>
    {receipt?.task_status&&<p>原任务状态：{({queued:'等待执行',running:'核对中',done:'核对结束',failed:'执行失败',partial:'部分完成',cancelled:'已取消'} as Record<string,string>)[receipt.task_status]??'请查看作业进度'}</p>}
    {receipt?.message&&<p>{receipt.message}</p>}{error&&<p role="alert">{error}</p>}
    {receipt?.provenance_annotations?.map((note,index)=><div className="pi-warning-text" key={index}><strong>{note.label}</strong><p>{note.note}</p></div>)}
    <Button loading={loading} onClick={()=>void load()}>重新读取原对账请求</Button>
    {notFound&&<Button mutation onClick={onRetry}>使用原请求重新核对</Button>}
    <small>仅恢复请求关联；任务完成不代表账差通过，也不会改变运行权限。</small>
  </section>;
}
