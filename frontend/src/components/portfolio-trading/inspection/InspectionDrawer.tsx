import { useLayoutEffect, useRef, type ReactNode } from "react";
import { Button, Drawer } from "antd";
import { ArrowLeft, ClipboardCheck, X } from "lucide-react";
import useInspectionPalette from "./useInspectionPalette";
import useInspectionBrowsePosition,{type BrowseScope} from "./useInspectionBrowsePosition";
import "./inspectionSurfaces.css";
import {AcceptanceNotice} from './AcceptanceControls';

interface Props {
  open: boolean;
  active?: boolean;
  title: ReactNode;
  context?: string;
  backLabel?: string;
  onBack?: () => void;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
  zIndex?: number;
  afterOpenChange?: (open: boolean) => void;
  initialScroll?: number;
  onScrollChange?: (top: number) => void;
  focused?: boolean;
  icon?: ReactNode;
  covered?: boolean;
  scrollKey?: string;
  browseScope?: BrowseScope;
}

/** A separate overlay per subject keeps the parent list and its scroll intact. */
export default function InspectionDrawer({ open, active = true, title, context, backLabel, onBack, onClose, footer, children,
  zIndex, afterOpenChange, initialScroll = 0, onScrollChange, focused = false, icon, covered = false, scrollKey, browseScope }: Props) {
  const content = useRef<HTMLDivElement>(null);
  const palette=useInspectionPalette();
  const scroll = useRef(initialScroll);
  const scrollScope = useRef(scrollKey);
  const trigger = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  const initialized = useRef(false);
  const notifyScroll = useRef(onScrollChange);
  notifyScroll.current = onScrollChange;
  useInspectionBrowsePosition(content,browseScope,Boolean(browseScope&&open&&active&&!covered),initialScroll,onScrollChange);
  useLayoutEffect(() => {
    if (scrollScope.current !== scrollKey) { scrollScope.current = scrollKey; scroll.current = initialScroll; }
  }, [scrollKey, initialScroll]);
  useLayoutEffect(() => {
    if (open && !wasOpen.current) {
      trigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      if(!initialized.current){scroll.current=initialScroll;initialized.current=true;}
    }
    wasOpen.current = open;
  }, [open]);
  useLayoutEffect(() => {
    if (!open || !active || covered || browseScope) return;
    const body = content.current?.closest<HTMLElement>(".ant-drawer-body");
    const frame = requestAnimationFrame(() => { if (body) body.scrollTop = scroll.current; });
    const remember = () => { if (body) { scroll.current=body.scrollTop; notifyScroll.current?.(scroll.current); } };
    body?.addEventListener("scroll",remember);
    return () => { cancelAnimationFrame(frame); body?.removeEventListener("scroll",remember); };
  }, [open, active, covered, scrollKey, Boolean(browseScope)]);
  return <Drawer className={`pi-inspection-drawer${focused ? " pi-drawer-focused" : ""}`} rootClassName="pi-inspection-drawer-root" width={760}
    open={open} zIndex={zIndex} mask={active&&!covered} keyboard={active&&!covered} maskClosable={active&&!covered}
    aria-hidden={covered||undefined}
    rootStyle={{...palette,...(active?{}:{visibility:"hidden",pointerEvents:"none"})}} styles={{content:{...palette,...(covered?{pointerEvents:"none" as const}:{})}}}
    closeIcon={focused ? <X size={18} aria-hidden="true"/> : undefined}
    title={focused ? <div className="pi-drawer-title"><span className="pi-drawer-title-icon" aria-hidden="true">{icon ?? <ClipboardCheck size={20}/>}</span><div>{context && <small>{context}</small>}<h2>{title}</h2></div></div> : <div>{context && <small>{context}</small>}<h2>{title}</h2></div>}
    onClose={()=>{if(open&&active&&!covered)onClose();}} destroyOnHidden={false} forceRender
    extra={focused&&!onBack?undefined:<div className="pi-overlay-navigation">{onBack && <Button type={focused ? "text" : "default"} icon={focused ? <ArrowLeft size={14} aria-hidden="true"/> : undefined} onClick={onBack}>{backLabel ?? "返回上一步"}</Button>}{!focused && <Button onClick={onClose}>关闭</Button>}</div>}
    footer={footer} afterOpenChange={shown => {
      afterOpenChange?.(shown);
      if (shown && active && !covered && !browseScope) {
        const body = content.current?.closest<HTMLElement>(".ant-drawer-body");
        if (body) body.scrollTop = scroll.current;
      }
      if (!shown && trigger.current?.isConnected) trigger.current.focus({preventScroll:true});
    }}>
    <div ref={content} style={palette} className="pi-workbench pi-drawer-content"><AcceptanceNotice compact/>{children}</div>
  </Drawer>;
}
