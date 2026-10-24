import {afterEach,expect,it} from "vitest";
import {cleanup,fireEvent,render,waitFor} from "@testing-library/react";
import InspectionDrawer from "../InspectionDrawer";
import {readInspectionBrowse,writeInspectionBrowse} from "../inspectionBrowseState";

afterEach(()=>{cleanup();sessionStorage.clear();});

it("preserves parent scroll while covered and resets scroll for a different subject",async()=>{
  const view=render(<InspectionDrawer open title="查看详情" onClose={()=>{}} scrollKey="first"><p>原对象</p></InspectionDrawer>);
  const body=document.querySelector('.ant-drawer-body') as HTMLElement;
  await waitFor(()=>expect(body.scrollTop).toBe(0));
  body.scrollTop=240;fireEvent.scroll(body);
  view.rerender(<InspectionDrawer open covered title="查看详情" onClose={()=>{}} scrollKey="first"><p>原对象</p></InspectionDrawer>);
  body.scrollTop=0;
  view.rerender(<InspectionDrawer open title="查看详情" onClose={()=>{}} scrollKey="first"><p>原对象</p></InspectionDrawer>);
  await waitFor(()=>expect(body.scrollTop).toBe(240));
  view.rerender(<InspectionDrawer open title="查看详情" onClose={()=>{}} scrollKey="second"><p>另一个对象</p></InspectionDrawer>);
  await waitFor(()=>expect(body.scrollTop).toBe(0));
});

it("keeps saved disclosures while async content is loading and restores them when rows arrive",async()=>{
  writeInspectionBrowse(1,"position:async",{top:0,open:[{index:0,label:"历史缺口"}]});
  const view=render(<InspectionDrawer open title="详情" onClose={()=>{}} browseScope={{portfolioId:1,key:"async"}}><p>正在读取</p></InspectionDrawer>);
  view.rerender(<InspectionDrawer open title="详情" onClose={()=>{}} browseScope={{portfolioId:1,key:"async"}}><details><summary>历史缺口</summary><p>来源</p></details></InspectionDrawer>);
  await waitFor(()=>expect(document.querySelector('details')!.open).toBe(true));
});

it("restores presentation scroll and disclosures after remount only for the same portfolio",async()=>{
  const content=<details><summary>来源明细</summary><p>实际来源</p></details>;
  const first=render(<InspectionDrawer open title="详情" onClose={()=>{}} browseScope={{portfolioId:1,key:"source"}}>{content}</InspectionDrawer>);
  const body=document.querySelector('.ant-drawer-body') as HTMLElement;
  body.scrollTop=180;fireEvent.scroll(body);const section=body.querySelector('details')!;section.open=true;fireEvent(window,new Event('pagehide'));
  await waitFor(()=>expect(readInspectionBrowse<{top:number}>(1,"position:source")?.top).toBe(180));first.unmount();
  const second=render(<InspectionDrawer open title="详情" onClose={()=>{}} browseScope={{portfolioId:2,key:"source"}}>{content}</InspectionDrawer>);
  expect((document.querySelector('.ant-drawer-body') as HTMLElement).scrollTop).toBe(0);second.unmount();
  render(<InspectionDrawer open title="详情" onClose={()=>{}} browseScope={{portfolioId:1,key:"source"}}>{content}</InspectionDrawer>);
  await waitFor(()=>expect((document.querySelector('.ant-drawer-body') as HTMLElement).scrollTop).toBe(180));
  expect(document.querySelector('details')!.open).toBe(true);
});
