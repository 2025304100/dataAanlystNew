import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from 'antd';
import {inspectionIsVerifying,inspectionReviewCurrent,subscribeInspectionVerification,subscribeInspectionChanges} from '../../../utils/inspectionFreshness';

export default function useInspectionFreshness(portfolioId: number, onInvalidate?: () => void) {
  const revision = useRef(0);
  const staleRef = useRef(false);
  const callback = useRef(onInvalidate); callback.current = onInvalidate;
  const [stale,setStale] = useState(false);
  const [checking,setChecking]=useState(()=>inspectionIsVerifying(portfolioId));
  useEffect(()=>subscribeInspectionVerification(portfolioId,()=>setChecking(inspectionIsVerifying(portfolioId))),[portfolioId]);
  useEffect(() => subscribeInspectionChanges(portfolioId, () => {
    revision.current++; staleRef.current = true; setStale(true); callback.current?.();
  }), [portfolioId]);
  const capture = useCallback(() => revision.current, []);
  const isStale = useCallback(() => staleRef.current||inspectionIsVerifying(portfolioId)||!inspectionReviewCurrent(portfolioId), [portfolioId]);
  const complete = useCallback((started: number) => {
    if (started !== revision.current||navigator.onLine===false) return false;
    staleRef.current = false; setStale(false); return true;
  }, []);
  return { stale, checking, capture, complete, isStale };
}

export function InspectionStaleNotice({onReload,loading=false}:{onReload:()=>void;loading?:boolean}) {
  return <div role="alert" className="pi-callout pi-warning" data-testid="inspection-stale-notice"><strong>当前结果已过期，请重新读取</strong><p>后台任务或其他页面可能已更新相关数据与运行依据，或暂时无法确认最新状态。旧报告暂停审核；重新读取成功后，请核实新的结论并重新确认。日期、筛选与浏览位置保留。</p><Button loading={loading} onClick={onReload}>重新读取当前依据</Button></div>;
}
