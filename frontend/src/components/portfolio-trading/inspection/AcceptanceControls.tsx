import {forwardRef} from 'react';
import {Button as AntButton,Modal as AntModal,type ButtonProps,type ModalProps,type GetRef} from 'antd';
import {LockKeyhole,RefreshCw} from 'lucide-react';
import {useAcceptanceMode,acceptanceMutationReason,refreshAcceptanceMode} from '../../../utils/acceptanceMode';
import './acceptanceMode.css';

export const AcceptanceButton=forwardRef<GetRef<typeof AntButton>,ButtonProps&{mutation?:boolean}>(function AcceptanceButton({mutation=false,disabled,onClick,...props},ref){
  const mode=useAcceptanceMode(),blocked=mutation&&mode.blocked;
  return <AntButton {...props} ref={ref} disabled={disabled||blocked} title={blocked?mode.reason:props.title}
    aria-description={blocked?mode.reason:undefined} data-acceptance-mutation={mutation||undefined}
    onClick={event=>{if(mutation&&acceptanceMutationReason()){event.preventDefault();event.stopPropagation();return;}onClick?.(event);}}/>;
});
export function AcceptanceModal({mutation=false,okButtonProps,onOk,children,...props}:ModalProps&{mutation?:boolean}){
  const mode=useAcceptanceMode();
  return <AntModal {...props} okButtonProps={{...okButtonProps,disabled:okButtonProps?.disabled||mutation&&mode.blocked,title:mutation&&mode.blocked?mode.reason:okButtonProps?.title}}
    onOk={event=>{if(mutation&&acceptanceMutationReason())return;onOk?.(event);}}><AcceptanceNotice compact/>{children}</AntModal>;
}
export function AcceptanceNotice({compact=false}:{compact?:boolean}){
  const mode=useAcceptanceMode();
  if(mode.state==='normal')return null;
  return <aside role="status" aria-label="验收服务模式" className={`pi-acceptance-mode${compact?' pi-acceptance-compact':''}`}>
    <LockKeyhole size={18} aria-hidden="true"/><div><strong>{mode.readOnly?'只读验收模式':mode.state==='checking'?'正在核实服务模式':'服务模式暂不可读'}</strong><p>{mode.reason}</p>{mode.readOnly&&mode.state==='unavailable'&&<small>服务暂不可读，继续保持只读保护。</small>}</div>
    <AntButton size="small" type="text" icon={<RefreshCw size={14}/>} loading={mode.state==='checking'} onClick={()=>void refreshAcceptanceMode()}>重新读取模式</AntButton>
  </aside>;
}
