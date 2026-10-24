import {afterEach,expect,it,vi} from 'vitest';
import {render,screen,fireEvent,cleanup,act} from '@testing-library/react';
import {AcceptanceButton,AcceptanceModal,AcceptanceNotice} from '../AcceptanceControls';
import {startAcceptanceMonitor,refreshAcceptanceMode} from '../../../../utils/acceptanceMode';
let stop=()=>{};
afterEach(()=>{cleanup();stop();vi.restoreAllMocks();vi.unstubAllGlobals();});
async function readonly(){vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,json:async()=>({mode:'inspection_acceptance',read_only:true})}));await act(async()=>{stop=startAcceptanceMonitor();await refreshAcceptanceMode();});}
it('shows a visible reason and disables only mutation intents while leaving history and reading enabled',async()=>{
  await readonly();const write=vi.fn(),read=vi.fn();render(<><AcceptanceNotice/><AcceptanceButton mutation onClick={write}>登记材料</AcceptanceButton><AcceptanceButton onClick={read}>查看历史</AcceptanceButton></>);
  expect(screen.getByText('只读验收模式')).toBeInTheDocument();const button=screen.getByRole('button',{name:'登记材料'});expect(button).toBeDisabled();expect(button).toHaveAttribute('title',expect.stringContaining('只读验收'));
  fireEvent.click(button);fireEvent.click(screen.getByRole('button',{name:'查看历史'}));expect(write).not.toHaveBeenCalled();expect(read).toHaveBeenCalledOnce();
});
it('blocks checking/failed mode before server confirmation and never unlocks on 404',async()=>{
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:404}));await act(async()=>{stop=startAcceptanceMonitor();});
  render(<><AcceptanceNotice/><AcceptanceButton mutation>重新核对</AcceptanceButton><AcceptanceButton>关闭</AcceptanceButton></>);
  await act(async()=>{await refreshAcceptanceMode();});expect(screen.getByRole('button',{name:'重新核对'})).toBeDisabled();expect(screen.getByRole('button',{name:/关\s*闭/})).toBeEnabled();
});
it('disables confirmation inside an already open modal while preserving close and the reason',async()=>{
  await readonly();const confirm=vi.fn();render(<AcceptanceModal mutation open title="确认登记" onOk={confirm} onCancel={vi.fn()} okText="保存本次审核" cancelText="关闭">限定材料</AcceptanceModal>);
  expect(screen.getByRole('button',{name:'保存本次审核'})).toBeDisabled();expect(screen.getByRole('button',{name:/^关\s*闭$/})).toBeEnabled();expect(screen.getByText('只读验收模式')).toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'保存本次审核'}));expect(confirm).not.toHaveBeenCalled();
});
it('same global mode survives a portfolio view remount and normal mode still honors original disabled rules',async()=>{
  await readonly();const view=render(<AcceptanceButton key={1} mutation>修复组合一</AcceptanceButton>);view.rerender(<AcceptanceButton key={2} mutation>修复组合二</AcceptanceButton>);expect(screen.getByRole('button',{name:'修复组合二'})).toBeDisabled();
  await act(async()=>{vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,json:async()=>({mode:'application',read_only:false})}));await refreshAcceptanceMode();});view.rerender(<AcceptanceButton mutation disabled>业务条件未满足</AcceptanceButton>);expect(screen.getByRole('button',{name:'业务条件未满足'})).toBeDisabled();
});


